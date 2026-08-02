param(
    [string]$EnvFile = "",
    [string]$StatePath = "",
    [string]$LogDirectory = "",
    [ValidateRange(0, [int]::MaxValue)]
    [int]$BaselineMissedDeadlines = 7,
    [ValidateRange(1, 10)]
    [int]$FailureThreshold = 2,
    [ValidateRange(300, 86400)]
    [int]$RestartCooldownSeconds = 1200,
    [ValidateRange(2, 30)]
    [int]$HealthTimeoutSeconds = 8,
    [ValidateRange(1, 300)]
    [int]$MaxDataAgeSeconds = 20,
    [string]$HealthJsonPath = "",
    [long]$NowEpochSeconds = 0,
    [switch]$NoRestart
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "Q15Local.psm1") -Force

$root = Get-Q15RepoRoot
Set-Location -LiteralPath $root
Import-Q15Env -EnvFile $EnvFile | Out-Null
Initialize-Q15LocalDirs
Set-Q15SafeTradingDefaults

if ($HealthJsonPath -and -not $NoRestart) {
    throw "-HealthJsonPath is diagnostic-only and requires -NoRestart"
}

if (-not $EnvFile) { $EnvFile = Join-Path $root ".env.local" }
if (-not $StatePath) {
    $StatePath = Join-Path $root "work\local-run\q15-collector-watchdog-v1.json"
}
if (-not $LogDirectory) {
    $LogDirectory = Join-Path $root "work\local-run\logs\collector-watchdog"
}
New-Item -ItemType Directory -Force -Path (
    Split-Path -Parent $StatePath
),$LogDirectory | Out-Null

