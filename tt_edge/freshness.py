"""Freshness guard — the single gate that keeps stale data out of analysis.

Stale data was the exact failure mode of manual analysis (aiscore served a
June 24 snapshot on July 18), so every scraped payload carries a ``fetched_at``
and every consumer calls :func:`check` before using it. A payload at or past
its limit is REJECTED (the boundary is stale: ``age >= limit`` fails, so a
board fetched exactly 30 minutes ago is already out).

All timestamps must be timezone-aware; naive datetimes raise immediately
rather than silently comparing wrong (TT Elite runs on CET, we normalize to
UTC at ingest).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

KINDS = ("board", "h2h", "form", "odds")


class NaiveTimestampError(ValueError):
    """A timestamp without tzinfo reached the freshness guard."""


def require_aware(name: str, value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise NaiveTimestampError(f"{name} must be timezone-aware; got {value!r}")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class FreshnessCheck:
    """Outcome of one guard evaluation (kept for logging + the alert's
    data-age line)."""

    kind: str
    age_seconds: float
    limit_seconds: int
    ok: bool

    @property
    def reason(self) -> str | None:
        return None if self.ok else f"stale_{self.kind}"


def age_seconds(fetched_at: datetime, now: datetime) -> float:
    """Age of a payload in seconds. A fetched_at in the future (clock skew
    between source and host) is clamped to age 0 rather than going negative."""
    fetched = require_aware("fetched_at", fetched_at)
    current = require_aware("now", now)
    return max(0.0, (current - fetched).total_seconds())


def check(kind: str, fetched_at: datetime, now: datetime,
          limit_seconds: int) -> FreshnessCheck:
    """Evaluate one payload against its limit. ``age >= limit`` is stale."""
    if kind not in KINDS:
        raise ValueError(f"unknown freshness kind {kind!r}; expected one of {KINDS}")
    if limit_seconds < 1:
        raise ValueError(f"limit_seconds must be >= 1; got {limit_seconds}")
    age = age_seconds(fetched_at, now)
    return FreshnessCheck(kind=kind, age_seconds=age,
                          limit_seconds=limit_seconds, ok=age < limit_seconds)


def format_age(seconds: float) -> str:
    """Human age for the alert's data-age line: 45s / 4m / 2h / 3d."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"
