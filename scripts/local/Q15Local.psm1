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
        [int]$PostCaptureSeconds = 100
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

function Get-Q15ExactCaptureWorkWindow {
    <#
    Return whether a bounded maintenance/test job would overlap the exact
    parent/+30/+60 capture guard.  ExpectedWorkSeconds is included before the
    next guard so callers cannot start a long job merely because this instant
    is safe.
    #>
    param(
        [DateTimeOffset]$Now = [DateTimeOffset]::UtcNow,
        [int]$PreCaptureSeconds = 75,
        [int]$PostCaptureSeconds = 100,
        [int]$ExpectedWorkSeconds = 0
    )
    foreach ($value in @(
        $PreCaptureSeconds,
        $PostCaptureSeconds,
        $ExpectedWorkSeconds
    )) {
        if ($value -lt 0) { throw "Exact-capture work timing values must be nonnegative" }
    }
    if ($PreCaptureSeconds + $PostCaptureSeconds + $ExpectedWorkSeconds -ge 900) {
        throw "Exact-capture work timing leaves no safe interval"
    }
    $epochSeconds = [long][Math]::Floor($Now.ToUnixTimeMilliseconds() / 1000.0)
    $phaseSeconds = [int](($epochSeconds % 900 + 900) % 900)
    $capturePhaseSeconds = 120
    $secondsUntilCapture = [int](($capturePhaseSeconds - $phaseSeconds + 900) % 900)
    $secondsSinceCapture = [int](($phaseSeconds - $capturePhaseSeconds + 900) % 900)
    $protectedBeforeSeconds = $PreCaptureSeconds + $ExpectedWorkSeconds
    $beforeCaptureRisk = $secondsUntilCapture -le $protectedBeforeSeconds
    $afterCaptureRisk = $secondsSinceCapture -le $PostCaptureSeconds
    $protected = $beforeCaptureRisk -or $afterCaptureRisk
    $retryAfterSeconds = 0
    $reason = "SAFE_FOR_BOUNDED_WORK"
    if ($beforeCaptureRisk) {
        $retryAfterSeconds = $secondsUntilCapture + $PostCaptureSeconds + 1
        $reason = "WORK_WOULD_OVERLAP_NEXT_EXACT_CAPTURE_GUARD"
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
        pre_capture_seconds = $PreCaptureSeconds
        post_capture_seconds = $PostCaptureSeconds
        expected_work_seconds = $ExpectedWorkSeconds
        protected_before_seconds = $protectedBeforeSeconds
        safe_work_seconds_before_guard = [math]::Max(
            0, $secondsUntilCapture - $PreCaptureSeconds
        )
        retry_after_seconds = [int]$retryAfterSeconds
    }
}

