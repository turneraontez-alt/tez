from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)


class TestFormatRunCycleBreakdown(unittest.TestCase):
    """The pure formatter is the load-bearing, deterministic part: it must name
    every dominant sub-stage and tolerate partial/missing timing dicts."""

    def test_names_dominant_substages(self):
        from q15_upgrade.checkpoint_v95 import _format_run_cycle_breakdown

        timing = {
            "total": 5.55, "parent_chain": 3.20, "v95_analysis": 1.80,
            "research_overlays": 0.75,
            "research_overlay_sub": {
                "marketlead": 0.10, "interval_research": 0.65,
            },
            "market_reconcile": 0.40, "signal_store_reconcile": 0.05, "other": 0.10,
            "v95_sub": {"deepcopy": 0.2, "build": 0.3, "analyse": 0.9, "record": 0.4},
        }
        chain = {"v94_super_chain": 1.0, "v91_pre_enrich": 1.5, "v91_finalize_all": 0.7}
        out = _format_run_cycle_breakdown(timing, chain)

        self.assertIn("total=5.55s", out)
        self.assertIn("parent_chain=3.20s", out)
        self.assertIn("v95_analysis=1.80s", out)
        # parent-chain remote-Postgres suspects are named explicitly
        self.assertIn("v91_pre_enrich=1.50s", out)
        self.assertIn("v91_finalize_all=0.70s", out)
        # v95 sub-stages and the periodic reconcile are named explicitly
        self.assertIn("record=0.40s", out)
        self.assertIn("analyse=0.90s", out)
        self.assertIn("research_overlays=0.75s", out)
        self.assertIn("interval_research=0.65s", out)
        self.assertIn("market_reconcile=0.40s", out)

    def test_tolerates_missing_and_malformed(self):
        from q15_upgrade.checkpoint_v95 import _format_run_cycle_breakdown

        # Only a total present, no chain timing, no v95_sub.
        out = _format_run_cycle_breakdown({"total": 1.0}, {})
        self.assertIn("total=1.00s", out)
        self.assertIn("parent_chain=0.00s", out)
        # A malformed (non-numeric) value must coerce to 0.0, not raise.
        out2 = _format_run_cycle_breakdown({"total": "oops", "v95_sub": None}, {})
        self.assertIn("total=0.00s", out2)


class TestSlowCycleEmitsBreakdownLog(unittest.TestCase):
    def setUp(self):
        os.environ["Q15_V95_PUBLIC_DATA_ENABLED"] = "false"
        # Force every cycle to count as "slow" so the diagnostic log fires
        # deterministically regardless of how fast the test box runs.
        os.environ["Q15_V95_SLOW_CYCLE_SECONDS"] = "0.0"
        from q15_upgrade.checkpoint_v95 import CheckpointPolicyV95
        from q15_upgrade.ledger_v95 import V95Ledger
        from tests.test_q15_v95 import snapshot, candles, FakeHub, FakeNotifier
        self._snapshot, self._candles, self.FakeNotifier = snapshot, candles, FakeNotifier
        self.tmp = tempfile.TemporaryDirectory()
        self.policy = CheckpointPolicyV95(None)
        self.policy.ledger = V95Ledger(os.path.join(self.tmp.name, "led.sqlite3"))
        self.policy.market_data = FakeHub()

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("Q15_V95_PUBLIC_DATA_ENABLED", None)
        os.environ.pop("Q15_V95_SLOW_CYCLE_SECONDS", None)
        import q15_upgrade.checkpoint_v95 as cp95
        with cp95._LATEST_LOCK:
            cp95._LATEST_ANALYSES.clear()
            cp95._LATEST_RANKING.clear()
            cp95._LATEST_LEDGER.clear()
            cp95._LATEST_CHECKPOINT = "UNKNOWN"

    def _run_one(self):
        now = time.time()
        snaps = {"BNB": self._snapshot(asset="BNB", checkpoint="15M", ask=52.0, target=100.0, spot=101.0)}
        for s in snaps.values():
            s["seconds_remaining"] = 900
            s["underlying_candles_5s"] = self._candles()[-12:]
            s["close_time"] = now + 900

        class FM:
            def update(self, snaps, now, wsh):
                return snaps

        class CE:
            def enrich_all(self, snaps, now, wsh):
                return snaps

        return now, lambda: self.policy.run_cycle(dict(snaps), now, {}, FM(), CE(), self.FakeNotifier())

    def test_slow_cycle_logs_named_breakdown(self):
        _now, run = self._run_one()
        with self.assertLogs("q15_upgrade.checkpoint_v95", level="WARNING") as cm:
            run()
        slow = [m for m in cm.output if "slow run_cycle" in m]
        self.assertTrue(slow, f"expected a 'slow run_cycle' breakdown log, got: {cm.output}")
        # The breakdown must name the top buckets so the log is actionable.
        line = slow[0]
        self.assertIn("total=", line)
        self.assertIn("parent_chain=", line)
        self.assertIn("v95_analysis=", line)

    def test_throttled_to_once_per_window(self):
        # Two cycles at the same `now` (within the throttle window) -> one log.
        now = time.time()
        snaps = {"BNB": self._snapshot(asset="BNB", checkpoint="15M", ask=52.0, target=100.0, spot=101.0)}
        for s in snaps.values():
            s["seconds_remaining"] = 900
            s["underlying_candles_5s"] = self._candles()[-12:]
            s["close_time"] = now + 900

        class FM:
            def update(self, snaps, now, wsh):
                return snaps

        class CE:
            def enrich_all(self, snaps, now, wsh):
                return snaps

        with self.assertLogs("q15_upgrade.checkpoint_v95", level="WARNING") as cm:
            self.policy.run_cycle(dict(snaps), now, {}, FM(), CE(), self.FakeNotifier())
            self.policy.run_cycle(dict(snaps), now, {}, FM(), CE(), self.FakeNotifier())
        slow = [m for m in cm.output if "slow run_cycle" in m]
        self.assertEqual(len(slow), 1, f"throttle should collapse to one log per window: {slow}")


if __name__ == "__main__":
    unittest.main()
