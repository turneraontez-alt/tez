"""Degraded-path coverage for the ~1s refresh loop.

The updated review flagged that the loop's failure modes — a market expiring and
the asset entering/leaving the cycling state, a feed returning ``None`` for the
orderbook or trades, and a crash inside ``deep_evaluation_snapshots`` cascading to
the signal/scalp stages — were never exercised through the real loop. The owner
trades real money off these alerts, so these are exactly the paths that must
survive a degraded feed without crashing or freezing the dashboard.

All deterministic, no network: ``discover_single`` / ``fetch_asset_raw`` /
``fetch_market_detail`` are stubbed at the module boundary and the market-data
subscribe is a no-op.
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("Q15_AUTOSTART_REFRESH", "0")  # never spawn the live loop

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

if __import__("importlib").util.find_spec("flask") is None:  # pragma: no cover
    raise unittest.SkipTest("flask not installed")

import app as appmod  # noqa: E402


def _iso(offset_seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _live_market(ticker: str = "BTC-LIVE", close_in: float = 600.0) -> dict:
    """A live, tradeable market (floor_strike set, close in the future)."""
    return {"ticker": ticker, "floor_strike": 100000.0, "close_time": _iso(close_in)}


def _expired_market(ticker: str = "BTC-OLD") -> dict:
    return {"ticker": ticker, "floor_strike": 100000.0, "close_time": _iso(-30.0)}


def _raw_result(ticker: str, *, ob_raw, trades, now: float) -> dict:
    """A fetch_asset_raw-shaped result with controllable orderbook/trades."""
    return {
        "asset": "BTC",
        "ticker": ticker,
        "ob_raw": ob_raw,
        "trades": trades,
        "spot": {"ok": True, "price": 100000.0, "bid": 99999.0, "ask": 100001.0,
                 "ts": now, "source": "test"},
        "source": "rest",
        "fetch_started_at": now,
        "fetch_completed_at": now,
        "fetch_latency_seconds": 0.0,
        "latest_trade_event_ts": None,
        "book_event_ts": now,
    }


class _LoopHarness(unittest.TestCase):
    def setUp(self):
        self._orig = {
            "discover": appmod.discover_single,
            "interval": appmod.REFRESH_INTERVAL,
            "deadline": appmod.FETCH_DEADLINE,
            "fetch_raw": appmod.fetch_asset_raw,
            "fetch_detail": appmod.fetch_market_detail,
            "subscribe": appmod.market_data.subscribe,
        }
        appmod.REFRESH_INTERVAL = 0.0
        appmod.FETCH_DEADLINE = 2.0
        appmod.fetch_market_detail = lambda ticker: {}
        appmod.market_data.subscribe = lambda *a, **k: None
        with appmod.state_lock:
            appmod.state.clear()
        appmod._last_cycle_ok = None

    def tearDown(self):
        appmod.discover_single = self._orig["discover"]
        appmod.REFRESH_INTERVAL = self._orig["interval"]
        appmod.FETCH_DEADLINE = self._orig["deadline"]
        appmod.fetch_asset_raw = self._orig["fetch_raw"]
        appmod.fetch_market_detail = self._orig["fetch_detail"]
        appmod.market_data.subscribe = self._orig["subscribe"]


class TestMarketExpiryCycling(_LoopHarness):
    def test_market_has_expired_pure(self):
        self.assertTrue(appmod.market_has_expired(_expired_market()))
        self.assertFalse(appmod.market_has_expired(_live_market()))
        # Missing / unparseable close_time is treated as not-expired (fail-open
        # on the timestamp, not on the trade) rather than raising.
        self.assertFalse(appmod.market_has_expired({"close_time": ""}))
        self.assertFalse(appmod.market_has_expired({"close_time": "not-a-date"}))

    def test_expired_with_no_replacement_enters_cycling(self):
        # Every discover returns an expired market then nothing — so each asset
        # expires and finds no next market, and must surface as 'cycling'.
        calls = {a: 0 for a in appmod.ASSETS}

        def discover(asset):
            calls[asset] += 1
            return _expired_market(f"{asset}-OLD") if calls[asset] == 1 else None

        appmod.discover_single = discover
        appmod.fetch_asset_raw = lambda *a, **k: None  # never reached (nothing active)
        appmod.refresh_loop(max_cycles=1)
        with appmod.state_lock:
            published = {a: appmod.state.get(a, {}) for a in appmod.ASSETS}
        self.assertIsNotNone(appmod._last_cycle_ok)
        for a in appmod.ASSETS:
            self.assertEqual(published[a].get("market_state"), "cycling",
                             f"{a} should be cycling after expiry with no replacement")

    def test_cycling_recovers_when_a_fresh_market_appears(self):
        # Expired on the first discovery, fresh thereafter: the asset must leave
        # the cycling state and re-enter the live path the same cycle.
        calls = {a: 0 for a in appmod.ASSETS}

        def discover(asset):
            calls[asset] += 1
            return _expired_market(f"{asset}-OLD") if calls[asset] == 1 else _live_market(f"{asset}-NEW")

        appmod.discover_single = discover
        appmod.fetch_asset_raw = lambda asset, market, when, last_ts: _raw_result(
            market["ticker"], ob_raw={"yes": [[40, 10]], "no": [[55, 10]]},
            trades=[], now=time.time(),
        )
        appmod.refresh_loop(max_cycles=2)
        with appmod.state_lock:
            published = {a: appmod.state.get(a, {}) for a in appmod.ASSETS}
        self.assertIsNotNone(appmod._last_cycle_ok)
        for a in appmod.ASSETS:
            self.assertNotEqual(published[a].get("market_state"), "cycling",
                                f"{a} should have recovered from cycling")
            self.assertNotEqual(published[a].get("market_state"), "none")


class TestNoneFeedIngest(_LoopHarness):
    def _drive_with(self, *, ob_raw, trades):
        appmod.discover_single = lambda asset: _live_market(f"{asset}-LIVE")
        appmod.fetch_asset_raw = lambda asset, market, when, last_ts: _raw_result(
            market["ticker"], ob_raw=ob_raw, trades=trades, now=time.time(),
        )
        # Two cycles: the first submits the fetch, the second guarantees ingest
        # even if the executor result lands a cycle late.
        appmod.refresh_loop(max_cycles=2)

    def test_none_orderbook_does_not_crash_loop(self):
        self._drive_with(ob_raw=None, trades=[])
        self.assertIsNotNone(appmod._last_cycle_ok, "None orderbook must not abort the cycle")
        with appmod.state_lock:
            published = set(appmod.state.keys())
        self.assertEqual(published, set(appmod.ASSETS))

    def test_none_trades_does_not_crash_loop(self):
        self._drive_with(ob_raw={"yes": [[40, 10]], "no": [[55, 10]]}, trades=None)
        self.assertIsNotNone(appmod._last_cycle_ok, "None trades must not abort the cycle")
        with appmod.state_lock:
            published = set(appmod.state.keys())
        self.assertEqual(published, set(appmod.ASSETS))

    def test_both_feeds_none_still_publishes(self):
        self._drive_with(ob_raw=None, trades=None)
        self.assertIsNotNone(appmod._last_cycle_ok)
        with appmod.state_lock:
            published = set(appmod.state.keys())
        self.assertEqual(published, set(appmod.ASSETS))


class TestIngestExceptionIsolation(_LoopHarness):
    def test_one_asset_ingest_crash_does_not_starve_the_others(self):
        # The updated review flagged that the per-asset ingest loop (unlike the
        # build_snapshot loop below it) was not wrapped in try/except: a single
        # poisoned tick aborted the whole loop and dropped EVERY other asset's
        # snapshot that cycle. Poison exactly one asset's orderbook parse and
        # confirm the remaining assets still publish and the cycle completes.
        poisoned = "ETH"
        appmod.discover_single = lambda asset: _live_market(f"{asset}-LIVE")

        def fetch(asset, market, when, last_ts):
            ob = {"__POISON__": True} if asset == poisoned else {"yes": [[40, 10]], "no": [[55, 10]]}
            return _raw_result(market["ticker"], ob_raw=ob, trades=[], now=time.time())

        orig_parse = appmod.parse_orderbook

        def parse(ob_raw):
            if isinstance(ob_raw, dict) and ob_raw.get("__POISON__"):
                raise ValueError("poisoned orderbook")
            return orig_parse(ob_raw)

        appmod.fetch_asset_raw = fetch
        appmod.parse_orderbook = parse
        try:
            appmod.refresh_loop(max_cycles=2)
        finally:
            appmod.parse_orderbook = orig_parse
        self.assertIsNotNone(appmod._last_cycle_ok,
                             "one asset's ingest crash must not abort the cycle")
        with appmod.state_lock:
            published = set(appmod.state.keys())
        expected = set(appmod.ASSETS) - {poisoned}
        self.assertEqual(published, expected,
                         "every asset except the poisoned one must still publish")


class TestDeepEvalCascade(_LoopHarness):
    def test_deep_eval_crash_is_isolated_and_signals_still_run(self):
        # A crash in deep_evaluation_snapshots must (a) not abort the cycle and
        # (b) fall back to deep_snaps={} so signal/scalp still run (with empty
        # input) rather than being skipped or propagating the exception.
        appmod.discover_single = lambda asset: None  # no markets needed for this path
        orig_deep = appmod.focus_manager.deep_evaluation_snapshots
        orig_sig = appmod.signal_engine.evaluate_all
        seen = {}

        def boom(*a, **k):
            raise RuntimeError("deep eval blew up")

        def spy_signals(deep_snaps, now, ws_health):
            seen["deep_snaps"] = deep_snaps
            return orig_sig(deep_snaps, now, ws_health)

        appmod.focus_manager.deep_evaluation_snapshots = boom
        appmod.signal_engine.evaluate_all = spy_signals
        try:
            appmod.refresh_loop(max_cycles=1)
        finally:
            appmod.focus_manager.deep_evaluation_snapshots = orig_deep
            appmod.signal_engine.evaluate_all = orig_sig
        self.assertIsNotNone(appmod._last_cycle_ok,
                             "a deep_eval crash must not abort the cycle")
        self.assertIn("deep_snaps", seen, "signals must still run after a deep_eval crash")
        self.assertEqual(seen["deep_snaps"], {},
                         "signals must receive the empty fallback, not stale data")


if __name__ == "__main__":
    unittest.main()
