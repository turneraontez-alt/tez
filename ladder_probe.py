"""Kalshi strike-ladder research collector.

Default-OFF behind ``Q15_FEED_LADDER``. The refresh loop only enqueues compact
window jobs; all Kalshi GETs and SQLite writes run on the collector worker. The
collector is read-only and never places, modifies, or cancels orders.
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
from typing import Any, Mapping, Sequence

from q15_upgrade.kalshi_rest import KalshiClient
from q15_upgrade.orderbook import parse_orderbook
from q15_upgrade.precision import dollars_to_cents

logger = logging.getLogger(__name__)

CHECKPOINT_MARKS = {
    "15M": 900,
    "13M": 780,
    "12M": 720,
    "11M": 660,
    "10M": 600,
    "9M": 540,
    "8M": 480,
    "7M": 420,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ladder_captures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    age_s REAL NOT NULL,
    asset TEXT NOT NULL,
    close_time REAL,
    checkpoint TEXT,
    snapshot_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    strike REAL,
    yes_bid REAL,
    yes_ask REAL,
    volume REAL,
    implied_sigma REAL,
    ladder_skew REAL,
    ladder_arb_flag INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ladder_asset_window
    ON ladder_captures(asset, close_time, checkpoint);
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
    return _env_bool("Q15_FEED_LADDER", False)


def _db_path() -> str:
    return os.environ.get("Q15_FEED_LADDER_DB", "data/q15_ladder_probe_v1.sqlite3")


def _band_seconds() -> float:
    return _env_float("Q15_FEED_LADDER_BAND_SECONDS", 25.0, minimum=1.0)


def _timeout_seconds() -> float:
    return _env_float("Q15_FEED_LADDER_TIMEOUT_SECONDS", 2.0, minimum=0.25)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _price_cents(*values: Any) -> float | None:
    for value in values:
        raw = _num(value)
        if raw is not None:
            return raw * 100.0 if raw <= 1.0 else raw
        parsed = dollars_to_cents(value)
        if parsed is not None:
            return float(parsed)
    return None


def _volume(market: Mapping[str, Any]) -> float | None:
    return _num(market.get("volume_fp")) or _num(market.get("volume"))


def _strike(market: Mapping[str, Any]) -> float | None:
    for key in ("floor_strike", "strike", "target_price", "settlement_threshold", "cap_strike"):
        value = _num(market.get(key))
        if value is not None:
            return value
    for key in ("yes_sub_title", "no_sub_title", "subtitle"):
        text = str(market.get(key) or "")
        if not text:
            continue
        cleaned = "".join(ch if ch.isdigit() or ch in ".-" else " " for ch in text)
        for part in cleaned.split():
            value = _num(part)
            if value is not None:
                return value
    return None


def _market_quote(market: Mapping[str, Any]) -> tuple[float | None, float | None]:
    yes_bid = _price_cents(
        market.get("yes_bid_dollars"),
        market.get("yes_bid"),
        market.get("yes_bid_cents"),
        market.get("yes_bid_fp"),
    )
    yes_ask = _price_cents(
        market.get("yes_ask_dollars"),
        market.get("yes_ask"),
        market.get("yes_ask_cents"),
        market.get("yes_ask_fp"),
    )
    if yes_ask is None:
        no_bid = _price_cents(market.get("no_bid_dollars"), market.get("no_bid"), market.get("no_bid_cents"))
        if no_bid is not None:
            yes_ask = 100.0 - no_bid
    return yes_bid, yes_ask


def _checkpoint(seconds_remaining: float, band: float) -> tuple[str, int] | None:
    for name, mark in CHECKPOINT_MARKS.items():
        if mark - band <= seconds_remaining <= mark:
            return name, mark
    return None


def ladder_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    points: list[tuple[float, float]] = []
    for row in rows:
        strike = _num(row.get("strike"))
        bid = _num(row.get("yes_bid"))
        ask = _num(row.get("yes_ask"))
        if strike is None or bid is None or ask is None:
            continue
        points.append((strike, (bid + ask) / 2.0))
    points.sort(key=lambda item: item[0])
    if len(points) < 2:
        return {"implied_sigma": None, "ladder_skew": None, "ladder_arb_flag": 0}
    slopes: list[float] = []
    arb = 0
    for (prev_strike, prev_mid), (strike, mid) in zip(points, points[1:]):
        distance = abs(strike - prev_strike)
        if distance > 0:
            slopes.append(abs(mid - prev_mid) / distance)
        if mid > prev_mid + 1e-6:
            arb = 1
    skew = points[-1][1] - points[0][1]
    sigma = sum(slopes) / len(slopes) if slopes else None
    return {
        "implied_sigma": None if sigma is None else round(sigma, 8),
        "ladder_skew": round(skew, 8),
        "ladder_arb_flag": arb,
    }


class LadderProbe:
    def __init__(self, *, db_path: str | None = None, client: Any | None = None) -> None:
        self.db_path = db_path or _db_path()
        self.enabled = _enabled()
        self.band_seconds = _band_seconds()
        self.timeout_seconds = _timeout_seconds()
        self.client = client or KalshiClient()
        self._queue: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=_env_int("Q15_FEED_LADDER_QUEUE", 256))
        self._seen: set[tuple[str, float | None, str]] = set()
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_capture_at: float | None = None
        self._last_error: str | None = None
        self._dropped_jobs = 0
        self._rows_written = 0
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
            self._worker = threading.Thread(target=self._worker_loop, name="ladder-probe", daemon=True)
            self._worker.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def observe(
        self,
        *,
        asset: str,
        series_ticker: str,
        close_time: float | None,
        seconds_remaining: float | None,
        now: float | None = None,
    ) -> bool:
        if not self.enabled or seconds_remaining is None or not series_ticker:
            return False
        cp = _checkpoint(float(seconds_remaining), self.band_seconds)
        if cp is None:
            return False
        checkpoint, _mark = cp
        key = (str(asset).upper(), close_time, checkpoint)
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
        job = {
            "asset": str(asset).upper(),
            "series_ticker": str(series_ticker),
            "close_time": close_time,
            "checkpoint": checkpoint,
            "ts": time.time() if now is None else float(now),
        }
        try:
            self._queue.put_nowait(job)
            self.start()
            return True
        except queue.Full:
            self._dropped_jobs += 1
            return False

    def _discover(self, series_ticker: str, close_time: float | None) -> list[Mapping[str, Any]]:
        min_close = None if close_time is None else int(float(close_time) - 2)
        max_close = None if close_time is None else int(float(close_time) + 2)
        return list(self.client.discover(
            series_ticker,
            limit=200,
            min_close_ts=min_close,
            max_close_ts=max_close,
        ) or [])

    def _quote_with_optional_book(self, ticker: str, market: Mapping[str, Any]) -> tuple[float | None, float | None]:
        yes_bid, yes_ask = _market_quote(market)
        if yes_bid is not None and yes_ask is not None:
            return yes_bid, yes_ask
        try:
            book = self.client.get_orderbook(ticker)
            parsed = parse_orderbook(book)
            return (
                yes_bid if yes_bid is not None else _num(parsed.get("yes_bid")),
                yes_ask if yes_ask is not None else _num(parsed.get("yes_ask")),
            )
        except Exception as exc:  # noqa: BLE001 - per-market best effort
            logger.debug("ladder orderbook fetch skipped for %s: %s", ticker, exc)
            return yes_bid, yes_ask

    def capture_job(self, job: Mapping[str, Any]) -> list[dict[str, Any]]:
        started = time.monotonic()
        markets = self._discover(str(job["series_ticker"]), _num(job.get("close_time")))
        rows: list[dict[str, Any]] = []
        for market in markets:
            if time.monotonic() - started > self.timeout_seconds:
                break
            ticker = str(market.get("ticker") or "")
            if not ticker:
                continue
            yes_bid, yes_ask = self._quote_with_optional_book(ticker, market)
            rows.append({
                "ts": job["ts"],
                "age_s": max(0.0, time.time() - float(job["ts"])),
                "asset": job["asset"],
                "close_time": job.get("close_time"),
                "checkpoint": job["checkpoint"],
                "snapshot_id": f"{job['asset']}:{job.get('close_time')}:{job['checkpoint']}",
                "ticker": ticker,
                "strike": _strike(market),
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "volume": _volume(market),
            })
        metrics = ladder_metrics(rows)
        return [{**row, **metrics} for row in rows]

    def _insert_rows(self, rows: Sequence[Mapping[str, Any]]) -> int:
        if not rows:
            return 0
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        try:
            conn.executemany(
                "INSERT INTO ladder_captures("
                "ts,age_s,asset,close_time,checkpoint,snapshot_id,ticker,strike,"
                "yes_bid,yes_ask,volume,implied_sigma,ladder_skew,ladder_arb_flag"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        row.get("ts"),
                        row.get("age_s"),
                        row.get("asset"),
                        row.get("close_time"),
                        row.get("checkpoint"),
                        row.get("snapshot_id"),
                        row.get("ticker"),
                        row.get("strike"),
                        row.get("yes_bid"),
                        row.get("yes_ask"),
                        row.get("volume"),
                        row.get("implied_sigma"),
                        row.get("ladder_skew"),
                        row.get("ladder_arb_flag"),
                    )
                    for row in rows
                ],
            )
            conn.commit()
            self._rows_written += len(rows)
            self._last_capture_at = time.time()
            return len(rows)
        finally:
            conn.close()

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._insert_rows(self.capture_job(job))
            except Exception as exc:  # noqa: BLE001 - collector boundary
                self._last_error = str(exc)[:300]
                logger.warning("ladder capture failed: %s", exc)
            finally:
                self._queue.task_done()

    def health(self) -> dict[str, Any]:
        now = time.time()
        return {
            "enabled": self.enabled,
            "read_only": True,
            "db_path": self.db_path,
            "last_capture_age_seconds": (
                None if self._last_capture_at is None else round(max(0.0, now - self._last_capture_at), 3)
            ),
            "last_error": self._last_error,
            "rows_written": self._rows_written,
            "dropped_jobs": self._dropped_jobs,
            "queue_size": self._queue.qsize(),
            "timeout_seconds": self.timeout_seconds,
            "status": "disabled" if not self.enabled else "running",
        }


_probe: LadderProbe | None = None
_probe_lock = threading.Lock()


def get_ladder_probe() -> LadderProbe:
    global _probe
    with _probe_lock:
        if _probe is None:
            _probe = LadderProbe()
        return _probe


def start_ladder_probe() -> bool:
    try:
        return get_ladder_probe().start()
    except Exception as exc:  # noqa: BLE001 - startup boundary
        logger.warning("Ladder probe start failed: %s", exc)
        return False


def ladder_health() -> dict[str, Any]:
    try:
        return get_ladder_probe().health()
    except Exception as exc:  # noqa: BLE001
        return {"enabled": _enabled(), "read_only": True, "error": f"{type(exc).__name__}: {exc}"}
