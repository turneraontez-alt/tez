"""Crash-safe one-shot V21 TRAIN/CALIBRATION/POLICY runner.

This module has no database, network, Telegram, paper-ledger, promotion, or
order capability.  It writes an exclusive reservation before invoking the
supplied authoritative-settlement callback.  A reservation without a final
result is permanently ambiguous and can never read labels again.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any, Callable, Mapping, Sequence

import joblib
import sklearn

from q15_upgrade.strategy_bots import rti_microstructure_v21_audit_identity as audit_identity
from q15_upgrade.strategy_bots import rti_microstructure_v21_identity as identity
from tools import q15_rti_v21_feature_seal as feature_seal
from tools import q15_rti_v21_modeling as modeling
from tools.q15_rti_v15_label_evidence import validate_label_evidence


RESERVED_STATUS = "V21_TRAIN_CAL_POLICY_LABEL_ACCESS_RESERVED"
PASS_STATUS = "V21_PRETEST_GATES_PASSED_UNTOUCHED_TEST_REMAINS_SEALED"
REJECT_STATUS = "V21_PRETEST_GATE_FAILED_UNTOUCHED_TEST_REMAINS_SEALED"


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sealed(payload: Mapping[str, Any]) -> dict[str, Any]:
    output = dict(payload)
    output.pop("state_sha256", None)
    output["state_sha256"] = _canonical_sha256(output)
    return output


def _validate_sealed(payload: Mapping[str, Any]) -> dict[str, Any]:
    output = dict(payload)
    supplied = str(output.pop("state_sha256", ""))
    if supplied != _canonical_sha256(output):
        raise ValueError("v21_pretest_state_sha256_invalid")
    output["state_sha256"] = supplied
    return output


def _read_sealed(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("v21_pretest_state_unreadable") from exc
    if not isinstance(value, Mapping):
        raise ValueError("v21_pretest_state_root_not_object")
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
        raise ValueError("v21_pretest_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("v21_pretest_timestamp_invalid")
    return parsed.astimezone(timezone.utc).isoformat()


def result_path_for(reservation_path: Path) -> Path:
    suffix = reservation_path.suffix or ".json"
    return reservation_path.with_name(f"{reservation_path.stem}.result{suffix}")


def artifact_path_for(reservation_path: Path) -> Path:
    return reservation_path.with_name(f"{reservation_path.stem}.model.joblib")


def _row_ids(rows: Sequence[Mapping[str, Any]]) -> tuple[int, ...]:
    return tuple(sorted(int(row["parent_id"]) for row in rows))


def _required_pretest_rows(seal: Mapping[str, Any]) -> list[dict[str, Any]]:
    ids = modeling.required_pretest_label_ids(seal)
    return [
        dict(row) for row in seal["rows"] if int(row["parent_id"]) in ids
    ]


def _test_rows(seal: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(row) for row in seal["rows"]
        if row["partition"] == modeling.TEST_PARTITION
    ]


def _feature_identity(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    return tuple(sorted(({
        "parent_id": int(row["parent_id"]),
        "intermediate_id": int(row["intermediate_id"]),
        "delayed_id": int(row["delayed_id"]),
        "partition": str(row["partition"]),
        "execution_supported": row["execution_supported"] is True,
        "feature_evidence_sha256": str(row["feature_evidence_sha256"]),
        "source_feature_evidence_sha256": str(
            row["source_feature_evidence_sha256"]
        ),
        "matched_benchmark_evidence_sha256": str(
            row["matched_benchmark_evidence_sha256"]
        ),
    } for row in rows), key=lambda item: item["parent_id"]))


def _expected_binding(
    seal: Mapping[str, Any], *, label_evidence_required: bool,
) -> dict[str, Any]:
    feature_seal.validate_seal(seal)
    pretest_rows = _required_pretest_rows(seal)
    test_rows = _test_rows(seal)
    return {
        "pretest_runner_version": audit_identity.PRETEST_RUNNER_VERSION,
        "modeling_version": audit_identity.MODELING_VERSION,
        "evaluator_contract_id": audit_identity.EVALUATOR_CONTRACT_ID,
        "evaluator_contract_sha256": audit_identity.EVALUATOR_CONTRACT_SHA256,
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "feature_seal_sha256": str(seal["seal_sha256"]),
        "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
        "feature_names_sha256": identity.FEATURE_NAMES_SHA256,
        "pretest_label_rows": len(pretest_rows),
        "untouched_test_rows": len(test_rows),
        "pretest_label_row_ids_sha256": _canonical_sha256(_row_ids(pretest_rows)),
        "untouched_test_row_ids_sha256": _canonical_sha256(_row_ids(test_rows)),
        "pretest_feature_identity_sha256": _canonical_sha256(
            _feature_identity(pretest_rows)
        ),
        "untouched_test_feature_identity_sha256": _canonical_sha256(
            _feature_identity(test_rows)
        ),
        "label_evidence_required": bool(label_evidence_required),
    }


def _validate_reservation(
    reservation: Mapping[str, Any], expected: Mapping[str, Any],
) -> None:
    if (
        reservation.get("state_version") != audit_identity.PRETEST_STATE_VERSION
        or reservation.get("status") != RESERVED_STATUS
    ):
        raise ValueError("v21_pretest_reservation_status_invalid")
    for key, value in expected.items():
        if reservation.get(key) != value:
            raise ValueError(f"v21_pretest_reservation_binding_mismatch:{key}")
    rows = int(reservation.get("pretest_label_rows") or 0)
    if (
        not 910 <= rows <= 1085
        or int(reservation.get("untouched_test_rows") or 0) != 175
        or any(reservation.get(key) is not False for key in (
            "outcome_labels_read", "model_fit_performed",
            "probability_scoring_performed", "untouched_test_labels_read",
            "untouched_test_scoring_performed", "audit_model_bundle_created",
            "paper_artifact_created", "notification_eligible",
            "telegram_allowed", "automatic_promotion", "real_trading_allowed",
        ))
    ):
        raise ValueError("v21_pretest_reservation_safety_invalid")


def _normalize_settlement_labels(
    raw_labels: Mapping[int, int], expected_ids: Sequence[int],
) -> dict[int, int]:
    if not isinstance(raw_labels, Mapping):
        raise ValueError("v21_pretest_settlement_labels_invalid")
    labels = {}
    for raw_id, value in raw_labels.items():
        try:
            row_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("v21_pretest_settlement_labels_invalid") from exc
        if isinstance(value, bool) or value not in (0, 1):
            raise ValueError("v21_pretest_settlement_labels_invalid")
        labels[row_id] = int(value)
    if tuple(sorted(labels)) != tuple(sorted(int(value) for value in expected_ids)):
        raise ValueError("v21_pretest_settlement_label_identity_invalid")
    return labels


def _survival_labels(
    rows: Sequence[Mapping[str, Any]], settlement_yes: Mapping[int, int],
) -> dict[int, int]:
    output = {}
    for row in rows:
        row_id = int(row["parent_id"])
        side = str(row["side"])
        if side not in {"YES", "NO"}:
            raise ValueError("v21_pretest_side_invalid")
        result_yes = int(settlement_yes[row_id])
        output[row_id] = result_yes if side == "YES" else 1 - result_yes
    return output


def _validate_fee_evidence(evidence: Mapping[str, Any] | None) -> None:
    if not isinstance(evidence, Mapping):
        raise ValueError("v21_pretest_fee_evidence_required")
    config = modeling.load_contract()["fee_schedule_verification"]
    fee = evidence.get("fee_schedule_verification")
    if not isinstance(fee, Mapping):
        raise ValueError("v21_pretest_fee_evidence_invalid")
    series = fee.get("series")
    if not isinstance(series, list):
        raise ValueError("v21_pretest_fee_evidence_invalid")
    expected = tuple(sorted(str(value) for value in config["series_tickers"]))
    observed = []
    for row in series:
        if not isinstance(row, Mapping):
            raise ValueError("v21_pretest_fee_evidence_invalid")
        ticker = str(row.get("series_ticker") or "")
        observed.append(ticker)
        if (
            row.get("fee_type") != config["required_fee_type"]
            or float(row.get("fee_multiplier") or 0.0)
            != float(config["required_fee_multiplier"])
            or not str(row.get("fetched_at") or "")
            or not str(row.get("source_url") or "").endswith(f"/series/{ticker}")
        ):
            raise ValueError("v21_pretest_fee_evidence_invalid")
    if (
        fee.get("verification_status")
        != "OFFICIAL_KALSHI_SERIES_FEE_METADATA_VERIFIED"
        or tuple(sorted(observed)) != expected
        or len(set(observed)) != len(observed)
        or fee.get("fee_schedule_version") != config["fee_schedule_version"]
        or fee.get("execution_cost_model_version")
        != config["execution_cost_model_version"]
        or float(fee.get("general_taker_fee_rate") or 0.0)
        != float(config["general_taker_fee_rate"])
    ):
        raise ValueError("v21_pretest_fee_evidence_invalid")


def _write_artifact_exclusive(
    path: Path, *, seal: Mapping[str, Any], report: Mapping[str, Any],
    artifacts: Mapping[str, Any],
) -> dict[str, Any]:
    payload = {
        "artifact_version": "q15-rti-v21-historical-audit-model-bundle-v1",
        "modeling_version": audit_identity.MODELING_VERSION,
        "evaluator_contract_id": audit_identity.EVALUATOR_CONTRACT_ID,
        "evaluator_contract_sha256": audit_identity.EVALUATOR_CONTRACT_SHA256,
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "feature_seal_sha256": str(seal["seal_sha256"]),
        "pretest_report_sha256": _canonical_sha256(dict(report)),
        "scikit_learn_version": sklearn.__version__,
        "cohorts": dict(artifacts),
        "paper_only": True,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ValueError("v21_pretest_artifact_already_exists")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", suffix=".tmp",
            dir=path.parent, delete=False,
        ) as handle:
            temporary = Path(handle.name)
        joblib.dump(payload, temporary, compress=0, protocol=5)
        with path.open("xb") as destination, temporary.open("rb") as source:
            shutil.copyfileobj(source, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {
        "sha256": _file_sha256(path),
        "bytes": path.stat().st_size,
        "artifact_version": payload["artifact_version"],
    }


def _validate_artifact(path: Path, result: Mapping[str, Any]) -> None:
    if (
        not path.exists()
        or _file_sha256(path) != result.get("audit_model_bundle_sha256")
        or path.stat().st_size != int(result.get("audit_model_bundle_bytes") or -1)
    ):
        raise ValueError("v21_pretest_artifact_file_invalid")
    try:
        payload = joblib.load(path)
    except Exception as exc:
        raise ValueError("v21_pretest_artifact_unreadable") from exc
    if (
        not isinstance(payload, Mapping)
        or payload.get("artifact_version")
        != result.get("audit_model_bundle_version")
        or payload.get("modeling_version") != audit_identity.MODELING_VERSION
        or payload.get("evaluator_contract_sha256")
        != audit_identity.EVALUATOR_CONTRACT_SHA256
        or payload.get("protocol_sha256") != identity.PROTOCOL_SHA256
        or payload.get("feature_seal_sha256")
        != result.get("feature_seal_sha256")
        or payload.get("pretest_report_sha256")
        != result.get("pretest_report_sha256")
        or payload.get("scikit_learn_version") != sklearn.__version__
        or set(payload.get("cohorts") or {}) != set(modeling.COHORTS)
        or any((payload.get("cohorts") or {}).get(cohort) is None
               for cohort in modeling.COHORTS)
        or payload.get("notification_eligible") is not False
        or payload.get("automatic_promotion") is not False
        or payload.get("real_trading_allowed") is not False
    ):
        raise ValueError("v21_pretest_artifact_identity_or_safety_invalid")


def _validate_result(
    result: Mapping[str, Any], reservation: Mapping[str, Any],
    artifact_path: Path, seal: Mapping[str, Any],
) -> None:
    report = result.get("pretest_report")
    settlement_rows = result.get("settlement_label_rows")
    survival_rows = result.get("survival_label_rows")
    if not isinstance(report, Mapping) or not isinstance(settlement_rows, list) or not isinstance(survival_rows, list):
        raise ValueError("v21_pretest_result_invalid")
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
        raise ValueError("v21_pretest_result_invalid") from exc
    ids = [row_id for row_id, _label in settlement_pairs]
    expected_survival = sorted(
        [int(row_id), int(label)]
        for row_id, label in _survival_labels(
            _required_pretest_rows(seal), dict(settlement_pairs)
        ).items()
    )
    evidence = result.get("label_read_evidence")
    if reservation.get("label_evidence_required") is True:
        if not isinstance(evidence, Mapping):
            raise ValueError("v21_pretest_result_invalid")
        stored = type("_StoredVerifiedLabels", (dict,), {
            "audit_evidence": dict(evidence),
        })(dict(settlement_pairs))
        verified = validate_label_evidence(
            stored, dict(settlement_pairs), ids, required=True,
            stage="v21_pretest",
        )
        _validate_fee_evidence(verified)
        if result.get("label_read_evidence_sha256") != verified["evidence_sha256"]:
            raise ValueError("v21_pretest_result_invalid")
    elif evidence is not None or result.get("label_read_evidence_sha256") is not None:
        raise ValueError("v21_pretest_result_invalid")
    passed = report.get("pretest_gate_met") is True
    if (
        result.get("state_version") != audit_identity.PRETEST_STATE_VERSION
        or result.get("pretest_runner_version") != audit_identity.PRETEST_RUNNER_VERSION
        or result.get("status") != (PASS_STATUS if passed else REJECT_STATUS)
        or result.get("reservation_state_sha256") != reservation.get("state_sha256")
        or result.get("feature_seal_sha256") != reservation.get("feature_seal_sha256")
        or len(settlement_pairs) != int(reservation["pretest_label_rows"])
        or len(set(ids)) != len(ids)
        or survival_pairs != expected_survival
        or _canonical_sha256(tuple(sorted(ids)))
        != reservation.get("pretest_label_row_ids_sha256")
        or result.get("settlement_labels_sha256") != _canonical_sha256(settlement_pairs)
        or result.get("survival_labels_sha256") != _canonical_sha256(survival_pairs)
        or result.get("pretest_report_sha256") != _canonical_sha256(dict(report))
        or report.get("modeling_version") != audit_identity.MODELING_VERSION
        or report.get("evaluator_contract_sha256")
        != audit_identity.EVALUATOR_CONTRACT_SHA256
        or report.get("feature_seal_sha256") != reservation.get("feature_seal_sha256")
        or report.get("train_calibration_policy_label_rows")
        != int(reservation["pretest_label_rows"])
        or report.get("train_calibration_policy_label_ids_sha256")
        != reservation.get("pretest_label_row_ids_sha256")
        or report.get("outcome_labels_read") is not True
        or report.get("untouched_test_labels_read") is not False
        or result.get("train_calibration_policy_labels_read_once") is not True
        or result.get("untouched_test_labels_read") is not False
        or result.get("untouched_test_scoring_performed") is not False
        or result.get("manual_untouched_test_eligible") != passed
        or result.get("audit_model_bundle_created") != passed
        or any(result.get(key) is not False for key in (
            "paper_artifact_created", "notification_eligible", "telegram_allowed",
            "automatic_promotion", "real_trading_allowed",
        ))
    ):
        raise ValueError("v21_pretest_result_invalid")
    if passed:
        _validate_artifact(artifact_path, result)
    elif (
        any(result.get(key) is not None for key in (
            "audit_model_bundle_sha256", "audit_model_bundle_bytes",
            "audit_model_bundle_version",
        ))
        or artifact_path.exists()
    ):
        raise ValueError("v21_pretest_rejected_artifact_invalid")


def run_pretest_once(
    *, seal: Mapping[str, Any], reservation_path: Path, confirmation: str,
    read_settlement_yes_labels: Callable[[Sequence[int]], Mapping[int, int]],
    require_label_evidence: bool = True, timestamp: str | None = None,
) -> dict[str, Any]:
    """Reserve and evaluate the exact frozen V21 non-test label population."""
    feature_seal.validate_seal(seal)
    modeling.load_contract()
    expected = _expected_binding(
        seal, label_evidence_required=require_label_evidence,
    )
    result_path = result_path_for(reservation_path)
    artifact_path = artifact_path_for(reservation_path)
    if reservation_path.exists():
        reservation = _read_sealed(reservation_path)
        _validate_reservation(reservation, expected)
        if result_path.exists():
            result = _read_sealed(result_path)
            _validate_result(result, reservation, artifact_path, seal)
            return {
                "status": "ALREADY_FINALIZED_NO_REREAD",
                "train_calibration_policy_labels_read_this_call": False,
                "reservation": reservation,
                "result": result,
            }
        return {
            "status": "AMBIGUOUS_RESERVED_NO_REREAD",
            "train_calibration_policy_labels_read_this_call": False,
            "reservation": reservation,
            "result": None,
        }
    if result_path.exists() or artifact_path.exists():
        raise ValueError("v21_pretest_output_exists_without_reservation")
    if confirmation != audit_identity.PRETEST_CONFIRMATION:
        raise ValueError("v21_pretest_explicit_one_shot_confirmation_required")
    reservation = _write_exclusive(reservation_path, {
        **expected,
        "state_version": audit_identity.PRETEST_STATE_VERSION,
        "status": RESERVED_STATUS,
        "reserved_at": _now_iso(timestamp),
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "untouched_test_labels_read": False,
        "untouched_test_scoring_performed": False,
        "audit_model_bundle_created": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "telegram_allowed": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    })
    rows = _required_pretest_rows(seal)
    ids = _row_ids(rows)
    raw_labels = read_settlement_yes_labels(ids)
    settlement_yes = _normalize_settlement_labels(raw_labels, ids)
    evidence = validate_label_evidence(
        raw_labels, settlement_yes, ids, required=require_label_evidence,
        stage="v21_pretest",
    )
    if require_label_evidence:
        _validate_fee_evidence(evidence)
    survival = _survival_labels(rows, settlement_yes)
    modeled = modeling.evaluate_pretest(seal, survival)
    report = dict(modeled["report"])
    passed = report.get("pretest_gate_met") is True
    artifact = None
    if passed:
        artifact = _write_artifact_exclusive(
            artifact_path, seal=seal, report=report,
            artifacts=modeled["artifacts"],
        )
    result = _write_exclusive(result_path, {
        "state_version": audit_identity.PRETEST_STATE_VERSION,
        "pretest_runner_version": audit_identity.PRETEST_RUNNER_VERSION,
        "status": PASS_STATUS if passed else REJECT_STATUS,
        "finalized_at": _now_iso(timestamp),
        "reservation_state_sha256": reservation["state_sha256"],
        "feature_seal_sha256": reservation["feature_seal_sha256"],
        "pretest_label_row_ids_sha256": reservation["pretest_label_row_ids_sha256"],
        "untouched_test_row_ids_sha256": reservation["untouched_test_row_ids_sha256"],
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
        "pretest_report": report,
        "pretest_report_sha256": _canonical_sha256(report),
        "audit_model_bundle_created": passed,
        "audit_model_bundle_version": (
            artifact["artifact_version"] if artifact is not None else None
        ),
        "audit_model_bundle_sha256": (
            artifact["sha256"] if artifact is not None else None
        ),
        "audit_model_bundle_bytes": (
            artifact["bytes"] if artifact is not None else None
        ),
        "train_calibration_policy_labels_read_once": True,
        "untouched_test_labels_read": False,
        "untouched_test_scoring_performed": False,
        "manual_untouched_test_eligible": passed,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "telegram_allowed": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    })
    _validate_result(result, reservation, artifact_path, seal)
    return {
        "status": result["status"],
        "train_calibration_policy_labels_read_this_call": True,
        "reservation": reservation,
        "result": result,
    }
