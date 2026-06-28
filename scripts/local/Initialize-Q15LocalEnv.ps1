param(
    [string]$Output = "",
    [switch]$Force,
    [switch]$PreserveTradingFlags
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "Q15Local.psm1") -Force
$root = Get-Q15RepoRoot
Set-Location -LiteralPath $root
if (-not $Output) { $Output = Join-Path $root ".env.local" }
if ((Test-Path -LiteralPath $Output) -and -not $Force) {
    throw "$Output already exists. Re-run with -Force to update non-secret values."
}

$secretKeys = @(
    "KALSHI_API_KEY_ID",
    "KALSHI_PRIVATE_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "GH_PUSH_TOKEN",
    "GITHUB_TOKEN",
    "DATABASE_URL"
)

$values = Read-Q15EnvFile -Path (Join-Path $root ".env.local.example")
$replit = Join-Path $root ".replit"
$inShared = $false
foreach ($line in Get-Content -LiteralPath $replit) {
    $trim = $line.Trim()
    if ($trim -eq "[userenv.shared]") {
        $inShared = $true
        continue
    }
    if ($inShared -and $trim.StartsWith("[")) { break }
    if (-not $inShared -or -not $trim -or $trim.StartsWith("#")) { continue }
    if ($trim -match '^\s*"?([^"=]+?)"?\s*=\s*"([^"]*)"\s*(#.*)?$') {
        $key = $Matches[1].Trim()
        $value = $Matches[2]
        if ($secretKeys -notcontains $key) {
            $values[$key] = $value
        }
    }
}

if (-not $PreserveTradingFlags) {
    $values["Q15_EXEC_DRY_RUN"] = "true"
    $values["Q15_EXEC_KILL"] = "true"
    $values["Q15_EXEC_YES_DRY_RUN"] = "true"
    $values["Q15_EXEC_YES_KILL"] = "true"
}

$lines = @(
    "# Generated local Windows env. Fill secrets manually; do not commit this file.",
    "# Generated at $(Get-Date -Format o)",
    ""
)
foreach ($key in $values.Keys) {
    $lines += "$key=$($values[$key])"
}
$lines | Set-Content -LiteralPath $Output -Encoding ASCII
Write-Host "Wrote $Output"
Write-Host "Secrets still need manual fill: $($secretKeys -join ', ')"
