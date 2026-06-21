"""End-to-end resilience of the ~1s refresh loop.

The loop wires ~15 subsystems together; the review flagged that none of them
were exercised through the real loop. These drive `refresh_loop` for a bounded
number of cycles (via the test-only `max_cycles` seam) and assert:
  * a clean cycle publishes a state entry for every asset and marks _last_cycle_ok;
  * an exception inside a per-stage `ct.safe(...)` subsystem is isolated and the
    cycle still completes;
  * an exception in discovery is isolated per-asset and never kills the loop.

No network is used: discovery is stubbed to "no market" so the loop runs its
no-market path through every enrichment stage.
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


class TestRefreshLoopResilience(unittest.TestCase):
    def setUp(self):
        self._orig_discover = appmod.discover_single
        self._orig_interval = appmod.REFRESH_INTERVAL
        appmod.REFRESH_INTERVAL = 0.0  # keep the bounded loop fast
        appmod.discover_single = lambda asset: None  # no live markets, no network
        with appmod.state_lock:
            appmod.state.clear()
        appmod._last_cycle_ok = None

    def tearDown(self):
        appmod.discover_single = self._orig_discover
        appmod.REFRESH_INTERVAL = self._orig_interval

    def test_clean_cycle_publishes_every_asset(self):
        appmod.refresh_loop(max_cycles=2)
        self.assertIsNotNone(appmod._last_cycle_ok, "a clean cycle must mark _last_cycle_ok")
        with appmod.state_lock:
            published = set(appmod.state.keys())
        self.assertEqual(published, set(appmod.ASSETS))

    def test_failing_subsystem_does_not_abort_the_cycle(self):
        # scalp_engine.evaluate runs under ct.safe(...) — a raise there must be
        # swallowed so the rest of the cycle (and _last_cycle_ok) still completes.
        orig = appmod.scalp_engine.evaluate

        def boom(*a, **k):
            raise RuntimeError("scalp blew up")

        appmod.scalp_engine.evaluate = boom
        try:
            appmod.refresh_loop(max_cycles=1)
        finally:
            appmod.scalp_engine.evaluate = orig
        self.assertIsNotNone(appmod._last_cycle_ok, "ct.safe must isolate a failing subsystem")

    def test_discovery_exception_is_isolated(self):
        def boom(asset):
            raise RuntimeError("discovery down")

        appmod.discover_single = boom
        # Must not raise out of the loop despite every discovery call failing.
        appmod.refresh_loop(max_cycles=1)
        with appmod.state_lock:
            published = set(appmod.state.keys())
        self.assertEqual(published, set(appmod.ASSETS))


if __name__ == "__main__":
    unittest.main()
