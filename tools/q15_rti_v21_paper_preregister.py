"""Validate V21's frozen prospective PAPER deployment/review contract.

This outcome-blind command cannot read a database, fit or score a model,
create an artifact, send a notification, promote a rule, or place an order.
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

from q15_upgrade.strategy_bots import rti_microstructure_v21_identity as source_identity
from q15_upgrade.strategy_bots import rti_microstructure_v21_paper_identity as identity
from q15_upgrade.strategy_bots.rti_microstructure_v15_audit_identity import (
    SETTLEMENT_EVIDENCE_VERSION,
)
from tools.q15_rti_microstructure_preregister import design_fingerprint


DEFAULT_PROTOCOL = ROOT / identity.PROTOCOL_RELATIVE_PATH


def load_protocol(path: Path = DEFAULT_PROTOCOL) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("v21_paper_protocol_unreadable") from exc
    if not isinstance(value, Mapping):
        raise ValueError("v21_paper_protocol_root_not_object")
    return dict(value)


def _mapping(protocol: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = protocol.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"v21_paper_protocol_{key}_invalid")
    return value


def validate_protocol(protocol: Mapping[str, Any]) -> dict[str, Any]:
    if design_fingerprint(protocol) != identity.PROTOCOL_SHA256:
        raise ValueError("v21_paper_protocol_sha256_mismatch")
    disclosure = _mapping(protocol, "outcome_blind_freeze_disclosure")
    if (
        protocol.get("protocol_id") != identity.PROTOCOL_ID
        or protocol.get("protocol_status")
        != "FROZEN_BEFORE_ANY_V21_ELIGIBLE_EVIDENCE_OUTCOME_OR_MODEL_FIT"
        or protocol.get("applies_to_design_id") != source_identity.DESIGN_ID
        or protocol.get("applies_to_source_protocol_id")
        != source_identity.PROTOCOL_ID
        or protocol.get("applies_to_source_protocol_sha256")
        != source_identity.PROTOCOL_SHA256
        or protocol.get("applies_to_evaluator_contract_id")
        != source_identity.EVALUATOR_CONTRACT_ID
        or protocol.get("applies_to_evaluator_contract_sha256")
        != source_identity.EVALUATOR_CONTRACT_SHA256
        or protocol.get("feature_builder_version")
        != source_identity.FEATURE_BUILDER_VERSION
        or int(protocol.get("feature_count") or 0) != source_identity.FEATURE_COUNT
        or protocol.get("feature_names_sha256")
        != source_identity.FEATURE_NAMES_SHA256
        or protocol.get("settlement_evidence_version")
        != SETTLEMENT_EVIDENCE_VERSION
        or int(
            disclosure.get("v21_eligible_rows_before_freeze")
            if disclosure.get("v21_eligible_rows_before_freeze") is not None
            else -1
        ) != 0
        or disclosure.get(
            "final_statistical_amendment_completed_before_first_eligible_close"
        ) is not True
        or disclosure.get(
            "final_feature_lineage_audit_completed_before_first_eligible_close"
        ) is not True
        or disclosure.get(
            "final_fair_ablation_audit_completed_before_first_eligible_close"
        ) is not True
        or disclosure.get("v21_outcomes_or_resolution_status_inspected")
        is not False
        or disclosure.get("v21_model_fit_or_probability_scoring_performed")
        is not False
        or disclosure.get("performance_metrics_inspected_before_freeze")
        is not False
        or any(disclosure.get(key) is not False for key in (
            "paper_artifact_created", "runtime_scoring_connected",
            "notifications_enabled", "automatic_promotion",
            "real_trading_allowed",
        ))
    ):
        raise ValueError("v21_paper_protocol_identity_or_freeze_invalid")

    activation = _mapping(protocol, "activation_prerequisites")
    if (
        activation.get("manual_activation_only") is not True
        or activation.get("exclusive_earliest_180_feature_seal_required")
        is not True
        or activation.get("passing_finalized_pretest_required") is not True
        or activation.get("passing_finalized_untouched_test_required") is not True
        or activation.get("fresh_authoritative_settlement_and_fee_evidence_required")
        is not True
        or activation.get("historical_gate_failure_result")
        != "NO_V21_PAPER_ARTIFACT_OR_NOTIFICATION"
        or activation.get("historical_results_alone_can_promote") is not False
        or activation.get("cohort_artifacts_are_separate") is not True
        or activation.get("cross_cohort_artifact_or_row_use_forbidden") is not True
        or activation.get("artifact_creation_is_exclusive_and_append_only")
        is not True
        or activation.get("artifact_must_reuse_exact_passing_pretest_base_model_platt_and_margin")
        is not True
        or activation.get("untouched_test_labels_may_not_enter_model_calibration_or_margin")
        is not True
        or activation.get("automatic_refit_recalibration_or_margin_change_forbidden")
        is not True
        or activation.get("manual_confirmation_phrase")
        != "CREATE_V21_PAPER_CHALLENGER_FROM_PASSING_AUDIT"
    ):
        raise ValueError("v21_paper_protocol_activation_invalid")

    boundary = _mapping(protocol, "prospective_boundary")
    if (
        boundary.get("historical_credit_allowed") is not False
        or boundary.get("decision_timestamp_formula")
        != "close_time_minus_720_seconds"
        or boundary.get("partial_activation_window_forbidden") is not True
        or boundary.get("earlier_rows_must_never_be_reclassified_as_prospective")
        is not True
        or boundary.get("boundary_is_persisted_in_artifact_and_ledger_metadata")
        is not True
        or boundary.get("same_close_assets_share_boundary") is not True
    ):
        raise ValueError("v21_paper_protocol_boundary_invalid")

    artifact = _mapping(protocol, "artifact_manifest")
    required_bindings = set(artifact.get("required_hash_bindings") or ())
    if (
        artifact.get("artifact_version") != identity.ARTIFACT_VERSION
        or artifact.get("one_artifact_per_cohort") is not True
        or artifact.get("paper_only_must_be_true") is not True
        or artifact.get("notification_label_must_equal") != "V21 PAPER"
        or artifact.get("automatic_promotion_must_be_false") is not True
        or artifact.get("real_trading_allowed_must_be_false") is not True
        or artifact.get("silent_fallback_to_another_model_forbidden") is not True
        or artifact.get("automatic_refit_forbidden") is not True
        or artifact.get("artifact_overwrite_forbidden") is not True
        or not {
            "paper_deployment_protocol_sha256", "feature_seal_sha256",
            "pretest_result_state_sha256", "audit_model_bundle_sha256",
            "untouched_test_result_state_sha256", "model_payload_sha256",
            "selected_margin_sha256",
        }.issubset(required_bindings)
        or not {
            "selected_spec", "selected_model_id",
            "v20_feature_map_ablation_selected_spec",
            "v20_feature_map_ablation_selected_model_id",
        }.issubset(set(artifact.get("required_fields") or ()))
    ):
        raise ValueError("v21_paper_protocol_artifact_invalid")

    population = _mapping(protocol, "runtime_population")
    cohorts = dict(population.get("allowed_cohorts") or {})
    if (
        population.get("prediction_interval") != "12M"
        or population.get("parent_interval") != "13M"
        or population.get("intermediate_interval") != "12M30S"
        or population.get("one_opportunity_row_per_asset_per_complete_close_window")
        is not True
        or population.get("all_seven_assets_share_one_atomic_source_window")
        is not True
        or cohorts != {
            "NON_BTC_TRANSFER": ["BNB", "DOGE", "ETH", "HYPE", "SOL", "XRP"],
            "BTC": ["BTC"],
        }
        or population.get("cohort_mixing_forbidden") is not True
        or set(population.get("required_record_statuses") or ()) != {
            "ACCEPTED_PAPER", "REJECTED_EDGE_POLICY", "NONEXECUTABLE_BOOK",
            "DATA_INELIGIBLE", "MODEL_ERROR",
        }
        or population.get("accepted_pick_count_includes_only")
        != "ACCEPTED_PAPER"
        or population.get("nonexecuted_or_ineligible_rows_receive_no_hypothetical_fill_or_pnl")
        is not True
    ):
        raise ValueError("v21_paper_protocol_population_invalid")

    evidence = _mapping(protocol, "evidence_and_timestamp_gate")
    if (
        evidence.get("parent_record_kind")
        != "RTI_PATH_13M_PROSPECTIVE_EXACT"
        or evidence.get("intermediate_record_kind")
        != "RTI_PATH_12M30_CONFIRM_PROSPECTIVE"
        or evidence.get("decision_record_kind")
        != "RTI_PATH_12M_CONFIRM_PROSPECTIVE"
        or evidence.get("exact_parent_plus_30s_plus_60s_lineage_required")
        is not True
        or evidence.get("complete_seven_asset_triplet_window_required") is not True
        or int(evidence.get("official_intermediate_path_samples") or 0) != 31
        or int(evidence.get("official_decision_path_samples") or 0) != 61
        or evidence.get("complete_native_fast_spot_paths_required") is not True
        or evidence.get("official_kalshi_websocket_microstructure_history_required")
        is not True
        or evidence.get("canonical_parent_intermediate_delayed_and_feature_hashes_must_recompute")
        is not True
        or evidence.get("all_75_features_must_recompute") is not True
        or evidence.get("feature_or_quote_timestamp_after_prediction_forbidden")
        is not True
        or evidence.get("historical_backfill_or_imputation_forbidden") is not True
        or evidence.get("reused_parent_or_intermediate_quote_for_decision_forbidden")
        is not True
        or evidence.get("new_12m_point_in_time_quote_required_for_each_asset")
        is not True
        or evidence.get("any_failure_status") != "DATA_INELIGIBLE"
    ):
        raise ValueError("v21_paper_protocol_evidence_invalid")

    entry = _mapping(protocol, "probability_and_entry_policy")
    if (
        entry.get("probability_formula_must_use_exact_artifact_base_model_and_platt")
        is not True
        or entry.get("selected_margin_must_equal_cohort_artifact") is not True
        or entry.get("accepted_pick_requires_edge_at_or_above_selected_margin")
        is not True
        or entry.get("accepted_pick_requires_row_level_full_fill_support")
        is not True
        or float(entry.get("minimum_displayed_depth_contracts") or 0.0) != 10.0
        or int(entry.get("simulation_contracts") or 0) != 10
        or entry.get("official_kalshi_general_quadratic_fees") is not True
        or entry.get("required_fee_type") != "quadratic"
        or float(entry.get("required_fee_multiplier") or 0.0) != 1.0
        or float(entry.get("slippage_cents_per_contract") or 0.0) != 2.0
        or entry.get("fee_evaluated_at_adverse_simulated_fill") is not True
        or entry.get("fake_or_partial_fill_assumptions_forbidden") is not True
        or entry.get("entry_policy_may_not_change_during_or_between_review_stages")
        is not True
    ):
        raise ValueError("v21_paper_protocol_entry_invalid")

    ledger = _mapping(protocol, "durable_ledger")
    notification_state = ledger.get("notification_state_machine")
    if (
        ledger.get("ledger_version") != identity.LEDGER_VERSION
        or ledger.get("one_ledger_per_cohort") is not True
        or ledger.get("cross_cohort_rows_in_same_ledger_forbidden") is not True
        or ledger.get("default_relative_paths")
        != identity.DEFAULT_LEDGER_RELATIVE_PATHS
        or ledger.get("sqlite_wal_required") is not True
        or ledger.get("foreign_keys_required") is not True
        or int(ledger.get("busy_timeout_milliseconds") or 0) != 5000
        or ledger.get("decision_insert_precedes_notification_enqueue") is not True
        or ledger.get("decision_identity_unique") is not True
        or ledger.get("metadata_keys_are_insert_once") is not True
        or ledger.get("duplicate_decision_returns_existing_row_without_rescore")
        is not True
        or ledger.get("crash_recovery_replays_only_incomplete_side_effects")
        is not True
        or ledger.get("historical_rows_may_not_be_inserted") is not True
        or "official_result"
        not in set(ledger.get("nullable_once_compare_and_set_columns") or ())
        or not isinstance(notification_state, Mapping)
        or notification_state.get("idempotency_key_is_immutable_after_first_write")
        is not True
        or notification_state.get("terminal_state_may_not_transition") is not True
        or list(notification_state.get("terminal_states") or ())
        != ["SENT", "EXPIRED", "DEAD_LETTER", "MUTED"]
    ):
        raise ValueError("v21_paper_protocol_ledger_invalid")

    telegram = _mapping(protocol, "telegram_delivery")
    message_fields = set(telegram.get("message_must_contain") or ())
    if (
        telegram.get("notifications_only_for_accepted_paper_rows") is not True
        or telegram.get("idempotency_key") != "V21_PAPER_SHA256_DECISION_KEY"
        or telegram.get("durable_outbox_required") is not True
        or telegram.get("network_io_in_capture_thread_forbidden") is not True
        or telegram.get("enqueue_expiry_equals_market_close_time") is not True
        or telegram.get("terminal_delivery_may_not_be_reenqueued") is not True
        or telegram.get("telegram_failure_may_not_rollback_or_delete_decision")
        is not True
        or telegram.get("notification_is_not_a_trade_or_promotion") is not True
        or not {"V21 PAPER", "rule_version", "paper_only_no_real_order"}.issubset(
            message_fields
        )
    ):
        raise ValueError("v21_paper_protocol_telegram_invalid")

    settlement = _mapping(protocol, "settlement_grading")
    if (
        settlement.get("source_id") != "KALSHI_PUBLIC_MARKET_API"
        or settlement.get("official_market_result_field") != "result"
        or settlement.get("required_market_status") != "finalized"
        or settlement.get("returned_ticker_must_equal_decision_ticker") is not True
        or float(settlement.get("returned_close_time_must_equal_decision_close_time_within_seconds") or 0.0)
        != 1.0
        or settlement.get("conflicting_results_fail_closed") is not True
        or settlement.get("resolution_is_compare_and_set_from_null") is not True
        or settlement.get("settled_result_may_not_be_overwritten") is not True
        or settlement.get("pnl_uses_recorded_point_in_time_12m_quote") is not True
        or settlement.get("pnl_uses_official_fee_schedule_and_two_cent_slippage")
        is not True
        or int(settlement.get("pnl_contracts") or 0) != 10
        or settlement.get("unaccepted_rows_have_no_paper_pnl") is not True
        or settlement.get("counterfactual_pnl_may_not_enter_accepted_pick_totals_or_promotion_gates")
        is not True
    ):
        raise ValueError("v21_paper_protocol_settlement_invalid")

    health = _mapping(protocol, "health_contract")
    required_health = (
        "artifact_identity_and_hash_status_required",
        "source_feed_freshness_by_provider_required",
        "settlement_pending_count_and_oldest_age_required",
        "settlement_conflict_count_required",
        "timestamp_hash_or_cohort_violation_count_required",
        "invalid_artifact_or_stale_source_disables_scoring_and_notification",
        "automatic_promotion_false_required", "real_trading_false_required",
    )
    if not all(health.get(key) is True for key in required_health):
        raise ValueError("v21_paper_protocol_health_invalid")

    reviews = _mapping(protocol, "prospective_reviews")
    gates = reviews.get("promotion_gates")
    required_gates = (
        "positive_fee_slippage_adjusted_pnl",
        "wilson_95_lower_accuracy_exceeds_average_break_even",
        "candidate_brier_better_than_market",
        "candidate_log_loss_better_than_market",
        "candidate_brier_better_than_v20_ablation",
        "candidate_log_loss_better_than_v20_ablation",
        "paired_cluster_bootstrap_one_sided_upper_below_zero_vs_market",
        "paired_cluster_bootstrap_one_sided_upper_below_zero_vs_v20_ablation",
        "no_timestamp_hash_settlement_ledger_or_delivery_integrity_failure",
        "no_cross_cohort_contamination",
    )
    if (
        list(reviews.get("review_bars_resolved_accepted_picks") or ())
        != [30, 60, 150]
        or reviews.get("cohorts_reviewed_separately") is not True
        or reviews.get("chronological_population")
        != "EARLIEST_N_PROSPECTIVE_RESOLVED_ACCEPTED_PICKS"
        or reviews.get("same_close_assets_are_one_bootstrap_cluster") is not True
        or reviews.get("review_population_may_not_be_subselected") is not True
        or not isinstance(gates, Mapping)
        or not all(gates.get(key) is True for key in required_gates)
        or reviews.get("trade_frequency_is_reported_but_not_outcome_tuned")
        is not True
        or reviews.get("manual_review_only") is not True
        or reviews.get("manual_decision_requires_exact_review_hash") is not True
        or reviews.get("manual_decision_is_append_only") is not True
        or reviews.get("automatic_promotion") is not False
        or reviews.get("real_trading_allowed") is not False
        or reviews.get("historical_results_never_count_toward_review_bars")
        is not True
    ):
        raise ValueError("v21_paper_protocol_reviews_invalid")

    tests = set(protocol.get("required_tests_before_activation") or ())
    if (
        "no_real_order_refit_or_automatic_promotion_capability" not in tests
        or "telegram_outbox_idempotency_and_expiry" not in tests
        or "review_bar_exact_population_and_cluster_bootstrap" not in tests
    ):
        raise ValueError("v21_paper_protocol_required_tests_invalid")
    safety = _mapping(protocol, "safety")
    if any(safety.get(key) is not False for key in (
        "protocol_allows_artifact_creation_now",
        "protocol_allows_runtime_scoring_now",
        "protocol_allows_notifications_now",
        "protocol_allows_automatic_refit",
        "protocol_allows_automatic_promotion",
        "protocol_allows_real_trading",
    )):
        raise ValueError("v21_paper_protocol_runtime_safety_invalid")

    return {
        "status": "VALID_OUTCOME_BLIND_V21_PAPER_DEPLOYMENT_PROTOCOL",
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "artifact_version": identity.ARTIFACT_VERSION,
        "ledger_version": identity.LEDGER_VERSION,
        "review_bars": [30, 60, 150],
        "v21_eligible_rows_before_freeze": 0,
        "outcome_labels_read": False,
        "artifact_created": False,
        "runtime_scoring_connected": False,
        "notifications_enabled": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol", default=str(DEFAULT_PROTOCOL))
    args = parser.parse_args()
    print(json.dumps(
        validate_protocol(load_protocol(Path(args.protocol))),
        indent=2, sort_keys=True,
    ))


if __name__ == "__main__":
    main()
