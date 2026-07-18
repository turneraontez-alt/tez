# TT-Edge — TT Elite Series betting analysis pipeline

Analysis and alerting ONLY. This system finds prices and sends Telegram
alerts; a human clicks buttons. Nothing here places, modifies, or cancels a
bet — automated execution violates sportsbook ToS and gets accounts limited.
Keep it that way.

## KILL CRITERIA (decided now, not after tilt)

**If, after 200 graded recommendations, ROI is below −8% OR closing-line
value is consistently negative, the model has no edge and the project stops
or gets reworked.** No extensions, no "one more tweak while live". These
numbers were chosen before the first bet so that future-you honors them.

Discipline for the first ~300 graded matches: paper / minimum stakes, data
collection only. The hand-set model weights are not fitted, and the
calibration transform stays identity, until that corpus exists.

## What it does

On each run (`jobs/scan.py`):

1. Loads today's TT Elite board + per-match H2H + per-player form from
   sofascore payloads captured by Playwright XHR interception
   (`scrapers/sofascore.py`) — envelope files stamped with `fetched_at`.
2. Reads the book's prices from manually-entered odds snapshots
   (`jobs/odds_entry.py`; Phase 1 replaces this with a scraper — the
   snapshot shape already matches).
3. Rejects anything stale (board ≥ 30 min, H2H/form ≥ 24 h, odds ≥ 10 min —
   `freshness.py`; stale data was the manual-mode failure this replaces).
4. Computes P(win) as a logistic blend of H2H rate (Laplace-smoothed,
   age-decayed), form differential (last 15, last 5 × 2), and
   common-opponent differential (last 60 days) — `model/`.
5. De-vigs the two-way price proportionally and computes
   `edge = model_p − fair_p` in exactly one place (`edge/edge_calc.py`).
6. Abstains (logged NO BET) on: stale data, insufficient data (< 5 H2H
   meetings AND < 3 common opponents), |edge| < 3 pts, edge < 5 pts,
   ≥ 15¢ adverse line move (fix-risk: movement is the news — hard pass),
   no bankroll, or ≥ 3 open picks (session correlation).
7. Otherwise sizes quarter-Kelly capped at 5% of the DB-stored bankroll
   (floor $1, cap dominates) and alerts via Telegram. Row first (the
   idempotency claim — a rescan can never double-alert), then send, then
   `alert_delivered_at` only after Telegram confirms.

`jobs/grade.py` records official results, settles exactly once (settlements
and the bankroll move in one transaction), grades EVERY prediction (not just
recommendations — the unbiased calibration corpus), and can fit/promote
Platt calibration versions (fit stores INACTIVE; promotion is a deliberate
manual flag).

Two deliberate Phase-0 policies, revisit with calibration data:

- **Model-favored side only.** A price-only "edge" on a side the model gives
  < 50% is not taken — an uncalibrated model disagreeing with the market
  that hard is more likely wrong than right.
- **Paper bankroll.** Every open recommendation settles, delivered or not;
  the bankroll is a paper ledger of what the system recommended until the
  operator goes live and reconciles it by hand.

## Quickstart — automated (Phase 1 autoscan)

```bash
pip install playwright                          # once, on the host
python3 -m tt_edge.jobs.bankroll --set 65.00    # once
python3 -m tt_edge.jobs.autoscan                # loop: fetch -> grade -> scan -> alert
```

Every 30 minutes the autoscan pulls the TT Elite boards (yesterday for
results, today + tomorrow for picks), each upcoming match's H2H / form /
sofascore odds, grades finished matches (bankroll moves automatically), and
alerts qualifying edges. Telegram: set `TT_EDGE_TELEGRAM_BOT_TOKEN` +
`TT_EDGE_TELEGRAM_CHAT_ID` for a dedicated channel, or leave them unset and
the default fallback rides the Q15 monitor's existing bot/chat
(`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`) — zero new setup.

**Sofascore prices are not your book's.** Autoscan alerts carry a
`Price source: sofascore — take X or better` line: the edge exists AT that
price; if your book posts a worse one, skip the bet.

## Quickstart — manual (Phase 0 flow, still supported)

```bash
python3 -m tt_edge.jobs.bankroll --set 65.00
python3 -m tt_edge.scrapers.sofascore --url <sofascore TT page> --out-dir data/tt_scrape
python3 -m tt_edge.jobs.odds_entry --match <event_id> --home -120 --away +100
python3 -m tt_edge.jobs.scan --data-dir data/tt_scrape --dry-run
# ... after the session:
python3 -m tt_edge.jobs.grade --result <event_id>=home
```

Manual odds entries live under book `manual`, autoscan prices under book
`sofascore`; each scan reads only its configured book (`TT_EDGE_BOOK`), so
the two flows never contaminate each other's line-movement history.

Config is entirely env-driven (`TT_EDGE_*`, documented in `.env.example`).
Storage defaults to SQLite at `data/tt_edge.sqlite3`; point
`TT_EDGE_DATABASE_URL` at Postgres for production (tables are `tt_`-prefixed
and coexist with the Q15 schema).

## Testing

`python3 -m pytest tests/test_tt_edge_*.py -q` — fully deterministic:
injected clock, fixture payloads, in-memory SQLite, and the real shared
Telegram client over a fake transport. No network, no browser.

## Phasing

- **Phase 0 (done):** scrapers + freshness guards + manual odds CLI +
  end-to-end alert pipeline with `--dry-run`.
- **Phase 1 (this code):** automated odds (sofascore aggregated prices,
  clearly labeled), the 30-min autoscan loop, automatic result grading.
  Still pending from the original Phase 1 scope: a scraper for the
  operator's actual book (needs to know which book) — until then the
  price-verification rule above stands. Paper-grade 100+ matches.
- **Phase 2:** calibration reports, fitted weights; go/no-go per the kill
  criteria above.
- **Phase 3 (only if Phase 2 is green):** BetsAPI migration, live-line
  monitoring, in-play alerts after set 1.
