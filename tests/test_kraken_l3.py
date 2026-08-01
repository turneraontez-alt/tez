from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import time
from urllib.parse import urlencode

import pytest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from kraken_l3 import (
    KrakenL3Collector,
    PARTIAL_FILL_FLOW_SCHEMA_VERSION,
    _configured_symbols,
    kraken_l3_health,
    kraken_l3_subscribe_payload,
    sign_kraken_private_request,
)


def test_kraken_l3_subscribe_payload_is_authenticated():
    payload = kraken_l3_subscribe_payload(
        ["ETH/USD", "XBT/USD"],
        token="token-1",
        depth=10,
    )

    assert payload == {
        "method": "subscribe",
        "params": {
            "channel": "level3",
            "symbol": ["ETH/USD", "XBT/USD"],
            "depth": 10,
            "snapshot": True,
            "token": "token-1",
        },
    }


def test_kraken_private_signature(monkeypatch):
    secret = base64.b64encode(b"secret-bytes").decode("ascii")
    monkeypatch.setenv("KRAKEN_API_SECRET", secret)
    data = {"nonce": "12345"}

    expected = base64.b64encode(
        hmac.new(
            b"secret-bytes",
            b"/0/private/GetWebSocketsToken"
            + hashlib.sha256(("12345" + urlencode(data)).encode("utf-8")).digest(),
            hashlib.sha512,
        ).digest()
    ).decode("ascii")

    assert sign_kraken_private_request("/0/private/GetWebSocketsToken", data) == expected


def test_kraken_l3_symbols_normalize_dash_to_slash(monkeypatch):
    monkeypatch.setenv("Q15_KRAKEN_L3_SYMBOLS", "XBT-USD,ETH/USD,XDG-USD")
    assert _configured_symbols() == ["BTC/USD", "ETH/USD", "DOGE/USD"]


def test_kraken_l3_snapshot_update_and_summary(tmp_path, monkeypatch):
    db = str(tmp_path / "kraken_l3.sqlite3")
    monkeypatch.setenv("Q15_KRAKEN_L3_RAW_EVENTS_ENABLED", "true")
    feed = KrakenL3Collector(symbols=["XBT/USD"], db_path=db)
    feed.summary_levels = 2
    feed.depth = 10

    feed.handle_message(json.dumps({
        "channel": "level3",
        "type": "snapshot",
        "data": [{
            "symbol": "XBT/USD",
            "checksum": 100,
            "bids": [
                {"order_id": "bid1", "limit_price": 100.0, "order_qty": 1.0},
                {"order_id": "bid2", "limit_price": 99.5, "order_qty": 2.0},
            ],
            "asks": [
                {"order_id": "ask1", "limit_price": 101.0, "order_qty": 1.5},
                {"order_id": "ask2", "limit_price": 101.5, "order_qty": 1.0},
            ],
        }],
    }))
    feed.handle_message(json.dumps({
        "channel": "level3",
        "type": "update",
        "data": [{
            "symbol": "XBT/USD",
            "checksum": 101,
            "bids": [{"order_id": "bid3", "limit_price": 100.5, "order_qty": 0.5}],
            "asks": [{"order_id": "ask2", "limit_price": 101.5, "order_qty": 0.0, "event": "delete"}],
        }],
    }))

    snapshot = feed.latest_snapshot("XBT-USD", now=time.time())
    assert snapshot is not None
    assert snapshot["book_timestamp"] > 0
    assert snapshot["book_age_seconds"] < 1.0
    assert snapshot["best_bid"] == 100.5
    assert feed.record_once() == 1

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM kraken_l3_summaries").fetchone()
        assert row["symbol"] == "XBT/USD"
        assert row["checksum"] == 101
        assert row["best_bid"] == 100.5
        assert row["best_ask"] == 101.0
        assert row["bid_order_count"] == 3
        assert row["ask_order_count"] == 1
        assert row["add_count_60s"] == 1
        assert row["delete_count_60s"] == 1
        assert conn.execute("SELECT count(*) FROM kraken_l3_events").fetchone()[0] == 2
    finally:
        conn.close()


def test_kraken_l3_modify_records_only_observed_partial_fill_delta(tmp_path):
    db = str(tmp_path / "kraken_l3.sqlite3")
    feed = KrakenL3Collector(symbols=["BTC/USD"], db_path=db)
    feed.summary_levels = 2
    feed.depth = 10
    feed.handle_message(json.dumps({
        "channel": "level3",
        "type": "snapshot",
        "data": [{
            "symbol": "BTC/USD",
            "checksum": 100,
            "bids": [
                {"order_id": "bid1", "limit_price": 100.0, "order_qty": 1.0},
            ],
            "asks": [
                {"order_id": "ask1", "limit_price": 101.0, "order_qty": 1.0},
                {"order_id": "ask2", "limit_price": 102.0, "order_qty": 1.0},
            ],
        }],
    }))
    feed.handle_message(json.dumps({
        "channel": "level3",
        "type": "update",
        "data": [{
            "symbol": "BTC/USD",
            "checksum": 101,
            "bids": [{
                "event": "modify",
                "order_id": "bid1",
                "limit_price": 100.0,
                "order_qty": 0.6,
            }],
            # A delete is ambiguous (full fill or cancel) and must not be
            # fabricated as signed trade flow.
            "asks": [{
                "event": "delete",
                "order_id": "ask1",
                "limit_price": 101.0,
                "order_qty": 1.0,
            }],
        }],
    }))

    assert feed._orders["BTC/USD"]["bid1"].size == pytest.approx(0.6)
    assert feed.record_once() == 1
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM kraken_l3_summaries").fetchone()
        assert row["partial_fill_flow_schema_version"] == (
            PARTIAL_FILL_FLOW_SCHEMA_VERSION
        )
        assert row["trade_count_60s"] == 1
        assert row["matched_buy_notional_60s"] == pytest.approx(40.0)
        assert row["matched_sell_notional_60s"] == pytest.approx(0.0)
        assert row["net_matched_buy_notional_60s"] == pytest.approx(40.0)
        assert row["delete_count_60s"] == 1
    finally:
        conn.close()


