param([string]$EnvFile = "")
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "Q15Local.psm1") -Force
$root = Get-Q15RepoRoot
Set-Location -LiteralPath $root
$values = Import-Q15Env -EnvFile $EnvFile
Add-Q15LocalToolPath
Initialize-Q15LocalDirs
Set-Q15SafeTradingDefaults
$requiredSecrets = @("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GH_PUSH_TOKEN")
$checks = foreach ($key in $requiredSecrets) {
    $value = [Environment]::GetEnvironmentVariable($key, 'Process')
    $present = [bool]$value -and $value -notmatch '^<.*>$'
    [pscustomobject]@{ Name = $key; Present = $present }
}
$python = Get-Q15Python
$pyCheck = @'
import importlib, json, os, sys
mods = ["flask", "requests", "websockets", "cryptography", "psycopg2"]
out = {m: True for m in mods}
for m in mods:
    try: importlib.import_module(m)
    except Exception as exc: out[m] = f"missing: {type(exc).__name__}: {exc}"
print(json.dumps(out, sort_keys=True))
'@
$tmp = New-TemporaryFile
try {
    Set-Content -LiteralPath $tmp.FullName -Value $pyCheck -Encoding ASCII
    & $python $tmp.FullName
    if ($LASTEXITCODE -ne 0) { throw "python dependency check failed" }
} finally {
    Remove-Item -LiteralPath $tmp.FullName -Force -ErrorAction SilentlyContinue
}
Write-Host "Secrets present:"
$checks | Format-Table -AutoSize
Write-Host "Safety flags: Q15_EXEC_DRY_RUN=$env:Q15_EXEC_DRY_RUN Q15_EXEC_KILL=$env:Q15_EXEC_KILL Q15_EXEC_YES_DRY_RUN=$env:Q15_EXEC_YES_DRY_RUN Q15_EXEC_YES_KILL=$env:Q15_EXEC_YES_KILL"
Write-Host "Loaded env keys: $($values.Count)"
