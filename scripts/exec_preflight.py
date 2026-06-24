#!/usr/bin/env python3
"""Executor preflight — verify the Kalshi trading key + config WITHOUT risking money.

Run ON THE REPL (where the KALSHI_* secrets live):

    python3 scripts/exec_preflight.py
        Safe read-only checks: does the key load, authenticate, and is the account
        funded? (A read-only key CAN pass these, so they prove auth, not trade scope.)

    python3 scripts/exec_preflight.py --probe-order <TICKER>
        DEFINITIVE trade-permission test: places 1 NO contract at 1c (a price that
        cannot fill) and immediately cancels it. If the place succeeds, the key can
        trade. Max risk if it somehow filled: $0.01, cancelled in the same breath.
"""
from __future__ import annotations

import os
import sys

# Allow running as `python3 scripts/exec_preflight.py` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv: list[str]) -> int:
    from q15_upgrade.executor.config import ExecutorConfig
    from q15_upgrade.executor.trading_client import KalshiTradingClient
    from kalshi_auth import KalshiSigner

    print("=== EXECUTOR PREFLIGHT ===")
    cfg = ExecutorConfig.from_env()
    print("config :", cfg.safety_summary())

    signer = KalshiSigner()
    if not signer.available:
        print("signer : NOT AVAILABLE ->", signer.error)
        print("  FIX: KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY not loading. Re-check the Secrets")
        print("       (PEM header/footer + literal \\n between lines) and restart the Repl.")
        return 1
    print(f"signer : OK (key_id {signer.key_id[:8]}...)")

    cli = KalshiTradingClient(cfg, signer=signer)

    bal = cli.get_balance_cents()
    if bal is None:
        print("balance: FAILED — the authenticated read returned nothing.")
        print("  FIX: key not authorized for portfolio, or demo-vs-production mismatch.")
        return 1
    funded = "" if bal > 0 else "  <-- ACCOUNT NOT FUNDED (add cash to trade)"
    print(f"balance: ${bal/100:,.2f}   (AUTH WORKS, account reachable){funded}")
    print(f"open positions: {len(cli.get_positions())}")
    ready, why = cli.live_ready
    print(f"live-ready for REAL orders: {ready}  ({why})")

    if "--probe-order" in argv:
        idx = argv.index("--probe-order")
        ticker = argv[idx + 1] if idx + 1 < len(argv) else None
        if not ticker:
            print("\n--probe-order needs a TICKER (an active market, e.g. KXBTCD-...).")
            return 2
        print(f"\n[PROBE] placing 1x NO @ 1c on {ticker} (cannot fill), then cancelling...")
        body = {"ticker": ticker, "action": "buy", "side": "no", "count": 1,
                "type": "limit", "no_price": 1, "client_order_id": "exec-preflight-probe"}
        r = cli._request("POST", "/portfolio/orders", body)          # explicit test: bypasses dry-run
        if r.get("ok"):
            oid = ((r.get("data") or {}).get("order") or {}).get("order_id")
            print(f"  PLACE OK -> *** TRADE PERMISSION CONFIRMED *** (order_id {oid})")
            if oid:
                c = cli._request("DELETE", f"/portfolio/orders/{oid}")  # force-cancel regardless of cfg
                print(f"  cancelled: {c.get('ok')}")
            return 0
        print("  PLACE FAILED ->", r.get("error"))
        print("  -> still read-only / no trade scope / unfunded. THIS is the blocker.")
        return 1

    print("\nNote: balance + positions confirm AUTH and READ access. A read-only key can")
    print("still pass those, so run  --probe-order <ticker>  for the definitive trade test,")
    print("or just watch the first live fire (placed[LIVE] vs order FAILED in the logs).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
