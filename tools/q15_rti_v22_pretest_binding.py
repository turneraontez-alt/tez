"""Outcome-blind V22 pretest label-population binding.

This module cannot read labels, access a database or network, fit a model,
write a reservation, send a notification, or place an order.  It binds the
future one-shot runner to the exact rows it may read after the 180-window seal.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots import rti_microstructure_v22 as v22  # noqa: E402
from q15_upgrade.strategy_bots import rti_microstructure_v22_audit_identity as audit_identity  # noqa: E402
from q15_upgrade.strategy_bots import rti_microstructure_v22_identity as identity  # noqa: E402
from tools import q15_rti_v22_feature_seal as feature_seal  # noqa: E402


TRAIN = "TRAIN"
CALIBRATION = "PROBABILITY_CALIBRATION"
POLICY = "EXECUTION_POLICY_SELECTION"
TEST = "UNTOUCHED_TEST"


def _sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode()).hexdigest()


def required_pretest_rows(seal: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return only rows consumed before the untouched test is opened."""
    feature_seal.validate_seal(seal)
    return [
        dict(row) for row in seal["rows"]
        if row["partition"] in {TRAIN, CALIBRATION}
        or (
            row["partition"] == POLICY
            and row.get("execution_supported") is True
        )
    ]


def untouched_test_rows(seal: Mapping[str, Any]) -> list[dict[str, Any]]:
    feature_seal.validate_seal(seal)
    return [dict(row) for row in seal["rows"] if row["partition"] == TEST]


def _row_ids(rows: Sequence[Mapping[str, Any]]) -> tuple[int, ...]:
    return tuple(sorted(int(row["parent_id"]) for row in rows))


