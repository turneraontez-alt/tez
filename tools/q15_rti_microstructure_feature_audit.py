"""Outcome-blind numerical audit for the preregistered RTI feature matrix.

The audit deliberately operates on the feature-only SQL allow-list used by the
one-shot freeze procedure.  It can inspect scale, variance, duplication, and
collinearity before modeling is unlocked, but it cannot read settlements, fit
a model, change the pinned design, notify, trade, refit, or promote anything.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots import rti_microstructure as feature_v1
from q15_upgrade.strategy_bots import rti_microstructure_v2 as feature_v2
from q15_upgrade.strategy_bots import rti_microstructure_v3 as feature_v3
from q15_upgrade.strategy_bots import rti_microstructure_v4 as feature_v4
from q15_upgrade.strategy_bots import rti_microstructure_v5 as feature_v5
from q15_upgrade.strategy_bots import rti_microstructure_v6 as feature_v6
from q15_upgrade.strategy_bots import rti_microstructure_v7 as feature_v7
from q15_upgrade.strategy_bots import rti_microstructure_v8 as feature_v8
from q15_upgrade.strategy_bots import rti_microstructure_v9 as feature_v9
from q15_upgrade.strategy_bots import rti_microstructure_v10 as feature_v10
from q15_upgrade.strategy_bots import rti_microstructure_v11 as feature_v11
from q15_upgrade.strategy_bots import rti_microstructure_v12 as feature_v12
from q15_upgrade.strategy_bots import rti_microstructure_v13 as feature_v13
from q15_upgrade.strategy_bots import rti_microstructure_v14 as feature_v14
from tools.q15_rti_feature_coverage_audit import (
    SAFE_FEATURE_PROFILE_KEYS,
    build_report as build_coverage,
    sanitize_feature_rows,
)
from tools.q15_rti_microstructure_freeze import load_feature_rows
from tools.q15_rti_output_integrity import (
    atomic_write_json,
    atomic_write_text,
    bind_design_output_directory,
)
from tools.q15_rti_microstructure_preregister import (
    DEFAULT_DB,
    build_readiness,
    design_fingerprint,
    validate_design,
)


AUDIT_VERSION = "q15-rti-microstructure-feature-audit-v3"
FIRST_REVIEW_WINDOWS = 30
CORRELATION_THRESHOLD = 0.95
EXACT_DUPLICATE_TOLERANCE = 1e-12
DEFAULT_V12_GEOMETRY_REVIEW_PROTOCOL = (
    ROOT / "config" / "q15_rti_v12_geometry_review_protocol.json"
)
EXPECTED_V12_GEOMETRY_REVIEW_PROTOCOL_SHA256 = (
    "8cab81cec789baf4a9bba316e84bfbb06c7ac6e2c747ed5589b10e9c69778aee"
)
DEFAULT_V12_COVARIATE_DRIFT_PROTOCOL = (
    ROOT / "config" / "q15_rti_v12_covariate_drift_protocol.json"
)
EXPECTED_V12_COVARIATE_DRIFT_PROTOCOL_SHA256 = (
    "ced627f34f7d50b8b9a9521bb5ce18bbae939a4008f5a6f37a2a24eda9a66211"
)
DEFAULT_V13_GEOMETRY_REVIEW_PROTOCOL = (
    ROOT / "config" / "q15_rti_v13_geometry_review_protocol.json"
)
EXPECTED_V13_GEOMETRY_REVIEW_PROTOCOL_SHA256 = (
    feature_v13.GEOMETRY_REVIEW_PROTOCOL_SHA256
)
DEFAULT_V13_COVARIATE_DRIFT_PROTOCOL = (
    ROOT / "config" / "q15_rti_v13_covariate_drift_protocol.json"
)
EXPECTED_V13_COVARIATE_DRIFT_PROTOCOL_SHA256 = (
    feature_v13.COVARIATE_DRIFT_PROTOCOL_SHA256
)
V13_GEOMETRY_REVIEW_WINDOWS = 30
V13_DRIFT_REVIEW_WINDOWS = 60


def _canonical_sha256(payload: Any) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def geometry_review_protocol_fingerprint(
    protocol: Mapping[str, Any],
) -> str:
    return _canonical_sha256(protocol)


def v12_geometry_review_protocol() -> dict[str, Any]:
    decoded = json.loads(
        DEFAULT_V12_GEOMETRY_REVIEW_PROTOCOL.read_text(encoding="utf-8")
    )
    if not isinstance(decoded, Mapping):
        raise ValueError("v12_geometry_review_protocol_root_not_object")
    return dict(decoded)


def v12_covariate_drift_protocol() -> dict[str, Any]:
    decoded = json.loads(
        DEFAULT_V12_COVARIATE_DRIFT_PROTOCOL.read_text(encoding="utf-8")
    )
    if not isinstance(decoded, Mapping):
        raise ValueError("v12_covariate_drift_protocol_root_not_object")
    return dict(decoded)


def v13_geometry_review_protocol() -> dict[str, Any]:
    decoded = json.loads(
        DEFAULT_V13_GEOMETRY_REVIEW_PROTOCOL.read_text(encoding="utf-8")
    )
    if not isinstance(decoded, Mapping):
        raise ValueError("v13_geometry_review_protocol_root_not_object")
    return dict(decoded)


def v13_covariate_drift_protocol() -> dict[str, Any]:
    decoded = json.loads(
        DEFAULT_V13_COVARIATE_DRIFT_PROTOCOL.read_text(encoding="utf-8")
    )
    if not isinstance(decoded, Mapping):
        raise ValueError("v13_covariate_drift_protocol_root_not_object")
    return dict(decoded)


def validate_v13_geometry_review_protocol(
    protocol: Mapping[str, Any],
    design: Mapping[str, Any],
) -> None:
    if _canonical_sha256(protocol) != (
        EXPECTED_V13_GEOMETRY_REVIEW_PROTOCOL_SHA256
    ):
        raise ValueError("v13_geometry_review_protocol_fingerprint_mismatch")
    if (
        protocol.get("protocol_id") != feature_v13.GEOMETRY_REVIEW_PROTOCOL_ID
        or protocol.get("protocol_status")
        != "PREREGISTERED_BEFORE_30_WINDOW_V13_FEATURE_STATISTICS"
    ):
        raise ValueError("v13_geometry_review_protocol_identity_mismatch")
    if (
        protocol.get("applies_to_design_id") != feature_v13.DESIGN_ID
        or protocol.get("applies_to_design_sha256") != feature_v13.DESIGN_SHA256
        or design.get("design_id") != feature_v13.DESIGN_ID
        or design_fingerprint(design) != feature_v13.DESIGN_SHA256
    ):
        raise ValueError("v13_geometry_review_design_binding_mismatch")
    evidence = protocol.get("evidence_available_at_preregistration")
    if not isinstance(evidence, Mapping) or (
        int(evidence.get("complete_executable_close_windows") or -1) != 1
        or evidence.get("feature_statistics_inspected") is not False
        or evidence.get("correlation_statistics_inspected") is not False
        or evidence.get("outcome_labels_read") is not False
        or evidence.get("model_fit_performed") is not False
        or evidence.get("performance_metrics_inspected") is not False
    ):
        raise ValueError("v13_geometry_review_outcome_blind_origin_invalid")
    trigger = protocol.get("review_trigger")
    if not isinstance(trigger, Mapping) or (
        int(trigger.get("complete_executable_close_windows") or 0)
        != V13_GEOMETRY_REVIEW_WINDOWS
        or trigger.get("same_seven_assets_required_per_close") is not True
        or trigger.get("partial_close_windows_forbidden") is not True
        or int(trigger.get("timestamp_alignment_failures_must_equal", -1)) != 0
        or int(trigger.get("nonfinite_feature_values_must_equal", -1)) != 0
    ):
        raise ValueError("v13_geometry_review_trigger_invalid")
    checks = protocol.get("fixed_checks")
    if not isinstance(checks, Mapping) or (
        float(checks.get("pairwise_absolute_correlation_ceiling") or 0.0)
        != CORRELATION_THRESHOLD
        or int(checks.get("exact_signed_duplicate_pairs_must_equal", -1)) != 0
        or float(checks.get("condition_number_nonzero_subspace_maximum") or 0.0)
        != 50.0
        or checks.get("btc_conditioned_feature_must_be_constant_zero") is not True
        or int(checks.get("btc_active_feature_count_maximum") or 0) != 19
        or int(checks.get(
            "btc_rank_deficiency_vs_active_features_must_equal", -1,
        )) != 0
        or int(checks.get(
            "non_btc_rank_deficiency_vs_active_features_must_equal", -1,
        )) != 0
        or float(checks.get(
            "btc_projected_train_rows_per_active_feature_minimum", 0.0,
        )) != 4.7
        or float(checks.get(
            "non_btc_projected_train_rows_per_active_feature_minimum", 0.0,
        )) != 10.0
    ):
        raise ValueError("v13_geometry_review_fixed_checks_invalid")
    conditioned = protocol.get("conditioned_feature")
    if not isinstance(conditioned, Mapping) or (
        conditioned.get("name") != feature_v13.COHORT_CONDITIONED_FEATURE
        or float(conditioned.get("expected_btc_value", math.nan)) != 0.0
    ):
        raise ValueError("v13_geometry_review_conditioned_feature_invalid")
    policy = protocol.get("failure_policy")
    if not isinstance(policy, Mapping) or (
        policy.get("any_failed_check_requires_manual_diagnosis") is not True
        or policy.get("new_successor_requires_new_preregistration") is not True
        or policy.get("v11_v12_and_v13_remain_frozen_parallel_controls")
        is not True
    ):
        raise ValueError("v13_geometry_review_failure_policy_invalid")
    for key in (
        "historical_credit_allowed",
        "automatic_design_creation_allowed",
        "automatic_feature_removal_allowed",
        "automatic_threshold_change_allowed",
        "automatic_refit_allowed",
        "automatic_activation_allowed",
        "automatic_promotion_allowed",
    ):
        if policy.get(key) is not False:
            raise ValueError("v13_geometry_review_automatic_action_guard_missing")
    for key, expected in (
        ("report_only", True),
        ("outcome_labels_forbidden", True),
        ("model_fit_forbidden", True),
        ("entry_policy_changes_forbidden", True),
        ("notification_eligible", False),
        ("real_trading_allowed", False),
    ):
        if protocol.get(key) is not expected:
            raise ValueError("v13_geometry_review_safety_guard_missing")


def validate_v13_covariate_drift_protocol(
    protocol: Mapping[str, Any],
    design: Mapping[str, Any],
) -> None:
    if _canonical_sha256(protocol) != (
        EXPECTED_V13_COVARIATE_DRIFT_PROTOCOL_SHA256
    ):
        raise ValueError("v13_covariate_drift_protocol_fingerprint_mismatch")
    if (
        protocol.get("protocol_id") != feature_v13.COVARIATE_DRIFT_PROTOCOL_ID
        or protocol.get("protocol_status")
        != "PREREGISTERED_BEFORE_60_WINDOW_V13_CHRONOLOGICAL_STATISTICS"
    ):
        raise ValueError("v13_covariate_drift_protocol_identity_mismatch")
    if (
        protocol.get("applies_to_design_id") != feature_v13.DESIGN_ID
        or protocol.get("applies_to_design_sha256") != feature_v13.DESIGN_SHA256
        or design.get("design_id") != feature_v13.DESIGN_ID
        or design_fingerprint(design) != feature_v13.DESIGN_SHA256
    ):
        raise ValueError("v13_covariate_drift_design_binding_mismatch")
    evidence = protocol.get("evidence_available_at_preregistration")
    if not isinstance(evidence, Mapping) or (
        int(evidence.get("complete_executable_close_windows") or -1) != 1
        or evidence.get("chronological_split_statistics_inspected") is not False
        or evidence.get("feature_statistics_inspected") is not False
        or evidence.get("outcome_labels_read") is not False
        or evidence.get("model_fit_performed") is not False
        or evidence.get("performance_metrics_inspected") is not False
        or evidence.get("source_v12_30_window_drift_breaches") != [
            "BTC:feature:log1p_realized_volatility_bps",
            "NON_BTC_TRANSFER:feature:log1p_realized_volatility_bps",
        ]
    ):
        raise ValueError("v13_covariate_drift_outcome_blind_origin_invalid")
    trigger = protocol.get("review_trigger")
    if not isinstance(trigger, Mapping) or (
        int(trigger.get("minimum_complete_executable_close_windows") or 0)
        != V13_DRIFT_REVIEW_WINDOWS
        or trigger.get("same_close_assets_must_share_half") is not True
        or trigger.get("chronological_halves") is not True
        or trigger.get("odd_middle_close_assigned_to_late_half") is not True
        or int(trigger.get("minimum_close_windows_per_half") or 0) != 30
        or trigger.get("partial_close_windows_forbidden") is not True
        or int(trigger.get("timestamp_alignment_failures_must_equal", -1)) != 0
        or int(trigger.get("nonfinite_feature_values_must_equal", -1)) != 0
    ):
        raise ValueError("v13_covariate_drift_trigger_invalid")
    metrics = protocol.get("fixed_metrics")
    if not isinstance(metrics, Mapping) or (
        float(metrics.get("absolute_standardized_mean_shift_maximum") or 0.0)
        != 1.0
        or float(metrics.get("dispersion_ratio_minimum") or 0.0) != 0.25
        or float(metrics.get("dispersion_ratio_maximum") or 0.0) != 4.0
        or float(metrics.get("both_halves_constant_dispersion_ratio") or 0.0)
        != 1.0
        or metrics.get("one_half_constant_other_active_is_breach") is not True
        or float(metrics.get(
            "missing_indicator_absolute_rate_shift_maximum", 0.0,
        )) != 0.25
        or metrics.get(
            "market_prior_is_audited_with_same_mean_and_dispersion_thresholds"
        ) is not True
        or metrics.get("constant_features_excluded_from_mean_shift_breaches")
        is not True
    ):
        raise ValueError("v13_covariate_drift_metrics_invalid")
    focus = protocol.get("mandatory_focus")
    if not isinstance(focus, Mapping) or (
        focus.get("feature") != "log1p_realized_volatility_bps"
        or focus.get("review_both_cohorts") is not True
        or focus.get("automatic_normalization_or_threshold_tuning_forbidden")
        is not True
        or focus.get("outcome_conditioned_interpretation_forbidden") is not True
    ):
        raise ValueError("v13_covariate_drift_focus_invalid")
    policy = protocol.get("breach_policy")
    if not isinstance(policy, Mapping) or (
        policy.get("any_feature_or_market_prior_breach_marks_drift_detected")
        is not True
        or policy.get("manual_root_cause_review_required") is not True
        or policy.get("v11_v12_and_v13_remain_frozen") is not True
    ):
        raise ValueError("v13_covariate_drift_breach_policy_invalid")
    for key in (
        "automatic_feature_removal_allowed",
        "automatic_threshold_change_allowed",
        "automatic_refit_allowed",
        "automatic_activation_allowed",
        "automatic_promotion_allowed",
        "historical_credit_allowed",
    ):
        if policy.get(key) is not False:
            raise ValueError("v13_covariate_drift_automatic_action_guard_missing")
    for key, expected in (
        ("report_only", True),
        ("outcome_labels_forbidden", True),
        ("model_fit_forbidden", True),
        ("notification_eligible", False),
        ("real_trading_allowed", False),
    ):
        if protocol.get(key) is not expected:
            raise ValueError("v13_covariate_drift_safety_guard_missing")


def validate_v12_covariate_drift_protocol(
    protocol: Mapping[str, Any],
    design: Mapping[str, Any],
) -> None:
    if _canonical_sha256(protocol) != (
        EXPECTED_V12_COVARIATE_DRIFT_PROTOCOL_SHA256
    ):
        raise ValueError("v12_covariate_drift_protocol_fingerprint_mismatch")
    if protocol.get("protocol_id") != (
        "q15-rti-v12-outcome-blind-covariate-drift-v1"
    ) or protocol.get("protocol_status") != (
        "PREREGISTERED_BEFORE_CHRONOLOGICAL_SPLIT_STATISTICS"
    ):
        raise ValueError("v12_covariate_drift_protocol_identity_mismatch")
    if (
        protocol.get("applies_to_design_id") != feature_v12.DESIGN_ID
        or protocol.get("applies_to_design_sha256") != feature_v12.DESIGN_SHA256
        or design.get("design_id") != feature_v12.DESIGN_ID
        or design_fingerprint(design) != feature_v12.DESIGN_SHA256
    ):
        raise ValueError("v12_covariate_drift_design_binding_mismatch")
    evidence = protocol.get("evidence_available_at_preregistration")
    if not isinstance(evidence, Mapping) or (
        int(evidence.get("aggregate_feature_audit_complete_windows") or -1)
        != 15
        or evidence.get("chronological_split_statistics_inspected") is not False
        or evidence.get("outcome_labels_read") is not False
        or evidence.get("model_fit_performed") is not False
        or evidence.get("performance_metrics_inspected") is not False
    ):
        raise ValueError("v12_covariate_drift_outcome_blind_origin_invalid")
    trigger = protocol.get("review_trigger")
    if not isinstance(trigger, Mapping) or (
        int(trigger.get("minimum_complete_executable_close_windows") or 0)
        != FIRST_REVIEW_WINDOWS
        or trigger.get("same_close_assets_must_share_half") is not True
        or trigger.get("chronological_halves") is not True
        or trigger.get("odd_middle_close_assigned_to_late_half") is not True
        or int(trigger.get("minimum_close_windows_per_half") or 0) != 15
        or trigger.get("partial_close_windows_forbidden") is not True
        or int(trigger.get("timestamp_alignment_failures_must_equal", -1)) != 0
        or int(trigger.get("nonfinite_feature_values_must_equal", -1)) != 0
    ):
        raise ValueError("v12_covariate_drift_trigger_invalid")
    metrics = protocol.get("fixed_metrics")
    if not isinstance(metrics, Mapping) or (
        float(metrics.get("absolute_standardized_mean_shift_maximum") or 0.0)
        != 1.0
        or float(metrics.get("dispersion_ratio_minimum") or 0.0) != 0.25
        or float(metrics.get("dispersion_ratio_maximum") or 0.0) != 4.0
        or float(metrics.get("both_halves_constant_dispersion_ratio") or 0.0)
        != 1.0
        or metrics.get("one_half_constant_other_active_is_breach") is not True
        or float(metrics.get(
            "missing_indicator_absolute_rate_shift_maximum", 0.0,
        )) != 0.25
        or metrics.get(
            "market_prior_is_audited_with_same_mean_and_dispersion_thresholds"
        ) is not True
        or metrics.get("constant_features_excluded_from_mean_shift_breaches")
        is not True
    ):
        raise ValueError("v12_covariate_drift_metrics_invalid")
    policy = protocol.get("breach_policy")
    if not isinstance(policy, Mapping) or (
        policy.get("any_feature_or_market_prior_breach_marks_drift_detected")
        is not True
        or policy.get("manual_root_cause_review_required") is not True
        or policy.get("v11_and_v12_remain_frozen") is not True
    ):
        raise ValueError("v12_covariate_drift_breach_policy_invalid")
    for key in (
        "automatic_feature_removal_allowed",
        "automatic_threshold_change_allowed",
        "automatic_refit_allowed",
        "automatic_activation_allowed",
        "automatic_promotion_allowed",
        "historical_credit_allowed",
    ):
        if policy.get(key) is not False:
            raise ValueError("v12_covariate_drift_automatic_action_guard_missing")
    for key, expected in (
        ("report_only", True),
        ("outcome_labels_forbidden", True),
        ("model_fit_forbidden", True),
        ("notification_eligible", False),
        ("real_trading_allowed", False),
    ):
        if protocol.get(key) is not expected:
            raise ValueError("v12_covariate_drift_safety_guard_missing")


def validate_v12_geometry_review_protocol(
    protocol: Mapping[str, Any],
    design: Mapping[str, Any],
) -> None:
    if geometry_review_protocol_fingerprint(protocol) != (
        EXPECTED_V12_GEOMETRY_REVIEW_PROTOCOL_SHA256
    ):
        raise ValueError("v12_geometry_review_protocol_fingerprint_mismatch")
    if protocol.get("protocol_id") != (
        "q15-rti-v12-outcome-blind-geometry-review-v1"
    ) or protocol.get("protocol_status") != (
        "PREREGISTERED_BEFORE_30_WINDOW_FEATURE_REVIEW"
    ):
        raise ValueError("v12_geometry_review_protocol_identity_mismatch")
    if (
        protocol.get("applies_to_design_id") != feature_v12.DESIGN_ID
        or protocol.get("applies_to_design_sha256") != feature_v12.DESIGN_SHA256
        or design.get("design_id") != feature_v12.DESIGN_ID
        or design_fingerprint(design) != feature_v12.DESIGN_SHA256
    ):
        raise ValueError("v12_geometry_review_design_binding_mismatch")
    evidence = protocol.get("evidence_available_at_preregistration")
    if not isinstance(evidence, Mapping) or (
        int(evidence.get("complete_executable_close_windows") or -1) != 15
        or evidence.get("outcome_labels_read") is not False
        or evidence.get("model_fit_performed") is not False
        or evidence.get("performance_metrics_inspected") is not False
    ):
        raise ValueError("v12_geometry_review_outcome_blind_origin_invalid")
    trigger = protocol.get("review_trigger")
    if not isinstance(trigger, Mapping) or (
        int(trigger.get("complete_executable_close_windows") or 0)
        != FIRST_REVIEW_WINDOWS
        or trigger.get("same_seven_assets_required_per_close") is not True
        or trigger.get("partial_close_windows_forbidden") is not True
        or int(trigger.get("timestamp_alignment_failures_must_equal", -1))
        != 0
        or int(trigger.get("nonfinite_feature_values_must_equal", -1)) != 0
    ):
        raise ValueError("v12_geometry_review_trigger_invalid")
    checks = protocol.get("fixed_checks")
    if not isinstance(checks, Mapping) or (
        float(checks.get("pairwise_absolute_correlation_ceiling") or 0.0)
        != CORRELATION_THRESHOLD
        or int(checks.get("exact_signed_duplicate_pairs_must_equal", -1))
        != 0
        or float(checks.get("condition_number_nonzero_subspace_maximum") or 0.0)
        != 50.0
        or checks.get("btc_numerical_rank_must_equal_centered_rank_limit")
        is not True
        or int(checks.get(
            "non_btc_rank_deficiency_vs_active_features_must_equal", -1,
        )) != 0
        or float(checks.get(
            "btc_projected_train_rows_per_active_feature_minimum", 0.0,
        )) != 4.0
        or float(checks.get(
            "non_btc_projected_train_rows_per_active_feature_minimum", 0.0,
        )) != 10.0
    ):
        raise ValueError("v12_geometry_review_fixed_checks_invalid")
    pair = protocol.get("predeclared_btc_alias_pair")
    if not isinstance(pair, Mapping) or (
        pair.get("left")
        != "target_minus_cross_asset_median_momentum_60s_bps"
        or pair.get("right")
        != "cross_asset_btc_minus_non_btc_median_60s"
    ):
        raise ValueError("v12_geometry_review_alias_pair_invalid")
    successor = protocol.get(
        "predeclared_successor_hypothesis_if_alias_persists"
    )
    if not isinstance(successor, Mapping) or (
        successor.get("replacement_feature_name")
        != "cross_asset_btc_minus_non_btc_median_non_btc_only_60s"
        or successor.get("new_design_required") is not True
        or successor.get("v11_and_v12_remain_frozen_parallel_controls")
        is not True
        or successor.get("historical_credit_allowed") is not False
        or successor.get("prospective_boundary_must_include_every_reviewed_close")
        is not True
        or successor.get("automatic_design_creation_allowed") is not False
        or successor.get("automatic_activation_allowed") is not False
        or successor.get("automatic_promotion_allowed") is not False
    ):
        raise ValueError("v12_geometry_review_successor_safety_invalid")
    for key, expected in (
        ("report_only", True),
        ("outcome_labels_forbidden", True),
        ("model_fit_forbidden", True),
        ("entry_policy_changes_forbidden", True),
        ("notification_eligible", False),
        ("real_trading_allowed", False),
    ):
        if protocol.get(key) is not expected:
            raise ValueError("v12_geometry_review_safety_guard_missing")


def _pair_correlation(
    cohort: Mapping[str, Any], left: str, right: str,
) -> float | None:
    pairs = cohort.get("correlation_diagnostics", {}).get(
        "high_absolute_correlation_pairs", []
    )
    for pair in pairs:
        if not isinstance(pair, Mapping):
            continue
        if {str(pair.get("left")), str(pair.get("right"))} == {left, right}:
            try:
                return float(pair["correlation"])
            except (KeyError, TypeError, ValueError):
                return None
    return None


def v12_geometry_review(
    report: Mapping[str, Any],
    design: Mapping[str, Any],
    protocol: Mapping[str, Any],
) -> dict[str, Any]:
    validate_v12_geometry_review_protocol(protocol, design)
    windows = int(report.get("complete_executable_close_windows") or 0)
    ready = windows >= FIRST_REVIEW_WINDOWS
    cohorts = report.get("cohorts", {})
    btc = cohorts.get("BTC", {}) if isinstance(cohorts, Mapping) else {}
    non_btc = (
        cohorts.get("NON_BTC_TRANSFER", {})
        if isinstance(cohorts, Mapping) else {}
    )
    pair = protocol["predeclared_btc_alias_pair"]
    alias_correlation = _pair_correlation(
        btc, str(pair["left"]), str(pair["right"]),
    )
    btc_corr = btc.get("correlation_diagnostics", {})
    non_btc_corr = non_btc.get("correlation_diagnostics", {})
    btc_geometry = btc.get("matrix_geometry", {})
    non_btc_geometry = non_btc.get("matrix_geometry", {})
    btc_capacity = btc.get("preregistered_fit_capacity", {})
    non_btc_capacity = non_btc.get("preregistered_fit_capacity", {})
    nonfinite_total = sum(
        int(value)
        for cohort in (btc, non_btc)
        for value in dict(cohort.get("nonfinite_counts", {})).values()
    )
    integrity_met = bool(
        int(report.get("timestamp_alignment_failures") or 0) == 0
        and nonfinite_total == 0
        and int(btc.get("independent_close_windows") or 0) == windows
        and int(non_btc.get("independent_close_windows") or 0) == windows
        and int(btc.get("rows") or 0) == windows
        and int(non_btc.get("rows") or 0) == windows * 6
    )
    checks = {
        "integrity_met": integrity_met,
        "btc_alias_pair_below_ceiling": bool(
            alias_correlation is None
            or abs(alias_correlation) < CORRELATION_THRESHOLD
        ),
        "btc_no_exact_signed_duplicates": len(
            btc_corr.get("exact_signed_duplicate_pairs", [])
        ) == 0,
        "non_btc_no_exact_signed_duplicates": len(
            non_btc_corr.get("exact_signed_duplicate_pairs", [])
        ) == 0,
        "btc_condition_number_within_limit": bool(
            btc_geometry.get("condition_number_nonzero_subspace") is not None
            and float(btc_geometry["condition_number_nonzero_subspace"]) <= 50.0
        ),
        "non_btc_condition_number_within_limit": bool(
            non_btc_geometry.get("condition_number_nonzero_subspace") is not None
            and float(non_btc_geometry["condition_number_nonzero_subspace"])
            <= 50.0
        ),
        "btc_rank_reaches_centered_limit": (
            btc_geometry.get("numerical_rank")
            == btc_geometry.get("centered_rank_limit")
        ),
        "non_btc_full_active_rank": (
            int(non_btc_geometry.get("rank_deficiency_vs_active_features") or 0)
            == 0
        ),
        "btc_capacity_minimum_met": bool(
            btc_capacity.get(
                "projected_train_rows_per_currently_active_feature"
            ) is not None
            and float(btc_capacity[
                "projected_train_rows_per_currently_active_feature"
            ]) >= 4.0
        ),
        "non_btc_capacity_minimum_met": bool(
            non_btc_capacity.get(
                "projected_train_rows_per_currently_active_feature"
            ) is not None
            and float(non_btc_capacity[
                "projected_train_rows_per_currently_active_feature"
            ]) >= 10.0
        ),
    }
    non_btc_high_pairs = len(
        non_btc_corr.get("high_absolute_correlation_pairs", [])
    )
    successor_triggered = bool(
        ready
        and integrity_met
        and alias_correlation is not None
        and abs(alias_correlation) >= CORRELATION_THRESHOLD
        and non_btc_high_pairs == 0
    )
    all_checks_met = all(checks.values())
    if not ready:
        status = "WAITING_FOR_30_COMPLETE_WINDOWS"
    elif not integrity_met:
        status = "FAIL_CLOSED_INTEGRITY"
    elif successor_triggered:
        status = "SUCCESSOR_HYPOTHESIS_TRIGGERED_MANUAL_PREREGISTRATION_REQUIRED"
    elif all_checks_met:
        status = "GEOMETRY_REVIEW_PASSED_NO_SUCCESSOR"
    else:
        status = "GEOMETRY_REVIEW_REQUIRES_MANUAL_DIAGNOSIS"
    report = {
        "protocol_id": protocol["protocol_id"],
        "protocol_sha256": geometry_review_protocol_fingerprint(protocol),
        "status": status,
        "review_ready": ready,
        "complete_executable_close_windows": windows,
        "windows_remaining": max(0, FIRST_REVIEW_WINDOWS - windows),
        "btc_alias_pair_correlation": alias_correlation,
        "non_btc_high_correlation_pair_count": non_btc_high_pairs,
        "checks": checks,
        "successor_hypothesis_triggered": successor_triggered,
        "successor_requires_new_frozen_design": successor_triggered,
        "historical_credit_allowed": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "automatic_design_change_allowed": False,
        "notification_eligible": False,
        "real_trading_allowed": False,
    }
    return report


def v13_geometry_review(
    report: Mapping[str, Any],
    design: Mapping[str, Any],
    protocol: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate only the predeclared V13 matrix checks without outcomes."""
    validate_v13_geometry_review_protocol(protocol, design)
    windows = int(report.get("complete_executable_close_windows") or 0)
    ready = windows >= V13_GEOMETRY_REVIEW_WINDOWS
    cohorts = report.get("cohorts", {})
    btc = cohorts.get("BTC", {}) if isinstance(cohorts, Mapping) else {}
    non_btc = (
        cohorts.get("NON_BTC_TRANSFER", {})
        if isinstance(cohorts, Mapping) else {}
    )
    btc_corr = btc.get("correlation_diagnostics", {})
    non_btc_corr = non_btc.get("correlation_diagnostics", {})
    btc_geometry = btc.get("matrix_geometry", {})
    non_btc_geometry = non_btc.get("matrix_geometry", {})
    btc_capacity = btc.get("preregistered_fit_capacity", {})
    non_btc_capacity = non_btc.get("preregistered_fit_capacity", {})
    conditioned = dict(btc.get("feature_statistics", {})).get(
        feature_v13.COHORT_CONDITIONED_FEATURE, {}
    )
    nonfinite_total = sum(
        int(value)
        for cohort in (btc, non_btc)
        for value in dict(cohort.get("nonfinite_counts", {})).values()
    )
    integrity_met = bool(
        int(report.get("timestamp_alignment_failures") or 0) == 0
        and nonfinite_total == 0
        and int(btc.get("independent_close_windows") or 0) == windows
        and int(non_btc.get("independent_close_windows") or 0) == windows
        and int(btc.get("rows") or 0) == windows
        and int(non_btc.get("rows") or 0) == windows * 6
    )
    checks_config = protocol["fixed_checks"]

    def _condition_within(cohort: Mapping[str, Any]) -> bool:
        value = cohort.get("condition_number_nonzero_subspace")
        return bool(
            value is not None
            and float(value) <= float(
                checks_config["condition_number_nonzero_subspace_maximum"]
            )
        )

    checks = {
        "integrity_met": integrity_met,
        "btc_conditioned_feature_constant_zero": bool(
            conditioned.get("constant") is True
            and float(conditioned.get("max_abs") or 0.0) == 0.0
        ),
        "btc_active_feature_count_within_maximum": bool(
            int(btc_geometry.get("active_feature_count") or 0)
            <= int(checks_config["btc_active_feature_count_maximum"])
        ),
        "btc_no_high_absolute_correlation_pairs": len(
            btc_corr.get("high_absolute_correlation_pairs", [])
        ) == 0,
        "non_btc_no_high_absolute_correlation_pairs": len(
            non_btc_corr.get("high_absolute_correlation_pairs", [])
        ) == 0,
        "btc_no_exact_signed_duplicates": len(
            btc_corr.get("exact_signed_duplicate_pairs", [])
        ) == 0,
        "non_btc_no_exact_signed_duplicates": len(
            non_btc_corr.get("exact_signed_duplicate_pairs", [])
        ) == 0,
        "btc_condition_number_within_limit": _condition_within(btc_geometry),
        "non_btc_condition_number_within_limit": _condition_within(
            non_btc_geometry
        ),
        "btc_full_active_rank": int(
            btc_geometry.get("rank_deficiency_vs_active_features") or 0
        ) == 0,
        "non_btc_full_active_rank": int(
            non_btc_geometry.get("rank_deficiency_vs_active_features") or 0
        ) == 0,
        "btc_capacity_minimum_met": bool(
            btc_capacity.get(
                "projected_train_rows_per_currently_active_feature"
            ) is not None
            and float(btc_capacity[
                "projected_train_rows_per_currently_active_feature"
            ]) >= float(checks_config[
                "btc_projected_train_rows_per_active_feature_minimum"
            ])
        ),
        "non_btc_capacity_minimum_met": bool(
            non_btc_capacity.get(
                "projected_train_rows_per_currently_active_feature"
            ) is not None
            and float(non_btc_capacity[
                "projected_train_rows_per_currently_active_feature"
            ]) >= float(checks_config[
                "non_btc_projected_train_rows_per_active_feature_minimum"
            ])
        ),
    }
    all_checks_met = all(checks.values())
    if not ready:
        status = "WAITING_FOR_30_COMPLETE_WINDOWS"
    elif not integrity_met:
        status = "FAIL_CLOSED_INTEGRITY"
    elif all_checks_met:
        status = "V13_GEOMETRY_REVIEW_PASSED"
    else:
        status = "V13_GEOMETRY_REVIEW_REQUIRES_MANUAL_DIAGNOSIS"
    return {
        "protocol_id": protocol["protocol_id"],
        "protocol_sha256": _canonical_sha256(protocol),
        "status": status,
        "review_ready": ready,
        "complete_executable_close_windows": windows,
        "windows_remaining": max(0, V13_GEOMETRY_REVIEW_WINDOWS - windows),
        "checks": checks,
        "all_checks_met": bool(ready and all_checks_met),
        "btc_conditioned_feature_statistics": conditioned,
        "historical_credit_allowed": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "automatic_design_change_allowed": False,
        "automatic_feature_removal_allowed": False,
        "automatic_threshold_change_allowed": False,
        "notification_eligible": False,
        "real_trading_allowed": False,
    }


