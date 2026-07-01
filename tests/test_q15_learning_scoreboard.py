from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.ledger_v95 import V95Ledger, TRACKED_CHECKPOINTS
from q15_upgrade.checkpoint_v94_unified import _detect_checkpoint
import notifications.reporting as reporting


def _mk_ledger():
    return V95Ledger(tempfile.mktemp(suffix=".sqlite3"))


def _core(d):
    """Just the win/loss core of a scoreboard bucket (drops CI/low_n extras)."""
    return {k: d[k] for k in ("right", "wrong", "n", "accuracy")}


def _record(led, ticker, cp, side, rank):
    return led.record_prediction(
        ticker=ticker, asset="ETH", checkpoint=cp, created_at=time.time(),
        close_time=time.time() + 600, predicted_side=side,
        raw_yes_probability=0.7, calibrated_yes_probability=0.7, challenger_yes_probability=0.7,
        baseline_yes_probability=0.6, selected_probability=0.7, conservative_probability=0.65,
        data_quality=0.8, evidence_quality=0.7, trade_quality=0.7,
        trade_decision="ENTRY_RECOMMENDED", regime="NORMAL",
        features={"momentum": 0.3}, contributions={"momentum": 0.2}, quote={"ask_cents": 50},
        rank=rank,
    )


class TestLedgerScoreboard(unittest.TestCase):
    def test_7m_is_a_tracked_checkpoint(self):
        self.assertIn("7M", TRACKED_CHECKPOINTS)
        led = _mk_ledger()
        self.assertEqual(led._checkpoint("7M"), "7M")
        self.assertEqual(led._checkpoint("7m"), "7M")
        self.assertEqual(led._checkpoint("garbage"), "10M")

    def test_records_rank_and_grades_by_interval_and_rank(self):
        led = _mk_ledger()
        _record(led, "T-15", "15M", "YES", 1)
        _record(led, "T-10", "10M", "YES", 1)
        _record(led, "T-7a", "7M", "NO", 2)
        _record(led, "T-7b", "7M", "YES", 3)
        led.resolve_ticker("T-15", "YES")   # #1 15M right
        led.resolve_ticker("T-10", "NO")    # #1 10M wrong
        led.resolve_ticker("T-7a", "NO")    # #2 7M right
        led.resolve_ticker("T-7b", "NO")    # #3 7M wrong

        sb = led.scoreboard()
        self.assertTrue(sb["available"])
        self.assertEqual(_core(sb["by_checkpoint"]["15M"]), {"right": 1, "wrong": 0, "n": 1, "accuracy": 1.0})
        self.assertEqual(_core(sb["by_checkpoint"]["10M"]), {"right": 0, "wrong": 1, "n": 1, "accuracy": 0.0})
        self.assertEqual(_core(sb["by_checkpoint"]["7M"]), {"right": 1, "wrong": 1, "n": 2, "accuracy": 0.5})
        self.assertEqual(_core(sb["by_rank"]["1"]), {"right": 1, "wrong": 1, "n": 2, "accuracy": 0.5})
        self.assertEqual(sb["by_rank"]["2"]["right"], 1)
        self.assertEqual(sb["by_rank"]["3"]["wrong"], 1)
        # Rank record separated by interval: the all-interval #1 blends 15M (right)
        # + 10M (wrong) into 1-1, but per-interval keeps them distinct.
        rbc = sb["rank_by_checkpoint"]
        self.assertEqual(_core(rbc["15M"]["1"]), {"right": 1, "wrong": 0, "n": 1, "accuracy": 1.0})
        self.assertEqual(_core(rbc["10M"]["1"]), {"right": 0, "wrong": 1, "n": 1, "accuracy": 0.0})
        self.assertEqual(rbc["7M"]["2"]["right"], 1)
        self.assertEqual(rbc["7M"]["3"]["wrong"], 1)
        self.assertEqual(rbc["10M"]["2"]["n"], 0)  # no 10M #2 settled

    def test_scoreboard_breaks_down_by_asset(self):
        led = _mk_ledger()
        # ETH #1 right, ETH #1 wrong, BTC #1 right, SOL #2 right
        for t, asset, cp, side, rank, res in [
            ("E1", "ETH", "10M", "YES", 1, "YES"),
            ("E2", "ETH", "10M", "YES", 1, "NO"),
            ("B1", "BTC", "10M", "YES", 1, "YES"),
            ("S1", "SOL", "7M", "NO", 2, "NO"),
        ]:
            led.record_prediction(
                ticker=t, asset=asset, checkpoint=cp, created_at=time.time(),
                close_time=time.time() + 600, predicted_side=side,
                raw_yes_probability=0.7, calibrated_yes_probability=0.7, challenger_yes_probability=0.7,
                baseline_yes_probability=0.6, selected_probability=0.7, conservative_probability=0.65,
                data_quality=0.8, evidence_quality=0.7, trade_quality=0.7,
                trade_decision="ENTRY_RECOMMENDED", regime="NORMAL",
                features={"momentum": 0.3}, contributions={"momentum": 0.2}, quote={"ask_cents": 50}, rank=rank,
            )
            led.resolve_ticker(t, res)
        sb = led.scoreboard()
        self.assertEqual(_core(sb["by_asset"]["ETH"]), {"right": 1, "wrong": 1, "n": 2, "accuracy": 0.5})
        self.assertEqual(sb["by_asset"]["BTC"]["right"], 1)
        # top_pick (#1) per coin: ETH 1/2, BTC 1/1; SOL was rank #2 so excluded
        self.assertEqual(_core(sb["top_pick_by_asset"]["ETH"]), {"right": 1, "wrong": 1, "n": 2, "accuracy": 0.5})
        self.assertEqual(sb["top_pick_by_asset"]["BTC"]["accuracy"], 1.0)
        self.assertNotIn("SOL", sb["top_pick_by_asset"])

    def test_status_counts_7m(self):
        led = _mk_ledger()
        _record(led, "S-7", "7M", "YES", 1)
        self.assertEqual(led.status()["seven_minute_predictions"], 1)

    def test_metrics_includes_scoreboard(self):
        led = _mk_ledger()
        _record(led, "M-1", "10M", "YES", 1)
        led.resolve_ticker("M-1", "YES")
        self.assertIn("scoreboard", led.metrics())

    def test_metrics_reports_expected_calibration_error(self):
        led = _mk_ledger()
        # Two predictions at selected_probability 0.70 → both in the 70-75% band.
        # One wins, one loses → actual win rate 0.50 vs mean predicted 0.70.
        _record(led, "C-1", "10M", "YES", 1)
        _record(led, "C-2", "10M", "YES", 1)
        led.resolve_ticker("C-1", "YES")  # correct
        led.resolve_ticker("C-2", "NO")   # wrong
        m = led.metrics()
        self.assertIn("expected_calibration_error", m)
        self.assertAlmostEqual(m["expected_calibration_error"], 0.20, places=6)

    def test_ece_is_none_without_resolved_rows(self):
        led = _mk_ledger()
        _record(led, "C-3", "10M", "YES", 1)  # recorded, never resolved
        self.assertIsNone(led.metrics()["expected_calibration_error"])

    def test_rank_persists_across_reopen(self):
        path = tempfile.mktemp(suffix=".sqlite3")
        led = V95Ledger(path)
        _record(led, "R-1", "10M", "YES", 2)
        led.resolve_ticker("R-1", "YES")
        # Reopen: migration path (_ensure_column) must be a no-op and data intact.
        led2 = V95Ledger(path)
        self.assertEqual(led2.scoreboard()["by_rank"]["2"]["right"], 1)

    def test_rank_quality_flags_inverted_top_pick(self):
        led = _mk_ledger()
        # #1 goes 1/5 while the non-#1 pool goes 25/25, so #1 trails the
        # comparison pool's Wilson lower bound. This is report-only.
        for i in range(5):
            _record(led, f"R1-{i}", "10M", "YES", 1)
            led.resolve_ticker(f"R1-{i}", "YES" if i == 0 else "NO")
        for i in range(20):
            _record(led, f"R2-{i}", "10M", "YES", 2)
            led.resolve_ticker(f"R2-{i}", "YES")
        for i in range(5):
            _record(led, f"REST-{i}", "10M", "YES", 4)
            led.resolve_ticker(f"REST-{i}", "YES")

        rq = led.rank_quality_scoreboard(limit=300)["by_checkpoint"]["10M"]
        self.assertEqual(rq["rank1"]["n"], 5)
        self.assertEqual(rq["rank23"]["n"], 20)
        self.assertEqual(rq["rest"]["n"], 5)
        self.assertTrue(rq["rank_inverted"])


