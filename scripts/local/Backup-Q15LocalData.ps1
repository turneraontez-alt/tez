param(
    [string]$BackupDir = "",
    [int]$Keep = 7,
    [switch]$IncludeHighVolume,
    [switch]$IncludeSecrets
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "Q15Local.psm1") -Force
$root = Get-Q15RepoRoot
if (-not $BackupDir) { $BackupDir = Join-Path $root "work/local-run/backups" }
New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
$python = Get-Q15Python
$tool = Join-Path $root "tools/local_backup.py"
$toolArgs = @($tool, "--repo", $root, "--destination", $BackupDir)
if ($IncludeHighVolume) { $toolArgs += "--include-high-volume" }
if ($IncludeSecrets) { $toolArgs += "--include-secrets" }
$dest = (& $python @toolArgs | Select-Object -Last 1).Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $dest)) {
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
Write-Host "Retention: newest $Keep backup(s); high-volume collectors included=$([bool]$IncludeHighVolume)"
