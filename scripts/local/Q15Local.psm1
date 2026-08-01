Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-Q15RepoRoot {
    return (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
}

function Read-Q15EnvFile {
    param([string]$Path)
    $values = [ordered]@{}
    if (-not (Test-Path -LiteralPath $Path)) { return $values }
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trim = $line.Trim()
        if (-not $trim -or $trim.StartsWith('#')) { continue }
        $idx = $trim.IndexOf('=')
        if ($idx -lt 1) { continue }
        $key = $trim.Substring(0, $idx).Trim()
        $value = $trim.Substring($idx + 1).Trim()
        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $values[$key] = $value
    }
    return $values
}

function Import-Q15Env {
    param([string]$EnvFile)
    $root = Get-Q15RepoRoot
    if (-not $EnvFile) { $EnvFile = Join-Path $root ".env.local" }
    if (-not (Test-Path -LiteralPath $EnvFile)) {
        throw "Missing env file: $EnvFile. Copy .env.local.example to .env.local and fill secrets."
    }
    $values = Read-Q15EnvFile -Path $EnvFile
    foreach ($key in $values.Keys) {
        [Environment]::SetEnvironmentVariable($key, [string]$values[$key], 'Process')
    }
    return $values
}

function Get-Q15Python {
    $root = Get-Q15RepoRoot
    if ($env:Q15_LOCAL_PYTHON -and (Test-Path -LiteralPath $env:Q15_LOCAL_PYTHON)) { return $env:Q15_LOCAL_PYTHON }
    $candidates = @(
        (Join-Path $root ".venv\Scripts\python.exe"),
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Python\pythoncore-3.11-64\python.exe",
        "C:\Python311\python.exe",
        "C:\Users\Turne\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe",
        "python"
    )
    foreach ($candidate in $candidates) {
        if ($candidate -eq "python") { return $candidate }
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return "python"
}

function Add-Q15LocalToolPath {
    $paths = @(
        "C:\Users\Turne\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\git\cmd",
        "$env:LOCALAPPDATA\Programs\Python\Python311",
        "$env:LOCALAPPDATA\Programs\Python\Python311\Scripts",
        "$env:LOCALAPPDATA\Python\pythoncore-3.11-64",
        "$env:LOCALAPPDATA\Python\pythoncore-3.11-64\Scripts",
        "C:\Python311",
        "C:\Python311\Scripts"
    )
    foreach ($path in $paths) {
        if ((Test-Path -LiteralPath $path) -and (($env:PATH -split ';') -notcontains $path)) {
            $env:PATH = "$path;$env:PATH"
        }
    }
}

function Initialize-Q15LocalDirs {
    $root = Get-Q15RepoRoot
    $dirs = @("data", "work\local-run", "work\local-run\logs", "work\local-run\pids", "work\local-run\backups")
    foreach ($dir in $dirs) { New-Item -ItemType Directory -Force -Path (Join-Path $root $dir) | Out-Null }
}

function Set-Q15SafeTradingDefaults {
    param([switch]$AllowLiveTrading)
    if ($AllowLiveTrading) { return }
    $env:Q15_EXEC_DRY_RUN = "true"
    $env:Q15_EXEC_KILL = "true"
    $env:Q15_EXEC_YES_DRY_RUN = "true"
    $env:Q15_EXEC_YES_KILL = "true"
}

function Get-Q15Port {
    if ($env:PORT) { return [int]$env:PORT }
    return 8000
}

function Get-Q15ExactCaptureRestartWindow {
    <#
    Return the deterministic restart risk around each exact-13M capture.

    Q15 close times are aligned to 15-minute Unix boundaries and the exact
    decision occurs 780 seconds before close, at epoch phase 120 modulo 900.
    A warm restart must budget for both the required 60-second lookback and
    the observed stop/maintenance/cold-start time.  This helper is pure so the
    launcher policy can be regression-tested without touching live services.
    #>
    param(
        [DateTimeOffset]$Now = [DateTimeOffset]::UtcNow,
        [int]$RequiredHistorySeconds = 60,
        [int]$RestartBudgetSeconds = 60,
        [int]$SafetyMarginSeconds = 10,
        [int]$PostCaptureSeconds = 5
    )
    foreach ($value in @(
        $RequiredHistorySeconds,
        $RestartBudgetSeconds,
        $SafetyMarginSeconds,
        $PostCaptureSeconds
    )) {
        if ($value -lt 0) { throw "Exact-capture restart timing values must be nonnegative" }
    }
    $epochSeconds = [long][Math]::Floor($Now.ToUnixTimeMilliseconds() / 1000.0)
    $phaseSeconds = [int](($epochSeconds % 900 + 900) % 900)
    $capturePhaseSeconds = 120
    $secondsUntilCapture = [int](($capturePhaseSeconds - $phaseSeconds + 900) % 900)
    $secondsSinceCapture = [int](($phaseSeconds - $capturePhaseSeconds + 900) % 900)
    $protectedBeforeSeconds = (
        $RequiredHistorySeconds + $RestartBudgetSeconds + $SafetyMarginSeconds
    )
    $beforeCaptureRisk = $secondsUntilCapture -le $protectedBeforeSeconds
    $afterCaptureRisk = $secondsSinceCapture -le $PostCaptureSeconds
    $protected = $beforeCaptureRisk -or $afterCaptureRisk
    $retryAfterSeconds = 0
    $reason = "SAFE_TO_RESTART"
    if ($beforeCaptureRisk) {
        $retryAfterSeconds = $secondsUntilCapture + $PostCaptureSeconds + 1
        $reason = "REQUIRED_HISTORY_AND_COLD_START_OVERLAP_NEXT_EXACT_CAPTURE"
    }
    elseif ($afterCaptureRisk) {
        $retryAfterSeconds = $PostCaptureSeconds - $secondsSinceCapture + 1
        $reason = "EXACT_CAPTURE_MAY_STILL_BE_COMMITTING"
    }
    return [pscustomobject]@{
        protected = [bool]$protected
        reason = $reason
        epoch_seconds = $epochSeconds
        phase_seconds = $phaseSeconds
        capture_phase_seconds = $capturePhaseSeconds
        seconds_until_capture = $secondsUntilCapture
        seconds_since_capture = $secondsSinceCapture
        protected_before_seconds = $protectedBeforeSeconds
        post_capture_seconds = $PostCaptureSeconds
        retry_after_seconds = [int]$retryAfterSeconds
    }
}
