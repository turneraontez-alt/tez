"""One-shot freeze procedure for the preregistered RTI microstructure study.

The command is intentionally two-stage and fail closed:

1. Feature readiness is checked without selecting any outcome column.
2. Only after the cohort's preregistered independent-window minimum is met are
   train/calibration labels read.
3. V11 must pass a separately hashed, expanding-window walk-forward gate and
   the frozen calibration gate using only pre-test windows.
4. Untouched-test labels are read only after both earlier gates pass and
   the operator supplies ``--confirm-score-untouched-test``.  An exclusive
   durable reservation prevents scoring that test a second time.

No output from this command is connected to notifications, orders, or automatic
promotion.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Callable, Mapping, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots.costs import (
    KALSHI_Q15_FEE_SCHEDULE_VERSION,
    RTI_EXECUTION_COST_MODEL_VERSION,
    rti_simulated_execution,
)
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
from q15_upgrade.strategy_bots.rti_microstructure import (
    FEATURE_NAMES,
    feature_vector,
)
from tools.q15_rti_feature_coverage_audit import (
    EXPECTED_ASSETS,
    SAFE_FEATURE_PROFILE_KEYS,
    build_report,
    feature_only_sql_projection,
    materialize_feature_only_row,
    sanitize_feature_rows,
)
from tools.q15_rti_output_integrity import (
    atomic_write_json,
    bind_design_output_directory,
)
from tools.q15_rti_microstructure_preregister import (
    DEFAULT_DB,
    DEFAULT_DESIGN,
    build_readiness,
    design_fingerprint,
    validate_design,
)


COHORT_ASSETS = {
    "BTC": frozenset({"BTC"}),
    "NON_BTC_TRANSFER": EXPECTED_ASSETS - {"BTC"},
}
DEFAULT_V11_WALK_FORWARD_PROTOCOL = (
    ROOT / "config" / "q15_rti_v11_walk_forward_protocol.json"
)
EXPECTED_V11_WALK_FORWARD_PROTOCOL_SHA256 = (
    feature_v11.EVALUATION_PROTOCOL_SHA256
)
DEFAULT_V12_WALK_FORWARD_PROTOCOL = (
    ROOT / "config" / "q15_rti_v12_walk_forward_protocol.json"
)
EXPECTED_V12_WALK_FORWARD_PROTOCOL_SHA256 = (
    feature_v12.EVALUATION_PROTOCOL_SHA256
)
DEFAULT_V13_WALK_FORWARD_PROTOCOL = (
    ROOT / "config" / "q15_rti_v13_walk_forward_protocol.json"
)
EXPECTED_V13_WALK_FORWARD_PROTOCOL_SHA256 = (
    feature_v13.EVALUATION_PROTOCOL_SHA256
)
DEFAULT_V14_WALK_FORWARD_PROTOCOL = (
    ROOT / "config" / "q15_rti_v14_walk_forward_protocol.json"
)
EXPECTED_V14_WALK_FORWARD_PROTOCOL_SHA256 = (
    feature_v14.EVALUATION_PROTOCOL_SHA256
)
DEFAULT_V11_REPORTING_PROTOCOL = (
    ROOT / "config" / "q15_rti_v11_reporting_protocol.json"
)
EXPECTED_V11_REPORTING_PROTOCOL_SHA256 = (
    feature_v11.REPORTING_PROTOCOL_SHA256
)
DEFAULT_V11_CALIBRATION_REPORTING_PROTOCOL = (
    ROOT / "config" / "q15_rti_v11_calibration_reporting_protocol.json"
)
EXPECTED_V11_CALIBRATION_REPORTING_PROTOCOL_SHA256 = (
    feature_v11.CALIBRATION_REPORTING_PROTOCOL_SHA256
)
DEFAULT_V11_SELECTIVE_VALUE_CURVE_PROTOCOL = (
    ROOT / "config" / "q15_rti_v11_selective_value_curve_protocol.json"
)
EXPECTED_V11_SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256 = (
    feature_v11.SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256
)
DEFAULT_V13_REPORTING_PROTOCOL = (
    ROOT / "config" / "q15_rti_v13_reporting_protocol.json"
)
EXPECTED_V13_REPORTING_PROTOCOL_SHA256 = feature_v13.REPORTING_PROTOCOL_SHA256
DEFAULT_V13_CALIBRATION_REPORTING_PROTOCOL = (
    ROOT / "config" / "q15_rti_v13_calibration_reporting_protocol.json"
)
EXPECTED_V13_CALIBRATION_REPORTING_PROTOCOL_SHA256 = (
    feature_v13.CALIBRATION_REPORTING_PROTOCOL_SHA256
)
DEFAULT_V13_SELECTIVE_VALUE_CURVE_PROTOCOL = (
    ROOT / "config" / "q15_rti_v13_selective_value_curve_protocol.json"
)
EXPECTED_V13_SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256 = (
    feature_v13.SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256
)
DEFAULT_V14_REPORTING_PROTOCOL = (
    ROOT / "config" / "q15_rti_v14_reporting_protocol.json"
)
EXPECTED_V14_REPORTING_PROTOCOL_SHA256 = feature_v14.REPORTING_PROTOCOL_SHA256
DEFAULT_V14_CALIBRATION_REPORTING_PROTOCOL = (
    ROOT / "config" / "q15_rti_v14_calibration_reporting_protocol.json"
)
EXPECTED_V14_CALIBRATION_REPORTING_PROTOCOL_SHA256 = (
    feature_v14.CALIBRATION_REPORTING_PROTOCOL_SHA256
)
DEFAULT_V14_SELECTIVE_VALUE_CURVE_PROTOCOL = (
    ROOT / "config" / "q15_rti_v14_selective_value_curve_protocol.json"
)
EXPECTED_V14_SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256 = (
    feature_v14.SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256
)


def evaluation_protocol_fingerprint(protocol: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        dict(protocol), sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def reporting_protocol_fingerprint(protocol: Mapping[str, Any]) -> str:
    return evaluation_protocol_fingerprint(protocol)


def calibration_reporting_protocol_fingerprint(
    protocol: Mapping[str, Any],
) -> str:
    return evaluation_protocol_fingerprint(protocol)


def selective_value_curve_protocol_fingerprint(
    protocol: Mapping[str, Any],
) -> str:
    return evaluation_protocol_fingerprint(protocol)


def validate_reporting_protocol(
    protocol: Mapping[str, Any], design: Mapping[str, Any],
) -> None:
    """Fail closed unless the report-only subgroup plan is immutable."""
    required_true = (
        "report_only",
        "paper_only",
        "cohort_pooling_forbidden",
        "same_close_assets_remain_in_same_chronological_fold",
        "untouched_test_scored_once",
    )
    required_false = (
        "outcome_labels_used_for_protocol",
        "performance_metrics_inspected_before_preregistration",
        "changes_features_model_hyperparameters_entry_policy_or_gates",
        "notification_eligible",
        "real_trading_allowed",
        "automatic_promotion",
    )
    if any(protocol.get(key) is not True for key in required_true):
        raise ValueError("reporting_protocol_required_true_flag_missing")
    if any(protocol.get(key) is not False for key in required_false):
        raise ValueError("reporting_protocol_required_false_flag_missing")
    design_id = str(design.get("design_id") or "")
    runtime = {
        feature_v11.DESIGN_ID: feature_v11,
        feature_v13.DESIGN_ID: feature_v13,
        feature_v14.DESIGN_ID: feature_v14,
    }.get(design_id)
    if runtime is None:
        raise ValueError("reporting_protocol_unsupported_design")
    version = (
        "V11" if runtime is feature_v11
        else "V13" if runtime is feature_v13
        else "V14"
    )
    expected_status = f"PREREGISTERED_BEFORE_ANY_{version}_OUTCOME_REVIEW"
    if protocol.get("protocol_status") != expected_status:
        raise ValueError("reporting_protocol_status_mismatch")
    if protocol.get("protocol_id") != runtime.REPORTING_PROTOCOL_ID:
        raise ValueError("reporting_protocol_id_mismatch")
    if protocol.get("applies_to_design_id") != design.get("design_id"):
        raise ValueError("reporting_protocol_design_id_mismatch")
    if protocol.get("applies_to_design_sha256") != design_fingerprint(design):
        raise ValueError("reporting_protocol_design_sha_mismatch")
    dimensions = protocol.get("dimensions")
    if not isinstance(dimensions, Mapping) or tuple(dimensions) != (
        "asset",
        "rti_side",
        "absolute_distance_tier",
        "realized_volatility_tier",
        "market_regime",
    ):
        raise ValueError("reporting_protocol_dimensions_mismatch")
    required_features = {
        "yes_signed_distance_bps",
        "log1p_realized_volatility_bps",
        "cross_asset_median_momentum_60s",
        "cross_asset_breadth_signed_60s",
    }
    if runtime is feature_v11:
        required_features.add("final_side_yes")
        if dict(dimensions["rti_side"]).get("source_feature") != "final_side_yes":
            raise ValueError("reporting_protocol_side_source_mismatch")
    else:
        if (
            dict(dimensions["rti_side"]).get("source")
            != "stored_point_in_time_side"
            or dict(dimensions["market_regime"]).get("target_side_source")
            != "stored_point_in_time_side"
        ):
            raise ValueError("reporting_protocol_side_source_mismatch")
    if not required_features.issubset(set(design.get("feature_names") or ())):
        raise ValueError("reporting_protocol_required_feature_missing")
    counterfactual = protocol.get("rejected_counterfactual_policy")
    if not isinstance(counterfactual, Mapping) or any(
        counterfactual.get(key) is not True
        for key in (
            "paper_counterfactual_only",
            "never_claimed_as_fill",
            "requires_point_in_time_side_ask",
            "requires_displayed_depth_at_least_simulation_contracts",
            "uses_official_kalshi_fee_schedule",
            "uses_same_two_cent_slippage_per_contract",
            "non_executable_rows_have_no_pnl",
        )
    ):
        raise ValueError("reporting_protocol_counterfactual_guard_missing")
    if runtime in {feature_v13, feature_v14}:
        sensitivity = protocol.get("known_loss_sensitivity")
        required = (
            (
                "three_pre_v13_known_losses_are_outside_v13_prospective_boundary",
                "known_losses_may_not_be_removed_from_any_v13_metric",
                "no_post_boundary_row_may_be_excluded_by_similarity_to_a_known_loss",
            )
            if runtime is feature_v13 else (
                "all_pre_v14_known_losses_are_outside_v14_prospective_boundary",
                "known_losses_may_not_be_removed_from_any_v14_metric",
                "no_post_boundary_row_may_be_excluded_by_similarity_to_a_known_loss",
            )
        )
        if not isinstance(sensitivity, Mapping) or any(
            sensitivity.get(key) is not True for key in required
        ):
            raise ValueError("reporting_protocol_known_loss_guard_missing")


def reporting_protocol_for_design(
    design: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    config = {
        feature_v11.DESIGN_ID: (
            DEFAULT_V11_REPORTING_PROTOCOL,
            EXPECTED_V11_REPORTING_PROTOCOL_SHA256,
        ),
        feature_v13.DESIGN_ID: (
            DEFAULT_V13_REPORTING_PROTOCOL,
            EXPECTED_V13_REPORTING_PROTOCOL_SHA256,
        ),
        feature_v14.DESIGN_ID: (
            DEFAULT_V14_REPORTING_PROTOCOL,
            EXPECTED_V14_REPORTING_PROTOCOL_SHA256,
        ),
    }.get(str(design.get("design_id") or ""))
    if config is None:
        return None
    path, expected_sha256 = config
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("reporting_protocol_root_not_object")
    validate_reporting_protocol(raw, design)
    if reporting_protocol_fingerprint(raw) != expected_sha256:
        raise ValueError("reporting_protocol_fingerprint_mismatch")
    return raw


def validate_calibration_reporting_protocol(
    protocol: Mapping[str, Any], design: Mapping[str, Any],
) -> None:
    required_false = (
        "outcome_labels_used_for_protocol",
        "performance_metrics_inspected_before_preregistration",
        "changes_features_model_hyperparameters_entry_policy_or_gates",
        "changes_deployment_gate",
        "automatic_promotion",
        "notification_eligible",
        "real_trading_allowed",
    )
    if any(protocol.get(key) is not False for key in required_false):
        raise ValueError("calibration_reporting_required_false_flag_missing")
    if protocol.get("report_only") is not True or (
        protocol.get("paper_only") is not True
    ):
        raise ValueError("calibration_reporting_safety_guard_missing")
    design_id = str(design.get("design_id") or "")
    runtime = {
        feature_v11.DESIGN_ID: feature_v11,
        feature_v13.DESIGN_ID: feature_v13,
        feature_v14.DESIGN_ID: feature_v14,
    }.get(design_id)
    if runtime is None:
        raise ValueError("calibration_reporting_unsupported_design")
    version = (
        "V11" if runtime is feature_v11
        else "V13" if runtime is feature_v13
        else "V14"
    )
    if protocol.get("protocol_status") != (
        f"PREREGISTERED_BEFORE_ANY_{version}_OUTCOME_REVIEW"
    ) or protocol.get("protocol_id") != (
        runtime.CALIBRATION_REPORTING_PROTOCOL_ID
    ):
        raise ValueError("calibration_reporting_protocol_identity_mismatch")
    bindings = {
        "applies_to_design_id": runtime.DESIGN_ID,
        "applies_to_design_sha256": runtime.DESIGN_SHA256,
        "applies_to_evaluation_protocol_id": runtime.EVALUATION_PROTOCOL_ID,
        "applies_to_evaluation_protocol_sha256": (
            runtime.EVALUATION_PROTOCOL_SHA256
        ),
        "applies_to_subgroup_reporting_protocol_id": (
            runtime.REPORTING_PROTOCOL_ID
        ),
        "applies_to_subgroup_reporting_protocol_sha256": (
            runtime.REPORTING_PROTOCOL_SHA256
        ),
    }
    if any(protocol.get(key) != value for key, value in bindings.items()):
        raise ValueError("calibration_reporting_protocol_binding_mismatch")
    if (
        design.get("design_id") != runtime.DESIGN_ID
        or design_fingerprint(design) != runtime.DESIGN_SHA256
    ):
        raise ValueError("calibration_reporting_design_binding_mismatch")
    weighting = protocol.get("weighting")
    if not isinstance(weighting, Mapping) or (
        weighting.get("row_weighting") != "equal"
        or weighting.get("same_close_assets_are_complete_and_equal_count")
        is not True
        or weighting.get("btc_and_non_btc_never_mixed") is not True
    ):
        raise ValueError("calibration_reporting_weighting_invalid")
    bins = protocol.get("probability_bins")
    expected_bins = (
        ("0.00_to_lt_0.20", 0.0, 0.2, False),
        ("0.20_to_lt_0.35", 0.2, 0.35, False),
        ("0.35_to_lt_0.50", 0.35, 0.5, False),
        ("0.50_to_lt_0.65", 0.5, 0.65, False),
        ("0.65_to_lt_0.80", 0.65, 0.8, False),
        ("0.80_to_1.00", 0.8, 1.0, True),
    )
    if not isinstance(bins, list) or len(bins) != len(expected_bins):
        raise ValueError("calibration_reporting_bins_invalid")
    for raw, (label, low, high, inclusive) in zip(bins, expected_bins):
        if not isinstance(raw, Mapping) or (
            raw.get("label") != label
            or float(raw.get("minimum_inclusive", -1.0)) != low
            or float(raw.get(
                "maximum_inclusive" if inclusive else "maximum_exclusive",
                -1.0,
            )) != high
            or ("maximum_inclusive" in raw) is not inclusive
            or ("maximum_exclusive" in raw) is inclusive
        ):
            raise ValueError("calibration_reporting_bins_invalid")
    model_source = (
        "v11_model" if runtime is feature_v11
        else "v13_model" if runtime is feature_v13
        else "v14_model"
    )
    if protocol.get("reported_for") != [model_source, "kalshi_market_prior"]:
        raise ValueError("calibration_reporting_sources_invalid")
    if protocol.get("required_overall_metrics") != [
        "rows",
        "mean_probability",
        "observed_yes_rate",
        "calibration_bias_probability_minus_observed",
        "expected_calibration_error",
        "maximum_calibration_error",
        "binned_reliability",
        "binned_resolution",
        "outcome_uncertainty",
    ] or protocol.get("required_per_observed_bin_metrics") != [
        "rows", "mean_probability", "observed_yes_rate",
        "absolute_calibration_gap",
    ]:
        raise ValueError("calibration_reporting_metrics_invalid")
    if protocol.get("empty_bins_must_be_reported") is not True or (
        protocol.get("comparisons") != [
            "model_minus_market_expected_calibration_error",
            "model_minus_market_maximum_calibration_error",
            "model_minus_market_absolute_calibration_bias",
        ]
    ):
        raise ValueError("calibration_reporting_comparison_invalid")
    small_sample = protocol.get("small_sample_interpretation")
    if not isinstance(small_sample, Mapping) or (
        int(small_sample.get("minimum_rows_for_promotional_claim") or 0) != 30
        or small_sample.get("empty_or_small_bins_may_not_be_claimed_as_calibrated")
        is not True
        or small_sample.get("historical_calibration_cannot_promote") is not True
    ):
        raise ValueError("calibration_reporting_small_sample_guard_invalid")


def calibration_reporting_protocol_for_design(
    design: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    config = {
        feature_v11.DESIGN_ID: (
            DEFAULT_V11_CALIBRATION_REPORTING_PROTOCOL,
            EXPECTED_V11_CALIBRATION_REPORTING_PROTOCOL_SHA256,
        ),
        feature_v13.DESIGN_ID: (
            DEFAULT_V13_CALIBRATION_REPORTING_PROTOCOL,
            EXPECTED_V13_CALIBRATION_REPORTING_PROTOCOL_SHA256,
        ),
        feature_v14.DESIGN_ID: (
            DEFAULT_V14_CALIBRATION_REPORTING_PROTOCOL,
            EXPECTED_V14_CALIBRATION_REPORTING_PROTOCOL_SHA256,
        ),
    }.get(str(design.get("design_id") or ""))
    if config is None:
        return None
    path, expected_sha256 = config
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("calibration_reporting_protocol_root_not_object")
    validate_calibration_reporting_protocol(raw, design)
    if calibration_reporting_protocol_fingerprint(raw) != expected_sha256:
        raise ValueError("calibration_reporting_protocol_fingerprint_mismatch")
    return raw


def validate_selective_value_curve_protocol(
    protocol: Mapping[str, Any], design: Mapping[str, Any],
) -> None:
    design_id = str(design.get("design_id") or "")
    runtime = {
        feature_v11.DESIGN_ID: feature_v11,
        feature_v13.DESIGN_ID: feature_v13,
        feature_v14.DESIGN_ID: feature_v14,
    }.get(design_id)
    if runtime is None:
        raise ValueError("selective_value_curve_unsupported_design")
    version = (
        "V11" if runtime is feature_v11
        else "V13" if runtime is feature_v13
        else "V14"
    )
    if protocol.get("protocol_status") != (
        f"PREREGISTERED_BEFORE_ANY_{version}_OUTCOME_REVIEW"
    ) or protocol.get("protocol_id") != (
        runtime.SELECTIVE_VALUE_CURVE_PROTOCOL_ID
    ):
        raise ValueError("selective_value_curve_protocol_identity_mismatch")
    bindings = {
        "applies_to_design_id": runtime.DESIGN_ID,
        "applies_to_design_sha256": runtime.DESIGN_SHA256,
        "applies_to_evaluation_protocol_id": runtime.EVALUATION_PROTOCOL_ID,
        "applies_to_evaluation_protocol_sha256": (
            runtime.EVALUATION_PROTOCOL_SHA256
        ),
        "applies_to_subgroup_reporting_protocol_id": (
            runtime.REPORTING_PROTOCOL_ID
        ),
        "applies_to_subgroup_reporting_protocol_sha256": (
            runtime.REPORTING_PROTOCOL_SHA256
        ),
        "applies_to_calibration_reporting_protocol_id": (
            runtime.CALIBRATION_REPORTING_PROTOCOL_ID
        ),
        "applies_to_calibration_reporting_protocol_sha256": (
            runtime.CALIBRATION_REPORTING_PROTOCOL_SHA256
        ),
    }
    if any(protocol.get(key) != value for key, value in bindings.items()):
        raise ValueError("selective_value_curve_protocol_binding_mismatch")
    if (
        design.get("design_id") != runtime.DESIGN_ID
        or design_fingerprint(design) != runtime.DESIGN_SHA256
    ):
        raise ValueError("selective_value_curve_design_binding_mismatch")
    for key in (
        "outcome_labels_used_for_protocol",
        "performance_metrics_inspected_before_preregistration",
        "changes_features_model_hyperparameters_entry_policy_or_gates",
        "changes_deployment_gate",
        "automatic_promotion",
        "notification_eligible",
        "real_trading_allowed",
    ):
        if protocol.get(key) is not False:
            raise ValueError("selective_value_curve_required_false_flag_missing")
    if protocol.get("report_only") is not True or (
        protocol.get("paper_only") is not True
    ):
        raise ValueError("selective_value_curve_safety_guard_missing")
    if protocol.get("fixed_expected_value_thresholds_cents") != [
        0.0, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0,
    ] or float(protocol.get("frozen_entry_policy_threshold_cents") or -1.0) != 3.0:
        raise ValueError("selective_value_curve_thresholds_invalid")
    rules = protocol.get("all_other_entry_rules_remain_fixed")
    expected_rules = {
        "maximum_ask_cents": 62.0,
        "maximum_spread_cents": 1.5,
        "minimum_displayed_depth_contracts": 10.0,
        "simulation_contracts": 10,
        "official_kalshi_fees": True,
        "slippage_cents_per_contract": 2.0,
    }
    if not isinstance(rules, Mapping) or dict(rules) != expected_rules:
        raise ValueError("selective_value_curve_entry_rules_invalid")
    if protocol.get("required_metrics_per_threshold") != [
        "decision_rows",
        "picks",
        "decision_row_coverage",
        "pick_frequency_per_close_window",
        "pick_correct",
        "pick_accuracy",
        "pick_wilson_95_low",
        "pick_wilson_95_high",
        "ten_contract_net_pnl_dollars",
        "fee_slippage_adjusted_ev_cents_per_trade",
        "max_drawdown_dollars_at_sim_size",
        "selected_row_ids_sha256",
    ]:
        raise ValueError("selective_value_curve_metrics_invalid")
    invariants = protocol.get("curve_invariants")
    if not isinstance(invariants, Mapping) or any(
        invariants.get(key) is not True
        for key in (
            "higher_threshold_pick_set_must_be_subset",
            "higher_threshold_pick_count_must_not_increase",
            "same_quote_cost_and_depth_rules_at_every_threshold",
            "btc_and_non_btc_never_mixed",
        )
    ):
        raise ValueError("selective_value_curve_invariants_invalid")
    interpretation = protocol.get("interpretation")
    if not isinstance(interpretation, Mapping) or any(
        interpretation.get(key) is not True
        for key in (
            "counterfactual_paper_curve_only",
            "never_claimed_as_historical_fills",
            "untouched_test_curve_cannot_select_or_change_the_live_threshold",
            "historical_curve_cannot_promote",
            "future_threshold_change_requires_new_preregistered_prospective_challenger",
        )
    ):
        raise ValueError("selective_value_curve_interpretation_invalid")


def selective_value_curve_protocol_for_design(
    design: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    config = {
        feature_v11.DESIGN_ID: (
            DEFAULT_V11_SELECTIVE_VALUE_CURVE_PROTOCOL,
            EXPECTED_V11_SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256,
        ),
        feature_v13.DESIGN_ID: (
            DEFAULT_V13_SELECTIVE_VALUE_CURVE_PROTOCOL,
            EXPECTED_V13_SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256,
        ),
        feature_v14.DESIGN_ID: (
            DEFAULT_V14_SELECTIVE_VALUE_CURVE_PROTOCOL,
            EXPECTED_V14_SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256,
        ),
    }.get(str(design.get("design_id") or ""))
    if config is None:
        return None
    path, expected_sha256 = config
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("selective_value_curve_protocol_root_not_object")
    validate_selective_value_curve_protocol(raw, design)
    if selective_value_curve_protocol_fingerprint(raw) != expected_sha256:
        raise ValueError("selective_value_curve_protocol_fingerprint_mismatch")
    return raw


def validate_walk_forward_protocol(
    protocol: Mapping[str, Any], design: Mapping[str, Any],
) -> None:
    """Fail closed unless the companion evaluation protocol is immutable.

    This protocol can make deployment harder, never easier.  It may fit
    temporary diagnostic models only on the already-preregistered train and
    calibration windows; the untouched test remains unavailable until every
    earlier gate passes and the operator explicitly confirms scoring it.
    """
    required_true = (
        "paper_only",
        "same_close_assets_must_share_fold",
        "btc_and_non_btc_must_be_evaluated_separately",
        "uses_only_preregistered_train_and_calibration_windows",
        "untouched_test_windows_forbidden",
        "temporary_fold_models_are_not_deployable_artifacts",
    )
    required_false = (
        "outcome_labels_used_for_protocol",
        "performance_metrics_inspected_before_preregistration",
        "changes_features_model_or_hyperparameters",
        "real_trading_allowed",
        "notification_eligible",
        "automatic_promotion",
        "automatic_hyperparameter_search",
    )
    if any(protocol.get(key) is not True for key in required_true):
        raise ValueError("walk_forward_required_true_flag_missing")
    if any(protocol.get(key) is not False for key in required_false):
        raise ValueError("walk_forward_required_false_flag_missing")
    expected_protocol_identity = {
        feature_v11.DESIGN_ID: (
            "PREREGISTERED_BEFORE_ANY_V11_OUTCOME_REVIEW",
            feature_v11.EVALUATION_PROTOCOL_ID,
        ),
        feature_v12.DESIGN_ID: (
            "PREREGISTERED_BEFORE_ANY_V12_OUTCOME_REVIEW",
            feature_v12.EVALUATION_PROTOCOL_ID,
        ),
        feature_v13.DESIGN_ID: (
            "PREREGISTERED_BEFORE_ANY_V13_OUTCOME_REVIEW",
            feature_v13.EVALUATION_PROTOCOL_ID,
        ),
        feature_v14.DESIGN_ID: (
            "PREREGISTERED_BEFORE_ANY_V14_OUTCOME_REVIEW",
            feature_v14.EVALUATION_PROTOCOL_ID,
        ),
    }.get(str(design.get("design_id") or ""))
    if expected_protocol_identity is None:
        raise ValueError("walk_forward_unsupported_design")
    expected_status, expected_protocol_id = expected_protocol_identity
    if protocol.get("protocol_status") != expected_status:
        raise ValueError("walk_forward_protocol_status_mismatch")
    if protocol.get("protocol_id") != expected_protocol_id:
        raise ValueError("walk_forward_protocol_id_mismatch")
    if protocol.get("applies_to_design_id") != design.get("design_id"):
        raise ValueError("walk_forward_design_id_mismatch")
    if protocol.get("applies_to_design_sha256") != design_fingerprint(design):
        raise ValueError("walk_forward_design_fingerprint_mismatch")
    if protocol.get("fold_policy") != (
        "EXPANDING_TRAIN_NEXT_CONTIGUOUS_BLOCK"
    ):
        raise ValueError("walk_forward_fold_policy_mismatch")
    gate = protocol.get("gate")
    if not isinstance(gate, Mapping):
        raise ValueError("walk_forward_gate_missing")
    for key in (
        "aggregate_brier_must_strictly_beat_market",
        "aggregate_log_loss_must_strictly_beat_market",
        "every_fold_brier_must_not_worsen",
        "every_fold_log_loss_must_not_worsen",
        "aggregate_brier_bootstrap_upper_must_be_strictly_negative",
        "aggregate_log_loss_bootstrap_upper_must_be_strictly_negative",
        "aggregate_bootstrap_upper_must_clear_fixed_effect_floor",
        "calibration_bootstrap_upper_must_be_strictly_negative",
        "calibration_bootstrap_upper_must_clear_fixed_effect_floor",
        "untouched_test_bootstrap_upper_must_be_strictly_negative",
        "untouched_test_bootstrap_upper_must_clear_fixed_effect_floor",
    ):
        if gate.get(key) is not True:
            raise ValueError(f"walk_forward_gate_flag_missing:{key}")
    if gate.get("untouched_test_may_be_read_when_not_met") is not False:
        raise ValueError("walk_forward_test_read_guard_missing")
    if gate.get("fallback_when_not_met") != "KALSHI_MARKET_PRIOR":
        raise ValueError("walk_forward_fallback_mismatch")
    bootstrap = protocol.get("paired_close_window_bootstrap")
    if not isinstance(bootstrap, Mapping):
        raise ValueError("walk_forward_bootstrap_missing")
    if bootstrap.get("version") != "q15-rti-paired-close-window-bootstrap-v1":
        raise ValueError("walk_forward_bootstrap_version_mismatch")
    if bootstrap.get("cluster_key") != "close_time":
        raise ValueError("walk_forward_bootstrap_cluster_mismatch")
    if int(bootstrap.get("resamples") or 0) != 5000:
        raise ValueError("walk_forward_bootstrap_resamples_mismatch")
    if float(bootstrap.get("confidence_level") or 0.0) != 0.9:
        raise ValueError("walk_forward_bootstrap_confidence_mismatch")
    if int(bootstrap.get("random_seed") or 0) != 2026072201:
        raise ValueError("walk_forward_bootstrap_seed_mismatch")
    if float(bootstrap.get("minimum_mean_brier_improvement") or 0.0) != 0.001:
        raise ValueError("walk_forward_bootstrap_brier_floor_mismatch")
    if float(
        bootstrap.get("minimum_mean_log_loss_improvement") or 0.0
    ) != 0.001:
        raise ValueError("walk_forward_bootstrap_log_loss_floor_mismatch")
    for key in (
        "model_minus_market_loss_delta",
        "same_close_assets_resampled_together",
        "one_sided_upper_bound_reported",
        "two_sided_interval_reported",
    ):
        if bootstrap.get(key) is not True:
            raise ValueError(f"walk_forward_bootstrap_flag_missing:{key}")
    if str(design.get("design_id") or "") == feature_v14.DESIGN_ID:
        selection = protocol.get("residual_trust_selection")
        configured = design.get("prediction_combination")
        if not isinstance(selection, Mapping) or not isinstance(configured, Mapping):
            raise ValueError("v14_residual_trust_selection_missing")
        if selection.get("architecture") != (
            "nested_chronological_safe_residual_trust_v1"
        ) or selection.get("fixed_factor_grid") != [
            0.0, 0.25, 0.5, 0.75, 1.0,
        ] or float(selection.get("fallback_factor", -1.0)) != 0.0:
            raise ValueError("v14_residual_trust_identity_mismatch")
        if selection.get("fixed_factor_grid") != configured.get(
            "fixed_factor_grid"
        ):
            raise ValueError("v14_residual_trust_design_grid_mismatch")
        for key in (
            "factor_zero_is_exact_market_prior",
            "selection_requires_observed_brier_delta_below_zero",
            "selection_requires_observed_log_loss_delta_below_zero",
            "selection_requires_brier_bootstrap_one_sided_upper_below_zero",
            "selection_requires_log_loss_bootstrap_one_sided_upper_below_zero",
            "same_close_assets_must_share_inner_fold",
            "inner_validation_strictly_after_inner_training",
            "factor_reselected_inside_each_outer_training_period",
        ):
            if selection.get(key) is not True:
                raise ValueError(f"v14_residual_trust_guard_missing:{key}")
        for key in (
            "outer_validation_labels_used_for_factor_selection",
            "calibration_labels_used_for_factor_selection",
            "untouched_test_labels_used_for_factor_selection",
        ):
            if selection.get(key) is not False:
                raise ValueError(f"v14_residual_trust_label_guard_missing:{key}")
        inner_bootstrap = selection.get("bootstrap")
        if not isinstance(inner_bootstrap, Mapping) or (
            int(inner_bootstrap.get("resamples") or 0) != 5000
            or float(inner_bootstrap.get("confidence_level") or 0.0) != 0.9
            or int(inner_bootstrap.get("random_seed") or 0) != 2026072202
            or float(inner_bootstrap.get("minimum_mean_brier_improvement", -1.0))
            != 0.0
            or float(inner_bootstrap.get("minimum_mean_log_loss_improvement", -1.0))
            != 0.0
        ):
            raise ValueError("v14_residual_trust_bootstrap_mismatch")
        inner_folds = selection.get("inner_folds")
        if not isinstance(inner_folds, Mapping) or dict(
            inner_folds.get("NON_BTC_TRANSFER") or {}
        ) != {"initial_train_windows": 12, "validation_block_windows": 4} or dict(
            inner_folds.get("BTC") or {}
        ) != {"initial_train_windows": 30, "validation_block_windows": 10}:
            raise ValueError("v14_residual_trust_inner_folds_mismatch")
    chronology = dict(design.get("chronology") or {})
    minimums = dict(design.get("cohorts") or {})
    cohort_rules = protocol.get("cohorts")
    if not isinstance(cohort_rules, Mapping):
        raise ValueError("walk_forward_cohorts_missing")
    for cohort in COHORT_ASSETS:
        rule = cohort_rules.get(cohort)
        if not isinstance(rule, Mapping):
            raise ValueError(f"walk_forward_cohort_missing:{cohort}")
        initial = int(rule.get("initial_train_windows") or 0)
        block = int(rule.get("validation_block_windows") or 0)
        count = int(rule.get("fold_count") or 0)
        if initial <= 0 or block <= 0 or count < 2:
            raise ValueError(f"walk_forward_fold_shape_invalid:{cohort}")
        minimum = int(minimums[cohort]["minimum_complete_close_windows"])
        pretest = int(minimum * (
            float(chronology["train_fraction"])
            + float(chronology["calibration_fraction"])
        ))
        if initial + block * count != pretest:
            raise ValueError(f"walk_forward_pretest_coverage_mismatch:{cohort}")


def walk_forward_protocol_for_design(
    design: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    design_id = str(design.get("design_id") or "")
    protocol_config = {
        feature_v11.DESIGN_ID: (
            DEFAULT_V11_WALK_FORWARD_PROTOCOL,
            EXPECTED_V11_WALK_FORWARD_PROTOCOL_SHA256,
        ),
        feature_v12.DESIGN_ID: (
            DEFAULT_V12_WALK_FORWARD_PROTOCOL,
            EXPECTED_V12_WALK_FORWARD_PROTOCOL_SHA256,
        ),
        feature_v13.DESIGN_ID: (
            DEFAULT_V13_WALK_FORWARD_PROTOCOL,
            EXPECTED_V13_WALK_FORWARD_PROTOCOL_SHA256,
        ),
        feature_v14.DESIGN_ID: (
            DEFAULT_V14_WALK_FORWARD_PROTOCOL,
            EXPECTED_V14_WALK_FORWARD_PROTOCOL_SHA256,
        ),
    }.get(design_id)
    if protocol_config is None:
        return None
    protocol_path, expected_sha256 = protocol_config
    raw = json.loads(
        protocol_path.read_text(encoding="utf-8")
    )
    if not isinstance(raw, Mapping):
        raise ValueError("walk_forward_protocol_root_not_object")
    validate_walk_forward_protocol(raw, design)
    if evaluation_protocol_fingerprint(raw) != expected_sha256:
        raise ValueError("walk_forward_protocol_fingerprint_mismatch")
    return raw


def _feature_runtime(design: Mapping[str, Any]) -> Any:
    design_id = str(design.get("design_id") or "")
    if design_id == feature_v1.DESIGN_ID:
        return feature_v1
    if design_id == feature_v2.DESIGN_ID:
        return feature_v2
    if design_id == feature_v3.DESIGN_ID:
        return feature_v3
    if design_id == feature_v4.DESIGN_ID:
        return feature_v4
    if design_id == feature_v5.DESIGN_ID:
        return feature_v5
    if design_id == feature_v6.DESIGN_ID:
        return feature_v6
    if design_id == feature_v7.DESIGN_ID:
        return feature_v7
    if design_id == feature_v8.DESIGN_ID:
        return feature_v8
    if design_id == feature_v9.DESIGN_ID:
        return feature_v9
    if design_id == feature_v10.DESIGN_ID:
        return feature_v10
    if design_id == feature_v11.DESIGN_ID:
        return feature_v11
    if design_id == feature_v12.DESIGN_ID:
        return feature_v12
    if design_id == feature_v13.DESIGN_ID:
        return feature_v13
    if design_id == feature_v14.DESIGN_ID:
        return feature_v14
    raise ValueError("unsupported_design_id")
OUTCOME_COLUMNS = frozenset({
    "official_result", "correct", "hypothetical_pnl_cents", "resolved_at",
})
FEATURE_SELECT_COLUMNS = (
    "id", "bot_name", "source_system", "record_kind", "interval", "ticker",
    "asset", "side", "close_time", "entry_ask_cents", "spread_cents",
    "depth_contracts", "source_captured_at", "evidence_as_of",
)


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _logit(probability: float) -> float:
    p = _clip(probability, 1e-6, 1.0 - 1e-6)
    return math.log(p / (1.0 - p))


def _sigmoid_array(values: np.ndarray) -> np.ndarray:
    bounded = np.clip(values, -709.0, 709.0)
    return 1.0 / (1.0 + np.exp(-bounded))


def chronological_folds(
    close_times: Sequence[float], design: Mapping[str, Any],
) -> dict[str, tuple[float, ...]]:
    windows = tuple(sorted({float(value) for value in close_times}))
    chronology = dict(design["chronology"])
    train_fraction = float(chronology["train_fraction"])
    calibration_fraction = float(chronology["calibration_fraction"])
    train_end = int(len(windows) * train_fraction)
    calibration_end = int(len(windows) * (train_fraction + calibration_fraction))
    if train_end <= 0 or calibration_end <= train_end or calibration_end >= len(windows):
        raise ValueError("insufficient_windows_for_preregistered_folds")
    return {
        "train": windows[:train_end],
        "calibration": windows[train_end:calibration_end],
        "test": windows[calibration_end:],
    }


def expanding_walk_forward_folds(
    pretest_close_times: Sequence[float],
    protocol: Mapping[str, Any],
    cohort: str,
) -> tuple[dict[str, Any], ...]:
    """Return fixed expanding-train / next-block validation folds."""
    if cohort not in COHORT_ASSETS:
        raise ValueError("unsupported_cohort")
    windows = tuple(sorted({float(value) for value in pretest_close_times}))
    rule = dict(protocol["cohorts"][cohort])
    initial = int(rule["initial_train_windows"])
    block = int(rule["validation_block_windows"])
    fold_count = int(rule["fold_count"])
    expected = initial + block * fold_count
    if len(windows) != expected:
        raise ValueError("walk_forward_pretest_window_count_mismatch")
    folds: list[dict[str, Any]] = []
    for index in range(fold_count):
        validation_start = initial + index * block
        validation_end = validation_start + block
        train = windows[:validation_start]
        validation = windows[validation_start:validation_end]
        if not train or len(validation) != block:
            raise ValueError("walk_forward_fold_incomplete")
        if max(train) >= min(validation):
            raise ValueError("walk_forward_chronology_violation")
        folds.append({
            "fold": index + 1,
            "train": train,
            "validation": validation,
        })
    validation_windows = [
        value for fold in folds for value in fold["validation"]
    ]
    if len(validation_windows) != len(set(validation_windows)):
        raise ValueError("walk_forward_validation_overlap")
    if tuple(validation_windows) != windows[initial:]:
        raise ValueError("walk_forward_validation_coverage_mismatch")
    return tuple(folds)


def _connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=30.0,
    )
    connection.row_factory = sqlite3.Row
    return connection


def _feature_only_sqlite_authorizer(
    action_code: int,
    _object_name: str | None,
    column_name: str | None,
    _database_name: str | None,
    _trigger_name: str | None,
) -> int:
    """Make the feature-only loader's no-label claim enforceable by SQLite."""
    if (
        action_code == sqlite3.SQLITE_READ
        and str(column_name or "") in OUTCOME_COLUMNS
    ):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _load_feature_rows(
    db_path: Path, *, after_close_time: float | None,
) -> list[dict[str, Any]]:
    """Load the feature allow-list, optionally restricted by close time."""
    if OUTCOME_COLUMNS.intersection(FEATURE_SELECT_COLUMNS):
        raise AssertionError("feature_allow_list_contains_outcome")
    boundary = None if after_close_time is None else _num(after_close_time)
    if after_close_time is not None and boundary is None:
        raise ValueError("feature_database_close_boundary_invalid")
    connection = _connect_read_only(db_path)
    connection.set_authorizer(_feature_only_sqlite_authorizer)
    try:
        columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(strategy_bot_decisions)"
            ).fetchall()
        }
        selected = [name for name in FEATURE_SELECT_COLUMNS if name in columns]
        required = {
            "id", "bot_name", "record_kind", "interval", "asset", "close_time",
            "threshold_json", "kalshi_microstructure_schema_version",
        }
        if not required.issubset(columns):
            raise ValueError("feature_database_schema_incomplete")
        feature_projection, profile_aliases = feature_only_sql_projection(
            columns
        )
        boundary_sql = "" if boundary is None else "AND close_time>? "
        query = (
            f"SELECT {','.join([*selected, *feature_projection])} "
            "FROM strategy_bot_decisions "
            "WHERE bot_name='rti_path_13m' AND interval='13M' "
            "AND record_kind='RTI_PATH_13M_PROSPECTIVE_EXACT' "
            f"{boundary_sql}"
            "ORDER BY close_time,id"
        )
        parameters: tuple[float, ...] = (
            () if boundary is None else (float(boundary),)
        )
        rows = [
            materialize_feature_only_row(row, profile_aliases)
            for row in connection.execute(query, parameters).fetchall()
        ]
        for row in rows:
            profile = json.loads(row["threshold_json"])
            if row.get("evidence_as_of") is None:
                row["evidence_as_of"] = profile.get("rti_evaluated_at")
            if row.get("kalshi_microstructure_captured_at") is None:
                row["kalshi_microstructure_captured_at"] = profile.get(
                    "kalshi_microstructure_captured_at"
                )
        return rows
    finally:
        connection.close()


