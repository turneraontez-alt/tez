"""Repository: migrations, idempotent writes (the dedup claims), bankroll,
settlement state machine, calibration versioning. In-memory SQLite."""
from datetime import timedelta, timezone

import pytest

from tests.tt_edge_fixtures import NOW, make_repo
from tt_edge.db.repo import RepoError
from tt_edge.model.calibration import fit_platt
from tt_edge.scrapers.events import ParsedEvent
from tt_edge.scrapers.odds import OddsSnapshot


def _event(event_id="9001"):
    return ParsedEvent(source_event_id=event_id, home_id="101",
                       home_name="Marud", away_id="102",
                       away_name="Gumulinski", start_time=NOW,
                       status_type="notstarted", winner_code=None,
                       tournament_name="TT Elite Series")


def _rec(repo, match_id="9001", day="2026-07-18", **overrides):
    kwargs = dict(match_source_id=match_id, market="ML", side="home",
                  pick_name="Marud", model_p="0.58", fair_p="0.5217",
                  edge="0.0583", price_american=-120, stake_cents=123,
                  bankroll_cents=6500, recommended_at=NOW,
                  recommended_on=day, basis_json="{}", data_ages_json="{}")
    kwargs.update(overrides)
    return repo.insert_recommendation(**kwargs)


class TestMigrations:
    def test_apply_twice_is_idempotent(self):
        repo = make_repo()               # first apply inside
        assert repo.apply_migrations() == []

    def test_first_apply_runs_init(self):
        from tt_edge.db import repo as repo_mod
        repo = repo_mod.connect(":memory:")
        assert repo.apply_migrations() == ["001_init.sql"]


class TestOddsSnapshots:
    def test_append_and_dedup_same_instant(self):
        repo = make_repo()
        snap = OddsSnapshot("9001", "manual", -120, 100, NOW)
        assert repo.insert_odds_snapshot(snap) is True
        assert repo.insert_odds_snapshot(snap) is False
        assert len(repo.odds_history("9001")) == 1

    def test_history_ordered_by_capture_time(self):
        repo = make_repo()
        late = OddsSnapshot("9001", "manual", -135, 115, NOW)
        early = OddsSnapshot("9001", "manual", -120, 100,
                             NOW - timedelta(minutes=30))
        repo.insert_odds_snapshot(late)
        repo.insert_odds_snapshot(early)
        history = repo.odds_history("9001")
        assert [s.home_price for s in history] == [-120, -135]
        assert history[0].captured_at.tzinfo == timezone.utc

    def test_history_filters_by_book(self):
        repo = make_repo()
        repo.insert_odds_snapshot(OddsSnapshot("9001", "manual", -120, 100,
                                               NOW))
        repo.insert_odds_snapshot(OddsSnapshot("9001", "sharp", -140, 120,
                                               NOW))
        manual_only = repo.odds_history("9001", "manual")
        assert [s.book for s in manual_only] == ["manual"]
        assert len(repo.odds_history("9001")) == 2


class TestPredictions:
    def test_one_row_per_match_market_day(self):
        repo = make_repo()
        kwargs = dict(match_source_id="9001", market="ML",
                      predicted_on="2026-07-18", model_p_home="0.58",
                      fair_p_home="0.52", decision="RECOMMEND",
                      reasons_json="[]", created_at=NOW)
        assert repo.insert_prediction(**kwargs) is True
        assert repo.insert_prediction(**kwargs) is False

    def test_grading_pairs_flow(self):
        repo = make_repo()
        repo.insert_prediction(match_source_id="9001", market="ML",
                               predicted_on="2026-07-18",
                               model_p_home="0.58", fair_p_home=None,
                               decision="NO_BET", reasons_json='["no_odds"]',
                               created_at=NOW)
        repo.record_result(match_source_id="9001", winner_side="home",
                           home_score=3, away_score=1, source="manual",
                           recorded_at=NOW)
        rows = repo.ungraded_predictions_with_results()
        assert len(rows) == 1
        repo.grade_prediction(int(rows[0]["id"]), 1, NOW)
        assert repo.ungraded_predictions_with_results() == []
        assert repo.graded_prediction_pairs() == [(0.58, 1.0)]


