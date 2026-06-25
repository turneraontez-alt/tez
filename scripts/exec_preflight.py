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

    python3 scripts/exec_preflight.py --verify-direction <TICKER>
        DEFINITIVE direction test (costs ~the bid/ask spread, a few cents): buys 1 NO
        through our real mapping (marketable so it fills), POLLS the position back to
        confirm it's LONG NO (not accidentally YES), then CLOSES exactly what it
        opened (reduce-only) and verifies you end flat. A try/finally guarantees no
        resting order is ever left behind. Run this ONCE before trading real size —
        a wrong bid/ask mapping = the exact opposite position.
        Use 'auto' for the ticker to auto-discover a live 15-min market.

    python3 scripts/exec_preflight.py --flatten <TICKER>
        Safety/cleanup: close ANY open position on the ticker back to flat (reduce-only)
        and cancel any resting orders. Run this if a verify/probe run was inconclusive
        and you want to be certain nothing is left open. ('auto' picks a live market.)

    python3 scripts/exec_preflight.py --orders
        READ-ONLY diagnostic: list recent orders on the account with a size histogram, so
        you can tell whether something placed many tiny (1-contract ~$1) orders, or one
        larger order that simply FILLED in many small pieces (which looks the same in a
        fills/activity view but is a single bet).

    python3 scripts/exec_preflight.py --fills
        READ-ONLY: answer "how many orders MISSED (placed but not filled)?" — classifies the
        LIVE account's recent orders into filled / partial / missed, and also prints the local
        executor_orders store summary (immediate fills, correlated to interval/asset) if present.

