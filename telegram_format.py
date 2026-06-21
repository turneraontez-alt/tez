"""Shared presentation helpers for Telegram messages (read-only).

Pure formatting functions that give every Telegram alert and report a single,
consistent vocabulary: percentages, prices, cents, countdowns, data freshness,
feed divergence and timezone-aware timestamps. There is no I/O and no engine
state here — each function is deterministic given its inputs (and, for the
"now" helpers, a caller-supplied clock), so the formatting layer can be tested
in isolation and reused by any builder without dragging in prediction logic.

Design rules enforced here so individual builders do not have to:
  * Missing / NaN / wrong-typed inputs degrade to an explicit placeholder
    ("n/a", "–", "unknown") rather than raising or printing "None".
  * Freshness and divergence treat *missing* signals as NOT trusted, never as
    silently healthy — a half-up feed must never read as "fresh".
  * All dynamic text destined for Telegram's HTML parse mode is escaped via
    :func:`escape_html`.
"""
from __future__ import annotations

import html
import math
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional, Tuple, Union

try:  # stdlib on 3.9+; needs the `tzdata` wheel on a bare container.
    from zoneinfo import ZoneInfo
    DEFAULT_TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - fallback if the tz database is missing
    DEFAULT_TZ = timezone(timedelta(hours=-5), "EST")

# Placeholders for missing data, kept distinct so a reader can tell "we have no
# value" (NA) from "this numeric slot is empty" (DASH).
NA = "n/a"
DASH = "–"  # en dash
CENT = "¢"

_Number = Union[int, float]


