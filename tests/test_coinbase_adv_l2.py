from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

import coinbase_adv_l2 as adv_l2
from coinbase_adv_l2 import CoinbaseAdvancedL2Collector, coinbase_adv_l2_health, load_cdp_key


def test_load_cdp_key_accepts_name_or_id(tmp_path):
    named = tmp_path / "named.json"
    named.write_text(json.dumps({"name": "organizations/x/apiKeys/y", "privateKey": "secret"}))
    assert load_cdp_key(str(named)) == ("organizations/x/apiKeys/y", "secret")

    id_key = tmp_path / "id.json"
    id_key.write_text(json.dumps({"id": "key-id", "privateKey": "secret-2"}))
    assert load_cdp_key(str(id_key)) == ("key-id", "secret-2")


def test_l2_snapshot_update_and_record(tmp_path):
    db = str(tmp_path / "adv_l2.sqlite3")
    feed = CoinbaseAdvancedL2Collector(products=["BTC-USD"], db_path=db, key_file=str(tmp_path / "missing.json"))
    feed.summary_levels = 2

    feed.handle_message(json.dumps({
        "channel": "l2_data",
        "sequence_num": 1,
        "events": [{
            "type": "snapshot",
            "product_id": "BTC-USD",
            "updates": [
                {"side": "bid", "price_level": "100.0", "new_quantity": "2.0"},
                {"side": "bid", "price_level": "99.5", "new_quantity": "1.0"},
                {"side": "offer", "price_level": "100.5", "new_quantity": "3.0"},
                {"side": "offer", "price_level": "101.0", "new_quantity": "1.0"},
            ],
        }],
    }))
    feed.handle_message(json.dumps({
        "channel": "l2_data",
        "sequence_num": 2,
        "events": [{
            "type": "update",
            "product_id": "BTC-USD",
            "updates": [
                {"side": "bid", "price_level": "100.25", "new_quantity": "0.5"},
                {"side": "offer", "price_level": "101.0", "new_quantity": "0"},
            ],
        }],
    }))

    assert feed.record_once() == 1

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM coinbase_adv_l2_snapshots").fetchone()
        assert row["product_id"] == "BTC-USD"
        assert row["sequence_num"] == 2
        assert row["best_bid"] == 100.25
        assert row["best_ask"] == 100.5
        assert row["bid_level_count"] == 3
        assert row["ask_level_count"] == 1
        assert row["stored_bid_level_count"] == 2
        assert row["remove_count_60s"] == 1
        assert json.loads(row["bid_levels_json"])[0] == [100.25, 0.5]
    finally:
        conn.close()


def test_l2_retention_prunes_old_rows(tmp_path):
    db = str(tmp_path / "adv_l2.sqlite3")
    feed = CoinbaseAdvancedL2Collector(products=["BTC-USD"], db_path=db, key_file=str(tmp_path / "missing.json"))
    feed.summary_levels = 1
    feed.retention_days = 1.0 / 86400.0

    feed.handle_message(json.dumps({
        "channel": "l2_data",
        "sequence_num": 1,
        "events": [{
            "type": "snapshot",
            "product_id": "BTC-USD",
            "updates": [
                {"side": "bid", "price_level": "100.0", "new_quantity": "1.0"},
                {"side": "offer", "price_level": "101.0", "new_quantity": "1.0"},
            ],
        }],
    }))
    assert feed.record_once() == 1

    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE coinbase_adv_l2_snapshots SET created_at = ?", (time.time() - 120.0,))
        conn.commit()
    finally:
        conn.close()

    feed._last_prune_at = 0.0
    assert feed.record_once() == 1

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT count(*) FROM coinbase_adv_l2_snapshots").fetchone()[0] == 1
    finally:
        conn.close()


