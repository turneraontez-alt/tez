"""Read-only health for V21 collection, historical gates, and PAPER storage."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

import joblib


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots import rti_microstructure_v21_paper_identity as identity
from q15_upgrade.strategy_bots.rti_microstructure_v21_paper_ledger import V21PaperLedger
from tools import q15_rti_v21_paper_artifact as artifact
from tools import q15_rti_v21_paper_preregister as preregister
from tools import q15_rti_v21_readiness as readiness
from tools.q15_rti_v17_development_seal import DEFAULT_DB


def _collection_health(strategy_db: Path) -> dict[str, Any]:
    parents = readiness.load_feature_rows_after(
        strategy_db, readiness.identity.PROSPECTIVE_AFTER_CLOSE_TIME,
    )
    trajectories = readiness.load_trajectory_feature_rows_after(
        strategy_db, readiness.identity.PROSPECTIVE_AFTER_CLOSE_TIME,
    )
    return readiness.build_readiness(parents, trajectories)


def build_health(
    *, strategy_db: Path, artifact_dir: Path,
    ledger_root: Path | None = None,
) -> dict[str, Any]:
    protocol = preregister.validate_protocol(preregister.load_protocol())
    collection = _collection_health(strategy_db)
    reservation_path = artifact_dir / "artifact.reservation.json"
    result_path = artifact_dir / "artifact.result.json"
    artifact_files = {
        cohort: artifact_dir / f"{cohort}.joblib"
        for cohort in artifact.COHORT_ASSETS
    }
    any_artifact_file = any(path.exists() for path in artifact_files.values())
    base = {
        "health_version": "q15-rti-v21-paper-health-v1",
        "paper_protocol_id": identity.PROTOCOL_ID,
        "paper_protocol_sha256": identity.PROTOCOL_SHA256,
        "paper_protocol_valid": protocol["status"].startswith("VALID_"),
        "collection": collection,
        "paper_only": True,
        "runtime_scoring_connected": False,
        "notifications_enabled": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }
    if not reservation_path.exists():
        if result_path.exists() or any_artifact_file:
            return {
                **base,
                "status": "INVALID_ARTIFACT_STATE_FAIL_CLOSED",
                "artifact_created": False,
                "artifact_state_error": "OUTPUT_EXISTS_WITHOUT_RESERVATION",
                "cohort_ledgers": {},
            }
        return {
            **base,
            "status": "DORMANT_AWAITING_PASSING_HISTORICAL_AUDIT",
            "artifact_created": False,
            "artifact_state_error": None,
            "cohort_ledgers": {},
        }
    try:
        reservation = artifact._read_state(reservation_path)
        artifact._validate_reservation(reservation, reservation["bindings"])
    except (KeyError, TypeError, ValueError) as exc:
        return {
            **base,
            "status": "INVALID_ARTIFACT_STATE_FAIL_CLOSED",
            "artifact_created": False,
            "artifact_state_error": str(exc),
            "cohort_ledgers": {},
        }
    if not result_path.exists():
        return {
            **base,
            "status": "AMBIGUOUS_ARTIFACT_RESERVATION_FAIL_CLOSED",
            "artifact_created": False,
            "artifact_state_error": "RESERVATION_WITHOUT_FINAL_RESULT",
            "cohort_ledgers": {},
        }
    try:
        result = artifact._read_state(result_path)
        artifact._validate_result(result, reservation, artifact_dir)
    except (KeyError, TypeError, ValueError) as exc:
        return {
            **base,
            "status": "INVALID_ARTIFACT_STATE_FAIL_CLOSED",
            "artifact_created": False,
            "artifact_state_error": str(exc),
            "cohort_ledgers": {},
        }
    ledger_root = Path(".") if ledger_root is None else Path(ledger_root)
    cohort_ledgers = {}
    for cohort, relative_path in identity.DEFAULT_LEDGER_RELATIVE_PATHS.items():
        artifact_path = artifact_files[cohort]
        payload = joblib.load(artifact_path)
        ledger_path = ledger_root / relative_path
        if not ledger_path.exists():
            cohort_ledgers[cohort] = {
                "status": "NOT_CREATED_AWAITING_MANUAL_RUNTIME_ACTIVATION",
                "path": str(ledger_path),
            }
            continue
        ledger = V21PaperLedger(
            ledger_path,
            cohort=cohort,
            artifact_sha256=artifact._file_sha256(artifact_path),
            artifact_created_at_unix=float(payload["created_at_unix"]),
            prospective_after_close_time=float(payload["prospective_after_close_time"]),
        )
        cohort_ledgers[cohort] = {"path": str(ledger_path), **ledger.health()}
    return {
        **base,
        "status": "PAPER_ARTIFACT_VALID_NOT_RUNTIME_CONNECTED",
        "artifact_created": True,
        "artifact_state_error": None,
        "artifact_result_state_sha256": result["state_sha256"],
        "cohort_ledgers": cohort_ledgers,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-db", default=str(DEFAULT_DB))
    parser.add_argument("--artifact-dir", default=str(artifact.DEFAULT_OUTPUT_DIR))
    parser.add_argument("--ledger-root", default=".")
    args = parser.parse_args()
    print(json.dumps(build_health(
        strategy_db=Path(args.strategy_db),
        artifact_dir=Path(args.artifact_dir),
        ledger_root=Path(args.ledger_root),
    ), indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
