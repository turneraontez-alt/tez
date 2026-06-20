"""Default-OFF fast path for the v9.5 canonical candle build.

``_canonical_candles`` (frozen, in ``checkpoint_v94_unified``) re-normalises the
full per-asset candle history every cycle.  The dominant avoidable cost is that
``_normalize_candle`` resolves each of its 8 fields through ``_first_value`` /
``_first_num``, and every one of those builds a normalised-alias tuple (and, on a
miss, walks the row).  Over a few hundred cached rows per asset that adds up.

This module provides a behaviour-IDENTICAL fast alternative, gated behind
``Q15_FAST_CANONICAL_CANDLES`` (default OFF) at the v9.5 call sites only.  The
frozen chain is untouched.

Equivalence argument (see ``tests/test_q15_fast_canonical_candles.py`` for the
empirical proof across cached rows, live candle shapes, ms/ISO timestamps,
missing-field rows, partial-match keys, and nested candidates):

* Candidate gathering is byte-for-byte the frozen logic — same key order, same
  ``candidate[-1000:]`` slice, same ``_walk`` scan of the whole snapshot (candle
  lists can nest anywhere, so this is kept identical and NOT pruned).
* Per row we attempt a fast normalisation that uses ONLY direct-alias hits.  The
  frozen ``_first_value`` checks direct aliases *before* it ever walks, so a
  direct hit returns the same value irrespective of nesting.  If ANY field
  misses all of its direct aliases — the only case where the frozen path would
  walk (and where nesting/partial-matches matter) — we hand the whole row to the
  frozen ``_normalize_candle`` unchanged.  So every row is normalised either by
  pure direct lookups (provably equal) or by the frozen function itself.
* De-dup key, sort key and the trailing ``[-600:]`` slice are identical.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Sequence

from .checkpoint_v94_unified import (
    _normalize_candle,
    _num,
    _parse_ts,
    _walk,
)

# Field alias groups — MUST match _normalize_candle exactly (order included).
_OPEN = ("open", "o")
_HIGH = ("high", "h")
_LOW = ("low", "l")
_CLOSE = ("close", "c", "last")
_START = ("start_time", "start", "timestamp", "time", "ts", "bucket")
_END = ("end_time", "end", "close_time")
_VOLUME = ("volume", "v")
_TRADE = ("trade_count", "trades", "count")

# Snapshot keys probed for candle lists — identical to _canonical_candles.
_CANDLE_KEYS = (
    "underlying_candles_5s", "underlying_candles", "spot_candles_5s",
    "spot_candles", "candles_5s", "candles", "ohlc", "ohlcv", "bars",
)

_MISS = object()  # a field that has no direct-alias hit (frozen path needed)


def _direct(raw: Mapping[str, Any], aliases: Sequence[str]) -> Any:
    """First non-None direct-alias value, matching _first_value's direct loop.

    Returns ``_MISS`` when no alias is present with a non-None value — i.e. the
    exact case in which _first_value would fall through to ``_walk``.
    """
    for alias in aliases:
        if alias in raw:
            value = raw.get(alias)
            if value is not None:
                return value
    return _MISS


def _fast_normalize_candle(raw: Mapping[str, Any]):
    """Byte-identical _normalize_candle for rows resolvable via direct aliases.

    Returns ``_MISS`` when any field needs the frozen walk path; the caller then
    falls back to ``_normalize_candle(raw)`` so behaviour is never approximated.
    """
    o = _direct(raw, _OPEN)
    h = _direct(raw, _HIGH)
    l = _direct(raw, _LOW)
    c = _direct(raw, _CLOSE)
    st = _direct(raw, _START)
    en = _direct(raw, _END)
    vol = _direct(raw, _VOLUME)
    tc = _direct(raw, _TRADE)
    if _MISS in (o, h, l, c, st, en, vol, tc):
        return _MISS  # some field would walk — defer to the frozen function

    open_value = _num(o)
    high = _num(h)
    low = _num(l)
    close = _num(c)
    start = _parse_ts(st)
    end = _parse_ts(en)
    if None in (open_value, high, low, close):
        return None
    if min(float(open_value), float(high), float(low), float(close)) <= 0:
        return None
    high = max(float(high), float(open_value), float(close))
    low = min(float(low), float(open_value), float(close))
    if end is None and start is not None:
        end = start + 5.0
    timestamp = end if end is not None else start if start is not None else 0.0
    return {
        "open": float(open_value),
        "high": high,
        "low": low,
        "close": float(close),
        "start_time": float(start or max(0.0, float(timestamp) - 5.0)),
        "end_time": float(end or timestamp),
        "timestamp": float(timestamp),
        "volume": float(_num(vol) or 0.0),
        "trade_count": float(_num(tc) or 0.0),
    }


def fast_canonical_candles(
    snapshot: Mapping[str, Any],
    cached: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, float]]:
    """Drop-in, behaviour-identical fast equivalent of ``_canonical_candles``."""
    candidates: list[Sequence[Any]] = []
    if cached:
        candidates.append(cached)
    for key in _CANDLE_KEYS:
        value = snapshot.get(key)
        if isinstance(value, (list, tuple)):
            candidates.append(value)
    for key, value in _walk(snapshot):
        if isinstance(value, (list, tuple)) and any(token in key for token in ("candle", "ohlc", "bar")):
            candidates.append(value)

    merged: dict[float, dict[str, float]] = {}
    for candidate in candidates:
        for raw in candidate[-1000:]:
            if not isinstance(raw, Mapping):
                continue
            candle = _fast_normalize_candle(raw)
            if candle is _MISS:
                candle = _normalize_candle(raw)  # frozen path for walk-needing rows
            if candle is None:
                continue
            key = round(candle["start_time"] or candle["timestamp"], 6)
            merged[key] = candle
    rows = sorted(merged.values(), key=lambda row: (row["timestamp"], row["start_time"]))
    return rows[-600:]
