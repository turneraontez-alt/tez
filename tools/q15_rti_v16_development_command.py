"""Manual, one-shot command for V16's sealed development evaluation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots import rti_microstructure_v16_audit_identity as identity
from tools import q15_rti_v16_development_evaluator as evaluator
from tools import q15_rti_v16_development_runner as runner
from tools import q15_rti_v16_development_seal as development_seal
from tools.q15_rti_microstructure_freeze import load_feature_rows
from tools.q15_rti_v15_pretest_command import KalshiVerifiedSQLiteLabelReader
from q15_upgrade.kalshi_rest import KalshiClient


DEFAULT_RESERVATION = (
    ROOT / "reports" / "q15_rti_v16_development_runs"
    / "non_btc_transfer" / "development-reservation.json"
)


def load_seal(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("v16_development_command_seal_unreadable") from exc
    if not isinstance(value, Mapping):
        raise ValueError("v16_development_command_seal_root_not_object")
    result = dict(value)
    development_seal.validate_development_seal(result)
    if result.get("seal_sha256") != identity.DEVELOPMENT_SEAL_SHA256:
        raise ValueError("v16_development_command_seal_identity_mismatch")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--seal",
        default=str(ROOT / identity.DEVELOPMENT_SEAL_RELATIVE_PATH),
    )
    parser.add_argument("--strategy-db", default=str(development_seal.DEFAULT_DB))
    parser.add_argument("--reservation", default=str(DEFAULT_RESERVATION))
    parser.add_argument("--confirmation", required=True)
    args = parser.parse_args()

    seal = load_seal(Path(args.seal))
    database = Path(args.strategy_db)
    examples = development_seal.reconstruct_development_examples(
        load_feature_rows(database), seal,
    )
    contract = evaluator.load_contract()
    protocol = evaluator.load_protocol()
    access = dict(contract["label_access"])
    kalshi = KalshiClient(
        rate=float(access["authoritative_verifier_max_requests_per_second"]),
        capacity=int(access["authoritative_verifier_capacity"]),
    )
    result = runner.run_development_once(
        seal=seal,
        development_rows=examples,
        reservation_path=Path(args.reservation),
        confirmation=args.confirmation,
        read_development_labels=KalshiVerifiedSQLiteLabelReader(
            database,
            expected_rows=examples,
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