class TestSevenMinuteDetection(unittest.TestCase):
    def test_explicit_7m(self):
        self.assertEqual(_detect_checkpoint({"ETH": {"checkpoint": "7M"}}, []), "7M")

    def test_time_based_buckets(self):
        now = time.time()
        self.assertEqual(_detect_checkpoint({"E": {"seconds_remaining": 800}}, []), "15M")
        self.assertEqual(_detect_checkpoint({"E": {"seconds_remaining": 560}}, []), "10M")
        self.assertEqual(_detect_checkpoint({"E": {"seconds_remaining": 420}}, []), "7M")

    def test_message_7m(self):
        self.assertEqual(_detect_checkpoint({}, ["7M FINAL CHECK"]), "7M")


class _FakeLedger:
    def __init__(self, sb):
        self._sb = sb
    def scoreboard(self):
        return self._sb


class TestHourlyReportScoreboard(unittest.TestCase):
    def _reporter(self, ledger):
        r = reporting.HourlyReporter(None, None, None, None, None, None, v95_ledger=ledger)
        return r

    def test_lines_render_when_data_present(self):
        sb = {
            "available": True,
            "overall": {"right": 5, "wrong": 3, "n": 8, "accuracy": 0.625},
            "by_checkpoint": {
                "15M": {"right": 2, "wrong": 1, "n": 3, "accuracy": 0.667},
                "10M": {"right": 2, "wrong": 1, "n": 3, "accuracy": 0.667},
                "7M": {"right": 1, "wrong": 1, "n": 2, "accuracy": 0.5},
            },
            "by_rank": {
                "1": {"right": 3, "wrong": 1, "n": 4, "accuracy": 0.75},
                "2": {"right": 1, "wrong": 1, "n": 2, "accuracy": 0.5},
                "3": {"right": 1, "wrong": 1, "n": 2, "accuracy": 0.5},
                "other": {"right": 0, "wrong": 0, "n": 0, "accuracy": None},
            },
            "rank_by_checkpoint": {
                "15M": {
                    "1": {"right": 3, "wrong": 0, "n": 3, "accuracy": 1.0},
                    "2": {"right": 1, "wrong": 2, "n": 3, "accuracy": 0.333},
                    "3": {"right": 0, "wrong": 0, "n": 0, "accuracy": None},
                },
                "10M": {
                    "1": {"right": 2, "wrong": 0, "n": 2, "accuracy": 1.0},
                    "2": {"right": 1, "wrong": 1, "n": 2, "accuracy": 0.5},
                    "3": {"right": 0, "wrong": 0, "n": 0, "accuracy": None},
                },
            },
        }
        sb["overall"]["accuracy"] = 0.625
        text = "\n".join(self._reporter(_FakeLedger(sb))._scoreboard_table())
        self.assertIn("Track record", text)
        self.assertIn("Settled 8 ·", text)
        self.assertIn("right", text)
        # The scoreboard helper now returns plain content; build_report owns the
        # single <pre> panel (see test_full_report_is_one_pre_panel).
        self.assertNotIn("<pre>", text)
        self.assertNotIn("<b>", text)
        # aligned rows for interval and rank
        self.assertIn("15M", text)
        self.assertIn("7M", text)
        self.assertIn("#1 pick", text)
        self.assertIn("67%", text)
        # A 15M-specific rank section with its own header and the 15M #1 record.
        self.assertIn("15M RANK PERFORMANCE", text)
        rank15_block = text.split("15M RANK PERFORMANCE", 1)[1].split("10M RANK PERFORMANCE", 1)[0]
        self.assertIn("3-0", rank15_block)  # 15M #1 went 3-0
        # A 10M-specific rank section with its own header and the 10M #1 record.
        self.assertIn("10M RANK PERFORMANCE", text)
        rank_block = text.split("10M RANK PERFORMANCE", 1)[1]
        self.assertIn("100%", rank_block)  # 10M #1 went 2-0
        self.assertIn("2-0", rank_block)

    def test_ten_minute_rank_section_shows_placeholders_before_settling(self):
        # The 10M rank section is always visible (0-0 rows) so the user sees it is
        # tracked even before any 10M pick has settled.
        sb = {
            "available": True,
            "overall": {"right": 2, "wrong": 1, "n": 3, "accuracy": 0.667},
            "by_checkpoint": {"15M": {"right": 2, "wrong": 1, "n": 3, "accuracy": 0.667}},
            "by_rank": {}, "rank_by_checkpoint": {},
        }
        text = "\n".join(self._reporter(_FakeLedger(sb))._scoreboard_table())
        self.assertIn("10M RANK PERFORMANCE", text)
        rank_block = text.split("10M RANK PERFORMANCE", 1)[1]
        self.assertIn("#1 pick", rank_block)
        self.assertIn("0-0", rank_block)

    def test_all_three_checkpoints_shown_even_when_empty(self):
        # 15M has settled rows; 10M/7M have none yet. All three must still appear
        # (10M/7M as zeroed placeholders) so the user can see they're tracked.
        sb = {
            "available": True,
            "overall": {"right": 2, "wrong": 1, "n": 3, "accuracy": 0.667},
            "by_checkpoint": {"15M": {"right": 2, "wrong": 1, "n": 3, "accuracy": 0.667}},
            "by_rank": {},
        }
        text = "\n".join(self._reporter(_FakeLedger(sb))._scoreboard_table())
        self.assertIn("15M", text)
        self.assertIn("10M", text)   # placeholder row, previously hidden
        self.assertIn("7M", text)    # placeholder row, previously hidden
        self.assertIn("0-0", text)   # the zeroed "awaiting data" marker

    def test_full_report_is_one_pre_panel(self):
        # The whole report body renders inside a single <pre> panel; only the bold
        # "Hourly Report —" header (reformatter-bypass marker) stays outside it.
        sb = {
            "available": True,
            "overall": {"right": 5, "wrong": 3, "n": 8, "accuracy": 0.625, "realized_total_cents": 12, "pnl_n": 8},
            "by_checkpoint": {"10M": {"right": 2, "wrong": 1, "n": 3, "accuracy": 0.667}},
            "by_rank": {}, "rank_by_checkpoint": {},
        }
        text = self._reporter(_FakeLedger(sb)).build_report()
        self.assertEqual(text.count("<pre>"), 1)
        self.assertEqual(text.count("</pre>"), 1)
        # Header is outside the panel and keeps the canonical marker.
        head, _, panel = text.partition("<pre>")
        self.assertIn("Hourly Report —", head)
        self.assertNotIn("<pre>", head)
        # Body content lives inside the panel.
        self.assertIn("Track record", panel)
        self.assertIn("Settled 8", panel)

    def test_full_report_includes_ultoim_v2_exit_warning_delivery_gap(self):
        sb = {
            "available": True,
            "overall": {"right": 5, "wrong": 3, "n": 8, "accuracy": 0.625},
            "by_checkpoint": {"10M": {"right": 2, "wrong": 1, "n": 3, "accuracy": 0.667}},
            "by_rank": {}, "rank_by_checkpoint": {},
        }

        class _ExitRunner:
            def exit_warning_delivery_counts_24h(self):
                return {
                    "recorded": 18,
                    "sent": 2,
                    "counts": {"SENT": 2, "MUTED": 16},
                }

        with patch("q15_upgrade.ultoim_v2.runner.get_runner", return_value=_ExitRunner()):
            text = self._reporter(_FakeLedger(sb)).build_report()

        self.assertIn("Hourly Report", text)
        self.assertIn("Ultoim V2 exit warnings 24h: recorded 18", text)
        self.assertIn("SENT 2/18", text)
        self.assertIn("muted 16", text)

    def test_full_report_includes_rank_quality_inversion_line(self):
        sb = {
            "available": True,
            "overall": {"right": 5, "wrong": 3, "n": 8, "accuracy": 0.625},
            "by_checkpoint": {"10M": {"right": 2, "wrong": 1, "n": 3, "accuracy": 0.667}},
            "by_rank": {}, "rank_by_checkpoint": {},
        }

        class _RankLedger(_FakeLedger):
            def rank_quality_scoreboard(self, limit=300):
                return {
                    "available": True,
                    "limit": limit,
                    "by_checkpoint": {
                        "10M": {
                            "n": 30,
                            "rank1": {"n": 5, "accuracy": 0.2, "ci_low": 0.04, "ci_high": 0.62},
                            "rank23": {"n": 20, "accuracy": 1.0, "ci_low": 0.84, "ci_high": 1.0},
                            "rest": {"n": 5, "accuracy": 1.0, "ci_low": 0.57, "ci_high": 1.0},
                            "rank_inverted": True,
                        }
                    },
                }

        text = self._reporter(_RankLedger(sb)).build_report()
        self.assertIn("Rank quality 10M last 30", text)
        self.assertIn("#1 20% [4-62] n=5", text)
        self.assertIn("#2-3 100% [84-100] n=20", text)
        self.assertIn("RANK INVERTED", text)

    def test_header_is_eastern_time(self):
        reporter = reporting.HourlyReporter(None, None, None, None, None, None, v95_ledger=None)
        header = reporting._eastern_header()
        # Eastern, not UTC, and carries an AM/PM + tz label.
        self.assertNotIn("UTC", header)
        self.assertTrue(("AM" in header) or ("PM" in header))
        self.assertTrue(header.endswith("EST") or header.endswith("EDT"))

    def test_empty_shows_building_history(self):
        sb = {"available": True, "overall": {"n": 0}, "by_checkpoint": {}, "by_rank": {}}
        lines = self._reporter(_FakeLedger(sb))._scoreboard_table()
        self.assertEqual(lines, ["No settled predictions yet — building history."])

    def test_no_ledger_is_safe(self):
        self.assertEqual(self._reporter(None)._scoreboard_table(), [])


