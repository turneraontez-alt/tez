#!/usr/bin/env python3
"""One-time repair: stamp production's actual decision onto historical shadow rows.

Background
----------
The Shadow-vs-Your-System comparison used to grade and rank the native ("Your
System") side by re-thresholding the RAW champion probability it had stored
(``control_prob_yes``). Production's sent ``predicted_side`` comes from the
CALIBRATED probability, and the two disagree on the side in ~34% of rows — so a
real loss could show as a ✓. The live fix captures production's decision
(``predicted_side`` + calibrated prob + rank) on every NEW shadow row. This script
backfills that decision onto rows recorded BEFORE the fix, so the historical
all-time records are re-graded correctly too.

It is READ-ONLY with respect to the production ledger; it only updates the
challenger DB's new ``native_predicted_side`` / ``native_prob_yes`` / ``native_rank``
columns (never an already-captured decision, never the stored predictions).

Run on the Repl (where the real ledgers live)::

    python3 -m tools.backfill_shadow_native_side            # all shadow model_versions
    python3 -m tools.backfill_shadow_native_side --version challenger-v5

DB paths come from the same env vars the app uses (``Q15_V95_LEDGER_DB`` /
``Q15_CHALLENGER_DB``); override on the command line if needed.
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from q15_upgrade.challenger.ledger import ShadowLedger

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill_shadow_native_side")

_PROD_DEFAULT = os.environ.get("Q15_V95_LEDGER_DB", "data/q15_v95_ledger_v1.sqlite3")
_CHALLENGER_DEFAULT = os.environ.get("Q15_CHALLENGER_DB", "data/q15_challenger_shadow_v1.sqlite3")


def build_production_lookup(prod_db_path: str) -> dict[tuple[str, str], tuple[str, float | None, int | None]]:
    """Map (ticker, checkpoint) -> (predicted_side, calibrated_yes_probability, rank).

    Read-only. When a (ticker, checkpoint) appears under more than one production
    model_version, the most recent decision (max ``created_at``) wins — the value
    closest to what the shadow row was paired with. Rows without a YES/NO
    ``predicted_side`` are skipped.
    """
    conn = sqlite3.connect(f"file:{prod_db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    lookup: dict[tuple[str, str], tuple[str, float | None, int | None]] = {}
    try:
        rows = conn.execute(
            "SELECT ticker, checkpoint, predicted_side, calibrated_yes_probability, rank, "
            "created_at FROM predictions ORDER BY created_at ASC"
        )
        for r in rows:
            side = str(r["predicted_side"]).upper() if r["predicted_side"] is not None else None
            if side not in ("YES", "NO"):
                continue
            key = (str(r["ticker"]), str(r["checkpoint"]))
            # ORDER BY created_at ASC + last-write-wins => most recent decision kept.
            lookup[key] = (side, r["calibrated_yes_probability"], r["rank"])
    finally:
        conn.close()
    return lookup


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prod-db", default=_PROD_DEFAULT, help="production v95 ledger sqlite path")
    ap.add_argument("--challenger-db", default=_CHALLENGER_DEFAULT, help="challenger shadow sqlite path")
    ap.add_argument("--version", default=None,
                    help="only this shadow model_version (default: every version in the file)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report how many rows WOULD be backfilled, change nothing")
    args = ap.parse_args(argv)

    if not os.path.exists(args.prod_db):
        logger.error("production ledger not found: %s", args.prod_db)
        return 2
    if not os.path.exists(args.challenger_db):
        logger.error("challenger ledger not found: %s", args.challenger_db)
        return 2

    logger.info("building production decision lookup from %s", args.prod_db)
    lookup = build_production_lookup(args.prod_db)
    logger.info("production decisions available: %d (ticker, checkpoint) keys", len(lookup))

    led = ShadowLedger(args.challenger_db)
    try:
        if args.version:
            versions = [args.version]
        else:
            versions = [
                str(row["model_version"])
                for row in led._conn.execute(  # noqa: SLF001 - maintenance script
                    "SELECT DISTINCT model_version FROM shadow_predictions ORDER BY model_version")
            ]
        logger.info("shadow model_versions to repair: %s", ", ".join(versions) or "(none)")

        def _lookup(contract: str, checkpoint: str):
            return lookup.get((str(contract), str(checkpoint)))

        totals = {"scanned": 0, "updated": 0, "unmatched": 0}
        for mv in versions:
            if args.dry_run:
                # Count only; do not mutate.
                pending = led._conn.execute(  # noqa: SLF001 - maintenance script
                    "SELECT COUNT(*) AS n FROM shadow_predictions "
                    "WHERE model_version=? AND native_predicted_side IS NULL", (mv,)
                ).fetchone()["n"]
                would = sum(1 for row in led._conn.execute(  # noqa: SLF001
                    "SELECT contract, checkpoint FROM shadow_predictions "
                    "WHERE model_version=? AND native_predicted_side IS NULL", (mv,))
                    if _lookup(row["contract"], row["checkpoint"]) is not None)
                logger.info("[dry-run] %s: %d missing, %d matchable in production", mv, pending, would)
                continue
            stats = led.backfill_native_decisions(_lookup, model_version=mv)
            logger.info("%s: scanned=%d updated=%d unmatched=%d",
                        mv, stats["scanned"], stats["updated"], stats["unmatched"])
            for k in totals:
                totals[k] += stats[k]
        if not args.dry_run:
            logger.info("DONE — total scanned=%d updated=%d unmatched=%d",
                        totals["scanned"], totals["updated"], totals["unmatched"])
    finally:
        led.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
