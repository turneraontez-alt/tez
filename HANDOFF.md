# Session handoff

Working notes so a fresh session can pick up cleanly. Delete when no longer
useful. See `CLAUDE.md` for architecture and `SYNC.md` for the Replit sync.

## Project in one line
Read-only paper monitor for Kalshi 15-minute crypto binaries. Predicts YES/NO at
the 15m/10m/7m checkpoints, sends Telegram alerts, learns from settled results.
Never places real orders. Runs on Replit; **GitHub `main` is the source of truth.**

## Git workflow (follow this)
- Develop on branch **`claude/wizardly-fermat-fuxwxt`**, open a PR into `main`,
  merge it. Each logical change = its own PR (that's how this repo has gone).
- After merging, the Replit deploy is updated by following `SYNC.md`
  (`git fetch ... main` → `git reset --hard FETCH_HEAD`).
- ⚠️ The Replit Agent makes its own commits and will re-diverge from GitHub if it
  edits code. Keep GitHub authoritative.
- Tests: `python3 -m pytest tests/ -q` → **325 passing, 4 skipped**. Add a test
  with every behavior change.

## Just completed (this session)
**Improvement #1 — market-price anchoring** (commit on the dev branch).
`analyse_v95` (in `q15_upgrade/checkpoint_v95.py`) now shrinks the model
probability toward the Kalshi market-implied probability, scaled by model
confidence (`data_quality × evidence_quality × Q15_V95_MARKET_ANCHOR_STRENGTH`).
- Helpers: `_market_implied_yes`, `_market_anchored_probability`.
- Drives side/conservative/edge; challenger anchored identically; `raw_yes` and
  the structural baseline are untouched.
- **Default is ON (strength 1.0, confidence-scaled).** It's a paper monitor so
  this is safe, but flag it for review. To disable: `Q15_V95_MARKET_ANCHOR_STRENGTH=0`.
  To anchor harder: lower the strength.
- New API fields: `model_yes_probability`, `market_implied_yes_probability`,
  `market_anchor` (see `/api/q15-v9-5/predictions` and `/diagnostics`).

## Immediate next step — DONE
The checkpoint message now shows the market-implied prob next to each pick
(`build_v95_message` in `q15_upgrade/checkpoint_v95.py`): the per-pick line reads
e.g. `🥇 BNB YES — 71.2% vs mkt 51.5% · grade B · NORMAL`. It uses
`market_implied_yes_probability` for the selected side (inverted for a NO pick) and
omits the ` vs mkt …` annotation when there's no quote. Tests live in
`tests/test_q15_v95.py` (`test_message_shows_market_implied_probability`,
`test_message_omits_market_implied_when_no_quote`).

## Next up
Start the accuracy roadmap below at **#2 (volatility model)** — the biggest lever.

## Remaining roadmap (my earlier prioritized suggestions, highest leverage first)
2. **Volatility model** — biggest accuracy lever at this horizon. Add intraday
   vol seasonality, jump/gap detection, and blend Deribit implied vol into
   `_robust_volatility` (`checkpoint_v95.py`).
3. **Calibration** — isotonic as an alternative to Platt; time-decay weighting of
   old results; plot the reliability curve on the dashboard. Lives in
   `ledger_v95.calibrate` and the scoreboard.
4. **Offline eval loop** — strengthen `q15_upgrade/oos_v9.py` into a real backtest
   so model variants can be A/B'd before shipping.
5. **Position sizing** — fractional Kelly on calibrated edge-after-costs (only if
   this ever drives real money).

## Invariants — do not break
- Read-only; nothing touches a real exchange order.
- Production champion weights are FROZEN; only the shadow challenger learns;
  promotion is manual + significance-tested.
- Keep `V9.5 CHECK` in checkpoint message headers and the suppression markers
  (`ENTRY RECOMMENDED` / `NO ENTRY YET`); keep the `Hourly Report —` header on the
  canonical report (the reformatter-bypass keys on it).
- Don't edit the frozen legacy chain (`checkpoint_v91..v94*`) unless changing base
  behavior — `v95` subclasses it.

## Gotchas
- Learning/scoreboard data is sparse until markets actually settle. Don't tune
  anything (incl. anchor strength) on tiny samples — wait for the calibration
  curve, and the promotion gate is significance-tested for a reason.
- The shared `MarketResultCache` (`market_cache.py`) caches only resolved markets;
  all four settlement reconcilers go through it.
