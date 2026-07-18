"""End-to-end scan: envelopes in -> features -> edge -> claim -> alert out.

Uses the REAL shared TelegramSendClient over a fake transport, an in-memory
repo, and a fixed clock. Covers the spec's alert shape, duplicate-alert
idempotency, send-failure retry (claim survives, send retries, no new row),
stale-data rejection, and the exposure guards.
"""
import json
from datetime import timedelta
from decimal import Decimal

import pytest

from tests import tt_edge_fixtures as fx
from tt_edge.alerts.telegram import AlertContext, build_alert
from tt_edge.jobs.scan import ScanData, ScanDataError, load_scan_data, run_scan
from tt_edge.scrapers import sofascore
from tt_edge.scrapers.odds import OddsSnapshot


def _scan_data(**ages) -> ScanData:
    env = fx.scenario_envelopes(**ages)
    return ScanData(board=env["board"], h2h=env["h2h"], form=env["form"])


def _seed_odds(repo, home=-120, away=100, age_s=45, match_id=fx.MATCH_ID):
    repo.insert_odds_snapshot(OddsSnapshot(
        match_id, "manual", home, away,
        fx.NOW - timedelta(seconds=age_s)))


def _run(repo, *, sender=None, dry_run=False, config=None, data=None):
    return run_scan(repo=repo, config=config or fx.make_config(),
                    data=data or _scan_data(), now=fx.NOW, sender=sender,
                    dry_run=dry_run)


class TestRecommendationFlow:
    def test_scan_recommends_and_delivers(self):
        repo = fx.make_repo()
        repo.set_bankroll_cents(6500, fx.NOW)
        _seed_odds(repo)
        sender, post = fx.make_sender()

        outcomes = _run(repo, sender=sender)

        assert len(outcomes) == 1
        outcome = outcomes[0]
        assert outcome.status == "RECOMMEND"
        assert outcome.side == "home"
        assert outcome.alert_sent and outcome.alert_delivered
        # Data-obvious pick at the 5% cap: $3.25 on the $65 roll.
        assert outcome.stake_cents == 325

        assert len(post.calls) == 1
        message = post.calls[0]["json"]
        assert message["parse_mode"] == "HTML"
        text = message["text"]
        assert text.startswith("🏓 TT EDGE | Marud vs Gumulinski | 14:30 UTC")
        assert "Pick: Marud ML @ -120 (fair -109)" in text
        assert "Market (de-vig): 52.2%" in text
        assert "Basis: H2H 15-5 (L20: 15-5), form +60%, "\
               "2 common opps net +4" in text
        assert "Stake: $3.25 (0.25K, roll $65.00)" in text
        assert "Data age: board 4m, H2H 2h, odds 45s" in text
        # Never trip the Q15 legacy formatter/suppression chain.
        for marker in ("ENTRY RECOMMENDED", "NO ENTRY YET", "V9.5 CHECK"):
            assert marker not in text

        row = repo.recommendation_by_key(fx.MATCH_ID, "ML", "2026-07-18")
        assert row["status"] == "open"
        assert row["alert_delivered_at"] is not None
        preds = repo._fetchall("SELECT decision FROM tt_predictions")
        assert [p["decision"] for p in preds] == ["RECOMMEND"]

    def test_rescan_same_day_does_not_duplicate(self):
        repo = fx.make_repo()
        repo.set_bankroll_cents(6500, fx.NOW)
        _seed_odds(repo)
        sender, post = fx.make_sender()

        _run(repo, sender=sender)
        outcomes = _run(repo, sender=sender)

        assert outcomes[0].reasons == ("duplicate_scan",)
        assert len(post.calls) == 1
        recs = repo._fetchall("SELECT id FROM tt_recommendations")
        preds = repo._fetchall("SELECT id FROM tt_predictions")
        assert len(recs) == 1 and len(preds) == 1

    def test_failed_send_retries_without_new_row(self):
        repo = fx.make_repo()
        repo.set_bankroll_cents(6500, fx.NOW)
        _seed_odds(repo)
        broken, broken_post = fx.make_sender(status_code=500)

        first = _run(repo, sender=broken)
        assert first[0].alert_sent and not first[0].alert_delivered
        row = repo.recommendation_by_key(fx.MATCH_ID, "ML", "2026-07-18")
        assert row["alert_delivered_at"] is None

        working, post = fx.make_sender()
        second = _run(repo, sender=working)
        assert second[0].alert_delivered
        assert len(post.calls) == 1
        rows = repo._fetchall("SELECT id, alert_delivered_at "
                              "FROM tt_recommendations")
        assert len(rows) == 1
        assert rows[0]["alert_delivered_at"] is not None

    def test_dry_run_creates_row_but_sends_nothing(self):
        repo = fx.make_repo()
        repo.set_bankroll_cents(6500, fx.NOW)
        _seed_odds(repo)

        outcomes = _run(repo, dry_run=True)

        outcome = outcomes[0]
        assert outcome.status == "RECOMMEND"
        assert outcome.alert_text is not None
        assert not outcome.alert_sent and not outcome.alert_delivered
        row = repo.recommendation_by_key(fx.MATCH_ID, "ML", "2026-07-18")
        assert row is not None and row["alert_delivered_at"] is None

    def test_muted_sender_keeps_records_without_delivery(self):
        repo = fx.make_repo()
        repo.set_bankroll_cents(6500, fx.NOW)
        _seed_odds(repo)
        muted, post = fx.make_sender(configured=False)

        outcomes = _run(repo, sender=muted)

        assert outcomes[0].status == "RECOMMEND"
        assert not outcomes[0].alert_sent
        assert post.calls == []
        assert repo.recommendation_by_key(fx.MATCH_ID, "ML",
                                          "2026-07-18") is not None


