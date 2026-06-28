"""Pure asset-specific strategy-bot decisions.

The thresholds in this file are intentionally named provisional. They came from
the latest learning-export review and must earn their keep out-of-sample.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Mapping


STRATEGY_VERSION = "filtered-alert-system-v3-confidence-tiers-provisional"

BOT_BASELINE = "baseline_control"
BOT_CONFIDENCE_TIER = "v3_confidence_tier"
BOT_BNB_NO = "bnb_no_confirmation"
BOT_BNB_YES_REVERSAL = "bnb_yes_reversal"
BOT_HYPE_YES = "hype_yes_confirmation"
BOT_MOREFIRE_BTC = "morefire_btc_confirmed"

TIER_A = "A"
TIER_B = "B"
TIER_C = "C"
TIER_NONE = "NONE"

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
    tier: str | None = None
    threshold_profile: Mapping[str, Any] = field(default_factory=dict)
    btc_context: Mapping[str, Any] | None = None
    side_override: str | None = None
    original_source_side: str | None = None
    entry_ask_cents: Any | None = None
    use_entry_ask_override: bool = False


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


def _entry_ask(row: Mapping[str, Any]) -> float | None:
    value = row.get("entry_ask_cents")
    if value is None:
        value = row.get("selected_ask_cents")
    return _num(value)


def _source_text(row: Mapping[str, Any]) -> str:
    return " ".join(
        str(value or "")
        for value in (
            source_rule(row),
            row.get("rule_name"),
            row.get("source_rule"),
            row.get("source_rule_name"),
            row.get("reason_codes"),
            row.get("source_reason_codes"),
        )
    ).upper()


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
        tier=TIER_NONE,
        threshold_profile={"control_group": True},
    )


def _confidence_thresholds() -> dict[str, Any]:
    return {
        "tier_a": {
            "ultoim_v2": {
                "BTC": {"YES": {"entry_ask_cents_min": 74.0}, "NO": {"entry_ask_cents_min": 75.0}},
                "DOGE": {"YES": {"entry_ask_cents_min": 69.0}, "NO": {"entry_ask_cents_min": 78.0}},
                "ETH": {"YES": {"entry_ask_cents_min": 80.0}},
            },
            "high_vol_flip": {
                "SOL": {"YES": {"entry_ask_cents_min": 66.0}},
                "ETH": {"NO": {"entry_ask_cents_max": 91.6}},
            },
        },
        "tier_b": {
            "ultoim_v2": {
                "BTC": {"YES": {"entry_ask_cents_min": 57.0}, "NO": {"entry_ask_cents_min": 62.0}},
                "ETH": {"YES": {"entry_ask_cents_min": 67.0}},
                "DOGE": {"NO": {"entry_ask_cents_min": 76.0}},
            },
        },
        "tier_c": {
            "ultoim_v2": {
                "XRP": {"NO": {"source_rule_contains": "EXPENSIVE_NO_ADMIT", "entry_ask_cents_min": 76.0}},
                "SOL": {"NO": {"source_rule_contains": "EXPENSIVE_NO_ADMIT", "entry_ask_cents_min": 76.0}},
                "BNB": {"NO": {"source_rule_contains": "EXPENSIVE_NO_ADMIT", "entry_ask_cents_min": 77.0}},
            },
        },
        "provisional": True,
        "paper_only": True,
    }


def _source_matches(source_system: str, expected: str) -> bool:
    return str(source_system or "").lower() == expected


def _ask_ge(row: Mapping[str, Any], threshold: float) -> bool:
    ask = _entry_ask(row)
    return ask is not None and ask >= float(threshold)


def _ask_le(row: Mapping[str, Any], threshold: float) -> bool:
    ask = _entry_ask(row)
    return ask is not None and ask <= float(threshold)


def _tier_rule_reason(prefix: str, source_system: str, asset: str, side: str, suffix: str) -> str:
    source = "ULTOIM" if source_system == "ultoim_v2" else "HVF"
    return f"{prefix}_{source}_{asset}_{side}_{suffix}"


def confidence_tier_decision(
    row: Mapping[str, Any],
    *,
    source_system: str,
) -> BotDecision:
    """Single prioritized A/B/C confidence-tier decision for every source row."""
    thresholds = _confidence_thresholds()
    source = str(source_system or "").lower()
    asset = _asset(row)
    side = source_side(row)
    ask = _entry_ask(row)
    base_profile = {
        **thresholds,
        "source_system": source_system,
        "asset": asset,
        "side": side,
        "entry_ask_cents": ask,
    }
    if not asset or side not in {"YES", "NO"}:
        return BotDecision(
            BOT_CONFIDENCE_TIER,
            REJECTED,
            ("V3_CONFIDENCE_TIER_INVALID_ASSET_OR_SIDE",),
            tier=TIER_NONE,
            threshold_profile=base_profile,
        )

    # Tier A always wins before Tier B, so a strict pick never double-counts as
    # volume expansion.
    tier_a_checks: list[tuple[bool, str]] = [
        (
            _source_matches(source, "ultoim_v2") and asset == "BTC" and side == "YES" and _ask_ge(row, 74.0),
            _tier_rule_reason("V3_TIER_A", source, asset, side, "ASK_GE_74"),
        ),
        (
            _source_matches(source, "ultoim_v2") and asset == "BTC" and side == "NO" and _ask_ge(row, 75.0),
            _tier_rule_reason("V3_TIER_A", source, asset, side, "ASK_GE_75"),
        ),
        (
            _source_matches(source, "ultoim_v2") and asset == "DOGE" and side == "YES" and _ask_ge(row, 69.0),
            _tier_rule_reason("V3_TIER_A", source, asset, side, "ASK_GE_69"),
        ),
        (
            _source_matches(source, "ultoim_v2") and asset == "DOGE" and side == "NO" and _ask_ge(row, 78.0),
            _tier_rule_reason("V3_TIER_A", source, asset, side, "ASK_GE_78"),
        ),
        (
            _source_matches(source, "high_vol_flip") and asset == "SOL" and side == "YES" and _ask_ge(row, 66.0),
            _tier_rule_reason("V3_TIER_A", source, asset, side, "ASK_GE_66"),
        ),
        (
            _source_matches(source, "high_vol_flip") and asset == "ETH" and side == "NO" and _ask_le(row, 91.6),
            _tier_rule_reason("V3_TIER_A", source, asset, side, "ASK_LE_91_6"),
        ),
        (
            _source_matches(source, "ultoim_v2") and asset == "ETH" and side == "YES" and _ask_ge(row, 80.0),
            _tier_rule_reason("V3_TIER_A", source, asset, side, "ASK_GE_80"),
        ),
    ]
    for passed, reason in tier_a_checks:
        if passed:
            return BotDecision(
                BOT_CONFIDENCE_TIER,
                ACCEPTED,
                ("V3_TIER_A_STRICT_7_HIGH_CONFIDENCE", reason),
                tier=TIER_A,
                threshold_profile={**base_profile, "tier": TIER_A},
            )

    tier_b_checks: list[tuple[bool, str]] = [
        (
            _source_matches(source, "ultoim_v2") and asset == "BTC" and side == "YES" and _ask_ge(row, 57.0),
            _tier_rule_reason("V3_TIER_B", source, asset, side, "ASK_GE_57"),
        ),
        (
            _source_matches(source, "ultoim_v2") and asset == "BTC" and side == "NO" and _ask_ge(row, 62.0),
            _tier_rule_reason("V3_TIER_B", source, asset, side, "ASK_GE_62"),
        ),
        (
            _source_matches(source, "ultoim_v2") and asset == "ETH" and side == "YES" and _ask_ge(row, 67.0),
            _tier_rule_reason("V3_TIER_B", source, asset, side, "ASK_GE_67"),
        ),
        (
            _source_matches(source, "ultoim_v2") and asset == "DOGE" and side == "NO" and _ask_ge(row, 76.0),
            _tier_rule_reason("V3_TIER_B", source, asset, side, "ASK_GE_76"),
        ),
    ]
    for passed, reason in tier_b_checks:
        if passed:
            return BotDecision(
                BOT_CONFIDENCE_TIER,
                ACCEPTED,
                ("V3_TIER_B_VOLUME_EXPANSION", reason),
                tier=TIER_B,
                threshold_profile={**base_profile, "tier": TIER_B},
            )

    text = _source_text(row)
    tier_c_checks: list[tuple[bool, str]] = [
        (
            _source_matches(source, "ultoim_v2")
            and asset == "XRP"
            and side == "NO"
            and "EXPENSIVE_NO_ADMIT" in text
            and _ask_ge(row, 76.0),
            "V3_TIER_C_ULTOIM_XRP_NO_EXPENSIVE_NO_ADMIT_ASK_GE_76",
        ),
        (
            _source_matches(source, "ultoim_v2")
            and asset == "SOL"
            and side == "NO"
            and "EXPENSIVE_NO_ADMIT" in text
            and _ask_ge(row, 76.0),
            "V3_TIER_C_ULTOIM_SOL_NO_EXPENSIVE_NO_ADMIT_ASK_GE_76",
        ),
        (
            _source_matches(source, "ultoim_v2")
            and asset == "BNB"
            and side == "NO"
            and "EXPENSIVE_NO_ADMIT" in text
            and _ask_ge(row, 77.0),
            "V3_TIER_C_ULTOIM_BNB_NO_EXPENSIVE_NO_ADMIT_ASK_GE_77",
        ),
    ]
    for passed, reason in tier_c_checks:
        if passed:
            return BotDecision(
                BOT_CONFIDENCE_TIER,
                RESEARCH_ONLY,
                ("V3_TIER_C_RESEARCH_ONLY", reason),
                tier=TIER_C,
                threshold_profile={**base_profile, "tier": TIER_C, "research_only": True},
            )

    reason = "V3_CONFIDENCE_TIER_NO_MATCH"
    if ask is None:
        reason = "V3_CONFIDENCE_TIER_ENTRY_ASK_MISSING"
    return BotDecision(
        BOT_CONFIDENCE_TIER,
        REJECTED,
        (reason,),
        tier=TIER_NONE,
        threshold_profile={**base_profile, "tier": TIER_NONE},
    )


def bnb_no_confirmation_decision(row: Mapping[str, Any]) -> BotDecision | None:
    if _asset(row) != "BNB":
        return None
    side = source_side(row)
    thresholds = {
        "spot_depth_trade_sell_notional_15s_min": 40.0,
        "spot_depth_imbalance_max": -0.02,
        "spot_depth_trade_net_notional_15s_max": -25.0,
        "spot_depth_trade_net_qty_15s_max": 0.0,
        "kalshi_taker_net_yes_volume_15s_max": 0.0,
        "bearish_score_min": 2,
        "veto_spot_depth_trade_net_notional_60s_min": 0.0,
        "veto_spot_depth_trade_net_qty_60s_min": 0.0,
        "veto_kalshi_taker_net_yes_volume_15s_min": 10.0,
        "veto_spot_depth_imbalance_min": 0.0,
        "provisional": True,
    }
    if side != "NO":
        return BotDecision(
            bot_name=BOT_BNB_NO,
            decision_status=REJECTED,
            reason_codes=("SIDE_NOT_NO",),
            threshold_profile=thresholds,
        )

    imbalance = _num(row.get("spot_depth_imbalance"))
    net_notional_60 = _num(row.get("spot_depth_trade_net_notional_60s"))
    net_qty_60 = _num(row.get("spot_depth_trade_net_qty_60s"))
    taker_net_yes = _num(row.get("kalshi_taker_net_yes_volume_15s"))
    veto_reasons: list[str] = []
    if net_notional_60 is not None and net_notional_60 > 0.0:
        veto_reasons.append("BNB_NO_VETO_SPOT_NET_NOTIONAL_60S_POSITIVE")
    if net_qty_60 is not None and net_qty_60 > 0.0:
        veto_reasons.append("BNB_NO_VETO_SPOT_NET_QTY_60S_POSITIVE")
    if taker_net_yes is not None and taker_net_yes >= 10.0:
        veto_reasons.append("BNB_NO_VETO_KALSHI_TAKER_YES_GE_10")
    if imbalance is not None and imbalance > 0.0:
        veto_reasons.append("BNB_NO_VETO_SPOT_IMBALANCE_POSITIVE")
    if veto_reasons:
        return BotDecision(
            bot_name=BOT_BNB_NO,
            decision_status=REJECTED,
            reason_codes=tuple(veto_reasons),
            threshold_profile=thresholds,
        )

    sell_15 = _num(row.get("spot_depth_trade_sell_notional_15s"))
    net_notional_15 = _num(row.get("spot_depth_trade_net_notional_15s"))
    net_qty_15 = _num(row.get("spot_depth_trade_net_qty_15s"))
    reasons: list[str] = []
    if sell_15 is not None and sell_15 >= 40.0:
        reasons.append("SELL_NOTIONAL_15S_GE_40")
    if imbalance is not None and imbalance <= -0.02:
        reasons.append("SPOT_IMBALANCE_LE_NEG_0_02")
    if net_notional_15 is not None and net_notional_15 <= -25.0:
        reasons.append("SPOT_NET_NOTIONAL_15S_LE_NEG_25")
    if net_qty_15 is not None and net_qty_15 <= 0.0:
        reasons.append("SPOT_NET_QTY_15S_LE_0")
    if taker_net_yes is not None and taker_net_yes <= 0.0:
        reasons.append("KALSHI_TAKER_NET_YES_15S_LE_0")

    if len(reasons) >= 2:
        return BotDecision(BOT_BNB_NO, ACCEPTED, tuple(reasons), threshold_profile=thresholds)

    reject_reasons = ["BNB_NO_BEARISH_SCORE_LT_2"]
    reject_reasons.extend(reasons)
    if imbalance is not None and -0.02 < imbalance < 0.0:
        reject_reasons.append("TINY_NEGATIVE_IMBALANCE")
    if all(value is None for value in (sell_15, imbalance, net_notional_15, net_qty_15, taker_net_yes)):
        reject_reasons.append("SPOT_DEPTH_MISSING")
    return BotDecision(
        bot_name=BOT_BNB_NO,
        decision_status=REJECTED,
        reason_codes=tuple(reject_reasons),
        threshold_profile=thresholds,
    )


def _bnb_no_veto_reasons(decision: BotDecision | None) -> tuple[str, ...]:
    if decision is None or decision.bot_name != BOT_BNB_NO:
        return ()
    return tuple(code for code in decision.reason_codes if code.startswith("BNB_NO_VETO_"))


def _yes_reversal_entry_ask(row: Mapping[str, Any]) -> tuple[Any | None, str | None]:
    explicit = row.get("yes_ask_cents") if row.get("yes_ask_cents") is not None else row.get("yes_ask")
    if _num(explicit) is not None:
        return explicit, None
    no_bid = _num(row.get("no_bid_cents"))
    if no_bid is not None:
        yes_ask = 100.0 - no_bid
        if 0.0 <= yes_ask <= 100.0:
            return yes_ask, "BNB_YES_REVERSAL_ENTRY_DERIVED_FROM_NO_BID"
    no_ask = _num(row.get("entry_ask_cents"))
    if no_ask is None:
        no_ask = _num(row.get("selected_ask_cents"))
    if no_ask is None:
        no_ask = _num(row.get("no_ask_cents"))
    spread = _num(row.get("spread_cents"))
    if no_ask is not None and spread is not None:
        yes_ask = 100.0 - no_ask + spread
        if 0.0 <= yes_ask <= 100.0:
            return yes_ask, "BNB_YES_REVERSAL_ENTRY_ESTIMATED_FROM_NO_SPREAD"
    return None, None


def bnb_yes_reversal_decision(
    row: Mapping[str, Any],
    *,
    source_system: str,
    no_decision: BotDecision | None = None,
) -> BotDecision | None:
    if source_system != "ultoim_v2":
        return None
    if _asset(row) != "BNB" or source_side(row) != "NO":
        return None
    veto_reasons = _bnb_no_veto_reasons(no_decision)
    if not veto_reasons:
        return None
    text = _source_text(row)
    if "ASK_ABOVE_BAND" not in text and "EXPENSIVE_NO_ADMIT" not in text:
        return None

    thresholds = {
        "spot_depth_imbalance_min": -0.05,
        "spot_depth_trade_net_notional_60s_min": 50.0,
        "spot_depth_trade_net_notional_15s_strong_min": 0.0,
        "kalshi_taker_net_yes_volume_15s_strong_min": 10.0,
        "min_resolved_before_promotion": 30,
        "provisional": True,
        "research_only": True,
    }
    imbalance = _num(row.get("spot_depth_imbalance"))
    net_notional_60 = _num(row.get("spot_depth_trade_net_notional_60s"))
    net_notional_15 = _num(row.get("spot_depth_trade_net_notional_15s"))
    taker_net_yes = _num(row.get("kalshi_taker_net_yes_volume_15s"))
    if imbalance is None or imbalance <= -0.05:
        return None
    if net_notional_60 is None or net_notional_60 < 50.0:
        return None

    reasons = [
        "BNB_YES_REVERSAL_RESEARCH_ONLY",
        "BNB_YES_REVERSAL_SOURCE_ULTOIM_V2",
        "BNB_YES_REVERSAL_RULE_ASK_ABOVE_OR_EXPENSIVE",
        "BNB_YES_REVERSAL_IMBALANCE_GT_NEG_0_05",
        "BNB_YES_REVERSAL_SPOT_NET_NOTIONAL_60S_GE_50",
        *veto_reasons,
    ]
    if net_notional_15 is not None and net_notional_15 > 0.0:
        reasons.append("BNB_YES_REVERSAL_STRONG_SPOT_NET_NOTIONAL_15S_POSITIVE")
    if taker_net_yes is not None and taker_net_yes >= 10.0:
        reasons.append("BNB_YES_REVERSAL_STRONG_KALSHI_TAKER_YES_GE_10")
    yes_ask, entry_reason = _yes_reversal_entry_ask(row)
    if entry_reason:
        reasons.append(entry_reason)

    return BotDecision(
        bot_name=BOT_BNB_YES_REVERSAL,
        decision_status=RESEARCH_ONLY,
        reason_codes=tuple(reasons),
        threshold_profile=thresholds,
        side_override="YES",
        original_source_side="NO",
        entry_ask_cents=yes_ask,
        use_entry_ask_override=True,
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
    decisions: list[BotDecision] = [
        baseline_decision(row),
        confidence_tier_decision(row, source_system=source_system),
    ]
    bnb = bnb_no_confirmation_decision(row)
    if bnb is not None:
        decisions.append(bnb)
        bnb_reversal = bnb_yes_reversal_decision(
            row,
            source_system=source_system,
            no_decision=bnb,
        )
        if bnb_reversal is not None:
            decisions.append(bnb_reversal)
    hype = hype_yes_confirmation_decision(row)
    if hype is not None:
        decisions.append(hype)
    if source_system == "high_vol_flip":
        morefire = morefire_btc_confirmed_decision(row, btc_context)
        if morefire is not None:
            decisions.append(morefire)
    return decisions
