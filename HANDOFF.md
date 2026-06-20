# Session handoff

Working notes so a fresh session resumes cheaply. See `CLAUDE.md` (architecture)
and `SYNC.md` (Replit sync). Live app: `phone-dashboard.replit.app` (Reserved VM,
`python3 app.py`). **The owner trades REAL money manually off the alerts**, so
reliability + honest data freshness matter more than new model features.

## 🔴 Immediate next step (where we stopped)
**Cycle time is fixed; we're now fighting the last few seconds to clear the 3s
data-age gate.** Connection-reuse + autocommit + a v91 round-trip cut are all
shipped and **verified live**. The remaining decision is owner-facing (bottom).

**VERIFIED LIVE (redeploy 08:32, ~74min uptime, steady-state):**
- `run_cycle_timing.total` **~17.5s → 6.19s** ✅
- `v95_analysis` **3.9s → 1.79s** ✅ (ledger conn-reuse)
- `parent_chain` **12.6s → 3.98s** ✅
- worst warmup cycle **75s → 6.9s** ✅ (no more cold-start freeze)
- `data_age_seconds` **~38s → 7.41s** ⬇ — BUT still over the **3s** gate, so
  `current_trade_decisions` is still `{AVOID_INVALID_DATA: 7}`.

Diagnostic arc: 54s (PR #10) → ~18s → profiled `run_cycle` → `parent_chain` →
`unified_loop` (15M-learner SQLite file-opens) → connection-reuse everywhere →
v91 round-trip cut. The dominant remaining cost is `parent_chain` (3.98s), most
of which is **frozen v91 `pre_enrich`** Postgres round-trips.

**Shipped (all behavior-preserving, 367 tests pass):**
1. **V9.5 ledger** (`ledger_v95.py`) — persistent SQLite connection.
2. **V9.4 context cache** (`checkpoint_v94.py`) — persistent SQLite connection +
   `self._cache_db_lock`.
3. **Telegram gate** (`checkpoint_v94_unified.py`) — persistent SQLite connection.
4. **Postgres autocommit** (`db.py` `SignalStore._conn`) — removes the separate
   COMMIT round-trip app-wide (also makes the v91 write→read below visible across
   pooled connections without an explicit commit).
5. **15M learner** (`checkpoint_v94_adaptive15.py`) — persistent SQLite connection.
6. **v91 round-trip cut** (`checkpoint_v91.py`, owner-approved frozen-chain edit) —
   `pre_enrich_all` was **6 Postgres round-trips/asset** (`insert_observation` +
   `recent_observations` + `freeze_prediction` *with its read-back* + 2×
   `get_prediction`). Now **4**: split the write (`write_prediction`) from the
   read-back, and collapse the two per-checkpoint reads into ONE batched
   `get_predictions_for(contract, asset, ("15M","10M"))`. No query *result*,
   value, or model-behavior change — `frozen`/`fifteen`/`ten` are identical to
   before (verified by `test_q15_v91_round_trips.py` + the unchanged v91–v94 suite).
Tests: `test_q15_v95_ledger_conn_reuse`, `test_q15_v94_context_cache_conn_reuse`,
`test_q15_v94_gate_conn_reuse`, `test_db_autocommit`, `test_q15_v91_round_trips`.

**STILL OVER THE 3s GATE after all the above — remaining levers (owner's call):**
- **Raise `Q15_*` `max_data_age_s`** from 3s to ~8s (env/config, no code) → a 6.2s
  cycle produces predictions immediately. Tradeoff: alerts act on data up to ~8s
  old. Behavior/risk decision for the owner (real money trades off these).
- **More frozen-chain round-trip cuts** (same sign-off, MEASURE the v91 cut first):
  bulk `insert_observation` (7 writes → 1 executemany/cycle); batch the per-asset
  decision writes in v92/v93/v94 (`_record_decision`, ON CONFLICT no-ops that
  still cost a round-trip each every cycle); thread v92's `get_prediction(15M)`
  read into the v91 batched read via the snapshot.
- Recommend: redeploy → measure the v91 cut, THEN decide gate-vs-more-surgery.

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
