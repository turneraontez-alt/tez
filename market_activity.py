"""Kalshi market-activity recorder.

Default-OFF behind ``Q15_FEED_MARKET_ACTIVITY``. The refresh loop supplies the
market detail and orderbook-change signal it already has; this module only
records and exposes freshness-stamped context. It never calls order endpoints.
"""
from __future__ import annotations

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

_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_activity_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    age_s REAL NOT NULL,
    asset TEXT NOT NULL,
    ticker TEXT NOT NULL,
    cumulative_volume REAL,
    open_interest REAL,
    seconds_since_last_book_change REAL,
    asleep_score REAL
);
CREATE INDEX IF NOT EXISTS idx_market_activity_ticker_ts
    ON market_activity_samples(ticker, ts);
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
    return _env_bool("Q15_FEED_MARKET_ACTIVITY", False)


def _db_path() -> str:
    return os.environ.get("Q15_FEED_MARKET_ACTIVITY_DB", "data/q15_market_activity_v1.sqlite3")


def _record_seconds() -> float:
    return _env_float("Q15_FEED_MARKET_ACTIVITY_RECORD_SECONDS", 5.0, minimum=1.0)


def _stale_seconds() -> float:
    return _env_float("Q15_FEED_MARKET_ACTIVITY_STALE_SECONDS", 10.0, minimum=1.0)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _field(market: Mapping[str, Any], *names: str) -> float | None:
    for name in names:
        value = _num(market.get(name))
        if value is not None:
            return value
    return None


def asleep_score(
    *,
    cumulative_volume: float | None,
    open_interest: float | None,
    seconds_since_last_book_change: float | None,
) -> float | None:
    if seconds_since_last_book_change is None:
        return None
    volume = cumulative_volume if cumulative_volume is not None else 0.0
    oi = open_interest if open_interest is not None else 0.0
    stale_component = min(1.0, max(0.0, seconds_since_last_book_change / 60.0))
    activity_basis = max(volume, oi * 0.25)
    low_activity_component = 1.0 - min(1.0, max(0.0, activity_basis / 100.0))
    return round(100.0 * (0.65 * stale_component + 0.35 * low_activity_component), 4)


def book_changed(delta: Mapping[str, Any] | None) -> bool:
    if not isinstance(delta, Mapping):
        return False
    for key in ("bid_liquidity_added", "bid_liquidity_removed", "ask_liquidity_added", "ask_liquidity_removed"):
        if (_num(delta.get(key)) or 0.0) > 0.0:
            return True
    return bool(delta.get("imbalance_flip"))


