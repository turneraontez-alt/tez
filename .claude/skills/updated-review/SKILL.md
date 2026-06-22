---
name: updated-review
description: Full audit of the tez prediction system — grades Shadow and Your System separately, compares 15M/10M/7M and #1/#2/#3 ranking, tests the background features, sweeps the live workflow for bugs/missing-data/grading errors, checks whether the last review's fixes landed, and recommends evidence-backed upgrades. Invoke when the user says "updated review", "update the review", "audit the system", "review my code", "rate my code", or asks for a fresh score of this repo.
---

# Updated Review — system auditor

Act as an ongoing **auditor, performance grader, bug detector, and upgrade
planner** for both prediction systems in this repo (a read-only Kalshi 15-min
crypto binary paper-trading monitor):

- **Your System** — the live frozen-champion model (v95) that produces the
  *official* 15M/10M/7M predictions and the #1/#2/#3 ranked picks.
- **Shadow** — the observational challenger(s): the out-of-sample challenger in
  `q15_upgrade/challenger/` (table `shadow_predictions`) and the background
  shadow-signal A/B in `q15_upgrade/shadow_signals.py`. Shadow learns and is
  graded but **never** drives a live alert.

This is a **read-only** skill. **Do not edit, commit, or push** unless the user
explicitly asks for implementation. The deliverable is the written review.

---

## Review rules (apply to everything below)

- **Use real stored records and live code paths.** Numbers come from the
  ledgers/DBs and the scoreboard methods — never from memory or estimation.
- **Never invent percentages or sample sizes.** If a record is empty, missing,
  or the local DB is a seed copy, write **INSUFFICIENT DATA (n=…)** — do not
  fabricate a number to fill the template.
- **Compare Shadow vs Your System only on matched rows** — same frozen data
  snapshot *and* same prediction timestamp/checkpoint. Give neither system
  credit for a prediction created after extra data was collected (look-ahead).
- **Separate confirmed bugs from suspected issues**, and **correlation from
  proven improvement** (require completed samples + out-of-sample evidence
  before calling a feature "helping").
- **Label thin samples.** A result under the module's own `min_rows` / n<30 is
  INSUFFICIENT, not a grade.
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
for db in q15_v95_ledger_v1 q15_challenger_shadow_v1; do
  git show "origin/learning-snapshots:dbs/$db.sqlite3.gz" | gunzip > "/tmp/ledgers/$db.sqlite3"
done
```

Check `learning_snapshot.json`'s `generated_at` / `git_commit` for freshness and
to confirm the snapshot matches the code under review. If the branch is missing
or stale (export not deployed yet, or a thin live record), say so and grade the
affected dimensions **INSUFFICIENT DATA** — never fabricate. With the gunzipped
DBs in `/tmp/ledgers`, point the constructors at them via
`Q15_V95_LEDGER_DB=/tmp/ledgers/q15_v95_ledger_v1.sqlite3` (and
`Q15_CHALLENGER_DB=...`) for the dumps below.

- DBs: `data/q15_v95_ledger_v1.sqlite3` (Your System), and the challenger DB
  `data/q15_challenger_shadow_v1.sqlite3` (env `Q15_CHALLENGER_DB`; table
  `shadow_predictions`, opened via `challenger.ledger.ShadowLedger(db_path)`).
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
  flip_warnings, entry_followups, official_report_lock, shadow_predictions) so
  you can state n for every grade and spot stored-but-ungraded / sent-but-not-
  official rows.
- If `scoreboard()`/`shadow_signal_experiment()` returns `available: False` or
  n≈0, say so plainly and grade those dimensions as INSUFFICIENT DATA.

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

Skip the frozen legacy chain (`checkpoint_v91..v94*`) unless base behavior is in
question (per `CLAUDE.md`).

### Step 3 — Verify, don't just trust the agents
Spot-check the highest-impact agent claims yourself (read the exact lines)
before reporting them as confirmed. Re-state any claim you couldn't verify as
"suspected".

---

## Grading

Grade **Your System** and **Shadow** *separately*, each /100, from real records.
Weight toward what makes money on this product:

| Dimension | What to check |
|---|---|
| Final-outcome accuracy | resolved correct / n, with Wilson CI |
| Probability calibration | Brier / log-loss, calibration vs realized |
| 15M / 10M / 7M accuracy | per-interval accuracy + n each |
| #1 / #2 / #3 ranking quality | per-rank accuracy & P&L; does rank order track skill? |
| YES vs NO performance | accuracy split by side (asymmetry?) |
| Flip detection | `flip_warnings` precision/recall vs realized flips |
| Manipulation detection | manip-flag accuracy / P&L split |
| Entry timing | entry-recommended hit rate, `entry_followups` |
| Data quality | stale/None/last-good fallback rates, snapshot freshness |
| Learning quality | shadow weight drift within caps, calibration convergence |
| Reliability after restarts | durable ledger/locks, no re-grade/loss on restart |
| Telegram delivery | sent vs failed vs muted; outbox reconcile correctness |
| Duplicate prevention | one official report per (version, interval, window) |

The **overall grade /100** reflects how well the *whole system does its job*:
correctness of the live prediction/alert path and grading, statistical honesty,
robustness to feeds/restarts, and signal quality — not generic tidiness.
**Any failing test, fabricated stat, or confirmed grading bug caps the grade.**

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
samples and out-of-sample evidence.

## Bug checklist (sweep the whole live workflow)
Missing 15M/10M/7M predictions · missing #1/#2/#3 picks · Shadow or Your System
producing no results · predictions generated but not stored · stored but not
graded · successfully-sent predictions excluded from official records ·
unsent predictions counted as official · duplicate predictions/grading ·
wrong contract/asset matching · wrong settlement results · time-zone &
scheduling errors · stale/missing market data · features calculated but never
used · features displayed but not calculated · look-ahead / future-data leakage
· restart-related data loss · DB inconsistencies · Telegram delivery failures ·
placeholder or fabricated statistics. Classify each Critical / Important / Minor
and mark confirmed vs suspected.

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

YOUR SYSTEM
Accuracy: __%
15M: __%
10M: __%
7M: __%
Main strength: ___
Main weakness: ___
Grade: __/100

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
- **Bugs** — each as `file:line`, Critical/Important/Minor, **confirmed** vs
  **suspected**, with the one-line fix.
- **Best next updates** — ranked by impact × evidence ÷ risk; each says which
  grade dimension it moves and roughly how much. Prefer default-OFF
  `Q15_*`-gated, test-backed changes for anything touching model behavior.
- **Last review's recommendations** — a checklist: implemented? working?
  measurably helped? (cite git log / `HANDOFF.md` / the test that proves it).

Keep the top block scannable; keep the detail honest. End with the single
highest-leverage next step.
