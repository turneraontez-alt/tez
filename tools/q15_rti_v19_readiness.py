"""Outcome-blind readiness counter for the frozen V19 fresh-60s study."""
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

from q15_upgrade.strategy_bots import rti_microstructure_v18 as v18
from q15_upgrade.strategy_bots import rti_microstructure_v19 as v19
from q15_upgrade.strategy_bots import rti_microstructure_v19_identity as identity
from tools.q15_rti_microstructure_freeze import (
    _feature_only_sqlite_authorizer,
    load_feature_rows_after,
)
from tools.q15_rti_v17_development_seal import DEFAULT_DB
from tools.q15_rti_v18_prospective_seal import _complete_windows


DELAYED_SELECT_COLUMNS = (
    "id", "bot_name", "record_kind", "interval", "ticker", "asset", "side",
    "close_time", "paper_only", "entry_ask_cents", "spread_cents",
    "depth_contracts", "threshold_json",
)


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def load_delayed_feature_rows_after(
    db_path: Path, after_close_time: float,
) -> list[dict[str, Any]]:
    """Select only outcome-free 12m rows under SQLite label denial."""
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
            raise ValueError("v19_delayed_feature_schema_incomplete")
        query = (
            f"SELECT {','.join(DELAYED_SELECT_COLUMNS)} "
            "FROM strategy_bot_decisions "
            "WHERE bot_name='rti_path_13m' AND interval='12M' "
            "AND close_time>? ORDER BY close_time,id"
        )
        return [
            dict(row) for row in connection.execute(
                query, (float(after_close_time),),
            ).fetchall()
        ]
    finally:
        connection.close()


