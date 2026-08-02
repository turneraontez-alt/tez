"""Outcome-blind readiness counter for the frozen V18 selective study."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots import rti_microstructure_v18 as v18
from q15_upgrade.strategy_bots import rti_microstructure_v18_identity as identity
from tools.q15_rti_microstructure_freeze import load_feature_rows_after
from tools.q15_rti_v17_development_seal import DEFAULT_DB
from tools.q15_rti_v18_prospective_seal import _complete_windows


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def build_readiness(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    complete = _complete_windows(rows)
    future_times = tuple(
        close_time for close_time in sorted(complete)
        if close_time > identity.PROSPECTIVE_AFTER_CLOSE_TIME
    )
    decisions = []
    controls = []
    candidate_failures: Counter[str] = Counter()
    candidate_by_asset: Counter[str] = Counter()
    source_evidence = []
    for close_time in future_times:
        for row in complete[close_time]:
            source_evidence.append(v18.evaluate_source_row(row)["evidence"])
            if str(row.get("asset") or "").upper() == "BTC":
                continue
            control = v18.evaluate_strict_control_row(row)
            result = v18.evaluate_row(row)
            if control["eligible"]:
                controls.append(int(row["id"]))
            candidate_failures.update(str(value) for value in result["failures"])
            if result["eligible"]:
                candidate_by_asset[str(row["asset"]).upper()] += 1
                decisions.append({
                    "id": int(row["id"]),
                    "asset": str(row["asset"]).upper(),
                    "close_time": float(close_time),
                    "side": str(result["decision"]),
                    "feature_evidence_sha256": result["feature_evidence_sha256"],
                })
    decisions.sort(key=lambda item: (item["close_time"], item["asset"], item["id"]))
    eligible = len(decisions)
    complete_count = len(future_times)
    ready = eligible >= 30 and complete_count >= 150
    raw_by_close: dict[float, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        try:
            close_time = float(row["close_time"])
        except (KeyError, TypeError, ValueError):
            continue
        if close_time > identity.PROSPECTIVE_AFTER_CLOSE_TIME:
            raw_by_close[close_time].append(row)
    all_assets = v18.ALL_ASSETS
    geometry_complete = {
        close_time for close_time, window_rows in raw_by_close.items()
        if len(window_rows) == 7
        and {str(row.get("asset") or "").upper() for row in window_rows}
        == all_assets
    }
    source_failure_counts: Counter[str] = Counter()
    source_failure_assets: Counter[str] = Counter()
    for close_time in sorted(geometry_complete - set(future_times)):
        for row in raw_by_close[close_time]:
            source = v18.evaluate_source_row(row)
            if source["available"] is not True:
                source_failure_counts.update(
                    str(value) for value in source["failures"]
                )
                source_failure_assets[str(row.get("asset") or "").upper()] += 1

    def maximum(key: str) -> float | None:
        values = [
            float(item[key]) for item in source_evidence
            if item.get(key) is not None
        ]
        return max(values) if values else None

    return {
        "readiness_version": "q15-rti-v18-outcome-blind-readiness-v2",
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "cohort": "NON_BTC_TRANSFER",
        "prospective_after_close_time": identity.PROSPECTIVE_AFTER_CLOSE_TIME,
        "first_eligible_close_time": identity.FIRST_ELIGIBLE_CLOSE_TIME,
        "successor_audit_complete_close_windows": complete_count,
        "minimum_complete_close_windows": 150,
        "complete_close_windows_remaining": max(0, 150 - complete_count),
        "eligible_picks": eligible,
        "first_review_picks_required": 30,
        "eligible_picks_remaining": max(0, 30 - eligible),
        "eligible_pick_close_windows": len({item["close_time"] for item in decisions}),
        "eligible_pick_ids_sha256": _canonical_sha256(
            tuple(sorted(item["id"] for item in decisions))
        ),
        "eligible_feature_evidence_sha256": _canonical_sha256(decisions),
        "strict_control_picks": len(controls),
        "strict_control_pick_ids_sha256": _canonical_sha256(
            tuple(sorted(controls))
        ),
        "candidate_qualification_rate_per_complete_window": (
            eligible / complete_count if complete_count else 0.0
        ),
        "candidate_picks_by_asset": dict(sorted(candidate_by_asset.items())),
        "candidate_failure_counts": dict(sorted(candidate_failures.items())),
        "source_health": {
            "raw_close_windows": len(raw_by_close),
            "all_seven_geometry_close_windows": len(geometry_complete),
            "complete_source_quality_close_windows": complete_count,
            "partial_geometry_close_windows": len(
                set(raw_by_close) - geometry_complete
            ),
            "source_quality_incomplete_close_windows": len(
                geometry_complete - set(future_times)
            ),
            "source_failure_counts": dict(sorted(source_failure_counts.items())),
            "source_failure_assets": dict(sorted(source_failure_assets.items())),
            "latest_complete_close_time": (
                max(future_times) if future_times else None
            ),
            "maximum_exact_timing_offset_seconds": maximum(
                "exact_timing_offset_seconds"
            ),
            "maximum_evaluation_delay_seconds": maximum(
                "evaluation_delay_seconds"
            ),
            "maximum_path_receive_age_seconds": maximum(
                "path_max_receive_age_seconds"
            ),
            "maximum_path_decision_age_seconds": maximum(
                "path_decision_age_seconds"
            ),
            "maximum_quote_age_seconds": maximum("quote_age_seconds"),
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
    print(json.dumps(
        build_readiness(load_feature_rows_after(
            Path(args.strategy_db), identity.PROSPECTIVE_AFTER_CLOSE_TIME,
        )),
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ))


if __name__ == "__main__":
    main()
