Local Windows runner scripts live in this directory.

Storage maintenance runs automatically before Start-Q15Local.ps1 launches the
services. Run it manually with:

  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\local\Optimize-Q15Storage.ps1

It compresses closed diagnostic JSONL files, keeps 14 days of those archives,
keeps the newest 7 local backup ZIPs, removes stale temporary backup files, and
warns if .git exceeds 5 GB. It never deletes or rewrites a live SQLite DB.

Create a consistent critical-data backup with:

  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\local\Backup-Q15LocalData.ps1

Backups use SQLite's online backup API, run PRAGMA quick_check, and retain the
newest 7 archives. Rolling Coinbase L2, Kraken L3, and spot-depth databases are
excluded by default because they are large and reproducible. Add
-IncludeHighVolume for a full collector archive. Secrets are excluded by
default; add -IncludeSecrets only when storing the ZIP in a protected location.

Install daily maintenance (02:45) and critical backup (03:00) tasks with:

  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\local\Install-Q15StorageTasks.ps1