class TestAbstainPaths:
    def test_stale_board_rejects_everything(self):
        repo = fx.make_repo()
        repo.set_bankroll_cents(6500, fx.NOW)
        _seed_odds(repo)
        outcomes = _run(repo, data=_scan_data(board_age=timedelta(minutes=31)))
        assert outcomes[0].status == "NO_BET"
        assert "stale_board" in outcomes[0].reasons
        assert repo._fetchall("SELECT id FROM tt_recommendations") == []
        preds = repo._fetchall("SELECT decision, reasons FROM tt_predictions")
        assert preds[0]["decision"] == "NO_BET"
        assert "stale_board" in json.loads(preds[0]["reasons"])

    def test_stale_h2h_rejects(self):
        repo = fx.make_repo()
        repo.set_bankroll_cents(6500, fx.NOW)
        _seed_odds(repo)
        outcomes = _run(repo, data=_scan_data(h2h_age=timedelta(hours=25)))
        assert "stale_h2h" in outcomes[0].reasons

    def test_no_odds_is_a_reasoned_no_bet(self):
        repo = fx.make_repo()
        repo.set_bankroll_cents(6500, fx.NOW)
        outcomes = _run(repo)
        assert outcomes[0].status == "NO_BET"
        assert "no_odds" in outcomes[0].reasons
        # The prediction row still lands (calibration corpus).
        assert len(repo._fetchall("SELECT id FROM tt_predictions")) == 1

    def test_stale_odds_rejects(self):
        repo = fx.make_repo()
        repo.set_bankroll_cents(6500, fx.NOW)
        _seed_odds(repo, age_s=601)
        outcomes = _run(repo)
        assert "stale_odds" in outcomes[0].reasons

    def test_line_steam_against_pick_is_fix_risk(self):
        repo = fx.make_repo()
        repo.set_bankroll_cents(6500, fx.NOW)
        # Home opened -140 and drifted to -120: +20c against the pick.
        repo.insert_odds_snapshot(OddsSnapshot(
            fx.MATCH_ID, "manual", -140, 120,
            fx.NOW - timedelta(minutes=8)))
        _seed_odds(repo)
        outcomes = _run(repo)
        assert outcomes[0].status == "NO_BET"
        assert "fix_risk_line_move" in outcomes[0].reasons

    def test_no_bankroll_abstains(self):
        repo = fx.make_repo()
        _seed_odds(repo)
        outcomes = _run(repo)
        assert outcomes[0].reasons == ("no_bankroll_set",)

    def test_max_concurrent_open_recommendations(self):
        repo = fx.make_repo()
        repo.set_bankroll_cents(6500, fx.NOW)
        _seed_odds(repo)
        for i in range(3):
            repo.insert_recommendation(
                match_source_id=f"800{i}", market="ML", side="home",
                pick_name="X", model_p="0.6", fair_p="0.5", edge="0.1",
                price_american=-120, stake_cents=100, bankroll_cents=6500,
                recommended_at=fx.NOW, recommended_on="2026-07-18",
                basis_json="{}", data_ages_json="{}")
        outcomes = _run(repo)
        assert outcomes[0].reasons == ("max_concurrent_open",)


class TestScanDataLoading:
    def test_loads_envelope_directory(self, tmp_path):
        env = fx.scenario_envelopes()
        sofascore.save_envelope(env["board"], tmp_path)
        sofascore.save_envelope(env["h2h"][fx.MATCH_ID], tmp_path)
        sofascore.save_envelope(env["form"][fx.HOME_ID], tmp_path)
        data = load_scan_data(tmp_path)
        assert data.board.entity_id == "2026-07-18"
        assert set(data.h2h) == {fx.MATCH_ID}
        assert set(data.form) == {fx.HOME_ID}

    def test_requires_exactly_one_board(self, tmp_path):
        with pytest.raises(ScanDataError):
            load_scan_data(tmp_path)


class TestAlertBuilder:
    def test_names_are_html_escaped_and_missing_h2h_dashed(self):
        ctx = AlertContext(
            home_name="O'Brien <A>", away_name="B & C", pick_name="B & C",
            start_time=fx.START, market="ML", price_american=110,
            fair_p=Decimal("0.45"), model_p=Decimal("0.52"),
            edge=Decimal("0.07"), h2h_wins=0, h2h_losses=0, l20_wins=0,
            l20_losses=0, form_diff=-0.13, common_opps=4, common_net=-3,
            stake_cents=250, kelly_multiplier=Decimal("0.25"),
            bankroll_cents=6500, board_age_s=240, h2h_age_s=None,
            odds_age_s=45)
        text = build_alert(ctx)
        assert "&lt;A&gt;" in text and "B &amp; C" in text
        assert "H2H -," in text
        assert "form -13%" in text
        assert "@ +110" in text
