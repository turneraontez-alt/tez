from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from spot_depth import SpotDepthRecorder, _configured_assets, spot_depth_health
import spot_depth as spot_depth_module


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
    assert row["trade_buy_qty_60s"] == 0.0
    assert row["trade_sell_qty_60s"] == 0.4
    assert row["last_trade_side"] == "sell"
    assert row["trade_side_semantics"] == "aggressor"


def test_live_capture_does_not_wait_for_periodic_database_write(tmp_path):
    db = str(tmp_path / "depth.sqlite3")
    feed = SpotDepthRecorder(assets=["BTC"], db_path=db)
    feed._handle_coinbase(json.dumps({
        "type": "snapshot",
        "product_id": "BTC-USD",
        "bids": [["100.0", "2.0"]],
        "asks": [["100.5", "1.5"]],
    }))

    snapshot = feed.capture_current("BTC")
    assert snapshot is not None
    assert snapshot["best_bid"] == 100.0
    assert snapshot["best_ask"] == 100.5
    assert snapshot["created_at"] <= time.time()
    assert snapshot["spot_mid_path_schema_version"] == "spot-mid-path-local-v1"
    assert snapshot["spot_mid_window_complete_60s"] is False
    assert not os.path.exists(db)


def test_spot_mid_path_is_local_complete_and_excludes_future_rows(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("Q15_SPOT_DEPTH_RECORD_SECONDS", "5")
    clock = [1000.0]
    monkeypatch.setattr(spot_depth_module.time, "time", lambda: clock[0])
    feed = SpotDepthRecorder(
        assets=["BTC"], db_path=str(tmp_path / "mid-path.sqlite3")
    )
    for step in range(13):
        clock[0] = 1000.0 + step * 5.0
        bid = 100.0 + step
        feed._replace_book(
            "BTC",
            provider="coinbase",
            symbol="BTC-USD",
            bids=[[bid, 2.0]],
            asks=[[bid + 1.0, 2.0]],
            ts=clock[0],
        )
        assert feed.record_once() == 1

    # A future row must never enter a decision-time path.
    feed._mid_history["BTC"].append({"created_at": 1065.0, "mid": 999.0})
    snapshot = feed.capture_current("BTC")
    assert snapshot is not None
    assert snapshot["spot_mid_path_time_basis"] == "local_created_at"
    assert snapshot["spot_mid_window_complete_15s"] is True
    assert snapshot["spot_mid_window_complete_60s"] is True
    assert snapshot["spot_mid_path_count_60s"] == 13
    assert snapshot["spot_mid_path_start_at_60s"] == 1000.0
    assert snapshot["spot_mid_path_end_at_60s"] == 1060.0
    assert snapshot["spot_mid_start_60s"] == 100.5
    assert snapshot["spot_mid_end_60s"] == 112.5
    assert snapshot["spot_mid_change_bps_60s"] == pytest.approx(
        (112.5 / 100.5 - 1.0) * 10_000.0
    )
    assert snapshot["spot_mid_range_bps_60s"] > 0.0
    assert snapshot["spot_mid_realized_volatility_bps_60s"] > 0.0
    assert snapshot["spot_mid_trend_efficiency_60s"] == 1.0
    assert snapshot["spot_mid_path_max_gap_seconds_60s"] == 5.0
    assert feed.health()["mid_history_rows"]["BTC"] == 14
    feed.close()


def test_spot_mid_path_fails_closed_on_restart_or_continuity_gap(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("Q15_SPOT_DEPTH_RECORD_SECONDS", "5")
    clock = [2000.0]
    monkeypatch.setattr(spot_depth_module.time, "time", lambda: clock[0])
    feed = SpotDepthRecorder(
        assets=["BTC"], db_path=str(tmp_path / "mid-gap.sqlite3")
    )
    feed._replace_book(
        "BTC", provider="coinbase", symbol="BTC-USD",
        bids=[[100.0, 1.0]], asks=[[101.0, 1.0]], ts=clock[0],
    )
    assert feed.record_once() == 1
    clock[0] = 2060.0
    feed._replace_book(
        "BTC", provider="coinbase", symbol="BTC-USD",
        bids=[[101.0, 1.0]], asks=[[102.0, 1.0]], ts=clock[0],
    )
    snapshot = feed.capture_current("BTC")
    assert snapshot is not None
    assert snapshot["spot_mid_window_complete_60s"] is False
    assert "INSUFFICIENT_PATH_SAMPLES" in (
        snapshot["spot_mid_path_missing_reason_60s"]
    )
    assert "PATH_CONTINUITY_GAP" in (
        snapshot["spot_mid_path_missing_reason_60s"]
    )
    feed.close()


def test_coinbase_last_match_and_errors_are_recorded(tmp_path):
    db = str(tmp_path / "depth.sqlite3")
    feed = SpotDepthRecorder(assets=["BTC"], db_path=db)

    feed._handle_coinbase(json.dumps({
        "type": "error",
        "message": "Failed to subscribe",
        "reason": "level2 requires authentication",
    }))
    assert "requires authentication" in feed.health()["last_error"]["coinbase_message"]

    feed._handle_coinbase(json.dumps({
        "type": "snapshot",
        "product_id": "BTC-USD",
        "bids": [["100.0", "2.0"]],
        "asks": [["100.5", "1.5"]],
    }))
    feed._handle_coinbase(json.dumps({
        "type": "last_match",
        "product_id": "BTC-USD",
        "side": "sell",
        "price": "100.25",
        "size": "0.2",
    }))

    assert feed.record_once() == 1
    row = _row(db)
    assert row["trade_buy_qty_60s"] == 0.2
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


def test_spot_depth_prunes_rows_outside_retention(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_SPOT_DEPTH_RETENTION_DAYS", "1")
    db = str(tmp_path / "depth.sqlite3")
    feed = SpotDepthRecorder(assets=["BTC"], db_path=db)
    conn = feed._connect()
    conn.execute(
        "INSERT INTO spot_depth_snapshots "
        "(created_at, asset, provider, symbol, source) VALUES (?,?,?,?,?)",
        (time.time() - 172800.0, "BTC", "coinbase", "BTC-USD", "old"),
    )
    conn.commit()
    feed._handle_coinbase(json.dumps({
        "type": "snapshot",
        "product_id": "BTC-USD",
        "bids": [["100.0", "2.0"]],
        "asks": [["100.5", "1.5"]],
    }))

    assert feed.record_once() == 1
    rows = conn.execute(
        "SELECT source FROM spot_depth_snapshots ORDER BY created_at"
    ).fetchall()
    assert [row[0] for row in rows] == ["coinbase BTC-USD"]
    assert feed.health()["records_pruned"] == 1
    feed.close()