def _linked_parent_id(row: Mapping[str, Any]) -> int:
    raw = row.get("threshold_json")
    if isinstance(raw, Mapping):
        profile = dict(raw)
    else:
        try:
            decoded = json.loads(str(raw or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            return 0
        profile = dict(decoded) if isinstance(decoded, Mapping) else {}
    try:
        return int(float(profile.get("rti_confirm_original_row_id") or 0))
    except (TypeError, ValueError):
        return 0


def build_readiness(
    parent_rows: Sequence[Mapping[str, Any]],
    delayed_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    complete_parents = _complete_windows(parent_rows)
    parent_times = tuple(
        close for close in sorted(complete_parents)
        if close > identity.PROSPECTIVE_AFTER_CLOSE_TIME
    )
    delayed_by_parent: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in delayed_rows:
        parent_id = _linked_parent_id(row)
        if parent_id > 0:
            delayed_by_parent[parent_id].append(row)

    matched_windows: dict[float, list[tuple[Mapping[str, Any], Mapping[str, Any]]]] = {}
    source_failures: Counter[str] = Counter()
    source_failure_examples = []
    missing_delayed_rows = duplicate_delayed_rows = 0
    for close in parent_times:
        pairs = []
        window_failed = False
        for parent in complete_parents[close]:
            matches = delayed_by_parent.get(int(parent["id"]), [])
            if len(matches) != 1:
                window_failed = True
                if not matches:
                    missing_delayed_rows += 1
                else:
                    duplicate_delayed_rows += 1
                continue
            delayed = matches[0]
            source = v19.evaluate_delayed_source(parent, delayed)
            if source["available"] is not True:
                window_failed = True
                source_failures.update(str(item) for item in source["failures"])
                if len(source_failure_examples) < 7:
                    source_failure_examples.append({
                        "parent_id": int(parent["id"]),
                        "delayed_id": int(delayed["id"]),
                        "asset": str(parent.get("asset") or "").upper(),
                        "failures": list(source["failures"]),
                        "evidence": dict(source["evidence"]),
                    })
            pairs.append((parent, delayed))
        if not window_failed and len(pairs) == 7:
            matched_windows[close] = pairs

    candidate_failures: Counter[str] = Counter()
    candidate_by_asset: Counter[str] = Counter()
    candidates = []
    controls = []
    evidence = []
    for close, pairs in sorted(matched_windows.items()):
        for parent, delayed in pairs:
            if str(parent.get("asset") or "").upper() == "BTC":
                continue
            result = v19.evaluate_pair(parent, delayed)
            evidence.append(result["evidence"])
            parent_control = v18.evaluate_row(parent)
            if parent_control["eligible"]:
                controls.append(int(parent["id"]))
            candidate_failures.update(str(item) for item in result["failures"])
            if result["eligible"]:
                asset = str(parent["asset"]).upper()
                candidate_by_asset[asset] += 1
                candidates.append({
                    "parent_id": int(parent["id"]),
                    "delayed_id": int(delayed["id"]),
                    "asset": asset,
                    "close_time": float(close),
                    "side": str(result["decision"]),
                    "feature_evidence_sha256": result[
                        "feature_evidence_sha256"
                    ],
                })
    candidates.sort(key=lambda item: (
        item["close_time"], item["asset"], item["parent_id"], item["delayed_id"],
    ))

    def maximum(key: str) -> float | None:
        values = [
            float(item[key]) for item in evidence if item.get(key) is not None
        ]
        return max(values) if values else None

    complete_count = len(matched_windows)
    eligible = len(candidates)
    ready = complete_count >= 150 and eligible >= 30
    return {
        "readiness_version": "q15-rti-v19-outcome-blind-readiness-v1",
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "cohort": "NON_BTC_TRANSFER",
        "prospective_after_close_time": identity.PROSPECTIVE_AFTER_CLOSE_TIME,
        "first_eligible_close_time": identity.FIRST_ELIGIBLE_CLOSE_TIME,
        "matched_parent_complete_close_windows": complete_count,
        "minimum_complete_close_windows": 150,
        "complete_close_windows_remaining": max(0, 150 - complete_count),
        "eligible_picks": eligible,
        "first_review_picks_required": 30,
        "eligible_picks_remaining": max(0, 30 - eligible),
        "eligible_pick_close_windows": len({item["close_time"] for item in candidates}),
        "eligible_pick_ids_sha256": _canonical_sha256(tuple(
            sorted((item["parent_id"], item["delayed_id"]) for item in candidates)
        )),
        "eligible_feature_evidence_sha256": _canonical_sha256(candidates),
        "matched_v18_parent_control_picks": len(controls),
        "matched_v18_parent_control_ids_sha256": _canonical_sha256(
            tuple(sorted(controls))
        ),
        "candidate_picks_by_asset": dict(sorted(candidate_by_asset.items())),
        "candidate_failure_counts": dict(sorted(candidate_failures.items())),
        "source_health": {
            "all_seven_complete_parent_windows": len(parent_times),
            "all_seven_complete_parent_and_delayed_windows": complete_count,
            "raw_delayed_rows": len(delayed_rows),
            "linked_delayed_parent_ids": len(delayed_by_parent),
            "missing_delayed_rows": missing_delayed_rows,
            "duplicate_delayed_rows": duplicate_delayed_rows,
            "delayed_source_failure_counts": dict(sorted(source_failures.items())),
            "delayed_source_failure_examples": source_failure_examples,
            "maximum_capture_gap_from_parent_seconds": maximum(
                "capture_gap_from_parent_seconds"
            ),
            "maximum_delayed_timing_offset_seconds": maximum(
                "timing_offset_seconds"
            ),
            "maximum_delayed_evaluation_delay_seconds": maximum(
                "evaluation_delay_seconds"
            ),
            "maximum_delayed_path_receive_age_seconds": maximum(
                "path_max_receive_age_seconds"
            ),
            "maximum_delayed_path_decision_age_seconds": maximum(
                "path_decision_age_seconds"
            ),
            "maximum_delayed_quote_age_seconds": maximum("quote_age_seconds"),
        },
        "status": (
            "READY_FOR_MANUAL_OUTCOME_BLIND_PROSPECTIVE_SEAL"
            if ready else "COLLECTING_PROSPECTIVE_FEATURES_NO_OUTCOMES"
        ),
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-db", default=str(DEFAULT_DB))
    args = parser.parse_args()
    db_path = Path(args.strategy_db)
    print(json.dumps(build_readiness(
        load_feature_rows_after(db_path, identity.PROSPECTIVE_AFTER_CLOSE_TIME),
        load_delayed_feature_rows_after(
            db_path, identity.PROSPECTIVE_AFTER_CLOSE_TIME,
        ),
    ), indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