def _drift_variable_metrics(
    early: Sequence[float],
    late: Sequence[float],
    *,
    minimum_std: float,
    missing_indicator: bool = False,
) -> dict[str, Any]:
    early_values = [float(value) for value in early]
    late_values = [float(value) for value in late]
    all_values = [*early_values, *late_values]
    if not early_values or not late_values or not all(
        math.isfinite(value) for value in all_values
    ):
        return {
            "available": False,
            "mean_shift_breach": True,
            "dispersion_breach": True,
            "missing_rate_breach": bool(missing_indicator),
            "any_breach": True,
        }
    early_mean = _mean(early_values)
    late_mean = _mean(late_values)
    overall_std = _population_std(all_values)
    early_std = _population_std(early_values, early_mean)
    late_std = _population_std(late_values, late_mean)
    active = overall_std > minimum_std
    standardized_mean_shift = (
        None
        if not active
        else (late_mean - early_mean) / max(overall_std, minimum_std)
    )
    early_constant = early_std <= minimum_std
    late_constant = late_std <= minimum_std
    one_half_constant = early_constant != late_constant
    if early_constant and late_constant:
        dispersion_ratio = 1.0
    elif one_half_constant:
        dispersion_ratio = None
    else:
        dispersion_ratio = late_std / early_std
    mean_shift_breach = bool(
        standardized_mean_shift is not None
        and abs(standardized_mean_shift) > 1.0
    )
    dispersion_breach = bool(
        one_half_constant
        or dispersion_ratio is None
        or dispersion_ratio < 0.25
        or dispersion_ratio > 4.0
    )
    missing_rate_shift = (
        abs(late_mean - early_mean) if missing_indicator else None
    )
    missing_rate_breach = bool(
        missing_indicator
        and missing_rate_shift is not None
        and missing_rate_shift > 0.25
    )
    return {
        "available": True,
        "active_overall": active,
        "early_n": len(early_values),
        "late_n": len(late_values),
        "early_mean": early_mean,
        "late_mean": late_mean,
        "overall_population_std": overall_std,
        "early_population_std": early_std,
        "late_population_std": late_std,
        "standardized_mean_shift": standardized_mean_shift,
        "dispersion_ratio": dispersion_ratio,
        "one_half_constant_other_active": one_half_constant,
        "missing_indicator": missing_indicator,
        "missing_rate_shift": missing_rate_shift,
        "mean_shift_breach": mean_shift_breach,
        "dispersion_breach": dispersion_breach,
        "missing_rate_breach": missing_rate_breach,
        "any_breach": bool(
            mean_shift_breach or dispersion_breach or missing_rate_breach
        ),
    }


