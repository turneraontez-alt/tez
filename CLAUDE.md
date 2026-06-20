# CLAUDE.md — agent guide

Read-only paper-trading monitor for Kalshi 15-minute crypto binaries
(BTC, ETH, SOL, XRP, DOGE, BNB, HYPE). It predicts YES/NO at the 15m / 10m / 7m
checkpoints, sends Telegram alerts, and learns from officially settled results.
**It never places, modifies, or cancels a real order.**

## Run / test
- Tests are the source of truth for behavior: `python3 -m pytest tests/ -q`
  (31 test files). Add/adjust a test with every behavior change.
- App: Flask in `app.py` — dashboard at `/`, JSON under `/api/...`. Runs on Replit.
- Config is entirely env-driven (`Q15_*`); see `.env.example`. No secrets in code.

## Where things live (read these first for most tasks)
- `app.py` — Flask routes + the ~1s refresh loop that drives every subsystem.
- `analysis.py` — builds the per-asset snapshot (spot, candles, orderbook, target).
- `spot_client.py`, `q15_upgrade/market_data_v95.py` — price/flow feeds
  (Coinbase / Kraken / OKX / Deribit), with a bounded last-good spot fallback.
- `q15_upgrade/checkpoint_v95.py` — **the live decision engine**: `analyse_v95`
  (probability + edge), `build_v95_message` (the checkpoint alert), `run_cycle`.
  Start here for prediction / alerting logic.
- `q15_upgrade/ledger_v95.py` — **the learning system**: prediction ledger,
  Platt calibration, shadow challenger (global + per-regime), the scoreboard
  (accuracy / P&L / Wilson CIs by interval, rank, asset) and significance-tested
  promotion. SQLite at `data/`.
- `reporting.py` — hourly Telegram report (leads with the ledger scoreboard).
- `notifier.py` — Telegram delivery + alert suppression (`should_suppress_alert`).
- `q15_upgrade/window_focus.py` — two-window (15m/10m/7m) live controller:
  EV ranking, the cross-checkpoint side veto, checkpoint alerts, self-review.
- `performance.py`, `db.py` — Postgres `signals` store + settlement stats.

## Do NOT read these unless changing base behavior
`checkpoint_v91/v92/v93/v94/v94_unified/v94_adaptive15.py` are frozen legacy
layers (~7k lines). v95 subclasses the chain:
`v95 → v94_unified → v94 → v93 → v92 → v91`. They work; skip them otherwise.

## Invariants (do not break)
- Read-only. Nothing touches a real exchange order.
- Production "champion" model weights are FROZEN. Only the observational shadow
  challenger learns; promotion is manual and significance-tested.
- Telegram messages are HTML. Preserve suppression markers
  (`ENTRY RECOMMENDED` / `NO ENTRY YET`) and the `V9.5 CHECK` tag on checkpoint
  messages (the formatter and suppression both key on them).
- The canonical hourly report is detected by its `Hourly Report —` header so it
  bypasses the legacy reformatters; keep that header.
