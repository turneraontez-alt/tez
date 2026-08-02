"""Create V21 cohort PAPER artifacts only after both historical gates pass.

This manual, crash-safe bridge copies the already-frozen pretest models.  It
cannot fit, recalibrate, change a margin, read a label, notify, promote, or
trade.  Creation alone does not connect either artifact to the live runtime.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import math
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any, Mapping

import joblib


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots import rti_microstructure_v21_identity as identity
from q15_upgrade.strategy_bots import rti_microstructure_v21_paper_identity as paper_identity
from tools import q15_rti_v21_feature_seal as feature_seal
from tools import q15_rti_v21_paper_preregister as preregister
from tools import q15_rti_v21_pretest_command as pretest_command
from tools import q15_rti_v21_pretest_runner as pretest
from tools import q15_rti_v21_untouched_test_runner as untouched


CONFIRMATION = "CREATE_V21_PAPER_CHALLENGER_FROM_PASSING_AUDIT"
STATE_VERSION = "q15-rti-v21-paper-artifact-creation-state-v1"
RESERVED_STATUS = "V21_PAPER_ARTIFACT_CREATION_RESERVED"
FINAL_STATUS = "V21_PAPER_ARTIFACTS_CREATED_NOT_RUNTIME_CONNECTED"
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "q15_rti_v21_paper_artifacts"

COHORT_ASSETS = {
    "NON_BTC_TRANSFER": ["BNB", "DOGE", "ETH", "HYPE", "SOL", "XRP"],
    "BTC": ["BTC"],
}
MODEL_KEYS = (
    "selected_spec",
    "base_model",
    "platt_calibrator",
    "selected_calibrator_method",
    "selected_margin",
    "v20_feature_map_ablation_base_model",
    "v20_feature_map_ablation_selected_spec",
    "v20_feature_map_ablation_selected_model_id",
    "v20_feature_map_ablation_platt_calibrator",
    "v20_feature_map_ablation_selected_calibrator_method",
)


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


def _read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("v21_paper_artifact_state_unreadable") from exc
    if not isinstance(value, Mapping):
        raise ValueError("v21_paper_artifact_state_invalid")
    output = dict(value)
    supplied = str(output.pop("state_sha256", ""))
    if supplied != _canonical_sha256(output):
        raise ValueError("v21_paper_artifact_state_sha256_invalid")
    output["state_sha256"] = supplied
    return output


def _write_state_exclusive(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    output = _sealed(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(output, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
    return output


def _timestamp(value: str | None) -> tuple[str, float]:
    if value is None:
        parsed = datetime.now(timezone.utc)
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("v21_paper_artifact_timestamp_invalid") from exc
        if parsed.tzinfo is None:
            raise ValueError("v21_paper_artifact_timestamp_invalid")
        parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat(), float(parsed.timestamp())


def _first_complete_close_after_load(created_at_unix: float) -> float:
    # A 12M decision occurs 720 seconds before a 15-minute close.  The first
    # credited decision must be strictly later than artifact creation/load.
    return float((math.floor((created_at_unix + 720.0) / 900.0) + 1) * 900)


def _model_payload_sha256(cohort_payload: Mapping[str, Any]) -> str:
    identity_fields = {
        "cohort": str(cohort_payload["cohort"]),
        "selected_model_id": str(cohort_payload.get("selected_model_id") or ""),
        "v20_feature_map_ablation_selected_model_id": str(
            cohort_payload.get("v20_feature_map_ablation_selected_model_id") or ""
        ),
    }
    object_hashes = {}
    for key in MODEL_KEYS:
        if key == "selected_margin":
            continue
        stream = BytesIO()
        # Serialize each object independently so Python pickle memo/reference
        # sharing cannot change the digest for an otherwise identical model.
        joblib.dump(cohort_payload[key], stream, compress=0, protocol=5)
        object_hashes[key] = hashlib.sha256(stream.getvalue()).hexdigest()
    return _canonical_sha256({
        "identity": identity_fields,
        "object_sha256": object_hashes,
    })


def _load_seal(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("v21_paper_artifact_feature_seal_unreadable") from exc
    if not isinstance(value, Mapping):
        raise ValueError("v21_paper_artifact_feature_seal_invalid")
    payload = dict(value)
    feature_seal.validate_seal(payload)
    return payload


def _load_passing_chain(
    *, seal: Mapping[str, Any], pretest_reservation_path: Path,
    untouched_reservation_path: Path,
) -> dict[str, Any]:
    _, pretest_result, bundle, bundle_path = untouched._load_passing_pretest(
        seal, pretest_reservation_path,
    )
    if not untouched_reservation_path.exists():
        raise ValueError("v21_paper_artifact_untouched_reservation_missing")
    test_reservation = untouched._read_sealed(untouched_reservation_path)
    expected = untouched._expected_binding(
        seal, pretest_result,
        label_evidence_required=bool(test_reservation.get("label_evidence_required")),
    )
    untouched._validate_reservation(test_reservation, expected)
    test_result_path = untouched.result_path_for(untouched_reservation_path)
    if not test_result_path.exists():
        raise ValueError("v21_paper_artifact_untouched_result_ambiguous")
    test_result = untouched._read_sealed(test_result_path)
    untouched._validate_result(test_result, test_reservation, seal)
    if (
        test_result.get("status") != untouched.PASS_STATUS
        or test_result.get("manual_paper_challenger_eligible") is not True
        or test_result.get("untouched_test_report", {}).get("historical_gate_met")
        is not True
    ):
        raise ValueError("v21_paper_artifact_historical_gate_not_passed")
    cohorts = bundle.get("cohorts")
    if not isinstance(cohorts, Mapping) or set(cohorts) != set(COHORT_ASSETS):
        raise ValueError("v21_paper_artifact_cohort_bundle_invalid")
    for cohort, payload in cohorts.items():
        if (
            not isinstance(payload, Mapping)
            or payload.get("cohort") != cohort
            or any(key not in payload for key in MODEL_KEYS)
        ):
            raise ValueError("v21_paper_artifact_cohort_bundle_invalid")
    return {
        "pretest_result": pretest_result,
        "audit_bundle": bundle,
        "audit_bundle_path": bundle_path,
        "test_reservation": test_reservation,
        "test_result": test_result,
        "test_result_path": test_result_path,
    }


def _chain_bindings(
    seal: Mapping[str, Any], chain: Mapping[str, Any],
    pretest_reservation_path: Path, untouched_reservation_path: Path,
) -> dict[str, str]:
    pretest_reservation = pretest._read_sealed(pretest_reservation_path)
    pretest_result = chain["pretest_result"]
    test_reservation = chain["test_reservation"]
    test_result = chain["test_result"]
    return {
        "source_protocol_sha256": identity.PROTOCOL_SHA256,
        "evaluator_contract_sha256": identity.EVALUATOR_CONTRACT_SHA256,
        "paper_deployment_protocol_sha256": paper_identity.PROTOCOL_SHA256,
        "feature_seal_sha256": str(seal["seal_sha256"]),
        "pretest_reservation_state_sha256": str(pretest_reservation["state_sha256"]),
        "pretest_result_state_sha256": str(pretest_result["state_sha256"]),
        "pretest_report_sha256": str(pretest_result["pretest_report_sha256"]),
        "audit_model_bundle_sha256": _file_sha256(chain["audit_bundle_path"]),
        "untouched_test_reservation_state_sha256": str(test_reservation["state_sha256"]),
        "untouched_test_result_state_sha256": str(test_result["state_sha256"]),
        "untouched_test_report_sha256": str(test_result["untouched_test_report_sha256"]),
        "feature_names_sha256": identity.FEATURE_NAMES_SHA256,
    }


def _expected_reservation(
    *, bindings: Mapping[str, str], created_at: str, created_at_unix: float,
    prospective_after_close_time: float,
) -> dict[str, Any]:
    return {
        "state_version": STATE_VERSION,
        "status": RESERVED_STATUS,
        "protocol_id": paper_identity.PROTOCOL_ID,
        "protocol_sha256": paper_identity.PROTOCOL_SHA256,
        "artifact_version": paper_identity.ARTIFACT_VERSION,
        "created_at": created_at,
        "created_at_unix": created_at_unix,
        "prospective_after_close_time": prospective_after_close_time,
        "bindings": dict(bindings),
        "cohorts": list(COHORT_ASSETS),
        "outcome_labels_read_by_artifact_command": False,
        "model_fit_performed": False,
        "recalibration_performed": False,
        "margin_selection_performed": False,
        "runtime_scoring_connected": False,
        "notifications_enabled": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }


def _validate_reservation(
    reservation: Mapping[str, Any], expected_bindings: Mapping[str, str],
) -> None:
    bindings = reservation.get("bindings")
    invariant_bindings = {
        "source_protocol_sha256": identity.PROTOCOL_SHA256,
        "evaluator_contract_sha256": identity.EVALUATOR_CONTRACT_SHA256,
        "paper_deployment_protocol_sha256": paper_identity.PROTOCOL_SHA256,
        "feature_names_sha256": identity.FEATURE_NAMES_SHA256,
    }
    try:
        parsed_created_at_unix = _timestamp(str(reservation.get("created_at")))[1]
    except ValueError:
        parsed_created_at_unix = float("nan")
    if (
        reservation.get("state_version") != STATE_VERSION
        or reservation.get("status") != RESERVED_STATUS
        or reservation.get("protocol_id") != paper_identity.PROTOCOL_ID
        or reservation.get("protocol_sha256") != paper_identity.PROTOCOL_SHA256
        or reservation.get("artifact_version") != paper_identity.ARTIFACT_VERSION
        or not math.isfinite(parsed_created_at_unix)
        or abs(
            parsed_created_at_unix - float(reservation.get("created_at_unix") or 0.0)
        ) > 1e-6
        or not isinstance(bindings, Mapping)
        or bindings != dict(expected_bindings)
        or any(bindings.get(key) != value for key, value in invariant_bindings.items())
        or any(
            len(str(value)) != 64
            for value in bindings.values()
        )
        or reservation.get("cohorts") != list(COHORT_ASSETS)
        or float(reservation.get("prospective_after_close_time") or 0.0)
        != _first_complete_close_after_load(float(reservation["created_at_unix"]))
        or any(reservation.get(key) is not False for key in (
            "outcome_labels_read_by_artifact_command", "model_fit_performed",
            "recalibration_performed", "margin_selection_performed",
            "runtime_scoring_connected", "notifications_enabled",
            "automatic_promotion", "real_trading_allowed",
        ))
    ):
        raise ValueError("v21_paper_artifact_reservation_invalid")


def _artifact_payload(
    *, cohort: str, cohort_payload: Mapping[str, Any],
    reservation: Mapping[str, Any],
) -> dict[str, Any]:
    model_hash = _model_payload_sha256(cohort_payload)
    margin_hash = _canonical_sha256({
        "cohort": cohort,
        "selected_margin": float(cohort_payload["selected_margin"]),
    })
    bindings = {
        **dict(reservation["bindings"]),
        "model_payload_sha256": model_hash,
        "selected_margin_sha256": margin_hash,
    }
    return {
        "artifact_version": paper_identity.ARTIFACT_VERSION,
        "cohort": cohort,
        "assets": list(COHORT_ASSETS[cohort]),
        "created_at": reservation["created_at"],
        "created_at_unix": reservation["created_at_unix"],
        "prospective_after_close_time": reservation["prospective_after_close_time"],
        "selected_model_id": str(cohort_payload.get("selected_model_id") or ""),
        "selected_spec": cohort_payload["selected_spec"],
        "base_model": cohort_payload["base_model"],
        "platt_calibrator": cohort_payload["platt_calibrator"],
        "selected_calibrator_method": cohort_payload[
            "selected_calibrator_method"
        ],
        "selected_margin": float(cohort_payload["selected_margin"]),
        "v20_feature_map_ablation_base_model": cohort_payload[
            "v20_feature_map_ablation_base_model"
        ],
        "v20_feature_map_ablation_selected_spec": cohort_payload[
            "v20_feature_map_ablation_selected_spec"
        ],
        "v20_feature_map_ablation_selected_model_id": cohort_payload[
            "v20_feature_map_ablation_selected_model_id"
        ],
        "v20_feature_map_ablation_platt_calibrator": cohort_payload[
            "v20_feature_map_ablation_platt_calibrator"
        ],
        "v20_feature_map_ablation_selected_calibrator_method": cohort_payload[
            "v20_feature_map_ablation_selected_calibrator_method"
        ],
        "bindings": bindings,
        "paper_only": True,
        "notification_label": "V21 PAPER",
        "runtime_scoring_connected": False,
        "notifications_enabled": False,
        "automatic_refit": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }


def _validate_artifact_payload(
    payload: Mapping[str, Any], *, cohort: str,
    reservation: Mapping[str, Any],
) -> None:
    bindings = payload.get("bindings")
    if not isinstance(bindings, Mapping):
        raise ValueError("v21_paper_artifact_payload_invalid")
    model_source = {"cohort": cohort, **{key: payload[key] for key in MODEL_KEYS}}
    model_source["selected_model_id"] = payload.get("selected_model_id")
    model_source["v20_feature_map_ablation_selected_model_id"] = payload.get(
        "v20_feature_map_ablation_selected_model_id"
    )
    checks = {
        "artifact_version": payload.get("artifact_version") == paper_identity.ARTIFACT_VERSION,
        "cohort": payload.get("cohort") == cohort,
        "assets": payload.get("assets") == COHORT_ASSETS[cohort],
        "created_at": payload.get("created_at") == reservation.get("created_at"),
        "created_at_unix": payload.get("created_at_unix") == reservation.get("created_at_unix"),
        "prospective_boundary": payload.get("prospective_after_close_time")
        == reservation.get("prospective_after_close_time"),
        "chain_bindings": not any(
            bindings.get(key) != value
            for key, value in reservation["bindings"].items()
        ),
        "model_payload_hash": bindings.get("model_payload_sha256")
        == _model_payload_sha256(model_source),
        "selected_margin_hash": bindings.get("selected_margin_sha256") == _canonical_sha256({
            "cohort": cohort,
            "selected_margin": float(payload["selected_margin"]),
        }),
        "paper_only": payload.get("paper_only") is True,
        "notification_label": payload.get("notification_label") == "V21 PAPER",
        "safety_flags": not any(payload.get(key) is not False for key in (
            "runtime_scoring_connected", "notifications_enabled",
            "automatic_refit", "automatic_promotion", "real_trading_allowed",
        )),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ValueError(
            "v21_paper_artifact_payload_invalid:" + ",".join(failed)
        )


def _write_joblib_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", suffix=".tmp",
            dir=path.parent, delete=False,
        ) as handle:
            temporary = Path(handle.name)
        joblib.dump(dict(payload), temporary, compress=0, protocol=5)
        with path.open("xb") as destination, temporary.open("rb") as source:
            shutil.copyfileobj(source, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _validate_result(
    result: Mapping[str, Any], reservation: Mapping[str, Any], output_dir: Path,
) -> None:
    artifacts = result.get("artifacts")
    if (
        result.get("state_version") != STATE_VERSION
        or result.get("status") != FINAL_STATUS
        or result.get("reservation_state_sha256") != reservation.get("state_sha256")
        or not isinstance(artifacts, Mapping)
        or set(artifacts) != set(COHORT_ASSETS)
        or any(result.get(key) is not False for key in (
            "outcome_labels_read_by_artifact_command", "model_fit_performed",
            "recalibration_performed", "margin_selection_performed",
            "runtime_scoring_connected", "notifications_enabled",
            "automatic_promotion", "real_trading_allowed",
        ))
    ):
        raise ValueError("v21_paper_artifact_result_invalid")
    for cohort, metadata in artifacts.items():
        path = output_dir / str(metadata.get("filename") or "")
        if (
            not path.is_file()
            or _file_sha256(path) != metadata.get("sha256")
            or path.stat().st_size != int(metadata.get("bytes") or -1)
        ):
            raise ValueError("v21_paper_artifact_file_invalid")
        try:
            payload = joblib.load(path)
        except Exception as exc:
            raise ValueError("v21_paper_artifact_file_unreadable") from exc
        if not isinstance(payload, Mapping):
            raise ValueError("v21_paper_artifact_file_invalid")
        _validate_artifact_payload(payload, cohort=cohort, reservation=reservation)


def create_paper_artifacts_once(
    *, seal: Mapping[str, Any], pretest_reservation_path: Path,
    untouched_reservation_path: Path, output_dir: Path, confirmation: str,
    timestamp: str | None = None,
) -> dict[str, Any]:
    preregister.validate_protocol(preregister.load_protocol())
    feature_seal.validate_seal(seal)
    chain = _load_passing_chain(
        seal=seal, pretest_reservation_path=pretest_reservation_path,
        untouched_reservation_path=untouched_reservation_path,
    )
    bindings = _chain_bindings(
        seal, chain, pretest_reservation_path, untouched_reservation_path,
    )
    reservation_path = output_dir / "artifact.reservation.json"
    result_path = output_dir / "artifact.result.json"
    if reservation_path.exists():
        reservation = _read_state(reservation_path)
        _validate_reservation(reservation, bindings)
        if result_path.exists():
            result = _read_state(result_path)
            _validate_result(result, reservation, output_dir)
            return {"status": "ALREADY_FINALIZED", "created": False, "result": result}
        return {"status": "AMBIGUOUS_RESERVED_NO_RETRY", "created": False, "result": None}
    if result_path.exists() or any((output_dir / f"{cohort}.joblib").exists() for cohort in COHORT_ASSETS):
        raise ValueError("v21_paper_artifact_output_exists_without_reservation")
    if confirmation != CONFIRMATION:
        raise ValueError("v21_paper_artifact_manual_confirmation_required")
    created_at, created_at_unix = _timestamp(timestamp)
    boundary = _first_complete_close_after_load(created_at_unix)
    reservation = _write_state_exclusive(
        reservation_path,
        _expected_reservation(
            bindings=bindings, created_at=created_at,
            created_at_unix=created_at_unix,
            prospective_after_close_time=boundary,
        ),
    )
    artifact_metadata = {}
    cohorts = chain["audit_bundle"]["cohorts"]
    for cohort in COHORT_ASSETS:
        payload = _artifact_payload(
            cohort=cohort, cohort_payload=cohorts[cohort],
            reservation=reservation,
        )
        _validate_artifact_payload(payload, cohort=cohort, reservation=reservation)
        path = output_dir / f"{cohort}.joblib"
        _write_joblib_exclusive(path, payload)
        artifact_metadata[cohort] = {
            "filename": path.name,
            "sha256": _file_sha256(path),
            "bytes": path.stat().st_size,
        }
    result = _write_state_exclusive(result_path, {
        "state_version": STATE_VERSION,
        "status": FINAL_STATUS,
        "finalized_at": created_at,
        "reservation_state_sha256": reservation["state_sha256"],
        "artifacts": artifact_metadata,
        "outcome_labels_read_by_artifact_command": False,
        "model_fit_performed": False,
        "recalibration_performed": False,
        "margin_selection_performed": False,
        "runtime_scoring_connected": False,
        "notifications_enabled": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    })
    _validate_result(result, reservation, output_dir)
    return {"status": FINAL_STATUS, "created": True, "result": result}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seal", default=str(feature_seal.DEFAULT_OUTPUT))
    parser.add_argument(
        "--pretest-reservation", default=str(pretest_command.DEFAULT_RESERVATION),
    )
    parser.add_argument(
        "--untouched-reservation",
        default=str(pretest_command.DEFAULT_STATE_DIR / "untouched-test-reservation.json"),
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--confirmation", required=True)
    args = parser.parse_args()
    result = create_paper_artifacts_once(
        seal=_load_seal(Path(args.seal)),
        pretest_reservation_path=Path(args.pretest_reservation),
        untouched_reservation_path=Path(args.untouched_reservation),
        output_dir=Path(args.output_dir),
        confirmation=args.confirmation,
    )
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
