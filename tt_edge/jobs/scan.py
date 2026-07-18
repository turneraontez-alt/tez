"""The main scan: board -> per-match analysis -> alert.

Data flows in as ENVELOPE files (see ``tt_edge.scrapers.sofascore``) from a
directory — produced live by the Playwright fetcher or wrapped by hand — plus
manually-entered odds already sitting in the DB (``jobs/odds_entry.py``).

For EVERY board match the scan writes a prediction row (the unbiased
calibration corpus), then either claims + alerts a recommendation or logs a
reasoned NO BET. Idempotency: the claim key is (match, market, the MATCH's
start date in UTC) — never the scan instant's date, so a rescan that crosses
UTC midnight cannot re-claim (TT Elite night sessions straddle it). A
recommendation whose alert send previously failed is re-sent WITHOUT a new
row, and that retry runs BEFORE re-evaluation — data gone stale since the
claim must not strand an undelivered alert.

Usage::

    python3 -m tt_edge.jobs.scan --data-dir data/tt_scrape --dry-run
    python3 -m tt_edge.jobs.scan --data-dir data/tt_scrape \
        --live-url https://www.sofascore.com/table-tennis
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from tt_edge import freshness
from tt_edge.alerts.telegram import AlertContext, TTEdgeTelegram, build_alert
from tt_edge.config import TTEdgeConfig, load_config
from tt_edge.db import repo as repo_mod
from tt_edge.edge import edge_calc
from tt_edge.edge.vig import movement_cents
from tt_edge.jobs import setup_logging
from tt_edge.model import features as feats
from tt_edge.model.calibration import CalibrationTransform
from tt_edge.model.win_prob import blend_probability
from tt_edge.scrapers import sofascore
from tt_edge.scrapers.board import BoardSnapshot, parse_board_payload
from tt_edge.scrapers.events import ParsedEvent
from tt_edge.scrapers.form import FormSnapshot, parse_form_payload
from tt_edge.scrapers.h2h import H2HSnapshot, parse_h2h_payload

logger = logging.getLogger(__name__)

MARKET_ML = "ML"


@dataclass(frozen=True)
class ScanData:
    """The envelopes one scan consumes."""

    board: sofascore.Envelope
    h2h: dict[str, sofascore.Envelope]     # by match source event id
    form: dict[str, sofascore.Envelope]    # by player source id


class ScanDataError(ValueError):
    """The data directory does not contain a usable input set."""


def load_scan_data(data_dir: Path) -> ScanData:
    """Load every envelope in ``data_dir``. Exactly one board envelope is
    required; unreadable files fail loudly (bad provenance must never be
    silently analyzed)."""
    boards: list[sofascore.Envelope] = []
    h2h: dict[str, sofascore.Envelope] = {}
    form: dict[str, sofascore.Envelope] = {}
    for path in sorted(data_dir.glob("*.json")):
        envelope = sofascore.load_envelope(path)
        if envelope.kind == sofascore.BOARD:
            boards.append(envelope)
        elif envelope.kind == sofascore.H2H:
            h2h[envelope.entity_id] = envelope
        elif envelope.kind == sofascore.FORM:
            form[envelope.entity_id] = envelope
    if len(boards) != 1:
        raise ScanDataError(
            f"{data_dir}: need exactly one board envelope, found {len(boards)}")
    return ScanData(board=boards[0], h2h=h2h, form=form)


@dataclass(frozen=True)
class MatchOutcome:
    """One match's scan result (also the test observation surface)."""

    match_source_id: str
    home_name: str
    away_name: str
    status: str                    # RECOMMEND | NO_BET
    reasons: tuple[str, ...]
    side: str | None
    edge: Decimal | None
    stake_cents: int | None
    alert_text: str | None
    alert_sent: bool
    alert_delivered: bool


