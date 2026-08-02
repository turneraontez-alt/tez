from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "local" / "Q15Local.psm1"
START_SCRIPT = ROOT / "scripts" / "local" / "Start-Q15Local.ps1"
MAINTENANCE_SCRIPT = ROOT / "scripts" / "local" / "Optimize-Q15Storage.ps1"
BACKUP_SCRIPT = ROOT / "scripts" / "local" / "Backup-Q15LocalData.ps1"
INSTALL_TASKS_SCRIPT = ROOT / "scripts" / "local" / "Install-Q15StorageTasks.ps1"
WORK_GUARD_SCRIPT = ROOT / "scripts" / "local" / "Test-Q15CaptureGuard.ps1"
BACKUP_SYNC_GUARD = ROOT / "scripts" / "local" / "Watch-Q15BackupSync.ps1"
BACKUP_SYNC_INSTALLER = ROOT / "scripts" / "local" / "Install-Q15BackupSyncGuard.ps1"


@pytest.mark.skipif(os.name != "nt", reason="local launcher is Windows-only")
def test_exact_capture_restart_window_protects_required_history_and_commit():
    command = (
        "$ErrorActionPreference='Stop'; "
        f"Import-Module '{MODULE}' -Force; "
        "$phases=@(889,890,0,119,120,125,220,221); "
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
    assert by_phase[120]["retry_after_seconds"] == 101
    assert by_phase[125]["protected"] is True
    assert by_phase[125]["retry_after_seconds"] == 96
    assert by_phase[220]["protected"] is True
    assert by_phase[220]["retry_after_seconds"] == 1
    assert by_phase[221]["protected"] is False


def test_launcher_guards_only_healthy_restart_and_requires_explicit_override():
    source = START_SCRIPT.read_text(encoding="utf-8")
    assert "Get-Q15ExactCaptureRestartWindow" in source
    assert "$healthyAppRunning -and -not $ForceUnsafeRestart" in source
    assert "Refusing to restart healthy Q15 app" in source
    assert "-ForceUnsafeRestart only for an emergency recovery" in source


@pytest.mark.skipif(os.name != "nt", reason="local launcher is Windows-only")
def test_exact_capture_work_window_reserves_runtime_before_guard():
    command = (
        "$ErrorActionPreference='Stop'; "
        f"Import-Module '{MODULE}' -Force; "
        "$phases=@(884,885,120,220,221,300,600); "
        "$phases | ForEach-Object { "
        "$now=[DateTimeOffset]::FromUnixTimeSeconds([long]$_); "
        "$expected=if($_ -in @(300,600)){600}else{60}; "
        "Get-Q15ExactCaptureWorkWindow -Now $now -ExpectedWorkSeconds $expected "
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
    assert by_phase[884]["protected"] is False
    assert by_phase[884]["safe_work_seconds_before_guard"] == 61
    assert by_phase[885]["protected"] is True
    assert by_phase[885]["protected_before_seconds"] == 135
    assert by_phase[120]["protected"] is True
    assert by_phase[220]["protected"] is True
    assert by_phase[221]["protected"] is False
    assert by_phase[300]["protected"] is False
    assert by_phase[300]["safe_work_seconds_before_guard"] == 645
    assert by_phase[600]["protected"] is True


def test_storage_jobs_and_default_schedules_protect_exact_capture():
    maintenance = MAINTENANCE_SCRIPT.read_text(encoding="utf-8")
    backup = BACKUP_SCRIPT.read_text(encoding="utf-8")
    installer = INSTALL_TASKS_SCRIPT.read_text(encoding="utf-8")
    for source in (maintenance, backup):
        assert "Get-Q15ExactCaptureWorkWindow" in source
        assert "ExpectedMaxRuntimeSeconds = 600" in source
        assert "$healthyAppRunning -and -not $ForceUnsafeCaptureOverlap" in source
    assert 'if (-not $IncludeAllState) { $toolArgs += "--critical-only" }' in backup
    assert "Get-Q15CriticalBackupDirectory" in backup
    assert '[string]$MaintenanceTime = "02:50"' in installer
    assert '[string]$BackupTime = "03:20"' in installer


@pytest.mark.skipif(os.name != "nt", reason="local launcher is Windows-only")
def test_capture_guard_command_exits_75_only_for_overlap():
    unsafe = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(WORK_GUARD_SCRIPT),
            "-ExpectedWorkSeconds", "60",
            "-NowEpochSeconds", "885",
            "-RequireSafe",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    safe = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(WORK_GUARD_SCRIPT),
            "-ExpectedWorkSeconds", "60",
            "-NowEpochSeconds", "884",
            "-RequireSafe",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert unsafe.returncode == 75
    assert json.loads(unsafe.stdout)["protected"] is True
    assert safe.returncode == 0
    assert json.loads(safe.stdout)["protected"] is False


def test_backup_sync_guard_pauses_pending_uploads_away_from_capture():
    source = BACKUP_SYNC_GUARD.read_text(encoding="utf-8")
    installer = BACKUP_SYNC_INSTALLER.read_text(encoding="utf-8")
    assert "Get-Q15ExactCaptureWorkWindow" in source
    assert "PreCaptureSeconds = 90" in source
    assert "PostCaptureSeconds = 100" in source
    assert "PAUSED_PENDING_ARCHIVE_FOR_CAPTURE" in source
    assert "RESUMED_PENDING_ARCHIVE_OUTSIDE_CAPTURE_GUARD" in source
    assert "Stop-Process -Force" in source
    assert "-WindowStyle Hidden" in source
    assert "-Second 30" in installer
    assert "-RepetitionInterval (New-TimeSpan -Minutes 1)" in installer
