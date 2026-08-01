"""Odds types and CLI-string parsing.

Phase 0 odds source is MANUAL entry (``jobs/odds_entry.py``) — the operator
types the prices posted at the book actually being bet, which are the only
prices that matter. The snapshot shape is already the Phase 1 shape, so a
Playwright/BetsAPI odds scraper later just becomes another producer of
:class:`OddsSnapshot` rows; nothing downstream changes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tt_edge.edge.vig import OddsError, validate_american
from tt_edge.freshness import require_aware


@dataclass(frozen=True)
class OddsSnapshot:
    """One two-way price observation for a match at a book. Append-only in
    the DB (keyed on match/book/captured_at) — the history enables the
    line-movement fix-risk guard now and CLV analysis later."""

    match_source_id: str
    book: str
    home_price: int           # American
    away_price: int           # American
    captured_at: datetime     # aware UTC

    def __post_init__(self) -> None:
        validate_american(self.home_price)
        validate_american(self.away_price)
        require_aware("captured_at", self.captured_at)
        if not self.match_source_id:
            raise ValueError("match_source_id must be non-empty")
        if not self.book:
            raise ValueError("book must be non-empty")


def parse_american_str(text: str) -> int:
    """Parse an operator-typed American price: '-120', '+100', '105'.

    A bare positive number is treated as underdog-positive ('105' -> +105);
    the sign is required only for clarity, not by the parser.
    """
    cleaned = text.strip().replace(" ", "")
    if not cleaned:
        raise OddsError("empty price")
    try:
        price = int(cleaned)
    except ValueError as exc:
        raise OddsError(f"not an American price: {text!r}") from exc
    return validate_american(price)
