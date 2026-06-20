"""The three settlement reconcilers (performance / window_focus / learning)
must stop a pass once the wall-clock budget is exceeded, so a batch of slow
Kalshi get_market lookups can't monopolise the ~1s refresh loop. Leftover
tickers are retried next cycle. One slow (0.6s) lookup with a 0.5s budget must
halt after the first call."""
from __future__ import annotations

import os
import sys
import time
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)


class SlowClient:
    def __init__(self, result=""):
        self.calls = 0
        self.result = result

    def get_market(self, ticker):
        self.calls += 1
        time.sleep(0.6)  # a single lookup already exceeds a 0.5s budget
        return {"status": "open", "result": self.result}


class TestReconcileBudgets(unittest.TestCase):
    def setUp(self):
        os.environ["Q15_RECONCILE_BUDGET_SECONDS"] = "0.5"

    def tearDown(self):
        os.environ.pop("Q15_RECONCILE_BUDGET_SECONDS", None)

    def test_performance_reconcile_stops_at_budget(self):
        from performance import PerformanceTracker

        class Store:
            enabled = True
            def pending_settlements(self):
                return [{"ticker": f"T{i}", "side": "YES", "entry_ask": 50} for i in range(8)]

        client = SlowClient()
        perf = PerformanceTracker(Store(), client, interval=0.0)
        perf.reconcile(now=10_000.0, max_calls=12)
        self.assertLessEqual(client.calls, 2)  # not all 8

    def test_window_focus_reconcile_stops_at_budget(self):
        from q15_upgrade.window_focus import FocusSettings, TwoWindowFocusManager

        class Store:
            enabled = False

        class Notifier:
            enabled = True
            def send(self, msg):
                return True

        client = SlowClient()
        settings = FocusSettings(reconcile_interval_seconds=0.0, max_reconcile_calls=8)
        m = TwoWindowFocusManager(Store(), Notifier(), None, client, settings)
        m._last_reconcile = 0.0
        m._cycles = {
            f"k{i}": {"ticker": f"T{i}", "graded": False, "close_time": "2020-01-01T00:00:00+00:00"}
            for i in range(8)
        }
        m.reconcile_settlements(now=10_000.0)
        self.assertLessEqual(client.calls, 2)

    def test_learning_reconcile_stops_at_budget(self):
        from q15_upgrade.learning import LearningEngine

        engine = LearningEngine.__new__(LearningEngine)  # bypass heavy __init__
        engine._last_reconcile = 0.0
        engine.last_reconcile_at = 0.0
        engine.v4 = types.SimpleNamespace(reconcile_interval_seconds=0.0, max_reconcile_calls=20)

        class DB:
            enabled = True
            def pending_decisions(self):
                return [{"id": i, "ticker": f"T{i}", "side": "YES",
                         "executable_ask": 50, "estimated_fee": 1} for i in range(8)]
            def pending_end_predictions(self):
                return []
            def settle_decision(self, *a, **k):
                pass
            def settle_end_prediction(self, *a, **k):
                pass

        engine.db = DB()
        client = SlowClient()
        engine.reconcile(now=10_000.0, client=client)
        self.assertLessEqual(client.calls, 2)


if __name__ == "__main__":
    unittest.main()
