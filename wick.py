"""Candle wick metrics. Zero-body safe.

Target semantics (distance / crossed-and-reclaimed) are meaningful for the
UNDERLYING price candle versus the strike (a crypto price level), not for the
0-100 YES contract price, so callers pass target only for underlying candles.
"""


def wick_metrics(candle, target=None, target_type="greater_or_equal"):
    if not candle:
        return None
    o = candle["open"]
    h = candle["high"]
    l = candle["low"]
    c = candle["close"]

    body_signed = c - o
    body = abs(body_signed)
    total_range = h - l
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    # Ratios: zero-body safe (None when body is effectively zero).
    if body > 1e-9:
        upper_ratio = upper_wick / body
        lower_ratio = lower_wick / body
    else:
        upper_ratio = None
        lower_ratio = None

    # Close location value in [-1, 1]: +1 = close at high, -1 = close at low.
    if total_range > 1e-9:
        clv = ((c - l) - (h - c)) / total_range
    else:
        clv = 0.0

    out = {
        "open": o, "high": h, "low": l, "close": c,
        "candle_body": round(body_signed, 6),
        "body_abs": round(body, 6),
        "upper_wick": round(upper_wick, 6),
        "lower_wick": round(lower_wick, 6),
        "total_range": round(total_range, 6),
        "upper_wick_to_body_ratio": round(upper_ratio, 3) if upper_ratio is not None else None,
        "lower_wick_to_body_ratio": round(lower_ratio, 3) if lower_ratio is not None else None,
        "close_location_value": round(clv, 3),
        "zero_body": body <= 1e-9,
        "distance_from_target": None,
        "distance_from_target_pct": None,
        "crossed_target": None,
        "crossed_and_reclaimed_target": None,
    }

    if target is not None and target > 0:
        out["distance_from_target"] = round(c - target, 6)
        out["distance_from_target_pct"] = round((c - target) / target * 100, 4)
        crossed = (l <= target <= h)
        out["crossed_target"] = crossed
        # Reclaim: wicked across the target but closed back on the open's side.
        if crossed:
            opened_above = o >= target
            closed_above = c >= target
            wicked_other_side = (l < target) if opened_above else (h > target)
            out["crossed_and_reclaimed_target"] = bool(
                wicked_other_side and (opened_above == closed_above)
            )
        else:
            out["crossed_and_reclaimed_target"] = False

    return out
