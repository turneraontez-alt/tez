param(
    [int]$ExpectedWorkSeconds = 0,
    [long]$NowEpochSeconds = -1,
    [switch]$RequireSafe
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "Q15Local.psm1") -Force

$now = if ($NowEpochSeconds -ge 0) {
    [DateTimeOffset]::FromUnixTimeSeconds($NowEpochSeconds)
}
else {
    [DateTimeOffset]::UtcNow
}
$state = Get-Q15ExactCaptureWorkWindow `
    -Now $now `
    -ExpectedWorkSeconds $ExpectedWorkSeconds
$state | ConvertTo-Json -Compress
if ($RequireSafe -and $state.protected) { exit 75 }
