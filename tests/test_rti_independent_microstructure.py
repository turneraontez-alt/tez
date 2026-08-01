from __future__ import annotations

import sqlite3

import pytest

from q15_upgrade.strategy_bots.rti_independent_microstructure import (
    KRAKEN_PARTIAL_FILL_FLOW_SCHEMA_VERSION,
    PERSISTED_KEYS,
    SCHEMA_VERSION,
    TIME_BASIS,
    capture_rti_independent_microstructure,
)
from q15_upgrade.strategy_bots.rules import SPOT_DEPTH_KEYS


def _coinbase_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE coinbase_adv_l2_snapshots ("
        "id INTEGER PRIMARY KEY, created_at REAL, product_id TEXT, "
        "last_message_age_seconds REAL, spread_bps REAL, "
        "summary_level_limit REAL, "
        "depth_imbalance REAL, bid_notional_levels REAL, "
        "ask_notional_levels REAL, update_count_15s REAL, "
        "remove_count_15s REAL, update_count_60s REAL, "
        "remove_count_60s REAL)"
    )
    rows = (
        (939.0, 1.0, 0.1, 1000.0, 900.0, 80.0, 20.0, 300.0, 80.0),
        (999.0, 2.0, 0.4, 1500.0, 1000.0, 100.0, 25.0, 400.0, 100.0),
        # Must never be used at the 1000.0 evidence cutoff.
        (1000.5, 999.0, -0.9, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0),
    )
    for values in rows:
        conn.execute(
            "INSERT INTO coinbase_adv_l2_snapshots "
            "(created_at,product_id,last_message_age_seconds,summary_level_limit,spread_bps,"
            "depth_imbalance,bid_notional_levels,ask_notional_levels,"
            "update_count_15s,remove_count_15s,update_count_60s,"
            "remove_count_60s) VALUES (?,'BTC-USD',0.2,10,?,?,?,?,?,?,?,?)",
            values,
        )
    conn.commit()
    conn.close()


def _kraken_db(path, *, flow_schema=KRAKEN_PARTIAL_FILL_FLOW_SCHEMA_VERSION) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE kraken_l3_summaries ("
        "id INTEGER PRIMARY KEY, created_at REAL, symbol TEXT, "
        "last_message_age_seconds REAL, spread_bps REAL, "
        "summary_level_limit REAL, "
        "depth_imbalance REAL, bid_notional_levels REAL, "
        "ask_notional_levels REAL, add_count_15s REAL, "
        "delete_count_15s REAL, add_count_60s REAL, delete_count_60s REAL, "
        "trade_count_60s REAL, matched_buy_notional_60s REAL, "
        "matched_sell_notional_60s REAL, "
        "partial_fill_flow_schema_version TEXT)"
    )
    rows = (
        (939.0, 1.0, -0.1, 500.0, 600.0, 20.0, 5.0, 80.0, 20.0, 0.0, 0.0, 0.0),
        (999.0, 4.0, 0.2, 800.0, 700.0, 30.0, 10.0, 120.0, 40.0, 2.0, 20.0, 60.0),
    )
    for values in rows:
        conn.execute(
            "INSERT INTO kraken_l3_summaries "
            "(created_at,symbol,last_message_age_seconds,summary_level_limit,spread_bps,"
            "depth_imbalance,bid_notional_levels,ask_notional_levels,"
            "add_count_15s,delete_count_15s,add_count_60s,delete_count_60s,"
            "trade_count_60s,matched_buy_notional_60s,"
            "matched_sell_notional_60s,partial_fill_flow_schema_version) "
            "VALUES (?,'BTC/USD',0.2,10,?,?,?,?,?,?,?,?,?,?,?,?)",
            (*values, flow_schema),
        )
    conn.commit()
    conn.close()


def test_independent_microstructure_is_point_in_time_and_scale_stable(tmp_path):
    coinbase = tmp_path / "coinbase.sqlite3"
    kraken = tmp_path / "kraken.sqlite3"
    _coinbase_db(coinbase)
    _kraken_db(kraken)

    row = capture_rti_independent_microstructure(
        "BTC",
        captured_at=1000.0,
        coinbase_db=str(coinbase),
        kraken_db=str(kraken),
        max_lag_seconds=10.0,
    )

    prefix = "rti_independent_microstructure"
    assert row[f"{prefix}_schema_version"] == SCHEMA_VERSION
    assert row[f"{prefix}_time_basis"] == TIME_BASIS
    assert row[f"{prefix}_status"] == "ok"
    assert row[f"{prefix}_available_count"] == 2
    assert row[f"{prefix}_coinbase_snapshot_created_at"] == 999.0
    assert row[f"{prefix}_coinbase_spread_bps"] == 2.0
    assert row[f"{prefix}_mean_depth_imbalance"] == pytest.approx(0.3)
    assert row[f"{prefix}_depth_imbalance_disagreement"] == pytest.approx(0.2)
    assert row[f"{prefix}_mean_depth_imbalance_change_60s"] == pytest.approx(0.3)
    assert row[f"{prefix}_mean_spread_bps"] == pytest.approx(3.0)
    assert row[f"{prefix}_max_spread_bps"] == pytest.approx(4.0)
    assert row[f"{prefix}_coinbase_remove_share_15s"] == pytest.approx(0.25)
    assert row[f"{prefix}_kraken_delete_share_15s"] == pytest.approx(0.25)
    assert row[
        f"{prefix}_kraken_partial_fill_aggressor_imbalance_60s"
    ] == pytest.approx(0.5)
    assert row[f"{prefix}_kraken_partial_fill_notional_60s"] == 80.0
    assert row[f"{prefix}_kraken_partial_fill_observed_60s"] == 1.0
    assert row[f"{prefix}_coinbase_summary_level_limit"] == 10.0
    assert row[f"{prefix}_coinbase_start_summary_level_limit_60s"] == 10.0
    assert set(PERSISTED_KEYS).issubset(SPOT_DEPTH_KEYS)
    assert set(row).issubset(PERSISTED_KEYS)


def test_independent_microstructure_rejects_unversioned_flow(tmp_path):
    coinbase = tmp_path / "coinbase.sqlite3"
    kraken = tmp_path / "kraken.sqlite3"
    _coinbase_db(coinbase)
    _kraken_db(kraken, flow_schema="legacy-ambiguous-delete-flow")

    row = capture_rti_independent_microstructure(
        "BTC",
        captured_at=1000.0,
        coinbase_db=str(coinbase),
        kraken_db=str(kraken),
    )

    assert row["rti_independent_microstructure_status"] == "missing"
    assert row["rti_independent_microstructure_missing_reason"] == (
        "kraken_partial_fill_schema_mismatch"
    )


def test_independent_microstructure_rejects_stale_start(tmp_path):
    coinbase = tmp_path / "coinbase.sqlite3"
    kraken = tmp_path / "kraken.sqlite3"
    _coinbase_db(coinbase)
    _kraken_db(kraken)
    conn = sqlite3.connect(kraken)
    conn.execute("DELETE FROM kraken_l3_summaries WHERE created_at < 999.0")
    conn.commit()
    conn.close()

    row = capture_rti_independent_microstructure(
        "BTC",
        captured_at=1000.0,
        coinbase_db=str(coinbase),
        kraken_db=str(kraken),
    )

    assert row["rti_independent_microstructure_status"] == "missing"
    assert "start_60s_snapshot_or_schema_missing" in row[
        "rti_independent_microstructure_missing_reason"
    ]
