"""Manual one-shot command for V18's sealed first prospective review."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.kalshi_rest import KalshiClient
from q15_upgrade.strategy_bots import rti_microstructure_v18 as v18
from q15_upgrade.strategy_bots import rti_microstructure_v18_audit_identity as identity
from q15_upgrade.strategy_bots import rti_microstructure_v18_identity as v18_identity
from tools import q15_rti_v18_first_review_evaluator as evaluator
from tools import q15_rti_v18_first_review_runner as runner
from tools import q15_rti_v18_prospective_seal as prospective_seal
from tools.q15_rti_microstructure_freeze import load_feature_rows_after
from tools.q15_rti_v15_pretest_command import KalshiVerifiedSQLiteLabelReader


DEFAULT_RESERVATION = (
    ROOT / "reports" / "q15_rti_v18_first_review_runs"
    / "non_btc_transfer" / "first-review-reservation.json"
)


def load_seal(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("v18_first_review_command_seal_unreadable") from exc
    if not isinstance(value, Mapping):
        raise ValueError("v18_first_review_command_seal_root_not_object")
    result = dict(value)
    prospective_seal.validate_seal(result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--seal",
        default=str(ROOT / identity.PROSPECTIVE_SEAL_RELATIVE_PATH),
    )
    parser.add_argument("--strategy-db", default=str(prospective_seal.DEFAULT_DB))
    parser.add_argument("--reservation", default=str(DEFAULT_RESERVATION))
    parser.add_argument("--confirmation", required=True)
    args = parser.parse_args()

    seal = load_seal(Path(args.seal))
    database = Path(args.strategy_db)
    examples = prospective_seal.reconstruct_examples(
        load_feature_rows_after(
            database, v18_identity.PROSPECTIVE_AFTER_CLOSE_TIME,
        ),
        seal,
    )
    control = examples["control"]
    candidate_ids = tuple(sorted(int(row["id"]) for row in examples["candidate"]))
    contract = prospective_seal.load_contract()
    protocol = v18.load_protocol()
    access = dict(contract["label_access"])
    kalshi = KalshiClient(
        rate=float(access["authoritative_verifier_max_requests_per_second"]),
        capacity=int(access["authoritative_verifier_capacity"]),
    )
    result = runner.run_first_review_once(
        seal=seal,
        control_rows=control,
        candidate_ids=candidate_ids,
        reservation_path=Path(args.reservation),
        confirmation=args.confirmation,
        read_control_labels=KalshiVerifiedSQLiteLabelReader(
            database,
            expected_rows=control,
            get_market=kalshi.get_market,
            fetch_attempts=int(access["fetch_attempts_per_contract"]),
        ),
        contract=contract,
        protocol=protocol,
        require_label_evidence=True,
    )
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
