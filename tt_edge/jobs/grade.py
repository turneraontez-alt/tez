"""Post-session grading — where the real value is.

Records official results, settles open recommendations exactly once, moves
the bankroll (the DB value staking reads — never a constant), grades every
prediction into the calibration corpus, and (on request) fits or promotes a
calibration version. Fitting stores the version INACTIVE with its evidence;
promotion is a separate manual flag, mirroring the Q15 champion discipline.

Settlement math (Decimal; conservative floor rounding):

* won  -> profit = stake * (decimal_odds - 1)
* lost -> profit = -stake
* void -> profit = 0 (handicap pushes later; ML voids = walkovers), and the
  match's predictions are NOT graded (a walkover says nothing about the
  model).

Policy: EVERY open recommendation settles, delivered or not — the bankroll
is a PAPER ledger of what the system recommended (dry-run and muted rows
included) until the operator is live and keeps it synced to reality by hand.

Usage::

    python3 -m tt_edge.jobs.grade --result 12345=home --result 12346=away
    python3 -m tt_edge.jobs.grade --results-file results.json
    python3 -m tt_edge.jobs.grade --fit-calibration
    python3 -m tt_edge.jobs.grade --activate-calibration 2
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

from tt_edge.config import load_config
from tt_edge.db import repo as repo_mod
from tt_edge.edge.vig import american_to_decimal_odds
from tt_edge.jobs import setup_logging
from tt_edge.model.calibration import CalibrationError, fit_platt

logger = logging.getLogger(__name__)

_ONE = Decimal(1)


@dataclass(frozen=True)
class ResultInput:
    match_source_id: str
    winner_side: str              # 'home' | 'away' | 'void'
    home_score: int | None = None
    away_score: int | None = None


class ResultInputError(ValueError):
    """A result argument/file entry is malformed."""


def parse_result_arg(text: str) -> ResultInput:
    """'12345=home' / '12345=away' / '12345=void'."""
    match_id, sep, winner = text.partition("=")
    if not sep or not match_id.strip() or winner not in ("home", "away", "void"):
        raise ResultInputError(
            f"expected '<match_source_id>=home|away|void'; got {text!r}")
    return ResultInput(match_source_id=match_id.strip(), winner_side=winner)


def load_results_file(path: Path) -> list[ResultInput]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ResultInputError(f"{path}: not valid JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise ResultInputError(f"{path}: expected a JSON array of results")
    results = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict) or \
                entry.get("winner") not in ("home", "away", "void") or \
                not entry.get("match_source_id"):
            raise ResultInputError(
                f"{path}[{index}]: need match_source_id and "
                "winner=home|away|void")
        results.append(ResultInput(
            match_source_id=str(entry["match_source_id"]),
            winner_side=entry["winner"],
            home_score=entry.get("home_score"),
            away_score=entry.get("away_score")))
    return results


def settlement_profit_cents(status: str, stake_cents: int,
                            price_american: int) -> int:
    """The ONLY place settlement P&L is computed. Floor rounding — winners
    never round up."""
    if status == "won":
        stake = Decimal(stake_cents)
        profit = stake * (american_to_decimal_odds(price_american) - _ONE)
        return int(profit.to_integral_value(rounding=ROUND_FLOOR))
    if status == "lost":
        return -stake_cents
    if status == "void":
        return 0
    raise ValueError(f"unknown settlement status {status!r}")


@dataclass(frozen=True)
class GradeReport:
    results_recorded: int
    recommendations_settled: int
    predictions_graded: int
    profit_cents: int
    bankroll_before_cents: int | None
    bankroll_after_cents: int | None


def run_grade(*, repo: repo_mod.TTEdgeRepo, results: list[ResultInput],
              now: datetime, source: str = "manual") -> GradeReport:
    """Record results, settle, move the bankroll, grade predictions."""
    recorded = 0
    for result in results:
        if repo.record_result(match_source_id=result.match_source_id,
                              winner_side=result.winner_side,
                              home_score=result.home_score,
                              away_score=result.away_score,
                              source=source, recorded_at=now):
            recorded += 1
        else:
            logger.info("result for match %s already recorded; keeping first",
                        result.match_source_id)

    bankroll_before = repo.get_bankroll_cents()
    planned: list[tuple[int, str, int]] = []
    for row in repo.open_recommendations_with_results():
        winner = row["winner_side"]
        if winner == "void":
            status = "void"
        elif winner == row["side"]:
            status = "won"
        else:
            status = "lost"
        profit = settlement_profit_cents(status, int(row["stake_cents"]),
                                         int(row["price_american"]))
        planned.append((int(row["id"]), status, profit))
        logger.info("rec %s (match %s): settling %s %+dc", row["id"],
                    row["match_source_id"], status, profit)

    # One transaction for settlements + bankroll: a crash cannot apply P&L
    # without its settlements or settle without moving the roll (the
    # settle-once guard would make that divergence permanent on rerun).
    settled_ids, total_profit, new_bankroll = repo.settle_batch(
        planned, now, move_bankroll=bankroll_before is not None)
    settled = len(settled_ids)
    bankroll_after = (new_bankroll if new_bankroll is not None
                      else bankroll_before)
    if bankroll_before is not None and new_bankroll == 0 and \
            bankroll_before + total_profit < 0:
        # Stakes are capped at 5% of roll, so hitting the zero clamp needs an
        # out-of-band bankroll edit; say so loudly.
        logger.warning("settlement drove bankroll below zero; clamped to 0")

    graded = 0
    for row in repo.ungraded_predictions_with_results():
        if row["winner_side"] == "void":
            continue
        outcome_home = 1 if row["winner_side"] == "home" else 0
        repo.grade_prediction(int(row["id"]), outcome_home, now)
        graded += 1

    return GradeReport(results_recorded=recorded,
                       recommendations_settled=settled,
                       predictions_graded=graded, profit_cents=total_profit,
                       bankroll_before_cents=bankroll_before,
                       bankroll_after_cents=bankroll_after)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tt_edge.jobs.grade",
        description="Grade results, settle recommendations, update bankroll.")
    parser.add_argument("--result", action="append", default=[],
                        help="'<match_source_id>=home|away|void' (repeatable)")
    parser.add_argument("--results-file", type=Path,
                        help="JSON array: [{match_source_id, winner, ...}]")
    parser.add_argument("--source", default="manual",
                        help="result provenance label (default: manual)")
    parser.add_argument("--fit-calibration", action="store_true",
                        help="fit a new (INACTIVE) Platt version from graded "
                             "predictions")
    parser.add_argument("--activate-calibration", type=int, metavar="VERSION",
                        help="promote a stored calibration version (manual "
                             "significance check first)")
    parser.add_argument("--db", help="override TT_EDGE_DATABASE_URL")
    args = parser.parse_args(argv)

    setup_logging()
    config = load_config()
    results = [parse_result_arg(text) for text in args.result]
    if args.results_file:
        results.extend(load_results_file(args.results_file))

    repo = repo_mod.connect(args.db or config.database_url)
    try:
        repo.apply_migrations()
        now = datetime.now(timezone.utc)
        report = run_grade(repo=repo, results=results, now=now,
                           source=args.source)
        print(f"results recorded: {report.results_recorded}; "
              f"recommendations settled: {report.recommendations_settled} "
              f"({report.profit_cents:+d}c); "
              f"predictions graded: {report.predictions_graded}")
        if report.bankroll_after_cents is not None:
            print(f"bankroll: {report.bankroll_before_cents}c -> "
                  f"{report.bankroll_after_cents}c")
        elif report.recommendations_settled:
            print("bankroll: NOT SET — seed it with "
                  "`python3 -m tt_edge.jobs.bankroll --set <dollars>`")

        if args.fit_calibration:
            pairs = repo.graded_prediction_pairs()
            try:
                fit = fit_platt(pairs, min_rows=config.calibration_min_rows)
            except CalibrationError as exc:
                print(f"calibration: {exc}")
                return 1
            version = repo.insert_calibration_version(
                fit, method="platt", fitted_at=now)
            print(f"calibration v{version} stored INACTIVE: "
                  f"intercept={fit.transform.intercept:.4f} "
                  f"slope={fit.transform.slope:.4f} n={fit.n_rows} "
                  f"brier {fit.brier_identity:.5f} -> {fit.brier_fitted:.5f} "
                  f"converged={fit.converged}; activate with "
                  f"--activate-calibration {version}")
        if args.activate_calibration is not None:
            repo.activate_calibration(args.activate_calibration)
            print(f"calibration v{args.activate_calibration} ACTIVE")
    finally:
        repo.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
