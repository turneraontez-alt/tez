"""Binance futures liquidation feed.

Default-OFF behind ``Q15_FEED_LIQ``. The collector subscribes only to Binance's
public forceOrder websocket stream, records liquidation events to its own
SQLite DB, and exposes rolling 60-second notional context. It has no trading or
private-account methods.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict, deque
import json
import logging
import math
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

try:
    import websockets

    _HAVE_WS = True
except Exception:  # pragma: no cover - optional dependency guard
    websockets = None
    _HAVE_WS = False


DEFAULT_SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
    "DOGE": "DOGEUSDT",
    "BNB": "BNBUSDT",
    "HYPE": "HYPEUSDT",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS liquidation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    event_age_s REAL NOT NULL,
    asset TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    qty REAL NOT NULL,
    notional REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_liq_asset_ts
    ON liquidation_events(asset, ts);
"""


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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


def _enabled() -> bool:
    return _env_bool("Q15_FEED_LIQ", False)


def _db_path() -> str:
    return os.environ.get("Q15_FEED_LIQ_DB", "data/q15_liq_feed_v1.sqlite3")


def _stale_seconds() -> float:
    return _env_float("Q15_FEED_LIQ_STALE_SECONDS", 90.0, minimum=5.0)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _epoch_s(value: Any) -> float | None:
    raw = _num(value)
    if raw is None:
        return None
    while raw > 10_000_000_000:
        raw /= 1000.0
    return raw


def _configured_symbols() -> dict[str, str]:
    out = dict(DEFAULT_SYMBOLS)
    raw = os.environ.get("Q15_FEED_LIQ_SYMBOLS", "").strip()
    if not raw:
        return out
    for part in raw.split(","):
        if "=" not in part:
            continue
        asset, symbol = (piece.strip().upper() for piece in part.split("=", 1))
        if asset and symbol:
            out[asset] = symbol
    return out


