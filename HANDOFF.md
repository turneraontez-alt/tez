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
6. **Frozen-chain round-trip batching** (`checkpoint_v91/v92/v93.py`,
   owner-approved). Per cycle (7 assets) this collapses per-asset Postgres chatter
   into a handful of bulk statements — all byte-for-byte equivalent (multi-row
   `VALUES … ON CONFLICT DO NOTHING` == N single inserts; OR-of-pairs read == N
   per-pair reads; threaded value == the read it replaces):
   - **v91 `pre_enrich_all`** — ONE `insert_observations` (was 7), ONE
     `write_predictions` (was 7), ONE `get_predictions_for_pairs` (was 7, and was
     itself a 6→4/asset cut earlier). Only the per-group rolling read
     (`recent_observations`, per-asset `LIMIT` window) stays per-asset — left
     un-batched on purpose (needs version-specific window-function SQL).
   - **v92** — threads v91's already-read 15M prediction via
     `snap["q15_v91_fifteen_prediction"]` instead of its own `get_prediction(15M)`
     (−7 reads/cycle); batches the 7 `_record_decision` writes into ONE.
   - **v93** — batches the 7 `_record_v93` writes into ONE.
   - **v94** — already covered (only per-asset DB work is the context cache, now
     on a persistent connection).
Tests: `test_q15_v95_ledger_conn_reuse`, `test_q15_v94_context_cache_conn_reuse`,
`test_q15_v94_gate_conn_reuse`, `test_db_autocommit`, `test_q15_v91_round_trips`
(asserts bulk methods + multi-asset single-round-trip + pairs-read equivalence).

**REMAINING LEVER if still over the 3s gate (owner's call):**
- **Raise `Q15_*` `max_data_age_s`** from 3s to ~8s (env/config, no code) → the
  cycle produces predictions even at ~6s. Tradeoff: alerts act on data up to ~8s
  old — behavior/risk decision for the owner (real money trades off these).
- Last code lever (riskiest, only if measured cycle is still just over): batch the
  v91 `recent_observations` per-group read with a Postgres window function.
- Recommend: redeploy → measure → if still over, flip `max_data_age_s` first.

✅ FIXED (was the latent stall family): the three OTHER settlement reconcilers
(`performance.reconcile`, `window_focus.reconcile_settlements`,
`learning.reconcile`) now carry the same wall-clock budget as the v95 one
(`Q15_RECONCILE_BUDGET_SECONDS`, default 4s) — a batch of slow Kalshi lookups
for recently-closed-but-unsettled markets (common right after a restart, when a
backlog of predictions awaits settlement) can no longer monopolise the refresh
loop; leftover tickers retry next cycle. Tests: `test_q15_reconcile_budget.py`.

🔬 DIAGNOSED + PARTLY OPTIMISED: the `slowest_run_cycle` atomic snapshot (in
`/api/health`, latches the worst cycle ≥ `Q15_V95_SLOW_CYCLE_SECONDS`, default
10s; includes run-cycle buckets + chain sub-stages + a `v95_sub` deepcopy/build/
analyse/record split) pinned the residual cost to **`unified_loop` (v94
`analyse_snapshot`) + `build_canonical_snapshot` (v95 `_canonical_candles`)** —
genuine per-asset candle analysis, ~2× inflated during warmup, NOT a stall and
NOT Kalshi. Steady-state was ~6s (74-min dump); warmup ~13s.

Behaviour-IDENTICAL efficiency wins applied to that path (no flag — pure
refactors, `test_q15_candle_efficiency.py`):
- Candle rows are flat dicts of immutable floats, so `copy.deepcopy` → `dict()`
  in `_candles()` (≈28 calls/cycle over the full history), the v95 canonical
  `candles=tuple(...)`, and the deep-eval candle copy. Same isolation, far cheaper.
- `_normalize_key` (regex sub run on EVERY snapshot key, twice/asset/cycle over a
  tiny repetitive vocabulary) is now `@lru_cache`d — huge hit rate, identical out.

✅ SHIPPED (this session): the **default-OFF flagged fast `_canonical_candles`**.
`Q15_FAST_CANONICAL_CANDLES` (off by default) routes the two v9.5 call sites
(`build_canonical_snapshot` + the bridge) through `q15_upgrade/fast_candles.py`.
It is byte-for-byte identical: a row is fast-normalised ONLY when every field
resolves via a direct-alias hit (the frozen `_first_value` checks direct aliases
before it ever walks, so a direct hit is identical regardless of nesting); any
row that would need the walk falls back to the frozen `_normalize_candle`
verbatim. Cached history rows have all 8 fields as direct keys, so the bulk takes
the fast path with zero key-normalisation / walks / alias-tuple allocations.
**~1.8x faster on the candle build, 390 tests pass.** The frozen v91..v94 chain
is untouched. Locked by `tests/test_q15_fast_canonical_candles.py` (hand cases +
a 400-iteration randomized fuzz comparing fast vs frozen byte-for-byte).
NEXT for this: redeploy with the flag set → confirm `current_trade_decisions`
candles + `v95_sub` unchanged, measure the steady-state shave.

KEY REFRAME found this session: the gate's `AVOID_INVALID_DATA` keys on the
**snapshot** `data_age_seconds = max(spot_age, book_age)` (`v5_hardening.py:52`),
i.e. FEED age stamped at fetch time — NOT the cycle/candle compute, which runs
after. So compute-shaving (this candle work, parallelization) does NOT move the
gate. The ~7.4s in the notes above is the *other* field (`/api/health`
`data_age_seconds` = `now − engine.last_update_ts`, the cycle period). The direct
unblock is a fresher spot feed: **turn on the already-built spot WS**
(`Q15_SPOT_WS_ENABLED=true`, all 7 assets, REST fallback) → `spot_age` sub-second
→ gate clears without loosening the threshold or touching frozen code.

NOT WORTH IT (investigated + declined this session): thread-parallelising the
per-asset enrich/`run_cycle` chain. The residual is pure-Python candle analysis
(no numpy/pandas) → GIL-bound, so threads give ~no speedup; the I/O is already
batched into single all-7-asset round-trips and lock-serialised (`self._lock`),
so parallelising would force un-batching and re-serialise on the lock. High risk
(frozen money path), ~zero reward. True parallelism would need multiprocessing.

Still-open levers: turn on spot WS (recommended, owner call), raise
`ALERT_MAX_DATA_AGE_S`/`max_data_age_s` (owner risk call). A count-cap on the
candle cache is NOT safe (could clip the previous-15m window the dual-window
context needs, cadence-dependent).

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
`Q15_V95_RECONCILE_BUDGET_SECONDS` (4) · `Q15_RECONCILE_BUDGET_SECONDS` (4, shared
wall-clock budget for the perf/window-focus/learning settlement reconcilers) ·
`Q15_V95_SLOW_CYCLE_SECONDS` (10, latches the `slowest_run_cycle` health snapshot).

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
