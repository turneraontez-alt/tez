"""Kraken L3 enrichment for V3 strategy-bot rows.

Kraken L3 is order-level book churn, not executed trade flow. This module stamps
compact nearest-summary features onto V3 decisions so the combined model can use
it as cross-exchange confirmation/warning against Coinbase L2.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.environ.get(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


def _path(name: str, default: str) -> str:
    return os.environ.get(name) or default


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _asset(row: Mapping[str, Any]) -> str:
    return str(row.get("asset") or "").upper()


def _symbol(asset: str) -> str | None:
    return {
        "BTC": "BTC/USD",
        "ETH": "ETH/USD",
        "SOL": "SOL/USD",
        "XRP": "XRP/USD",
        "DOGE": "DOGE/USD",
        "BNB": "BNB/USD",
        "HYPE": "HYPE/USD",
    }.get(asset)


def _connect_readonly(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def _nearest_row(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    timestamp: float,
) -> tuple[dict[str, Any] | None, float | None]:
    # Keep V3 decision features strictly point-in-time. A later L3 summary can
    # contain book churn that was not available when the alert fired.
    row = conn.execute(
        "SELECT * FROM kraken_l3_summaries WHERE symbol=? AND created_at<=? "
        "ORDER BY created_at DESC LIMIT 1",
        (symbol, timestamp),
    ).fetchone()
    if row is None:
        return None, None
    age = max(0.0, timestamp - float(row["created_at"]))
    return dict(row), age


def enrich_kraken_l3(row: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(row)
    if not _bool("Q15_V3_KRAKEN_L3_ENABLED", True):
        out.setdefault("kraken_l3_status", "disabled")
        out.setdefault("kraken_l3_missing_reason", "v3_kraken_l3_disabled")
        return out

    timestamp = _num(out.get("created_at"))
    symbol = _symbol(_asset(out))
    if timestamp is None or symbol is None:
        out["kraken_l3_status"] = "missing"
        out["kraken_l3_missing_reason"] = "missing_timestamp_or_symbol"
        return out

    db = _path("Q15_KRAKEN_L3_DB", "data/q15_kraken_l3_v1.sqlite3")
    out["kraken_l3_symbol"] = symbol
    if not Path(db).exists():
        out["kraken_l3_status"] = "missing"
        out["kraken_l3_missing_reason"] = "kraken_l3_db_missing"
        return out

    max_age = _float_env("Q15_V3_KRAKEN_L3_MAX_AGE_SECONDS", 20.0, minimum=0.1)
    try:
        conn = _connect_readonly(db)
        try:
            snapshot, age = _nearest_row(conn, symbol=symbol, timestamp=timestamp)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        out["kraken_l3_status"] = "error"
        out["kraken_l3_missing_reason"] = f"kraken_l3_query_error:{type(exc).__name__}"
        return out

    if snapshot is None or age is None:
        out["kraken_l3_status"] = "missing"
        out["kraken_l3_missing_reason"] = "kraken_l3_snapshot_missing"
        return out
    out["kraken_l3_age_seconds"] = age
    out["kraken_l3_snapshot_created_at"] = snapshot.get("created_at")
    if age > max_age:
        out["kraken_l3_status"] = "stale"
        out["kraken_l3_missing_reason"] = "kraken_l3_snapshot_gt_max_age"
        return out

    best_bid = _num(snapshot.get("best_bid"))
    best_ask = _num(snapshot.get("best_ask"))
    if best_bid is not None and best_ask is not None and best_bid >= best_ask:
        out["kraken_l3_status"] = "invalid"
        out["kraken_l3_missing_reason"] = "kraken_l3_crossed_book_snapshot"
        out["kraken_l3_best_bid"] = best_bid
        out["kraken_l3_best_ask"] = best_ask
        return out

    out.update({
        "kraken_l3_status": "ok",
        "kraken_l3_missing_reason": None,
        "kraken_l3_checksum": snapshot.get("checksum"),
        "kraken_l3_last_message_age_seconds": snapshot.get("last_message_age_seconds"),
        "kraken_l3_best_bid": snapshot.get("best_bid"),
        "kraken_l3_best_ask": snapshot.get("best_ask"),
        "kraken_l3_mid": snapshot.get("mid"),
        "kraken_l3_spread_bps": snapshot.get("spread_bps"),
        "kraken_l3_bid_order_count": snapshot.get("bid_order_count"),
        "kraken_l3_ask_order_count": snapshot.get("ask_order_count"),
        "kraken_l3_bid_level_count": snapshot.get("bid_level_count"),
        "kraken_l3_ask_level_count": snapshot.get("ask_level_count"),
        "kraken_l3_bid_depth_top": snapshot.get("bid_depth_top"),
        "kraken_l3_ask_depth_top": snapshot.get("ask_depth_top"),
        "kraken_l3_bid_depth_levels": snapshot.get("bid_depth_levels"),
        "kraken_l3_ask_depth_levels": snapshot.get("ask_depth_levels"),
        "kraken_l3_bid_notional_levels": snapshot.get("bid_notional_levels"),
        "kraken_l3_ask_notional_levels": snapshot.get("ask_notional_levels"),
        "kraken_l3_bid_order_count_levels": snapshot.get("bid_order_count_levels"),
        "kraken_l3_ask_order_count_levels": snapshot.get("ask_order_count_levels"),
        "kraken_l3_avg_bid_order_size_levels": snapshot.get("avg_bid_order_size_levels"),
        "kraken_l3_avg_ask_order_size_levels": snapshot.get("avg_ask_order_size_levels"),
        "kraken_l3_depth_imbalance": snapshot.get("depth_imbalance"),
        "kraken_l3_add_count_5s": snapshot.get("add_count_5s"),
        "kraken_l3_update_count_5s": snapshot.get("update_count_5s"),
        "kraken_l3_delete_count_5s": snapshot.get("delete_count_5s"),
        "kraken_l3_trade_count_5s": snapshot.get("trade_count_5s"),
        "kraken_l3_cancel_to_add_5s": snapshot.get("cancel_to_add_5s"),
        "kraken_l3_add_count_15s": snapshot.get("add_count_15s"),
        "kraken_l3_update_count_15s": snapshot.get("update_count_15s"),
        "kraken_l3_delete_count_15s": snapshot.get("delete_count_15s"),
        "kraken_l3_trade_count_15s": snapshot.get("trade_count_15s"),
        "kraken_l3_cancel_to_add_15s": snapshot.get("cancel_to_add_15s"),
        "kraken_l3_add_count_60s": snapshot.get("add_count_60s"),
        "kraken_l3_update_count_60s": snapshot.get("update_count_60s"),
        "kraken_l3_delete_count_60s": snapshot.get("delete_count_60s"),
        "kraken_l3_trade_count_60s": snapshot.get("trade_count_60s"),
        "kraken_l3_cancel_to_add_60s": snapshot.get("cancel_to_add_60s"),
        "kraken_l3_matched_buy_notional_60s": snapshot.get("matched_buy_notional_60s"),
        "kraken_l3_matched_sell_notional_60s": snapshot.get("matched_sell_notional_60s"),
        "kraken_l3_net_matched_buy_notional_60s": snapshot.get("net_matched_buy_notional_60s"),
    })
    return out
