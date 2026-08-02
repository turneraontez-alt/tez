"""Outcome-blind readiness for the frozen V22 common-source challenger."""
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

from q15_upgrade.strategy_bots import rti_microstructure_v22 as v22  # noqa: E402
from q15_upgrade.strategy_bots import rti_microstructure_v21 as v21_source  # noqa: E402
from q15_upgrade.strategy_bots import rti_microstructure_v22_identity as identity  # noqa: E402
from q15_upgrade.strategy_bots import rti_microstructure_v22_top_book_features as features  # noqa: E402
from q15_upgrade.strategy_bots import rti_spot_rest_top_book_reservoir_identity as rest_identity  # noqa: E402
from tools.q15_rti_microstructure_freeze import load_feature_rows_after  # noqa: E402
from tools.q15_rti_spot_rest_top_book_readiness import (  # noqa: E402
    _quality_failures as rest_quality_failures,
    load_rows as load_rest_rows,
)
from tools.q15_rti_v17_development_seal import DEFAULT_DB as STRATEGY_DB  # noqa: E402
from tools.q15_rti_v18_prospective_seal import _complete_windows  # noqa: E402
from tools.q15_rti_v19_readiness import _linked_parent_id  # noqa: E402
from tools.q15_rti_v21_readiness import load_trajectory_feature_rows_after  # noqa: E402


ASSETS = frozenset(rest_identity.SOURCE_IDENTITIES)


def _sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode()).hexdigest()


