"""Bounded worker that records prospective path forecasts without alerting."""
from __future__ import annotations

from collections import deque
import json
import logging
import math
import os
from pathlib import Path
import queue
import sqlite3
import threading
import time
from typing import Any, Mapping

import numpy as np

from .ledger import PathForecastLedger
from .model import MODEL_VERSION, PathForecastModel
from .reconstruct import (
    CHECKPOINT_SECONDS,
    _clean_points,
    build_live_features,
    canonical_close_time,
    label_future_path,
)


logger = logging.getLogger("q15.path_forecast")


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


class PathForecastRunner:
    def __init__(
        self,
        *,
        model_path: str | None = None,
        db_path: str | None = None,
        enabled: bool | None = None,
    ) -> None:
        self.enabled = _bool("Q15_PATH_FORECAST_ENABLED", False) if enabled is None else bool(enabled)
        self.model_path = model_path or os.environ.get(
            "Q15_PATH_FORECAST_MODEL", "work/path-forecast/model-v1.npz"
        )
        self.db_path = db_path or os.environ.get(
            "Q15_PATH_FORECAST_DB", "data/q15_path_forecast_v1.sqlite3"
        )
        self.path_db = os.environ.get(
            "Q15_PATH_FORECAST_PATH_DB", "data/q15_path_recorder_v1.sqlite3"
        )
        self.metadata_db = os.environ.get(
            "Q15_PATH_FORECAST_METADATA_DB", "data/q15_v95_ledger_v1.sqlite3"
        )
        self.max_checkpoint_lag_seconds = max(
            1.0, float(os.environ.get("Q15_PATH_FORECAST_MAX_CHECKPOINT_LAG_SECONDS", "12"))
        )
        self._model: PathForecastModel | None = None
        self._ledger: PathForecastLedger | None = None
        self._paths: dict[tuple[str, float], deque[dict[str, float | None]]] = {}
        self._done: set[tuple[str, float, int]] = set()
        self._queue: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=128)
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last_sample_at: dict[tuple[str, float], float] = {}
        self._last_prediction_at: float | None = None
        self._last_reconcile_at: float | None = None
        self._last_error: str | None = None
        self._recorded = 0
        self._duplicates = 0
        self._rejected = 0
        self._dropped = 0
        self._reconciled = 0
        if self.enabled:
            self._initialize()

    def _initialize(self) -> None:
        model = PathForecastModel.load(self.model_path)
        if not bool(model.audit_summary.get("forward_shadow_eligible")):
            raise ValueError("path forecast model is not forward-shadow eligible")
        self._model = model
        self._ledger = PathForecastLedger(self.db_path)

    def start(self) -> bool:
        if not self.enabled:
            return False
        if self._model is None or self._ledger is None:
            self._initialize()
        if self._worker is None or not self._worker.is_alive():
            self._stop.clear()
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="path-forecast-shadow",
                daemon=True,
            )
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
        target_px: float | None,
        index_px: float | None,
        spot_px: float | None,
        yes_bid: float | None,
        yes_ask: float | None,
        now: float | None = None,
    ) -> bool:
        if not self.enabled:
            return False
        close = _finite(close_time)
        remaining = _finite(seconds_remaining)
        target = _finite(target_px)
        px = _finite(index_px)
        if px is None:
            px = _finite(spot_px)
        if close is None or remaining is None or target is None or px is None or target <= 0.0 or px <= 0.0:
            return False
        asset_name = str(asset or "").upper()
        ts = time.time() if now is None else float(now)
        close = canonical_close_time(close)
        key = (asset_name, close)
        bid = _finite(yes_bid)
        ask = _finite(yes_ask)
        yes_mid = None if bid is None or ask is None else (bid + ask) / 2.0
        jobs: list[dict[str, Any]] = []
        with self._lock:
            prior = self._last_sample_at.get(key, 0.0)
            if ts - prior >= 0.5:
                self._last_sample_at[key] = ts
                self._paths.setdefault(key, deque(maxlen=1800)).append({
                    "ts": ts,
                    "px": px,
                    "yes_mid": yes_mid,
                })
            points = list(self._paths.get(key, ()))
            for checkpoint in CHECKPOINT_SECONDS:
                job_key = (asset_name, close, checkpoint)
                lag = float(checkpoint) - remaining
                if job_key in self._done or not 0.0 <= lag <= self.max_checkpoint_lag_seconds:
                    continue
                self._done.add(job_key)
                try:
                    vector, current_px, current_yes_mid, diagnostics = build_live_features(
                        asset=asset_name,
                        close_time=close,
                        checkpoint_seconds=checkpoint,
                        target_px=target,
                        points=points,
                        max_decision_age_seconds=self.max_checkpoint_lag_seconds,
                    )
                except ValueError as exc:
                    self._rejected += 1
                    self._last_error = str(exc)[:200]
                    continue
                jobs.append({
                    "created_at": ts,
                    "asset": asset_name,
                    "close_time": close,
                    "checkpoint_seconds": checkpoint,
                    "decision_time": close - checkpoint,
                    "captured_offset_seconds": diagnostics["decision_age_seconds"],
                    "target_px": target,
                    "current_px": current_px,
                    "current_yes_mid": current_yes_mid,
                    "feature_vector": vector,
                })
            expired = [path_key for path_key in self._paths if path_key[1] < ts - 60.0]
            for path_key in expired:
                self._paths.pop(path_key, None)
                self._last_sample_at.pop(path_key, None)
        self.start()
        queued = False
        for job in jobs:
            try:
                self._queue.put_nowait(job)
                queued = True
            except queue.Full:
                self._dropped += 1
                self._last_error = "path forecast queue full"
        return queued

    def _process(self, job: Mapping[str, Any]) -> None:
        model = self._model
        ledger = self._ledger
        if model is None or ledger is None:
            raise RuntimeError("path forecast runtime is not initialized")
        vector = np.asarray(job["feature_vector"], dtype=float)
        prediction = model.predict(vector)
        feature_json = [None if not math.isfinite(float(value)) else float(value) for value in vector]
        _, inserted = ledger.record(
            created_at=float(job["created_at"]),
            asset=str(job["asset"]),
            close_time=float(job["close_time"]),
            checkpoint_seconds=int(job["checkpoint_seconds"]),
            decision_time=float(job["decision_time"]),
            captured_offset_seconds=float(job["captured_offset_seconds"]),
            target_px=float(job["target_px"]),
            current_px=float(job["current_px"]),
            current_yes_mid=_finite(job.get("current_yes_mid")),
            prediction=prediction,
            feature_vector=feature_json,
        )
        if inserted:
            self._recorded += 1
        else:
            self._duplicates += 1
        self._last_prediction_at = time.time()
        self._last_error = None

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=1.0)
            except queue.Empty:
                self._safe_reconcile()
                continue
            try:
                self._process(job)
            except (ValueError, RuntimeError, sqlite3.Error, OSError) as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"[:300]
                logger.warning("path forecast job failed: %s", self._last_error)
            finally:
                self._queue.task_done()
            self._safe_reconcile()

    def _safe_reconcile(self) -> int:
        try:
            return self._reconcile_if_due()
        except (ValueError, sqlite3.Error, OSError, json.JSONDecodeError) as exc:
            self._last_error = f"reconcile {type(exc).__name__}: {exc}"[:300]
            logger.warning("path forecast reconciliation failed: %s", self._last_error)
            return 0

    def _official_result(self, conn: sqlite3.Connection, *, asset: str, close_time: float) -> str | None:
        row = conn.execute(
            "SELECT official_result FROM predictions WHERE asset=? AND close_time BETWEEN ? AND ? "
            "AND official_result IN ('YES','NO') ORDER BY resolved_at DESC LIMIT 1",
            (asset, close_time - 15.0, close_time + 15.0),
        ).fetchone()
        if row is None:
            return None
        official = str(row[0] or "").upper()
        return official if official in {"YES", "NO"} else None

    def _reconcile_if_due(self, *, now: float | None = None) -> int:
        current = time.time() if now is None else float(now)
        if self._last_reconcile_at is not None and current - self._last_reconcile_at < 30.0:
            return 0
        self._last_reconcile_at = current
        ledger = self._ledger
        if ledger is None:
            return 0
        pending = ledger.pending(before_close_time=current - 20.0, limit=100)
        if not pending:
            return 0
        path_file = Path(self.path_db)
        metadata_file = Path(self.metadata_db)
        if not path_file.exists() or not metadata_file.exists():
            return 0
        path_conn = sqlite3.connect(str(path_file), timeout=10.0)
        metadata_conn = sqlite3.connect(str(metadata_file), timeout=10.0)
        resolved = 0
        try:
            for row in pending:
                official = self._official_result(
                    metadata_conn,
                    asset=str(row["asset"]),
                    close_time=float(row["close_time"]),
                )
                if official is None:
                    continue
                path_row = path_conn.execute(
                    "SELECT path_json_gz FROM window_paths WHERE asset=? AND close_time BETWEEN ? AND ? "
                    "ORDER BY ABS(close_time-?) LIMIT 1",
                    (
                        row["asset"],
                        float(row["close_time"]) - 2.0,
                        float(row["close_time"]) + 2.0,
                        float(row["close_time"]),
                    ),
                ).fetchone()
                if path_row is None:
                    continue
                try:
                    points = _clean_points(path_row[0])
                except ValueError:
                    continue
                decision_time = float(row["decision_time"])
                observed = [point for point in points if float(point["ts"]) <= decision_time]
                future = [point for point in points if float(point["ts"]) > decision_time]
                if len(observed) < 8 or len(future) < 8:
                    continue
                try:
                    actual, crossed, _, _, _ = label_future_path(
                        observed_points=observed,
                        future_points=future,
                        decision_time=decision_time,
                        close_time=float(row["close_time"]),
                        target_px=float(row["target_px"]),
                    )
                    prediction = json.loads(str(row["prediction_json"] or "{}"))
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
                predicted_yes = float(prediction.get("settlement_yes_probability", 0.5)) >= 0.5
                correct = predicted_yes == (official == "YES")
                if ledger.resolve(
                    int(row["id"]),
                    official_result=official,
                    resolved_at=current,
                    actual_archetype=actual,
                    actual_strike_crossed=crossed,
                    settlement_correct=int(correct),
                ):
                    resolved += 1
        finally:
            path_conn.close()
            metadata_conn.close()
        self._reconciled += resolved
        return resolved

    def _drain_once_for_tests(self) -> bool:
        try:
            job = self._queue.get_nowait()
        except queue.Empty:
            return False
        try:
            self._process(job)
            return True
        finally:
            self._queue.task_done()

    def health(self) -> dict[str, Any]:
        now = time.time()
        ledger_status = self._ledger.status() if self._ledger is not None else {
            "db_path": self.db_path,
            "rows": 0,
            "resolved_rows": 0,
            "latest": None,
        }
        return {
            "enabled": self.enabled,
            "paper_only": True,
            "read_only": True,
            "notification_eligible": False,
            "trading_eligible": False,
            "model_version": MODEL_VERSION,
            "model_path": str(Path(self.model_path)),
            "audit_summary": None if self._model is None else dict(self._model.audit_summary),
            "thread_alive": bool(self._worker and self._worker.is_alive()),
            "active_windows": len(self._paths),
            "queue_size": self._queue.qsize(),
            "recorded_this_process": self._recorded,
            "reconciled_this_process": self._reconciled,
            "duplicates_this_process": self._duplicates,
            "rejected_checkpoints": self._rejected,
            "dropped_jobs": self._dropped,
            "last_prediction_age_seconds": (
                None if self._last_prediction_at is None else round(max(0.0, now - self._last_prediction_at), 3)
            ),
            "last_reconcile_age_seconds": (
                None if self._last_reconcile_at is None else round(max(0.0, now - self._last_reconcile_at), 3)
            ),
            "last_error": self._last_error,
            **ledger_status,
        }


_runner: PathForecastRunner | None = None
_runner_lock = threading.Lock()


def get_path_forecast_runner() -> PathForecastRunner:
    global _runner
    with _runner_lock:
        if _runner is None:
            try:
                _runner = PathForecastRunner()
            except (FileNotFoundError, ValueError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
                logger.warning("path forecast disabled after initialization failure: %s", exc)
                _runner = PathForecastRunner(enabled=False)
                _runner._last_error = f"{type(exc).__name__}: {exc}"[:300]
        return _runner


def path_forecast_health() -> dict[str, Any]:
    return get_path_forecast_runner().health()


def reset_path_forecast_runner_for_tests() -> None:
    global _runner
    with _runner_lock:
        if _runner is not None:
            _runner.stop()
            if _runner._ledger is not None:
                _runner._ledger.close()
        _runner = None
