from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.hybrid_data import HybridMarketData


class FakeWs:
    def __init__(self, connected=True):
        self._connected = connected

    def is_connected(self):
        return self._connected

    def subscribe(self, tickers):
        pass

    def health(self):
        return {"connected": self._connected, "mode": "websocket"}

    def book_ages(self):
        return {"TICK-A": 0.3, "TICK-B": 1.2}

    def get_orderbook(self, ticker):
        return None

    def get_trades(self, ticker, min_ts=None):
        return []


class FakeRest:
    def get_orderbook(self, ticker):
        return {"yes_dollars": [], "no_dollars": []}

    def get_trades(self, ticker, min_ts=None):
        return []


class TestHybridBookAges(unittest.TestCase):
    def test_health_surfaces_per_ticker_book_ages(self):
        hd = HybridMarketData(FakeRest(), FakeWs())
        h = hd.health()
        self.assertIn("book_ages", h)
        self.assertEqual(h["book_ages"]["TICK-A"], 0.3)
        self.assertIn("ticker_sources", h)

    def test_health_without_ws_omits_book_ages(self):
        hd = HybridMarketData(FakeRest(), None)
        h = hd.health()
        self.assertEqual(h["mode"], "rest-fallback")
        self.assertNotIn("book_ages", h)


if __name__ == "__main__":
    unittest.main()
