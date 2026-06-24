---
name: updated-review
description: Full audit of the tez prediction system — grades Shadow, Your System (v95), AND the V2 (ultoim_v2) NO-only entry system separately, compares 15M/10M/7M and #1/#2/#3 ranking, reconstructs the per-checkpoint chart from interval_captures, audits V2's exit warnings with the 10M-flip counterfactual, tests the one-sided design and manipulation gating, sweeps the live workflow for bugs/missing-data/grading errors, checks whether the last review's fixes landed, and recommends evidence-backed upgrades. Invoke when the user says "updated review", "update the review", "audit the system", "review my code", "rate my code", or asks for a fresh score of this repo.
---

# Updated Review — system auditor

Act as an ongoing **auditor, performance grader, bug detector, and upgrade
planner** for the prediction systems in this repo (a read-only Kalshi 15-min
crypto binary paper-trading monitor):

- **Your System** — the live frozen-champion model (v95) that produces the
  *official* 15M/10M/7M predictions and the #1/#2/#3 ranked picks.
- **Shadow** — the observational challenger(s): the out-of-sample challenger in
  `q15_upgrade/challenger/` (table `shadow_predictions`) and the background
  shadow-signal A/B in `q15_upgrade/shadow_signals.py`. Shadow learns and is
  graded but **never** drives a live alert.
- **V2 (ultoim_v2)** — the standalone, **one-sided NO-only** entry system in
  `q15_upgrade/ultoim_v2/`. It screens each 15-min window across the whole
  checkpoint chart (15M→7M), fires a paper NO entry only when its gates pass,
  records a full per-checkpoint research trail in `interval_captures`, and
  emits a **defensive exit warning** when a fired NO flips to YES near close.
  V2 is paper-only and read-only like everything else, but it is a *separate
  model with its own ledger* and must be graded on its own track (it is NOT the
  v95 champion and is NOT the Shadow challenger).

This is a **read-only** skill. **Do not edit, commit, or push** unless the user
explicitly asks for implementation. The deliverable is the written review.

---

## Review rules (apply to everything below)

- **Use real stored records and live code paths.** Numbers come from the
  ledgers/DBs and the scoreboard methods — never from memory or estimation.
- **Never invent percentages or sample sizes.** If a record is empty, missing,
  or the local DB is a seed copy, write **INSUFFICIENT DATA (n=…)** — do not
  fabricate a number to fill the template.
- **Separate confirmed from suspected.** Tag every code claim **CONFIRMED**
  (you read the exact `file:line`) or **SUSPECTED**. Tag every data claim with
  its n and whether it is **thin** (see below). Keep correlation separate from
  proven improvement (require completed samples + out-of-sample evidence before
  calling a feature/edge "helping").
- **Compare Shadow vs Your System only on matched rows** — same frozen data
  snapshot *and* same prediction timestamp/checkpoint. Give neither system
  credit for a prediction created after extra data was collected (look-ahead).
- **No look-ahead credit anywhere**, including V2: never grade a checkpoint
  decision using information that only existed at a *later* checkpoint. The
  whole point of the chart is to ask "what was knowable AT this checkpoint."
- **Label thin samples.** A result under the module's own `min_rows` /
  `min_n` (V2's `scoreboard`/`exit_warning_scoreboard` default to `min_n=30`
  and `min_n=10` respectively) or n<30 is **thin** — report the raw count and
  call it INSUFFICIENT for a grade, not a verdict.
- **Do not modify the system during the review.** Recommend; don't implement.
- **Rank recommendations** by expected impact × evidence ÷ implementation risk.
- **Check the last review:** were its recommendations implemented, and did they
  help? (git log / `HANDOFF.md` / new tests.)

---

## How to run it

### Step 0 — Re-ground (don't reuse a past rating from memory)
- `git log --oneline -12` and `git status` — what moved since last time.
- `find . -name '*.py' -not -path './.git/*' | xargs wc -l | tail -1` — size.
- Ensure deps, then run the suite (it is the behavioral source of truth):
  `pip install pytest "websockets>=12.0" flask -q` if needed, then
  `python3 -m pytest tests/ -q`. Record pass/skip counts. **A failing suite
  caps the overall grade** — report the failing output.
- Recover the last review from `HANDOFF.md` (grep `updated-review`, `rating`,
  `/10`, `/100`) and note its recommendation list — you will check each off.

