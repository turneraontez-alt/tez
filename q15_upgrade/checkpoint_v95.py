"""Q15 V9.5 canonical snapshot, calibrated ensemble, and champion/challenger policy.

This release is deliberately read-only.  It produces a directional probability
whenever core data is valid, then evaluates whether the current Kalshi quote is
actually executable and attractive.  Production coefficients are frozen; only
a bounded shadow challenger learns from unique, officially settled 15-minute
predictions.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import statistics
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, MutableMapping, Sequence

from .checkpoint_v94_unified import (
    CheckpointPolicyV94Unified,
    _BufferedNotifier,
    _asset_name,
    _book_score,
    _cadence,
    _canonical_candles,
    _detect_checkpoint,
    _estimated_costs,
    _ewma_variance,
    _first_num,
    _first_value,
    _flow_score,
    _iso,
    _log_returns,
    _parse_ts,
    _selected_quote,
    _seconds_remaining,
    _spot,
    _target,
    _ticker,
    _window_return,
    _wick_score,
    _winsorize,
    _yes_is_higher,
    format_telegram_message as _format_v94_message,
)
from .ledger_v95 import (
    CHAMPION_WEIGHTS,
    FEATURE_SCHEMA_VERSION,
    MODEL_VERSION,
    V95Ledger,
)
from .market_data_v95 import PublicMarketDataHub

VERSION = "q15-v9.5.2-runtime-activation-data-bridge-v1"
READ_ONLY = True

_LATEST_LOCK = threading.RLock()
_LATEST_ANALYSES: dict[str, dict[str, Any]] = {}
_LATEST_RANKING: list[dict[str, Any]] = []
_LATEST_CHECKPOINT = "UNKNOWN"
_LATEST_LEDGER: dict[str, Any] = {}


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
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def _logit(probability: float) -> float:
    p = _clamp(probability, 1e-6, 1.0 - 1e-6)
    return math.log(p / (1.0 - p))


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _weighted_mean(values: Sequence[tuple[float, float]]) -> float | None:
    cleaned = [(float(value), max(0.0, float(weight))) for value, weight in values if math.isfinite(float(value)) and float(weight) > 0]
    total = sum(weight for _, weight in cleaned)
    return None if total <= 0 else sum(value * weight for value, weight in cleaned) / total


def _weighted_median(values: Sequence[tuple[float, float]]) -> float | None:
    cleaned = sorted((float(value), max(0.0, float(weight))) for value, weight in values if float(value) > 0 and float(weight) > 0)
    if not cleaned:
        return None
    total = sum(weight for _, weight in cleaned)
    running = 0.0
    for value, weight in cleaned:
        running += weight
        if running >= total / 2.0:
            return value
    return cleaned[-1][0]


def _source_timestamp(snapshot: Mapping[str, Any], aliases: Sequence[str]) -> float | None:
    return _parse_ts(_first_value(snapshot, aliases))


def _asset_vol_floor(asset: str) -> float:
    defaults = {
        "BTC": 0.000012, "ETH": 0.000016, "SOL": 0.000030, "BNB": 0.000022,
        "XRP": 0.000028, "DOGE": 0.000035, "HYPE": 0.000045,
    }
    key = f"Q15_V95_{asset}_SIGMA_FLOOR"
    return _env_float(key, defaults.get(asset, 0.000030), 0.000002, 0.001)


@dataclass(frozen=True)
class CanonicalSnapshot:
    asset: str
    ticker: str | None
    checkpoint: str
    observed_at: float
    observed_at_iso: str
    settlement_time: float | None
    seconds_remaining: float | None
    threshold: float | None
    spot: float | None
    spot_source: str
    yes_is_higher: bool
    candles: tuple[dict[str, float], ...]
    context: dict[str, Any]
    public: dict[str, Any]
    feed_timestamps: dict[str, float | None]
    feed_ages: dict[str, float | None]
    alignment_seconds: float | None
    core_valid: bool
    core_errors: tuple[str, ...]
    data_quality: float

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["candles"] = list(self.candles)
        return value


def build_canonical_snapshot(
    snapshot: Mapping[str, Any],
    *,
    asset: str,
    checkpoint: str,
    now: float,
    cached_candles: Sequence[Mapping[str, Any]] | None,
    context: Mapping[str, Any] | None,
    public: Mapping[str, Any] | None,
) -> CanonicalSnapshot:
    candles = _canonical_candles(snapshot, cached_candles)
    public_data = copy.deepcopy(dict(public or {}))
    snapshot_spot = _spot(snapshot, candles)
    public_spot = _num(public_data.get("composite_price"))
    source_agreement = _clamp(_num(public_data.get("source_agreement"), 0.0) or 0.0, 0.0, 1.0)
    public_age = _num(public_data.get("age_seconds"))
    public_freshness = 0.0
    if public_spot is not None and public_spot > 0:
        if public_age is None:
            public_freshness = 0.45
        else:
            public_freshness = _clamp(math.exp(-max(0.0, public_age - 5.0) / 30.0), 0.0, 1.0)
    candidates: list[tuple[float, float]] = []
    if snapshot_spot is not None and snapshot_spot > 0:
        candidates.append((snapshot_spot, 1.0))
    if public_spot is not None and public_spot > 0 and public_freshness > 0.20:
        candidates.append((public_spot, max(0.15, public_freshness * (0.50 + 0.50 * source_agreement))))
    spot = _weighted_median(candidates)
    spot_source = "missing"
    if spot is not None:
        if snapshot_spot is not None and public_spot is not None:
            spot_source = "snapshot_public_weighted_median"
        elif snapshot_spot is not None:
            spot_source = "snapshot"
        else:
            spot_source = "public_composite"

    seconds = _seconds_remaining(snapshot, now)
    settlement = None if seconds is None else now + seconds
    target = _target(snapshot)
    core_ts = _source_timestamp(snapshot, ("snapshot_time", "snapshot_timestamp", "updated_at", "last_update", "timestamp", "ts"))
    candle_ts = float(candles[-1].get("timestamp") or candles[-1].get("end_time") or 0.0) if candles else None
    quote_ts = _source_timestamp(snapshot, ("quote_timestamp", "market_quote_timestamp", "orderbook_timestamp", "kalshi_timestamp"))
    public_ts = _num(public_data.get("fetched_at"))
    if core_ts is None:
        core_ts = now
    timestamps = {"core": core_ts, "candle": candle_ts, "quote": quote_ts, "public": public_ts}
    ages = {name: None if ts is None else max(0.0, now - ts) for name, ts in timestamps.items()}
    alignment_values = [ts for ts in timestamps.values() if ts is not None and now - ts <= 120.0]
    alignment = max(alignment_values) - min(alignment_values) if len(alignment_values) >= 2 else None

    errors: list[str] = []
    if spot is None or spot <= 0:
        errors.append("missing_or_invalid_spot")
    if target is None or target <= 0:
        errors.append("missing_or_invalid_threshold")
    if seconds is None:
        errors.append("missing_time_remaining")
    elif seconds <= 0:
        errors.append("contract_closed")
    max_core_age = _env_float("Q15_V95_MAX_CORE_AGE_SECONDS", 30.0, 2.0, 300.0)
    if ages["core"] is not None and ages["core"] > max_core_age:
        errors.append("stale_core_snapshot")
    if candle_ts is not None and ages["candle"] is not None and ages["candle"] > 90.0:
        errors.append("stale_candle_cache")

    candle_quality = _clamp(len(candles) / 120.0, 0.0, 1.0)
    core_quality = 1.0 if ages["core"] is not None and ages["core"] <= 10.0 else 0.75
    alignment_quality = 0.65 if alignment is None else _clamp(1.0 - alignment / 45.0, 0.0, 1.0)
    public_quality = 0.0 if public_spot is None else public_freshness * (0.40 + 0.60 * source_agreement)
    data_quality = _clamp(0.38 * core_quality + 0.32 * candle_quality + 0.18 * alignment_quality + 0.12 * public_quality, 0.0, 1.0)
    if errors:
        data_quality *= 0.50
    return CanonicalSnapshot(
        asset=asset,
        ticker=_ticker(snapshot),
        checkpoint=checkpoint,
        observed_at=now,
        observed_at_iso=_iso(now) or "",
        settlement_time=settlement,
        seconds_remaining=seconds,
        threshold=target,
        spot=spot,
        spot_source=spot_source,
        yes_is_higher=_yes_is_higher(snapshot),
        candles=tuple(candles),  # already freshly built by _canonical_candles; no copy needed
        context=copy.deepcopy(dict(context or {})),
        public=public_data,
        feed_timestamps=timestamps,
        feed_ages=ages,
        alignment_seconds=alignment,
        core_valid=not errors,
        core_errors=tuple(errors),
        data_quality=data_quality,
    )


def _robust_volatility(canonical: CanonicalSnapshot) -> dict[str, Any]:
    candles = canonical.candles
    cadence = _cadence(candles)
    returns = _winsorize(_log_returns(candles), limit=6.0)
    variance = _ewma_variance(returns[-240:], cadence, half_life_seconds=90.0)
    floor = _asset_vol_floor(canonical.asset)
    sigma = floor
    source = "asset_floor"
    if variance is not None and variance > 0:
        sigma = max(floor, math.sqrt(variance / max(cadence, 0.25)))
        source = "robust_ewma"
    sample_quality = _clamp(len(returns) / 90.0, 0.0, 1.0)
    return {
        "sigma_per_sqrt_second": sigma,
        "cadence_seconds": cadence,
        "return_count": len(returns),
        "quality": sample_quality,
        "source": source,
        "floor": floor,
    }


def _multi_horizon_returns(canonical: CanonicalSnapshot) -> dict[str, float | None]:
    result = {f"return_{seconds}s": _window_return(canonical.candles, float(seconds)) for seconds in (5, 15, 30, 60, 180, 900, 1800)}
    public_returns = canonical.public.get("price_returns") if isinstance(canonical.public.get("price_returns"), Mapping) else {}
    for seconds in (5, 15, 30, 60, 180):
        key = f"return_{seconds}s"
        public_value = _num(public_returns.get(key))
        candle_value = result.get(key)
        if public_value is not None and candle_value is not None:
            result[key] = 0.65 * candle_value + 0.35 * math.log1p(public_value)
        elif candle_value is None and public_value is not None:
            result[key] = math.log1p(public_value)
    return result


def _structural_probability(canonical: CanonicalSnapshot, volatility: Mapping[str, Any], returns: Mapping[str, Any]) -> dict[str, Any]:
    if not canonical.core_valid or canonical.spot is None or canonical.threshold is None or canonical.seconds_remaining is None:
        return {"available": False, "yes_probability": None}
    orientation = 1.0 if canonical.yes_is_higher else -1.0
    distance = orientation * math.log(canonical.spot / canonical.threshold)
    sigma = float(volatility["sigma_per_sqrt_second"])
    horizon_sigma = max(1e-8, sigma * math.sqrt(max(1.0, canonical.seconds_remaining)))
    short = _num(returns.get("return_30s"), 0.0) or 0.0
    medium = _num(returns.get("return_180s"), short) or short
    per_second_drift = 0.65 * short / 30.0 + 0.35 * medium / 180.0
    projected = orientation * per_second_drift * canonical.seconds_remaining
    drift_cap = _env_float("Q15_V95_MAX_DRIFT_FRACTION_OF_SIGMA", 0.35, 0.0, 1.0) * horizon_sigma
    projected = _clamp(projected, -drift_cap, drift_cap)
    z = _clamp((distance + projected) / horizon_sigma, -5.0, 5.0)
    yes_probability = _clamp(_normal_cdf(z), 0.01, 0.99)
    return {
        "available": True,
        "yes_probability": yes_probability,
        "signed_log_distance": distance,
        "projected_signed_drift": projected,
        "horizon_sigma": horizon_sigma,
        "z_score": z,
        "distance_in_sigma": distance / horizon_sigma,
    }


def _momentum_feature(returns: Mapping[str, Any], volatility: Mapping[str, Any], canonical: CanonicalSnapshot) -> tuple[float, float, dict[str, Any]]:
    orientation = 1.0 if canonical.yes_is_higher else -1.0
    sigma = max(float(volatility["sigma_per_sqrt_second"]), 1e-9)
    terms: list[tuple[float, float]] = []
    for seconds, weight in ((15, 0.20), (30, 0.30), (60, 0.30), (180, 0.20)):
        value = _num(returns.get(f"return_{seconds}s"))
        if value is not None:
            normalized = orientation * value / max(sigma * math.sqrt(seconds), 1e-9)
            terms.append((_clamp(normalized / 2.0, -1.0, 1.0), weight))
    score = _weighted_mean(terms)
    quality = _clamp(sum(weight for _, weight in terms), 0.0, 1.0)
    return (score or 0.0), quality, {"horizons": len(terms), "returns": dict(returns)}


def _combine_public_signal(public: Mapping[str, Any], field: str) -> tuple[float | None, float, dict[str, Any]]:
    sources = public.get("sources") if isinstance(public.get("sources"), Mapping) else {}
    values: list[tuple[float, float]] = []
    details: dict[str, Any] = {}
    age = _num(public.get("age_seconds"))
    freshness = 0.55 if age is None else _clamp(math.exp(-max(0.0, age - 5.0) / 30.0), 0.0, 1.0)
    for name, source in sources.items():
        if not isinstance(source, Mapping):
            continue
        section = source.get(field)
        if not isinstance(section, Mapping) or not section.get("available"):
            continue
        raw = _num(section.get("imbalance"))
        quality = _num(source.get("quality"), 0.5) or 0.5
        if raw is not None:
            values.append((_clamp(raw, -1.0, 1.0), quality * freshness))
            details[str(name)] = dict(section)
    score = _weighted_mean(values)
    quality = _clamp(sum(weight for _, weight in values) / max(1.0, len(values)), 0.0, 1.0)
    return score, quality, details


def _flow_feature(snapshot: Mapping[str, Any], canonical: CanonicalSnapshot) -> tuple[float, float, dict[str, Any]]:
    local, local_quality, local_details = _flow_score(snapshot)
    external, external_quality, external_details = _combine_public_signal(canonical.public, "flow")
    values: list[tuple[float, float]] = []
    if local is not None:
        values.append((local, local_quality))
    if external is not None:
        values.append((external, external_quality))
    score = _weighted_mean(values)
    quality = _clamp(sum(weight for _, weight in values) / max(1.0, len(values)), 0.0, 1.0)
    return (score or 0.0), quality, {"local": local_details, "public": external_details, "available_sources": len(values)}


def _book_feature(snapshot: Mapping[str, Any], canonical: CanonicalSnapshot) -> tuple[float, float, dict[str, Any]]:
    local, local_quality, local_details = _book_score(snapshot)
    external, external_quality, external_details = _combine_public_signal(canonical.public, "book")
    values: list[tuple[float, float]] = []
    if local is not None:
        values.append((local, local_quality))
    if external is not None:
        values.append((external, external_quality))
    score = _weighted_mean(values)
    quality = _clamp(sum(weight for _, weight in values) / max(1.0, len(values)), 0.0, 1.0)
    return (score or 0.0), quality, {"local": local_details, "public": external_details, "available_sources": len(values)}


def _context_feature(canonical: CanonicalSnapshot) -> tuple[float, float, dict[str, Any]]:
    context = canonical.context
    direction_map = {"BULLISH": 1.0, "BEARISH": -1.0, "NEUTRAL": 0.0, "MIXED": 0.0}
    previous = context.get("previous_15m") if isinstance(context.get("previous_15m"), Mapping) else {}
    current = context.get("current_15m") if isinstance(context.get("current_15m"), Mapping) else {}
    values: list[tuple[float, float]] = []
    for row, weight in ((previous, 0.40), (current, 0.60)):
        direction = direction_map.get(str(row.get("direction") or "").upper())
        coverage = _clamp(_num(row.get("coverage"), 0.0) or 0.0, 0.0, 1.0)
        if direction is not None and coverage > 0:
            values.append((direction, weight * coverage))
    score = _weighted_mean(values)
    if score is None:
        raw = _num(context.get("combined_side_support"))
        selected_side = str(context.get("selected_side") or "").upper()
        if raw is not None and selected_side in {"YES", "NO"}:
            yes_score = raw if selected_side == "YES" else -raw
            score = yes_score
            values.append((yes_score, 0.45))
    orientation = 1.0 if canonical.yes_is_higher else -1.0
    yes_score = (score or 0.0) * orientation
    quality = _clamp(sum(weight for _, weight in values), 0.0, 1.0)
    return _clamp(yes_score, -1.0, 1.0), quality, {"previous": previous, "current": current, "status": context.get("status")}


def _threshold_interaction(canonical: CanonicalSnapshot) -> tuple[float, float, dict[str, Any]]:
    if canonical.threshold is None or len(canonical.candles) < 6:
        return 0.0, 0.0, {"available": False}
    rows = list(canonical.candles[-180:])
    orientation = 1.0 if canonical.yes_is_higher else -1.0
    signed = [orientation * math.log(float(row["close"]) / canonical.threshold) for row in rows]
    signs = [1 if value >= 0 else -1 for value in signed]
    crossings = sum(1 for a, b in zip(signs, signs[1:]) if a != b)
    above_fraction = sum(1 for value in signed if value >= 0) / len(signed)
    recent = signed[-min(12, len(signed)):]
    persistence = sum(1 for value in recent if value >= 0) / len(recent)
    latest = signed[-1]
    max_positive = max(signed)
    min_negative = min(signed)
    failed_breakout = max_positive > 0 and latest < 0 and crossings >= 1
    failed_breakdown = min_negative < 0 and latest > 0 and crossings >= 1
    score = 0.45 * (2.0 * persistence - 1.0) + 0.25 * (2.0 * above_fraction - 1.0)
    score += 0.30 if failed_breakdown else -0.30 if failed_breakout else 0.0
    if crossings >= 6:
        score *= 0.55
    quality = _clamp(len(rows) / 90.0, 0.0, 1.0)
    return _clamp(score, -1.0, 1.0), quality, {
        "available": True, "crossings": crossings, "yes_side_fraction": above_fraction,
        "recent_yes_persistence": persistence, "failed_breakout": failed_breakout,
        "failed_breakdown": failed_breakdown, "latest_signed_log_distance": latest,
    }


def _exchange_consensus(canonical: CanonicalSnapshot, returns: Mapping[str, Any]) -> tuple[float, float, dict[str, Any]]:
    public = canonical.public
    count = int(_num(public.get("source_count"), 0) or 0)
    agreement = _clamp(_num(public.get("source_agreement"), 0.0) or 0.0, 0.0, 1.0)
    divergence = max(0.0, _num(public.get("divergence_bps"), 0.0) or 0.0)
    public_returns = public.get("price_returns") if isinstance(public.get("price_returns"), Mapping) else {}
    momentum = _num(public_returns.get("return_30s"))
    if momentum is None:
        momentum = _num(returns.get("return_30s"))
    orientation = 1.0 if canonical.yes_is_higher else -1.0
    score = 0.0 if momentum is None else _clamp(orientation * momentum / 0.0025, -1.0, 1.0)
    quality = _clamp((count / 2.0) * agreement * math.exp(-divergence / 50.0), 0.0, 1.0)
    return score, quality, {"source_count": count, "agreement": agreement, "divergence_bps": divergence}


def _derivatives_feature(canonical: CanonicalSnapshot, momentum_score: float) -> tuple[float, float, dict[str, Any]]:
    derivative = canonical.public.get("derivatives") if isinstance(canonical.public.get("derivatives"), Mapping) else {}
    changes = canonical.public.get("derivative_changes") if isinstance(canonical.public.get("derivative_changes"), Mapping) else {}
    mark_return = _num(changes.get("mark_return_180s"))
    oi_change = _num(changes.get("open_interest_change_180s"))
    basis = _num(derivative.get("basis_bps"))
    funding = _num(derivative.get("current_funding"))
    values: list[float] = []
    if mark_return is not None:
        values.append(_clamp(mark_return / 0.003, -1.0, 1.0))
    if oi_change is not None and abs(oi_change) > 1e-8:
        values.append(_clamp(math.copysign(min(abs(oi_change) / 0.01, 1.0), momentum_score or oi_change), -1.0, 1.0))
    if basis is not None:
        values.append(_clamp(basis / 25.0, -1.0, 1.0) * 0.35)
    if funding is not None:
        values.append(_clamp(funding / 0.0005, -1.0, 1.0) * 0.20)
    score = statistics.mean(values) if values else 0.0
    quality = _clamp(len(values) / 3.0, 0.0, 1.0)
    orientation = 1.0 if canonical.yes_is_higher else -1.0
    return _clamp(score * orientation, -1.0, 1.0), quality, {"derivatives": derivative, "changes": changes}


def _absorption_feature(flow: float, flow_quality: float, momentum: float, momentum_quality: float) -> tuple[float, float, dict[str, Any]]:
    if flow_quality < 0.25 or momentum_quality < 0.25 or abs(flow) < 0.35:
        return 0.0, 0.0, {"available": False}
    opposition = flow * momentum <= 0.0 or abs(momentum) < 0.10
    if not opposition:
        return 0.0, min(flow_quality, momentum_quality), {"available": True, "absorbed": False}
    # Positive aggressive flow failing to lift price is bearish; negative flow
    # failing to push price lower is bullish.
    score = -math.copysign(min(abs(flow), 1.0), flow)
    quality = min(flow_quality, momentum_quality)
    return score, quality, {"available": True, "absorbed": True, "flow": flow, "momentum": momentum}


def _regime(canonical: CanonicalSnapshot, volatility: Mapping[str, Any], returns: Mapping[str, Any], threshold: Mapping[str, Any], exchange: Mapping[str, Any]) -> dict[str, Any]:
    sigma = float(volatility["sigma_per_sqrt_second"])
    one_minute_sigma = sigma * math.sqrt(60.0)
    distance_sigma = abs(_num(threshold.get("latest_signed_log_distance"), 0.0) or 0.0) / max(sigma * math.sqrt(max(1.0, canonical.seconds_remaining or 1.0)), 1e-9)
    crossings = int(_num(threshold.get("crossings"), 0) or 0)
    divergence = _num(exchange.get("divergence_bps"), 0.0) or 0.0
    return_180 = abs(_num(returns.get("return_180s"), 0.0) or 0.0)
    if divergence >= 35:
        name, uncertainty = "EXCHANGE_DIVERGENCE", 0.18
    elif distance_sigma <= 0.25 and crossings >= 4:
        name, uncertainty = "THRESHOLD_PIN", 0.20
    elif one_minute_sigma >= 0.0045 or return_180 >= 0.012:
        name, uncertainty = "HIGH_VOLATILITY", 0.16
    elif return_180 >= 0.004 and crossings <= 2:
        name, uncertainty = "TREND", 0.06
    elif crossings >= 5:
        name, uncertainty = "RANGE_REVERSAL", 0.12
    else:
        name, uncertainty = "NORMAL", 0.08
    return {"name": name, "uncertainty": uncertainty, "one_minute_sigma": one_minute_sigma, "distance_sigma": distance_sigma}


def _model_probability(structural: Mapping[str, Any], features: Mapping[str, float], qualities: Mapping[str, float], weights: Mapping[str, float], regime: Mapping[str, Any], data_quality: float) -> tuple[float, dict[str, float]]:
    base = _clamp(float(structural["yes_probability"]), 0.01, 0.99)
    evidence_cap = _env_float("Q15_V95_EVIDENCE_LOGIT_CAP", 1.25, 0.25, 3.0)
    contributions: dict[str, float] = {"structural_logit": _logit(base)}
    evidence = float(weights.get("intercept", 0.0))
    contributions["intercept"] = evidence
    for name, value in features.items():
        if name == "intercept":
            continue
        quality = _clamp(float(qualities.get(name, 0.0)), 0.0, 1.0)
        contribution = float(weights.get(name, 0.0)) * float(value) * quality
        contributions[name] = contribution
        evidence += contribution
    evidence = _clamp(evidence, -evidence_cap, evidence_cap)
    raw = _sigmoid(_logit(base) + evidence)
    temperature = 1.0 + (1.0 - data_quality) * 0.75 + float(regime.get("uncertainty", 0.08))
    adjusted = _sigmoid(_logit(raw) / temperature)
    quality_cap = 0.80 + 0.18 * data_quality
    adjusted = _clamp(adjusted, 1.0 - quality_cap, quality_cap)
    contributions["evidence_total"] = evidence
    contributions["temperature"] = temperature
    return adjusted, contributions


def _kalshi_depth(snapshot: Mapping[str, Any], side: str) -> float | None:
    aliases = ("yes_ask_size", "yes_offer_size", "yes_depth_at_ask") if side == "YES" else ("no_ask_size", "no_offer_size", "no_depth_at_ask")
    direct = _first_num(snapshot, aliases)
    if direct is not None:
        return max(0.0, direct)
    orderbook = snapshot.get("orderbook") or snapshot.get("kalshi_orderbook")
    if isinstance(orderbook, Mapping):
        key = "yes_asks" if side == "YES" else "no_asks"
        rows = orderbook.get(key)
        if isinstance(rows, Sequence) and rows:
            first = rows[0]
            if isinstance(first, Sequence) and len(first) >= 2:
                return max(0.0, _num(first[1], 0.0) or 0.0)
    return None


def _market_implied_yes(snapshot: Mapping[str, Any]) -> float | None:
    """Market-implied P(YES) from the Kalshi quote (a YES price in cents is the
    market's probability estimate). Uses the YES mid, falling back to the NO mid."""
    def _mid(bid_key: str, ask_key: str) -> float | None:
        cents = [c for c in (_num(snapshot.get(bid_key)), _num(snapshot.get(ask_key)))
                 if c is not None and 0.0 <= c <= 100.0]
        return (sum(cents) / len(cents)) if cents else None

    yes_mid = _mid("yes_bid", "yes_ask")
    if yes_mid is not None:
        return _clamp(yes_mid / 100.0, 0.01, 0.99)
    no_mid = _mid("no_bid", "no_ask")
    if no_mid is not None:
        return _clamp(1.0 - no_mid / 100.0, 0.01, 0.99)
    return None


def _market_anchored_probability(model_yes: float, market_yes: float | None,
                                 data_quality: float, evidence_quality: float,
                                 strength: float) -> tuple[float, dict[str, Any]]:
    """Shrink the model probability toward the market-implied probability.

    At these horizons the Kalshi market is an efficient predictor, so an
    independent model should only deviate from it in proportion to its own
    confidence: model_trust = data_quality x evidence_quality x strength. With
    no quote (or strength 0) the model is used unchanged."""
    model_yes = _clamp(model_yes, 0.01, 0.99)
    if market_yes is None or strength <= 0.0:
        return model_yes, {"applied": False,
                           "reason": "no_market_quote" if market_yes is None else "disabled"}
    market_yes = _clamp(market_yes, 0.01, 0.99)
    trust = _clamp(data_quality * evidence_quality * strength, 0.0, 1.0)
    anchored = _clamp(_sigmoid(_logit(market_yes) + trust * (_logit(model_yes) - _logit(market_yes))), 0.01, 0.99)
    return anchored, {
        "applied": True, "market_yes": round(market_yes, 4), "model_yes": round(model_yes, 4),
        "model_trust": round(trust, 4), "anchored_yes": round(anchored, 4), "strength": strength,
    }


def analyse_v95(
    snapshot: Mapping[str, Any],
    canonical: CanonicalSnapshot,
    ledger: V95Ledger | None,
) -> dict[str, Any]:
    base_result: dict[str, Any] = {
        "version": VERSION, "model_version": MODEL_VERSION,
        "feature_schema_version": FEATURE_SCHEMA_VERSION, "read_only": True,
        "asset": canonical.asset, "ticker": canonical.ticker, "checkpoint": canonical.checkpoint,
        "canonical": canonical.public_dict(), "prediction_available": False,
        "entry_allowed": False, "automatic_production_learning": False,
        "automatic_threshold_changes": False,
    }
    if not canonical.core_valid:
        return {
            **base_result, "trade_decision": "AVOID_INVALID_DATA", "main_blocker": ",".join(canonical.core_errors),
            "yes_probability": None, "no_probability": None, "selected_probability": None,
            "conservative_probability": None, "data_quality": canonical.data_quality,
            "evidence_quality": 0.0, "trade_quality": 0.0,
        }
    volatility = _robust_volatility(canonical)
    returns = _multi_horizon_returns(canonical)
    structural = _structural_probability(canonical, volatility, returns)
    momentum, momentum_q, momentum_d = _momentum_feature(returns, volatility, canonical)
    flow, flow_q, flow_d = _flow_feature(snapshot, canonical)
    book, book_q, book_d = _book_feature(snapshot, canonical)
    wick_raw, wick_d = _wick_score(canonical.candles, canonical.yes_is_higher)
    wick = float(wick_raw or 0.0)
    wick_q = 0.0 if wick_raw is None else _clamp(len(canonical.candles) / 12.0, 0.0, 1.0)
    context, context_q, context_d = _context_feature(canonical)
    threshold, threshold_q, threshold_d = _threshold_interaction(canonical)
    exchange, exchange_q, exchange_d = _exchange_consensus(canonical, returns)
    derivatives, derivatives_q, derivatives_d = _derivatives_feature(canonical, momentum)
    absorption, absorption_q, absorption_d = _absorption_feature(flow, flow_q, momentum, momentum_q)
    feature_values = {
        "momentum": momentum, "flow": flow, "book": book, "wick": wick,
        "context": context, "threshold_interaction": threshold,
        "exchange_consensus": exchange, "derivatives": derivatives,
        "absorption": absorption,
    }
    feature_quality = {
        "momentum": momentum_q, "flow": flow_q, "book": book_q, "wick": wick_q,
        "context": context_q, "threshold_interaction": threshold_q,
        "exchange_consensus": exchange_q, "derivatives": derivatives_q,
        "absorption": absorption_q,
    }
    evidence_quality = _clamp(
        0.25 * momentum_q + 0.16 * flow_q + 0.12 * book_q + 0.08 * wick_q +
        0.12 * context_q + 0.15 * threshold_q + 0.08 * exchange_q + 0.04 * derivatives_q,
        0.0, 1.0,
    )
    data_quality = _clamp(0.70 * canonical.data_quality + 0.30 * evidence_quality, 0.0, 1.0)
    regime = _regime(canonical, volatility, returns, threshold_d, exchange_d)
    raw_yes, contributions = _model_probability(structural, feature_values, feature_quality, CHAMPION_WEIGHTS, regime, data_quality)
    calibration = ledger.calibrate(raw_yes, canonical.checkpoint, canonical.asset) if ledger else {"probability": raw_yes, "active": False, "reason": "ledger_unavailable"}
    shadow_calibrated_yes = _clamp(float(calibration["probability"]), 0.01, 0.99)
    production_calibration_enabled = _env_bool("Q15_V95_PRODUCTION_CALIBRATION_ENABLED", False)
    model_yes = shadow_calibrated_yes if production_calibration_enabled and calibration.get("active") else raw_yes
    # Market-price anchoring: defer to the (efficient) Kalshi market unless the
    # model has earned the confidence to deviate. This is the bot's working prob.
    market_implied_yes = _market_implied_yes(snapshot)
    anchor_strength = _env_float("Q15_V95_MARKET_ANCHOR_STRENGTH", 1.0, 0.0, 1.0)
    calibrated_yes, market_anchor = _market_anchored_probability(
        model_yes, market_implied_yes, data_quality, evidence_quality, anchor_strength
    )
    challenger_weights = ledger.challenger_weights(canonical.checkpoint, regime.get("name")) if ledger else CHAMPION_WEIGHTS
    challenger_yes, challenger_contributions = _model_probability(structural, feature_values, feature_quality, challenger_weights, regime, data_quality)
    provisional_side = "YES" if calibrated_yes >= 0.5 else "NO"
    pattern = ledger.pattern_similarity(feature_values, provisional_side, canonical.checkpoint) if ledger else {"active": False, "shadow_adjustment": 0.0}
    shadow_pattern_adjustment = float(pattern.get("shadow_adjustment") or 0.0)
    challenger_yes = _clamp(challenger_yes + (shadow_pattern_adjustment if provisional_side == "YES" else -shadow_pattern_adjustment), 0.01, 0.99)
    # Anchor the challenger identically so champion-vs-challenger compares weights, not anchoring.
    challenger_yes, _ = _market_anchored_probability(challenger_yes, market_implied_yes, data_quality, evidence_quality, anchor_strength)
    side = provisional_side
    selected = calibrated_yes if side == "YES" else 1.0 - calibrated_yes
    uncertainty = 0.018 + (1.0 - data_quality) * 0.12 + float(regime.get("uncertainty", 0.08)) * 0.25
    divergence = _num(exchange_d.get("divergence_bps"), 0.0) or 0.0
    uncertainty += min(0.04, divergence / 1000.0)
    conservative = _clamp(selected - uncertainty, 0.01, 0.99)

    quote = _selected_quote(snapshot, side)
    costs = _estimated_costs(snapshot, quote)
    ask = _num(quote.get("ask_cents"))
    spread = _num(quote.get("spread_cents"))
    depth = _kalshi_depth(snapshot, side)
    quote_ts = canonical.feed_timestamps.get("quote")
    quote_age = None if quote_ts is None else max(0.0, canonical.observed_at - quote_ts)
    # Per-checkpoint gates. 7M defaults mirror 10M so adding the 7-minute tracker
    # does not change live entry behavior; both stay overridable via env.
    _checkpoint = canonical.checkpoint if canonical.checkpoint in ("10M", "15M", "7M") else "10M"
    _required_edge_default = {"10M": 6.0, "7M": 6.0, "15M": 4.0}.get(_checkpoint, 4.0)
    _min_prob_default = {"10M": 0.60, "7M": 0.60, "15M": 0.58}.get(_checkpoint, 0.58)
    required_edge = _env_float(f"Q15_V95_{_checkpoint}_REQUIRED_EDGE_CENTS", _required_edge_default, 0.0, 25.0)
    minimum_probability = _env_float(f"Q15_V95_{_checkpoint}_MIN_PROBABILITY", _min_prob_default, 0.50, 0.90)
    minimum_quality = _env_float("Q15_V95_MIN_DATA_QUALITY", 0.55, 0.20, 0.95)
    max_spread = _env_float("Q15_V95_MAX_SPREAD_CENTS", 12.0, 1.0, 50.0)
    min_depth = _env_float("Q15_V95_MIN_DEPTH_AT_ASK", 3.0, 0.0, 10000.0)
    min_seconds = _env_float("Q15_V95_MIN_SECONDS_REMAINING", 20.0, 0.0, 300.0)
    total_costs = float(costs.get("total_cents") if "total_cents" in costs else costs.get("total_cost_cents") or 0.0)
    net_edge = None if ask is None else conservative * 100.0 - ask - total_costs
    ideal_entry = _clamp(conservative * 100.0 - total_costs - required_edge, 0.0, 100.0)
    liquidity_quality = 1.0
    if spread is not None:
        liquidity_quality *= _clamp(1.0 - spread / max(max_spread * 1.5, 1.0), 0.0, 1.0)
    if depth is not None and min_depth > 0:
        liquidity_quality *= _clamp(depth / min_depth, 0.0, 1.0)
    if quote_age is not None:
        liquidity_quality *= _clamp(math.exp(-max(0.0, quote_age - 5.0) / 20.0), 0.0, 1.0)
    trade_quality = _clamp(0.40 * selected + 0.25 * data_quality + 0.20 * liquidity_quality + 0.15 * _clamp(((net_edge if net_edge is not None else -10.0) + 10.0) / 20.0, 0.0, 1.0), 0.0, 1.0)

    decision = "ENTRY_RECOMMENDED"
    blocker = None
    if ask is None:
        decision, blocker = "PREDICTION_ONLY", "executable_ask_unavailable"
    elif canonical.seconds_remaining is not None and canonical.seconds_remaining < min_seconds:
        decision, blocker = "WATCH_TIME", "too_little_time_remaining"
    elif quote_age is not None and quote_age > 30.0:
        decision, blocker = "WATCH_LIQUIDITY", "stale_kalshi_quote"
    elif spread is not None and spread > max_spread:
        decision, blocker = "WATCH_LIQUIDITY", "spread_too_wide"
    elif depth is not None and depth < min_depth:
        decision, blocker = "WATCH_LIQUIDITY", "insufficient_depth_at_ask"
    elif data_quality < minimum_quality:
        decision, blocker = "WATCH_DATA_QUALITY", "data_quality_below_threshold"
    elif conservative < minimum_probability:
        decision, blocker = "WATCH_CONFIDENCE", "conservative_probability_below_threshold"
    elif net_edge is None or net_edge < required_edge:
        decision, blocker = "WATCH_PRICE", "price_not_attractive_after_costs"
    entry_allowed = decision == "ENTRY_RECOMMENDED"
    grade = "A" if selected >= 0.70 and data_quality >= 0.75 else "B" if selected >= 0.62 and data_quality >= 0.60 else "C" if selected >= 0.55 else "D"

    factor_rows = []
    for name, contribution in contributions.items():
        if name in {"structural_logit", "intercept", "evidence_total", "temperature"}:
            continue
        factor_rows.append({"name": name, "contribution_logit": contribution, "feature": feature_values.get(name), "quality": feature_quality.get(name)})
    supporting = sorted([row for row in factor_rows if row["contribution_logit"] > 0], key=lambda row: row["contribution_logit"], reverse=True)[:3]
    opposing = sorted([row for row in factor_rows if row["contribution_logit"] < 0], key=lambda row: row["contribution_logit"])[:3]
    return {
        **base_result,
        "prediction_available": True,
        "prediction_side": side,
        "yes_probability": calibrated_yes,
        "no_probability": 1.0 - calibrated_yes,
        "raw_yes_probability": raw_yes,
        "model_yes_probability": model_yes,
        "market_implied_yes_probability": market_implied_yes,
        "market_anchor": market_anchor,
        "baseline_yes_probability": float(structural["yes_probability"]),
        "challenger_yes_probability": challenger_yes,
        "selected_probability": selected,
        "conservative_probability": conservative,
        "confidence_grade": grade,
        "data_quality": data_quality,
        "evidence_quality": evidence_quality,
        "trade_quality": trade_quality,
        "uncertainty_penalty": uncertainty,
        "regime": regime,
        "volatility": volatility,
        "returns": returns,
        "structural": structural,
        "feature_values": feature_values,
        "feature_quality": feature_quality,
        "feature_details": {
            "momentum": momentum_d, "flow": flow_d, "book": book_d, "wick": wick_d,
            "context": context_d, "threshold_interaction": threshold_d,
            "exchange_consensus": exchange_d, "derivatives": derivatives_d,
            "absorption": absorption_d,
        },
        "contributions": contributions,
        "challenger_contributions": challenger_contributions,
        "supporting_factors": supporting,
        "opposing_factors": opposing,
        "calibration": {**calibration, "production_enabled": production_calibration_enabled},
        "shadow_calibrated_yes_probability": shadow_calibrated_yes,
        "pattern_similarity": pattern,
        "quote": {**quote, "ask_depth": depth, "quote_age_seconds": quote_age},
        "costs": costs,
        "net_edge_cents": net_edge,
        "required_edge_cents": required_edge,
        "ideal_entry_cents": ideal_entry,
        "minimum_probability": minimum_probability,
        "minimum_data_quality": minimum_quality,
        "liquidity_quality": liquidity_quality,
        "trade_decision": decision,
        "main_blocker": blocker,
        "entry_allowed": entry_allowed,
        "production_weights_frozen": True,
        "shadow_challenger_active": bool(ledger and ledger.learning_enabled(canonical.checkpoint)),
        "shadow_challenger_checkpoint": canonical.checkpoint,
    }


def apply_v95_policy(snapshot: MutableMapping[str, Any], analysis: Mapping[str, Any]) -> MutableMapping[str, Any]:
    snapshot["q15_v9_5_version"] = VERSION
    snapshot["q15_v9_5_model_version"] = MODEL_VERSION
    snapshot["q15_v9_5_feature_schema_version"] = FEATURE_SCHEMA_VERSION
    snapshot["q15_v9_5_prediction_available"] = bool(analysis.get("prediction_available"))
    snapshot["q15_v9_5_selected_side"] = analysis.get("prediction_side")
    snapshot["q15_v9_5_yes_probability"] = analysis.get("yes_probability")
    snapshot["q15_v9_5_no_probability"] = analysis.get("no_probability")
    snapshot["q15_v9_5_conservative_probability"] = analysis.get("conservative_probability")
    snapshot["q15_v9_5_data_quality"] = analysis.get("data_quality")
    snapshot["q15_v9_5_evidence_quality"] = analysis.get("evidence_quality")
    snapshot["q15_v9_5_trade_quality"] = analysis.get("trade_quality")
    snapshot["q15_v9_5_trade_decision"] = analysis.get("trade_decision")
    snapshot["q15_v9_5_net_edge_cents"] = analysis.get("net_edge_cents")
    snapshot["q15_v9_5_ideal_entry_cents"] = analysis.get("ideal_entry_cents")
    snapshot["q15_v9_5_regime"] = (analysis.get("regime") or {}).get("name")
    snapshot["q15_v9_5_entry_allowed"] = bool(analysis.get("entry_allowed"))
    # V9.5 is authoritative for the returned snapshot. It never places orders.
    snapshot["selected_side"] = analysis.get("prediction_side")
    snapshot["decision_state"] = analysis.get("trade_decision")
    snapshot["entry_allowed"] = bool(analysis.get("entry_allowed"))
    snapshot["new_entry_allowed"] = bool(analysis.get("entry_allowed"))
    return snapshot


def rank_analyses(analyses: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    priority = {
        "ENTRY_RECOMMENDED": 8, "WATCH_PRICE": 7, "WATCH_CONFIDENCE": 6,
        "WATCH_DATA_QUALITY": 5, "WATCH_LIQUIDITY": 4, "WATCH_TIME": 3,
        "PREDICTION_ONLY": 2, "AVOID_INVALID_DATA": 0,
    }
    rows = []
    for asset, analysis in analyses.items():
        score = (
            priority.get(str(analysis.get("trade_decision")), 1),
            float(analysis.get("net_edge_cents") if analysis.get("net_edge_cents") is not None else -999.0),
            float(analysis.get("conservative_probability") or 0.0),
            float(analysis.get("data_quality") or 0.0),
        )
        rows.append((score, asset, analysis))
    rows.sort(key=lambda row: row[0], reverse=True)
    return [
        {
            "rank": index, "asset": asset, "prediction_side": analysis.get("prediction_side"),
            "selected_probability": analysis.get("selected_probability"),
            "conservative_probability": analysis.get("conservative_probability"),
            "data_quality": analysis.get("data_quality"), "trade_quality": analysis.get("trade_quality"),
            "trade_decision": analysis.get("trade_decision"), "net_edge_cents": analysis.get("net_edge_cents"),
            "ideal_entry_cents": analysis.get("ideal_entry_cents"), "regime": (analysis.get("regime") or {}).get("name"),
            "ticker": analysis.get("ticker"),
        }
        for index, (_, asset, analysis) in enumerate(rows, 1)
    ]


def _fmt_probability(value: Any) -> str:
    parsed = _num(value)
    return "n/a" if parsed is None else f"{parsed * 100:.1f}%"


def _fmt_cents(value: Any, signed: bool = False) -> str:
    parsed = _num(value)
    if parsed is None:
        return "n/a"
    return f"{parsed:+.2f}¢" if signed else f"{parsed:.2f}¢"


# Plain-language labels for the canonical core-data failures, so a degraded
# cycle explains itself instead of rendering a wall of "n/a".
_V95_REASON_LABELS = {
    "missing_or_invalid_spot": "underlying spot price unavailable",
    "missing_or_invalid_threshold": "strike/threshold not set yet",
    "missing_time_remaining": "time-to-close unavailable",
    "contract_closed": "market already closed",
    "stale_core_snapshot": "core market data is stale",
    "stale_candle_cache": "candle history is stale",
    "executable_ask_unavailable": "no executable ask quote",
    "ledger_unavailable": "calibration ledger unavailable",
}


def _humanize_v95_reasons(blocker: Any) -> str:
    tokens = [token.strip() for token in str(blocker or "").split(",") if token.strip()]
    if not tokens:
        return "core market data was incomplete this cycle"
    seen: list[str] = []
    for token in tokens:
        label = _V95_REASON_LABELS.get(token, token.replace("_", " ").strip())
        if label and label not in seen:
            seen.append(label)
    return "; ".join(seen)


_DECISION_LABELS = {
    "ENTRY_RECOMMENDED": "ENTRY",
    "WATCH_PRICE": "price too high",
    "WATCH_CONFIDENCE": "low confidence",
    "WATCH_DATA_QUALITY": "weak data",
    "WATCH_LIQUIDITY": "thin/wide book",
    "WATCH_TIME": "too little time",
    "PREDICTION_ONLY": "no executable quote",
    "AVOID_INVALID_DATA": "no data",
}


def _decision_label(decision: Any) -> str:
    token = str(decision or "").upper()
    return _DECISION_LABELS.get(token, token.replace("_", " ").lower() or "watch")


def _c(value: Any, signed: bool = False) -> str:
    """Compact whole-cent formatter for the per-cycle checkpoint message."""
    parsed = _num(value)
    if parsed is None:
        return "—"
    return f"{parsed:+.0f}¢" if signed else f"{parsed:.0f}¢"


def build_v95_message(checkpoint: str, analyses: Mapping[str, Mapping[str, Any]], ranking: Sequence[Mapping[str, Any]], ledger_status: Mapping[str, Any], result_events: Sequence[Mapping[str, Any]] | None = None) -> str:
    any_entry = any(bool(row.get("entry_allowed")) for row in analyses.values())
    emoji = "✅" if any_entry else "👀"
    state = "ENTRY RECOMMENDED" if any_entry else "NO ENTRY YET"
    medals = ["🥇", "🥈", "🥉"]
    # Header keeps "V9.5 CHECK" (formatter guard) and the ENTRY/NO ENTRY markers
    # (alert-suppression classification).
    lines = [f"{emoji} <b>{checkpoint} V9.5 CHECK · {state}</b>"]
    for index, row in enumerate(ranking[:3]):
        asset = str(row["asset"])
        analysis = analyses[asset]
        medal = medals[index] if index < 3 else f"{index + 1}."
        side = analysis.get("prediction_side") or "—"
        if not analysis.get("prediction_available"):
            lines.append(f"{medal} <b>{asset}</b> — no prediction")
            lines.append(f"   ⛔ {_humanize_v95_reasons(analysis.get('main_blocker'))}")
            continue
        regime = (analysis.get("regime") or {}).get("name") or "—"
        prob = _fmt_probability(analysis.get("selected_probability"))
        # Market-implied prob for the selected side (invert the YES-implied for a
        # NO pick) so the model-vs-market gap shows at a glance; omit if no quote.
        market_yes = _num(analysis.get("market_implied_yes_probability"))
        market_for_side = None if market_yes is None else (market_yes if side == "YES" else 1.0 - market_yes)
        mkt = "" if market_for_side is None else f" vs mkt {_fmt_probability(market_for_side)}"
        grade = analysis.get("confidence_grade") or "—"
        ask = _c((analysis.get("quote") or {}).get("ask_cents"))
        net = analysis.get("net_edge_cents")
        lines.append(f"{medal} <b>{asset} {side}</b> — {prob}{mkt} · grade {grade} · {regime}")
        if analysis.get("entry_allowed"):
            lines.append(f"   ✅ ENTRY · edge {_c(net, signed=True)} · ask {ask} → max {_c(analysis.get('ideal_entry_cents'))}")
        else:
            reason = _decision_label(analysis.get("trade_decision"))
            edge = f"edge {_c(net, signed=True)} (need {_c(analysis.get('required_edge_cents'))})" if net is not None else "no executable edge"
            lines.append(f"   👀 {reason} · {edge} · ask {ask}")
    if result_events:
        marks = "  ".join(f"{e.get('asset')} {'✅' if e.get('correct') else '❌'}" for e in result_events[:4])
        lines.append(f"Recent results — {marks}")
    lines.append("<i>Paper monitor · not advice · no orders placed</i>")
    text = "\n".join(lines)
    return text if len(text) <= 4000 else text[:3990] + "…"


def _notification_identity(checkpoint: str, analyses: Mapping[str, Mapping[str, Any]], ranking: Sequence[Mapping[str, Any]], now: float) -> tuple[str, str, str]:
    top = ranking[0] if ranking else {}
    ticker = str(top.get("ticker") or "UNKNOWN")
    # Kalshi ticker is the stable contract identity.  Do not derive a key from
    # a moving `now + seconds_remaining` estimate; that can cross a rounding
    # boundary and create duplicate Telegram sends one second apart.
    if ticker and ticker != "UNKNOWN":
        event_key = f"{VERSION}|{checkpoint}|{ticker}"
    else:
        window = 900 if checkpoint == "15M" else 600
        event_key = f"{VERSION}|{checkpoint}|UNKNOWN|{int(now // window)}"
    has_entry = any(bool(analysis.get("entry_allowed")) for analysis in analyses.values())
    state = "ENTRY_RECOMMENDED" if has_entry else "WATCH"
    fingerprint = hashlib.sha256(json.dumps({
        "checkpoint": checkpoint,
        "state": state,
        "top": [(row.get("asset"), row.get("prediction_side"), row.get("trade_decision")) for row in ranking[:3]],
    }, sort_keys=True).encode()).hexdigest()[:20]
    return event_key, state, fingerprint


def _bridge_parent_inputs(policy: Any, snapshots: Mapping[str, Mapping[str, Any]], now: float) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Inject one canonical candle/flow/book view before the legacy parent runs.

    V9.5 remains authoritative, but its V9.4 parent still executes to maintain
    compatibility with the surrounding application.  Some live projects feed
    the parent only a compact snapshot while the full candle history lives in
    the persistent V9.4 cache.  This bridge gives the parent the same candles
    V9.5 will later use, preventing false `0/3 candles` and unavailable momentum
    diagnostics if a parent formatter is observed during startup.
    """
    bridged: dict[str, dict[str, Any]] = {}
    stats = {
        "assets": 0, "assets_with_candles": 0, "candles_injected": 0,
        "momentum_injected": 0, "public_flow_injected": 0,
        "public_book_injected": 0, "errors": [],
    }
    for asset_key, raw in snapshots.items():
        if not isinstance(raw, Mapping):
            continue
        row = copy.deepcopy(dict(raw))
        asset = _asset_name(asset_key, row)
        stats["assets"] += 1
        try:
            cached = policy._candles(asset) if hasattr(policy, "_candles") else []
            candles = _canonical_candles(row, cached)
            if candles:
                # Flat float candle dicts: dict() copies are equivalent to
                # deepcopy but cheaper.
                canonical_rows = [dict(c) for c in candles]
                row["underlying_candles_5s"] = canonical_rows
                row["spot_candles_5s"] = canonical_rows
                row["candles_5s"] = canonical_rows
                row["candles"] = canonical_rows
                row["q15_v9_5_bridge_candle_count"] = len(canonical_rows)
                stats["assets_with_candles"] += 1
                stats["candles_injected"] += len(canonical_rows)
                r30 = _window_return(canonical_rows, 30.0)
                r60 = _window_return(canonical_rows, 60.0)
                r180 = _window_return(canonical_rows, 180.0)
                if r30 is not None or r60 is not None or r180 is not None:
                    if r30 is not None:
                        row["momentum_30s"] = r30
                        row["spot_momentum_30s"] = r30
                    if r60 is not None:
                        row["spot_momentum"] = r60
                        row["momentum_60s"] = r60
                    if r180 is not None:
                        row["momentum_180s"] = r180
                        row["spot_momentum_180s"] = r180
                    row["q15_v9_5_bridge_momentum_available"] = True
                    stats["momentum_injected"] += 1

            public = policy.market_data.snapshot(asset, now) if hasattr(policy, "market_data") else {}
            if isinstance(public, Mapping):
                flow, _, _ = _combine_public_signal(public, "flow")
                if flow is not None and _flow_score(row)[0] is None:
                    row["taker_buy_volume"] = 100.0 * (1.0 + _clamp(flow, -1.0, 1.0)) / 2.0
                    row["taker_sell_volume"] = 100.0 - row["taker_buy_volume"]
                    row["q15_v9_5_bridge_flow_source"] = "public_composite"
                    stats["public_flow_injected"] += 1
                book, _, _ = _combine_public_signal(public, "book")
                if book is not None and _book_score(row)[0] is None:
                    row["orderbook_imbalance"] = _clamp(book, -1.0, 1.0)
                    row["q15_v9_5_bridge_book_source"] = "public_composite"
                    stats["public_book_injected"] += 1
        except Exception as exc:
            stats["errors"].append(f"{asset}:{type(exc).__name__}:{exc}")
        bridged[str(asset_key)] = row
    return bridged, stats


class CheckpointPolicyV95(CheckpointPolicyV94Unified):
    VERSION = VERSION

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.v95_enabled = _env_bool("Q15_V95_FORCE_ACTIVE", True) and not _env_bool("Q15_V95_EMERGENCY_DISABLE", False)
        self.signal_store = args[0] if args else kwargs.get("store")
        self.market_data = PublicMarketDataHub()
        self.ledger = V95Ledger()
        self._v95_lock = threading.RLock()
        self._latest_v95: dict[str, dict[str, Any]] = {}
        self._latest_ranking_v95: list[dict[str, Any]] = []
        self._latest_public: dict[str, dict[str, Any]] = {}
        self._last_checkpoint_v95 = "UNKNOWN"
        self._last_reconcile: dict[str, Any] = {}
        self._last_market_reconcile: dict[str, Any] = {}
        self._run_cycle_timing: dict[str, Any] = {}
        self._slowest_run_cycle: dict[str, Any] | None = None
        self._last_reconcile_at = 0.0
        # Optional Kalshi client (set by the app) so predictions can be settled
        # directly from official results, not only via the signals table.
        self.kalshi_client = None
        self._cycles = 0
        self._errors = 0
        self._last_error: str | None = None
        self._telegram_sent_v95 = 0
        self._telegram_failed_v95 = 0
        self._telegram_suppressed_v95 = 0
        self._bridge_status: dict[str, Any] = {}
        self._runtime_binding = "CheckpointPolicyV95"

    def run_cycle(self, snapshots: dict[str, dict], now: float, ws_health: Mapping[str, Any] | None,
                  focus_manager: Any, calibrated_edge: Any, notifier: Any) -> dict[str, dict]:
        _rc_start = time.monotonic()
        _t: dict[str, float] = {}
        deferred = _BufferedNotifier(notifier)
        assets = [_asset_name(key, value) for key, value in snapshots.items() if isinstance(value, Mapping)]
        self.market_data.schedule(assets, now)
        bridged_snapshots, bridge_status = _bridge_parent_inputs(self, snapshots, now)
        self._bridge_status = copy.deepcopy(bridge_status)
        _t0 = time.monotonic()
        parent_output = super().run_cycle(bridged_snapshots, now, ws_health, focus_manager, calibrated_edge, deferred)
        _t["parent_chain"] = round(time.monotonic() - _t0, 3)
        if not self.v95_enabled:
            deferred.flush(None)
            return parent_output
        try:
            checkpoint = _detect_checkpoint(
                {str(key): value for key, value in parent_output.items() if isinstance(value, Mapping)},
                deferred.messages(),
            )
            _t0 = time.monotonic()
            analyses: dict[str, dict[str, Any]] = {}
            output: dict[str, dict] = {}
            public_map: dict[str, dict[str, Any]] = {}
            canonicals: dict[str, Any] = {}
            # Coarse sub-timers (accumulated across assets) so a v95_analysis
            # spike can be attributed to deepcopy vs canonical-build vs
            # model-eval vs ledger-write without guessing.
            _sub = {"deepcopy": 0.0, "build": 0.0, "analyse": 0.0}
            for asset_key, raw in parent_output.items():
                if not isinstance(raw, Mapping):
                    continue
                _s = time.monotonic()
                snapshot = copy.deepcopy(dict(raw))
                asset = _asset_name(asset_key, snapshot)
                cached = self._candles(asset) if hasattr(self, "_candles") else []
                context: Mapping[str, Any] = {}
                if hasattr(self, "_latest_context"):
                    lock = getattr(self, "_context_lock", threading.RLock())
                    with lock:
                        candidate = copy.deepcopy(getattr(self, "_latest_context", {}).get(asset))
                    if isinstance(candidate, Mapping):
                        context = candidate
                public = self.market_data.snapshot(asset, now)
                public_map[asset] = copy.deepcopy(public)
                _sub["deepcopy"] += time.monotonic() - _s
                _s = time.monotonic()
                canonical = build_canonical_snapshot(
                    snapshot, asset=asset, checkpoint=checkpoint, now=now,
                    cached_candles=cached, context=context, public=public,
                )
                _sub["build"] += time.monotonic() - _s
                _s = time.monotonic()
                analysis = analyse_v95(snapshot, canonical, self.ledger)
                apply_v95_policy(snapshot, analysis)
                _sub["analyse"] += time.monotonic() - _s
                analyses[asset] = copy.deepcopy(analysis)
                output[asset_key] = snapshot
                canonicals[asset] = canonical
            ranking = rank_analyses(analyses)
            ranks = {str(row["asset"]): int(row["rank"]) for row in ranking}
            _s_record = time.monotonic()
            # Record each prediction AFTER ranking so its pick rank (#1/#2/#3) is
            # persisted with it, enabling per-rank accuracy tracking.
            for key, snapshot in output.items():
                asset = _asset_name(key, snapshot)
                rank = ranks.get(asset)
                snapshot["q15_v9_5_rank"] = rank
                snapshot["q15_v9_5_top_pick"] = rank == 1
                if asset not in analyses:
                    continue
                analysis = analyses[asset]
                analysis["rank"] = rank
                analysis["top_pick"] = rank == 1
                canonical = canonicals.get(asset)
                if canonical is not None and analysis.get("prediction_available") and canonical.ticker:
                    prediction_id, inserted = self.ledger.record_prediction(
                        ticker=canonical.ticker, asset=asset, checkpoint=checkpoint,
                        created_at=now, close_time=canonical.settlement_time,
                        predicted_side=str(analysis["prediction_side"]),
                        raw_yes_probability=float(analysis["raw_yes_probability"]),
                        calibrated_yes_probability=float(analysis["yes_probability"]),
                        challenger_yes_probability=float(analysis["challenger_yes_probability"]),
                        baseline_yes_probability=float(analysis["baseline_yes_probability"]),
                        selected_probability=float(analysis["selected_probability"]),
                        conservative_probability=float(analysis["conservative_probability"]),
                        data_quality=float(analysis["data_quality"]),
                        evidence_quality=float(analysis["evidence_quality"]),
                        trade_quality=float(analysis["trade_quality"]),
                        trade_decision=str(analysis["trade_decision"]),
                        regime=str((analysis.get("regime") or {}).get("name") or "UNKNOWN"),
                        features=analysis["feature_values"], contributions=analysis["contributions"],
                        quote=analysis["quote"], rank=rank, costs=analysis.get("costs"),
                    )
                    analysis["prediction_id"] = prediction_id
                    analysis["new_unique_prediction_recorded"] = inserted

            _sub["record"] = time.monotonic() - _s_record
            _t["v95_analysis"] = round(time.monotonic() - _t0, 3)
            _t["v95_sub"] = {k: round(v, 3) for k, v in _sub.items()}
            result_events: list[Mapping[str, Any]] = []
            if now - self._last_reconcile_at >= 30.0:
                self._last_reconcile_at = now
                if self.signal_store is not None:
                    _t0 = time.monotonic()
                    self._last_reconcile = self.ledger.reconcile_from_signal_store(self.signal_store)
                    _t["signal_store_reconcile"] = round(time.monotonic() - _t0, 3)
                    result_events = list(self._last_reconcile.get("result_events") or [])
                # Settle any remaining closed markets directly from Kalshi, so
                # predictions without a signals row still get graded.
                get_market = getattr(self.kalshi_client, "get_market", None)
                if callable(get_market):
                    _t0 = time.monotonic()
                    self._last_market_reconcile = self.ledger.reconcile_pending_from_market(get_market, now)
                    _t["market_reconcile"] = round(time.monotonic() - _t0, 3)
                    result_events = list(self._last_market_reconcile.get("result_events") or []) + result_events
            ledger_status = self.ledger.status()
            message = build_v95_message(checkpoint, analyses, ranking, ledger_status, result_events) if ranking else None
            # Discard all parent V9.4 messages. V9.5 owns the final state machine.
            deferred.suppress_all(generated_message=message is not None)
            sent = failed = 0
            if message:
                event_key, desired_state, fingerprint = _notification_identity(checkpoint, analyses, ranking, now)
                previous = self.ledger.notification_state(event_key)
                state = "ENTRY_WITHDRAWN" if previous == "ENTRY_RECOMMENDED" and desired_state != "ENTRY_RECOMMENDED" else desired_state
                permit_key = self.ledger.reserve_notification(
                    event_key=event_key, checkpoint=checkpoint, state=state,
                    fingerprint=fingerprint, now=now,
                )
                if permit_key:
                    fresh = _BufferedNotifier(notifier)
                    sent, failed, _ = fresh.flush(message)
                    self.ledger.complete_notification(event_key=permit_key, success=sent > 0 and failed == 0, now=now)
                else:
                    self._telegram_suppressed_v95 += 1
            self._telegram_sent_v95 += sent
            self._telegram_failed_v95 += failed
            self._cycles += 1
            self._last_error = None
            with self._v95_lock:
                self._latest_v95 = copy.deepcopy(analyses)
                self._latest_ranking_v95 = copy.deepcopy(ranking)
                self._latest_public = public_map
                self._last_checkpoint_v95 = checkpoint
            with _LATEST_LOCK:
                global _LATEST_CHECKPOINT
                _LATEST_ANALYSES.clear(); _LATEST_ANALYSES.update(copy.deepcopy(analyses))
                _LATEST_RANKING.clear(); _LATEST_RANKING.extend(copy.deepcopy(ranking))
                _LATEST_LEDGER.clear(); _LATEST_LEDGER.update(copy.deepcopy(ledger_status))
                _LATEST_CHECKPOINT = checkpoint
            _t["total"] = round(time.monotonic() - _rc_start, 3)
            _t["other"] = round(max(0.0, _t["total"] - sum(
                v for k, v in _t.items() if k != "total" and isinstance(v, (int, float))
            )), 3)
            self._run_cycle_timing = _t
            # Latch the worst cycle's FULL breakdown atomically (run-cycle buckets
            # + chain sub-stages + v95 sub-timers together) so a slow cycle can be
            # attributed exactly, instead of reading two out-of-sync timing dicts.
            try:
                threshold = float(os.environ.get("Q15_V95_SLOW_CYCLE_SECONDS", "10"))
            except (TypeError, ValueError):
                threshold = 10.0
            prev = (self._slowest_run_cycle or {}).get("run_cycle_timing", {}).get("total", 0.0)
            if _t["total"] >= threshold and _t["total"] >= float(prev or 0.0):
                self._slowest_run_cycle = {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "run_cycle_timing": copy.deepcopy(_t),
                    "parent_chain_timing": copy.deepcopy(getattr(self, "_chain_timing", {})),
                }
            return output
        except Exception as exc:
            self._errors += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            for snapshot in parent_output.values():
                if isinstance(snapshot, MutableMapping):
                    snapshot["q15_v9_5_error"] = self._last_error
                    snapshot["q15_v9_5_entry_allowed"] = False
                    snapshot["entry_allowed"] = False
                    snapshot["new_entry_allowed"] = False
            deferred.suppress_all(generated_message=False)
            return parent_output

    def predictions(self) -> dict[str, Any]:
        with self._v95_lock:
            analyses = copy.deepcopy(self._latest_v95)
            ranking = copy.deepcopy(self._latest_ranking_v95)
            checkpoint = self._last_checkpoint_v95
        return {
            "version": VERSION, "model_version": MODEL_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION, "read_only": True,
            "checkpoint": checkpoint, "top_pick": ranking[0] if ranking else None,
            "ranking": ranking,
            "assets": [{"asset": asset, **analysis} for asset, analysis in sorted(analyses.items())],
        }

    def market_data_status(self) -> dict[str, Any]:
        with self._v95_lock:
            latest = copy.deepcopy(self._latest_public)
        return {"version": VERSION, "read_only": True, "hub": self.market_data.health(), "assets": latest}

    def calibration_status(self) -> dict[str, Any]:
        return {"version": VERSION, "read_only": True, **self.ledger.metrics()}

    def scoreboard(self) -> dict[str, Any]:
        """Right/wrong record by interval (15M/10M/7M) and by pick rank (#1/#2/#3)."""
        return {"version": VERSION, "read_only": True, **self.ledger.scoreboard()}

    def learning_status(self) -> dict[str, Any]:
        return {
            "version": VERSION, "read_only": True, **self.ledger.status(),
            "last_reconcile": copy.deepcopy(self._last_reconcile),
            "last_market_reconcile": copy.deepcopy(self._last_market_reconcile),
            "scope": "SEPARATE_CHECKPOINT_CHALLENGERS_10M_PRIMARY",
            "primary_learning_checkpoint": self.ledger.primary_learning_checkpoint,
            "production_weights_frozen": True,
            "automatic_promotion": False, "automatic_threshold_changes": False,
        }

    def health(self) -> dict[str, Any]:
        try:
            parent = super().health()
        except Exception as exc:
            parent = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
        return {
            "version": VERSION, "enabled": self.v95_enabled, "read_only": True,
            "cycles": self._cycles, "errors": self._errors, "last_error": self._last_error,
            "run_cycle_timing": copy.deepcopy(self._run_cycle_timing),
            "parent_chain_timing": copy.deepcopy(getattr(self, "_chain_timing", {})),
            "slowest_run_cycle": copy.deepcopy(self._slowest_run_cycle),
            "last_checkpoint": self._last_checkpoint_v95,
            "telegram_sent": self._telegram_sent_v95,
            "telegram_failed": self._telegram_failed_v95,
            "telegram_suppressed": self._telegram_suppressed_v95,
            "canonical_snapshot": True, "timestamp_alignment": True,
            "runtime_binding": self._runtime_binding, "runtime_active": self._cycles > 0,
            "parent_input_bridge": copy.deepcopy(self._bridge_status),
            "raw_model_signal_independent_of_price": True,
            "market_anchor_strength": _env_float("Q15_V95_MARKET_ANCHOR_STRENGTH", 1.0, 0.0, 1.0),
            "production_weights_frozen": True, "shadow_challenger": True,
            "primary_learning_checkpoint": self.ledger.primary_learning_checkpoint,
            "learning_enabled_by_checkpoint": dict(self.ledger.learning_enabled_by_checkpoint),
            "automatic_promotion": False, "order_placement": False,
            "ledger": self.ledger.status(), "public_market_data": self.market_data.health(),
            "parent_v94": parent,
        }

    def diagnostics(self) -> dict[str, Any]:
        return {
            **self.health(),
            "settings": {
                "15m_min_probability": _env_float("Q15_V95_15M_MIN_PROBABILITY", 0.58, 0.50, 0.90),
                "10m_min_probability": _env_float("Q15_V95_10M_MIN_PROBABILITY", 0.60, 0.50, 0.90),
                "15m_required_edge_cents": _env_float("Q15_V95_15M_REQUIRED_EDGE_CENTS", 4.0, 0.0, 25.0),
                "10m_required_edge_cents": _env_float("Q15_V95_10M_REQUIRED_EDGE_CENTS", 6.0, 0.0, 25.0),
                "minimum_data_quality": _env_float("Q15_V95_MIN_DATA_QUALITY", 0.55, 0.20, 0.95),
                "champion_weights": dict(CHAMPION_WEIGHTS),
            },
            "hard_failures": [
                "missing or invalid spot", "missing or invalid threshold",
                "missing or expired time remaining", "stale core data",
            ],
            "continuous_evidence": [
                "multi-horizon momentum", "executed flow and absorption",
                "spot order-book pressure", "wicks", "prior/current 15-minute context",
                "threshold crossings and failed breakouts", "exchange consensus",
                "derivatives pressure",
            ],
            "latest": self.predictions(), "calibration": self.calibration_status(),
            "learning": self.learning_status(), "scoreboard": self.ledger.scoreboard(),
        }

    def decision_stats(self) -> dict[str, Any]:
        with self._v95_lock:
            counts: dict[str, int] = {}
            for analysis in self._latest_v95.values():
                state = str(analysis.get("trade_decision") or "UNKNOWN")
                counts[state] = counts.get(state, 0) + 1
        return {
            "version": VERSION, "read_only": True,
            "current_trade_decisions": counts,
            "ledger": self.ledger.status(), "metrics": self.ledger.metrics(),
        }


def format_telegram_message(text: Any) -> str:
    message = str(text or "")
    upper = message.upper()
    if "V9.5 CHECK" in message:
        return message
    with _LATEST_LOCK:
        analyses = copy.deepcopy(_LATEST_ANALYSES)
        ranking = copy.deepcopy(_LATEST_RANKING)
        ledger = copy.deepcopy(_LATEST_LEDGER)
        checkpoint = _LATEST_CHECKPOINT
    is_q15_report = any(token in upper for token in ("10M", "15M", "WATCH", "ENTRY", "NO TRADE", "THREE REQUIREMENTS", "30M CHART CONTEXT"))
    if analyses and ranking and is_q15_report:
        return build_v95_message(checkpoint, analyses, ranking, ledger)
    # Keep non-Q15 notifications compatible.  For a Q15 report before the first
    # V9.5 cycle, mark it as legacy instead of presenting missing data as a live
    # V9.5 conclusion.  The runtime activation installer ensures this state is
    # temporary and validates that the live constructor is CheckpointPolicyV95.
    if is_q15_report:
        return "🟡 Q15 V9.5 STARTUP — canonical analysis is not ready; legacy report suppressed. Check /api/q15-v9-5/diagnostics."
    return _format_v94_message(message)


__all__ = [
    "CanonicalSnapshot", "CheckpointPolicyV95", "FEATURE_SCHEMA_VERSION", "MODEL_VERSION",
    "READ_ONLY", "VERSION", "analyse_v95", "apply_v95_policy", "build_canonical_snapshot",
    "build_v95_message", "format_telegram_message", "rank_analyses",
]
