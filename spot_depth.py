"""Actual-coin spot depth and trade recorder.

Default ON, with an explicit opt-out via ``Q15_SPOT_DEPTH_ENABLED=false``. A
background thread subscribes to public exchange orderbook and trade channels for
the same spot symbols used by ``spot_client``. It records visible top-of-book
depth and recent trade pressure to SQLite for later research. BTC is always kept
in the configured asset list as the cross-market baseline.

Read-only market data only: no authentication, no orders. Public books expose
visible liquidity and trades, not hidden/iceberg size or trader identity.
"""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterable

from spot_client import SPOT_SOURCES

logger = logging.getLogger(__name__)

try:
    import websockets
    _HAVE_WS = True
except Exception:  # pragma: no cover - optional dependency guard
    websockets = None
    _HAVE_WS = False

COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
OKX_WS = "wss://ws.okx.com:8443/ws/v5/public"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spot_depth_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    asset TEXT NOT NULL,
    provider TEXT NOT NULL,
    symbol TEXT NOT NULL,
    source TEXT NOT NULL,
    book_age_seconds REAL,
    trade_age_seconds REAL,
    best_bid REAL,
    best_ask REAL,
    mid REAL,
    spread_bps REAL,
    bid_depth_top REAL,
    ask_depth_top REAL,
    bid_depth_levels REAL,
    ask_depth_levels REAL,
    bid_notional_levels REAL,
    ask_notional_levels REAL,
    depth_imbalance REAL,
    bid_levels_json TEXT,
    ask_levels_json TEXT,
    trade_buy_qty_5s REAL,
    trade_sell_qty_5s REAL,
    trade_net_qty_5s REAL,
    trade_buy_notional_5s REAL,
    trade_sell_notional_5s REAL,
    trade_buy_qty_15s REAL,
    trade_sell_qty_15s REAL,
    trade_net_qty_15s REAL,
    trade_buy_notional_15s REAL,
    trade_sell_notional_15s REAL,
    trade_buy_qty_60s REAL,
    trade_sell_qty_60s REAL,
    trade_net_qty_60s REAL,
    trade_buy_notional_60s REAL,
    trade_sell_notional_60s REAL,
    last_trade_price REAL,
    last_trade_side TEXT,
    last_trade_size REAL,
    orderbook_ts REAL,
    trade_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_spot_depth_asset_time
    ON spot_depth_snapshots(asset, created_at);