def _covariate_drift_review(
    examples: Sequence[Mapping[str, Any]],
    feature_names: Sequence[str],
    design: Mapping[str, Any],
    protocol: Mapping[str, Any],
    *,
    timestamp_alignment_failures: int,
    validate_protocol: Any,
    review_windows: int,
    minimum_half_windows: int,
    non_btc_assets: Sequence[str],
    waiting_status: str,
) -> dict[str, Any]:
    validate_protocol(protocol, design)
    close_times = sorted({float(row["close_time"]) for row in examples})
    split_index = len(close_times) // 2
    early_closes = set(close_times[:split_index])
    late_closes = set(close_times[split_index:])
    ready = len(close_times) >= review_windows
    cohort_reports: dict[str, Any] = {}
    nonfinite_total = 0
    observed_breaches: list[str] = []
    expected_assets = {
        "BTC": {"BTC"},
        "NON_BTC_TRANSFER": set(non_btc_assets),
    }
    geometry_valid = True
    for cohort, assets in expected_assets.items():
        cohort_rows = [
            row for row in examples
            if str(row.get("asset") or "").upper() in assets
        ]
        early_rows = [
            row for row in cohort_rows
            if float(row["close_time"]) in early_closes
        ]
        late_rows = [
            row for row in cohort_rows
            if float(row["close_time"]) in late_closes
        ]
        expected_per_close = len(assets)
        if (
            len(early_rows) != len(early_closes) * expected_per_close
            or len(late_rows) != len(late_closes) * expected_per_close
        ):
            geometry_valid = False
        variables: dict[str, Any] = {}
        for index, name in enumerate(feature_names):
            early_values = [float(row["features"][index]) for row in early_rows]
            late_values = [float(row["features"][index]) for row in late_rows]
            nonfinite_total += sum(
                not math.isfinite(value)
                for value in (*early_values, *late_values)
            )
            metrics = _drift_variable_metrics(
                early_values,
                late_values,
                minimum_std=float(
                    design["fixed_training_config"]["standardization_min_std"]
                ),
                missing_indicator=str(name).endswith("_missing"),
            )
            variables[str(name)] = metrics
            if metrics["any_breach"]:
                observed_breaches.append(f"{cohort}:feature:{name}")
        market = _drift_variable_metrics(
            [float(row["market_yes_probability"]) for row in early_rows],
            [float(row["market_yes_probability"]) for row in late_rows],
            minimum_std=float(
                design["fixed_training_config"]["standardization_min_std"]
            ),
        )
        if market["any_breach"]:
            observed_breaches.append(f"{cohort}:market_prior")
        cohort_reports[cohort] = {
            "early_rows": len(early_rows),
            "late_rows": len(late_rows),
            "early_close_windows": len(early_closes),
            "late_close_windows": len(late_closes),
            "feature_metrics": variables,
            "market_prior_metrics": market,
            "breach_count": sum(
                metrics["any_breach"] for metrics in variables.values()
            ) + int(market["any_breach"]),
        }
    integrity_met = bool(
        timestamp_alignment_failures == 0
        and nonfinite_total == 0
        and geometry_valid
        and (not ready or (
            len(early_closes) >= minimum_half_windows
            and len(late_closes) >= minimum_half_windows
        ))
    )
    drift_detected = bool(ready and integrity_met and observed_breaches)
    if not ready:
        status = waiting_status
    elif not integrity_met:
        status = "FAIL_CLOSED_INTEGRITY"
    elif drift_detected:
        status = "COVARIATE_DRIFT_DETECTED_MANUAL_REVIEW_REQUIRED"
    else:
        status = "COVARIATE_STABILITY_REVIEW_PASSED"
    return {
        "protocol_id": protocol["protocol_id"],
        "protocol_sha256": _canonical_sha256(protocol),
        "status": status,
        "review_ready": ready,
        "complete_executable_close_windows": len(close_times),
        "windows_remaining": max(0, review_windows - len(close_times)),
        "early_first_close_time": (
            None if not early_closes else min(early_closes)
        ),
        "early_last_close_time": (
            None if not early_closes else max(early_closes)
        ),
        "late_first_close_time": None if not late_closes else min(late_closes),
        "late_last_close_time": None if not late_closes else max(late_closes),
        "same_close_assets_share_half": True,
        "integrity_met": integrity_met,
        "nonfinite_feature_values": nonfinite_total,
        "observed_breaches": sorted(observed_breaches),
        "preview_breach_count": len(observed_breaches),
        "drift_detected": drift_detected,
        "cohorts": cohort_reports,
        "report_only": True,
        "historical_credit_allowed": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "automatic_feature_removal_allowed": False,
        "automatic_threshold_change_allowed": False,
        "automatic_refit_allowed": False,
        "automatic_activation_allowed": False,
        "automatic_promotion_allowed": False,
        "notification_eligible": False,
        "real_trading_allowed": False,
    }


