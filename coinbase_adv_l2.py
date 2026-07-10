"""Coinbase Advanced Trade full Level2 book collector.

Advanced Trade does not expose a public ``level3`` channel in the documented
channel list. Its ``level2`` channel sends the full price-level book snapshot
and every subsequent price-level update. Market-data subscriptions are public;
when a valid CDP JSON key is available the collector still uses the JWT flow.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict, deque
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterable

logger = logging.getLogger(__name__)
_last_stale_warning_at = 0.0

try:
    from coinbase import jwt_generator

    _HAVE_COINBASE_SDK = True
    _COINBASE_SDK_ERROR = None
except Exception as exc:  # pragma: no cover - optional dependency guard
    jwt_generator = None
    _HAVE_COINBASE_SDK = False
    _COINBASE_SDK_ERROR = f"{type(exc).__name__}: {exc}"

try:
    import websockets

    _HAVE_WS = True
except Exception:  # pragma: no cover - optional dependency guard
    websockets = None
    _HAVE_WS = False


ADVANCED_TRADE_WS = "wss://advanced-trade-ws.coinbase.com"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS coinbase_adv_l2_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    product_id TEXT NOT NULL,
    sequence_num INTEGER,
    last_message_age_seconds REAL,
    best_bid REAL,
    best_ask REAL,
    mid REAL,
    spread_bps REAL,
    bid_level_count INTEGER,
    ask_level_count INTEGER,
    stored_bid_level_count INTEGER,
    stored_ask_level_count INTEGER,
    bid_depth_levels REAL,
    ask_depth_levels REAL,
    bid_notional_levels REAL,
    ask_notional_levels REAL,
    depth_imbalance REAL,
    bid_levels_json TEXT,
    ask_levels_json TEXT,
    update_count_5s INTEGER,
    remove_count_5s INTEGER,
    update_count_15s INTEGER,
    remove_count_15s INTEGER,
    update_count_60s INTEGER,
    remove_count_60s INTEGER,
    snapshot_loaded INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_coinbase_adv_l2_product_time
    ON coinbase_adv_l2_snapshots(product_id, created_at);
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


def _f(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return out


def _enabled() -> bool:
    return _env_bool("Q15_COINBASE_ADV_L2_ENABLED", False)


def _db_path() -> str:
    return os.environ.get("Q15_COINBASE_ADV_L2_DB", "data/q15_coinbase_adv_l2_v1.sqlite3")


def _record_seconds() -> float:
    return _env_float("Q15_COINBASE_ADV_L2_RECORD_SECONDS", 5.0, minimum=1.0)


def _summary_levels() -> int:
    return _env_int("Q15_COINBASE_ADV_L2_SUMMARY_LEVELS", 250, minimum=1)


def _jwt_refresh_seconds() -> float:
    return _env_float("Q15_COINBASE_ADV_L2_JWT_REFRESH_SECONDS", 110.0, minimum=30.0)


def _ws_max_size() -> int:
    return _env_int(
        "Q15_COINBASE_ADV_L2_WS_MAX_SIZE_BYTES",
        128 * 1024 * 1024,
        minimum=1024 * 1024,
    )


def _retention_days() -> float:
    return _env_float("Q15_COINBASE_ADV_L2_RETENTION_DAYS", 7.0, minimum=0.0)


def _stale_warning_seconds() -> float:
    return _env_float("Q15_COINBASE_ADV_L2_STALE_WARNING_SECONDS", 600.0, minimum=60.0)


def _stale_warning_cooldown_seconds() -> float:
    return _env_float("Q15_COINBASE_ADV_L2_STALE_WARNING_COOLDOWN_SECONDS", 600.0, minimum=60.0)


def _configured_products() -> list[str]:
    raw = os.environ.get("Q15_COINBASE_ADV_L2_PRODUCTS", "").strip()
    if raw:
        products = [p.strip().upper() for p in raw.split(",") if p.strip()]
    else:
        products = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD"]
    out: list[str] = []
    seen: set[str] = set()
    for product in products:
        if product and product not in seen:
            out.append(product)
            seen.add(product)
    return out


def _default_key_file() -> str:
    downloads = Path.home() / "Downloads"
    if downloads.exists():
        matches = sorted(
            downloads.glob("cdp_api_key*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if matches:
            return str(matches[0])
    return str(downloads / "cdp_api_key.json")


def _key_file() -> str:
    return os.environ.get("Q15_COINBASE_ADV_L2_KEY_FILE") or _default_key_file()


def load_cdp_key(path: str | None = None) -> tuple[str, str]:
    key_path = Path(path or _key_file()).expanduser()
    env_path = os.environ.get("Q15_COINBASE_ADV_L2_KEY_FILE")
    if not key_path.exists() and (path is None or (env_path and str(key_path) == str(Path(env_path).expanduser()))):
        fallback = Path(_default_key_file()).expanduser()
        if fallback != key_path and fallback.exists():
            key_path = fallback
    data = json.loads(key_path.read_text(encoding="utf-8"))
    key_name = data.get("name") or data.get("id")
    private_key = data.get("privateKey")
    if not key_name or not private_key:
        raise ValueError("CDP key JSON must contain name/id and privateKey")
    return str(key_name), str(private_key)


def _has_key_file() -> bool:
    try:
        load_cdp_key()
        return True
    except Exception:
        return False


def _preferred_connection_mode() -> str:
    return "authenticated" if _HAVE_COINBASE_SDK and _has_key_file() else "public"


def _snapshot_age_health(db_path: str, products: Iterable[str], now: float) -> dict[str, Any]:
    path = Path(db_path)
    if not path.exists():
        return {
            "snapshot_age_seconds": None,
            "latest_snapshot_age_seconds": None,
            "snapshot_age_by_product_seconds": {},
        }
    try:
        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
            rows = conn.execute(
                "SELECT product_id, MAX(created_at) AS latest_at "
                "FROM coinbase_adv_l2_snapshots GROUP BY product_id"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {
            "snapshot_age_seconds": None,
            "latest_snapshot_age_seconds": None,
            "snapshot_age_by_product_seconds": {},
            "snapshot_age_error": str(exc)[:200],
        }
    configured = {str(p).upper() for p in products}
    ages: dict[str, float] = {}
    for product_id, latest_at in rows:
        product = str(product_id or "").upper()
        if configured and product not in configured:
            continue
        try:
            ts = float(latest_at)
        except (TypeError, ValueError):
            continue
        age = max(0.0, now - ts)
        ages[product] = round(age, 3)
    return {
        "snapshot_age_seconds": round(max(ages.values()), 3) if ages else None,
        "latest_snapshot_age_seconds": round(min(ages.values()), 3) if ages else None,
        "snapshot_age_by_product_seconds": ages,
    }


def _annotate_and_warn_stale(info: dict[str, Any], now: float) -> dict[str, Any]:
    global _last_stale_warning_at
    threshold = _stale_warning_seconds()
    age = _f(info.get("snapshot_age_seconds"))
    stale = bool(age is not None and age > threshold)
    info["snapshot_stale"] = stale
    info["snapshot_stale_seconds"] = threshold
    if not (_enabled() and stale):
        return info
    cooldown = _stale_warning_cooldown_seconds()
    if _last_stale_warning_at and now - _last_stale_warning_at < cooldown:
        return info
    _last_stale_warning_at = now
    logger.warning(
        "Coinbase Advanced L2 snapshots stale: age %.0fs exceeds %.0fs; "
        "thread_alive=%s status=%s thread_error=%s",
        age,
        threshold,
        info.get("thread_alive"),
        info.get("status"),
        info.get("thread_error"),
    )
    return info


class CoinbaseAdvancedL2Collector:
    """Advanced Trade L2 collector with optional CDP authentication."""

    def __init__(
        self,
        products: Iterable[str] | None = None,
        db_path: str | None = None,
        key_file: str | None = None,
    ) -> None:
        self.products = list(products or _configured_products())
        self.db_path = db_path or _db_path()
        self.key_file = key_file or _key_file()
        self.record_seconds = _record_seconds()
        self.summary_levels = _summary_levels()
        self.jwt_refresh_seconds = _jwt_refresh_seconds()
        self.retention_days = _retention_days()
        self._lock = threading.Lock()
        self._books: dict[str, dict[str, dict[float, float]]] = {
            p: {"bid": {}, "ask": {}} for p in self.products
        }
        self._snapshot_loaded: dict[str, bool] = {p: False for p in self.products}
        self._sequence_num: dict[str, int] = {}
        self._updates: dict[str, deque[dict[str, Any]]] = {p: deque() for p in self.products}
        self._last_message_at: float | None = None
        self._last_record_at: float | None = None
        self._last_error: dict[str, str] = {}
        self._connected = False
        self._connection_mode = _preferred_connection_mode()
        self._records_written = 0
        self._updates_seen = 0
        self._last_prune_at = 0.0
        self._conn: sqlite3.Connection | None = None
        self._thread: threading.Thread | None = None
        self._thread_started_at: float | None = None
        self._thread_exited_at: float | None = None
        self._thread_error: str | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if not _HAVE_WS:
            logger.warning("Coinbase Advanced L2 disabled: `pip install websockets`")
            return
        if not self.products:
            logger.warning("Coinbase Advanced L2 not started: no products configured")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread_started_at = time.time()
        self._thread_exited_at = None
        self._thread_error = None
        self._thread = threading.Thread(target=self._thread_main, name="coinbase-adv-l2", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def health(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            thread_alive = bool(self._thread and self._thread.is_alive())
            info = {
                "enabled": _enabled(),
                "have_ws": _HAVE_WS,
                "have_coinbase_sdk": _HAVE_COINBASE_SDK,
                "coinbase_sdk_error": _COINBASE_SDK_ERROR,
                "authenticated_key_loaded": _has_key_file(),
                "connection_mode": self._connection_mode,
                "key_file": self.key_file,
                "products": list(self.products),
                "db_path": self.db_path,
                "connected": self._connected,
                "snapshot_loaded": dict(self._snapshot_loaded),
                "sequence_num": dict(self._sequence_num),
                "last_message_age_seconds": (
                    round(now - self._last_message_at, 3) if self._last_message_at else None
                ),
                "last_record_age_seconds": (
                    round(now - self._last_record_at, 3) if self._last_record_at else None
                ),
                "records_written": self._records_written,
                "updates_seen": self._updates_seen,
                "summary_levels": self.summary_levels,
                "retention_days": self.retention_days,
                "estimated_summary_rows_per_day": self._estimated_summary_rows_per_day(),
                "estimated_summary_mb_per_day": self._estimated_summary_mb_per_day(),
                "thread_alive": thread_alive,
                "thread_started_at": self._thread_started_at,
                "thread_exit_age_seconds": (
                    round(now - self._thread_exited_at, 3) if self._thread_exited_at else None
                ),
                "thread_error": self._thread_error,
                "last_error": dict(self._last_error),
                "status": self._status_locked(),
            }
        info.update(_snapshot_age_health(self.db_path, self.products, now))
        return _annotate_and_warn_stale(info, now)

    def _status_locked(self) -> str:
        if not _enabled():
            return "disabled"
        if not _HAVE_WS:
            return "websockets_missing"
        if self._connected:
            return "connected"
        if self._thread_exited_at and not self._stop.is_set():
            return "thread_exited"
        return "starting"

    def _estimated_summary_rows_per_day(self) -> int:
        if self.record_seconds <= 0:
            return 0
        return int((86400.0 / self.record_seconds) * max(1, len(self.products)))

    def _estimated_summary_mb_per_day(self) -> float:
        bytes_per_row = 1400 + (max(1, self.summary_levels) * 48)
        return round((self._estimated_summary_rows_per_day() * bytes_per_row) / (1024 * 1024), 2)

    def handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except Exception:
            return
        if data.get("type") == "error":
            with self._lock:
                self._last_error["coinbase_message"] = str(data.get("message") or data)[:200]
            return
        channel = data.get("channel")
        if channel == "heartbeats":
            with self._lock:
                self._last_message_at = time.time()
            return
        if channel != "l2_data":
            return
        sequence_num = data.get("sequence_num")
        now = time.time()
        for event in data.get("events") or []:
            product_id = event.get("product_id")
            if product_id not in self._books:
                continue
            if event.get("type") == "snapshot":
                with self._lock:
                    self._books[product_id] = {"bid": {}, "ask": {}}
                    self._snapshot_loaded[product_id] = True
            self._apply_updates(product_id, event.get("updates") or [], now, sequence_num)
        with self._lock:
            self._last_message_at = now

    def _apply_updates(
        self, product_id: str, updates: Iterable[dict[str, Any]], now: float, sequence_num: Any
    ) -> None:
        with self._lock:
            if sequence_num is not None:
                try:
                    self._sequence_num[product_id] = int(sequence_num)
                except (TypeError, ValueError):
                    pass
            book = self._books.setdefault(product_id, {"bid": {}, "ask": {}})
            history = self._updates.setdefault(product_id, deque())
            for update in updates:
                side = self._normalize_side(update.get("side"))
                price = _f(update.get("price_level"))
                qty = _f(update.get("new_quantity"))
                if side is None or price is None or price <= 0 or qty is None or qty < 0:
                    continue
                removed = qty <= 0
                if removed:
                    book[side].pop(price, None)
                else:
                    book[side][price] = qty
                history.append({
                    "created_at": now,
                    "side": side,
                    "price": price,
                    "qty": qty,
                    "removed": removed,
                })
                self._updates_seen += 1
            cutoff = now - 120.0
            while history and float(history[0].get("created_at") or 0.0) < cutoff:
                history.popleft()

    @staticmethod
    def _normalize_side(value: Any) -> str | None:
        text = str(value or "").strip().lower()
        if text in {"bid", "buy"}:
            return "bid"
        if text in {"ask", "offer", "sell"}:
            return "ask"
        return None

    def record_once(self) -> int:
        now = time.time()
        with self._lock:
            rows = [self._snapshot_locked(product, now) for product in self.products]
            rows = [row for row in rows if row is not None]
        if not rows:
            return 0
        conn = self._connect()
        with self._lock:
            for row in rows:
                self._insert_row_locked(conn, row)
            self._prune_locked(conn, now)
            conn.commit()
            self._records_written += len(rows)
            self._last_record_at = now
        return len(rows)

    def _snapshot_locked(self, product_id: str, now: float) -> dict[str, Any] | None:
        if not self._snapshot_loaded.get(product_id):
            return None
        book = self._books.get(product_id) or {}
        bids = self._top_levels(book.get("bid") or {}, reverse=True)
        asks = self._top_levels(book.get("ask") or {}, reverse=False)
        if not bids or not asks:
            return None
        best_bid = bids[0][0]
        best_ask = asks[0][0]
        mid = (best_bid + best_ask) / 2.0
        bid_depth = sum(row[1] for row in bids)
        ask_depth = sum(row[1] for row in asks)
        denom = bid_depth + ask_depth
        history = list(self._updates.get(product_id) or [])
        row = {
            "created_at": now,
            "product_id": product_id,
            "sequence_num": self._sequence_num.get(product_id),
            "last_message_age_seconds": (
                round(now - self._last_message_at, 3) if self._last_message_at else None
            ),
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
            "spread_bps": ((best_ask - best_bid) / mid * 10_000.0) if mid else None,
            "bid_level_count": len(book.get("bid") or {}),
            "ask_level_count": len(book.get("ask") or {}),
            "stored_bid_level_count": len(bids),
            "stored_ask_level_count": len(asks),
            "bid_depth_levels": bid_depth,
            "ask_depth_levels": ask_depth,
            "bid_notional_levels": sum(row[0] * row[1] for row in bids),
            "ask_notional_levels": sum(row[0] * row[1] for row in asks),
            "depth_imbalance": ((bid_depth - ask_depth) / denom) if denom > 0 else None,
            "bid_levels_json": json.dumps(bids, separators=(",", ":")),
            "ask_levels_json": json.dumps(asks, separators=(",", ":")),
            "snapshot_loaded": 1,
        }
        row.update(self._update_sums(history, now, 5.0, "5s"))
        row.update(self._update_sums(history, now, 15.0, "15s"))
        row.update(self._update_sums(history, now, 60.0, "60s"))
        return row

    def _top_levels(self, levels: dict[float, float], *, reverse: bool) -> list[list[float]]:
        ordered = sorted(levels.items(), key=lambda item: item[0], reverse=reverse)[: self.summary_levels]
        return [[price, qty] for price, qty in ordered]

    @staticmethod
    def _update_sums(history: list[dict[str, Any]], now: float, window: float, suffix: str) -> dict[str, int]:
        cutoff = now - window
        update_count = 0
        remove_count = 0
        for update in history:
            if float(update.get("created_at") or 0.0) < cutoff:
                continue
            update_count += 1
            if update.get("removed"):
                remove_count += 1
        return {
            f"update_count_{suffix}": update_count,
            f"remove_count_{suffix}": remove_count,
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
            "created_at", "product_id", "sequence_num", "last_message_age_seconds",
            "best_bid", "best_ask", "mid", "spread_bps", "bid_level_count",
            "ask_level_count", "stored_bid_level_count", "stored_ask_level_count",
            "bid_depth_levels", "ask_depth_levels", "bid_notional_levels",
            "ask_notional_levels", "depth_imbalance", "bid_levels_json",
            "ask_levels_json", "update_count_5s", "remove_count_5s",
            "update_count_15s", "remove_count_15s", "update_count_60s",
            "remove_count_60s", "snapshot_loaded",
        )
        conn.execute(
            f"INSERT INTO coinbase_adv_l2_snapshots({','.join(cols)}) "
            f"VALUES({','.join('?' for _ in cols)})",
            [row.get(col) for col in cols],
        )

    def _prune_locked(self, conn: sqlite3.Connection, now: float) -> None:
        if self.retention_days <= 0 or now - self._last_prune_at < 600.0:
            return
        cutoff = now - (self.retention_days * 86400.0)
        conn.execute("DELETE FROM coinbase_adv_l2_snapshots WHERE created_at < ?", (cutoff,))
        self._last_prune_at = now

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:  # pragma: no cover - thread guard
            err = f"{type(exc).__name__}: {exc}"
            with self._lock:
                self._thread_error = err[:200]
                self._last_error["thread"] = err[:200]
            logger.warning("Coinbase Advanced L2 thread exited: %s", exc)
        finally:
            with self._lock:
                self._connected = False
                self._thread_exited_at = time.time()

    async def _run(self) -> None:
        await asyncio.gather(self._provider_loop(), self._recorder_loop())

    async def _recorder_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.record_once()
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._last_error["recorder"] = str(exc)[:200]
                logger.warning("Coinbase Advanced L2 recorder error: %s", exc)
            await asyncio.sleep(self.record_seconds)

    async def _provider_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                key_name: str | None = None
                private_key: str | None = None
                if _HAVE_COINBASE_SDK:
                    try:
                        key_name, private_key = load_cdp_key(self.key_file)
                    except Exception:
                        pass
                connection_mode = "authenticated" if key_name and private_key else "public"
                async with websockets.connect(
                    ADVANCED_TRADE_WS,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_queue=5000,
                    max_size=_ws_max_size(),
                ) as socket:
                    with self._lock:
                        self._connected = True
                        self._connection_mode = connection_mode
                        self._last_error.pop("coinbase", None)
                    await self._subscribe(socket, key_name, private_key)
                    backoff = 1.0
                    reconnect_at = (
                        time.monotonic() + self.jwt_refresh_seconds
                        if connection_mode == "authenticated"
                        else float("inf")
                    )
                    while not self._stop.is_set() and time.monotonic() < reconnect_at:
                        try:
                            message = await asyncio.wait_for(socket.recv(), timeout=20.0)
                        except asyncio.TimeoutError:
                            continue
                        self.handle_message(message)
            except Exception as exc:
                with self._lock:
                    self._last_error["coinbase"] = str(exc)[:200]
                logger.warning("Coinbase Advanced L2 reconnecting after error: %s", exc)
            finally:
                with self._lock:
                    self._connected = False
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 1.8)

    async def _subscribe(
        self,
        socket: Any,
        key_name: str | None = None,
        private_key: str | None = None,
    ) -> None:
        authenticated = bool(key_name and private_key and jwt_generator is not None)
        heartbeat = {
            "type": "subscribe",
            "channel": "heartbeats",
        }
        if authenticated:
            heartbeat["jwt"] = jwt_generator.build_ws_jwt(key_name, private_key)
        await socket.send(json.dumps(heartbeat))
        for product_id in self.products:
            subscription = {
                "type": "subscribe",
                "product_ids": [product_id],
                "channel": "level2",
            }
            if authenticated:
                subscription["jwt"] = jwt_generator.build_ws_jwt(key_name, private_key)
            await socket.send(json.dumps(subscription))


_feed: CoinbaseAdvancedL2Collector | None = None
_feed_lock = threading.Lock()


def get_coinbase_adv_l2_feed() -> CoinbaseAdvancedL2Collector:
    global _feed
    with _feed_lock:
        if _feed is None:
            _feed = CoinbaseAdvancedL2Collector()
            _feed.start()
        return _feed


def start_coinbase_adv_l2() -> None:
    if not _enabled():
        return
    try:
        get_coinbase_adv_l2_feed()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Coinbase Advanced L2 start failed: %s", exc)


def coinbase_adv_l2_health() -> dict[str, Any]:
    now = time.time()
    products = _configured_products()
    db_path = _db_path()
    info = {
        "enabled": _enabled(),
        "have_ws": _HAVE_WS,
        "have_coinbase_sdk": _HAVE_COINBASE_SDK,
        "coinbase_sdk_error": _COINBASE_SDK_ERROR,
        "authenticated_key_loaded": _has_key_file(),
        "connection_mode": _preferred_connection_mode(),
        "key_file": _key_file(),
        "products": products,
        "db_path": db_path,
        "summary_levels": _summary_levels(),
        "retention_days": _retention_days(),
    }
    info.update(_snapshot_age_health(db_path, products, now))
    if not _enabled() or _feed is None:
        if _enabled():
            info["status"] = "websockets_missing" if not _HAVE_WS else f"ready_{info['connection_mode']}"
        return _annotate_and_warn_stale(info, now)
    try:
        info.update(_feed.health())
    except Exception as exc:  # pragma: no cover - defensive
        info["error"] = str(exc)[:200]
    return _annotate_and_warn_stale(info, now)
