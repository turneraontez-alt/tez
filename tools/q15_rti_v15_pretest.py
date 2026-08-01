"""Durable one-shot V15 development/calibration evaluation.

This module has no database, settlement, notification, promotion, or order
capability.  A caller supplies an explicit callback for the exact pretest row
IDs authorized by a ready outcome-blind audit seal.  The append-only
reservation is written before that callback can run.  The untouched-test IDs
remain sealed regardless of whether the pretest gates pass.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from q15_upgrade.strategy_bots import (
    rti_microstructure_v15_audit_identity as audit_identity,
)
from q15_upgrade.strategy_bots.rti_microstructure_v15_identity import (
    DESIGN_ID,
    DESIGN_SHA256,
    EVALUATION_PROTOCOL_ID,
    EVALUATION_PROTOCOL_SHA256,
)
from tools import q15_rti_v15_untouched_test as untouched
from tools.q15_rti_v15_label_evidence import validate_label_evidence
from tools import q15_rti_v15_walk_forward as walk


PRETEST_RUNNER_VERSION = audit_identity.PRETEST_RUNNER_VERSION
PRETEST_STATE_VERSION = audit_identity.PRETEST_STATE_VERSION
CONFIRMATION_PHRASE = "OPEN_V15_TRAIN_CAL_LABELS_ONCE"
RESERVED_STATUS = "TRAIN_CAL_LABEL_ACCESS_RESERVED"
PASS_STATUS = "PRETEST_GATES_PASSED_UNTOUCHED_TEST_REMAINS_SEALED"
WALK_REJECT_STATUS = (
    "WALK_FORWARD_GATE_FAILED_UNTOUCHED_TEST_REMAINS_SEALED"
)
CALIBRATION_REJECT_STATUS = (
    "CALIBRATION_GATE_FAILED_UNTOUCHED_TEST_REMAINS_SEALED"
)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sealed(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result.pop("state_sha256", None)
    result["state_sha256"] = _canonical_sha256(result)
    return result


def _validate_sealed(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    expected = str(result.pop("state_sha256", ""))
    if expected != _canonical_sha256(result):
        raise ValueError("v15_pretest_state_sha256_invalid")
    result["state_sha256"] = expected
    return result


def _read_sealed(path: Path) -> dict[str, Any]:
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("v15_pretest_state_unreadable") from exc
    if not isinstance(decoded, Mapping):
        raise ValueError("v15_pretest_state_root_not_object")
    return _validate_sealed(decoded)


def _write_exclusive(
    path: Path, payload: Mapping[str, Any],
) -> dict[str, Any]:
    sealed = _sealed(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(sealed, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
    return sealed


def _now_iso(timestamp: str | None) -> str:
    if timestamp is None:
        return datetime.now(timezone.utc).isoformat()
    try:
        parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("v15_pretest_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("v15_pretest_timestamp_invalid")
    return parsed.astimezone(timezone.utc).isoformat()


def result_path_for(reservation_path: Path) -> Path:
    suffix = reservation_path.suffix or ".json"
    return reservation_path.with_name(
        f"{reservation_path.stem}.result{suffix}"
    )


def _expected_binding(
    *,
    seal: Mapping[str, Any],
    cohort: str,
    pretest_rows: Sequence[Mapping[str, Any]],
    test_rows: Sequence[Mapping[str, Any]],
    label_evidence_required: bool = False,
) -> dict[str, Any]:
    return {
        "pretest_runner_version": PRETEST_RUNNER_VERSION,
        "design_id": DESIGN_ID,
        "design_sha256": DESIGN_SHA256,
        "evaluation_protocol_id": EVALUATION_PROTOCOL_ID,
        "evaluation_protocol_sha256": EVALUATION_PROTOCOL_SHA256,
        "audit_seal_sha256": str(seal["seal_sha256"]),
        "cohort": cohort,
        "selected_feature_evidence_sha256": str(
            seal["selected_feature_evidence_sha256"]
        ),
        "pretest_row_ids_sha256": untouched._hash_ids(pretest_rows),
        "pretest_close_times_sha256": untouched._hash_times(pretest_rows),
        "untouched_test_row_ids_sha256": untouched._hash_ids(test_rows),
        "untouched_test_close_times_sha256": untouched._hash_times(test_rows),
        "label_evidence_required": bool(label_evidence_required),
    }


def _validate_existing_reservation(
    reservation: Mapping[str, Any],
    expected_binding: Mapping[str, Any],
) -> None:
    if (
        reservation.get("state_version") != PRETEST_STATE_VERSION
        or reservation.get("status") != RESERVED_STATUS
    ):
        raise ValueError("v15_pretest_reservation_status_invalid")
    for key, value in expected_binding.items():
        if reservation.get(key) != value:
            raise ValueError(f"v15_pretest_reservation_binding_mismatch:{key}")
    if (
        reservation.get("pretest_labels_read") is not False
        or reservation.get("walk_forward_scoring_performed") is not False
        or reservation.get("calibration_scoring_performed") is not False
        or reservation.get("untouched_test_labels_read") is not False
        or reservation.get("untouched_test_scoring_performed") is not False
        or reservation.get("paper_artifact_created") is not False
        or reservation.get("notification_eligible") is not False
        or reservation.get("automatic_promotion") is not False
        or reservation.get("real_trading_allowed") is not False
    ):
        raise ValueError("v15_pretest_reservation_safety_invalid")


def _validate_existing_result(
    result: Mapping[str, Any],
    reservation: Mapping[str, Any],
) -> None:
    walk_report = result.get("walk_forward_report")
    calibration_report = result.get("calibration_report")
    label_rows = result.get("pretest_label_rows")
    label_evidence = result.get("label_read_evidence")
    if (
        not isinstance(walk_report, Mapping)
        or not isinstance(label_rows, list)
        or not label_rows
    ):
        raise ValueError("v15_pretest_final_result_invalid")
    try:
        label_pairs = sorted(
            [int(item["id"]), int(item["label_yes"])]
            for item in label_rows
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("v15_pretest_final_result_invalid") from exc
    label_ids = [row_id for row_id, label in label_pairs]
    if (
        len(set(label_ids)) != len(label_ids)
        or any(label not in {0, 1} for _, label in label_pairs)
    ):
        raise ValueError("v15_pretest_final_result_invalid")
    status = result.get("status")
    walk_passed = walk_report.get("gate_met") is True
    calibration_passed = (
        isinstance(calibration_report, Mapping)
        and calibration_report.get("gate_met") is True
    )
    evidence_required = (
        reservation.get("label_evidence_required") is True
    )
    if evidence_required:
        if not isinstance(label_evidence, Mapping):
            raise ValueError("v15_pretest_final_result_invalid")
        try:
            validated_evidence = validate_label_evidence(
                type(
                    "_StoredVerifiedLabels",
                    (dict,),
                    {"audit_evidence": dict(label_evidence)},
                )(dict(label_pairs)),
                dict(label_pairs),
                label_ids,
                required=True,
                stage="pretest",
            )
        except ValueError as exc:
            raise ValueError("v15_pretest_final_result_invalid") from exc
        if (
            validated_evidence is None
            or result.get("label_read_evidence_sha256")
            != validated_evidence.get("evidence_sha256")
        ):
            raise ValueError("v15_pretest_final_result_invalid")
    elif (
        label_evidence is not None
        or result.get("label_read_evidence_sha256") is not None
    ):
        raise ValueError("v15_pretest_final_result_invalid")
    expected_status = (
        PASS_STATUS
        if walk_passed and calibration_passed
        else (
            WALK_REJECT_STATUS
            if not walk_passed
            else CALIBRATION_REJECT_STATUS
        )
    )
    if (
        result.get("state_version") != PRETEST_STATE_VERSION
        or result.get("pretest_runner_version") != PRETEST_RUNNER_VERSION
        or status != expected_status
        or result.get("reservation_state_sha256")
        != reservation.get("state_sha256")
        or result.get("audit_seal_sha256")
        != reservation.get("audit_seal_sha256")
        or result.get("cohort") != reservation.get("cohort")
        or result.get("pretest_row_ids_sha256")
        != reservation.get("pretest_row_ids_sha256")
        or _canonical_sha256(tuple(sorted(label_ids)))
        != reservation.get("pretest_row_ids_sha256")
        or result.get("pretest_labels_sha256")
        != _canonical_sha256(label_pairs)
        or result.get("untouched_test_row_ids_sha256")
        != reservation.get("untouched_test_row_ids_sha256")
        or result.get("walk_forward_report_sha256")
        != _canonical_sha256(dict(walk_report))
        or (
            calibration_report is None
            and result.get("calibration_report_sha256") is not None
        )
        or (
            isinstance(calibration_report, Mapping)
            and result.get("calibration_report_sha256")
            != _canonical_sha256(dict(calibration_report))
        )
        or (not walk_passed and calibration_report is not None)
        or result.get("pretest_labels_read_once") is not True
        or result.get("untouched_test_labels_read") is not False
        or result.get("untouched_test_scoring_performed") is not False
        or result.get("paper_artifact_created") is not False
        or result.get("notification_eligible") is not False
        or result.get("automatic_promotion") is not False
        or result.get("real_trading_allowed") is not False
        or int(walk_report.get("untouched_test_rows_used", -1)) != 0
        or walk_report.get("input_rows_sha256")
        != reservation.get("pretest_row_ids_sha256")
        or (
            isinstance(calibration_report, Mapping)
            and (
                int(
                    calibration_report.get(
                        "untouched_test_rows_used", -1
                    )
                ) != 0
                or calibration_report.get("input_rows_sha256")
                != reservation.get("pretest_row_ids_sha256")
            )
        )
    ):
        raise ValueError("v15_pretest_final_result_invalid")


def run_pretest_once(
    *,
    seal: Mapping[str, Any],
    selected_feature_rows: Sequence[Mapping[str, Any]],
    design: Mapping[str, Any],
    protocol: Mapping[str, Any],
    cohort: str,
    reservation_path: Path,
    confirmation: str,
    read_pretest_labels: Callable[
        [Sequence[int]], Mapping[int, int]
    ],
    require_label_evidence: bool = False,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Evaluate one sealed pretest cohort once; never access test labels."""
    untouched._validate_design_protocol(design, protocol)
    if cohort not in walk.COHORT_ASSETS:
        raise ValueError("v15_pretest_unsupported_cohort")
    _, pretest_unlabeled, test_unlabeled = untouched._prepare_projected_rows(
        selected_feature_rows,
        seal=seal,
        cohort=cohort,
        protocol=protocol,
    )
    expected_binding = _expected_binding(
        seal=seal,
        cohort=cohort,
        pretest_rows=pretest_unlabeled,
        test_rows=test_unlabeled,
        label_evidence_required=require_label_evidence,
    )
    result_path = result_path_for(reservation_path)
    if reservation_path.exists():
        reservation = _read_sealed(reservation_path)
        _validate_existing_reservation(reservation, expected_binding)
        if result_path.exists():
            result = _read_sealed(result_path)
            _validate_existing_result(result, reservation)
            return {
                "status": "ALREADY_FINALIZED_NO_REREAD",
                "pretest_labels_read_this_call": False,
                "reservation": reservation,
                "result": result,
            }
        return {
            "status": "AMBIGUOUS_RESERVED_NO_REREAD",
            "pretest_labels_read_this_call": False,
            "reservation": reservation,
            "result": None,
        }
    if result_path.exists():
        raise ValueError("v15_pretest_result_exists_without_reservation")
    if confirmation != CONFIRMATION_PHRASE:
        raise ValueError("v15_pretest_explicit_one_shot_confirmation_required")

    reservation = _write_exclusive(reservation_path, {
        **expected_binding,
        "state_version": PRETEST_STATE_VERSION,
        "status": RESERVED_STATUS,
        "reserved_at": _now_iso(timestamp),
        "pretest_labels_read": False,
        "walk_forward_scoring_performed": False,
        "calibration_scoring_performed": False,
        "untouched_test_labels_read": False,
        "untouched_test_scoring_performed": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    })

    pretest_ids = tuple(
        sorted(int(row["id"]) for row in pretest_unlabeled)
    )
    raw_labels = read_pretest_labels(pretest_ids)
    labels = untouched._validated_labels(
        raw_labels,
        pretest_ids,
        stage="pretest",
    )
    label_evidence = validate_label_evidence(
        raw_labels,
        labels,
        pretest_ids,
        required=require_label_evidence,
        stage="pretest",
    )
    pretest = untouched._attach_labels(pretest_unlabeled, labels)
    walk_report = walk.evaluate_walk_forward(
        pretest,
        cohort=cohort,
        design=design,
        protocol=protocol,
    )
    calibration_report = None
    if walk_report.get("gate_met") is True:
        calibration_report = walk.evaluate_calibration(
            pretest,
            cohort=cohort,
            design=design,
            protocol=protocol,
            walk_forward_report=walk_report,
        )
    passed = (
        walk_report.get("gate_met") is True
        and isinstance(calibration_report, Mapping)
        and calibration_report.get("gate_met") is True
    )
    status = (
        PASS_STATUS
        if passed
        else (
            WALK_REJECT_STATUS
            if walk_report.get("gate_met") is not True
            else CALIBRATION_REJECT_STATUS
        )
    )
    result = _write_exclusive(result_path, {
        "state_version": PRETEST_STATE_VERSION,
        "pretest_runner_version": PRETEST_RUNNER_VERSION,
        "status": status,
        "finalized_at": _now_iso(timestamp),
        "reservation_state_sha256": reservation["state_sha256"],
        "audit_seal_sha256": reservation["audit_seal_sha256"],
        "cohort": cohort,
        "pretest_row_ids_sha256": reservation[
            "pretest_row_ids_sha256"
        ],
        "untouched_test_row_ids_sha256": reservation[
            "untouched_test_row_ids_sha256"
        ],
        "pretest_labels_sha256": _canonical_sha256(sorted(
            [int(row_id), int(label)]
            for row_id, label in labels.items()
        )),
        "pretest_label_rows": [
            {"id": int(row_id), "label_yes": int(label)}
            for row_id, label in sorted(labels.items())
        ],
        "label_read_evidence": label_evidence,
        "label_read_evidence_sha256": (
            label_evidence["evidence_sha256"]
            if label_evidence is not None
            else None
        ),
        "walk_forward_report": walk_report,
        "walk_forward_report_sha256": _canonical_sha256(walk_report),
        "calibration_report": calibration_report,
        "calibration_report_sha256": (
            _canonical_sha256(calibration_report)
            if calibration_report is not None
            else None
        ),
        "pretest_labels_read_once": True,
        "untouched_test_labels_read": False,
        "untouched_test_scoring_performed": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    })
    _validate_existing_result(result, reservation)
    return {
        "status": status,
        "pretest_labels_read_this_call": True,
        "reservation": reservation,
        "result": result,
    }
