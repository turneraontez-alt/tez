"""Data-quality warnings (spec item 12).

These surface the real limitations of the public-REST + spot-proxy mode so the
analysis is never presented as more reliable than it is.
"""
import time


def data_quality_warnings(ctx):
    warns = []

    # disconnected stream / REST mode (no authenticated WebSocket yet)
    if not ctx.get("ws_connected", False):
        warns.append("Live WebSocket not connected — using REST polling (add Kalshi API keys to enable)")

    # stale data feed
    age = ctx.get("data_age_s")
    if age is not None and age > 5:
        warns.append(f"Stale data feed — last update {round(age,1)}s ago")

    # missing CF feed (always, since we proxy with spot)
    spot = ctx.get("spot") or {}
    if spot.get("ok"):
        if spot.get("quote") == "USDT":
            warns.append("CF Benchmark feed unavailable — using USDT spot proxy (may differ from USD settlement)")
        else:
            warns.append("CF Benchmark feed unavailable — using spot proxy")
    else:
        warns.append("Underlying spot feed unavailable")

    # insufficient trades
    if ctx.get("trades_15s_count", 0) < 3:
        warns.append("Insufficient trades — low confidence in candles/flow")

    # wide spread
    spread = ctx.get("spread")
    if spread is not None and spread > 8:
        warns.append(f"Wide spread ({spread}c)")

    # thin order book
    depth = (ctx.get("ob") or {}).get("orderbook_imbalance")
    yes_depth = ctx.get("yes_bid_qty", 0) or 0
    no_depth = ctx.get("no_bid_qty", 0) or 0
    if (yes_depth + no_depth) < 20:
        warns.append("Thin order book")

    # delayed candle (no fresh trade in the current/last bucket)
    last_trade_age = ctx.get("last_trade_age_s")
    if last_trade_age is not None and last_trade_age > 10:
        warns.append(f"Delayed candle — no trade in {round(last_trade_age,1)}s")

    # spot/CF disagreement — cannot verify without CF; note proxy uncertainty
    # (covered by the CF-unavailable warning above).

    return warns
