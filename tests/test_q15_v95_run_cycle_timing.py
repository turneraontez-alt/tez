from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)


class TestRunCycleTiming(unittest.TestCase):
    def setUp(self):
        os.environ["Q15_V95_PUBLIC_DATA_ENABLED"] = "false"
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
        import q15_upgrade.checkpoint_v95 as cp95
        with cp95._LATEST_LOCK:
            cp95._LATEST_ANALYSES.clear()
            cp95._LATEST_RANKING.clear()
            cp95._LATEST_LEDGER.clear()
            cp95._LATEST_CHECKPOINT = "UNKNOWN"
        os.environ.pop("Q15_V95_PUBLIC_DATA_ENABLED", None)

    def test_run_cycle_timing_is_populated_in_health(self):
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

        self.policy.run_cycle(dict(snaps), now, {}, FM(), CE(), self.FakeNotifier())

        timing = self.policy.health()["run_cycle_timing"]
        for key in ("parent_chain", "v95_analysis", "total", "other"):
            self.assertIn(key, timing)
        self.assertGreaterEqual(timing["total"], 0.0)
        # total accounts for the measured spans plus the unattributed remainder.
        self.assertGreaterEqual(timing["other"], 0.0)


if __name__ == "__main__":
    unittest.main()
