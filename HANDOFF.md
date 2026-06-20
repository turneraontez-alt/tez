# Session handoff

Working notes so a fresh session resumes cheaply. See `CLAUDE.md` (architecture)
and `SYNC.md` (Replit sync). Live app on Replit (`python3 app.py`). **The owner
trades REAL money manually off the alerts**, so reliability + honest data
freshness + honest accuracy measurement matter more than new model features.

⚠️ Fresh container: `pytest` and `websockets` are NOT preinstalled →
`pip install pytest "websockets>=12.0" -q` first. Tests: `python3 -m pytest tests/ -q`
→ **435 passed, 4 skipped**.

## ✅ Shipped (branch `claude/read-handoff-e79js5`) — Telegram UX + alert dedup
Verified live first: speedup (~4.5s → ~2.3–2.6s) and the checkpoint-label fix
(saw `7M` in `/predictions`, impossible under the old all-`15M` bug).
1. **Checkpoint alert restyled to the hourly-report look** (`build_v95_message`,
   `checkpoint_v95.py`). Bold header (keeps `V9.5 CHECK` + `ENTRY/NO ENTRY`),
   one-line headline, a `<pre>` monospace table (asset · side · model P · market
   P · edge), an `ask→max` economics line per live entry, unavailable picks
   listed below. `augment_telegram_message` (`calibrated_edge.py`) now skips any
   `V9.5 CHECK` message so the clean table isn't followed by a `V9 TRADE QUALITY`
   block.
2. **Eastern time on the report** (`reporting.py`): header renders
   `America/New_York` (auto EST/EDT) via `_eastern_header`; `tzdata` added to
   `requirements.txt`. Dedup/claim key stays UTC (whole-hour offset → same firing
   moment).
3. **Hourly scoreboard always shows 15M/10M/7M** (`_sb_row(..., placeholder=True)`
   for the checkpoint group) — empty buckets render `0-0  —  —` instead of
   vanishing, so 10M/7M are visible as they start accumulating.
4. **"4 alerts at the 10m mark" → exactly one, only once decided**
   (`checkpoint_v95.py`). Root cause: `_notification_identity` keyed the dedup on
   the top-ranked **ticker**, which churns as edges jitter → a fresh key + send
   per leader. Now keyed per **(checkpoint, 15-min market window)**
   (`Q15_V95_SINGLE_ALERT_PER_CHECKPOINT`, default ON). Plus a stability gate
   `_decision_settled`: holds the alert until the top-3 (asset/side/entry)
   signature is stable for `Q15_V95_DECISION_STABILITY_CYCLES` (default 3) cycles,
   with a force-send fallback as the band closes
   (`Q15_V95_DECISION_FORCE_MARGIN_SECONDS`, default 60). NOTE: a genuine
   WATCH→ENTRY upgrade after the first send still re-fires (intentional — don't
   miss a real entry); only jitter is suppressed.
   **Timing fix:** the detection bands (660s/480s) made the alert fire at band
   *entry* — 10M at ~11:00 and 7M at ~8:00 remaining (~1 min early). The gate now
   holds each alert until the clock reaches its **named minute**
   (`_CHECKPOINT_TARGET_SECONDS` = 900/600/420s; `Q15_V95_FIRE_AT_CHECKPOINT_MARK`
   default ON, `Q15_V95_CHECKPOINT_MARK_TOLERANCE_SECONDS` default 15) so 15M/10M/7M
   land on time.
5. **Hourly report "14 min late"**: scheduling code is correct (fires every ~1s at
   the UTC boundary). Owner confirmed Always-On, so cause is restarts. Added
   send-time logging (`:NN past the hour`) + a restart catch-up
   (`Q15_HOURLY_CATCHUP_MINUTES`, default 5) so a restart shortly after the hour
   still delivers (claim_event keeps it idempotent). Watch the logs to confirm.

Tests: `450 passed, 4 skipped`. New: `test_q15_v95_single_alert.py`; updated
checkpoint-message + scoreboard + calibrated-edge tests for the new format.
**Still TODO on Replit:** set `Q15_ALERT_LEVEL_10M=all` / `_7M=all` in Secrets to
actually receive the 10m/7m checks (else muted as `NO ENTRY YET` under `balanced`).