def _pick_perspective_basis(decision_side: str, h2h: feats.H2HFeature | None,
                            form_home: feats.FormFeature,
                            form_away: feats.FormFeature,
                            common: feats.CommonOpponentFeature) -> dict:
    """Basis stats flipped to the PICK's perspective for display/storage."""
    if h2h is None:
        h2h_w = h2h_l = l20_w = l20_l = 0
    elif decision_side == "home":
        h2h_w, h2h_l = h2h.career_wins, h2h.career_losses
        l20_w, l20_l = h2h.last20_wins, h2h.last20_losses
    else:
        h2h_w, h2h_l = h2h.career_losses, h2h.career_wins
        l20_w, l20_l = h2h.last20_losses, h2h.last20_wins
    form_diff = feats.form_differential(form_home, form_away)
    common_net = common.net_units
    if decision_side == "away":
        form_diff = -form_diff
        common_net = -common_net
    return {"h2h_w": h2h_w, "h2h_l": h2h_l, "l20_w": l20_w, "l20_l": l20_l,
            "form_diff": round(form_diff, 4), "common_opps": common.n_common,
            "common_net": common_net}


def _alert_context(event: ParsedEvent, pick_name: str, side: str,
                   decision: edge_calc.Decision, basis: dict,
                   stake_cents: int, bankroll_cents: int,
                   config: TTEdgeConfig, ages: dict) -> AlertContext:
    return AlertContext(
        home_name=event.home_name, away_name=event.away_name,
        pick_name=pick_name, start_time=event.start_time, market=MARKET_ML,
        price_american=decision.price_american, fair_p=decision.fair_p,
        model_p=decision.model_p, edge=decision.edge,
        h2h_wins=basis["h2h_w"], h2h_losses=basis["h2h_l"],
        l20_wins=basis["l20_w"], l20_losses=basis["l20_l"],
        form_diff=basis["form_diff"], common_opps=basis["common_opps"],
        common_net=basis["common_net"], stake_cents=stake_cents,
        kelly_multiplier=config.kelly_fraction, bankroll_cents=bankroll_cents,
        board_age_s=ages["board"], h2h_age_s=ages.get("h2h"),
        odds_age_s=ages["odds"])


def run_scan(*, repo: repo_mod.TTEdgeRepo, config: TTEdgeConfig,
             data: ScanData, now: datetime, sender: TTEdgeTelegram | None,
             dry_run: bool) -> list[MatchOutcome]:
    """Analyze every board match. Pure orchestration — all decisions come
    from edge_calc; all persistence goes through the repo."""
    board: BoardSnapshot = parse_board_payload(
        data.board.payload, fetched_at=data.board.fetched_at,
        tournament_keyword=config.tournament_keyword)
    board_check = freshness.check("board", board.fetched_at, now,
                                  config.max_board_age_s)
    transform = repo.active_calibration() or CalibrationTransform(0.0, 1.0)
    outcomes: list[MatchOutcome] = []

    logger.info("scan: %d board match(es), board_age=%.0fs stale=%s",
                len(board.matches), board_check.age_seconds,
                not board_check.ok)

    for event in board.matches:
        outcomes.append(_scan_match(
            repo=repo, config=config, event=event, data=data,
            board_check=board_check, transform=transform, now=now,
            sender=sender, dry_run=dry_run))
    return outcomes


