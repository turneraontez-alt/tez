"""Outcome-blind source validation for the frozen V21 trajectory study."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

from . import rti_delayed_feature_reservoir_identity as reservoir_identity
from . import rti_microstructure_v18 as v18
from . import rti_microstructure_v19 as v19
from . import rti_microstructure_v21_features as features
from . import rti_microstructure_v21_identity as identity
from tools.q15_rti_delayed_feature_reservoir_readiness import (
    REQUIRED_PERSISTED_KEYS,
    _feature_quality_failures,
)
from tools.q15_rti_microstructure_preregister import design_fingerprint


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROTOCOL = ROOT / identity.PROTOCOL_RELATIVE_PATH
DEFAULT_EVALUATOR_CONTRACT = ROOT / identity.EVALUATOR_CONTRACT_RELATIVE_PATH
MAX_DATA_AGE_SECONDS = 3.0
MAX_TIMING_OFFSET_SECONDS = 2.0
INTERMEDIATE_SECONDS_BEFORE_CLOSE = 750.0
INTERMEDIATE_DELAY_SECONDS = 30.0
INTERMEDIATE_EXPECTED_PATH_COUNT = 31.0
ALLOWED_QUOTE_AGE_SOURCES = v19.ALLOWED_QUOTE_AGE_SOURCES
ALLOWED_QUOTE_EVIDENCE_SOURCES = v19.ALLOWED_QUOTE_EVIDENCE_SOURCES


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


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


def _flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"true", "yes", "1"}:
        return True
    if text in {"false", "no", "0"}:
        return False
    return None


def _confirmation_side_matches_distance(
    original_side: str, confirmation_side: str,
    signed_distance_bps: float | None,
) -> bool:
    if (
        original_side not in {"YES", "NO"}
        or confirmation_side not in {"YES", "NO"}
        or signed_distance_bps is None
    ):
        return False
    expected = (
        ("YES" if signed_distance_bps >= 0.0 else "NO")
        if original_side == "YES"
        else ("NO" if signed_distance_bps > 0.0 else "YES")
    )
    return confirmation_side == expected


def _load_json(path: Path, error_prefix: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{error_prefix}_unreadable") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"{error_prefix}_root_not_object")
    return dict(value)


def load_protocol(path: Path = DEFAULT_PROTOCOL) -> dict[str, Any]:
    protocol = _load_json(path, "v21_protocol")
    disclosure = dict(protocol.get("outcome_blind_design_disclosure") or {})
    population = dict(protocol.get("population") or {})
    feature_contract = dict(protocol.get("feature_contract") or {})
    source_quality = dict(protocol.get("source_quality_gates") or {})
    separation = dict(protocol.get("population_execution_separation") or {})
    evaluation = dict(protocol.get("historical_evaluation") or {})
    promotion = dict(protocol.get("prospective_paper_promotion") or {})
    collection = dict(protocol.get("collection") or {})
    if (
        design_fingerprint(protocol) != identity.PROTOCOL_SHA256
        or protocol.get("protocol_id") != identity.PROTOCOL_ID
        or protocol.get("design_id") != identity.DESIGN_ID
        or protocol.get("protocol_status")
        != "FROZEN_BEFORE_ANY_V21_PROSPECTIVE_OUTCOME_ACCESS"
        or disclosure.get("prospective_or_reservoir_outcomes_inspected") is not False
        or disclosure.get("prospective_resolution_status_inspected") is not False
        or disclosure.get(
            "labels_used_to_choose_features_models_thresholds_or_partitions"
        ) is not False
        or disclosure.get("v20_results_inspected") is not False
        or disclosure.get(
            "final_feature_lineage_audit_completed_before_first_eligible_close"
        ) is not True
        or int(disclosure.get(
            "v21_eligible_rows_before_final_feature_lineage_audit"
        ) if disclosure.get(
            "v21_eligible_rows_before_final_feature_lineage_audit"
        ) is not None else -1) != 0
        or float(population.get("prospective_after_close_time") or 0.0)
        != identity.PROSPECTIVE_AFTER_CLOSE_TIME
        or float(population.get("first_eligible_close_time") or 0.0)
        != identity.FIRST_ELIGIBLE_CLOSE_TIME
        or population.get("separate_cohort_models_reports_and_gates_required")
        is not True
        or population.get("all_seven_assets_in_one_close_are_one_chronological_cluster")
        is not True
        or population.get("historical_backfill_or_imputation_forbidden") is not True
        or feature_contract.get("feature_builder_version")
        != identity.FEATURE_BUILDER_VERSION
        or int(feature_contract.get("feature_count") or 0)
        != identity.FEATURE_COUNT
        or feature_contract.get("feature_names_sha256")
        != identity.FEATURE_NAMES_SHA256
        or _canonical_sha256(list(features.FEATURE_NAMES))
        != identity.FEATURE_NAMES_SHA256
        or tuple(feature_contract.get("trajectory_features") or ())
        != features.TRAJECTORY_FEATURE_NAMES
        or int(feature_contract.get("trajectory_feature_count") or 0) != 24
        or source_quality.get("parent_original_and_record_side_lineage_must_match")
        is not True
        or source_quality.get(
            "confirmation_side_must_match_signed_distance_at_both_checkpoints"
        ) is not True
        or source_quality.get(
            "positive_distinct_parent_intermediate_and_delayed_ids_required"
        ) is not True
        or separation.get("feature_window_credit_requires_all_seven_rows_executable")
        is not False
        or separation.get("row_level_execution_requires_actual_12m_displayed_support_for_all_10_contracts")
        is not True
        or separation.get("fake_fill_assumptions_forbidden") is not True
        or int(evaluation.get("minimum_complete_close_windows") or 0)
        != identity.MINIMUM_COMPLETE_CLOSE_WINDOWS
        or int(evaluation.get("train_windows") or 0)
        != identity.TRAIN_CLOSE_WINDOWS
        or int(evaluation.get("probability_calibration_windows") or 0)
        != identity.PROBABILITY_CALIBRATION_CLOSE_WINDOWS
        or int(evaluation.get("execution_policy_selection_windows") or 0)
        != identity.EXECUTION_POLICY_SELECTION_CLOSE_WINDOWS
        or int(evaluation.get("untouched_test_windows") or 0)
        != identity.UNTOUCHED_TEST_CLOSE_WINDOWS
        or evaluation.get("calibration_labels_never_used_for_model_or_margin_selection")
        is not True
        or evaluation.get("policy_selection_labels_never_used_for_model_or_calibrator_fit")
        is not True
        or evaluation.get(
            "policy_selection_labels_may_choose_identity_or_frozen_platt_without_refit"
        )
        is not True
        or evaluation.get("untouched_test_opened_once_after_all_prior_gates_pass")
        is not True
        or promotion.get("manual_promotion_only") is not True
        or promotion.get("automatic_promotion") is not False
        or promotion.get("real_trading_allowed") is not False
        or any(collection.get(key) is not False for key in (
            "outcome_access_allowed_now",
            "model_fit_allowed_now",
            "probability_scoring_allowed_now",
            "paper_artifact_allowed_now",
            "notifications_allowed_now",
            "telegram_allowed_now",
            "automatic_promotion_allowed",
            "real_trading_allowed",
        ))
    ):
        raise ValueError("v21_protocol_identity_or_safety_invalid")
    return protocol


def load_evaluator_contract(
    path: Path = DEFAULT_EVALUATOR_CONTRACT,
) -> dict[str, Any]:
    contract = _load_json(path, "v21_evaluator_contract")
    amendment = dict(contract.get("pre_evidence_implementation_amendment") or {})
    dependency = dict(contract.get("dependency_contract") or {})
    partitions = dict(contract.get("partitions") or {})
    preprocessing = dict(contract.get("preprocessing") or {})
    candidates = dict(contract.get("candidate_models") or {})
    selection = dict(contract.get("model_selection") or {})
    calibration = dict(contract.get("probability_calibration") or {})
    policy = dict(contract.get("execution_policy_selection") or {})
    bootstrap = dict(contract.get("bootstrap") or {})
    untouched = dict(contract.get("untouched_test") or {})
    ablation = dict(contract.get("v20_feature_map_ablation") or {})
    fees = dict(contract.get("fee_schedule_verification") or {})
    subgroups = dict(contract.get("reporting_subgroups") or {})
    safety = dict(contract.get("safety") or {})
    if (
        design_fingerprint(contract) != identity.EVALUATOR_CONTRACT_SHA256
        or contract.get("contract_id") != identity.EVALUATOR_CONTRACT_ID
        or contract.get("contract_status")
        != "FROZEN_BEFORE_ANY_V21_LABEL_ACCESS_OR_MODEL_FIT"
        or contract.get("protocol_id") != identity.PROTOCOL_ID
        or contract.get("feature_builder_version")
        != identity.FEATURE_BUILDER_VERSION
        or int(contract.get("feature_count") or 0) != identity.FEATURE_COUNT
        or contract.get("feature_names_sha256") != identity.FEATURE_NAMES_SHA256
        or amendment.get("v21_eligible_rows_before_amendment") != 0
        or amendment.get("outcomes_or_resolution_status_inspected") is not False
        or amendment.get("model_fit_or_probability_scoring_performed") is not False
        or dependency.get("exact_version") != "1.9.0"
        or dependency.get("single_threaded_fit_required") is not True
        or int(dependency.get("deterministic_random_seed") or -1) != 1521
        or int(partitions.get("exclusive_earliest_complete_windows") or 0)
        != identity.MINIMUM_COMPLETE_CLOSE_WINDOWS
        or list(partitions.get("train") or ()) != [0, 104]
        or list(partitions.get("probability_calibration") or ()) != [105, 129]
        or list(partitions.get("execution_policy_selection") or ()) != [130, 154]
        or list(partitions.get("untouched_test") or ()) != [155, 179]
        or partitions.get("probability_calibration_and_policy_selection_are_disjoint")
        is not True
        or preprocessing.get("numeric_center") != "MEDIAN"
        or preprocessing.get("numeric_scale") != "INTERQUARTILE_RANGE"
        or preprocessing.get("quantile_method") != "linear"
        or list(preprocessing.get("probability_clip") or ())
        != [0.000001, 0.999999]
        or len(candidates.get("NON_BTC_TRANSFER") or ()) != 2
        or len(candidates.get("BTC") or ()) != 1
        or selection.get("primary_validation_rows")
        != "ROW_LEVEL_EXECUTABLE_VALIDATION_ROWS_ONLY"
        or calibration.get("method")
        != "DISJOINT_POLICY_SELECTION_BETWEEN_IDENTITY_AND_L2_REGULARIZED_PLATT_ON_LOGIT"
        or list(calibration.get("candidate_methods") or ()) != [
            "IDENTITY", "L2_REGULARIZED_PLATT_ON_LOGIT",
        ]
        or float(calibration.get("C") or 0.0) != 0.1
        or calibration.get("solver") != "lbfgs"
        or calibration.get("fit_intercept") is not True
        or float(calibration.get("tol") or 0.0) != 0.00000001
        or int(calibration.get("max_iter") or 0) != 5000
        or calibration.get("fit_rows")
        != "ALL_FEATURE_COMPLETE_CALIBRATION_ROWS"
        or int(calibration.get("minimum_rows_non_btc") or 0) != 150
        or int(calibration.get("minimum_rows_btc") or 0) != 25
        or calibration.get("proper_score_gate_population")
        != "DISJOINT_ROW_LEVEL_EXECUTABLE_POLICY_ROWS"
        or calibration.get("selection_primary_metric")
        != "LOWEST_POLICY_LOG_LOSS"
        or list(calibration.get("selection_secondary_metrics") or ()) != [
            "LOWEST_POLICY_BRIER_SCORE",
            "IDENTITY_BEFORE_PLATT_LEXICOGRAPHIC",
        ]
        or list(calibration.get("proper_score_gate_requires") or ()) != [
            "SELECTED_CALIBRATOR_LOG_LOSS_STRICTLY_BEATS_12M_MARKET",
            "SELECTED_CALIBRATOR_BRIER_STRICTLY_BEATS_12M_MARKET",
        ]
        or calibration.get("in_sample_calibration_scores_are_diagnostic_only")
        is not True
        or calibration.get("policy_or_test_labels_for_calibrator_fit_forbidden")
        is not True
        or calibration.get("policy_labels_may_select_but_never_refit_calibrator")
        is not True
        or policy.get("probability_calibration_refit_forbidden") is not True
        or int(bootstrap.get("resamples") or 0) != 5000
        or int(bootstrap.get("random_seed") or -1) != 1521
        or bootstrap.get("quantile_method") != "linear"
        or untouched.get("one_shot_only") is not True
        or untouched.get("refit_recalibration_model_choice_margin_choice_or_threshold_change_forbidden")
        is not True
        or untouched.get("label_population")
        != "ALL_FEATURE_COMPLETE_UNTOUCHED_TEST_ROWS_AFTER_EXCLUSIVE_RESERVATION"
        or untouched.get("candidate_trade_population")
        != "ROW_LEVEL_FULL_FILL_SUPPORTED_AND_CALIBRATED_EDGE_AT_OR_ABOVE_FROZEN_SELECTED_MARGIN"
        or untouched.get("pnl_scored_only_for_row_level_full_fill_supported_picks")
        is not True
        or int(untouched.get("bootstrap_resamples") or 0) != 5000
        or list(untouched.get("bootstrap_quantiles") or ()) != [0.025, 0.975]
        or float(untouched.get("bootstrap_policy_lower_quantile") or -1.0) != 0.2
        or int(untouched.get("minimum_picks_non_btc") or 0) != 25
        or int(untouched.get("minimum_picks_btc") or 0) != 8
        or int(untouched.get("minimum_picks_each_side") or 0) != 4
        or list(untouched.get("required_gates") or ()) != [
            "FEE_SLIPPAGE_ADJUSTED_PNL_STRICTLY_POSITIVE",
            "CLOSE_CLUSTER_BOOTSTRAP_20TH_PERCENTILE_MEAN_PNL_STRICTLY_POSITIVE",
            "WILSON_95_LOWER_STRICTLY_EXCEEDS_AVERAGE_BREAK_EVEN",
            "ALL_ROW_LOG_LOSS_STRICTLY_BEATS_12M_MARKET",
            "ALL_ROW_BRIER_STRICTLY_BEATS_12M_MARKET",
            "ALL_ROW_LOG_LOSS_STRICTLY_BEATS_V20_FEATURE_MAP_ABLATION",
            "ALL_ROW_BRIER_STRICTLY_BEATS_V20_FEATURE_MAP_ABLATION",
            "MAXIMUM_DRAWDOWN_PER_PICK_STRICTLY_BELOW_ALL_SOURCE_EXECUTABLE_CONTROL",
            "FROZEN_TEST_VOLUME_AND_SIDE_MINIMA",
        ]
        or ablation.get("never_used_for_v21_model_selection_calibration_or_policy_selection")
        is not True
        or ablation.get("untouched_test_proper_score_benchmark_is_required_gate")
        is not True
        or int(ablation.get("feature_count") or 0) != 52
        or ablation.get("feature_names_sha256")
        != "fff635c3dddba73732dc4225622b33b763b55e881f14a4d031278adcf6bfe35d"
        or list(ablation.get("feature_indices_zero_based") or ()) != [0, 51]
        or ablation.get("model_spec")
        != "INDEPENDENTLY_WALK_FORWARD_SELECTED_FROM_SAME_FROZEN_CANDIDATE_GRID_USING_ONLY_FIRST_52_FEATURES"
        or ablation.get("same_frozen_candidate_grid_as_v21") is not True
        or ablation.get("selection_uses_only_train_internal_walk_forward")
        is not True
        or ablation.get("selection_never_uses_calibration_policy_or_test")
        is not True
        or ablation.get("selected_ablation_spec_may_differ_from_v21") is not True
        or ablation.get("fit_rows") != "SAME_ALL_FEATURE_COMPLETE_TRAIN_ROWS"
        or ablation.get("calibration")
        != "SEPARATE_REGULARIZED_PLATT_ON_ALL_FEATURE_COMPLETE_CALIBRATION_ROWS_WITH_IDENTITY_VERSUS_PLATT_SELECTED_ON_EXECUTABLE_POLICY_ROWS_WITHOUT_REFIT"
        or ablation.get("test_metric_population")
        != "ALL_FEATURE_COMPLETE_UNTOUCHED_TEST_COHORT_ROWS"
        or fees.get("fresh_at_each_manual_label_stage") is not True
        or fees.get("outcome_free_series_fee_precondition_before_label_reservation")
        is not True
        or fees.get("required_fee_type") != "quadratic"
        or float(fees.get("required_fee_multiplier") or 0.0) != 1.0
        or float(fees.get("general_taker_fee_rate") or 0.0) != 0.07
        or fees.get("fee_schedule_version") != "kalshi-fee-schedule-20260707"
        or fees.get("execution_cost_model_version")
        != "rti-quote-plus-slippage-fee-at-fill-20260721-v2"
        or len(fees.get("series_tickers") or ()) != 7
        or subgroups.get("report_only_never_used_for_selection_or_gates")
        is not True
        or len(subgroups.get("distance_absolute_bps_tiers") or ()) != 3
        or len(subgroups.get("volatility_raw_bps_tiers") or ()) != 3
        or len(subgroups.get("reversal_risk_probability_tiers") or ()) != 3
        or len(subgroups.get("settlement_average_risk_absolute_ratio_tiers") or ())
        != 3
        or len(subgroups.get("trajectory_curvature_bps_tiers") or ()) != 4
        or safety.get("pretest_label_reservation_population")
        != "ALL_TRAIN_AND_CALIBRATION_ROWS_PLUS_ONLY_ROW_LEVEL_EXECUTABLE_POLICY_ROWS"
        or safety.get("nonexecutable_policy_labels_forbidden")
        is not True
        or safety.get("untouched_test_labels_forbidden_during_pretest") is not True
        or any(safety.get(key) is not False for key in (
            "outcome_access_allowed_now",
            "model_fit_allowed_now",
            "probability_scoring_allowed_now",
            "automatic_command_confirmation_allowed",
            "paper_artifact_allowed_now",
            "notification_allowed_now",
            "telegram_allowed_now",
            "automatic_promotion_allowed",
            "real_trading_allowed",
        ))
    ):
        raise ValueError("v21_evaluator_contract_identity_or_safety_invalid")
    return contract


def _reservoir_failures(
    profile: Mapping[str, Any], *, expected_path_count: int = 61,
    expected_delay_seconds: int = 60,
) -> list[str]:
    failures = []
    if profile.get("delayed_feature_reservoir_version") != reservoir_identity.RESERVOIR_VERSION:
        failures.append("RESERVOIR_VERSION_IDENTITY")
    if profile.get("delayed_feature_reservoir_record_only") is not True:
        failures.append("RESERVOIR_RECORD_ONLY_IDENTITY")
    if profile.get("delayed_feature_reservoir_used_for_decision") is not False:
        failures.append("RESERVOIR_NOT_USED_FOR_DECISION_IDENTITY")
    if any(key not in profile for key in REQUIRED_PERSISTED_KEYS):
        failures.append("RESERVOIR_PERSISTED_SCHEMA_INCOMPLETE")
    failures.extend(_feature_quality_failures(
        profile,
        expected_path_count=expected_path_count,
        expected_delay_seconds=expected_delay_seconds,
    ))
    return failures


def evaluate_intermediate_source(
    parent_row: Mapping[str, Any], intermediate_row: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the genuine +30s quote/path checkpoint without an outcome."""
    load_protocol()
    parent_source = v18.evaluate_source_row(parent_row)
    parent_evidence = dict(parent_source.get("evidence") or {})
    profile = _profile(intermediate_row)
    close_time = _num(_value(intermediate_row, profile, "close_time"))
    parent_close = _num(parent_evidence.get("close_time"))
    parent_id = int(_num(parent_evidence.get("id")) or 0)
    intermediate_id = int(_num(intermediate_row.get("id")) or 0)
    linked_parent_id = int(_num(profile.get("rti_confirm_original_row_id")) or 0)
    asset = str(_value(intermediate_row, profile, "asset") or "").upper()
    ticker = str(_value(intermediate_row, profile, "ticker") or "")
    side = str(_value(intermediate_row, profile, "side") or "").upper()
    original_side = str(profile.get("rti_confirm_original_side") or "").upper()
    confirmation_side = str(profile.get("rti_confirm_side") or "").upper()
    signed_distance_bps = _num(profile.get("rti_confirm_signed_distance_bps"))
    target_at = _num(profile.get("rti_confirm_target_at"))
    quote_captured_at = _num(profile.get("rti_confirm_quote_captured_at"))
    evaluated_at = _num(profile.get("rti_confirm_evaluated_at"))
    timing_offset = _num(profile.get("rti_confirm_timing_offset_s"))
    evaluation_delay = _num(profile.get("rti_confirm_evaluation_delay_s"))
    delay_seconds = _num(profile.get("rti_confirm_delay_seconds"))
    path_count = _num(profile.get("rti_confirm_path_count"))
    expected_count = _num(profile.get("rti_confirm_path_expected_count"))
    path_complete = _flag(profile.get("rti_confirm_path_complete"))
    path_max_age = _num(profile.get("rti_confirm_path_max_receive_age_s"))
    path_decision_age = _num(profile.get("rti_confirm_path_decision_age_s"))
    quote_age = _num(_value(intermediate_row, profile, "quote_age_seconds"))
    quote_age_source = str(_value(intermediate_row, profile, "quote_age_source") or "")
    quote_evidence_source = str(
        _value(intermediate_row, profile, "quote_evidence_source") or ""
    )
    expected_target = (
        None if close_time is None else close_time - INTERMEDIATE_SECONDS_BEFORE_CLOSE
    )
    parent_captured = _num(parent_evidence.get("source_captured_at"))
    capture_gap = (
        None if quote_captured_at is None or parent_captured is None
        else quote_captured_at - parent_captured
    )
    failures = []
    if parent_source.get("available") is not True:
        failures.append("PARENT_SOURCE_INCOMPLETE")
    if parent_id <= 0 or intermediate_id <= 0 or parent_id == intermediate_id:
        failures.append("INTERMEDIATE_SOURCE_IDENTITY_INVALID")
    if close_time is None or close_time <= identity.PROSPECTIVE_AFTER_CLOSE_TIME:
        failures.append("STRICTLY_PROSPECTIVE_V21_CLOSE_REQUIRED")
    if str(_value(intermediate_row, profile, "bot_name") or "") != "rti_path_13m":
        failures.append("INTERMEDIATE_BOT_IDENTITY")
    if str(_value(intermediate_row, profile, "record_kind") or "").upper() != (
        "RTI_PATH_12M30_CONFIRM_PROSPECTIVE"
    ):
        failures.append("INTERMEDIATE_RECORD_KIND_IDENTITY")
    if str(_value(intermediate_row, profile, "interval") or "").upper() != "12M30S":
        failures.append("INTERMEDIATE_INTERVAL_IDENTITY")
    if (
        asset != parent_evidence.get("asset")
        or ticker != parent_evidence.get("ticker")
        or close_time != parent_close
        or linked_parent_id != parent_id
    ):
        failures.append("INTERMEDIATE_PARENT_CONTRACT_IDENTITY")
    if delay_seconds != INTERMEDIATE_DELAY_SECONDS:
        failures.append("INTERMEDIATE_DELAY_IDENTITY")
    if expected_target is None or target_at is None or abs(target_at - expected_target) > 1e-6:
        failures.append("INTERMEDIATE_TARGET_TIMESTAMP_IDENTITY")
    if (
        quote_captured_at is None or target_at is None
        or quote_captured_at < target_at - 1e-6
        or quote_captured_at - target_at > MAX_TIMING_OFFSET_SECONDS
        or timing_offset is None
        or not -1e-6 <= timing_offset <= MAX_TIMING_OFFSET_SECONDS
        or abs(timing_offset - (quote_captured_at - target_at)) > 0.05
    ):
        failures.append("FRESH_30S_CAPTURE_TIMING")
    if (
        evaluated_at is None or quote_captured_at is None
        or evaluated_at < quote_captured_at - 1e-6
        or evaluated_at - quote_captured_at > MAX_TIMING_OFFSET_SECONDS
        or evaluation_delay is None or target_at is None
        or not -1e-6 <= evaluation_delay <= MAX_TIMING_OFFSET_SECONDS
        or abs(evaluation_delay - (evaluated_at - target_at)) > 0.05
    ):
        failures.append("FRESH_30S_EVALUATION_TIMING")
    if capture_gap is None or not 28.0 <= capture_gap <= 32.1:
        failures.append("NEW_30S_QUOTE_NOT_INDEPENDENT_OF_PARENT")
    if (
        path_complete is not True
        or path_count != INTERMEDIATE_EXPECTED_PATH_COUNT
        or expected_count != INTERMEDIATE_EXPECTED_PATH_COUNT
        or path_max_age is None or path_decision_age is None
        or not -1e-6 <= path_max_age <= MAX_DATA_AGE_SECONDS
        or not -1e-6 <= path_decision_age <= MAX_DATA_AGE_SECONDS
    ):
        failures.append("FRESH_31_SAMPLE_RTI_PATH")
    if quote_age is None or not -1e-6 <= quote_age <= MAX_DATA_AGE_SECONDS:
        failures.append("FRESH_INTERMEDIATE_QUOTE")
    if (
        quote_age_source not in ALLOWED_QUOTE_AGE_SOURCES
        or quote_evidence_source not in ALLOWED_QUOTE_EVIDENCE_SOURCES
    ):
        failures.append("OFFICIAL_INTERMEDIATE_QUOTE_SOURCE_IDENTITY")
    parent_side = str(parent_evidence.get("side") or "").upper()
    if (
        side not in {"YES", "NO"}
        or original_side not in {"YES", "NO"}
        or confirmation_side not in {"YES", "NO"}
    ):
        failures.append("INTERMEDIATE_SIDE_IDENTITY_MISSING")
    if side != parent_side or original_side != parent_side:
        failures.append("INTERMEDIATE_ORIGINAL_SIDE_LINEAGE_MISMATCH")
    if not _confirmation_side_matches_distance(
        original_side, confirmation_side, signed_distance_bps,
    ):
        failures.append("INTERMEDIATE_CONFIRMATION_SIDE_DISTANCE_CONTRADICTION")
    if _flag(_value(intermediate_row, profile, "paper_only")) is not True:
        failures.append("INTERMEDIATE_ROW_NOT_PAPER_ONLY")
    failures.extend(_reservoir_failures(
        profile, expected_path_count=int(INTERMEDIATE_EXPECTED_PATH_COUNT),
        expected_delay_seconds=int(INTERMEDIATE_DELAY_SECONDS),
    ))
    evidence = {
        "parent_id": parent_id,
        "intermediate_id": intermediate_id,
        "asset": asset,
        "ticker": ticker,
        "close_time": close_time,
        "parent_side": str(parent_evidence.get("side") or ""),
        "intermediate_side": side,
        "original_side": original_side,
        "confirmation_side": confirmation_side,
        "signed_distance_bps": signed_distance_bps,
        "target_at": target_at,
        "quote_captured_at": quote_captured_at,
        "evaluated_at": evaluated_at,
        "timing_offset_seconds": timing_offset,
        "evaluation_delay_seconds": evaluation_delay,
        "capture_gap_from_parent_seconds": capture_gap,
        "path_complete": path_complete is True,
        "path_count": path_count,
        "path_expected_count": expected_count,
        "path_max_receive_age_seconds": path_max_age,
        "path_decision_age_seconds": path_decision_age,
        "quote_age_seconds": quote_age,
        "quote_age_source": quote_age_source,
        "quote_evidence_source": quote_evidence_source,
        "parent_feature_evidence_sha256": parent_source.get(
            "feature_evidence_sha256"
        ),
    }
    return {
        "available": not failures,
        "failures": failures,
        "evidence": evidence,
        "feature_evidence_sha256": _canonical_sha256(evidence),
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }


