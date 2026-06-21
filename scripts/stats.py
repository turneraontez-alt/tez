#!/usr/bin/env python3
"""All-stats dump for the V9.5 monitor — run this ON THE REPL (where data/ lives).

READ-ONLY. Prints, in one place:

  * ledger status (availability, model/feature-schema version, row counts);
  * the OFFICIAL win/loss record (15M/10M/7M, YES/NO, entry, manipulation) —
    graded only against settled outcomes;
  * the SHADOW FACTOR LAB — per-factor reliability attribution for every official
    prediction, with the promotion gate (reported, never applied).

It never writes production state, never changes a live prediction or entry, and
never touches the frozen champion or a real order.

Usage (on the Repl):
    python3 scripts/stats.py
    python3 scripts/stats.py --ledger data/q15_v95_ledger_v1.sqlite3
    python3 scripts/stats.py --checkpoint 10M --detail
    python3 scripts/stats.py --min-samples 30 --reliability 0.55
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Mapping

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from q15_upgrade import factor_lab


def _pct(value: Any) -> str:
    try:
        if value is None:
            return "—"
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return "—"


def _wl(bucket: Mapping[str, Any] | None) -> str:
    bucket = bucket or {}
    right = int(bucket.get("right") or 0)
    wrong = int(bucket.get("wrong") or 0)
    acc = bucket.get("accuracy")
    tail = " (low n)" if bucket.get("low_n") else ""
    return f"{right}W-{wrong}L {_pct(acc)}{tail}"


def _print_official(board: Mapping[str, Any]) -> None:
    print("OFFICIAL RECORD (sent predictions graded vs settlement)")
    if not board.get("available"):
        print("  unavailable (no ledger / no settled official predictions yet)")
        return
    for interval in ("15M", "10M", "7M"):
        grp = board.get(interval) or {}
        print(f"  {interval:<4} Y {_wl(grp.get('yes'))}   N {_wl(grp.get('no'))}   "
              f"T {_wl(grp.get('total'))}")
    print(f"  ENTRY  Y {_wl((board.get('entry') or {}).get('yes'))}   "
          f"N {_wl((board.get('entry') or {}).get('no'))}   "
          f"T {_wl((board.get('entry') or {}).get('total'))}")
    print(f"  MANIP  {_wl(board.get('manipulation'))}")


def _print_status(status: Mapping[str, Any]) -> None:
    print("LEDGER STATUS")
    if not status.get("available"):
        print("  unavailable")
        return
    print(f"  model_version={status.get('model_version')} "
          f"feature_schema={status.get('feature_schema_version')} "
          f"frozen={status.get('production_weights_frozen')} "
          f"auto_promotion={status.get('automatic_promotion')}")
    print(f"  predictions={status.get('unique_predictions')} "
          f"resolved={status.get('unique_resolved')} "
          f"(15M={status.get('fifteen_minute_predictions')} "
          f"10M={status.get('ten_minute_predictions')} "
          f"7M={status.get('seven_minute_predictions')})")
    if status.get("dropped_feature_rows"):
        print(f"  dropped_feature_rows={status.get('dropped_feature_rows')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="All V9.5 stats (read-only).")
    parser.add_argument("--ledger", help="path to the ledger SQLite file "
                        "(default: Q15_V95_LEDGER_DB / data/ default)")
    parser.add_argument("--checkpoint", choices=["15M", "10M", "7M"],
                        help="restrict the factor lab to one interval")
    parser.add_argument("--detail", action="store_true",
                        help="show per-interval reliability + why a factor is blocked")
    parser.add_argument("--top", type=int, default=12, help="factors to list (default 12)")
    parser.add_argument("--min-samples", type=int, default=factor_lab.DEFAULT_MIN_SAMPLES)
    parser.add_argument("--reliability", type=float, default=factor_lab.DEFAULT_RELIABILITY_THRESHOLD)
    parser.add_argument("--deadzone", type=float, default=factor_lab.DEFAULT_DEADZONE)
    args = parser.parse_args(argv)

    if args.ledger:
        os.environ["Q15_V95_LEDGER_DB"] = args.ledger

    # Imported AFTER the env override so the ledger opens the requested file.
    from q15_upgrade.ledger_v95 import V95Ledger

    ledger = V95Ledger()
    print("=" * 64)
    _print_status(ledger.status())
    print("-" * 64)
    _print_official(ledger.official_scoreboard())
    print("-" * 64)
    rows = ledger.resolved_factor_rows(args.checkpoint)
    report = factor_lab.analyze(
        rows, deadzone=args.deadzone, min_samples=args.min_samples,
        reliability_threshold=args.reliability,
    )
    scope = f" [{args.checkpoint}]" if args.checkpoint else ""
    print(f"FACTOR LAB{scope}")
    for line in factor_lab.format_report(report, top=args.top, detail=args.detail):
        print(line)
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
