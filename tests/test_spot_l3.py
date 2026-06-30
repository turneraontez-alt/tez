from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from spot_l3 import (
    CoinbaseL3Collector,
    _configured_products,
    coinbase_l3_subscribe_payload,
    spot_l3_health,
)


def test_coinbase_l3_subscribe_payload_is_authenticated(monkeypatch):
    secret = base64.b64encode(b"secret-bytes").decode("ascii")
    monkeypatch.setenv("COINBASE_EXCHANGE_API_KEY", "key-1")
    monkeypatch.setenv("COINBASE_EXCHANGE_API_SECRET", secret)
    monkeypatch.setenv("COINBASE_EXCHANGE_API_PASSPHRASE", "pass-1")

    payload = coinbase_l3_subscribe_payload(["ETH-USD", "BTC-USD"], timestamp="123.45")
    expected = base64.b64encode(
        hmac.new(b"secret-bytes", b"123.45GET/users/self/verify", hashlib.sha256).digest()
    ).decode("ascii")

    assert payload["type"] == "subscribe"
    assert payload["channels"] == ["level3"]
    assert payload["product_ids"] == ["BTC-USD", "ETH-USD"]
    assert payload["key"] == "key-1"
    assert payload["passphrase"] == "pass-1"
    assert payload["signature"] == expected


def test_coinbase_l3_products_filter_to_coinbase_symbols(monkeypatch):
    monkeypatch.setenv("Q15_SPOT_L3_PRODUCTS", "BTC-USD,BNB-USDT,SOL-USD,HYPE-USDT")
    assert _configured_products() == ["BTC-USD", "SOL-USD"]


def test_coinbase_l3_book_summary_and_raw_events(tmp_path, monkeypatch):
    db = str(tmp_path / "l3.sqlite3")
    monkeypatch.setenv("Q15_SPOT_L3_RAW_EVENTS_ENABLED", "true")
    feed = CoinbaseL3Collector(products=["BTC-USD"], db_path=db)
    feed.summary_levels = 2

    feed._replace_product_book(
        "BTC-USD",
        sequence=10,
        bids=[["100.0", "1.0", "bid1"], ["99.5", "2.0", "bid2"]],
        asks=[["101.0", "1.5", "ask1"], ["101.5", "1.0", "ask2"]],
    )
    feed.handle_coinbase_l3('["open","BTC-USD","11","bid3","buy","100.5","0.5","2026-01-01T00:00:00Z"]')
    feed.handle_coinbase_l3('["match","BTC-USD","12","ask1","taker1","101.0","0.25","buy","2026-01-01T00:00:01Z"]')
    feed.handle_coinbase_l3('["done","BTC-USD","13","bid1","buy","100.0","1.0","canceled","2026-01-01T00:00:02Z"]')
    feed.handle_coinbase_l3('["change","BTC-USD","14","ask2","sell","101.5","1.0","0.25","2026-01-01T00:00:03Z"]')

    assert feed.record_once() == 1

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM spot_l3_summaries").fetchone()
        assert row["product_id"] == "BTC-USD"
        assert row["sequence"] == 14
        assert row["best_bid"] == 100.5
        assert row["best_ask"] == 101.0
        assert row["bid_order_count"] == 2
        assert row["ask_order_count"] == 2
        assert row["open_count_60s"] == 1
        assert row["done_count_60s"] == 1
        assert row["cancel_count_60s"] == 1
        assert row["cancel_to_open_60s"] == 1.0
        assert row["change_count_60s"] == 1
        assert row["match_count_60s"] == 1
        assert row["matched_buy_notional_60s"] == 25.25
        assert conn.execute("SELECT count(*) FROM spot_l3_events").fetchone()[0] == 4
    finally:
        conn.close()


def test_spot_l3_health_reports_missing_credentials(monkeypatch):
    monkeypatch.setenv("Q15_SPOT_L3_ENABLED", "true")
    for name in (
        "COINBASE_EXCHANGE_API_KEY",
        "COINBASE_EXCHANGE_API_SECRET",
        "COINBASE_EXCHANGE_API_PASSPHRASE",
        "COINBASE_API_KEY",
        "COINBASE_API_SECRET",
        "COINBASE_API_PASSPHRASE",
    ):
        monkeypatch.delenv(name, raising=False)

    health = spot_l3_health()
    assert health["enabled"] is True
    assert health["authenticated"] is False
    assert health["status"] == "missing_credentials"
