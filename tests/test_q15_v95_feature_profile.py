"""Default-OFF per-stage profiler for analyse_v95 (read-only instrumentation).

Locks two properties: (1) when off, `_timed` is a transparent passthrough that
records nothing and changes no result; (2) when on, it records per-stage wall
time and `feature_profile_health` ranks stages by total time.
"""
import os
import unittest

from q15_upgrade import checkpoint_v95
from q15_upgrade.checkpoint_v95 import (
    _timed, feature_profile_health, feature_profile_reset, _feature_profile_enabled,
)


class FeatureProfileTest(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("Q15_V95_PROFILE_FEATURES", None)
        feature_profile_reset()

    def tearDown(self):
        feature_profile_reset()
        if self._saved is not None:
            os.environ["Q15_V95_PROFILE_FEATURES"] = self._saved
        else:
            os.environ.pop("Q15_V95_PROFILE_FEATURES", None)

    def test_default_is_off(self):
        self.assertFalse(_feature_profile_enabled())

    def test_off_is_transparent_and_records_nothing(self):
        result = _timed(False, "momentum", lambda a, b: a + b, 2, 3)
        self.assertEqual(result, 5)
        self.assertEqual(feature_profile_health()["stages"], {})

    def test_on_records_calls_and_returns_result(self):
        result = _timed(True, "flow", lambda x: x * 2, 21)
        self.assertEqual(result, 42)
        _timed(True, "flow", lambda: None)
        health = feature_profile_health()
        self.assertEqual(health["stages"]["flow"]["calls"], 2)
        self.assertIn("avg_ms", health["stages"]["flow"])

    def test_ranked_by_total_time(self):
        with checkpoint_v95._FEATURE_PROFILE_LOCK:
            checkpoint_v95._FEATURE_PROFILE["fast"] = {"calls": 1.0, "total_s": 0.001}
            checkpoint_v95._FEATURE_PROFILE["slow"] = {"calls": 1.0, "total_s": 0.01}
        names = list(feature_profile_health()["stages"].keys())
        self.assertEqual(names[0], "slow")  # most total time first

    def test_passthrough_propagates_exceptions(self):
        def boom():
            raise ValueError("x")
        with self.assertRaises(ValueError):
            _timed(True, "boom", boom)
        # the failed call is still timed/recorded
        self.assertIn("boom", feature_profile_health()["stages"])


if __name__ == "__main__":
    unittest.main()
