"""Independent reference equations for the five frozen path features.

This intentionally does not import the production path implementation.  It
exists to catch a common-mode failure where capture and reconstruction share
the same implementation bug and therefore agree with each other.
"""
from __future__ import annotations

import json
import math
from typing import Any, Callable, Mapping, Sequence


REFERENCE_VERSION = "q15-rti-independent-path-reference-equations-v1"
FEATURE_KEYS = (
    "rti_independent_path_mean_depth_imbalance_60s",
    "rti_independent_path_mean_depth_imbalance_half_delta_60s",
    "rti_independent_path_depth_direction_agreement_60s",
    "rti_independent_path_log1p_max_spread_stress_ratio_60s",
    "rti_independent_path_kraken_partial_fill_imbalance_acceleration_60s",
)


def _number(value: Any) -> float:
    if value is None or isinstance(value, bool):
        raise ValueError("reference_numeric_value_invalid")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("reference_numeric_value_nonfinite")
    return result


def _time_weighted(
    rows: Sequence[Mapping[str, Any]],
    value: Callable[[Mapping[str, Any]], float],
    *,
    start: float,
    end: float,
) -> float:
    if not rows or end <= start:
        raise ValueError("reference_time_weighted_interval_invalid")
    ordered = sorted(rows, key=lambda row: _number(row.get("created_at")))
    eligible = [
        row for row in ordered if _number(row.get("created_at")) <= start
    ]
    if not eligible:
        raise ValueError("reference_start_state_missing")
    active = eligible[-1]
    cursor = start
    total = 0.0
    for row in ordered:
        timestamp = _number(row.get("created_at"))
        if timestamp <= start:
            continue
        if timestamp >= end:
            break
        total += value(active) * (timestamp - cursor)
        active = row
        cursor = timestamp
    total += value(active) * (end - cursor)
    return total / (end - start)


def _depth(row: Mapping[str, Any]) -> float:
    return _number(row.get("depth_imbalance"))


def _spread(row: Mapping[str, Any]) -> float:
    return _number(row.get("spread_bps"))


def _partial_fill_imbalance(row: Mapping[str, Any]) -> float:
    resting_bid_filled = _number(row.get("matched_buy_notional_60s"))
    resting_ask_filled = _number(row.get("matched_sell_notional_60s"))
    total = resting_bid_filled + resting_ask_filled
    if total <= 0.0:
        return 0.0
    return (resting_ask_filled - resting_bid_filled) / total


def reference_feature_values(row: Mapping[str, Any]) -> dict[str, float]:
    payload = json.loads(str(row.get("rti_independent_path_evidence_json") or ""))
    if not isinstance(payload, Mapping):
        raise ValueError("reference_evidence_root_invalid")
    cutoff = _number(payload.get("captured_at"))
    if _number(payload.get("horizon_seconds")) != 60.0:
        raise ValueError("reference_horizon_mismatch")
    venues = payload.get("venues")
    if not isinstance(venues, Mapping) or set(venues) != {"coinbase", "kraken"}:
        raise ValueError("reference_venues_invalid")
    start = cutoff - 60.0
    summaries: dict[str, dict[str, float]] = {}
    for venue in ("coinbase", "kraken"):
        points = venues.get(venue)
        if not isinstance(points, list) or not points:
            raise ValueError(f"reference_{venue}_points_invalid")
        if any(_number(point.get("created_at")) > cutoff for point in points):
            raise ValueError(f"reference_{venue}_future_point")
        mean_depth = _time_weighted(
            points, _depth, start=start, end=cutoff,
        )
        first_depth = _time_weighted(
            points, _depth, start=start, end=start + 30.0,
        )
        second_depth = _time_weighted(
            points, _depth, start=start + 30.0, end=cutoff,
        )
        mean_spread = _time_weighted(
            points, _spread, start=start, end=cutoff,
        )
        max_spread = max(_spread(point) for point in points)
        stress = max_spread / max(mean_spread, 1e-9)
        summaries[venue] = {
            "mean_depth": mean_depth,
            "depth_half_delta": second_depth - first_depth,
            "spread_stress": math.log1p(max(0.0, stress - 1.0)),
        }
    coinbase = summaries["coinbase"]
    kraken = summaries["kraken"]
    left = coinbase["mean_depth"]
    right = kraken["mean_depth"]
    agreement = (
        0.5 if abs(left) <= 1e-12 or abs(right) <= 1e-12
        else 1.0 if (left > 0.0) == (right > 0.0)
        else 0.0
    )
    kraken_points = venues["kraken"]
    prior_flow = _time_weighted(
        kraken_points, _partial_fill_imbalance,
        start=start, end=cutoff - 15.0,
    )
    recent_flow = _time_weighted(
        kraken_points, _partial_fill_imbalance,
        start=cutoff - 15.0, end=cutoff,
    )
    return {
        FEATURE_KEYS[0]: (left + right) / 2.0,
        FEATURE_KEYS[1]: (
            coinbase["depth_half_delta"] + kraken["depth_half_delta"]
        ) / 2.0,
        FEATURE_KEYS[2]: agreement,
        FEATURE_KEYS[3]: max(
            coinbase["spread_stress"], kraken["spread_stress"],
        ),
        FEATURE_KEYS[4]: recent_flow - prior_flow,
    }


def verify_reference_formulas(row: Mapping[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    expected: dict[str, float] = {}
    try:
        expected = reference_feature_values(row)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"reference_formula_evidence_invalid:{type(exc).__name__}")
    for key, value in expected.items():
        try:
            stored = _number(row.get(key))
        except (TypeError, ValueError):
            errors.append(f"reference_formula_stored_invalid:{key}")
            continue
        if not math.isclose(stored, value, rel_tol=1e-10, abs_tol=1e-10):
            errors.append(f"reference_formula_mismatch:{key}")
    return {
        "valid": not errors,
        "errors": errors,
        "reference_version": REFERENCE_VERSION,
        "expected_features": expected,
        "outcome_labels_read": False,
        "model_fit_performed": False,
    }
