"""Manual command for the sealed one-shot V15 untouched test.

This command accepts only a finalized passing pretest state.  It validates
that state against the current seal and reconstructed outcome-free evidence,
then delegates to the append-only untouched-test runner.  Its read-only label
callback receives only the exact untouched-test IDs after the test
reservation has been written.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Callable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import q15_rti_v15_audit_seal as audit_seal
from tools import q15_rti_v15_pretest as pretest
from tools import q15_rti_v15_pretest_command as pretest_command
from tools import q15_rti_v15_untouched_test as untouched
from tools.q15_rti_microstructure_freeze import load_feature_rows


def default_pretest_reservation_path(cohort: str) -> Path:
    return pretest_command.default_reservation_path(cohort)


def default_test_reservation_path(cohort: str) -> Path:
    return (
        pretest_command.DEFAULT_STATE_DIR
        / str(cohort).lower()
        / "untouched-test-reservation.json"
    )


def load_verified_passing_pretest(
    *,
    reservation_path: Path,
    seal: Mapping[str, Any],
    selected_feature_rows: Sequence[Mapping[str, Any]],
    protocol: Mapping[str, Any],
    cohort: str,
) -> dict[str, Any]:
    _, pretest_rows, test_rows = untouched._prepare_projected_rows(
        selected_feature_rows,
        seal=seal,
        cohort=cohort,
        protocol=protocol,
    )
    expected = pretest._expected_binding(
        seal=seal,
        cohort=cohort,
        pretest_rows=pretest_rows,
        test_rows=test_rows,
    )
    reservation = pretest._read_sealed(Path(reservation_path))
    pretest._validate_existing_reservation(reservation, expected)
    result_path = pretest.result_path_for(Path(reservation_path))
    if not result_path.exists():
        raise ValueError(
            "v15_test_command_pretest_reserved_but_not_finalized"
        )
    result = pretest._read_sealed(result_path)
    pretest._validate_existing_result(result, reservation)
    if result.get("status") != pretest.PASS_STATUS:
        raise ValueError("v15_test_command_pretest_gates_not_passed")
    label_rows = list(result["pretest_label_rows"])
    labels = untouched._validated_labels(
        {
            int(item["id"]): int(item["label_yes"])
            for item in label_rows
        },
        tuple(sorted(int(row["id"]) for row in pretest_rows)),
        stage="pretest",
    )
    return {
        "reservation": reservation,
        "result": result,
        "pretest_labels": labels,
        "walk_forward_report": dict(result["walk_forward_report"]),
        "calibration_report": dict(result["calibration_report"]),
    }


def run_verified_untouched_test_once(
    *,
    pretest_reservation_path: Path,
    test_reservation_path: Path,
    seal: Mapping[str, Any],
    selected_feature_rows: Sequence[Mapping[str, Any]],
    design: Mapping[str, Any],
    protocol: Mapping[str, Any],
    reporting_protocol: Mapping[str, Any],
    cohort: str,
    confirmation: str,
    read_untouched_test_labels: Callable[
        [Sequence[int]], Mapping[int, int]
    ],
    require_label_evidence: bool = False,
) -> dict[str, Any]:
    verified = load_verified_passing_pretest(
        reservation_path=pretest_reservation_path,
        seal=seal,
        selected_feature_rows=selected_feature_rows,
        protocol=protocol,
        cohort=cohort,
    )
    return untouched.run_untouched_test_once(
        seal=seal,
        selected_feature_rows=selected_feature_rows,
        pretest_labels=verified["pretest_labels"],
        supplied_walk_forward_report=verified["walk_forward_report"],
        supplied_calibration_report=verified["calibration_report"],
        design=design,
        protocol=protocol,
        reporting_protocol=reporting_protocol,
        cohort=cohort,
        reservation_path=test_reservation_path,
        confirmation=confirmation,
        read_untouched_test_labels=read_untouched_test_labels,
        require_label_evidence=require_label_evidence,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cohort", choices=tuple(audit_seal.COHORT_ASSETS))
    parser.add_argument("--seal", required=True)
    parser.add_argument(
        "--strategy-db", default=str(audit_seal.DEFAULT_DB),
    )
    parser.add_argument("--pretest-reservation")
    parser.add_argument("--test-reservation")
    parser.add_argument("--confirmation", required=True)
    args = parser.parse_args()

    seal = pretest_command.load_ready_seal(Path(args.seal))
    cohort = str(seal["cohort"])
    if args.cohort is not None and args.cohort != cohort:
        raise ValueError("v15_test_command_cohort_mismatch")
    (
        design,
        _v14_design,
        _charter,
        protocol,
        _artifact,
        _artifact_sha,
    ) = audit_seal._load_inputs()
    database_path = Path(args.strategy_db)
    selected = pretest_command.select_sealed_feature_rows(
        load_feature_rows(database_path),
        seal,
    )
    pretest_reservation_path = (
        Path(args.pretest_reservation)
        if args.pretest_reservation
        else default_pretest_reservation_path(cohort)
    )
    reporting = untouched.load_reporting_protocol()
    test_reservation_path = (
        Path(args.test_reservation)
        if args.test_reservation
        else default_test_reservation_path(cohort)
    )
    outcome = run_verified_untouched_test_once(
        pretest_reservation_path=pretest_reservation_path,
        test_reservation_path=test_reservation_path,
        seal=seal,
        selected_feature_rows=selected,
        design=design,
        protocol=protocol,
        reporting_protocol=reporting,
        cohort=cohort,
        confirmation=args.confirmation,
        read_untouched_test_labels=(
            pretest_command.KalshiVerifiedSQLiteLabelReader(
                database_path,
                expected_rows=selected,
            )
        ),
        require_label_evidence=True,
    )
    print(json.dumps(outcome, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
