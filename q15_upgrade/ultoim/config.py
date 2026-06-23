"""Ultoim Build — configuration.

Ultoim Build is a SEPARATE, read-only research reporting system that runs beside
the live champion. It never trades and never touches the live system's
predictions, records, or Telegram channel. It is ON by default (a read-only
collector) but stays MUTED until its own Telegram channel is configured, so it
silently accrues research data without delivering; set ``Q15_ULTOIM_ENABLED=false``
for a fully inert, byte-identical app. It reuses the champion's frozen per-asset
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
    # Default ON: Ultoim is a read-only research collector and the whole point is
    # to accrue data. It stays MUTED (records, never delivers) until a separate
    # Telegram channel is configured, so default-on cannot spam or touch the live
    # system. Set Q15_ULTOIM_ENABLED=false for a fully inert, byte-identical app.
    enabled: bool = field(default_factory=lambda: _bool("Q15_ULTOIM_ENABLED", True))
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
    # is research-only and recalibratable). A/B cutoffs; below B is C. Calibrated
    # to the weighted-average scorer below, whose realized range on live picks is
    # ~0.35..0.70 (the old all-multiplicative scorer collapsed every pick under
    # the B line, so every grade was a C).
    grade_a_min: float = field(default_factory=lambda: _float("Q15_ULTOIM_GRADE_A_MIN", 0.60))
    grade_b_min: float = field(default_factory=lambda: _float("Q15_ULTOIM_GRADE_B_MIN", 0.50))
    # Grade = WEIGHTED AVERAGE of independent positive signals (directional
    # confidence, data quality, evidence quality, model agreement) — not a product
    # — so a strong setup lands high instead of being multiplied toward zero.
    # Weights are tunable and need not sum to 1 (the scorer normalises by their
    # sum). Confidence carries the most weight; it is necessary but not sufficient.
    grade_weight_confidence: float = field(
        default_factory=lambda: _float("Q15_ULTOIM_GRADE_W_CONFIDENCE", 0.50)
    )
    grade_weight_data: float = field(
        default_factory=lambda: _float("Q15_ULTOIM_GRADE_W_DATA", 0.15)
    )
    grade_weight_evidence: float = field(
        default_factory=lambda: _float("Q15_ULTOIM_GRADE_W_EVIDENCE", 0.15)
    )
    grade_weight_agreement: float = field(
        default_factory=lambda: _float("Q15_ULTOIM_GRADE_W_AGREEMENT", 0.20)
    )
    # Exclude the unvalidated, poorly-calibrated observational challenger from the
    # model-agreement term by default: per the live record it diverges and would
    # otherwise drag the grade. Set true to fold it back in.
    grade_includes_challenger: bool = field(
        default_factory=lambda: _bool("Q15_ULTOIM_GRADE_INCLUDES_CHALLENGER", False)
    )
    # Manipulation anti-signal: bounded multiplicative penalty on the grade.
    manip_grade_penalty: float = field(
        default_factory=lambda: _float("Q15_ULTOIM_MANIP_GRADE_PENALTY", 0.85)
    )
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
