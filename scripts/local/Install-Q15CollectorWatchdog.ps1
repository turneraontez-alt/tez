param(
    [string]$TaskName = "Q15 Collector Watchdog",
    [string]$EnvFile = "",
    [ValidateRange(0, [int]::MaxValue)]
    [int]$BaselineMissedDeadlines = 7,
    [ValidateRange(1, 15)]
    [int]$IntervalMinutes = 1
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "Q15Local.psm1") -Force

$root = Get-Q15RepoRoot
$watchdog = Join-Path $root "scripts\local\Watch-Q15Collector.ps1"
if (-not $EnvFile) { $EnvFile = Join-Path $root ".env.local" }

$arguments = (
    "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"{0}`" " +
    "-EnvFile `"{1}`" -BaselineMissedDeadlines {2}"
) -f $watchdog,$EnvFile,$BaselineMissedDeadlines
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments
$trigger = New-ScheduledTaskTrigger `
    -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description (
        "Capture-safe external Q15 collector watchdog; frozen missed-deadline baseline={0}." -f
        $BaselineMissedDeadlines
    ) `
    -Force | Out-Null

Write-Host "Registered: $TaskName every $IntervalMinutes minute(s)"
Write-Host "Frozen exact missed-deadline baseline: $BaselineMissedDeadlines"
Write-Host "The task never restarts inside the exact-capture guard and always restores trading kill switches."
