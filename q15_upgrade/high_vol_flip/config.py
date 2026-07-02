"""High Volatility Flip configuration.

HVF is paper-only and independent from V2's 10M entry picker. It may share the
same Telegram chat for convenience, but every row is written to its own ledger
under the configured HVF model version.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _str(name: str, default: str) -> str:
    return os.environ.get(name, default) or default


def _csv_set(name: str, default: str = "") -> frozenset[str]:
    raw = os.environ.get(name, default) or ""
    return frozenset(part.strip().upper() for part in raw.split(",") if part.strip())


INTERVAL_MARKS: dict[str, int] = {
    "13M": 780,
    "12M": 720,
    "11M": 660,
    "10M": 600,
    "9M": 540,
    "8M": 480,
    "7M": 420,
}


@dataclass(frozen=True)
class HighVolFlipConfig:
    enabled: bool = field(default_factory=lambda: _bool("Q15_HVF_ENABLED", False))
    model_version: str = field(
        default_factory=lambda: _str("Q15_HVF_MODEL_VERSION", "high-vol-flip-v3-confirmed")
    )
    db_path: str = field(
        default_factory=lambda: _str("Q15_HVF_DB", "data/q15_high_vol_flip_v1.sqlite3")
    )
    telegram_enabled: bool = field(
        default_factory=lambda: _bool("Q15_HVF_TELEGRAM_ENABLED", False)
    )
    alert_telegram_enabled: bool = field(
        default_factory=lambda: _bool("Q15_HVF_ALERT_TELEGRAM_ENABLED", False)
    )
    suppress_bnb_telegram_for_v3: bool = field(
        default_factory=lambda: _bool("Q15_V3_SUPPRESS_OLD_BNB_NOTIFICATIONS", False)
    )
    # User requested the existing V2 Telegram room. A dedicated HVF chat can still
    # override it later without changing code.
    telegram_chat_id: str = field(
        default_factory=lambda: (
            os.environ.get("Q15_HVF_TELEGRAM_CHAT_ID")
            or os.environ.get("Q15_ULTOIM_V2_TELEGRAM_CHAT_ID")
            or os.environ.get("TELEGRAM_CHAT_ID")
            or ""
        )
    )
    assets: frozenset[str] = field(
        default_factory=lambda: _csv_set("Q15_HVF_ASSETS", "XRP,SOL,ETH,HYPE,BNB")
    )
    excluded_assets: frozenset[str] = field(
        default_factory=lambda: _csv_set("Q15_HVF_EXCLUDED_ASSETS", "DOGE")
    )
    intervals: frozenset[str] = field(
        default_factory=lambda: _csv_set("Q15_HVF_INTERVALS", "12M,11M,10M,9M,8M,7M")
    )
    mark_band_seconds: float = field(
        default_factory=lambda: _float("Q15_HVF_BAND_SECONDS", 25.0)
    )
    reconcile_every_seconds: float = field(
        default_factory=lambda: _float("Q15_HVF_RECONCILE_EVERY_SECONDS", 30.0)
    )
    paper_only: bool = field(default_factory=lambda: _bool("Q15_HVF_PAPER_ONLY", True))
    min_data_quality: float = field(
        default_factory=lambda: _float("Q15_HVF_MIN_DATA_QUALITY", 0.0)
    )
    max_spread_cents: float = field(
        default_factory=lambda: _float("Q15_HVF_MAX_SPREAD_CENTS", 8.0)
    )
    min_depth_contracts: float = field(
        default_factory=lambda: _float("Q15_HVF_MIN_DEPTH_CONTRACTS", 0.0)
    )
    max_alerts_per_window: int = field(
        default_factory=lambda: max(1, int(_float("Q15_HVF_MAX_ALERTS_PER_WINDOW", 2.0)))
    )

    more_fire_strict_enabled: bool = field(
        default_factory=lambda: _bool("Q15_HVF_MORE_FIRE_STRICT_ENABLED", False)
    )
    # Default OFF: when enabled, the historically negative MORE_FIRE_STRICT rule
    # remains measured as research but never sends Telegram or competes for alert
    # priority/slots.
    mute_more_fire: bool = field(
        default_factory=lambda: _bool("Q15_HVF_MUTE_MORE_FIRE", False)
    )
    more_fire_strict_assets: frozenset[str] = field(
        default_factory=lambda: _csv_set("Q15_HVF_MORE_FIRE_STRICT_ASSETS", "ETH,SOL,XRP")
    )
    more_fire_excluded_assets: frozenset[str] = field(
        default_factory=lambda: _csv_set("Q15_HVF_MORE_FIRE_EXCLUDED_ASSETS", "BNB,HYPE")
    )
    more_fire_strict_intervals: frozenset[str] = field(
        default_factory=lambda: _csv_set("Q15_HVF_MORE_FIRE_STRICT_INTERVALS", "12M,10M,8M,7M")
    )
    more_fire_yes_ask_lo: float = field(
        default_factory=lambda: _float("Q15_HVF_MORE_FIRE_YES_ASK_LO", 55.0)
    )
    more_fire_yes_ask_hi: float = field(
        default_factory=lambda: _float("Q15_HVF_MORE_FIRE_YES_ASK_HI", 70.0)
    )
    more_fire_spread_max: float = field(
        default_factory=lambda: _float("Q15_HVF_MORE_FIRE_SPREAD_MAX", 3.0)
    )
    more_fire_min_calibrated_yes: float = field(
        default_factory=lambda: _float("Q15_HVF_MORE_FIRE_MIN_CAL_YES", 0.60)
    )
    more_fire_min_mid_jump_cents: float = field(
        default_factory=lambda: _float("Q15_HVF_MORE_FIRE_MIN_MID_JUMP_CENTS", 4.0)
    )
    more_fire_min_yes_bid_ask_depth_ratio: float = field(
        default_factory=lambda: _float("Q15_HVF_MORE_FIRE_MIN_YES_BID_ASK_DEPTH_RATIO", 3.0)
    )

    depth_veto_enabled: bool = field(
        default_factory=lambda: _bool("Q15_HVF_DEPTH_VETO_ENABLED", True)
    )
    depth_veto_min_selected_bid_ask_ratio: float = field(
        default_factory=lambda: _float("Q15_HVF_DEPTH_VETO_MIN_SELECTED_RATIO", 0.10)
    )
    depth_veto_rule_codes: frozenset[str] = field(
        default_factory=lambda: _csv_set(
            "Q15_HVF_DEPTH_VETO_RULE_CODES",
            "HVF_OWN_STRONG_SELECTED,HVF_OWN_NO_FLASH,"
            "HVF_OWN_EARLY_FLIP,HVF_HYPE_EARLY_BULLISH_FLIP",
        )
    )

    early_enabled: bool = field(default_factory=lambda: _bool("Q15_HVF_EARLY_ENABLED", True))
    early_watch_enabled: bool = field(
        default_factory=lambda: _bool("Q15_HVF_EARLY_WATCH_ENABLED", True)
    )
    early_entry_enabled: bool = field(
        default_factory=lambda: _bool("Q15_HVF_EARLY_ENTRY_ENABLED", False)
    )
    early_ask_lo: float = field(default_factory=lambda: _float("Q15_HVF_EARLY_ASK_LO", 50.0))
    early_ask_hi: float = field(default_factory=lambda: _float("Q15_HVF_EARLY_ASK_HI", 65.0))
    early_mid_lo: float = field(default_factory=lambda: _float("Q15_HVF_EARLY_MID_LO", 45.0))
    early_mid_hi: float = field(default_factory=lambda: _float("Q15_HVF_EARLY_MID_HI", 62.0))
    early_spread_max: float = field(default_factory=lambda: _float("Q15_HVF_EARLY_SPREAD_MAX", 6.0))
    own_early_assets: frozenset[str] = field(
        default_factory=lambda: _csv_set("Q15_HVF_OWN_EARLY_ASSETS", "XRP,SOL,ETH,BNB")
    )
    own_early_yes_min: float = field(default_factory=lambda: _float("Q15_HVF_OWN_EARLY_YES_MIN", 0.57))
    own_early_no_max: float = field(default_factory=lambda: _float("Q15_HVF_OWN_EARLY_NO_MAX", 0.43))
    hype_early_enabled: bool = field(
        default_factory=lambda: _bool("Q15_HVF_HYPE_EARLY_ENABLED", False)
    )
    hype_early_yes_min: float = field(default_factory=lambda: _float("Q15_HVF_HYPE_EARLY_YES_MIN", 0.58))
    btc_early_follow_enabled: bool = field(
        default_factory=lambda: _bool("Q15_HVF_BTC_EARLY_FOLLOW_ENABLED", False)
    )
    btc_early_follow_mid_min: float = field(
        default_factory=lambda: _float("Q15_HVF_BTC_EARLY_FOLLOW_MID_MIN", 75.0)
    )
    btc_early_follow_assets: frozenset[str] = field(
        default_factory=lambda: _csv_set("Q15_HVF_BTC_EARLY_FOLLOW_ASSETS", "XRP,SOL,ETH")
    )

    own_no_yes_max: float = field(default_factory=lambda: _float("Q15_HVF_OWN_NO_YES_MAX", 0.35))
    own_no_bid_min: float = field(default_factory=lambda: _float("Q15_HVF_OWN_NO_BID_MIN", 70.0))
    own_strong_bid_min: float = field(default_factory=lambda: _float("Q15_HVF_OWN_STRONG_BID_MIN", 80.0))
    own_strong_spread_max: float = field(default_factory=lambda: _float("Q15_HVF_OWN_STRONG_SPREAD_MAX", 3.0))
    hype_bull_yes_min: float = field(default_factory=lambda: _float("Q15_HVF_HYPE_BULL_YES_MIN", 0.65))
    hype_bull_yes_bid_min: float = field(default_factory=lambda: _float("Q15_HVF_HYPE_BULL_YES_BID_MIN", 70.0))
    hype_bull_spread_max: float = field(default_factory=lambda: _float("Q15_HVF_HYPE_BULL_SPREAD_MAX", 4.0))
    btc_follow_enabled: bool = field(
        default_factory=lambda: _bool("Q15_HVF_BTC_FOLLOW_ENABLED", False)
    )
    btc_follow_mid_min: float = field(default_factory=lambda: _float("Q15_HVF_BTC_FOLLOW_MID_MIN", 80.0))
    btc_follow_asset_mid_min: float = field(default_factory=lambda: _float("Q15_HVF_BTC_FOLLOW_ASSET_MID_MIN", 75.0))
    btc_follow_assets: frozenset[str] = field(
        default_factory=lambda: _csv_set("Q15_HVF_BTC_FOLLOW_ASSETS", "XRP,SOL,ETH")
    )
    btc_follow_excluded_assets: frozenset[str] = field(
        default_factory=lambda: _csv_set("Q15_HVF_BTC_FOLLOW_EXCLUDED_ASSETS", "BNB,HYPE,DOGE")
    )
    divergence_watch_enabled: bool = field(
        default_factory=lambda: _bool("Q15_HVF_DIVERGENCE_WATCH_ENABLED", True)
    )
    divergence_watch_assets: frozenset[str] = field(
        default_factory=lambda: _csv_set("Q15_HVF_DIVERGENCE_WATCH_ASSETS", "XRP")
    )
    divergence_btc_mid_min: float = field(
        default_factory=lambda: _float("Q15_HVF_DIVERGENCE_BTC_MID_MIN", 75.0)
    )
    divergence_btc_jump_min: float = field(
        default_factory=lambda: _float("Q15_HVF_DIVERGENCE_BTC_JUMP_MIN", 13.0)
    )
    divergence_asset_same_mid_lo: float = field(
        default_factory=lambda: _float("Q15_HVF_DIVERGENCE_ASSET_SAME_MID_LO", 45.0)
    )
    divergence_asset_same_mid_hi: float = field(
        default_factory=lambda: _float("Q15_HVF_DIVERGENCE_ASSET_SAME_MID_HI", 60.0)
    )

    @classmethod
    def from_env(cls) -> "HighVolFlipConfig":
        return cls()


_enabled_cache: bool | None = None


def is_enabled() -> bool:
    global _enabled_cache
    if _enabled_cache is None:
        _enabled_cache = HighVolFlipConfig.from_env().enabled
    return _enabled_cache


def reset_enabled_cache() -> None:
    global _enabled_cache
    _enabled_cache = None


def window_key(close_time: float | None, now: float) -> int:
    basis = float(close_time) if close_time is not None else float(now)
    return int(basis // 900)
