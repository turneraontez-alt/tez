"""Q15 V5 data integrity, calibration dedupe, and paper risk helpers."""

from __future__ import annotations

import math
import os


def _num(value, default=None):
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _env_flag(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def latest_trade_ts(trades):
    timestamps = []
    for trade in trades or []:
        ts = _num(trade.get("ts"))
        if ts is not None:
            timestamps.append(ts)
    return max(timestamps) if timestamps else None


def apply_snapshot_freshness(snapshot, raw_result, now, ws_health=None, config=None):
    """Use source timestamps, not local snapshot rebuild time."""
    snap = dict(snapshot)
    raw = raw_result or {}
    health = ws_health or {}
    source = raw.get("source") or "unknown"
    spot = raw.get("spot") or {}

    spot_ts = _num(spot.get("ts"))
    # When the spot feed serves a bounded last-good fallback, spot["ts"] is
    # re-stamped to `now` for candle continuity, which would make a stale
    # underlying read as fresh and slip past the freshness gate below. Honor the
    # original event time so the gate sees the TRUE age (a 30s-old price reads as
    # 30s old, not 0s). Gated so the behavior is reversible; default ON because
    # honest freshness on the price the owner trades on is the safe posture.
    if _env_flag("Q15_V5_GATE_STALE_SPOT", True) and spot.get("stale"):
        original_ts = _num(spot.get("original_ts"))
        if original_ts is not None:
            spot_ts = original_ts
    trade_ts = _num(raw.get("latest_trade_event_ts"))
    if trade_ts is None:
        trade_ts = latest_trade_ts(raw.get("trades"))

    if source == "ws":
        book_ts = _num(raw.get("book_event_ts"))
        if book_ts is None:
            book_ts = _num(health.get("last_orderbook_at"))
        if book_ts is None:
            book_ts = _num(health.get("last_message_at"))
    else:
        book_ts = _num(raw.get("fetch_completed_at"), now)

    spot_age = max(0.0, now - spot_ts) if spot_ts is not None else None
    book_age = max(0.0, now - book_ts) if book_ts is not None else None
    trade_age = max(0.0, now - trade_ts) if trade_ts is not None else None
    required_ages = [age for age in (spot_age, book_age) if age is not None]
    data_age = max(required_ages) if required_ages else 999999.0

    configured_age = getattr(config, "max_data_age_s", None)
    max_age = _num(configured_age, _num(os.environ.get("Q15_V5_MAX_SOURCE_AGE_S"), 5.0))
    reasons = []

    if not spot.get("ok"):
        reasons.append("spot_unavailable")
    if spot_age is None:
        reasons.append("spot_timestamp_missing")
    elif spot_age > max_age:
        reasons.append(f"spot_stale_{spot_age:.1f}s")

    if book_age is None:
        reasons.append("orderbook_timestamp_missing")
    elif book_age > max_age:
        reasons.append(f"orderbook_stale_{book_age:.1f}s")

    yes_bid = _num(snap.get("yes_bid"))
    yes_ask = _num(snap.get("yes_ask"))
    if yes_bid is None or yes_ask is None:
        reasons.append("two_sided_book_missing")
    elif yes_ask < yes_bid:
        reasons.append("crossed_orderbook")

    snap.update({
        "source_mode": source,
        "spot_event_ts": spot_ts,
        "latest_trade_event_ts": trade_ts,
        "orderbook_event_ts": book_ts,
        "spot_age_seconds": round(spot_age, 3) if spot_age is not None else None,
        "trade_age_seconds": round(trade_age, 3) if trade_age is not None else None,
        "orderbook_age_seconds": round(book_age, 3) if book_age is not None else None,
        "data_age_seconds": round(data_age, 3),
        "v5_data_valid": not reasons,
        "v5_data_invalid_reasons": reasons,
    })

    if reasons:
        snap["data_warning"] = ";".join(reasons)
        flags = list(snap.get("flags") or [])
        flags.append("DATA INVALID — " + ", ".join(reasons))
        snap["flags"] = list(dict.fromkeys(flags))
    return snap


def enforce_fail_closed(snapshot):
    """Suppress all alerts when required market data is invalid."""
    snap = dict(snapshot)
    if snap.get("v5_data_valid") is False:
        snap.update({
            "entry_status": "NO ENTRY — DATA INVALID",
            "entry_side": None,
            "decision_state": "NO TRADE",
            "estimated_edge": None,
            "max_entry_price": None,
            "entry_allowed": False,
            "signal_suppressed": True,
            "suppression_reason": "v5_data_invalid",
        })
    return snap


def risk_preview(snapshot):
    """Bankroll-based paper sizing only. No order is ever placed."""
    snap = dict(snapshot)
    bankroll = max(0.0, _num(os.environ.get("Q15_PAPER_BANKROLL"), 1000.0))
    risk_pct = min(0.05, max(0.0001, _num(os.environ.get("Q15_RISK_PER_TRADE_PCT"), 0.005)))
    max_contracts = max(0, int(_num(os.environ.get("Q15_MAX_CONTRACTS_PER_TRADE"), 10)))
    depth_fraction = min(0.50, max(0.01, _num(os.environ.get("Q15_MAX_DEPTH_FRACTION"), 0.10)))

    side = snap.get("entry_side")
    ask = _num(snap.get("entry_ask"))
    fee = _num(snap.get("entry_fee"), 0.0)
    spread = max(0.0, _num(snap.get("spread"), 0.0))
    slippage_reserve = max(0.25, spread * 0.50)
    depth = _num(
        snap.get("yes_ask_qty") if side == "YES" else snap.get("no_ask_qty"),
        0.0,
    )

    risk_budget = bankroll * risk_pct
    if ask is None or ask <= 0 or side not in {"YES", "NO"}:
        contracts = 0
        worst_loss = None
    else:
        per_contract_loss = max(0.01, (ask + fee + slippage_reserve) / 100.0)
        bankroll_cap = int(risk_budget // per_contract_loss)
        liquidity_cap = int(max(0.0, depth * depth_fraction))
        contracts = max(0, min(max_contracts, bankroll_cap, liquidity_cap))
        worst_loss = round(contracts * per_contract_loss, 2)

    snap.update({
        "paper_bankroll": round(bankroll, 2),
        "paper_risk_budget": round(risk_budget, 2),
        "paper_suggested_contracts": contracts,
        "paper_worst_case_loss": worst_loss,
        "paper_only": True,
    })
    return snap


def collapse_learning_rows(rows):
    """Keep one statistically independent outcome per ticker and side."""
    selected = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if row.get("outcome") not in {"win", "loss"}:
            continue
        if row.get("valid_for_learning") is False:
            continue
        ticker = row.get("ticker")
        side = row.get("side")
        if not ticker or side not in {"YES", "NO"}:
            continue

        strategy = str(row.get("strategy") or "")
        priority = (
            0 if strategy == "settlement" and not bool(row.get("shadow"))
            else 1 if strategy == "settlement"
            else 2 if not bool(row.get("shadow"))
            else 3
        )
        timestamp = _num(row.get("decision_timestamp"), 0.0)
        candidate = (priority, timestamp, row)
        key = (ticker, side)
        if key not in selected or candidate[:2] < selected[key][:2]:
            selected[key] = candidate
    return [candidate[2] for candidate in selected.values()]


def current_feature_drift_breaker(engine):
    metrics = getattr(engine, "_drift_metrics", {}) or {}
    config = getattr(engine, "v4", None)
    if config is None or not getattr(config, "circuit_breakers_enabled", False):
        return False
    limit = _num(getattr(config, "max_feature_drift_psi", None), 0.25)
    return _num(metrics.get("max_psi"), 0.0) > limit
