"""Feed integrity gates: a corrupt or frozen book must never reach a model input.

Two collectors were publishing data no downstream consumer could tell was wrong:

* ``spot_depth`` had no crossed-book guard (both sibling collectors do), so a
  delta stream that lost a cancel during a reconnect gap emitted a negative
  spread and a bogus ``mid`` — which feeds the rti microstructure features.
* MarketLead's freshness gates all measured the TRANSPORT, never the book, so a
  venue whose level2 subscription died while heartbeats kept flowing was ingested
  as a live source at quality 1.0.
"""
from __future__ import annotations

import threading
import time

from q15_upgrade import kalshi_rest
from q15_upgrade.marketlead.config import MarketLeadConfig
from q15_upgrade.marketlead.features import MarketLeadFeatureEngine
from spot_depth import SpotDepthRecorder


def test_kalshi_rest_reuses_one_read_only_session_per_thread(monkeypatch):
    created = []

    class Response:
        status_code = 200
        headers = {}

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"ok": True}

    class Session:
        def __init__(self):
            self.headers = {}
            created.append(self)

        def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(kalshi_rest.requests, "Session", Session)
    client = kalshi_rest.KalshiClient(rate=100.0, capacity=100)
    assert client._get("/first") == {"ok": True}
    assert client._get("/second") == {"ok": True}
    assert len(created) == 1

    thread = threading.Thread(target=lambda: client._get("/third"))
    thread.start()
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert len(created) == 2


def test_kalshi_rest_get_series_uses_official_series_endpoint(monkeypatch):
    client = kalshi_rest.KalshiClient(rate=100.0, capacity=100)
    observed = []

    def fake_get(path, **kwargs):
        observed.append((path, kwargs))
        return {"series": {"ticker": "KXBTC15M", "fee_type": "quadratic"}}

    monkeypatch.setattr(client, "_get", fake_get)
    assert client.get_series("KXBTC15M") == {
        "ticker": "KXBTC15M", "fee_type": "quadratic",
    }
    assert observed == [("/series/KXBTC15M", {"retries": 3})]


# ------------------------------------------------------------ spot_depth guard

def _recorder(tmp_path):
    return SpotDepthRecorder(assets=["BTC"], db_path=str(tmp_path / "depth.sqlite3"))


def _seed_book(rec, *, bids, asks, ts):
    rec._replace_book("BTC", provider="coinbase", symbol="BTC-USD",
                      bids=bids, asks=asks, ts=ts)


def test_healthy_book_still_publishes(tmp_path):
    rec = _recorder(tmp_path)
    now = time.time()
    _seed_book(rec, bids=[["100.0", "2"]], asks=[["101.0", "3"]], ts=now)

    with rec._lock:
        row = rec._snapshot_locked("BTC", rec._books["BTC"], now)

    assert row is not None
    assert row["best_bid"] == 100.0 and row["best_ask"] == 101.0
    assert row["mid"] == 100.5
    assert row["spread_bps"] > 0


def test_spot_freshness_and_trade_windows_use_local_receipt_time(tmp_path):
    """Exchange clocks are provenance, never the freshness authority."""
    rec = _recorder(tmp_path)
    now = time.time()
    rec._replace_book(
        "BTC",
        provider="coinbase",
        symbol="BTC-USD",
        bids=[["100.0", "2"]],
        asks=[["101.0", "3"]],
        ts=now + 1.0,
        received_at=now - 0.1,
    )
    rec._record_trade(
        "BTC",
        provider="coinbase",
        symbol="BTC-USD",
        side="buy",
        price=100.5,
        size=2.0,
        ts=now + 0.8,
        received_at=now - 0.2,
    )

    with rec._lock:
        row = rec._snapshot_locked("BTC", rec._books["BTC"], now)

    assert row is not None
    assert row["book_age_seconds"] == 0.1
    assert row["book_source_age_seconds"] == -1.0
    assert row["orderbook_received_at"] == now - 0.1
    assert row["orderbook_ts"] == now + 1.0
    assert row["trade_age_seconds"] == 0.2
    assert row["trade_source_age_seconds"] == -0.8
    assert row["trade_received_at"] == now - 0.2
    assert row["trade_ts"] == now + 0.8
    assert row["trade_buy_notional_5s"] == 201.0


