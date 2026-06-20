"""One checkpoint alert per (checkpoint, market window), sent only once settled.

Covers the two-part fix for the "4 alerts at the 10m mark" report:
  * `_notification_identity` keys on the 15-minute market window, not the
    top-ranked ticker, so leader/edge churn cannot mint duplicate sends.
  * `_decision_settled` holds the alert until the verdict is stable for a few
    cycles (with a force-send fallback as the band closes).
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.checkpoint_v95 import (
    CheckpointPolicyV95,
    _notification_identity,
)

NOW = 1_700_000_000.0  # fixed so `now // 900` is constant within a test


def _analyses(side="YES", entry=False):
    return {"BTC": {"prediction_side": side, "entry_allowed": entry, "trade_decision": "WATCH_PRICE"}}


def _ranking(ticker="T-AAA"):
    return [{"asset": "BTC", "ticker": ticker, "prediction_side": "YES", "trade_decision": "WATCH_PRICE"}]


class TestNotificationIdentity(unittest.TestCase):
    def test_key_ignores_ticker_churn_within_a_window(self):
        k1, _, _ = _notification_identity("10M", _analyses(), _ranking("T-AAA"), NOW)
        k2, _, _ = _notification_identity("10M", _analyses(), _ranking("T-BBB"), NOW + 1.0)
        self.assertEqual(k1, k2)  # different leader, same checkpoint+window -> one key

    def test_key_differs_by_checkpoint(self):
        k_10, _, _ = _notification_identity("10M", _analyses(), _ranking(), NOW)
        k_7, _, _ = _notification_identity("7M", _analyses(), _ranking(), NOW)
        self.assertNotEqual(k_10, k_7)

    def test_key_differs_across_market_windows(self):
        k_now, _, _ = _notification_identity("10M", _analyses(), _ranking(), NOW)
        k_next, _, _ = _notification_identity("10M", _analyses(), _ranking(), NOW + 900.0)
        self.assertNotEqual(k_now, k_next)

    def test_disabling_flag_restores_ticker_key(self):
        os.environ["Q15_V95_SINGLE_ALERT_PER_CHECKPOINT"] = "false"
        try:
            k1, _, _ = _notification_identity("10M", _analyses(), _ranking("T-AAA"), NOW)
            k2, _, _ = _notification_identity("10M", _analyses(), _ranking("T-BBB"), NOW)
            self.assertNotEqual(k1, k2)  # legacy behaviour: per-ticker
        finally:
            del os.environ["Q15_V95_SINGLE_ALERT_PER_CHECKPOINT"]


class TestDecisionSettledGate(unittest.TestCase):
    def _policy(self):
        # Skip __init__ (needs a store); we only exercise the stability tracker.
        obj = CheckpointPolicyV95.__new__(CheckpointPolicyV95)
        obj._decision_stability = {}
        return obj

    def test_holds_until_stable_for_required_cycles(self):
        obj = self._policy()
        os.environ["Q15_V95_DECISION_STABILITY_CYCLES"] = "3"
        try:
            results = [
                obj._decision_settled("10M", _analyses(), _ranking(), {}, NOW)
                for _ in range(3)
            ]
            self.assertEqual(results, [False, False, True])
        finally:
            del os.environ["Q15_V95_DECISION_STABILITY_CYCLES"]

    def test_changed_verdict_resets_the_counter(self):
        obj = self._policy()
        os.environ["Q15_V95_DECISION_STABILITY_CYCLES"] = "2"
        try:
            self.assertFalse(obj._decision_settled("10M", _analyses("YES"), _ranking(), {}, NOW))
            self.assertTrue(obj._decision_settled("10M", _analyses("YES"), _ranking(), {}, NOW))
            # The verdict flips side -> back to unsettled.
            self.assertFalse(obj._decision_settled("10M", _analyses("NO"), _ranking(), {}, NOW))
        finally:
            del os.environ["Q15_V95_DECISION_STABILITY_CYCLES"]

    def test_force_emits_as_band_closes(self):
        obj = self._policy()
        os.environ["Q15_V95_DECISION_STABILITY_CYCLES"] = "99"  # never reach via stability
        try:
            # 10M band closes at 480s remaining; within the 60s force margin -> emit.
            snaps = {"BTC": {"seconds_remaining": 500.0}}
            self.assertTrue(obj._decision_settled("10M", _analyses(), _ranking(), snaps, NOW))
            # Plenty of time left and not yet stable -> still held.
            obj._decision_stability.clear()
            snaps_early = {"BTC": {"seconds_remaining": 650.0}}
            self.assertFalse(obj._decision_settled("10M", _analyses(), _ranking(), snaps_early, NOW))
        finally:
            del os.environ["Q15_V95_DECISION_STABILITY_CYCLES"]

    def test_disabled_flag_is_always_settled(self):
        obj = self._policy()
        os.environ["Q15_V95_SINGLE_ALERT_PER_CHECKPOINT"] = "false"
        try:
            self.assertTrue(obj._decision_settled("10M", _analyses(), _ranking(), {}, NOW))
        finally:
            del os.environ["Q15_V95_SINGLE_ALERT_PER_CHECKPOINT"]


if __name__ == "__main__":
    unittest.main()
