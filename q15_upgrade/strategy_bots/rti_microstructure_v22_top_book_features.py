"""Outcome-blind V22 official spot top-book feature construction.

This module transforms exact parent/+30s/+60s RTI/Kalshi evidence and four
strictly prospective official REST top-of-book snapshots.  Frozen V21 is not
changed.  Spot-dependent V21 fields are deliberately excluded so BNB/HYPE do
not depend on an unavailable Coinbase L2 source.  This module has no database,
outcome, model, notification, order, or trading capability.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping, Sequence

from q15_upgrade.rti_spot_rest_top_book import (
    DEPTH_SCOPE,
    EVIDENCE_COLUMNS,
    STAGE_DELAY_SECONDS,
)

from . import rti_microstructure_v19 as v19
from . import rti_microstructure_v21 as v21_source
from . import rti_microstructure_v21_features as v21_features
from . import rti_microstructure_v22_identity as v22_identity
from . import rti_spot_rest_top_book_reservoir_identity as rest_identity


FEATURE_BUILDER_VERSION = "q15-rti-v22-official-rest-top-book-features-v1"
STAGES = ("13M", "12M30S", "12M", "11M30S")
STAGE_SUFFIXES = ("13m", "12m30s", "12m", "11m30s")
REST_FEATURE_NAMES = (
    *(f"side_rest_top_imbalance_{suffix}" for suffix in STAGE_SUFFIXES),
    *(f"log1p_rest_spread_bps_{suffix}" for suffix in STAGE_SUFFIXES),
    *(f"log1p_rest_top_notional_{suffix}" for suffix in STAGE_SUFFIXES),
    "side_rest_top_imbalance_mean",
    "side_rest_top_imbalance_min",
    "side_rest_top_imbalance_last_minus_first",
    "side_rest_top_imbalance_slope_per_30s",
    "rest_top_imbalance_side_persistence",
    "log1p_rest_top_imbalance_flip_count",
    "log1p_rest_spread_bps_mean",
    "log1p_rest_spread_bps_max",
    "rest_spread_bps_last_minus_first",
    "side_rest_mid_return_30s_bps",
    "side_rest_mid_return_60s_bps",
    "side_rest_mid_return_90s_bps",
    "side_rest_mid_acceleration_30v90_bps",
    "log1p_rest_mid_path_range_bps",
    "rest_mid_path_trend_efficiency",
    "rest_log_top_notional_mean",
    "rest_log_top_notional_last_minus_first",
)
EXCLUDED_SPOT_DERIVED_FEATURE_NAMES = frozenset({
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
    "intermediate_side_spot_fast_move_15s_bps",
    "side_spot_fast_move_15s_change_30s_bps",
    "intermediate_side_spot_flow_15s_signed_log1p",
    "side_spot_flow_15s_signed_log1p_change_30s",
})
BASE_FEATURE_NAMES = tuple(
    name for name in v21_features.FEATURE_NAMES
    if name not in EXCLUDED_SPOT_DERIVED_FEATURE_NAMES
)
FEATURE_NAMES = (*BASE_FEATURE_NAMES, *REST_FEATURE_NAMES)
ALLOWED_REPLACED_SPOT_SOURCE_FAILURES = frozenset({
    "SPOT_DEPTH_SOURCE_UNUSABLE",
    "SPOT_TIMESTAMP_LINEAGE_MISSING",
    "SPOT_FAST_MID_PATH_IDENTITY_INVALID",
    "SPOT_FAST_MID_PATH_INCOMPLETE",
    "SPOT_FEATURE_VALUES_MISSING",
})
NEUTRAL_SPOT_KEYS = (
    "spot_fast_mid_change_bps_15s",
    "spot_fast_mid_change_bps_60s",
    "spot_fast_mid_range_bps_60s",
    "spot_fast_mid_realized_volatility_bps_60s",
    "spot_fast_mid_trend_efficiency_60s",
    "spot_depth_imbalance",
    "spot_depth_trade_net_notional_5s",
    "spot_depth_trade_net_notional_15s",
    "spot_depth_trade_net_notional_60s",
)
FORBIDDEN_OUTCOME_KEYS = frozenset({
    "outcome", "result", "result_yes", "resolved", "correct", "settlement",
    "settlement_result", "pnl", "profit", "label", "label_survives",
})


def _canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


FEATURE_NAMES_SHA256 = _sha256(list(FEATURE_NAMES))


def _number(value: Any) -> float:
    if value is None or isinstance(value, bool):
        raise ValueError("v22_rest_feature_value_invalid")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("v22_rest_feature_value_invalid") from exc
    if not math.isfinite(number):
        raise ValueError("v22_rest_feature_value_invalid")
    return number


def _clip(value: float, low: float, high: float) -> float:
    return max(float(low), min(float(high), float(value)))


def _walk_keys(value: Any):
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key).lower()
            yield from _walk_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_keys(child)


def _row_evidence_valid(row: Mapping[str, Any]) -> bool:
    raw = row.get("evidence_json")
    digest = str(row.get("evidence_sha256") or "")
    if not isinstance(raw, str) or len(digest) != 64:
        return False
    try:
        decoded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return False
    if (
        not isinstance(decoded, Mapping)
        or raw != _canonical(decoded)
        or hashlib.sha256(raw.encode("utf-8")).hexdigest() != digest
    ):
        return False
    for key in EVIDENCE_COLUMNS:
        if key not in decoded:
            return False
        left = row.get(key)
        right = decoded.get(key)
        try:
            left_number = None if left is None or isinstance(left, bool) else float(left)
            right_number = None if right is None or isinstance(right, bool) else float(right)
        except (TypeError, ValueError):
            left_number = right_number = None
        if left_number is not None or right_number is not None:
            if (
                left_number is None or right_number is None
                or not math.isfinite(left_number) or not math.isfinite(right_number)
                or abs(left_number - right_number) > 1e-9
            ):
                return False
        elif left != right:
            return False
    return True


def _linear_slope(values: Sequence[float]) -> float:
    # Equally spaced at 30 seconds.  The denominator is sum((x-mean)^2)=5.
    mean = sum(values) / 4.0
    return sum((index - 1.5) * (value - mean) for index, value in enumerate(values)) / 5.0


def _profile(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("threshold_json")
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        decoded = json.loads(str(raw or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def _with_neutral_spot_values(row: Mapping[str, Any]) -> dict[str, Any]:
    output = dict(row)
    profile = _profile(row)
    for key in NEUTRAL_SPOT_KEYS:
        output[key] = 0.0
        profile[key] = 0.0
    output["threshold_json"] = profile
    return output


def _triplet_base_features(
    parent_row: Mapping[str, Any],
    intermediate_row: Mapping[str, Any],
    delayed_row: Mapping[str, Any],
) -> dict[str, Any]:
    intermediate = v21_source.evaluate_intermediate_source(
        parent_row, intermediate_row,
    )
    unexpected_intermediate = set(intermediate.get("failures") or ()) - (
        ALLOWED_REPLACED_SPOT_SOURCE_FAILURES
    )
    delayed = v19.evaluate_delayed_source(parent_row, delayed_row)
    unexpected_delayed_reservoir = set(v21_source._reservoir_failures(
        _profile(delayed_row),
    )) - ALLOWED_REPLACED_SPOT_SOURCE_FAILURES
    if (
        unexpected_intermediate
        or delayed.get("available") is not True
        or unexpected_delayed_reservoir
    ):
        raise ValueError("v22_triplet_nonspot_source_incomplete")
    intermediate_evidence = dict(intermediate.get("evidence") or {})
    delayed_evidence = dict(delayed.get("evidence") or {})
    if (
        intermediate_evidence.get("parent_id") != delayed_evidence.get("parent_id")
        or intermediate_evidence.get("asset") != delayed_evidence.get("asset")
        or intermediate_evidence.get("ticker") != delayed_evidence.get("ticker")
        or intermediate_evidence.get("close_time") != delayed_evidence.get("close_time")
        or intermediate_evidence.get("original_side")
        != delayed_evidence.get("original_side")
    ):
        raise ValueError("v22_triplet_lineage_mismatch")
    delayed_profile = _profile(delayed_row)
    if not v21_source._confirmation_side_matches_distance(
        str(delayed_evidence.get("original_side") or "").upper(),
        str(delayed_evidence.get("confirmation_side") or "").upper(),
        v21_source._num(delayed_profile.get("rti_confirm_signed_distance_bps")),
    ):
        raise ValueError("v22_delayed_side_distance_contradiction")
    source_ids = (
        int(intermediate_evidence.get("parent_id") or 0),
        int(intermediate_evidence.get("intermediate_id") or 0),
        int(delayed_evidence.get("delayed_id") or 0),
    )
    if min(source_ids) <= 0 or len(set(source_ids)) != 3:
        raise ValueError("v22_triplet_source_identity_invalid")
    full = v21_features.feature_vector(
        parent_row,
        _with_neutral_spot_values(intermediate_row),
        _with_neutral_spot_values(delayed_row),
    )
    if full.get("available") is not True:
        raise ValueError("v22_triplet_nonspot_feature_source_incomplete")
    feature_map = dict(full.get("feature_map") or {})
    base_features = tuple(_number(feature_map.get(name)) for name in BASE_FEATURE_NAMES)
    if len(base_features) != len(BASE_FEATURE_NAMES):
        raise ValueError("v22_triplet_base_feature_geometry_invalid")
    return {
        "parent_id": source_ids[0],
        "intermediate_id": source_ids[1],
        "delayed_id": source_ids[2],
        "asset": str(delayed_evidence["asset"]),
        "ticker": str(delayed_evidence["ticker"]),
        "close_time": float(delayed_evidence["close_time"]),
        "side": str(delayed_evidence["parent_side"]),
        "features": base_features,
        "execution_supported": (
            delayed_evidence.get("sim_contracts") == 10.0
            and delayed_evidence.get("sim_full_fill_supported") is True
        ),
        "entry_ask_cents": delayed_evidence.get("entry_ask_cents"),
        "spread_cents": delayed_evidence.get("spread_cents"),
        "depth_contracts": delayed_evidence.get("depth_contracts"),
        "sim_contracts": delayed_evidence.get("sim_contracts"),
        "parent_source_evidence_sha256": delayed_evidence.get(
            "parent_feature_evidence_sha256"
        ),
        "intermediate_source_evidence_sha256": intermediate.get(
            "feature_evidence_sha256"
        ),
        "delayed_source_evidence_sha256": delayed.get("feature_evidence_sha256"),
        "replaced_spot_source_failures": sorted(
            set(intermediate.get("failures") or ())
            | set(v21_source._reservoir_failures(_profile(delayed_row)))
        ),
    }


def build_features(
    parent_row: Mapping[str, Any],
    intermediate_row: Mapping[str, Any],
    delayed_row: Mapping[str, Any],
    rest_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return frozen nonspot RTI/Kalshi plus official REST features."""
    if any(
        FORBIDDEN_OUTCOME_KEYS & set(_walk_keys(value))
        for value in (parent_row, intermediate_row, delayed_row, rest_rows)
    ):
        raise ValueError("v22_outcome_or_label_input_forbidden")
    base_row = _triplet_base_features(parent_row, intermediate_row, delayed_row)
    asset = str(base_row.get("asset") or "").upper()
    ticker = str(base_row.get("ticker") or "")
    side = str(base_row.get("side") or "").upper()
    close_time = _number(base_row.get("close_time"))
    parent_id = int(base_row.get("parent_id") or 0)
    base_features = tuple(_number(value) for value in base_row.get("features") or ())
    if (
        FEATURE_BUILDER_VERSION != v22_identity.FEATURE_BUILDER_VERSION
        or FEATURE_NAMES_SHA256 != v22_identity.FEATURE_NAMES_SHA256
        or len(FEATURE_NAMES) != v22_identity.FEATURE_COUNT
        or asset not in rest_identity.SOURCE_IDENTITIES
        or not ticker or side not in {"YES", "NO"} or parent_id <= 0
        or len(base_features) != len(BASE_FEATURE_NAMES)
    ):
        raise ValueError("v22_base_feature_identity_invalid")
    by_stage: dict[str, Mapping[str, Any]] = {}
    for raw_row in rest_rows:
        row = dict(raw_row)
        stage = str(row.get("stage") or "").upper()
        expected_source = rest_identity.SOURCE_IDENTITIES.get(asset)
        if (
            stage not in STAGES or stage in by_stage
            or str(row.get("asset") or "").upper() != asset
            or str(row.get("ticker") or "") != ticker
            or abs(_number(row.get("close_time")) - close_time) > 1e-6
            or row.get("protocol_id") != rest_identity.PROTOCOL_ID
            or row.get("protocol_sha256") != rest_identity.PROTOCOL_SHA256
            or row.get("schema_version") != rest_identity.SCHEMA_VERSION
            or row.get("depth_scope") != DEPTH_SCOPE
            or row.get("status") != "OK" or row.get("failure_reason") is not None
            or expected_source is None
            or (
                str(row.get("provider") or ""),
                str(row.get("symbol") or ""),
                str(row.get("quote_currency") or ""),
            ) != tuple(expected_source)
            or not _row_evidence_valid(row)
        ):
            raise ValueError("v22_rest_source_identity_invalid")
        target = _number(row.get("target_at"))
        submitted = _number(row.get("submitted_at"))
        started = _number(row.get("request_started_at"))
        received = _number(row.get("received_at"))
        start_offset = _number(row.get("request_start_offset_seconds"))
        latency = _number(row.get("response_latency_seconds"))
        receive_offset = _number(row.get("receive_offset_seconds"))
        expected_target = close_time - 780.0 + STAGE_DELAY_SECONDS[stage]
        if (
            abs(target - expected_target) > 1e-6
            or submitted < target or started < submitted or received < started
            or start_offset < 0.0
            or start_offset > rest_identity.MAX_REQUEST_START_OFFSET_SECONDS
            or latency < 0.0
            or latency > rest_identity.MAX_RESPONSE_LATENCY_SECONDS
            or receive_offset < 0.0
            or receive_offset > rest_identity.MAX_RECEIVE_OFFSET_SECONDS
            or abs((started - target) - start_offset) > 1e-6
            or abs((received - started) - latency) > 1e-6
            or abs((received - target) - receive_offset) > 1e-6
        ):
            raise ValueError("v22_rest_timestamp_alignment_invalid")
        source_timestamp = row.get("source_timestamp")
        if source_timestamp is not None:
            source_time = _number(source_timestamp)
            mutation_age = _number(row.get("source_mutation_age_seconds"))
            if (
                abs((received - source_time) - mutation_age) > 1e-6
                or mutation_age < -rest_identity.MAX_EXCHANGE_CLOCK_LEAD_SECONDS
            ):
                raise ValueError("v22_rest_source_timestamp_provenance_invalid")
        by_stage[stage] = row
    if tuple(by_stage) != STAGES and set(by_stage) != set(STAGES):
        raise ValueError("v22_rest_stage_geometry_invalid")
    ordered = [by_stage[stage] for stage in STAGES]
    sign = 1.0 if side == "YES" else -1.0
    imbalance = []
    spread = []
    mids = []
    log_notional = []
    for row in ordered:
        bid = _number(row.get("best_bid"))
        ask = _number(row.get("best_ask"))
        bid_size = _number(row.get("bid_size"))
        ask_size = _number(row.get("ask_size"))
        mid = _number(row.get("mid"))
        row_spread = _number(row.get("spread_bps"))
        row_imbalance = _number(row.get("top_imbalance"))
        if (
            bid <= 0.0 or ask <= 0.0 or bid > ask
            or bid_size <= 0.0 or ask_size <= 0.0 or mid <= 0.0
            or abs(mid - (bid + ask) / 2.0) > 1e-9
            or row_spread < 0.0
            or abs(row_spread - (ask - bid) / mid * 10_000.0) > 1e-6
            or not -1.0 <= row_imbalance <= 1.0
            or abs(row_imbalance - (bid_size - ask_size) / (bid_size + ask_size)) > 1e-9
        ):
            raise ValueError("v22_rest_book_geometry_invalid")
        imbalance.append(sign * row_imbalance)
        spread.append(row_spread)
        mids.append(mid)
        notional = bid * bid_size + ask * ask_size
        log_notional.append(math.log1p(_clip(notional, 0.0, 1_000_000_000_000.0)))

    step_returns = [
        (mids[index] / mids[index - 1] - 1.0) * 10_000.0
        for index in range(1, 4)
    ]
    cumulative = [
        sign * (mids[index] / mids[0] - 1.0) * 10_000.0
        for index in range(1, 4)
    ]
    path_range = (max(mids) - min(mids)) / mids[0] * 10_000.0
    path_distance = sum(abs(value) for value in step_returns)
    efficiency = 0.0 if path_distance <= 1e-12 else abs(sum(step_returns)) / path_distance
    flips = sum(
        (imbalance[index] > 0.0) != (imbalance[index - 1] > 0.0)
        for index in range(1, 4)
        if imbalance[index] != 0.0 and imbalance[index - 1] != 0.0
    )
    rest_features = (
        *(_clip(value, -1.0, 1.0) for value in imbalance),
        *(math.log1p(_clip(value, 0.0, 100.0)) for value in spread),
        *log_notional,
        _clip(sum(imbalance) / 4.0, -1.0, 1.0),
        _clip(min(imbalance), -1.0, 1.0),
        _clip(imbalance[-1] - imbalance[0], -2.0, 2.0),
        _clip(_linear_slope(imbalance), -1.0, 1.0),
        sum(value >= 0.0 for value in imbalance) / 4.0,
        math.log1p(flips),
        math.log1p(_clip(sum(spread) / 4.0, 0.0, 100.0)),
        math.log1p(_clip(max(spread), 0.0, 100.0)),
        _clip(spread[-1] - spread[0], -25.0, 25.0),
        *(_clip(value, -100.0, 100.0) for value in cumulative),
        _clip(cumulative[0] - cumulative[-1] / 3.0, -100.0, 100.0),
        math.log1p(_clip(path_range, 0.0, 200.0)),
        _clip(efficiency, 0.0, 1.0),
        sum(log_notional) / 4.0,
        _clip(log_notional[-1] - log_notional[0], -20.0, 20.0),
    )
    features = (*base_features, *rest_features)
    if len(rest_features) != len(REST_FEATURE_NAMES) or len(features) != len(FEATURE_NAMES):
        raise ValueError("v22_feature_geometry_invalid")
    evidence_core = {
        "feature_builder_version": FEATURE_BUILDER_VERSION,
        "parent_id": parent_id,
        "asset": asset,
        "ticker": ticker,
        "close_time": close_time,
        "side": side,
        "parent_source_evidence_sha256": str(
            base_row["parent_source_evidence_sha256"]
        ),
        "intermediate_source_evidence_sha256": str(
            base_row["intermediate_source_evidence_sha256"]
        ),
        "delayed_source_evidence_sha256": str(
            base_row["delayed_source_evidence_sha256"]
        ),
        "rest_evidence_sha256_by_stage": {
            stage: str(by_stage[stage]["evidence_sha256"]) for stage in STAGES
        },
        "feature_names": list(FEATURE_NAMES),
        "features": list(features),
    }
    return {
        **evidence_core,
        "protocol_id": v22_identity.PROTOCOL_ID,
        "protocol_sha256": v22_identity.PROTOCOL_SHA256,
        "feature_count": len(features),
        "feature_names_sha256": FEATURE_NAMES_SHA256,
        "feature_evidence_sha256": _sha256(evidence_core),
        "intermediate_id": base_row["intermediate_id"],
        "delayed_id": base_row["delayed_id"],
        "execution_supported": base_row["execution_supported"],
        "entry_ask_cents": base_row["entry_ask_cents"],
        "spread_cents": base_row["spread_cents"],
        "depth_contracts": base_row["depth_contracts"],
        "sim_contracts": base_row["sim_contracts"],
        "replaced_spot_source_failures": base_row[
            "replaced_spot_source_failures"
        ],
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "real_trading_allowed": False,
    }
