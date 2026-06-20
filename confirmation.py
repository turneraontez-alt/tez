"""Multi-factor reversal confirmation and transparent 0-100 confidence.

A wick alone never confirms a reversal (spec item 5): we require at least two
independent confirmations. Confidence (item 6) is reported as seven separate
components plus a weighted total, with no claim of certainty.
"""

CONFIRMATION_LABELS = {
    "following_candle": "following candle confirmation",
    "target": "target reclaim or target loss",
    "taker_flow": "taker-side trade imbalance",
    "volume": "volume increase",
    "orderbook_absorption": "orderbook absorption",
    "depth_shift": "bid/ask depth shift",
    "broader_market": "broader BTC/ETH market confirmation",
}

# Weights for the 7 confidence components (sum = 1.0).
WEIGHTS = {
    "wick_quality": 0.20,
    "target_location": 0.15,
    "volume_confirmation": 0.15,
    "taker_flow_confirmation": 0.15,
    "orderbook_confirmation": 0.15,
    "broader_market_confirmation": 0.10,
    "follow_through_confirmation": 0.10,
}


def _aligned(direction, value, pos_is_bull=True):
    if direction == "bullish":
        return value > 0 if pos_is_bull else value < 0
    if direction == "bearish":
        return value < 0 if pos_is_bull else value > 0
    return False


def get_confirmations(ctx, direction):
    """Return list of human-readable confirmation reasons that hold."""
    reasons = []
    if direction not in ("bullish", "bearish"):
        return reasons

    uc = ctx.get("u_latest")
    up = ctx.get("u_prev")
    w = ctx.get("wick") or {}

    # following candle confirmation — a completed candle AFTER the pattern
    # candle that continued in the pattern's direction (computed in analysis.py
    # from pending-pattern state). A wick/pattern candle never confirms itself.
    if ctx.get("follow_through"):
        reasons.append(CONFIRMATION_LABELS["following_candle"])

    # target reclaim / loss
    if w.get("crossed_and_reclaimed_target"):
        reasons.append(CONFIRMATION_LABELS["target"])
    elif ctx.get("target") and uc and up:
        t = ctx["target"]
        if (direction == "bullish" and up["close"] < t <= uc["close"]) or \
           (direction == "bearish" and up["close"] > t >= uc["close"]):
            reasons.append(CONFIRMATION_LABELS["target"])

    # taker-side trade imbalance
    tk_yes = ctx.get("taker_yes_15s", 0)
    tk_no = ctx.get("taker_no_15s", 0)
    tot = tk_yes + tk_no
    if tot > 0:
        skew = (tk_yes - tk_no) / tot
        if _aligned(direction, skew) and abs(skew) >= 0.2:
            reasons.append(CONFIRMATION_LABELS["taker_flow"])

    # volume increase
    if ctx.get("vol_15s", 0) > ctx.get("vol_prev_15s", 0) * 1.2 and ctx.get("vol_prev_15s", 0) > 0:
        reasons.append(CONFIRMATION_LABELS["volume"])

    # orderbook absorption (supportive liquidity added / opposing removed)
    ob = ctx.get("ob") or {}
    if direction == "bullish":
        if ob.get("bid_liquidity_added", 0) > 0 and \
           ob.get("bid_liquidity_added", 0) >= ob.get("ask_liquidity_added", 0) * 1.5:
            reasons.append(CONFIRMATION_LABELS["orderbook_absorption"])
    else:
        if ob.get("ask_liquidity_added", 0) > 0 and \
           ob.get("ask_liquidity_added", 0) >= ob.get("bid_liquidity_added", 0) * 1.5:
            reasons.append(CONFIRMATION_LABELS["orderbook_absorption"])

    # bid/ask depth shift (imbalance or flip aligned)
    imb = ob.get("orderbook_imbalance")
    if imb is not None and _aligned(direction, imb) and abs(imb) >= 0.2:
        reasons.append(CONFIRMATION_LABELS["depth_shift"])
    elif ob.get("imbalance_flip") and imb is not None and _aligned(direction, imb):
        if CONFIRMATION_LABELS["depth_shift"] not in reasons:
            reasons.append(CONFIRMATION_LABELS["depth_shift"])

    # broader BTC/ETH market confirmation
    broader = ctx.get("broader") or {}
    btc = broader.get("BTC", 0)
    eth = broader.get("ETH", 0)
    if _aligned(direction, btc) or _aligned(direction, eth):
        reasons.append(CONFIRMATION_LABELS["broader_market"])

    return reasons


def confidence_components(ctx, direction, reasons):
    """Return dict of 7 components (0-100) plus weighted reversal_confidence."""
    w = ctx.get("wick") or {}
    comp = {k: 0.0 for k in WEIGHTS}

    if direction in ("bullish", "bearish"):
        # wick quality from the supportive wick ratio + close location
        ratio_key = "lower_wick_to_body_ratio" if direction == "bullish" else "upper_wick_to_body_ratio"
        r = w.get(ratio_key) or 0.0
        clv = w.get("close_location_value", 0.0)
        clv_dir = clv if direction == "bullish" else -clv
        comp["wick_quality"] = max(0.0, min(100.0, (min(r, 3.0) / 3.0) * 60 + max(0.0, clv_dir) * 40))

        # target location: closeness to / reclaim of target
        if w.get("crossed_and_reclaimed_target"):
            comp["target_location"] = 90.0
        else:
            dpct = w.get("distance_from_target_pct")
            if dpct is not None:
                comp["target_location"] = max(0.0, min(100.0, 100.0 - min(abs(dpct), 1.0) * 100))

        # volume confirmation
        vp = ctx.get("vol_prev_15s", 0)
        v = ctx.get("vol_15s", 0)
        if vp > 0:
            comp["volume_confirmation"] = max(0.0, min(100.0, (v / vp - 1.0) * 100))
        elif v > 0:
            comp["volume_confirmation"] = 50.0

        # taker flow confirmation
        tk_yes = ctx.get("taker_yes_15s", 0)
        tk_no = ctx.get("taker_no_15s", 0)
        tot = tk_yes + tk_no
        if tot > 0:
            skew = (tk_yes - tk_no) / tot
            skew_dir = skew if direction == "bullish" else -skew
            comp["taker_flow_confirmation"] = max(0.0, min(100.0, max(0.0, skew_dir) * 100))

        # orderbook confirmation
        ob = ctx.get("ob") or {}
        imb = ob.get("orderbook_imbalance")
        if imb is not None:
            imb_dir = imb if direction == "bullish" else -imb
            comp["orderbook_confirmation"] = max(0.0, min(100.0, max(0.0, imb_dir) * 100))

        # broader market confirmation
        broader = ctx.get("broader") or {}
        score = 0.0
        for k in ("BTC", "ETH"):
            val = broader.get(k, 0)
            if _aligned(direction, val):
                score += 50.0
        comp["broader_market_confirmation"] = min(100.0, score)

        # follow-through confirmation
        if CONFIRMATION_LABELS["following_candle"] in reasons:
            comp["follow_through_confirmation"] = 80.0

    total = sum(comp[k] * WEIGHTS[k] for k in WEIGHTS)
    out = {k: round(v, 1) for k, v in comp.items()}
    out["reversal_confidence"] = round(total, 1)
    return out
