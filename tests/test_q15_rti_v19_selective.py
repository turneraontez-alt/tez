from __future__ import annotations

from copy import deepcopy
import json

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v18_identity as v18_identity
from q15_upgrade.strategy_bots import rti_microstructure_v19 as v19
from q15_upgrade.strategy_bots import rti_microstructure_v19_identity as identity
from q15_upgrade.strategy_bots import rti_microstructure_v20 as v20
from q15_upgrade.strategy_bots import rti_microstructure_v20_features as v20_features
from q15_upgrade.strategy_bots import rti_microstructure_v20_identity as v20_identity
from tools.q15_rti_microstructure_preregister import design_fingerprint
from tools import q15_rti_v20_readiness as v20_readiness


def _parent() -> dict:
    close = identity.FIRST_ELIGIBLE_CLOSE_TIME
    captured = close - 780.0 + 0.25
    return {
        "id": 101,
        "ticker": "KXETH15M-V19",
        "bot_name": "rti_path_13m",
        "record_kind": "RTI_PATH_13M_PROSPECTIVE_EXACT",
        "interval": "13M",
        "asset": "ETH",
        "close_time": close,
        "side": "YES",
        "entry_ask_cents": 57.0,
        "spread_cents": 1.0,
        "source_captured_at": captured,
        "evidence_as_of": captured + 0.05,
        "threshold_json": {
            "asset_cohort": "ETH",
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
        },
    }


def _delayed() -> dict:
    parent = _parent()
    close = float(parent["close_time"])
    target = close - 720.0
    captured = target + 0.2
    return {
        "id": 202,
        "ticker": parent["ticker"],
        "bot_name": "rti_path_13m",
        "record_kind": "RTI_PATH_12M_CONFIRM_PROSPECTIVE",
        "interval": "12M",
        "asset": "ETH",
        "close_time": close,
        "side": "YES",
        "paper_only": True,
        "entry_ask_cents": 55.0,
        "spread_cents": 1.0,
        "depth_contracts": 20.0,
        "threshold_json": {
            "paper_only": True,
            "rti_confirm_original_row_id": parent["id"],
            "rti_confirm_original_strict_accepted": True,
            "rti_confirm_original_side": "YES",
            "rti_confirm_side": "YES",
            "rti_confirm_target_at": target,
            "rti_confirm_delay_seconds": 60.0,
            "rti_confirm_quote_captured_at": captured,
            "rti_confirm_evaluated_at": captured + 0.05,
            "rti_confirm_timing_offset_s": 0.2,
            "rti_confirm_evaluation_delay_s": 0.25,
            "rti_confirm_path_complete": True,
            "rti_confirm_path_expected_count": 61,
            "rti_confirm_path_count": 61,
            "rti_confirm_path_max_receive_age_s": 0.1,
            "rti_confirm_path_decision_age_s": 0.2,
            "quote_age_seconds": 0.1,
            "quote_age_source": "kalshi_ws_exact_sampler",
            "quote_evidence_source": "kalshi_official_websocket_book",
            "sim_contracts": 10,
            "sim_full_fill_supported": True,
        },
    }


