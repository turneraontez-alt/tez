"""Append-only one-shot runner for V17's sealed development audit.

This module has no database, network, Telegram, promotion, or order capability.
The exclusive reservation is durable before the supplied label callback runs.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from q15_upgrade.strategy_bots import rti_microstructure_v17_audit_identity as identity
from q15_upgrade.strategy_bots import rti_microstructure_v17_identity as v17_identity
from tools import q15_rti_v17_development_evaluator as evaluator
from tools.q15_rti_v15_label_evidence import validate_label_evidence


RESERVED_STATUS = "V17_DEVELOPMENT_LABEL_ACCESS_RESERVED"
PASS_STATUS = "DEVELOPMENT_GATE_PASSED_FUTURE_CALIBRATION_REMAINS_SEALED"
REJECT_STATUS = "DEVELOPMENT_GATE_FAILED_FUTURE_CALIBRATION_FORBIDDEN"
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
        raise ValueError("v17_development_state_sha256_invalid")
    result["state_sha256"] = expected
    return result


def _read_sealed(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("v17_development_state_unreadable") from exc
    if not isinstance(value, Mapping):
        raise ValueError("v17_development_state_root_not_object")
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
        raise ValueError("v17_development_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("v17_development_timestamp_invalid")
    return parsed.astimezone(timezone.utc).isoformat()


def result_path_for(reservation_path: Path) -> Path:
    suffix = reservation_path.suffix or ".json"
    return reservation_path.with_name(f"{reservation_path.stem}.result{suffix}")


def _row_ids_sha256(rows: Sequence[Mapping[str, Any]]) -> str:
    return _canonical_sha256(tuple(sorted(int(row["id"]) for row in rows)))


def _close_times_sha256(rows: Sequence[Mapping[str, Any]]) -> str:
    return _canonical_sha256(tuple(sorted({float(row["close_time"]) for row in rows})))


def _expected_binding(
    *,
    seal: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    label_evidence_required: bool,
) -> dict[str, Any]:
    return {
        "runner_version": identity.RUNNER_VERSION,
        "evaluator_version": identity.EVALUATOR_VERSION,
        "evaluator_contract_id": identity.EVALUATOR_CONTRACT_ID,
        "evaluator_contract_sha256": identity.EVALUATOR_CONTRACT_SHA256,
        "protocol_id": v17_identity.PROTOCOL_ID,
        "protocol_sha256": v17_identity.PROTOCOL_SHA256,
        "development_seal_sha256": str(seal["seal_sha256"]),
        "selected_feature_evidence_sha256": str(
            seal["selected_feature_evidence_sha256"]
        ),
        "cohort": COHORT,
        "development_row_ids_sha256": _row_ids_sha256(rows),
        "development_close_times_sha256": _close_times_sha256(rows),
        "development_rows": len(rows),
        "development_close_windows": len({float(row["close_time"]) for row in rows}),
        "label_evidence_required": bool(label_evidence_required),
    }


def _validate_reservation(
    reservation: Mapping[str, Any], expected: Mapping[str, Any],
) -> None:
    if (
        reservation.get("state_version") != identity.STATE_VERSION
        or reservation.get("status") != RESERVED_STATUS
    ):
        raise ValueError("v17_development_reservation_status_invalid")
    for key, value in expected.items():
        if reservation.get(key) != value:
            raise ValueError(f"v17_development_reservation_binding_mismatch:{key}")
    if any(reservation.get(key) is not False for key in (
        "development_labels_read", "model_fit_performed",
        "probability_scoring_performed", "future_calibration_labels_read",
        "future_test_labels_read", "paper_artifact_created",
        "notification_eligible", "automatic_promotion", "real_trading_allowed",
    )):
        raise ValueError("v17_development_reservation_safety_invalid")


def _validate_result(
    result: Mapping[str, Any], reservation: Mapping[str, Any],
) -> None:
    report = result.get("development_report")
    label_rows = result.get("development_label_rows")
    if not isinstance(report, Mapping) or not isinstance(label_rows, list):
        raise ValueError("v17_development_result_invalid")
    try:
        pairs = sorted(
            [int(item["id"]), int(item["label_yes"])] for item in label_rows
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("v17_development_result_invalid") from exc
    ids = [row_id for row_id, _label in pairs]
    expected_status = PASS_STATUS if report.get("gate_met") is True else REJECT_STATUS
    evidence = result.get("label_read_evidence")
    if reservation.get("label_evidence_required") is True:
        if not isinstance(evidence, Mapping):
            raise ValueError("v17_development_result_invalid")
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
                stage="v17_development",
            )
        except ValueError as exc:
            raise ValueError("v17_development_result_invalid") from exc
        if (
            verified is None
            or result.get("label_read_evidence_sha256")
            != verified.get("evidence_sha256")
        ):
            raise ValueError("v17_development_result_invalid")
    elif evidence is not None or result.get("label_read_evidence_sha256") is not None:
        raise ValueError("v17_development_result_invalid")
    if (
        result.get("state_version") != identity.STATE_VERSION
        or result.get("runner_version") != identity.RUNNER_VERSION
        or result.get("status") != expected_status
        or result.get("reservation_state_sha256") != reservation.get("state_sha256")
        or result.get("development_seal_sha256")
        != reservation.get("development_seal_sha256")
        or len(ids) != 1440
        or len(set(ids)) != 1440
        or any(label not in {0, 1} for _row_id, label in pairs)
        or _canonical_sha256(tuple(sorted(ids)))
        != reservation.get("development_row_ids_sha256")
        or result.get("development_labels_sha256") != _canonical_sha256(pairs)
        or result.get("development_report_sha256")
        != _canonical_sha256(dict(report))
        or report.get("evaluator_version") != identity.EVALUATOR_VERSION
        or report.get("evaluator_contract_id") != identity.EVALUATOR_CONTRACT_ID
        or report.get("evaluator_contract_sha256")
        != identity.EVALUATOR_CONTRACT_SHA256
        or report.get("protocol_id") != v17_identity.PROTOCOL_ID
        or report.get("protocol_sha256") != v17_identity.PROTOCOL_SHA256
        or report.get("development_seal_sha256")
        != identity.DEVELOPMENT_SEAL_SHA256
        or report.get("cohort") != COHORT
        or int(report.get("input_rows", -1)) != 1440
        or int(report.get("input_close_windows", -1)) != 240
        or int(report.get("walk_forward_validation_rows", -1)) != 720
        or int(report.get("walk_forward_validation_close_windows", -1)) != 120
        or report.get("input_row_ids_sha256")
        != reservation.get("development_row_ids_sha256")
        or report.get("input_close_times_sha256")
        != reservation.get("development_close_times_sha256")
        or int(report.get("future_calibration_rows_used", -1)) != 0
        or int(report.get("future_test_rows_used", -1)) != 0
        or report.get("candidate_market_v16_v15_v14_identical_rows") is not True
        or report.get("same_close_assets_share_every_fold") is not True
        or report.get("accuracy_is_report_only") is not True
        or report.get("paper_artifact_created") is not False
        or report.get("notification_eligible") is not False
        or report.get("automatic_promotion") is not False
        or report.get("real_trading_allowed") is not False
        or result.get("development_labels_read_once") is not True
        or result.get("model_fit_performed") is not True
        or result.get("probability_scoring_performed") is not True
        or result.get("future_calibration_label_access_eligible")
        != (expected_status == PASS_STATUS)
        or any(result.get(key) is not False for key in (
            "future_calibration_labels_read", "future_test_labels_read",
            "paper_artifact_created", "notification_eligible",
            "automatic_promotion", "real_trading_allowed",
        ))
    ):
        raise ValueError("v17_development_result_invalid")


def run_development_once(
    *,
    seal: Mapping[str, Any],
    development_rows: Sequence[Mapping[str, Any]],
    reservation_path: Path,
    confirmation: str,
    read_development_labels: Callable[[Sequence[int]], Mapping[int, int]],
    contract: Mapping[str, Any] | None = None,
    protocol: Mapping[str, Any] | None = None,
    require_label_evidence: bool = False,
    timestamp: str | None = None,
) -> dict[str, Any]:
    contract = dict(contract or evaluator.load_contract())
    protocol = dict(protocol or evaluator.load_protocol())
    if contract != evaluator.load_contract() or protocol != evaluator.load_protocol():
        raise ValueError("v17_development_runner_contract_identity_invalid")
    if (
        str(seal.get("seal_sha256")) != identity.DEVELOPMENT_SEAL_SHA256
        or len(development_rows) != 1440
        or any(str(row.get("asset") or "").upper() == "BTC" for row in development_rows)
    ):
        raise ValueError("v17_development_runner_input_identity_invalid")
    expected = _expected_binding(
        seal=seal,
        rows=development_rows,
        label_evidence_required=require_label_evidence,
    )
    if (
        expected["development_row_ids_sha256"] != seal.get("selected_row_ids_sha256")
        or expected["development_close_times_sha256"]
        != seal.get("selected_close_times_sha256")
        or expected["development_close_windows"] != 240
        or expected["selected_feature_evidence_sha256"]
        != seal.get("selected_feature_evidence_sha256")
    ):
        raise ValueError("v17_development_runner_seal_binding_mismatch")
    result_path = result_path_for(reservation_path)
    if reservation_path.exists():
        reservation = _read_sealed(reservation_path)
        _validate_reservation(reservation, expected)
        if result_path.exists():
            result = _read_sealed(result_path)
            _validate_result(result, reservation)
            return {
                "status": "ALREADY_FINALIZED_NO_REREAD",
                "development_labels_read_this_call": False,
                "reservation": reservation,
                "result": result,
            }
        return {
            "status": "AMBIGUOUS_RESERVED_NO_REREAD",
            "development_labels_read_this_call": False,
            "reservation": reservation,
            "result": None,
        }
    if result_path.exists():
        raise ValueError("v17_development_result_exists_without_reservation")
    if confirmation != identity.CONFIRMATION_PHRASE:
        raise ValueError("v17_development_explicit_one_shot_confirmation_required")

    reservation = _write_exclusive(reservation_path, {
        **expected,
        "state_version": identity.STATE_VERSION,
        "status": RESERVED_STATUS,
        "reserved_at": _now_iso(timestamp),
        "development_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "future_calibration_labels_read": False,
        "future_test_labels_read": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    })

    ids = tuple(sorted(int(row["id"]) for row in development_rows))
    raw_labels = read_development_labels(ids)
    try:
        labels = {int(key): int(value) for key, value in raw_labels.items()}
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("v17_development_labels_invalid") from exc
    if set(labels) != set(ids) or any(value not in {0, 1} for value in labels.values()):
        raise ValueError("v17_development_labels_invalid")
    evidence = validate_label_evidence(
        raw_labels,
        labels,
        ids,
        required=require_label_evidence,
        stage="v17_development",
    )
    labeled = [
        {**dict(row), "label_yes": int(labels[int(row["id"])])}
        for row in development_rows
    ]
    report = evaluator.evaluate_development(
        labeled, contract=contract, protocol=protocol,
    )
    status = PASS_STATUS if report.get("gate_met") is True else REJECT_STATUS
    pairs = sorted([int(row_id), int(label)] for row_id, label in labels.items())
    result = _write_exclusive(result_path, {
        "state_version": identity.STATE_VERSION,
        "runner_version": identity.RUNNER_VERSION,
        "status": status,
        "finalized_at": _now_iso(timestamp),
        "reservation_state_sha256": reservation["state_sha256"],
        "development_seal_sha256": reservation["development_seal_sha256"],
        "evaluator_contract_sha256": identity.EVALUATOR_CONTRACT_SHA256,
        "cohort": COHORT,
        "development_labels_sha256": _canonical_sha256(pairs),
        "development_label_rows": [
            {"id": row_id, "label_yes": label} for row_id, label in pairs
        ],
        "label_read_evidence": evidence,
        "label_read_evidence_sha256": (
            evidence["evidence_sha256"] if evidence is not None else None
        ),
        "development_report": report,
        "development_report_sha256": _canonical_sha256(report),
        "development_labels_read_once": True,
        "model_fit_performed": True,
        "probability_scoring_performed": True,
        "future_calibration_label_access_eligible": status == PASS_STATUS,
        "future_calibration_labels_read": False,
        "future_test_labels_read": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    })
    _validate_result(result, reservation)
    return {
        "status": status,
        "development_labels_read_this_call": True,
        "reservation": reservation,
        "result": result,
    }
