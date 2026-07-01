"""Pure asset-specific strategy-bot decisions.

The thresholds in this file are intentionally named provisional. They came from
the latest learning-export review and must earn their keep out-of-sample.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from typing import Any, Mapping


STRATEGY_VERSION = "filtered-alert-system-v3-15m-depth-formula-research-provisional"

BOT_BASELINE = "baseline_control"
BOT_CONFIDENCE_TIER = "v3_confidence_tier"
BOT_BNB_NO = "bnb_no_confirmation"
BOT_BNB_YES_REVERSAL = "bnb_yes_reversal"
BOT_HYPE_YES = "hype_yes_confirmation"
BOT_MOREFIRE_BTC = "morefire_btc_confirmed"
BOT_HVF_DEPTH_FLOW = "hvf_depth_flow_wrapper"
BOT_BTC_REGIME = "btc_regime_context_probe"
BOT_DEPTH_FORMULA_15M = "v3_15m_depth_formula_research"

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

COINBASE_L2_BANDS = (1, 5, 12, 25, 50, 60, 100, 250)

COINBASE_L2_BASE_KEYS = (
    "coinbase_l2_status",
    "coinbase_l2_missing_reason",
    "coinbase_l2_product_id",
    "coinbase_l2_age_seconds",
    "coinbase_l2_snapshot_created_at",
    "coinbase_l2_last_message_age_seconds",
    "coinbase_l2_best_bid",
    "coinbase_l2_best_ask",
    "coinbase_l2_mid",
    "coinbase_l2_spread_bps",
    "coinbase_l2_bid_level_count",
    "coinbase_l2_ask_level_count",
    "coinbase_l2_stored_bid_level_count",
    "coinbase_l2_stored_ask_level_count",
    "coinbase_l2_update_count_5s",
    "coinbase_l2_remove_count_5s",
    "coinbase_l2_remove_rate_5s",
    "coinbase_l2_update_count_15s",
    "coinbase_l2_remove_count_15s",
    "coinbase_l2_remove_rate_15s",
    "coinbase_l2_update_count_60s",
    "coinbase_l2_remove_count_60s",
    "coinbase_l2_remove_rate_60s",
)

COINBASE_L2_TARGET_KEYS = (
    "coinbase_l2_target_price",
    "coinbase_l2_target_source",
    "coinbase_l2_target_age_seconds",
    "coinbase_l2_distance_to_target",
    "coinbase_l2_distance_to_target_pct",
    "coinbase_l2_distance_to_target_bps",
    "coinbase_l2_up_to_target_notional",
    "coinbase_l2_up_to_target_levels",
    "coinbase_l2_up_to_target_visible",
    "coinbase_l2_down_to_target_notional",
    "coinbase_l2_down_to_target_levels",
    "coinbase_l2_down_to_target_visible",
    "coinbase_l2_easier_side",
    "coinbase_l2_easier_side_notional",
    "coinbase_l2_harder_side_notional",
    "coinbase_l2_target_notional_ratio",
    "coinbase_l2_target_side_flow_15s",
    "coinbase_l2_target_side_flow_60s",
    "coinbase_l2_flow_needed_seconds_15s",
    "coinbase_l2_flow_needed_seconds_60s",
)

COINBASE_L2_BAND_KEYS = tuple(
    f"coinbase_l2_top_{band}_{suffix}"
    for band in COINBASE_L2_BANDS
    for suffix in (
        "bid_qty",
        "ask_qty",
        "bid_notional",
        "ask_notional",
        "imbalance_qty",
        "imbalance_notional",
    )
)

COINBASE_L2_KEYS = COINBASE_L2_BASE_KEYS + COINBASE_L2_BAND_KEYS + COINBASE_L2_TARGET_KEYS

KRAKEN_L3_KEYS = (
    "kraken_l3_status",
    "kraken_l3_missing_reason",
    "kraken_l3_symbol",
    "kraken_l3_age_seconds",
    "kraken_l3_snapshot_created_at",
    "kraken_l3_checksum",
    "kraken_l3_last_message_age_seconds",
    "kraken_l3_best_bid",
    "kraken_l3_best_ask",
    "kraken_l3_mid",
    "kraken_l3_spread_bps",
    "kraken_l3_bid_order_count",
    "kraken_l3_ask_order_count",
    "kraken_l3_bid_level_count",
    "kraken_l3_ask_level_count",
    "kraken_l3_bid_depth_top",
    "kraken_l3_ask_depth_top",
    "kraken_l3_bid_depth_levels",
    "kraken_l3_ask_depth_levels",
    "kraken_l3_bid_notional_levels",
    "kraken_l3_ask_notional_levels",
    "kraken_l3_bid_order_count_levels",
    "kraken_l3_ask_order_count_levels",
    "kraken_l3_avg_bid_order_size_levels",
    "kraken_l3_avg_ask_order_size_levels",
    "kraken_l3_depth_imbalance",
    "kraken_l3_add_count_5s",
    "kraken_l3_update_count_5s",
    "kraken_l3_delete_count_5s",
    "kraken_l3_trade_count_5s",
    "kraken_l3_cancel_to_add_5s",
    "kraken_l3_add_count_15s",
    "kraken_l3_update_count_15s",
    "kraken_l3_delete_count_15s",
    "kraken_l3_trade_count_15s",
    "kraken_l3_cancel_to_add_15s",
    "kraken_l3_add_count_60s",
    "kraken_l3_update_count_60s",
    "kraken_l3_delete_count_60s",
    "kraken_l3_trade_count_60s",
    "kraken_l3_cancel_to_add_60s",
    "kraken_l3_matched_buy_notional_60s",
    "kraken_l3_matched_sell_notional_60s",
    "kraken_l3_net_matched_buy_notional_60s",
)

BTC_REGIME_KEYS = (
    "btc_regime",
    "btc_regime_agreement",
    "btc_regime_vote_yes",
    "btc_regime_vote_no",
    "btc_regime_vote_detail",
    "btc_regime_status",
    "btc_regime_missing_reason",
    "btc_spot_age_seconds",
    "btc_spot_depth_imbalance",
    "btc_spot_trade_net_notional_15s",
    "btc_spot_trade_net_notional_60s",
    "btc_coinbase_l2_age_seconds",
    "btc_coinbase_l2_last_message_age_seconds",
    "btc_coinbase_l2_top_12_bid_notional",
    "btc_coinbase_l2_top_12_ask_notional",
    "btc_coinbase_l2_top_12_imbalance_notional",
    "btc_coinbase_l2_top_60_bid_notional",
    "btc_coinbase_l2_top_60_ask_notional",
    "btc_coinbase_l2_top_60_imbalance_notional",
    "btc_coinbase_l2_top_250_bid_notional",
    "btc_coinbase_l2_top_250_ask_notional",
    "btc_coinbase_l2_top_250_imbalance_notional",
    "btc_kalshi_book_imbalance",
    "btc_kalshi_taker_net_yes_volume_15s",
    "btc_v95_age_seconds",
    "btc_v95_checkpoint",
    "btc_v95_grade",
    "btc_v95_predicted_side",
    "btc_v95_selected_probability",
    "btc_kraken_l3_age_seconds",
    "btc_kraken_l3_depth_imbalance",
    "btc_kraken_l3_cancel_to_add_60s",
    "btc_kraken_l3_net_matched_buy_notional_60s",
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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.environ.get(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


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


def _top12_deadband() -> float:
    return _env_float("Q15_V3_COINBASE_TOP12_DEADBAND", 0.01, minimum=0.0)


def _top60_deadband() -> float:
    return _env_float("Q15_V3_COINBASE_TOP60_DEADBAND", 0.01, minimum=0.0)


def _top12_contra_reject_enabled() -> bool:
    return _env_bool("Q15_V3_COINBASE_TOP12_CONTRA_REJECT_ENABLED", True)


def _top12_required_enabled() -> bool:
    return _env_bool("Q15_V3_COINBASE_TOP12_REQUIRED_FOR_ACCEPT", True)


def _combined_contra_reject_enabled() -> bool:
    return _env_bool("Q15_V3_COMBINED_CONTRA_REJECT_ENABLED", True)


def _top250_deadband() -> float:
    return _env_float("Q15_V3_COINBASE_TOP250_DEADBAND", 0.05, minimum=0.0)


def _kraken_l3_deadband() -> float:
    return _env_float("Q15_V3_KRAKEN_L3_DEADBAND", 0.10, minimum=0.0)


def _kraken_l3_hard_veto_enabled() -> bool:
    return _env_bool("Q15_V3_KRAKEN_L3_HARD_VETO_ENABLED", False)


def _spot_net_notional_60s_min() -> float:
    return _env_float("Q15_V3_SPOT_NET_NOTIONAL_60S_CONTRA_MIN", 500.0, minimum=0.0)


def _kalshi_taker_yes_15s_min() -> float:
    return _env_float("Q15_V3_KALSHI_TAKER_YES_15S_CONTRA_MIN", 250.0, minimum=0.0)


def _hvf_wrapper_enabled() -> bool:
    return _env_bool("Q15_V3_HVF_DEPTH_FLOW_WRAPPER_ENABLED", True)


def _hvf_wrapper_research_on_missing() -> bool:
    return _env_bool("Q15_V3_HVF_WRAPPER_RESEARCH_ON_MISSING", True)


def _hvf_morefire_l2_alignment_enabled() -> bool:
    return _env_bool("Q15_V3_HVF_MOREFIRE_L2_ALIGNMENT_ENABLED", True)


def _hvf_own_strong_top12_recovery_enabled() -> bool:
    return _env_bool("Q15_V3_HVF_OWN_STRONG_TOP12_RECOVERY_ENABLED", True)


def _hvf_own_strong_recovery_entry_max() -> float:
    return _env_float("Q15_V3_HVF_OWN_STRONG_TOP12_RECOVERY_ENTRY_MAX", 85.0, minimum=0.0)


def _v3_positive_ev_gate_enabled() -> bool:
    return _env_bool("Q15_V3_POSITIVE_EV_GATE_ENABLED", True)


def _v3_morefire_accept_enabled() -> bool:
    return _env_bool("Q15_V3_MOREFIRE_ACCEPT_ENABLED", False)


def _v3_hype_yes_accept_enabled() -> bool:
    return _env_bool("Q15_V3_HYPE_YES_ACCEPT_ENABLED", False)


def _v3_hvf_own_strong_interval_gate_enabled() -> bool:
    return _env_bool("Q15_V3_HVF_OWN_STRONG_INTERVAL_GATE_ENABLED", True)


def _hvf_yes_contra_veto_enabled() -> bool:
    return _env_bool("Q15_V3_HVF_YES_CONTRA_VETO_ENABLED", True)


def _hvf_yes_spot_imbalance_deadband() -> float:
    return _env_float("Q15_V3_HVF_YES_SPOT_IMBALANCE_DEADBAND", 0.01, minimum=0.0)


def _depth_formula_research_enabled() -> bool:
    return _env_bool("Q15_V3_DEPTH_FORMULA_RESEARCH_ENABLED", True)


def _depth_formula_spread_max() -> float:
    return _env_float("Q15_V3_DEPTH_FORMULA_SPREAD_MAX_CENTS", 10.0, minimum=0.0)


def _depth_formula_entry_ask_max() -> float:
    return _env_float("Q15_V3_DEPTH_FORMULA_ENTRY_ASK_MAX_CENTS", 55.0, minimum=0.0)


def _depth_formula_selected_ask_depth_max() -> float:
    return _env_float("Q15_V3_DEPTH_FORMULA_SELECTED_ASK_DEPTH_MAX", 50.0, minimum=0.0)


def _depth_formula_bid_ask_ratio_min() -> float:
    return _env_float("Q15_V3_DEPTH_FORMULA_BID_ASK_RATIO_MIN", 0.25, minimum=0.0)


def _depth_signal(row: Mapping[str, Any], key: str, deadband: float) -> tuple[str | None, float | None]:
    value = _num(row.get(key))
    if value is None:
        return None, None
    if value >= deadband:
        return "YES", value
    if value <= -deadband:
        return "NO", value
    return "NEUTRAL", value


def _opposite(side: str) -> str:
    return "NO" if side == "YES" else "YES"


def _selected_bid_depth(row: Mapping[str, Any], side: str | None) -> float | None:
    if side == "YES":
        return _num(row.get("yes_bid_depth_contracts"))
    if side == "NO":
        return _num(row.get("no_bid_depth_contracts"))
    return None


def _flow_side(value: float | None, threshold: float) -> str | None:
    if value is None:
        return None
    if value >= threshold:
        return "YES"
    if value <= -threshold:
        return "NO"
    return None


def _signal_bucket(signal: str | None, side: str | None) -> str:
    if signal is None:
        return "MISSING"
    if signal == "NEUTRAL":
        return "NEUTRAL"
    if side in {"YES", "NO"} and signal == side:
        return "AGREE"
    if side in {"YES", "NO"} and signal == _opposite(side):
        return "CONTRA"
    return "UNKNOWN"


def _flow_bucket(value: float | None, signal: str | None, side: str | None) -> str:
    if value is None:
        return "MISSING"
    if signal is None:
        return "NEUTRAL"
    return _signal_bucket(signal, side)


def _btc_regime_testing_enabled() -> bool:
    return _env_bool("Q15_V3_BTC_REGIME_TESTING_ENABLED", True)


def _btc_regime(row: Mapping[str, Any]) -> str | None:
    regime = str(row.get("btc_regime") or "").upper()
    return regime if regime in {"BULLISH", "BEARISH", "CHOP"} else None


def _btc_regime_side(regime: str | None) -> str | None:
    if regime == "BULLISH":
        return "YES"
    if regime == "BEARISH":
        return "NO"
    return None


def _btc_regime_agreement(row: Mapping[str, Any], side: str | None) -> str | None:
    regime = _btc_regime(row)
    if regime is None or side is None:
        return None
    if regime == "CHOP":
        return "CHOP"
    return "AGREES" if _btc_regime_side(regime) == side else "CONTRADICTS"


def _kalshi_book_signal(row: Mapping[str, Any], deadband: float = 0.05) -> tuple[str | None, float | None]:
    yes = _num(row.get("yes_bid_depth_contracts"))
    no = _num(row.get("no_bid_depth_contracts"))
    if yes is None or no is None or yes + no <= 0:
        return None, None
    imb = (yes - no) / (yes + no)
    if imb >= deadband:
        return "YES", imb
    if imb <= -deadband:
        return "NO", imb
    return "NEUTRAL", imb


def _side_matches(signal: str | None, side: str | None) -> bool:
    return side in {"YES", "NO"} and signal == side


def _side_contra(signal: str | None, side: str | None) -> bool:
    return side in {"YES", "NO"} and signal == _opposite(side)


def _local_confirmation_profile(row: Mapping[str, Any], side: str | None) -> dict[str, Any]:
    spot60 = _flow_side(_num(row.get("spot_depth_trade_net_notional_60s")), _spot_net_notional_60s_min())
    spot15 = _flow_side(_num(row.get("spot_depth_trade_net_notional_15s")), 25.0)
    taker = _flow_side(_num(row.get("kalshi_taker_net_yes_volume_15s")), _kalshi_taker_yes_15s_min())
    top12, top12_value = _depth_signal(row, "coinbase_l2_top_12_imbalance_notional", _top12_deadband())
    top60, top60_value = _depth_signal(row, "coinbase_l2_top_60_imbalance_notional", _top60_deadband())
    top250, top250_value = _depth_signal(row, "coinbase_l2_top_250_imbalance_notional", _top250_deadband())
    kraken, kraken_value = _depth_signal(row, "kraken_l3_depth_imbalance", _kraken_l3_deadband())
    kalshi_book, kalshi_book_value = _kalshi_book_signal(row)
    signals = {
        "spot15": spot15,
        "spot60": spot60,
        "kalshi_taker": taker,
        "coinbase_top12": top12,
        "coinbase_top60": top60,
        "coinbase_top250": top250,
        "kraken_l3": kraken,
        "kalshi_book": kalshi_book,
    }
    confirmations = [name for name, signal in signals.items() if _side_matches(signal, side)]
    contradictions = [name for name, signal in signals.items() if _side_contra(signal, side)]
    return {
        "local_confirmation_score": len(confirmations),
        "local_contradiction_score": len(contradictions),
        "local_confirmations": confirmations,
        "local_contradictions": contradictions,
        "local_strong": len(confirmations) >= 3 and len(contradictions) <= 1,
        "local_signals": signals,
        "coinbase_top12_imbalance": top12_value,
        "coinbase_top60_imbalance": top60_value,
        "coinbase_top250_imbalance": top250_value,
        "kraken_l3_imbalance": kraken_value,
        "kalshi_book_imbalance": kalshi_book_value,
    }


def _coinbase_top12_signal(row: Mapping[str, Any]) -> tuple[str | None, float | None, str | None]:
    status = str(row.get("coinbase_l2_status") or "").lower()
    if status and status != "ok":
        return None, None, f"COINBASE_L2_STATUS_{status.upper()}"
    value = _num(row.get("coinbase_l2_top_12_imbalance_notional"))
    if value is None:
        return None, None, "COINBASE_L2_TOP12_MISSING"
    deadband = _top12_deadband()
    if value >= deadband:
        return "YES", value, None
    if value <= -deadband:
        return "NO", value, None
    return "NEUTRAL", value, None


def _apply_top12_confirmation(
    *,
    tier: str,
    side: str,
    base_status: str,
    base_reasons: tuple[str, ...],
    base_profile: Mapping[str, Any],
    row: Mapping[str, Any],
) -> tuple[str, tuple[str, ...], dict[str, Any]]:
    signal, imbalance, missing_reason = _coinbase_top12_signal(row)
    profile = {
        **base_profile,
        "coinbase_top12_deadband": _top12_deadband(),
        "coinbase_top12_signal": signal,
        "coinbase_top12_imbalance_notional": imbalance,
        "coinbase_top12_missing_reason": missing_reason,
        "coinbase_top12_contra_reject_enabled": _top12_contra_reject_enabled(),
        "coinbase_top12_required_for_accept": _top12_required_enabled(),
    }
    reasons = list(base_reasons)
    if missing_reason:
        reasons.append(f"V3_TIER_{tier}_TOP12_NOT_CONFIRMED_{missing_reason}")
        if base_status == ACCEPTED and tier in {TIER_A, TIER_B} and _top12_required_enabled():
            reasons.append(f"V3_TIER_{tier}_RESEARCH_ONLY_TOP12_MISSING")
            return RESEARCH_ONLY, tuple(reasons), profile
        return base_status, tuple(reasons), profile
    if signal == "NEUTRAL":
        reasons.append(f"V3_TIER_{tier}_TOP12_NEUTRAL")
        if base_status == ACCEPTED and tier in {TIER_A, TIER_B} and _top12_required_enabled():
            reasons.append(f"V3_TIER_{tier}_RESEARCH_ONLY_TOP12_NEUTRAL")
            return RESEARCH_ONLY, tuple(reasons), profile
        return base_status, tuple(reasons), profile
    if signal == side:
        reasons.append(f"V3_TIER_{tier}_CONFIRMED_COINBASE_TOP12_{side}")
        return base_status, tuple(reasons), profile
    reasons.append(f"V3_TIER_{tier}_CONTRADICTED_BY_COINBASE_TOP12_{signal}")
    if tier == TIER_C:
        reasons.append(f"V3_TIER_{tier}_RESEARCH_ONLY_TOP12_CONTRA")
        return base_status, tuple(reasons), profile
    if _top12_contra_reject_enabled():
        reasons.append(f"V3_TIER_{tier}_REJECTED_BY_COINBASE_TOP12_CONTRA")
        return REJECTED, tuple(reasons), profile
    return base_status, tuple(reasons), profile


def _apply_combined_contra_veto(
    *,
    tier: str,
    side: str,
    base_status: str,
    base_reasons: tuple[str, ...],
    base_profile: Mapping[str, Any],
    row: Mapping[str, Any],
) -> tuple[str, tuple[str, ...], dict[str, Any]]:
    profile = {
        **base_profile,
        "combined_contra_reject_enabled": _combined_contra_reject_enabled(),
        "coinbase_top250_deadband": _top250_deadband(),
        "kraken_l3_deadband": _kraken_l3_deadband(),
        "kraken_l3_hard_veto_enabled": _kraken_l3_hard_veto_enabled(),
        "spot_net_notional_60s_contra_min": _spot_net_notional_60s_min(),
        "kalshi_taker_yes_15s_contra_min": _kalshi_taker_yes_15s_min(),
    }
    reasons = list(base_reasons)
    contra: list[str] = []
    confirm: list[str] = []

    top250_signal, top250_value = _depth_signal(
        row,
        "coinbase_l2_top_250_imbalance_notional",
        _top250_deadband(),
    )
    profile["coinbase_top250_signal"] = top250_signal
    profile["coinbase_top250_imbalance_notional"] = top250_value
    if top250_signal == side:
        confirm.append(f"V3_TIER_{tier}_CONFIRMED_COINBASE_TOP250_{side}")
    elif top250_signal == _opposite(side):
        contra.append(f"V3_TIER_{tier}_VETO_COINBASE_TOP250_{top250_signal}")

    spot_flow = _num(row.get("spot_depth_trade_net_notional_60s"))
    spot_flow_side = _flow_side(spot_flow, _spot_net_notional_60s_min())
    profile["spot_net_notional_60s_side"] = spot_flow_side
    profile["spot_net_notional_60s"] = spot_flow
    if spot_flow_side == side:
        confirm.append(f"V3_TIER_{tier}_CONFIRMED_SPOT_FLOW_60S_{side}")
    elif spot_flow_side == _opposite(side):
        contra.append(f"V3_TIER_{tier}_VETO_SPOT_FLOW_60S_{spot_flow_side}")

    taker_yes = _num(row.get("kalshi_taker_net_yes_volume_15s"))
    taker_side = _flow_side(taker_yes, _kalshi_taker_yes_15s_min())
    profile["kalshi_taker_net_yes_15s_side"] = taker_side
    profile["kalshi_taker_net_yes_volume_15s"] = taker_yes
    if taker_side == side:
        confirm.append(f"V3_TIER_{tier}_CONFIRMED_KALSHI_TAKER_15S_{side}")
    elif taker_side == _opposite(side):
        contra.append(f"V3_TIER_{tier}_VETO_KALSHI_TAKER_15S_{taker_side}")

    kraken_signal, kraken_value = _depth_signal(
        row,
        "kraken_l3_depth_imbalance",
        _kraken_l3_deadband(),
    )
    profile["kraken_l3_signal"] = kraken_signal
    profile["kraken_l3_depth_imbalance"] = kraken_value
    if kraken_signal == side:
        confirm.append(f"V3_TIER_{tier}_CONFIRMED_KRAKEN_L3_{side}")
    elif kraken_signal == _opposite(side):
        code = f"V3_TIER_{tier}_WARN_KRAKEN_L3_{kraken_signal}"
        if _kraken_l3_hard_veto_enabled():
            code = f"V3_TIER_{tier}_VETO_KRAKEN_L3_{kraken_signal}"
            contra.append(code)
        else:
            reasons.append(code)

    reasons.extend(confirm)
    if contra:
        reasons.extend(contra)
        reasons.append(f"V3_TIER_{tier}_REJECTED_BY_COMBINED_CONTRA")
        if _combined_contra_reject_enabled():
            return REJECTED, tuple(reasons), profile
    return base_status, tuple(reasons), profile


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
            status, reasons, profile = _apply_top12_confirmation(
                tier=TIER_A,
                side=side,
                base_status=ACCEPTED,
                base_reasons=("V3_TIER_A_STRICT_7_HIGH_CONFIDENCE", reason),
                base_profile={**base_profile, "tier": TIER_A},
                row=row,
            )
            if status == ACCEPTED:
                status, reasons, profile = _apply_combined_contra_veto(
                    tier=TIER_A,
                    side=side,
                    base_status=status,
                    base_reasons=reasons,
                    base_profile=profile,
                    row=row,
                )
            return BotDecision(
                BOT_CONFIDENCE_TIER,
                status,
                reasons,
                tier=TIER_A,
                threshold_profile=profile,
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
            status, reasons, profile = _apply_top12_confirmation(
                tier=TIER_B,
                side=side,
                base_status=ACCEPTED,
                base_reasons=("V3_TIER_B_VOLUME_EXPANSION", reason),
                base_profile={**base_profile, "tier": TIER_B},
                row=row,
            )
            if status == ACCEPTED:
                status, reasons, profile = _apply_combined_contra_veto(
                    tier=TIER_B,
                    side=side,
                    base_status=status,
                    base_reasons=reasons,
                    base_profile=profile,
                    row=row,
                )
            return BotDecision(
                BOT_CONFIDENCE_TIER,
                status,
                reasons,
                tier=TIER_B,
                threshold_profile=profile,
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
            status, reasons, profile = _apply_top12_confirmation(
                tier=TIER_C,
                side=side,
                base_status=RESEARCH_ONLY,
                base_reasons=("V3_TIER_C_RESEARCH_ONLY", reason),
                base_profile={**base_profile, "tier": TIER_C, "research_only": True},
                row=row,
            )
            return BotDecision(
                BOT_CONFIDENCE_TIER,
                status,
                reasons,
                tier=TIER_C,
                threshold_profile=profile,
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
        "positive_ev_gate_enabled": _v3_positive_ev_gate_enabled(),
        "hype_yes_accept_override_enabled": _v3_hype_yes_accept_enabled(),
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
        accepted_reasons = spot_reasons + kalshi_reasons
        if _v3_positive_ev_gate_enabled() and not _v3_hype_yes_accept_enabled():
            return BotDecision(
                BOT_HYPE_YES,
                RESEARCH_ONLY,
                tuple(accepted_reasons + ["V3_POSITIVE_EV_GATE_HYPE_YES_RESEARCH_ONLY"]),
                threshold_profile=thresholds,
            )
        if _v3_positive_ev_gate_enabled():
            accepted_reasons.append("V3_POSITIVE_EV_GATE_HYPE_YES_ALLOWED_BY_OVERRIDE")
        return BotDecision(
            BOT_HYPE_YES,
            ACCEPTED,
            tuple(accepted_reasons),
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
        "positive_ev_gate_enabled": _v3_positive_ev_gate_enabled(),
        "morefire_accept_override_enabled": _v3_morefire_accept_enabled(),
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
    local = _local_confirmation_profile(row, "YES")
    local_contra = int(local.get("local_contradiction_score") or 0) >= 2

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
        reasons.append("BTC_DOMINANT_SIDE_NO_WARNING")
        if local_contra:
            contra = True
            reasons.append("BTC_DOMINANT_SIDE_NO_WITH_LOCAL_CONTRA")
    if model_side == "NO" and (
        (cal_yes is not None and cal_yes < 0.48)
        or (model_yes is not None and model_yes < 0.48)
        or (market_yes is not None and market_yes < 0.48)
    ):
        reasons.append("BTC_MODEL_MARKET_CONTRA_WARNING")
        if local_contra:
            contra = True
            reasons.append("BTC_MODEL_MARKET_CONTRA_WITH_LOCAL_CONTRA")
    if local_contra:
        reasons.append("LOCAL_DEPTH_FLOW_CONTRA")

    if depth is not None and depth >= 1225.0 and pressure is not None and pressure >= 0.0 and not contra:
        if _v3_positive_ev_gate_enabled() and not _v3_morefire_accept_enabled():
            return BotDecision(
                BOT_MOREFIRE_BTC,
                RESEARCH_ONLY,
                tuple(reasons + ["V3_POSITIVE_EV_GATE_MOREFIRE_RESEARCH_ONLY"]),
                threshold_profile=thresholds,
                btc_context=ctx,
            )
        if _v3_positive_ev_gate_enabled():
            reasons.append("V3_POSITIVE_EV_GATE_MOREFIRE_ALLOWED_BY_OVERRIDE")
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


def hvf_depth_flow_wrapper_decision(
    row: Mapping[str, Any],
    *,
    source_system: str,
) -> BotDecision | None:
    """Primary V3 alert-owner for HVF rows.

    HVF stays as the source generator/control ledger. This wrapper decides what
    reaches the V3 Telegram channel and what remains shadow/research tracking.
    """
    if not _hvf_wrapper_enabled() or source_system != "high_vol_flip":
        return None
    rule = source_rule(row)
    kind = str(row.get("record_kind") or "")
    side = source_side(row)
    asset = _asset(row)
    interval = str(row.get("interval") or "").upper()
    thresholds = {
        "spot_net_notional_60s_contra_min": _spot_net_notional_60s_min(),
        "kalshi_taker_yes_15s_crowd_min": _kalshi_taker_yes_15s_min(),
        "coinbase_top12_deadband": _top12_deadband(),
        "coinbase_top60_deadband": _top60_deadband(),
        "coinbase_top250_deadband": _top250_deadband(),
        "morefire_l2_alignment_enabled": _hvf_morefire_l2_alignment_enabled(),
        "own_strong_top12_recovery_enabled": _hvf_own_strong_top12_recovery_enabled(),
        "own_strong_top12_recovery_entry_max": _hvf_own_strong_recovery_entry_max(),
        "morefire_missing_required_data_research_only": _hvf_wrapper_research_on_missing(),
        "own_strong_spot60_contra_research_only": True,
        "top_depth_mode": "top250_hard_morefire_top12_recovery_own_strong",
        "positive_ev_gate_enabled": _v3_positive_ev_gate_enabled(),
        "morefire_accept_override_enabled": _v3_morefire_accept_enabled(),
        "own_strong_interval_gate_enabled": _v3_hvf_own_strong_interval_gate_enabled(),
        "own_strong_repair_buckets_enabled": True,
        "own_strong_repeat_window_bucket_available": False,
        "provisional": True,
        "paper_only": True,
    }
    base_profile = {
        **thresholds,
        "source_rule": rule,
        "record_kind": kind,
        "side": side,
        "interval": interval,
    }
    if side not in {"YES", "NO"}:
        return BotDecision(
            BOT_HVF_DEPTH_FLOW,
            REJECTED,
            ("HVF_WRAPPER_INVALID_SIDE",),
            threshold_profile=base_profile,
        )

    spot60 = _num(row.get("spot_depth_trade_net_notional_60s"))
    spot15 = _num(row.get("spot_depth_trade_net_notional_15s"))
    taker_net_yes = _num(row.get("kalshi_taker_net_yes_volume_15s"))
    selected_ratio = _num(row.get("selected_depth_ratio"))
    spot60_side = _flow_side(spot60, _spot_net_notional_60s_min())
    spot15_side = _flow_side(spot15, 25.0)
    taker_side = _flow_side(taker_net_yes, _kalshi_taker_yes_15s_min())
    top12_signal, top12_imbalance = _depth_signal(
        row,
        "coinbase_l2_top_12_imbalance_notional",
        _top12_deadband(),
    )
    top60_signal, top60_imbalance = _depth_signal(
        row,
        "coinbase_l2_top_60_imbalance_notional",
        _top60_deadband(),
    )
    top250_signal, top250_imbalance = _depth_signal(
        row,
        "coinbase_l2_top_250_imbalance_notional",
        _top250_deadband(),
    )
    spot_imb_signal, spot_imbalance = _depth_signal(
        row,
        "spot_depth_imbalance",
        _hvf_yes_spot_imbalance_deadband(),
    )
    kalshi_book_signal, kalshi_book_imbalance = _kalshi_book_signal(row)
    profile = {
        **base_profile,
        "spot_net_notional_60s": spot60,
        "spot_net_notional_60s_side": spot60_side,
        "spot_net_notional_15s": spot15,
        "spot_net_notional_15s_side": spot15_side,
        "kalshi_taker_net_yes_volume_15s": taker_net_yes,
        "kalshi_taker_net_yes_15s_side": taker_side,
        "selected_depth_ratio": selected_ratio,
        "coinbase_top12_signal": top12_signal,
        "coinbase_top12_imbalance_notional": top12_imbalance,
        "coinbase_top60_signal": top60_signal,
        "coinbase_top60_imbalance_notional": top60_imbalance,
        "coinbase_top250_signal": top250_signal,
        "coinbase_top250_imbalance_notional": top250_imbalance,
        "spot_depth_imbalance_signal": spot_imb_signal,
        "spot_depth_imbalance_value": spot_imbalance,
        "kalshi_book_signal": kalshi_book_signal,
        "kalshi_book_imbalance": kalshi_book_imbalance,
    }

    audit_veto_reasons: list[str] = []
    if side == "YES" and _hvf_yes_contra_veto_enabled():
        if spot60_side == "NO":
            audit_veto_reasons.append("HVF_WRAPPER_YES_SPOT60_CONTRA_VETO")
        if spot_imb_signal == "NO":
            audit_veto_reasons.append("HVF_WRAPPER_YES_SPOT_IMB_CONTRA_VETO")
        if kalshi_book_signal == "NO":
            audit_veto_reasons.append("HVF_WRAPPER_YES_KALSHI_BOOK_CONTRA_VETO")
        if taker_side == "YES":
            audit_veto_reasons.append("HVF_WRAPPER_YES_TAKER_CROWD_VETO")

    def _hvf_decision(status: str, reasons: list[str] | tuple[str, ...]) -> BotDecision:
        final_status = status
        final_reasons = list(reasons)
        if status == ACCEPTED and audit_veto_reasons:
            final_status = RESEARCH_ONLY
            final_reasons.extend(["HVF_WRAPPER_YES_AUDIT_VETO", *audit_veto_reasons])
        return BotDecision(
            BOT_HVF_DEPTH_FLOW,
            final_status,
            tuple(dict.fromkeys(final_reasons)),
            threshold_profile=profile,
        )

    is_morefire = rule == "HVF_MORE_FIRE_STRICT" or kind == "MORE_FIRE_STRICT_ALERT"
    if is_morefire:
        reasons = ["HVF_WRAPPER_MOREFIRE_DEPTH_FLOW_EVAL"]
        rejected = False
        if side != "YES":
            return BotDecision(
                BOT_HVF_DEPTH_FLOW,
                REJECTED,
                ("HVF_WRAPPER_MOREFIRE_SIDE_NOT_YES",),
                threshold_profile=profile,
            )
        if spot60_side == "NO":
            reasons.append("HVF_WRAPPER_MOREFIRE_REJECT_SPOT60_NO")
            rejected = True
        if taker_side == "YES":
            reasons.append("HVF_WRAPPER_MOREFIRE_WARN_KALSHI_TAKER_YES_CROWD")
        elif taker_side == "NO":
            reasons.append("HVF_WRAPPER_MOREFIRE_WARN_KALSHI_TAKER_NO_FLOW")
        if _hvf_morefire_l2_alignment_enabled():
            if top250_signal is None:
                reasons.append("HVF_WRAPPER_MOREFIRE_MISSING_COINBASE_TOP250")
            elif top250_signal == _opposite(side):
                reasons.append(f"HVF_WRAPPER_MOREFIRE_REJECT_COINBASE_TOP250_{top250_signal}")
                rejected = True
            if top12_signal is None:
                reasons.append("HVF_WRAPPER_MOREFIRE_MISSING_COINBASE_TOP12")
            elif top12_signal == _opposite(side):
                reasons.append(f"HVF_WRAPPER_MOREFIRE_RESEARCH_COINBASE_TOP12_{top12_signal}")
            if top60_signal is None:
                reasons.append("HVF_WRAPPER_MOREFIRE_MISSING_COINBASE_TOP60")
            elif top60_signal == _opposite(side):
                reasons.append(f"HVF_WRAPPER_MOREFIRE_RESEARCH_COINBASE_TOP60_{top60_signal}")
        if rejected:
            return BotDecision(
                BOT_HVF_DEPTH_FLOW,
                REJECTED,
                tuple(reasons),
                threshold_profile=profile,
            )

        missing: list[str] = []
        if selected_ratio is None:
            missing.append("HVF_WRAPPER_MOREFIRE_MISSING_SELECTED_DEPTH_RATIO")
        if spot60 is None:
            missing.append("HVF_WRAPPER_MOREFIRE_MISSING_SPOT60_FLOW")
        if taker_net_yes is None:
            missing.append("HVF_WRAPPER_MOREFIRE_MISSING_KALSHI_TAKER_FLOW")
        if _hvf_morefire_l2_alignment_enabled():
            if top250_signal is None:
                missing.append("HVF_WRAPPER_MOREFIRE_MISSING_TOP250_ALIGNMENT")
            if top12_signal is None:
                missing.append("HVF_WRAPPER_MOREFIRE_MISSING_TOP12_ALIGNMENT")
            if top60_signal is None:
                missing.append("HVF_WRAPPER_MOREFIRE_MISSING_TOP60_ALIGNMENT")
            if top12_signal == _opposite(side) or top60_signal == _opposite(side):
                return BotDecision(
                    BOT_HVF_DEPTH_FLOW,
                    RESEARCH_ONLY,
                    tuple(reasons + ["HVF_WRAPPER_MOREFIRE_RESEARCH_ONLY_SHALLOW_L2_CONTRA"]),
                    threshold_profile=profile,
                )
        if missing and _hvf_wrapper_research_on_missing():
            return BotDecision(
                BOT_HVF_DEPTH_FLOW,
                RESEARCH_ONLY,
                tuple(reasons + missing + ["HVF_WRAPPER_MOREFIRE_RESEARCH_ONLY_MISSING_DATA"]),
                threshold_profile=profile,
            )
        if _v3_positive_ev_gate_enabled() and not _v3_morefire_accept_enabled():
            return BotDecision(
                BOT_HVF_DEPTH_FLOW,
                RESEARCH_ONLY,
                tuple(reasons + ["V3_POSITIVE_EV_GATE_MOREFIRE_RESEARCH_ONLY"]),
                threshold_profile=profile,
            )
        if _v3_positive_ev_gate_enabled():
            reasons.append("V3_POSITIVE_EV_GATE_MOREFIRE_ALLOWED_BY_OVERRIDE")
        return _hvf_decision(
            ACCEPTED,
            reasons + ["HVF_WRAPPER_MOREFIRE_ACCEPT_NO_SPOT_TAKER_CONTRA"],
        )

    if rule == "HVF_OWN_STRONG_SELECTED":
        reasons = ["HVF_WRAPPER_OWN_STRONG_SELECTED_EVAL"]
        if _v3_positive_ev_gate_enabled() and _v3_hvf_own_strong_interval_gate_enabled():
            interval_gate_reason: str | None = None
            if asset == "ETH" and interval == "12M":
                interval_gate_reason = "V3_POSITIVE_EV_GATE_HVF_OWN_STRONG_ETH_12M_RESEARCH_ONLY"
            elif asset == "SOL" and interval == "12M" and side == "NO":
                interval_gate_reason = "V3_POSITIVE_EV_GATE_HVF_OWN_STRONG_SOL_12M_NO_RESEARCH_ONLY"
            if interval_gate_reason is not None:
                background_reasons = [interval_gate_reason, "V3_HVF_OWN_STRONG_BACKGROUND_RESEARCH_ONLY"]
                if asset == "ETH" and interval == "12M":
                    background_reasons.extend(
                        [
                            f"HVF_REPAIR_ETH_12M_SPOT60_{_flow_bucket(spot60, spot60_side, side)}",
                            f"HVF_REPAIR_ETH_12M_SPOT15_{_flow_bucket(spot15, spot15_side, side)}",
                        ]
                    )
                elif asset == "SOL" and interval == "12M" and side == "NO":
                    taker_bucket = _flow_bucket(taker_net_yes, taker_side, side)
                    if taker_bucket == "CONTRA":
                        background_reasons.append("HVF_REPAIR_SOL_12M_NO_TAKER_CONTRA")
                    elif taker_bucket in {"AGREE", "NEUTRAL"}:
                        background_reasons.extend(
                            [
                                "HVF_REPAIR_SOL_12M_NO_TAKER_NOT_CONTRA",
                                f"HVF_REPAIR_SOL_12M_NO_TAKER_{taker_bucket}",
                            ]
                        )
                    else:
                        background_reasons.append(f"HVF_REPAIR_SOL_12M_NO_TAKER_{taker_bucket}")
                    background_reasons.append("HVF_REPAIR_SOL_12M_NO_REPEAT_WINDOW_UNAVAILABLE")
                return BotDecision(
                    BOT_HVF_DEPTH_FLOW,
                    RESEARCH_ONLY,
                    tuple(reasons + background_reasons),
                    threshold_profile=profile,
                )
        if spot60_side == _opposite(side):
            ask = _entry_ask(row)
            if (
                _hvf_own_strong_top12_recovery_enabled()
                and top12_signal == side
                and ask is not None
                and ask <= _hvf_own_strong_recovery_entry_max()
            ):
                recovery_reasons = reasons + [
                    f"HVF_WRAPPER_OWN_STRONG_SPOT60_CONTRA_{spot60_side}",
                    f"HVF_WRAPPER_OWN_STRONG_ACCEPT_TOP12_RECOVERY_{side}",
                ]
                if _v3_positive_ev_gate_enabled():
                    recovery_reasons.append("V3_POSITIVE_EV_GATE_HVF_OWN_STRONG_ALLOWED")
                return _hvf_decision(ACCEPTED, recovery_reasons)
            recovery_reason = "HVF_WRAPPER_OWN_STRONG_TOP12_RECOVERY_NOT_CONFIRMED"
            if ask is not None and ask > _hvf_own_strong_recovery_entry_max():
                recovery_reason = "HVF_WRAPPER_OWN_STRONG_TOP12_RECOVERY_ENTRY_TOO_EXPENSIVE"
            elif top12_signal is None:
                recovery_reason = "HVF_WRAPPER_OWN_STRONG_TOP12_RECOVERY_TOP12_MISSING"
            elif top12_signal != side:
                recovery_reason = f"HVF_WRAPPER_OWN_STRONG_TOP12_RECOVERY_TOP12_{top12_signal}"
            return BotDecision(
                BOT_HVF_DEPTH_FLOW,
                RESEARCH_ONLY,
                tuple(
                    reasons
                    + [
                        f"HVF_WRAPPER_OWN_STRONG_RESEARCH_SPOT60_CONTRA_{spot60_side}",
                        recovery_reason,
                    ]
                ),
                threshold_profile=profile,
            )
        if taker_side == _opposite(side):
            return BotDecision(
                BOT_HVF_DEPTH_FLOW,
                RESEARCH_ONLY,
                tuple(reasons + [f"HVF_WRAPPER_OWN_STRONG_RESEARCH_TAKER_CONTRA_{taker_side}"]),
                threshold_profile=profile,
            )
        if _v3_positive_ev_gate_enabled():
            reasons.append("V3_POSITIVE_EV_GATE_HVF_OWN_STRONG_ALLOWED")
        return _hvf_decision(
            ACCEPTED,
            reasons + ["HVF_WRAPPER_OWN_STRONG_ACCEPT_SOURCE_STRENGTH"],
        )

    if rule == "HVF_OWN_NO_FLASH":
        if _v3_positive_ev_gate_enabled() and asset == "XRP":
            return BotDecision(
                BOT_HVF_DEPTH_FLOW,
                RESEARCH_ONLY,
                (
                    "HVF_WRAPPER_OWN_NO_FLASH_EVAL",
                    "V3_POSITIVE_EV_GATE_HVF_XRP_NO_FLASH_RESEARCH_ONLY",
                ),
                threshold_profile=profile,
            )
        reasons = ["HVF_WRAPPER_OWN_NO_FLASH_ACCEPT_NO_HARD_DEPTH_VETO"]
        if _v3_positive_ev_gate_enabled():
            reasons.append("V3_POSITIVE_EV_GATE_HVF_OWN_NO_FLASH_ALLOWED")
        return _hvf_decision(ACCEPTED, reasons)

    if rule == "HVF_BTC_FOLLOW_EXTREME":
        if side == "NO":
            return _hvf_decision(ACCEPTED, ("HVF_WRAPPER_BTC_FOLLOW_NO_ACCEPT_PROVISIONAL",))
        return BotDecision(
            BOT_HVF_DEPTH_FLOW,
            RESEARCH_ONLY,
            ("HVF_WRAPPER_BTC_FOLLOW_YES_RESEARCH_WEAK_BASELINE",),
            threshold_profile=profile,
        )

    if rule in {
        "HVF_OWN_EARLY_FLIP",
        "HVF_HYPE_BULLISH_FLASH",
        "HVF_HYPE_EARLY_BULLISH_FLIP",
        "HVF_BTC_DIVERGENCE_ACCEL_WATCH",
    }:
        return BotDecision(
            BOT_HVF_DEPTH_FLOW,
            RESEARCH_ONLY,
            (f"HVF_WRAPPER_{rule}_RESEARCH_SMALL_SAMPLE",),
            threshold_profile=profile,
        )

    return BotDecision(
        BOT_HVF_DEPTH_FLOW,
        RESEARCH_ONLY,
        ("HVF_WRAPPER_UNKNOWN_RULE_RESEARCH_ONLY",),
        threshold_profile=profile,
    )


def btc_regime_context_probe_decision(
    row: Mapping[str, Any],
    *,
    source_system: str,
) -> BotDecision | None:
    """Record-only BTC lead-market probe for alt rows.

    This intentionally never returns ACCEPTED/REJECTED. It labels what the BTC
    regime would have done so we can collect out-of-sample evidence before
    changing live alert routing.
    """
    if not _btc_regime_testing_enabled() or _asset(row) == "BTC":
        return None
    side = source_side(row)
    if side not in {"YES", "NO"}:
        return None
    regime = _btc_regime(row)
    agreement = _btc_regime_agreement(row, side)
    local = _local_confirmation_profile(row, side)
    local_strong = bool(local.get("local_strong"))
    asset = _asset(row)
    rule = source_rule(row)
    reasons: list[str] = ["BTC_REGIME_PROBE_RESEARCH_ONLY"]
    profile = {
        "paper_only": True,
        "provisional": True,
        "source_system": source_system,
        "source_rule": rule,
        "side": side,
        "asset": asset,
        "btc_regime": regime,
        "btc_regime_agreement": agreement,
        "btc_regime_vote_yes": _num(row.get("btc_regime_vote_yes")),
        "btc_regime_vote_no": _num(row.get("btc_regime_vote_no")),
        "btc_regime_vote_detail": row.get("btc_regime_vote_detail"),
        **local,
    }
    if regime is None:
        reasons.append("BTC_REGIME_MISSING")
        reasons.append("BTC_REGIME_WOULD_KEEP_RESEARCH_ONLY_MISSING")
    else:
        reasons.append(f"BTC_REGIME_{regime}")
        if agreement:
            reasons.append(f"BTC_REGIME_{agreement}")
        if regime == "CHOP":
            if local_strong:
                reasons.append("BTC_REGIME_CHOP_LOCAL_STRONG_WOULD_KEEP")
            else:
                reasons.append("BTC_REGIME_CHOP_WOULD_DOWNGRADE_RESEARCH_ONLY")
        elif agreement == "AGREES":
            reasons.append("BTC_REGIME_WOULD_BOOST")
        elif agreement == "CONTRADICTS":
            if asset in {"ETH", "HYPE"}:
                reasons.append(f"BTC_REGIME_{asset}_CONTRA_WARNING_ONLY")
            elif asset == "DOGE":
                reasons.append("BTC_REGIME_DOGE_CONTRA_WOULD_DOWNGRADE_RESEARCH_ONLY")
            elif asset == "BNB":
                reasons.append("BTC_REGIME_BNB_CONTRA_NO_HARD_VETO_KEEP_BNB_LOCAL_RULES_DOMINANT")
            else:
                reasons.append("BTC_REGIME_CONTRA_WOULD_WARN")

    if asset == "DOGE":
        if regime == "CHOP":
            reasons.append("BTC_REGIME_DOGE_REQUIRES_NON_CHOP_NOT_MET")
        elif agreement == "AGREES":
            reasons.append("BTC_REGIME_DOGE_PREFERRED_AGREEMENT_MET")
        elif agreement == "CONTRADICTS":
            reasons.append("BTC_REGIME_DOGE_PREFERRED_AGREEMENT_NOT_MET")
    elif asset == "BNB":
        if agreement == "AGREES":
            reasons.append("BTC_REGIME_BNB_AGREEMENT_BOOST_ONLY")
        reasons.append("BTC_REGIME_BNB_SPOT_KALSHI_VETO_REMAINS_PRIMARY")
    elif asset in {"ETH", "HYPE"} and agreement == "CONTRADICTS":
        reasons.append(f"BTC_REGIME_{asset}_NO_HARD_VETO")

    if rule == "HVF_MORE_FIRE_STRICT":
        local_contra = int(local.get("local_contradiction_score") or 0) >= 2
        if agreement == "CONTRADICTS" and local_contra:
            reasons.append("BTC_REGIME_MOREFIRE_CONTRA_WITH_LOCAL_CONTRA_WOULD_DOWNGRADE")
        elif agreement == "CONTRADICTS":
            reasons.append("BTC_REGIME_MOREFIRE_CONTRA_WARNING_ONLY")
        else:
            reasons.append("BTC_REGIME_MOREFIRE_BTC_AGREEMENT_NOT_MANDATORY_IN_TEST")

    return BotDecision(
        BOT_BTC_REGIME,
        RESEARCH_ONLY,
        tuple(dict.fromkeys(reasons)),
        threshold_profile=profile,
        btc_context={key: row.get(key) for key in BTC_REGIME_KEYS if row.get(key) is not None},
    )


def depth_formula_15m_research_decision(row: Mapping[str, Any]) -> BotDecision | None:
    """Research-only replay of the measured 15M NO depth-support formula."""
    if not _depth_formula_research_enabled():
        return None
    interval = str(row.get("interval") or "").upper()
    if interval != "15M":
        return None
    side = source_side(row)
    ask = _entry_ask(row)
    spread = _num(row.get("spread_cents"))
    selected_ask_depth = _num(row.get("depth_contracts"))
    selected_bid_depth = _selected_bid_depth(row, side)
    ratio = None
    if selected_ask_depth is not None and selected_ask_depth > 0 and selected_bid_depth is not None:
        ratio = selected_bid_depth / selected_ask_depth
    thresholds = {
        "paper_only": True,
        "research_only": True,
        "data_source": "q15_strategy_bots_v3 historical 15M depth/PnL scan",
        "observed_formula_pnl_cents": 2489.0,
        "observed_formula_rows": 141,
        "observed_formula_win_rate_pct": 70.2127659574468,
        "observed_baseline_pnl_cents": -8769.0,
        "observed_baseline_rows": 907,
        "observed_baseline_win_rate_pct": 49.72436604189636,
        "chronological_train_pnl_cents": 2025.0,
        "chronological_train_rows": 87,
        "chronological_test_pnl_cents": 464.0,
        "chronological_test_rows": 54,
        "side_required": "NO",
        "interval_required": "15M",
        "spread_cents_lt": _depth_formula_spread_max(),
        "entry_ask_cents_lt": _depth_formula_entry_ask_max(),
        "selected_ask_depth_lt": _depth_formula_selected_ask_depth_max(),
        "selected_bid_to_ask_depth_ratio_gte": _depth_formula_bid_ask_ratio_min(),
        "side": side,
        "entry_ask_cents": ask,
        "spread_cents": spread,
        "selected_ask_depth": selected_ask_depth,
        "selected_bid_depth": selected_bid_depth,
        "selected_bid_to_ask_depth_ratio": ratio,
    }
    reasons: list[str] = ["V3_15M_DEPTH_FORMULA_RESEARCH_EVAL"]
    failures: list[str] = []
    if side != "NO":
        failures.append("V3_15M_DEPTH_FORMULA_SIDE_NOT_NO")
    if ask is None:
        failures.append("V3_15M_DEPTH_FORMULA_ENTRY_ASK_MISSING")
    elif ask >= _depth_formula_entry_ask_max():
        failures.append("V3_15M_DEPTH_FORMULA_ENTRY_ASK_TOO_HIGH")
    if spread is None:
        failures.append("V3_15M_DEPTH_FORMULA_SPREAD_MISSING")
    elif spread >= _depth_formula_spread_max():
        failures.append("V3_15M_DEPTH_FORMULA_SPREAD_TOO_WIDE")
    if selected_ask_depth is None:
        failures.append("V3_15M_DEPTH_FORMULA_SELECTED_ASK_DEPTH_MISSING")
    elif selected_ask_depth >= _depth_formula_selected_ask_depth_max():
        failures.append("V3_15M_DEPTH_FORMULA_SELECTED_ASK_DEPTH_TOO_DEEP")
    if selected_bid_depth is None:
        failures.append("V3_15M_DEPTH_FORMULA_SELECTED_BID_DEPTH_MISSING")
    elif ratio is None:
        failures.append("V3_15M_DEPTH_FORMULA_DEPTH_RATIO_UNAVAILABLE")
    elif ratio < _depth_formula_bid_ask_ratio_min():
        failures.append("V3_15M_DEPTH_FORMULA_BID_ASK_RATIO_TOO_LOW")
    if failures:
        return BotDecision(
            BOT_DEPTH_FORMULA_15M,
            REJECTED,
            tuple(reasons + failures),
            threshold_profile={**thresholds, "passed": False},
        )
    return BotDecision(
        BOT_DEPTH_FORMULA_15M,
        RESEARCH_ONLY,
        tuple(
            reasons
            + [
                "V3_15M_DEPTH_FORMULA_NO_SIDE",
                "V3_15M_DEPTH_FORMULA_SPREAD_LT_10",
                "V3_15M_DEPTH_FORMULA_ENTRY_ASK_LT_55",
                "V3_15M_DEPTH_FORMULA_SELECTED_ASK_DEPTH_LT_50",
                "V3_15M_DEPTH_FORMULA_BID_ASK_RATIO_GTE_0_25",
            ]
        ),
        threshold_profile={**thresholds, "passed": True},
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
    depth_formula = depth_formula_15m_research_decision(row)
    if depth_formula is not None:
        decisions.append(depth_formula)
    btc_probe = btc_regime_context_probe_decision(row, source_system=source_system)
    if btc_probe is not None:
        decisions.append(btc_probe)
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
        hvf_wrapper = hvf_depth_flow_wrapper_decision(row, source_system=source_system)
        if hvf_wrapper is not None:
            decisions.append(hvf_wrapper)
        morefire = morefire_btc_confirmed_decision(row, btc_context)
        if morefire is not None:
            decisions.append(morefire)
    return decisions