def test_crossed_book_is_dropped_not_published(tmp_path):
    """bid >= ask means the book is corrupt; publishing it yields a negative spread
    and a mid that never existed on the exchange."""
    rec = _recorder(tmp_path)
    now = time.time()
    _seed_book(rec, bids=[["105.0", "2"]], asks=[["101.0", "3"]], ts=now)

    with rec._lock:
        row = rec._snapshot_locked("BTC", rec._books["BTC"], now)

    assert row is None, "crossed book was published"
    assert "BTC" in rec._resync_assets
    assert rec._crossed_books["BTC"] == 1


def test_flagged_book_stops_absorbing_deltas_until_a_snapshot(tmp_path):
    """Patching more deltas onto a known-corrupt book propagates the error and keeps
    refreshing its timestamp, so it goes on looking fresh."""
    rec = _recorder(tmp_path)
    now = time.time()
    _seed_book(rec, bids=[["105.0", "2"]], asks=[["101.0", "3"]], ts=now)
    with rec._lock:
        rec._snapshot_locked("BTC", rec._books["BTC"], now)
    assert "BTC" in rec._resync_assets

    rec._update_book("BTC", provider="coinbase", symbol="BTC-USD",
                     changes=[["buy", "106.0", "1"]], ts=now + 1)

    assert "BTC" not in rec._books, "corrupt book kept absorbing deltas"

    # A full snapshot clears the flag and restores service.
    _seed_book(rec, bids=[["100.0", "2"]], asks=[["101.0", "3"]], ts=now + 2)
    assert "BTC" not in rec._resync_assets
    with rec._lock:
        row = rec._snapshot_locked("BTC", rec._books["BTC"], now + 2)
    assert row is not None and row["mid"] == 100.5


# ------------------------------------------------------- marketlead book age

def _ws_source(book_age):
    return {
        "price": 100.0,
        "timestamp": None,           # filled per-call; read-time stamped in production
        "transport": "websocket_coinbase",
        "transport_connected": True,
        "transport_message_age_seconds": 0.1,   # heartbeats keep this low
        "best_bid": 99.5,
        "best_ask": 100.5,
        "spread_bps": 10.0,
        "book_update_age_seconds": book_age,
    }


def _run(engine, book_age, now):
    src = _ws_source(book_age)
    src["timestamp"] = now          # sample_timestamp is stamped at READ time
    return engine._update_sources("BTC", {"sources": {"coinbase": src},
                                          "fetched_at": now}, now)


def test_frozen_book_is_rejected_despite_a_healthy_transport():
    """The failure this closes: level2 dies, heartbeats continue, and a ten-minute-old
    book is ingested as a live venue."""
    engine = MarketLeadFeatureEngine(MarketLeadConfig(book_stale_seconds=60.0))

    assert _run(engine, 600.0, time.time()) == []


def test_fresh_book_is_accepted():
    engine = MarketLeadFeatureEngine(MarketLeadConfig(book_stale_seconds=60.0))

    assert len(_run(engine, 0.5, time.time())) == 1


def test_book_age_gate_can_be_disabled():
    """0 disables the check — an escape hatch if a venue stops reporting book age."""
    engine = MarketLeadFeatureEngine(MarketLeadConfig(book_stale_seconds=0.0))

    assert len(_run(engine, 600.0, time.time())) == 1


def test_missing_book_age_does_not_reject():
    """A venue that never reports book age keeps prior behaviour rather than going dark."""
    engine = MarketLeadFeatureEngine(MarketLeadConfig(book_stale_seconds=60.0))

    assert len(_run(engine, None, time.time())) == 1
