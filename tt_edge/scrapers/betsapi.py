"""BetsAPI (b365api.com) — the cloud-native data source.

Sofascore blocks datacenter traffic, so cloud runs use BetsAPI instead: a
paid REST API (the spec's "cleanest" option, TT Elite league confirmed)
that serves upcoming/ended events, per-event history (H2H + both players'
recent form in ONE call), and Bet365 pre-match odds as a TIME SERIES —
real line-movement history for the fix-risk guard, and real book prices
(book label "bet365", so the "aggregated price" alert caveat still applies
only to sofascore-priced picks... this book IS a book).

Everything is translated into the pipeline's CANONICAL event dicts (the
sofascore shape the existing parsers consume): the entire downstream —
board parsing, features, edge, claims, grading — runs unchanged.

Client is stdlib urllib (no hard dependency; honors HTTPS_PROXY from the
environment) with an injectable transport for tests. Requests carry only
the token as a query parameter; the token is never logged.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping

from tt_edge.edge.vig import OddsError
from tt_edge.scrapers.odds import OddsSnapshot
from tt_edge.scrapers.sofa_odds import decimal_odds_to_american
from tt_edge.scrapers import sofascore

logger = logging.getLogger(__name__)

BOOK = "bet365"
SPORT_ID_TABLE_TENNIS = 92
DEFAULT_BASE_URL = "https://api.b365api.com"
DEFAULT_LEAGUE_ID = "29128"     # TT Elite Series (from the build spec)
# The high-frequency table-tennis leagues a typical book posts live, by
# BetsAPI league id (verified against the live upcoming feed):
#   29128 TT Elite Series | 29097 TT Cup | 22742 Czech Liga Pro
#   (22307 Setka Cup is also available; add it if the book offers it).
DEFAULT_LEAGUE_IDS = ("29128", "29097", "22742")


def parse_league_ids(raw: str | None) -> list[str]:
    """Comma-separated league ids -> a de-duplicated, order-preserving list;
    falls back to DEFAULT_LEAGUE_IDS when empty/unset."""
    if raw is None or not raw.strip():
        return list(DEFAULT_LEAGUE_IDS)
    seen: dict[str, None] = {}
    for token in raw.split(","):
        cleaned = token.strip()
        if cleaned:
            seen.setdefault(cleaned, None)
    return list(seen) or list(DEFAULT_LEAGUE_IDS)

# BetsAPI time_status -> the canonical status types the board understands.
_STATUS_MAP = {
    "0": "notstarted",
    "1": "inprogress",
    "3": "finished",
    "4": "postponed",
    "5": "canceled",
}


class BetsAPIError(RuntimeError):
    """Transport failure or an API response with success != 1."""


def winner_code_from_score(ss: Any) -> int | None:
    """BetsAPI final score string '3-1' -> 1 (home) / 2 (away) / None."""
    if not isinstance(ss, str) or "-" not in ss:
        return None
    home_text, _, away_text = ss.partition("-")
    try:
        home_score = int(home_text.strip())
        away_score = int(away_text.strip())
    except ValueError:
        return None
    if home_score == away_score:
        return None
    return 1 if home_score > away_score else 2


def translate_event(raw: Any) -> dict | None:
    """One BetsAPI event -> the canonical (sofascore-shaped) event dict.
    None for events missing required fields (skipped upstream)."""
    if not isinstance(raw, Mapping):
        return None
    home = raw.get("home")
    away = raw.get("away")
    league = raw.get("league")
    if not isinstance(home, Mapping) or not isinstance(away, Mapping):
        return None
    try:
        start_ts = int(raw.get("time"))
    except (TypeError, ValueError):
        return None
    event: dict = {
        "id": raw.get("id"),
        "tournament": {"name": (league or {}).get("name", "")
                       if isinstance(league, Mapping) else ""},
        "status": {"type": _STATUS_MAP.get(str(raw.get("time_status")), "")},
        "homeTeam": {"id": home.get("id"), "name": home.get("name")},
        "awayTeam": {"id": away.get("id"), "name": away.get("name")},
        "startTimestamp": start_ts,
    }
    winner = winner_code_from_score(raw.get("ss"))
    if winner is not None and event["status"]["type"] == "finished":
        event["winnerCode"] = winner
    return event


def translate_events(raw_events: Any) -> list[dict]:
    translated = []
    for raw in raw_events if isinstance(raw_events, list) else []:
        event = translate_event(raw)
        if event is not None:
            translated.append(event)
    return translated


def decimal_str_to_american(text: Any) -> int:
    """BetsAPI odds are DECIMAL strings ('1.80', '2') — never fractional."""
    if not isinstance(text, str) or not text.strip() or text.strip() == "-":
        raise OddsError(f"no odds value: {text!r}")
    try:
        value = Decimal(text.strip())
    except InvalidOperation as exc:
        raise OddsError(f"unparseable decimal odds: {text!r}") from exc
    return decimal_odds_to_american(value)


def parse_odds_series(payload: Any, match_source_id: str,
                      sport_id: int = SPORT_ID_TABLE_TENNIS) -> list[OddsSnapshot]:
    """The moneyline series from ``/v2/event/odds`` -> chronological
    OddsSnapshots (book 'bet365'). Entries with missing/suspended prices or
    timestamps are skipped; an unexpected top-level shape raises."""
    if not isinstance(payload, Mapping):
        raise BetsAPIError("odds payload is not an object")
    results = payload.get("results")
    odds = results.get("odds") if isinstance(results, Mapping) else None
    if not isinstance(odds, Mapping):
        return []
    series = odds.get(f"{sport_id}_1")
    if not isinstance(series, list):
        return []
    snapshots: list[OddsSnapshot] = []
    for entry in series:
        if not isinstance(entry, Mapping):
            continue
        try:
            captured = datetime.fromtimestamp(int(entry.get("add_time")),
                                              tz=timezone.utc)
            home_price = decimal_str_to_american(entry.get("home_od"))
            away_price = decimal_str_to_american(entry.get("away_od"))
        except (TypeError, ValueError, OddsError):
            continue
        snapshots.append(OddsSnapshot(
            match_source_id=match_source_id, book=BOOK,
            home_price=home_price, away_price=away_price,
            captured_at=captured))
    snapshots.sort(key=lambda snapshot: snapshot.captured_at)
    return snapshots


def _default_transport(url: str, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise BetsAPIError(f"BetsAPI request failed: {exc}") from exc


class BetsAPIClient:
    """Minimal token-authenticated GET client with success-flag checking."""

    def __init__(self, token: str, *, base_url: str = DEFAULT_BASE_URL,
                 timeout: float = 15.0,
                 transport: Callable[[str, float], tuple[int, bytes]] | None = None
                 ) -> None:
        if not token:
            raise BetsAPIError("BetsAPI token is empty")
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport or _default_transport

    def _get(self, path: str, **params: Any) -> dict:
        params["token"] = self._token
        url = f"{self._base_url}{path}?{urllib.parse.urlencode(params)}"
        status, body = self._transport(url, self._timeout)
        if status != 200:
            raise BetsAPIError(f"BetsAPI HTTP {status} for {path}")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise BetsAPIError(f"BetsAPI non-JSON response for {path}") from exc
        if not isinstance(payload, dict) or payload.get("success") != 1:
            error = payload.get("error") if isinstance(payload, dict) else None
            raise BetsAPIError(f"BetsAPI error for {path}: {error!r}")
        return payload

    def upcoming(self, day: str, league_id: str) -> list[dict]:
        """Upcoming events for a UTC day ('YYYYMMDD'), one page (50) —
        plenty for one league's day."""
        payload = self._get("/v3/events/upcoming",
                            sport_id=SPORT_ID_TABLE_TENNIS,
                            league_id=league_id, day=day)
        results = payload.get("results")
        return results if isinstance(results, list) else []

    def ended(self, day: str, league_id: str) -> list[dict]:
        payload = self._get("/v3/events/ended",
                            sport_id=SPORT_ID_TABLE_TENNIS,
                            league_id=league_id, day=day)
        results = payload.get("results")
        return results if isinstance(results, list) else []

    def history(self, event_id: str, qty: int = 20) -> dict:
        """H2H + both players' recent events in one call."""
        payload = self._get("/v1/event/history", event_id=event_id, qty=qty)
        results = payload.get("results")
        return results if isinstance(results, dict) else {}

    def odds(self, event_id: str) -> dict:
        return self._get("/v2/event/odds", event_id=event_id)


