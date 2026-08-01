"""Validate the frozen V15 prospective PAPER deployment/review contract.

This command is outcome-blind.  It cannot read a database, fit or score a
model, create an artifact, send a notification, promote a rule, or place an
order.  The canonical protocol hash makes every field immutable as a unit;
the explicit checks below make the safety contract reviewable.
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

from q15_upgrade.strategy_bots import (
    rti_microstructure_v15_paper_identity as identity,
)
from q15_upgrade.strategy_bots.rti_microstructure_v15_audit_identity import (
    REPORTING_PROTOCOL_ID,
    REPORTING_PROTOCOL_SHA256,
    SETTLEMENT_EVIDENCE_VERSION,
)
from q15_upgrade.strategy_bots.rti_microstructure_v15_identity import (
    DESIGN_ID,
    DESIGN_SHA256,
    EVALUATION_PROTOCOL_ID,
    EVALUATION_PROTOCOL_SHA256,
)
from tools.q15_rti_microstructure_preregister import design_fingerprint


DEFAULT_PROTOCOL = ROOT / identity.PROTOCOL_RELATIVE_PATH


def load_protocol(path: Path = DEFAULT_PROTOCOL) -> dict[str, Any]:
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("v15_paper_protocol_unreadable") from exc
    if not isinstance(decoded, Mapping):
        raise ValueError("v15_paper_protocol_root_not_object")
    return dict(decoded)


def _all_false(data: Mapping[str, Any], keys: tuple[str, ...]) -> bool:
    return all(data.get(key) is False for key in keys)


def validate_protocol(protocol: Mapping[str, Any]) -> dict[str, Any]:
    if design_fingerprint(protocol) != identity.PROTOCOL_SHA256:
        raise ValueError("v15_paper_protocol_sha256_mismatch")
    if (
        protocol.get("protocol_id") != identity.PROTOCOL_ID
        or protocol.get("protocol_status")
        != "PREREGISTERED_BEFORE_ANY_V15_OUTCOME_REVIEW"
        or protocol.get("applies_to_design_id") != DESIGN_ID
        or protocol.get("applies_to_design_sha256") != DESIGN_SHA256
        or protocol.get("applies_to_evaluation_protocol_id")
        != EVALUATION_PROTOCOL_ID
        or protocol.get("applies_to_evaluation_protocol_sha256")
        != EVALUATION_PROTOCOL_SHA256
        or protocol.get("applies_to_reporting_protocol_id")
        != REPORTING_PROTOCOL_ID
        or protocol.get("applies_to_reporting_protocol_sha256")
        != REPORTING_PROTOCOL_SHA256
        or protocol.get("settlement_evidence_version")
        != SETTLEMENT_EVIDENCE_VERSION
        or protocol.get("outcome_labels_used_for_protocol") is not False
        or protocol.get(
            "performance_metrics_inspected_before_preregistration"
        ) is not False
        or protocol.get("paper_artifact_created") is not False
        or protocol.get("runtime_scoring_connected") is not False
        or protocol.get("notifications_enabled") is not False
        or protocol.get("automatic_promotion") is not False
        or protocol.get("real_trading_allowed") is not False
    ):
        raise ValueError("v15_paper_protocol_identity_or_safety_invalid")

    activation = protocol.get("activation_prerequisites")
    if not isinstance(activation, Mapping) or (
        activation.get("manual_activation_only") is not True
        or activation.get("ready_audit_seal_required") is not True
        or activation.get("passing_finalized_pretest_required") is not True
        or activation.get(
            "passing_finalized_untouched_test_required"
        ) is not True
        or activation.get(
            "authoritative_settlement_evidence_required_for_both_label_stages"
        ) is not True
        or activation.get(
            "historical_gate_failure_result"
        ) != "NO_V15_ARTIFACT_OR_PAPER_CHALLENGER"
        or activation.get("historical_results_can_promote") is not False
        or activation.get("cohort_artifacts_are_separate") is not True
        or activation.get(
            "untouched_test_labels_may_not_enter_artifact_model_fit"
        ) is not True
        or activation.get(
            "artifact_model_training_population"
        ) != "SEALED_PRETEST_TRAIN_PLUS_CALIBRATION_ONLY"
        or activation.get(
            "artifact_must_reuse_exact_passing_pretest_model_and_selected_trust"
        ) is not True
        or activation.get("manual_confirmation_phrase")
        != "CREATE_V15_PAPER_CHALLENGER_FROM_PASSING_AUDIT"
    ):
        raise ValueError("v15_paper_protocol_activation_invalid")

    boundary = protocol.get("prospective_boundary")
    if not isinstance(boundary, Mapping) or (
        boundary.get("historical_credit_allowed") is not False
        or boundary.get(
            "decision_timestamp_formula"
        ) != "close_time_minus_780_seconds"
        or boundary.get("partial_activation_window_forbidden") is not True
        or boundary.get(
            "earlier_rows_must_never_be_reclassified_as_prospective"
        ) is not True
        or boundary.get("same_close_assets_share_boundary") is not True
    ):
        raise ValueError("v15_paper_protocol_boundary_invalid")

    artifact = protocol.get("artifact_manifest")
    if not isinstance(artifact, Mapping) or (
        artifact.get("artifact_version") != identity.ARTIFACT_VERSION
        or artifact.get("one_artifact_per_cohort") is not True
        or artifact.get("paper_only_must_be_true") is not True
        or artifact.get("notification_label_must_equal") != "V15 PAPER"
        or artifact.get("automatic_promotion_must_be_false") is not True
        or artifact.get("real_trading_allowed_must_be_false") is not True
        or artifact.get("automatic_refit_forbidden") is not True
        or artifact.get("artifact_overwrite_forbidden") is not True
        or "untouched_test_result_state_sha256"
        not in list(artifact.get("required_hash_bindings") or ())
        or "paper_deployment_protocol_sha256"
        not in list(artifact.get("required_hash_bindings") or ())
    ):
        raise ValueError("v15_paper_protocol_artifact_invalid")

    population = protocol.get("runtime_population")
    if not isinstance(population, Mapping) or (
        population.get("decision_interval") != "EXACT_13M"
        or population.get(
            "one_opportunity_row_per_asset_per_complete_close_window"
        ) is not True
        or population.get("cohort_mixing_forbidden") is not True
        or population.get(
            "data_ineligible_rows_receive_no_hypothetical_fill_or_pnl"
        ) is not True
        or population.get("rejected_counterfactuals_are_preserved") is not True
        or set(dict(population.get("allowed_cohorts") or {}))
        != {"NON_BTC_TRANSFER", "BTC"}
    ):
        raise ValueError("v15_paper_protocol_population_invalid")

    evidence = protocol.get("evidence_and_timestamp_gate")
    if not isinstance(evidence, Mapping) or (
        evidence.get("source_record_kind")
        != "RTI_PATH_13M_PROSPECTIVE_EXACT"
        or float(evidence.get("exact_capture_offset_maximum_seconds") or 0)
        != 2.0
        or evidence.get(
            "complete_seven_asset_source_window_required_for_every_cohort"
        ) is not True
        or evidence.get(
            "complete_independent_coinbase_and_kraken_paths_required"
        ) is not True
        or evidence.get("canonical_source_path_hash_required") is not True
        or evidence.get("source_path_hash_must_recompute") is not True
        or evidence.get("v14_base_features_must_recompute") is not True
        or evidence.get("all_25_v15_features_must_recompute") is not True
        or evidence.get(
            "feature_or_quote_timestamp_after_decision_forbidden"
        ) is not True
        or evidence.get("stale_evidence_forbidden") is not True
        or evidence.get("reused_quote_forbidden") is not True
        or evidence.get(
            "new_point_in_time_quote_required_for_each_asset"
        ) is not True
        or evidence.get("any_failure_status") != "DATA_INELIGIBLE"
    ):
        raise ValueError("v15_paper_protocol_evidence_invalid")

    entry = protocol.get("probability_and_entry_policy")
    if not isinstance(entry, Mapping) or (
        entry.get("probability_formula_must_match_frozen_v15") is not True
        or entry.get("selected_trust_must_equal_artifact") is not True
        or float(entry.get("minimum_expected_value_cents_after_costs") or 0)
        != 3.0
        or float(entry.get("maximum_ask_cents") or 0) != 62.0
        or float(entry.get("maximum_spread_cents") or 0) != 1.5
        or float(entry.get("minimum_displayed_depth_contracts") or 0)
        != 10.0
        or int(entry.get("simulation_contracts") or 0) != 10
        or entry.get("official_kalshi_fees") is not True
        or float(entry.get("slippage_cents_per_contract") or 0) != 2.0
        or entry.get("fake_fill_assumptions_forbidden") is not True
        or entry.get(
            "accepted_pick_requires_depth_for_all_simulated_contracts"
        ) is not True
    ):
        raise ValueError("v15_paper_protocol_entry_invalid")

    ledger = protocol.get("durable_ledger")
    notification_state = (
        ledger.get("notification_state_machine")
        if isinstance(ledger, Mapping)
        else None
    )
    if not isinstance(ledger, Mapping) or (
        ledger.get("ledger_version") != identity.LEDGER_VERSION
        or ledger.get("default_relative_path")
        != identity.DEFAULT_LEDGER_RELATIVE_PATH
        or ledger.get("sqlite_wal_required") is not True
        or ledger.get("decision_insert_precedes_notification_enqueue")
        is not True
        or ledger.get("decision_identity_unique") is not True
        or ledger.get("metadata_keys_are_insert_once") is not True
        or ledger.get(
            "duplicate_decision_returns_existing_row_without_rescore"
        ) is not True
        or ledger.get(
            "crash_recovery_replays_only_incomplete_side_effects"
        ) is not True
        or ledger.get("historical_rows_may_not_be_inserted") is not True
        or "official_result"
        not in list(ledger.get("nullable_once_compare_and_set_columns") or ())
        or "fee_slippage_adjusted_pnl_cents"
        not in list(ledger.get("nullable_once_compare_and_set_columns") or ())
        or not isinstance(notification_state, Mapping)
        or notification_state.get(
            "idempotency_key_is_immutable_after_first_write"
        ) is not True
        or notification_state.get(
            "terminal_state_may_not_transition"
        ) is not True
        or list(notification_state.get("terminal_states") or ())
        != ["SENT", "EXPIRED", "DEAD_LETTER", "MUTED"]
    ):
        raise ValueError("v15_paper_protocol_ledger_invalid")

    telegram = protocol.get("telegram_delivery")
    if not isinstance(telegram, Mapping) or (
        telegram.get("notifications_only_for_accepted_paper_rows") is not True
        or telegram.get("durable_outbox_required") is not True
        or telegram.get("network_io_in_capture_thread_forbidden") is not True
        or telegram.get("enqueue_expiry_equals_market_close_time") is not True
        or telegram.get("terminal_delivery_may_not_be_reenqueued") is not True
        or telegram.get("notification_is_not_a_trade_or_promotion") is not True
        or "V15 PAPER" not in list(telegram.get("message_must_contain") or ())
        or "paper_only_no_real_order"
        not in list(telegram.get("message_must_contain") or ())
    ):
        raise ValueError("v15_paper_protocol_telegram_invalid")

    settlement = protocol.get("settlement_grading")
    if not isinstance(settlement, Mapping) or (
        settlement.get("source_id") != "KALSHI_PUBLIC_MARKET_API"
        or settlement.get("official_market_result_field") != "result"
        or settlement.get("required_market_status") != "finalized"
        or settlement.get("returned_ticker_must_equal_decision_ticker")
        is not True
        or settlement.get("conflicting_results_fail_closed") is not True
        or settlement.get("settled_result_may_not_be_overwritten") is not True
        or settlement.get("pnl_uses_recorded_point_in_time_quote") is not True
        or settlement.get("pnl_uses_official_fee_schedule") is not True
        or settlement.get(
            "pnl_uses_two_cent_slippage_per_contract"
        ) is not True
        or int(settlement.get("pnl_contracts") or 0) != 10
        or settlement.get(
            "unaccepted_rows_have_no_actual_or_paper_pnl"
        ) is not True
        or settlement.get(
            "entry_policy_rejections_with_complete_execution_evidence_may_have_separately_named_counterfactual_pnl_in_reports"
        ) is not True
        or settlement.get(
            "counterfactual_pnl_may_not_enter_accepted_pick_totals_or_promotion_gates"
        ) is not True
        or settlement.get(
            "data_ineligible_or_model_error_rows_may_not_have_counterfactual_pnl"
        ) is not True
    ):
        raise ValueError("v15_paper_protocol_settlement_invalid")

    health = protocol.get("health_contract")
    if not isinstance(health, Mapping) or (
        health.get("artifact_identity_and_hash_status_required") is not True
        or health.get("source_feed_freshness_by_venue_required") is not True
        or health.get(
            "settlement_pending_count_and_oldest_age_required"
        ) is not True
        or health.get("settlement_conflict_count_required") is not True
        or health.get(
            "timestamp_or_hash_violation_count_required"
        ) is not True
        or health.get(
            "invalid_artifact_or_stale_source_disables_scoring_and_notification"
        ) is not True
        or health.get("automatic_promotion_false_required") is not True
        or health.get("real_trading_false_required") is not True
    ):
        raise ValueError("v15_paper_protocol_health_invalid")

    reviews = protocol.get("prospective_reviews")
    gates = (
        reviews.get("promotion_gates")
        if isinstance(reviews, Mapping)
        else None
    )
    required_gates = (
        "positive_fee_slippage_adjusted_pnl",
        "wilson_95_lower_accuracy_exceeds_average_fee_slippage_adjusted_break_even_rate",
        "candidate_brier_better_than_market",
        "candidate_log_loss_better_than_market",
        "candidate_brier_better_than_v14",
        "candidate_log_loss_better_than_v14",
        "paired_cluster_bootstrap_one_sided_upper_below_zero_vs_market",
        "paired_cluster_bootstrap_one_sided_upper_below_zero_vs_v14",
        "no_timestamp_hash_settlement_or_ledger_integrity_failure",
        "no_cross_cohort_contamination",
    )
    if not isinstance(reviews, Mapping) or not isinstance(gates, Mapping) or (
        list(reviews.get("review_bars_resolved_accepted_picks") or ())
        != [30, 60, 150]
        or reviews.get("cohorts_reviewed_separately") is not True
        or reviews.get(
            "chronological_population"
        ) != "EARLIEST_N_PROSPECTIVE_RESOLVED_ACCEPTED_PICKS"
        or reviews.get("same_close_assets_are_one_bootstrap_cluster")
        is not True
        or reviews.get("review_population_may_not_be_subselected") is not True
        or not all(gates.get(key) is True for key in required_gates)
        or reviews.get("manual_review_only") is not True
        or reviews.get("automatic_promotion") is not False
        or reviews.get("real_trading_allowed") is not False
        or reviews.get(
            "historical_results_never_count_toward_review_bars"
        ) is not True
    ):
        raise ValueError("v15_paper_protocol_reviews_invalid")

    safety = protocol.get("safety")
    if not isinstance(safety, Mapping) or not _all_false(
        safety,
        (
            "protocol_allows_artifact_creation_now",
            "protocol_allows_runtime_scoring_now",
            "protocol_allows_notifications_now",
            "protocol_allows_automatic_refit",
            "protocol_allows_automatic_promotion",
            "protocol_allows_real_trading",
        ),
    ):
        raise ValueError("v15_paper_protocol_runtime_safety_invalid")

    return {
        "status": "VALID_OUTCOME_BLIND_V15_PAPER_DEPLOYMENT_PROTOCOL",
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "artifact_version": identity.ARTIFACT_VERSION,
        "ledger_version": identity.LEDGER_VERSION,
        "review_bars": [30, 60, 150],
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
    protocol = load_protocol(Path(args.protocol))
    print(json.dumps(validate_protocol(protocol), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
