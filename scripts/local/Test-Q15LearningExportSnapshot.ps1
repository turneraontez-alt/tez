param([string]$EnvFile = "")
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "Q15Local.psm1") -Force
$root = Get-Q15RepoRoot
Set-Location -LiteralPath $root
Import-Q15Env -EnvFile $EnvFile | Out-Null
Initialize-Q15LocalDirs
$python = Get-Q15Python
$py = @'
from datetime import datetime, timezone
import json
import os
import sys

sys.path.insert(0, os.getcwd())
from tools import learning_export as lx

data_dir = os.environ.get("LEARNING_EXPORT_DATA_DIR") or "data"
snapshot, artifacts = lx.build_snapshot(
    data_dir,
    now=datetime.now(timezone.utc),
    head_commit=None,
    build_info=None,
)
out = {
    "generated_at": snapshot.get("generated_at"),
    "database_count": len(snapshot.get("databases", {})),
    "artifact_count": len(artifacts),
    "scoreboards": sorted((snapshot.get("scoreboards") or {}).keys()),
}
print(json.dumps(out, sort_keys=True))
'@
$tmp = New-TemporaryFile
try {
    Set-Content -LiteralPath $tmp.FullName -Value $py -Encoding ASCII
    & $python $tmp.FullName
    if ($LASTEXITCODE -ne 0) { throw "learning export snapshot check failed" }
} finally {
    Remove-Item -LiteralPath $tmp.FullName -Force -ErrorAction SilentlyContinue
}
