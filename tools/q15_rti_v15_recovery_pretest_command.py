"""Run V15's disjoint NON-BTC recovery pretest exactly once."""
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
from tools import q15_rti_v15_pretest as pretest
from tools import q15_rti_v15_pretest_command as pretest_command
from tools import q15_rti_v15_recovery_audit as recovery
from tools.q15_rti_microstructure_freeze import load_feature_rows


DEFAULT_RESERVATION = ROOT / identity.DEFAULT_STATE_RELATIVE_PATH


def run(
    *,
    seal_path: Path,
    database_path: Path,
    reservation_path: Path,
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
    return pretest.run_pretest_once(
        seal=seal,
        selected_feature_rows=selected,
        design=design,
        protocol=protocol,
        cohort=recovery.COHORT,
        reservation_path=reservation_path,
        confirmation=confirmation,
        read_pretest_labels=(
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
        "--reservation", default=str(DEFAULT_RESERVATION),
    )
    parser.add_argument("--confirmation", required=True)
    args = parser.parse_args()
    outcome = run(
        seal_path=Path(args.seal),
        database_path=Path(args.strategy_db),
        reservation_path=Path(args.reservation),
        confirmation=args.confirmation,
    )
    print(json.dumps(outcome, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
