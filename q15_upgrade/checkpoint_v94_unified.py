"""Q15 V9.4 unified probability, candle, ranking, and Telegram repair.

This module sits on top of the existing V9.4 dual-window context policy.  It
fixes the report contradictions caused by separate candle consumers and an old
binary wick/flow gate.

Key properties
--------------
* One canonical candle cache feeds volatility, momentum, wick analysis, and the
  existing prior/current 15-minute context.
* A directional probability is produced whenever spot, threshold, time, and
  freshness are valid. Optional evidence changes confidence; it is not a hard
  veto.
* Probability is anchored to threshold distance divided by robust expected move
  and then adjusted conservatively by independent evidence.
* Kalshi price is used only for executable value, not as a second copy of the
  probability model.
* Ranking is calculated after economics and always sorts the best executable
  opportunity first.
* Online learning remains 15M-only, bounded, version-scoped, and reconciled only
  from officially settled signal rows. Ten-minute predictions never update the
  learner.
* Read-only. No order submission, modification, or cancellation surface exists.
"""
from __future__ import annotations

from contextlib import closing

import copy
import hashlib
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
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

from .checkpoint_v94 import (
    CheckpointPolicyV94,
    format_telegram_message as _format_v94_message,
)
from .checkpoint_v94_adaptive15 import Adaptive15LearningStore

VERSION = "q15-v9.4-unified-probability-report-v4-telegram-dedupe"
MODEL_VERSION = "q15-v9.4-unified-probability-model-v3"
FEATURE_SCHEMA_VERSION = "q15-v9.4-unified-canonical-candles-v3"
READ_ONLY = True

_BASE_WEIGHTS: dict[str, float] = {
    "intercept": 0.00,
    "threshold_distance": 0.35,
    "parent_probability": 0.55,
    "momentum_short": 0.32,
    "momentum_medium": 0.24,
    "executed_flow": 0.24,
    "book_pressure": 0.18,
    "wick_structure": 0.14,
    "cross_asset": 0.22,
    "market_prior": 0.00,  # deliberately excluded from probability to avoid circular value
}

_LATEST_LOCK = threading.RLock()
_LATEST_ANALYSES: dict[str, dict[str, Any]] = {}
_LATEST_RANKING: list[dict[str, Any]] = []
_LATEST_LEARNING: dict[str, Any] = {}
_LATEST_CHECKPOINT = "UNKNOWN"


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


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


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


def _normalize_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")


def _walk(node: Any, depth: int = 0) -> Iterable[tuple[str, Any]]:
    if depth > 6:
        return
    if isinstance(node, Mapping):
        for key, value in node.items():
            normalized = _normalize_key(key)
            yield normalized, value
            if isinstance(value, (Mapping, list, tuple)):
                yield from _walk(value, depth + 1)
    elif isinstance(node, (list, tuple)):
        for value in node[:500]:
            if isinstance(value, (Mapping, list, tuple)):
                yield from _walk(value, depth + 1)


def _first_value(snapshot: Mapping[str, Any], aliases: Sequence[str]) -> Any:
    aliases_normalized = tuple(_normalize_key(alias) for alias in aliases)
    for alias in aliases:
        if alias in snapshot and snapshot.get(alias) is not None:
            return snapshot.get(alias)
    exact: list[Any] = []
    partial: list[Any] = []
    for key, value in _walk(snapshot):
        if value is None:
            continue
        if key in aliases_normalized:
            exact.append(value)
        elif any(alias in key for alias in aliases_normalized):
            partial.append(value)
    return exact[0] if exact else partial[0] if partial else None


def _first_num(snapshot: Mapping[str, Any], aliases: Sequence[str]) -> float | None:
    return _num(_first_value(snapshot, aliases))


def _probability(value: Any) -> float | None:
    parsed = _num(value)
    if parsed is None:
        return None
    if 1.0 < parsed <= 100.0:
        parsed /= 100.0
    return parsed if 0.0 <= parsed <= 1.0 else None


def _first_probability(snapshot: Mapping[str, Any], aliases: Sequence[str]) -> float | None:
    return _probability(_first_value(snapshot, aliases))


def _cents(value: Any) -> float | None:
    parsed = _num(value)
    if parsed is None:
        return None
    if 0.0 <= parsed <= 1.000001:
        parsed *= 100.0
    return parsed if 0.0 <= parsed <= 100.0 else None


def _first_cents(snapshot: Mapping[str, Any], aliases: Sequence[str]) -> float | None:
    return _cents(_first_value(snapshot, aliases))


def _asset_name(asset: Any, snapshot: Mapping[str, Any]) -> str:
    value = str(snapshot.get("asset") or asset or "UNKNOWN").strip().upper()
    return {"XBT": "BTC", "XDG": "DOGE"}.get(value, value or "UNKNOWN")


def _ticker(snapshot: Mapping[str, Any]) -> str | None:
    value = _first_value(snapshot, ("ticker", "market_ticker", "contract_ticker", "symbol"))
    text = str(value).strip() if value is not None else ""
    return text or None


def _yes_is_higher(snapshot: Mapping[str, Any]) -> bool:
    text = str(
        _first_value(snapshot, ("target_type", "strike_type", "comparison", "market_direction"))
        or "greater_or_equal"
    ).lower()
    return not any(token in text for token in ("less", "below", "under", "lower", "at_most"))


def _seconds_remaining(snapshot: Mapping[str, Any], now: float) -> float | None:
    seconds = _first_num(
        snapshot,
        ("seconds_remaining", "remaining_seconds", "time_remaining_seconds", "secs_remaining"),
    )
    if seconds is not None:
        return max(0.0, seconds)
    close_ts = _parse_ts(_first_value(
        snapshot,
        ("close_time", "market_close", "contract_close", "expiration_time", "expires_at"),
    ))
    return None if close_ts is None else max(0.0, close_ts - now)


def _spot(snapshot: Mapping[str, Any], candles: Sequence[Mapping[str, float]]) -> float | None:
    value = _first_num(
        snapshot,
        (
            "underlying_current", "current_underlying", "spot_price", "index_price",
            "underlying_price", "current_price", "last_price", "benchmark_price",
        ),
    )
    if value is None and candles:
        value = _num(candles[-1].get("close"))
    return value


def _target(snapshot: Mapping[str, Any]) -> float | None:
    return _first_num(snapshot, ("target_value", "floor_strike", "threshold", "strike", "target"))


def _normalize_candle(row: Mapping[str, Any]) -> dict[str, float] | None:
    open_value = _first_num(row, ("open", "o"))
    high = _first_num(row, ("high", "h"))
    low = _first_num(row, ("low", "l"))
    close = _first_num(row, ("close", "c", "last"))
    start = _parse_ts(_first_value(row, ("start_time", "start", "timestamp", "time", "ts", "bucket")))
    end = _parse_ts(_first_value(row, ("end_time", "end", "close_time")))
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
        "volume": float(_first_num(row, ("volume", "v")) or 0.0),
        "trade_count": float(_first_num(row, ("trade_count", "trades", "count")) or 0.0),
    }


def _canonical_candles(
    snapshot: Mapping[str, Any],
    cached: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, float]]:
    candidates: list[Sequence[Any]] = []
    if cached:
        candidates.append(cached)
    for key in (
        "underlying_candles_5s", "underlying_candles", "spot_candles_5s",
        "spot_candles", "candles_5s", "candles", "ohlc", "ohlcv", "bars",
    ):
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
            candle = _normalize_candle(raw)
            if candle is None:
                continue
            key = round(candle["start_time"] or candle["timestamp"], 6)
            merged[key] = candle
    rows = sorted(merged.values(), key=lambda row: (row["timestamp"], row["start_time"]))
    return rows[-600:]


def _cadence(candles: Sequence[Mapping[str, float]]) -> float:
    times = [float(row.get("timestamp") or 0.0) for row in candles if row.get("timestamp")]
    gaps = [b - a for a, b in zip(times, times[1:]) if 0.25 <= b - a <= 300.0]
    return _clamp(statistics.median(gaps) if gaps else 5.0, 0.25, 300.0)


def _log_returns(candles: Sequence[Mapping[str, float]]) -> list[float]:
    values: list[float] = []
    for previous, current in zip(candles, candles[1:]):
        a = _num(previous.get("close"))
        b = _num(current.get("close"))
        if a is not None and b is not None and a > 0 and b > 0:
            values.append(math.log(b / a))
    return values


