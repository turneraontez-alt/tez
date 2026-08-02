from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from zoneinfo import ZoneInfo

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v18_identity as v18_identity
from q15_upgrade.strategy_bots import rti_microstructure_v21 as v21
from q15_upgrade.strategy_bots import rti_microstructure_v21_features as features
from q15_upgrade.strategy_bots import rti_microstructure_v21_identity as identity
from tools import q15_rti_v21_readiness as readiness
from tools.q15_rti_delayed_feature_reservoir_readiness import REQUIRED_PERSISTED_KEYS
from tools.q15_rti_microstructure_preregister import design_fingerprint


ASSETS = ("BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP")
EASTERN = ZoneInfo("America/New_York")


def _ticker(asset: str, close_time: float) -> str:
    close = datetime.fromtimestamp(close_time, tz=EASTERN)
    return (
        f"KX{asset}15M-{close:%y}{close:%b}".upper()
        + f"{close:%d%H%M}-{close:%M}"
    )


def _reservoir(target: float) -> dict:
    profile = {key: 0.0 for key in REQUIRED_PERSISTED_KEYS}
    profile.update({
        "delayed_feature_reservoir_version": (
            "q15-rti-delayed-feature-reservoir-v1"
        ),
        "delayed_feature_reservoir_record_only": True,
        "delayed_feature_reservoir_used_for_decision": False,
        "quote_age_seconds": 0.1,
        "quote_age_source": "kalshi_ws_exact_sampler",
        "quote_evidence_source": "kalshi_official_websocket_book",
        "sim_contracts": 10,
        "sim_full_fill_supported": True,
        "kalshi_microstructure_extension_schema_version": "test-v1",
        "kalshi_microstructure_evidence_source": (
            "kalshi_official_websocket_history"
        ),
        "kalshi_microstructure_transport_connected": True,
        "kalshi_microstructure_transport_age_seconds": 0.1,
        "kalshi_microstructure_time_basis": "local_received_at",
        "kalshi_microstructure_captured_at": target + 0.2,
        "spot_depth_status": "ok",
        "spot_depth_source": "coinbase_advanced_l2",
        "rti_spot_book_received_at": target + 0.1,
        "rti_spot_snapshot_created_at": target + 0.15,
        "rti_spot_evidence_as_of": target + 0.2,
        "rti_spot_book_age_s": 0.05,
        "rti_spot_book_source_at": target + 0.1,
        "rti_spot_book_source_age_s": 0.1,
        "spot_fast_mid_path_schema_version": (
            "spot-fast-mid-path-local-observed-v1"
        ),
        "spot_fast_mid_path_time_basis": "local_received_or_captured_at",
    })
    for horizon in (5, 15, 30, 60):
        profile[f"kalshi_microstructure_window_complete_{horizon}s"] = True
        profile[f"kalshi_book_delta_pressure_yes_{horizon}s"] = 10.0 + horizon
        profile[f"kalshi_trade_imbalance_yes_{horizon}s"] = 0.2
    for horizon in (15, 60):
        profile[f"spot_fast_mid_window_complete_{horizon}s"] = True
    profile.update({
        "spot_depth_mid": 100.0,
        "spot_depth_imbalance": 0.25,
        "spot_depth_trade_net_notional_5s": 50.0,
        "spot_depth_trade_net_notional_15s": 100.0,
        "spot_depth_trade_net_notional_60s": 250.0,
        "spot_fast_mid_change_bps_15s": 0.8,
        "spot_fast_mid_change_bps_60s": 1.5,
    })
    return profile


def _parent(
    *, asset: str = "ETH", row_id: int = 101,
    close_time: float = identity.FIRST_ELIGIBLE_CLOSE_TIME,
) -> dict:
    captured = close_time - 780.0 + 0.25
    return {
        "id": row_id,
        "ticker": _ticker(asset, close_time),
        "bot_name": "rti_path_13m",
        "record_kind": "RTI_PATH_13M_PROSPECTIVE_EXACT",
        "interval": "13M",
        "asset": asset,
        "close_time": close_time,
        "side": "YES",
        "entry_ask_cents": 57.0,
        "spread_cents": 1.0,
        "depth_contracts": 30.0,
        "source_captured_at": captured,
        "evidence_as_of": captured + 0.05,
        "threshold_json": {
            "asset_cohort": asset,
            "rti_side": "YES",
            "paper_only": True,
            "passed": True,
            "rule_version": "eth-rti-path-13m-62c-transfer-exact-v3",
            "rti_risk_policy_version": v18_identity.RISK_POLICY_VERSION,
            "rti_reversal_risk_class": "low",
            "rti_reversal_risk_reason_codes": [],
            "rti_path_status": "ok",
            "rti_path_complete": True,
            "rti_path_expected_count": 61,
            "rti_path_count": 61,
            "rti_path_max_receive_age_s": 0.1,
            "rti_decision_age_s": 0.2,
            "rti_timing_offset_s": 0.25,
            "rti_path_evaluation_delay_s": 0.05,
            "quote_age_seconds": 0.1,
            "quote_age_source": "kalshi_ws_exact_sampler",
            "quote_evidence_source": "kalshi_official_websocket_book",
            "rti_signed_distance_bps": 5.0,
            "rti_distance_to_remaining_volatility": 1.2,
            "rti_side_move_bps": 2.0,
            "rti_path_first_half_side_move_bps": 0.5,
            "rti_path_second_half_side_move_bps": 1.5,
            "rti_path_acceleration_bps": 1.0,
            "rti_path_persistence": 0.9,
            "rti_path_trend_efficiency": 0.7,
            "rti_path_strike_crossings": 1,
            "rti_path_seconds_since_last_crossing": 40.0,
            "rti_path_range_bps": 4.0,
            "rti_path_realized_volatility_bps": 2.5,
            "rti_market_mid_probability": 0.58,
            "rti_opposite_depth_contracts": 25.0,
        },
    }


