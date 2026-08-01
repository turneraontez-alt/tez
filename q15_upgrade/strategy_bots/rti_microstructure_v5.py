"""Prospective RTI dynamics feature design layered on the frozen v4 control.

V5 compresses the optional queue, microprice, and trade-path extension into a
small set of scale-stable dynamics.  It is intentionally feature-only: no
settlement read, fitting, notification, order, refit, or promotion surface is
present here.  Rows at or before the preregistration boundary are ineligible.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from . import rti_microstructure as v1
from . import rti_microstructure_v4 as v4
from .rti_microstructure_extension import (
    EXTENSION_SCHEMA_VERSION,
    HORIZONS,
    REQUIRED_METRICS,
)


FEATURE_SCHEMA_VERSION = "rti-probability-microstructure-features-v5"
MODEL_FAMILY = v4.MODEL_FAMILY
DESIGN_ID = "q15-rti-market-residual-dynamics-v5"
DESIGN_SHA256 = "1d773697299d67caf136ec3cfc3a8563298e1a48dc36c54f63d8c9ee4e287316"
SOURCE_SCHEMA = v4.SOURCE_SCHEMA
SOURCE_TIME_BASIS = v4.SOURCE_TIME_BASIS
NON_BTC_ASSETS = v4.NON_BTC_ASSETS
EXPECTED_ASSETS = v4.EXPECTED_ASSETS

# The design gives no credit through the 05:15 ET close boundary.  The first
# eligible close is 05:30 ET, ensuring its exact-13M evidence is captured after
# freeze even if preregistration and service deployment timestamps differ.
PROSPECTIVE_AFTER_CLOSE_TIME = 1784625300.0
FIRST_ELIGIBLE_CLOSE_TIME = 1784626200.0

FEATURE_NAMES = (
    *v4.FEATURE_NAMES,
    "kalshi_queue_pressure_yes_5s",
    "kalshi_queue_pressure_yes_30s",
    "kalshi_queue_pressure_acceleration_yes",
    "kalshi_microprice_velocity_yes_5s",
    "kalshi_microprice_velocity_yes_30s",
    "kalshi_microprice_velocity_acceleration_yes",
    "kalshi_microprice_directional_efficiency_yes_30s",
    "kalshi_log1p_microprice_range_cents_30s",
    "kalshi_trade_price_velocity_yes_30s",
    "kalshi_trade_price_velocity_acceleration_yes",
    "kalshi_trade_price_directional_efficiency_yes_30s",
    "kalshi_microprice_minus_trade_vwap_cents_30s",
    "kalshi_trade_share_of_updates_30s",
    "kalshi_trade_vwap_missing_30s",
)


def _required(
    row: Mapping[str, Any], profile: Mapping[str, Any], key: str,
) -> tuple[float | None, str | None]:
    value = v1._num(v1._value(row, profile, key))
    if value is None:
        return None, f"required_extension_feature_missing:{key}"
    return float(value), None


def _extension_integrity_error(
    row: Mapping[str, Any], profile: Mapping[str, Any],
) -> str | None:
    value = lambda key: v1._value(row, profile, key)
    if str(value("kalshi_microstructure_extension_schema_version") or "") != (
        EXTENSION_SCHEMA_VERSION
    ):
        return "extension_schema_mismatch"
    close_time = v1._num(value("close_time"))
    if close_time is None:
        return "close_time_missing"
    if close_time <= PROSPECTIVE_AFTER_CLOSE_TIME:
        return "pre_v5_prospective_boundary"
    if close_time < FIRST_ELIGIBLE_CLOSE_TIME:
        return "before_first_eligible_close"
    for horizon in HORIZONS:
        if v4._flag(value(
            f"kalshi_microstructure_window_complete_{horizon}s"
        )) is not True:
            return f"extension_window_incomplete_{horizon}s"
        for metric in REQUIRED_METRICS:
            if v1._num(value(f"kalshi_{metric}_{horizon}s")) is None:
                return f"required_extension_feature_missing:{metric}_{horizon}s"
        trade_count = v1._num(value(f"kalshi_trade_count_{horizon}s"))
        if trade_count is None:
            return f"trade_count_missing_{horizon}s"
        trade_vwap = v1._num(value(
            f"kalshi_trade_yes_vwap_cents_{horizon}s"
        ))
        if trade_count > 0.0 and trade_vwap is None:
            return f"trade_vwap_missing_with_trades_{horizon}s"
    return None


def _queue_pressure(
    row: Mapping[str, Any], profile: Mapping[str, Any], horizon: int,
) -> tuple[float | None, str | None]:
    names = (
        "book_add_volume_yes",
        "book_remove_volume_yes",
        "book_add_volume_no",
        "book_remove_volume_no",
    )
    values = []
    for name in names:
        value, error = _required(row, profile, f"kalshi_{name}_{horizon}s")
        if error:
            return None, error
        values.append(max(0.0, float(value)))
    yes_add, yes_remove, no_add, no_remove = values
    total = yes_add + yes_remove + no_add + no_remove
    if total <= 0.0:
        return 0.0, None
    # YES bid additions and NO bid removals support YES; the opposite actions
    # support NO.  Normalization prevents activity level from dominating.
    pressure = (yes_add + no_remove - yes_remove - no_add) / total
    return v1._clip(pressure, -1.0, 1.0), None


def _velocity(change_cents: float, horizon: int) -> float:
    return v1._clip(change_cents / float(horizon), -5.0, 5.0)


def _signed_efficiency(change: float, efficiency: float) -> float:
    if change == 0.0:
        return 0.0
    return math.copysign(v1._clip(efficiency, 0.0, 1.0), change)


def feature_vector(row: Mapping[str, Any]) -> dict[str, Any]:
    """Build the pinned v5 vector strictly from decision-time evidence."""
    profile = v1._profile(row)
    integrity_error = _extension_integrity_error(row, profile)
    if integrity_error:
        return {"available": False, "error": integrity_error}

    base = v4.feature_vector(row)
    if not base.get("available"):
        return {
            "available": False,
            "error": f"base:{base.get('error') or 'unavailable'}",
        }

    queue_5, error = _queue_pressure(row, profile, 5)
    if error:
        return {"available": False, "error": error}
    queue_30, error = _queue_pressure(row, profile, 30)
    if error:
        return {"available": False, "error": error}

    micro_change_5, error = _required(
        row, profile, "kalshi_microprice_change_cents_5s"
    )
    if error:
        return {"available": False, "error": error}
    micro_change_30, error = _required(
        row, profile, "kalshi_microprice_change_cents_30s"
    )
    if error:
        return {"available": False, "error": error}
    micro_efficiency_30, error = _required(
        row, profile, "kalshi_microprice_trend_efficiency_30s"
    )
    if error:
        return {"available": False, "error": error}
    micro_range_30, error = _required(
        row, profile, "kalshi_microprice_range_cents_30s"
    )
    if error:
        return {"available": False, "error": error}

    trade_change_5, error = _required(
        row, profile, "kalshi_trade_yes_price_change_cents_5s"
    )
    if error:
        return {"available": False, "error": error}
    trade_change_30, error = _required(
        row, profile, "kalshi_trade_yes_price_change_cents_30s"
    )
    if error:
        return {"available": False, "error": error}
    trade_efficiency_30, error = _required(
        row, profile, "kalshi_trade_yes_price_trend_efficiency_30s"
    )
    if error:
        return {"available": False, "error": error}
    event_count_30, error = _required(
        row, profile, "kalshi_event_count_30s"
    )
    if error:
        return {"available": False, "error": error}
    trade_count_30, error = _required(
        row, profile, "kalshi_trade_count_30s"
    )
    if error:
        return {"available": False, "error": error}

    micro_5_velocity = _velocity(float(micro_change_5), 5)
    micro_30_velocity = _velocity(float(micro_change_30), 30)
    trade_5_velocity = _velocity(float(trade_change_5), 5)
    trade_30_velocity = _velocity(float(trade_change_30), 30)
    current_microprice, error = _required(
        row, profile, "kalshi_yes_microprice_cents"
    )
    if error:
        return {"available": False, "error": error}
    trade_vwap = v1._num(v1._value(
        row, profile, "kalshi_trade_yes_vwap_cents_30s"
    ))
    vwap_missing = trade_vwap is None
    vwap_edge = 0.0 if vwap_missing else v1._clip(
        float(current_microprice) - float(trade_vwap), -20.0, 20.0
    )
    activity_total = max(0.0, float(event_count_30)) + max(
        0.0, float(trade_count_30)
    )
    trade_share = (
        0.0 if activity_total <= 0.0
        else max(0.0, float(trade_count_30)) / activity_total
    )

    dynamics = [
        float(queue_5),
        float(queue_30),
        v1._clip(float(queue_5) - float(queue_30), -2.0, 2.0),
        micro_5_velocity,
        micro_30_velocity,
        v1._clip(micro_5_velocity - micro_30_velocity, -10.0, 10.0),
        _signed_efficiency(float(micro_change_30), float(micro_efficiency_30)),
        math.log1p(v1._clip(float(micro_range_30), 0.0, 100.0)),
        trade_30_velocity,
        v1._clip(trade_5_velocity - trade_30_velocity, -10.0, 10.0),
        _signed_efficiency(float(trade_change_30), float(trade_efficiency_30)),
        vwap_edge,
        v1._clip(trade_share, 0.0, 1.0),
        1.0 if vwap_missing else 0.0,
    ]
    vector = [*base["features"], *dynamics]
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
    ]
    return v1.model_feature_window_coverage(
        eligible,
        feature_builder=feature_vector,
        source_schema=SOURCE_SCHEMA,
    )
