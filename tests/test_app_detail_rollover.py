"""Rollover-safety guards in the refresh loop.

The review flagged that a market rollover could serve the *prior* market's
volume (or reset an engine to a stale ticker) if a request or a cached detail
landed against the wrong ticker. These lock the two pure guards the loop uses:

  * `_fetch_result_is_current` — drop a late fetch whose ticker has rolled over.
  * `_resolve_cached_detail`   — only reuse last-good volume for the SAME ticker.

No network, no full loop: the guards are pure and deterministic.
"""
from __future__ import annotations

import os
import sys
import unittest

os.environ.setdefault("Q15_AUTOSTART_REFRESH", "0")  # never spawn the live loop

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

if __import__("importlib").util.find_spec("flask") is None:  # pragma: no cover
    raise unittest.SkipTest("flask not installed")

import app as appmod  # noqa: E402


class TestFetchResultIsCurrent(unittest.TestCase):
    def test_matching_ticker_is_current(self):
        active = {"BTC": {"ticker": "T1"}}
        self.assertTrue(appmod._fetch_result_is_current(active, "BTC", {"ticker": "T1"}))

    def test_rolled_over_ticker_is_dropped(self):
        active = {"BTC": {"ticker": "T2"}}  # market already rolled to T2
        self.assertFalse(appmod._fetch_result_is_current(active, "BTC", {"ticker": "T1"}))

    def test_asset_no_longer_active_is_dropped(self):
        self.assertFalse(appmod._fetch_result_is_current({}, "BTC", {"ticker": "T1"}))


class TestResolveCachedDetail(unittest.TestCase):
    def test_same_ticker_reuses_detail(self):
        cache = {"BTC": ("T1", {"volume": 111})}
        self.assertEqual(appmod._resolve_cached_detail(cache, "BTC", "T1"), {"volume": 111})

    def test_rolled_over_ticker_does_not_reuse_prior_volume(self):
        # The crux of the review concern: after rollover to T2, the T1 volume must
        # NOT be served for the new market.
        cache = {"BTC": ("T1", {"volume": 111})}
        self.assertIsNone(appmod._resolve_cached_detail(cache, "BTC", "T2"))

    def test_no_cache_returns_none(self):
        self.assertIsNone(appmod._resolve_cached_detail({}, "BTC", "T1"))


if __name__ == "__main__":
    unittest.main()