"""


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


def _configured_assets() -> list[str]:
    raw = os.environ.get("Q15_SPOT_DEPTH_ASSETS", "")
    if raw.strip():
        wanted = [x.strip().upper() for x in raw.split(",") if x.strip()]
    else:
        wanted = list(SPOT_SOURCES.keys())
    if "BTC" in SPOT_SOURCES and "BTC" not in wanted:
        wanted = ["BTC", *wanted]
    assets: list[str] = []
    seen: set[str] = set()
    for asset in wanted:
        if asset in SPOT_SOURCES and asset not in seen:
            assets.append(asset)
            seen.add(asset)
    return assets


def _db_path() -> str:
    return os.environ.get("Q15_SPOT_DEPTH_DB", "data/q15_spot_depth_v1.sqlite3")


def _record_seconds() -> float:
    return _env_float("Q15_SPOT_DEPTH_RECORD_SECONDS", 5.0, minimum=1.0)


def _levels() -> int:
    return _env_int("Q15_SPOT_DEPTH_LEVELS", 5, minimum=1)


def _max_book_age() -> float:
    return _env_float("Q15_SPOT_DEPTH_MAX_BOOK_AGE_SECONDS", 30.0, minimum=1.0)


def _enabled() -> bool:
    return _env_bool("Q15_SPOT_DEPTH_ENABLED", True)


def _f(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return out


def _source_ts(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value)
    if not text:
        return None
    if text.isdigit():
        # OKX uses milliseconds.
        raw = float(text)
        return raw / 1000.0 if raw > 10_000_000_000 else raw
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _side(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if text in {"buy", "bid"}:
        return "buy"
    if text in {"sell", "ask"}:
        return "sell"
    return None


class SpotDepthRecorder:
    """Background actual-coin depth/trade collector."""

    def __init__(self, assets: Iterable[str] | None = None, db_path: str | None = None) -> None:
        self.assets = list(assets or _configured_assets())
        self.db_path = db_path or _db_path()
        self.level_count = _levels()
        self._record_seconds = _record_seconds()
        self._max_book_age = _max_book_age()
        self._lock = threading.Lock()
        self._books: dict[str, dict[str, Any]] = {}
        self._trades: dict[str, deque[dict[str, Any]]] = {asset: deque() for asset in self.assets}
        self._latest_snapshot: dict[str, dict[str, Any]] = {}
        self._connected = {"coinbase": False, "okx": False}
        self._last_error: dict[str, str] = {}
        self._last_message_at: dict[str, float] = {}
        self._last_record_at: float | None = None
        self._records_written = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._conn: sqlite3.Connection | None = None

        wanted = set(self.assets)
        self._coinbase = {
            sym: asset
            for asset, (provider, sym, _quote) in SPOT_SOURCES.items()
            if asset in wanted and provider == "coinbase"
        }
        self._okx = {
            sym: asset
            for asset, (provider, sym, _quote) in SPOT_SOURCES.items()
            if asset in wanted and provider == "okx"
        }

    def start(self) -> None:
        if not _HAVE_WS:
            logger.warning("Spot depth disabled: `pip install websockets`")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._thread_main, name="spot-depth", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def get_latest(self, asset: str, max_age: float | None = None) -> dict[str, Any] | None:
        now = time.time()
        limit = _max_book_age() if max_age is None else max_age
        with self._lock:
            snap = self._latest_snapshot.get(asset.upper())
        if not snap:
            return None
        if now - float(snap.get("created_at") or 0.0) > limit:
            return None
        return dict(snap)

    def health(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            books = {
                asset: round(now - float(book.get("orderbook_ts") or 0.0), 3)
                for asset, book in self._books.items()
            }
            trades = {}
            for asset, rows in self._trades.items():
                if rows:
                    trades[asset] = round(now - float(rows[-1].get("ts") or 0.0), 3)
            return {
                "enabled": _enabled(),
                "have_ws": _HAVE_WS,
                "assets": list(self.assets),
                "db_path": self.db_path,
                "record_seconds": self._record_seconds,
                "levels": self.level_count,
                "connected": dict(self._connected),
                "book_age_seconds": books,
                "trade_age_seconds": trades,
                "last_record_age_seconds": (
                    round(now - self._last_record_at, 3) if self._last_record_at else None
                ),
                "records_written": self._records_written,
                "last_error": dict(self._last_error),
            }

    def close(self) -> None:
        self.stop()
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # --- parsing ---------------------------------------------------------
    def _handle_coinbase(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except Exception:
            return
        typ = data.get("type")
        asset = self._coinbase.get(data.get("product_id"))
        if not asset:
            return
        if typ == "snapshot":
            self._replace_book(
                asset,
                provider="coinbase",
                symbol=str(data.get("product_id")),
                bids=data.get("bids") or [],
                asks=data.get("asks") or [],
                ts=time.time(),
            )
        elif typ == "l2update":
            self._update_book(
                asset,
                provider="coinbase",
                symbol=str(data.get("product_id")),
                changes=data.get("changes") or [],
                ts=_source_ts(data.get("time")) or time.time(),
            )
        elif typ == "match":
            self._record_trade(
                asset,
                provider="coinbase",
                symbol=str(data.get("product_id")),
                side=_side(data.get("side")),
                price=_f(data.get("price")),
                size=_f(data.get("size")),
                ts=_source_ts(data.get("time")) or time.time(),
            )

    def _handle_okx(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except Exception:
            return
        arg = data.get("arg") or {}
        channel = str(arg.get("channel") or "")
        for row in data.get("data") or []:
            inst = arg.get("instId") or row.get("instId")
            asset = self._okx.get(inst)
            if not asset:
                continue
            ts = _source_ts(row.get("ts")) or time.time()
            if channel.startswith("books"):
                self._replace_book(
                    asset,
                    provider="okx",
                    symbol=str(inst),
                    bids=row.get("bids") or [],
                    asks=row.get("asks") or [],
                    ts=ts,
                )
            elif channel == "trades":
                self._record_trade(
                    asset,
                    provider="okx",
                    symbol=str(inst),
                    side=_side(row.get("side")),
                    price=_f(row.get("px")),
                    size=_f(row.get("sz")),
                    ts=ts,
                )

    def _replace_book(
        self,
        asset: str,
        *,
        provider: str,
        symbol: str,
        bids: Iterable[Any],
        asks: Iterable[Any],
        ts: float,
    ) -> None:
        bid_map = self._levels_to_map(bids)
        ask_map = self._levels_to_map(asks)
        if not bid_map or not ask_map:
            return
        with self._lock:
            self._books[asset] = {
                "provider": provider,
                "symbol": symbol,
                "bids": bid_map,
                "asks": ask_map,
                "orderbook_ts": ts,
            }

    def _update_book(
        self,
        asset: str,
        *,
        provider: str,
        symbol: str,
        changes: Iterable[Any],
        ts: float,
    ) -> None:
        with self._lock:
            book = self._books.setdefault(
                asset,
                {"provider": provider, "symbol": symbol, "bids": {}, "asks": {}, "orderbook_ts": ts},
            )
            book["provider"] = provider
            book["symbol"] = symbol
            for change in changes:
                try:
                    side, price_raw, size_raw = change[:3]
                except (TypeError, ValueError):
                    continue
                price = _f(price_raw)
                size = _f(size_raw)
                if price is None or size is None:
                    continue
                target = book["bids"] if str(side).lower() in {"buy", "bid"} else book["asks"]
                if size <= 0:
                    target.pop(price, None)
                else:
                    target[price] = (size, None)
            book["orderbook_ts"] = ts

    @staticmethod
    def _levels_to_map(levels: Iterable[Any]) -> dict[float, tuple[float, float | None]]:
        out: dict[float, tuple[float, float | None]] = {}
        for level in levels:
            try:
                price_raw, size_raw = level[:2]
            except (TypeError, ValueError):
                continue
            price = _f(price_raw)
            size = _f(size_raw)
            order_count = _f(level[3]) if isinstance(level, (list, tuple)) and len(level) > 3 else None
            if price is None or size is None or price <= 0 or size <= 0:
                continue
            out[price] = (size, order_count)
        return out

    def _record_trade(
        self,
        asset: str,
        *,
        provider: str,
        symbol: str,
        side: str | None,
        price: float | None,
        size: float | None,
        ts: float,
    ) -> None:
        if side not in {"buy", "sell"} or price is None or size is None or price <= 0 or size <= 0:
            return
        trade = {
            "ts": ts,
            "side": side,
            "price": price,
            "size": size,
            "notional": price * size,
            "provider": provider,
            "symbol": symbol,
        }
        cutoff = time.time() - 120.0
        with self._lock:
            rows = self._trades.setdefault(asset, deque())
            rows.append(trade)
            while rows and float(rows[0].get("ts") or 0.0) < cutoff:
                rows.popleft()

    # --- recording -------------------------------------------------------
    def record_once(self) -> int:
        now = time.time()
        rows: list[dict[str, Any]] = []
        with self._lock:
            for asset, book in self._books.items():
                row = self._snapshot_locked(asset, book, now)
                if row:
                    self._latest_snapshot[asset] = dict(row)
                    rows.append(row)
        if not rows:
            return 0
        conn = self._connect()
        with self._lock:
            for row in rows:
                self._insert_row_locked(conn, row)
            conn.commit()
            self._last_record_at = now
            self._records_written += len(rows)
        return len(rows)

    def _snapshot_locked(self, asset: str, book: dict[str, Any], now: float) -> dict[str, Any] | None:
        orderbook_ts = _f(book.get("orderbook_ts"))
        if orderbook_ts is None or now - orderbook_ts > self._max_book_age:
            return None
        bids = self._top_levels(book.get("bids") or {}, reverse=True)
        asks = self._top_levels(book.get("asks") or {}, reverse=False)
        if not bids or not asks:
            return None
        best_bid = bids[0][0]
        best_ask = asks[0][0]
        mid = (best_bid + best_ask) / 2.0 if best_bid > 0 and best_ask > 0 else None
        spread_bps = ((best_ask - best_bid) / mid * 10_000.0) if mid else None
        bid_depth = sum(level[1] for level in bids)
        ask_depth = sum(level[1] for level in asks)
        denom = bid_depth + ask_depth
        trades = list(self._trades.get(asset) or [])
        last_trade = trades[-1] if trades else {}
        out = {
            "created_at": now,
            "asset": asset,
            "provider": book.get("provider"),
            "symbol": book.get("symbol"),
            "source": f"{book.get('provider')} {book.get('symbol')}",
            "book_age_seconds": round(now - orderbook_ts, 3),
            "trade_age_seconds": (
                round(now - float(last_trade.get("ts")), 3) if last_trade.get("ts") else None
            ),
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
            "spread_bps": spread_bps,
            "bid_depth_top": bids[0][1],
            "ask_depth_top": asks[0][1],
            "bid_depth_levels": bid_depth,
            "ask_depth_levels": ask_depth,
            "bid_notional_levels": sum(level[0] * level[1] for level in bids),
            "ask_notional_levels": sum(level[0] * level[1] for level in asks),
            "depth_imbalance": ((bid_depth - ask_depth) / denom) if denom > 0 else None,
            "bid_levels_json": json.dumps(bids, separators=(",", ":")),
            "ask_levels_json": json.dumps(asks, separators=(",", ":")),
            "last_trade_price": last_trade.get("price"),
            "last_trade_side": last_trade.get("side"),
            "last_trade_size": last_trade.get("size"),
            "orderbook_ts": orderbook_ts,
            "trade_ts": last_trade.get("ts"),
        }
        out.update(self._trade_sums(trades, now, 5.0, "5s"))
        out.update(self._trade_sums(trades, now, 15.0, "15s"))
        out.update(self._trade_sums(trades, now, 60.0, "60s"))
        return out

    def _top_levels(
        self,
        levels: dict[float, tuple[float, float | None]],
        *,
        reverse: bool,
    ) -> list[list[float | None]]:
        ordered = sorted(levels.items(), key=lambda item: item[0], reverse=reverse)[: self.level_count]
        return [[price, size, order_count] for price, (size, order_count) in ordered]

    @staticmethod
    def _trade_sums(
        trades: list[dict[str, Any]], now: float, window: float, suffix: str
    ) -> dict[str, float]:
        buy_qty = sell_qty = buy_notional = sell_notional = 0.0
        cutoff = now - window
        for trade in trades:
            if float(trade.get("ts") or 0.0) < cutoff:
                continue
            size = float(trade.get("size") or 0.0)
            notional = float(trade.get("notional") or 0.0)
            if trade.get("side") == "buy":
                buy_qty += size
                buy_notional += notional
            elif trade.get("side") == "sell":
                sell_qty += size
                sell_notional += notional
        return {
            f"trade_buy_qty_{suffix}": buy_qty,
            f"trade_sell_qty_{suffix}": sell_qty,
            f"trade_net_qty_{suffix}": buy_qty - sell_qty,
            f"trade_buy_notional_{suffix}": buy_notional,
            f"trade_sell_notional_{suffix}": sell_notional,
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
    def _insert_row_locked(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        cols = (
            "created_at", "asset", "provider", "symbol", "source",
            "book_age_seconds", "trade_age_seconds", "best_bid", "best_ask",
            "mid", "spread_bps", "bid_depth_top", "ask_depth_top",
            "bid_depth_levels", "ask_depth_levels", "bid_notional_levels",
            "ask_notional_levels", "depth_imbalance", "bid_levels_json",
            "ask_levels_json", "trade_buy_qty_5s", "trade_sell_qty_5s",
            "trade_net_qty_5s", "trade_buy_notional_5s",
            "trade_sell_notional_5s", "trade_buy_qty_15s",
            "trade_sell_qty_15s", "trade_net_qty_15s",
            "trade_buy_notional_15s", "trade_sell_notional_15s",
            "trade_buy_qty_60s", "trade_sell_qty_60s", "trade_net_qty_60s",
            "trade_buy_notional_60s", "trade_sell_notional_60s",
            "last_trade_price", "last_trade_side", "last_trade_size",
            "orderbook_ts", "trade_ts",
        )
        conn.execute(
            f"INSERT INTO spot_depth_snapshots({','.join(cols)}) "
            f"VALUES({','.join('?' for _ in cols)})",
            [row.get(col) for col in cols],
        )

    # --- networking ------------------------------------------------------
    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:  # pragma: no cover - thread guard
            logger.warning("Spot depth thread exited: %s", exc)

    async def _run(self) -> None:
        tasks = [self._recorder_loop()]
        if self._coinbase:
            tasks.append(self._provider_loop("coinbase", COINBASE_WS, self._subscribe_coinbase, self._handle_coinbase))
        if self._okx:
            tasks.append(self._provider_loop("okx", OKX_WS, self._subscribe_okx, self._handle_okx))
        await asyncio.gather(*tasks)

    async def _recorder_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.record_once()
            except Exception as exc:  # noqa: BLE001 - recorder must not kill feed
                with self._lock:
                    self._last_error["recorder"] = str(exc)[:200]
                logger.warning("Spot depth recorder error: %s", exc)
            await asyncio.sleep(self._record_seconds)

    async def _provider_loop(self, name: str, url: str, subscribe, handle) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=20, close_timeout=5, max_queue=1000
                ) as socket:
                    with self._lock:
                        self._connected[name] = True
                        self._last_error.pop(name, None)
                        self._last_message_at[name] = time.time()
                    await subscribe(socket)
                    backoff = 1.0
                    while not self._stop.is_set():
                        try:
                            message = await asyncio.wait_for(socket.recv(), timeout=20.0)
                        except asyncio.TimeoutError:
                            continue
                        handle(message)
                        with self._lock:
                            self._last_message_at[name] = time.time()
            except Exception as exc:
                with self._lock:
                    self._last_error[name] = str(exc)[:200]
                logger.warning("Spot depth %s reconnecting after error: %s", name, exc)
            finally:
                with self._lock:
                    self._connected[name] = False
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 1.8)

    async def _subscribe_coinbase(self, socket) -> None:
        await socket.send(json.dumps({
            "type": "subscribe",
            "product_ids": sorted(self._coinbase.keys()),
            "channels": ["level2", "matches"],
        }))

    async def _subscribe_okx(self, socket) -> None:
        args = []
        for sym in sorted(self._okx.keys()):
            args.append({"channel": "books5", "instId": sym})
            args.append({"channel": "trades", "instId": sym})
        await socket.send(json.dumps({"op": "subscribe", "args": args}))


_feed: SpotDepthRecorder | None = None
_feed_lock = threading.Lock()


def get_feed() -> SpotDepthRecorder:
    global _feed
    with _feed_lock:
        if _feed is None:
            _feed = SpotDepthRecorder()
            _feed.start()
        return _feed


def start_spot_depth() -> None:
    """Start the optional collector if enabled. Never raises."""
    if not _enabled():
        return
    try:
        get_feed()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Spot depth start failed: %s", exc)


def get_latest_spot_depth(asset: str, max_age: float | None = None) -> dict[str, Any] | None:
    if not _enabled():
        return None
    try:
        return get_feed().get_latest(asset, max_age=max_age)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("spot depth lookup failed for %s: %s", asset, exc)
        return None


def spot_depth_health() -> dict[str, Any]:
    info = {
        "enabled": _enabled(),
        "have_ws": _HAVE_WS,
        "db_path": _db_path(),
        "assets": _configured_assets(),
    }
    if not _enabled() or _feed is None:
        return info
    try:
        info.update(_feed.health())
    except Exception as exc:  # pragma: no cover - defensive
        info["error"] = str(exc)[:200]
    return info
