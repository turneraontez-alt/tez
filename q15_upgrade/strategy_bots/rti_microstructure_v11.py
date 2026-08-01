"""Prospective cross-asset regime successor to frozen compact RTI V10.

V11 adds eight compact, scale-stable features describing whether the exact
target move is broad or isolated across the seven-asset crypto complex.  All
raw inputs are Coinbase/Kraken rows timestamped at or before the exact cutoff.
The module is feature construction only and cannot fit, notify, promote, or
trade.
"""
from __future__ import annotations

import math
from statistics import median
from typing import Any, Mapping, Sequence

from . import rti_microstructure as v1
from . import rti_microstructure_v10 as v10
from .rti_cross_asset_context import (
    ASSETS,
    SCHEMA_VERSION as CROSS_ASSET_SCHEMA_VERSION,
    TIME_BASIS as CROSS_ASSET_TIME_BASIS,
)
from .rti_microstructure_v11_identity import (
    CALIBRATION_REPORTING_PROTOCOL_ID,
    CALIBRATION_REPORTING_PROTOCOL_SHA256,
    DESIGN_ID,
    DESIGN_SHA256,
    EVALUATION_PROTOCOL_ID,
    EVALUATION_PROTOCOL_SHA256,
    REPORTING_PROTOCOL_ID,
    REPORTING_PROTOCOL_SHA256,
    SELECTIVE_VALUE_CURVE_PROTOCOL_ID,
    SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256,
)


FEATURE_SCHEMA_VERSION = "rti-probability-microstructure-features-v11"
MODEL_FAMILY = v10.MODEL_FAMILY
SOURCE_SCHEMA = v10.SOURCE_SCHEMA
SOURCE_TIME_BASIS = v10.SOURCE_TIME_BASIS
NON_BTC_ASSETS = v10.NON_BTC_ASSETS
EXPECTED_ASSETS = v10.EXPECTED_ASSETS
MICROSTRUCTURE_SCHEMA_VERSION = v10.MICROSTRUCTURE_SCHEMA_VERSION
MICROSTRUCTURE_TIME_BASIS = v10.MICROSTRUCTURE_TIME_BASIS
KRAKEN_PARTIAL_FILL_FLOW_SCHEMA_VERSION = (
    v10.KRAKEN_PARTIAL_FILL_FLOW_SCHEMA_VERSION
)

# Venue-level cross-asset audit persistence was finalized after the design's
# first draft. V11 receives no credit through the 17:00 ET close; its first
# eligible exact capture is 17:02 ET for the 17:15 close.
PROSPECTIVE_AFTER_CLOSE_TIME = 1784667600.0
FIRST_ELIGIBLE_CLOSE_TIME = 1784668500.0

FEATURE_NAMES = (
    *v10.FEATURE_NAMES,
    "cross_asset_median_momentum_15s",
    "cross_asset_median_momentum_60s",
    "cross_asset_breadth_signed_15s",
    "cross_asset_breadth_signed_60s",
    "log1p_cross_asset_dispersion_mad_60s",
    "cross_asset_centered_rank_60s",
    "cross_asset_btc_minus_non_btc_median_60s",
    "cross_asset_btc_direction_agreement_60s",
)
if len(FEATURE_NAMES) != 71:
    raise RuntimeError("v11_feature_schema_mismatch")


def _value(row: Mapping[str, Any], profile: Mapping[str, Any], key: str) -> Any:
    return v1._value(row, profile, key)


def _required(
    row: Mapping[str, Any], profile: Mapping[str, Any], key: str,
) -> tuple[float | None, str | None]:
    value = v1._num(_value(row, profile, key))
    if value is None:
        return None, f"required_cross_asset_feature_missing:{key}"
    return float(value), None


def _signed_breadth(values: Sequence[float]) -> float:
    epsilon = 1e-12
    return sum(
        1.0 if value > epsilon else -1.0 if value < -epsilon else 0.0
        for value in values
    ) / len(values)


def _centered_rank(target: float, values: Sequence[float]) -> float:
    less = sum(value < target for value in values)
    greater = sum(value > target for value in values)
    return (less - greater) / (len(values) - 1.0)


def _agreement(left: float, right: float) -> float:
    epsilon = 1e-12
    if abs(left) <= epsilon or abs(right) <= epsilon:
        return 0.5
    return 1.0 if (left > 0.0) == (right > 0.0) else 0.0


def _close(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-10, abs_tol=1e-9)


