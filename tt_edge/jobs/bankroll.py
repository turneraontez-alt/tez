"""Seed or inspect the bankroll (integer cents in the DB).

Staking reads this value; ``grade.py`` moves it on settlement. It is never a
constant in code — a fresh install must run ``--set`` once before the scan
will recommend anything (until then every pick abstains with
``no_bankroll_set``).

Usage::

    python3 -m tt_edge.jobs.bankroll --set 65.00
    python3 -m tt_edge.jobs.bankroll --show
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from tt_edge.config import load_config
from tt_edge.db import repo as repo_mod
from tt_edge.jobs import setup_logging


def dollars_to_cents(text: str) -> int:
    try:
        value = Decimal(text.strip().lstrip("$"))
    except InvalidOperation as exc:
        raise ValueError(f"not a dollar amount: {text!r}") from exc
    if value < 0:
        raise ValueError("bankroll cannot be negative")
    cents = value * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"sub-cent amounts not allowed: {text!r}")
    return int(cents)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tt_edge.jobs.bankroll",
        description="Seed or inspect the TT-Edge bankroll.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--set", dest="set_amount", metavar="DOLLARS",
                       help="set the bankroll, e.g. 65.00")
    group.add_argument("--show", action="store_true")
    parser.add_argument("--db", help="override TT_EDGE_DATABASE_URL")
    args = parser.parse_args(argv)

    setup_logging()
    config = load_config()
    repo = repo_mod.connect(args.db or config.database_url)
    try:
        repo.apply_migrations()
        if args.set_amount:
            cents = dollars_to_cents(args.set_amount)
            repo.set_bankroll_cents(cents, datetime.now(timezone.utc))
            print(f"bankroll set to ${cents / 100:.2f}")
        else:
            cents = repo.get_bankroll_cents()
            if cents is None:
                print("bankroll NOT SET — run --set <dollars> first")
                return 1
            print(f"bankroll: ${cents / 100:.2f}")
    finally:
        repo.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