### Step 1 — Pull the REAL records (read-only)
The live data runs on Replit; the `data/*.sqlite3` in a fresh container is
**absent or a seed/empty copy** (`data/` is gitignored). The Repl publishes the
real ledgers hourly to the dedicated **`learning-snapshots`** branch
(`tools/learning_export.py`). **Pull that FIRST** — it is the only real data a
container has:

```bash
git fetch origin learning-snapshots
# at-a-glance: curated scoreboards + row counts + when/which commit
git show origin/learning-snapshots:learning_snapshot.json | python3 -m json.tool | less
# raw ledgers for matched-row / arbitrary SQL (gitignored locally, so restore to /tmp):
mkdir -p /tmp/ledgers
for db in q15_v95_ledger_v1 q15_challenger_shadow_v1 \
          q15_ultoim_v2_v1 q15_interval_research_v1 q15_ultoim_v1; do
  git show "origin/learning-snapshots:dbs/$db.sqlite3.gz" | gunzip > "/tmp/ledgers/$db.sqlite3"
done
```

Check `learning_snapshot.json`'s `generated_at` / `git_commit` for freshness and
to confirm the snapshot matches the code under review. If the branch is missing
or stale (export not deployed yet, or a thin live record), say so and grade the
affected dimensions **INSUFFICIENT DATA** — never fabricate.

**Important — the snapshot's curated `scoreboards` block only carries `v95` and
`challenger`. There is NO V2 scoreboard in `learning_snapshot.json`.** V2 must
be graded from the **raw** DBs you just gunzipped:
- `q15_ultoim_v2_v1.sqlite3` — tables `ultoim_v2_predictions`,
  `ultoim_v2_exit_warnings`, `ultoim_v2_report_lock`, `ultoim_v2_alert_lock`,
  `ultoim_v2_meta`. This is V2's prediction + exit ledger.
- `q15_interval_research_v1.sqlite3` — table `interval_captures`: the
  per-checkpoint **chart** (one row per interval per contract-window:
  15M/13M/12M/11M/10M/9M/8M/7M, with roles EARLY_RESEARCH /
  INTERMEDIATE_RESEARCH / OFFENSIVE_ENTRY / CONFIRMATION_DEFENSIVE[_RESEARCH]).
  Columns include `calibrated_yes_probability`, `flip_probability`,
  `manipulation_score`, `manipulation_suspected`, `distance_from_strike`,
  `yes_ask_cents`, `predicted_side`, `entry_recommended`, `trade_decision`,
  `official_result`, `correct`, `realized_pnl_cents`.
- **There is no OHLC candle table** — `q15_v94_context.sqlite3` ships empty.
  Reconstruct "the chart" / probability path from `interval_captures` +
  `ultoim_v2_predictions`, not from candles.

Point constructors at the dumps via env, e.g.
`Q15_V95_LEDGER_DB=/tmp/ledgers/q15_v95_ledger_v1.sqlite3`,
`Q15_CHALLENGER_DB=...`. For V2, open the ledger directly with its real
constructor and DB path (see the V2 track below).

- DBs: `data/q15_v95_ledger_v1.sqlite3` (Your System), the challenger DB
  `data/q15_challenger_shadow_v1.sqlite3` (env `Q15_CHALLENGER_DB`; table
  `shadow_predictions`, opened via `challenger.ledger.ShadowLedger(db_path)`),
  and the V2 DBs above.
- Dump the scoreboards as JSON without guessing internals, e.g.:
  ```python
  from q15_upgrade import ledger_v95
  L = ledger_v95.V95Ledger()          # use the repo's real constructor
  import json
  print(json.dumps(L.scoreboard(), default=str)[:4000])          # by_interval (15M/10M/7M), by_rank (#1/#2/#3), direction, asset, flip, manip
  print(json.dumps(L.official_scoreboard(), default=str)[:2000]) # official W/L record
  print(json.dumps(L.shadow_signal_experiment(), default=str)[:3000])  # the 5-feature A/B
  ```
- Row counts straight from SQLite (predictions, sent_predictions,
  flip_warnings, entry_followups, official_report_lock, shadow_predictions,
  **ultoim_v2_predictions, ultoim_v2_exit_warnings, interval_captures**) so
  you can state n for every grade and spot stored-but-ungraded / sent-but-not-
  official rows.
- If `scoreboard()`/`shadow_signal_experiment()`/V2 `scoreboard()` returns
  `available: False` or n≈0, say so plainly and grade those dimensions as
  INSUFFICIENT DATA.

