"""Background outcome-blind monitor for frozen V14 review milestones."""
from __future__ import annotations

import os
import threading
from typing import Any

from .v13_readiness_monitor import (
    CAPTURE_PROTECTED_AFTER_SECONDS,
    CAPTURE_PROTECTED_BEFORE_SECONDS,
    V13ReadinessMonitor,
)


def _enabled() -> bool:
    return os.environ.get(
        "Q15_V14_READINESS_MONITOR", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}


def _interval_seconds() -> float:
    try:
        value = float(os.environ.get(
            "Q15_V14_READINESS_INTERVAL_SECONDS", "300"
        ))
    except (TypeError, ValueError):
        value = 300.0
    return min(3600.0, max(60.0, value))


class V14ReadinessMonitor(V13ReadinessMonitor):
    NOTICE_MODULE = "tools.q15_rti_v14_readiness_notice"
    LOG_LABEL = "V14"
    THREAD_NAME = "q15-v14-paper-readiness"

    def __init__(self, **kwargs: Any) -> None:
        if "enabled" not in kwargs:
            kwargs["enabled"] = _enabled()
        if "interval_seconds" not in kwargs:
            kwargs["interval_seconds"] = _interval_seconds()
        super().__init__(**kwargs)


_monitor: V14ReadinessMonitor | None = None
_monitor_lock = threading.Lock()


def get_v14_readiness_monitor() -> V14ReadinessMonitor:
    global _monitor
    with _monitor_lock:
        if _monitor is None:
            _monitor = V14ReadinessMonitor()
        return _monitor


def start_v14_readiness_monitor() -> bool:
    return get_v14_readiness_monitor().start()


def v14_readiness_monitor_health() -> dict[str, Any]:
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


def reset_v14_readiness_monitor() -> None:
    global _monitor
    with _monitor_lock:
        if _monitor is not None:
            _monitor.stop()
        _monitor = None
