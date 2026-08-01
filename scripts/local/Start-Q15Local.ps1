param(
    [string]$EnvFile = "",
    [switch]$SkipInstall,
    [switch]$NoRelay,
    [switch]$NoLearningExport,
    [switch]$AllowLiveTrading,
    [switch]$ForceUnsafeRestart
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
$appPidPath = Join-Path $pidDir "app.pid"
$healthyAppRunning = $false
if (Test-Path -LiteralPath $appPidPath) {
    try {
        $existingAppPid = [int](
            (Get-Content -LiteralPath $appPidPath -Raw).Trim()
        )
        $existingApp = Get-Process -Id $existingAppPid -ErrorAction Stop
        $healthyAppRunning = -not $existingApp.HasExited
    }
    catch {
        $healthyAppRunning = $false
    }
}
if ($healthyAppRunning -and -not $ForceUnsafeRestart) {
    $restartWindow = Get-Q15ExactCaptureRestartWindow
    if ($restartWindow.protected) {
        # NOTE the parentheses around the concatenation: -f binds TIGHTER than +,
        # so without them the format operator consumed only the final fragment
        # (which has no placeholders) and the message printed a literal
        # "{0}; next capture in {1}s. Retry in {2}s" — telling the operator
        # nothing about how long to wait, in exactly the situation where they
        # need it.
        throw ((
            "Refusing to restart healthy Q15 app inside the exact-capture " +
            "protection window ({0}; next capture in {1}s). Retry in {2}s " +
            "or use -ForceUnsafeRestart only for an emergency recovery."
        ) -f
            $restartWindow.reason,
            $restartWindow.seconds_until_capture,
            $restartWindow.retry_after_seconds
        )
    }
}
$stopScript = Join-Path $PSScriptRoot "Stop-Q15Local.ps1"
if (Test-Path -LiteralPath $stopScript) {
    & $stopScript -PidDir $pidDir -IncludeStale
}
$storageScript = Join-Path $PSScriptRoot "Optimize-Q15Storage.ps1"
if (Test-Path -LiteralPath $storageScript) {
    & $storageScript
}
$services = @(
    @{ Name = "app"; Args = @("app.py") }
)
if (-not $NoRelay) { $services += @{ Name = "github-relay"; Args = @("tools/github_relay.py") } }
if (-not $NoLearningExport) { $services += @{ Name = "learning-export"; Args = @("tools/learning_export.py") } }
foreach ($svc in $services) {
    $out = Join-Path $logDir ($svc.Name + ".out.log")
    $err = Join-Path $logDir ($svc.Name + ".err.log")
    $proc = Start-Process -FilePath $python -ArgumentList $svc.Args -WorkingDirectory $root -RedirectStandardOutput $out -RedirectStandardError $err -PassThru -WindowStyle Hidden
    Set-Content -LiteralPath (Join-Path $pidDir ($svc.Name + ".pid")) -Value $proc.Id -Encoding ASCII
    Write-Host ("started {0} pid={1}" -f $svc.Name, $proc.Id)
}
Write-Host ("health: http://127.0.0.1:{0}/api/health" -f (Get-Q15Port))
Write-Host "live trading safety: Q15_EXEC_DRY_RUN=$env:Q15_EXEC_DRY_RUN Q15_EXEC_KILL=$env:Q15_EXEC_KILL Q15_EXEC_YES_DRY_RUN=$env:Q15_EXEC_YES_DRY_RUN Q15_EXEC_YES_KILL=$env:Q15_EXEC_YES_KILL"
