"""Read-only coverage and performance report for Q15 MarketLead."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.precision import estimated_fee_cents


def _wilson(correct: int, total: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    probability = correct / total
    denominator = 1.0 + z * z / total
    center = (probability + z * z / (2.0 * total)) / denominator
    radius = z * math.sqrt(
        (probability * (1.0 - probability) + z * z / (4.0 * total)) / total
    ) / denominator
    return max(0.0, center - radius), min(1.0, center + radius)


def _metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    resolved = [row for row in rows if row.get("official_result") in {"YES", "NO"}]
    correct = sum(int(row.get("correct") or 0) for row in resolved)
    low, high = _wilson(correct, len(resolved))
    net = 0.0
    priced = 0
    for row in resolved:
        ask = row.get("entry_ask_cents")
        if ask is None:
            continue
        ask = float(ask)
        fee = estimated_fee_cents(ask)
        net += (100.0 - ask if row.get("correct") else -ask) - fee
        priced += 1
    return {
        "emitted": len(rows),
        "resolved": len(resolved),
        "correct": correct,
        "accuracy": round(correct / len(resolved), 6) if resolved else None,
        "wilson_95_low": round(low, 6) if low is not None else None,
        "wilson_95_high": round(high, 6) if high is not None else None,
        "average_entry_cents": round(
            sum(float(row["entry_ask_cents"]) for row in resolved if row.get("entry_ask_cents") is not None)
            / priced,
            4,
        ) if priced else None,
        "estimated_fee_adjusted_pnl_cents": round(net, 4),
        "estimated_net_cents_per_trade": round(net / priced, 4) if priced else None,
    }


def build_report(db_path: str) -> dict[str, Any]:
    path = Path(db_path).resolve()
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        rows = [dict(row) for row in connection.execute(
            "SELECT * FROM marketlead_observations ORDER BY window_key,asset"
        )]
    finally:
        connection.close()
    low_price = [
        row for row in rows
        if row.get("entry_ask_cents") is not None and float(row["entry_ask_cents"]) < 70.0
    ]
    ready_low = [row for row in low_price if row.get("evidence_status") == "READY"]
    candidates = [row for row in ready_low if int(row.get("joint_alignment") or 0) == 1]
    windows = sorted({int(row["window_key"]) for row in rows})
    train_end = int(len(windows) * 0.60)
    validation_end = int(len(windows) * 0.80)
    split_windows = {
        "train_older_60pct": set(windows[:train_end]),
        "validation_next_20pct": set(windows[train_end:validation_end]),
        "test_newest_20pct": set(windows[validation_end:]),
    }
    splits = {
        name: _metrics([row for row in candidates if int(row["window_key"]) in selected])
        for name, selected in split_windows.items()
    }
    ready_settled = sum(row.get("official_result") in {"YES", "NO"} for row in ready_low)
    enough_evidence = ready_settled >= 500 and splits["test_newest_20pct"]["resolved"] >= 100
    accuracy_pass = enough_evidence and all(
        split["accuracy"] is not None and split["accuracy"] >= 0.86
        for split in splits.values()
    )
    profit_pass = enough_evidence and all(
        (split["estimated_net_cents_per_trade"] or 0.0) > 0.0
        for split in splits.values()
    )
    target_status = (
        "COLLECTING" if not enough_evidence else "PASS" if accuracy_pass and profit_pass else "FAIL"
    )
    return {
        "system": "Q15 MarketLead",
        "system_version": rows[-1]["system_version"] if rows else "q15-marketlead-v1",
        "read_only": True,
        "target_status": target_status,
        "requirements": {
            "settled_ready_low_price": 500,
            "newest_test_candidates": 100,
            "accuracy_each_split": 0.86,
            "positive_fee_adjusted_expectancy_each_split": True,
        },
        "coverage": {
            "observations": len(rows),
            "windows": len(windows),
            "low_price": len(low_price),
            "ready_low_price": len(ready_low),
            "settled_ready_low_price": ready_settled,
            "joint_alignment_candidates": len(candidates),
            "paper_touches": sum(int(row.get("paper_limit_touched") or 0) for row in rows),
            "complete_markouts": sum(row.get("execution_status") == "COMPLETE" for row in rows),
        },
        "candidate_overall": _metrics(candidates),
        "candidate_splits": splits,
        "accuracy_target_pass": bool(accuracy_pass),
        "profit_target_pass": bool(profit_pass),
        "note": (
            "Joint alignment is a prospective research cohort, not an order rule. "
            "No notification or execution path consumes this report."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/q15_marketlead_v1.sqlite3")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = build_report(args.db)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"Q15 MarketLead: {report['target_status']}")
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
