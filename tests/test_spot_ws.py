from __future__ import annotations

import json
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

import spot_ws
import spot_client


class TestSpotWsFeed(unittest.TestCase):
    """Parsing + freshness logic, exercised without any live network."""

    def test_coinbase_handler_parses_ticker(self):
        feed = spot_ws.SpotWebSocketFeed()
        feed._handle_coinbase(json.dumps({
            "type": "ticker", "product_id": "BTC-USD",
            "price": "50000.1", "best_bid": "50000.0", "best_ask": "50000.2",
        }))
        tick = feed.get_tick("BTC", max_age=5)
        self.assertIsNotNone(tick)
        self.assertEqual(tick["price"], 50000.1)
        self.assertEqual(tick["bid"], 50000.0)
        self.assertEqual(tick["ask"], 50000.2)
        self.assertIn("Coinbase WS", tick["source"])

    def test_okx_handler_parses_tickers(self):
        feed = spot_ws.SpotWebSocketFeed()
        feed._handle_okx(json.dumps({
            "arg": {"channel": "tickers", "instId": "BNB-USDT"},
            "data": [{"last": "600.5", "bidPx": "600.4", "askPx": "600.6"}],
        }))
        tick = feed.get_tick("BNB", max_age=5)
        self.assertIsNotNone(tick)
        self.assertEqual(tick["price"], 600.5)

    def test_get_tick_drops_stale(self):
        feed = spot_ws.SpotWebSocketFeed()
        feed._ticks["ETH"] = {"price": 2000.0, "bid": None, "ask": None, "ts": time.time() - 10, "source": "x"}
        self.assertIsNone(feed.get_tick("ETH", max_age=3))
        self.assertIsNotNone(feed.get_tick("ETH", max_age=30))

    def test_garbage_message_is_ignored(self):
        feed = spot_ws.SpotWebSocketFeed()
        feed._handle_coinbase("not json")
        feed._handle_coinbase(json.dumps({"type": "ticker", "product_id": "BTC-USD", "price": "0"}))
        self.assertIsNone(feed.get_tick("BTC", max_age=99))


class TestGetWsSpot(unittest.TestCase):
    def setUp(self):
        spot_ws._feed = None
        for k in ("Q15_SPOT_WS_ENABLED", "Q15_SPOT_WS_MAX_AGE_SECONDS"):
            os.environ.pop(k, None)

    def tearDown(self):
        spot_ws._feed = None
        for k in ("Q15_SPOT_WS_ENABLED", "Q15_SPOT_WS_MAX_AGE_SECONDS"):
            os.environ.pop(k, None)

    def _seed_feed(self, asset, price, age=0.0):
        feed = spot_ws.SpotWebSocketFeed()
        feed._ticks[asset] = {"price": price, "bid": None, "ask": None,
                              "ts": time.time() - age, "source": f"Coinbase WS {asset}"}
        spot_ws._feed = feed  # pre-set so get_feed() won't spawn a thread
        return feed

    def test_disabled_by_default(self):
        self._seed_feed("BTC", 100.0)
        self.assertIsNone(spot_ws.get_ws_spot("BTC"))

    def test_returns_fresh_tick_when_enabled(self):
        os.environ["Q15_SPOT_WS_ENABLED"] = "true"
        self._seed_feed("BTC", 12345.6)
        out = spot_ws.get_ws_spot("BTC")
        self.assertIsNotNone(out)
        self.assertTrue(out["ok"])
        self.assertTrue(out["ws"])
        self.assertEqual(out["price"], 12345.6)
        self.assertEqual(out["quote"], "USD")

    def test_stale_tick_returns_none(self):
        os.environ["Q15_SPOT_WS_ENABLED"] = "true"
        os.environ["Q15_SPOT_WS_MAX_AGE_SECONDS"] = "3"
        self._seed_feed("ETH", 2000.0, age=10.0)
        self.assertIsNone(spot_ws.get_ws_spot("ETH"))


class TestGetSpotIntegration(unittest.TestCase):
    """spot_client.get_spot prefers WS when enabled, else unchanged REST path."""

    def setUp(self):
        spot_ws._feed = None
        spot_client._LAST_GOOD.clear()
        self._orig_fetch = spot_client._fetch_spot
        for k in ("Q15_SPOT_WS_ENABLED", "Q15_SPOT_WS_MAX_AGE_SECONDS"):
            os.environ.pop(k, None)

    def tearDown(self):
        spot_client._fetch_spot = self._orig_fetch
        spot_client._LAST_GOOD.clear()
        spot_ws._feed = None
        for k in ("Q15_SPOT_WS_ENABLED", "Q15_SPOT_WS_MAX_AGE_SECONDS"):
            os.environ.pop(k, None)

    def test_prefers_ws_and_skips_rest(self):
        os.environ["Q15_SPOT_WS_ENABLED"] = "true"
        feed = spot_ws.SpotWebSocketFeed()
        feed._ticks["BTC"] = {"price": 99.0, "bid": None, "ask": None, "ts": time.time(), "source": "Coinbase WS BTC-USD"}
        spot_ws._feed = feed
        # REST must NOT be consulted when a fresh WS tick exists.
        spot_client._fetch_spot = lambda a: self.fail("REST should not be called when WS tick is fresh")
        out = spot_client.get_spot("BTC")
        self.assertTrue(out["ok"])
        self.assertEqual(out["price"], 99.0)
        self.assertTrue(out.get("ws"))
        self.assertIn("BTC", spot_client._LAST_GOOD)

    def test_falls_back_to_rest_when_ws_disabled(self):
        # Flag off: behaves exactly like before, hitting the REST path.
        spot_client._fetch_spot = lambda a: {"ok": True, "price": 250.0, "ts": time.time(), "source": "CB"}
        out = spot_client.get_spot("BTC")
        self.assertTrue(out["ok"])
        self.assertEqual(out["price"], 250.0)
        self.assertNotIn("ws", out)


if __name__ == "__main__":
    unittest.main()
