# CLAUDE.md — agent guide

> **READ FIRST:** [`ENGINEERING_GUIDELINES.md`](ENGINEERING_GUIDELINES.md) is the
> standing engineering standard for this repo — read it before any code task. The
> SessionStart hook restates it and its rule list at the start of every session.
>
> **Operating standard (applies to every model):** work at maximum effort and
> rigor on every task, and use parallel subagents for substantive multi-step or
> fan-out work. See "Operating standard — effort and orchestration" at the top of
> the guidelines.

Read-only paper-trading monitor for Kalshi 15-minute crypto binaries
(BTC, ETH, SOL, XRP, DOGE, BNB, HYPE). It predicts YES/NO at the 15m / 10m / 7m
checkpoints, sends Telegram alerts, and learns from officially settled results.
**It never places, modifies, or cancels a real order.**

## Run / test
- Tests are the source of truth for behavior: `python3 -m pytest tests/ -q`.
  Add/adjust a test with every behavior change.
- App: Flask in `app.py` — dashboard at `/`, JSON under `/api/...`. Runs on Replit.
- Config is entirely env-driven (`Q15_*`); see `.env.example`. No secrets in code.

## Where things live (read these first for most tasks)
- `app.py` — wiring + the ~1s refresh loop that drives every subsystem.
- `routes/` — ALL HTTP routes (`api_core`, `api_v95_books`, `api_legacy`), extracted from
  `app.py`; endpoints/rules are frozen by `tests/test_route_table.py`.
- `notifications/telegram_client.py` — shared Telegram send mechanics for the three book
  senders (strategy_bots / high_vol_flip / ultoim_v2 adapters); the champion path
  (`notifier.py`/`outbox_v9.py`) is deliberately NOT on it.
- `tools/config_audit.py` — env-var inventory/`--check` (899 Q15_*/env reads; undocumented
  ones must be in `.env.example` or consciously baselined in `tools/config_baseline.json`).
- `analysis.py` — builds the per-asset snapshot (spot, candles, orderbook, target).
- `spot_client.py`, `q15_upgrade/market_data_v95.py` — price/flow feeds
  (Coinbase / Kraken / OKX / Deribit), with a bounded last-good spot fallback.
- `q15_upgrade/checkpoint_v95.py` — **the live decision engine**: `analyse_v95`
  (probability + edge), `build_v95_message` (the checkpoint alert), `run_cycle`.
  Start here for prediction / alerting logic.
- `q15_upgrade/ledger_v95.py` — **the learning system**: prediction ledger,
  Platt calibration, shadow challenger (global + per-regime), the scoreboard
  (accuracy / P&L / Wilson CIs by interval, rank, asset) and significance-tested
  promotion. SQLite at `data/`.
- `notifications/` — **all PURE Telegram code in one package**: `notifier.py` (delivery +
  `should_suppress_alert`), `outbox_v9.py` (retry outbox), `reporting.py` (hourly report),
  `panels_v95.py` (checkpoint/ranked/recap panels), `manipulation_alert.py`, `alert_config.py`.
  Two things stay outside it on purpose: the legacy `format_telegram_message` chain (welded
  into the FROZEN `q15_upgrade/checkpoint_v9{1..5}.py`) and the hourly-report builders in
  `q15_upgrade/{setup_miner,shadow_economics,accuracy_report}.py` (learning modules with a
  report method only).
- `q15_upgrade/window_focus.py` — two-window (15m/10m/7m) live controller:
  EV ranking, the cross-checkpoint side veto, checkpoint alerts, self-review.
- `performance.py`, `db.py` — Postgres `signals` store + settlement stats.

## Do NOT read these unless changing base behavior
`checkpoint_v91/v92/v93/v94/v94_unified/v94_adaptive15.py` are frozen legacy
layers (~7k lines). v95 subclasses the chain:
`v95 → v94_unified → v94 → v93 → v92 → v91`. They work; skip them otherwise.

## Single-branch policy (owner directive)
**The owner wants exactly ONE long-lived branch: `main`.** Do not accumulate
`claude/*` session branches. Concretely, every session:
- Ship work to `main` with `scripts/ship.sh "summary"` (it merges + pushes), then
  treat `main` as the source of truth. If the platform assigned this session its
  own `claude/*` branch, that branch should be pruned once the work is on `main`.
- Prune stale branches with `scripts/prune_branches.sh --all` (run on the Repl /
  anywhere the GitHub token can delete refs — the web sandbox's relay proxy
  returns 403 on ref deletion, so it can't prune from there).
- The per-session branch *name* is assigned by the Claude Code web environment,
  not the repo, so true "always use main" is set in the claude.ai/code
  environment config (development branch → `main`), not here.
- **Exception — `learning-snapshots` is a machine-managed data branch, not a
  session branch.** The Repl force-pushes the live learning ledgers there hourly
  (`tools/learning_export.py`) so reviews can pull real data. It is orphan/
  single-commit (no history bloat), invisible to the GitHub Relay (relay syncs
  only `main`), and the pruner ignores it (it only deletes `origin/claude/*`).
  Leave it alone. To audit against real data: `git fetch origin learning-snapshots`
  then read `learning_snapshot.json` / gunzip `dbs/*.sqlite3.gz` (see the
  `updated-review` skill, Step 1).

## Merge policy (standing authorization — every session)
The owner has authorized auto-merging finished work to `main` in **all** sessions:
once the task is done and the full suite passes (`python3 -m pytest tests/ -q`),
merge and push without asking each time. `main` is the deploy branch (GitHub Relay
syncs it to/from the Repl). Procedure:
1. `git fetch origin main` first (Relay pushes empty "Published your App" commits;
   fetching avoids non-FF surprises).
2. **Data-safety guard (the only gate): never let a merge delete existing data.**
   Check `git log --stat origin/main ^<branch>` for `main`-only commits that *add or
   modify content* (real data — not the empty "Published your App" commits). If any
   exist, preserve them: merge `origin/main` in first and verify the merge diff
   removes no `main`-only lines or files. If a merge would drop such data, STOP and
   tell the owner instead of merging.
3. **Update `HANDOFF.md` in the same change**, before merging: add/refresh a "Shipped
   THIS session" entry, bump the test count, and note the deploy-pending state. Keep
   the handoff current without being asked.
4. Merge with `--no-ff`, push `main`, then return to the working branch.
Only merge when tests are green and the data-safety guard passes; otherwise stay
on the branch and report why.

## Invariants (do not break)
- The prediction/alert path is read-only: the model chain never submits an order.
  **The one exception is `q15_upgrade/executor/`** — a separate, default-OFF, opt-in
  layer that places REAL Kalshi orders, triple-gated on `Q15_EXEC_ENABLED` (default
  false), `Q15_EXEC_DRY_RUN` (default true) and `Q15_EXEC_KILL`, with a second
  independently gated YES book (`Q15_EXEC_YES_*`). Never add an order-submission path
  anywhere else. The `place_order(` source guards in `tests/test_q15_v9*.py` cover the
  decision engine only — they cannot see the executor, so they are not proof the system
  cannot trade.
- Production "champion" model weights are FROZEN. Only the observational shadow
  challenger learns; promotion is manual and significance-tested.
- Telegram messages are HTML. Preserve suppression markers
  (`ENTRY RECOMMENDED` / `NO ENTRY YET`) and the `V9.5 CHECK` tag on checkpoint
  messages (the formatter and suppression both key on them).
- The canonical hourly report is detected by its `Hourly Report —` header so it
  bypasses the legacy reformatters; keep that header.
