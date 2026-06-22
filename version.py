"""Deploy/version surface — proves which code the RUNNING app is on.

``build_info.json`` is stamped on every ship (see ``scripts/ship.sh``) with the
shipped commit, UTC timestamp, summary and test count. It is read ONCE at process
start, so ``/version`` reflects the code the running process actually loaded — if
it still shows the old build, the app hasn't been restarted (Stop ▸ Run) onto the
new code yet, even though the GitHub Relay may have already synced the files.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone

_DIR = os.path.dirname(os.path.abspath(__file__))
_BUILD_INFO_PATH = os.path.join(_DIR, "build_info.json")

# Captured once, at import (≈ process start). Distinguishes "restarted onto new
# code" from "files synced but not yet reloaded".
_PROCESS_STARTED_AT = time.time()


def _read_build_info() -> dict:
    info = {"commit": "unknown", "branch": None, "committed_at": None,
            "summary": None, "tests": None}
    try:
        with open(_BUILD_INFO_PATH, encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            info.update({k: loaded.get(k, info[k]) for k in info})
    except (OSError, ValueError):
        pass
    return info


def _runtime_git_head() -> str | None:
    """Best-effort live HEAD of the deployed checkout (None if git unavailable).
    A cross-check: it should match build_info.commit on a clean deploy."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=_DIR,
            capture_output=True, text=True, timeout=2.0,
        )
        sha = out.stdout.strip()
        return sha or None
    except (OSError, subprocess.SubprocessError):
        return None


# Build info is immutable for the life of the process; read it once.
_BUILD_INFO = _read_build_info()


def build_info() -> dict:
    return dict(_BUILD_INFO)


def version_payload() -> dict:
    started = datetime.fromtimestamp(_PROCESS_STARTED_AT, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    live_head = _runtime_git_head()
    info = build_info()
    return {
        **info,
        "running_commit": live_head,
        "matches_checkout": (live_head is not None and live_head == info.get("commit")),
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
        + ("  ✓ matches" if p.get("matches_checkout") else "  ⚠ differs / unknown"),
        f"started : {p.get('process_started_at')}  (up {p.get('uptime_seconds')}s)",
    ]
    return "\n".join(lines) + "\n"
