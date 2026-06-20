"""Q15 V9.4 adaptive 15-minute prediction and bounded online learning.

This module is a targeted successor to ``checkpoint_v92``.  It changes only
15-minute checkpoint output.  Ten-minute behavior is delegated to the stable V9.2 base without
modification.

Design goals
------------
* Produce a directional YES/NO estimate whenever critical spot/threshold/time
  data is valid, even when optional candles, flow, or order-book inputs are
  unavailable.
* Keep prediction quality separate from executable trade quality.
* Store exactly one 15-minute prediction per contract/model version.
* Reconcile only against officially settled outcomes already persisted by the
  monitor's settlement process.
* Apply a small, bounded online update after each newly resolved prediction.
* After at least ten unique resolved 15-minute predictions, compare feature
  patterns from correct and incorrect calls and apply a capped similarity
  adjustment.
* Never place, modify, or cancel orders.  Never auto-change entry thresholds.

The online learner is intentionally conservative.  One result cannot move any
feature coefficient by more than the configured per-result cap, and cumulative
coefficient drift is bounded relative to the frozen base model.
"""
from __future__ import annotations

import copy
from contextlib import closing
import json
import math
import os
import re
import sqlite3
import statistics
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, MutableMapping, Sequence

from .checkpoint_v92 import (
    CheckpointPolicyV92,
    format_telegram_message as _format_v92_message,
)

VERSION = "q15-v9.4-adaptive-15m-learning-pro-v2"
MODEL_VERSION = "q15-v9.4-adaptive-15m-model-v2"
FEATURE_SCHEMA_VERSION = "q15-v9.4-adaptive-15m-features-v2"
READ_ONLY = True

_BASE_WEIGHTS: dict[str, float] = {
    "intercept": 0.00,
    "threshold_distance": 1.10,
    "parent_probability": 0.75,
    "momentum_short": 0.45,
    "momentum_medium": 0.30,
    "executed_flow": 0.35,
    "book_pressure": 0.25,
    "wick_structure": 0.20,
    "cross_asset": 0.15,
    "market_prior": 0.25,
}

_FEATURE_QUALITY_WEIGHTS: dict[str, float] = {
    "threshold_distance": 0.32,
    "parent_probability": 0.15,
    "momentum_short": 0.11,
    "momentum_medium": 0.08,
    "executed_flow": 0.10,
    "book_pressure": 0.08,
    "wick_structure": 0.08,
    "cross_asset": 0.03,
    "market_prior": 0.05,
}

_LATEST_LOCK = threading.RLock()
_LATEST_ANALYSES: dict[str, dict[str, Any]] = {}
_LATEST_RANKING: list[dict[str, Any]] = []
_LATEST_LEARNING: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Configuration and numeric helpers
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    return max(low, min(high, value))


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return default
        parsed = float(value)
        return parsed if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    return numerator / denominator if abs(denominator) > 1e-15 else default


def _sigmoid(value: float) -> float:
    if value >= 0:
        term = math.exp(-value)
        return 1.0 / (1.0 + term)
    term = math.exp(value)
    return term / (1.0 + term)


def _logit(probability: float) -> float:
    probability = _clamp(probability, 1e-6, 1.0 - 1e-6)
    return math.log(probability / (1.0 - probability))


def _normalize_probability(value: Any) -> float | None:
    parsed = _num(value)
    if parsed is None:
        return None
    if 1.0 < parsed <= 100.0:
        parsed /= 100.0
    if 0.0 <= parsed <= 1.0:
        return parsed
    return None


def _normalize_cents(value: Any) -> float | None:
    parsed = _num(value)
    if parsed is None:
        return None
    if 0.0 <= parsed <= 1.000001:
        parsed *= 100.0
    return parsed if 0.0 <= parsed <= 100.0 else None


