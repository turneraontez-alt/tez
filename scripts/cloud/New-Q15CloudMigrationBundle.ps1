param(
    [string]$Destination = "",
    [switch]$StopLocal
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Import-Module (Join-Path $PSScriptRoot "..\local\Q15Local.psm1") -Force
$root = Get-Q15RepoRoot
if (-not $Destination) {
    $Destination = Join-Path $root "work\cloud-migration"
}
New-Item -ItemType Directory -Force -Path $Destination | Out-Null

if ($StopLocal) {
    & (Join-Path $root "scripts\local\Stop-Q15Local.ps1") -IncludeStale
}

$python = Get-Q15Python
$backupTool = Join-Path $root "tools\local_backup.py"
$archive = (& $python $backupTool --repo $root --destination $Destination --include-high-volume |
    Select-Object -Last 1).Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $archive)) {
    throw "Q15 migration backup failed"
}

$metadata = [ordered]@{
    created_at = (Get-Date).ToUniversalTime().ToString("o")
    archive = (Split-Path -Leaf $archive)
    sha256 = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    source_commit = (& git -C $root rev-parse HEAD).Trim()
    local_stopped = [bool]$StopLocal
    includes_secrets = $false
}
$metadataPath = Join-Path $Destination "migration-info.json"
$metadata | ConvertTo-Json | Set-Content -LiteralPath $metadataPath -Encoding UTF8

Write-Host "Migration database bundle: $archive"
Write-Host "Metadata: $metadataPath"
Write-Host "Secrets are not included. Transfer .env.local separately and protect it as mode 0640."
if (-not $StopLocal) {
    Write-Warning "This is a preflight snapshot. Use -StopLocal for the final cutover snapshot."
}
