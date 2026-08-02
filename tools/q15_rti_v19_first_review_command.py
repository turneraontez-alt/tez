"""Manual one-shot command for V19's sealed first prospective review."""
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
from q15_upgrade.strategy_bots import rti_microstructure_v19 as v19
from q15_upgrade.strategy_bots import rti_microstructure_v19_audit_identity as identity
from q15_upgrade.strategy_bots import rti_microstructure_v19_identity as v19_identity
from tools import q15_rti_v19_first_review_runner as runner
from tools import q15_rti_v19_prospective_seal as prospective_seal
from tools.q15_rti_microstructure_freeze import load_feature_rows_after
from tools.q15_rti_v15_pretest_command import KalshiVerifiedSQLiteLabelReader
from tools.q15_rti_v19_readiness import load_delayed_feature_rows_after


DEFAULT_RESERVATION = (
    ROOT / "reports" / "q15_rti_v19_first_review_runs"
    / "non_btc_transfer" / "first-review-reservation.json"
)


def load_seal(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("v19_first_review_command_seal_unreadable") from exc
    if not isinstance(value, Mapping):
        raise ValueError("v19_first_review_command_seal_root_not_object")
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
            database, v19_identity.PROSPECTIVE_AFTER_CLOSE_TIME,
        ),
        load_delayed_feature_rows_after(
            database, v19_identity.PROSPECTIVE_AFTER_CLOSE_TIME,
        ),
        seal,
    )
    control = examples["control"]
    candidate_pair_ids = tuple(sorted(
        (int(row["parent_id"]), int(row["delayed_id"]))
        for row in examples["candidate"]
    ))
    expected_label_rows = [
        {**row, "id": int(row["parent_id"])} for row in control
    ]
    contract = prospective_seal.load_contract()
    protocol = v19.load_protocol()
    access = dict(contract["label_access"])
    kalshi = KalshiClient(
        rate=float(access["authoritative_verifier_max_requests_per_second"]),
        capacity=int(access["authoritative_verifier_capacity"]),
    )
    result = runner.run_first_review_once(
        seal=seal,
        control_rows=control,
        candidate_pair_ids=candidate_pair_ids,
        reservation_path=Path(args.reservation),
        confirmation=args.confirmation,
        read_control_labels=KalshiVerifiedSQLiteLabelReader(
            database,
            expected_rows=expected_label_rows,
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
