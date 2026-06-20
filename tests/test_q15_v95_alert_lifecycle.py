"""Alert lifecycle: one alert per material verdict, expiry, and stability trend.

Covers item-1 behavior:
  * the notification key embeds the *material* state (best coin / side / grade),
    so minor jitter reuses the key (deduped) but a direction or confidence-band
    change mints a new key (a replacement alert);
  * each interval's alert auto-expires before the next mark — a 7M alert does not
    stay live until market close;
  * predictions are tagged stable / strengthening / weakening / changed.
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.checkpoint_v95 import (
    CheckpointPolicyV95,
    _checkpoint_expired,
    _checkpoint_expiry_seconds,
    _material_token,
    _notification_identity,
)

NOW = 1_700_000_000.0  # fixed so now//900 is constant within a window


def _an(side="YES", grade="A", prob=0.70, edge=5.0, entry=False, available=True):
    return {"BTC": {
        "prediction_available": available, "prediction_side": side,
        "confidence_grade": grade, "selected_probability": prob,
        "net_edge_cents": edge, "entry_allowed": entry,
    }}


def _rank():
    return [{"asset": "BTC", "ticker": "T-BTC"}]


class TestMaterialKey(unittest.TestCase):
    def test_same_material_state_reuses_key(self):
        k1, _, _ = _notification_identity("10M", _an(prob=0.70), _rank(), NOW)
        # Minor probability jitter within the same side+grade -> same key (deduped).
        k2, _, _ = _notification_identity("10M", _an(prob=0.73), _rank(), NOW + 2)
        self.assertEqual(k1, k2)

    def test_direction_change_mints_new_key(self):
        k_yes, _, _ = _notification_identity("10M", _an(side="YES"), _rank(), NOW)
        k_no, _, _ = _notification_identity("10M", _an(side="NO"), _rank(), NOW + 2)
        self.assertNotEqual(k_yes, k_no)

    def test_grade_change_mints_new_key(self):
        k_a, _, _ = _notification_identity("10M", _an(grade="A"), _rank(), NOW)
        k_c, _, _ = _notification_identity("10M", _an(grade="C"), _rank(), NOW + 2)
        self.assertNotEqual(k_a, k_c)

    def test_token_encodes_best_pick(self):
        self.assertEqual(_material_token("10M", _an(side="NO", grade="B"), _rank()), "BTC:NO:B")


class TestExpiry(unittest.TestCase):
    def test_seven_minute_expires_before_close(self):
        # Default 7M cutoff is 120s; it must NOT linger to market close (0s).
        self.assertFalse(_checkpoint_expired("7M", 200.0))
        self.assertTrue(_checkpoint_expired("7M", 90.0))
        self.assertTrue(_checkpoint_expired("7M", 10.0))

    def test_each_interval_expires_at_next_mark(self):
        self.assertEqual(_checkpoint_expiry_seconds("15M"), 600.0)
        self.assertEqual(_checkpoint_expiry_seconds("10M"), 420.0)
        self.assertFalse(_checkpoint_expired("15M", 700.0))
        self.assertTrue(_checkpoint_expired("15M", 550.0))   # 10M mark passed

    def test_seven_minute_cutoff_is_configurable(self):
        os.environ["Q15_V95_7M_EXPIRY_SECONDS"] = "180"
        try:
            self.assertTrue(_checkpoint_expired("7M", 170.0))
            self.assertFalse(_checkpoint_expired("7M", 200.0))
        finally:
            del os.environ["Q15_V95_7M_EXPIRY_SECONDS"]

    def test_decision_settled_suppresses_after_expiry(self):
        obj = CheckpointPolicyV95.__new__(CheckpointPolicyV95)
        obj._decision_stability = {}
        os.environ["Q15_V95_DECISION_STABILITY_CYCLES"] = "1"
        try:
            snaps = {"BTC": {"seconds_remaining": 60.0}}  # past the 7M 120s cutoff
            self.assertFalse(obj._decision_settled("7M", _an(), _rank(), snaps, NOW))
        finally:
            del os.environ["Q15_V95_DECISION_STABILITY_CYCLES"]


class TestStabilityMarker(unittest.TestCase):
    def _policy(self):
        obj = CheckpointPolicyV95.__new__(CheckpointPolicyV95)
        obj._prediction_trend = {}
        return obj

    def test_trend_transitions(self):
        obj = self._policy()
        a = {"prediction_available": True, "prediction_side": "YES", "selected_probability": 0.60}
        self.assertEqual(obj._stability_marker("BTC", "10M", a, NOW), "stable")          # first
        a["selected_probability"] = 0.68
        self.assertEqual(obj._stability_marker("BTC", "10M", a, NOW + 1), "strengthening")
        a["selected_probability"] = 0.55
        self.assertEqual(obj._stability_marker("BTC", "10M", a, NOW + 2), "weakening")
        a["prediction_side"] = "NO"
        self.assertEqual(obj._stability_marker("BTC", "10M", a, NOW + 3), "changed")

    def test_unavailable_is_none(self):
        obj = self._policy()
        self.assertIsNone(obj._stability_marker("BTC", "10M", {"prediction_available": False}, NOW))


if __name__ == "__main__":
    unittest.main()
