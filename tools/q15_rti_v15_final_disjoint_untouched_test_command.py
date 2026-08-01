"""Run the fully disjoint V15 NON-BTC untouched test once."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import q15_rti_v15_audit_seal as audit_seal
from tools import q15_rti_v15_final_disjoint_audit as final
from tools import q15_rti_v15_pretest_command as pretest_command
from tools import q15_rti_v15_untouched_test as untouched
from tools import q15_rti_v15_untouched_test_command as command
from tools.q15_rti_microstructure_freeze import load_feature_rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seal", default=str(final.DEFAULT_OUTPUT))
    parser.add_argument("--strategy-db", default=str(audit_seal.DEFAULT_DB))
    parser.add_argument(
        "--pretest-reservation", default=str(final.DEFAULT_PRETEST_STATE),
    )
    parser.add_argument(
        "--test-reservation", default=str(final.DEFAULT_TEST_STATE),
    )
    parser.add_argument("--confirmation", required=True)
    args = parser.parse_args()
    seal = final.load_seal(Path(args.seal))
    database = Path(args.strategy_db)
    selected = final.select_rows(load_feature_rows(database), seal)
    design, _v14, _charter, protocol, _geometry, _sha = (
        audit_seal._load_inputs()
    )
    result = command.run_verified_untouched_test_once(
        pretest_reservation_path=Path(args.pretest_reservation),
        test_reservation_path=Path(args.test_reservation),
        seal=seal,
        selected_feature_rows=selected,
        design=design,
        protocol=protocol,
        reporting_protocol=untouched.load_reporting_protocol(),
        cohort=final.COHORT,
        confirmation=args.confirmation,
        read_untouched_test_labels=(
            pretest_command.KalshiVerifiedSQLiteLabelReader(
                database, expected_rows=selected,
            )
        ),
        require_label_evidence=True,
    )
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