def test_health_reports_db_snapshot_age_when_thread_absent(tmp_path, monkeypatch):
    db = str(tmp_path / "adv_l2.sqlite3")
    now = time.time()

    conn = sqlite3.connect(db)
    try:
        conn.executescript(
            "CREATE TABLE coinbase_adv_l2_snapshots ("
            "created_at REAL, product_id TEXT, sequence_num INTEGER, last_message_age_seconds REAL, "
            "best_bid REAL, best_ask REAL, mid REAL, spread_bps REAL, bid_level_count INTEGER, "
            "ask_level_count INTEGER, stored_bid_level_count INTEGER, stored_ask_level_count INTEGER, "
            "bid_depth_levels REAL, ask_depth_levels REAL, bid_notional_levels REAL, "
            "ask_notional_levels REAL, depth_imbalance REAL, bid_levels_json TEXT, "
            "ask_levels_json TEXT, update_count_5s INTEGER, remove_count_5s INTEGER, "
            "update_count_15s INTEGER, remove_count_15s INTEGER, update_count_60s INTEGER, "
            "remove_count_60s INTEGER, snapshot_loaded INTEGER)"
        )
        conn.execute(
            "INSERT INTO coinbase_adv_l2_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                now - 450.0, "BTC-USD", None, None, 1, 2, 1.5, 1, 1, 1, 1, 1,
                1, 1, 1, 1, 0, "[]", "[]", 0, 0, 0, 0, 0, 0, 1,
            ),
        )
        conn.execute(
            "INSERT INTO coinbase_adv_l2_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                now - 30.0, "ETH-USD", None, None, 1, 2, 1.5, 1, 1, 1, 1, 1,
                1, 1, 1, 1, 0, "[]", "[]", 0, 0, 0, 0, 0, 0, 1,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("Q15_COINBASE_ADV_L2_ENABLED", "true")
    monkeypatch.setenv("Q15_COINBASE_ADV_L2_DB", db)
    monkeypatch.setenv("Q15_COINBASE_ADV_L2_PRODUCTS", "BTC-USD,ETH-USD")
    info = coinbase_adv_l2_health()

    assert info["snapshot_age_seconds"] >= 400.0
    assert info["latest_snapshot_age_seconds"] < 120.0
    assert info["snapshot_age_by_product_seconds"]["BTC-USD"] >= 400.0
    assert info["snapshot_age_by_product_seconds"]["ETH-USD"] < 120.0


def test_health_warns_once_when_snapshots_stale(tmp_path, monkeypatch, caplog):
    db = str(tmp_path / "adv_l2.sqlite3")
    now = time.time()
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE coinbase_adv_l2_snapshots (created_at REAL, product_id TEXT)")
        conn.execute("INSERT INTO coinbase_adv_l2_snapshots VALUES (?, ?)", (now - 1200.0, "BTC-USD"))
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("Q15_COINBASE_ADV_L2_ENABLED", "true")
    monkeypatch.setenv("Q15_COINBASE_ADV_L2_DB", db)
    monkeypatch.setenv("Q15_COINBASE_ADV_L2_PRODUCTS", "BTC-USD")
    monkeypatch.setenv("Q15_COINBASE_ADV_L2_STALE_WARNING_SECONDS", "600")
    monkeypatch.setenv("Q15_COINBASE_ADV_L2_STALE_WARNING_COOLDOWN_SECONDS", "600")
    adv_l2._last_stale_warning_at = 0.0
    caplog.set_level("WARNING", logger="coinbase_adv_l2")

    first = adv_l2.coinbase_adv_l2_health()
    second = adv_l2.coinbase_adv_l2_health()

    assert first["snapshot_stale"] is True
    assert first["snapshot_age_seconds"] >= 1000.0
    assert second["snapshot_stale"] is True
    assert caplog.text.count("Coinbase Advanced L2 snapshots stale") == 1


def test_health_reports_missing_coinbase_sdk(monkeypatch):
    monkeypatch.setenv("Q15_COINBASE_ADV_L2_ENABLED", "true")
    monkeypatch.setattr(adv_l2, "_HAVE_COINBASE_SDK", False)
    monkeypatch.setattr(adv_l2, "_COINBASE_SDK_ERROR", "ImportError: missing coinbase")

    info = adv_l2.coinbase_adv_l2_health()

    assert info["have_coinbase_sdk"] is False
    assert info["coinbase_sdk_error"] == "ImportError: missing coinbase"
    assert info["status"] == "coinbase_sdk_missing"
