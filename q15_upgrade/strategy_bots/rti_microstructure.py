"""Deterministic point-in-time features for the preregistered RTI v4 study.

This module contains feature construction only.  It has no settlement query,
model fitting, notification, order, or promotion path.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from typing import Any, Mapping

from .rti_probability import (
    FEATURE_NAMES as BASE_FEATURE_NAMES,
    feature_vector as base_feature_vector,
)


FEATURE_SCHEMA_VERSION = "rti-probability-microstructure-features-v1"
MODEL_FAMILY = "regularized_market_prior_residual_logit"
DESIGN_ID = "q15-rti-market-residual-microstructure-v1"
DESIGN_SHA256 = "a192895fa61bf365eff21062e47d9dbfd5674020f2fe7213ff85992dada67e61"
SOURCE_SCHEMA = "rti-exact-microstructure-v1"
NON_BTC_ASSETS = ("BNB", "DOGE", "ETH", "HYPE", "SOL", "XRP")
EXPECTED_ASSETS = frozenset({"BTC", *NON_BTC_ASSETS})
FEATURE_NAMES = (
    "final_side_yes",
    "yes_signed_distance_bps",
    "yes_side_move_bps",
    "yes_acceleration_bps",
    "log1p_path_range_bps",
    "log1p_realized_volatility_bps",
    "trend_efficiency",
    "yes_persistence_signal",
    "log1p_strike_crossings",
    "seconds_since_crossing_fraction",
    "yes_distance_to_remaining_volatility",
    "spot_imbalance",
    "spread_cents",
    "market_distance_from_half",
    *(f"asset_{asset.lower()}" for asset in NON_BTC_ASSETS),
    "kalshi_yes_microprice_edge_cents",
    "kalshi_book_delta_pressure_yes_5s",
    "kalshi_book_delta_pressure_yes_30s",
    "kalshi_trade_imbalance_yes_5s",
    "kalshi_trade_imbalance_yes_30s",
    "kalshi_taker_imbalance_yes_5s",
    "kalshi_taker_imbalance_yes_30s",
    "kalshi_taker_imbalance_yes_60s",
    "kalshi_best_level_flow_pressure_yes_30s",
    "spot_signed_log_net_notional_15s",
    "spot_signed_log_net_notional_60s",
    "kalshi_microstructure_missing",
    "spot_flow_missing",
)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _profile(row: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = row.get("threshold_json")
    if isinstance(raw, Mapping):
        return raw
    try:
        decoded = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, Mapping) else {}


def _value(row: Mapping[str, Any], profile: Mapping[str, Any], key: str) -> Any:
    value = row.get(key)
    return profile.get(key) if value is None else value


def _neutralized(
    row: Mapping[str, Any],
    profile: Mapping[str, Any],
    key: str,
    *,
    low: float,
    high: float,
) -> tuple[float, bool]:
    value = _num(_value(row, profile, key))
    return (0.0, True) if value is None else (_clip(value, low, high), False)


def _taker_imbalance(
    row: Mapping[str, Any], profile: Mapping[str, Any], horizon: int,
) -> tuple[float, bool]:
    yes = _num(_value(row, profile, f"kalshi_taker_yes_volume_{horizon}s"))
    no = _num(_value(row, profile, f"kalshi_taker_no_volume_{horizon}s"))
    if yes is None or no is None:
        return 0.0, True
    total = max(0.0, yes) + max(0.0, no)
    if total <= 0.0:
        return 0.0, False
    return _clip((yes - no) / total, -1.0, 1.0), False


def _best_level_flow_pressure(
    row: Mapping[str, Any], profile: Mapping[str, Any], horizon: int,
) -> tuple[float, bool]:
    keys = (
        f"kalshi_yes_best_depletion_{horizon}s",
        f"kalshi_no_best_depletion_{horizon}s",
        f"kalshi_yes_best_refill_{horizon}s",
        f"kalshi_no_best_refill_{horizon}s",
    )
    values = [_num(_value(row, profile, key)) for key in keys]
    if any(value is None for value in values):
        return 0.0, True
    yes_depletion, no_depletion, yes_refill, no_refill = (
        max(0.0, float(value)) for value in values
    )
    total = yes_depletion + no_depletion + yes_refill + no_refill
    if total <= 0.0:
        return 0.0, False
    # YES bid refill and NO bid depletion support YES; the converse supports NO.
    pressure = (
        yes_refill + no_depletion - yes_depletion - no_refill
    ) / total
    return _clip(pressure, -1.0, 1.0), False


def _signed_log1p(value: float | None) -> tuple[float, bool]:
    if value is None:
        return 0.0, True
    bounded = _clip(value, -1_000_000_000_000.0, 1_000_000_000_000.0)
    return math.copysign(math.log1p(abs(bounded)), bounded), False


def feature_vector(row: Mapping[str, Any]) -> dict[str, Any]:
    """Build the pinned 33-feature vector from decision-time evidence only."""
    base = base_feature_vector(row)
    if not base.get("available"):
        return {
            "available": False,
            "error": f"base:{base.get('error') or 'unavailable'}",
        }
    profile = _profile(row)
    base_values = dict(zip(BASE_FEATURE_NAMES, base["features"]))

    micro_values: list[float] = []
    micro_missing = False
    for key, low, high in (
        ("kalshi_yes_microprice_edge_cents", -10.0, 10.0),
        ("kalshi_book_delta_pressure_yes_5s", -1.0, 1.0),
        ("kalshi_book_delta_pressure_yes_30s", -1.0, 1.0),
        ("kalshi_trade_imbalance_yes_5s", -1.0, 1.0),
        ("kalshi_trade_imbalance_yes_30s", -1.0, 1.0),
    ):
        value, missing = _neutralized(row, profile, key, low=low, high=high)
        micro_values.append(value)
        micro_missing = micro_missing or missing
    for horizon in (5, 30, 60):
        value, missing = _taker_imbalance(row, profile, horizon)
        micro_values.append(value)
        micro_missing = micro_missing or missing
    best_flow, best_flow_missing = _best_level_flow_pressure(row, profile, 30)
    micro_values.append(best_flow)
    micro_missing = micro_missing or best_flow_missing

    spot_15, spot_15_missing = _signed_log1p(
        _num(_value(row, profile, "spot_depth_trade_net_notional_15s"))
    )
    spot_60, spot_60_missing = _signed_log1p(
        _num(_value(row, profile, "spot_depth_trade_net_notional_60s"))
    )
    spot_missing = bool(
        spot_15_missing
        or spot_60_missing
        or base_values.get("spot_imbalance_missing", 1.0) >= 0.5
    )

    vector = [
        base_values["final_side_yes"],
        base_values["yes_signed_distance_bps"],
        base_values["yes_side_move_bps"],
        base_values["yes_acceleration_bps"],
        base_values["log1p_path_range_bps"],
        base_values["log1p_realized_volatility_bps"],
        base_values["trend_efficiency"],
        base_values["yes_persistence_signal"],
        base_values["log1p_strike_crossings"],
        base_values["seconds_since_crossing_fraction"],
        base_values["yes_distance_to_remaining_volatility"],
        base_values["spot_imbalance"],
        base_values["spread_cents"],
        base_values["market_distance_from_half"],
        *(base_values[f"asset_{asset.lower()}"] for asset in NON_BTC_ASSETS),
        *micro_values,
        spot_15,
        spot_60,
        1.0 if micro_missing else 0.0,
        1.0 if spot_missing else 0.0,
    ]
    if len(vector) != len(FEATURE_NAMES):
        return {"available": False, "error": "feature_schema_length_mismatch"}
    return {
        **base,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "features": vector,
        "microstructure_missing": micro_missing,
        "spot_flow_missing": spot_missing,
        "design_id": DESIGN_ID,
        "design_sha256": DESIGN_SHA256,
    }


def model_feature_window_coverage(
    rows: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    *,
    feature_builder: Any = feature_vector,
    source_schema: str = SOURCE_SCHEMA,
) -> dict[str, Any]:
    """Count independent seven-asset folds usable by the pinned feature vector."""
    grouped: dict[float, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if str(row.get("bot_name") or "") != "rti_path_13m":
            continue
        if str(row.get("interval") or "").upper() != "13M":
            continue
        if str(row.get("record_kind") or "").upper() != (
            "RTI_PATH_13M_PROSPECTIVE_EXACT"
        ):
            continue
        if row.get("kalshi_microstructure_schema_version") != source_schema:
            continue
        close = _num(row.get("close_time"))
        if close is not None:
            grouped[close].append(row)

    complete_close_times: list[float] = []
    schema_complete_close_times: list[float] = []
    unavailable_rows: list[dict[str, Any]] = []
    timestamp_failures: list[dict[str, Any]] = []
    unusable_windows: list[dict[str, Any]] = []
    for close, window_rows in sorted(grouped.items()):
        assets = {str(row.get("asset") or "").upper() for row in window_rows}
        if len(window_rows) != len(EXPECTED_ASSETS) or assets != EXPECTED_ASSETS:
            continue
        schema_complete_close_times.append(close)
        window_errors = []
        for row in window_rows:
            profile = _profile(row)
            source = _num(row.get("source_captured_at"))
            if source is None:
                source = _num(profile.get("quote_captured_at"))
            captured = _num(_value(
                row, profile, "kalshi_microstructure_captured_at"
            ))
            evidence = _num(row.get("evidence_as_of"))
            if evidence is None:
                evidence = _num(profile.get("rti_evaluated_at"))
            timing_reasons = []
            if source is None or captured is None or evidence is None:
                timing_reasons.append("TIMESTAMP_MISSING")
            else:
                if not 0.0 <= captured - (close - 780.0) <= 2.0:
                    timing_reasons.append("NOT_EXACT_13M")
                if abs(captured - source) > 1e-6:
                    timing_reasons.append("QUOTE_SOURCE_TIMESTAMP_MISMATCH")
                if captured > evidence + 1e-6:
                    timing_reasons.append("EVIDENCE_PRECEDES_CAPTURE")
            if timing_reasons:
                error = {
                    "id": row.get("id"),
                    "close_time": close,
                    "asset": str(row.get("asset") or "").upper(),
                    "error": "timestamp_alignment_failure",
                    "reasons": timing_reasons,
                }
                timestamp_failures.append(error)
                window_errors.append(error)
                continue
            result = feature_builder(row)
            if result.get("available"):
                continue
            error = {
                "id": row.get("id"),
                "close_time": close,
                "asset": str(row.get("asset") or "").upper(),
                "error": str(result.get("error") or "feature_unavailable"),
            }
            unavailable_rows.append(error)
            window_errors.append(error)
        if window_errors:
            unusable_windows.append({
                "close_time": close,
                "unavailable_rows": window_errors,
            })
        else:
            complete_close_times.append(close)
    return {
        "schema_complete_model_candidate_close_windows": len(
            schema_complete_close_times
        ),
        "schema_complete_model_candidate_close_times": (
            schema_complete_close_times
        ),
        "complete_model_feature_close_windows": len(complete_close_times),
        "model_feature_complete_close_times": complete_close_times,
        "model_feature_unavailable_rows": unavailable_rows,
        "model_feature_timestamp_failures": timestamp_failures,
        "unusable_model_feature_close_windows": unusable_windows,
    }
