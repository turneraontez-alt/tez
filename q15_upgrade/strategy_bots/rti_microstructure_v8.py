"""Prospective RTI independent-venue/index consensus design.

V8 branches from frozen v5 rather than requiring v6's single primary spot
path.  Coinbase and Kraken must both be fresh at the exact source timestamp;
their consensus is compared directly with the settlement-index path.  This is
feature construction only and has no labels, fitting, notification, promotion,
or order surface.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from . import rti_microstructure as v1
from . import rti_microstructure_v5 as v5
from .rti_cross_venue import (
    INDEPENDENT_SCHEMA_VERSION,
    TIME_BASIS as INDEPENDENT_TIME_BASIS,
)


FEATURE_SCHEMA_VERSION = "rti-probability-microstructure-features-v8"
MODEL_FAMILY = v5.MODEL_FAMILY
DESIGN_ID = "q15-rti-market-residual-independent-venue-v8"
DESIGN_SHA256 = "823d70f8ff658a9476c535b8a1894b42cf6acd694c95db6daea7073a8295f709"
SOURCE_SCHEMA = v5.SOURCE_SCHEMA
SOURCE_TIME_BASIS = v5.SOURCE_TIME_BASIS
NON_BTC_ASSETS = v5.NON_BTC_ASSETS
EXPECTED_ASSETS = v5.EXPECTED_ASSETS

# The design was frozen after the 07:32 evidence for the 07:45 close.  The
# first eligible exact evidence is 07:47 for the 08:00 ET close.
PROSPECTIVE_AFTER_CLOSE_TIME = 1784634300.0
FIRST_ELIGIBLE_CLOSE_TIME = 1784635200.0

FEATURE_NAMES = (
    *v5.FEATURE_NAMES,
    "independent_index_basis_current_bps",
    "independent_minus_index_momentum_60s_bps",
    "independent_consensus_momentum_15s_bps",
    "independent_consensus_momentum_60s_bps",
    "log1p_independent_momentum_spread_60s_bps",
    "independent_direction_agreement_60s",
    "log1p_independent_current_divergence_bps",
)


def _integrity_error(
    row: Mapping[str, Any], profile: Mapping[str, Any],
) -> str | None:
    value = lambda key: v1._value(row, profile, key)
    close = v1._num(value("close_time"))
    if close is None:
        return "close_time_missing"
    if close <= PROSPECTIVE_AFTER_CLOSE_TIME:
        return "pre_v8_prospective_boundary"
    if close < FIRST_ELIGIBLE_CLOSE_TIME:
        return "before_first_eligible_close"
    if str(value("rti_independent_venue_schema_version") or "") != (
        INDEPENDENT_SCHEMA_VERSION
    ):
        return "independent_venue_schema_mismatch"
    if str(value("rti_independent_venue_time_basis") or "") != (
        INDEPENDENT_TIME_BASIS
    ):
        return "independent_venue_time_basis_mismatch"
    if str(value("rti_independent_venue_status") or "") != "ok":
        return "independent_venue_status_not_ok"
    if v1._num(value("rti_independent_venue_available_count")) != 2.0:
        return "independent_venue_available_count_mismatch"
    source = v1._num(value("source_captured_at"))
    evidence = v1._num(value("evidence_as_of"))
    cutoff = v1._num(value("rti_independent_venue_evidence_cutoff_at"))
    max_lag = v1._num(value("rti_independent_venue_max_lag_seconds"))
    if any(raw is None for raw in (source, evidence, cutoff, max_lag)):
        return "independent_venue_timestamp_evidence_missing"
    if abs(cutoff - source) > 1e-6:
        return "independent_venue_cutoff_not_source_timestamp"
    if cutoff > evidence + 1e-6:
        return "independent_venue_cutoff_after_evidence"
    if not 1.0 <= max_lag <= 15.0:
        return "independent_venue_lag_budget_invalid"
    for venue in ("coinbase", "kraken"):
        if str(value(f"rti_independent_venue_{venue}_status") or "") != "ok":
            return f"independent_venue_{venue}_status_not_ok"
        end_at = v1._num(value(
            f"rti_independent_venue_{venue}_snapshot_created_at"
        ))
        end_age = v1._num(value(
            f"rti_independent_venue_{venue}_snapshot_age_seconds"
        ))
        message_age = v1._num(value(
            f"rti_independent_venue_{venue}_message_age_seconds"
        ))
        mid = v1._num(value(f"rti_independent_venue_{venue}_mid"))
        if any(raw is None for raw in (end_at, end_age, message_age, mid)):
            return f"independent_venue_{venue}_endpoint_missing"
        if mid <= 0.0 or not 0.0 <= end_age <= max_lag:
            return f"independent_venue_{venue}_endpoint_invalid"
        if end_at > cutoff + 1e-6:
            return f"independent_venue_{venue}_future_endpoint"
        if abs((cutoff - end_at) - end_age) > 1e-3:
            return f"independent_venue_{venue}_endpoint_age_contradiction"
        if not 0.0 <= message_age <= max_lag:
            return f"independent_venue_{venue}_transport_age_invalid"
        for horizon in (15, 60):
            start_at = v1._num(value(
                f"rti_independent_venue_{venue}_start_created_at_{horizon}s"
            ))
            start_age = v1._num(value(
                f"rti_independent_venue_{venue}_start_age_seconds_{horizon}s"
            ))
            start_mid = v1._num(value(
                f"rti_independent_venue_{venue}_start_mid_{horizon}s"
            ))
            move = v1._num(value(
                f"rti_independent_venue_{venue}_change_bps_{horizon}s"
            ))
            if any(raw is None for raw in (
                start_at, start_age, start_mid, move,
            )):
                return f"independent_venue_{venue}_{horizon}s_start_missing"
            target = cutoff - float(horizon)
            if start_mid <= 0.0 or not 0.0 <= start_age <= max_lag:
                return f"independent_venue_{venue}_{horizon}s_start_invalid"
            if start_at > target + 1e-6:
                return f"independent_venue_{venue}_{horizon}s_future_start"
            if abs((target - start_at) - start_age) > 1e-3:
                return f"independent_venue_{venue}_{horizon}s_age_contradiction"
    return None


def _required(
    row: Mapping[str, Any], profile: Mapping[str, Any], key: str,
) -> tuple[float | None, str | None]:
    raw = v1._num(v1._value(row, profile, key))
    if raw is None:
        return None, f"required_independent_venue_feature_missing:{key}"
    return float(raw), None


def feature_vector(row: Mapping[str, Any]) -> dict[str, Any]:
    profile = v1._profile(row)
    error = _integrity_error(row, profile)
    if error:
        return {"available": False, "error": error}
    base = v5.feature_vector(row)
    if not base.get("available"):
        return {
            "available": False,
            "error": f"base:{base.get('error') or 'unavailable'}",
        }
    required = {}
    for key in (
        "rti_independent_venue_consensus_mid",
        "rti_independent_venue_consensus_start_mid_60s",
        "rti_independent_venue_consensus_change_bps_15s",
        "rti_independent_venue_consensus_change_bps_60s",
        "rti_independent_venue_momentum_spread_bps_60s",
        "rti_independent_venue_direction_agreement_60s",
        "rti_independent_venue_current_divergence_bps",
        "rti_path_start_px",
        "rti_path_end_px",
    ):
        raw, missing = _required(row, profile, key)
        if missing:
            return {"available": False, "error": missing}
        required[key] = float(raw)
    index_start = required["rti_path_start_px"]
    index_end = required["rti_path_end_px"]
    venue_start = required["rti_independent_venue_consensus_start_mid_60s"]
    venue_end = required["rti_independent_venue_consensus_mid"]
    if min(index_start, index_end, venue_start, venue_end) <= 0.0:
        return {"available": False, "error": "independent_index_price_invalid"}
    basis_current = (venue_end - index_end) / index_end * 10_000.0
    venue_move = (venue_end - venue_start) / venue_start * 10_000.0
    index_move = (index_end - index_start) / index_start * 10_000.0
    extension = [
        v1._clip(basis_current, -100.0, 100.0),
        v1._clip(venue_move - index_move, -100.0, 100.0),
        v1._clip(required[
            "rti_independent_venue_consensus_change_bps_15s"
        ], -100.0, 100.0),
        v1._clip(required[
            "rti_independent_venue_consensus_change_bps_60s"
        ], -200.0, 200.0),
        math.log1p(v1._clip(required[
            "rti_independent_venue_momentum_spread_bps_60s"
        ], 0.0, 500.0)),
        v1._clip(required[
            "rti_independent_venue_direction_agreement_60s"
        ], 0.0, 1.0),
        math.log1p(v1._clip(required[
            "rti_independent_venue_current_divergence_bps"
        ], 0.0, 500.0)),
    ]
    vector = [*base["features"], *extension]
    if len(vector) != len(FEATURE_NAMES):
        return {"available": False, "error": "feature_schema_length_mismatch"}
    return {
        **base,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "features": vector,
        "design_id": DESIGN_ID,
        "design_sha256": DESIGN_SHA256,
        "prospective_after_close_time": PROSPECTIVE_AFTER_CLOSE_TIME,
        "first_eligible_close_time": FIRST_ELIGIBLE_CLOSE_TIME,
    }


def model_feature_window_coverage(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    eligible = [
        row for row in rows
        if v1._num(row.get("close_time")) is not None
        and float(v1._num(row.get("close_time"))) > PROSPECTIVE_AFTER_CLOSE_TIME
        and row.get("rti_independent_venue_schema_version")
        == INDEPENDENT_SCHEMA_VERSION
    ]
    return v1.model_feature_window_coverage(
        eligible,
        feature_builder=feature_vector,
        source_schema=SOURCE_SCHEMA,
    )
