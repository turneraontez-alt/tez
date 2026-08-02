"""Append-only one-shot runner for V18's first prospective review.

This module has no database, network, Telegram, promotion, or order capability.
It durably reserves the exact sealed control population before invoking the
supplied label callback.  A reserved-but-unfinalized run is permanently
ambiguous and can never reread outcomes.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from q15_upgrade.strategy_bots import rti_microstructure_v18 as v18
from q15_upgrade.strategy_bots import rti_microstructure_v18_audit_identity as identity
from q15_upgrade.strategy_bots import rti_microstructure_v18_identity as v18_identity
from tools import q15_rti_v18_first_review_evaluator as evaluator
from tools import q15_rti_v18_prospective_seal as prospective_seal
from tools.q15_rti_microstructure_freeze import OUTCOME_COLUMNS
from tools.q15_rti_v15_label_evidence import validate_label_evidence


RESERVED_STATUS = "V18_FIRST_REVIEW_LABEL_ACCESS_RESERVED"
PASS_STATUS = "V18_FIRST_REVIEW_GATE_PASSED_MANUAL_CONSIDERATION_ONLY"
REJECT_STATUS = "V18_FIRST_REVIEW_GATE_FAILED_NO_PAPER_NOTIFICATIONS"
COHORT = "NON_BTC_TRANSFER"


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _sealed(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result.pop("state_sha256", None)
    result["state_sha256"] = _canonical_sha256(result)
    return result


def _validate_sealed(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    expected = str(result.pop("state_sha256", ""))
    if expected != _canonical_sha256(result):
        raise ValueError("v18_first_review_state_sha256_invalid")
    result["state_sha256"] = expected
    return result


def _read_sealed(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("v18_first_review_state_unreadable") from exc
    if not isinstance(value, Mapping):
        raise ValueError("v18_first_review_state_root_not_object")
    return _validate_sealed(value)


def _write_exclusive(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    result = _sealed(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
    return result


def _now_iso(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("v18_first_review_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("v18_first_review_timestamp_invalid")
    return parsed.astimezone(timezone.utc).isoformat()


def result_path_for(reservation_path: Path) -> Path:
    suffix = reservation_path.suffix or ".json"
    return reservation_path.with_name(f"{reservation_path.stem}.result{suffix}")


def _row_ids(rows: Sequence[Mapping[str, Any]]) -> tuple[int, ...]:
    return tuple(sorted(int(row["id"]) for row in rows))


def _candidate_rows(
    control_rows: Sequence[Mapping[str, Any]], candidate_ids: Sequence[int],
) -> list[dict[str, Any]]:
    wanted = {int(value) for value in candidate_ids}
    return [dict(row) for row in control_rows if int(row["id"]) in wanted]


def _expected_binding(
    *,
    seal: Mapping[str, Any],
    control_rows: Sequence[Mapping[str, Any]],
    candidate_ids: Sequence[int],
    label_evidence_required: bool,
) -> dict[str, Any]:
    control_ids = _row_ids(control_rows)
    candidate = _candidate_rows(control_rows, candidate_ids)
    candidate_row_ids = _row_ids(candidate)
    return {
        "runner_version": identity.RUNNER_VERSION,
        "evaluator_version": identity.EVALUATOR_VERSION,
        "audit_contract_id": identity.AUDIT_CONTRACT_ID,
        "audit_contract_sha256": identity.AUDIT_CONTRACT_SHA256,
        "protocol_id": v18_identity.PROTOCOL_ID,
        "protocol_sha256": v18_identity.PROTOCOL_SHA256,
        "prospective_seal_sha256": str(seal["seal_sha256"]),
        "selected_candidate_feature_evidence_sha256": str(
            seal["selected_candidate_feature_evidence_sha256"]
        ),
        "selected_control_feature_evidence_sha256": str(
            seal["selected_control_feature_evidence_sha256"]
        ),
        "cohort": COHORT,
        "selected_complete_close_windows": int(
            seal["selected_complete_close_windows"]
        ),
        "candidate_rows": len(candidate_row_ids),
        "control_rows": len(control_ids),
        "candidate_row_ids_sha256": _canonical_sha256(candidate_row_ids),
        "control_row_ids_sha256": _canonical_sha256(control_ids),
        "candidate_feature_evidence_sha256": _canonical_sha256(candidate),
        "control_feature_evidence_sha256": _canonical_sha256(
            [dict(row) for row in control_rows]
        ),
        "label_evidence_required": bool(label_evidence_required),
    }


def _validate_reservation(
    reservation: Mapping[str, Any], expected: Mapping[str, Any],
) -> None:
    if (
        reservation.get("state_version") != identity.STATE_VERSION
        or reservation.get("status") != RESERVED_STATUS
    ):
        raise ValueError("v18_first_review_reservation_status_invalid")
    for key, value in expected.items():
        if reservation.get(key) != value:
            raise ValueError(f"v18_first_review_reservation_binding_mismatch:{key}")
    if any(reservation.get(key) is not False for key in (
        "outcome_labels_read", "model_fit_performed",
        "probability_scoring_performed", "paper_artifact_created",
        "notification_eligible", "telegram_allowed", "automatic_promotion",
        "real_trading_allowed",
    )):
        raise ValueError("v18_first_review_reservation_safety_invalid")


def _validate_result(
    result: Mapping[str, Any], reservation: Mapping[str, Any],
) -> None:
    report = result.get("first_review_report")
    label_rows = result.get("control_label_rows")
    if not isinstance(report, Mapping) or not isinstance(label_rows, list):
        raise ValueError("v18_first_review_result_invalid")
    try:
        pairs = sorted(
            [int(item["id"]), int(item["label_yes"])] for item in label_rows
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("v18_first_review_result_invalid") from exc
    ids = [row_id for row_id, _label in pairs]
    expected_status = PASS_STATUS if report.get("gate_met") is True else REJECT_STATUS
    evidence = result.get("label_read_evidence")
    if reservation.get("label_evidence_required") is True:
        if not isinstance(evidence, Mapping):
            raise ValueError("v18_first_review_result_invalid")
        try:
            verified = validate_label_evidence(
                type(
                    "_StoredVerifiedLabels",
                    (dict,),
                    {"audit_evidence": dict(evidence)},
                )(dict(pairs)),
                dict(pairs),
                ids,
                required=True,
                stage="v18_first_review",
            )
        except ValueError as exc:
            raise ValueError("v18_first_review_result_invalid") from exc
        if (
            verified is None
            or result.get("label_read_evidence_sha256")
            != verified.get("evidence_sha256")
        ):
            raise ValueError("v18_first_review_result_invalid")
    elif evidence is not None or result.get("label_read_evidence_sha256") is not None:
        raise ValueError("v18_first_review_result_invalid")
    if (
        result.get("state_version") != identity.STATE_VERSION
        or result.get("runner_version") != identity.RUNNER_VERSION
        or result.get("status") != expected_status
        or result.get("reservation_state_sha256") != reservation.get("state_sha256")
        or result.get("prospective_seal_sha256")
        != reservation.get("prospective_seal_sha256")
        or len(ids) != int(reservation["control_rows"])
        or len(set(ids)) != len(ids)
        or any(label not in {0, 1} for _row_id, label in pairs)
        or _canonical_sha256(tuple(sorted(ids)))
        != reservation.get("control_row_ids_sha256")
        or result.get("control_labels_sha256") != _canonical_sha256(pairs)
        or result.get("first_review_report_sha256")
        != _canonical_sha256(dict(report))
        or report.get("evaluator_version") != identity.EVALUATOR_VERSION
        or report.get("audit_contract_id") != identity.AUDIT_CONTRACT_ID
        or report.get("audit_contract_sha256") != identity.AUDIT_CONTRACT_SHA256
        or report.get("protocol_id") != v18_identity.PROTOCOL_ID
        or report.get("protocol_sha256") != v18_identity.PROTOCOL_SHA256
        or report.get("prospective_seal_sha256")
        != reservation.get("prospective_seal_sha256")
        or report.get("cohort") != COHORT
        or int(report.get("control_input_rows", -1))
        != int(reservation["control_rows"])
        or int(report.get("candidate_input_rows", -1))
        != int(reservation["candidate_rows"])
        or int(report.get("selected_complete_close_windows", -1))
        != int(reservation["selected_complete_close_windows"])
        or report.get("control_row_ids_sha256")
        != reservation.get("control_row_ids_sha256")
        or report.get("candidate_row_ids_sha256")
        != reservation.get("candidate_row_ids_sha256")
        or report.get("outcome_labels_read") is not True
        or report.get("model_fit_performed") is not False
        or report.get("probability_scoring_performed") is not False
        or report.get("paper_artifact_created") is not False
        or report.get("notification_eligible") is not False
        or report.get("automatic_promotion") is not False
        or report.get("real_trading_allowed") is not False
        or result.get("control_labels_read_once") is not True
        or result.get("manual_consideration_eligible")
        != (expected_status == PASS_STATUS)
        or any(result.get(key) is not False for key in (
            "model_fit_performed", "probability_scoring_performed",
            "paper_artifact_created", "notification_eligible",
            "telegram_allowed", "automatic_promotion", "real_trading_allowed",
        ))
    ):
        raise ValueError("v18_first_review_result_invalid")


def run_first_review_once(
    *,
    seal: Mapping[str, Any],
    control_rows: Sequence[Mapping[str, Any]],
    candidate_ids: Sequence[int],
    reservation_path: Path,
    confirmation: str,
    read_control_labels: Callable[[Sequence[int]], Mapping[int, int]],
    contract: Mapping[str, Any] | None = None,
    protocol: Mapping[str, Any] | None = None,
    require_label_evidence: bool = False,
    timestamp: str | None = None,
) -> dict[str, Any]:
    prospective_seal.validate_seal(seal)
    contract = dict(contract or prospective_seal.load_contract())
    protocol = dict(protocol or v18.load_protocol())
    if contract != prospective_seal.load_contract() or protocol != v18.load_protocol():
        raise ValueError("v18_first_review_runner_contract_identity_invalid")
    controls = [dict(row) for row in control_rows]
    requested_candidate_ids = tuple(sorted(int(value) for value in candidate_ids))
    if (
        not controls
        or len(_row_ids(controls)) != len(set(_row_ids(controls)))
        or len(requested_candidate_ids) != len(set(requested_candidate_ids))
        or any(str(row.get("asset") or "").upper() == "BTC" for row in controls)
        or any(OUTCOME_COLUMNS.intersection(row) for row in controls)
        or any("label_yes" in row for row in controls)
    ):
        raise ValueError("v18_first_review_runner_input_identity_invalid")
    expected = _expected_binding(
        seal=seal,
        control_rows=controls,
        candidate_ids=requested_candidate_ids,
        label_evidence_required=require_label_evidence,
    )
    if (
        expected["candidate_row_ids_sha256"]
        != seal.get("selected_candidate_row_ids_sha256")
        or expected["control_row_ids_sha256"]
        != seal.get("selected_control_row_ids_sha256")
        or expected["candidate_feature_evidence_sha256"]
        != seal.get("selected_candidate_feature_evidence_sha256")
        or expected["control_feature_evidence_sha256"]
        != seal.get("selected_control_feature_evidence_sha256")
        or expected["candidate_rows"] != int(seal["selected_candidate_picks"])
        or expected["control_rows"] != int(seal["selected_control_picks"])
        or expected["candidate_rows"] < 30
        or expected["selected_complete_close_windows"] < 150
    ):
        raise ValueError("v18_first_review_runner_seal_binding_mismatch")
    result_path = result_path_for(reservation_path)
    if reservation_path.exists():
        reservation = _read_sealed(reservation_path)
        _validate_reservation(reservation, expected)
        if result_path.exists():
            result = _read_sealed(result_path)
            _validate_result(result, reservation)
            return {
                "status": "ALREADY_FINALIZED_NO_REREAD",
                "control_labels_read_this_call": False,
                "reservation": reservation,
                "result": result,
            }
        return {
            "status": "AMBIGUOUS_RESERVED_NO_REREAD",
            "control_labels_read_this_call": False,
            "reservation": reservation,
            "result": None,
        }
    if result_path.exists():
        raise ValueError("v18_first_review_result_exists_without_reservation")
    if confirmation != identity.CONFIRMATION_PHRASE:
        raise ValueError("v18_first_review_explicit_one_shot_confirmation_required")

    reservation = _write_exclusive(reservation_path, {
        **expected,
        "state_version": identity.STATE_VERSION,
        "status": RESERVED_STATUS,
        "reserved_at": _now_iso(timestamp),
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "telegram_allowed": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    })

    ids = _row_ids(controls)
    raw_labels = read_control_labels(ids)
    try:
        labels = {int(key): int(value) for key, value in raw_labels.items()}
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("v18_first_review_labels_invalid") from exc
    if set(labels) != set(ids) or any(value not in {0, 1} for value in labels.values()):
        raise ValueError("v18_first_review_labels_invalid")
    evidence = validate_label_evidence(
        raw_labels,
        labels,
        ids,
        required=require_label_evidence,
        stage="v18_first_review",
    )
    labeled = [
        {**row, "label_yes": int(labels[int(row["id"])])}
        for row in controls
    ]
    report = evaluator.evaluate_first_review(
        labeled,
        candidate_ids=requested_candidate_ids,
        seal=seal,
        contract=contract,
    )
    status = PASS_STATUS if report.get("gate_met") is True else REJECT_STATUS
    pairs = sorted([int(row_id), int(label)] for row_id, label in labels.items())
    result = _write_exclusive(result_path, {
        "state_version": identity.STATE_VERSION,
        "runner_version": identity.RUNNER_VERSION,
        "status": status,
        "finalized_at": _now_iso(timestamp),
        "reservation_state_sha256": reservation["state_sha256"],
        "prospective_seal_sha256": reservation["prospective_seal_sha256"],
        "audit_contract_sha256": identity.AUDIT_CONTRACT_SHA256,
        "cohort": COHORT,
        "control_labels_sha256": _canonical_sha256(pairs),
        "control_label_rows": [
            {"id": row_id, "label_yes": label} for row_id, label in pairs
        ],
        "label_read_evidence": evidence,
        "label_read_evidence_sha256": (
            evidence["evidence_sha256"] if evidence is not None else None
        ),
        "first_review_report": report,
        "first_review_report_sha256": _canonical_sha256(report),
        "control_labels_read_once": True,
        "manual_consideration_eligible": status == PASS_STATUS,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "telegram_allowed": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    })
    _validate_result(result, reservation)
    return {
        "status": status,
        "control_labels_read_this_call": True,
        "reservation": reservation,
        "result": result,
    }
