from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from coinbase_adv_l2 import CoinbaseAdvancedL2Collector, load_cdp_key


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
