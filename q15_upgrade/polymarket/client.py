"""Read-only Polymarket client (public market data — no auth).

Two public APIs, both read-only:
  * Gamma  (``gamma-api.polymarket.com``) — market discovery + resolution metadata.
  * CLOB   (``clob.polymarket.com``)      — order book / prices for a token id.

Only market-data endpoints are used; nothing here can place or cancel an order.
Every call is timeout-bounded and swallows network/parse errors (returns None),
so a Polymarket outage can never raise into the live loop.

NOTE (live-validation): the exact Gamma field names for a 15-minute up/down
event (the "price to beat" reference and the resolution payload) must be
confirmed against the live API. Parsing here is deliberately tolerant — it checks
several plausible fields and degrades to None rather than guessing — so an
unexpected shape yields "no reference / not yet resolved", never a crash or a
fabricated value.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

try:  # requests is a declared runtime dep; guard so import never breaks the app
    import requests
except Exception:  # pragma: no cover - defensive
    requests = None  # type: ignore

from .ledger import WINDOW_SECONDS

logger = logging.getLogger(__name__)


@dataclass
class PolymarketMarket:
    asset: str
    slug: str
    condition_id: str
    yes_token_id: str | None          # UP outcome token
    no_token_id: str | None           # DOWN outcome token
    open_price: float | None          # "price to beat" (window-open reference)
    window_open: float
    window_close: float


@dataclass
class BookMetrics:
    yes_bid: float | None             # cents (0..100)
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    spread: float | None              # cents
    depth: float | None               # contracts near top of book
    last_trade: float | None          # cents
    market_prob_up: float | None      # 0..1 implied P(up)


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def updown_slug(asset: str, window_open_epoch: float) -> str:
    """Polymarket event slug for a 15-minute up/down window, e.g.
    ``btc-updown-15m-1777248900`` (the number is the window OPEN unix time)."""
    return f"{asset.lower()}-updown-15m-{int(window_open_epoch)}"


class PolymarketClient:
    def __init__(self, *, clob_base: str, gamma_base: str, timeout: float = 5.0,
                 session: Any = None):
        self.clob_base = clob_base.rstrip("/")
        self.gamma_base = gamma_base.rstrip("/")
        self.timeout = float(timeout)
        self._session = session or (requests.Session() if requests is not None else None)

    # ---- HTTP ----
    def _get_json(self, url: str, params: dict | None = None) -> Any:
        if self._session is None:
            return None
        try:
            resp = self._session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception:  # requests.RequestException / JSON errors — never raise out
            logger.debug("polymarket GET failed: %s", url, exc_info=True)
            return None

    # ---- discovery ----
    def find_updown_market(self, asset: str, window_close_epoch: float) -> PolymarketMarket | None:
        """Locate the up/down event for ``asset`` whose 15-minute window ends at
        ``window_close_epoch``. Returns None when no such market exists (the common
        case for assets Polymarket doesn't list at 15m) or on any fetch/parse error."""
        open_epoch = float(window_close_epoch) - WINDOW_SECONDS
        slug = updown_slug(asset, open_epoch)
        data = self._get_json(f"{self.gamma_base}/events", {"slug": slug})
        event = self._first_event(data)
        if not event:
            return None
        market = self._first_market(event)
        if not market:
            return None
        yes_tok, no_tok = self._token_ids(market)
        condition_id = (market.get("conditionId") or market.get("condition_id")
                        or event.get("id"))
        if not condition_id:
            return None
        return PolymarketMarket(
            asset=asset.upper(), slug=slug, condition_id=str(condition_id),
            yes_token_id=yes_tok, no_token_id=no_tok,
            open_price=self._price_to_beat(event, market),
            window_open=open_epoch, window_close=float(window_close_epoch),
        )

    @staticmethod
    def _first_event(data: Any) -> dict | None:
        if isinstance(data, list):
            return data[0] if data and isinstance(data[0], dict) else None
        if isinstance(data, dict):
            evs = data.get("events") or data.get("data")
            if isinstance(evs, list) and evs:
                return evs[0] if isinstance(evs[0], dict) else None
            return data if data.get("markets") or data.get("slug") else None
        return None

    @staticmethod
    def _first_market(event: dict) -> dict | None:
        markets = event.get("markets")
        if isinstance(markets, list) and markets and isinstance(markets[0], dict):
            return markets[0]
        return event if event.get("clobTokenIds") or event.get("conditionId") else None

    @staticmethod
    def _token_ids(market: dict) -> tuple[str | None, str | None]:
        raw = market.get("clobTokenIds") or market.get("clob_token_ids")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                raw = None
        if isinstance(raw, list) and len(raw) >= 2:
            return str(raw[0]), str(raw[1])      # [YES/UP, NO/DOWN] by convention
        return None, None

    @staticmethod
    def _price_to_beat(event: dict, market: dict) -> float | None:
        for src in (market, event):
            for key in ("priceToBeat", "price_to_beat", "openingPrice", "referencePrice"):
                v = _num(src.get(key))
                if v is not None and v > 0.0:
                    return v
        return None

    # ---- order book / prices ----
    def book_metrics(self, market: PolymarketMarket) -> BookMetrics:
        """Best YES/NO bid/ask (cents), spread, top-of-book depth, last trade, and
        the market-implied P(up). Derived from the YES (UP) token's book; NO side
        is the binary complement (no_ask = 100 - yes_bid). All-None on failure."""
        empty = BookMetrics(None, None, None, None, None, None, None, None)
        if not market.yes_token_id:
            return empty
        book = self._get_json(f"{self.clob_base}/book", {"token_id": market.yes_token_id})
        if not isinstance(book, dict):
            return empty
        yes_bid, yes_ask, depth = self._top(book)
        no_bid = None if yes_ask is None else 100.0 - yes_ask
        no_ask = None if yes_bid is None else 100.0 - yes_bid
        spread = None if (yes_bid is None or yes_ask is None) else max(0.0, yes_ask - yes_bid)
        last = self._last_trade(market.yes_token_id)
        # Implied P(up): mid of the YES book (cents -> 0..1); fall back to last trade.
        if yes_bid is not None and yes_ask is not None:
            prob_up = (yes_bid + yes_ask) / 200.0
        elif last is not None:
            prob_up = last / 100.0
        else:
            prob_up = None
        return BookMetrics(yes_bid, yes_ask, no_bid, no_ask, spread, depth, last, prob_up)

    @staticmethod
    def _top(book: dict) -> tuple[float | None, float | None, float | None]:
        """Best bid (cents), best ask (cents), and summed top-of-book size."""
        bids = book.get("bids") or []
        asks = book.get("asks") or []

        def _levels(side):
            out = []
            for lvl in side if isinstance(side, list) else []:
                p = _num(lvl.get("price")) if isinstance(lvl, dict) else None
                s = _num(lvl.get("size")) if isinstance(lvl, dict) else None
                if p is not None:
                    out.append((p, s or 0.0))
            return out

        b = _levels(bids)
        a = _levels(asks)
        best_bid = max((p for p, _ in b), default=None)
        best_ask = min((p for p, _ in a), default=None)
        depth = sum(s for _, s in b) + sum(s for _, s in a) if (b or a) else None
        # Polymarket prices are 0..1 dollars; present as cents.
        return (None if best_bid is None else best_bid * 100.0,
                None if best_ask is None else best_ask * 100.0,
                depth)

    def _last_trade(self, token_id: str) -> float | None:
        data = self._get_json(f"{self.clob_base}/last-trade-price", {"token_id": token_id})
        if isinstance(data, dict):
            p = _num(data.get("price"))
            return None if p is None else p * 100.0
        return None

    # ---- resolution ----
    def resolved_result(self, market: PolymarketMarket) -> str | None:
        """Polymarket's own settled result: "UP", "DOWN", or None if not yet
        resolved / unknown. Read from the event's outcome prices ([YES, NO])."""
        data = self._get_json(f"{self.gamma_base}/events", {"slug": market.slug})
        event = self._first_event(data)
        if not event:
            return None
        mk = self._first_market(event) or {}
        prices = mk.get("outcomePrices") or mk.get("outcome_prices")
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except (TypeError, ValueError):
                prices = None
        if isinstance(prices, list) and len(prices) >= 2:
            yes = _num(prices[0])
            no = _num(prices[1])
            if yes is not None and no is not None:
                if yes >= 0.99 and no <= 0.01:
                    return "UP"
                if no >= 0.99 and yes <= 0.01:
                    return "DOWN"
        return None
