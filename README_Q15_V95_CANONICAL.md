# Q15 V9.5 — Canonical Snapshot + Calibrated Champion/Challenger

## Install

Upload `q15_v9_5_canonical_champion_challenger_installer.py` to the Replit
workspace root and run:

```bash
python q15_v9_5_canonical_champion_challenger_installer.py artifacts/kalshi-monitor
```

Restart or redeploy afterward.

## What V9.5 includes

- One canonical timestamped market snapshot for probability, wicks, momentum,
  context, economics, Telegram, settlement review, and learning.
- Robust threshold/time/volatility structural probability.
- Multi-horizon momentum, executed flow, absorption, order-book pressure,
  wick structure, prior/current 15-minute context, threshold interaction,
  exchange consensus, and derivatives evidence.
- Separate outcome probability, evidence quality, data quality, trade quality,
  conservative probability, and executable value.
- Version-scoped prediction ledger with Brier score, log loss, calibration bands,
  asset/checkpoint/regime reporting, and official settlement reconciliation.
- Frozen production champion plus bounded 15M-only shadow challenger.
- Winner/loser similarity diagnostics after 10 resolved 15M predictions; shadow
  influence begins only after 30. Production influence remains zero.
- Production calibration is opt-in and disabled by default. The calibration
  model is still calculated in shadow for review.
- Persistent Telegram transition state: one initial report, entry transitions,
  entry withdrawals, and retryable failed sends.
- Read-only operation. No order placement, modification, cancellation, automatic
  promotion, or automatic threshold changes.

## Endpoints

- `/api/q15-v9-5/diagnostics`
- `/api/q15-v9-5/predictions`
- `/api/q15-v9-5/market-data`
- `/api/q15-v9-5/calibration`
- `/api/q15-v9-5/learning`
- `/api/q15-v9-5/decision-stats`

## Safety defaults

Keep `Q15_V95_PRODUCTION_CALIBRATION_ENABLED=false` until a version-scoped
forward sample demonstrates improvement. The shadow challenger can learn after
settlement, but it cannot promote itself.
