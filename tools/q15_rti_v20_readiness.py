"""Outcome-blind readiness counter for frozen V20 reversal-hazard features."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots import rti_microstructure_v20 as v20
from q15_upgrade.strategy_bots import rti_microstructure_v20_identity as identity
from tools.q15_rti_microstructure_freeze import load_feature_rows_after
from tools.q15_rti_v17_development_seal import DEFAULT_DB
from tools.q15_rti_v18_prospective_seal import _complete_windows
from tools.q15_rti_v19_readiness import (
    _linked_parent_id,
    load_delayed_feature_rows_after,
)


ASSETS = frozenset({"BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP"})


def _readiness_status(complete_windows: int) -> str:
    """Match the seal's earliest-complete-window rule exactly.

    Earlier incomplete or missing windows remain visible diagnostics, but do
    not poison readiness forever after 150 later, fully valid windows exist.
    """
    return (
        "READY_FOR_MANUAL_V20_FEATURE_SEAL"
        if complete_windows >= identity.MINIMUM_COMPLETE_CLOSE_WINDOWS
        else "COLLECTING_V20_PROSPECTIVE_FEATURES_NO_OUTCOMES"
    )


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_readiness(
    parent_rows: list[Mapping[str, Any]],
    delayed_rows: list[Mapping[str, Any]],
) -> dict[str, Any]:
    v20.load_protocol()
    parents = {
        close_time: rows
        for close_time, rows in _complete_windows(parent_rows).items()
        if close_time > identity.PROSPECTIVE_AFTER_CLOSE_TIME
    }
    delayed_by_parent: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in delayed_rows:
        if float(row.get("close_time") or 0.0) <= identity.PROSPECTIVE_AFTER_CLOSE_TIME:
            continue
        delayed_by_parent[_linked_parent_id(row)].append(row)

    complete_windows = 0
    feature_rows = []
    failure_counts: Counter[str] = Counter()
    failure_examples = []
    missing_pairs = duplicate_pairs = 0
    rows_by_asset: Counter[str] = Counter()
    rows_by_cohort: Counter[str] = Counter()
    for close_time, window in sorted(parents.items()):
        window_rows = []
        window_failed = False
        for parent in window:
            matches = delayed_by_parent.get(int(parent["id"]), [])
            if len(matches) != 1:
                window_failed = True
                if matches:
                    duplicate_pairs += 1
                else:
                    missing_pairs += 1
                continue
            result = v20.evaluate_pair(parent, matches[0])
            if result["eligible_for_v20_feature_credit"] is not True:
                window_failed = True
                failure_counts.update(result["failures"])
                if len(failure_examples) < 7:
                    failure_examples.append({
                        "close_time": close_time,
                        "asset": parent.get("asset"),
                        "parent_id": parent.get("id"),
                        "delayed_id": matches[0].get("id"),
                        "failures": list(result["failures"]),
                    })
                continue
            evidence = dict(result["evidence"])
            window_rows.append(evidence)
        if (
            not window_failed
            and len(window_rows) == 7
            and {str(row.get("asset") or "").upper() for row in window_rows}
            == ASSETS
        ):
            complete_windows += 1
            feature_rows.extend(window_rows)
            rows_by_asset.update(str(row["asset"]) for row in window_rows)
            rows_by_cohort.update(str(row["cohort"]) for row in window_rows)

    evidence_identity = [
        {
            "parent_id": row["parent_id"],
            "delayed_id": row["delayed_id"],
            "asset": row["asset"],
            "cohort": row["cohort"],
            "ticker": row["ticker"],
            "close_time": row["close_time"],
            "feature_evidence_sha256": row["feature_evidence_sha256"],
        }
        for row in feature_rows
    ]
    return {
        "readiness_version": "q15-rti-v20-outcome-blind-readiness-v1",
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
        "feature_count": identity.FEATURE_COUNT,
        "feature_names_sha256": identity.FEATURE_NAMES_SHA256,
        "prospective_after_close_time": identity.PROSPECTIVE_AFTER_CLOSE_TIME,
        "first_eligible_close_time": identity.FIRST_ELIGIBLE_CLOSE_TIME,
        "minimum_complete_close_windows": identity.MINIMUM_COMPLETE_CLOSE_WINDOWS,
        "v20_feature_complete_close_windows": complete_windows,
        "complete_close_windows_remaining": max(
            0, identity.MINIMUM_COMPLETE_CLOSE_WINDOWS - complete_windows
        ),
        "feature_rows": len(feature_rows),
        "rows_by_asset": dict(sorted(rows_by_asset.items())),
        "rows_by_cohort": dict(sorted(rows_by_cohort.items())),
        "missing_parent_delayed_pairs": missing_pairs,
        "duplicate_parent_delayed_pairs": duplicate_pairs,
        "feature_failure_counts": dict(sorted(failure_counts.items())),
        "feature_failure_examples": failure_examples,
        "eligible_feature_evidence_sha256": _canonical_sha256(
            evidence_identity
        ),
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
        "excluded_window_quality_issues_are_diagnostic_only": True,
        "status": _readiness_status(complete_windows),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-db", default=str(DEFAULT_DB))
    args = parser.parse_args()
    db_path = Path(args.strategy_db)
    parents = load_feature_rows_after(
        db_path, identity.PROSPECTIVE_AFTER_CLOSE_TIME,
    )
    delayed = load_delayed_feature_rows_after(
        db_path, identity.PROSPECTIVE_AFTER_CLOSE_TIME,
    )
    print(json.dumps(
        build_readiness(parents, delayed), indent=2, sort_keys=True,
    ))


if __name__ == "__main__":
    main()
