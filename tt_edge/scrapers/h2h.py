"""Head-to-head history between two players from a sofascore H2H payload
(``/api/v1/event/{id}/h2h/events``)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tt_edge.freshness import require_aware
from tt_edge.model.features import MatchResult
from tt_edge.scrapers.events import event_list, finished_match_result, parse_event

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class H2HSnapshot:
    """All decided meetings between the pair, newest data first not required
    (features sort for themselves)."""

    home_id: str
    away_id: str
    meetings: tuple[MatchResult, ...]
    fetched_at: datetime          # aware UTC
    skipped_events: int


def parse_h2h_payload(payload: Any, *, home_id: str, away_id: str,
                      fetched_at: datetime) -> H2HSnapshot:
    """Extract decided meetings between exactly this pair.

    Events involving anyone else (sofascore occasionally mixes team events
    into duel lists) and undecided/in-progress events are dropped. Malformed
    events are skipped and counted; a payload without an ``events`` array
    raises ParseError.
    """
    fetched_at = require_aware("fetched_at", fetched_at)
    pair = {home_id, away_id}
    meetings: list[MatchResult] = []
    skipped = 0
    for raw in event_list(payload):
        try:
            event = parse_event(raw)
        except ValueError as exc:
            skipped += 1
            logger.warning("h2h %s-%s: skipped malformed event: %s",
                           home_id, away_id, exc)
            continue
        if {event.home_id, event.away_id} != pair:
            continue
        result = finished_match_result(event)
        if result is not None:
            meetings.append(result)
    return H2HSnapshot(home_id=home_id, away_id=away_id,
                       meetings=tuple(meetings), fetched_at=fetched_at,
                       skipped_events=skipped)