def load_feature_rows(db_path: Path) -> list[dict[str, Any]]:
    """Load all feature rows without exposing settlement/P&L columns."""
    return _load_feature_rows(db_path, after_close_time=None)


def load_feature_rows_after(
    db_path: Path, after_close_time: float,
) -> list[dict[str, Any]]:
    """Load only rows strictly after a frozen outcome-blind close boundary."""
    return _load_feature_rows(
        db_path, after_close_time=float(after_close_time),
    )


def _complete_window_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    feature_builder: Callable[[Mapping[str, Any]], Mapping[str, Any]] = feature_vector,
    source_schema: str = feature_v1.SOURCE_SCHEMA,
) -> dict[float, list[dict[str, Any]]]:
    grouped: dict[float, list[dict[str, Any]]] = defaultdict(list)
    for raw in rows:
        row = dict(raw)
        if row.get("kalshi_microstructure_schema_version") != source_schema:
            continue
        close = _num(row.get("close_time"))
        if close is not None:
            grouped[close].append(row)
    output = {}
    for close, window_rows in sorted(grouped.items()):
        if len(window_rows) != len(EXPECTED_ASSETS):
            continue
        if {str(row.get("asset") or "").upper() for row in window_rows} != (
            EXPECTED_ASSETS
        ):
            continue
        if any(not feature_builder(row).get("available") for row in window_rows):
            continue
        output[close] = window_rows
    return output


