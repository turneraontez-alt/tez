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
import heapq
import json
import logging
import math
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
COINBASE_BOOK_CHANNEL = "level2_50"
SPOT_MID_PATH_SCHEMA_VERSION = "spot-mid-path-local-v1"
SPOT_MID_PATH_TIME_BASIS = "local_created_at"
SPOT_MID_PATH_HORIZONS = (15, 60)
SPOT_MID_PATH_RETENTION_SECONDS = 180.0
SPOT_FAST_MID_PATH_SCHEMA_VERSION = "spot-fast-mid-path-local-observed-v1"
SPOT_FAST_MID_PATH_TIME_BASIS = "local_received_or_captured_at"
SPOT_FAST_MID_SAMPLE_SECONDS = 1.0

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
    trade_side_semantics TEXT,
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
    trade_ts REAL,
    orderbook_received_at REAL,
    trade_received_at REAL,
    book_source_age_seconds REAL,
    trade_source_age_seconds REAL
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


def _retention_days() -> float:
    return _env_float("Q15_SPOT_DEPTH_RETENTION_DAYS", 7.0, minimum=0.0)


def _levels() -> int:
    return _env_int("Q15_SPOT_DEPTH_LEVELS", 5, minimum=1)


def _ws_max_size() -> int:
    return _env_int("Q15_SPOT_DEPTH_WS_MAX_SIZE_BYTES", 16 * 1024 * 1024, minimum=1024 * 1024)


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


def _coinbase_aggressor_side(value: Any) -> str | None:
    """Convert Coinbase Exchange's maker-side match label to taker side."""
    maker_side = _side(value)
    if maker_side == "buy":
        return "sell"
    if maker_side == "sell":
        return "buy"
    return None