### Step 1b — V2 (ultoim_v2) audit track  ← NEW, run this in full

V2 is a separate model. Grade it on its own /100 from its own ledger. Open it
with the real constructor (do not hand-roll SQL for the scoreboard — use the
module so you grade exactly what the live code grades):

```python
from q15_upgrade.ultoim_v2.ledger import UltoimV2Ledger
import json
V = UltoimV2Ledger("/tmp/ledgers/q15_ultoim_v2_v1.sqlite3")
mv = "ultoim-v2"  # confirm via: SELECT DISTINCT model_version FROM ultoim_v2_predictions
print(json.dumps(V.scoreboard(mv), default=str)[:4000])             # by interval / side / regime / manip
print(json.dumps(V.exit_warning_scoreboard(mv), default=str)[:2000])# exit-warning precision
print(json.dumps(V.s15_research_scoreboard(mv), default=str)[:2000])
print(json.dumps(V.distance_research_scoreboard(mv), default=str)[:2000])
for r in V.loss_rows(mv): print(r)                                  # walk every loss
```

Then run these audits directly on the raw DBs (real records only; if a cell is
thin, write INSUFFICIENT DATA (n=…) — V2's own `min_n` is 30 for the main
scoreboard and 10 for exit warnings, so almost everything here is **thin** and
must be labelled as such, reported as raw counts, not graded as settled rates):

1. **Per-interval accuracy of FIRED entries** (`ultoim_v2_predictions WHERE
   fired=1 AND correct IS NOT NULL`, GROUP BY `interval`). Report n + acc for
   15M / 10M / 7M. **Flag the EARLIEST checkpoint (15M) as the weak link** if
   the data confirms it — the established pattern is 15M far worse than 10M/7M.
   State whether firing at 15M is net-negative vs waiting for the 10M offensive
   checkpoint, and whether the loss rows cluster at 15M.

2. **One-sided NO-only design test.** Confirm `fired=1` rows are 100% `NO`
   (`SELECT predicted_side, COUNT(*) … WHERE fired=1 GROUP BY predicted_side`).
   Then check whether V2 *ever forms a YES view internally*: look at
   `record_kind='RESEARCH_YES'` and at `predicted_side='YES'` rows with
   `fired=0` — V2 records YES research but never delivers it. The gate is at
   `q15_upgrade/ultoim_v2/gate.py` (gate_a is `(not cfg.no_only) or side ==
   "NO"`, ~`gate.py:163`); the default `no_only=True` lives in
   `config.py` (~`config.py:75`, env `Q15_ULTOIM_V2_NO_ONLY`). Judge: is NO-only
   *earning* its restriction (is the NO base rate / NO accuracy genuinely
   better), or is it leaving graded-but-undelivered YES winners on the table?
   Quantify the foregone YES research accuracy if resolved YES-research rows
   exist; otherwise INSUFFICIENT DATA.

3. **Reconstruct the per-checkpoint chart** for the resolved windows from
   `interval_captures` (ORDER BY `seconds_remaining DESC` per `window_key` +
   `ticker`). For each window show the path of `calibrated_yes_probability`,
   `flip_probability`, `manipulation_suspected`, `distance_from_strike`,
   `predicted_side`, `entry_recommended`, `trade_decision` across
   15M→13M→…→7M, and the final `official_result`. This is "the chart" — there
   are no candles. Use it to see *where the side actually flipped* relative to
   where V2 fired.