def _stage(parent: dict, delay_seconds: int, row_id: int) -> dict:
    close_time = float(parent["close_time"])
    target = close_time - (780.0 - delay_seconds)
    interval = "12M30S" if delay_seconds == 30 else "12M"
    record_kind = (
        "RTI_PATH_12M30_CONFIRM_PROSPECTIVE"
        if delay_seconds == 30
        else "RTI_PATH_12M_CONFIRM_PROSPECTIVE"
    )
    expected_count = 31 if delay_seconds == 30 else 61
    profile = _reservoir(target)
    profile.update({
        "paper_only": True,
        "rti_confirm_original_row_id": parent["id"],
        "rti_confirm_original_strict_accepted": True,
        "rti_confirm_original_side": "YES",
        "rti_confirm_side": "YES",
        "rti_confirm_path_status": "ok",
        "rti_confirm_path_missing_reason": None,
        "rti_confirm_original_end_px": 100.0,
        "rti_confirm_end_px": 101.0,
        "rti_confirm_target_at": target,
        "rti_confirm_delay_seconds": float(delay_seconds),
        "rti_confirm_quote_captured_at": target + 0.2,
        "rti_confirm_evaluated_at": target + 0.25,
        "rti_confirm_timing_offset_s": 0.2,
        "rti_confirm_evaluation_delay_s": 0.25,
        "rti_confirm_path_complete": True,
        "rti_confirm_path_expected_count": expected_count,
        "rti_confirm_path_count": expected_count,
        "rti_confirm_path_max_receive_age_s": 0.1,
        "rti_confirm_path_decision_age_s": 0.2,
        "rti_confirm_continuation_bps": 1.0 if delay_seconds == 30 else 1.8,
        "rti_confirm_signed_distance_bps": 6.0 if delay_seconds == 30 else 6.8,
        "rti_market_mid_probability": 0.60 if delay_seconds == 30 else 0.62,
        "kalshi_yes_microprice_edge_cents": 1.0 if delay_seconds == 30 else 1.4,
        "kalshi_microprice_change_cents_5s": 0.5,
        "kalshi_microprice_change_cents_60s": 1.0,
        "kalshi_book_delta_pressure_yes_5s": 20.0,
        "kalshi_book_delta_pressure_yes_15s": 30.0 + delay_seconds,
        "kalshi_book_delta_pressure_yes_60s": 50.0,
        "kalshi_trade_imbalance_yes_15s": 0.3 if delay_seconds == 30 else 0.4,
        "kalshi_taker_net_yes_volume_5s": 3.0,
        "kalshi_taker_net_yes_volume_15s": 6.0,
        "kalshi_taker_net_yes_volume_60s": 10.0,
        "kalshi_yes_best_depletion_15s": 4.0,
        "kalshi_yes_best_refill_15s": 7.0,
        "kalshi_no_best_depletion_15s": 2.0,
        "kalshi_no_best_refill_15s": 1.0,
        "spot_fast_mid_change_bps_15s": 0.9 if delay_seconds == 30 else 1.2,
        "spot_fast_mid_change_bps_60s": 2.0,
        "spot_fast_mid_range_bps_60s": 3.0,
        "spot_fast_mid_realized_volatility_bps_60s": 1.5,
        "spot_fast_mid_trend_efficiency_60s": 0.6,
        "spot_depth_imbalance": 0.3,
        "spot_depth_trade_net_notional_5s": 100.0,
        "spot_depth_trade_net_notional_15s": (
            150.0 if delay_seconds == 30 else 180.0
        ),
        "spot_depth_trade_net_notional_60s": 400.0,
    })
    return {
        "id": row_id,
        "ticker": parent["ticker"],
        "bot_name": "rti_path_13m",
        "record_kind": record_kind,
        "interval": interval,
        "asset": parent["asset"],
        "close_time": close_time,
        "side": "YES",
        "paper_only": True,
        "entry_ask_cents": 56.0 if delay_seconds == 30 else 55.0,
        "spread_cents": 1.0,
        "depth_contracts": 20.0,
        "threshold_json": profile,
    }


