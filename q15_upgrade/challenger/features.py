"""Leakage-safe feature extraction for the challenger.

Every feature is computed ONLY from information available at decision time
(current spot, strike, time-remaining, recent price action, volatility, and the
prediction-market quote). Settlement / outcome fields are never read here — see
``FORBIDDEN_KEYS`` and ``assert_no_leakage`` which the tests enforce.

The 9 frozen champion signals, when present on the snapshot under
``feature_values``, are reused as inputs (prefixed ``champ_``) so the challenger
can learn corrections on top of the same evidence rather than from scratch.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping

from .mathx import clamp, num

# Champion signals (decision-time) we fold in as inputs when available.
_CHAMP_SIGNALS = (
    "momentum", "flow", "book", "wick", "context",
    "threshold_interaction", "exchange_consensus", "derivatives", "absorption",
)

# Derived features computed here (order is FROZEN — models key on this order).
_DERIVED = (
    "normalized_distance",
    "log_distance",
    "pct_distance",
    "direction",
    "minutes_remaining",
    "sqrt_minutes_remaining",
    "volatility_per_min",
    "market_implied_prob",
    "market_logit",
    "contract_spread_cents",
    "book_imbalance",
)

FEATURE_NAMES: tuple[str, ...] = _DERIVED + tuple(f"champ_{s}" for s in _CHAMP_SIGNALS)

# Substrings that would indicate look-ahead / settlement leakage if used.
FORBIDDEN_KEYS = ("result", "outcome", "settle", "resolved", "official", "won", "pnl", "profit")


def assert_no_leakage(used_keys) -> None:
    """Raise if any consumed snapshot key looks like outcome/settlement info."""
    for k in used_keys:
        lk = str(k).lower()
        for bad in FORBIDDEN_KEYS:
            if bad in lk:
                raise ValueError(f"potential leakage: feature reads snapshot key '{k}'")


def _first(snap: Mapping[str, Any], *keys, default=None):
    for k in keys:
        if k in snap and snap[k] is not None:
            return snap[k]
    return default


@dataclass
class FeatureVector:
    values: list[float]                       # aligned to FEATURE_NAMES
    coverage: list[bool]                      # True where the source datum was present
    names: tuple[str, ...] = FEATURE_NAMES
    details: dict[str, float] = field(default_factory=dict)
    # Auxiliary decision-time context (NOT model features) used by decision.py.
    market_yes_prob: float | None = None
    yes_ask_cents: float | None = None
    yes_bid_cents: float | None = None
    no_ask_cents: float | None = None
    no_bid_cents: float | None = None
    spread_cents: float | None = None
    depth_contracts: float | None = None
    seconds_remaining: float | None = None
    data_quality: float | None = None
    spot: float | None = None
    target: float | None = None

    @property
    def coverage_fraction(self) -> float:
        return sum(1 for c in self.coverage if c) / len(self.coverage) if self.coverage else 0.0


def _volatility_per_min(snap: Mapping[str, Any]) -> tuple[float, bool]:
    """Per-minute fractional volatility estimate (decision-time).

    Prefers an explicit field; else derives from recent 1-minute candle closes;
    else a conservative crypto default. Returns (value, present_flag).
    """
    v = num(_first(snap, "volatility_per_min", "vol_per_min", "realized_vol_per_min"))
    if v is not None and v > 0:
        return v, True
    closes = _first(snap, "recent_closes", "minute_closes", default=None)
    if isinstance(closes, (list, tuple)) and len(closes) >= 3:
        rets = []
        prev = num(closes[0])
        for c in closes[1:]:
            cur = num(c)
            if prev and cur and prev > 0:
                rets.append(math.log(cur / prev))
            prev = cur if cur else prev
        if len(rets) >= 2:
            m = sum(rets) / len(rets)
            var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
            sd = math.sqrt(max(0.0, var))
            if sd > 0:
                return sd, True
    return 0.004, False  # ~0.4%/min fallback; flagged absent


def extract(snap: Mapping[str, Any]) -> FeatureVector:
    """Build a FeatureVector from a decision-time snapshot dict.

    Robust to missing fields: each missing source datum yields a neutral value
    (0.0) with its coverage flag set False, so downstream code can penalise thin
    evidence instead of treating it as a clean reading.
    """
    spot = num(_first(snap, "spot", "spot_price", "current_price", "underlying_price"))
    target = num(_first(snap, "target", "target_price", "strike", "threshold"))
    seconds = num(_first(snap, "seconds_remaining", "time_remaining", "time_remaining_seconds"))
    vol, vol_present = _volatility_per_min(snap)

    minutes = (seconds / 60.0) if (seconds is not None and seconds > 0) else None
    sqrt_min = math.sqrt(minutes) if minutes else None

    # Distance-to-target family.
    if spot and target and spot > 0:
        raw_dist = target - spot
        pct_dist = raw_dist / spot
        log_dist = math.log(target / spot) if target > 0 else 0.0
        direction = 1.0 if raw_dist > 0 else (-1.0 if raw_dist < 0 else 0.0)
        dist_present = True
    else:
        raw_dist = pct_dist = log_dist = direction = 0.0
        dist_present = False

    # normalized_distance = (target - spot) / (spot * vol_per_min * sqrt(minutes))
    if dist_present and minutes and vol > 0 and spot:
        denom = spot * vol * sqrt_min
        norm_dist = (target - spot) / denom if denom else 0.0
        norm_present = vol_present  # value exists but is only trustworthy if vol was real
    else:
        norm_dist = 0.0
        norm_present = False

    # Prediction-market quote (the strongest single signal).
    yes_bid = num(_first(snap, "yes_bid", "yes_bid_cents"))
    yes_ask = num(_first(snap, "yes_ask", "yes_ask_cents"))
    no_bid = num(_first(snap, "no_bid", "no_bid_cents"))
    no_ask = num(_first(snap, "no_ask", "no_ask_cents"))
    market_prob = num(_first(snap, "market_implied_prob", "market_yes_prob"))
    if market_prob is None and yes_bid is not None and yes_ask is not None:
        market_prob = (yes_bid + yes_ask) / 200.0
    market_present = market_prob is not None
    market_prob = clamp(market_prob, 0.01, 0.99) if market_present else 0.5
    market_logit = math.log(market_prob / (1.0 - market_prob))

    if yes_bid is not None and yes_ask is not None:
        spread = max(0.0, yes_ask - yes_bid)
        spread_present = True
    else:
        spread = num(_first(snap, "spread_cents")) or 0.0
        spread_present = "spread_cents" in snap

    book_imb = num(_first(snap, "book_imbalance", "orderbook_imbalance"))
    book_present = book_imb is not None
    book_imb = book_imb if book_present else 0.0

    depth = num(_first(snap, "depth_contracts", "ask_depth", "executable_depth"))
    data_quality = num(_first(snap, "data_quality"))

    derived_vals = {
        "normalized_distance": (norm_dist, norm_present),
        "log_distance": (log_dist, dist_present),
        "pct_distance": (pct_dist, dist_present),
        "direction": (direction, dist_present),
        "minutes_remaining": ((minutes or 0.0), minutes is not None),
        "sqrt_minutes_remaining": ((sqrt_min or 0.0), sqrt_min is not None),
        "volatility_per_min": (vol, vol_present),
        "market_implied_prob": (market_prob, market_present),
        "market_logit": (market_logit, market_present),
        "contract_spread_cents": (spread, spread_present),
        "book_imbalance": (book_imb, book_present),
    }

    champ = _first(snap, "feature_values", default={}) or {}
    if not isinstance(champ, Mapping):
        champ = {}

    values: list[float] = []
    coverage: list[bool] = []
    details: dict[str, float] = {}
    for name in _DERIVED:
        v, present = derived_vals[name]
        values.append(float(v))
        coverage.append(bool(present))
        details[name] = float(v)
    for sig in _CHAMP_SIGNALS:
        cv = num(champ.get(sig))
        present = cv is not None
        v = cv if present else 0.0
        values.append(float(v))
        coverage.append(present)
        details[f"champ_{sig}"] = float(v)

    return FeatureVector(
        values=values,
        coverage=coverage,
        details=details,
        market_yes_prob=market_prob if market_present else None,
        yes_ask_cents=yes_ask,
        yes_bid_cents=yes_bid,
        no_ask_cents=no_ask,
        no_bid_cents=no_bid,
        spread_cents=spread if spread_present else None,
        depth_contracts=depth,
        seconds_remaining=seconds,
        data_quality=data_quality,
        spot=spot,
        target=target,
    )
