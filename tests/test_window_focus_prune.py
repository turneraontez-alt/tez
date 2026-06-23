"""Bounded-retention prune for TwoWindowFocusManager.

Behaviour-neutral memory fix: `reconcile_settlements` must evict cycles that are
BOTH graded AND whose close_time is older than `_CYCLE_RETENTION_SECONDS`, and
drop the matching close-keyed `_top_by_close` / `_rankings` entries — while never
touching ungraded, current, or recently-closed cycles. Evicted cycles re-hydrate
from Postgres on demand, so no prediction/learning output changes.

Deterministic: cycles are injected directly into the manager's in-memory maps and
all DB/network access is stubbed (no real Postgres, no real Kalshi).
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.window_focus import (  # noqa: E402
    FocusSettings,
    TwoWindowFocusManager,
    _CYCLE_RETENTION_SECONDS,
)


class Store:
    """Inert store: no DB. _query/_execute degrade to no-ops because the methods
    `query`/`execute` are absent, so hydration on a re-created cycle is a no-op."""
    enabled = False


class UnsettledClient:
    """Reconcile's settlement fetch returns an unsettled market, so the pending
    loop never grades anything itself — the test controls `graded` explicitly and
    isolates the prune step."""

    def __init__(self):
        self.calls = []

    def get_market(self, ticker):
        self.calls.append(ticker)
        return {"status": "active", "result": ""}


# Fixed reference "now" well past any epoch boundary so arithmetic is exact and
# nothing depends on wall-clock time.
NOW = 2_000_000_000.0


def _manager():
    # reconcile_interval_seconds default is 30; _last_reconcile starts at 0 and
    # NOW is huge, so the first reconcile call always proceeds.
    return TwoWindowFocusManager(Store(), None, None, UnsettledClient(), FocusSettings())


def _make_cycle(ticker, asset, close_ts, graded):
    """Minimal cycle dict mirroring TwoWindowFocusManager._cycle()."""
    return {
        "ticker": ticker,
        "asset": asset,
        "close_time": float(close_ts),
        "predictions": {"15m": {"side": "YES"}, "10m": {"side": "YES"}},
        "final_side": "YES",
        "agreement": "SAME",
        "decision_state": "AGREEMENT",
        "graded": graded,
        "actual_result": "YES" if graded else None,
    }


def _inject(manager, cycle):
    """Register a cycle plus its close-keyed ranking/top state, the way
    _rebuild_rankings would (both maps keyed by str(close_time))."""
    ticker = cycle["ticker"]
    asset = cycle["asset"]
    close_key = str(cycle["close_time"])
    manager._cycles[ticker] = cycle
    manager._top_by_close[close_key] = {asset}
    manager._rankings[close_key] = {"close_time": close_key, "final": [{"asset": asset}]}


class PruneTests(unittest.TestCase):
    def test_only_old_and_graded_cycles_are_evicted(self):
        m = _manager()

        # Comfortably past the cutoff (graded + old)  -> EVICT
        old_close = NOW - _CYCLE_RETENTION_SECONDS - 1_000.0
        old_graded = _make_cycle("OLD_GRADED", "BTC", old_close, graded=True)

        # Graded but closed recently (within retention)  -> RETAIN
        recent_close = NOW - 60.0
        recent_graded = _make_cycle("RECENT_GRADED", "ETH", recent_close, graded=True)

        # Old close_time but NOT graded  -> RETAIN (critical invariant)
        old_ungraded = _make_cycle("OLD_UNGRADED", "SOL", old_close - 5.0, graded=False)

        # Current/future close, ungraded  -> RETAIN
        future_ungraded = _make_cycle("FUTURE_UNGRADED", "XRP", NOW + 600.0, graded=False)

        for cycle in (old_graded, recent_graded, old_ungraded, future_ungraded):
            _inject(m, cycle)

        m.reconcile_settlements(NOW)

        # Old AND graded is gone, including its ranking/top entries.
        self.assertNotIn("OLD_GRADED", m._cycles)
        old_key = str(old_graded["close_time"])
        self.assertNotIn(old_key, m._rankings)
        self.assertNotIn(old_key, m._top_by_close)

        # Everything else is retained.
        self.assertIn("RECENT_GRADED", m._cycles)
        self.assertIn("OLD_UNGRADED", m._cycles)
        self.assertIn("FUTURE_UNGRADED", m._cycles)

        # Retained cycles keep their close-keyed ranking/top entries.
        for cycle in (recent_graded, old_ungraded, future_ungraded):
            key = str(cycle["close_time"])
            self.assertIn(key, m._rankings)
            self.assertIn(key, m._top_by_close)

        # The old-but-ungraded cycle's window must NOT have been dropped even
        # though its close_time is past the cutoff — its cycle is still in memory.
        self.assertIn(str(old_ungraded["close_time"]), m._rankings)

    def test_retained_cycle_still_queryable(self):
        m = _manager()
        old_close = NOW - _CYCLE_RETENTION_SECONDS - 1_000.0
        _inject(m, _make_cycle("OLD_GRADED", "BTC", old_close, graded=True))
        _inject(m, _make_cycle("RECENT_GRADED", "ETH", NOW - 30.0, graded=True))

        m.reconcile_settlements(NOW)

        tickers = {row["ticker"] for row in m.predictions_status()}
        self.assertNotIn("OLD_GRADED", tickers)
        self.assertIn("RECENT_GRADED", tickers)

        # focus_status reflects the same pruned membership and stays well-formed.
        status = m.focus_status()
        status_tickers = {row["ticker"] for row in status["cycles"]}
        self.assertEqual(status_tickers, {"RECENT_GRADED"})
        self.assertNotIn(str(old_close), status["top_assets_by_close"])

    def test_close_window_kept_while_any_cycle_for_it_remains(self):
        # Two cycles share one old close window: one graded, one ungraded. The
        # graded one is evicted, but the window's ranking/top entries must remain
        # because the ungraded cycle for that close window is still in memory.
        m = _manager()
        shared_close = NOW - _CYCLE_RETENTION_SECONDS - 500.0
        shared_key = str(float(shared_close))

        graded = _make_cycle("SHARED_GRADED", "BTC", shared_close, graded=True)
        ungraded = _make_cycle("SHARED_UNGRADED", "ETH", shared_close, graded=False)
        m._cycles["SHARED_GRADED"] = graded
        m._cycles["SHARED_UNGRADED"] = ungraded
        m._top_by_close[shared_key] = {"BTC", "ETH"}
        m._rankings[shared_key] = {"close_time": shared_key, "final": [{"asset": "BTC"}, {"asset": "ETH"}]}

        m.reconcile_settlements(NOW)

        self.assertNotIn("SHARED_GRADED", m._cycles)
        self.assertIn("SHARED_UNGRADED", m._cycles)
        # Window state survives because an in-memory cycle still references it.
        self.assertIn(shared_key, m._rankings)
        self.assertIn(shared_key, m._top_by_close)

    def test_prune_is_idempotent_and_noop_without_stale(self):
        m = _manager()
        _inject(m, _make_cycle("RECENT_GRADED", "ETH", NOW - 30.0, graded=True))
        _inject(m, _make_cycle("FUTURE_UNGRADED", "XRP", NOW + 600.0, graded=False))

        before = set(m._cycles)
        m._prune_settled_cycles(NOW)
        m._prune_settled_cycles(NOW)
        self.assertEqual(set(m._cycles), before)


if __name__ == "__main__":
    unittest.main()