def _winsorize(values: Sequence[float], limit: float = 8.0) -> list[float]:
    """Winsorize returns with a robust scale that survives repeated tick values.

    Five-second crypto feeds often contain many identical returns, making MAD
    exactly zero. Returning the raw series in that case lets one bad print
    explode the volatility estimate. We therefore fall back to the 90th
    percentile absolute deviation, which remains resistant to one or two flash
    spikes while preserving genuine regime changes that persist across many
    observations.
    """
    if len(values) < 5:
        return list(values)
    median = statistics.median(values)
    deviations = sorted(abs(value - median) for value in values)
    mad = statistics.median(deviations)
    robust_sigma = 1.4826 * mad
    if robust_sigma <= 1e-15:
        index = min(len(deviations) - 1, max(0, int(math.floor(0.90 * (len(deviations) - 1)))))
        p90 = deviations[index]
        robust_sigma = p90 / 1.2815515655446004 if p90 > 1e-15 else 0.0
    if robust_sigma <= 1e-15:
        return list(values)
    low = median - limit * robust_sigma
    high = median + limit * robust_sigma
    return [_clamp(value, low, high) for value in values]


def _ewma_variance(values: Sequence[float], cadence: float, half_life_seconds: float = 90.0) -> float | None:
    if len(values) < 3:
        return None
    decay = math.exp(math.log(0.5) * cadence / max(half_life_seconds, cadence))
    mean = values[0]
    variance = 0.0
    weight = 0.0
    for value in values[1:]:
        delta = value - mean
        mean = decay * mean + (1.0 - decay) * value
        variance = decay * variance + (1.0 - decay) * delta * delta
        weight = decay * weight + (1.0 - decay)
    if weight <= 0:
        return None
    return max(variance / weight, 0.0)


def _window_return(candles: Sequence[Mapping[str, float]], seconds: float) -> float | None:
    if len(candles) < 2:
        return None
    cadence = _cadence(candles)
    count = max(2, int(round(seconds / cadence)) + 1)
    subset = candles[-min(len(candles), count):]
    start = _num(subset[0].get("close"))
    end = _num(subset[-1].get("close"))
    if start is None or end is None or start <= 0 or end <= 0:
        return None
    return math.log(end / start)


def _wick_score(candles: Sequence[Mapping[str, float]], yes_is_higher: bool) -> tuple[float | None, dict[str, Any]]:
    if len(candles) < 3:
        return None, {"available": False, "reason": "need_three_completed_candles", "candle_count": len(candles)}
    rows = list(candles[-3:])
    weighted = 0.0
    total_weight = 0.0
    rejection_ratios: list[float] = []
    opposite_ratios: list[float] = []
    for index, row in enumerate(rows, 1):
        open_value = float(row["open"])
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        span = max(high - low, 1e-12)
        upper = max(0.0, high - max(open_value, close)) / span
        lower = max(0.0, min(open_value, close) - low) / span
        body = _clamp((close - open_value) / span, -1.0, 1.0)
        raw = (lower - upper) * 0.65 + body * 0.35
        rejection = lower if yes_is_higher else upper
        opposite = upper if yes_is_higher else lower
        if not yes_is_higher:
            raw *= -1.0
        weight = float(index)
        weighted += _clamp(raw, -1.0, 1.0) * weight
        total_weight += weight
        rejection_ratios.append(rejection)
        opposite_ratios.append(opposite)
    score = _safe_div(weighted, total_weight)
    return score, {
        "available": True,
        "candle_count": len(candles),
        "sequence_candles": 3,
        "score": score,
        "mean_rejection_wick": statistics.mean(rejection_ratios),
        "mean_opposite_wick": statistics.mean(opposite_ratios),
        "opposite_wick_veto": max(opposite_ratios) >= 0.70 and score < -0.35,
    }


def _flow_score(snapshot: Mapping[str, Any]) -> tuple[float | None, float, dict[str, Any]]:
    buy = _first_num(snapshot, ("taker_yes_volume_15s", "taker_buy_volume", "aggressive_buy_volume", "buy_volume"))
    sell = _first_num(snapshot, ("taker_no_volume_15s", "taker_sell_volume", "aggressive_sell_volume", "sell_volume"))
    if buy is not None and sell is not None and buy + sell > 0:
        score = (buy - sell) / (buy + sell)
        return _clamp(score, -1.0, 1.0), 0.90, {"source": "explicit_taker_volume", "buy": buy, "sell": sell}
    trades = _first_value(snapshot, ("recent_trades", "trades", "executions"))
    if isinstance(trades, (list, tuple)):
        buy_total = sell_total = 0.0
        observed = 0
        for trade in trades[-250:]:
            if not isinstance(trade, Mapping):
                continue
            size = _first_num(trade, ("size", "quantity", "count", "volume")) or 1.0
            side = str(_first_value(trade, ("taker_outcome_side", "side", "direction")) or "").upper()
            if side in {"YES", "BUY", "BID", "BULLISH"}:
                buy_total += size
                observed += 1
            elif side in {"NO", "SELL", "ASK", "BEARISH"}:
                sell_total += size
                observed += 1
        total = buy_total + sell_total
        if total > 0:
            score = (buy_total - sell_total) / total
            quality = _clamp(observed / 30.0, 0.30, 0.80)
            return _clamp(score, -1.0, 1.0), quality, {"source": "recent_trades", "trade_count": observed}
    return None, 0.0, {"source": "unavailable"}


def _book_score(snapshot: Mapping[str, Any]) -> tuple[float | None, float, dict[str, Any]]:
    imbalance = _first_num(snapshot, ("orderbook_imbalance", "book_imbalance", "depth_imbalance", "persistent_book_pressure"))
    if imbalance is not None:
        if 1.0 < abs(imbalance) <= 100.0:
            imbalance /= 100.0
        return _clamp(imbalance, -1.0, 1.0), 0.80, {"source": "explicit_imbalance"}
    bid_depth = _first_num(snapshot, ("bid_depth", "yes_bid_depth", "bid_liquidity", "buy_depth"))
    ask_depth = _first_num(snapshot, ("ask_depth", "yes_ask_depth", "ask_liquidity", "sell_depth"))
    if bid_depth is not None and ask_depth is not None and bid_depth + ask_depth > 0:
        score = (bid_depth - ask_depth) / (bid_depth + ask_depth)
        return _clamp(score, -1.0, 1.0), 0.65, {"source": "top_book_depth", "bid_depth": bid_depth, "ask_depth": ask_depth}
    return None, 0.0, {"source": "unavailable"}


def _selected_quote(snapshot: Mapping[str, Any], side: str) -> dict[str, float | None]:
    if side == "YES":
        bid = _first_cents(snapshot, ("yes_bid", "yes_bid_cents", "best_yes_bid"))
        ask = _first_cents(snapshot, ("yes_ask", "yes_ask_cents", "best_yes_ask"))
    else:
        bid = _first_cents(snapshot, ("no_bid", "no_bid_cents", "best_no_bid"))
        ask = _first_cents(snapshot, ("no_ask", "no_ask_cents", "best_no_ask"))
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
        slippage = 0.50 * spread if spread is not None else 0.75
    impact = _first_num(snapshot, ("estimated_market_impact_cents", "market_impact_cents", "market_impact"))
    if impact is None:
        impact = 0.25
    total = max(0.0, fee) + max(0.0, slippage) + max(0.0, impact)
    return {"fee_cents": max(0.0, fee), "slippage_cents": max(0.0, slippage), "market_impact_cents": max(0.0, impact), "total_cents": total}


def _detect_checkpoint(
    snapshots: Mapping[str, Mapping[str, Any]],
    messages: Sequence[str],
) -> str:
    for snapshot in snapshots.values():
        explicit = str(_first_value(snapshot, ("checkpoint", "checkpoint_name", "horizon", "stage")) or "").upper()
        if "10M" in explicit or explicit in {"10", "TEN_MINUTE"}:
            return "10M"
        if "15M" in explicit or explicit in {"15", "FIFTEEN_MINUTE"}:
            return "15M"
    combined = "\n".join(messages).upper()
    if "10M" in combined:
        return "10M"
    if "15M" in combined:
        return "15M"
    times = []
    now = time.time()
    for snapshot in snapshots.values():
        seconds = _seconds_remaining(snapshot, now)
        if seconds is not None:
            times.append(seconds)
    return "15M" if times and max(times) >= 660.0 else "10M"


@dataclass(frozen=True)
class ProbabilityResult:
    yes_probability: float
    structural_yes_probability: float
    raw_yes_probability: float
    quality: float
    feature_values: Mapping[str, float]
    feature_available: Mapping[str, bool]
    feature_quality: Mapping[str, float]
    contributions: Mapping[str, float]
    details: Mapping[str, Any]


