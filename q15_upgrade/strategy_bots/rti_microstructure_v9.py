"""Prospective independent-venue microstructure RTI design.

V9 extends frozen V8 with scale-stable Coinbase L2 and Kraken L3 book,
liquidity, cancellation/update, and versioned partial-fill signals.  It is
feature construction only: no labels, fitting, notification, promotion, or
order surface exists here.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from . import rti_microstructure as v1
from . import rti_microstructure_v8 as v8
from .rti_independent_microstructure import (
    KRAKEN_PARTIAL_FILL_FLOW_SCHEMA_VERSION,
    SCHEMA_VERSION as MICROSTRUCTURE_SCHEMA_VERSION,
    TIME_BASIS as MICROSTRUCTURE_TIME_BASIS,
)


FEATURE_SCHEMA_VERSION = "rti-probability-microstructure-features-v9"
MODEL_FAMILY = v8.MODEL_FAMILY
DESIGN_ID = "q15-rti-market-residual-independent-microstructure-v9"
DESIGN_SHA256 = "d57bb2455f94d1d5fbb75873da23804f78a42137177f3411ac47adab69466f58"
SOURCE_SCHEMA = v8.SOURCE_SCHEMA
SOURCE_TIME_BASIS = v8.SOURCE_TIME_BASIS
NON_BTC_ASSETS = v8.NON_BTC_ASSETS
EXPECTED_ASSETS = v8.EXPECTED_ASSETS

# Durable top-10 audit-field persistence was finalized after the exact evidence
# for the 16:00 ET close had been captured. V9 receives no credit through that
# close; first eligible evidence is the 16:02 capture for the 16:15 close.
PROSPECTIVE_AFTER_CLOSE_TIME = 1784664000.0
FIRST_ELIGIBLE_CLOSE_TIME = 1784664900.0

FEATURE_NAMES = (
    *v8.FEATURE_NAMES,
    "independent_mean_depth_imbalance",
    "independent_depth_imbalance_disagreement",
    "independent_mean_depth_imbalance_change_60s",
    "independent_mean_spread_bps",
    "independent_max_spread_bps",
    "independent_mean_log1p_depth_notional",
    "independent_abs_log_depth_ratio",
    "coinbase_remove_share_15s",
    "kraken_delete_share_15s",
    "kraken_partial_fill_aggressor_imbalance_60s",
    "kraken_log1p_partial_fill_notional_60s",
    "kraken_partial_fill_observed_60s",
)


def _integrity_error(
    row: Mapping[str, Any], profile: Mapping[str, Any],
) -> str | None:
    value = lambda key: v1._value(row, profile, key)
    close = v1._num(value("close_time"))
    if close is None:
        return "close_time_missing"
    if close <= PROSPECTIVE_AFTER_CLOSE_TIME:
        return "pre_v9_prospective_boundary"
    if close < FIRST_ELIGIBLE_CLOSE_TIME:
        return "before_first_eligible_close"
    if str(value("rti_independent_microstructure_schema_version") or "") != (
        MICROSTRUCTURE_SCHEMA_VERSION
    ):
        return "independent_microstructure_schema_mismatch"
    if str(value("rti_independent_microstructure_time_basis") or "") != (
        MICROSTRUCTURE_TIME_BASIS
    ):
        return "independent_microstructure_time_basis_mismatch"
    if str(value("rti_independent_microstructure_status") or "") != "ok":
        return "independent_microstructure_status_not_ok"
    if v1._num(value("rti_independent_microstructure_available_count")) != 2.0:
        return "independent_microstructure_available_count_mismatch"
    source = v1._num(value("source_captured_at"))
    evidence = v1._num(value("evidence_as_of"))
    cutoff = v1._num(value(
        "rti_independent_microstructure_evidence_cutoff_at"
    ))
    max_lag = v1._num(value("rti_independent_microstructure_max_lag_seconds"))
    if any(raw is None for raw in (source, evidence, cutoff, max_lag)):
        return "independent_microstructure_timestamp_evidence_missing"
    if abs(cutoff - source) > 1e-6:
        return "independent_microstructure_cutoff_not_source_timestamp"
    if cutoff > evidence + 1e-6:
        return "independent_microstructure_cutoff_after_evidence"
    if not 1.0 <= max_lag <= 15.0:
        return "independent_microstructure_lag_budget_invalid"
    for venue in ("coinbase", "kraken"):
        prefix = f"rti_independent_microstructure_{venue}"
        if str(value(f"{prefix}_status") or "") != "ok":
            return f"independent_microstructure_{venue}_status_not_ok"
        end_at = v1._num(value(f"{prefix}_snapshot_created_at"))
        end_age = v1._num(value(f"{prefix}_snapshot_age_seconds"))
        message_age = v1._num(value(f"{prefix}_message_age_seconds"))
        start_at = v1._num(value(f"{prefix}_start_created_at_60s"))
        start_age = v1._num(value(f"{prefix}_start_age_seconds_60s"))
        start_message_age = v1._num(value(
            f"{prefix}_start_message_age_seconds_60s"
        ))
        level_limit = v1._num(value(f"{prefix}_summary_level_limit"))
        start_level_limit = v1._num(value(
            f"{prefix}_start_summary_level_limit_60s"
        ))
        if any(raw is None for raw in (
            end_at, end_age, message_age, start_at, start_age,
            start_message_age, level_limit, start_level_limit,
        )):
            return f"independent_microstructure_{venue}_endpoint_missing"
        if not 0.0 <= end_age <= max_lag:
            return f"independent_microstructure_{venue}_endpoint_stale"
        if not 0.0 <= message_age <= max_lag:
            return f"independent_microstructure_{venue}_transport_stale"
        if end_at > cutoff + 1e-6:
            return f"independent_microstructure_{venue}_future_endpoint"
        if abs((cutoff - end_at) - end_age) > 1e-3:
            return f"independent_microstructure_{venue}_endpoint_age_contradiction"
        target = cutoff - 60.0
        if not 0.0 <= start_age <= max_lag:
            return f"independent_microstructure_{venue}_start_stale"
        if not 0.0 <= start_message_age <= max_lag:
            return f"independent_microstructure_{venue}_start_transport_stale"
        if start_at > target + 1e-6:
            return f"independent_microstructure_{venue}_future_start"
        if abs((target - start_at) - start_age) > 1e-3:
            return f"independent_microstructure_{venue}_start_age_contradiction"
        if level_limit != 10.0 or start_level_limit != 10.0:
            return f"independent_microstructure_{venue}_depth_limit_mismatch"
    flow_schema = str(value(
        "rti_independent_microstructure_kraken_partial_fill_flow_schema_version"
    ) or "")
    if flow_schema != KRAKEN_PARTIAL_FILL_FLOW_SCHEMA_VERSION:
        return "kraken_partial_fill_flow_schema_mismatch"
    return None


def _required(
    row: Mapping[str, Any], profile: Mapping[str, Any], key: str,
) -> tuple[float | None, str | None]:
    raw = v1._num(v1._value(row, profile, key))
    if raw is None:
        return None, f"required_independent_microstructure_feature_missing:{key}"
    return float(raw), None


def feature_vector(row: Mapping[str, Any]) -> dict[str, Any]:
    profile = v1._profile(row)
    error = _integrity_error(row, profile)
    if error:
        return {"available": False, "error": error}
    base = v8.feature_vector(row)
    if not base.get("available"):
        return {
            "available": False,
            "error": f"base:{base.get('error') or 'unavailable'}",
        }
    keys = (
        "rti_independent_microstructure_mean_depth_imbalance",
        "rti_independent_microstructure_depth_imbalance_disagreement",
        "rti_independent_microstructure_mean_depth_imbalance_change_60s",
        "rti_independent_microstructure_mean_spread_bps",
        "rti_independent_microstructure_max_spread_bps",
        "rti_independent_microstructure_coinbase_bid_notional_levels",
        "rti_independent_microstructure_coinbase_ask_notional_levels",
        "rti_independent_microstructure_kraken_bid_notional_levels",
        "rti_independent_microstructure_kraken_ask_notional_levels",
        "rti_independent_microstructure_coinbase_remove_share_15s",
        "rti_independent_microstructure_kraken_delete_share_15s",
        "rti_independent_microstructure_kraken_partial_fill_aggressor_imbalance_60s",
        "rti_independent_microstructure_kraken_partial_fill_notional_60s",
        "rti_independent_microstructure_kraken_partial_fill_observed_60s",
    )
    required: dict[str, float] = {}
    for key in keys:
        raw, missing = _required(row, profile, key)
        if missing:
            return {"available": False, "error": missing}
        required[key] = float(raw)
    cb_depth = (
        required["rti_independent_microstructure_coinbase_bid_notional_levels"]
        + required["rti_independent_microstructure_coinbase_ask_notional_levels"]
    )
    kr_depth = (
        required["rti_independent_microstructure_kraken_bid_notional_levels"]
        + required["rti_independent_microstructure_kraken_ask_notional_levels"]
    )
    if cb_depth <= 0.0 or kr_depth <= 0.0:
        return {"available": False, "error": "independent_depth_notional_invalid"}
    cb_log_depth = math.log1p(cb_depth)
    kr_log_depth = math.log1p(kr_depth)
    extension = [
        v1._clip(required[
            "rti_independent_microstructure_mean_depth_imbalance"
        ], -1.0, 1.0),
        v1._clip(required[
            "rti_independent_microstructure_depth_imbalance_disagreement"
        ], 0.0, 2.0),
        v1._clip(required[
            "rti_independent_microstructure_mean_depth_imbalance_change_60s"
        ], -2.0, 2.0),
        v1._clip(required[
            "rti_independent_microstructure_mean_spread_bps"
        ], 0.0, 100.0),
        v1._clip(required[
            "rti_independent_microstructure_max_spread_bps"
        ], 0.0, 200.0),
        (cb_log_depth + kr_log_depth) / 2.0,
        abs(cb_log_depth - kr_log_depth),
        v1._clip(required[
            "rti_independent_microstructure_coinbase_remove_share_15s"
        ], 0.0, 1.0),
        v1._clip(required[
            "rti_independent_microstructure_kraken_delete_share_15s"
        ], 0.0, 1.0),
        v1._clip(required[
            "rti_independent_microstructure_kraken_partial_fill_aggressor_imbalance_60s"
        ], -1.0, 1.0),
        math.log1p(v1._clip(required[
            "rti_independent_microstructure_kraken_partial_fill_notional_60s"
        ], 0.0, 1_000_000_000.0)),
        v1._clip(required[
            "rti_independent_microstructure_kraken_partial_fill_observed_60s"
        ], 0.0, 1.0),
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
        and row.get("rti_independent_microstructure_schema_version")
        == MICROSTRUCTURE_SCHEMA_VERSION
    ]
    return v1.model_feature_window_coverage(
        eligible,
        feature_builder=feature_vector,
        source_schema=SOURCE_SCHEMA,
    )
