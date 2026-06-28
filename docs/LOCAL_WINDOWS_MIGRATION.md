# Local Windows Migration Runbook

This repo used Replit to run three long-lived workflows in parallel:

1. `python3.11 app.py` - Flask app plus refresh loop, websocket/REST market polling, spot depth collector, Telegram/reporting, V2/HVF/v3 bots, settlement reconciliation, and dry-run/live executor hooks.
2. `python3 tools/github_relay.py` - two-way GitHub sync for `main`.
3. `python3 tools/learning_export.py` - recurring learning snapshot push to the `learning-snapshots` branch.

The Windows local runner mirrors those three processes with PowerShell scripts in `scripts/local/`.

## Safety Defaults

Local startup is deliberately conservative. Unless `Start-Q15Local.ps1` is called with `-AllowLiveTrading`, it forces:

- `Q15_EXEC_DRY_RUN=true`
- `Q15_EXEC_KILL=true`
- `Q15_EXEC_YES_DRY_RUN=true`
- `Q15_EXEC_YES_KILL=true`

That means alerts, ledgers, v3 decisions, learning exports, and would-order logs can run, but no real order should be sent.

## One-Time Setup

```powershell
cd C:\Users\Turne\Documents\Codex\2026-06-26\i\work\tez-push-main
powershell -ExecutionPolicy Bypass -File scripts\local\Initialize-Q15LocalEnv.ps1
notepad .env.local
powershell -ExecutionPolicy Bypass -File scripts\local\Test-Q15LocalConfig.ps1
```

Fill these secrets in `.env.local` without committing them:

- `KALSHI_API_KEY_ID`
- `KALSHI_PRIVATE_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `GH_PUSH_TOKEN`
- optional `DATABASE_URL`

If `DATABASE_URL` is omitted, Postgres-backed signal persistence is disabled, but the SQLite learning/bot ledgers under `data/` still work.

## Start Locally

```powershell
powershell -ExecutionPolicy Bypass -File scripts\local\Start-Q15Local.ps1
```

Logs go to `work/local-run/logs/`. PIDs go to `work/local-run/pids/`.

To start without GitHub relay or learning export during the first smoke test:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\local\Start-Q15Local.ps1 -NoRelay -NoLearningExport
```

## Health Checks

After the app starts:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\local\Test-Q15LocalHealth.ps1
```

Manual URLs:

- `http://127.0.0.1:8000/api/health`
- `http://127.0.0.1:8000/api/q15-v3/scoreboard`
- `http://127.0.0.1:8000/api/q15-hvf/scoreboard`

Check learning-export snapshot generation without pushing:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\local\Test-Q15LearningExportSnapshot.ps1
```

## Stop Locally

```powershell
powershell -ExecutionPolicy Bypass -File scripts\local\Stop-Q15Local.ps1
```

## Back Up Local Data

```powershell
powershell -ExecutionPolicy Bypass -File scripts\local\Backup-Q15LocalData.ps1
```

Backups are zip files under `work/local-run/backups/`.

## Optional Windows Task Scheduler

Only install this after manual smoke tests pass:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\local\Install-Q15ScheduledTask.ps1
```

This registers a logon task named `Q15 Local Stack`.

## Cutover Checklist

1. Pull latest `main` locally.
2. Run `Initialize-Q15LocalEnv.ps1`, then fill secrets in `.env.local`.
3. Run `Test-Q15LocalConfig.ps1`.
4. Run unit tests with the local Python.
5. Start with `-NoRelay -NoLearningExport`.
6. Run `Test-Q15LocalHealth.ps1`.
7. Run `Test-Q15LearningExportSnapshot.ps1`.
8. Confirm Telegram routing: old V2 cards muted, v3 notifications enabled.
9. Confirm executor safety flags show dry-run and kill switches.
10. Start full local stack with relay/export.
11. Watch logs for at least several cycles.
12. Confirm learning snapshots advance from local export.
13. Only after local is stable, pause/disable Replit workflows.

## Rollback

If local fails:

1. Run `Stop-Q15Local.ps1`.
2. Leave `.env.local` intact for diagnosis.
3. Re-enable or keep Replit running.
4. Use the latest `work/local-run/backups/*.zip` if data rollback is needed.
5. Do not flip `-AllowLiveTrading` until dry-run logs, health, settlement grading, v3 scoreboard, and Telegram routing are proven stable.
