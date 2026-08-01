from __future__ import annotations

import copy
from pathlib import Path

import pytest

from q15_upgrade.strategy_bots.rti_independent_path import DERIVED_FEATURE_KEYS
from q15_upgrade.strategy_bots.rti_independent_path_successor_identity import (
    CHARTER_ID,
    CHARTER_SHA256,
    EVALUATION_PROTOCOL_ID,
    EVALUATION_PROTOCOL_SHA256,
    PROPOSED_DESIGN_ID,
)
from tools import q15_rti_independent_path_successor_preregister as prereg
from tools.q15_rti_microstructure_preregister import design_fingerprint


def _manifests():
    return prereg.load_charter(), prereg.load_protocol()


def test_v15_path_charter_and_protocol_identities_are_frozen():
    charter, protocol = _manifests()
    assert charter["charter_id"] == CHARTER_ID
    assert design_fingerprint(charter) == CHARTER_SHA256
    assert protocol["protocol_id"] == EVALUATION_PROTOCOL_ID
    assert design_fingerprint(protocol) == EVALUATION_PROTOCOL_SHA256
    assert protocol["applies_to_proposed_design_id"] == PROPOSED_DESIGN_ID
    prereg.validate_preregistration(charter, protocol)


def test_preregistration_used_only_feature_geometry_and_has_no_authority():
    charter, protocol = _manifests()
    evidence = charter["evidence_available_at_preregistration"]
    assert evidence["complete_reconstructable_close_windows"] == 16
    assert evidence["geometry_review_ready"] is False
    assert evidence["outcome_columns_selected"] is False
    assert evidence["outcome_labels_read"] is False
    assert evidence["model_fit_performed"] is False
    assert evidence["performance_metrics_inspected"] is False
    summary = prereg.validate_preregistration(charter, protocol)
    assert summary["executable_design_created"] is False
    assert summary["outcome_labels_read"] is False
    assert summary["model_fit_performed"] is False
    assert summary["automatic_scoring"] is False
    assert summary["notification_eligible"] is False
    assert summary["automatic_promotion"] is False
    assert summary["real_trading_allowed"] is False


def test_single_candidate_adds_exactly_five_frozen_features_to_v14():
    charter, protocol = _manifests()
    candidate = charter["proposed_candidate"]
    assert candidate["base_feature_count"] == 20
    assert candidate["added_feature_count"] == 5
    assert candidate["total_feature_count"] == 25
    assert charter["fixed_added_feature_names_in_order"] == list(
        DERIVED_FEATURE_KEYS
    )
    assert candidate["feature_interactions_allowed"] is False
    assert candidate["polynomial_expansion_allowed"] is False
    assert candidate["automatic_feature_selection_allowed"] is False
    assert candidate["automatic_hyperparameter_search_allowed"] is False
    architecture = protocol["candidate_architecture"]
    assert architecture["base_optimizer_and_training_config_must_equal_v14"]
    assert architecture["nested_residual_trust_architecture_must_equal_v14"]
    assert architecture["factor_zero_is_exact_kalshi_market_prior"]
    assert architecture["missing_path_imputation_forbidden"]


def test_geometry_and_cohort_readiness_fail_closed_before_labels():
    charter, protocol = _manifests()
    geometry = protocol["geometry_prerequisite"]
    assert geometry["review_window"] == 30
    assert geometry["outcome_labels_must_remain_unread"] is True
    assert geometry["model_fit_before_pass_forbidden"] is True
    assert geometry[
        "failure_does_not_allow_automatic_feature_or_threshold_change"
    ] is True
    assert protocol["cohorts"]["NON_BTC_TRANSFER"][
        "minimum_complete_close_windows"
    ] == 60
    assert protocol["cohorts"]["BTC"][
        "minimum_complete_close_windows"
    ] == 150
    readiness = charter["readiness_prerequisites"]
    assert readiness[
        "btc_and_non_btc_must_be_fit_scored_and_gated_separately"
    ] is True
    assert readiness["non_btc_labels_may_not_be_read_before_non_btc_readiness"]
    assert readiness["btc_labels_may_not_be_read_before_btc_readiness"]


