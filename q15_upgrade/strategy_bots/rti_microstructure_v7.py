"""Prospective exact-13M RTI cross-venue consensus feature design.

V7 adds only compact Coinbase/Kraken agreement and divergence evidence to the
frozen v6 vector.  Every endpoint must have existed at or before the exact RTI
cutoff.  This module constructs features only; it cannot read outcomes, fit,
notify, promote, or trade.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from . import rti_microstructure as v1
from . import rti_microstructure_v6 as v6
from .rti_cross_venue import SCHEMA_VERSION as CROSS_VENUE_SCHEMA
from .rti_cross_venue import TIME_BASIS as CROSS_VENUE_TIME_BASIS


FEATURE_SCHEMA_VERSION = "rti-probability-microstructure-features-v7"
MODEL_FAMILY = v6.MODEL_FAMILY
DESIGN_ID = "q15-rti-market-residual-cross-venue-v7"
DESIGN_SHA256 = "1711257f38333fc3075347002ef49ba2fa7e860118507f9df0751acbb0c3658f"
SOURCE_SCHEMA = v6.SOURCE_SCHEMA
SOURCE_TIME_BASIS = v6.SOURCE_TIME_BASIS
NON_BTC_ASSETS = v6.NON_BTC_ASSETS
EXPECTED_ASSETS = v6.EXPECTED_ASSETS

# The 06:30 close's exact evidence was due before the design was frozen.  The
# first eligible fold is therefore 06:45 ET (exact evidence at 06:32 ET).
PROSPECTIVE_AFTER_CLOSE_TIME = 1784629800.0
FIRST_ELIGIBLE_CLOSE_TIME = 1784630700.0

FEATURE_NAMES = (
    *v6.FEATURE_NAMES,
    "cross_venue_consensus_momentum_15s_bps",
    "cross_venue_consensus_momentum_60s_bps",
    "primary_minus_cross_venue_momentum_60s_bps",
    "log1p_cross_venue_momentum_spread_60s_bps",
    "cross_venue_direction_agreement_60s",
    "log1p_cross_venue_current_divergence_bps",
    "primary_cross_venue_basis_bps",
)


def _required(
    row: Mapping[str, Any], profile: Mapping[str, Any], key: str,
) -> tuple[float | None, str | None]:
    value = v1._num(v1._value(row, profile, key))
    if value is None:
        return None, f"required_cross_venue_feature_missing:{key}"
    return float(value), None


def _integrity_error(
    row: Mapping[str, Any], profile: Mapping[str, Any],
) -> str | None:
    value = lambda key: v1._value(row, profile, key)
    close_time = v1._num(value("close_time"))
    if close_time is None:
        return "close_time_missing"
    if close_time <= PROSPECTIVE_AFTER_CLOSE_TIME:
        return "pre_v7_prospective_boundary"
    if close_time < FIRST_ELIGIBLE_CLOSE_TIME:
        return "before_first_eligible_close"
    if str(value("rti_cross_venue_schema_version") or "") != (
        CROSS_VENUE_SCHEMA
    ):
        return "cross_venue_schema_mismatch"
    if str(value("rti_cross_venue_time_basis") or "") != (
        CROSS_VENUE_TIME_BASIS
    ):
        return "cross_venue_time_basis_mismatch"
    if str(value("rti_cross_venue_status") or "") != "ok":
        return "cross_venue_status_not_ok"
    available = v1._num(value("rti_cross_venue_available_count"))
    if available != 2.0:
        return "cross_venue_available_count_mismatch"

    source_captured = v1._num(value("source_captured_at"))
    evidence_as_of = v1._num(value("evidence_as_of"))
    cutoff = v1._num(value("rti_cross_venue_evidence_cutoff_at"))
    max_lag = v1._num(value("rti_cross_venue_max_lag_seconds"))
    if any(raw is None for raw in (
        source_captured, evidence_as_of, cutoff, max_lag,
    )):
        return "cross_venue_timestamp_evidence_missing"
    if abs(cutoff - source_captured) > 1e-6:
        return "cross_venue_cutoff_not_source_timestamp"
    if cutoff > evidence_as_of + 1e-6:
        return "cross_venue_cutoff_after_evidence"
    if not 1.0 <= max_lag <= 15.0:
        return "cross_venue_lag_budget_invalid"

    for venue in ("coinbase", "kraken"):
        if str(value(f"rti_cross_venue_{venue}_status") or "") != "ok":
            return f"cross_venue_{venue}_status_not_ok"
        end_at = v1._num(value(
            f"rti_cross_venue_{venue}_snapshot_created_at"
        ))
        end_age = v1._num(value(
            f"rti_cross_venue_{venue}_snapshot_age_seconds"
        ))
        message_age = v1._num(value(
            f"rti_cross_venue_{venue}_message_age_seconds"
        ))
        mid = v1._num(value(f"rti_cross_venue_{venue}_mid"))
        if any(raw is None for raw in (end_at, end_age, message_age, mid)):
            return f"cross_venue_{venue}_endpoint_missing"
        if mid <= 0.0 or not 0.0 <= end_age <= max_lag:
            return f"cross_venue_{venue}_endpoint_invalid"
        if end_at > cutoff + 1e-6:
            return f"cross_venue_{venue}_future_endpoint"
        if abs((cutoff - end_at) - end_age) > 1e-3:
            return f"cross_venue_{venue}_endpoint_age_contradiction"
        if not 0.0 <= message_age <= max_lag:
            return f"cross_venue_{venue}_transport_age_invalid"
        for horizon in (15, 60):
            start_at = v1._num(value(
                f"rti_cross_venue_{venue}_start_created_at_{horizon}s"
            ))
            start_age = v1._num(value(
                f"rti_cross_venue_{venue}_start_age_seconds_{horizon}s"
            ))
            start_mid = v1._num(value(
                f"rti_cross_venue_{venue}_start_mid_{horizon}s"
            ))
            move = v1._num(value(
                f"rti_cross_venue_{venue}_change_bps_{horizon}s"
            ))
            if any(raw is None for raw in (
                start_at, start_age, start_mid, move,
            )):
                return f"cross_venue_{venue}_{horizon}s_start_missing"
            target = cutoff - float(horizon)
            if start_mid <= 0.0 or not 0.0 <= start_age <= max_lag:
                return f"cross_venue_{venue}_{horizon}s_start_invalid"
            if start_at > target + 1e-6:
                return f"cross_venue_{venue}_{horizon}s_future_start"
            if abs((target - start_at) - start_age) > 1e-3:
                return f"cross_venue_{venue}_{horizon}s_age_contradiction"
    return None


def feature_vector(row: Mapping[str, Any]) -> dict[str, Any]:
    """Build the pinned 60-feature, outcome-blind v7 vector."""
    profile = v1._profile(row)
    error = _integrity_error(row, profile)
    if error:
        return {"available": False, "error": error}
    base = v6.feature_vector(row)
    if not base.get("available"):
        return {
            "available": False,
            "error": f"base:{base.get('error') or 'unavailable'}",
        }
    raw_values: dict[str, float] = {}
    for key in (
        "rti_cross_venue_consensus_change_bps_15s",
        "rti_cross_venue_consensus_change_bps_60s",
        "rti_cross_venue_primary_minus_consensus_bps_60s",
        "rti_cross_venue_momentum_spread_bps_60s",
        "rti_cross_venue_direction_agreement_60s",
        "rti_cross_venue_current_divergence_bps",
        "rti_cross_venue_primary_basis_bps",
    ):
        raw, missing = _required(row, profile, key)
        if missing:
            return {"available": False, "error": missing}
        raw_values[key] = float(raw)
    extension = [
        v1._clip(raw_values[
            "rti_cross_venue_consensus_change_bps_15s"
        ], -100.0, 100.0),
        v1._clip(raw_values[
            "rti_cross_venue_consensus_change_bps_60s"
        ], -200.0, 200.0),
        v1._clip(raw_values[
            "rti_cross_venue_primary_minus_consensus_bps_60s"
        ], -100.0, 100.0),
        math.log1p(v1._clip(raw_values[
            "rti_cross_venue_momentum_spread_bps_60s"
        ], 0.0, 500.0)),
        v1._clip(raw_values[
            "rti_cross_venue_direction_agreement_60s"
        ], 0.0, 1.0),
        math.log1p(v1._clip(raw_values[
            "rti_cross_venue_current_divergence_bps"
        ], 0.0, 500.0)),
        v1._clip(raw_values[
            "rti_cross_venue_primary_basis_bps"
        ], -100.0, 100.0),
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
        and row.get("rti_cross_venue_schema_version") == CROSS_VENUE_SCHEMA
    ]
    return v1.model_feature_window_coverage(
        eligible,
        feature_builder=feature_vector,
        source_schema=SOURCE_SCHEMA,
    )
