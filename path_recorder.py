"""Final-window path recorder.

Default-OFF behind ``Q15_FEED_PATH_RECORDER``. The refresh loop contributes one
compact point per active asset/window; this module keeps an in-memory bounded
ring and writes one compressed row per window after settlement. It is read-only.
"""
from __future__ import annotations

from collections import deque
import gzip
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

_SCHEMA = """
CREATE TABLE IF NOT EXISTS window_paths (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    age_s REAL NOT NULL,
    asset TEXT NOT NULL,
    close_time REAL NOT NULL,
    point_count INTEGER NOT NULL,
    path_json_gz BLOB NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_window_paths_asset_close
    ON window_paths(asset, close_time);
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
    return _env_bool("Q15_FEED_PATH_RECORDER", False)


def _db_path() -> str:
    return os.environ.get("Q15_FEED_PATH_RECORDER_DB", "data/q15_path_recorder_v1.sqlite3")


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _yes_mid(yes_bid: Any, yes_ask: Any) -> float | None:
    bid = _num(yes_bid)
    ask = _num(yes_ask)
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2.0


class PathRecorder:
    def __init__(self, *, db_path: str | None = None) -> None:
        self.db_path = db_path or _db_path()
        self.enabled = _enabled()
        self.max_points_per_window = _env_int("Q15_FEED_PATH_RECORDER_MAX_POINTS", 900)
        self.sample_seconds = _env_float("Q15_FEED_PATH_RECORDER_SAMPLE_SECONDS", 1.0, minimum=0.1)
        self._paths: dict[tuple[str, float], deque[dict[str, float | None]]] = {}
        self._last_sample_at: dict[tuple[str, float], float] = {}
        self._flushed: set[tuple[str, float]] = set()
        self._lock = threading.Lock()
        self._queue: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=_env_int("Q15_FEED_PATH_RECORDER_QUEUE", 128))
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._rows_written = 0
        self._dropped_flushes = 0
        self._last_flush_at: float | None = None
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
            self._worker = threading.Thread(target=self._worker_loop, name="path-recorder", daemon=True)
            self._worker.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def observe(
        self,
        *,
        asset: str,
        close_time: float | None,
        seconds_remaining: float | None,
        index_px: float | None = None,
        spot_px: float | None = None,
        yes_bid: float | None = None,
        yes_ask: float | None = None,
        now: float | None = None,
    ) -> bool:
        if not self.enabled or close_time is None or seconds_remaining is None:
            return False
        sr = float(seconds_remaining)
        if sr < 0.0 or sr > 900.0:
            return False
        ts = time.time() if now is None else float(now)
        key = (str(asset).upper(), float(close_time))
        with self._lock:
            if key in self._flushed:
                return False
            last = self._last_sample_at.get(key, 0.0)
            if ts - last < self.sample_seconds:
                return False
            self._last_sample_at[key] = ts
            path = self._paths.setdefault(key, deque(maxlen=self.max_points_per_window))
            px = _num(index_px)
            source = "index" if px is not None else "spot"
            if px is None:
                px = _num(spot_px)
            path.append({
                "ts": ts,
                "px": px,
                "px_source": source,
                "yes_mid": _yes_mid(yes_bid, yes_ask),
            })
        self.start()
        return True

    def flush_window(self, *, asset: str, close_time: float | None, now: float | None = None) -> bool:
        if not self.enabled or close_time is None:
            return False
        ts = time.time() if now is None else float(now)
        key = (str(asset).upper(), float(close_time))
        with self._lock:
            if key in self._flushed:
                return False
            points = list(self._paths.pop(key, deque()))
            self._flushed.add(key)
            self._last_sample_at.pop(key, None)
        if not points:
            return False
        job = {"ts": ts, "asset": key[0], "close_time": key[1], "points": points}
        try:
            self._queue.put_nowait(job)
            self.start()
            return True
        except queue.Full:
            self._dropped_flushes += 1
            return False

    def flush_expired(self, *, now: float | None = None) -> int:
        current = time.time() if now is None else float(now)
        with self._lock:
            keys = [key for key in self._paths if key[1] <= current]
        return sum(1 for asset, close_time in keys if self.flush_window(asset=asset, close_time=close_time, now=current))

    def _insert(self, conn: sqlite3.Connection, job: Mapping[str, Any]) -> None:
        points = list(job.get("points") or [])
        payload = gzip.compress(json.dumps(points, sort_keys=True, separators=(",", ":")).encode("utf-8"), mtime=0)
        conn.execute(
            "INSERT OR IGNORE INTO window_paths(ts,age_s,asset,close_time,point_count,path_json_gz) "
            "VALUES(?,?,?,?,?,?)",
            (
                job.get("ts"),
                max(0.0, time.time() - float(job.get("ts") or time.time())),
                job.get("asset"),
                job.get("close_time"),
                len(points),
                payload,
            ),
        )
        conn.commit()
        self._rows_written += 1
        self._last_flush_at = time.time()

    def _worker_loop(self) -> None:
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        try:
            while not self._stop.is_set():
                try:
                    job = self._queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                try:
                    self._insert(conn, job)
                except sqlite3.Error as exc:
                    self._last_error = str(exc)[:200]
                    logger.warning("path recorder insert failed: %s", exc)
                finally:
                    self._queue.task_done()
        finally:
            conn.close()

    def _drain_once_for_tests(self) -> bool:
        try:
            job = self._queue.get_nowait()
        except queue.Empty:
            return False
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        try:
            self._insert(conn, job)
            return True
        finally:
            conn.close()
            self._queue.task_done()

    def health(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            active_windows = len(self._paths)
            point_count = sum(len(path) for path in self._paths.values())
            latest_point_ts = max(
                (path[-1]["ts"] for path in self._paths.values() if path),
                default=None,
            )
        return {
            "enabled": self.enabled,
            "read_only": True,
            "db_path": self.db_path,
            "active_windows": active_windows,
            "points_in_memory": point_count,
            "max_points_per_window": self.max_points_per_window,
            "rows_written": self._rows_written,
            "dropped_flushes": self._dropped_flushes,
            "latest_point_age_seconds": (
                None if latest_point_ts is None else round(max(0.0, now - float(latest_point_ts)), 3)
            ),
            "last_flush_age_seconds": (
                None if self._last_flush_at is None else round(max(0.0, now - self._last_flush_at), 3)
            ),
            "queue_size": self._queue.qsize(),
            "last_error": self._last_error,
            "status": "disabled" if not self.enabled else "running",
        }


_recorder: PathRecorder | None = None
_lock = threading.Lock()


def get_path_recorder() -> PathRecorder:
    global _recorder
    with _lock:
        if _recorder is None:
            _recorder = PathRecorder()
        return _recorder


def start_path_recorder() -> bool:
    try:
        return get_path_recorder().start()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Path recorder start failed: %s", exc)
        return False


def path_recorder_health() -> dict[str, Any]:
    try:
        return get_path_recorder().health()
    except Exception as exc:  # noqa: BLE001
        return {"enabled": _enabled(), "read_only": True, "error": f"{type(exc).__name__}: {exc}"}