def v12_covariate_drift_review(
    examples: Sequence[Mapping[str, Any]],
    feature_names: Sequence[str],
    design: Mapping[str, Any],
    protocol: Mapping[str, Any],
    *,
    timestamp_alignment_failures: int,
) -> dict[str, Any]:
    return _covariate_drift_review(
        examples,
        feature_names,
        design,
        protocol,
        timestamp_alignment_failures=timestamp_alignment_failures,
        validate_protocol=validate_v12_covariate_drift_protocol,
        review_windows=FIRST_REVIEW_WINDOWS,
        minimum_half_windows=15,
        non_btc_assets=tuple(feature_v12.NON_BTC_ASSETS),
        waiting_status="WAITING_FOR_30_COMPLETE_WINDOWS",
    )


def v13_covariate_drift_review(
    examples: Sequence[Mapping[str, Any]],
    feature_names: Sequence[str],
    design: Mapping[str, Any],
    protocol: Mapping[str, Any],
    *,
    timestamp_alignment_failures: int,
) -> dict[str, Any]:
    return _covariate_drift_review(
        examples,
        feature_names,
        design,
        protocol,
        timestamp_alignment_failures=timestamp_alignment_failures,
        validate_protocol=validate_v13_covariate_drift_protocol,
        review_windows=V13_DRIFT_REVIEW_WINDOWS,
        minimum_half_windows=30,
        non_btc_assets=tuple(feature_v13.NON_BTC_ASSETS),
        waiting_status="WAITING_FOR_60_COMPLETE_WINDOWS",
    )


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _population_std(values: Sequence[float], mean: float | None = None) -> float:
    if not values:
        return math.nan
    center = _mean(values) if mean is None else float(mean)
    return math.sqrt(sum((value - center) ** 2 for value in values) / len(values))


