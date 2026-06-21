#!/usr/bin/env python3
"""Read-only resolution health check for the v95 ledger — run ON THE REPL.

Answers one question: is the prediction ledger actually turning predictions into
officially-settled ("resolved") rows under the CURRENT model version, or is
settlement stuck?

It NEVER writes: the SQLite file is opened in read-only mode (mode=ro), so it
cannot create, mutate, or migrate the db. Safe to run while the app is live.

Usage (on the Repl):
    python3 scripts/check_resolution.py
    python3 scripts/check_resolution.py --db data/q15_v95_ledger_v1.sqlite3
    python3 scripts/check_resolution.py --health http://localhost:8080/api/health
    python3 scripts/check_resolution.py --stale-minutes 10   # settlement grace

A row is "resolved" when its Kalshi market has officially settled YES/NO and
that result is written back (official_result IS NOT NULL). "Pending" = the
market has closed (close_time <= now) but is not yet graded — a few minutes of
this is normal (settlement lag); a growing backlog of OLD pending rows means
reconciliation is not running (suspect a paused deploy or a Kalshi client with
no get_market).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

# Running a script file puts scripts/ on sys.path, not the repo root, so add the
# repo root so `q15_upgrade` imports regardless of the working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Canonical model version + calibration threshold, imported from the live code so
# this check stays in lock-step with production. Falls back to literals if the
# package isn't importable from wherever the script is run.
try:  # pragma: no cover - environment dependent
    from q15_upgrade.ledger_v95 import MODEL_VERSION as CODE_MODEL_VERSION
except Exception:  # noqa: BLE001
    CODE_MODEL_VERSION = None
try:  # pragma: no cover
    import os
    CALIB_MIN = int(os.environ.get("Q15_V95_CALIBRATION_MIN_ROWS", "30"))
except Exception:  # noqa: BLE001
    CALIB_MIN = 30

CHECKPOINTS = ("15M", "10M", "7M")


def _ro_connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"ledger not found: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds/60:.0f}m ago"
    return f"{seconds/3600:.1f}h ago"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/q15_v95_ledger_v1.sqlite3")
    ap.add_argument("--stale-minutes", type=float, default=5.0,
                    help="grace period before a closed-but-unresolved row counts as STUCK")
    ap.add_argument("--health", default=None,
                    help="optional /api/health URL to read live reconcile counters")
    args = ap.parse_args()

    now = time.time()
    conn = _ro_connect(Path(args.db))
    print(f"ledger : {args.db}  (opened read-only)")

    # --- model versions present vs. the version the code is writing now ----------
    versions = conn.execute(
        "SELECT model_version, COUNT(*) n FROM predictions GROUP BY model_version ORDER BY n DESC"
    ).fetchall()
    print(f"code MODEL_VERSION : {CODE_MODEL_VERSION or '(unimportable — run from repo root)'}")
    print("rows by model_version:")
    for v in versions:
        marker = "  <- current" if v["model_version"] == CODE_MODEL_VERSION else ""
        print(f"  {v['n']:6d}  {v['model_version']}{marker}")

    mv = CODE_MODEL_VERSION or (versions[0]["model_version"] if versions else None)
    if mv is None:
        print("no predictions at all — ledger is empty.")
        return 0
    print(f"\nscoping all stats below to: {mv}\n")

    stale_cutoff = now - args.stale_minutes * 60.0

    # --- per-checkpoint breakdown ------------------------------------------------
    print(f"{'cp':>4} | {'total':>6} {'resolved':>9} {'pending':>8} {'stuck':>6} "
          f"{'acc%':>6} {'P&L¢':>8} {'calib':>14}")
    print("-" * 74)
    tot_all = res_all = pend_all = stuck_all = 0
    for cp in CHECKPOINTS:
        row = conn.execute(
            """SELECT
                 COUNT(*) total,
                 SUM(official_result IS NOT NULL) resolved,
                 SUM(official_result IS NULL AND close_time IS NOT NULL AND close_time <= ?) pending,
                 SUM(official_result IS NULL AND close_time IS NOT NULL AND close_time <= ?) stuck,
                 SUM(CASE WHEN official_result IS NOT NULL THEN correct ELSE 0 END) correct,
                 SUM(CASE WHEN official_result IS NOT NULL THEN COALESCE(realized_cents,0) ELSE 0 END) pnl
               FROM predictions WHERE model_version=? AND checkpoint=?""",
            (now, stale_cutoff, mv, cp),
        ).fetchone()
        total = row["total"] or 0
        resolved = row["resolved"] or 0
        pending = row["pending"] or 0
        stuck = row["stuck"] or 0
        acc = (100.0 * (row["correct"] or 0) / resolved) if resolved else None
        pnl = row["pnl"] or 0
        ready = "READY" if resolved >= CALIB_MIN else f"{resolved}/{CALIB_MIN}"
        print(f"{cp:>4} | {total:6d} {resolved:9d} {pending:8d} {stuck:6d} "
              f"{(f'{acc:.1f}' if acc is not None else '—'):>6} {pnl:8.0f} {ready:>14}")
        tot_all += total; res_all += resolved; pend_all += pending; stuck_all += stuck
    print("-" * 74)
    print(f"{'all':>4} | {tot_all:6d} {res_all:9d} {pend_all:8d} {stuck_all:6d}")

    # --- recency: is settlement actually moving? ---------------------------------
    last_res = conn.execute(
        "SELECT MAX(resolved_at) t FROM predictions WHERE model_version=? AND official_result IS NOT NULL",
        (mv,),
    ).fetchone()["t"]
    last_pred = conn.execute(
        "SELECT MAX(created_at) t FROM predictions WHERE model_version=?", (mv,)
    ).fetchone()["t"]
    oldest_stuck = conn.execute(
        """SELECT MIN(close_time) t FROM predictions
           WHERE model_version=? AND official_result IS NULL
             AND close_time IS NOT NULL AND close_time <= ?""",
        (mv, stale_cutoff),
    ).fetchone()["t"]
    print(f"\nlast prediction recorded : {_fmt_age(now - last_pred) if last_pred else '—'}")
    print(f"last row resolved        : {_fmt_age(now - last_res) if last_res else '— (none yet)'}")
    if oldest_stuck:
        print(f"oldest STUCK pending     : closed {_fmt_age(now - oldest_stuck)} "
              f"(> {args.stale_minutes:.0f}m grace)")

    # --- optional: live reconcile counters from /api/health ----------------------
    if args.health:
        try:
            import json
            import urllib.request
            with urllib.request.urlopen(args.health, timeout=5) as fh:
                health = json.load(fh)
            v95 = (health.get("q15_v9_5") or health.get("q15_v95") or {})
            rec = v95.get("market_reconcile") or v95.get("last_market_reconcile") or {}
            print("\n/api/health market_reconcile:")
            for k in ("available", "tickers_checked", "market_calls",
                      "new_predictions_resolved", "budget_exceeded", "elapsed_seconds"):
                if k in rec:
                    print(f"  {k}: {rec[k]}")
            if not rec:
                print("  (no market_reconcile block found — check the health JSON shape)")
        except Exception as exc:  # noqa: BLE001
            print(f"\n/api/health unreachable: {type(exc).__name__}: {exc}")

    # --- verdict -----------------------------------------------------------------
    print("\nverdict:")
    if res_all == 0 and tot_all == 0:
        print("  EMPTY — no predictions under the current model version yet.")
    elif res_all == 0 and stuck_all == 0:
        print("  FRESH/BAKING — predictions recorded, none closed long enough to settle. Normal early on.")
    elif stuck_all > 0 and (last_res is None or (now - last_res) > 3600):
        print("  ⚠️ LIKELY STUCK — closed markets are not being graded and nothing has resolved")
        print("     in over an hour. Suspect: deploy not live (Relay paused), Kalshi client has")
        print("     no get_market, or reconcile budget starved. Check the app logs + /api/health.")
    elif stuck_all > 0:
        print(f"  PROGRESSING with a backlog — {stuck_all} closed rows awaiting settlement; last")
        print("     resolution was recent, so reconciliation is running (settlement lag / budget).")
    else:
        print("  HEALTHY — resolutions are current and there is no stuck backlog.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
