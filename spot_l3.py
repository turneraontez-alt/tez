"""Coinbase Exchange Level 3 research collector.

This is intentionally separate from ``spot_depth.py``. The production L2
collector stays lightweight and public; this collector is authenticated,
research-only, and writes compact per-order-book summaries by default.
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

import requests

from spot_client import SPOT_SOURCES

logger = logging.getLogger(__name__)

try:
    import websockets

    _HAVE_WS = True
except Exception:  # pragma: no cover - optional dependency guard
    websockets = None
    _HAVE_WS = False


COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
COINBASE_REST = "https://api.exchange.coinbase.com"
VERIFY_PATH = "/users/self/verify"
BOOK_PATH_TEMPLATE = "/products/{product_id}/book?level=3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spot_l3_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    product_id TEXT NOT NULL,
    sequence INTEGER,
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
    open_count_5s INTEGER,
    done_count_5s INTEGER,
    cancel_count_5s INTEGER,
    change_count_5s INTEGER,
    match_count_5s INTEGER,
    cancel_to_open_5s REAL,
    open_count_15s INTEGER,
    done_count_15s INTEGER,
    cancel_count_15s INTEGER,
    change_count_15s INTEGER,
    match_count_15s INTEGER,
    cancel_to_open_15s REAL,
    open_count_60s INTEGER,
    done_count_60s INTEGER,
    cancel_count_60s INTEGER,
    change_count_60s INTEGER,
    match_count_60s INTEGER,
    cancel_to_open_60s REAL,
    matched_buy_notional_60s REAL,
    matched_sell_notional_60s REAL,
    net_matched_buy_notional_60s REAL
);
CREATE INDEX IF NOT EXISTS idx_spot_l3_summary_product_time
    ON spot_l3_summaries(product_id, created_at);

CREATE TABLE IF NOT EXISTS spot_l3_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    product_id TEXT NOT NULL,
    sequence INTEGER,
    event_type TEXT NOT NULL,
    order_id TEXT,
    side TEXT,
    price REAL,
    size REAL,
    notional REAL,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_spot_l3_events_product_time
    ON spot_l3_events(product_id, created_at);
"""


@dataclass
class L3Order:
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


