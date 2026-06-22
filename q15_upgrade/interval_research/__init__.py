"""Interval-timing research — a default-OFF, read-only research collector.

Captures the frozen champion's per-asset analysis at eight marks before
settlement (15M/13M/12M/11M/10M/9M/8M/7M) into its own SQLite ledger so the best
entry / confirmation / defensive-exit timing can be measured on real, matched,
out-of-sample data. It never trades, never sends Telegram, and never changes the
champion. Enable prospective collection with ``Q15_INTERVAL_RESEARCH_ENABLED=true``.
"""
from __future__ import annotations

from .config import (INTERVAL_MARKS, INTERVAL_ROLES, LIVE_INTERVALS,
                     RESEARCH_ONLY_INTERVALS, REASON_CODES,
                     IntervalResearchConfig, is_enabled)
from .runner import IntervalResearchRunner, get_runner, reset_runner

__all__ = [
    "INTERVAL_MARKS", "INTERVAL_ROLES", "LIVE_INTERVALS", "RESEARCH_ONLY_INTERVALS",
    "REASON_CODES", "IntervalResearchConfig", "is_enabled",
    "IntervalResearchRunner", "get_runner", "reset_runner",
]
