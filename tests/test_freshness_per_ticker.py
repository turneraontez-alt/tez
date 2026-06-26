"""Per-ticker websocket/REST freshness regression tests (P1).

The bug: ``fetch_asset_raw`` labelled the whole fetch ``"ws"`` whenever the
websocket was connected, but ``HybridMarketData.get_orderbook`` can fall back to
REST *per ticker*. The freshness gate then took the websocket branch and, when
the per-ticker book carried no event time, borrowed global websocket health
(``last_orderbook_at``), which advances whenever ANY market updates. So one
active market could make a different market's missing/stale book read as fresh
and slip past the ``v5_data_valid`` gate.

These tests pin the fix end to end:
  * HybridMarketData stamps the per-ticker book source onto the returned book.
  * fetch_asset_raw carries that source out and reads the ws ``_updated_at``.
  * apply_snapshot_freshness judges the book on the current ticker's source and
    timestamp, never on global websocket health.

All deterministic: fixed timestamps, no network, no wall clock.
"""
from __future__ import annotations

import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)
os.environ.setdefault("Q15_AUTOSTART_REFRESH", "0")  # never spawn the live loop

from q15_upgrade.hybrid_data import HybridMarketData
from q15_upgrade.v5_hardening import apply_snapshot_freshness


# --------------------------------------------------------------------------- helpers
def _cfg5():
    """Pin the freshness budget to 5s so the env can't perturb the test."""
    return types.SimpleNamespace(max_data_age_s=5.0)


def _fresh_book(_now):
    # Valid, two-sided book so the only axis under test is freshness.
    return {"yes_bid": 40.0, "yes_ask": 60.0}


class _FakeWs:
    def __init__(self, book, connected=True):
        self._book = book
        self._connected = connected

    def is_connected(self):
        return self._connected

    def subscribe(self, tickers):
        pass

    def health(self):
        return {"connected": self._connected, "mode": "websocket"}

    def get_orderbook(self, ticker):
        return self._book

    def get_trades(self, ticker, min_ts=None):
        return []


class _FakeRest:
    def get_orderbook(self, ticker):
        return {"yes_dollars": [], "no_dollars": []}

    def get_trades(self, ticker, min_ts=None):
        return []


# --------------------------------------------------------------------------- HybridMarketData tagging
def test_hybrid_tags_ws_book_and_preserves_updated_at():
    ws_book = {
        "yes_dollars": [["0.40", "10"]],
        "no_dollars": [["0.40", "10"]],
        "_source": "websocket",
        "_updated_at": 123.0,
    }
    hd = HybridMarketData(_FakeRest(), _FakeWs(ws_book))
    out = hd.get_orderbook("T")
    assert out["_source"] == "ws"
    assert out["_updated_at"] == 123.0
    # The upstream ws dict must not be mutated in place.
    assert ws_book["_source"] == "websocket"


def test_hybrid_tags_rest_when_ws_book_missing():
    # Connected, but the websocket has no fresh book for this ticker -> REST.
    hd = HybridMarketData(_FakeRest(), _FakeWs(None))
    out = hd.get_orderbook("T")
    assert out["_source"] == "rest"
    assert hd._ticker_sources["T"]["book"] == "rest"


def test_hybrid_tags_rest_when_disconnected():
    hd = HybridMarketData(_FakeRest(), _FakeWs(None, connected=False))
    out = hd.get_orderbook("T")
    assert out["_source"] == "rest"


def test_hybrid_handles_none_rest_book():
    class _NoneRest:
        def get_orderbook(self, ticker):
            return None

        def get_trades(self, ticker, min_ts=None):
            return []

    hd = HybridMarketData(_NoneRest(), _FakeWs(None))
    assert hd.get_orderbook("T") is None  # nothing to tag, must not raise


# --------------------------------------------------------------------------- apply_snapshot_freshness
def test_rest_fallback_book_not_rescued_by_global_ws_health():
    # The core bug: overall connection is "ws", but THIS ticker's book came from
    # the REST fallback and is genuinely stale (fetched 10s ago). Global ws
    # health stays fresh because OTHER markets are live. The gate must judge the
    # book on the per-ticker REST fetch time, not on global health.
    now = 1000.0
    raw = {
        "source": "ws",
        "book_source": "rest",
        "spot": {"ok": True, "ts": now},
        "fetch_completed_at": now - 10.0,
        "book_event_ts": None,
        "trades": [],
    }
    ws_health = {"last_orderbook_at": now, "last_message_at": now}
    out = apply_snapshot_freshness(_fresh_book(now), raw, now, ws_health, _cfg5())
    assert out["v5_data_valid"] is False
    assert any(r.startswith("orderbook_stale_") for r in out["v5_data_invalid_reasons"])
    assert out["book_source_mode"] == "rest"