def evaluate_triplet(
    parent_row: Mapping[str, Any],
    intermediate_row: Mapping[str, Any],
    delayed_row: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one V21 row while separating features from actual executability."""
    load_protocol()
    load_evaluator_contract()
    intermediate = evaluate_intermediate_source(parent_row, intermediate_row)
    delayed = v19.evaluate_delayed_source(parent_row, delayed_row)
    delayed_profile = _profile(delayed_row)
    delayed_quality_failures = _reservoir_failures(delayed_profile)
    feature = features.feature_vector(parent_row, intermediate_row, delayed_row)
    failures = []
    if intermediate.get("available") is not True:
        failures.extend(intermediate.get("failures") or ())
    if delayed.get("available") is not True:
        failures.extend(delayed.get("failures") or ())
    failures.extend(delayed_quality_failures)
    if feature.get("available") is not True:
        failures.append("V21_TRAJECTORY_FEATURE_SOURCE_INCOMPLETE")
    intermediate_evidence = dict(intermediate.get("evidence") or {})
    delayed_evidence = dict(delayed.get("evidence") or {})
    if (
        intermediate_evidence.get("parent_id") != delayed_evidence.get("parent_id")
        or intermediate_evidence.get("asset") != delayed_evidence.get("asset")
        or intermediate_evidence.get("ticker") != delayed_evidence.get("ticker")
        or intermediate_evidence.get("close_time") != delayed_evidence.get("close_time")
        or intermediate_evidence.get("original_side") != delayed_evidence.get("original_side")
    ):
        failures.append("V21_TRIPLET_LINEAGE_MISMATCH")
    parent_side = str(delayed_evidence.get("parent_side") or "").upper()
    delayed_row_side = str(delayed_evidence.get("delayed_side") or "").upper()
    delayed_original_side = str(
        delayed_evidence.get("original_side") or ""
    ).upper()
    delayed_confirmation_side = str(
        delayed_evidence.get("confirmation_side") or ""
    ).upper()
    delayed_signed_distance = _num(
        delayed_profile.get("rti_confirm_signed_distance_bps")
    )
    if (
        parent_side not in {"YES", "NO"}
        or delayed_row_side != parent_side
        or delayed_original_side != parent_side
        or delayed_confirmation_side not in {"YES", "NO"}
    ):
        failures.append("V21_DELAYED_ORIGINAL_SIDE_LINEAGE_MISMATCH")
    if not _confirmation_side_matches_distance(
        delayed_original_side,
        delayed_confirmation_side,
        delayed_signed_distance,
    ):
        failures.append("V21_DELAYED_CONFIRMATION_SIDE_DISTANCE_CONTRADICTION")
    source_ids = (
        int(_num(feature.get("parent_id")) or 0),
        int(_num(feature.get("intermediate_id")) or 0),
        int(_num(feature.get("delayed_id")) or 0),
    )
    if min(source_ids) <= 0 or len(set(source_ids)) != 3:
        failures.append("V21_TRIPLET_SOURCE_IDENTITY_INVALID")
    close_time = _num(feature.get("close_time"))
    if close_time is None or close_time <= identity.PROSPECTIVE_AFTER_CLOSE_TIME:
        failures.append("STRICTLY_PROSPECTIVE_V21_CLOSE_REQUIRED")
    execution_supported = (
        delayed_evidence.get("sim_contracts") == 10.0
        and delayed_evidence.get("sim_full_fill_supported") is True
    )
    evidence = {
        "parent_id": feature.get("parent_id"),
        "intermediate_id": feature.get("intermediate_id"),
        "delayed_id": feature.get("delayed_id"),
        "asset": feature.get("asset"),
        "cohort": feature.get("cohort"),
        "ticker": feature.get("ticker"),
        "close_time": close_time,
        "side": feature.get("side"),
        "feature_builder_version": feature.get("feature_builder_version"),
        "feature_evidence_sha256": feature.get("feature_evidence_sha256"),
        "base_feature_evidence_sha256": feature.get(
            "base_feature_evidence_sha256"
        ),
        "intermediate_source_evidence_sha256": intermediate.get(
            "feature_evidence_sha256"
        ),
        "delayed_source_evidence_sha256": delayed.get(
            "feature_evidence_sha256"
        ),
        "feature_count": len(feature.get("features") or ()),
        "execution_supported": execution_supported,
        "entry_ask_cents": delayed_evidence.get("entry_ask_cents"),
        "spread_cents": delayed_evidence.get("spread_cents"),
        "depth_contracts": delayed_evidence.get("depth_contracts"),
        "sim_contracts": delayed_evidence.get("sim_contracts"),
    }
    return {
        "available": not failures,
        "eligible_for_v21_feature_credit": not failures,
        "eligible_for_v21_execution_evaluation": not failures and execution_supported,
        "failures": list(dict.fromkeys(str(item) for item in failures)),
        "evidence": evidence,
        "source_feature_evidence_sha256": _canonical_sha256(evidence),
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "evaluator_contract_id": identity.EVALUATOR_CONTRACT_ID,
        "evaluator_contract_sha256": identity.EVALUATOR_CONTRACT_SHA256,
        "feature_names": feature.get("feature_names"),
        "features": feature.get("features"),
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }
