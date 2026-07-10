param(
    [string]$MaintenanceTaskName = "Q15 Storage Maintenance",
    [string]$BackupTaskName = "Q15 Critical Data Backup",
    [string]$MaintenanceTime = "02:45",
    [string]$BackupTime = "03:00"
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "Q15Local.psm1") -Force
$root = Get-Q15RepoRoot
$maintenance = Join-Path $root "scripts\local\Optimize-Q15Storage.ps1"
$backup = Join-Path $root "scripts\local\Backup-Q15LocalData.ps1"
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4)

$maintenanceAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument (
    "-NoProfile -ExecutionPolicy Bypass -File `"$maintenance`""
)
$maintenanceTrigger = New-ScheduledTaskTrigger -Daily -At $MaintenanceTime
Register-ScheduledTask -TaskName $MaintenanceTaskName -Action $maintenanceAction `
    -Trigger $maintenanceTrigger -Principal $principal -Settings $settings -Force | Out-Null

$backupAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument (
    "-NoProfile -ExecutionPolicy Bypass -File `"$backup`" -Keep 7"
)
$backupTrigger = New-ScheduledTaskTrigger -Daily -At $BackupTime
Register-ScheduledTask -TaskName $BackupTaskName -Action $backupAction `
    -Trigger $backupTrigger -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Registered: $MaintenanceTaskName daily at $MaintenanceTime"
Write-Host "Registered: $BackupTaskName daily at $BackupTime (newest 7 retained)"
