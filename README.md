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
- Shadow learning engine (read-only — never executes real trades)
- Live dashboard at `/`; JSON API at `/api/snapshot`
- 315+ passing tests across 31 test files

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

All alert logic is **strictly read-only and paper-only** — no real orders are
ever placed or submitted.

---

## Tests

```bash
python3 -m pytest -q
# 275 passed, 2 known pre-existing failures in test_q15_self_review_real.py
```

---

## Project Layout

```
app.py                  Flask app + refresh loop
notifier.py             Telegram notifier (outbox wrapper)
db.py                   PostgreSQL signal store
q15_upgrade/
  window_focus.py       15m/10m/7m checkpoints + DIP alert
  checkpoint_v95.py     V9.5 checkpoint logic
  outbox_v9.py          Reliable Telegram outbox
  calibrated_edge.py    Edge model (read-only overlay)
  learning_store.py     Shadow learning store
  signals.py            Signal engine
  runtime.py            Entry candidate lifecycle
  model.py              Core edge/probability model
  professional_v7.py    Message formatting + diagnostics
tests/                  25 test files, 275 tests
```
