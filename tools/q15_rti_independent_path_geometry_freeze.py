"""Manual immutable freeze for the first-30 independent-path geometry review.

The command uses the feature-only audit projection, refuses to write before
30 complete reconstructable windows, and never opens settlements or fits a
model.  An existing artifact is immutable and accepted only when its hashes
match a fresh reconstruction of the same earliest-30 evidence.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots.rti_independent_path_geometry_freeze_identity import (
    CONTRACT_ID,
    CONTRACT_SHA256,
    DEFAULT_ARTIFACT_RELATIVE_PATH,
)
from q15_upgrade.strategy_bots.rti_independent_path_geometry_identity import (
    PROTOCOL_ID as GEOMETRY_PROTOCOL_ID,
    PROTOCOL_SHA256 as GEOMETRY_PROTOCOL_SHA256,
)
from q15_upgrade.strategy_bots.rti_independent_path_identity import (
    DESIGN_ID,
    DESIGN_SHA256,
    FIRST_ELIGIBLE_CLOSE_TIME,
)
from q15_upgrade.strategy_bots.rti_independent_path_successor_identity import (
    CHARTER_ID as SUCCESSOR_CHARTER_ID,
    CHARTER_SHA256 as SUCCESSOR_CHARTER_SHA256,
    EVALUATION_PROTOCOL_ID as SUCCESSOR_EVALUATION_PROTOCOL_ID,
    EVALUATION_PROTOCOL_SHA256 as SUCCESSOR_EVALUATION_PROTOCOL_SHA256,
)
from tools.q15_rti_independent_path_audit import (
    CONTRACT_IDENTITY_VERSION,
    DEFAULT_DB,
    DEFAULT_DESIGN,
    DEFAULT_GEOMETRY_PROTOCOL,
    build_report,
    evaluate_geometry_review,
    validate_design,
    validate_geometry_protocol,
)
from tools.q15_rti_microstructure_freeze import (
    FEATURE_SELECT_COLUMNS,
    OUTCOME_COLUMNS,
    load_feature_rows,
)
from tools.q15_rti_microstructure_preregister import design_fingerprint


ARTIFACT_VERSION = "q15-rti-independent-path-geometry-review-artifact-v1"
DEFAULT_CONTRACT = (
    ROOT / "config" / "q15_rti_independent_path_geometry_freeze_contract_v1.json"
)
DEFAULT_ARTIFACT = ROOT / DEFAULT_ARTIFACT_RELATIVE_PATH
REQUIRED_PAYLOAD_KEYS = {
    "source_design_id", "source_design_sha256", "geometry_protocol_id",
    "geometry_protocol_sha256", "successor_charter_id",
    "successor_charter_sha256", "successor_evaluation_protocol_id",
    "successor_evaluation_protocol_sha256", "selected_complete_close_times",
    "selected_close_times_sha256", "selected_rows", "selected_geometry",
    "selected_geometry_sha256", "selected_source_quality",
    "selected_source_quality_sha256", "geometry_review", "decision",
    "consequences", "outcome_columns_selected", "outcome_labels_read",
    "model_fit_performed", "feature_selection_performed", "automatic_scoring",
    "automatic_promotion", "real_trading_allowed",
}
ACTUAL_PAYLOAD_KEYS = REQUIRED_PAYLOAD_KEYS | {"frozen_at", "selection"}
SELECTED_EVIDENCE_IDENTITY_VERSION = (
    "q15-rti-independent-path-selected-feature-evidence-sha256-v1"
)


def _load_object(path: Path, error: str) -> dict[str, Any]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, Mapping):
        raise ValueError(error)
    return dict(decoded)


def load_contract(path: Path = DEFAULT_CONTRACT) -> dict[str, Any]:
    return _load_object(path, "geometry_freeze_contract_root_not_object")


def validate_contract(contract: Mapping[str, Any]) -> None:
    if design_fingerprint(contract) != CONTRACT_SHA256:
        raise ValueError("geometry_freeze_contract_sha256_mismatch")
    if (
        contract.get("contract_id") != CONTRACT_ID
        or contract.get("contract_status")
        != "PREREGISTERED_BEFORE_30_WINDOW_REVIEW"
        or contract.get("applies_to_source_design_id") != DESIGN_ID
        or contract.get("applies_to_source_design_sha256") != DESIGN_SHA256
        or contract.get("applies_to_geometry_protocol_id")
        != GEOMETRY_PROTOCOL_ID
        or contract.get("applies_to_geometry_protocol_sha256")
        != GEOMETRY_PROTOCOL_SHA256
        or contract.get("applies_to_successor_charter_id")
        != SUCCESSOR_CHARTER_ID
        or contract.get("applies_to_successor_charter_sha256")
        != SUCCESSOR_CHARTER_SHA256
        or contract.get("applies_to_successor_evaluation_protocol_id")
        != SUCCESSOR_EVALUATION_PROTOCOL_ID
        or contract.get("applies_to_successor_evaluation_protocol_sha256")
        != SUCCESSOR_EVALUATION_PROTOCOL_SHA256
    ):
        raise ValueError("geometry_freeze_contract_lineage_mismatch")
    evidence = contract.get("evidence_available_at_preregistration")
    if not isinstance(evidence, Mapping) or (
        int(evidence.get("complete_reconstructable_close_windows") or 0) != 18
        or int(evidence.get("credited_rows") or 0) != 126
        or int(evidence.get("geometry_windows_remaining") or 0) != 12
        or evidence.get("source_quality_status")
        != "PASS_ALL_CREDITED_COMPLETE_ROWS"
        or int(evidence.get("source_integrity_breaches", -1)) != 0
        or evidence.get("outcome_labels_read") is not False
        or evidence.get("model_fit_performed") is not False
    ):
        raise ValueError("geometry_freeze_contract_origin_invalid")
    trigger = contract.get("trigger")
    if not isinstance(trigger, Mapping) or (
        int(trigger.get("complete_reconstructable_close_windows_at_least") or 0)
        != 30
        or trigger.get("evidence_selection")
        != "EARLIEST_30_COMPLETE_RECONSTRUCTABLE_WINDOWS"
        or int(trigger.get("selected_close_windows_must_equal") or 0) != 30
        or int(trigger.get("selected_all_seven_rows_must_equal") or 0) != 210
        or int(trigger.get("selected_btc_rows_must_equal") or 0) != 30
        or int(trigger.get("selected_non_btc_transfer_rows_must_equal") or 0)
        != 180
        or trigger.get("geometry_review_ready_must_be_true") is not True
        or trigger.get("source_quality_status_must_equal")
        != "PASS_ALL_CREDITED_COMPLETE_ROWS"
        or int(trigger.get("source_integrity_breaches_must_equal", -1)) != 0
        or int(trigger.get("evidence_parse_failures_must_equal", -1)) != 0
    ):
        raise ValueError("geometry_freeze_contract_trigger_invalid")
    artifact = contract.get("artifact")
    if not isinstance(artifact, Mapping) or (
        artifact.get("default_path") != DEFAULT_ARTIFACT_RELATIVE_PATH
        or any(artifact.get(key) is not True for key in (
            "canonical_json_sha256_required",
            "selected_close_times_sha256_required",
            "selected_geometry_sha256_required",
            "selected_source_quality_sha256_required",
            "existing_matching_artifact_is_idempotent",
            "existing_mismatched_artifact_must_fail_closed",
            "artifact_may_not_be_written_before_trigger",
            "artifact_may_not_be_overwritten",
        ))
    ):
        raise ValueError("geometry_freeze_contract_artifact_policy_invalid")
    if set(contract.get("required_payload") or ()) != REQUIRED_PAYLOAD_KEYS:
        raise ValueError("geometry_freeze_contract_required_payload_invalid")
    decision = contract.get("decision_policy")
    if not isinstance(decision, Mapping) or (
        decision.get("pass_status")
        != "PASS_CONTINUE_PROSPECTIVE_COLLECTION_NO_MODEL"
        or decision.get("failure_status")
        != "GEOMETRY_REVIEW_REQUIRED_NO_AUTOMATIC_ACTION"
        or decision.get("pass_continues_to_non_btc_60_and_btc_150") is not True
        or decision.get("pass_allows_outcome_access_at_30") is not False
        or decision.get("pass_allows_model_fit_at_30") is not False
        or decision.get("failure_requires_manual_diagnosis") is not True
        or any(decision.get(key) is not False for key in (
            "failure_allows_automatic_feature_removal",
            "failure_allows_automatic_threshold_change",
            "failure_allows_automatic_refit",
            "failure_allows_automatic_activation",
            "either_decision_allows_automatic_promotion",
            "either_decision_allows_real_trading",
        ))
    ):
        raise ValueError("geometry_freeze_contract_decision_policy_invalid")
    execution = contract.get("execution")
    if not isinstance(execution, Mapping) or (
        execution.get("manual_command_only") is not True
        or execution.get("background_monitor_may_write_artifact") is not False
        or execution.get("dry_run_before_trigger_allowed") is not True
        or execution.get("outcome_columns_forbidden") is not True
        or any(execution.get(key) is not False for key in (
            "outcome_labels_read", "model_fit_performed",
            "feature_selection_performed", "automatic_scoring",
            "automatic_promotion", "real_trading_allowed",
        ))
    ):
        raise ValueError("geometry_freeze_contract_execution_invalid")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _valid_sha256(value: Any) -> bool:
    text = str(value or "")
    if len(text) != 64:
        return False
    try:
        int(text, 16)
    except ValueError:
        return False
    return True


def _valid_selected_close_times(value: Any) -> bool:
    if not isinstance(value, list) or len(value) != 30:
        return False
    if any(
        isinstance(item, bool) or not isinstance(item, (int, float))
        for item in value
    ):
        return False
    try:
        times = [float(item) for item in value]
    except (TypeError, ValueError):
        return False
    if not (
        all(math.isfinite(item) for item in times)
        and times == sorted(times)
        and len(set(times)) == 30
        and times[0] >= FIRST_ELIGIBLE_CLOSE_TIME
    ):
        return False
    return all(
        abs((item - FIRST_ELIGIBLE_CLOSE_TIME) / 900.0 - round(
            (item - FIRST_ELIGIBLE_CLOSE_TIME) / 900.0
        )) <= 1e-9
        for item in times
    )


def _valid_frozen_at(value: Any) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None


def _valid_selected_evidence_identity(
    source_quality: Mapping[str, Any],
) -> bool:
    identity = source_quality.get("selected_feature_evidence_identity")
    try:
        rows = int(dict(identity or {}).get("rows") or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        isinstance(identity, Mapping)
        and identity.get("version") == SELECTED_EVIDENCE_IDENTITY_VERSION
        and rows == 210
        and _valid_sha256(identity.get("sha256"))
        and identity.get("outcome_columns_selected") is False
        and identity.get("outcome_labels_read") is False
    )


def _valid_contract_identity(source_quality: Mapping[str, Any]) -> bool:
    identity = source_quality.get("contract_identity")
    try:
        rows = int(dict(identity or {}).get("rows") or 0)
        mismatches = int(dict(identity or {}).get("mismatch_rows") or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        isinstance(identity, Mapping)
        and identity.get("version") == CONTRACT_IDENTITY_VERSION
        and rows == 210
        and mismatches == 0
        and identity.get("ticker_asset_alignment_required") is True
        and identity.get("ticker_close_time_alignment_required") is True
        and identity.get("dst_fold_safe") is True
        and identity.get("outcome_labels_read") is False
    )


def _expected_review_from_evidence(
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    protocol = _load_object(
        DEFAULT_GEOMETRY_PROTOCOL,
        "geometry_freeze_geometry_protocol_root_not_object",
    )
    validate_geometry_protocol(protocol)
    return evaluate_geometry_review({
        "complete_seven_asset_close_windows": 30,
        "geometry_review_evidence": dict(evidence),
    }, protocol)


def _validate_ready_report(report: Mapping[str, Any]) -> None:
    review = report.get("geometry_review")
    evidence = report.get("geometry_review_evidence")
    if not isinstance(review, Mapping) or not isinstance(evidence, Mapping):
        raise ValueError("geometry_freeze_report_sections_missing")
    close_times = evidence.get("complete_close_times")
    if (
        report.get("design_id") != DESIGN_ID
        or report.get("design_sha256") != DESIGN_SHA256
        or review.get("protocol_id") != GEOMETRY_PROTOCOL_ID
        or review.get("protocol_sha256") != GEOMETRY_PROTOCOL_SHA256
        or int(report.get("complete_seven_asset_close_windows") or 0) < 30
        or review.get("review_ready") is not True
        or review.get("status") not in {
            "PASS_CONTINUE_PROSPECTIVE_COLLECTION_NO_MODEL",
            "GEOMETRY_REVIEW_REQUIRED_NO_AUTOMATIC_ACTION",
        }
        or int(evidence.get("complete_close_windows") or 0) != 30
        or int(evidence.get("rows") or 0) != 210
        or not _valid_selected_close_times(close_times)
        or evidence.get("selection")
        != "EARLIEST_30_COMPLETE_RECONSTRUCTABLE_WINDOWS"
        or evidence.get("outcome_columns_selected") is not False
        or evidence.get("outcome_labels_read") is not False
        or evidence.get("model_fit_performed") is not False
    ):
        raise ValueError("geometry_freeze_report_not_ready_or_unsafe")
    cohorts = evidence.get("cohorts")
    source_quality = evidence.get("source_quality")
    if not isinstance(cohorts, Mapping) or (
        int(dict(cohorts.get("ALL_SEVEN") or {}).get("rows") or 0) != 210
        or int(dict(cohorts.get("BTC") or {}).get("rows") or 0) != 30
        or int(dict(cohorts.get("NON_BTC_TRANSFER") or {}).get("rows") or 0)
        != 180
    ):
        raise ValueError("geometry_freeze_report_row_geometry_invalid")
    if not isinstance(source_quality, Mapping) or (
        source_quality.get("status") != "PASS_ALL_CREDITED_COMPLETE_ROWS"
        or int(source_quality.get("credited_complete_rows") or 0) != 210
        or int(source_quality.get("evidence_parse_failures", -1)) != 0
        or int(source_quality.get("integrity_breaches", -1)) != 0
        or source_quality.get("outcome_labels_read") is not False
        or source_quality.get("source_thresholds_from_frozen_design") is not True
        or source_quality.get("thresholds_selected_from_outcomes") is not False
        or not _valid_selected_evidence_identity(source_quality)
        or not _valid_contract_identity(source_quality)
    ):
        raise ValueError("geometry_freeze_report_source_quality_invalid")
    if _canonical_bytes(review) != _canonical_bytes(
        _expected_review_from_evidence(evidence)
    ):
        raise ValueError("geometry_freeze_report_review_recomputation_mismatch")
    if any(report.get(key) is not False for key in (
        "outcome_columns_selected", "outcome_labels_read",
        "model_fit_performed", "automatic_scoring", "automatic_promotion",
        "real_trading_allowed",
    )):
        raise ValueError("geometry_freeze_report_safety_invalid")


def build_payload(
    report: Mapping[str, Any], *, frozen_at: str | None = None,
) -> dict[str, Any]:
    _validate_ready_report(report)
    evidence = dict(report["geometry_review_evidence"])
    review = dict(report["geometry_review"])
    close_times = list(evidence["complete_close_times"])
    geometry = dict(evidence["cohorts"])
    source_quality = dict(evidence["source_quality"])
    decision = str(review["status"])
    passed = decision == "PASS_CONTINUE_PROSPECTIVE_COLLECTION_NO_MODEL"
    return {
        "frozen_at": frozen_at or datetime.now(timezone.utc).isoformat(),
        "source_design_id": DESIGN_ID,
        "source_design_sha256": DESIGN_SHA256,
        "geometry_protocol_id": GEOMETRY_PROTOCOL_ID,
        "geometry_protocol_sha256": GEOMETRY_PROTOCOL_SHA256,
        "successor_charter_id": SUCCESSOR_CHARTER_ID,
        "successor_charter_sha256": SUCCESSOR_CHARTER_SHA256,
        "successor_evaluation_protocol_id": SUCCESSOR_EVALUATION_PROTOCOL_ID,
        "successor_evaluation_protocol_sha256": (
            SUCCESSOR_EVALUATION_PROTOCOL_SHA256
        ),
        "selection": "EARLIEST_30_COMPLETE_RECONSTRUCTABLE_WINDOWS",
        "selected_complete_close_times": close_times,
        "selected_close_times_sha256": canonical_sha256(close_times),
        "selected_rows": 210,
        "selected_geometry": geometry,
        "selected_geometry_sha256": canonical_sha256(geometry),
        "selected_source_quality": source_quality,
        "selected_source_quality_sha256": canonical_sha256(source_quality),
        "geometry_review": review,
        "decision": decision,
        "consequences": {
            "continue_to_non_btc_60_and_btc_150": passed,
            "manual_diagnosis_required": not passed,
            "outcome_access_allowed_at_30": False,
            "model_fit_allowed_at_30": False,
            "automatic_feature_or_threshold_change_allowed": False,
            "automatic_activation_allowed": False,
            "automatic_promotion_allowed": False,
            "real_trading_allowed": False,
        },
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "feature_selection_performed": False,
        "automatic_scoring": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }


def wrap_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "artifact_version": ARTIFACT_VERSION,
        "contract_id": CONTRACT_ID,
        "contract_sha256": CONTRACT_SHA256,
        "payload_sha256": canonical_sha256(payload),
        "payload": dict(payload),
    }


def validate_artifact(artifact: Mapping[str, Any]) -> None:
    payload = artifact.get("payload")
    if not isinstance(payload, Mapping) or (
        artifact.get("artifact_version") != ARTIFACT_VERSION
        or artifact.get("contract_id") != CONTRACT_ID
        or artifact.get("contract_sha256") != CONTRACT_SHA256
        or artifact.get("payload_sha256") != canonical_sha256(payload)
    ):
        raise ValueError("geometry_freeze_artifact_identity_or_sha_invalid")
    geometry = payload.get("selected_geometry")
    source_quality = payload.get("selected_source_quality")
    review = payload.get("geometry_review")
    consequences = payload.get("consequences")
    decision = payload.get("decision")
    if (
        set(payload) != ACTUAL_PAYLOAD_KEYS
        or not _valid_frozen_at(payload.get("frozen_at"))
        or payload.get("selection")
        != "EARLIEST_30_COMPLETE_RECONSTRUCTABLE_WINDOWS"
        or payload.get("source_design_id") != DESIGN_ID
        or payload.get("source_design_sha256") != DESIGN_SHA256
        or payload.get("geometry_protocol_id") != GEOMETRY_PROTOCOL_ID
        or payload.get("geometry_protocol_sha256") != GEOMETRY_PROTOCOL_SHA256
        or payload.get("successor_charter_id") != SUCCESSOR_CHARTER_ID
        or payload.get("successor_charter_sha256") != SUCCESSOR_CHARTER_SHA256
        or payload.get("successor_evaluation_protocol_id")
        != SUCCESSOR_EVALUATION_PROTOCOL_ID
        or payload.get("successor_evaluation_protocol_sha256")
        != SUCCESSOR_EVALUATION_PROTOCOL_SHA256
        or decision not in {
            "PASS_CONTINUE_PROSPECTIVE_COLLECTION_NO_MODEL",
            "GEOMETRY_REVIEW_REQUIRED_NO_AUTOMATIC_ACTION",
        }
        or not isinstance(geometry, Mapping)
        or int(dict(geometry.get("ALL_SEVEN") or {}).get("rows") or 0) != 210
        or int(dict(geometry.get("BTC") or {}).get("rows") or 0) != 30
        or int(dict(geometry.get("NON_BTC_TRANSFER") or {}).get("rows") or 0)
        != 180
        or not isinstance(source_quality, Mapping)
        or source_quality.get("status") != "PASS_ALL_CREDITED_COMPLETE_ROWS"
        or int(source_quality.get("credited_complete_rows") or 0) != 210
        or int(source_quality.get("evidence_parse_failures", -1)) != 0
        or int(source_quality.get("integrity_breaches", -1)) != 0
        or source_quality.get("outcome_labels_read") is not False
        or source_quality.get("source_thresholds_from_frozen_design") is not True
        or source_quality.get("thresholds_selected_from_outcomes") is not False
        or not _valid_selected_evidence_identity(source_quality)
        or not _valid_contract_identity(source_quality)
        or not isinstance(review, Mapping)
        or review.get("protocol_id") != GEOMETRY_PROTOCOL_ID
        or review.get("protocol_sha256") != GEOMETRY_PROTOCOL_SHA256
        or review.get("review_ready") is not True
        or review.get("status") != decision
        or review.get("outcome_labels_read") is not False
        or review.get("model_fit_performed") is not False
        or not isinstance(consequences, Mapping)
        or consequences.get("continue_to_non_btc_60_and_btc_150")
        is not (
            decision == "PASS_CONTINUE_PROSPECTIVE_COLLECTION_NO_MODEL"
        )
        or consequences.get("manual_diagnosis_required")
        is not (
            decision == "GEOMETRY_REVIEW_REQUIRED_NO_AUTOMATIC_ACTION"
        )
        or any(consequences.get(key) is not False for key in (
            "outcome_access_allowed_at_30", "model_fit_allowed_at_30",
            "automatic_feature_or_threshold_change_allowed",
            "automatic_activation_allowed", "automatic_promotion_allowed",
            "real_trading_allowed",
        ))
        or
        payload.get("selected_close_times_sha256")
        != canonical_sha256(payload.get("selected_complete_close_times"))
        or payload.get("selected_geometry_sha256")
        != canonical_sha256(payload.get("selected_geometry"))
        or payload.get("selected_source_quality_sha256")
        != canonical_sha256(payload.get("selected_source_quality"))
        or int(payload.get("selected_rows") or 0) != 210
        or not _valid_selected_close_times(
            payload.get("selected_complete_close_times")
        )
        or payload.get("outcome_columns_selected") is not False
        or payload.get("outcome_labels_read") is not False
        or payload.get("model_fit_performed") is not False
        or payload.get("feature_selection_performed") is not False
        or payload.get("automatic_scoring") is not False
        or payload.get("automatic_promotion") is not False
        or payload.get("real_trading_allowed") is not False
    ):
        raise ValueError("geometry_freeze_artifact_payload_invalid")
    evidence = {
        "complete_close_windows": 30,
        "complete_close_times": list(payload["selected_complete_close_times"]),
        "rows": 210,
        "cohorts": dict(geometry),
        "source_quality": dict(source_quality),
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
    }
    if _canonical_bytes(review) != _canonical_bytes(
        _expected_review_from_evidence(evidence)
    ):
        raise ValueError("geometry_freeze_artifact_review_recomputation_mismatch")


def _evidence_identity(payload: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        payload.get("selected_close_times_sha256"),
        payload.get("selected_geometry_sha256"),
        payload.get("selected_source_quality_sha256"),
        payload.get("decision"),
    )


def freeze_report(
    report: Mapping[str, Any], artifact_path: Path, *, dry_run: bool = False,
    frozen_at: str | None = None,
) -> dict[str, Any]:
    if artifact_path.exists():
        artifact = _load_object(
            artifact_path, "geometry_freeze_existing_artifact_root_not_object",
        )
        validate_artifact(artifact)
        current = build_payload(
            report, frozen_at=str(dict(artifact["payload"])["frozen_at"]),
        )
        if _evidence_identity(current) != _evidence_identity(artifact["payload"]):
            raise ValueError("geometry_freeze_existing_artifact_evidence_mismatch")
        return {
            "status": "EXISTING_IMMUTABLE_ARTIFACT_VERIFIED",
            "artifact_written": False,
            "artifact_path": str(artifact_path),
            "payload_sha256": artifact["payload_sha256"],
            "decision": dict(artifact["payload"])["decision"],
        }
    if int(report.get("complete_seven_asset_close_windows") or 0) < 30:
        return {
            "status": "WAITING_FOR_30_COMPLETE_WINDOWS",
            "artifact_written": False,
            "artifact_path": str(artifact_path),
            "complete_windows": int(
                report.get("complete_seven_asset_close_windows") or 0
            ),
            "windows_remaining": max(
                0, 30 - int(
                    report.get("complete_seven_asset_close_windows") or 0
                ),
            ),
            "outcome_labels_read": False,
            "model_fit_performed": False,
        }
    payload = build_payload(report, frozen_at=frozen_at)
    artifact = wrap_payload(payload)
    if dry_run:
        return {
            "status": "READY_DRY_RUN_NO_ARTIFACT_WRITTEN",
            "artifact_written": False,
            "artifact_path": str(artifact_path),
            "payload_sha256": artifact["payload_sha256"],
            "decision": payload["decision"],
            "outcome_labels_read": False,
            "model_fit_performed": False,
        }
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    with artifact_path.open("x", encoding="utf-8") as handle:
        json.dump(artifact, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return {
        "status": "IMMUTABLE_GEOMETRY_ARTIFACT_WRITTEN",
        "artifact_written": True,
        "artifact_path": str(artifact_path),
        "payload_sha256": artifact["payload_sha256"],
        "decision": payload["decision"],
        "outcome_labels_read": False,
        "model_fit_performed": False,
    }


def build_live_report(
    *, database_path: Path = DEFAULT_DB, design_path: Path = DEFAULT_DESIGN,
    geometry_protocol_path: Path = DEFAULT_GEOMETRY_PROTOCOL,
) -> dict[str, Any]:
    if not OUTCOME_COLUMNS.isdisjoint(FEATURE_SELECT_COLUMNS):
        raise AssertionError("geometry_freeze_feature_projection_contains_outcome")
    design = _load_object(design_path, "geometry_freeze_design_root_not_object")
    protocol = _load_object(
        geometry_protocol_path, "geometry_freeze_geometry_protocol_root_not_object",
    )
    validate_design(design)
    validate_geometry_protocol(protocol)
    return build_report(load_feature_rows(database_path), design, protocol)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", default=str(DEFAULT_CONTRACT))
    parser.add_argument("--strategy-db", default=str(DEFAULT_DB))
    parser.add_argument("--design", default=str(DEFAULT_DESIGN))
    parser.add_argument(
        "--geometry-protocol", default=str(DEFAULT_GEOMETRY_PROTOCOL),
    )
    parser.add_argument("--artifact", default=str(DEFAULT_ARTIFACT))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    contract = load_contract(Path(args.contract))
    validate_contract(contract)
    report = build_live_report(
        database_path=Path(args.strategy_db), design_path=Path(args.design),
        geometry_protocol_path=Path(args.geometry_protocol),
    )
    result = freeze_report(
        report, Path(args.artifact), dry_run=bool(args.dry_run),
    )
    print(json.dumps({
        "contract_id": CONTRACT_ID,
        "contract_sha256": CONTRACT_SHA256,
        **result,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
