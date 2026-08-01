"""Outcome-blind executable RTI V15 feature construction.

V15 preserves the frozen V14 feature vector byte-for-byte in its first twenty
positions and appends the five path summaries frozen before their first
eligible capture.  Every path value is accepted only after recomputing it from
the persisted canonical Coinbase/Kraken evidence.  Missing, stale, partial, or
tampered path evidence fails closed; this module cannot read outcomes, fit,
score, notify, promote, or trade.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from . import rti_microstructure as v1
from . import rti_microstructure_v14 as v14
from .rti_independent_path import (
    DERIVED_FEATURE_KEYS,
    SCHEMA_VERSION as PATH_SCHEMA_VERSION,
    TIME_BASIS as PATH_TIME_BASIS,
    validate_persisted_independent_path,
)
from .rti_independent_path_identity import (
    DESIGN_ID as PATH_DESIGN_ID,
    DESIGN_SHA256 as PATH_DESIGN_SHA256,
)
from .rti_microstructure_v15_identity import (
    CHARTER_ID,
    CHARTER_SHA256,
    DESIGN_ID,
    DESIGN_SHA256,
    EVALUATION_PROTOCOL_ID,
    EVALUATION_PROTOCOL_SHA256,
    FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME,
)


FEATURE_SCHEMA_VERSION = "rti-probability-microstructure-features-v15"
MODEL_FAMILY = v14.MODEL_FAMILY
SOURCE_SCHEMA = v14.SOURCE_SCHEMA
SOURCE_TIME_BASIS = v14.SOURCE_TIME_BASIS
NON_BTC_ASSETS = v14.NON_BTC_ASSETS
EXPECTED_ASSETS = v14.EXPECTED_ASSETS
MICROSTRUCTURE_SCHEMA_VERSION = v14.MICROSTRUCTURE_SCHEMA_VERSION
MICROSTRUCTURE_TIME_BASIS = v14.MICROSTRUCTURE_TIME_BASIS
KRAKEN_PARTIAL_FILL_FLOW_SCHEMA_VERSION = (
    v14.KRAKEN_PARTIAL_FILL_FLOW_SCHEMA_VERSION
)
CROSS_ASSET_SCHEMA_VERSION = v14.CROSS_ASSET_SCHEMA_VERSION
CROSS_ASSET_TIME_BASIS = v14.CROSS_ASSET_TIME_BASIS

BASE_FEATURE_NAMES = tuple(v14.FEATURE_NAMES)
PATH_FEATURE_NAMES = tuple(DERIVED_FEATURE_KEYS)
FEATURE_NAMES = (*BASE_FEATURE_NAMES, *PATH_FEATURE_NAMES)

if len(BASE_FEATURE_NAMES) != 20:
    raise RuntimeError("v15_v14_base_feature_count_mismatch")
if len(PATH_FEATURE_NAMES) != 5:
    raise RuntimeError("v15_path_feature_count_mismatch")
if len(FEATURE_NAMES) != 25 or len(FEATURE_NAMES) != len(set(FEATURE_NAMES)):
    raise RuntimeError("v15_joint_feature_schema_mismatch")


def _number(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def feature_vector(row: Mapping[str, Any]) -> dict[str, Any]:
    profile = v1._profile(row)
    close = _number(v1._value(row, profile, "close_time"))
    if close is None:
        return {"available": False, "error": "close_time_missing"}
    if close <= PROSPECTIVE_AFTER_CLOSE_TIME:
        return {"available": False, "error": "pre_v15_source_boundary"}
    if close < FIRST_ELIGIBLE_CLOSE_TIME:
        return {"available": False, "error": "before_v15_first_eligible_close"}

    base = v14.feature_vector(row)
    if not base.get("available"):
        return {
            "available": False,
            "error": f"v14_base:{base.get('error') or 'unavailable'}",
        }
    if (
        tuple(base.get("feature_names") or ()) != BASE_FEATURE_NAMES
        or len(base.get("features") or ()) != len(BASE_FEATURE_NAMES)
    ):
        return {"available": False, "error": "v14_base_schema_mismatch"}

    source_captured = _number(row.get("source_captured_at"))
    cutoff = _number(row.get("rti_independent_path_evidence_cutoff_at"))
    if source_captured is None or cutoff is None:
        return {"available": False, "error": "path_cutoff_timestamp_missing"}
    if not math.isclose(
        source_captured, cutoff, rel_tol=0.0, abs_tol=1e-6,
    ):
        return {"available": False, "error": "path_cutoff_not_source_capture"}
    exact_offset = source_captured - (close - 780.0)
    if not 0.0 <= exact_offset <= 2.0:
        return {"available": False, "error": "path_not_exact_13m"}
    if (
        row.get("rti_independent_path_design_id") != PATH_DESIGN_ID
        or row.get("rti_independent_path_design_sha256")
        != PATH_DESIGN_SHA256
        or row.get("rti_independent_path_schema_version")
        != PATH_SCHEMA_VERSION
        or row.get("rti_independent_path_time_basis") != PATH_TIME_BASIS
        or row.get("rti_independent_path_status") != "ok"
        or _number(row.get("rti_independent_path_available_count")) != 2.0
    ):
        return {"available": False, "error": "path_source_identity_mismatch"}

    path_validation = validate_persisted_independent_path(row)
    if not path_validation.get("valid"):
        errors = ",".join(str(item) for item in (
            path_validation.get("errors") or ()
        ))
        return {
            "available": False,
            "error": f"path_evidence_invalid:{errors or 'unknown'}",
        }
    if path_validation.get("prospective_credit_eligible") is not True:
        return {"available": False, "error": "path_outside_frozen_boundary"}

    path_values: list[float] = []
    for name in PATH_FEATURE_NAMES:
        value = _number(row.get(name))
        if value is None:
            return {
                "available": False,
                "error": f"path_feature_missing_or_nonfinite:{name}",
            }
        path_values.append(value)
    base_values = [float(value) for value in base["features"]]
    vector = [*base_values, *path_values]
    return {
        **base,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "features": vector,
        "design_id": DESIGN_ID,
        "design_sha256": DESIGN_SHA256,
        "source_v14_design_id": v14.DESIGN_ID,
        "source_v14_design_sha256": v14.DESIGN_SHA256,
        "source_path_design_id": PATH_DESIGN_ID,
        "source_path_design_sha256": PATH_DESIGN_SHA256,
        "source_path_evidence_sha256": row.get(
            "rti_independent_path_evidence_sha256"
        ),
        "source_successor_charter_id": CHARTER_ID,
        "source_successor_charter_sha256": CHARTER_SHA256,
        "evaluation_protocol_id": EVALUATION_PROTOCOL_ID,
        "evaluation_protocol_sha256": EVALUATION_PROTOCOL_SHA256,
        "prospective_after_close_time": PROSPECTIVE_AFTER_CLOSE_TIME,
        "first_eligible_close_time": FIRST_ELIGIBLE_CLOSE_TIME,
        "base_features_identical_to_v14": True,
        "path_features_recomputed_from_canonical_evidence": True,
        "missing_path_imputation_performed": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
    }


def model_feature_window_coverage(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    eligible = [
        row for row in rows
        if _number(row.get("close_time")) is not None
        and float(_number(row.get("close_time")))
        > PROSPECTIVE_AFTER_CLOSE_TIME
        and row.get("rti_cross_asset_schema_version")
        == CROSS_ASSET_SCHEMA_VERSION
        and row.get("rti_independent_path_schema_version")
        == PATH_SCHEMA_VERSION
    ]
    result = v1.model_feature_window_coverage(
        eligible,
        feature_builder=feature_vector,
        source_schema=SOURCE_SCHEMA,
    )
    return {
        **result,
        "design_id": DESIGN_ID,
        "design_sha256": DESIGN_SHA256,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_count": len(FEATURE_NAMES),
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
    }
