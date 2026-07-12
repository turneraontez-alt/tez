"""Interval-timing research collector + economics (default-OFF, read-only)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.interval_research import config as cfg_mod
from q15_upgrade.interval_research.config import IntervalResearchConfig, INTERVAL_MARKS, INTERVAL_ROLES
from q15_upgrade.interval_research.ledger import IntervalResearchLedger
from q15_upgrade.interval_research.runner import IntervalResearchRunner, get_runner, reset_runner
from q15_upgrade.interval_research import economics as econ


class _Canon:
    def __init__(self, ticker, seconds_remaining, settlement_time=1_000_000.0):
        self.ticker = ticker
        self.seconds_remaining = seconds_remaining
        self.settlement_time = settlement_time


def _analysis(side="NO", ask=61.0, available=True, decision="WATCH_PRICE", edge=4.0):
    return {
        "prediction_available": available,
        "prediction_side": side,
        "raw_yes_probability": 0.45, "yes_probability": 0.40,
        "conservative_probability": 0.38,
        "flip_risk": {"score": 22.0}, "manipulation": {"suspected": True, "score": 0.33},
        "regime": {"distance_sigma": 0.18}, "shadow_signals": {"prediction_stability": 0.5},
        "data_quality": 0.9, "trade_decision": decision, "net_edge_cents": edge,
        "entry_ask_cents": ask, "main_blocker": "price_not_attractive_after_costs",
        "quote": {"bid_cents": 58.0, "ask_cents": 63.0, "spread_cents": 5.0, "ask_depth": 40.0},
        "costs": {"fee_cents": 1.0, "slippage_cents": 0.5, "total_cents": 2.0},
    }


def _cfg(tmp, **kw):
    base = dict(enabled=True, model_version="ir-test",
               db_path=os.path.join(tmp, "ir.sqlite3"), mark_band_seconds=25.0,
               min_data_quality=0.0)
    base.update(kw)
    return IntervalResearchConfig(**base)


class _DriftRecorder:
    """Small runner-adapter fake; source-rule behavior lives in its own tests."""

    def __init__(self):
        self.window_calls = []
        self.checkpoint_calls = []
        self.pick_rows = []
        self.no_rows = []
        self.checkpoint_rows = []

    def observe_window(self, **kwargs):
        self.window_calls.append(kwargs)
        return False

    def observe_checkpoint(self, **kwargs):
        self.checkpoint_calls.append(kwargs)
        return 0

    def picks_for_window(self, model_version, window_key):
        return list(self.pick_rows)

    def no_mirror_rows_for_window(self, model_version, window_key):
        return list(self.no_rows)

    def checkpoint_rows_for_window(self, model_version, window_key, interval):
        return list(self.checkpoint_rows)

    def scoreboard(self):
        return {}


class ConfigTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("Q15_INTERVAL_RESEARCH_ENABLED", None)
        os.environ.pop("Q15_INTERVAL_RESEARCH_DB", None)
        cfg_mod.reset_enabled_cache()
        reset_runner()

    def test_eight_marks_and_roles(self):
        self.assertEqual(list(INTERVAL_MARKS), ["15M", "13M", "12M", "11M", "10M", "9M", "8M", "7M"])
        self.assertEqual(INTERVAL_MARKS["10M"], 600)
        self.assertEqual(INTERVAL_ROLES["10M"], "OFFENSIVE_ENTRY")
        self.assertEqual(INTERVAL_ROLES["7M"], "CONFIRMATION_DEFENSIVE")

    def test_crossing_bounds_default_to_ninety_and_forty_five_seconds(self):
        cfg = IntervalResearchConfig()
        self.assertEqual(cfg.crossing_max_seconds, 90.0)
        self.assertEqual(cfg.crossing_max_offset_seconds, 45.0)

    def test_default_on_capture_only(self):
        os.environ.pop("Q15_INTERVAL_RESEARCH_ENABLED", None)
        os.environ["Q15_INTERVAL_RESEARCH_DB"] = os.path.join(tempfile.mkdtemp(), "on.sqlite3")
        cfg_mod.reset_enabled_cache()
        reset_runner()
        self.assertTrue(IntervalResearchConfig.from_env().enabled)
        self.assertIsNotNone(get_runner())

    def test_explicit_disable_still_works(self):
        os.environ["Q15_INTERVAL_RESEARCH_ENABLED"] = "false"
        cfg_mod.reset_enabled_cache()
        reset_runner()
        self.assertFalse(IntervalResearchConfig.from_env().enabled)
        self.assertIsNone(get_runner())


class CaptureResolveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.r = IntervalResearchRunner(_cfg(self.tmp))

    def _observe(self, ticker, seconds, side="NO", ask=61.0, **kw):
        self.r.observe(analyses={"BTC": _analysis(side=side, ask=ask, **kw)},
                       canonicals={"BTC": _Canon(ticker, seconds)}, now=1000.0)

    def test_capture_in_band_and_dedup(self):
        self._observe("KX1", 600)   # exactly 10M
        self._observe("KX1", 595)   # still in 10M band -> dedup
        rows = self.r.ledger.resolved_rows()  # none resolved yet
        self.assertEqual(rows, [])
        cnt = self.r.ledger.counts()
        self.assertEqual(cnt["by_interval"].get("10M"), 1)
        self.assertEqual(cnt["captured"], 1)

    def test_out_of_band_not_captured(self):
        self._observe("KX2", 650)   # between 11M(660) and 10M(600) bands -> 650 in 11M band [635,660]
        # 650 is within 11M band (660-25=635..660) -> captured as 11M
        self.assertEqual(self.r.ledger.counts()["by_interval"].get("11M"), 1)
        self._observe("KX3", 500)   # 500: 9M band is [515,540]; 8M band [455,480]; 500 in neither
        self.assertIsNone(self.r.ledger.counts()["by_interval"].get("9M"))

    def test_missing_reason_when_model_cannot_score(self):
        self.r.observe(analyses={"BTC": _analysis(available=False)},
                       canonicals={"BTC": _Canon("KXm", 600)}, now=1000.0)
        cnt = self.r.ledger.counts()
        self.assertEqual(cnt["by_missing_reason"].get("MODEL_COULD_NOT_SCORE"), 1)
        self.assertEqual(cnt["captured"], 0)

    def test_resolve_correctness_and_pnl(self):
        self._observe("KXr", 600, side="NO", ask=61.0)
        # settle NO -> correct, pnl = 100 - 61 = 39
        self.r.resolve_settled([{"ticker": "KXr", "result": "NO"}], now=2000.0)
        row = [r for r in self.r.ledger.resolved_rows() if r["ticker"] == "KXr"][0]
        self.assertEqual(row["correct"], 1)
        self.assertAlmostEqual(row["realized_pnl_cents"], 39.0, places=3)

    def test_resolve_loss_pnl(self):
        self._observe("KXl", 600, side="NO", ask=61.0)
        self.r.resolve_settled([{"ticker": "KXl", "result": "YES"}], now=2000.0)
        row = [r for r in self.r.ledger.resolved_rows() if r["ticker"] == "KXl"][0]
        self.assertEqual(row["correct"], 0)
        self.assertAlmostEqual(row["realized_pnl_cents"], -61.0, places=3)

    def test_restart_safe_no_duplicate(self):
        self._observe("KXs", 600)
        # New ledger instance on the SAME path (simulating a restart) + same capture.
        r2 = IntervalResearchRunner(_cfg(self.tmp))
        r2.observe(analyses={"BTC": _analysis()}, canonicals={"BTC": _Canon("KXs", 600)}, now=3000.0)
        self.assertEqual(r2.ledger.counts()["by_interval"].get("10M"), 1)  # still one

    def test_disabled_is_noop(self):
        r = IntervalResearchRunner(_cfg(self.tmp, enabled=False, db_path=os.path.join(self.tmp, "off.sqlite3")))
        r.observe(analyses={"BTC": _analysis()}, canonicals={"BTC": _Canon("KXo", 600)}, now=1000.0)
        self.assertEqual(r.ledger.counts()["captured"], 0)

    def test_13m_capture_feeds_strategy_bots_when_flag_on(self):
        analysis = _analysis(side="YES", ask=55.0, edge=8.0)
        analysis["selected_probability"] = 0.65
        analysis["market_implied_yes_probability"] = 0.54
        analysis["quote"]["spot_depth_trade_net_notional_60s"] = 125.0
        analysis["quote"]["no_ask_cents"] = 45.0
        with patch.dict(os.environ, {"Q15_V3_13M_SNIPER_FEED": "true"}):
            with patch("q15_upgrade.strategy_bots.runtime.record_source_row") as record:
                self.r.observe(
                    analyses={"BTC": analysis},
                    canonicals={"BTC": _Canon("KX13", 780, settlement_time=9000.0)},
                    now=1000.0,
                )

        record.assert_called_once()
        source_row = record.call_args.args[0]
        self.assertEqual(record.call_args.kwargs["source_system"], "ultoim_v2")
        self.assertEqual(source_row["ticker"], "KX13")
        self.assertEqual(source_row["interval"], "13M")
        self.assertEqual(source_row["window_key"], 10)
        self.assertEqual(source_row["close_time"], 9000.0)
        self.assertEqual(source_row["calibrated_yes_probability"], 0.40)
        self.assertEqual(source_row["flip_probability"], 22.0)
        self.assertEqual(source_row["entry_ask_cents"], 55.0)
        self.assertEqual(source_row["spot_depth_trade_net_notional_60s"], 125.0)

    def test_13m_capture_feed_default_off(self):
        with patch("q15_upgrade.strategy_bots.runtime.record_source_row") as record:
            self.r.observe(
                analyses={"BTC": _analysis(side="YES", ask=55.0)},
                canonicals={"BTC": _Canon("KX13-OFF", 780, settlement_time=9000.0)},
                now=1000.0,
            )
        record.assert_not_called()

    def test_13m_crossing_820_to_742_records_nearest_endpoint_and_full_dispatch(self):
        rec = _DriftRecorder()
        with patch.dict(os.environ, {"Q15_V3_TOP_PICK_13M": "false"}):
            with patch("q15_upgrade.drift_shadow.get_recorder", return_value=rec):
                self.r.observe(
                    analyses={"SOL": _analysis(side="YES", ask=67.0)},
                    canonicals={"SOL": _Canon("KXCROSS", 820, settlement_time=1820.0)},
                    now=1000.0,
                )
                self.r.observe(
                    analyses={"SOL": _analysis(side="YES", ask=67.0)},
                    canonicals={"SOL": _Canon("KXCROSS", 742, settlement_time=1820.0)},
                    now=1078.0,
                )

        rows = self.r.ledger.captures_for_window("ir-test", "13M", 2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["seconds_remaining"], 742.0)
        self.assertEqual(rows[0]["captured_at"], 1078.0)
        self.assertEqual(len(rec.window_calls), 1)
        self.assertEqual(len(rec.window_calls[0]["slate"]), 1)
        self.assertEqual(rec.window_calls[0]["now"], 1078.0)

    def test_13m_crossing_uses_previous_endpoint_when_it_is_closer(self):
        rec = _DriftRecorder()
        with patch.dict(os.environ, {"Q15_V3_TOP_PICK_13M": "false"}):
            with patch("q15_upgrade.drift_shadow.get_recorder", return_value=rec):
                self.r.observe(
                    analyses={"SOL": _analysis(side="YES", ask=67.0)},
                    canonicals={"SOL": _Canon("KXPREV", 789, settlement_time=1789.0)},
                    now=1000.0,
                )
                self.r.observe(
                    analyses={"SOL": _analysis(side="YES", ask=67.0)},
                    canonicals={"SOL": _Canon("KXPREV", 710, settlement_time=1789.0)},
                    now=1079.0,
                )

        rows = self.r.ledger.captures_for_window("ir-test", "13M", 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["seconds_remaining"], 789.0)
        self.assertEqual(rows[0]["captured_at"], 1000.0)
        self.assertEqual(rec.window_calls[0]["now"], 1000.0)

    def test_13m_same_cycle_dispatch_waits_for_complete_slate(self):
        rec = _DriftRecorder()
        analyses = {
            asset: _analysis(side="YES", ask=67.0)
            for asset in ("SOL", "DOGE", "XRP")
        }
        canonicals = {
            asset: _Canon(f"KX{asset}", 776, settlement_time=1776.0)
            for asset in analyses
        }
        with patch("q15_upgrade.drift_shadow.get_recorder", return_value=rec):
            self.r.observe(analyses=analyses, canonicals=canonicals, now=1000.0)

        self.assertEqual(len(rec.window_calls), 1)
        self.assertEqual(len(rec.window_calls[0]["slate"]), 3)
        self.assertEqual(
            {row["ticker"] for row in rec.window_calls[0]["slate"]},
            {"KXSOL", "KXDOGE", "KXXRP"},
        )

    def test_precision13_shadow_records_without_drift_or_delivery(self):
        assets = ("BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP")
        analyses = {asset: _analysis(side="YES") for asset in assets}
        for analysis in analyses.values():
            analysis["yes_probability"] = 0.65
        close_time = 1900.0
        with patch.dict(os.environ, {"Q15_V3_TOP_PICK_13M": "false"}):
            with patch("q15_upgrade.drift_shadow.get_recorder", return_value=None):
                with patch(
                    "q15_upgrade.strategy_bots.runtime.record_precision13_row"
                ) as notify:
                    self.r.observe(
                        analyses=analyses,
                        canonicals={
                            asset: _Canon(f"KX{asset}-P13", 900, close_time)
                            for asset in assets
                        },
                        now=1000.0,
                    )
                    self.r.observe(
                        analyses=analyses,
                        canonicals={
                            asset: _Canon(f"KX{asset}-P13", 780, close_time)
                            for asset in assets
                        },
                        now=1120.0,
                    )

        self.assertEqual(notify.call_count, 7)
        self.assertEqual(
            {call.args[0]["record_kind"] for call in notify.call_args_list},
            {"PRECISION_13M_SIZED_V2"},
        )

        rows = self.r.ledger.precision13_rows()
        self.assertEqual(len(rows), 7)
        self.assertEqual({row["decision"] for row in rows}, {"YES"})
        self.assertEqual({row["policy_version"] for row in rows}, {
            "q15-13m-selective-precision-v2",
        })

        # Replaying the same complete window remains insert-only.
        self.r._record_precision13_shadow(
            self.r.ledger.captures_for_window("ir-test", "13M", 2), 2, 1120.0
        )
        self.assertEqual(len(self.r.ledger.precision13_rows()), 7)

        self.r.resolve_settled(
            [{"ticker": f"KX{asset}-P13", "result": "YES"} for asset in assets],
            now=2000.0,
        )
        resolved = self.r.ledger.precision13_rows()
        self.assertEqual({row["correct"] for row in resolved}, {1})

    def test_13m_staggered_crossing_defers_until_every_mapped_asset_crosses(self):
        rec = _DriftRecorder()
        analyses = {
            "SOL": _analysis(side="YES", ask=67.0),
            "XRP": _analysis(side="YES", ask=68.0),
        }
        close_time = 1780.0
        with patch.dict(os.environ, {"Q15_V3_TOP_PICK_13M": "false"}):
            with patch("q15_upgrade.drift_shadow.get_recorder", return_value=rec):
                # SOL has crossed, but XRP is still one second above the mark.
                self.r.observe(
                    analyses=analyses,
                    canonicals={
                        "SOL": _Canon("KXSOL-STAG", 776, close_time),
                        "XRP": _Canon("KXXRP-STAG", 781, close_time),
                    },
                    now=1000.0,
                )
                self.assertEqual(rec.window_calls, [])
                self.assertEqual(
                    self.r.ledger.counts()["by_interval"].get("13M"), 1
                )

                # The next refresh captures XRP and dispatches the complete
                # durable slate, retaining each endpoint's real timestamp.
                self.r.observe(
                    analyses=analyses,
                    canonicals={
                        "SOL": _Canon("KXSOL-STAG", 771, close_time),
                        "XRP": _Canon("KXXRP-STAG", 776, close_time),
                    },
                    now=1005.0,
                )

        self.assertEqual(len(rec.window_calls), 1)
        slate = rec.window_calls[0]["slate"]
        self.assertEqual({row["ticker"] for row in slate}, {
            "KXSOL-STAG", "KXXRP-STAG",
        })
        self.assertEqual(
            {row["ticker"]: row["captured_at"] for row in slate},
            {"KXSOL-STAG": 1000.0, "KXXRP-STAG": 1005.0},
        )

    def test_13m_crossing_guards_stale_new_market_and_cold_late_start(self):
        rec = _DriftRecorder()
        with patch.dict(os.environ, {"Q15_V3_TOP_PICK_13M": "false"}):
            with patch("q15_upgrade.drift_shadow.get_recorder", return_value=rec):
                # Cold first observation below the normal band never backfills.
                self.r.observe(
                    analyses={"SOL": _analysis()},
                    canonicals={"SOL": _Canon("KXCOLD", 742, settlement_time=1820.0)},
                    now=1078.0,
                )
                # A new ticker/window cannot inherit the prior market's clock.
                self.r.observe(
                    analyses={"DOGE": _analysis()},
                    canonicals={"DOGE": _Canon("KXOLD", 820, settlement_time=1820.0)},
                    now=1000.0,
                )
                self.r.observe(
                    analyses={"DOGE": _analysis()},
                    canonicals={"DOGE": _Canon("KXNEW", 742, settlement_time=2720.0)},
                    now=1078.0,
                )
                # Same market, but a >90s wall gap is deliberately not repaired.
                self.r.observe(
                    analyses={"XRP": _analysis()},
                    canonicals={"XRP": _Canon("KXSTALE", 820, settlement_time=1820.0)},
                    now=1000.0,
                )
                self.r.observe(
                    analyses={"XRP": _analysis()},
                    canonicals={"XRP": _Canon("KXSTALE", 742, settlement_time=1820.0)},
                    now=1091.0,
                )

        self.assertIsNone(self.r.ledger.counts()["by_interval"].get("13M"))
        self.assertEqual(rec.window_calls, [])

    def test_late_restart_replays_persisted_active_window_without_fabrication(self):
        close_time = 1776.0
        with patch.dict(os.environ, {"Q15_V3_TOP_PICK_13M": "false"}):
            with patch("q15_upgrade.drift_shadow.get_recorder", return_value=None):
                self.r.observe(
                    analyses={"SOL": _analysis(side="YES", ask=67.0)},
                    canonicals={"SOL": _Canon("KXRESTART", 776, close_time)},
                    now=1000.0,
                )

            restarted = IntervalResearchRunner(_cfg(self.tmp))
            rec = _DriftRecorder()
            rec.pick_rows = [{
                "created_at": 1000.0,
                "asset": "SOL",
                "ticker": "KXRESTART",
                "close_time": close_time,
                "ask_cents": 67.0,
                "pick_rank": 1,
                "slate_n": 1,
            }]
            with patch("q15_upgrade.drift_shadow.get_recorder", return_value=rec):
                with patch("q15_upgrade.strategy_bots.runtime.record_drift_pick_row") as record:
                    restarted.observe(
                        analyses={"SOL": _analysis(side="YES", ask=67.0)},
                        canonicals={"SOL": _Canon("KXRESTART", 700, close_time)},
                        now=1076.0,
                    )

        self.assertEqual(len(rec.window_calls), 1)
        self.assertEqual(
            rec.window_calls[0]["slate"][0]["captured_at"], 1000.0
        )
        record.assert_called_once()
        self.assertEqual(record.call_args.args[0]["created_at"], 1000.0)
        self.assertEqual(restarted.ledger.counts()["by_interval"]["13M"], 1)

        # A separate cold runner at the same late point has no durable source
        # capture to replay and must not invent one.
        cold = IntervalResearchRunner(_cfg(tempfile.mkdtemp()))
        cold_rec = _DriftRecorder()
        with patch("q15_upgrade.drift_shadow.get_recorder", return_value=cold_rec):
            cold.observe(
                analyses={"XRP": _analysis(side="YES", ask=67.0)},
                canonicals={"XRP": _Canon("KXCOLD-700", 700, close_time)},
                now=1076.0,
            )
        self.assertEqual(cold_rec.window_calls, [])
        self.assertIsNone(cold.ledger.counts()["by_interval"].get("13M"))

    def test_no_expansion_adapter_keeps_per_row_time_for_spot_flow_cutoff(self):
        rec = _DriftRecorder()
        rec.no_rows = [
            {
                "created_at": 1000.0, "asset": "XRP", "ticker": "KXNO-A",
                "close_time": 1780.0, "ask_cents": 64.0,
            },
            {
                "created_at": 1005.0, "asset": "DOGE", "ticker": "KXNO-B",
                "close_time": 1780.0, "ask_cents": 66.0,
            },
        ]
        with patch(
            "q15_upgrade.strategy_bots.runtime.record_drift_no_expansion_window"
        ) as record:
            self.r._alert_drift_no_expansion(
                rec, 1, now=1100.0, replay_all=True
            )

        payloads = record.call_args.args[0]
        self.assertEqual([row["created_at"] for row in payloads], [1000.0, 1005.0])
        self.assertEqual([row["seconds_remaining"] for row in payloads], [780.0, 775.0])

    def test_late_restart_replays_persisted_checkpoint_slate(self):
        close_time = 1719.0
        with patch("q15_upgrade.drift_shadow.get_recorder", return_value=None):
            self.r.observe(
                analyses={"SOL": _analysis(side="YES", ask=67.0)},
                canonicals={"SOL": _Canon("KXCKPT-RESTART", 719, close_time)},
                now=1000.0,
            )

        restarted = IntervalResearchRunner(_cfg(self.tmp))
        rec = _DriftRecorder()
        with patch("q15_upgrade.drift_shadow.get_recorder", return_value=rec):
            restarted.observe(
                analyses={"SOL": _analysis(side="YES", ask=67.0)},
                canonicals={"SOL": _Canon("KXCKPT-RESTART", 650, close_time)},
                now=1069.0,
            )

        replay = next(
            call for call in rec.checkpoint_calls if call["interval"] == "12M"
        )
        self.assertEqual(replay["slate"][0]["captured_at"], 1000.0)

    def test_checkpoint_capture_dispatches_event_after_full_slate(self):
        rec = _DriftRecorder()
        analyses = {
            asset: _analysis(side="YES", ask=67.0)
            for asset in ("SOL", "DOGE", "XRP")
        }
        canonicals = {
            asset: _Canon(f"KX{asset}12", 719, settlement_time=1719.0)
            for asset in analyses
        }
        with patch("q15_upgrade.drift_shadow.get_recorder", return_value=rec):
            self.r.observe(analyses=analyses, canonicals=canonicals, now=1000.0)

        self.assertEqual(len(rec.checkpoint_calls), 1)
        call = rec.checkpoint_calls[0]
        self.assertEqual(call["interval"], "12M")
        self.assertEqual(call["now"], 1000.0)
        self.assertEqual(len(call["slate"]), 3)


class DriftReplayApiTest(unittest.TestCase):
    def test_all_row_replay_apis_preserve_source_created_at(self):
        from q15_upgrade.drift_shadow import DriftShadow

        db_path = os.path.join(tempfile.mkdtemp(), "drift.sqlite3")
        with patch.dict(os.environ, {"Q15_DRIFT_SHADOW": "true"}):
            rec = DriftShadow(db_path=db_path)
        yes = {
            "asset": "SOL", "ticker": "KXSOL-R", "predicted_side": "YES",
            "yes_ask_cents": 67.0, "entry_ask_cents": 67.0,
            "distance_from_strike": 1e-5, "flip_probability": 20.0,
            "calibrated_yes_probability": 0.72, "spread_cents": 3.0,
            "captured_at": 1000.0,
        }
        no = {
            "asset": "XRP", "ticker": "KXXRP-R", "predicted_side": "NO",
            "yes_ask_cents": 36.0, "entry_ask_cents": 64.0,
            "distance_from_strike": 1e-5, "flip_probability": 20.0,
            "calibrated_yes_probability": 0.30, "spread_cents": 2.0,
            "captured_at": 1005.0,
        }
        self.assertTrue(rec.observe_window(
            model_version="m", window_key=2, close_time=1780.0,
            slate=[yes, no], now=1010.0,
        ))
        self.assertEqual(rec.observe_checkpoint(
            model_version="m", window_key=2, interval="12M", close_time=1780.0,
            slate=[dict(yes, yes_ask_cents=68.0, captured_at=1060.0)], now=1070.0,
        ), 1)

        picks = rec.picks_for_window("m", 2)
        no_rows = rec.no_mirror_rows_for_window("m", 2)
        checkpoints = rec.checkpoint_rows_for_window("m", 2, "12M")
        self.assertEqual([row["created_at"] for row in picks], [1000.0])
        self.assertEqual([row["created_at"] for row in no_rows], [1005.0])
        self.assertEqual([row["created_at"] for row in checkpoints], [1060.0])
        self.assertEqual(checkpoints[0]["record_kind"], "DRIFT_ADDON_REQUAL")


class EconomicsTest(unittest.TestCase):
    def _row(self, ticker, interval, side, ask, result, recommended=0):
        correct = 1 if side == result else 0
        pnl = (100.0 - ask) if correct else -ask
        return {"ticker": ticker, "interval": interval, "predicted_side": side,
                "entry_ask_cents": ask, "realized_pnl_cents": pnl, "correct": correct,
                "official_result": result, "conservative_probability": 0.7,
                "net_edge_cents": 5.0, "entry_recommended": recommended,
                "mark_seconds": INTERVAL_MARKS[interval], "seconds_remaining": INTERVAL_MARKS[interval]}

    def test_classify_separates_prediction_and_trade(self):
        # 97% accurate but 97c ask -> HIGH prediction / LOW trade value.
        c = econ.classify(0.97, 97.0)
        self.assertEqual(c["prediction_quality"], "HIGH")
        self.assertEqual(c["trade_value"], "LOW")
        # 70% at 61c -> room ~9c -> HIGH trade value.
        c2 = econ.classify(0.70, 61.0)
        self.assertEqual(c2["trade_value"], "HIGH")

    def test_per_interval_economics(self):
        rows = [self._row("A", "10M", "NO", 61.0, "NO"),
                self._row("B", "10M", "NO", 61.0, "YES"),
                self._row("C", "7M", "NO", 97.0, "NO")]
        out = econ.per_interval_economics(rows)
        self.assertEqual(out["10M"]["n"], 2)
        self.assertEqual(out["10M"]["accuracy"], 0.5)
        # 7M bucket captures the rich executable ask (the priced-out case).
        self.assertEqual(out["7M"]["avg_executable_ask_cents"], 97.0)
        self.assertEqual(out["10M"]["avg_executable_ask_cents"], 61.0)

    def test_cohort_split_isolates_late_only(self):
        # late-only contract (7M only) vs a fuller one.
        rows = [self._row("LATE", "7M", "NO", 97.0, "NO")]
        for iv in INTERVAL_MARKS:
            rows.append(self._row("FULL", iv, "NO", 70.0, "NO"))
        split = econ.cohort_split(rows)
        self.assertEqual(split["late_only"]["contracts"], 1)
        self.assertEqual(split["full"]["contracts"], 1)
        self.assertEqual(split["late_only"]["seven_m"]["n"], 1)

    def test_matched_cohort_only_shared_contracts(self):
        rows = [self._row("X", "10M", "NO", 60.0, "NO"), self._row("X", "7M", "NO", 95.0, "NO"),
                self._row("Y", "7M", "NO", 95.0, "NO")]  # Y has no 10M
        m = econ.matched_cohort_comparison(rows, ["10M", "7M"])
        self.assertEqual(m["matched_contracts"], 1)  # only X
        self.assertEqual(m["by_interval"]["7M"]["n"], 1)

    def test_defensive_exit_grade_flip_and_lead(self):
        # Contract flips 10M YES -> 7M NO, settles NO (sustained true warning).
        rows = [self._row("F", "10M", "YES", 55.0, "NO"), self._row("F", "7M", "NO", 96.0, "NO")]
        # fix the 10M correctness/result to the settled NO
        for r in rows:
            r["official_result"] = "NO"
        g = econ.defensive_exit_grade(rows)
        self.assertEqual(g["contracts_with_flip"], 1)
        self.assertEqual(g["true_warnings"], 1)
        self.assertEqual(g["precision"], 1.0)
        # warning at 7M, original side worth ~4c (100-96) -> economically late
        self.assertEqual(g["economically_late"], 1)


if __name__ == "__main__":
    unittest.main()