def test_kraken_l3_drops_crossed_book_and_requests_resync(tmp_path):
    feed = KrakenL3Collector(symbols=["BTC/USD"], db_path=str(tmp_path / "kraken_l3.sqlite3"))
    feed.summary_levels = 2
    feed.depth = 10

    feed.handle_message(json.dumps({
        "channel": "level3",
        "type": "snapshot",
        "data": [{
            "symbol": "BTC/USD",
            "checksum": 100,
            "bids": [{"order_id": "bid1", "limit_price": 101.0, "order_qty": 1.0}],
            "asks": [{"order_id": "ask1", "limit_price": 100.0, "order_qty": 1.0}],
        }],
    }))

    assert feed.record_once() == 0
    health = feed.health()
    assert health["crossed_book_drops"] == 1
    assert health["snapshot_loaded"]["BTC/USD"] is False
    assert feed._take_resync_requested() is True
    assert feed._take_resync_requested() is False


def test_kraken_l3_truncates_levels_pushed_out_of_subscription_depth(tmp_path):
    feed = KrakenL3Collector(
        symbols=["BTC/USD"], db_path=str(tmp_path / "kraken_l3.sqlite3")
    )
    feed.depth = 2
    feed.summary_levels = 2
    feed.handle_message(json.dumps({
        "channel": "level3",
        "type": "snapshot",
        "data": [{
            "symbol": "BTC/USD",
            "checksum": 1,
            "bids": [
                {"order_id": "bid100", "limit_price": 100.0, "order_qty": 1.0},
                {"order_id": "bid99", "limit_price": 99.0, "order_qty": 1.0},
            ],
            "asks": [
                {"order_id": "ask101", "limit_price": 101.0, "order_qty": 1.0},
                {"order_id": "ask102", "limit_price": 102.0, "order_qty": 1.0},
            ],
        }],
    }))
    feed.handle_message(json.dumps({
        "channel": "level3",
        "type": "update",
        "data": [{
            "symbol": "BTC/USD",
            "checksum": 2,
            "bids": [
                {"event": "add", "order_id": "bid1005", "limit_price": 100.5, "order_qty": 1.0},
            ],
            "asks": [],
        }],
    }))

    assert "bid99" not in feed._orders["BTC/USD"]
    assert set(feed._levels["BTC/USD"]["buy"]) == {100.0, 100.5}
    assert feed.record_once() == 1
    health = feed.health()
    assert health["depth_truncations"] == 1
    assert health["crossed_book_drops"] == 0


def test_kraken_l3_retention_prunes_old_summary_and_event_rows(tmp_path, monkeypatch):
    db = str(tmp_path / "kraken_l3.sqlite3")
    monkeypatch.setenv("Q15_KRAKEN_L3_RAW_EVENTS_ENABLED", "true")
    feed = KrakenL3Collector(symbols=["XBT/USD"], db_path=db)
    feed.summary_levels = 1
    feed.depth = 10
    feed.summary_retention_days = 1.0 / 86400.0
    feed.raw_event_retention_days = 1.0 / 86400.0

    feed.handle_message(json.dumps({
        "channel": "level3",
        "type": "snapshot",
        "data": [{
            "symbol": "XBT/USD",
            "checksum": 100,
            "bids": [{"order_id": "bid1", "limit_price": 100.0, "order_qty": 1.0}],
            "asks": [{"order_id": "ask1", "limit_price": 101.0, "order_qty": 1.0}],
        }],
    }))
    feed.handle_message(json.dumps({
        "channel": "level3",
        "type": "update",
        "data": [{
            "symbol": "XBT/USD",
            "checksum": 101,
            "bids": [{"order_id": "bid2", "limit_price": 100.5, "order_qty": 0.5}],
        }],
    }))
    assert feed.record_once() == 1

    conn = sqlite3.connect(db)
    try:
        old = time.time() - 120.0
        conn.execute("UPDATE kraken_l3_summaries SET created_at = ?", (old,))
        conn.execute("UPDATE kraken_l3_events SET created_at = ?", (old,))
        conn.commit()
    finally:
        conn.close()

    feed._last_prune_at = 0.0
    assert feed.record_once() == 1

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT count(*) FROM kraken_l3_summaries").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM kraken_l3_events").fetchone()[0] == 0
    finally:
        conn.close()


def test_kraken_l3_health_reports_missing_credentials(monkeypatch):
    monkeypatch.setenv("Q15_KRAKEN_L3_ENABLED", "true")
    monkeypatch.delenv("KRAKEN_API_KEY", raising=False)
    monkeypatch.delenv("KRAKEN_API_SECRET", raising=False)
    monkeypatch.delenv("KRAKEN_SPOT_API_KEY", raising=False)
    monkeypatch.delenv("KRAKEN_SPOT_API_SECRET", raising=False)

    health = kraken_l3_health()

    assert health["enabled"] is True
    assert health["authenticated"] is False
    assert health["status"] == "missing_credentials"