def _feature_identity(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    return tuple(sorted(({
        "parent_id": int(row["parent_id"]),
        "intermediate_id": int(row["intermediate_id"]),
        "delayed_id": int(row["delayed_id"]),
        "asset": str(row["asset"]),
        "ticker": str(row["ticker"]),
        "close_time": float(row["close_time"]),
        "partition": str(row["partition"]),
        "execution_supported": row["execution_supported"] is True,
        "feature_evidence_sha256": str(row["feature_evidence_sha256"]),
        "parent_source_evidence_sha256": str(
            row["parent_source_evidence_sha256"]
        ),
        "intermediate_source_evidence_sha256": str(
            row["intermediate_source_evidence_sha256"]
        ),
        "delayed_source_evidence_sha256": str(
            row["delayed_source_evidence_sha256"]
        ),
        "rest_evidence_sha256_by_stage": dict(
            row["rest_evidence_sha256_by_stage"]
        ),
    } for row in rows), key=lambda item: item["parent_id"]))


def expected_binding(
    seal: Mapping[str, Any], *, label_evidence_required: bool = True,
) -> dict[str, Any]:
    """Hash-bind allowed pretest rows while proving test IDs stay sealed."""
    v22.load_protocol()
    v22.load_evaluator_contract()
    feature_seal.validate_seal(seal)
    pretest_rows = required_pretest_rows(seal)
    test_rows = untouched_test_rows(seal)
    pretest_ids = _row_ids(pretest_rows)
    test_ids = _row_ids(test_rows)
    if label_evidence_required is not True:
        raise ValueError("v22_pretest_authoritative_label_evidence_required")
    if (
        not 910 <= len(pretest_rows) <= 1085
        or len(test_rows) != 175
        or len(set(pretest_ids)) != len(pretest_ids)
        or len(set(test_ids)) != len(test_ids)
        or set(pretest_ids) & set(test_ids)
        or any(
            row["partition"] == POLICY
            and row.get("execution_supported") is not True
            for row in pretest_rows
        )
    ):
        raise ValueError("v22_pretest_binding_population_invalid")
    binding = {
        "binding_version": audit_identity.PRETEST_BINDING_VERSION,
        "pretest_runner_version": audit_identity.PRETEST_RUNNER_VERSION,
        "pretest_state_version": audit_identity.PRETEST_STATE_VERSION,
        "pretest_confirmation": audit_identity.PRETEST_CONFIRMATION,
        "untouched_test_runner_version": (
            audit_identity.UNTOUCHED_TEST_RUNNER_VERSION
        ),
        "untouched_test_state_version": (
            audit_identity.UNTOUCHED_TEST_STATE_VERSION
        ),
        "untouched_test_confirmation": audit_identity.UNTOUCHED_TEST_CONFIRMATION,
        "evaluator_contract_id": identity.EVALUATOR_CONTRACT_ID,
        "evaluator_contract_sha256": identity.EVALUATOR_CONTRACT_SHA256,
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "feature_seal_sha256": str(seal["seal_sha256"]),
        "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
        "feature_names_sha256": identity.FEATURE_NAMES_SHA256,
        "pretest_label_rows": len(pretest_rows),
        "untouched_test_rows": len(test_rows),
        "pretest_label_row_ids_sha256": _sha256(pretest_ids),
        "untouched_test_row_ids_sha256": _sha256(test_ids),
        "pretest_feature_identity_sha256": _sha256(
            _feature_identity(pretest_rows)
        ),
        "untouched_test_feature_identity_sha256": _sha256(
            _feature_identity(test_rows)
        ),
        "label_evidence_required": True,
        "policy_nonexecutable_labels_forbidden": True,
        "untouched_test_labels_read": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "reservation_created": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }
    binding["binding_sha256"] = _sha256(binding)
    validate_binding(binding, seal)
    return binding


def validate_binding(
    binding: Mapping[str, Any], seal: Mapping[str, Any],
) -> dict[str, Any]:
    feature_seal.validate_seal(seal)
    supplied = dict(binding)
    binding_sha = str(supplied.pop("binding_sha256", ""))
    pretest_rows = required_pretest_rows(seal)
    test_rows = untouched_test_rows(seal)
    expected = {
        "binding_version": audit_identity.PRETEST_BINDING_VERSION,
        "pretest_runner_version": audit_identity.PRETEST_RUNNER_VERSION,
        "pretest_state_version": audit_identity.PRETEST_STATE_VERSION,
        "pretest_confirmation": audit_identity.PRETEST_CONFIRMATION,
        "untouched_test_runner_version": audit_identity.UNTOUCHED_TEST_RUNNER_VERSION,
        "untouched_test_state_version": audit_identity.UNTOUCHED_TEST_STATE_VERSION,
        "untouched_test_confirmation": audit_identity.UNTOUCHED_TEST_CONFIRMATION,
        "evaluator_contract_id": identity.EVALUATOR_CONTRACT_ID,
        "evaluator_contract_sha256": identity.EVALUATOR_CONTRACT_SHA256,
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "feature_seal_sha256": str(seal["seal_sha256"]),
        "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
        "feature_names_sha256": identity.FEATURE_NAMES_SHA256,
        "pretest_label_rows": len(pretest_rows),
        "untouched_test_rows": len(test_rows),
        "pretest_label_row_ids_sha256": _sha256(_row_ids(pretest_rows)),
        "untouched_test_row_ids_sha256": _sha256(_row_ids(test_rows)),
        "pretest_feature_identity_sha256": _sha256(_feature_identity(pretest_rows)),
        "untouched_test_feature_identity_sha256": _sha256(_feature_identity(test_rows)),
        "label_evidence_required": True,
        "policy_nonexecutable_labels_forbidden": True,
        "untouched_test_labels_read": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "reservation_created": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }
    if supplied != expected or binding_sha != _sha256(supplied):
        raise ValueError("v22_pretest_binding_invalid")
    return {
        "valid": True,
        "binding_sha256": binding_sha,
        "pretest_label_rows": len(pretest_rows),
        "untouched_test_rows": len(test_rows),
        "outcome_labels_read": False,
        "reservation_created": False,
        "real_trading_allowed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seal", default=str(feature_seal.DEFAULT_OUTPUT))
    args = parser.parse_args()
    path = Path(args.seal)
    if not path.exists():
        print(json.dumps({
            **v22.status(),
            "feature_seal_exists": False,
            "pretest_binding_created": False,
            "reservation_created": False,
            "outcome_labels_read": False,
            "model_fit_performed": False,
            "probability_scoring_performed": False,
            "status": "AWAITING_V22_EARLIEST_180_FEATURE_SEAL",
        }, indent=2, sort_keys=True))
        return
    try:
        seal = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("v22_pretest_binding_feature_seal_unreadable") from exc
    if not isinstance(seal, Mapping):
        raise ValueError("v22_pretest_binding_feature_seal_invalid")
    print(json.dumps(expected_binding(seal), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
