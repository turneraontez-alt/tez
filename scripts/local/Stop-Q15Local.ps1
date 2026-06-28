param([string]$PidDir = "")
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "Q15Local.psm1") -Force
$root = Get-Q15RepoRoot
if (-not $PidDir) { $PidDir = Join-Path $root "work/local-run/pids" }
if (-not (Test-Path -LiteralPath $PidDir)) { Write-Host "No pid dir: $PidDir"; exit 0 }
Get-ChildItem -LiteralPath $PidDir -Filter "*.pid" | ForEach-Object {
    $name = $_.BaseName
    $pidValue = (Get-Content -LiteralPath $_.FullName -Raw).Trim()
    if ($pidValue) {
        $proc = Get-Process -Id ([int]$pidValue) -ErrorAction SilentlyContinue
        if ($proc) {
            Write-Host "stopping $name pid=$pidValue"
            Stop-Process -Id ([int]$pidValue) -Force
        }
    }
    Remove-Item -LiteralPath $_.FullName -Force
}
