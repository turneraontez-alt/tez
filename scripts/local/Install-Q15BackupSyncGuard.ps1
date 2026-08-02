param(
    [string]$TaskName = "Q15 Backup Sync Guard",
    [string]$EnvFile = ""
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "Q15Local.psm1") -Force
$root = Get-Q15RepoRoot
if (-not $EnvFile) { $EnvFile = Join-Path $root ".env.local" }
$script = Join-Path $root "scripts\local\Watch-Q15BackupSync.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument (
    "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`" " +
    "-EnvFile `"$EnvFile`""
)
$first = Get-Date -Second 30 -Millisecond 0
if ($first -le (Get-Date)) { $first = $first.AddMinutes(1) }
$trigger = New-ScheduledTaskTrigger `
    -Once -At $first -RepetitionInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 30)
Register-ScheduledTask `
    -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force `
    -Description "Pauses pending Q15 OneDrive archive sync before exact captures." |
    Out-Null
Write-Host "Registered: $TaskName every minute at second 30"
