"""One cloud pick cycle — BetsAPI data, git-persisted state, no browser.

Designed to be fired hourly by a Claude Code Routine in a FRESH ephemeral
session: restore the SQLite state from the ``tt-edge-state`` branch, fetch
the TT Elite boards + per-match history + Bet365 odds series from BetsAPI,
run the unchanged grade -> scan -> alert pipeline, then push the state
back. Exits 3 (quietly, by design) when TT_EDGE_BETSAPI_KEY is unset so a
scheduled run costs nearly nothing until the operator adds the key.

Output contract for the Routine driver: a ``PICKS (n)`` section lists each
new alert verbatim; ``no qualifying picks`` otherwise. Telegram delivery
happens in-process when credentials are present (TT_EDGE_TELEGRAM_* or the
Q15 fallback); the printed picks let the Routine's completion notification
reach the operator's phone even with no Telegram configured.

Usage::

    python3 -m tt_edge.jobs.cloud_cycle [--dry-run] [--no-state]
"""
from __future__ import annotations

import argparse
import dataclasses
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from tt_edge.alerts.telegram import TTEdgeTelegram
from tt_edge.config import load_config
from tt_edge.db import repo as repo_mod
from tt_edge.envfile import REPO_ROOT, bootstrap_env
from tt_edge.integration import ensure_bankroll
from tt_edge.jobs import setup_logging
from tt_edge.jobs.autoscan import CycleReport, cycle_dates, run_cycle
from tt_edge.scrapers import betsapi
from tt_edge import state_sync

logger = logging.getLogger(__name__)


def run_cloud_cycle(*, repo: repo_mod.TTEdgeRepo, config, client,
                    now: datetime, sender: TTEdgeTelegram | None,
                    league_id: str, dry_run: bool
                    ) -> tuple[CycleReport, int]:
    """Fetch from BetsAPI and run one cycle. Injected client — testable."""
    fetch_dates, scan_dates = cycle_dates(now, config.autoscan_dates_forward)
    bundle = betsapi.fetch_cloud_bundle(
        client, fetch_dates, league_id=league_id,
        max_events=config.autoscan_max_matches, now=now)
    inserted = sum(1 for snapshot in bundle.odds_snapshots
                   if repo.insert_odds_snapshot(snapshot))
    report = run_cycle(repo=repo, config=config, envelopes=bundle.envelopes,
                       scan_dates=scan_dates, now=now, sender=sender,
                       dry_run=dry_run)
    return report, inserted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tt_edge.jobs.cloud_cycle",
        description="One BetsAPI-powered cycle with git-persisted state.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print alerts instead of sending Telegram")
    parser.add_argument("--no-state", action="store_true",
                        help="skip the state-branch restore/push (testing)")
    parser.add_argument("--db", help="override TT_EDGE_DATABASE_URL")
    args = parser.parse_args(argv)

    setup_logging()
    bootstrap_env()
    token = os.environ.get("TT_EDGE_BETSAPI_KEY", "").strip()
    if not token:
        print("TT-EDGE CLOUD: no TT_EDGE_BETSAPI_KEY — skipped")
        return 3

    config = load_config()
    if os.environ.get("TT_EDGE_BOOK") is None:
        config = dataclasses.replace(config, book=betsapi.BOOK)
    league_id = os.environ.get("TT_EDGE_BETSAPI_LEAGUE_ID",
                               betsapi.DEFAULT_LEAGUE_ID)
    base_url = os.environ.get("TT_EDGE_BETSAPI_BASE",
                              betsapi.DEFAULT_BASE_URL)
    branch = os.environ.get("TT_EDGE_STATE_BRANCH", state_sync.DEFAULT_BRANCH)
    database_url = args.db or config.database_url

    use_state = not args.no_state and not database_url.startswith(
        ("postgres://", "postgresql://"))
    db_path = Path(database_url.removeprefix("sqlite:///"))
    if use_state:
        state_sync.restore_state(db_path, REPO_ROOT, branch)

    repo = repo_mod.connect(database_url)
    try:
        repo.apply_migrations()
        ensure_bankroll(repo)
        sender = None if args.dry_run else TTEdgeTelegram.from_config(config)
        now = datetime.now(timezone.utc)
        report, odds_inserted = run_cloud_cycle(
            repo=repo, config=config,
            client=betsapi.BetsAPIClient(token, base_url=base_url),
            now=now, sender=sender, league_id=league_id,
            dry_run=args.dry_run)
        bankroll = repo.get_bankroll_cents()
    finally:
        repo.close()

    if use_state:
        state_sync.push_state(db_path, REPO_ROOT, branch,
                              label=now.isoformat())

    picks = [outcome for outcome in report.outcomes
             if outcome.alert_text and not outcome.reasons]
    print(f"TT-EDGE CLOUD CYCLE: {len(report.outcomes)} match(es) scanned, "
          f"{odds_inserted} odds row(s), "
          f"{report.grade.recommendations_settled} settled "
          f"({report.grade.profit_cents:+d}c), bankroll "
          f"{'-' if bankroll is None else f'{bankroll}c'}")
    if picks:
        print(f"PICKS ({len(picks)}):")
        for outcome in picks:
            delivered = "delivered" if outcome.alert_delivered else \
                "NOT delivered (telegram unavailable)"
            print(f"--- {delivered} ---")
            print(outcome.alert_text)
    else:
        print("no qualifying picks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
