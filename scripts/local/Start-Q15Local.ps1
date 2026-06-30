param(
    [string]$EnvFile = "",
    [switch]$SkipInstall,
    [switch]$NoRelay,
    [switch]$NoLearningExport,
    [switch]$AllowLiveTrading
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "Q15Local.psm1") -Force
$root = Get-Q15RepoRoot
Set-Location -LiteralPath $root
Import-Q15Env -EnvFile $EnvFile | Out-Null
Initialize-Q15LocalDirs
Add-Q15LocalToolPath
Set-Q15SafeTradingDefaults -AllowLiveTrading:$AllowLiveTrading
$python = Get-Q15Python
if (-not $SkipInstall) {
    & $python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
}
$logRel = if ($env:Q15_LOCAL_LOG_DIR) { $env:Q15_LOCAL_LOG_DIR } else { "work/local-run/logs" }
$pidRel = if ($env:Q15_LOCAL_PID_DIR) { $env:Q15_LOCAL_PID_DIR } else { "work/local-run/pids" }
$logDir = Join-Path $root $logRel
$pidDir = Join-Path $root $pidRel
New-Item -ItemType Directory -Force -Path $logDir,$pidDir | Out-Null
$stopScript = Join-Path $PSScriptRoot "Stop-Q15Local.ps1"
if (Test-Path -LiteralPath $stopScript) {
    & $stopScript -PidDir $pidDir -IncludeStale
}
$services = @(
    @{ Name = "app"; Args = @((Join-Path $root "app.py")) }
)
if (-not $NoRelay) { $services += @{ Name = "github-relay"; Args = @((Join-Path $root "tools/github_relay.py")) } }
if (-not $NoLearningExport) { $services += @{ Name = "learning-export"; Args = @((Join-Path $root "tools/learning_export.py")) } }
foreach ($svc in $services) {
    $out = Join-Path $logDir ($svc.Name + ".out.log")
    $err = Join-Path $logDir ($svc.Name + ".err.log")
    $proc = Start-Process -FilePath $python -ArgumentList $svc.Args -WorkingDirectory $root -RedirectStandardOutput $out -RedirectStandardError $err -PassThru -WindowStyle Hidden
    Set-Content -LiteralPath (Join-Path $pidDir ($svc.Name + ".pid")) -Value $proc.Id -Encoding ASCII
    Write-Host ("started {0} pid={1}" -f $svc.Name, $proc.Id)
}
Write-Host ("health: http://127.0.0.1:{0}/api/health" -f (Get-Q15Port))
Write-Host "live trading safety: Q15_EXEC_DRY_RUN=$env:Q15_EXEC_DRY_RUN Q15_EXEC_KILL=$env:Q15_EXEC_KILL Q15_EXEC_YES_DRY_RUN=$env:Q15_EXEC_YES_DRY_RUN Q15_EXEC_YES_KILL=$env:Q15_EXEC_YES_KILL"
