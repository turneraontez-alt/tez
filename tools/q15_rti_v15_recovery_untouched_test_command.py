"""Run V15's disjoint NON-BTC recovery untouched test exactly once."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots import (
    rti_microstructure_v15_recovery_identity as identity,
)
from tools import q15_rti_v15_audit_seal as audit_seal
from tools import q15_rti_v15_pretest_command as pretest_command
from tools import q15_rti_v15_recovery_audit as recovery
from tools import q15_rti_v15_untouched_test as untouched
from tools import q15_rti_v15_untouched_test_command as command
from tools.q15_rti_microstructure_freeze import load_feature_rows


DEFAULT_PRETEST_RESERVATION = (
    ROOT / identity.DEFAULT_STATE_RELATIVE_PATH
)
DEFAULT_TEST_RESERVATION = (
    ROOT / identity.DEFAULT_TEST_STATE_RELATIVE_PATH
)


def run(
    *,
    seal_path: Path,
    database_path: Path,
    pretest_reservation_path: Path,
    test_reservation_path: Path,
    confirmation: str,
) -> dict[str, Any]:
    seal = recovery.load_recovery_seal(seal_path)
    rows = load_feature_rows(database_path)
    selected = recovery.select_recovery_feature_rows(rows, seal)
    (
        design,
        _v14_design,
        _charter,
        protocol,
        _artifact,
        _artifact_sha,
    ) = audit_seal._load_inputs()
    return command.run_verified_untouched_test_once(
        pretest_reservation_path=pretest_reservation_path,
        test_reservation_path=test_reservation_path,
        seal=seal,
        selected_feature_rows=selected,
        design=design,
        protocol=protocol,
        reporting_protocol=untouched.load_reporting_protocol(),
        cohort=recovery.COHORT,
        confirmation=confirmation,
        read_untouched_test_labels=(
            pretest_command.KalshiVerifiedSQLiteLabelReader(
                database_path,
                expected_rows=selected,
            )
        ),
        require_label_evidence=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seal", default=str(recovery.DEFAULT_OUTPUT))
    parser.add_argument(
        "--strategy-db", default=str(audit_seal.DEFAULT_DB),
    )
    parser.add_argument(
        "--pretest-reservation",
        default=str(DEFAULT_PRETEST_RESERVATION),
    )
    parser.add_argument(
        "--test-reservation",
        default=str(DEFAULT_TEST_RESERVATION),
    )
    parser.add_argument("--confirmation", required=True)
    args = parser.parse_args()
    outcome = run(
        seal_path=Path(args.seal),
        database_path=Path(args.strategy_db),
        pretest_reservation_path=Path(args.pretest_reservation),
        test_reservation_path=Path(args.test_reservation),
        confirmation=args.confirmation,
    )
    print(json.dumps(outcome, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
