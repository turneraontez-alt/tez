# TT-Edge prediction system v2 — build plan

Owner-approved direction (2026-07-19): evolve from the hand-set 3-feature
blend into a fully fleshed-out, league-aware prediction system that makes
its OWN odds, validated before it is trusted. This document is the standing
plan; build phases in order, each behind the shadow/challenger discipline
(observe → grade → promote only on evidence). Nothing here changes live
alerting until a promotion gate passes.

## Ground rules (inherited, non-negotiable)

- Analysis and alerts only; nothing places bets.
- No look-ahead: every feature is computed strictly from data timestamped
  before the match's start (the codebase's aware-`now` discipline).
- Every prediction (not just picks) is graded — the corpus is unbiased.
- Champion stays frozen; challengers observe silently; promotion needs a
  significance gate, and the kill criteria in README.md still apply.
- Walk-forward evaluation only (train on past, test on future). Never
  random cross-validation on time-series data.

## Current state (baseline to beat)

- Features: decayed/smoothed H2H rate, last-15 form differential (hot-5
  doubled), 60-day common-opponent differential.
- Blend: hand-set weights 0.45/0.35/0.20 through a sigmoid; identity
  calibration; graded corpus ~270 predictions and growing hourly.
- Known weaknesses: weights unfitted; overconfident at extremes (both
  biggest-edge picks lost on day one); H2H and form double-count player
  strength; no fatigue/schedule awareness; single global calibration
  across three structurally different leagues.

## Phase A — data foundation

1. **Results backfill**: page BetsAPI `events/ended` per league backward
   (target: 90+ days each for TT Elite 29128, TT Cup 29097, Czech Liga Pro
   22742; add Setka Cup 22307 results-only for rating linkage). Store into
   `tt_results` + `tt_matches` via the existing translators, marked
   `source='backfill'`.
2. **Per-set scores**: verify what BetsAPI serves per event (`ss` detail /
   event view endpoint). Set-level data unlocks the margin, clutch, and
   comeback features below; if unavailable, those features degrade to
   match-level proxies and the plan continues.
3. **New tables** (all additive, `tt_`-prefixed, migration-versioned):
   - `tt_player_ratings(player_source_id, league_id, elo, games, rd,
     updated_at)` — current rating per player per league.
   - `tt_rating_history(match_source_id, player_source_id, elo_before,
     elo_after, k_used)` — full audit trail, enables deterministic replay.
   - `tt_feature_rows(match_source_id, feature_version, computed_at,
     features_json)` — the frozen pre-match feature vector for every
     scanned match; training data and audit in one place.
   - `tt_model_versions(id, kind, params_json, fitted_on_rows, metrics_json,
     active)` — fitted weights/coefficients, INACTIVE until promoted
     (mirrors `tt_calibration_versions`).

## Phase B — ratings engine (the new core)

- **Elo per player per league**, K decaying with games played (fast entry,
  stable veterans). League pools are mostly disjoint; where a player
  appears in multiple leagues, keep per-league ratings plus a shrunk
  cross-league prior for their debut in a new league.
- **Margin-of-victory scaling**: 3-0 moves ratings more than 3-2
  (multiplier on K from set differential; standard MOV-Elo with
  autocorrelation guard).
- **Uncertainty (Glicko-style RD)**: new/returning players carry high RD;
  predictions abstain (insufficient_data) while RD is above a threshold —
  replaces the blunt "n_meetings < 5" gate with a principled one.
- **Nightly + per-cycle updates**: ratings update incrementally as results
  arrive in the hourly cycle; deterministic rebuild from
  `tt_rating_history` must reproduce identical values (test-enforced).
- **Rating → probability**: logistic on rating gap, slope FITTED per league
  on backfilled history (walk-forward), not assumed.
- Ship as a **shadow head**: every scanned match gets an Elo prediction
  recorded alongside the blend's; graded identically; zero effect on picks.

## Phase C — feature expansion (the missing features)

Standard features stay. Add, in priority order (each derivable from data
we already collect — timestamps, results, scores):

1. **Same-day rematch** — these leagues run round-robin days; opponents
   often meet twice in one session. Prior meeting TODAY (result + margin)
   is a strong, market-underpriced signal. Cheap and high value.
