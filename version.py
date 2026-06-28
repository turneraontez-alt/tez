"""Deploy/version surface: proves which code the running app loaded.

``build_info.json`` is stamped by the ship script, but Replit can also run code
that was synced without refreshing that file. The payload reports both the file
stamp and the live checkout HEAD, then marks the stamp stale when they disagree.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone

_DIR = os.path.dirname(os.path.abspath(__file__))
_BUILD_INFO_PATH = os.path.join(_DIR, "build_info.json")

# Captured once, at import. Distinguishes a restarted process from synced files.
_PROCESS_STARTED_AT = time.time()


def _read_build_info() -> dict:
    info = {
        "commit": "unknown",
        "branch": None,
        "committed_at": None,
        "summary": None,
        "tests": None,
    }
    try:
        with open(_BUILD_INFO_PATH, encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            info.update({k: loaded.get(k, info[k]) for k in info})
    except (OSError, ValueError):
        pass
    return info


def _git_value(args: list[str]) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=_DIR,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    value = out.stdout.strip()
    return value or None


def _runtime_git_head() -> str | None:
    return _git_value(["rev-parse", "--short", "HEAD"])


# Build info is immutable for the life of the process; read it once.
_BUILD_INFO = _read_build_info()


def build_info() -> dict:
    return dict(_BUILD_INFO)


def _stale_build_info(info: dict, live_head: str | None) -> bool:
    stamped_commit = info.get("commit")
    return bool(
        live_head
        and stamped_commit
        and stamped_commit != "unknown"
        and stamped_commit != live_head
    )


def version_payload() -> dict:
    started = datetime.fromtimestamp(_PROCESS_STARTED_AT, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    live_head = _runtime_git_head()
    info = build_info()
    stale = _stale_build_info(info, live_head)
    return {
        **info,
        "running_commit": live_head,
        "matches_checkout": (live_head is not None and live_head == info.get("commit")),
        "build_info_stale": stale,
        "stale_build_info_commit": info.get("commit") if stale else None,
        "process_started_at": started.isoformat(),
        "uptime_seconds": round(now.timestamp() - _PROCESS_STARTED_AT, 1),
        "server_time": now.isoformat(),
    }


def version_text() -> str:
    p = version_payload()
    lines = [
        f"build   : {p.get('commit')}  ({p.get('branch') or '?'})",
        f"shipped : {p.get('committed_at') or '?'}",
        f"summary : {p.get('summary') or '?'}",
        f"tests   : {p.get('tests') or '?'}",
        f"running : {p.get('running_commit') or '?'}"
        + ("  matches" if p.get("matches_checkout") else "  differs / unknown"),
        f"stale   : {p.get('build_info_stale')}",
        f"started : {p.get('process_started_at')}  (up {p.get('uptime_seconds')}s)",
    ]
    return "\n".join(lines) + "\n"