class TestRecommendations:
    def test_same_day_claim_is_exclusive(self):
        repo = make_repo()
        assert _rec(repo) is not None
        assert _rec(repo) is None                       # duplicate rescan
        assert _rec(repo, day="2026-07-19") is not None  # next day is new

    def test_mark_delivered_only_once(self):
        repo = make_repo()
        rec_id = _rec(repo)
        repo.mark_alert_delivered(rec_id, NOW)
        first = repo.recommendation_by_key("9001", "ML", "2026-07-18")
        repo.mark_alert_delivered(rec_id, NOW + timedelta(hours=1))
        second = repo.recommendation_by_key("9001", "ML", "2026-07-18")
        assert first["alert_delivered_at"] == second["alert_delivered_at"]
        assert first["alert_delivered_at"].tzinfo == timezone.utc

    def test_open_count_tracks_settlement(self):
        repo = make_repo()
        rec_id = _rec(repo)
        _rec(repo, match_id="9002")
        assert repo.open_recommendation_count() == 2
        assert repo.settle_recommendation(rec_id, "won", 270, NOW) is True
        assert repo.open_recommendation_count() == 1

    def test_settle_is_idempotent(self):
        repo = make_repo()
        rec_id = _rec(repo)
        assert repo.settle_recommendation(rec_id, "won", 270, NOW) is True
        assert repo.settle_recommendation(rec_id, "lost", -123, NOW) is False
        row = repo.recommendation_by_key("9001", "ML", "2026-07-18")
        assert row["status"] == "won"
        assert row["profit_cents"] == 270

    def test_settle_rejects_unknown_status(self):
        repo = make_repo()
        rec_id = _rec(repo)
        with pytest.raises(RepoError):
            repo.settle_recommendation(rec_id, "push", 0, NOW)

    def test_settle_batch_moves_bankroll_only_by_applied_rows(self):
        repo = make_repo()
        repo.set_bankroll_cents(6500, NOW)
        first = _rec(repo)
        second = _rec(repo, match_id="9002")
        repo.settle_recommendation(first, "won", 270, NOW)  # already settled
        settled, profit, bankroll = repo.settle_batch(
            [(first, "lost", -123), (second, "won", 100)], NOW,
            move_bankroll=True)
        assert settled == [second]
        assert profit == 100
        assert bankroll == 6600
        assert repo.get_bankroll_cents() == 6600
        # The already-settled row kept its original outcome.
        row = repo.recommendation_by_key("9001", "ML", "2026-07-18")
        assert row["status"] == "won" and row["profit_cents"] == 270

    def test_settle_batch_invalid_status_rolls_back_everything(self):
        repo = make_repo()
        repo.set_bankroll_cents(6500, NOW)
        first = _rec(repo)
        with pytest.raises(RepoError):
            repo.settle_batch([(first, "won", 270), (first, "push", 0)], NOW,
                              move_bankroll=True)
        row = repo.recommendation_by_key("9001", "ML", "2026-07-18")
        assert row["status"] == "open"           # nothing applied
        assert repo.get_bankroll_cents() == 6500

    def test_settle_batch_without_bankroll_row(self):
        repo = make_repo()
        rec_id = _rec(repo)
        settled, profit, bankroll = repo.settle_batch(
            [(rec_id, "won", 270)], NOW, move_bankroll=True)
        assert settled == [rec_id] and profit == 270 and bankroll is None


