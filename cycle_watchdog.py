"""Lightweight per-cycle stage timer for the refresh loop.

Times each named stage of a refresh cycle, remembers the slowest stage and the
worst-ever duration per stage, and flags any cycle that runs longer than
``Q15_CYCLE_WATCHDOG_SECONDS``. When the ~1s loop stalls, ``/api/health``
("cycle_watchdog") then names the offending stage — and any time spent outside
the timed stages (discovery/fetch) shows up as ``unaccounted_seconds`` — instead
of leaving us to guess.

Pure-Python, no third-party deps. Read-only/diagnostic: it never changes a
decision, it only measures.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def _blank_state() -> dict:
    return {
        "last_cycle_seconds": None,
        "last_stage_seconds": {},
        "unaccounted_seconds": None,
        "slowest_stage": None,
        "slowest_stage_seconds": None,
        "worst_stage_seconds": {},
        "slow_cycle_count": 0,
        "last_slow_cycle_iso": None,
        "last_slow_breakdown": None,
    }


_state = _blank_state()


def threshold_seconds() -> float:
    try:
        return max(0.5, float(os.environ.get("Q15_CYCLE_WATCHDOG_SECONDS", "10") or 10))
    except (TypeError, ValueError):
        return 10.0


class CycleTimer:
    """Accumulates per-stage durations for one cycle, then ``commit``s them."""

    def __init__(self):
        self.stages: dict[str, float] = {}

    def time(self, stage: str, fn, *args, **kwargs):
        """Run ``fn`` timed under ``stage`` and return its result (re-raises)."""
        t0 = time.monotonic()
        try:
            return fn(*args, **kwargs)
        finally:
            self.stages[stage] = self.stages.get(stage, 0.0) + (time.monotonic() - t0)

    def safe(self, stage: str, fn, *args) -> None:
        """Like :meth:`time` but swallows + logs exceptions (mirrors app._safe)."""
        t0 = time.monotonic()
        try:
            fn(*args)
        except Exception:
            logger.exception("%s failed", stage)
        finally:
            self.stages[stage] = self.stages.get(stage, 0.0) + (time.monotonic() - t0)

    def commit(self, total_seconds: float) -> bool:
        """Publish this cycle's timings. Returns True if it was a slow cycle."""
        slowest, slowest_s = None, 0.0
        for name, secs in self.stages.items():
            if secs > slowest_s:
                slowest, slowest_s = name, secs
        staged = sum(self.stages.values())
        unaccounted = max(0.0, total_seconds - staged)
        is_slow = total_seconds >= threshold_seconds()
        with _lock:
            _state["last_cycle_seconds"] = round(total_seconds, 3)
            _state["last_stage_seconds"] = {k: round(v, 3) for k, v in self.stages.items()}
            _state["unaccounted_seconds"] = round(unaccounted, 3)
            _state["slowest_stage"] = slowest
            _state["slowest_stage_seconds"] = round(slowest_s, 3)
            for name, secs in self.stages.items():
                if round(secs, 3) > _state["worst_stage_seconds"].get(name, 0.0):
                    _state["worst_stage_seconds"][name] = round(secs, 3)
            if is_slow:
                _state["slow_cycle_count"] += 1
                _state["last_slow_cycle_iso"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                _state["last_slow_breakdown"] = dict(
                    _state["last_stage_seconds"], unaccounted=round(unaccounted, 3)
                )
        if is_slow:
            logger.warning(
                "Slow cycle: %.2fs (slowest stage %s %.2fs, unaccounted %.2fs)",
                total_seconds, slowest, slowest_s, unaccounted,
            )
        return is_slow


def health() -> dict:
    with _lock:
        snap = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _state.items()}
    snap["threshold_seconds"] = threshold_seconds()
    return snap


def reset() -> None:
    """Reset accumulated state (used by tests)."""
    with _lock:
        _state.clear()
        _state.update(_blank_state())
