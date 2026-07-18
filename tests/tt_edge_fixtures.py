"""Shared builders for the tt_edge test files (not collected by pytest).

Everything is deterministic: fixed clock, in-memory SQLite, sofascore-shaped
dict payloads, and a Telegram transport fake that runs through the REAL
shared TelegramSendClient (so the tests exercise the actual delivery path).
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from notifications.telegram_client import TelegramSendClient

from tt_edge.alerts.telegram import TTEdgeTelegram
from tt_edge.config import TTEdgeConfig
from tt_edge.db import repo as repo_mod
from tt_edge.scrapers import sofascore

# A fixed scan instant: 2026-07-18 14:26 UTC (matches start 14:30).
NOW = datetime(2026, 7, 18, 14, 26, 0, tzinfo=timezone.utc)


def make_config(**overrides: Any) -> TTEdgeConfig:
    """The production defaults, overridable per test."""
    base = TTEdgeConfig(
        database_url=":memory:",
        max_board_age_s=1800, max_h2h_age_s=86400, max_form_age_s=86400,
        max_odds_age_s=600,
        min_edge=Decimal("0.05"), no_edge_band=Decimal("0.03"),
        kelly_fraction=Decimal("0.25"), kelly_cap_pct=Decimal("0.05"),
        stake_floor_cents=100, max_open_recommendations=3, fix_risk_cents=15,
        w_h2h=0.45, w_form=0.35, w_common=0.20, blend_scale=2.5,
        h2h_half_life_days=365.0, common_opp_window_days=60,
        form_last_n=15, form_hot_n=5,
        min_h2h_meetings=5, min_common_opps=3, calibration_min_rows=300,
        tournament_keyword="TT Elite",
        telegram_token="test-token", telegram_chat_id="test-chat",
        telegram_enabled=True)
    return dataclasses.replace(base, **overrides) if overrides else base


def make_repo() -> repo_mod.TTEdgeRepo:
    repo = repo_mod.connect(":memory:")
    repo.apply_migrations()
    return repo


# ── sofascore payload builders ────────────────────────────────────────────


def event_dict(event_id: Any, home_id: Any, home_name: str, away_id: Any,
               away_name: str, start: datetime, *,
               tournament: str = "TT Elite Series",
               status: str = "notstarted",
               winner_code: int | None = None) -> dict:
    body: dict = {
        "id": event_id,
        "tournament": {"name": tournament},
        "status": {"type": status},
        "homeTeam": {"id": home_id, "name": home_name},
        "awayTeam": {"id": away_id, "name": away_name},
        "startTimestamp": int(start.timestamp()),
    }
    if winner_code is not None:
        body["winnerCode"] = winner_code
    return body


def finished(event_id: Any, home_id: Any, away_id: Any, start: datetime,
             winner_code: int, *, home_name: str = "H",
             away_name: str = "A") -> dict:
    return event_dict(event_id, home_id, home_name, away_id, away_name, start,
                      status="finished", winner_code=winner_code)


def envelope(kind: str, entity_id: str, payload: Any,
             fetched_at: datetime) -> sofascore.Envelope:
    return sofascore.Envelope(kind=kind, entity_id=entity_id,
                              url=f"test:{kind}/{entity_id}",
                              fetched_at=fetched_at, payload=payload)


# ── the standard one-match scan scenario ─────────────────────────────────
#
# Match 9001: Marud (home, id 101) vs Gumulinski (away, id 102), 14:30 UTC.
# The data heavily favors MARUD: H2H 15-5 in the last 20, red-hot form, and
# both common opponents beaten. With home -120 / away +100 the de-vig fair
# home probability is 12/23 ~ 0.5217 while the blend lands ~0.82 — a clear
# recommendation whose quarter-Kelly stake pins at the 5% cap ($3.25 on $65).

MATCH_ID = "9001"
HOME_ID, HOME_NAME = "101", "Marud"
AWAY_ID, AWAY_NAME = "102", "Gumulinski"
START = NOW + timedelta(minutes=4)


def board_payload() -> dict:
    return {"events": [event_dict(int(MATCH_ID), int(HOME_ID), HOME_NAME,
                                  int(AWAY_ID), AWAY_NAME, START)]}


def h2h_payload() -> dict:
    events = []
    for i in range(20):
        winner = 1 if i % 4 != 3 else 2       # home wins 15 of 20
        events.append(finished(8000 + i, int(HOME_ID), int(AWAY_ID),
                               NOW - timedelta(days=3 + i), winner,
                               home_name=HOME_NAME, away_name=AWAY_NAME))
    return {"events": events}


def form_payload_home() -> dict:
    """Marud: 12-3 over the last 15, winners recent; includes wins over the
    common opponents 201 and 202."""
    events = []
    for i in range(15):
        opponent = 201 if i == 0 else (202 if i == 1 else 300 + i)
        winner = 1 if i < 12 else 2           # 12 wins then 3 losses
        events.append(finished(7000 + i, int(HOME_ID), opponent,
                               NOW - timedelta(days=1 + i), winner,
                               home_name=HOME_NAME))
    return {"events": events}


def form_payload_away() -> dict:
    """Gumulinski: 5-10, cold recently; loses to both common opponents."""
    events = []
    for i in range(15):
        opponent = 201 if i == 0 else (202 if i == 1 else 400 + i)
        winner = 2 if i < 10 else 1           # away perspective: 10 losses
        events.append(finished(6000 + i, int(AWAY_ID), opponent,
                               NOW - timedelta(days=1 + i), winner,
                               home_name=AWAY_NAME))
    return {"events": events}


def scenario_envelopes(*, board_age: timedelta = timedelta(minutes=4),
                       h2h_age: timedelta = timedelta(hours=2),
                       form_age: timedelta = timedelta(hours=1)) -> dict:
    return {
        "board": envelope(sofascore.BOARD, "2026-07-18", board_payload(),
                          NOW - board_age),
        "h2h": {MATCH_ID: envelope(sofascore.H2H, MATCH_ID, h2h_payload(),
                                   NOW - h2h_age)},
        "form": {
            HOME_ID: envelope(sofascore.FORM, HOME_ID, form_payload_home(),
                              NOW - form_age),
            AWAY_ID: envelope(sofascore.FORM, AWAY_ID, form_payload_away(),
                              NOW - form_age),
        },
    }


# ── Telegram fakes (real client, fake transport) ─────────────────────────


class FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code

    def json(self) -> dict:
        return {"result": {"message_id": 7}}


class FakePost:
    """requests.post stand-in; records every payload it was asked to send."""

    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.calls: list[dict] = []

    def __call__(self, url: str, json: dict | None = None,
                 timeout: float | None = None) -> FakeResponse:
        self.calls.append({"url": url, "json": json})
        return FakeResponse(self.status_code)


def make_sender(*, status_code: int = 200,
                configured: bool = True) -> tuple[TTEdgeTelegram, FakePost]:
    post = FakePost(status_code)
    client = TelegramSendClient(
        "tok" if configured else None, "chat" if configured else "",
        enabled=True, retries=0, sleep_seconds=0.0, post=post)
    return TTEdgeTelegram(client), post
