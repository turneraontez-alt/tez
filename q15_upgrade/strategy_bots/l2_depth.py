"""Coinbase L2 depth enrichment for V3 strategy-bot rows.

This keeps raw 250-level JSON in the Coinbase collector DB and stamps compact,
decision-time features onto V3 rows.
"""
from __future__ import annotations

import bisect
import json
import math
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping


BANDS = (1, 5, 12, 25, 50, 60, 100, 250)


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


def _side(row: Mapping[str, Any]) -> str | None:
    side = str(
        row.get("predicted_side")
        or row.get("predicted_outcome")
        or row.get("selected_side")
        or row.get("side")
        or ""
    ).upper()
    return side if side in {"YES", "NO"} else None


def _product_id(asset: str) -> str | None:
    if not asset:
        return None
    mapped = {
        "BTC": "BTC-USD",
        "ETH": "ETH-USD",
        "SOL": "SOL-USD",
        "XRP": "XRP-USD",
        "DOGE": "DOGE-USD",
        "BNB": "BNB-USD",
        "HYPE": "HYPE-USD",
    }
    return mapped.get(asset)


def _parse_levels(raw: Any) -> list[tuple[float, float]]:
    if not raw:
        return []
    try:
        arr = json.loads(str(raw))
    except Exception:
        return []
    out: list[tuple[float, float]] = []
    for item in arr:
        try:
            price = float(item[0])
            qty = float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if price > 0 and qty >= 0 and math.isfinite(price) and math.isfinite(qty):
            out.append((price, qty))
    return out


def _imbalance(left: float, right: float) -> float | None:
    denom = left + right
    return (left - right) / denom if denom > 0 else None


def _remove_rate(update_count: Any, remove_count: Any) -> float | None:
    updates = _num(update_count)
    removes = _num(remove_count)
    if updates is None or removes is None:
        return None
    total = updates + removes
    return removes / total if total > 0 else None


def _band(levels: list[tuple[float, float]], n: int) -> tuple[float, float, int]:
    selected = levels[:n]
    qty = sum(qty for _, qty in selected)
    notional = sum(price * qty for price, qty in selected)
    return qty, notional, len(selected)


