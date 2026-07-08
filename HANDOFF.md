# Session handoff

> **OPERATIONAL NOTE (owner, 2026-07-08): the Repl is disconnected — the app now
> runs on the owner's LOCAL machine.** The local checkout pulls `main` and
> `tools/learning_export.py` still force-pushes `learning-snapshots` hourly, so
> the review workflow is unchanged. But "Stop ▸ Run on the Repl" advice is
> obsolete: deploys = local `git pull` + restart the local app process. Older
> references to "the Repl" in this file and CLAUDE.md should be read as "the
> local host".

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