def test_ws_book_uses_own_event_ts_when_fresh():
    now = 1000.0
    raw = {
        "source": "ws",
        "book_source": "ws",
        "spot": {"ok": True, "ts": now},
        "book_event_ts": now,
        "fetch_completed_at": now,
        "trades": [],
    }
    out = apply_snapshot_freshness(_fresh_book(now), raw, now, {}, _cfg5())
    assert out["v5_data_valid"] is True
    assert out["orderbook_event_ts"] == now
    assert out["book_source_mode"] == "ws"


def test_ws_book_missing_event_ts_not_rescued_by_global_health():
    # A ws book that somehow lost its event time must fail closed (timestamp
    # missing) rather than borrow another ticker's global websocket freshness.
    now = 1000.0
    raw = {
        "source": "ws",
        "book_source": "ws",
        "spot": {"ok": True, "ts": now},
        "book_event_ts": None,
        "fetch_completed_at": now,
        "trades": [],
    }
    ws_health = {"last_orderbook_at": now, "last_message_at": now}
    out = apply_snapshot_freshness(_fresh_book(now), raw, now, ws_health, _cfg5())
    assert out["v5_data_valid"] is False
    assert "orderbook_timestamp_missing" in out["v5_data_invalid_reasons"]


def test_ws_book_stale_event_ts_is_flagged():
    now = 1000.0
    raw = {
        "source": "ws",
        "book_source": "ws",
        "spot": {"ok": True, "ts": now},
        "book_event_ts": now - 9.0,
        "fetch_completed_at": now,
        "trades": [],
    }
    out = apply_snapshot_freshness(
        _fresh_book(now), raw, now, {"last_orderbook_at": now}, _cfg5()
    )
    assert out["v5_data_valid"] is False
    assert any(r.startswith("orderbook_stale_") for r in out["v5_data_invalid_reasons"])


def test_legacy_raw_without_book_source_falls_back_to_source():
    # Backward-compat: a raw result that predates book_source still works off the
    # overall connection source (here REST -> judged by fetch-completion time).
    now = 1000.0
    raw = {
        "source": "rest",
        "spot": {"ok": True, "ts": now},
        "fetch_completed_at": now,
        "trades": [],
    }
    out = apply_snapshot_freshness(_fresh_book(now), raw, now, {}, _cfg5())
    assert out["v5_data_valid"] is True
    assert out["book_source_mode"] == "rest"


# --------------------------------------------------------------------------- fetch_asset_raw wiring
def test_fetch_asset_raw_carries_ws_source_and_event_ts(monkeypatch):
    pytest.importorskip("flask")
    import app as appmod

    class _MD:
        def is_connected(self):
            return True

        def get_orderbook(self, ticker):
            return {
                "yes_dollars": [["0.40", "10"]],
                "no_dollars": [["0.40", "10"]],
                "_source": "ws",
                "_updated_at": 555.0,
            }

        def get_trades(self, ticker, min_ts=None):
            return []

    monkeypatch.setattr(appmod, "market_data", _MD())
    monkeypatch.setattr(appmod, "get_spot", lambda asset: {"ok": True, "ts": 999.0})
    raw = appmod.fetch_asset_raw("BTC", {"ticker": "BTC-T"}, 1000.0, None)
    assert raw["book_source"] == "ws"
    assert raw["book_event_ts"] == 555.0


def test_fetch_asset_raw_rest_book_has_no_event_ts(monkeypatch):
    pytest.importorskip("flask")
    import app as appmod

    class _MD:
        def is_connected(self):
            return True  # globally connected, but THIS ticker fell back to REST

        def get_orderbook(self, ticker):
            return {"yes_dollars": [], "no_dollars": [], "_source": "rest"}

        def get_trades(self, ticker, min_ts=None):
            return []

    monkeypatch.setattr(appmod, "market_data", _MD())
    monkeypatch.setattr(appmod, "get_spot", lambda asset: {"ok": True, "ts": 999.0})
    raw = appmod.fetch_asset_raw("BTC", {"ticker": "BTC-T"}, 1000.0, None)
    # Per-ticker source is REST even though is_connected() is True.
    assert raw["book_source"] == "rest"
    assert raw["book_event_ts"] is None
    assert raw["source"] == "ws"  # overall connection flag preserved for callers