def _is_real_number(value) -> bool:
    """True only for genuine, finite numbers — bools and NaN/inf are rejected.

    ``isinstance(True, int)`` is True in Python, so booleans are excluded
    explicitly; otherwise ``format_pct(True)`` would render "100%".
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def escape_html(value) -> str:
    """HTML-escape dynamic text for Telegram's HTML parse mode (``None`` -> "").

    ``quote=True`` also escapes single/double quotes so the value is safe inside
    attribute-like contexts. Builders pass user/feed-derived strings (asset
    names, reasons, source labels) through this before interpolating them next
    to intended ``<b>`` / ``<pre>`` markup.
    """
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def format_pct(value: Optional[_Number], digits: int = 0, *, none: str = NA) -> str:
    """Render a 0..1 probability/fraction as a percentage, e.g. 0.62 -> "62%".

    Non-numeric / NaN inputs return ``none`` instead of raising, so an optional
    confidence field that is missing never breaks a message.
    """
    if not _is_real_number(value):
        return none
    return f"{value * 100:.{digits}f}%"


def format_cents(
    value: Optional[_Number], digits: int = 0, *, signed: bool = False, none: str = DASH
) -> str:
    """Render a Kalshi price/edge in cents, e.g. 53 -> "53¢", +1.8 -> "+1.8¢".

    ``signed=True`` always shows the sign (useful for an edge that can be
    negative); the glyph is a true cent sign so it reads identically in Telegram.
    """
    if not _is_real_number(value):
        return none
    sign = "+" if signed and value >= 0 else ""
    return f"{sign}{value:.{digits}f}{CENT}"


def format_price(value: Optional[_Number], *, none: str = DASH) -> str:
    """Render an underlying spot/strike in USD with magnitude-aware precision.

    Sub-$10 assets (XRP, DOGE) keep 4 decimals; everything else uses 2, with
    thousands separators. Mirrors the dashboard's ``fmtUsd`` so a price reads the
    same on the web and in Telegram.
    """
    if not _is_real_number(value):
        return none
    decimals = 4 if abs(value) < 10 else 2
    return f"${value:,.{decimals}f}"


def format_countdown(seconds: Optional[_Number], *, none: str = DASH) -> str:
    """Render a remaining duration as "9m 00s" (or "1h 05m" past an hour).

    Negative values clamp to zero (a window never shows negative time left).
    """
    if not _is_real_number(seconds):
        return none
    total = int(max(0, seconds))
    minutes, secs = divmod(total, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {secs:02d}s"


def format_age(seconds: Optional[_Number], *, none: str = "?") -> str:
    """Render a feed age compactly: "3s", "2m 05s", "1h 04m".

    Distinct from :func:`format_countdown` so a small age reads as "3s" rather
    than "0m 03s".
    """
    if not _is_real_number(seconds):
        return none
    age = max(0.0, float(seconds))
    if age < 60:
        return f"{age:.0f}s"
    minutes, secs = divmod(int(age), 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def freshness(
    age_seconds: Optional[_Number], *, warn_after: float = 5.0, stale_after: float = 15.0
) -> Tuple[str, bool]:
    """Classify how current a feed is: ``(label, is_stale)``.

    ``label`` is a short human string ("fresh (3s)", "aging (8s)",
    "STALE (20s)"); ``is_stale`` lets a caller gate or flag on one boolean.
    A *missing* age returns ``("unknown", True)`` — an absent freshness signal is
    treated as not-trusted, never silently fresh, so stale data is never
    accepted by omission.
    """
    if not _is_real_number(age_seconds):
        return ("unknown", True)
    age = max(0.0, float(age_seconds))
    if age >= stale_after:
        return (f"STALE ({format_age(age)})", True)
    if age >= warn_after:
        return (f"aging ({format_age(age)})", False)
    return (f"fresh ({format_age(age)})", False)


def divergence(
    price_a: Optional[_Number],
    price_b: Optional[_Number],
    *,
    label_a: str = "Coinbase",
    label_b: str = "OKX",
    warn_bps: float = 10.0,
    none: str = NA,
) -> Tuple[str, bool]:
    """Describe agreement between two reference prices: ``(text, diverged)``.

    The gap is reported in basis points relative to the two prices' midpoint, so
    it is comparable across assets of very different magnitude. ``diverged`` is
    True once ``|gap| >= warn_bps``. If either price is missing or non-positive
    the result is ``(none, False)`` — a half-up feed must not raise a false
    divergence alarm.
    """
    if not (_is_real_number(price_a) and _is_real_number(price_b)):
        return (none, False)
    if price_a <= 0 or price_b <= 0:
        return (none, False)
    mid = (price_a + price_b) / 2.0
    if mid <= 0:  # pragma: no cover - both positive implies mid > 0
        return (none, False)
    bps = abs(price_a - price_b) / mid * 10000.0
    diverged = bps >= warn_bps
    state = "diverge" if diverged else "agree"
    return (f"{label_a}/{label_b} {state} ({bps:.0f} bps)", diverged)


def _coerce_dt(when, tz) -> Optional[datetime]:
    """Normalise a datetime or epoch-seconds value into an aware dt in ``tz``."""
    if isinstance(when, datetime):
        dt = when
    elif _is_real_number(when):
        dt = datetime.fromtimestamp(float(when), tz=timezone.utc)
    else:
        return None
    if dt.tzinfo is None:  # assume naive timestamps are UTC, never local
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def format_timestamp(
    when, *, tz=DEFAULT_TZ, with_date: bool = False, clock_24h: bool = True, none: str = NA
) -> str:
    """Render a datetime/epoch as a timezone-labelled wall-clock string.

    Always carries the timezone abbreviation (e.g. "15:07:42 EDT") so a reader is
    never left guessing which zone a time is in. ``clock_24h=False`` switches to
    12-hour ("3:07:42 PM EDT"); ``with_date=True`` prefixes the calendar date.
    """
    dt = _coerce_dt(when, tz)
    if dt is None:
        return none
    if clock_24h:
        time_part = dt.strftime("%H:%M:%S %Z")
    else:
        time_part = dt.strftime("%I:%M:%S %p %Z").lstrip("0")
    if with_date:
        return f"{dt.strftime('%Y-%m-%d')} {time_part}"
    return time_part


def now_label(
    *,
    tz=DEFAULT_TZ,
    with_date: bool = False,
    clock_24h: bool = True,
    clock: Optional[Callable[[], datetime]] = None,
) -> str:
    """Generation-timestamp label for "now", timezone-aware and testable.

    ``clock`` (default :func:`datetime.now` in UTC) is injectable so tests pin a
    fixed instant rather than depending on wall-clock time.
    """
    now = clock() if clock is not None else datetime.now(timezone.utc)
    return format_timestamp(now, tz=tz, with_date=with_date, clock_24h=clock_24h)


def clamp_for_telegram(text: str, *, limit: int = 4096, suffix: str = "\n… (truncated)") -> str:
    """Guarantee a message fits Telegram's hard length limit (4096 chars).

    If ``text`` is over ``limit`` it is cut to ``limit`` including a visible
    truncation marker, so an over-long report degrades to a clipped-but-valid
    message instead of a Telegram 400. Callers that build inside a ``<pre>``
    block should budget for it; this is the final backstop.
    """
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(suffix))
    return text[:keep] + suffix
