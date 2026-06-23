"""Interval-timing research — configuration.

A SEPARATE, default-OFF, read-only research collector that captures the
champion's frozen per-asset analysis at EIGHT time-marks before settlement
(15M..7M) so the best entry / confirmation / defensive timing can be measured
on real, matched data. It NEVER trades, never sends Telegram, and never touches
the live champion's probability, edge, or entry decision — it only records.

It is ON by default (capture-only): the new intervals (13M/12M/11M/9M/8M) have
NO historical data, so this module collects them PROSPECTIVELY from the deploy
timestamp. It never trades and never sends Telegram — it only records to its own
DB. Nothing about it is "useful" until out-of-sample results prove it — until
then it is pure capture. Set ``Q15_INTERVAL_RESEARCH_ENABLED=false`` for a fully
inert, byte-identical app.
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


# The eight research marks, in SECONDS before settlement. The champion already
# runs 15M/10M/7M live; the rest are research-only (no live trade ever).
INTERVAL_MARKS: dict[str, int] = {
    "15M": 900, "13M": 780, "12M": 720, "11M": 660,
    "10M": 600, "9M": 540, "8M": 480, "7M": 420,
}

# Provisional role configuration (best-supported, re-evaluated as data accrues).
INTERVAL_ROLES: dict[str, str] = {
    "15M": "EARLY_RESEARCH",
    "13M": "INTERMEDIATE_RESEARCH",
    "12M": "INTERMEDIATE_RESEARCH",
    "11M": "INTERMEDIATE_RESEARCH",
    "10M": "OFFENSIVE_ENTRY",
    "9M": "CONFIRMATION_DEFENSIVE_RESEARCH",
    "8M": "CONFIRMATION_DEFENSIVE_RESEARCH",
    "7M": "CONFIRMATION_DEFENSIVE",
}

# Intervals the FROZEN champion already scores+alerts live. The research module
# never changes their live behaviour; it only also records them so cross-interval
# comparisons use one consistent schema.
LIVE_INTERVALS = frozenset({"15M", "10M", "7M"})
# New intervals that exist ONLY in research capture (no live trade, no alert).
RESEARCH_ONLY_INTERVALS = frozenset({"13M", "12M", "11M", "9M", "8M"})

# Exactly one reason code is saved for every interval where a capture is missing.
# Kept distinct so "no directional prediction" is never conflated with "valid
# prediction but no economic entry".
REASON_CODES = (
    "MARKET_NOT_AVAILABLE",        # contract/market not present at this mark
    "CONTRACT_NOT_MAPPED",         # no canonical ticker resolved
    "DATA_QUALITY_FAILED",         # snapshot below the data-quality floor
    "SCHEDULER_MISSED",            # the mark band was never observed this cycle window
    "MODEL_COULD_NOT_SCORE",       # prediction_available False (model produced no side)
    "BELOW_PREDICTION_THRESHOLD",  # scored but below the confidence floor
    "PREDICTION_GENERATED_NOT_SENT",  # directional prediction existed, not delivered
    "NO_ECONOMIC_ENTRY",           # valid prediction but no entry qualified (price/edge)
    "DUPLICATE_BLOCKED",           # a capture already exists for (ticker, interval)
    "OTHER_ERROR",
)


@dataclass(frozen=True)
class IntervalResearchConfig:
    # Default ON (capture-only): the owner asked to START collecting the full
    # 8-mark ladder so the 9M/8M "knee" and per-mark executable economics accrue
    # prospectively. It NEVER trades and NEVER sends Telegram — it only records to
    # its own DB. Set Q15_INTERVAL_RESEARCH_ENABLED=false for a fully inert app.
    enabled: bool = field(default_factory=lambda: _bool("Q15_INTERVAL_RESEARCH_ENABLED", True))
    model_version: str = field(
        default_factory=lambda: _str("Q15_INTERVAL_RESEARCH_MODEL_VERSION", "interval-research-v1")
    )
    db_path: str = field(
        default_factory=lambda: _str("Q15_INTERVAL_RESEARCH_DB", "data/q15_interval_research_v1.sqlite3")
    )
    # A capture fires when seconds_remaining first falls into [mark - band, mark].
    # A tight band keeps each capture close to its nominal mark; the per-(ticker,
    # interval) unique key prevents double-recording across the ~1s loop.
    mark_band_seconds: float = field(
        default_factory=lambda: _float("Q15_INTERVAL_RESEARCH_BAND_SECONDS", 25.0)
    )
    # Minimum snapshot data-quality to record a *capture* (below this we record a
    # DATA_QUALITY_FAILED missing-row instead, so coverage stays honest).
    min_data_quality: float = field(
        default_factory=lambda: _float("Q15_INTERVAL_RESEARCH_MIN_DATA_QUALITY", 0.0)
    )

    @classmethod
    def from_env(cls) -> "IntervalResearchConfig":
        return cls()


_enabled_cache: bool | None = None


def is_enabled() -> bool:
    global _enabled_cache
    if _enabled_cache is None:
        _enabled_cache = IntervalResearchConfig.from_env().enabled
    return _enabled_cache


def reset_enabled_cache() -> None:
    """Test hook: clear the cached enabled read."""
    global _enabled_cache
    _enabled_cache = None


def window_key(close_time: float | None, now: float) -> int:
    """Bucket a contract into its 15-minute settlement window (shared across the
    assets that settle together), mirroring the rest of the system."""
    basis = float(close_time) if close_time is not None else float(now)
    return int(basis // 900)
