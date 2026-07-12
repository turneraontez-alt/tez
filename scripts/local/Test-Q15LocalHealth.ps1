param(
    [string]$EnvFile = "",
    [int]$ScoreboardTimeoutSec = 60
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "Q15Local.psm1") -Force
$root = Get-Q15RepoRoot
Set-Location -LiteralPath $root
Import-Q15Env -EnvFile $EnvFile | Out-Null
$port = Get-Q15Port
$urls = @(
    "http://127.0.0.1:$port/api/health",
    "http://127.0.0.1:$port/api/q15-v3/scoreboard",
    "http://127.0.0.1:$port/api/q15-hvf/scoreboard"
)
foreach ($url in $urls) {
    try {
        $timeoutSec = if ($url -like "*/api/health") { 12 } else { $ScoreboardTimeoutSec }
        $res = Invoke-RestMethod -Uri $url -TimeoutSec $timeoutSec
        Write-Host "OK $url"
        if ($url -like "*/api/health") {
            Write-Host "  status=$($res.status) persistence=$($res.persistence) mode=$($res.mode)"
        }
        if ($url -like "*/q15-v3/*") {
            Write-Host "  v3 rows=$($res.total_rows) resolved=$($res.resolved)"
        }
    } catch {
        Write-Host "FAIL $url :: $($_.Exception.Message)"
        exit 1
    }
}
