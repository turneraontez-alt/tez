param([string]$BackupDir = "")
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "Q15Local.psm1") -Force
$root = Get-Q15RepoRoot
if (-not $BackupDir) { $BackupDir = Join-Path $root "work/local-run/backups" }
New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$dest = Join-Path $BackupDir "q15-data-$stamp.zip"
$items = @()
if (Test-Path -LiteralPath (Join-Path $root "data")) { $items += (Join-Path $root "data") }
if (Test-Path -LiteralPath (Join-Path $root ".env.local")) { $items += (Join-Path $root ".env.local") }
if (-not $items) { throw "Nothing to back up" }
Compress-Archive -LiteralPath $items -DestinationPath $dest -Force
Write-Host "Wrote backup: $dest"