def feature_statistics(
    feature_names: Sequence[str],
    matrix: Sequence[Sequence[float]],
    *,
    minimum_std: float,
) -> dict[str, dict[str, Any]]:
    """Return deterministic per-column statistics without using labels."""
    width = len(feature_names)
    if any(len(row) != width for row in matrix):
        raise ValueError("feature_matrix_width_mismatch")
    output: dict[str, dict[str, Any]] = {}
    for index, name in enumerate(feature_names):
        raw = [float(row[index]) for row in matrix]
        finite = [value for value in raw if math.isfinite(value)]
        nonfinite = len(raw) - len(finite)
        if not finite:
            output[str(name)] = {
                "n": len(raw),
                "finite_n": 0,
                "nonfinite": nonfinite,
                "mean": None,
                "population_std": None,
                "min": None,
                "max": None,
                "max_abs": None,
                "unique_6dp": 0,
                "constant": False,
                "near_zero_variance": False,
                "tiny_nonzero_variance": False,
            }
            continue
        mean = _mean(finite)
        std = _population_std(finite, mean)
        output[str(name)] = {
            "n": len(raw),
            "finite_n": len(finite),
            "nonfinite": nonfinite,
            "mean": mean,
            "population_std": std,
            "min": min(finite),
            "max": max(finite),
            "max_abs": max(abs(value) for value in finite),
            "unique_6dp": len({round(value, 6) for value in finite}),
            "constant": std == 0.0,
            "near_zero_variance": std <= minimum_std,
            "tiny_nonzero_variance": 0.0 < std <= minimum_std,
        }
    return output