def _scan_match(*, repo: repo_mod.TTEdgeRepo, config: TTEdgeConfig,
                event: ParsedEvent, data: ScanData,
                board_check: freshness.FreshnessCheck,
                transform: CalibrationTransform, now: datetime,
                sender: TTEdgeTelegram | None, dry_run: bool) -> MatchOutcome:
    match_id = event.source_event_id
    # The idempotency day is the MATCH's day, not the scan's: a rescan just
    # after UTC midnight must hit the same claim as the one just before.
    day_key = event.start_time.astimezone(timezone.utc).date().isoformat()
    repo.upsert_player(event.home_id, event.home_name, now)
    repo.upsert_player(event.away_id, event.away_name, now)
    repo.upsert_match(event, now)

    # A started (or earlier) match is not bettable pre-match, whatever the
    # board's freshness says — a 29-minute-old board passes the guard while
    # its matches may already be mid-play.
    if event.start_time <= now:
        existing = repo.recommendation_by_key(match_id, MARKET_ML, day_key)
        if existing is not None and existing["status"] == "open" and \
                existing["alert_delivered_at"] is None:
            logger.warning("match %s: claimed recommendation expired "
                           "undelivered (match started)", match_id)
        logger.info("match %s: NO_BET match_started", match_id)
        return MatchOutcome(match_id, event.home_name, event.away_name,
                            edge_calc.NO_BET, ("match_started",), None, None,
                            None, None, False, False)

    # Claim check FIRST: an undelivered claimed alert is re-sent regardless
    # of what a re-evaluation would say now (the claim is the commitment);
    # a delivered claim ends the match's scan immediately.
    existing = repo.recommendation_by_key(match_id, MARKET_ML, day_key)
    if existing is not None:
        if existing["status"] != "open" or \
                existing["alert_delivered_at"] is not None:
            logger.info("match %s: already claimed for %s; skip",
                        match_id, day_key)
            return MatchOutcome(match_id, event.home_name, event.away_name,
                                edge_calc.RECOMMEND, ("duplicate_scan",),
                                existing["side"], Decimal(existing["edge"]),
                                int(existing["stake_cents"]), None, False,
                                False)
        return _resend_claimed(repo=repo, config=config, event=event,
                               data=data, board_check=board_check,
                               existing=existing, now=now, sender=sender,
                               dry_run=dry_run)

    checks = [board_check]
    ages: dict = {"board": board_check.age_seconds}

    # H2H (optional — its absence is handled by the insufficient-data guard).
    h2h_env = data.h2h.get(match_id)
    h2h_snapshot: H2HSnapshot | None = None
    h2h_feature: feats.H2HFeature | None = None
    if h2h_env is not None:
        repo.record_h2h_snapshot(match_id, json.dumps(h2h_env.payload),
                                 h2h_env.fetched_at)
        h2h_snapshot = parse_h2h_payload(
            h2h_env.payload, home_id=event.home_id, away_id=event.away_id,
            fetched_at=h2h_env.fetched_at)
        h2h_check = freshness.check("h2h", h2h_snapshot.fetched_at, now,
                                    config.max_h2h_age_s)
        checks.append(h2h_check)
        ages["h2h"] = h2h_check.age_seconds
        h2h_feature = feats.h2h_feature(
            list(h2h_snapshot.meetings), event.home_id, now,
            half_life_days=config.h2h_half_life_days)

    # Form (optional per player, same reasoning).
    def _form(player_id: str) -> FormSnapshot | None:
        env = data.form.get(player_id)
        if env is None:
            return None
        repo.record_form_snapshot(player_id, json.dumps(env.payload),
                                  env.fetched_at)
        snapshot = parse_form_payload(env.payload, player_id=player_id,
                                      fetched_at=env.fetched_at)
        checks.append(freshness.check("form", snapshot.fetched_at, now,
                                      config.max_form_age_s))
        return snapshot

    form_home_snap = _form(event.home_id)
    form_away_snap = _form(event.away_id)
    home_results = list(form_home_snap.results) if form_home_snap else []
    away_results = list(form_away_snap.results) if form_away_snap else []
    form_home = feats.form_feature(home_results, event.home_id, now,
                                   last_n=config.form_last_n,
                                   hot_n=config.form_hot_n)
    form_away = feats.form_feature(away_results, event.away_id, now,
                                   last_n=config.form_last_n,
                                   hot_n=config.form_hot_n)
    common = feats.common_opponent_feature(
        home_results, away_results, event.home_id, event.away_id, now,
        window_days=config.common_opp_window_days)

    # Model probability (raw blend -> active calibration -> Decimal).
    blend = blend_probability(h2h_feature, form_home, form_away, common,
                              w_h2h=config.w_h2h, w_form=config.w_form,
                              w_common=config.w_common,
                              scale=config.blend_scale)
    p_home = transform.apply(blend.p_home)
    model_p_home = Decimal(str(round(p_home, 6)))

    # Odds: manual snapshots from the DB, ONLY the configured book (mixing
    # books would fabricate line movement). No odds -> reasoned NO BET (the
    # prediction row is still written for the calibration corpus).
    history = repo.odds_history(match_id, config.book)
    h2h_n = h2h_feature.n_meetings if h2h_feature else 0
    if not history:
        reasons = edge_calc.dedupe_reasons(
            [c.reason for c in checks if not c.ok] + ["no_odds"])
        _write_prediction(repo, match_id, day_key, model_p_home, None,
                          edge_calc.NO_BET, reasons, now)
        logger.info("match %s: NO_BET %s", match_id, ",".join(reasons))
        return MatchOutcome(match_id, event.home_name, event.away_name,
                            edge_calc.NO_BET, reasons, None, None, None,
                            None, False, False)

    latest = history[-1]
    earliest = history[0]
    odds_check = freshness.check("odds", latest.captured_at, now,
                                 config.max_odds_age_s)
    checks.append(odds_check)
    ages["odds"] = odds_check.age_seconds

    favored_home = model_p_home >= Decimal("0.5")
    movement = None
    if len(history) > 1:
        movement = (movement_cents(earliest.home_price, latest.home_price)
                    if favored_home else
                    movement_cents(earliest.away_price, latest.away_price))

    market = edge_calc.MarketView(home_price=latest.home_price,
                                  away_price=latest.away_price,
                                  favored_side_movement_cents=movement)
    decision = edge_calc.evaluate(
        model_p_home=model_p_home, market=market, freshness_checks=checks,
        h2h_meetings=h2h_n, common_opponents=common.n_common,
        open_recommendations=repo.open_recommendation_count(),
        bankroll_cents=repo.get_bankroll_cents(), config=config)

    fair_p_home = (decision.fair_p if decision.side == "home"
                   else Decimal(1) - decision.fair_p)
    _write_prediction(repo, match_id, day_key, model_p_home,
                      fair_p_home, decision.status, decision.reasons, now)

    if not decision.is_recommendation:
        logger.info("match %s: NO_BET %s (edge=%s)", match_id,
                    ",".join(decision.reasons), decision.edge)
        return MatchOutcome(match_id, event.home_name, event.away_name,
                            decision.status, decision.reasons, decision.side,
                            decision.edge, None, None, False, False)

    # ── recommendation: claim row -> send -> mark delivered ──────────────
    pick_name = (event.home_name if decision.side == "home"
                 else event.away_name)
    basis = _pick_perspective_basis(decision.side, h2h_feature, form_home,
                                    form_away, common)
    bankroll_cents = repo.get_bankroll_cents()
    stake_cents = decision.stake.stake_cents
    rec_id = repo.insert_recommendation(
        match_source_id=match_id, market=MARKET_ML, side=decision.side,
        pick_name=pick_name, model_p=str(decision.model_p),
        fair_p=str(decision.fair_p), edge=str(decision.edge),
        price_american=decision.price_american, stake_cents=stake_cents,
        bankroll_cents=bankroll_cents, recommended_at=now,
        recommended_on=day_key, basis_json=json.dumps(basis),
        data_ages_json=json.dumps({k: round(v, 1) for k, v in ages.items()
                                   if v is not None}))

    if rec_id is None:
        # Defensive: the claim was checked at the top of this function, so
        # losing it here means a concurrent scanner won the race. Skip —
        # that scanner owns the send.
        logger.info("match %s: lost claim race for %s; skip", match_id,
                    day_key)
        return MatchOutcome(match_id, event.home_name, event.away_name,
                            edge_calc.RECOMMEND, ("duplicate_scan",),
                            decision.side, decision.edge, stake_cents, None,
                            False, False)

    text = build_alert(_alert_context(event, pick_name, decision.side,
                                      decision, basis, stake_cents,
                                      bankroll_cents, config, ages))
    sent = delivered = False
    if dry_run:
        logger.info("DRY RUN alert for match %s:\n%s", match_id, text)
    elif sender is not None:
        result = sender.send(text)
        sent = not result["muted"]
        delivered = bool(result["ok"])
        if delivered:
            repo.mark_alert_delivered(rec_id, now)
        elif sent:
            logger.error("match %s: alert send failed (%s); will retry on "
                         "next scan", match_id, result["error"])
    logger.info("match %s: RECOMMEND %s %s stake=%dc edge=%s delivered=%s",
                match_id, pick_name, decision.price_american, stake_cents,
                decision.edge, delivered)
    return MatchOutcome(match_id, event.home_name, event.away_name,
                        edge_calc.RECOMMEND, (), decision.side, decision.edge,
                        stake_cents, text, sent, delivered)


