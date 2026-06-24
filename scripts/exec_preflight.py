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


def _discover_ticker() -> str | None:
    """Auto-find a current live Kalshi 15-min crypto market ticker (so you don't have to
    hunt for one). 15-min markets aren't under status=open, so query a forward close-time
    window and take the soonest future market."""
    import time
    try:
        from kalshi_client import KalshiClient
    except Exception:
        from q15_upgrade.kalshi_rest import KalshiClient  # type: ignore
    c = KalshiClient()
    now = int(time.time())
    for series in ("KXBTC15M", "KXETH15M", "KXSOL15M"):
        try:
            mkts = c.discover(series, min_close_ts=now, max_close_ts=now + 6 * 3600) or c.discover(series)
        except Exception:
            continue
        fut = [m for m in (mkts or []) if m.get("ticker")]
        if fut:
            fut.sort(key=lambda m: m.get("close_time", ""))
            return fut[0]["ticker"]
    return None


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
        if not ticker or ticker == "auto":
            ticker = _discover_ticker()
            if not ticker:
                print("\ncould not auto-discover a live market; pass one explicitly:")
                print("  python3 scripts/exec_preflight.py --probe-order <full-current-ticker>")
                return 2
            print(f"auto-discovered live market: {ticker}")
        from q15_upgrade.executor.trading_client import _v2_side_price, _coid_uuid
        v2_side, price_str = _v2_side_price("no", "buy", 1)   # buy NO @ 1c -> unfillable
        print(f"\n[PROBE] V2 order on {ticker}: 1x {v2_side} @ {price_str} (cannot fill), then cancel...")
        body = {"ticker": ticker, "client_order_id": _coid_uuid("exec-preflight-probe"),
                "side": v2_side, "count": "1.00", "price": price_str,
                "time_in_force": "good_till_canceled", "self_trade_prevention_type": "taker_at_cross",
                "post_only": False, "cancel_order_on_pause": False, "reduce_only": False,
                "subaccount": 0, "exchange_index": 0}
        r = cli._request("POST", "/portfolio/events/orders", body)   # explicit test: bypasses dry-run
        if r.get("ok"):
            data = r.get("data") or {}
            oid = data.get("order_id")
            filled = str(data.get("fill_count") or "0")
            print(f"  PLACE OK -> *** ENDPOINT + KEY WORK *** (order_id {oid})")
            if filled not in ("0", "0.0", "0.00", ""):
                print(f"  *** WARNING: order FILLED {filled} contract(s) before cancel — you now")
                print(f"      HOLD a real position on {ticker}. Close it manually on Kalshi! ***")
            else:
                print("  fill_count: 0 (did NOT fill — no position left, as intended)")
            if oid:
                c = cli._request("DELETE", f"/portfolio/events/orders/{oid}")  # force-cancel
                print(f"  cancelled: {c.get('ok')}")
            print("  NOTE: this confirms the V2 schema is accepted. CONFIRM the NO->bid/ask")
            print("        DIRECTION against the docs before going live (see trading_client).")
            return 0
        print("  PLACE FAILED ->", r.get("error"))
        print("  -> read the error: schema field issue, or trade scope / funding.")
        return 1

    print("\nNote: balance + positions confirm AUTH and READ access. A read-only key can")
    print("still pass those, so run  --probe-order <ticker>  for the definitive trade test,")
    print("or just watch the first live fire (placed[LIVE] vs order FAILED in the logs).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
