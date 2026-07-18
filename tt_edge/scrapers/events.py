"""Shared parsing of sofascore event objects.

The undocumented sofascore JSON API (``api.sofascore.com/api/v1/...``)
represents matches the same way everywhere — scheduled board events, H2H
duel lists, and a player's recent events all share this shape — so the
board/h2h/form parsers delegate the per-event work here.

Field notes (observed shape, validated defensively because it is
undocumented): ``homeTeam``/``awayTeam`` carry ``id`` and ``name``;
``startTimestamp`` is epoch seconds UTC (normalized here — TT Elite displays
CET locally, we never parse local strings); ``status.type`` is
``notstarted`` / ``inprogress`` / ``finished`` / etc.; ``winnerCode`` on
finished events is 1 (home) or 2 (away).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from tt_edge.model.features import MatchResult


class ParseError(ValueError):
    """The top-level payload does not have the expected shape."""


@dataclass(frozen=True)
class ParsedEvent:
    """One sofascore event, normalized."""

    source_event_id: str
    home_id: str
    home_name: str
    away_id: str
    away_name: str
    start_time: datetime          # aware UTC
    status_type: str
    winner_code: int | None       # 1 home, 2 away, None if absent/unfinished
    tournament_name: str


def _team(raw: Any, side: str) -> tuple[str, str]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{side} team missing")
    team_id = raw.get("id")
    name = raw.get("name")
    if team_id is None or not isinstance(name, str) or not name.strip():
        raise ValueError(f"{side} team lacks id/name")
    return str(team_id), name.strip()


def parse_event(raw: Any) -> ParsedEvent:
    """Normalize one event object. Raises ValueError on any malformed field
    (callers skip-and-count individual failures)."""
    if not isinstance(raw, Mapping):
        raise ValueError("event is not an object")
    event_id = raw.get("id")
    if event_id is None:
        raise ValueError("event lacks id")
    home_id, home_name = _team(raw.get("homeTeam"), "home")
    away_id, away_name = _team(raw.get("awayTeam"), "away")
    start_raw = raw.get("startTimestamp")
    if not isinstance(start_raw, (int, float)) or isinstance(start_raw, bool):
        raise ValueError("event lacks startTimestamp")
    start_time = datetime.fromtimestamp(float(start_raw), tz=timezone.utc)
    status = raw.get("status")
    status_type = ""
    if isinstance(status, Mapping):
        value = status.get("type")
        if isinstance(value, str):
            status_type = value
    winner_raw = raw.get("winnerCode")
    winner_code = winner_raw if winner_raw in (1, 2) else None
    tournament = raw.get("tournament")
    tournament_name = ""
    if isinstance(tournament, Mapping):
        value = tournament.get("name")
        if isinstance(value, str):
            tournament_name = value
    return ParsedEvent(source_event_id=str(event_id), home_id=home_id,
                       home_name=home_name, away_id=away_id,
                       away_name=away_name, start_time=start_time,
                       status_type=status_type, winner_code=winner_code,
                       tournament_name=tournament_name)


def finished_match_result(event: ParsedEvent) -> MatchResult | None:
    """A finished event with a decisive winner as a model MatchResult;
    None otherwise (in-progress, walkovers without winnerCode, ...)."""
    if event.status_type != "finished" or event.winner_code is None:
        return None
    winner_id = event.home_id if event.winner_code == 1 else event.away_id
    return MatchResult(start_time=event.start_time, winner_id=winner_id,
                       home_id=event.home_id, away_id=event.away_id)


def event_list(payload: Any) -> list[Any]:
    """Extract the ``events`` array from a sofascore payload or raise
    ParseError."""
    if not isinstance(payload, Mapping):
        raise ParseError("payload is not a JSON object")
    events = payload.get("events")
    if not isinstance(events, list):
        raise ParseError("payload lacks an 'events' array")
    return events