class _FakeFlipLedger:
    """Ledger stub exposing only the two flip-report methods."""
    def __init__(self, perf, stats):
        self._perf, self._stats = perf, stats
    def flip_warning_performance(self):
        return self._perf
    def flip_stats(self):
        return self._stats


class TestHourlyFlipScoreboard(unittest.TestCase):
    def _reporter(self, ledger):
        return reporting.HourlyReporter(None, None, None, None, None, None, v95_ledger=ledger)

    def _perf(self):
        agg = lambda alerts, correct, false, prec, det, act, miss, pnl: {
            "alerts": alerts, "correct": correct, "false": false, "precision": prec,
            "detected": det, "actual_flips": act, "missed": miss,
            "detection_rate": (det / act if act else None), "avg_advance_seconds": 150,
            "realized_total_cents": pnl,
        }
        return {
            "overall": agg(3, 2, 1, 0.667, 2, 4, 2, 18),
            "by_checkpoint": {"15M": agg(2, 1, 1, 0.5, 1, 2, 1, 8),
                              "10M": agg(1, 1, 0, 1.0, 1, 2, 1, 10)},
            "by_direction": {"NO → YES": agg(2, 2, 0, 1.0, 2, 3, 1, 16),
                             "YES → NO": agg(1, 0, 1, 0.0, 0, 1, 1, 2)},
            "by_asset": {"BTC": agg(3, 2, 1, 0.667, 2, 4, 2, 18)},
            "by_score_bucket": {},
        }

    def _stats(self):
        return {"available": True, "by_checkpoint": {"10M": {
            "NO → YES": {"overall": {"samples": 12, "buckets": {
                "40-60%": {"n": 6, "flip_rate": 0.33},
                "60-80%": {"n": 5, "flip_rate": 0.6},
            }}},
            "YES → NO": {"overall": {"samples": 0, "buckets": {}}},
        }}}

    def test_flip_table_uses_interval_format(self):
        text = "\n".join(self._reporter(_FakeFlipLedger(self._perf(), self._stats()))._flip_scoreboard())
        # Same aligned grid as the intervals: a W-L/Acc/P-L header.
        self.assertIn("FLIP WARNING PERFORMANCE", text)
        self.assertIn("W-L", text)
        self.assertIn("Acc", text)
        # Rows by checkpoint (placeholder for the missing 7M), direction, asset.
        self.assertIn("15M", text)
        self.assertIn("7M", text)            # 0-0 placeholder
        self.assertIn("BY DIRECTION", text)
        self.assertIn("NO→YES", text)
        self.assertIn("BY ASSET", text)
        self.assertIn("BTC", text)
        # Precision renders as the Acc column (10M went 1-0 = 100%).
        self.assertIn("100%", text)
        # Learned flip-rate curve carried through as its own mini-table.
        self.assertIn("LEARNED FLIP RATE", text)

    def test_flip_table_empty_until_warned_when_alerts_armed(self):
        # Alerts armed but nothing tripped yet, no learned curve -> empty.
        empty = {"overall": {"alerts": 0}, "alerts_enabled": True,
                 "by_checkpoint": {}, "by_direction": {}, "by_asset": {}}
        out = self._reporter(_FakeFlipLedger(empty, {"available": False}))._flip_scoreboard()
        self.assertEqual(out, [])

    def test_flip_table_notes_disabled_channel(self):
        # Alerts OFF (default): the empty record must be labelled as a disabled
        # channel, not left blank (which reads as a detection failure).
        empty = {"overall": {"alerts": 0}, "alerts_enabled": False,
                 "by_checkpoint": {}, "by_direction": {}, "by_asset": {}}
        out = self._reporter(_FakeFlipLedger(empty, {"available": False}))._flip_scoreboard()
        self.assertTrue(any("disabled" in ln.lower() for ln in out))

    def test_flip_table_safe_without_methods(self):
        # build_report's fake ledger may lack the flip methods entirely.
        class _Bare:
            def scoreboard(self):
                return {"available": True, "overall": {"n": 0}, "by_checkpoint": {}, "by_rank": {}}
        self.assertEqual(self._reporter(_Bare())._flip_scoreboard(), [])


