"""A player's recent results from a sofascore team-events payload
(``/api/v1/team/{id}/events/last/0`` — in table tennis a "team" is the
player)."""
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
class FormSnapshot:
    """Decided recent results involving ``player_id``."""

    player_id: str
    results: tuple[MatchResult, ...]
    fetched_at: datetime          # aware UTC
    skipped_events: int


def parse_form_payload(payload: Any, *, player_id: str,
                       fetched_at: datetime) -> FormSnapshot:
    """Extract the player's decided matches from their recent-events payload.

    Events not involving the player (defensive — shouldn't happen on this
    endpoint) and undecided events are dropped; malformed events are skipped
    and counted; a payload without ``events`` raises ParseError.
    """
    fetched_at = require_aware("fetched_at", fetched_at)
    results: list[MatchResult] = []
    skipped = 0
    for raw in event_list(payload):
        try:
            event = parse_event(raw)
        except ValueError as exc:
            skipped += 1
            logger.warning("form %s: skipped malformed event: %s", player_id, exc)
            continue
        if player_id not in (event.home_id, event.away_id):
            continue
        result = finished_match_result(event)
        if result is not None:
            results.append(result)
    return FormSnapshot(player_id=player_id, results=tuple(results),
                        fetched_at=fetched_at, skipped_events=skipped)
