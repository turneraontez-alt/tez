# CLAUDE.md — agent guide

> **READ FIRST — every session, before any other file:**
> [`ENGINEERING_GUIDELINES.md`](ENGINEERING_GUIDELINES.md) is the staff-engineer
> meta-prompt and the standing engineering standard for this repo. Read it before
> starting any code task and apply its substantive rules (failure modes,
> idempotency, stale-feed/WebSocket handling, `Decimal` for prices, parameterized
> SQL, narrow exception handling, deterministic tests, adversarial self-review).

Read-only paper-trading monitor for Kalshi 15-minute crypto binaries
(BTC, ETH, SOL, XRP, DOGE, BNB, HYPE). It predicts YES/NO at the 15m / 10m / 7m
checkpoints, sends Telegram alerts, and learns from officially settled results.
**It never places, modifies, or cancels a real order.**

## Run / test
- Tests are the source of truth for behavior: `python3 -m pytest tests/ -q`
  (31 test files). Add/adjust a test with every behavior change.
- App: Flask in `app.py` — dashboard at `/`, JSON under `/api/...`. Runs on Replit.
- Config is entirely env-driven (`Q15_*`); see `.env.example`. No secrets in code.

## Where things live (read these first for most tasks)
- `app.py` — Flask routes + the ~1s refresh loop that drives every subsystem.
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
- `telegram/` — **all PURE Telegram code in one package**: `notifier.py` (delivery +
  `should_suppress_alert`), `outbox_v9.py` (reliable retry outbox), `reporting.py`
  (hourly report), `panels_v95.py` (checkpoint/ranked/recap panels),
  `manipulation_alert.py`, `alert_config.py`. NOTE: the legacy `format_telegram_message`
  reformatter chain still lives in the FROZEN `q15_upgrade/checkpoint_v9{1..5}.py` (its
  formatting is welded into frozen decision logic — can't be moved without editing frozen
  code). The hourly-report builders `q15_upgrade/{setup_miner,shadow_economics,
  accuracy_report}.py` are learning/analysis modules (a report method only) so they stay.
- `q15_upgrade/window_focus.py` — two-window (15m/10m/7m) live controller:
  EV ranking, the cross-checkpoint side veto, checkpoint alerts, self-review.
- `performance.py`, `db.py` — Postgres `signals` store + settlement stats.

## Do NOT read these unless changing base behavior
`checkpoint_v91/v92/v93/v94/v94_unified/v94_adaptive15.py` are frozen legacy
layers (~7k lines). v95 subclasses the chain:
`v95 → v94_unified → v94 → v93 → v92 → v91`. They work; skip them otherwise.

## Merge policy (standing authorization — every session)
The owner has authorized auto-merging finished work to `main` in **all** sessions.
After completing a task and confirming the full suite passes
(`python3 -m pytest tests/ -q`), merge the working branch into `main` and push —
no need to ask each time. `main` is the deploy branch (GitHub Relay syncs it
to/from the Repl). Procedure:
1. `git fetch origin main` first (Relay pushes empty "Published your App" commits
   to `main`; fetching avoids non-FF surprises).
2. **Data-safety guard (the only gate): never let a merge delete existing data.**
   Inspect `git log --stat origin/main ^<branch>` for commits that exist only on
   `main`. If any of them *add or modify file content* (new data — not the empty
   "Published your App" commits), the merge MUST preserve it: merge `origin/main`
   in first / resolve keeping that content, and verify the merge diff removes no
   `main`-only lines or files. If a merge would drop data that exists only on
   `main`, STOP and tell the owner instead of merging.
3. **Update `HANDOFF.md` as part of the same change**, before merging: add/refresh
   a "Shipped THIS session" entry for the work, bump the test count, and note the
   deploy-pending state. The handoff is kept current automatically — don't wait to
   be asked.
4. Merge with `--no-ff` and push `main`, then return to the working branch.
Only merge when tests are green and the data-safety guard passes; otherwise stay
on the branch and report why.

## Invariants (do not break)
- Read-only. Nothing touches a real exchange order.
- Production "champion" model weights are FROZEN. Only the observational shadow
  challenger learns; promotion is manual and significance-tested.
- Telegram messages are HTML. Preserve suppression markers
  (`ENTRY RECOMMENDED` / `NO ENTRY YET`) and the `V9.5 CHECK` tag on checkpoint
  messages (the formatter and suppression both key on them).
- The canonical hourly report is detected by its `Hourly Report —` header so it
  bypasses the legacy reformatters; keep that header.
