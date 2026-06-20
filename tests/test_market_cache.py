from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from market_cache import MarketResultCache


class FakeClient:
    def __init__(self, markets):
        self.markets = markets        # ticker -> market dict (or None)
        self.calls = []               # tickers fetched
        self.other_called = 0

    def get_market(self, ticker):
        self.calls.append(ticker)
        return self.markets.get(ticker)

    def get_orderbook(self, ticker):  # arbitrary non-cached method
        self.other_called += 1
        return {"ticker": ticker}


class TestMarketResultCache(unittest.TestCase):
    def test_resolved_market_fetched_once_then_served_from_cache(self):
        fc = FakeClient({"T": {"ticker": "T", "result": "YES", "status": "settled"}})
        cache = MarketResultCache(fc)
        a = cache.get_market("T")
        b = cache.get_market("T")
        c = cache.get_market("T")
        self.assertEqual(a["result"], "YES")
        self.assertIs(b, a)
        self.assertIs(c, a)
        self.assertEqual(fc.calls, ["T"])          # exactly one upstream fetch
        s = cache.stats()
        self.assertEqual(s["fetches"], 1)
        self.assertEqual(s["hits"], 2)
        self.assertEqual(s["cached_results"], 1)

    def test_unresolved_market_is_never_cached(self):
        fc = FakeClient({"T": {"ticker": "T", "result": "", "status": "active"}})
        cache = MarketResultCache(fc)
        cache.get_market("T")
        cache.get_market("T")
        self.assertEqual(fc.calls, ["T", "T"])      # keeps polling until resolved
        self.assertEqual(cache.stats()["cached_results"], 0)

    def test_closed_without_result_not_cached_until_result_posts(self):
        # A market can be closed/expired before the official result is posted.
        markets = {"T": {"ticker": "T", "result": "", "status": "closed"}}
        fc = FakeClient(markets)
        cache = MarketResultCache(fc)
        self.assertIsNone(cache._resolved_result(cache.get_market("T")))
        # result arrives later
        markets["T"] = {"ticker": "T", "result": "NO", "status": "settled"}
        self.assertEqual(cache.get_market("T")["result"], "NO")
        # now cached
        self.assertEqual(cache.get_market("T")["result"], "NO")
        self.assertEqual(fc.calls.count("T"), 2)    # 1 unresolved poll + 1 resolving; then cached

    def test_none_market_is_safe(self):
        fc = FakeClient({})
        cache = MarketResultCache(fc)
        self.assertIsNone(cache.get_market("MISSING"))
        self.assertEqual(cache.stats()["cached_results"], 0)

    def test_proxy_delegates_other_methods(self):
        fc = FakeClient({})
        cache = MarketResultCache(fc)
        self.assertEqual(cache.get_orderbook("T"), {"ticker": "T"})
        self.assertEqual(fc.other_called, 1)

    def test_eviction_bounds_memory(self):
        markets = {f"T{i}": {"result": "YES"} for i in range(10)}
        fc = FakeClient(markets)
        cache = MarketResultCache(fc, max_entries=3)
        for i in range(10):
            cache.get_market(f"T{i}")
        self.assertEqual(cache.stats()["cached_results"], 3)
        # the three most-recent remain
        self.assertEqual(fc.calls.count("T9"), 1)
        cache.get_market("T9")
        self.assertEqual(fc.calls.count("T9"), 1)   # still cached -> no new fetch


if __name__ == "__main__":
    unittest.main()
