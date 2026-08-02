"""Outcome-blind V16 feature construction on top of frozen V15 evidence.

The first 25 values are byte-for-byte V15.  Five fixed asset indicators and
fifteen bounded interactions encode reversal mechanisms named before any V16
population outcomes may be opened.  This module cannot read labels, fit,
score, notify, promote, or trade.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from . import rti_microstructure as v1
from . import rti_microstructure_v15 as v15
from .rti_microstructure_v16_identity import (
    DESIGN_ID,
    FEATURE_SOURCE_AFTER_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME,
    PROTOCOL_ID,
    PROTOCOL_SHA256,
)


FEATURE_SCHEMA_VERSION = "rti-probability-microstructure-features-v16"
BASE_FEATURE_NAMES = tuple(v15.FEATURE_NAMES)
ASSET_INDICATOR_NAMES = (
    "asset_is_doge",
    "asset_is_eth",
    "asset_is_hype",
    "asset_is_sol",
    "asset_is_xrp",
)
INTERACTION_NAMES = (
    "distance_x_trend_efficiency",
    "distance_to_vol_x_persistence",
    "distance_to_vol_x_crossing_recency",
    "acceleration_x_persistence",
    "recent_crossing_pressure",
    "market_edge_x_path_direction_agreement",
    "spot_flow_x_path_depth_imbalance",
    "path_depth_delta_x_partial_fill_acceleration",
    "spread_stress_x_distance_to_vol",
    "cross_asset_breadth_x_independent_agreement",
    "cross_asset_rank_x_distance_to_vol",
    "relative_momentum_x_cross_asset_breadth",
    "btc_divergence_x_cross_asset_breadth",
    "market_edge_x_kalshi_taker_imbalance",
    "path_depth_x_kalshi_taker_imbalance",
)
FEATURE_NAMES = (
    *BASE_FEATURE_NAMES,
    *ASSET_INDICATOR_NAMES,
    *INTERACTION_NAMES,
)
EXPECTED_ASSETS = frozenset(("BTC", "BNB", "DOGE", "ETH", "HYPE", "SOL", "XRP"))

if len(BASE_FEATURE_NAMES) != 25:
    raise RuntimeError("v16_v15_base_feature_count_mismatch")
if len(FEATURE_NAMES) != 45 or len(FEATURE_NAMES) != len(set(FEATURE_NAMES)):
    raise RuntimeError("v16_feature_schema_mismatch")


def _number(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _bounded(value: float, scale: float = 1.0) -> float:
    return math.tanh(float(value) / float(scale))


def _clamp_unit(value: float) -> float:
    return max(-1.0, min(1.0, float(value)))


def feature_vector(row: Mapping[str, Any]) -> dict[str, Any]:
    profile = v1._profile(row)
    close = _number(v1._value(row, profile, "close_time"))
    if close is None:
        return {"available": False, "error": "close_time_missing"}
    if close <= FEATURE_SOURCE_AFTER_CLOSE_TIME:
        return {"available": False, "error": "pre_v16_feature_source_boundary"}
    asset = str(v1._value(row, profile, "asset") or "").upper()
    if asset not in EXPECTED_ASSETS:
        return {"available": False, "error": "asset_missing_or_unsupported"}

    base = v15.feature_vector(row)
    if not base.get("available"):
        return {
            "available": False,
            "error": f"v15_base:{base.get('error') or 'unavailable'}",
        }
    if (
        tuple(base.get("feature_names") or ()) != BASE_FEATURE_NAMES
        or len(base.get("features") or ()) != len(BASE_FEATURE_NAMES)
    ):
        return {"available": False, "error": "v15_base_schema_mismatch"}
    values = {
        name: _number(value)
        for name, value in zip(BASE_FEATURE_NAMES, base["features"])
    }
    if any(value is None for value in values.values()):
        return {"available": False, "error": "v15_base_nonfinite"}

    v = {name: float(value) for name, value in values.items()}
    distance = _bounded(v["yes_signed_distance_bps"], 10.0)
    acceleration = _bounded(v["yes_acceleration_bps"], 5.0)
    distance_to_vol = _bounded(
        v["yes_distance_to_remaining_volatility"], 3.0,
    )
    market_edge = _bounded(v["kalshi_yes_microprice_edge_cents"], 10.0)
    spot_flow = _bounded(v["spot_signed_log_net_notional_60s"], 10.0)
    relative_momentum = _bounded(
        v["target_minus_cross_asset_median_momentum_60s_bps"], 10.0,
    )
    btc_divergence = _bounded(
        v["cross_asset_btc_minus_non_btc_median_non_btc_only_60s"], 10.0,
    )
    taker = _bounded(v["kalshi_taker_imbalance_yes_60s"], 10.0)
    spread_stress = _bounded(
        v["rti_independent_path_log1p_max_spread_stress_ratio_60s"], 2.0,
    )
    persistence = _clamp_unit(v["yes_persistence_signal"])
    trend_efficiency = _clamp_unit(v["trend_efficiency"])
    crossing_recency = _clamp_unit(v["seconds_since_crossing_fraction"])
    crossing_pressure = (
        _bounded(v["log1p_strike_crossings"], 2.0)
        * (1.0 - max(0.0, crossing_recency))
    )
    path_direction = _clamp_unit(
        v["rti_independent_path_depth_direction_agreement_60s"]
    )
    path_depth = _clamp_unit(
        v["rti_independent_path_mean_depth_imbalance_60s"]
    )
    path_depth_delta = _clamp_unit(
        v["rti_independent_path_mean_depth_imbalance_half_delta_60s"]
    )
    partial_fill_acceleration = _bounded(
        v["rti_independent_path_kraken_partial_fill_imbalance_acceleration_60s"]
    )
    breadth = _clamp_unit(v["cross_asset_breadth_signed_60s"])
    independent_agreement = _clamp_unit(
        v["independent_direction_agreement_60s"]
    )
    centered_rank = _clamp_unit(v["cross_asset_centered_rank_60s"])

    indicators = [
        1.0 if asset == name else 0.0
        for name in ("DOGE", "ETH", "HYPE", "SOL", "XRP")
    ]
    interactions = [
        distance * trend_efficiency,
        distance_to_vol * persistence,
        distance_to_vol * crossing_recency,
        acceleration * persistence,
        crossing_pressure,
        market_edge * path_direction,
        spot_flow * path_depth,
        path_depth_delta * partial_fill_acceleration,
        spread_stress * distance_to_vol,
        breadth * independent_agreement,
        centered_rank * distance_to_vol,
        relative_momentum * breadth,
        btc_divergence * breadth,
        market_edge * taker,
        path_depth * taker,
    ]
    vector = [
        *[float(value) for value in base["features"]],
        *indicators,
        *interactions,
    ]
    if len(vector) != len(FEATURE_NAMES) or not all(
        math.isfinite(value) for value in vector
    ):
        return {"available": False, "error": "v16_derived_feature_invalid"}
    return {
        **base,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "features": vector,
        "design_id": DESIGN_ID,
        "protocol_id": PROTOCOL_ID,
        "protocol_sha256": PROTOCOL_SHA256,
        "prospective_after_close_time": PROSPECTIVE_AFTER_CLOSE_TIME,
        "prospective_calibration_eligible": (
            close > PROSPECTIVE_AFTER_CLOSE_TIME
        ),
        "base_features_identical_to_v15": True,
        "bounded_interactions_outcome_blind": True,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "notification_eligible": False,
        "real_trading_allowed": False,
    }


def model_feature_window_coverage(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    eligible = [
        row for row in rows
        if (_number(row.get("close_time")) or 0.0)
        > FEATURE_SOURCE_AFTER_CLOSE_TIME
    ]
    result = v1.model_feature_window_coverage(
        eligible,
        feature_builder=feature_vector,
        source_schema=v15.SOURCE_SCHEMA,
    )
    return {
        **result,
        "design_id": DESIGN_ID,
        "protocol_id": PROTOCOL_ID,
        "protocol_sha256": PROTOCOL_SHA256,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_count": len(FEATURE_NAMES),
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
    }