def correlation_diagnostics(
    feature_names: Sequence[str],
    matrix: Sequence[Sequence[float]],
    statistics: Mapping[str, Mapping[str, Any]],
    *,
    minimum_std: float,
    threshold: float = CORRELATION_THRESHOLD,
) -> dict[str, Any]:
    """Find high correlations and exact signed duplicates among active columns."""
    width = len(feature_names)
    if any(len(row) != width for row in matrix):
        raise ValueError("feature_matrix_width_mismatch")
    pairs: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    if not matrix:
        return {
            "threshold": threshold,
            "high_absolute_correlation_pairs": [],
            "exact_signed_duplicate_pairs": [],
            "max_absolute_correlation": None,
        }
    for left in range(width):
        left_name = str(feature_names[left])
        left_std = statistics[left_name].get("population_std")
        if left_std is None or float(left_std) <= minimum_std:
            continue
        left_values = [float(row[left]) for row in matrix]
        left_mean = float(statistics[left_name]["mean"])
        for right in range(left + 1, width):
            right_name = str(feature_names[right])
            right_std = statistics[right_name].get("population_std")
            if right_std is None or float(right_std) <= minimum_std:
                continue
            right_values = [float(row[right]) for row in matrix]
            if not all(
                math.isfinite(a) and math.isfinite(b)
                for a, b in zip(left_values, right_values)
            ):
                continue
            right_mean = float(statistics[right_name]["mean"])
            covariance = sum(
                (a - left_mean) * (b - right_mean)
                for a, b in zip(left_values, right_values)
            ) / len(left_values)
            correlation = covariance / (float(left_std) * float(right_std))
            correlation = max(-1.0, min(1.0, correlation))
            if abs(correlation) >= threshold:
                pairs.append({
                    "left": left_name,
                    "right": right_name,
                    "correlation": correlation,
                })
            same = all(
                abs(a - b) <= EXACT_DUPLICATE_TOLERANCE
                for a, b in zip(left_values, right_values)
            )
            opposite = all(
                abs(a + b) <= EXACT_DUPLICATE_TOLERANCE
                for a, b in zip(left_values, right_values)
            )
            if same or opposite:
                duplicates.append({
                    "left": left_name,
                    "right": right_name,
                    "relationship": "same" if same else "opposite",
                })
    pairs.sort(key=lambda item: (
        -abs(float(item["correlation"])), item["left"], item["right"]
    ))
    duplicates.sort(key=lambda item: (item["left"], item["right"]))
    return {
        "threshold": threshold,
        "high_absolute_correlation_pairs": pairs,
        "exact_signed_duplicate_pairs": duplicates,
        "max_absolute_correlation": (
            None if not pairs else abs(float(pairs[0]["correlation"]))
        ),
    }


def matrix_geometry_diagnostics(
    feature_names: Sequence[str],
    matrix: Sequence[Sequence[float]],
    statistics: Mapping[str, Mapping[str, Any]],
    *,
    minimum_std: float,
) -> dict[str, Any]:
    """Measure joint feature dimensionality without consulting outcomes.

    Pairwise correlation cannot detect a feature that is a linear combination
    of several other columns.  The centered, standardized singular spectrum
    exposes that failure mode and quantifies how much independent geometry the
    eventual regularized fit actually has.
    """
    width = len(feature_names)
    if any(len(row) != width for row in matrix):
        raise ValueError("feature_matrix_width_mismatch")
    active_indexes = [
        index
        for index, name in enumerate(feature_names)
        if statistics[str(name)].get("population_std") is not None
        and float(statistics[str(name)]["population_std"]) > minimum_std
    ]
    active_names = [str(feature_names[index]) for index in active_indexes]
    rows = len(matrix)
    base = {
        "rows": rows,
        "total_feature_count": width,
        "active_feature_count": len(active_indexes),
        "inactive_feature_count": width - len(active_indexes),
        "active_feature_names": active_names,
        "rows_per_active_feature": (
            None if not active_indexes else rows / len(active_indexes)
        ),
        "centered_rank_limit": min(max(0, rows - 1), len(active_indexes)),
        "numerical_rank": 0,
        "rank_deficiency_vs_active_features": len(active_indexes),
        "stable_rank": None,
        "condition_number_nonzero_subspace": None,
        "largest_singular_value": None,
        "smallest_nonzero_singular_value": None,
        "singular_value_tolerance": None,
        "singular_values": [],
        "finite": True,
    }
    if not matrix or not active_indexes:
        return base
    values = np.asarray(matrix, dtype=np.float64)[:, active_indexes]
    if not bool(np.isfinite(values).all()):
        return {**base, "finite": False}
    means = np.asarray([
        float(statistics[str(feature_names[index])]["mean"])
        for index in active_indexes
    ])
    stds = np.asarray([
        float(statistics[str(feature_names[index])]["population_std"])
        for index in active_indexes
    ])
    standardized = (values - means) / stds
    # Remove residual floating-point centering error so the mathematical
    # ``rows - 1`` rank ceiling is respected even for very small previews.
    standardized -= standardized.mean(axis=0, keepdims=True)
    singular = np.linalg.svd(standardized, compute_uv=False)
    largest = float(singular[0]) if singular.size else 0.0
    tolerance = (
        max(standardized.shape) * np.finfo(np.float64).eps * largest
    )
    nonzero = singular[singular > tolerance]
    rank = int(nonzero.size)
    stable_rank = (
        None
        if largest <= 0.0
        else float(np.square(singular).sum() / (largest * largest))
    )
    return {
        **base,
        "numerical_rank": rank,
        "rank_deficiency_vs_active_features": len(active_indexes) - rank,
        "stable_rank": stable_rank,
        "condition_number_nonzero_subspace": (
            None if not rank else float(largest / float(nonzero[-1]))
        ),
        "largest_singular_value": None if not rank else largest,
        "smallest_nonzero_singular_value": (
            None if not rank else float(nonzero[-1])
        ),
        "singular_value_tolerance": float(tolerance),
        "singular_values": [float(value) for value in singular],
    }


def _cohort_summary(
    examples: Sequence[Mapping[str, Any]],
    *,
    feature_names: Sequence[str],
    minimum_std: float,
    minimum_complete_windows: int,
    train_fraction: float,
) -> dict[str, Any]:
    matrix = [list(map(float, row["features"])) for row in examples]
    statistics = feature_statistics(
        feature_names, matrix, minimum_std=minimum_std,
    )
    nonfinite = {
        name: int(values["nonfinite"])
        for name, values in statistics.items()
        if int(values["nonfinite"]) > 0
    }
    constants = [
        name for name, values in statistics.items() if values["constant"]
    ]
    near_zero = [
        name for name, values in statistics.items()
        if values["near_zero_variance"]
    ]
    tiny_nonzero = [
        name for name, values in statistics.items()
        if values["tiny_nonzero_variance"]
    ]
    market = [float(row["market_yes_probability"]) for row in examples]
    close_times = sorted({float(row["close_time"]) for row in examples})
    correlations = correlation_diagnostics(
        feature_names,
        matrix,
        statistics,
        minimum_std=minimum_std,
    )
    geometry = matrix_geometry_diagnostics(
        feature_names,
        matrix,
        statistics,
        minimum_std=minimum_std,
    )
    missing_rates = {}
    for name in ("kalshi_microstructure_missing", "spot_flow_missing"):
        if name not in feature_names:
            continue
        values = [float(row["features"][feature_names.index(name)]) for row in examples]
        missing_rates[name] = None if not values else sum(values) / len(values)
    rows_per_window = (
        None if not close_times else len(examples) / len(close_times)
    )
    projected_train_windows = int(minimum_complete_windows * train_fraction)
    projected_train_rows = (
        None
        if rows_per_window is None
        else int(round(projected_train_windows * rows_per_window))
    )
    active_count = int(geometry["active_feature_count"])
    return {
        "rows": len(examples),
        "independent_close_windows": len(close_times),
        "first_close_time": None if not close_times else close_times[0],
        "last_close_time": None if not close_times else close_times[-1],
        "nonfinite_counts": nonfinite,
        "constant_features": constants,
        "near_zero_variance_features": near_zero,
        "tiny_nonzero_variance_features": tiny_nonzero,
        "market_prior": {
            "mean": None if not market else _mean(market),
            "population_std": None if not market else _population_std(market),
            "min": None if not market else min(market),
            "max": None if not market else max(market),
        },
        "missing_indicator_rates": missing_rates,
        "feature_statistics": statistics,
        "correlation_diagnostics": correlations,
        "matrix_geometry": geometry,
        "preregistered_fit_capacity": {
            "minimum_complete_windows": minimum_complete_windows,
            "train_fraction": train_fraction,
            "projected_train_windows": projected_train_windows,
            "observed_rows_per_window": rows_per_window,
            "projected_train_rows": projected_train_rows,
            "projected_train_rows_per_currently_active_feature": (
                None
                if projected_train_rows is None or active_count <= 0
                else projected_train_rows / active_count
            ),
            "projected_train_rows_exceed_currently_active_features": (
                None
                if projected_train_rows is None
                else projected_train_rows > active_count
            ),
        },
    }