def _triplet() -> tuple[dict, dict, dict]:
    parent = _parent()
    return parent, _stage(parent, 30, 201), _stage(parent, 60, 202)


def test_v21_protocol_and_evaluator_are_frozen_silent_and_disjoint():
    protocol = v21.load_protocol()
    evaluator = v21.load_evaluator_contract()
    assert design_fingerprint(protocol) == identity.PROTOCOL_SHA256
    assert design_fingerprint(evaluator) == identity.EVALUATOR_CONTRACT_SHA256
    assert protocol["historical_evaluation"]["execution_policy_selection_windows"] == 25
    assert evaluator["partitions"]["probability_calibration"] == [105, 129]
    assert evaluator["partitions"]["execution_policy_selection"] == [130, 154]
    assert protocol["collection"]["outcome_access_allowed_now"] is False
    assert protocol["collection"]["notifications_allowed_now"] is False
    assert protocol["collection"]["real_trading_allowed"] is False


def test_v21_feature_vector_uses_intermediate_trajectory_and_is_deterministic():
    parent, intermediate, delayed = _triplet()
    first = v21.evaluate_triplet(parent, intermediate, delayed)
    second = v21.evaluate_triplet(
        deepcopy(parent), deepcopy(intermediate), deepcopy(delayed)
    )
    assert first["eligible_for_v21_feature_credit"] is True
    assert first["eligible_for_v21_execution_evaluation"] is True
    assert len(first["features"]) == identity.FEATURE_COUNT == 76
    assert first["feature_names"] == list(features.FEATURE_NAMES)
    assert first["source_feature_evidence_sha256"] == second[
        "source_feature_evidence_sha256"
    ]
    feature_map = dict(zip(first["feature_names"], first["features"], strict=True))
    assert feature_map["intermediate_continuation_bps"] == pytest.approx(1.0)
    assert feature_map["intermediate_side_unchanged"] == pytest.approx(1.0)
    assert feature_map["delayed_confirmation_side_unchanged"] == pytest.approx(1.0)
    assert feature_map["second_leg_continuation_bps"] == pytest.approx(0.8)
    assert feature_map["continuation_curvature_bps"] == pytest.approx(-0.2)
    assert first["outcome_labels_read"] is False
    assert first["model_fit_performed"] is False


def test_record_only_execution_ladder_cannot_change_v21_features_or_identity():
    parent, intermediate, delayed = _triplet()
    control = v21.evaluate_triplet(parent, intermediate, delayed)
    for row in (intermediate, delayed):
        row["threshold_json"].update({
            "rti_execution_ladder_schema_version": (
                "kalshi-execution-ladder-10x2c-v1"
            ),
            "rti_ladder_depth_within_2c_contracts": 1000.0,
            "rti_ladder_10_contract_filled_contracts": 10.0,
            "rti_ladder_10_contract_full_fill_supported": True,
            "rti_ladder_10_contract_vwap_cents": 99.0,
            "rti_ladder_10_contract_worst_price_cents": 100.0,
            "rti_ladder_10_contract_slippage_cents": 2.0,
        })
    challenger = v21.evaluate_triplet(parent, intermediate, delayed)
    assert challenger["features"] == control["features"]
    assert challenger["source_feature_evidence_sha256"] == control[
        "source_feature_evidence_sha256"
    ]
    assert challenger["eligible_for_v21_execution_evaluation"] == control[
        "eligible_for_v21_execution_evaluation"
    ]


def test_v21_legitimate_confirmation_reversal_is_measured_not_rejected():
    parent, intermediate, delayed = _triplet()
    delayed["threshold_json"]["rti_confirm_side"] = "NO"
    delayed["threshold_json"]["rti_confirm_signed_distance_bps"] = -1.0
    result = v21.evaluate_triplet(parent, intermediate, delayed)
    assert result["eligible_for_v21_feature_credit"] is True
    feature_map = dict(zip(
        result["feature_names"], result["features"], strict=True,
    ))
    assert feature_map["intermediate_side_unchanged"] == pytest.approx(1.0)
    assert feature_map["delayed_confirmation_side_unchanged"] == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("mutation", "expected_failure"),
    [
        ("contradictory_side_distance", "V21_DELAYED_CONFIRMATION_SIDE_DISTANCE_CONTRADICTION"),
        ("missing_confirmation_side", "V21_DELAYED_ORIGINAL_SIDE_LINEAGE_MISMATCH"),
        ("wrong_original_side", "V21_DELAYED_ORIGINAL_SIDE_LINEAGE_MISMATCH"),
        ("duplicate_source_id", "V21_TRIPLET_SOURCE_IDENTITY_INVALID"),
    ],
)
def test_v21_delayed_lineage_tamper_fails_closed(mutation, expected_failure):
    parent, intermediate, delayed = _triplet()
    if mutation == "contradictory_side_distance":
        delayed["threshold_json"]["rti_confirm_side"] = "NO"
    elif mutation == "missing_confirmation_side":
        delayed["threshold_json"].pop("rti_confirm_side")
    elif mutation == "wrong_original_side":
        delayed["threshold_json"]["rti_confirm_original_side"] = "NO"
    else:
        delayed["id"] = intermediate["id"]
    result = v21.evaluate_triplet(parent, intermediate, delayed)
    assert result["eligible_for_v21_feature_credit"] is False
    assert expected_failure in result["failures"]