def prepare_unlabeled_examples(
    rows: Sequence[Mapping[str, Any]],
    design: Mapping[str, Any],
    cohort: str,
) -> tuple[list[dict[str, Any]], tuple[float, ...]]:
    if cohort not in COHORT_ASSETS:
        raise ValueError("unsupported_cohort")
    runtime = _feature_runtime(design)
    feature_names = tuple(runtime.FEATURE_NAMES)
    coverage = build_report(
        rows, source_schema=str(design.get("source_schema") or ""),
    )
    # Readiness must use the same design-bound feature availability and
    # timestamp scope as preregistration and the outcome-blind readiness
    # monitor.  Raw source-schema history can contain pre-design gaps that are
    # intentionally visible for diagnostics but cannot poison this frozen
    # design forever.  The model feature builder still fails closed on every
    # eligible row and timestamp.
    coverage.update(runtime.model_feature_window_coverage(rows))
    readiness = build_readiness(design, coverage)
    if not readiness["cohorts"][cohort]["ready_for_locked_freeze"]:
        raise ValueError(f"cohort_not_ready:{cohort}")
    minimum = int(design["cohorts"][cohort]["minimum_complete_close_windows"])
    complete = _complete_window_rows(
        rows,
        feature_builder=runtime.feature_vector,
        source_schema=str(design.get("source_schema") or ""),
    )
    selected_windows = tuple(sorted(complete)[:minimum])
    if len(selected_windows) != minimum:
        raise ValueError("minimum_complete_window_selection_failed")
    assets = COHORT_ASSETS[cohort]
    examples: list[dict[str, Any]] = []
    for close in selected_windows:
        for row in complete[close]:
            asset = str(row.get("asset") or "").upper()
            if asset not in assets:
                continue
            vector = runtime.feature_vector(row)
            if not vector.get("available"):
                raise ValueError(
                    f"feature_unavailable:{close}:{asset}:{vector.get('error')}"
                )
            examples.append({
                "id": int(row["id"]),
                "close_time": close,
                "asset": asset,
                "side": str(row.get("side") or "").upper(),
                "cohort": cohort,
                "features": list(vector["features"]),
                "feature_names": list(feature_names),
                "market_yes_probability": float(vector["market_yes_probability"]),
                "yes_ask_cents": float(vector["yes_ask_cents"]),
                "no_ask_cents": float(vector["no_ask_cents"]),
                "yes_depth_contracts": float(vector["yes_depth_contracts"]),
                "no_depth_contracts": float(vector["no_depth_contracts"]),
                "yes_depth_available": bool(vector["yes_depth_available"]),
                "no_depth_available": bool(vector["no_depth_available"]),
                "spread_cents": float(vector["spread_cents"]),
            })
    expected_rows = minimum * len(assets)
    if len(examples) != expected_rows:
        raise ValueError("cohort_rows_incomplete")
    return examples, selected_windows


def load_labels(db_path: Path, row_ids: Sequence[int]) -> dict[int, int]:
    """Read outcomes only for explicitly supplied row ids, in bounded chunks."""
    requested = tuple(sorted({int(value) for value in row_ids}))
    labels: dict[int, int] = {}
    connection = _connect_read_only(db_path)
    try:
        for start in range(0, len(requested), 400):
            chunk = requested[start:start + 400]
            placeholders = ",".join("?" for _ in chunk)
            query = (
                "SELECT id,official_result FROM strategy_bot_decisions "
                f"WHERE id IN ({placeholders})"
            )
            for row in connection.execute(query, chunk).fetchall():
                result = str(row["official_result"] or "").upper()
                if result in {"YES", "NO"}:
                    labels[int(row["id"])] = int(result == "YES")
        return labels
    finally:
        connection.close()


def labels_available(db_path: Path, row_ids: Sequence[int]) -> bool:
    """Check completeness without selecting outcome values."""
    requested = tuple(sorted({int(value) for value in row_ids}))
    found = 0
    connection = _connect_read_only(db_path)
    try:
        for start in range(0, len(requested), 400):
            chunk = requested[start:start + 400]
            placeholders = ",".join("?" for _ in chunk)
            query = (
                "SELECT COUNT(*) FROM strategy_bot_decisions "
                f"WHERE id IN ({placeholders}) "
                "AND official_result IN ('YES','NO')"
            )
            found += int(connection.execute(query, chunk).fetchone()[0])
        return found == len(requested)
    finally:
        connection.close()


def _attach_labels(
    examples: Sequence[Mapping[str, Any]], labels: Mapping[int, int],
) -> list[dict[str, Any]]:
    missing = [int(row["id"]) for row in examples if int(row["id"]) not in labels]
    if missing:
        raise ValueError(f"labels_incomplete:{len(missing)}")
    return [{**dict(row), "label_yes": int(labels[int(row["id"])])} for row in examples]


def _window_weights(examples: Sequence[Mapping[str, Any]]) -> np.ndarray:
    counts = Counter(float(row["close_time"]) for row in examples)
    return np.asarray(
        [1.0 / counts[float(row["close_time"])] for row in examples],
        dtype=np.float64,
    )