$mutex = [Threading.Mutex]::new($false, "Local\Q15CollectorWatchdogV1")
$lockAcquired = $false
try {
    try {
        $lockAcquired = $mutex.WaitOne(0)
    }
    catch [Threading.AbandonedMutexException] {
        $lockAcquired = $true
    }
    if (-not $lockAcquired) {
        Write-Output '{"action":"SKIPPED_OVERLAPPING_WATCHDOG"}'
        exit 0
    }

    $now = if ($NowEpochSeconds -gt 0) {
        [DateTimeOffset]::FromUnixTimeSeconds($NowEpochSeconds)
    } else {
        [DateTimeOffset]::UtcNow
    }
    $nowEpoch = $now.ToUnixTimeSeconds()
    $nowIso = $now.UtcDateTime.ToString("o")
    $logPath = Join-Path $LogDirectory (
        "q15-collector-watchdog-{0}.jsonl" -f $now.UtcDateTime.ToString("yyyyMMdd")
    )

    # Daily logs are bounded without touching any live database or evidence file.
    Get-ChildItem -LiteralPath $LogDirectory -File -Filter "q15-collector-watchdog-*.jsonl" |
        Where-Object { $_.LastWriteTimeUtc -lt $now.UtcDateTime.AddDays(-30) } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }

    $state = [ordered]@{
        schema_version = "q15-collector-watchdog-state-v1"
        baseline_missed_deadlines = $BaselineMissedDeadlines
        consecutive_failures = 0
        first_failure_at = $null
        last_failure_at = $null
        last_failure_reasons = @()
        last_healthy_at = $null
        last_restart_at = $null
        last_restart_epoch = 0
        restart_count = 0
        last_action = "INITIALIZED"
        updated_at = $nowIso
    }
    if (Test-Path -LiteralPath $StatePath) {
        $loaded = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
        foreach ($property in $loaded.PSObject.Properties) {
            if ($state.Contains($property.Name)) {
                $state[$property.Name] = $property.Value
            }
        }
        if ([int]$state.baseline_missed_deadlines -ne $BaselineMissedDeadlines) {
            throw (
                "Installed missed-deadline baseline mismatch: state={0} argument={1}. " +
                "The watchdog will not move the baseline automatically."
            ) -f $state.baseline_missed_deadlines,$BaselineMissedDeadlines
        }
    }

    function Save-WatchdogState {
        $state.updated_at = $nowIso
        $temporary = "$StatePath.tmp.$PID"
        $state | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temporary -Encoding UTF8
        Move-Item -LiteralPath $temporary -Destination $StatePath -Force
    }

    function Write-WatchdogEvent {
        param(
            [Parameter(Mandatory = $true)][string]$Action,
            [string[]]$Reasons = @(),
            [AllowNull()]$Evaluation = $null,
            [AllowNull()]$Guard = $null,
            [AllowNull()]$Detail = $null
        )
        $event = [ordered]@{
            schema_version = "q15-collector-watchdog-event-v1"
            at = $nowIso
            action = $Action
            reasons = @($Reasons)
            consecutive_failures = [int]$state.consecutive_failures
            baseline_missed_deadlines = $BaselineMissedDeadlines
            observed_missed_deadlines = if ($null -eq $Evaluation) {
                $null
            } else { $Evaluation.observed_missed_deadlines }
            registration_watchdog_status = if ($null -eq $Evaluation) {
                $null
            } else { $Evaluation.registration_watchdog_status }
            restart_count = [int]$state.restart_count
            capture_guard_protected = if ($null -eq $Guard) {
                $null
            } else { [bool]$Guard.protected }
            capture_guard_reason = if ($null -eq $Guard) {
                $null
            } else { $Guard.reason }
            detail = $Detail
        }
        $line = $event | ConvertTo-Json -Depth 8 -Compress
        Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
        Write-Output $line
    }

    $evaluation = $null
    try {
        if ($HealthJsonPath) {
            $health = Get-Content -LiteralPath $HealthJsonPath -Raw | ConvertFrom-Json
        }
        else {
            $port = Get-Q15Port
            $health = Invoke-RestMethod `
                -Uri "http://127.0.0.1:$port/api/health" `
                -TimeoutSec $HealthTimeoutSeconds
        }
        $evaluation = Test-Q15CollectorHealthPayload `
            -Health $health `
            -BaselineMissedDeadlines $BaselineMissedDeadlines `
            -MaxDataAgeSeconds $MaxDataAgeSeconds
    }
    catch {
        $evaluation = [pscustomobject]@{
            healthy = $false
            reasons = @("HEALTH_ENDPOINT_UNREACHABLE")
            observed_missed_deadlines = $null
            registration_watchdog_status = $null
            uptime_seconds = $null
            detail = $_.Exception.Message
        }
    }

    if ($evaluation.healthy) {
        $state.consecutive_failures = 0
        $state.first_failure_at = $null
        $state.last_failure_reasons = @()
        $state.last_healthy_at = $nowIso
        $state.last_action = "HEALTHY"
        Save-WatchdogState
        Write-WatchdogEvent -Action "HEALTHY" -Evaluation $evaluation
        exit 0
    }

    $state.consecutive_failures = [int]$state.consecutive_failures + 1
    if ($null -eq $state.first_failure_at) { $state.first_failure_at = $nowIso }
    $state.last_failure_at = $nowIso
    $state.last_failure_reasons = @($evaluation.reasons)
    if ([int]$state.consecutive_failures -lt $FailureThreshold) {
        $state.last_action = "FAILURE_BELOW_THRESHOLD"
        Save-WatchdogState
        Write-WatchdogEvent `
            -Action "FAILURE_BELOW_THRESHOLD" `
            -Reasons $evaluation.reasons `
            -Evaluation $evaluation
        exit 0
    }

    $guard = Get-Q15ExactCaptureRestartWindow -Now $now
    $secondsSinceRestart = $nowEpoch - [long]$state.last_restart_epoch
    if ([long]$state.last_restart_epoch -gt 0 -and
        $secondsSinceRestart -lt $RestartCooldownSeconds) {
        $state.last_action = "RESTART_DEFERRED_COOLDOWN"
        Save-WatchdogState
        Write-WatchdogEvent `
            -Action "RESTART_DEFERRED_COOLDOWN" `
            -Reasons $evaluation.reasons `
            -Evaluation $evaluation `
            -Guard $guard `
            -Detail @{ seconds_remaining = $RestartCooldownSeconds - $secondsSinceRestart }
        exit 0
    }
    if ($guard.protected) {
        $state.last_action = "RESTART_DEFERRED_CAPTURE_GUARD"
        Save-WatchdogState
        Write-WatchdogEvent `
            -Action "RESTART_DEFERRED_CAPTURE_GUARD" `
            -Reasons $evaluation.reasons `
            -Evaluation $evaluation `
            -Guard $guard `
            -Detail @{ retry_after_seconds = $guard.retry_after_seconds }
        exit 0
    }
    if ($NoRestart) {
        $state.last_action = "RESTART_SUPPRESSED_BY_NO_RESTART"
        Save-WatchdogState
        Write-WatchdogEvent `
            -Action "RESTART_SUPPRESSED_BY_NO_RESTART" `
            -Reasons $evaluation.reasons `
            -Evaluation $evaluation `
            -Guard $guard
        exit 0
    }

    $startScript = Join-Path $PSScriptRoot "Start-Q15Local.ps1"
    try {
        $restartOutput = (& $startScript -EnvFile $EnvFile -SkipInstall 2>&1 | Out-String).Trim()
        $state.last_restart_at = $nowIso
        $state.last_restart_epoch = $nowEpoch
        $state.restart_count = [int]$state.restart_count + 1
        $state.consecutive_failures = 0
        $state.first_failure_at = $null
        $state.last_action = "RESTARTED_SAFE_DEFAULTS"
        Save-WatchdogState
        Write-WatchdogEvent `
            -Action "RESTARTED_SAFE_DEFAULTS" `
            -Reasons $evaluation.reasons `
            -Evaluation $evaluation `
            -Guard $guard `
            -Detail @{ launcher_output = $restartOutput }
    }
    catch {
        $state.last_action = "RESTART_FAILED"
        Save-WatchdogState
        Write-WatchdogEvent `
            -Action "RESTART_FAILED" `
            -Reasons $evaluation.reasons `
            -Evaluation $evaluation `
            -Guard $guard `
            -Detail @{ error = $_.Exception.Message }
        exit 1
    }
}
finally {
    if ($lockAcquired) { [void]$mutex.ReleaseMutex() }
    $mutex.Dispose()
}