## ✅ Shipped prior session (branch `claude/hand-off-report-ezzh3s`, merged to `main`)
1. **THE FIX — 10m/7m checkpoints never fired** (`checkpoint_v95.py`,
   `test_q15_v95_checkpoint_time_authoritative.py`). Root cause: `_detect_checkpoint`
   (inherited, frozen) consults a recursive snapshot key-walk + the buffered
   parent message text BEFORE its time fallback. The parent emits a
   `30M CHART CONTEXT — PRIOR 15M + CURRENT 15M` header (contains "15M", never
   "10M"), so every cycle was labeled **15M** regardless of clock. Confirmed live:
   `/predictions` said `15M` at the 10-min mark and the ledger held 37 resolved
   rows ALL under 15M (zero 10M/7M). Since checkpoint drives both the alert event
   key and the ledger bucket, this suppressed every 10m/7m alert and starved their
   scoreboards. Fix: `_resolve_checkpoint` resolves time-first from
   `seconds_remaining` (same 660/480 boundaries), heuristics only when no time.
   Gated `Q15_V95_TIME_AUTHORITATIVE_CHECKPOINT` (default ON).
2. **Per-checkpoint alert levels** (`notifier.py`, `test_q15_alert_suppression.py`).
   `Q15_ALERT_LEVEL_10M` / `_7M` / `_15M` override the global `Q15_ALERT_LEVEL`
   per checkpoint (keyed off the header label). Unset → inherit global (default
   behaviour unchanged). Lets the owner get 10m/7m checks delivered even when the
   verdict is `NO ENTRY YET`, while 15m stays muted under `balanced`.
3. **Muted the `V9.5 STARTUP` Telegram spam** (`notifier.py`). The
   "canonical analysis is not ready" placeholder (emitted before the first v9.5
   cycle populates globals — re-opens on every restart) is now a non-actionable
   marker → muted under `balanced`, still visible under `all` + in /api/health.
4. **Speedup ~4.5s → ~2.3–2.6s/cycle** (no model-output change):
   - **Ledger caches** (`ledger_v95.py`, `test_q15_v95_ledger_cache.py`):
     `calibrate` (12-iter Platt/Newton over ≤2500 rows), `pattern_similarity`
     (500-row fetch + JSON parse), and `challenger_weights` were recomputed per
     asset per ~1s cycle but only change every ~30s. Now memoized against a
     monotonic data version bumped in `resolve_ticker` + `_apply_shadow_update`;
     cache hit is byte-identical. `calibrate` split into a cached fit + cheap
     apply. Kill-switch `Q15_V95_LEDGER_CACHE=false`. (~0.3–0.65s)
   - **`Q15_FAST_CANONICAL_CANDLES` default flipped ON** (fuzz-locked identical;
     `=false` reverts).
   - **Disabled the legacy v9.4 unified loop** (`.replit [userenv.shared]`
     `Q15_V94_UNIFIED_ENABLED = "false"`). v9.5 reads ZERO `q15_v9_4_unified_*`
     fields yet that loop ran a full second per-asset model every cycle (~1.6s)
     only to be discarded. The flag short-circuits `v94_unified.run_cycle` right
     after `super().run_cycle()`, which still produces the dual-window context
     v9.5 needs. Trade-off (owner-approved): `/api/q15-v9-4/unified/*` endpoints,
     `q15_v9_4_unified_*` snapshot keys, and the v9.4 15M learning ledger go dark
     — all superseded by v9.5.
5. **Per-stage profiler for `analyse_v95`** (`checkpoint_v95.py`,
   `test_q15_v95_feature_profile.py`). `Q15_V95_PROFILE_FEATURES=true` (default
   OFF) times each feature/model/ledger stage → `/api/health.q15_v9_5.feature_profile`
   ranked by total time. Use it to pick the next optimisation target in `analyse_v95`.

## 🔴 Immediate next steps (deploy + verify, then bake)
- **Deploy:** code changes load on Repl **Stop ▸ Run** after pulling `main`. The
  **`.replit` env change (`Q15_V94_UNIFIED_ENABLED`) needs a real RESTART/REBOOT**
  (Stop▸Run may not re-read `.replit`); if it stays enabled, set the var in
  Replit **Secrets** instead.
- **For 10m/7m Telegram pings:** set `Q15_ALERT_LEVEL_10M=all` + `Q15_ALERT_LEVEL_7M=all`
  in Secrets (else they stay muted as `NO ENTRY YET` under `balanced`).
