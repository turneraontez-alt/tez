from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "local" / "Q15Local.psm1"
WATCHDOG = ROOT / "scripts" / "local" / "Watch-Q15Collector.ps1"
INSTALLER = ROOT / "scripts" / "local" / "Install-Q15CollectorWatchdog.ps1"


def _healthy_payload() -> dict:
    assets = ["BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP"]
    return {
        "status": "ok",
        "data_age_seconds": 1.0,
        "uptime_seconds": 600,
        "rti_exact_13m": {
            "enabled": True,
            "thread_alive": True,
            "registration_watchdog_ok": True,
            "registration_watchdog_status": "OK",
            "overdue_registration_assets": [],
            "registered_assets": assets,
            "missed_deadlines": 7,
            "delayed_confirmation": {"record_thread_alive": True},
            "spot_rest_top_book_reservoir": {
                "started": True,
                "protocol_valid": True,
                "worker_count": 7,
                "worker_threads_alive": 7,
            },
        },
        "settlement_index": {
            "connected": True,
            "all_assets_ready": True,
            "missing_assets": [],
            "stale_assets": [],
        },
        "drift_delivery_reconcile": {
            "live_refresh_loop_blocking_allowed": False,
        },
    }


def _evaluate(tmp_path: Path, payload: dict, *, baseline: int = 7) -> dict:
    health_path = tmp_path / "health.json"
    health_path.write_text(json.dumps(payload), encoding="utf-8")
    command = (
        "$ErrorActionPreference='Stop'; "
        f"Import-Module '{MODULE}' -Force; "
        f"$h=Get-Content -LiteralPath '{health_path}' -Raw | ConvertFrom-Json; "
        f"Test-Q15CollectorHealthPayload -Health $h -BaselineMissedDeadlines {baseline} "
        "| ConvertTo-Json -Depth 6 -Compress"
    )
    result = subprocess.run(
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
    return json.loads(result.stdout)


@pytest.mark.skipif(os.name != "nt", reason="local watchdog is Windows-only")
def test_collector_watchdog_accepts_complete_fresh_health(tmp_path: Path):
    result = _evaluate(tmp_path, _healthy_payload())
    assert result["healthy"] is True
    assert result["reasons"] == []
    assert result["observed_missed_deadlines"] == 7


@pytest.mark.skipif(os.name != "nt", reason="local watchdog is Windows-only")
def test_collector_watchdog_never_absorbs_new_missed_deadlines(tmp_path: Path):
    payload = _healthy_payload()
    payload["rti_exact_13m"]["missed_deadlines"] = 8
    result = _evaluate(tmp_path, payload)
    assert result["healthy"] is False
    assert "EXACT_MISSED_DEADLINES_INCREASED" in result["reasons"]
    assert result["baseline_missed_deadlines"] == 7


@pytest.mark.skipif(os.name != "nt", reason="local watchdog is Windows-only")
def test_collector_watchdog_rejects_a_counter_reset_below_frozen_baseline(
    tmp_path: Path,
):
    payload = _healthy_payload()
    payload["rti_exact_13m"]["missed_deadlines"] = 0
    result = _evaluate(tmp_path, payload)
    assert result["healthy"] is False
    assert "EXACT_MISSED_DEADLINES_BELOW_BASELINE" in result["reasons"]
    assert result["baseline_missed_deadlines"] == 7


@pytest.mark.skipif(os.name != "nt", reason="local watchdog is Windows-only")
def test_collector_watchdog_detects_registration_worker_and_feed_failures(
    tmp_path: Path,
):
    payload = copy.deepcopy(_healthy_payload())
    payload["rti_exact_13m"]["registration_watchdog_ok"] = False
    payload["rti_exact_13m"]["overdue_registration_assets"] = ["BTC"]
    payload["rti_exact_13m"]["spot_rest_top_book_reservoir"][
        "worker_threads_alive"
    ] = 6
    payload["settlement_index"]["stale_assets"] = ["ETH"]
    result = _evaluate(tmp_path, payload)
    assert result["healthy"] is False
    assert {
        "EXACT_REGISTRATION_STALE",
        "EXACT_REGISTRATION_OVERDUE",
        "REST_RESERVOIR_WORKERS_UNHEALTHY",
        "SETTLEMENT_ASSETS_STALE",
    }.issubset(result["reasons"])


@pytest.mark.skipif(os.name != "nt", reason="local watchdog is Windows-only")
def test_watchdog_requires_two_failures_and_defers_inside_capture_guard(
    tmp_path: Path,
):
    payload = _healthy_payload()
    payload["rti_exact_13m"]["registration_watchdog_ok"] = False
    health_path = tmp_path / "health.json"
    state_path = tmp_path / "state.json"
    log_dir = tmp_path / "logs"
    health_path.write_text(json.dumps(payload), encoding="utf-8")
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(WATCHDOG),
        "-HealthJsonPath",
        str(health_path),
        "-StatePath",
        str(state_path),
        "-LogDirectory",
        str(log_dir),
        "-BaselineMissedDeadlines",
        "7",
        "-NowEpochSeconds",
        "890",
        "-NoRestart",
    ]
    first = subprocess.run(command, check=True, capture_output=True, text=True)
    second = subprocess.run(command, check=True, capture_output=True, text=True)
    assert json.loads(first.stdout)["action"] == "FAILURE_BELOW_THRESHOLD"
    event = json.loads(second.stdout)
    assert event["action"] == "RESTART_DEFERRED_CAPTURE_GUARD"
    assert event["capture_guard_protected"] is True


def test_watchdog_and_installer_freeze_recovery_safety_contract():
    source = WATCHDOG.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")
    assert "Get-Q15ExactCaptureRestartWindow" in source
    assert "RestartCooldownSeconds = 1200" in source
    assert "FailureThreshold = 2" in source
    assert "Set-Q15SafeTradingDefaults" in source
    assert "-SkipInstall" in source
    assert "will not move the baseline automatically" in source
    assert 'TaskName = "Q15 Collector Watchdog"' in installer
    assert "-MultipleInstances IgnoreNew" in installer
    assert "-BaselineMissedDeadlines" in installer
