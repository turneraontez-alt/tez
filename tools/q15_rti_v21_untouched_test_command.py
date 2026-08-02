"""Manual authoritative-Kalshi command for V21's one-shot untouched test."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import q15_rti_v21_feature_seal as feature_seal
from tools import q15_rti_v21_pretest_command as pretest_command
from tools import q15_rti_v21_untouched_test_runner as runner


DEFAULT_RESERVATION = (
    pretest_command.DEFAULT_STATE_DIR / "untouched-test-reservation.json"
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seal", default=str(feature_seal.DEFAULT_OUTPUT))
    parser.add_argument("--strategy-db", default=str(feature_seal.DEFAULT_DB))
    parser.add_argument(
        "--pretest-reservation",
        default=str(pretest_command.DEFAULT_RESERVATION),
    )
    parser.add_argument("--reservation", default=str(DEFAULT_RESERVATION))
    parser.add_argument("--confirmation", required=True)
    args = parser.parse_args()
    seal = pretest_command.load_feature_seal(Path(args.seal))
    label_reader = pretest_command.KalshiFeeVerifiedSQLiteLabelReader(
        Path(args.strategy_db),
        expected_rows=pretest_command.expected_database_rows(seal),
    )
    # Fee metadata contains no outcomes and is checked before reserving the
    # one-shot test labels, so endpoint failure cannot burn the test.
    label_reader.verify_fee_precondition()
    result = runner.run_untouched_test_once(
        seal=seal,
        pretest_reservation_path=Path(args.pretest_reservation),
        reservation_path=Path(args.reservation),
        confirmation=args.confirmation,
        read_settlement_yes_labels=label_reader,
        require_label_evidence=True,
    )
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