def collect_feature_windows(
    parent_rows: Sequence[Mapping[str, Any]],
    trajectory_rows: Sequence[Mapping[str, Any]],
    rest_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Counter[str]]:
    """Reconstruct every complete V22 window without reading an outcome."""
    v22.load_protocol()
    v22.load_evaluator_contract()
    parents = {
        float(close): rows for close, rows in _complete_windows(parent_rows).items()
        if float(close) >= identity.FIRST_ELIGIBLE_COMMON_CLOSE_TIME
    }
    trajectory: dict[int, dict[str, list[Mapping[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in trajectory_rows:
        try:
            close = float(row.get("close_time") or 0.0)
        except (TypeError, ValueError):
            continue
        interval = str(row.get("interval") or "").upper()
        if close >= identity.FIRST_ELIGIBLE_COMMON_CLOSE_TIME and interval in {
            "12M30S", "12M",
        }:
            trajectory[_linked_parent_id(row)][interval].append(row)
    rest_by_close_asset: dict[
        float, dict[str, list[Mapping[str, Any]]]
    ] = defaultdict(lambda: defaultdict(list))
    rest_failures: Counter[str] = Counter()
    for row in rest_rows:
        try:
            close = float(row.get("close_time"))
        except (TypeError, ValueError):
            continue
        if close < identity.FIRST_ELIGIBLE_COMMON_CLOSE_TIME:
            continue
        failures = rest_quality_failures(row)
        if failures:
            rest_failures.update(failures)
            continue
        rest_by_close_asset[close][str(row.get("asset") or "").upper()].append(row)

    closes = sorted(set(parents) | set(rest_by_close_asset))
    complete = []
    excluded = []
    for close in closes:
        window_failures: Counter[str] = Counter()
        failure_examples = []
        parent_window = parents.get(close, [])
        if (
            len(parent_window) != 7
            or {str(row.get("asset") or "").upper() for row in parent_window}
            != ASSETS
        ):
            excluded.append({
                "close_time": close,
                "failure_counts": {"PARENT_COMMON_WINDOW_INCOMPLETE": 1},
                "failure_examples": [],
            })
            continue
        window_rows = []
        window_v21 = 0
        window_executable = 0
        for parent in sorted(
            parent_window, key=lambda row: str(row.get("asset") or ""),
        ):
            asset = str(parent.get("asset") or "").upper()
            parent_id = int(parent["id"])
            intermediate_matches = trajectory.get(parent_id, {}).get(
                "12M30S", [],
            )
            delayed_matches = trajectory.get(parent_id, {}).get("12M", [])
            books = rest_by_close_asset.get(close, {}).get(asset, [])
            if (
                len(intermediate_matches) != 1 or len(delayed_matches) != 1
                or len(books) != 4
            ):
                window_failures["V22_TRIPLET_OR_REST_GEOMETRY_INVALID"] += 1
                continue
            try:
                result = features.build_features(
                    parent, intermediate_matches[0], delayed_matches[0], books,
                )
            except ValueError as exc:
                window_failures[str(exc)] += 1
                if len(failure_examples) < 7:
                    failure_examples.append({
                        "close_time": close,
                        "asset": asset,
                        "failure": str(exc),
                    })
                continue
            matched = v21_source.evaluate_triplet(
                parent, intermediate_matches[0], delayed_matches[0],
            )
            matched_eligible = matched.get("eligible_for_v21_feature_credit") is True
            result = {
                **result,
                "matched_frozen_v21_eligible": matched_eligible,
                "matched_frozen_v21_source_feature_evidence_sha256": matched.get(
                    "source_feature_evidence_sha256"
                ),
            }
            window_rows.append(result)
            if result.get("execution_supported") is True:
                window_executable += 1
            if matched_eligible:
                window_v21 += 1
        if len(window_rows) == 7 and {row["asset"] for row in window_rows} == ASSETS:
            complete.append({
                "close_time": close,
                "rows": window_rows,
                "matched_frozen_v21_rows": window_v21,
                "row_level_executable_rows": window_executable,
            })
        else:
            window_failures["V22_COMPLETE_COMMON_CLOSE_REQUIRED"] += 1
            excluded.append({
                "close_time": close,
                "failure_counts": dict(sorted(window_failures.items())),
                "failure_examples": failure_examples,
            })
    return complete, excluded, rest_failures


def build_readiness(
    parent_rows: Sequence[Mapping[str, Any]],
    trajectory_rows: Sequence[Mapping[str, Any]],
    rest_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    complete, excluded, rest_failures = collect_feature_windows(
        parent_rows, trajectory_rows, rest_rows,
    )
    complete_rows = [row for window in complete for row in window["rows"]]
    complete_closes = [float(window["close_time"]) for window in complete]
    matched_v21_rows = sum(
        int(window["matched_frozen_v21_rows"]) for window in complete
    )
    executable_rows = sum(
        int(window["row_level_executable_rows"]) for window in complete
    )
    common_failures: Counter[str] = Counter()
    failure_examples = []
    for window in excluded:
        common_failures.update(dict(window["failure_counts"]))
        failure_examples.extend(list(window["failure_examples"]))
    failure_examples = failure_examples[:7]

    credited = [{
        "parent_id": row["parent_id"],
        "asset": row["asset"],
        "ticker": row["ticker"],
        "close_time": row["close_time"],
        "feature_evidence_sha256": row["feature_evidence_sha256"],
        "rest_evidence_sha256_by_stage": row["rest_evidence_sha256_by_stage"],
    } for row in complete_rows]
    return {
        **v22.status(),
        "v22_feature_complete_common_close_windows": len(complete_closes),
        "complete_common_close_windows_remaining": max(
            0, identity.MINIMUM_COMPLETE_COMMON_CLOSE_WINDOWS - len(complete_closes)
        ),
        "feature_rows": len(complete_rows),
        "row_level_executable_feature_rows": executable_rows,
        "rows_by_asset": dict(sorted(Counter(
            str(row["asset"]) for row in complete_rows
        ).items())),
        "matched_frozen_v21_feature_rows_diagnostic_only": matched_v21_rows,
        "matched_frozen_v21_missing_rows_diagnostic_only": (
            len(complete_rows) - matched_v21_rows
        ),
        "rest_quality_failure_counts": dict(sorted(rest_failures.items())),
        "common_feature_failure_counts": dict(sorted(common_failures.items())),
        "common_feature_failure_examples": failure_examples,
        "eligible_feature_evidence_sha256": _sha256(credited),
        "database_schema_outcome_free": True,
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
        "status": (
            "READY_TO_FREEZE_V22_HISTORICAL_SEAL_NO_LABELS_OPENED"
            if len(complete_closes) >= identity.MINIMUM_COMPLETE_COMMON_CLOSE_WINDOWS
            else "COLLECTING_V22_COMMON_SOURCE_WINDOWS_NO_OUTCOMES"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-db", default=str(STRATEGY_DB))
    parser.add_argument("--rest-db", default=str(
        ROOT / rest_identity.DATABASE_RELATIVE_PATH
    ))
    args = parser.parse_args()
    strategy_db = Path(args.strategy_db)
    parent_rows = load_feature_rows_after(
        strategy_db, rest_identity.PROSPECTIVE_AFTER_CLOSE_TIME,
    )
    trajectory_rows = load_trajectory_feature_rows_after(
        strategy_db, rest_identity.PROSPECTIVE_AFTER_CLOSE_TIME,
    )
    report = build_readiness(
        parent_rows, trajectory_rows, load_rest_rows(Path(args.rest_db)),
    )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