class LiquidationFeed:
    def __init__(self, *, db_path: str | None = None, symbols: Mapping[str, str] | None = None) -> None:
        self.db_path = db_path or _db_path()
        self.enabled = _enabled()
        self.have_ws = _HAVE_WS
        self.symbols = {str(k).upper(): str(v).upper() for k, v in (symbols or _configured_symbols()).items()}
        self.symbol_to_asset = {symbol: asset for asset, symbol in self.symbols.items()}
        self.stale_seconds = _stale_seconds()
        self._events: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=5000))
        self._latest_by_asset: dict[str, float] = {}
        self._lock = threading.Lock()
        self._queue: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=_env_int("Q15_FEED_LIQ_QUEUE", 8192))
        self._writer: threading.Thread | None = None
        self._ws_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connected = False
        self._last_message_at: float | None = None
        self._last_error: str | None = None
        self._rows_written = 0
        self._dropped_rows = 0
        self._init_db()

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def start(self) -> bool:
        if not self.enabled:
            return False
        if not self.have_ws:
            self._last_error = "websockets_dependency_missing"
            logger.warning("Liquidation feed disabled: websockets dependency missing")
            return False
        self._stop.clear()
        if self._writer is None or not self._writer.is_alive():
            self._writer = threading.Thread(target=self._writer_loop, name="liq-feed-writer", daemon=True)
            self._writer.start()
        if self._ws_thread is None or not self._ws_thread.is_alive():
            self._ws_thread = threading.Thread(target=self._ws_thread_main, name="liq-feed-ws", daemon=True)
            self._ws_thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _ws_thread_main(self) -> None:
        try:
            asyncio.run(self._run_forever())
        except RuntimeError as exc:
            self._last_error = str(exc)[:200]

    def _ws_url(self) -> str:
        streams = "/".join(f"{symbol.lower()}@forceOrder" for symbol in sorted(set(self.symbols.values())))
        return os.environ.get("Q15_FEED_LIQ_WS_URL") or f"wss://fstream.binance.com/stream?streams={streams}"

    async def _run_forever(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self._ws_url(),
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_queue=1000,
                ) as socket:
                    self._connected = True
                    self._last_error = None
                    backoff = 1.0
                    while not self._stop.is_set():
                        try:
                            raw = await asyncio.wait_for(socket.recv(), timeout=2.0)
                        except asyncio.TimeoutError:
                            continue
                        self.handle_message(raw)
            except Exception as exc:  # noqa: BLE001 - collector boundary
                self._last_error = str(exc)[:300]
                logger.warning("Liquidation feed reconnecting after error: %s", exc)
            finally:
                self._connected = False
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 1.8)

    def handle_message(self, raw: str | bytes | Mapping[str, Any]) -> dict[str, Any] | None:
        if isinstance(raw, Mapping):
            payload = raw
        else:
            try:
                payload = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                return None
        if not isinstance(payload, Mapping):
            return None
        event = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
        order = event.get("o") if isinstance(event.get("o"), Mapping) else {}
        symbol = str(order.get("s") or event.get("s") or "").upper()
        asset = self.symbol_to_asset.get(symbol)
        if not asset:
            return None
        side = str(order.get("S") or "").upper()
        price = _num(order.get("ap")) or _num(order.get("p"))
        qty = _num(order.get("z")) or _num(order.get("q"))
        event_ts = _epoch_s(order.get("T")) or _epoch_s(event.get("E")) or time.time()
        if side not in {"BUY", "SELL"} or price is None or qty is None or price <= 0 or qty <= 0:
            return None
        now = time.time()
        row = {
            "ts": event_ts,
            "event_age_s": max(0.0, now - event_ts),
            "asset": asset,
            "symbol": symbol,
            "side": side,
            "price": price,
            "qty": qty,
            "notional": price * qty,
        }
        with self._lock:
            self._events[asset].append(row)
            self._latest_by_asset[asset] = now
            self._last_message_at = now
            self._prune_locked(now)
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            self._dropped_rows += 1
        return row

    def _prune_locked(self, now: float) -> None:
        cutoff = now - 60.0
        for rows in self._events.values():
            while rows and float(rows[0].get("ts") or 0.0) < cutoff:
                rows.popleft()

    def snapshot(self, asset: str, *, now: float | None = None) -> dict[str, Any]:
        current = time.time() if now is None else float(now)
        asset_key = str(asset).upper()
        with self._lock:
            last = self._latest_by_asset.get(asset_key)
            if self._last_message_at is not None and current - self._last_message_at > self.stale_seconds:
                return {"liq_notional_60s": None, "liq_side": None, "liq_age_s": None}
            rows = list(self._events.get(asset_key, ()))
            rows = [row for row in rows if current - float(row.get("ts") or current) <= 60.0]
        totals = {"BUY": 0.0, "SELL": 0.0}
        for row in rows:
            side = str(row.get("side") or "").upper()
            if side in totals:
                totals[side] += float(row.get("notional") or 0.0)
        if totals["BUY"] <= 0.0 and totals["SELL"] <= 0.0:
            return {
                "liq_notional_60s": 0.0,
                "liq_side": None,
                "liq_age_s": None if last is None else max(0.0, current - last),
            }
        side = "BUY" if totals["BUY"] >= totals["SELL"] else "SELL"
        return {
            "liq_notional_60s": round(totals[side], 4),
            "liq_side": side,
            "liq_age_s": None if last is None else max(0.0, current - last),
        }

    def _insert(self, conn: sqlite3.Connection, row: Mapping[str, Any]) -> None:
        conn.execute(
            "INSERT INTO liquidation_events(ts,event_age_s,asset,symbol,side,price,qty,notional) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                row.get("ts"),
                row.get("event_age_s"),
                row.get("asset"),
                row.get("symbol"),
                row.get("side"),
                row.get("price"),
                row.get("qty"),
                row.get("notional"),
            ),
        )
        conn.commit()
        self._rows_written += 1

    def _writer_loop(self) -> None:
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        try:
            while not self._stop.is_set():
                try:
                    row = self._queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                try:
                    self._insert(conn, row)
                except sqlite3.Error as exc:
                    self._last_error = str(exc)[:200]
                    logger.warning("liquidation recorder error: %s", exc)
                finally:
                    self._queue.task_done()
        finally:
            conn.close()

    def _drain_once_for_tests(self) -> bool:
        try:
            row = self._queue.get_nowait()
        except queue.Empty:
            return False
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        try:
            self._insert(conn, row)
            return True
        finally:
            conn.close()
            self._queue.task_done()

    def health(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            ages = {
                asset: round(max(0.0, now - ts), 3)
                for asset, ts in self._latest_by_asset.items()
            }
            last_message_at = self._last_message_at
        latest_age = min(ages.values()) if ages else None
        return {
            "enabled": self.enabled,
            "read_only": True,
            "have_ws": self.have_ws,
            "connected": self._connected,
            "db_path": self.db_path,
            "symbols": dict(self.symbols),
            "latest_age_seconds": latest_age,
            "age_by_asset_seconds": ages,
            "last_message_age_seconds": None if last_message_at is None else round(max(0.0, now - last_message_at), 3),
            "rows_written": self._rows_written,
            "dropped_rows": self._dropped_rows,
            "queue_size": self._queue.qsize(),
            "last_error": self._last_error,
            "stale_seconds": self.stale_seconds,
            "status": "disabled" if not self.enabled else ("connected" if self._connected else "starting_or_disconnected"),
        }


_feed: LiquidationFeed | None = None
_lock = threading.Lock()


def get_liq_feed() -> LiquidationFeed:
    global _feed
    with _lock:
        if _feed is None:
            _feed = LiquidationFeed()
        return _feed


def start_liq_feed() -> bool:
    try:
        return get_liq_feed().start()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Liquidation feed start failed: %s", exc)
        return False


def liq_context(asset: str, *, now: float | None = None) -> dict[str, Any]:
    try:
        feed = get_liq_feed()
        if not feed.enabled:
            return {"liq_notional_60s": None, "liq_side": None, "liq_age_s": None}
        return feed.snapshot(asset, now=now)
    except Exception:
        logger.debug("liquidation context unavailable", exc_info=True)
        return {"liq_notional_60s": None, "liq_side": None, "liq_age_s": None}


def liq_health() -> dict[str, Any]:
    try:
        return get_liq_feed().health()
    except Exception as exc:  # noqa: BLE001
        return {"enabled": _enabled(), "read_only": True, "error": f"{type(exc).__name__}: {exc}"}