4. **Audit the exit warnings AND run the 10M counterfactual.** From
   `ultoim_v2_exit_warnings`: report n, how many were `warning_correct=1` vs
   `false_alarm=1` (precision), the entry intervals, `warn_seconds_remaining`,
   and `recovered_cents`. Then run the explicit counterfactual the owner cares
   about: **"could the flip have been caught at the 10M (600s) checkpoint?"**
   - The exit watcher is **structurally unable to fire before 7M.**
     `runner.py:_maybe_exit_warning` (~`runner.py:444`) returns early when
     `seconds_remaining > cfg.exit_watch_from_seconds` (~`runner.py:452`), and
     `exit_watch_from_seconds` defaults to **420.0s = 7M**
     (`config.py:191`, env `Q15_ULTOIM_V2_EXIT_WATCH_SECONDS`). So at the 10M
     (600s) checkpoint the watcher is disabled by design.
   - For each exit-warning window, pull that window's 10M and 9M
     `interval_captures` rows and check whether the NO→YES deterioration was
     **already visible at 10M** (rising `flip_probability`, `manipulation_
     suspected` flipping 0→1, `calibrated_yes_probability` crossing 0.5,
     `distance_from_strike` collapsing to 0). If it was visible at 10M but the
     warning didn't fire until <420s, that is a **real, quantified
     opportunity** — report the avg seconds and cents that earlier exit would
     have saved, as a candidate to lower `exit_watch_from_seconds` to the 10M
     mark (gated, test-backed).
   - Known reference cases to verify against the live data (do not assume —
     re-pull and confirm): a BTC window where a 10M re-screen row shows
     `flip_probability` jumping and `manip` flipping 0→1 vs the 15M entry but
     was only used to decline a *new* entry, never to exit the *existing* NO;
     and an XRP window where the NO fired at the most-confident-NO 10M point and
     then flipped to YES at the very next (9M) checkpoint and stayed YES.

5. **Manipulation-suspected: predictive AND gating?** Two separate tests:
   - *Predictive:* split FIRED+resolved entries by `manipulation_suspected`
     (0 vs 1) and by `regime_name` (THRESHOLD_PIN vs HIGH_VOLATILITY); report n
     + acc each. The established pattern is manip=1 *worse* than manip=0 and
     THRESHOLD_PIN worse than HIGH_VOLATILITY — confirm or refute, labelling
     thin.
   - *Gating:* read `gate.py` / `screen.py` to determine whether
     `manipulation_suspected` actually **blocks or de-weights** an entry, or is
     merely *recorded* and fires anyway. If manip=1 entries still fire at a
     materially worse hit rate, that is a candidate gate (default-OFF,
     `Q15_*`-gated). Cite the exact gate line for whatever you conclude.

Bring every V2 number into the output block's V2 section (below). Where n is
under V2's own `min_n`, print the raw count and the word INSUFFICIENT — do not
launder a thin count into a confident percentage.

### Step 2 — Fan out the audit in parallel (Explore agents, one message)
Each agent is read-only, returns `file:line` findings, a sub-grade, and clearly
separates **confirmed** from **suspected**. Cover:

- **A) Records, grading correctness & Shadow-vs-Yours comparison** —
  `q15_upgrade/ledger_v95.py` (`scoreboard`, `official_scoreboard`,
  `_scoreboard_rows`, resolve/grading, dedup via `official_report_lock` /
  `sent_predictions`), `q15_upgrade/challenger/{ledger,stats,experiment}.py`
  (`paired_differences`, `mcnemar`, `block_bootstrap_ci`, `holdout_tainted`),
  `performance.py`, `db.py`. Verify: correct contract/asset matching, correct
  settlement results, sent-but-excluded / unsent-but-counted accounting,
  duplicate grading, and that any Shadow-vs-Yours comparison is matched on the
  same snapshot+timestamp (no look-ahead/holdout taint).
- **B) Feature pipeline** — `q15_upgrade/shadow_signals.py` end-to-end
  (compute → store → `resolved_shadow_signal_rows` → `evaluate` significance),
  `notifications/reporting.py` (`_shadow_signal_lines`). For each of the 5
  features decide HELPING / HURTING / INSUFFICIENT / BROKEN with n and the OOS
  test result, and flag any feature that is missing, always-null, stale,
  computed-but-unused, or displayed-but-not-computed.
- **C) Live-workflow bug sweep** — `q15_upgrade/checkpoint_v95.py` (`run_cycle`,
  checkpoint scheduling for 15M/10M/7M, ranked-pick build for #1/#2/#3,
  delivery reconcile), `q15_upgrade/window_focus.py`, `app.py` loop,
  `cycle_watchdog.py`. Hunt the full bug checklist below.
- **D) Decision & feed code quality** — `q15_upgrade/checkpoint_v95.py` math,
  `analysis.py`, `spot_client.py`, `q15_upgrade/market_data_v95.py`,
  `notifications/notifier.py`: None/stale handling, `Decimal` money, div-zero,
  prob/edge math, HTML + suppression-marker (`V9.5 CHECK`) preservation.
