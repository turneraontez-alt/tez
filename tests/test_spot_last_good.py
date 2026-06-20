from __future__ import annotations

import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

import spot_client


class TestSpotLastGood(unittest.TestCase):
    def setUp(self):
        spot_client._LAST_GOOD.clear()
        os.environ["Q15_SPOT_MAX_STALE_SECONDS"] = "30"
        self._orig = spot_client._fetch_spot

    def tearDown(self):
        spot_client._fetch_spot = self._orig
        spot_client._LAST_GOOD.clear()
        os.environ.pop("Q15_SPOT_MAX_STALE_SECONDS", None)

    def _set_fetch(self, fn):
        spot_client._fetch_spot = fn

    def test_success_is_cached_and_returned_clean(self):
        self._set_fetch(lambda a: {"ok": True, "price": 100.0, "ts": time.time(), "source": "X"})
        out = spot_client.get_spot("BTC")
        self.assertTrue(out["ok"])
        self.assertNotIn("stale", out)
        self.assertIn("BTC", spot_client._LAST_GOOD)

    def test_failure_falls_back_to_last_good_within_budget(self):
        self._set_fetch(lambda a: {"ok": True, "price": 250.0, "ts": time.time(), "source": "OKX"})
        spot_client.get_spot("BNB")  # seed cache
        self._set_fetch(lambda a: {"ok": False, "price": None, "error": "network down"})
        out = spot_client.get_spot("BNB")
        self.assertTrue(out["ok"], "should serve last good price")
        self.assertEqual(out["price"], 250.0)
        self.assertTrue(out["stale"])
        self.assertEqual(out["error"], "network down")
        self.assertIn("last good", out["source"])

    def test_failure_returns_failure_when_cache_too_old(self):
        self._set_fetch(lambda a: {"ok": True, "price": 9.0, "ts": time.time(), "source": "CB"})
        spot_client.get_spot("ETH")
        # Age the cached entry beyond the budget.
        ts, good = spot_client._LAST_GOOD["ETH"]
        spot_client._LAST_GOOD["ETH"] = (ts - 120.0, good)
        self._set_fetch(lambda a: {"ok": False, "price": None, "error": "timeout"})
        out = spot_client.get_spot("ETH")
        self.assertFalse(out["ok"], "stale beyond budget must report failure")
        self.assertEqual(out["error"], "timeout")

    def test_failure_with_no_cache_returns_failure(self):
        self._set_fetch(lambda a: {"ok": False, "price": None, "error": "boom"})
        out = spot_client.get_spot("SOL")
        self.assertFalse(out["ok"])


if __name__ == "__main__":
    unittest.main()