def _connect_readonly(path: str) -> sqlite3.Connection:
    # Plain read/write open is more tolerant on Windows when a WAL writer is live.
    conn = sqlite3.connect(path, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def _nearest_row(
    conn: sqlite3.Connection,
    *,
    table: str,
    where_col: str,
    where_value: str,
    timestamp: float,
) -> tuple[dict[str, Any] | None, float | None]:
    # Decision-time enrichment must be point-in-time: only snapshots already
    # recorded at or before the alert timestamp are admissible.
    row = conn.execute(
        f"SELECT * FROM {table} WHERE {where_col}=? AND created_at<=? "
        "ORDER BY created_at DESC LIMIT 1",
        (where_value, timestamp),
    ).fetchone()
    if row is None:
        return None, None
    age = max(0.0, timestamp - float(row["created_at"]))
    return dict(row), age


def _target_row(row: Mapping[str, Any], timestamp: float) -> tuple[dict[str, Any] | None, float | None]:
    db = _path("Q15_CHALLENGER_SHADOW_DB", "data/q15_challenger_shadow_v1.sqlite3")
    if not Path(db).exists():
        return None, None
    asset = _asset(row)
    ticker = str(row.get("ticker") or "")
    if not asset or not ticker:
        return None, None
    try:
        conn = _connect_readonly(db)
        try:
            row = conn.execute(
                "SELECT created_at, asset, contract, spot, target, seconds_remaining "
                "FROM shadow_predictions WHERE asset=? AND contract=? AND target IS NOT NULL "
                "AND created_at<=? ORDER BY created_at DESC LIMIT 1",
                (asset, ticker, timestamp),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None, None
    if row is None:
        return None, None
    age = max(0.0, timestamp - float(row["created_at"]))
    return dict(row), age


def _target_metrics(
    row: Mapping[str, Any],
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    *,
    mid: float | None,
    target: float | None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "coinbase_l2_target_price": target,
        "coinbase_l2_distance_to_target": None,
        "coinbase_l2_distance_to_target_pct": None,
        "coinbase_l2_distance_to_target_bps": None,
        "coinbase_l2_up_to_target_notional": None,
        "coinbase_l2_up_to_target_levels": None,
        "coinbase_l2_up_to_target_visible": None,
        "coinbase_l2_down_to_target_notional": None,
        "coinbase_l2_down_to_target_levels": None,
        "coinbase_l2_down_to_target_visible": None,
        "coinbase_l2_easier_side": None,
        "coinbase_l2_easier_side_notional": None,
        "coinbase_l2_harder_side_notional": None,
        "coinbase_l2_target_notional_ratio": None,
        "coinbase_l2_target_side_flow_15s": None,
        "coinbase_l2_target_side_flow_60s": None,
        "coinbase_l2_flow_needed_seconds_15s": None,
        "coinbase_l2_flow_needed_seconds_60s": None,
    }
    if mid is None or target is None or mid <= 0 or target <= 0:
        return out

    distance = mid - target
    out["coinbase_l2_distance_to_target"] = distance
    out["coinbase_l2_distance_to_target_pct"] = distance / target * 100.0
    out["coinbase_l2_distance_to_target_bps"] = distance / target * 10_000.0

    if mid < target:
        up_levels = [(price, qty) for price, qty in asks if price <= target]
        up_visible = bool(asks and target <= asks[-1][0])
        up_notional = sum(price * qty for price, qty in up_levels)
        down_notional = 0.0
        down_levels = 0
        down_visible = True
    elif mid > target:
        down_levels_raw = [(price, qty) for price, qty in bids if price >= target]
        down_visible = bool(bids and target >= bids[-1][0])
        down_notional = sum(price * qty for price, qty in down_levels_raw)
        down_levels = len(down_levels_raw)
        up_notional = 0.0
        up_levels = []
        up_visible = True
    else:
        up_notional = down_notional = 0.0
        up_levels = []
        down_levels = 0
        up_visible = down_visible = True

    out["coinbase_l2_up_to_target_notional"] = up_notional
    out["coinbase_l2_up_to_target_levels"] = len(up_levels)
    out["coinbase_l2_up_to_target_visible"] = 1 if up_visible else 0
    out["coinbase_l2_down_to_target_notional"] = down_notional
    out["coinbase_l2_down_to_target_levels"] = down_levels
    out["coinbase_l2_down_to_target_visible"] = 1 if down_visible else 0

    if up_notional <= down_notional:
        easier, easier_notional, harder_notional = "YES", up_notional, down_notional
    else:
        easier, easier_notional, harder_notional = "NO", down_notional, up_notional
    out["coinbase_l2_easier_side"] = easier
    out["coinbase_l2_easier_side_notional"] = easier_notional
    out["coinbase_l2_harder_side_notional"] = harder_notional
    if easier_notional > 0:
        out["coinbase_l2_target_notional_ratio"] = harder_notional / easier_notional

    side = _side(row)
    if side == "YES":
        needed = up_notional
        visible = up_visible
    elif side == "NO":
        needed = down_notional
        visible = down_visible
    else:
        needed = None
        visible = False

    flow15 = _num(row.get("spot_depth_trade_net_notional_15s"))
    flow60 = _num(row.get("spot_depth_trade_net_notional_60s"))
    side_flow15 = None
    side_flow60 = None
    if side == "YES":
        side_flow15 = flow15 if flow15 is not None and flow15 > 0 else None
        side_flow60 = flow60 if flow60 is not None and flow60 > 0 else None
    elif side == "NO":
        side_flow15 = abs(flow15) if flow15 is not None and flow15 < 0 else None
        side_flow60 = abs(flow60) if flow60 is not None and flow60 < 0 else None
    out["coinbase_l2_target_side_flow_15s"] = side_flow15
    out["coinbase_l2_target_side_flow_60s"] = side_flow60
    if needed is not None and needed <= 0:
        out["coinbase_l2_flow_needed_seconds_15s"] = 0.0
        out["coinbase_l2_flow_needed_seconds_60s"] = 0.0
    elif visible and needed is not None:
        if side_flow15 and side_flow15 > 0:
            out["coinbase_l2_flow_needed_seconds_15s"] = needed / (side_flow15 / 15.0)
        if side_flow60 and side_flow60 > 0:
            out["coinbase_l2_flow_needed_seconds_60s"] = needed / (side_flow60 / 60.0)
    return out


def enrich_coinbase_l2(row: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(row)
    if not _bool("Q15_V3_COINBASE_L2_ENABLED", True):
        out.setdefault("coinbase_l2_status", "disabled")
        out.setdefault("coinbase_l2_missing_reason", "v3_coinbase_l2_disabled")
        return out

    timestamp = _num(out.get("created_at"))
    asset = _asset(out)
    product_id = _product_id(asset)
    if timestamp is None or product_id is None:
        out["coinbase_l2_status"] = "missing"
        out["coinbase_l2_missing_reason"] = "missing_timestamp_or_product"
        return out

    db = _path("Q15_COINBASE_ADV_L2_DB", "data/q15_coinbase_adv_l2_v1.sqlite3")
    if not Path(db).exists():
        out["coinbase_l2_status"] = "missing"
        out["coinbase_l2_missing_reason"] = "coinbase_l2_db_missing"
        out["coinbase_l2_product_id"] = product_id
        return out

    max_age = _float_env("Q15_V3_COINBASE_L2_MAX_AGE_SECONDS", 10.0, minimum=0.1)
    try:
        conn = _connect_readonly(db)
        try:
            snapshot, age = _nearest_row(
                conn,
                table="coinbase_adv_l2_snapshots",
                where_col="product_id",
                where_value=product_id,
                timestamp=timestamp,
            )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        out["coinbase_l2_status"] = "error"
        out["coinbase_l2_missing_reason"] = f"coinbase_l2_query_error:{type(exc).__name__}"
        out["coinbase_l2_product_id"] = product_id
        return out

    out["coinbase_l2_product_id"] = product_id
    if snapshot is None or age is None:
        out["coinbase_l2_status"] = "missing"
        out["coinbase_l2_missing_reason"] = "coinbase_l2_snapshot_missing"
        return out
    if age > max_age:
        out["coinbase_l2_status"] = "stale"
        out["coinbase_l2_missing_reason"] = "coinbase_l2_snapshot_gt_max_age"
        out["coinbase_l2_age_seconds"] = age
        out["coinbase_l2_snapshot_created_at"] = snapshot.get("created_at")
        return out

    bids = _parse_levels(snapshot.get("bid_levels_json"))
    asks = _parse_levels(snapshot.get("ask_levels_json"))
    if not bids or not asks:
        out["coinbase_l2_status"] = "missing"
        out["coinbase_l2_missing_reason"] = "coinbase_l2_levels_missing"
        out["coinbase_l2_age_seconds"] = age
        out["coinbase_l2_snapshot_created_at"] = snapshot.get("created_at")
        return out

    out.update({
        "coinbase_l2_status": "ok",
        "coinbase_l2_missing_reason": None,
        "coinbase_l2_age_seconds": age,
        "coinbase_l2_snapshot_created_at": snapshot.get("created_at"),
        "coinbase_l2_last_message_age_seconds": snapshot.get("last_message_age_seconds"),
        "coinbase_l2_best_bid": snapshot.get("best_bid"),
        "coinbase_l2_best_ask": snapshot.get("best_ask"),
        "coinbase_l2_mid": snapshot.get("mid"),
        "coinbase_l2_spread_bps": snapshot.get("spread_bps"),
        "coinbase_l2_bid_level_count": snapshot.get("bid_level_count"),
        "coinbase_l2_ask_level_count": snapshot.get("ask_level_count"),
        "coinbase_l2_stored_bid_level_count": snapshot.get("stored_bid_level_count"),
        "coinbase_l2_stored_ask_level_count": snapshot.get("stored_ask_level_count"),
        "coinbase_l2_update_count_5s": snapshot.get("update_count_5s"),
        "coinbase_l2_remove_count_5s": snapshot.get("remove_count_5s"),
        "coinbase_l2_remove_rate_5s": _remove_rate(
            snapshot.get("update_count_5s"), snapshot.get("remove_count_5s")
        ),
        "coinbase_l2_update_count_15s": snapshot.get("update_count_15s"),
        "coinbase_l2_remove_count_15s": snapshot.get("remove_count_15s"),
        "coinbase_l2_remove_rate_15s": _remove_rate(
            snapshot.get("update_count_15s"), snapshot.get("remove_count_15s")
        ),
        "coinbase_l2_update_count_60s": snapshot.get("update_count_60s"),
        "coinbase_l2_remove_count_60s": snapshot.get("remove_count_60s"),
        "coinbase_l2_remove_rate_60s": _remove_rate(
            snapshot.get("update_count_60s"), snapshot.get("remove_count_60s")
        ),
    })

    for band in BANDS:
        bid_qty, bid_notional, _ = _band(bids, band)
        ask_qty, ask_notional, _ = _band(asks, band)
        prefix = f"coinbase_l2_top_{band}"
        out[f"{prefix}_bid_qty"] = bid_qty
        out[f"{prefix}_ask_qty"] = ask_qty
        out[f"{prefix}_bid_notional"] = bid_notional
        out[f"{prefix}_ask_notional"] = ask_notional
        out[f"{prefix}_imbalance_qty"] = _imbalance(bid_qty, ask_qty)
        out[f"{prefix}_imbalance_notional"] = _imbalance(bid_notional, ask_notional)

    target, target_age = _target_row(out, timestamp)
    if target is not None:
        out["coinbase_l2_target_source"] = "challenger_shadow"
        out["coinbase_l2_target_age_seconds"] = target_age
    else:
        out["coinbase_l2_target_source"] = None
        out["coinbase_l2_target_age_seconds"] = None
    out.update(_target_metrics(
        out,
        bids,
        asks,
        mid=_num(out.get("coinbase_l2_mid")),
        target=_num(target.get("target")) if target else None,
    ))
    return out
