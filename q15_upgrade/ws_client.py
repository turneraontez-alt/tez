from __future__ import annotations

import asyncio
import base64
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
        try:
            return websockets.connect(
                self.url,
                additional_headers=headers,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
                max_queue=10000,
            )
        except TypeError:  # websockets < 12
            return websockets.connect(
                self.url,
                extra_headers=headers,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
                max_queue=10000,
            )

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
        with self._lock:
            book = self._books.setdefault(ticker, {"yes": {}, "no": {}, "updated_at": now})
            new_qty = float(book[side].get(price, 0.0)) + delta
            if new_qty <= 0:
                book[side].pop(price, None)
            else:
                book[side][price] = new_qty
            book["updated_at"] = now
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
