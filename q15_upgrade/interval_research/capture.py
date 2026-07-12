"""Interval-timing research — build a capture row from the champion's analysis.

Pure, read-only field extraction: given the per-asset ``analysis`` dict and its
``canonical`` snapshot (exactly the frozen objects the champion already built),
produce either a capture row for the ledger or a single missing-reason code.
Never mutates the analysis and never influences the prediction.
"""
from __future__ import annotations

from typing import Any, Mapping

from ..lineage import lineage_stamp
from .config import INTERVAL_ROLES


CAPTURE_FEATURE_SCHEMA_VERSION = "interval-capture-v2-drift-evidence"


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _data_quality_status(dq: float | None) -> str:
    if dq is None:
        return "UNKNOWN"
    if dq >= 0.8:
        return "OK"
    if dq >= 0.5:
        return "DEGRADED"
    return "THIN"


def missing_reason(analysis: Mapping[str, Any] | None, canonical: Any,
                   min_data_quality: float) -> str | None:
    """Return the single reason code if this (asset, interval) cannot be captured,
    else None (meaning a capture is possible). Distinguishes 'no directional
    prediction' from 'valid prediction but no economic entry' (the latter is NOT
    a missing capture — it is captured with entry_recommended=0)."""
    if canonical is None or not getattr(canonical, "ticker", None):
        return "CONTRACT_NOT_MAPPED"
    if not (isinstance(analysis, Mapping) and analysis.get("prediction_available")):
        return "MODEL_COULD_NOT_SCORE"
    dq = _num(analysis.get("data_quality"))
    if dq is not None and dq < min_data_quality:
        return "DATA_QUALITY_FAILED"
    return None


def build_capture_row(*, model_version: str, interval: str, mark_seconds: int,
                      asset: str, analysis: Mapping[str, Any], canonical: Any,
                      window_key: int, now: float) -> dict[str, Any]:
    """Build the full capture row (prediction-quality + trade-quality fields).
    Caller has already confirmed ``missing_reason`` returned None."""
    quote = analysis.get("quote") if isinstance(analysis.get("quote"), Mapping) else {}
    manip = analysis.get("manipulation") if isinstance(analysis.get("manipulation"), Mapping) else {}
    flip = analysis.get("flip_risk") if isinstance(analysis.get("flip_risk"), Mapping) else {}
    regime = analysis.get("regime") if isinstance(analysis.get("regime"), Mapping) else {}
    sigs = analysis.get("shadow_signals") if isinstance(analysis.get("shadow_signals"), Mapping) else {}
    costs = analysis.get("costs") if isinstance(analysis.get("costs"), Mapping) else {}
    settlement_index = (
        analysis.get("settlement_index")
        if isinstance(analysis.get("settlement_index"), Mapping)
        else {}
    )

    dq = _num(analysis.get("data_quality"))
    decision = str(analysis.get("trade_decision") or "")
    entry_recommended = 1 if decision == "ENTRY_RECOMMENDED" else 0
    # Executable ask for the PREDICTED side (the champion already computed it);
    # fall back to the raw quote ask if absent.
    entry_ask = _num(analysis.get("entry_ask_cents"))
    if entry_ask is None:
        entry_ask = _num(quote.get("ask_cents"))

    depth = _num(quote.get("ask_depth"))
    if depth is None:
        depth = _num(quote.get("depth_contracts"))

    lineage = lineage_stamp(
        feature_schema_version=CAPTURE_FEATURE_SCHEMA_VERSION,
        config={"interval": interval, "mark_seconds": int(mark_seconds)},
    )
    return {
        "model_version": model_version,
        "ticker": str(canonical.ticker),
        "asset": asset,
        "interval": interval,
        "role": INTERVAL_ROLES.get(interval),
        "mark_seconds": mark_seconds,
        "window_key": window_key,
        "captured_at": now,
        "seconds_remaining": _num(getattr(canonical, "seconds_remaining", None)),
        "close_time": _num(getattr(canonical, "settlement_time", None)),
        # prediction quality
        "prediction_available": 1,
        "predicted_side": str(analysis.get("prediction_side") or "").upper() or None,
        "raw_yes_probability": _num(analysis.get("raw_yes_probability")),
        "calibrated_yes_probability": _num(analysis.get("yes_probability")),
        "conservative_probability": _num(analysis.get("conservative_probability")),
        "flip_probability": _num(flip.get("score")),
        "manipulation_score": _num(manip.get("score")),
        "manipulation_suspected": 1 if manip.get("suspected") else 0,
        "distance_from_strike": _num(regime.get("distance_sigma")),
        "prediction_stability": _num(sigs.get("prediction_stability")),
        "data_quality": dq,
        "data_quality_status": _data_quality_status(dq),
        # trade quality / executable economics
        "yes_bid_cents": _num(quote.get("bid_cents")),
        "yes_ask_cents": _num(quote.get("ask_cents")),
        "spread_cents": _num(quote.get("spread_cents")),
        "depth_contracts": depth,
        "quote_age_seconds": _num(quote.get("quote_age_seconds")),
        "yes_bid_depth_contracts": _num(quote.get("yes_bid_depth_contracts")),
        "yes_ask_depth_contracts": _num(quote.get("yes_ask_depth_contracts")),
        "no_bid_depth_contracts": _num(quote.get("no_bid_depth_contracts")),
        "no_ask_depth_contracts": _num(quote.get("no_ask_depth_contracts")),
        "kalshi_depth_status": quote.get("kalshi_depth_status"),
        "kalshi_depth_missing_reason": quote.get("kalshi_depth_missing_reason"),
        "kalshi_depth_retry_used": 1 if quote.get("kalshi_depth_retry_used") else 0,
        "kalshi_taker_yes_volume_15s": _num(quote.get("kalshi_taker_yes_volume_15s")),
        "kalshi_taker_no_volume_15s": _num(quote.get("kalshi_taker_no_volume_15s")),
        "kalshi_taker_net_yes_volume_15s": _num(
            quote.get("kalshi_taker_net_yes_volume_15s")
        ),
        "entry_ask_cents": entry_ask,
        "net_edge_cents": _num(analysis.get("net_edge_cents")),
        "fee_cents": _num(costs.get("fee_cents")),
        "slippage_cents": _num(costs.get("slippage_cents")),
        "total_cost_cents": _num(costs.get("total_cents")),
        "trade_decision": decision or None,
        "entry_recommended": entry_recommended,
        "entry_reject_reason": (str(analysis.get("main_blocker")) if not entry_recommended
                                and analysis.get("main_blocker") else None),
        "index_px": _num(settlement_index.get("index_px")),
        "basis_cents": _num(settlement_index.get("basis_cents")),
        "index_age_s": _num(settlement_index.get("index_age_s")),
        "index_status": settlement_index.get("index_status"),
        "index_missing_reason": settlement_index.get("index_missing_reason"),
        "index_id": settlement_index.get("index_id"),
        "index_source_ts": _num(settlement_index.get("index_source_ts")),
        **lineage,
        "missing_reason": None,
    }
