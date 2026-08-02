"""Fail-closed protocol validation for the dormant V22 challenger."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import rti_microstructure_v22_identity as identity
from . import rti_microstructure_v22_top_book_features as features
from . import rti_spot_rest_top_book_reservoir_identity as rest_identity


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROTOCOL = ROOT / identity.PROTOCOL_RELATIVE_PATH
DEFAULT_EVALUATOR = ROOT / identity.EVALUATOR_CONTRACT_RELATIVE_PATH


def load_protocol(path: Path = DEFAULT_PROTOCOL) -> dict[str, Any]:
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != identity.PROTOCOL_SHA256:
        raise ValueError("v22_protocol_sha256_mismatch")
    try:
        protocol = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("v22_protocol_json_invalid") from exc
    source = dict(protocol.get("source_contract") or {})
    feature = dict(protocol.get("feature_contract") or {})
    audit = dict(protocol.get("historical_audit_contract") or {})
    partitions = dict(audit.get("partitions") or {})
    safety = dict(protocol.get("safety") or {})
    freeze = dict(protocol.get("freeze_evidence") or {})
    if (
        protocol.get("protocol_id") != identity.PROTOCOL_ID
        or freeze.get("v22_feature_complete_close_windows_before_amendment") != 0
        or freeze.get("outcomes_or_resolution_status_inspected") is not False
        or freeze.get("labels_read") is not False
        or freeze.get("model_fit_or_probability_scoring_performed") is not False
        or source.get("rest_protocol_id") != rest_identity.PROTOCOL_ID
        or source.get("rest_protocol_sha256") != rest_identity.PROTOCOL_SHA256
        or source.get("first_eligible_common_close_time")
        != identity.FIRST_ELIGIBLE_COMMON_CLOSE_TIME
        or feature.get("feature_builder_version") != identity.FEATURE_BUILDER_VERSION
        or feature.get("base_feature_count") != identity.BASE_FEATURE_COUNT
        or feature.get("added_feature_count") != identity.ADDED_FEATURE_COUNT
        or feature.get("total_feature_count") != identity.FEATURE_COUNT
        or feature.get("feature_names_sha256") != identity.FEATURE_NAMES_SHA256
        or features.FEATURE_BUILDER_VERSION != identity.FEATURE_BUILDER_VERSION
        or len(features.FEATURE_NAMES) != identity.FEATURE_COUNT
        or len(features.BASE_FEATURE_NAMES) != identity.BASE_FEATURE_COUNT
        or len(features.EXCLUDED_SPOT_DERIVED_FEATURE_NAMES) != 14
        or features.FEATURE_NAMES_SHA256 != identity.FEATURE_NAMES_SHA256
        or audit.get("exclusive_earliest_complete_common_close_windows")
        != identity.MINIMUM_COMPLETE_COMMON_CLOSE_WINDOWS
        or partitions != {
            "train": [0, 104],
            "probability_calibration": [105, 129],
            "execution_policy_selection": [130, 154],
            "untouched_test": [155, 179],
        }
        or any(safety.get(key) is not False for key in (
            "outcome_access_allowed_now", "label_access_allowed_now",
            "model_fit_allowed_now", "probability_scoring_allowed_now",
            "paper_artifact_allowed_now", "notifications_allowed_now",
            "automatic_promotion_allowed", "real_trading_allowed",
        ))
    ):
        raise ValueError("v22_protocol_contract_invalid")
    return protocol


def load_evaluator_contract(path: Path = DEFAULT_EVALUATOR) -> dict[str, Any]:
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != identity.EVALUATOR_CONTRACT_SHA256:
        raise ValueError("v22_evaluator_sha256_mismatch")
    try:
        contract = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("v22_evaluator_json_invalid") from exc
    freeze = dict(contract.get("freeze_evidence") or {})
    amendment = dict(contract.get("pre_evidence_implementation_amendment") or {})
    dependency = dict(contract.get("dependency_contract") or {})
    partitions = dict(contract.get("partitions") or {})
    preprocessing = dict(contract.get("preprocessing") or {})
    candidates = dict(contract.get("candidate_models") or {})
    selection = dict(contract.get("model_selection") or {})
    calibration = dict(contract.get("probability_calibration") or {})
    policy = dict(contract.get("execution_policy_selection") or {})
    bootstrap = dict(contract.get("bootstrap") or {})
    ablation = dict(contract.get("base_feature_ablation") or {})
    untouched = dict(contract.get("untouched_test") or {})
    fees = dict(contract.get("fee_schedule_verification") or {})
    reporting = dict(contract.get("reporting_subgroups") or {})
    safety = dict(contract.get("safety") or {})
    required_gates = [
        "FEE_SLIPPAGE_ADJUSTED_PNL_STRICTLY_POSITIVE",
        "CLOSE_CLUSTER_BOOTSTRAP_20TH_PERCENTILE_MEAN_PNL_STRICTLY_POSITIVE",
        "WILSON_95_LOWER_STRICTLY_EXCEEDS_AVERAGE_BREAK_EVEN",
        "ALL_ROW_LOG_LOSS_STRICTLY_BEATS_12M_MARKET",
        "ALL_ROW_BRIER_STRICTLY_BEATS_12M_MARKET",
        "ALL_ROW_LOG_LOSS_STRICTLY_BEATS_INDEPENDENT_62_FEATURE_BASE_ABLATION",
        "ALL_ROW_BRIER_STRICTLY_BEATS_INDEPENDENT_62_FEATURE_BASE_ABLATION",
        "MAXIMUM_DRAWDOWN_PER_PICK_STRICTLY_BELOW_ALL_SOURCE_EXECUTABLE_CONTROL",
        "FROZEN_TEST_VOLUME_AND_SIDE_MINIMA",
    ]
    if (
        contract.get("contract_id") != identity.EVALUATOR_CONTRACT_ID
        or contract.get("contract_status")
        != "FROZEN_BEFORE_ANY_V22_LABEL_ACCESS_MODEL_FIT_OR_PROBABILITY_SCORING"
        or freeze.get("v22_feature_complete_close_windows_at_freeze") != 3
        or freeze.get("v22_feature_rows_at_freeze") != 21
        or freeze.get("v22_outcomes_or_resolution_status_inspected") is not False
        or freeze.get("v22_labels_read") is not False
        or freeze.get("v22_model_fit_or_probability_scoring_performed") is not False
        or amendment.get("v22_feature_complete_close_windows_at_amendment") != 4
        or amendment.get("v22_outcomes_or_resolution_status_inspected") is not False
        or amendment.get("v22_labels_read") is not False
        or amendment.get("v22_model_fit_or_probability_scoring_performed") is not False
        or contract.get("protocol_id") != identity.PROTOCOL_ID
        or contract.get("protocol_sha256") != identity.PROTOCOL_SHA256
        or contract.get("feature_builder_version") != identity.FEATURE_BUILDER_VERSION
        or contract.get("feature_count") != identity.FEATURE_COUNT
        or contract.get("feature_names_sha256") != identity.FEATURE_NAMES_SHA256
        or dependency != {
            "python_package": "scikit-learn",
            "exact_version": "1.9.0",
            "single_threaded_fit_required": True,
            "deterministic_random_seed": 1522,
        }
        or partitions.get("exclusive_earliest_complete_windows")
        != identity.MINIMUM_COMPLETE_COMMON_CLOSE_WINDOWS
        or partitions.get("train") != [0, 104]
        or partitions.get("probability_calibration") != [105, 129]
        or partitions.get("execution_policy_selection") != [130, 154]
        or partitions.get("untouched_test") != [155, 179]
        or partitions.get("same_close_all_asset_cluster_may_not_cross_partitions")
        is not True
        or partitions.get("probability_calibration_and_policy_selection_are_disjoint")
        is not True
        or len(contract.get("internal_walk_forward_folds") or ()) != 4
        or dict(contract.get("cohorts") or {}).get("separate")
        != ["NON_BTC_TRANSFER", "BTC"]
        or preprocessing.get("fit_scope") != "EACH_TRAINING_FOLD_ONLY"
        or preprocessing.get("numeric_center") != "MEDIAN"
        or preprocessing.get("numeric_scale") != "INTERQUARTILE_RANGE"
        or preprocessing.get("quantile_method") != "linear"
        or preprocessing.get("probability_clip") != [0.000001, 0.999999]
        or preprocessing.get("learned_imputation_forbidden") is not True
        or len(candidates.get("NON_BTC_TRANSFER") or ()) != 2
        or len(candidates.get("BTC") or ()) != 1
        or selection.get("primary_validation_rows")
        != "ROW_LEVEL_EXECUTABLE_VALIDATION_ROWS_ONLY"
        or selection.get("minimum_executable_rows_each_validation_fold_non_btc")
        != 20
        or selection.get("minimum_executable_rows_each_validation_fold_btc") != 4
        or calibration.get("candidate_methods")
        != ["IDENTITY", "L2_REGULARIZED_PLATT_ON_LOGIT"]
        or calibration.get("fit_windows") != [105, 129]
        or calibration.get("minimum_rows_non_btc") != 150
        or calibration.get("minimum_rows_btc") != 25
        or policy.get("windows") != [130, 154]
        or policy.get("contracts") != 10
        or policy.get("edge_margin_grid") != [0.0, 0.02, 0.04, 0.06]
        or policy.get("minimum_picks_non_btc") != 25
        or policy.get("minimum_picks_btc") != 8
        or policy.get("minimum_picks_each_side") != 4
        or bootstrap.get("unit") != "COMPLETE_CLOSE_CLUSTER"
        or bootstrap.get("resamples") != 5000
        or bootstrap.get("random_seed") != 1522
        or ablation.get("feature_count") != identity.BASE_FEATURE_COUNT
        or ablation.get("feature_indices_zero_based") != [0, 61]
        or ablation.get("feature_names_sha256")
        != "4148a6f7de978cbd918354b26cd4c598887c0b0656875febc4472038861ae590"
        or ablation.get("same_candidate_grid_as_v22") is not True
        or ablation.get("selection_uses_only_train_internal_walk_forward") is not True
        or untouched.get("windows") != [155, 179]
        or untouched.get("one_shot_only") is not True
        or untouched.get("required_gates") != required_gates
        or untouched.get("minimum_picks_non_btc") != 25
        or untouched.get("minimum_picks_btc") != 8
        or untouched.get("minimum_picks_each_side") != 4
        or fees.get("required_fee_type") != "quadratic"
        or fees.get("required_fee_multiplier") != 1
        or fees.get("general_taker_fee_rate") != 0.07
        or len(fees.get("series_tickers") or ()) != 7
        or reporting.get("report_only_never_used_for_selection_or_gates") is not True
        or reporting.get("rejected_trade_counterfactual_required") is not True
        or len(reporting.get("distance_absolute_bps_tiers") or ()) != 3
        or len(reporting.get("volatility_raw_bps_tiers") or ()) != 3
        or len(reporting.get("rest_imbalance_persistence_tiers") or ()) != 3
        or len(reporting.get("rest_spread_bps_tiers") or ()) != 3
        or len(reporting.get("rest_path_curvature_bps_tiers") or ()) != 4
        or safety.get("feature_seal_must_exist_before_any_label_reservation")
        is not True
        or safety.get("label_reservation_must_be_hash_bound_to_exact_parent_ids_contracts_and_feature_hashes")
        is not True
        or any(safety.get(key) is not False for key in (
            "outcome_access_allowed_now", "model_fit_allowed_now",
            "probability_scoring_allowed_now",
            "automatic_command_confirmation_allowed", "paper_artifact_allowed_now",
            "notification_allowed_now", "telegram_allowed_now",
            "automatic_promotion_allowed", "real_trading_allowed",
        ))
    ):
        raise ValueError("v22_evaluator_contract_invalid")
    return contract


def status() -> dict[str, Any]:
    load_protocol()
    load_evaluator_contract()
    return {
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "evaluator_contract_id": identity.EVALUATOR_CONTRACT_ID,
        "evaluator_contract_sha256": identity.EVALUATOR_CONTRACT_SHA256,
        "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
        "feature_count": identity.FEATURE_COUNT,
        "feature_names_sha256": identity.FEATURE_NAMES_SHA256,
        "first_eligible_common_close_time": identity.FIRST_ELIGIBLE_COMMON_CLOSE_TIME,
        "minimum_complete_common_close_windows": (
            identity.MINIMUM_COMPLETE_COMMON_CLOSE_WINDOWS
        ),
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
        "status": "DORMANT_COLLECTING_OUTCOME_BLIND_COMMON_SOURCE_WINDOWS",
    }
