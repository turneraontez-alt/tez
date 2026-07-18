"""Grading: settlement math (Decimal, floor rounding), bankroll movement,
voids, regrade idempotency, and the result-input parsing."""
import json
from decimal import Decimal

import pytest

from tests import tt_edge_fixtures as fx
from tt_edge.jobs.bankroll import dollars_to_cents
from tt_edge.jobs.grade import (ResultInput, ResultInputError,
                                load_results_file, parse_result_arg,
                                run_grade, settlement_profit_cents)


def _seed_rec(repo, match_id="9001", side="home", price=-120, stake=325):
    rec_id = repo.insert_recommendation(
        match_source_id=match_id, market="ML", side=side, pick_name="Marud",
        model_p="0.82", fair_p="0.5217", edge="0.30", price_american=price,
        stake_cents=stake, bankroll_cents=6500, recommended_at=fx.NOW,
        recommended_on="2026-07-18", basis_json="{}", data_ages_json="{}")
    assert rec_id is not None
    return rec_id


def _seed_prediction(repo, match_id="9001"):
    assert repo.insert_prediction(
        match_source_id=match_id, market="ML", predicted_on="2026-07-18",
        model_p_home="0.82", fair_p_home="0.5217", decision="RECOMMEND",
        reasons_json="[]", created_at=fx.NOW)


class TestSettlementMath:
    def test_winner_profit_positive_price(self):
        assert settlement_profit_cents("won", 200, 150) == 300

    def test_winner_profit_negative_price_floors(self):
        # 325 * (5/6) = 270.833... -> 270 (winners never round up).
        assert settlement_profit_cents("won", 325, -120) == 270

    def test_loss_is_full_stake(self):
        assert settlement_profit_cents("lost", 325, -120) == -325

    def test_void_returns_stake(self):
        assert settlement_profit_cents("void", 325, -120) == 0

    def test_unknown_status_rejected(self):
        with pytest.raises(ValueError):
            settlement_profit_cents("push", 100, -120)


class TestRunGrade:
    def test_win_settles_and_grows_bankroll(self):
        repo = fx.make_repo()
        repo.set_bankroll_cents(6500, fx.NOW)
        _seed_rec(repo)
        _seed_prediction(repo)

        report = run_grade(repo=repo,
                           results=[ResultInput("9001", "home", 3, 1)],
                           now=fx.NOW)

        assert report.results_recorded == 1
        assert report.recommendations_settled == 1
        assert report.profit_cents == 270
        assert report.bankroll_after_cents == 6770
        assert repo.get_bankroll_cents() == 6770
        row = repo.recommendation_by_key("9001", "ML", "2026-07-18")
        assert row["status"] == "won" and row["profit_cents"] == 270
        assert report.predictions_graded == 1
        assert repo.graded_prediction_pairs() == [(0.82, 1.0)]

    def test_loss_shrinks_bankroll(self):
        repo = fx.make_repo()
        repo.set_bankroll_cents(6500, fx.NOW)
        _seed_rec(repo)
        report = run_grade(repo=repo, results=[ResultInput("9001", "away")],
                           now=fx.NOW)
        assert report.profit_cents == -325
        assert repo.get_bankroll_cents() == 6175
        row = repo.recommendation_by_key("9001", "ML", "2026-07-18")
        assert row["status"] == "lost"

    def test_void_returns_stake_and_skips_calibration(self):
        repo = fx.make_repo()
        repo.set_bankroll_cents(6500, fx.NOW)
        _seed_rec(repo)
        _seed_prediction(repo)
        report = run_grade(repo=repo, results=[ResultInput("9001", "void")],
                           now=fx.NOW)
        assert report.profit_cents == 0
        assert repo.get_bankroll_cents() == 6500
        assert report.predictions_graded == 0     # walkovers teach nothing
        row = repo.recommendation_by_key("9001", "ML", "2026-07-18")
        assert row["status"] == "void"

    def test_regrade_is_idempotent(self):
        repo = fx.make_repo()
        repo.set_bankroll_cents(6500, fx.NOW)
        _seed_rec(repo)
        results = [ResultInput("9001", "home")]
        run_grade(repo=repo, results=results, now=fx.NOW)
        report = run_grade(repo=repo, results=results, now=fx.NOW)
        assert report.results_recorded == 0
        assert report.recommendations_settled == 0
        assert repo.get_bankroll_cents() == 6770   # unchanged by the rerun

    def test_conflicting_second_result_is_ignored(self):
        repo = fx.make_repo()
        repo.set_bankroll_cents(6500, fx.NOW)
        _seed_rec(repo)
        run_grade(repo=repo, results=[ResultInput("9001", "home")], now=fx.NOW)
        run_grade(repo=repo, results=[ResultInput("9001", "away")], now=fx.NOW)
        row = repo.recommendation_by_key("9001", "ML", "2026-07-18")
        assert row["status"] == "won"              # first result stands

    def test_multiple_recommendations_settle_together(self):
        repo = fx.make_repo()
        repo.set_bankroll_cents(6500, fx.NOW)
        _seed_rec(repo, match_id="9001", side="home")            # wins +270
        _seed_rec(repo, match_id="9002", side="away", price=100,
                  stake=100)                                     # loses -100
        report = run_grade(repo=repo,
                           results=[ResultInput("9001", "home"),
                                    ResultInput("9002", "home")],
                           now=fx.NOW)
        assert report.recommendations_settled == 2
        assert report.profit_cents == 170
        assert repo.get_bankroll_cents() == 6670

    def test_missing_bankroll_still_settles(self):
        repo = fx.make_repo()
        _seed_rec(repo)
        report = run_grade(repo=repo, results=[ResultInput("9001", "home")],
                           now=fx.NOW)
        assert report.recommendations_settled == 1
        assert report.bankroll_after_cents is None


class TestResultInputs:
    def test_parse_result_arg(self):
        assert parse_result_arg("9001=home") == ResultInput("9001", "home")
        assert parse_result_arg(" 9001 =void").winner_side == "void"

    @pytest.mark.parametrize("bad", ["9001", "9001=draw", "=home", "9001="])
    def test_parse_result_arg_rejects(self, bad):
        with pytest.raises(ResultInputError):
            parse_result_arg(bad)

    def test_load_results_file(self, tmp_path):
        path = tmp_path / "results.json"
        path.write_text(json.dumps([
            {"match_source_id": "9001", "winner": "home",
             "home_score": 3, "away_score": 1},
            {"match_source_id": "9002", "winner": "void"},
        ]))
        results = load_results_file(path)
        assert results[0] == ResultInput("9001", "home", 3, 1)
        assert results[1].winner_side == "void"

    @pytest.mark.parametrize("body", [
        '{"not": "a list"}',
        '[{"match_source_id": "1", "winner": "draw"}]',
        '[{"winner": "home"}]',
        "{broken",
    ])
    def test_load_results_file_rejects(self, tmp_path, body):
        path = tmp_path / "bad.json"
        path.write_text(body)
        with pytest.raises(ResultInputError):
            load_results_file(path)


class TestBankrollCli:
    def test_dollars_to_cents(self):
        assert dollars_to_cents("65.00") == 6500
        assert dollars_to_cents("$65") == 6500
        assert dollars_to_cents("0.01") == 1

    @pytest.mark.parametrize("bad", ["65.005", "-1", "abc", ""])
    def test_dollars_to_cents_rejects(self, bad):
        with pytest.raises(ValueError):
            dollars_to_cents(bad)