@pytest.mark.parametrize(
    ("cohort", "expected_total", "expected_assets"),
    [
        ("NON_BTC_TRANSFER", 60, ["BNB", "DOGE", "ETH", "HYPE", "SOL", "XRP"]),
        ("BTC", 150, ["BTC"]),
    ],
)
def test_outer_inner_and_untouched_fold_geometry_is_chronological(
    cohort, expected_total, expected_assets,
):
    _, protocol = _manifests()
    raw = protocol["cohorts"][cohort]
    assert raw["assets"] == expected_assets
    assert (
        raw["initial_train_windows"]
        + raw["validation_block_windows"] * raw["walk_forward_fold_count"]
        + raw["untouched_test_windows"]
    ) == expected_total
    assert (
        raw["development_train_windows"]
        + raw["calibration_windows"]
        + raw["untouched_test_windows"]
    ) == expected_total
    for fold_index in range(raw["walk_forward_fold_count"]):
        outer_train = (
            raw["initial_train_windows"]
            + fold_index * raw["validation_block_windows"]
        )
        assert (
            outer_train - raw["inner_initial_train_windows"]
        ) % raw["inner_validation_block_windows"] == 0
    assert protocol["fold_policy"][
        "all_validation_times_strictly_after_training_times"
    ] is True
    assert protocol["fold_policy"][
        "untouched_test_labels_may_select_factor"
    ] is False


def test_candidate_must_materially_beat_market_and_frozen_v14():
    _, protocol = _manifests()
    gate = protocol["walk_forward_gate"]
    assert gate["accuracy_is_report_only"] is True
    assert gate["candidate_brier_must_beat_market"] is True
    assert gate["candidate_log_loss_must_beat_market"] is True
    assert gate["candidate_brier_must_beat_v14"] is True
    assert gate["candidate_log_loss_must_beat_v14"] is True
    assert gate[
        "aggregate_candidate_minus_market_brier_mean_must_be_at_most"
    ] == -0.001
    assert gate[
        "aggregate_candidate_minus_v14_brier_mean_must_be_at_most"
    ] == -0.00025
    assert gate["untouched_test_may_be_read_when_gate_fails"] is False
    assert protocol["comparators"][
        "candidate_v14_and_market_use_identical_outer_validation_rows"
    ] is True
    assert protocol["comparators"]["v14_control_may_not_receive_path_features"]


def test_economics_are_executable_quote_only_and_promotion_is_prospective():
    charter, protocol = _manifests()
    entry = protocol["entry_policy"]
    assert entry["minimum_expected_value_cents_after_costs"] == 3.0
    assert entry["maximum_ask_cents"] == 62.0
    assert entry["maximum_spread_cents"] == 1.5
    assert entry["simulation_contracts"] == 10
    assert entry["official_kalshi_fees"] is True
    assert entry["slippage_cents_per_contract"] == 2.0
    assert entry["fake_fill_assumptions_forbidden"] is True
    assert entry["reused_quotes_forbidden"] is True
    paper = protocol["paper_challenger_policy"]
    assert paper["historical_credit_allowed"] is False
    assert paper["manual_review_only_at_resolved_picks"] == [30, 60, 150]
    assert paper["positive_fee_slippage_adjusted_pnl_required"] is True
    assert paper[
        "wilson_95_lower_accuracy_must_exceed_cohort_average_fee_adjusted_break_even_rate"
    ] is True
    assert paper["automatic_promotion"] is False
    assert paper["real_trading_allowed"] is False
    assert charter["source_history_policy"][
        "paper_challenger_receives_historical_credit"
    ] is False


