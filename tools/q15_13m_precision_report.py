"""Read-only verifier for the selective 13M precision policy."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.interval_research.precision13 import EXPECTED_ASSETS, evaluate_slate


def _wilson(correct: int, total: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    p = correct / total
    den = 1.0 + z * z / total
    center = (p + z * z / (2.0 * total)) / den
    radius = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * total)) / total) / den
    return max(0.0, center - radius), min(1.0, center + radius)


def _metrics(decisions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    emitted = [row for row in decisions if row["decision"] in {"YES", "NO"}]
    correct = sum(row["decision"] == row["official_result"] for row in emitted)
    low, high = _wilson(correct, len(emitted))
    return {
        "eligible_rows": len(decisions),
        "emitted": len(emitted),
        "abstained": len(decisions) - len(emitted),
        "coverage": round(len(emitted) / len(decisions), 6) if decisions else None,
        "correct": correct,
        "accuracy": round(correct / len(emitted), 6) if emitted else None,
        "wilson_95_low": round(low, 6) if low is not None else None,
        "wilson_95_high": round(high, 6) if high is not None else None,
        "yes_emitted": sum(row["decision"] == "YES" for row in emitted),
        "no_emitted": sum(row["decision"] == "NO" for row in emitted),
    }


def build_report(db_path: str) -> dict[str, Any]:
    path = Path(db_path)
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    try:
        rows_13m = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM interval_captures WHERE interval='13M' "
                "AND official_result IN ('YES','NO') "
                "AND predicted_side IN ('YES','NO') ORDER BY window_key, asset"
            )
        ]
        rows_15m = {
            str(row["ticker"]): dict(row)
            for row in connection.execute(
                "SELECT ticker, calibrated_yes_probability FROM interval_captures "
                "WHERE interval='15M' AND calibrated_yes_probability IS NOT NULL"
            )
        }
    finally:
        connection.close()

    by_window: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows_13m:
        by_window[int(row["window_key"])].append(row)

    windows = sorted(
        window_key
        for window_key, rows in by_window.items()
        if len(rows) == len(EXPECTED_ASSETS)
        and {str(row.get("asset") or "").upper() for row in rows} == EXPECTED_ASSETS
    )
    decisions: list[dict[str, Any]] = []
    for window_key in windows:
        source_rows = by_window[window_key]
        outcomes = {str(row["ticker"]): str(row["official_result"]) for row in source_rows}
        for decision in evaluate_slate(source_rows, rows_15m):
            decisions.append(
                {
                    **decision,
                    "window_key": window_key,
                    "official_result": outcomes.get(str(decision.get("ticker") or "")),
                }
            )

    train_end = int(len(windows) * 0.60)
    validation_end = int(len(windows) * 0.80)
    split_windows = {
        "train_older_60pct": set(windows[:train_end]),
        "validation_next_20pct": set(windows[train_end:validation_end]),
        "test_newest_20pct": set(windows[validation_end:]),
        "latest_50_windows": set(windows[-50:]),
    }
    splits = {
        name: _metrics([row for row in decisions if row["window_key"] in selected])
        for name, selected in split_windows.items()
    }
    test = splits["test_newest_20pct"]
    target_pass = (
        all(
            splits[name]["accuracy"] is not None and splits[name]["accuracy"] >= 0.85
            for name in (
                "train_older_60pct",
                "validation_next_20pct",
                "test_newest_20pct",
            )
        )
        and test["emitted"] >= 100
        and splits["latest_50_windows"]["accuracy"] is not None
        and splits["latest_50_windows"]["accuracy"] >= 0.85
        and splits["latest_50_windows"]["emitted"] >= 30
    )
    return {
        "policy": "q15-13m-selective-precision-v2",
        "read_only": True,
        "target_accuracy": 0.85,
        "target_status": "PASS" if target_pass else "FAIL",
        "complete_windows": len(windows),
        "window_range": [windows[0], windows[-1]] if windows else None,
        "overall": _metrics(decisions),
        "splits": splits,
        "note": (
            "Accuracy applies only to emitted 13M calls. All other rows abstain "
            "and wait for a later checkpoint. The policy records prospective "
            "shadow decisions only; it cannot alert, trade, size, or alter the champion."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/q15_interval_research_v1.sqlite3")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = build_report(args.db)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"13M precision target: {report['target_status']}")
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["target_status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
