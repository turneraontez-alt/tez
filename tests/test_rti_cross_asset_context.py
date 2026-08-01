from __future__ import annotations

import sqlite3

import pytest

from q15_upgrade.strategy_bots.rti_cross_asset_context import (
    ASSETS,
    PERSISTED_KEYS,
    SCHEMA_VERSION,
    TIME_BASIS,
    capture_rti_cross_asset_context,
)
from q15_upgrade.strategy_bots.rules import SPOT_DEPTH_KEYS


SYMBOLS = {
    "BTC": ("BTC-USD", "BTC/USD"),
    "ETH": ("ETH-USD", "ETH/USD"),
    "SOL": ("SOL-USD", "SOL/USD"),
    "XRP": ("XRP-USD", "XRP/USD"),
    "DOGE": ("DOGE-USD", "DOGE/USD"),
    "BNB": ("BNB-USD", "BNB/USD"),
    "HYPE": ("HYPE-USD", "HYPE/USD"),
}


def _database(path, *, coinbase: bool, omit: str | None = None) -> None:
    table = (
        "coinbase_adv_l2_snapshots" if coinbase else "kraken_l3_summaries"
    )
    symbol_column = "product_id" if coinbase else "symbol"
    symbol_index = 0 if coinbase else 1
    conn = sqlite3.connect(path)
    conn.execute(
        f"CREATE TABLE {table} ("
        "id INTEGER PRIMARY KEY,created_at REAL NOT NULL,"
        f"{symbol_column} TEXT NOT NULL,last_message_age_seconds REAL,"
        "best_bid REAL,best_ask REAL,mid REAL)"
    )
    for index, asset in enumerate(ASSETS):
        if asset == omit:
            continue
        base = 100.0 + index * 10.0
        # Cross-sectional 60-second moves rise monotonically by asset index;
        # 15-second moves have the same ordering at half the magnitude.
        move_60_bps = float(index - 3)
        start_60 = base
        current = start_60 * (1.0 + move_60_bps / 10_000.0)
        start_15 = current / (1.0 + move_60_bps / 20_000.0)
        symbol = SYMBOLS[asset][symbol_index]
        for created_at, price in (
            (939.0, start_60),
            (984.0, start_15),
            (999.0, current),
            # A spectacular future print must never enter the exact cutoff.
            (1000.5, current * 10.0),
        ):
            conn.execute(
                f"INSERT INTO {table} "
                f"(created_at,{symbol_column},last_message_age_seconds,"
                "best_bid,best_ask,mid) VALUES (?,?,?,?,?,?)",
                (created_at, symbol, 0.2, price - 0.01, price + 0.01, price),
            )
    conn.commit()
    conn.close()


def test_cross_asset_context_is_exact_point_in_time_and_auditable(tmp_path):
    coinbase = tmp_path / "coinbase.sqlite3"
    kraken = tmp_path / "kraken.sqlite3"
    _database(coinbase, coinbase=True)
    _database(kraken, coinbase=False)

    row = capture_rti_cross_asset_context(
        "ETH",
        captured_at=1000.0,
        coinbase_db=str(coinbase),
        kraken_db=str(kraken),
        max_lag_seconds=10.0,
    )

    assert row["rti_cross_asset_schema_version"] == SCHEMA_VERSION
    assert row["rti_cross_asset_time_basis"] == TIME_BASIS
    assert row["rti_cross_asset_status"] == "ok"
    assert row["rti_cross_asset_available_asset_count"] == 7
    assert row["rti_cross_asset_latest_snapshot_created_at"] == 999.0
    assert row["rti_cross_asset_latest_start_created_at_60s"] == 939.0
    assert row["rti_cross_asset_max_snapshot_age_seconds"] == 1.0
    assert row["rti_cross_asset_median_momentum_bps_60s"] == pytest.approx(0.0)
    assert row["rti_cross_asset_breadth_signed_60s"] == pytest.approx(0.0)
    assert row["rti_cross_asset_dispersion_mad_bps_60s"] == pytest.approx(2.0)
    assert row["rti_cross_asset_eth_consensus_change_bps_60s"] == pytest.approx(0.0)
    assert row["rti_cross_asset_coinbase_eth_change_bps_60s"] == pytest.approx(0.0)
    assert row["rti_cross_asset_kraken_eth_change_bps_60s"] == pytest.approx(0.0)
    assert row["rti_cross_asset_asset_centered_rank_60s"] == pytest.approx(0.0)
    assert row["rti_cross_asset_asset_btc_direction_agreement_60s"] == 0.5
    assert set(PERSISTED_KEYS).issubset(SPOT_DEPTH_KEYS)


def test_cross_asset_context_fails_closed_when_any_asset_is_missing(tmp_path):
    coinbase = tmp_path / "coinbase.sqlite3"
    kraken = tmp_path / "kraken.sqlite3"
    _database(coinbase, coinbase=True)
    _database(kraken, coinbase=False, omit="HYPE")

    row = capture_rti_cross_asset_context(
        "BTC",
        captured_at=1000.0,
        coinbase_db=str(coinbase),
        kraken_db=str(kraken),
    )

    assert row["rti_cross_asset_status"] == "missing"
    assert row["rti_cross_asset_available_asset_count"] == 0
    assert "kraken:HYPE:current:snapshot_missing" in row[
        "rti_cross_asset_missing_reason"
    ]
