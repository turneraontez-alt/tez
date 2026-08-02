"""Background outcome-blind monitor for frozen V13 review milestones."""
from __future__ import annotations

import importlib
import logging
import os
import threading
import time
from typing import Any, Callable, Mapping


LOGGER = logging.getLogger(__name__)
CAPTURE_PHASE_SECONDS = 120
CAPTURE_PROTECTED_BEFORE_SECONDS = 130
CAPTURE_PROTECTED_AFTER_SECONDS = 5


def capture_protection_delay_seconds(now: float) -> float:
    """Delay heavy readiness scans beyond an exact-capture protection window."""
    epoch = int(float(now))
    phase = (epoch % 900 + 900) % 900
    seconds_until = (CAPTURE_PHASE_SECONDS - phase + 900) % 900
    seconds_since = (phase - CAPTURE_PHASE_SECONDS + 900) % 900
    if seconds_until <= CAPTURE_PROTECTED_BEFORE_SECONDS:
        return float(seconds_until + CAPTURE_PROTECTED_AFTER_SECONDS + 1)
    if seconds_since <= CAPTURE_PROTECTED_AFTER_SECONDS:
        return float(CAPTURE_PROTECTED_AFTER_SECONDS - seconds_since + 1)
    return 0.0


def _enabled() -> bool:
    return os.environ.get(
        "Q15_V13_READINESS_MONITOR", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}


def _interval_seconds() -> float:
    try:
        value = float(os.environ.get("Q15_V13_READINESS_INTERVAL_SECONDS", "300"))
    except (TypeError, ValueError):
        value = 300.0
    return min(3600.0, max(60.0, value))