def soft_input_integrity(
    examples: Sequence[Mapping[str, Any]],
    feature_names: Sequence[str],
) -> dict[str, Any]:
    """Expose neutralized retained inputs without changing model eligibility.

    Frozen feature builders deliberately encode some unavailable evidence as a
    neutral value plus an explicit ``*_missing`` indicator.  That is honest
    model input, but it is not the same thing as a fully observed row.  Keep
    the distinction visible at row and independent-close-window granularity so
    a readiness count can never be mistaken for perfect source coverage.
    """
    names = tuple(str(name) for name in feature_names)
    missing_indexes = tuple(
        index
        for index, name in enumerate(names)
        if name.endswith("_missing") or "_missing_" in name
    )
    all_windows: set[float] = set()
    degraded_windows: set[float] = set()
    degraded_rows: list[dict[str, Any]] = []
    degraded_by_asset: dict[str, int] = {}
    degraded_by_reason: dict[str, int] = {}
    for example in examples:
        close = float(example["close_time"])
        asset = str(example.get("asset") or "UNKNOWN").upper()
        values = tuple(float(value) for value in example["features"])
        if len(values) != len(names):
            raise ValueError("soft_integrity_feature_width_mismatch")
        all_windows.add(close)
        reasons = [
            names[index]
            for index in missing_indexes
            if values[index] >= 0.5
        ]
        if not reasons:
            continue
        degraded_windows.add(close)
        degraded_by_asset[asset] = degraded_by_asset.get(asset, 0) + 1
        for reason in reasons:
            degraded_by_reason[reason] = degraded_by_reason.get(reason, 0) + 1
        degraded_rows.append({
            "id": int(example["id"]),
            "asset": asset,
            "close_time": close,
            "neutralized_input_indicators": reasons,
        })
    fully_observed_windows = all_windows - degraded_windows
    return {
        "status": (
            "SOFT_DEGRADATION_PRESENT"
            if degraded_rows else "ALL_RETAINED_INPUTS_OBSERVED"
        ),
        "scope": "executable_model_rows_only",
        "rows": len(examples),
        "independent_close_windows": len(all_windows),
        "fully_observed_rows": len(examples) - len(degraded_rows),
        "soft_degraded_rows": len(degraded_rows),
        "fully_observed_close_windows": len(fully_observed_windows),
        "soft_degraded_close_windows": len(degraded_windows),
        "retained_missing_indicator_features": [
            names[index] for index in missing_indexes
        ],
        "degraded_by_asset": dict(sorted(degraded_by_asset.items())),
        "degraded_by_reason": dict(sorted(degraded_by_reason.items())),
        "degraded_row_details": degraded_rows,
        "diagnostic_only": True,
        "changes_frozen_feature_values": False,
        "changes_executable_window_eligibility": False,
        "changes_readiness_credit": False,
        "outcome_labels_read": False,
    }