@dataclass(frozen=True)
class CloudBundle:
    """Everything one cloud cycle needs, already canonical."""

    envelopes: list[sofascore.Envelope]
    odds_snapshots: list[OddsSnapshot]
    events_fetched: int


def fetch_cloud_bundle(client: BetsAPIClient, fetch_dates: list[str], *,
                       league_id: str, max_events: int,
                       now: datetime) -> CloudBundle:
    """BetsAPI -> canonical envelopes + odds snapshots.

    Per date: upcoming + ended events merged into ONE board envelope (the
    scan takes the not-started ones, grading takes the finished/canceled
    ones). Per upcoming event (capped): one history call feeding the H2H
    envelope AND both form envelopes, plus the full odds series. Failures
    are per-event: logged, skipped, never fatal to the bundle."""
    envelopes: list[sofascore.Envelope] = []
    odds_snapshots: list[OddsSnapshot] = []
    upcoming_events: list[tuple[str, dict]] = []

    for date_str in fetch_dates:
        day = date_str.replace("-", "")
        board_events: list[dict] = []
        for fetch in (client.upcoming, client.ended):
            try:
                board_events.extend(translate_events(fetch(day, league_id)))
            except BetsAPIError as exc:
                logger.warning("betsapi: %s failed for %s: %s",
                               fetch.__name__, date_str, exc)
        envelopes.append(sofascore.Envelope(
            kind=sofascore.BOARD, entity_id=date_str,
            url=f"betsapi:events/{day}", fetched_at=now,
            payload={"events": board_events}))
        for event in board_events:
            if event["status"]["type"] == "notstarted":
                upcoming_events.append((str(event["id"]), event))

    for event_id, event in upcoming_events[:max_events]:
        try:
            history = client.history(event_id)
        except BetsAPIError as exc:
            logger.warning("betsapi: history failed for %s: %s", event_id, exc)
            history = {}
        envelopes.append(sofascore.Envelope(
            kind=sofascore.H2H, entity_id=event_id,
            url=f"betsapi:event/history/{event_id}", fetched_at=now,
            payload={"events": translate_events(history.get("h2h"))}))
        for side, player_key in (("home", "homeTeam"), ("away", "awayTeam")):
            player_id = str(event[player_key]["id"])
            envelopes.append(sofascore.Envelope(
                kind=sofascore.FORM, entity_id=player_id,
                url=f"betsapi:event/history/{event_id}#{side}",
                fetched_at=now,
                payload={"events": translate_events(history.get(side))}))
        try:
            odds_snapshots.extend(parse_odds_series(client.odds(event_id),
                                                    event_id))
        except BetsAPIError as exc:
            logger.warning("betsapi: odds failed for %s: %s", event_id, exc)

    if len(upcoming_events) > max_events:
        logger.warning("betsapi: %d upcoming events, fetched details for the "
                       "first %d (TT_EDGE_AUTOSCAN_MAX_MATCHES)",
                       len(upcoming_events), max_events)
    return CloudBundle(envelopes=envelopes, odds_snapshots=odds_snapshots,
                       events_fetched=len(upcoming_events[:max_events]))
