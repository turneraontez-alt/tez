"""Manual authoritative-Kalshi command for V20 train/calibration labels."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots import rti_microstructure_v20_audit_identity as audit_identity
from tools import q15_rti_v15_pretest_command as authoritative
from tools import q15_rti_v20_feature_seal as feature_seal
from tools import q15_rti_v20_pretest_runner as runner


DEFAULT_STATE_DIR = ROOT / "reports" / "q15_rti_v20_audit"
DEFAULT_RESERVATION = DEFAULT_STATE_DIR / "pretest-reservation.json"


def load_feature_seal(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("v20_pretest_command_feature_seal_unreadable") from exc
    if not isinstance(value, Mapping):
        raise ValueError("v20_pretest_command_feature_seal_root_not_object")
    payload = dict(value)
    feature_seal.validate_seal(payload)
    return payload


def expected_database_rows(seal: Mapping[str, Any]) -> list[dict[str, Any]]:
    feature_seal.validate_seal(seal)
    return [
        {
            "id": int(row["parent_id"]),
            "ticker": str(row["ticker"]),
            "asset": str(row["asset"]),
            "close_time": float(row["close_time"]),
        }
        for row in seal["rows"]
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seal", default=str(feature_seal.DEFAULT_OUTPUT))
    parser.add_argument("--strategy-db", default=str(feature_seal.DEFAULT_DB))
    parser.add_argument("--reservation", default=str(DEFAULT_RESERVATION))
    parser.add_argument("--confirmation", required=True)
    args = parser.parse_args()
    seal = load_feature_seal(Path(args.seal))
    database_path = Path(args.strategy_db)
    result = runner.run_pretest_once(
        seal=seal,
        reservation_path=Path(args.reservation),
        confirmation=args.confirmation,
        read_settlement_yes_labels=(
            authoritative.KalshiVerifiedSQLiteLabelReader(
                database_path,
                expected_rows=expected_database_rows(seal),
            )
        ),
        require_label_evidence=True,
    )
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
