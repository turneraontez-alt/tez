#!/usr/bin/env python3
"""Run the web app plus Replit background workers as one deployment process."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable or "python3"


@dataclass
class Service:
    name: str
    argv: Sequence[str]
    required: bool = False
    disabled_env: str | None = None
    requires_token: bool = False
    requires_git_dir: bool = False
    process: subprocess.Popen | None = None
    restart_at: float = 0.0
    failures: int = 0
    started_at: float = 0.0
    blocked: bool = False


def _log(message: str) -> None:
    print(f"[supervisor] {message}", flush=True)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_disabled(service: Service) -> bool:
    return bool(service.disabled_env and _truthy(os.environ.get(service.disabled_env)))


def _github_token() -> str | None:
    return os.environ.get("GH_PUSH_TOKEN") or os.environ.get("GITHUB_TOKEN")


def _blocked_reason(service: Service) -> str | None:
    if _is_disabled(service):
        return f"disabled by {service.disabled_env}"
    if service.requires_token and not _github_token():
        return "missing GH_PUSH_TOKEN/GITHUB_TOKEN"
    if service.requires_git_dir and not (ROOT / ".git").exists():
        return "missing .git checkout"
    return None


def _start(service: Service) -> None:
    reason = _blocked_reason(service)
    if reason:
        if not service.blocked:
            _log(f"{service.name} not started: {reason}")
        service.blocked = True
        return
    service.blocked = False
    _log(f"starting {service.name}: {' '.join(service.argv)}")
    service.process = subprocess.Popen(list(service.argv), cwd=ROOT)
    service.started_at = time.time()


def _stop(service: Service, timeout: float = 15.0) -> None:
    process = service.process
    if process is None or process.poll() is not None:
        return
    _log(f"stopping {service.name}")
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _log(f"killing {service.name} after {timeout:.0f}s")
        process.kill()
        process.wait(timeout=5)


def _restart_delay(service: Service, now: float) -> float:
    ran_for = now - service.started_at if service.started_at else 0.0
    if ran_for >= 300:
        service.failures = 0
    service.failures += 1
    return min(300.0, 5.0 * (2 ** min(service.failures - 1, 6)))


def main() -> int:
    services = [
        Service(
            "app",
            [PYTHON, "app.py"],
            required=True,
            disabled_env="REPLIT_SUPERVISOR_DISABLE_APP",
        ),
        Service(
            "github-relay",
            [PYTHON, "tools/github_relay.py"],
            disabled_env="REPLIT_SUPERVISOR_DISABLE_RELAY",
            requires_token=True,
            requires_git_dir=True,
        ),
        Service(
            "learning-export",
            [PYTHON, "tools/learning_export.py"],
            disabled_env="REPLIT_SUPERVISOR_DISABLE_LEARNING_EXPORT",
            requires_token=True,
        ),
    ]

    stopping = False
    app_exit_code = 0

    def _handle_signal(signum: int, _frame: object) -> None:
        nonlocal stopping, app_exit_code
        stopping = True
        app_exit_code = 128 + signum
        _log(f"received signal {signum}; shutting down")

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    for service in services:
        _start(service)

    while not stopping:
        now = time.time()
        for service in services:
            process = service.process
            if process is None:
                if not service.blocked and now >= service.restart_at:
                    _start(service)
                continue

            return_code = process.poll()
            if return_code is None:
                continue

            service.process = None
            _log(f"{service.name} exited with code {return_code}")
            if service.required:
                app_exit_code = return_code
                stopping = True
                break

            delay = _restart_delay(service, now)
            service.restart_at = now + delay
            _log(f"restarting {service.name} in {delay:.0f}s")

        time.sleep(1)

    for service in reversed(services):
        _stop(service)

    return app_exit_code


if __name__ == "__main__":
    raise SystemExit(main())
