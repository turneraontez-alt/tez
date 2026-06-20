from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.ledger_v95 import V95Ledger, TRACKED_CHECKPOINTS
from q15_upgrade.checkpoint_v94_unified import _detect_checkpoint
import reporting


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

    def test_rank_persists_across_reopen(self):
        path = tempfile.mktemp(suffix=".sqlite3")
        led = V95Ledger(path)
        _record(led, "R-1", "10M", "YES", 2)
        led.resolve_ticker("R-1", "YES")
        # Reopen: migration path (_ensure_column) must be a no-op and data intact.
        led2 = V95Ledger(path)
        self.assertEqual(led2.scoreboard()["by_rank"]["2"]["right"], 1)


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
        }
        sb["overall"]["accuracy"] = 0.625
        text = "\n".join(self._reporter(_FakeLedger(sb))._scoreboard_table())
        self.assertIn("Track record", text)
        self.assertIn("Settled 8 ·", text)
        self.assertIn("right", text)
        self.assertIn("<pre>", text)
        # aligned rows for interval and rank
        self.assertIn("15M", text)
        self.assertIn("7M", text)
        self.assertIn("#1 pick", text)
        self.assertIn("67%", text)

    def test_empty_shows_building_history(self):
        sb = {"available": True, "overall": {"n": 0}, "by_checkpoint": {}, "by_rank": {}}
        lines = self._reporter(_FakeLedger(sb))._scoreboard_table()
        self.assertEqual(lines, ["No settled predictions yet — building history."])

    def test_no_ledger_is_safe(self):
        self.assertEqual(self._reporter(None)._scoreboard_table(), [])


if __name__ == "__main__":
    unittest.main()


class TestRunCycleRecordsRankAndSevenMinute(unittest.TestCase):
    """End-to-end: a 7-minute cycle records each prediction as 7M with its rank."""

    def setUp(self):
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
        # A real cycle populates module-level "latest" caches; clear them so we
        # don't leak state into tests asserting a pre-first-cycle startup state.
        import q15_upgrade.checkpoint_v95 as cp95
        with cp95._LATEST_LOCK:
            cp95._LATEST_ANALYSES.clear()
            cp95._LATEST_RANKING.clear()
            cp95._LATEST_LEDGER.clear()
            cp95._LATEST_CHECKPOINT = "UNKNOWN"

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
