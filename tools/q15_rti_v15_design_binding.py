"""Validate the manually bound, outcome-blind RTI V15 feature design.

This tool reads only immutable manifests and the immutable 30-window geometry
artifact.  It cannot read strategy outcomes, fit or score a model, create a
model artifact, notify, promote, or trade.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots import rti_microstructure_v14 as v14
from q15_upgrade.strategy_bots import rti_microstructure_v15 as v15
from q15_upgrade.strategy_bots.rti_independent_path import DERIVED_FEATURE_KEYS
from q15_upgrade.strategy_bots.rti_independent_path_geometry_freeze_identity import (
    CONTRACT_ID as GEOMETRY_CONTRACT_ID,
    CONTRACT_SHA256 as GEOMETRY_CONTRACT_SHA256,
)
from q15_upgrade.strategy_bots.rti_independent_path_identity import (
    DESIGN_ID as PATH_DESIGN_ID,
    DESIGN_SHA256 as PATH_DESIGN_SHA256,
)
from q15_upgrade.strategy_bots.rti_microstructure_v15_identity import (
    CHARTER_ID,
    CHARTER_SHA256,
    DESIGN_ID,
    DESIGN_SHA256,
    EVALUATION_PROTOCOL_ID,
    EVALUATION_PROTOCOL_SHA256,
    FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME,
)
from tools import q15_rti_independent_path_geometry_freeze as geometry_freeze
from tools import q15_rti_independent_path_successor_preregister as preregister
from tools import q15_rti_microstructure_preregister as base_preregister


DEFAULT_DESIGN = ROOT / "config" / "q15_rti_microstructure_design_v15.json"
DEFAULT_V14_DESIGN = (
    ROOT / "config" / "q15_rti_microstructure_design_v14.json"
)
DEFAULT_CHARTER = (
    ROOT / "config" / "q15_rti_independent_path_successor_preregistration_v1.json"
)
DEFAULT_PROTOCOL = ROOT / "config" / "q15_rti_v15_walk_forward_protocol.json"
DEFAULT_GEOMETRY_ARTIFACT = (
    ROOT
    / "reports"
    / "q15_rti_independent_path_geometry_freeze_30"
    / "geometry-review.json"
)

EXPECTED_STATUS = (
    "OUTCOME_BLIND_EXECUTABLE_FEATURE_DESIGN_BOUND_AFTER_IMMUTABLE_GEOMETRY_PASS"
)
EXPECTED_GEOMETRY_DECISION = (
    "PASS_CONTINUE_PROSPECTIVE_COLLECTION_NO_MODEL"
)
EXPECTED_ARCHITECTURE = (
    "single_joint_v14_plus_five_path_feature_residual_with_nested_safe_trust"
)


def _load(path: Path, error: str) -> dict[str, Any]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, Mapping):
        raise ValueError(error)
    return dict(decoded)


def _parse_timestamp(value: Any, error: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(error)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(error) from exc
    if parsed.tzinfo is None:
        raise ValueError(error)
    return parsed


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _all_false(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> bool:
    return all(mapping.get(key) is False for key in keys)


def validate_design_binding(
    design: Mapping[str, Any],
    *,
    v14_design: Mapping[str, Any],
    charter: Mapping[str, Any],
    protocol: Mapping[str, Any],
    geometry_artifact: Mapping[str, Any],
    geometry_artifact_file_sha256: str,
) -> None:
    preregister.validate_charter(charter)
    preregister.validate_protocol(protocol, charter)
    base_preregister.validate_design(v14_design)
    geometry_freeze.validate_artifact(geometry_artifact)

    if base_preregister.design_fingerprint(design) != DESIGN_SHA256:
        raise ValueError("v15_design_binding_sha256_mismatch")
    if (
        design.get("design_id") != DESIGN_ID
        or design.get("design_status") != EXPECTED_STATUS
        or design.get("source_successor_charter_id") != CHARTER_ID
        or design.get("source_successor_charter_sha256") != CHARTER_SHA256
        or design.get("source_evaluation_protocol_id")
        != EVALUATION_PROTOCOL_ID
        or design.get("source_evaluation_protocol_sha256")
        != EVALUATION_PROTOCOL_SHA256
        or design.get("source_v14_design_id") != v14.DESIGN_ID
        or design.get("source_v14_design_sha256") != v14.DESIGN_SHA256
        or design.get("source_path_design_id") != PATH_DESIGN_ID
        or design.get("source_path_design_sha256") != PATH_DESIGN_SHA256
    ):
        raise ValueError("v15_design_binding_lineage_mismatch")
    if (
        design.get("source_successor_charter_sha256")
        != base_preregister.design_fingerprint(charter)
        or design.get("source_evaluation_protocol_sha256")
        != base_preregister.design_fingerprint(protocol)
        or design.get("source_v14_design_sha256")
        != base_preregister.design_fingerprint(v14_design)
    ):
        raise ValueError("v15_design_binding_source_fingerprint_mismatch")

    source_geometry = design.get("source_geometry_artifact")
    payload = geometry_artifact.get("payload")
    if not isinstance(source_geometry, Mapping) or not isinstance(
        payload, Mapping,
    ):
        raise ValueError("v15_design_binding_geometry_missing")
    selected_identity = dict(
        dict(payload.get("selected_source_quality") or {}).get(
            "selected_feature_evidence_identity"
        )
        or {}
    )
    if (
        source_geometry.get("contract_id") != GEOMETRY_CONTRACT_ID
        or source_geometry.get("contract_sha256")
        != GEOMETRY_CONTRACT_SHA256
        or source_geometry.get("file_sha256")
        != geometry_artifact_file_sha256
        or source_geometry.get("payload_sha256")
        != geometry_artifact.get("payload_sha256")
        or source_geometry.get("selected_feature_evidence_sha256")
        != selected_identity.get("sha256")
        or source_geometry.get("decision") != EXPECTED_GEOMETRY_DECISION
        or source_geometry.get("decision") != payload.get("decision")
        or int(source_geometry.get("complete_close_windows") or 0) != 30
        or int(source_geometry.get("selected_rows") or 0) != 210
        or source_geometry.get("outcome_labels_read") is not False
        or source_geometry.get("model_fit_performed") is not False
    ):
        raise ValueError("v15_design_binding_geometry_lineage_mismatch")
    bound_at = _parse_timestamp(
        design.get("bound_at"), "v15_design_binding_timestamp_invalid",
    )
    frozen_at = _parse_timestamp(
        payload.get("frozen_at"), "v15_design_binding_geometry_time_invalid",
    )
    if bound_at < frozen_at:
        raise ValueError("v15_design_binding_precedes_geometry_pass")

    feature_names = tuple(design.get("feature_names") or ())
    if (
        feature_names != v15.FEATURE_NAMES
        or feature_names[:20] != tuple(v14.FEATURE_NAMES)
        or feature_names[20:] != tuple(DERIVED_FEATURE_KEYS)
        or int(design.get("base_feature_count") or 0) != 20
        or int(design.get("added_path_feature_count") or 0) != 5
        or int(design.get("total_feature_count") or 0) != 25
        or design.get("base_features_are_exact_v14_features") is not True
        or design.get("added_features_are_exact_frozen_path_features")
        is not True
    ):
        raise ValueError("v15_design_binding_feature_projection_mismatch")
    if (
        design.get("feature_schema_version") != v15.FEATURE_SCHEMA_VERSION
        or design.get("model_family") != v14.MODEL_FAMILY
        or design.get("architecture") != EXPECTED_ARCHITECTURE
        or design.get("target") != v14_design.get("target")
        or design.get("market_prior") != v14_design.get("market_prior")
        or design.get("fixed_training_config")
        != v14_design.get("fixed_training_config")
        or design.get("prediction_combination")
        != v14_design.get("prediction_combination")
        or design.get("entry_policy") != v14_design.get("entry_policy")
    ):
        raise ValueError("v15_design_binding_v14_invariance_mismatch")
    for key in (
        "base_features_are_exact_v14_features",
        "added_features_are_exact_frozen_path_features",
    ):
        if design.get(key) is not True:
            raise ValueError(f"v15_design_binding_guard_missing:{key}")
    for key in (
        "feature_interactions_allowed",
        "polynomial_expansion_allowed",
        "automatic_feature_selection_allowed",
        "automatic_hyperparameter_search_allowed",
    ):
        if design.get(key) is not False:
            raise ValueError(f"v15_design_binding_guard_missing:{key}")

    if (
        float(design.get("prospective_after_close_time") or 0.0)
        != PROSPECTIVE_AFTER_CLOSE_TIME
        or float(design.get("first_eligible_close_time") or 0.0)
        != FIRST_ELIGIBLE_CLOSE_TIME
        or design.get("decision_interval") != "exact_13m"
        or design.get(
            "locked_evaluation_source_rows_at_or_after_first_eligible_close_allowed"
        ) is not True
        or design.get("historical_results_can_promote") is not False
        or design.get("paper_challenger_historical_credit_allowed") is not False
    ):
        raise ValueError("v15_design_binding_boundary_or_credit_mismatch")
    path_integrity = design.get("path_integrity")
    if not isinstance(path_integrity, Mapping) or (
        path_integrity.get(
            "complete_reconstructable_seven_asset_close_window_required"
        ) is not True
        or path_integrity.get("coinbase_and_kraken_both_required") is not True
        or path_integrity.get(
            "evidence_cutoff_must_equal_source_captured_at"
        ) is not True
        or float(path_integrity.get(
            "exact_capture_offset_minimum_seconds", -1.0
        )) != 0.0
        or float(path_integrity.get(
            "exact_capture_offset_maximum_seconds", -1.0
        )) != 2.0
        or float(path_integrity.get("path_horizon_seconds") or 0.0) != 60.0
        or float(path_integrity.get("path_maximum_gap_seconds") or 0.0)
        != 10.0
        or path_integrity.get("canonical_evidence_sha256_required") is not True
        or path_integrity.get(
            "persisted_evidence_must_recompute_every_added_feature"
        ) is not True
        or path_integrity.get("future_source_rows_forbidden") is not True
        or path_integrity.get("missing_path_imputation_allowed") is not False
        or path_integrity.get("partial_close_windows_allowed") is not False
    ):
        raise ValueError("v15_design_binding_path_integrity_mismatch")

    cohorts = design.get("cohorts")
    protocol_cohorts = protocol.get("cohorts")
    if not isinstance(cohorts, Mapping) or not isinstance(
        protocol_cohorts, Mapping,
    ) or set(cohorts) != {"BTC", "NON_BTC_TRANSFER"}:
        raise ValueError("v15_design_binding_cohorts_invalid")
    for name in ("BTC", "NON_BTC_TRANSFER"):
        actual = dict(cohorts.get(name) or {})
        expected = dict(protocol_cohorts.get(name) or {})
        for key in (
            "assets",
            "minimum_complete_close_windows",
            "development_train_windows",
            "calibration_windows",
            "untouched_test_windows",
        ):
            if actual.get(key) != expected.get(key):
                raise ValueError(
                    f"v15_design_binding_cohort_mismatch:{name}:{key}"
                )
    chronology = design.get("chronology")
    if not isinstance(chronology, Mapping) or (
        chronology.get("same_close_assets_must_share_fold") is not True
        or chronology.get("partial_close_windows_forbidden") is not True
        or chronology.get("timestamp_alignment_fail_closed") is not True
        or chronology.get(
            "outcome_labels_forbidden_before_cohort_readiness"
        ) is not True
        or chronology.get("btc_and_non_btc_must_never_be_pooled") is not True
        or chronology.get("test_may_be_scored_once") is not True
        or chronology.get(
            "test_may_not_tune_features_hyperparameters_factor_grid_entry_rules_or_thresholds"
        ) is not True
    ):
        raise ValueError("v15_design_binding_chronology_invalid")

    safety = design.get("safety")
    if (
        design.get("paper_only") is not True
        or design.get("notification_eligible") is not False
        or not _all_false(design, (
            "outcome_labels_used_for_binding",
            "performance_metrics_inspected_for_binding",
            "model_fit_performed_for_binding",
            "feature_selection_performed_for_binding",
            "threshold_selection_performed_for_binding",
            "automatic_scoring",
            "automatic_refit",
            "automatic_promotion",
            "real_trading_allowed",
        ))
        or not isinstance(safety, Mapping)
        or safety.get("executable_feature_design_frozen") is not True
        or not _all_false(safety, (
            "outcome_access_allowed_now",
            "model_fit_allowed_now",
            "probability_scoring_allowed_now",
            "model_artifact_allowed_now",
            "notifications_allowed_now",
            "automatic_promotion_allowed",
            "real_trading_allowed",
        ))
    ):
        raise ValueError("v15_design_binding_safety_invalid")


def validate_files(
    *,
    design_path: Path = DEFAULT_DESIGN,
    v14_design_path: Path = DEFAULT_V14_DESIGN,
    charter_path: Path = DEFAULT_CHARTER,
    protocol_path: Path = DEFAULT_PROTOCOL,
    geometry_artifact_path: Path = DEFAULT_GEOMETRY_ARTIFACT,
) -> dict[str, Any]:
    design = _load(design_path, "v15_design_binding_root_not_object")
    v14_design = _load(v14_design_path, "v15_v14_design_root_not_object")
    charter = _load(charter_path, "v15_charter_root_not_object")
    protocol = _load(protocol_path, "v15_protocol_root_not_object")
    artifact = _load(
        geometry_artifact_path, "v15_geometry_artifact_root_not_object",
    )
    artifact_file_sha256 = _file_sha256(geometry_artifact_path)
    validate_design_binding(
        design,
        v14_design=v14_design,
        charter=charter,
        protocol=protocol,
        geometry_artifact=artifact,
        geometry_artifact_file_sha256=artifact_file_sha256,
    )
    return {
        "status": "V15_EXECUTABLE_FEATURE_DESIGN_BOUND_AND_VERIFIED",
        "design_id": DESIGN_ID,
        "design_sha256": DESIGN_SHA256,
        "feature_schema_version": v15.FEATURE_SCHEMA_VERSION,
        "feature_count": len(v15.FEATURE_NAMES),
        "base_feature_count": len(v15.BASE_FEATURE_NAMES),
        "path_feature_count": len(v15.PATH_FEATURE_NAMES),
        "geometry_decision": dict(artifact["payload"])["decision"],
        "geometry_payload_sha256": artifact["payload_sha256"],
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "model_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--design", default=str(DEFAULT_DESIGN))
    parser.add_argument("--v14-design", default=str(DEFAULT_V14_DESIGN))
    parser.add_argument("--charter", default=str(DEFAULT_CHARTER))
    parser.add_argument("--protocol", default=str(DEFAULT_PROTOCOL))
    parser.add_argument(
        "--geometry-artifact", default=str(DEFAULT_GEOMETRY_ARTIFACT),
    )
    args = parser.parse_args()
    result = validate_files(
        design_path=Path(args.design),
        v14_design_path=Path(args.v14_design),
        charter_path=Path(args.charter),
        protocol_path=Path(args.protocol),
        geometry_artifact_path=Path(args.geometry_artifact),
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
