"""/api/health computes data_age from a state-lock-consistent timestamp map.

The review flagged that the health route read engines[a].last_update_ts while
holding state_lock, even though the refresh loop mutates the engine outside that
lock — a (benign, GIL-atomic) inconsistency. data_age is now derived from
`engine_update_ts`, written under state_lock alongside the published snapshot, so
the route reads a value consistent with the state it just snapshotted.
"""
from __future__ import annotations

import os
import sys
import time
import unittest

os.environ.setdefault("Q15_AUTOSTART_REFRESH", "0")

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

if __import__("importlib").util.find_spec("flask") is None:  # pragma: no cover
    raise unittest.SkipTest("flask not installed")

import app as appmod  # noqa: E402


class TestHealthDataAge(unittest.TestCase):
    def setUp(self):
        self.client = appmod.app.test_client()
        self.asset = appmod.ASSETS[0]
        with appmod.state_lock:
            self._saved_state = dict(appmod.state)
            self._saved_ts = dict(appmod.engine_update_ts)
            appmod.state.clear()
            appmod.engine_update_ts.clear()

    def tearDown(self):
        with appmod.state_lock:
            appmod.state.clear()
            appmod.state.update(self._saved_state)
            appmod.engine_update_ts.clear()
            appmod.engine_update_ts.update(self._saved_ts)

    def test_data_age_comes_from_engine_update_ts(self):
        with appmod.state_lock:
            appmod.state[self.asset] = {"asset": self.asset, "market_state": "live"}
            appmod.engine_update_ts[self.asset] = time.time() - 2.0
        body = self.client.get("/api/health").get_json()
        self.assertIsNotNone(body["data_age_seconds"])
        # ~2s old, allow generous slack for test scheduling.
        self.assertGreaterEqual(body["data_age_seconds"], 1.0)
        self.assertLess(body["data_age_seconds"], 30.0)

    def test_live_asset_without_a_timestamp_yields_none(self):
        # A live snapshot but no engine_update_ts entry must read as None (the
        # route reads the map, not an unsynchronized engines[a] attribute).
        with appmod.state_lock:
            appmod.state[self.asset] = {"asset": self.asset, "market_state": "live"}
            appmod.engine_update_ts.pop(self.asset, None)
        body = self.client.get("/api/health").get_json()
        self.assertIsNone(body["data_age_seconds"])


if __name__ == "__main__":
    unittest.main()