def test_v21_intermediate_confirmation_side_distance_contradiction_fails_closed():
    parent, intermediate, delayed = _triplet()
    intermediate["threshold_json"]["rti_confirm_side"] = "NO"
    result = v21.evaluate_triplet(parent, intermediate, delayed)
    assert result["eligible_for_v21_feature_credit"] is False
    assert "INTERMEDIATE_CONFIRMATION_SIDE_DISTANCE_CONTRADICTION" in result["failures"]


def test_v21_feature_credit_is_separate_from_real_row_level_execution():
    parent, intermediate, delayed = _triplet()
    delayed["threshold_json"]["sim_full_fill_supported"] = False
    result = v21.evaluate_triplet(parent, intermediate, delayed)
    assert result["eligible_for_v21_feature_credit"] is True
    assert result["eligible_for_v21_execution_evaluation"] is False
    assert result["evidence"]["execution_supported"] is False


def test_v21_source_fails_closed_on_timestamp_lineage_and_quality_tamper():
    parent, intermediate, delayed = _triplet()
    intermediate["threshold_json"]["rti_confirm_original_row_id"] = 999
    intermediate["threshold_json"]["rti_confirm_path_count"] = 30
    intermediate["threshold_json"]["kalshi_microstructure_transport_connected"] = False
    result = v21.evaluate_triplet(parent, intermediate, delayed)
    assert result["eligible_for_v21_feature_credit"] is False
    assert "INTERMEDIATE_PARENT_CONTRACT_IDENTITY" in result["failures"]
    assert "FRESH_31_SAMPLE_RTI_PATH" in result["failures"]
    assert "KALSHI_MICROSTRUCTURE_TRANSPORT_STALE" in result["failures"]


def test_v21_readiness_counts_feature_window_but_only_real_executable_rows():
    parents = []
    trajectory = []
    close_time = identity.FIRST_ELIGIBLE_CLOSE_TIME
    for index, asset in enumerate(ASSETS, start=1):
        parent = _parent(asset=asset, row_id=1000 + index, close_time=close_time)
        intermediate = _stage(parent, 30, 2000 + index)
        delayed = _stage(parent, 60, 3000 + index)
        if asset == "HYPE":
            delayed["threshold_json"]["sim_full_fill_supported"] = False
        parents.append(parent)
        trajectory.extend((intermediate, delayed))
    report = readiness.build_readiness(parents, trajectory)
    assert report["v21_feature_complete_close_windows"] == 1
    assert report["feature_rows"] == 7
    assert report["row_level_executable_feature_rows"] == 6
    assert report["all_seven_executable_close_windows_diagnostic_only"] == 0
    assert report["feature_credit_requires_all_rows_executable"] is False
    assert report["pnl_credit_requires_row_level_execution_supported"] is True
    assert report["outcome_labels_read"] is False
    assert report["notification_eligible"] is False
    assert report["real_trading_allowed"] is False

    missing = readiness.build_readiness(parents, trajectory[:-2])
    assert missing["v21_feature_complete_close_windows"] == 0
    assert missing["missing_intermediate_pairs"] == 1
    assert missing["missing_delayed_pairs"] == 1


def test_v21_tampered_protocol_or_evaluator_fails_closed(tmp_path):
    protocol = deepcopy(v21.load_protocol())
    protocol["collection"]["telegram_allowed_now"] = True
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(protocol), encoding="utf-8")
    with pytest.raises(ValueError, match="identity_or_safety"):
        v21.load_protocol(path)

    evaluator = deepcopy(v21.load_evaluator_contract())
    evaluator["partitions"]["execution_policy_selection"] = [129, 154]
    path = tmp_path / "evaluator.json"
    path.write_text(json.dumps(evaluator), encoding="utf-8")
    with pytest.raises(ValueError, match="identity_or_safety"):
        v21.load_evaluator_contract(path)