def _probability_model(
    snapshot: Mapping[str, Any],
    candles: Sequence[Mapping[str, float]],
    context: Mapping[str, Any] | None,
    now: float,
    checkpoint: str,
    weights: Mapping[str, float],
) -> ProbabilityResult | None:
    spot = _spot(snapshot, candles)
    target = _target(snapshot)
    seconds = _seconds_remaining(snapshot, now)
    snapshot_ts = _parse_ts(_first_value(snapshot, ("snapshot_time", "snapshot_timestamp", "observed_at", "as_of", "updated_at")))
    max_age = _env_float("Q15_V94_UNIFIED_MAX_CORE_AGE_SECONDS", 30.0, 2.0, 300.0)
    age = None if snapshot_ts is None else max(0.0, now - snapshot_ts)
    if spot is None or target is None or seconds is None or spot <= 0 or target <= 0:
        return None
    if age is not None and age > max_age:
        return None

    yes_higher = _yes_is_higher(snapshot)
    direction = 1.0 if yes_higher else -1.0
    cadence = _cadence(candles)
    returns = _winsorize(_log_returns(candles[-240:]))
    variance = _ewma_variance(returns, cadence)
    explicit_vol = _first_num(snapshot, ("realized_volatility", "volatility", "ewma_volatility", "short_volatility"))

    vol_source = "candles"
    if variance is not None and variance > 1e-16:
        sigma_per_sqrt_second = math.sqrt(variance / cadence)
        vol_quality = _clamp(len(returns) / 60.0, 0.45, 1.0)
    elif explicit_vol is not None and explicit_vol > 0:
        # Small explicit values are interpreted as horizon fractional volatility.
        horizon_sigma = explicit_vol if explicit_vol < 1.0 else explicit_vol / spot
        sigma_per_sqrt_second = horizon_sigma / math.sqrt(max(seconds, 1.0))
        vol_source = "snapshot"
        vol_quality = 0.65
    else:
        # Conservative fallback avoids fake 95% estimates from a missing volatility feed.
        fallback_horizon_sigma = _env_float("Q15_V94_UNIFIED_FALLBACK_HORIZON_VOL", 0.0035, 0.0005, 0.03)
        sigma_per_sqrt_second = fallback_horizon_sigma / math.sqrt(max(seconds, 1.0))
        vol_source = "fallback"
        vol_quality = 0.30

    minimum_sigma = _env_float("Q15_V94_UNIFIED_MIN_SIGMA_PER_SQRT_SECOND", 0.000015, 0.000001, 0.001)
    sigma_per_sqrt_second = max(sigma_per_sqrt_second, minimum_sigma)
    horizon_sigma = sigma_per_sqrt_second * math.sqrt(max(seconds, 1.0))

    log_distance = direction * math.log(spot / target)
    short_return = _window_return(candles, 30.0)
    medium_return = _window_return(candles, 180.0)
    recent = returns[-min(len(returns), max(3, int(round(120.0 / cadence)))):]
    drift_per_second = statistics.mean(recent) / cadence if recent else 0.0
    consistency = 0.0
    if recent:
        supportive = sum(1 for value in recent if direction * value > 0)
        consistency = abs(supportive / len(recent) - 0.5) * 2.0
    drift_horizon = direction * drift_per_second * seconds * _clamp(len(recent) / 30.0, 0.0, 1.0) * consistency
    drift_cap = 0.45 * horizon_sigma
    drift_horizon = _clamp(drift_horizon, -drift_cap, drift_cap)
    structural_z = _clamp((log_distance + drift_horizon) / max(horizon_sigma, 1e-12), -4.0, 4.0)
    structural_selected = _clamp(_normal_cdf(structural_z), 0.01, 0.99)
    structural_yes = structural_selected if yes_higher else 1.0 - structural_selected

    parent_probability = _first_probability(
        snapshot,
        (
            "fair_yes_probability", "model_yes_probability", "yes_probability",
            "chart_yes_probability", "blended_yes_probability", "q15_probability_yes",
        ),
    )
    flow, flow_quality, flow_details = _flow_score(snapshot)
    book, book_quality, book_details = _book_score(snapshot)
    wick, wick_details = _wick_score(candles, yes_higher)
    context_score_raw = _num((context or {}).get("combined_side_support"))
    if context_score_raw is None:
        context_score_raw = _first_num(snapshot, ("q15_v9_4_context_score", "context_score", "dual_window_score"))
    context_selected_side = str(
        (context or {}).get("selected_side")
        or snapshot.get("selected_side")
        or snapshot.get("entry_side")
        or "YES"
    ).upper()
    # V9.4 stores combined_side_support relative to the side selected by its
    # parent model. Convert it back to a YES-oriented feature before blending;
    # otherwise a supportive NO context would incorrectly raise YES odds.
    context_score = None
    if context_score_raw is not None:
        context_score = -context_score_raw if context_selected_side == "NO" else context_score_raw
    context_quality = 0.0
    if context_score is not None:
        previous = (context or {}).get("previous_15m") if isinstance(context, Mapping) else None
        current = (context or {}).get("current_15m") if isinstance(context, Mapping) else None
        previous_coverage = _num(previous.get("coverage")) if isinstance(previous, Mapping) else None
        current_coverage = _num(current.get("coverage")) if isinstance(current, Mapping) else None
        context_quality = _clamp(((previous_coverage or 0.5) + (current_coverage or 0.5)) / 2.0, 0.35, 1.0)

    short_z = 0.0 if short_return is None else _clamp(direction * short_return / max(sigma_per_sqrt_second * math.sqrt(30.0), 1e-12) / 2.0, -1.0, 1.0)
    medium_z = 0.0 if medium_return is None else _clamp(direction * medium_return / max(sigma_per_sqrt_second * math.sqrt(180.0), 1e-12) / 2.5, -1.0, 1.0)
    parent_feature = 0.0
    if parent_probability is not None:
        structural_logit = _logit(structural_yes)
        parent_feature = _clamp((_logit(parent_probability) - structural_logit) / 3.0, -1.0, 1.0)

    values: dict[str, float] = {
        "threshold_distance": _clamp(structural_z / 3.0, -1.0, 1.0),
        "parent_probability": parent_feature,
        "momentum_short": short_z,
        "momentum_medium": medium_z,
        "executed_flow": float(flow or 0.0),
        "book_pressure": float(book or 0.0),
        "wick_structure": float(wick or 0.0),
        "cross_asset": _clamp(float(context_score or 0.0), -1.0, 1.0),
        "market_prior": 0.0,
    }
    available: dict[str, bool] = {
        "threshold_distance": True,
        "parent_probability": parent_probability is not None,
        "momentum_short": short_return is not None,
        "momentum_medium": medium_return is not None,
        "executed_flow": flow is not None,
        "book_pressure": book is not None,
        "wick_structure": wick is not None,
        "cross_asset": context_score is not None,
        "market_prior": False,
    }
    qualities: dict[str, float] = {
        "threshold_distance": vol_quality,
        "parent_probability": 0.60 if parent_probability is not None else 0.0,
        "momentum_short": _clamp(len(candles) / 12.0, 0.0, 0.90) if short_return is not None else 0.0,
        "momentum_medium": _clamp(len(candles) / 40.0, 0.0, 0.90) if medium_return is not None else 0.0,
        "executed_flow": flow_quality,
        "book_pressure": book_quality,
        "wick_structure": _clamp(len(candles) / 12.0, 0.35, 0.90) if wick is not None else 0.0,
        "cross_asset": context_quality,
        "market_prior": 0.0,
    }

    # Structural threshold probability is the anchor. Independent evidence is a
    # deliberately capped log-odds adjustment, not another mandatory gate.
    contributions: dict[str, float] = {}
    evidence_logit = 0.0
    for name, value in values.items():
        if name in {"threshold_distance", "market_prior"}:
            contribution = 0.0
        else:
            contribution = float(weights.get(name, _BASE_WEIGHTS.get(name, 0.0))) * value * qualities[name]
        contributions[name] = contribution
        evidence_logit += contribution
    evidence_cap = _env_float("Q15_V94_UNIFIED_EVIDENCE_LOGIT_CAP", 1.10, 0.20, 2.00)
    evidence_logit = _clamp(evidence_logit, -evidence_cap, evidence_cap)
    structural_logit = _logit(structural_yes)
    structural_scale = 1.0
    if checkpoint == "15M":
        # The reused bounded learner starts threshold_distance at 1.10. Only
        # the learned delta, not the absolute coefficient, calibrates the
        # structural model. This gives every resolved 15M outcome an immediate
        # but tightly bounded influence on future probability sharpness.
        learned_threshold = float(weights.get("threshold_distance", 1.10))
        structural_scale = _clamp(1.0 + (learned_threshold - 1.10) * 0.75, 0.75, 1.25)
    contributions["threshold_distance"] = (structural_scale - 1.0) * structural_logit
    raw_logit = structural_logit * structural_scale + evidence_logit
    raw_yes = _sigmoid(raw_logit)

    quality_components = {
        "core_volatility": 0.38 * vol_quality,
        "canonical_candles": 0.20 * _clamp(len(candles) / 60.0, 0.0, 1.0),
        "momentum": 0.12 * ((qualities["momentum_short"] + qualities["momentum_medium"]) / 2.0),
        "context": 0.10 * context_quality,
        "flow": 0.06 * flow_quality,
        "book": 0.05 * book_quality,
        "wicks": 0.05 * qualities["wick_structure"],
        "parent": 0.04 * qualities["parent_probability"],
    }
    quality = _clamp(sum(quality_components.values()), 0.0, 1.0)
    temperature = 1.0 + _env_float("Q15_V94_UNIFIED_LOW_QUALITY_TEMPERATURE", 1.25, 0.0, 3.0) * (1.0 - quality)
    calibrated_yes = _sigmoid(raw_logit / temperature)
    calibrated_yes = _clamp(calibrated_yes, 0.02, 0.98)

    return ProbabilityResult(
        yes_probability=calibrated_yes,
        structural_yes_probability=structural_yes,
        raw_yes_probability=raw_yes,
        quality=quality,
        feature_values={name: values[name] * qualities[name] for name in values},
        feature_available=available,
        feature_quality=qualities,
        contributions=contributions,
        details={
            "spot": spot,
            "target": target,
            "seconds_remaining": seconds,
            "yes_is_higher": yes_higher,
            "snapshot_timestamp": snapshot_ts,
            "snapshot_age_seconds": age,
            "candle_count": len(candles),
            "candle_cadence_seconds": cadence,
            "return_count": len(returns),
            "volatility_source": vol_source,
            "sigma_per_sqrt_second": sigma_per_sqrt_second,
            "horizon_sigma_fraction": horizon_sigma,
            "signed_log_distance": log_distance,
            "drift_horizon_fraction": drift_horizon,
            "structural_z": structural_z,
            "short_return_log": short_return,
            "medium_return_log": medium_return,
            "flow": flow_details,
            "book": book_details,
            "wick": wick_details,
            "context_score": context_score,
            "context_score_raw_selected_side": context_score_raw,
            "context_selected_side": context_selected_side,
            "context_quality": context_quality,
            "quality_components": quality_components,
            "temperature": temperature,
            "evidence_logit": evidence_logit,
            "structural_scale": structural_scale,
            "checkpoint": checkpoint,
            "market_price_used_in_probability": False,
        },
    )