def _side(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if text in {"buy", "bid"}:
        return "buy"
    if text in {"sell", "ask"}:
        return "sell"
    return None


def _enabled() -> bool:
    return _env_bool("Q15_SPOT_L3_ENABLED", False)


def _db_path() -> str:
    return os.environ.get("Q15_SPOT_L3_DB", "data/q15_spot_l3_v1.sqlite3")


def _record_seconds() -> float:
    return _env_float("Q15_SPOT_L3_RECORD_SECONDS", 5.0, minimum=1.0)


def _summary_levels() -> int:
    return _env_int("Q15_SPOT_L3_SUMMARY_LEVELS", 10, minimum=1)


def _ws_max_size() -> int:
    return _env_int("Q15_SPOT_L3_WS_MAX_SIZE_BYTES", 64 * 1024 * 1024, minimum=1024 * 1024)


def _raw_events_enabled() -> bool:
    return _env_bool("Q15_SPOT_L3_RAW_EVENTS_ENABLED", False)


def _configured_products() -> list[str]:
    raw = os.environ.get("Q15_SPOT_L3_PRODUCTS", "").strip()
    if raw:
        products = [p.strip().upper() for p in raw.split(",") if p.strip()]
    else:
        # Safe default if someone flips the feature on without narrowing scope.
        products = ["BTC-USD"]
    known = {sym for provider, sym, _quote in SPOT_SOURCES.values() if provider == "coinbase"}
    out: list[str] = []
    seen: set[str] = set()
    for product in products:
        if product in known and product not in seen:
            out.append(product)
            seen.add(product)
    return out


def _credential(name: str, *aliases: str) -> str | None:
    for key in (name, *aliases):
        value = os.environ.get(key)
        if value:
            return value.strip()
    return None


def _credentials() -> dict[str, str | None]:
    return {
        "key": _credential("COINBASE_EXCHANGE_API_KEY", "COINBASE_API_KEY"),
        "secret": _credential("COINBASE_EXCHANGE_API_SECRET", "COINBASE_API_SECRET"),
        "passphrase": _credential("COINBASE_EXCHANGE_API_PASSPHRASE", "COINBASE_API_PASSPHRASE"),
    }


def _has_credentials() -> bool:
    creds = _credentials()
    return bool(creds["key"] and creds["secret"] and creds["passphrase"])


def _secret_bytes(secret: str) -> bytes:
    try:
        return base64.b64decode(secret, validate=True)
    except Exception:
        return secret.encode("utf-8")


def sign_coinbase_message(timestamp: str, method: str, request_path: str, body: str = "") -> str:
    secret = _credentials().get("secret")
    if not secret:
        raise ValueError("missing Coinbase Exchange API secret")
    msg = f"{timestamp}{method.upper()}{request_path}{body}".encode("utf-8")
    digest = hmac.new(_secret_bytes(secret), msg, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def coinbase_auth_headers(method: str, request_path: str, body: str = "") -> dict[str, str]:
    creds = _credentials()
    if not (creds["key"] and creds["secret"] and creds["passphrase"]):
        raise ValueError("missing Coinbase Exchange API credentials")
    timestamp = str(time.time())
    return {
        "CB-ACCESS-KEY": str(creds["key"]),
        "CB-ACCESS-SIGN": sign_coinbase_message(timestamp, method, request_path, body),
        "CB-ACCESS-TIMESTAMP": timestamp,
        "CB-ACCESS-PASSPHRASE": str(creds["passphrase"]),
        "User-Agent": "q15-spot-l3-research",
    }


def coinbase_l3_subscribe_payload(products: Iterable[str], timestamp: str | None = None) -> dict[str, Any]:
    creds = _credentials()
    if not (creds["key"] and creds["secret"] and creds["passphrase"]):
        raise ValueError("missing Coinbase Exchange API credentials")
    ts = timestamp or str(time.time())
    return {
        "type": "subscribe",
        "product_ids": sorted(set(products)),
        "channels": ["level3"],
        "signature": sign_coinbase_message(ts, "GET", VERIFY_PATH),
        "key": creds["key"],
        "passphrase": creds["passphrase"],
        "timestamp": ts,
    }


class CoinbaseL3Collector:
    """Authenticated Coinbase L3 book tracker with compact summary persistence."""

    def __init__(self, products: Iterable[str] | None = None, db_path: str | None = None) -> None:
        self.products = list(products or _configured_products())
        self.db_path = db_path or _db_path()
        self.record_seconds = _record_seconds()
        self.summary_levels = _summary_levels()
        self.raw_events_enabled = _raw_events_enabled()
        self._lock = threading.Lock()
        self._orders: dict[str, dict[str, L3Order]] = {p: {} for p in self.products}
        self._levels: dict[str, dict[str, dict[float, list[float]]]] = {
            p: {"buy": {}, "sell": {}} for p in self.products
        }
        self._events: dict[str, deque[dict[str, Any]]] = {p: deque() for p in self.products}
        self._raw_events: list[dict[str, Any]] = []
        self._sequence: dict[str, int] = {}
        self._book_ts: dict[str, float] = {}
        self._snapshot_loaded: dict[str, bool] = {p: False for p in self.products}
        self._last_message_at: float | None = None
        self._last_record_at: float | None = None
        self._last_error: dict[str, str] = {}
        self._connected = False
        self._records_written = 0
        self._events_seen = 0
        self._conn: sqlite3.Connection | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pending_resync: set[str] = set()

    def start(self) -> None:
        if not _HAVE_WS:
            logger.warning("Coinbase L3 disabled: `pip install websockets`")
            return
        if not _has_credentials():
            logger.warning("Coinbase L3 not started: missing Coinbase Exchange API credentials")
            return
        if not self.products:
            logger.warning("Coinbase L3 not started: no Coinbase products configured")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._thread_main, name="spot-l3", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def health(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            book_age = {
                product: round(now - ts, 3)
                for product, ts in self._book_ts.items()
                if ts
            }
            return {
                "enabled": _enabled(),
                "have_ws": _HAVE_WS,
                "authenticated": _has_credentials(),
                "products": list(self.products),
                "db_path": self.db_path,
                "connected": self._connected,
                "snapshot_loaded": dict(self._snapshot_loaded),
                "sequence": dict(self._sequence),
                "book_age_seconds": book_age,
                "last_message_age_seconds": (
                    round(now - self._last_message_at, 3) if self._last_message_at else None
                ),
                "last_record_age_seconds": (
                    round(now - self._last_record_at, 3) if self._last_record_at else None
                ),
                "records_written": self._records_written,
                "events_seen": self._events_seen,
                "summary_levels": self.summary_levels,
                "raw_events_enabled": self.raw_events_enabled,
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
        if not self.products:
            return "no_products"
        if self._connected:
            return "connected"
        return "starting"

    # --- book accounting -------------------------------------------------
    def _replace_product_book(
        self,
        product_id: str,
        *,
        sequence: int | None,
        bids: Iterable[Any],
        asks: Iterable[Any],
        ts: float | None = None,
    ) -> None:
        orders: dict[str, L3Order] = {}
        levels = {"buy": {}, "sell": {}}
        for side, rows in (("buy", bids), ("sell", asks)):
            for row in rows:
                try:
                    price_raw, size_raw, order_id = row[:3]
                except (TypeError, ValueError):
                    continue
                price = _f(price_raw)
                size = _f(size_raw)
                oid = str(order_id or "")
                if not oid or price is None or size is None or price <= 0 or size <= 0:
                    continue
                orders[oid] = L3Order(side=side, price=price, size=size)
                self._adjust_level_map(levels[side], price, size, 1)
        with self._lock:
            self._orders[product_id] = orders
            self._levels[product_id] = levels
            if sequence is not None:
                self._sequence[product_id] = sequence
            self._book_ts[product_id] = ts or time.time()
            self._snapshot_loaded[product_id] = True

    @staticmethod
    def _adjust_level_map(levels: dict[float, list[float]], price: float, qty_delta: float, count_delta: int) -> None:
        row = levels.setdefault(price, [0.0, 0.0])
        row[0] += qty_delta
        row[1] += count_delta
        if row[0] <= 1e-12 or row[1] <= 0:
            levels.pop(price, None)

    def _add_order_locked(self, product_id: str, order_id: str, side: str, price: float, size: float) -> None:
        existing = self._orders.setdefault(product_id, {}).pop(order_id, None)
        if existing:
            self._adjust_level_map(self._levels[product_id][existing.side], existing.price, -existing.size, -1)
        self._orders[product_id][order_id] = L3Order(side=side, price=price, size=size)
        self._adjust_level_map(self._levels[product_id][side], price, size, 1)

    def _remove_order_locked(self, product_id: str, order_id: str) -> L3Order | None:
        order = self._orders.setdefault(product_id, {}).pop(order_id, None)
        if order:
            self._adjust_level_map(self._levels[product_id][order.side], order.price, -order.size, -1)
        return order

    def _reduce_order_locked(self, product_id: str, order_id: str, size: float) -> L3Order | None:
        order = self._orders.setdefault(product_id, {}).get(order_id)
        if not order:
            return None
        old_size = order.size
        new_size = max(0.0, old_size - size)
        self._adjust_level_map(self._levels[product_id][order.side], order.price, new_size - old_size, 0)
        if new_size <= 1e-12:
            self._orders[product_id].pop(order_id, None)
            self._adjust_level_map(self._levels[product_id][order.side], order.price, 0.0, -1)
        else:
            order.size = new_size
        return order

    def _change_order_locked(self, product_id: str, order_id: str, new_size: float) -> L3Order | None:
        order = self._orders.setdefault(product_id, {}).get(order_id)
        if not order:
            return None
        old_size = order.size
        if new_size <= 1e-12:
            return self._remove_order_locked(product_id, order_id)
        order.size = new_size
        self._adjust_level_map(self._levels[product_id][order.side], order.price, new_size - old_size, 0)
        return order

    # --- message parsing -------------------------------------------------
    def handle_coinbase_l3(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except Exception:
            return
        if isinstance(data, dict):
            typ = str(data.get("type") or "")
            if typ == "error":
                with self._lock:
                    self._last_error["coinbase_message"] = str(data.get("message") or data)[:200]
            return
        if not isinstance(data, list) or not data:
            return
        event_type = str(data[0] or "").lower()
        if event_type in {"open", "done", "match", "change", "noop"}:
            self._apply_l3_event(data)

    def _apply_l3_event(self, row: list[Any]) -> None:
        event_type = str(row[0]).lower()
        product_id = str(row[1]) if len(row) > 1 else ""
        if product_id not in self._orders:
            return
        sequence = _i(row[2]) if len(row) > 2 else None
        now = time.time()
        with self._lock:
            current = self._sequence.get(product_id)
            if sequence is not None and current is not None:
                if sequence <= current:
                    return
                if sequence > current + 1:
                    self._last_error[f"{product_id}_sequence_gap"] = f"{current}->{sequence}"
                    self._pending_resync.add(product_id)
                    return
            if sequence is not None:
                self._sequence[product_id] = sequence
            self._last_message_at = now
            self._book_ts[product_id] = now
            event = self._apply_l3_event_locked(product_id, event_type, row, now, sequence)
            if event:
                self._events_seen += 1
                events = self._events.setdefault(product_id, deque())
                events.append(event)
                cutoff = now - 120.0
                while events and float(events[0].get("created_at") or 0.0) < cutoff:
                    events.popleft()
                if self.raw_events_enabled:
                    self._raw_events.append(event)

    def _apply_l3_event_locked(
        self, product_id: str, event_type: str, row: list[Any], now: float, sequence: int | None
    ) -> dict[str, Any] | None:
        if event_type == "noop":
            return {
                "created_at": now,
                "product_id": product_id,
                "sequence": sequence,
                "event_type": "noop",
            }
        if event_type == "open":
            if len(row) < 7:
                return None
            order_id = str(row[3])
            side = _side(row[4])
            price = _f(row[5])
            size = _f(row[6])
            if not order_id or side is None or price is None or size is None or price <= 0 or size <= 0:
                return None
            self._add_order_locked(product_id, order_id, side, price, size)
            return self._event(now, product_id, sequence, "open", order_id, side, price, size)
        if event_type == "done":
            if len(row) < 4:
                return None
            order_id = str(row[3])
            order = self._remove_order_locked(product_id, order_id)
            side = order.side if order else (_side(row[4]) if len(row) > 4 else None)
            price = order.price if order else (_f(row[5]) if len(row) > 5 else None)
            size = order.size if order else (_f(row[6]) if len(row) > 6 else None)
            reason = str(row[-2]) if len(row) > 7 else None
            return self._event(now, product_id, sequence, "done", order_id, side, price, size, reason)
        if event_type == "match":
            if len(row) < 7:
                return None
            order_id = str(row[3])
            price = _f(row[5])
            size = _f(row[6])
            taker_side = _side(row[7]) if len(row) > 7 else None
            if size is None or size <= 0:
                return None
            order = self._reduce_order_locked(product_id, order_id, size)
            side = taker_side or (order.side if order else None)
            if price is None and order:
                price = order.price
            return self._event(now, product_id, sequence, "match", order_id, side, price, size)
        if event_type == "change":
            if len(row) < 6:
                return None
            order_id = str(row[3])
            numeric = [_f(x) for x in row[4:]]
            nums = [x for x in numeric if x is not None]
            if not nums:
                return None
            new_size = nums[-1]
            order = self._change_order_locked(product_id, order_id, new_size)
            return self._event(
                now,
                product_id,
                sequence,
                "change",
                order_id,
                order.side if order else None,
                order.price if order else None,
                new_size,
            )
        return None

    @staticmethod
    def _event(
        created_at: float,
        product_id: str,
        sequence: int | None,
        event_type: str,
        order_id: str | None = None,
        side: str | None = None,
        price: float | None = None,
        size: float | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return {
            "created_at": created_at,
            "product_id": product_id,
            "sequence": sequence,
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
            rows = [self._summary_locked(product, now) for product in self.products]
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
            conn.commit()
            self._records_written += len(rows)
            self._last_record_at = now
        return len(rows)

    def _summary_locked(self, product_id: str, now: float) -> dict[str, Any] | None:
        if not self._snapshot_loaded.get(product_id):
            return None
        levels = self._levels.get(product_id) or {}
        bids = self._top_levels(levels.get("buy") or {}, reverse=True)
        asks = self._top_levels(levels.get("sell") or {}, reverse=False)
        if not bids or not asks:
            return None
        best_bid = bids[0][0]
        best_ask = asks[0][0]
        mid = (best_bid + best_ask) / 2.0
        bid_depth = sum(row[1] for row in bids)
        ask_depth = sum(row[1] for row in asks)
        bid_orders = sum(int(row[2]) for row in bids)
        ask_orders = sum(int(row[2]) for row in asks)
        denom = bid_depth + ask_depth
        events = list(self._events.get(product_id) or [])
        row = {
            "created_at": now,
            "product_id": product_id,
            "sequence": self._sequence.get(product_id),
            "book_age_seconds": (
                round(now - self._book_ts[product_id], 3) if self._book_ts.get(product_id) else None
            ),
            "last_message_age_seconds": (
                round(now - self._last_message_at, 3) if self._last_message_at else None
            ),
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
            "spread_bps": ((best_ask - best_bid) / mid * 10_000.0) if mid else None,
            "bid_order_count": sum(1 for order in self._orders.get(product_id, {}).values() if order.side == "buy"),
            "ask_order_count": sum(1 for order in self._orders.get(product_id, {}).values() if order.side == "sell"),
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
        ordered = sorted(levels.items(), key=lambda item: item[0], reverse=reverse)[: self.summary_levels]
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
            if typ == "match":
                notional = float(event.get("notional") or 0.0)
                if event.get("side") == "buy":
                    buy_notional += notional
                elif event.get("side") == "sell":
                    sell_notional += notional
        opens = counts["open"]
        done = counts["done"]
        cancels = sum(
            1
            for event in events
            if float(event.get("created_at") or 0.0) >= cutoff
            and event.get("event_type") == "done"
            and str(event.get("reason") or "").lower().startswith("cancel")
        )
        return {
            f"open_count_{suffix}": opens,
            f"done_count_{suffix}": done,
            f"cancel_count_{suffix}": cancels,
            f"change_count_{suffix}": counts["change"],
            f"match_count_{suffix}": counts["match"],
            f"cancel_to_open_{suffix}": (cancels / opens) if opens else None,
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
            "created_at", "product_id", "sequence", "book_age_seconds",
            "last_message_age_seconds", "best_bid", "best_ask", "mid",
            "spread_bps", "bid_order_count", "ask_order_count", "bid_level_count",
            "ask_level_count", "bid_depth_top", "ask_depth_top", "bid_depth_levels",
            "ask_depth_levels", "bid_notional_levels", "ask_notional_levels",
            "bid_order_count_levels", "ask_order_count_levels",
            "avg_bid_order_size_levels", "avg_ask_order_size_levels",
            "depth_imbalance", "bid_levels_json", "ask_levels_json",
            "open_count_5s", "done_count_5s", "cancel_count_5s",
            "change_count_5s", "match_count_5s", "cancel_to_open_5s",
            "open_count_15s", "done_count_15s", "cancel_count_15s",
            "change_count_15s", "match_count_15s", "cancel_to_open_15s",
            "open_count_60s", "done_count_60s", "cancel_count_60s",
            "change_count_60s", "match_count_60s", "cancel_to_open_60s",
            "matched_buy_notional_60s", "matched_sell_notional_60s",
            "net_matched_buy_notional_60s",
        )
        conn.execute(
            f"INSERT INTO spot_l3_summaries({','.join(cols)}) "
            f"VALUES({','.join('?' for _ in cols)})",
            [row.get(col) for col in cols],
        )

    @staticmethod
    def _insert_event_locked(conn: sqlite3.Connection, event: dict[str, Any]) -> None:
        cols = (
            "created_at", "product_id", "sequence", "event_type", "order_id",
            "side", "price", "size", "notional", "reason",
        )
        conn.execute(
            f"INSERT INTO spot_l3_events({','.join(cols)}) "
            f"VALUES({','.join('?' for _ in cols)})",
            [event.get(col) for col in cols],
        )

    # --- networking ------------------------------------------------------
    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:  # pragma: no cover - thread guard
            logger.warning("Coinbase L3 thread exited: %s", exc)

    async def _run(self) -> None:
        await asyncio.gather(self._provider_loop(), self._recorder_loop())

    async def _recorder_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.record_once()
            except Exception as exc:  # noqa: BLE001 - recorder must not kill feed
                with self._lock:
                    self._last_error["recorder"] = str(exc)[:200]
                logger.warning("Coinbase L3 recorder error: %s", exc)
            await asyncio.sleep(self.record_seconds)

    async def _provider_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    COINBASE_WS,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_queue=5000,
                    max_size=_ws_max_size(),
                ) as socket:
                    with self._lock:
                        self._connected = True
                        self._last_error.pop("coinbase", None)
                    await socket.send(json.dumps(coinbase_l3_subscribe_payload(self.products)))
                    for product in self.products:
                        await asyncio.to_thread(self._load_snapshot, product)
                    backoff = 1.0
                    while not self._stop.is_set():
                        try:
                            message = await asyncio.wait_for(socket.recv(), timeout=20.0)
                        except asyncio.TimeoutError:
                            continue
                        self.handle_coinbase_l3(message)
                        for product in self._drain_pending_resync():
                            await asyncio.to_thread(self._load_snapshot, product)
            except Exception as exc:
                with self._lock:
                    self._last_error["coinbase"] = str(exc)[:200]
                logger.warning("Coinbase L3 reconnecting after error: %s", exc)
            finally:
                with self._lock:
                    self._connected = False
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 1.8)

    def _drain_pending_resync(self) -> list[str]:
        with self._lock:
            out = sorted(self._pending_resync)
            self._pending_resync.clear()
            return out

    def _load_snapshot(self, product_id: str) -> None:
        request_path = BOOK_PATH_TEMPLATE.format(product_id=product_id)
        headers = coinbase_auth_headers("GET", request_path)
        resp = requests.get(f"{COINBASE_REST}{request_path}", headers=headers, timeout=(5, 30))
        resp.raise_for_status()
        data = resp.json()
        self._replace_product_book(
            product_id,
            sequence=_i(data.get("sequence")),
            bids=data.get("bids") or [],
            asks=data.get("asks") or [],
            ts=time.time(),
        )


_feed: CoinbaseL3Collector | None = None
_feed_lock = threading.Lock()


def get_l3_feed() -> CoinbaseL3Collector:
    global _feed
    with _feed_lock:
        if _feed is None:
            _feed = CoinbaseL3Collector()
            _feed.start()
        return _feed


def start_spot_l3() -> None:
    """Start the optional Coinbase L3 collector if enabled. Never raises."""
    if not _enabled():
        return
    try:
        get_l3_feed()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Coinbase L3 start failed: %s", exc)


def spot_l3_health() -> dict[str, Any]:
    info = {
        "enabled": _enabled(),
        "have_ws": _HAVE_WS,
        "authenticated": _has_credentials(),
        "db_path": _db_path(),
        "products": _configured_products(),
        "raw_events_enabled": _raw_events_enabled(),
        "summary_levels": _summary_levels(),
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
