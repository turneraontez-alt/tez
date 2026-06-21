"""Per-asset circuit breaker for the public market-data hub.

After repeated all-source failures the hub should stop scheduling fetches for
that asset for a cooldown window (retry less during an outage) and recover on
the next success. Drives the breaker via _complete()/schedule() with no network.
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from concurrent.futures import Future

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.market_data_v95 import PublicMarketDataHub

ASSET = "BTC"


def _future(result: dict) -> Future:
    f: Future = Future()
    f.set_result(result)
    return f


def _fail(asset: str = ASSET, at: float | None = None) -> dict:
    return {"asset": asset, "available": False, "fetched_at": at or time.time(),
            "sources": {}, "source_errors": {"coinbase": "network:down"}}


def _ok(asset: str = ASSET, at: float | None = None) -> dict:
    return {"asset": asset, "available": True, "fetched_at": at or time.time(),
            "composite_price": 100.0, "sources": {"coinbase": {}}}


class TestPublicBreaker(unittest.TestCase):
    def _hub(self, threshold=3, cooldown=30.0):
        os.environ["Q15_V95_PUBLIC_BREAKER_THRESHOLD"] = str(threshold)
        os.environ["Q15_V95_PUBLIC_BREAKER_COOLDOWN_SECONDS"] = str(cooldown)
        # Pin the feed enabled so a leaked Q15_V95_PUBLIC_DATA_ENABLED=false from
        # another test can't make schedule() a no-op (it would mask the breaker).
        prior_enabled = os.environ.get("Q15_V95_PUBLIC_DATA_ENABLED")
        os.environ["Q15_V95_PUBLIC_DATA_ENABLED"] = "true"
        try:
            return PublicMarketDataHub()
        finally:
            os.environ.pop("Q15_V95_PUBLIC_BREAKER_THRESHOLD", None)
            os.environ.pop("Q15_V95_PUBLIC_BREAKER_COOLDOWN_SECONDS", None)
            if prior_enabled is None:
                os.environ.pop("Q15_V95_PUBLIC_DATA_ENABLED", None)
            else:
                os.environ["Q15_V95_PUBLIC_DATA_ENABLED"] = prior_enabled

    def test_opens_after_threshold_consecutive_failures(self):
        hub = self._hub(threshold=3, cooldown=30.0)
        now = time.time()
        for _ in range(2):
            hub._complete(ASSET, _future(_fail(at=now)))
        self.assertNotIn(ASSET, hub.health()["breaker_open_assets"])  # not yet
        hub._complete(ASSET, _future(_fail(at=now)))  # third failure trips it
        self.assertIn(ASSET, hub.health()["breaker_open_assets"])

    def test_open_circuit_suppresses_scheduling(self):
        hub = self._hub(threshold=1, cooldown=60.0)
        hub._complete(ASSET, _future(_fail()))  # trips immediately
        before = hub._requests
        hub.schedule([ASSET], now=time.time())
        self.assertEqual(hub._requests, before)  # no new fetch submitted
        self.assertNotIn(ASSET, hub._inflight)

    def test_success_resets_breaker(self):
        hub = self._hub(threshold=1, cooldown=60.0)
        hub._complete(ASSET, _future(_fail()))
        self.assertIn(ASSET, hub.health()["breaker_open_assets"])
        hub._complete(ASSET, _future(_ok()))
        self.assertNotIn(ASSET, hub.health()["breaker_open_assets"])
        self.assertEqual(hub._consecutive_failures[ASSET], 0)

    def test_threshold_zero_disables_breaker(self):
        hub = self._hub(threshold=0, cooldown=60.0)
        for _ in range(10):
            hub._complete(ASSET, _future(_fail()))
        self.assertEqual(hub.health()["breaker_open_assets"], [])

    def test_breaker_recovers_after_cooldown_expires(self):
        # Time-based reset (not just reset-on-success): once the cooldown window
        # elapses, scheduling must resume even with no intervening success.
        hub = self._hub(threshold=1, cooldown=30.0)
        hub._fetch_asset = lambda asset, now: _ok(asset, at=now)  # no network on resume
        t0 = time.time()
        hub._complete(ASSET, _future(_fail(at=t0)))  # trips the breaker, open until t0+30
        self.assertIn(ASSET, hub.health()["breaker_open_assets"])

        before = hub._requests
        hub.schedule([ASSET], now=t0 + 1.0)        # still inside cooldown
        self.assertEqual(hub._requests, before, "suppressed while circuit open")

        # Cooldown elapsed: scheduling resumes (a request is submitted) even though
        # no success intervened — this is the time-based reset.
        hub.schedule([ASSET], now=t0 + 31.0)
        self.assertEqual(hub._requests, before + 1, "scheduling resumes after cooldown")


if __name__ == "__main__":
    unittest.main()
