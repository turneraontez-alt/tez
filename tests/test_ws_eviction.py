from __future__ import annotations

import os
import sys
import threading
import time
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)


def _ensure_ws_client_importable():
    """Import ws_client even where the optional crypto/ws deps are unbuildable.

    ``ws_client`` guards its optional imports with ``except Exception`` and
    falls back to a disabled feed.  In some sandboxes ``cryptography`` raises a
    ``pyo3_runtime.PanicException`` (a ``BaseException``, not ``Exception``)
    at import because ``_cffi_backend`` is absent, which escapes that guard.
    These eviction tests never touch the signing path, so we install harmless
    stub modules only when the real import is broken — production is untouched.
    """
    try:
        import cryptography.hazmat.primitives.hashes  # noqa: F401
        return
    except BaseException:
        pass
    for name in (
        "cryptography",
        "cryptography.hazmat",
        "cryptography.hazmat.primitives",
        "cryptography.hazmat.primitives.hashes",
        "cryptography.hazmat.primitives.serialization",
        "cryptography.hazmat.primitives.asymmetric",
        "cryptography.hazmat.primitives.asymmetric.padding",
    ):
        sys.modules.setdefault(name, types.ModuleType(name))
    if "websockets" not in sys.modules:
        sys.modules.setdefault("websockets", types.ModuleType("websockets"))


_ensure_ws_client_importable()

from q15_upgrade.hybrid_data import HybridMarketData
from q15_upgrade.ws_client import KalshiWebSocketFeed


def _make_feed() -> KalshiWebSocketFeed:
    """Construct a feed without spawning the network thread.

    With no Kalshi key / private key configured the feed is ``enabled=False``
    so ``__init__`` never calls ``start()`` — deterministic, no sockets.
    """
    for key in (
        "KALSHI_API_KEY_ID",
        "KALSHI_ACCESS_KEY",
        "KALSHI_PRIVATE_KEY",
        "KALSHI_API_PRIVATE_KEY",
        "KALSHI_PRIVATE_KEY_PATH",
    ):
        os.environ.pop(key, None)
    feed = KalshiWebSocketFeed()
    assert not feed.enabled  # guard: must not have started a thread
    return feed


def _seed_ticker(feed: KalshiWebSocketFeed, ticker: str, now: float) -> None:
    """Populate every leaked per-ticker dict for ``ticker``."""
    feed._books[ticker] = {"yes": {0.5: 10.0}, "no": {0.5: 10.0}, "updated_at": now}
    feed._trades[ticker].append({"id": f"{ticker}-1", "ts": now, "yes_cents": 50.0, "count": 1.0})
    feed._market_status[ticker] = {"market_ticker": ticker, "status": "open"}