def _v20_pair() -> tuple[dict, dict]:
    parent = _parent()
    delayed = _delayed()
    close = v20_identity.FIRST_ELIGIBLE_CLOSE_TIME
    parent_captured = close - 780.0 + 0.25
    parent.update({
        "close_time": close,
        "source_captured_at": parent_captured,
        "evidence_as_of": parent_captured + 0.05,
        "depth_contracts": 30.0,
    })
    parent["threshold_json"].update({
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
    })
    target = close - 720.0
    delayed_captured = target + 0.2
    delayed.update({
        "close_time": close,
        "depth_contracts": 40.0,
    })
    delayed["threshold_json"].update({
        "rti_confirm_target_at": target,
        "rti_confirm_quote_captured_at": delayed_captured,
        "rti_confirm_evaluated_at": delayed_captured + 0.05,
        "rti_confirm_continuation_bps": 1.5,
        "rti_confirm_signed_distance_bps": 6.5,
        "rti_market_mid_probability": 0.61,
        "kalshi_yes_microprice_edge_cents": 1.2,
        "kalshi_microprice_change_cents_5s": 0.5,
        "kalshi_microprice_change_cents_60s": 1.0,
        "kalshi_book_delta_pressure_yes_5s": 20.0,
        "kalshi_book_delta_pressure_yes_15s": 30.0,
        "kalshi_book_delta_pressure_yes_60s": 50.0,
        "kalshi_trade_imbalance_yes_15s": 0.4,
        "kalshi_taker_net_yes_volume_5s": 3.0,
        "kalshi_taker_net_yes_volume_15s": 6.0,
        "kalshi_taker_net_yes_volume_60s": 10.0,
        "kalshi_yes_best_depletion_15s": 4.0,
        "kalshi_yes_best_refill_15s": 7.0,
        "kalshi_no_best_depletion_15s": 2.0,
        "kalshi_no_best_refill_15s": 1.0,
        "spot_fast_mid_change_bps_15s": 1.2,
        "spot_fast_mid_change_bps_60s": 2.0,
        "spot_fast_mid_range_bps_60s": 3.0,
        "spot_fast_mid_realized_volatility_bps_60s": 1.5,
        "spot_fast_mid_trend_efficiency_60s": 0.6,
        "spot_depth_imbalance": 0.3,
        "spot_depth_trade_net_notional_5s": 100.0,
        "spot_depth_trade_net_notional_15s": 180.0,
        "spot_depth_trade_net_notional_60s": 400.0,
    })
    return parent, delayed


def _v20_window() -> tuple[list[dict], list[dict]]:
    parents = []
    delayed_rows = []
    for index, asset in enumerate(("BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP"), start=1):
        parent, delayed = _v20_pair()
        parent["id"] = 1000 + index
        parent["asset"] = asset
        parent["ticker"] = f"KX{asset}15M-26AUG011145-45"
        parent["threshold_json"]["asset_cohort"] = asset
        delayed["id"] = 2000 + index
        delayed["asset"] = asset
        delayed["ticker"] = parent["ticker"]
        delayed["threshold_json"]["rti_confirm_original_row_id"] = parent["id"]
        parents.append(parent)
        delayed_rows.append(delayed)
    return parents, delayed_rows


def test_protocol_is_frozen_small_sample_disclosed_and_silent():
    protocol = v19.load_protocol()
    assert design_fingerprint(protocol) == identity.PROTOCOL_SHA256
    disclosure = protocol["development_selection_disclosure"]
    assert disclosure["development_trades"] == 6
    assert disclosure["robustness_screen_passed"] is False
    assert protocol["collection"]["notifications_allowed_now"] is False
    assert protocol["collection"]["real_trading_allowed"] is False


def test_valid_fresh_pair_is_eligible_without_outcomes():
    result = v19.evaluate_pair(_parent(), _delayed())
    assert result["eligible"] is True
    assert result["decision"] == "YES"
    assert result["outcome_labels_read"] is False
    assert result["notification_eligible"] is False
    assert result["real_trading_allowed"] is False
    assert result["evidence"]["capture_gap_from_parent_seconds"] == pytest.approx(59.95)


def test_rule_fails_closed_on_side_price_spread_and_depth():
    delayed = _delayed()
    delayed["side"] = "NO"
    delayed["threshold_json"]["rti_confirm_side"] = "NO"
    delayed["entry_ask_cents"] = 63.0
    delayed["spread_cents"] = 1.6
    delayed["depth_contracts"] = 9.0
    result = v19.evaluate_pair(_parent(), delayed)
    assert result["available"] is True
    assert result["eligible"] is False
    assert "OFFICIAL_RTI_SIDE_DID_NOT_REMAIN_SAME" in result["failures"]
    assert "NEW_ASK_MAX_62" in result["failures"]
    assert "NEW_SPREAD_0_TO_1_5" in result["failures"]
    assert "NEW_DEPTH_SUPPORTS_10" in result["failures"]


