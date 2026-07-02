from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market_cache import MarketResultCache
from q15_upgrade.kalshi_rest import KalshiClient
from q15_upgrade.ledger_v95 import V95Ledger


def _parse_ts(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        parsed = float(value)
        while parsed > 10_000_000_000:
            parsed /= 1000.0
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _official_result(market: Any) -> str | None:
    if not isinstance(market, Mapping):
        return None
    result = str(market.get("result") or "").upper()
    return result if result in {"YES", "NO"} else None


def backfill_resolutions(
    *,
    ledger: V95Ledger,
    get_market: Callable[[str], Any],
    now: float | None = None,
    dry_run: bool = False,
    retry_parked: bool = False,
    limit: int | None = None,
    emit: Callable[[str], None] = print,
) -> dict[str, Any]:
    current = time.time() if now is None else float(now)
    requeued = ledger.requeue_parked_reconcile_tickers() if retry_parked else 0
    tickers = ledger.unresolved_past_close_tickers(
        now=current,
        include_parked=True,
        limit=limit,
    )
    resolved_rows = 0
    determined_tickers = 0
    shadow_updates = 0
    errors: list[dict[str, str]] = []
    undetermined: list[dict[str, Any]] = []

    emit(
        f"Backfill starting: {len(tickers)} unresolved past-close ticker(s)"
        + (f"; requeued {requeued} parked ticker(s)" if retry_parked else "")
        + ("; dry-run" if dry_run else "")
    )
    for index, row in enumerate(tickers, start=1):
        ticker = str(row["ticker"])
        try:
            market = get_market(ticker)
        except Exception as exc:  # external API boundary; keep going.
            errors.append({"ticker": ticker, "error": f"{type(exc).__name__}: {exc}"})
            emit(f"[{index}/{len(tickers)}] {ticker}: fetch error {type(exc).__name__}")
            continue
        result = _official_result(market)
        if result is None:
            sample = V95Ledger._undetermined_market_sample(ticker, market if isinstance(market, Mapping) else {})
            undetermined.append(sample)
            emit(f"[{index}/{len(tickers)}] {ticker}: undetermined")
            continue
        outcome_time = (
            _parse_ts(market.get("close_time") if isinstance(market, Mapping) else None)
            or _parse_ts(market.get("settled_at") if isinstance(market, Mapping) else None)
            or current
        )
        pending_rows = ledger.unresolved_prediction_count(ticker)
        determined_tickers += 1
        if dry_run:
            resolved_rows += pending_rows
            emit(f"[{index}/{len(tickers)}] {ticker}: would resolve {pending_rows} row(s) as {result}")
            continue
        outcome = ledger.resolve_ticker(ticker, result, outcome_time)
        resolved = int(outcome.get("resolved") or 0)
        updates = int(outcome.get("updates_applied") or 0)
        resolved_rows += resolved
        shadow_updates += updates
        emit(f"[{index}/{len(tickers)}] {ticker}: resolved {resolved} row(s) as {result}; shadow_updates={updates}")

    summary = {
        "dry_run": dry_run,
        "tickers_examined": len(tickers),
        "determined_tickers": determined_tickers,
        "new_predictions_resolved": resolved_rows,
        "shadow_predictions_resolved": shadow_updates,
        "requeued_parked": requeued,
        "errors": errors,
        "remaining_undetermined": undetermined,
    }
    emit("Backfill summary:")
    emit(json.dumps(summary, indent=2, sort_keys=True))
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill unresolved V9.5 prediction outcomes from Kalshi.")
    parser.add_argument("--db", help="Path to the V95 ledger SQLite DB. Defaults to Q15_V95_LEDGER_DB/data path.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch outcomes and print what would resolve without writing.")
    parser.add_argument("--retry-parked", action="store_true", help="Requeue parked reconcile tickers before walking backlog.")
    parser.add_argument("--limit", type=int, default=None, help="Optional safety limit for manual test runs.")
    args = parser.parse_args(argv)

    ledger = V95Ledger(args.db) if args.db else V95Ledger()
    cache = MarketResultCache(KalshiClient())
    summary = backfill_resolutions(
        ledger=ledger,
        get_market=cache.get_market,
        dry_run=bool(args.dry_run),
        retry_parked=bool(args.retry_parked),
        limit=args.limit,
    )
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
