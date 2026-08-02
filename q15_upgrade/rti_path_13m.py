"""Pure feature construction for the RTI-path 13M paper cohorts."""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence


RTI_POINT_IN_TIME_RISK_POLICY_VERSION = (
    "rti-point-in-time-risk-taxonomy-20260720-v1"
)
RTI_DISTANCE_TO_REMAINING_VOLATILITY_CAP = 10.0


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _side(price: float | None, strike: float | None) -> str | None:
    if price is None or strike is None:
        return None
    return "YES" if price >= strike else "NO"


def build_rti_path_features(
    path_context: Mapping[str, Any],
    *,
    strike: float | None,
) -> dict[str, Any]:
    """Summarize a 61-second RTI path without manufacturing missing values."""
    raw_rows = path_context.get("rows")
    rows: Sequence[Mapping[str, Any]] = (
        raw_rows
        if isinstance(raw_rows, Sequence) and not isinstance(raw_rows, (str, bytes))
        else ()
    )
    prices = [
        price
        for row in rows
        if isinstance(row, Mapping)
        for price in [_num(row.get("index_px"))]
        if price is not None
    ]
    strike_value = _num(strike)
    start_px = prices[0] if prices else None
    end_px = prices[-1] if prices else None
    side_14m = _side(start_px, strike_value)
    side_13m = _side(end_px, strike_value)
    path_sides = [_side(price, strike_value) for price in prices]
    persistence = (
        sum(1 for side in path_sides if side == side_13m) / len(path_sides)
        if path_sides and side_13m in {"YES", "NO"}
        else None
    )
    raw_move = None if start_px is None or end_px is None else end_px - start_px
    side_move = (
        None
        if raw_move is None or side_13m not in {"YES", "NO"}
        else raw_move * (1.0 if side_13m == "YES" else -1.0)
    )
    side_move_bps = (
        None
        if side_move is None or start_px is None or start_px == 0.0
        else side_move / start_px * 10_000.0
    )
    sign = 1.0 if side_13m == "YES" else -1.0 if side_13m == "NO" else None
    signed_distance_bps = (
        None
        if sign is None or end_px is None or strike_value is None or strike_value == 0.0
        else sign * (end_px / strike_value - 1.0) * 10_000.0
    )
    absolute_distance_bps = (
        None if signed_distance_bps is None else abs(signed_distance_bps)
    )
    path_range_bps = (
        None
        if not prices or end_px is None or end_px == 0.0
        else (max(prices) - min(prices)) / end_px * 10_000.0
    )
    log_returns_bps = [
        10_000.0 * math.log(right / left)
        for left, right in zip(prices, prices[1:])
        if left > 0.0 and right > 0.0
    ]
    realized_volatility_bps = (
        math.sqrt(sum(value * value for value in log_returns_bps))
        if log_returns_bps
        else None
    )
    total_variation = sum(
        abs(right - left) for left, right in zip(prices, prices[1:])
    )
    trend_efficiency = (
        None
        if start_px is None or end_px is None
        else (1.0 if total_variation == 0.0 else abs(end_px - start_px) / total_variation)
    )
    midpoint = len(prices) // 2
    midpoint_px = prices[midpoint] if prices else None
    first_half_side_move_bps = (
        None
        if sign is None or start_px is None or midpoint_px is None or start_px == 0.0
        else sign * (midpoint_px / start_px - 1.0) * 10_000.0
    )
    second_half_side_move_bps = (
        None
        if sign is None or midpoint_px is None or end_px is None or midpoint_px == 0.0
        else sign * (end_px / midpoint_px - 1.0) * 10_000.0
    )
    acceleration_bps = (
        None
        if first_half_side_move_bps is None or second_half_side_move_bps is None
        else second_half_side_move_bps - first_half_side_move_bps
    )
    crossings = sum(
        left != right
        for left, right in zip(path_sides, path_sides[1:])
        if left is not None and right is not None
    )
    last_crossing_index = next(
        (
            index
            for index in range(len(path_sides) - 1, 0, -1)
            if path_sides[index] is not None
            and path_sides[index - 1] is not None
            and path_sides[index] != path_sides[index - 1]
        ),
        None,
    )
    seconds_since_last_crossing = (
        None
        if last_crossing_index is None
        else float(len(path_sides) - 1 - last_crossing_index)
    )
    remaining_volatility_bps = (
        None
        if realized_volatility_bps is None
        else realized_volatility_bps * math.sqrt(780.0 / 60.0)
    )
    zero_remaining_volatility_limit = bool(
        absolute_distance_bps is not None
        and remaining_volatility_bps == 0.0
    )
    distance_to_remaining_volatility = (
        None
        if absolute_distance_bps is None or remaining_volatility_bps is None
        else (
            0.0
            if remaining_volatility_bps == 0.0 and absolute_distance_bps == 0.0
            else RTI_DISTANCE_TO_REMAINING_VOLATILITY_CAP
            if remaining_volatility_bps == 0.0
            else min(
                RTI_DISTANCE_TO_REMAINING_VOLATILITY_CAP,
                absolute_distance_bps / remaining_volatility_bps,
            )
        )
    )
    return {
        "rti_path_status": path_context.get("status"),
        "rti_path_missing_reason": path_context.get("missing_reason"),
        "rti_index_id": path_context.get("index_id"),
        "rti_path_expected_count": path_context.get("expected_count"),
        "rti_path_count": path_context.get("count"),
        "rti_path_complete": bool(path_context.get("complete")),
        "rti_path_missing_seconds": path_context.get("missing_seconds"),
        "rti_path_max_receive_age_s": path_context.get("max_receive_age_s"),
        "rti_decision_age_s": path_context.get("decision_age_s"),
        "rti_strike": strike_value,
        "rti_path_start_px": start_px,
        "rti_path_end_px": end_px,
        "rti_14m_side": side_14m,
        "rti_side": side_13m,
        "rti_same_side_14m": (
            side_14m == side_13m if side_14m and side_13m else None
        ),
        "rti_path_persistence": persistence,
        "rti_move_raw": raw_move,
        "rti_side_move": side_move,
        "rti_side_move_bps": side_move_bps,
        "rti_signed_distance_bps": signed_distance_bps,
        "rti_absolute_distance_bps": absolute_distance_bps,
        "rti_path_range_bps": path_range_bps,
        "rti_path_realized_volatility_bps": realized_volatility_bps,
        "rti_path_trend_efficiency": trend_efficiency,
        "rti_path_first_half_side_move_bps": first_half_side_move_bps,
        "rti_path_second_half_side_move_bps": second_half_side_move_bps,
        "rti_path_acceleration_bps": acceleration_bps,
        "rti_path_strike_crossings": crossings,
        "rti_path_seconds_since_last_crossing": seconds_since_last_crossing,
        "rti_expected_remaining_volatility_bps": remaining_volatility_bps,
        "rti_distance_to_remaining_volatility": distance_to_remaining_volatility,
        "rti_distance_to_remaining_volatility_cap": (
            RTI_DISTANCE_TO_REMAINING_VOLATILITY_CAP
        ),
        "rti_zero_remaining_volatility_limit_applied": (
            zero_remaining_volatility_limit
        ),
    }


