"""Prospective market-anchored V14 feature identity.

The point-in-time vector is exactly V13.  V14 changes only the later model
combination rule: residual trust is selected from a fixed grid using inner
chronological out-of-fold predictions contained entirely inside the current
training period.  This module constructs features and coverage only; it cannot
read outcomes, fit, notify, promote, or trade.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from . import rti_microstructure as v1
from . import rti_microstructure_v13 as v13
from .rti_microstructure_v14_identity import (
    CALIBRATION_REPORTING_PROTOCOL_ID,
    CALIBRATION_REPORTING_PROTOCOL_SHA256,
    DESIGN_ID,
    DESIGN_SHA256,
    EVALUATION_PROTOCOL_ID,
    EVALUATION_PROTOCOL_SHA256,
    FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME,
    REPORTING_PROTOCOL_ID,
    REPORTING_PROTOCOL_SHA256,
    SELECTIVE_VALUE_CURVE_PROTOCOL_ID,
    SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256,
)


FEATURE_SCHEMA_VERSION = "rti-probability-microstructure-features-v14"
MODEL_FAMILY = v13.MODEL_FAMILY
SOURCE_SCHEMA = v13.SOURCE_SCHEMA
SOURCE_TIME_BASIS = v13.SOURCE_TIME_BASIS
NON_BTC_ASSETS = v13.NON_BTC_ASSETS
EXPECTED_ASSETS = v13.EXPECTED_ASSETS
MICROSTRUCTURE_SCHEMA_VERSION = v13.MICROSTRUCTURE_SCHEMA_VERSION
MICROSTRUCTURE_TIME_BASIS = v13.MICROSTRUCTURE_TIME_BASIS
KRAKEN_PARTIAL_FILL_FLOW_SCHEMA_VERSION = (
    v13.KRAKEN_PARTIAL_FILL_FLOW_SCHEMA_VERSION
)
CROSS_ASSET_SCHEMA_VERSION = v13.CROSS_ASSET_SCHEMA_VERSION
CROSS_ASSET_TIME_BASIS = v13.CROSS_ASSET_TIME_BASIS
FEATURE_NAMES = v13.FEATURE_NAMES


def feature_vector(row: Mapping[str, Any]) -> dict[str, Any]:
    profile = v1._profile(row)
    close = v1._num(v1._value(row, profile, "close_time"))
    if close is None:
        return {"available": False, "error": "close_time_missing"}
    if close <= PROSPECTIVE_AFTER_CLOSE_TIME:
        return {"available": False, "error": "pre_v14_prospective_boundary"}
    if close < FIRST_ELIGIBLE_CLOSE_TIME:
        return {"available": False, "error": "before_first_eligible_close"}
    base = v13.feature_vector(row)
    if not base.get("available"):
        return {
            "available": False,
            "error": f"base:{base.get('error') or 'unavailable'}",
        }
    return {
        **base,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "design_id": DESIGN_ID,
        "design_sha256": DESIGN_SHA256,
        "prospective_after_close_time": PROSPECTIVE_AFTER_CLOSE_TIME,
        "first_eligible_close_time": FIRST_ELIGIBLE_CLOSE_TIME,
        "feature_formulas_identical_to_v13": True,
        "nested_safe_residual_trust_applied_only_after_model_fit": True,
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
