"""Today's TT Elite match board from a sofascore scheduled-events payload."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tt_edge.freshness import require_aware
from tt_edge.scrapers.events import ParsedEvent, event_list, parse_event

logger = logging.getLogger(__name__)

# Board statuses worth analyzing: not started (bettable) is the target;
# postponed/cancelled/finished are excluded.
_ANALYZABLE_STATUSES = ("notstarted", "")


@dataclass(frozen=True)
class BoardSnapshot:
    """The scraped board: upcoming TT Elite matches plus provenance."""

    matches: tuple[ParsedEvent, ...]
    fetched_at: datetime          # aware UTC
    source: str
    skipped_events: int           # malformed entries dropped by the parser


def parse_board_payload(payload: Any, *, fetched_at: datetime,
                        tournament_keyword: str,
                        source: str = "sofascore") -> BoardSnapshot:
    """Filter a scheduled-events payload down to upcoming TT Elite matches.

    ``tournament_keyword`` is matched case-insensitively against the event's
    tournament name (sofascore lists many leagues on one date endpoint).
    Malformed events are skipped and counted, never fatal; a payload without
    an ``events`` array raises ParseError.
    """
    fetched_at = require_aware("fetched_at", fetched_at)
    keyword = tournament_keyword.strip().lower()
    matches: list[ParsedEvent] = []
    skipped = 0
    for raw in event_list(payload):
        try:
            event = parse_event(raw)
        except ValueError as exc:
            skipped += 1
            logger.warning("board: skipped malformed event: %s", exc)
            continue
        if keyword and keyword not in event.tournament_name.lower():
            continue
        if event.status_type not in _ANALYZABLE_STATUSES:
            continue
        matches.append(event)
    matches.sort(key=lambda event: (event.start_time, event.source_event_id))
    return BoardSnapshot(matches=tuple(matches), fetched_at=fetched_at,
                         source=source, skipped_events=skipped)
