param(
    [string]$PidDir = "",
    [switch]$IncludeStale
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "Q15Local.psm1") -Force
$root = Get-Q15RepoRoot
if (-not $PidDir) { $PidDir = Join-Path $root "work/local-run/pids" }

$stopped = @{}

function Stop-Q15ProcessById {
    param(
        [int]$ProcessId,
        [string]$Name
    )
    if ($ProcessId -le 0 -or $stopped.ContainsKey($ProcessId)) { return }
    Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcessId" -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Q15ProcessById -ProcessId ([int]$_.ProcessId) -Name "$Name-child" }
    $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($proc) {
        Write-Host "stopping $Name pid=$ProcessId"
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
        $stopped[$ProcessId] = $true
    }
}

if (Test-Path -LiteralPath $PidDir) {
    Get-ChildItem -LiteralPath $PidDir -Filter "*.pid" | ForEach-Object {
        $name = $_.BaseName
        $pidValue = (Get-Content -LiteralPath $_.FullName -Raw).Trim()
        if ($pidValue) {
            Stop-Q15ProcessById -ProcessId ([int]$pidValue) -Name $name
        }
        Remove-Item -LiteralPath $_.FullName -Force
    }
}

if ($IncludeStale) {
    $serviceScripts = @(
        (Join-Path $root "app.py"),
        (Join-Path $root "tools/github_relay.py"),
        (Join-Path $root "tools/learning_export.py")
    )
    $escapedScripts = $serviceScripts | ForEach-Object { [regex]::Escape($_) }
    $repoPython = Join-Path $root ".venv\Scripts\python.exe"
    $serviceNamePattern = "(^|[\s`"])(app\.py|tools[\\/](github_relay|learning_export)\.py)([\s`"]|$)"

    Get-CimInstance Win32_Process | Where-Object {
        $cmd = [string]$_.CommandLine
        $exe = [string]$_.ExecutablePath
        $byAbsoluteScript = $false
        foreach ($script in $escapedScripts) {
            if ($cmd -match $script) {
                $byAbsoluteScript = $true
                break
            }
        }
        $byRepoPython = $exe -and $repoPython -and ($exe -ieq $repoPython) -and ($cmd -match $serviceNamePattern)
        $byAbsoluteScript -or $byRepoPython
    } | ForEach-Object {
        Stop-Q15ProcessById -ProcessId ([int]$_.ProcessId) -Name "stale-local-service"
    }

    try {
        $port = Get-Q15Port
        Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique |
            ForEach-Object { Stop-Q15ProcessById -ProcessId ([int]$_) -Name "q15-port-$port" }
    } catch {
        Write-Host "port cleanup skipped: $($_.Exception.Message)"
    }
}
