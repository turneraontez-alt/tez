"""Pure asset-specific strategy-bot decisions.

The thresholds in this file are intentionally named provisional. They came from
the latest learning-export review and must earn their keep out-of-sample.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Mapping


STRATEGY_VERSION = "filtered-alert-system-v3-provisional"

BOT_BASELINE = "baseline_control"
BOT_BNB_NO = "bnb_no_confirmation"
BOT_HYPE_YES = "hype_yes_confirmation"
BOT_MOREFIRE_BTC = "morefire_btc_confirmed"

ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"
RESEARCH_ONLY = "RESEARCH_ONLY"

SPOT_DEPTH_KEYS = (
    "spot_depth_status",
    "spot_depth_missing_reason",
    "spot_depth_source",
    "spot_depth_age_seconds",
    "spot_depth_trade_age_seconds",
    "spot_depth_best_bid",
    "spot_depth_best_ask",
    "spot_depth_mid",
    "spot_depth_spread_bps",
    "spot_depth_bid_depth_top",
    "spot_depth_ask_depth_top",
    "spot_depth_bid_depth_levels",
    "spot_depth_ask_depth_levels",
    "spot_depth_bid_notional_levels",
    "spot_depth_ask_notional_levels",
    "spot_depth_imbalance",
    "spot_depth_trade_buy_qty_5s",
    "spot_depth_trade_sell_qty_5s",
    "spot_depth_trade_net_qty_5s",
    "spot_depth_trade_buy_notional_5s",
    "spot_depth_trade_sell_notional_5s",
    "spot_depth_trade_net_notional_5s",
    "spot_depth_trade_buy_qty_15s",
    "spot_depth_trade_sell_qty_15s",
    "spot_depth_trade_net_qty_15s",
    "spot_depth_trade_buy_notional_15s",
    "spot_depth_trade_sell_notional_15s",
    "spot_depth_trade_net_notional_15s",
    "spot_depth_trade_buy_qty_60s",
    "spot_depth_trade_sell_qty_60s",
    "spot_depth_trade_net_qty_60s",
    "spot_depth_trade_buy_notional_60s",
    "spot_depth_trade_sell_notional_60s",
    "spot_depth_trade_net_notional_60s",
    "spot_depth_last_trade_price",
    "spot_depth_last_trade_side",
    "spot_depth_last_trade_size",
)

KALSHI_FLOW_KEYS = (
    "kalshi_taker_yes_volume_15s",
    "kalshi_taker_no_volume_15s",
    "kalshi_taker_net_yes_volume_15s",
)

KALSHI_DEPTH_KEYS = (
    "depth_contracts",
    "yes_bid_depth_contracts",
    "yes_ask_depth_contracts",
    "no_bid_depth_contracts",
    "no_ask_depth_contracts",
    "kalshi_depth_status",
    "kalshi_depth_missing_reason",
    "kalshi_depth_retry_used",
)


@dataclass(frozen=True)
class BotDecision:
    bot_name: str
    decision_status: str
    reason_codes: tuple[str, ...]
    strategy_version: str = STRATEGY_VERSION
    threshold_profile: Mapping[str, Any] = field(default_factory=dict)
    btc_context: Mapping[str, Any] | None = None


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _side(value: Any) -> str | None:
    side = str(value or "").upper()
    return side if side in {"YES", "NO"} else None


def _asset(row: Mapping[str, Any]) -> str:
    return str(row.get("asset") or "").upper()


def source_side(row: Mapping[str, Any]) -> str | None:
    return _side(
        row.get("predicted_side")
        or row.get("predicted_outcome")
        or row.get("selected_side")
    )


def source_rule(row: Mapping[str, Any]) -> str:
    code = row.get("rule_code")
    if code:
        return str(code)
    reason = str(row.get("reason_codes") or "").strip()
    if reason:
        return reason.split(",")[0].strip()
    return str(row.get("record_kind") or "UNKNOWN")


def _is_research_row(row: Mapping[str, Any]) -> bool:
    delivery = str(row.get("delivery_status") or "").upper()
    kind = str(row.get("record_kind") or "").upper()
    return delivery == "RESEARCH" or "RESEARCH" in kind or "WATCH" in kind


def baseline_decision(row: Mapping[str, Any]) -> BotDecision:
    if _is_research_row(row):
        status = RESEARCH_ONLY
        reason = "BASELINE_RESEARCH_ONLY"
    elif int(row.get("fired") or 0) == 1:
        status = ACCEPTED
        reason = "BASELINE_FIRED"
    elif row.get("fired") is None:
        status = ACCEPTED
        reason = "BASELINE_ALERT_RECORDED"
    else:
        status = REJECTED
        reason = "BASELINE_GATE_REJECTED"
    return BotDecision(
        bot_name=BOT_BASELINE,
        decision_status=status,
        reason_codes=(reason,),
        threshold_profile={"control_group": True},
    )


def bnb_no_confirmation_decision(row: Mapping[str, Any]) -> BotDecision | None:
    if _asset(row) != "BNB":
        return None
    side = source_side(row)
    thresholds = {
        "spot_depth_trade_sell_notional_15s_min": 40.0,
        "spot_depth_imbalance_max": -0.02,
        "tiny_negative_imbalance_floor": -0.02,
        "provisional": True,
    }
    if side != "NO":
        return BotDecision(
            bot_name=BOT_BNB_NO,
            decision_status=REJECTED,
            reason_codes=("SIDE_NOT_NO",),
            threshold_profile=thresholds,
        )

    sell_15 = _num(row.get("spot_depth_trade_sell_notional_15s"))
    imbalance = _num(row.get("spot_depth_imbalance"))
    reasons: list[str] = []
    if sell_15 is not None and sell_15 >= 40.0:
        reasons.append("SELL_NOTIONAL_15S_GE_40")
    if imbalance is not None and imbalance <= -0.02:
        reasons.append("SPOT_IMBALANCE_LE_NEG_0_02")

    if reasons:
        return BotDecision(BOT_BNB_NO, ACCEPTED, tuple(reasons), threshold_profile=thresholds)

    reject_reasons = ["NO_SPOT_SELL_CONFIRMATION"]
    if imbalance is not None and -0.02 < imbalance < 0.0:
        reject_reasons.append("TINY_NEGATIVE_IMBALANCE")
    if sell_15 is None and imbalance is None:
        reject_reasons.append("SPOT_DEPTH_MISSING")
    return BotDecision(
        bot_name=BOT_BNB_NO,
        decision_status=REJECTED,
        reason_codes=tuple(reject_reasons),
        threshold_profile=thresholds,
    )


def _yes_market_pressure(row: Mapping[str, Any]) -> float | None:
    implied = _num(row.get("market_implied_yes_probability"))
    if implied is None:
        implied = _num(row.get("selected_probability"))
    if implied is None:
        implied = _num(row.get("model_yes_probability"))
    if implied is None:
        return None
    # Convert probability to cents above/below a balanced book.
    return (implied - 0.5) * 100.0


def hype_yes_confirmation_decision(row: Mapping[str, Any]) -> BotDecision | None:
    if _asset(row) != "HYPE":
        return None
    side = source_side(row)
    thresholds = {
        "spot_depth_imbalance_bullish_max": -0.056,
        "spot_depth_trade_net_qty_60s_min": 0.0,
        "spot_depth_trade_net_qty_60s_strong_min": 38.4,
        "yes_ask_depth_contracts_min": 260.0,
        "kalshi_yes_pressure_cents_min": -2.0,
        "kalshi_taker_net_yes_volume_15s_min": -25.0,
        "provisional": True,
    }
    if side != "YES":
        return BotDecision(BOT_HYPE_YES, REJECTED, ("SIDE_NOT_YES",), threshold_profile=thresholds)

    imbalance = _num(row.get("spot_depth_imbalance"))
    net_qty_60 = _num(row.get("spot_depth_trade_net_qty_60s"))
    yes_ask_depth = _num(row.get("yes_ask_depth_contracts"))
    taker_net = _num(row.get("kalshi_taker_net_yes_volume_15s"))
    yes_pressure = _yes_market_pressure(row)

    spot_reasons: list[str] = []
    strong_spot = False
    if imbalance is not None and imbalance <= -0.056:
        spot_reasons.append("SPOT_IMBALANCE_BULLISH_LE_NEG_0_056")
        strong_spot = True
    if net_qty_60 is not None and net_qty_60 > 0.0:
        spot_reasons.append("SPOT_NET_QTY_60S_POSITIVE")
        if net_qty_60 >= 38.4:
            spot_reasons.append("SPOT_NET_QTY_60S_GE_38_4")
            strong_spot = True

    kalshi_reasons: list[str] = []
    if yes_ask_depth is not None and yes_ask_depth >= 260.0:
        kalshi_reasons.append("YES_ASK_DEPTH_GE_260")
    if yes_pressure is None or yes_pressure >= -2.0:
        kalshi_reasons.append("KALSHI_YES_PRESSURE_NOT_CONTRA")
    if taker_net is None:
        kalshi_reasons.append("TAKER_FLOW_MISSING_STRONGER_CONFIRM_REQUIRED")
    elif taker_net >= -25.0:
        kalshi_reasons.append("TAKER_NET_NOT_STRONGLY_NO")

    book_ok = (
        yes_ask_depth is not None
        and yes_ask_depth >= 260.0
        and (yes_pressure is None or yes_pressure >= -2.0)
    )
    taker_ok = taker_net is not None and taker_net >= -25.0
    missing_taker_ok = taker_net is None and strong_spot and book_ok
    if spot_reasons and book_ok and (taker_ok or missing_taker_ok):
        return BotDecision(
            BOT_HYPE_YES,
            ACCEPTED,
            tuple(spot_reasons + kalshi_reasons),
            threshold_profile=thresholds,
        )

    reject_reasons: list[str] = []
    if not spot_reasons:
        reject_reasons.append("SPOT_NOT_BULLISH")
    if not book_ok:
        reject_reasons.append("KALSHI_BOOK_NOT_CONFIRMED")
    if taker_net is None and not missing_taker_ok:
        reject_reasons.append("TAKER_MISSING_WITH_WEAK_CONFIRM")
    if taker_net is not None and taker_net < -25.0:
        reject_reasons.append("TAKER_NET_STRONGLY_NO")
    return BotDecision(
        BOT_HYPE_YES,
        REJECTED,
        tuple(reject_reasons or ("HYPE_YES_CONFIRMATION_FAILED",)),
        threshold_profile=thresholds,
    )


def _mid_from_bid_ask(bid: Any, ask: Any) -> float | None:
    vals = [v for v in (_num(bid), _num(ask)) if v is not None and 0.0 <= v <= 100.0]
    return sum(vals) / len(vals) if vals else None


def _btc_context(row: Mapping[str, Any], btc: Mapping[str, Any] | None) -> dict[str, Any]:
    source = btc or {}
    yes_mid = _num(source.get("yes_mid_cents"))
    if yes_mid is None:
        yes_mid = _mid_from_bid_ask(source.get("yes_bid_cents"), source.get("yes_ask_cents"))
    if yes_mid is None:
        yes_mid = _num(row.get("btc_yes_mid_cents"))
    no_mid = _num(source.get("no_mid_cents"))
    if no_mid is None:
        no_mid = _mid_from_bid_ask(source.get("no_bid_cents"), source.get("no_ask_cents"))
    if no_mid is None:
        no_mid = _num(row.get("btc_no_mid_cents"))

    dominant = _side(source.get("dominant_side")) or _side(row.get("btc_dominant_side"))
    if dominant is None and (yes_mid is not None or no_mid is not None):
        dominant = "YES" if no_mid is None or (yes_mid is not None and yes_mid >= no_mid) else "NO"

    model_side = _side(source.get("predicted_side") or row.get("btc_model_predicted_side"))
    model_yes = _num(source.get("model_yes_probability") or row.get("btc_model_yes_probability"))
    cal_yes = _num(source.get("calibrated_yes_probability") or row.get("btc_calibrated_yes_probability"))
    market_yes = _num(
        source.get("market_implied_yes_probability")
        or row.get("btc_market_implied_yes_probability")
    )
    pressure_cents = None if yes_mid is None or no_mid is None else yes_mid - no_mid
    depth = _num(source.get("depth_contracts") or row.get("btc_depth_contracts"))
    return {
        "btc_ticker": source.get("ticker") or row.get("btc_ticker"),
        "btc_depth_contracts": depth,
        "btc_dominant_side": dominant,
        "btc_yes_mid_cents": yes_mid,
        "btc_no_mid_cents": no_mid,
        "btc_book_pressure_cents": pressure_cents,
        "btc_model_predicted_side": model_side,
        "btc_model_yes_probability": model_yes,
        "btc_calibrated_yes_probability": cal_yes,
        "btc_market_implied_yes_probability": market_yes,
        "btc_yes_bid_depth_contracts": _num(source.get("yes_bid_depth_contracts")),
        "btc_yes_ask_depth_contracts": _num(source.get("yes_ask_depth_contracts")),
        "btc_no_bid_depth_contracts": _num(source.get("no_bid_depth_contracts")),
        "btc_no_ask_depth_contracts": _num(source.get("no_ask_depth_contracts")),
        "btc_kalshi_taker_net_yes_volume_15s": _num(
            source.get("kalshi_taker_net_yes_volume_15s")
        ),
        "btc_context_available": bool(source or row.get("btc_ticker")),
    }


def morefire_btc_confirmed_decision(
    row: Mapping[str, Any],
    btc_context: Mapping[str, Any] | None = None,
) -> BotDecision | None:
    rule = source_rule(row)
    kind = str(row.get("record_kind") or "")
    if rule != "HVF_MORE_FIRE_STRICT" and kind != "MORE_FIRE_STRICT_ALERT":
        return None
    thresholds = {
        "btc_depth_contracts_min": 1225.0,
        "btc_book_pressure_cents_min": 0.0,
        "btc_model_yes_probability_floor_for_no_veto": 0.48,
        "provisional": True,
    }
    side = source_side(row)
    ctx = _btc_context(row, btc_context)
    if side != "YES":
        return BotDecision(
            BOT_MOREFIRE_BTC,
            REJECTED,
            ("SIDE_NOT_YES",),
            threshold_profile=thresholds,
            btc_context=ctx,
        )

    depth = _num(ctx.get("btc_depth_contracts"))
    pressure = _num(ctx.get("btc_book_pressure_cents"))
    dominant = _side(ctx.get("btc_dominant_side"))
    model_side = _side(ctx.get("btc_model_predicted_side"))
    model_yes = _num(ctx.get("btc_model_yes_probability"))
    cal_yes = _num(ctx.get("btc_calibrated_yes_probability"))
    market_yes = _num(ctx.get("btc_market_implied_yes_probability"))

    reasons: list[str] = []
    if not ctx.get("btc_context_available"):
        reasons.append("BTC_CONTEXT_MISSING")
    if depth is not None and depth >= 1225.0:
        reasons.append("BTC_DEPTH_GE_1225")
    else:
        reasons.append("BTC_DEPTH_WEAK_OR_MISSING")
    if pressure is not None and pressure >= 0.0:
        reasons.append("BTC_BOOK_PRESSURE_SUPPORTIVE")
    else:
        reasons.append("BTC_BOOK_PRESSURE_WEAK_OR_MISSING")

    contra = False
    if dominant == "NO":
        contra = True
        reasons.append("BTC_DOMINANT_SIDE_NO")
    if model_side == "NO" and (
        (cal_yes is not None and cal_yes < 0.48)
        or (model_yes is not None and model_yes < 0.48)
        or (market_yes is not None and market_yes < 0.48)
    ):
        contra = True
        reasons.append("BTC_MODEL_MARKET_CONTRA")

    if depth is not None and depth >= 1225.0 and pressure is not None and pressure >= 0.0 and not contra:
        return BotDecision(
            BOT_MOREFIRE_BTC,
            ACCEPTED,
            tuple(reasons),
            threshold_profile=thresholds,
            btc_context=ctx,
        )
    status = RESEARCH_ONLY if "BTC_CONTEXT_MISSING" not in reasons else RESEARCH_ONLY
    return BotDecision(
        BOT_MOREFIRE_BTC,
        status,
        tuple(reasons or ("BTC_SUPPORT_NOT_CONFIRMED",)),
        threshold_profile=thresholds,
        btc_context=ctx,
    )


def decisions_for_row(
    row: Mapping[str, Any],
    *,
    source_system: str,
    btc_context: Mapping[str, Any] | None = None,
) -> list[BotDecision]:
    """Return the side-by-side strategy decisions for one source alert row."""
    decisions: list[BotDecision] = [baseline_decision(row)]
    bnb = bnb_no_confirmation_decision(row)
    if bnb is not None:
        decisions.append(bnb)
    hype = hype_yes_confirmation_decision(row)
    if hype is not None:
        decisions.append(hype)
    if source_system == "high_vol_flip":
        morefire = morefire_btc_confirmed_decision(row, btc_context)
        if morefire is not None:
            decisions.append(morefire)
    return decisions
