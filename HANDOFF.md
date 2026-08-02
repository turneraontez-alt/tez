# Session handoff

## Shipped THIS session 2026-08-01 - Reversal watch: paper delivery of the challenger cheap-YES pocket

Ledger analysis session (read-only) + one narrowly scoped delivery wiring.

**The audit (forward-settled evidence, not backtest).** challenger-v5 was frozen
2026-06-22 20:38 UTC and its shadow began recording the same minute, so its
entire 5.5-week settled record is forward/out-of-sample. Across the v95 shadow
ledger (`data/q15_challenger_shadow_v1.sqlite3`, 64,886 predictions) plus the
ultoim_v2 ledger (13,350 picks) and the 2026-06-25 live executor day:

- The fired NO book is net-negative after fees: 10M NO 77.1% win vs 78.1%
  break-even (-1.50c/bet, n=834). HYPE -21.1c/bet, SOL -6.3c, BNB -3.0c,
  DOGE -3.1c; BTC +1.8c and XRP +0.8c carry it. The live executor day
  contract-weighted -$735 as traded vs -$23 with HYPE/SOL/BNB/DOGE removed.
- The one large positive-margin pocket: challenger BUY_YES with executable
  ask <45c at 10M/7M on BTC/ETH/DOGE — 131 settled events, 79.4% win,
  +42.8c/contract net of the full cost model; clustered-bootstrap 5th
  percentile +36.5c; smooth at every ask ceiling 40-55c (not a tuned knife
  edge). 15M, expensive YES, and the NO side were negative or unproven.
- Overfit audit downgraded two candidate rules: BTC-only NO (z=+0.69, not
  significant) and the 78c price floor (z=+0.44). The toxic-alt cull is
  bootstrap-confirmed negative-bleed and independently confirmed by the live
  day. The cheap-YES pocket remains exposed to fill realism (never executed),
  single-regime evidence, and v5's own training provenance
  (`training_period: NOT_PROVIDED` in lineage) — hence PAPER delivery, not
  executor wiring.

**The wiring (this change).** New `q15_upgrade/challenger/reversal_watch.py`:
the preregistered gate (10M/7M, BTC/ETH/DOGE, BUY_YES, ask <45c — defaults ARE
the frozen rule), an idempotent `reversal_alert_lock` claim table inside the
challenger's own SQLite (restart-safe, one alert per contract+checkpoint), and
a `REVERSAL WATCH - PAPER` Telegram card that re-reports the pocket's live
settled record (n / win rate / avg net P&L) on every fire so decay is visible
on the message that depends on it. Wired in `ShadowRunner.observe` (after the
shadow row records; never raises into production) and drained in `app.py`
beside the challenger report via `notifier.send`. Gated on
`Q15_CHALLENGER_REVERSAL_WATCH` (default OFF; documented in `.env.example`).
Strictly read-only: no order path was added anywhere; the executor invariants
are untouched. New regression `tests/test_challenger_reversal_watch.py` 11/11
(gate exactness, asset/checkpoint/ask rejections, restart-safe idempotency,
message marker, pocket-record grading, runner wiring, default-OFF).
Challenger suite 88/89 locally; the single failure
(`test_numpy_fit_matches_dependency_free_fallback`) is a managed-runtime numpy
version artifact — it compares the numpy fast path against the pure-python
fallback and neither path was touched by this diff (verify in the project venv
before deploy). Full-suite and config-audit runs were blocked in this sandbox
by OneDrive-dehydrated files (pre-existing: `notifications/notifier.py` is a
placeholder), so `python3 -m pytest tests/ -q` must be run from the Repl/venv
before this is considered shipped there. Enabled locally via
`Q15_CHALLENGER_REVERSAL_WATCH=true` in `.env.local`; takes effect on the next
app restart.

## Current 2026-08-01 - V21 historical-first PAPER deployment contract frozen

A separate historical-first V22 top-book challenger is now preregistered at
protocol SHA
`1f81a05dd679e68e1713cc95d661f93b744257fe77f3ae44db606c4968753a9a`.
It does not change frozen V21.  Its source-only readiness initially failed
closed at zero V22 rows because inheriting the full V21 vector also inherited
the intermittent Coinbase spot-depth failure for BNB/HYPE.  Before any V22
feature credit, outcome, label, fit, or score, the protocol was honestly
amended: V22 retains the 62 RTI/Kalshi features that do not depend on that
source, excludes all 14 V21 spot-derived fields, and adds 29 independently
captured official REST top-book features over the parent/+30/+60/+90 stages.
The final feature map has 91 features and names SHA
`d0823eb838a834d43eaffe28267fe71b5fb8dfba18d613d78b871b42c4b0d853`.
Timing/provider/failure metadata and every outcome/label field are forbidden
as model inputs.  Neutral internal spot values only permit reuse of frozen
nonspot formulas and are dropped before the vector/hash; adversarial tests
prove changing those excluded values cannot change V22 evidence.

After recovery and the next protected capture, V22 readiness reached **12/180**
complete common windows, **84** rows, **58** row-level executable rows, zero
REST or common-feature failures, and evidence SHA
`82e3236aa2b8e9de5a808c7c922745b81de3bdf9be005e90e0d1ccb6be821175`.
Frozen V21 is available on 78/84 matched rows; V22 safely represents the six
rows where V21's unrelated spot source was unavailable.  No outcomes, labels,
profitability results, model fits,
paper alerts, orders, promotion, or real trading were opened.  The eventual
audit must use the earliest 180 complete V22 windows, chronological
105/25/25/25 partitions, separate BTC/non-BTC cohorts, an independently fit
62-feature base ablation, official fees, 2c slippage, actual ten-contract
depth, and a one-shot untouched test before paper consideration.

V22's evaluator is now independently hash-frozen at SHA
`9273b3fad068a388d093fdf575696826fa0386440e49a5879550b5c5907cf1d0`
before any V22 labels, fit, or score.  It freezes the model grids, train-only
walk-forward selection, disjoint calibration and policy partitions, margin
grid, cluster bootstrap, Wilson/break-even and after-cost gates, matched V21
diagnostic, rejected-trade counterfactuals, and the independently fit
62-feature base ablation plus report-only distance, volatility, imbalance,
spread, and path-curvature tiers.  The tier amendment occurred outcome-blind
at four feature windows with zero labels, models, or scores.  Both official
Kalshi API domains were checked
source-only at freeze time: all seven series reported `fee_type=quadratic`
and `fee_multiplier=1`.  Two consecutive live V22 readiness recomputations
produced the same evidence SHA, proving deterministic reconstruction at the
then-current 3-window checkpoint.  The current dedicated V22 regression
passes **35/35**.

`tools/q15_rti_v22_feature_seal.py` now provides the exclusive outcome-blind
earliest-180 seal gate.  It reconstructs rows through the same deterministic
collector, freezes exact parent/+30/+60 and four-stage REST identities,
enforces all-seven same-close chronology and 105/25/25/25 partitions, hashes
all 1,260 rows, refuses outcome/label fields, and uses exclusive durable
creation with exact confirmation
`SEAL_V22_EARLIEST_180_FEATURES_NO_LABELS`.  At 12/180 it correctly creates
nothing and reports 168 remaining; synthetic 180-window tamper, partition,
idempotency, and exclusive-mismatch tests pass.

V22's future label population is also frozen outcome-blind in
`tools/q15_rti_v22_pretest_binding.py`, with immutable audit identities in
`q15_upgrade/strategy_bots/rti_microstructure_v22_audit_identity.py`.  It
permits all TRAIN and CALIBRATION rows plus only executable POLICY rows,
commits separately to all 175 untouched-test IDs, proves the two sets are
disjoint, requires authoritative label evidence, and cannot access a database,
network, labels, models, reservation state, notifications, or orders.  The
manual phrases are `OPEN_V22_TRAIN_CAL_POLICY_LABELS_ONCE` and
`SCORE_V22_UNTOUCHED_TEST_ONCE`.  Its live dormant status confirms that the
feature seal and reservation do not exist and no labels have been read.

The complete dormant V22 audit engine now exists in
`tools/q15_rti_v22_modeling.py`, `tools/q15_rti_v22_pretest_runner.py`,
`tools/q15_rti_v22_untouched_test_runner.py`, and their two manual command
entry points.  Synthetic sealed chronology proves both cohorts can pass the
full walk-forward/calibration/policy pipeline and be scored once on the
untouched test without refit, recalibration, margin reselection, notification,
promotion, or trading.  The runners reserve label access before the callback,
never reread after a crash or finalized run, hash-bind the exact V22 REST and
feature identities, and refuse any call that disables authoritative Kalshi
settlement evidence.  The manual commands verify all seven official
quadratic-fee series before burning a reservation.  No live V22 seal, labels,
fit, score, artifact, alert, promotion, or trading has been created.

At 19:05 ET an adversarial live check caught a misleading health state: the
exact sampler thread was alive and its old miss counter was zero, but refresh
registration was stuck on the 19:00-close contracts, so the 19:02 decision for
the 19:15 close never registered.  That fold is permanently excluded and was
not backfilled.  Logs identified a synchronous Drift delivery reconciliation
that had occupied the live refresh loop for 75.95 seconds and later stalled it.
This maintenance is now dispatched to a single nonblocking daemon worker;
concurrent requests coalesce and `/api/health` exposes its inflight duration,
last result, and the invariant that live-loop blocking is forbidden.  Exact
health now independently computes the latest expected decision slot and marks
missing, stale, or late seven-asset registration even when the sampler thread
is alive.  After restart, the late missed fold produced an honest seven-miss
baseline; the next 19:17 decision registered all seven assets 92.703-118.636s
early, captured all seven exactly, completed all 21 delayed stages and 28 REST
rows, and advanced V22 from 6 to 7 windows with no source failures.  The focused
exact/Drift/health regression passes **63/63** (62-test run plus the explicit
health-worker check), and no scoring or outcomes were opened.

An independent Windows collector watchdog is now installed as the scheduled
task `Q15 Collector Watchdog` and runs every minute outside the app process.
It verifies fresh app data, all-seven exact registration, the exact and delayed
threads, all seven official REST workers, settlement coverage, the nonblocking
Drift invariant, and the frozen exact miss baseline of **7**.  It requires two
consecutive failures, refuses every restart inside the exact-capture protection
window, enforces a 20-minute restart cooldown, and always launches through the
safe dry-run/kill-switch defaults.  Its baseline cannot move automatically;
any eighth miss remains a durable failure.  The first manual and scheduled
runs both returned `HEALTHY`, Task Scheduler returned result 0, and the focused
watchdog/restart-guard regression passes **11/11**, including rejection of a
counter reset below the frozen baseline.  The next 19:32 capture
advanced V22 from 7 to 8 complete windows with zero source-quality failures,
proving installation did not disturb collection.  The combined V22,
exact-capture, Drift, health-ledger, watchdog, and restart-guard regression
passes **113/113**.

`tools/q15_rti_v22_feature_quality.py` now performs an independent,
outcome-blind structural audit of every accumulating 91-feature row.  It
rehashes each row, validates exact feature/protocol identities and seven-asset
chronology, rejects nonfinite or duplicate evidence, and reports feature
variation and redundancy without reading labels or fitting/scoring anything.
At 12/180 it passes all 84 rows with zero structural, excluded-window, or REST
failures and report SHA
`fa05a2c77fc9076a36694e0473668b41f21a81e2b177ec5351911f0b920f630a`.
The only variation diagnostics are expected to remain informational at this
small sample: `delayed_side_unchanged` is currently constant and two
mathematically related distance/continuation fields correlate at 0.999999878.
The previously identical intermediate/+60 side-stability indicators diverged
on the tenth window, empirically confirming they are not formula duplicates.
Frozen V22 was not changed.  The new
quality regression passes **4/4**.

Disaster recovery now covers the irreplaceable prospective evidence rather
than only live databases.  `tools/local_backup.py` adds a bounded secret-free
`support/` snapshot containing current source, tests, immutable configs,
HANDOFF, watchdog state, and future V22 seal/audit artifacts, with CRC, exact
member geometry, size, and SHA-256 verification on every scheduled archive.
The daily `Q15 Critical Data Backup` task is installed for 03:20 ET and uses a
critical-only set that fits between protected captures; `-IncludeAllState`
retains the comprehensive option.  The first capture-safe archive is
`q15-data-20260801-200510.zip`: 284,651,232 bytes, eight critical SQLite
snapshots, 708 support files, all required V22 files present, no `.env.local`,
and full verification passed.  Backup/restore/restart-guard tests pass **9/9**.

The verified archive is also copied to the physical OneDrive tree at
`OneDrive\Documents\Q15 Critical Backups`, rather than the workspace's
AppData-backed `work` junction.  `Q15 Backup Sync Guard` is installed at second
30 of every minute.  It detects a pending Q15 archive through Windows'
availability status, stops OneDrive 90 seconds before exact capture through the
+90 commit buffer, and resumes hidden only in a safe interval.  Its first
scheduled run returned 0.  A live adversarial exercise stopped a pending 285 MB
upload before the 20:17 capture; all seven exact/+30/+60/+90 stages then passed
and V22 advanced from 10 to 11 without a miss or source failure.  The upload is
queued and actively connected but is not yet claimed remotely complete.  The
expanded backup/sync/restart-guard regression passes **10/10**.

At the 20:32 ET protected capture, the collector remained healthy with the
frozen exact missed-deadline baseline of 7 and V22 advanced cleanly from 11 to
12 complete windows.  The collector and backup-sync scheduled tasks both
returned result 0.  The 285 MB archive still reports `Sync pending`; its full
manifest/hash verification passes (8 databases, 708 support files), and it
contains no real `.env.local`.  A separate reliability issue was found without
touching collection: OneDrive has the workspace `.venv` launcher and NumPy DLL
marked offline, so a fresh process cannot currently start from that environment.
The live collector remains healthy, and the outcome-blind V22 checks were run
from a new AppData-local audit environment.  A pin/hydration request is pending;
do not claim restart safety or remote backup completion until both states verify.

At 17:35 ET the first strictly prospective V2 official spot REST top-book
fold passed its complete outcome-blind integrity check.  The frozen V2
protocol SHA is
`b4e3e342ae73c94679becb917a680020eabf9ee6cd3a80fa14b0781d2eb92a17`;
its boundary is strictly after the 17:30 ET close and its first eligible close
is 17:45 ET.  All **28/28** expected rows were accepted (seven assets at the
parent, +30s, +60s, and +90s stages), all four seven-asset stage windows were
valid, the complete close-window count is **1**, and there were zero quality
failures, duplicate submissions, rejected submissions, or worker errors.
The terminally excluded V1 evidence receives no V2 credit.  No outcome labels,
profitability results, model fitting, alerts, orders, or real trading were
opened by this check.  V21 remains frozen and unchanged at **9/180** complete
windows (63 feature rows, 47 row-level executable), while the independent
execution-ladder reservoir now has 7 usable windows and 10 genuine recovered
full fills; its seven older schema failures remain preserved.

V21 cannot enter PAPER forward use merely because its code exists.  The
outcome-blind deployment/review protocol is frozen at SHA
`81065754fa45ddbccc2a535e7be3327d9e175bf1756b98bd6356a446cad53e66` and
requires a passing 180-window historical audit, including the one-shot
untouched test, before a manual paper artifact can even be considered.  The
manual creation phrase is `CREATE_V21_PAPER_CHALLENGER_FROM_PASSING_AUDIT`;
historical results alone can never promote it, and the future prospective
reviews remain manual at 30, 60, and 150 resolved accepted picks per cohort.
The protocol freezes actual ten-contract depth, official quadratic fees, 2c
adverse slippage, authoritative settlement grading, a WAL ledger, idempotent
Telegram delivery, and no automatic refit, promotion, or real trading.
The final pre-evidence amendment (still 0 V21 rows) freezes one WAL ledger per
cohort at `data/q15_rti_v21_paper_non_btc_transfer_v1.sqlite3` and
`data/q15_rti_v21_paper_btc_v1.sqlite3`; cross-cohort rows in one ledger are
forbidden, matching the separate BTC and transfer model artifacts.

`tools/q15_rti_v21_paper_preregister.py` validates this protocol without any
database, outcome, model, network, notification, or order capability.  A
fail-closed validator bug that interpreted the honest zero-row freeze as a
missing value was corrected; 16 adversarial preregistration tests now cover
the zero case, tampering, exact entry economics, prospective boundary, ledger,
settlement, and review rules.  No paper artifact, runtime scorer, or V21
notification route exists or is enabled.

The dormant historical-to-paper bridge is implemented in
`tools/q15_rti_v21_paper_artifact.py`.  It requires both finalized historical
gates to pass and the exact manual confirmation, revalidates the full audit
chain, copies the frozen base/Platt/V20-ablation models and selected margin
without refitting, writes exclusive hash-bound cohort artifacts, and treats an
interrupted reservation as permanently ambiguous.  Artifact creation alone
does not connect scoring or notifications.  The separate-cohort durable
ledger/outbox/grader is in
`q15_upgrade/strategy_bots/rti_microstructure_v21_paper_ledger.py`: it rejects
historical rows, fake depth, insufficient after-cost edge, and immutable-field
mutation; settlement is authoritative compare-and-set; notification claims
are leased and idempotent; terminal sends cannot be re-enqueued.  Read-only
status is available from `tools/q15_rti_v21_paper_health.py` and currently
reports `DORMANT_AWAITING_PASSING_HISTORICAL_AUDIT`, 0/180, no artifact, and
notifications/trading false.  All 65 V21 tests pass; the targeted collector +
V18-V21 integration regression passes 161/161.  Live collector health remains
OK with exact and delayed threads alive, zero misses/write failures, empty
spool, and connected Coinbase L2.

At 14:30/14:45 ET, heavy local audit tests briefly starved the exact/delayed
scheduler.  The affected seven-asset source rows are diagnostic only and predate
V21's first eligible 15:00 close, so they receive no V21 credit.  The source-only
reservoir readiness check had also been too permissive: it could call a persisted
feature shell observable even when the newly captured confirmation RTI path was
missing.  It now fails closed on official quote timing/source, the exact expected
31- or 61-sample path, freshness ages, original/confirmation sides, path prices,
continuation, and signed strike distance.  The delayed-reservoir report exposes
latest-window health plus the consecutive latest usable-window count while still
retaining every older integrity failure.  Focused reservoir/V21 trajectory tests
pass 23/23.  At 14:41 ET the live feeds had recovered: all seven settlement-index
assets were fresh, queue size was zero, exact and delayed threads were alive, and
the durable spool was empty.  Do not run heavy offline tests inside the exact
parent/+30/+60 capture guard; no outcomes were opened by this diagnosis.

The first V21-eligible 15:00 ET close then captured successfully after keeping
the guard clear: **1/180 complete close windows**, seven feature-complete rows,
one BTC plus six NON_BTC_TRANSFER rows, and five rows with actual ten-contract
displayed-depth execution support (BTC, DOGE, ETH, SOL, XRP).  The eligible
feature-evidence SHA is
`9350d6858274b98e09257b5010eb1f72f545e24897af793fe1510a9a9d3851ea`.
The latest reservoir window is identity- and feature-complete with no latest-
window quality failures; the older missed diagnostic window remains visible,
so the aggregate reservoir status honestly remains an integrity failure.  The
combined V21 plus reservoir regression passes **76/76**.  A post-test live check
still had both threads alive, unchanged old miss counters, zero write failures,
queue/spool zero, and all seven settlement feeds fresh.  This is source evidence,
not profitability evidence: outcomes, fits, scores, alerts, artifacts, promotion,
and real trading all remain unopened/disabled.

Capture protection is now machine-enforced for local work.  The previous daily
storage defaults (02:45 maintenance and 03:00 backup) both fell inside exact-
capture guards, while the scripts themselves only had restart protection.  A
new pure `Get-Q15ExactCaptureWorkWindow` reserves the 75s pre-capture guard,
100s post-capture commit period, and the caller's honest expected runtime.
`Optimize-Q15Storage.ps1` and `Backup-Q15LocalData.ps1` now fail closed when a
healthy local collector is running and bounded work would overlap that interval;
their safe default task times are 02:50 and 03:20.  Operators/automations can run
`scripts/local/Test-Q15CaptureGuard.ps1 -ExpectedWorkSeconds N -RequireSafe`;
it prints JSON and exits 75 when work must defer.  Simulated boundary/static
integration tests pass 5/5, and both live storage scripts were verified to
refuse without beginning work in a protected predicted-runtime interval.

The next 15:15 ET close also captured cleanly under the new work discipline.
V21 readiness is now **2/180 complete windows**, 14 feature rows, 11 row-level
executable rows, zero feature failures, and evidence SHA
`293dd456da460c9ce01b7e2485343af89c6cb27044ada302827ec31908922110`.
Executable counts are BTC/DOGE/ETH/SOL/XRP 2 each, HYPE 1, BNB 0; missing depth
remains honest nonexecutability, never a synthetic fill.  The reservoir's latest
two windows are consecutively usable with no latest-window failures.  Exact and
delayed miss counters stayed at their older pre-V21 values (7/21), with zero new
misses, write failures, queue, or spool backlog and all seven feeds fresh.

An outcome-blind volume audit explains BNB's 0/2 row-level executability: both
fresh official 12M books genuinely showed only six contracts at the RTI-selected
best ask.  This was not missing/stale evidence.  However, the WS and REST parsers
had the full displayed ladder in memory while the exact collector discarded it,
so the audit could not tell whether ten real contracts existed within the 2c
slippage already charged by V21.  The collector now computes and persists a
compact record-only selected-side ladder summary: displayed depth, filled size,
full-fill flag, and—only for genuine full fills—VWAP, worst price, and slippage
within best ask +2c.  Partial fills leave all price claims null.  Existing
`sim_full_fill_supported`, V18-V21, decisions, alerts, and trading are unchanged.

This evidence has its own pre-evidence frozen reservoir protocol,
`q15-rti-execution-ladder-reservoir-v1`, SHA
`2cb30fd0362a761b24ade0be1034209af25e04004e298aa8682575b1079cf0a4`,
boundary close `1785612600` and first eligible close `1785613500` (15:45 ET).
It is strictly record-only, outcome-blind, non-backfillable, and unavailable to
V21.  Its readiness command is
`tools/q15_rti_execution_ladder_reservoir_readiness.py`; predeployment readiness
is correctly 0 windows/0 rows with no outcomes.  Ten protocol/readiness/parser
tests and the combined collector/V21 source suite pass; latest focused total is
87/87 for the affected collector/source files.

The 15:30 ET V21 close captured cleanly before deployment: readiness is now
**3/180**, 21 feature rows, 16 actual row-level executable rows, zero feature
failures, and evidence SHA
`ae686c4802e1a55abf3132c83c70bbd0baf879d8171b5b8d21b57a3fbaf9ea8f`.
The service was then restarted safely outside the guard to load the record-only
ladder collector.  Post-restart direct health is OK: exact and delayed threads
alive, per-process misses zero, queue/spool zero, all seven settlement feeds
fresh, and Coinbase L2 connected.  Trading kill/dry-run defaults remained set.
The broad local health script's V3 scoreboard request timed out during warm-up,
but core `/api/health` succeeded; this was display-route latency, not a collector
failure.  The ladder reservoir remains 0 before its 15:45 first eligible close.

Follow-up adversarial coverage now proves the fresh official REST fallback
retains a six-contract best ask plus enough genuine next-level quantity to form
a ten-contract 60.4c VWAP/61c worst fill, while partial ladders never receive a
fill price.  A V21 invariance test injects extreme ladder values and proves its
76 features, source-evidence hash, and executability decision remain byte-for-
byte unchanged.  Combined capture-guard/collector/V21/ladder tests pass **94/94**.

The first ladder-eligible 15:45 ET window is **excluded**, not counted.  After
the deployment restart, an interval-research SQLite replay ran for 143.56s
inside the live refresh loop.  That stalled discovery and prevented the next
contract from being registered before its 13-minute capture timestamp; V21
therefore correctly remains 3/180 and ladder readiness remains 0.  The replay
is now isolated in a single background worker.  Each enqueue receives deep-
copied point-in-time analyses, canonicals, and source snapshots; the live loop
never waits for it and skips rather than queues overlapping replay work.
Focused interval-research and V95 timing tests pass **42/42** and **10/10**.
After the repair and safe restart, direct health is OK, the heartbeat and exact
threads are alive, all seven settlement feeds are fresh, and ordinary cycles
returned to about 2 seconds.  The 16:00 ET close is the first possible clean
post-repair V21/ladder window and must be validated outcome-blind before credit.

The 16:00 ET close validated the nonblocking repair: all seven exact parents
and all 21 delayed stages captured with fresh official evidence, zero new
misses/write failures, empty WAL spool, and V21 advanced cleanly to **4/180**
(28 feature rows, 20 row-level executable, zero feature failures), evidence SHA
`052dbcb446d8e07391b766cb9180d9d2ce87e007b0a3b7c4d5c75ca635de1bce`.
The ladder reservoir did not receive credit: geometry was 1 window but all
seven rows failed `LADDER_SCHEMA_INCOMPLETE`.  Outcome-blind tracing found the
sampler did capture the ladder summary, but the delayed-policy evidence-key
allowlist omitted it before ledger serialization.  The seven record-only keys
are now explicitly included; no frozen decision consumes them, the incomplete
16:00 ladder rows remain unmodified/excluded, and no backfill is allowed.  The
service was restarted safely at 15:51 ET for the 16:15 window.  The combined
collector, ladder, V21, and asynchronous-replay regression passes **120/120**.

The 16:15 ET validation window then passed end to end.  V21 is now **5/180**,
35 feature rows, 26 row-level executable rows, zero feature failures, and
evidence SHA
`4e7043de277470f55bb89725ecd82bf125c7cd93eeb434fd37923d84916227f8`.
The ladder report now has two geometry windows but exactly **1 usable complete
window**: the older seven incomplete rows remain excluded, while all seven new
rows carry valid ladder evidence and genuine 10-contract support within 2c.
HYPE is the first honest ladder recovery: top-of-book depth was below ten, but
the displayed next levels supported the full size.  The source reservoir's
latest five windows are consecutively feature-complete; its aggregate failure
status intentionally retains the single older seven-row integrity failure.
Live health after commit: exact and delayed threads alive, 7 parent and 21
delayed records, zero misses/write failures/retries, WAL spool empty, ordinary
cycle 2.6s, and all seven settlement feeds fresh.  No outcomes, labels, fits,
scores, alerts, paper artifacts, promotion, or real trading were opened.

Exact registration is now identity-auditable and immutable within a process.
Previously, the app's per-cycle duplicate registration rebuilt the same ticker
and silently replaced its first-seen timestamp; a contradictory same-ticker
strike/close could also overwrite the capture identity.  Duplicate identical
registrations now preserve the original object and lead time.  Same-ticker
strike or close contradictions fail closed, increment an in-process
health counter, retain the registered identity, and expose both observed values.
`/api/health -> rti_exact_13m.registration_by_asset` now reports ticker, close,
decision time, strike, first registration time, lead seconds, and whether it
preceded the decision.  Focused exact-sampler tests pass **23/23**.  The safe
16:07 ET deploy recovered the already-completed 16:15 parents as expected, so
those restart-time registrations honestly show negative lead but create no new
misses or rows; the next rollover must show positive first-seen lead on all
seven assets before its window is credited.

The 16:30 ET source window captured all 7 parents and 21 delayed stages with
positive immutable registration lead (96.323s for BNB/DOGE/HYPE and 118.851s
for BTC/ETH/SOL/XRP), zero identity conflicts, and zero misses/write failures.
It does **not** receive V21 feature credit: BNB's +30s OKX book was genuinely
3.155s old against the frozen 2s source gate, although its parent and +60s
snapshots were fresh.  The row remains excluded without threshold loosening or
backfill, so V21 honestly stays 5/180; the latest source failure is visible.
The separate ladder evidence was valid and advanced to **2 usable windows**;
all seven rows again supported genuine ten-contract fills, with BNB and DOGE
newly recovered from displayed next levels.  No outcomes were inspected.

Normal `/api/health` reads use a cheap live overlay outside the guard but serve
only the older full cache inside it; this explained an apparent registration
reversion during the protected interval.  Exact health now stamps
`health_generated_at`, while `health_cache.live_overlay_updated_at` and
`protected` disclose whether the exact mapping is live or cached.  The 16:23 ET
safe deploy is healthy: current exact snapshot age is directly computable,
conflicts/misses/failures are zero, ordinary cycles are about 2s, and all seven
settlement feeds are fresh.  Focused exact tests remain **23/23**.

The final 13:45 ET pre-evidence adversarial audit found and corrected three
statistical weaknesses while readiness was still exactly 0 rows and no labels,
fit, or scores had been opened.  Calibration now fits all feature-complete
calibration rows (150 non-BTC and 25 BTC), rather than only executable rows
with a BTC minimum of eight.  The disjoint policy partition selects exactly
one of identity calibration or the frozen regularized Platt mapping; policy
labels never refit either, and the selected mapping must beat the 12M Kalshi
market on both log loss and Brier.  This avoids both in-sample calibration
credit and forced degradation when the base model is already better calibrated.
The untouched test now additionally requires V21 to beat the held-fixed
52-feature V20 ablation on both proper scores, requires a positive close-
cluster-bootstrap 20th-percentile mean P/L, and compares maximum drawdown per
pick instead of raw totals across unequal trade counts.  Modeling/audit state
versions are v2; the final evaluator SHA is
`cf2f8a7daecfe83e5d38afbf63dadf9e44c686ac645539578abe85bc8ffd5de1`.

The final feature-lineage audit, also completed with exactly zero eligible
V21 rows, found that the inherited delayed-side flag did not independently
encode the observed +60s confirmation side.  V20 remains frozen.  V21 feature
builder v2 now adds a separate observed delayed-confirmation-side feature,
requires parent/original/record-side lineage to match, verifies each observed
confirmation side against its signed strike distance, and rejects missing,
nonpositive, or reused source IDs.  Legitimate reversals remain eligible and
are encoded as reversals rather than being filtered away.

The same zero-row audit replaced a weak V20 ablation comparison.  The
52-feature V20 map no longer inherits V21's selected family/hyperparameters;
it independently runs the identical train-only walk-forward candidate grid,
freezes its own winning specification, fits/calibrates only on its allowed
partitions, and carries that exact model into the untouched test.  V21 must
therefore beat a fairly optimized older feature map, not a potentially
handicapped comparator.  Modeling identity is now v3.

## Current 2026-08-01 - V21 trajectory challenger frozen prospectively

V21 is frozen before any V21-eligible evidence, outcome, label, or fit as
`q15-rti-v21-intraminute-trajectory-prospective-v1`, protocol SHA
`11b7e4c39280d793ae118c5237bf34eaadf83d1c281daf9171d5720b75d32454`.
Its prospective boundary is the 14:45 ET close (`1785609900`) and its first
eligible close is 15:00 ET (`1785610800`).  It preserves all 52 V20 inputs and
adds 24 fixed point-in-time trajectory features from the genuine +30s and +60s
quotes, RTI, Kalshi, and spot checkpoints, for 76 inputs total and feature-name
SHA `7e49760012a82d82bef6b6442d7c556bba822b702a983e10be184c8cc775dfd8`.
The new fields explicitly measure first-leg versus second-leg continuation,
distance, price/ask curvature, microprice change, spot momentum, trade
imbalance, book pressure, and spot-flow change.  The existing collector already
captures these sources; no runtime or frozen-control behavior was changed.

V21 corrects two audit-design weaknesses without changing V20.  Feature credit
requires a complete exact-parent/+30s/+60s triplet for all seven assets in the
same close, but it does not pretend every asset was executable.  The model may
learn survival from every feature-complete row; trade and P/L scoring is
strictly row-level and requires the actual 12M displayed book to support all
ten contracts.  Unknown or partial fills never count.  Its evaluator is frozen
at SHA `cf2f8a7daecfe83e5d38afbf63dadf9e44c686ac645539578abe85bc8ffd5de1`
and uses 180 exclusive close windows split 105 train / 25 probability
calibration / 25 disjoint execution-policy selection / 25 one-shot untouched
test.  Calibration can no longer double as margin selection.  A pre-evidence
implementation review, completed while V21 still had zero eligible rows,
explicitly froze probability clipping, quantile behavior, solver tolerances,
intercepts, and histogram early-stopping/bin settings that had initially been
implicit.  It also minimizes future pretest label access to all TRAIN rows plus
only row-level executable CALIBRATION and POLICY rows; nonexecutable rows in
those two partitions and every untouched-test row remain unread.

The same zero-row pre-evidence review froze the exact untouched-test gates:
positive official-fee/2c-slippage P/L, Wilson 95% lower accuracy above average
break-even, all-row log loss and Brier both better than the 12M Kalshi market,
lower maximum drawdown than the row-level executable side-follow control, and
the frozen cohort/side volume minima.  It also defines a report-only 52-feature
V20 ablation, matched V18 accuracy-only reporting (V18 did not require ten-
contract depth), matched V19 executable P/L, rejected-trade counterfactuals,
and all required subgroups.  Fresh official Kalshi series metadata must confirm
quadratic fee type and multiplier one for all seven series before either label
reservation; a changed or unavailable fee identity fails closed without
burning the one-shot audit.

The offline evaluator is now implemented in
`tools/q15_rti_v21_modeling.py`.  It has no SQLite, network, Telegram, paper
ledger, promotion, or order capability.  It performs same-close cluster-
weighted walk-forward selection, fold-local robust scaling, disjoint Platt
calibration, disjoint execution-margin selection, official fees, actual 12M
ask plus 2c slippage, and 5,000 close-cluster bootstrap resamples.  A complete
synthetic pretest passes both cohorts and confirms the untouched test remains
sealed, but synthetic data is only implementation validation and is not
evidence that V21 predicts live markets better.

The future manual audit commands are now implemented but have not been run:
`tools/q15_rti_v21_pretest_command.py` requires
`OPEN_V21_TRAIN_CAL_POLICY_LABELS_ONCE`; only a passing result can unlock
`tools/q15_rti_v21_untouched_test_command.py`, which requires
`SCORE_V21_UNTOUCHED_TEST_ONCE`.  Both write an exclusive reservation before
their settlement callback, bind exact feature/contract/evidence hashes, verify
fresh finalized Kalshi settlements, and permanently refuse a second label read
after an ambiguous interruption.  The test command can only use the validated
pretest model bundle and cannot refit, recalibrate, retune a margin, notify,
promote, or trade.

Outcome-blind observability found eight complete historical +30s and +60s
source sets after the reservoir boundary.  Those diagnostic rows predate the
final V21 feature-builder-v2 boundary and can never be backfilled or credited.
V21 readiness is correctly **0/180** before its future boundary.  The manual-only
seal preview reports 0/180, creates no directory or artifact, and requires
`CREATE_V21_EXCLUSIVE_FEATURE_SEAL_ONCE` only after readiness.  The daily
automation now reports V21 feature windows and row-level executable rows but is
explicitly forbidden to provide the confirmation, open outcomes, fit, score,
notify, tune, promote, or trade.
Sixty-five focused V21 adversarial tests pass; the targeted collector/V18-V21/audit
regression passes **161/161**.  Live health remains OK with zero exact or
delayed misses/write failures, an empty spool, connected fresh L2, and the
native spot sampler alive.

## Current 2026-08-01 - V20 auditable-readiness overcount corrected

An outcome-blind adversarial check found that V20 readiness credited a row when
all 52 features were present even if its new 12M displayed book did not support
the protocol's full ten-contract simulated fill.  The final exclusive seal
already rejected that row, so the readiness headline could overstate what was
actually sealable.  `rti_microstructure_v20.evaluate_pair` now enforces the
frozen full-fill requirement at feature-credit time, and the readiness status
uses the same earliest-150-complete-window rule as the seal.  Earlier excluded
windows remain visible diagnostics but no longer poison readiness forever after
150 later valid windows exist.

The honest current V20 count is therefore **0/150 sealable complete windows**,
not 2/150.  Across the first three attempted seven-asset windows, six rows
lacked full-fill support and each close was correctly excluded.  This is a
real execution-evidence failure, not a model result: no outcomes, settlement
status, labels, fit, score, threshold selection, artifact, notification,
promotion, or trade were opened.  The exact/delayed collector itself remains
healthy with zero missed deadlines or record failures.

## Current 2026-08-01 - V20 exclusive seal and one-shot audit stack ready

V20 now has a manual-only, outcome-blind exclusive earliest-150 feature seal in
`tools/q15_rti_v20_feature_seal.py`.  It freezes exactly 150 complete
seven-asset close clusters into 90 train, 30 disjoint calibration, and 30
one-shot untouched-test windows; binds exact contract identity, parent/delayed
lineage, the fixed 52-feature vector and hashes, fresh execution evidence,
matched V18/V19 benchmark identities, and all no-fit/no-score/no-notify/no-trade
safety flags; and fails closed on label fields, feature mutation, duplicate
identities, cross-close or cross-partition rows, chronology changes, invalid
full-fill evidence, or an attempted competing seal.  This is seal schema v2;
schema v1 was superseded before any eligible V20 window existed.  The only
write confirmation is
`CREATE_V20_EXCLUSIVE_FEATURE_SEAL_ONCE`.  It must not be supplied by an
automation; creation remains a manual action only after all 150 windows exist.

The offline evaluator is independently frozen at contract SHA
`dc5c2eabb14d498b1a70fef59718e0c44437b03a4276c211283d795b3383c2b6`.
It fixes scikit-learn 1.9.0, 28 non-BTC and four BTC candidates, four exact
same-close walk-forward folds, fold-local median/IQR preprocessing, deterministic
tie-breaking, train-only refit, calibration-only Platt scaling, official Kalshi
fees plus 2-cent slippage at the actual 12M fill, four edge margins, close-cluster
bootstrap, volume/side gates, final test gates, and report-only distance,
volatility, regime, reversal-risk, and settlement-average-risk tiers.  The
dependency is explicit in `requirements.txt` and installed in the local venv.

`tools/q15_rti_v20_pretest_runner.py` reserves exactly the 840 TRAIN/CALIBRATION
rows before any callback, converts authoritative YES settlement into original-
side survival labels, runs the frozen model grid, and writes a hash-bound model
bundle only if both cohorts pass.  A crash after reservation is permanently
ambiguous and cannot reread labels.  `tools/q15_rti_v20_untouched_test_runner.py`
then reserves exactly the remaining 210 rows, validates the passing pretest and
model SHA, and scores once with no refit, recalibration, model choice, or margin
choice.  It reports market/all-source/V18/V19 benchmarks, fee-adjusted P/L,
Wilson interval, EV, drawdown, cluster intervals, subgroups, and rejected-trade
counterfactuals.  Passing is manual paper consideration only; neither runner
can notify, promote, or trade.

The manual commands use the existing fresh official Kalshi API verifier and
fail closed on unavailable, non-final, wrong-ticker, wrong-close, mismatched, or
tampered settlement evidence.  Their exact confirmations are
`OPEN_V20_TRAIN_CAL_LABELS_ONCE` and `SCORE_V20_UNTOUCHED_TEST_ONCE`.  The daily
automation is explicitly forbidden from supplying these or the feature-seal
confirmation.

Focused V20/authoritative-evidence/fee coverage passes **94/94**; the complete
32-model synthetic sealed audit also passes end to end.  The earlier
feature-present count was **2/150 V20 windows / 14 rows**, but the corrected
sealable count is **0/150** because each attempted close has at least one row
without full ten-contract displayed support.  V18 is **28/150 windows / 3 picks**,
V19 **14/150 windows / 0 picks**, and the reservoir **6 windows / 42 rows**.
The collector is healthy with zero exact/delayed misses or write failures, an
empty spool, connected WebSockets, and fresh L2.  No live outcome column, label,
resolution status, fit, score, calibration, margin selection, notification,
model/paper artifact, promotion, or trade was opened or created.

The legacy V15 source population now reports the exact
`successor_audit_complete_close_windows` count as **714**, but this does not
authorize another audit.  Its preserved fourth-disjoint one-shot result is
terminal `WALK_FORWARD_GATE_FAILED_UNTOUCHED_TEST_REMAINS_SEALED`: aggregate
walk-forward accuracy was 70.83% with Wilson 95% lower bound 62.95%, the gate
failed, and untouched-test labels remain unopened.  Never rerun V15 because the
counter grows.  The stale `continue-v15-rti-audit-at-60` automation is already
absent from the app.

## Current 2026-08-01 - exact capture/persistence isolation

The delayed-feature reservoir is now frozen at protocol SHA
`2e31993e2219f4dc6db734f3dabfab5e9540ec06dcf0c77b27982065b80b816e`,
prospective boundary `1785594600`, first eligible close `1785595500` (10:45 ET).
No reservoir outcome or prospective resolution status was accessed.  All
earlier rows remain preserved but are excluded and may never be backfilled.

The 09:45-close live proof froze all seven exact 13M rows and all seven +30s
quotes on time, but it correctly received zero reservoir credit.  Serial calls
through the shared 1.5 GB strategy ledger then occupied the exact worker for
about 27 seconds: the +30s rows' source timestamps remained immutable, but the
+60s stage missed all seven quote deadlines.  The same contention produced
3.4-20.1s gaps in the one-second spot path.  Feature-only inspection found this
before any outcome access; no label, resolution status, fit, or threshold was
opened.

Live delayed evidence capture is now separated from persistence.  Each source
is built exactly once from its genuine quote/path/spot timestamps and handed
to a single isolated daemon writer; the exact scheduler continues servicing
later stages while idempotent strategy-ledger writes wait.  Injected-time tests
retain synchronous behavior.  The one-second spot sampler also moved from the
busy WebSocket asyncio loop to a dedicated native thread, while event-driven
samples and the frozen legacy path remain unchanged.  Health exposes both
threads, queue depth/inflight writes, sampler age, late iterations, and maximum
interval.  Focused exact/spot/feed/interval/reservoir coverage passes **89/89**
and `git diff --check` has no errors.  The local service restarted safely with
all execution kill switches and dry-run switches true; startup health is OK,
both new threads are alive, the write queue is empty, and the native sampler's
observed maximum interval is 1.042s.  Await the 10:00-close live proof.

That 10:00-close proof confirmed the persistence isolation itself: all seven
13M rows and all 21 delayed stages captured and persisted with zero misses,
while +30s writes were visibly queued and drained without blocking +60s.  The
feature-only reservoir audit nevertheless rejected the window.  Five Coinbase
assets carried exchange-source ages around 37 seconds despite near-zero local
handler ages, proving the single Coinbase consumer was processing queued old
batch-book messages; DOGE/XRP/HYPE also had incomplete fast paths.  BNB alone
passed every feature-quality gate.  The whole close remains excluded.

The backlog was traced to an O(full book) `max(bids)`/`min(asks)` rescan on
every Coinbase 50ms batch.  Books now maintain best prices incrementally and
recompute only when the current best is removed; top-N snapshot extraction uses
a bounded heap instead of sorting the full book.  A synthetic 40,000-level
benchmark processes 1,000 updates in 0.0023s and captures a top-five snapshot
in 0.0029s.  Health now reports local and source book ages separately plus each
asset's recent fast-path count/gap/age.  Focused coverage passes **49/49**.  The
service restarted safely; immediate live source ages were -0.734 to -0.655s
(source clock slightly ahead, within the frozen +/-5s gate), per-asset recent
path gaps were 0.879-1.595s, and both isolated threads were healthy.  Await the
10:15-close proof; never credit the superseded 10:00 close.

Before that eligible window, the remaining REST timing margin was hardened
prospectively too.  The bounded eight-worker exact quote pool now persists
across stages, and each worker owns a read-only HTTP session so TLS connections
are reused without sharing mutable session state across threads.  Health
reports the active pool/connection-reuse contract; focused coverage is now
**50/50**.  This changed no quote timestamp, evidence source, threshold, or
control, and the 10:15 eligibility boundary did not need to move because no row
after the 10:00 exclusion boundary had yet been captured.

The 10:15-close proof then exposed one final same-process interaction: exact
and delayed quote timing remained perfect, but asynchronous strategy-ledger
writes still starved both spot WebSocket providers for up to 46 seconds.  All
14 +30/+60 spot contexts failed closed; the window remains excluded and no
outcome was accessed.  Delayed sources now enter a separate 250ms/WAL,
hash-verified, idempotent SQLite spool immediately after capture.  The heavy
strategy ledger cannot read them until decision+95s, five seconds after +90s.
The spool survives restarts, detects mutation, and prevents recovery from
recapturing an interval already frozen there.  Its 21-source synthetic enqueue
takes 0.0047s, focused coverage passes **54/54**, and live health exposes queue,
release, retry, hash/integrity, WAL and outcome-free state.  After restart the
spool is empty/healthy, both worker threads and the quote pool are alive, source
ages are within 1.24s, and recent fast-path gaps are 1.17-1.75s.  Await the
10:30-close proof; never credit the superseded 10:15 close.

The durable-spool mechanism itself passed the 10:30-close live exercise: all
21 delayed sources entered the outcome-free spool, no heavy write ran before
+95s, and all 21 later drained with zero retry or integrity error.  However,
the high-frequency verification loop repeatedly requested the expensive full
health graph after 13M; those requests starved the source feeds before +30s and
made all seven delayed spot contexts fail closed.  The close is excluded.

`/api/health` now serves its last immutable cached snapshot from 75 seconds
before 13M through 100 seconds afterward, covering the complete path, all three
delayed stages, and spool release.  The launcher restart guard uses the same
+100s post-capture interval.  The combined health/restart/exact/spool/source
suite passes **110/110**.  The service restarted safely; validate the
10:45-close window without high-frequency full-health requests and inspect its
immutable rows only after the spool drains.

The 10:45-close proof **passed**.  Direct outcome-free monitoring observed 7
exact parent rows, then spool depth 7 at +30s, 14 at +60s, and 21 at +90s with
zero delayed ledger rows before release.  At decision+95s the spool drained all
21 rows idempotently with zero retry/error.  The 12M reservoir audit found all
seven assets complete with no identity or feature-quality failure: exact timing
offsets 0.259-1.329s, evaluation delays 1.346-1.352s, complete 61-sample RTI
paths, official WebSocket histories, Kalshi transport ages 0.537-1.526s,
nonnegative local spot book ages 0.013-1.123s, source-clock ages -1.129 to
+0.826s, and complete fast 60s paths with 61-66 samples / 1.434-1.621s maximum
gaps.  Outcomes remain unopened.  Honest readiness is now reservoir **1 usable
window / 7 rows**, V18 **23/150 windows, 3/30 picks**, and V19 **9/150 matched
windows, 0/30 picks**.  Continue prospective collection; do not alter this
successful boundary or backfill any excluded close.

The next prospective 11:00-close window independently **passed** the same
outcome-blind pipeline.  A primary-key-bounded monitor observed all seven exact
parents, spool depth 7 -> 14 -> 21 at +30s/+60s/+90s with zero attempts, and an
empty spool after all 21 delayed sources were released idempotently.  The first
parent read briefly exposed six durable rows while the seventh commit was still
finishing; by +30s all seven were present, and the seventh row retained its
genuine +3.269s source-created timestamp.  The frozen reservoir audit now has
**2/2 usable close windows and 14/14 rows**, with no identity, schema, or
feature-quality failure.  V18 outcome-blind readiness is **24/150 complete
windows, 3/30 picks**; V19 is **10/150 matched windows, 0/30 picks**.  No
outcome column, label, resolution status, fit, score, or threshold was opened.

Routine health monitoring is now nonblocking outside the capture guard too.
A bounded production profile confirmed that the complete graph spends most of
its wall time in health-only large-ledger/status reconstruction; one explicit
post-restart `?full=1` request took 14.103s.  Normal live `/api/health` requests
now reuse that age-disclosed diagnostic snapshot while refreshing only bounded
in-memory market state, Kalshi WebSocket status, exact RTI state, settlement
index state, spot state, and watchdog liveness.  The same production request
then completed in **0.054s** with `LIVE_NONBLOCKING_HEALTH`, exact misses 0,
settlement coverage 1.0, the spot sampler alive with zero late iterations, and
an empty/error-free spool.  Full rebuilds remain manual and are always refused
inside the exact guard.  This changes no evidence timestamp, feature, rule,
threshold, label policy, or frozen boundary.

The first post-health-change 11:15-close window also **passed**, making three
consecutive valid unseen reservoir closes.  All seven exact parents were
durable by +5s with source-created offsets +0.240s to +1.798s; the spool again
progressed 7 -> 14 -> 21 with zero attempts and drained all 21 rows after
release.  Frozen readiness is now reservoir **3/3 usable windows, 21/21 rows**,
V18 **25/150 windows, 3/30 picks**, and V19 **11/150 matched windows, 0/30
picks**.  Outcomes remain unopened.

`tools/q15_rti_delayed_feature_reservoir_geometry.py` now performs a reusable
SQLite-authorizer-protected, outcome-blind observability audit over complete
seven-asset close clusters.  At three windows it reports
`FEATURE_OBSERVABILITY_OK`: 207 numeric required fields observed, 199 varying
globally and within assets through time, and eight constants that are all
intentional path-count/simulation/retention/cadence settings.  There are zero
unexpected dead numeric signals.  It cannot select labels, fit, score, notify,
promote, or trade; focused coverage passes 9/9.  The daily continuation now
requires this geometry check after reservoir readiness.

V20 is now preregistered **before any credited V20 evidence or outcome** as
`q15-rti-v20-delayed-reversal-hazard-prospective-v1`, protocol SHA
`cdd860d63be6c2165d7e91431d576d57645c3218701df616853ac969a9db9450`.
Its conservative boundary is the 11:30 close (`1785598200`) and first eligible
close is 11:45 (`1785599100`), so the three observability windows and the 11:30
window receive no V20 credit.  The fixed 52-feature map predicts survival of
the original 13M RTI side using side-normalized parent path geometry, genuinely
new 12M RTI/quote evidence, Kalshi book/trade dynamics, spot path/flow, and
execution liquidity.  Ages, timestamps, retention settings, and storage delay
remain quality gates and cannot become model inputs.  All 21 existing
outcome-blind pairs build the map successfully, but none can receive V20 credit.

V20 requires the exclusive earliest 150 complete seven-asset windows: 90 train,
30 non-overlapping calibration, and 30 one-shot untouched test.  Internal
expanding folds never include calibration; test rows can never drive refitting,
calibration, feature selection, or margin selection.  NON_BTC_TRANSFER compares
fixed elastic-net logistic and shallow histogram boosting grids; BTC uses its
own strongly regularized ridge-logistic grid.  Selection is by internal
walk-forward log loss, then Brier score and lower complexity.  The fixed
fee-aware edge grid is selected on calibration only with close-cluster bootstrap
and volume/side minimums; otherwise the candidate abstains.  Historical passage
can create only a manual paper challenger, followed by 30/60/150 resolved-pick
reviews.  Runtime readiness is currently **0/150**, outcome-blind as expected.

## Superseded 2026-08-01 - spot receipt-time and independent fast-path hardening

Live proof exposed two additional timestamp/cadence defects before outcome
access.  Spot book/trade freshness and trade-window membership used exchange
timestamps, which produced negative ages when source clocks led the local PC.
Freshness now uses local receipt time; source timestamps and source-clock ages
are retained separately as provenance.  The 09:00 ET close then passed this new
lineage but correctly received zero reservoir credit because the frozen legacy
SQLite-cadence spot path had only 9 samples and continuity gaps during exact
processing.  That legacy path and every frozen control remain unchanged.  The
reservoir instead records a separate one-second, local-observation-time fast
spot-mid path, fed by both book events and an independent timer so unchanged
books are observed without depending on SQLite latency.

The 09:15 ET window then missed its 60-second RTI deadline by 5.93s and also
receives zero credit. Internal cycle telemetry—not guesswork—identified the
cause: optional interval research took 45.47s because nine rollback-journal
queries each waited up to 5s behind a writer. Its ledger now uses WAL and a
250ms fail-closed busy timeout, so optional research cannot hold the exact
scheduler through repeated lock waits.

The 09:30 ET window then exposed a second independent interval-research stall:
WAL removed the lock cascade, but replay-safe optional top-pick/Drift scoring
still ran synchronously for 18.33s at 13M and caused another fail-closed 60s
miss. Interval source rows still freeze at their real 13M timestamp; only that
optional scoring/replay work is now deferred through 11m20s, after all exact
30/60/90s captures. A regression proves the durable 13M source is unchanged and
the shadow replays it later. The protected-window suite passes **72/72**.

Targeted timestamp/path tests pass **47/47**, the interval contention suite
passes **71/71**, and the expanded exact/V19/feed/
strategy suite passes **271/271**.  The service was restarted with both
execution kill switches and dry-run switches true.  Await live 09:45 ET proof;
credit it only if all seven rows pass source provenance, local timestamp
lineage, Kalshi 5/15/30/60s histories, fast spot 15/60s paths, and exact delayed
RTI-path completeness.

The old V15 trigger is obsolete: the preserved fourth-disjoint V15 one-shot
result is terminal `WALK_FORWARD_GATE_FAILED_UNTOUCHED_TEST_REMAINS_SEALED`.
Its untouched-test labels were never read, and V15 must not be rerun merely
because the broader source counter now exceeds 60.

## Superseded 2026-08-01 - exact collector recovered; initial reservoir proof

At that time, the post-amendment 08:45 ET close appeared to be the first usable delayed-feature
reservoir window: **1 complete window / 7 rows**, zero identity failures, zero
feature-quality failures, and `outcome_labels_read=false`.  All seven rows have
the official WebSocket-history source, transport age 0.000-0.010s, complete
Kalshi 5/15/30/60s histories, complete spot 15/60s paths, complete 61-sample RTI
paths, and evaluation delay 0.132-0.134s.  Actual 60s Kalshi event counts ranged
from 420 to 8,596 and spot net-flow values were non-null on every asset.

The repeated null-flow failure was traced to two independent causes.  The
generic shadow challenger was retraining a 40,000-row pure-Python model twice at
every market rollover and starving the in-process feed threads, so local online
refits are now disabled with `Q15_CHALLENGER_REFIT_EVERY=0`; the shadow still
starts, predicts, records, grades and reports.  Also, an unchanged WebSocket
book correctly triggered a new REST execution snapshot, but that fallback
discarded the independently captured WebSocket event/trade history.  Execution
price and flow are now kept as separate, timestamped sources: REST may supply
the fresh fill quote while only official WebSocket history supplies flow.

That superseded repair changed prospective evidence identity, so the reservoir was
amended before any outcome access and moved forward rather than backfilled.
Its current protocol SHA is
`2eefe3e5b720dee3ff13fe5c8bc7386e1f8b1403c08dd44f32c1ecd0e4da062f`,
prospective boundary `1785587400`, first eligible close `1785588300`.  The two
defective windows remain preserved but are outside the amended population.
Focused source/provenance/V19/exact/live-loop coverage passes **307/307**.
After the live proof, the service is healthy with zero stale feeds, zero exact
or delayed misses/write failures, no pending delayed stages, and no post-restart
shadow-refit events.  Honest readiness is now **V18 15/150 windows, 2/30 picks**
and **V19 4/150 matched windows, 0/30 picks**; no outcomes were opened.

The live 08:15 ET close advanced the honest frozen counters to **V18 13/150
complete windows and 2/30 picks** and **V19 2/150 matched parent+fresh-60s
windows and 0/30 picks**.  The exact 13-minute stage captured all seven assets
with 7 quotes, 7 decisions, zero misses/write failures, and a maximum scheduler
offset of 0.186s.  V18 and V19 still report `outcome_labels_read=false`; no seal,
scoring, paper artifact, Telegram signal, promotion, or real trade was created.

The superseded initial outcome-blind delayed-feature reservoir freeze used SHA
`810a465661574beec7bdf10d9aeac6c6737c645937a5fc199cf41b2914ebf0a0`.
It starts at the 08:15 ET close, never backfills, and records Kalshi/spot flow
only for a future independently frozen model; V18/V19 decisions cannot read it.
The persisted spot set was narrowed from an accidental 298-field null-heavy
superset to 72 actually captured fields (207 total required reservoir keys).
The first seven rows had correct lineage/schema but their Kalshi flow was absent
after an official REST fallback and their spot books were 28s stale during a
live feed interruption.  The coverage audit originally exposed the keys but not
their null values; it now requires complete Kalshi 5/15/30/60s windows, complete
spot 15/60s paths, usable source/timestamp identities, and non-null core flow.
Consequently that window is preserved but receives **0 usable-feature credit**
instead of a false pass.  The required 60-second V19 record itself remained
valid; the separate optional 90-second diagnostic missed seven deadlines during
the same interruption and is not hidden.  Focused reservoir/V19/exact/live-loop
coverage now passes **275/275**.

Outcome-blind readiness is the only allowed continuation.  The daily
`continue-v18-v19-rti-prospective-audit` heartbeat must report V18's
`successor_audit_complete_close_windows`, V19's matched complete-window count,
and the reservoir's `usable_feature_complete_close_windows`; it must never infer
reservoir usability from schema presence alone or open any outcomes.

## Earlier 2026-08-01 - exact collector recovered; V19 source identity hardened

The exact collector stall was traced with `py-spy` to completed legacy V11,
V13, V14, and independent-path readiness scans repeatedly materializing the
wide historical strategy table in the live process.  Their safe loaders now
query only post-freeze rows, and the terminal monitors default OFF while
remaining explicitly opt-in.  A second startup stall was traced to drift replay
scanning the 161,959-row interval-research table without an index matching its
window lookup.  `idx_ir_model_interval_window_scored` now covers that query;
live cycles immediately advanced from 1 to 18 and every stale feed cleared.
The runtime manifest is valid and both execution books remain dry-run plus
kill-switched.  Post-repair exact windows continue to capture 7/7 assets.  The
07:30 ET close recorded all seven exact rows and fourteen delayed-stage rows
with no misses, record failures, retries, stale feeds, or collector error.

Two additional live-loop stalls were removed without changing any RTI rule.
The Coinbase Advanced L2 health path no longer rescans Downloads after an
explicit legacy key path is unavailable; an active collector reuses its loaded
key and connection state.  Drift delivery reconciliation now uses the partial
SQLite index `idx_strategy_bot_drift_retry` instead of scanning 243,907
strategy rows.  The exact live query uses the new index and completes in
1.529ms; `/api/health` fell from 6.177s to 0.186s.  Per-overlay cycle timing is
also persisted and reported so any future regression can be attributed.

Outcome-blind V18 readiness is now **12/150 complete close windows** and **2/30
picks**.  V19 is now honestly **1/150 complete matched parent+fresh-60s
windows** and **0/30 picks**; no V18/V19 outcomes were read.  Its first delayed window
was 15.7s late during the legacy-monitor stall.  The next window met scheduler
timing but six inactive WebSocket books had no event newer than the freshness
gate.  A new official Kalshi REST orderbook snapshot fallback was added without
changing any frozen V19 threshold.  Its first sequential live attempt exposed
cross-asset starvation, so fallback requests are now concurrent, independently
timestamped, and bounded to a 0.35s connect/1.0s read timeout; slow or missing
responses still abstain.  Official snapshots are requested 0.75s before target,
but retain their genuine receipt timestamps and pre-target responses are never
credited.  The live all-seven proof had maximum quote timing offset 0.621s,
maximum path/evaluation age 1.638s, and quote age 0.0s.  It produced no V19 pick
because no parent qualified, which is a valid abstention.  Earlier failed
windows retain no credit.  A later outcome-blind adversarial review found that
the strategy ledger retained quote age but omitted the official quote-evidence
source.  The two previously credited V19 windows were therefore revoked rather
than grandfathered.  V19 now requires the exact 12M record kind, an official
Kalshi WebSocket/REST source identity, internally aligned evaluation timestamps,
and explicit full-fill support for ten contracts.  The source-identity amendment
was frozen before any V19 label access; future rows persist the missing field.
The first post-amendment live window earned credit with all seven official
WebSocket source identities, timing offsets of 0.034s, evaluation delays of
0.217-0.220s, complete 61-second paths, and explicit ten-contract fill evidence.

V19's first-review contract is hash-bound at
`ecd153b13aad4bd6b322c00f401beef606da7ca6b0cc12cde2fe27dd7d689030`.
Its outcome-blind earliest-prefix seal, distinct 12M/13M cost evaluator,
append-only reservation runner, and manual authoritative-Kalshi command are now
implemented.  They require 150 complete paired windows plus 30 picks, forbid BTC,
fail closed on lineage/timestamp/hash/evidence mismatches, and cannot notify,
promote, or trade.  The latest source/seal/review/collector/live-loop suite passes
124/124; the earlier broader suite passes 242/242; and
`git diff --check` has no errors.

## Shipped 2026-08-01 - V19 fresh-60s study frozen without touching V18 outcomes

The existing 30s/60s delayed-confirmation stream was audited only through the
pre-V18 development boundary (`close_time <= 1785573900`) in
`reports/q15_rti_v19_exploration/pre-v18-delayed-confirmation-development-v1.json`
(file SHA `0d4dfe9d8a1799fbefcea936c377727c54a23de29afd99517617ff7a55e0df30`).
It uses matched parent lineage, the newly captured delayed ask, 2c slippage,
official Kalshi fees, and 10 contracts.  Fresh 30s confirmation was 24/56 and
-$97.36; fresh 60s was 26/44 and -$6.75.  The development-selected low-reversal
+ fresh-60s subset was 5/6 and +$14.85, but its Wilson lower bound was only
43.65%, so this is far too small to claim improvement or promote.

V19 is therefore frozen only as a second **silent prospective study** in
`config/q15_rti_v19_low_reversal_fresh_60s_protocol.json`, protocol SHA
`50239502295e9890588bd708e3b66b3ef72e570f4a6b8582628398d751c1c34a`.
It begins strictly after close `1785577500` (first eligible `1785578400`),
requires the unchanged V18 parent to be eligible, then requires a complete new
61-second official-RTI path, the same RTI side, a new quote captured at 12m
within 2s, quote/path age <=3s, ask <=62c, spread 0-1.5c, and depth >=10.  It
cannot reuse the 13m quote.  Readiness uses an SQLite outcome-column authorizer,
requires all-seven parent/delayed lineage-complete windows, and currently has
1/150 windows and 0/30 picks after the source-identity audit.  It cannot read labels, create paper alerts, notify,
promote, or trade.  Seven V19 selector/readiness tests pass; the broader
V18/V19/exact/safe-loader selection completed 96/96 and `git diff --check`
found no errors.

## Shipped 2026-08-01 - V17 rejected honestly; V18 selective study frozen

V16 and V17 are both terminally rejected.  V17's one-shot state is
`reports/q15_rti_v17_development_runs/non_btc_transfer/development-reservation.result.json`,
state SHA `97ee7c24ebf617d48b07b95499c54e6f212658aec462b0bb0825fe14bc39e22f`.
Fresh Kalshi verification covered exactly 1,440 finalized NON-BTC contracts
(evidence SHA `aef14f3c9383d1b7b558c459a9e9d7cd7f32e565759e7a899fe3eda58d343124`).
Across 120 walk-forward windows / 720 rows, the point-in-time market was
460/720 = 63.89% accurate (Wilson 60.32%-67.32%), Brier 0.216552 and log loss
0.620521.  V17, V16, V15 and V14 all selected residual trust 0.0 in every fold
and were exactly the market prior.  V17 failed 20 proper-score/effect/bootstrap
gates; future V17 calibration, test, paper artifacts and notifications are
permanently forbidden.  BTC labels were never read.

Two fully disclosed development-only explorations are preserved under
`reports/q15_rti_v18_exploration/`.  The corrected slate-context report is
`v17-development-slate-context-v2.json` (file SHA
`27de7a1ac61859bfbb070076a79a60abf8fc3208ce32b38f6aab4805786adf05`);
all six residual candidates worsened raw Brier/log loss and selected zero trust.
Its v1 artifact is preserved and explicitly superseded because it incorrectly
named a zero-trust tie as a best candidate.  The cost-aware selective report is
`v17-development-selective-value-v1.json` (file SHA
`1064d37549c8843113203346ad34c303beca3b15cf1044dada178a441bff99ed`).
Frozen strict control was 55/90 = 61.11% and -$5.04 after official fees and 2c
slippage at 10 contracts.  The pre-existing low-reversal-risk subset was 17/23
= 73.91% and +$26.46, but its Wilson lower bound 53.53% was below its 62.41%
average break-even; no exploratory rule passed the robustness screen.

The only advancing work is therefore a **silent prospective study**, not a
claimed improvement.  V18 is frozen in
`config/q15_rti_v18_selective_low_reversal_protocol.json`, protocol SHA
`1e7a8c6c7529950e848b5b0eb9b247c78627b7312f8b2881a3e93743c4e3f2f5`.
It preserves every strict exact-13M control gate and additionally requires the
already-existing `rti-point-in-time-risk-taxonomy-20260720-v1` reversal class
to be `low`; the RTI side is unchanged.  It starts strictly after close
`1785573900` (first eligible close `1785574800`).  First manual review requires
both 150 complete prospective windows and 30 resolved eligible picks.  Outcome
access, automatic grading, paper picks, Telegram, promotion and real trading
are locked until then.  Outcome-blind readiness is now 10/150 windows and 2/30
picks; no prospective label or resolution status has been read.

The first-review contract was frozen and then transparently amended before any
future outcomes in
`config/q15_rti_v18_first_review_contract.json`, SHA
`b070c89a035737a55ba37132250b308b5b6537fe888750ea9f04d4c4ea35c6d4`.
The outcome-blind integration review found that the first implementation both
omitted frozen strategy/risk fields from the safe loader (making every real
row abstain) and inherited V17 model-feature completeness (discarding exact
windows for unused fields such as cross-asset breadth).  V2 now binds each of
all seven rows to exact contract identity, a complete fresh 61-second RTI path,
fresh quote/path ages, exact timing, paper identity, and the frozen risk-policy
identity.  It does not relax any side, price, spread, depth, strict-control, or
low-reversal threshold.  A flat RTI path now records the finite capped limit of
distance/zero-volatility plus an explicit flag instead of manufacturing a
missing feature.  The clustered bootstrap was also corrected to resample whole
close windows while recomputing pick-weighted economics; evaluator/runner state
versions were bumped before label access.

`tools/q15_rti_v18_prospective_seal.py` will exclusively seal the shortest
earliest prefix satisfying both bars, without reading resolution status.
`tools/q15_rti_v18_first_review_command.py` is the only outcome-opening path:
it reserves first, reads exactly the sealed strict-control IDs, requires fresh
finalized Kalshi evidence, preserves candidate/control/rejected comparisons,
and becomes permanently ambiguous if interrupted.  A passing result only
permits manual consideration; it cannot create paper picks, notify, promote,
or trade.  The V2 seal does not yet exist because readiness is below both bars,
so no prospective outcome was opened.

The relevant V14-V18, exact sampler, and safe-loader suite passes 255/255.  The
V18 readiness/seal/command paths now query only rows strictly after the frozen
boundary through the same SQLite outcome-column authorizer: readiness fell
from roughly 13 seconds to 0.65 seconds without changing row or feature hashes.
Its additive outcome-blind health report shows 3/3 all-seven windows source
complete, no partial or source-failed windows, maximum exact offset 0.151s,
maximum evaluation delay 0.126s, maximum quote age 0.969s, one strict/candidate
pick (SOL), and explicit strict/reversal rejection counts.  A dry seal attempt
exited `2` as not ready and created no seal.
The real outcome-blind projection also reconstructed 21/21 exact contract-bound
rows across the three windows, with one full-fill strict/candidate pick and no
outcome column present.

The local service was restarted through the environment-aware safe launcher;
all
8/8 runtime-manifest flags match.  Its first post-restart capture completed 7/7
exact rows, 7/7 independent paths, and 7/7 cross-asset contexts with zero
missing paths, missed deadlines, retry exhaustion or record failures; timing
offset was 0.129 seconds and live data age 1.54 seconds.  The service remains
paper-only with both executor books at `DRY_RUN=true` and `KILL=true`.

## Shipped 2026-08-01 - V16 frozen; development sealed; future count 1/60

V15 remains rejected.  Its fourth disjoint result is immutable at state SHA
`c711c85b0299f1f4efebd9af9e40966c9396cb0776f7713579f10fbffb51f8ac`:
24 validation windows / 144 NON-BTC rows, 70.83% accuracy, Brier 0.190433,
log loss 0.565212, and residual trust 0.0 in every fold.  Its untouched test
was never opened.  Do not rerun, reopen, tune, deploy, or notify V15.

The label-blind V16 successor is frozen in
`config/q15_rti_v16_successor_protocol.json`, protocol SHA
`8684c5d74fea0ba6d296cd5033d109e5e36bd6b2dabb3eca3d991793d390b69a`.
It preserves V15's 25 base features and preregisters five asset indicators plus
15 bounded reversal/microstructure interactions.  The market-offset residual
model, regularization, trust grid, costs, chronology, proper-score gates, and
paper-only safety are fixed; BTC labels are forbidden.

`tools/q15_rti_v16_development_seal.py` created the exclusive development seal
at `reports/q15_rti_v16_development/non_btc_transfer-development-240-v1.json`,
seal SHA `6f496b8486cb52b60c127acf09f1df9da9bf5883eb5300a306cb3263247a43d6`.
It excludes every selected close window in all four V15 seals: 240 historical
selections collapse to 204 unique windows.  The seal commits to the earliest
240 remaining complete windows / 1,440 NON-BTC rows, 1,680 all-seven source
rows, zero contract-identity mismatches, a passing outcome-blind market-prior
audit, and four disjoint 120/30 expanding walk-forward validation folds.
Calibration is not reused as validation.  No outcome, fit, score, paper
artifact, Telegram notification, promotion, or trade occurred.

The future calibration boundary is close time `1785565800`.  At the seal run,
`successor_audit_complete_close_windows` was **1/60**; use the V16 tool's
prospective readiness counter, never the broader source-complete counter.  The
continuation automation now targets V16 and must not open future labels before
the exclusive one-shot development gate and later calibration reservation.

The local exact collector was restarted paper-only with both executor books at
`DRY_RUN=true` and `KILL=true`.  The 02:32 ET capture completed 7/7 exact rows
and 7/7 independent paths with zero misses or record failures; the broader V15
readiness count advanced to 685.  The path-forecast timestamp gate now derives
remaining time from canonical close, records actual capture offset and feature
age, and classifies all 31,893 older rows as legacy/unaligned.  Its first four
new aligned rows had 4.23-second capture offset and 2.91-second feature age.

`tools/learning_export.py` now excludes the multi-GB settlement/strategy DBs,
streams gzip compression, uses a compact SQL scoreboard, and exits on invalid
Git credentials instead of retaining multi-GB memory.  The current stack was
started with `-NoLearningExport` because the configured GitHub credentials are
invalid.  Focused V16, path-forecast, and exporter coverage passed **44/44**.

## Shipped 2026-07-31 - Full-repo review: secret leak, executor lifecycle, freshness gates

Full audit of the repo (6 parallel reviewers + adversarial verification), then
every confirmed finding fixed, then merged with the TT-Edge work on `origin/main`.
Suite (both systems together): **3030 passed, 7 skipped**; `config_audit --check` OK.

**Cross-system bug the merge exposed — `tt_edge` was rewriting the test
environment.**  `tt_edge/envfile.py::bootstrap_env()` splices the real
`.env.local` into `os.environ` (setdefault), and every tt_edge job module calls
it at import.  So once any tt_edge test ran, ~24 live `Q15_ULTOIM_V2_*` values
were resident for the rest of the session and every Q15 test after it was
silently evaluated against PRODUCTION config instead of the defaults it asserts
— 31 failures the first time both suites ran in one process, and, worse, quietly
meaningless Q15 assertions whenever ordering put tt_edge first.  `bootstrap_env`
is now a no-op under pytest when targeting the real `os.environ` (explicit
mappings and `load_env_file` are unaffected, so the tt_edge tests that exercise
the loader still do).  Separately, the two `TestEnsurePlaywright` happy-path
tests now skip when playwright is not installed rather than failing — a missing
optional scraper dep is an environment fact, not a regression.  The local stack was restarted onto this code and verified healthy
(7 assets tracked, executor still `DRY_RUN=true` + `KILL=true` on both books).

**SECURITY - ACTION REQUIRED: the Telegram bot token was published.**
`notifications/telegram_client.py` stored the raw `requests` exception text as
the delivery error, and that text embeds the request URL - i.e.
`.../bot<TOKEN>/sendMessage`.  Three such rows sat in `data/q15_ultoim_v1.sqlite3`,
which `tools/learning_export.py` force-pushes hourly to the **public** repo
`turneraontez-alt/tez`, branch `learning-snapshots`.  Verified present in the
published blob.  Local DBs are now scrubbed (UPDATE + `wal_checkpoint(TRUNCATE)`
+ `VACUUM`, so the bytes are gone from the file, not just the rows); a repo-wide
rescan of 27 DBs / 27.9M text cells is clean.  **The token itself still needs
rotating in BotFather - that is the only outstanding item.**  Redaction now lives
in one shared helper (`redact_token`) used by the shared client, the ultoim
sender and the champion `notifier._redact`; it also scrubs a URL-embedded token
that is not the configured one, so it keeps working across a rotation.

**Executor (live-money) - position lifecycle.**  `on_exit` now CANCELS a still-
resting GTC entry before selling (previously the IoC reduce_only sell filled 0,
we booked ourselves flat, and the original buy kept working).  Positions,
open-tickers and entry order ids now rehydrate from the store, so a restart no
longer makes every defensive close return `NO_POSITION`.  An ambiguous POST
failure (read timeout / mid-flight drop - distinguished from a definite
rejection, which only `ConnectTimeout` proves) now BOOKS the exposure as
`ORDER_UNCERTAIN` instead of silently freeing the window budget, and is recorded
under a new `UNCERTAIN` fill label for reconciliation.  The daily stop persists
its day-start reference (`executor_meta`) and rolls over on the UTC date, so a
restart cannot re-arm it and a long-running process is not frozen on a stale
loss.  Contract count is sized off the price actually PAID (`ask + offset`), not
the ask, so a full fill can no longer exceed `max_stake_per_pick_cents`.
`Q15_EXEC_KILL` / `Q15_EXEC_DRY_RUN` are now live switches - only those two
refresh, so a half-edited environment cannot resize a running book.

**NOT changed, deliberately:** `max_per_window_pct` is still not applied in flat
/ per-interval sizing.  Enforcing it clamped the owner's chosen $100 10M
concentration to $80 and broke the flat two-pick budget; the field's docstring
scopes it to `per_pick_pct * max_picks`.  The docstring is now explicit that
under flat sizing the real ceiling is `flat_stake_cents * max_picks_per_window`.

**Decision-engine freshness gates that were silently inert.**  `quote_age` was
resolved from four key names nothing writes, so `stale_kalshi_quote` and the
liquidity age decay never ran and every ledger row stored
`quote_age_seconds=None`; it now reads `orderbook_event_ts`, the key
`v5_hardening` actually writes.  `apply_v95_policy` unconditionally overwrote the
v5 fail-closed verdict, so a snapshot v5 judged stale could still return
ENTRY_RECOMMENDED - v9.5 may now narrow a decision but never re-open one v5
closed.  `_resolve_checkpoint` took `max()` over ALL snapshots including
`market_state="upcoming"` markets tens of minutes out, pinning the label to 15M
for whole windows (mis-filing predictions and, since 15M alerts default off,
suppressing alerts entirely); it now considers only live markets within one
window (`Q15_V95_MAX_WINDOW_SECONDS`).

**Feed integrity.**  `spot_depth` had no crossed-book guard (both siblings do):
a crossed book now drops the snapshot, flags a resync, and stops absorbing deltas
until a full snapshot rebuilds it.  MarketLead's freshness gates all measured the
transport, never the book - `sample_timestamp` is stamped at read time and
heartbeats keep `message_age` low after a level2 subscription dies - so a frozen
book was ingested at quality 1.0; added `Q15_MARKETLEAD_BOOK_STALE_SECONDS`
(default 60).

**Statistics.**  Isotonic calibration blocks are now shrunk toward the sample base
rate (`Q15_V95_ISOTONIC_PRIOR_WEIGHT`, default 5): raw PAVA on binary labels ends
in blocks of ONE whose mean is exactly 0/1, and since prediction clamps beyond the
outermost anchor, a single settled contract could pin every raw probability above
it to 0.99.  `_paired_better_test` now clusters by `close_time` before the paired
t-test - one row per asset per checkpoint meant up to 7 rows shared one settlement,
understating the standard error ~2x.  Replicating a window no longer changes the
p-value.  **Standing caveat: the strongest evidence in this repo is still the
fourth disjoint V15 audit, which failed all 14 gates with the candidate scoring
identical to the market.  Nothing here establishes the system is +EV.**

**Security / ops.**  Flask now binds loopback by default (`Q15_BIND_HOST`) - all 83
routes are unauthenticated and `/api/q15-v9/telegram-outbox` returns the verbatim
text of recent alerts; optional `Q15_API_TOKEN` gates `/api/*` and `/data/*` (the
dashboard stays open).  **If you viewed the dashboard from another device, set
`Q15_BIND_HOST=0.0.0.0` to restore it.**  The 455 MB `q15_v91_state.sqlite3` was
the one DB genuinely inside OneDrive (`data/` and `work/` were already junctions
since 2026-07-14); copied via SQLite's online backup API to
`%LOCALAPPDATA%\Q15\state\`, integrity verified, 1,279,433 observations intact,
and `Q15_V91_SQLITE_PATH` set in `.env.local` - the original is still in the repo
root and can be deleted after a clean day.  v91 and the settlement index now open
WAL instead of the rollback journal.  Removed 168 `.pytest_tmp_*` dirs (5.46 GB)
from the synced folder and gitignored the pattern.  README/CLAUDE.md no longer
claim the system cannot trade: the executor is called out explicitly, since the
`place_order(` source guards only cover the decision engine and cannot see it.

**Corrections to earlier findings in this session:** the settlement index is NOT
corrupt (`integrity_check: ok`, 8.26M rows scan clean) - the "malformed" reading
was an artifact of opening a live-written DB with `immutable=1`.  And `data/` was
never OneDrive-synced.

## Shipped 2026-07-26 - V15 rejected; untouched test remains sealed

The third disjoint V15 pretest reservation verified all 288 authorized
NON_BTC_TRANSFER contracts against finalized Kalshi evidence, then stopped
before scoring because the architecture rows carried `v15_features` beside
the wrong feature-name vector.  It produced no result and remains permanently
ambiguous.  `_with_features` now binds `v15_features` to
`v15_feature_names` and `v14_features` to `v14_feature_names`; unknown
feature keys fail closed.  Regression coverage protects the binding.

A fourth protocol was frozen before any fourth-population label access:
`config/q15_rti_v15_fourth_disjoint_audit_protocol.json`, identity
`q15-rti-v15-non-btc-fourth-disjoint-audit-v1`, SHA-256
`baad00d291d83d4992059fd112eb2925b93ac384fb4ad460981dd14a6bb401db`.
It excluded all 144 close windows / 864 rows previously authorized across the
three ambiguous reservations, regardless of outcome.  The new earliest-60
seal is
`reports/q15_rti_v15_audit_seals/non_btc_transfer-fourth-disjoint-60-v1.json`,
SHA-256
`ce0b40c032113b869f1b9918e8d1607d48b40b88237efa242300d0fafc0b9a94`.

The one-shot pretest completed with authoritative Kalshi verification for
exactly 288 sealed train/calibration contracts.  The expanding walk-forward
validation covered 24 close windows / 144 rows.  V15 selected zero residual
trust in every fold and was identical to V14 and the point-in-time market on
all validation rows: accuracy 70.83% (102/144, Wilson 95% 62.95%-77.64%),
Brier 0.190433, and log loss 0.565212.  Every required improvement/effect gate
failed.  Result status is
`WALK_FORWARD_GATE_FAILED_UNTOUCHED_TEST_REMAINS_SEALED`.

Calibration was not run; its preregistered rows were already part of the
expanding walk-forward sequence and would not have been an independent sample.
The untouched-test labels were never read or scored.  No V15 artifact,
challenger, Telegram notification, promotion, or trading path was created.
The existing frozen controls remain unchanged.  Focused V15/health coverage
passed 102/102; this session did not rerun the complete repository suite.

## Shipped 2026-07-23 - V15/V14 audit execution failures quarantined

V14 still has no defensible accuracy result.  The first exclusive
NON_BTC_TRANSFER V15/V14 pretest reservation was created from the earliest 60
eligible windows, but its label callback stopped before scoring because one
authorized XRP row was unresolved in the local settlement cache.  Kalshi later
reported that exact contract finalized.  The reservation remains permanently
ambiguous and was not deleted or replayed.

A recovery protocol then excluded all 48 close windows authorized by that
first reservation, regardless of outcome, and sealed the earliest 60 disjoint
windows.  Its one-shot pretest also stopped before scoring: nine authorized
local rows had YES/NO labels but `resolved_at` timestamps between 0.04 and 2.78
seconds before scheduled close.  That reservation is also permanently
ambiguous and will never be replayed.  Neither run produced walk-forward,
calibration, untouched-test, V14 accuracy, V15 accuracy, model, artifact,
notification, promotion, or trading evidence.

The settlement verifier now treats exact finalized Kalshi result, close time,
and post-close `settlement_ts` as authoritative.  A resolved local label must
still agree, but an unresolved or timing-degraded local cache cannot override
valid Kalshi evidence.  Future strategy-ledger resolution timestamps are
clamped to at least contract close.  Focused settlement/V15/ledger coverage
passed 36/36.

`config/q15_rti_v15_final_disjoint_audit_protocol.json` freezes a third audit
before any of its labels are read.  It excludes the union of all 96 previously
authorized close windows / 576 NON-BTC rows, with close-time SHA-256
`406865685aa3295e91d18c9fe1e9c4a3a22d797a4d249474cad638b33c73b1b6`
and row-ID SHA-256
`ada9a6471284a2b4a2b6d42d77e12b9ebc440d528e6c90382f21ccde9d5e5672`.
The frozen protocol identity is
`q15-rti-v15-non-btc-final-disjoint-audit-v1`, SHA-256
`04a9dbf0abaf86b9c6e80b7e91efea0dbed8675f55fdb97a231191d04ad32bac`.
At freeze time 20 complete disjoint windows existed, so 40 additional
15-minute windows (approximately ten hours) were required.  Features, models,
thresholds, costs, 36/12/12 folds, gates, BTC isolation, paper-only status,
and manual promotion rules remain unchanged.

## Shipped 2026-07-23 - outcome-blind V15 prospective PAPER contract

The prospective deployment and review rules are now frozen before any V15
outcome review in `config/q15_rti_v15_paper_deployment_protocol.json`.
Identity
`q15-rti-v15-prospective-paper-deployment-and-review-v1` has canonical
SHA-256
`b4ae8b458a5241c289d03d32186fec8ff2f6b6247d00323cca18863a40f82175`.
`tools/q15_rti_v15_paper_preregister.py` validates the exact protocol without
database, outcome, model, Telegram, promotion, or order capability.  Focused
protocol/readiness/health coverage passed 100/100.  After the overlap
disclosure hardening, the complete repository passed 2,662 tests with 5
skipped in 269.27 seconds.

The contract permits manual artifact creation only after a ready seal and
passing finalized pretest plus untouched test with authoritative Kalshi
settlement evidence.  The deployed model must be the exact passing pretest
train-plus-calibration model/trust; untouched-test labels cannot enter the
artifact fit.  The prospective boundary is the first complete close whose
exact 13-minute decision timestamp is strictly after successful artifact
load, and no historical row can be reclassified.

The future ledger identity is
`q15-rti-v15-prospective-paper-ledger-v1`.  It freezes one opportunity row per
asset/window, immutable decision/source/model/quote evidence, insert-before-
notification ordering, deterministic decision and Telegram idempotency keys,
crash recovery without rescore, Kalshi official-result evidence, compare-and-
set resolution, official fees plus 2-cent slippage for 10 contracts, and zero
P/L for unaccepted or data-ineligible rows.  Notifications must say
`V15 PAPER` and remain producer-only through the durable outbox.

Prospective reviews use the earliest 30, 60, and 150 resolved accepted picks
per cohort without sub-selection.  They require positive adjusted P/L, Wilson
95% lower accuracy above the cohort's average adjusted break-even rate,
candidate Brier/log-loss improvement and clustered upper bounds below zero
versus both the point-in-time market and frozen V14, plus zero timestamp,
hash, settlement, ledger, or cohort-contamination failures.  Trade frequency
and rejected counterfactuals are reported but cannot be outcome-tuned.
Automatic refit, automatic promotion, and real trading remain forbidden.
No artifact, scorer, ledger database, or V15 notification has been created or
enabled; this section freezes the later implementation contract only.
The 07:47 and 08:02 exact captures completed 7/7.  After safe restarts, live
health returned `status: ok`, 56 source-complete windows, 55 V15-auditable
windows, one feature-ineligible source window, nine maintenance closes with
zero credit, source quality PASS, labels unread, and the frozen PAPER protocol
identity visible.  Artifact creation, V15 scoring, V15 notifications,
automatic promotion, and real trading all remain false.

## Shipped 2026-07-23 - durable V15 pretest and honest audit-ready counter

`tools/q15_rti_v15_pretest.py` now provides the missing durable one-shot
development/calibration execution layer.  It reconstructs and hash-checks the
ready audit seal and its exact feature rows before writing an exclusive
reservation.  Only after that reservation exists may the supplied callback
read the exact train/calibration row IDs.  A crash after reservation is
permanently ambiguous and cannot invoke the callback again.  The result stores
the authorized labels and exact walk-forward/calibration report hashes, while
untouched-test labels, paper artifacts, notifications, automatic promotion,
and real trading remain false.  Walk-forward failure never runs calibration;
either earlier failure leaves the untouched test sealed.

`tools/q15_rti_v15_pretest_command.py` is the manual database command for this
layer.  Its read-only SQLite callback selects only the IDs passed by the
reserved core runner and rejects missing, unresolved, invalid, or duplicate
rows.  It additionally verifies every returned ticker, asset, and close time
against the sealed outcome-free evidence and refuses labels whose resolution
timestamp precedes the contract close.  The production callback now performs
a fresh, independent check against Kalshi's public market endpoint after the
exclusive reservation exists.  It fetches exactly the authorized contracts,
requires the returned ticker and close time to match the seal, requires final
`finalized` status and a YES/NO result, and fails closed if Kalshi disagrees with
the local label.  The returned evidence manifest is hash-sealed and bound into
the append-only audit result.  Train/calibration cannot query untouched-test
contracts.  The immutable identities are now
`q15-rti-v15-durable-one-shot-train-calibration-v4` and
`q15-rti-v15-append-only-train-calibration-state-v4`; live health exposes both.

`tools/q15_rti_v15_untouched_test_command.py` closes the corresponding final
execution gap.  It accepts only a finalized passing pretest state, revalidates
that state against the current seal and reconstructed outcome-free evidence,
and passes the stored train/calibration labels and exact report hashes directly
to the frozen untouched-test runner.  Its callback receives only the sealed
test IDs and is invoked only after the exclusive test reservation exists.
Failed or ambiguous pretests cannot create a test reservation or read a test
label.  Repeated completed calls return without rereading labels.
The untouched-test identities are now
`q15-rti-v15-durable-one-shot-untouched-test-v4` and
`q15-rti-v15-append-only-untouched-test-state-v4`; that stage applies the same
exact-contract Kalshi verification and result binding.
The verifier was exercised live only against a pre-V15 exact DOGE contract
whose outcome was already in the legacy ledger.  That probe exposed that
Kalshi's actual terminal status is `finalized`, not `settled`; the old
assumption would have made the one-shot audit fail permanently before scoring.
Settlement evidence v2 now requires `finalized`, exact ticker and close-time
alignment, a YES/NO result, and a settlement timestamp no earlier than close.
The same pre-V15 probe then passed with an exact close-time match and Kalshi
settlement 3.957354 seconds after close.  No protected V15 outcome was queried.
The verifier was then exercised across one complete, already-graded pre-V15
close containing BNB, BTC, DOGE, ETH, HYPE, SOL, and XRP.  All seven exact
contracts passed with `finalized` status and exact close-time alignment.
Focused maintenance, V15 audit, paper-contract, and health coverage passed
144/144 after this correction.  The subsequent complete repository run passed
2,663 tests with 5 skipped in 276.79 seconds.
An adversarial fold audit also made a preregistered dependency explicit:
the expanding walk-forward validation sequence includes the later 12-window
calibration partition, and the calibration gate then evaluates that partition
again.  This is not test leakage and the frozen architecture is not changed
from the result, but calibration is therefore not a second independent sample.
Evaluator
`q15-rti-v15-paired-comparator-walk-forward-v2` now emits and hash-binds the
exact overlapping close times and row IDs, labels the calibration evidence as
non-independent, and identifies the one-shot untouched test as the only
independent final historical confirmation.  Calibration refuses a missing or
tampered overlap disclosure.
The audit-seal writer also refuses to persist a `WAITING` state, so an early
manual command cannot consume the exclusive production path and block the
later ready seal.  A live 51/60 refusal exited nonzero and created no file.

The readiness monitor now distinguishes source-complete path windows from the
strict V15 audit population.  `GEOMETRY_30` still uses source-complete path
evidence, but `NON_BTC_60` and `BTC_150` now use the exact stricter population
from the audit seal: all seven assets, exact contract identity, every frozen
V14 feature, and all five independent-path features must pass together.  This
found one honest exclusion at close 1784798100 (05:15 ET): its 05:02
post-maintenance capture had complete independent paths but insufficient
60-second upstream V14 context after restart.  It remains source-health
evidence but receives no V15 model/audit credit.

Focused V15/readiness coverage passed 138/138 and the latest complete
repository run passed 2,645 tests with 5 skipped in 263.91 seconds.  The V15
audit population scan
now skips all timestamps before the immutable V15 boundary before building
features; this preserves the exact eligible population while returning cached
live health in 0.33 seconds after refresh.  After safe restarts, live health
showed 54 source-complete windows / 378 rows, 53 V15-auditable windows / 371
rows, one
feature-ineligible source window, nine maintenance closes with zero audit
credit, zero degradation events, source quality PASS, labels unread, no model
fit, scoring disabled, and real trading disabled.  A one-time same-thread
continuation is active for 09:20 ET, just after the expected 60th auditable
capture at 09:17, to run the frozen manual non-BTC audit if collection remains
clean.  The local paper service was safely restarted at 07:37 ET; health
returned `status: ok`, the readiness thread was alive, the 07:32 capture was
7/7, and health exposed both v2 one-shot runner identities plus the
authoritative Kalshi settlement-evidence identity.  Labels remain unread and
all scoring, promotion, candidate notification, and trading authority remain
disabled.

## Shipped 2026-07-23 - date-bounded scheduled-maintenance classification

The user confirmed that the missing exact windows from 03:00 through 05:00
ET on 2026-07-23 were scheduled maintenance.  The outcome-blind readiness
monitor now classifies exactly those nine close times as scheduled
maintenance using `config/q15_rti_scheduled_maintenance.json`, version
`q15-rti-scheduled-maintenance-v1`, canonical SHA-256
`3e56900ccca6f1e9f970b0eee5f182e6fcf8356486837f857bf84338689204ef`.
The exception is limited to this date and close-time range.  It changes health
classification only: all nine windows remain missing, receive zero readiness
or audit credit, and cannot be backfilled.  It does not change features,
models, thresholds, gates, labels, scoring, notification eligibility,
promotion, restart policy, or trading authority.  Missing windows outside the
frozen range still trigger the prospective-degradation path.

Focused readiness/health coverage passed 78/78 and the complete repository
passed 2,616 tests with 5 skipped in 379.46 seconds.  The local service was
restarted safely and returned `status: ok`.  The 06:32 ET capture completed
7/7 with a 0.884-second offset, zero missed deadlines, zero record failures,
and all seven independent paths present.  A fresh outcome-blind snapshot then
showed 50 complete windows / 350 rows, nine maintenance closes, zero
prospective degradation events, source quality PASS, labels unread, scoring
disabled, and real trading disabled.

## Shipped 2026-07-23 - V15 paired audit and append-only untouched test

`tools/q15_rti_v15_walk_forward.py` is the pure, in-memory V15 evaluator.  It
reconstructs the frozen non-BTC and BTC folds, fits V15 and the frozen V14
architecture separately, selects residual trust only inside earlier
chronological data, and requires V15 to clear the preregistered Brier/log-loss
and paired close-window bootstrap gates against both V14 and the point-in-time
Kalshi prior.  Calibration is a separate gate; only after it passes are the
final V15/V14 trust factors reselected from development plus calibration.
Accuracy remains report-only and no test row is used during either stage.

`tools/q15_rti_v15_untouched_test.py` now implements the later manual one-shot
test.  Before any test label callback it reconstructs the exact seal evidence,
replays and byte-hash compares the passing walk-forward/calibration reports,
refits both architectures on the identical pretest population, and writes an
exclusive append-only reservation.  Reservation and result are separate
exclusive files: a crash after reservation is permanently ambiguous and can
never invoke the label callback again.  A finalized run also returns without
rescoring.  The test requires the frozen proper-score/bootstrap effects versus
market and V14, at least five executable picks, and positive official-fee plus
2-cent-slippage P/L at 10 contracts.  It reports accuracy/Wilson, calibration,
P/L, EV, drawdown, rejected counterfactuals, and fixed asset/side/distance/
volatility/regime/path-depth/path-spread slices.  Passing is historical
evidence only; no artifact, notification, automatic promotion, or real order
is created.

The reporting bins were frozen outcome-blind in
`config/q15_rti_v15_reporting_protocol.json`, canonical SHA-256
`57c668865a90be5dc18a301210bff4f77614b3275b414223e8f03aaffa60439d`.
The audit seal advanced to
`q15-rti-v15-outcome-blind-audit-execution-seal-v2` and now binds the explicit
YES/NO depth-availability flags and V14 feature-name identity used by the
economics simulator, preventing a missing book side from becoming a fake
fill.  Adversarial tests cover duplicate/resumed callbacks, crash ambiguity,
feature/report/reporting-protocol tampering, rehashed reservation safety
tampering, rehashed finalized-result tampering, and the positive-P/L/
minimum-pick gate.  Focused V15/health coverage passed 57/57 and the complete
repository passed 2,614 tests with 5 skipped in 400.00 seconds.  The local
service restarted healthy with exact collection alive, source quality PASS,
V15 binding verified, the audit-tooling identities visible in health, and no
outcome or scoring authority.

## Shipped 2026-07-22 - outcome-blind executable V15 feature binding

The first-30 immutable independent-path geometry review passed at exactly 30
complete seven-asset windows / 210 rows.  Every one of the eleven frozen
checks passed, source quality was `PASS_ALL_CREDITED_COMPLETE_ROWS`, contract
and independent-formula mismatches were zero, and outcomes/model/scoring
remained unread and disabled.  The immutable artifact is
`reports/q15_rti_independent_path_geometry_freeze_30/geometry-review.json`;
its payload SHA-256 is
`d4831b2a68a0af6cb73af721829d2a0a54df2786451063110fa9be4ce58afe7c`,
its exact selected-evidence SHA-256 is
`0097294deccc40b845fdc8a0a62a7b697d42f2ada78422482fddb66c8388718a`,
and its byte SHA-256 is
`2890fecf1482d8ada40bdeaaed87cdc2a8ad10be727737f3e0298ee60f573eb1`.
The decision is `PASS_CONTINUE_PROSPECTIVE_COLLECTION_NO_MODEL`; it does not
authorize outcome access or a fit.

The separately authorized V15 executable **feature** design is now bound at
`config/q15_rti_microstructure_design_v15.json` as
`q15-rti-market-anchored-independent-path-augmented-residual-v15`, SHA-256
`8c6facc23a0a56968c8a24408272f676923e78a7b51111a39122577d902f0ba7`.
Its first 20 inputs are the frozen V14 vector in identical order and values;
the final five are the frozen independent-path summaries.  The runtime in
`q15_upgrade/strategy_bots/rti_microstructure_v15.py` accepts a row only after
recomputing every added value from its canonical Coinbase/Kraken evidence and
checking the path design/schema, two-venue availability, exact source cutoff,
and 0-2 second exact-13M timing.  Missing, partial, non-finite, stale, or
tampered path evidence fails closed; no path imputation is allowed.

`tools/q15_rti_v15_design_binding.py` verifies the charter/protocol/V14/path
lineage, exact immutable geometry artifact bytes and payload, binding time,
feature order, unchanged V14 training/trust/entry policy, cohort isolation,
chronology, and all safety flags.  A rehashed feature-order tamper and an
artifact-byte mismatch are both rejected.  The readiness monitor and health
surface the bound design identity and feature runtime while keeping outcome
access, fitting, probability scoring, model artifacts, candidate
notifications, automatic promotion, and real trading false.

The live feature-only preflight accepted 30 complete V15 windows with zero
timestamp failures before restart; the next 23:32 ET exact capture then
completed 7/7 at offsets 0.009-0.207 seconds, advancing collection to 31
complete windows / 217 rows.  Focused binding/health coverage passed 87/87 and
the complete repository passed 2,582 tests with 5 skipped in 228.72 seconds.
The local service was restarted and returned `status: ok`, Kalshi websocket
connected, exact collector thread alive, V15 feature design created, and every
V15 predictive/trading capability still disabled.  Continue collection to
60 non-BTC windows and 150 BTC windows; do not open either cohort's labels
before its independent readiness gate.

## Shipped 2026-07-22 - V15 outcome-blind cohort audit execution seal

`tools/q15_rti_v15_audit_seal.py` now commits the future one-shot audit to the
exact earliest eligible evidence before any label can be opened.  It validates
the immutable V15 binding and geometry artifact, accepts only complete
seven-asset exact windows with valid Kalshi contract identity, independently
checks the point-in-time market prior, proves V15's first 20 values equal V14,
and freezes cohort row IDs, feature evidence, development/calibration/test
partitions, and every outer/inner chronological fold by SHA-256.  V15, frozen
V14, and the Kalshi market must use identical rows; V14 is explicitly forbidden
from receiving the five path inputs.

The seal contains no label callback, settlement query, model fitter,
probability scorer, Telegram sender, promotion path, or order path.  Merely
becoming ready still leaves train/calibration and untouched-test label access
false.  A later separate manual action may open only the ready cohort's
train/calibration IDs; the other cohort stays sealed, and untouched-test IDs
require all prior gates plus an exclusive one-shot reservation.  The seal
writer uses exclusive-create semantics and refuses an existing output.

Adversarial synthetic coverage proves 60-window non-BTC and 150-window BTC
fold geometry independently, keeps all same-close assets together, removes
credit for one missing asset or a wrong Kalshi contract, shows that changing
outcome values cannot change the seal/evidence hashes, and rejects rehashed
safety, minimum, partition, or comparator tampering.  The live command
correctly returned `WAITING` at 32/60 non-BTC and 32/150 BTC with all outcome,
fit, scoring, artifact, notification, promotion, and trading flags false.
The 23:47 ET capture completed 7/7 at offsets 0.093-0.263 seconds.  Focused
coverage passed 23/23 and the complete repository passed 2,593 tests with 5
skipped in 238.26 seconds.

## Shipped 2026-07-22 - exact Kalshi contract identity alignment

Audit v7 now refuses readiness credit unless every independent-path row names
the correct Kalshi asset series and the ticker-encoded local close maps to the
stored absolute `close_time`.  The validator also checks the redundant ticker
minute suffix and evaluates both Eastern-time folds on the fall DST boundary,
so ambiguous 01:xx closes are matched by timestamp rather than guessed.
Malformed tickers, cross-asset ticker reuse, neighboring-close joins, and
suffix mismatches all fail closed without reading outcomes.

The outcome-blind live preflight checked all 2,881 exact rows in the strategy
ledger and found zero contract-identity mismatches.  The frozen review's
source-quality object now binds the identity version, selected-row count,
zero-mismatch requirement, asset/close alignment requirements, DST-safe flag,
and `outcome_labels_read: false`.  The readiness notice requires this object,
and health exposes it separately from the feature-evidence SHA-256.  Focused
coverage passed 101/101 and the complete repository passed 2,565 tests with 5
skipped in 228.57 seconds using a test-only outbox.  The local service was
restarted and the 23:02 ET capture completed 7/7 with a 0.380-second offset.
Audit v7 is at 29 windows/203 rows, zero formula or
contract mismatches, source quality PASS, and no outcome/model/trading access.

The two V15 comparators were refreshed outcome-blind.  The independent Kalshi
prior preflight checked all 224 path-boundary rows: zero mismatches, maximum
probability delta `5.55e-17`, maximum exact offset 0.479 seconds, and zero
Kalshi/source cutoff delta.  The frozen V14 base-feature audit found all 20
features available across 34 complete executable windows, with zero
unavailable rows, unusable windows, or timestamp failures.  Its durable report
is `reports/q15_rti_v15_v14_base_feature_preflight/audit.json`, SHA-256
`73a657fb0b3b534ef2f00f823e03713b4266b3e2d95fd871c36bded36e200349`;
the design binding SHA-256 is
`6c08f4bd3c7991d7abbe1bb8e9af5c983443f114448967e2818e8b54a00dc820`.
Both preflights report outcomes unread, model fit false, and no artifact,
automatic refit, promotion, notification, or trading authority.

## Shipped 2026-07-22 - exact first-30 evidence identity and review recomputation

Audit v6 now commits the geometry review to the exact outcome-free feature
evidence, not only aggregate covariance/source-quality summaries.  The
earliest review slice receives a canonical, order-stable SHA-256 over asset,
ticker, side, timestamps, and every `rti_independent_path_*` field.  Outcome
and P/L columns are excluded by construction and adversarial tests prove that
changing labels cannot alter the hash while changing a path feature does.

The immutable freeze requires this identity to cover exactly 210 rows at the
30-window trigger.  It also independently recomputes the complete geometry
decision from the frozen protocol before accepting either a live report or a
rehashed artifact.  A forged PASS over failing geometry, changed row-evidence
hash, duplicate/off-cadence close identity, unsafe source flags, malformed
timestamp, or altered payload schema now fails closed.  The readiness notice
requires audit v6, the independent reference verifier, zero formula
mismatches, and the exact evidence identity before any milestone message can
be sent.  Health exposes the identity version, row count, SHA-256, and its
outcome-access flags.

The first post-deploy capture at 22:32 ET completed 7/7 at a 0.078-second
offset with zero missing paths, failures, or missed deadlines.  Collection is
at 29 complete windows / 203 credited rows, leaving 1 window to the immutable
freeze.  The live
identity SHA-256 is
`09dcfefa7d140f9805ee3229e21bd23becfa3e5b75c45f221fe018d6c6102a60`;
source quality is PASS, reference mismatches and prospective degradation
events are zero, and outcomes/model fit/promotion/trading remain false.
Focused coverage passed 99/99 and the complete repository passed 2,563 tests
with 5 skipped in 226.34 seconds using a test-only Telegram outbox.  The local
service was restarted and health returned audit v6 with the 182-row identity.

## Shipped 2026-07-22 - exact-ticker settlement conflict firewall

Future RTI/V15 grading now fails closed at both settlement reconciliation
boundaries.  `IntervalResearchLedger.resolved_results_for_tickers` requires
one unanimous explicit YES/NO label for the exact requested Kalshi ticker; an
adversarial two-model test now proves that conflicting persisted labels return
no result.  The live current-event lane also normalizes each source batch
before touching either ledger.  Duplicate unanimous labels are graded exactly
once, while a same-batch YES/NO conflict for one ticker is logged and rejected
everywhere instead of accepting whichever label arrived first.  Outcomes are
never inferred from spot direction, neighboring closes, or price movement.

The 22:02 ET independent-path capture completed 7/7 at a 0.429-second exact
offset with zero missing paths, record failures, or missed deadlines.  The
outcome-blind audit advanced to 25 complete seven-asset windows/175 credited
rows, leaving 5 windows to the immutable geometry freeze.  Source quality is
PASS for every credited row, reference-formula mismatches and prospective
degradation events remain zero, and outcome access/model fit/promotion/trading
remain false.  Focused settlement coverage passed 180/180 before the final
hardening, the expanded interval suite passed 38/38, and the complete
repository passed 2,555 tests with 5 skipped in 237.07 seconds.  The local
service was restarted and returned healthy with the readiness monitor at
25/30.

## Shipped 2026-07-22 - immutable first-30 path geometry freeze

The independent-path geometry decision now has a separate immutable manual
freeze contract, created before the 30-window trigger.  The contract is
`q15-rti-independent-path-geometry-30-immutable-freeze-v1`, SHA-256
`0c624e12bf0015188e0b20c1c3b5354f70517f044994a89ee584183e02df6340`.
It binds the frozen source design, outcome-blind geometry protocol, V15
charter/evaluation protocol, earliest-30 selection rule, exact seven-asset row
counts, output schema, and fail-closed behavior.

`tools/q15_rti_independent_path_geometry_freeze.py` is the only writer.  It is
manual-only: the background readiness monitor reports the milestone but
cannot create or overwrite the artifact.  Before 30 complete windows it
returns `WAITING_FOR_30_COMPLETE_WINDOWS` and creates no directory or file.
At 30 it will select exactly the earliest 30 complete windows (210 rows: 30
BTC and 180 non-BTC), project feature/source evidence only, and write
`reports/q15_rti_independent_path_geometry_freeze_30/geometry-review.json`
with exclusive-create semantics.  Later invocations can only verify the same
canonical payload and evidence hashes; any mismatch fails closed.

The feature-only database loader is now protected by a SQLite authorizer in
addition to its explicit SQL allow-list.  SQLite itself denies reads of
`official_result`, `correct`, `hypothetical_pnl_cents`, or `resolved_at` while
feature-only evidence is loaded.  This prevents a future query edit from
silently reading labels and then claiming `outcome_labels_read: false`.

The V15 preregistration validator now semantically enforces the full frozen
contract in addition to verifying its SHA-256.  Even a newly rehashed manifest
is rejected if it weakens point-in-time fail-closed evidence, same-close/inner
fold chronology, the exact market or V14 comparators, close-window bootstrap,
effect-size bounds, calibration population, fixed reporting, or prospective
V15/PAPER notification policy.  This closes the gap where an immutable file
could still be faithfully hashed after its meaning had been weakened.

Audit v5 adds a genuinely independent equation verifier,
`q15-rti-independent-path-reference-equations-v1`.  The production collector
and production reconstruction previously shared their feature combiner, so a
common formula bug could theoretically make both agree.  The reference module
imports none of the production combiner and separately calculates all five
frozen features from canonical raw Coinbase/Kraken rows.  Audit credit now
requires both verifiers to pass.  An adversarial test deliberately injects a
wrong production combiner: production capture and reconstruction agree, while
the reference verifier rejects the row as intended.  Live history has zero
reference-formula mismatches.  Health exposes the audit/reference versions and
mismatch count after the readiness monitor has a snapshot.

The V15 market comparator now has a repeatable, outcome-blind preflight at
`tools/q15_rti_market_prior_consistency_audit.py`.  It independently despreads
the decision-time YES/NO quotes and checks that the stored, side-normalized
Kalshi probability is identical.  It also requires the source capture to be
within the existing two-second exact window and the Kalshi/source cutoffs to
match within `1e-6` seconds.  Across all 196 rows at or after the path
boundary, 196 were checked, zero mismatched above `1e-9`, and the maximum
absolute difference was floating-point noise (`5.55e-17`).  The tool uses the
database-level outcome-column authorizer, cannot fit or score a model, and
tests cover YES/NO normalization, quote fallback, boundary exclusion, and an
inconsistent/reused-market-mid or timestamp failure.  The maximum live exact
offset is 0.440 seconds and every Kalshi/source cutoff delta is zero.

Execution economics were rechecked against Kalshi's official July 7, 2026 fee
schedule.  The frozen code matches the governing taker equation
`0.07*C*P*(1-P)` with multiplier 1, rounds fee plus position cost to a
centicent, evaluates the fee at the 2-cent-worse simulated fill, and applies
no settlement fee.  All 21 published 100-contract table rows now have
regression coverage.  The PDF displays their centicent values rounded up to a
whole cent (for example 173.25c as $1.74); tests verify that presentation layer
without incorrectly charging the display rounding in paper P/L.  Side-specific
asks/depth remain required, 10 displayed contracts are mandatory, and no
historical row is claimed as an actual fill.

The freeze cannot read outcomes, fit or score a model, send candidate picks,
promote a rule, or trade.  Passing geometry only allows collection to
continue; failing geometry blocks the 60/150 outcome audits and requires
manual diagnosis without automatic feature or threshold changes.  The live
dry-run at 18 windows returned `WAITING` and emitted no artifact.  Collection
has since reached 29 complete seven-asset windows/203 rows, leaving 1 window
to the locked review.  All five features remain active, finite, full-rank,
and below the frozen correlation/conditioning ceilings; source quality is
PASS.  The most recent exact capture recorded 7/7 paths at a 0.380-second
offset with zero failures or missed deadlines.  After the database-level
outcome firewall, semantic validator hardening, and independent reference
verifier plus market-prior preflight, focused suites passed and the complete
repository suite passed 2,565 tests with 5 skipped in 228.57 seconds.

## Shipped 2026-07-22 - outcome-blind V15 path successor preregistration

Before opening any independent-path outcome, one successor candidate and its
entire evaluation contract were frozen.  The charter is
`q15-rti-v15-independent-path-augmented-nested-safe-residual-preregistration-v1`,
SHA-256
`513c240977ee6640b0ebd06f9839dc37b787e5a13930c87eee2d1e4a7407791e`.
The evaluation protocol is
`q15-rti-v15-path-augmented-nested-safe-expanding-walk-forward-v1`, SHA-256
`e4233be29f211e314344d817de296ca3ef99f5026f67edcaadbe2a44d25b3536`.
Both are validated by
`tools/q15_rti_independent_path_successor_preregister.py` without database or
model-execution access.

The sole proposed V15 architecture is a single 25-feature regularized market-
prior residual: V14's exact 20 features followed by the five frozen path
features.  V14's optimizer, training config, entry policy, nested training-only
trust grid `0/0.25/0.5/0.75/1`, and exact factor-zero Kalshi fallback remain
unchanged.  Interactions, polynomial expansion, feature selection,
hyperparameter search, partial windows, and missing-path imputation are all
forbidden.  Frozen V14 and the point-in-time market are parallel controls on
identical rows.

No executable design can be created unless the first-30-window locked geometry
review passes and a separate manual action occurs.  Non-BTC remains sealed
until 60 complete windows and BTC until 150.  The candidate must materially
beat both market and V14 on Brier and log loss in chronological same-close
folds, pass paired close-window bootstrap bounds, calibration halves, and the
one-shot untouched test before a PAPER challenger may be created.  Accuracy is
report-only.  Economics retain official Kalshi fees, 2-cent slippage, stored
executable quotes, and 10 contracts; fake fills and reused quotes are
forbidden.  Any future PAPER ledger starts at the next unseen close with zero
historical credit and manual reviews only at 30/60/150 resolutions.

At preregistration, feature-only evidence was 16 windows/112 rows with all five
features active and full-rank in every cohort, source quality PASS, zero
integrity breaches, and outcomes/model/performance unread.  Collection then
advanced cleanly to 18 windows/126 rows, leaving 12 to geometry review.  The
monitor expects all four post-notice-boundary closes, reports zero entirely
missing closes or degradation events, and now exposes three separate pending
milestones: `GEOMETRY_30`, `NON_BTC_60`, and `BTC_150`.  A failed geometry
review can release only the manual geometry notice; it blocks both cohort
audits.  Health exposes the charter/protocol hashes and keeps executable
design, model fit, notification, promotion, and trading false.  Focused
semantic/health tests passed 73/73; the final full suite passed 2,499 tests
with 5 skipped in 242.50 seconds.

## Shipped 2026-07-22 - prospective independent-path degradation notices

The independent L2/L3 path collector now fails loudly in Telegram when a new
seven-asset close cannot be reconstructed.  The notice policy was frozen
before its first eligible close as
`q15-rti-independent-path-prospective-degradation-notice-v1`, SHA-256
`e84a3a36ee98a7637a77ac8aa1bb143ddc1e2c97144dc7364d96d599d77a8e91`.
Historical incomplete windows never generate notices.  The prospective
boundary is close time `1784763000`; the first eligible close is
`1784763900` (19:45 ET, exact decision at 19:32 ET).

Every eligible incomplete close gets one durable, policy-hash-bound V3
Telegram message labeled `PAPER ADMIN`.  It reports the valid row count,
missing assets, and source-only failure reasons.  The window remains excluded
from readiness credit and cannot be backfilled.  The notice performs no
restart, threshold/feature/model change, scoring, promotion, or trading
action, and it cannot select or read outcomes.  Same-process completion
tracking plus the SQLite outbox prevents duplicate delivery across repeated
checks and restarts.  `/api/health -> independent_path_readiness_monitor`
exposes the policy identity/boundary, prospective event list, completed close
times, last delivery result, and unchanged safety flags.

The monitor does not rely only on observed ledger rows: it reconstructs the
expected 900-second close cadence at the immutable close-minus-780-second
decision instant and waits the pinned five-second capture grace.  A total
recorder failure with zero rows is therefore reported with all seven assets
missing instead of disappearing from the audit.  Tests cover the pre-grace
boundary and the entirely absent-window case.

The first two eligible exact captures were clean: 7/7 independent paths each,
zero missing rows, record failures, or missed deadlines; the second decision
offset was 0.230 seconds.  The background monitor advanced from 14 windows/98
rows to 16 windows/112 rows, expected both due closes, found zero entirely
missing closes, and reported zero prospective degradation events.  The
refreshed durable audit has 16 complete windows/112 credited rows, source
quality PASS, zero integrity breaches, and 14 windows remaining to the frozen
geometry review.  Coinbase L2, Kraken L3, settlement index, exact capture, and
the readiness monitor were connected/alive.  Outcome access, model
fitting/scoring, promotion, and real trading all remained false.  Focused
policy/audit/health tests passed 63/63.  The final full repository suite passed
2,476 tests with 5 skipped in 235.64 seconds after deployment.

## Shipped 2026-07-22 - reconstructable pre-decision L2/L3 path evidence

The next outcome-blind data improvement is frozen and collecting separately
from V14.  The audit found that the local Coinbase L2 and Kraken L3 stores
retain dense within-minute paths, while V14's 20 learned fields discard nearly
all of that shape.  A live, label-free benchmark showed 12-13 points per venue
for every asset over 60 seconds, maximum gaps of 5.28-5.56 seconds, all seven
assets complete, and 0.032 seconds total query/reconstruction time.

The source design is `q15-rti-independent-venue-path-evidence-v1`, SHA-256
`ed73729468715279d0d92e1d3beed431cfad416767e4bb92cb88c230d4b904a2`.
It was preregistered before its first eligible exact capture.  No row through
the 15:00 ET close receives credit; the first eligible close is 15:15 ET and
its exact decision timestamp is 15:02 ET.  The benchmark receives no credit.
V14 outcomes and performance were not opened.

For each asset, the source reads only rows at or before the immutable exact
13M cutoff, includes the last state at/before the 60-second start, requires
both venues, at least eight points per venue, top-10 depth, <=10-second start,
end, transport, and continuity ages, and the versioned Kraken partial-fill
schema.  It persists canonical raw selected rows plus their SHA-256.  A second
verifier reconstructs every summary from that evidence and rejects future
rows, stale transport, gaps, schema changes, fingerprints, or stored-feature
tampering.

Exactly five hypotheses were frozen: time-weighted independent depth
imbalance, first-half-to-second-half depth acceleration, cross-venue depth
direction agreement, transient spread-stress ratio, and Kraken observable
partial-fill-flow acceleration.  This does not restore V11's 71-dimensional
design.  A future successor may add only these five fields to V14, must retain
V14's nested market-anchored trust/fallback, keep BTC separate, and wait for
30/60/150 outcome-blind/model/review gates.  The source itself cannot fit,
score, notify, promote, or trade and does not change V14.

The feature-only audit is under
`reports/q15_rti_independent_path_audit_live/`; it selects no outcome column
and had zero eligible rows before deployment, as required.  The first live
collection period produced 77 eligible rows across 11 windows: 71 valid rows,
six invalid rows, and eight complete seven-asset windows.  Every invalid row
had the same outcome-blind availability failure: Kraken's
`cancel_to_add_5s` was NULL when its denominator, `add_count_5s`, was exactly
zero.  That ratio is retained audit context and is not one of the five frozen
features.  The validator now accepts NULL only for this mathematically
undefined zero-denominator case; all other activity fields remain fail-closed.
No five-feature formula, boundary, label, model, notification, or trading
surface changed, and the six historical misses were not backfilled.

The ratio repair added reconstruction coverage for the allowed NULL case.
Focused tests passed 189/189 and the full suite passed 2,430 with 5 skipped in
236.84 seconds.  The paper service was restarted safely after an exact capture;
health returned `status=ok`, the exact thread was alive, all seven assets were
registered, Coinbase L2, Kraken L3, and settlement feeds were connected, and
post-restart exact/path/failure counters were clean at zero.  The first exact
post-repair window then captured the 18:15 ET close at 18:02 ET: 7/7 durable
decisions, 7/7 valid independent paths, zero missing paths, zero record
failures, and zero missed deadlines.  The separate raw-evidence reconstructor
also accepted all seven rows and marked close 1784758500 complete, proving the
runtime counters were not the only source of verification.

The outcome-blind source audit is now v2 and prepares the frozen 30-window
geometry review without waiting to design it after seeing labels.  For all
seven assets together, BTC alone, and the six-asset non-BTC transfer cohort it
reports per-feature variance, active fields, centered standardized numerical
and stable rank, condition number, singular values, maximum pairwise
correlation, high-correlation pairs, and exact signed duplicates.  It cannot
read a settlement column, fit a model, select a feature, or set a predictive
threshold.  At ten complete windows all five fields were active and full
rank in every cohort, with no exact signed duplicates; those values are an
availability preview only, with the
manual review still locked until 30 complete windows.  The focused path suite
passed 9/9, the broader path/exact/runtime/health suite passed 179/179, and the
final repository-wide suite passed 2,431 with 5 skipped in 231.61 seconds.

An outcome-blind independent-path readiness monitor is now deployed.  It
reconstructs the source audit every five minutes, exposes its design binding,
credited windows/rows, excluded invalid rows, and cohort geometry under
`/api/health -> independent_path_readiness_monitor`, and has exactly one
frozen milestone: `GEOMETRY_30`.  At that milestone it can send one durable,
idempotent Telegram message labeled `PAPER ADMIN`; the message explicitly says
that labels are sealed, no model/feature selection/scoring ran, and it is not
a trade signal.  A deficient geometry still triggers manual review rather
than silently selecting or removing a field.

The first exact window after deploying this monitor again captured 7/7 valid
paths with zero misses, record failures, or missed deadlines.  On the second
background check, live health reported 11 complete reconstructable windows / 77
credited rows, six older invalid rows excluded from credit, no ready or
completed milestone, no error, and every label/fit/selection/scoring/trading
flag false.  Focused monitor/path/app-health verification passed 40/40; the
final repository-wide suite passed 2,452 with 5 skipped in 232.86 seconds.

The independent-path audit is now v3 and continuously measures source quality
against the already-frozen collection limits.  For each venue it reports point
count, effective end age, maximum continuity gap, maximum transport age, and
remaining integrity margin distributions, plus the minimum margin by asset.
These are availability diagnostics only; no performance outcome or predictive
threshold is consulted.  A parse failure or frozen-integrity breach blocks the
30-window admin notice rather than allowing questionable evidence to advance.

Live credited evidence currently has 12 complete windows / 84 rows.  Coinbase
has 12-13 points per path and Kraken 12-13; the observed worst continuity gap
is 6.25 seconds against the frozen 10-second ceiling, worst effective end age
is 5.39 seconds, and the minimum integrity margin is 3.75 seconds.  The third
background check reported `PASS_ALL_CREDITED_COMPLETE_ROWS`, zero evidence
parse failures, zero integrity breaches, `WAITING_FOR_MILESTONE`, and no ready
or completed notice.  The exact window following deployment was again 7/7
with zero misses/failures/deadlines.  Focused verification passed 42/42 and the
final repository-wide suite passed 2,454 with 5 skipped in 235.96 seconds.

The independent-path 30-window geometry decision is now frozen before the
milestone.  Protocol
`q15-rti-independent-path-outcome-blind-geometry-review-v1`, SHA-256
`05ab77d4936fa46870437c3c599f73aea8c41d9e4b81355f10060ba8678f7d1a`,
was preregistered after an honestly disclosed 12-window outcome-blind preview
and before any outcome or performance inspection.  Its correlation ceiling
0.95 and condition-number ceiling 50 reuse the earlier frozen V13 geometry
standard; activity, rank, duplicate, finiteness, row-count, and source-quality
checks are structural.  Evaluation is permanently pinned to the earliest 30
complete reconstructable windows, even if run later.  A pass authorizes only
continued collection toward non-BTC 60 / BTC 150; a failure authorizes only
manual diagnosis.  Neither permits outcome access, fitting, feature removal,
threshold changes, activation, or trading.

At the 18:47 ET exact decision, all seven new independent paths correctly
failed closed on `coinbase:path_continuity_gap`.  Raw Coinbase timestamps had
an 11.90-second gap against the frozen 10-second maximum.  The preceding full
`/api/health` request ran from 18:46:39-18:46:43 and began at the same point the
normal five-second Coinbase persistence cadence stalled; the first uncached
health request after a later restart independently took 14.07 seconds.  The
seven failed rows receive no credit and were not backfilled.

`/api/health` now protects the evidence history: during the 75 seconds before
an exact decision and five seconds after, a live service returns its most
recent cached health payload instead of rebuilding the expensive graph.  If no
cache exists it returns an explicit cache-warming status rather than competing
with collection.  A controlled request at 19:01:07, 53 seconds before the next
capture, returned the cached payload in 226 ms.  The 19:02 decision then
captured 7/7 valid paths with zero miss/failure/deadline; raw Coinbase evidence
contained 13 points and a 5.59-second maximum gap.  The outcome-blind ledger is
now 13 complete windows / 91 credited rows, with 17 windows remaining.  Audit
v4 binds the source design and geometry protocol and reports only the two
expected incomplete trigger checks at this stage.  Focused verification passed
48/48; the final repository-wide suite passed 2,460 with 5 skipped in 239.60
seconds.

All recurring outcome-blind readiness scans now share a second capture guard.
V13, V14, and independent-path monitor threads cannot begin a heavy evidence
scan during the 130 seconds before exact capture or five seconds after it.  A
deferred scan waits beyond the protected interval without counting as a check
or error; health exposes its deferral count, timestamp, and requested delay.
The wider 130-second bound covers the required 60-second path plus the observed
uncached health/audit runtimes and margin.

Focused V13/V14/path-monitor/app-health verification passed 85/85 and the full
repository suite passed 2,461 with 5 skipped in 237.61 seconds.  In the first
live protected cycle, V13 woke at epoch 1784762225 (five seconds after the
19:17 ET exact timestamp) and recorded a one-second deferral instead of
scanning; V14 and the path monitor woke later and required no deferral.  The
19:17 decision captured 7/7 valid paths with zero misses, failures, or missed
deadlines, and raw Coinbase evidence had 12 points with a 5.76-second maximum
gap.  The feature-only audit is now at 14 complete windows / 98 credited rows,
zero integrity breaches, and 16 windows remaining to the frozen geometry
review; outcomes remain unread.

## Shipped 2026-07-22 - V14 applies the honest V11 lesson prospectively

V11's opened non-BTC walk-forward evidence was applied without opening its
untouched test or any V13/V14 outcomes.  Across 144 rows / 24 same-close
validation windows, frozen V11 and the market were both 104/144 (72.22%), but
V11 was worse on Brier (0.1993437300 vs 0.1991637153) and log loss
(0.5854907859 vs 0.5850803140).  A nested safe blend over the fixed factors
0/0.25/0.5/0.75/1 selected factor 0 in every chronological fold.  Its honest
opened-fold result was therefore the exact market baseline: 104/144, 72.22%,
Wilson 95% 64.40%-78.89%, Brier 0.1991637153, and log loss 0.5850803140.
This is not a claim of edge or profitability; entry/fill P/L was not inferred
from a probability-only replay, BTC was not included, and the untouched test
remains sealed.

That evidence is now encoded as a separate paper-only V14 challenger instead
of changing V11/V12/V13.  V14 fits the same frozen 20-feature base residual as
V13, then selects how much of that residual to trust using inner chronological
OOF predictions from the current outer training period only.  A nonzero trust
factor must improve both Brier and log loss and pass paired same-close-window
one-sided bootstrap gates; otherwise it falls back exactly to the stored
Kalshi market probability.  Validation, calibration, and test labels never
select the factor, and BTC remains a separate cohort.  V14 never receives
historical credit.

The preregistration charter SHA-256 is
`30d1d00af4cd6abac5d1775e8e722a39b49cb2849311eea7f304c1b2bd2ec670`.
The V14 design SHA-256 is
`aa5efa9a986dc575ee4e358777cd2394b38550ad7328154b58a7d06bf55c3dda`;
the nested walk-forward protocol is
`638db046f638324b1bcf0459c8362a0f0f12cfef35fe9ebbf7d94dd0add87257`.
The reporting, calibration, and selective-value protocol hashes are,
respectively, `88609210e20799933e7b860ee701b47127eb5f799b5b9c2d28ffb90b2c7003eb`,
`72e89f5950b5f70b8603036b0339a35f136421b6051729db7356335c9c6def45`,
and `5a5a3a703f73a021a04993c26824ec9e998f7a6aae74deded5e61b889a654fa4`.
The prospective boundary is close time 1784742300 and the first eligible close
is 1784743200.

The first eligible V14 window captured all seven assets with zero timestamp
failure and zero neutralized input: 7/7 rows and 1/1 window fully observed,
exact offsets 0.0641-0.5347 seconds, and maximum evidence assembly lag 0.5226
seconds.  Outcomes remain unread, no model has been fit, and no artifact,
prediction alert, promotion, or trade is enabled.  An outcome-blind,
idempotent PAPER ADMIN monitor now watches the frozen 30/60/150 readiness
gates and is exposed at `/api/health -> v14_readiness_monitor`.  Focused tests
passed 140/140 and the final full suite passed 2,422 with 5 skipped in 210.24
seconds.  After the local restart, health returned `status=ok`, the V14 thread
was alive with one successful check, 1/1 fully observed windows, zero degraded
rows, no error, and `WAITING_FOR_MILESTONE`; outcome access, scoring,
promotion, and trading were all false.  Coinbase Advanced L2, Kraken L3, and
settlement-index connections were live, and exact-capture record failures
were zero.

## Shipped 2026-07-22 - V13 soft input integrity is no longer hidden

The outcome-blind V13 numerical audit now distinguishes executable model rows
from fully observed rows.  Any retained `*_missing` indicator at or above 0.5
is reported by row, independent close window, asset, and feature.  This is a
diagnostic only: it does not alter a frozen feature value, executable-window
eligibility, readiness credit, outcomes, fitting, notifications, promotion, or
trading.  Exact-capture offset and evidence-assembly lag ranges are also
reported for every executable row.  The audit version is now
`q15-rti-microstructure-feature-audit-v3`.

The same integrity block is included in the V13 milestone snapshot and exposed
continuously at `/api/health -> v13_readiness_monitor`; future 30/60/150 PAPER
ADMIN messages will state both complete windows and fully observed windows.
The current feature-only ledger has four executable seven-asset V13 windows
(28 rows), zero timestamp failures, and zero unusable windows.  Of those, 27
rows and three windows are fully observed.  The first BNB row alone has
`spot_flow_missing=1`: its snapshot itself was current, but the local book was
2.51 seconds old against the frozen 2.0-second limit, so all BNB spot-flow
values were honestly neutralized.  All later BNB rows are present.  Current
exact offsets are 0.0135-0.4664 seconds and evidence assembly is
0.0212-0.4436 seconds.  Cross-asset source ages stayed below 5.68 seconds under
the pinned 10-second budget.

After restart, Coinbase Advanced L2, Kraken L3, settlement index, exact-13M,
and the V13 monitor were all connected/alive; settlement freshness was 1.0,
exact record failures were zero, and health reported the same 27/28 rows and
3/4 fully observed windows with readiness credit unchanged.  Focused tests
passed 60/60 and the final full suite passed 2,388 with 5 skipped in 215.62
seconds.  Durable audit artifacts are under
`reports/q15_rti_v13_feature_audit_live_integrity/`.

The first genuinely new post-restart close was then captured successfully:
7/7 quotes, cross-asset contexts, cross-venue contexts, and spot contexts at a
0.2235-second exact-decision offset, with zero missed deadlines, write
failures, or collector errors.  The feature-only ledger and refreshed audit
now contain five executable windows / 35 rows, of which four windows / 34 rows
are fully observed.  The only degradation remains the original BNB row; the
new seven-asset window added no missing indicator or timestamp failure.  The
background `/api/health` cache then refreshed to the same five windows, four
fully observed windows, 34/35 fully observed rows, one original BNB
degradation, unchanged readiness credit, and no monitor error.

## Shipped 2026-07-22 - idempotent V13 PAPER readiness milestones

V13 now has an outcome-blind background milestone monitor.  It reads the same
feature-only SQL projection as preregistration, binds the V13 design plus
geometry/drift/walk-forward hashes, and fails closed on any timestamp,
coverage, label-access, fit, artifact, scoring, promotion, or trading flag.
It sends at most one durable Telegram administrative notice per frozen gate:
`GEOMETRY_30`, `NON_BTC_60`, and `BTC_150`.  Every message says `V3 V13`,
`PAPER ADMIN`, outcomes `SEALED / unread`, model fit/scoring not run,
artifact/promotion/trading disabled, and explicitly that it is not a trade
signal.  Static design-bound outbox keys make repeated checks and restarts
idempotent.

Runtime controls are `Q15_V13_READINESS_MONITOR` (default true) and
`Q15_V13_READINESS_INTERVAL_SECONDS` (default 300, clamped 60-3600).  The
monitor stays alive until all three gates are proven delivered and exposes
completed/pending milestones, counts, last status/error, and all safety flags
at `/api/health -> v13_readiness_monitor`.  The first deployed check saw three
clean V13 windows, no ready milestone, constructed no delivery, and reported
`WAITING_FOR_MILESTONE`; outcomes/scoring/promotion/trading were false.
WebSocket, exact capture, and settlement remained healthy with zero record
failures and 100% settlement freshness.

Tests cover feature-only loading, every unsafe flag, exact 29/30/59/60/149/150
boundaries, protocol-hash tampering, PAPER message text, durable outbox
deduplication, lazy sender construction, partial milestone completion,
monitor lifecycle/health, app health, and config documentation.  The final
full suite passed 2,386 tests with 5 skipped in 218.45 seconds.

## Frozen 2026-07-22 - V13 performance and economic reporting before labels

Before any V13 outcome label or performance metric was opened, its entire
future scorecard was frozen.  The subgroup protocol
`q15-rti-v13-fixed-subgroup-reporting-v1` has SHA-256
`ea4a273530cb2a807d091703703d594ec2e7923cfb173fa5acd3de4d13b2a823`.
It fixes slices by asset, stored point-in-time RTI side, absolute distance,
realized volatility, and broad-market regime.  Every observed slice must show
sample size, close-window count, accuracy, Wilson interval, market accuracy,
Brier score, log loss, trade frequency, 10-contract fee/slippage-adjusted P/L,
EV/trade, drawdown, and rejected-trade counterfactuals.  Non-executable rows
receive no P/L and counterfactuals can never be claimed as fills.  The three
known pre-V13 losses are outside the prospective boundary and cannot be used
to remove any V13 row or any future similar-looking loss.

The calibration protocol `q15-rti-v13-fixed-calibration-reporting-v1`, SHA-256
`cc7e8ddcca5d797d6a1407d3acc0b4d20eba02ee7d8897f9af6c346f6a1120ce`,
fixes six probability bins, equal row weighting, separate BTC/non-BTC cohorts,
ECE, maximum calibration error, bias, reliability, resolution, and the same
market-prior comparison.  The selectivity protocol
`q15-rti-v13-fixed-selective-value-curve-v1`, SHA-256
`848c0155bcae020c3daf1d7dc34cab2d3663b2f32ceca909b5be05e1f05a25e7`,
fixes EV thresholds 0/1/2/3/5/8/12 cents while preserving ask <=62c, spread
<=1.5c, depth >=10, official Kalshi fees, 2c slippage, and 10 contracts.  The
3c rule remains frozen; untouched-test curves cannot select another threshold
or promote anything.

The freeze/report implementation now supports this V13 chain and dynamically
labels the model source as V13.  A future compact-freeze crash was also fixed:
V12/V13 omit `spread_cents` from the learned feature list, so the freeze now
reads the preserved point-in-time quote field rather than indexing a missing
learned feature.  Stored RTI side is carried explicitly into examples.  Tests
cover hash tampering, cohort mixing, fake-fill guards, fee/slippage invariants,
stored-side integrity, calibration labels, selectivity monotonicity, and the
compact spread path.  Focused regressions passed 239; the final full suite
passed 2,360 with 5 skipped.  Live health exposes all three reporting hashes,
outcome access/scoring/notifications/trading remain false, and collection was
healthy at V11=71, V12=33, V13=2 with zero exact-capture failures.

## Frozen 2026-07-22 - V13 outcome-blind review gates before statistics

With exactly one clean V13 window available and before inspecting any V13
feature, correlation, chronological-split, outcome, or performance statistic,
two future reviews were frozen.  The 30-window geometry protocol is
`q15-rti-v13-outcome-blind-geometry-review-v1`, SHA-256
`550e8dfd3132712020aa90232dab97679cf209a8d5ba5438c6bd7d786b42605b`.
It requires complete same-close seven-asset windows, clean timestamps, finite
features, the cohort-conditioned input to be constant zero for BTC, no
high-correlation or signed-duplicate pairs, bounded condition numbers, full
active rank, and fixed fit-capacity floors.  Failures require manual diagnosis;
they cannot remove features, change thresholds, create another design, fit,
activate, promote, notify, or trade.

The mandatory 60-window volatility review is
`q15-rti-v13-outcome-blind-60-window-covariate-drift-v1`, SHA-256
`91589996d48ec047b74b5e8c25c4b92533b220dd4a41729cbc71f91aa14a5856`.
It keeps each close together, compares chronological 30-window halves within
separate BTC/non-BTC cohorts, pins standardized mean-shift, dispersion, missing
rate, and market-prior thresholds, and explicitly rechecks
`log1p_realized_volatility_bps`.  Drift remains report-only and cannot trigger
automatic normalization, tuning, refitting, activation, or promotion.

After both hashes were frozen, the one-window plumbing preview correctly
reported `WAITING_FOR_30_COMPLETE_WINDOWS` and
`WAITING_FOR_60_COMPLETE_WINDOWS`; BTC's conditioned feature had maximum
absolute value exactly 0.0.  It read no outcomes and performed no fit or
automatic change.  Live health exposes both protocol IDs, hashes, and separate
countdowns.  New protocol, tamper, boundary, synthetic pass/fail, drift,
ledger, health, and config regressions passed 192/192.  The service was
restarted cleanly with WebSocket connected, exact capture alive, zero record
failures, and settlement freshness 100%.

## Shipped 2026-07-22 - prospective V13 BTC-alias successor collection

The manually preregistered V13 successor is now an executable feature design,
but remains collection-only.  Its design is
`q15-rti-market-residual-cohort-conditioned-compact-v13`, SHA-256
`adc900b5882567446cb3d4a8f5fc0cb795e278dd38db2c6179e54cc83fc673ed`.
It changes exactly one V12 input: the structurally redundant
`cross_asset_btc_minus_non_btc_median_60s` is replaced with
`cross_asset_btc_minus_non_btc_median_non_btc_only_60s`, which is exactly zero
for BTC and preserves the original point-in-time value for non-BTC.  The other
19 feature formulas, training configuration, entry policy, and all source and
timestamp integrity requirements remain unchanged.  V11 and V12 remain frozen
parallel controls.

The design-bound evaluation protocol is
`q15-rti-v13-expanding-walk-forward-evaluation-v1`, SHA-256
`8abc35d34ca74bb70b2886913648c6ff4189ba9427eed825b79dddc5955b490c`.
It retains separate BTC/non-BTC cohorts, same-close fold isolation, expanding
walk-forward validation, clustered bootstrap gates, sealed untouched tests,
and market-prior fallback.  No V13 outcome label has been read and no fit,
artifact, score, alert, promotion, or order path exists.

The charter's first candidate close at 1784737800 was conservatively excluded:
its exact 13-minute row arrived seconds before the executable design and
protocol were finalized.  The executable boundary is therefore stricter than
the charter: after close 1784737800, first eligible close 1784738700.  At
16:34 UTC the first post-freeze seven-asset window was live and complete with
zero unavailable rows, zero unusable windows, and zero timestamp failures.
Health reports V13 1/60 non-BTC readiness windows and 1/150 BTC windows while
V12 remains at 31 and V11 at 69.  WebSocket and settlement feeds were healthy,
settlement freshness was 100%, exact capture recorded all seven assets, and
record failures were zero.

Tests explicitly pin both hashes, reject boundary rows, verify BTC maps to
zero while non-BTC preserves the original gap, ensure exactly one V12 feature
changed, validate the walk-forward binding, and keep every runtime authority
off.  Focused design/protocol tests passed 73; runtime/ledger tests passed 190;
the final full suite passed 2,351 with 5 skipped in 214.05 seconds.

## Frozen 2026-07-22 - V12 30-window review and V13 successor charter

The first outcome-blind V12 review ran at exactly 30 complete eligible windows.
The 12:02 ET capture recorded all seven assets with zero write failures and
fresh settlement coverage.  No outcome, fit, artifact, notification, or trade
was used.  Non-BTC geometry is healthy: 19 active inputs, full active rank,
condition number 6.99, 9.47 observed rows per active input, and no pair at or
above 0.95 correlation.  BTC retained the preregistered structural alias:
`target_minus_cross_asset_median_momentum_60s_bps` versus
`cross_asset_btc_minus_non_btc_median_60s` correlated 0.987797.  All other
fixed geometry checks passed.  Evidence is in
`reports/q15_rti_v12_feature_audit_30/audit.json` (SHA-256
`957d935bbd9ea3d87fa6dd3a69cc6f5a0eb5d6508f5bc9ecefd0ee212db90ca3`).

The frozen covariate review also flagged a real regime shift in
`log1p_realized_volatility_bps` for both cohorts.  It remains report-only: the
feature was not removed, normalized, tuned, or outcome-conditioned.  A repeat
review at 60 V13 windows is mandatory.  V12 remains frozen, collection-only,
and 30 windows short of its non-BTC locked audit; no V12 label has been read.

A diagnosis restricted to V11's already-opened 48 train/calibration windows
compared its 71-feature control with the independently frozen 20-feature V12
algebra.  The sealed V11 test contributed zero rows.  Compact V12 reduced the
Brier degradation versus market from +0.0001800 to +0.0001020 and log-loss
degradation from +0.0004105 to +0.0002262, but still did not beat the market.
A nested chronological safe-blend selected residual factor 0.0 in all three
outer folds, correctly falling back to the point-in-time market prior.  This is
development evidence only and gives V12 no historical credit.  The durable
diagnostic is `reports/q15_rti_v11_opened_fold_diagnostic.json` (SHA-256
`9989855a72fa5aefd62a0f3e4ea22bde27175b61f28e09c2e714860bcb511323`).

The predeclared BTC alias trigger was therefore manually frozen as the V13
successor charter `q15-rti-v13-btc-alias-successor-preregistration-v1`, SHA-256
`f55e3772f4b6bced8a2315c94d007bf35eac05b38a27391e071d4dd570abae78`.
Its only proposed feature change is a cohort-conditioned replacement that is
zero for BTC and preserves the original gap for non-BTC.  The prospective
boundary includes every reviewed close (after 1784736900; first eligible close
1784737800).  V13 is deliberately not executable yet: it still requires a new
design manifest, feature identity, and design-bound walk-forward protocol.
V11/V12 remain frozen controls, historical credit is forbidden, and every
activation/notification/trading flag is false.  Charter/diagnostic safety tests
passed 28/28.

## Audited 2026-07-22 - V11 non-BTC rejected before untouched test

The requested first locked V11 audit ran after 66 complete design-eligible
seven-asset windows were available.  The frozen sample used exactly the
earliest 60; assets in a close stayed together and BTC remained excluded.  A
readiness mismatch was found and fixed before scoring: `prepare_unlabeled_examples`
had omitted the same design-scoped model-feature coverage used by the frozen
preregistration CLI.  The old source-history gate therefore rejected a valid
design cohort even though no eligible feature/timestamp failure existed.  The
fix aligns those two gates without changing the design, sample, thresholds, or
folds.  Regression coverage proves the design-scoped gate is supplied.

Eleven train/calibration rows in three old close windows were then found
ungraded.  The authoritative interval ledger already contained unanimous
official results for every exact ticker, but the source-version-scoped side
handler had skipped some rows.  Contract-scoped, idempotent settlement grading
and a bounded startup reconciliation were added.  The one-shot repair matched
41 of 50 pending RTI contracts and graded 444 PAPER side-ledger rows; nine
unmatched contracts remain unresolved rather than guessed.  No trading or
notification decisions are affected.  The repair path accepts only explicit
YES/NO results that are unanimous for the exact ticker.

V11 then failed its preregistered expanding walk-forward gate.  Across 24
chronological validation close windows / 144 non-BTC rows it was 104/144
correct (72.22%, Wilson 95% 64.40%-78.89%), exactly matching market-direction
accuracy.  V11 Brier was 0.19934373 versus market 0.19916372 and log loss was
0.58549079 versus market 0.58508031, so both proper scores were slightly worse.
Fold accuracies were 77.08%, 81.25%, and 58.33%.  Because the walk-forward gate
failed, calibration/final fitting stopped, the 12-window untouched test was
not read, and no artifact, promotion, scoring service, alert rule, or trading
path was created.  Durable evidence is in
`reports/q15_rti_v11_non_btc_freeze_20260722/non_btc_transfer-report.json`.

An outcome-blind V11 readiness monitor is now part of the local app.  It sent
one idempotent `PAPER ADMIN` notice at the 60-window gate and stopped after
proven delivery.  Its health explicitly reports zero outcome access,
automatic scoring, promotion, and real trading.  Safety/idempotency, interval,
strategy-ledger, and freeze focused suites passed; the final full suite passed
2,341 tests with 5 skipped.  At 11:48 ET the exact sampler remained healthy and had
captured all seven assets in the first post-deploy window with zero failures.

## Shipped 2026-07-22 - frozen V11 volume-versus-value curve before outcomes

The first V11 test will now show the volume/accuracy/economics tradeoff without
using that same untouched result to choose a better-looking threshold.  Before
any V11 label review, `q15-rti-v11-fixed-selective-value-curve-v1` was frozen
with SHA-256
`7f50aa65edfc96ea5181a00114e0c0efb9a26e8eed41d55a1df2600e66b6ad35`.
It evaluates fixed post-fee/slippage EV thresholds of 0, 1, 2, 3, 5, 8, and
12 cents while holding ask <=62 cents, spread <=1.5 cents, displayed depth >=10,
ten simulated contracts, official Kalshi fees, and 2-cent slippage unchanged.
The existing 3-cent rule remains the frozen policy; the curve cannot replace it.

For every threshold the report includes decision-row coverage, picks per close,
accuracy, Wilson interval, ten-contract adjusted P/L, EV per trade, maximum
drawdown, and a digest of the exact selected row IDs.  The evaluator proves
that every higher-threshold pick set is a subset of the preceding set and that
pick counts never increase.  All rows use stored decision-time quotes and
depth, but the curve is explicitly a paper counterfactual and never a claim of
historical fills.  BTC/non-BTC mixing, threshold selection from the untouched
test, historical promotion, and automatic feature/rule/activation/notification/
trading changes are forbidden.  Any future threshold change requires a new
preregistered prospective challenger.

The one-shot state and future artifact/runtime health now require the exact
curve identity/hash and `fixed_selective_value_curve_required=true`.  The real
no-label preview at 08:49 ET advanced to 55 clean V11 windows (5 remaining to
non-BTC), with zero label reads, model fits, or artifacts.  Focused freeze/
runtime tests passed 71; broad RTI feature/freeze/runtime/strategy-ledger/app-
health verification passed 302.  Live health at 08:51 ET showed 98/98 exact
decisions with complete cross-asset evidence, zero misses/write failures, 17
clean V12 windows, connected settlement, fresh data, and Telegram configured
with dead-letter protection.  The service was not restarted because no active
scoring artifact exists and collection code did not change.

## Shipped 2026-07-22 - V11 fixed calibration reporting before label access

The first V11 score can no longer look convincing from accuracy and P/L while
hiding systematically overconfident probabilities.  Before the 60-window
non-BTC label gate, the report-only
`q15-rti-v11-fixed-calibration-reporting-v1` annex was frozen with SHA-256
`d10553be7b14c761934bfec82ccd5d87c7e859a4080828d2600deda4c691f27c`.
It evaluates every untouched-test probability before accepted-pick filtering
and compares V11 directly with the stored point-in-time Kalshi market prior.

Six immutable probability bins cover 0%-100% and always report empty bins.  For
both V11 and Kalshi the annex reports row count, mean probability, observed YES
rate, signed calibration bias, expected and maximum calibration error, binned
reliability/resolution, and outcome uncertainty.  It also reports model-minus-
market calibration deltas.  BTC and non-BTC cannot mix; historical calibration
is explicitly non-promotional, cannot change the deployment gate, and provides
no activation, notification, promotion, or trading permission.

The one-shot untouched-test reservation, finalized state, emitted artifact,
runtime scorer, and artifact health must carry the exact annex identity/hash
and `fixed_calibration_reporting_required=true`.  A missing or modified annex,
metric table, empty bin, comparison, or artifact guard fails closed.  The real
design-bound preview at 08:41 ET has 54 clean V11 windows (6 remaining to the
non-BTC gate) and exposes the exact calibration identity while confirming zero
label reads, zero model fits, and zero artifacts.  Focused freeze/runtime tests
passed 67; broad feature/freeze/runtime/strategy-ledger/app-health verification
passed 298 tests.  No live restart was needed because V11 has no artifact and
the scoring bridge remains dormant; collection continues unchanged.

## Shipped 2026-07-22 - preregistered V12 chronological covariate-drift audit

V12's feature-only audit now detects a failure mode that aggregate rank and
correlation checks cannot see: a feed or feature distribution shifting between
early and late forward collection.  Before inspecting any chronological split
statistics, `q15-rti-v12-outcome-blind-covariate-drift-v1` was frozen with
SHA-256 `ced627f34f7d50b8b9a9521bb5ce18bbae939a4008f5a6f37a2a24eda9a66211`.
It keeps every asset sharing a close in the same half and keeps BTC separate
from the six-asset transfer cohort.  At 30 complete windows it applies fixed
limits of 1.0 overall-standard-deviation for mean shift, 0.25x-4.0 for early/
late dispersion ratio, and 0.25 absolute rate shift for missing indicators;
the point-in-time Kalshi market prior is audited by the same mean/dispersion
rules.

Any partial close, timestamp failure, nonfinite value, one-half-only constant
feature, or threshold breach fails closed for manual root-cause review.  The
protocol is report-only: it cannot read labels, change a feature or threshold,
fit/refit, activate, notify, promote, claim historical credit, or trade, and it
keeps V11/V12 frozen.  Synthetic tests cover stable data, a regime shift,
missingness changing from 0% to 100%, partial-close leakage, timestamp failure,
and protocol-threshold tampering.

The first real preview was calculated only after the protocol was hashed.  At
16 V12 windows, the chronological 8-vs-8 split has zero preview breaches in
either cohort.  BTC's market-prior standardized mean shift is 0.560 with a
1.393 dispersion ratio; non-BTC is 0.122 and 1.043.  These are feed-stability
diagnostics, not predictive-performance evidence, and confirmed drift remains
false until the frozen 30-window gate.  Broad RTI feature/freeze/runtime/ledger/
health verification passed 294 tests.  Live health refreshed at 08:35 ET to
54 clean V11 windows (6 remaining to non-BTC), 16 V12 windows, 91/91 exact
decisions with complete cross-asset evidence, zero misses or write failures,
connected settlement, and Telegram with dead-letter protection.  The service
was not restarted because no live runtime path changed.

## Shipped 2026-07-22 - frozen V12 BTC geometry decision before 30-window review

The 15-window V12 forward feature-only preview remains outcome blind and
numerically healthy for non-BTC: 90 rows, 19 active inputs, full rank 19,
stable rank 4.39, condition number 12.63, projected 11.37 training rows per
active input, and no pair at |r| >= 0.95.  BTC has 15 rows, the maximum possible
centered rank 14, stable rank 2.95, condition number 20.80, and projected 4.74
training rows per active input.  It also exposes one credible structural risk:
`target_minus_cross_asset_median_momentum_60s_bps` and
`cross_asset_btc_minus_non_btc_median_60s` have correlation 0.989016 because
both subtract a broad-market center from BTC momentum.  No label, accuracy,
P/L, settlement, model fit, or artifact was inspected or created.

Before the frozen 30-window feature review, the separate report-only protocol
`q15-rti-v12-outcome-blind-geometry-review-v1` was preregistered with SHA-256
`8cab81cec789baf4a9bba316e84bfbb06c7ac6e2c747ed5589b10e9c69778aee`.
It pins the 30-window integrity, rank, condition-number, correlation, and
capacity checks.  If and only if the BTC alias remains at |r| >= 0.95 while
non-BTC has no high-correlation pair, the predeclared successor hypothesis is
to replace the regime-gap input with a cohort-conditioned version that is zero
for BTC and unchanged for the six transfer assets.  That action requires a new
manually frozen design, a prospective boundary after every reviewed close, and
zero historical credit; V11 and V12 remain controls.  The protocol cannot
create or activate the successor, read labels, fit, notify, promote, or trade.

Adversarial protocol-hash, wait/trigger/pass, feature-audit, freeze, runtime,
strategy-ledger, and app-health verification passed 291 tests.  The running
paper service was not restarted because only the offline audit tool and config
changed.  Live health at 08:27 ET showed 53 clean V11 folds (7 remaining to the
non-BTC gate and 97 to BTC), 15 V12 folds, 84/84 exact decisions with complete
cross-asset context, zero missed deadlines, zero write failures, fresh data,
connected settlement, and Telegram configured with dead-letter protection.

## Shipped 2026-07-22 - outcome-blind V11 historical-performance reporting contract

V11 can now produce the requested historical performance table without changing
its frozen model or inspecting outcomes early.  The separately immutable
`q15-rti-v11-fixed-subgroup-reporting-v1` manifest (SHA-256
`e4381605acf7039436813ea8feba78df3a3ccf15efa9c84b57a678bae3d98143`)
fixes the post-score breakdowns before any V11 outcome review: asset, RTI side,
absolute distance tier, realized-volatility tier, and cross-asset market regime.
It is report-only and cannot change model features, hyperparameters, entry rules,
deployment gates, notifications, promotion, or trading permissions.

The one-shot untouched-test path now requires and persists the exact reporting
protocol identity.  It validates that every observed subgroup contains all
required accuracy, Wilson-interval, fee/slippage-adjusted P/L, EV, and drawdown
metrics; that every row partitions exactly once in every dimension; and that
rejected-trade counterfactuals remain explicitly paper-only.  Counterfactual P/L
is only computed when the stored decision-time side quote and displayed depth
could support the ten-contract simulation, and is never claimed as a fill.
Future V11 runtime artifacts fail closed if this reporting lineage or safety
guard is missing or altered.

The real design-bound replay at 08:16 ET found 52 clean V11 close windows.  The
non-BTC cohort is eight windows short of its frozen 60-window label-access gate;
BTC is 98 windows short of its independent 150-window gate.  The replay read no
labels, fitted no model, and emitted no artifact.  Coverage is clean with zero
timestamp-alignment failures.  Seven unavailable rows are the single previously
audited seven-asset restart-gap window; older rows lack exact V11 evidence and
cannot be backfilled honestly.  Focused freeze/runtime/ledger/health verification
passed 214 tests.  Full repository verification passed 2,302 tests with 5 skips
in 342.03 seconds.  The local paper service remained running and was not
restarted.

## Shipped 2026-07-22 - exact-capture-safe local restart guard

The sole V11 unusable eligible close was reconstructed outcome-blind: all seven
rows at the 04:45 ET close failed only because both Coinbase and Kraken had a
simultaneous collector gap immediately before the 04:32 exact capture.  The
60-second BNB start snapshots were therefore more than the frozen 10-second lag
cap, and V11 correctly returned `cross_asset_status_not_ok`.  All subsequent
eligible windows were clean; there was no timestamp leakage or recurring feed
defect.

`Start-Q15Local.ps1` now refuses to stop a healthy running app during a
deterministic protected interval around the epoch-phase-120 exact capture.  The
window includes the required 60-second lookback, a 60-second observed restart
budget, a 10-second safety margin, and five seconds for capture commit.  A
stopped app still starts immediately; `-ForceUnsafeRestart` is explicit and
emergency-only.  Pure fixed-epoch tests cover both boundaries and the launcher
safety contract.  This changes no feature, model, threshold, label access,
notification, promotion, or trading permission.

Full repository verification passed with 2,296 tests and 5 skips.  The running
paper service was deliberately not restarted for this launcher-only change.
At 07:47 ET it had recorded ten consecutive post-restart seven-asset folds:
70/70 exact decisions, 70/70 cross-asset contexts, zero misses, zero write
failures, and zero missing cross-asset contexts.  Fresh health reported V11 at
51 clean windows (9 to non-BTC, 99 to BTC), V12 at 13 clean windows with zero
unavailable or unusable windows, 7/7 fresh settlement coverage, and Telegram
configured with dead-letter protection.

## Shipped 2026-07-22 - outcome-blind orthogonal compact V12 collection candidate

The 38-window outcome-blind V11 numerical review exposed a credible
overfitting risk before any labels were read: BTC had 61 active inputs, stable
rank 4.83, and only 1.48 projected training rows per active input at its frozen
150-window gate; non-BTC had 70 active inputs, stable rank 7.58, and 3.09
projected training rows per active input.  V11 and the strict control remain
unchanged.  A separate `q15-rti-market-residual-orthogonal-compact-v12`
candidate was preregistered at 04:40:11 ET with design SHA
`3f878d532c1ba6578e9eeb347ab2025b1c5a14c9ce27cd79a367016238ff854a`.
Its boundary excludes every inspected close through 04:45 ET and grants no
historical credit.

V12 uses a fixed 20-feature, domain-balanced projection spanning path/reversal
risk, Kalshi microstructure, external flow, independent-venue confirmation,
and cross-asset regime.  It removes asset identity dummies and the duplicated
market-prior magnitude, retains an explicit spot-flow missing indicator, and
replaces the collinear target-momentum/broad-momentum pair with
`target_minus_cross_asset_median_momentum_60s_bps`.  Replaying the pre-boundary
feature-only allow-list produced full 20/20 numerical rank in both cohorts,
zero pairs at |r| >= 0.95, no near-zero columns, projected training rows per
active input of 4.50 BTC and 10.80 non-BTC, and condition numbers 16.07 and
8.62 respectively.  This is geometry evidence, not predictive evidence.

The separate V12 walk-forward protocol was frozen at 04:54:47 ET before any
V12 outcome review.  Protocol SHA
`d69dcfd73805eda398040590eb1271df1e0644b316603eff694b0ddc19d47a4f`
retains same-close fold isolation, separate BTC/non-BTC folds, 5,000 fixed-seed
paired close-window resamples, fixed 0.001 Brier/log-loss effect floors,
one-shot untouched-test scoring, and manual-only promotion.  The first
genuinely prospective 05:00-close fold captured 7/7 assets with complete
cross-asset/cross-venue context, zero unavailable features, zero timestamp
failures, and no model fit or artifact.  V12 readiness is 1 clean window (59
to non-BTC and 149 to BTC); V11 concurrently advanced to 39 clean windows.
Runtime/ledger health now exposes V12 independently while keeping it silent,
paper-only, non-trading, and non-promotable.  Focused feature, readiness,
freeze, ledger, runtime, and health verification: 258 passed.  Full repository
verification: 2,287 passed, 5 skipped in 280.18 seconds.
The local paper service restarted at 05:04 ET.  Its durable snapshot
repopulated to 40 clean V11 windows and 2 clean V12 windows (58 remaining to
the non-BTC V12 gate and 148 to BTC), with zero V12 unavailable rows,
timestamp failures, or unusable windows.  Health returned OK with the exact
sampler alive, 7/7 fresh settlement coverage, fresh Kalshi/Coinbase/Kraken
feeds, the exact V12 design SHA/schema, and all V12 label/model/artifact/
notification/trading flags still disabled.

## Shipped 2026-07-22 - paired close-window prospective uncertainty gate

Future V11 prospective promotion can no longer treat the six non-BTC assets
sharing one Q15 close as six independent probability trials.  The V11-only
scorecard now computes deterministic model-minus-Kalshi Brier and log-loss
deltas after equal-weighting assets inside each close, then resamples entire
close windows together for 5,000 fixed-seed bootstrap draws.  It reports the
90% one-sided upper bounds plus central 90% intervals under the immutable
`q15-rti-paired-close-window-bootstrap-v1` protocol.  Duplicating every asset
inside a close leaves the observed deltas and uncertainty bounds unchanged.

The manual promotion gate now fails closed unless every prospective prediction
is paired to its stored decision-time Kalshi probability and both clustered
one-sided upper bounds demonstrate at least a 0.001 mean improvement in their
respective proper loss.  Point-estimate improvements alone, tiny negative loss
deltas, missing bootstrap evidence, altered resample counts/seeds/floors, and
fabricated pass flags cannot promote V11.  V2/V3 behavior, frozen strict rules,
features, model fitting, entry criteria, notifications, artifact activation,
and trading permissions are unchanged.

Adversarial verification covers deterministic replay, same-close row
replication, zero/tiny skill rejection, exact protocol tampering, and the
interaction with immutable cohort lineage.  Focused RTI/runtime/health/exact
verification: 184 passed.  Full repository verification: 2,282 passed, 5
skipped in 271.17 seconds.  The local paper service restarted at 04:31 ET and
returned healthy with every watchdog feed fresh, 7/7 settlement coverage, and
the exact sampler alive.  Its 04:32 fold recorded 7/7 parent decisions with
zero misses, exhausted retries, or write failures.  Because the restart was
only about 36 seconds before exact capture, the fold correctly failed closed
for missing 61-second cross-asset/cross-venue history and was counted as one
unusable window rather than training evidence.  The 38 prior clean windows
remain intact; the explicit audit still read zero labels, fit zero models, and
emitted zero artifacts.  Live health exposes the new 5,000-resample bootstrap
as unavailable/failing with zero prospective rows, while artifact availability,
V11 paper recording, notifications, promotion, and real trading all remain
disabled.

## Shipped 2026-07-22 - cohort-safe immutable prospective lineage gate

A future V11 paper book can no longer satisfy promotion criteria by pooling
rows from multiple model versions, artifacts, untouched-test states, or test-
metrics snapshots.  Every prospective record—including unresolved exposure—is
now assigned to its BTC or non-BTC transfer cohort and contributes to a durable
lineage summary.  Each cohort must have exactly one model version, one valid
artifact SHA, a matching stored cohort, and, for V11, one valid test-state SHA,
one valid test-metrics SHA, the finalized passing test status, and the exact
frozen design/protocol identities.  BTC and non-BTC may correctly use different
locked artifacts without contaminating one another.

The existing manual 30/60/150 review remains fail closed.  Positive fee-and-
slippage-adjusted P/L, a Wilson lower bound above the cohort break-even rate,
and proper-score improvement over Kalshi are no longer sufficient by
themselves: the cohort lineage gate must also pass.  Mixed lineage changes the
health status to `LINEAGE_INTEGRITY_FAILED_REVIEW_REQUIRED` and forces
`promotion_criteria_met=false`; automatic promotion remains impossible.
Adversarial tests prove that unresolved rows immediately expose an artifact
swap, that mixed BTC lineage fails, that an independently consistent non-BTC
artifact still passes its own lineage gate, and that otherwise excellent 30-
pick economics/proper scores cannot override a lineage failure.

Focused strategy and supporting verification: 215 passed.  Full repository
verification: 2,279 passed, 5 skipped in 281.23 seconds.  The live 04:02 fold
recorded another 7/7 exact captures and decisions with zero retries, misses,
exhaustion, or write failures and complete cross-asset, cross-venue, and spot
context.  The explicit outcome-blind audit advanced to 38 clean windows (22 to
non-BTC; 112 to BTC), with zero labels, test reads, fits, or artifacts.  The
paper service restarted at 04:08 ET; health returned OK, repopulated the 38-
window count, kept V11 record activation false and evidence rows at zero, and
reported fresh 7/7 settlement coverage.

## Shipped 2026-07-22 - dormant prospective V11 paper-ledger bridge

The future locked V11 artifact now has a complete prospective evidence path,
but that path is explicitly disabled by default through
`Q15_V3_RTI_MICROSTRUCTURE_V11_PAPER_RECORD=false`.  Merely deploying this code
does not call V11, add a challenger verdict to current rows, notify Telegram,
trade, promote, or grant historical credit.  After a cohort passes its locked
freeze and untouched-test gate, an operator must install the artifact and
separately enable this record-only flag.  Health exposes the flag and reports
`DISABLED_MANUAL_ACTIVATION_REQUIRED` until then.

Once manually enabled, the bridge stores the decision-time model version,
artifact SHA, finalized untouched-test state/metrics SHAs, exact V11 design and
walk-forward protocol hashes, cohort, prospective cutoff, probability, market
prior, OOD state, selected side, fresh quote, displayed depth, simulated fill,
official fee schedule, and 2-cent slippage assumptions inside the existing
idempotent RTI parent row.  It accepts only evidence after the stored cutoff
with a passing finalized test, exact lineage, valid probabilities, in-
distribution features, adequate depth/spread, and at least 3 cents expected
value after costs.  The derived V11 book reuses automatic official settlement
grading, reconstructs its own side/entry P&L, scores every stored probability
against the Kalshi prior, reports rejected counterfactuals, and remains manual-
review-only at 30/60/150.  It has no notification or order surface.

The V11 design/protocol identity was moved to a dependency-free immutable
module so rules and feature construction share the same constants without a
circular import.  Focused strategy/runtime/ledger/health/freeze/exact
verification: 213 passed; the final focused integration run was 163 passed.
Full repository verification: 2,277 passed, 5 skipped in 320.05 seconds.  The
outcome-blind audit advanced to 37 clean windows (23 to non-BTC; 113 to BTC),
with zero label/test reads, fits, or artifacts.  The paper service restarted at
03:41 ET; health returned OK, settlement coverage was fresh 7/7, the durable
V11 count repopulated to 36, both artifacts remained missing, record activation
was false, and the new V11 scorecard contained zero evidence rows.  The first
post-deploy 03:47 fold captured and durably recorded 7/7 exact assets with zero
retries, misses, exhaustion, or write failures, 7/7 cross-asset, cross-venue,
and spot context, a 0.624-second maximum offset, and 0.128-second-old settlement
data.  All 21 delayed decisions completed with zero retries, misses, failures,
or pending work.  Record activation and V11 evidence rows both remained zero.

## Shipped 2026-07-22 - crash-safe one-shot untouched-test lineage

The locked RTI freeze can now survive a process crash after its one allowed
untouched-test score without ever reading those labels a second time.  Its
exclusive reservation and finalized result use a self-hashed
`q15-rti-untouched-test-state-v2` record bound to the exact design, cohort,
train/test data fingerprint, fitted-model fingerprint, chronological test-fold
geometry, prospective boundary, fee/cost versions, and V11 walk-forward
protocol.  A finalized pass stores the full test metrics and gate before an
artifact is built.  A later run reconstructs the byte-identical paper artifact
from that state and deterministic training evidence without calling either the
test-label reader or test-settlement checker.  A finalized rejection is also
recoverable without a rescore.  An incomplete reservation remains permanently
ambiguous and fails closed; it can never become an automatic second attempt.

Future V11 artifacts additionally carry the finalized test-state version/SHA,
test-metrics SHA, and exact passing status.  The dormant runtime rejects
missing, malformed, altered, or rejected lineage.  No prediction feature,
training hyperparameter, entry threshold, notification rule, activation state,
or trading permission changed.  Focused adversarial verification: 48 passed,
including identical crash recovery, rejected-state recovery, raw-state
tampering, rehashed model-binding tampering, and ambiguous-reservation refusal.
Full repository verification: 2,273 passed, 5 skipped in 374.11 seconds.

Both real design-bound V11 freeze previews remained outcome blind and advanced
to 35 complete executable windows: 25 remain to the non-BTC gate and 115 to the
BTC gate.  They read zero labels, performed zero fits, and emitted zero
artifacts.  The local paper service restarted at 03:05 ET.  Its cold health
cache repopulated the existing 34 durable windows, service health returned OK,
the exact sampler was alive with no errors or pending work, and settlement-index
coverage was fresh 7/7 with sub-second messages.  The first post-restart 03:17
fold then captured and durably recorded 7/7 exact assets with zero retries,
misses, exhausted retries, or write failures; cross-asset, cross-venue, and spot
contexts were 7/7, maximum timing offset was 0.218 seconds, and the settlement
message was 0.406 seconds old.  All 21 delayed 30/60/90-second captures and
decisions also completed with zero retries, misses, failures, or pending work.

## Shipped 2026-07-22 - signed optimizer numerical-integrity evidence

The fixed V11 residual-logit optimizer now records the exact regularized
objective consistent with its existing gradients: close-window-weighted log
loss plus the frozen L2 penalties on weights and bias.  It records the initial
and final objective, objective improvement, final maximum absolute gradient,
iterations, learning rate, L2, residual scale, and finite/not-worse proof flags.
Fitting fails closed on nonfinite inputs/weights/gradients or a final objective
worse than initialization.  A future locked artifact must carry the exact
optimizer identity/config and internally consistent finite objective evidence;
the dormant runtime rejects missing, altered, worsening, negative, or
arithmetically inconsistent claims.  The update does not change any feature,
gradient, iteration, hyperparameter, label boundary, probability, entry rule,
notification, or trading permission.

Focused freeze/runtime verification: 41 passed, including deterministic
objective reduction, NaN rejection, and rejection of fabricated optimizer
improvement.  Full repository verification: 2,266 passed, 5 skipped in 275.39
seconds.  The paper service restarted at 02:41 ET.  Its first 02:47 fold
recorded 7/7 exact captures and durable decisions with zero retries, misses, or
write failures, 7/7 cross-asset, cross-venue, and spot contexts, a 0.267-second
maximum offset, and 0.366-second-old settlement messages.  Explicit V11 audit
readiness advanced to 33 clean windows (27 to non-BTC freeze; 117 to BTC
freeze), with zero label reads, model fits, or artifacts.

## Shipped 2026-07-22 - signed equal-window training invariant

The preregistered V11 `window_equal_weighting=true` rule is now executable and
artifact-bound instead of implicit.  Fitting fails closed if the flag is not
exactly true or if the derived row weights do not sum to one inside every close
window.  The fitted model records deterministic weighting diagnostics.  A
future locked artifact must additionally prove the exact cohort geometry: BTC
90 train windows / 90 rows / one row per window; non-BTC 36 train windows / 216
rows / six rows per window.  The dormant runtime rejects any altered row count,
window count, per-window weight, total sample weight, or verification flag.
This changes no feature, label, threshold, hyperparameter, model fit, alert, or
trading permission; it prevents the six non-BTC assets in one crypto-wide move
from silently receiving six times a BTC window's training weight.

Focused freeze/runtime/output-integrity verification: 43 passed, including
invariance when every within-window row is duplicated and rejection when equal
weighting is disabled.  Full repository verification: 2,263 passed, 5 skipped
in 280.31 seconds.  The paper service restarted at 02:28 ET.  The first 02:32
fold froze all 7 quotes with zero retries/misses, then durably recorded all 7
decisions with zero failures and 7/7 cross-asset, cross-venue, and spot
contexts; maximum offset was 0.493 seconds and settlement messages were 0.031
seconds old.  The explicit design-bound V11 audit advanced to 32 clean windows
(28 to non-BTC freeze; 118 to BTC freeze), still with zero label reads, model
fits, or artifacts.

## Shipped 2026-07-22 - 30-window V11 review and exact retry starvation fix

The first explicit V11 outcome-blind feature review is complete.  The 02:02 ET
fold raised the design-bound audit to 30 clean executable seven-asset windows
with zero unavailable feature rows, timestamp failures, nonfinite values, or
exact duplicate feature pairs.  It read no outcomes, fit no model, emitted no
artifact, made no design change, sent no notification, and had no trading or
promotion authority.  Non-BTC has 180 rows across 30 independent close windows,
70 active features, numerical rank 67, stable rank 7.34, one |r| >= 0.95 pair,
and projected locked-fit capacity of 216 train rows / 3.09 rows per active
feature.  BTC has only 30 independent rows, 61 active features, rank 29, stable
rank 4.51, and ten high-correlation pairs; it is still plainly sample-starved,
which confirms rather than weakens the separate 150-window BTC gate.  Both
locked freeze previews remained `WAITING_FOR_COMPLETE_WINDOWS` and proved zero
label/test reads, fits, or artifacts.

The preceding 01:47 ET fold exposed and correctly rejected an operational
failure instead of being credited as window 30.  DOGE and BTC froze at +0.027s
and +0.048s, but five other books entered quote retry.  The exact worker then
started slower spot/path/enrichment work and did not revisit those retries
inside the two-second deadline, producing 2 captures / 5 explicit misses.  The
sampler now drains every due primary quote retry before any spot, path, SQLite,
or Telegram work.  Injected-time tests retain their deterministic one-attempt-
per-tick behavior.  Health additionally reports retry-drain cycles, the recent
missed/exhausted tickers, and the last failure reason per ticker.  The incomplete
01:47 fold remains permanently excluded; there is no backfill or historical
credit.

Focused exact/health verification: 29 passed.  Full repository verification:
2,260 passed, 5 skipped in 279.12 seconds.  The paper service restarted with the
fix at 02:07 ET.  Its first post-fix 02:17 fold recorded 7/7 captures and 7/7
decisions, zero retries/misses/write failures, 7/7 cross-asset, cross-venue, and
spot contexts, a 0.426-second maximum offset, and 0.008-second-old settlement
messages.  The explicit V11 audit then advanced to 31 clean windows (29 remain
to non-BTC freeze; 119 to BTC freeze).  The one-time review heartbeat was
deleted after completion.

## Shipped 2026-07-22 - design-bound audits and compact V11 readiness

All version-sensitive RTI research directories are now exclusively bound to
one immutable design ID/SHA in `design-binding.json`.  Existing legacy JSON is
adopted only if every direct report already matches that design.  A different
design fails before it can reserve an untouched test, overwrite a report, or
emit an artifact.  JSON/Markdown reports and final test-state replacements are
written through same-directory, fsynced temporary files and atomically
replaced; an interrupted replacement preserves the last complete report and
cleans its temporary file.  The canonical feature-audit and freeze-preview
directories are bound to V11.  A real V4-to-V11 overwrite attempt exited 1
with `output_directory_design_binding_mismatch`; the V11 report SHA-256 stayed
byte-identical before/after.

Live health now has a compact `v11_collection_readiness` headline that accepts
only the exact V11 design/schema hashes and paper-only/outcome-blind guards.
It derives 30/60/150 remaining windows from executable V11 folds rather than
trusting a reported counter, and collapses to zero/unavailable on identity,
safety, or numeric tampering.  It is health-only and cannot influence feature
construction, qualification, notifications, promotion, or trading.

Focused output-integrity verification: 69 passed.  Focused compact-health
verification: 30 passed.  Full repository verification after the final
runtime change: 2,259 passed, 5 skipped in 248.81 seconds.  The live 01:17 ET
fold was uninterrupted and recorded 7/7 more exact rows with zero misses or
failures, advancing explicit V11 readiness to 28 clean windows.  The paper
service restarted at 01:20 ET, health returned OK, settlement indexes were
fresh 7/7, and the new headline reported 28 executable windows, 2 to the first
feature review, 32 to non-BTC freeze, 122 to BTC freeze, zero unavailable rows,
zero timestamp failures, no model/artifact, and no notification/trading
authority.

## Shipped 2026-07-22 - fail-closed V11 artifact bridge and audit targeting

Added a dormant, paper-only runtime bridge for the future separately locked
BTC and non-BTC V11 artifacts.  It has no activation, notification, order,
automatic-refit, or automatic-promotion surface.  Artifact loading now binds
the exact V11 design/schema/evaluation-protocol hashes, exact training and
entry-cost policies, the exact training-data hash in `model_version`, the
prospective boundary, same-close fold isolation, completed walk-forward and
one-shot-test guards, finite 71-column parameters, and zero weights on
inactive features.  A cohort is part of the cache key, so a validated BTC file
cannot be reused as a non-BTC artifact through cache state.  Live health shows
both expected cohort files as `WAITING_FOR_LOCKED_ARTIFACT`, paper-only,
notification-ineligible, trading-disabled, and manual-activation-required.

The generic preregistration, feature-audit, and freeze CLIs now require an
explicit `--design` manifest.  This removes the stale-default failure mode
that briefly made a V4 source-window count look like V11 readiness.  The
authoritative explicit V11 audit is at 27 clean executable seven-asset
windows, zero unavailable rows, and zero timestamp failures: 3 windows to the
outcome-blind feature review, 33 to the locked non-BTC freeze, and 123 to the
locked BTC freeze.  No V11 label was read, model fitted, artifact emitted,
notification sent, or trade authorized.

Focused runtime, CLI-safety, feature-audit, freeze, preregistration, and health
verification: 86 passed.  Full repository verification: 2,251 passed, 5
skipped in 245.88 seconds.  The local paper service restarted at 00:54 ET;
health returned OK, the exact sampler thread was alive with zero failures,
settlement indexes were fresh 7/7 with sub-second messages, Telegram remained
configured, and both dormant V11 cohort health records bound the frozen design
and protocol hashes.  The first post-restart 01:02 ET fold then captured and
recorded all 7 assets with zero misses/failures, 7/7 cross-asset, cross-venue,
and spot contexts, a 0.269-second timing offset, and 0.102-second-old settlement
messages.  Explicit V11-only audit/freeze commands remained outcome blind,
read no labels, fit no model, emitted no artifact, and reported 27 windows.

## Shipped 2026-07-21/22 - RTI execution-cost integrity and nonblocking audit

The frozen strict control and every prediction rule remain unchanged.  The
paper simulator now has one versioned Q15 execution-cost implementation using
the official 2026-07-07 Kalshi quadratic taker fee, the cohort's declared
10-contract size, and the required 2-cent-per-contract adverse slippage.  The
simulated fill is quote plus slippage and the fee is calculated at that fill.
Resolved rows are dynamically reconstructed from immutable side, result,
entry, and cost evidence; missing or contradictory evidence fails promotion
closed.  Same-side challenger overrides always use the challenger's own quote,
so a delayed challenger cannot silently reuse its parent entry.  Health now
reports cost-evidence completeness, label-integrity failures, unscoreable
resolved rows, fee/cost model versions, and audited drawdown/P&L.

The RTI health audit was separated into a compact decision-book read and a
full exact-feature read.  On the same frozen 5,379-row database snapshot, the
optimized report was exactly equal to the prior report (matching canonical
SHA-256) while build time fell from 20.094 seconds to 6.957 seconds.  The live
stale-while-revalidate endpoint returns the warm health snapshot in about
1.4-2.2 seconds instead of blocking on the rebuild.  Focused verification:
144 passed.  Full repository verification: 2,233 passed, 5 skipped in 276.13
seconds.  The local service restarted successfully after this verification.

At 00:32 ET, V11 had 25 complete executable seven-asset windows, zero feature
unavailable rows, and zero timestamp-alignment failures.  It remains outcome
blind with no fitted model, artifact, notification, promotion, or trading
authority.  Five windows remain to the first 30-window feature review; the
locked cohort gates remain 60 non-BTC and 150 BTC windows.  The currently
notifying impulse paper challenger is only 1/2 resolved, -$2.1388 at 10
contracts, with complete cost evidence; this tiny result is not evidence of an
edge.  Settlement-index health was connected and fresh for 7/7 assets with
sub-second messages.  The restart landed just after the 00:02 exact deadline,
so the sampler correctly recorded seven deadline misses instead of accepting
late evidence.  The next eligible 00:17 ET fold then recorded 7/7 exact
captures and 7/7 decisions with zero write failures, 7/7 cross-asset, 7/7
cross-venue, and 7/7 target-spot contexts, no current error, and a 0.303-second
maximum timing offset.  The refreshed health snapshot was served in 0.222
seconds.

The outcome-blind V11 numerical preview was refreshed at 25 windows.  It read
zero outcome labels, fit no model, emitted no artifact, and found zero exact
duplicate pairs or nonfinite values.  BTC currently has 61 active features,
rank 24, stable rank 4.60, and nine |r| >= 0.95 pairs; NON_BTC_TRANSFER has 70
active features, rank 67, stable rank 7.62, and one |r| >= 0.95 pair.  The
projected preregistered training capacity at the locked gates is 1.48 BTC and
3.09 non-BTC rows per currently active feature, so the fixed heavy L2 penalty,
separate cohorts, proper-score walk-forward gate, and market-prior fallback
remain mandatory.  This preview is not a performance claim and cannot alter
V11.  Refreshed artifacts: `q15_rti_v11_feature_audit/`.  Both V11 locked
freeze previews returned `WAITING_FOR_COMPLETE_WINDOWS` while proving zero
label reads, zero untouched-test reads, zero fits, and zero artifacts.

Before any V11 outcome review, the separately hashed evaluation protocol was
hardened to `q15-rti-v11-expanding-walk-forward-evaluation-v2`, SHA-256
`04600797bfbb2170c36972a32c40a4acccba52df37804f79a45b454603c1408b`.
Every walk-forward aggregate, calibration segment, and one-shot untouched test
now reports a deterministic 5,000-resample paired bootstrap over whole close
windows (90% confidence, fixed seed).  Same-close non-BTC assets are resampled
together, so one crypto-wide move cannot masquerade as six independent wins.
Both the Brier and log-loss model-minus-market one-sided upper bounds must
clear a preregistered 0.001 absolute improvement floor; a merely negative or
microscopic delta fails closed to the Kalshi market prior.  This changes no V11
feature, model family, hyperparameter, prospective boundary, notification, or
trading permission.  Protocol tests include deterministic replay, identical
results after sixfold same-window replication, tamper rejection, no-skill
rejection, microscopic-skill rejection, and preservation of untouched-test
secrecy.  Focused freeze verification: 23 passed.  Canonical readiness reports
in `q15_rti_v11_freeze_preview/` bind protocol v2 and still prove zero label
reads, fits, artifacts, or test access.

Full repository verification after protocol-v2 bootstrap/effect-floor
hardening: 2,236 passed, 5 skipped in 267.61 seconds.  During that run the
00:32 ET live fold recorded another 7/7 exact decisions with zero write
failures and 7/7 cross-asset contexts; settlement messages were approximately
0.04 seconds old and V11 advanced to 25 clean windows.

## Shipped THIS session - cross-asset regime RTI v11

Frozen V10 remains unchanged.  New outcome-blind, paper-only design
`q15-rti-market-residual-cross-asset-regime-v11`, SHA-256
`e4a5d65485d7559e2eaa84a82d1aeca63f1f87a42914916438af9034c79b0480`,
adds eight compact features that distinguish broad crypto-complex momentum
from isolated asset moves: robust 15s/60s median momentum and signed breadth,
60s cross-sectional MAD, target relative rank, BTC-versus-alt momentum, and
target/BTC direction agreement.  This is a pre-outcome mechanism hypothesis,
not a fitted or retrospectively selected result.

The collector reconstructs all seven assets on both Coinbase and Kraken using
only local rows at or before the immutable exact cutoff.  It persists all 28
venue/asset/horizon moves, the 14 corresponding consensus moves, and source
timing maxima. V11 recomputes each consensus and every selected derivative
before use.  Any missing asset,
future endpoint, stale transport, timestamp contradiction, or arithmetic
mismatch fails the whole fold closed.  Seven-asset live reconstruction takes
about 0.062 seconds.  All 71 audit fields are present in the durable ledger.

V11 receives no credit through the 17:00 ET close (`1784667600`); first
eligible capture is 17:02 ET for the 17:15 close (`1784668500`).  It cannot
read labels, fit, notify, promote, or trade.  Focused collector, runtime,
manifest, feature, freeze, health, and strategy verification: 230 passed.
Full repository verification after the final V11 venue-level audit hardening:
2216 passed, 5 skipped in 274.54 seconds.  The local paper-only service
restarted cleanly with the health-cache hardening at 16:53 ET.  The deliberately non-eligible 17:00-close
rehearsal fold captured all seven assets with cross-asset status `ok`, seven
available assets, approximately 2.3-2.6 second worst-source age, and all 71
feature values passing consensus/derivative recomputation.  No outcome was
read and the rehearsal receives no prospective credit.

The first eligible 17:15-close fold captured at 17:02 ET is clean: all seven
assets produced 71 features, zero feature-unavailable rows, zero timestamp
failures, and no arithmetic mismatches.  Worst cross-asset source age was
approximately 4.53-4.81 seconds (within the unchanged 10-second cap);
Coinbase was approximately 1.07-1.35 seconds and Kraken 4.53-4.81 seconds.
Readiness is 1 complete window (non-BTC 59 remaining, BTC 149 remaining).
Durable artifacts: `q15_rti_v11_preregister.json` and
`q15_rti_v11_feature_audit/`.

Operational health was also hardened: the expensive RTI scoreboard is cached
by exact mutation generation plus SQLite `data_version`, invalidates on every
RTI insert/grade, and returns deep copies.  Direct cached scoreboard latency
fell from about 22.35 seconds to 0.011 seconds; live cached `/api/health` is
about 1.6 seconds after warm-up.  This does not alter any prediction or
collector evidence.

Follow-up 17:26-17:33 ET: exact-sampler health now exposes separate durable
`cross_asset_ok` / `cross_asset_missing` counters, including the no-reader
fail-closed path.  The focused RTI/strategy/health suite passed 157 tests and
the full repository suite passed 2216 with 5 skipped in 297.42 seconds.  The
paper-only stack restarted at 17:26 ET.  Its first post-restart exact capture,
at 17:32 ET for the 17:45 close, recorded all seven assets with 0 missed
deadlines and 0 write failures; capture offset was at most 0.210 seconds.
Cross-asset context was `ok` on 7/7 rows with no missing V11 evidence and a
4.94-second worst source/message age (inside the frozen 10-second cap).  The
target-specific HYPE primary spot snapshot was stale on that fold, so the
older target cross-venue diagnostic correctly reported 6 ok / 1 missing;
Coinbase and Kraken point-in-time evidence themselves were `ok` for HYPE.
V11 remains outcome blind and now has 3 complete executable seven-asset
windows, 0 unavailable rows, and 0 timestamp failures (BTC 147 windows and
non-BTC 57 windows remain before locked freezes).  It still has no fitted
model, notifications, promotion, or trading authority.

The latest HYPE target-spot miss was audited against the immutable cutoff and
was correct, not a collector fault: the last OKX book change was 2.367 seconds
old just before 17:32, outside the frozen 2-second target-spot rule, and the
next fresh update arrived after the decision timestamp.  No threshold or
feature was loosened and no future row was reused.  Separately, the durable
outcome-blind numerical audit is now
`q15-rti-microstructure-feature-audit-v2`.  In addition to constants,
pairwise correlation, and exact duplicates, it standardizes and recenters the
feature matrix, computes its singular spectrum, numerical/stable rank,
multivariate rank deficiency, and the preregistered train-row capacity per
currently active feature.  This closes a real overfitting-observability gap
without touching V11's frozen hash or reading labels.  The 3-window preview
projects about 1.64 BTC and 3.09 non-BTC training rows per currently active
feature at the locked gates, so regularization and untouched-test fallback
remain essential; these are preliminary geometry diagnostics, not performance
claims or a basis for automatic feature changes.  Updated artifact:
`q15_rti_v11_feature_audit/`; 96 probability/microstructure/preregister/freeze/
cross-asset tests pass.  Full repository verification after the audit-v2
change: 2217 passed, 5 skipped in 297.80 seconds.  The 17:47 ET exact capture
advanced V11 to 4 complete executable windows (26 remain to the outcome-blind
first feature review), still with 0 unavailable rows and 0 timestamp failures.
Live health at 17:50 ET: service `ok`, exact thread alive, 14/14 post-restart
asset captures recorded, 0 missed deadlines, 0 write failures, cross-asset
context 14 ok / 0 missing, and all settlement feeds fresh.  No V11 outcome was
read, model fit, artifact, notification, promotion, or trading permission was
created.

Follow-up 19:21-19:22 ET: V11's one-shot freeze now has a separately
preregistered expanding-window evaluation protocol at
`config/q15_rti_v11_walk_forward_protocol.json`, ID
`q15-rti-v11-expanding-walk-forward-evaluation-v1`, SHA-256
`d7b15d9e241ad00df6a2d716417e13effc12b641e1f0eef11b79879cbcb7a0df`.
It does not change V11's design hash, features, fixed hyperparameters, entry
policy, or prospective boundary.  Before the original calibration gate or
one-shot untouched test can run, non-BTC must pass three expanding folds with
24/32/40 prior train windows and the next 8-window validation block; BTC must
pass 60/80/100 prior train windows and the next 20-window block.  Every fold
and the aggregate must be no worse than the point-in-time Kalshi market prior
on both Brier and log loss, with strict aggregate improvement on both.  A
failure prevents the final model, calibration gate, test-label read, and test
reservation.  Same-close assets remain inseparable, cohorts remain separate,
temporary fold models cannot become artifacts, and the protocol fingerprint
is hard-pinned.  Readiness-only freeze reports are durable in
`q15_rti_v11_freeze_preview/` and prove 0 label reads, 0 fits, and 0 artifacts.
Focused protocol/freeze/preregister/audit/health verification: 66 passed;
full repository verification: 2223 passed, 5 skipped in 285.00 seconds.

V11 has now accumulated 10 complete executable seven-asset windows, with 20
remaining to the outcome-blind feature review, 50 remaining to the non-BTC
freeze, and 140 remaining to the BTC freeze.  The refreshed 10-window geometry
has no exact duplicate features; BTC has 61 currently active features and a
projected 1.48 train rows per active feature, while non-BTC has 70 and 3.09.
These are feature-only capacity diagnostics, not performance.  Live health at
19:22 ET remains `ok`: 56/56 post-restart exact asset captures recorded, 0
missed deadlines, 0 write failures, 56 cross-asset contexts ok / 0 missing,
and all settlement feeds fresh.  V11 outcomes remain unread.

Follow-up 19:49-19:50 ET closed a latent outcome-leakage channel in the
offline audit/freeze path.  The SQL column allow-list had excluded
`official_result`, `correct`, and P/L columns, but it still selected the entire
`threshold_json` object, which contains display-only historical
`resolved_accuracy`, `resolved_correct`, Wilson, and net-P/L fields.  Current
V11 builders did not reference them, but that made the no-outcome boundary
depend on implementation restraint.  Feature tooling now has an explicit
425-key decision-time profile allow-list.  The normal CLI query projects only
approved JSON paths inside SQLite, coalesces approved persisted feature
columns, reconstructs the 21 legitimate base/path fallbacks, and never returns
the raw JSON blob to Python.  Programmatic callers are sanitized again before
coverage, feature construction, or freeze logic.  Nested adversarial
accuracy/result/P&L poisoning produces the identical feature audit, and the
durable reports explicitly record `feature_profile_allow_list_enforced=true`
and `raw_threshold_json_selected_by_cli_loader=false`.  All frozen V1-V11
lineages remain readable; V11 retained every eligible fold.  Focused leakage,
freeze, preregistration, and audit tests: 63 passed.  Full repository:
2225 passed, 5 skipped in 292.10 seconds.

V11 now has 12 complete executable seven-asset windows (18 to the feature-only
review, 48 to non-BTC freeze, 138 to BTC freeze), with 0 unavailable rows and
0 timestamp failures.  Live health at 19:50 ET: 70/70 post-restart exact asset
captures recorded, 0 misses, 0 write failures, 70 cross-asset contexts ok / 0
missing, all settlement feeds fresh, paper-only.  No V11 label, fit, artifact,
notification, promotion, or real-trading permission exists.

## Shipped THIS session - compact outcome-blind RTI v10

The formal V8 first-feature review completed at 30 prospective seven-asset
windows.  It read no outcomes, fit no model, and found exactly two duplicate
feature pairs: `kalshi_queue_pressure_yes_5s` equals
`kalshi_book_delta_pressure_yes_5s`, and the corresponding 30-second fields
are also identical.  Audit artifacts are in `q15_rti_v8_feature_audit/`.

Frozen V9 remains unchanged.  New paper-only design
`q15-rti-market-residual-independent-microstructure-compact-v10`, SHA-256
`bc329e1a563a3bb5d7e703ad9584c076bf7ab12db1ce0bc791a1699eaf1a47ce`,
removes only the two proven duplicates.  Every other V9 value is preserved by
name, reducing 65 columns to 63 with no information loss.  No settlement
labels, fitting, historical credit, notification, promotion, or trading are
allowed.  V10 receives no credit through the 16:15 ET close
(`1784664900`); its first eligible window is the 16:30 close, captured at
16:17 ET (`1784665800`).  V9 remains the frozen control.

The local service was restarted at 16:09 ET and is paper-only.  Coinbase L2,
Kraken L3, and all seven settlement-index feeds are connected and fresh;
Coinbase and Kraken are both pinned to top-10 depth at five-second archival
cadence.  V10's first eligible 16:30-close fold was captured at 16:17 ET:
all seven assets were executable, with zero unavailable rows and zero timestamp
failures.  Readiness is therefore 1 clean fold (BTC 149 remaining, non-BTC 59
remaining).  `q15_rti_v10_preregister.json` and
`q15_rti_v10_feature_audit/` are durable, outcome-blind artifacts.  The focused
V10/runtime/manifest suite passed 220 tests; the full suite passed 2208 with 5
skipped in 244.24 seconds.

## Shipped THIS session - independent top-10 venue microstructure RTI v9
Run time: 2026-07-21 08:09-15:51 ET.

Frozen outcome-blind paper design
`q15-rti-market-residual-independent-microstructure-v9`, SHA-256
`d57bb2455f94d1d5fbb75873da23804f78a42137177f3411ac47adab69466f58`.
It extends V8's 53 fields with 12 scale-stable independent-venue signals:
mean/disagreement/change in Coinbase/Kraken depth imbalance, mean/max spread,
mean log top-10 depth notional, venue depth divergence, Coinbase removal share,
Kraken delete share, observable partial-fill aggressor imbalance/notional, and
an explicit partial-fill-observed indicator. Total feature count is 65. It is
paper-only, reads no labels, performs no fit, emits no artifact, cannot notify,
and cannot trade. V4-V8 remain frozen controls.

Kraken's official L3 contract says `modify` reports remaining quantity after a
fill, while `delete` can mean either full fill or cancellation. The collector
now records only the positive prior-minus-remaining quantity from `modify` as
partial-fill flow. Ambiguous deletes are never fabricated as signed flow. Rows
carry source schema `kraken-l3-partial-fill-flow-v1`; V9 fails closed on any
other schema. Independent venue microstructure rows use
`rti-independent-venue-microstructure-v2` and persist 67 raw/derived audit
fields as first-class ledger columns.

The pre-eligible source audit caught a structural comparability problem:
Coinbase archived 250 levels while Kraken exposed 10. Both are now pinned to a
persisted top-10 basis at five-second cadence. Coinbase projected archive
growth fell from roughly 1.5 GB/day to 216.87 MB/day. Seven-asset reconstruction
took 0.077s in the benchmark. The first attempted 16:00-close fold then failed
closed because the depth-limit fields were computed but omitted from the
durable allow-list. It received no credit. The allow-list and ledger migration
were fixed, all 67 fields were verified present, and the boundary was advanced
again: no credit through the 16:00 close; first eligible close is 16:15 ET
(exact evidence at 16:02).

Preregistration readiness now scopes integrity to each design's eligible model
windows. Historical partial windows remain reported but cannot poison every
future design forever; timestamp corruption inside an eligible window still
blocks readiness. The test suite explicitly covers this distinction.

V8 reached 29 complete prospective windows during the work, with zero
timestamp failures; it is one fold from the outcome-blind 30-window feature
review but remains below the 60-window model gate. Its 29-window preview found
two exact duplicate pairs: 5s and 30s queue pressure repeat the corresponding
book-delta pressure fields. Frozen designs are unchanged; this can only inform
a later preregistered compact challenger. Focused final persistence suite:
210 passed. Final full suite after the persistence correction: 2,204 passed,
5 skipped in 254.57 seconds.

## Shipped THIS session - independent-venue RTI v8 and fresh archive cadence
Run time: 2026-07-21 07:32-08:03 ET.

Frozen a new outcome-blind, paper-only design,
`q15-rti-market-residual-independent-venue-v8`, SHA-256
`823d70f8ff658a9476c535b8a1894b42cf6acd694c95db6daea7073a8295f709`.
V8 branches from frozen v5 and does not require the single primary spot path.
It requires both Coinbase L2 and Kraken L3 point-in-time histories plus the
settlement-index path. Its 53 fields are the 46 v5 controls plus independent
venue/index basis, venue-minus-index 60s momentum, venue consensus 15s/60s
momentum, momentum dispersion, direction agreement, and current divergence.
All current and 15s/60s endpoints must be at or before the exact evidence
cutoff and no more than 10 seconds old. V4-v7 remain untouched controls.

The first eligible 08:00-close fold was honestly rejected on all seven assets:
the local environment had explicitly archived Coinbase/Kraken summaries every
15 seconds, so Kraken start endpoints could exceed the frozen 10-second cap.
No model threshold was loosened. Both archive cadences were changed to five
seconds and the paper service restarted. The next 08:15-close fold was clean
for all seven assets: one complete prospective V8 window, zero timestamp
failures, zero sampler record failures/missed deadlines, and seven cross-venue
OK captures. Readiness remains `WAITING_FOR_COMPLETE_WINDOWS`: 59 non-BTC and
149 BTC complete folds remain before the separate locked-freeze minimums.
V8 reads no outcome labels, performs no fit, emits no artifact, sends no
notification, and cannot trade.

Focused V8 integration suite: 209 passed; app health suite: 8 passed. Final
full release suite: 2,189 passed, 5 skipped in 217.93 seconds. At 08:08 ET the
service was healthy, its cycle age was 0.07s, the settlement-index age was
0.46s, Coinbase/Kraken record ages were 1.38s/1.39s, and the exact sampler was
alive with zero record failures or missed deadlines.

The separate frozen-control audit has now reached 100 forward trades: 48/100
(48.0%, Wilson 38.5%-57.7%), versus a 62.19% average fee-plus-slippage
break-even rate; ten-contract net is -$141.93 and EV is -14.19c/contract.
This remains unacceptable. It is preserved as a control and is not used as a
claim for V8.

## Shipped THIS session - point-in-time cross-venue RTI v7 and feed isolation
Run time: 2026-07-21 06:08-07:31 ET.

Added `rti-cross-venue-consensus-v1`, which reconstructs Coinbase L2 and
Kraken L3 current/15s/60s prices using only locally created snapshots at or
before the immutable exact-13M source timestamp. It rejects stale current or
start endpoints, effective transport age beyond 10 seconds, crossed/invalid
books, missing primary spot paths, and future timestamps. The durable exact
row now stores both venue endpoints, ages, moves, consensus momentum,
direction agreement, momentum dispersion, current divergence, and the primary
venue residual as first-class ledger columns. The strict/v4-v6 controls were
not modified.

Frozen paper-only design `q15-rti-market-residual-cross-venue-v7`, SHA-256
`1711257f38333fc3075347002ef49ba2fa7e860118507f9df0751acbb0c3658f`.
It preserves all 53 v6 features and adds seven compact signals: 15s/60s
cross-venue consensus momentum, primary-minus-consensus 60s momentum,
log momentum dispersion, 60s direction agreement, log current divergence,
and primary-versus-consensus basis. No outcomes were used. It gives no credit
through the 06:30 close; first eligible close is 06:45. It cannot fit, notify,
promote, or trade before the locked manual process.

Prospective evidence honestly exposed a source bottleneck: first v7 folds were
unavailable rather than silently credited when Coinbase/Kraken persistence or
the primary spot book missed frozen freshness limits. Root cause for much of
the lag was synchronous multi-gigabyte SQLite/WAL work on the websocket
asyncio threads while holding live-book locks. Coinbase L2, Kraken L3, and
spot-depth recorders now run disk writes off-loop and release their book locks
before insert/commit. The first post-fix fold had six of seven assets fully
clean; BNB remained rejected because its OKX source timestamp was 2.37s old
against the unchanged 2.0s primary-book cap. No threshold was loosened.

Current outcome-blind coverage: v4=15 complete folds, v5=9, v6=3, v7=0;
all have zero timestamp failures. V7 has four schema-eligible windows and 17
unavailable asset rows. The preregistration gate is
`WAITING_FOR_COMPLETE_WINDOWS`, with zero labels read, fits, or artifacts.
The frozen strict control is now 45/95 (47.37%, Wilson 37.63%-57.31%),
-$141.97 at 10 contracts after official fees plus 2c slippage, so it remains
unacceptable and is not being represented as recovered. Final release suite:
2,186 passed, 5 skipped. The local paper service was restarted at 07:15 ET and
remains the active collector.

## Shipped THIS session - point-in-time spot/index lead-lag and RTI v6
Run time: 2026-07-21 05:38-06:06 ET.

Added a local spot-mid path to `spot_depth.py`. The recorder retains 180
seconds of five-second mid snapshots per asset and the exact sampler now
freezes 15s/60s path count, start/end timestamps and prices, maximum gap,
change/range/realized-volatility bps, and trend efficiency. Completeness fails
closed unless the path reaches the real window start, has enough samples, has
no continuity gap beyond twice the configured cadence, and ends at the exact
current capture. Future rows are excluded. Health exposes per-asset path rows,
history seconds, schema `spot-mid-path-local-v1`, and local-created-at timing.

The exact parent combines that path with its independent 61-second settlement
index path to freeze current/start spot-index basis, basis change, index and
spot momentum, and spot-minus-index momentum under schema
`rti-spot-index-lead-lag-v1`. It independently rechecks capture/evidence order,
60-second history and retention, start/end alignment, and continuity before
granting `status=ok`. All fields persist as first-class strategy-ledger columns.

Frozen the separate paper-only design
`q15-rti-market-residual-lead-lag-v6`, SHA-256
`67ee276a06d7a03ad177560a439ce7dda7bcacd8a6c35f33fb7ed310b699256f`.
It keeps the 46 frozen v5 fields and adds only seven compact signals: current
spot/index basis, 60s spot-minus-index momentum, spot momentum at 15s/60s,
log spot range/realized volatility, and spot trend efficiency. Model settings
are unchanged from v5 so any later comparison isolates the lead-lag block.
V6 gives no credit through the 06:00 close and first accepts the 06:15 close;
all v1-v5 designs remain frozen controls.

The first eligible 06:02 capture was clean on all seven assets: both spot
horizons complete, 14 samples per 60s path, maximum gap 5.252s, about 183s of
history, valid lead-lag status, and finite basis/momentum values. All 7 exact
parents and 21 delayed stages recorded with zero misses or failures. V6
readiness is 1 clean fold, zero unavailable rows or timestamp failures, with
59 non-BTC and 149 BTC folds remaining. Its numerical preview read no labels
and had zero nonfinite values; a locked-freeze dry run left an adversarial
label reader uncalled and emitted no fit or artifact.

Collector/exact/ledger/design focused suite: 216 passed. Full release suite:
2,178 passed, 5 skipped. The final service restarted at 06:05 ET; health is OK,
WebSocket connected, exact sampler alive, and reports v4=10, v5=4, v6=1 clean
folds. V6 is not notification eligible, cannot trade, and has no performance
claim.

## Shipped THIS session - prospective RTI dynamics v5 design
Run time: 2026-07-21 04:28-05:29 ET.

Frozen a new secondary, paper-only design,
`q15-rti-market-residual-dynamics-v5`, at SHA-256
`1d773697299d67caf136ec3cfc3a8563298e1a48dc36c54f63d8c9ee4e287316`.
It preserves v4 as the accumulating primary control and compresses the
additive dynamics extension into 14 scale-stable signals: normalized YES queue
pressure at 5s/30s and its acceleration, microprice velocity/acceleration,
signed microprice efficiency and range, trade-price velocity/acceleration,
signed trade efficiency, current-microprice versus fresh 30s trade VWAP,
trade/update activity share, and explicit VWAP missingness. Together with the
32 frozen v4 features the vector has 46 fields.

No outcome was read to choose or validate these features. The design forbids
all extension rows at or before close time 1784625300 and sets the first
eligible close to 1784626200 (05:30 ET), ensuring its exact-13M evidence was
captured after preregistration. It requires extension-v1 on source-v2, local
receive time, no count cap, complete 5s/15s/30s/60s histories, and fresh VWAP
whenever trades exist. All earlier v1-v4 manifests and their interpretations
remain unchanged.

Preregistration, locked-freeze, and numerical feature-audit tooling now support
v5 explicitly. Ledger and runtime health expose v5 separately as the next
preregistered design, with no label read, model fit, artifact, notification,
automatic refit/promotion, or real-trading path. The service restarted at
05:26 ET. At 05:27 health was OK, WebSocket connected, exact sampler alive,
v4 had 7 clean folds, and v5 had its first complete seven-asset fold with 59
non-BTC and 149 BTC folds remaining. A feature-only audit found zero nonfinite
values; single-fold correlations were treated as uninterpretable and did not
change the frozen design. A locked-freeze dry run returned
`WAITING_FOR_COMPLETE_WINDOWS` while an adversarial label reader remained
uncalled: no train/calibration/test labels, model, or artifact were accessed.
Focused v5, freeze, ledger, and health regression: 185 passed.
The first post-restart fold then recorded 7/7 exact parents and all 21 delayed
stages with zero misses, failures, or retry exhaustion. V4 advanced to 8 clean
folds and v5 to 2, with zero v5 unavailable rows, unusable windows, or
timestamp failures. Final release suite: 2,169 passed, 5 skipped.

Final 05:38 performance snapshot for the unchanged strict control is 44/94
(46.81%, Wilson 37.05%-56.82%) and -$145.61 at 10 contracts with official
fees plus 2c slippage. Its latest 10 are 7/10 and +$5.83, but the latest 20 are
12/20 and -$7.26, so the short run is not evidence of recovery. V5 has no
predictions or performance claim; its folds are feature evidence only.

## Shipped THIS session - additive RTI dynamics capture without resetting v4
Run time: 2026-07-21 04:00-04:23 ET.

Added the parallel, outcome-blind extension schema
`rti-exact-microstructure-extension-v1` to source-v2 exact rows. It records
5s/15s/30s/60s YES/NO queue add/remove volumes, microprice change/range/total
variation/trend efficiency, and YES trade-price change/range/variation/trend
efficiency/VWAP. The extension is additive: frozen v4 does not consume these
fields, its accumulation was not reset, and no decision, alert, notification,
model fitting, artifact, promotion, or trading path uses the extension.

The WebSocket collector now records microprice after each book delta and
preserves the correct rolling baseline as old events leave retention. The
extension fails closed on wrong source schema, non-local time basis, count
caps, incomplete horizons, missing dynamics, and contradictory VWAP evidence.
Ledger and health coverage are reported by independent seven-asset close
window; the coverage path is explicitly outcome-blind.

The service was restarted at 04:11 ET. The first prospective extension window
at 04:17 ET captured all seven parent rows on source-v2 with complete 61/61
paths, complete 5s/15s/30s/60s histories, local receive timestamps, and no
count cap. All 21 delayed +30/+60/+90 captures followed with zero misses or
record failures. At 04:23 ET the durable ledger contained 3 complete source-v2
windows (21 rows), zero timestamp-alignment failures, zero partial same-close
windows, and 1 complete extension window with zero unusable or unavailable
extension rows. Frozen-v4 research therefore has 3 honest prospective folds;
the extension has only 1 and is not eligible for an efficacy claim.

Representative BTC extension evidence in the first window included 10,833
book events and 657 trades over 30 seconds, +1.095c microprice change, 10.542c
microprice range, 81.913c total variation, and 49.458c trade VWAP. These are raw
research measurements only, not selected predictors. Focused extension tests
passed 209 cases; the release suite passed 2,163 tests with 5 skipped, followed
by 37 focused eviction-state tests after the last test-only assertion update.

## Shipped THIS session - genuine Kalshi horizons and post-fix RTI v4
Run time: 2026-07-21 03:15-03:43 ET.

An outcome-blind collector audit proved the live BTC feed could exceed the old
5,000-row deque inside 30 seconds. The latest captured BTC row had exactly
5,000 events at both 30s and 60s, so those nominal horizons were truncated by
message count rather than genuine elapsed time. Frozen v1-v3 rows, manifests,
and designs remain unchanged and retain their original interpretation.

`q15_upgrade/ws_client.py` now retains order-book events for 90 seconds and
trades for 20 minutes by immutable local `received_at`, with no count cap.
Window membership uses the decision-available receive timestamp and rejects
future evidence. A book snapshot or any WebSocket reconnect starts a new
continuity epoch, clears pre-gap book/trade evidence, and forces every horizon
to warm again. Each 5s/15s/30s/60s output carries independent book, trade, and
combined completeness flags plus history start/age, retention, time-basis, and
count-cap evidence. Zero activity becomes neutral zero only after the relevant
window is known complete. Health now exposes live per-ticker buffer counts and
history ages under `kalshi_microstructure_history`.

New exact rows use source schema `rti-exact-microstructure-v2`; the persisted
columns include every continuity/completeness field. A new outcome-blind
preregistration, `q15-rti-market-residual-microstructure-v4`, is pinned at
SHA-256 `b2c240e2a29009b1475be79dd05631fb6ab4fa3bbe85fdc8a97ecf910b7cbee0`.
It has 32 features, accepts only source-v2/local-receive-time rows, proves the
history timestamps cover the claimed horizons, forbids count-capped or
incomplete evidence, and gives zero credit to all pre-fix v1 rows. V1-v3 are
still visible as frozen diagnostics; v4 is the new primary research design.

Both live preregistration and locked-freeze dry runs start at 0/60 non-BTC and
0/150 BTC folds and report no labels read, no model fit, no artifact, no
notification eligibility, and no trading path. Adversarial tests cover >5,000
events, receive-time rather than exchange-time membership, future evidence,
time pruning, reconnect invalidation/warm-up, source-schema isolation,
timestamp contradictions, incomplete horizons, and outcome-field poisoning.
Focused integration: 238 passed. A final absolute timestamp-integrity stop was
then added to both offline and live readiness: even 150 later clean folds cannot
hide a single timestamp-alignment failure. Its focused gate suite passed 158
tests. The final release suite passed 2,162 tests with 5 skipped.

The paper service was restarted at 03:42 ET. Startup health is OK, WebSocket is
connected, settlement coverage is fresh 7/7, exact sampler is alive with zero
misses/failures/error, live history reports `count_capped=false` and
`time_basis=local_received_at`, and v4 correctly remains at zero pre-fix folds.
The first untouched source-v2 exact capture at 03:47 ET passed end to end: all
7 parent rows had complete 61/61 RTI paths, fresh books and spot evidence,
receive-time continuity >106 seconds, complete 5s/15s/30s/60s windows, and no
count cap. BTC contained 3,579/8,149/20,485/34,471 real events across the four
horizons; its 30s/60s book pressures were independently 0.00141/0.00598. All
21 delayed +30/+60/+90 rows also arrived with complete 31/31, 61/61, and 91/91
paths, fresh executable books, and maximum timing offset 0.350s. Runtime health
shows 7 parent + 21 delayed captures, zero exact/delayed misses, record or
recovery failures, and zero retry exhaustion. V4 now honestly counts 1 clean
fold (59 remaining non-BTC, 149 BTC), with zero unavailable rows or timestamp
failures. Its outcome-blind numerical preview and freeze dry-run still read no
labels, fit no model, emit no artifact, and create no notification/trading path.
The final gate build was restarted at 03:55 ET; 03:59 health remained OK with
7/7 fresh settlement assets, receive-time/no-cap collection, one durable v4
fold, zero timestamp failures, and `timestamp_integrity_clean=true`.

## Shipped THIS session - outcome-blind numerical audit and de-duplicated RTI v3
Run time: 2026-07-21 02:31-02:54 ET.

Added `tools/q15_rti_microstructure_feature_audit.py`, an outcome-blind audit
of the exact feature matrix. It uses the same feature-only SQL allow-list and
executable seven-asset folds as the locked freeze, and reports nonfinite
values, constant/tiny-variance columns, feature scale, missingness, exact
signed duplicates, and correlations at |r| >= 0.95 separately for BTC and
non-BTC. It cannot read outcomes, fit a model, alter a design, emit an
artifact, notify, trade, refit, or promote. Adversarial tests prove outcome
fields cannot affect the report.

The first audit found that v1's 5s and 30s trade-imbalance columns were exact
duplicates of its same-horizon taker-imbalance columns. The pinned v1 manifest
was preserved. An outcome-blind v2 replaced those columns with 15s/60s book
pressure, but the next audit caught that BTC's 5,000-event capture cap made its
30s and 60s book-pressure histories identical. The pinned v2 evidence was
also preserved rather than silently changed. Raw feature-only checks showed
15s taker imbalance varied independently of the retained 5s/30s/60s flow and
15s book-pressure features.

The final primary preregistration is therefore
`q15-rti-market-residual-microstructure-v3`, SHA-256
`c7c33dd0e05b2ca3711c5ae0d097350a944b2602f18cf4d217c5b35b8409b3fc`.
It keeps 33 features and replaces only the bad 60s book-pressure column with
15s taker imbalance. The live v3 numerical preview has 27 executable
seven-asset folds / 29 schema-complete folds, 2 unusable stale-book folds, 4
unavailable rows, and zero timestamp failures. BTC and non-BTC both have zero
nonfinite values, zero tiny nonzero-variance features, zero exact duplicate
pairs, and zero |r| >= 0.95 active-feature pairs. BTC's constant spread,
asset-dummy, and zero-missingness columns are safely disabled later by the
pinned <=1e-8 standard-deviation guard; non-BTC has no constant columns.

Both v3 cohort freeze dry-runs returned `WAITING_FOR_COMPLETE_WINDOWS` with
`outcome_labels_read=false`, `untouched_test_labels_read=false`,
`model_fit_performed=false`, and `artifact_emitted=false`. No runtime model,
paper picks, notifications, or trading path were created. The first numerical
review is three clean folds away; locked modeling remains 33 folds away for
non-BTC and 123 for BTC. Focused RTI/health suite: 207 passed.

The 03:02 post-restart parent window then captured all 7 assets with complete
61-second paths, fresh spot evidence, quote ages 0.076-0.325s, timing offsets
0.052-0.168s, zero retries, zero misses, and zero record failures. Its +60 and
+90 checkpoints were also fresh, but all seven +30 quotes failed closed at
3.25-3.96s old after a heavy health request overlapped the deadline. No stale
fill was credited. The delayed sampler previously lacked the exact parent's
bounded retry path; it now retries stale/incomplete books within the genuine
two-second deadline, freezes the real successful retry timestamp, and persists
honest missing evidence if retries exhaust. Health exposes delayed retry
attempt/success/exhausted/pending counters. New stale-to-fresh and exhaustion
tests plus the RTI/runtime health set: 154 passed. Final full suite: 2,152
passed, 5 skipped.

The delayed-retry build was deployed at 03:10 ET. Post-restart health recovered
to 7/7 assets, no stale feeds, zero exact/delayed misses or record/recovery
failures, and the new delayed retry counters are visible at zero. Final 03:11
performance snapshot: strict forward control 40/87 (45.98%) and -$140.83;
guarded probability v3 value book 15/32 (46.88%) and +$16.08 at 10 contracts.
The v3 all-row scorecard is 116/185 (62.70%) but still worse than Kalshi on
Brier (0.22180 vs 0.21711) and log loss (0.63409 vs 0.62373), so it remains
silent, PAPER-only, and unpromoted.

At the status check immediately before this work, the frozen strict forward
control was 39/86 (45.35%) and -$144.37 per 10-contract simulation. Guarded
probability v3 was 14/31 (45.16%) but +$12.45 because its average fee+2c
break-even was 41.15%; its last five resolved accepted picks were wins, with
one HYPE YES pick for the 02:45 close then unresolved. Its all-row Brier and
log loss still trailed Kalshi, so it remains silent and unpromoted. Telegram's
latest messages were SENT on the first attempt; 47 reported dead letters are
historical. The alert-enabled impulse lane remains zero-volume.

> **OPERATIONAL NOTE (owner, 2026-07-08): the Repl is disconnected — the app now
> runs on the owner's LOCAL machine.** The local checkout pulls `main` and
> `tools/learning_export.py` still force-pushes `learning-snapshots` hourly, so
> the review workflow is unchanged. But "Stop ▸ Run on the Repl" advice is
> obsolete: deploys = local `git pull` + restart the local app process. Older
> references to "the Repl" in this file and CLAUDE.md should be read as "the
> local host".

## Shipped THIS session - all-evaluation probability audit and v3 forward diagnosis
Run time: 2026-07-21 00:34-00:50 ET.

Added durable `probability_scorecards` to the RTI challenger ledger and live
health. Unlike the value-book P/L view, these score every resolved prediction
with stored point-in-time evidence whether the trade was accepted or rejected.
They enforce `close_time > stored prospective_after_close_time`, never
recompute historical predictions, keep BTC and non-BTC transfer cohorts
separate, and report accuracy/Wilson intervals, Brier score, log loss,
calibration bias, Kalshi midpoint baselines, per-asset results, OOD/saturation
counts, and full seven-asset close-window completeness. Regression coverage
proves that rejected predictions still count, pre-freeze rows do not count,
unresolved rows do not leak labels, and v2/v3 artifacts remain isolated.

The first honest v3 all-row forward scorecard has 124 resolved predictions
across 18 chronological close windows (17 complete seven-asset folds): 79/124
(63.71%, Wilson 54.95%-71.64%), Brier 0.22142, and log loss 0.63378. Kalshi's
stored midpoint baseline is 65.32%, Brier 0.21179, and log loss 0.61193. Thus
v3 has -4.55% Brier skill and +0.02185 worse log loss; it is not a probability
improvement. The failure is concentrated in BTC: 10/18 (55.56%) versus the
market's 83.33%, Brier 0.20169 versus 0.16669, with an 18.13-point downward YES
bias. Non-BTC is 69/106 (65.09%) versus market 62.26%, but still has slightly
worse Brier (0.22477 versus 0.21944) and a 10.66-point downward YES bias.

The executable v3 value book is separately 8/22 and -$12.91 at 10 contracts.
BTC is 1/6 and -$13.63; non-BTC is 7/16 and +$0.72. These are accepted-trade
economics, not the all-row probability score. V3 remains frozen, silent,
PAPER-only, and unpromoted. V2 now also reports an explicit quarantined status
and `promotion_prohibited=true` in the research-book health view.

Focused RTI/health suite: 158 passed. Full suite: 2,109 passed, 5 skipped.
The service was restarted once at 00:48 ET. The exact 13M parent captures for
the active window were already durable and recovered, but the restart occurred
after the +90-second checkpoint; seven delayed +90 rows were correctly marked
missed rather than backfilled or fabricated. Main exact capture has zero misses
or record failures. Settlement coverage is 7/7 and Kalshi/spot/index feeds are
fresh. Verify the next untouched fold records all 7 parent plus 21 delayed
captures before treating post-restart delayed coverage as clean.

Follow-up 00:53-01:06 ET: four predeclared static v4 designs were evaluated
without changing v3: calibration-selected residual shrinkage with exact market
fallback, stability-gated shrinkage, market-side-only confirmation, and an
ambiguous-market-only residual. Each selected hyperparameters only on the
calibration period and then failed the locked final fold, especially BTC. A
triple-agreement trade filter also retained only 2/22 trades and lost $2.34 at
10 contracts. These are rejected diagnostics; no v4 artifact or live book was
created, and none receives prospective or promotion credit.

Promotion health now requires a probability-specific proper-score gate in the
same transfer cohort. At least 30 paired resolved predictions are required,
and both Brier score and log loss must strictly beat the stored Kalshi midpoint
baseline in addition to the existing resolved-trade, P/L, Wilson, and
fee+slippage break-even gates. V3 now correctly reports
`ACTIVE_PAPER_RESEARCH_SKILL_NOT_PROVEN` and `promotion_eligible=false` despite
being numerically valid. Current proper-score gate: 131 paired rows, Brier
0.22136 vs market 0.21322 and log loss 0.63365 vs 0.61504. BTC is especially
failed (Brier skill -22.19%); non-BTC is also negative (-1.44%).

The next untouched 01:15 close window was captured cleanly before deployment:
7 exact 13M parents plus 7 each at +30/+60/+90, all 61-second parents complete,
maximum parent evaluation delay 0.153s. The service was then restarted safely
between decision windows. Post-restart health has zero main/delayed misses,
zero recovery failures, 7 recovered parents/0 recovered stages, 7/7 fresh
settlement coverage, and fresh Kalshi/spot feeds. Focused suite: 159 passed.
Full suite: 2,110 passed, 5 skipped.

The outcome-blind feature-coverage audit had one additional audit defect: it
called 147 correlated per-asset rows "ready" even though they represented only
21 independent close windows. Readiness now requires complete seven-asset
chronological folds. Current state is 147 rows / 21 of 21 complete windows,
zero timestamp failures, zero partial folds, first feature review blocked until
30 windows, and modeling blocked until 60 windows. Focused suite after this
correction: 162 passed. Final full suite: 2,111 passed, 5 skipped.

Follow-up 01:16-01:29 ET: preregistered the outcome-blind replacement design
`q15-rti-market-residual-microstructure-v1` before model readiness. The exact
33-feature manifest, fixed residual-logit training configuration, 60/20/20
same-close fold split, entry economics, and calibration/test gates are pinned
to SHA-256
`a192895fa61bf365eff21062e47d9dbfd5674020f2fe7213ff85992dada67e61`.
Any later feature, threshold, cohort, cost, or safety change invalidates the
pin instead of silently changing the experiment. BTC requires 150 complete
seven-asset close windows; the separate non-BTC transfer cohort requires 60.
The readiness command cannot fit a model, emit an artifact, read outcome
columns, notify, trade, refit, or promote. The later executable-feature audit
found that schema completeness alone was insufficient: 4 of 182 rich rows had
no fresh executable Kalshi quote, affecting 2 of 26 otherwise complete folds.
Readiness therefore counts 24 fully executable seven-asset folds, not 26
schema-tagged folds: 36 remain for non-BTC and 126 for BTC. Timestamp failures
and partial/incomplete schema folds remain zero. Added adversarial tests for
manifest tampering, cohort boundaries, dirty folds, and a feature-only SQLite
database with no outcome columns. Focused RTI/health suite: 182 passed. Full
suite at that checkpoint: 2,125 passed, 5 skipped.

Follow-up 01:35-01:56 ET: added deterministic point-in-time construction for
the pinned 33-feature manifest and a fail-closed one-shot freeze command. The
command selects no settlement/P&L columns before readiness, deterministically
uses the first 60 non-BTC or first 150 BTC executable folds, reads only
train/calibration labels after readiness, requires strict Brier and log-loss
improvement overall plus non-worsening in both chronological calibration
halves, and cannot read the untouched test without an explicit confirmation.
An exclusive durable reservation prevents a second test score. The untouched
test additionally requires both proper scores to beat Kalshi, at least five
fee+2c-slippage picks, and positive 10-contract P/L before it can emit an
unconnected PAPER artifact. No runtime challenger, notification, order path,
or automatic promotion was added.

The four unavailable feature rows were genuine `book_stale` captures, not an
audit bug. The exact sampler now retries a stale/incomplete executable Kalshi
book inside the existing genuine two-second deadline, freezes the actual
successful retry timestamp, and never substitutes an old quote. If retries
remain stale it persists an honestly missing row before the deadline. Health
tracks retry attempts/successes/exhaustion. Focused RTI/health suite: 198
passed. Full suite: 2,141 passed, 5 skipped. Service restarted at 01:55 ET with
kill switches preserved; startup recovered to all seven registered assets and
fresh feeds. The first untouched retry-enabled fold at 02:02 ET captured all
seven executable books 0.017-0.106s after target, with maximum genuine quote
age 1.796s, zero retries needed, zero exact misses/failures, and no stale feed.

Follow-up 02:08-02:29 ET: live `/api/health` now exposes the same pinned
executable-fold readiness used by the offline gate: design ID/fingerprint,
feature count, schema vs executable windows, unusable windows/rows, timestamp
failures, cohort-specific 60/150 requirements, and explicit false flags for
outcome use, model fit, artifact emission, notifications, trading, refit, and
promotion. One early exact fold stored `evidence_as_of` only in its immutable
decision profile; the offline audit already recovered it, while the first live
health implementation did not. Both paths now use the same point-in-time
fallback and agree at 26 schema / 24 executable folds, 2 unusable stale-book
folds, and zero timestamp failures. The 02:17 fold was independently clean and
all +30/+60/+90 stages were durable before deployment. Service restarted at
02:23 ET and recovered to 7/7 assets with fresh feeds and zero misses/failures.
Targeted readiness/health suite: 168 passed before the profile fallback and
146 passed after it. Final full suite: 2,141 passed, 5 skipped.

## Shipped THIS session - v2 numerical quarantine, guarded probability v3, exact microstructure capture
Run time: 2026-07-20 19:33-20:13 ET.

The first v2 prospective close exposed a model-integrity defect before its
outcome was known. At the 19:47 ET decision for the 20:00 close, every non-BTC
row had raw YES probability 0.99 and the same calibrated YES probability
0.384517. The cause was an invariant historical
`kalshi_depth_ratio_missing` feature with a tiny nonzero std (~2.4e-15) and an
active weight. When fresh opposite depth became available live, its
standardized value exploded to ~4.1e14. The negative Platt slope then collapsed
all non-BTC ranks to one constant. V2's two accepted rows (BTC NO at 39c and
SOL NO at 52c) were still unresolved when this diagnosis and the replacement
rules were frozen. They later resolved 0/2 and -$9.84 at 10 contracts, but those
outcomes were not used to design v3.

V2 remains byte-for-byte frozen as a diagnostic control, but artifact health
now reports `QUARANTINED_NUMERICAL_OOD`, `promotion_eligible=false`, and the
offending cohort/feature/std/weight. Its paper decisions remain measurable; it
cannot notify, trade, or promote.

Added frozen `rti_probability_value_v3`, model
`rti-probability-shadow-v3-4492a3eb1e71`, artifact
`config/q15_rti_probability_v3.json`. It zeros features with std<=1e-8, clips
standardized inputs to +/-6, records the pre-clip maximum z-score, rejects an
entry above |z|=8, and constrains Platt calibration to a positive monotone
slope. Artifact validation refuses v3 artifacts without those guards. The
prospective cutoff is close time 1784592000 (20:00 ET), which includes every
decision row inspected during diagnosis even when its outcome was unresolved.
The v3 ledger starts at 0 and receives no v2 or historical credit.

Seen historical diagnostics only: BTC Brier 0.1923 versus market 0.1973 and
17 value picks at 9/17 / +$14.09 per 10-lot; non-BTC Brier 0.2117 versus market
0.2156 and 15 value picks at 6/15 / -$0.82. These folds cannot promote v3.
BTC/non-BTC reviews remain separate and manual at 30/60/150 prospective
resolutions. V3 is silent PAPER-only and has no order or Telegram path.

Also added `rti-exact-microstructure-v1` collection. Each exact row now freezes
Kalshi microprice, event/trade counts, taker YES/NO/net volume, directional book
pressure, trade imbalance, and best-level depletion/refill at 5/15/30/60s,
alongside the existing spot aggressive-flow fields. This does not change v2 or
v3 features; it creates honest point-in-time evidence for a later challenger.
`tools/q15_rti_feature_coverage_audit.py` never reads outcomes and checks exact
timestamps plus same-close asset completeness. First live rich window: 7/7
assets, zero alignment failures, zero partial folds, no backfill.

A dedicated ledger mapping now persists exact `rti_evaluated_at` as
`evidence_as_of`; the audit can recover pre-fix rows only from their immutable
stored threshold evidence. Focused suite: 158 passed. Full suite: 2,107 passed,
5 skipped. Service restarted at 20:13 ET: all seven RTI indexes fresh, Kalshi
websocket connected, exact thread alive, zero record failures, v2 quarantined,
v3 active with 0/0/0 evaluated/qualified/resolved, and no missed restart
deadline.

## Shipped THIS session - corrected RTI probability/value v2 PAPER challenger
Run time: 2026-07-20 18:53-19:32 ET.

The frozen exact-13M continuation control remains economically failed forward:
31/73 correct (42.47%, Wilson 31.78%-53.90%) and -$141.78 at 10 contracts
after official Kalshi fees plus 2c/contract slippage. BTC is 8/18 and -$30.36;
the non-BTC transfer cohort is 23/55 and -$111.42. Existing countertrend,
+30s, +60s, +90s, spot, wide-spread, and impulse books remain unchanged and
are not being promoted from small samples.

An adversarial orientation review found a research-only v1 defect:
`rti_market_mid_probability` is the selected RTI side's probability, but v1
treated it as a YES probability even when RTI side was NO. No live rule was
affected because v1 failed its holdout and was never deployed. V2 explicitly
converts a NO-side midpoint to `1 - p` before fitting/scoring and keeps BTC and
non-BTC transfer cohorts separate in every global chronological fold.

The corrected frozen artifact is `config/q15_rti_probability_v2.json`, model
`rti-probability-shadow-v2-d917c64faa3f`, with a strict prospective boundary at
close time 1784589300 (2026-07-20 19:15 ET). Its already-seen historical fold
is diagnostic only and receives no promotion credit. It suggested that the
useful target is executable value rather than blind RTI continuation: the
frozen scorer estimates final YES/NO settlement probability, evaluates both
fresh executable sides, subtracts official fees and 2c slippage, and selects a
side only when expected value is at least 3c/contract, ask<=62c, spread<=1.5c,
and displayed depth supports 10 contracts.

Added `rti_probability_value_v2` as a silent PAPER-only prospective challenger.
It records the artifact fingerprint, cohort, market/estimated probabilities,
selected side, exact entry quote/depth, fee, and expected value in the durable
decision ledger. It cannot notify or trade, cannot receive historical credit,
cannot mix BTC/non-BTC cohorts, cannot auto-refit or auto-promote, and requires
manual reviews only at 30/60/150 resolved picks. The frozen strict control is
unchanged. Regression tests cover YES/NO orientation, artifact boundary and
tampering, official-fee/slippage value selection, runtime persistence,
opposite-side quote grading, and notification isolation. Focused suite: 132
passed. Full suite: 2,098 passed, 5 skipped.

## Shipped THIS session - +90 stability challenger after observed +60 failures
Run time: 2026-07-20 18:45-18:52 ET.

Several hours of prospective collection moved the +60 continuation book from
1/1 to 1/3 and -$7.72 at 10 contracts. Both new losses were BTC and the failure
was reconstructed from the official per-second RTI feed. The 16:00 ET BTC NO
crossed against the pick 27 seconds after its +60 capture, crossed the strike
13 times, and its final-minute average settled 3.21 bps against it. The 17:30 ET
BTC YES crossed after 13 seconds, also crossed 13 times, and its final-minute
average settled 3.55 bps against it. The ETH winner's brief adverse crossing
did not occur until 60 seconds after the +60 capture and recovered.

Added the separate `rti_delayed_stability_90s_v1` PAPER record-only challenger.
It waits 90 seconds after the original exact 13M decision, freezes a fourth
genuinely new Kalshi quote/spot context, and requires an independent complete
91-second official RTI path. It uses the unchanged same-side, ask<=62c,
spread<=1.5c, depth>=10, and <=2s freshness gates. It cannot reuse the 13M,
+30s, or +60s quote; cannot notify or trade; claims no historical credit; and
starts manual review at 30/60/150 prospective resolutions. Because it was
selected after seeing the two +60 losses, it is explicitly marked as such and
no retrospective result is credited.

Scheduler recovery, durable interval identity (`11M30S`), matched parent
economics, confirmation ladder, lineage, rejected counterfactuals, and health
reporting now include +90. Focused suite: 143 passed. Full suite: 2,094 passed,
5 skipped. Service restarted healthy with all three delayed policies visible,
all seven RTI feeds fresh, websocket connected, and zero recovery failures.
Seven immediate active-window recovery rows failed closed because their +90
deadline had passed during restart; qualified/resolved remain 0/0 and no older
history was backfilled.

## Shipped THIS session - confirmation ladder, promotion guardrails, rejected probability model
Run time: 2026-07-20 07:30-18:44 ET.

Added a joint durable confirmation-ladder audit across each strict 13M parent,
its fresh +30s quote/path, its fresh +60s continuation quote/path, and the +60s
hard-flip book. Parent/ticker/asset/close lineage is validated and pre-policy
rows are excluded explicitly instead of mislabeled as broken links. The first
five common resolved parents produced: strict control -$0.65 at 10 contracts,
+30s policy -$5.87, +60s continuation +$4.13, and the post-flip-policy hard-flip
book +$3.93 on one taken BTC reversal. Those challenger results are each n=1
and remain mechanism proofs only, not improvement claims.

Every RTI research book now reports resolved/correct/accuracy, Wilson 95%,
fee-only and fee+2c-slippage break-even rates, canonical 10-lot P/L, maximum
drawdown, rejected counterfactuals, BTC/non-BTC cohorts, and manual 30/60/150
review progress. Cohort mixing and automatic promotion are forbidden. Live
health correctly refuses to promote the 1/1 hard-flip result: Wilson lower
20.65% versus 60.72% break-even, with 29 more resolved picks required before
the first manual review. Focused suite: 142 passed. Full suite: 2,088 passed,
5 skipped. Service restarted healthy; all seven official RTI feeds, Kalshi
websocket, exact sampler, restart recovery, and delayed recorder are live.

Built and froze a leakage-resistant market-prior residual-logit experiment in
`tools/q15_rti_probability_freeze.py`, with runtime-safe JSON feature/artifact
code in `q15_upgrade/strategy_bots/rti_probability.py`. It uses 800 genuine
point-in-time exact examples, keeps every same-close asset in the same global
chronological fold, fits BTC and non-BTC independently, uses train for weights,
calibration only for Platt calibration, performs no hyperparameter search, and
scores the final historical fold once. Missing historical opposite depth is an
explicit feature and forbids opposite-side simulated fills; it is never
invented. Artifact/report paths are `config/q15_rti_probability_v1.json` and
`work/rti-probability-freeze/`.

The honest holdout failed the trading bar. BTC improved Brier versus the market
midpoint (0.2509 vs 0.2667) but its executable policy was only 7/19 and -$1.97
at 10 contracts. Non-BTC worsened Brier (0.3112 vs 0.2502), was 11/37, and lost
$29.81. Therefore this artifact is preserved as rejected evidence but is NOT
connected to the live rule, Telegram, or a paper trade book, and it must not be
called an improvement. Five regression tests cover fold isolation, artifact
tampering, prospective-boundary enforcement, path features, and the
no-fake-depth rule. Final pre-+90 full suite: 2,093 passed, 5 skipped.

## Shipped THIS session - honest forward drift audit + 60s hard-flip book
Run time: 2026-07-20 06:55-07:29 ET.

Added `tools/q15_rti_forward_drift_audit.py` and durable reports under
`work/rti-forward-drift/`.  The audit admits only genuine exact records with a
coherent close-minus-780s capture timestamp, <=2s evaluation/quote freshness,
complete 61-second official RTI path, coherent settlement grade, and canonical
official Kalshi fee + 2c slippage economics.  Assets sharing a close cannot
cross the frozen/forward boundary.  Legacy one-decimal ledger P/L is tolerated
only within its historical rounding precision and is recomputed canonically.

Honest result at report generation: frozen control 7/10 (70.0%, Wilson
39.7%-89.2%, +$9.49 at 10 contracts) versus post-freeze 28/61 (45.9%, Wilson
34.0%-58.3%, -$98.74).  Two-sided Fisher exact p=0.1887: the economic failure
is real, but ten original picks were too few to prove a stable edge or a secure
regime break.  Both BTC (7/14, -$16.57) and non-BTC transfer (21/47, -$82.16)
failed fee/slippage break-even.  >=1bps 61-second momentum was especially bad
forward (12/37, -$112.54), the opposite of its tiny frozen result.  Because the
61 forward outcomes have now been inspected, they are diagnosis only and can
never promote a threshold or be called untouched.

Added the independent `rti_delayed_flip_60s_v1` PAPER record-only challenger.
It does nothing unless an originally accepted strict parent has crossed to the
opposite official RTI side at +60s.  It then grades that flipped side at the
genuinely new +60s opposite-side ask/depth, requiring the same <=62c,
<=1.5c-spread, depth>=10, exact-path, and <=2s freshness rules.  The quote side
must exactly match the flipped official side.  It cannot notify or trade,
forbids reuse of the 13M/+30s quotes, claims no historical credit, starts at
n=0, and has its own rejected counterfactuals, matched parent economics, and
manual 30/60/150 reviews.  The frozen control, +30s, and +60s continuation
policies are unchanged.

Focused suites: 133 and 138 passed.  Full suite: 2,087 passed, 5 skipped.
Service restarted at 07:29 ET; websocket, all official RTI feeds, exact thread,
restart recovery, and Telegram health are good.  The new flip book is visible
at evaluated=0 / qualified=0 / resolved=0 and notification-eligible=false, so
no old +60s rows were backfilled.

First prospective proof for the 07:45 ET close: 7/7 exact parents, 7/7 +30s,
and 7/7 +60s rows recorded; 14 delayed quotes, zero delayed misses/failures,
and no pending work.  The flip policy evaluated all seven and correctly
qualified zero because none satisfied every strict-parent + hard-flip gate.
An initial health report called three older strict-parent +60s rows "invalid"
only because they predated the flip policy and contained no flip decision.
The matched audit now distinguishes `pre_policy_parent_rows_excluded` from
malformed post-policy lineage.  After final restart: evaluated=7, qualified=0,
invalid links=0, pre-policy excluded=3, notification-eligible=false.  The
lineage correction has a regression test; final full suite remains 2,087
passed, 5 skipped.

## Shipped THIS session - independent +60s fresh-quote RTI challenger
Run time: 2026-07-20 06:02-06:13 ET.

The first matched +30s window showed the remaining failure honestly: BTC and
HYPE still confirmed after 30 seconds and then reversed.  The original research
plan had already pre-specified a 30-60 second confirmation experiment, so the
frozen +30s policy was not retuned from that loss.  Instead, a separate
`rti_delayed_confirm_60s_v1` PAPER record-only challenger now waits exactly 60
seconds after the 13M decision, captures a genuinely new executable Kalshi
quote and fresh spot context, and reads the independent 61-second official RTI
path.  It uses the same pre-registered ask <=62c, spread <=1.5c, depth >=10,
same-side, strict-parent, and <=2s freshness gates as +30s.

The +30s and +60s rows have distinct interval identities (`12M30S` and `12M`),
policy versions, qualification/rejection books, rejected-trade
counterfactuals, and matched parent-control economics.  Both remain silent,
paper-only, forward-only, ineligible for historical credit, and subject to
manual 30/60/150 reviews.  The frozen 13M control and Telegram routing were not
changed.  Scheduler regression coverage proves each stage uses its own quote
and path; the +60s fill cannot reuse the 13M or +30s price.

Focused RTI/ledger/health suite: 134 passed.  Full suite: 2,080 passed,
5 skipped.  Local service restarted at 06:12 ET; status, websocket, exact RTI
thread, and all seven official settlement feeds are healthy.  Health exposes
both policies.  The +60s book correctly starts at zero prospective examples;
no historical rows were backfilled.

First live proof for the 06:30 ET close: 7/7 exact parent rows, 7/7 +30s rows,
and 7/7 +60s rows were durably recorded.  The delayed scheduler captured 14
new quotes with zero deadline misses, zero record failures, and no pending
work; 13/14 optional spot contexts were fresh and one failed closed.  The +60s
book has seven genuine evaluations but zero qualifiers because this cohort had
no accepted strict parent.  It remains at zero resolved picks, zero historical
credit, zero invalid parent links, and cannot notify.

Delayed schedules are also restart-durable now.  On registration, a fresh
process performs a bounded read of the strategy ledger, validates the unique
exact parent and current per-asset model version, reconstructs its original
row ID/side/path endpoint, detects completed delayed intervals, and schedules
only missing stages.  Re-registration is idempotent; an existing parent
suppresses a false exact-deadline miss, while an overdue missing stage still
records explicit missing evidence and fails closed.  Recovery errors/counters
are visible in health.  After the final restart, all seven current parents were
recovered, both already-completed stages were correctly skipped (zero new
stages/duplicates), exact misses stayed zero, and recovery failures stayed
zero.  Updated focused suite: 136 passed.  Updated full suite: 2,082 passed,
5 skipped.

## Shipped THIS session - matched 13M vs +30s prospective economics
Run time: 2026-07-20 05:48-06:01 ET.

The RTI challenger scoreboard now pairs every +30s row to its durable strict
13M parent and validates parent ID, ticker, asset, close time, interval, and
accepted status.  It reports control P/L, delayed-policy P/L, incremental P/L,
saved losses, skipped winners, later-ask change, invalid links, BTC/non-BTC
cohorts, and reversal/settlement-risk groups.  A delayed rejection is modeled
as zero exposure; a delayed acceptance pays its genuinely later quote.  This is
paper-only reporting and changes no rule or notification.

First matched live result (06:00 ET close): all three strict YES parents (BTC,
ETH, HYPE) settled NO.  The control lost $18.21 at 10 contracts.  At +30s the
policy kept BTC YES 62c and HYPE YES 60c (both wrong) and rejected ETH at 66c /
2c spread, saving that one loss.  Delayed policy P/L was still -$12.93, but
incremental P/L versus control was +$5.28.  Average later ask was +5.67c overall
and +5.5c on trades actually retained.  This is damage reduction from one
window, NOT profitability evidence and not a basis for promotion or retuning.

Focused suite: 132 passed.  Full suite: 2,078 passed, 5 skipped.  Service was
restarted; live matched report showed 3/3 resolved pairs, zero invalid links,
one saved loss, zero skipped winners.

## Shipped THIS session - prospective RTI risk taxonomy
Run time: 2026-07-20 early morning session.

The refreshed leakage-aware audit covers 1,075 point-in-time examples / 160
chronological windows and still cannot promote a historical challenger.  Its
frozen control has only 10 historical qualifiers; high settlement-average-risk
rows were 1/3 and -$7.32 at 10 contracts, which is useful diagnostic evidence
but far too small and historically exposed for a gate.

Added `rti-point-in-time-risk-taxonomy-20260720-v1` as telemetry only.  Every
new exact 13M row now freezes reversal risk (low/medium/high), settlement-average
risk, path regime, Kalshi/RTI agreement, and explicit reason codes using only
decision-time evidence.  Missing inputs become `unknown`, never low.  These
labels cannot notify, trade, or change the frozen control and claim no
historical credit.  The V3 ledger automatically reports future outcome accuracy
and fee/slippage-adjusted counterfactual P/L by label for both all exact rows
and strict-control qualifiers.

Live proof for the 2026-07-20 06:00 ET close: 7/7 exact rows labeled under the
new policy, zero capture failures, one shared close-time cohort, and three
strict qualifiers (BTC YES, ETH YES, HYPE YES).  All three were medium reversal
risk; ETH was also medium settlement-average risk.  The +30s challenger then
accepted BTC YES at 62c and HYPE YES at 60c, while rejecting ETH at 66c / 2c
spread.  All are unresolved prospective observations.  The delayed scheduler
recorded 7/7 with zero failures/misses; one delayed spot context failed closed.

Focused RTI/ledger/health suite: 131 passed.  Full suite: 2,077 passed,
5 skipped.  Local service restarted; health, websocket, and settlement RTI are
connected.

## Shipped THIS session - true +30s RTI confirmation challenger
Run time: 2026-07-20 early session.

The prior `spot_book_confirm_v1` was same-timestamp context and therefore did
not satisfy the required 30-60 second confirmation experiment.  Added the
separate forward-only `rti_delayed_confirm_30s_v1` PAPER research book.  It
keeps the frozen 13M control untouched, waits exactly 30 seconds, captures a
new Kalshi websocket quote and fresh spot snapshot, reads the independent
31-second official RTI path, and prices its simulation at that later ask.  It
requires an originally accepted strict-control row, the same RTI side at +30s,
ask <=62c, spread <=1.5c, displayed depth >=10, and <=2s quote/path timing.
It cannot notify or trade, claims no historical credit, and remains subject to
manual 30/60/150 reviews.

The exact scheduler now freezes every asset's Kalshi quote before making any
spot-venue call at both the 13M and +30s stages.  Delayed accepted and rejected
rows are durable and automatically settlement-graded; health includes the
complete rejection funnel plus rejected-trade counterfactual accuracy/P&L so
saved losses and skipped winners remain visible.

Live proof for the 2026-07-20 03:15 ET close: 7/7 13M parents and 7/7 delayed
rows recorded, zero exact/delayed record failures, zero delayed deadline misses,
and 7/7 fresh delayed spot contexts.  Delayed quote offsets were +0.019s..
+0.226s, RTI evidence completed by +0.334s, and storage by +0.538s.  Each quote
was genuinely new roughly 29.95-29.98s after its parent; examples were BTC
77c->63c and XRP 73c->57c.  All seven parents were strict-control rejections,
so the delayed book correctly recorded 0 qualifiers and 7 rejected
counterfactuals for this first cohort.

Focused RTI/ledger/health suite: 130 passed.  Full suite: 2,076 passed,
5 skipped.  Local service restarted and health/websocket/Telegram are OK.

## Shipped THIS session - RTI prospective failure audit + countertrend research
Run time: 2026-07-19 late session.

The first real audit of `impulse_strength_v1` found 531 exact evaluations and
zero qualifiers.  Feed infrastructure was healthy (521 fresh point-in-time spot
captures, 10 fail-closed misses, zero exact record failures); the dominant
problem was the fixed 25% raw trend-efficiency threshold, which is structurally
too high for the noisy one-second RTI path.  More importantly, the underlying
new strict continuation sample was only 18/37 and -$48.08 at 10 contracts after
fees/slippage.  The impulse rule was therefore NOT loosened into alerts and no
historical improvement is claimed.

Challenger health now reports evaluated count, qualified count/rate, last
evaluation, and the complete rejection funnel.  An evaluated>=30/qualified=0
book is explicitly `ZERO_VOLUME_REVIEW_REQUIRED`, so a dead challenger can no
longer appear healthy merely because its accepted scoreboard is empty.

The exact sampler now freezes the independent opposite-side ask and displayed
depth from the same websocket snapshot.  A separate
`rti_countertrend_value_v1` research-only book buys the frozen opposite side
only at 41-50c, spread <=2c, and displayed depth >=10.  Its side, entry quote,
fees, 2c slippage, and settlement result are graded independently in the
challenger scoreboard.  This rule was created after reviewing outcomes, claims
no historical credit, cannot notify, cannot trade, and begins prospectively at
n=0 with manual 30/60/150 reviews.  The retrospective pattern was regime-
inconsistent, so collection rather than promotion is the only defensible use.

The first live countertrend capture then exposed serial timestamp contamination:
BTC evidence completed at +0.19s but later assets appeared at +3.6s..+6.9s
because each row was enriched/persisted before the next asset was processed.
`ExactRTI13MSampler.tick` is now explicitly three-phase: freeze every due
Kalshi quote + live spot snapshot, read every exact RTI path, then persist rows.
`rti_path_evaluation_delay_s` measures immutable evidence completion;
`rti_storage_delay_s` separately records harmless downstream persistence lag.
A regression test proves all quotes/paths precede the first recorder call.

Post-restart live proof at the 2026-07-20 02:32 ET capture (02:45 close): all
7 assets recorded, zero exact failures, and 7/7 fresh spot captures.  Genuine
Kalshi quote offsets were +0.001s..+0.444s and every 61-second RTI path was read
by +0.493s.  Persistence completed at +0.493s..+1.275s, confirming storage lag
is now separated from immutable decision evidence.  The research-only
countertrend book froze two new independent candidates (BTC NO 47c and ETH YES
47c); neither is Telegram-eligible or promoted.

Full suite: 2,072 passed, 5 skipped.

## Shipped THIS session - RTI impulse-strength prospective challenger
Run time: 2026-07-19.

The frozen exact-13M strict RTI control is unchanged and continues to record and
settle.  Telegram delivery now requires the separate forward-only PAPER
challenger `impulse_strength_v1`; strict-control-only rows no longer notify.
The previously independent `strong_path_wide_v1` and `value_price_wide_v1`
books remain frozen counterfactual ledgers but are notification-muted after
their forward review.  No real trading or automatic promotion was enabled.

The new challenger was created after inspecting the reviewed losses, explicitly
claims no historical credit, starts prospectively at n=0, and is manually
reviewed at 30/60/150 resolved rows with BTC isolated from the non-BTC transfer
cohort.  It requires every frozen strict gate plus: >=1.0 bps signed strike
distance, >=0.5 bps 61-second signed move, >=25% trend efficiency, a non-fading
second half, at most one strike crossing (and >=20s since it), displayed ask
depth for all 10 simulated contracts, and fresh spot pressure no worse than
-0.25 in the predicted direction.  Cards say PAPER, identify the challenger
policy, show the impulse/reversal/fill evidence, and remain idempotent through
the durable V3 outbox.

The exact path feature record now also freezes distance, path range, realized
volatility, trend efficiency, first/second-half movement, acceleration, strike
crossings, time since crossing, and expected-remaining-volatility diagnostics.
Exact RTI decisions use an on-demand in-memory spot snapshot instead of waiting
for the five-second research DB write cadence; the evidence is persisted in the
strategy ledger and freshness still fails closed.  This improves evidence
coverage without increasing the already-large spot SQLite write rate.

## Shipped THIS session - exact-13M RTI reliability challenger and frozen audit
Run time: 2026-07-18/19.

The existing seven-asset strict RTI Path 13M system remains the immutable
control. A leakage-aware audit now lives in `tools/q15_rti_improvement_audit.py`
and writes `work/rti-improvement/{audit.json,audit.md}`. The historical universe
is frozen through close `1784432700` (2026-07-18 23:45 local); all assets sharing
a close remain in one chronological fold. Later outcomes cannot move fold
boundaries and reconstructed post-freeze rows are explicitly excluded from
promotion. The report also admits that the historical final fold was viewed
during exploration, so the durable prospective ledger is the only genuinely
untouched test.

Current-fee replay of the frozen control is 7/10, 70.0%, Wilson 95% 39.7%-89.2%,
+$9.49 at 10 contracts with 2c/contract slippage. The selected collection-only
shadow is `spot_book_confirm_v1`: strict control must pass, the local spot-depth
snapshot must have existed at/before decision and be <=3s old, exchange book age
must be within the explicit -3s..+2s clock band, and nonzero depth imbalance must
align with the RTI side. Frozen-history result is only 5/5, +$19.65, Wilson lower
56.6% versus 58.7% fee-only break-even (60.7% with slippage), so it is NOT
promotion-eligible. Excluding the three known losses leaves shadow 5/5 versus
control 7/7: no defensible improvement. Rejected control rows were 2/5 and
-$10.15, showing both apparent saved losses and the tiny/post-hoc sample risk.

The exact sampler now captures point-in-time spot context with fail-closed
timestamp checks. Every new decision freezes the challenger verdict and policy
version in the existing durable strategy ledger; normal official settlement
reconciliation grades it with current Kalshi July-7-2026 centicent fee rounding,
10 contracts, and 2c slippage. `/api/health -> rti_path_13m_challenger` exposes
rows/resolved/accuracy/Wilson/break-even/P&L/drawdown and a non-automatic
promotion-criteria flag. Review bars are 30/60/150 resolved prospective picks,
manual only. The shadow cannot independently trigger Telegram, but when it
overlaps a strict alert, that PAPER card adds `UNPROMOTED PAPER SHADOW` plus the
shadow rule version. No order-placement surface was added.

Audit coverage includes every requested challenger/signal family, all-candidate
fee/slippage tables, BTC vs non-BTC transfer cohorts, asset/side/distance/
volatility/regime/reversal/settlement-average risk breakouts, and rejected-trade
counterfactuals. A delayed 30-60s confirmation was correctly not backtested:
there are zero point-in-time stored Kalshi quotes near those timestamps and the
13M quote is never reused. Config audit: 1,058 variables OK. Final suite: 2,065
passed, 5 skipped. Local stack restarted on port 8000 with executor dry-run/kill
defaults, websocket connected, settlement connected, all seven RTI assets
registered, and the challenger at prospective n=0 by design.
Post-restart live proof on the 00:45-close window: 7/7 exact quotes and 7/7
durable rows at +30ms, every RTI path 61/61, zero record failures. Six spot
contexts passed; BNB's exchange book age was 2.234s and correctly failed closed
as `spot_depth_book_stale`. No strict or shadow pick qualified, so the prospective
shadow book correctly remained n=0 and no Telegram card was due.

## Shipped THIS session - seven-asset RTI-path 13M exact-time V3 system
Run time: 2026-07-15.

Owner selected the strict 62c rule after an exact historical audit and asked to
measure it on genuinely new data, then extended the same gate to every tracked
asset. BTC keeps the separately versioned historically audited rule in a clean
exact-capture forward book (`btc-rti-path-13m-62c-exact-v3`).
ETH/SOL/XRP/DOGE/BNB/HYPE each use their official settlement RTI and an
independent `*-transfer-exact-v3` rule identity, explicitly
labeled as unvalidated transfer cohorts. Exact gate: 13M capture; all 61 RTI
seconds from 14M through 13M present and <=2s fresh; same side at both endpoints;
>=80% path persistence; non-negative side-adjusted RTI movement; executable
RTI-side ask <=62c; spread <=1.5c. Every evaluated market persists as ACCEPTED
or REJECTED with point-in-time evidence; no missing/stale input fails open and a
cold restart does not backfill the in-memory path.

The first forward funnel exposed an infrastructure bottleneck rather than a
reason to weaken the rule: 22/24 v1 candidates missed exact timing, 17/24 had the
old missing quote age, and the first otherwise-clean window arrived at +2.49s,
just beyond the 2s gate. Those v1 rows remain honest rejected audit evidence but
cannot contaminate the new version.

`q15_upgrade/rti_exact_13m.py` is now the owner of RTI decision capture when
`Q15_V3_RTI_EXACT_SAMPLER=true`. Its read-only 50ms worker freezes both sides of
the live Kalshi websocket book at the real close-minus-780s instant, then waits
at most 2s for that exact official RTI second. It never relabels a late quote,
never backfills after a cold restart, and persists a failure rather than failing
open. The slow interval/model loop yields ownership while the sampler is on.
Health is visible at `/api/health -> rti_exact_13m`.

The first exact-v2 live capture proved the scheduler (7/7 rows, 6.9ms final
offset, 61/61 RTI seconds) and also exposed websocket float-dust ghost levels:
sub-nanocontract residues survived depletion and appeared as impossible crossed
books. No card escaped because all seven signal paths independently rejected.
The feed now removes <=1e-9 contract dust, microstructure filters it, crossed
books are unavailable, and strict/challenger rules hard-reject negative spread
with `SPREAD_CROSSED`. The clean v3 identity keeps those seven v2 rows isolated.

Live v3 proof on the 2026-07-15 18:45-close window: 7/7 durable rows, every
official path 61/61 and fresh, quote ages 0.00s..0.28s, zero crossed/dust/depth
violations, zero record failures, and a 1.535ms final capture offset. No strict
row qualified, for genuine signal/price/spread reasons. XRP was the intended
shadow-volume example: strict rejected a 2c spread, while both pre-registered
challengers accepted its 100% persistence, positive move, 53c entry and fully
fresh evidence. It remains notification-disabled pending forward settlement.

Two outcome-blind volume challengers are pre-registered and stored in every
exact decision's point-in-time threshold JSON: `strong_path_wide_v1` allows a
2c spread only with >=90% RTI persistence; `value_price_wide_v1` allows a 2c
spread only at <=58c. Both retain all strict freshness/timing/index/same-side/
momentum gates, never notify, and require manual promotion reviews at 30/60/150
resolved rows. Their forward books are exposed under the V3 scoreboard's
`rti_path_challengers`; thresholds cannot be changed after seeing outcomes.

Accepted rows enqueue an explicit per-asset `V3 <ASSET> RTI PATH 13M | PAPER`
card through the durable V3 outbox. Settlement grading uses a 10-contract
simulation with the official order-level fee formula plus 2c/contract slippage,
matching the audit. Local flags enable notifications and all seven assets. Full
suite: 2050 passed, 5 skipped. Local app restarted cleanly on port 8000; health
`ok`, exact sampler thread alive with all seven assets registered, Kalshi
websocket connected, and every official RTI fresh. Executor dry-run + kill
switches remain true.
Each forward book starts at n=0 by design; never aggregate transfer cohorts into
BTC's validated performance.
## Shipped THIS session - PREDICTION_PLAN.md: model v2 build plan (owner-requested)
Run time: 2026-07-19 (follow-on; owner wants a fully fleshed-out prediction
system for the TT leagues with non-standard features, plan first).

``tt_edge/PREDICTION_PLAN.md`` is now the standing build plan: Phase A data
foundation (results backfill, per-set score verification, ratings/feature/
model-version tables) -> B league-aware MOV-Elo with Glicko-style RD gating
as a shadow head -> C feature expansion (same-day rematch, fatigue/workload,
time-of-day residuals, layoff, clutch/deciding-set, opponent-adjusted form
residual, H2H residual vs rating expectation, margin trend, per-league
calibration; market features quarantined to a separate overlay head) ->
D fitted logistic + per-league Platt -> E promotion gates (Brier/log-loss
vs champion AND vs market close, CLV tracking to start early) -> F edge-
ranked slot claiming + report integration. Build phases in order in future
sessions; every phase behind the shadow/challenger discipline.

## Shipped THIS session - CLOUD-FIRST: Routine owns all leagues, home autoscan default OFF
Run time: 2026-07-19 (follow-on; owner: "just make the tt elite pics arive
here too so i can see them all when i ask").

Split coverage lasted ~40 minutes: the owner wants EVERY league's picks
visible from cloud sessions on demand, so the operating mode is now
CLOUD-FIRST:
- Routine recreated as "TT-Edge cloud pick cycle (all leagues)"
  (trig_01CdhzBLRdyUfwbHyEm22Gz8, hourly at :01) — default league list
  (29128,29097,22742), no pin.
- ``integration.autoscan_enabled`` production default flipped to OFF (with
  docstring + test updates): an unconfigured app restart must not start a
  second loop that double-alerts against the Routine. Opt back in with
  TT_EDGE_AUTOSCAN_ENABLED=true only after pausing the Routine.
- CLAUDE.md gained a "TT-Edge picks on demand" section: when the owner asks
  for picks, run ``python3 -m tt_edge.jobs.cloud_cycle`` in-session and
  report the PICKS section + near-miss edges from the scan log; in-session
  output IS the delivery (no Telegram creds in sandboxes; claims are
  idempotent so the Routine won't re-alert). README + .env.example updated
  from split-coverage to cloud-first wording.

Live activity this session (in-session runs with the owner-pasted token,
now also in environment settings): 2 pre-split TT Elite claims settled via
the new grade-only path (Malcher won +138c, Rutkowski/Olbrycht lost -205c),
2 new TT Cup picks claimed and reported in-chat (Moravec -150 edge +10.2,
Lasota -110 edge +10.5, both start ~06:00Z). Ledger now 6W-3L, net -286c,
bankroll 6214c, 3 open. KEY-REACHES-ROUTINE still UNVERIFIED: the 00:01
fire's state push (if any) was overwritten by an in-session run at 00:04 —
verify at a fire with no in-session runs nearby (state branch commit or
odds stamps after the :01 fire = confirmed).

## Shipped THIS session - TT-Edge fix: grade-only results for unscanned leagues
Run time: 2026-07-18/19 (follow-on; owner pasted the BetsAPI token in-session
and asked for fresh odds — the live cups-only run then exposed the gap).

Bug (latent, would have bitten within hours): the cups-only Routine never
fetches TT Elite results, so the 3 OPEN TT Elite claims in the cloud DB
(recommended 22:56Z, pre-split) could never settle — and
``edge_calc`` abstains on ``open_recommendations >= 3``, so every future
cup pick would have been silently suppressed forever. Fix: the cloud cycle
now self-heals — ``grade_only_result_dates`` maps each open claim's
tournament to its BetsAPI league id (new ``betsapi.LEAGUE_ID_BY_NAME``) and,
for leagues NOT in the scan list, fetches a results-only bundle
(``betsapi.fetch_results_bundle``: ended feed only, not-started events
dropped, no history/odds calls) on each claim's own start date. Grading
settles them; the board parser only analyzes ``notstarted`` events, so a
grade-only league structurally cannot produce a new pick (no double-alert
with the home loop). Unmapped tournament names log a warning naming the fix.

Verification: 3 new tests (full settle-without-scan flow incl. no
upcoming/history/odds calls for the unscanned league; date derivation skips
scanned + unmapped leagues and reaches days-old claims; results bundle is
ended-only and degrades per-date). Live token validated in-session: cups
cycle ran clean (101 matches, 22 odds rows, no qualifying picks — selective,
not broken). Full suite green (below); config audit OK.

## Shipped THIS session - PC back: SPLIT COVERAGE decided + Routine now cups-only
Run time: 2026-07-18 (owner: "okay im on my pc set up what you need to").

Owner is back at the PC (its app last ran 2026-07-16 / commit c78c4de —
pre-TT-Edge, so a pull+restart is pending there). Asked the owner to choose
the TT-Edge operating mode; they chose **SPLIT COVERAGE**:
- HOME (the PC's in-app autoscan, automatic on pull+restart) owns TT Elite
  Series — sofascore prices, 30-min cadence, Q15 Telegram fallback.
- CLOUD (hourly Routine) owns the cups ONLY. The old all-3-leagues Routine
  was deleted and recreated as "TT-Edge cloud pick cycle (cups only)"
  (trig_014BzJsXMdjdzVys4QjzYdLe, hourly at :01) with
  ``TT_EDGE_BETSAPI_LEAGUE_ID=29097,22742`` pinned on the command line in
  its prompt — structurally cannot double-alert TT Elite regardless of what
  the environment settings hold. Still exits quietly until the owner puts
  TT_EDGE_BETSAPI_KEY in the Claude Code environment settings (owner-only).
Convention documented in ``.env.example`` (cloud block) and
``tt_edge/README.md`` so future sessions honor the per-league either/or.

OWNER ACTIONS PENDING (given in-session): PC ``git pull`` + app restart,
then ``python3 -m tt_edge.jobs.autoscan --probe --test-message``; put
TT_EDGE_BETSAPI_KEY in environment settings; from the PC run
``scripts/prune_branches.sh --all`` (58 stale claude/* refs) and
``git push origin --delete tt-edge-state-test`` (sandbox cannot delete refs).

Verification: docs-only diff — full suite 2296 passed, 5 skipped; config
audit OK (1072 env reads documented/baselined). Sandbox env note: this
session's container needed ``pip install -r requirements.txt pytest
playwright --ignore-installed PyJWT`` before the suite would run (two
tt_edge integration tests assume the playwright package is importable).

## Shipped THIS session - TT-Edge fix: BetsAPI stale-odds false-reject
Run time: 2026-07-18 (follow-on; while hunting a live pick, the DB showed 52
matches/sweep rejected for stale_odds).

Bug: cloud odds snapshots used Bet365's ``add_time`` (when the PRICE last
changed) as ``captured_at``. On low-liquidity table-tennis markets a price
sits unchanged for many minutes, so the freshness guard (odds >10m = stale)
false-rejected the CURRENT live price. This suppressed the majority of cloud
picks. Fix: ``betsapi.current_odds`` returns the newest price and
``fetch_cloud_bundle`` stamps it at FETCH time (now) — one observation per
fetch, movement measured across fetches, exactly like the sofascore/home
path. Live effect: odds rows per sweep 7 -> 48, and a pick surfaced
immediately (Rutkowski -138, +5.5). parse_odds_series retained for its tests
but no longer feeds the pipeline directly.

Verification: 2 new tests (current_odds newest-wins; a 2h-old stable price is
stamped at now and passes freshness); tt_edge total 300. Full suite 2296
passed, 5 skipped; config audit OK.

## Shipped THIS session - TT-Edge multi-league cloud (TT Elite + TT Cup + Czech)
Run time: 2026-07-18 (follow-on; owner sent a book screenshot: "check these
cups as well" — their book lists TT Elite, TT Cup, Czech Republic Pro League
live).

Confirmed the live token WORKS end to end (validated in-session, redacted):
real Bet365 odds, and it produced real picks — a TT Elite pick (Gesiarz -138,
+10.5 edge) and, after adding leagues, a Czech Liga Pro pick (Sychra -200,
+5.2). Enumerated BetsAPI table-tennis leagues from the live feed:
29128 TT Elite Series, 29097 TT Cup, 22742 Czech Liga Pro, 22307 Setka Cup.
Made multi-league permanent:
- `betsapi.parse_league_ids` + `DEFAULT_LEAGUE_IDS=(29128,29097,22742)`.
- `cloud_cycle.run_cloud_cycle` now takes `league_ids` and fetches each
  league independently (one failing league doesn't sink the others),
  concatenating canonical envelopes + odds into ONE merged cycle; match ids
  are globally unique so claims/grading/the shared paper bankroll compose.
- Cloud mode now disables the sofascore-style tournament_keyword filter
  (the BetsAPI league id already isolates each league; the old default
  "TT Elite" would have dropped TT Cup/Czech boards). Operator override via
  TT_EDGE_TOURNAMENT_KEYWORD still honored.
- TT_EDGE_BETSAPI_LEAGUE_ID is now comma-separated (default = the 3 leagues).

IMPORTANT (still true): the hourly Routine can't run until the owner puts
TT_EDGE_BETSAPI_KEY in the Claude Code ENVIRONMENT SETTINGS. I cannot set it
(no tool; embedding it in the Routine prompt was classifier-blocked, twice —
credentials must live in env settings, owner-only). Until then, picks come
only from me running cloud_cycle on demand in a session. The hourly trigger
(trig id in list_triggers) is armed and skips quietly with no key.

Verification: 4 new cloud tests (parse_league_ids, two-league merge routing
by id, updated signatures); tt_edge total 298. Full suite green (below);
config audit OK.

## Shipped THIS session - TT-Edge CLOUD mode (BetsAPI + hourly Routine)
Run time: 2026-07-18 (follow-on; owner: "can we just run it on the cloud,
I don't have my computer rn").

Cloud feasibility was tested, not assumed: api.telegram.org reachable from
the sandbox (HTTP 302); sofascore blocked on every route (curl 403 via the
egress proxy, Playwright ERR_CONNECTION_RESET, harness WebFetch 404 even on
known-good endpoints — their bot-wall rejects datacenter traffic; no
evasion attempted); BetsAPI fully reachable (docs 200, API 401 = answers,
wants a token); pushing non-claude state branches allowed. Hence:
- `scrapers/betsapi.py`: stdlib-urllib client (token never logged,
  success-flag checked) + translators from BetsAPI shapes into the
  pipeline's canonical event dicts — board (upcoming+ended merged),
  H2H + both players' form from ONE history call, and the Bet365
  moneyline TIME SERIES -> chronological odds snapshots under book
  "bet365" (real line-movement for the fix-risk guard; decimal-odds
  parsing kept separate from the sofascore fractional heuristic — "2"
  means +100 here, not 2/1).
- `state_sync.py`: SQLite state persisted across ephemeral sessions as a
  single orphan commit on `tt-edge-state` (learning-snapshots pattern;
  plumbing only: hash-object -> mktree -> commit-tree -> push -f; restore
  never clobbers a local DB; failures degrade, never crash).
- `jobs/cloud_cycle.py`: one cycle — restore state, fetch bundle, insert
  odds, grade+scan (unchanged pipeline), push state, print a `PICKS (n)`
  section with each alert verbatim. Exits 3 quietly without
  TT_EDGE_BETSAPI_KEY so the hourly Routine is ~free until the key exists.
- Hourly Claude Code Routine (fresh session per fire, push notifications)
  created to run it; picks reach the owner's phone via the Routine
  notification even with no Telegram configured, and via Telegram once
  creds are in the environment.
- Env: TT_EDGE_BETSAPI_KEY / _LEAGUE_ID (29128) / _BASE, TT_EDGE_STATE_BRANCH.
  OPERATOR: run EITHER cloud mode OR a home loop, never both (separate DBs
  would double-alert). Stray branch `tt-edge-state-test` (push-permission
  probe) can be deleted from a host with ref-delete rights.

Verification: 40 new tests (translation/status/winner mapping, decimal-odds
strictness, series parsing, client error paths, full cloud cycle incl.
idempotency + later grading + history-failure degradation, state round-trip
on real git repos incl. single-commit force-push and no-clobber restore);
tt_edge total 294. Full suite green (below); config audit OK.

## Shipped THIS session - TT-Edge zero-command: autoscan inside app.py
Run time: 2026-07-18 (follow-on; owner: "wait, I have to do the commands?").

The pick loop now needs NO commands beyond the owner's normal deploy
routine (git pull + restart the app). `tt_edge/integration.py` +
a guarded 6-line hook in app.py's `_start_refresh` (house pattern — same
try/except as every other subsystem):
- Daemon thread runs the autoscan cycle every TT_EDGE_AUTOSCAN_INTERVAL_S;
  kill switch TT_EDGE_AUTOSCAN_ENABLED=false; forced off for every test via
  conftest autouse (like ultoim).
- Self-bootstrap (TT_EDGE_AUTO_INSTALL, default on): pip-installs
  playwright and runs the idempotent chromium install if missing; failure
  degrades to cache-only cycles with periodic errors, never a crash.
- PAPER bankroll auto-seeds once from TT_EDGE_BANKROLL_INIT_DOLLARS
  (default 65.00, the spec's launch tier); never reseeds an existing value.
- Telegram needs nothing: inside the app process the Q15 creds exist, so
  the fallback delivers picks to the owner's existing channel.
- Containment: config errors, cycle crashes, and bootstrap failures are all
  logged-and-survived; the Q15 monitor cannot be taken down by TT-Edge.
  Do NOT also run the standalone jobs/autoscan.py against the same DB.

Verification: 11 new tests (gating incl. production default ON, bootstrap
paths, bankroll seed/override/no-reseed, loop containment: a cycle that
raises twice keeps cycling; invalid config returns instead of raising);
tt_edge total 254. Full suite green (see run below); config audit OK.
DEPLOY: owner's local host — git pull + restart the app. Picks then flow
with zero further action (verify anytime:
`python3 -m tt_edge.jobs.autoscan --probe --test-message`).

## Shipped THIS session - TT-Edge: --probe diagnosis + .env bootstrap
Run time: 2026-07-18 (follow-on; owner: "I'm not seeing any" picks).

Root cause of "no picks": nothing is RUNNING the loop — the owner's host
last pushed a learning snapshot 2026-07-16 (running_commit c78c4de, pre-
TT-Edge), so the autoscan has never been started there; and the web sandbox
cannot run it for real (its egress proxy resets sofascore connections —
verified live — and holds no Telegram secrets). Made the on-host start
foolproof instead:
- `python3 -m tt_edge.jobs.autoscan --probe [--test-message]`: one-shot
  chain diagnosis — bankroll, Telegram (which bot: dedicated vs Q15
  fallback; optional real test send), board fetch/freshness, upcoming-match
  and odds coverage — with a READY/NOT READY verdict naming the broken
  link. The loop also logs an ERROR pointing at --probe whenever a cycle
  sees no board.
- `tt_edge/envfile.py`: job CLIs auto-load repo-root `.env` / `.env.local`
  (setdefault semantics — real env always wins) so Telegram secrets stored
  in files reach a fresh terminal's autoscan.
- `PLAYWRIGHT_CHROMIUM_EXECUTABLE`: point Playwright at an existing
  Chromium when the pip version disagrees with installed browsers (the web
  sandbox needs /opt/pw-browsers/chromium; validated — browser launched).

Verification: 6 new tests (envfile semantics, telegram source tagging,
probe READY/NOT-READY/stale paths); tt_edge total 243. Full suite 2239
passed, 5 skipped; config audit OK. NEXT OPERATOR STEP (nothing happens
until this): on the local host `git pull`, `pip install playwright`,
`python3 -m tt_edge.jobs.bankroll --set 65.00`, then
`python3 -m tt_edge.jobs.autoscan` (verify first with --probe).

## Shipped THIS session - TT-Edge Phase 1: autoscan (automated picks loop)
Run time: 2026-07-18 (same session as Phase 0 below; owner: "set it up so
you start sending me picks, do whatever you need to do").

Picks now arrive without babysitting. New `jobs/autoscan.py` loop (default
30 min): fetches the TT Elite boards via browser-context API GETs (one page
load for the fingerprint — plain HTTP gets 403; then polite `page.request`
GETs with a 350ms gap), yesterday's board for RESULTS + today's/tomorrow's
for picks, per-match H2H/form (cached 6h) and sofascore odds; grades
finished matches FIRST (bankroll moves automatically, frees pick slots);
appends pre-match odds under book "sofascore" (per-cycle history powers the
fix-risk movement guard); then runs the unchanged Phase 0 scan and alerts.
- `scrapers/sofa_odds.py`: fractional ("4/5", "21/20", "1") and decimal
  ("1.80") strings -> American, Decimal-exact; winner-market extraction;
  is_live odds never priced. Odds URL kind + envelope support.
- Telegram: TT_EDGE_TELEGRAM_* preferred; DEFAULT FALLBACK to the Q15
  monitor's TELEGRAM_BOT_TOKEN/CHAT (TT_EDGE_TELEGRAM_FALLBACK=false to
  separate) — picks reach the owner's existing channel with zero setup.
  Alerts priced from sofascore carry "Price source: sofascore — take X or
  better at your book" (aggregated prices are not the book's; the edge
  exists AT that price).
- Books never mix: manual entries stay book "manual", autoscan book
  "sofascore"; each scan reads only its configured book's series.
- Operator start (on the local host): `pip install playwright` +
  `python3 -m tt_edge.jobs.bankroll --set 65.00` +
  `python3 -m tt_edge.jobs.autoscan` (`--once` = cron mode).

Verification: 35 new tests (odds conversion/payloads, results-from-board,
fallback selection, full cycle: recommend->deliver->idempotent->auto-grade,
live-odds refusal, results-only stale boards); tt_edge total 237; full
suite 2233 passed, 5 skipped; config audit OK (1066 vars). Deploy: owner
`git pull` + start autoscan; Q15 runtime untouched.

## Shipped THIS session - TT-Edge: TT Elite betting analysis pipeline (Phase 0)
Run time: 2026-07-18.

Owner commissioned a NEW, separate subsystem (`tt_edge/` package, ~2.4k lines
+ 189 tests): an analysis-and-alerting pipeline for TT Elite Series (Polish
league) table tennis moneylines. Analysis only — it finds prices and alerts;
a human clicks buttons; nothing can place/modify/cancel a bet. Phase 0 per
the build spec:
- `scrapers/`: sofascore XHR-intercept fetcher (Playwright, lazy import —
  parsers and tests never need a browser) with provenance ENVELOPE files;
  pure defensive parsers for board / H2H / per-player form; manual odds
  entry CLI as the stopgap odds source (append-only snapshots keyed
  (match, book, captured_at) — feeds the fix-risk movement guard now, CLV
  later).
- `freshness.py`: hard-reject guard (board >=30m, H2H/form >=24h, odds
  >=10m stale). Every payload carries fetched_at; naive datetimes raise.
- `model/`: H2H rate (Laplace + 365d half-life decay), form differential
  (last 15, hot 5 x2, no look-ahead), common-opponent differential (60d);
  hand-set logistic blend 0.45/0.35/0.20 (weights frozen until >=300 graded
  rows); Platt calibration port of the ledger_v95 fit (clamps, applied-step
  convergence), versioned in DB, INACTIVE until manually promoted.
- `edge/`: Decimal-only proportional de-vig; `edge_calc.py` is the SINGLE
  edge source (the tri-calc drift Q15 once had cannot recur); abstains:
  stale data, insufficient data (<5 H2H AND <3 common opps), |edge|<3pts,
  edge<5pts, >=15c adverse line move (fix-risk hard pass), no bankroll,
  >=3 open picks. Quarter-Kelly staking, 5% cap dominates $1 floor,
  bankroll is a DB value (never a constant).
- `alerts/telegram.py`: rides the shared notifications TelegramSendClient on
  its own TT_EDGE_TELEGRAM_* bot/chat; claim-row -> send -> mark-delivered
  ordering (failed send retries WITHOUT a new row; rescan cannot
  double-alert — DB unique claim per match/market/day). No legacy
  formatter/suppression markers (tested).
- `db/`: portable schema (tt_-prefixed; SQLite default, Postgres via
  TT_EDGE_DATABASE_URL) + one repo module holding every SQL statement.
  `jobs/`: scan (--dry-run end-to-end), grade (settle-once, bankroll move,
  grades EVERY prediction into the calibration corpus, --fit/--activate),
  odds_entry, bankroll CLIs.
- `tt_edge/README.md` carries the pre-committed KILL CRITERIA: at 200 graded
  recommendations, ROI < -8% or persistently negative CLV = stop/rework.

An adversarial review pass (parallel subagent, repro-confirmed findings) was
applied before merge:
- Idempotency claims now key on the MATCH's UTC start date, not the scan
  instant's date — a rescan across UTC midnight (TT Elite night sessions)
  can no longer double-alert and double-settle the same match.
- Undelivered claimed alerts re-send BEFORE re-evaluation, so odds gone
  stale since the claim can't strand an alert the operator never got.
- Started matches are never bettable (a 29-min-old board passes freshness
  while its matches are mid-play); missing-status events dropped.
- Settlements + bankroll now move in ONE transaction (settle_batch); scan
  reads only the configured book's odds series (TT_EDGE_BOOK) so a second
  book can't fabricate line movement; H2H look-ahead filter; one-sided form
  drops its blend weight; PG dialect render/param tests added.

Verification: full suite 2198 passed, 5 skipped (was 1996+5; +202 tt_edge).
config_audit --check OK (1058 vars; all TT_EDGE_* documented in
.env.example). CLI smoke run on a temp DB: bankroll seed -> odds entry ->
dry-run scan (spec-format alert, $3.25 capped stake on the $65 example
roll) -> grade (+270c settlement, bankroll 6500c->6770c). Deploy: nothing to
restart — new standalone package, no Q15 runtime code touched; operator
starts using it via the CLIs when ready (needs playwright installed only for
live fetching).

## Shipped THIS session - Drift validated add-ons and settlement repair
Run time: 2026-07-10.

Owner approved the add-ons only after a pre-launch replay reproduced the frozen
tape exactly. Implemented the evidence-backed paper configuration:
- `drift_addon_requal`: first full-rule requalification at 12M..7M, sent to the
  V3 channel as a correlated PAPER add-on. Cards cap the add at 0.5x and the
  total window at 1.5x; these rows are excluded from independent-pick accuracy.
- `drift_latequal_12m_11m`: clean sub-60c 13M rows that reprice into 60-73c at
  12M/11M, sent as RESEARCH ONLY. The repeatedly losing 10M extension is off.
- Base Drift cards now enforce the tradeable 60-73c band, so diagnostic 74-80c
  rows remain recorded but cannot be sent as accepted picks.
- Checkpoint rows now flow through durable V3 records/cards with Coinbase L2,
  Kraken L3, and BTC-regime enrichment where available. The V3 scoreboard has
  separate independent, correlated-add-on, and total-exposure views.
- Interval settlement now grades Drift strategy rows and runs a one-time source-
  ledger reconciliation. On restart it repaired all 34 historical Drift cards:
  28-6, +444c, zero overdue Drift rows.

Safety: all new paths are PAPER_RESEARCH and place no orders. Local startup kept
executor dry-run and kill switches on. Verification: focused 148 passed; full
suite 1841 passed, 4 skipped; config audit 981 vars OK.

## Shipped THIS session - Picklo HD (realistic Piccolo) Blender asset (non-runtime)
Run time: 2026-07-08.

Owner asked for a "highly detailed, realistic 3D model" follow-up to the blocky
R15 Picklo. Built a subdivided, organically-modeled Piccolo as a new asset
(the blocky picklo_r15.blend is untouched). Non-runtime art asset:
- `assets/picklo/picklo_hd.blend` — realistic model (Blender 5.0, ~284K). Same
  15 R15 parts + joint-pivot origins + LowerTorso-rooted hierarchy, but each
  part is a lofted/subdivided organic mesh: detailed head (scowling brow ridge,
  deep-set stern eyes, sharp nose, mouth crease, long pointed ears, forward-
  arcing segmented antennae, eyeball children), muscular pink ribbed arms with
  red-orange wrist bands, green hands (four curled fingers + thumb + black
  nails), purple gi with modeled folds/armholes/collar, blue obi sash (stacked
  wraps + knot + draping tails child), baggy cinched pants, orange pointed
  shoes with soles. Realistic Cycles materials (green SSS skin, woven gi, silk
  sash, leather shoes, lacquer nails) + a studio (cove backdrop, 3-point
  lighting, Camera_Full / Camera_Head, AgX tone-mapping).
- `assets/picklo/hd/` — component modules (spec, materials, head, torso, arms,
  hands, legs, scene) each exposing build()/register()/setup().
  `assets/picklo/build_picklo_hd.py` assembles + wires + saves + `--verify`s +
  `--render`s. `tests/test_picklo_asset.py` gained an HD contract test
  (auto-skips without bpy).

Verification: pytest -> 1799 passed, 14 skipped. build_picklo_hd --verify ->
15 HD parts (10.7k cage verts), hierarchy + materials + studio present.
Renders (Camera_Full / Camera_Head) checked visually against the reference.
No runtime code touched.

## Shipped THIS session - Picklo R15 Blender asset (owner request, non-runtime)
Run time: 2026-07-08.

Owner asked for an R15-style "Picklo" character model with every part separated
and individually colored, delivered as a Blender file — then clarified with a
reference image that Picklo = Piccolo (Dragon Ball). Not runtime code — a
standalone art asset:
- `assets/picklo/picklo_r15.blend` — the model (Blender 5.0, compressed).
  15 mesh parts with R15 names (Head, Upper/LowerTorso, L/R Upper/LowerArm,
  Hand, Upper/LowerLeg, Foot), each with its own uniquely colored material
  (`Picklo_<Part>`), Piccolo palette: green head/hands, purple gi torso+legs,
  blue sash (LowerTorso), pink ribbed arm segments, orange pointed shoes
  (toe-tapered). The Head mesh carries Piccolo's forward-tilted antennae and
  pointed side ears (same object, so the model stays exactly 15 parts).
  Origins sit at the joints and parts are parented LowerTorso -> UpperTorso ->
  Head/arms, LowerTorso -> legs, so parts rotate naturally for posing. A
  separate `Environment` collection holds ground/sun/camera for instant renders.
- `assets/picklo/build_picklo_r15.py` — deterministic generator + `--verify`
  (regenerate with `pip install bpy`, then run the script; also works inside
  Blender's own Python).
- `tests/test_picklo_asset.py` — asset contract test; auto-skips wherever
  `bpy` isn't installed (it is NOT a dependency of the monitor).

Verification: pytest (web sandbox) -> 1798 passed, 14 skipped (env-dependent
skips; asset test runs and passes where bpy exists). No runtime code touched.
Deploy: nothing to restart — pure repo asset.

## Shipped THIS session - Drift Shadow pick cards into the V3 Telegram chat
Run time: 2026-07-08 (follow-on to v3).

Owner approved the DRIFT PICK card design and asked for delivery into the V3
channel, styled like the ultoim_v2 channel's UI (bold header outside <pre>,
body inside one <pre> panel). Implementation:
- `strategy_bots`: new bot `drift_13m` (BOT_DRIFT_13M) — `drift_pick_13m_decision`
  (rules), `record_drift_pick_row` (runtime; durable claim per (window, ticker) —
  multi-pick book), `build_drift_pick_alert` (telegram; v2 panel grammar). Card
  carries: size banner from the stack tilt (⭐ FULL ≥1.25x / ✅ NORMAL / 🔉 HALF
  <0.75x), BUY line + breakeven, sizing reasons (spread/session), fill doctrine
  (depth>=50 -> rest at ask; thin -> pay +1c now), 25-50/100 size guide, live
  book record + verdict-n footer. No live formatter/suppression markers (tested).
- Wiring: interval-research runner `_alert_drift_picks` fires after
  `observe_window` writes; recorder gains read-only `picks_recorded_at` (the
  recorder itself still never notifies).
- Flags (default ON per owner directive): `Q15_V3_DRIFT_13M`,
  `Q15_V3_DRIFT_13M_NOTIFY`; delivery also requires `Q15_V3_TELEGRAM_ENABLED`.
- ADD-ON / LATE-QUAL cards deliberately NOT built yet: those tracks have n=0
  live; wire their cards once the v3 tracks actually record (next session).

Verification: pytest -> 1836 passed, 4 skipped. config_audit OK (977 vars).
Deploy-pending: owner's local host needs git pull + app restart (also still
pending for the v3 recorder tracks themselves — see operational note above).

## Shipped THIS session - Drift Shadow v3: verified improvement tracks (record-only)
Run time: 2026-07-08.

Origin: owner asked for a deep improvement search on the drift book ("top 5, review
first"), then approved adding the survivors to the shadow AS SEPARATE PARTS, with a
standing directive: if the shadow is ever enabled as a live book, it takes ALL
components whose bars passed ("full_enable_blueprint" in the scoreboard). An 8-family
parallel search (~40 hypotheses: checkpoint sweep, late qualifiers, sizing 2.0,
ranking 2.0, band edges, cross-signals, execution, mechanism measurement) was run on a
fresh oracle-graded multi-checkpoint dataset (66,302 rows, all 8 marks); every survivor
was then re-verified by hand (ablation, walk-forward, holiday/day-concentration
controls, null baselines). Survivors -> v3 tracks in q15_upgrade/drift_shadow.py:

- ADD-ON track (drift_addons): 13M volume-book pick re-passes the FULL rule at
  12M..7M -> record ONE add-on at that checkpoint's ask (first re-qual only).
  Tape: n=123, +12.14c/add, WR 81.3%, both halves + all quarters positive,
  ablation-tolerant (11M-7M-only +18.7c), null-controlled (unconditional adds
  +6.94). Bars: KILL n>=40 if EV<=0|WR<be; PROMOTE n>=120 if EV>=4 & WLB>be.
- LATE-QUAL track (drift_lq_watch -> drift_latequal): clean-but-cheap 13M signal
  (alt YES, dist/fp clean, ask<60) that reprices INTO 60-73 by 12M/11M/10M.
  Tape: n=38, +9.34c/pick but n_train=8 and 44% one-day concentration -> full
  bar before anything (KILL n>=40; PROMOTE n>=150 if EV>=2 & WLB>be).
- SIZING TILTS (columns on drift_picks): spread_weight (3-4c -> 1.5x, >=5c ->
  0.5x; the 3-4c cohort held ~+14c/pick in EVERY sub-period), session_weight
  (UTC 16-24 -> 1.33x / 8-16 -> 0.75x / 0-8 -> 0.84x; survives weekday+holiday
  controls), stack_weight = clip(product). Reported in scoreboard
  tilts_volume_book alongside flat + size_weight; never gate any bar.
- Execution context: spread_cents + depth_contracts now stored per pick; doctrine
  for manual trading: chase +1c ONLY when displayed depth<50; 25-50 contracts/pick
  comfort zone, ~100 conservative ceiling.
- Wiring: runner _maybe_drift_checkpoints feeds 12M..7M slates (bands mark-40..
  mark-10s); ledger captures_for_window now also returns yes_bid_cents +
  depth_contracts. v2 books/bars UNTOUCHED; older DBs migrate in place (ALTER
  columns + new tables).

Killed with receipts (do not revisit without new tape): every other checkpoint as a
standalone book (only 13M works; 11M's value lives entirely in the add-on overlap),
undifferentiated late entries, sub-60 floors (55-59 train-negative; 50-54 one-day/
post-hoc), re-ranking (no-op in 88% of windows; train-best ranker degrades test),
BTC-state/slate-size/day-of-week tilts, sizing-2.0 refinements over terciles.
Honest note: on the rebuilt split the shipped tercile edge is only +0.09 c/unit
(8.98 vs 8.89) - much smaller than the original split suggested; the spread/session
tilts are 20-30x larger effects on the same basis. Mechanism receipt: near-strike alt
YES-favorites settle +4.8pp above their price (both halves, all quarters); NO-favorites
are fairly priced; the mispricing peaks exactly at 13M.

Verification: python -m pytest tests/ -q -> 1831 passed, 4 skipped (22 drift tests).
config_audit --check OK (975 vars; v3 adds none). Deploy-pending: host pull+restart.

## Shipped THIS session - drift recorder: disagreement-weighted sizing (record-only)
Run time: 2026-07-07 (same session as v2, follow-on).

Origin: owner asked for accuracy-without-profit-loss levers on the drift book. 28 levers
tested (16 exclusion/filter + 12 structural). The question CLOSED with a structural result:
every slice of the volume book is net-profitable on the tape, so ANY exclusion filter lowers
total profit — accuracy and profit are provably a trade-off inside this book (22/77 losers
are settlement shocks; accuracy ceiling ~81%). Exactly ONE lever raised profit without
touching accuracy or trade count: disagreement-tercile sizing — 1.5x the top tercile, 0.5x
the bottom. Tape: +317c (+19%) at identical trades/accuracy; OOS check (train-fit terciles
applied to the held-out half): +9.47 vs +9.26 c/unit. In-flight 7M exits, by contrast,
DESTROY value here (TP90 -365c, CUT40 -1,127c) — do not add exit logic.

Implemented in `q15_upgrade/drift_shadow.py` (still record-only, bars UNTOUCHED):
- Each row now records `size_weight` (1.5/1.0/0.5) from FROZEN train-fit tercile thresholds
  lo=-0.1157 / hi=-0.0917 (`Q15_DRIFT_SHADOW_W_LO_T`/`_W_HI_T`, research-only overrides).
- scoreboard() reports `weighted_pnl_cents` + `weighted_ev_per_unit_cents` per book
  ALONGSIDE flat; kill/promote bars still grade FLAT numbers only (sizing is observational).
- Column migration: existing v2 DBs (host runs 519fb29) gain `size_weight` via ALTER TABLE;
  pre-existing NULL rows read as weight 1.0.

Verification: `python -m pytest tests/ -q` -> 1824 passed, 4 skipped (15 drift tests incl.
tercile boundaries, weighted-scoreboard math, v2-pre-sizing column migration).
config_audit --check OK (975 vars). Deploy-pending: host pull+restart.

## Shipped THIS session - drift recorder v2 (audited volume expansion, 3 nested books)
Run time: 2026-07-07.

Origin: owner asked for definitive ways to get MORE picks or HIGHER win% on the drift book.
Ran (a) an inline beta study, (b) an inline ablation study, (c) a 18-agent parallel
improvement workflow (6 angles x 38 hypotheses + adversarial audits). Findings:

CONFIRMED (audited, all-quarters-positive, ablation-tolerant, permutation p<0.01):
- Ask-floor widening to 60 + taking ALL qualifying picks (not top-1): the volume book
  60-73/all-quals = n=285 on tape, ~19.6/day (2.3x), EV +5.95c full, quarters all positive.
- Dropping the one-per-interval cap alone: +13% volume at EQUAL-or-better EV (extras
  standalone-positive in both halves).
DEFINITIVE NEGATIVES (equally binding — protect the book):
- No aggregate up-drift in the tape (50.1% YES): the mechanism is NOT unconditional drift.
  Daily P&L corr +0.63 with day's %YES => the book is free optionality on alt-up days
  (~breakeven on down days, prints on up days).
- BTC regime gate REDUNDANT (baseline already implicitly selects BTC-up windows; anti-gate
  cohort n=11/127); gates only remove picks.
- NO-side mirror FAILS even in BTC-down regimes: the effect is YES-specific.
- cheap-YES (buying YES against the model's NO near strike): all 8 variants train-negative.
  The edge REQUIRES model-side agreement — it is model-skill x near-strike x YES.
- dist<=3e-5 and fp<=30 are hard boundaries: each relaxation's marginal cohort loses in
  both halves. 74-80 ask ceiling is a train-mirage (train +8.4 -> test -4.6).

Implemented drift_shadow v2 (record-only, never trades/notifies):
- Records the 60-80 SUPERSET envelope, every qualifying pick per interval with pick_rank;
  UNIQUE(model_version, window_key, ticker); v1 schema auto-migrates aside (drift_picks_v1).
- scoreboard() grades THREE nested books from one stream, each vs frozen bars:
  primary 65-73 top-1 (2026-07-06 bars UNCHANGED: KILL n>=40 / PROMOTE n>=150 + guards),
  volume 60-73 all (2026-07-07 bars: KILL n>=60 if EV<=0|WR<be; PROMOTE n>=150 if EV>=2 &
  WLB>be), diag 74-80 (no bar; settles the mirage question forward).
- Module replay reproduces research exactly: primary n=127/+8.57c, volume n=285/+5.95c,
  diag n=47/+2.04c.

Verification: `python -m pytest tests/ -q` -> 1820 passed, 4 skipped (11 drift tests incl.
migration + nested-book slicing). config_audit --check OK (973 vars).
Deploy-pending: host pull+restart; volume book accrues ~19.6 picks/day -> n=150 in ~8 days.

## Shipped THIS session - drift-hypothesis shadow recorder (record-only forward test)
Run time: 2026-07-06.

Origin: a 34-agent parallel constrained-box strategy search (13M / ask 65-73 / alt / taker,
one-pick-per-interval, shared walk-forward harness) tested 113 hypotheses. 23 claimed
survivors, 2 passed the automated leak-audit, but MY adversarial re-verification overturned
both: S1 (depth/mom/dq carve-out stack) collapses on ablation (overfit); the survivor list
size (23/113) matches the false-discovery rate of pure noise. Decomposing the one plausible
lead (S2 = near-strike+tight-spread+low-flip) revealed the real mechanism: a YES-side drift
effect. Near-strike flips YES from -0.6pts to +6.5pts vs breakeven and does NOTHING for NO
(-3.8 both ways); +low-flip sharpens YES to +8.9pts, diversified across DOGE/BNB/XRP. Economic
story: at-the-money contracts are max-sensitive to the final 13min of price drift; crypto
carries a small upward intraday drift; so near-strike YES is underpriced by the 65-73 market.
Mechanism-first, not pattern-first -- the best candidate the box produced. STILL in-sample
(the YES-narrowing was itself a post-hoc cut); only forward data can confirm.

Implemented `q15_upgrade/drift_shadow.py` (record-only, own SQLite ledger, NEVER trades/notifies):
- Frozen rule: 13M, alt, ask 65-73, distance_sigma<=3e-5 (~34th pctile), flip<=30, side YES;
  one pick/interval ranked by model-market disagreement; else NO PICK. ~9 picks/day.
- Pre-registered bars in scoreboard(): KILL at n>=40 if EV<=0 or WR<breakeven; PROMOTE at
  n>=150 if EV>=+2c AND Wilson-LB>breakeven AND no day>40% of pnl AND >=3 assets.
- Wired into interval_research runner: observe (740-770s band, dedup per window) + resolve
  (grades on champion settlement events). Env-flagged (Q15_DRIFT_SHADOW*, default ON record-only).
- Module replay on the full tape reproduces the analysis: n=123, win 78.9% vs breakeven 70.1%,
  EV +8.73c, Wilson-LB 0.708>breakeven, 4 assets, status ACCRUING (n<150 promote gate).

Verification:
- `python -m pytest tests/ -q` -> 1818 passed, 4 skipped (9 new in tests/test_drift_shadow.py:
  fee/pnl, rule-gate matrix, one-best-per-window idempotency, resolve grading, scoreboard bars).
- `python tools/config_audit.py --check` -> OK, 973 env vars documented/baselined.
- Deploy-pending: on host pull+restart to start the forward sample; read scoreboard() /
  drift_picks table after ~1-2 weeks. KILL or PROMOTE is decided by the frozen bars, not by hand.

## Shipped THIS session - V3 Books 1 & 2 (warn-flip band entry + 10M favorite band)
Run time: 2026-07-05.

Implemented (all default-OFF, paper-only, env-flagged):
- **Book 1 `warn_flip_entry`** (`q15_upgrade/strategy_bots/rules.py`): follows a confirmed
  ultoim_v2 exit-warning flip as a fresh entry on the flip side when its live ask is inside
  the pre-registered 55-75c band (discovery n=58, +11.93c/tr, halves +5.30/+5.74). Dedicated
  runtime path `record_exit_warning_row` (books stay clean); fed from
  `UltoimV2Runner._fire_exit_warning` with the flip side's executable ask at warn time.
  Tiers PRIME (<70c) / EDGE (70-75c), optional staleness floor, chase guidance (ask+1c),
  empirical Wilson-LB EV once n>=30, auto-mute n>=80 / WLB<0.70.
- **Book 2 `fav_10m`**: buys the predicted (favorite) side at the 10M mark inside 85-90c
  (backtest n=656, +3.04c/tr, all four chronological quarters positive). Fed by the
  generalized interval-research mark feed (`_feed_v3_marks`, flag `Q15_V3_FAV10M_FEED`).
  Spread gate (<=6c, fail-open when missing), auto-mute n>=300 / WLB<0.87 (power-matched to
  the ~89% breakeven), exempted from the empirical late-interval delivery guard like the sniper.
- **New Telegram UI** (`strategy_bots/telegram.py`): action-first cards — the BUY line with
  chase limit is the message; tier/freshness, confirmed-flip provenance, live book W-L +
  Wilson LB, after-fee EV with prior labeling; auto-mute notices parameterized per book.
- Env docs in `.env.example` (Q15_V3_WARN_FLIP_*, Q15_V3_FAV10M_*). To go live the owner sets:
  `Q15_V3_WARN_FLIP=true`, `Q15_V3_WARN_FLIP_NOTIFY=true` (Book 1, validated — deliver now),
  `Q15_V3_FAV10M=true`, `Q15_V3_FAV10M_FEED=true` (Book 2 recording; keep
  `Q15_V3_FAV10M_NOTIFY=false` until its pre-registered forward bar passes: n>=600 forward,
  Wilson-LB win > ask+fee breakeven, forward EV >= +1c).

Also shipped THIS session - hard one-pick-per-window guarantee for BEST TRADE 13M:
- Owner requirement: exactly one 13M pick per 15m window. Two-phase fire in the
  interval-research runner: PRIMARY in sr [740,770] once >= min_assets scored (unchanged);
  FALLBACK in sr [600,740) fires with whatever slate exists (>=1 asset; card annotated
  "late/thin slate (fallback fire)", profile `pick_phase`); below sr 620 with zero
  scorable captures a one-shot "NO PICK - data gap" card keeps the cadence visible
  (durable per-window claim; `send_top_pick_gap_notice`). Single-asset slates handled
  (no runner-up). At-most-one unchanged via the existing claim.
- Limits stated honestly: the guarantee holds while the app process runs; a dead process
  or fully dead feed cannot self-report (the export watchdog gap remains open).

Also shipped THIS session - v3.1: graded BEST TRADE cards (research-first, max-depth probed):
- Research findings (n=1,078 top-picks, oracle-graded, all halves/quarters-validated):
  BTC+ETH picks are a stable losing cell (-3.92c/tr; negative in all four quarters, both
  assets independently, every hour block; bootstrap P(worse)=96.3%; NOT a depth proxy).
  Mechanism confirmed in calibration curves: majors' win rate sits below ask+fee breakeven
  in every price bucket (efficient books); alts sit at/above (the harvested inefficiency).
  Hours effect (06-12 UTC) DOWNGRADED to hypothesis (h1 flips sign under boundary shifts).
  Rank-demotion of majors tested and REJECTED (+0.29c vs +0.25c - replacement alts in
  majors-led windows are junk); v3.1 therefore keeps the ranking untouched and GRADES the
  card: TRADE (alt fav band, +1.21c), CAUTION (alt 60-80c, +1.55c pooled, decays),
  SKIP (majors or out-of-band, -2.90c with h1 -2.96 / h2 -2.84 - the stablest cell found).
  Traded set (non-SKIP): +1.49c/tr at ~58/day vs +0.25c baseline; card count unchanged (~81/day).
- Implementation: `_pick_grade` in the interval-research runner (env
  `Q15_V3_TOP_PICK_SKIP_ASSETS=BTC,ETH`), grade fields on the source row + threshold
  profile (`ranker_version: v3.1-graded`), grade line on the card
  (✅ TRADE / ⚠️ CAUTION / ⛔ SKIP with reason). Ranking, cadence, books untouched.
- Pre-registered forward bars: SKIP cell must stay <= 0c at forward n>=150 (else grades
  recalibrate); H2 (06-12 UTC) needs >= 3c forward gap at n>=150 before any use.

Also shipped THIS session - BEST TRADE 13M upgrade + defaults ON (owner directive):
- Owner directive 2026-07-05: "always give me one [pick per cycle] ... highest profit" and
  "make everything on by default". Implemented:
- **Profit-ranked selection**: the per-window pick now ranks the slate by measured
  per-price-bucket EV at 13M (oracle-graded, n=969 cycles; 85-90c favorite band +0.35c is
  the only ~breakeven+ cell) with market-extremity and conviction tiebreaks. Card renamed
  **"V3 BEST TRADE 13M"** with a BUY/chase line, cell label (FAV-BAND vs FALLBACK with its
  measured EV), and "size SMALL / warn-flip cards outrank this" mode line. Fires every
  cycle (~96/day when notify on).
- **Defaults flipped ON** for the three new books and their NOTIFY flags:
  Q15_V3_WARN_FLIP(+_NOTIFY), Q15_V3_FAV10M(+_FEED, +_NOTIFY), Q15_V3_TOP_PICK_13M(+_NOTIFY).
  Older books (13M sniper etc.) keep their existing defaults. Delivery still requires
  Q15_V3_TELEGRAM_ENABLED + chat id. Affected tests updated (fav_10m adds one decision row
  per ultoim_v2 10M source row; disabled-path tests now set flags false explicitly).

Also shipped THIS session - Top Pick 13M display alert:
- **`top_pick_13m`**: one card per 15m window — the single most market-extreme call across
  the asset slate at the 13M mark (backtest top-1 accuracy ~74.4% ranking by |candidate
  ask - 50|; model-conviction tiebreak). Fired from the interval-research runner
  (`_maybe_top_pick_13m`, sr 740-770 firing band, min 3 scored assets), recorded via
  `runtime.record_top_pick_row` with a durable once-per-window meta claim, resolved by the
  normal ultoim_v2 reconcile so live accuracy accrues. The card is explicitly display-only
  ("not a trade signal — EV after fees is negative"); no existing alert path touched.
  Flags: `Q15_V3_TOP_PICK_13M`, `Q15_V3_TOP_PICK_13M_NOTIFY`, `Q15_V3_TOP_PICK_13M_MIN_ASSETS`
  (all default OFF/3; documented in .env.example).

Verification:
- `python -m pytest tests/ -q` -> 1799 passed, 4 skipped (includes 26 new tests in
  `tests/test_v3_new_books.py`: gate matrices, staleness/auto-mute governors, notify gating,
  message content + champion-marker safety, both feeds, end-to-end warn-flip recording).
- `python tools/config_audit.py --check` -> OK, 965 env vars documented/baselined.
- Deploy-pending: flags above are not yet set on the local Windows host; books are inert
  until the owner enables them.

## Local THIS session - runtime flag persistence + measurement watchdog
Run time: 2026-07-03T05:23:00Z.

Implemented:
- Confirmed the local Windows startup path is `scripts/local/Start-Q15Local.ps1`, which imports `.env.local`
  through `Import-Q15Env` before launching `python app.py`, `tools/github_relay.py`, and
  `tools/learning_export.py`. The old `.replit` path is not used for this host.
- Persisted the required local flags in `.env.local` and mirrored them in checked-in `.env.local.example`:
  `Q15_STRANGLE_SHADOW=true`, `Q15_STRANGLE_SHADOW_OPEN_MARKS=780,600,420`,
  `Q15_FEED_PATH_RECORDER=true`, `Q15_FEED_LADDER=true`, `Q15_FEED_MARKET_ACTIVITY=true`,
  `Q15_FEED_SETTLE_INDEX=true`, `Q15_FEED_LIQ=true`, and
  `Q15_ULTOIM_V2_DELIVERY_QUALITY_GUARD=true`.
- Added `tools/expected_runtime_flags.json` plus `startup_config_manifest.py`. At boot it compares the
  checked-in manifest to the live env, logs one summary line, exposes `startup_config_manifest` on
  `/api/health`, and sends one persisted-cooldown direct Telegram ops page via the heartbeat pager path if
  required flags are missing/off/wrong. It is diagnostic-only and never gates behavior.
- Added `strangle_shadow_health()` and registered `strangle_shadow` in the feed watchdog using
  `MAX(created_at)` from `strangle_windows`.
- Tightened feed-watchdog coverage so enabled-but-empty collectors age from process start instead of being
  silently ignored, and routed feed-watchdog pages through the dependency-free ops pager so
  `Q15_ALERT_LEVEL=balanced` cannot mute stale-collector pages.

Verification:
- Baseline before edits: `python -m pytest tests/ -q` -> `1761 passed, 4 skipped`.
- Focused after edits: `52 passed` across manifest, strangle, export, health, and watchdog tests.
- Final full suite: `python -m pytest tests/ -q` -> `1772 passed, 4 skipped`.
- `python tools/config_audit.py --check` -> OK, 947 env vars documented/baselined.
- `python -m py_compile app.py routes/api_core.py cycle_watchdog.py strangle_shadow.py startup_config_manifest.py tools/learning_export.py` passed.

Restart/export verification:
- Restarted local with `scripts/local/Stop-Q15Local.ps1 -IncludeStale` then
  `scripts/local/Start-Q15Local.ps1 -SkipInstall`; health returned `status=ok`.
- Startup manifest health: `expected_count=8`, `mismatch_count=0`, `ok=true`.
- Pre-restart DB counts: `strangle_windows` missing, `window_paths=52`, `ladder_captures=439`,
  `market_activity_samples=7871`, `settlement_index_ticks=7376`, `liquidation_events=0`.
- Published learning snapshot `2026-07-03T05:19:51.705846+00:00`: `strangle_windows=49`,
  `window_paths=66`, `ladder_captures=544`, `market_activity_samples=9607`,
  `settlement_index_ticks=9134`, `liquidation_events=0`.
- Local live DB counts at `2026-07-03T05:22:59Z`: `strangle_windows=49`, `window_paths=66`,
  `ladder_captures=558`, `market_activity_samples=9761`, `settlement_index_ticks=9275`,
  `liquidation_events=0`.

Remaining blockers:
- The requested success sentence (`strangle_windows is accruing rows and every collector count is moving in
  the hourly export.`) is NOT yet true because `liquidation_events` remains 0 after 30+ minutes.
- Binance futures appears blocked/silent from this host: Binance futures REST returned HTTP 451 and configured/all-market
  forceOrder plus a mark-price control WebSocket received 0 messages during probes. The app reports
  `liq_feed.connected=true`, `last_error=null`, but `last_message_age_seconds=null` and feed watchdog marks
  `liq_feed` stale. To make liquidation rows move on this host, use an allowed network/proxy/VPN for Binance
  futures or implement an alternate liquidation provider.
- Coinbase Advanced L2 is still stale for a separate credential reason: SDK is present (`coinbase-advanced-py 1.8.4`),
  but startup logs `Coinbase Advanced L2 not started: bad/missing CDP key: [Errno 2] No such file or directory`
  for the configured CDP key path, and `/api/health` reports `coinbase_adv_l2.status=missing_or_bad_key_file`.

## ✅ Shipped THIS session — microstructure research bridge (tools/micro_extract.py)
The deep-microstructure sources exist on this host (spot_depth_snapshots 1.1M rows / 683MB,
kraken_l3_summaries 1.09M rows / 926MB incl. full bid/ask level arrays; kraken_l3_events is 0 —
raw events were never stored, summaries are max historical resolution) but are export-excluded,
so research containers could never see them. New `tools/micro_extract.py`: per settled window,
slices both sources over the final 900s, downsamples to ≤300 pts/source, computes compact
feature vectors incl. a WALL metric (max/median level size + distance from the levels_json
arrays), gzips one row per (asset, close_time) into `q15_micro_paths_v1.sqlite3` (registered in
the export; ~3-5MB/day). Idempotent, read-only sources, 5 tests. **Owner: set
`Q15_MICRO_EXTRACT=true`** (with the other flags) — the export loop then runs it each cycle.
PRE-REGISTERED STUDY (run when ≥5 days of paths exist; oracle-graded, chrono OOS, both halves,
vs the base flip-rate curve 35.7%@10m/17.8%@5m/9.6%@2m):
 H1 refill half-life asymmetry after depletion → direction over remainder;
 H2 cancel-to-add burst near strike → flip within 120s (exit-engine accelerator);
 H3 avg order-size shift (avg_bid vs avg_ask order size divergence) → fill toxicity for the
    strangle quoter (condition its TTL);
 H4 wall appearance/pull (ratio & distance path) → pin vs break;
 H5 5s-bucket signed-flow imbalance (VPIN-ish) → flip-risk forecast quality vs flip_probability.

## ✅ Shipped THIS session — strangle quoter MULTI-ROUND mode (surface multiplication)
Strategic reframe after 3 hunt rounds (~40 hypotheses, ~4 survivors): stop hunting new per-trade
edges; multiply the surface of validated mechanisms. `Q15_STRANGLE_SHADOW_OPEN_MARKS="780,600,420"`
re-quotes the strangle at each mark after the prior round resolves (pin premium measured 1.62x at
13M decaying to 1.25x at 7M — three harvests per window instead of one). Per-round 120s TTL, same
hedge logic, UNIQUE(asset,close,round). Legacy DBs migrate (ALTER) and degrade to single-round
until rotated. +2 tests (suite 1761/4). Owner: set OPEN_MARKS="780,600,420" alongside
Q15_STRANGLE_SHADOW=true. Remaining multiplications (briefs pending owner go): global late-flip
watcher (10x surface on the +3-4c warn-time entry — the only surviving taker edge) and the
new-listing monitor (the day-one asleep harvest as a calendar event).

## ✅ Shipped THIS session — hunt round 2 verdicts + quoter instrumentation
Round-2 fleet (5 miners + synthesis) results, folded into the 13M book:
- **KILLED:** taker latency bot (index/mid ~contemporaneous at 2.5s; ≤1.7c capturable < costs);
  standalone fill-fade (−7.1c all cells); fade-side hedge overweight (−18.6c vs hedged-1x — KEEP 1x);
  asleep-session taker retest (−13.5c); session-gated asleep (it's DATE decay, not sessions);
  buy-favorite taker; 15M ladder arb (VACUOUS — one market/window, no ladder exists on 15M).
- **Round-1 numbers superseded:** fill-fade +4.19c → 0 (unproven, artifact-risk; only testable as an
  overlay on REAL quoter fills, pre-registered test in the synthesis); "index leads 3–6s" → book fact,
  not an edge.
- **Best genuine discovery: implied/realized vol = 1.39** (6/7 assets, both halves same sign) — a pin
  premium the maker quoter can passively harvest. One 2.2h day; promotion needs ≥5/7 days ≥5/7 assets.
- **Structural facts now canonical:** liquidity is ASSET-stratified (thin-alts spread≥3c 60–65% always;
  majors 1c) → quoter venue = thin-alts first; base flip-rate curve 35.7%@10m→1.9%@1m is the null for
  all flip claims; depth_contracts are all-zero before 2026-06-27 10:19 (epoch filter forever).
- **Quoter instrumentation shipped** (this commit): utc_hour/ttl/hedge_delay stamped per window,
  spread_at_first_fill, and a 120s post-fill mid path (postfill_mids_json) — the pre-registered
  promotion tests for fill-fade overlay, session TTL, and vol skew all read from these columns.
  Plus Q15_STRANGLE_SHADOW_ASSETS allowlist (venue = thin-alts).
- **OWNER ACTIONS on the live stack:** set `Q15_STRANGLE_SHADOW=true` (start measuring) and
  `Q15_FEED_LIQ=true` (liq feed has ZERO rows — wiring exists, flag was never enabled), restart.

## ✅ Shipped THIS session — shadow strangle quoter (paper maker research, default OFF)
New `strangle_shadow.py` + app-loop wiring + 10 tests + learning-export registration. Places
VIRTUAL both-side maker bids (mid±width) at the ~13M mark, conservative fill detection (the
opposing ask must trade at/below our bid — mid-touch does NOT fill), waits
`Q15_STRANGLE_SHADOW_HEDGE_DELAY_S` (20s) for the second maker fill (→ LOCKED, fee-free
100−2·width) else hedges at the observed ask + slip (→ HEDGED, bounded small loss,
outcome-independent P&L). Restart-safe (UNIQUE window row, never re-quotes). Grades the path-study
finding (+4.8..8.0c/window, n=44 — that sim's both-fill classification had look-ahead; this
module measures the honest policy). **To start measuring: set `Q15_STRANGLE_SHADOW=true` on the
live stack and restart.** Promotion bar (pre-registered, in module docstring): ≥500 windows,
≥15 days, clustered t≥2, both halves positive, both-fill rate ≥40%. DB exports as
`q15_strangle_shadow_v1` on learning-snapshots.

## Local THIS session - Q15 grading repair stages 1-5
Run time: 2026-07-02T04:38:59Z.

Shipped in staged commits:
- Stage 1: unblocked V9.5/challenger grading with a persistent `reconcile_skip` park list, parked-ticker requeue path,
  warning-level fetch/zero-progress logs, one-shot Telegram grading-stall alert, and `tools/backfill_resolutions.py`.
- Stage 2: hourly report now prints `Grading: resolved 24h / backlog / oldest / parked`; `/api/health` exposes the same
  grading block; stale scoreboard/rank-quality lines stamp `(data through MM-DD)` when resolved data is older than 24h.
- Stage 3: Ultoim V2 exit-warning outbox path was already present and tested; `.env.example` now pins the owner posture
  `Q15_ULTOIM_V2_EXIT_WARN_OUTBOX=true`.
- Stage 4: repaired the V3 13M sniper path by feeding interval-research 13M captures into strategy-bot source rows behind
  `Q15_V3_13M_SNIPER_FEED`, widening the ledger helper signatures, and warning once per process on swallowed 13M context
  failures. `.env.example` documents `Q15_V3_13M_SNIPER=true`, `Q15_V3_13M_SNIPER_FEED=true`, and leaves
  `Q15_V3_13M_SNIPER_NOTIFY` as owner choice.
- Stage 5: V2 scoreboard headline/`overall` now counts settled fired NO rows only (`fired_resolved` keeps the total fired
  YES+NO count visible), heartbeat pager falls back to in-memory cooldown if the cooldown file cannot be written, and
  Coinbase Advanced L2 health now guards missing SDK/key failures, exposes snapshot age, and logs one rate-limited WARNING
  when snapshots are stale for more than 10 minutes.

Backfill runbook:
1. On the live host, run `python tools/backfill_resolutions.py --db <live-v95-ledger.sqlite3>` once after deploy.
2. Use `--dry-run` first if you need a no-write count of unresolved past-close tickers.
3. If parked tickers should be retried, rerun with `--retry-parked`.
4. Verify the next hourly report's `Grading:` line shows backlog near 0 and parked count understood.

Verification:
- Stage 5 focused affected suites: `261 passed`.
- Full suite under Python 3.11: `python -m pytest tests/ -q` -> `1749 passed, 4 skipped`.
- Local `python3 -m pytest tests/ -q` remains unsuitable on this Windows host because `python3` resolves to Python 3.13;
  the repo guard requires Python 3.11. `python` is Python 3.11.9 here.

Found during repair:
- `outputs/` is an existing runtime artifact and was left untracked/unstaged.
- The live Coinbase Advanced L2 root cause still needs confirmation from host logs/runtime health. The code now surfaces
  `thread_error`, `thread_exit_age_seconds`, `status`, `have_coinbase_sdk`, key status, and stale snapshot age so a missing
  SDK/key or exited thread is explicit rather than guessed.

## Local THIS session - Q15 collector feeds wired; activation blocked
Run time: 2026-07-02T03:48:52Z.

Implemented and wired five default-OFF read-only collectors plus learning export support:
- `settlement_index.py` (`Q15_FEED_SETTLE_INDEX`): Kalshi CF Benchmarks RTI websocket reference feed, DB
  `data/q15_settlement_index_v1.sqlite3`, candidate/interval additive columns `index_px`, `basis_cents`,
  `index_age_s`.
- `ladder_probe.py` (`Q15_FEED_LADDER`): Kalshi strike-ladder checkpoint snapshots, DB
  `data/q15_ladder_probe_v1.sqlite3`, stores implied sigma/skew/arb flag.
- `market_activity.py` (`Q15_FEED_MARKET_ACTIVITY`): cumulative volume/open-interest/book-staleness/asleep score,
  DB `data/q15_market_activity_v1.sqlite3`, additive candidate stamps.
- `path_recorder.py` (`Q15_FEED_PATH_RECORDER`): bounded final-15m ring buffer, compressed `window_paths`, DB
  `data/q15_path_recorder_v1.sqlite3`. Shakeout found and fixed a missed-rollover flush by calling
  `flush_expired()` each refresh cycle.
- `liq_feed.py` (`Q15_FEED_LIQ`): Binance futures public forceOrder websocket, DB
  `data/q15_liq_feed_v1.sqlite3`, additive candidate stamps `liq_notional_60s` / `liq_side`.
- `tools/learning_export.py` now discovers configured non-default paths for all five DBs.

Verification:
- Focused collectors/exporter: `python -m pytest tests/test_settlement_index.py tests/test_ladder_probe.py tests/test_market_activity.py tests/test_path_recorder.py tests/test_liq_feed.py tests/test_learning_export.py -q` -> `43 passed`.
- Adjacent Ultoim V2 + interval research: `231 passed`.
- Full suite under Python 3.11: `python -m pytest tests/ -q` -> `1695 passed, 4 skipped`.
- Bare `python3 -m pytest tests/ -q` is blocked locally because Windows `python3` resolves to Python 3.13;
  repo/app guard requires Python 3.11. `python` is Python 3.11.9 here.
- Local shakeout with all five flags ON ran across the 03:15, 03:30, and 03:45 UTC rollovers. Health stayed OK.
  Final observed row counts: settlement index 3726, ladder 210, market activity 4026, path recorder 24,
  liquidation events 0. Liquidation websocket was connected, but Binance emitted no tracked forceOrder events during
  the run, so the live liq table did not satisfy the "nonzero rows" activation criterion.
- Final health: status OK, data age ~0.45s, settlement/path/market-activity feed ages fresh, ladder fresh, liq
  connected with null age because no event was received. Slow cycles were startup/rollover-bound and attributed to
  existing `run_cycle`/settlement/report stages, not collector queues.
- Logs had no tracebacks or ERROR/CRITICAL entries for the new collectors. Existing unrelated warnings persisted:
  Coinbase Advanced L2 missing/bad local key path, repeated Kraken crossed-book snapshot refreshes, and missing
  Ultoim V2 exit-warning Telegram channel.

Activation/deploy status:
- Commit `318373b8` (`Auto sync local Q15 changes 2026-07-01 23:14`) is already on `origin/main`; it was created by
  the hourly auto-sync while the shakeout was running and includes these collector changes plus pre-existing local
  13M/strategy-bot edits.
- `.replit` flags were intentionally NOT enabled because the local shakeout did not produce a nonzero
  `liquidation_events` row. To activate later, add the five `Q15_FEED_*` flags set to `true` in `.replit` with a
  restart-required comment, restart the Repl, then confirm within one hour that `learning_snapshot.json` lists the new
  DB tables with nonzero row counts. Do not touch `learning-snapshots`.

## Local THIS session - V3 13M early-entry sniper (activation blocked)
Implemented the local code/test changes for the owner-requested V3 `thirteen_m_sniper` alert, but did **not**
flip `.replit` live flags or merge because the required full-suite gate is not green in this Windows workspace.

What changed locally:
- Restored Ultoim V2 `13M` capture at 780s (`Q15_ULTOIM_V2_ENABLE_13M`, default true), record-only via
  `research_only_intervals`; it records calibrated YES probability, flip probability, entry ask, distance, and
  the existing depth/flow fields without alerting or executor routing.
- Added V3 bot `thirteen_m_sniper` (strategy version suffix `-provisional`): default-off recording flag
  `Q15_V3_13M_SNIPER`; separate default-off Telegram flag `Q15_V3_13M_SNIPER_NOTIFY`.
- Gates: conviction, market-asleep/not-already-priced, flip-safe, 60s spot-flow fail-open contra veto, fee-adjusted
  EV floor using the empirical accepted-slice Wilson lower bound once n>=30.
- Added persistent accepted-slice stats, trailing 70th percentile flow context, V3 Telegram card
  `V3 13M EARLY`, per-ticker/window dedup through the existing strategy-bot ledger uniqueness, and one-time
  auto-mute notice when n>=80 and Wilson LB accuracy <0.55.

Verification:
- Focused suite: `python -m pytest tests/test_strategy_bots.py tests/test_ultoim_v2.py -q` -> `203 passed`.
- Syntax/whitespace: `python -m compileall -q q15_upgrade/strategy_bots q15_upgrade/ultoim_v2` and
  `git diff --check` passed.
- Requested full suite: `python3 -m pytest tests/ -q` cannot collect here because `python3` points at Python 3.13;
  the app exits with "Python 3.11 is required".
- Full suite under Python 3.11: `python -m pytest tests/ -q` -> `1505 passed, 4 skipped, 164 failed, 2 errors`.
  Failures/errors match the already-documented Windows SQLite temp-file lock issue (`PermissionError` deleting
  temp `.sqlite3` files). WSL and Docker are unavailable in this workspace.

Activation status:
- `.replit` was intentionally **not** changed because the owner specified flipping
  `Q15_V3_13M_SNIPER=true` and `Q15_V3_13M_SNIPER_NOTIFY=true` only after the full suite is green.
- When a green Linux/Replit suite is available and those flags are set, the Repl must restart to pick up the
  new env values.

## Shipped THIS session - item 6: rank-inversion scoreboard line
Added a report-only `rank_quality_scoreboard(limit=300)` to the frozen V9.5 ledger. For each checkpoint it
reads the latest resolved rows only, splits #1 vs #2-3 vs rest, computes Wilson CIs, and flags
`rank_inverted` when #1 accuracy trails the non-#1 pool's Wilson lower bound. No champion weights, ranking
formula, challenger promotion, or recalibration behavior changed.

The hourly report now prints compact per-checkpoint lines such as `Rank quality 10M last 300: #1 ...; #2-3
...; rest ... RANK INVERTED` inside the existing `Hourly Report —` panel.

Tests:
- Focused rank-quality tests: `.venv\Scripts\python.exe -m pytest tests/test_q15_learning_scoreboard.py::TestLedgerScoreboard::test_rank_quality_flags_inverted_top_pick tests/test_q15_learning_scoreboard.py::TestHourlyReportScoreboard::test_full_report_includes_rank_quality_inversion_line tests/test_q15_learning_scoreboard.py::TestHourlyReportScoreboard::test_full_report_is_one_pre_panel -q` -> `3 passed`.
- Full suite after item 6 on this Windows runner: `1498 passed, 4 skipped, 164 failed, 2 errors`.
  Remaining failures/errors are still the pre-existing Windows SQLite temp-file cleanup/lock issue.

## Shipped THIS session - item 5: durable heartbeat watchdog pager
Added a durable refresh-loop heartbeat at the top of every production cycle (`Q15_HEARTBEAT_PATH`, default
`work/local-run/q15_cycle_heartbeat.json`) and exposes `heartbeat_watchdog` on `/api/health`. A tiny daemon
supervisor thread starts only for the production infinite loop, checks heartbeat age, and sends a stdlib-only
Telegram page (`Q15 HEARTBEAT WATCHDOG`) when age exceeds `Q15_HEARTBEAT_STALE_SECONDS` (default 120s).
It is page-only: no auto-restart path was added.

The heartbeat pager cooldown is persisted in `Q15_HEARTBEAT_COOLDOWN_PATH` (default
`work/local-run/q15_cycle_heartbeat_cooldown.json`) so restarts do not re-fire immediately. The same
dependency-free path sends one rate-limited `Q15 PROCESS EXIT` page on interpreter exit. Telegram delivery uses
only stdlib `urllib` and never logs token values; tests inject a sender and never touch the network.

Tests:
- Focused watchdog suite: `.venv\Scripts\python.exe -m pytest tests/test_cycle_watchdog.py tests/test_cycle_watchdog_pager.py -q` -> `17 passed`.
- Full suite after item 5 on this Windows runner: `1496 passed, 4 skipped, 164 failed, 2 errors`.
  Remaining failures/errors are still the pre-existing Windows SQLite temp-file cleanup/lock issue.

## Shipped THIS session - item 4: HVF MORE_FIRE_STRICT mute flag
Added `Q15_HVF_MUTE_MORE_FIRE` (default OFF). When the owner flips it ON, `HVF_MORE_FIRE_STRICT` still
records gradeable research rows (`MORE_FIRE_STRICT_RESEARCH`, `delivery_status=RESEARCH`) but never sends
Telegram, never enters the alert table, and does not consume alert slots or same-ticker alert uniqueness.
The static `_RULE_PRIORITY` table no longer carries the 760 MORE_FIRE slot; default behavior restores the
760 boost only through `_rule_priority()` while the mute flag is OFF.

Owner action needed: set `Q15_HVF_MUTE_MORE_FIRE=true` in the live environment to mute this negative rule.
Evidence basis from the request: n=166 resolved, 59.6% accuracy, -1,699c; HVF book without it +281c.

Tests:
- Focused HVF suite: `.venv\Scripts\python.exe -m pytest tests/test_high_vol_flip.py -q` -> `21 passed`.
- Full suite after item 4 on this Windows runner: `1493 passed, 4 skipped, 164 failed, 2 errors`.
  Remaining failures/errors are still the pre-existing Windows SQLite temp-file cleanup/lock issue.

## Shipped THIS session - item 3: Ultoim V2 exit-warning delivery hardening
Added `Q15_ULTOIM_V2_EXIT_WARN_OUTBOX` (default OFF) and `Q15_ULTOIM_V2_EXIT_WARN_OUTBOX_DB` so defensive
exit-warning cards can route through the persistent V9 Telegram outbox when explicitly enabled. The warning
decision and ledger record are unchanged: records are still written first, actual `SENT` credit is only given
after true Telegram delivery, and queued retry rows remain recorded-but-not-SENT until the outbox confirms.

Close-time TTL is enforced before delivery: if a warning is recorded at/after its window close it is marked
`EXPIRED` with `window_settled` and no Telegram/outbox send occurs. Startup now logs an unconfigured V2 exit
channel and exposes a one-shot `Q15 ULTOIM V2 EXIT CHANNEL` alert message for the main app to send through the
canonical notifier, avoiding the broken V2 channel itself. The canonical hourly report now includes a 24h
recorded-vs-SENT line for Ultoim V2 exit warnings so muted/unconfigured delivery gaps are visible.

Tests:
- Focused item 3 suite: `.venv\Scripts\python.exe -m pytest tests/test_ultoim_v2.py tests/test_q15_learning_scoreboard.py::TestHourlyReportScoreboard::test_full_report_includes_ultoim_v2_exit_warning_delivery_gap -q` -> `123 passed`.
- Full suite after item 3 on this Windows runner: `1492 passed, 4 skipped, 164 failed, 2 errors`.
  Remaining failures/errors are still the pre-existing Windows SQLite temp-file cleanup/lock issue; no focused
  item 3 failures remain.

## Shipped THIS session - item 1: Ultoim V2 delivery-quality guard default-OFF
Restored the default-OFF invariant for `Q15_ULTOIM_V2_DELIVERY_QUALITY_GUARD`: code default is now
false, while `.replit` pins it true so live local/Replit behavior remains unchanged. Added direct gate
coverage for the three delivery blocks (HYPE/SOL assets, ask < 60c, spread >= 3c), verified
research_fired stays true, and strengthened the guard-off test to compare byte-identical output against
a neutral guard configuration.

Tests:
- Focused Ultoim suite: `.venv\Scripts\python.exe -m pytest tests/test_ultoim_v2.py tests/test_ultoim_v2_btc_confirm.py tests/test_ultoim_v2_distance_gate.py tests/test_ultoim_v2_inverse_edge.py tests/test_ultoim_v2_risk_tier.py tests/test_ultoim_v2_s15.py tests/test_ultoim_v2_skip_7m_no.py -q` -> `171 passed`.
- Full suite after item 1 on this Windows runner: `1482 passed, 4 skipped, 164 failed, 2 errors`.
  This is improved from the pre-change baseline `1465 passed, 4 skipped, 180 failed, 2 errors`; the
  remaining failures are the pre-existing Windows SQLite temp-file cleanup issue. WSL/Docker are not
  available in this workspace, so a Linux full-suite green run remains blocked here.

## Shipped THIS session - item 2: Coinbase L2 feed-age watchdog and V3 degraded stamp
Added feed freshness state to `cycle_watchdog`: the refresh loop samples Coinbase Advanced L2 DB-backed
snapshot age before V3 notification work, exposes `feed_watchdog` on `/api/health`, and sends one
distinct Telegram page (`Q15 FEED WATCHDOG`, no checkpoint markers) after a feed is stale for 10 minutes
over the 300s threshold. This is page-only; no auto-restart path was added.

V3 Telegram alerts now get a send-time `DEGRADED` line plus `V3_DEGRADED_FEED_*` reason stamp when a
feed is stale, without changing the recorded strategy-bot decision status or champion/ranking behavior.
Coinbase L2 health now also reports DB snapshot ages even when the collector thread is absent, plus
thread alive/exit/error diagnostics so a fatal `_thread_main` exit is visible instead of only logged.

Diagnosis from the current local repo shell: `coinbase_adv_l2_health()` reports DB snapshots about
`184445s` old and `authenticated_key_loaded=False` with the collector disabled in this shell. In the live
app environment, if the flag is enabled but the key path is missing/bad, health should now make that state
and stale DB age explicit; if the thread actually exits, `thread_error` and `thread_exit_age_seconds` will
identify it.

Tests:
- Focused item 2 suite: `.venv\Scripts\python.exe -m pytest tests/test_cycle_watchdog.py tests/test_cycle_watchdog_pager.py tests/test_coinbase_adv_l2.py tests/test_strategy_bots.py -q` -> `92 passed`.
- Full suite after item 2 on this Windows runner: `1486 passed, 4 skipped, 164 failed, 2 errors`.
  Remaining failures are the same Windows SQLite temp-file cleanup blocker recorded above.

Working notes so a fresh session resumes cheaply. See `CLAUDE.md` (architecture)
and `SYNC.md` (Replit sync). Live app on Replit (`python3 app.py`). **The owner
trades REAL money manually off the alerts**, so reliability + honest data
freshness + honest accuracy measurement matter more than new model features.

⚠️ Fresh container: `pytest`/`websockets`/`flask` are NOT preinstalled →
`pip install pytest "websockets>=12.0" flask -q` first. A broken `cffi`/`cryptography`
may need `pip install --force-reinstall --ignore-installed cffi cryptography -q`
(else the two app-level test files error on collection instead of skipping).
Tests: `python3 -m pytest tests/ -q` → **1620 passed / 13 skipped here** (needs
`pip install --user coinbase-advanced-py cffi cryptography` too in a fresh container, else 3 test
files error on collection — env issue, not the diff; skip/error count varies with install state).

## ✅ Shipped THIS session — staged refactor, Stages 1–5 (behavior-preserving)
**Suite 1703 passed / 4 skipped** (up from 1643/13: flask now installed in the container un-skipped
the app tests, and the stages added ~60 tests). Four parallel work streams, each on its own branch,
merged after independent green runs. NO trading-logic changes anywhere; frozen v91–v94 chain,
executor order logic, ledger schemas, and Telegram markers untouched.
- **Stage 1 — config audit tooling** (`tools/config_audit.py`, `tools/config_baseline.json`,
  8 tests): AST inventory of every env read — **899 vars, 317 documented, 582 baselined as frozen
  debt**. `--check` fails on any NEW undocumented var; `--write-baseline` regenerates consciously.
- **Stage 2 — app.py split** (`routes/{api_core,api_v95_books,api_legacy}.py`, route-table test):
  59 route functions moved out verbatim (free names lazily qualified `_app.<name>` against the
  host module passed as `sys.modules[__name__]` — never `import app`, which double-boots under
  `python3 app.py`). app.py 1,449 → 1,103 lines. All 83 url rules + endpoint names byte-identical,
  pinned by `tests/test_route_table.py` + `tests/data_route_table.json`; 16-route client smoke all 200.
- **Stage 3 — run_cycle decomposition** (`checkpoint_v95.py`): pure extract-method, 584 → 63 lines
  + six `_`-methods in call order (`_analyse_cycle_assets`, `_record_cycle_predictions`,
  `_dispatch_research_overlays`, `_deliver_checkpoint_alerts`, `_dispatch_post_cycle_alerts`,
  `_finalize_cycle_state`). Moved blocks verified byte-identical modulo dedent against HEAD~ by
  script; outer try/except scope and `super().run_cycle` untouched; 335 v95/checkpoint tests green.
- **Stage 4 — telegram unification** (`notifications/telegram_client.py`, 18 tests): the three
  book senders (strategy_bots / high_vol_flip / ultoim_v2) were byte-identical in mechanics and now
  delegate to one injectable client; per-adapter ctor/env-gate semantics preserved exactly
  (V3 default-OFF gate, HVF `enabled=` AND, V2 token+chat, `send_with_result` outbox alias).
  **Champion path (`notifier.py`/`outbox_v9.py`) deliberately NOT touched.**
- **Stage 5-lite — CLAUDE.md map updated** for routes/, telegram_client, config_audit. Physical
  file moves were SKIPPED deliberately: the local Windows stack references current paths; move
  churn is risk with zero behavior gain.
- Post-deploy check for the owner: boot the local stack, confirm dashboard + `/api/health` +
  one checkpoint alert with `V9.5 CHECK` marker intact, and watch one full cycle's timing
  (cycle-time budget unchanged; the split is registration-time only).

## ✅ Shipped THIS session — bug fix: 13M sniper auto-mute notice could never send
`strategy_bots/runtime.py:254` calls `ledger.claim_meta_once(key)` but `StrategyBotLedger`
never had that method — the AttributeError was swallowed by the notice's catch-all, so the
auto-mute Telegram notice silently never sent (its test failed on Linux; the Windows runner's
pre-existing SQLite failures masked it). Added `strategy_bot_meta` table +
`StrategyBotLedger.claim_meta_once()` (INSERT OR IGNORE on PRIMARY KEY: durable, atomic,
once-per-key across restarts). `test_13m_sniper_auto_mute_records_and_sends_notice_once` now
passes. **Suite 1643 passed / 13 skipped.**

## ✅ Shipped THIS session — Stage 0 refactor precondition: dedicated guard tests + env docs
**Suite 1620 passed / 13 skipped** (this container). Stage 0 of the owner-approved staged
refactor was "green the suite" (16 gate tests failed while the delivery-quality guard defaulted
ON). The default→OFF flip + test alignment **landed on `main` in parallel from the owner's local
stack** (main also pins `Q15_ULTOIM_V2_DELIVERY_QUALITY_GUARD="true"` in `.replit`, whose header
now reflects the **local-Windows cutover — Replit runtime disabled, local stack is source of
truth**). This branch merged main in (conflicts resolved taking main's side) and contributes the
missing pieces:
- **New `tests/test_ultoim_v2_delivery_quality_guard.py` (8 tests):** default-OFF, byte-identical
  when off, HYPE/SOL block, ask<60 block + 60c boundary, spread>=3 block + 2.9 pass,
  clean-candidate fires, yes_notify also suppressed, missing-spread fails open. The guard's
  data case (ask<60 NOs 39.4%/-1,047c; HYPE -785c; SOL -387c settled) is real — the issue was
  only the DEFAULT.
- `.env.example`: documented the 4 guard vars (previously undocumented).
- Next refactor stages (owner-approved, not yet started): config registry, app.py route/loop
  split (golden-master first), telegram-client unification, rename/move pass.

## ✅ Shipped a prior session — v2 audit tool + drop NO-7M + paper YES notifications (deploy-pending)
**Suite 1507 / 4 skipped** (+28 tests). Two fleets confirmed the 15m taker market is efficient net of
fees (−2 to −4c/contract); the only validated edge is **NO @ 10M** (+11.5c delivered-strict, CI lower
bound > 0, survives LOAO + time-split), while **7M NO is break-even noise** (+0.75c, CI spans 0) that
PR #65 had re-enabled — and the **paper YES harvest** (hiconv + BTC-confirm) had NEVER fired (`no_only`
blocked it) despite being +8.6c/95%-win in-sample. Three changes, all read-only / paper:
- **Drop NO-7M (`skip_7m_no_deliver`, gate `SKIP_7M_NO`):** NO-only 7M DELIVERY skip; research_fired
  unchanged; YES-7M untouched (a global `skip_7m` would kill the good YES-7M). Shipped as a ready,
  default-OFF lever but **LEFT OFF for now** (owner: activate YES only) — `.replit SKIP_7M_NO=false`;
  7M NO keeps firing as before. Flip to `true` later to drop the dilutive 7M NO. `SKIP_7M=false`.
- **Paper YES notifications (`yes_notify_enabled`, gate `yes_notify` signal + isolated runner block):**
  hiconv YES (cal≥0.70 & mkt≥0.60) that BTC confirms now delivers a Telegram NOTIFICATION + records
  fired=1. SEPARATE from the NO `fired` path (NO byte-identical). NEVER routes to an executor —
  `_maybe_execute` is hard NO-only, and the real `yes_live_enabled` bot stays OFF (`Q15_EXEC_YES_ENABLED`
  unchanged). `.replit YES_NOTIFY=true`. This is "turn YES on for alerts," paper-only.
- **`tools/v2_audit.py` (+ `tests/test_v2_audit.py`, 14 tests):** foolproof, auto-updating config-gate
  audit. Auto-reflects EVERY `UltoimV2Config` field + its TRUE env var (AST-parsed from source, so a new
  field/abbreviated env name appears with zero edits). Data-driven `GATES` registry (add a gate = one
  line). Per-gate marginal effect, cumulative C5 stacks per side, per-interval/asset breakdowns, OOS
  (60/40 time-split + leave-one-asset-out), NET-of-fee P&L (recomputed, never trusts gross), deterministic
  bootstrap CIs. Run: `python3 tools/v2_audit.py [--db PATH] [--side NO|YES|BOTH] [--json]`.
- **DEPLOY-PENDING:** changes are merged-to-PR but the live Repl must restart to pick up the new code +
  the new `.replit` env vars (confirm no Replit Secret overrides `Q15_ULTOIM_V2_SKIP_7M_NO` /
  `Q15_ULTOIM_V2_YES_NOTIFY`). Until then the bot runs the prior config.
- **+14 gate tests** (`test_ultoim_v2_skip_7m_no.py`, `test_ultoim_v2_yes_notify.py`).

## ✅ Shipped THIS session — DURABLE per-window cap (restart-reset over-placement fix, BOTH books)
**Suite 1414 / 13 skipped** (+2 tests). A 5-agent audit (triggered by an owner "4 ETH trades instead of 2"
report — which turned out BENIGN: 6 NO-book ETH trades across 6 different windows) confirmed a real latent
durability bug in the SHARED executor: `max_picks_per_window` was enforced only from the in-memory
`PortfolioState.window_count`, which resets to 0 on a process restart. A restart mid-window (Repl
redeploy/crash) + a rolled at-the-money strike could admit >2 entries in one settlement window. Affects the
NO book and the YES bot identically (shared `Executor`).
- **Fix (`executor/{executor,store}.py`):** `Executor.__init__` now calls `_rehydrate_window_cap()`, which
  seeds `window_count`/`window_tickers` from the durable orders store (`ExecutorStore.recent_window_entries`,
  successful `http_ok=1` entries in the last 2h) so the per-window cap AND the dup-ticker guard survive a
  restart. Also closes the place-before-claim gap (the order is recorded in `on_fire` before the runner's
  persistent alert-lock claim, so a crash in between is still counted). Best-effort: store off/empty ⇒
  unchanged behaviour. One change in the shared `__init__` ⇒ both books fixed.
- **+2 tests:** restart-survives-cap (2 entries → fresh Executor on same store refuses the 3rd WINDOW_FULL,
  different window still allowed) and rehydrate-noop-without-store.
- NOT rehydrated (minor, noted): `window_committed_cents` (per-window $ budget) and `open_count` — the
  count cap is the binding guard for the over-placement bug.

## ✅ Shipped THIS session — executor latency: order before Telegram (fill-rate fix)
**Suite 1448 / 4 skipped** (+4 tests). Diagnosis (11-agent workflow, all hops cited file:line): real orders
were built on a **5.4s-avg (8.1s max) stale price** (`snapshot_age_ms`), so the limit stopped crossing and
**~59% of entries RESTED unfilled** (13 FILLED/12 RESTED/4 PARTIAL/3 FAILED of 32). The HTTP order POST
(~136ms) and balance GET (~0ms, skipped) are NOT the bottleneck — the lag was upstream. Root cause: in
`ultoim_v2/runner.py:_record_and_maybe_alert` the **synchronous `telegram.send` ran BEFORE `_maybe_execute`**
on the single serial worker, so every real order waited behind its own pick's Telegram RTT AND every earlier
co-settling pick's record+Telegram. **Fix A (shipped): moved `_maybe_execute` to run before `telegram.send`,
strictly inside the fired + claim_alert-won block.** Safety (both adversarial verdicts SHIP/safe): deterministic
`client_order_id=_coid(window_key,ticker,'entry')` + the unchanged `claim_alert` lock + in-`decide()` gates
make a duplicate/wrong-side/ungated order impossible — the reorder changes only WHEN the order POSTs. Direction
was separately confirmed CORRECT (buy NO = sell YES per Kalshi duality; fills land at ~100-NO_px on the YES
book). NOT yet done (follow-ups): Fix B async/off-worker Telegram, Fix D fresh re-quote in on_fire (re-gated),
price-aware entry offset bump (1->3 only for ask>=75c), and the deeper snapshot-latency cut.

## ✅ Shipped THIS session — LIVE "YES BOT" (separate, isolated real-money YES executor)
**Suite 1412 / 13 skipped** (+15 tests). Owner-directed LIVE: a SECOND executor that trades the **YES**
side (the inverse-v2 rule) **fully isolated from the NO book** — its own `Executor` instance,
`PortfolioState`, orders DB (`data/q15_executor_yes_orders_v1.sqlite3`), `client_order_id` prefix
(`v2xy-`), and alert-lock namespace (`<ticker>|YES`). An adversarial isolation audit (5-agent workflow)
+ a byte-identical-NO test confirm the NO path is unchanged. **THE RULE (10M only):** place a flat **$150**
YES buy when the champion leans YES AND v2 market-implied **P(YES) ≥ 0.55** AND **BTC contemporaneously
bullish (lean ≥ 0.55)** AND asset **≠ BNB** — up to **2 picks/window ($300), NO conviction doubling**.
Sends a clearly-labelled **LIVE order** Telegram alert on each placement.
- **Why these thresholds:** in-sample (~2.6d) the set ran **96% (n=25) / +$1.4k** flat-$150. The two
  loser patterns (BTC head-fake; BNB decoupling) are exactly what `min_btc_lean=0.55` (HARD, no breadth
  override) and the BNB exclusion remove. ⚠️ **THIN / in-sample** — watch across regimes.
- **Executor (`executor/{config,risk,executor,__init__}.py`):** 3 new `risk.decide()` gates
  (`YES_PROB_FLOOR`/`BTC_LEAN_FLOOR`/`ASSET_EXCLUDED`), **inert at default config** so NO `decide()` is
  byte-identical; they **fail closed** (missing signal → refuse). `Pick.yes_prob` added. `on_fire` now
  places `side=pick.side` (NO still→`no`) with a per-executor `coid_prefix` (audit fix: side-agnostic
  `_coid` could dedupe a NO+YES order at Kalshi). New `yes_config_from_env()` + `get_yes_executor()`
  singleton. **`dry_run` reads ONLY `Q15_EXEC_YES_DRY_RUN`** (never inherits the NO book's flag — audit fix).
- **Runner (`ultoim_v2/{runner,config,panel}.py`):** additive default-OFF block in `_decide_interval`
  (after the RESEARCH-YES block) routes the highest-P(YES) 10M YES candidate(s) to `get_yes_executor()`
  via new `_maybe_execute_yes` (reads only `evaluated`/`_gate_ctx`; writes NO NO-path ledger/lock/report
  state; swallows errors). `panel.build_yes_live_alert` (marker-safe LIVE card). `config.yes_live_enabled`
  (= `Q15_EXEC_YES_ENABLED`) + `yes_live_intervals` (default `{10M}`).
- **`.replit` (LIVE):** `Q15_EXEC_YES_ENABLED="true"`, `Q15_EXEC_YES_DRY_RUN="false"`. KILL SWITCH:
  `Q15_EXEC_YES_KILL="true"` (or global `Q15_EXEC_KILL`). Dry-run shakeout: set `Q15_EXEC_YES_DRY_RUN="true"`.
- **+15 tests:** 3 gates incl. fail-closed, byte-identical-NO, side=`yes` + distinct coid, isolation
  (YES on_fire doesn't touch NO state), dry-run independence, factory-None-when-disabled, high-favorite
  band, runner route + YES-namespaced alert, default-OFF no-route.

## ✅ Shipped a prior session — BTC cross-asset entry GATE (LIVE real-money, default ON)
**Suite 1392 / 13 skipped** (+10 tests). Owner-directed LIVE executor gate (not observational — owner
chose "active immediately"). Suppresses an alt-NO entry when **BTC is contemporaneously bullish
(lean≥0.50) OR the complex is risk-on (prior-window breadth≥0.50)** — the regime where alt-NO fails
because the alts co-move up with BTC (P(alt YES|BTC YES)≈65–88%, breadth 76%). **EXEMPTS ≥3-co-trigger
10M conviction windows** (`stake_multiplier>1`) — those run ~100% and are protected by the defensive
exit. New refusal reason `BTC_GATE`.
- **Plumbing:** `runner._observe_sync` computes per-window `(btc_lean, prior_breadth)` into
  `self._gate_ctx` (BTC's contemporaneous market-implied-YES from the cross-asset candidates + new
  `ledger.prior_window_breadth(wk)`), attached to the executor pick in `_maybe_execute`. `executor.on_fire`
  → `Pick.btc_lean/prior_breadth` → `risk.decide` gate. **No same-window-settlement lookahead.**
- **Config (`executor/config.py`):** `btc_gate_enabled` (env `Q15_EXEC_BTC_GATE`, **default True**),
  `btc_gate_lean` (0.50), `btc_gate_breadth` (0.50). Pinned `Q15_EXEC_BTC_GATE="true"` in `.replit` with
  the KILL SWITCH note (`=false` disables instantly, no code change). Shows in `safety_summary()`.
- **Evidence/caveats:** backtest in-sample ~2.6 days (~560 windows): kept-book accuracy 78→90%, trims
  the BTC-up co-settling losers; a $1000 / 10%-per-bet compounding sim ran $1051 (baseline) → $4753 (rule)
  but that magnitude is an **in-sample, path-dependent, compounding artifact — NOT an expectation**. The
  gate only acts on a PRESENT signal (~47% of alt windows have a BTC read; breadth covers the rest) and is
  one regime. Validate across BTC-down days; `=false` to revert. +10 tests (gate cases, conviction exempt,
  thresholds, prior_window_breadth, runner plumbing).

## ✅ Shipped THIS session — settlement-streak signal (observational, +3 tests)
Owner asked "does 3 YES in a row = uptrend?" Live data: weak/noise-level (after 3 YES, next-YES 67%
n=12, CI [39,86] overlaps the 42% base; 3-NO run flips the other way). So added it the disciplined way —
**record + grade, never gates a trade** (champion stays frozen). Files: `ultoim_v2/{config,ledger,runner,panel}.py`.
- `ledger.settlement_streak(mv, asset, before_wk)`: signed consecutive same-outcome SETTLED-window run
  as of prediction time (+N YES / −N NO, time-adjacent only, no lookahead). New nullable column
  `settlement_streak` (migration + `_COLS`).
- `runner._build_row` stamps it on every recorded prediction (guarded; `Q15_ULTOIM_V2_STREAK_SIGNAL`,
  default ON; record-only, a failure never breaks recording).
- `ledger.streak_research_scoreboard(mv)` grades P(next settles YES) by streak bucket (Wilson CI);
  surfaced in the hourly RESEARCH RECAP (`_recap_sync` → `sb["streak_research"]`, rendered in `panel`)
  alongside the other shadow research signals. NOT in the per-trade alert; never changes `side`/entries.
- Promotion path: if "after 3+ YES → YES" holds up over a few hundred more windows, it can graduate to
  influence entries — same as how flow/s15/distance research signals validate before gating.

## ✅ Shipped THIS session — executor sizing: $150/pick + conviction doubling (LIVE real-money)
**Suite 1411 / 4 skipped** (+9 tests). Owner-directed live sizing change on PR #55 (with the V2
conviction rules below). The executor is LIVE (`Q15_EXEC_ENABLED=true`, `DRY_RUN=false`), so this is
REAL money. ⚠️ Leverage on thin (~3-day) data; **no daily circuit breaker is set**, so per-trade size
is the main risk control.
- **`.replit` (live config):** flat per-pick stake **$75→$150** (`Q15_EXEC_FLAT_STAKE_CENTS=15000`),
  hard per-pick cap **$75→$300** (`Q15_EXEC_MAX_STAKE_PER_PICK_CENTS=30000`), and the old
  `Q15_EXEC_STAKE_BY_INTERVAL="10M:10000"` ($100 10M lever) **removed** so 10M is the uniform $150 too.
- **Conviction doubling (`executor/{risk,config,executor}.py`, `Q15_EXEC_CONVICTION_SIZING` default ON):**
  a v2 pick from a >=3-co-trigger 10M window carries `stake_multiplier=2` (threaded `on_fire`→`Pick`→
  `decide`). Owner rule: **"first $150, extras double"** — the LEAD pick of a window stays $150; only the
  **2nd-or-later** pick of a >=3 window doubles to $300 (gated on `window_count>=1`). The per-window budget
  scales with the multiplier so the extra isn't clamped; the hard per-pick cap is **absolute** (NOT scaled)
  so it still ceilings the doubled extra. A 2-pick conviction window commits up to **$450**.
- Reversible: `Q15_EXEC_FLAT_STAKE_CENTS=7500` / `Q15_EXEC_CONVICTION_SIZING=false`. Note: the executor's
  `max_picks_per_window=2` (live) is what makes a 2nd pick — hence any doubling — possible.

## ✅ Shipped a PRIOR session — 1st-pick ask floor (live config, owner-approved)
**`.replit` only — no code change** (the `ENTRY_ASK_FLOOR` gate already exists in `risk.py` and is tested,
`test_executor.py:414-441`). Set `Q15_EXEC_MIN_ENTRY_ASK = "55"` so EVERY pick (incl. the 1st/main) must
have ask≥55c. Previously only the 2nd pick was floored (`SECOND_PICK_MIN_ASK=60`); the main pick had no
floor, so a losing cheap-NO could be the primary bet. Data (ultoim_v2 live, n=230 fired NO resolved):
the loss is concentrated at **ask≤51 (−356c)**; ask≥52 is a positive plateau (52→60 all ≈+1600c, within
noise). 55 sits at the low edge of that plateau. Protects the **$100 10M stake first** — 10M at ask<58
was −7c/pick, the one zone 10M loses. Reversible (set 0). Owner set 56 (PR #54), then lowered to 55.

## ✅ Shipped a PRIOR session — defensive-exit FIX (live-money path) + sooner exits
**Suite 1349 / 13 skipped** (+6 tests). Branch `claude/gifted-thompson-77cd01`.
- **Root cause (confirmed from real data, `learning-snapshots:dbs/q15_executor_orders_v1.sqlite3`):** the
  exit-WARNING layer works (51 warnings, 84% correct, net +624c), but **every defensive SELL failed**.
  All 4 exit orders ever placed were rejected by Kalshi HTTP 400 `reduce_only can only be used with IoC
  orders` — the close was sent `time_in_force=good_till_canceled` with `reduce_only=true`. So 0 positions
  were ever actually closed. (NOT the no-op/no-position theory HANDOFF previously feared — sells DID reach
  the API; they were malformed.)
- **Fix 1 — `executor/trading_client.py`:** a `reduce_only` (sell/close) order now goes out
  `time_in_force="immediate_or_cancel"` (entries stay GTC). Confirmed vs Kalshi docs. IoC is correct for a
  close anyway, and reduce_only caps the count to the held position (so over-counts from optimistic state
  are safe — no position-tracking change needed).
- **Fix 2 — `executor/{config,executor}.py`:** new `exit_limit_offset_cents` (env
  `Q15_EXEC_EXIT_LIMIT_OFFSET_CENTS`, default **3**). An IoC close at ~mid would cancel unfilled; the exit
  now sells `offset` cents UNDER the estimated exit value to cross the resting bid and actually fill.
- **Fix 3 — `ultoim_v2/config.py`:** `exit_watch_from_seconds` default **420→480** (watch from 8M). Live
  data showed 11 correct flips were clipped by the 420s watch-start; watching from 8M fires already-correct
  exits sooner (corr(time-left, recovered)=+0.31) WITHOUT touching the anti-spike gate or decisiveness bar,
  so the 84% precision / false-alarm rate is unchanged. Env-overridable.
- **NOT changed (deliberately):** exit_confirm_cycles/seconds and exit_min_flip_conf — the data can't measure
  looser values (no sub-0.55 / sub-20s rows exist), and the false alarms cluster at the low-confidence edge.
  Also scope note: the executor only ENTERED 10 of ~51 paper picks (it re-gates independently), so only
  positions it actually holds are exitable — separate from the (broader) warning layer.
- Tests: reduce_only⇒IoC vs buy⇒GTC; exit prices under fair value + clamps to 1c + offset=0 keeps fair;
  config defaults (offset=3, watch=480). +6 tests, full suite green.

## ✅ Shipped THIS session — V2 conviction rules (owner-enabled, DEFAULT ON)
**Suite 1396 / 4 skipped** (+11 tests). Two owner-chosen rules on the V2 (`ultoim_v2`) NO-only entry
system, keyed on how many assets co-trigger a NO in the SAME 15-min window this cycle (count of
would-fire entries — known at bet time, **no look-ahead**, top_n-independent). Derived from a
hand-audit of the settled alert ledger (3 days, thin) cross-checked by two workflows:
- **Rule A — `Q15_ULTOIM_V2_SKIP_12M_UNLESS_MIN` (default ON, MIN=3)** `ultoim_v2/runner.py`: skip 12M
  delivery unless >=3 co-trigger; below that the picks downgrade to research (graded, never
  alerted/fired/executed, reason `SKIP_12M_UNDER_MIN`). Purely defensive — 12M loses money as a whole
  (62.5%, −85c) and the lone/pair 12M alerts are the losers. This one genuinely cuts losses.
- **Rule B — `Q15_ULTOIM_V2_DOUBLE_10M_ON_MIN` (default ON, MIN=3, stake 2)** `ultoim_v2/runner.py`: 2x
  the 10M stake when >=3 co-trigger (settled cohort ~86% vs ~74%). **LEVERAGE on the COUNT, not
  correctness — it doubles a losing window too** (a market-wide YES sweep; e.g. yesterday a −193c 10M
  window would become −386c). New `stake_multiplier` column (default 1, migrated); `hypothetical_pnl_cents`
  is now scored at the size taken so the ledger tracks staked P&L (legacy rows stake 1 = unchanged). The
  alert shows `🔥 2× CONVICTION` so the owner (manual sizer) doubles; executor payload carries the size hint.
- Honesty caveats carried in code + `.env.example`: evidence is ~3 days/thin; in-sample backtest showed
  +1242c→+2286c (+84%) but ~28% of that (12M) rests on 2 windows. Both reversible via their `Q15_*` vars.
  In-sample on the bad-window check, Rule A cut losses; Rule B amplified them — owner accepted the leverage.

## ✅ Shipped THIS session — YES-prediction edge audit + 3 gated knobs
**Suite 1385 / 4 skipped** (+10 tests). Merged to `main` via PR #52 (branch
`claude/intelligent-mccarthy-c1gi38`). Deep audit (real `learning-snapshots` data @ 972f92f, 24-agent workflow, all 14 numeric
claims independently re-verified) of why YES underperforms NO and where YES pulls ahead. Key finding:
**YES is NOT less accurate than NO overall (0.673 vs 0.677, n=3164)** — the deficit is a 15M
issuance/selection problem (YES recall 0.385 vs 0.671; model issues YES 35% vs ~46% base rate) that
vanishes by 7M (YES is the only profitable side at 7M, +1.43c). The side cut is a clean symmetric
`>=0.5` (`checkpoint_v95.py`); the lean is upstream in the probabilities, and calibration is correctly
fixing it (70% of raw-YES→cal-NO flips were really NO). V2's "foregone YES" is net **−1079c** (market
prices the ask efficiently) — its NO-only restriction is economically correct. Three default-OFF,
frozen-champion-safe knobs landed (every prior test byte-identical):
- **R1 — shadow scoreboard visibility fix `challenger/ledger.py`**: reporting/marker methods defaulted
  `model_version` to the dead literal `"challenger-v1"`, but live rows are `challenger-v5`, so a bare
  `scoreboard()` (as `tools/learning_export.py` calls) matched 0 rows → snapshot reported `resolved:0`
  while **3164 resolved rows existed**, hiding the challenger's real late-checkpoint YES skill (10M YES
  recall 0.670 vs v95 0.622; BNB +0.206, HYPE +0.124). Now defaults to `ChallengerConfig().model_version`
  (module const `_DEFAULT_MODEL_VERSION`). Reporting-only, no trading path. +3 tests.
- **R2 — 15M YES decision-threshold knob `checkpoint_v95.py`**: `Q15_V95_YES_DECISION_THRESHOLD_15M`
  (default 0.5 = byte-identical), 15M-scoped (10M/7M always 0.5), clamped 0.40–0.60. Lever for the one
  checkpoint where the deficit lives; observational until A/B-validated (global threshold cuts backfire,
  0.707→0.668, so it is deliberately 15M-only). +4 tests.
- **R3 — observational challenger YES-assist `checkpoint_v95.py`**: `Q15_V95_CHALLENGER_YES_ASSIST`
  (default OFF) marks the 10M/7M × BNB/HYPE/XRP/DOGE pocket where the challenger calls YES but the
  champion leans NO. Recorded in the snapshot only — **never** changes `side`/alert (the challenger is
  globally worse, Brier 0.214 vs 0.205, and must not drive a live decision). +3 tests.

Open follow-ups: R4 (class-balanced Platt fit, calibration-side) and R5 (V2 7M YES delivery — likely
net-negative, recommend research-only) were NOT implemented. 15M high-confidence YES is data-thin
(n=10 @ selected≥0.7) — collect before trusting any 15M threshold relaxation.

## ✅ Shipped a PRIOR session — slow-cycle log attribution (diagnostic)
**Suite 1343 / 13 skipped** (+5 tests). On branch `claude/gifted-thompson-77cd01` (draft PR; NOT
merged to main — owner asked for a safe change that can't touch the live money path).
- **Why:** owner's logs showed repeated slow refresh cycles (~10–14s vs the ~1s target; last 14.43s,
  ~5.6s from the 20s "Predictions pause" pager) with the watchdog only naming the opaque top-level
  `run_cycle ~5.5s`. run_cycle's rich internal split already exists (`parent_chain`, `v95_analysis`
  + `v95_sub`, `market_reconcile`, `other`, plus `parent_chain_timing`) but only in `/api/health`,
  which is hard to catch at the exact slow moment.
- **Change (`q15_upgrade/checkpoint_v95.py`):** added `_format_run_cycle_breakdown()` (pure) and a
  throttled WARNING (`slow run_cycle …`, once/60s, keyed `slow_run_cycle`) emitted when
  `_t["total"] >= Q15_V95_SLOW_CYCLE_SECONDS` (default 10s). Diagnostic-only — no decision, alert,
  delivery, or settlement path touched. +5 tests (`tests/test_q15_v95_slow_cycle_log.py`).
- **Open root cause (NOT yet fixed):** run_cycle is ~5.5s on *every* sampled cycle (not the 1-in-30
  reconcile spike), pointing at per-cycle cost — most likely the remote-Postgres round-trips in the
  v94→v91 parent chain (`checkpoint_v91.py:590/595/619/620`) and/or synchronous Kalshi REST. The
  new log will name it live. Next safe fixes (deferred, need owner sign-off as they touch hot paths):
  move the every-30s settlement reconcile off the run_cycle thread (`checkpoint_v95.py:2833-2847`,
  mirror the polymarket/ultoim off-thread shadows) and make the Telegram first-send async.

## ✅ Shipped a PRIOR session — V2 audit fixes + live-execution hardening
**Suite 1322 / 13 skipped** (+18 tests). Plus, on top of the 7 audit recs below: executor latency
telemetry, ORDER/FILL RECORDING (answers "how many orders missed"), and two owner-approved live
config flips — the **10M=$100 profit lever** and a **1c limit offset** (Q15_EXEC_LIMIT_OFFSET_CENTS=1,
fill reliability) — both set in `.replit [userenv.shared]`.
- **Exit-fill visibility — `scripts/exec_preflight.py --fills` (PR #50)**: the diagnostic called
  `fill_summary()` with no action filter, so it lumped entry buys and defensive-exit sells together and
  could NOT answer "did a defensive SELL fire and fill?". Now splits the local-store summary by action —
  prints `ENTRIES (buys)` and `DEFENSIVE EXITS (sells)` separately, with an explicit "no exit-sell orders
  recorded yet" branch (brand-new store, or real exits predating it). Read-only; executor order path
  untouched. +1 test (fill_summary action partition). Verified exit code path is live-wired
  (`runner.py:647 → executor.on_exit` real sell) and that placed orders fill (live store: 4/4, 100%);
  the open item is catching a real exit sell in the store via a Repl re-run.
- **CRITICAL FIX — executor position leak halted the bot `executor/risk.py` + `executor.py`**: optimistic
  `apply_fill` incremented `open_count`/`open_tickers` on every placement but only the rare defensive
  `apply_exit` ever removed them — so positions settled on the exchange every 15 min but were NEVER released
  in memory. `open_count` climbed to `MAX_OPEN=6` and every asset hit `DUP_TICKER` after its first entry ->
  the bot silently STOPPED taking trades after ~6-7 entries (until a restart reset it). Fix: `prune_settled()`
  releases positions whose 15-min window (`window_key=close_time//900`, monotonic) is strictly older than the
  incoming pick's; called in `on_fire` before the caps. Within-window caps unchanged. +3 tests.
- **Daily stop REMOVED (owner-chosen) `executor/{executor,config}.py` + `.replit`**: both
  `Q15_EXEC_DAILY_LOSS_LIMIT_CENTS=0` and `_PCT=0` -> no daily loss circuit breaker. `_refresh_daily_pnl`
  now SKIPS the per-order balance GET when the stop is disabled (removes latency + decouples the bot from
  the shared account so manual trades can't pause it). safety_summary prints "NO daily stop (no circuit
  breaker)". ⚠️ remaining guards: $75/$100 per-pick cap, max-6-open, 1-pick/window, kill switch. Re-enable
  by setting the CENTS env back to 10000.
- **12M expensive-NO slice -> live (gated) `ultoim_v2/{config,gate,runner}.py`**: `Q15_ULTOIM_V2_DELIVER_12M`
  (default OFF) promotes 12M from research-only to live delivery, but ONLY ask>=`floor_12m_ask` (65);
  the cheap <65 zone is suppressed from delivery (ASK_FLOOR_12M) and kept as research. Data: 12M ask>=65
  is 90.9%/+17c/bet (n=11 THIN) vs ask<65 33%/-17c/bet — same expensive-NO edge as 10M. If you set
  `Q15_EXEC_ALLOWED_INTERVALS`, include 12M. To enable: `Q15_ULTOIM_V2_DELIVER_12M=true`.
- **Bot trades independently of manual positions (locked)**: confirmed + test — the executor's
  DUP_TICKER/MAX_OPEN/WINDOW caps are scoped to the bot's OWN in-memory state (no get_positions sync), so
  a manual position never blocks a bot entry. ⚠️ OPEN: the daily stop is `balance - day_start_balance`
  (shared account), so a manual stake reads as a bot loss and can trip the $100 stop — needs a decision.
- **Fill reconciliation `executor/store.py` (new) + `executor/{config,executor}.py` + `scripts/exec_preflight.py`**:
  every placement is recorded with the RAW Kalshi response + a defensive fill classification
  (FILLED/PARTIAL/RESTED/CANCELED/FAILED/DRY_RUN/UNKNOWN) to `data/q15_executor_orders_v1.sqlite3`
  (gated `Q15_EXEC_RECORD_ORDERS`, default ON; best-effort, never blocks an order). `on_fire`/`on_exit`
  log+return `fill_status`. `exec_preflight.py --fills` answers "how many missed" from the LIVE account
  (final fills) AND the local store (immediate). NOTE: `apply_fill` bookkeeping is still optimistic —
  classifier validated against real responses FIRST, then wire the bookkeeping fix (a follow-up).
- **1c limit offset (LIVE, owner-approved)**: `Q15_EXEC_LIMIT_OFFSET_CENTS=1` -> limit at ask+1c
  (clamped to the 50-85c band) so a fast tick-up doesn't leave the NO buy resting. <=1c/contract cost.
## ✅ V2 audit fixes (7 stress-tested recs, all gated/default-OFF)
A multi-agent audit + adversarial stress-test of V2's
live-money path (189 fired NO, 74.6%, +977c) surfaced 2 confirmed live-money defects + 4 backstops,
and a follow-up probe of the recent session found 1 profit lever (interval sizing). All survivors
shipped (rejected: BTC gate / evidence floor / ceiling 78→85 / flip-veto / recal / time-of-day block /
60c ask-floor / first-fire selector / exit-on-every-warning — HURTS, thin, or one-day overfit).
Deploy-pending. Each reversible via its `Q15_*` env var.
- **Latency instrumentation `executor/executor.py` + `ultoim_v2/runner.py`**: `on_fire` now logs+returns
  `snapshot_age_ms` (cycle decision -> order POST, fed by a new `fired_at` plumbed from the runner),
  `balance_latency_ms` (the pre-order balance GET), and `order_latency_ms` (the POST). Pure observability,
  no order-behaviour change. Log line: `executor timing TICKER wWINDOW: snapshot_age=…ms balance=…ms order=…ms total=…ms`.
  Use it to decide whether to cross the spread (`Q15_EXEC_LIMIT_OFFSET_CENTS`). Suite 1318/13.
- **Rec #7 (PROFIT lever) `executor/{config,risk}.py`**: interval-conditioned stake
  (`Q15_EXEC_STAKE_BY_INTERVAL`, e.g. `10M:10000` -> $100 on 10M; default empty = flat $75 for all,
  byte-identical). The override is the stake AND the per-pick ceiling for that interval (supersedes the
  $75 cap), still gated by the $100 daily stop (checked before sizing). Concentrates capital on the only
  +EV-over-breakeven interval (10M 80.6%/+1171c n=103). Probe verdict: +16.2% session, +126c full-sample
  (full lift EXCEEDS session = anti-overfit), +$0.62 on a 23-window out-of-sample holdout. ⚠️ a single
  losing 10M NO at $100 = the -$100 daily stop in one trade (deliberate, gated). 7M downsize NOT shipped
  (net-negative in isolation). Owner chose "10M=$100 only".
- **Rec #1 `ultoim_v2/config.py` + `gate.py`**: `cap_7m_ask` default **True→False**. The 7M ask cap was
  INVERTED on the fuller 189-sample (vetoed ask>72 winners 82.5%/+98c n=40, kept ask<=72 losers
  55.6%/-174c n=27); flipped OFF + reconciled the stale gate.py "DEFAULT OFF" comment. Re-enable via
  `Q15_ULTOIM_V2_CAP_7M_ASK=true`. Thin signal (non-stationary) — documented in the comment.
- **Rec #2 `executor/executor.py`**: `on_fire` now bands/limits on `entry_ask_cents` (the gate's admission
  field + the marketable fill price), not `best_entry_cents` — they differed in 42/189 rows so the gate-
  admitted and executor-accepted sets could diverge (gate admits, executor refuses PRICE_BAND). `entry_price_cents` override still wins.
- **Rec #3 `executor/{config,risk,executor}.py` + `ultoim_v2/runner.py`**: optional executor interval allowlist
  (`Q15_EXEC_ALLOWED_INTERVALS`, default empty = ALLOW-ALL/byte-identical). When set (e.g. `10M,7M`) refuses a
  non-listed interval → backstop so a v2 gating regression can't leak a -EV 15M/12M order to real money.
  `interval` plumbed runner→Pick→decide; kept OUT of `client_order_id` (idempotency unaffected).
- **Rec #4 `ultoim_v2/{config,runner}.py`**: gated fail-CLOSED stale option (`Q15_ULTOIM_V2_STALE_FAIL_CLOSED`,
  default OFF). When ON, an UNKNOWN spot-staleness (None) abstains (STALE_FEED) instead of fail-open firing.
  ⚠️ depth_contracts/quote_age population is an UPSTREAM champion-feed follow-up (still 100% NULL — not wired here).
- **Rec #5 (test-only)**: guard test locking the delivery selector to reward:risk (cheapest ask), net_edge
  tie-break only — net_edge is INVERSE for NO, so this catches a future edge-max regression that would pick losers.
- **Rec #6 (test-only)**: lock test asserting `no_only=True` at BOTH gate + executor (foregone YES is -983c/n=120).

## ✅ Shipped (prior session) — executor ABSOLUTE $100 stop loss (owner-chosen)
**Suite 1304 / 13 skipped** (+3 tests). Owner wanted a hard $-stop, not the %-based one.
- **`executor/config.py`**: new `daily_loss_limit_cents` (env `Q15_EXEC_DAILY_LOSS_LIMIT_CENTS`, default
  **10000=$100**). When >0 it GOVERNS the daily stop (the 20% `daily_loss_limit_pct` is ignored). `safety_summary`
  now prints `stop -$100`.
- **`executor/risk.py`**: `decide()` floor = `-daily_loss_limit_cents` when set, else the % of day-start; halt
  NEW entries (DAILY_STOP) once `day_realized_pnl_cents <= floor`. Exits never gated.
- **`executor/executor.py`**: `_refresh_daily_pnl()` reads the LIVE balance before each `on_fire` and sets
  realized P&L = `balance − day_start_balance` (cash-drawdown basis: a still-open position reads as a loss, so it
  errs toward STOPPING — the safe direction). `_day_start_balance` captured at init from the real balance (live)
  or bankroll (dry-run). No-op in dry-run.
- ⚠️ **Limitations (be honest w/ owner):** (1) in-memory — a Repl restart resets `_day_start_balance`, so the
  day's loss counter restarts (the standing persistence TODO). (2) Post-trade check + $75 flat bets straddle $100:
  after one −$75 loss you're not yet at −$100 so a 2nd entry is allowed; if it also loses you reach ~−$150 before
  the stop trips. To cap realized at exactly $100 you'd need pre-trade budgeting or a smaller stake.
- Reversible: `Q15_EXEC_DAILY_LOSS_LIMIT_CENTS=0` → back to the 20% stop. Tests pin `daily_loss_limit_cents=0`
  in `_cfg`; +3 tests (absolute stop binds, governs over %, fires from a falling live balance).

## ✅ Shipped THIS session — executor FLAT $75/pick sizing, 1 pick/window (owner-chosen)
**Suite 1301 / 13 skipped** (+5 flat-mode tests). Owner switched from %-sizing to a FIXED dollar stake.
- **`executor/config.py`**: new `flat_stake_cents` (env `Q15_EXEC_FLAT_STAKE_CENTS`, default **7500=$75**);
  `max_picks_per_window` default 2→**1**; `max_stake_per_pick_cents` 5000→**7500** (earlier this session).
- **`executor/risk.py`**: `decide()` flat branch — when `flat_stake_cents>0`, stake = that fixed amount
  (per-window budget = flat × max_picks), overriding `per_pick_pct`; still clamped to the hard per-pick cap
  AND to bankroll-on-hand. The `%` path is the fallback when flat=0.
- Net effect LIVE: a flat **$75 on the single best pick per window** (~115 contracts at a 65¢ ask). At the
  ~$252 balance that's ~30%/trade; the −20% daily stop (≈−$50) halts new entries after one $75 loss.
- Reversible: `Q15_EXEC_FLAT_STAKE_CENTS=0` → back to 4%/pick. `safety_summary()` prints `FLAT $75/pick`.
- Tests: `_cfg` pins `flat_stake_cents=0` (existing % tests unchanged); new owner-default + flat-mode tests.

## ✅ Shipped THIS session — ROI-sweep config: edge gate ENFORCED + expensive cap 78 + top-2 (owner-chosen)
**Suite 1296 passed / 13 skipped** (defaults changed, tests updated — no net new tests). A **13-agent ultracode
workflow** (6 sweep + skeptics + synth, 254k tok) searched the v2 config space for highest ROBUST ROI over the
~42h captures, with adversarial verification (both time-halves + leave-one-asset-out). I re-verified every number
with the shared harness (`scratchpad/replay_grid.py`). Findings:
- The remembered **7M-only ~24% ROI is a MIRAGE** — fails the time-split (first-half 47.5% ROI/90% win →
  second-half 2.0%/65%). Skeptics killed it; do not chase it.
- **Verified robust winner (owner picked it):** marks 10M+7M, rank cheap, **edge gate ENFORCED** (no_edge_waive
  off), **expensive_no_ask_hi 78** (was 85), **deliver_top_n 2**. ROI **15.6%→23.4%**, win **72%→81%**, n=73,
  +$11.20@$1; both time-halves positive (27% / 17%), every leave-one-asset-out >0.
- **Config defaults changed** (`ultoim_v2/config.py`, all reversible via Q15_* env): `no_edge_waive` True→**False**,
  `expensive_no_ask_hi` 85→**78**, `deliver_top_n` 1→**2**.
- ⚠️ **This REVERSES the earlier owner-enabled `no_edge_waive=True`.** That waive optimized TOTAL gross $ on a
  smaller sample; the ROI sweep optimizes ROI (return per $ staked) on the fuller data — they point different ways.
  Enforcing the gate trims the expensive 7M tail so the delivered book leans 10M (7M 35→8 bets). The owner chose ROI.
  A gentler, higher-GROSS alternative was offered & declined: exp_hi 78 + top-2 with waive ON → +$15.5@$1, 18.6% ROI,
  keeps 7M. To revert to that: `Q15_ULTOIM_V2_NO_EDGE_WAIVE=true`.
- Tests: `test_owner_default_config_is_aggressive` now asserts no_edge_waive False / exp_hi 78 / top_n 2; the
  edge-waive + expensive-band + 7M-cap mechanism tests pin their own fixture values (waive on / band 85) so only the
  production default moved. Harness + replays live in `scratchpad/` (not committed — analysis artifacts).
- **Still PAPER + one ~42h regime + n=73**: treat as a forward-test hypothesis; re-verify after +30-40 settled picks.

## ✅ Shipped THIS session — 12M reverted to RECORD-ONLY (delivery back to 10M + 7M)
**Suite 1296 passed / 13 skipped** (+1 net test). Owner-directed after a faithful **~42h replay** of the
live config over `interval_captures` (4774 captures → 2614 resolved evals, 87 windows — far more than the
~1 in-sample day 12M was enabled on). The replay drove the REAL `gate.evaluate` + delivery rule and matched
the actual ledger (+7.3¢/bet replay vs +7.1¢ live, so it's faithful). Finding: the DELIVERED **12M** slice
runs net-NEGATIVE (57.6% win at a ~60¢ break-even, −2¢/bet) while **10M/7M carry the book** (+10¢/+21¢ per bet).
- **Change:** `research_only_intervals` default `{"11M"}` → `{"11M","12M"}` in `ultoim_v2/config.py`. 12M
  stays ENABLED (keeps accruing gradeable data) but never DELIVERS — and since the executor only fires on a
  delivered row (`runner._maybe_execute` runs only when `verdict["fired"]`), 12M now **never alerts and never
  trades**. Delivered/traded marks = **10M + 7M** (15M skipped). Fully reversible (drop 12M from the set).
- **Replay of the change:** delivered book 139→93 bets, **68.3%→72.0% win, +7.3¢→+9.7¢/bet** (+$451@$50 over 42h).
- Tests: `test_owner_default_config_is_aggressive` updated; `test_12m_delivers_live_alert_when_enabled` →
  `test_12m_records_only_by_default` + new `test_10m_still_delivers_live_alert`.
- **NOT changed** (offered, owner did not take it up): top-1 ranking stays `deliver_by_reward_risk=True`
  (cheapest ask). Replay suggested ranking by edge/confidence beats cheapest-ask (+9¢ vs +7¢, 78% vs 68% win) —
  left as a measurement note for a future call.

## ✅ Shipped THIS session — executor `--verify-direction` preflight (branch `claude/confident-bohr-soiqy0` → main)
**Suite 1295 passed / 13 skipped here** (no new tests — preflight is a manual diagnostic; deploy-pending on the Repl). The ONE thing blocking real-money live trading was unverified: does our `buy NO -> bid/ask` mapping (`_v2_side_price` in `trading_client.py`) actually put us LONG NO, or backwards into YES? A wrong mapping = the exact opposite position. Resolved it with a definitive, ~few-cents real test instead of a multi-day dry-run.
- **`scripts/exec_preflight.py --verify-direction <ticker|auto>`** — buys 1 NO through the REAL mapping (marketable at a 95¢ limit so it fills at the book), reads the position back (Kalshi convention: positive=long YES, negative=long NO), prints CORRECT vs 🚨 BACKWARDS, then CLOSES exactly what it opened (reduce-only, reversing the *actual* delta side) and re-reads to confirm flat — warns loudly if not. Cost ≈ the bid/ask spread (a few cents), self-closing. Auto-discovers a live 15-min market with `auto`.
- Helpers `_order` / `_pos` / `_verify_direction` added; wired into `main()` alongside `--probe-order`.
- Also removed a dead unreachable second `return` in `trading_client.cancel_order` (leftover from the V1→V2 endpoint migration).
- **NEXT (owner action):** run `python3 scripts/exec_preflight.py --verify-direction auto` on the Repl. If ✅ CORRECT → mapping is proven; clear to flip live (`Q15_EXEC_ENABLED=true`, `Q15_EXEC_DRY_RUN=false`, $50/pick cap holds). If 🚨 BACKWARDS → flip the NO branch in `_v2_side_price` before any real size. **STILL TODO before real money: order persistence + fill reconciliation** (executor snapshot is in-memory; a Repl restart orphans open-position state).

## ⚠️ NEW — Ultoim V2 EXECUTOR (`q15_upgrade/executor/`): opt-in LIVE-ORDER layer (DEFAULT-OFF, DRY-RUN)
**+22 tests.** Owner is building toward automated REAL trading. This is the FIRST code in the repo
that can place a live Kalshi order — it is double-gated and v2 itself stays read-only. **WIRED into
the runner** (`_record_and_maybe_alert` → `executor.on_fire`; exit-warning → `on_exit`), guarded so
it is a byte-identical no-op while disabled (`get_executor()` returns None; hook failures swallowed).
Owner set a **HARD $50 per-pick cap** for live testing. Core built, tested, dry-run-safe. Suite 1294.
- **`config.py`** — `enabled` (default False) + `dry_run` (default True) + `kill_switch` panic env.
  Sizing defaults encode the owner rule: `per_pick_pct=0.04`, `max_picks_per_window=2`,
  `max_per_window_pct=0.08` (correlation guard — picks co-settle ~76%), `daily_loss_limit_pct=0.20`,
  **`max_stake_per_pick_cents=5000` ($50 hard cap/trade)**.
- **`risk.py`** — PURE `decide(pick, state, cfg)`: sizes count from bankroll (integer cents) and
  enforces KILL / WRONG_SIDE / PRICE_BAND / DAILY_STOP / MAX_OPEN / DUP_TICKER / WINDOW_FULL /
  SIZE_TOO_SMALL. Fully unit-tested, no I/O.
- **`trading_client.py`** — reuses the existing RSA-PSS `KalshiSigner` (`kalshi_auth.py`) for signed
  POST `/portfolio/orders`. In dry-run it LOGS the would-be order and touches NO network/signer
  (tests assert this). Idempotent via deterministic `client_order_id`.
- **`executor.py`** — `on_fire(pick)` -> risk -> place/dry-run -> update snapshot; `on_exit()` sells a
  flipped position. `get_executor()` returns None unless enabled.
- **ACTIVATION:** (1) wiring DONE. (2) `Q15_EXEC_ENABLED=true` + `Q15_EXEC_DRY_RUN=true` (+ keys +
  `Q15_EXEC_BANKROLL_CENTS`) on the Repl → logs the orders it WOULD place against live fires for
  several days; (3) verify logged orders + that real fills match the paper ask; (4) only then
  `Q15_EXEC_DRY_RUN=false` (still under the $50/pick cap). **DO NOT flip live until the edge is
  multi-day confirmed AND fills are validated** — the whole book rests on ~1 in-sample day and
  assumes you fill at the ask. `Q15_EXEC_KILL=true` is the instant panic stop. **STILL TODO before
  real money: order persistence + fill reconciliation** (the executor tracks an in-memory snapshot
  only; a restart loses open-position state — fine for dry-run, must be added before live size).

## 🔬 V2 RESEARCH FINDINGS (this session — analysis on the learning-snapshot chart; ALL ~1 in-sample day, treat as direction not forecast)
Data = `interval_captures` (the per-checkpoint chart, ~1151 resolved NO + the YES side) + v95 (637 NO) + v1 (277 NO). Everything is ONE ~17h session — directionally informative, dollar figures unproven. The shipped config (above) encodes the winners; the **dead ends below are the valuable part — do NOT re-chase them.**

**WHAT WORKS (→ shipped in the live config):**
1. **Drop 15M.** -EV on all 7 assets cross-ledger (v95 15M EV −8.7¢, n=308). 10M/7M carry the book.
2. **Edge gate is counterproductive for NO** (the stated edge is INVERSE). Waiving it (`no_edge_waive`) lifts the gate-pass NO book ~+$1.8k→~+$4.1k at $100/bet; the picks it cut were the cheapest, highest-EV winners (~+$35/bet).
3. **Deliver top-3/4 by reward:risk, NOT single best-by-edge.** confidence corr ask = **+0.49**, so "most confident" = "most expensive" = worst reward:risk. Single-best delivered EV ~+$8/bet vs top-3 ~+$31. Live = per-checkpoint (can't pool 10M+7M without look-ahead).
4. **7M ≫ 10M** (closer to settlement, fewer flips): per-bet EV ~+$32–45 (7M) vs ~+$12–27 (10M). reward:risk auto-weights 7M (cheaper asks).
5. **Cross-learner check:** V2's gate on v95 (637 NO) and v1 (277 NO) holds ~80% W/L, EV ~+$10/bet — vindicates the thin 29-trade book on 10–20× the data.

**DEAD ENDS — proven NOT to work OOS (do not rebuild):**
6. **No loss-curbing rule exists on the gated book.** A 7-agent fleet (flow-against-NO, ask-cap, distance/pin, confidence, flip-risk, microstructure, multi-signal) + 2 adversarial validators: ZERO survive time-split + cross-ledger. Residual losses are **time-clustered** (one bad evening, ~7–11pm ET that day), not feature-separable — any in-sample "loss filter" reverses OOS.
7. **`flow-against-NO` (PR #32) is NOT a loss-curb on the gated book** — its value was the 15M losers, which `skip_15m` already drops. corr(flow,win)≈0; carried by 3 DOGE rows; fails cross-ledger. It's shipped as an **info-only alert label**, not a gate.
8. **Per-asset bans don't hold.** SOL looked worst on v95 but is FINE/best on the native chart — the "worst ticker" reshuffles every dataset (noise). v1 is the SAME day as v95 (not independent confirmation).
9. **Time-of-day filters don't hold** — one bad evening, not a recurring pattern (needs multi-day data).
10. **Two-sided (bet YES when model crosses YES) adds ≈ $0.** The YES read is ~75% accurate but the YES ask is ~73¢ when confident (market already priced the flip) → YES side per-contract +0.6¢ to −11¢. NO-only is data-correct.
11. **YES dip-buy doesn't work.** YES picks DO dip after 10M then recover — but the LOSERS dip MORE (63–94% of NO-settling picks dip to ≤60–70¢ vs 29–49% of winners). A dip-buy limit crashes win-rate 75%→58% and goes NEGATIVE. The dip is information (price falling toward the outcome), not a discount.

**ECONOMICS / SIZING (for when real money is sized off this):**
12. Adverse R:R: you buy the FAVORED side (~68–72¢), so a win pays ~+$40 but a loss costs the full −$100 → breakeven win-rate ~69%. Losses are full-stake → managing loss IMPACT (sizing) beats predicting losses (impossible, see #6).
13. **Sizing:** bet a FRACTION of current bankroll (can't ruin), **2–3%** (NOT Kelly — full-Kelly here is ~39%, blows up; a 6-loss streak exists in the data). **Cap per WINDOW** (5–7 co-settling assets = one correlated bet, worst-window −$563 at flat $100). Reward:risk sizing > confidence sizing for $-stakes (confidence = expensive = bad R:R).
14. **Current config on the chart day:** ~86 alerts → +$2,423 net at $100/bet (86% win), ~125 alerts/day. In-sample; treat as shape, not a promise.

**NEXT for a future session:** re-run ALL of the above on v2's OWN multi-day data once it accrues (the live captures are building it). Per-asset / time-of-day / loss-curb / dip-buy / YES-side were ALL one-day artifacts — only re-validate, don't re-derive from scratch.

## ✅ Shipped THIS session — Ultoim V2 net levers ACTIVATED + selective-trader preset (branch `claude/confident-bohr-soiqy0` → main)
**Suite 1272 passed / 13 skipped here** (+6 tests). Owner-directed: make the data-backed profit levers LIVE now (all reversible via `Q15_*` env). Basis: a 6-agent verification workflow + direct re-checks on the FRESH learning-snapshot (`q15_ultoim_v2_v1` delivered book + `interval_captures` 3276 rows). The owner trades **1–2 picks per 15-min window manually** off the alerts, so the preset optimizes for *the single best entry, surfaced early/cheap, plus the sell signal.*

**New owner-default LIVE config (config.py `INTERVAL_MARKS` now `{15M,12M,11M,10M,7M}`):**
- **`cap_7m_ask=True` (NEW, default ON).** At 7M only, veto a NO whose `ask > 72¢`. Evidenced on the **DELIVERED book**: 7M >72¢ is **live net-NEGATIVE** (−57¢/21 bets) vs **+26¢/bet** ≤72¢; that slice both-halves sign-flips. `gate.evaluate` suppresses BOTH `fired` and `research_fired` (tag `ASK_CAP_7M`); 10M/YES untouched. The one delivered-money-backed lever. Revert `=false`.
- **`enable_12m=True` + 12M DELIVERS live** (removed from `research_only_intervals`). 12M entries run cheaper/earlier than 10M for a similar in-sample win (12M@60-69 ~91%, both-halves 93/89, broad per-asset) → better reward:risk. For a 1-pick-per-window trader the redundancy concern is moot (you take ONE entry, not a stack), and the per-CONTRACT alert lock fires each contract once at its earliest qualifying mark. **CAVEAT: 12M rests on ~1 in-sample day (~27 windows); 10M is the more-proven anchor.**
- **`enable_11m=True` but RECORD-ONLY** (`research_only_intervals={11M}`): 11M is the redundant middle between 12M and 10M, so it accrues gradeable data without a 3rd near-duplicate alert. Records the would-FIRE favourites (top-N reward:risk), NOT the max-`net_edge` longshot (edge is inverse for NO). To promote: drop 11M from that set.
- **`deliver_top_n=1` (was 3, default 1 now).** Single best by reward:risk per mark; with 12M/10M/7M firing + per-contract dedup → ~1–2 distinct alerts/window, concentrated on the best pick (settled: ~+13¢/bet vs ~+11¢ taking all, fewer fees). **13M deliberately EXCLUDED (fragile, 82%→65%).**
- Already live (unchanged): `exit_warnings` (defensive-flip SELL alert — backtested net **+19%/trade**: 13 warnings, 11 correct, recovered 202.9¢ − forfeited 39.0¢ = **+163.9¢**), `no_edge_waive`, `expensive_no` (10M), `skip_15m`, `deliver_by_reward_risk`.

**REJECTED (do not add):** raising `deliver_top_n` beyond the selective 1 for the auto book (marginal bets correlated, not significant); raising `ask_lo` (per-bet EV flat → only cuts net); 13M (fragile); prior-window / trend / cross-asset / volume-flow / regime / time-of-day features (all confirmed dead ends). **DO NOT re-enable edge gating for NO** (`no_edge_waive` is load-bearing — NO edge is inverse).

**Per-trade economics confirmed (delivered book, ~29h / 1 day):** v2 **10M hit rate 90.7%** (49/54, CI 80–96%); break-even at 65¢ entry is 64.5%. **OWNER SIZING (chosen): 2 picks per window @ ~4% of bankroll each (~5–8% per-window risk budget).** Rationale: the 2 picks/window co-settle **76%** of the time (correlated, NOT diversifying), so size by the WINDOW not per-trade; full-Kelly is ~25–29% (rides a 90% drawdown — uninvestable); 2@4% gave +64% over the day at a 26% max drawdown (worst 4-loss streak). Do NOT use 15% (worst real stretch −50–72% DD; full-Kelly blows up). **Liquidity ceiling is the hard cap** — thin 15-min books cap the strategy at low-tens-of-thousands of bankroll; compounding projections past that are fiction. **NEXT: confirm the delivered-10M rate holds ≥80% across MULTIPLE days before sizing up; 12M/11M new-window edge must re-confirm forward before 11M promotes / 12M is trusted beyond discretionary use.**

**Promotion gate (before any of this goes default-ON / 11M-12M go live):** accrue 11M/12M research-only over MULTIPLE days (≥30–40 *independent windows*/mark, not per-bet n), re-run both-halves + leave-one-asset-out + window-collapsed Wilson-low > NO breakeven, and re-run the marginal-net test on forward data (the new-window net must be positive/stable — the test 11M FAILED in-sample); confirm the 7M cap still beats >72¢ on the next delivered slice. Visibility note: scoreboard/panel/validate hardcode `(15M,10M,7M)`, so 11M/12M rows grade but are invisible there until those tuples are extended (part of the promotion edit) — query the DB directly meanwhile.

## ✅ Shipped THIS session — Ultoim V2 owner-enabled LIVE config: edge-waive + skip-15M + top-3 reward:risk (branch `claude/sweet-ride-wq61n7` → main)
**Suite 1268 passed / 4 skipped here.** ⚠️ **Behaviour-changing, owner-directed, ONE in-sample day of basis** — all reversible via env. Four V2 defaults flipped ON (owner trades real money off these alerts, so watch volume/results and revert if off):
- **`no_edge_waive=true`** (NEW flag): waives the edge gate (`gate_c`) for NO. The stated NO edge is INVERSE, so `edge>=min` was cutting the cheapest, highest reward:risk NO **winners** (settled record: removed ~+$35/bet picks; gate-pass NO book ~+$1.8k→~+$4.1k at $100/bet). Conf floor + ask band still apply; `min_edge_cents`/best-entry price untouched (clean flag, not a min_edge hack). `gate.py` adds `NO_EDGE_WAIVE` tag. Revert: `Q15_ULTOIM_V2_NO_EDGE_WAIVE=false`.
- **`skip_15m=true`**: fire only at 10M/7M (15M is -EV on all 7 assets cross-ledger). Revert: `=false`.
- **`deliver_top_n=3` + `deliver_by_reward_risk=true`**: top-3 by cheapest ask per checkpoint (7M-weighted). RAISES correlated exposure + alert volume — pair with per-window stake cap + fractional-bankroll sizing. Revert: `=1` / `=false`.

Tests pin the LEGACY defaults in the shared `_config`/`_runner`/`_cfg` helpers so existing tests isolate old behaviour; new defaults asserted in `test_owner_default_config_is_aggressive` + `test_no_edge_waive_fires_sub_min_edge_no`. **NEXT:** this is a live experiment — let v2-native multi-day data accrue, then re-grade (per-asset/time/sizing) before trusting the dollars or sizing up.

## ✅ Shipped THIS session — Ultoim V2 top-N reward:risk delivery (opt-in; default byte-identical) (branch `claude/sweet-ride-wq61n7`)
**Suite 1266 passed / 4 skipped here** (+3 tests). DEFAULT-OFF / reversible — `deliver_top_n=1` + net-edge selection is byte-identical to the prior single-best rule. Backtest motivation (v95 637-NO + native interval_captures chart, ~1 day, in-sample): the single best-by-edge pick per (interval, window) keeps the *expensive* NOs (confidence corr ask +0.49) and drops the cheaper high-payoff ones — delivered EV ~+$8/bet vs gate-pass EV ~+$28. Delivering **top-3/4 by reward:risk (cheapest ask)** recovers most of it (top-4 EV +$33 vs deliver-all +$28, with less correlation). And **7M ≫ 10M** (EV +$45 vs +$12; closer to settlement, fewer flips) — reward:risk delivers more at 7M because 7M asks run cheaper.
- **`Q15_ULTOIM_V2_DELIVER_TOP_N`** (default 1): deliver the top-N candidates per checkpoint/window (one alert per CONTRACT per window still holds via the alert lock). N>1 RAISES correlated exposure (co-settling assets co-move) + alert volume — size per window.
- **`Q15_ULTOIM_V2_DELIVER_BY_RR`** (default false): select by cheapest ask (reward:risk) instead of net-edge. Recommended with TOP_N≥3.
- `runner._decide_interval` now selects top-N (was single `chosen`); RESEARCH-YES dedup keyed on the delivered-ticker SET. **NOTE (live reality):** selection is per-checkpoint (10M decides ~3min before 7M; cannot pool across checkpoints without look-ahead) — closest to the "2×10M+2×7M" config, not the pooled top-4 backtest. **NEXT:** owner enables `DELIVER_TOP_N=3 DELIVER_BY_RR=true` (and `SKIP_15M=true`) when ready; re-grade on multi-day v2-native data before trusting the dollar figures.

## ✅ Shipped THIS session — Ultoim V2 flow-against-NO now VISIBLE on the live entry alert (branch `claude/sweet-ride-wq61n7` → PR #36)
**Suite 1263 passed / 4 skipped here** (+4: PR #32 flow screen +3, live flow warning +1). Brought PR #32's record-only flow screen into this branch (cherry-pick `256c2fd`) AND, per owner directive, surfaced the signal on the LIVE notification — `panel.build_entry_alert` now prints `⚠️ Flow against NO: buy-side flow {f} ≥ {thr} (historical loss-zone — info only, not graded)` directly under the BEST ENTRY line whenever a fired **NO** card has `champion_flow ≥ flow_against_no_threshold` (default 0.6). **Visibility ONLY:** the alert is still SENT (never abstained) and grading/P&L are UNCHANGED — a P&L-safety helper verified the grading functions are byte-identical to main and `gate.evaluate` never reads `champion_flow`/`flow_against_no_threshold` (the warning is display text inserted AFTER the fire decision; `champion_flow` is a nullable record-only column). Shown only for NO + flow≥thr; never for YES, weak flow, or missing flow. **NEXT:** watch the live cards + the recap flow block accrue real `champion_flow` data; consider a true abstain gate only once flow-abstain clears `validate.py`'s bar.

## ✅ Shipped THIS session — Ultoim V2 flow-against-NO research screen (record-only; the #1 loss fix) (branch `claude/elegant-mayer-js11yk`)
**Suite 1216 passed / 13 skipped here** (+3 tests). Deploy-pending — branch + draft PR. Record-only; **changes NO decision**; DEFAULT-OFF.

A four-agent FORENSIC loss analysis (v2 39 losers / v95 250) found the single robust, OOS-validated P&L fix: **abstain on a NO bet placed against strong buy-side flow** (`champion_flow = feature_values["flow"] >= 0.6`) — +513c (+50%) on the v95 NO side, survives a 3-way time-split + leave-one-asset-out, and is the *smart* 15M filter (drops ~8% of picks, keeps the 15M winners blanket-15M-abstain discards). Audited first: this signal does NOT already exist in v2 (v2 read `threshold_interaction` from feature_values but not `flow`); the v2-native proxy `regime_directional==YES_PRONE` IS already recorded + scoreboard-split, so the build complements it. Other agent findings: losses cluster into ~46 effective window-blocks (low power); blanket-15M-abstain and distance-pin-on-NO both FAIL the time-split (don't gate them); early-exit detects (AUC 0.83) but isn't net-positive (move already priced by 10M).

**What shipped (record-only, NEVER gates):** capture `champion_flow` (new ledger col, from `feature_values["flow"]`, like threshold_interaction); `flow_research_scoreboard` measures the would-abstain (flow>=thr) vs keep AND the v2-native regime proxy; rendered in the recap (marker-safe); knob `Q15_ULTOIM_V2_FLOW_AGAINST_NO=0.6`. On the real 70-row NO snapshot the regime-proxy cut already shows it working: abstain n=7 @ 0% / -29.3c/pick vs keep n=63 @ 76% / +15.0c/pick. champion_flow accrues after the Repl redeploys. **NEXT: deploy → watch the recap flow block across sessions → gate only once flow-abstain clears validate.py's bar.**

## 🚀 Deploy / verify workflow (NEW)
- **Ship to main with one command:** `scripts/ship.sh "summary"` — fetches origin/main,
  merges it INTO the work branch first (structural data-safety guard: can only add main's
  content, never drop it; a conflict aborts), runs the full suite (aborts if red), stamps
  `build_info.json`, merges `--no-ff` to main, pushes main + the branch. Replaces the manual
  merge dance (and the stale-local-main trap).
- **Verify the RUNNING Repl app:** open `<repl-url>/version` (human text) or `/api/version`
  (JSON). `build_info.json` is read at app startup, so it reflects the code the process
  actually loaded — if it shows the old build you haven't Stop ▸ Run onto the new code yet
  (the Relay syncs files but the app doesn't hot-reload). `running_commit`/`matches_checkout`
  cross-check the live `git HEAD` against the stamp. Boot also logs a `BUILD …` line.
- **Automatic CI:** `.github/workflows/tests.yml` runs the suite on every push to main + PRs.

## ✅ Shipped THIS session — Polymarket shadow: verified 15m targeting + XRP + smoke test (branch `claude/brave-noether-2u4l5p` → PR + merge)
**Suite 1258 passed / 13 skipped** (no new tests — config default + docs + one read-only script). Verified the EXISTING read-only Polymarket up/down shadow (`q15_upgrade/polymarket/`, gated by `Q15_POLYMARKET_ENABLED`, **default OFF**; wired at `checkpoint_v95.py:2723` observe + `app.py:625` reconcile/report) is correctly aimed at **Polymarket's 15-minute crypto Up/Down markets**: the slug `{asset}-updown-15m-{window_open}` matches a live event (`btc-updown-15m-1777269600`), the markets exist for BTC/ETH/SOL/XRP, and the contract (close≥open, Chainlink) matches the model. Changes:
- Default assets `BTC,ETH,SOL` → **`BTC,ETH,SOL,XRP`** (Polymarket's confirmed 15m set; the Kalshi-only DOGE/BNB/HYPE aren't on Polymarket's 15m board → `NO MATCHING MARKET`).
- Refreshed the `client.py` live-validation note: the slug is now CONFIRMED; only the exact Gamma JSON field names remain to byte-verify.
- Added **`scripts/polymarket_smoke.py`** (read-only) — run it ON THE REPL to confirm discovery + field-parsing against the live API.
- **Caveat:** the Polymarket API is network-blocked from the dev sandbox (403 policy denial on gamma-api/clob), so the final field-name confirmation + enabling must be validated on the Repl (verify it can reach `gamma-api.polymarket.com`/`clob.polymarket.com`). The code fails safe (degrades to `NO MATCHING MARKET`, never crashes production).

## ✅ Shipped THIS session — Ultoim V2 expensive-NO admit band, ENABLED (branch `claude/brave-noether-2u4l5p` → PR + merge)
**Suite 1258 passed / 13 skipped here** (+12 tests: 11 `tests/test_ultoim_v2_expensive_no.py` + 1 runner e2e; the lone ask>72 ceiling test was updated for the new NO ceiling). The one P&L-ADD lever that cleared the owner's "raise P&L without adding net losers" bar (found by the expensive-NO research fan-out; the parked item).

On a **non-15M** interval, a NO candidate with ask in `(ask_hi=72, expensive_no_ask_hi=85]` is now ADMITTED even with sub-min stated edge: `gate.evaluate` lifts the ask ceiling AND waives the edge gate for that band only (`expensive_no` verdict flag, tagged `EXPENSIVE_NO_ADMIT`). The confidence floor and ask_lo still apply; 15M and the YES side are never touched; mutually exclusive with the 15M distance gate. The card shows the ask as the entry (not a never-fill 72). **DEFAULT ON** — `Q15_ULTOIM_V2_EXPENSIVE_NO=false` opts out (byte-identical plain band); `Q15_ULTOIM_V2_EXPENSIVE_NO_ASK_HI` (default 85) caps it.
- **Basis:** for NO the model's stated edge is INVERSE (the best NO entries carry negative edge). The (72,85] band wins **83.8% over n=893 (v1+v95+v2)**, net-positive after the expensive losses; on v2's own gate it took the delivered book 80%→85% win / +434→+564¢. Held at 85 — the (85,100] slice is net-negative (thin max-profit lets the rare loss dominate), so NOT raised to 100.
- **Higher-variance than the distance-gate CUT** (it's an ADD — more bets; ~84% win ⇒ ~1 in 6 lose ~80¢), but +EV across the pooled ledgers and reversible via the flag.

## ✅ Shipped THIS session — cycle-timing perf fixes: 3 unbounded-growth leaks (branch `claude/brave-noether-2u4l5p` → PR + merge)
**Suite 1246 passed / 13 skipped here** (+17 tests). **Behavior-NEUTRAL** — no probability/edge/side/alert/prediction change; champion weights frozen; read-only preserved. Diagnosed by a 3-agent fan-out, fixed by 3 more on disjoint files; all diffs reviewed + full suite green before merge.

The ~1s `refresh_loop` was degrading over runtime: several collections grow unbounded (no pruning/retention anywhere) and are scanned every cycle — classic O(n)-in-growing-data slowdown. Three independent leaks fixed:
- **`ledger_v95.status()` ran ~5 full-table scans EVERY ~1s** (`checkpoint_v95.py:2860`, outside the 30s throttle) over the unpruned `predictions` table — and it's DISPLAY-ONLY (the alert "pushed accuracy" line + `/api/health`). Now TTL-memoized (`_STATUS_CACHE_TTL_SECONDS=15`, `time.monotonic`, deep-copied so callers can't mutate the cache; deliberately NOT coupled to `_data_version` so calibration-bust cadence is unchanged). Plus 4 idempotent indexes: `predictions(ticker) WHERE official_result IS NULL`, `timing_experiment(contract)`, `flip_decisions(contract)`, partial `predictions(model_version,resolved_at) WHERE official_result IS NOT NULL`.
- **WS ticker-dict leak** — `ws_client._books`/`_trades`/`_market_status` and `hybrid_data._ticker_sources` were never evicted as 15m markets roll over (~28 new tickers/hr), and `health()` rescans them ~8×/cycle on the hot path. `subscribe()` now evicts entries not in the desired set (lock-safe, keyed off the authoritative desired set — never age, so an active book is never dropped).
- **`window_focus._cycles`/`_rankings`/`_top_by_close`** grew ~672 keys/day, scanned O(n) every reconcile. New `_prune_settled_cycles` (2h retention) evicts graded+old cycles (re-hydrate from Postgres on demand via `_hydrate_cycle`); ungraded/recent/current/unparseable always retained.
- **Confirmed NOT the cause:** the ultoim_v2 overlay + this session's distance-gate / interval-split / research_fired work all run on the throttled worker/recap thread, off the hot loop. **Confirm live:** `GET /api/health → cycle_watchdog` → `slowest_stage` / `worst_stage_seconds` (expect `run_cycle` to fall and stop climbing).

## ✅ Shipped THIS session — Ultoim V2 distance gate ENABLED + research-population honesty + interval-split (branch `claude/brave-noether-2u4l5p` → merged to main)
**Suite 1229 passed / 13 skipped here** (+16 tests: 8 `tests/test_ultoim_v2_distance_gate.py`, 6 `tests/test_ultoim_v2_research_fired.py`, +2 interval-split in `tests/test_ultoim_v2_research.py`). Built by parallel agents on disjoint files; verified + merged via PR. **The distance gate is now ON by default (owner directive)** — set `Q15_ULTOIM_V2_DISTANCE_GATE=false` to opt out (byte-identical no-gate). The overlay master switch `Q15_ULTOIM_V2_ENABLED` is unchanged (still env-controlled, default-OFF).

Prior sessions surfaced `distance_sigma` (near-strike NO loses, far NO wins) as a record-only recap screen but never let it act. Verified on v2's settled book the toxicity is **15M-specific**: 15M near-strike NO 1/5 −157¢, while 10M near +12.5¢ / 7M near +40¢ are *profitable* — so a blunt all-interval gate would cut winners. This session adds the narrowly-scoped lever + a counting-honesty fix, nothing else:
- **Distance gate — NOW ENABLED** (`Q15_ULTOIM_V2_DISTANCE_GATE`, default **true** by owner directive; reuses `Q15_ULTOIM_V2_DISTANCE_PIN_SIGMA=0.15`). It ABSTAINS (suppresses paper DELIVERY only — `fired`; `research_fired` is UNCHANGED so the recap's distance scoreboard keeps measuring) on **15M NO** candidates with `|distance_sigma| < pin`. **10M/7M and YES untouched.** `gate.evaluate` gained a keyword-only `interval`; `runner._decide_interval` passes it; new `NEAR_STRIKE_PIN` reason. Fail-open on missing distance; `==pin` is FAR (strict `<`).
- **`research_fired` undercount fix** (record-only) — the research-population predicate (`ledger._research_fired`, `validate._is_gated`) fell back to delivered `fired`, which is structurally 0 for YES rows, so YES research rows whose `research_fired` was backfilled to 0 by the migration were silently dropped from the YES-side / regime N that QUALIFY the promotion verdict. Now derives "passed the gate" from the authoritative `gate_b_pass AND gate_c_pass` (original-schema columns, present on every row; `research_fired==1` ⇔ both pass), keeping the old `research_fired`/`fired` fallbacks for fixtures lacking those columns.
- **Interval-split distance research** (record-only, recap) — `distance_research_scoreboard` is now interval-aware (`by_interval` 15M/10M/7M near/far) and `panel.build_recap` renders it, because the aggregate near-pin reads BENIGN (+7.8¢ on the live book) only because profitable 10M/7M near entries MASK the toxic 15M near-strike bucket (−31¢). Surfaces the exact 15M bucket the gate keys on — and whose N gates the record-first go/no-go — so it's finally observable. Top-level keys unchanged (back-compat); marker-safe.
- **Composition:** a gated 15M near-strike NO row (`fired=0`, `gate_b_pass=gate_c_pass=1`) drops from the delivered book but STAYS in the research population — measurement continues while delivery stops. **Enablement basis + caveat:** the near-strike-NO loss replicates on the v95 champion ledger (n=186, −6.8¢ near vs +5.7¢ far) and is pin-robust (flat 0.10–0.25), but it's a payoff-asymmetry edge **not yet p<0.05** on the small delivered 15M sample (n=8) — enabled on the owner's informed call, fully reversible via the env flag. Projected delivered book +277¢→**+434¢** (+11.1→+21.7¢/entry). **Deliberately NOT added** (our own 3-agent research rejected them): `min_conf 0.62` (stacking *lowers* P&L to +339¢ — it cuts the inverse-edge winners), regime/exit gating (n=7/4, not yet significant), and tightening ask_hi/min_edge (would delete the winning expensive-NO bucket). **NEXT:** watch the interval-split recap block; revisit promotion when delivered 15M clears n≥50.

## ✅ Shipped (prior, now on main) — Ultoim V2 review-consensus additions (record-only + robustness) (branch `claude/elegant-mayer-js11yk`)
**Suite 1213 passed / 13 skipped here** (+7 tests, new file `tests/test_ultoim_v2_research.py`; −1 dead test). Deploy-pending — branch + draft PR. All record-only / robustness; **changes NO decision**; DEFAULT-OFF.

A four-agent review of v2 (perf C−, code A−/B+, shadow features: 1 of 6 works) found the gate adds ~0 selection lift over "bet NO" (calibration works, but fire/skip doesn't select), and the research machinery wasn't surfaced. The agents' consensus "add list" — implemented here, none of it a new gate:
- **Surface the research screens in the recap** — `s15_research_scoreboard` was built+tested but never displayed; added it plus a NEW `distance_research_scoreboard` (near-strike "pin" vs far split — `distance_sigma` is the one record-only feature shown to TRANSPORT across ledgers). Both rendered in `panel.build_recap` as SHADOW blocks (marker-safe), wired into `_recap_sync`. New knob `Q15_ULTOIM_V2_DISTANCE_PIN_SIGMA=0.15`.
- **Hardened `observe()`** — extracted `_extract_candidate`; the per-asset loop now isolates a malformed asset (one bad analysis can't drop the whole cycle) and guards the `x_market_flow` compute, so the method honours its own "never raises" contract in-module.
- **Tests** — real worker-loop integration (the previously-untested queue+thread+task_done surface), distance scoreboard, recap research-block render + marker-safety, observe isolation; de-duped the research `_agg`; removed dead `screen.size_fraction` (+ its test).
- **Did NOT add** any new delivery gate (data too thin, single NO-heavy session); explicitly deferred the cross-module `_num`/`_wilson` dedup and the env-driven promotion hook (low-value / higher-risk per the agents). **NEXT: deploy main to the Repl so s15/distance actually accrue, watch the recap blocks, gather a 2nd independent (ideally YES-favorable) session before trusting any P&L.**

## ✅ Shipped (prior, now on main) — Ultoim V2 15M selective-entry research SCREEN (record-only) (branch `claude/elegant-mayer-js11yk`)
**Suite 1203 passed / 13 skipped here** (+24 tests, new file `tests/test_ultoim_v2_s15.py`). Deploy-pending — branch + draft PR. Read-only/paper; **record-only — changes NO decision** (no fire/size/alert/delivery effect); scoped to **15M-NO rows only** (10M/7M untouched). Still DEFAULT-OFF (`Q15_ULTOIM_V2_ENABLED`).

A five-agent fan-out over the real settled ledgers (v95 + ultoim_v2 snapshots) found the **unfiltered 15M-NO book is a money-loser (~−2c/pick)** — 15M is a near-the-money coin flip — but a *selective* subset is profitable, and the signal is **selection, not forecasting** (model confidence/net-edge is INVERSE for NO; confirmed across v1, v2, v95). Robust core that survived a time-split: **LUKEWARM** (`selected_probability < ~0.55`) **& CHEAP** (`ask ∈ [~47,60)`; expensive NOs ≥60c are ~−13c/pick), lifting ~−2c → ~+18c/pick; plus two orthogonal **tilts** (record-only, not hard gates): **CAL_DRIFT** (`calibrated_yes − raw_yes ≤ −0.03`) and **FRESH** (`seconds_remaining ≥ 875`). The early-EXIT idea (bail on a 15M→10M adverse drift) was REJECTED in synthesis — on the *selected* picks the dip is mean-reverting noise (you bail winners). Caveat: single ~11h session, ~147 picks — direction believable, exact thresholds NOT bankable → ships record-only; promotion needs `validate.py`'s bar on cross-session data.

**What shipped (all record-only, ZERO decision effect, 15M-NO scoped):**
- **`fifteen_min.py` (NEW, pure/no-I/O)** — `evaluate_15m()` / `features()` → `{s15_pass (=LUKEWARM&CHEAP), s15_codes, s15_cal_drift, s15_version}`; all-None for 10M/7M and YES (inert). `S15_VERSION="lukewarm-cheap-1"`. Full rationale + single-session caveat in the module docstring.
- **Config** — 5 tunable knobs (`Q15_ULTOIM_V2_S15_*`); none read by the gate.
- **Ledger** — 4 nullable columns (`s15_pass, s15_codes, s15_cal_drift, s15_version`) via the existing idempotent ALTER migration; `_build_row` stamps every row (mirrors the `screen.py` blowup-shadow pattern), never reads `fired`. New read-only `s15_research_scoreboard()` compares the would-fire subset vs the full 15M-NO book (+ tilt cuts).
- **Tests (+24)** — `tests/test_ultoim_v2_s15.py`: scoping/inertness, core+tilt boundary math, missing-data short-circuit, ledger round-trip + scoreboard, and the two **never-gates** proofs (a confident NO still fires with `s15_pass=0`; a lukewarm NO still abstains with `s15_pass=1`).
- **NEXT (gate to ever influence a decision):** accrue cross-session 15M-NO data, then clear `validate.py`'s bar (n≥50, Wilson-lower > base, p<0.05) prospectively. Until then it only records.

## ✅ Shipped (prior session) — Ultoim V2 pin-break SHADOW signals (record-only) (branch `claude/keen-fermat-ce81uv`)
**Suite 1224 passed / 4 skipped here** (+4 ultoim_v2 tests, 75 total in the file). Deploy-pending — branch + PR #25. Read-only/paper; **record-only — changes NO decision**. Still DEFAULT-OFF.

Outcome of a four-agent search for *what could actually lift v2 P&L* (the whole 08:00-snapshot session is summarized in the PR). Established, with evidence: **no decision-time skip-screen works** — conf_gap, distance_sigma, AND every signal v2 already records fail to separate winners from losers (the strike-pin 10M regime where the losses live is un-screenable; distance even flips sign between ledgers). The cross-system audit (champion v95 n=595 + ultoim v1 n=234) showed the real edge is **short-interval NO** (7M 80.5%/+667¢, 10M 72.5%/+333¢) and **15M is the structural loser** (52.6%/−2297¢) — already addressable via the existing `Q15_ULTOIM_V2_SKIP_15M` flag (built, NOT yet enabled live).

The one new build: record two **pin-break** features, shadow-only, derived from the analysis dict `observe()` already receives (zero champion-code change, zero extra compute):
- **`pin_break_drift`** = `analysis["structural"]["z_score"]` (checkpoint_v95.py:473) — vol-normalised drift THROUGH the strike; the only signal that keeps directional content when `distance_sigma`→0. Mechanistic best bet, but UNTESTABLE on stored data (structural isn't persisted in feature_json).
- **`threshold_interaction`** = `analysis["feature_values"]["threshold_interaction"]` (the scalar; feature_json == feature_values). Backtested on the v95 136-row 10M-NO ledger: **AUC 0.698** (best discriminator found) and the **first signal to admit a winner-sparing OOS cut** (skip ti>0.5 → +38¢, 0 winners touched, holds out-of-sample). Small (1 loser, +6%) — promising, not proven.
- **Files:** `q15_upgrade/ultoim_v2/{ledger,runner}.py`, `.env.example`, `tests/test_ultoim_v2.py` (+4: null-when-absent, persist, record-only-doesn't-change-fire, migrate+roundtrip). Two nullable columns via the idempotent ALTER migration (verified non-destructive on the real snapshot DB). No env knob (always recorded when overlay enabled, like conf_gap; pure dict reads).
- **NEXT:** deploy main to the Repl (it's ahead — live runs 08221c4, main is past PR #25) so these + conf_gap/blowup_risk/x_market_flow actually accrue data; flip `Q15_ULTOIM_V2_SKIP_15M=true`; then validate threshold_interaction's winner-sparing cut prospectively before it could ever gate. Sizing: only fractional-bankroll (risk control) is defensible — edge-tilt was unproven (n=13, one-entry-driven).

## ✅ Shipped (prior session) — Ultoim V2 blowup-risk SHADOW screen (record-only) + four-agent stress test (branch `claude/keen-fermat-ce81uv`)
**Suite 1215 passed / 4 skipped here** (+11 ultoim_v2 tests, 66 total in the file). Deploy-pending — branch + PR #25. Read-only/paper; **record-only — changes NO decision** (no fire/size/alert/delivery effect). Still DEFAULT-OFF (`Q15_ULTOIM_V2_ENABLED`).

The owner asked to add a 10M "blowup defense" using `conf_gap` (= `selected − conservative`, the model's own internal uncertainty), with two hard constraints: (A) never cost a positive-edge win, (B) never add a loss. A four-agent adversarial team stress-tested it on the **fresh 2026-06-23 07:20 UTC** learning snapshot (v95 ledger, 122 settled 10M-NO rows, + the independent ultoim_v2 ledger). **The screen did NOT hold up as a P&L gate** — so it ships as a record-only shadow, not a live filter:
- **No leakage** (the one clean pass): `conf_gap`/`conservative`/`evidence_quality` are point-in-time (`checkpoint_v95.py:1130–1159`); settlement updates only outcome columns (`ledger_v95.py:1999–2003`).
- **Inert out-of-sample:** walk-forward over 102 prospective decisions skipped 0 winners AND 0 losers (P&L delta **+0.0¢**). The 3 in-sample "saves" all predate any deployable decision and share **one settlement window** (effective n≈1).
- **Did not transport:** `blowup_risk` AUC 0.671 in-sample vs **0.491 (chance)** on the independent ultoim_v2 ledger; the 0.30 evidence term is dead weight (≈+0.002 AUC over conf_gap alone). Earlier "AUC 0.759 / clean monotone ladder / T\*=0.522" claims were **refuted** (true AUC 0.67; bootstrap 95% CI on the P&L gain = [0.000, 4.46]¢, lower bound on zero). `T* = max(risk among winners)` makes "no winner touched" **tautological**.
- **The transportable signal is `distance_sigma`** (distance to strike): AUC ~0.615 on the independent ledger, |σ|≥0.15 → ~0% loss. Already recorded on every row; reported alongside blowup_risk so the better candidate accrues data.

**What shipped (all record-only, ZERO decision effect):**
- **`screen.py` (NEW, pure/no-I/O)** — `conf_gap`, `blowup_risk` (constants flagged in-docstring as in-sample-fit-that-didn't-transport), `distance_risk` (the transportable companion), an inert `size_fraction` `(1−risk)²`, and `shadow_features()` → `{conf_gap, blowup_risk, screen_version}`. `SCREEN_VERSION="blowup-shadow-1"`. The full stress-test verdict is the module docstring so no future reader re-fits it blind.
- **Ledger** — 3 nullable columns (`conf_gap`, `blowup_risk`, `screen_version`) via the existing **idempotent ALTER migration** (verified non-destructive on the real 41-row snapshot DB; existing rows backfill NULL). `_build_row` stamps the score on **every** recorded row (delivered NO + research YES); it never reads `fired`/sizing.
- **`validate.screen_shadow_report()` (NEW, read-only)** — over the **gated/fired** population (the population the gate actually fires, not the v95 superset — the methodology fix the audit demanded): rank-based `auc()`, loss-rate terciles, and a **threshold sweep that reports how many WINNERS each cutoff would touch** (the honest test the circular T\* hides). Verdict is permanently `SHADOW_ONLY`; `n_met` stays False until n≥`min_promote_n`. On the current 12-entry fired population: blowup AUC 0.66, distance AUC 0.59 — UNPROVEN.
- **Recap** — one honest line: `Blowup screen (SHADOW · record-only · no effect on entries): N=… · blowup AUC … · distance AUC … · UNPROVEN`. Marker-safe.
- **Tests (+11)** — pure-function bounds/monotonicity/None+bool rejection, `size_fraction` inert-safe, `distance_risk` toward-strike, `shadow_features` shape+stamp, `auc()` known-values+ties, `screen_shadow_report` SHADOW_ONLY+honest+empty, ledger round-trip+legacy-row-NULL+migration, **runner stamps score WITHOUT affecting fire** (high-risk candidate still fires — the record-only invariant), recap shadow line marker-safe.
- **NEXT (gate to ever influence a decision):** re-derive constants + threshold on the **delivered** population with a held-out/forward test, and clear `validate.py`'s existing bar (n≥50, Wilson-lower > base, p<0.05) prospectively. Until then it only records. Prefer investigating `distance_sigma` as the real signal.

## ✅ Shipped THIS session — Ultoim V2 defensive-exit / flip warning (branch `claude/keen-fermat-ce81uv`)
**Suite 1204 passed / 4 skipped here** (+7 tests). Deploy-pending — branch + PR #25. Read-only/paper; default-ON when the overlay is enabled (`Q15_ULTOIM_V2_EXIT_WARNINGS`).

A new alert that warns you to bail when the model reverses on a pick it already suggested. Grounded in the live-data finding (multi-bot analysis) that the ONLY reliable "the bot made a mistake" signal is a **persistent cross-checkpoint side flip** (78% of such flips lose), not flip_probability (≤36%), flip_risk, or distance-to-strike.
- **Fires in chat ONLY when BOTH:** (A) a paper entry was SUGGESTED earlier in this window (`fired=1` row exists for the contract), AND (B) at/after the 7M mark the champion has FLIPPED to the opposite side. Anti-spike: the flip must be **decisive** (new-side prob ≥ `EXIT_MIN_FLIP_CONF`=0.55) AND **sustained** (held ≥ `EXIT_CONFIRM_CYCLES`=3 consecutive observations spanning ≥ `EXIT_CONFIRM_SECONDS`=20s) — a momentary wobble never triggers. One warning per (ticker, window); debounce state is worker-thread-local + a DB dedup lock (restart-safe).
- **Data behind it:** the card shows the original pick (side @ ask), the flip (new side, P(YES), conviction), why-it's-not-a-spike (cycles × span, flip-risk), an estimated **sell-to-close value** (recover this vs 0 if it settles against you), and a **live track record** ("N% of these flips lost, k/n") — marker-safe (no `V9.5 CHECK`/`ENTRY RECOMMENDED`/`NO ENTRY YET`/`Hourly Report —`/`TOP 3 PICKS`).
- **Learns from mistakes:** every warning is recorded and graded at settlement — CORRECT if the entry side lost (bailing saved you, records recovered¢), FALSE ALARM if the entry would have won (records forfeited¢). New `ultoim_v2_exit_warnings` table; `exit_warning_scoreboard` (precision, recovered/forfeited/net, Wilson CI) surfaces in the recap and feeds the card's track-record line (auto-updates).
- **Files:** `q15_upgrade/ultoim_v2/{config,ledger,runner,panel}.py`, `.env.example`, `tests/test_ultoim_v2.py` (+7). Live-loop-safe (the exit pass runs on the worker; observe still only enqueues). Honest limit (from the data): ~7–15% of losses are un-warnable (confidently-wrong A-grade that gets *more* sure late) — size positions so a full loss is survivable.

## ✅ Shipped THIS session — Ultoim V2 → PROVABLE: gate fix + YES research recording + significance/validation module (branch `claude/keen-fermat-ce81uv`)
**Suite 1197 passed / 4 skipped here** (+25 ultoim_v2 tests, 48 total in the file). Deploy-pending — branch + draft PR.
**Still DEFAULT-OFF** (`Q15_ULTOIM_V2_ENABLED`) and read-only; delivery stays NO-only; frozen champion untouched. App byte-identical when the overlay is unset.

Built off the previous entry (Ultoim V2 paper entry-alert system), which the handoff flagged as **UNPROVEN**: NO-only, 0 YES-prone windows seen, and `edge≥2` not statistically separable from the ~75% base NO rate. A full parallel review team (architecture / gate+stats / safety / UI / tests, then a final adversarial diff review) scoped the path to a provable promotion decision. Implemented end-to-end, all in the `ultoim_v2/` package + a new pure `validate.py`:

- **Gate correctness (`gate.py`)** — fixed a **HIGH float-boundary bug**: `net_edge >= min_edge_cents` silently rejected mathematically-exact 2.0¢ edges (`0.58*100−56 = 1.999999999999993`), corrupting the gate's one fitted knob; now tolerant by `1e-9`. Also: reject `bool`/non-finite `sel`/`ask` as `MISSING_DATA` (the gate no longer inherits upstream type bugs); `display_entry` floors a fractional ask and re-clamps into the ask band (was `int(49.9)=49`, below the 50¢ floor). Added side-agnostic `research_fired` (gate_b AND gate_c, ignoring NO-only) so YES candidates can be recorded without ever delivering.
- **YES-side research recording (`runner.py`+`ledger.py`)** — the system previously recorded ONE chosen NO row per (interval, window), so YES-prone windows produced **zero gradeable data** and could never be proven. Now the best YES candidate per (interval, window) is also recorded as a `RESEARCH_YES` row: `fired=0`, `delivery_status='RESEARCH'`, **never alerted, never claims the alert lock, never inflates the delivered headline** (`fired==1`). Skipped when it's the same contract already delivered (UNIQUE would reject it anyway). Toggle `Q15_ULTOIM_V2_RESEARCH_YES=false`. New `record_kind`/`research_fired` columns via an **idempotent ALTER migration** (existing DBs upgrade in place; no destructive rebuild); `record_decision` defaults the new columns so any caller still inserts cleanly.
- **Validation / promotion math (`validate.py`, NEW, pure/read-only)** — exact one-sided **binomial test**, **Wilson CI**, base-rate lift vs BOTH the empirical base rate and the standing 0.75 prior, **edge-bucket monotonicity** (the key "is edge≥2 real signal or curve-fit noise?" diagnostic), per-side / per-interval splits, decision-time regime split AND a **realized-window regime** (post-hoc bucketing by settled YES-share — leakage-safe, never fed back to a decision), ROI. A `promotion_verdict` returns INSUFFICIENT / NOT_SEPARABLE / BEATS; `fully_proven` additionally requires the YES side AND the YES-prone realized regime to each clear the bar. Worked numbers baked into tests: 11/12 (p=0.158) and 5/6 (p=0.534) are NOT separable from 0.75; ~50–60 resolved with Wilson-low > base rate is the real bar. New `Q15_ULTOIM_V2_MIN_PROMOTE_N` (default **50**) is the promotion bar, DISTINCT from the n=30 print-floor.
- **Honest reporting (`panel.py`)** — the entry card's caveat was **hardcoded** `"1 regime · 0 YES-prone · CI wide"` (would lie the moment data accrued); now DERIVED from the scoreboard. The recap gains a sig-gated `PROMOTION:` verdict line (with an "overall only — YES-side / YES-prone unproven" qualifier so it can't over-claim) plus `By side:` (YES = research-only) and `By regime:` sections. **Suppression-marker safety preserved** and the test guard expanded to include the previously-missing `NO ENTRY YET` (all 5 forbidden routing markers now asserted absent across the card + every verdict state).
- **Scoreboard** — added `by_side` (NO delivered vs YES research, each with its own base-rate/edge) and a `resolved_rows` accessor feeding `validate.py`. Overlay guards bumped debug→warning so a persistently-failing overlay is observable.
- **Tests (+25)** — gate float-boundary/bool/display/research_fired + multi-reason collection; Wilson & binomial known-values; lift/edge-bucket/regime/verdict; ledger by_side/resolved_rows/report-lock/base-rate-tie/restart-migration; runner RESEARCH_YES-alongside-delivered-NO (+ disabled + stale boundary); recap promotion verdict (insufficient/not-separable/beats+qualifier) + side/regime sections + derived caveat. `.env.example` now documents the whole overlay.
- **NOT YET USEFUL until data accrues (honest):** the validation module reports n=0 / INSUFFICIENT until the overlay is enabled on the Repl and YES-prone windows settle. Enabling it can't trade, can't touch the champion, and can't deliver YES. Next step is unchanged from the prior entry: promote to "live" only once the verdict reads BEATS BASE RATE on the NO side AND the YES side / a YES-prone regime each clear the bar.

## 💡 IDEA — NOT IMPLEMENTED (parked for later) — Ultoim V2 manipulation YES+suspected veto
**Status: investigated only, no code written.** Data as of `learning-snapshots` snapshot
`2026-06-23T06:19Z` (git_commit `08221c4`); v2 ledger had **n=7 fired+resolved (~3h since reset)**
→ directional, not significant. Revisit when v2 has more data.

**The question:** can changing how the manipulation signal is used help Ultoim V2 without hurting it?

**What v2 does today:** v2 RECORDS `manipulation_suspected` on every candidate row
(`ultoim_v2/runner.py:148,263`) but the gate (`ultoim_v2/gate.py:evaluate`) **never reads it** —
the decision is purely NO-only + conf≥0.55 + ask∈[50,72] + net_edge≥2¢ + not-stale. So manipulation
is captured-but-unused dead weight in v2.

**What the records show (champion ledger `q15_v95_ledger_v1`, n=306 suspected):** the manipulation
flag's entire loss is ONE bucket — **YES + suspected = 54.5% acc, −13.2¢/contract (n=101)**. NO+suspected
is +1.1¢, NO+clean +6.3¢, YES+clean +2.7¢. The flag over-fires (~80% of all rows; 82% of v2's candidates).

**Two evidence-safe changes (the only ones the data backs):**
1. **Default-OFF "veto YES + suspected" gate** in `ultoim_v2/gate.py` (+ `Q15_ULTOIM_V2_*` flag). It's a
   **no-op on current behaviour** (v2 is NO-only → 0 YES fired) and only bites as a safety rail if anyone
   sets `Q15_ULTOIM_V2_NO_ONLY=false`. Then it blocks exactly the −13.2¢ bucket while allowing YES+clean.
2. **Add a manipulation split to v2's scoreboard/recap** (`ultoim_v2/ledger.py:scoreboard`, read-only) —
   v2 tracks by_interval/by_regime but not by manipulation, even though every row carries the flag. This is
   the instrument that tells you whether NO-suspected ever turns toxic at v2's gate before acting on it.

**Backtest on v2's OWN record = ZERO measured impact (can't hurt, not yet proven to help):**
- Took all v2 trades: **before veto +210¢ realized / +62¢ net-edge (n=7); after veto identical. Delta +0¢.**
- Even simulating YES enabled: before/after both +158¢ — because all 3 YES+suspected candidates in v2's
  record **fail v2's own conf/ask gate anyway** (BTC 15M conf 0.546; DOGE 7M ask 19¢; SOL 7M conf 0.508),
  so the veto never had a trade to remove. Its justification is the champion's broader record, not v2's.

**DO NOT do this (the trap):** a hard "veto NO+suspected" gate would have killed **6 of v2's 7 fires**
(82% of candidates are flagged) and NO+suspected is still mildly +EV — gating the NO side is unsupported
and the early v2 data leans against it (6/7 winners were suspected). Dropped the earlier "tie-break toward
clean" idea for the same reason.

**Next step when revisiting:** ship #2 (scoreboard split, read-only) first to accumulate the v2-native
clean-vs-suspected record; add #1 (YES veto) only when/if `no_only` is turned off. Both `Q15_*`-gated,
test-backed, no-ops on current behaviour.

## ✅ Shipped THIS session — Ultoim V2: skip-15M + cross-asset-flow recorder (branch `claude/dazzling-cori-85ptaa`)
**Suite 1145 passed / 13 skipped here** (+4 ultoim_v2 tests; ~1137 in a complete env). Both DEFAULT-OFF,
`Q15_*`-gated, gate untouched → byte-identical app unless a flag is flipped. Deploy-pending — branch + PR #26.

Came out of a 3-agent research pass this session (RSI / chart-patterns / other-ideas, all read-only on the
`learning-snapshots` data). Findings: **RSI = INSUFFICIENT** (recorded nowhere joinable to a v2 decision —
0/546 champion rows; lives only in `window_focus.py:687`/`end_predictor.py:88` → Postgres, not exported;
needs instrumentation + ~6-8wk before it's testable). **Head-and-shoulders / daily chart patterns =
NOT-APPLICABLE** (multi-hour reversal vs ≤15-min settlement; ~10-min candle retention — 40-500× horizon
mismatch; the existing 5s `patterns.py` is the right horizon). The two changes below are the evidence-backed,
no-op-today wins the user picked:
- **`Q15_ULTOIM_V2_SKIP_15M` (default false):** when true, V2 fires only at 10M/7M, dropping the weak 15M bin.
  Verified on V2's OWN record (snapshot `2026-06-23T07:40Z`, commit `08221c4`): 15M fires 1/3 = −59¢ (only
  losing bin) vs 10M 6/8 +147¢ / 7M 1/1 +50¢; dropping 15M lifts total **+138¢→+197¢** and per-trade
  **+11.5¢→+21.9¢**. Corroborated by the timing-by-mark curve (900s 58% → 600s 77% → 420s 86%, n=98+) and by
  the champion already disabling its own 15M alert delivery. (`config.py` flag; `runner._observe_sync` skips
  `interval=="15M"`.) Tiny n caveat (15M n=3) — hence default-OFF, reversible.
- **`Q15_ULTOIM_V2_RECORD_XFLOW` (default false):** measure-first. When true, records the broad-market
  cross-asset flow factor `x_market_flow` (mean of per-asset `flow`, YES-signed, via `shadow_factors.compute_market`)
  on every V2 candidate row — for later validation of a possible NO-side veto (high market-wide YES pressure
  preceded NO losses on an OOS time-split: low-flow NO 78.5% vs high-flow 60%, n=93/35). **Pure observation —
  NEVER read by the gate** (test-asserted the fire decision is identical on/off). New nullable `x_market_flow`
  column (+additive `_ensure_columns` migration for the live DB; old rows read NULL).
- **Files:** `q15_upgrade/ultoim_v2/{config,ledger,runner}.py`, `tests/test_ultoim_v2.py` (+4), `.env.example`
  (new Ultoim V2 block). **Deploy:** set `Q15_ULTOIM_V2_SKIP_15M=true` and `Q15_ULTOIM_V2_RECORD_XFLOW=true`
  in the Repl env, then Stop ▸ Run. The manipulation YES+suspected veto idea above stays PARKED (not built).

## ✅ Shipped a PRIOR session — Ultoim V2: paper entry-alert system (branch `claude/sleepy-cray-8ktugn`)
**Suite 1125 passed / 13 skipped here** (+23 ultoim_v2 tests). Deploy-pending — branch + draft PR.
**Default-OFF** (`Q15_ULTOIM_V2_ENABLED`, default false → app byte-identical when unset).

A SEPARATE, read-only, paper entry-alert system in new package `q15_upgrade/ultoim_v2/` —
own DB (`data/q15_ultoim_v2_v1.sqlite3`), own `model_version="ultoim-v2"`, own Telegram chat
(`Q15_ULTOIM_V2_TELEGRAM_CHAT_ID`). Never places real orders; never touches the champion or the
real Ultoim; reuses the frozen analysis read-only. Wired as two guarded try-blocks
(`checkpoint_v95.py:2779` observe, `app.py:648` reconcile+recap), mirroring the ultoim pattern.

- **Entry gate** (`gate.py`, pure): NO-only, `selected≥0.55`, ask∈[50,72]¢, `net_edge≥2¢`,
  inclusive comparators, NULL-SKIP, reason codes. `best_entry = floor(sel·100 − cost − 2)` clamped
  to band, never above market ask. Validated on live data: 12 trades / 92% / +52% ROI (in-sample 6
  @100%, fresh OOS 6 @83%) — but ONE NO-leaning regime, 0 YES-prone windows → UNPROVEN; the gate's
  one fitted knob is `edge≥2` and it is NOT yet statistically separable from the ~75% base NO rate.
- **Alerts**: live "BEST ENTRY" card grammar, labeled `ULTOIM V2 · PAPER ENTRY` with a per-message
  `N / 1-regime / 0 YES-prone` caveat; never emits `V9.5 CHECK`/`ENTRY RECOMMENDED`/`Hourly Report —`
  /`TOP 3 PICKS` (suppression-marker safety; test-asserted). One alert per contract per window
  (alert-lock), earliest qualifying checkpoint. Freshness gate abstains on a stale spot (STALE_FEED).
- **Records everything** on entry AND no-entry: gate pass/fail + reason codes, cushion-to-strike
  (`distance_sigma`), regime_directional, market-implied prob, depth/quote-age, base_rate_side,
  session_id, full feature vector + settlement + realized P&L. `learning_export` auto-globs its DB.
- **30-min research recap** (`build_recap`, header `ULTOIM V2 — RESEARCH RECAP`): resolved/total/
  pending, W-L + Wilson CI, ROI, base-rate + edge-over-base, by-interval, recent picks, and a
  "recent losses (for review)" section. Headline % suppressed below n=30 (`INSUFFICIENT DATA`).
- Built + stress-tested by 4 read-only review agents first (architecture, rule/signal, UI/format,
  safety) before implementation. Owner directive: research overlay but live-formatted real-time
  signals so it can be paper-traded and visualized; promote to "live" only after it beats base rate
  across a YES-prone window.

## ✅ Shipped THIS session — Track the full 15M→7M mark ladder (incl. 9M/8M) across the learning systems (branch `claude/sleepy-cray-8ktugn`)
**Suite 1102 passed / 13 skipped here** (+1 net interval-research test; 1133 in a complete env).
Deploy-pending — branch + draft PR, NOT merged to main.

Motivation: a multi-agent entry-economics review (read-only, on the live `learning-snapshots`
ledgers — one ~2h session, 7 assets) found 9M/8M were **NOT TRACKED CORRECTLY**: the live
timing writer enumerated only 780/720/660 (13/12/11M) and the dedicated 8-mark `interval_research`
module was default-OFF, so the 10M→7M "knee" (where executable EV flips positive) was
unmeasurable. Everything else in the review was INSUFFICIENT (single regime, 0 live entries).

Changes (all OBSERVATIONAL — no trading, no Telegram, frozen champion untouched):
- `checkpoint_v95._timing_experiment_marks()` default `780,720,660` → full ladder
  `900,780,720,660,600,540,480,420` (15/13/12/11/10/9/8/7M). The hourly report's
  "Entry-timing experiment" section (`notifications/reporting.py:_timing_experiment_lines`)
  renders marks generically, so the whole 15→7M accuracy curve now shows up as rows resolve.
- `interval_research` flipped to **default-ON (capture-only)** (`Q15_INTERVAL_RESEARCH_ENABLED`
  default True) — the purpose-built 8-mark collector that also records per-mark **executable
  economics** (ask/edge/P&L). It is already wired into `run_cycle` as a guarded observer +
  settlement resolver; `learning_export.py` auto-globs `data/*.sqlite3`, so its new DB exports
  to `learning-snapshots` for review with no plumbing change. `=false` for a fully inert app.
- Tests updated: timing default-marks assertion (full ladder); interval-research `test_default_off`
  → `test_default_on_capture_only` + `test_explicit_disable_still_works`.

Reviews of the OTHER learning systems (read-only, same single-regime data): shadow challenger
INSUFFICIENT (pinned to control by cold-start mirror, 0 trades); 5 shadow signals + flip predictor
INSUFFICIENT/non-predictive (A/B "significance" is a pure-NO-fold artifact; flip AUC ≈0.51) but
correctly dormant; weight learners LEARNING within caps, champion provably frozen, promotion
correctly withheld (<50 rows). No BROKEN code paths found.

## ✅ Shipped THIS session — Ultoim grade fix: "always C" → real A/B/C spread (branch `claude/magical-cannon-6dkv8s`)
**Suite 1133 passed / 4 skipped** (+5 ultoim tests). Diagnosed via the live snapshot
(`learning-snapshots` + pre-reset `tez_review_dump.json`): the Ultoim multi-factor grade was
stuck at C on **every** pick because `quality_score` multiplied a base term (already small —
`(side_prob-0.5)/0.49`) by ~5 sub-1.0 penalty factors, compounding every score under the B
line. Real proof: all 12 stored ultoim picks were C (quality_score 0.015–0.471; the B cutoff
was 0.48, so the best pick ever missed B by 0.009).
- **Fix (all in `q15_upgrade/ultoim/{ranker,config,runner}.py`):** `quality_score` is now a
  **weighted average** of positive signals (confidence 0.50 / data 0.15 / evidence 0.15 /
  agreement 0.20) — not a product — so a strong setup lands high. Confidence uses the
  **calibrated** chosen-side probability (champion is under-confident). The unvalidated,
  miscalibrated **challenger is excluded** from the agreement term by default
  (`Q15_ULTOIM_GRADE_INCLUDES_CHALLENGER=false`). Validated vetoes (YES-quality, flip, manip)
  stay as bounded multiplicative penalties. `grade_b_min` recalibrated 0.48 → **0.50** to match
  the new ~0.35–0.70 range. All knobs env-tunable; manip penalty promoted to config.
- **Re-grading the 12 real picks with the shipped defaults: 3 A / 5 B / 4 C** (was 12 C).
- **Tests:** `tests/test_ultoim_build.py` +5 (strong→A regression, weak→C, monotonic in
  calibrated confidence, challenger-excluded-by-default, recalibrated default cutoff). Existing
  penalty/ranking tests unchanged and green. `.env.example` documents the new vars.
- Research-only + read-only; the champion and live alerts are untouched.

## ✅ Shipped THIS session — Interval-timing research collector (default-OFF, prospective)
**Suite 1096 passed / 13 skipped** (+14 tests). New package `q15_upgrade/interval_research/`
(`config/ledger/capture/runner/economics` + `tests/test_interval_research.py`). A SEPARATE,
read-only research system (Ultoim pattern) that — when `Q15_INTERVAL_RESEARCH_ENABLED=true` —
captures the frozen champion's per-asset analysis at EIGHT marks (15M/13M/12M/11M/10M/9M/8M/7M)
into its own SQLite table `interval_captures` (DB `data/q15_interval_research_v1.sqlite3`).
Motivation (from the timing analysis): EV peaks at **10M** (acc 70%, ask ~69¢, best/only-positive
EV) and erodes by **7M** (acc 78% but ask ~79–97¢ → edge priced out); the fresh-7M-NO 97.7% is
largely a **late-only coverage artifact**. 13M/12M/11M/9M/8M have NO history, so this collects them
PROSPECTIVELY — no fabricated rows. Invariants: never trades/sends/alters the champion; wired as a
read-only observer + settlement-resolver in `run_cycle` alongside Ultoim; default-OFF.
- Captures per (ticker,interval): side, raw/calibrated/conservative prob, flip prob, manip score,
  yes bid/ask, spread, depth, slippage/fees, distance-from-strike, stability, data-quality, executable
  ask, net edge, trade_decision, entry_recommended, + one of 10 REASON_CODES when a capture is missing.
- `economics.py` (read-only): per-interval executable economics (acc/ask/edge/entry-rate/EV/ROI/drawdown),
  PREDICTION-quality vs TRADE-value kept SEPARATE (`classify`: 97%@97¢ => HIGH prediction / LOW trade),
  cohort split (full / partial / late-only), matched-cohort comparison (only contracts at all compared
  marks), defensive-exit grading (true/false/late warnings, lead time, value recoverable).
- Restart-safe (UNIQUE(model_version,ticker,interval) + INSERT OR IGNORE), no look-ahead (point-in-time
  band capture). Roles are PROVISIONAL: 10M=OFFENSIVE_ENTRY, 7M=CONFIRMATION_DEFENSIVE, others research.
- NOT YET USEFUL: results require prospective resolved data; module reports n=0 honestly until then.
  Enable + redeploy to start collecting; champion live behaviour unchanged.

## ✅ Shipped THIS session — Manipulation reason×side scoreboard cut (validation tool)
**Suite 1076 passed / 13 skipped** (+1 test). Read-only/additive. Adds `by_reason_side` to
`ledger_v95._by_manipulation`: crosses tell-type (`absorption` = any ABSORPTION row; `pin_only`
= rows whose only tell is PIN) with side (YES/NO). On the live ledger this REFUTED the earlier
"ABSORPTION is the signal" read: controlling for side, pin_only·NO (71.5%, −0.75¢) ≈ absorption·NO
(70.4%, −1.6¢), while both YES buckets bleed (~62-65%, −9¢). So the manipulation flag's only real
discriminator is the **NO side**, not the reason type — the ABSORPTION edge was a side-mix confound.
Also validated (read-only) that a PIN distance-tightening cut does NOT help: closer-to-strike PIN
flags don't discriminate better (non-monotonic; tightest ~60% score 64.5% vs farthest ~40% at 70.9%),
so `Q15_V95_MANIPULATION_PIN_MAX_DISTANCE_SIGMA` should stay OFF.
**Then built the PERSISTENCE cut** (`by_persistence`, point-in-time: does a flag fire at an EARLIER
checkpoint of the same contract?). Pooled, persistent looked far better (72.6% vs 61.1%) — but that
is an INTERVAL CONFOUND (15M flags are always "fresh" and 15M is the weak interval). Controlled per
checkpoint the relationship REVERSES: 7M fresh 91.7% (+2.83¢) vs 7M persistent 75.8% (−4.37¢); 10M
fresh 71.9% (+1.32¢) vs 10M persistent 69.2% (−2.9¢). So the real signal is **freshness near close,
not persistence** — `by_persistence` ships BOTH the pooled and the `by_checkpoint` (honest) views so
the confound stays visible. Best manipulation subset on record: **fresh-flag @ 7M (91.7%, +2.83¢, n=84)**.
**Then validated & exposed that signal** (`by_persistence.fresh_near_close`, side-split). OOS-checked:
accuracy holds out-of-sample (older half 92.9% → newer half 90.5%), NOT a recent-regime artifact
(spread over 2 days, 4/84 recent), and concentrates on the **NO side: fresh-near-close·NO = 97.7%,
+8.92¢, n=43, Wilson CI [0.879, 0.996]** — the strongest manipulation subset found. Caveat: P&L noisier
than accuracy (test half −1¢), so it's a confidence signal first.
**ACTIVATED (owner directive) as a default-ON alert TAG** (`Q15_V95_FRESH_MANIP_TAG`, default true):
at the 7M checkpoint, when manipulation is suspected AND the contract was NOT flagged at 15M/10M
(`ledger.manipulation_flagged_before`, point-in-time), the checkpoint alert appends, to that asset's
Manipulation-watch line, "🎯 FRESH 7M·<SIDE> — predicted <SIDE> NN.N% right (k/n)" where the rate is
the LIVE historical hit-rate of that fresh-7M-<side> bucket (`ledger.fresh_near_close_rate(side)`,
auto-updates; below `Q15_V95_SCOREBOARD_MIN_N` it says "building, n=k"). Fires on BOTH sides so the
owner can weight them: live record is **NO 97.7% (42/43)** vs **YES 85.4% (35/41)** — NO is the real
edge, YES is weaker and P&L-negative. Owner chose the SAFE form: it surfaces the signal only — it does
NOT touch the frozen champion's probability/edge/entry decision, and preserves ENTRY/V9.5 CHECK markers.
Toggle off with `Q15_V95_FRESH_MANIP_TAG=false`. ACCURACY ≠ profit: read the tag together with the
entry/edge line (a 97%-accurate NO at a rich price is still thin). Next step if P&L proves durable: an
opt-in confidence/quality boost.

## ✅ Shipped THIS session — Challenger v6 research + Entry Economics v1 (two workstreams)
**Suite 1066 passed / 13 skipped** in a complete env (+45 tests:
`tests/test_entry_economics.py` 32, `tests/test_challenger_v6_research.py` 13).
Both workstreams are READ-ONLY and ADDITIVE — with default env the live app and the
live challenger shadow are byte-identical (no active prediction or entry gate
changed). Deploy-pending on the branch + a Repl Stop ▸ Run; nothing auto-promoted.

- **Workstream 1 — Challenger review (identity preserved).** Documented the
  challenger's distinguishing logic (learned L2-logistic on its OWN ledger +
  independent Platt/isotonic calibration + decisiveness ranking + own OOD + own
  cost/decision model — vs Your System's market-baseline-plus-residual +
  net-edge ranking). Added a NEW research version **`challenger-v6`** WITHOUT
  touching the live v5 path:
  - `q15_upgrade/challenger/features_v6.py` — leakage-safe, APPEND-ONLY superset of
    the frozen v5 feature vector (+12 microstructure features: strike pressure,
    time above/below strike, strike-cross rate, failed continuation, flow
    persistence, book resiliency, return entropy, regime transition, spot-vs-
    contract disagreement, cross-asset confirmation, manipulation score).
  - `q15_upgrade/challenger/research.py` — purged walk-forward OOS harness that
    grades v6 vs v5 PAIRED, with a strict promotion gate (significant log-loss win,
    no Brier/calibration regression) — proven to promote a real signal and keep
    noise in research. Plus a prediction-stability EMA (default OFF).
  - Config switches `Q15_CHALLENGER_FEATURE_SET` (default `v5`) /
    `Q15_CHALLENGER_STABILITY_HALFLIFE` (default 0). v6 stays in RESEARCH MODE —
    promotion is a deliberate config switch after a win on real settled data.
- **Workstream 2 — Entry Economics repair (`entry-econ-v1`).** NEW separate,
  read-only package `q15_upgrade/entry_economics/` answering "is this contract
  worth buying at an executable price?" with **ENTER / WAIT / SKIP**. Fixes the
  real economics: the **Kalshi fee is now ceil-rounded** (was under-charged),
  depth-walk slippage + partial-fill, latency + stale surcharge, a rich
  **conservative probability** (calibration/sample/coverage/disagreement/
  instability/regime/flip/manip/interval/asset — combined by geometric mean so a
  genuine edge can still ENTER), break-even prob, max/recommended entry, EV +
  EV-after-uncertainty, R:R, liquidity capacity. Compact Telegram panel + an
  independent entry-performance ledger (only ENTER-sent-before-settlement counts;
  WAIT/SKIP studied in background; restart-safe + dedup). Surfaced live in the
  snapshot (`q15_entry_econ_*` + panel); ledger writes are default-OFF
  (`Q15_ENTRY_ECON_LEDGER`). Wired read-only into `apply_v95_policy` (evaluate +
  surface) and `ledger_v95._shadow_resolve` (settlement grading), both guarded.
- **Operator tool** `tools/entry_research_report.py {challenger|entry}` — run the
  v6 OOS comparison on the production ledger / print the entry scoreboard.
- **Incomplete (honest):** crediting OFFICIAL entries (`mark_enter_sent`) requires
  the entry-econ ENTER panel to actually be delivered before close; that delivery
  hook is exposed on the runner but not wired into the send path this session (it
  must couple to real Telegram delivery — no fabricated fills). Until wired,
  `official_entries` stays 0 by design; direction grading + WAIT/SKIP studies do
  populate once `Q15_ENTRY_ECON_LEDGER=true`.

## ✅ Shipped THIS session — Challenger (shadow) orientation fix: calibrated control + cold-start mirror
**Suite 1030 passed / 13 skipped** (+2 tests). Branch `claude/ultracode-mode-question-5aj8un` (PR #20).
Shadow-only, zero live impact (`primary_probability` returns champion unless promoted AND trained — never).
Root-caused from real data: the challenger looked anti-predictive (challenger_prob_yes 43.7%, control
anti-calibrated) because of two orientation bugs, NOT a bad model:
- **`ledger_v95.py` `_shadow_observe`** — fed the challenger `control_prob_yes=raw_yes_probability` (the
  pre-calibration value, **50.8% directional acc, flat**) instead of `calibrated_yes_probability` (the
  champion's REAL prob, **67.9% acc, monotonic**). The whole Shadow-vs-Yours comparison was against the
  champion's throwaway raw number. Fixed to pass the calibrated prob.
- **`challenger/runner.py observe`** — the cold-start mirror only triggered when `market_yes_prob is None`,
  so with a quote present the untrained shadow parroted the raw market quote (~42% acc here). Now it mirrors
  the champion whenever untrained (its own documented "start at parity" intent), regardless of the quote.
- Verified: champion `calibrated_yes_probability` calibrates cleanly (0.0-0.3→11% YES, 0.7-1.0→84% YES);
  `raw_yes_probability` does not (50.8%). Fix is forward-looking; historical shadow rows stay as recorded.
- Tests: cold-start mirrors champion even with a quote; record_prediction hands the shadow the calibrated control.

## ✅ Shipped THIS session — Manipulation detection: tunable PIN tell + scoreboard discrimination
**Suite 1028 passed / 13 skipped** (+7 tests) on this container's env. Branch
`claude/ultracode-mode-question-5aj8un` (PR #20, draft). Read-only, **defaults byte-identical**.
Motivated by the live record: the manipulation flag fires on ~76% of markets at baseline
accuracy (no edge), driven by an over-firing PIN tell.
- **`checkpoint_v95.py`** — new default-OFF knob `Q15_V95_MANIPULATION_PIN_MAX_DISTANCE_SIGMA`
  narrows the observational PIN *tell* below the regime's 0.25 band (recommended 0.15 to
  validate). The FROZEN `THRESHOLD_PIN` *regime* (feeds champion uncertainty) is untouched;
  `_pin_tell_passes` keeps the tell when the knob is unset or the measurement is missing.
- **`ledger_v95.py`** — `_by_manipulation` scoreboard now adds `by_reason_isolated`
  (single-tell-only buckets), `by_checkpoint`, and `by_side` (all purely additive; old keys
  unchanged). On the live ledger this immediately separates **ABSORPTION-only 74.3% / −0.17¢**
  (the edge) from **PIN-only 67.0% / −4.82¢** (the noise), and shows manip-flagged **NO side
  is +0.74¢ vs YES −10.21¢** — structure the blended view hid.
- Next: with `by_reason_isolated` now measurable, validate the PIN tightening out-of-sample,
  then consider promoting a stricter default (significance-tested, per invariants).

## ✅ Shipped THIS session — Ultoim Build: separate read-only research reporting system
**Suite 1052 passed / 4 skipped** in a complete env (+15 tests). New package
`q15_upgrade/ultoim/` + `tests/test_ultoim_build.py`; wired with two guarded,
default-OFF hooks (`checkpoint_v95.run_cycle` observe + `app.py` reconcile).
Read-only; never trades. **Default ON** as a read-only collector but stays MUTED
(records, never delivers) until `Q15_ULTOIM_TELEGRAM_CHAT_ID` is set, so default-on
is safe and silent; `Q15_ULTOIM_ENABLED=false` makes the app byte-identical. Tests
stay deterministic via `tests/conftest.py` (autouse: Ultoim off in the suite).
In-process (own worker thread, like the polymarket shadow) — no `.replit` change.
Deploy-pending on `main` + a Repl Stop ▸ Run to load it.
- **What it is:** a SEPARATE research system (own DB `data/q15_ultoim_v1.sqlite3`,
  own model_version `ultoim-build-v1`, own counters/reset marker, own Telegram
  channel via `Q15_ULTOIM_TELEGRAM_CHAT_ID` reusing `TELEGRAM_BOT_TOKEN`). Reuses
  the champion's frozen per-asset analysis (read-only) to publish exactly 3 picks
  at **12M / 10M / 7M** (12M replaces the toxic 15M per this session's findings).
- **Picks apply the session's analysis:** ranked by **quality × net-edge (value)**
  not raw confidence; **multi-factor A/B/C grade** (calibration, data/evidence
  quality, model agreement, flip risk, manipulation anti-signal, the validated
  YES-quality veto via order-flow-persistence/book-resiliency) — never probability
  alone. Records hypothetical P&L per pick.
- **Flip = active research:** ensemble stability score (flip_risk_score +
  prediction_stability + book_resiliency + order_flow_persistence), recorded with
  decision/original/expected-side; graded at settlement (genuine flip = locked
  side != official result). Weighted LOW in the grade until it beats
  flip_risk_score OOS. Never claimed validated.
- **Compact Telegram only** (`ULTOIM BUILD · 12M` / medal / asset side / Grade /
  Outcome% / `Research mode · read-only`); all calc/feature internals stay in the DB.
- **Records + safety:** immutable picks (UNIQUE model_version,ticker,interval);
  one report per (interval, window) via `ultoim_report_lock`; settlement grading
  via the SHARED `market_cache` (ground truth, no record-mixing); delivery-failed
  picks kept for learning but EXCLUDED from the visible scoreboard; missing/failed
  never counted as losses; restart-safe (no double record/grade) — all tested.
- **Files:** `q15_upgrade/ultoim/{__init__,config,ledger,ranker,flip_research,
  panel,telegram,runner}.py` (new), `tests/test_ultoim_build.py` (new),
  `q15_upgrade/checkpoint_v95.py` (+observe hook), `app.py` (+reconcile hook),
  `.env.example`.
- **Deploy:** already ON by default (records silently). To DELIVER, set
  `Q15_ULTOIM_TELEGRAM_CHAT_ID=<chat>` in the Repl, then **Stop ▸ Run**.
  `Q15_V95_SHADOW_SIGNALS_ENABLED` is already default ON (feeds grade + flip).

## ✅ Shipped THIS session — Hourly learning-snapshot export to a dedicated branch
**Suite 1036 passed / 4 skipped** in a complete env (+12 tests). New worker
`tools/learning_export.py` + `tests/test_learning_export.py`; wired into `.replit`
as a third parallel workflow ("Learning Export"). Read-only wrt the ledgers and
the real exchanges; frozen champion untouched. Deploy-pending on `main` AND needs
a Repl Stop ▸ Run to start the new worker.
- **Why:** a fresh review container has no `data/*.sqlite3` (gitignored), so
  `updated-review` could only grade code, not real records. The Repl now publishes
  the live learning ledgers hourly so any session can pull real data.
- **What it does:** hourly, takes a *consistent* read-only SQLite online-backup of
  every `data/*.sqlite3`, builds `learning_snapshot.json` (v95 + challenger
  scoreboards, official W/L, the 5-feature shadow A/B, timing experiment, flip
  perf, per-table row counts) + gzipped raw DBs, and **force-pushes one orphan
  commit** to the dedicated **`learning-snapshots`** branch.
- **Why a side branch, not `main`:** the GitHub Relay two-way-syncs only `main`
  every ~20s; a Repl-regenerated file on `main` is exactly what forced
  `health_snapshot.json` to be gitignored ("conflict on every sync, stall
  deploys"). The side branch is invisible to the relay and the `claude/*`-only
  pruner → zero deploy risk, zero `main` churn. Orphan/force-push ⇒ no history
  bloat.
- **Safety:** built entirely with git plumbing against a TEMP index (hash-object
  → write-tree → commit-tree → push by SHA) so it NEVER touches HEAD / the working
  tree / the app's index — safe in the live checkout. Reuses the relay's
  `GH_PUSH_TOKEN`/`GITHUB_TOKEN` (in-memory URL, masked logs, no .git/config
  writes). Refuses to target `main`/`master`/`HEAD`. Online-backup ⇒ never writes
  the live DB / no torn copy.
- **Consume (review side):** `git fetch origin learning-snapshots` then read
  `learning_snapshot.json` or `git show origin/learning-snapshots:dbs/<db>.gz |
  gunzip > /tmp/...`. The `updated-review` skill Step 1 now does this first.
- **Config (all default-sane, `.env.example`):** `LEARNING_EXPORT_BRANCH`
  (default `learning-snapshots`), `LEARNING_EXPORT_INTERVAL` (3600s),
  `LEARNING_EXPORT_DATA_DIR` (`data`), `LEARNING_EXPORT_REPO`.
- **Deploy:** after this reaches the Repl, **Stop ▸ Run** so the new workflow
  starts; first push creates the branch within ~1h (or sooner if INTERVAL lowered).
- **Files:** `tools/learning_export.py` (new), `tests/test_learning_export.py`
  (new), `.replit`, `.env.example`, `CLAUDE.md`,
  `.claude/skills/updated-review/SKILL.md`.

## ✅ Shipped THIS session — Polymarket Up/Down shadow (read-only, default-OFF)
**Suite 1024 passed / 4 skipped** in a complete env (+23 tests). New package
`q15_upgrade/polymarket/` — a SECOND read-only shadow, sibling to the challenger.
- **Why it's a separate contract, not a Kalshi match:** Polymarket's 15-min crypto
  markets are **Up/Down** ("close >= window-open price", Chainlink/Binance), whereas we
  trade Kalshi **strike** markets ("≥ $X", CF/Coinbase). Different YES meaning + price
  source → never matched/merged. We grade against Polymarket's OWN result and compare
  **our model vs the Polymarket market on that same up/down contract** (valid), never
  Kalshi-outcome vs Polymarket-outcome (invalid).
- **What it does (when `Q15_POLYMARKET_ENABLED=true`):** at 15M/10M/7M it freezes the
  champion's snapshot, computes OUR `P(up)` via `probability.updown_probability` (the
  champion's `_structural_probability` math re-thresholded at the open price), records
  Polymarket's executable book, and after close grades model + market. Emits a compact
  `POLYMARKET SHADOW` card; "NO MATCHING MARKET" for assets Polymarket doesn't list.
- **Reliability:** all HTTP + SQLite work runs on the shadow's OWN background worker
  (`runner.py` queue + daemon); `observe()`/`reconcile()` only enqueue, so the live ~1s
  loop never blocks. Ledger `check_same_thread=False` + write lock. Default-OFF ⇒
  production byte-identical. Wiring: `checkpoint_v95` observe hook + `app.py` drain.
- **Live-validation TODO:** confirm the Gamma field names for the "price to beat"
  (window-open reference) and the resolution payload against the live API. `client.py`
  parses tolerantly and degrades to None (market-only grading) rather than guessing —
  enabling it can't crash/fabricate, but OUR-model grading needs that field wired.

## ✅ Shipped earlier THIS session (PR #16, merged to main) — A/B/C/D grade on the V9.5 CHECK
**Suite 1001 passed / 4 skipped** in a complete env (+2 tests). Merged to `main` via
`scripts/ship.sh` after the owner authorized always-ship.
- **Grade restored on the official check** — `build_ranked_checkpoint_panel` now renders each
  ranked pick as `medal asset side — confidence% · GRADE` (e.g. `🥇 SOL NO — 72% · B`). The
  grade is the champion's existing A/B/C/D `confidence_grade` (computed once in `analyse_v95`
  from selected_probability + data_quality), **surfaced — never recomputed**: `_extract_pick`
  adds `confidence_grade` to the pick contract and a defensive `panels_v95._grade()` maps it to
  `A/B/C/D` or `—`. No change to scoring, suppression markers, or the `V9.5 CHECK` tag.
- **CI fix (pre-existing red, not from this change)** — `.github/workflows/tests.yml` installed
  `pytest websockets flask cffi cryptography` but NOT `requests` (a hard import in
  `kalshi_rest`/`notifier`/`spot_client`), so 16 app-level modules errored on collection and CI
  was red on `main` too. Now installs `-r requirements.txt` (single source of truth) + `pytest`/
  `cffi`, so those modules import and run. Verified: full suite green with the real runtime deps.

## ✅ Shipped a prior session (branch `claude/trusting-bardeen-yufks0`) — champion-review fixes (calibration + edge levers)
**Suite 994 passed, 4 skipped (+8).** Frozen champion WEIGHTS untouched; these act on
the post-model calibration layer + entry-gate knobs. Deploy-pending on `main`.
- **OOS self-selecting calibration (DEFAULT ON, safe-by-construction)** — `ledger_v95`:
  `_calibration_fit` now splits resolved rows chronologically (older train / newer test) and
  `calibrate()` applies the held-out-best of {identity, Platt, isotonic} ONLY if it beats
  identity by `Q15_V95_CALIBRATION_OOS_FLOOR` — else identity. So "turning calibration on"
  can only help or no-op OOS, never silently ship a worse transform. This is the validated
  answer to the under-confidence (predicts 0.78 / wins 0.95) with NO change to the frozen model.
- **Platt convergence fix** — the slope ceiling was 1.50 (an under-confident model needs ~2 to
  sharpen), so Newton pinned at the clamp and looped → "unconverged" → identity forever. Now:
  wider clamp (`SLOPE_MAX=3.0`), higher budget (`MAX_ITERS=50`), and convergence judged on the
  APPLIED (post-clamp) step so a constrained optimum counts as converged. Verified: under-confident
  data now reaches slope ~2.2 and maps 0.70 → ~0.87.
- **calibration_experiment()** — per-checkpoint OOS Brier of identity vs Platt vs isotonic +
  the applied method; the numbers the next review reads to confirm the lift with real data.
- **Adverse-selection cost adder** (`Q15_V95_EDGE_ADVERSE_SELECTION_CENTS`, default 0.0): added
  to the edge's total_costs; the shadow-econ A/B measures the ~1c gap. Flip on once it proves out.
- **Volatility-aware edge bar** (`Q15_V95_EDGE_VOLATILITY_SCALING`, default OFF): required_edge
  *= 1 + k*(2c-1)^2, so extreme-conviction favourites need a thicker cushion.
- **15M min-prob default raised 0.58 → 0.60** (15M is a coin flip; 0.58 admitted near-random picks).
- **Refactors:** shared `_fit_platt` helper + `_brier`; `_select_calibration_method`. Helpers/flags:
  `_calibration_oos`, `Q15_V95_CALIBRATION_OOS_SELECT/FLOOR/TEST_FRACTION`,
  `CALIBRATION_SLOPE_MIN/MAX`, `CALIBRATION_INTERCEPT_CAP`; `CALIBRATION_ISOTONIC_FALLBACK`
  default flipped OFF→ON. Legacy calibration mechanics-tests opt into the legacy path explicitly.
- **Files:** `q15_upgrade/ledger_v95.py`, `q15_upgrade/checkpoint_v95.py`, `.env.example`,
  `tests/test_champion_fixes.py` (new), `tests/test_q15_ledger_review_fixes.py`,
  `tests/test_q15_v95_ledger_cache.py`, `tests/test_review_fixes_v5.py`.

## ✅ Shipped THIS session (branch `claude/trusting-bardeen-yufks0`) — flip-decision engine, net-edge gate, rank-by-skill
**Suite 986 passed, 4 skipped (+16).** Read-only wrt real exchanges; frozen-champion
live behaviour unchanged unless an explicit default-OFF flag is set. Deploy-pending on `main`.
- **Strict flip-decision engine** (`q15_upgrade/flip_decision.py` NEW + ledger `flip_decisions`
  table): per interval, for the #1 pick, decides YES/NO whether the predicted side genuinely
  FLIPS (settles the opposite way) by close. `flip_probability()` is a bounded, oriented blend
  (primary basis = manipulation score, but manipulation that SUPPORTS the pick lowers it — a
  high score never forces YES) of strike distance/crossings, wick/momentum/flow-against,
  book fragility, prediction stability, entropy, regime transition, spot-vs-contract
  disagreement and time. Thresholds are LEARNED per interval from completed post-reset history
  via chronological OOS (`select_threshold`), targeting >=80% YES-precision; Decision=YES only
  when the interval threshold is VALIDATED on the unseen test fold AND the probability clears
  it — else NO (background). Every decision is stored before settlement (one per
  contract+interval) and graded against the official result only (`resolve_ticker` sets
  `flipped = predicted_side != official_result`). Metrics (`flip_decision_report`): YES-precision,
  recall, FPR/FNR, coverage, n, by-interval + by-asset.
- **Full flip reset** (`reset_flip_data` / `flip_reset_at` / `flip_reset_status`): archives every
  current decision to `flip_decisions_archive`, clears the active table, stamps `flip_reset_at`;
  all thresholds/metrics read post-reset rows only. `Q15_V95_FLIP_SHOW_POST_RESET_ONLY`.
- **Visible output**: only the 3-line block inside each interval panel —
  `<CP> FLIP CHECK / Decision / Flip Probability / Required Threshold` (panels_v95). The old
  loose "Flip risk:"/"Manipulation:" lines and all manipulation math are now HIDDEN. No
  separate flip/manip report.
- **Net-edge-after-cost entry gate** (`_is_actionable_entry`, `Q15_V95_NET_EDGE_GATE_ENABLED`,
  default OFF): an ENTRY is actionable only if net edge >= min cents — attacks the -4.18c/pick
  P&L bleed. Default OFF (frozen champion).
- **Rank-by-skill** (`Q15_V95_RANK_BY_SKILL`, default OFF): orders #1/#2/#3 by decision
  priority -> confidence grade -> decisiveness -> net edge (grade tracks accuracy on the record;
  net-edge rank did not). Default OFF.
- **Leakage guards (tested):** thresholds chosen on TRAIN, validated on a later TEST fold, never
  tuned on the rows they grade; each decision freezes the threshold in force; temporary moves
  never count (graded on settlement only); one decision per interval; precision is YES-precision
  (not overall) so a NO-always model cannot look good; unvalidated intervals stay background.
- **Files:** `q15_upgrade/flip_decision.py` (new), `q15_upgrade/ledger_v95.py`,
  `q15_upgrade/checkpoint_v95.py`, `notifications/panels_v95.py`, `.env.example`,
  `tests/test_flip_decision.py` (new), `tests/test_q15_ranked_panel.py`.

## ✅ Shipped THIS session (branch `claude/trusting-bardeen-yufks0`) — updated-review fixes from LIVE data
**Ran `updated-review` against the LIVE Replit ledgers** (a dump script pulled the real
scoreboards: v95 1,106 resolved @ 68.6%, 15M 53%/10M 69%/7M 81%, NEGATIVE P&L −4.18¢/pick;
official manip alert 23.7% correct n=59; Platt calibration unconverged→identity at every
checkpoint; challenger behind champion). Implemented the data-grounded fixes. Suite **970
passed, 4 skipped** (+13). Read-only wrt real exchanges; frozen champion's live probabilities
unchanged unless an explicit default-OFF flag is set. Deploy-pending on `main`.
- **15M alert delivery OFF by default** (`checkpoint_v95._interval_alerts_enabled`,
  flag `Q15_V95_15M_ALERTS_ENABLED=false`): 15M is a coin flip that loses money, so its
  panel/entry/manip/follow-up DELIVERY is suppressed; the 15M prediction is still recorded
  observationally (learning + timing experiment). 10M/7M always deliver. Recaps still fire.
- **Manipulation standalone alert OFF by default** (`Q15_V95_MANIPULATION_ALERTS_ENABLED`
  default flipped True→False): the delivered manip alert was an anti-signal (23.7% correct).
  Detection/tracking still runs for the learning record.
- **Entry-timing experiment (observational)** — answers "is 13/12/11 min better than 15?"
  with data, not guesses. New isolated `timing_experiment` table; `record_timing_observation`
  in `run_cycle` captures the model's call at extra marks (`Q15_V95_TIMING_EXPERIMENT_SECONDS`
  default `780,720,660`), `resolve_ticker` grades them on settlement, `timing_experiment_scoreboard()`
  + an hourly-report block surface per-mark accuracy with Wilson CIs. Never delivered, never
  official, isolated from the 15M/10M/7M interval semantics (free-form `mark_seconds`).
- **Repaired the dead `prediction_stability` shadow signal** (`shadow_signals.compute_signals`):
  the 0..100 flip score was used as a 0..1 prob (`1.0 - flip` → clamped to 0), zeroing the
  signal on the live record (mean_abs_signal=0.0). Now normalised `/100.0` first.
- **Isotonic calibration as the unconverged-Platt fallback** (`ledger_v95`, flag
  `Q15_V95_CALIBRATION_ISOTONIC_FALLBACK=false`, DEFAULT OFF → no live change): when Platt
  fails to converge (the live situation at every checkpoint), use the isotonic curve instead
  of shipping identity (no calibration). Validate OOS before enabling on the money path.
- **Deferred (need significance testing per the FROZEN-champion invariant):** the net-edge
  entry gate and the #1/#2/#3 rank-ordering rework — both change live money behaviour, so
  they were NOT auto-shipped. Reconciling the challenger "control" proxy (45–50%) to mirror
  the live predicted side (68.6%) is also pending.
- **Files:** `q15_upgrade/checkpoint_v95.py`, `q15_upgrade/ledger_v95.py`,
  `q15_upgrade/shadow_signals.py`, `notifications/reporting.py`, `.env.example`,
  `tests/test_review_fixes_v5.py` (new), `tests/test_q15_manipulation_alert.py`.

## ✅ Shipped THIS session (branch `claude/modest-curie-9eex8c`) — default effort level → xhigh
**Harness config only — no app/test change; suite unchanged at 957 passed, 4 skipped.**
Deploy-pending on `main`. Owner asked to set the session to `ultracode` as a persistent
default. Per the Claude Code docs `ultracode` is **session-only by design** and explicitly
excluded from saved config (not part of the `effortLevel` setting, the `--effort` flag, or
`CLAUDE_CODE_EFFORT_LEVEL`) — so it cannot be persisted, and on the web/cloud env the
`/effort` command that would set it per-session is unavailable. Closest durable option:
- Added top-level `"effortLevel": "xhigh"` to `.claude/settings.json` (the highest
  *persistable* effort). Gives ultracode's deep reasoning depth but NOT its automatic
  workflow orchestration (that piece has no persistent setting).
- **Caveats:** web honoring of `effortLevel` from `settings.json` isn't doc-confirmed (only
  *hooks* are documented to carry into cloud sessions); the guaranteed-on-web alternative is
  the `CLAUDE_CODE_EFFORT_LEVEL=xhigh` env var in the claude.ai/code environment config. The
  setting applies repo-wide (higher cost/latency per turn for anyone using this repo).
- No test added: this changes Claude Code harness behavior, not the prediction app's behavior
  (the suite tests the app). **Files:** `.claude/settings.json` only.

## ✅ Shipped THIS session (branch `claude/optimistic-wright-zv15es`) — CLAUDE.md cleanup
**Docs-only.** Tightened the agent guide; no code/test change, suite **957 passed,
4 skipped**. Deploy-pending on `main`.
- Dropped the stale "31 test files" count (live suite is ~957) — removed the brittle
  number rather than re-hardcode one that rots.
- Collapsed the READ-FIRST blockquote to a pointer; the SessionStart hook already
  restates the `ENGINEERING_GUIDELINES` rule list at the start of every session.
- Tightened the `notifications/` note and the Merge-policy wording, preserving every
  rule intact (data-safety guard, STOP rule, HANDOFF update, 4-step procedure).
- Invariants section untouched. **Files:** `CLAUDE.md` only.

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

## 🔬 Shipped THIS session — diagnostic: capture Kalshi's INSTANT determined outcome (step 1 of instant settlement)
Owner: Kalshi gives the result instantly after the timeframe but takes a while to actually settle — can we
use the instant result? Investigation: BOTH settlement paths (`ledger_v95.reconcile_pending_from_market:1429`,
`market_cache._resolved_result:36`) only accept `market["result"] ∈ {YES,NO}` and skip otherwise; nothing
reads `status`/`settlement_value`/`expiration_value`. Reconcile runs every 30s. So if Kalshi populates
`result` only at FINAL settlement (the laggy part) we ignore the instant determination → the grid/official
record wait needlessly. This SUPERSEDES the earlier spot-vs-strike "provisional ~" plan: Kalshi's own
determined value is authoritative (no near-strike guessing risk).
- **Step 1 (this commit, read-only):** `reconcile_pending_from_market` now CAPTURES closed-but-`result`-empty
  markets — curated determination fields (`status`, `settlement_value`, `expiration_value`, `floor/cap_strike`,
  `strike_type`, …) + the full key list — into the return under `undetermined_market_samples`
  (+`undetermined_closed_count`), surfaced at **`/api/q15-v9-5/learning` → `last_market_reconcile`**. Bounded by
  `Q15_V95_UNDETERMINED_SAMPLE_LIMIT` (default 8). +2 tests; suite **925 passed, 13 skipped**.
- **Step 2 (NEXT, after one deployed window):** read the confirmed instant field and resolve from it
  (treat as official, or provisional-until-`result` finalizes). Don't guess the field blind — a wrong
  strike/value field would invert a result in the money path.

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

## ✅ Shipped THIS session — Read-only learning-progress report tool (merged from `claude/updated-review-2x7wyr`)
`tools/learning_progress.py` dumps the live state of every learning subsystem in one shot — run it **on
the Repl** (where the live SQLite ledgers are): `python3 tools/learning_progress.py` (human) or `--json`.
It opens the production v95 ledger and the Shadow-vs-Yours challenger ledger directly and calls the SAME
builders the `/api/q15-v9-5/*` endpoints use (scoreboard, calibration/metrics, accuracy, shadow-signal
A/B, ranked comparison, native delivery audit). Strictly read-only — never writes, skips an absent store
instead of creating an empty one. Tests: `tests/test_learning_progress.py`.
- Note: in a fresh web clone the committed DBs are a post-reset SEED (14 v95 preds, 0 resolved, 0
  weight updates; shadow DB not committed) — real numbers appear when run on the Repl.

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
## Current 2026-08-01 - Official spot REST top-book V2 deployed outcome-blind

V2 is frozen as `q15-rti-spot-rest-top-book-reservoir-v2`, SHA
`b4e3e342ae73c94679becb917a680020eabf9ee6cd3a80fa14b0781d2eb92a17`.
Its boundary is strictly after the 17:30 ET close (`1785619800`) and its first
eligible close is 17:45 ET (`1785620700`). It writes only to the independent WAL
database `data/q15_rti_spot_rest_top_book_v2.sqlite3`; V1 rows receive no V2
credit. Local request/receipt remains the only freshness authority. Exchange
mutation time is provenance only and may lead the host clock by at most the
frozen five-second sanity bound; it never establishes when a feature was
available. Provider/symbol/quote/endpoint/query identities, four stages, timing
limits, no-redirect behavior, monotonic-clock cross-check, schema, hashes, and
all safety exclusions remain frozen.

V2 was frozen with zero eligible rows and no outcome/label/model/P&L access. Its
focused suite passes 34/34 and the complete collector/V21/strategy regression
passes 272/272. The service restarted safely before the V2 boundary with 7/7
workers, protocol/hash valid, the exact worker alive, settlement feeds ready,
and zero V2 rows/errors/rejections. The first 17:45 ET fold still requires
outcome-blind validation after +90.

## Current 2026-08-01 - Official spot REST top-book V1 terminally excluded

A separate prospective source reservoir was frozen as
`q15-rti-spot-rest-top-book-reservoir-v1`, SHA
`291fb660cc05135704b8983f0644ce3253bb9de407bb7feb4d32f36436ee104c`.
Its boundary is close `1785618900` (17:15 ET) and first eligible close is
`1785619800` (17:30 ET). It submits only after the existing exact-stage Kalshi
quote and spot context are frozen, then uses seven isolated workers to request
official Coinbase/OKX level-1 books at 13M/+30/+60/+90. Local request/receipt
time is the freshness authority; an old OKX mutation timestamp is retained as
provenance and is not confused with a stale newly requested snapshot. Results
are canonical-hash-bound in the separate WAL database
`data/q15_rti_spot_rest_top_book_v1.sqlite3`; failures are retained and identities
cannot be overwritten. There are no outcome, scoring, notification, model, or
trading surfaces, no backfill, and V21 cannot read this database.

Final adversarial review at **zero eligible rows** found that the first frozen
document named each provider but still inherited asset symbols from mutable
`spot_client` configuration. Before any eligible capture, the protocol and
runtime were amended to hash-freeze every provider/symbol/quote-currency plus
the exact endpoint/query contract. Readiness now also proves the SQLite unique
constraint and rejects duplicate exact or asset-stage identities. The replaced
zero-row hash is permanently non-evidentiary.

Runtime startup also revalidates the exact column set, declared unique identity,
every canonical evidence JSON/hash, every row/evidence field binding, and the
frozen protocol/schema identity before it loads completed keys. A modified
schema, missing uniqueness constraint, or tampered existing row disables this
optional reservoir instead of trusting or overwriting it; the exact/V21 worker
continues independently.

Network capture now refuses redirects, preserves real non-200 HTTP status codes,
and compares wall-clock elapsed time against an independent monotonic timer.
Backward/stepped wall time or more than 100ms disagreement is retained as a
failed `LOCAL_CLOCK_DISCONTINUITY` row rather than treated as fresh evidence.

The first V1 fold produced all 28 expected hash-bound rows, but eight OKX rows
and one Coinbase row were rejected because an implementation-only one-second
future-source cutoff contradicted the frozen declaration that exchange mutation
time is provenance rather than receipt freshness. The defect was diagnosed from
source timestamps/failure codes only. V1 is terminally excluded, cannot be
backfilled, and its evidence rollup is preserved in
`reports/q15_rti_spot_rest_top_book_v1_terminal_exclusion.json` with ordered
evidence-hash SHA
`bc27e20a6e2efb0901d8c77f4ad74fec37d6d681a18b7f7f5858f5086528d64b`.

`tools/q15_rti_spot_rest_top_book_readiness.py` now validates V2 protocol/hash/schema,
exact ticker/close/stage timing, official provider identity, response timing,
book geometry, canonical evidence, and seven-asset/four-stage folds while
selecting no outcomes. Coinbase and OKX live public response shapes were checked.
The final V1 exact/reservoir suite passed 33/33 and the full V1-era regression
passed 271/271 before its terminal exclusion.

Latest outcome-blind readiness: V21 is 9/180 complete windows, 63 feature rows,
and 47 row-level executable; the execution-ladder reservoir is 6 usable windows
with 9 genuine next-level fill recoveries. Frozen controls remain
unchanged.
