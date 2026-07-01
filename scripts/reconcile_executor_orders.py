#!/usr/bin/env python3
"""Reconcile local executor order rows with Kalshi account order history."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from q15_upgrade.executor.config import ExecutorConfig, yes_config_from_env  # noqa: E402
from q15_upgrade.executor.store import ExecutorStore  # noqa: E402
from q15_upgrade.executor.trading_client import KalshiTradingClient  # noqa: E402


def _default_db_paths() -> list[str]:
    paths = [ExecutorConfig.from_env().orders_db_path, yes_config_from_env().orders_db_path]
    out: list[str] = []
    for path in paths:
        if path not in out:
            out.append(path)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--orders-db",
        action="append",
        dest="orders_dbs",
        help="Local executor order sqlite DB to update; repeatable. Defaults to NO and YES stores.",
    )
    args = parser.parse_args(argv)

    cfg = ExecutorConfig.from_env()
    client = KalshiTradingClient(cfg)
    account_orders = client.get_orders()
    print(f"broker_orders={len(account_orders)}")

    paths = args.orders_dbs or _default_db_paths()
    for path in paths:
        db_path = Path(path)
        if not db_path.exists():
            print(f"{path}: skipped_missing_local_store")
            continue
        store = ExecutorStore(str(db_path))
        summary = store.reconcile_orders(account_orders)
        final = store.final_fill_summary()
        print(
            f"{path}: matched={summary['matched']} updated={summary['updated']} "
            f"unmatched_broker_orders={summary['unmatched']} final_fill_rate={final['fill_rate']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
