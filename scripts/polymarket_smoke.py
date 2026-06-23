#!/usr/bin/env python3
"""Read-only Polymarket 15-minute up/down smoke test.

Run this from a host that can reach the Polymarket public API (e.g. the Repl) to
CONFIRM the shadow is actually finding and parsing live 15-minute crypto markets.
It never trades — it only GETs public market data and prints what it found, so it
is the fastest way to close the last "are the Gamma field names right?" gap.

    python3 scripts/polymarket_smoke.py
    python3 scripts/polymarket_smoke.py BTC ETH SOL XRP   # explicit assets

For each asset it discovers the current 15m window's up/down market and prints the
slug, condition id, token ids, the window-open "price to beat", and the live order
book (best YES/NO, spread, depth, implied P(up)); it also probes a recently-closed
window to exercise the resolution path. All-None / NO MATCHING MARKET points at a
slug or Gamma-field mismatch to fix in q15_upgrade/polymarket/client.py.

(From the dev sandbox the Polymarket API is network-blocked, so every market will
read NO MATCHING MARKET here — that is expected; run it on the Repl for a real check.)
"""
from __future__ import annotations

import os
import sys
import time

# Running a script file puts scripts/ on sys.path, not the repo root — add the
# repo root before importing the package (mirrors the other scripts/ helpers).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from q15_upgrade.polymarket.client import PolymarketClient
from q15_upgrade.polymarket.config import PolymarketConfig
from q15_upgrade.polymarket.ledger import WINDOW_SECONDS

_W = int(WINDOW_SECONDS)


def _window_close(now: float, back: int = 0) -> float:
    """Close epoch of the 15m window ``back`` windows before the current one."""
    cur_open = (int(now) // _W) * _W
    return float(cur_open + _W - back * _W)


def main(argv: list[str]) -> int:
    cfg = PolymarketConfig.from_env()
    assets = tuple(a.upper() for a in argv[1:]) or cfg.assets
    client = PolymarketClient(clob_base=cfg.clob_base, gamma_base=cfg.gamma_base,
                              timeout=cfg.http_timeout)
    now = time.time()
    print(f"Polymarket smoke · gamma={cfg.gamma_base} clob={cfg.clob_base}")
    print(f"assets={','.join(assets)} window={_W}s now={int(now)}\n")

    found = 0
    for asset in assets:
        close = _window_close(now, back=0)
        slug = f"{asset.lower()}-updown-15m-{int(close - WINDOW_SECONDS)}"
        print(f"=== {asset}  (current window closes {int(close)}; slug {slug}) ===")
        market = client.find_updown_market(asset, close)
        if market is None:
            print("  discovery: NO MATCHING MARKET")
        else:
            found += 1
            book = client.book_metrics(market)
            print(f"  condition_id={market.condition_id}")
            print(f"  yes_token={market.yes_token_id}  no_token={market.no_token_id}")
            print(f"  open_price(price-to-beat)={market.open_price}")
            print(f"  book: yes_bid={book.yes_bid} yes_ask={book.yes_ask} "
                  f"no_bid={book.no_bid} no_ask={book.no_ask}")
            print(f"        spread={book.spread} depth={book.depth} "
                  f"last={book.last_trade} P(up)={book.market_prob_up}")
        past_close = _window_close(now, back=3)
        past = client.find_updown_market(asset, past_close)
        if past is not None:
            print(f"  resolved({int(past_close)}): {client.resolved_result(past)}")
        print()

    if not found:
        print("No live markets discovered. If Polymarket DOES list 15m up/down now, "
              "the slug or Gamma fields need adjusting in "
              "q15_upgrade/polymarket/client.py (or this host can't reach the API).")
        return 1
    print(f"{found}/{len(assets)} markets discovered + parsed. Confirm the fields "
          "above are populated (not all None) to validate the Gamma/CLOB field names.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