if __name__ == "__main__":
    unittest.main()


class TestRunCycleRecordsRankAndSevenMinute(unittest.TestCase):
    """End-to-end: a 7-minute cycle records each prediction as 7M with its rank."""

    def setUp(self):
        self._prior_public = os.environ.get("Q15_V95_PUBLIC_DATA_ENABLED")
        os.environ["Q15_V95_PUBLIC_DATA_ENABLED"] = "false"
        from q15_upgrade.checkpoint_v95 import CheckpointPolicyV95
        from tests.test_q15_v95 import snapshot, candles, FakeHub, FakeNotifier
        self._snapshot, self._candles = snapshot, candles
        self.tmp = tempfile.TemporaryDirectory()
        self.policy = CheckpointPolicyV95(None)
        self.policy.ledger = V95Ledger(os.path.join(self.tmp.name, "led.sqlite3"))
        self.policy.market_data = FakeHub()
        self.FakeNotifier = FakeNotifier

    def tearDown(self):
        self.tmp.cleanup()
        if self._prior_public is None:
            os.environ.pop("Q15_V95_PUBLIC_DATA_ENABLED", None)
        else:
            os.environ["Q15_V95_PUBLIC_DATA_ENABLED"] = self._prior_public
        # A real cycle populates module-level "latest" caches; clear them so we
        # don't leak state into tests asserting a pre-first-cycle startup state.
        import q15_upgrade.checkpoint_v95 as cp95
        with cp95._LATEST_LOCK:
            cp95._LATEST_ANALYSES.clear()
            cp95._LATEST_RANKING.clear()
            cp95._LATEST_LEDGER.clear()
            cp95._LATEST_CHECKPOINT = "UNKNOWN"

    def test_no_entry_checkpoint_panel_behaviour(self):
        # A pricey ask -> no executable edge -> no recommended entry. Under the
        # default compact panel, a forward-looking panel STILL goes out every
        # checkpoint (deduped to one), but as a NON-entry state — never
        # ENTER NOW / ENTRY RECOMMENDED. The legacy entry-only muting is preserved
        # behind Q15_V95_COMPACT_PANEL=false.
        class FM:
            def update(self, snaps, now, wsh):
                return snaps

        class CE:
            def enrich_all(self, snaps, now, wsh):
                return snaps

        def _run(base_now, env):
            notifier = self.FakeNotifier()
            with patch.dict(os.environ, env):
                for i in range(5):
                    snaps = {"BNB": self._snapshot(asset="BNB", checkpoint="10M", ask=98.0,
                                                   target=100.0, spot=101.0)}
                    for s in snaps.values():
                        s["seconds_remaining"] = 600
                        s["underlying_candles_5s"] = self._candles()[-12:]
                        s["close_time"] = base_now + 600
                    self.policy.run_cycle(dict(snaps), base_now + i, {}, FM(), CE(), notifier)
            return [m for m in notifier.messages if "V9.5 CHECK" in m]

        # New default (compact ON): exactly one panel, and it is NOT an entry.
        compact_msgs = _run(time.time(), {})
        self.assertEqual(len(compact_msgs), 1)
        self.assertNotIn("ENTER NOW", compact_msgs[0])
        self.assertNotIn("ENTRY RECOMMENDED", compact_msgs[0])

        # Legacy entry-only muting still available behind the flag (distinct
        # market window so the dedup ledger from the run above can't interfere).
        legacy_msgs = _run(time.time() + 5000, {"Q15_V95_COMPACT_PANEL": "false"})
        self.assertEqual(legacy_msgs, [])

    def test_cycle_buckets_seven_minute_and_persists_rank(self):
        now = time.time()
        snaps = {
            "BNB": self._snapshot(asset="BNB", checkpoint="7M", ask=52.0, target=100.0, spot=101.0),
            "SOL": self._snapshot(asset="SOL", checkpoint="7M", ask=60.0, target=100.0, spot=100.5),
        }
        for s in snaps.values():
            s["seconds_remaining"] = 420
            s["underlying_candles_5s"] = self._candles()[-12:]
            s["close_time"] = now + 420

        class FM:
            def update(self, snaps, now, wsh):
                return snaps

        class CE:
            def enrich_all(self, snaps, now, wsh):
                return snaps

        self.policy.run_cycle(dict(snaps), now, {}, FM(), CE(), self.FakeNotifier())

        self.assertEqual(self.policy._last_checkpoint_v95, "7M")
        status = self.policy.ledger.status()
        self.assertEqual(status["seven_minute_predictions"], 2)
        self.assertEqual(status["ten_minute_predictions"], 0)  # not mislabeled
        # Ranks were persisted (one #1, one #2).
        sb = self.policy.scoreboard()
        self.assertEqual(sb["by_checkpoint"]["7M"]["n"], 0)  # unresolved yet
        import sqlite3
        con = sqlite3.connect(str(self.policy.ledger.path))
        ranks = sorted(r[0] for r in con.execute("SELECT rank FROM predictions"))
        con.close()
        self.assertEqual(ranks, [1, 2])
