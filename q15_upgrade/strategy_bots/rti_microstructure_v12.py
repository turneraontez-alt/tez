"""Outcome-blind orthogonal compact successor to frozen RTI V11.

V12 preserves a fixed, domain-balanced subset of V11's point-in-time signal
families and replaces collinear target and broad-market momentum inputs with a
single explicit relative-momentum residual.  The schema was preregistered from
feature geometry only; it cannot read outcomes, fit, notify, promote, or trade.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from . import rti_microstructure as v1
from . import rti_microstructure_v11 as v11
from .rti_microstructure_v12_identity import (
    DESIGN_ID,
    DESIGN_SHA256,
    EVALUATION_PROTOCOL_ID,
    EVALUATION_PROTOCOL_SHA256,
)


FEATURE_SCHEMA_VERSION = "rti-probability-microstructure-features-v12"
MODEL_FAMILY = v11.MODEL_FAMILY
SOURCE_SCHEMA = v11.SOURCE_SCHEMA
SOURCE_TIME_BASIS = v11.SOURCE_TIME_BASIS
NON_BTC_ASSETS = v11.NON_BTC_ASSETS
EXPECTED_ASSETS = v11.EXPECTED_ASSETS
MICROSTRUCTURE_SCHEMA_VERSION = v11.MICROSTRUCTURE_SCHEMA_VERSION
MICROSTRUCTURE_TIME_BASIS = v11.MICROSTRUCTURE_TIME_BASIS
KRAKEN_PARTIAL_FILL_FLOW_SCHEMA_VERSION = (
    v11.KRAKEN_PARTIAL_FILL_FLOW_SCHEMA_VERSION
)
CROSS_ASSET_SCHEMA_VERSION = v11.CROSS_ASSET_SCHEMA_VERSION
CROSS_ASSET_TIME_BASIS = v11.CROSS_ASSET_TIME_BASIS

# The outcome-blind 38-window V11 feature audit and the incomplete 04:45 ET
# restart window were inspected before this design was pinned.  V12 receives
# no credit through that close and begins with the following complete window.
PROSPECTIVE_AFTER_CLOSE_TIME = 1784709900.0
FIRST_ELIGIBLE_CLOSE_TIME = 1784710800.0

RELATIVE_MOMENTUM_FEATURE = (
    "target_minus_cross_asset_median_momentum_60s_bps"
)
FEATURE_NAMES = (
    "yes_signed_distance_bps",
    "yes_acceleration_bps",
    "log1p_realized_volatility_bps",
    "trend_efficiency",
    "yes_persistence_signal",
    "log1p_strike_crossings",
    "seconds_since_crossing_fraction",
    "yes_distance_to_remaining_volatility",
    "kalshi_yes_microprice_edge_cents",
    "kalshi_book_delta_pressure_yes_30s",
    "kalshi_taker_imbalance_yes_60s",
    "spot_signed_log_net_notional_60s",
    "spot_flow_missing",
    RELATIVE_MOMENTUM_FEATURE,
    "independent_direction_agreement_60s",
    "cross_asset_median_momentum_60s",
    "cross_asset_breadth_signed_60s",
    "log1p_cross_asset_dispersion_mad_60s",
    "cross_asset_centered_rank_60s",
    "cross_asset_btc_minus_non_btc_median_60s",
)
if len(FEATURE_NAMES) != 20 or len(FEATURE_NAMES) != len(set(FEATURE_NAMES)):
    raise RuntimeError("v12_compact_feature_schema_mismatch")

SOURCE_FEATURES = frozenset(
    name for name in FEATURE_NAMES if name != RELATIVE_MOMENTUM_FEATURE
)
if not SOURCE_FEATURES.issubset(v11.FEATURE_NAMES):
    raise RuntimeError("v12_source_feature_missing_from_v11")


def feature_vector(row: Mapping[str, Any]) -> dict[str, Any]:
    profile = v1._profile(row)
    close = v1._num(v1._value(row, profile, "close_time"))
    if close is None:
        return {"available": False, "error": "close_time_missing"}
    if close <= PROSPECTIVE_AFTER_CLOSE_TIME:
        return {"available": False, "error": "pre_v12_prospective_boundary"}
    if close < FIRST_ELIGIBLE_CLOSE_TIME:
        return {"available": False, "error": "before_first_eligible_close"}
    base = v11.feature_vector(row)
    if not base.get("available"):
        return {
            "available": False,
            "error": f"base:{base.get('error') or 'unavailable'}",
        }
    by_name = dict(zip(v11.FEATURE_NAMES, base["features"]))
    relative_momentum = v1._clip(
        float(by_name["independent_consensus_momentum_60s_bps"])
        - float(by_name["cross_asset_median_momentum_60s"]),
        -400.0,
        400.0,
    )
    by_name[RELATIVE_MOMENTUM_FEATURE] = relative_momentum
    vector = [float(by_name[name]) for name in FEATURE_NAMES]
    return {
        **base,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "features": vector,
        "design_id": DESIGN_ID,
        "design_sha256": DESIGN_SHA256,
        "prospective_after_close_time": PROSPECTIVE_AFTER_CLOSE_TIME,
        "first_eligible_close_time": FIRST_ELIGIBLE_CLOSE_TIME,
        "outcome_blind_compact_projection": True,
        "relative_momentum_orthogonalization": (
            "independent_consensus_momentum_60s_bps"
            "-cross_asset_median_momentum_60s"
        ),
    }


def model_feature_window_coverage(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    eligible = [
        row for row in rows
        if v1._num(row.get("close_time")) is not None
        and float(v1._num(row.get("close_time")))
        > PROSPECTIVE_AFTER_CLOSE_TIME
        and row.get("rti_cross_asset_schema_version")
        == CROSS_ASSET_SCHEMA_VERSION
    ]
    return v1.model_feature_window_coverage(
        eligible,
        feature_builder=feature_vector,
        source_schema=SOURCE_SCHEMA,
    )