def test_manifest_tampering_is_rejected_before_semantic_use():
    charter, protocol = _manifests()
    bad_charter = copy.deepcopy(charter)
    bad_charter["proposed_candidate"]["added_feature_count"] = 6
    with pytest.raises(ValueError, match="v15_path_charter_sha256_mismatch"):
        prereg.validate_charter(bad_charter)
    bad_protocol = copy.deepcopy(protocol)
    bad_protocol["walk_forward_gate"][
        "aggregate_candidate_minus_v14_brier_mean_must_be_at_most"
    ] = 0.0
    with pytest.raises(ValueError, match="v15_path_protocol_sha256_mismatch"):
        prereg.validate_protocol(bad_protocol, charter)


@pytest.mark.parametrize(
    ("path", "replacement", "error"),
    [
        (
            ("proposed_candidate", "feature_schema_version"),
            "wrong-schema",
            "v15_path_charter_candidate_identity_invalid",
        ),
        (
            (
                "readiness_prerequisites",
                "non_btc_labels_may_not_be_read_before_non_btc_readiness",
            ),
            False,
            "v15_path_charter_readiness_invalid",
        ),
        (
            ("required_comparisons",),
            ["v15_candidate_vs_point_in_time_kalshi_market_prior"],
            "v15_path_charter_comparators_invalid",
        ),
    ],
)
def test_charter_semantics_reject_unsafe_rehashed_manifest(
    monkeypatch, path, replacement, error,
):
    charter, _ = _manifests()
    altered = copy.deepcopy(charter)
    target = altered
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = replacement
    monkeypatch.setattr(prereg, "CHARTER_SHA256", design_fingerprint(altered))
    with pytest.raises(ValueError, match=error):
        prereg.validate_charter(altered)


@pytest.mark.parametrize(
    ("path", "replacement", "error"),
    [
        (
            (
                "candidate_architecture",
                "base_optimizer_and_training_config_must_equal_v14",
            ),
            False,
            "v15_path_protocol_architecture_invalid",
        ),
        (
            ("population", "timestamp_alignment_fail_closed"),
            False,
            "v15_path_protocol_population_invalid",
        ),
        (
            ("fold_policy", "inner"),
            "RANDOM_FOLDS",
            "v15_path_protocol_fold_policy_invalid",
        ),
        (
            ("comparators", "market"),
            "POST_SETTLEMENT_MARKET",
            "v15_path_protocol_comparators_invalid",
        ),
        (
            ("paired_close_window_bootstrap", "version"),
            "row-bootstrap",
            "v15_path_protocol_bootstrap_invalid",
        ),
        (
            (
                "walk_forward_gate",
                "aggregate_candidate_minus_market_bootstrap_upper_must_be_at_most",
            ),
            0.0,
            "v15_path_protocol_gate_invalid",
        ),
        (
            ("calibration_gate", "population"),
            "ACCEPTED_PICKS_ONLY",
            "v15_path_protocol_calibration_gate_invalid",
        ),
        (
            ("fixed_reporting", "accepted_picks"),
            ["picks", "accuracy"],
            "v15_path_protocol_reporting_invalid",
        ),
        (
            ("paper_challenger_policy", "notifications_must_say_V15_and_PAPER"),
            False,
            "v15_path_protocol_paper_policy_invalid",
        ),
    ],
)
def test_protocol_semantics_reject_unsafe_rehashed_manifest(
    monkeypatch, path, replacement, error,
):
    charter, protocol = _manifests()
    altered = copy.deepcopy(protocol)
    target = altered
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = replacement
    monkeypatch.setattr(
        prereg, "EVALUATION_PROTOCOL_SHA256", design_fingerprint(altered)
    )
    with pytest.raises(ValueError, match=error):
        prereg.validate_protocol(altered, charter)


def test_validator_has_no_database_or_model_execution_path():
    source = Path(prereg.__file__).read_text(encoding="utf-8")
    assert "import sqlite3" not in source
    assert "load_feature_rows" not in source
    assert ".execute(" not in source
    assert "fit_residual_model" not in source
    assert "ReliableTelegramOutbox" not in source
    assert "V3Telegram" not in source
