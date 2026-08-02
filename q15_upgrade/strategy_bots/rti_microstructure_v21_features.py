"""Outcome-blind V21 intraminute trajectory feature construction."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping

from . import rti_microstructure_v20_features as v20_features


FEATURE_BUILDER_VERSION = "q15-rti-v21-intraminute-trajectory-features-v2"
TRAJECTORY_FEATURE_NAMES = (
    "intermediate_side_unchanged",
    "delayed_confirmation_side_unchanged",
    "intermediate_continuation_bps",
    "second_leg_continuation_bps",
    "continuation_curvature_bps",
    "intermediate_distance_bps",
    "intermediate_market_side_probability",
    "market_probability_first_leg_change",
    "market_probability_second_leg_change",
    "market_probability_curvature",
    "intermediate_ask_cents",
    "ask_first_leg_change_cents",
    "ask_second_leg_change_cents",
    "ask_curvature_cents",
    "intermediate_side_microprice_edge_cents",
    "side_microprice_edge_change_30s_cents",
    "intermediate_side_spot_fast_move_15s_bps",
    "side_spot_fast_move_15s_change_30s_bps",
    "intermediate_side_trade_imbalance_15s",
    "side_trade_imbalance_15s_change_30s",
    "intermediate_side_book_pressure_15s_signed_log1p",
    "side_book_pressure_15s_signed_log1p_change_30s",
    "intermediate_side_spot_flow_15s_signed_log1p",
    "side_spot_flow_15s_signed_log1p_change_30s",
)
FEATURE_NAMES = (*v20_features.FEATURE_NAMES, *TRAJECTORY_FEATURE_NAMES)


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _profile(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("threshold_json")
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        decoded = json.loads(str(raw or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _value(
    row: Mapping[str, Any], profile: Mapping[str, Any], key: str,
) -> Any:
    value = row.get(key)
    return profile.get(key) if value is None else value


def _required(
    row: Mapping[str, Any], profile: Mapping[str, Any], key: str,
) -> float:
    value = _num(_value(row, profile, key))
    if value is None:
        raise ValueError(f"v21_feature_missing:{key}")
    return value


def _clip(value: float, low: float, high: float) -> float:
    return max(float(low), min(float(high), float(value)))


def _signed_log1p(value: float, cap: float = 1_000_000_000.0) -> float:
    clipped = _clip(value, -cap, cap)
    return math.copysign(math.log1p(abs(clipped)), clipped)


def feature_vector(
    parent_row: Mapping[str, Any],
    intermediate_row: Mapping[str, Any],
    delayed_row: Mapping[str, Any],
) -> dict[str, Any]:
    """Add the genuine +30s state to V20's exact +60s feature vector."""
    base = v20_features.feature_vector(parent_row, delayed_row)
    if base.get("available") is not True:
        return {
            "available": False,
            "error": "v21_base_feature_source_incomplete",
            "base_error": base.get("error"),
            "outcome_labels_read": False,
            "model_fit_performed": False,
            "probability_scoring_performed": False,
        }
    parent = _profile(parent_row)
    intermediate = _profile(intermediate_row)
    delayed = _profile(delayed_row)
    side = str(base.get("side") or "").upper()
    sign = 1.0 if side == "YES" else -1.0 if side == "NO" else 0.0
    if sign == 0.0:
        return {
            "available": False,
            "error": "v21_side_identity_invalid",
            "outcome_labels_read": False,
            "model_fit_performed": False,
            "probability_scoring_performed": False,
        }
    intermediate_side = str(intermediate.get("rti_confirm_side") or "").upper()
    delayed_confirmation_side = str(delayed.get("rti_confirm_side") or "").upper()
    if (
        intermediate_side not in {"YES", "NO"}
        or delayed_confirmation_side not in {"YES", "NO"}
    ):
        return {
            "available": False,
            "error": "v21_confirmation_side_identity_invalid",
            "outcome_labels_read": False,
            "model_fit_performed": False,
            "probability_scoring_performed": False,
        }
    try:
        parent_market = _required(parent_row, parent, "rti_market_mid_probability")
        parent_ask = _required(parent_row, parent, "entry_ask_cents")
        intermediate_continuation = _required(
            intermediate_row, intermediate, "rti_confirm_continuation_bps"
        )
        intermediate_distance = _required(
            intermediate_row, intermediate, "rti_confirm_signed_distance_bps"
        )
        delayed_continuation = _required(
            delayed_row, delayed, "rti_confirm_continuation_bps"
        )
        intermediate_market = _required(
            intermediate_row, intermediate, "rti_market_mid_probability"
        )
        delayed_market = _required(
            delayed_row, delayed, "rti_market_mid_probability"
        )
        intermediate_ask = _required(
            intermediate_row, intermediate, "entry_ask_cents"
        )
        delayed_ask = _required(delayed_row, delayed, "entry_ask_cents")
        intermediate_microprice = sign * _required(
            intermediate_row, intermediate, "kalshi_yes_microprice_edge_cents"
        )
        delayed_microprice = sign * _required(
            delayed_row, delayed, "kalshi_yes_microprice_edge_cents"
        )
        intermediate_spot_move = sign * _required(
            intermediate_row, intermediate, "spot_fast_mid_change_bps_15s"
        )
        delayed_spot_move = sign * _required(
            delayed_row, delayed, "spot_fast_mid_change_bps_15s"
        )
        intermediate_trade = sign * _required(
            intermediate_row, intermediate, "kalshi_trade_imbalance_yes_15s"
        )
        delayed_trade = sign * _required(
            delayed_row, delayed, "kalshi_trade_imbalance_yes_15s"
        )
        intermediate_pressure = _signed_log1p(sign * _required(
            intermediate_row, intermediate, "kalshi_book_delta_pressure_yes_15s"
        ))
        delayed_pressure = _signed_log1p(sign * _required(
            delayed_row, delayed, "kalshi_book_delta_pressure_yes_15s"
        ))
        intermediate_spot_flow = _signed_log1p(sign * _required(
            intermediate_row, intermediate, "spot_depth_trade_net_notional_15s"
        ))
        delayed_spot_flow = _signed_log1p(sign * _required(
            delayed_row, delayed, "spot_depth_trade_net_notional_15s"
        ))
    except ValueError as exc:
        return {
            "available": False,
            "error": str(exc),
            "outcome_labels_read": False,
            "model_fit_performed": False,
            "probability_scoring_performed": False,
        }

    second_leg_continuation = delayed_continuation - intermediate_continuation
    first_market_change = intermediate_market - parent_market
    second_market_change = delayed_market - intermediate_market
    first_ask_change = intermediate_ask - parent_ask
    second_ask_change = delayed_ask - intermediate_ask
    trajectory_values = (
        1.0 if intermediate_side == side else 0.0,
        1.0 if delayed_confirmation_side == side else 0.0,
        _clip(intermediate_continuation, -40.0, 40.0),
        _clip(second_leg_continuation, -40.0, 40.0),
        _clip(second_leg_continuation - intermediate_continuation, -60.0, 60.0),
        _clip(intermediate_distance, -60.0, 60.0),
        _clip(intermediate_market, 0.0, 1.0),
        _clip(first_market_change, -1.0, 1.0),
        _clip(second_market_change, -1.0, 1.0),
        _clip(second_market_change - first_market_change, -1.0, 1.0),
        _clip(intermediate_ask, 0.0, 99.0),
        _clip(first_ask_change, -40.0, 40.0),
        _clip(second_ask_change, -40.0, 40.0),
        _clip(second_ask_change - first_ask_change, -60.0, 60.0),
        _clip(intermediate_microprice, -25.0, 25.0),
        _clip(delayed_microprice - intermediate_microprice, -50.0, 50.0),
        _clip(intermediate_spot_move, -50.0, 50.0),
        _clip(delayed_spot_move - intermediate_spot_move, -80.0, 80.0),
        _clip(intermediate_trade, -1.0, 1.0),
        _clip(delayed_trade - intermediate_trade, -2.0, 2.0),
        intermediate_pressure,
        _clip(delayed_pressure - intermediate_pressure, -30.0, 30.0),
        intermediate_spot_flow,
        _clip(delayed_spot_flow - intermediate_spot_flow, -30.0, 30.0),
    )
    values = (*tuple(base["features"]), *trajectory_values)
    if len(values) != len(FEATURE_NAMES):
        return {
            "available": False,
            "error": "v21_feature_schema_length_mismatch",
            "outcome_labels_read": False,
            "model_fit_performed": False,
            "probability_scoring_performed": False,
        }
    feature_map = dict(zip(FEATURE_NAMES, values, strict=True))
    identity_evidence = {
        "feature_builder_version": FEATURE_BUILDER_VERSION,
        "parent_id": base.get("parent_id"),
        "intermediate_id": intermediate_row.get("id"),
        "delayed_id": base.get("delayed_id"),
        "asset": base.get("asset"),
        "ticker": base.get("ticker"),
        "close_time": base.get("close_time"),
        "side": side,
        "base_feature_evidence_sha256": base.get("feature_evidence_sha256"),
        "feature_names": list(FEATURE_NAMES),
        "features": list(values),
    }
    return {
        "available": True,
        "feature_builder_version": FEATURE_BUILDER_VERSION,
        "asset": base.get("asset"),
        "cohort": base.get("cohort"),
        "ticker": base.get("ticker"),
        "close_time": base.get("close_time"),
        "side": side,
        "parent_id": base.get("parent_id"),
        "intermediate_id": intermediate_row.get("id"),
        "delayed_id": base.get("delayed_id"),
        "feature_names": list(FEATURE_NAMES),
        "features": list(values),
        "feature_map": feature_map,
        "base_feature_evidence_sha256": base.get("feature_evidence_sha256"),
        "feature_evidence_sha256": _canonical_sha256(identity_evidence),
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }
