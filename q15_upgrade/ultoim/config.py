"""Ultoim Build — configuration.

Ultoim Build is a SEPARATE, read-only research reporting system that runs beside
the live champion. It never trades, never touches the live system's predictions,
records, or Telegram channel, and is byte-identical-to-production OFF by default
(``Q15_ULTOIM_ENABLED`` unset/false). It reuses the champion's frozen per-asset
analysis (read-only) to publish three value-ranked final-outcome picks at the
12M / 10M / 7M marks into its OWN database + Telegram channel.
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


# The research intervals and their fire mark (seconds before settlement). NOTE:
# 12M replaces the champion's toxic 15M checkpoint (the timing experiment showed
# the 12-minute mark is ~74% vs 15M's coin-flip), so Ultoim never reports a 15M.
INTERVAL_MARKS: dict[str, int] = {"12M": 720, "10M": 600, "7M": 420}


@dataclass(frozen=True)
class UltoimConfig:
    enabled: bool = field(default_factory=lambda: _bool("Q15_ULTOIM_ENABLED", False))
    model_version: str = field(
        default_factory=lambda: _str("Q15_ULTOIM_MODEL_VERSION", "ultoim-build-v1")
    )
    db_path: str = field(
        default_factory=lambda: _str("Q15_ULTOIM_DB", "data/q15_ultoim_v1.sqlite3")
    )
    # Same bot token as the live system (TELEGRAM_BOT_TOKEN) but a SEPARATE chat,
    # so reports land in their own channel and never mix with the live feed.
    telegram_chat_id: str = field(
        default_factory=lambda: _str("Q15_ULTOIM_TELEGRAM_CHAT_ID", "")
    )
    # Fire a report when seconds_remaining first falls into [mark - band, mark].
    # A generous band tolerates a multi-second loop stall without mislabelling a
    # late 12M report as a 10M one (if the band is missed entirely the interval is
    # simply skipped for that window).
    mark_band_seconds: float = field(
        default_factory=lambda: _float("Q15_ULTOIM_MARK_BAND_SECONDS", 90.0)
    )
    top_k: int = 3
    reconcile_every_seconds: float = field(
        default_factory=lambda: _float("Q15_ULTOIM_RECONCILE_EVERY_SECONDS", 30.0)
    )
    # Value ranking: rank_score = quality + edge_weight * clamp(net_edge/edge_scale).
    # Quality dominates; the net-edge term lifts cheap-but-good picks and breaks
    # ties — applying the finding that pure-confidence ranking is flat/unprofitable.
    edge_weight: float = field(default_factory=lambda: _float("Q15_ULTOIM_EDGE_WEIGHT", 0.15))
    edge_scale_cents: float = field(
        default_factory=lambda: _float("Q15_ULTOIM_EDGE_SCALE_CENTS", 10.0)
    )
    # Multi-factor grade thresholds on the 0..1 quality score (tunable; the grade
    # is research-only and recalibratable). A/B cutoffs; below B is C.
    grade_a_min: float = field(default_factory=lambda: _float("Q15_ULTOIM_GRADE_A_MIN", 0.60))
    grade_b_min: float = field(default_factory=lambda: _float("Q15_ULTOIM_GRADE_B_MIN", 0.48))
    # Flip influence on the grade. Held LOW by default: the flip predictor is in
    # active research and has not cleared its out-of-sample bar, so it informs the
    # grade only weakly until validated.
    flip_grade_weight: float = field(
        default_factory=lambda: _float("Q15_ULTOIM_FLIP_GRADE_WEIGHT", 0.20)
    )
    flip_decision_threshold: float = field(
        default_factory=lambda: _float("Q15_ULTOIM_FLIP_DECISION_THRESHOLD", 0.55)
    )

    @classmethod
    def from_env(cls) -> "UltoimConfig":
        return cls()


_enabled_cache: bool | None = None


def is_enabled() -> bool:
    global _enabled_cache
    if _enabled_cache is None:
        _enabled_cache = UltoimConfig.from_env().enabled
    return _enabled_cache


def reset_enabled_cache() -> None:
    """Test hook: clear the cached enabled read."""
    global _enabled_cache
    _enabled_cache = None
