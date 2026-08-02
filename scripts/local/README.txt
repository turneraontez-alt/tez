Local Windows runner scripts live in this directory.

Start-Q15Local.ps1 protects exact-13M evidence when it detects an already
running app.  It refuses a voluntary warm restart during the interval where
the required 60-second history plus the observed cold-start budget overlaps an
exact capture, and reports when to retry.  A genuinely stopped app still starts
immediately.  -ForceUnsafeRestart is an emergency-only override and will make
the affected exact window ineligible when history is incomplete.

Storage maintenance runs automatically before Start-Q15Local.ps1 launches the
services. Run it manually with:

  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\local\Optimize-Q15Storage.ps1

It compresses closed diagnostic JSONL files, keeps 14 days of those archives,
keeps the newest 7 local backup ZIPs, removes stale temporary backup files, and
warns if .git exceeds 5 GB. It never deletes or rewrites a live SQLite DB.

Create a consistent critical-data backup with:

  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\local\Backup-Q15LocalData.ps1

Backups use SQLite's online backup API, run PRAGMA quick_check, and retain the
newest 7 archives. The scheduled/default backup is critical-only so it fits
between protected captures: it includes the RTI strategy ledger, V22 REST
evidence, confirmation spool, delivery/order state, source/contracts/tests,
watchdog state, and future V22 audit artifacts. Add -IncludeAllState for all
non-rolling databases. Rolling Coinbase L2, Kraken L3, and spot-depth databases
remain excluded unless -IncludeHighVolume is also supplied. Secrets are excluded by
default; add -IncludeSecrets only when storing the ZIP in a protected location.
The support/ tree also preserves current Python/PowerShell source, tests,
immutable research configs, HANDOFF.md, watchdog state, and any V22 seal/audit
artifacts. It never includes .env.local unless -IncludeSecrets is explicit.
When OneDrive is available, the default destination is
`OneDrive\Documents\Q15 Critical Backups`, outside the AppData-backed `work`
junction, so losing the PC does not also lose every archive. Set
Q15_LOCAL_BACKUP_DIR to override this destination.
Install `Install-Q15BackupSyncGuard.ps1` to protect capture bandwidth while a
large archive is pending. It checks at second 30 every minute, pauses OneDrive
90 seconds before an exact capture through the +90 commit buffer, and resumes
only outside that guard. It does not stop OneDrive when the newest archive is
already synchronized.

Both storage scripts fail closed when a healthy collector is running and their
bounded runtime would overlap the exact parent/+30/+60 capture guard. The daily
defaults are deliberately placed in safe gaps: maintenance at 02:50 and the
critical backup at 03:20.

Before any manual test or maintenance job, check its expected duration with:

  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\local\Test-Q15CaptureGuard.ps1 -ExpectedWorkSeconds 120 -RequireSafe

It prints JSON and exits 75 when the bounded job would overlap a capture guard.

Install those daily tasks with:

  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\local\Install-Q15StorageTasks.ps1

Install the independent collector watchdog with:

  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\local\Install-Q15CollectorWatchdog.ps1 -BaselineMissedDeadlines 7

It checks the app, exact registration, delayed recorder, official REST workers,
settlement freshness, and the nonblocking Drift policy every minute. Two
consecutive failures are required before recovery. Recovery is deferred inside
the exact-capture guard and for 20 minutes after a restart, and always restores
dry-run/kill-switch defaults. The installed missed-deadline baseline is frozen;
an increase is reported as a failure and is never silently accepted.