def _validated_context(
    row: Mapping[str, Any], profile: Mapping[str, Any],
) -> tuple[dict[str, float] | None, str | None]:
    value = lambda key: _value(row, profile, key)
    close = v1._num(value("close_time"))
    if close is None:
        return None, "close_time_missing"
    if close <= PROSPECTIVE_AFTER_CLOSE_TIME:
        return None, "pre_v11_prospective_boundary"
    if close < FIRST_ELIGIBLE_CLOSE_TIME:
        return None, "before_first_eligible_close"
    if str(value("rti_cross_asset_schema_version") or "") != (
        CROSS_ASSET_SCHEMA_VERSION
    ):
        return None, "cross_asset_schema_mismatch"
    if str(value("rti_cross_asset_time_basis") or "") != CROSS_ASSET_TIME_BASIS:
        return None, "cross_asset_time_basis_mismatch"
    if str(value("rti_cross_asset_status") or "") != "ok":
        return None, "cross_asset_status_not_ok"
    required_count = v1._num(value("rti_cross_asset_required_asset_count"))
    available_count = v1._num(value("rti_cross_asset_available_asset_count"))
    if required_count != 7.0 or available_count != 7.0:
        return None, "cross_asset_count_mismatch"
    source = v1._num(value("source_captured_at"))
    evidence = v1._num(value("evidence_as_of"))
    cutoff = v1._num(value("rti_cross_asset_evidence_cutoff_at"))
    max_lag = v1._num(value("rti_cross_asset_max_lag_seconds"))
    latest = v1._num(value("rti_cross_asset_latest_snapshot_created_at"))
    max_age = v1._num(value("rti_cross_asset_max_snapshot_age_seconds"))
    max_message = v1._num(value("rti_cross_asset_max_message_age_seconds"))
    if any(raw is None for raw in (
        source, evidence, cutoff, max_lag, latest, max_age, max_message,
    )):
        return None, "cross_asset_timestamp_evidence_missing"
    if abs(cutoff - source) > 1e-6:
        return None, "cross_asset_cutoff_not_source_timestamp"
    if cutoff > evidence + 1e-6 or latest > cutoff + 1e-6:
        return None, "cross_asset_future_evidence"
    if not 1.0 <= max_lag <= 15.0:
        return None, "cross_asset_lag_budget_invalid"
    if not 0.0 <= max_age <= max_lag or not 0.0 <= max_message <= max_lag:
        return None, "cross_asset_current_stale"

    asset = str(value("asset") or "").upper()
    if asset not in ASSETS:
        return None, "cross_asset_target_invalid"
    moves: dict[int, dict[str, float]] = {}
    derived: dict[str, float] = {}
    for horizon in (15, 60):
        latest_start, error = _required(
            row, profile, f"rti_cross_asset_latest_start_created_at_{horizon}s"
        )
        start_age, error_age = _required(
            row, profile, f"rti_cross_asset_max_start_age_seconds_{horizon}s"
        )
        start_message, error_message = _required(
            row, profile,
            f"rti_cross_asset_max_start_message_age_seconds_{horizon}s",
        )
        if error or error_age or error_message:
            return None, error or error_age or error_message
        if latest_start > cutoff - horizon + 1e-6:
            return None, f"cross_asset_future_start_{horizon}s"
        if not 0.0 <= start_age <= max_lag:
            return None, f"cross_asset_start_stale_{horizon}s"
        if not 0.0 <= start_message <= max_lag:
            return None, f"cross_asset_start_transport_stale_{horizon}s"

        horizon_moves: dict[str, float] = {}
        for candidate in ASSETS:
            consensus, error = _required(
                row, profile,
                f"rti_cross_asset_{candidate.lower()}_consensus_change_bps_{horizon}s",
            )
            if error:
                return None, error
            coinbase, error = _required(
                row, profile,
                f"rti_cross_asset_coinbase_{candidate.lower()}_change_bps_{horizon}s",
            )
            if error:
                return None, error
            kraken, error = _required(
                row, profile,
                f"rti_cross_asset_kraken_{candidate.lower()}_change_bps_{horizon}s",
            )
            if error:
                return None, error
            recomputed_consensus = (float(coinbase) + float(kraken)) / 2.0
            if not _close(float(consensus), recomputed_consensus):
                return None, (
                    "cross_asset_consensus_mismatch:"
                    f"{candidate.lower()}_{horizon}s"
                )
            horizon_moves[candidate] = recomputed_consensus
        values = list(horizon_moves.values())
        center = float(median(values))
        mad = float(median([abs(raw - center) for raw in values]))
        non_btc_center = float(median([
            horizon_moves[candidate]
            for candidate in ASSETS if candidate != "BTC"
        ]))
        recomputed = {
            "median_momentum_bps": center,
            "breadth_signed": _signed_breadth(values),
            "dispersion_mad_bps": mad,
            "btc_minus_non_btc_median_bps": (
                horizon_moves["BTC"] - non_btc_center
            ),
            "asset_centered_rank": _centered_rank(
                horizon_moves[asset], values
            ),
            "asset_btc_direction_agreement": _agreement(
                horizon_moves[asset], horizon_moves["BTC"]
            ),
        }
        for key, expected in recomputed.items():
            stored, error = _required(
                row, profile, f"rti_cross_asset_{key}_{horizon}s"
            )
            if error:
                return None, error
            if not _close(float(stored), expected):
                return None, f"cross_asset_derived_mismatch:{key}_{horizon}s"
            derived[f"{key}_{horizon}s"] = expected
        moves[horizon] = horizon_moves
    return derived, None


def feature_vector(row: Mapping[str, Any]) -> dict[str, Any]:
    profile = v1._profile(row)
    context, error = _validated_context(row, profile)
    if error:
        return {"available": False, "error": error}
    base = v10.feature_vector(row)
    if not base.get("available"):
        return {
            "available": False,
            "error": f"base:{base.get('error') or 'unavailable'}",
        }
    assert context is not None
    extension = [
        v1._clip(context["median_momentum_bps_15s"], -100.0, 100.0),
        v1._clip(context["median_momentum_bps_60s"], -200.0, 200.0),
        v1._clip(context["breadth_signed_15s"], -1.0, 1.0),
        v1._clip(context["breadth_signed_60s"], -1.0, 1.0),
        math.log1p(v1._clip(context["dispersion_mad_bps_60s"], 0.0, 500.0)),
        v1._clip(context["asset_centered_rank_60s"], -1.0, 1.0),
        v1._clip(
            context["btc_minus_non_btc_median_bps_60s"], -200.0, 200.0,
        ),
        v1._clip(context["asset_btc_direction_agreement_60s"], 0.0, 1.0),
    ]
    vector = [*base["features"], *extension]
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
        and row.get("rti_cross_asset_schema_version") == CROSS_ASSET_SCHEMA_VERSION
    ]
    return v1.model_feature_window_coverage(
        eligible,
        feature_builder=feature_vector,
        source_schema=SOURCE_SCHEMA,
    )
