param(
    [string]$EnvFile = "",
    [string]$BackupDir = "",
    [ValidateRange(75, 300)][int]$PreCaptureSeconds = 90,
    [ValidateRange(100, 300)][int]$PostCaptureSeconds = 100,
    [long]$NowEpochSeconds = 0,
    [switch]$NoProcessChanges
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "Q15Local.psm1") -Force
$root = Get-Q15RepoRoot
Import-Q15Env -EnvFile $EnvFile | Out-Null
$BackupDir = Get-Q15CriticalBackupDirectory -BackupDir $BackupDir -Root $root
$now = if ($NowEpochSeconds -gt 0) {
    [DateTimeOffset]::FromUnixTimeSeconds($NowEpochSeconds)
} else { [DateTimeOffset]::UtcNow }
$guard = Get-Q15ExactCaptureWorkWindow `
    -Now $now `
    -PreCaptureSeconds $PreCaptureSeconds `
    -PostCaptureSeconds $PostCaptureSeconds

$action = "NO_ARCHIVE"
$status = $null
$archive = Get-ChildItem -LiteralPath $BackupDir -Filter "q15-data-*.zip" -File `
    -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if ($archive) {
    $shell = New-Object -ComObject Shell.Application
    $folder = $shell.Namespace($BackupDir)
    $item = if ($folder) { $folder.ParseName($archive.Name) } else { $null }
    $availabilityIndex = $null
    if ($folder) {
        for ($index = 0; $index -lt 400; $index++) {
            if ($folder.GetDetailsOf($null, $index) -eq "Availability status") {
                $availabilityIndex = $index
                break
            }
        }
    }
    if ($item -and $null -ne $availabilityIndex) {
        $status = [string]$folder.GetDetailsOf($item, $availabilityIndex)
    }
    $pending = $status -match "Sync pending|Syncing|Uploading"
    $oneDrive = @(Get-Process OneDrive -ErrorAction SilentlyContinue)
    $running = $oneDrive.Count -gt 0
    if ($guard.protected -and $pending) {
        if ($running -and -not $NoProcessChanges) {
            $oneDrive | Stop-Process -Force
            $running = $false
            $action = "PAUSED_PENDING_ARCHIVE_FOR_CAPTURE"
        }
        else {
            $action = "PENDING_ARCHIVE_PAUSED_OR_DRY_RUN"
        }
    }
    elseif (-not $guard.protected -and $pending) {
        if (-not $running -and -not $NoProcessChanges) {
            $oneDriveExe = Join-Path $env:LOCALAPPDATA "Microsoft\OneDrive\OneDrive.exe"
            if (-not (Test-Path -LiteralPath $oneDriveExe)) {
                throw "OneDrive executable is unavailable"
            }
            Start-Process -FilePath $oneDriveExe -WindowStyle Hidden
            $action = "RESUMED_PENDING_ARCHIVE_OUTSIDE_CAPTURE_GUARD"
        }
        elseif ($running) {
            $action = "PENDING_ARCHIVE_SYNC_RUNNING_OUTSIDE_CAPTURE_GUARD"
        }
        else {
            $action = "PENDING_ARCHIVE_RESUME_DRY_RUN"
        }
    }
    elseif ($status) {
        $action = "ARCHIVE_NOT_PENDING"
    }
    else {
        $action = "ARCHIVE_SYNC_STATUS_UNAVAILABLE"
    }
}

$event = [ordered]@{
    schema_version = "q15-backup-sync-capture-guard-v1"
    at = $now.UtcDateTime.ToString("o")
    action = $action
    archive = if ($archive) { $archive.Name } else { $null }
    availability_status = $status
    capture_guard_protected = [bool]$guard.protected
    capture_guard_reason = $guard.reason
    seconds_until_capture = $guard.seconds_until_capture
    seconds_since_capture = $guard.seconds_since_capture
    one_drive_running = [bool](Get-Process OneDrive -ErrorAction SilentlyContinue)
    process_changes_allowed = -not [bool]$NoProcessChanges
}
$logDir = Join-Path $root "work\local-run\logs\backup-sync-guard"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logPath = Join-Path $logDir (
    "q15-backup-sync-guard-{0}.jsonl" -f $now.UtcDateTime.ToString("yyyyMMdd")
)
$line = $event | ConvertTo-Json -Compress
Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
Write-Output $line
