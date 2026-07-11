"""Point-in-time spot depth enrichment for strategy-bot decisions.

The source collectors record raw snapshots independently of strategy bots. This
module joins the newest snapshot that already existed at decision time and
normalizes Coinbase's historical maker-side trade labels to aggressor flow.
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


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _invert_side(side: Any) -> str | None:
    text = str(side or "").strip().lower()
    if text == "buy":
        return "sell"
    if text == "sell":
        return "buy"
    return text or None


def _trade_values(snapshot: Mapping[str, Any], suffix: str, invert: bool) -> dict[str, Any]:
    buy_qty = _num(snapshot.get(f"trade_buy_qty_{suffix}"))
    sell_qty = _num(snapshot.get(f"trade_sell_qty_{suffix}"))
    buy_notional = _num(snapshot.get(f"trade_buy_notional_{suffix}"))
    sell_notional = _num(snapshot.get(f"trade_sell_notional_{suffix}"))
    if invert:
        buy_qty, sell_qty = sell_qty, buy_qty
        buy_notional, sell_notional = sell_notional, buy_notional
    return {
        f"spot_depth_trade_buy_qty_{suffix}": buy_qty,
        f"spot_depth_trade_sell_qty_{suffix}": sell_qty,
        f"spot_depth_trade_net_qty_{suffix}": (
            buy_qty - sell_qty if buy_qty is not None and sell_qty is not None else None
        ),
        f"spot_depth_trade_buy_notional_{suffix}": buy_notional,
        f"spot_depth_trade_sell_notional_{suffix}": sell_notional,
        f"spot_depth_trade_net_notional_{suffix}": (
            buy_notional - sell_notional
            if buy_notional is not None and sell_notional is not None
            else None
        ),
    }


def enrich_spot_depth(row: Mapping[str, Any]) -> dict[str, Any]:
    """Stamp a decision row with the freshest admissible actual-coin snapshot."""
    out = dict(row)
    if not _bool("Q15_V3_SPOT_DEPTH_ENRICHMENT_ENABLED", True):
        out["spot_depth_status"] = "disabled"
        out["spot_depth_missing_reason"] = "v3_spot_depth_enrichment_disabled"
        return out

    timestamp = _num(out.get("created_at"))
    asset = str(out.get("asset") or "").upper()
    if timestamp is None or not asset:
        out["spot_depth_status"] = "missing"
        out["spot_depth_missing_reason"] = "missing_timestamp_or_asset"
        return out

    db_path = os.environ.get("Q15_SPOT_DEPTH_DB") or "data/q15_spot_depth_v1.sqlite3"
    if not Path(db_path).exists():
        out["spot_depth_status"] = "missing"
        out["spot_depth_missing_reason"] = "spot_depth_db_missing"
        return out

    try:
        conn = sqlite3.connect(db_path, timeout=2.0)
        conn.row_factory = sqlite3.Row
        try:
            found = conn.execute(
                "SELECT * FROM spot_depth_snapshots WHERE asset=? AND created_at<=? "
                "ORDER BY created_at DESC LIMIT 1",
                (asset, timestamp),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        out["spot_depth_status"] = "error"
        out["spot_depth_missing_reason"] = f"spot_depth_query_error:{type(exc).__name__}"
        return out

    if found is None:
        out["spot_depth_status"] = "missing"
        out["spot_depth_missing_reason"] = "spot_depth_snapshot_missing"
        return out

    snapshot = dict(found)
    snapshot_at = _num(snapshot.get("created_at"))
    snapshot_age = max(0.0, timestamp - snapshot_at) if snapshot_at is not None else None
    source_book_age = _num(snapshot.get("book_age_seconds"))
    source_trade_age = _num(snapshot.get("trade_age_seconds"))
    # Exchange timestamps can lead the local clock by a small amount. A negative
    # source age must never make an older persisted snapshot look fresher at the
    # decision timestamp, so normalize it before computing effective age.
    raw_book_age = max(0.0, source_book_age) if source_book_age is not None else None
    raw_trade_age = max(0.0, source_trade_age) if source_trade_age is not None else None
    effective_book_age = (
        snapshot_age + raw_book_age
        if snapshot_age is not None and raw_book_age is not None
        else snapshot_age
    )
    effective_trade_age = (
        snapshot_age + raw_trade_age
        if snapshot_age is not None and raw_trade_age is not None
        else None
    )
    provider = str(snapshot.get("provider") or "").lower()
    semantics = str(snapshot.get("trade_side_semantics") or "").lower()
    invert = provider == "coinbase" and semantics != "aggressor"

    out.update({
        "spot_depth_source": snapshot.get("source") or snapshot.get("provider"),
        "spot_depth_age_seconds": effective_book_age,
        "spot_depth_trade_age_seconds": effective_trade_age,
        "spot_depth_best_bid": snapshot.get("best_bid"),
        "spot_depth_best_ask": snapshot.get("best_ask"),
        "spot_depth_mid": snapshot.get("mid"),
        "spot_depth_spread_bps": snapshot.get("spread_bps"),
        "spot_depth_bid_depth_top": snapshot.get("bid_depth_top"),
        "spot_depth_ask_depth_top": snapshot.get("ask_depth_top"),
        "spot_depth_bid_depth_levels": snapshot.get("bid_depth_levels"),
        "spot_depth_ask_depth_levels": snapshot.get("ask_depth_levels"),
        "spot_depth_bid_notional_levels": snapshot.get("bid_notional_levels"),
        "spot_depth_ask_notional_levels": snapshot.get("ask_notional_levels"),
        "spot_depth_imbalance": snapshot.get("depth_imbalance"),
        "spot_depth_last_trade_price": snapshot.get("last_trade_price"),
        "spot_depth_last_trade_side": (
            _invert_side(snapshot.get("last_trade_side"))
            if invert
            else snapshot.get("last_trade_side")
        ),
        "spot_depth_last_trade_size": snapshot.get("last_trade_size"),
        # Diagnostic-only fields live in threshold_json even though the compact
        # strategy table stores the effective ages above.
        "spot_depth_snapshot_age_seconds": snapshot_age,
        "spot_depth_raw_book_age_seconds": raw_book_age,
        "spot_depth_raw_trade_age_seconds": raw_trade_age,
        "spot_depth_provider": provider or None,
        "spot_depth_trade_side_semantics": "aggressor" if invert else semantics or None,
    })
    for suffix in ("5s", "15s", "60s"):
        out.update(_trade_values(snapshot, suffix, invert))

    snapshot_max = _float_env("Q15_V3_DRIFT_SPOT_SNAPSHOT_MAX_AGE_SECONDS", 15.0, 0.1)
    book_max = _float_env("Q15_V3_DRIFT_SPOT_BOOK_MAX_AGE_SECONDS", 15.0, 0.1)
    trade_max = _float_env("Q15_V3_DRIFT_SPOT_TRADE_MAX_AGE_SECONDS", 15.0, 0.1)
    stale_reason = None
    if snapshot_age is None or snapshot_age > snapshot_max:
        stale_reason = "spot_depth_snapshot_stale"
    elif raw_book_age is None or effective_book_age is None or effective_book_age > book_max:
        stale_reason = "spot_depth_book_stale"
    elif effective_trade_age is None or effective_trade_age > trade_max:
        stale_reason = "spot_depth_trade_stale"

    buy60 = _num(out.get("spot_depth_trade_buy_notional_60s"))
    sell60 = _num(out.get("spot_depth_trade_sell_notional_60s"))
    if stale_reason:
        out["spot_depth_status"] = "stale"
        out["spot_depth_missing_reason"] = stale_reason
    elif buy60 is None or sell60 is None or buy60 + sell60 <= 0:
        out["spot_depth_status"] = "missing"
        out["spot_depth_missing_reason"] = "spot_depth_60s_trade_flow_missing"
    else:
        out["spot_depth_status"] = "ok"
        out["spot_depth_missing_reason"] = None
    return out
