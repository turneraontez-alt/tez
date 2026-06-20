from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return int(default)


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}



def _unsafe_allowed() -> bool:
    return _bool("Q15_ALLOW_UNSAFE_THRESHOLDS", False)


def _safe_float(name: str, default: float, floor: float) -> float:
    value = _float(name, default)
    return value if _unsafe_allowed() else max(floor, value)


def _safe_int(name: str, default: int, floor: int) -> int:
    value = _int(name, default)
    return value if _unsafe_allowed() else max(floor, value)


def _json_dict(name: str) -> Dict[str, Any]:
    raw = os.environ.get(name, "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@dataclass(frozen=True)
class Q15Settings:
    """Safety-first settings for the unified Q15 decision pipeline.

    V3 adds hysteresis: entry conditions must remain stable before an alert,
    while a confirmed setup is not invalidated merely because a fast-moving
    confirmation score, spread, or order-book feature flickers for a moment.
    """

    entry_size_contracts: float = field(default_factory=lambda: _float("Q15_ENTRY_SIZE", 10.0))
    min_new_entry_seconds: int = field(default_factory=lambda: _safe_int("Q15_MIN_NEW_ENTRY_SECONDS", 60, 60))

    # New entries must remain valid for both a duration and a number of cycles.
    # These intentionally use new environment names, so an old
    # Q15_PERSISTENCE_SECONDS=2 secret cannot silently restore twitchy behavior.
    candidate_persistence_seconds: float = field(default_factory=lambda: _safe_float("Q15_ENTRY_STABILITY_SECONDS", 8.0, 8.0))
    entry_stability_samples: int = field(default_factory=lambda: _safe_int("Q15_ENTRY_STABILITY_SAMPLES", 4, 4))
    candidate_grace_seconds: float = field(default_factory=lambda: _float("Q15_ENTRY_GRACE_SECONDS", 3.0))
    cooldown_seconds: float = field(default_factory=lambda: _float("Q15_INVALIDATION_COOLDOWN_SECONDS", 30.0))

    # Once an entry is confirmed, soft deterioration must persist before an
    # exit alert. Hard price/opposite-setup invalidations bypass these timers.
    minimum_hold_seconds: float = field(default_factory=lambda: _float("Q15_MINIMUM_HOLD_SECONDS", 15.0))
    soft_exit_persistence_seconds: float = field(default_factory=lambda: _float("Q15_SOFT_EXIT_PERSISTENCE_SECONDS", 12.0))
    exit_win_probability: float = field(default_factory=lambda: _float("Q15_EXIT_WIN_PROB", 0.49))
    exit_edge_cents: float = field(default_factory=lambda: _float("Q15_EXIT_EDGE_CENTS", 0.0))

    min_confirmation_groups: int = field(default_factory=lambda: _safe_int("Q15_MIN_CONFIRMATION_GROUPS", 3, 3))
    min_confirmation_score: float = field(default_factory=lambda: _safe_float("Q15_MIN_CONFIRMATION_SCORE", 48.0, 48.0))
    min_model_confidence: float = field(default_factory=lambda: _safe_float("Q15_MIN_MODEL_CONFIDENCE", 0.55, 0.55))
    min_win_probability: float = field(default_factory=lambda: _safe_float("Q15_MIN_WIN_PROB", 0.55, 0.55))

    # Adaptive confirmation: a primary setup needs `min_confirmation_groups`
    # independent groups at `min_confirmation_score`. A setup with fewer groups
    # (down to `adaptive_min_groups`) may still qualify if it clears a stricter
    # bar: higher score, an extra edge margin, and higher model quality.
    adaptive_confirmation_enabled: bool = field(default_factory=lambda: _bool("Q15_ADAPTIVE_CONFIRMATION", True))
    adaptive_min_groups: int = field(default_factory=lambda: _safe_int("Q15_ADAPTIVE_MIN_GROUPS", 2, 2))
    adaptive_min_score: float = field(default_factory=lambda: _safe_float("Q15_ADAPTIVE_MIN_SCORE", 62.0, 62.0))
    adaptive_extra_edge_cents: float = field(default_factory=lambda: _safe_float("Q15_ADAPTIVE_EXTRA_EDGE_CENTS", 2.0, 2.0))
    adaptive_min_model_quality: float = field(default_factory=lambda: _safe_float("Q15_ADAPTIVE_MIN_MODEL_QUALITY", 0.65, 0.65))

    max_spread_cents: float = field(default_factory=lambda: _float("Q15_MAX_SPREAD_CENTS", 3.0))
    max_ws_age_seconds: float = field(default_factory=lambda: _float("Q15_MAX_WS_AGE_SECONDS", 3.0))
    max_rest_age_seconds: float = field(default_factory=lambda: _float("Q15_MAX_REST_AGE_SECONDS", 5.0))
    min_depth_contracts: float = field(default_factory=lambda: _float("Q15_MIN_DEPTH_CONTRACTS", 20.0))
    wide_spread_min_depth: float = field(default_factory=lambda: _float("Q15_WIDE_SPREAD_MIN_DEPTH", 35.0))
    spread_penalty_per_cent: float = field(default_factory=lambda: _float("Q15_SPREAD_PENALTY_PER_CENT", 1.0))

    edge_12_15m: float = field(default_factory=lambda: _safe_float("Q15_EDGE_12_15M", 8.0, 8.0))
    edge_6_12m: float = field(default_factory=lambda: _safe_float("Q15_EDGE_6_12M", 6.0, 6.0))
    edge_2_6m: float = field(default_factory=lambda: _safe_float("Q15_EDGE_2_6M", 5.0, 5.0))
    edge_60_120s: float = field(default_factory=lambda: _safe_float("Q15_EDGE_60_120S", 8.0, 8.0))

    fee_rate: float = field(default_factory=lambda: _float("Q15_FEE_RATE", 0.07))
    default_tick_cents: float = field(default_factory=lambda: _float("Q15_DEFAULT_TICK_CENTS", 0.1))
    market_anchor_weight: float = field(default_factory=lambda: _float("Q15_MARKET_ANCHOR_WEIGHT", 0.20))
    momentum_z_cap: float = field(default_factory=lambda: _float("Q15_MOMENTUM_Z_CAP", 0.30))

    send_watch_alerts: bool = field(default_factory=lambda: _bool("Q15_SEND_WATCH_ALERTS", True))
    send_invalidation_alerts: bool = field(default_factory=lambda: _bool("Q15_SEND_INVALIDATIONS", True))
    correlation_control: bool = field(default_factory=lambda: _bool("Q15_CORRELATION_CONTROL", True))

    # Safety gate: alerts are paper-only until explicitly enabled and a minimum
    # settled sample exists. This changes wording only; the monitor remains
    # read-only and never submits orders.
    actionable_alerts: bool = field(default_factory=lambda: _bool("Q15_ACTIONABLE_ALERTS", False))
    min_settled_for_actionable: int = field(default_factory=lambda: _int("Q15_MIN_SETTLED_FOR_ACTIONABLE", 30))

    calibration: Dict[str, Any] = field(default_factory=lambda: _json_dict("Q15_CALIBRATION_JSON"))


    def min_edge_for(self, seconds_remaining: int | None) -> float:
        if seconds_remaining is None:
            return self.edge_6_12m
        if seconds_remaining >= 720:
            return self.edge_12_15m
        if seconds_remaining >= 360:
            return self.edge_6_12m
        if seconds_remaining >= 120:
            return self.edge_2_6m
        return self.edge_60_120s
