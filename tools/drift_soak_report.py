#!/usr/bin/env python3
"""CLI for the read-only Drift 24-48 hour operational soak audit."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.drift_soak import (  # noqa: E402
    DEFAULT_ASSETS,
    SoakAuditError,
    build_soak_report,
    format_soak_report,
    parse_epoch,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Audit Drift's paper-only operational soak without mutating any database. "
            "Before 24h status is COLLECTING; at/after 24h it is PASS or FAIL."
        )
    )
    parser.add_argument(
        "--start",
        "--start-epoch",
        dest="start",
        required=True,
        help="soak start as Unix epoch or timezone-qualified ISO-8601",
    )
    parser.add_argument("--now", help="audit end epoch/ISO (default: current time)")
    parser.add_argument(
        "--interval-db",
        default=os.environ.get(
            "Q15_INTERVAL_RESEARCH_DB", "data/q15_interval_research_v1.sqlite3"
        ),
    )
    parser.add_argument(
        "--strategy-db",
        default=os.environ.get("Q15_STRATEGY_BOTS_DB", "data/q15_strategy_bots_v3.sqlite3"),
    )
    parser.add_argument(
        "--outbox-db",
        default=os.environ.get(
            "Q15_V3_DRIFT_OUTBOX_DB", "data/q15_drift_telegram_outbox.sqlite3"
        ),
    )
    parser.add_argument(
        "--cycle-log",
        default=str(
            Path(os.environ.get("Q15_LOCAL_LOG_DIR", "work/local-run/logs"))
            / "app.err.log"
        ),
    )
    parser.add_argument(
        "--cycle-max-seconds",
        type=float,
        help="trusted max-cycle metric; use when the current log does not cover the soak start",
    )
    parser.add_argument("--model-version", default="interval-research-v1")
    parser.add_argument(
        "--expected-assets",
        default=",".join(DEFAULT_ASSETS),
        help="comma-separated asset set (default: seven deployed assets)",
    )
    parser.add_argument("--settlement-grace-seconds", type=float, default=900.0)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        start = parse_epoch(args.start)
        now = time.time() if args.now is None else parse_epoch(args.now)
        assets = tuple(
            asset.strip().upper() for asset in args.expected_assets.split(",") if asset.strip()
        )
        report = build_soak_report(
            start_epoch=start,
            now_epoch=now,
            interval_db_path=args.interval_db,
            strategy_db_path=args.strategy_db,
            outbox_db_path=args.outbox_db,
            cycle_log_path=args.cycle_log,
            model_version=args.model_version,
            expected_assets=assets,
            provided_cycle_max_seconds=args.cycle_max_seconds,
            settlement_grace_seconds=args.settlement_grace_seconds,
        )
    except SoakAuditError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_soak_report(report))
    return 1 if report["status"] == "FAIL" else 0

if __name__ == "__main__":
    raise SystemExit(main())
