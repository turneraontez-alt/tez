"""Outcome-blind V17 features from pre-existing Kalshi path dynamics."""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from . import rti_microstructure as v1
from . import rti_microstructure_v16 as v16
from .rti_microstructure_extension import (
    EXTENSION_SCHEMA_VERSION,
    SOURCE_SCHEMA,
    TIME_BASIS,
)
from .rti_microstructure_v17_identity import (
    DESIGN_ID,
    FEATURE_SOURCE_AFTER_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME,
    PROTOCOL_ID,
    PROTOCOL_SHA256,
)


FEATURE_SCHEMA_VERSION = "rti-probability-microstructure-features-v17"
BASE_FEATURE_NAMES = tuple(v16.FEATURE_NAMES)
PATH_DYNAMIC_FEATURE_NAMES = (
    "kalshi_microprice_change_bounded_5s",
    "kalshi_microprice_change_bounded_15s",
    "kalshi_microprice_change_bounded_30s",
    "kalshi_microprice_change_bounded_60s",
    "kalshi_microprice_short_acceleration",
    "kalshi_microprice_long_acceleration",
    "kalshi_microprice_short_long_agreement",
    "kalshi_microprice_range_bounded_60s",
    "kalshi_microprice_variation_bounded_60s",
    "kalshi_microprice_trend_efficiency_60s",
    "kalshi_trade_price_change_bounded_5s",
    "kalshi_trade_price_change_bounded_15s",
    "kalshi_trade_price_change_bounded_30s",
    "kalshi_trade_price_change_bounded_60s",
    "kalshi_trade_price_short_acceleration",
    "kalshi_trade_price_short_long_agreement",
    "kalshi_trade_microprice_divergence_60s",
    "kalshi_trade_imbalance_yes_5s",
    "kalshi_trade_imbalance_yes_15s",
    "kalshi_trade_imbalance_yes_30s",
    "kalshi_book_delta_pressure_yes_5s",
    "kalshi_book_delta_pressure_yes_15s",
    "kalshi_book_delta_pressure_yes_60s",
    "kalshi_refill_depletion_pressure_yes_5s",
    "kalshi_refill_depletion_pressure_yes_15s",
    "kalshi_refill_depletion_pressure_yes_30s",
    "kalshi_refill_depletion_pressure_yes_60s",
    "kalshi_add_remove_pressure_yes_5s",
    "kalshi_add_remove_pressure_yes_15s",
    "kalshi_add_remove_pressure_yes_30s",
    "kalshi_add_remove_pressure_yes_60s",
    "rti_distance_x_kalshi_microprice_change_5s",
    "rti_distance_x_kalshi_microprice_change_60s",
    "kalshi_log_trade_count_bounded_5s",
    "kalshi_log_trade_count_bounded_60s",
    "kalshi_log_event_count_bounded_60s",
)
FEATURE_NAMES = (*BASE_FEATURE_NAMES, *PATH_DYNAMIC_FEATURE_NAMES)
HORIZONS = (5, 15, 30, 60)

if len(BASE_FEATURE_NAMES) != 45:
    raise RuntimeError("v17_v16_base_feature_count_mismatch")
if len(PATH_DYNAMIC_FEATURE_NAMES) != 36 or len(FEATURE_NAMES) != 81:
    raise RuntimeError("v17_feature_schema_mismatch")
if len(FEATURE_NAMES) != len(set(FEATURE_NAMES)):
    raise RuntimeError("v17_duplicate_feature_name")


def _number(row: Mapping[str, Any], profile: Mapping[str, Any], key: str) -> float | None:
    value = v1._value(row, profile, key)
    try:
        if value is None or isinstance(value, bool):
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _flag(row: Mapping[str, Any], profile: Mapping[str, Any], key: str) -> bool | None:
    value = v1._value(row, profile, key)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and float(value) in {0.0, 1.0}:
        return bool(value)
    return None


def _bounded(value: float, scale: float) -> float:
    return math.tanh(float(value) / float(scale))


def _unit(value: float) -> float:
    return max(-1.0, min(1.0, float(value)))


def _flow_pressure(
    yes_positive: float,
    yes_negative: float,
    no_positive: float,
    no_negative: float,
) -> float:
    numerator = (
        (yes_positive - yes_negative) - (no_positive - no_negative)
    )
    denominator = 1.0 + sum(abs(value) for value in (
        yes_positive, yes_negative, no_positive, no_negative,
    ))
    return _unit(numerator / denominator)


