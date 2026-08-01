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
    min_venue_sources: int = field(
        default_factory=lambda: _int("Q15_MARKETLEAD_MIN_VENUE_SOURCES", 2)
    )
    proxy_sources_csv: str = field(
        default_factory=lambda: os.environ.get(
            "Q15_MARKETLEAD_PROXY_SOURCES", "coinbase,kraken"
        )
    )
    source_stale_seconds: float = field(
        default_factory=lambda: _float("Q15_MARKETLEAD_SOURCE_STALE_SECONDS", 3.0)
    )
    transport_stale_seconds: float = field(
        default_factory=lambda: _float("Q15_MARKETLEAD_TRANSPORT_STALE_SECONDS", 10.0)
    )
    # Max age of the BOOK ITSELF for a websocket venue, in seconds.
    #
    # Distinct from the two above, and the only one that can catch a silently dropped
    # subscription: `source_stale_seconds` is measured against `sample_timestamp`, which the
    # collectors stamp at READ time (so it is always ~0), and `transport_stale_seconds` is
    # measured against the last message of ANY kind — and heartbeats keep flowing after a
    # level2 subscription dies. A venue can therefore report transport_connected=True,
    # last_message_age~0 and a book that has not moved in ten minutes. Reject on real book age.
    # Deliberately generous (a genuinely live crypto venue updates sub-second, so this only
    # bites on a frozen book); 0 disables the check.
    book_stale_seconds: float = field(
        default_factory=lambda: _float("Q15_MARKETLEAD_BOOK_STALE_SECONDS", 60.0)
    )
    source_future_tolerance_seconds: float = field(
        default_factory=lambda: _float(
            "Q15_MARKETLEAD_SOURCE_FUTURE_TOLERANCE_SECONDS", 0.5
        )
    )
    max_source_spread_bps: float = field(
        default_factory=lambda: _float("Q15_MARKETLEAD_MAX_SOURCE_SPREAD_BPS", 50.0)
    )
    sync_tolerance_seconds: float = field(
        default_factory=lambda: _float("Q15_MARKETLEAD_SYNC_TOLERANCE_SECONDS", 2.0)
    )
    max_proxy_dispersion_bps: float = field(
        default_factory=lambda: _float(
            "Q15_MARKETLEAD_MAX_PROXY_DISPERSION_BPS", 25.0
        )
    )
    require_live_proxy_sources: bool = field(
        default_factory=lambda: _bool("Q15_MARKETLEAD_REQUIRE_LIVE_PROXY_SOURCES", True)
    )
    history_seconds: float = field(
        default_factory=lambda: _float("Q15_MARKETLEAD_HISTORY_SECONDS", 300.0)
    )
    lead_lag_pressure_max: float = field(
        default_factory=lambda: _float(
            "Q15_MARKETLEAD_LEAD_LAG_PRESSURE_MAX", -0.10
        )
    )
    kalshi_stale_seconds: float = field(
        default_factory=lambda: _float(
            "Q15_MARKETLEAD_KALSHI_STALE_SECONDS", 3.0
        )
    )
    paper_limit_cents: float = field(
        default_factory=lambda: _float("Q15_MARKETLEAD_PAPER_LIMIT_CENTS", 69.0)
    )
    v3_notify_enabled: bool = field(
        default_factory=lambda: _bool("Q15_MARKETLEAD_V3_NOTIFY", False)
    )
    v3_rule_version: str = field(
        default_factory=lambda: os.environ.get(
            "Q15_MARKETLEAD_V3_RULE_VERSION", "marketlead-prospective-audit-v2"
        )
    )
    v3_min_proxy_distance_bps: float = field(
        default_factory=lambda: _float(
            "Q15_MARKETLEAD_V3_MIN_PROXY_DISTANCE_BPS", 5.0
        )
    )
    v3_min_venue_impulse: float = field(
        default_factory=lambda: _float(
            "Q15_MARKETLEAD_V3_MIN_VENUE_IMPULSE", 0.20
        )
    )
    v3_max_kalshi_pressure: float = field(
        default_factory=lambda: _float(
            "Q15_MARKETLEAD_V3_MAX_KALSHI_PRESSURE", -0.20
        )
    )
    v3_guard_min_resolved: int = field(
        default_factory=lambda: _int(
            "Q15_MARKETLEAD_V3_GUARD_MIN_RESOLVED", 8
        )
    )
    v3_guard_accuracy_min: float = field(
        default_factory=lambda: _float(
            "Q15_MARKETLEAD_V3_GUARD_ACCURACY_MIN", 0.80
        )
    )
    v3_guard_lookback: int = field(
        default_factory=lambda: _int(
            "Q15_MARKETLEAD_V3_GUARD_LOOKBACK", 20
        )
    )
    audit_block_windows: int = field(
        default_factory=lambda: _int(
            "Q15_MARKETLEAD_AUDIT_BLOCK_WINDOWS", 20
        )
    )
    audit_min_blocks: int = field(
        default_factory=lambda: _int("Q15_MARKETLEAD_AUDIT_MIN_BLOCKS", 3)
    )
    audit_accuracy_min: float = field(
        default_factory=lambda: _float(
            "Q15_MARKETLEAD_AUDIT_ACCURACY_MIN", 0.86
        )
    )
    audit_wilson_lb_min: float = field(
        default_factory=lambda: _float(
            "Q15_MARKETLEAD_AUDIT_WILSON_LB_MIN", 0.75
        )
    )
    v3_notify_retry_seconds: float = field(
        default_factory=lambda: _float(
            "Q15_MARKETLEAD_V3_NOTIFY_RETRY_SECONDS", 30.0
        )
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
