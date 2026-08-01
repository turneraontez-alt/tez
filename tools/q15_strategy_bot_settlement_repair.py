"""Idempotently repair RTI side-ledger settlements from the interval ledger.

This tool never derives or guesses an outcome.  It considers only explicit
pending RTI tickers and accepts a source label only when all persisted interval
rows for that exact ticker agree on YES or NO.  It has no trading or alerting
surface.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.interval_research.ledger import IntervalResearchLedger
from q15_upgrade.strategy_bots.ledger import StrategyBotLedger


def repair(
    *,
    strategy_db: Path,
    interval_db: Path,
    now: float | None = None,
    limit: int = 500,
) -> dict[str, object]:
    current = time.time() if now is None else float(now)
    strategy = StrategyBotLedger(strategy_db)
    interval = IntervalResearchLedger(str(interval_db))
    try:
        pending = strategy.unresolved_rti_tickers(now=current, limit=limit)
        settled = interval.resolved_results_for_tickers(pending)
        graded = 0
        repaired_tickers = []
        for row in settled:
            count = strategy.resolve_ticker(
                ticker=str(row["ticker"]),
                official_result=str(row["official_result"]),
                now=row.get("resolved_at") or current,
            )
            if count:
                graded += count
                repaired_tickers.append(str(row["ticker"]))
        remaining = strategy.unresolved_rti_tickers(now=current, limit=limit)
        return {
            "repair_version": "q15-rti-contract-settlement-repair-v1",
            "paper_only": True,
            "official_results_guessed": False,
            "unanimous_source_result_required": True,
            "pending_tickers_before": len(pending),
            "authoritative_tickers_found": len(settled),
            "repaired_tickers": len(repaired_tickers),
            "rows_graded": graded,
            "pending_tickers_after": len(remaining),
        }
    finally:
        strategy.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--strategy-db", default="data/q15_strategy_bots_v3.sqlite3"
    )
    parser.add_argument(
        "--interval-db", default="data/q15_interval_research_v1.sqlite3"
    )
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Required mutation acknowledgement; operation is idempotent.",
    )
    args = parser.parse_args()
    if not args.apply:
        raise SystemExit("Refusing to mutate without --apply")
    report = repair(
        strategy_db=Path(args.strategy_db),
        interval_db=Path(args.interval_db),
        limit=args.limit,
    )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