def feature_vector(row: Mapping[str, Any]) -> dict[str, Any]:
    profile = v1._profile(row)
    close = _number(row, profile, "close_time")
    if close is None:
        return {"available": False, "error": "close_time_missing"}
    if close <= FEATURE_SOURCE_AFTER_CLOSE_TIME:
        return {"available": False, "error": "pre_v17_feature_source_boundary"}
    if (
        v1._value(row, profile, "kalshi_microstructure_schema_version")
        != SOURCE_SCHEMA
        or v1._value(
            row, profile, "kalshi_microstructure_extension_schema_version"
        ) != EXTENSION_SCHEMA_VERSION
        or v1._value(row, profile, "kalshi_microstructure_time_basis")
        != TIME_BASIS
        or _flag(row, profile, "kalshi_history_count_capped") is not False
    ):
        return {"available": False, "error": "v17_extension_identity_invalid"}
    for horizon in HORIZONS:
        if _flag(
            row,
            profile,
            f"kalshi_microstructure_window_complete_{horizon}s",
        ) is not True:
            return {
                "available": False,
                "error": f"v17_extension_window_incomplete_{horizon}s",
            }

    base = v16.feature_vector(row)
    if (
        not base.get("available")
        or tuple(base.get("feature_names") or ()) != BASE_FEATURE_NAMES
        or len(base.get("features") or ()) != len(BASE_FEATURE_NAMES)
    ):
        return {
            "available": False,
            "error": f"v16_base:{base.get('error') or 'schema_mismatch'}",
        }

    required: dict[str, float] = {}
    metric_names = (
        "microprice_change_cents",
        "trade_yes_price_change_cents",
        "trade_imbalance_yes",
        "book_delta_pressure_yes",
        "yes_best_refill",
        "yes_best_depletion",
        "no_best_refill",
        "no_best_depletion",
        "book_add_volume_yes",
        "book_remove_volume_yes",
        "book_add_volume_no",
        "book_remove_volume_no",
        "trade_count",
        "event_count",
    )
    for horizon in HORIZONS:
        for metric in metric_names:
            key = f"kalshi_{metric}_{horizon}s"
            value = _number(row, profile, key)
            if value is None:
                return {"available": False, "error": f"v17_missing:{key}"}
            required[key] = value
    for metric in (
        "microprice_range_cents",
        "microprice_variation_cents",
        "microprice_trend_efficiency",
    ):
        key = f"kalshi_{metric}_60s"
        value = _number(row, profile, key)
        if value is None:
            return {"available": False, "error": f"v17_missing:{key}"}
        required[key] = value

    micro = {
        horizon: _bounded(required[f"kalshi_microprice_change_cents_{horizon}s"], 10.0)
        for horizon in HORIZONS
    }
    trade = {
        horizon: _bounded(required[f"kalshi_trade_yes_price_change_cents_{horizon}s"], 10.0)
        for horizon in HORIZONS
    }
    trade_imbalance = {
        horizon: _unit(required[f"kalshi_trade_imbalance_yes_{horizon}s"])
        for horizon in HORIZONS
    }
    book_pressure = {
        horizon: _unit(required[f"kalshi_book_delta_pressure_yes_{horizon}s"])
        for horizon in HORIZONS
    }
    refill_pressure = {
        horizon: _flow_pressure(
            required[f"kalshi_yes_best_refill_{horizon}s"],
            required[f"kalshi_yes_best_depletion_{horizon}s"],
            required[f"kalshi_no_best_refill_{horizon}s"],
            required[f"kalshi_no_best_depletion_{horizon}s"],
        )
        for horizon in HORIZONS
    }
    add_remove_pressure = {
        horizon: _flow_pressure(
            required[f"kalshi_book_add_volume_yes_{horizon}s"],
            required[f"kalshi_book_remove_volume_yes_{horizon}s"],
            required[f"kalshi_book_add_volume_no_{horizon}s"],
            required[f"kalshi_book_remove_volume_no_{horizon}s"],
        )
        for horizon in HORIZONS
    }
    distance = _bounded(float(base["features"][0]), 10.0)
    dynamics = [
        micro[5], micro[15], micro[30], micro[60],
        _unit(micro[5] - micro[15]),
        _unit(micro[15] - micro[60]),
        micro[5] * micro[60],
        _bounded(required["kalshi_microprice_range_cents_60s"], 20.0),
        _bounded(math.log1p(max(0.0, required["kalshi_microprice_variation_cents_60s"])), 5.0),
        _unit(required["kalshi_microprice_trend_efficiency_60s"]),
        trade[5], trade[15], trade[30], trade[60],
        _unit(trade[5] - trade[15]),
        trade[5] * trade[60],
        _unit(trade[60] - micro[60]),
        trade_imbalance[5], trade_imbalance[15], trade_imbalance[30],
        book_pressure[5], book_pressure[15], book_pressure[60],
        refill_pressure[5], refill_pressure[15],
        refill_pressure[30], refill_pressure[60],
        add_remove_pressure[5], add_remove_pressure[15],
        add_remove_pressure[30], add_remove_pressure[60],
        distance * micro[5], distance * micro[60],
        _bounded(math.log1p(max(0.0, required["kalshi_trade_count_5s"])), 5.0),
        _bounded(math.log1p(max(0.0, required["kalshi_trade_count_60s"])), 5.0),
        _bounded(math.log1p(max(0.0, required["kalshi_event_count_60s"])), 5.0),
    ]
    vector = [*[float(value) for value in base["features"]], *dynamics]
    if (
        len(vector) != len(FEATURE_NAMES)
        or not all(math.isfinite(value) for value in vector)
        or any(abs(value) > 1.0 + 1e-12 for value in dynamics)
    ):
        return {"available": False, "error": "v17_derived_feature_invalid"}
    return {
        **base,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "features": vector,
        "design_id": DESIGN_ID,
        "protocol_id": PROTOCOL_ID,
        "protocol_sha256": PROTOCOL_SHA256,
        "prospective_after_close_time": PROSPECTIVE_AFTER_CLOSE_TIME,
        "prospective_calibration_eligible": close > PROSPECTIVE_AFTER_CLOSE_TIME,
        "base_features_identical_to_v16": True,
        "extension_features_outcome_blind": True,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "notification_eligible": False,
        "real_trading_allowed": False,
    }


def model_feature_window_coverage(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    result = v1.model_feature_window_coverage(
        rows,
        feature_builder=feature_vector,
        source_schema=SOURCE_SCHEMA,
    )
    return {
        **result,
        "design_id": DESIGN_ID,
        "protocol_id": PROTOCOL_ID,
        "protocol_sha256": PROTOCOL_SHA256,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_count": len(FEATURE_NAMES),
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
    }