class SpotDepthRecorder:
    """Background actual-coin depth/trade collector."""

    def __init__(self, assets: Iterable[str] | None = None, db_path: str | None = None) -> None:
        self.assets = list(assets or _configured_assets())
        self.db_path = db_path or _db_path()
        self.level_count = _levels()
        self._record_seconds = _record_seconds()
        self._retention_days = _retention_days()
        self._max_book_age = _max_book_age()
        self._lock = threading.Lock()
        self._books: dict[str, dict[str, Any]] = {}
        # Crossed-book detection state. `_resync_assets` is drained by the provider loop,
        # which drops the corrupt book so the next snapshot message rebuilds it from scratch;
        # `_crossed_books` is a diagnostic counter surfaced in health output.
        self._resync_assets: set[str] = set()
        self._crossed_books: dict[str, int] = {}
        self._trades: dict[str, deque[dict[str, Any]]] = {asset: deque() for asset in self.assets}
        self._mid_history: dict[str, deque[dict[str, float]]] = {
            asset: deque() for asset in self.assets
        }
        # A separate event-driven path for future outcome-blind research.  The
        # legacy 5-second DB-cadence path above remains byte-for-byte compatible
        # with frozen controls.
        self._fast_mid_history: dict[str, deque[dict[str, float]]] = {
            asset: deque() for asset in self.assets
        }
        self._latest_snapshot: dict[str, dict[str, Any]] = {}
        self._connected = {"coinbase": False, "okx": False}
        self._last_error: dict[str, str] = {}
        self._last_message_at: dict[str, float] = {}
        self._last_record_at: float | None = None
        self._records_written = 0
        self._records_pruned = 0
        self._last_prune_at = 0.0
        self._thread: threading.Thread | None = None
        self._fast_mid_thread: threading.Thread | None = None
        self._last_fast_mid_sample_at: float | None = None
        self._fast_mid_sampler_iterations = 0
        self._fast_mid_sampler_late_iterations = 0
        self._fast_mid_sampler_max_interval_seconds = 0.0
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
        self._stop.clear()
        if not self._fast_mid_thread or not self._fast_mid_thread.is_alive():
            self._fast_mid_thread = threading.Thread(
                target=self._fast_mid_sampler_thread_main,
                name="spot-fast-mid",
                daemon=True,
            )
            self._fast_mid_thread.start()
        if not self._thread or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._thread_main, name="spot-depth", daemon=True
            )
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

    def capture_current(self, asset: str) -> dict[str, Any] | None:
        """Freeze the live in-memory book without waiting for the DB cadence.

        Exact-time decision systems persist the returned evidence in their own
        ledgers.  Keeping this read path separate from ``record_once`` avoids a
        five-second sampling blind spot without multiplying SQLite write load.
        """
        asset_key = str(asset or "").upper()
        now = time.time()
        with self._lock:
            book = self._books.get(asset_key)
            if not book:
                return None
            row = self._snapshot_locked(asset_key, book, now)
            if row is not None:
                row.update(self._mid_path_features_locked(asset_key, row, now))
                row.update(self._fast_mid_path_features_locked(asset_key, row, now))
        return None if row is None else dict(row)

    def health(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            books = {
                asset: round(now - float(book.get("orderbook_received_at") or 0.0), 3)
                for asset, book in self._books.items()
            }
            book_source_ages = {
                asset: round(now - float(book.get("orderbook_ts") or 0.0), 3)
                for asset, book in self._books.items()
            }
            trades = {}
            for asset, rows in self._trades.items():
                if rows:
                    trades[asset] = round(
                        now - float(rows[-1].get("received_at") or 0.0), 3
                    )
            mid_history_rows = {
                asset: len(rows) for asset, rows in self._mid_history.items()
            }
            fast_mid_history_rows = {
                asset: len(rows) for asset, rows in self._fast_mid_history.items()
            }
            fast_mid_recent_path: dict[str, dict[str, Any]] = {}
            cutoff = now - 60.0
            for asset, rows in self._fast_mid_history.items():
                prior = next(
                    (
                        row for row in reversed(rows)
                        if float(row.get("created_at") or 0.0) <= cutoff
                    ),
                    None,
                )
                recent = [
                    row for row in rows
                    if float(row.get("created_at") or 0.0) > cutoff
                ]
                selected = ([] if prior is None else [prior]) + recent
                times = [float(row["created_at"]) for row in selected]
                gaps = [right - left for left, right in zip(times, times[1:])]
                fast_mid_recent_path[asset] = {
                    "count_60s": len(selected),
                    "max_gap_seconds_60s": (
                        None if not gaps else round(max(gaps), 3)
                    ),
                    "latest_age_seconds": (
                        None if not times else round(now - times[-1], 3)
                    ),
                }
            mid_history_seconds = {
                asset: round(
                    max(0.0, now - float(rows[0].get("created_at") or now)), 3
                )
                for asset, rows in self._mid_history.items()
                if rows
            }
            return {
                "enabled": _enabled(),
                "have_ws": _HAVE_WS,
                "assets": list(self.assets),
                "db_path": self.db_path,
                "record_seconds": self._record_seconds,
                "retention_days": self._retention_days,
                "levels": self.level_count,
                "trade_side_semantics": "aggressor",
                "connected": dict(self._connected),
                "book_age_seconds": books,
                "book_source_age_seconds": book_source_ages,
                "trade_age_seconds": trades,
                "mid_path_schema_version": SPOT_MID_PATH_SCHEMA_VERSION,
                "mid_path_time_basis": SPOT_MID_PATH_TIME_BASIS,
                "mid_path_retention_seconds": SPOT_MID_PATH_RETENTION_SECONDS,
                "mid_history_rows": mid_history_rows,
                "fast_mid_path_schema_version": SPOT_FAST_MID_PATH_SCHEMA_VERSION,
                "fast_mid_path_time_basis": SPOT_FAST_MID_PATH_TIME_BASIS,
                "fast_mid_sample_seconds": SPOT_FAST_MID_SAMPLE_SECONDS,
                "fast_mid_sampler_thread_alive": bool(
                    self._fast_mid_thread and self._fast_mid_thread.is_alive()
                ),
                "fast_mid_sampler_iterations": self._fast_mid_sampler_iterations,
                "fast_mid_sampler_late_iterations": (
                    self._fast_mid_sampler_late_iterations
                ),
                "fast_mid_sampler_max_interval_seconds": round(
                    self._fast_mid_sampler_max_interval_seconds, 3
                ),
                "fast_mid_sampler_age_seconds": (
                    None
                    if self._last_fast_mid_sample_at is None
                    else round(now - self._last_fast_mid_sample_at, 3)
                ),
                "fast_mid_history_rows": fast_mid_history_rows,
                "fast_mid_recent_path": fast_mid_recent_path,
                "mid_history_seconds": mid_history_seconds,
                "last_record_age_seconds": (
                    round(now - self._last_record_at, 3) if self._last_record_at else None
                ),
                "records_written": self._records_written,
                "records_pruned": self._records_pruned,
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
        received_at = time.time()
        try:
            data = json.loads(raw)
        except Exception:
            return
        typ = data.get("type")
        if typ == "error":
            message = data.get("message") or "coinbase_error"
            reason = data.get("reason")
            text = f"{message}: {reason}" if reason else str(message)
            with self._lock:
                self._last_error["coinbase_message"] = text[:200]
            return
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
                ts=received_at,
                received_at=received_at,
            )
        elif typ == "l2update":
            self._update_book(
                asset,
                provider="coinbase",
                symbol=str(data.get("product_id")),
                changes=data.get("changes") or [],
                ts=_source_ts(data.get("time")) or received_at,
                received_at=received_at,
            )
        elif typ in {"match", "last_match"}:
            self._record_trade(
                asset,
                provider="coinbase",
                symbol=str(data.get("product_id")),
                side=_coinbase_aggressor_side(data.get("side")),
                price=_f(data.get("price")),
                size=_f(data.get("size")),
                ts=_source_ts(data.get("time")) or received_at,
                received_at=received_at,
            )

    def _handle_okx(self, raw: str) -> None:
        received_at = time.time()
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
                    received_at=received_at,
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
                    received_at=received_at,
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
        received_at: float | None = None,
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
                "best_bid": max(bid_map),
                "best_ask": min(ask_map),
                "orderbook_ts": ts,
                "orderbook_received_at": received_at if received_at is not None else ts,
            }
            self._append_fast_mid_from_book_locked(
                asset, self._books[asset],
                received_at if received_at is not None else ts,
            )
            # A full snapshot is exactly what a flagged resync was waiting for.
            self._resync_assets.discard(asset)

    def _update_book(
        self,
        asset: str,
        *,
        provider: str,
        symbol: str,
        changes: Iterable[Any],
        ts: float,
        received_at: float | None = None,
    ) -> None:
        with self._lock:
            # A book flagged as crossed is known-corrupt: patching more deltas onto it only
            # propagates the error, and every delta refreshes orderbook_ts so it keeps looking
            # fresh. Drop it and stay dark until a full snapshot rebuilds it (fail-closed —
            # the same posture kraken_l3 takes when it detects a crossed book).
            if asset in self._resync_assets:
                self._books.pop(asset, None)
                return
            book = self._books.setdefault(
                asset,
                {
                    "provider": provider,
                    "symbol": symbol,
                    "bids": {},
                    "asks": {},
                    "orderbook_ts": ts,
                    "orderbook_received_at": received_at if received_at is not None else ts,
                },
            )
            book["provider"] = provider
            book["symbol"] = symbol
            best_bid = _f(book.get("best_bid"))
            best_ask = _f(book.get("best_ask"))
            for change in changes:
                try:
                    side, price_raw, size_raw = change[:3]
                except (TypeError, ValueError):
                    continue
                price = _f(price_raw)
                size = _f(size_raw)
                if price is None or size is None:
                    continue
                is_bid = str(side).lower() in {"buy", "bid"}
                target = book["bids"] if is_bid else book["asks"]
                if size <= 0:
                    target.pop(price, None)
                    if is_bid and best_bid == price:
                        best_bid = None
                    elif not is_bid and best_ask == price:
                        best_ask = None
                else:
                    target[price] = (size, None)
                    if is_bid and (best_bid is None or price > best_bid):
                        best_bid = price
                    elif not is_bid and (best_ask is None or price < best_ask):
                        best_ask = price
            if best_bid is None and book["bids"]:
                best_bid = max(book["bids"])
            if best_ask is None and book["asks"]:
                best_ask = min(book["asks"])
            book["best_bid"] = best_bid
            book["best_ask"] = best_ask
            book["orderbook_ts"] = ts
            book["orderbook_received_at"] = received_at if received_at is not None else ts
            self._append_fast_mid_from_book_locked(
                asset, book, received_at if received_at is not None else ts,
            )

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
        received_at: float | None = None,
    ) -> None:
        if side not in {"buy", "sell"} or price is None or size is None or price <= 0 or size <= 0:
            return
        local_received_at = received_at if received_at is not None else ts
        trade = {
            "ts": ts,
            "received_at": local_received_at,
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
            while rows and float(rows[0].get("received_at") or 0.0) < cutoff:
                rows.popleft()

    # --- recording -------------------------------------------------------
    def record_once(self) -> int:
        now = time.time()
        rows: list[dict[str, Any]] = []
        with self._lock:
            for asset, book in self._books.items():
                row = self._snapshot_locked(asset, book, now)
                if row:
                    self._append_mid_history_locked(asset, row, now)
                    self._latest_snapshot[asset] = dict(row)
                    rows.append(row)
        if not rows:
            return 0
        conn = self._connect()
        pruned = 0
        did_prune = False
        # Disk latency must not block Coinbase/OKX message handling; otherwise
        # BNB/HYPE books can appear stale exactly when the RTI sampler fires.
        for row in rows:
            self._insert_row_locked(conn, row)
        if self._retention_days > 0 and now - self._last_prune_at >= 600.0:
            cutoff = now - (self._retention_days * 86400.0)
            cur = conn.execute(
                "DELETE FROM spot_depth_snapshots WHERE created_at < ?",
                (cutoff,),
            )
            pruned = max(0, int(cur.rowcount or 0))
            did_prune = True
        conn.commit()
        with self._lock:
            if did_prune:
                self._records_pruned += pruned
                self._last_prune_at = now
            self._last_record_at = now
            self._records_written += len(rows)
        return len(rows)

    def _append_mid_history_locked(
        self, asset: str, row: dict[str, Any], now: float,
    ) -> None:
        created_at = _f(row.get("created_at"))
        mid = _f(row.get("mid"))
        if created_at is None or mid is None or mid <= 0.0:
            return
        history = self._mid_history.setdefault(asset, deque())
        history.append({"created_at": created_at, "mid": mid})
        cutoff = now - SPOT_MID_PATH_RETENTION_SECONDS
        while history and float(history[0].get("created_at") or 0.0) < cutoff:
            history.popleft()

    def _append_fast_mid_from_book_locked(
        self, asset: str, book: dict[str, Any], observed_at: float,
    ) -> None:
        """Rate-limit live book events into an in-memory, receipt-time path."""
        history = self._fast_mid_history.setdefault(asset, deque())
        if (
            history
            and observed_at - float(history[-1].get("created_at") or 0.0)
            < SPOT_FAST_MID_SAMPLE_SECONDS * 0.8
        ):
            return
        bids = book.get("bids") or {}
        asks = book.get("asks") or {}
        if not bids or not asks:
            return
        best_bid = _f(book.get("best_bid"))
        best_ask = _f(book.get("best_ask"))
        if best_bid is None:
            best_bid = max(bids)
            book["best_bid"] = best_bid
        if best_ask is None:
            best_ask = min(asks)
            book["best_ask"] = best_ask
        if best_bid <= 0.0 or best_ask <= 0.0 or best_bid >= best_ask:
            return
        history.append({
            "created_at": observed_at,
            "mid": (best_bid + best_ask) / 2.0,
        })
        cutoff = observed_at - SPOT_MID_PATH_RETENTION_SECONDS
        while history and float(history[0].get("created_at") or 0.0) < cutoff:
            history.popleft()

    def _sample_fast_mid_once(self, now: float | None = None) -> int:
        """Observe unchanged live books too, without coupling to SQLite writes."""
        observed_at = time.time() if now is None else now
        sampled = 0
        with self._lock:
            for asset, book in self._books.items():
                received_at = _f(book.get("orderbook_received_at"))
                if (
                    received_at is None
                    or received_at > observed_at
                    or observed_at - received_at > self._max_book_age
                ):
                    continue
                before = len(self._fast_mid_history.get(asset, ()))
                self._append_fast_mid_from_book_locked(asset, book, observed_at)
                if len(self._fast_mid_history.get(asset, ())) > before:
                    sampled += 1
        return sampled

    def _mid_path_features_locked(
        self, asset: str, current_row: dict[str, Any], now: float,
    ) -> dict[str, Any]:
        """Summarize locally available spot-mid paths without future rows."""
        return self._summarize_mid_path_locked(
            history_rows=self._mid_history.get(asset, ()),
            current_row=current_row,
            now=now,
            prefix="spot_mid",
            schema_version=SPOT_MID_PATH_SCHEMA_VERSION,
            time_basis=SPOT_MID_PATH_TIME_BASIS,
            interval_seconds=self._record_seconds,
        )

    def _fast_mid_path_features_locked(
        self, asset: str, current_row: dict[str, Any], now: float,
    ) -> dict[str, Any]:
        """Summarize the outcome-blind event-driven spot-mid reservoir."""
        return self._summarize_mid_path_locked(
            history_rows=self._fast_mid_history.get(asset, ()),
            current_row=current_row,
            now=now,
            prefix="spot_fast_mid",
            schema_version=SPOT_FAST_MID_PATH_SCHEMA_VERSION,
            time_basis=SPOT_FAST_MID_PATH_TIME_BASIS,
            interval_seconds=SPOT_FAST_MID_SAMPLE_SECONDS,
        )

    @staticmethod
    def _summarize_mid_path_locked(
        *,
        history_rows: Iterable[dict[str, float]],
        current_row: dict[str, Any],
        now: float,
        prefix: str,
        schema_version: str,
        time_basis: str,
        interval_seconds: float,
    ) -> dict[str, Any]:
        history = [
            dict(row) for row in history_rows
            if _f(row.get("created_at")) is not None
            and float(row["created_at"]) <= now
            and _f(row.get("mid")) is not None
            and float(row["mid"]) > 0.0
        ]
        current_at = _f(current_row.get("created_at"))
        current_mid = _f(current_row.get("mid"))
        if current_at is not None and current_mid is not None and current_mid > 0.0:
            if not history or abs(float(history[-1]["created_at"]) - current_at) > 1e-9:
                history.append({"created_at": current_at, "mid": current_mid})
        history.sort(key=lambda row: float(row["created_at"]))
        started_at = _f(history[0].get("created_at")) if history else None
        output: dict[str, Any] = {
            f"{prefix}_path_schema_version": schema_version,
            f"{prefix}_path_time_basis": time_basis,
            f"{prefix}_path_captured_at": current_at,
            f"{prefix}_history_started_at": started_at,
            f"{prefix}_history_seconds": (
                None if started_at is None else max(0.0, now - started_at)
            ),
            f"{prefix}_history_retention_seconds": (
                SPOT_MID_PATH_RETENTION_SECONDS
            ),
            f"{prefix}_record_interval_seconds": interval_seconds,
        }
        start_tolerance = max(2.0, interval_seconds * 1.75)
        max_allowed_gap = max(3.0, interval_seconds * 2.0)
        for horizon in SPOT_MID_PATH_HORIZONS:
            cutoff = now - float(horizon)
            prior = next(
                (
                    row for row in reversed(history)
                    if float(row["created_at"]) <= cutoff
                ),
                None,
            )
            after = [
                row for row in history if float(row["created_at"]) > cutoff
            ]
            selected = ([] if prior is None else [prior]) + after
            deduplicated: list[dict[str, Any]] = []
            for row in selected:
                if (
                    deduplicated
                    and abs(
                        float(deduplicated[-1]["created_at"])
                        - float(row["created_at"])
                    ) <= 1e-9
                ):
                    deduplicated[-1] = row
                else:
                    deduplicated.append(row)
            selected = deduplicated
            timestamps = [float(row["created_at"]) for row in selected]
            prices = [float(row["mid"]) for row in selected]
            gaps = [right - left for left, right in zip(timestamps, timestamps[1:])]
            max_gap = max(gaps) if gaps else None
            required_count = max(3, int(float(horizon) / interval_seconds))
            reasons: list[str] = []
            if prior is None:
                reasons.append("HISTORY_DOES_NOT_REACH_WINDOW_START")
            elif cutoff - float(prior["created_at"]) > start_tolerance:
                reasons.append("WINDOW_START_SAMPLE_TOO_OLD")
            if len(selected) < required_count:
                reasons.append("INSUFFICIENT_PATH_SAMPLES")
            if max_gap is None or max_gap > max_allowed_gap:
                reasons.append("PATH_CONTINUITY_GAP")
            if current_at is None or abs(current_at - now) > 2.0:
                reasons.append("CURRENT_CAPTURE_NOT_EXACT")
            complete = not reasons
            start_mid = prices[0] if prices else None
            end_mid = prices[-1] if prices else None
            change_bps = (
                None
                if start_mid is None or end_mid is None or start_mid <= 0.0
                else (end_mid / start_mid - 1.0) * 10_000.0
            )
            range_bps = (
                None
                if not prices or end_mid is None or end_mid <= 0.0
                else (max(prices) - min(prices)) / end_mid * 10_000.0
            )
            returns = [
                10_000.0 * math.log(right / left)
                for left, right in zip(prices, prices[1:])
                if left > 0.0 and right > 0.0
            ]
            realized = (
                None
                if not returns
                else math.sqrt(sum(value * value for value in returns))
            )
            variation = sum(
                abs(right - left) for left, right in zip(prices, prices[1:])
            )
            efficiency = (
                None
                if start_mid is None or end_mid is None
                else 1.0
                if variation == 0.0
                else abs(end_mid - start_mid) / variation
            )
            suffix = f"_{horizon}s"
            output.update({
                f"{prefix}_window_complete{suffix}": complete,
                f"{prefix}_path_missing_reason{suffix}": (
                    None if complete else ",".join(reasons)
                ),
                f"{prefix}_path_count{suffix}": len(selected),
                f"{prefix}_path_start_at{suffix}": (
                    timestamps[0] if timestamps else None
                ),
                f"{prefix}_path_end_at{suffix}": (
                    timestamps[-1] if timestamps else None
                ),
                f"{prefix}_path_max_gap_seconds{suffix}": max_gap,
                f"{prefix}_start{suffix}": start_mid,
                f"{prefix}_end{suffix}": end_mid,
                f"{prefix}_change_bps{suffix}": change_bps,
                f"{prefix}_range_bps{suffix}": range_bps,
                f"{prefix}_realized_volatility_bps{suffix}": realized,
                f"{prefix}_trend_efficiency{suffix}": efficiency,
            })
        return output

    def _snapshot_locked(self, asset: str, book: dict[str, Any], now: float) -> dict[str, Any] | None:
        orderbook_ts = _f(book.get("orderbook_ts"))
        orderbook_received_at = _f(book.get("orderbook_received_at"))
        if orderbook_received_at is None:
            # Compatibility for in-memory fixtures and pre-upgrade books only.
            orderbook_received_at = orderbook_ts
        if (
            orderbook_ts is None
            or orderbook_received_at is None
            or now - orderbook_received_at > self._max_book_age
        ):
            return None
        bids = self._top_levels(book.get("bids") or {}, reverse=True)
        asks = self._top_levels(book.get("asks") or {}, reverse=False)
        if not bids or not asks:
            return None
        best_bid = bids[0][0]
        best_ask = asks[0][0]
        # CROSSED-BOOK GUARD. Both sibling collectors (coinbase_adv_l2, kraken_l3) refuse to
        # publish a crossed book; this one did not, so a delta stream that lost a cancel during
        # a reconnect gap could emit best_bid > best_ask with a NEGATIVE spread and a bogus mid.
        # That mid feeds spot_mid_change_bps_* / realized-vol features consumed by the rti
        # microstructure models, so a corrupt book became a model input rather than an error.
        # Drop the snapshot and force a resync instead of publishing nonsense.
        if best_bid > 0 and best_ask > 0 and best_bid >= best_ask:
            self._crossed_books[asset] = self._crossed_books.get(asset, 0) + 1
            self._resync_assets.add(asset)
            logger.warning(
                "spot_depth crossed book for %s (%s): bid %.8f >= ask %.8f — dropping "
                "snapshot and flagging resync", asset, book.get("provider"),
                best_bid, best_ask)
            return None
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
            "book_age_seconds": round(now - orderbook_received_at, 3),
            "book_source_age_seconds": round(now - orderbook_ts, 3),
            "trade_age_seconds": (
                round(now - float(last_trade.get("received_at")), 3)
                if last_trade.get("received_at") else None
            ),
            "trade_source_age_seconds": (
                round(now - float(last_trade.get("ts")), 3)
                if last_trade.get("ts") else None
            ),
            "trade_side_semantics": "aggressor",
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
            "orderbook_received_at": orderbook_received_at,
            "trade_received_at": last_trade.get("received_at"),
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
        selector = heapq.nlargest if reverse else heapq.nsmallest
        ordered = selector(self.level_count, levels.items(), key=lambda item: item[0])
        return [[price, size, order_count] for price, (size, order_count) in ordered]

    @staticmethod
    def _trade_sums(
        trades: list[dict[str, Any]], now: float, window: float, suffix: str
    ) -> dict[str, float]:
        buy_qty = sell_qty = buy_notional = sell_notional = 0.0
        cutoff = now - window
        for trade in trades:
            if float(trade.get("received_at") or 0.0) < cutoff:
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
            columns = {
                row[1] for row in self._conn.execute("PRAGMA table_info(spot_depth_snapshots)")
            }
            if "trade_side_semantics" not in columns:
                self._conn.execute(
                    "ALTER TABLE spot_depth_snapshots ADD COLUMN trade_side_semantics TEXT"
                )
            for column in (
                "orderbook_received_at",
                "trade_received_at",
                "book_source_age_seconds",
                "trade_source_age_seconds",
            ):
                if column not in columns:
                    self._conn.execute(
                        f"ALTER TABLE spot_depth_snapshots ADD COLUMN {column} REAL"
                    )
            self._conn.commit()
            return self._conn

    @staticmethod
    def _insert_row_locked(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        cols = (
            "created_at", "asset", "provider", "symbol", "source",
            "book_age_seconds", "trade_age_seconds", "trade_side_semantics",
            "best_bid", "best_ask",
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
            "orderbook_ts", "trade_ts", "orderbook_received_at",
            "trade_received_at", "book_source_age_seconds",
            "trade_source_age_seconds",
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

    def _fast_mid_sampler_thread_main(self) -> None:
        """Sample unchanged books outside the busy WebSocket event loop."""
        while not self._stop.is_set():
            started_at = time.time()
            self._sample_fast_mid_once(started_at)
            completed_at = time.time()
            with self._lock:
                prior = self._last_fast_mid_sample_at
                if prior is not None:
                    interval = max(0.0, started_at - prior)
                    self._fast_mid_sampler_max_interval_seconds = max(
                        self._fast_mid_sampler_max_interval_seconds, interval
                    )
                    if interval > SPOT_FAST_MID_SAMPLE_SECONDS * 2.0:
                        self._fast_mid_sampler_late_iterations += 1
                self._last_fast_mid_sample_at = started_at
                self._fast_mid_sampler_iterations += 1
            elapsed = max(0.0, completed_at - started_at)
            self._stop.wait(
                max(0.05, SPOT_FAST_MID_SAMPLE_SECONDS - elapsed)
            )

    async def _recorder_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self.record_once)
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
                    url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_queue=1000,
                    max_size=_ws_max_size(),
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
            "channels": [COINBASE_BOOK_CHANNEL, "matches"],
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


def capture_current_spot_depth(asset: str, max_age: float | None = None) -> dict[str, Any] | None:
    """Return an on-demand live snapshot for a point-in-time decision.

    ``max_age`` remains accepted for drop-in reader compatibility; book age is
    frozen in the result and the strategy's explicit freshness gate decides
    whether it is usable.
    """
    del max_age
    if not _enabled():
        return None
    try:
        return get_feed().capture_current(asset)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("spot depth live capture failed for %s: %s", asset, exc)
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
