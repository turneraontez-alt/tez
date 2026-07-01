#!/usr/bin/env python3
"""Read-only executor P&L and exit-counterfactual report."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from q15_upgrade.executor.reporting import (  # noqa: E402
    load_executor_orders,
    load_official_results,
    reconstruct_order_pnl,
)


DEFAULT_ORDER_DBS = (
    "data/q15_executor_orders_v1.sqlite3",
    "data/q15_executor_yes_orders_v1.sqlite3",
)
DEFAULT_RESULT_DBS = (
    "data/q15_ultoim_v2_v1.sqlite3",
    "data/q15_high_vol_flip_v1.sqlite3",
    "data/q15_strategy_bots_v3.sqlite3",
    "data/q15_v95_ledger_v1.sqlite3",
)


def _existing(paths: tuple[str, ...]) -> list[str]:
    return [p for p in paths if Path(p).exists()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--orders-db",
        action="append",
        dest="orders_dbs",
        help="Local executor order sqlite DB to read; repeatable.",
    )
    parser.add_argument(
        "--results-db",
        action="append",
        dest="results_dbs",
        help="SQLite DB containing ticker + official_result columns; repeatable.",
    )
    parser.add_argument(
        "--prefer-final",
        action="store_true",
        help="Use reconciled final_* order fields when present instead of immediate-fill fields.",
    )
    parser.add_argument("--json", action="store_true", help="Print full JSON report.")
    args = parser.parse_args(argv)

    order_paths = args.orders_dbs or _existing(DEFAULT_ORDER_DBS)
    result_paths = args.results_dbs or _existing(DEFAULT_RESULT_DBS)
    orders = []
    for path in order_paths:
        orders.extend(load_executor_orders(path))
    results, conflicts = load_official_results(result_paths)
    report = reconstruct_order_pnl(orders, results, prefer_final=args.prefer_final)
    report["inputs"] = {
        "orders_dbs": order_paths,
        "results_dbs": result_paths,
        "order_rows": len(orders),
        "official_results": len(results),
        "official_result_conflicts": {k: sorted(v) for k, v in conflicts.items()},
        "prefer_final": bool(args.prefer_final),
    }

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    s = report["summary"]
    print(f"order_rows={len(orders)} official_results={len(results)} prefer_final={bool(args.prefer_final)}")
    print(
        "contracts "
        f"entry={s['entry_contracts']} exit={s['exit_contracts']} "
        f"settlement={s['settlement_contracts']}"
    )
    print(
        "pnl_cents "
        f"exit={s['exit_pnl_cents']} settlement={s['settlement_pnl_cents']} "
        f"total={s['total_pnl_cents']} exit_minus_hold={s['exit_minus_hold_cents']}"
    )
    print(
        "data_gaps "
        f"missing_results={s['missing_result_contracts']} "
        f"unmatched_exits={s['unmatched_exit_contracts']} "
        f"missing_price_orders={s['missing_price_orders']} "
        f"missing_fee_orders={s['missing_fee_orders']} "
        f"missing_side_orders={s['missing_side_orders']} "
        f"missing_fill_count_orders={s['missing_fill_count_orders']} "
        f"result_conflicts={len(conflicts)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