class TestWsClientEviction(unittest.TestCase):
    def test_subscribe_evicts_non_desired_and_retains_desired(self):
        feed = _make_feed()
        now = time.time()
        for ticker in ("A", "B", "C"):
            _seed_ticker(feed, ticker, now)
        feed.subscribe(["A", "B", "C"])
        self.assertEqual(feed._desired, {"A", "B", "C"})

        # Roll over: drop A and B, keep C, add D.
        feed.subscribe(["C", "D"])
        self.assertEqual(feed._desired, {"C", "D"})

        # A and B are no longer desired -> evicted from every per-ticker dict.
        for ticker in ("A", "B"):
            self.assertNotIn(ticker, feed._books)
            self.assertNotIn(ticker, feed._trades)
            self.assertNotIn(ticker, feed._market_status)

        # C is still desired -> its populated state is retained untouched.
        self.assertIn("C", feed._books)
        self.assertEqual(feed._books["C"]["yes"], {0.5: 10.0})
        self.assertIn("C", feed._trades)
        self.assertEqual(len(feed._trades["C"]), 1)
        self.assertIn("C", feed._market_status)

        # D works: it can receive and serve fresh data after subscribing.
        _seed_ticker(feed, "D", time.time())
        book = feed.get_orderbook("D", max_age=3.0)
        self.assertIsNotNone(book)
        self.assertEqual(book["_source"], "websocket")

    def test_active_ticker_never_evicted_when_set_unchanged(self):
        feed = _make_feed()
        now = time.time()
        _seed_ticker(feed, "C", now)
        feed.subscribe(["C"])
        # Re-subscribing to the same set must not drop the active book.
        feed.subscribe(["C"])
        self.assertIn("C", feed._books)
        self.assertIn("C", feed._market_status)
        self.assertIn("C", feed._trades)

    def test_eviction_is_lock_safe_under_concurrent_updates(self):
        """No exception when subscribe() prunes while another thread writes books."""
        feed = _make_feed()
        stop = threading.Event()
        errors = []

        def writer():
            try:
                while not stop.is_set():
                    feed._handle_book_snapshot(
                        {
                            "market_ticker": "LIVE",
                            "yes_dollars": [["0.50", "10"]],
                            "no_dollars": [["0.50", "10"]],
                        },
                        time.time(),
                    )
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        try:
            for _ in range(200):
                # "LIVE" stays desired throughout, so it is never evicted.
                feed.subscribe(["LIVE", "OTHER"])
                feed.subscribe(["LIVE"])
        finally:
            stop.set()
            t.join(timeout=2.0)

        self.assertEqual(errors, [])
        self.assertIn("LIVE", feed._desired)
        # The continuously-written, always-desired ticker survived the churn.
        self.assertIn("LIVE", feed._books)


class FakeWs:
    def __init__(self, connected=True):
        self._connected = connected
        self.subscribed = None

    def is_connected(self):
        return self._connected

    def subscribe(self, tickers):
        self.subscribed = set(tickers)

    def health(self):
        return {"connected": self._connected, "mode": "websocket"}

    def get_orderbook(self, ticker):
        return None

    def get_trades(self, ticker, min_ts=None):
        return []


class FakeRest:
    def get_orderbook(self, ticker):
        return {"yes_dollars": [], "no_dollars": []}

    def get_trades(self, ticker, min_ts=None):
        return []


class TestHybridTickerSourcesPrune(unittest.TestCase):
    def test_connected_ws_rest_book_fallback_is_marked_rest(self):
        hd = HybridMarketData(FakeRest(), FakeWs(connected=True))
        book = hd.get_orderbook("A")
        self.assertEqual(book["_hybrid_source"], "rest")
        self.assertEqual(hd._ticker_sources["A"]["book"], "rest")
        self.assertIn("_updated_at", book)

    def test_subscribe_prunes_ticker_sources_to_wanted(self):
        hd = HybridMarketData(FakeRest(), FakeWs())
        # Populate diagnostic source metadata for A, B, C (as get_orderbook would).
        hd._ticker_sources = {
            "A": {"book": "ws"},
            "B": {"book": "rest"},
            "C": {"book": "ws", "trades": "ws"},
        }
        hd.subscribe(["C", "D"])
        self.assertNotIn("A", hd._ticker_sources)
        self.assertNotIn("B", hd._ticker_sources)
        # C retained with its metadata intact; D not yet seen.
        self.assertEqual(hd._ticker_sources["C"], {"book": "ws", "trades": "ws"})
        self.assertNotIn("D", hd._ticker_sources)

    def test_ticker_sources_does_not_grow_unbounded(self):
        hd = HybridMarketData(FakeRest(), FakeWs(connected=False))
        # Simulate many market rollovers; only the latest wanted set should remain.
        for batch in range(10):
            ticker = f"MKT-{batch}"
            hd.get_orderbook(ticker)  # records a source entry
            hd.subscribe([ticker])   # prune to the single wanted ticker
            self.assertEqual(set(hd._ticker_sources), {ticker})


if __name__ == "__main__":
    unittest.main()
