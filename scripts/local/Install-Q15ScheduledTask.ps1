param(
    [string]$TaskName = "Q15 Local Stack",
    [string]$EnvFile = ""
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "Q15Local.psm1") -Force
$root = Get-Q15RepoRoot
$script = Join-Path $root "scripts\local\Start-Q15Local.ps1"
if (-not $EnvFile) { $EnvFile = Join-Path $root ".env.local" }
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`" -EnvFile `"$EnvFile`" -SkipInstall"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null
Write-Host "Registered scheduled task: $TaskName"
Write-Host "It starts at logon. Use Stop-Q15Local.ps1 before disabling/removing it."
