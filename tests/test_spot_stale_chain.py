"""Spot staleness propagates through the loop's fetch seam.

`test_spot_last_good` covers `get_spot` in isolation; the review flagged the
missing integration: that the loop's fetch worker (`fetch_asset_raw`) carries the
stale flag through (so downstream freshness gates can see it) and that a spot
whose last-good cache is past budget surfaces as a failure rather than a silently
served stale price. No network: the orderbook/trades feeds and the spot fetch are
stubbed.
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
import spot_client  # noqa: E402


class TestSpotStaleChain(unittest.TestCase):
    def setUp(self):
        spot_client._LAST_GOOD.clear()
        os.environ["Q15_SPOT_MAX_STALE_SECONDS"] = "30"
        self._orig_fetch = spot_client._fetch_spot
        self._orig_get_spot = appmod.get_spot
        # Use the real get_spot (fallback logic) inside fetch_asset_raw.
        appmod.get_spot = spot_client.get_spot
        self._orig_ob = appmod.market_data.get_orderbook
        self._orig_tr = appmod.market_data.get_trades
        self._orig_conn = appmod.market_data.is_connected
        appmod.market_data.get_orderbook = lambda ticker: {}
        appmod.market_data.get_trades = lambda ticker, min_ts=None: []
        appmod.market_data.is_connected = lambda: False

    def tearDown(self):
        spot_client._fetch_spot = self._orig_fetch
        spot_client._LAST_GOOD.clear()
        os.environ.pop("Q15_SPOT_MAX_STALE_SECONDS", None)
        appmod.get_spot = self._orig_get_spot
        appmod.market_data.get_orderbook = self._orig_ob
        appmod.market_data.get_trades = self._orig_tr
        appmod.market_data.is_connected = self._orig_conn

    def test_stale_fallback_flag_reaches_fetch_result(self):
        spot_client._fetch_spot = lambda a: {"ok": True, "price": 100.0, "ts": time.time(), "source": "CB"}
        spot_client.get_spot("BTC")  # seed last-good
        # Now the live fetch fails but the cache is fresh → served stale, flagged.
        spot_client._fetch_spot = lambda a: {"ok": False, "price": None, "error": "network down"}
        out = appmod.fetch_asset_raw("BTC", {"ticker": "BTC-T"}, time.time(), None)
        self.assertTrue(out["spot"]["ok"], "fresh last-good is still usable")
        self.assertTrue(out["spot"]["stale"], "stale flag must reach the loop's fetch input")
        self.assertEqual(out["spot"]["error"], "network down")

    def test_budget_exhausted_spot_surfaces_as_failure(self):
        spot_client._fetch_spot = lambda a: {"ok": True, "price": 100.0, "ts": time.time(), "source": "CB"}
        spot_client.get_spot("BTC")
        # Age the cache beyond the staleness budget.
        ts, good = spot_client._LAST_GOOD["BTC"]
        spot_client._LAST_GOOD["BTC"] = (ts - 120.0, good)
        spot_client._fetch_spot = lambda a: {"ok": False, "price": None, "error": "timeout"}
        out = appmod.fetch_asset_raw("BTC", {"ticker": "BTC-T"}, time.time(), None)
        self.assertFalse(out["spot"]["ok"], "stale-beyond-budget must report failure, not serve a stale price")


if __name__ == "__main__":
    unittest.main()
