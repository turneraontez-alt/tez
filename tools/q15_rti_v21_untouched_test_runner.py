"""Crash-safe one-shot scoring of V21's sealed untouched test.

Only the validated pretest model bundle may score the exact sealed test rows.
This module cannot fit, recalibrate, tune a margin, notify, promote, or trade.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import joblib

from q15_upgrade.strategy_bots import rti_microstructure_v21_audit_identity as audit_identity
from q15_upgrade.strategy_bots import rti_microstructure_v21_identity as identity
from tools import q15_rti_v21_feature_seal as feature_seal
from tools import q15_rti_v21_modeling as modeling
from tools import q15_rti_v21_pretest_runner as pretest
from tools.q15_rti_v15_label_evidence import validate_label_evidence


RESERVED_STATUS = "V21_UNTOUCHED_TEST_LABEL_ACCESS_RESERVED"
PASS_STATUS = "V21_HISTORICAL_GATES_PASSED_MANUAL_PAPER_CONSIDERATION_ONLY"
REJECT_STATUS = "V21_UNTOUCHED_TEST_GATE_FAILED_NO_PAPER_CHALLENGER"


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _sealed(payload: Mapping[str, Any]) -> dict[str, Any]:
    output = dict(payload)
    output.pop("state_sha256", None)
    output["state_sha256"] = _canonical_sha256(output)
    return output


def _validate_sealed(payload: Mapping[str, Any]) -> dict[str, Any]:
    output = dict(payload)
    supplied = str(output.pop("state_sha256", ""))
    if supplied != _canonical_sha256(output):
        raise ValueError("v21_untouched_test_state_sha256_invalid")
    output["state_sha256"] = supplied
    return output


def _read_sealed(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("v21_untouched_test_state_unreadable") from exc
    if not isinstance(value, Mapping):
        raise ValueError("v21_untouched_test_state_root_not_object")
    return _validate_sealed(value)


def _write_exclusive(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    output = _sealed(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(output, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
    return output


def _now_iso(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("v21_untouched_test_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("v21_untouched_test_timestamp_invalid")
    return parsed.astimezone(timezone.utc).isoformat()


def result_path_for(reservation_path: Path) -> Path:
    suffix = reservation_path.suffix or ".json"
    return reservation_path.with_name(f"{reservation_path.stem}.result{suffix}")


def _test_rows(seal: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(row) for row in seal["rows"]
        if row["partition"] == modeling.TEST_PARTITION
    ]


def _row_ids(rows: Sequence[Mapping[str, Any]]) -> tuple[int, ...]:
    return tuple(sorted(int(row["parent_id"]) for row in rows))


def _feature_identity(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    return tuple(sorted(({
        "parent_id": int(row["parent_id"]),
        "intermediate_id": int(row["intermediate_id"]),
        "delayed_id": int(row["delayed_id"]),
        "execution_supported": row["execution_supported"] is True,
        "feature_evidence_sha256": str(row["feature_evidence_sha256"]),
        "source_feature_evidence_sha256": str(
            row["source_feature_evidence_sha256"]
        ),
        "matched_benchmark_evidence_sha256": str(
            row["matched_benchmark_evidence_sha256"]
        ),
    } for row in rows), key=lambda item: item["parent_id"]))


def _load_passing_pretest(
    seal: Mapping[str, Any], reservation_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], Path]:
    if not reservation_path.exists():
        raise ValueError("v21_untouched_test_pretest_reservation_missing")
    reservation = pretest._read_sealed(reservation_path)
    expected = pretest._expected_binding(
        seal,
        label_evidence_required=bool(reservation.get("label_evidence_required")),
    )
    pretest._validate_reservation(reservation, expected)
    result_path = pretest.result_path_for(reservation_path)
    artifact_path = pretest.artifact_path_for(reservation_path)
    if not result_path.exists():
        raise ValueError("v21_untouched_test_pretest_ambiguous")
    result = pretest._read_sealed(result_path)
    pretest._validate_result(result, reservation, artifact_path, seal)
    if (
        result.get("status") != pretest.PASS_STATUS
        or result.get("manual_untouched_test_eligible") is not True
        or result.get("audit_model_bundle_created") is not True
        or result.get("untouched_test_labels_read") is not False
    ):
        raise ValueError("v21_untouched_test_pretest_gate_not_passed")
    try:
        bundle = joblib.load(artifact_path)
    except Exception as exc:
        raise ValueError("v21_untouched_test_model_bundle_unreadable") from exc
    if not isinstance(bundle, Mapping):
        raise ValueError("v21_untouched_test_model_bundle_invalid")
    return reservation, result, dict(bundle), artifact_path


def _expected_binding(
    seal: Mapping[str, Any], pretest_result: Mapping[str, Any],
    *, label_evidence_required: bool,
) -> dict[str, Any]:
    rows = _test_rows(seal)
    return {
        "untouched_test_runner_version": audit_identity.UNTOUCHED_TEST_RUNNER_VERSION,
        "modeling_version": audit_identity.MODELING_VERSION,
        "evaluator_contract_id": audit_identity.EVALUATOR_CONTRACT_ID,
        "evaluator_contract_sha256": audit_identity.EVALUATOR_CONTRACT_SHA256,
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "feature_seal_sha256": str(seal["seal_sha256"]),
        "pretest_result_state_sha256": str(pretest_result["state_sha256"]),
        "pretest_report_sha256": str(pretest_result["pretest_report_sha256"]),
        "audit_model_bundle_sha256": str(
            pretest_result["audit_model_bundle_sha256"]
        ),
        "untouched_test_rows": len(rows),
        "untouched_test_row_ids_sha256": _canonical_sha256(_row_ids(rows)),
        "untouched_test_feature_identity_sha256": _canonical_sha256(
            _feature_identity(rows)
        ),
        "label_evidence_required": bool(label_evidence_required),
    }


def _validate_reservation(
    reservation: Mapping[str, Any], expected: Mapping[str, Any],
) -> None:
    if (
        reservation.get("state_version")
        != audit_identity.UNTOUCHED_TEST_STATE_VERSION
        or reservation.get("status") != RESERVED_STATUS
    ):
        raise ValueError("v21_untouched_test_reservation_status_invalid")
    for key, value in expected.items():
        if reservation.get(key) != value:
            raise ValueError(
                f"v21_untouched_test_reservation_binding_mismatch:{key}"
            )
    if (
        int(reservation.get("untouched_test_rows") or 0) != 175
        or any(reservation.get(key) is not False for key in (
            "untouched_test_labels_read", "untouched_test_scoring_performed",
            "model_refit_performed", "recalibration_performed",
            "margin_selection_performed", "paper_artifact_created",
            "notification_eligible", "telegram_allowed", "automatic_promotion",
            "real_trading_allowed",
        ))
    ):
        raise ValueError("v21_untouched_test_reservation_safety_invalid")


def _normalize_settlement_labels(
    raw_labels: Mapping[int, int], expected_ids: Sequence[int],
) -> dict[int, int]:
    if not isinstance(raw_labels, Mapping):
        raise ValueError("v21_untouched_test_settlement_labels_invalid")
    labels = {}
    for raw_id, value in raw_labels.items():
        try:
            row_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("v21_untouched_test_settlement_labels_invalid") from exc
        if isinstance(value, bool) or value not in (0, 1):
            raise ValueError("v21_untouched_test_settlement_labels_invalid")
        labels[row_id] = int(value)
    if tuple(sorted(labels)) != tuple(sorted(int(value) for value in expected_ids)):
        raise ValueError("v21_untouched_test_settlement_label_identity_invalid")
    return labels


def _survival_labels(
    rows: Sequence[Mapping[str, Any]], settlement_yes: Mapping[int, int],
) -> dict[int, int]:
    return pretest._survival_labels(rows, settlement_yes)


def _validate_result(
    result: Mapping[str, Any], reservation: Mapping[str, Any],
    seal: Mapping[str, Any],
) -> None:
    report = result.get("untouched_test_report")
    settlement_rows = result.get("settlement_label_rows")
    survival_rows = result.get("survival_label_rows")
    if not isinstance(report, Mapping) or not isinstance(settlement_rows, list) or not isinstance(survival_rows, list):
        raise ValueError("v21_untouched_test_result_invalid")
    try:
        settlement_pairs = sorted(
            [int(row["parent_id"]), int(row["result_yes"])]
            for row in settlement_rows
        )
        survival_pairs = sorted(
            [int(row["parent_id"]), int(row["label_survives"])]
            for row in survival_rows
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("v21_untouched_test_result_invalid") from exc
    ids = [row_id for row_id, _label in settlement_pairs]
    expected_survival = sorted(
        [int(row_id), int(label)]
        for row_id, label in _survival_labels(
            _test_rows(seal), dict(settlement_pairs)
        ).items()
    )
    evidence = result.get("label_read_evidence")
    if reservation.get("label_evidence_required") is True:
        if not isinstance(evidence, Mapping):
            raise ValueError("v21_untouched_test_result_invalid")
        stored = type("_StoredVerifiedLabels", (dict,), {
            "audit_evidence": dict(evidence),
        })(dict(settlement_pairs))
        verified = validate_label_evidence(
            stored, dict(settlement_pairs), ids, required=True,
            stage="v21_untouched_test",
        )
        pretest._validate_fee_evidence(verified)
        if result.get("label_read_evidence_sha256") != verified["evidence_sha256"]:
            raise ValueError("v21_untouched_test_result_invalid")
    elif evidence is not None or result.get("label_read_evidence_sha256") is not None:
        raise ValueError("v21_untouched_test_result_invalid")
    passed = report.get("historical_gate_met") is True
    if (
        result.get("state_version") != audit_identity.UNTOUCHED_TEST_STATE_VERSION
        or result.get("untouched_test_runner_version")
        != audit_identity.UNTOUCHED_TEST_RUNNER_VERSION
        or result.get("status") != (PASS_STATUS if passed else REJECT_STATUS)
        or result.get("reservation_state_sha256") != reservation.get("state_sha256")
        or result.get("feature_seal_sha256") != reservation.get("feature_seal_sha256")
        or len(settlement_pairs) != 175
        or len(set(ids)) != len(ids)
        or survival_pairs != expected_survival
        or _canonical_sha256(tuple(sorted(ids)))
        != reservation.get("untouched_test_row_ids_sha256")
        or result.get("settlement_labels_sha256") != _canonical_sha256(settlement_pairs)
        or result.get("survival_labels_sha256") != _canonical_sha256(survival_pairs)
        or result.get("untouched_test_report_sha256")
        != _canonical_sha256(dict(report))
        or report.get("evaluator_contract_sha256")
        != audit_identity.EVALUATOR_CONTRACT_SHA256
        or report.get("feature_seal_sha256")
        != reservation.get("feature_seal_sha256")
        or report.get("untouched_test_label_rows") != 175
        or report.get("untouched_test_labels_read") is not True
        or report.get("untouched_test_scoring_performed") is not True
        or result.get("untouched_test_labels_read_once") is not True
        or result.get("model_refit_performed") is not False
        or result.get("recalibration_performed") is not False
        or result.get("margin_selection_performed") is not False
        or result.get("manual_paper_challenger_eligible") != passed
        or any(result.get(key) is not False for key in (
            "paper_artifact_created", "notification_eligible", "telegram_allowed",
            "automatic_promotion", "real_trading_allowed",
        ))
    ):
        raise ValueError("v21_untouched_test_result_invalid")


def run_untouched_test_once(
    *, seal: Mapping[str, Any], pretest_reservation_path: Path,
    reservation_path: Path, confirmation: str,
    read_settlement_yes_labels: Callable[[Sequence[int]], Mapping[int, int]],
    require_label_evidence: bool = True, timestamp: str | None = None,
) -> dict[str, Any]:
    feature_seal.validate_seal(seal)
    modeling.load_contract()
    _pretest_reservation, pretest_result, bundle, _artifact = (
        _load_passing_pretest(seal, pretest_reservation_path)
    )
    expected = _expected_binding(
        seal, pretest_result,
        label_evidence_required=require_label_evidence,
    )
    result_path = result_path_for(reservation_path)
    if reservation_path.exists():
        reservation = _read_sealed(reservation_path)
        _validate_reservation(reservation, expected)
        if result_path.exists():
            result = _read_sealed(result_path)
            _validate_result(result, reservation, seal)
            return {
                "status": "ALREADY_FINALIZED_NO_REREAD",
                "untouched_test_labels_read_this_call": False,
                "reservation": reservation,
                "result": result,
            }
        return {
            "status": "AMBIGUOUS_RESERVED_NO_REREAD",
            "untouched_test_labels_read_this_call": False,
            "reservation": reservation,
            "result": None,
        }
    if result_path.exists():
        raise ValueError("v21_untouched_test_output_exists_without_reservation")
    if confirmation != audit_identity.UNTOUCHED_TEST_CONFIRMATION:
        raise ValueError(
            "v21_untouched_test_explicit_one_shot_confirmation_required"
        )
    reservation = _write_exclusive(reservation_path, {
        **expected,
        "state_version": audit_identity.UNTOUCHED_TEST_STATE_VERSION,
        "status": RESERVED_STATUS,
        "reserved_at": _now_iso(timestamp),
        "untouched_test_labels_read": False,
        "untouched_test_scoring_performed": False,
        "model_refit_performed": False,
        "recalibration_performed": False,
        "margin_selection_performed": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "telegram_allowed": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    })
    rows = _test_rows(seal)
    ids = _row_ids(rows)
    raw_labels = read_settlement_yes_labels(ids)
    settlement_yes = _normalize_settlement_labels(raw_labels, ids)
    evidence = validate_label_evidence(
        raw_labels, settlement_yes, ids, required=require_label_evidence,
        stage="v21_untouched_test",
    )
    if require_label_evidence:
        pretest._validate_fee_evidence(evidence)
    survival = _survival_labels(rows, settlement_yes)
    report = modeling.evaluate_untouched_test(seal, survival, bundle)
    passed = report.get("historical_gate_met") is True
    result = _write_exclusive(result_path, {
        "state_version": audit_identity.UNTOUCHED_TEST_STATE_VERSION,
        "untouched_test_runner_version": audit_identity.UNTOUCHED_TEST_RUNNER_VERSION,
        "status": PASS_STATUS if passed else REJECT_STATUS,
        "finalized_at": _now_iso(timestamp),
        "reservation_state_sha256": reservation["state_sha256"],
        "feature_seal_sha256": reservation["feature_seal_sha256"],
        "pretest_result_state_sha256": reservation[
            "pretest_result_state_sha256"
        ],
        "settlement_labels_sha256": _canonical_sha256(sorted(
            [int(row_id), int(label)] for row_id, label in settlement_yes.items()
        )),
        "survival_labels_sha256": _canonical_sha256(sorted(
            [int(row_id), int(label)] for row_id, label in survival.items()
        )),
        "settlement_label_rows": [
            {"parent_id": int(row_id), "result_yes": int(label)}
            for row_id, label in sorted(settlement_yes.items())
        ],
        "survival_label_rows": [
            {"parent_id": int(row_id), "label_survives": int(label)}
            for row_id, label in sorted(survival.items())
        ],
        "label_read_evidence": evidence,
        "label_read_evidence_sha256": (
            evidence["evidence_sha256"] if evidence is not None else None
        ),
        "untouched_test_report": report,
        "untouched_test_report_sha256": _canonical_sha256(report),
        "untouched_test_labels_read_once": True,
        "model_refit_performed": False,
        "recalibration_performed": False,
        "margin_selection_performed": False,
        "manual_paper_challenger_eligible": passed,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "telegram_allowed": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    })
    _validate_result(result, reservation, seal)
    return {
        "status": result["status"],
        "untouched_test_labels_read_this_call": True,
        "reservation": reservation,
        "result": result,
    }
