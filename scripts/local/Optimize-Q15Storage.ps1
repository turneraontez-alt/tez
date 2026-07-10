param(
    [int]$KeepBackups = 7,
    [double]$DiagnosticRetentionDays = 14
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "Q15Local.psm1") -Force
$root = Get-Q15RepoRoot
$python = Get-Q15Python
$tool = Join-Path $root "tools/storage_maintenance.py"
& $python $tool --repo $root --keep-backups $KeepBackups --diagnostic-days $DiagnosticRetentionDays
if ($LASTEXITCODE -ne 0) { throw "Q15 storage maintenance failed" }
