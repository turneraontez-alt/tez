# Session handoff

Working notes so a fresh session resumes cheaply. See `CLAUDE.md` (architecture)
and `SYNC.md` (Replit sync). Live app on Replit (`python3 app.py`). **The owner
trades REAL money manually off the alerts**, so reliability + honest data
freshness + honest accuracy measurement matter more than new model features.

⚠️ Fresh container: `pytest`/`websockets`/`flask` are NOT preinstalled →
`pip install pytest "websockets>=12.0" flask -q` first. A broken `cffi`/`cryptography`
may need `pip install --force-reinstall --ignore-installed cffi cryptography -q`
(else the two app-level test files error on collection instead of skipping).
Tests: `python3 -m pytest tests/ -q` → **949 passed, 4 skipped** in a complete env
(skip count rises when `flask`/`websockets`/cffi/crypto aren't fully installed).

## ✅ Shipped THIS session — Implemented the updated-review fixes (critical → polish)
**On branch `claude/updated-review-ecyp4x`.** Worked the latest auditor review's
confirmed findings end-to-end; suite **949 passed, 4 skipped** (+~13). All
read-only wrt real exchanges; frozen champion untouched; live probability path
byte-for-byte unchanged unless an explicit default-OFF flag is set.
- **CRITICAL — per-asset ingest isolation** (`app.py`): wrapped the ingest loop
  (ensure_market/ingest_trades/ingest_spot/parse_orderbook) in a per-asset
  try/except so one poisoned tick no longer aborts the loop and starves every
  other asset of a snapshot that cycle. Test: `test_app_loop_degraded_paths.py::
  TestIngestExceptionIsolation`.
- **HIGHEST — isotonic calibration** (`ledger_v95.py`): added pure PAVA
  `_isotonic_fit`/`_isotonic_predict`, computed over the same resolved rows and
  applied by `calibrate()` ONLY when `Q15_V95_CALIBRATION_ISOTONIC=1`
  (DEFAULT OFF → live = Platt, unchanged). Targets the recorded high-band
  UNDER-confidence (pred ~0.78 → win ~0.95) a slope-capped Platt can't bend.
  Tests: `test_review_fixes_v4.py`.
- **MEDIUM — shadow-signal data collection ON by default**
  (`shadow_signals.py`): `Q15_V95_SHADOW_SIGNALS_ENABLED` now defaults True.
  Verified the signals are write-only to `predictions.shadow_signal_json` and
  read back only by the OOS A/B — they never touch the live probability — so the
  5 features can finally accrue evidence while staying in shadow.
- **MEDIUM — repaired 3 shadow features** (`shadow_signals.py`):
  order_flow_persistence now carries a damped flow lean on candle gaps instead
  of going dead at 0.0; prediction_stability treats a MISSING flip-risk as
  neutral (0.5) not "perfectly stable"; regime_transition de-confidences (0.5)
  when all regime inputs are absent instead of assuming "no transition".
- **POLISH — p-value precision** (`ledger_v95._round_p`): strong promotion
  p-values no longer flatten to 0.0 at 6dp. **POLISH — flip-alert honesty**
  (`ledger_v95.flip_warning_performance` + `reporting._flip_scoreboard`): the
  disabled high-flip-risk channel is now labelled "alerts disabled" rather than
  reading as 0-detected/100%-missed.
- **Examined, NOT a bug:** ticker↔asset "mismatch" (Kalshi tickers are
  market-unique; recorded together per market) and rank double-counting
  (`prediction_id` is the PK) — documented, no code change.
- **DB:** no schema change (isotonic fit is in-memory; shadow_signal_json column
  already existed). **Restart/ET/grading/dedup** re-verified end-to-end.
- **Files:** `app.py`, `q15_upgrade/ledger_v95.py`, `q15_upgrade/shadow_signals.py`,
  `notifications/reporting.py`, `.gitignore`, + tests
  (`test_review_fixes_v4.py` new; updated `test_app_loop_degraded_paths.py`,
  `test_shadow_signals.py`, `test_q15_v95_significance.py`,
  `test_q15_learning_scoreboard.py`).

## ✅ Shipped earlier THIS session — Expanded `updated-review` into a full system auditor
**On branch `claude/updated-review-ecyp4x`, merged to `main`.** Rewrote
`.claude/skills/updated-review/SKILL.md` (skill-only; no app/test change, suite
unchanged at **906 passed, 13 skipped** in this container env). The skill now
grades **Shadow** and **Your System** separately (/100) from real ledger records
(`V95Ledger().scoreboard()`/`official_scoreboard()`/`shadow_signal_experiment()`,
challenger `ShadowLedger` + `challenger/stats.py` paired tests), compares
15M/10M/7M and #1/#2/#3 ranking **only on matched snapshot+timestamp rows**
(no look-ahead credit), tests the 5 background features against
`shadow_signals.SIGNAL_NAMES` (HELPING/HURTING/INSUFFICIENT/BROKEN, OOS-gated),
runs the full live-workflow bug checklist (confirmed vs suspected), checks
whether the last review's recommendations landed, and emits the owner's fixed
output template. Read-only; rules forbid inventing numbers (label
`INSUFFICIENT DATA (n=…)`). NOTE: container `data/*.sqlite3` are seed/empty
copies — real numbers populate when run against live Replit data.

## ⚙️ Merge policy (NEW — applies every session)
Finished + green work **auto-merges to `main`** without asking (owner-authorized;
see CLAUDE.md "Merge policy"). **HANDOFF.md is refreshed automatically as part of
every such change** (this section + the "Shipped" entries) — no need to be asked. `main` is the deploy branch (GitHub Relay syncs it
to/from the Repl). **The one gate is a data-safety guard:** before merging, `git
fetch origin main` and inspect commits that exist ONLY on `main` (`git log --stat
origin/main ^<branch>`); if any add/modify real file content (NOT the empty
"Published your App" syncs), merge `origin/main` INTO the branch first and verify
the merge drops no `main`-only lines/files — then merge back. If a merge would
delete data that only exists on `main`, STOP and report. (This already caught a
6.3k-line `health_snapshot.json` + a perf commit another chat had pushed to `main`.)

## ✅ Shipped THIS session — END-RESULT grid "tries its best on each one" (no needless blanks)
Owner: don't leave empty slots — fill every interval/rank with the best real data available.
- **Root cause of blanks:** the grid fed from `latest_window_cases`, which forces ALL three intervals
  to share one global `max(window_id)`. If 10M/7M's latest settled window was older than 15M's they were
  dropped entirely; and #2/#3 were blank whenever the single chosen window had <3 settled assets.
- **Fix — per-interval best-filled window:** new `ShadowLedger.best_filled_window_cases` resolves EACH
  checkpoint independently, choosing the most recent settled window that has ≥ top_k graded picks (else
  the fullest, newest-on-tie). The END-RESULT CALL now fills 10M/7M from their own latest windows and
  fills #1/#2/#3 from a fuller recent window when the just-closed one is sparse. Each interval row is
  labelled with its own window time (they can differ). A slot reads — ONLY when that interval/rank has
  no settled pick at all (e.g. 7M right after the v5 reset) — it fills as data accrues. Honest: every
  shown pick is real and graded against its own window's result; nothing is fabricated.
- **Tests:** `test_end_result_grid_fills_each_interval_from_its_own_window`,
  `test_end_result_grid_prefers_fuller_window_to_fill_ranks` (+ existing grid test). `latest_window_cases`
  still powers the strict LAST WINDOW section (unchanged). Suite: **920 passed, 13 skipped**.

## ✅ Shipped earlier THIS session — Shadow-vs-Yours: ranked END-RESULT grid + v5 reset (branch `claude/manipulation-learning-progress-liqs9z`)
Owner: the END-RESULT CALL didn't fill #2/#3 (15M) or any rank at 10M/7M, and wanted the comparison reset.
- **END-RESULT CALL is now a ranked #1/#2/#3 grid across ALL three intervals** (`challenger/runner.py`),
  Shadow vs Your System, graded ✓/✗, built from `latest_window_cases` (same data as LAST WINDOW). Every
  interval and rank is always rendered; empty slots read — instead of being omitted. The old per-asset
  layout (which hard-coded #2/#3 to — because a single asset has one pick per interval, and dropped
  intervals with no pick) is gone. `latest_window_end_results` stays on the ledger (still unit-tested).
  Header "END-RESULT CALL · 15M, 10M & 7M" preserved.
- **Reset (Shadow-vs-Yours only):** `model_version` default bumped `challenger-v4 → challenger-v5`
  (`challenger/config.py`, `.env.example`). v5 starts empty; v1..v4 stay archived as
  PRE-SYNCHRONIZED-RESET (never deleted, never scored). Takes effect on deploy to `main`. The CYCLE
  CLOSED running record (official sent_predictions) was deliberately NOT reset.
- **Tests:** `test_default_model_version_is_v5_fresh_reset` + `test_end_result_call_is_ranked_grid_all_intervals`.
  Suite: **869 passed, 13 skipped**. **⚠️ Deploy note:** if the Repl pins `Q15_CHALLENGER_MODEL_VERSION`
  in its env, that overrides the new default — unset it (or set `=challenger-v5`) for the reset to apply.

## ✅ Shipped THIS session — Fix Shadow-vs-Yours "0 sent · N failed" delivery mis-accounting
**On branch `claude/updated-review-2x7wyr`.** Root cause: Your System's native picks were marked
`DELIVERY_FAILED` the instant the **synchronous** outbox attempt didn't return delivered — but the
notifier is the async `ReliableTelegramOutbox`, whose **background worker** delivers most reports on
retry (that's why the owner keeps RECEIVING reports while the record showed `0 sent · 39 failed`).
The sync attempt routinely loses to the worker's `claim()` (`outbox_v9.py:118-120`) or hits a Telegram
429, so it was never a real failure. Suite **937 passed, 4 skipped** (+7 `tests/test_delivery_reconcile.py`).
- **Fix — credit from the outbox's TRUE status, not the sync attempt:** the official report is now
  enqueued with a deterministic idempotency key (`v95-official:{checkpoint}:{window_close}`); a
  synchronously-undelivered (non-mute) send tags each pick **PENDING** under that key instead of
  failing it; and `run_cycle` reconciles each cycle — `SENT` (sync OR worker) → credited, `DEAD_LETTER`
  → failed, transient → still pending. Same applied to the compact-panel fallback.
- **New plumbing:** `ReliableTelegramOutbox.status_by_key` (+ both backends); challenger
  `ledger.mark_native_pending` / `reconcile_native_delivery` (+ new `native_delivery_key` column,
  additive migration); runner + `ledger_v95._shadow_mark_pending` / `_shadow_reconcile_delivery`
  wrappers; `checkpoint_v95._send_with_optional_key`. Read-only wrt production; what reaches Telegram
  is unchanged — only HOW Your System's record is computed.
- **Rate-limit investigation (the "why so many failures"):** there is NO proactive pacing or
  `429`/`Retry-After` handling. The worker is naturally paced (~0.5 msg/s, `outbox_v9.py:502`) and
  reliable; the UNPACED synchronous burst (official report + flip + follow-up + hourly in one cycle)
  exceeds Telegram's ~1 msg/s/chat limit → 429 → fixed 30s+ backoff → late worker delivery. The
  accounting fix makes the record correct regardless. **Optional follow-up (not done):** a min-interval
  send pacer + `Retry-After` parsing to cut the 429 rate.
- **Files:** `notifications/outbox_v9.py`, `q15_upgrade/challenger/{ledger,runner}.py`,
  `q15_upgrade/ledger_v95.py`, `q15_upgrade/checkpoint_v95.py`, `tests/test_delivery_reconcile.py` (new, 7).
  **DB:** additive `shadow_predictions.native_delivery_key`; backward-compatible.

## ✅ Shipped THIS session — Background shadow-signal experiment (5 new signals + continuous A/B)
**On branch `claude/updated-review-2x7wyr`.** Adds five experimental signals computed from data the
system ALREADY collects, recorded next to each prediction, and graded by a continuous, significance-
tested **out-of-sample** A/B that answers "would this signal improve the probability?" — WITHOUT
touching the frozen champion or the live probability. Read-only, default-OFF (`Q15_V95_SHADOW_SIGNALS_ENABLED`).
Suite **924 passed, 4 skipped** (+11 in `tests/test_shadow_signals.py`).
- **New module `q15_upgrade/shadow_signals.py`:** `compute_signals(analysis, canonical)` →
  five YES-oriented signals in [-1,1]: `order_flow_persistence` (#5), `book_resiliency` (#6, wick-
  rejection replenishment proxy), `prediction_stability` (#14, flip-risk → confidence), `entropy_noise`
  (#17, return-sign Shannon entropy → confidence), `regime_transition` (#18b, regime-boundary proximity
  → confidence). `evaluate(rows)` fits a 1-parameter logistic *adjustment* on top of the champion prob
  and measures the **out-of-sample** Brier reduction (time-ordered train/test split) with a paired
  t-test; `build_report_lines`/`scores_to_dict` render it. All pure/deterministic (no clock/IO).
- **Storage:** new additive `predictions.shadow_signal_json` column (validated `_ensure_column`
  migration), written only on fresh insert in the SAME isolated-column pattern as `shadow_factor_json`
  — never in `feature_json`, so it can never reach champion/challenger/calibration. Backward-compatible.
- **Recording:** `record_prediction(..., shadow_signals=...)`; `run_cycle` computes them when the flag
  is on (guarded; a compute failure can't break recording). `ledger.resolved_shadow_signal_rows()` +
  `ledger.shadow_signal_experiment()` grade settled rows oldest-first.
- **Reporting/dashboard:** compact "🧪 Experimental signals" block in the hourly Telegram report
  (`notifications/reporting.py`, default-OFF) + new endpoint `/api/q15-v9-5/shadow-signals`.
- **Promotion stays manual.** A signal is flagged a promotion candidate only when its out-of-sample
  Brier reduction clears the floor AND the paired t-test is significant; nothing is auto-applied.
- **Files:** `q15_upgrade/shadow_signals.py` (new), `q15_upgrade/ledger_v95.py`,
  `q15_upgrade/checkpoint_v95.py`, `notifications/reporting.py`, `app.py`,
  `tests/test_shadow_signals.py` (new, 11). **Env:** `Q15_V95_SHADOW_SIGNALS_ENABLED` (default OFF)
  + `_MIN_ROWS`/`_TRAIN_FRACTION`/`_BRIER_FLOOR`/`_ALPHA`/`_RECENT_CANDLES` tunables.

## ✅ Shipped THIS session — Updated-review fixes (Highest / Medium / Polish)
**On branch `claude/updated-review-2x7wyr` (not merged to main — session is branch-scoped).**
Implemented the full improvement list from the latest review. Read-only + frozen-champion
invariants intact; every model-touching change is default-OFF `Q15_*`-gated. Suite **913 passed,
4 skipped** (was 893; +20 in `tests/test_review_fixes_v3.py`).
- **H1 — honest thin-evidence (`checkpoint_v95.analyse_v95` / `build_v95_message`):** evidence
  coverage is now computed unconditionally and exposed as `evidence_coverage` / `low_evidence` /
  `absent_features` on the analysis + snapshot (`q15_v9_5_*`). A feature below the coverage floor is
  *absent* (quality gates it out of the logit — contribution = weight·value·quality), never a neutral
  0.0 that reads as support. New default-OFF `Q15_V95_LOW_EVIDENCE_FLAG` adds a compact "⚠ Thin
  evidence" note to the checkpoint card (markers preserved). `Q15_V95_LOW_EVIDENCE_MIN_COVERAGE`
  (default 0.50) tunes the flag.
- **H2 — canonical cent precision (new `q15_upgrade/money.py`):** one `Decimal`/banker's-rounding
  helper shared by the ledger settlement P&L, the scoreboard, the performance store, and the live
  money path (`net_edge_cents` as a signed edge; `ideal_entry_cents` clamped to a valid `[0,100]¢`
  price). Kills float noise like `3.3299¢` and impossible `>100¢` levels; ledger/performance now
  round identically (was 4-dp vs 2-dp).
- **M1 — structural fail-closed (`analyse_v95`):** if the structural base probability fails to load,
  the analysis no longer pushes thin volatility-derived features into the ensemble — it returns a
  `PREDICTION_ONLY` degraded result (`structural_model_unavailable`).
- **M2 — SQL identifier hardening (`ledger._ensure_column`):** table/column names are validated
  against a strict identifier whitelist and the DDL must begin with the column name; the last
  f-string-DDL gap is closed (raises `ValueError` on anything unsafe).
- **M3 — learning observability:** `_apply_shadow_update` records the effective step magnitude and a
  throttled "learning effectively frozen" warning when the knobs collapse every step to ~0; surfaced
  via `status()` (`last_learning_step_magnitude`, `learning_frozen_results`).
- **P1 — per-checkpoint dropped-row counters:** `dropped_feature_rows_by_checkpoint` so a corruption
  confined to one interval isn't masked as system-wide (aggregate kept for compatibility).
- **P2 — Wilson flag:** scoreboard buckets carry `ci_excludes_half`, distinguishing "clean but tiny"
  (3-0, low_n, straddles 0.5) from "genuinely separated from chance".
- **P3 — WS data-staleness watchdog (`spot_ws.py`):** forces a reconnect when an *open* socket stops
  delivering ticks for `Q15_SPOT_WS_DATA_TIMEOUT` (default 45s, 0=off); `health()` now reports
  `last_message_age_seconds` / `data_stale`.
- **Files:** `q15_upgrade/money.py` (new), `q15_upgrade/checkpoint_v95.py`, `q15_upgrade/ledger_v95.py`,
  `performance.py`, `spot_ws.py`, `tests/test_review_fixes_v3.py` (new, 20). **DB:** no schema change
  beyond the existing additive `_ensure_column` migrations (now validated); fully backward-compatible.

## ✅ Shipped THIS session — outbox send_with_result: fix delivery mis-detection (branch `claude/manipulation-learning-progress-liqs9z`)
> NOTE: a parallel `updated-review` session on `main` fixed the SAME delivery mis-accounting via
> per-cycle outbox-status reconciliation (`status_by_key` + `mark_native_pending` /
> `reconcile_native_delivery`). Both fixes now coexist after the merge: this branch's
> `send_with_result` gives the synchronous attempt a truthful delivered/message_id result, and the
> reconciliation path credits later worker-retry deliveries. Belt-and-suspenders, not conflicting.
Owner asked why the "Your System delivery: 0 sent · 12 failed · 23 pending" delivery was failing.
**It mostly WASN'T failing — delivery DETECTION was broken.** Production wires
`notifier = ReliableTelegramOutbox` (`app.py:91`) and passes it into `run_cycle` (`app.py:565`).
The official interval-report path does `if hasattr(notifier, "send_with_result"): … else: delivered =
ok and notifier.last_message_id is not None`. The outbox had **neither** `send_with_result` nor
`last_message_id`, so it fell to the else-branch and `delivered` was **always False** even though the
outbox's synchronous attempt actually delivered to Telegram. Consequences: every native pick recorded
`DELIVERY_FAILED` ("handled_no_message_id"); the official `sent_predictions` scoreboard never wrote
(it requires a message_id, `ledger_v95.py:856`); and the manipulation-alert gate saw the normal check
as not-delivered.
- **Fix:** `notifications/outbox_v9.py` now implements `send_with_result` (and a `_raw_send` helper +
  `_attempt_result`) that performs the synchronous attempt and returns the wrapped notifier's real
  `{ok, delivered, muted, message_id}`. The message_id is captured from the raw result INSIDE the
  delivery lock (never read back from shared state) so a concurrent worker delivery can't race it.
  Durable retry is unchanged; `send()` keeps its bool contract. Legacy bool notifiers fall back to
  delivered=ok with no id. So the official report's message_id now flows -> official scoreboard,
  Shadow-vs-Yours native side, and the manip-alert gate all populate truthfully.
- **Tests:** +3 in `tests/test_q15_v9.py` (rich-result passthrough w/ message_id; failure stays
  retryable & not-delivered; legacy bool fallback). Suite: **868 passed, 13 skipped**.
- **Note:** this is independent of the grading-default flip below (that made the COMPARISON robust to
  delivery; this makes delivery DETECTION truthful so the official record + manip alerts work too).

## ✅ Shipped THIS session — Shadow-vs-Yours: grade Your System on generated predictions (branch `claude/manipulation-learning-progress-liqs9z`)
Owner: "Your System" showed all `—`/`0W–0L` while Shadow filled. **Root cause:** the
native side was gated to picks DELIVERED to Telegram before close
(`Q15_CHALLENGER_NATIVE_SENT_ONLY`, was default ON) while the Shadow has no such gate;
delivery was failing (audit: 0 sent · 12 failed · 23 pending) so nothing cleared the gate.
- **Fix (owner-chosen): default the grading rule to count-all** — `native_sent_only`
  default flipped `True → False` in `q15_upgrade/challenger/config.py` (comment + `.env.example`
  updated). Your System is now graded on the SAME generated predictions as the Shadow, so the
  card is true model-vs-model and fills every window regardless of Telegram health. The
  `Your System delivery: …` audit line stays, so send health is still visible. Reversible via
  `Q15_CHALLENGER_NATIVE_SENT_ONLY=true`.
- **Tests:** the two gating-mechanism tests now pin `native_sent_only=True` explicitly (they test
  the gate, not the default); added `test_default_grades_generated_predictions_not_delivery` and
  `test_config_default_is_count_all`. Suite: **865 passed, 13 skipped**.
- **⚠️ Deploy note:** if the Repl has `Q15_CHALLENGER_NATIVE_SENT_ONLY=true` set explicitly in its
  env/secrets, that OVERRIDES the new code default — unset it (or set `=false`) for the change to
  take effect. Separately, the 12 failed deliveries mean real alerts aren't reaching Telegram; that
  delivery failure is still unaddressed (owner chose to fix the comparison, not delivery).

## ✅ Shipped THIS session — flip-learning in the decision-stats snapshot (branch `claude/manipulation-learning-progress-liqs9z`)
Owner asked for a single snapshot that captures BOTH manipulation tracks.
- **`decision_stats()` now carries a `flip_learning` block** (`checkpoint_v95.py`) wrapping the
  existing read-only ledger methods `flip_stats()` (learned flip-rate-by-risk curves + thresholds
  per checkpoint/direction/asset) and `flip_warning_performance()` (precision / detection-rate of
  fired warnings). Purely additive — the prior keys (`version`, `read_only`,
  `current_trade_decisions`, `ledger`, `metrics`) are unchanged. So one capture of
  `/api/q15-v9-5/decision-stats` (the `v95_ledger_snapshot.json` source) now shows the
  by_manipulation reliability scoreboard AND the flip-risk learning that was previously omitted.
- **Test:** `tests/test_q15_v95_flip_risk.py::TestDecisionStatsExposesFlipLearning` (records a real
  NO→YES flip, asserts the curve + warning-perf surface and the old contract is preserved). Suite:
  **863 passed, 13 skipped**.
- **DIVERGENCE investigation (no code change):** the DIVERGENCE manipulation tell never accumulates
  graded rows because it requires ≥35 bps (0.35%) cross-venue spot deviation
  (`Q15_V95_MANIPULATION_DIVERGENCE_BPS`, and the `EXCHANGE_DIVERGENCE` regime is hardcoded to the
  same 35 bps gate at `checkpoint_v95.py:647`). Major venues stay within a few bps via arbitrage, so
  35 bps is a tail event. Corroborated by the latest snapshot: across 910 resolved predictions
  `metrics.by_regime` has only HIGH_VOLATILITY + THRESHOLD_PIN (no EXCHANGE_DIVERGENCE) and
  `by_manipulation.by_reason` has only PIN + ABSORPTION. Fix is operational: lower the (already
  env-tunable) threshold to a realistic band (~8–12 bps) — pending owner's chosen value, since it
  changes the observational scoreboard's composition (it does NOT touch predictions/edge).

## ✅ Shipped THIS session — Shadow vs Yours: synchronized snapshot + Eastern Time + reset
**Merged to main (7e474f0), deploy-pending (Relay syncs main → Repl).** Builds on the window-grading repair below.
- **Simultaneous predictions / shared frozen snapshot:** both systems are already scored from one
  `record_prediction` call (champion features/quote → shadow `observe`); made it auditable by
  threading a shared `snapshot_id` (`f"{checkpoint}@{int(now)}"`, computed once per interval batch
  in `run_cycle`) through `record_prediction → _shadow_observe → runner.observe → ledger.record`,
  stamped on the paired `shadow_predictions` row (+`snapshot_id` column, migrated). Neither system
  fetches newer data; the id locks with the first INSERT-OR-IGNORE write. Also exposed on the
  snapshot as `q15_v9_5_snapshot_id`.
- **Eastern Time (America/Detroit, DST-aware):** new `q15_upgrade/timez.py` (EDT in summer / EST in
  winter via IANA zone — never a fixed offset). All VISIBLE comparison times now render Eastern with
  the EDT/EST label: report reset timestamp, LAST WINDOW label, the recap close label
  (`_recap_close_label`), and an additive dashboard field `q15_v9_5_prediction_timestamp_eastern`.
  **UTC stays internal** (storage, contract matching, the existing ISO field, window bucketing by
  epoch) — display-only conversion; the contract-close instant and interval timing are unchanged
  (verified: `to_eastern(ts).timestamp()==ts`).
- **Synchronized reset:** comparison `model_version` bumped `challenger-v3 → challenger-v4`. The v4
  visible record starts empty (counts only post-reset predictions); v1/v2/v3 rows stay archived in
  the same file as **PRE-SYNCHRONIZED-RESET** (never scored — every query filters on model_version),
  surfaced via `ledger.archived_versions()` and labelled in the report. Post-reset banner now shows
  exactly: `Comparison reset: <Eastern EDT/EST>` · `Synchronization: Same snapshot and prediction
  time` · `Time zone: America/Detroit` · `Shadow: 0W–0L | N/A` · `Your System: 0W–0L | N/A`.
- **Files:** `q15_upgrade/timez.py` (new), `q15_upgrade/challenger/{ledger,runner,config}.py`,
  `q15_upgrade/ledger_v95.py`, `q15_upgrade/checkpoint_v95.py`. **DB:** `shadow_predictions`
  +`snapshot_id` (migrated). New tests `tests/test_challenger_sync_tz_reset.py` (9). Full suite
  **893 passed, 4 skipped**. Read-only shadow only; no model/decision change.

## ✅ Shipped THIS session — Challenger Shadow vs Your System window-grading repair
**Merged to main (7e474f0), deploy-pending (Relay syncs main → Repl).** The deployed report showed Your System as `0W–0L | N/A`
everywhere and Shadow with only one 15M pick (ranks #2/#3 and the 10M/7M intervals missing).
- **Root cause (Shadow ranks/intervals collapsing):** the production canonical stores
  `close_time = now + seconds_remaining` (`checkpoint_v95.py:329`) — a per-asset, per-cycle
  ESTIMATE. The shadow ledger bucketed grading cases by the EXACT close second (`round(close)`),
  so a few seconds of per-asset/per-cycle jitter shattered each window into singleton cases →
  only rank #1 of one asset survived and whole intervals vanished. Reproduced (`n_cases=10`
  instead of 3) and fixed: cases now bucket by the NEAREST 15-minute boundary
  (`_window_id = round(close/900)`; true Kalshi closes are exact multiples of 900s) in
  `_resolved_cases`, `latest_window_cases`, `latest_window_end_results`. Result: 3 cases, all
  three intervals show ranks #1/#2/#3 for both systems.
- **Your System delivery status (was: invisible/blank):** added `native_delivery_status` /
  `native_delivery_error` / `native_delivery_at` columns (migrated, backward-compatible). A
  delivered pick is stamped `SENT` (counts in the visible record); a FAILED official send is now
  recorded as background `DELIVERY_FAILED` with the exact error (out of the visible win/loss
  totals, never silently lost) — wired from the ranked-panel non-mute failure branch via
  `V95Ledger._shadow_mark_failed` → `runner.mark_native_delivery_failed`. The report footer shows
  a delivery audit (`N sent · N failed · N pending`) so an empty "Yours" is explained honestly.
- **Confirmed already-correct (no fabricated changes):** Shadow records EVERY observed pick (no
  strict confidence gate — low-confidence picks are ranked, not suppressed); a 15M never blocks
  10M/7M (independent `UNIQUE(model_version,contract,checkpoint)` rows + independent cases);
  resolve settles all checkpoints per ticker; restart preserves rows and never re-grades.
- **Files:** `q15_upgrade/challenger/ledger.py`, `q15_upgrade/challenger/runner.py`,
  `q15_upgrade/ledger_v95.py`, `q15_upgrade/checkpoint_v95.py`. **DB table:** `shadow_predictions`
  (+3 nullable columns, migrated). New `tests/test_challenger_window_repair.py` (8 tests). Full
  suite **884 passed, 4 skipped**. NOT a model change; read-only shadow only.

## ✅ Shipped THIS session — "updated review" robustness/observability fixes (branch claude/updated-review-99rj6l)
**Merged to main, deploy-pending.** Implemented
every item from the fresh code review, all read-only + backward-compatible (no schema changes):
- **Loop can't be frozen by a feed hint:** `market_data.subscribe()` in `app.py` is now wrapped in
  try/except (logs + continues on REST) — previously the one call that could halt the cycle.
- **Silent learning-layer degradation is now visible:** `ledger.status()` + `/api/health.ledger`
  surface `calibration_unconverged_fallbacks`, `shadow_errors`, `last_shadow_error`. New
  `V95Ledger._note_shadow_error()` counts the previously-swallowed shadow observe/mark_sent/resolve
  exceptions (still read-only; never raises into the alert path).
- **Notifier message_id robustness:** `TelegramNotifier._coerce_message_id` accepts int/float/numeric
  string, rejects junk/bool/NaN; deliveries that arrive without a usable id are counted
  (`delivered_without_id_count`) + logged so the Shadow-vs-Yours record can't silently diverge.
- **Detail fetches isolated:** separate `detail_executor` (4 workers) so slow Kalshi `get_market`
  calls can't starve the freshness-critical fetch pool; `detail_cache` is now pruned of
  non-consumable (inactive / rolled-over ticker) entries.
- **Ranked official report no longer skips silently:** when the top pick's canonical/settlement_time
  isn't built yet, `checkpoint_v95` logs (throttled 60s) instead of dropping the interval quietly.
- **Default-OFF, `Q15_*`-gated model knobs (no behaviour change unless enabled):**
  `Q15_V95_PROMOTION_BONFERRONI` (per-test α/2 on the promotion screen — surfaces `per_test_alpha`),
  `Q15_V95_BRIDGE_MAX_PUBLIC_AGE_SECONDS` (hard freshness fence on the public-composite bridge).
  `_regime_key` is now length-bounded to 64 chars.
- **Tests:** new `tests/test_review_fixes_v2.py` (13 tests) + the app-loop subscribe-guard exercised
  through the real `refresh_loop`. Full suite **876 passed, 4 skipped** (complete env w/ flask +
  websockets; bare-container skip count is higher). App imports clean and serves `/api/health` 200.

## ✅ Shipped THIS session — challenger learning view in the stats command
**On the branch, deploy-pending.** Owner asked how to check the CHALLENGER's learning.
`scripts/stats.py` now prints a CHALLENGER section: (1) the shadow MODEL vs your system —
challenger vs current accuracy overall + per interval and the ranked W/L (from the
`q15_upgrade/challenger` package's `ShadowLedger.comparison`/`ranked_comparison`, keyed on the
configured `model_version`); and (2) the ledger's online champion-vs-challenger WEIGHT learning
(`shadow_updates_applied`, per-checkpoint, regime challengers active). Read-only, try/except-
guarded (never crashes the command), degrades to a hint when no challenger DB exists. Points to
`scripts/challenger_eval.py` for the full out-of-sample verdict. Test in test_q15_stats_cli.py.
Full suite **863 passed, 4 skipped**.

## ✅ Shipped THIS session — factor-lab combinations broken down by interval
**On the branch, deploy-pending.** Owner wanted to see whether the strongest agreeing factor
pairs are stronger at 15M vs 10M vs 7M. `factor_lab._combos` now attaches a per-interval
breakdown to each combination, and `format_report` renders it under each pair
(`15M xx%(n)  10M xx%(n)  7M xx%(n)`). `scripts/stats.py` shows it automatically. Read-only;
no model change. Tests in `tests/test_q15_factor_lab.py`. Full suite **863 passed, 4 skipped**.

## ✅ Shipped THIS session — consolidated all pure Telegram code into a `notifications/` package
**On the branch, deploy-pending.** Owner: put all Telegram-notification files in one folder.
Moved the PURE delivery/formatting modules into a new top-level `notifications/` package (history
preserved via `git mv`): `notifier.py`, `reporting.py`, `alert_config.py` (from root) and
`panels_v95.py`, `manipulation_alert.py`, `outbox_v9.py` (from `q15_upgrade/`).

- **⚠️ Package name MUST NOT be `telegram`**: the first pass named it `telegram/`, which on the
  GCE deployment image collided with the preinstalled `python-telegram-bot` top-level `telegram`
  module — `from telegram import panels_v95` resolved to that lib, the v95 import failed, and the
  owner got NO checks. Renamed to `notifications/` (collision-proof). Keep it that way.
- **Import sites updated**: `app.py` (4), `q15_upgrade/checkpoint_v95.py` (2:
  `from notifications import panels_v95 / manipulation_alert`), and ~10 test files.
  `notifications/__init__.py` is intentionally import-light (no eager submodule imports) so the
  notifier ⇄ checkpoint_v95 formatter chain has no circular-import hazard.
- **Left in place (documented):** the frozen `format_telegram_message` reformatter chain in
  `q15_upgrade/checkpoint_v9{1..5}.py` (formatting welded into frozen decision code — can't move
  without editing frozen files); and the hourly-report builders `q15_upgrade/{setup_miner,
  shadow_economics,accuracy_report}.py` (learning/analysis modules with a report method, not pure
  Telegram). `professional_v7`/`calibrated_edge` reformatters also stay (mixed with their engines).
- **Docs**: `CLAUDE.md` + `README.md` updated to the new layout. Full suite **861 passed,
  4 skipped**; `import app` + refresh loop start clean.

## ✅ Shipped THIS session — fix duplicate report every ~minute (don't unlock on send FAILURE) + min-gap backstop
**On the branch, deploy-pending.** Owner clarified: SINGLE deployment, one report arriving
roughly every minute — a resend loop, not multiple instances. **Root cause:**
`_send_ranked_panel` released the report lock on ANY `not delivered`, including an ambiguous send
FAILURE (HTTP timeout / 429 rate-limit). On a 429 the message often DID reach Telegram, but we
read it as failed, unlocked, and re-sent next cycle — throttled by the rate limit to ~one
duplicate per minute. **Fix:** unlock ONLY on an intentional MUTE (nothing was sent); on a
failure KEEP the lock (one attempt per window). Still warns on handled-but-not-delivered. Tests:
`test_failed_send_keeps_lock_no_resend_loop`, `test_muted_send_releases_lock_for_retry`.

Plus a defense-in-depth **per-interval minimum-gap backstop** independent of the window key (for
the other single-process cause — an unstable window key from a contract-mapping flip near a
:00/:15/:30/:45 boundary, or a restart re-fire):

- `V95Ledger.last_official_report_at(interval)` = `MAX(locked_at)` over `official_report_lock`
  for that interval (all windows). `_send_ranked_panel` now refuses to send the SAME interval's
  report within `Q15_V95_REPORT_MIN_GAP_SECONDS` (default 600, clamp ≤870). Legitimate same-
  interval reports are ~900s apart (one per 15-min contract), so a sub-900 gap can only ever
  block a duplicate — never the next window's report. Checked BEFORE the lock claim/send.
- **Note for next session:** if duplicates persist after this, the cause is almost certainly
  MULTIPLE BOT INSTANCES sharing the Telegram token (e.g. the Replit editor "Run" process AND a
  Deployment, each with its own `data/` ledger → separate locks). That is infra, not code — the
  owner must stop one instance. Asked the owner to confirm instance count.
- **Tests** (`test_q15_ranked_panel.py`, `test_q15_v95_single_alert.py`): `last_official_report_at`
  tracks the latest per interval; the gap guard blocks a duplicate before any send/lock. Caught a
  self-inflicted bug too — an edit had dropped the `official_scoreboard` signature; restored, the
  full suite is **859 passed, 4 skipped**; app imports clean.

## ✅ Shipped THIS session — official interval report ALWAYS delivers (fixes empty "Your System")
**On the branch, deploy-pending.** Owner reported the bot "not putting out anything" and the
Challenger-Shadow "Your System" record stuck at `0W–0L` while Shadow filled. Root cause: under
the default `balanced` alert level, the official interval report carries `NO ENTRY YET` and was
MUTED on no-entry intervals — and "Your System" records only DELIVERED predictions
(`_shadow_mark_sent` runs after the delivered-gate), so it never filled. But the spec mandates
one 15M/10M/7M check EVERY interval with the three ranked picks.

- **Fix**: the ranked panel now stamps its header `· TOP 3 PICKS ·`
  (`panels_v95.build_ranked_checkpoint_panel`), and `notifier.should_suppress_alert` always
  delivers a message carrying that marker — overriding the NO-ENTRY mute — gated by
  `Q15_V95_RANKED_REPORT_ALWAYS_DELIVER` (default ON; `false` restores the old muting).
  Routine/legacy `V9.5 CHECK · NO ENTRY YET` checks are unaffected (still muted). The
  per-window lock still guarantees exactly ONE official report per interval, so this delivers
  3 checks per 15-min contract (the mandated cadence), not spam. Now each delivered report runs
  `_shadow_mark_sent`, so "Your System" fills and the owner sees every interval's picks.
- **Tests**: `tests/test_q15_alert_suppression.py` (official delivers under balanced; flag-off
  re-mutes; entry official delivers; routine still muted) + `tests/test_q15_ranked_panel.py`
  (header carries `TOP 3 PICKS`). Full suite **857 passed, 4 skipped**; app imports clean.

## ✅ Shipped THIS session (branch `claude/prediction-system-rebuild-ogptop`) — compact final-outcome interval reports
**On the branch, deploy-pending.** Reworked the visible 15M/10M/7M CHECK report from the
verbose per-pick dump into the compact, final-outcome format the owner asked for:
"communicate simply, analyze deeply in the background." The detailed
learning/grading/calibration/shadow machinery is untouched — only the *visible* surface
changed. Read-only + frozen-champion invariants intact.

- **Compact report** (`panels_v95.build_ranked_checkpoint_panel`): header keeps the
  `V9.5 CHECK` + `ENTRY RECOMMENDED`/`NO ENTRY YET` suppression markers; body is now just
  the three ranked final-outcome picks (`🥇 SOL NO — 72%`, confidence = P(settles on the
  predicted side)) plus ONE decision block keyed to the headline (#1) pick: `Flip risk`
  (with `→ SIDE` only when ≥35 and toward the opposite side), `Manipulation` (with
  `— WAIT` only when ≥60), `Entry` (ENTER/WAIT/SKIP), `Best entry` (`≤max¢`), one
  `Main reason`, and `Sample`. The old per-pick P(Yes)/entry-score/wick/flow/edge/decision
  lines are gone from the visible report. Missing rank → `—` (never invented).
- **Final-outcome framing** (`checkpoint_v95._extract_pick` + helpers): each pick now also
  carries `flip_prob`/`flip_side` (genuine-flip risk = flip-risk score; target = monitored
  opposite side), `manip_prob` (temporary-manipulation = the manipulation block's own 0..1
  score rescaled — kept DISTINCT from flip risk), `entry_label` (ENTER/WAIT/SKIP via
  `_ENTRY_LABEL_BY_DECISION`), `best_entry_max`, `main_reason` (`_main_reason`: strongest
  side-aligned feature, else "<SIDE> most likely at close" — no invented number), and
  `sample` (calibration rows). Detail fields the record path/shadow overlays read are
  retained, so grading, the official record, the one-report-per-window lock, restart
  protection, and the shadow comparison are unchanged.
- **Tests** (`tests/test_q15_ranked_panel.py`): rewrote the panel-format suite for the
  compact layout + new fields (flip arrow gating, manipulation `— WAIT`, ENTER/WAIT/SKIP,
  `Sample: —` when no calibration, confidence oriented to the predicted side). Full suite
  **836 passed, 4 skipped**; `import app` starts the refresh loop cleanly.
- **One-report-per-interval fix** (`checkpoint_v95._send_ranked_panel`): the official
  interval report is now suppressed when the top asset's settlement window is unknown
  (`window_close is None`). A `None` close fell back to `now // 900` — a DIFFERENT bucket
  than the real settlement window — so once the canonical appeared the per-window lock saw
  a fresh key and a SECOND report fired. Now it waits one cycle for the real close, so the
  lock key is always the settlement window and the interval is delivered exactly once.
  The deliberate "take its time, but within the minute" behavior is the existing
  `_decision_settled` gate (stable verdict for `Q15_V95_DECISION_STABILITY_CYCLES` cycles +
  hold until the named 15:00/10:00/7:00 mark + a force-send safety net as the band closes).
  Regression test in `tests/test_q15_v95_single_alert.py`.

## ✅ Shipped THIS session — cross-asset shadow factors (new reliable data)
**On the branch, deploy-pending.** Adds the genuinely-new reliable data the codebase can
collect TODAY with no external feed: cross-asset / correlated-movement signals. Each cycle
already holds all 7 assets' snapshots, so broad-market + relative-strength factors are free
and leakage-free. Recorded for the factor lab ONLY, isolated from the frozen champion.

- **`q15_upgrade/shadow_factors.py`** (pure): `compute_market(analyses)` → mean momentum/
  flow across assets + BTC (leader) momentum; `for_asset()` → YES-signed factors
  `x_market_momentum`, `x_market_flow`, `x_leader_momentum`, `x_rel_strength`
  (asset − market), `x_div_from_leader` (asset − BTC). Same sign convention as the champion
  features so the lab grades them identically.
- **Isolation**: stored in a NEW `predictions.shadow_factor_json` column — never in
  `feature_json`. The champion/challenger/calibration/pattern overlay read only their known
  feature names, so this cannot perturb a live decision. `record_prediction(shadow_factors=)`
  writes it in the same transaction (insert-only); `resolved_factor_rows` merges it back so
  the lab + `scripts/stats.py` grade the `x_*` factors automatically. Default-ON flag
  `Q15_V95_SHADOW_FACTORS_ENABLED` (pure rollback switch).
- **Already-tracked richer data** the lab now also surfaces: `derivatives` (Deribit
  funding/OI-derived), `exchange_consensus` (cross-exchange spot), `absorption`, `context`,
  `threshold_interaction` were always recorded — the lab grades all of them.
- **Still needs a real feed (NOT faked):** sentiment, social, on-chain, calendar/seasonality
  as a *signed* factor. Each is a separate ingestion task with a verified source.
- **Tests** (`tests/test_q15_shadow_factors.py` + extended `test_q15_stats_cli.py`): market
  aggregation/skip-missing, signed relative factors, leader self-divergence = 0, and the
  isolated round-trip into the export. Full suite **854 passed, 4 skipped**; app imports +
  refresh loop start clean.

## ✅ Shipped THIS session — shadow FACTOR LAB + all-stats CLI
**On the branch, deploy-pending.** Adds a read-only, shadow-mode factor-attribution
layer over the official predictions the system ALREADY records, plus a single command to
dump every stat on the Repl. Nothing here is auto-applied: the frozen champion, live
predictions, and entry decisions are untouched; promotion stays manual.

- **No new data collection needed** — `predictions.feature_json` already stores every
  decision-time factor per prediction, graded against the settled `official_result`. The
  new `V95Ledger.resolved_factor_rows([checkpoint])` exports those settled rows (factors +
  final result + `correct`/`changed_before_close` targets); factors are decision-time only,
  so there is no look-ahead in the inputs.
- **`q15_upgrade/factor_lab.py`** (pure, dependency-free, deterministic): per factor,
  overall and split by interval/regime — *fired* count, *final-aligned reliability* with a
  Wilson lower bound (the rest were temporary/misleading moves), *probability shift*
  (YES-rate when it leaned YES minus when it leaned NO), *accuracy lift* vs base, and
  *final-change rate*. Plus agreeing-pair **combinations** and a **promotion gate** that
  marks a factor *promotion-ready* only when it clears: enough samples, Wilson-LB
  reliability ≥ threshold, out-of-sample consistency across a time split, and a measurable
  shift. The gate REPORTS readiness; it never applies anything.
- **`scripts/stats.py`** — the one command: `python3 scripts/stats.py` prints ledger
  status, the official W/L record (15M/10M/7M · YES/NO · entry · manip), and the factor
  lab. Flags: `--ledger PATH`, `--checkpoint 10M`, `--detail`, `--min-samples`,
  `--reliability`, `--deadzone`. Read-only; opens the real `data/` ledger.
- **Not yet collected:** funding/OI/sentiment/correlated-asset factors aren't in
  `feature_json` (no verified feed exists), so the lab analyses the factors actually
  present and does not fabricate the rest. Adding a new source is a separate ingestion task.
- **Tests** (`tests/test_q15_factor_lab.py`, `tests/test_q15_stats_cli.py`): Wilson bound,
  reliability/deadzone/misleading-factor math, all four promotion-gate rejections, the
  export shape + filter, and the CLI end-to-end against a real temp ledger. Full suite
  **848 passed, 4 skipped**.

## ✅ Shipped (branch `claude/shadow-system-reset-aeilgv`) — RESET the Challenger Shadow vs Your System comparison on deploy
**On the branch, deploy-pending (needs a Repl restart to pick up the `model_version`
v2→v3 reset).** Owner: reset the visible "CHALLENGER SHADOW vs YOUR SYSTEM" comparison
on this deploy — all Shadow + Your-System wins/losses/win-rates/samples/last-window/
#1·#2·#3 rank records (separately for 15M/10M/7M) back to zero; grade only predictions
made AFTER the reset; clear dormant/incomplete/duplicate/mis-formatted rows from the
visible record; keep the old raw data archived (PRE-RESET) for debugging; show
`Comparison reset: <ts>` and `0W–0L | N/A` until the first new prediction settles; keep
the three-pick 15M/10M/7M format. Read-only/observational throughout; frozen champion
untouched.

- **Reset = one model_version bump** (`config.py`): default `Q15_CHALLENGER_MODEL_VERSION`
  `challenger-v2`→`challenger-v3`. Every scoring query (`ranked_comparison`,
  `ranked_by_checkpoint`, `latest_window_cases`, `latest_window_end_results`,
  `comparison`, `scoreboard`) filters on `model_version`, so v3 starts completely empty
  while ALL prior-version rows (v1 + v2) survive untouched in the same SQLite file as an
  internal PRE-RESET archive — never deleted, never mixed in. Dormant/incomplete/
  duplicate/mis-formatted rows are pre-reset rows by construction → excluded from the new
  visible record. `ledger.reset_marker` stamps v3's reset instant on its first record
  (shown as `Comparison reset: <UTC>`); only post-reset predictions are graded. The
  3-pick 15M/10M/7M report format (`REPORT_CHECKPOINTS`, `report_message`) is unchanged
  and already shows `0W–0L | N/A` per interval until the first v3 case settles. No env
  override active (`.env.example` line documents the new default, commented out).
- **Tests** (`tests/test_challenger_runner.py`, +2): pin the default `model_version` to
  `challenger-v3` (a forgotten reset / accidental edit now fails loudly) and verify a
  fresh v3 record is empty while v1+v2 rows stay archived in the file and a new
  post-reset prediction starts the comparison. Full suite **832 passed, 4 skipped**.

## ✅ Shipped (branch `claude/monitor-challenger-shadow-system-y4yu2p`) — official interval reports: 3 ranked picks + report-frequency lock (prompt sections 1 & 2)
**On the branch, deploy-pending.** Completes the two remaining sections of the original
prompt — the *official* checkpoint reports (the rest was the read-only challenger
comparison, already merged). Touches the live alert path; gated + test-backed; the
frozen champion and the read-only invariant are untouched (the entry score is a SHADOW
overlay, never a driver).

- **Section 2 — 3 ranked picks per official report** (`panels_v95.build_ranked_checkpoint_panel`
  + `checkpoint_v95._build_ranked_picks`/`_extract_pick`): each 15M/10M/7M report now
  carries the top-3 picks (existing executable-trade ranking order) with every field the
  system calculates — asset, YES/NO, confidence %, P(Yes)/P(No), **entry score**,
  manipulation %, current price, recommended + max entry, wick/price-action status,
  flow/momentum status, edge, final decision. Fewer than 3 valid assets → `—` for the
  missing rank (never invented). Keeps the `V9.5 CHECK` + `ENTRY RECOMMENDED`/`NO ENTRY
  YET` markers (single `<pre>`).
- **Entry score** (`checkpoint_v95._entry_score`): a 0–100 read-only SHADOW composite with
  the documented weights (30 dir-conf / 25 edge / 20 wick / 15 momentum / 10 manip), each
  mapped to 0..1; returns `—` when the prediction is unavailable (no fabricated number).
  Edge cap tunable via `Q15_V95_ENTRY_SCORE_EDGE_CAP`. Does NOT affect ranking or the live
  entry decision.
- **Section 1 — one report per interval, locked** (`ledger_v95.report_locked` /
  `lock_official_report` / `unlock_official_report`, new `official_report_lock` table keyed
  `(model_version, interval, 15-min window)`): exactly ONE official 15M/10M/7M report per
  contract-window. The lock is CLAIMED before the send (cross-process dedup) and RELEASED
  only if the send didn't deliver (muted/failed → retries next cycle); a delivered report
  is locked permanently — later cycles keep analysing in the background but never resend or
  replace it. `_send_ranked_panel` writes the immutable official record for EVERY delivered
  pick (and marks each in the Challenger-Shadow "Your System" sent record, which now fills
  ranks #2/#3 too), arming slot/follow-up for the top entry only.
- **Wiring:** `run_cycle` calls `_send_ranked_panel` under `Q15_V95_RANKED_PANEL` (default
  ON; OFF = legacy single-pick `_send_compact_panel`).
- **Tests:** `tests/test_q15_ranked_panel.py` (+14) — entry-score formula/bounds/None,
  pick extraction (incl. NO-side confidence), top-3 build (skips unavailable / leaves short),
  panel render (all fields, missing rank `—`, markers), lock semantics (one per window,
  reject second claim, unlock-retry, independent intervals/windows). The existing run-cycle
  integration tests now exercise the ranked path (one send across cycles, official record
  written). Full suite **830 passed, 4 skipped**.

## ✅ Shipped THIS session (branch `claude/monitor-challenger-shadow-system-y4yu2p`, MERGED to `main`) — Challenger Shadow vs Your System rework + RESET
**Merged to `main` (owner-approved) — deploy-pending (needs a Repl restart to pick up
the `model_version` v1→v2 reset).**
Owner: fix/reactivate the "CHALLENGER SHADOW vs YOUR SYSTEM" comparison — it must
not stay dormant/empty/one-pick — to 3 ranked picks per interval across ALL THREE
intervals (15M/10M/7M), a clearly-graded end-result call, all-time per-rank/per-
interval records with combined totals, ✓/✗/— symbols only, and a RESET on deploy.
Read-only/observational throughout; the frozen champion is untouched.

- **7M is now a first-class interval** everywhere in the comparison (was 15M/10M
  only). New `ledger.REPORT_CHECKPOINTS=("15M","10M","7M")`; `ranked_by_checkpoint`,
  `latest_window_end_results`, and the report all iterate it. 7M reads
  `0W–0L | N/A` until its cases settle (safe even if 7M rows don't flow yet).
- **Reset that archives, not deletes** (`config.py`): default
  `Q15_CHALLENGER_MODEL_VERSION` bumped `challenger-v1`→`challenger-v2`. All scoring
  keys on `model_version`, so the old v1 rows survive in the SAME SQLite file as an
  internal PRE-RESET archive (debug only) while the new comparison starts at
  `0W–0L | N/A`. New `Q15_CHALLENGER_RESET_AT` (epoch, optional) + `shadow_meta`
  table + `ledger.reset_marker()` stamp the reset instant ONCE (stable), shown as
  `Comparison reset: <UTC>`.
- **New all-time records** (`ledger.ranked_by_checkpoint`): per interval, per rank
  1..3, per system {correct,wrong,accuracy} + a per-interval total. Each resolved
  case contributes ≤1 result per (interval, rank) — the UNIQUE
  (model_version, contract, checkpoint) row key + per-rank scoring = graded once
  (duplicate protection).
- **Report rewrite** (`runner.report_message`): one bold `CHALLENGER SHADOW vs YOUR
  SYSTEM` title + one `<pre>` block (notifier bypass marker preserved). LAST WINDOW
  shows `#k Shadow: SOL NO ✓ | Yours: SOL NO ✓` for each of 15M/10M/7M; END-RESULT
  CALL grades all ranks across all three intervals; ALL-TIME RANK RESULTS renders
  SHADOW RECORD + YOUR SYSTEM RECORD + per-interval TOTALs. Only ✓/✗/— marks (no
  bare ok/X/+/-). Missing rank/asset → `—` (never an invented pick).
- **Tests** (`tests/test_challenger_runner.py`, +5; one updated to the new format):
  symbol-only + all-section render, `ranked_by_checkpoint` per-rank/interval incl.
  empty 10M, reset-marker stamped-once + pre-reset archive excluded, empty report
  shows reset + `0W–0L | N/A`. `.env.example` documents the challenger flags.
  Full suite **813 passed, 4 skipped**.
- **Official grading rule — Your System counts only DELIVERED predictions** (follow-up
  on this branch, post-merge): the visible "Yours" record now counts a native prediction
  only if it was actually sent before close. New `native_sent` column (+ guarded SQLite
  migration) on `shadow_predictions`; `ShadowLedger.mark_native_sent` (idempotent),
  `runner.mark_native_sent`, and `ledger_v95._shadow_mark_sent`. Wired in
  `checkpoint_v95._send_compact_panel` on a REAL Telegram delivery (read-only wrt
  production; the observe row is recorded first at line 2081, the send marks it at 2171).
  All visible native scoring (`ranked_comparison`, `ranked_by_checkpoint`,
  `latest_window_cases`, `latest_window_end_results`, `comparison`) is sent-gated; the
  Shadow side is exempt (read-only test, still created before close). Unsent predictions
  stay as internal background rows (graded for learning, never in the visible record).
  Gated `Q15_CHALLENGER_NATIVE_SENT_ONLY` (default ON; OFF = legacy count-all). +4 tests
  (unsent-excluded-but-shadow-counts, mark promotes + idempotent, legacy-DB migration,
  delivered-vs-undelivered end-to-end). Full suite **816 passed, 4 skipped**.
  ⚠️ In production only the TOP pick's panel is delivered per checkpoint+window today, so
  the visible Yours record populates rank #1 (ranks #2/#3 will fill once the official
  interval report sends 3 ranked picks — the separate "RANKED PICKS" item).

## ✅ Shipped THIS session (branch `claude/crypto-prediction-monitor-ygugze`) — engineering-standard meta-prompt persisted
**Merged to `main` (docs/config only, no behavior change). Tests: 809 passed, 12 skipped.**
Owner pasted a "Staff Engineer" meta-prompt and asked to persist it so every new
session reads it first.
- **`ENGINEERING_GUIDELINES.md` (new):** the full meta-prompt as the standing
  engineering standard — system context, engineering approach, coding standards,
  reliability requirements (WebSocket / Postgres / Telegram / model-chain),
  testing, security, response format, adversarial review.
- **`CLAUDE.md`:** a prominent **READ FIRST** callout at the very top points to it.
- **`.claude/settings.json` (new):** a `SessionStart` hook injects a one-line
  reminder to read `ENGINEERING_GUIDELINES.md` into every session's context.

## ✅ Shipped THIS session (branch `claude/review-update-handoff-1zful1`, MERGED to `main`) — resolution health check
**Merged to `main` — deploy-pending (Relay syncs `main`→Repl; run it there).** Ran a
fresh `updated-review` (held at **7.5/10**; ceiling is data/calibration, not code — all
fan-out "criticals" collapsed on verification: locked `_LATEST_*` writes, in-code-documented
multiple-comparison + chosen-side ECE choices, and the entry ladder blocks a >30s quote at
`WATCH_LIQUIDITY` before the edge gate). Then, diagnosing why the v95 ledger shows ~0
resolved: confirmed it's **environmental, not a bug** — settlement reconciliation IS wired
into the live loop (`checkpoint_v95.run_cycle` calls `reconcile_from_signal_store` +
`reconcile_pending_from_market(get_market, now)` every 30s, `ledger_v95.py:2129-2143`); a
fresh CI clone just has no live exchange and the scoreboard was reset by the `MODEL_VERSION`
bump (re-baking). A row is "resolved" only when its Kalshi market settles YES/NO and that
official result is written back (`official_result IS NOT NULL`); closed ≠ resolved.
- **Tool (`scripts/check_resolution.py`, READ-ONLY `mode=ro`, run ON THE REPL):** per-checkpoint
  total/resolved/pending/stuck, calibration readiness vs the 30-row threshold, settlement
  recency, optional `/api/health` reconcile counters, and an EMPTY/BAKING/PROGRESSING/STUCK/HEALTHY
  verdict. Flags the Relay-paused / no-`get_market` failure mode. Imports the canonical
  `MODEL_VERSION` (adds repo root to `sys.path`). No new tests (pure read-only diagnostic).
- Suite green: **809 passed, 4 skipped**.

## ✅ Shipped THIS session (branch `claude/replit-workspace-connect-pfspn4`) — "is anything 100% correct?" forensic audit
**On the branch, NOT merged to `main` (per this session's branch policy), deploy-pending.**
Owner asked, as a Staff quant: has any variable / rule / combination historically
predicted with a *credible* 100% win rate — not just a displayed 100%, but one free
of leakage, duplicate-counting, and overfitting, that survives out-of-sample? Built a
read-only forensic rule-miner to answer it honestly. **Cannot be run from a CI clone —
`data/` is schema-only/empty here; the settled history lives ONLY on the Repl.**

- **Tool (`scripts/perfect_condition_audit.py`, READ-ONLY, run ON THE REPL):** opens
  the v95 ledger `mode=ro`, mines single-variable / threshold / pair / triple rules
  (≤3 vars by default) for 100% conditions, with the controls that make a 100% claim
  trustworthy instead of a data-snooping artifact:
  - **Leakage firewall** — only fields known at `created_at` can be rule variables;
    `official_result`/`correct`/`realized_cents`/`*_brier`/`*_logloss`/`resolved_at`
    and post-decision `changed_before_close`/`learning_applied` are denylisted (the
    miner *raises* if one slips in). The target is recomputed `predicted_side ==
    official_result` and cross-checked vs the stored `correct` flag (integrity guard).
  - **No duplicate counting** — the ledger stores one row per `(ticker,checkpoint)`, so
    a market appears ≤3× and those rows are correlated. ALL stats (sample tier, Wilson
    CI, multiple-testing) use UNIQUE MARKETS, and a market is a rule "win" only if EVERY
    matched checkpoint row was correct (conservative collapse).
  - **Chronological 60/20/20** split on MARKET boundaries (a market never straddles a
    split); thresholds chosen from discovery quantiles only; test set untouched until
    the final table. "Historical 100%" = discovery-perfect, then *classified* by its
    OOS fate (CREDIBLE only if perfect through the untouched test set with ≥20 markets).
  - **Multiple-testing honesty** — counts every hypothesis and reports the expected
    number of spurious perfect rules under the null (Σ base_acc**n), plus `p_perfect_by
    _chance` and a rule-of-three reminder that a finite 100% never proves a 100% future.
  - Importable `audit()` + CLI `main()` (`--max-vars`, `--min-test-markets`, `--json`).
- **Tests:** `tests/test_perfect_condition_audit.py` (+22, deterministic, adversarial):
  duplicate-market collapse, leakage firewall + denylist, target recompute/integrity,
  chronological split (no straddle), threshold boundary inclusivity, missing/invalid
  rows, tiny-sample rejection, multiple-testing accounting, an OOS rule that's perfect
  early then dies (rejected) vs a genuinely separable rule that survives (credible),
  read-only-open (won't create/mutate the db), and run-to-run reproducibility.
- Suite green locally: **737 passed, 12 skipped**.
- **Executive answer until run on real Repl data:** *cannot* be stated from here — the
  conclusion is whatever the tool emits on the Repl. Run `python3 scripts/perfect_
  condition_audit.py` and read the EXECUTIVE CONCLUSION line; expect "No perfect
  condition found" or "found but failed validation / insufficient sample" unless a rule
  truly stays 100% on the untouched recent test set. No historical pattern should ever
  be called guaranteed for future markets.

## ✅ Shipped (branch `claude/manipulation-score-review-ibt7i0`) — manipulation-score refactor
**On the branch, NOT merged to `main` (per this session's branch policy), deploy-pending.**
Reviewed and clarified the suspected-manipulation score (`_manipulation_signal` in
`checkpoint_v95.py`) without changing its output. Split the monolith into small,
named, type-hinted helpers (`_manipulation_divergence_threshold_bps`,
`_absorption_lean`, `_collect_manipulation_tells`, `_manipulation_score`); replaced
magic numbers with documented constants (3 tells, 0.34 absorption bonus, 1.0 cap,
35-bps divergence band) with **explicit units** (basis points / 0..1 score). Added an
optional **per-asset** divergence band (`Q15_V95_MANIPULATION_DIVERGENCE_BPS_<ASSET>`,
defaults to the global 35-bps so behaviour is unchanged when unset) and a debug log on
suspicion only. Fixed a latent **unit mismatch** in `_panel_manipulation`: the
fallback manipulation score (0..1) is now rescaled to the panel's 0..100 `risk`/level
scale (the flip-risk path was always 0..100; the fallback is effectively dead in
production since flip-risk is attached every cycle). Verified byte-for-byte output
parity on 10 representative inputs. Suite **+27 manipulation tests**
(`tests/test_q15_v95_manipulation.py`: 13 → 40), full run **740 passed, 12 skipped**.

## ✅ Shipped (branch `claude/replit-workspace-connect-pfspn4`) — shadow challenger was feature-starved
**On the branch, NOT merged to `main` (per this session's branch policy), deploy-pending.**
Diagnosed "shadow learning system doing horrible": the read-only challenger was
**blind to the dominant drivers of a 15-minute binary**. The production prediction
only forwarded the Kalshi bid/ask quote to the shadow (`_selected_quote` returns
just `bid/ask/spread`), so the challenger's distance-to-target, time-decay, and
volatility features all extracted as **zeros** at both train and predict time — it
was effectively learning from the market price plus the 9 compressed champion
signals only.

- **Fix (`checkpoint_v95.py`):** `analysis["quote"]` now also carries `spot`,
  `target` (strike), `seconds_remaining`, `volatility_per_min`, `depth_contracts`,
  and `data_quality`. Keys are additive — existing readers only touch
  `bid/ask/spread`. New helper `_shadow_vol_per_min()` converts the robust
  `sigma_per_sqrt_second` → per-minute fractional vol (`× √60`).
- **Fix (`challenger/extract_bridge.py`):** `_row_to_snapshot` now also reads
  passthrough context from `quote_json`, so future offline retrains /
  `walk_forward_evaluate` aren't blind either (pre-fix production rows still lack
  it retroactively).
- **Tests:** `tests/test_q15_v95_shadow_features.py` (+5) — vol conversion, quote
  carries context end-to-end through `analyse_v95`, the challenger feature vector is
  now live (pct/log/normalized distance + minutes-remaining non-zero, coverage True),
  plus a guard test proving the old bid/ask-only quote collapsed those to zero.
- **Measurement tool (`scripts/challenger_eval.py`, READ-ONLY, run ON THE REPL):**
  exports resolved production rows, reconstructs the same decision-time features,
  and runs a purged walk-forward — POOLED vs PER-CHECKPOINT — reporting challenger
  OOS log-loss/Brier vs the market-only / volatility-only baselines AND vs the frozen
  champion (control prob). This is the evidence gate for the per-checkpoint split
  decision (don't split blindly; each split sees ⅓ the data). Importable
  `evaluate_ledger()` + CLI `main()`. Tests: `tests/test_challenger_eval_script.py` (+5).
  Run: `python3 scripts/challenger_eval.py` (or `--min-rows 60 --splits 4` on thin data).
- Suite green locally: **715 passed, 12 skipped** (10 new this session; higher skip
  count than the 734-env figure above only because `flask`/`websockets` weren't
  installed here → the two app-level files skip).
- ⚠️ Caveat to owner: the quote fix is the *plumbing* fix. The challenger is still one
  pooled, L2=10 logistic across all assets/checkpoints vs a specialized champion, so
  it may still trail until that's addressed; and it needs `min_train_rows=200` before
  it stops mirroring the market mid. **Decision held for evidence:** run
  `scripts/challenger_eval.py` on the Repl first — split per-checkpoint only if those
  splits actually beat POOLED out-of-sample.

## ✅ Shipped (branch `claude/updated-review-vy09iw`) — updated-review fixes
**On the branch, NOT merged to `main` (per this session's branch policy), deploy-pending.**
Ran a fresh fan-out `updated-review` (held at **7.5/10** — no code had moved since
the prior review), then implemented every actionable item. Adversarial verification
again collapsed several fan-out "criticals" (the calibration `_data_version` cache
re-checks under lock before storing at `ledger_v95.py:1591`; both `target`-division
sites already guard `0` via `and target`; the per-checkpoint alert-level env read
already `.strip().lower()`s at `notifier.py:114`) — those were left untouched. Suite
**713 → 720 passed, 12 skipped** (+7). All changes `Q15_*`-gated and reversible.

- **Highest — stale-spot fails closed honestly** (`v5_hardening.apply_snapshot_freshness`):
  the bounded last-good fallback re-stamps `ts=now` for candle continuity, which let a
  stale underlying read as fresh past the freshness gate. Now (default-ON
  `Q15_V5_GATE_STALE_SPOT`) the gate honors `original_ts`, so a 25s-old price reports its
  TRUE age and trips `spot_stale_…` → `v5_data_valid=False`. Tests in
  `test_q15_v5_fail_closed.py` (+3: gated true-age, within-budget still valid, gate-off
  legacy path).
- **Medium — public-price freshness is tunable** (`checkpoint_v95.build_canonical_snapshot`):
  the previously-hardcoded `exp(-(age-5)/30)` is now `Q15_V95_PUBLIC_PRICE_GRACE_SECONDS` /
  `Q15_V95_PUBLIC_PRICE_DECAY_SECONDS` (named honestly — tau is an e-folding constant, NOT
  a half-life). Test asserts a tighter decay lowers `data_quality` for the same snapshot.
- **Medium — ECE in the scoreboard** (`ledger_v95.metrics`): added
  `expected_calibration_error` = count-weighted mean |predicted−actual| over the 50–100%
  bands (observational only, never steers a decision). Tests in
  `test_q15_learning_scoreboard.py` (+2: 0.20 ECE case, None without resolved rows).
- **Medium — in-flight TTL + executor shutdown** (`app._harvest_and_submit`, `refresh_loop`):
  a permanently hung fetch is now cancelled/dropped past `Q15_FETCH_INFLIGHT_TTL_S` (60s)
  so `inflight` can't grow unbounded; `inflight` now maps `asset -> (Future, submitted_at)`.
  The pool is shut down on the bounded-test return and via `atexit` for the forever-loop.
  Test in `test_app_fetch_inflight.py` (+1: abandon-and-replace past TTL).
- **Polish — return-coordinate contract** (`checkpoint_v95._multi_horizon_returns`):
  documented the candle=log / public=simple-fractional contract and now drop a public
  return outside (−1, 1) (percent-scaled / already-log) before `log1p`, which would
  otherwise raise/-inf. Test in `test_q15_v95.py` (+1, plus the freshness test = +2 there).

## ✅ Shipped earlier (branch `claude/read-hand-off-719tb9`) — updated-review fixes
**On the branch, NOT merged to `main` (per this session's branch policy), deploy-pending.**
Ran a fresh fan-out `updated-review` (overall **7.5/10**, up from 7.0), then
adversarially verified every "critical/high" finding — the three headline bugs all
collapsed (calibration cache already re-checks `_data_version` under lock at
`ledger_v95.py:1525`; a stale core snapshot IS rejected via `core_valid=not errors`
at `checkpoint_v95.py:344,374`; the "engine-state race" is a GIL-atomic float read on
a diagnostics route). The real holdback was **test coverage of degraded paths**, so
that's where most of the work went. Suite **691 → 714 passed, 4 skipped** (+23).

- **Highest — 4 degraded-path loop tests** (`tests/test_app_loop_degraded_paths.py`,
  the headline gap): market expiry → cycling and **recovery** when a fresh market
  appears; `get_orderbook()→None`, `get_trades()→None`, and both-None feeds ingested
  through the real loop without crashing/freezing the dashboard; a
  `deep_evaluation_snapshots` crash isolated to `deep_snaps={}` with signals/scalp
  still running on the empty fallback (not skipped, not stale).
- **Medium — alert-path visibility** (`checkpoint_v95._send_compact_panel`): a new
  throttled `_throttled_warn` (≤1 WARNING / 60s / key) surfaces (a) a
  `reserve_notification` that returns no permit (dedup OR ledger hiccup) and (b) a
  handled-but-**not-delivered** send (muted / no message_id → no official record).
  Both were silent before. Tests: `test_q15_v95_alert_logging.py` (+2).
  ⚠️ The orderbook-age gate I floated is **already covered** by
  `v5_hardening.apply_snapshot_freshness` (`Q15_V5_MAX_SOURCE_AGE_S`, default 5s,
  fail-closed) — I did NOT add a redundant second gate.
- **Polish:** (1) **exact Student-t** promotion p-value — `_two_sided_p(t, df=…)` via
  the regularized incomplete beta (`_betai`/`_betacf`); the paired test now passes
  `df=n-1` (the normal approx was slightly anti-conservative at n~50). Back-compat:
  `df=None` keeps the normal path. (2) **dropped-feature counter under the lock** —
  the unlocked centroid build now accumulates drops locally and folds them in under
  `self._lock` (`ledger_v95._pattern_centroids`). (3) **challenger tail-calibration
  embargo** (`challenger/harness.train_predictor`): purge fit rows whose label window
  `[t,t+horizon]` reaches into the calibration slice, with a safe fallback to the
  un-embargoed split if it would starve training (shadow-only). (4) **health `data_age`
  self-consistent** — `/api/health` reads a new `engine_update_ts` map written under
  `state_lock` beside the snapshot, instead of an unsynchronized `engines[a]` read.
  Tests: `test_q15_v95_student_t.py` (+8), `test_q15_ledger_dropped_rows.py` (+1),
  `test_challenger_harness_embargo.py` (+3), `test_app_health_data_age.py` (+2).
- All model-behavior-adjacent changes are shadow-only or observability-only; frozen
  champion output is unchanged. No new always-on production behavior.

## ✅ Shipped THIS session (branch `claude/read-hand-off-5ou5op`) — challenger shadow system
**Read-only, default-OFF, zero production impact. NOT wired into the live loop yet
(by design — single documented seam to promote it).** A new self-contained package
`q15_upgrade/challenger/` that estimates calibrated P(Yes) for the 15-min binaries
as a SHADOW model, built to be promoted to primary with one switch.

- **Decoupled:** own `ChallengerConfig` (all flags default OFF: `Q15_CHALLENGER_ENABLED`,
  `Q15_CHALLENGER_AS_PRIMARY`), own SQLite ledger (`data/q15_challenger_shadow_v1.sqlite3`,
  gitignored), no order execution, no writes to production tables.
- **Pure-python by default** (`backend=logistic`) — the deploy target has no
  numpy/sklearn/xgboost. Optional `xgboost`/`lightgbm` backends used only if importable
  (your depth-3 / eta-0.03 / min_child-20 / subsample-0.75 / λ-10 config is pre-wired).
  Baselines: `market_only`, `volatility_only`.
- **Leakage-safe:** `features.py` reads only decision-time fields (+ a `FORBIDDEN_KEYS`
  guard); validation is **purged walk-forward + embargo** (`validation.py`); calibration
  (Platt / isotonic / identity, `auto`-selectable) fits on a held-out fold only.
- **8 required outputs** via `ShadowPredictor.predict` (P(Yes)/P(No)/confidence/edge-vs-
  market/net-edge-after-costs/recommendation/top-factors/warnings). Decision logic uses
  the EXECUTABLE ask (never midpoint), a no-trade zone, risk gates, conservative
  fractional sizing (never Kelly).
- **Scoring:** `ShadowLedger` records immutably pre-settlement, grades after official
  result, `scoreboard()` reports Brier / log-loss / ECE / accuracy / Wilson-CI trade
  stats for challenger vs control. `harness.walk_forward_evaluate` gives OOS metrics vs
  baselines; `harness.train_predictor` freezes a live shadow predictor.
- **Promotion seam (the "easy switch"):** `primary_probability(snapshot, champion_p, predictor=...)`
  returns the champion unchanged when `as_primary` is OFF (byte-identical production),
  the challenger's calibrated P(Yes) when ON+trained, champion fallback otherwise. That
  one call is the entire integration — nothing is wired into `app.py`/`checkpoint_v95.py` yet.
- Tests: `tests/test_challenger.py` (22) — features/leakage, logistic learning, Platt/
  isotonic, purged-WF embargo+purge, decision gates, predictor 8-outputs+cold-start,
  harness OOS eval, ledger record/resolve/score, promotion seam on/off/untrained.
- ⚠️ **Data reality:** the v95 ledger has ~0 resolved rows under the current MODEL_VERSION,
  so the challenger is cold-start (defers to market price) until data accrues. Judge it
  ONLY on OOS log-loss/Brier/ECE/net-cents vs control once ≥ a few hundred resolved/checkpoint.
- **Governance addendum controls** (same package, all EXECUTED+TESTED): `schema.py`
  (min contract-observation schema + validate), `features.assert_point_in_time` (reject
  receive-after-decision), richer cost model (latency + adverse-selection cents),
  `ood.py` (out-of-distribution score → severe forces NO TRADE; lowers confidence),
  confidence redefined (coverage/freshness/decisiveness/OOD, not just distance-from-0.5),
  `stats.py` (paired log-loss/Brier diff, block-bootstrap CI, McNemar, effective-N),
  `lineage.py` (code commit + config hash + version stamps on every recorded prediction),
  `experiment.py` (frozen PreRegistration + append-only experiment & holdout-access ledger).
  ⚠️ DATA-DEPENDENT pieces (control reproduction, real backtest, live shadow vs production
  feed) remain **NOT EXECUTED — REQUIRES EXTERNAL DATA**; no fabricated metrics anywhere.
- **Extraction bridge** (`extract_bridge.py`, EXECUTED+TESTED): `export_production_ledger`
  (read-only `SELECT *` JSONL dump to run on the Repl), `load_jsonl`, `inspect_feature_json`,
  `rows_to_samples` → (ts, feature_dicts, y, control_probs, groups) for the harness. Reuses
  `features.extract` so shadow == live features. Feature mapping is defensive pending
  confirmation of the real `feature_json` keys (run `inspect_feature_json` on the dump first).
- **Owner sent an AGGREGATE ledger snapshot** (metrics/scoreboard, NOT row-level) on 2026-06.
  MEASURED facts from it (real production data, 301 resolved): overall acc 67.4%, champion
  log-loss 0.600 vs market-baseline 0.723 (champion has real skill); shadow challenger NOT
  promotable (10M vs-champion p=0.37; 15M effect below floor) — matches the gate. **ECE=0.069
  with systematic UNDER-confidence above 0.60** (predicts 60-65% → wins 80%; predicts 75%+ →
  wins ~100%): a real, actionable recalibration opportunity. Realized P&L NEGATIVE overall
  (-496c/295) despite 67% acc — SOL/DOGE/XRP bleed, BNB/ETH/BTC positive. 15M is the weak
  checkpoint (51.6%), 7M strong (76.8%). Row-level export still needed to train/eval the
  challenger for real (aggregate can't reconstruct per-row features).
- **Challenger is now WIRED LIVE as a read-only SHADOW and ENABLED** (`.replit`:
  `Q15_CHALLENGER_ENABLED=true`, `Q15_CHALLENGER_AS_PRIMARY=false`). Hooks live in
  `ledger_v95.record_prediction` (→ `runner.observe`, on each NEW unique prediction)
  and `resolve_ticker` (→ `runner.resolve`, on settlement). `runner.py` records a
  PAIRED challenger prediction beside the champion (champion `raw_yes_probability` =
  control), grades on settlement, **re-trains from its own settled rows every
  `Q15_CHALLENGER_REFIT_EVERY`=10 resolutions ("learns as it goes")**, and at each
  15-min window boundary emits a Telegram **`CHALLENGER SHADOW`** report =
  challenger-vs-current accuracy overall + by checkpoint. Cold start mirrors the
  champion (parity) then diverges as it learns. `notifier.send` delivers the
  `CHALLENGER SHADOW` header as-is (skips reformatters + suppression). app loop
  drains+sends the report each cycle. **Zero overhead + byte-identical production
  when disabled** (`get_runner()` returns None fast; all hooks try/except). NOT
  primary — never drives live output. Needs a Repl reboot to pick up `.replit`.
  Tests: `test_challenger_runner.py` (6). Suite: **641 passed, 4 skipped**.
- **Ranked Top-1/2/3 report** (per owner request): the `CHALLENGER SHADOW` report now
  ranks each model's per-asset picks within a CASE (= one 15-min market × checkpoint)
  by confidence (|P-0.5|) and scores each rank independently — no double-counting.
  Shows the latest window's Top-3 per checkpoint for BOTH models with OK/X vs the
  official result, a running per-rank C/W/accuracy table (challenger vs native), the
  combined totals, and a side-by-side verdict. New `ledger.ranked_comparison` /
  `latest_window_cases`; shadow rows now store `close_time` for exact case grouping.
  `runner.report_message` rewritten. +2 tests. Suite: **691 passed, 4 skipped**.
- **Report UI cleanup** (owner: "it all looks so confusing"): `runner.report_message`
  redesigned into ONE card — only the **bold title** (`CHALLENGER SHADOW vs YOUR
  SYSTEM`) is bright/white; *everything else* now lives inside a single `<pre>`
  monospace block (owner: "take out the white text I only want the title"). Dropped
  the long scoring paragraph for a 3-line plain-English note; "NATIVE" → "Yours";
  empty `P2/P3 –` rows in the latest window are no longer printed (only ranks that
  had a pick); totals collapsed from `C/W/acc` dual columns to `hit` (e.g. `1/2`) +
  whole-% `acc`; one clear `Winner:` line + plain `Learning:` state. Same data, far
  cleaner layout. Tests updated to new wording. Suite: **691 passed, 4 skipped**.
- **End-result section** (owner: "add a different section where at 10M and 15M they
  both predict the end result, keep what you already have, make it clean"). NOTE:
  the ranked report scores **directional accuracy only** — it does NOT read the
  shadow's `recommendation` (BUY_YES/BUY_NO/NO_TRADE); a no-trade is **never** a
  loss (P&L is computed for traded rows only in `scoreboard()`, NO_TRADE = sit-out
  = 0). New `ledger.latest_window_end_results(checkpoints=("15M","10M"))`: for the
  latest settled 15-min window (bucketed by the 900s boundary), per asset, each
  model's predicted side + hit at 15M and 10M vs the actual result. Rendered as a
  compact `END-RESULT CALL · 15M & 10M` block (`Res` col + `Y+/N-` cells, + right /
  - wrong) inside the same `<pre>` card, between LAST WINDOW and TOTALS. +1 test
  (`test_end_result_section`). Suite: **692 passed, 4 skipped**.

## ✅ Shipped THIS session (branch `claude/read-hand-off-5ou5op`) — entry-economics shadow A/B
Acting on the review's #1 finding (~67% accurate but negative P&L = the entry gate
isn't selective on price/cost). Owner: "implement it but ON in shadow so we can see
both play out and whether it improves." New `q15_upgrade/shadow_economics.py` (pure,
tested) runs a stricter, cost-aware gate BESIDE the live one and grades both on the
SAME settled trades — **never changes the live recommendation** (frozen champion +
read-only invariant intact). CONTROL replicates production exactly (`net_edge =
P*100-ask-cost >= required_edge`, same defaults + same `Q15_V95_{cp}_REQUIRED_EDGE_CENTS`
env). SHADOW charges the costs the live model omits (slippage 0.5¢ + adverse 0.25¢),
enforces a hard no-trade floor (1¢), and requires risk-adjusted EV `net_edge - k*σ ≥ 0`
(σ = binary cent-stdev, so thin-confidence edges are penalised). Shadow P&L is ALSO
charged the extra modelled cents per entry → it must earn its selectivity, never
flattered. Comparison computed from columns already persisted on every resolved row
(`conservative_probability`, `entry_ask_cents`, `entry_cost_cents`, `realized_cents`)
via read-only `V95Ledger.resolved_economics_rows()` — zero schema change. Live
visibility: `apply_v95_policy` stamps `q15_v9_5_shadow_econ_{enter,net_edge_cents,reason}`
for the dashboard. Hourly report gains an `ENTRY-ECONOMICS SHADOW` Live-vs-Shadow
table (trades/win%/P&L/avg + verdict), self-silent until settled rows exist. Flags
`Q15_SHADOW_ECON_*` in `.env.example` (default ON). +8 tests
(`test_q15_shadow_economics.py`). Suite: **742 passed, 4 skipped**.

## ✅ Shipped THIS session (branch `claude/read-hand-off-5ou5op`) — 10M setup miner (leakage-safe)
Owner asked: find a 10M setup/combination that always predicted the outcome,
testing every occurrence, with no hindsight/leakage/cherry-picking. **First the
honest finding:** the row-level data needed (per-prediction features+outcome) does
NOT survive in this container — live `predictions` table = 2 synthetic test rows;
`v95_ledger_snapshot.json` is AGGREGATES only (301 resolved summarised; no per-row
features); v94 DB = 1 row; v7 JSONL = 126 decision-state events, no outcomes. The
only 100%-looking artifact is the pooled calibration curve (model prob ≥75% → 27/27
across ALL checkpoints), but that's in-sample, small-n, not 10M-specific, and its
Wilson LB is only 87.5% — not a validated rule; the system's own test already says
`challenger_not_significantly_better`. **Then built the miner so it answers this as
real data accrues:** new `q15_upgrade/setup_miner.py` (pure, tested) — enumerates
conjunctions (≤2) of decision-time conditions (feature sign splits + regime eq),
with ALL guards enforced: chronological train/test split (discover on train,
CONFIRM on later untouched test), minimum support (train ≥30 / test ≥10),
Bonferroni multiple-testing correction, Wilson lower bounds (never the raw rate),
missing-data rows excluded not guessed, outcome never used as an input. Read-only
`V95Ledger.resolved_setup_rows(checkpoint)` feeds it; `reporting.HourlyReporter
._setup_scan_lines` renders a compact `10M SETUP SCAN` block that self-silences
until there's enough data (verified: live ledger → "insufficient data … have 1/1
of 2"). Flags in `.env.example` (`Q15_SETUP_MINER_*`, default ON). +9 tests
(`test_q15_setup_miner.py`: stats, undecidable conditions, insufficient-data,
genuine-signal validates, noise doesn't, in-sample-perfect-but-test-fails flagged,
multiple-testing correction). Suite: **711 passed, 4 skipped**.

## ✅ Shipped THIS session (branch `claude/read-hand-off-5ou5op`) — 15M rank performance in hourly report
Owner: "on the hourly report add 15M rank performance." `reporting.HourlyReporter
._scoreboard_table` now renders a **`15M RANK PERFORMANCE`** section (the #1/#2/#3
pick judged within the 15M checkpoint) directly above the existing `10M RANK
PERFORMANCE` block, same W-L/Acc/P/L grid + 0-0 placeholders before settling. Data
already existed in `scoreboard()["rank_by_checkpoint"]["15M"]` (built for every
tracked checkpoint) — only the report rendered it for 10M. Test updated
(`test_q15_learning_scoreboard.py`). Suite: **702 passed, 4 skipped**.

## ✅ Shipped THIS session (branch `claude/read-hand-off-5ou5op`) — gated manipulation alerts
**Detection unchanged; only NOTIFICATIONS are restricted.** New module
`q15_upgrade/manipulation_alert.py` + wiring in `checkpoint_v95.run_cycle`. The
flip-risk / manipulation overlay keeps running every cycle; a manipulation alert
is now PUSHED to Telegram only when ALL three owner conditions hold:
1. **High probability** the manipulation actually occurs — gated on the learned
   flip hit-rate (Wilson lower bound) `>= Q15_V95_MANIPULATION_ALERT_MIN_PROBABILITY`
   (default 0.70). No learned data ⇒ no alert (conservative, "ignore unconfirmed").
2. **Normal check delivered first** — the interval's compact-panel `V9.5 CHECK`
   must have actually reached the owner (`delivered`, not muted/failed). Recorded
   in `_send_compact_panel` into `self._normal_check[checkpoint]`. Muted/failed
   normal check ⇒ no manipulation alert for that interval.
3. **Recommendation changed** — the manipulation analysis must recommend a
   different side / entry / exit / action than the normal check did.
Low-probability / low-confidence / unchanged / repetitive findings are dropped
(dedup set keyed `(ticker, checkpoint, new_side)`); all qualifying findings for an
interval are **combined into ONE concise alert** carrying the six required fields
(probability, confidence, what changed, original rec, new rec, evidence, interval).
- **Flow:** `_process_flip_risk` collects candidates each cycle (read-only, no send,
  no state mutation); after `_send_compact_panel`, `_dispatch_manipulation_alerts`
  applies the gate and sends at most one combined alert. Runs AFTER the normal
  check by construction. Did NOT re-enable the legacy HIGH FLIP RISK / CONFIRMED
  FLIP Telegram UIs (still off).
- **Flags** (`.env.example`): `Q15_V95_MANIPULATION_ALERTS_ENABLED` (default true,
  but heavily gated), `..._MIN_PROBABILITY` (0.70), `..._MIN_CONFIDENCE` (40).
- **Tests:** `tests/test_q15_manipulation_alert.py` (10) — gate conditions, message
  content (all six fields), and manager glue (no-send-without-normal-check, single
  combined send, dedup, interval filtering). Suite: **702 passed, 4 skipped**.

## ✅ Shipped THIS session (branch `claude/read-hand-off-5ou5op`) — alert-delivery hardening
## ✅ Shipped THIS session (branch `claude/hand-off-review-ucy2ee`, MERGED to `main`) — PHASE 1: official-record + compact panels + recap
**Merged — not yet deployed (needs a Repl reboot).** Phases 2 (shadow 0–100 score on
the panel) and 3 (entry recheck + manipulation grading rule) are still TODO; see the
multi-phase plan below. Phase 1 is complete + green (637 passed, 4 skipped).
Owner-approved multi-phase rework (decisions locked): **(1)** compact unified panel
sent EVERY checkpoint (reverses `Q15_V95_SEND_ONLY_ON_ENTRY`) carrying the YES/NO
call + compact records + manipulation + graduated entry guidance; **(2)** a 0–100
entry score (30 dir-conf / 25 edge / 20 wick / 15 momentum / 10 manip) as a
**shadow** overlay first (does NOT drive live entries until it earns OOS lift —
champion stays frozen); **(3)** entry RECHECK = extend the existing `entry_followups`
(ENTER NOW / KEEP WAITING / SKIP ENTRY) + manipulation grading. Records display
compact W-L/% **with a low-n marker** (Wilson-backed). WATCH states must read clearly
as "not an entry" and show the prior call (e.g. "now YES, was NO at 15M") — each
interval graded on its OWN sent call (no retroactive regrade).

**Layout (locked with owner):** hourly report UNCHANGED. Live 15M/10M/7M panels are
forward-looking only (manipulation + prediction w/ WATCH clarity + prior-side flip +
graduated entry guidance), NO W-L. A single END-OF-CYCLE RECAP fires once after a
contract settles: per-interval hit/miss + flips + predictability + the entry result +
manipulation call + the RUNNING official totals. Records show W-L/% with a low-n marker.

**Done so far (Phase-1 FOUNDATION + FORMATTERS, committed to branch, inert until wired):**
- `notifier.send_with_result()` → `{ok, delivered, muted, message_id}` + `last_message_id`;
  `send()` stays a bool wrapper. Muted = handled-but-NOT-delivered (no message_id).
- Ledger `sent_predictions` immutable table + `record_sent_prediction(...)` (rejects
  no-message_id and sent_at≥close_time) + `official_scoreboard()` (15M/10M/7M YES/NO/Total,
  entry, manipulation; graded vs settled outcome via join; Wilson low_n flags; insert-only).
- `q15_upgrade/panels_v95.py` — pure `build_checkpoint_panel()` + `build_cycle_recap()`
  (single `<pre>`, keeps `V9.5 CHECK` marker, flip/WATCH clarity, running-record block).
- Tests: `test_q15_official_record.py`, `test_q15_notifier_message_id.py`, `test_q15_panels.py` (+24).
**WIRED (live, default ON via `Q15_V95_COMPACT_PANEL`; legacy entry-only path preserved
under the flag):** `checkpoint_v95._send_compact_panel` sends the forward-looking panel
for the top-ranked pick once per checkpoint+window (reuses the reserve/settle dedup),
maps analysis → panel (manipulation block, prediction w/ prior-side flip, graduated entry
state from `trade_decision`), and on a real delivery writes the immutable official record
from the Telegram message_id: `interval` always, `entry` + slot/pushed/follow-up when the
pick is ENTRY_RECOMMENDED, `manipulation` (direction-after) when flagged. Muted = no
message_id = not official. `build_compact_checkpoint_panel` + helpers do the mapping.
Tests: `test_q15_v95.py::test_compact_panel_writes_official_record`, reworked
`test_no_entry_checkpoint_panel_behaviour`. Suite 632 → 633.

**RECAP (live, gated `Q15_V95_CYCLE_RECAP` default ON):** on settlement,
`checkpoint_v95._send_cycle_recaps` fires ONE `CYCLE CLOSED` recap per settled ticker
(deduped via a `recap:<ticker>` reservation), built from `ledger.contract_recap()` —
per-interval hit/miss + flips + entry result + manipulation call, graded only on what was
officially delivered — rendered via `panels_v95.build_cycle_recap` with the running
`official_scoreboard` totals. `format_telegram_message` bypasses `CYCLE CLOSED` so the
recap is never re-rendered by the legacy chain. Suite 633 → **637**.

**Next — Phase 2:** the 0–100 entry score (30 dir-conf / 25 edge / 20 wick / 15 momentum /
10 manip) as a SHADOW value shown on the panel (does NOT drive live entries; champion
frozen) + the WAIT target range. **Phase 3:** retrofit `entry_followups` into the
ENTER NOW / KEEP WAITING / SKIP ENTRY recheck (price/trigger-driven) + the manipulation
grading rule (default: direction-after vs settled outcome — confirm with owner).

## ✅ Shipped THIS session (branch `claude/hand-off-review-ucy2ee`, MERGED to `main`) — consolidated Telegram UI
**Merged to `main` — not yet deployed (needs a Repl reboot).** Follow-up to the flip
removal: a full audit found Telegram delivery used **3 different formats**. The
unified single-`<pre>`-panel cards are the checkpoint alert (`build_v95_message`,
`V9.5 CHECK`), the hourly report, and the entry follow-up. The OLD plain-text
format was used by (a) flip alerts [removed prior entry], (b) the window_focus
**two-window checkpoint alerts** (`🎯 10M FINAL #1 READY …`), and (c) the
**dip alerts** (`⚡ DIP …`) — and the two-window path fires LIVE via
`checkpoint_v91.update()→_maybe_notify`, so the owner was getting both a unified
panel AND a separate old-format alert for the same checkpoint. Owner chose to
**disable** the old-format senders (not reformat). Now OFF by default:
`Q15_FOCUS_CHECKPOINT_ALERTS` (new, False) gates the two-window sends and
`Q15_DIP_ALERT_ENABLED` (True→False) gates the dip alert — the two-window
ranking/learning/dashboard still run; only Telegram delivery is muted. So only the
unified panel / hourly report / follow-up are delivered. Tests updated to enable
the flags where they exercise the send path; new muted-by-default locks in
`test_q15_alert_send_retry.py::DefaultDeliveryConsolidationTest`. Suite **605 → 608**.

## ✅ Shipped THIS session (branch `claude/hand-off-review-ucy2ee`, MERGED to `main`) — removed flip-alert UI
**Merged to `main` — not yet deployed (needs a Repl reboot to take effect).** Owner
asked to remove the old flip-alert Telegram UI (the `CONFIRMED PREDICTION FLIP —
DOGE/BTC 7M` cards with "Manipulation risk before flip / Estimated flip probability
/ Main evidence"). Both flip Telegram sends are now **OFF by default**:
`Q15_V95_FLIP_CONFIRMED_ALERTS` (True→False, `checkpoint_v95._process_flip_risk`)
and `Q15_V95_FLIP_ALERTS_ENABLED` (True→False). Read-only flip tracking, the
`flip_risk` scoring/learning, and the dashboard block are **unchanged** — only the
Telegram delivery is muted; re-enable either flag to bring it back. Lock test:
`test_q15_v95_flip_risk.py::FlipAlertDeliveryDefaultTest` (muted by default; still
sends when re-enabled). Suite **603 → 605**.

## ✅ Shipped THIS session (branch `claude/hand-off-review-ucy2ee`, MERGED to `main`) — review-fix batch
**Merged to `main` — not yet deployed (needs a Repl reboot to take effect).**
Ran a fresh fan-out review (decision engine 7/10, learning 6.5/10, app+loop 6.5/10,
overall **7.0/10**), then implemented the Highest/Medium/Polish fixes it surfaced.
Every model-behavior change is flag-gated; shadow-only changes never touch frozen
champion output. Suite **589 → 603 passed, 4 skipped** (+14 tests).

1. **Primary-learner boost actually applies** (`ledger_v95.py:_apply_shadow_update`).
   A legacy `min(1.0, sample_weight*primary_learning_weight)` clamp erased the 10M
   1.25× boost for exactly the high-quality rows whose base weight already hit 1.0,
   so 10M learned no faster than 15M. The boosted weight now scales fully (bounded
   by the per-result + total-drift caps, so >1.0 is safe). Gated
   `Q15_V95_PRIMARY_LEARNING_BOOST` (default ON; OFF = legacy clamp). Shadow-only.
2. **Platt identity fallback** (`ledger_v95._calibration_fit`). An unconverged /
   near-singular fit no longer silently transforms live probabilities — it reverts
   to identity (raw passes through), flagged `fallback=identity_unconverged` +
   `reason=platt_unconverged_identity`, counted in `status().calibration_unconverged_fallbacks`.
   Gated `Q15_V95_CALIBRATION_REQUIRE_CONVERGED` (default ON). Newton budget now
   `Q15_V95_CALIBRATION_MAX_ITERS` (default 12, behaviour-identical; makes the
   fallback deterministically testable).
3. **App enrichment exceptions no longer freeze state** (`app.py` refresh loop). The
   enrichment pipeline ran under `ct.time` (re-raises), so one stage's exception
   skipped `state.update(snaps)` (stale dashboard for ALL assets) AND every
   best-effort subsystem below. Wrapped in try/finally → partial snapshot always
   published; `deep_evaluation_snapshots` isolated so signals/scalp/report/learn
   still run.
4. **Regime schema guard** (`ledger_v95.py`): `regime_challenger_weights` now carries
   the same `CHECK(checkpoint IN ('10M','15M'))` as its siblings (fresh DBs), so an
   accidental 7M write fails loudly instead of contaminating regime learning.
5. **Missing-close_time is no longer silent** (`window_focus._maybe_notify`): when a
   market is near a checkpoint but `close_key` is empty (degraded feed), a throttled
   (60s) WARNING surfaces the dropped alert instead of returning silently.
6. **Unknown-depth liquidity penalty** (`checkpoint_v95.analyse_v95`): when the
   orderbook is unavailable (`depth=None`), optionally discount `liquidity_quality`
   so an unverifiable book ranks below a confirmed-liquid one. Gated
   `Q15_V95_PENALIZE_UNKNOWN_DEPTH` (default OFF) / `Q15_V95_UNKNOWN_DEPTH_FACTOR`
   (0.5). Ranking-only — never an entry gate, so it cannot place a trade.

**Debunked on verification (no change made):** the review's "checkpoint_v95 entry
alerts don't retry on send failure" was a FALSE POSITIVE — `complete_notification(
success=False)` clears `reserved_until` without setting `sent_at`, so the next cycle
re-reserves and retries. Left the working state machine untouched.

New tests: `test_q15_ledger_review_fixes.py` (boost / Platt fallback / regime CHECK),
`test_q15_v95.py` (gated unknown-depth penalty), `test_app_refresh_loop.py`
(enrichment-stage isolation), `test_q15_alert_send_retry.py` (empty-close_key warn).

## ✅ Shipped (branch `claude/read-hand-off-5ou5op`, merged to `main`) — alert-delivery hardening
**Not yet deployed — needs a Repl reboot to take effect.** Implemented the top-3
fixes from a fresh reliability review (the rest of the review's findings were
either debunked on verification — e.g. the "KeyError in `_harvest_and_submit`"
and "leaked executor" were misreads — or filed as Medium/optional follow-ups).

1. **Alert send failures are no longer silently dropped** (`window_focus.py`).
   `notifier.send()`'s return value was ignored: a Telegram 429/400/network blip
   (send returns falsy) still advanced the alert state as if delivered, so the
   alert was lost with no retry. New `_claim_and_send()` returns
   `sent`/`duplicate`/`failed`; on `failed` it **releases the local claim** and
   the caller does NOT advance state, so the next cycle retries. The dip alert and
   all three checkpoint sends (15M/10M/7M) route through it. Preserves the
   intentional anti-starvation advance on a *lost* claim (`duplicate`). Gated
   `Q15_V95_ALERT_RETRY_ON_SEND_FAILURE` (default ON; OFF = legacy consume-on-fail).
   ⚠️ The shared claim store's ledger is permanent, so a true re-fire only happens
   in the common single-process deployment; against a shared store the retry is
   seen as already-delivered. Either way the failure is now logged, not silent.
2. **`_claim` now fails CLOSED on a claim-store error** (`window_focus.py`).
   `store.claim_event()` raising was swallowed (`except: pass`) and then returned
   `True`, so a transient store outage could let dev+prod each deliver the SAME
   alert (cross-process duplicate). Now logs and returns `False` (skip this cycle,
   retry once the store recovers).
3. **Kalshi REST honors `Retry-After` on 429** (`q15_upgrade/kalshi_rest.py`).
   429 backoff ignored the server's `Retry-After`; now parsed (delta-seconds or
   HTTP-date) and used on the retry paths (e.g. discovery `retries=3`), capped at
   `_MAX_RETRY_AFTER_SECONDS`=8 so a bad value can't stall the ~1s loop. The
   per-cycle single-attempt default (`retries=1`) is unchanged on purpose — the
   loop itself is the retry and the token bucket caps the request rate.

New tests: `test_q15_alert_send_retry.py` (claim fail-closed, `_claim_and_send`
tri-state, dip retry on/off) + `test_q15_kalshi_retry_after.py` (header parse /
cap / fallback). Suite: **588 passed, 4 skipped** (was 574).

## ✅ Shipped THIS session — part 2 (branch `claude/read-handoff-ipxm5a`, MERGED to `main` @ `4954e7b`)
**Not yet deployed — needs a Repl reboot to take effect.** Newer work, on top of part 1:

A. **Flip-warning report in the interval-scoreboard table format** (`reporting.py`):
   the old free-text `MANIPULATION WARNING PERFORMANCE` block now renders as the same
   aligned W-L/Acc/P-L grid as the intervals (`_flip_scoreboard`/`_flip_row`), by
   checkpoint/direction/asset, plus a learned flip-rate-by-risk mini-table.
B. **Flip 70% hit-rate gate** (owner: "the 70% is for flips"): a HIGH FLIP RISK
   warning is DORMANT until the learned flip-rate for the current risk bucket is
   reliably ≥70% — its **95% Wilson LOWER bound** must clear `Q15_V95_FLIP_MIN_HITRATE`
   (0.70). Bootstraps from background settled flips; thin samples ⇒ wide CI ⇒ stays
   quiet. `flip_risk.wilson_lower_bound`/`bucket_flip_reliability`;
   `Q15_V95_FLIP_REQUIRE_HITRATE` (ON). Stacks with the existing require-learned gate.
C. **One ACTIVE prediction per timeframe + pushed-vs-background accuracy**
   (`Q15_V95_ONE_ACTIVE_PER_TIMEFRAME`, ON): once an entry is recommended + delivered,
   that contract holds the checkpoint's slot (`pushed_slots` table) until it closes —
   no 2nd push for the same time frame while live. New `pushed` column marks delivered
   entries; `scoreboard()` reports `by_pushed` (pushed vs background) and the hourly
   report + checkpoint alert show pushed-only accuracy (background never inflates it).
D. **One follow-up check per interval** (`Q15_V95_ENTRY_FOLLOWUP_ENABLED`, ON): on a
   delivered entry, arm exactly ONE follow-up per (contract, interval) — fires once
   after `Q15_V95_FOLLOWUP_DELAY_SECONDS` (120) confirming still-valid / side-changed /
   hold / take-profit / avoid / exit, then never repeats. `entry_followups` table;
   `build_followup_message`/`_followup_verdict`/`_dispatch_entry_followups`. Per
   (ticker, checkpoint) so 15M never blocks 10M. Eligible: 15M+10M. "FOLLOW-UP" added
   to the notifier actionable allowlist so it's never muted.
E. **Best Entry top/detail consistency** (fixed the BNB-on-top-while-BTC-is-#1 bug):
   the `🏆 BEST ENTRY` block is now `_best_entry()` = rank #1 of the QUALIFYING entries
   from the SAME `rank_analyses` ordering the detail renders (executable trade score,
   not confidence-only `_best_pick`). Only ENTRY_RECOMMENDED assets qualify; none ⇒
   `NO ENTRY RECOMMENDED`. `_best_entry_consistent` guard suppresses the alert if top
   ≠ detail #1. New top format (asset/side, interval, status, prob, recommended entry,
   conservative net edge, follow-up remaining).
F. **Scalp engine DISABLED by default** (`alert_config.py`: `SCALP_ENABLED` default
   False) — owner-directed; `ScalpEngine.evaluate` short-circuits. Re-enable with
   `SCALP_ENABLED=true`.
G. **NO-ENTRY checkpoints muted entirely** (`Q15_V95_SEND_ONLY_ON_ENTRY`, default
   ON): `run_cycle` does not send the checkpoint alert when `best_entry is None`,
   so you're only messaged on a recommended entry — independent of the notifier
   alert level (hard-mutes the "multiple NO ENTRY per interval" the owner saw).
   Flip / follow-up alerts are separate and unaffected; the dashboard still shows
   everything. ⚠️ If symptoms persist after a Repl restart, suspect the **GitHub
   Relay is PAUSED on a conflict** so the Repl never received the merged code —
   check the *GitHub Relay* console and `python3 tools/github_reconcile.py`.
H. **`health_snapshot.json` untracked + git-ignored** — it's a generated file the
   Repl rewrites, so tracking it made the relay conflict on every sync and stall
   deploys. Removed from the repo (local/Repl copy kept). If a future session sees
   it reappear tracked, re-ignore it.

## ✅ Shipped THIS session — part 1 (branch `claude/read-handoff-ipxm5a`, MERGED to `main`)
**Not yet deployed — needs a Repl reboot to take effect.** Order of work:

1. **Review-hardening** (closed the prior review's gaps): extracted + unit-tested
   the loop's rollover/in-flight logic (`_fetch_result_is_current`,
   `_resolve_cached_detail`, `_harvest_and_submit` in `app.py`); None-contract
   fixes (`analysis.ingest_trades`, `spot_client` WS `ok`+`price` gate);
   `_two_sided_p` returns 1.0 (not 0.0) for non-finite t; top-level `ledger`
   block in `/api/health`; fixed two leaky test teardowns.
2. **Model-improvement flags flipped DEFAULT-ON + scoreboard RESET** (owner-directed):
   `Q15_V95_REGIME_AWARE_ANCHOR`, `Q15_V95_EVIDENCE_COVERAGE_PENALTY` (0.0→0.08),
   `Q15_V95_PRODUCTION_CALIBRATION_ENABLED`, `Q15_V95_15M_SHADOW_LEARNING` are now
   ON in code. **`MODEL_VERSION` bumped `…-v1`→`q15-v9.5.2-…-v2`** to start the
   scoreboard fresh (non-destructive; everything keys on `model_version`).
   ⚠️ **7M shadow learning stays OFF** — the `checkpoint_challenger_*` tables'
   CHECK constraint only admits `'10M'/'15M'`; enabling 7M raises IntegrityError
   on every 7M resolution. Needs a `'7M'` schema migration first.
3. **Manipulation tracking** (`_manipulation_signal` in `checkpoint_v95.py`): a
   read-only suspected-manipulation flag (pin / order-wall absorption / cross-
   exchange divergence) recorded per prediction, broken down in the scoreboard
   (`by_manipulation` suspected-vs-clean + by-tell), tagged on the checkpoint
   alert. Gated `Q15_V95_MANIPULATION_*` (default ON). Owner chose to KEEP it
   (retire later via `Q15_V95_MANIPULATION_ALERT_TAG=false` once flip-risk matures).
4. **Hourly report `10M RANK PERFORMANCE` section** + ledger `rank_by_checkpoint`
   (rank #1/#2/#3 within each interval, not blended).
5. **Flip-risk subsystem** (the big one — new module `q15_upgrade/flip_risk.py`):
   measures whether the FROZEN prediction is at risk of flipping. Three SEPARATE
   values — 0-100 manipulation/flip RISK score + confidence (missing data lowers
   confidence, never the score), learned flip PROBABILITY (historical flip rate of
   the score's bucket), learned THRESHOLD (per checkpoint/direction/asset, ≥30-sample
   gate → overall fallback). Flip = frozen 15M≠10M, 10M≠7M, or resolution-opposite;
   YES→NO and NO→YES separate. Point-in-time learning from resolved contracts only
   (`ledger.flip_stats`/`_flip_observations`/`flip_warnings` table). Gated alert
   state machine (threshold + 3-obs persistence + ≥2 evidence categories + flip-prob
   + confidence + 90s cooldown + hysteresis + re-arm); NORMAL/WATCH silent, only
   HIGH FLIP RISK + CONFIRMED FLIP send. Dashboard block + `MANIPULATION WARNING
   PERFORMANCE` report section. **Posture: HIGH FLIP RISK alerts are DORMANT until
   a learned threshold exists** (`Q15_V95_FLIP_ALERTS_REQUIRE_LEARNED` ON) — so
   nothing fires until ~30 flips accrue (days). All `Q15_V95_FLIP_*`, default ON.
6. **Single-panel Telegram layout** for BOTH the checkpoint alert and the hourly
   report: the whole body now renders inside ONE `<pre>` block; only the bold
   title / `Hourly Report —` header stays outside (markers + reformatter-bypass).

🔴 **Immediate next step: reboot the Repl** to deploy all of the above. Then bake —
the fresh scoreboard + flip history must accumulate (≥30 settled per checkpoint /
per flip direction) before the new defaults can be judged or HIGH FLIP RISK alerts
can fire. Judge on skill-vs-market / calibration ECE / realized cents, not raw %.

📊 **Optional: old-deployment history.** Owner has prior-deployment data; if sent,
treat as PRIOR-VERSION reference (audit + calibration fit), NOT folded into the
freshly-reset board (it's under the old `MODEL_VERSION`/config and has no
historical flip-risk scores, so it can't bootstrap flip-learning).

## ✅ Shipped (branch `claude/read-handoff-e79js5`) — alerts, UI, learning priority
Four-part request — fewer/expiring alerts, consistent UI, richer prediction
cards, and a 10-minute learning priority with per-interval metrics.
1. **One alert per *material* verdict + auto-expiry** (`checkpoint_v95.py`). The
   notification key now embeds the best pick's material state (coin/side/grade)
   via `_material_token`, so an unchanged verdict is deduplicated and only a real
   direction/confidence-band change (or an entry appearing/withdrawing, still via
   the state machine) sends a replacement. `_decision_signature` keys on the
   single best pick (not top-3). `_checkpoint_expired` auto-expires each interval
   (15M at 10:00, 10M at 7:00, 7M at `Q15_V95_7M_EXPIRY_SECONDS`=120 before close)
   so a 7-minute alert no longer lingers to market close.
2. **Stability trend** (`_stability_marker`): each prediction is tagged stable /
   strengthening / weakening / changed per (asset, checkpoint, window).
3. **UI consistency** (`format_telegram_message`): the legacy v94 reformatter is
   now disabled by default (`Q15_V95_LEGACY_FALLBACK_FORMAT`, default OFF) so no
   message renders in the old layout; `V9.5 CHECK` + the canonical hourly report
   still own their clean output.
4. **Richer prediction cards** (`templates/index.html` + new `q15_v9_5_*`
   snapshot keys): interval, side, grade (A high / B moderate / C low-developing),
   confidence %, P(Yes)/P(No) (sum ~100%), timestamp, time-remaining, and the
   stability trend; expired predictions dim.
5. **10-minute learning priority** (`ledger_v95.py`): configurable primary sample
   weight (`Q15_V95_PRIMARY_LEARNING_WEIGHT`=1.25), and per-interval metrics in
   `scoreboard()` — precision/recall for Yes & No, FPR/FNR, by-confidence-grade,
   and prediction-stability (change-rate) via new columns
   (`confidence_grade`/`original_predicted_side`/`changed_before_close`) +
   `note_prediction_revision`. 7M/15M keep collecting + grading; 10M just trains
   heavier. Displayed confidence is unchanged — validate real lift OOS.

## ✅ Shipped (branch `claude/read-handoff-e79js5`) — review follow-ups (rating → ~8.5)
Implemented the "how to raise the rating" list from the code review. Every
model-behavior change is **default-OFF** behind a `Q15_*` flag (shadow-validate
first) so production output is byte-identical until enabled.
- **Loop + concurrency tests** (`tests/test_app_refresh_loop.py`,
  `test_app_concurrent_routes.py`) — the biggest gap. Added a test-only seam:
  `refresh_loop(max_cycles=…)` and `Q15_AUTOSTART_REFRESH=0` (skip autostart on
  import). Tests drive a real cycle (no network), prove `ct.safe`/discovery
  exception isolation, and hammer `/api/snapshot|summary` from many threads
  against a writer to prove the `state_lock` contract (no deadlock / 500).
- **Regime-aware market anchor** (`Q15_V95_REGIME_AWARE_ANCHOR`,
  `_regime_anchor_strength` in `checkpoint_v95.py`): shrinks model deviation from
  the market in noisy regimes (high-vol / divergence / pin), full deviation in
  clean trends. Knobs `..._SENSITIVITY`, `..._MIN_FACTOR`.
- **Evidence-coverage confidence penalty** (`Q15_V95_EVIDENCE_COVERAGE_PENALTY`):
  thin evidence now widens the conservative haircut toward 0.5 instead of reading
  as a clean neutral — the "no data ≠ neutral" fix.
- **Pinned the frozen model constants**: `CHAMPION_WEIGHTS` and the new named
  `_EVIDENCE_QUALITY_WEIGHTS` (was an inline literal) are documented and locked by
  `tests/test_q15_v95_weights.py` so an accidental edit fails loudly.
- **Public-feed circuit breaker** (`market_data_v95.py`,
  `Q15_V95_PUBLIC_BREAKER_THRESHOLD`/`_COOLDOWN_SECONDS`, default 5/30s): per-asset
  backoff after repeated all-source failures; `health()["breaker_open_assets"]`.
- **Ledger observability**: Platt fit now logs non-convergence and returns
  `converged`/`iterations`; unparseable `feature_json` rows increment
  `dropped_feature_rows` (in ledger `stats()`) instead of vanishing silently.
- Skipped from the list: full `Q15_*`→dataclass centralization (too large/risky
  vs value for the frozen-adjacent engine; flagged as polish). The per-loop
  executor item was a false positive — it's already created once before `while`.

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
