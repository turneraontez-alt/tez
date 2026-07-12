from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import os
import threading
import time
from collections import defaultdict, deque
from copy import deepcopy
from typing import Dict, Iterable, List, Optional, Set

from .kalshi_rest import parse_ts
from .precision import dollars_to_cents, normalized_taker_side

logger = logging.getLogger(__name__)

try:
    import websockets
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    _HAVE_WS = True
except Exception:  # pragma: no cover - optional dependency guard
    websockets = None
    hashes = serialization = padding = None
    _HAVE_WS = False


PROD_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
DEMO_URL = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
WS_PATH = "/trade-api/ws/v2"


def _read_private_key() -> bytes | None:
    inline = os.environ.get("KALSHI_PRIVATE_KEY") or os.environ.get("KALSHI_API_PRIVATE_KEY")
    if inline:
        try:
            from kalshi_auth import _normalize_pem
            inline = _normalize_pem(inline)
        except Exception:
            inline = inline.replace("\\n", "\n")
        return inline.encode()
    path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if path:
        try:
            with open(path, "rb") as handle:
                return handle.read()
        except OSError as exc:
            logger.warning("Unable to read KALSHI_PRIVATE_KEY_PATH: %s", exc)
    return None


class KalshiWebSocketFeed:
    """Read-only authenticated market-data feed.

    The class only subscribes to orderbook, public trade, and lifecycle data.
    It has no order-submission methods.
    """

    def __init__(self):
        self.key_id = os.environ.get("KALSHI_API_KEY_ID") or os.environ.get("KALSHI_ACCESS_KEY")
        self.private_key_bytes = _read_private_key()
        self.url = os.environ.get("KALSHI_WS_URL") or (
            DEMO_URL if os.environ.get("KALSHI_ENV", "production").lower() == "demo" else PROD_URL
        )
        self.enabled = bool(_HAVE_WS and self.key_id and self.private_key_bytes)
        self._lock = threading.RLock()
        self._desired: Set[str] = set()
        self._subscribed: Set[str] = set()
        self._books: Dict[str, dict] = {}
        self._book_events: Dict[str, deque] = defaultdict(lambda: deque(maxlen=5000))
        self._trades: Dict[str, deque] = defaultdict(lambda: deque(maxlen=5000))
        self._market_status: Dict[str, dict] = {}
        self._connected = False
        self._last_message_at: float | None = None
        self._last_orderbook_at: float | None = None
        self._last_trade_at: float | None = None
        self._last_error: str | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._reconnect = threading.Event()
        self._message_id = 1
        if self.enabled:
            self.start()
        elif not _HAVE_WS:
            logger.warning("WebSocket disabled: install websockets and cryptography")
        else:
            logger.info("WebSocket disabled until KALSHI_API_KEY_ID and private key are configured")

    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._thread_main, name="kalshi-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._reconnect.set()

    def subscribe(self, market_tickers: Iterable[str]) -> None:
        wanted = {str(t) for t in market_tickers if t}
        with self._lock:
            changed = wanted != self._desired
            self._desired = wanted
            # Evict cached per-ticker state for markets no longer desired so
            # these dicts track the desired set instead of growing unbounded as
            # 15m markets roll over.  Key the prune off the authoritative
            # desired set (never age) so an active book is never dropped; these
            # structures are read fresh-or-None, so dropping non-desired tickers
            # loses nothing.
            for cache in (self._books, self._book_events, self._trades, self._market_status):
                for ticker in [t for t in cache if t not in wanted]:
                    del cache[ticker]
        if changed:
            self._reconnect.set()

    def is_connected(self) -> bool:
        with self._lock:
            return bool(self._connected)

    def health(self) -> dict:
        now = time.time()
        with self._lock:
            return {
                "connected": self._connected,
                "enabled": self.enabled,
                "last_message_at": self._last_message_at,
                "last_orderbook_at": self._last_orderbook_at,
                "last_trade_at": self._last_trade_at,
                "data_age_seconds": (now - self._last_message_at) if self._last_message_at else None,
                "orderbook_age_seconds": (now - self._last_orderbook_at) if self._last_orderbook_at else None,
                "trade_age_seconds": (now - self._last_trade_at) if self._last_trade_at else None,
                "subscribed_markets": sorted(self._subscribed),
                "last_error": self._last_error,
                "settlement_feed": False,
                "mode": "websocket" if self._connected else "rest-fallback",
            }

    def book_ages(self) -> dict:
        """Per-ticker age (seconds) of the cached orderbook — freshness probe."""
        now = time.time()
        with self._lock:
            return {t: round(now - b.get("updated_at", 0.0), 3) for t, b in self._books.items()}

    def get_orderbook(self, ticker: str, max_age: float = 3.0):
        now = time.time()
        with self._lock:
            book = self._books.get(ticker)
            if not book or now - book.get("updated_at", 0.0) > max_age:
                return None
            yes = [[f"{price / 100:.4f}", f"{qty:.2f}"] for price, qty in sorted(book["yes"].items()) if qty > 0]
            no = [[f"{price / 100:.4f}", f"{qty:.2f}"] for price, qty in sorted(book["no"].items()) if qty > 0]
            return {"yes_dollars": yes, "no_dollars": no, "_source": "websocket", "_updated_at": book["updated_at"]}

    def get_trades(self, ticker: str, min_ts: float | None = None, max_age: float = 10.0) -> List[dict]:
        now = time.time()
        with self._lock:
            rows = list(self._trades.get(ticker, ()))
        out = []
        for row in rows:
            if min_ts is not None and row["ts"] < min_ts:
                continue
            if now - row["ts"] > max_age and min_ts is None:
                continue
            out.append(dict(row))
        return out

    def get_microstructure(
        self,
        ticker: str,
        *,
        now: float | None = None,
        horizons: tuple[int, ...] = (5, 15, 30, 60),
        max_book_age: float = 5.0,
    ) -> dict:
        """Return bounded event-level book and trade evidence for research.

        The output is descriptive only. A negative book delta can be a cancel
        or execution, so it is deliberately named ``book_delta_pressure`` and
        is never represented as identified order intent.
        """
        now = time.time() if now is None else float(now)
        with self._lock:
            book = deepcopy(self._books.get(ticker))
            events = [dict(row) for row in self._book_events.get(ticker, ())]
            trades = [dict(row) for row in self._trades.get(ticker, ())]
        if not book:
            return {"available": False, "reason": "book_missing"}
        book_age = max(0.0, now - float(book.get("updated_at", 0.0)))
        if book_age > max(0.0, float(max_book_age)):
            return {
                "available": False,
                "reason": "book_stale",
                "book_age_seconds": book_age,
            }

        yes = book.get("yes") if isinstance(book.get("yes"), dict) else {}
        no = book.get("no") if isinstance(book.get("no"), dict) else {}
        yes_levels = [(float(price), float(qty)) for price, qty in yes.items() if float(qty) > 0]
        no_levels = [(float(price), float(qty)) for price, qty in no.items() if float(qty) > 0]
        yes_best = max(yes_levels, default=(None, None), key=lambda row: row[0])
        no_best = max(no_levels, default=(None, None), key=lambda row: row[0])
        yes_bid, yes_bid_qty = yes_best
        no_bid, no_bid_qty = no_best
        yes_ask = None if no_bid is None else 100.0 - no_bid
        no_ask = None if yes_bid is None else 100.0 - yes_bid
        yes_mid = None
        yes_microprice = None
        if yes_bid is not None and yes_ask is not None:
            yes_mid = (yes_bid + yes_ask) / 2.0
            total_top = float(yes_bid_qty or 0.0) + float(no_bid_qty or 0.0)
            if total_top > 0:
                yes_microprice = (
                    yes_ask * float(yes_bid_qty or 0.0)
                    + yes_bid * float(no_bid_qty or 0.0)
                ) / total_top

        out = {
            "available": yes_bid is not None and no_bid is not None,
            "reason": None if yes_bid is not None and no_bid is not None else "two_sided_book_missing",
            "book_age_seconds": book_age,
            "yes_bid_cents": yes_bid,
            "yes_ask_cents": yes_ask,
            "no_bid_cents": no_bid,
            "no_ask_cents": no_ask,
            "yes_bid_qty": yes_bid_qty,
            "yes_ask_qty": no_bid_qty,
            "yes_mid_cents": yes_mid,
            "yes_microprice_cents": yes_microprice,
            "yes_microprice_edge_cents": (
                None if yes_mid is None or yes_microprice is None else yes_microprice - yes_mid
            ),
            "event_age_seconds": (
                None if not events else max(0.0, now - float(events[-1].get("ts", now)))
            ),
        }
        for horizon in sorted({max(1, int(value)) for value in horizons}):
            cutoff = now - horizon
            selected_events = [row for row in events if float(row.get("ts", 0.0)) >= cutoff]
            selected_trades = [row for row in trades if float(row.get("ts", 0.0)) >= cutoff]
            signed_delta = 0.0
            absolute_delta = 0.0
            yes_depletion = no_depletion = 0.0
            yes_refill = no_refill = 0.0
            for row in selected_events:
                delta = float(row.get("delta", 0.0) or 0.0)
                side_sign = 1.0 if row.get("side") == "yes" else -1.0
                signed_delta += side_sign * delta
                absolute_delta += abs(delta)
                if row.get("at_best_before") and delta < 0:
                    if row.get("side") == "yes":
                        yes_depletion += -delta
                    else:
                        no_depletion += -delta
                if row.get("at_best_after") and delta > 0:
                    if row.get("side") == "yes":
                        yes_refill += delta
                    else:
                        no_refill += delta
            yes_taker = no_taker = 0.0
            for row in selected_trades:
                count = max(0.0, float(row.get("count", 0.0) or 0.0))
                side = str(row.get("taker_side") or "").upper()
                if side == "YES":
                    yes_taker += count
                elif side == "NO":
                    no_taker += count
            total_taker = yes_taker + no_taker
            suffix = f"_{horizon}s"
            out[f"event_count{suffix}"] = len(selected_events)
            out[f"trade_count{suffix}"] = len(selected_trades)
            out[f"book_delta_pressure_yes{suffix}"] = (
                None if absolute_delta <= 0 else signed_delta / absolute_delta
            )
            out[f"trade_imbalance_yes{suffix}"] = (
                None if total_taker <= 0 else (yes_taker - no_taker) / total_taker
            )
            out[f"yes_best_depletion{suffix}"] = yes_depletion
            out[f"no_best_depletion{suffix}"] = no_depletion
            out[f"yes_best_refill{suffix}"] = yes_refill
            out[f"no_best_refill{suffix}"] = no_refill
        return out

    def _headers(self) -> dict:
        timestamp = str(int(time.time() * 1000))
        private_key = serialization.load_pem_private_key(self.private_key_bytes, password=None)
        message = f"{timestamp}GET{WS_PATH}".encode()
        signature = private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }

    def _thread_main(self) -> None:
        asyncio.run(self._run_forever())

    async def _connect(self, headers):
        header_kw = (
            "additional_headers"
            if "additional_headers" in inspect.signature(websockets.connect).parameters
            else "extra_headers"
        )
        kwargs = {
            header_kw: headers,
            "ping_interval": 20,
            "ping_timeout": 20,
            "close_timeout": 5,
            "max_queue": 10000,
        }
        return websockets.connect(self.url, **kwargs)

    async def _run_forever(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                headers = self._headers()
                connector = await self._connect(headers)
                async with connector as socket:
                    with self._lock:
                        self._connected = True
                        self._last_error = None
                        desired = sorted(self._desired)
                        self._subscribed = set(desired)
                    self._reconnect.clear()
                    if desired:
                        await self._send_subscriptions(socket, desired)
                    backoff = 1.0
                    while not self._stop.is_set() and not self._reconnect.is_set():
                        try:
                            message = await asyncio.wait_for(socket.recv(), timeout=5.0)
                        except asyncio.TimeoutError:
                            continue
                        self._handle_message(message)
            except Exception as exc:
                with self._lock:
                    self._last_error = str(exc)[:300]
                logger.warning("Kalshi WebSocket reconnecting after error: %s", exc)
            finally:
                with self._lock:
                    self._connected = False
                    self._subscribed = set()
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 1.8)

    async def _send_subscriptions(self, socket, tickers: List[str]) -> None:
        # Keep the legacy two-scale orderbook convention explicit.  The parser
        # supports separate YES and NO bid price scales and derives asks safely.
        orderbook = {
            "id": self._next_id(),
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": tickers,
                "use_yes_price": False,
            },
        }
        trades = {
            "id": self._next_id(),
            "cmd": "subscribe",
            "params": {"channels": ["trade"], "market_tickers": tickers},
        }
        lifecycle = {
            "id": self._next_id(),
            "cmd": "subscribe",
            "params": {"channels": ["market_lifecycle_v2"]},
        }
        for payload in (orderbook, trades, lifecycle):
            await socket.send(json.dumps(payload))

    def _next_id(self) -> int:
        with self._lock:
            value = self._message_id
            self._message_id += 1
            return value

    def _handle_message(self, raw_message) -> None:
        try:
            data = json.loads(raw_message)
        except Exception:
            return
        now = time.time()
        msg_type = data.get("type")
        message = data.get("msg") or {}
        with self._lock:
            self._last_message_at = now
        if msg_type == "orderbook_snapshot":
            self._handle_book_snapshot(message, now)
        elif msg_type == "orderbook_delta":
            self._handle_book_delta(message, now)
        elif msg_type == "trade":
            self._handle_trade(message, now)
        elif msg_type in {"market_lifecycle_v2", "market_lifecycle"}:
            ticker = message.get("market_ticker") or message.get("ticker")
            if ticker:
                with self._lock:
                    self._market_status[ticker] = dict(message)
        elif msg_type == "error":
            with self._lock:
                self._last_error = f"WS error {message.get('code')}: {message.get('msg')}"

    def _handle_book_snapshot(self, message: dict, now: float) -> None:
        ticker = message.get("market_ticker")
        if not ticker:
            return
        yes_levels = message.get("yes_dollars_fp") or message.get("yes_dollars") or []
        no_levels = message.get("no_dollars_fp") or message.get("no_dollars") or []
        yes = self._level_map(yes_levels)
        no = self._level_map(no_levels)
        with self._lock:
            self._books[ticker] = {"yes": yes, "no": no, "updated_at": now}
            self._book_events[ticker].clear()
            self._last_orderbook_at = now

    def _handle_book_delta(self, message: dict, now: float) -> None:
        ticker = message.get("market_ticker")
        side = message.get("side")
        price = dollars_to_cents(message.get("price_dollars"))
        try:
            delta = float(message.get("delta_fp", message.get("delta", 0)) or 0)
        except (TypeError, ValueError):
            delta = 0.0
        if not ticker or side not in {"yes", "no"} or price is None:
            return
        price = round(float(price), 4)
        event_ts = parse_ts(message.get("ts_ms")) or parse_ts(message.get("ts")) or now
        with self._lock:
            book = self._books.setdefault(ticker, {"yes": {}, "no": {}, "updated_at": now})
            side_book = book[side]
            previous_qty = float(side_book.get(price, 0.0))
            best_before = max(side_book, default=None)
            new_qty = previous_qty + delta
            if new_qty <= 0:
                side_book.pop(price, None)
            else:
                side_book[price] = new_qty
            best_after = max(side_book, default=None)
            book["updated_at"] = now
            self._book_events[ticker].append({
                "ts": float(event_ts),
                "received_at": now,
                "side": side,
                "price_cents": price,
                "delta": delta,
                "quantity_before": previous_qty,
                "quantity_after": max(0.0, new_qty),
                "at_best_before": best_before is not None and price == best_before,
                "at_best_after": best_after is not None and price == best_after,
            })
            self._last_orderbook_at = now

    def _handle_trade(self, message: dict, now: float) -> None:
        ticker = message.get("market_ticker") or message.get("ticker")
        yes_cents = dollars_to_cents(message.get("yes_price_dollars"))
        try:
            count = float(message.get("count_fp", message.get("count", 0)) or 0)
        except (TypeError, ValueError):
            count = 0.0
        ts = parse_ts(message.get("ts_ms")) or parse_ts(message.get("ts")) or now
        trade_id = message.get("trade_id") or f"{ticker}:{ts}:{yes_cents}:{count}"
        if not ticker or yes_cents is None:
            return
        row = {
            "id": trade_id,
            "ts": ts,
            "created_time": message.get("created_time", ""),
            "yes_cents": round(float(yes_cents), 4),
            "count": count,
            "taker_side": normalized_taker_side(message),
            "is_block": bool(message.get("is_block_trade", False)),
        }
        with self._lock:
            trades = self._trades[ticker]
            if not trades or trades[-1].get("id") != trade_id:
                trades.append(row)
            self._last_trade_at = now

    @staticmethod
    def _level_map(levels) -> dict:
        out = {}
        for level in levels or []:
            try:
                price = dollars_to_cents(level[0])
                qty = float(level[1])
            except Exception:
                continue
            if price is not None and qty > 0:
                out[round(float(price), 4)] = qty
        return out


_feed: KalshiWebSocketFeed | None = None
_feed_lock = threading.Lock()


def get_feed() -> KalshiWebSocketFeed:
    global _feed
    with _feed_lock:
        if _feed is None:
            _feed = KalshiWebSocketFeed()
        return _feed
