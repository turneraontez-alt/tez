"""Kraken Spot Level 3 research collector.

Kraken L3 is an authenticated websocket feed that exposes individual order IDs
inside the visible depth band. This collector is research-only and stores
compact summaries by default. Raw event rows are opt-in because L3 event volume
can grow quickly.
"""
from __future__ import annotations

import asyncio
import base64
from collections import defaultdict, deque
from dataclasses import dataclass
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterable
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

try:
    import websockets

    _HAVE_WS = True
except Exception:  # pragma: no cover - optional dependency guard
    websockets = None
    _HAVE_WS = False


KRAKEN_WS = "wss://ws-l3.kraken.com/v2"
KRAKEN_REST = "https://api.kraken.com"
TOKEN_PATH = "/0/private/GetWebSocketsToken"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kraken_l3_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    symbol TEXT NOT NULL,
    checksum INTEGER,
    book_age_seconds REAL,
    last_message_age_seconds REAL,
    best_bid REAL,
    best_ask REAL,
    mid REAL,
    spread_bps REAL,
    bid_order_count INTEGER,
    ask_order_count INTEGER,
    bid_level_count INTEGER,
    ask_level_count INTEGER,
    bid_depth_top REAL,
    ask_depth_top REAL,
    bid_depth_levels REAL,
    ask_depth_levels REAL,
    bid_notional_levels REAL,
    ask_notional_levels REAL,
    bid_order_count_levels INTEGER,
    ask_order_count_levels INTEGER,
    avg_bid_order_size_levels REAL,
    avg_ask_order_size_levels REAL,
    depth_imbalance REAL,
    bid_levels_json TEXT,
    ask_levels_json TEXT,
    add_count_5s INTEGER,
    update_count_5s INTEGER,
    delete_count_5s INTEGER,
    trade_count_5s INTEGER,
    cancel_to_add_5s REAL,
    add_count_15s INTEGER,
    update_count_15s INTEGER,
    delete_count_15s INTEGER,
    trade_count_15s INTEGER,
    cancel_to_add_15s REAL,
    add_count_60s INTEGER,
    update_count_60s INTEGER,
    delete_count_60s INTEGER,
    trade_count_60s INTEGER,
    cancel_to_add_60s REAL,
    matched_buy_notional_60s REAL,
    matched_sell_notional_60s REAL,
    net_matched_buy_notional_60s REAL
);
CREATE INDEX IF NOT EXISTS idx_kraken_l3_summary_symbol_time
    ON kraken_l3_summaries(symbol, created_at);

CREATE TABLE IF NOT EXISTS kraken_l3_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    symbol TEXT NOT NULL,
    checksum INTEGER,
    event_type TEXT NOT NULL,
    order_id TEXT,
    side TEXT,
    price REAL,
    size REAL,
    notional REAL,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_kraken_l3_events_symbol_time
    ON kraken_l3_events(symbol, created_at);
