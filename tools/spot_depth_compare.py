#!/usr/bin/env python3
"""Compare actual-coin spot depth context against resolved prediction rows.

This reads the record-only ``spot_depth_*`` columns stamped onto V2/HVF rows and
prints quick buckets for whether actual coin book/trade pressure helped.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3
from statistics import mean
from typing import Any


SOURCES = (
    ("v2", "data/q15_ultoim_v2_v1.sqlite3", "ultoim_v2_predictions", "record_kind"),
    ("hvf", "data/q15_high_vol_flip_v1.sqlite3", "hvf_alerts", "rule_code"),
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _bucket_imbalance(value: Any) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "no depth"
    if v >= 0.20:
        return "bid-heavy"
    if v <= -0.20:
        return "ask-heavy"
    return "balanced"


def _bucket_trade(value: Any) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "no trades"
    if v > 0:
        return "net-buy"
    if v < 0:
        return "net-sell"
    return "flat"


def _bucket_kalshi_depth(value: Any) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "kalshi unknown"
    if v < 10:
        return "kalshi thin"
    if v < 100:
        return "kalshi medium"
    return "kalshi deep"


def _read_rows(path: Path, table: str) -> list[sqlite3.Row]:
    if not path.is_file():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cols = _columns(conn, table)
        required = {"correct", "entry_ask_cents", "hypothetical_pnl_cents", "spot_depth_imbalance"}
        if not required.issubset(cols):
            return []
        return conn.execute(
            f'SELECT * FROM "{table}" WHERE correct IS NOT NULL '
            "AND spot_depth_imbalance IS NOT NULL ORDER BY created_at"
        ).fetchall()
    finally:
        conn.close()


def _summary(rows: list[sqlite3.Row], group_key: str) -> list[tuple[str, int, int, float, float, float]]:
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        if group_key == "spot_imbalance":
            key = _bucket_imbalance(row["spot_depth_imbalance"])
        elif group_key == "spot_trade_15s":
            key = _bucket_trade(row["spot_depth_trade_net_qty_15s"])
        else:
            key = _bucket_kalshi_depth(row["depth_contracts"])
        groups.setdefault(key, []).append(row)
    out = []
    for key, group in sorted(groups.items()):
        n = len(group)
        wins = sum(1 for row in group if int(row["correct"] or 0) == 1)
        asks = [float(row["entry_ask_cents"]) for row in group if row["entry_ask_cents"] is not None]
        pnls = [float(row["hypothetical_pnl_cents"]) for row in group if row["hypothetical_pnl_cents"] is not None]
        out.append((
            key,
            n,
            wins,
            wins / n * 100.0 if n else 0.0,
            mean(asks) if asks else 0.0,
            sum(pnls),
        ))
    return out


def _print_table(title: str, rows: list[tuple[str, int, int, float, float, float]]) -> None:
    print(f"\n{title}")
    print("bucket,n,wins,accuracy,avg_ask,net_pl_cents")
    for bucket, n, wins, acc, ask, pnl in rows:
        print(f"{bucket},{n},{wins},{acc:.1f}%,{ask:.1f},{pnl:.1f}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--v2-db", default="data/q15_ultoim_v2_v1.sqlite3")
    parser.add_argument("--hvf-db", default="data/q15_high_vol_flip_v1.sqlite3")
    args = parser.parse_args(argv)

    paths = {"v2": Path(args.v2_db), "hvf": Path(args.hvf_db)}
    any_rows = False
    for name, _default, table, _label in SOURCES:
        rows = _read_rows(paths[name], table)
        print(f"\n{name.upper()} rows with resolved spot depth: {len(rows)}")
        if not rows:
            continue
        any_rows = True
        _print_table(f"{name.upper()} by actual-coin depth imbalance", _summary(rows, "spot_imbalance"))
        _print_table(f"{name.upper()} by actual-coin 15s trade pressure", _summary(rows, "spot_trade_15s"))
        _print_table(f"{name.upper()} by Kalshi ask depth", _summary(rows, "kalshi_depth"))
    if not any_rows:
        print("\nNo resolved rows with spot_depth_* columns yet. Let the collector run, then wait for settlements.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