class V13ReadinessMonitor:
    # V13/V14 milestones are one-shot administrative notices.  Once every
    # idempotent delivery is complete there is no reason to keep rescanning the
    # wide strategy ledger.  The independent-path subclass overrides this
    # because it also watches ongoing source degradation.
    STOP_AFTER_ALL_MILESTONES = True
    NOTICE_MODULE = "tools.q15_rti_v13_readiness_notice"
    LOG_LABEL = "V13"
    THREAD_NAME = "q15-v13-paper-readiness"

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
        self._completed_milestones: set[str] = set()
        self._last_checked_at: float | None = None
        self._last_snapshot: dict[str, Any] | None = None
        self._last_result: dict[str, Any] | None = None
        self._last_error: str | None = None
        self._capture_deferrals = 0
        self._last_capture_deferred_at: float | None = None
        self._last_capture_delay_seconds: float | None = None

    def _notice_module(self) -> Any:
        return importlib.import_module(self.NOTICE_MODULE)

    def _default_snapshot_builder(self) -> Mapping[str, Any]:
        return self._notice_module().build_outcome_blind_snapshot()

    def _default_sender_factory(self) -> Callable[..., Mapping[str, Any]]:
        return self._notice_module()._default_sender()

    def check_once(self) -> dict[str, Any]:
        notice = self._notice_module()
        MILESTONES = notice.MILESTONES
        ready_milestones = notice.ready_milestones
        send_ready_milestones = notice.send_ready_milestones

        builder = self._snapshot_builder or self._default_snapshot_builder
        snapshot = dict(builder())
        ready = ready_milestones(snapshot)
        if ready:
            if self._sender is None:
                factory = self._sender_factory or self._default_sender_factory
                self._sender = factory()
            result = send_ready_milestones(snapshot, self._sender)
        else:
            result = {
                "status": "WAITING_FOR_MILESTONE",
                "notice_attempted": False,
                "ready_milestones": [],
                "deliveries": {},
            }
        completed = set()
        for milestone, raw in dict(result.get("deliveries") or {}).items():
            delivery = dict(raw or {})
            if delivery.get("delivered") or delivery.get("muted"):
                completed.add(str(milestone))
        with self._lock:
            self._checks += 1
            self._last_checked_at = time.time()
            self._last_snapshot = snapshot
            self._last_result = dict(result)
            self._last_error = None
            self._completed_milestones.update(completed)
            all_completed = self._completed_milestones == set(MILESTONES)
        return {**dict(result), "all_milestones_completed": all_completed}

    def _run(self) -> None:
        while not self._stop.is_set():
            current = time.time()
            capture_delay = capture_protection_delay_seconds(current)
            if capture_delay > 0.0:
                with self._lock:
                    self._capture_deferrals += 1
                    self._last_capture_deferred_at = current
                    self._last_capture_delay_seconds = capture_delay
                if self._stop.wait(capture_delay):
                    break
                continue
            try:
                result = self.check_once()
                if (
                    self.STOP_AFTER_ALL_MILESTONES
                    and result.get("all_milestones_completed") is True
                ):
                    return
            except Exception as exc:
                with self._lock:
                    self._checks += 1
                    self._last_checked_at = time.time()
                    self._last_error = f"{type(exc).__name__}: {exc}"
                LOGGER.warning(
                    "%s PAPER readiness check failed: %s", self.LOG_LABEL, exc
                )
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
                name=self.THREAD_NAME,
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
        MILESTONES = self._notice_module().MILESTONES

        with self._lock:
            snapshot = dict(self._last_snapshot or {})
            result = dict(self._last_result or {})
            soft_integrity = dict(
                snapshot.get("soft_input_integrity") or {}
            )
            completed = sorted(self._completed_milestones)
            pending = [name for name in MILESTONES if name not in completed]
            return {
                "enabled": self.enabled,
                "paper_only": True,
                "administrative_notices_only": True,
                "notification_is_trade_signal": False,
                "outcome_labels_read": False,
                "automatic_scoring": False,
                "automatic_promotion": False,
                "real_trading_allowed": False,
                "thread_alive": bool(self._thread and self._thread.is_alive()),
                "all_milestones_completed": not pending,
                "completed_milestones": completed,
                "pending_milestones": pending,
                "checks": self._checks,
                "interval_seconds": self.interval_seconds,
                "last_checked_at": self._last_checked_at,
                "last_error": self._last_error,
                "capture_protection_enabled": True,
                "capture_protected_before_seconds": (
                    CAPTURE_PROTECTED_BEFORE_SECONDS
                ),
                "capture_protected_after_seconds": (
                    CAPTURE_PROTECTED_AFTER_SECONDS
                ),
                "capture_deferrals": self._capture_deferrals,
                "last_capture_deferred_at": self._last_capture_deferred_at,
                "last_capture_delay_seconds": self._last_capture_delay_seconds,
                "complete_executable_close_windows": snapshot.get(
                    "complete_executable_close_windows"
                ),
                "soft_input_integrity_status": soft_integrity.get("status"),
                "fully_observed_rows": soft_integrity.get(
                    "fully_observed_rows"
                ),
                "soft_degraded_rows": soft_integrity.get(
                    "soft_degraded_rows"
                ),
                "fully_observed_close_windows": soft_integrity.get(
                    "fully_observed_close_windows"
                ),
                "soft_degraded_close_windows": soft_integrity.get(
                    "soft_degraded_close_windows"
                ),
                "soft_degradation_by_asset": soft_integrity.get(
                    "degraded_by_asset", {}
                ),
                "soft_degradation_by_reason": soft_integrity.get(
                    "degraded_by_reason", {}
                ),
                "soft_degradation_changes_readiness_credit": (
                    soft_integrity.get("changes_readiness_credit")
                ),
                "ready_milestones": result.get("ready_milestones", []),
                "last_status": result.get("status"),
            }


_monitor: V13ReadinessMonitor | None = None
_monitor_lock = threading.Lock()


def get_v13_readiness_monitor() -> V13ReadinessMonitor:
    global _monitor
    with _monitor_lock:
        if _monitor is None:
            _monitor = V13ReadinessMonitor()
        return _monitor


def start_v13_readiness_monitor() -> bool:
    return get_v13_readiness_monitor().start()


def v13_readiness_monitor_health() -> dict[str, Any]:
    monitor = _monitor
    if monitor is None:
        return {
            "enabled": _enabled(),
            "paper_only": True,
            "administrative_notices_only": True,
            "notification_is_trade_signal": False,
            "outcome_labels_read": False,
            "automatic_scoring": False,
            "automatic_promotion": False,
            "real_trading_allowed": False,
            "thread_alive": False,
            "all_milestones_completed": False,
            "completed_milestones": [],
            "pending_milestones": ["GEOMETRY_30", "NON_BTC_60", "BTC_150"],
            "capture_protection_enabled": True,
            "capture_protected_before_seconds": (
                CAPTURE_PROTECTED_BEFORE_SECONDS
            ),
            "capture_protected_after_seconds": CAPTURE_PROTECTED_AFTER_SECONDS,
            "capture_deferrals": 0,
            "checks": 0,
        }
    return monitor.health()


def reset_v13_readiness_monitor() -> None:
    """Test hook."""
    global _monitor
    with _monitor_lock:
        if _monitor is not None:
            _monitor.stop()
        _monitor = None
