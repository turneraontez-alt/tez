from __future__ import annotations

import sqlite3

import pytest

from q15_upgrade.strategy_bots.rti_cross_venue import (
    SCHEMA_VERSION,
    TIME_BASIS,
    capture_rti_cross_venue,
)


def _database(path, *, coinbase: bool, future: bool = False) -> None:
    conn = sqlite3.connect(path)
    if coinbase:
        table = "coinbase_adv_l2_snapshots"
        symbol_column = "product_id"
        symbol = "BTC-USD"
    else:
        table = "kraken_l3_summaries"
        symbol_column = "symbol"
        symbol = "BTC/USD"
    conn.execute(
        f"CREATE TABLE {table} ("
        "id INTEGER PRIMARY KEY, created_at REAL NOT NULL, "
        f"{symbol_column} TEXT NOT NULL, last_message_age_seconds REAL, "
        "best_bid REAL, best_ask REAL, mid REAL)"
    )
    prices = (100.0, 101.0, 102.0) if coinbase else (200.0, 201.0, 202.0)
    for created_at, price in zip((939.0, 984.0, 999.0), prices):
        conn.execute(
            f"INSERT INTO {table} "
            f"(created_at,{symbol_column},last_message_age_seconds,best_bid,best_ask,mid) "
            "VALUES (?,?,?,?,?,?)",
            (created_at, symbol, 0.2, price - 0.1, price + 0.1, price),
        )
    if future:
        conn.execute(
            f"INSERT INTO {table} "
            f"(created_at,{symbol_column},last_message_age_seconds,best_bid,best_ask,mid) "
            "VALUES (?,?,?,?,?,?)",
            (1000.5, symbol, 0.0, 999.0, 1001.0, 1000.0),
        )
    conn.commit()
    conn.close()


def test_cross_venue_is_point_in_time_and_excludes_future_rows(tmp_path):
    coinbase = tmp_path / "coinbase.sqlite3"
    kraken = tmp_path / "kraken.sqlite3"
    _database(coinbase, coinbase=True, future=True)
    _database(kraken, coinbase=False, future=True)

    result = capture_rti_cross_venue(
        "BTC",
        captured_at=1000.0,
        primary_mid=150.0,
        primary_change_bps_15s=75.0,
        primary_change_bps_60s=150.0,
        primary_source="test-primary",
        coinbase_db=str(coinbase),
        kraken_db=str(kraken),
        max_lag_seconds=10.0,
    )

    assert result["rti_cross_venue_schema_version"] == SCHEMA_VERSION
    assert result["rti_cross_venue_time_basis"] == TIME_BASIS
    assert result["rti_cross_venue_status"] == "ok"
    assert result["rti_cross_venue_available_count"] == 2
    assert result["rti_cross_venue_coinbase_snapshot_created_at"] == 999.0
    assert result["rti_cross_venue_kraken_snapshot_created_at"] == 999.0
    assert result["rti_cross_venue_coinbase_mid"] == 102.0
    assert result["rti_cross_venue_kraken_mid"] == 202.0
    assert result["rti_cross_venue_consensus_mid"] == 152.0
    assert result["rti_cross_venue_current_divergence_bps"] == pytest.approx(
        100.0 / 152.0 * 10_000.0
    )
    assert result["rti_cross_venue_coinbase_start_created_at_60s"] == 939.0
    assert result["rti_cross_venue_kraken_start_created_at_60s"] == 939.0
    assert result["rti_independent_microstructure_schema_version"] == (
        "rti-independent-venue-microstructure-v2"
    )
    # This legacy-minimal fixture intentionally lacks depth/activity columns;
    # the new capture fails closed without changing the frozen price consensus.
    assert result["rti_independent_microstructure_status"] == "missing"


def test_cross_venue_fails_closed_when_a_start_endpoint_is_stale(tmp_path):
    coinbase = tmp_path / "coinbase.sqlite3"
    kraken = tmp_path / "kraken.sqlite3"
    _database(coinbase, coinbase=True)
    _database(kraken, coinbase=False)
    conn = sqlite3.connect(kraken)
    conn.execute(
        "DELETE FROM kraken_l3_summaries WHERE created_at < 984.0"
    )
    conn.commit()
    conn.close()

    result = capture_rti_cross_venue(
        "BTC",
        captured_at=1000.0,
        primary_mid=150.0,
        primary_change_bps_15s=1.0,
        primary_change_bps_60s=2.0,
        coinbase_db=str(coinbase),
        kraken_db=str(kraken),
        max_lag_seconds=10.0,
    )

    assert result["rti_cross_venue_status"] == "missing"
    assert result["rti_cross_venue_available_count"] == 1
    assert result["rti_cross_venue_kraken_missing_reason"] == (
        "start_60s_snapshot_missing"
    )
    assert "kraken:start_60s_snapshot_missing" in result[
        "rti_cross_venue_missing_reason"
    ]


def test_cross_venue_requires_primary_decision_time_path(tmp_path):
    coinbase = tmp_path / "coinbase.sqlite3"
    kraken = tmp_path / "kraken.sqlite3"
    _database(coinbase, coinbase=True)
    _database(kraken, coinbase=False)

    result = capture_rti_cross_venue(
        "BTC",
        captured_at=1000.0,
        primary_mid=150.0,
        primary_change_bps_15s=1.0,
        primary_change_bps_60s=None,
        coinbase_db=str(coinbase),
        kraken_db=str(kraken),
    )

    assert result["rti_cross_venue_status"] == "missing"
    assert result["rti_cross_venue_missing_reason"] == (
        "primary_spot_context_missing"
    )
    assert result["rti_cross_venue_available_count"] == 2
    assert result["rti_independent_venue_status"] == "ok"
    assert result["rti_independent_venue_available_count"] == 2
    assert result["rti_independent_venue_consensus_mid"] == 152.0