"""


@dataclass
class KrakenL3Order:
    side: str
    price: float
    size: float


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.environ.get(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(float(os.environ.get(name, str(default)) or default)))
    except (TypeError, ValueError):
        return default


def _f(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return out


def _i(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _enabled() -> bool:
    return _env_bool("Q15_KRAKEN_L3_ENABLED", False)


def _db_path() -> str:
    return os.environ.get("Q15_KRAKEN_L3_DB", "data/q15_kraken_l3_v1.sqlite3")


def _record_seconds() -> float:
    return _env_float("Q15_KRAKEN_L3_RECORD_SECONDS", 5.0, minimum=1.0)


def _summary_levels() -> int:
    return _env_int("Q15_KRAKEN_L3_SUMMARY_LEVELS", 10, minimum=1)


def _depth() -> int:
    raw = _env_int("Q15_KRAKEN_L3_DEPTH", 10, minimum=10)
    return 1000 if raw >= 1000 else 100 if raw >= 100 else 10


def _ws_max_size() -> int:
    return _env_int("Q15_KRAKEN_L3_WS_MAX_SIZE_BYTES", 64 * 1024 * 1024, minimum=1024 * 1024)


def _snapshot_reconnect_seconds() -> float:
    return _env_float("Q15_KRAKEN_L3_SNAPSHOT_RECONNECT_SECONDS", 300.0, minimum=30.0)


def _raw_events_enabled() -> bool:
    return _env_bool("Q15_KRAKEN_L3_RAW_EVENTS_ENABLED", False)


def _summary_retention_days() -> float:
    return _env_float("Q15_KRAKEN_L3_SUMMARY_RETENTION_DAYS", 7.0, minimum=0.0)


def _raw_event_retention_days() -> float:
    return _env_float("Q15_KRAKEN_L3_RAW_EVENT_RETENTION_DAYS", 1.0, minimum=0.0)


def _configured_symbols() -> list[str]:
    raw = os.environ.get("Q15_KRAKEN_L3_SYMBOLS", "").strip()
    if raw:
        symbols = [p.strip().upper().replace("-", "/") for p in raw.split(",") if p.strip()]
    else:
        symbols = ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "DOGE/USD", "BNB/USD", "HYPE/USD"]
    out: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        symbol = {"XBT/USD": "BTC/USD", "XDG/USD": "DOGE/USD"}.get(symbol, symbol)
        if symbol and symbol not in seen:
            out.append(symbol)
            seen.add(symbol)
    return out


def _credential(name: str, *aliases: str) -> str | None:
    for key in (name, *aliases):
        value = os.environ.get(key)
        if value:
            return value.strip()
    return None


def _credentials() -> dict[str, str | None]:
    return {
        "key": _credential("KRAKEN_API_KEY", "KRAKEN_SPOT_API_KEY"),
        "secret": _credential("KRAKEN_API_SECRET", "KRAKEN_SPOT_API_SECRET"),
    }


def _has_credentials() -> bool:
    creds = _credentials()
    return bool(creds["key"] and creds["secret"])


def sign_kraken_private_request(path: str, data: dict[str, Any]) -> str:
    secret = _credentials().get("secret")
    if not secret:
        raise ValueError("missing Kraken API secret")
    post_data = urlencode(data)
    encoded = (str(data.get("nonce") or "") + post_data).encode("utf-8")
    message = path.encode("utf-8") + hashlib.sha256(encoded).digest()
    digest = hmac.new(base64.b64decode(secret), message, hashlib.sha512).digest()
    return base64.b64encode(digest).decode("ascii")


def kraken_private_headers(path: str, data: dict[str, Any]) -> dict[str, str]:
    key = _credentials().get("key")
    if not key:
        raise ValueError("missing Kraken API key")
    return {
        "API-Key": key,
        "API-Sign": sign_kraken_private_request(path, data),
        "User-Agent": "q15-kraken-l3-research",
    }


def fetch_kraken_ws_token() -> str:
    data = {"nonce": str(int(time.time() * 1000))}
    resp = requests.post(
        f"{KRAKEN_REST}{TOKEN_PATH}",
        headers=kraken_private_headers(TOKEN_PATH, data),
        data=data,
        timeout=(5, 15),
    )
    resp.raise_for_status()
    payload = resp.json()
    errors = payload.get("error") or []
    if errors:
        raise RuntimeError("; ".join(str(x) for x in errors))
    token = (payload.get("result") or {}).get("token")
    if not token:
        raise RuntimeError("Kraken token response did not include token")
    return str(token)


def kraken_l3_subscribe_payload(
    symbols: Iterable[str],
    *,
    token: str,
    depth: int | None = None,
    snapshot: bool = True,
) -> dict[str, Any]:
    return {
        "method": "subscribe",
        "params": {
            "channel": "level3",
            "symbol": sorted(set(symbols)),
            "depth": depth or _depth(),
            "snapshot": bool(snapshot),
            "token": token,
        },
    }


class KrakenL3Collector:
    """Authenticated Kraken L3 book tracker with compact summary persistence."""

    def __init__(self, symbols: Iterable[str] | None = None, db_path: str | None = None) -> None:
        self.symbols = list(symbols or _configured_symbols())
        self.db_path = db_path or _db_path()
        self.record_seconds = _record_seconds()
        self.summary_levels = _summary_levels()
        self.depth = _depth()
        self.snapshot_reconnect_seconds = _snapshot_reconnect_seconds()
        self.raw_events_enabled = _raw_events_enabled()
        self.summary_retention_days = _summary_retention_days()
        self.raw_event_retention_days = _raw_event_retention_days()
        self._lock = threading.Lock()
        self._orders: dict[str, dict[str, KrakenL3Order]] = {s: {} for s in self.symbols}
        self._levels: dict[str, dict[str, dict[float, list[float]]]] = {
            s: {"buy": {}, "sell": {}} for s in self.symbols
        }
        self._events: dict[str, deque[dict[str, Any]]] = {s: deque() for s in self.symbols}
        self._raw_events: list[dict[str, Any]] = []
        self._checksum: dict[str, int] = {}
        self._book_ts: dict[str, float] = {}
        self._snapshot_loaded: dict[str, bool] = {s: False for s in self.symbols}
        self._last_message_at: float | None = None
        self._last_record_at: float | None = None
        self._last_error: dict[str, str] = {}
        self._connected = False
        self._records_written = 0
        self._events_seen = 0
        self._depth_truncations = 0
        self._crossed_book_drops = 0
        self._resync_requested = False
        self._last_prune_at = 0.0
        self._conn: sqlite3.Connection | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if not _HAVE_WS:
            logger.warning("Kraken L3 disabled: `pip install websockets`")
            return
        if not _has_credentials():
            logger.warning("Kraken L3 not started: missing Kraken API credentials")
            return
        if not self.symbols:
            logger.warning("Kraken L3 not started: no symbols configured")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._thread_main, name="kraken-l3", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def health(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            book_age = {symbol: round(now - ts, 3) for symbol, ts in self._book_ts.items() if ts}
            return {
                "enabled": _enabled(),
                "have_ws": _HAVE_WS,
                "authenticated": _has_credentials(),
                "symbols": list(self.symbols),
                "db_path": self.db_path,
                "connected": self._connected,
                "snapshot_loaded": dict(self._snapshot_loaded),
                "checksum": dict(self._checksum),
                "book_age_seconds": book_age,
                "last_message_age_seconds": (
                    round(now - self._last_message_at, 3) if self._last_message_at else None
                ),
                "last_record_age_seconds": (
                    round(now - self._last_record_at, 3) if self._last_record_at else None
                ),
                "records_written": self._records_written,
                "events_seen": self._events_seen,
                "depth_truncations": self._depth_truncations,
                "depth": self.depth,
                "summary_levels": self.summary_levels,
                "snapshot_reconnect_seconds": self.snapshot_reconnect_seconds,
                "raw_events_enabled": self.raw_events_enabled,
                "summary_retention_days": self.summary_retention_days,
                "raw_event_retention_days": self.raw_event_retention_days,
                "crossed_book_drops": self._crossed_book_drops,
                "estimated_summary_rows_per_day": self._estimated_summary_rows_per_day(),
                "estimated_summary_mb_per_day": self._estimated_summary_mb_per_day(),
                "last_error": dict(self._last_error),
                "status": self._status_locked(),
            }

    def _status_locked(self) -> str:
        if not _enabled():
            return "disabled"
        if not _HAVE_WS:
            return "websockets_missing"
        if not _has_credentials():
            return "missing_credentials"
        if not self.symbols:
            return "no_symbols"
        if self._connected:
            return "connected"
        return "starting"

    def _estimated_summary_rows_per_day(self) -> int:
        if self.record_seconds <= 0:
            return 0
        return int((86400.0 / self.record_seconds) * max(1, len(self.symbols)))

    def _estimated_summary_mb_per_day(self) -> float:
        # Compact rows store two small JSON level arrays. This is intentionally a
        # rough planning number; SQLite page overhead and WAL rotation vary.
        bytes_per_row = 1600 + (max(1, self.summary_levels) * 110)
        return round((self._estimated_summary_rows_per_day() * bytes_per_row) / (1024 * 1024), 2)

    # --- book accounting -------------------------------------------------
    def _replace_book(self, symbol: str, *, bids: Iterable[Any], asks: Iterable[Any], checksum: int | None) -> None:
        orders: dict[str, KrakenL3Order] = {}
        levels = {"buy": {}, "sell": {}}
        for side, rows in (("buy", bids), ("sell", asks)):
            for row in rows:
                order_id, price, size = self._parse_order_row(row)
                if not order_id or price is None or size is None or price <= 0 or size <= 0:
                    continue
                orders[order_id] = KrakenL3Order(side=side, price=price, size=size)
                self._adjust_level_map(levels[side], price, size, 1)
        now = time.time()
        with self._lock:
            self._orders[symbol] = orders
            self._levels[symbol] = levels
            if checksum is not None:
                self._checksum[symbol] = checksum
            self._book_ts[symbol] = now
            self._snapshot_loaded[symbol] = True
            self._last_message_at = now

    @staticmethod
    def _parse_order_row(row: Any) -> tuple[str | None, float | None, float | None]:
        if isinstance(row, dict):
            order_id = row.get("order_id") or row.get("orderid") or row.get("id")
            price = _f(row.get("limit_price") or row.get("price"))
            size = _f(row.get("order_qty") or row.get("qty") or row.get("size"))
            return (str(order_id) if order_id else None, price, size)
        if isinstance(row, (list, tuple)) and len(row) >= 3:
            order_id = str(row[0]) if row[0] else None
            price = _f(row[1])
            size = _f(row[2])
            return order_id, price, size
        return None, None, None

    @staticmethod
    def _adjust_level_map(levels: dict[float, list[float]], price: float, qty_delta: float, count_delta: int) -> None:
        row = levels.setdefault(price, [0.0, 0.0])
        row[0] += qty_delta
        row[1] += count_delta
        if row[0] <= 1e-12 or row[1] <= 0:
            levels.pop(price, None)

    def _upsert_order_locked(self, symbol: str, order_id: str, side: str, price: float, size: float) -> str:
        existing = self._orders.setdefault(symbol, {}).get(order_id)
        if existing:
            self._adjust_level_map(self._levels[symbol][existing.side], existing.price, -existing.size, -1)
            event_type = "update"
        else:
            event_type = "add"
        self._orders[symbol][order_id] = KrakenL3Order(side=side, price=price, size=size)
        self._adjust_level_map(self._levels[symbol][side], price, size, 1)
        return event_type

    def _remove_order_locked(self, symbol: str, order_id: str) -> KrakenL3Order | None:
        order = self._orders.setdefault(symbol, {}).pop(order_id, None)
        if order:
            self._adjust_level_map(self._levels[symbol][order.side], order.price, -order.size, -1)
        return order

    def _truncate_book_locked(self, symbol: str) -> None:
        """Drop orders at price levels that have fallen outside subscribed depth.

        Kraken does not emit deletes for levels pushed out of scope. Retaining
        those orders creates a stale, eventually crossed local book and forces
        repeated full reconnects. The feed contract requires truncation after
        every update.
        """
        levels = self._levels.get(symbol) or {}
        keep_by_side: dict[str, set[float]] = {}
        for side, reverse in (("buy", True), ("sell", False)):
            side_levels = levels.get(side) or {}
            keep_by_side[side] = set(
                sorted(side_levels, reverse=reverse)[: self.depth]
            )
        stale_ids = [
            order_id
            for order_id, order in self._orders.get(symbol, {}).items()
            if order.price not in keep_by_side.get(order.side, set())
        ]
        for order_id in stale_ids:
            self._remove_order_locked(symbol, order_id)
        self._depth_truncations += len(stale_ids)

    # --- message parsing -------------------------------------------------
    def handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        if data.get("method") == "subscribe":
            if data.get("success") is False:
                with self._lock:
                    self._last_error["kraken_subscribe"] = str(data.get("error") or data)[:200]
            return
        if data.get("channel") == "heartbeat":
            with self._lock:
                self._last_message_at = time.time()
            return
        if data.get("channel") != "level3":
            return
        typ = str(data.get("type") or "").lower()
        for item in data.get("data") or []:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").upper()
            if symbol not in self._orders:
                continue
            checksum = _i(item.get("checksum"))
            if typ == "snapshot":
                self._replace_book(
                    symbol,
                    bids=item.get("bids") or [],
                    asks=item.get("asks") or [],
                    checksum=checksum,
                )
            else:
                self._apply_update(symbol, item, checksum)

    def _apply_update(self, symbol: str, item: dict[str, Any], checksum: int | None) -> None:
        now = time.time()
        produced: list[dict[str, Any]] = []
        with self._lock:
            if checksum is not None:
                self._checksum[symbol] = checksum
            self._last_message_at = now
            self._book_ts[symbol] = now
            for side, key in (("buy", "bids"), ("sell", "asks")):
                for row in item.get(key) or []:
                    event = self._apply_order_update_locked(symbol, side, row, now, checksum)
                    if event:
                        produced.append(event)
            self._truncate_book_locked(symbol)
            events = self._events.setdefault(symbol, deque())
            for event in produced:
                self._events_seen += 1
                events.append(event)
                if self.raw_events_enabled:
                    self._raw_events.append(event)
            cutoff = now - 120.0
            while events and float(events[0].get("created_at") or 0.0) < cutoff:
                events.popleft()

    def _apply_order_update_locked(
        self,
        symbol: str,
        side: str,
        row: Any,
        now: float,
        checksum: int | None,
    ) -> dict[str, Any] | None:
        order_id, price, size = self._parse_order_row(row)
        if not order_id:
            return None
        raw_event = str(row.get("event") or row.get("type") or "").lower() if isinstance(row, dict) else ""
        remove = (size is not None and size <= 0) or raw_event in {"delete", "deleted", "remove", "cancel", "canceled"}
        trade = raw_event in {"trade", "match", "fill", "filled"}
        if remove:
            order = self._remove_order_locked(symbol, order_id)
            return self._event(
                now,
                symbol,
                checksum,
                "trade" if trade else "delete",
                order_id,
                side,
                order.price if order else price,
                order.size if order else size,
                raw_event or None,
            )
        if price is None or size is None or price <= 0 or size <= 0:
            return None
        event_type = "trade" if trade else self._upsert_order_locked(symbol, order_id, side, price, size)
        return self._event(now, symbol, checksum, event_type, order_id, side, price, size, raw_event or None)

    @staticmethod
    def _event(
        created_at: float,
        symbol: str,
        checksum: int | None,
        event_type: str,
        order_id: str | None = None,
        side: str | None = None,
        price: float | None = None,
        size: float | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return {
            "created_at": created_at,
            "symbol": symbol,
            "checksum": checksum,
            "event_type": event_type,
            "order_id": order_id,
            "side": side,
            "price": price,
            "size": size,
            "notional": (price * size) if price is not None and size is not None else None,
            "reason": reason,
        }

    # --- recording -------------------------------------------------------
    def record_once(self) -> int:
        now = time.time()
        with self._lock:
            rows = [self._summary_locked(symbol, now) for symbol in self.symbols]
            rows = [row for row in rows if row is not None]
            raw_events = list(self._raw_events)
            self._raw_events.clear()
        if not rows and not raw_events:
            return 0
        conn = self._connect()
        with self._lock:
            for row in rows:
                self._insert_summary_locked(conn, row)
            if self.raw_events_enabled:
                for event in raw_events:
                    self._insert_event_locked(conn, event)
            self._prune_locked(conn, now)
            conn.commit()
            self._records_written += len(rows)
            self._last_record_at = now
        return len(rows)

    def latest_snapshot(
        self, symbol: str, *, now: float | None = None
    ) -> dict[str, Any] | None:
        """Return a lock-consistent in-memory book without network or database I/O."""
        observed_at = time.time() if now is None else float(now)
        normalized = str(symbol or "").upper().replace("-", "/")
        with self._lock:
            row = self._summary_locked(normalized, observed_at)
            book_timestamp = self._book_ts.get(normalized)
            if row is None or book_timestamp is None:
                return None
            row["book_timestamp"] = book_timestamp
            row["book_age_seconds"] = max(0.0, observed_at - book_timestamp)
            return row

    def latest_top_snapshot(
        self, symbol: str, *, now: float | None = None
    ) -> dict[str, Any] | None:
        """Return only top-of-book fields so live consumers never stall ingestion."""
        observed_at = time.time() if now is None else float(now)
        normalized = str(symbol or "").upper().replace("-", "/")
        with self._lock:
            if not self._snapshot_loaded.get(normalized):
                return None
            levels = self._levels.get(normalized) or {}
            bids = levels.get("buy") or {}
            asks = levels.get("sell") or {}
            book_timestamp = self._book_ts.get(normalized)
            if not bids or not asks or book_timestamp is None:
                return None
            best_bid = max(bids)
            best_ask = min(asks)
            if best_bid >= best_ask:
                self._crossed_book_drops += 1
                self._resync_requested = True
                self._snapshot_loaded[normalized] = False
                self._last_error[f"{normalized}_crossed_book"] = (
                    f"{best_bid}>={best_ask}"
                )
                return None
            bid_size = float(bids[best_bid][0])
            ask_size = float(asks[best_ask][0])
            depth = bid_size + ask_size
            mid = (best_bid + best_ask) / 2.0
            return {
                "symbol": normalized,
                "checksum": self._checksum.get(normalized),
                "sample_timestamp": observed_at,
                "book_timestamp": book_timestamp,
                "book_age_seconds": max(0.0, observed_at - book_timestamp),
                "transport_connected": self._connected,
                "last_message_age_seconds": (
                    max(0.0, observed_at - self._last_message_at)
                    if self._last_message_at is not None
                    else None
                ),
                "best_bid": best_bid,
                "best_ask": best_ask,
                "mid": mid,
                "spread_bps": (best_ask - best_bid) / mid * 10_000.0,
                "bid_depth_top": bid_size,
                "ask_depth_top": ask_size,
                "depth_imbalance": (
                    (bid_size - ask_size) / depth if depth > 0 else None
                ),
            }

    def _summary_locked(self, symbol: str, now: float) -> dict[str, Any] | None:
        if not self._snapshot_loaded.get(symbol):
            return None
        levels = self._levels.get(symbol) or {}
        bids = self._top_levels(levels.get("buy") or {}, reverse=True)
        asks = self._top_levels(levels.get("sell") or {}, reverse=False)
        if not bids or not asks:
            return None
        best_bid = bids[0][0]
        best_ask = asks[0][0]
        if best_bid >= best_ask:
            self._crossed_book_drops += 1
            self._resync_requested = True
            self._last_error[f"{symbol}_crossed_book"] = f"{best_bid}>={best_ask}"
            self._snapshot_loaded[symbol] = False
            return None
        mid = (best_bid + best_ask) / 2.0
        bid_depth = sum(row[1] for row in bids)
        ask_depth = sum(row[1] for row in asks)
        bid_orders = sum(int(row[2]) for row in bids)
        ask_orders = sum(int(row[2]) for row in asks)
        denom = bid_depth + ask_depth
        events = list(self._events.get(symbol) or [])
        row = {
            "created_at": now,
            "symbol": symbol,
            "checksum": self._checksum.get(symbol),
            "book_age_seconds": (
                round(now - self._book_ts[symbol], 3) if self._book_ts.get(symbol) else None
            ),
            "last_message_age_seconds": (
                round(now - self._last_message_at, 3) if self._last_message_at else None
            ),
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
            "spread_bps": ((best_ask - best_bid) / mid * 10_000.0) if mid else None,
            "bid_order_count": sum(1 for order in self._orders.get(symbol, {}).values() if order.side == "buy"),
            "ask_order_count": sum(1 for order in self._orders.get(symbol, {}).values() if order.side == "sell"),
            "bid_level_count": len(levels.get("buy") or {}),
            "ask_level_count": len(levels.get("sell") or {}),
            "bid_depth_top": bids[0][1],
            "ask_depth_top": asks[0][1],
            "bid_depth_levels": bid_depth,
            "ask_depth_levels": ask_depth,
            "bid_notional_levels": sum(row[0] * row[1] for row in bids),
            "ask_notional_levels": sum(row[0] * row[1] for row in asks),
            "bid_order_count_levels": bid_orders,
            "ask_order_count_levels": ask_orders,
            "avg_bid_order_size_levels": bid_depth / bid_orders if bid_orders else None,
            "avg_ask_order_size_levels": ask_depth / ask_orders if ask_orders else None,
            "depth_imbalance": ((bid_depth - ask_depth) / denom) if denom > 0 else None,
            "bid_levels_json": json.dumps(bids, separators=(",", ":")),
            "ask_levels_json": json.dumps(asks, separators=(",", ":")),
        }
        row.update(self._event_sums(events, now, 5.0, "5s"))
        row.update(self._event_sums(events, now, 15.0, "15s"))
        row.update(self._event_sums(events, now, 60.0, "60s"))
        return row

    def _top_levels(self, levels: dict[float, list[float]], *, reverse: bool) -> list[list[float]]:
        limit = min(self.summary_levels, self.depth)
        ordered = sorted(levels.items(), key=lambda item: item[0], reverse=reverse)[:limit]
        return [[price, values[0], values[1]] for price, values in ordered]

    @staticmethod
    def _event_sums(events: list[dict[str, Any]], now: float, window: float, suffix: str) -> dict[str, Any]:
        cutoff = now - window
        counts = defaultdict(int)
        buy_notional = sell_notional = 0.0
        for event in events:
            if float(event.get("created_at") or 0.0) < cutoff:
                continue
            typ = str(event.get("event_type") or "")
            counts[typ] += 1
            if typ == "trade":
                notional = float(event.get("notional") or 0.0)
                if event.get("side") == "buy":
                    buy_notional += notional
                elif event.get("side") == "sell":
                    sell_notional += notional
        adds = counts["add"]
        deletes = counts["delete"]
        return {
            f"add_count_{suffix}": adds,
            f"update_count_{suffix}": counts["update"],
            f"delete_count_{suffix}": deletes,
            f"trade_count_{suffix}": counts["trade"],
            f"cancel_to_add_{suffix}": (deletes / adds) if adds else None,
            **(
                {
                    "matched_buy_notional_60s": buy_notional,
                    "matched_sell_notional_60s": sell_notional,
                    "net_matched_buy_notional_60s": buy_notional - sell_notional,
                }
                if suffix == "60s"
                else {}
            ),
        }

    def _connect(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is not None:
                return self._conn
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=15.0)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            return self._conn

    @staticmethod
    def _insert_summary_locked(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        cols = (
            "created_at", "symbol", "checksum", "book_age_seconds",
            "last_message_age_seconds", "best_bid", "best_ask", "mid",
            "spread_bps", "bid_order_count", "ask_order_count", "bid_level_count",
            "ask_level_count", "bid_depth_top", "ask_depth_top", "bid_depth_levels",
            "ask_depth_levels", "bid_notional_levels", "ask_notional_levels",
            "bid_order_count_levels", "ask_order_count_levels",
            "avg_bid_order_size_levels", "avg_ask_order_size_levels",
            "depth_imbalance", "bid_levels_json", "ask_levels_json",
            "add_count_5s", "update_count_5s", "delete_count_5s",
            "trade_count_5s", "cancel_to_add_5s",
            "add_count_15s", "update_count_15s", "delete_count_15s",
            "trade_count_15s", "cancel_to_add_15s",
            "add_count_60s", "update_count_60s", "delete_count_60s",
            "trade_count_60s", "cancel_to_add_60s",
            "matched_buy_notional_60s", "matched_sell_notional_60s",
            "net_matched_buy_notional_60s",
        )
        conn.execute(
            f"INSERT INTO kraken_l3_summaries({','.join(cols)}) "
            f"VALUES({','.join('?' for _ in cols)})",
            [row.get(col) for col in cols],
        )

    @staticmethod
    def _insert_event_locked(conn: sqlite3.Connection, event: dict[str, Any]) -> None:
        cols = (
            "created_at", "symbol", "checksum", "event_type", "order_id",
            "side", "price", "size", "notional", "reason",
        )
        conn.execute(
            f"INSERT INTO kraken_l3_events({','.join(cols)}) "
            f"VALUES({','.join('?' for _ in cols)})",
            [event.get(col) for col in cols],
        )

    def _prune_locked(self, conn: sqlite3.Connection, now: float) -> None:
        if now - self._last_prune_at < 600.0:
            return
        if self.summary_retention_days > 0:
            summary_cutoff = now - (self.summary_retention_days * 86400.0)
            conn.execute("DELETE FROM kraken_l3_summaries WHERE created_at < ?", (summary_cutoff,))
        if self.raw_event_retention_days > 0:
            event_cutoff = now - (self.raw_event_retention_days * 86400.0)
            conn.execute("DELETE FROM kraken_l3_events WHERE created_at < ?", (event_cutoff,))
        self._last_prune_at = now

    # --- networking ------------------------------------------------------
    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:  # pragma: no cover - thread guard
            logger.warning("Kraken L3 thread exited: %s", exc)

    async def _run(self) -> None:
        await asyncio.gather(self._provider_loop(), self._recorder_loop())

    async def _recorder_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.record_once()
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._last_error["recorder"] = str(exc)[:200]
                logger.warning("Kraken L3 recorder error: %s", exc)
            await asyncio.sleep(self.record_seconds)

    async def _provider_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                token = await asyncio.to_thread(fetch_kraken_ws_token)
                async with websockets.connect(
                    KRAKEN_WS,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_queue=5000,
                    max_size=_ws_max_size(),
                ) as socket:
                    with self._lock:
                        self._connected = True
                        self._last_error.pop("kraken", None)
                    await socket.send(json.dumps(kraken_l3_subscribe_payload(
                        self.symbols,
                        token=token,
                        depth=self.depth,
                    )))
                    backoff = 1.0
                    reconnect_at = time.monotonic() + self.snapshot_reconnect_seconds
                    while not self._stop.is_set():
                        if time.monotonic() >= reconnect_at:
                            raise RuntimeError("scheduled Kraken L3 snapshot refresh")
                        if self._take_resync_requested():
                            raise RuntimeError("Kraken L3 crossed book; refreshing snapshot")
                        try:
                            message = await asyncio.wait_for(socket.recv(), timeout=20.0)
                        except asyncio.TimeoutError:
                            continue
                        self.handle_message(message)
            except Exception as exc:
                with self._lock:
                    self._last_error["kraken"] = str(exc)[:200]
                logger.warning("Kraken L3 reconnecting after error: %s", exc)
            finally:
                with self._lock:
                    self._connected = False
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 1.8)

    def _take_resync_requested(self) -> bool:
        with self._lock:
            if not self._resync_requested:
                return False
            self._resync_requested = False
            return True


_feed: KrakenL3Collector | None = None
_feed_lock = threading.Lock()


def peek_kraken_l3_feed() -> KrakenL3Collector | None:
    """Return the running singleton without starting a collector."""
    return _feed


def get_kraken_l3_feed() -> KrakenL3Collector:
    global _feed
    with _feed_lock:
        if _feed is None:
            _feed = KrakenL3Collector()
            _feed.start()
        return _feed


def start_kraken_l3() -> None:
    """Start the optional Kraken L3 collector if enabled. Never raises."""
    if not _enabled():
        return
    try:
        get_kraken_l3_feed()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Kraken L3 start failed: %s", exc)


def kraken_l3_health() -> dict[str, Any]:
    info = {
        "enabled": _enabled(),
        "have_ws": _HAVE_WS,
        "authenticated": _has_credentials(),
        "symbols": _configured_symbols(),
        "db_path": _db_path(),
        "depth": _depth(),
        "summary_levels": _summary_levels(),
        "snapshot_reconnect_seconds": _snapshot_reconnect_seconds(),
        "raw_events_enabled": _raw_events_enabled(),
        "summary_retention_days": _summary_retention_days(),
        "raw_event_retention_days": _raw_event_retention_days(),
    }
    if not _enabled() or _feed is None:
        if _enabled() and not _has_credentials():
            info["status"] = "missing_credentials"
        return info
    try:
        info.update(_feed.health())
    except Exception as exc:  # pragma: no cover - defensive
        info["error"] = str(exc)[:200]
    return info
