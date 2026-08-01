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


def grade_only_result_dates(repo: repo_mod.TTEdgeRepo,
                            scan_league_ids: list[str]) -> dict[str, list[str]]:
    """league_id -> UTC dates whose RESULTS must be fetched to settle open
    claims in leagues NOT scanned this cycle.

    Without this, a claim left behind when its league drops out of the scan
    list (split coverage) never sees a result again — it stays open forever
    and pins the max-open-picks cap, silently suppressing every future pick.
    Dates come from each claim's own match start time, so even a days-old
    claim gets its result day fetched. Unmapped tournament names are logged
    and skipped (they need an entry in betsapi.LEAGUE_ID_BY_NAME)."""
    dates_by_league: dict[str, dict[str, None]] = {}
    for row in repo.open_recommendation_matches():
        league_id = betsapi.LEAGUE_ID_BY_NAME.get(row["tournament"])
        if league_id is None:
            logger.warning(
                "cloud: open pick in unmapped tournament %r — cannot fetch "
                "its results (add it to betsapi.LEAGUE_ID_BY_NAME)",
                row["tournament"])
            continue
        if league_id in scan_league_ids or row["start_time"] is None:
            continue
        day = row["start_time"].astimezone(timezone.utc).date().isoformat()
        dates_by_league.setdefault(league_id, {}).setdefault(day, None)
    return {league_id: list(days)
            for league_id, days in dates_by_league.items()}


def run_cloud_cycle(*, repo: repo_mod.TTEdgeRepo, config, client,
                    now: datetime, sender: TTEdgeTelegram | None,
                    league_ids: list[str], dry_run: bool
                    ) -> tuple[CycleReport, int]:
    """Fetch every league from BetsAPI and run ONE merged cycle.

    Each league is fetched independently (a failure in one is logged and
    skipped, never fatal to the others) and their canonical envelopes +
    odds are concatenated. Match ids are globally unique across leagues, so
    claims, grading, and the shared paper bankroll all compose correctly.
    Leagues outside ``league_ids`` that still hold open claims get a
    results-only fetch so those claims settle without ever being scanned.
    Injected client — testable."""
    fetch_dates, scan_dates = cycle_dates(now, config.autoscan_dates_forward)
    envelopes = []
    odds_snapshots = []
    for league_id in league_ids:
        bundle = betsapi.fetch_cloud_bundle(
            client, fetch_dates, league_id=league_id,
            max_events=config.autoscan_max_matches, now=now)
        envelopes.extend(bundle.envelopes)
        odds_snapshots.extend(bundle.odds_snapshots)
    grade_fetches = grade_only_result_dates(repo, league_ids)
    for league_id, result_dates in grade_fetches.items():
        bundle = betsapi.fetch_results_bundle(
            client, result_dates, league_id=league_id, now=now)
        envelopes.extend(bundle.envelopes)
    if grade_fetches:
        logger.info("cloud: grade-only results fetched for league(s) %s",
                    ", ".join(sorted(grade_fetches)))
    inserted = sum(1 for snapshot in odds_snapshots
                   if repo.insert_odds_snapshot(snapshot))
    report = run_cycle(repo=repo, config=config, envelopes=envelopes,
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
    # In cloud mode the BetsAPI league id already isolates each league, so
    # the sofascore-style tournament-name filter is redundant and would drop
    # every non-"TT Elite" league; disable it unless the operator set one.
    if os.environ.get("TT_EDGE_TOURNAMENT_KEYWORD") is None:
        config = dataclasses.replace(config, tournament_keyword="")
    league_ids = betsapi.parse_league_ids(
        os.environ.get("TT_EDGE_BETSAPI_LEAGUE_ID"))
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
            now=now, sender=sender, league_ids=league_ids,
            dry_run=args.dry_run)
        bankroll = repo.get_bankroll_cents()
    finally:
        repo.close()

    if use_state:
        state_sync.push_state(db_path, REPO_ROOT, branch,
                              label=now.isoformat())

    picks = [outcome for outcome in report.outcomes
             if outcome.alert_text and not outcome.reasons]
    print(f"TT-EDGE CLOUD CYCLE: {len(league_ids)} league(s), "
          f"{len(report.outcomes)} match(es) scanned, "
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
