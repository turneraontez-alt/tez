"""Autoscan — the recurring loop that makes picks arrive on their own.

Each cycle, on an interval (default 30 min):

1. FETCH a bundle from sofascore (browser-context API GETs): yesterday's
   board (for results), today's + tomorrow's boards (for picks), and per
   upcoming TT Elite match its H2H, both players' form, and the odds.
   Cached H2H/form younger than TT_EDGE_H2H_REFRESH_S are reused.
2. GRADE first: finished events on any cached board become results; open
   recommendations settle and the bankroll moves (frees concurrent-pick
   slots before new picks are considered).
3. ODDS: pre-match sofascore prices are appended as odds snapshots under
   book "sofascore" — the per-cycle history powers the fix-risk movement
   guard exactly like manual entries do.
4. SCAN today's + tomorrow's boards; qualifying edges alert via Telegram
   (the TT_EDGE_* bot, or the Q15 bot/chat via the default fallback).

Everything downstream is the Phase 0 pipeline unchanged — same freshness
guards, same abstains, same idempotent claims — so running autoscan and the
manual CLIs together is safe.

PRICE CONTRACT: sofascore odds are aggregated prices, not your book's. An
alert from this book means "the edge exists at this price" — take it only
if your book posts that price or better (the alert says so on its last
line). Manual odds entry (book "manual", TT_EDGE_BOOK=manual) remains the
source of truth when you have the book open.

Usage::

    python3 -m tt_edge.jobs.autoscan            # loop until Ctrl-C
    python3 -m tt_edge.jobs.autoscan --once     # single cycle (cron mode)
    python3 -m tt_edge.jobs.autoscan --once --dry-run --no-fetch
"""
from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tt_edge.alerts.telegram import TTEdgeTelegram
from tt_edge.config import TTEdgeConfig, load_config
from tt_edge.db import repo as repo_mod
from tt_edge.jobs import setup_logging
from tt_edge.jobs.grade import GradeReport, ResultInput, run_grade
from tt_edge.jobs.scan import MatchOutcome, ScanData, run_scan
from tt_edge.scrapers import sofa_odds, sofascore
from tt_edge.scrapers.events import event_list, parse_event
from tt_edge.scrapers.odds import OddsSnapshot

logger = logging.getLogger(__name__)

# Board statuses that void a claimed pick (no contest, stake returned).
_VOID_STATUSES = ("canceled", "cancelled", "postponed")


def results_from_board(payload: Any) -> list[ResultInput]:
    """Official results carried by a board payload: finished events with a
    decisive winner, plus canceled/postponed events as voids. Malformed
    events are skipped (the archive keeps the raw payload for forensics)."""
    results: list[ResultInput] = []
    for raw in event_list(payload):
        try:
            event = parse_event(raw)
        except ValueError:
            continue
        if event.status_type == "finished" and event.winner_code is not None:
            winner = "home" if event.winner_code == 1 else "away"
            results.append(ResultInput(match_source_id=event.source_event_id,
                                       winner_side=winner))
        elif event.status_type in _VOID_STATUSES:
            results.append(ResultInput(match_source_id=event.source_event_id,
                                       winner_side="void"))
    return results


def load_envelopes(data_dir: Path) -> list[sofascore.Envelope]:
    """Every readable envelope in the cache dir (unreadable files logged and
    skipped — a corrupt cache entry must not kill the cycle)."""
    envelopes: list[sofascore.Envelope] = []
    for path in sorted(data_dir.glob("*.json")):
        try:
            envelopes.append(sofascore.load_envelope(path))
        except sofascore.EnvelopeError as exc:
            logger.warning("autoscan: skipping cache file %s: %s", path, exc)
    return envelopes


@dataclass(frozen=True)
class CycleReport:
    """One autoscan cycle, summarized."""

    grade: GradeReport
    outcomes: list[MatchOutcome]
    odds_rows_inserted: int
    boards_scanned: int

    @property
    def recommended(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "RECOMMEND"
                   and not o.reasons)

    @property
    def delivered(self) -> int:
        return sum(1 for o in self.outcomes if o.alert_delivered)


