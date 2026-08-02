param(
    [int]$KeepBackups = 7,
    [double]$DiagnosticRetentionDays = 14,
    [int]$ExpectedMaxRuntimeSeconds = 600,
    [switch]$ForceUnsafeCaptureOverlap
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "Q15Local.psm1") -Force
$root = Get-Q15RepoRoot
$pidDir = Join-Path $root $(
    if ($env:Q15_LOCAL_PID_DIR) { $env:Q15_LOCAL_PID_DIR }
    else { "work/local-run/pids" }
)
$appPidPath = Join-Path $pidDir "app.pid"
$healthyAppRunning = $false
if (Test-Path -LiteralPath $appPidPath) {
    try {
        $appPid = [int](Get-Content -LiteralPath $appPidPath -Raw).Trim()
        $appProcess = Get-Process -Id $appPid -ErrorAction Stop
        $healthyAppRunning = -not $appProcess.HasExited
    }
    catch { $healthyAppRunning = $false }
}
if ($healthyAppRunning -and -not $ForceUnsafeCaptureOverlap) {
    $captureWindow = Get-Q15ExactCaptureWorkWindow `
        -ExpectedWorkSeconds $ExpectedMaxRuntimeSeconds
    if ($captureWindow.protected) {
        throw ((
            "Refusing storage maintenance because bounded work would overlap " +
            "exact RTI capture ({0}). Retry in {1}s."
        ) -f $captureWindow.reason, $captureWindow.retry_after_seconds)
    }
}
$python = Get-Q15Python
$tool = Join-Path $root "tools/storage_maintenance.py"
& $python $tool --repo $root --keep-backups $KeepBackups --diagnostic-days $DiagnosticRetentionDays
if ($LASTEXITCODE -ne 0) { throw "Q15 storage maintenance failed" }