def _resend_claimed(*, repo: repo_mod.TTEdgeRepo, config: TTEdgeConfig,
                    event: ParsedEvent, data: ScanData,
                    board_check: freshness.FreshnessCheck, existing: dict,
                    now: datetime, sender: TTEdgeTelegram | None,
                    dry_run: bool) -> MatchOutcome:
    """Re-send a claimed-but-undelivered recommendation VERBATIM (the stored
    numbers are the commitment; only the data ages are refreshed). Runs
    before re-evaluation on purpose: odds gone stale since the claim must
    not strand an alert the operator never received."""
    match_id = event.source_event_id
    h2h_env = data.h2h.get(match_id)
    history = repo.odds_history(match_id, config.book)
    basis = json.loads(existing["basis"])
    text = build_alert(AlertContext(
        home_name=event.home_name, away_name=event.away_name,
        pick_name=existing["pick_name"], start_time=event.start_time,
        market=MARKET_ML, price_american=int(existing["price_american"]),
        fair_p=Decimal(existing["fair_p"]), model_p=Decimal(existing["model_p"]),
        edge=Decimal(existing["edge"]), h2h_wins=basis["h2h_w"],
        h2h_losses=basis["h2h_l"], l20_wins=basis["l20_w"],
        l20_losses=basis["l20_l"], form_diff=basis["form_diff"],
        common_opps=basis["common_opps"], common_net=basis["common_net"],
        stake_cents=int(existing["stake_cents"]),
        kelly_multiplier=config.kelly_fraction,
        bankroll_cents=int(existing["bankroll_cents"]),
        board_age_s=board_check.age_seconds,
        h2h_age_s=(freshness.age_seconds(h2h_env.fetched_at, now)
                   if h2h_env is not None else None),
        odds_age_s=(freshness.age_seconds(history[-1].captured_at, now)
                    if history else None)))
    sent = delivered = False
    if dry_run:
        logger.info("DRY RUN resend for match %s:\n%s", match_id, text)
    elif sender is not None:
        result = sender.send(text)
        sent = not result["muted"]
        delivered = bool(result["ok"])
        if delivered:
            repo.mark_alert_delivered(int(existing["id"]), now)
        elif sent:
            logger.error("match %s: alert resend failed (%s); will retry on "
                         "next scan", match_id, result["error"])
    logger.info("match %s: RESEND %s delivered=%s", match_id,
                existing["pick_name"], delivered)
    return MatchOutcome(match_id, event.home_name, event.away_name,
                        edge_calc.RECOMMEND, ("resend_undelivered",),
                        existing["side"], Decimal(existing["edge"]),
                        int(existing["stake_cents"]), text, sent, delivered)


