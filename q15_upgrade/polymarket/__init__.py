"""Polymarket shadow — a SECOND read-only prediction-market observatory.

This is a sibling of ``q15_upgrade.challenger`` but for a *different* venue and a
*different* contract: Polymarket's 15-minute crypto **Up/Down** markets ("will the
asset close >= its window-open price?"), resolved by Chainlink. Our system trades
Kalshi **strike-threshold** markets ("will the asset be >= $X at close?"), so the
two contracts are NOT the same bet and are never matched/merged.

What this package does (when ``Q15_POLYMARKET_ENABLED=true`` — default OFF):
- At each 15M/10M/7M checkpoint it freezes the SAME decision-time snapshot the
  champion used and emits OUR model's probability for the Polymarket up/down
  contract (the champion's structural diffusion model, re-thresholded at the
  window-open price instead of the fixed strike).
- It records Polymarket's executable order book (best YES/NO ask, spread, depth,
  last trade) and the market-implied up probability alongside.
- After the window closes it grades BOTH our model and the Polymarket market
  against Polymarket's OWN resolution — a valid apples-to-apples model-vs-market
  comparison on one contract (never against the Kalshi strike outcome).

It writes only to its own SQLite ledger, never executes a trade, and never
changes the live champion output. With the flag OFF the package is inert and
production is byte-identical.
"""

from __future__ import annotations