def test_rule_fails_closed_when_full_ten_contract_fill_is_not_supported():
    delayed = _delayed()
    delayed["threshold_json"]["sim_full_fill_supported"] = False
    result = v19.evaluate_pair(_parent(), delayed)
    assert result["eligible"] is False
    assert "NEW_BOOK_FULL_FILL_NOT_SUPPORTED" in result["failures"]


def test_timestamp_lineage_parent_and_path_fail_closed():
    parent = _parent()
    delayed = _delayed()
    delayed["threshold_json"]["rti_confirm_original_row_id"] = 999
    delayed["threshold_json"]["rti_confirm_path_count"] = 60
    delayed["threshold_json"]["rti_confirm_quote_captured_at"] += 3.0
    result = v19.evaluate_pair(parent, delayed)
    assert result["eligible"] is False
    assert "PARENT_CONTRACT_IDENTITY" in result["failures"]
    assert "FRESH_61_SECOND_RTI_PATH" in result["failures"]
    assert "FRESH_60S_CAPTURE_TIMING" in result["failures"]

    parent = _parent()
    parent["threshold_json"]["rti_reversal_risk_class"] = "medium"
    result = v19.evaluate_pair(parent, _delayed())
    assert result["eligible"] is False
    assert "PARENT_V18_NOT_ELIGIBLE" in result["failures"]


def test_record_quote_source_and_evaluation_timestamp_identity_fail_closed():
    delayed = _delayed()
    delayed["record_kind"] = "UNRELATED_ROW"
    delayed["threshold_json"]["quote_evidence_source"] = "untrusted_cache"
    delayed["threshold_json"]["rti_confirm_evaluation_delay_s"] = 0.9
    result = v19.evaluate_pair(_parent(), delayed)
    assert result["eligible"] is False
    assert "DELAYED_RECORD_KIND_IDENTITY" in result["failures"]
    assert "OFFICIAL_NEW_QUOTE_SOURCE_IDENTITY" in result["failures"]
    assert "FRESH_60S_EVALUATION_TIMING" in result["failures"]


def test_boundary_and_tampered_protocol_fail_closed(tmp_path):
    parent = _parent()
    delayed = _delayed()
    parent["close_time"] = identity.PROSPECTIVE_AFTER_CLOSE_TIME
    delayed["close_time"] = identity.PROSPECTIVE_AFTER_CLOSE_TIME
    result = v19.evaluate_pair(parent, delayed)
    assert result["eligible"] is False
    assert "STRICTLY_PROSPECTIVE_CLOSE_REQUIRED" in result["failures"]

    protocol = deepcopy(v19.load_protocol())
    protocol["collection"]["telegram_allowed_now"] = True
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(protocol), encoding="utf-8")
    with pytest.raises(ValueError, match="identity_or_safety"):
        v19.load_protocol(path)


def test_v20_protocol_is_hash_bound_silent_and_disjoint():
    protocol = v20.load_protocol()
    assert design_fingerprint(protocol) == v20_identity.PROTOCOL_SHA256
    assert protocol["historical_evaluation"]["train_windows"] == 90
    assert protocol["historical_evaluation"]["calibration_windows"] == 30
    assert protocol["historical_evaluation"]["untouched_test_windows"] == 30
    assert protocol["historical_evaluation"][
        "calibration_rows_never_part_of_internal_validation"
    ] is True
    assert protocol["collection"]["outcome_access_allowed_now"] is False
    assert protocol["collection"]["model_fit_allowed_now"] is False
    assert protocol["collection"]["notifications_allowed_now"] is False
    assert protocol["collection"]["real_trading_allowed"] is False


