"""Validate the outcome-blind V15 independent-path successor preregistration.

This tool reads only immutable JSON manifests.  It cannot read strategy rows,
settlement outcomes, fit a model, emit an artifact, notify, promote, or trade.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots.rti_independent_path import DERIVED_FEATURE_KEYS
from q15_upgrade.strategy_bots.rti_independent_path_geometry_identity import (
    PROTOCOL_ID as GEOMETRY_PROTOCOL_ID,
    PROTOCOL_SHA256 as GEOMETRY_PROTOCOL_SHA256,
)
from q15_upgrade.strategy_bots.rti_independent_path_identity import (
    DESIGN_ID as PATH_DESIGN_ID,
    DESIGN_SHA256 as PATH_DESIGN_SHA256,
    FIRST_ELIGIBLE_CLOSE_TIME as PATH_FIRST_ELIGIBLE_CLOSE_TIME,
)
from q15_upgrade.strategy_bots.rti_independent_path_successor_identity import (
    CHARTER_ID,
    CHARTER_SHA256,
    EVALUATION_PROTOCOL_ID,
    EVALUATION_PROTOCOL_SHA256,
    PROPOSED_DESIGN_ID,
)
from q15_upgrade.strategy_bots.rti_microstructure_v14_identity import (
    DESIGN_ID as V14_DESIGN_ID,
    DESIGN_SHA256 as V14_DESIGN_SHA256,
    EVALUATION_PROTOCOL_ID as V14_EVALUATION_PROTOCOL_ID,
    EVALUATION_PROTOCOL_SHA256 as V14_EVALUATION_PROTOCOL_SHA256,
)
from tools.q15_rti_microstructure_preregister import design_fingerprint


DEFAULT_CHARTER = (
    ROOT / "config" / "q15_rti_independent_path_successor_preregistration_v1.json"
)
DEFAULT_PROTOCOL = ROOT / "config" / "q15_rti_v15_walk_forward_protocol.json"
EXPECTED_ASSETS = {
    "BTC": ["BTC"],
    "NON_BTC_TRANSFER": ["BNB", "DOGE", "ETH", "HYPE", "SOL", "XRP"],
}
EXPECTED_FACTORS = [0.0, 0.25, 0.5, 0.75, 1.0]


def _load(path: Path, error: str) -> dict[str, Any]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, Mapping):
        raise ValueError(error)
    return dict(decoded)


def load_charter(path: Path = DEFAULT_CHARTER) -> dict[str, Any]:
    return _load(path, "v15_path_charter_root_not_object")


def load_protocol(path: Path = DEFAULT_PROTOCOL) -> dict[str, Any]:
    return _load(path, "v15_path_protocol_root_not_object")


def _all_false(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> bool:
    return all(mapping.get(key) is False for key in keys)


def validate_charter(charter: Mapping[str, Any]) -> None:
    if design_fingerprint(charter) != CHARTER_SHA256:
        raise ValueError("v15_path_charter_sha256_mismatch")
    if (
        charter.get("charter_id") != CHARTER_ID
        or charter.get("charter_status")
        != "PREREGISTERED_BEFORE_ANY_INDEPENDENT_PATH_OUTCOME_REVIEW"
        or charter.get("source_path_design_id") != PATH_DESIGN_ID
        or charter.get("source_path_design_sha256") != PATH_DESIGN_SHA256
        or charter.get("source_geometry_protocol_id") != GEOMETRY_PROTOCOL_ID
        or charter.get("source_geometry_protocol_sha256")
        != GEOMETRY_PROTOCOL_SHA256
        or charter.get("source_v14_design_id") != V14_DESIGN_ID
        or charter.get("source_v14_design_sha256") != V14_DESIGN_SHA256
        or charter.get("source_v14_evaluation_protocol_id")
        != V14_EVALUATION_PROTOCOL_ID
        or charter.get("source_v14_evaluation_protocol_sha256")
        != V14_EVALUATION_PROTOCOL_SHA256
    ):
        raise ValueError("v15_path_charter_lineage_mismatch")
    evidence = charter.get("evidence_available_at_preregistration")
    if not isinstance(evidence, Mapping) or (
        int(evidence.get("complete_reconstructable_close_windows") or 0) != 16
        or int(evidence.get("credited_rows") or 0) != 112
        or evidence.get("geometry_review_ready") is not False
        or evidence.get("geometry_review_status")
        != "WAITING_FOR_30_COMPLETE_WINDOWS"
        or evidence.get("source_quality_status")
        != "PASS_ALL_CREDITED_COMPLETE_ROWS"
        or int(evidence.get("source_integrity_breaches", -1)) != 0
        or not _all_false(evidence, (
            "outcome_columns_selected",
            "outcome_labels_read",
            "model_fit_performed",
            "performance_metrics_inspected",
        ))
    ):
        raise ValueError("v15_path_charter_evidence_invalid")
    for cohort, expected_rows in (
        ("all_seven", 112), ("btc", 16), ("non_btc_transfer", 96),
    ):
        geometry = evidence.get(cohort)
        if not isinstance(geometry, Mapping) or (
            int(geometry.get("rows") or 0) != expected_rows
            or int(geometry.get("feature_count") or 0) != 5
            or int(geometry.get("active_feature_count") or 0) != 5
            or int(geometry.get("numerical_rank") or 0) != 5
            or int(geometry.get("exact_signed_duplicate_count", -1)) != 0
        ):
            raise ValueError(f"v15_path_charter_geometry_invalid:{cohort}")
    candidate = charter.get("proposed_candidate")
    if not isinstance(candidate, Mapping) or (
        candidate.get("design_id") != PROPOSED_DESIGN_ID
        or candidate.get("feature_schema_version")
        != "rti-probability-microstructure-features-v15"
        or candidate.get("model_family")
        != "regularized_market_prior_residual_logit"
        or candidate.get("architecture")
        != "single_joint_v14_plus_five_path_feature_residual_with_nested_safe_trust"
        or int(candidate.get("base_feature_count") or 0) != 20
        or int(candidate.get("added_feature_count") or 0) != 5
        or int(candidate.get("total_feature_count") or 0) != 25
        or list(candidate.get("fixed_residual_trust_factors") or ())
        != EXPECTED_FACTORS
        or float(candidate.get("fallback_factor", -1.0)) != 0.0
    ):
        raise ValueError("v15_path_charter_candidate_identity_invalid")
    for key in (
        "base_features_are_exact_v14_features",
        "added_features_are_exact_frozen_path_features",
        "complete_reconstructable_seven_asset_path_required",
        "base_optimizer_and_fixed_training_config_unchanged_from_v14",
        "nested_residual_trust_rule_unchanged_from_v14",
        "factor_zero_is_exact_kalshi_market_prior",
        "entry_policy_unchanged_from_v14",
        "frozen_v14_is_parallel_control",
        "kalshi_market_prior_is_parallel_control",
    ):
        if candidate.get(key) is not True:
            raise ValueError(f"v15_path_charter_candidate_guard_missing:{key}")
    for key in (
        "feature_interactions_allowed",
        "polynomial_expansion_allowed",
        "automatic_feature_selection_allowed",
        "automatic_hyperparameter_search_allowed",
        "partial_close_windows_allowed",
        "missing_path_imputation_allowed",
    ):
        if candidate.get(key) is not False:
            raise ValueError(f"v15_path_charter_candidate_guard_missing:{key}")
    if list(charter.get("fixed_added_feature_names_in_order") or ()) != list(
        DERIVED_FEATURE_KEYS
    ):
        raise ValueError("v15_path_charter_feature_order_mismatch")
    readiness = charter.get("readiness_prerequisites")
    if not isinstance(readiness, Mapping) or (
        readiness.get(
            "frozen_geometry_review_must_pass_at_exactly_first_30_complete_windows"
        ) is not True
        or int(readiness.get("non_btc_minimum_complete_windows") or 0) != 60
        or int(readiness.get("btc_minimum_complete_windows") or 0) != 150
        or readiness.get(
            "btc_and_non_btc_must_be_fit_scored_and_gated_separately"
        ) is not True
        or readiness.get(
            "same_close_assets_must_share_every_outer_and_inner_fold"
        ) is not True
        or readiness.get(
            "non_btc_labels_may_not_be_read_before_non_btc_readiness"
        ) is not True
        or readiness.get(
            "btc_labels_may_not_be_read_before_btc_readiness"
        ) is not True
        or readiness.get(
            "geometry_failure_allows_automatic_feature_or_model_change"
        ) is not False
    ):
        raise ValueError("v15_path_charter_readiness_invalid")
    if list(charter.get("required_comparisons") or ()) != [
        "v15_candidate_vs_point_in_time_kalshi_market_prior",
        "v15_candidate_vs_frozen_v14_on_identical_rows",
    ]:
        raise ValueError("v15_path_charter_comparators_invalid")
    history = charter.get("source_history_policy")
    if not isinstance(history, Mapping) or (
        history.get(
            "source_rows_at_or_after_path_first_eligible_close_may_enter_locked_chronological_evaluation"
        ) is not True
        or history.get("pre_path_boundary_rows_receive_credit") is not False
        or history.get("historical_evaluation_can_promote") is not False
        or history.get("paper_challenger_receives_historical_credit") is not False
        or history.get(
            "if_all_historical_gates_pass_paper_ledger_starts_at_next_unseen_close"
        ) is not True
    ):
        raise ValueError("v15_path_charter_history_policy_invalid")
    authority = charter.get("implementation_authority")
    if not isinstance(authority, Mapping) or not _all_false(authority, (
        "charter_creates_executable_model",
        "charter_allows_outcome_access",
        "charter_allows_model_fit",
        "charter_allows_probability_scoring",
        "charter_allows_notifications",
        "charter_allows_automatic_promotion",
        "charter_allows_real_trading",
    )) or authority.get(
        "executable_design_requires_geometry_pass_and_separate_manual_action"
    ) is not True:
        raise ValueError("v15_path_charter_authority_invalid")
    if (
        charter.get("paper_only") is not True
        or charter.get("outcome_columns_forbidden") is not True
        or not _all_false(charter, (
            "outcome_labels_read", "model_fit_performed", "automatic_scoring",
            "automatic_promotion", "real_trading_allowed",
        ))
    ):
        raise ValueError("v15_path_charter_safety_invalid")


def validate_protocol(
    protocol: Mapping[str, Any], charter: Mapping[str, Any],
) -> None:
    if design_fingerprint(protocol) != EVALUATION_PROTOCOL_SHA256:
        raise ValueError("v15_path_protocol_sha256_mismatch")
    if (
        protocol.get("protocol_id") != EVALUATION_PROTOCOL_ID
        or protocol.get("protocol_status")
        != "PREREGISTERED_BEFORE_ANY_INDEPENDENT_PATH_OUTCOME_REVIEW"
        or protocol.get("applies_to_proposed_design_id") != PROPOSED_DESIGN_ID
        or protocol.get("source_successor_charter_id") != CHARTER_ID
        or protocol.get("source_successor_charter_sha256") != CHARTER_SHA256
        or protocol.get("source_path_design_id") != PATH_DESIGN_ID
        or protocol.get("source_path_design_sha256") != PATH_DESIGN_SHA256
        or protocol.get("frozen_v14_control_design_id") != V14_DESIGN_ID
        or protocol.get("frozen_v14_control_design_sha256")
        != V14_DESIGN_SHA256
        or protocol.get("frozen_v14_control_evaluation_protocol_id")
        != V14_EVALUATION_PROTOCOL_ID
        or protocol.get("frozen_v14_control_evaluation_protocol_sha256")
        != V14_EVALUATION_PROTOCOL_SHA256
        or protocol.get("source_successor_charter_sha256")
        != design_fingerprint(charter)
    ):
        raise ValueError("v15_path_protocol_lineage_mismatch")
    if (
        protocol.get("outcome_labels_used_for_protocol") is not False
        or protocol.get("performance_metrics_inspected_before_preregistration")
        is not False
        or protocol.get("feature_rows_inspected_without_labels") is not True
        or protocol.get("paper_only") is not True
        or protocol.get("notification_eligible") is not False
        or protocol.get("automatic_promotion") is not False
        or protocol.get("real_trading_allowed") is not False
    ):
        raise ValueError("v15_path_protocol_origin_or_safety_invalid")
    geometry = protocol.get("geometry_prerequisite")
    if not isinstance(geometry, Mapping) or (
        int(geometry.get("review_window") or 0) != 30
        or geometry.get("required_status")
        != "PASS_CONTINUE_PROSPECTIVE_COLLECTION_NO_MODEL"
        or geometry.get("protocol_id") != GEOMETRY_PROTOCOL_ID
        or geometry.get("protocol_sha256") != GEOMETRY_PROTOCOL_SHA256
        or geometry.get("outcome_labels_must_remain_unread") is not True
        or geometry.get("model_fit_before_pass_forbidden") is not True
        or geometry.get(
            "failure_does_not_allow_automatic_feature_or_threshold_change"
        ) is not True
    ):
        raise ValueError("v15_path_protocol_geometry_prerequisite_invalid")
    architecture = protocol.get("candidate_architecture")
    if not isinstance(architecture, Mapping) or (
        architecture.get("model_family")
        != "regularized_market_prior_residual_logit"
        or
        int(architecture.get("joint_feature_count") or 0) != 25
        or int(architecture.get("v14_base_feature_count") or 0) != 20
        or int(architecture.get("independent_path_added_feature_count") or 0)
        != 5
        or list(architecture.get("fixed_factor_grid") or ())
        != EXPECTED_FACTORS
        or float(architecture.get("fallback_factor", -1.0)) != 0.0
        or architecture.get("feature_order_must_match_future_design_manifest")
        is not True
        or architecture.get("base_optimizer_and_training_config_must_equal_v14")
        is not True
        or architecture.get("nested_residual_trust_architecture_must_equal_v14")
        is not True
        or architecture.get("factor_zero_is_exact_kalshi_market_prior")
        is not True
        or architecture.get("complete_path_window_required") is not True
        or architecture.get("missing_path_imputation_forbidden") is not True
        or architecture.get("feature_interactions_forbidden") is not True
        or architecture.get("automatic_feature_selection_forbidden") is not True
        or architecture.get("automatic_hyperparameter_search_forbidden")
        is not True
    ):
        raise ValueError("v15_path_protocol_architecture_invalid")
    population = protocol.get("population")
    if not isinstance(population, Mapping) or (
        float(population.get("first_source_eligible_close_time") or 0.0)
        != PATH_FIRST_ELIGIBLE_CLOSE_TIME
        or population.get("selection")
        != "EARLIEST_COMPLETE_RECONSTRUCTABLE_CLOSE_WINDOWS_PER_COHORT"
        or population.get("partial_close_windows_forbidden") is not True
        or population.get(
            "same_close_assets_must_share_every_partition_and_fold"
        ) is not True
        or population.get("point_in_time_v14_and_path_features_both_required")
        is not True
        or population.get("timestamp_alignment_fail_closed") is not True
        or population.get("post_decision_source_rows_forbidden") is not True
        or population.get("btc_and_non_btc_must_never_be_pooled") is not True
    ):
        raise ValueError("v15_path_protocol_population_invalid")
    cohorts = protocol.get("cohorts")
    if not isinstance(cohorts, Mapping) or set(cohorts) != set(EXPECTED_ASSETS):
        raise ValueError("v15_path_protocol_cohorts_invalid")
    for name, total in (("NON_BTC_TRANSFER", 60), ("BTC", 150)):
        raw = cohorts.get(name)
        if not isinstance(raw, Mapping) or (
            list(raw.get("assets") or ()) != EXPECTED_ASSETS[name]
            or int(raw.get("minimum_complete_close_windows") or 0) != total
        ):
            raise ValueError(f"v15_path_protocol_cohort_invalid:{name}")
        initial = int(raw.get("initial_train_windows") or 0)
        block = int(raw.get("validation_block_windows") or 0)
        folds = int(raw.get("walk_forward_fold_count") or 0)
        development = int(raw.get("development_train_windows") or 0)
        calibration = int(raw.get("calibration_windows") or 0)
        test = int(raw.get("untouched_test_windows") or 0)
        inner_initial = int(raw.get("inner_initial_train_windows") or 0)
        inner_block = int(raw.get("inner_validation_block_windows") or 0)
        if (
            initial + block * folds + test != total
            or development + calibration + test != total
            or development + calibration != initial + block * folds
            or any(
                (initial + block * index - inner_initial) % inner_block != 0
                for index in range(folds)
            )
        ):
            raise ValueError(f"v15_path_protocol_fold_geometry_invalid:{name}")
    fold = protocol.get("fold_policy")
    if not isinstance(fold, Mapping) or (
        fold.get("outer") != "EXPANDING_TRAIN_NEXT_CONTIGUOUS_BLOCK"
        or fold.get("inner")
        != "EXPANDING_TRAIN_NEXT_CONTIGUOUS_BLOCK_INSIDE_CURRENT_OUTER_TRAIN_ONLY"
        or fold.get("all_validation_times_strictly_after_training_times")
        is not True
        or fold.get("factor_reselected_inside_each_outer_training_period")
        is not True
        or fold.get("outer_validation_labels_may_select_factor") is not False
        or fold.get("calibration_labels_may_select_factor") is not False
        or fold.get("untouched_test_labels_may_select_factor") is not False
        or fold.get("temporary_fold_models_are_deployable") is not False
    ):
        raise ValueError("v15_path_protocol_fold_policy_invalid")
    comparators = protocol.get("comparators")
    if not isinstance(comparators, Mapping) or (
        comparators.get("market")
        != "POINT_IN_TIME_DESPREAD_KALSHI_YES_PROBABILITY"
        or comparators.get("v14")
        != "FROZEN_V14_REFIT_AND_NESTED_TRUST_SELECTED_ON_IDENTICAL_EARLIER_TRAINING_WINDOWS"
        or
        comparators.get("candidate_v14_and_market_use_identical_outer_validation_rows")
        is not True
        or comparators.get("candidate_v14_and_market_use_identical_untouched_test_rows")
        is not True
        or comparators.get("v14_control_may_not_receive_path_features")
        is not True
    ):
        raise ValueError("v15_path_protocol_comparators_invalid")
    bootstrap = protocol.get("paired_close_window_bootstrap")
    if not isinstance(bootstrap, Mapping) or (
        bootstrap.get("version") != "q15-rti-paired-close-window-bootstrap-v1"
        or bootstrap.get("cluster_key") != "close_time"
        or bootstrap.get("same_close_assets_resampled_together") is not True
        or int(bootstrap.get("resamples") or 0) != 5000
        or float(bootstrap.get("confidence_level") or 0.0) != 0.9
        or int(bootstrap.get("candidate_minus_market_random_seed") or 0)
        != 2026072203
        or int(bootstrap.get("candidate_minus_v14_random_seed") or 0)
        != 2026072204
        or bootstrap.get("one_sided_upper_bound_reported") is not True
        or bootstrap.get("two_sided_interval_reported") is not True
        or bootstrap.get("loss_delta_direction")
        != "CANDIDATE_MINUS_COMPARATOR"
    ):
        raise ValueError("v15_path_protocol_bootstrap_invalid")
    gate = protocol.get("walk_forward_gate")
    if not isinstance(gate, Mapping) or (
        gate.get("accuracy_is_report_only") is not True
        or float(gate.get(
            "aggregate_candidate_minus_market_brier_mean_must_be_at_most"
        ) or 0.0) != -0.001
        or float(gate.get(
            "aggregate_candidate_minus_market_log_loss_mean_must_be_at_most"
        ) or 0.0) != -0.001
        or float(gate.get(
            "aggregate_candidate_minus_market_bootstrap_upper_must_be_at_most"
        ) or 0.0) != -0.001
        or float(gate.get(
            "aggregate_candidate_minus_v14_brier_mean_must_be_at_most"
        ) or 0.0) != -0.00025
        or float(gate.get(
            "aggregate_candidate_minus_v14_log_loss_mean_must_be_at_most"
        ) or 0.0) != -0.00025
        or gate.get("untouched_test_may_be_read_when_gate_fails") is not False
        or gate.get("fallback_when_not_met")
        != "NO_V15_ARTIFACT_OR_PAPER_CHALLENGER"
    ):
        raise ValueError("v15_path_protocol_gate_invalid")
    for key in (
        "candidate_brier_must_beat_market",
        "candidate_log_loss_must_beat_market",
        "candidate_brier_must_beat_v14",
        "candidate_log_loss_must_beat_v14",
        "every_fold_brier_and_log_loss_must_not_worsen_vs_market",
        "every_fold_brier_and_log_loss_must_not_worsen_vs_v14",
        "aggregate_candidate_minus_v14_bootstrap_upper_must_be_below_zero",
    ):
        if gate.get(key) is not True:
            raise ValueError(f"v15_path_protocol_gate_guard_missing:{key}")
    calibration = protocol.get("calibration_gate")
    if not isinstance(calibration, Mapping) or (
        calibration.get("population")
        != "CALIBRATION_ROWS_BEFORE_ANY_ENTRY_FILTER"
        or calibration.get(
            "candidate_brier_and_log_loss_must_pass_walk_forward_effect_floors_vs_market"
        ) is not True
        or calibration.get(
            "candidate_brier_and_log_loss_must_pass_walk_forward_effect_floors_vs_v14"
        ) is not True
        or calibration.get(
            "first_half_must_not_worsen_brier_or_log_loss_vs_either_comparator"
        ) is not True
        or calibration.get(
            "second_half_must_not_worsen_brier_or_log_loss_vs_either_comparator"
        ) is not True
        or calibration.get(
            "factor_reselected_from_development_plus_calibration_inner_oof_only_after_gate"
        ) is not True
        or calibration.get("fallback_when_not_met")
        != "NO_V15_ARTIFACT_OR_PAPER_CHALLENGER"
    ):
        raise ValueError("v15_path_protocol_calibration_gate_invalid")
    untouched = protocol.get("untouched_test_policy")
    if not isinstance(untouched, Mapping) or (
        untouched.get("scored_once") is not True
        or untouched.get(
            "may_not_tune_features_hyperparameters_factor_grid_entry_rules_or_thresholds"
        ) is not True
        or untouched.get(
            "candidate_brier_and_log_loss_must_pass_effect_floors_vs_market"
        ) is not True
        or untouched.get(
            "candidate_brier_and_log_loss_must_pass_effect_floors_vs_v14"
        ) is not True
        or untouched.get("paired_close_window_bootstrap_upper_must_pass_required_bounds")
        is not True
        or int(untouched.get("minimum_simulated_picks") or 0) != 5
        or untouched.get("fee_and_slippage_adjusted_pnl_must_be_positive")
        is not True
        or untouched.get("historical_results_can_promote") is not False
        or untouched.get("failure_result")
        != "NO_V15_ARTIFACT_OR_PAPER_CHALLENGER"
    ):
        raise ValueError("v15_path_protocol_untouched_test_policy_invalid")
    reporting = protocol.get("fixed_reporting")
    if not isinstance(reporting, Mapping) or (
        list(reporting.get("all_probability_rows") or ()) != [
            "rows", "close_windows", "accuracy", "wilson_95_low",
            "wilson_95_high", "brier_score", "log_loss",
            "expected_calibration_error", "maximum_calibration_error",
        ]
        or list(reporting.get("comparisons") or ()) != [
            "candidate_minus_market_brier_and_log_loss",
            "candidate_minus_v14_brier_and_log_loss",
            "paired_close_window_bootstrap_intervals_for_both_comparators",
        ]
        or list(reporting.get("subgroups") or ()) != [
            "asset", "rti_side", "absolute_distance_tier",
            "realized_volatility_tier", "market_regime",
            "path_depth_agreement", "path_spread_stress_tier",
        ]
        or reporting.get("rejected_counterfactuals_required") is not True
        or reporting.get("rejected_rows_without_executable_quotes_have_no_pnl")
        is not True
        or list(reporting.get("accepted_picks") or ()) != [
            "picks", "trades_per_day", "accuracy", "wilson_95_low",
            "wilson_95_high", "fee_slippage_adjusted_ten_contract_pnl",
            "ev_cents_per_trade", "maximum_drawdown",
        ]
    ):
        raise ValueError("v15_path_protocol_reporting_invalid")
    entry = protocol.get("entry_policy")
    if not isinstance(entry, Mapping) or (
        entry.get("unchanged_from_v14") is not True
        or float(entry.get("minimum_expected_value_cents_after_costs") or 0.0)
        != 3.0
        or float(entry.get("maximum_ask_cents") or 0.0) != 62.0
        or float(entry.get("maximum_spread_cents") or 0.0) != 1.5
        or float(entry.get("minimum_displayed_depth_contracts") or 0.0)
        != 10.0
        or int(entry.get("simulation_contracts") or 0) != 10
        or entry.get("official_kalshi_fees") is not True
        or float(entry.get("slippage_cents_per_contract") or 0.0) != 2.0
        or entry.get("fake_fill_assumptions_forbidden") is not True
        or entry.get("reused_quotes_forbidden") is not True
    ):
        raise ValueError("v15_path_protocol_entry_policy_invalid")
    paper = protocol.get("paper_challenger_policy")
    if not isinstance(paper, Mapping) or (
        paper.get("created_only_after_every_historical_gate_passes") is not True
        or paper.get("prospective_boundary_is_first_unseen_close_after_manual_creation")
        is not True
        or paper.get("historical_credit_allowed") is not False
        or paper.get("notifications_must_say_V15_and_PAPER") is not True
        or list(paper.get("manual_review_only_at_resolved_picks") or ())
        != [30, 60, 150]
        or paper.get("positive_fee_slippage_adjusted_pnl_required") is not True
        or paper.get(
            "wilson_95_lower_accuracy_must_exceed_cohort_average_fee_adjusted_break_even_rate"
        ) is not True
        or paper.get("durable_ledger_required") is not True
        or paper.get("automatic_settlement_grading_required") is not True
        or paper.get("idempotent_telegram_delivery_required") is not True
        or paper.get(
            "timestamp_leakage_stale_evidence_reused_quotes_and_cross_asset_fold_leakage_forbidden"
        ) is not True
        or paper.get("automatic_promotion") is not False
        or paper.get("real_trading_allowed") is not False
    ):
        raise ValueError("v15_path_protocol_paper_policy_invalid")
    safety = protocol.get("safety")
    if not isinstance(safety, Mapping) or not _all_false(safety, (
        "protocol_allows_outcome_access_now",
        "protocol_allows_model_fit_now",
        "protocol_allows_artifact_now",
        "protocol_allows_probability_scoring_now",
        "protocol_allows_notifications_now",
        "protocol_allows_automatic_promotion",
        "protocol_allows_real_trading",
    )):
        raise ValueError("v15_path_protocol_safety_invalid")


def validate_preregistration(
    charter: Mapping[str, Any], protocol: Mapping[str, Any],
) -> dict[str, Any]:
    validate_charter(charter)
    validate_protocol(protocol, charter)
    return {
        "status": "VALID_OUTCOME_BLIND_PREREGISTRATION",
        "charter_id": CHARTER_ID,
        "charter_sha256": CHARTER_SHA256,
        "proposed_design_id": PROPOSED_DESIGN_ID,
        "evaluation_protocol_id": EVALUATION_PROTOCOL_ID,
        "evaluation_protocol_sha256": EVALUATION_PROTOCOL_SHA256,
        "added_feature_count": 5,
        "total_feature_count": 25,
        "geometry_review_required_at_windows": 30,
        "non_btc_minimum_complete_windows": 60,
        "btc_minimum_complete_windows": 150,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "executable_design_created": False,
        "automatic_scoring": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--charter", default=str(DEFAULT_CHARTER))
    parser.add_argument("--protocol", default=str(DEFAULT_PROTOCOL))
    args = parser.parse_args()
    result = validate_preregistration(
        load_charter(Path(args.charter)), load_protocol(Path(args.protocol)),
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
