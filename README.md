# Kalshi 15-Minute Crypto Monitor

A read-only, paper-trading monitor for Kalshi crypto binary markets. Watches
BTC, ETH, SOL, XRP, DOGE, BNB and HYPE 15-minute contracts, runs a multi-factor
edge model, and fires
Telegram alerts at the 15m, 10m, and 7m checkpoints — plus a between-checkpoint
**DIP alert** when a favorable price appears mid-window.

**Version:** 2.0.0 · **Policy:** q15-v9.5.2

---

## Features

- Real-time Kalshi orderbook + Coinbase/OKX spot price feed
- Multi-factor edge model: spread, depth, momentum, calibrated probability
- Checkpoint alerts at 15m / 10m / 7m before contract close
- Between-checkpoint DIP alert when conservative edge ≥ 6¢ appears
- Reliable Telegram outbox with idempotency keys, retry, and dead-letter
- Shadow learning engine (observational — never executes real trades)
- Live dashboard at `/`; JSON API at `/api/snapshot`
- 2690+ passing tests across 219 test files

---

## Quickstart

### Requirements

- Python 3.11+
- PostgreSQL database (`DATABASE_URL` env var)
- Kalshi API credentials
- Telegram bot token + chat ID

### Install

```bash
pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
# Edit .env with your credentials
```

### Run

```bash
python3 app.py
```

The server starts on `PORT` (default `8000`). Open `http://localhost:8000` for
the dashboard, or hit `http://localhost:8000/api/snapshot` for a JSON status
snapshot.

---

## Required Environment Variables

| Variable | Description |
|---|---|
| `KALSHI_API_KEY_ID` | Kalshi API key ID |
| `KALSHI_PRIVATE_KEY` | Kalshi EC private key (PEM) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Telegram chat/channel ID |
| `DATABASE_URL` | PostgreSQL connection string |

See `.env.example` for all optional tuning variables (~300 `Q15_*` knobs).

---

## API Endpoints

| Path | Description |
|---|---|
| `GET /` | Live dashboard (HTML) |
| `GET /api/snapshot` | Full JSON status snapshot |
| `GET /api/q15-v9/health` | Health check |
| `GET /api/q15-v9/telegram-outbox` | Outbox queue state |
| `GET /api/q15-v9/dead-letters` | Dead-letter Telegram messages |
| `GET /data/health` | Deploy health check |

---

## Architecture

```
Kalshi REST/WS  ──►  refresh_loop (every ~1s)
Coinbase/OKX    ──►      │
                         ▼
                   EdgeModel + CalibratedEdge
                         │
                   WindowFocus (15m/10m/7m checkpoints + DIP)
                         │
                   SignalEngine ──► claim_event (dedup)
                         │
                   ReliableTelegramOutbox ──► Telegram
                         │
                   ShadowLearning (read-only, never executes)
```

The prediction and alert path is **strictly read-only and paper-only** — the model
chain never places, modifies, or cancels an order.

> **One exception, and it is real money.** `q15_upgrade/executor/` is a separate,
> default-OFF, opt-in layer that turns v2's paper fire signals into REAL Kalshi
> orders. It is triple-gated — `Q15_EXEC_ENABLED` (default `false`), `Q15_EXEC_DRY_RUN`
> (default `true`, logs the order it *would* send), and `Q15_EXEC_KILL` (panic button,
> blocks all placement). There is a second, independently gated "YES bot"
> (`Q15_EXEC_YES_*`). Nothing else in the repo can submit an order; the guard tests in
> `tests/test_q15_v9*.py` enforce that for the decision engine, and the executor has its
> own coverage in `tests/test_executor.py` and `tests/test_executor_lifecycle.py`.
> **Do not read "read-only" as "cannot trade" — check the executor env before running.**

---

## Tests

```bash
python3 -m pytest tests/ -q
# 2690 passed, 5 skipped
```

---

## Project Layout

```
app.py                  Flask app + refresh loop
db.py                   PostgreSQL signal store
notifications/          All pure Telegram code (one package)
  notifier.py           Telegram delivery + suppression
  outbox_v9.py          Reliable Telegram outbox
  reporting.py          Hourly performance report
  panels_v95.py         V9.5 checkpoint/ranked/recap panels
  manipulation_alert.py Manipulation alert
  alert_config.py       Alert thresholds
q15_upgrade/
  window_focus.py       15m/10m/7m checkpoints + DIP alert
  checkpoint_v95.py     V9.5 checkpoint logic
  calibrated_edge.py    Edge model (read-only overlay)
  learning_store.py     Shadow learning store
  signals.py            Signal engine
  runtime.py            Entry candidate lifecycle
  model.py              Core edge/probability model
  professional_v7.py    Message formatting + diagnostics
q15_upgrade/executor/   OPT-IN live-order layer (default OFF — see the note above)
tests/                  219 test files, 2690 tests
```