def analyse_snapshot(
    snapshot: Mapping[str, Any],
    candles: Sequence[Mapping[str, Any]],
    context: Mapping[str, Any] | None,
    now: float,
    checkpoint: str,
    learner: Adaptive15LearningStore | None = None,
    *,
    minimum_probability: float | None = None,
    required_edge_cents: float | None = None,
    minimum_quality: float | None = None,
    maximum_spread_cents: float | None = None,
) -> dict[str, Any]:
    normalized_candles = _canonical_candles(snapshot, candles)
    min_probability = minimum_probability if minimum_probability is not None else _env_float(
        f"Q15_V94_UNIFIED_{checkpoint}_MIN_PROBABILITY", 0.58 if checkpoint == "15M" else 0.60, 0.50, 0.90
    )
    required_edge = required_edge_cents if required_edge_cents is not None else _env_float(
        f"Q15_V94_UNIFIED_{checkpoint}_REQUIRED_EDGE_CENTS", 4.0 if checkpoint == "15M" else 6.0, 0.0, 25.0
    )
    min_quality = minimum_quality if minimum_quality is not None else _env_float(
        f"Q15_V94_UNIFIED_{checkpoint}_MIN_DATA_QUALITY", 0.45 if checkpoint == "15M" else 0.50, 0.10, 0.95
    )
    max_spread = maximum_spread_cents if maximum_spread_cents is not None else _env_float(
        "Q15_V94_UNIFIED_MAX_SPREAD_CENTS", 12.0, 1.0, 50.0
    )

    weights = learner.weights() if checkpoint == "15M" and learner is not None else dict(_BASE_WEIGHTS)
    model = _probability_model(snapshot, normalized_candles, context, now, checkpoint, weights)
    base = {
        "version": VERSION,
        "model_version": MODEL_VERSION,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "read_only": True,
        "checkpoint": checkpoint,
        "ticker": _ticker(snapshot),
        "asset": _asset_name(snapshot.get("asset"), snapshot),
        "candle_count": len(normalized_candles),
        "minimum_probability": min_probability,
        "required_edge_cents": required_edge,
        "minimum_data_quality": min_quality,
        "maximum_spread_cents": max_spread,
        "learning_scope": "15M_ONLY",
        "automatic_threshold_changes": False,
    }
    if model is None:
        return {
            **base,
            "prediction_available": False,
            "prediction_side": "UNCERTAIN",
            "yes_probability": None,
            "no_probability": None,
            "selected_probability": None,
            "conservative_probability": None,
            "data_quality": 0.0,
            "confidence_grade": "UNAVAILABLE",
            "trade_decision": "AVOID_INVALID_DATA",
            "entry_allowed": False,
            "main_blocker": "Missing or stale spot, threshold, or time",
            "context": copy.deepcopy(dict(context or {})),
        }

    yes_probability = model.yes_probability
    preliminary_side = "YES" if yes_probability >= 0.5 else "NO"
    pattern = {"active": False, "adjustment_probability": 0.0, "reason": "10m_learning_disabled"}
    training_yes_probability = yes_probability
    if checkpoint == "15M" and learner is not None:
        pattern = learner.pattern_adjustment(model.feature_values, model.feature_available, preliminary_side)
        adjustment = _num(pattern.get("adjustment_probability"), 0.0) or 0.0
        yes_probability = _clamp(yes_probability + (adjustment if preliminary_side == "YES" else -adjustment), 0.02, 0.98)

    side = "YES" if yes_probability >= 0.5 else "NO"
    selected_probability = yes_probability if side == "YES" else 1.0 - yes_probability
    quality = model.quality
    uncertainty_margin = _clamp(0.035 + (1.0 - quality) * 0.085, 0.035, 0.12)
    conservative_probability = _clamp(selected_probability - uncertainty_margin, 0.0, 1.0)
    quote = _selected_quote(snapshot, side)
    costs = _estimated_costs(snapshot, quote)
    ask = _num(quote.get("ask_cents"))
    spread = _num(quote.get("spread_cents"))
    net_edge = None if ask is None else conservative_probability * 100.0 - ask - costs["total_cents"]
    ideal_entry = max(0.0, conservative_probability * 100.0 - costs["total_cents"] - required_edge)

    if quality >= 0.78 and selected_probability >= 0.68:
        grade = "A"
    elif quality >= 0.62 and selected_probability >= 0.60:
        grade = "B"
    elif quality >= 0.45:
        grade = "C"
    else:
        grade = "D"

    if ask is None:
        decision = "PREDICTION_ONLY"
        blocker = "Executable ask unavailable"
    elif spread is not None and spread > max_spread:
        decision = "WATCH_LIQUIDITY"
        blocker = f"Spread {spread:.2f}¢ exceeds {max_spread:.2f}¢"
    elif quality < min_quality:
        decision = "WATCH_DATA_QUALITY"
        blocker = f"Data quality {quality * 100:.1f}% below {min_quality * 100:.1f}%"
    elif conservative_probability < min_probability:
        decision = "WATCH_CONFIDENCE"
        blocker = f"Conservative probability {conservative_probability * 100:.1f}% below {min_probability * 100:.1f}%"
    elif net_edge is None or net_edge < required_edge:
        decision = "WATCH_PRICE"
        blocker = f"Net edge {net_edge:.2f}¢ below {required_edge:.2f}¢" if net_edge is not None else "Net edge unavailable"
    else:
        decision = "ENTRY_RECOMMENDED"
        blocker = None

    # Sort score is economics-first. This directly fixes SOL being ranked ahead
    # of BNB despite lower probability and negative edge.
    entry_tier = 1.0 if decision == "ENTRY_RECOMMENDED" else 0.0
    edge_score = _clamp((net_edge if net_edge is not None else -25.0) / 25.0, -1.0, 1.0)
    ranking_score = entry_tier * 10.0 + edge_score * 2.0 + conservative_probability + quality * 0.25

    return {
        **base,
        "prediction_available": True,
        "prediction_side": side,
        "yes_probability": yes_probability,
        "no_probability": 1.0 - yes_probability,
        "selected_probability": selected_probability,
        "structural_yes_probability": model.structural_yes_probability,
        "raw_yes_probability": model.raw_yes_probability,
        "training_yes_probability": training_yes_probability,
        "conservative_probability": conservative_probability,
        "uncertainty_margin": uncertainty_margin,
        "data_quality": quality,
        "confidence_grade": grade,
        "feature_values": dict(model.feature_values),
        "feature_available": dict(model.feature_available),
        "feature_quality": dict(model.feature_quality),
        "feature_contributions": dict(model.contributions),
        "model_details": dict(model.details),
        "pattern_learning": pattern,
        "quote": quote,
        "costs": costs,
        "net_edge_cents": net_edge,
        "ideal_entry_cents": ideal_entry,
        "trade_decision": decision,
        "entry_allowed": decision == "ENTRY_RECOMMENDED",
        "main_blocker": blocker,
        "ranking_score": ranking_score,
        "context": copy.deepcopy(dict(context or {})),
        "missing_optional_evidence": [name for name, present in model.feature_available.items() if not present],
        "flow_status": "AVAILABLE" if model.feature_available.get("executed_flow") else "UNAVAILABLE_NOT_OPPOSING",
        "book_status": "AVAILABLE" if model.feature_available.get("book_pressure") else "UNAVAILABLE_NOT_OPPOSING",
        "wick_status": "AVAILABLE" if model.feature_available.get("wick_structure") else "INSUFFICIENT_NOT_FAILED",
        "market_price_used_in_probability": False,
    }