2. **Fatigue / workload** — matches played in the last 3h / 12h / 24h,
   minutes since last match, match number in the player's current session
   (8–15 matches/day is normal; late-session collapse is real).
3. **Time-of-day profile** — per-player win-rate residual by hour bucket
   (Czech Liga runs around the clock; 3am-block specialists and faders
   exist). Needs the residualization from #6 to avoid confounding with
   opponent strength.
4. **Layoff / rust** — days since last competitive match; new-to-league
   flag (interacts with RD gate).
5. **Clutch profile** — deciding-set (2-2 → set 5) win rate, comeback rate
   from 0-2 down, tight-set (11-9/12-10) win rate. Requires per-set data
   from Phase A.2; else degrades to "share of 3-2 results won".
6. **Opponent-quality-adjusted form (form residual)** — replace raw form
   rate with actual-vs-Elo-expected over the last N matches ("beating
   expectations" momentum). Fixes the current double-count where form and
   H2H both proxy raw strength.
7. **H2H residual** — model the H2H record's deviation from what the
   rating gap predicts (style matchup), not the raw rate. A 6-2 H2H
   against an equal-rated player means something; 6-2 against a much
   weaker one means nothing. Replaces the raw H2H feature.
8. **Margin trend** — average set differential over last 10 vs career
   (form quality, catches decline before W/L does).
9. **League context** — per-league favorite-longshot bias correction and
   per-league calibration (three separate Platt layers); listed-first
   "home" bias measured and corrected if present.
10. **Market features — SEPARATE overlay head only**: opening-vs-current
    line move, velocity, (later) cross-book divergence. The independent
    model must stay market-blind so its odds are truly our own; a distinct
    "market-aware" ensemble head may consume both. Fix-risk steam stays a
    hard VETO, never a feature that could rationalize it.

Every feature ships with: a leakage test (computable strictly pre-match),
an ablation backtest number (did log-loss improve on walk-forward?), and a
null-behavior contract (missing data drops the feature, never fakes it).

## Phase D — fitted model + calibration

- Logistic regression over `tt_feature_rows` (small, interpretable,
  auditable — no black boxes while the corpus is thin), L2-regularized,
  walk-forward refits on a schedule; coefficients land in
  `tt_model_versions` INACTIVE.
- Per-league Platt calibration on top (replaces the single global iden-
  tity transform), fitted only past `TT_EDGE_CALIBRATION_MIN_ROWS` per
  league.
- Three heads recorded per match from here on: **blend** (current
  champion), **elo**, **fitted** — all graded, one scoreboard.

## Phase E — evaluation & promotion gates

Scoreboard per head, per league, walk-forward only:

- Brier score + log-loss (primary), calibration curve by probability
  bucket, favorite-longshot skew.
- **vs market**: Brier of head vs Brier of the de-vigged close; CLV of
  hypothetical picks (this is the kill-criteria metric — start measuring
  it NOW, not at Phase E: store the last pre-start price per match).
- Promotion gate (any head replacing the champion): ≥500 graded matches
  in-league, better log-loss than champion with p < 0.05 (paired test on
  per-match losses), calibration slope in [0.9, 1.1], and no league where
  it is materially worse. Manual owner sign-off flips `active` — same as
  today's calibration promotion.

## Phase F — productionize

- Picks engine consumes the promoted head; edge threshold re-derived from
  measured calibration error (not the flat 5pts); staking stays
  quarter-Kelly capped.
- Scan order becomes edge-ranked within each cycle before claiming slots
  (fixes the observed board-order quirk where a +8.8 took the last slot
  over a +22.5).
- Hourly report additions: rating movers, head-vs-head scoreboard deltas,
  CLV running total.

## Sequencing & effort

A (foundation) → B (Elo shadow) ship together first — that alone starts
producing the comparison data everything else needs. C lands feature-by-
feature, each with its ablation number. D/E when per-league corpora reach
fitting size. F only after a promotion gate passes. Every phase is a
normal session's work or less, tests-first, shipped to `main` per the
repo's merge policy.

## Explicit non-goals (for now)

- In-play / live betting models (Phase 3 of the original spec; different
  data cadence entirely).
- Black-box models (GBMs/NNs) before the interpretable baseline is
  exhausted — with ~10 features and thousands of matches, logistic + good
  features is the right capacity, and every coefficient is an argument we
  can audit.
- Automated bet placement. Never.
