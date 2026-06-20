# Session handoff

Working notes so a fresh session resumes cheaply. See `CLAUDE.md` (architecture)
and `SYNC.md` (Replit sync). Live app: `phone-dashboard.replit.app` (Reserved VM,
`python3 app.py`). **The owner trades REAL money manually off the alerts**, so
reliability + honest data freshness matter more than new model features.

## 🔴 Immediate next step (where we stopped)
**Verify the `unified_loop` fix on the live Repl.** This session isolated and fixed
the dominant stall; the only thing left is to confirm it on real hardware (the
container can't reach `phone-dashboard.replit.app` — outbound is blocked here).

Diagnostic arc this session: 54s (PR #10) → ~18s residual → profiled `run_cycle`
into sub-stages (`parent_chain` ~44s dominates) → profiled `parent_chain` into
chain sub-stages (`unified_loop` ~33s dominates) → **fixed**: the 15M learner
(`Adaptive15LearningStore`) opened a fresh SQLite connection on every
`weights()` / `pattern_adjustment()` / `record_prediction()` call (~3 × 7 assets ×
every cycle). On Replit's networked `data/` disk the file-open is the dominant
cost. Now it reuses one persistent connection (`close()` no-op, `check_same_thread
=False`, serialized by the existing `self._lock`). No query/value/model change.

**To verify:** **Stop ▸ Run** on Replit (code is already on the dev branch / synced),
then read `/api/health`:
- `q15_v9_5.parent_chain_timing.unified_loop` should drop from ~33s toward ~0.
- `cycle_watchdog.slowest_stage` / `data_age_seconds` should fall under the **3s**
  freshness gate so `current_trade_decisions` stops being `{AVOID_INVALID_DATA: 7}`.
- If a residual remains, `parent_chain_timing` now names the next-largest sub-stage
  (`v94_own_work`, `v91_run_cycle`, `v91_pre_enrich`/`focus_update`/`calibrated_edge`/
  `finalize_all`) — chase that one next.

⚠️ Latent stalls of the same family (not yet triggered): the three OTHER settlement
reconcilers (`performance.reconcile`, `window_focus.reconcile_settlements`,
`learning.reconcile`) are still bound only by call-count (max 12), NOT wall-clock —
the pre-PR-#10 pattern that caused the 54s freeze. They run as their own watchdog
stages (`perf`/`focus_settlement`/`learning_reconcile`). If the watchdog ever names
one, port the PR-#10 wall-clock-budget pattern to it (leftover tickers retry next
cycle).

## Repo state
- Active dev branch `claude/festive-albattani-s1af1l` carries the whole arc and is
  pushed/even with origin. NOTE: in this container `main` is only the initial 3
  commits — the GitHub Relay / PR workflow described below is from the prior Replit
  setup; here all development lives on the dev branch. Latest commits:
  `parent_chain` sub-stage timing (`aeba348`) → SQLite connection reuse fix
  (`a35a8e1`) + `test_q15_v94_adaptive15_conn_reuse.py`.
- Tests: `python3 -m pytest tests/ -q` → **356 passed, 4 skipped**.
  ⚠️ `pytest` is NOT preinstalled in a fresh container — `pip install pytest -q` first.

## Shipped this session (all merged to main)
- **PR #8** — `sync.sh` (one-command Replit sync, no data loss); `spot_ws.py`
  optional low-latency spot feed (Coinbase/OKX websockets, default-OFF
  `Q15_SPOT_WS_ENABLED`, `get_spot` prefers fresh WS tick, per-asset REST
  fallback); `cycle_watchdog.py` (times every refresh stage, flags slow cycles in
  `/api/health.cycle_watchdog`); per-ticker Kalshi WS `websocket_book_ages`.
- **PR #9** — docs: reframed `SYNC.md` around the **GitHub Relay** (see below);
  `sync.sh` is now recovery-only.
- **PR #10** — fixed the **54s freeze**: `ledger_v95.reconcile_pending_from_market`
  did up to 12 sequential ~4.5s Kalshi `get_market` calls every 30s inside
  `run_cycle`. Added a wall-clock budget `Q15_V95_RECONCILE_BUDGET_SECONDS`
  (default 4s); leftover tickers retry next cycle.
- **PR #11** — **watchdog pager**: `cycle_watchdog.alert_message` sends a Telegram
  alert when a cycle exceeds `Q15_WATCHDOG_ALERT_SECONDS` (default 20s), with
  warmup + cooldown guards. So a freeze pages instead of going silent.

## Live runtime facts (from last /api/health)
- Kalshi WS **on** (`KALSHI_WS_ENABLED` + keys set) → `mode: ws-primary`,
  `websocket_book_ages` sub-second. Spot WS still OFF (`spot_ws.enabled=false`).
- `telegram_status: configured_outbox`, delivery healthy. Telegram is NOT the
  problem; the freeze is the prediction path (stale data → AVOID_INVALID_DATA).
- `actionable_alerts:false`, `min_settled_for_actionable:30`, only ~3 settled —
  strong ENTRY alerts are gated until ~30 markets settle (by design).

## New env flags (all optional)
`Q15_SPOT_WS_ENABLED` / `Q15_SPOT_WS_MAX_AGE_SECONDS` (3) ·
`Q15_CYCLE_WATCHDOG_SECONDS` (10) · `Q15_WATCHDOG_ALERT_ENABLED` (on) /
`_SECONDS` (20) / `_WARMUP_SECONDS` (60) / `_COOLDOWN_SECONDS` (600) ·
`Q15_V95_RECONCILE_BUDGET_SECONDS` (4).

## Workflow & sync
- Develop on `claude/beautiful-lamport-mrwnwm` → PR into `main` → merge when green
  (owner asked Claude to merge after tests pass). Commit identity:
  `user.email noreply@anthropic.com`, `user.name Claude`.
- **GitHub Relay** (`tools/github_relay.py`, runs on the Repl) auto-syncs `main`
  ⇄ Repl every ~20s (merge-based, never force-push). So after a merge the Repl
  gets code in ~20s — only **Stop ▸ Run** is needed to load it. ⚠️ It also pushes
  Repl-side commits straight to `main`, bypassing PR review (that's how
  `"Published your App"` / `"Saved progress"` commits appear on main). `./sync.sh`
  is now a destructive recovery override only.

## Roadmap remaining (owner-facing recommendation)
1. **Prove the edge** (highest value): turn `q15_upgrade/oos_v9.py` into a real
   backtest. The scoreboard is `3/3` — statistically meaningless. Don't size real
   money until the edge is demonstrated. *(Owner was offered this next.)*
3. **Trim cruft**: collapse the duplicated `q15_v9_1..v9_5` health blocks; retire
   dead layers. Heavy ~7k-line frozen stack is hard to trust with money.

## Invariants — do not break
- Read-only; nothing places a real exchange order (the human trades manually).
- Champion weights FROZEN; only shadow challenger learns; promotion manual +
  significance-tested. Gate model-behavior changes behind default-OFF `Q15_*` flags.
- Keep `V9.5 CHECK` + `ENTRY RECOMMENDED`/`NO ENTRY YET` markers in checkpoint
  messages; keep the `Hourly Report —` header. Keep `.env.example` free of real
  secret-scanner patterns. Don't edit the frozen `checkpoint_v91..v94*` chain.

## Gotchas
- Data is sparse until markets settle — don't tune on tiny samples.
- `MarketResultCache` (`market_cache.py`) caches only resolved markets; all four
  settlement reconcilers go through it. Unresolved markets are re-fetched live
  every cycle (the root of the reconcile-stall family of bugs).
