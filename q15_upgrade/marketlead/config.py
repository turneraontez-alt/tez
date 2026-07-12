"""Configuration for the paper-only Q15 MarketLead collector."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class MarketLeadConfig:
    enabled: bool = field(default_factory=lambda: _bool("Q15_MARKETLEAD_ENABLED", True))
    db_path: str = field(
        default_factory=lambda: os.environ.get(
            "Q15_MARKETLEAD_DB", "data/q15_marketlead_v1.sqlite3"
        )
    )
    system_version: str = field(
        default_factory=lambda: os.environ.get(
            "Q15_MARKETLEAD_VERSION", "q15-marketlead-v1"
        )
    )
    mark_seconds: int = field(
        default_factory=lambda: _int("Q15_MARKETLEAD_MARK_SECONDS", 780)
    )
    mark_band_seconds: float = field(
        default_factory=lambda: _float("Q15_MARKETLEAD_MARK_BAND_SECONDS", 25.0)
    )
    crossing_max_seconds: float = field(
        default_factory=lambda: _float("Q15_MARKETLEAD_CROSSING_MAX_SECONDS", 90.0)
    )
    crossing_max_offset_seconds: float = field(
        default_factory=lambda: _float("Q15_MARKETLEAD_CROSSING_MAX_OFFSET_SECONDS", 45.0)
    )
    min_proxy_sources: int = field(
        default_factory=lambda: _int("Q15_MARKETLEAD_MIN_PROXY_SOURCES", 2)
    )
    proxy_sources_csv: str = field(
        default_factory=lambda: os.environ.get(
            "Q15_MARKETLEAD_PROXY_SOURCES", "coinbase,kraken"
        )
    )
    source_stale_seconds: float = field(
        default_factory=lambda: _float("Q15_MARKETLEAD_SOURCE_STALE_SECONDS", 20.0)
    )
    sync_tolerance_seconds: float = field(
        default_factory=lambda: _float("Q15_MARKETLEAD_SYNC_TOLERANCE_SECONDS", 10.0)
    )
    history_seconds: float = field(
        default_factory=lambda: _float("Q15_MARKETLEAD_HISTORY_SECONDS", 300.0)
    )
    paper_limit_cents: float = field(
        default_factory=lambda: _float("Q15_MARKETLEAD_PAPER_LIMIT_CENTS", 69.0)
    )

    @property
    def proxy_sources(self) -> frozenset[str]:
        return frozenset(
            value.strip().lower()
            for value in self.proxy_sources_csv.split(",")
            if value.strip()
        )

    @classmethod
    def from_env(cls) -> "MarketLeadConfig":
        return cls()