- **E) V2 (ultoim_v2) code & grading correctness** — `q15_upgrade/ultoim_v2/`
  end-to-end: `screen.py` + `gate.py` (gate_a/b/c, NO-only restriction,
  manipulation handling), `validate.py` (s15 second-stage screen),
  `runner.py` (cycle, entry firing, `_maybe_exit_warning` and the 420s watch
  window, re-screen handling), `ledger.py` (`record_decision`, `resolve`,
  `scoreboard`, `exit_warning_scoreboard`, the `UNIQUE(model_version, ticker,
  interval)` keys), `fifteen_min.py`, `panel.py`, `telegram.py`,
  `config.py` defaults. Verify: fired/resolved/correct accounting matches the
  raw rows; exit-warning resolution and precision are graded correctly;
  per-interval scoreboard math is right; no NO-only YES research leaks into the
  delivered record; the 10M deterioration data is captured but (per the live
  code) NOT used to exit an open NO — confirm that gap at `file:line`. Apply
  the same no-look-ahead and thin-sample rules.

Skip the frozen legacy chain (`checkpoint_v91..v94*`) unless base behavior is in
question (per `CLAUDE.md`).

### Step 3 — Verify, don't just trust the agents
Spot-check the highest-impact agent claims yourself (read the exact lines)
before reporting them as confirmed. Re-state any claim you couldn't verify as
"suspected".

---

## Grading

Grade **Your System (v95)**, **Shadow**, and **V2 (ultoim_v2)** *separately*,
each /100, from real records. Weight toward what makes money on this product:

| Dimension | What to check |
|---|---|
| Final-outcome accuracy | resolved correct / n, with Wilson CI |
| Probability calibration | Brier / log-loss, calibration vs realized |
| 15M / 10M / 7M accuracy | per-interval accuracy + n each |
| #1 / #2 / #3 ranking quality | per-rank accuracy & P&L; does rank order track skill? (v95) |
| YES vs NO performance | accuracy split by side (asymmetry?) — and for V2, whether NO-only is justified |
| Flip / exit detection | `flip_warnings` (v95) and V2 exit warnings: precision/recall vs realized flips, and timeliness (could it fire earlier?) |
| Manipulation detection | manip-flag accuracy / P&L split; for V2, is it predictive AND does it gate? |
| Entry timing | entry-recommended hit rate; for V2, the per-interval firing decision and the 15M weakness |
| Data quality | stale/None/last-good fallback rates, snapshot freshness |
| Learning quality | shadow weight drift within caps, calibration convergence |
| Reliability after restarts | durable ledger/locks, no re-grade/loss on restart |
| Telegram delivery | sent vs failed vs muted; outbox reconcile correctness |
| Duplicate prevention | one official report per (version, interval, window) |

The **overall grade /100** reflects how well the *whole system does its job*:
correctness of the live prediction/alert path and grading, statistical honesty,
robustness to feeds/restarts, and signal quality — not generic tidiness.
**Any failing test, fabricated stat, or confirmed grading bug caps the grade.**
For **V2 specifically**, weight the per-interval honesty (don't average away a
bad 15M), the legitimacy of the NO-only restriction, and the exit-warning
timeliness (the 10M counterfactual). If V2's resolved n is below its own
`min_n`, the V2 grade is **provisional / INSUFFICIENT** and must say so.

## Feature test rule
Only the 5 background features below, mapped to `shadow_signals.SIGNAL_NAMES`:
order-flow persistence→`order_flow_persistence`, book resiliency→
`book_resiliency`, prediction stability/flip risk→`prediction_stability`,
market noise/entropy→`entropy_noise`, regime transition→`regime_transition`.
They stay in **background Shadow testing**. Mark each:
- **HELPING** — significant OOS gain in accuracy/calibration or fewer false
  flips, with n ≥ module `min_rows`.
- **HURTING** — significant OOS degradation.
- **INSUFFICIENT** — n too small / no resolved OOS samples yet.
- **BROKEN** — missing, always-null, stale, computed-but-unused, or display-only.

Never recommend activating a feature just because it exists — require completed
samples and out-of-sample evidence. (V2's `manipulation_suspected`, regime, and
distance signals are evaluated under the V2 track above, not here.)

## Bug checklist (sweep the whole live workflow)
Missing 15M/10M/7M predictions · missing #1/#2/#3 picks · Shadow / Your System /
**V2** producing no results · predictions generated but not stored · stored but
not graded · successfully-sent predictions excluded from official records ·
unsent predictions counted as official · duplicate predictions/grading · wrong
contract/asset matching · wrong settlement results · time-zone & scheduling
errors · stale/missing market data · features calculated but never used ·
features displayed but not calculated · **deterioration captured but never acted
on (V2's 10M re-screen used only to decline new entries, never to exit an open
NO)** · **exit warning structurally unable to fire before 7M** · look-ahead /
future-data leakage · restart-related data loss · DB inconsistencies · Telegram
delivery failures · placeholder or fabricated statistics. Classify each
Critical / Important / Minor and mark confirmed vs suspected.

