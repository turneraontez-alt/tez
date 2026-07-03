"""Runtime flag manifest diagnostics.

This module is intentionally read-only: it compares the live process
environment against a checked-in manifest and reports drift. It never changes
configuration and never gates startup.
"""
from __future__ import annotations

import html
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Any, Callable

import cycle_watchdog

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_MANIFEST_PATH = REPO_ROOT / "tools" / "expected_runtime_flags.json"

_last_status: dict[str, Any] = {
    "available": False,
    "manifest_present": False,
    "ok": True,
    "mismatches": [],
}


def _iso_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _normalise(value: Any) -> str:
    return str(value).strip()


def _load_manifest(path: Path) -> dict[str, str] | None:
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("runtime flag manifest must be an object")
    return {str(name): _normalise(expected) for name, expected in raw.items()}


def evaluate(
    *,
    manifest_path: str | os.PathLike[str] = DEFAULT_MANIFEST_PATH,
    env: Mapping[str, str] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Compare expected runtime flags to ``env`` and return health payload."""
    current = time.time() if now is None else float(now)
    path = Path(manifest_path)
    try:
        manifest = _load_manifest(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return {
            "available": False,
            "manifest_present": path.is_file(),
            "manifest_path": str(path),
            "ok": True,
            "checked_at": _iso_from_epoch(current),
            "expected_count": 0,
            "mismatch_count": 0,
            "mismatches": [],
            "error": f"{type(exc).__name__}: {exc}",
        }
    if manifest is None:
        return {
            "available": False,
            "manifest_present": False,
            "manifest_path": str(path),
            "ok": True,
            "checked_at": _iso_from_epoch(current),
            "expected_count": 0,
            "mismatch_count": 0,
            "mismatches": [],
            "error": None,
        }

    source = os.environ if env is None else env
    mismatches: list[dict[str, str]] = []
    for name, expected in sorted(manifest.items()):
        actual = source.get(name)
        expected_is_true = expected.lower() == "true"
        if actual is None:
            mismatches.append({"name": name, "expected": expected, "status": "missing"})
            continue
        actual_norm = _normalise(actual)
        if expected_is_true:
            if not _truthy(actual_norm):
                mismatches.append({"name": name, "expected": expected, "status": "off"})
        elif actual_norm != expected:
            mismatches.append({"name": name, "expected": expected, "status": "wrong"})

    return {
        "available": True,
        "manifest_present": True,
        "manifest_path": str(path),
        "ok": not mismatches,
        "checked_at": _iso_from_epoch(current),
        "expected_count": len(manifest),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "error": None,
    }


def alert_message(status: Mapping[str, Any]) -> str | None:
    """Build a marker-safe HTML Telegram ops alert for manifest drift."""
    mismatches = list(status.get("mismatches") or [])
    if not status.get("available") or not mismatches:
        return None
    lines = [
        "<b>Q15 OPS CONFIG MANIFEST</b>",
        "Expected runtime flags are missing/off/wrong; collectors may be silent.",
    ]
    for item in mismatches[:20]:
        name = html.escape(str(item.get("name") or "unknown"))
        expected = html.escape(str(item.get("expected") or ""))
        reason = html.escape(str(item.get("status") or "mismatch"))
        lines.append(f"{name}: {reason}; expected {expected}.")
    if len(mismatches) > 20:
        lines.append(f"...and {len(mismatches) - 20} more.")
    lines.append("Check /api/health -> startup_config_manifest.")
    return "\n".join(lines)


def maybe_send_alert(
    status: Mapping[str, Any],
    *,
    now: float | None = None,
    send_fn: Callable[[str], Any] | None = None,
) -> str | None:
    """Send one persisted-cooldown ops page for manifest drift."""
    msg = alert_message(status)
    if not msg:
        return None
    current = time.time() if now is None else float(now)
    cooldown = cycle_watchdog.heartbeat_pager_cooldown_seconds()
    if not cycle_watchdog.claim_persistent_cooldown("runtime_config_manifest", current, cooldown):
        return None
    sender = send_fn or cycle_watchdog.send_dependency_free_telegram
    try:
        sender(msg)
    except Exception:
        logger.debug("runtime flag manifest page send failed", exc_info=True)
    return msg


def check_startup_config(
    *,
    manifest_path: str | os.PathLike[str] = DEFAULT_MANIFEST_PATH,
    env: Mapping[str, str] | None = None,
    now: float | None = None,
    send_alert: bool = True,
    send_fn: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Evaluate, log one summary line, optionally page, and cache health."""
    global _last_status
    status = evaluate(manifest_path=manifest_path, env=env, now=now)
    _last_status = dict(status)
    if not status.get("available"):
        logger.info(
            "Runtime flag manifest skipped: present=%s path=%s error=%s",
            status.get("manifest_present"),
            status.get("manifest_path"),
            status.get("error"),
        )
        return _last_status
    if status.get("ok"):
        logger.info(
            "Runtime flag manifest OK: %s expected flags matched",
            status.get("expected_count"),
        )
    else:
        names = ",".join(str(item.get("name")) for item in status.get("mismatches") or [])
        logger.warning(
            "Runtime flag manifest mismatch: %s/%s flags drifted: %s",
            status.get("mismatch_count"),
            status.get("expected_count"),
            names,
        )
        if send_alert:
            maybe_send_alert(status, now=now, send_fn=send_fn)
    return _last_status


def health() -> dict[str, Any]:
    return dict(_last_status)
