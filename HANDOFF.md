# Session handoff

Working notes so a fresh session resumes cheaply. See `CLAUDE.md` (architecture)
and `SYNC.md` (Replit sync). Live app on Replit (`python3 app.py`). **The owner
trades REAL money manually off the alerts**, so reliability + honest data
freshness + honest accuracy measurement matter more than new model features.

⚠️ Fresh container: `pytest`/`websockets`/`flask` are NOT preinstalled →
`pip install pytest "websockets>=12.0" flask -q` first. A broken `cffi`/`cryptography`
may need `pip install --force-reinstall --ignore-installed cffi cryptography -q`
(else the two app-level test files error on collection instead of skipping).
Tests: `python3 -m pytest tests/ -q` → **809 passed, 4 skipped** (12 skipped in a
bare container where `flask`/`websockets` aren't installed; skip count varies with
cffi/crypto availability).

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