def decision_timing_integrity(
    examples: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Summarize exact-capture and evidence-assembly timing for usable rows."""
    offsets: list[float] = []
    assembly_lags: list[float] = []
    violations: list[dict[str, Any]] = []
    for example in examples:
        close = float(example["close_time"])
        source = float(example["source_captured_at"])
        evidence = float(example["evidence_as_of"])
        offset = source - (close - 780.0)
        assembly_lag = evidence - source
        offsets.append(offset)
        assembly_lags.append(assembly_lag)
        reasons = []
        if not 0.0 <= offset <= 2.0:
            reasons.append("NOT_EXACT_13M")
        if assembly_lag < -1e-6:
            reasons.append("EVIDENCE_PRECEDES_SOURCE")
        if reasons:
            violations.append({
                "id": int(example["id"]),
                "asset": str(example.get("asset") or "UNKNOWN").upper(),
                "close_time": close,
                "exact_capture_offset_seconds": offset,
                "evidence_assembly_lag_seconds": assembly_lag,
                "reasons": reasons,
            })
    return {
        "status": "OK" if not violations else "FAIL_CLOSED",
        "rows": len(examples),
        "exact_capture_offset_seconds": {
            "minimum": None if not offsets else min(offsets),
            "maximum": None if not offsets else max(offsets),
        },
        "evidence_assembly_lag_seconds": {
            "minimum": None if not assembly_lags else min(assembly_lags),
            "maximum": None if not assembly_lags else max(assembly_lags),
        },
        "violations": violations,
        "outcome_labels_read": False,
    }


def build_feature_audit(
    feature_rows: Sequence[Mapping[str, Any]],
    design: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the feature-only preview using executable seven-asset folds."""
    validate_design(design)
    feature_rows = sanitize_feature_rows(feature_rows)
    design_id = str(design.get("design_id") or "")
    if design_id == feature_v1.DESIGN_ID:
        runtime = feature_v1
    elif design_id == feature_v2.DESIGN_ID:
        runtime = feature_v2
    elif design_id == feature_v3.DESIGN_ID:
        runtime = feature_v3
    elif design_id == feature_v4.DESIGN_ID:
        runtime = feature_v4
    elif design_id == feature_v5.DESIGN_ID:
        runtime = feature_v5
    elif design_id == feature_v6.DESIGN_ID:
        runtime = feature_v6
    elif design_id == feature_v7.DESIGN_ID:
        runtime = feature_v7
    elif design_id == feature_v8.DESIGN_ID:
        runtime = feature_v8
    elif design_id == feature_v9.DESIGN_ID:
        runtime = feature_v9
    elif design_id == feature_v10.DESIGN_ID:
        runtime = feature_v10
    elif design_id == feature_v11.DESIGN_ID:
        runtime = feature_v11
    elif design_id == feature_v12.DESIGN_ID:
        runtime = feature_v12
    elif design_id == feature_v13.DESIGN_ID:
        runtime = feature_v13
    elif design_id == feature_v14.DESIGN_ID:
        runtime = feature_v14
    else:
        raise ValueError("unsupported_design_id")
    feature_names = tuple(runtime.FEATURE_NAMES)
    if tuple(design.get("feature_names") or ()) != feature_names:
        raise ValueError("design_runtime_feature_names_mismatch")
    coverage = build_coverage(
        feature_rows, source_schema=str(design.get("source_schema") or ""),
    )
    model_coverage = runtime.model_feature_window_coverage(tuple(feature_rows))
    coverage.update(model_coverage)
    readiness = build_readiness(design, coverage)
    complete_times = {
        float(value) for value in model_coverage["model_feature_complete_close_times"]
    }
    examples: list[dict[str, Any]] = []
    for row in feature_rows:
        close = row.get("close_time")
        try:
            close_time = float(close)
        except (TypeError, ValueError):
            continue
        if close_time not in complete_times:
            continue
        vector = runtime.feature_vector(row)
        if not vector.get("available"):
            raise ValueError("complete_window_contains_unavailable_feature")
        values = [float(value) for value in vector["features"]]
        examples.append({
            "id": int(row["id"]),
            "asset": str(row.get("asset") or "").upper(),
            "close_time": close_time,
            "features": values,
            "market_yes_probability": float(vector["market_yes_probability"]),
            "source_captured_at": float(row["source_captured_at"]),
            "evidence_as_of": float(row["evidence_as_of"]),
        })
    minimum_std = float(design["fixed_training_config"]["standardization_min_std"])
    cohorts = {
        "BTC": [row for row in examples if row["asset"] == "BTC"],
        "NON_BTC_TRANSFER": [row for row in examples if row["asset"] != "BTC"],
    }
    train_fraction = float(design["chronology"]["train_fraction"])
    summaries = {}
    for name, rows in cohorts.items():
        summaries[name] = _cohort_summary(
            rows,
            feature_names=feature_names,
            minimum_std=minimum_std,
            minimum_complete_windows=int(
                design["cohorts"][name]["minimum_complete_close_windows"]
            ),
            train_fraction=train_fraction,
        )
    nonfinite_total = sum(
        sum(summary["nonfinite_counts"].values())
        for summary in summaries.values()
    )
    complete_windows = len(complete_times)
    if model_coverage["model_feature_timestamp_failures"] or nonfinite_total:
        status = "FAIL_CLOSED_NUMERICAL_OR_TIMESTAMP_INTEGRITY"
    elif complete_windows < FIRST_REVIEW_WINDOWS:
        status = "PREVIEW_ONLY_BEFORE_30_WINDOWS"
    else:
        status = "OUTCOME_BLIND_FIRST_FEATURE_REVIEW_READY"
    report = {
        "audit_version": AUDIT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "design_id": str(design["design_id"]),
        "design_sha256": design_fingerprint(design),
        "feature_schema_version": str(design["feature_schema_version"]),
        "feature_count": len(feature_names),
        "status": status,
        "preview_only": complete_windows < FIRST_REVIEW_WINDOWS,
        "first_review_complete_windows": FIRST_REVIEW_WINDOWS,
        "windows_remaining_to_first_review": max(
            0, FIRST_REVIEW_WINDOWS - complete_windows
        ),
        "complete_executable_close_windows": complete_windows,
        "schema_complete_close_windows": model_coverage[
            "schema_complete_model_candidate_close_windows"
        ],
        "unusable_close_windows": len(model_coverage[
            "unusable_model_feature_close_windows"
        ]),
        "feature_unavailable_rows": len(model_coverage[
            "model_feature_unavailable_rows"
        ]),
        "timestamp_alignment_failures": len(model_coverage[
            "model_feature_timestamp_failures"
        ]),
        "standardization_min_std": minimum_std,
        "outcome_labels_read": False,
        "feature_profile_allow_list_enforced": True,
        "raw_threshold_json_selected_by_cli_loader": False,
        "allowed_feature_profile_key_count": len(SAFE_FEATURE_PROFILE_KEYS),
        "model_fit_performed": False,
        "artifact_emitted": False,
        "notification_eligible": False,
        "real_trading_allowed": False,
        "automatic_design_change_allowed": False,
        "automatic_refit": False,
        "automatic_promotion": False,
        "readiness": readiness,
        "cohorts": summaries,
        "soft_input_integrity": soft_input_integrity(
            examples, feature_names,
        ),
        "decision_timing_integrity": decision_timing_integrity(examples),
    }
    if design_id == feature_v12.DESIGN_ID:
        protocol = v12_geometry_review_protocol()
        review = v12_geometry_review(report, design, protocol)
        report["geometry_review_protocol_id"] = review["protocol_id"]
        report["geometry_review_protocol_sha256"] = review["protocol_sha256"]
        report["geometry_review"] = review
        drift_protocol = v12_covariate_drift_protocol()
        drift_review = v12_covariate_drift_review(
            examples,
            feature_names,
            design,
            drift_protocol,
            timestamp_alignment_failures=int(
                report["timestamp_alignment_failures"]
            ),
        )
        report["covariate_drift_protocol_id"] = drift_review["protocol_id"]
        report["covariate_drift_protocol_sha256"] = drift_review[
            "protocol_sha256"
        ]
        report["covariate_drift_review"] = drift_review
    elif design_id == feature_v13.DESIGN_ID:
        protocol = v13_geometry_review_protocol()
        review = v13_geometry_review(report, design, protocol)
        report["geometry_review_protocol_id"] = review["protocol_id"]
        report["geometry_review_protocol_sha256"] = review["protocol_sha256"]
        report["geometry_review"] = review
        drift_protocol = v13_covariate_drift_protocol()
        drift_review = v13_covariate_drift_review(
            examples,
            feature_names,
            design,
            drift_protocol,
            timestamp_alignment_failures=int(
                report["timestamp_alignment_failures"]
            ),
        )
        report["covariate_drift_protocol_id"] = drift_review["protocol_id"]
        report["covariate_drift_protocol_sha256"] = drift_review[
            "protocol_sha256"
        ]
        report["covariate_drift_review"] = drift_review
    return report


def _markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Q15 RTI microstructure numerical feature audit",
        "",
        f"- Status: `{report['status']}`",
        f"- Design: `{report['design_id']}`",
        f"- Fingerprint: `{report['design_sha256']}`",
        f"- Executable seven-asset windows: {report['complete_executable_close_windows']}",
        f"- Windows remaining to first review: {report['windows_remaining_to_first_review']}",
        f"- Outcome labels read: {report['outcome_labels_read']}",
        f"- Model fit: {report['model_fit_performed']}",
        f"- Feature profile allow-list enforced: {report['feature_profile_allow_list_enforced']}",
        f"- Raw threshold JSON selected by CLI loader: {report['raw_threshold_json_selected_by_cli_loader']}",
        "",
        "| Cohort | Rows | Windows | Active | Rank | Stable rank | Projected train rows/active | High |r| pairs | Exact duplicates |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, cohort in report["cohorts"].items():
        correlation = cohort["correlation_diagnostics"]
        geometry = cohort["matrix_geometry"]
        capacity = cohort["preregistered_fit_capacity"]
        stable_rank = geometry["stable_rank"]
        projected_ratio = capacity[
            "projected_train_rows_per_currently_active_feature"
        ]
        lines.append(
            f"| {name} | {cohort['rows']} | {cohort['independent_close_windows']} | "
            f"{geometry['active_feature_count']} | "
            f"{geometry['numerical_rank']} | "
            f"{'n/a' if stable_rank is None else f'{stable_rank:.2f}'} | "
            f"{'n/a' if projected_ratio is None else f'{projected_ratio:.2f}'} | "
            f"{len(correlation['high_absolute_correlation_pairs'])} | "
            f"{len(correlation['exact_signed_duplicate_pairs'])} |"
        )
    soft_integrity = report["soft_input_integrity"]
    timing = report["decision_timing_integrity"]
    offset = timing["exact_capture_offset_seconds"]
    assembly = timing["evidence_assembly_lag_seconds"]
    lines.extend((
        "",
        "## Outcome-blind input integrity",
        "",
        f"- Status: `{soft_integrity['status']}`",
        f"- Fully observed executable rows: "
        f"{soft_integrity['fully_observed_rows']}/{soft_integrity['rows']}",
        f"- Soft-degraded executable rows: "
        f"{soft_integrity['soft_degraded_rows']}",
        f"- Fully observed independent close windows: "
        f"{soft_integrity['fully_observed_close_windows']}/"
        f"{soft_integrity['independent_close_windows']}",
        f"- Soft-degraded independent close windows: "
        f"{soft_integrity['soft_degraded_close_windows']}",
        f"- Degradation by asset: "
        f"{json.dumps(soft_integrity['degraded_by_asset'], sort_keys=True)}",
        f"- Degradation by retained indicator: "
        f"{json.dumps(soft_integrity['degraded_by_reason'], sort_keys=True)}",
        f"- Exact-capture offset range: "
        f"{offset['minimum']} to {offset['maximum']} seconds",
        f"- Evidence-assembly lag range: "
        f"{assembly['minimum']} to {assembly['maximum']} seconds",
        f"- Timing status: `{timing['status']}`",
        "- Frozen eligibility/readiness credit changed: False",
        "",
        "This is an outcome-blind diagnostic. It cannot change the pinned design, "
        "fit a model, emit an artifact, send a notification, trade, refit, or promote.",
        "",
    ))
    geometry_review = report.get("geometry_review")
    if isinstance(geometry_review, Mapping):
        alias = geometry_review.get("btc_alias_pair_correlation")
        lines.extend((
            f"## Frozen {'V13' if report['design_id'] == feature_v13.DESIGN_ID else 'V12'} geometry review",
            "",
            f"- Protocol: `{geometry_review['protocol_id']}`",
            f"- Protocol SHA-256: `{geometry_review['protocol_sha256']}`",
            f"- Status: `{geometry_review['status']}`",
            f"- Review ready: {geometry_review['review_ready']}",
        ))
        if "successor_hypothesis_triggered" in geometry_review:
            lines.extend((
                f"- BTC predeclared alias correlation: "
                f"{'n/a' if alias is None else f'{float(alias):.6f}'}",
                f"- Successor hypothesis triggered: "
                f"{geometry_review['successor_hypothesis_triggered']}",
            ))
        else:
            conditioned = geometry_review.get(
                "btc_conditioned_feature_statistics", {}
            )
            lines.extend((
                f"- BTC conditioned feature maximum absolute value: "
                f"{conditioned.get('max_abs', 'n/a')}",
                f"- All frozen checks met: "
                f"{geometry_review.get('all_checks_met', False)}",
            ))
        lines.extend((
            "- Outcome labels read: False",
            "- Automatic design change: False",
            "",
        ))
    drift_review = report.get("covariate_drift_review")
    if isinstance(drift_review, Mapping):
        lines.extend((
            f"## Frozen {'V13' if report['design_id'] == feature_v13.DESIGN_ID else 'V12'} chronological covariate-drift review",
            "",
            f"- Protocol: `{drift_review['protocol_id']}`",
            f"- Protocol SHA-256: `{drift_review['protocol_sha256']}`",
            f"- Status: `{drift_review['status']}`",
            f"- Review ready: {drift_review['review_ready']}",
            f"- Same-close assets share a half: "
            f"{drift_review['same_close_assets_share_half']}",
            f"- Preview breach count: {drift_review['preview_breach_count']}",
            f"- Confirmed drift detected: {drift_review['drift_detected']}",
            "- Outcome labels read: False",
            "- Automatic feature or threshold changes: False",
            "",
        ))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--design",
        required=True,
        help=(
            "Explicit frozen design manifest. Required so an audit cannot "
            "silently report an older cohort as the current challenger."
        ),
    )
    parser.add_argument("--strategy-db", default=str(DEFAULT_DB))
    parser.add_argument(
        "--output-dir", default="work/rti-microstructure-feature-audit"
    )
    args = parser.parse_args()
    design = json.loads(Path(args.design).read_text(encoding="utf-8"))
    if not isinstance(design, Mapping):
        raise ValueError("design_root_not_object")
    report = build_feature_audit(
        load_feature_rows(Path(args.strategy_db)), design,
    )
    output = Path(args.output_dir)
    bind_design_output_directory(
        output,
        design_id=report["design_id"],
        design_sha256=report["design_sha256"],
    )
    json_path = output / "audit.json"
    markdown_path = output / "audit.md"
    atomic_write_json(json_path, report)
    atomic_write_text(markdown_path, _markdown(report))
    print(json.dumps({
        "status": report["status"],
        "json": str(json_path),
        "markdown": str(markdown_path),
        "complete_executable_close_windows": report[
            "complete_executable_close_windows"
        ],
        "windows_remaining_to_first_review": report[
            "windows_remaining_to_first_review"
        ],
        "outcome_labels_read": report["outcome_labels_read"],
        "model_fit_performed": report["model_fit_performed"],
        "artifact_emitted": report["artifact_emitted"],
    }, indent=2))


if __name__ == "__main__":
    main()