def _write_prediction(repo: repo_mod.TTEdgeRepo, match_id: str, day_key: str,
                      model_p_home: Decimal, fair_p_home: Decimal | None,
                      decision: str, reasons: tuple[str, ...],
                      now: datetime) -> None:
    repo.insert_prediction(
        match_source_id=match_id, market=MARKET_ML, predicted_on=day_key,
        model_p_home=str(model_p_home),
        fair_p_home=None if fair_p_home is None else str(fair_p_home),
        decision=decision, reasons_json=json.dumps(list(reasons)),
        created_at=now)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tt_edge.jobs.scan",
        description="Scan the TT Elite board and alert on qualifying edges.")
    parser.add_argument("--data-dir", type=Path, required=True,
                        help="directory of sofascore envelope files")
    parser.add_argument("--live-url", action="append", default=[],
                        help="fetch this sofascore page into --data-dir first "
                             "(repeatable; requires playwright)")
    parser.add_argument("--db", help="override TT_EDGE_DATABASE_URL")
    parser.add_argument("--dry-run", action="store_true",
                        help="print alerts instead of sending Telegram")
    args = parser.parse_args(argv)

    setup_logging()
    config = load_config()
    for url in args.live_url:
        for envelope in sofascore.fetch_payloads(url):
            sofascore.save_envelope(envelope, args.data_dir)

    data = load_scan_data(args.data_dir)
    repo = repo_mod.connect(args.db or config.database_url)
    try:
        repo.apply_migrations()
        sender = None if args.dry_run else TTEdgeTelegram.from_config(config)
        outcomes = run_scan(repo=repo, config=config, data=data,
                            now=datetime.now(timezone.utc), sender=sender,
                            dry_run=args.dry_run)
    finally:
        repo.close()

    recommended = sum(1 for o in outcomes if o.status == edge_calc.RECOMMEND)
    print(f"scanned {len(outcomes)} match(es): {recommended} recommended, "
          f"{len(outcomes) - recommended} no-bet")
    for outcome in outcomes:
        label = (f"stake {outcome.stake_cents}c" if outcome.stake_cents
                 else ",".join(outcome.reasons) or "-")
        print(f"  {outcome.match_source_id} {outcome.home_name} vs "
              f"{outcome.away_name}: {outcome.status} ({label})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
