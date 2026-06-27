from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from spot_depth import SpotDepthRecorder, _configured_assets, spot_depth_health


def _row(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM spot_depth_snapshots").fetchone()
    finally:
        conn.close()


def test_coinbase_depth_and_trade_are_recorded(tmp_path):
    db = str(tmp_path / "depth.sqlite3")
    feed = SpotDepthRecorder(assets=["BTC"], db_path=db)

    feed._handle_coinbase(json.dumps({
        "type": "snapshot",
        "product_id": "BTC-USD",
        "bids": [["100.0", "2.0"], ["99.5", "3.0"]],
        "asks": [["100.5", "1.5"], ["101.0", "3.5"]],
    }))
    feed._handle_coinbase(json.dumps({
        "type": "match",
        "product_id": "BTC-USD",
        "side": "buy",
        "price": "100.25",
        "size": "0.4",
    }))

    assert feed.record_once() == 1
    row = _row(db)
    assert row["asset"] == "BTC"
    assert row["provider"] == "coinbase"
    assert row["best_bid"] == 100.0
    assert row["best_ask"] == 100.5
    assert row["bid_depth_levels"] == 5.0
    assert row["ask_depth_levels"] == 5.0
    assert row["trade_buy_qty_60s"] == 0.4
    assert row["trade_sell_qty_60s"] == 0.0
    assert row["last_trade_side"] == "buy"


def test_okx_depth_trade_order_count_and_health(tmp_path):
    db = str(tmp_path / "depth.sqlite3")
    feed = SpotDepthRecorder(assets=["HYPE"], db_path=db)
    now_ms = str(int(time.time() * 1000))

    feed._handle_okx(json.dumps({
        "arg": {"channel": "books5", "instId": "HYPE-USDT"},
        "data": [{
            "ts": now_ms,
            "bids": [["40.0", "10.0", "0", "2"], ["39.9", "5.0", "0", "1"]],
            "asks": [["40.1", "8.0", "0", "3"], ["40.2", "7.0", "0", "1"]],
        }],
    }))
    feed._handle_okx(json.dumps({
        "arg": {"channel": "trades", "instId": "HYPE-USDT"},
        "data": [{"ts": now_ms, "side": "sell", "px": "40.05", "sz": "1.25"}],
    }))

    assert feed.record_once() == 1
    row = _row(db)
    assert row["asset"] == "HYPE"
    assert row["provider"] == "okx"
    assert row["trade_sell_qty_60s"] == 1.25
    assert row["trade_net_qty_60s"] == -1.25
    assert row["depth_imbalance"] == 0.0
    bids = json.loads(row["bid_levels_json"])
    assert bids[0] == [40.0, 10.0, 2.0]
    health = feed.health()
    assert health["records_written"] == 1
    assert health["book_age_seconds"]["HYPE"] >= 0.0


def test_spot_depth_health_is_enabled_by_default(monkeypatch):
    monkeypatch.delenv("Q15_SPOT_DEPTH_ENABLED", raising=False)
    health = spot_depth_health()
    assert health["enabled"] is True
    assert "spot_depth" in health["db_path"]


def test_spot_depth_custom_asset_list_keeps_btc(monkeypatch):
    monkeypatch.setenv("Q15_SPOT_DEPTH_ASSETS", "ETH,SOL,XRP,BNB")
    assert _configured_assets() == ["BTC", "ETH", "SOL", "XRP", "BNB"]


def test_spot_depth_can_be_explicitly_disabled(monkeypatch):
    monkeypatch.setenv("Q15_SPOT_DEPTH_ENABLED", "false")
    health = spot_depth_health()
    assert health["enabled"] is False
