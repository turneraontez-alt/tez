"""Build and validate V15's disjoint NON-BTC recovery audit seal.

The original pretest reservation was consumed before scoring when one local
settlement-cache row was unresolved.  This module never replays that run.  It
excludes every close window whose labels were authorized by the parent
reservation, regardless of outcome, then selects the earliest 60 remaining
complete V15 windows under the unchanged frozen protocol.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots import (
    rti_microstructure_v15_recovery_identity as identity,
)
from q15_upgrade.strategy_bots.rti_microstructure_v15_identity import (
    DESIGN_ID,
    DESIGN_SHA256,
    EVALUATION_PROTOCOL_ID,
    EVALUATION_PROTOCOL_SHA256,
)
from tools import q15_rti_v15_audit_seal as audit_seal
from tools import q15_rti_v15_pretest as pretest
from tools import q15_rti_v15_pretest_command as pretest_command
from tools import q15_rti_v15_untouched_test as untouched
from tools.q15_rti_microstructure_freeze import load_feature_rows
from tools.q15_rti_microstructure_preregister import design_fingerprint


DEFAULT_PROTOCOL = ROOT / identity.PROTOCOL_RELATIVE_PATH
DEFAULT_PARENT_SEAL = (
    ROOT
    / "reports"
    / "q15_rti_v15_audit_seals"
    / "non_btc_transfer-earliest-60-v3.json"
)
DEFAULT_PARENT_RESERVATION = (
    ROOT
    / "reports"
    / "q15_rti_v15_audit_runs"
    / "non_btc_transfer"
    / "pretest-reservation.json"
)
DEFAULT_OUTPUT = ROOT / identity.DEFAULT_SEAL_RELATIVE_PATH
COHORT = "NON_BTC_TRANSFER"


def _load_mapping(path: Path, *, error: str) -> dict[str, Any]:
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(error) from exc
    if not isinstance(decoded, Mapping):
        raise ValueError(error)
    return dict(decoded)


def load_protocol(path: Path = DEFAULT_PROTOCOL) -> dict[str, Any]:
    protocol = _load_mapping(
        path, error="v15_recovery_protocol_unreadable",
    )
    if (
        design_fingerprint(protocol) != identity.PROTOCOL_SHA256
        or protocol.get("protocol_id") != identity.PROTOCOL_ID
        or protocol.get("protocol_status")
        != "FROZEN_BEFORE_ANY_RECOVERY_POPULATION_LABEL_ACCESS"
        or protocol.get("cohort") != COHORT
        or protocol.get("applies_to_design_id") != DESIGN_ID
        or protocol.get("applies_to_design_sha256") != DESIGN_SHA256
        or protocol.get("applies_to_evaluation_protocol_id")
        != EVALUATION_PROTOCOL_ID
        or protocol.get("applies_to_evaluation_protocol_sha256")
        != EVALUATION_PROTOCOL_SHA256
        or protocol.get("selection_rule") != identity.SELECTION_RULE
        or int(protocol.get("minimum_complete_close_windows") or 0) != 60
        or int(protocol.get("authorized_parent_pretest_close_windows") or 0)
        != 48
        or int(protocol.get("authorized_parent_pretest_rows") or 0) != 288
        or int(protocol.get("development_train_windows") or 0) != 36
        or int(protocol.get("calibration_windows") or 0) != 12
        or int(protocol.get("untouched_test_windows") or 0) != 12
        or protocol.get("parent_pretest_result_must_be_absent") is not True
        or protocol.get(
            "all_parent_pretest_close_windows_excluded_regardless_of_outcome"
        ) is not True
        or protocol.get(
            "features_models_thresholds_costs_and_gates_unchanged"
        ) is not True
        or protocol.get("outcome_values_used_for_recovery_selection") is not False
        or protocol.get("recovery_population_labels_read_before_freeze") is not False
        or protocol.get("diagnostic_outcomes_may_enter_recovery_population")
        is not False
        or protocol.get("other_cohort_labels_remain_sealed") is not True
        or protocol.get("automatic_promotion") is not False
        or protocol.get("real_trading_allowed") is not False
    ):
        raise ValueError("v15_recovery_protocol_identity_or_safety_invalid")
    return protocol


def _parent_context(
    rows: Sequence[Mapping[str, Any]],
    *,
    protocol: Mapping[str, Any],
    parent_seal_path: Path = DEFAULT_PARENT_SEAL,
    parent_reservation_path: Path = DEFAULT_PARENT_RESERVATION,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    tuple[float, ...],
    tuple[int, ...],
]:
    parent_seal = pretest_command.load_ready_seal(parent_seal_path)
    parent_reservation = pretest._read_sealed(parent_reservation_path)
    if pretest.result_path_for(parent_reservation_path).exists():
        raise ValueError("v15_recovery_parent_result_must_be_absent")
    if (
        parent_seal.get("seal_sha256")
        != protocol.get("parent_audit_seal_sha256")
        or parent_reservation.get("state_sha256")
        != protocol.get("parent_pretest_reservation_sha256")
        or parent_reservation.get("status")
        != protocol.get("parent_pretest_reservation_status")
        or parent_reservation.get("audit_seal_sha256")
        != parent_seal.get("seal_sha256")
        or parent_reservation.get("walk_forward_scoring_performed") is not False
        or parent_reservation.get("calibration_scoring_performed") is not False
        or parent_reservation.get("untouched_test_labels_read") is not False
        or parent_reservation.get("untouched_test_scoring_performed") is not False
        or parent_reservation.get("paper_artifact_created") is not False
        or parent_reservation.get("notification_eligible") is not False
        or parent_reservation.get("real_trading_allowed") is not False
    ):
        raise ValueError("v15_recovery_parent_state_invalid")

    design, _v14, _charter, evaluation, _geometry, _sha = (
        audit_seal._load_inputs()
    )
    selected = pretest_command.select_sealed_feature_rows(
        rows, parent_seal,
    )
    _, parent_pretest, parent_test = untouched._prepare_projected_rows(
        selected,
        seal=parent_seal,
        cohort=COHORT,
        protocol=evaluation,
    )
    excluded_times = tuple(sorted({
        float(row["close_time"]) for row in parent_pretest
    }))
    excluded_ids = tuple(sorted(int(row["id"]) for row in parent_pretest))
    test_times = tuple(sorted({
        float(row["close_time"]) for row in parent_test
    }))
    test_ids = tuple(sorted(int(row["id"]) for row in parent_test))
    if (
        len(excluded_times) != 48
        or len(excluded_ids) != 288
        or audit_seal.canonical_sha256(excluded_times)
        != protocol.get("excluded_parent_pretest_close_times_sha256")
        or audit_seal.canonical_sha256(excluded_ids)
        != protocol.get("excluded_parent_pretest_row_ids_sha256")
        or audit_seal.canonical_sha256(test_times)
        != protocol.get("still_sealed_parent_test_close_times_sha256")
        or audit_seal.canonical_sha256(test_ids)
        != protocol.get("still_sealed_parent_test_row_ids_sha256")
    ):
        raise ValueError("v15_recovery_parent_partition_identity_invalid")
    return parent_seal, parent_reservation, excluded_times, excluded_ids


def _filtered_rows(
    rows: Sequence[Mapping[str, Any]],
    excluded_times: Sequence[float],
) -> list[dict[str, Any]]:
    excluded = set(float(value) for value in excluded_times)
    return [
        dict(row)
        for row in rows
        if float(row.get("close_time") or 0.0) not in excluded
    ]


def build_recovery_seal(
    rows: Sequence[Mapping[str, Any]],
    *,
    generated_at: str | None = None,
    recovery_protocol: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    recovery_protocol = dict(recovery_protocol or load_protocol())
    parent, reservation, excluded_times, excluded_ids = _parent_context(
        rows, protocol=recovery_protocol,
    )
    filtered = _filtered_rows(rows, excluded_times)
    (
        design,
        v14_design,
        charter,
        evaluation,
        geometry,
        geometry_sha,
    ) = audit_seal._load_inputs()
    result = audit_seal.build_audit_seal(
        filtered,
        cohort=COHORT,
        design=design,
        v14_design=v14_design,
        charter=charter,
        protocol=evaluation,
        geometry_artifact=geometry,
        geometry_artifact_file_sha256=geometry_sha,
        generated_at=generated_at,
    )
    if result.get("status") != audit_seal.READY_STATUS:
        raise ValueError("v15_recovery_population_not_ready")
    result.update({
        "selection": identity.SELECTION_RULE,
        "recovery_protocol_id": identity.PROTOCOL_ID,
        "recovery_protocol_sha256": identity.PROTOCOL_SHA256,
        "recovery_parent_audit_seal_sha256": parent["seal_sha256"],
        "recovery_parent_pretest_reservation_sha256": (
            reservation["state_sha256"]
        ),
        "recovery_excluded_close_windows": len(excluded_times),
        "recovery_excluded_rows": len(excluded_ids),
        "recovery_excluded_close_times_sha256": (
            audit_seal.canonical_sha256(excluded_times)
        ),
        "recovery_excluded_row_ids_sha256": (
            audit_seal.canonical_sha256(excluded_ids)
        ),
        "recovery_population_labels_read_before_freeze": False,
        "recovery_outcome_values_used_for_selection": False,
        "recovery_diagnostic_outcomes_may_enter_population": False,
        "recovery_features_models_thresholds_costs_and_gates_unchanged": True,
    })
    result["seal_sha256"] = audit_seal.seal_fingerprint(result)
    validate_recovery_seal(result, recovery_protocol=recovery_protocol)
    return result


def validate_recovery_seal(
    seal: Mapping[str, Any],
    *,
    recovery_protocol: Mapping[str, Any] | None = None,
) -> None:
    audit_seal.validate_audit_seal(seal)
    protocol = dict(recovery_protocol or load_protocol())
    if (
        seal.get("cohort") != COHORT
        or seal.get("selection") != identity.SELECTION_RULE
        or seal.get("recovery_protocol_id") != identity.PROTOCOL_ID
        or seal.get("recovery_protocol_sha256") != identity.PROTOCOL_SHA256
        or seal.get("recovery_parent_audit_seal_sha256")
        != protocol.get("parent_audit_seal_sha256")
        or seal.get("recovery_parent_pretest_reservation_sha256")
        != protocol.get("parent_pretest_reservation_sha256")
        or int(seal.get("recovery_excluded_close_windows") or 0) != 48
        or int(seal.get("recovery_excluded_rows") or 0) != 288
        or seal.get("recovery_excluded_close_times_sha256")
        != protocol.get("excluded_parent_pretest_close_times_sha256")
        or seal.get("recovery_excluded_row_ids_sha256")
        != protocol.get("excluded_parent_pretest_row_ids_sha256")
        or seal.get("recovery_population_labels_read_before_freeze") is not False
        or seal.get("recovery_outcome_values_used_for_selection") is not False
        or seal.get("recovery_diagnostic_outcomes_may_enter_population") is not False
        or seal.get(
            "recovery_features_models_thresholds_costs_and_gates_unchanged"
        ) is not True
    ):
        raise ValueError("v15_recovery_seal_identity_or_safety_invalid")


def select_recovery_feature_rows(
    rows: Sequence[Mapping[str, Any]],
    seal: Mapping[str, Any],
) -> list[dict[str, Any]]:
    protocol = load_protocol()
    validate_recovery_seal(seal, recovery_protocol=protocol)
    _parent, _reservation, excluded_times, _ids = _parent_context(
        rows, protocol=protocol,
    )
    selected = pretest_command.select_sealed_feature_rows(
        _filtered_rows(rows, excluded_times),
        seal,
    )
    if any(
        float(row["close_time"]) in set(excluded_times)
        for row in selected
    ):
        raise ValueError("v15_recovery_selected_excluded_window")
    return selected


def load_recovery_seal(path: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    seal = _load_mapping(
        path, error="v15_recovery_seal_unreadable",
    )
    validate_recovery_seal(seal)
    return seal


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--strategy-db", default=str(audit_seal.DEFAULT_DB),
    )
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    seal = build_recovery_seal(
        load_feature_rows(Path(args.strategy_db)),
    )
    audit_seal.write_seal_exclusive(Path(args.output), seal)
    print(json.dumps(seal, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