class MarketActivityRecorder:
    def __init__(self, *, db_path: str | None = None) -> None:
        self.db_path = db_path or _db_path()
        self.enabled = _enabled()
        self.record_seconds = _record_seconds()
        self.stale_seconds = _stale_seconds()
        self._queue: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=_env_int("Q15_FEED_MARKET_ACTIVITY_QUEUE", 1024))
        self._latest: dict[str, dict[str, Any]] = {}
        self._last_book_change: dict[str, float] = {}
        self._last_recorded: dict[str, float] = {}
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._rows_written = 0
        self._dropped_rows = 0
        self._last_error: str | None = None
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
        if self._worker is None or not self._worker.is_alive():
            self._stop.clear()
            self._worker = threading.Thread(target=self._worker_loop, name="market-activity", daemon=True)
            self._worker.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def observe(
        self,
        *,
        asset: str,
        ticker: str,
        market: Mapping[str, Any],
        orderbook_changed: bool,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        if not self.enabled or not ticker:
            return None
        ts = time.time() if now is None else float(now)
        ticker_key = str(ticker)
        asset_key = str(asset).upper()
        with self._lock:
            if orderbook_changed or ticker_key not in self._last_book_change:
                self._last_book_change[ticker_key] = ts
            last_change = self._last_book_change.get(ticker_key)
            since_change = None if last_change is None else max(0.0, ts - last_change)
            last_recorded = self._last_recorded.get(ticker_key, 0.0)
        volume = _field(market, "volume", "volume_fp", "_volume")
        open_interest = _field(market, "open_interest", "open_interest_fp")
        score = asleep_score(
            cumulative_volume=volume,
            open_interest=open_interest,
            seconds_since_last_book_change=since_change,
        )
        row = {
            "ts": ts,
            "age_s": 0.0,
            "asset": asset_key,
            "ticker": ticker_key,
            "cumulative_volume": volume,
            "open_interest": open_interest,
            "seconds_since_last_book_change": since_change,
            "asleep_score": score,
        }
        with self._lock:
            self._latest[ticker_key] = dict(row)
            self._latest[asset_key] = dict(row)
            if ts - last_recorded < self.record_seconds:
                return row
            self._last_recorded[ticker_key] = ts
        try:
            self._queue.put_nowait(row)
            self.start()
        except queue.Full:
            self._dropped_rows += 1
        return row

    def latest(self, *, asset: str | None = None, ticker: str | None = None, now: float | None = None) -> dict[str, Any] | None:
        current = time.time() if now is None else float(now)
        key = str(ticker or asset or "").upper()
        if not key:
            return None
        with self._lock:
            row = dict(self._latest.get(key) or {})
        if not row:
            return None
        age = current - float(row.get("ts") or current)
        if age > self.stale_seconds:
            return None
        row["age_s"] = max(0.0, age)
        return row

    def context(self, *, asset: str | None = None, ticker: str | None = None, now: float | None = None) -> dict[str, Any]:
        row = self.latest(asset=asset, ticker=ticker, now=now)
        if row is None:
            return {
                "market_volume": None,
                "open_interest": None,
                "seconds_since_last_book_change": None,
                "asleep_score": None,
                "market_activity_age_s": None,
            }
        return {
            "market_volume": row.get("cumulative_volume"),
            "open_interest": row.get("open_interest"),
            "seconds_since_last_book_change": row.get("seconds_since_last_book_change"),
            "asleep_score": row.get("asleep_score"),
            "market_activity_age_s": row.get("age_s"),
        }

    def _worker_loop(self) -> None:
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
                    logger.warning("market activity recorder error: %s", exc)
                finally:
                    self._queue.task_done()
        finally:
            conn.close()

    def _insert(self, conn: sqlite3.Connection, row: Mapping[str, Any]) -> None:
        conn.execute(
            "INSERT INTO market_activity_samples("
            "ts,age_s,asset,ticker,cumulative_volume,open_interest,"
            "seconds_since_last_book_change,asleep_score"
            ") VALUES(?,?,?,?,?,?,?,?)",
            (
                row.get("ts"),
                row.get("age_s"),
                row.get("asset"),
                row.get("ticker"),
                row.get("cumulative_volume"),
                row.get("open_interest"),
                row.get("seconds_since_last_book_change"),
                row.get("asleep_score"),
            ),
        )
        conn.commit()
        self._rows_written += 1

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
            ages = [
                max(0.0, now - float(row.get("ts") or now))
                for key, row in self._latest.items()
                if "-" in key
            ]
        latest_age = min(ages) if ages else None
        return {
            "enabled": self.enabled,
            "read_only": True,
            "db_path": self.db_path,
            "latest_age_seconds": None if latest_age is None else round(latest_age, 3),
            "rows_written": self._rows_written,
            "dropped_rows": self._dropped_rows,
            "queue_size": self._queue.qsize(),
            "last_error": self._last_error,
            "stale_seconds": self.stale_seconds,
            "status": "disabled" if not self.enabled else "running",
        }


_recorder: MarketActivityRecorder | None = None
_lock = threading.Lock()


def get_market_activity_recorder() -> MarketActivityRecorder:
    global _recorder
    with _lock:
        if _recorder is None:
            _recorder = MarketActivityRecorder()
        return _recorder


def start_market_activity() -> bool:
    try:
        return get_market_activity_recorder().start()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Market activity recorder start failed: %s", exc)
        return False


def market_activity_context(*, asset: str | None = None, ticker: str | None = None, now: float | None = None) -> dict[str, Any]:
    try:
        rec = get_market_activity_recorder()
        if not rec.enabled:
            return {
                "market_volume": None,
                "open_interest": None,
                "seconds_since_last_book_change": None,
                "asleep_score": None,
                "market_activity_age_s": None,
            }
        return rec.context(asset=asset, ticker=ticker, now=now)
    except Exception:
        logger.debug("market activity context unavailable", exc_info=True)
        return {
            "market_volume": None,
            "open_interest": None,
            "seconds_since_last_book_change": None,
            "asleep_score": None,
            "market_activity_age_s": None,
        }


def market_activity_health() -> dict[str, Any]:
    try:
        return get_market_activity_recorder().health()
    except Exception as exc:  # noqa: BLE001
        return {"enabled": _enabled(), "read_only": True, "error": f"{type(exc).__name__}: {exc}"}