def _parse_ts(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed = float(value)
        if parsed > 10_000_000_000:
            parsed /= 1000.0
        return parsed if math.isfinite(parsed) else None
    text = str(value).strip()
    if not text:
        return None
    numeric = _num(text)
    if numeric is not None:
        return numeric / 1000.0 if numeric > 10_000_000_000 else numeric
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _iso(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _asset_name(asset: Any, snapshot: Mapping[str, Any]) -> str:
    value = str(snapshot.get("asset") or asset or "UNKNOWN").strip().upper()
    return {"XBT": "BTC", "XDG": "DOGE"}.get(value, value or "UNKNOWN")


# ---------------------------------------------------------------------------
# Robust field and candle extraction
# ---------------------------------------------------------------------------

def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")


def _walk(node: Any, depth: int = 0) -> Iterable[tuple[str, Any]]:
    if depth > 5:
        return
    if isinstance(node, Mapping):
        for key, value in node.items():
            normalized = _normalized_key(key)
            yield normalized, value
            if isinstance(value, (Mapping, list, tuple)):
                yield from _walk(value, depth + 1)
    elif isinstance(node, (list, tuple)):
        for value in node[:250]:
            if isinstance(value, (Mapping, list, tuple)):
                yield from _walk(value, depth + 1)


def _first_value(snapshot: Mapping[str, Any], aliases: Sequence[str]) -> Any:
    normalized_aliases = tuple(_normalized_key(alias) for alias in aliases)
    for alias in aliases:
        if alias in snapshot and snapshot.get(alias) is not None:
            return snapshot.get(alias)
    exact: list[Any] = []
    partial: list[Any] = []
    for key, value in _walk(snapshot):
        if value is None:
            continue
        if key in normalized_aliases:
            exact.append(value)
        elif any(alias in key for alias in normalized_aliases):
            partial.append(value)
    return exact[0] if exact else partial[0] if partial else None


def _first_num(snapshot: Mapping[str, Any], aliases: Sequence[str]) -> float | None:
    direct = _first_value(snapshot, aliases)
    return _num(direct)


def _first_probability(snapshot: Mapping[str, Any], aliases: Sequence[str]) -> float | None:
    direct = _first_value(snapshot, aliases)
    return _normalize_probability(direct)


def _first_cents(snapshot: Mapping[str, Any], aliases: Sequence[str]) -> float | None:
    direct = _first_value(snapshot, aliases)
    return _normalize_cents(direct)


def _seconds_remaining(snapshot: Mapping[str, Any], now: float) -> float | None:
    seconds = _first_num(
        snapshot,
        ("seconds_remaining", "remaining_seconds", "time_remaining_seconds", "secs_remaining"),
    )
    if seconds is not None:
        return max(0.0, seconds)
    close_value = _first_value(
        snapshot,
        ("close_time", "market_close", "contract_close", "expiration_time", "end_time"),
    )
    close_ts = _parse_ts(close_value)
    return None if close_ts is None else max(0.0, close_ts - now)


def _close_time(snapshot: Mapping[str, Any], now: float) -> float | None:
    close_value = _first_value(
        snapshot,
        ("close_time", "market_close", "contract_close", "expiration_time", "end_time"),
    )
    close_ts = _parse_ts(close_value)
    if close_ts is not None:
        return close_ts
    remaining = _seconds_remaining(snapshot, now)
    return None if remaining is None else now + remaining


def _spot(snapshot: Mapping[str, Any]) -> float | None:
    return _first_num(
        snapshot,
        (
            "underlying_current", "current_underlying", "underlying_price", "spot_price",
            "reference_price", "index_price", "current_price", "last_price", "price",
        ),
    )


def _target(snapshot: Mapping[str, Any]) -> float | None:
    return _first_num(
        snapshot,
        ("target_value", "floor_strike", "strike", "threshold", "settlement_threshold", "target"),
    )


def _yes_is_higher(snapshot: Mapping[str, Any]) -> bool:
    text = str(
        _first_value(snapshot, ("target_type", "strike_type", "comparison", "market_direction"))
        or "greater_or_equal"
    ).lower()
    return not any(token in text for token in ("less", "below", "under", "lower", "at_most"))


def _ticker(snapshot: Mapping[str, Any]) -> str | None:
    value = _first_value(snapshot, ("ticker", "market_ticker", "contract_ticker", "symbol"))
    text = str(value).strip() if value is not None else ""
    return text or None


def _normalize_candle(row: Mapping[str, Any]) -> dict[str, float] | None:
    open_value = _first_num(row, ("open", "o"))
    high = _first_num(row, ("high", "h"))
    low = _first_num(row, ("low", "l"))
    close = _first_num(row, ("close", "c", "last"))
    timestamp = _parse_ts(_first_value(row, ("end_time", "timestamp", "time", "ts", "start_time")))
    if None in (open_value, high, low, close):
        return None
    if high < low or high < max(open_value, close) or low > min(open_value, close):
        return None
    return {
        "open": float(open_value),
        "high": float(high),
        "low": float(low),
        "close": float(close),
        "timestamp": float(timestamp) if timestamp is not None else 0.0,
    }


def _extract_candles(snapshot: Mapping[str, Any]) -> list[dict[str, float]]:
    preferred = (
        "underlying_candles_5s", "underlying_candles", "candles_5s", "candles",
        "ohlc", "ohlcv", "bars", "price_candles", "chart_candles",
    )
    candidates: list[Any] = []
    for key in preferred:
        value = snapshot.get(key)
        if isinstance(value, (list, tuple)):
            candidates.append(value)
    if not candidates:
        for key, value in _walk(snapshot):
            if isinstance(value, (list, tuple)) and any(
                token in key for token in ("candle", "ohlc", "bar")
            ):
                candidates.append(value)
    best: list[dict[str, float]] = []
    for candidate in candidates:
        normalized = [
            candle for row in candidate[-400:]
            if isinstance(row, Mapping) and (candle := _normalize_candle(row)) is not None
        ]
        if len(normalized) > len(best):
            best = normalized
    if best and any(row["timestamp"] for row in best):
        best.sort(key=lambda row: row["timestamp"])
    return best


def _returns(candles: Sequence[Mapping[str, float]]) -> list[float]:
    values: list[float] = []
    for previous, current in zip(candles, candles[1:]):
        a = _num(previous.get("close"))
        b = _num(current.get("close"))
        if a is not None and b is not None and a > 0 and b > 0:
            values.append(math.log(b / a))
    return values


def _candle_cadence(candles: Sequence[Mapping[str, float]]) -> float:
    times = [float(row.get("timestamp") or 0.0) for row in candles if row.get("timestamp")]
    gaps = [b - a for a, b in zip(times, times[1:]) if 0.1 <= b - a <= 300]
    return statistics.median(gaps) if gaps else 5.0


def _window_return(candles: Sequence[Mapping[str, float]], count: int) -> float | None:
    if len(candles) < 2:
        return None
    subset = candles[-max(2, min(len(candles), count)) :]
    start = _num(subset[0].get("close"))
    end = _num(subset[-1].get("close"))
    if start is None or end is None or start <= 0:
        return None
    return end / start - 1.0


def _wick_score(candles: Sequence[Mapping[str, float]], yes_is_higher: bool) -> float | None:
    if not candles:
        return None
    rows = list(candles[-3:])
    weighted = 0.0
    total_weight = 0.0
    for index, row in enumerate(rows, 1):
        open_value = float(row["open"])
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        span = max(high - low, 1e-12)
        upper = max(0.0, high - max(open_value, close)) / span
        lower = max(0.0, min(open_value, close) - low) / span
        body_direction = _clamp((close - open_value) / span, -1.0, 1.0)
        raw = (lower - upper) * 0.70 + body_direction * 0.30
        if not yes_is_higher:
            raw *= -1.0
        weight = float(index)
        weighted += _clamp(raw, -1.0, 1.0) * weight
        total_weight += weight
    return _safe_div(weighted, total_weight)


# ---------------------------------------------------------------------------
# Continuous feature extraction
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FeatureValue:
    value: float
    available: bool
    quality: float
    source: str
    details: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "available": self.available,
            "quality": self.quality,
            "source": self.source,
            "details": dict(self.details),
        }


def _missing(source: str, **details: Any) -> FeatureValue:
    return FeatureValue(0.0, False, 0.0, source, details)


def _threshold_feature(
    snapshot: Mapping[str, Any], candles: Sequence[Mapping[str, float]], now: float
) -> tuple[FeatureValue, dict[str, Any]]:
    spot = _spot(snapshot)
    target = _target(snapshot)
    seconds = _seconds_remaining(snapshot, now)
    snapshot_ts = _parse_ts(_first_value(
        snapshot,
        ("snapshot_time", "snapshot_timestamp", "observed_at", "as_of", "updated_at", "timestamp"),
    ))
    snapshot_age = None if snapshot_ts is None else max(0.0, now - snapshot_ts)
    max_core_age = _env_float("Q15_V94_15M_MAX_CORE_AGE_SECONDS", 30.0, 2.0, 300.0)
    core = {
        "spot": spot,
        "target": target,
        "seconds_remaining": seconds,
        "yes_is_higher": _yes_is_higher(snapshot),
        "snapshot_timestamp": snapshot_ts,
        "snapshot_age_seconds": snapshot_age,
        "maximum_core_age_seconds": max_core_age,
    }
    if spot is None or target is None or seconds is None or spot <= 0 or target <= 0:
        return _missing("core", reason="missing_spot_target_or_time"), core
    if snapshot_age is not None and snapshot_age > max_core_age:
        return _missing("core", reason="stale_core_snapshot", snapshot_age_seconds=snapshot_age), core

    direction = 1.0 if core["yes_is_higher"] else -1.0
    signed_distance = direction * (spot - target)
    candle_returns = _returns(candles[-180:])
    cadence = _candle_cadence(candles)
    volatility = statistics.pstdev(candle_returns) if len(candle_returns) >= 3 else None
    explicit_vol = _first_num(
        snapshot,
        ("realized_volatility", "volatility", "ewma_volatility", "short_volatility"),
    )
    expected_move: float
    volatility_source: str
    if volatility is not None and volatility > 1e-9:
        expected_move = spot * volatility * math.sqrt(max(seconds, cadence) / cadence)
        volatility_source = "candles"
    elif explicit_vol is not None and explicit_vol > 0:
        # Treat small values as fractional volatility over the current window;
        # larger values as an absolute expected move.
        expected_move = spot * explicit_vol if explicit_vol < 1.0 else explicit_vol
        volatility_source = "snapshot"
    else:
        # A fallback scale allows a directional estimate without fabricating a
        # precise probability.  Quality is reduced substantially below.
        expected_move = max(abs(spot - target), spot * 0.0015, 1e-9)
        volatility_source = "fallback_scale"
    z_score = _clamp(signed_distance / max(expected_move, 1e-9), -4.0, 4.0)
    feature_value = _clamp(z_score / 2.5, -1.0, 1.0)
    quality = 1.0 if volatility_source == "candles" else 0.75 if volatility_source == "snapshot" else 0.45
    return FeatureValue(
        feature_value,
        True,
        quality,
        "threshold_distance_time_volatility",
        {
            **core,
            "signed_distance": signed_distance,
            "expected_move": expected_move,
            "z_score": z_score,
            "volatility_source": volatility_source,
            "candle_count": len(candles),
            "candle_cadence_seconds": cadence,
        },
    ), core


def _parent_probability_feature(snapshot: Mapping[str, Any]) -> FeatureValue:
    probability = _first_probability(
        snapshot,
        (
            "fair_yes_probability", "model_yes_probability", "yes_probability",
            "chart_yes_probability", "blended_yes_probability", "fair_prob",
            "q15_probability_yes", "probability_yes", "model_probability",
        ),
    )
    if probability is None:
        return _missing("parent_probability")
    value = _clamp(_logit(probability) / 3.0, -1.0, 1.0)
    return FeatureValue(value, True, 0.85, "parent_probability", {"yes_probability": probability})


def _momentum_features(
    snapshot: Mapping[str, Any], candles: Sequence[Mapping[str, float]], yes_is_higher: bool
) -> tuple[FeatureValue, FeatureValue]:
    short_return = _first_num(
        snapshot,
        ("return_15s", "spot_return_15s", "momentum_15s", "return_30s", "spot_momentum"),
    )
    medium_return = _first_num(
        snapshot,
        ("return_60s", "spot_return_60s", "momentum_60s", "return_180s", "trend_return"),
    )
    cadence = _candle_cadence(candles)
    if short_return is None and len(candles) >= 2:
        short_return = _window_return(candles, max(2, round(30.0 / cadence) + 1))
    if medium_return is None and len(candles) >= 2:
        medium_return = _window_return(candles, max(2, round(180.0 / cadence) + 1))
    direction = 1.0 if yes_is_higher else -1.0

    def convert(value: float | None, source: str) -> FeatureValue:
        if value is None:
            return _missing(source)
        # 0.30% over a short crypto window maps near the feature cap.
        normalized = _clamp(direction * value / 0.0030, -1.0, 1.0)
        return FeatureValue(normalized, True, 0.75, source, {"return": value})

    return convert(short_return, "short_momentum"), convert(medium_return, "medium_momentum")


def _executed_flow_feature(snapshot: Mapping[str, Any], yes_is_higher: bool) -> FeatureValue:
    yes_volume = _first_num(
        snapshot,
        ("taker_yes_volume_15s", "taker_buy_volume", "aggressive_buy_volume", "buy_volume"),
    )
    no_volume = _first_num(
        snapshot,
        ("taker_no_volume_15s", "taker_sell_volume", "aggressive_sell_volume", "sell_volume"),
    )
    if yes_volume is not None and no_volume is not None and yes_volume + no_volume > 0:
        imbalance = (yes_volume - no_volume) / (yes_volume + no_volume)
        return FeatureValue(
            _clamp(imbalance, -1.0, 1.0),
            True,
            0.90,
            "explicit_taker_flow",
            {"yes_volume": yes_volume, "no_volume": no_volume, "imbalance": imbalance},
        )

    trades = _first_value(snapshot, ("recent_trades", "trades", "executions"))
    if isinstance(trades, (list, tuple)):
        supportive = opposing = 0.0
        observed = 0
        for trade in trades[-250:]:
            if not isinstance(trade, Mapping):
                continue
            size = _first_num(trade, ("size", "quantity", "count", "volume")) or 1.0
            side_text = str(
                _first_value(trade, ("taker_outcome_side", "taker_book_side", "side", "direction"))
                or ""
            ).upper()
            if side_text in {"YES", "BUY", "BID", "BULLISH"}:
                supportive += size
                observed += 1
            elif side_text in {"NO", "SELL", "ASK", "BEARISH"}:
                opposing += size
                observed += 1
        total = supportive + opposing
        if total > 0:
            imbalance = (supportive - opposing) / total
            return FeatureValue(
                _clamp(imbalance, -1.0, 1.0),
                True,
                _clamp(observed / 20.0, 0.35, 0.85),
                "recent_trades",
                {"supportive": supportive, "opposing": opposing, "trade_count": observed},
            )
    return _missing("executed_flow")


def _book_feature(snapshot: Mapping[str, Any]) -> FeatureValue:
    imbalance = _first_num(
        snapshot,
        ("orderbook_imbalance", "book_imbalance", "depth_imbalance", "persistent_book_pressure"),
    )
    if imbalance is not None:
        if abs(imbalance) > 1.0 and abs(imbalance) <= 100.0:
            imbalance /= 100.0
        return FeatureValue(
            _clamp(imbalance, -1.0, 1.0), True, 0.80, "book_imbalance", {"imbalance": imbalance}
        )
    bid_depth = _first_num(snapshot, ("bid_depth", "yes_bid_depth", "bid_liquidity", "buy_depth"))
    ask_depth = _first_num(snapshot, ("ask_depth", "yes_ask_depth", "ask_liquidity", "sell_depth"))
    if bid_depth is not None and ask_depth is not None and bid_depth + ask_depth > 0:
        value = (bid_depth - ask_depth) / (bid_depth + ask_depth)
        return FeatureValue(
            _clamp(value, -1.0, 1.0),
            True,
            0.65,
            "top_book_depth",
            {"bid_depth": bid_depth, "ask_depth": ask_depth},
        )
    return _missing("book_pressure")


def _cross_asset_feature(snapshot: Mapping[str, Any]) -> FeatureValue:
    value = _first_num(
        snapshot,
        ("cross_asset_score", "broader_market_score", "market_breadth", "leader_confirmation"),
    )
    if value is None:
        return _missing("cross_asset")
    if abs(value) > 1.0 and abs(value) <= 100.0:
        value /= 100.0
    return FeatureValue(_clamp(value, -1.0, 1.0), True, 0.55, "cross_asset", {"score": value})


def _market_prior_feature(snapshot: Mapping[str, Any]) -> FeatureValue:
    yes_bid = _first_cents(snapshot, ("yes_bid", "yes_bid_cents", "best_yes_bid"))
    yes_ask = _first_cents(snapshot, ("yes_ask", "yes_ask_cents", "best_yes_ask"))
    if yes_bid is None or yes_ask is None or yes_ask < yes_bid:
        return _missing("market_prior")
    mid = (yes_bid + yes_ask) / 200.0
    return FeatureValue(
        _clamp(_logit(_clamp(mid, 0.01, 0.99)) / 4.0, -1.0, 1.0),
        True,
        0.65,
        "kalshi_mid_once",
        {"yes_bid_cents": yes_bid, "yes_ask_cents": yes_ask, "mid_probability": mid},
    )


def extract_features(snapshot: Mapping[str, Any], now: float) -> dict[str, Any]:
    candles = _extract_candles(snapshot)
    threshold, core = _threshold_feature(snapshot, candles, now)
    yes_is_higher = bool(core.get("yes_is_higher", True))
    short_momentum, medium_momentum = _momentum_features(snapshot, candles, yes_is_higher)
    wick_value = _wick_score(candles, yes_is_higher)
    wick = (
        FeatureValue(wick_value, True, _clamp(len(candles) / 12.0, 0.35, 0.90), "candles", {"candle_count": len(candles)})
        if wick_value is not None
        else _missing("wick_structure", candle_count=len(candles))
    )
    feature_objects: dict[str, FeatureValue] = {
        "threshold_distance": threshold,
        "parent_probability": _parent_probability_feature(snapshot),
        "momentum_short": short_momentum,
        "momentum_medium": medium_momentum,
        "executed_flow": _executed_flow_feature(snapshot, yes_is_higher),
        "book_pressure": _book_feature(snapshot),
        "wick_structure": wick,
        "cross_asset": _cross_asset_feature(snapshot),
        "market_prior": _market_prior_feature(snapshot),
    }
    quality = 0.0
    coverage = 0.0
    for name, configured_weight in _FEATURE_QUALITY_WEIGHTS.items():
        feature = feature_objects[name]
        if feature.available:
            coverage += configured_weight
            quality += configured_weight * _clamp(feature.quality, 0.0, 1.0)
    quality = _clamp(quality, 0.0, 1.0)
    return {
        "core_valid": threshold.available,
        "core": core,
        "candles": candles,
        "candle_count": len(candles),
        "values": {name: feature.value for name, feature in feature_objects.items()},
        "available": {name: feature.available for name, feature in feature_objects.items()},
        "details": {name: feature.as_dict() for name, feature in feature_objects.items()},
        "data_quality": quality,
        "evidence_coverage": _clamp(coverage, 0.0, 1.0),
    }


# ---------------------------------------------------------------------------
# Persistent bounded online learner
# ---------------------------------------------------------------------------

class Adaptive15LearningStore:
    """SQLite-backed, version-scoped online learner for unique 15M contracts."""

    def __init__(self, path: str | Path | None = None) -> None:
        default_path = Path(__file__).resolve().parent.parent / "data" / "q15_v94_adaptive15_pro_v2.sqlite3"
        # V2 uses a fresh, version-scoped database because its reliability-weighted
        # feature schema is not mathematically interchangeable with V1 weights.
        configured = path or os.environ.get("Q15_V94_15M_LEARNING_DB_V2") or default_path
        self.path = Path(configured).expanduser().resolve()
        self.enabled = _env_bool("Q15_V94_15M_ONLINE_LEARNING", True)
        self.learning_rate = _env_float("Q15_V94_15M_LEARNING_RATE", 0.060, 0.001, 0.25)
        self.per_result_cap = _env_float("Q15_V94_15M_MAX_WEIGHT_DELTA", 0.025, 0.001, 0.10)
        self.total_drift_cap = _env_float("Q15_V94_15M_MAX_WEIGHT_DRIFT", 0.50, 0.05, 1.50)
        self.min_learning_quality = _env_float("Q15_V94_15M_MIN_LEARNING_QUALITY", 0.45, 0.10, 0.95)
        self.l2_pull = _env_float("Q15_V94_15M_L2_PULL", 0.0025, 0.0, 0.05)
        self.pattern_minimum = _env_int("Q15_V94_15M_PATTERN_MIN_RESOLVED", 10, 10, 1000)
        self.pattern_cap_probability = _env_float("Q15_V94_15M_PATTERN_CAP", 0.05, 0.0, 0.10)
        self._lock = threading.RLock()
        self._available = True
        self._last_error: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()
        except Exception as exc:  # predictions continue even if persistence fails
            self._available = False
            self._last_error = f"{type(exc).__name__}: {exc}"

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS adaptive15_weights (
                    name TEXT PRIMARY KEY,
                    value REAL NOT NULL,
                    base_value REAL NOT NULL,
                    grad_sq REAL NOT NULL DEFAULT 0,
                    updates INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS adaptive15_predictions (
                    prediction_id TEXT PRIMARY KEY,
                    ticker TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    predicted_side TEXT NOT NULL,
                    yes_probability REAL NOT NULL,
                    selected_probability REAL NOT NULL,
                    data_quality REAL NOT NULL,
                    feature_json TEXT NOT NULL,
                    available_json TEXT NOT NULL,
                    weights_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    close_time REAL,
                    model_version TEXT NOT NULL,
                    resolved_at REAL,
                    official_result TEXT,
                    correct INTEGER,
                    learning_applied INTEGER NOT NULL DEFAULT 0,
                    brier REAL
                );
                CREATE INDEX IF NOT EXISTS idx_adaptive15_pending
                    ON adaptive15_predictions(learning_applied, ticker);
                CREATE INDEX IF NOT EXISTS idx_adaptive15_resolved
                    ON adaptive15_predictions(resolved_at);
                CREATE TABLE IF NOT EXISTS adaptive15_updates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prediction_id TEXT NOT NULL UNIQUE,
                    result TEXT NOT NULL,
                    correct INTEGER NOT NULL,
                    before_json TEXT NOT NULL,
                    after_json TEXT NOT NULL,
                    delta_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS adaptive15_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            now = time.time()
            for name, base in _BASE_WEIGHTS.items():
                connection.execute(
                    """INSERT OR IGNORE INTO adaptive15_weights
                       (name, value, base_value, grad_sq, updates, updated_at)
                       VALUES (?, ?, ?, 0, 0, ?)""",
                    (name, base, base, now),
                )
            connection.commit()

    def _meta(self, connection: sqlite3.Connection, key: str, default: str = "") -> str:
        row = connection.execute(
            "SELECT value FROM adaptive15_meta WHERE key=?", (key,)
        ).fetchone()
        return str(row["value"]) if row else default

    def _set_meta(self, connection: sqlite3.Connection, key: str, value: Any) -> None:
        connection.execute(
            """INSERT INTO adaptive15_meta(key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (key, str(value), time.time()),
        )

    def weights(self) -> dict[str, float]:
        if not self._available:
            return dict(_BASE_WEIGHTS)
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT name, value FROM adaptive15_weights ORDER BY name"
            ).fetchall()
        values = {str(row["name"]): float(row["value"]) for row in rows}
        return {name: values.get(name, base) for name, base in _BASE_WEIGHTS.items()}

    def record_prediction(
        self,
        *,
        ticker: str,
        asset: str,
        side: str,
        yes_probability: float,
        selected_probability: float,
        data_quality: float,
        features: Mapping[str, float],
        available: Mapping[str, bool],
        weights: Mapping[str, float],
        created_at: float,
        close_time: float | None,
        training_yes_probability: float | None = None,
    ) -> tuple[str, bool]:
        prediction_id = f"{MODEL_VERSION}|15M|{ticker}"
        if not self._available:
            return prediction_id, False
        feature_payload: dict[str, Any] = dict(features)
        normalized_training_probability = _normalize_probability(training_yes_probability)
        if normalized_training_probability is not None:
            feature_payload["__training_yes_probability"] = normalized_training_probability
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO adaptive15_predictions
                   (prediction_id, ticker, asset, predicted_side, yes_probability,
                    selected_probability, data_quality, feature_json, available_json,
                    weights_json, created_at, close_time, model_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    prediction_id, ticker, asset, side, yes_probability,
                    selected_probability, data_quality, _json_dumps(feature_payload),
                    _json_dumps(dict(available)), _json_dumps(dict(weights)),
                    created_at, close_time, MODEL_VERSION,
                ),
            )
            connection.commit()
            inserted = cursor.rowcount == 1
        return prediction_id, inserted

    @staticmethod
    def _official_result_from_signal(row: Mapping[str, Any]) -> str | None:
        side = str(row.get("side") or "").upper()
        outcome = str(row.get("outcome") or "").lower()
        if side not in {"YES", "NO"} or outcome not in {"win", "loss"}:
            return None
        if outcome == "win":
            return side
        return "NO" if side == "YES" else "YES"

    def reconcile_from_signal_store(self, signal_store: Any) -> dict[str, Any]:
        if not self._available:
            return {"available": False, "reason": self._last_error or "learning_store_unavailable"}
        query = getattr(signal_store, "query", None)
        if not callable(query):
            return {"available": False, "reason": "signal_store_query_unavailable"}
        try:
            rows = query(
                "SELECT ticker, asset, side, outcome, settled_at FROM signals "
                "WHERE ticker IS NOT NULL AND side IN ('YES','NO') "
                "AND outcome IN ('win','loss') ORDER BY settled_at DESC LIMIT 2500"
            ) or []
        except Exception as exc:
            self._last_error = f"signal_reconcile:{type(exc).__name__}: {exc}"
            return {"available": False, "reason": self._last_error}
        newest_by_ticker: dict[str, Mapping[str, Any]] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            ticker = str(row.get("ticker") or "").strip()
            if ticker and ticker not in newest_by_ticker:
                newest_by_ticker[ticker] = row
        resolved = applied = 0
        for ticker, row in newest_by_ticker.items():
            official = self._official_result_from_signal(row)
            if official is None:
                continue
            outcome_time = _parse_ts(row.get("settled_at")) or time.time()
            result = self.resolve_ticker(ticker, official, outcome_time)
            resolved += int(result.get("resolved", 0))
            applied += int(result.get("updates_applied", 0))
        return {
            "available": True,
            "signal_rows_examined": len(rows),
            "unique_settled_tickers": len(newest_by_ticker),
            "new_predictions_resolved": resolved,
            "updates_applied": applied,
        }

    def resolve_ticker(self, ticker: str, official_result: str, resolved_at: float | None = None) -> dict[str, int]:
        official_result = str(official_result).upper()
        if official_result not in {"YES", "NO"} or not self._available:
            return {"resolved": 0, "updates_applied": 0}
        resolved_at = resolved_at or time.time()
        resolved = applied = 0
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT * FROM adaptive15_predictions
                   WHERE ticker=? AND official_result IS NULL ORDER BY created_at ASC""",
                (ticker,),
            ).fetchall()
            for row in rows:
                predicted_side = str(row["predicted_side"])
                correct = int(predicted_side == official_result)
                yes_probability = float(row["yes_probability"])
                actual_yes = 1.0 if official_result == "YES" else 0.0
                brier = (yes_probability - actual_yes) ** 2
                connection.execute(
                    """UPDATE adaptive15_predictions SET resolved_at=?, official_result=?,
                       correct=?, brier=? WHERE prediction_id=?""",
                    (resolved_at, official_result, correct, brier, row["prediction_id"]),
                )
                resolved += 1
            connection.commit()
        # Weight updates use separate transactions so each prediction update is
        # atomic and idempotent through adaptive15_updates.prediction_id UNIQUE.
        for row in rows:
            if self._apply_learning_for_prediction(str(row["prediction_id"])):
                applied += 1
        self._refresh_performance_guard()
        return {"resolved": resolved, "updates_applied": applied}

    def _apply_learning_for_prediction(self, prediction_id: str) -> bool:
        if not self.enabled or not self._available:
            return False
        with self._lock, closing(self._connect()) as connection:
            if self._meta(connection, "learning_paused", "0") == "1":
                return False
            row = connection.execute(
                "SELECT * FROM adaptive15_predictions WHERE prediction_id=?", (prediction_id,)
            ).fetchone()
            if row is None or row["official_result"] not in {"YES", "NO"}:
                return False
            if int(row["learning_applied"] or 0) == 1:
                return False
            if float(row["data_quality"] or 0.0) < self.min_learning_quality:
                connection.execute(
                    "UPDATE adaptive15_predictions SET learning_applied=-1 WHERE prediction_id=?",
                    (prediction_id,),
                )
                connection.commit()
                return False
            if connection.execute(
                "SELECT 1 FROM adaptive15_updates WHERE prediction_id=?", (prediction_id,)
            ).fetchone():
                connection.execute(
                    "UPDATE adaptive15_predictions SET learning_applied=1 WHERE prediction_id=?",
                    (prediction_id,),
                )
                connection.commit()
                return False

            features = json.loads(str(row["feature_json"]))
            available = json.loads(str(row["available_json"]))
            weight_rows = connection.execute(
                "SELECT * FROM adaptive15_weights ORDER BY name"
            ).fetchall()
            before = {str(item["name"]): float(item["value"]) for item in weight_rows}
            weight_meta = {str(item["name"]): item for item in weight_rows}
            actual_yes = 1.0 if row["official_result"] == "YES" else 0.0
            probability = _normalize_probability(features.get("__training_yes_probability"))
            if probability is None:
                probability = float(row["yes_probability"])
            error = actual_yes - probability
            quality = _clamp(float(row["data_quality"] or 0.0), 0.0, 1.0)
            quality_span = max(1e-9, 1.0 - self.min_learning_quality)
            quality_factor = _clamp((quality - self.min_learning_quality) / quality_span, 0.0, 1.0)
            sample_weight = 0.35 + 0.65 * quality_factor
            deltas: dict[str, float] = {}
            after = dict(before)
            for name, base in _BASE_WEIGHTS.items():
                x = 1.0 if name == "intercept" else float(features.get(name, 0.0))
                if name != "intercept" and not bool(available.get(name, False)):
                    continue
                meta = weight_meta.get(name)
                grad_sq = float(meta["grad_sq"] if meta is not None else 0.0)
                current = before.get(name, base)
                regularization = self.l2_pull * (current - base)
                gradient = sample_weight * error * x - regularization
                next_grad_sq = grad_sq + gradient * gradient
                adaptive_step = self.learning_rate * gradient / math.sqrt(next_grad_sq + 1.0)
                delta = _clamp(adaptive_step, -self.per_result_cap, self.per_result_cap)
                low = base - self.total_drift_cap
                high = base + self.total_drift_cap
                next_value = _clamp(current + delta, low, high)
                actual_delta = next_value - current
                if abs(actual_delta) < 1e-15:
                    continue
                deltas[name] = actual_delta
                after[name] = next_value
                connection.execute(
                    """UPDATE adaptive15_weights SET value=?, grad_sq=?, updates=updates+1,
                       updated_at=? WHERE name=?""",
                    (next_value, next_grad_sq, time.time(), name),
                )
            correct = int(row["correct"] or 0)
            connection.execute(
                """INSERT INTO adaptive15_updates
                   (prediction_id, result, correct, before_json, after_json, delta_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    prediction_id, str(row["official_result"]), correct,
                    _json_dumps(before), _json_dumps(after), _json_dumps(deltas), time.time(),
                ),
            )
            connection.execute(
                "UPDATE adaptive15_predictions SET learning_applied=1 WHERE prediction_id=?",
                (prediction_id,),
            )
            self._set_meta(connection, "last_update_prediction_id", prediction_id)
            self._set_meta(connection, "last_update_at", time.time())
            self._set_meta(connection, "last_training_probability", probability)
            self._set_meta(connection, "last_learning_sample_weight", sample_weight)
            connection.commit()
            return True

    def _refresh_performance_guard(self) -> None:
        if not self._available:
            return
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT brier FROM adaptive15_predictions
                   WHERE brier IS NOT NULL ORDER BY resolved_at DESC LIMIT 20"""
            ).fetchall()
            if len(rows) < 20:
                return
            newest = [float(row["brier"]) for row in rows[:10]]
            older = [float(row["brier"]) for row in rows[10:20]]
            recent_mean = statistics.mean(newest)
            prior_mean = statistics.mean(older)
            paused = recent_mean > prior_mean * 1.20 and recent_mean - prior_mean > 0.06
            if paused:
                self._set_meta(connection, "learning_paused", "1")
                self._set_meta(
                    connection,
                    "learning_pause_reason",
                    f"trailing_brier_worsened:{prior_mean:.4f}->{recent_mean:.4f}",
                )
            connection.commit()

    def pattern_adjustment(
        self,
        features: Mapping[str, float],
        available: Mapping[str, bool],
        selected_side: str,
    ) -> dict[str, Any]:
        if not self._available:
            return {"active": False, "adjustment_probability": 0.0, "reason": "store_unavailable"}
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT predicted_side, correct, feature_json, available_json, data_quality, resolved_at
                   FROM adaptive15_predictions WHERE correct IN (0,1)
                   ORDER BY resolved_at DESC LIMIT 500"""
            ).fetchall()
        if len(rows) < self.pattern_minimum:
            return {
                "active": False,
                "adjustment_probability": 0.0,
                "reason": "need_more_unique_resolved_15m_predictions",
                "resolved": len(rows),
                "minimum": self.pattern_minimum,
            }
        winners: list[dict[str, float]] = []
        losers: list[dict[str, float]] = []
        for row in rows:
            row_features = json.loads(str(row["feature_json"]))
            row_available = json.loads(str(row["available_json"]))
            direction = 1.0 if str(row["predicted_side"]) == "YES" else -1.0
            aligned = {
                name: direction * float(row_features.get(name, 0.0))
                for name in _FEATURE_QUALITY_WEIGHTS
                if bool(row_available.get(name, False))
            }
            aligned["__sample_weight"] = _clamp(float(row["data_quality"] or 0.0), 0.25, 1.0)
            (winners if int(row["correct"]) == 1 else losers).append(aligned)
        if len(winners) < 3 or len(losers) < 3:
            return {
                "active": False,
                "adjustment_probability": 0.0,
                "reason": "need_at_least_three_winners_and_three_losers",
                "resolved": len(rows),
                "winners": len(winners),
                "losers": len(losers),
            }

        def centroid(group: Sequence[Mapping[str, float]]) -> dict[str, float]:
            result: dict[str, float] = {}
            for name in _FEATURE_QUALITY_WEIGHTS:
                weighted_values = [
                    (float(item[name]), float(item.get("__sample_weight", 1.0)))
                    for item in group if name in item
                ]
                if weighted_values:
                    total_weight = sum(weight for _, weight in weighted_values)
                    weighted_mean = _safe_div(
                        sum(value * weight for value, weight in weighted_values),
                        total_weight,
                        0.0,
                    )
                    # Shrink small effective samples toward zero.
                    shrink = total_weight / (total_weight + 8.0)
                    result[name] = weighted_mean * shrink
            return result

        winner_centroid = centroid(winners)
        loser_centroid = centroid(losers)
        direction = 1.0 if selected_side == "YES" else -1.0
        current = {
            name: direction * float(features.get(name, 0.0))
            for name in _FEATURE_QUALITY_WEIGHTS
            if bool(available.get(name, False))
        }

        def cosine(left: Mapping[str, float], right: Mapping[str, float]) -> float:
            names = set(left).intersection(right)
            if not names:
                return 0.0
            dot = sum(left[name] * right[name] for name in names)
            left_norm = math.sqrt(sum(left[name] ** 2 for name in names))
            right_norm = math.sqrt(sum(right[name] ** 2 for name in names))
            return _safe_div(dot, left_norm * right_norm, 0.0)

        win_similarity = cosine(current, winner_centroid)
        loss_similarity = cosine(current, loser_centroid)
        raw = (win_similarity - loss_similarity) * self.pattern_cap_probability
        adjustment = _clamp(raw, -self.pattern_cap_probability, self.pattern_cap_probability)
        return {
            "active": True,
            "adjustment_probability": adjustment,
            "winner_similarity": win_similarity,
            "loser_similarity": loss_similarity,
            "resolved": len(rows),
            "winners": len(winners),
            "losers": len(losers),
            "winner_centroid": winner_centroid,
            "loser_centroid": loser_centroid,
            "cap_probability_points": self.pattern_cap_probability,
        }

    def status(self) -> dict[str, Any]:
        if not self._available:
            return {
                "version": VERSION,
                "available": False,
                "enabled": self.enabled,
                "error": self._last_error,
                "path": str(self.path),
            }
        with self._lock, closing(self._connect()) as connection:
            counts = connection.execute(
                """SELECT COUNT(*) AS total,
                   SUM(CASE WHEN official_result IS NOT NULL THEN 1 ELSE 0 END) AS resolved,
                   SUM(CASE WHEN correct=1 THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN correct=0 THEN 1 ELSE 0 END) AS losses,
                   SUM(CASE WHEN learning_applied=1 THEN 1 ELSE 0 END) AS applied
                   FROM adaptive15_predictions"""
            ).fetchone()
            last_update = connection.execute(
                "SELECT * FROM adaptive15_updates ORDER BY id DESC LIMIT 1"
            ).fetchone()
            paused = self._meta(connection, "learning_paused", "0") == "1"
            pause_reason = self._meta(connection, "learning_pause_reason", "") or None
            last_training_probability = _normalize_probability(self._meta(connection, "last_training_probability", ""))
            last_learning_sample_weight = _num(self._meta(connection, "last_learning_sample_weight", ""))
        resolved = int(counts["resolved"] or 0)
        wins = int(counts["wins"] or 0)
        losses = int(counts["losses"] or 0)
        return {
            "version": VERSION,
            "available": True,
            "enabled": self.enabled,
            "path": str(self.path),
            "unique_predictions": int(counts["total"] or 0),
            "unique_resolved": resolved,
            "correct": wins,
            "wrong": losses,
            "accuracy": wins / resolved if resolved else None,
            "updates_applied": int(counts["applied"] or 0),
            "learning_paused": paused,
            "pause_reason": pause_reason,
            "weights": self.weights(),
            "base_weights": dict(_BASE_WEIGHTS),
            "per_result_weight_cap": self.per_result_cap,
            "total_weight_drift_cap": self.total_drift_cap,
            "minimum_learning_quality": self.min_learning_quality,
            "l2_pull_to_base": self.l2_pull,
            "last_training_probability": last_training_probability,
            "last_learning_sample_weight": last_learning_sample_weight,
            "pattern_minimum_resolved": self.pattern_minimum,
            "pattern_probability_cap": self.pattern_cap_probability,
            "last_update": dict(last_update) if last_update is not None else None,
            "automatic_threshold_changes": False,
            "ten_minute_learning": False,
        }


# ---------------------------------------------------------------------------
# Prediction and trade-quality separation
# ---------------------------------------------------------------------------

def _selected_quote(snapshot: Mapping[str, Any], side: str) -> dict[str, float | None]:
    if side == "YES":
        bid = _first_cents(snapshot, ("yes_bid", "yes_bid_cents", "best_yes_bid"))
        ask = _first_cents(snapshot, ("yes_ask", "yes_ask_cents", "best_yes_ask"))
    else:
        bid = _first_cents(snapshot, ("no_bid", "no_bid_cents", "best_no_bid"))
        ask = _first_cents(snapshot, ("no_ask", "no_ask_cents", "best_no_ask"))
        # Binary complement fallback is valid only when the opposite quote is known.
        if bid is None:
            yes_ask = _first_cents(snapshot, ("yes_ask", "yes_ask_cents", "best_yes_ask"))
            bid = None if yes_ask is None else 100.0 - yes_ask
        if ask is None:
            yes_bid = _first_cents(snapshot, ("yes_bid", "yes_bid_cents", "best_yes_bid"))
            ask = None if yes_bid is None else 100.0 - yes_bid
    spread = None if bid is None or ask is None else max(0.0, ask - bid)
    return {"bid_cents": bid, "ask_cents": ask, "spread_cents": spread}


def _estimated_costs(snapshot: Mapping[str, Any], quote: Mapping[str, Any]) -> dict[str, float]:
    ask = _num(quote.get("ask_cents")) or 50.0
    explicit_fee = _first_num(snapshot, ("estimated_fee_cents", "fee_cents", "fee"))
    fee = explicit_fee if explicit_fee is not None else max(0.10, 0.07 * ask * (100.0 - ask) / 100.0)
    slippage = _first_num(snapshot, ("estimated_slippage_cents", "slippage_cents", "slippage"))
    if slippage is None:
        spread = _num(quote.get("spread_cents"))
        slippage = max(0.10, (spread or 0.0) * 0.35)
    impact = _first_num(snapshot, ("estimated_market_impact_cents", "market_impact_cents"))
    if impact is None:
        impact = 0.10
    return {
        "fee_cents": max(0.0, fee),
        "slippage_cents": max(0.0, slippage),
        "market_impact_cents": max(0.0, impact),
        "total_cost_cents": max(0.0, fee) + max(0.0, slippage) + max(0.0, impact),
    }


def analyse_15m_snapshot(
    snapshot: Mapping[str, Any],
    now: float,
    learner: Adaptive15LearningStore | None = None,
    *,
    minimum_probability: float = 0.58,
    required_edge_cents: float = 4.0,
    minimum_quality: float = 0.45,
    maximum_spread_cents: float = 12.0,
    minimum_seconds_remaining: float = 20.0,
) -> dict[str, Any]:
    extracted = extract_features(snapshot, now)
    if not extracted["core_valid"]:
        return {
            "version": VERSION,
            "model_version": MODEL_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "read_only": True,
            "prediction_available": False,
            "prediction_side": None,
            "yes_probability": None,
            "no_probability": None,
            "selected_probability": None,
            "conservative_probability": None,
            "confidence_grade": "UNAVAILABLE",
            "data_quality": extracted["data_quality"],
            "evidence_coverage": extracted["evidence_coverage"],
            "features": extracted,
            "trade_decision": "AVOID_INVALID_DATA",
            "entry_allowed": False,
            "trade_reason": "missing_or_invalid_spot_threshold_or_time",
            "learning": {"pattern": {"active": False, "adjustment_probability": 0.0}},
        }

    weights = learner.weights() if learner is not None else dict(_BASE_WEIGHTS)
    score = float(weights.get("intercept", 0.0))
    contributions: dict[str, float] = {"intercept": score}
    effective_feature_values: dict[str, float] = {}
    feature_quality: dict[str, float] = {}
    for name in _FEATURE_QUALITY_WEIGHTS:
        raw_value = float(extracted["values"].get(name, 0.0))
        reliability = _clamp(float(extracted["details"].get(name, {}).get("quality", 0.0)), 0.0, 1.0)
        feature_quality[name] = reliability
        effective_value = raw_value * reliability if extracted["available"].get(name) else 0.0
        effective_feature_values[name] = effective_value
        if not extracted["available"].get(name):
            continue
        contribution = float(weights.get(name, _BASE_WEIGHTS[name])) * effective_value
        score += contribution
        contributions[name] = contribution
    quality = float(extracted["data_quality"])
    # Per-feature reliability already attenuates weak evidence. This smaller
    # global shrink handles missing breadth without double-penalizing it.
    score *= 0.80 + 0.20 * quality
    base_yes_probability = _clamp(_sigmoid(score), 0.05, 0.95)
    provisional_side = "YES" if base_yes_probability >= 0.5 else "NO"
    pattern = (
        learner.pattern_adjustment(effective_feature_values, extracted["available"], provisional_side)
        if learner is not None
        else {"active": False, "adjustment_probability": 0.0, "reason": "learner_unavailable"}
    )
    pattern_adjustment = float(pattern.get("adjustment_probability") or 0.0)
    # Pattern adjustment is side-aligned.  A positive value strengthens the
    # provisional side; a negative value weakens it and may flip only near 50%.
    yes_probability = base_yes_probability + (pattern_adjustment if provisional_side == "YES" else -pattern_adjustment)
    yes_probability = _clamp(yes_probability, 0.05, 0.95)
    side = "YES" if yes_probability >= 0.5 else "NO"
    selected_probability = yes_probability if side == "YES" else 1.0 - yes_probability
    no_probability = 1.0 - yes_probability

    learning_status = learner.status() if learner is not None else {"unique_resolved": 0}
    resolved_count = int(learning_status.get("unique_resolved") or 0)
    sample_uncertainty = min(0.07, 0.11 / math.sqrt(max(1.0, resolved_count + 1.0)))
    shrink = 0.58 + 0.42 * quality
    conservative_probability = 0.5 + (selected_probability - 0.5) * shrink - sample_uncertainty
    conservative_probability = _clamp(conservative_probability, 0.50, selected_probability)
    strength = abs(selected_probability - 0.5) * 2.0
    if strength >= 0.30 and quality >= 0.70:
        grade = "A"
    elif strength >= 0.20 and quality >= 0.55:
        grade = "B"
    elif strength >= 0.10:
        grade = "C"
    else:
        grade = "D"

    quote = _selected_quote(snapshot, side)
    costs = _estimated_costs(snapshot, quote)
    ask = _num(quote.get("ask_cents"))
    net_edge = None if ask is None else conservative_probability * 100.0 - ask - costs["total_cost_cents"]
    fair_max_entry = conservative_probability * 100.0 - costs["total_cost_cents"] - required_edge_cents
    seconds_remaining = _num(extracted["core"].get("seconds_remaining"))
    spread = _num(quote.get("spread_cents"))

    entry_allowed = False
    if seconds_remaining is not None and seconds_remaining < minimum_seconds_remaining:
        trade_decision = "WATCH_TIME"
        reason = "too_little_time_remaining_for_new_entry"
    elif ask is None:
        trade_decision = "PREDICTION_ONLY"
        reason = "direction_available_but_executable_ask_missing"
    elif spread is not None and spread > maximum_spread_cents:
        trade_decision = "WATCH_LIQUIDITY"
        reason = "spread_too_wide"
    elif quality < minimum_quality:
        trade_decision = "WATCH_DATA_QUALITY"
        reason = "optional_evidence_coverage_is_low"
    elif conservative_probability < minimum_probability:
        trade_decision = "WATCH_CONFIDENCE"
        reason = "conservative_probability_below_entry_threshold"
    elif net_edge is None or net_edge < required_edge_cents:
        trade_decision = "WATCH_PRICE"
        reason = "current_ask_does_not_preserve_required_edge"
    else:
        trade_decision = "ENTRY_RECOMMENDED"
        reason = "probability_quality_and_executable_edge_pass"
        entry_allowed = True

    ranking_score = strength * (0.55 + 0.45 * quality)
    if net_edge is not None:
        ranking_score += _clamp(net_edge / 25.0, -0.25, 0.35)

    return {
        "version": VERSION,
        "model_version": MODEL_VERSION,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "read_only": True,
        "prediction_available": True,
        "prediction_side": side,
        "yes_probability": yes_probability,
        "no_probability": no_probability,
        "base_yes_probability_before_pattern": base_yes_probability,
        "selected_probability": selected_probability,
        "conservative_probability": conservative_probability,
        "confidence_grade": grade,
        "directional_score": score,
        "data_quality": quality,
        "evidence_coverage": extracted["evidence_coverage"],
        "available_feature_count": sum(bool(value) for value in extracted["available"].values()),
        "total_feature_count": len(extracted["available"]),
        "feature_values": effective_feature_values,
        "raw_feature_values": extracted["values"],
        "feature_quality": feature_quality,
        "feature_available": extracted["available"],
        "feature_details": extracted["details"],
        "feature_contributions": contributions,
        "weights": weights,
        "core": extracted["core"],
        "candle_count": extracted["candle_count"],
        "trade_decision": trade_decision,
        "entry_allowed": entry_allowed,
        "trade_reason": reason,
        "quote": quote,
        "costs": costs,
        "net_edge_cents": net_edge,
        "required_edge_cents": required_edge_cents,
        "fair_max_entry_cents": _clamp(fair_max_entry, 1.0, 99.0),
        "minimum_probability": minimum_probability,
        "minimum_data_quality": minimum_quality,
        "maximum_spread_cents": maximum_spread_cents,
        "ranking_score": ranking_score,
        "learning": {
            "immediate_bounded_updates": True,
            "pattern": pattern,
            "resolved_count": resolved_count,
            "automatic_threshold_changes": False,
            "ten_minute_learning": False,
        },
        "prediction_is_separate_from_trade": True,
        "missing_optional_data_is_not_a_directional_veto": True,
    }


def apply_adaptive_15m_policy(snapshot: MutableMapping[str, Any], analysis: Mapping[str, Any]) -> MutableMapping[str, Any]:
    snapshot["q15_v9_4_adaptive15_version"] = VERSION
    snapshot["q15_v9_4_adaptive15"] = copy.deepcopy(dict(analysis))
    snapshot["q15_v9_4_adaptive15_prediction_available"] = bool(analysis.get("prediction_available"))
    snapshot["q15_v9_4_adaptive15_side"] = analysis.get("prediction_side")
    snapshot["q15_v9_4_adaptive15_probability"] = analysis.get("selected_probability")
    snapshot["q15_v9_4_adaptive15_yes_probability"] = analysis.get("yes_probability")
    snapshot["q15_v9_4_adaptive15_conservative_probability"] = analysis.get("conservative_probability")
    snapshot["q15_v9_4_adaptive15_confidence_grade"] = analysis.get("confidence_grade")
    snapshot["q15_v9_4_adaptive15_data_quality"] = analysis.get("data_quality")
    snapshot["q15_v9_4_adaptive15_trade_decision"] = analysis.get("trade_decision")
    snapshot["q15_v9_4_adaptive15_entry_allowed"] = bool(analysis.get("entry_allowed"))
    snapshot["q15_v9_4_adaptive15_read_only"] = True

    if analysis.get("prediction_available") and analysis.get("prediction_side") in {"YES", "NO"}:
        side = str(analysis["prediction_side"])
        for key in ("selected_side", "entry_side", "recommended_side", "q15_selected_side"):
            snapshot[key] = side
        snapshot["q15_final_result"] = side
        snapshot["prediction_side"] = side
        snapshot["prediction_probability"] = analysis.get("selected_probability")
    if analysis.get("entry_allowed"):
        snapshot["decision_state"] = "ENTRY RECOMMENDED"
        snapshot["entry_status"] = "ENTRY RECOMMENDED"
        snapshot["new_entry_allowed"] = True
        snapshot["entry_allowed"] = True
    elif analysis.get("prediction_available"):
        snapshot["decision_state"] = "WATCH"
        snapshot["entry_status"] = "WATCH"
        snapshot["new_entry_allowed"] = False
        snapshot["entry_allowed"] = False
    else:
        snapshot["decision_state"] = "NO TRADE"
        snapshot["entry_status"] = "NO TRADE"
        snapshot["new_entry_allowed"] = False
        snapshot["entry_allowed"] = False
    return snapshot


def rank_analyses(analyses: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for asset, analysis in analyses.items():
        rows.append(
            {
                "asset": asset,
                "side": analysis.get("prediction_side"),
                "probability": analysis.get("selected_probability"),
                "conservative_probability": analysis.get("conservative_probability"),
                "confidence_grade": analysis.get("confidence_grade"),
                "data_quality": analysis.get("data_quality"),
                "trade_decision": analysis.get("trade_decision"),
                "entry_allowed": analysis.get("entry_allowed"),
                "net_edge_cents": analysis.get("net_edge_cents"),
                "fair_max_entry_cents": analysis.get("fair_max_entry_cents"),
                "ranking_score": _num(analysis.get("ranking_score"), -999.0),
            }
        )
    rows.sort(
        key=lambda row: (
            bool(row.get("entry_allowed")),
            _num(row.get("ranking_score"), -999.0),
            _num(row.get("probability"), 0.0),
        ),
        reverse=True,
    )
    for index, row in enumerate(rows, 1):
        row["rank"] = index
    return rows


# ---------------------------------------------------------------------------
# 15M-only cycle detection and Telegram rewriting
# ---------------------------------------------------------------------------

def _checkpoint_text(snapshot: Mapping[str, Any]) -> str:
    pieces: list[str] = []
    for key in (
        "checkpoint", "checkpoint_name", "window", "phase", "signal_type",
        "q15_checkpoint", "decision_checkpoint", "report_type",
    ):
        value = snapshot.get(key)
        if value is not None:
            pieces.append(str(value))
    return " ".join(pieces).upper()


def _cycle_is_15m(
    parent_output: Mapping[str, Any], captured_messages: Sequence[tuple[tuple[Any, ...], Mapping[str, Any]]]
) -> bool:
    message_text = " ".join(str(args[0]) for args, _ in captured_messages if args).upper()
    if "10M" in message_text and "15M" not in message_text:
        return False
    if "15M" in message_text:
        return True
    found_10m = False
    for snapshot in parent_output.values():
        if not isinstance(snapshot, Mapping):
            continue
        text = _checkpoint_text(snapshot)
        if "10M" in text:
            found_10m = True
        if "15M" in text:
            return True
    if found_10m:
        return False
    # Last-resort time-based inference: an early checkpoint normally has more
    # than 10 minutes remaining.  Unknown is treated as non-15M to protect 10M.
    times = [
        _num(snapshot.get("seconds_remaining"))
        for snapshot in parent_output.values()
        if isinstance(snapshot, Mapping)
    ]
    valid = [value for value in times if value is not None]
    return bool(valid and max(valid) >= 660.0)


class _DeferredNotifier:
    def __init__(self, real: Any) -> None:
        self.real = real
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.real, "enabled", True))

    def send(self, *args: Any, **kwargs: Any) -> bool:
        self.calls.append((args, dict(kwargs)))
        return True

    def __getattr__(self, name: str) -> Any:
        return getattr(self.real, name)

    def flush(
        self,
        *,
        cycle_is_15m: bool,
        analyses: Mapping[str, Mapping[str, Any]],
        ranking: Sequence[Mapping[str, Any]],
        learning: Mapping[str, Any],
    ) -> tuple[int, int, int]:
        sender = getattr(self.real, "send", None)
        if not callable(sender):
            failures = len(self.calls)
            self.calls.clear()
            return 0, failures, 0
        sent = failed = suppressed = 0
        adaptive_used = False
        adaptive_message = _build_adaptive_message(analyses, ranking, learning) if cycle_is_15m else None
        for args, kwargs in self.calls:
            if not args:
                continue
            original = str(args[0] or "")
            is_15m_message = "15M" in original.upper() or (cycle_is_15m and "10M" not in original.upper())
            if cycle_is_15m and is_15m_message:
                if adaptive_used:
                    suppressed += 1
                    continue
                rewritten = (adaptive_message, *args[1:])
                adaptive_used = True
            else:
                rewritten = args
            try:
                result = sender(*rewritten, **kwargs)
                if result is False:
                    failed += 1
                else:
                    sent += 1
            except Exception:
                failed += 1
        self.calls.clear()
        return sent, failed, suppressed


def _fmt_probability(value: Any) -> str:
    probability = _normalize_probability(value)
    return "n/a" if probability is None else f"{probability * 100:.1f}%"


def _fmt_cents(value: Any) -> str:
    number = _num(value)
    return "n/a" if number is None else f"{number:.2f}¢"


def _build_adaptive_message(
    analyses: Mapping[str, Mapping[str, Any]],
    ranking: Sequence[Mapping[str, Any]],
    learning: Mapping[str, Any],
) -> str:
    available = [row for row in ranking if row.get("side") in {"YES", "NO"}]
    if not available:
        return (
            "🛑 15M ADAPTIVE CHECK — CORE DATA UNAVAILABLE\n"
            "No valid YES/NO estimate could be produced because spot price, contract threshold, "
            "or time remaining was missing or malformed.\n\n"
            "Read-only monitor — no orders are placed."
        )
    top = available[0]
    any_entry = any(bool(row.get("entry_allowed")) for row in available)
    headline = (
        f"✅ 15M ADAPTIVE CHECK — ENTRY RECOMMENDED: {top['asset']} {top['side']}"
        if any_entry
        else f"👀 15M ADAPTIVE CHECK — BEST PICK: {top['asset']} {top['side']}"
    )
    medals = ["🥇", "🥈", "🥉"]
    lines = [headline, ""]
    lines.append(
        "  ·  ".join(
            f"{medals[index]} {row['asset']} {row['side']} {_fmt_probability(row.get('probability'))}"
            for index, row in enumerate(available[:3])
        )
    )
    for index, row in enumerate(available[:3]):
        analysis = analyses.get(str(row["asset"])) or {}
        pattern = (analysis.get("learning") or {}).get("pattern") or {}
        lines.extend(
            [
                "",
                f"{medals[index]} <b>{row['asset']} {row['side']}</b>",
                f"YES {_fmt_probability(analysis.get('yes_probability'))} | NO {_fmt_probability(analysis.get('no_probability'))}",
                f"Selected {_fmt_probability(row.get('probability'))} | conservative {_fmt_probability(row.get('conservative_probability'))} | grade {row.get('confidence_grade') or 'n/a'}",
                f"Data mapped: {analysis.get('available_feature_count', 0)}/{analysis.get('total_feature_count', 0)} feature groups | quality {_fmt_probability(row.get('data_quality'))} | candles {analysis.get('candle_count', 0)}",
                f"Trade: <b>{row.get('trade_decision') or 'n/a'}</b> | net edge {_fmt_cents(row.get('net_edge_cents'))} | ideal max entry {_fmt_cents(row.get('fair_max_entry_cents'))}",
                f"Reason: {analysis.get('trade_reason') or 'n/a'}",
                (
                    f"Pattern learning: active | winner similarity {float(pattern.get('winner_similarity') or 0.0):+.2f} | loser similarity {float(pattern.get('loser_similarity') or 0.0):+.2f} | probability adjustment {float(pattern.get('adjustment_probability') or 0.0) * 100:+.1f} pts"
                    if pattern.get("active")
                    else f"Pattern learning: collecting data ({int(pattern.get('resolved') or learning.get('unique_resolved') or 0)}/{int(pattern.get('minimum') or learning.get('pattern_minimum_resolved') or 10)} resolved)"
                ),
            ]
        )
    lines.extend(
        [
            "",
            f"15M learning record: {int(learning.get('correct') or 0)} correct / {int(learning.get('wrong') or 0)} wrong from {int(learning.get('unique_resolved') or 0)} unique resolved contracts.",
            "Each settlement can make only a small bounded coefficient update. Entry thresholds do not change automatically, and the 10M model is untouched.",
            "Probabilistic paper estimate only; it can be wrong. Read-only monitor — no orders are placed.",
        ]
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Policy integration
# ---------------------------------------------------------------------------

class CheckpointPolicyV94Adaptive15(CheckpointPolicyV92):
    VERSION = VERSION

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.signal_store = getattr(self, "store", None) or (args[0] if args else None)
        self.adaptive_enabled = _env_bool("Q15_V94_ADAPTIVE_15M_ENABLED", True)
        self.minimum_probability = _env_float("Q15_V94_15M_MIN_PROBABILITY", 0.58, 0.51, 0.90)
        self.required_edge_cents = _env_float("Q15_V94_15M_REQUIRED_EDGE_CENTS", 4.0, 0.0, 25.0)
        self.minimum_quality = _env_float("Q15_V94_15M_MIN_DATA_QUALITY", 0.45, 0.10, 0.95)
        self.maximum_spread_cents = _env_float("Q15_V94_15M_MAX_SPREAD_CENTS", 12.0, 1.0, 40.0)
        self.minimum_seconds_remaining = _env_float("Q15_V94_15M_MIN_SECONDS_REMAINING", 20.0, 0.0, 180.0)
        self.learning = Adaptive15LearningStore()
        self._adaptive_lock = threading.RLock()
        self._latest: dict[str, dict[str, Any]] = {}
        self._ranking: list[dict[str, Any]] = []
        self._cycles = 0
        self._fifteen_minute_cycles = 0
        self._ten_minute_passthrough_cycles = 0
        self._errors = 0
        self._last_error: str | None = None
        self._last_cycle_at: float | None = None
        self._recorded_predictions = 0
        self._telegram_sent = 0
        self._telegram_failed = 0
        self._telegram_duplicates_suppressed = 0
        self._last_reconcile: dict[str, Any] = {}

    def run_cycle(
        self,
        snapshots: Dict[str, dict],
        now: float,
        ws_health: Mapping[str, Any] | None,
        focus_manager: Any,
        calibrated_edge: Any,
        notifier: Any,
    ) -> Dict[str, dict]:
        deferred = _DeferredNotifier(notifier)
        parent_output = super().run_cycle(
            snapshots, now, ws_health, focus_manager, calibrated_edge, deferred
        )
        self._cycles += 1
        self._last_cycle_at = now
        is_15m = _cycle_is_15m(parent_output, deferred.calls)
        if not self.adaptive_enabled or not is_15m:
            if not is_15m:
                self._ten_minute_passthrough_cycles += 1
            sent, failed, suppressed = deferred.flush(
                cycle_is_15m=False, analyses={}, ranking=[], learning=self.learning.status()
            )
            self._telegram_sent += sent
            self._telegram_failed += failed
            self._telegram_duplicates_suppressed += suppressed
            return parent_output

        self._fifteen_minute_cycles += 1
        output: Dict[str, dict] = {}
        analyses: dict[str, dict[str, Any]] = {}
        try:
            for asset_key, raw_snapshot in parent_output.items():
                if not isinstance(raw_snapshot, Mapping):
                    output[asset_key] = raw_snapshot
                    continue
                snapshot = copy.deepcopy(dict(raw_snapshot))
                asset = _asset_name(asset_key, snapshot)
                analysis = analyse_15m_snapshot(
                    snapshot,
                    now,
                    self.learning,
                    minimum_probability=self.minimum_probability,
                    required_edge_cents=self.required_edge_cents,
                    minimum_quality=self.minimum_quality,
                    maximum_spread_cents=self.maximum_spread_cents,
                    minimum_seconds_remaining=self.minimum_seconds_remaining,
                )
                apply_adaptive_15m_policy(snapshot, analysis)
                ticker = _ticker(snapshot)
                if analysis.get("prediction_available") and ticker:
                    prediction_id, inserted = self.learning.record_prediction(
                        ticker=ticker,
                        asset=asset,
                        side=str(analysis["prediction_side"]),
                        yes_probability=float(analysis["yes_probability"]),
                        selected_probability=float(analysis["selected_probability"]),
                        data_quality=float(analysis["data_quality"]),
                        features=analysis["feature_values"],
                        available=analysis["feature_available"],
                        weights=analysis["weights"],
                        created_at=now,
                        close_time=_close_time(snapshot, now),
                        training_yes_probability=float(analysis["base_yes_probability_before_pattern"]),
                    )
                    analysis["prediction_id"] = prediction_id
                    analysis["new_unique_prediction_recorded"] = inserted
                    if inserted:
                        self._recorded_predictions += 1
                output[asset_key] = snapshot
                analyses[asset] = copy.deepcopy(analysis)

            ranking = rank_analyses(analyses)
            ranks = {str(row["asset"]): int(row["rank"]) for row in ranking}
            for asset_key, snapshot in output.items():
                if not isinstance(snapshot, MutableMapping):
                    continue
                asset = _asset_name(asset_key, snapshot)
                snapshot["q15_v9_4_adaptive15_rank"] = ranks.get(asset)
                snapshot["q15_v9_4_adaptive15_top_pick"] = ranks.get(asset) == 1
                if asset in analyses:
                    analyses[asset]["rank"] = ranks.get(asset)
                    analyses[asset]["top_pick"] = ranks.get(asset) == 1

            if self.signal_store is not None:
                self._last_reconcile = self.learning.reconcile_from_signal_store(self.signal_store)
            learning_status = self.learning.status()
            with self._adaptive_lock:
                self._latest = copy.deepcopy(analyses)
                self._ranking = copy.deepcopy(ranking)
            with _LATEST_LOCK:
                _LATEST_ANALYSES.clear()
                _LATEST_ANALYSES.update(copy.deepcopy(analyses))
                _LATEST_RANKING.clear()
                _LATEST_RANKING.extend(copy.deepcopy(ranking))
                _LATEST_LEARNING.clear()
                _LATEST_LEARNING.update(copy.deepcopy(learning_status))
            sent, failed, suppressed = deferred.flush(
                cycle_is_15m=True,
                analyses=analyses,
                ranking=ranking,
                learning=learning_status,
            )
            self._telegram_sent += sent
            self._telegram_failed += failed
            self._telegram_duplicates_suppressed += suppressed
            return output
        except Exception as exc:
            self._errors += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            # Fail closed for trade entry, while preserving the parent output.
            for snapshot in parent_output.values():
                if isinstance(snapshot, MutableMapping):
                    snapshot["q15_v9_4_adaptive15_error"] = self._last_error
                    snapshot["q15_v9_4_adaptive15_entry_allowed"] = False
                    snapshot["entry_allowed"] = False
                    snapshot["new_entry_allowed"] = False
            sent, failed, suppressed = deferred.flush(
                cycle_is_15m=False, analyses={}, ranking=[], learning=self.learning.status()
            )
            self._telegram_sent += sent
            self._telegram_failed += failed
            self._telegram_duplicates_suppressed += suppressed
            return parent_output

    def adaptive_predictions(self) -> dict[str, Any]:
        with self._adaptive_lock:
            analyses = copy.deepcopy(self._latest)
            ranking = copy.deepcopy(self._ranking)
        return {
            "version": VERSION,
            "model_version": MODEL_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "read_only": True,
            "checkpoint_scope": "15M_ONLY",
            "top_pick": ranking[0] if ranking else None,
            "ranking": ranking,
            "assets": [{"asset": asset, **analysis} for asset, analysis in sorted(analyses.items())],
        }

    def adaptive_learning_status(self) -> dict[str, Any]:
        return {
            **self.learning.status(),
            "last_reconcile": copy.deepcopy(self._last_reconcile),
            "scope": "15M_ONLY",
            "ten_minute_path_modified": False,
        }

    def health(self) -> dict[str, Any]:
        try:
            parent = super().health()
        except Exception as exc:
            parent = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
        return {
            "version": VERSION,
            "enabled": self.adaptive_enabled,
            "read_only": True,
            "checkpoint_scope": "15M_ONLY",
            "cycles": self._cycles,
            "fifteen_minute_cycles": self._fifteen_minute_cycles,
            "ten_minute_passthrough_cycles": self._ten_minute_passthrough_cycles,
            "errors": self._errors,
            "last_error": self._last_error,
            "last_cycle_at": _iso(self._last_cycle_at),
            "unique_15m_predictions_recorded": self._recorded_predictions,
            "telegram_sent": self._telegram_sent,
            "telegram_failed": self._telegram_failed,
            "telegram_duplicate_15m_messages_suppressed": self._telegram_duplicates_suppressed,
            "prediction_is_separate_from_trade": True,
            "missing_optional_evidence_is_hard_veto": False,
            "bounded_online_learning": self.learning.enabled,
            "automatic_threshold_changes": False,
            "ten_minute_path_modified": False,
            "learning": self.learning.status(),
            "base_v92": parent,
        }

    def diagnostics(self) -> dict[str, Any]:
        try:
            parent = super().diagnostics()
        except Exception as exc:
            parent = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
        return {
            **self.health(),
            "settings": {
                "minimum_probability": self.minimum_probability,
                "required_edge_cents": self.required_edge_cents,
                "minimum_data_quality": self.minimum_quality,
                "maximum_spread_cents": self.maximum_spread_cents,
                "minimum_seconds_remaining": self.minimum_seconds_remaining,
                "maximum_core_age_seconds": _env_float("Q15_V94_15M_MAX_CORE_AGE_SECONDS", 30.0, 2.0, 300.0),
                "base_weights": dict(_BASE_WEIGHTS),
            },
            "hard_failures": [
                "missing or malformed spot price",
                "missing or malformed contract threshold",
                "missing or malformed time remaining",
                "stale timestamped core snapshot",
            ],
            "continuous_optional_evidence": [
                "parent probability",
                "short and medium momentum",
                "executed trade flow",
                "order-book pressure",
                "wick and candle structure",
                "cross-asset context",
                "Kalshi market prior used once",
            ],
            "latest_predictions": self.adaptive_predictions(),
            "base_v92_diagnostics": parent,
        }

    def decision_stats(self) -> dict[str, Any]:
        try:
            parent = super().decision_stats()
        except Exception as exc:
            parent = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
        with self._adaptive_lock:
            counts: dict[str, int] = {}
            for analysis in self._latest.values():
                state = str(analysis.get("trade_decision") or "UNKNOWN")
                counts[state] = counts.get(state, 0) + 1
        return {
            "version": VERSION,
            "read_only": True,
            "checkpoint_scope": "15M_ONLY",
            "current_15m_trade_decisions": counts,
            "learning": self.learning.status(),
            "base_v92_decision_stats": parent,
        }

    def consensus_status(self) -> dict[str, Any]:
        try:
            parent = super().consensus_status()
        except Exception as exc:
            parent = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
        return {
            "version": VERSION,
            "read_only": True,
            "fifteen_minute_direction_is_independent": True,
            "optional_evidence_is_continuous_not_binary": True,
            "ten_minute_parent_consensus_unchanged": True,
            "adaptive_15m_predictions": self.adaptive_predictions(),
            "base_v92_consensus": parent,
        }


def format_telegram_message(text: Any) -> str:
    message = str(text or "")
    if "15M ADAPTIVE CHECK" in message:
        return message
    upper = message.upper()
    if "15M" not in upper:
        return _format_v92_message(message)
    with _LATEST_LOCK:
        analyses = copy.deepcopy(_LATEST_ANALYSES)
        ranking = copy.deepcopy(_LATEST_RANKING)
        learning = copy.deepcopy(_LATEST_LEARNING)
    if analyses and ranking:
        return _build_adaptive_message(analyses, ranking, learning)
    return _format_v92_message(message)


__all__ = [
    "Adaptive15LearningStore",
    "CheckpointPolicyV94Adaptive15",
    "FEATURE_SCHEMA_VERSION",
    "MODEL_VERSION",
    "READ_ONLY",
    "VERSION",
    "analyse_15m_snapshot",
    "apply_adaptive_15m_policy",
    "extract_features",
    "format_telegram_message",
    "rank_analyses",
]