function Get-Q15ObjectProperty {
    <# Return a property value without violating StrictMode on partial health payloads. #>
    param(
        [AllowNull()]$Object,
        [Parameter(Mandatory = $true)][string]$Name
    )
    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Test-Q15CollectorHealthPayload {
    <#
    Evaluate only the collector invariants that justify an external recovery.

    This stays pure so the scheduled watchdog can be tested without touching the
    live process.  A cumulative exact missed-deadline count above the explicitly
    installed baseline is a failure; the watchdog must never silently move that
    baseline forward.
    #>
    param(
        [Parameter(Mandatory = $true)]$Health,
        [ValidateRange(0, [int]::MaxValue)][int]$BaselineMissedDeadlines = 0,
        [ValidateRange(1.0, 300.0)][double]$MaxDataAgeSeconds = 20.0,
        [ValidateRange(1, 32)][int]$RequiredAssetCount = 7
    )
    $reasons = [System.Collections.Generic.List[string]]::new()
    $status = Get-Q15ObjectProperty -Object $Health -Name "status"
    if ([string]$status -ne "ok") { $reasons.Add("APP_STATUS_NOT_OK") }

    $dataAge = Get-Q15ObjectProperty -Object $Health -Name "data_age_seconds"
    $parsedDataAge = 0.0
    if (
        $null -eq $dataAge -or
        -not [double]::TryParse(
            [string]$dataAge,
            [Globalization.NumberStyles]::Float,
            [Globalization.CultureInfo]::InvariantCulture,
            [ref]$parsedDataAge
        ) -or
        $parsedDataAge -lt 0.0 -or
        $parsedDataAge -gt $MaxDataAgeSeconds
    ) {
        $reasons.Add("APP_DATA_STALE")
    }

    $exact = Get-Q15ObjectProperty -Object $Health -Name "rti_exact_13m"
    if ($null -eq $exact) {
        $reasons.Add("EXACT_HEALTH_MISSING")
    }
    else {
        if ((Get-Q15ObjectProperty $exact "enabled") -ne $true) {
            $reasons.Add("EXACT_DISABLED")
        }
        if ((Get-Q15ObjectProperty $exact "thread_alive") -ne $true) {
            $reasons.Add("EXACT_THREAD_DEAD")
        }
        if ((Get-Q15ObjectProperty $exact "registration_watchdog_ok") -ne $true) {
            $reasons.Add("EXACT_REGISTRATION_STALE")
        }
        $overdue = @(Get-Q15ObjectProperty $exact "overdue_registration_assets")
        if ($overdue.Count -gt 0) {
            $reasons.Add("EXACT_REGISTRATION_OVERDUE")
        }
        $registered = @(Get-Q15ObjectProperty $exact "registered_assets")
        if ($registered.Count -ne $RequiredAssetCount) {
            $reasons.Add("EXACT_REGISTERED_ASSET_COUNT_INVALID")
        }
        $missedRaw = Get-Q15ObjectProperty $exact "missed_deadlines"
        $missed = -1
        if (-not [int]::TryParse([string]$missedRaw, [ref]$missed)) {
            $reasons.Add("EXACT_MISSED_DEADLINES_INVALID")
        }
        elseif ($missed -lt $BaselineMissedDeadlines) {
            $reasons.Add("EXACT_MISSED_DEADLINES_BELOW_BASELINE")
        }
        elseif ($missed -gt $BaselineMissedDeadlines) {
            $reasons.Add("EXACT_MISSED_DEADLINES_INCREASED")
        }

        $delayed = Get-Q15ObjectProperty $exact "delayed_confirmation"
        if ($null -eq $delayed -or
            (Get-Q15ObjectProperty $delayed "record_thread_alive") -ne $true) {
            $reasons.Add("DELAYED_CONFIRMATION_THREAD_DEAD")
        }

        $rest = Get-Q15ObjectProperty $exact "spot_rest_top_book_reservoir"
        if ($null -eq $rest) {
            $reasons.Add("REST_RESERVOIR_HEALTH_MISSING")
        }
        else {
            if ((Get-Q15ObjectProperty $rest "started") -ne $true) {
                $reasons.Add("REST_RESERVOIR_STOPPED")
            }
            if ((Get-Q15ObjectProperty $rest "protocol_valid") -ne $true) {
                $reasons.Add("REST_RESERVOIR_PROTOCOL_INVALID")
            }
            $workers = 0
            $alive = -1
            [void][int]::TryParse(
                [string](Get-Q15ObjectProperty $rest "worker_count"), [ref]$workers
            )
            [void][int]::TryParse(
                [string](Get-Q15ObjectProperty $rest "worker_threads_alive"), [ref]$alive
            )
            if ($workers -ne $RequiredAssetCount -or $alive -ne $workers) {
                $reasons.Add("REST_RESERVOIR_WORKERS_UNHEALTHY")
            }
        }
    }

    $settlement = Get-Q15ObjectProperty -Object $Health -Name "settlement_index"
    if ($null -eq $settlement) {
        $reasons.Add("SETTLEMENT_HEALTH_MISSING")
    }
    else {
        if ((Get-Q15ObjectProperty $settlement "connected") -ne $true) {
            $reasons.Add("SETTLEMENT_DISCONNECTED")
        }
        if ((Get-Q15ObjectProperty $settlement "all_assets_ready") -ne $true) {
            $reasons.Add("SETTLEMENT_ASSETS_NOT_READY")
        }
        if (@(Get-Q15ObjectProperty $settlement "missing_assets").Count -gt 0) {
            $reasons.Add("SETTLEMENT_ASSETS_MISSING")
        }
        if (@(Get-Q15ObjectProperty $settlement "stale_assets").Count -gt 0) {
            $reasons.Add("SETTLEMENT_ASSETS_STALE")
        }
    }

    $drift = Get-Q15ObjectProperty -Object $Health -Name "drift_delivery_reconcile"
    if ($null -eq $drift -or
        (Get-Q15ObjectProperty $drift "live_refresh_loop_blocking_allowed") -ne $false) {
        $reasons.Add("DRIFT_RECONCILE_BLOCKING_POLICY_INVALID")
    }

    $uniqueReasons = @($reasons | Sort-Object -Unique)
    return [pscustomobject]@{
        healthy = ($uniqueReasons.Count -eq 0)
        reasons = $uniqueReasons
        baseline_missed_deadlines = $BaselineMissedDeadlines
        observed_missed_deadlines = if ($null -eq $exact) {
            $null
        } else {
            Get-Q15ObjectProperty $exact "missed_deadlines"
        }
        registration_watchdog_status = if ($null -eq $exact) {
            $null
        } else {
            Get-Q15ObjectProperty $exact "registration_watchdog_status"
        }
        uptime_seconds = Get-Q15ObjectProperty -Object $Health -Name "uptime_seconds"
    }
}

function Get-Q15CriticalBackupDirectory {
    <# Resolve the off-machine archive destination with an explicit local fallback. #>
    param(
        [string]$BackupDir = "",
        [string]$Root = "",
        [string]$Override = $env:Q15_LOCAL_BACKUP_DIR,
        [string]$OneDrivePath = $env:OneDrive
    )
    if (-not $Root) { $Root = Get-Q15RepoRoot }
    if ($BackupDir) { return [IO.Path]::GetFullPath($BackupDir) }
    if ($Override) {
        if ([IO.Path]::IsPathRooted($Override)) {
            return [IO.Path]::GetFullPath($Override)
        }
        return [IO.Path]::GetFullPath((Join-Path $Root $Override))
    }
    if ($OneDrivePath -and (Test-Path -LiteralPath $OneDrivePath)) {
        return [IO.Path]::GetFullPath((
            Join-Path $OneDrivePath "Documents\Q15 Critical Backups"
        ))
    }
    return [IO.Path]::GetFullPath((Join-Path $Root "work/local-run/backups"))
}
