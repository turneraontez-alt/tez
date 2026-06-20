"""Pattern detection on the underlying 5s candle (with target + flow context).

EVERY result is an ESTIMATE, not a confirmed signal. Detection alone never
implies a reversal; confirmation.py applies the multi-factor gate.

Context (built in analysis.py) keys used here:
  u_latest, u_prev   : latest/previous completed underlying candles (5s)
  y_latest           : latest completed YES-contract candle (5s)
  wick, wick_prev    : wick metrics for u_latest / u_prev (vs target)
  target             : strike price (underlying units) or None
  taker_yes_15s, taker_no_15s
  vol_15s, vol_prev_15s
  ob                 : {orderbook_imbalance, imbalance_flip, ...}
  recent_high, recent_low : recent underlying extremes (excluding current)
"""

# Priority order when multiple patterns fire (reversal-relevant first).
PRIORITY = [
    "liquidity sweep", "double rejection", "failed breakout", "failed breakdown",
    "target reclaim", "target rejection", "bullish rejection", "bearish rejection",
    "absorption", "orderbook imbalance flip", "possible exhaustion",
    "momentum continuation",
]


def _ratio(w, key):
    v = w.get(key) if w else None
    return v if v is not None else 0.0


def detect_patterns(ctx):
    """Return (primary_dict_or_None, all_detected_list)."""
    found = []
    uc = ctx.get("u_latest")
    up = ctx.get("u_prev")
    w = ctx.get("wick") or {}
    wp = ctx.get("wick_prev") or {}
    target = ctx.get("target")

    def add(name, direction, detail):
        found.append({"name": name, "direction": direction, "detail": detail})

    if uc:
        clv = w.get("close_location_value", 0.0)
        lower_r = _ratio(w, "lower_wick_to_body_ratio")
        upper_r = _ratio(w, "upper_wick_to_body_ratio")

        # 1/2 rejections
        if lower_r >= 1.5 and clv >= 0.3:
            add("bullish rejection", "bullish",
                "long lower wick, close in upper range (estimate)")
        if upper_r >= 1.5 and clv <= -0.3:
            add("bearish rejection", "bearish",
                "long upper wick, close in lower range (estimate)")

        if target:
            # 3/4 failed breakout/breakdown (greater_or_equal: above=YES side)
            if uc["high"] > target and uc["close"] < target:
                add("failed breakout", "bearish",
                    "crossed above target then closed back below (estimate)")
            if uc["low"] < target and uc["close"] > target:
                add("failed breakdown", "bullish",
                    "dipped below target then closed back above (estimate)")

            # 5 target reclaim (open one side, close other side)
            if up:
                if up["close"] < target <= uc["close"]:
                    add("target reclaim", "bullish",
                        "reclaimed target to the upside (estimate)")
                elif up["close"] > target >= uc["close"]:
                    add("target reclaim", "bearish",
                        "lost target to the downside (estimate)")

            # 6 target rejection (touched target, pushed away)
            if uc["high"] >= target and uc["close"] < target and clv <= -0.2:
                add("target rejection", "bearish",
                    "rejected at target from above (estimate)")
            if uc["low"] <= target and uc["close"] > target and clv >= 0.2:
                add("target rejection", "bullish",
                    "rejected at target from below (estimate)")

        # 7 momentum continuation
        if up:
            b1 = up["close"] - up["open"]
            b2 = uc["close"] - uc["open"]
            if b1 * b2 > 0 and abs(b2) >= abs(b1) * 0.8:
                vol_up = ctx.get("vol_15s", 0) >= ctx.get("vol_prev_15s", 0)
                if vol_up:
                    add("momentum continuation",
                        "bullish" if b2 > 0 else "bearish",
                        "trend continuation with sustained volume (estimate)")

            # 8 possible exhaustion: big prior move, small opposing current
            if abs(b1) > 0 and abs(b2) <= abs(b1) * 0.4:
                if ctx.get("vol_15s", 0) < ctx.get("vol_prev_15s", 0):
                    add("possible exhaustion",
                        "bearish" if b1 > 0 else "bullish",
                        "momentum stalling after extended move (estimate)")

        # 9 liquidity sweep
        rh = ctx.get("recent_high")
        rl = ctx.get("recent_low")
        if rl is not None and uc["low"] < rl and uc["close"] > rl:
            add("liquidity sweep", "bullish",
                "swept below recent low then reclaimed (estimate)")
        if rh is not None and uc["high"] > rh and uc["close"] < rh:
            add("liquidity sweep", "bearish",
                "swept above recent high then reversed (estimate)")

        # 10 double rejection
        if wp:
            up_r_prev = _ratio(wp, "upper_wick_to_body_ratio")
            lo_r_prev = _ratio(wp, "lower_wick_to_body_ratio")
            if upper_r >= 1.2 and up_r_prev >= 1.2:
                add("double rejection", "bearish",
                    "two consecutive upper-wick rejections (estimate)")
            if lower_r >= 1.2 and lo_r_prev >= 1.2:
                add("double rejection", "bullish",
                    "two consecutive lower-wick rejections (estimate)")

        # 11 absorption: heavy contract volume, tiny underlying move
        y = ctx.get("y_latest")
        body = w.get("body_abs", 0.0)
        rng = w.get("total_range", 0.0)
        if y and y.get("volume", 0) >= ctx.get("vol_prev_15s", 0) * 1.5 and \
                rng > 0 and body <= rng * 0.25:
            tk_yes = ctx.get("taker_yes_15s", 0)
            tk_no = ctx.get("taker_no_15s", 0)
            direction = "bullish" if tk_yes > tk_no else "bearish" if tk_no > tk_yes else "neutral"
            add("absorption", direction,
                "high volume absorbed with little price movement (estimate)")

    # 12 orderbook imbalance flip
    ob = ctx.get("ob") or {}
    if ob.get("imbalance_flip"):
        imb = ob.get("orderbook_imbalance", 0) or 0
        add("orderbook imbalance flip",
            "bullish" if imb > 0 else "bearish",
            "order book pressure flipped sides (estimate)")

    if not found:
        return None, []

    rank = {n: i for i, n in enumerate(PRIORITY)}
    found.sort(key=lambda p: rank.get(p["name"], 99))
    return found[0], found
