# Session handoff

Working notes so a fresh session resumes cheaply. See `CLAUDE.md` (architecture)
and `SYNC.md` (Replit sync). Live app on Replit (`python3 app.py`). **The owner
trades REAL money manually off the alerts**, so reliability + honest data
freshness + honest accuracy measurement matter more than new model features.

⚠️ Fresh container: `pytest` and `websockets` are NOT preinstalled →
`pip install pytest "websockets>=12.0" -q` first. Tests: `python3 -m pytest tests/ -q`
→ **408 passed, 4 skipped**.

## ✅ Current state (after this session)
- **The model is producing predictions again.** The long-standing
  `current_trade_decisions: {AVOID_INVALID_DATA: 7}` is fixed — the inner chain now
  emits real decisions (`WATCH_CONFIDENCE` / `WATCH_PRICE`). This was the headline
  bug and it's resolved (root cause below).
- **Spot WS is ON** (`Q15_SPOT_WS_ENABLED=true`), connected to Coinbase+OKX,
  sub-second ticks for all 7 assets. (It did NOT clear the gate — that wasn't the
  cause — but it's a real latency win and is live.)
- **Accuracy is now measurable**: `GET /api/q15-v9-5/accuracy` + a one-line
  `model_accuracy` headline in `/api/health`.
- Scoreboard is still tiny (~3 resolved — the bug starved it for weeks). Now that
  predictions flow it will accumulate. Calibration is still `"identity"` (none
  fitted yet — needs data).

## 🔴 Immediate next step
**Let it bake and watch the accuracy readout.** Nothing to build for accuracy
right now — the binding constraint is resolved data, not the model.
- `curl -s localhost:8000/api/q15-v9-5/accuracy | python3 -m json.tool`
- Wait for **≥30** resolved (trust) / **≥50** (promotion gate) before judging.
- Judge on **`champion_skill_vs_market`** (>0 = beats the Kalshi line),
  **`calibration_error_ece`** (0 = perfect), and **`realized_avg_cents`** — NOT raw
  % accuracy. When a checkpoint's verdict flips to `PROMOTION_CANDIDATE`, that's
  the (evidence-based) signal to manually promote the challenger.

## Shipped this session (all merged to `main`, latest HEAD `3105cab`)
1. **THE FIX — false `AVOID_INVALID_DATA`** (`analysis.py`,
   `test_q15_snapshot_time_core_age.py`). Root cause: the v9.4/v9.5 "core age"
   staleness checks resolve the snapshot observe-time via `_first_value`, which
   *walks the whole snapshot* for any `timestamp`/`ts`/`updated_at`-ish key — and
   nothing ever set an authoritative one, so it latched onto a stale nested
   timestamp and tripped `stale_core_snapshot` → `AVOID_INVALID_DATA` for all 7
   assets, persistently, regardless of feed freshness. Fix: `build_snapshot` now
   stamps `snapshot_time = now`, which both gates check *first*. Freshness is NOT
   weakened (v5 spot/book age + the 90s candle-age check stay; a genuinely
   un-rebuilt snapshot still ages out — locked by test).
2. **Spot WS enabled** — `Q15_SPOT_WS_ENABLED = "true"` in `.replit`
   `[userenv.shared]` (same mechanism as `KALSHI_WS_ENABLED`). Per-asset REST
   fallback, read-only public tickers. `have_ws:true` on the Repl confirms the
   `websockets` dep is installed there.
3. **`Q15_FAST_CANONICAL_CANDLES`** (default OFF) — behaviour-identical fast
   `_canonical_candles` for the two v9.5 call sites (`q15_upgrade/fast_candles.py`,
   `test_q15_fast_canonical_candles.py` incl. a 400-iter fuzz). ~1.8x on the candle
   *build*, but that's only tens of ms/cycle — leave it OFF unless chasing compute.
4. **data_age cuts** (`app.py`, `checkpoint_v91.py`, `test_q15_v91_rolling_batch.py`):
   - **Decoupled the slow detail fetch.** `client.get_market` (volume, REST, up to
     ~7s) was the only REST leg left in the per-cycle critical fetch and was
     blowing the 3s deadline → deferral → aged snapshot. It's now refreshed OFF the
     critical path into a per-ticker last-good cache (`fetch_market_detail` +
     `detail_cache` in `refresh_loop`); the critical fetch is WS-only. Closes the
     ~1.8s deferral gap.
   - **Batched the v91 rolling read** — `pre_enrich_all` did one Postgres
     round-trip per asset for `recent_observations`; now one via
     `recent_observations_for_pairs` (PG `UNION ALL` of the *exact* per-pair query,
     byte-identical; SQLite stays per-pair). This was the prior handoff's named
     "last lever".
5. **OOS reviews-table error spam silenced** (`oos_v9.py`,
   `test_q15_oos_reviews_table_probe.py`) — `q15_ten_minute_reviews` /
   `q15_10m_reviews` were never created; probe with `to_regclass()` first, only
   SELECT what exists.
6. **Accuracy / promotion-readiness readout** (`q15_upgrade/accuracy_report.py`,
   `test_q15_accuracy_report.py`) — pure interpreter over `V95Ledger.metrics()`:
   per-checkpoint verdict (`LEARNING_OFF`/`ACCUMULATING`/`CHALLENGER_NOT_BETTER`/
   `PROMOTION_CANDIDATE`), ECE, skill-vs-market, challenger-vs-champion p-values,
   realized edge. `GET /api/q15-v9-5/accuracy` + `/api/health.model_accuracy`.

## Key reframes / correct the record
- **The earlier handoff was WRONG** that `AVOID_INVALID_DATA` keyed on a 3s
  feed-age gate (`max(spot_age, book_age)`). The actual blocker producing the 7×
  AVOID was the `stale_core_snapshot` timestamp-walk bug (fixed above). That's why
  enabling spot WS alone didn't clear it.
- **Two different `data_age` numbers.** `/api/health.data_age_seconds` =
  `now − engine.last_update_ts` ≈ the **cycle period** (~3.5–5s). It does NOT gate
  predictions. The gates use spot/target/time *presence* + a 30s core-age + 90s
  candle-age.
- **parent_chain (~2.0s) is mostly GIL-bound per-asset compute** (v94 candle
  analysis), not DB. DB round-trips are now minimal. Further cuts need either the
  flagged candle path or the high-risk decoupled-ingest loop.

## Levers still open (none urgent — predictions flow + accuracy doesn't gate)
- **Decoupled fast-ingest loop** to push `data_age` < 3s: run a tight ~1s
  fetch/ingest loop independent of the heavy chain. Biggest data_age win but real
  concurrency risk on a money path → flag it + test hard. LOW ROI now (data_age
  doesn't gate; ENTRY alerts gated by `min_settled:30` anyway).
- **Fit a real calibrator** once ≥~50 resolved (replace `identity`). Needs data.
- **Trim cruft**: collapse the duplicated `q15_v9_1..v9_5` health blocks; retire
  dead layers. Heavy ~7k-line frozen stack is hard to trust with money.
- NOT WORTH IT: thread-parallelising the per-asset chain (GIL-bound + already
  batched/lock-serialised → ~zero reward, high risk).

## Repo state & workflow
- Dev branch this session: **`claude/vibrant-franklin-x16oqb`** (pushed, even with
  origin). `main` carries the full history; merges go straight to `main` (the
  GitHub Relay auto-syncs `main` ⇄ Repl ~20s, never force-push, and also pushes
  Repl-side `"Published your App"` commits to `main` — reconcile with
  `git fetch origin main` before pushing). Commit identity: `user.name Claude`,
  `user.email noreply@anthropic.com`.
- **Code** changes need only the relay sync + **Stop ▸ Run** to load. **Env**
  changes (`.replit [userenv.shared]`) need a real restart/reboot — Stop▸Run may
  not re-read `.replit`; if `spot_ws.enabled` stays false after a code restart,
  set the var in Replit **Secrets** instead.

## New env flags (all optional)
`Q15_SPOT_WS_ENABLED` (now ON via .replit) / `Q15_SPOT_WS_MAX_AGE_SECONDS` (3) ·
`Q15_FAST_CANONICAL_CANDLES` (default OFF) · `Q15_CYCLE_WATCHDOG_SECONDS` (10) ·
`Q15_WATCHDOG_ALERT_*` · `Q15_V95_RECONCILE_BUDGET_SECONDS` (4) /
`Q15_RECONCILE_BUDGET_SECONDS` (4) · `Q15_V95_SLOW_CYCLE_SECONDS` (10) ·
`Q15_V95_PROMOTION_MIN_ROWS` (50) / `Q15_V95_PROMOTION_ALPHA` (0.05).

## Invariants — do not break
- Read-only; nothing places a real exchange order (the human trades manually).
- Champion weights FROZEN; only the shadow challenger learns; promotion is manual +
  significance-tested. Gate model-behavior changes behind default-OFF `Q15_*` flags.
- Keep `V9.5 CHECK` + `ENTRY RECOMMENDED`/`NO ENTRY YET` markers in checkpoint
  messages; keep the `Hourly Report —` header. Keep `.env.example` free of real
  secret-scanner patterns. Don't edit the frozen `checkpoint_v91..v94*` chain
  except for owner-approved, behaviour-IDENTICAL, test-locked round-trip cuts
  (e.g. this session's v91 rolling batch).

## Gotchas
- Data is sparse until markets settle — don't tune on tiny samples (3/3 is noise).
- For binaries, **calibration + skill-vs-market + edge** beat raw % accuracy.
- `MarketResultCache` (`market_cache.py`) caches only resolved markets; unresolved
  ones are re-fetched live each cycle (root of the old reconcile-stall bugs).
- Market detail/volume is now last-good (refreshed off the critical fetch); it's
  keyed by ticker so a rollover never reuses the prior market's volume.
