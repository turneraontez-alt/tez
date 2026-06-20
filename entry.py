"""Entry status with real Kalshi economics.

Combines pattern direction, confirmation count, and confidence with the actual
ask, spread, Kalshi fee, payout, time remaining, and an estimated fair
probability. A valid pattern is NOT tradeable if the contract is already too
expensive relative to fair value.

estimated_fair_probability uses a transparent log-normal model of the
underlying reaching the strike by close; it is an ESTIMATE, not certainty.
"""
import math


def kalshi_fee_cents(price_cents, contracts=1):
    """Kalshi general fee: ceil(0.07 * C * P * (1-P)) in dollars -> cents."""
    if price_cents is None:
        return 0
    p = max(0.0, min(1.0, price_cents / 100.0))
    fee_dollars = 0.07 * contracts * p * (1.0 - p)
    return int(math.ceil(fee_dollars * 100))


def _phi(x):
    """Standard normal CDF via erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def estimated_fair_probability(ctx):
    """Estimate P(YES) using underlying distance to strike, time, volatility.

    Returns (prob, method_str). Falls back to the market mid when inputs are
    insufficient.
    """
    U = ctx.get("underlying")
    K = ctx.get("target")
    secs = ctx.get("seconds_remaining")
    sigma_per_s = ctx.get("underlying_vol_per_s")  # std of 1s log returns
    ttype = ctx.get("target_type", "greater_or_equal")

    if U and K and U > 0 and K > 0 and secs and secs > 0 and sigma_per_s and sigma_per_s > 0:
        total_sigma = sigma_per_s * math.sqrt(secs)
        if total_sigma > 1e-9:
            z = math.log(U / K) / total_sigma
            p_above = _phi(z)
            prob = p_above if ttype == "greater_or_equal" else (1.0 - p_above)
            return max(0.001, min(0.999, prob)), "lognormal(underlying,strike,time,vol)"

    # Fallback: market-implied mid.
    yb = ctx.get("yes_bid")
    ya = ctx.get("yes_ask")
    if yb is not None and ya is not None:
        return max(0.001, min(0.999, (yb + ya) / 200.0)), "market mid (fallback)"
    return None, "unavailable"


def decide_entry(ctx, primary, reasons, confidence):
    """Return a dict with entry_status and supporting economics."""
    fair_prob, fair_method = estimated_fair_probability(ctx)
    direction = primary["direction"] if primary else None
    rev_conf = confidence.get("reversal_confidence", 0.0)
    confirmed = len(reasons) >= 2

    yes_bid = ctx.get("yes_bid")
    yes_ask = ctx.get("yes_ask")
    spread = ctx.get("spread")
    secs = ctx.get("seconds_remaining")

    out = {
        "entry_status": "NO ENTRY",
        "entry_side": None,
        "estimated_fair_probability": round(fair_prob, 4) if fair_prob is not None else None,
        "fair_probability_method": fair_method,
        "entry_ask": None,
        "entry_fee": None,
        "fee_adjusted_break_even": None,
        "estimated_edge": None,
        "payout": 100,
    }

    if direction not in ("bullish", "bearish") or fair_prob is None:
        return out

    # Choose the side the pattern favors.
    if direction == "bullish":
        side = "YES"
        ask = yes_ask
        win_prob = fair_prob
    else:
        side = "NO"
        ask = (100 - yes_bid) if yes_bid is not None else None  # NO ask
        win_prob = 1.0 - fair_prob

    out["entry_side"] = side
    if ask is None:
        return out

    fee = kalshi_fee_cents(ask)
    break_even = ask + fee  # cents the position must be worth to break even
    fair_price = win_prob * 100.0
    edge = round(fair_price - break_even, 2)

    out["entry_ask"] = ask
    out["entry_fee"] = fee
    out["fee_adjusted_break_even"] = break_even
    out["estimated_edge"] = edge

    # --- status logic ---
    # Overpriced / too late: ask already rich vs fair, or barely any time left.
    if ask >= 95 or (secs is not None and secs < 20):
        out["entry_status"] = "TOO LATE / OVERPRICED"
        return out
    if edge <= -3:
        out["entry_status"] = "TOO LATE / OVERPRICED"
        return out

    # Reversal risk: strong opposing taker flow / imbalance against the side.
    tk_yes = ctx.get("taker_yes_15s", 0)
    tk_no = ctx.get("taker_no_15s", 0)
    tot = tk_yes + tk_no
    opposing = False
    if tot > 0:
        skew = (tk_yes - tk_no) / tot  # +ve favors YES
        if direction == "bullish" and skew <= -0.35:
            opposing = True
        if direction == "bearish" and skew >= 0.35:
            opposing = True
    if opposing:
        out["entry_status"] = "REVERSAL RISK"
        return out

    wide = spread is not None and spread > 8
    if confirmed and edge > 0 and rev_conf >= 60 and not wide and (secs is None or secs >= 30):
        out["entry_status"] = "CONFIRMED ENTRY"
    else:
        out["entry_status"] = "WATCH"
    return out
