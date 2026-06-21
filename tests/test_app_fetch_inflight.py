"""Per-asset in-flight fetch contract for the refresh loop.

The review flagged that the loop's concurrency (at-most-one request per asset, a
slow upstream deferred past the deadline and harvested later, a failed future
isolated) was driven only on the happy path. These exercise `_harvest_and_submit`
directly with a real executor and a controllable slow worker — no network, fully
deterministic via an Event.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("Q15_AUTOSTART_REFRESH", "0")

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

if __import__("importlib").util.find_spec("flask") is None:  # pragma: no cover
    raise unittest.SkipTest("flask not installed")

import app as appmod  # noqa: E402


class TestHarvestAndSubmit(unittest.TestCase):
    def setUp(self):
        self.executor = ThreadPoolExecutor(max_workers=4)

    def tearDown(self):
        self.executor.shutdown(wait=False)

    def test_fast_assets_return_results(self):
        active = {"BTC": {"ticker": "B"}, "ETH": {"ticker": "E"}}
        inflight = {}

        def submit(asset, market, when):
            return {"asset": asset, "ticker": market["ticker"]}

        results = appmod._harvest_and_submit(self.executor, inflight, active, 0.0, 1.0, submit)
        self.assertEqual(set(results), {"BTC", "ETH"})
        self.assertEqual(inflight, {})  # everything completed, nothing left in flight

    def test_slow_asset_is_deferred_then_harvested_next_cycle(self):
        active = {"BTC": {"ticker": "B"}, "ETH": {"ticker": "E"}}
        inflight = {}
        release = threading.Event()

        def submit(asset, market, when):
            if asset == "ETH":
                release.wait(5)  # block past the deadline on the first cycle
            return {"asset": asset, "ticker": market["ticker"]}

        # Cycle 1: a tiny deadline — BTC completes, ETH is still running and stays
        # in flight (not crashed, not blocking BTC).
        results = appmod._harvest_and_submit(self.executor, inflight, active, 0.0, 0.05, submit)
        self.assertIn("BTC", results)
        self.assertNotIn("ETH", results, "slow asset must not block the cycle")
        self.assertIn("ETH", inflight, "slow asset stays in flight for a later cycle")
        self.assertNotIn("BTC", inflight)

        # Let ETH finish; cycle 2 harvests it without resubmitting BTC mid-flight.
        release.set()
        time.sleep(0.05)
        results2 = appmod._harvest_and_submit(self.executor, inflight, active, 1.0, 1.0, submit)
        self.assertIn("ETH", results2, "deferred slow asset recovers next cycle")
        self.assertEqual(inflight, {})

    def test_no_duplicate_request_while_in_flight(self):
        active = {"BTC": {"ticker": "B"}}
        inflight = {}
        calls = []
        release = threading.Event()

        def submit(asset, market, when):
            calls.append(asset)
            release.wait(5)
            return {"asset": asset, "ticker": market["ticker"]}

        appmod._harvest_and_submit(self.executor, inflight, active, 0.0, 0.05, submit)
        self.assertIn("BTC", inflight)
        # Second cycle while BTC is still running must NOT submit a duplicate.
        appmod._harvest_and_submit(self.executor, inflight, active, 1.0, 0.05, submit)
        self.assertEqual(calls, ["BTC"], "at most one outstanding request per asset")
        release.set()
        time.sleep(0.05)

    def test_failed_future_is_isolated(self):
        active = {"BTC": {"ticker": "B"}, "ETH": {"ticker": "E"}}
        inflight = {}

        def submit(asset, market, when):
            if asset == "BTC":
                raise RuntimeError("upstream 500")
            return {"asset": asset, "ticker": market["ticker"]}

        results = appmod._harvest_and_submit(self.executor, inflight, active, 0.0, 1.0, submit)
        self.assertNotIn("BTC", results, "a raising fetch is dropped, not surfaced")
        self.assertIn("ETH", results, "a sibling failure must not lose the good result")
        self.assertEqual(inflight, {})

    def test_hung_request_is_abandoned_past_ttl(self):
        # A permanently hung upstream must not accumulate in-flight entries forever:
        # past the TTL it is dropped from tracking so a fresh request can replace it.
        active = {"BTC": {"ticker": "B"}}
        inflight = {}
        release = threading.Event()

        def submit(asset, market, when):
            release.wait(10)  # hang until released
            return {"asset": asset, "ticker": market["ticker"]}

        # Cycle 1 at t=0: submit, doesn't finish within the tiny deadline.
        appmod._harvest_and_submit(self.executor, inflight, active, 0.0, 0.02, submit, stale_ttl=5.0)
        self.assertIn("BTC", inflight)
        self.assertEqual(inflight["BTC"][1], 0.0, "submit time tracked alongside the future")

        # Cycle 2 at t=10 (> ttl): the hung request is abandoned and re-submitted.
        appmod._harvest_and_submit(self.executor, inflight, active, 10.0, 0.02, submit, stale_ttl=5.0)
        self.assertIn("BTC", inflight, "a fresh request takes the abandoned slot")
        self.assertEqual(inflight["BTC"][1], 10.0, "in-flight entry was replaced, not duplicated")
        release.set()
        time.sleep(0.05)


if __name__ == "__main__":
    unittest.main()