def apply_unified_policy(snapshot: MutableMapping[str, Any], analysis: Mapping[str, Any]) -> MutableMapping[str, Any]:
    side = str(analysis.get("prediction_side") or "UNCERTAIN")
    entry_allowed = bool(analysis.get("entry_allowed"))
    snapshot["q15_v9_4_unified"] = copy.deepcopy(dict(analysis))
    snapshot["q15_v9_4_unified_version"] = VERSION
    snapshot["q15_v9_4_unified_probability_yes"] = analysis.get("yes_probability")
    snapshot["q15_v9_4_unified_probability_no"] = analysis.get("no_probability")
    snapshot["q15_v9_4_unified_selected_side"] = side
    snapshot["q15_v9_4_unified_trade_decision"] = analysis.get("trade_decision")
    snapshot["q15_v9_4_unified_entry_allowed"] = entry_allowed
    snapshot["q15_v9_4_unified_data_quality"] = analysis.get("data_quality")
    snapshot["q15_v9_4_unified_net_edge_cents"] = analysis.get("net_edge_cents")
    snapshot["q15_v9_4_unified_rank_score"] = analysis.get("ranking_score")
    snapshot["q15_v9_4_read_only"] = True
    if side in {"YES", "NO"}:
        snapshot["selected_side"] = side
        snapshot["entry_side"] = side
        snapshot["q15_final_result"] = side
    # Canonical final trade state supersedes the obsolete three-hard-gate state.
    snapshot["entry_allowed"] = entry_allowed
    snapshot["new_entry_allowed"] = entry_allowed
    snapshot["q15_new_entry_allowed"] = entry_allowed
    snapshot["decision_state"] = analysis.get("trade_decision")
    snapshot["entry_status"] = analysis.get("trade_decision")
    return snapshot


