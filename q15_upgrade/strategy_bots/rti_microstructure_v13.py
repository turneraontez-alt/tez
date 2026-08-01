"""Prospective cohort-conditioned compact successor to frozen RTI V12.

V13 changes exactly one V12 feature: the BTC-minus-non-BTC regime gap is set
to zero for BTC, where it was a structural near-alias of target-relative
momentum, and is preserved for the non-BTC transfer cohort.  The change was
predeclared and triggered from outcome-blind geometry.  This module can only
construct point-in-time features and coverage; it cannot read outcomes, fit,
notify, promote, or trade.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from . import rti_microstructure as v1
from . import rti_microstructure_v12 as v12
from .rti_microstructure_v13_identity import (
    CALIBRATION_REPORTING_PROTOCOL_ID,
    CALIBRATION_REPORTING_PROTOCOL_SHA256,
    COVARIATE_DRIFT_PROTOCOL_ID,
    COVARIATE_DRIFT_PROTOCOL_SHA256,
    DESIGN_ID,
    DESIGN_SHA256,
    EVALUATION_PROTOCOL_ID,
    EVALUATION_PROTOCOL_SHA256,
    FIRST_ELIGIBLE_CLOSE_TIME,
    GEOMETRY_REVIEW_PROTOCOL_ID,
    GEOMETRY_REVIEW_PROTOCOL_SHA256,
    PROSPECTIVE_AFTER_CLOSE_TIME,
    REPORTING_PROTOCOL_ID,
    REPORTING_PROTOCOL_SHA256,
    SELECTIVE_VALUE_CURVE_PROTOCOL_ID,
    SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256,
)


FEATURE_SCHEMA_VERSION = "rti-probability-microstructure-features-v13"
MODEL_FAMILY = v12.MODEL_FAMILY
SOURCE_SCHEMA = v12.SOURCE_SCHEMA
SOURCE_TIME_BASIS = v12.SOURCE_TIME_BASIS
NON_BTC_ASSETS = v12.NON_BTC_ASSETS
EXPECTED_ASSETS = v12.EXPECTED_ASSETS
MICROSTRUCTURE_SCHEMA_VERSION = v12.MICROSTRUCTURE_SCHEMA_VERSION
MICROSTRUCTURE_TIME_BASIS = v12.MICROSTRUCTURE_TIME_BASIS
KRAKEN_PARTIAL_FILL_FLOW_SCHEMA_VERSION = (
    v12.KRAKEN_PARTIAL_FILL_FLOW_SCHEMA_VERSION
)
CROSS_ASSET_SCHEMA_VERSION = v12.CROSS_ASSET_SCHEMA_VERSION
CROSS_ASSET_TIME_BASIS = v12.CROSS_ASSET_TIME_BASIS

REPLACED_FEATURE = "cross_asset_btc_minus_non_btc_median_60s"
COHORT_CONDITIONED_FEATURE = (
    "cross_asset_btc_minus_non_btc_median_non_btc_only_60s"
)
FEATURE_NAMES = tuple(
    COHORT_CONDITIONED_FEATURE if name == REPLACED_FEATURE else name
    for name in v12.FEATURE_NAMES
)
if len(FEATURE_NAMES) != 20 or len(FEATURE_NAMES) != len(set(FEATURE_NAMES)):
    raise RuntimeError("v13_compact_feature_schema_mismatch")
if set(FEATURE_NAMES) - {COHORT_CONDITIONED_FEATURE} != (
    set(v12.FEATURE_NAMES) - {REPLACED_FEATURE}
):
    raise RuntimeError("v13_changed_more_than_frozen_replacement")


def feature_vector(row: Mapping[str, Any]) -> dict[str, Any]:
    profile = v1._profile(row)
    close = v1._num(v1._value(row, profile, "close_time"))
    if close is None:
        return {"available": False, "error": "close_time_missing"}
    if close <= PROSPECTIVE_AFTER_CLOSE_TIME:
        return {"available": False, "error": "pre_v13_prospective_boundary"}
    if close < FIRST_ELIGIBLE_CLOSE_TIME:
        return {"available": False, "error": "before_first_eligible_close"}
    base = v12.feature_vector(row)
    if not base.get("available"):
        return {
            "available": False,
            "error": f"base:{base.get('error') or 'unavailable'}",
        }
    by_name = dict(zip(v12.FEATURE_NAMES, base["features"]))
    asset = str(row.get("asset") or profile.get("asset") or "").upper()
    if asset not in EXPECTED_ASSETS:
        return {"available": False, "error": "asset_missing_or_unsupported"}
    original_gap = float(by_name.pop(REPLACED_FEATURE))
    by_name[COHORT_CONDITIONED_FEATURE] = (
        0.0 if asset == "BTC" else original_gap
    )
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
        "outcome_blind_cohort_conditioning": True,
        "cohort_conditioned_feature": COHORT_CONDITIONED_FEATURE,
        "cohort_conditioned_source_feature": REPLACED_FEATURE,
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
