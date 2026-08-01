param(
    [string]$TaskName = "Q15 Local Stack",
    [string]$EnvFile = "",
    [switch]$RunElevated
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "Q15Local.psm1") -Force
$root = Get-Q15RepoRoot
$script = Join-Path $root "scripts\local\Start-Q15Local.ps1"
if (-not $EnvFile) { $EnvFile = Join-Path $root ".env.local" }
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`" -EnvFile `"$EnvFile`" -SkipInstall"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$runLevel = if ($RunElevated) { "Highest" } else { "Limited" }
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel $runLevel
try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null
    Write-Host "Registered scheduled task: $TaskName"
    Write-Host "It starts at logon with run level: $runLevel. Use Stop-Q15Local.ps1 before disabling/removing it."
} catch {
    if ($RunElevated) { throw }
    $runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
    $command = "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`" -EnvFile `"$EnvFile`" -SkipInstall"
    New-Item -Path $runKey -Force | Out-Null
    New-ItemProperty -Path $runKey -Name $TaskName -PropertyType String -Value $command -Force | Out-Null
    Write-Host "Task Scheduler registration was unavailable; registered user logon startup: $TaskName"
    Write-Host "It starts at logon without elevation. Use Stop-Q15Local.ps1 before disabling/removing it."
}
