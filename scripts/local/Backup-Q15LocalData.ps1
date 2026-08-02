param(
    [string]$BackupDir = "",
    [int]$Keep = 7,
    [int]$ExpectedMaxRuntimeSeconds = 600,
    [switch]$IncludeHighVolume,
    [switch]$IncludeAllState,
    [switch]$IncludeSecrets,
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
            "Refusing critical-data backup because bounded work would overlap " +
            "exact RTI capture ({0}). Retry in {1}s."
        ) -f $captureWindow.reason, $captureWindow.retry_after_seconds)
    }
}
# `work` is deliberately junctioned to fast local AppData storage.  The shared
# resolver therefore prefers a real OneDrive directory for disaster recovery.
$BackupDir = Get-Q15CriticalBackupDirectory -BackupDir $BackupDir -Root $root
New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
$python = Get-Q15Python
$tool = Join-Path $root "tools/local_backup.py"
$toolArgs = @($tool, "--repo", $root, "--destination", $BackupDir)
if (-not $IncludeAllState) { $toolArgs += "--critical-only" }
if ($IncludeHighVolume) { $toolArgs += "--include-high-volume" }
if ($IncludeSecrets) { $toolArgs += "--include-secrets" }
$toolOutput = @(& $python @toolArgs)
$toolExitCode = $LASTEXITCODE
$dest = if ($toolOutput.Count -gt 0) {
    ([string]$toolOutput[-1]).Trim()
} else { "" }
if ($toolExitCode -ne 0 -or -not $dest -or -not (Test-Path -LiteralPath $dest)) {
    throw "Q15 backup failed"
}

$Keep = [math]::Max(1, $Keep)
$old = Get-ChildItem -LiteralPath $BackupDir -Filter "q15-data-*.zip" -File |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip $Keep
foreach ($file in $old) {
    Remove-Item -LiteralPath $file.FullName -Force
}
Write-Host "Wrote verified backup: $dest"
Write-Host "Retention: newest $Keep backup(s); critical-only=$(-not [bool]$IncludeAllState); high-volume collectors included=$([bool]$IncludeHighVolume)"