class TestResultsAndBankroll:
    def test_result_recorded_once(self):
        repo = make_repo()
        kwargs = dict(match_source_id="9001", winner_side="home",
                      home_score=3, away_score=2, source="manual",
                      recorded_at=NOW)
        assert repo.record_result(**kwargs) is True
        kwargs["winner_side"] = "away"
        assert repo.record_result(**kwargs) is False    # first result stands

    def test_invalid_winner_rejected(self):
        repo = make_repo()
        with pytest.raises(RepoError):
            repo.record_result(match_source_id="9001", winner_side="draw",
                               home_score=None, away_score=None,
                               source="manual", recorded_at=NOW)

    def test_bankroll_lifecycle(self):
        repo = make_repo()
        assert repo.get_bankroll_cents() is None
        repo.set_bankroll_cents(6500, NOW)
        assert repo.get_bankroll_cents() == 6500
        repo.set_bankroll_cents(6770, NOW)
        assert repo.get_bankroll_cents() == 6770
        with pytest.raises(RepoError):
            repo.set_bankroll_cents(-1, NOW)


class TestCalibrationVersions:
    def _fit(self):
        pairs = [(0.6, 1.0)] * 80 + [(0.6, 0.0)] * 20
        return fit_platt(pairs, min_rows=50)

    def test_versions_number_sequentially_and_start_inactive(self):
        repo = make_repo()
        assert repo.insert_calibration_version(self._fit(), method="platt",
                                               fitted_at=NOW) == 1
        assert repo.insert_calibration_version(self._fit(), method="platt",
                                               fitted_at=NOW) == 2
        assert repo.active_calibration() is None

    def test_manual_activation_is_exclusive(self):
        repo = make_repo()
        fit = self._fit()
        repo.insert_calibration_version(fit, method="platt", fitted_at=NOW)
        repo.insert_calibration_version(fit, method="platt", fitted_at=NOW)
        repo.activate_calibration(1)
        repo.activate_calibration(2)
        active = repo.active_calibration()
        assert active is not None
        assert active.intercept == pytest.approx(fit.transform.intercept)
        assert active.slope == pytest.approx(fit.transform.slope)

    def test_activating_missing_version_fails(self):
        repo = make_repo()
        with pytest.raises(RepoError):
            repo.activate_calibration(7)


class TestPostgresDialect:
    """The PG path can't run in CI, but its query/DDL rendering can."""

    def test_percent_s_params_pass_through(self):
        from tt_edge.db.repo import TTEdgeRepo
        repo = TTEdgeRepo.__new__(TTEdgeRepo)
        repo.dialect = "postgres"
        assert repo._sql("SELECT x FROM t WHERE a = %s AND b = %s") == \
            "SELECT x FROM t WHERE a = %s AND b = %s"
        repo.dialect = "sqlite"
        assert repo._sql("WHERE a = %s") == "WHERE a = ?"

    def test_migrations_render_for_postgres(self):
        from pathlib import Path
        import tt_edge.db.repo as repo_mod
        tokens = repo_mod._DIALECT_TOKENS["postgres"]
        for path in sorted(
                (Path(repo_mod.__file__).parent / "migrations").glob("*.sql")):
            ddl = path.read_text(encoding="utf-8")
            for token, replacement in tokens.items():
                ddl = ddl.replace(token, replacement)
            ddl = "\n".join(line for line in ddl.splitlines()
                            if not line.lstrip().startswith("--"))
            assert "{" not in ddl, f"{path.name}: unresolved token"
            statements = [s for s in ddl.split(";") if s.strip()]
            assert statements, f"{path.name}: no statements"
            assert any("BIGSERIAL" in s for s in statements)
            assert "%" not in ddl, f"{path.name}: % would trip psycopg2"


class TestPlayersAndMatches:
    def test_upserts_are_stable(self):
        repo = make_repo()
        repo.upsert_player("101", "Marud", NOW)
        repo.upsert_player("101", "Marud D.", NOW)     # name refresh
        repo.upsert_match(_event(), NOW)
        repo.upsert_match(_event(), NOW)               # no duplicate
        players = repo._fetchall("SELECT * FROM tt_players")
        matches = repo._fetchall("SELECT * FROM tt_matches")
        assert len(players) == 1 and players[0]["name"] == "Marud D."
        assert len(matches) == 1
