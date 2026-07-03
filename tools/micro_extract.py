"""Microstructure research extract — per-window compressed feature paths.

The deep-microstructure sources (spot_depth_snapshots ~1.1M rows / 683MB,
kraken_l3_summaries ~1.1M rows / 926MB) are too large for the learning-snapshots
export, so the research container can never see them. This tool bridges that:
for each recently SETTLED 15-min window it slices both sources over the final
900 seconds, downsamples to a bounded point count, computes compact per-point
feature vectors (including a wall metric from the level arrays), and writes ONE
gzip-JSON row per (asset, close_time) into a small DB that the export CAN ship
(~3-5MB/day).

Feature vector per point (versioned; see FEATURES_V1):
  spot:   ts, mid, spread_bps, depth_imbalance, bid_depth_top, ask_depth_top,
          trade_net_qty_5s, trade_buy_notional_5s, trade_sell_notional_5s
  kraken: ts, mid, spread_bps, depth_imbalance, cancel_to_add_5s, add_count_5s,
          delete_count_5s, trade_count_5s, avg_bid_order_size_levels,
          avg_ask_order_size_levels, net_matched_buy_notional_60s
  walls:  per side, from the levels_json top levels: max_level/median_level
          size ratio and that level's distance from mid in bps.

Read-only toward its sources. Idempotent: UNIQUE(asset, close_time) with
INSERT OR IGNORE. Run on the live host (cron/next to the learning export):
  python tools/micro_extract.py [--hours 26] [--dry-run]
Env: Q15_SPOT_DEPTH_DB, Q15_KRAKEN_L3_DB, Q15_V95_LEDGER_DB, Q15_MICRO_PATHS_DB.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import sqlite3
import statistics
import time

FEATURES_V1 = "micro-paths-v1"
MAX_POINTS_PER_SOURCE = 300          # ~3s effective cadence over 900s

_SCHEMA = """
CREATE TABLE IF NOT EXISTS micro_paths (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    asset TEXT NOT NULL,
    close_time REAL NOT NULL,
    features_version TEXT NOT NULL,
    spot_points INTEGER, kraken_points INTEGER,
    official_result TEXT,             -- from the v95 oracle when known at extract time
    blob_gz BLOB NOT NULL,            -- {"spot": [...], "kraken": [...]}
    UNIQUE(asset, close_time)
);
"""


def _num(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def wall_metric(levels_json: str | None, mid: float | None):
    """(max_size/median_size, distance_bps of that level) from a levels array.

    Accepts [[price, size], ...] or [{"price":..,"size"/"qty":..}, ...]; top 10
    levels only. Returns (None, None) when unparseable — walls are a bonus
    feature, never a blocker.
    """
    if not levels_json or mid is None or mid <= 0:
        return (None, None)
    try:
        levels = json.loads(levels_json)[:10]
        parsed = []
        for lv in levels:
            if isinstance(lv, (list, tuple)) and len(lv) >= 2:
                px, sz = _num(lv[0]), _num(lv[1])
            elif isinstance(lv, dict):
                px = _num(lv.get("price"))
                sz = _num(lv.get("size", lv.get("qty")))
            else:
                continue
            if px and sz and sz > 0:
                parsed.append((px, sz))
        if len(parsed) < 3:
            return (None, None)
        sizes = [s for _, s in parsed]
        med = statistics.median(sizes)
        if med <= 0:
            return (None, None)
        px, mx = max(parsed, key=lambda t: t[1])
        return (round(mx / med, 2), round(abs(px - mid) / mid * 10_000.0, 1))
    except Exception:
        return (None, None)


def _downsample(rows, max_points=MAX_POINTS_PER_SOURCE):
    if len(rows) <= max_points:
        return rows
    step = len(rows) / max_points
    return [rows[int(i * step)] for i in range(max_points)]


def _spot_slice(conn, asset, close_time):
    rows = conn.execute(
        "SELECT created_at, mid, spread_bps, depth_imbalance, bid_depth_top,"
        " ask_depth_top, trade_net_qty_5s, trade_buy_notional_5s,"
        " trade_sell_notional_5s, bid_levels_json, ask_levels_json"
        " FROM spot_depth_snapshots WHERE asset=? AND created_at BETWEEN ? AND ?"
        " ORDER BY created_at", (asset, close_time - 900.0, close_time)).fetchall()
    out = []
    for r in _downsample(rows):
        mid = _num(r[1])
        bw, bd = wall_metric(r[9], mid)
        aw, ad = wall_metric(r[10], mid)
        out.append([round(r[0], 1), mid, _num(r[2]), _num(r[3]), _num(r[4]),
                    _num(r[5]), _num(r[6]), _num(r[7]), _num(r[8]), bw, bd, aw, ad])
    return out


_KRAKEN_SYMBOL = {"BTC": "BTC/USD", "ETH": "ETH/USD", "SOL": "SOL/USD",
                  "XRP": "XRP/USD", "DOGE": "DOGE/USD", "BNB": "BNB/USD",
                  "HYPE": "HYPE/USD"}


def _kraken_slice(conn, asset, close_time):
    sym = _KRAKEN_SYMBOL.get(asset, f"{asset}/USD")
    rows = conn.execute(
        "SELECT created_at, mid, spread_bps, depth_imbalance, cancel_to_add_5s,"
        " add_count_5s, delete_count_5s, trade_count_5s,"
        " avg_bid_order_size_levels, avg_ask_order_size_levels,"
        " net_matched_buy_notional_60s, bid_levels_json, ask_levels_json"
        " FROM kraken_l3_summaries WHERE symbol=? AND created_at BETWEEN ? AND ?"
        " ORDER BY created_at", (sym, close_time - 900.0, close_time)).fetchall()
    out = []
    for r in _downsample(rows):
        mid = _num(r[1])
        bw, bd = wall_metric(r[11], mid)
        aw, ad = wall_metric(r[12], mid)
        out.append([round(r[0], 1), mid, _num(r[2]), _num(r[3]), _num(r[4]),
                    r[5], r[6], r[7], _num(r[8]), _num(r[9]), _num(r[10]),
                    bw, bd, aw, ad])
    return out


def run(spot_db, kraken_db, v95_db, out_db, hours=26.0, dry_run=False, now=None):
    now = now or time.time()
    out = sqlite3.connect(out_db)
    out.executescript(_SCHEMA)
    spot = sqlite3.connect(f"file:{spot_db}?mode=ro", uri=True) if os.path.exists(spot_db) else None
    kraken = sqlite3.connect(f"file:{kraken_db}?mode=ro", uri=True) if os.path.exists(kraken_db) else None
    if spot is None and kraken is None:
        print("micro_extract: no source DBs found; nothing to do")
        return {"windows": 0, "written": 0}
    # settled windows in the lookback, from the v95 ledger (the window census)
    v95 = sqlite3.connect(f"file:{v95_db}?mode=ro", uri=True)
    wins = v95.execute(
        "SELECT DISTINCT asset, close_time, MAX(official_result) FROM predictions"
        " WHERE close_time BETWEEN ? AND ? GROUP BY asset, close_time",
        (now - hours * 3600.0, now - 60.0)).fetchall()
    written = 0
    for asset, close_time, result in wins:
        sp = _spot_slice(spot, asset, close_time) if spot else []
        kr = _kraken_slice(kraken, asset, close_time) if kraken else []
        if not sp and not kr:
            continue
        if dry_run:
            written += 1
            continue
        blob = gzip.compress(json.dumps({"spot": sp, "kraken": kr}).encode())
        cur = out.execute(
            "INSERT OR IGNORE INTO micro_paths (created_at, asset, close_time,"
            " features_version, spot_points, kraken_points, official_result, blob_gz)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (now, asset, close_time, FEATURES_V1, len(sp), len(kr),
             result if result in ("YES", "NO") else None, blob))
        written += cur.rowcount
    out.commit()
    print(f"micro_extract: {len(wins)} settled windows scanned, {written} paths "
          f"{'would be ' if dry_run else ''}written")
    return {"windows": len(wins), "written": written}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--spot-db", default=os.environ.get("Q15_SPOT_DEPTH_DB", "data/q15_spot_depth_v1.sqlite3"))
    ap.add_argument("--kraken-db", default=os.environ.get("Q15_KRAKEN_L3_DB", "data/q15_kraken_l3_v1.sqlite3"))
    ap.add_argument("--v95-db", default=os.environ.get("Q15_V95_LEDGER_DB", "data/q15_v95_ledger_v1.sqlite3"))
    ap.add_argument("--out", default=os.environ.get("Q15_MICRO_PATHS_DB", "data/q15_micro_paths_v1.sqlite3"))
    ap.add_argument("--hours", type=float, default=26.0)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    run(a.spot_db, a.kraken_db, a.v95_db, a.out, hours=a.hours, dry_run=a.dry_run)


if __name__ == "__main__":
    main()
