"""Outcome-blind background monitor for the frozen V11 audit gate.

The monitor is administrative and PAPER-only.  It checks the feature-only
readiness projection, sends one idempotent Telegram notice at 60 clean
non-BTC windows, and then stops.  It never loads outcomes, fits or scores a
model, creates an artifact, promotes a rule, or places an order.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Mapping


LOGGER = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.environ.get(
        "Q15_V11_READINESS_MONITOR", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}


def _interval_seconds() -> float:
    try:
        value = float(os.environ.get("Q15_V11_READINESS_INTERVAL_SECONDS", "300"))
    except (TypeError, ValueError):
        value = 300.0
    return min(3600.0, max(60.0, value))


class V11ReadinessMonitor:
    def __init__(
        self,
        *,
        enabled: bool | None = None,
        interval_seconds: float | None = None,
        snapshot_builder: Callable[[], Mapping[str, Any]] | None = None,
        sender_factory: Callable[[], Callable[..., Mapping[str, Any]]] | None = None,
    ) -> None:
        self.enabled = _enabled() if enabled is None else bool(enabled)
        self.interval_seconds = (
            _interval_seconds()
            if interval_seconds is None
            else min(3600.0, max(60.0, float(interval_seconds)))
        )
        self._snapshot_builder = snapshot_builder
        self._sender_factory = sender_factory
        self._sender: Callable[..., Mapping[str, Any]] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._checks = 0
        self._completed = False
        self._last_checked_at: float | None = None
        self._last_snapshot: dict[str, Any] | None = None
        self._last_result: dict[str, Any] | None = None
        self._last_error: str | None = None

    @staticmethod
    def _default_snapshot_builder() -> Mapping[str, Any]:
        from tools.q15_rti_v11_readiness_notice import (
            build_outcome_blind_snapshot,
        )

        return build_outcome_blind_snapshot()

    @staticmethod
    def _default_sender_factory() -> Callable[..., Mapping[str, Any]]:
        from tools.q15_rti_v11_readiness_notice import _default_sender

        return _default_sender()

    def check_once(self) -> dict[str, Any]:
        from tools.q15_rti_v11_readiness_notice import (
            send_notice_if_ready,
            snapshot_is_notice_ready,
        )

        builder = self._snapshot_builder or self._default_snapshot_builder
        snapshot = dict(builder())
        if snapshot_is_notice_ready(snapshot):
            if self._sender is None:
                factory = self._sender_factory or self._default_sender_factory
                self._sender = factory()
            result = send_notice_if_ready(snapshot, self._sender)
        else:
            result = {
                "status": "WAITING_FOR_COMPLETE_WINDOWS",
                "notice_attempted": False,
            }
        delivery = dict(result.get("delivery") or {})
        completed = bool(delivery.get("delivered") or delivery.get("muted"))
        with self._lock:
            self._checks += 1
            self._last_checked_at = time.time()
            self._last_snapshot = snapshot
            self._last_result = dict(result)
            self._last_error = None
            self._completed = self._completed or completed
        return dict(result)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception as exc:
                with self._lock:
                    self._checks += 1
                    self._last_checked_at = time.time()
                    self._last_error = f"{type(exc).__name__}: {exc}"
                LOGGER.warning("V11 PAPER readiness check failed: %s", exc)
            with self._lock:
                completed = self._completed
            if completed:
                return
            self._stop.wait(self.interval_seconds)

    def start(self) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="q15-v11-paper-readiness",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def health(self) -> dict[str, Any]:
        with self._lock:
            snapshot = dict(self._last_snapshot or {})
            result = dict(self._last_result or {})
            thread_alive = bool(self._thread and self._thread.is_alive())
            return {
                "enabled": self.enabled,
                "paper_only": True,
                "outcome_labels_read": False,
                "automatic_scoring": False,
                "automatic_promotion": False,
                "real_trading_allowed": False,
                "thread_alive": thread_alive,
                "completed": self._completed,
                "checks": self._checks,
                "interval_seconds": self.interval_seconds,
                "last_checked_at": self._last_checked_at,
                "last_error": self._last_error,
                "complete_executable_close_windows": snapshot.get(
                    "complete_executable_close_windows"
                ),
                "windows_remaining": snapshot.get("windows_remaining"),
                "last_status": result.get("status"),
            }


_monitor: V11ReadinessMonitor | None = None
_monitor_lock = threading.Lock()


def get_v11_readiness_monitor() -> V11ReadinessMonitor:
    global _monitor
    with _monitor_lock:
        if _monitor is None:
            _monitor = V11ReadinessMonitor()
        return _monitor


def start_v11_readiness_monitor() -> bool:
    return get_v11_readiness_monitor().start()


def v11_readiness_monitor_health() -> dict[str, Any]:
    monitor = _monitor
    if monitor is None:
        return {
            "enabled": _enabled(),
            "paper_only": True,
            "outcome_labels_read": False,
            "automatic_scoring": False,
            "automatic_promotion": False,
            "real_trading_allowed": False,
            "thread_alive": False,
            "completed": False,
            "checks": 0,
        }
    return monitor.health()


def reset_v11_readiness_monitor() -> None:
    """Test hook."""
    global _monitor
    with _monitor_lock:
        if _monitor is not None:
            _monitor.stop()
        _monitor = None
