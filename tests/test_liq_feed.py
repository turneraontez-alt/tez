import json
import sqlite3
import time

from liq_feed import LiquidationFeed
from q15_upgrade.ultoim_v2.ledger import UltoimV2Ledger


def _msg(symbol="BTCUSDT", side="SELL", price="100.0", qty="2.0", ts_ms=1_000_000):
    return json.dumps({
        "stream": f"{symbol.lower()}@forceOrder",
        "data": {
            "e": "forceOrder",
            "E": ts_ms,
            "o": {
                "s": symbol,
                "S": side,
                "ap": price,
                "q": qty,
                "z": qty,
                "T": ts_ms,
            },
        },
    })


def test_liq_feed_parses_records_and_rolls_60s(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_FEED_LIQ", "true")
    feed = LiquidationFeed(db_path=str(tmp_path / "liq.sqlite3"), symbols={"BTC": "BTCUSDT"})
    base = time.time()
    row = feed.handle_message(_msg(ts_ms=int(base * 1000)))
    assert row["asset"] == "BTC"
    assert row["side"] == "SELL"
    assert row["notional"] == 200.0
    snap = feed.snapshot("BTC", now=base)
    assert snap["liq_notional_60s"] == 200.0
    assert snap["liq_side"] == "SELL"

    assert feed._drain_once_for_tests() is True
    conn = sqlite3.connect(str(tmp_path / "liq.sqlite3"))
    try:
        stored = conn.execute(
            "SELECT asset,symbol,side,price,qty,notional,event_age_s FROM liquidation_events"
        ).fetchone()
    finally:
        conn.close()
    assert stored[:6] == ("BTC", "BTCUSDT", "SELL", 100.0, 2.0, 200.0)
    assert stored[6] >= 0


def test_liq_feed_uses_dominant_rolling_side(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_FEED_LIQ", "true")
    feed = LiquidationFeed(db_path=str(tmp_path / "liq.sqlite3"), symbols={"BTC": "BTCUSDT"})
    base = time.time()
    feed.handle_message(_msg(side="BUY", price="100", qty="1", ts_ms=int(base * 1000)))
    feed.handle_message(_msg(side="SELL", price="100", qty="3", ts_ms=int((base + 1) * 1000)))
    snap = feed.snapshot("BTC", now=base + 1)
    assert snap["liq_side"] == "SELL"
    assert snap["liq_notional_60s"] == 300.0


def test_liq_feed_stale_returns_nulls(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_FEED_LIQ", "true")
    monkeypatch.setenv("Q15_FEED_LIQ_STALE_SECONDS", "5")
    feed = LiquidationFeed(db_path=str(tmp_path / "liq.sqlite3"), symbols={"BTC": "BTCUSDT"})
    base = time.time()
    feed.handle_message(_msg(ts_ms=int(base * 1000)))
    assert feed.snapshot("BTC", now=base + 10.0) == {
        "liq_notional_60s": None,
        "liq_side": None,
        "liq_age_s": None,
    }


def test_liq_feed_disabled_context_is_null(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_FEED_LIQ", "false")
    feed = LiquidationFeed(db_path=str(tmp_path / "liq.sqlite3"), symbols={"BTC": "BTCUSDT"})
    assert feed.enabled is False


def test_ultoim_v2_liq_columns_migrate_and_record(tmp_path):
    led = UltoimV2Ledger(str(tmp_path / "u2.sqlite3"))
    cols = {row["name"] for row in led._conn.execute("PRAGMA table_info(ultoim_v2_predictions)")}
    assert {"liq_notional_60s", "liq_side", "liq_age_s"}.issubset(cols)
    row = {
        "created_at": 1000.0,
        "model_version": "test",
        "asset": "BTC",
        "ticker": "KXBTC",
        "interval": "10M",
        "window_key": 1,
        "mark_seconds": 600.0,
        "fired": 0,
        "close_time": 2000.0,
        "delivery_status": "RECORDED",
        "liq_notional_60s": 200.0,
        "liq_side": "SELL",
        "liq_age_s": 1.0,
    }
    assert led.record_decision(row) is not None
    stored = led._conn.execute(
        "SELECT liq_notional_60s,liq_side,liq_age_s FROM ultoim_v2_predictions"
    ).fetchone()
    assert tuple(stored) == (200.0, "SELL", 1.0)
