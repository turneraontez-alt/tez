"""Outcome-blind readiness for frozen V21 parent/+30s/+60s triplets."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots import rti_microstructure_v21 as v21
from q15_upgrade.strategy_bots import rti_microstructure_v21_identity as identity
from tools.q15_rti_microstructure_freeze import (
    _feature_only_sqlite_authorizer,
    load_feature_rows_after,
)
from tools.q15_rti_v17_development_seal import DEFAULT_DB
from tools.q15_rti_v18_prospective_seal import _complete_windows
from tools.q15_rti_v19_readiness import _linked_parent_id


ASSETS = frozenset({"BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP"})
DELAYED_SELECT_COLUMNS = (
    "id", "bot_name", "record_kind", "interval", "ticker", "asset", "side",
    "close_time", "paper_only", "entry_ask_cents", "spread_cents",
    "depth_contracts", "threshold_json",
)


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_trajectory_feature_rows_after(
    db_path: Path, after_close_time: float,
) -> list[dict[str, Any]]:
    """Read only outcome-free +30s/+60s evidence under SQLite denial."""
    connection = sqlite3.connect(
        f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True, timeout=30.0,
    )
    connection.row_factory = sqlite3.Row
    connection.set_authorizer(_feature_only_sqlite_authorizer)
    try:
        columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(strategy_bot_decisions)"
            ).fetchall()
        }
        if not set(DELAYED_SELECT_COLUMNS).issubset(columns):
            raise ValueError("v21_delayed_feature_schema_incomplete")
        query = (
            f"SELECT {','.join(DELAYED_SELECT_COLUMNS)} "
            "FROM strategy_bot_decisions "
            "WHERE bot_name='rti_path_13m' "
            "AND interval IN ('12M30S','12M') AND close_time>? "
            "ORDER BY close_time,id"
        )
        return [
            dict(row) for row in connection.execute(
                query, (float(after_close_time),),
            ).fetchall()
        ]
    finally:
        connection.close()


def build_readiness(
    parent_rows: Sequence[Mapping[str, Any]],
    trajectory_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    v21.load_protocol()
    v21.load_evaluator_contract()
    parents = {
        close_time: rows
        for close_time, rows in _complete_windows(parent_rows).items()
        if close_time > identity.PROSPECTIVE_AFTER_CLOSE_TIME
    }
    stages: dict[int, dict[str, list[Mapping[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in trajectory_rows:
        try:
            close_time = float(row.get("close_time") or 0.0)
        except (TypeError, ValueError):
            continue
        interval = str(row.get("interval") or "").upper()
        if (
            close_time > identity.PROSPECTIVE_AFTER_CLOSE_TIME
            and interval in {"12M30S", "12M"}
        ):
            stages[_linked_parent_id(row)][interval].append(row)

    complete_windows = 0
    feature_rows = []
    executable_rows = 0
    all_seven_executable_windows = 0
    rows_by_asset: Counter[str] = Counter()
    executable_by_asset: Counter[str] = Counter()
    rows_by_cohort: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    failure_examples = []
    missing_intermediate = missing_delayed = 0
    duplicate_intermediate = duplicate_delayed = 0
    for close_time, window in sorted(parents.items()):
        window_rows = []
        window_failed = False
        window_executable = 0
        for parent in window:
            parent_id = int(parent["id"])
            intermediate_matches = stages.get(parent_id, {}).get("12M30S", [])
            delayed_matches = stages.get(parent_id, {}).get("12M", [])
            if len(intermediate_matches) != 1 or len(delayed_matches) != 1:
                window_failed = True
                if not intermediate_matches:
                    missing_intermediate += 1
                elif len(intermediate_matches) > 1:
                    duplicate_intermediate += 1
                if not delayed_matches:
                    missing_delayed += 1
                elif len(delayed_matches) > 1:
                    duplicate_delayed += 1
                continue
            result = v21.evaluate_triplet(
                parent, intermediate_matches[0], delayed_matches[0]
            )
            if result.get("eligible_for_v21_feature_credit") is not True:
                window_failed = True
                failures.update(str(item) for item in result.get("failures") or ())
                if len(failure_examples) < 7:
                    failure_examples.append({
                        "close_time": close_time,
                        "asset": parent.get("asset"),
                        "parent_id": parent_id,
                        "intermediate_id": intermediate_matches[0].get("id"),
                        "delayed_id": delayed_matches[0].get("id"),
                        "failures": list(result.get("failures") or ()),
                    })
                continue
            evidence = dict(result["evidence"])
            window_rows.append({
                **evidence,
                "source_feature_evidence_sha256": result[
                    "source_feature_evidence_sha256"
                ],
            })
            if evidence.get("execution_supported") is True:
                window_executable += 1
        if (
            not window_failed
            and len(window_rows) == 7
            and {str(row.get("asset") or "").upper() for row in window_rows}
            == ASSETS
        ):
            complete_windows += 1
            feature_rows.extend(window_rows)
            for row in window_rows:
                asset = str(row["asset"])
                rows_by_asset[asset] += 1
                rows_by_cohort[str(row["cohort"])] += 1
                if row.get("execution_supported") is True:
                    executable_rows += 1
                    executable_by_asset[asset] += 1
            if window_executable == 7:
                all_seven_executable_windows += 1

    credited_identity = [
        {
            "parent_id": row["parent_id"],
            "intermediate_id": row["intermediate_id"],
            "delayed_id": row["delayed_id"],
            "asset": row["asset"],
            "close_time": row["close_time"],
            "feature_evidence_sha256": row["feature_evidence_sha256"],
            "execution_supported": row["execution_supported"],
        }
        for row in feature_rows
    ]
    return {
        "readiness_version": "q15-rti-v21-outcome-blind-readiness-v1",
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "evaluator_contract_id": identity.EVALUATOR_CONTRACT_ID,
        "evaluator_contract_sha256": identity.EVALUATOR_CONTRACT_SHA256,
        "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
        "feature_count": identity.FEATURE_COUNT,
        "feature_names_sha256": identity.FEATURE_NAMES_SHA256,
        "prospective_after_close_time": identity.PROSPECTIVE_AFTER_CLOSE_TIME,
        "first_eligible_close_time": identity.FIRST_ELIGIBLE_CLOSE_TIME,
        "minimum_complete_close_windows": identity.MINIMUM_COMPLETE_CLOSE_WINDOWS,
        "v21_feature_complete_close_windows": complete_windows,
        "complete_close_windows_remaining": max(
            0, identity.MINIMUM_COMPLETE_CLOSE_WINDOWS - complete_windows
        ),
        "feature_rows": sum(rows_by_asset.values()),
        "row_level_executable_feature_rows": executable_rows,
        "all_seven_executable_close_windows_diagnostic_only": (
            all_seven_executable_windows
        ),
        "rows_by_asset": dict(sorted(rows_by_asset.items())),
        "executable_rows_by_asset": dict(sorted(executable_by_asset.items())),
        "rows_by_cohort": dict(sorted(rows_by_cohort.items())),
        "missing_intermediate_pairs": missing_intermediate,
        "missing_delayed_pairs": missing_delayed,
        "duplicate_intermediate_pairs": duplicate_intermediate,
        "duplicate_delayed_pairs": duplicate_delayed,
        "feature_failure_counts": dict(sorted(failures.items())),
        "feature_failure_examples": failure_examples,
        "eligible_feature_evidence_sha256": _canonical_sha256(
            credited_identity
        ),
        "feature_credit_requires_all_rows_executable": False,
        "pnl_credit_requires_row_level_execution_supported": True,
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
        "status": (
            "READY_FOR_MANUAL_V21_FEATURE_SEAL"
            if complete_windows >= identity.MINIMUM_COMPLETE_CLOSE_WINDOWS
            else "COLLECTING_V21_PROSPECTIVE_FEATURES_NO_OUTCOMES"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-db", default=str(DEFAULT_DB))
    args = parser.parse_args()
    db_path = Path(args.strategy_db)
    parents = load_feature_rows_after(
        db_path, identity.PROSPECTIVE_AFTER_CLOSE_TIME,
    )
    trajectory = load_trajectory_feature_rows_after(
        db_path, identity.PROSPECTIVE_AFTER_CLOSE_TIME,
    )
    print(json.dumps(
        build_readiness(parents, trajectory), indent=2, sort_keys=True,
    ))


if __name__ == "__main__":
    main()
