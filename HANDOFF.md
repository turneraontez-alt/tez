# Session handoff

Working notes so a fresh session can pick up cleanly. Delete when no longer
useful. See `CLAUDE.md` for architecture and `SYNC.md` for the Replit sync.

## Project in one line
Read-only paper monitor for Kalshi 15-minute crypto binaries. Predicts YES/NO at
the 15m/10m/7m checkpoints, sends Telegram alerts, learns from settled results.
Never places real orders. Runs on Replit; **GitHub `main` is the source of truth.**

## Current state
- `main` is at the **PR #6 merge**. This session shipped PR #5 (checkpoint message)
  and PR #6 (deploy-scanner cleanup); both merged.
- Tests: `python3 -m pytest tests/ -q` → **327 passing, 4 skipped**.
- ⚠️ `pytest` is NOT preinstalled in a fresh Claude cloud container — run
  `pip install pytest -q` first.

## Git workflow (follow this)
- Develop on branch **`claude/wizardly-fermat-fuxwxt`**, open a PR into `main`.
- **Merge each PR once the suite is green** (owner asked Claude to merge after
  tests pass). Each logical change = its own PR.
- After merging, update the Replit deploy via `SYNC.md` (`git fetch ... main` →
  `git reset --hard FETCH_HEAD`).
- Set `git config user.email noreply@anthropic.com` / `user.name Claude` so commits
  are "verified". Merge commits made via the GitHub API are committed by
  `noreply@github.com` and will show "Unverified" — cosmetic, don't rewrite them.
- ⚠️ The Replit Agent makes its own commits and will re-diverge from GitHub if it
  edits code. Keep GitHub authoritative. Add/adjust a test with every behavior change.

## Replit deployment (runbook)
Live app is a Replit **Deployment** at `phone-dashboard.replit.app`.
- **Type: Reserved VM** (always-on). It runs a ~1s loop + sends Telegram alerts
  around the clock, so Autoscale (sleeps when idle) is wrong. `app.py:852` reports
  `reserved-vm` when `REPLIT_DEPLOYMENT` is set.
- **Run command: `python3 app.py`** — binds `0.0.0.0:$PORT` (default 8000,
  `app.py:908`). Set it in the Deployment config's "Run command" field, or in
  `.replit` under `[deployment]` as `run = ["python3", "app.py"]`. The top-level
  `run =` only drives the workspace Run button; deployments need their own.
- **Build command: none** (Flask; deps install from `requirements.txt`).
- `.replit` / `replit.nix` are NOT in the repo (Replit-managed), so the SYNC.md
  `git reset --hard` never touches the deploy config.
- **Security scan:** publishes were failing ~7s at the deploy security-scan step.
  PR #6 removed the code-side trigger (literal `-----BEGIN ... PRIVATE KEY-----`
  markers + a Telegram-token-shaped string in `.env.example` and two
  `kalshi_auth.py` comments). If it STILL dies with "scan skipped: connection
  lost", that's Replit infra, not our code → status.replit.com + Replit support
  (build IDs seen: 3c97913b / f003d57e).
- **Sync auth:** repo is private, so the SYNC.md fetch needs a fine-grained PAT
  (Contents: Read): `git fetch https://YOUR_TOKEN@github.com/turneraontez-alt/tez.git main`.

## Done this session
- **PR #5 — checkpoint message shows market-implied prob.** `build_v95_message`
  per-pick line reads e.g. `🥇 BNB YES — 71.2% vs mkt 51.5% · grade B · NORMAL`
  (market prob for the selected side, inverted for NO, omitted when no quote).
  Tests: `test_message_shows_market_implied_probability`,
  `test_message_omits_market_implied_when_no_quote`.
- **PR #6 — deploy secret-scanner cleanup.** Sanitized `.env.example` placeholders
  and two `kalshi_auth.py` comments so they no longer match private-key / bot-token
  secret rules. No behavior change (functional PEM substrings + regexes untouched).
- (Prior) **#1 — market-price anchoring.** `analyse_v95` shrinks the model prob
  toward the Kalshi market-implied prob, scaled by `data_quality × evidence_quality
  × Q15_V95_MARKET_ANCHOR_STRENGTH` (default 1.0; set 0 to disable). Helpers
  `_market_implied_yes`, `_market_anchored_probability`. `raw_yes` + structural
  baseline untouched; challenger anchored identically.

## Next up — accuracy roadmap (highest leverage first)
2. **Volatility model** — biggest lever at this horizon. Add intraday vol
   seasonality, jump/gap detection, blend Deribit implied vol into
   `_robust_volatility` (`checkpoint_v95.py`). Ship behind a default-OFF `Q15_*`
   flag so it can't shift the FROZEN champion's live predictions until enabled.
3. **Calibration** — isotonic as an alternative to Platt; time-decay weighting of
   old results; plot the reliability curve on the dashboard. Lives in
   `ledger_v95.calibrate` and the scoreboard.
4. **Offline eval loop** — strengthen `q15_upgrade/oos_v9.py` into a real backtest
   so model variants can be A/B'd before shipping.
5. **Position sizing** — fractional Kelly on calibrated edge-after-costs. Deferred:
   read-only paper monitor, so only relevant if it ever drives real money.

## Invariants — do not break
- Read-only; nothing touches a real exchange order.
- Production champion weights are FROZEN; only the shadow challenger learns;
  promotion is manual + significance-tested. Gate model-behavior changes behind
  default-OFF `Q15_*` flags.
- Keep `V9.5 CHECK` in checkpoint message headers and the suppression markers
  (`ENTRY RECOMMENDED` / `NO ENTRY YET`); keep the `Hourly Report —` header on the
  canonical report (the reformatter-bypass keys on it).
- Don't edit the frozen legacy chain (`checkpoint_v91..v94*`) unless changing base
  behavior — `v95` subclasses it.
- Keep `.env.example` free of strings that match secret scanners (no real
  `-----BEGIN ... PRIVATE KEY-----` markers or `\d{8,10}:AA…` token shapes).

## Gotchas
- Learning/scoreboard data is sparse until markets actually settle. Don't tune
  anything (incl. anchor strength) on tiny samples — wait for the calibration
  curve; the promotion gate is significance-tested for a reason.
- The shared `MarketResultCache` (`market_cache.py`) caches only resolved markets;
  all four settlement reconcilers go through it.