def window_weight_diagnostics(
    examples: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if not examples:
        raise ValueError("window_weight_examples_empty")
    counts = Counter(float(row["close_time"]) for row in examples)
    weights = _window_weights(examples)
    totals: dict[float, float] = defaultdict(float)
    for row, weight in zip(examples, weights):
        totals[float(row["close_time"])] += float(weight)
    values = tuple(totals.values())
    verified = bool(
        len(totals) == len(counts)
        and all(abs(value - 1.0) <= 1e-12 for value in values)
    )
    return {
        "version": "q15-close-window-equal-weight-v1",
        "rows": len(examples),
        "close_windows": len(counts),
        "minimum_rows_per_close_window": min(counts.values()),
        "maximum_rows_per_close_window": max(counts.values()),
        "total_sample_weight": float(weights.sum()),
        "minimum_close_window_weight": min(values),
        "maximum_close_window_weight": max(values),
        "every_close_window_total_weight_one": verified,
    }


def _optimizer_objective_and_gradients(
    standardized: np.ndarray,
    labels: np.ndarray,
    offsets: np.ndarray,
    sample_weight: np.ndarray,
    weights: np.ndarray,
    bias: float,
    *,
    weight_total: float,
    l2: float,
    residual_logit_scale: float,
) -> tuple[float, np.ndarray, float]:
    predictions = _sigmoid_array(
        offsets
        + residual_logit_scale * (bias + standardized @ weights)
    )
    bounded = np.clip(predictions, 1e-12, 1.0 - 1e-12)
    log_loss = -float(np.sum(sample_weight * (
        labels * np.log(bounded)
        + (1.0 - labels) * np.log(1.0 - bounded)
    ))) / weight_total
    regularization = (
        l2 * float(weights @ weights) / (2.0 * weight_total)
        + l2 * float(bias * bias) / (8.0 * weight_total)
    )
    errors = (
        (predictions - labels)
        * sample_weight
        * residual_logit_scale
    )
    gradient = (
        standardized.T @ errors + l2 * weights
    ) / weight_total
    bias_gradient = (
        float(errors.sum()) + (l2 / 4.0) * bias
    ) / weight_total
    return log_loss + regularization, gradient, float(bias_gradient)


def fit_residual_model(
    examples: Sequence[Mapping[str, Any]], config: Mapping[str, Any],
) -> dict[str, Any]:
    if config.get("window_equal_weighting") is not True:
        raise ValueError("window_equal_weighting_required")
    weighting = window_weight_diagnostics(examples)
    if weighting["every_close_window_total_weight_one"] is not True:
        raise ValueError("window_equal_weighting_verification_failed")
    matrix = np.asarray([row["features"] for row in examples], dtype=np.float64)
    labels = np.asarray([row["label_yes"] for row in examples], dtype=np.float64)
    market = np.asarray(
        [row["market_yes_probability"] for row in examples], dtype=np.float64,
    )
    sample_weight = _window_weights(examples)
    weight_total = float(sample_weight.sum()) or 1.0
    means = np.average(matrix, axis=0, weights=sample_weight)
    variance = np.average((matrix - means) ** 2, axis=0, weights=sample_weight)
    stds = np.sqrt(variance)
    min_std = float(config["standardization_min_std"])
    safe_stds = np.where(stds > min_std, stds, 1.0)
    standardized = (matrix - means) / safe_stds
    standardized[:, stds <= min_std] = 0.0
    standardized = np.clip(
        standardized,
        -float(config["standardization_z_clip"]),
        float(config["standardization_z_clip"]),
    )
    weights = np.zeros(matrix.shape[1], dtype=np.float64)
    bias = 0.0
    offsets = np.asarray([_logit(value) for value in market])
    l2 = float(config["model_l2"])
    learning_rate = float(config["model_learning_rate"])
    scale = float(config["residual_logit_scale"])
    iterations = int(config["model_iterations"])
    initial_objective, _, _ = _optimizer_objective_and_gradients(
        standardized,
        labels,
        offsets,
        sample_weight,
        weights,
        bias,
        weight_total=weight_total,
        l2=l2,
        residual_logit_scale=scale,
    )
    for _ in range(iterations):
        _, gradient, bias_gradient = _optimizer_objective_and_gradients(
            standardized,
            labels,
            offsets,
            sample_weight,
            weights,
            bias,
            weight_total=weight_total,
            l2=l2,
            residual_logit_scale=scale,
        )
        weights -= learning_rate * gradient
        bias -= learning_rate * bias_gradient
    final_objective, final_gradient, final_bias_gradient = (
        _optimizer_objective_and_gradients(
            standardized,
            labels,
            offsets,
            sample_weight,
            weights,
            bias,
            weight_total=weight_total,
            l2=l2,
            residual_logit_scale=scale,
        )
    )
    final_max_abs_gradient = max(
        float(np.max(np.abs(final_gradient))) if len(final_gradient) else 0.0,
        abs(float(final_bias_gradient)),
    )
    objective_improvement = initial_objective - final_objective
    optimizer_values = (
        initial_objective,
        final_objective,
        objective_improvement,
        final_max_abs_gradient,
        *weights.tolist(),
        bias,
    )
    optimizer_finite = all(math.isfinite(float(value)) for value in optimizer_values)
    final_not_worse = bool(
        optimizer_finite and final_objective <= initial_objective + 1e-12
    )
    optimizer_verified = bool(optimizer_finite and final_not_worse)
    if not optimizer_verified:
        raise ValueError("optimizer_numerical_integrity_failed")
    feature_names = tuple(
        examples[0].get("feature_names") or FEATURE_NAMES
    ) if examples else tuple(FEATURE_NAMES)
    if len(feature_names) != matrix.shape[1]:
        raise ValueError("fit_feature_names_width_mismatch")
    return {
        "means": means.tolist(),
        "stds": stds.tolist(),
        "weights": weights.tolist(),
        "bias": float(bias),
        "window_weighting": weighting,
        "optimizer": {
            "version": "q15-fixed-residual-gradient-descent-audit-v1",
            "iterations": iterations,
            "learning_rate": learning_rate,
            "model_l2": l2,
            "residual_logit_scale": scale,
            "initial_regularized_objective": initial_objective,
            "final_regularized_objective": final_objective,
            "regularized_objective_improvement": objective_improvement,
            "final_max_abs_gradient": final_max_abs_gradient,
            "all_values_finite": optimizer_finite,
            "final_objective_not_worse": final_not_worse,
            "numerical_integrity_verified": optimizer_verified,
        },
        "inactive_near_zero_variance_features": [
            feature_names[index]
            for index, std in enumerate(stds)
            if std <= min_std
        ],
    }


def predict_probabilities(
    model: Mapping[str, Any],
    examples: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
) -> tuple[list[float], list[dict[str, Any]]]:
    means = np.asarray(model["means"], dtype=np.float64)
    stds = np.asarray(model["stds"], dtype=np.float64)
    weights = np.asarray(model["weights"], dtype=np.float64)
    min_std = float(config["standardization_min_std"])
    z_clip = float(config["standardization_z_clip"])
    max_abs_allowed = float(config["out_of_distribution_max_abs_z"])
    scale = float(config["residual_logit_scale"])
    probabilities: list[float] = []
    diagnostics: list[dict[str, Any]] = []
    for row in examples:
        values = np.asarray(row["features"], dtype=np.float64)
        safe_stds = np.where(stds > min_std, stds, 1.0)
        z = (values - means) / safe_stds
        z[stds <= min_std] = 0.0
        max_abs_z = float(np.max(np.abs(z))) if len(z) else 0.0
        market = float(row["market_yes_probability"])
        out_of_distribution = max_abs_z > max_abs_allowed
        if out_of_distribution:
            probability = market
        else:
            residual = float(model["bias"]) + float(
                np.clip(z, -z_clip, z_clip) @ weights
            )
            probability = float(_sigmoid_array(np.asarray([
                _logit(market) + scale * residual
            ]))[0])
        probabilities.append(_clip(probability, 0.01, 0.99))
        diagnostics.append({
            "max_abs_z_preclip": max_abs_z,
            "out_of_distribution": out_of_distribution,
            "market_fallback_used": out_of_distribution,
        })
    return probabilities, diagnostics


def _paired_close_window_bootstrap(
    examples: Sequence[Mapping[str, Any]],
    probabilities: Sequence[float],
    bootstrap_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Paired model-minus-market uncertainty with close windows as clusters.

    All assets sharing a Kalshi close are resampled together.  This prevents
    the six non-BTC assets in one crypto-wide move from masquerading as six
    independent observations.  The seed and resample count are separately
    preregistered before V11 may read any label.
    """
    if len(examples) != len(probabilities) or not examples:
        raise ValueError("paired_bootstrap_length_mismatch")
    resamples = int(bootstrap_config.get("resamples") or 0)
    confidence = float(bootstrap_config.get("confidence_level") or 0.0)
    seed = int(bootstrap_config.get("random_seed") or 0)
    if resamples < 1000:
        raise ValueError("paired_bootstrap_resamples_too_small")
    if not 0.5 < confidence < 1.0:
        raise ValueError("paired_bootstrap_confidence_invalid")
    clustered: dict[float, list[tuple[float, float]]] = defaultdict(list)
    for row, raw_probability in zip(examples, probabilities):
        close_time = _num(row.get("close_time"))
        label = int(row["label_yes"])
        if close_time is None or label not in {0, 1}:
            raise ValueError("paired_bootstrap_evidence_invalid")
        model = _clip(float(raw_probability), 1e-6, 1.0 - 1e-6)
        market = _clip(
            float(row["market_yes_probability"]), 1e-6, 1.0 - 1e-6,
        )
        model_brier = (model - label) ** 2
        market_brier = (market - label) ** 2
        model_log = -(
            label * math.log(model) + (1 - label) * math.log(1.0 - model)
        )
        market_log = -(
            label * math.log(market) + (1 - label) * math.log(1.0 - market)
        )
        clustered[float(close_time)].append((
            model_brier - market_brier,
            model_log - market_log,
        ))
    close_times = tuple(sorted(clustered))
    if not close_times:
        raise ValueError("paired_bootstrap_windows_empty")
    window_deltas = np.asarray([
        np.mean(np.asarray(clustered[close_time], dtype=np.float64), axis=0)
        for close_time in close_times
    ], dtype=np.float64)
    if not np.isfinite(window_deltas).all():
        raise ValueError("paired_bootstrap_nonfinite_delta")
    rng = np.random.default_rng(seed)
    indexes = rng.integers(
        0,
        len(close_times),
        size=(resamples, len(close_times)),
        endpoint=False,
    )
    bootstrap_means = window_deltas[indexes].mean(axis=1)
    alpha = 1.0 - confidence

    def _summary(column: int) -> dict[str, float]:
        values = bootstrap_means[:, column]
        return {
            "observed_mean_delta": float(window_deltas[:, column].mean()),
            "two_sided_lower": float(np.quantile(values, alpha / 2.0)),
            "two_sided_upper": float(np.quantile(values, 1.0 - alpha / 2.0)),
            "one_sided_upper": float(np.quantile(values, confidence)),
            "bootstrap_probability_delta_below_zero": float(
                np.mean(values < 0.0)
            ),
        }

    return {
        "version": str(bootstrap_config.get("version") or ""),
        "cluster_key": "close_time",
        "close_windows": len(close_times),
        "rows": len(examples),
        "resamples": resamples,
        "confidence_level": confidence,
        "random_seed": seed,
        "same_close_assets_resampled_together": True,
        "loss_delta_direction": "MODEL_MINUS_MARKET",
        "brier_delta": _summary(0),
        "log_loss_delta": _summary(1),
    }


def _proper_scores(
    examples: Sequence[Mapping[str, Any]],
    probabilities: Sequence[float],
    *,
    bootstrap_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not examples:
        raise ValueError("proper_score_examples_empty")
    labels = [int(row["label_yes"]) for row in examples]
    model = [_clip(value, 1e-6, 1.0 - 1e-6) for value in probabilities]
    market = [
        _clip(float(row["market_yes_probability"]), 1e-6, 1.0 - 1e-6)
        for row in examples
    ]
    def brier(values: Sequence[float]) -> float:
        return sum((value - label) ** 2 for value, label in zip(values, labels)) / len(labels)
    def log_loss(values: Sequence[float]) -> float:
        return -sum(
            label * math.log(value) + (1 - label) * math.log(1.0 - value)
            for value, label in zip(values, labels)
        ) / len(labels)
    model_brier = brier(model)
    model_log_loss = log_loss(model)
    market_brier = brier(market)
    market_log_loss = log_loss(market)
    correct = sum(
        (probability >= 0.5) == bool(label)
        for probability, label in zip(model, labels)
    )
    market_correct = sum(
        (probability >= 0.5) == bool(label)
        for probability, label in zip(market, labels)
    )
    wilson_low, wilson_high = _wilson(correct, len(labels))
    paired_bootstrap = (
        None
        if bootstrap_config is None
        else _paired_close_window_bootstrap(
            examples, probabilities, bootstrap_config,
        )
    )
    return {
        "rows": len(examples),
        "close_windows": len({float(row["close_time"]) for row in examples}),
        "correct": correct,
        "accuracy": correct / len(labels),
        "wilson_95_low": wilson_low,
        "wilson_95_high": wilson_high,
        "market_correct": market_correct,
        "market_accuracy": market_correct / len(labels),
        "brier_score": model_brier,
        "log_loss": model_log_loss,
        "market_brier_score": market_brier,
        "market_log_loss": market_log_loss,
        "brier_skill_vs_market": (
            None if market_brier <= 0.0 else 1.0 - model_brier / market_brier
        ),
        "log_loss_delta_vs_market": model_log_loss - market_log_loss,
        "paired_close_window_bootstrap": paired_bootstrap,
    }


def blend_residual_probability(
    market_probability: float,
    base_residual_probability: float,
    factor: float,
) -> float:
    """Blend in logit space; factor zero is exactly the market prior."""
    market = _clip(float(market_probability), 0.01, 0.99)
    base = _clip(float(base_residual_probability), 0.01, 0.99)
    trust = float(factor)
    if trust == 0.0:
        return market
    value = _logit(market) + trust * (_logit(base) - _logit(market))
    return _clip(float(_sigmoid_array(np.asarray([value]))[0]), 0.01, 0.99)


def _inner_chronological_oof_predictions(
    examples: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    selection: Mapping[str, Any],
    cohort: str,
) -> tuple[list[Mapping[str, Any]], list[float], dict[str, Any]]:
    """Create OOF predictions using only earlier windows in one train period."""
    if cohort not in COHORT_ASSETS:
        raise ValueError("residual_trust_unsupported_cohort")
    rule = dict(dict(selection["inner_folds"])[cohort])
    initial = int(rule["initial_train_windows"])
    block = int(rule["validation_block_windows"])
    windows = tuple(sorted({float(row["close_time"]) for row in examples}))
    if len(windows) <= initial or (len(windows) - initial) % block != 0:
        raise ValueError("residual_trust_inner_fold_geometry_invalid")
    expected_assets = COHORT_ASSETS[cohort]
    for close_time in windows:
        assets = {
            str(row.get("asset") or "").upper()
            for row in examples if float(row["close_time"]) == close_time
        }
        if assets != expected_assets:
            raise ValueError("residual_trust_inner_same_close_asset_leakage")
    rows: list[Mapping[str, Any]] = []
    probabilities: list[float] = []
    ood_rows = 0
    blocks = []
    cursor = initial
    while cursor < len(windows):
        train_times = set(windows[:cursor])
        validation_times = set(windows[cursor:cursor + block])
        if max(train_times) >= min(validation_times):
            raise ValueError("residual_trust_inner_chronology_violation")
        train = [
            row for row in examples
            if float(row["close_time"]) in train_times
        ]
        validation = [
            row for row in examples
            if float(row["close_time"]) in validation_times
        ]
        model = fit_residual_model(train, config)
        predicted, diagnostics = predict_probabilities(
            model, validation, config,
        )
        ood_rows += sum(bool(item["out_of_distribution"]) for item in diagnostics)
        rows.extend(validation)
        probabilities.extend(predicted)
        blocks.append({
            "train_close_windows": len(train_times),
            "validation_close_windows": len(validation_times),
            "train_last_close_time": max(train_times),
            "validation_first_close_time": min(validation_times),
        })
        cursor += block
    return rows, probabilities, {
        "initial_train_windows": initial,
        "validation_block_windows": block,
        "oof_close_windows": len({float(row["close_time"]) for row in rows}),
        "oof_rows": len(rows),
        "out_of_distribution_rows": ood_rows,
        "blocks": blocks,
    }


def select_residual_trust_factor(
    training_examples: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    protocol: Mapping[str, Any],
    cohort: str,
) -> dict[str, Any]:
    """Select trust inside training data; validation/test labels stay unseen."""
    selection = protocol.get("residual_trust_selection")
    if not isinstance(selection, Mapping):
        raise ValueError("residual_trust_protocol_missing")
    inner_rows, base_probabilities, inner = (
        _inner_chronological_oof_predictions(
            training_examples, config, selection, cohort,
        )
    )
    bootstrap = selection.get("bootstrap")
    if not isinstance(bootstrap, Mapping):
        raise ValueError("residual_trust_bootstrap_missing")
    candidates = []
    for raw_factor in selection["fixed_factor_grid"]:
        factor = float(raw_factor)
        blended = [
            blend_residual_probability(
                float(row["market_yes_probability"]), probability, factor,
            )
            for row, probability in zip(inner_rows, base_probabilities)
        ]
        metrics = _proper_scores(
            inner_rows, blended, bootstrap_config=bootstrap,
        )
        brier_delta = float(metrics["brier_score"]) - float(
            metrics["market_brier_score"]
        )
        log_delta = float(metrics["log_loss"]) - float(
            metrics["market_log_loss"]
        )
        paired = metrics["paired_close_window_bootstrap"]
        eligible = bool(
            brier_delta < 0.0
            and log_delta < 0.0
            and float(paired["brier_delta"]["one_sided_upper"]) < 0.0
            and float(paired["log_loss_delta"]["one_sided_upper"]) < 0.0
        )
        relative_score = (
            brier_delta / max(float(metrics["market_brier_score"]), 1e-12)
            + log_delta / max(float(metrics["market_log_loss"]), 1e-12)
        ) / 2.0
        candidates.append({
            "factor": factor,
            "brier_delta_vs_market": brier_delta,
            "log_loss_delta_vs_market": log_delta,
            "brier_bootstrap_one_sided_upper": float(
                paired["brier_delta"]["one_sided_upper"]
            ),
            "log_loss_bootstrap_one_sided_upper": float(
                paired["log_loss_delta"]["one_sided_upper"]
            ),
            "mean_relative_proper_score_delta": relative_score,
            "eligible": eligible,
        })
    eligible = [row for row in candidates if row["eligible"]]
    fallback = float(selection["fallback_factor"])
    selected = min(
        eligible,
        key=lambda row: (
            float(row["mean_relative_proper_score_delta"]),
            float(row["factor"]),
        ),
        default=next(
            row for row in candidates if float(row["factor"]) == fallback
        ),
    )
    return {
        "architecture": selection["architecture"],
        "selected_factor": float(selected["factor"]),
        "market_fallback_selected": float(selected["factor"]) == 0.0,
        "selection_used_only_inner_training_oof_labels": True,
        "outer_validation_labels_used_for_selection": False,
        "calibration_labels_used_for_selection": False,
        "untouched_test_labels_used_for_selection": False,
        "inner_oof": inner,
        "candidates": candidates,
    }


def apply_residual_trust(
    examples: Sequence[Mapping[str, Any]],
    base_probabilities: Sequence[float],
    selection: Mapping[str, Any],
) -> list[float]:
    if len(examples) != len(base_probabilities):
        raise ValueError("residual_trust_prediction_geometry_mismatch")
    factor = float(selection["selected_factor"])
    return [
        blend_residual_probability(
            float(row["market_yes_probability"]), probability, factor,
        )
        for row, probability in zip(examples, base_probabilities)
    ]


def _wilson(correct: int, count: int) -> tuple[float | None, float | None]:
    if count <= 0:
        return None, None
    z = 1.959963984540054
    rate = correct / count
    denominator = 1.0 + z * z / count
    centre = rate + z * z / (2.0 * count)
    margin = z * math.sqrt(
        rate * (1.0 - rate) / count + z * z / (4.0 * count * count)
    )
    return (
        max(0.0, (centre - margin) / denominator),
        min(1.0, (centre + margin) / denominator),
    )


def calibration_gate(
    examples: Sequence[Mapping[str, Any]],
    probabilities: Sequence[float],
    *,
    bootstrap_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    windows = tuple(sorted({float(row["close_time"]) for row in examples}))
    split = max(1, len(windows) // 2)
    halves = (set(windows[:split]), set(windows[split:]))
    overall = _proper_scores(
        examples,
        probabilities,
        bootstrap_config=bootstrap_config,
    )
    half_metrics = []
    for close_times in halves:
        indexes = [
            index for index, row in enumerate(examples)
            if float(row["close_time"]) in close_times
        ]
        half_metrics.append(_proper_scores(
            [examples[index] for index in indexes],
            [probabilities[index] for index in indexes],
            bootstrap_config=bootstrap_config,
        ))
    strict_overall = (
        overall["brier_score"] < overall["market_brier_score"]
        and overall["log_loss"] < overall["market_log_loss"]
    )
    halves_not_worse = all(
        metrics["brier_score"] <= metrics["market_brier_score"]
        and metrics["log_loss"] <= metrics["market_log_loss"]
        for metrics in half_metrics
    )
    bootstrap = overall.get("paired_close_window_bootstrap")
    brier_floor = float(
        (bootstrap_config or {}).get("minimum_mean_brier_improvement") or 0.0
    )
    log_loss_floor = float(
        (bootstrap_config or {}).get("minimum_mean_log_loss_improvement")
        or 0.0
    )
    uncertainty_met = bool(
        bootstrap is None
        or (
            float(bootstrap["brier_delta"]["one_sided_upper"])
            <= -brier_floor
            and float(bootstrap["log_loss_delta"]["one_sided_upper"])
            <= -log_loss_floor
        )
    )
    return {
        "met": bool(strict_overall and halves_not_worse and uncertainty_met),
        "overall": overall,
        "chronological_halves": half_metrics,
        "strict_overall_improvement": strict_overall,
        "both_halves_not_worse": halves_not_worse,
        "paired_close_window_uncertainty_met": uncertainty_met,
        "minimum_mean_brier_improvement": brier_floor,
        "minimum_mean_log_loss_improvement": log_loss_floor,
    }


def expanding_walk_forward_gate(
    labeled_pretest: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    protocol: Mapping[str, Any],
    cohort: str,
) -> dict[str, Any]:
    """Evaluate fixed temporary models on strictly later contiguous blocks."""
    close_times = tuple(sorted({
        float(row["close_time"]) for row in labeled_pretest
    }))
    folds = expanding_walk_forward_folds(close_times, protocol, cohort)
    fold_reports: list[dict[str, Any]] = []
    aggregate_examples: list[Mapping[str, Any]] = []
    aggregate_probabilities: list[float] = []
    bootstrap_config = protocol.get("paired_close_window_bootstrap")
    if not isinstance(bootstrap_config, Mapping):
        raise ValueError("walk_forward_bootstrap_missing")
    expected_assets = COHORT_ASSETS[cohort]
    for fold in folds:
        train_times = set(fold["train"])
        validation_times = set(fold["validation"])
        train = [
            row for row in labeled_pretest
            if float(row["close_time"]) in train_times
        ]
        validation = [
            row for row in labeled_pretest
            if float(row["close_time"]) in validation_times
        ]
        expected_train_rows = len(train_times) * len(expected_assets)
        expected_validation_rows = len(validation_times) * len(expected_assets)
        if (
            len(train) != expected_train_rows
            or len(validation) != expected_validation_rows
        ):
            raise ValueError("walk_forward_cohort_rows_incomplete")
        for close_time in (*train_times, *validation_times):
            assets = {
                str(row.get("asset") or "").upper()
                for row in labeled_pretest
                if float(row["close_time"]) == close_time
            }
            if assets != expected_assets:
                raise ValueError("walk_forward_same_close_asset_leakage")
        trust_selection = None
        if protocol.get("residual_trust_selection") is not None:
            trust_selection = select_residual_trust_factor(
                train, config, protocol, cohort,
            )
        model = fit_residual_model(train, config)
        base_probabilities, diagnostics = predict_probabilities(
            model, validation, config,
        )
        probabilities = (
            base_probabilities
            if trust_selection is None
            else apply_residual_trust(
                validation, base_probabilities, trust_selection,
            )
        )
        metrics = _proper_scores(
            validation,
            probabilities,
            bootstrap_config=bootstrap_config,
        )
        fold_reports.append({
            "fold": int(fold["fold"]),
            "train_close_windows": len(train_times),
            "train_first_close_time": min(train_times),
            "train_last_close_time": max(train_times),
            "validation_close_windows": len(validation_times),
            "validation_first_close_time": min(validation_times),
            "validation_last_close_time": max(validation_times),
            "validation_out_of_distribution_rows": sum(
                bool(row["out_of_distribution"]) for row in diagnostics
            ),
            **({} if trust_selection is None else {
                "residual_trust_selection": trust_selection,
                "selected_residual_trust_factor": trust_selection[
                    "selected_factor"
                ],
            }),
            "metrics": metrics,
            "brier_not_worse_than_market": (
                metrics["brier_score"] <= metrics["market_brier_score"]
            ),
            "log_loss_not_worse_than_market": (
                metrics["log_loss"] <= metrics["market_log_loss"]
            ),
        })
        aggregate_examples.extend(validation)
        aggregate_probabilities.extend(probabilities)
    aggregate = _proper_scores(
        aggregate_examples,
        aggregate_probabilities,
        bootstrap_config=bootstrap_config,
    )
    aggregate_brier_improved = (
        aggregate["brier_score"] < aggregate["market_brier_score"]
    )
    aggregate_log_loss_improved = (
        aggregate["log_loss"] < aggregate["market_log_loss"]
    )
    every_fold_brier_not_worse = all(
        bool(row["brier_not_worse_than_market"]) for row in fold_reports
    )
    every_fold_log_loss_not_worse = all(
        bool(row["log_loss_not_worse_than_market"]) for row in fold_reports
    )
    paired_bootstrap = aggregate["paired_close_window_bootstrap"]
    brier_floor = float(
        bootstrap_config["minimum_mean_brier_improvement"]
    )
    log_loss_floor = float(
        bootstrap_config["minimum_mean_log_loss_improvement"]
    )
    aggregate_brier_uncertainty_met = bool(
        float(paired_bootstrap["brier_delta"]["one_sided_upper"])
        <= -brier_floor
    )
    aggregate_log_loss_uncertainty_met = bool(
        float(paired_bootstrap["log_loss_delta"]["one_sided_upper"])
        <= -log_loss_floor
    )
    return {
        "met": bool(
            aggregate_brier_improved
            and aggregate_log_loss_improved
            and every_fold_brier_not_worse
            and every_fold_log_loss_not_worse
            and aggregate_brier_uncertainty_met
            and aggregate_log_loss_uncertainty_met
        ),
        "temporary_model_fits": len(fold_reports),
        "temporary_models_are_deployable": False,
        "untouched_test_rows_used": 0,
        "aggregate": aggregate,
        "aggregate_brier_improved": aggregate_brier_improved,
        "aggregate_log_loss_improved": aggregate_log_loss_improved,
        "every_fold_brier_not_worse": every_fold_brier_not_worse,
        "every_fold_log_loss_not_worse": every_fold_log_loss_not_worse,
        "aggregate_brier_bootstrap_upper_strictly_negative": bool(
            float(paired_bootstrap["brier_delta"]["one_sided_upper"]) < 0.0
        ),
        "aggregate_log_loss_bootstrap_upper_strictly_negative": bool(
            float(paired_bootstrap["log_loss_delta"]["one_sided_upper"]) < 0.0
        ),
        "aggregate_brier_bootstrap_upper_clears_fixed_effect_floor": (
            aggregate_brier_uncertainty_met
        ),
        "aggregate_log_loss_bootstrap_upper_clears_fixed_effect_floor": (
            aggregate_log_loss_uncertainty_met
        ),
        "minimum_mean_brier_improvement": brier_floor,
        "minimum_mean_log_loss_improvement": log_loss_floor,
        "folds": fold_reports,
    }


def _entry(
    probability_yes: float,
    row: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> dict[str, Any] | None:
    spread = float(row["spread_cents"])
    if spread > float(policy["maximum_spread_cents"]):
        return None
    candidates = []
    for side, win_probability, ask_key, depth_key, availability_key in (
        ("YES", probability_yes, "yes_ask_cents", "yes_depth_contracts", "yes_depth_available"),
        ("NO", 1.0 - probability_yes, "no_ask_cents", "no_depth_contracts", "no_depth_available"),
    ):
        if not bool(row[availability_key]):
            continue
        ask = float(row[ask_key])
        depth = float(row[depth_key])
        if ask > float(policy["maximum_ask_cents"]):
            continue
        if depth < float(policy["minimum_displayed_depth_contracts"]):
            continue
        contracts = int(policy["simulation_contracts"])
        execution = rti_simulated_execution(
            ask,
            contracts,
            float(policy["slippage_cents_per_contract"]),
        )
        if execution is None:
            continue
        fee = float(execution["fee_cents_per_contract"])
        fill = float(execution["simulated_fill_cents"])
        ev = win_probability * 100.0 - fill - fee
        if ev >= float(policy["minimum_expected_value_cents_after_costs"]):
            candidates.append((ev, side, ask, fill, fee))
    if not candidates:
        return None
    ev, side, ask, fill, fee = max(candidates)
    return {
        "side": side,
        "ask_cents": ask,
        "simulated_fill_cents": fill,
        "fee_cents": fee,
        "ev_cents": ev,
        "fee_schedule_version": KALSHI_Q15_FEE_SCHEDULE_VERSION,
        "execution_cost_model_version": RTI_EXECUTION_COST_MODEL_VERSION,
    }


def _pnl_cents_per_contract(
    *, correct: bool, simulated_fill_cents: float, fee_cents: float,
) -> float:
    gross = 100.0 - simulated_fill_cents if correct else -simulated_fill_cents
    return gross - fee_cents


def _maximum_drawdown_cents(pnls: Sequence[float]) -> float:
    cumulative = 0.0
    peak = 0.0
    maximum = 0.0
    for raw in pnls:
        cumulative += float(raw)
        peak = max(peak, cumulative)
        maximum = max(maximum, peak - cumulative)
    return maximum


def _probability_side_evidence(
    row: Mapping[str, Any], probability_yes: float,
) -> tuple[str, float, float, bool]:
    if probability_yes >= 0.5:
        return (
            "YES",
            float(row["yes_ask_cents"]),
            float(row["yes_depth_contracts"]),
            bool(row["yes_depth_available"]),
        )
    return (
        "NO",
        float(row["no_ask_cents"]),
        float(row["no_depth_contracts"]),
        bool(row["no_depth_available"]),
    )


def _entry_rejection_reasons(
    row: Mapping[str, Any], probability_yes: float, policy: Mapping[str, Any],
) -> tuple[str, ...]:
    side, ask, depth, available = _probability_side_evidence(
        row, probability_yes,
    )
    win_probability = probability_yes if side == "YES" else 1.0 - probability_yes
    reasons: list[str] = []
    if float(row["spread_cents"]) > float(policy["maximum_spread_cents"]):
        reasons.append("SPREAD_ABOVE_MAXIMUM")
    if not available:
        reasons.append("SIDE_DEPTH_UNAVAILABLE")
    if ask > float(policy["maximum_ask_cents"]):
        reasons.append("ASK_ABOVE_MAXIMUM")
    if depth < float(policy["minimum_displayed_depth_contracts"]):
        reasons.append("DISPLAYED_DEPTH_BELOW_MINIMUM")
    execution = rti_simulated_execution(
        ask,
        int(policy["simulation_contracts"]),
        float(policy["slippage_cents_per_contract"]),
    )
    if execution is None:
        reasons.append("SIMULATED_EXECUTION_UNAVAILABLE")
    else:
        ev = (
            win_probability * 100.0
            - float(execution["simulated_fill_cents"])
            - float(execution["fee_cents_per_contract"])
        )
        if ev < float(policy["minimum_expected_value_cents_after_costs"]):
            reasons.append("EXPECTED_VALUE_BELOW_MINIMUM")
    return tuple(sorted(set(reasons or ("OTHER_SIDE_DOMINATED",))))


def _trade_path_metrics(
    examples: Sequence[Mapping[str, Any]],
    probabilities: Sequence[float],
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    if len(examples) != len(probabilities):
        raise ValueError("trade_metrics_length_mismatch")
    ordered = sorted(
        zip(examples, probabilities),
        key=lambda pair: (
            float(pair[0]["close_time"]), int(pair[0].get("id") or 0),
        ),
    )
    picks: list[dict[str, Any]] = []
    rejected_correct = 0
    rejected_rows = 0
    counterfactual_rows: list[dict[str, Any]] = []
    rejection_reason_counts: Counter[str] = Counter()
    contracts = int(policy["simulation_contracts"])
    for row, raw_probability in ordered:
        probability = float(raw_probability)
        entry = _entry(probability, row, policy)
        official_side = "YES" if int(row["label_yes"]) else "NO"
        if entry is not None:
            correct = entry["side"] == official_side
            pnl = _pnl_cents_per_contract(
                correct=correct,
                simulated_fill_cents=float(entry["simulated_fill_cents"]),
                fee_cents=float(entry["fee_cents"]),
            )
            picks.append({**entry, "correct": correct, "pnl_cents": pnl})
            continue
        rejected_rows += 1
        side, ask, depth, available = _probability_side_evidence(
            row, probability,
        )
        predicted_correct = side == official_side
        rejected_correct += int(predicted_correct)
        rejection_reason_counts.update(
            _entry_rejection_reasons(row, probability, policy)
        )
        # This is deliberately narrower than a fill assumption: only a stored
        # side quote with enough displayed size receives hypothetical P/L.
        if not available or depth < contracts:
            continue
        execution = rti_simulated_execution(
            ask, contracts, float(policy["slippage_cents_per_contract"]),
        )
        if execution is None:
            continue
        pnl = _pnl_cents_per_contract(
            correct=predicted_correct,
            simulated_fill_cents=float(execution["simulated_fill_cents"]),
            fee_cents=float(execution["fee_cents_per_contract"]),
        )
        counterfactual_rows.append({
            "correct": predicted_correct,
            "pnl_cents": pnl,
        })
    pick_correct = sum(bool(row["correct"]) for row in picks)
    pick_pnls = [float(row["pnl_cents"]) for row in picks]
    counterfactual_correct = sum(
        bool(row["correct"]) for row in counterfactual_rows
    )
    counterfactual_pnls = [
        float(row["pnl_cents"]) for row in counterfactual_rows
    ]
    close_windows = len({float(row["close_time"]) for row in examples})
    pick_wilson = _wilson(pick_correct, len(picks))
    rejected_wilson = _wilson(rejected_correct, rejected_rows)
    counterfactual_wilson = _wilson(
        counterfactual_correct, len(counterfactual_rows)
    )
    return {
        "picks": len(picks),
        "pick_frequency_per_close_window": len(picks) / max(1, close_windows),
        "pick_correct": pick_correct,
        "pick_accuracy": None if not picks else pick_correct / len(picks),
        "ten_contract_net_pnl_dollars": (
            sum(pick_pnls) * contracts / 100.0
        ),
        "fee_slippage_adjusted_ev_cents_per_trade": (
            None if not picks else sum(pick_pnls) / len(picks)
        ),
        "pick_wilson_95_low": pick_wilson[0],
        "pick_wilson_95_high": pick_wilson[1],
        "max_drawdown_dollars_at_sim_size": (
            _maximum_drawdown_cents(pick_pnls) * contracts / 100.0
        ),
        "rejected_counterfactual": {
            "paper_counterfactual_only": True,
            "never_claimed_as_fill": True,
            "rejected_rows": rejected_rows,
            "predicted_side_correct": rejected_correct,
            "predicted_side_accuracy": (
                None if not rejected_rows else rejected_correct / rejected_rows
            ),
            "predicted_side_wilson_95_low": rejected_wilson[0],
            "predicted_side_wilson_95_high": rejected_wilson[1],
            "quote_executable_rows": len(counterfactual_rows),
            "non_executable_rows": rejected_rows - len(counterfactual_rows),
            "quote_executable_correct": counterfactual_correct,
            "quote_executable_accuracy": (
                None
                if not counterfactual_rows
                else counterfactual_correct / len(counterfactual_rows)
            ),
            "quote_executable_wilson_95_low": counterfactual_wilson[0],
            "quote_executable_wilson_95_high": counterfactual_wilson[1],
            "ten_contract_net_pnl_dollars": (
                sum(counterfactual_pnls) * contracts / 100.0
            ),
            "fee_slippage_adjusted_ev_cents_per_trade": (
                None
                if not counterfactual_rows
                else sum(counterfactual_pnls) / len(counterfactual_rows)
            ),
            "max_drawdown_dollars_at_sim_size": (
                _maximum_drawdown_cents(counterfactual_pnls)
                * contracts
                / 100.0
            ),
            "rejection_reason_counts": dict(sorted(rejection_reason_counts.items())),
            "fee_schedule_version": KALSHI_Q15_FEE_SCHEDULE_VERSION,
            "execution_cost_model_version": RTI_EXECUTION_COST_MODEL_VERSION,
        },
    }


def _feature_values(row: Mapping[str, Any]) -> dict[str, float]:
    names = tuple(row.get("feature_names") or ())
    values = tuple(row.get("features") or ())
    if len(names) != len(values) or len(set(names)) != len(names):
        raise ValueError("reporting_feature_vector_invalid")
    output = {str(name): float(value) for name, value in zip(names, values)}
    if not all(math.isfinite(value) for value in output.values()):
        raise ValueError("reporting_feature_vector_nonfinite")
    return output


def _fixed_bin_label(value: float, bins: Sequence[Mapping[str, Any]]) -> str:
    for raw_bin in bins:
        minimum = float(raw_bin["minimum_inclusive"])
        maximum_raw = raw_bin.get("maximum_exclusive")
        maximum = None if maximum_raw is None else float(maximum_raw)
        if value >= minimum and (maximum is None or value < maximum):
            return str(raw_bin["label"])
    raise ValueError("reporting_value_outside_fixed_bins")


def _reporting_labels(
    row: Mapping[str, Any], protocol: Mapping[str, Any],
) -> dict[str, str]:
    features = _feature_values(row)
    dimensions = dict(protocol["dimensions"])
    side_definition = dict(dimensions["rti_side"])
    if side_definition.get("source_feature"):
        side_sign = (
            1.0
            if features[str(side_definition["source_feature"])] >= 0.0
            else -1.0
        )
    elif side_definition.get("source") == "stored_point_in_time_side":
        stored_side = str(row.get("side") or "").upper()
        if stored_side not in {"YES", "NO"}:
            raise ValueError("reporting_stored_side_invalid")
        side_sign = 1.0 if stored_side == "YES" else -1.0
    else:
        raise ValueError("reporting_side_source_invalid")
    distance = abs(features["yes_signed_distance_bps"])
    realized_volatility = math.expm1(
        features["log1p_realized_volatility_bps"]
    )
    median_momentum = features["cross_asset_median_momentum_60s"]
    breadth = features["cross_asset_breadth_signed_60s"]
    target_momentum = side_sign * median_momentum
    target_breadth = side_sign * breadth
    threshold = float(
        dimensions["market_regime"]["broad_projected_breadth_threshold"]
    )
    if target_momentum > 0.0 and target_breadth >= threshold:
        regime = "BROAD_ALIGNED"
    elif target_momentum > 0.0:
        regime = "THIN_OR_ISOLATED_ALIGNED"
    elif target_momentum < 0.0 and target_breadth <= -threshold:
        regime = "BROAD_OPPOSED"
    else:
        regime = "MIXED_OR_FLAT"
    return {
        "asset": str(row.get("asset") or "").upper(),
        "rti_side": "YES" if side_sign > 0.0 else "NO",
        "absolute_distance_tier": _fixed_bin_label(
            distance, dimensions["absolute_distance_tier"]["bins"]
        ),
        "realized_volatility_tier": _fixed_bin_label(
            realized_volatility,
            dimensions["realized_volatility_tier"]["bins"],
        ),
        "market_regime": regime,
    }


def fixed_subgroup_report(
    examples: Sequence[Mapping[str, Any]],
    probabilities: Sequence[float],
    diagnostics: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any],
    reporting_protocol: Mapping[str, Any],
) -> dict[str, Any]:
    if not (len(examples) == len(probabilities) == len(diagnostics)):
        raise ValueError("reporting_rows_length_mismatch")
    expected_sha256 = {
        feature_v11.REPORTING_PROTOCOL_ID: (
            EXPECTED_V11_REPORTING_PROTOCOL_SHA256
        ),
        feature_v13.REPORTING_PROTOCOL_ID: (
            EXPECTED_V13_REPORTING_PROTOCOL_SHA256
        ),
        feature_v14.REPORTING_PROTOCOL_ID: (
            EXPECTED_V14_REPORTING_PROTOCOL_SHA256
        ),
    }.get(str(reporting_protocol.get("protocol_id") or ""))
    if expected_sha256 is None or (
        reporting_protocol_fingerprint(reporting_protocol) != expected_sha256
    ):
        raise ValueError("reporting_protocol_fingerprint_mismatch")
    labels = [
        _reporting_labels(row, reporting_protocol) for row in examples
    ]
    dimensions = dict(reporting_protocol["dimensions"])
    report_dimensions: dict[str, Any] = {}
    for dimension, definition in dimensions.items():
        if "categories" in definition:
            categories = tuple(str(value) for value in definition["categories"])
        elif "bins" in definition:
            categories = tuple(str(value["label"]) for value in definition["bins"])
        else:
            categories = tuple(
                str(value) for value in definition["categories_in_priority_order"]
            )
        observed: dict[str, Any] = {}
        unobserved: list[str] = []
        partition_rows = 0
        for category in categories:
            indexes = [
                index for index, row_labels in enumerate(labels)
                if row_labels[dimension] == category
            ]
            if not indexes:
                unobserved.append(category)
                continue
            partition_rows += len(indexes)
            observed[category] = test_metrics(
                [examples[index] for index in indexes],
                [probabilities[index] for index in indexes],
                [diagnostics[index] for index in indexes],
                policy,
            )
        if partition_rows != len(examples):
            raise ValueError(f"reporting_dimension_partition_invalid:{dimension}")
        report_dimensions[dimension] = {
            "fixed_categories": list(categories),
            "unobserved_categories": unobserved,
            "observed_slices": observed,
            "partition_rows": partition_rows,
        }
    cohorts = sorted({str(row.get("cohort") or "") for row in examples})
    if len(cohorts) != 1:
        raise ValueError("reporting_cohort_mixing_forbidden")
    return {
        "protocol_id": reporting_protocol["protocol_id"],
        "protocol_sha256": reporting_protocol_fingerprint(reporting_protocol),
        "report_only": True,
        "changes_deployment_gate": False,
        "cohort": cohorts[0],
        "rows": len(examples),
        "close_windows": len({float(row["close_time"]) for row in examples}),
        "dimensions": report_dimensions,
    }


def _calibration_source_metrics(
    examples: Sequence[Mapping[str, Any]],
    probabilities: Sequence[float],
    protocol: Mapping[str, Any],
) -> dict[str, Any]:
    if len(examples) != len(probabilities) or not examples:
        raise ValueError("calibration_reporting_probability_geometry_invalid")
    labels = [int(row["label_yes"]) for row in examples]
    values = [float(value) for value in probabilities]
    if any(value < 0.0 or value > 1.0 or not math.isfinite(value) for value in values):
        raise ValueError("calibration_reporting_probability_invalid")
    bins = list(protocol["probability_bins"])
    assignments: dict[str, list[int]] = {
        str(raw["label"]): [] for raw in bins
    }
    for index, probability in enumerate(values):
        matched = None
        for raw in bins:
            low = float(raw["minimum_inclusive"])
            if "maximum_exclusive" in raw:
                inside = low <= probability < float(raw["maximum_exclusive"])
            else:
                inside = low <= probability <= float(raw["maximum_inclusive"])
            if inside:
                matched = str(raw["label"])
                break
        if matched is None:
            raise ValueError("calibration_reporting_probability_unbinned")
        assignments[matched].append(index)
    observed_rate = sum(labels) / len(labels)
    reliability = 0.0
    resolution = 0.0
    expected_calibration_error = 0.0
    maximum_calibration_error = 0.0
    observed_bins: dict[str, Any] = {}
    empty_bins: list[str] = []
    for raw in bins:
        label = str(raw["label"])
        indexes = assignments[label]
        if not indexes:
            empty_bins.append(label)
            observed_bins[label] = {
                "rows": 0,
                "mean_probability": None,
                "observed_yes_rate": None,
                "absolute_calibration_gap": None,
            }
            continue
        mean_probability = sum(values[index] for index in indexes) / len(indexes)
        bin_observed = sum(labels[index] for index in indexes) / len(indexes)
        gap = abs(mean_probability - bin_observed)
        weight = len(indexes) / len(labels)
        reliability += weight * (mean_probability - bin_observed) ** 2
        resolution += weight * (bin_observed - observed_rate) ** 2
        expected_calibration_error += weight * gap
        maximum_calibration_error = max(maximum_calibration_error, gap)
        observed_bins[label] = {
            "rows": len(indexes),
            "mean_probability": mean_probability,
            "observed_yes_rate": bin_observed,
            "absolute_calibration_gap": gap,
        }
    mean_probability = sum(values) / len(values)
    return {
        "rows": len(values),
        "mean_probability": mean_probability,
        "observed_yes_rate": observed_rate,
        "calibration_bias_probability_minus_observed": (
            mean_probability - observed_rate
        ),
        "expected_calibration_error": expected_calibration_error,
        "maximum_calibration_error": maximum_calibration_error,
        "binned_reliability": reliability,
        "binned_resolution": resolution,
        "outcome_uncertainty": observed_rate * (1.0 - observed_rate),
        "bins": observed_bins,
        "empty_bins": empty_bins,
    }


def fixed_calibration_report(
    examples: Sequence[Mapping[str, Any]],
    probabilities: Sequence[float],
    protocol: Mapping[str, Any],
) -> dict[str, Any]:
    model = _calibration_source_metrics(examples, probabilities, protocol)
    market = _calibration_source_metrics(
        examples,
        [float(row["market_yes_probability"]) for row in examples],
        protocol,
    )
    cohorts = sorted({str(row.get("cohort") or "") for row in examples})
    if len(cohorts) != 1:
        raise ValueError("calibration_reporting_cohort_mixing_forbidden")
    reported_for = list(protocol.get("reported_for") or ())
    if len(reported_for) != 2 or reported_for[1] != "kalshi_market_prior":
        raise ValueError("calibration_reporting_sources_invalid")
    model_source = str(reported_for[0])
    return {
        "protocol_id": protocol["protocol_id"],
        "protocol_sha256": calibration_reporting_protocol_fingerprint(protocol),
        "report_only": True,
        "changes_deployment_gate": False,
        "historical_calibration_cannot_promote": True,
        "cohort": cohorts[0],
        "rows": len(examples),
        "close_windows": len({float(row["close_time"]) for row in examples}),
        "fixed_probability_bins": [
            str(raw["label"]) for raw in protocol["probability_bins"]
        ],
        "sources": {
            model_source: model,
            "kalshi_market_prior": market,
        },
        "comparisons": {
            "model_minus_market_expected_calibration_error": (
                model["expected_calibration_error"]
                - market["expected_calibration_error"]
            ),
            "model_minus_market_maximum_calibration_error": (
                model["maximum_calibration_error"]
                - market["maximum_calibration_error"]
            ),
            "model_minus_market_absolute_calibration_bias": (
                abs(model["calibration_bias_probability_minus_observed"])
                - abs(market["calibration_bias_probability_minus_observed"])
            ),
        },
        "minimum_rows_for_promotional_claim": 30,
        "promotional_claim_allowed": False,
        "outcome_labels_read_only_after_all_prior_gates": True,
        "paper_only": True,
        "notification_eligible": False,
        "real_trading_allowed": False,
    }


def validate_fixed_calibration_metrics(
    metrics: Mapping[str, Any],
    examples: Sequence[Mapping[str, Any]],
    probabilities: Sequence[float],
    protocol: Mapping[str, Any],
) -> None:
    report = metrics.get("fixed_calibration_reporting")
    if not isinstance(report, Mapping):
        raise ValueError("fixed_calibration_reporting_missing")
    expected = fixed_calibration_report(examples, probabilities, protocol)
    if dict(report) != expected:
        raise ValueError("fixed_calibration_reporting_mismatch")


def fixed_selective_value_curve(
    examples: Sequence[Mapping[str, Any]],
    probabilities: Sequence[float],
    entry_policy: Mapping[str, Any],
    protocol: Mapping[str, Any],
) -> dict[str, Any]:
    if len(examples) != len(probabilities) or not examples:
        raise ValueError("selective_value_curve_geometry_invalid")
    cohorts = sorted({str(row.get("cohort") or "") for row in examples})
    if len(cohorts) != 1:
        raise ValueError("selective_value_curve_cohort_mixing_forbidden")
    ordered = sorted(
        zip(examples, probabilities),
        key=lambda pair: (
            float(pair[0]["close_time"]), int(pair[0].get("id") or 0),
        ),
    )
    thresholds = [
        float(value)
        for value in protocol["fixed_expected_value_thresholds_cents"]
    ]
    rows_by_threshold: dict[str, Any] = {}
    prior_selected: set[int] | None = None
    monotonic = True
    frozen_key = ""
    required = tuple(protocol["required_metrics_per_threshold"])
    for threshold in thresholds:
        policy = dict(entry_policy)
        policy["minimum_expected_value_cents_after_costs"] = threshold
        trade = _trade_path_metrics(examples, probabilities, policy)
        selected_ids = [
            int(row["id"])
            for row, probability in ordered
            if _entry(float(probability), row, policy) is not None
        ]
        selected_set = set(selected_ids)
        if prior_selected is not None and not selected_set.issubset(prior_selected):
            monotonic = False
        prior_selected = selected_set
        label = f"ev_ge_{int(threshold)}c"
        if threshold == float(protocol["frozen_entry_policy_threshold_cents"]):
            frozen_key = label
        row_metrics = {
            "decision_rows": len(examples),
            "picks": int(trade["picks"]),
            "decision_row_coverage": trade["picks"] / len(examples),
            "pick_frequency_per_close_window": trade[
                "pick_frequency_per_close_window"
            ],
            "pick_correct": int(trade["pick_correct"]),
            "pick_accuracy": trade["pick_accuracy"],
            "pick_wilson_95_low": trade["pick_wilson_95_low"],
            "pick_wilson_95_high": trade["pick_wilson_95_high"],
            "ten_contract_net_pnl_dollars": trade[
                "ten_contract_net_pnl_dollars"
            ],
            "fee_slippage_adjusted_ev_cents_per_trade": trade[
                "fee_slippage_adjusted_ev_cents_per_trade"
            ],
            "max_drawdown_dollars_at_sim_size": trade[
                "max_drawdown_dollars_at_sim_size"
            ],
            "selected_row_ids_sha256": _canonical_sha256(selected_ids),
        }
        if tuple(row_metrics) != required:
            raise ValueError("selective_value_curve_metric_order_mismatch")
        rows_by_threshold[label] = row_metrics
    if not monotonic:
        raise ValueError("selective_value_curve_pick_sets_not_monotonic")
    if not frozen_key:
        raise ValueError("selective_value_curve_frozen_threshold_missing")
    return {
        "protocol_id": protocol["protocol_id"],
        "protocol_sha256": selective_value_curve_protocol_fingerprint(protocol),
        "cohort": cohorts[0],
        "rows": len(examples),
        "close_windows": len({float(row["close_time"]) for row in examples}),
        "threshold_order": [
            f"ev_ge_{int(threshold)}c" for threshold in thresholds
        ],
        "frozen_entry_policy_threshold": frozen_key,
        "thresholds": rows_by_threshold,
        "higher_threshold_pick_set_subset_verified": True,
        "higher_threshold_pick_count_nonincreasing_verified": True,
        "same_quote_cost_and_depth_rules_verified": True,
        "counterfactual_paper_curve_only": True,
        "never_claimed_as_historical_fills": True,
        "historical_curve_cannot_promote": True,
        "threshold_selection_from_test_forbidden": True,
        "future_change_requires_new_prospective_challenger": True,
        "report_only": True,
        "changes_deployment_gate": False,
        "paper_only": True,
        "notification_eligible": False,
        "real_trading_allowed": False,
    }


def validate_fixed_selective_value_curve(
    metrics: Mapping[str, Any],
    examples: Sequence[Mapping[str, Any]],
    probabilities: Sequence[float],
    entry_policy: Mapping[str, Any],
    protocol: Mapping[str, Any],
) -> None:
    report = metrics.get("fixed_selective_value_curve")
    if not isinstance(report, Mapping):
        raise ValueError("fixed_selective_value_curve_missing")
    expected = fixed_selective_value_curve(
        examples, probabilities, entry_policy, protocol,
    )
    if dict(report) != expected:
        raise ValueError("fixed_selective_value_curve_mismatch")


def validate_fixed_subgroup_metrics(
    metrics: Mapping[str, Any],
    examples: Sequence[Mapping[str, Any]],
    reporting_protocol: Mapping[str, Any],
) -> None:
    report = metrics.get("fixed_subgroup_reporting")
    if not isinstance(report, Mapping):
        raise ValueError("fixed_subgroup_reporting_missing")
    if report.get("protocol_id") != reporting_protocol.get("protocol_id"):
        raise ValueError("fixed_subgroup_reporting_protocol_id_mismatch")
    if report.get("protocol_sha256") != reporting_protocol_fingerprint(
        reporting_protocol
    ):
        raise ValueError("fixed_subgroup_reporting_protocol_sha_mismatch")
    if report.get("report_only") is not True or (
        report.get("changes_deployment_gate") is not False
    ):
        raise ValueError("fixed_subgroup_reporting_safety_guard_missing")
    cohorts = {str(row.get("cohort") or "") for row in examples}
    if len(cohorts) != 1 or report.get("cohort") != next(iter(cohorts)):
        raise ValueError("fixed_subgroup_reporting_cohort_mismatch")
    expected_windows = len({float(row["close_time"]) for row in examples})
    if (
        report.get("rows") != len(examples)
        or report.get("close_windows") != expected_windows
    ):
        raise ValueError("fixed_subgroup_reporting_geometry_mismatch")
    report_dimensions = report.get("dimensions")
    protocol_dimensions = reporting_protocol.get("dimensions")
    if not isinstance(report_dimensions, Mapping) or not isinstance(
        protocol_dimensions, Mapping
    ) or tuple(report_dimensions) != tuple(protocol_dimensions):
        raise ValueError("fixed_subgroup_reporting_dimensions_mismatch")
    required_metrics = dict(
        reporting_protocol["required_metrics_per_observed_slice"]
    )
    for dimension, definition in protocol_dimensions.items():
        details = report_dimensions.get(dimension)
        if not isinstance(details, Mapping):
            raise ValueError("fixed_subgroup_reporting_dimension_missing")
        if "categories" in definition:
            categories = [str(value) for value in definition["categories"]]
        elif "bins" in definition:
            categories = [str(value["label"]) for value in definition["bins"]]
        else:
            categories = [
                str(value)
                for value in definition["categories_in_priority_order"]
            ]
        observed = details.get("observed_slices")
        unobserved = details.get("unobserved_categories")
        if not isinstance(observed, Mapping) or not isinstance(unobserved, list):
            raise ValueError("fixed_subgroup_reporting_category_shape_invalid")
        if details.get("fixed_categories") != categories:
            raise ValueError("fixed_subgroup_reporting_categories_mismatch")
        if set(observed).intersection(unobserved) or (
            set(observed).union(unobserved) != set(categories)
        ):
            raise ValueError("fixed_subgroup_reporting_category_partition_invalid")
        if details.get("partition_rows") != len(examples) or sum(
            int(slice_metrics.get("rows") or 0)
            for slice_metrics in observed.values()
            if isinstance(slice_metrics, Mapping)
        ) != len(examples):
            raise ValueError("fixed_subgroup_reporting_row_partition_invalid")
        for slice_metrics in observed.values():
            if not isinstance(slice_metrics, Mapping):
                raise ValueError("fixed_subgroup_reporting_slice_invalid")
            if any(
                key not in slice_metrics
                for key in required_metrics["all_rows"]
            ) or any(
                key not in slice_metrics
                for key in required_metrics["accepted_picks"]
            ):
                raise ValueError("fixed_subgroup_reporting_metric_missing")
            rejected = slice_metrics.get("rejected_counterfactual")
            if not isinstance(rejected, Mapping) or any(
                key not in rejected
                for key in required_metrics["rejected_counterfactual"]
            ):
                raise ValueError("fixed_subgroup_reporting_counterfactual_missing")
            if rejected.get("paper_counterfactual_only") is not True or (
                rejected.get("never_claimed_as_fill") is not True
            ):
                raise ValueError(
                    "fixed_subgroup_reporting_counterfactual_guard_missing"
                )


def test_metrics(
    examples: Sequence[Mapping[str, Any]],
    probabilities: Sequence[float],
    diagnostics: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any],
    *,
    bootstrap_config: Mapping[str, Any] | None = None,
    reporting_protocol: Mapping[str, Any] | None = None,
    calibration_reporting_protocol: Mapping[str, Any] | None = None,
    selective_value_curve_protocol: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    proper = _proper_scores(
        examples,
        probabilities,
        bootstrap_config=bootstrap_config,
    )
    trade_metrics = _trade_path_metrics(examples, probabilities, policy)
    result = {
        **proper,
        "out_of_distribution_rows": sum(
            bool(row.get("out_of_distribution")) for row in diagnostics
        ),
        **trade_metrics,
        "fee_schedule_version": KALSHI_Q15_FEE_SCHEDULE_VERSION,
        "execution_cost_model_version": RTI_EXECUTION_COST_MODEL_VERSION,
    }
    if reporting_protocol is not None:
        result["fixed_subgroup_reporting"] = fixed_subgroup_report(
            examples,
            probabilities,
            diagnostics,
            policy,
            reporting_protocol,
        )
    if calibration_reporting_protocol is not None:
        result["fixed_calibration_reporting"] = fixed_calibration_report(
            examples,
            probabilities,
            calibration_reporting_protocol,
        )
    if selective_value_curve_protocol is not None:
        result["fixed_selective_value_curve"] = fixed_selective_value_curve(
            examples,
            probabilities,
            policy,
            selective_value_curve_protocol,
        )
    return result


TEST_STATE_VERSION = "q15-rti-untouched-test-state-v2"
FINAL_TEST_STATUSES = frozenset({
    "PASSED_UNTOUCHED_TEST_PAPER_ARTIFACT_ONLY",
    "REJECTED_ON_UNTOUCHED_TEST",
})


def _canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def test_state_fingerprint(payload: Mapping[str, Any]) -> str:
    canonical = dict(payload)
    canonical.pop("test_state_sha256", None)
    return _canonical_sha256(canonical)


def _sealed_test_state(payload: Mapping[str, Any]) -> dict[str, Any]:
    sealed = dict(payload)
    sealed.pop("test_state_sha256", None)
    sealed["test_state_version"] = TEST_STATE_VERSION
    sealed["test_state_sha256"] = test_state_fingerprint(sealed)
    return sealed


def load_test_state(path: Path) -> dict[str, Any]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, Mapping):
        raise ValueError("untouched_test_state_root_not_object")
    state = dict(decoded)
    if state.get("test_state_version") != TEST_STATE_VERSION:
        raise ValueError("untouched_test_state_version_mismatch")
    expected = str(state.get("test_state_sha256") or "")
    if not expected or expected != test_state_fingerprint(state):
        raise ValueError("untouched_test_state_fingerprint_mismatch")
    return state


def reserve_test_score(
    path: Path, payload: Mapping[str, Any],
) -> dict[str, Any]:
    sealed = _sealed_test_state(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(sealed, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return sealed


def _update_test_state(
    path: Path, payload: Mapping[str, Any],
) -> dict[str, Any]:
    sealed = _sealed_test_state(payload)
    atomic_write_json(path, sealed)
    return sealed


def _model_fingerprint(model: Mapping[str, Any]) -> str:
    return _canonical_sha256(dict(model))


def _training_data_fingerprint(
    examples: Sequence[Mapping[str, Any]],
) -> str:
    return _canonical_sha256([
        [int(row["id"]), float(row["close_time"]), row["asset"], row["features"]]
        for row in examples
    ])


def _validate_existing_test_state(
    state: Mapping[str, Any],
    *,
    design: Mapping[str, Any],
    design_sha256: str,
    cohort: str,
    folds: Mapping[str, Sequence[float]],
    data_sha256: str,
    model_sha256: str,
    protocol_binding: Mapping[str, Any],
) -> None:
    """Prove an existing reservation belongs to this exact frozen run.

    Validation happens before either the untouched-test availability checker or
    label reader is called.  An ambiguous reservation is therefore never
    silently converted into a second score attempt.
    """
    if state.get("design_id") != design.get("design_id"):
        raise ValueError("untouched_test_state_design_id_mismatch")
    if state.get("design_sha256") != design_sha256:
        raise ValueError("untouched_test_state_design_sha_mismatch")
    if state.get("cohort") != cohort:
        raise ValueError("untouched_test_state_cohort_mismatch")
    if state.get("fee_schedule_version") != KALSHI_Q15_FEE_SCHEDULE_VERSION:
        raise ValueError("untouched_test_state_fee_schedule_mismatch")
    if state.get("execution_cost_model_version") != (
        RTI_EXECUTION_COST_MODEL_VERSION
    ):
        raise ValueError("untouched_test_state_cost_model_mismatch")
    for key, expected in protocol_binding.items():
        if state.get(key) != expected:
            raise ValueError(f"untouched_test_state_protocol_mismatch:{key}")
    protocol_keys = {
        "walk_forward_protocol_id",
        "walk_forward_protocol_sha256",
        "walk_forward_required_before_untouched_test",
        "reporting_protocol_id",
        "reporting_protocol_sha256",
        "fixed_subgroup_reporting_required",
        "calibration_reporting_protocol_id",
        "calibration_reporting_protocol_sha256",
        "fixed_calibration_reporting_required",
        "selective_value_curve_protocol_id",
        "selective_value_curve_protocol_sha256",
        "fixed_selective_value_curve_required",
    }
    if not protocol_binding and any(key in state for key in protocol_keys):
        raise ValueError("untouched_test_state_unexpected_protocol_binding")
    test_windows = tuple(float(value) for value in folds["test"])
    expected_geometry = {
        "test_first_close_time": min(test_windows),
        "test_last_close_time": max(test_windows),
        "test_close_windows": len(test_windows),
    }
    if any(state.get(key) != expected for key, expected in expected_geometry.items()):
        raise ValueError("untouched_test_state_fold_geometry_mismatch")
    if state.get("data_sha256") != data_sha256:
        raise ValueError("untouched_test_state_data_sha_mismatch")
    if state.get("model_sha256") != model_sha256:
        raise ValueError("untouched_test_state_model_sha_mismatch")
    boundary = _num(state.get("prospective_after_close_time"))
    if boundary is None or boundary < max(test_windows):
        raise ValueError("untouched_test_state_prospective_boundary_invalid")
    status = str(state.get("status") or "")
    if status == "TEST_SCORE_RESERVED":
        if "scored_at" in state or "test_metrics" in state or "test_gate" in state:
            raise ValueError("untouched_test_state_ambiguous_reservation_invalid")
        return
    if status not in FINAL_TEST_STATUSES:
        raise ValueError("untouched_test_state_status_invalid")
    if not str(state.get("scored_at") or ""):
        raise ValueError("untouched_test_state_scored_at_missing")
    if state.get("untouched_test_labels_read_once") is not True:
        raise ValueError("untouched_test_state_once_guard_missing")
    metrics = state.get("test_metrics")
    gate = state.get("test_gate")
    if not isinstance(metrics, Mapping) or not isinstance(gate, Mapping):
        raise ValueError("untouched_test_state_final_evidence_missing")
    if state.get("test_metrics_sha256") != _canonical_sha256(dict(metrics)):
        raise ValueError("untouched_test_state_metrics_fingerprint_mismatch")
    passed = status == "PASSED_UNTOUCHED_TEST_PAPER_ARTIFACT_ONLY"
    if gate.get("met") is not passed:
        raise ValueError("untouched_test_state_gate_status_mismatch")


def _build_paper_artifact(
    *,
    runtime: Any,
    design: Mapping[str, Any],
    design_sha256: str,
    cohort: str,
    data_sha256: str,
    model: Mapping[str, Any],
    config: Mapping[str, Any],
    protocol_binding: Mapping[str, Any],
    walk_forward: Mapping[str, Any] | None,
    test_state: Mapping[str, Any],
) -> dict[str, Any]:
    if test_state.get("status") != (
        "PASSED_UNTOUCHED_TEST_PAPER_ARTIFACT_ONLY"
    ):
        raise ValueError("paper_artifact_requires_passed_test_state")
    artifact = {
        "model_version": (
            f"rti-microstructure-paper-{design['design_id']}-{data_sha256[:12]}"
        ),
        "model_family": runtime.MODEL_FAMILY,
        "feature_schema_version": runtime.FEATURE_SCHEMA_VERSION,
        "feature_names": list(runtime.FEATURE_NAMES),
        "design_id": design["design_id"],
        "design_sha256": design_sha256,
        "created_at": str(test_state["scored_at"]),
        "data_sha256": data_sha256,
        "cohort": cohort,
        "paper_only": True,
        "notification_eligible": False,
        "real_trading_allowed": False,
        "automatic_refit": False,
        "automatic_promotion": False,
        "historical_credit_allowed": False,
        "prospective_after_close_time": float(
            test_state["prospective_after_close_time"]
        ),
        "same_close_fold_isolation": True,
        "window_equal_weighting_verified": bool(
            model["window_weighting"]["every_close_window_total_weight_one"]
        ),
        "optimizer_numerical_integrity_verified": bool(
            model["optimizer"]["numerical_integrity_verified"]
        ),
        **dict(protocol_binding),
        "walk_forward_gate_passed": (
            None if walk_forward is None else bool(walk_forward["met"])
        ),
        "test_scored_once": True,
        "test_state_version": TEST_STATE_VERSION,
        "test_state_sha256": str(test_state["test_state_sha256"]),
        "test_metrics_sha256": str(test_state["test_metrics_sha256"]),
        "untouched_test_status": str(test_state["status"]),
        "model": dict(model),
        "fixed_training_config": dict(config),
        "entry_policy": {
            **dict(design["entry_policy"]),
            "fee_schedule_version": KALSHI_Q15_FEE_SCHEDULE_VERSION,
            "execution_cost_model_version": RTI_EXECUTION_COST_MODEL_VERSION,
        },
    }
    artifact["artifact_sha256"] = _canonical_sha256(artifact)
    return artifact


def run_locked_freeze(
    *,
    design: Mapping[str, Any],
    coverage: Mapping[str, Any],
    feature_rows: Sequence[Mapping[str, Any]],
    cohort: str,
    prospective_boundary: float,
    read_labels: Callable[[Sequence[int]], Mapping[int, int]],
    labels_are_available: Callable[[Sequence[int]], bool],
    confirm_score_untouched_test: bool,
    test_state_path: Path,
    walk_forward_protocol: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    validate_design(design)
    feature_rows = sanitize_feature_rows(feature_rows)
    runtime = _feature_runtime(design)
    fingerprint = design_fingerprint(design)
    protocol = (
        walk_forward_protocol
        if walk_forward_protocol is not None
        else walk_forward_protocol_for_design(design)
    )
    if protocol is not None:
        validate_walk_forward_protocol(protocol, design)
    protocol_binding = (
        {}
        if protocol is None
        else {
            "walk_forward_protocol_id": protocol["protocol_id"],
            "walk_forward_protocol_sha256": (
                evaluation_protocol_fingerprint(protocol)
            ),
            "walk_forward_required_before_untouched_test": True,
        }
    )
    reporting_protocol = reporting_protocol_for_design(design)
    reporting_binding = (
        {}
        if reporting_protocol is None
        else {
            "reporting_protocol_id": reporting_protocol["protocol_id"],
            "reporting_protocol_sha256": (
                reporting_protocol_fingerprint(reporting_protocol)
            ),
            "fixed_subgroup_reporting_required": True,
        }
    )
    calibration_reporting_protocol = (
        calibration_reporting_protocol_for_design(design)
    )
    calibration_reporting_binding = (
        {}
        if calibration_reporting_protocol is None
        else {
            "calibration_reporting_protocol_id": (
                calibration_reporting_protocol["protocol_id"]
            ),
            "calibration_reporting_protocol_sha256": (
                calibration_reporting_protocol_fingerprint(
                    calibration_reporting_protocol
                )
            ),
            "fixed_calibration_reporting_required": True,
        }
    )
    selective_value_curve_protocol = (
        selective_value_curve_protocol_for_design(design)
    )
    selective_value_curve_binding = (
        {}
        if selective_value_curve_protocol is None
        else {
            "selective_value_curve_protocol_id": (
                selective_value_curve_protocol["protocol_id"]
            ),
            "selective_value_curve_protocol_sha256": (
                selective_value_curve_protocol_fingerprint(
                    selective_value_curve_protocol
                )
            ),
            "fixed_selective_value_curve_required": True,
        }
    )
    protocol_binding = {
        **protocol_binding,
        **reporting_binding,
        **calibration_reporting_binding,
        **selective_value_curve_binding,
    }
    readiness = build_readiness(design, coverage)
    cohort_ready = bool(readiness.get("cohorts", {}).get(cohort, {}).get(
        "ready_for_locked_freeze"
    ))
    base_report: dict[str, Any] = {
        "audit_version": "q15-rti-microstructure-locked-freeze-v2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "design_id": design["design_id"],
        "design_sha256": fingerprint,
        "cohort": cohort,
        "paper_only": True,
        "automatic_promotion": False,
        "fee_schedule_version": KALSHI_Q15_FEE_SCHEDULE_VERSION,
        "execution_cost_model_version": RTI_EXECUTION_COST_MODEL_VERSION,
        "readiness": readiness,
        "outcome_labels_read": False,
        "feature_profile_allow_list_enforced": True,
        "raw_threshold_json_selected_by_cli_loader": False,
        "allowed_feature_profile_key_count": len(SAFE_FEATURE_PROFILE_KEYS),
        "untouched_test_labels_read": False,
        "model_fit_performed": False,
        "final_model_fit_performed": False,
        "artifact_emitted": False,
        **protocol_binding,
    }
    if not cohort_ready:
        return None, {**base_report, "status": "WAITING_FOR_COMPLETE_WINDOWS"}

    examples, selected_windows = prepare_unlabeled_examples(
        feature_rows, design, cohort
    )
    folds = chronological_folds(selected_windows, design)
    fold_sets = {name: set(values) for name, values in folds.items()}
    split = {
        name: [
            row for row in examples if float(row["close_time"]) in close_times
        ]
        for name, close_times in fold_sets.items()
    }
    train_cal = split["train"] + split["calibration"]
    train_cal_ids = [int(row["id"]) for row in train_cal]
    if not labels_are_available(train_cal_ids):
        return None, {
            **base_report,
            "status": "WAITING_FOR_TRAIN_CALIBRATION_SETTLEMENTS",
            "selected_close_windows": len(selected_windows),
        }
    train_cal_labels = dict(read_labels(train_cal_ids))
    labeled_train = _attach_labels(split["train"], train_cal_labels)
    labeled_calibration = _attach_labels(split["calibration"], train_cal_labels)
    config = dict(design["fixed_training_config"])
    walk_forward = None
    if protocol is not None:
        walk_forward = expanding_walk_forward_gate(
            [*labeled_train, *labeled_calibration],
            config,
            protocol,
            cohort,
        )
        if not walk_forward["met"]:
            return None, {
                **base_report,
                "status": "REJECTED_ON_WALK_FORWARD_GATE",
                "outcome_labels_read": True,
                "model_fit_performed": True,
                "final_model_fit_performed": False,
                "selected_close_windows": len(selected_windows),
                "walk_forward_gate": walk_forward,
            }
    calibration_trust_selection = None
    if protocol is not None and protocol.get("residual_trust_selection") is not None:
        calibration_trust_selection = select_residual_trust_factor(
            labeled_train, config, protocol, cohort,
        )
    model = fit_residual_model(labeled_train, config)
    calibration_base_probabilities, calibration_diagnostics = predict_probabilities(
        model, labeled_calibration, config
    )
    calibration_probabilities = (
        calibration_base_probabilities
        if calibration_trust_selection is None
        else apply_residual_trust(
            labeled_calibration,
            calibration_base_probabilities,
            calibration_trust_selection,
        )
    )
    if protocol is None:
        gate = calibration_gate(
            labeled_calibration, calibration_probabilities,
        )
    else:
        gate = calibration_gate(
            labeled_calibration,
            calibration_probabilities,
            bootstrap_config=protocol["paired_close_window_bootstrap"],
        )
    report = {
        **base_report,
        "outcome_labels_read": True,
        "model_fit_performed": True,
        "final_model_fit_performed": True,
        "selected_close_windows": len(selected_windows),
        "split_boundaries": {
            name: {
                "close_windows": len(values),
                "first_close_time": min(values),
                "last_close_time": max(values),
            }
            for name, values in folds.items()
        },
        "calibration_gate": gate,
        "calibration_out_of_distribution_rows": sum(
            bool(row["out_of_distribution"]) for row in calibration_diagnostics
        ),
        **({} if calibration_trust_selection is None else {
            "calibration_residual_trust_selection": (
                calibration_trust_selection
            ),
            "calibration_selected_residual_trust_factor": (
                calibration_trust_selection["selected_factor"]
            ),
        }),
        **({} if walk_forward is None else {
            "walk_forward_gate": walk_forward,
        }),
    }
    if not gate["met"]:
        return None, {**report, "status": "REJECTED_ON_CALIBRATION_GATE"}
    if calibration_trust_selection is not None:
        final_training = [*labeled_train, *labeled_calibration]
        final_trust_selection = select_residual_trust_factor(
            final_training, config, protocol, cohort,
        )
        model = fit_residual_model(final_training, config)
        model["residual_trust_selection"] = final_trust_selection
        report = {
            **report,
            "final_model_training_close_windows": len({
                float(row["close_time"]) for row in final_training
            }),
            "final_residual_trust_selection": final_trust_selection,
            "final_selected_residual_trust_factor": final_trust_selection[
                "selected_factor"
            ],
            "untouched_test_labels_used_for_final_factor_selection": False,
        }
    if not confirm_score_untouched_test:
        return None, {
            **report,
            "status": "CALIBRATION_PASSED_AWAITING_EXPLICIT_TEST_SCORE",
        }
    test_ids = [int(row["id"]) for row in split["test"]]
    data_hash = _training_data_fingerprint(examples)
    model_hash = _model_fingerprint(model)

    def _result_from_existing_state(
        existing_state: Mapping[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        _validate_existing_test_state(
            existing_state,
            design=design,
            design_sha256=fingerprint,
            cohort=cohort,
            folds=folds,
            data_sha256=data_hash,
            model_sha256=model_hash,
            protocol_binding=protocol_binding,
        )
        if existing_state["status"] == "TEST_SCORE_RESERVED":
            return None, {
                **report,
                "status": "UNTOUCHED_TEST_SCORE_RESERVED_AMBIGUOUS_NO_RESCORE",
                "untouched_test_labels_read": False,
                "untouched_test_labels_previously_scored": None,
                "untouched_test_scored_once": False,
                "recovered_from_finalized_test_state": False,
                "test_state_sha256": existing_state["test_state_sha256"],
            }
        stored_metrics = dict(existing_state["test_metrics"])
        stored_gate = dict(existing_state["test_gate"])
        recovered_report = {
            **report,
            "status": (
                "RECOVERED_PASSED_UNTOUCHED_TEST_PAPER_ARTIFACT_ONLY"
                if stored_gate["met"]
                else "RECOVERED_REJECTED_ON_UNTOUCHED_TEST"
            ),
            "untouched_test_labels_read": False,
            "untouched_test_labels_previously_scored": True,
            "untouched_test_scored_once": True,
            "recovered_from_finalized_test_state": True,
            "test_metrics": stored_metrics,
            "test_gate": stored_gate,
            "test_state_sha256": existing_state["test_state_sha256"],
        }
        if not stored_gate["met"]:
            return None, recovered_report
        recovered_artifact = _build_paper_artifact(
            runtime=runtime,
            design=design,
            design_sha256=fingerprint,
            cohort=cohort,
            data_sha256=data_hash,
            model=model,
            config=config,
            protocol_binding=protocol_binding,
            walk_forward=walk_forward,
            test_state=existing_state,
        )
        recovered_report["artifact_emitted"] = True
        recovered_report["artifact_sha256"] = recovered_artifact[
            "artifact_sha256"
        ]
        return recovered_artifact, recovered_report

    # This check deliberately precedes both untouched-test callbacks.  A
    # finalized state can reconstruct the artifact; an incomplete reservation
    # is ambiguous and therefore permanently refuses an automatic rescore.
    if test_state_path.exists():
        return _result_from_existing_state(load_test_state(test_state_path))
    if not labels_are_available(test_ids):
        return None, {**report, "status": "WAITING_FOR_UNTOUCHED_TEST_SETTLEMENTS"}
    boundary = _num(prospective_boundary)
    if boundary is None or boundary < max(folds["test"]):
        raise ValueError("prospective_boundary_precedes_untouched_test")
    reservation = {
        "status": "TEST_SCORE_RESERVED",
        "reserved_at": datetime.now(timezone.utc).isoformat(),
        "design_id": design["design_id"],
        "design_sha256": fingerprint,
        "cohort": cohort,
        "data_sha256": data_hash,
        "model_sha256": model_hash,
        "prospective_after_close_time": boundary,
        "test_first_close_time": min(folds["test"]),
        "test_last_close_time": max(folds["test"]),
        "test_close_windows": len(folds["test"]),
        "fee_schedule_version": KALSHI_Q15_FEE_SCHEDULE_VERSION,
        "execution_cost_model_version": RTI_EXECUTION_COST_MODEL_VERSION,
        **protocol_binding,
    }
    try:
        reservation = reserve_test_score(test_state_path, reservation)
    except FileExistsError:
        # Another process won the exclusive reservation race.  Never proceed
        # to the test reader in the losing process.
        return _result_from_existing_state(load_test_state(test_state_path))
    test_labels = dict(read_labels(test_ids))
    labeled_test = _attach_labels(split["test"], test_labels)
    test_base_probabilities, test_diagnostics = predict_probabilities(
        model, labeled_test, config
    )
    test_probabilities = (
        test_base_probabilities
        if "residual_trust_selection" not in model
        else apply_residual_trust(
            labeled_test,
            test_base_probabilities,
            model["residual_trust_selection"],
        )
    )
    metrics = test_metrics(
        labeled_test,
        test_probabilities,
        test_diagnostics,
        design["entry_policy"],
        bootstrap_config=(
            None
            if protocol is None
            else protocol["paired_close_window_bootstrap"]
        ),
        reporting_protocol=reporting_protocol,
        calibration_reporting_protocol=calibration_reporting_protocol,
        selective_value_curve_protocol=selective_value_curve_protocol,
    )
    if reporting_protocol is not None:
        validate_fixed_subgroup_metrics(
            metrics, labeled_test, reporting_protocol,
        )
    if calibration_reporting_protocol is not None:
        validate_fixed_calibration_metrics(
            metrics,
            labeled_test,
            test_probabilities,
            calibration_reporting_protocol,
        )
    if selective_value_curve_protocol is not None:
        validate_fixed_selective_value_curve(
            metrics,
            labeled_test,
            test_probabilities,
            design["entry_policy"],
            selective_value_curve_protocol,
        )
    gate_config = dict(design["untouched_test_deployment_gate"])
    test_gate = {
        "brier_improved": metrics["brier_score"] < metrics["market_brier_score"],
        "log_loss_improved": metrics["log_loss"] < metrics["market_log_loss"],
        "minimum_picks_met": metrics["picks"] >= int(gate_config["minimum_simulated_picks"]),
        "positive_pnl": metrics["ten_contract_net_pnl_dollars"] > 0.0,
    }
    if protocol is not None:
        paired_bootstrap = metrics["paired_close_window_bootstrap"]
        bootstrap_config = protocol["paired_close_window_bootstrap"]
        brier_floor = float(
            bootstrap_config["minimum_mean_brier_improvement"]
        )
        log_loss_floor = float(
            bootstrap_config["minimum_mean_log_loss_improvement"]
        )
        test_gate.update({
            "brier_bootstrap_upper_strictly_negative": (
                float(paired_bootstrap["brier_delta"]["one_sided_upper"])
                < 0.0
            ),
            "log_loss_bootstrap_upper_strictly_negative": (
                float(paired_bootstrap["log_loss_delta"]["one_sided_upper"])
                < 0.0
            ),
            "brier_bootstrap_upper_clears_fixed_effect_floor": (
                float(paired_bootstrap["brier_delta"]["one_sided_upper"])
                <= -brier_floor
            ),
            "log_loss_bootstrap_upper_clears_fixed_effect_floor": (
                float(paired_bootstrap["log_loss_delta"]["one_sided_upper"])
                <= -log_loss_floor
            ),
        })
    test_gate["met"] = all(test_gate.values())
    final_status = (
        "PASSED_UNTOUCHED_TEST_PAPER_ARTIFACT_ONLY"
        if test_gate["met"]
        else "REJECTED_ON_UNTOUCHED_TEST"
    )
    state = {
        **reservation,
        "status": final_status,
        "scored_at": datetime.now(timezone.utc).isoformat(),
        "untouched_test_labels_read_once": True,
        "test_metrics": metrics,
        "test_gate": test_gate,
        "test_metrics_sha256": _canonical_sha256(metrics),
    }
    state = _update_test_state(test_state_path, state)
    final_report = {
        **report,
        "status": final_status,
        "untouched_test_labels_read": True,
        "untouched_test_labels_previously_scored": False,
        "untouched_test_scored_once": True,
        "recovered_from_finalized_test_state": False,
        "test_metrics": metrics,
        "test_gate": test_gate,
        "test_state_sha256": state["test_state_sha256"],
    }
    if not test_gate["met"]:
        return None, final_report
    artifact = _build_paper_artifact(
        runtime=runtime,
        design=design,
        design_sha256=fingerprint,
        cohort=cohort,
        data_sha256=data_hash,
        model=model,
        config=config,
        protocol_binding=protocol_binding,
        walk_forward=walk_forward,
        test_state=state,
    )
    final_report["artifact_emitted"] = True
    final_report["artifact_sha256"] = artifact["artifact_sha256"]
    return artifact, final_report


def _inspection_boundary(rows: Sequence[Mapping[str, Any]]) -> float:
    values = [_num(row.get("close_time")) for row in rows]
    valid = [value for value in values if value is not None]
    if not valid:
        raise ValueError("inspection_boundary_missing")
    return max(valid)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--design",
        required=True,
        help=(
            "Explicit frozen design manifest. Required to prevent scoring "
            "the wrong version through a stale default."
        ),
    )
    parser.add_argument("--strategy-db", default=str(DEFAULT_DB))
    parser.add_argument("--cohort", choices=tuple(COHORT_ASSETS), required=True)
    parser.add_argument("--output-dir", default="work/rti-microstructure-freeze")
    parser.add_argument("--confirm-score-untouched-test", action="store_true")
    args = parser.parse_args()

    design = json.loads(Path(args.design).read_text(encoding="utf-8"))
    if not isinstance(design, Mapping):
        raise ValueError("design_root_not_object")
    validate_design(design)
    db_path = Path(args.strategy_db)
    feature_rows = load_feature_rows(db_path)
    coverage = build_report(
        feature_rows, source_schema=str(design.get("source_schema") or ""),
    )
    coverage.update(
        _feature_runtime(design).model_feature_window_coverage(feature_rows)
    )
    output_dir = Path(args.output_dir)
    bind_design_output_directory(
        output_dir,
        design_id=design["design_id"],
        design_sha256=design_fingerprint(design),
    )
    state_path = output_dir / f"{args.cohort.lower()}-untouched-test-state.json"
    artifact, report = run_locked_freeze(
        design=design,
        coverage=coverage,
        feature_rows=feature_rows,
        cohort=args.cohort,
        prospective_boundary=_inspection_boundary(feature_rows),
        read_labels=lambda ids: load_labels(db_path, ids),
        labels_are_available=lambda ids: labels_available(db_path, ids),
        confirm_score_untouched_test=args.confirm_score_untouched_test,
        test_state_path=state_path,
    )
    report_path = output_dir / f"{args.cohort.lower()}-report.json"
    atomic_write_json(report_path, report)
    artifact_path = None
    if artifact is not None:
        artifact_path = output_dir / f"{args.cohort.lower()}-paper-artifact.json"
        atomic_write_json(artifact_path, artifact)
    print(json.dumps({
        "status": report["status"],
        "cohort": args.cohort,
        "report": str(report_path),
        "artifact": None if artifact_path is None else str(artifact_path),
        "outcome_labels_read": report["outcome_labels_read"],
        "untouched_test_labels_read": report["untouched_test_labels_read"],
        "model_fit_performed": report["model_fit_performed"],
        "artifact_emitted": report["artifact_emitted"],
    }, indent=2))


if __name__ == "__main__":
    main()