def rank_analyses(analyses: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for asset, analysis in analyses.items():
        if not analysis.get("prediction_available"):
            continue
        rows.append({
            "asset": asset,
            "side": analysis.get("prediction_side"),
            "selected_probability": analysis.get("selected_probability"),
            "conservative_probability": analysis.get("conservative_probability"),
            "data_quality": analysis.get("data_quality"),
            "trade_decision": analysis.get("trade_decision"),
            "entry_allowed": bool(analysis.get("entry_allowed")),
            "net_edge_cents": analysis.get("net_edge_cents"),
            "ideal_entry_cents": analysis.get("ideal_entry_cents"),
            "ranking_score": analysis.get("ranking_score"),
        })
    rows.sort(
        key=lambda row: (
            bool(row["entry_allowed"]),
            _num(row["net_edge_cents"], -1e9),
            _num(row["conservative_probability"], -1e9),
            _num(row["data_quality"], -1e9),
            str(row["asset"]),
        ),
        reverse=True,
    )
    for index, row in enumerate(rows, 1):
        row["rank"] = index
    return rows



@dataclass(frozen=True)
class _TelegramPermit:
    event_key: str
    kind: str
    fingerprint: str
    has_entry: bool
    reserved_at: float


class TelegramNotificationGate:
    """Persistent, concurrency-safe once-per-checkpoint Telegram gate.

    Normal behavior is intentionally conservative:
    * one initial report for each 10M/15M checkpoint and contract close window;
    * at most one additional report if that same checkpoint transitions from
      no-entry to ENTRY_RECOMMENDED;
    * failed sends remain retryable;
    * reservations expire so a crashed worker cannot permanently suppress an
      alert;
    * state survives process restarts and multi-worker deployments.
    """

    SCHEMA_VERSION = "q15-v94-telegram-gate-v1"

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self.db_path = str(
            db_path
            or os.environ.get("Q15_V94_TELEGRAM_GATE_DB")
            or "data/q15_v94_telegram_gate_v1.sqlite3"
        )
        self.reservation_seconds = _env_float(
            "Q15_V94_TELEGRAM_RESERVATION_SECONDS", 30.0, 5.0, 300.0
        )
        self.retention_seconds = _env_float(
            "Q15_V94_TELEGRAM_DEDUPE_RETENTION_SECONDS", 172800.0, 3600.0, 2592000.0
        )
        self.entry_transition_enabled = _env_bool(
            "Q15_V94_TELEGRAM_ENTRY_TRANSITION_ENABLED", True
        )
        self._lock = threading.RLock()
        path = os.path.abspath(self.db_path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db_path = path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _initialize(self) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_checkpoint_events (
                    event_key TEXT PRIMARY KEY,
                    checkpoint TEXT NOT NULL,
                    close_bucket INTEGER NOT NULL,
                    initial_sent INTEGER NOT NULL DEFAULT 0,
                    entry_sent INTEGER NOT NULL DEFAULT 0,
                    last_fingerprint TEXT,
                    last_sent_at REAL,
                    reserved_until REAL,
                    reserved_kind TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    successes INTEGER NOT NULL DEFAULT 0,
                    failures INTEGER NOT NULL DEFAULT 0,
                    suppressed INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_telegram_checkpoint_updated "
                "ON telegram_checkpoint_events(updated_at)"
            )

    def acquire(
        self,
        *,
        event_key: str,
        checkpoint: str,
        close_bucket: int,
        fingerprint: str,
        has_entry: bool,
        now: float,
    ) -> _TelegramPermit | None:
        """Atomically reserve an initial or entry-transition notification."""
        kind: str | None = None
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM telegram_checkpoint_events WHERE event_key = ?",
                    (event_key,),
                ).fetchone()
                if row is None:
                    kind = "INITIAL_ENTRY" if has_entry else "INITIAL"
                    connection.execute(
                        """
                        INSERT INTO telegram_checkpoint_events (
                            event_key, checkpoint, close_bucket, reserved_until,
                            reserved_kind, attempts, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            event_key,
                            checkpoint,
                            int(close_bucket),
                            now + self.reservation_seconds,
                            kind,
                            now,
                            now,
                        ),
                    )
                else:
                    reserved_until = _num(row["reserved_until"], 0.0) or 0.0
                    if reserved_until > now:
                        connection.execute(
                            "UPDATE telegram_checkpoint_events "
                            "SET suppressed = suppressed + 1, updated_at = ? WHERE event_key = ?",
                            (now, event_key),
                        )
                        connection.commit()
                        return None
                    initial_sent = bool(row["initial_sent"])
                    entry_sent = bool(row["entry_sent"])
                    if not initial_sent:
                        kind = "INITIAL_ENTRY" if has_entry else "INITIAL"
                    elif self.entry_transition_enabled and has_entry and not entry_sent:
                        kind = "ENTRY_TRANSITION"
                    else:
                        connection.execute(
                            "UPDATE telegram_checkpoint_events "
                            "SET suppressed = suppressed + 1, updated_at = ? WHERE event_key = ?",
                            (now, event_key),
                        )
                        connection.commit()
                        return None
                    connection.execute(
                        """
                        UPDATE telegram_checkpoint_events
                        SET reserved_until = ?, reserved_kind = ?, attempts = attempts + 1,
                            updated_at = ?
                        WHERE event_key = ?
                        """,
                        (now + self.reservation_seconds, kind, now, event_key),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return _TelegramPermit(
            event_key=event_key,
            kind=str(kind),
            fingerprint=fingerprint,
            has_entry=bool(has_entry),
            reserved_at=now,
        )

    def complete(self, permit: _TelegramPermit, *, success: bool, now: float) -> None:
        """Commit delivery state, or release the reservation for retry."""
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if success:
                    initial_sent = 1
                    entry_sent = 1 if permit.has_entry else 0
                    connection.execute(
                        """
                        UPDATE telegram_checkpoint_events
                        SET initial_sent = MAX(initial_sent, ?),
                            entry_sent = MAX(entry_sent, ?),
                            last_fingerprint = ?, last_sent_at = ?,
                            reserved_until = NULL, reserved_kind = NULL,
                            successes = successes + 1, updated_at = ?
                        WHERE event_key = ?
                        """,
                        (
                            initial_sent,
                            entry_sent,
                            permit.fingerprint,
                            now,
                            now,
                            permit.event_key,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE telegram_checkpoint_events
                        SET reserved_until = NULL, reserved_kind = NULL,
                            failures = failures + 1, updated_at = ?
                        WHERE event_key = ?
                        """,
                        (now, permit.event_key),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def cleanup(self, now: float) -> int:
        cutoff = now - self.retention_seconds
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                "DELETE FROM telegram_checkpoint_events WHERE updated_at < ?",
                (cutoff,),
            )
            return int(cursor.rowcount or 0)

    def status(self) -> dict[str, Any]:
        with self._lock, closing(self._connect()) as connection:
            totals = connection.execute(
                """
                SELECT COUNT(*) AS events,
                       COALESCE(SUM(attempts), 0) AS attempts,
                       COALESCE(SUM(successes), 0) AS successes,
                       COALESCE(SUM(failures), 0) AS failures,
                       COALESCE(SUM(suppressed), 0) AS suppressed
                FROM telegram_checkpoint_events
                """
            ).fetchone()
            latest = connection.execute(
                """
                SELECT event_key, checkpoint, close_bucket, initial_sent, entry_sent,
                       last_sent_at, attempts, successes, failures, suppressed
                FROM telegram_checkpoint_events
                ORDER BY updated_at DESC LIMIT 1
                """
            ).fetchone()
        return {
            "schema_version": self.SCHEMA_VERSION,
            "db_path": self.db_path,
            "events": int(totals["events"] or 0),
            "attempts": int(totals["attempts"] or 0),
            "successes": int(totals["successes"] or 0),
            "failures": int(totals["failures"] or 0),
            "suppressed": int(totals["suppressed"] or 0),
            "entry_transition_enabled": self.entry_transition_enabled,
            "latest": dict(latest) if latest is not None else None,
        }


def _notification_close_bucket(
    snapshots: Mapping[str, Mapping[str, Any]],
    analyses: Mapping[str, Mapping[str, Any]],
    now: float,
) -> int:
    close_times: list[float] = []
    for asset_key, snapshot in snapshots.items():
        if not isinstance(snapshot, Mapping):
            continue
        asset = _asset_name(asset_key, snapshot)
        analysis = analyses.get(asset) or {}
        details = analysis.get("model_details") or {}
        seconds = _num(details.get("seconds_remaining"))
        if seconds is None:
            seconds = _first_num(
                snapshot,
                (
                    "seconds_remaining", "time_remaining_seconds", "remaining_seconds",
                    "seconds_to_close", "time_to_close_seconds",
                ),
            )
        if seconds is not None and 0.0 <= seconds <= 7200.0:
            close_times.append(now + seconds)
            continue
        raw_close = _first_value(
            snapshot,
            ("close_time", "expiration_time", "expires_at", "settlement_time", "end_time"),
        )
        parsed_close = _parse_ts(raw_close)
        if parsed_close is not None:
            close_times.append(parsed_close)
    if close_times:
        # Median protects the group key from one malformed asset timestamp.
        close_time = statistics.median(close_times)
        return int(round(close_time / 60.0) * 60)
    # Fail-safe fallback: a five-minute bucket prevents a tight loop from
    # becoming a Telegram flood even when contract time fields disappear.
    return int(now // 300.0 * 300.0)


def _notification_identity(
    checkpoint: str,
    snapshots: Mapping[str, Mapping[str, Any]],
    analyses: Mapping[str, Mapping[str, Any]],
    ranking: Sequence[Mapping[str, Any]],
    now: float,
) -> tuple[str, int, str, bool]:
    normalized_checkpoint = checkpoint if checkpoint in {"10M", "15M"} else "UNKNOWN"
    close_bucket = _notification_close_bucket(snapshots, analyses, now)
    event_key = f"{normalized_checkpoint}:{close_bucket}"
    rows: list[dict[str, Any]] = []
    for row in ranking[:3]:
        rows.append(
            {
                "asset": str(row.get("asset") or ""),
                "side": str(row.get("side") or ""),
                "decision": str(row.get("trade_decision") or ""),
                "entry": bool(row.get("entry_allowed")),
                "probability_bin": (
                    round(float(row["selected_probability"]) * 20.0) / 20.0
                    if _num(row.get("selected_probability")) is not None
                    else None
                ),
                "edge_bin": (
                    round(float(row["net_edge_cents"]) / 2.0) * 2.0
                    if _num(row.get("net_edge_cents")) is not None
                    else None
                ),
            }
        )
    payload = {
        "checkpoint": normalized_checkpoint,
        "close_bucket": close_bucket,
        "rows": rows,
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    has_entry = any(bool(row.get("entry_allowed")) for row in ranking)
    return event_key, close_bucket, fingerprint, has_entry


class _BufferedNotifier:
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

    def messages(self) -> list[str]:
        return [str(args[0] or "") for args, _ in self.calls if args]

    def suppress_all(self, *, generated_message: bool = False) -> tuple[int, int, int]:
        suppressed = len([1 for args, _ in self.calls if args])
        if generated_message and suppressed == 0:
            suppressed = 1
        self.calls.clear()
        return 0, 0, suppressed

    def flush(self, message: str | None = None) -> tuple[int, int, int]:
        sender = getattr(self.real, "send", None)
        if not callable(sender):
            failures = len(self.calls)
            self.calls.clear()
            return 0, failures, 0
        sent = failed = suppressed = 0
        replacement_used = False
        for args, kwargs in self.calls:
            if not args:
                continue
            if message is not None:
                if replacement_used:
                    suppressed += 1
                    continue
                outgoing = (message, *args[1:])
                replacement_used = True
            else:
                outgoing = args
            try:
                result = sender(*outgoing, **kwargs)
                if result is False:
                    failed += 1
                else:
                    sent += 1
            except Exception:
                failed += 1
        # Some parent builds may not emit a message despite a valid cycle.
        if message is not None and not replacement_used:
            try:
                result = sender(message)
                if result is False:
                    failed += 1
                else:
                    sent += 1
            except Exception:
                failed += 1
        self.calls.clear()
        return sent, failed, suppressed


def _fmt_probability(value: Any) -> str:
    parsed = _probability(value)
    return "n/a" if parsed is None else f"{parsed * 100:.1f}%"


def _fmt_cents(value: Any, signed: bool = False) -> str:
    parsed = _num(value)
    if parsed is None:
        return "n/a"
    return f"{parsed:+.2f}¢" if signed else f"{parsed:.2f}¢"


def build_unified_message(
    checkpoint: str,
    analyses: Mapping[str, Mapping[str, Any]],
    ranking: Sequence[Mapping[str, Any]],
    learning: Mapping[str, Any],
) -> str:
    top = ranking[:3]
    any_entry = any(bool(row.get("entry_allowed")) for row in top)
    header = (
        f"✅ {checkpoint} UNIFIED CHECK — ENTRY RECOMMENDED"
        if any_entry
        else f"👀 {checkpoint} UNIFIED CHECK — BEST PICKS, NO ENTRY YET"
    )
    medal = ["🥇", "🥈", "🥉"]
    summary = "  ·  ".join(
        f"{medal[index]} {row['asset']} {row.get('side', '?')}"
        for index, row in enumerate(top)
    )
    lines = [header, summary, ""]
    for index, row in enumerate(top):
        analysis = analyses[str(row["asset"])]
        context = analysis.get("context") or {}
        previous = context.get("previous_15m") or {}
        current = context.get("current_15m") or {}
        details = analysis.get("model_details") or {}
        feature_available = analysis.get("feature_available") or {}
        quote = analysis.get("quote") or {}
        costs = analysis.get("costs") or {}
        side = analysis.get("prediction_side")
        lines.extend([
            f"{medal[index]} {row['asset']} {side}",
            f"Outcome probability — YES {_fmt_probability(analysis.get('yes_probability'))} | NO {_fmt_probability(analysis.get('no_probability'))}",
            "PROBABILITY QUALITY",
            f"Threshold/time/volatility base: {_fmt_probability(analysis.get('structural_yes_probability'))} YES",
            f"Final selected: {_fmt_probability(analysis.get('selected_probability'))} | conservative: {_fmt_probability(analysis.get('conservative_probability'))}",
            f"Confidence: {analysis.get('confidence_grade', 'n/a')} | data quality: {_fmt_probability(analysis.get('data_quality'))}",
            "CANONICAL PRICE ACTION",
            f"Shared candle cache: {analysis.get('candle_count', 0)} completed candles | cadence: {float(details.get('candle_cadence_seconds') or 0.0):.1f}s",
            f"Wicks: {analysis.get('wick_status')} | 30s momentum: {'AVAILABLE' if feature_available.get('momentum_short') else 'UNAVAILABLE'} | 180s momentum: {'AVAILABLE' if feature_available.get('momentum_medium') else 'UNAVAILABLE'}",
            f"Prior 15M: {previous.get('direction', 'UNAVAILABLE')} ({previous.get('candle_count', 0)} candles) | current 15M: {current.get('direction', 'UNAVAILABLE')} ({current.get('candle_count', 0)} candles)",
            "INDEPENDENT EVIDENCE",
            f"Executed flow: {analysis.get('flow_status')} | order book: {analysis.get('book_status')} | context score: {details.get('context_score', 'n/a')}",
            "EXECUTABLE VALUE",
            f"{side} ask: {_fmt_cents(quote.get('ask_cents'))} | estimated costs: {_fmt_cents(costs.get('total_cents'))}",
            f"Conservative net edge: {_fmt_cents(analysis.get('net_edge_cents'), signed=True)} | required: {_fmt_cents(analysis.get('required_edge_cents'))}",
            f"Ideal maximum entry: {_fmt_cents(analysis.get('ideal_entry_cents'))}",
            f"Decision: {analysis.get('trade_decision')}",
            f"Main blocker: {analysis.get('main_blocker') or 'None'}",
            "",
        ])
    if checkpoint == "15M":
        lines.extend([
            f"15M learning: {learning.get('unique_resolved', 0)} resolved | {learning.get('correct', 0)} correct | {learning.get('wrong', 0)} wrong | updates {learning.get('updates_applied', 0)}",
            "Learning is bounded and 15M-only; 10M outcomes never modify weights or thresholds.",
        ])
    lines.append("Probabilistic paper estimate only; it can be wrong. Read-only monitor — no orders are placed.")
    return "\n".join(lines).strip()


class CheckpointPolicyV94Unified(CheckpointPolicyV94):
    VERSION = VERSION

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.unified_enabled = _env_bool("Q15_V94_UNIFIED_ENABLED", True)
        self.signal_store = args[0] if args else kwargs.get("store")
        self.learning = Adaptive15LearningStore(
            os.environ.get("Q15_V94_UNIFIED_15M_LEARNING_DB")
            or "data/q15_v94_unified_15m_learning_v3.sqlite3"
        )
        self._unified_lock = threading.RLock()
        self._latest_unified: dict[str, dict[str, Any]] = {}
        self._latest_ranking: list[dict[str, Any]] = []
        self._last_checkpoint = "UNKNOWN"
        self._last_reconcile: dict[str, Any] = {}
        self._unified_cycles = 0
        self._unified_errors = 0
        self._unified_last_error: str | None = None
        self._telegram_sent = 0
        self._telegram_failed = 0
        self._telegram_suppressed = 0
        self.telegram_gate = TelegramNotificationGate()
        self._telegram_gate_errors = 0
        self._telegram_gate_last_error: str | None = None
        self._telegram_last_cleanup_at = 0.0

    def run_cycle(
        self,
        snapshots: dict[str, dict],
        now: float,
        ws_health: Mapping[str, Any] | None,
        focus_manager: Any,
        calibrated_edge: Any,
        notifier: Any,
    ) -> dict[str, dict]:
        deferred = _BufferedNotifier(notifier)
        parent_output = super().run_cycle(
            snapshots, now, ws_health, focus_manager, calibrated_edge, deferred
        )
        if not self.unified_enabled:
            sent, failed, suppressed = deferred.flush(None)
            self._telegram_sent += sent
            self._telegram_failed += failed
            self._telegram_suppressed += suppressed
            return parent_output

        try:
            checkpoint = _detect_checkpoint(
                {str(key): value for key, value in parent_output.items() if isinstance(value, Mapping)},
                deferred.messages(),
            )
            analyses: dict[str, dict[str, Any]] = {}
            output: dict[str, dict] = {}
            for asset_key, raw_snapshot in parent_output.items():
                if not isinstance(raw_snapshot, Mapping):
                    continue
                snapshot = copy.deepcopy(dict(raw_snapshot))
                asset = _asset_name(asset_key, snapshot)
                cached = self._candles(asset) if hasattr(self, "_candles") else []
                context = None
                if hasattr(self, "_latest_context"):
                    lock = getattr(self, "_context_lock", threading.RLock())
                    with lock:
                        context = copy.deepcopy(getattr(self, "_latest_context", {}).get(asset))
                if context is None:
                    maybe = snapshot.get("q15_v9_4_context")
                    context = copy.deepcopy(maybe) if isinstance(maybe, Mapping) else {}
                analysis = analyse_snapshot(
                    snapshot,
                    cached,
                    context,
                    now,
                    checkpoint,
                    self.learning if checkpoint == "15M" else None,
                )
                apply_unified_policy(snapshot, analysis)
                output[asset_key] = snapshot
                analyses[asset] = copy.deepcopy(analysis)

                if checkpoint == "15M" and analysis.get("prediction_available"):
                    ticker = _ticker(snapshot)
                    if ticker:
                        prediction_id, inserted = self.learning.record_prediction(
                            ticker=ticker,
                            asset=asset,
                            side=str(analysis["prediction_side"]),
                            yes_probability=float(analysis["yes_probability"]),
                            selected_probability=float(analysis["selected_probability"]),
                            data_quality=float(analysis["data_quality"]),
                            features=analysis["feature_values"],
                            available=analysis["feature_available"],
                            weights=self.learning.weights(),
                            created_at=now,
                            close_time=now + float(analysis.get("model_details", {}).get("seconds_remaining") or 0.0),
                            training_yes_probability=float(analysis["training_yes_probability"]),
                        )
                        analysis["prediction_id"] = prediction_id
                        analysis["new_unique_prediction_recorded"] = inserted

            ranking = rank_analyses(analyses)
            ranks = {str(row["asset"]): int(row["rank"]) for row in ranking}
            for asset_key, snapshot in output.items():
                asset = _asset_name(asset_key, snapshot)
                snapshot["q15_v9_4_unified_rank"] = ranks.get(asset)
                snapshot["q15_v9_4_unified_top_pick"] = ranks.get(asset) == 1
                if asset in analyses:
                    analyses[asset]["rank"] = ranks.get(asset)
                    analyses[asset]["top_pick"] = ranks.get(asset) == 1

            if checkpoint == "15M" and self.signal_store is not None:
                self._last_reconcile = self.learning.reconcile_from_signal_store(self.signal_store)
            learning_status = self.learning.status()
            message = build_unified_message(checkpoint, analyses, ranking, learning_status) if ranking else None
            if message is not None:
                event_key, close_bucket, fingerprint, has_entry = _notification_identity(
                    checkpoint, output, analyses, ranking, now
                )
                permit = self.telegram_gate.acquire(
                    event_key=event_key,
                    checkpoint=checkpoint,
                    close_bucket=close_bucket,
                    fingerprint=fingerprint,
                    has_entry=has_entry,
                    now=now,
                )
                if permit is None:
                    sent, failed, suppressed = deferred.suppress_all(generated_message=True)
                else:
                    sent, failed, suppressed = deferred.flush(message)
                    self.telegram_gate.complete(permit, success=sent > 0 and failed == 0, now=now)
                if now - self._telegram_last_cleanup_at >= 3600.0:
                    self.telegram_gate.cleanup(now)
                    self._telegram_last_cleanup_at = now
            else:
                sent, failed, suppressed = deferred.suppress_all(generated_message=False)
            self._telegram_sent += sent
            self._telegram_failed += failed
            self._telegram_suppressed += suppressed
            self._unified_cycles += 1
            self._unified_last_error = None
            with self._unified_lock:
                self._latest_unified = copy.deepcopy(analyses)
                self._latest_ranking = copy.deepcopy(ranking)
                self._last_checkpoint = checkpoint
            with _LATEST_LOCK:
                global _LATEST_CHECKPOINT
                _LATEST_ANALYSES.clear()
                _LATEST_ANALYSES.update(copy.deepcopy(analyses))
                _LATEST_RANKING.clear()
                _LATEST_RANKING.extend(copy.deepcopy(ranking))
                _LATEST_LEARNING.clear()
                _LATEST_LEARNING.update(copy.deepcopy(learning_status))
                _LATEST_CHECKPOINT = checkpoint
            return output
        except Exception as exc:
            self._unified_errors += 1
            self._unified_last_error = f"{type(exc).__name__}: {exc}"
            # Preserve the parent report on repair-layer failure and fail closed
            # for fresh entries in the returned snapshot.
            for snapshot in parent_output.values():
                if isinstance(snapshot, MutableMapping):
                    snapshot["q15_v9_4_unified_error"] = self._unified_last_error
                    snapshot["q15_v9_4_unified_entry_allowed"] = False
                    snapshot["entry_allowed"] = False
                    snapshot["new_entry_allowed"] = False
            try:
                error_checkpoint = _detect_checkpoint(
                    {str(key): value for key, value in parent_output.items() if isinstance(value, Mapping)},
                    deferred.messages(),
                )
                error_message = (
                    f"⚠️ {error_checkpoint} V9.4 REPORT ERROR — NOTIFICATION THROTTLED\n"
                    f"{self._unified_last_error}\n"
                    "Trade entry is fail-closed. Read-only monitor — no orders are placed."
                )
                error_analyses: dict[str, dict[str, Any]] = {}
                event_key, close_bucket, fingerprint, _ = _notification_identity(
                    error_checkpoint,
                    {str(key): value for key, value in parent_output.items() if isinstance(value, Mapping)},
                    error_analyses,
                    [],
                    now,
                )
                error_key = f"ERROR:{event_key}:{hashlib.sha256(self._unified_last_error.encode()).hexdigest()[:12]}"
                permit = self.telegram_gate.acquire(
                    event_key=error_key,
                    checkpoint=error_checkpoint,
                    close_bucket=close_bucket,
                    fingerprint=fingerprint,
                    has_entry=False,
                    now=now,
                )
                if permit is None:
                    sent, failed, suppressed = deferred.suppress_all(generated_message=True)
                else:
                    sent, failed, suppressed = deferred.flush(error_message)
                    self.telegram_gate.complete(permit, success=sent > 0 and failed == 0, now=now)
            except Exception as gate_exc:
                self._telegram_gate_errors += 1
                self._telegram_gate_last_error = f"{type(gate_exc).__name__}: {gate_exc}"
                sent, failed, suppressed = deferred.suppress_all(generated_message=True)
            self._telegram_sent += sent
            self._telegram_failed += failed
            self._telegram_suppressed += suppressed
            return parent_output

    def unified_predictions(self) -> dict[str, Any]:
        with self._unified_lock:
            analyses = copy.deepcopy(self._latest_unified)
            ranking = copy.deepcopy(self._latest_ranking)
            checkpoint = self._last_checkpoint
        return {
            "version": VERSION,
            "model_version": MODEL_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "read_only": True,
            "checkpoint": checkpoint,
            "top_pick": ranking[0] if ranking else None,
            "ranking": ranking,
            "assets": [{"asset": asset, **analysis} for asset, analysis in sorted(analyses.items())],
        }

    def unified_learning_status(self) -> dict[str, Any]:
        return {
            **self.learning.status(),
            "last_reconcile": copy.deepcopy(self._last_reconcile),
            "scope": "15M_ONLY",
            "ten_minute_learning": False,
            "automatic_threshold_changes": False,
        }

    def health(self) -> dict[str, Any]:
        try:
            parent = super().health()
        except Exception as exc:
            parent = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
        return {
            "version": VERSION,
            "enabled": self.unified_enabled,
            "read_only": True,
            "cycles": self._unified_cycles,
            "errors": self._unified_errors,
            "last_error": self._unified_last_error,
            "last_checkpoint": self._last_checkpoint,
            "telegram_sent": self._telegram_sent,
            "telegram_failed": self._telegram_failed,
            "telegram_duplicates_suppressed": self._telegram_suppressed,
            "telegram_gate_errors": self._telegram_gate_errors,
            "telegram_gate_last_error": self._telegram_gate_last_error,
            "telegram_gate": self.telegram_gate.status(),
            "telegram_policy": "ONE_INITIAL_PLUS_ONE_ENTRY_TRANSITION_PER_CHECKPOINT",
            "single_canonical_candle_pipeline": True,
            "probability_separate_from_value": True,
            "market_price_used_in_probability": False,
            "optional_evidence_hard_veto": False,
            "learning_scope": "15M_ONLY",
            "automatic_threshold_changes": False,
            "parent_v94": parent,
        }

    def diagnostics(self) -> dict[str, Any]:
        try:
            parent = super().diagnostics()
        except Exception as exc:
            parent = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
        return {
            **self.health(),
            "settings": {
                "ten_minute_minimum_probability": _env_float("Q15_V94_UNIFIED_10M_MIN_PROBABILITY", 0.60, 0.50, 0.90),
                "fifteen_minute_minimum_probability": _env_float("Q15_V94_UNIFIED_15M_MIN_PROBABILITY", 0.58, 0.50, 0.90),
                "ten_minute_required_edge_cents": _env_float("Q15_V94_UNIFIED_10M_REQUIRED_EDGE_CENTS", 6.0, 0.0, 25.0),
                "fifteen_minute_required_edge_cents": _env_float("Q15_V94_UNIFIED_15M_REQUIRED_EDGE_CENTS", 4.0, 0.0, 25.0),
                "maximum_spread_cents": _env_float("Q15_V94_UNIFIED_MAX_SPREAD_CENTS", 12.0, 1.0, 50.0),
                "base_weights": dict(_BASE_WEIGHTS),
                "telegram_gate_db": self.telegram_gate.db_path,
                "telegram_reservation_seconds": self.telegram_gate.reservation_seconds,
                "telegram_dedupe_retention_seconds": self.telegram_gate.retention_seconds,
            },
            "hard_failures": [
                "missing or malformed spot price",
                "missing or malformed threshold",
                "missing or malformed time remaining",
                "stale timestamped core data",
            ],
            "optional_not_hard_failures": [
                "wick sequence",
                "executed flow",
                "order book",
                "parent model probability",
                "30-minute context",
            ],
            "latest": self.unified_predictions(),
            "learning": self.unified_learning_status(),
            "parent_v94_diagnostics": parent,
        }

    def decision_stats(self) -> dict[str, Any]:
        try:
            parent = super().decision_stats()
        except Exception as exc:
            parent = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
        with self._unified_lock:
            counts: dict[str, int] = {}
            for analysis in self._latest_unified.values():
                state = str(analysis.get("trade_decision") or "UNKNOWN")
                counts[state] = counts.get(state, 0) + 1
        return {
            "version": VERSION,
            "read_only": True,
            "checkpoint": self._last_checkpoint,
            "current_trade_decisions": counts,
            "learning": self.unified_learning_status(),
            "parent_v94_decision_stats": parent,
        }


def format_telegram_message(text: Any) -> str:
    message = str(text or "")
    if "UNIFIED CHECK" in message:
        return message
    with _LATEST_LOCK:
        analyses = copy.deepcopy(_LATEST_ANALYSES)
        ranking = copy.deepcopy(_LATEST_RANKING)
        learning = copy.deepcopy(_LATEST_LEARNING)
        checkpoint = _LATEST_CHECKPOINT
    if analyses and ranking and any(token in message.upper() for token in ("10M", "15M", "WATCH", "ENTRY", "NO TRADE")):
        return build_unified_message(checkpoint, analyses, ranking, learning)
    return _format_v94_message(message)


__all__ = [
    "FEATURE_SCHEMA_VERSION",
    "MODEL_VERSION",
    "READ_ONLY",
    "VERSION",
    "CheckpointPolicyV94Unified",
    "TelegramNotificationGate",
    "analyse_snapshot",
    "apply_unified_policy",
    "build_unified_message",
    "format_telegram_message",
    "rank_analyses",
]
