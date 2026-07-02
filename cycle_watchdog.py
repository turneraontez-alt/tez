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

import json
import atexit
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import parse, request
from urllib.error import URLError

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        return min(high, max(low, float(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


_lock = threading.Lock()
_heartbeat_lock = threading.Lock()
_supervisor_lock = threading.Lock()
_supervisor_started = False
_heartbeat_cooldown_memory: dict[str, float] = {}


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


def _blank_feed_state() -> dict:
    return {
        "feeds": {},
        "last_alert_at": 0.0,
    }


_feed_state = _blank_feed_state()


def threshold_seconds() -> float:
    return _env_float("Q15_CYCLE_WATCHDOG_SECONDS", 10.0, 0.5, 3600.0)


def feed_stale_seconds() -> float:
    return _env_float("Q15_FEED_WATCHDOG_STALE_SECONDS", 300.0, 1.0, 86400.0)


def feed_grace_seconds() -> float:
    return _env_float("Q15_FEED_WATCHDOG_GRACE_SECONDS", 600.0, 0.0, 86400.0)


def feed_alert_cooldown_seconds() -> float:
    return _env_float("Q15_FEED_WATCHDOG_COOLDOWN_SECONDS", 86400.0, 30.0, 86400.0 * 7)


def heartbeat_enabled() -> bool:
    return _env_bool("Q15_HEARTBEAT_ENABLED", True)


def heartbeat_path() -> Path:
    return Path(os.environ.get("Q15_HEARTBEAT_PATH", "work/local-run/q15_cycle_heartbeat.json"))


def heartbeat_cooldown_path() -> Path:
    return Path(
        os.environ.get(
            "Q15_HEARTBEAT_COOLDOWN_PATH",
            "work/local-run/q15_cycle_heartbeat_cooldown.json",
        )
    )


def heartbeat_stale_seconds() -> float:
    return _env_float("Q15_HEARTBEAT_STALE_SECONDS", 120.0, 5.0, 86400.0)


def heartbeat_supervisor_interval_seconds() -> float:
    return _env_float("Q15_HEARTBEAT_SUPERVISOR_INTERVAL_SECONDS", 15.0, 1.0, 3600.0)


def heartbeat_pager_cooldown_seconds() -> float:
    return _env_float("Q15_HEARTBEAT_PAGER_COOLDOWN_SECONDS", 600.0, 30.0, 86400.0)


def _iso_from_epoch(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def _clean_age(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out) or out < 0:
        return None
    return out


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def write_heartbeat(*, now: float | None = None, cycle: int | None = None,
                    status: str = "cycle_start") -> None:
    """Durably stamp the refresh loop heartbeat. Best-effort and non-fatal."""
    if not heartbeat_enabled():
        return
    current = time.time() if now is None else float(now)
    payload = {
        "pid": os.getpid(),
        "timestamp": current,
        "timestamp_iso": _iso_from_epoch(current),
        "cycle": cycle,
        "status": status,
    }
    try:
        with _heartbeat_lock:
            _write_json_atomic(heartbeat_path(), payload)
    except Exception:
        logger.debug("heartbeat write failed", exc_info=True)


def heartbeat_status(*, now: float | None = None) -> dict:
    current = time.time() if now is None else float(now)
    path = heartbeat_path()
    payload = _read_json(path)
    ts = _clean_age(payload.get("timestamp"))
    age = None if ts is None else max(0.0, current - ts)
    stale_threshold = heartbeat_stale_seconds()
    exists = path.exists()
    return {
        "enabled": heartbeat_enabled(),
        "path": str(path),
        "exists": exists,
        "pid": payload.get("pid"),
        "cycle": payload.get("cycle"),
        "status": payload.get("status"),
        "last_heartbeat_iso": _iso_from_epoch(ts),
        "age_seconds": None if age is None else round(age, 3),
        "stale_seconds": stale_threshold,
        "stale": bool(exists and age is not None and age > stale_threshold),
        "supervisor_interval_seconds": heartbeat_supervisor_interval_seconds(),
        "cooldown_seconds": heartbeat_pager_cooldown_seconds(),
        "cooldown_path": str(heartbeat_cooldown_path()),
    }


def _claim_persistent_cooldown(kind: str, now: float, cooldown: float) -> bool:
    path = heartbeat_cooldown_path()
    with _heartbeat_lock:
        data = _read_json(path)
        key = f"{kind}_alert_at"
        last = _clean_age(data.get(key)) or _heartbeat_cooldown_memory.get(key, 0.0) or 0.0
        if last and now - last < cooldown:
            return False
        data[key] = float(now)
        data[f"{kind}_alert_at_iso"] = _iso_from_epoch(float(now))
        try:
            _write_json_atomic(path, data)
        except Exception:
            _heartbeat_cooldown_memory[key] = float(now)
            logger.debug("heartbeat cooldown write failed", exc_info=True)
        else:
            _heartbeat_cooldown_memory[key] = float(now)
        return True


def _telegram_target() -> tuple[str | None, str | None]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or None
    chat = (
        os.environ.get("Q15_HEARTBEAT_TELEGRAM_CHAT_ID")
        or os.environ.get("TELEGRAM_CHAT_ID")
        or None
    )
    return token, chat


def send_dependency_free_telegram(text: str) -> dict:
    """Send a Telegram page with only stdlib urllib. Never logs secret values."""
    token, chat = _telegram_target()
    if not token or not chat:
        return {"ok": False, "delivered": False, "error": "telegram_unconfigured"}
    body = parse.urlencode({
        "chat_id": chat,
        "text": str(text),
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=8) as resp:  # noqa: S310 - Telegram API target from env token.
            ok = 200 <= int(getattr(resp, "status", 0) or 0) < 300
            return {"ok": ok, "delivered": ok, "error": None if ok else f"HTTP {resp.status}"}
    except URLError as exc:
        return {"ok": False, "delivered": False, "error": type(exc).__name__}
    except Exception as exc:
        return {"ok": False, "delivered": False, "error": type(exc).__name__}


def heartbeat_alert_message(status: dict | None = None, *, now: float | None = None) -> str | None:
    current_status = heartbeat_status(now=now) if status is None else status
    if not current_status.get("enabled") or not current_status.get("stale"):
        return None
    age = current_status.get("age_seconds")
    threshold = current_status.get("stale_seconds")
    return (
        "<b>Q15 HEARTBEAT WATCHDOG</b>\n"
        f"Refresh-loop heartbeat age is {age:.0f}s (threshold {threshold:.0f}s).\n"
        "The loop may be hung; no restart attempted."
    )


def heartbeat_supervisor_tick(*, now: float | None = None, send_fn=None) -> str | None:
    """One testable supervisor pass. Sends at most one stale-heartbeat page."""
    current = time.time() if now is None else float(now)
    status = heartbeat_status(now=current)
    msg = heartbeat_alert_message(status, now=current)
    if not msg:
        return None
    if not _claim_persistent_cooldown("stale", current, heartbeat_pager_cooldown_seconds()):
        return None
    sender = send_fn or send_dependency_free_telegram
    try:
        sender(msg)
    except Exception:
        logger.debug("heartbeat stale page send failed", exc_info=True)
    return msg


def process_exit_alert_message(*, now: float | None = None) -> str | None:
    if not heartbeat_enabled() or not _env_bool("Q15_HEARTBEAT_EXIT_ALERT_ENABLED", True):
        return None
    current = time.time() if now is None else float(now)
    return (
        "<b>Q15 PROCESS EXIT</b>\n"
        f"Monitor process is exiting at {_iso_from_epoch(current)}.\n"
        "No restart attempted."
    )


def send_process_exit_alert(*, now: float | None = None, send_fn=None) -> str | None:
    current = time.time() if now is None else float(now)
    msg = process_exit_alert_message(now=current)
    if not msg:
        return None
    if not _claim_persistent_cooldown("exit", current, heartbeat_pager_cooldown_seconds()):
        return None
    sender = send_fn or send_dependency_free_telegram
    try:
        sender(msg)
    except Exception:
        logger.debug("heartbeat exit page send failed", exc_info=True)
    return msg


def start_heartbeat_supervisor() -> bool:
    """Start the background heartbeat checker once. It pages only; no restart."""
    global _supervisor_started
    if not heartbeat_enabled():
        return False
    with _supervisor_lock:
        if _supervisor_started:
            return False
        _supervisor_started = True

        def _run() -> None:
            while True:
                time.sleep(heartbeat_supervisor_interval_seconds())
                heartbeat_supervisor_tick()

        thread = threading.Thread(target=_run, name="q15-heartbeat-supervisor", daemon=True)
        thread.start()
        atexit.register(send_process_exit_alert)
        return True


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


def observe_feed_ages(feed_ages: dict[str, float | None], *, now: float | None = None) -> dict:
    """Record latest feed ages and stale episodes for operator-facing paging.

    A feed becomes stale as soon as its snapshot age exceeds
    ``Q15_FEED_WATCHDOG_STALE_SECONDS``. Paging is delayed until that stale state
    has held for ``Q15_FEED_WATCHDOG_GRACE_SECONDS`` so short collector hiccups
    do not alert.
    """
    current = time.time() if now is None else float(now)
    threshold = feed_stale_seconds()
    grace = feed_grace_seconds()
    with _pager_lock:
        feeds = _feed_state["feeds"]
        for name, raw_age in feed_ages.items():
            feed_name = str(name)
            age = _clean_age(raw_age)
            prev = dict(feeds.get(feed_name) or {})
            stale = bool(age is not None and age > threshold)
            if stale:
                stale_since = float(prev.get("stale_since_epoch") or current)
                alerted_at = prev.get("alerted_at_epoch")
            else:
                stale_since = None
                alerted_at = None
            stale_for = (current - stale_since) if stale_since is not None else 0.0
            feeds[feed_name] = {
                "age_seconds": round(age, 3) if age is not None else None,
                "stale": stale,
                "stale_for_seconds": round(max(0.0, stale_for), 3),
                "stale_since_epoch": stale_since,
                "stale_since_iso": _iso_from_epoch(stale_since),
                "alert_ready": bool(stale and stale_for >= grace),
                "alerted_at_epoch": alerted_at,
                "alerted_at_iso": _iso_from_epoch(float(alerted_at)) if alerted_at else None,
            }
    return feed_health()


def feed_health() -> dict:
    with _pager_lock:
        feeds = {
            name: {
                key: value
                for key, value in info.items()
                if key not in {"stale_since_epoch", "alerted_at_epoch"}
            }
            for name, info in _feed_state["feeds"].items()
        }
        last_alert_at = float(_feed_state.get("last_alert_at") or 0.0)
    return {
        "enabled": _env_bool("Q15_FEED_WATCHDOG_ENABLED", True),
        "stale_seconds": feed_stale_seconds(),
        "grace_seconds": feed_grace_seconds(),
        "cooldown_seconds": feed_alert_cooldown_seconds(),
        "last_alert_at_iso": _iso_from_epoch(last_alert_at) if last_alert_at else None,
        "feeds": feeds,
    }


def degraded_feeds() -> list[str]:
    with _pager_lock:
        return sorted(name for name, info in _feed_state["feeds"].items() if info.get("stale"))


def feed_alert_message(now: float | None = None) -> str | None:
    """Return one rate-limited Telegram page for stale feeds, or None."""
    if not _env_bool("Q15_FEED_WATCHDOG_ENABLED", True):
        return None
    current = time.time() if now is None else float(now)
    cooldown = feed_alert_cooldown_seconds()
    with _pager_lock:
        last_alert_at = float(_feed_state.get("last_alert_at") or 0.0)
        if last_alert_at and current - last_alert_at < cooldown:
            return None
        ready: list[tuple[str, dict]] = []
        for name, info in _feed_state["feeds"].items():
            if not info.get("stale") or not info.get("alert_ready") or info.get("alerted_at_epoch"):
                continue
            ready.append((name, dict(info)))
        if not ready:
            return None
        _feed_state["last_alert_at"] = current
        for name, _info in ready:
            _feed_state["feeds"][name]["alerted_at_epoch"] = current
            _feed_state["feeds"][name]["alerted_at_iso"] = _iso_from_epoch(current)
    lines = ["<b>Q15 FEED WATCHDOG</b>", "Collector snapshot freshness is stale; no restart attempted."]
    for name, info in ready:
        age = info.get("age_seconds")
        stale_for = info.get("stale_for_seconds")
        lines.append(f"{name}: age {age:.0f}s, stale for {stale_for:.0f}s.")
    lines.append("Check /api/health -> feed_watchdog and collector logs.")
    return "\n".join(lines)


# --- Pager: turn a freeze into a Telegram alert instead of silence -----------
_pager_lock = threading.Lock()
_pager_state = {"last_alert_at": 0.0}


def alert_message(now: float, uptime_seconds: float) -> str | None:
    """Return a Telegram alert string if the latest cycle is a real stall, else None.

    Stateful and cooldown-limited, so a sustained freeze pages once per window
    rather than every cycle. Returns None unless the last cycle exceeded
    ``Q15_WATCHDOG_ALERT_SECONDS`` (default 20s — higher than the watchdog's own
    log threshold, so only genuine freezes page), the process is past warmup, and
    the cooldown has elapsed. Never raises.
    """
    if not _env_bool("Q15_WATCHDOG_ALERT_ENABLED", True):
        return None
    alert_threshold = _env_float("Q15_WATCHDOG_ALERT_SECONDS", 20.0, 1.0, 600.0)
    warmup = _env_float("Q15_WATCHDOG_ALERT_WARMUP_SECONDS", 60.0, 0.0, 3600.0)
    cooldown = _env_float("Q15_WATCHDOG_ALERT_COOLDOWN_SECONDS", 600.0, 30.0, 86400.0)
    with _lock:
        cycle_s = _state.get("last_cycle_seconds")
        slowest = _state.get("slowest_stage")
        slowest_s = _state.get("slowest_stage_seconds") or 0.0
    if cycle_s is None or cycle_s < alert_threshold:
        return None
    if uptime_seconds < warmup:
        return None  # ignore expected cold-start heaviness right after a restart
    with _pager_lock:
        if now - _pager_state["last_alert_at"] < cooldown:
            return None
        _pager_state["last_alert_at"] = now
    return (
        "⚠️ <b>Monitor cycle stall</b>\n"
        f"Last refresh cycle took <b>{cycle_s:.0f}s</b> "
        f"(slowest stage: {slowest} {slowest_s:.0f}s).\n"
        "Predictions pause while the loop is blocked — "
        "check /api/health → cycle_watchdog."
    )


def reset() -> None:
    """Reset accumulated state (used by tests)."""
    with _lock:
        _state.clear()
        _state.update(_blank_state())
    with _pager_lock:
        _pager_state["last_alert_at"] = 0.0
        _feed_state.clear()
        _feed_state.update(_blank_feed_state())
    with _heartbeat_lock:
        _heartbeat_cooldown_memory.clear()