def run_cycle(*, repo: repo_mod.TTEdgeRepo, config: TTEdgeConfig,
              envelopes: list[sofascore.Envelope], scan_dates: list[str],
              now: datetime, sender: TTEdgeTelegram | None,
              dry_run: bool) -> CycleReport:
    """One full grade -> odds -> scan cycle over the given envelopes.
    Injected inputs only — no browser, no wall clock, no file I/O."""
    boards = [e for e in envelopes if e.kind == sofascore.BOARD]
    h2h = {e.entity_id: e for e in envelopes if e.kind == sofascore.H2H}
    form = {e.entity_id: e for e in envelopes if e.kind == sofascore.FORM}
    odds = [e for e in envelopes if e.kind == sofascore.ODDS]

    # 1. Grade from EVERY cached board (old boards carry final results).
    results: dict[str, ResultInput] = {}
    for board in boards:
        for result in results_from_board(board.payload):
            results.setdefault(result.match_source_id, result)
    grade_report = run_grade(repo=repo, results=list(results.values()),
                             now=now, source="sofascore")

    # 2. Append this cycle's odds observations (pre-match only).
    inserted = 0
    for envelope in odds:
        try:
            parsed = sofa_odds.parse_odds_payload(envelope.payload)
        except sofa_odds.OddsError as exc:
            logger.warning("autoscan: bad odds payload for match %s: %s",
                           envelope.entity_id, exc)
            continue
        if parsed is None or parsed.is_live:
            continue
        if repo.insert_odds_snapshot(OddsSnapshot(
                match_source_id=envelope.entity_id, book=sofa_odds.BOOK,
                home_price=parsed.home_price, away_price=parsed.away_price,
                captured_at=envelope.fetched_at)):
            inserted += 1

    # 3. Scan the current boards (older cached boards are results-only).
    outcomes: list[MatchOutcome] = []
    scanned = 0
    for board in boards:
        if board.entity_id not in scan_dates:
            continue
        scanned += 1
        outcomes.extend(run_scan(
            repo=repo, config=config,
            data=ScanData(board=board, h2h=h2h, form=form), now=now,
            sender=sender, dry_run=dry_run))

    report = CycleReport(grade=grade_report, outcomes=outcomes,
                         odds_rows_inserted=inserted, boards_scanned=scanned)
    logger.info(
        "cycle: %d board(s), %d match(es), %d recommended, %d delivered, "
        "%d odds row(s), %d result(s), %d settled (%+dc), bankroll=%s",
        report.boards_scanned, len(outcomes), report.recommended,
        report.delivered, inserted, grade_report.results_recorded,
        grade_report.recommendations_settled, grade_report.profit_cents,
        grade_report.bankroll_after_cents)
    return report


def cycle_dates(now: datetime, forward: int) -> tuple[list[str], list[str]]:
    """(fetch_dates, scan_dates): fetch also covers YESTERDAY so late
    finishes get graded; scanning covers today + forward-1 days."""
    today = now.astimezone(timezone.utc).date()
    scan = [(today + timedelta(days=offset)).isoformat()
            for offset in range(forward)]
    return [(today - timedelta(days=1)).isoformat()] + scan, scan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tt_edge.jobs.autoscan",
        description="Recurring fetch -> grade -> scan -> alert loop.")
    parser.add_argument("--once", action="store_true",
                        help="run one cycle and exit (cron mode)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print alerts instead of sending Telegram")
    parser.add_argument("--no-fetch", action="store_true",
                        help="skip the browser; use cached envelopes only")
    parser.add_argument("--data-dir", type=Path,
                        help="envelope cache dir (default: "
                             "TT_EDGE_AUTOSCAN_DATA_DIR)")
    parser.add_argument("--db", help="override TT_EDGE_DATABASE_URL")
    args = parser.parse_args(argv)

    setup_logging()
    config = load_config()
    # Autoscan prices come from sofascore; only an EXPLICIT TT_EDGE_BOOK
    # overrides that (e.g. an operator running manual entries in parallel
    # keeps book=manual for the manual scan CLI, not for autoscan).
    if os.environ.get("TT_EDGE_BOOK") is None:
        config = dataclasses.replace(config, book=sofa_odds.BOOK)
    data_dir = args.data_dir or Path(config.autoscan_data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    repo = repo_mod.connect(args.db or config.database_url)
    try:
        repo.apply_migrations()
        if repo.get_bankroll_cents() is None:
            print("bankroll NOT SET — seed it once with:\n"
                  "  python3 -m tt_edge.jobs.bankroll --set 65.00")
            return 2
        sender = None if args.dry_run else TTEdgeTelegram.from_config(config)
        if sender is not None and not sender.configured:
            print("Telegram is not configured: set TT_EDGE_TELEGRAM_BOT_TOKEN"
                  " + TT_EDGE_TELEGRAM_CHAT_ID, or leave the fallback on and"
                  " set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (the Q15"
                  " channel), or run with --dry-run.")
            return 2

        while True:
            now = datetime.now(timezone.utc)
            fetch_dates, scan_dates = cycle_dates(
                now, config.autoscan_dates_forward)
            if not args.no_fetch:
                try:
                    sofascore.fetch_bundle(
                        fetch_dates, data_dir,
                        tournament_keyword=config.tournament_keyword,
                        provider_id=config.odds_provider_id,
                        max_matches=config.autoscan_max_matches,
                        request_gap_s=config.request_gap_ms / 1000.0,
                        h2h_refresh_s=float(config.h2h_refresh_s))
                except RuntimeError as exc:
                    # playwright missing — actionable, not a crash loop.
                    print(str(exc))
                    return 2
            run_cycle(repo=repo, config=config,
                      envelopes=load_envelopes(data_dir),
                      scan_dates=scan_dates,
                      now=datetime.now(timezone.utc), sender=sender,
                      dry_run=args.dry_run)
            if args.once:
                return 0
            try:
                time.sleep(config.autoscan_interval_s)
            except KeyboardInterrupt:
                logger.info("autoscan stopped")
                return 0
    except KeyboardInterrupt:
        logger.info("autoscan stopped")
        return 0
    finally:
        repo.close()


if __name__ == "__main__":
    raise SystemExit(main())