def test_v20_fixed_feature_vector_is_complete_deterministic_and_label_free():
    parent, delayed = _v20_pair()
    first = v20.evaluate_pair(parent, delayed)
    second = v20.evaluate_pair(deepcopy(parent), deepcopy(delayed))
    assert first["available"] is True
    assert first["eligible_for_v20_feature_credit"] is True
    assert len(first["features"]) == v20_identity.FEATURE_COUNT
    assert first["feature_names"] == list(v20_features.FEATURE_NAMES)
    assert first["source_feature_evidence_sha256"] == second[
        "source_feature_evidence_sha256"
    ]
    assert first["outcome_columns_selected"] is False
    assert first["outcome_labels_read"] is False
    assert first["model_fit_performed"] is False
    assert first["probability_scoring_performed"] is False
    assert first["notification_eligible"] is False
    assert first["real_trading_allowed"] is False


def test_v20_missing_signal_and_pre_boundary_close_fail_closed():
    parent, delayed = _v20_pair()
    delayed["threshold_json"].pop("spot_fast_mid_change_bps_60s")
    result = v20.evaluate_pair(parent, delayed)
    assert result["available"] is False
    assert "V20_FEATURE_SOURCE_INCOMPLETE" in result["failures"]

    parent, delayed = _v20_pair()
    prior_close = v20_identity.PROSPECTIVE_AFTER_CLOSE_TIME
    parent["close_time"] = delayed["close_time"] = prior_close
    parent_captured = prior_close - 780.0 + 0.25
    parent["source_captured_at"] = parent_captured
    parent["evidence_as_of"] = parent_captured + 0.05
    target = prior_close - 720.0
    delayed["threshold_json"].update({
        "rti_confirm_target_at": target,
        "rti_confirm_quote_captured_at": target + 0.2,
        "rti_confirm_evaluated_at": target + 0.25,
    })
    result = v20.evaluate_pair(parent, delayed)
    assert result["available"] is False
    assert "STRICTLY_PROSPECTIVE_V20_CLOSE_REQUIRED" in result["failures"]


def test_v20_feature_credit_requires_full_ten_contract_fill_support():
    parent, delayed = _v20_pair()
    delayed["threshold_json"]["sim_full_fill_supported"] = False
    result = v20.evaluate_pair(parent, delayed)
    assert result["available"] is False
    assert result["eligible_for_v20_feature_credit"] is False
    assert "V20_FULL_TEN_CONTRACT_FILL_SUPPORT_REQUIRED" in result["failures"]

    parents, delayed_rows = _v20_window()
    delayed_rows[0]["threshold_json"]["sim_full_fill_supported"] = False
    report = v20_readiness.build_readiness(parents, delayed_rows)
    assert report["v20_feature_complete_close_windows"] == 0
    assert report["feature_rows"] == 0
    assert report["feature_failure_counts"] == {
        "V20_FULL_TEN_CONTRACT_FILL_SUPPORT_REQUIRED": 1,
    }


def test_v20_readiness_credits_only_complete_seven_asset_clusters():
    parents, delayed = _v20_window()
    report = v20_readiness.build_readiness(parents, delayed)
    assert report["v20_feature_complete_close_windows"] == 1
    assert report["feature_rows"] == 7
    assert report["rows_by_cohort"] == {"BTC": 1, "NON_BTC_TRANSFER": 6}
    assert report["feature_failure_counts"] == {}
    assert report["outcome_columns_selected"] is False
    assert report["outcome_labels_read"] is False
    assert report["model_fit_performed"] is False
    assert report["notification_eligible"] is False
    assert report["real_trading_allowed"] is False

    report = v20_readiness.build_readiness(parents, delayed[:-1])
    assert report["v20_feature_complete_close_windows"] == 0
    assert report["feature_rows"] == 0
    assert report["missing_parent_delayed_pairs"] == 1


def test_v20_readiness_status_matches_earliest_complete_window_seal_rule():
    assert v20_readiness._readiness_status(149) == (
        "COLLECTING_V20_PROSPECTIVE_FEATURES_NO_OUTCOMES"
    )
    assert v20_readiness._readiness_status(150) == (
        "READY_FOR_MANUAL_V20_FEATURE_SEAL"
    )
