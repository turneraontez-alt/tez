from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "local" / "Q15Local.psm1"
START_SCRIPT = ROOT / "scripts" / "local" / "Start-Q15Local.ps1"


@pytest.mark.skipif(os.name != "nt", reason="local launcher is Windows-only")
def test_exact_capture_restart_window_protects_required_history_and_commit():
    command = (
        "$ErrorActionPreference='Stop'; "
        f"Import-Module '{MODULE}' -Force; "
        "$phases=@(889,890,0,119,120,125,126); "
        "$phases | ForEach-Object { "
        "$now=[DateTimeOffset]::FromUnixTimeSeconds([long]$_); "
        "Get-Q15ExactCaptureRestartWindow -Now $now "
        "} | ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    by_phase = {
        int(row["phase_seconds"]): row
        for row in json.loads(completed.stdout)
    }
    assert by_phase[889]["protected"] is False
    assert by_phase[889]["seconds_until_capture"] == 131
    assert by_phase[890]["protected"] is True
    assert by_phase[890]["protected_before_seconds"] == 130
    assert by_phase[0]["protected"] is True
    assert by_phase[119]["protected"] is True
    assert by_phase[120]["protected"] is True
    assert by_phase[120]["retry_after_seconds"] == 6
    assert by_phase[125]["protected"] is True
    assert by_phase[125]["retry_after_seconds"] == 1
    assert by_phase[126]["protected"] is False


def test_launcher_guards_only_healthy_restart_and_requires_explicit_override():
    source = START_SCRIPT.read_text(encoding="utf-8")
    assert "Get-Q15ExactCaptureRestartWindow" in source
    assert "$healthyAppRunning -and -not $ForceUnsafeRestart" in source
    assert "Refusing to restart healthy Q15 app" in source
    assert "-ForceUnsafeRestart only for an emergency recovery" in source
