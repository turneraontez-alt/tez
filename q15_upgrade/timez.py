"""Eastern-Time display helpers (IANA ``America/Detroit``).

The whole system keeps UTC internally (APIs, SQLite storage, contract matching),
but every VISIBLE timestamp — Telegram reports, the comparison reset, window
labels, dashboards, logs — is rendered in Eastern Time using the IANA zone
``America/Detroit``. That zone is daylight-saving aware, so the rendered label is
**EDT** during daylight-saving and **EST** during standard time automatically;
nothing here uses a fixed UTC−5 offset.

Internal epoch seconds are never altered — these are formatting-only helpers, so
contract-close instants and interval checks are unchanged.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Default and intended zone. Q15_DISPLAY_TZ exists only as an escape hatch; if it
# is set to something unresolvable we fall back to Eastern rather than crash a
# render path.
TZ_NAME = os.environ.get("Q15_DISPLAY_TZ", "America/Detroit") or "America/Detroit"
try:
    EASTERN = ZoneInfo(TZ_NAME)
except Exception:  # pragma: no cover - defensive: bad override / missing tzdata
    TZ_NAME = "America/Detroit"
    EASTERN = ZoneInfo("America/Detroit")


def to_eastern(epoch: float) -> datetime:
    """A timezone-aware Eastern datetime for a unix epoch (UTC seconds)."""
    return datetime.fromtimestamp(float(epoch), tz=EASTERN)


def tz_abbrev(epoch: float | None = None) -> str:
    """'EDT' or 'EST' for the given instant (or now) — DST-correct."""
    ts = epoch if epoch is not None else datetime.now(timezone.utc).timestamp()
    return to_eastern(ts).strftime("%Z")


def fmt_eastern(epoch: float, fmt: str = "%Y-%m-%d %H:%M %Z") -> str:
    """Format an epoch in Eastern Time. Default includes the EDT/EST label."""
    return to_eastern(epoch).strftime(fmt)


def fmt_eastern_hm(epoch: float) -> str:
    """'HH:MM EDT' / 'HH:MM EST' — the compact window/clock label."""
    return to_eastern(epoch).strftime("%H:%M %Z")


def fmt_eastern_iso(epoch: float) -> str:
    """ISO-8601 in Eastern with offset (e.g. 2026-06-21T14:53:00-04:00) — a
    visible, sortable timestamp that still encodes the exact instant."""
    return to_eastern(epoch).isoformat()