---

## Output format

Lead with this exact, easy-to-read block (fill from real records; use
`INSUFFICIENT DATA (n=…)` wherever records are thin or absent — never invent):

```
UPDATED REVIEW

Overall system grade: __/100

SHADOW
Accuracy: __%
15M: __%
10M: __%
7M: __%
Main strength: ___
Main weakness: ___
Grade: __/100

YOUR SYSTEM (v95)
Accuracy: __%
15M: __%
10M: __%
7M: __%
Main strength: ___
Main weakness: ___
Grade: __/100

V2 (ultoim_v2)
Fired entries (n): __   (delivered side: NO __ / YES __  — NO-only design)
Accuracy 15M: __% (n=__)   10M: __% (n=__)   7M: __% (n=__)
Weak checkpoint: ___ (expect 15M)
Exit-warning precision: __/__ correct (__%), avg warn @__s left, avg recovered __c
10M-early-exit opportunity: caught-at-10M __/__ windows; avg __s / __c earlier exit possible
Manipulation predictive?: manip=1 __% (n=__) vs manip=0 __% (n=__)  → YES/NO/INSUFFICIENT
Manipulation gates entry?: YES (file:line) / NO — recorded only
Main strength: ___
Main weakness: ___
Grade: __/100   (mark PROVISIONAL if resolved n < min_n=30)

FEATURE TESTS
Order-flow persistence: HELPING / HURTING / INSUFFICIENT / BROKEN
Book resiliency: HELPING / HURTING / INSUFFICIENT / BROKEN
Prediction stability: HELPING / HURTING / INSUFFICIENT / BROKEN
Entropy/noise: HELPING / HURTING / INSUFFICIENT / BROKEN
Regime transition: HELPING / HURTING / INSUFFICIENT / BROKEN

BUGS FOUND
Critical: __
Important: __
Minor: __

BEST NEXT UPDATES
Highest impact: ___
Medium impact: ___
Polish: ___

RECOMMENDATION
Keep testing / Activate feature / Repair first / Roll back
```

Then, below the block, add the supporting detail (this is where the evidence
lives):

- **Shadow vs Your System (matched rows only)** — the head-to-head on the same
  snapshot+timestamp: per-interval and per-rank deltas with n, paired test
  result (mcnemar/bootstrap), and whether the holdout is untainted.
- **#1/#2/#3 ranking** — does accuracy/P&L decline monotonically by rank? n each.
- **YES vs NO, flips, manipulation, entry timing** — splits with n.
- **V2 (ultoim_v2) detail** — (1) per-interval table with n and the 15M-weakness
  call; (2) the NO-only verdict: is the restriction earning its keep, and what
  resolved YES-research accuracy (if any) is being left undelivered; (3) the
  reconstructed per-checkpoint chart for each exit-warning window (prob /
  flip_probability / manip / distance path 15M→7M → settlement); (4) the exit
  warnings list with precision and the **10M counterfactual** — per window,
  was the flip visible at 10M, and the avg seconds/cents an earlier exit would
  have saved, citing `runner.py:452` + `config.py:191` for why it can't fire
  before 7M today; (5) manipulation predictiveness + whether it gates, with the
  gate `file:line`.
- **Bugs** — each as `file:line`, Critical/Important/Minor, **confirmed** vs
  **suspected**, with the one-line fix.
- **Best next updates** — ranked by impact × evidence ÷ risk; each says which
  grade dimension it moves and roughly how much. Prefer default-OFF
  `Q15_*`-gated, test-backed changes for anything touching model behavior
  (e.g. lowering `exit_watch_from_seconds` to the 10M mark behind
  `Q15_ULTOIM_V2_EXIT_WATCH_SECONDS`, or a manip-gate behind a new flag — only
  if the data supports it and with a test).
- **Last review's recommendations** — a checklist: implemented? working?
  measurably helped? (cite git log / `HANDOFF.md` / the test that proves it).

Keep the top block scannable; keep the detail honest. End with the single
highest-leverage next step.
