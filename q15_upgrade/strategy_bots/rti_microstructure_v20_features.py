"""Outcome-blind feature map for the V20 delayed reversal-hazard study."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping

from . import rti_microstructure_v19 as v19


FEATURE_BUILDER_VERSION = "q15-rti-v20-delayed-reversal-features-v1"
NON_BTC_ASSETS = ("BNB", "DOGE", "ETH", "HYPE", "SOL", "XRP")
FEATURE_NAMES = (
    "side_is_yes",
    "parent_distance_bps",
    "parent_distance_to_remaining_volatility",
    "parent_side_move_bps",
    "parent_first_half_move_bps",
    "parent_second_half_move_bps",
    "parent_acceleration_bps",
    "parent_persistence",
    "parent_trend_efficiency",
    "log1p_parent_crossings",
    "parent_seconds_since_crossing_fraction",
    "log1p_parent_range_bps",
    "log1p_parent_realized_volatility_bps",
    "parent_market_side_probability",
    "parent_ask_cents",
    "parent_spread_cents",
    "log1p_parent_side_depth",
    "parent_log_depth_ratio",
    "delayed_side_unchanged",
    "delayed_continuation_bps",
    "delayed_distance_bps",
    "delayed_distance_change_bps",
    "delayed_ask_change_cents",
    "delayed_market_probability_change",
    "side_microprice_edge_cents",
    "side_microprice_change_60s",
    "side_microprice_acceleration_5v60",
    "side_book_pressure_15s_signed_log1p",
    "side_book_pressure_acceleration_5v60",
    "side_trade_imbalance_15s",
    "side_taker_flow_15s_signed_log1p",
    "side_taker_flow_acceleration_5v60",
    "side_best_level_support_15s",
    "side_spot_fast_move_60s_bps",
    "side_spot_fast_acceleration_15v60_bps",
    "log1p_spot_fast_range_60s_bps",
    "log1p_spot_fast_volatility_60s_bps",
    "spot_fast_trend_efficiency_60s",
    "side_spot_book_imbalance",
    "side_spot_net_notional_15s_signed_log1p",
    "side_spot_flow_acceleration_5v60",
    "delayed_distance_to_spot_remaining_volatility",
    "kalshi_spot_direction_agreement_60s",
    "log1p_delayed_depth",
    "delayed_spread_cents",
    "delayed_market_side_probability",
    *(f"asset_{asset.lower()}" for asset in NON_BTC_ASSETS),
)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clip(value: float, low: float, high: float) -> float:
    return max(float(low), min(float(high), float(value)))


def _signed_log1p(value: float, cap: float = 1_000_000_000.0) -> float:
    clipped = _clip(value, -cap, cap)
    return math.copysign(math.log1p(abs(clipped)), clipped)


def _profile(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("threshold_json")
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        decoded = json.loads(str(raw or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def _value(
    row: Mapping[str, Any], profile: Mapping[str, Any], key: str,
) -> Any:
    value = row.get(key)
    return profile.get(key) if value is None else value


def _required_number(
    row: Mapping[str, Any], profile: Mapping[str, Any], key: str,
) -> float:
    number = _num(_value(row, profile, key))
    if number is None:
        raise ValueError(f"feature_missing:{key}")
    return number


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def feature_vector(
    parent_row: Mapping[str, Any], delayed_row: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the fixed point-in-time vector; no outcome field is accepted."""
    source = v19.evaluate_delayed_source(parent_row, delayed_row)
    if source.get("available") is not True:
        return {
            "available": False,
            "error": "source_incomplete",
            "source_failures": list(source.get("failures") or ()),
            "outcome_labels_read": False,
            "model_fit_performed": False,
            "probability_scoring_performed": False,
        }
    parent = _profile(parent_row)
    delayed = _profile(delayed_row)
    evidence = dict(source["evidence"])
    asset = str(evidence.get("asset") or "").upper()
    side = str(evidence.get("parent_side") or "").upper()
    if asset not in {"BTC", *NON_BTC_ASSETS} or side not in {"YES", "NO"}:
        return {
            "available": False,
            "error": "asset_or_side_identity",
            "outcome_labels_read": False,
            "model_fit_performed": False,
            "probability_scoring_performed": False,
        }
    sign = 1.0 if side == "YES" else -1.0
    try:
        parent_distance = _required_number(
            parent_row, parent, "rti_signed_distance_bps"
        )
        parent_distance_normalized = _required_number(
            parent_row, parent, "rti_distance_to_remaining_volatility"
        )
        parent_side_move = _required_number(
            parent_row, parent, "rti_side_move_bps"
        )
        parent_first = _required_number(
            parent_row, parent, "rti_path_first_half_side_move_bps"
        )
        parent_second = _required_number(
            parent_row, parent, "rti_path_second_half_side_move_bps"
        )
        parent_acceleration = _required_number(
            parent_row, parent, "rti_path_acceleration_bps"
        )
        parent_persistence = _required_number(
            parent_row, parent, "rti_path_persistence"
        )
        parent_efficiency = _required_number(
            parent_row, parent, "rti_path_trend_efficiency"
        )
        parent_crossings = max(0.0, _required_number(
            parent_row, parent, "rti_path_strike_crossings"
        ))
        seconds_since_crossing = _num(
            _value(
                parent_row, parent, "rti_path_seconds_since_last_crossing"
            )
        )
        parent_range = max(0.0, _required_number(
            parent_row, parent, "rti_path_range_bps"
        ))
        parent_volatility = max(0.0, _required_number(
            parent_row, parent, "rti_path_realized_volatility_bps"
        ))
        parent_market = _required_number(
            parent_row, parent, "rti_market_mid_probability"
        )
        parent_ask = _required_number(
            parent_row, parent, "entry_ask_cents"
        )
        parent_spread = _required_number(
            parent_row, parent, "spread_cents"
        )
        parent_depth = max(0.0, _required_number(
            parent_row, parent, "depth_contracts"
        ))
        parent_opposite_depth = max(0.0, _required_number(
            parent_row, parent, "rti_opposite_depth_contracts"
        ))

        continuation = _required_number(
            delayed_row, delayed, "rti_confirm_continuation_bps"
        )
        delayed_distance = _required_number(
            delayed_row, delayed, "rti_confirm_signed_distance_bps"
        )
        delayed_ask = _required_number(
            delayed_row, delayed, "entry_ask_cents"
        )
        delayed_spread = _required_number(
            delayed_row, delayed, "spread_cents"
        )
        delayed_depth = max(0.0, _required_number(
            delayed_row, delayed, "depth_contracts"
        ))
        delayed_market = _required_number(
            delayed_row, delayed, "rti_market_mid_probability"
        )
        yes_microprice_edge = _required_number(
            delayed_row, delayed, "kalshi_yes_microprice_edge_cents"
        )
        microprice_5 = _required_number(
            delayed_row, delayed, "kalshi_microprice_change_cents_5s"
        )
        microprice_60 = _required_number(
            delayed_row, delayed, "kalshi_microprice_change_cents_60s"
        )
        pressure_5 = _required_number(
            delayed_row, delayed, "kalshi_book_delta_pressure_yes_5s"
        )
        pressure_15 = _required_number(
            delayed_row, delayed, "kalshi_book_delta_pressure_yes_15s"
        )
        pressure_60 = _required_number(
            delayed_row, delayed, "kalshi_book_delta_pressure_yes_60s"
        )
        trade_imbalance_15 = _required_number(
            delayed_row, delayed, "kalshi_trade_imbalance_yes_15s"
        )
        taker_5 = _required_number(
            delayed_row, delayed, "kalshi_taker_net_yes_volume_5s"
        )
        taker_15 = _required_number(
            delayed_row, delayed, "kalshi_taker_net_yes_volume_15s"
        )
        taker_60 = _required_number(
            delayed_row, delayed, "kalshi_taker_net_yes_volume_60s"
        )
        selected_depletion_15 = _required_number(
            delayed_row,
            delayed,
            (
                "kalshi_yes_best_depletion_15s"
                if side == "YES"
                else "kalshi_no_best_depletion_15s"
            ),
        )
        selected_refill_15 = _required_number(
            delayed_row,
            delayed,
            (
                "kalshi_yes_best_refill_15s"
                if side == "YES"
                else "kalshi_no_best_refill_15s"
            ),
        )
        spot_move_15 = _required_number(
            delayed_row, delayed, "spot_fast_mid_change_bps_15s"
        )
        spot_move_60 = _required_number(
            delayed_row, delayed, "spot_fast_mid_change_bps_60s"
        )
        spot_range_60 = max(0.0, _required_number(
            delayed_row, delayed, "spot_fast_mid_range_bps_60s"
        ))
        spot_volatility_60 = max(0.0, _required_number(
            delayed_row,
            delayed,
            "spot_fast_mid_realized_volatility_bps_60s",
        ))
        spot_efficiency_60 = _required_number(
            delayed_row, delayed, "spot_fast_mid_trend_efficiency_60s"
        )
        spot_imbalance = _required_number(
            delayed_row, delayed, "spot_depth_imbalance"
        )
        spot_flow_5 = _required_number(
            delayed_row, delayed, "spot_depth_trade_net_notional_5s"
        )
        spot_flow_15 = _required_number(
            delayed_row, delayed, "spot_depth_trade_net_notional_15s"
        )
        spot_flow_60 = _required_number(
            delayed_row, delayed, "spot_depth_trade_net_notional_60s"
        )
    except ValueError as exc:
        return {
            "available": False,
            "error": str(exc),
            "outcome_labels_read": False,
            "model_fit_performed": False,
            "probability_scoring_performed": False,
        }

    delayed_side = str(evidence.get("delayed_side") or "").upper()
    no_crossing = parent_crossings <= 0.0
    crossing_fraction = (
        1.0
        if no_crossing
        else _clip(float(seconds_since_crossing or 0.0) / 61.0, 0.0, 1.0)
    )
    microprice_acceleration = sign * (
        microprice_5 / 5.0 - microprice_60 / 60.0
    ) * 15.0
    pressure_acceleration = sign * (
        pressure_5 / 5.0 - pressure_60 / 60.0
    ) * 15.0
    taker_acceleration = sign * (
        taker_5 / 5.0 - taker_60 / 60.0
    ) * 15.0
    side_spot_move_60 = sign * spot_move_60
    spot_acceleration = sign * (
        spot_move_15 - spot_move_60 / 4.0
    )
    side_spot_flow_15 = sign * spot_flow_15
    spot_flow_acceleration = sign * (
        spot_flow_5 / 5.0 - spot_flow_60 / 60.0
    ) * 15.0
    remaining_spot_volatility = spot_volatility_60 * math.sqrt(720.0 / 60.0)
    distance_to_spot_volatility = (
        10.0
        if remaining_spot_volatility <= 1e-9 and abs(delayed_distance) > 0.0
        else 0.0
        if remaining_spot_volatility <= 1e-9
        else abs(delayed_distance) / remaining_spot_volatility
    )
    direction_agreement = _clip(
        continuation * side_spot_move_60 / 25.0, -1.0, 1.0
    )

    values = (
        1.0 if side == "YES" else 0.0,
        _clip(parent_distance, -40.0, 40.0),
        _clip(parent_distance_normalized, 0.0, 10.0),
        _clip(parent_side_move, -30.0, 30.0),
        _clip(parent_first, -30.0, 30.0),
        _clip(parent_second, -30.0, 30.0),
        _clip(parent_acceleration, -40.0, 40.0),
        _clip(parent_persistence, 0.0, 1.0),
        _clip(parent_efficiency, 0.0, 1.0),
        math.log1p(_clip(parent_crossings, 0.0, 60.0)),
        crossing_fraction,
        math.log1p(_clip(parent_range, 0.0, 200.0)),
        math.log1p(_clip(parent_volatility, 0.0, 200.0)),
        _clip(parent_market, 0.0, 1.0),
        _clip(parent_ask, 0.0, 99.0),
        _clip(parent_spread, 0.0, 20.0),
        math.log1p(_clip(parent_depth, 0.0, 1_000_000.0)),
        _clip(
            math.log((parent_depth + 1.0) / (parent_opposite_depth + 1.0)),
            -10.0,
            10.0,
        ),
        1.0 if delayed_side == side else 0.0,
        _clip(continuation, -40.0, 40.0),
        _clip(delayed_distance, -60.0, 60.0),
        _clip(delayed_distance - parent_distance, -40.0, 40.0),
        _clip(delayed_ask - parent_ask, -40.0, 40.0),
        _clip(delayed_market - parent_market, -1.0, 1.0),
        _clip(sign * yes_microprice_edge, -25.0, 25.0),
        _clip(sign * microprice_60, -50.0, 50.0),
        _clip(microprice_acceleration, -50.0, 50.0),
        _signed_log1p(sign * pressure_15),
        _signed_log1p(pressure_acceleration),
        _clip(sign * trade_imbalance_15, -1.0, 1.0),
        _signed_log1p(sign * taker_15),
        _signed_log1p(taker_acceleration),
        _signed_log1p(selected_refill_15 - selected_depletion_15),
        _clip(side_spot_move_60, -50.0, 50.0),
        _clip(spot_acceleration, -50.0, 50.0),
        math.log1p(_clip(spot_range_60, 0.0, 200.0)),
        math.log1p(_clip(spot_volatility_60, 0.0, 200.0)),
        _clip(spot_efficiency_60, 0.0, 1.0),
        _clip(sign * spot_imbalance, -1.0, 1.0),
        _signed_log1p(side_spot_flow_15),
        _signed_log1p(spot_flow_acceleration),
        _clip(distance_to_spot_volatility, 0.0, 10.0),
        direction_agreement,
        math.log1p(_clip(delayed_depth, 0.0, 1_000_000.0)),
        _clip(delayed_spread, 0.0, 20.0),
        _clip(delayed_market, 0.0, 1.0),
        *(1.0 if asset == candidate else 0.0 for candidate in NON_BTC_ASSETS),
    )
    if len(values) != len(FEATURE_NAMES):
        return {
            "available": False,
            "error": "feature_schema_length_mismatch",
            "outcome_labels_read": False,
            "model_fit_performed": False,
            "probability_scoring_performed": False,
        }
    feature_map = dict(zip(FEATURE_NAMES, values))
    identity_evidence = {
        "feature_builder_version": FEATURE_BUILDER_VERSION,
        "parent_id": evidence.get("parent_id"),
        "delayed_id": evidence.get("delayed_id"),
        "asset": asset,
        "ticker": evidence.get("ticker"),
        "close_time": evidence.get("close_time"),
        "side": side,
        "source_feature_evidence_sha256": source.get(
            "feature_evidence_sha256"
        ),
        "feature_names": list(FEATURE_NAMES),
        "features": list(values),
    }
    return {
        "available": True,
        "feature_builder_version": FEATURE_BUILDER_VERSION,
        "asset": asset,
        "cohort": "BTC" if asset == "BTC" else "NON_BTC_TRANSFER",
        "ticker": evidence.get("ticker"),
        "close_time": evidence.get("close_time"),
        "side": side,
        "parent_id": evidence.get("parent_id"),
        "delayed_id": evidence.get("delayed_id"),
        "feature_names": list(FEATURE_NAMES),
        "features": list(values),
        "feature_map": feature_map,
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