def classify_rti_point_in_time_risk(
    row: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze interpretable risk labels using decision-time evidence only.

    These labels are diagnostics, not entry gates.  The thresholds match the
    frozen audit taxonomy and claim no historical promotion credit.  Returning
    ``unknown`` on missing inputs keeps data-quality failures visible instead of
    silently treating them as low risk.
    """
    crossings = _num(row.get("rti_path_strike_crossings"))
    acceleration = _num(row.get("rti_path_acceleration_bps"))
    seconds_since_cross = _num(row.get("rti_path_seconds_since_last_crossing"))
    persistence = _num(row.get("rti_path_persistence"))
    distance = _num(row.get("rti_signed_distance_bps"))
    normalized = _num(row.get("rti_distance_to_remaining_volatility"))
    ask = _num(row.get("entry_ask_cents"))

    reversal_reasons: list[str] = []
    if crossings is None or acceleration is None:
        reversal_risk = "unknown"
        reversal_reasons.append("REVERSAL_INPUT_MISSING")
    elif crossings >= 3.0:
        reversal_risk = "high"
        reversal_reasons.append("STRIKE_CROSSINGS_3_PLUS")
    elif (
        acceleration < 0.0
        and crossings >= 1.0
        and seconds_since_cross is not None
        and seconds_since_cross < 30.0
    ):
        reversal_risk = "high"
        reversal_reasons.extend(("PATH_DECELERATING", "RECENT_STRIKE_CROSSING"))
    elif crossings >= 1.0 or acceleration < 0.0:
        reversal_risk = "medium"
        if crossings >= 1.0:
            reversal_reasons.append("STRIKE_CROSSING_PRESENT")
        if acceleration < 0.0:
            reversal_reasons.append("PATH_DECELERATING")
    else:
        reversal_risk = "low"
        reversal_reasons.append("NO_CROSSING_OR_DECELERATION")

    settlement_reasons: list[str] = []
    if distance is None or normalized is None:
        settlement_risk = "unknown"
        settlement_reasons.append("SETTLEMENT_MARGIN_INPUT_MISSING")
    elif distance < 0.75 or normalized < 0.05:
        settlement_risk = "high"
        if distance < 0.75:
            settlement_reasons.append("DISTANCE_UNDER_0_75_BPS")
        if normalized < 0.05:
            settlement_reasons.append("VOL_NORMALIZED_MARGIN_UNDER_0_05")
    elif distance < 1.5 or normalized < 0.15:
        settlement_risk = "medium"
        if distance < 1.5:
            settlement_reasons.append("DISTANCE_UNDER_1_5_BPS")
        if normalized < 0.15:
            settlement_reasons.append("VOL_NORMALIZED_MARGIN_UNDER_0_15")
    else:
        settlement_risk = "low"
        settlement_reasons.append("MARGIN_ABOVE_FROZEN_RISK_BANDS")

    if crossings is None or persistence is None:
        regime = "unknown"
    elif crossings >= 3.0:
        regime = "choppy"
    elif persistence >= 0.95 and crossings <= 1.0:
        regime = "persistent"
    else:
        regime = "mixed"

    market_agreement = (
        "unknown"
        if ask is None
        else "disagrees_under_50"
        if ask < 50.0
        else "weak_50_to_55"
        if ask < 55.0
        else "confirms_55_plus"
    )
    return {
        "rti_risk_policy_version": RTI_POINT_IN_TIME_RISK_POLICY_VERSION,
        "rti_reversal_risk_class": reversal_risk,
        "rti_reversal_risk_reason_codes": reversal_reasons,
        "rti_settlement_average_risk_class": settlement_risk,
        "rti_settlement_average_risk_reason_codes": settlement_reasons,
        "rti_path_regime_class": regime,
        "rti_market_agreement_class": market_agreement,
        "rti_risk_notification_eligible": False,
        "rti_risk_historical_credit_allowed": False,
    }


def quote_for_rti_side(
    analysis: Mapping[str, Any],
    *,
    rti_side: str | None,
) -> dict[str, Any]:
    """Convert the champion's selected-side quote to the RTI-selected side."""
    quote = analysis.get("quote")
    quote = quote if isinstance(quote, Mapping) else {}
    selected_side = str(analysis.get("prediction_side") or "").upper()
    if selected_side not in {"YES", "NO"}:
        selected_side = ""
    bid = _num(quote.get("bid_cents"))
    ask = _num(quote.get("ask_cents"))
    inverted = bool(rti_side in {"YES", "NO"} and selected_side and rti_side != selected_side)
    if inverted:
        rti_bid = None if ask is None else 100.0 - ask
        rti_ask = None if bid is None else 100.0 - bid
    elif rti_side in {"YES", "NO"} and selected_side == rti_side:
        rti_bid, rti_ask = bid, ask
    else:
        rti_bid = rti_ask = None
    spread = (
        rti_ask - rti_bid
        if rti_bid is not None and rti_ask is not None
        else _num(quote.get("spread_cents"))
    )
    depth_key = f"{str(rti_side or '').lower()}_ask_depth_contracts"
    depth = _num(quote.get(depth_key)) if rti_side in {"YES", "NO"} else None
    if depth is None and not inverted and selected_side == rti_side:
        depth = _num(quote.get("ask_depth"))
        if depth is None:
            depth = _num(quote.get("depth_contracts"))
    return {
        "rti_quote_source_side": selected_side or None,
        "rti_quote_inverted": inverted,
        "entry_ask_cents": rti_ask,
        "yes_bid_cents": rti_bid,
        "yes_ask_cents": rti_ask,
        "spread_cents": spread,
        "depth_contracts": depth,
        "quote_age_seconds": _num(quote.get("quote_age_seconds")),
        "quote_age_source": quote.get("quote_age_source"),
        "kalshi_depth_status": quote.get("kalshi_depth_status"),
        "kalshi_depth_missing_reason": quote.get("kalshi_depth_missing_reason"),
        "yes_bid_depth_contracts": _num(quote.get("yes_bid_depth_contracts")),
        "yes_ask_depth_contracts": _num(quote.get("yes_ask_depth_contracts")),
        "no_bid_depth_contracts": _num(quote.get("no_bid_depth_contracts")),
        "no_ask_depth_contracts": _num(quote.get("no_ask_depth_contracts")),
    }
