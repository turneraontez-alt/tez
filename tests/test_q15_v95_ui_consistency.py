"""UI consistency (item 2) + per-prediction snapshot fields (item 3 data path).

  * format_telegram_message never re-renders a message in the legacy v94 layout
    by default (the old UI is disabled); V9.5 CHECK messages always pass through
    unchanged so the clean UI owns the checkpoint alert.
  * a real run_cycle stamps each prediction snapshot with the richer card fields
    (interval, grade, confidence, P(yes)/P(no), timestamp, stability, expiry).
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

import q15_upgrade.checkpoint_v95 as cp95
from q15_upgrade.checkpoint_v95 import format_telegram_message


class TestNoLegacyUI(unittest.TestCase):
    def setUp(self):
        with cp95._LATEST_LOCK:
            cp95._LATEST_ANALYSES.clear()
            cp95._LATEST_RANKING.clear()
            cp95._LATEST_CHECKPOINT = "UNKNOWN"

    def test_v95_check_passes_through_unchanged(self):
        msg = "👀 <b>10M V9.5 CHECK · NO ENTRY YET</b>\nBest: BTC YES 70% (grade A)"
        self.assertEqual(format_telegram_message(msg), msg)

    def test_non_q15_message_is_not_reformatted_by_default(self):
        # A non-checkpoint alert (no Q15 tokens) must not be rebuilt in old UI.
        msg = "🎯 DIP — BTC YES +5¢ vs market"
        self.assertEqual(format_telegram_message(msg), msg)

    def test_legacy_fallback_is_opt_in(self):
        msg = "some plain non-q15 notification"
        self.assertEqual(format_telegram_message(msg), msg)  # default: unchanged
        os.environ["Q15_V95_LEGACY_FALLBACK_FORMAT"] = "true"
        try:
            # Opt-in path runs the legacy reformatter; just assert it stays a str.
            self.assertIsInstance(format_telegram_message(msg), str)
        finally:
            del os.environ["Q15_V95_LEGACY_FALLBACK_FORMAT"]


class TestRunCycleStampsCardFields(unittest.TestCase):
    def setUp(self):
        os.environ["Q15_V95_PUBLIC_DATA_ENABLED"] = "false"
        from q15_upgrade.checkpoint_v95 import CheckpointPolicyV95
        from q15_upgrade.ledger_v95 import V95Ledger
        from tests.test_q15_v95 import snapshot, candles, FakeHub, FakeNotifier
        self._snapshot, self._candles, self._FakeNotifier = snapshot, candles, FakeNotifier
        self.tmp = tempfile.TemporaryDirectory()
        self.policy = CheckpointPolicyV95(None)
        self.policy.ledger = V95Ledger(os.path.join(self.tmp.name, "led.sqlite3"))
        self.policy.market_data = FakeHub()

    def tearDown(self):
        self.tmp.cleanup()
        with cp95._LATEST_LOCK:
            cp95._LATEST_ANALYSES.clear()
            cp95._LATEST_RANKING.clear()
            cp95._LATEST_LEDGER.clear()
            cp95._LATEST_CHECKPOINT = "UNKNOWN"

    def test_snapshot_carries_prediction_card_fields(self):
        now = time.time()
        snaps = {"BNB": self._snapshot(asset="BNB", checkpoint="10M", ask=52.0, target=100.0, spot=101.0)}
        s = snaps["BNB"]
        s["seconds_remaining"] = 560
        s["underlying_candles_5s"] = self._candles()[-12:]
        s["close_time"] = now + 560

        class FM:
            def update(self, snaps, now, wsh): return snaps

        class CE:
            def enrich_all(self, snaps, now, wsh): return snaps

        out = self.policy.run_cycle(dict(snaps), now, {}, FM(), CE(), self._FakeNotifier())
        snap = out["BNB"]
        self.assertEqual(snap["q15_v9_5_interval"], "10M")
        self.assertIn(snap["q15_v9_5_confidence_grade"], ("A", "B", "C", "D"))
        self.assertIn(snap["q15_v9_5_stability"], ("stable", "strengthening", "weakening", "changed"))
        self.assertFalse(snap["q15_v9_5_expired"])  # 560s left, well inside the 10M interval
        self.assertIsNotNone(snap["q15_v9_5_prediction_timestamp"])
        # Yes/No probabilities are present and total ~100%.
        y, n = snap["q15_v9_5_yes_probability"], snap["q15_v9_5_no_probability"]
        self.assertAlmostEqual(y + n, 1.0, places=6)


if __name__ == "__main__":
    unittest.main()