- **Verify speedup:** `curl /api/health` → `cycle_watchdog.last_cycle_seconds`
  should be ~2.3–2.6s; `q15_v9_5.run_cycle_timing.parent_chain` should drop ~1.6s.
- **Verify the fix:** at the 10-min mark `curl /api/q15-v9-5/predictions` →
  `checkpoint` should now print `10M` (was `15M`). 10m/7m will start appearing in
  the hourly report once they settle.
- **Then bake.** Wait for ≥30 resolved (trust) / ≥50 (promotion) PER checkpoint.
  Judge on `champion_skill_vs_market`, `calibration_error_ece`, `realized_avg_cents`
  — not raw %. Calibration is still `identity` (ECE ~0.07); fit a real calibrator
  once ≥~50 resolved.

## Why "no entry" (asked + answered — it's intended, not a bug)
The v9.5 ladder (`analyse_v95` ~lines 790–810) needs BOTH ≥0.60 conservative
win-prob AND ≥6¢ net edge after costs at 10m (0.58/4¢ at 15m). `conservative =
selected − ~0.08`, and `selected` is **market-anchored** (`Q15_V95_MARKET_ANCHOR_STRENGTH=1.0`)
toward the ~50/50 Kalshi line, so it rarely clears the bar. Scoreboard (54% acc,
−480¢ realized over 37) confirms the model hasn't earned the right to deviate —
suppressing entries is correct. The fix is data + calibration, NOT looser gates.
Levers (use with caution): `Q15_V95_10M_MIN_PROBABILITY`,
`Q15_V95_10M_REQUIRED_EDGE_CENTS`, `Q15_V95_MARKET_ANCHOR_STRENGTH`.

## Levers still open
- **Next compute cut:** turn on `Q15_V95_PROFILE_FEATURES`, read
  `feature_profile`, optimise the top `analyse_v95` stage behaviour-identically.
  Remaining parent cost (`v94_super_chain` ~0.4s) is frozen + produces the context
  v9.5 needs — hard to cut safely.
- Fit a real calibrator once ≥~50 resolved (replace `identity`).
- DOGE spot-WS tick age runs >3s (thin feed) → uses REST last-good fallback;
  fine, only touch if you want fresher DOGE spot.

## Invariants — do not break
- Read-only; nothing places a real exchange order (the human trades manually).
- Champion weights FROZEN; only the shadow challenger learns; promotion is manual +
  significance-tested. Gate model-behavior changes behind default-OFF `Q15_*` flags.
- Don't edit the frozen `checkpoint_v91..v94*` chain except owner-approved,
  behaviour-IDENTICAL, test-locked changes. This session deliberately avoided
  frozen-chain edits (the ~1.6s win was the existing `Q15_V94_UNIFIED_ENABLED` flag).
- Keep `V9.5 CHECK` + `ENTRY RECOMMENDED`/`NO ENTRY YET` markers in checkpoint
  messages; keep the `Hourly Report —` header. Keep `.env.example` free of real
  secret-scanner patterns.

## Repo state & workflow
- Dev branch this session: **`claude/hand-off-report-ezzh3s`** (merged to `main`).
  The GitHub Relay auto-syncs `main` ⇄ Repl ~20s (never force-push; also pushes
  Repl-side "Published your App" commits to `main` — `git fetch origin main`
  before pushing). Commit identity: `user.name Claude`, `user.email noreply@anthropic.com`.

## New env flags this session (all optional)
`Q15_V95_TIME_AUTHORITATIVE_CHECKPOINT` (ON) · `Q15_ALERT_LEVEL_10M` / `_7M` /
`_15M` (inherit global) · `Q15_V95_LEDGER_CACHE` (ON) ·
`Q15_FAST_CANONICAL_CANDLES` (now ON) · `Q15_V94_UNIFIED_ENABLED` (now false via
.replit) · `Q15_V95_PROFILE_FEATURES` (OFF).

## Gotchas
- Data is sparse until markets settle — don't tune on tiny samples.
- For binaries, calibration + skill-vs-market + edge beat raw % accuracy.
- After the checkpoint fix, the prior 37 resolved rows remain labeled 15M; 10M/7M
  scoreboards correctly start from 0 and accumulate going forward.
