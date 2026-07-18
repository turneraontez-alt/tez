"""Manual odds entry — the Phase 0 stopgap odds source.

The operator types the two-way prices currently posted at the book actually
being bet (the only prices that matter). Each entry is an append-only
snapshot; entering fresh prices repeatedly builds the movement history the
fix-risk guard reads. After writing, the CLI prints the line movement so a
steaming line is visible immediately at entry time.

Usage::

    python3 -m tt_edge.jobs.odds_entry --match 12345 --home -120 --away +100
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

from tt_edge.config import load_config
from tt_edge.db import repo as repo_mod
from tt_edge.edge.vig import movement_cents
from tt_edge.envfile import bootstrap_env
from tt_edge.jobs import setup_logging
from tt_edge.scrapers.odds import OddsSnapshot, parse_american_str


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tt_edge.jobs.odds_entry",
        description="Record the book's current two-way prices for a match.")
    parser.add_argument("--match", required=True,
                        help="match source event id (from the scan output)")
    parser.add_argument("--home", required=True,
                        help="American price on the home player, e.g. -120")
    parser.add_argument("--away", required=True,
                        help="American price on the away player, e.g. +100")
    parser.add_argument("--book", default="manual",
                        help="book label (default: manual)")
    parser.add_argument("--captured-at",
                        help="ISO timestamp of the observation "
                             "(default: now UTC)")
    parser.add_argument("--db", help="override TT_EDGE_DATABASE_URL")
    args = parser.parse_args(argv)

    setup_logging()
    bootstrap_env()
    config = load_config()
    captured_at = (datetime.fromisoformat(args.captured_at)
                   if args.captured_at else datetime.now(timezone.utc))
    snapshot = OddsSnapshot(match_source_id=args.match, book=args.book,
                            home_price=parse_american_str(args.home),
                            away_price=parse_american_str(args.away),
                            captured_at=captured_at)

    repo = repo_mod.connect(args.db or config.database_url)
    try:
        repo.apply_migrations()
        inserted = repo.insert_odds_snapshot(snapshot)
        history = repo.odds_history(args.match)
    finally:
        repo.close()

    if not inserted:
        print("duplicate capture instant — snapshot already recorded")
    print(f"match {args.match}: {len(history)} snapshot(s) on record")
    if len(history) > 1:
        first, last = history[0], history[-1]
        print(f"  movement since first capture: "
              f"home {movement_cents(first.home_price, last.home_price):+d}c, "
              f"away {movement_cents(first.away_price, last.away_price):+d}c "
              f"(>= +{config.fix_risk_cents}c against the model side "
              f"= fix-risk hard pass)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
