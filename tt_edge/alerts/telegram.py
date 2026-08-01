"""TT-Edge Telegram alerts.

Delivery rides the shared, battle-tested Q15 client
(``notifications.telegram_client.TelegramSendClient``) — HTML mode, bounded
retry, structured result — via its own bot/chat env vars
(``TT_EDGE_TELEGRAM_*``), completely separate from the Q15 channels.

Ordering contract (the Q15 claim-after-send fix, applied here from day one):

1. The RECOMMENDATION ROW is inserted first — the DB unique key is the
   duplicate-alert claim, so a concurrent/rescan pass can never double-alert.
2. The alert is sent.
3. ``alert_delivered_at`` is set ONLY after Telegram confirms (send-then-
   claim): a failed send leaves the row undelivered and the next scan
   retries the SEND without creating a new recommendation.

These messages intentionally carry none of the Q15 champion-path markers
("ENTRY RECOMMENDED" / "NO ENTRY YET" / "V9.5 CHECK") — they must never be
picked up by the legacy formatters or suppression logic.
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from notifications.telegram_client import TelegramSendClient

from tt_edge.config import TTEdgeConfig
from tt_edge.edge.vig import probability_to_american
from tt_edge.freshness import format_age, require_aware


def _fmt_american(price: int) -> str:
    return f"+{price}" if price > 0 else str(price)


def _fmt_pct(p: Decimal) -> str:
    return f"{(p * 100):.1f}%"


def _fmt_signed_points(value: Decimal) -> str:
    points = value * 100
    sign = "+" if points >= 0 else ""
    return f"{sign}{points:.1f}"


def _fmt_dollars(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:.2f}"


@dataclass(frozen=True)
class AlertContext:
    """Everything the alert template needs, already from the PICK's
    perspective (the scan job flips home-perspective stats when the model
    favors the away player)."""

    home_name: str
    away_name: str
    pick_name: str
    start_time: datetime            # aware UTC
    market: str                     # 'ML'
    price_american: int
    fair_p: Decimal                 # de-vig market prob of the pick
    model_p: Decimal                # model prob of the pick
    edge: Decimal
    h2h_wins: int                   # career, pick's perspective
    h2h_losses: int
    l20_wins: int
    l20_losses: int
    form_diff: float                # pick minus opponent, [-1, 1]
    common_opps: int
    common_net: int                 # net units, pick's perspective
    stake_cents: int
    kelly_multiplier: Decimal       # e.g. 0.25
    bankroll_cents: int
    board_age_s: float
    h2h_age_s: float | None         # None = no H2H data (common-opps pick)
    odds_age_s: float | None        # None only on a resend with no fresh odds
    book: str = "manual"            # odds source; non-manual adds a caveat


def build_alert(ctx: AlertContext) -> str:
    """Render the alert exactly in the spec shape. Every alert shows its
    data ages — an operator must always see how old the inputs were."""
    start = require_aware("start_time", ctx.start_time)
    fair_price = probability_to_american(ctx.fair_p)
    form_pct = f"{ctx.form_diff * 100:+.0f}%"
    lines = [
        (f"🏓 TT EDGE | {html.escape(ctx.home_name)} vs "
         f"{html.escape(ctx.away_name)} | {start:%H:%M} UTC"),
        (f"Pick: {html.escape(ctx.pick_name)} {ctx.market} @ "
         f"{_fmt_american(ctx.price_american)} "
         f"(fair {_fmt_american(fair_price)})"),
        (f"Model: {_fmt_pct(ctx.model_p)} | Market (de-vig): "
         f"{_fmt_pct(ctx.fair_p)} | Edge: {_fmt_signed_points(ctx.edge)}"),
        (f"Basis: H2H {ctx.h2h_wins}-{ctx.h2h_losses} "
         f"(L20: {ctx.l20_wins}-{ctx.l20_losses}), form {form_pct}, "
         f"{ctx.common_opps} common opps net {ctx.common_net:+d}"),
        (f"Stake: {_fmt_dollars(ctx.stake_cents)} ({ctx.kelly_multiplier}K, "
         f"roll {_fmt_dollars(ctx.bankroll_cents)})"),
        (f"Data age: board {format_age(ctx.board_age_s)}, "
         f"H2H {'-' if ctx.h2h_age_s is None else format_age(ctx.h2h_age_s)}, "
         f"odds "
         f"{'-' if ctx.odds_age_s is None else format_age(ctx.odds_age_s)}"),
    ]
    if ctx.book != "manual":
        # Aggregated prices are not the operator's book: the edge exists AT
        # this price. Never chase a worse one.
        lines.append(f"Price source: {html.escape(ctx.book)} — take "
                     f"{_fmt_american(ctx.price_american)} or better at "
                     f"your book")
    return "\n".join(lines)


class TTEdgeTelegram:
    """Send adapter. Missing token/chat means muted — records still written,
    nothing delivered (same contract as every other book sender)."""

    def __init__(self, client: TelegramSendClient,
                 source: str = "dedicated") -> None:
        self._client = client
        # 'dedicated' (TT_EDGE bot), 'q15_fallback', or 'unconfigured' —
        # surfaced by the autoscan --probe so the operator can see where
        # (or why not) alerts will land.
        self.source = source if client.enabled else "unconfigured"

    @classmethod
    def from_config(cls, config: TTEdgeConfig, **client_kwargs) -> "TTEdgeTelegram":
        """Prefer the dedicated TT_EDGE_TELEGRAM_* bot; when it is unset and
        the fallback is enabled (default), ride the Q15 monitor's existing
        bot/chat so picks flow with zero new setup. TT EDGE alerts carry
        their own 🏓 header and none of the Q15 formatter markers, so they
        pass through that channel untouched."""
        token = config.telegram_token
        chat_id = config.telegram_chat_id
        source = "dedicated"
        if not token and config.telegram_fallback and config.q15_telegram_token:
            token = config.q15_telegram_token
            chat_id = config.q15_telegram_chat_id
            source = "q15_fallback"
        return cls(TelegramSendClient(
            token, chat_id, enabled=config.telegram_enabled, **client_kwargs),
            source=source)

    @property
    def configured(self) -> bool:
        return self._client.enabled

    def send(self, text: str) -> dict:
        """Returns the shared result contract: {ok, delivered, muted,
        message_id, error}."""
        return self._client.send(text)