The no-flag run now also LISTS every open position (ticker, size, long NO/YES) so you
can eyeball whether anything is open before/after a test.
"""
from __future__ import annotations

import os
import sys
import uuid

# Allow running as `python3 scripts/exec_preflight.py` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _discover_ticker(min_runway_s: int = 0) -> str | None:
    """Auto-find a current live Kalshi 15-min crypto market ticker (so you don't have to
    hunt for one). 15-min markets aren't under status=open, so query a forward close-time
    window and take the soonest future market.

    ``min_runway_s`` skips markets about to settle: the direction test must hold a 1-lot
    long enough to READ the position back, so it needs a window with runway (a near-close
    market fills at ~99c and resolves before /portfolio/positions ever shows it)."""
    import time
    try:
        from kalshi_client import KalshiClient
    except Exception:
        from q15_upgrade.kalshi_rest import KalshiClient  # type: ignore
    c = KalshiClient()
    now = int(time.time())
    cutoff = now + int(min_runway_s)
    for series in ("KXBTC15M", "KXETH15M", "KXSOL15M"):
        try:
            mkts = c.discover(series, min_close_ts=cutoff, max_close_ts=now + 6 * 3600) or c.discover(series)
        except Exception:
            continue
        fut = [m for m in (mkts or []) if m.get("ticker")]
        if fut:
            fut.sort(key=lambda m: m.get("close_time", ""))
            return fut[0]["ticker"]
    return None


def _fresh_coid() -> str:
    """A NEW random UUID per call. The production executor uses DETERMINISTic ids
    (idempotent retries), but a repeatable manual test must use a fresh id each run —
    Kalshi remembers every client_order_id account-wide and rejects a reuse with 409."""
    return str(uuid.uuid4())


def _order(cli, ticker, side, price, coid, count=1, reduce_only=False):
    body = {"ticker": ticker, "client_order_id": coid, "side": side, "count": f"{int(count)}.00",
            "price": price, "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross", "post_only": False,
            "cancel_order_on_pause": False, "reduce_only": reduce_only,
            "subaccount": 0, "exchange_index": 0}
    return cli._request("POST", "/portfolio/events/orders", body)


def _pos(cli, ticker):
    for p in cli.get_positions():
        if p.get("ticker") == ticker:
            return p
    return None


def _pos_int(cli, ticker):
    return int((_pos(cli, ticker) or {}).get("position") or 0)


def _poll_pos(cli, ticker, differ_from, tries=10, delay=0.8):
    """Read the position repeatedly until it differs from ``differ_from`` (Kalshi's
    /portfolio/positions lags a fill by a beat) or we run out of tries."""
    import time
    cur = differ_from
    for _ in range(tries):
        cur = _pos_int(cli, ticker)
        if cur != differ_from:
            return cur
        time.sleep(delay)
    return cur


def _cancel(cli, order_id):
    """Cancel an order, tolerant to which V2 path the account uses. A 404 means it is
    already gone (filled/terminal), which is success for our purposes."""
    for path in (f"/portfolio/orders/{order_id}", f"/portfolio/events/orders/{order_id}"):
        r = cli._request("DELETE", path)
        if r.get("ok"):
            return True
        if r.get("status") == 404:
            return True
    return False


def _cancel_all_resting(cli, ticker):
    """Belt-and-braces: cancel every still-resting order on ``ticker`` so the test can
    NEVER leave a live order on the book. Returns how many it cleared."""
    r = cli._request("GET", f"/portfolio/orders?ticker={ticker}&status=resting")
    orders = (r.get("data") or {}).get("orders") or [] if r.get("ok") else []
    n = 0
    for o in orders:
        oid = o.get("order_id")
        if oid and _cancel(cli, oid):
            n += 1
    if n:
        print(f"  cleared {n} resting order(s) on {ticker}.")
    return n


def _flatten(cli, ticker) -> int:
    """Close any open position on ``ticker`` back to zero (reduce-only, marketable), and
    clear any resting orders. Use this to clean up after an inconclusive verify run."""
    _cancel_all_resting(cli, ticker)
    pos = _pos_int(cli, ticker)
    print(f"  position on {ticker}: {pos}")
    if pos == 0:
        print("  already flat — nothing to close."); return 0
    # negative = long NO -> buy YES (bid) to close; positive = long YES -> sell YES (ask).
    side, price = ("bid", "0.9900") if pos < 0 else ("ask", "0.0100")
    print(f"  CLOSE: side={side} price={price} x{abs(pos)} reduce_only ...")
    rc = _order(cli, ticker, side, price, _fresh_coid(), count=abs(pos), reduce_only=True)
    if not rc.get("ok"):
        print("  CLOSE FAILED ->", rc.get("error")); return 1
    final = _poll_pos(cli, ticker, differ_from=pos)
    _cancel_all_resting(cli, ticker)
    final = _pos_int(cli, ticker)
    print(f"  final position: {final}")
    if final != 0:
        print(f"  🚨 WARNING: still NOT flat ({final}) — CLOSE IT MANUALLY on Kalshi NOW.")
        return 1
    print("  ✅ flat."); return 0


def _verify_direction(cli, ticker) -> int:
    """Definitive direction test (~the bid/ask spread). Buys 1 NO via our mapping at a
    maximally-marketable price so it crosses, POLLS the position back (the positions feed
    lags a fill), reports CORRECT vs BACKWARDS, then CLOSES exactly what it opened and
    re-confirms flat. Kalshi convention: positive = long YES, negative = long NO. A
    try/finally guarantees no resting order is ever left on the book."""
    from q15_upgrade.executor.trading_client import _v2_side_price
    base = _pos_int(cli, ticker)
    print(f"  baseline position on {ticker}: {base}")

    try:
        # buy NO @ 99c == sell YES @ 1c -> crosses ANY yes bid >= 1c, so it fills if the book is 2-sided.
        v2_side, price = _v2_side_price("no", "buy", 99)
        print(f"  OPEN (our 'buy NO'): side={v2_side} price={price} x1 ...")
        r = _order(cli, ticker, v2_side, price, _fresh_coid())
        if not r.get("ok"):
            print("  OPEN FAILED ->", r.get("error")); return 1
        od = r.get("data") or {}
        # nested or flat: V2 may return {"order": {...}} — handle both.
        inner = od.get("order") if isinstance(od.get("order"), dict) else od
        print(f"  order ack: {inner}")
        try:
            filled = float(inner.get("fill_count") or 0)
        except (TypeError, ValueError):
            filled = 0.0
        oid = inner.get("order_id")
        if oid:
            _cancel(cli, oid)   # kill any unfilled remainder immediately

        after = _poll_pos(cli, ticker, differ_from=base)   # waits out the positions-feed lag
        delta = after - base
        print(f"  position now: {after}  (delta {delta:+d})")
        if delta == 0:
            if filled > 0:
                print(f"  order FILLED {filled} @ {inner.get('average_fill_price')} but the position")
                print("  reads 0 -> the market likely SETTLED or the feed lags. Raw position entry:")
                for p in cli.get_positions():
                    if p.get("ticker") == ticker:
                        print("   ", p)
                print("  Re-run on a window with more runway (auto-discovery now asks for >=6 min).")
            else:
                print("  VERDICT: inconclusive — nothing crossed (thin/one-sided book).")
            return 2
        if delta < 0:
            print("  VERDICT: ✅ CORRECT — our 'buy NO' produced a NO position (delta negative).")
            ok = True
        else:
            print("  VERDICT: 🚨 BACKWARDS — our 'buy NO' produced a YES position. FLIP the NO")
            print("           branch in _v2_side_price BEFORE any real-size trading.")
            ok = False

        # Close EXACTLY the net change: long NO (delta<0) -> buy YES; long YES (delta>0) -> sell YES.
        side, cprice = ("bid", "0.9900") if delta < 0 else ("ask", "0.0100")
        print(f"  CLOSE (flatten {abs(delta)}): side={side} price={cprice} reduce_only ...")
        rc = _order(cli, ticker, side, cprice, _fresh_coid(),
                    count=abs(delta), reduce_only=True)
        rcd = rc.get("data") or {}
        rcid = (rcd.get("order") or rcd).get("order_id") if isinstance(rcd, dict) else None
        if rcid:
            _cancel(cli, rcid)
        final = _poll_pos(cli, ticker, differ_from=after)
        print(f"  final position: {final}  (baseline {base})")
        if final != base:
            print(f"  🚨 WARNING: NOT flat ({final} vs {base}) — run --flatten {ticker} or close on Kalshi NOW.")
        return 0 if ok else 1
    finally:
        _cancel_all_resting(cli, ticker)   # never leave a live order behind


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
    held = [p for p in cli.get_positions() if int(p.get("position") or 0) != 0]
    print(f"open positions: {len(held)}")
    for p in held:
        sign = "long NO" if int(p.get("position") or 0) < 0 else "long YES"
        print(f"  - {p.get('ticker')}: {p.get('position')} ({sign})")
    ready, why = cli.live_ready
    print(f"live-ready for REAL orders: {ready}  ({why})")

    if "--orders" in argv:
        # READ-ONLY diagnostic: list recent orders on the account so we can tell whether the
        # executor placed many tiny orders, or one big order that just FILLED in many pieces.
        from collections import Counter
        recent = cli._request("GET", "/portfolio/orders")
        orders = (recent.get("data") or {}).get("orders") or []
        if not recent.get("ok"):
            print("\norders read FAILED ->", recent.get("error")); return 1
        print(f"\n[ORDERS] {len(orders)} order(s) returned by Kalshi (most recent first):")
        # size histogram: how many orders were 1-contract ('$1 bets') vs larger.
        def _cnt(o):
            for k in ("initial_count", "count", "place_count"):
                if o.get(k) not in (None, ""):
                    try: return int(float(o[k]))
                    except (TypeError, ValueError): pass
            return None
        hist = Counter(_cnt(o) for o in orders)
        ones = sum(n for c, n in hist.items() if c == 1)
        print(f"  size histogram (contracts -> #orders): {dict(sorted((k,v) for k,v in hist.items() if k is not None))}")
        print(f"  -> {ones} order(s) were 1 contract (~$1 bets); {len(orders)-ones} were larger")
        statuses = Counter(o.get("status") for o in orders)
        print(f"  status: {dict(statuses)}")
        print("  most recent 25:")
        for o in orders[:25]:
            px = o.get("yes_price") if o.get("yes_price") is not None else o.get("price")
            print(f"    {o.get('created_time','?')} {o.get('ticker')} side={o.get('side')} "
                  f"action={o.get('action')} count={_cnt(o)} price={px} "
                  f"status={o.get('status')} filled={o.get('fill_count')} coid={str(o.get('client_order_id'))[:18]}")
        return 0

    if "--fills" in argv:
        # READ-ONLY: answer "how many orders MISSED (placed but not filled)?" two ways —
        # (1) the LIVE account's order history (source of truth for FINAL fills), and
        # (2) our local executor_orders store (immediate fills, correlated to interval/asset).
        from collections import Counter
        recent = cli._request("GET", "/portfolio/orders")
        orders = (recent.get("data") or {}).get("orders") or []
        if not recent.get("ok"):
            print("\norders read FAILED ->", recent.get("error"))
        else:
            def _cnt(o):
                for k in ("initial_count", "count", "place_count"):
                    if o.get(k) not in (None, ""):
                        try:
                            return int(float(o[k]))
                        except (TypeError, ValueError):
                            pass
                return None

            def _filled(o):
                try:
                    return int(float(o.get("fill_count") or 0))
                except (TypeError, ValueError):
                    return 0
            filled = partial = missed = unknown = 0
            for o in orders:
                c, f = _cnt(o), _filled(o)
                if c is None:
                    unknown += 1
                elif f >= c and c > 0:
                    filled += 1
                elif f > 0:
                    partial += 1
                else:
                    missed += 1
            n = len(orders)
            print(f"\n[FILLS] LIVE ACCOUNT — {n} recent order(s):")
            print(f"  filled (fully):     {filled}")
            print(f"  partial:            {partial}")
            print(f"  MISSED (0 filled):  {missed}")
            if unknown:
                print(f"  unknown size:       {unknown}")
            if n:
                print(f"  fill rate (fully filled / total): {filled}/{n} = {100*filled/n:.0f}%")
            print(f"  status breakdown: {dict(Counter(o.get('status') for o in orders))}")
        # local store (if the executor has been recording)
        try:
            from q15_upgrade.executor.store import ExecutorStore
            if os.path.exists(cfg.orders_db_path):
                store = ExecutorStore(cfg.orders_db_path)
                summ = store.fill_summary()
                print(f"\n[FILLS] LOCAL STORE ({cfg.orders_db_path}):")
                print(f"  total recorded: {summ['total']}  (live {summ.get('live_orders')})")
                print(f"  filled: {summ['filled']}  partial: {summ['partial']}  "
                      f"MISSED-at-placement: {summ['missed']}")
                fr = summ.get("fill_rate")
                print(f"  immediate fill rate: {f'{100*fr:.0f}%' if fr is not None else 'n/a'}")
                print(f"  by status: { {k: v['n'] for k, v in summ['by_status'].items()} }")
                # ENTRY vs EXIT split — the defensive-sell path is action='exit'. The aggregate
                # above lumps both, so it can't answer "did a defensive SELL fire and fill?".
                for act, lab in (("entry", "ENTRIES (buys) "), ("exit", "DEFENSIVE EXITS (sells)")):
                    a = store.fill_summary(action=act)
                    afr = a.get("fill_rate")
                    rate = f", fill rate {100*afr:.0f}%" if afr is not None else ""
                    print(f"  {lab}: recorded {a['total']} (live {a['live_orders']}) -> "
                          f"filled {a['filled']}, partial {a['partial']}, missed {a['missed']}{rate}")
                if store.fill_summary(action="exit")["total"] == 0:
                    print("  -> NO exit-sell orders recorded yet: either no flip-warning has fired on a")
                    print("     position the EXECUTOR held since the store began recording, or the real")
                    print("     exits predate the store (the store is newer than the live history).")
                print("  NOTE: 'RESTED' here = unfilled AT PLACEMENT; a resting limit can still")
                print("  fill later in the window — the LIVE ACCOUNT block above is the final word.")
            else:
                print(f"\n[FILLS] LOCAL STORE: none yet at {cfg.orders_db_path} (no orders recorded).")
        except Exception as e:  # noqa: BLE001
            print(f"\n[FILLS] local store read skipped: {e}")
        return 0

    if "--flatten" in argv:
        idx = argv.index("--flatten")
        ticker = argv[idx + 1] if idx + 1 < len(argv) else None
        if not ticker or ticker == "auto":
            ticker = _discover_ticker()
        if not ticker:
            print("\npass the ticker to flatten: --flatten <ticker>  (or a live one via 'auto')")
            return 2
        print(f"\n[FLATTEN] closing any open position + resting orders on {ticker} ...")
        return _flatten(cli, ticker)

    if "--verify-direction" in argv:
        idx = argv.index("--verify-direction")
        ticker = argv[idx + 1] if idx + 1 < len(argv) else None
        if not ticker or ticker == "auto":
            # need RUNWAY (>=6 min) so the 1-lot doesn't settle before we can read it back.
            ticker = _discover_ticker(min_runway_s=360)
            if not ticker:
                print("\ncould not auto-discover a live market with runway; pass one explicitly:")
                print("  python3 scripts/exec_preflight.py --verify-direction <full-current-ticker>")
                return 2
            print(f"auto-discovered live market (>=6min runway): {ticker}")
        print(f"\n[VERIFY-DIRECTION] real ~few-cent test on {ticker}: buy 1 NO, read the")
        print("position back, then CLOSE exactly what opened (reduce-only). Confirms the")
        print("buy-NO -> bid/ask mapping puts you LONG NO before any real-size trading.\n")
        return _verify_direction(cli, ticker)

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
        from q15_upgrade.executor.trading_client import _v2_side_price
        v2_side, price_str = _v2_side_price("no", "buy", 1)   # buy NO @ 1c -> unfillable
        print(f"\n[PROBE] V2 order on {ticker}: 1x {v2_side} @ {price_str} (cannot fill), then cancel...")
        body = {"ticker": ticker, "client_order_id": _fresh_coid(),
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
