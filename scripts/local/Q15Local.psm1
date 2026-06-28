Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-Q15RepoRoot {
    return (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
}

function Read-Q15EnvFile {
    param([string]$Path)
    $values = [ordered]@{}
    if (-not (Test-Path -LiteralPath $Path)) { return $values }
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trim = $line.Trim()
        if (-not $trim -or $trim.StartsWith('#')) { continue }
        $idx = $trim.IndexOf('=')
        if ($idx -lt 1) { continue }
        $key = $trim.Substring(0, $idx).Trim()
        $value = $trim.Substring($idx + 1).Trim()
        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $values[$key] = $value
    }
    return $values
}

function Import-Q15Env {
    param([string]$EnvFile)
    $root = Get-Q15RepoRoot
    if (-not $EnvFile) { $EnvFile = Join-Path $root ".env.local" }
    if (-not (Test-Path -LiteralPath $EnvFile)) {
        throw "Missing env file: $EnvFile. Copy .env.local.example to .env.local and fill secrets."
    }
    $values = Read-Q15EnvFile -Path $EnvFile
    foreach ($key in $values.Keys) {
        [Environment]::SetEnvironmentVariable($key, [string]$values[$key], 'Process')
    }
    return $values
}

function Get-Q15Python {
    $root = Get-Q15RepoRoot
    if ($env:Q15_LOCAL_PYTHON -and (Test-Path -LiteralPath $env:Q15_LOCAL_PYTHON)) { return $env:Q15_LOCAL_PYTHON }
    $candidates = @(
        (Join-Path $root ".venv\Scripts\python.exe"),
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Python\pythoncore-3.11-64\python.exe",
        "C:\Python311\python.exe",
        "C:\Users\Turne\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe",
        "python"
    )
    foreach ($candidate in $candidates) {
        if ($candidate -eq "python") { return $candidate }
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return "python"
}

function Add-Q15LocalToolPath {
    $paths = @(
        "C:\Users\Turne\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\git\cmd",
        "$env:LOCALAPPDATA\Programs\Python\Python311",
        "$env:LOCALAPPDATA\Programs\Python\Python311\Scripts",
        "$env:LOCALAPPDATA\Python\pythoncore-3.11-64",
        "$env:LOCALAPPDATA\Python\pythoncore-3.11-64\Scripts",
        "C:\Python311",
        "C:\Python311\Scripts"
    )
    foreach ($path in $paths) {
        if ((Test-Path -LiteralPath $path) -and (($env:PATH -split ';') -notcontains $path)) {
            $env:PATH = "$path;$env:PATH"
        }
    }
}

function Initialize-Q15LocalDirs {
    $root = Get-Q15RepoRoot
    $dirs = @("data", "work\local-run", "work\local-run\logs", "work\local-run\pids", "work\local-run\backups")
    foreach ($dir in $dirs) { New-Item -ItemType Directory -Force -Path (Join-Path $root $dir) | Out-Null }
}

function Set-Q15SafeTradingDefaults {
    param([switch]$AllowLiveTrading)
    if ($AllowLiveTrading) { return }
    $env:Q15_EXEC_DRY_RUN = "true"
    $env:Q15_EXEC_KILL = "true"
    $env:Q15_EXEC_YES_DRY_RUN = "true"
    $env:Q15_EXEC_YES_KILL = "true"
}

function Get-Q15Port {
    if ($env:PORT) { return [int]$env:PORT }
    return 8000
}
