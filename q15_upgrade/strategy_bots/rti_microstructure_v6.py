"""Prospective RTI versus spot lead-lag feature design.

V6 adds a compact, point-in-time cross-venue block to frozen dynamics v5.  It
only accepts locally timed, continuity-complete spot-mid paths captured after
its preregistration boundary.  This module constructs features only and has no
outcome, fit, notification, order, refit, or promotion surface.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from . import rti_microstructure as v1
from . import rti_microstructure_v4 as v4
from . import rti_microstructure_v5 as v5
from .rti_microstructure_extension import EXTENSION_SCHEMA_VERSION


FEATURE_SCHEMA_VERSION = "rti-probability-microstructure-features-v6"
MODEL_FAMILY = v5.MODEL_FAMILY
DESIGN_ID = "q15-rti-market-residual-lead-lag-v6"
DESIGN_SHA256 = "67ee276a06d7a03ad177560a439ce7dda7bcacd8a6c35f33fb7ed310b699256f"
SOURCE_SCHEMA = v5.SOURCE_SCHEMA
SOURCE_TIME_BASIS = v5.SOURCE_TIME_BASIS
SPOT_PATH_SCHEMA = "spot-mid-path-local-v1"
SPOT_PATH_TIME_BASIS = "local_created_at"
LEAD_LAG_SCHEMA = "rti-spot-index-lead-lag-v1"
NON_BTC_ASSETS = v5.NON_BTC_ASSETS
EXPECTED_ASSETS = v5.EXPECTED_ASSETS

# No row through 06:00 ET receives v6 credit.  The first eligible close is
# 06:15 ET, whose exact-13M evidence is captured at 06:02 after collector warmup.
PROSPECTIVE_AFTER_CLOSE_TIME = 1784628000.0
FIRST_ELIGIBLE_CLOSE_TIME = 1784628900.0

FEATURE_NAMES = (
    *v5.FEATURE_NAMES,
    "spot_index_basis_current_bps",
    "spot_minus_index_momentum_60s_bps",
    "spot_mid_momentum_15s_bps",
    "spot_mid_momentum_60s_bps",
    "log1p_spot_mid_range_bps_60s",
    "log1p_spot_mid_realized_volatility_bps_60s",
    "spot_mid_trend_efficiency_60s",
)


def _required(
    row: Mapping[str, Any], profile: Mapping[str, Any], key: str,
) -> tuple[float | None, str | None]:
    value = v1._num(v1._value(row, profile, key))
    if value is None:
        return None, f"required_lead_lag_feature_missing:{key}"
    return float(value), None


def _integrity_error(
    row: Mapping[str, Any], profile: Mapping[str, Any],
) -> str | None:
    value = lambda key: v1._value(row, profile, key)
    close_time = v1._num(value("close_time"))
    if close_time is None:
        return "close_time_missing"
    if close_time <= PROSPECTIVE_AFTER_CLOSE_TIME:
        return "pre_v6_prospective_boundary"
    if close_time < FIRST_ELIGIBLE_CLOSE_TIME:
        return "before_first_eligible_close"
    if str(value("kalshi_microstructure_extension_schema_version") or "") != (
        EXTENSION_SCHEMA_VERSION
    ):
        return "extension_schema_mismatch"
    if str(value("spot_mid_path_schema_version") or "") != SPOT_PATH_SCHEMA:
        return "spot_path_schema_mismatch"
    if str(value("spot_mid_path_time_basis") or "") != SPOT_PATH_TIME_BASIS:
        return "spot_path_time_basis_mismatch"
    if str(value("rti_spot_lead_lag_schema_version") or "") != LEAD_LAG_SCHEMA:
        return "lead_lag_schema_mismatch"
    if str(value("rti_spot_lead_lag_status") or "") != "ok":
        return "lead_lag_status_not_ok"
    for horizon in (15, 60):
        if v4._flag(value(f"spot_mid_window_complete_{horizon}s")) is not True:
            return f"spot_mid_window_incomplete_{horizon}s"

    source_captured = v1._num(value("source_captured_at"))
    evidence_as_of = v1._num(value("evidence_as_of"))
    spot_captured = v1._num(value("spot_mid_path_captured_at"))
    history_started = v1._num(value("spot_mid_history_started_at"))
    history_seconds = v1._num(value("spot_mid_history_seconds"))
    retention = v1._num(value("spot_mid_history_retention_seconds"))
    interval = v1._num(value("spot_mid_record_interval_seconds"))
    start_at = v1._num(value("spot_mid_path_start_at_60s"))
    end_at = v1._num(value("spot_mid_path_end_at_60s"))
    max_gap = v1._num(value("spot_mid_path_max_gap_seconds_60s"))
    if any(raw is None for raw in (
        source_captured, evidence_as_of, spot_captured, history_started,
        history_seconds, retention, interval, start_at, end_at, max_gap,
    )):
        return "spot_path_timestamp_evidence_missing"
    if not 0.0 <= spot_captured - source_captured <= 3.0:
        return "spot_path_not_decision_time"
    if spot_captured > evidence_as_of:
        return "spot_path_after_evidence_cutoff"
    if history_seconds < 60.0 or retention < 60.0:
        return "spot_path_history_below_60s"
    if history_started > spot_captured - 60.0:
        return "spot_path_history_start_contradiction"
    if start_at > spot_captured - 60.0:
        return "spot_path_window_start_contradiction"
    if abs(end_at - spot_captured) > 1e-6:
        return "spot_path_window_end_contradiction"
    if interval <= 0.0 or max_gap > max(3.0, interval * 2.0):
        return "spot_path_continuity_contradiction"
    return None


def feature_vector(row: Mapping[str, Any]) -> dict[str, Any]:
    """Build the pinned 53-feature point-in-time v6 vector."""
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

    values = []
    for key, low, high in (
        ("rti_spot_basis_bps", -100.0, 100.0),
        ("rti_spot_minus_index_momentum_bps_60s", -100.0, 100.0),
        ("spot_mid_change_bps_15s", -100.0, 100.0),
        ("spot_mid_change_bps_60s", -200.0, 200.0),
    ):
        raw, missing = _required(row, profile, key)
        if missing:
            return {"available": False, "error": missing}
        values.append(v1._clip(float(raw), low, high))
    spot_range, error = _required(
        row, profile, "spot_mid_range_bps_60s"
    )
    if error:
        return {"available": False, "error": error}
    spot_volatility, error = _required(
        row, profile, "spot_mid_realized_volatility_bps_60s"
    )
    if error:
        return {"available": False, "error": error}
    spot_efficiency, error = _required(
        row, profile, "spot_mid_trend_efficiency_60s"
    )
    if error:
        return {"available": False, "error": error}
    lead_lag = [
        *values,
        math.log1p(v1._clip(float(spot_range), 0.0, 500.0)),
        math.log1p(v1._clip(float(spot_volatility), 0.0, 500.0)),
        v1._clip(float(spot_efficiency), 0.0, 1.0),
    ]
    vector = [*base["features"], *lead_lag]
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
        and row.get("kalshi_microstructure_extension_schema_version")
        == EXTENSION_SCHEMA_VERSION
        and row.get("spot_mid_path_schema_version") == SPOT_PATH_SCHEMA
        and row.get("rti_spot_lead_lag_schema_version") == LEAD_LAG_SCHEMA
    ]
    return v1.model_feature_window_coverage(
        eligible,
        feature_builder=feature_vector,
        source_schema=SOURCE_SCHEMA,
    )
