"""Outcome-blind source validation for the frozen V20 model study."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from . import rti_microstructure_v20_features as features
from . import rti_microstructure_v20_identity as identity
from . import rti_microstructure_v19 as v19
from tools.q15_rti_microstructure_preregister import design_fingerprint


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROTOCOL = ROOT / identity.PROTOCOL_RELATIVE_PATH


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_protocol(path: Path = DEFAULT_PROTOCOL) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("v20_protocol_unreadable") from exc
    if not isinstance(value, Mapping):
        raise ValueError("v20_protocol_root_not_object")
    protocol = dict(value)
    disclosure = dict(protocol.get("outcome_blind_design_disclosure") or {})
    population = dict(protocol.get("population") or {})
    feature_contract = dict(protocol.get("feature_contract") or {})
    evaluation = dict(protocol.get("historical_evaluation") or {})
    models = dict(protocol.get("model_families") or {})
    execution = dict(protocol.get("selective_execution_policy") or {})
    historical_gates = dict(protocol.get("historical_gates") or {})
    promotion = dict(protocol.get("prospective_paper_promotion") or {})
    collection = dict(protocol.get("collection") or {})
    if (
        design_fingerprint(protocol) != identity.PROTOCOL_SHA256
        or protocol.get("protocol_id") != identity.PROTOCOL_ID
        or protocol.get("design_id") != identity.DESIGN_ID
        or protocol.get("protocol_status")
        != "FROZEN_BEFORE_ANY_V20_PROSPECTIVE_OUTCOME_ACCESS"
        or disclosure.get("reservoir_outcomes_or_resolution_status_inspected")
        is not False
        or disclosure.get("v18_or_v19_prospective_outcomes_inspected")
        is not False
        or disclosure.get("labels_used_to_choose_features_models_or_thresholds")
        is not False
        or float(population.get("prospective_after_close_time") or 0.0)
        != identity.PROSPECTIVE_AFTER_CLOSE_TIME
        or float(population.get("first_eligible_close_time") or 0.0)
        != identity.FIRST_ELIGIBLE_CLOSE_TIME
        or population.get("separate_cohort_models_and_reports_required")
        is not True
        or population.get("cross_cohort_pooling_for_promotion_forbidden")
        is not True
        or population.get("all_seven_assets_in_one_close_are_one_chronological_cluster")
        is not True
        or feature_contract.get("feature_builder_version")
        != identity.FEATURE_BUILDER_VERSION
        or int(feature_contract.get("feature_count") or 0)
        != identity.FEATURE_COUNT
        or feature_contract.get("feature_names_sha256")
        != identity.FEATURE_NAMES_SHA256
        or tuple(feature_contract.get("feature_names") or ())
        != features.FEATURE_NAMES
        or _canonical_sha256(list(features.FEATURE_NAMES))
        != identity.FEATURE_NAMES_SHA256
        or feature_contract.get("missing_required_feature_fails_closed")
        is not True
        or feature_contract.get("feature_selection_after_label_access_forbidden")
        is not True
        or int(evaluation.get("minimum_complete_close_windows") or 0)
        != identity.MINIMUM_COMPLETE_CLOSE_WINDOWS
        or int(evaluation.get("train_windows") or 0)
        != identity.TRAIN_CLOSE_WINDOWS
        or int(evaluation.get("calibration_windows") or 0)
        != identity.CALIBRATION_CLOSE_WINDOWS
        or int(evaluation.get("untouched_test_windows") or 0)
        != identity.UNTOUCHED_TEST_CLOSE_WINDOWS
        or evaluation.get("calibration_rows_never_part_of_internal_validation")
        is not True
        or evaluation.get("untouched_test_opened_once_after_all_prior_gates_pass")
        is not True
        or models.get("selection_metric")
        != "LOWEST_MEAN_INTERNAL_WALK_FORWARD_LOG_LOSS"
        or models.get("test_guided_refit_recalibration_or_model_selection_forbidden")
        is not True
        or list(execution.get("calibration_edge_margin_grid") or ())
        != [0.0, 0.02, 0.04, 0.06]
        or historical_gates.get("historical_results_alone_can_promote")
        is not False
        or promotion.get("paper_challenger_created_only_if_every_historical_gate_passes")
        is not True
        or promotion.get("manual_promotion_only") is not True
        or promotion.get("automatic_promotion") is not False
        or promotion.get("real_trading_allowed") is not False
        or collection.get("outcome_access_allowed_now") is not False
        or collection.get("model_fit_allowed_now") is not False
        or collection.get("probability_scoring_allowed_now") is not False
        or collection.get("paper_artifact_allowed_now") is not False
        or collection.get("notifications_allowed_now") is not False
        or collection.get("telegram_allowed_now") is not False
        or collection.get("automatic_promotion_allowed") is not False
        or collection.get("real_trading_allowed") is not False
    ):
        raise ValueError("v20_protocol_identity_or_safety_invalid")
    return protocol


def evaluate_pair(
    parent_row: Mapping[str, Any], delayed_row: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate one V20 source pair and build its label-free feature vector."""
    load_protocol()
    result = features.feature_vector(parent_row, delayed_row)
    delayed_source = v19.evaluate_delayed_source(parent_row, delayed_row)
    failures = []
    try:
        close_time = float(result.get("close_time"))
    except (TypeError, ValueError):
        close_time = None
    if result.get("available") is not True:
        failures.append("V20_FEATURE_SOURCE_INCOMPLETE")
    execution = dict(delayed_source.get("evidence") or {})
    if (
        delayed_source.get("available") is not True
        or execution.get("sim_contracts") != 10.0
        or execution.get("sim_full_fill_supported") is not True
    ):
        failures.append("V20_FULL_TEN_CONTRACT_FILL_SUPPORT_REQUIRED")
    if (
        close_time is None
        or close_time <= identity.PROSPECTIVE_AFTER_CLOSE_TIME
    ):
        failures.append("STRICTLY_PROSPECTIVE_V20_CLOSE_REQUIRED")
    evidence = {
        "parent_id": result.get("parent_id"),
        "delayed_id": result.get("delayed_id"),
        "asset": result.get("asset"),
        "cohort": result.get("cohort"),
        "ticker": result.get("ticker"),
        "close_time": close_time,
        "side": result.get("side"),
        "feature_builder_version": result.get("feature_builder_version"),
        "feature_evidence_sha256": result.get("feature_evidence_sha256"),
        "feature_count": len(result.get("features") or ()),
    }
    return {
        "available": not failures,
        "eligible_for_v20_feature_credit": not failures,
        "failures": failures,
        "evidence": evidence,
        "source_feature_evidence_sha256": _canonical_sha256(evidence),
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "feature_names": result.get("feature_names"),
        "features": result.get("features"),
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }
