"""Point-in-time independent-venue microstructure for exact RTI research.

The reader reconstructs Coinbase L2 and Kraken L3 state only from local rows
created at or before the immutable exact-13M cutoff.  Kraken signed flow uses
only observable L3 ``modify`` quantity reductions; ambiguous deletes are never
treated as trades.  This module captures features only and cannot fit, notify,
promote, or trade.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping


SCHEMA_VERSION = "rti-independent-venue-microstructure-v2"
TIME_BASIS = "local_created_at"
KRAKEN_PARTIAL_FILL_FLOW_SCHEMA_VERSION = "kraken-l3-partial-fill-flow-v1"
HORIZON_SECONDS = 60

_ASSET_SYMBOLS = {
    "BTC": ("BTC-USD", "BTC/USD"),
    "ETH": ("ETH-USD", "ETH/USD"),
    "SOL": ("SOL-USD", "SOL/USD"),
    "XRP": ("XRP-USD", "XRP/USD"),
    "DOGE": ("DOGE-USD", "DOGE/USD"),
    "BNB": ("BNB-USD", "BNB/USD"),
    "HYPE": ("HYPE-USD", "HYPE/USD"),
}

_COMMON_METRICS = (
    "summary_level_limit",
    "spread_bps",
    "depth_imbalance",
    "bid_notional_levels",
    "ask_notional_levels",
)
_COINBASE_ACTIVITY = (
    "update_count_15s",
    "remove_count_15s",
    "update_count_60s",
    "remove_count_60s",
)
_KRAKEN_ACTIVITY = (
    "add_count_15s",
    "delete_count_15s",
    "add_count_60s",
    "delete_count_60s",
    "trade_count_60s",
    "matched_buy_notional_60s",
    "matched_sell_notional_60s",
    "partial_fill_flow_schema_version",
)
_PERSISTED_VENUE_FIELDS = (
    "status", "missing_reason", "symbol", "snapshot_created_at",
    "snapshot_age_seconds", "message_age_seconds", "spread_bps",
    "summary_level_limit",
    "depth_imbalance", "bid_notional_levels", "ask_notional_levels",
    "start_created_at_60s", "start_age_seconds_60s",
    "start_message_age_seconds_60s", "start_spread_bps_60s",
    "start_summary_level_limit_60s",
    "start_depth_imbalance_60s", "start_bid_notional_levels_60s",
    "start_ask_notional_levels_60s",
)


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _max_lag_seconds() -> float:
    try:
        return max(
            1.0,
            float(os.environ.get("Q15_RTI_CROSS_VENUE_MAX_LAG_SECONDS", "10")),
        )
    except (TypeError, ValueError):
        return 10.0


def _nearest(
    conn: sqlite3.Connection,
    *,
    table: str,
    symbol_column: str,
    symbol: str,
    cutoff: float,
    columns: tuple[str, ...],
) -> dict[str, Any] | None:
    available = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
    }
    required = {
        "created_at", symbol_column, "last_message_age_seconds", *columns,
    }
    if not required.issubset(available):
        return None
    selected = ("created_at", "last_message_age_seconds", *columns)
    row = conn.execute(
        f"SELECT {','.join(selected)} FROM {table} "
        f"WHERE {symbol_column}=? AND created_at<=? "
        "ORDER BY created_at DESC LIMIT 1",
        (symbol, cutoff),
    ).fetchone()
    return dict(row) if row is not None else None


def _endpoint(
    row: Mapping[str, Any] | None,
    *,
    cutoff: float,
    max_lag: float,
    metric_columns: tuple[str, ...],
) -> tuple[dict[str, Any] | None, str | None]:
    if not row:
        return None, "snapshot_or_schema_missing"
    created_at = _num(row.get("created_at"))
    message_age = _num(row.get("last_message_age_seconds"))
    if created_at is None or message_age is None or message_age < 0.0:
        return None, "timestamp_or_transport_invalid"
    lag = cutoff - created_at
    effective_age = max(0.0, lag) + message_age
    if lag < -1e-6:
        return None, "snapshot_after_cutoff"
    if lag > max_lag:
        return None, "snapshot_stale"
    if effective_age > max_lag:
        return None, "transport_stale"
    out: dict[str, Any] = {
        "created_at": created_at,
        "lag_seconds": max(0.0, lag),
        "effective_message_age_seconds": effective_age,
    }
    for key in metric_columns:
        value = row.get(key)
        if key == "partial_fill_flow_schema_version":
            out[key] = str(value) if value is not None else None
            continue
        number = _num(value)
        if number is None:
            return None, f"metric_invalid:{key}"
        out[key] = number
    spread = float(out["spread_bps"])
    imbalance = float(out["depth_imbalance"])
    bid_notional = float(out["bid_notional_levels"])
    ask_notional = float(out["ask_notional_levels"])
    if not 0.0 <= spread <= 10_000.0:
        return None, "spread_invalid"
    if not -1.0 <= imbalance <= 1.0:
        return None, "imbalance_invalid"
    if bid_notional < 0.0 or ask_notional < 0.0 or (
        bid_notional + ask_notional <= 0.0
    ):
        return None, "depth_notional_invalid"
    return out, None


def _venue(
    *,
    db_path: str,
    table: str,
    symbol_column: str,
    symbol: str,
    captured_at: float,
    max_lag: float,
    activity_columns: tuple[str, ...],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "missing",
        "missing_reason": None,
        "symbol": symbol,
        "snapshot_created_at": None,
        "snapshot_age_seconds": None,
        "message_age_seconds": None,
        "start_created_at_60s": None,
        "start_age_seconds_60s": None,
        "start_message_age_seconds_60s": None,
    }
    for key in _COMMON_METRICS:
        result[key] = None
        result[f"start_{key}_60s"] = None
    for key in activity_columns:
        result[key] = None
    if not Path(db_path).exists():
        result["missing_reason"] = "database_missing"
        return result
    current_columns = (*_COMMON_METRICS, *activity_columns)
    try:
        conn = sqlite3.connect(db_path, timeout=2.0)
        conn.row_factory = sqlite3.Row
        try:
            end, reason = _endpoint(
                _nearest(
                    conn,
                    table=table,
                    symbol_column=symbol_column,
                    symbol=symbol,
                    cutoff=captured_at,
                    columns=current_columns,
                ),
                cutoff=captured_at,
                max_lag=max_lag,
                metric_columns=current_columns,
            )
            start = None
            if reason is None and end is not None:
                start, start_reason = _endpoint(
                    _nearest(
                        conn,
                        table=table,
                        symbol_column=symbol_column,
                        symbol=symbol,
                        cutoff=captured_at - HORIZON_SECONDS,
                        columns=_COMMON_METRICS,
                    ),
                    cutoff=captured_at - HORIZON_SECONDS,
                    max_lag=max_lag,
                    metric_columns=_COMMON_METRICS,
                )
                if start_reason is not None:
                    reason = f"start_60s_{start_reason}"
        finally:
            conn.close()
    except sqlite3.Error as exc:
        result["missing_reason"] = f"query_error:{type(exc).__name__}"
        return result
    if reason is not None or end is None or start is None:
        result["missing_reason"] = reason or "endpoint_missing"
        return result
    if end.get("summary_level_limit") != 10.0 or (
        start.get("summary_level_limit") != 10.0
    ):
        result["missing_reason"] = "depth_level_limit_mismatch"
        return result
    result.update({
        "status": "ok",
        "missing_reason": None,
        "snapshot_created_at": end["created_at"],
        "snapshot_age_seconds": end["lag_seconds"],
        "message_age_seconds": end["effective_message_age_seconds"],
        "start_created_at_60s": start["created_at"],
        "start_age_seconds_60s": start["lag_seconds"],
        "start_message_age_seconds_60s": (
            start["effective_message_age_seconds"]
        ),
    })
    for key in _COMMON_METRICS:
        result[key] = end[key]
        result[f"start_{key}_60s"] = start[key]
    for key in activity_columns:
        result[key] = end[key]
    return result


def _share(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator > 0.0 else 0.0


def capture_rti_independent_microstructure(
    asset: str,
    *,
    captured_at: float,
    coinbase_db: str | None = None,
    kraken_db: str | None = None,
    max_lag_seconds: float | None = None,
) -> dict[str, Any]:
    """Freeze independent venue book/activity state at the exact cutoff."""
    captured = _num(captured_at)
    max_lag = _max_lag_seconds() if max_lag_seconds is None else max(
        1.0, float(max_lag_seconds)
    )
    prefix = "rti_independent_microstructure"
    out: dict[str, Any] = {
        f"{prefix}_schema_version": SCHEMA_VERSION,
        f"{prefix}_time_basis": TIME_BASIS,
        f"{prefix}_status": "missing",
        f"{prefix}_missing_reason": None,
        f"{prefix}_evidence_cutoff_at": captured,
        f"{prefix}_max_lag_seconds": max_lag,
        f"{prefix}_available_count": 0,
        f"{prefix}_mean_depth_imbalance": None,
        f"{prefix}_depth_imbalance_disagreement": None,
        f"{prefix}_mean_depth_imbalance_change_60s": None,
        f"{prefix}_mean_spread_bps": None,
        f"{prefix}_max_spread_bps": None,
        f"{prefix}_coinbase_remove_share_15s": None,
        f"{prefix}_kraken_delete_share_15s": None,
        f"{prefix}_kraken_partial_fill_aggressor_imbalance_60s": None,
        f"{prefix}_kraken_partial_fill_notional_60s": None,
        f"{prefix}_kraken_partial_fill_observed_60s": None,
    }
    for venue in ("coinbase", "kraken"):
        for key in _PERSISTED_VENUE_FIELDS:
            out[f"{prefix}_{venue}_{key}"] = None
    for key in _COINBASE_ACTIVITY:
        out[f"{prefix}_coinbase_{key}"] = None
    for key in _KRAKEN_ACTIVITY:
        out[f"{prefix}_kraken_{key}"] = None

    symbols = _ASSET_SYMBOLS.get(str(asset or "").upper())
    if captured is None or symbols is None:
        out[f"{prefix}_missing_reason"] = "invalid_asset_or_timestamp"
        return out
    venues = {
        "coinbase": _venue(
            db_path=coinbase_db or os.environ.get(
                "Q15_COINBASE_ADV_L2_DB", "data/q15_coinbase_adv_l2_v1.sqlite3"
            ),
            table="coinbase_adv_l2_snapshots",
            symbol_column="product_id",
            symbol=symbols[0],
            captured_at=captured,
            max_lag=max_lag,
            activity_columns=_COINBASE_ACTIVITY,
        ),
        "kraken": _venue(
            db_path=kraken_db or os.environ.get(
                "Q15_KRAKEN_L3_DB", "data/q15_kraken_l3_v1.sqlite3"
            ),
            table="kraken_l3_summaries",
            symbol_column="symbol",
            symbol=symbols[1],
            captured_at=captured,
            max_lag=max_lag,
            activity_columns=_KRAKEN_ACTIVITY,
        ),
    }
    for venue, values in venues.items():
        for key, value in values.items():
            out[f"{prefix}_{venue}_{key}"] = value
    available = sum(row.get("status") == "ok" for row in venues.values())
    out[f"{prefix}_available_count"] = available
    if available != 2:
        out[f"{prefix}_missing_reason"] = ";".join(
            f"{venue}:{row.get('missing_reason') or row.get('status')}"
            for venue, row in venues.items()
            if row.get("status") != "ok"
        )
        return out
    cb = venues["coinbase"]
    kr = venues["kraken"]
    if kr.get("partial_fill_flow_schema_version") != (
        KRAKEN_PARTIAL_FILL_FLOW_SCHEMA_VERSION
    ):
        out[f"{prefix}_missing_reason"] = "kraken_partial_fill_schema_mismatch"
        return out
    numeric_activity = (
        *(cb.get(key) for key in _COINBASE_ACTIVITY),
        *(kr.get(key) for key in _KRAKEN_ACTIVITY if key != "partial_fill_flow_schema_version"),
    )
    if any(_num(value) is None or float(value) < 0.0 for value in numeric_activity):
        out[f"{prefix}_missing_reason"] = "activity_metric_invalid"
        return out
    cb_imb = float(cb["depth_imbalance"])
    kr_imb = float(kr["depth_imbalance"])
    cb_imb_start = float(cb["start_depth_imbalance_60s"])
    kr_imb_start = float(kr["start_depth_imbalance_60s"])
    cb_spread = float(cb["spread_bps"])
    kr_spread = float(kr["spread_bps"])
    cb_updates = float(cb["update_count_15s"])
    cb_removes = float(cb["remove_count_15s"])
    kr_adds = float(kr["add_count_15s"])
    kr_deletes = float(kr["delete_count_15s"])
    matched_bid = float(kr["matched_buy_notional_60s"])
    matched_ask = float(kr["matched_sell_notional_60s"])
    partial_total = matched_bid + matched_ask
    out.update({
        f"{prefix}_status": "ok",
        f"{prefix}_missing_reason": None,
        f"{prefix}_mean_depth_imbalance": (cb_imb + kr_imb) / 2.0,
        f"{prefix}_depth_imbalance_disagreement": abs(cb_imb - kr_imb),
        f"{prefix}_mean_depth_imbalance_change_60s": (
            ((cb_imb - cb_imb_start) + (kr_imb - kr_imb_start)) / 2.0
        ),
        f"{prefix}_mean_spread_bps": (cb_spread + kr_spread) / 2.0,
        f"{prefix}_max_spread_bps": max(cb_spread, kr_spread),
        f"{prefix}_coinbase_remove_share_15s": _share(
            cb_removes, cb_updates
        ),
        f"{prefix}_kraken_delete_share_15s": _share(
            kr_deletes, kr_adds + kr_deletes
        ),
        # A filled resting ask implies aggressive buy flow; a filled resting
        # bid implies aggressive sell flow.
        f"{prefix}_kraken_partial_fill_aggressor_imbalance_60s": (
            _share(matched_ask - matched_bid, partial_total)
        ),
        f"{prefix}_kraken_partial_fill_notional_60s": partial_total,
        f"{prefix}_kraken_partial_fill_observed_60s": (
            1.0 if partial_total > 0.0 else 0.0
        ),
    })
    return out


PERSISTED_KEYS = (
    "rti_independent_microstructure_schema_version",
    "rti_independent_microstructure_time_basis",
    "rti_independent_microstructure_status",
    "rti_independent_microstructure_missing_reason",
    "rti_independent_microstructure_evidence_cutoff_at",
    "rti_independent_microstructure_max_lag_seconds",
    "rti_independent_microstructure_available_count",
    "rti_independent_microstructure_mean_depth_imbalance",
    "rti_independent_microstructure_depth_imbalance_disagreement",
    "rti_independent_microstructure_mean_depth_imbalance_change_60s",
    "rti_independent_microstructure_mean_spread_bps",
    "rti_independent_microstructure_max_spread_bps",
    "rti_independent_microstructure_coinbase_remove_share_15s",
    "rti_independent_microstructure_kraken_delete_share_15s",
    "rti_independent_microstructure_kraken_partial_fill_aggressor_imbalance_60s",
    "rti_independent_microstructure_kraken_partial_fill_notional_60s",
    "rti_independent_microstructure_kraken_partial_fill_observed_60s",
    *(
        f"rti_independent_microstructure_{venue}_{key}"
        for venue in ("coinbase", "kraken")
        for key in _PERSISTED_VENUE_FIELDS
    ),
    *(
        f"rti_independent_microstructure_coinbase_{key}"
        for key in _COINBASE_ACTIVITY
    ),
    *(
        f"rti_independent_microstructure_kraken_{key}"
        for key in _KRAKEN_ACTIVITY
    ),
)
