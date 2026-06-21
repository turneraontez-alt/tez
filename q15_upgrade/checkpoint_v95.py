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
import logging
import math
import os
import statistics
import threading
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from typing import Any, Mapping, MutableMapping, Sequence

logger = logging.getLogger(__name__)

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
from . import flip_risk
from . import manipulation_alert
from . import panels_v95
from . import shadow_economics
from .fast_candles import fast_canonical_candles
from .ledger_v95 import (
    CHAMPION_WEIGHTS,
    FEATURE_SCHEMA_VERSION,
    MODEL_VERSION,
    V95Ledger,
)
from .market_data_v95 import PublicMarketDataHub

VERSION = "q15-v9.5.2-runtime-activation-data-bridge-v1"
READ_ONLY = True

# Coverage weights for evidence_quality: a feature-importance-ordered blend of
# the per-feature qualities (sums to 1.0). Pinned by test_q15_v95_weights.py so
# an accidental edit fails loudly instead of silently re-weighting confidence.
# (absorption is excluded — it is derived from flow+momentum, so weighting its
# quality here would double-count those two.)
_EVIDENCE_QUALITY_WEIGHTS: dict[str, float] = {
    "momentum": 0.25,
    "flow": 0.16,
    "book": 0.12,
    "wick": 0.08,
    "context": 0.12,
    "threshold_interaction": 0.15,
    "exchange_consensus": 0.08,
    "derivatives": 0.04,
}

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


# --- Optional per-stage profiler for analyse_v95 -----------------------------
# Default OFF (Q15_V95_PROFILE_FEATURES): when on, times each feature/model/
# ledger stage inside analyse_v95 and accumulates totals so /api/health
# ("q15_v9_5.feature_profile") names the real hotspot to optimise next. Read-only
# and behaviour-neutral: when off, _timed just calls the function. Cumulative
# across cycles since enabled; judge on avg_ms (calls is per-asset-per-cycle).
_FEATURE_PROFILE_LOCK = threading.RLock()
_FEATURE_PROFILE: dict[str, dict[str, float]] = {}


def _feature_profile_enabled() -> bool:
    return _env_bool("Q15_V95_PROFILE_FEATURES", False)


def _record_feature_time(stage: str, seconds: float) -> None:
    with _FEATURE_PROFILE_LOCK:
        slot = _FEATURE_PROFILE.setdefault(stage, {"calls": 0.0, "total_s": 0.0})
        slot["calls"] += 1.0
        slot["total_s"] += seconds


def _timed(enabled: bool, stage: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Call ``fn`` and, when profiling is on, record its wall time under ``stage``.
    When off this is a transparent passthrough (one bool check)."""
    if not enabled:
        return fn(*args, **kwargs)
    start = time.monotonic()
    try:
        return fn(*args, **kwargs)
    finally:
        _record_feature_time(stage, time.monotonic() - start)


def feature_profile_health() -> dict[str, Any]:
    """Accumulated per-stage timing for analyse_v95, ranked by total time."""
    with _FEATURE_PROFILE_LOCK:
        stages = {
            name: {
                "calls": int(slot["calls"]),
                "total_s": round(slot["total_s"], 4),
                "avg_ms": round(1000.0 * slot["total_s"] / slot["calls"], 4) if slot["calls"] else 0.0,
            }
            for name, slot in sorted(_FEATURE_PROFILE.items(), key=lambda kv: kv[1]["total_s"], reverse=True)
        }
    return {"enabled": _feature_profile_enabled(), "stages": stages}


def feature_profile_reset() -> None:
    with _FEATURE_PROFILE_LOCK:
        _FEATURE_PROFILE.clear()


def _build_candles(
    snapshot: Mapping[str, Any],
    cached: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, float]]:
    """Canonical candle build for the v9.5 layer.

    Behaviour-identical to the frozen ``_canonical_candles`` (fuzz-locked in
    tests/test_q15_fast_canonical_candles.py); it uses the optimised equivalent
    that skips redundant per-row alias resolution on the cached history. Now
    default ON — set ``Q15_FAST_CANONICAL_CANDLES=false`` to revert to the frozen
    builder.
    """
    if _env_bool("Q15_FAST_CANONICAL_CANDLES", True):
        return fast_canonical_candles(snapshot, cached)
    return _canonical_candles(snapshot, cached)


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
        # Shallow field copy, NOT dataclasses.asdict(): asdict recursively
        # deep-copies every field — including the ~600-row candle tuple and the
        # full multi-exchange context/public dicts — on every analyse_v95 call
        # (it dominated ~78% of analyse_v95 in profiling). The deep copy was also
        # wasted on candles, which were immediately overwritten by list() below.
        # A one-level copy is value-identical here (the result is JSON-serialised
        # for the API and deep-copied downstream) and matches asdict's types.
        value: dict[str, Any] = {f.name: getattr(self, f.name) for f in fields(self)}
        value["candles"] = list(self.candles)
        value["context"] = dict(self.context)
        value["public"] = dict(self.public)
        value["feed_timestamps"] = dict(self.feed_timestamps)
        value["feed_ages"] = dict(self.feed_ages)
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
    candles = _build_candles(snapshot, cached_candles)
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
            # exp(-(age-grace)/tau): freshness 1.0 within `grace`, then an
            # e-folding decay with time constant `tau` seconds (NOT a half-life;
            # at age=grace+tau freshness is e^-1 ≈ 0.37).
            grace = _env_float("Q15_V95_PUBLIC_PRICE_GRACE_SECONDS", 5.0, 0.0, 60.0)
            tau = _env_float("Q15_V95_PUBLIC_PRICE_DECAY_SECONDS", 30.0, 2.0, 600.0)
            public_freshness = _clamp(math.exp(-max(0.0, public_age - grace) / tau), 0.0, 1.0)
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
    # Coordinate contract: `_window_return` yields LOG returns (candle space); the
    # public feed quotes SIMPLE fractional returns (e.g. 0.012 == +1.2%), which we
    # lift into log space with log1p before blending. A value outside (-1, 1) can't
    # be a plausible short-horizon fractional return (it's likely percent-scaled or
    # already-log from a feed change) and would make log1p raise/-inf, so it is
    # dropped rather than trusted — the candle return then stands alone.
    result = {f"return_{seconds}s": _window_return(canonical.candles, float(seconds)) for seconds in (5, 15, 30, 60, 180, 900, 1800)}
    public_returns = canonical.public.get("price_returns") if isinstance(canonical.public.get("price_returns"), Mapping) else {}
    for seconds in (5, 15, 30, 60, 180):
        key = f"return_{seconds}s"
        public_value = _num(public_returns.get(key))
        if public_value is not None and not (-1.0 < public_value < 1.0):
            public_value = None  # implausible coordinate; don't blend it
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


def _regime_anchor_strength(base_strength: float, regime: Mapping[str, Any]) -> tuple[float, float]:
    """Optionally scale the market-anchor strength by regime trustworthiness.

    The model is noisiest in chaotic regimes (high volatility, exchange
    divergence, threshold pin) and most reliable in clean trends. When
    ``Q15_V95_REGIME_AWARE_ANCHOR`` (default ON) we shrink the model's allowed
    deviation from the market as regime uncertainty rises above the NORMAL
    baseline (0.08), so a noisy regime is anchored harder to the (efficient)
    market. Set the flag to ``false`` to restore the identity factor of 1.0.

    Returns ``(effective_strength, factor)``.
    """
    if not _env_bool("Q15_V95_REGIME_AWARE_ANCHOR", True):
        return base_strength, 1.0
    baseline = 0.08
    uncertainty = float(regime.get("uncertainty", baseline) or baseline)
    sensitivity = _env_float("Q15_V95_REGIME_ANCHOR_SENSITIVITY", 3.0, 0.0, 20.0)
    floor = _env_float("Q15_V95_REGIME_ANCHOR_MIN_FACTOR", 0.40, 0.0, 1.0)
    factor = _clamp(1.0 - sensitivity * max(0.0, uncertainty - baseline), floor, 1.0)
    return base_strength * factor, factor


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


_MANIPULATION_REASON_PHRASES = {
    "ABSORPTION": "order-wall absorption",
    "PIN": "strike pin (outcome unstable)",
    "DIVERGENCE": "cross-exchange divergence",
}


def _manipulation_signal(regime: Mapping[str, Any], absorption: Mapping[str, Any],
                         exchange: Mapping[str, Any]) -> dict[str, Any]:
    """Read-only suspicion that large players are pushing the price around.

    Composed ENTIRELY from signals the model already computed this cycle — it
    never changes the prediction or the edge. Three classic "big spender" tells
    near a binary strike:

      * ABSORPTION — aggressive taker flow getting eaten by resting orders
        without price moving (someone defending a level); the most *directional*
        flip signal, so it also returns a ``lean`` (the side it is likely to
        reverse toward).
      * PIN — price stapled to the strike with repeated crossings: the outcome
        is unstable and prone to flip at settlement.
      * DIVERGENCE — one public venue pushed off the others' consensus.

    Returns ``{suspected, reasons, lean, score}``. Disable via
    ``Q15_V95_MANIPULATION_TRACKING=false``; ``Q15_V95_MANIPULATION_MIN_SIGNALS``
    (default 1) sets how many tells must agree to flag.
    """
    if not _env_bool("Q15_V95_MANIPULATION_TRACKING", True):
        return {"suspected": False, "reasons": [], "lean": None, "score": 0.0}
    regime_name = str((regime or {}).get("name") or "")
    absorption = absorption or {}
    exchange = exchange or {}
    divergence = _num(exchange.get("divergence_bps"), 0.0) or 0.0
    div_threshold = _env_float("Q15_V95_MANIPULATION_DIVERGENCE_BPS", 35.0, 0.0, 500.0)

    reasons: list[str] = []
    lean: str | None = None
    if absorption.get("available") and absorption.get("absorbed"):
        reasons.append("ABSORPTION")
        flow = _num(absorption.get("flow"), 0.0) or 0.0
        # Positive aggressive flow failing to lift price is bearish (leans NO);
        # negative flow failing to push price down is bullish (leans YES).
        lean = "NO" if flow > 0 else "YES" if flow < 0 else None
    if regime_name == "THRESHOLD_PIN":
        reasons.append("PIN")
    if regime_name == "EXCHANGE_DIVERGENCE" or divergence >= div_threshold:
        reasons.append("DIVERGENCE")

    min_signals = int(_env_float("Q15_V95_MANIPULATION_MIN_SIGNALS", 1.0, 1.0, 3.0))
    suspected = len(reasons) >= min_signals
    # Weighted toward absorption (the directional flip tell).
    score = min(1.0, len(reasons) / 3.0 + (0.34 if "ABSORPTION" in reasons else 0.0))
    return {
        "suspected": suspected,
        "reasons": reasons if suspected else [],
        "lean": (lean if suspected else None),
        "score": round(score, 4) if suspected else 0.0,
    }


def _manipulation_phrase(manip: Mapping[str, Any]) -> str:
    """Human-readable one-liner for a suspected-manipulation signal."""
    reasons = list(manip.get("reasons") or [])
    parts = [_MANIPULATION_REASON_PHRASES.get(r, r.lower()) for r in reasons]
    text = ", ".join(parts) if parts else "suspected"
    lean = manip.get("lean")
    if lean in ("YES", "NO"):
        text += f" · may flip → {lean}"
    return text


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
    prof = _feature_profile_enabled()
    volatility = _timed(prof, "volatility", _robust_volatility, canonical)
    returns = _timed(prof, "returns", _multi_horizon_returns, canonical)
    structural = _timed(prof, "structural", _structural_probability, canonical, volatility, returns)
    momentum, momentum_q, momentum_d = _timed(prof, "momentum", _momentum_feature, returns, volatility, canonical)
    flow, flow_q, flow_d = _timed(prof, "flow", _flow_feature, snapshot, canonical)
    book, book_q, book_d = _timed(prof, "book", _book_feature, snapshot, canonical)
    wick_raw, wick_d = _timed(prof, "wick", _wick_score, canonical.candles, canonical.yes_is_higher)
    wick = float(wick_raw or 0.0)
    wick_q = 0.0 if wick_raw is None else _clamp(len(canonical.candles) / 12.0, 0.0, 1.0)
    context, context_q, context_d = _timed(prof, "context", _context_feature, canonical)
    threshold, threshold_q, threshold_d = _timed(prof, "threshold", _threshold_interaction, canonical)
    exchange, exchange_q, exchange_d = _timed(prof, "exchange", _exchange_consensus, canonical, returns)
    derivatives, derivatives_q, derivatives_d = _timed(prof, "derivatives", _derivatives_feature, canonical, momentum)
    absorption, absorption_q, absorption_d = _timed(prof, "absorption", _absorption_feature, flow, flow_q, momentum, momentum_q)
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
    # Evidence quality = how much of the model's evidence is actually backed by
    # data, as a coverage-weighted blend of the per-feature qualities. The blend
    # weights sum to 1.0 and track feature importance (momentum/threshold/flow
    # lead; wick/derivatives trail) so that a missing high-value feature drags
    # quality down more than a missing minor one. This feeds both the market
    # anchor's model_trust and data_quality (-> temperature), so thin evidence
    # automatically yields a more conservative, market-anchored probability.
    # Weights are pinned by test_q15_v95_weights.py.
    evidence_quality = _clamp(
        sum(w * feature_quality[name] for name, w in _EVIDENCE_QUALITY_WEIGHTS.items()),
        0.0, 1.0,
    )
    data_quality = _clamp(0.70 * canonical.data_quality + 0.30 * evidence_quality, 0.0, 1.0)
    regime = _timed(prof, "regime", _regime, canonical, volatility, returns, threshold_d, exchange_d)
    raw_yes, contributions = _timed(prof, "model_champion", _model_probability, structural, feature_values, feature_quality, CHAMPION_WEIGHTS, regime, data_quality)
    calibration = _timed(prof, "calibrate", ledger.calibrate, raw_yes, canonical.checkpoint, canonical.asset) if ledger else {"probability": raw_yes, "active": False, "reason": "ledger_unavailable"}
    shadow_calibrated_yes = _clamp(float(calibration["probability"]), 0.01, 0.99)
    production_calibration_enabled = _env_bool("Q15_V95_PRODUCTION_CALIBRATION_ENABLED", True)
    model_yes = shadow_calibrated_yes if production_calibration_enabled and calibration.get("active") else raw_yes
    # Market-price anchoring: defer to the (efficient) Kalshi market unless the
    # model has earned the confidence to deviate. This is the bot's working prob.
    market_implied_yes = _market_implied_yes(snapshot)
    base_anchor_strength = _env_float("Q15_V95_MARKET_ANCHOR_STRENGTH", 1.0, 0.0, 1.0)
    anchor_strength, anchor_regime_factor = _regime_anchor_strength(base_anchor_strength, regime)
    calibrated_yes, market_anchor = _market_anchored_probability(
        model_yes, market_implied_yes, data_quality, evidence_quality, anchor_strength
    )
    if anchor_regime_factor != 1.0:
        market_anchor["regime_factor"] = round(anchor_regime_factor, 4)
        market_anchor["base_strength"] = base_anchor_strength
    challenger_weights = _timed(prof, "challenger_weights", ledger.challenger_weights, canonical.checkpoint, regime.get("name")) if ledger else CHAMPION_WEIGHTS
    challenger_yes, challenger_contributions = _timed(prof, "model_challenger", _model_probability, structural, feature_values, feature_quality, challenger_weights, regime, data_quality)
    provisional_side = "YES" if calibrated_yes >= 0.5 else "NO"
    pattern = _timed(prof, "pattern_similarity", ledger.pattern_similarity, feature_values, provisional_side, canonical.checkpoint) if ledger else {"active": False, "shadow_adjustment": 0.0}
    shadow_pattern_adjustment = float(pattern.get("shadow_adjustment") or 0.0)
    challenger_yes = _clamp(challenger_yes + (shadow_pattern_adjustment if provisional_side == "YES" else -shadow_pattern_adjustment), 0.01, 0.99)
    # Anchor the challenger identically so champion-vs-challenger compares weights, not anchoring.
    challenger_yes, _ = _market_anchored_probability(challenger_yes, market_implied_yes, data_quality, evidence_quality, anchor_strength)
    side = provisional_side
    selected = calibrated_yes if side == "YES" else 1.0 - calibrated_yes
    uncertainty = 0.018 + (1.0 - data_quality) * 0.12 + float(regime.get("uncertainty", 0.08)) * 0.25
    divergence = _num(exchange_d.get("divergence_bps"), 0.0) or 0.0
    uncertainty += min(0.04, divergence / 1000.0)
    # Evidence-coverage penalty: "insufficient evidence" must read as low
    # confidence, not as a clean neutral signal. Features that have no data
    # contribute nothing (quality 0), so a thin snapshot yields low coverage;
    # low coverage widens the conservative haircut toward 0.5. Default 0.08
    # (moderate); set to 0.0 to disable, up to 0.20 for a stronger thin-data haircut.
    coverage_penalty = _env_float("Q15_V95_EVIDENCE_COVERAGE_PENALTY", 0.08, 0.0, 0.20)
    if coverage_penalty > 0.0:
        coverage_floor = _env_float("Q15_V95_EVIDENCE_COVERAGE_FLOOR", 0.40, 0.0, 1.0)
        covered = sum(1 for q in feature_quality.values() if q >= coverage_floor)
        coverage = covered / max(1, len(feature_quality))
        uncertainty += coverage_penalty * (1.0 - coverage)
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
    # Unknown depth (orderbook feed unavailable) is not the same as deep liquidity.
    # When enabled, discount the liquidity component for an unverifiable book so it
    # ranks below confirmed-liquid markets. Default OFF (model-scoring change); only
    # affects trade_quality ranking, never the entry gate, so it cannot place a trade.
    penalize_unknown_depth = _env_bool("Q15_V95_PENALIZE_UNKNOWN_DEPTH", False)
    unknown_depth_factor = _env_float("Q15_V95_UNKNOWN_DEPTH_FACTOR", 0.5, 0.0, 1.0)
    min_seconds = _env_float("Q15_V95_MIN_SECONDS_REMAINING", 20.0, 0.0, 300.0)
    total_costs = float(costs.get("total_cents") if "total_cents" in costs else costs.get("total_cost_cents") or 0.0)
    net_edge = None if ask is None else conservative * 100.0 - ask - total_costs
    ideal_entry = _clamp(conservative * 100.0 - total_costs - required_edge, 0.0, 100.0)
    liquidity_quality = 1.0
    if spread is not None:
        liquidity_quality *= _clamp(1.0 - spread / max(max_spread * 1.5, 1.0), 0.0, 1.0)
    if depth is not None and min_depth > 0:
        liquidity_quality *= _clamp(depth / min_depth, 0.0, 1.0)
    elif depth is None and penalize_unknown_depth:
        liquidity_quality *= unknown_depth_factor
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
        "manipulation": _manipulation_signal(regime, absorption_d, exchange_d),
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
    # Surface the blocker (e.g. the AVOID_INVALID_DATA core_errors) so the exact
    # reason a prediction is withheld is visible in /api/snapshot and the dashboard.
    snapshot["q15_v9_5_main_blocker"] = analysis.get("main_blocker")
    snapshot["q15_v9_5_net_edge_cents"] = analysis.get("net_edge_cents")
    snapshot["q15_v9_5_ideal_entry_cents"] = analysis.get("ideal_entry_cents")
    # Shadow entry-economics (read-only A/B): what a stricter, cost-aware gate would
    # decide on this same pick. NEVER changes the live decision — only surfaced so
    # the live vs shadow gates can be watched live and graded in the hourly report.
    try:
        _se_cfg = shadow_economics.EconConfig.from_env()
        if _se_cfg.enabled:
            _quote = analysis.get("quote") or {}
            _costs = analysis.get("costs") or {}
            _ask = _num(_quote.get("ask_cents"))
            _cost = _num(_costs.get("total_cents"))
            if _cost is None:
                _cost = _num(_costs.get("total_cost_cents"), 0.0) or 0.0
            _sd = shadow_economics.shadow_gate(
                _num(analysis.get("conservative_probability")), _ask, _cost,
                shadow_economics.required_edge_for(analysis.get("checkpoint") or analysis.get("interval") or ""),
                _se_cfg)
            snapshot["q15_v9_5_shadow_econ_enter"] = bool(_sd.enter)
            snapshot["q15_v9_5_shadow_econ_net_edge_cents"] = _sd.net_edge_cents
            snapshot["q15_v9_5_shadow_econ_reason"] = _sd.reason
    except Exception:
        pass
    snapshot["q15_v9_5_regime"] = (analysis.get("regime") or {}).get("name")
    snapshot["q15_v9_5_entry_allowed"] = bool(analysis.get("entry_allowed"))
    # Suspected price-manipulation tracking (read-only; does not affect the call).
    _manip = analysis.get("manipulation") or {}
    snapshot["q15_v9_5_manipulation_suspected"] = bool(_manip.get("suspected"))
    snapshot["q15_v9_5_manipulation_reason"] = ",".join(_manip.get("reasons") or []) or None
    snapshot["q15_v9_5_manipulation_lean"] = _manip.get("lean")
    # Flip-risk overlay base fields (threshold/flip-prob/state added in run_cycle
    # where the learned stats + cross-cycle persistence are available).
    _fr = analysis.get("flip_risk") or {}
    snapshot["q15_v9_5_flip_risk_score"] = _fr.get("score")
    snapshot["q15_v9_5_flip_risk_confidence"] = _fr.get("confidence")
    snapshot["q15_v9_5_flip_risk_primary_reason"] = _fr.get("primary_reason")
    snapshot["q15_v9_5_flip_risk_direction"] = _fr.get("direction_monitored")
    snapshot["q15_v9_5_flip_risk_evidence_count"] = _fr.get("evidence_count")
    # Richer prediction-card fields (the run_cycle recording loop refreshes
    # interval/stability/expiry/timestamp with live clock context each cycle).
    snapshot["q15_v9_5_confidence_grade"] = analysis.get("confidence_grade")
    snapshot["q15_v9_5_selected_probability"] = analysis.get("selected_probability")
    snapshot["q15_v9_5_interval"] = analysis.get("checkpoint")
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


def _pct0(value: Any) -> str:
    """Whole-percent formatter for the compact monospace checkpoint table."""
    parsed = _num(value)
    return "—" if parsed is None else f"{parsed * 100:.0f}%"


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


def _seconds_phrase(value: Any) -> str:
    """`~Xm Ys left` entry-deadline phrase from seconds-remaining, or `—`."""
    secs = _num(value)
    if secs is None or secs < 0:
        return "—"
    total = int(secs)
    return f"~{total // 60}m {total % 60:02d}s left"


def build_v95_message(checkpoint: str, analyses: Mapping[str, Mapping[str, Any]], ranking: Sequence[Mapping[str, Any]], ledger_status: Mapping[str, Any], result_events: Sequence[Mapping[str, Any]] | None = None, followup_remaining: bool = True) -> str:
    """Render the checkpoint alert in the hourly-report house style.

    A bold header, a one-line headline, then a compact ``<pre>`` monospace table
    of the top picks (asset / side / model prob / market prob / edge) — the same
    aesthetic as ``reporting.HourlyReporter._scoreboard_table``. Live entries get
    a highlighted ask→max economics line below the table (the detail you'd act
    on); unavailable picks are listed below it so the columns never break.

    The header deliberately keeps ``V9.5 CHECK`` (formatter guard) and the
    ``ENTRY RECOMMENDED`` / ``NO ENTRY YET`` markers (alert-suppression keys on
    them). It must NOT carry the ``Hourly Report —`` marker (that would reroute
    it past the reformatters).
    """
    any_entry = any(bool(row.get("entry_allowed")) for row in analyses.values())
    emoji = "✅" if any_entry else "👀"
    state = "ENTRY RECOMMENDED" if any_entry else "NO ENTRY YET"
    # The bold title stays OUTSIDE the box (suppression + reformatters key on the
    # V9.5 CHECK / ENTRY RECOMMENDED / NO ENTRY YET markers here); everything else
    # goes INSIDE one <pre> block so the whole card renders as a single panel.
    header = f"{emoji} <b>{checkpoint} V9.5 CHECK · {state}</b>"
    body: list[str] = []

    top = list(ranking[:3])
    available = [r for r in top if analyses.get(str(r["asset"]), {}).get("prediction_available")]
    unavailable = [r for r in top if not analyses.get(str(r["asset"]), {}).get("prediction_available")]

    # The recommended BEST ENTRY is rank #1 of the qualifying entries — the SAME
    # ranking the detailed table below renders, so the top summary and detail can
    # never disagree. Only an asset with an actual recommended entry is eligible.
    best_entry = _best_entry(analyses, ranking)
    pbc = (ledger_status.get("pushed_by_checkpoint") or {}).get(checkpoint) or {}
    p_settled = int(pbc.get("settled") or 0)
    p_acc = pbc.get("accuracy")
    p_acc_s = f"{p_acc * 100:.0f}%" if isinstance(p_acc, (int, float)) else "n/a"
    if best_entry is not None:
        be_asset, ba = best_entry
        be_side = ba.get("prediction_side") or "—"
        _ne = _num(ba.get("net_edge_cents"))
        ne_s = f"{_ne:+.1f}¢" if _ne is not None else "—"
        body += [
            f"🏆 BEST ENTRY — {be_asset} {be_side}",
            f"Interval: {checkpoint}",
            "Entry status: RECOMMENDED",
            f"Probability: {_fmt_probability(ba.get('selected_probability'))}",
            f"Recommended entry: {_c(ba.get('ideal_entry_cents'))} or lower",
            f"Conservative net edge: {ne_s}",
            f"Follow-up check remaining: {'YES' if followup_remaining else 'NO'}",
            f"P(Yes) {_pct0(ba.get('yes_probability'))} · P(No) {_pct0(ba.get('no_probability'))}",
            f"Type: checkpoint entry · entry by {_seconds_phrase(ba.get('seconds_remaining'))} · "
            f"pushed {checkpoint} acc {p_acc_s} (n={p_settled})",
        ]
    else:
        # No qualifying entry. Show NO ENTRY RECOMMENDED, plus the single best
        # prediction we are watching (held), so the card still informs.
        body.append("👀 NO ENTRY RECOMMENDED")
        best = _best_pick(analyses, ranking)
        if best is not None and analyses.get(best[0], {}).get("prediction_available"):
            b_asset, a = best
            side = a.get("prediction_side") or "—"
            net = a.get("net_edge_cents")
            grade = a.get("confidence_grade") or "—"
            conf = _pct0(a.get("selected_probability"))
            stab = a.get("stability")
            tag = f" · {stab}" if stab else ""
            if net is not None:
                need = a.get("required_edge_cents")
                body.append(
                    f"Watching: {b_asset} {side} {conf} (grade {grade}){tag} · "
                    f"edge {_c(net, signed=True)} (need {_c(need)}) — holding"
                )
            else:
                body.append(f"Watching: {b_asset} {side} {conf} (grade {grade}){tag} · no executable edge — holding")
            body.append(f"P(Yes) {_pct0(a.get('yes_probability'))} · P(No) {_pct0(a.get('no_probability'))} · {checkpoint} interval")
        else:
            body.append("No prediction available this cycle")

    # Aligned monospace table of the available picks (model vs market + edge).
    if available:
        body.append("")
        body.append("Top picks")
        body.append(f"{'':<6}{'Side':>5}{'P':>6}{'Mkt':>6}{'Edge':>7}")
        for row in available:
            asset = str(row["asset"])
            a = analyses[asset]
            side = a.get("prediction_side") or "—"
            prob = _pct0(a.get("selected_probability"))
            # Market-implied prob for the selected side (invert YES-implied for a NO pick).
            market_yes = _num(a.get("market_implied_yes_probability"))
            market_for_side = None if market_yes is None else (market_yes if side == "YES" else 1.0 - market_yes)
            mkt = _pct0(market_for_side)
            edge = _c(a.get("net_edge_cents"), signed=True)
            body.append(f"{asset:<6}{side:>5}{prob:>6}{mkt:>6}{edge:>7}")

    # Entry economics — only for live entries; the actionable detail.
    for row in available:
        asset = str(row["asset"])
        a = analyses[asset]
        if a.get("entry_allowed"):
            ask = _c((a.get("quote") or {}).get("ask_cents"))
            body.append(
                f"✅ {asset} entry — ask {ask} → max {_c(a.get('ideal_entry_cents'))} · "
                f"edge {_c(a.get('net_edge_cents'), signed=True)}"
            )

    # Suspected price-manipulation watch: surface any flagged pick so you can read
    # the alert with the caveat. Read-only signal — it never changed the call.
    if _env_bool("Q15_V95_MANIPULATION_ALERT_TAG", True):
        flagged = [
            (str(row["asset"]), analyses.get(str(row["asset"]), {}).get("manipulation") or {})
            for row in top
        ]
        flagged = [(asset, manip) for asset, manip in flagged if manip.get("suspected")]
        if flagged:
            body.append("")
            body.append("⚠ Manipulation watch")
            for asset, manip in flagged:
                body.append(f"{asset}: {_manipulation_phrase(manip)}")

    # Unavailable picks (kept in the box so the whole card is one panel).
    for row in unavailable:
        asset = str(row["asset"])
        a = analyses[asset]
        body.append(f"⛔ {asset} — no prediction ({_humanize_v95_reasons(a.get('main_blocker'))})")

    if result_events:
        marks = "  ".join(f"{e.get('asset')} {'✅' if e.get('correct') else '❌'}" for e in result_events[:4])
        body.append(f"Recent results — {marks}")
    body.append("")
    body.append("Paper monitor · not advice · no orders placed")

    # Header outside, the entire body inside one <pre> "panel".
    lines = [header, "<pre>", *body, "</pre>"]
    text = "\n".join(lines)
    return text if len(text) <= 4000 else text[:3985] + "…\n</pre>"


# Lower seconds-remaining boundary of each checkpoint band (mirrors
# _resolve_checkpoint): a band "closes" when the clock drops below this.
_CHECKPOINT_BAND_LOWER = {"15M": 660.0, "10M": 480.0, "7M": 0.0}

# The named minute each checkpoint is *about* — the alert is held until the clock
# reaches this many seconds before close so e.g. the "10M" check lands at the
# 10-minute mark, not at band entry (~11:00). The detection bands above are wider
# than these marks; firing is mark-driven, not band-entry-driven.
_CHECKPOINT_TARGET_SECONDS = {"15M": 900.0, "10M": 600.0, "7M": 420.0}


# Seconds-remaining at which each interval's alert is considered expired: the
# next interval's mark for 15M/10M, and a configurable cutoff before close for
# 7M (the final leg) so a 7-minute alert does NOT stay live until market close.
def _checkpoint_expiry_seconds(checkpoint: str) -> float:
    if checkpoint == "15M":
        return 600.0   # superseded by the 10M check at the 10:00 mark
    if checkpoint == "10M":
        return 420.0   # superseded by the 7M check at the 7:00 mark
    # 7M is last: expire it well before close (default 2:00 left) so it doesn't
    # linger as an "active" prediction with nothing left to act on.
    return _env_float("Q15_V95_7M_EXPIRY_SECONDS", 120.0, 0.0, 420.0)


def _checkpoint_expired(checkpoint: str, seconds_left: float | None) -> bool:
    """A checkpoint alert is expired once the clock passes its interval end."""
    if seconds_left is None:
        return False
    return seconds_left <= _checkpoint_expiry_seconds(checkpoint)


def _iso_from_epoch(epoch: float) -> str:
    """UTC ISO-8601 timestamp for a unix epoch (the prediction's wall-clock)."""
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return datetime.now(timezone.utc).isoformat()


def _best_entry(analyses: Mapping[str, Mapping[str, Any]], ranking: Sequence[Mapping[str, Any]]) -> tuple[str, Mapping[str, Any]] | None:
    """The single recommended BEST ENTRY: rank #1 of the qualifying entries.

    This is the ONE source of truth for "best entry" — derived from the SAME
    ``ranking`` (``rank_analyses``) the detailed table renders, never an
    independent confidence-only calculation. ``rank_analyses`` floats every
    ENTRY_RECOMMENDED pick (priority 8) above all non-entries and orders them by
    conservative net edge → confidence → data quality, so the first entry-allowed
    row is also the overall rank #1 whenever any entry qualifies. Only an asset
    with an actual recommended entry is eligible. Returns ``(asset, analysis)`` or
    ``None`` when nothing qualifies.
    """
    for row in ranking:
        asset = str(row.get("asset"))
        a = analyses.get(asset)
        if a and a.get("entry_allowed"):
            return asset, a
    return None


def _best_entry_consistent(analyses: Mapping[str, Mapping[str, Any]], ranking: Sequence[Mapping[str, Any]]) -> bool:
    """Validation guard: the displayed best entry MUST equal rank #1 of the
    detailed ranking. True when there is no entry, or the first entry-allowed row
    in the ranking is also ``ranking[0]`` (top summary and detail agree). The
    alert is suppressed if this fails, so a top/detail mismatch can never ship.
    """
    best = _best_entry(analyses, ranking)
    if best is None:
        return True
    return bool(ranking) and str(ranking[0].get("asset")) == best[0]


def _followup_verdict(recommended_side: str, a: Mapping[str, Any] | None) -> tuple[str, str]:
    """Read-only verdict for the one follow-up check. Returns (tag, advice) where
    tag is an actionable header token (HOLD / EXIT / REVERSAL) the notifier won't
    suppress. Covers hold / take-profit / avoid / exit / side-change."""
    if a is None:
        return "EXIT", "Contract no longer active — interval ended. No further action."
    cur_side = str(a.get("prediction_side") or "").upper()
    rec = str(recommended_side or "").upper()
    if cur_side and rec and cur_side != rec:
        return "REVERSAL", f"SIDE CHANGED → now {cur_side}. Avoid adding; exit if already in."
    if not a.get("entry_allowed"):
        return "EXIT", (f"Entry no longer valid ({_decision_label(a.get('trade_decision'))}). "
                        "Avoid new entry; take profit / exit if filled.")
    flip_score = _num((a.get("flip_risk") or {}).get("score"), 0.0) or 0.0
    if flip_score >= _env_float("Q15_V95_FOLLOWUP_FLIP_RISK_SCORE", 70.0, 0.0, 100.0):
        return "HOLD", "Still valid but elevated reversal risk — consider taking profit / tightening."
    ask = _c((a.get("quote") or {}).get("ask_cents"))
    return "HOLD", f"Still valid — HOLD. Entry ≤ {_c(a.get('ideal_entry_cents'))} (ask {ask})."


def _followup_checkpoints() -> set[str]:
    """Intervals eligible for the one follow-up check (15M and 10M by default;
    7M is the final short leg and is excluded). Override via
    Q15_V95_FOLLOWUP_CHECKPOINTS (comma-separated)."""
    raw = os.environ.get("Q15_V95_FOLLOWUP_CHECKPOINTS", "15M,10M")
    return {tok.strip().upper() for tok in raw.split(",") if tok.strip()}


def build_followup_message(checkpoint: str, asset: str, recommended_side: str,
                           a: Mapping[str, Any] | None) -> str:
    """The single per-interval follow-up alert confirming a recommended entry."""
    tag, advice = _followup_verdict(recommended_side, a)
    header = f"🔁 <b>FOLLOW-UP — {asset} {checkpoint} · {tag}</b>"
    cur = str((a or {}).get("prediction_side") or "—").upper()
    status = "still valid" if (a and a.get("entry_allowed")) else "not valid"
    body = [
        f"Recommended side: {recommended_side or '—'}",
        f"Current side: {cur}",
        f"Entry status now: {status}",
        f"Verdict: {advice}",
        "",
        "Paper monitor · not advice · no orders placed",
    ]
    return header + "\n<pre>\n" + "\n".join(body) + "\n</pre>"


# --- compact checkpoint-panel mapping (forward-looking V9.5 CHECK push) --------
_PRIOR_CHECKPOINT = {"10M": "15M", "7M": "10M"}

_ENTRY_STATE_BY_DECISION = {
    "ENTRY_RECOMMENDED": panels_v95.ENTRY_RECOMMENDED,
    "WATCH_PRICE": panels_v95.WAIT,
    "WATCH_CONFIDENCE": panels_v95.WATCH,
    "WATCH_DATA_QUALITY": panels_v95.WATCH,
    "WATCH_LIQUIDITY": panels_v95.WATCH,
    "WATCH_TIME": panels_v95.WATCH,
    "PREDICTION_ONLY": panels_v95.NO_ENTRY,
    "AVOID_INVALID_DATA": panels_v95.NO_ENTRY,
}

_WATCH_REASON_BY_BLOCKER = {
    "conservative_probability_below_threshold": "directional confidence not yet sufficient",
    "data_quality_below_threshold": "data coverage too thin to commit",
    "spread_too_wide": "spread too wide to enter",
    "insufficient_depth_at_ask": "not enough resting size at the ask",
    "stale_kalshi_quote": "Kalshi quote is stale",
    "too_little_time_remaining": "too little time left in the contract",
}


def _direction_after_side(analysis: Mapping[str, Any]) -> str | None:
    """The side a flagged manipulation is expected to settle toward — the gradable
    'direction after'. Prefers the flip-risk monitored direction ('NO → YES'),
    falling back to the manipulation lean."""
    fr = analysis.get("flip_risk") or {}
    monitored = str(fr.get("direction_monitored") or "")
    if "→" in monitored:
        tail = monitored.split("→")[-1].strip().upper()
        if tail in {"YES", "NO"}:
            return tail
    lean = str((analysis.get("manipulation") or {}).get("lean") or "").upper()
    return lean if lean in {"YES", "NO"} else None


def _panel_manipulation(analysis: Mapping[str, Any]) -> dict[str, Any] | None:
    """Map the read-only manipulation/flip signals into the panel's MANIPULATION
    block, or None when nothing is flagged. ``risk`` is a 0..100 number."""
    fr = analysis.get("flip_risk") or {}
    manip = analysis.get("manipulation") or {}
    score = fr.get("score")
    if score is None:
        score = manip.get("score")
    suspected = bool(manip.get("suspected")) or (score is not None and float(score) >= 60.0)
    if not suspected or score is None:
        return None
    score = float(score)
    level = "HIGH" if score >= 60.0 else "MEDIUM" if score >= 35.0 else "LOW"
    reasons = list(manip.get("reasons") or [])
    if not reasons and fr.get("primary_reason"):
        reasons = [str(fr.get("primary_reason"))]
    return {
        "risk": score,
        "level": level,
        "type": ", ".join(str(r) for r in reasons) if reasons else None,
        "direction_after": _direction_after_side(analysis),
        "entry_effect": "WAIT",
    }


def _panel_entry(analysis: Mapping[str, Any], entry_state: str) -> dict[str, Any]:
    """Entry-guidance detail block (target/trigger for WAIT, reason for WATCH)."""
    quote = analysis.get("quote") or {}
    ask = _num(quote.get("ask_cents"))
    ideal = _num(analysis.get("ideal_entry_cents"))
    if entry_state == panels_v95.WAIT:
        return {
            "current_price": ask,
            "max_price": ideal,
            "trigger": "price at/below the maximum, or a rejection wick / momentum turn",
        }
    if entry_state in (panels_v95.ENTER_NOW, panels_v95.ENTRY_RECOMMENDED):
        return {"current_price": ask, "max_price": ideal}
    if entry_state == panels_v95.WATCH:
        blocker = str(analysis.get("main_blocker") or "")
        return {"reason": _WATCH_REASON_BY_BLOCKER.get(blocker, blocker or "not actionable yet")}
    return {}


def build_compact_checkpoint_panel(checkpoint: str, asset: str,
                                   analysis: Mapping[str, Any],
                                   prior_side: str | None) -> str:
    """Assemble the forward-looking V9.5 CHECK panel for one (asset, checkpoint)."""
    side = str(analysis.get("prediction_side") or "")
    yes_p = _num(analysis.get("yes_probability"))
    if yes_p is not None and side.upper() == "NO":
        side_prob = 1.0 - yes_p
    else:
        side_prob = yes_p
    entry_state = _ENTRY_STATE_BY_DECISION.get(str(analysis.get("trade_decision")), panels_v95.NO_ENTRY)
    return panels_v95.build_checkpoint_panel(
        checkpoint=checkpoint, asset=asset, side=side, probability=side_prob,
        prior_side=prior_side, prior_checkpoint=_PRIOR_CHECKPOINT.get(str(checkpoint).upper()),
        manipulation=_panel_manipulation(analysis),
        entry_state=entry_state, entry=_panel_entry(analysis, entry_state),
    )


def _best_pick(analyses: Mapping[str, Mapping[str, Any]], ranking: Sequence[Mapping[str, Any]]) -> tuple[str, Mapping[str, Any]] | None:
    """The single highest-confidence prediction this cycle.

    Prefers picks whose prediction is available, choosing the one with the
    greatest ``selected_probability`` (confidence), tie-broken by net edge. Falls
    back to the top-ranked row when none are marked available so the identity is
    still well-defined. Returns ``(asset, analysis)`` or ``None``.
    """
    ranked = [(str(r.get("asset")), analyses.get(str(r.get("asset")), {})) for r in ranking]
    if not ranked:
        return None
    available = [(a, x) for a, x in ranked if x.get("prediction_available")]
    pool = available if available else ranked

    def _confidence(item: tuple[str, Mapping[str, Any]]) -> tuple[float, float]:
        _a, x = item
        return (float(x.get("selected_probability") or 0.0), float(x.get("net_edge_cents") or -1e9))

    return max(pool, key=_confidence)


def _material_token(checkpoint: str, analyses: Mapping[str, Mapping[str, Any]], ranking: Sequence[Mapping[str, Any]]) -> str:
    """A short token for the *material* state of the best pick: which coin, which
    side (direction), and which confidence grade. The alert key embeds this, so a
    new alert is only minted when the direction or confidence band materially
    changes — not on minor probability/edge jitter within the same grade."""
    best = _best_pick(analyses, ranking)
    if best is None:
        return "none"
    asset, a = best
    return f"{asset}:{a.get('prediction_side') or '-'}:{a.get('confidence_grade') or '-'}"


def _decision_signature(analyses: Mapping[str, Mapping[str, Any]], ranking: Sequence[Mapping[str, Any]]) -> tuple:
    """Identity of the current verdict — the single best pick's (asset, side,
    grade, entry?) material state.

    Used to decide when the model has "made up its mind": the alert is held until
    this signature is stable across several cycles, so leader/edge jitter early in
    a checkpoint band no longer produces a burst of alerts. Keying on the single
    best pick (not the whole top-3) means a reshuffle of the trailing picks no
    longer counts as a new verdict.
    """
    best = _best_pick(analyses, ranking)
    if best is None:
        return ()
    asset, a = best
    return (asset, a.get("prediction_side"), a.get("confidence_grade"), bool(a.get("entry_allowed")))


def _notification_identity(checkpoint: str, analyses: Mapping[str, Mapping[str, Any]], ranking: Sequence[Mapping[str, Any]], now: float) -> tuple[str, str, str]:
    # One alert per (checkpoint, 15-minute market window, material state). All
    # assets share the XX:00/15/30/45 boundaries, so `now // 900` is identical for
    # every cycle and asset within a market. Embedding the material token (best
    # coin / side / grade) means an unchanged verdict reuses the same key (and is
    # deduplicated after the first send), while a real direction or confidence-band
    # change mints a fresh key — i.e. a replacement alert — exactly as wanted.
    # Disable via Q15_V95_SINGLE_ALERT_PER_CHECKPOINT=false.
    if _env_bool("Q15_V95_SINGLE_ALERT_PER_CHECKPOINT", True):
        token = _material_token(checkpoint, analyses, ranking)
        event_key = f"{VERSION}|{checkpoint}|W{int(now // 900)}|{token}"
    else:
        # Legacy fallback (single-alert mode OFF): key on the 15-minute market
        # window only — timestamp-seeded, not per-ticker. A per-ticker key churned
        # whenever the top ticker flipped between cycles (most acutely UNKNOWN →
        # real ticker as a stale book recovered), re-firing the same checkpoint;
        # all assets share the XX:00/15/30/45 boundaries, so the window is stable.
        event_key = f"{VERSION}|{checkpoint}|W{int(now // 900)}"
    has_entry = any(bool(analysis.get("entry_allowed")) for analysis in analyses.values())
    state = "ENTRY_RECOMMENDED" if has_entry else "WATCH"
    fingerprint = hashlib.sha256(json.dumps({
        "checkpoint": checkpoint,
        "state": state,
        "top": [(row.get("asset"), row.get("prediction_side"), row.get("trade_decision")) for row in ranking[:3]],
    }, sort_keys=True).encode()).hexdigest()[:20]
    return event_key, state, fingerprint


def _resolve_checkpoint(
    snapshots: Mapping[str, Mapping[str, Any]],
    messages: Sequence[str],
    now: float,
) -> str:
    """Authoritatively resolve the active checkpoint (15M/10M/7M).

    The inherited ``_detect_checkpoint`` consults a recursive snapshot key-walk
    (``_first_value``) and the buffered parent message text BEFORE its
    time-based fallback. Both are unreliable on the live path: a stale nested
    ``*checkpoint*``/``*stage*``/``*horizon*`` value, or any parent message that
    merely contains the substring ``"15M"`` (e.g. the
    ``30M CHART CONTEXT — PRIOR 15M + CURRENT 15M`` header), pins the label to
    ``15M`` for the whole cycle. The observed effect was every prediction being
    recorded under 15M — so 10M/7M never accumulated and the 10M/7M checkpoint
    alerts never fired.

    When the feed carries ``seconds_remaining`` we trust it: classify by the
    *same* boundaries ``_detect_checkpoint`` uses for its time fallback, and only
    defer to the heuristic detector when no time is available (or when disabled
    via ``Q15_V95_TIME_AUTHORITATIVE_CHECKPOINT=false``).
    """
    if not _env_bool("Q15_V95_TIME_AUTHORITATIVE_CHECKPOINT", True):
        return _detect_checkpoint(snapshots, messages)
    times = []
    for snapshot in snapshots.values():
        if not isinstance(snapshot, Mapping):
            continue
        seconds = _seconds_remaining(snapshot, now)
        if seconds is not None:
            times.append(seconds)
    if not times:
        return _detect_checkpoint(snapshots, messages)
    # Boundaries mirror _detect_checkpoint's time fallback exactly (same env
    # vars + defaults): >=15m boundary is 15M, >=7m boundary is 10M, else 7M.
    fifteen_boundary = _env_float("Q15_V95_15M_BOUNDARY_SECONDS", 660.0, 0.0, 1800.0)
    seven_boundary = _env_float("Q15_V95_7M_BOUNDARY_SECONDS", 480.0, 0.0, 1800.0)
    longest = max(times)
    if longest >= fifteen_boundary:
        return "15M"
    if longest >= seven_boundary:
        return "10M"
    return "7M"


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
            candles = _build_candles(row, cached)
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
        # Per (checkpoint, market-window) verdict-stability tracker so an alert is
        # only emitted once the decision has held steady for a few cycles.
        self._decision_stability: dict[tuple[str, int], dict[str, Any]] = {}
        # Per (asset, checkpoint, market-window) trend tracker: the prior cycle's
        # side + confidence, so each prediction can be tagged stable / strengthening
        # / weakening / changed for the UI and alert.
        self._prediction_trend: dict[tuple[str, str, int], dict[str, Any]] = {}
        # Flip-risk alert state per (asset, checkpoint, ticker): persistence count,
        # hysteresis latch, last-alert score/time/categories — carried across cycles
        # by the alert state machine. And a dedup set for confirmed-flip notices.
        self._flip_alert_state: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._flip_confirmed_sent: set[tuple[str, str, str]] = set()
        # Gated manipulation-alert notification policy: detection always runs; a
        # manipulation alert is only PUSHED after the interval's normal check was
        # delivered AND the later analysis recommends a different action (see
        # manipulation_alert). Per-cycle candidate buffer, the last delivered
        # normal check per interval, and a dedup set of already-sent findings.
        self._manip_candidates: list[Any] = []
        self._normal_check: dict[str, Any] = {}
        self._manip_alert_sent: set[tuple[str, str, str]] = set()
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
        self._warn_throttle: dict[str, float] = {}

    def _throttled_warn(self, key: str, fmt: str, *args: Any, now: float | None = None,
                        interval: float = 60.0) -> None:
        """Log a WARNING at most once per ``interval`` seconds per ``key`` so a
        recurring degraded condition surfaces without flooding the ~1s loop."""
        ts = now if now is not None else time.time()
        last = self._warn_throttle.get(key)
        if last is not None and (ts - last) < interval:
            return
        self._warn_throttle[key] = ts
        logger.warning(fmt, *args)

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
            checkpoint = _resolve_checkpoint(
                {str(key): value for key, value in parent_output.items() if isinstance(value, Mapping)},
                deferred.messages(),
                now,
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
                # Flip-risk overlay (read-only; never changes the prediction).
                if _env_bool("Q15_V95_FLIP_RISK_TRACKING", True):
                    _ra = flip_risk.compute_risk(analysis)
                    analysis["flip_risk"] = _ra.as_dict()
                    analysis["flip_risk_obj"] = _ra
                apply_v95_policy(snapshot, analysis)
                _sub["analyse"] += time.monotonic() - _s
                analyses[asset] = copy.deepcopy(analysis)
                output[asset_key] = snapshot
                canonicals[asset] = canonical
            ranking = rank_analyses(analyses)
            ranks = {str(row["asset"]): int(row["rank"]) for row in ranking}
            # Learned flip stats (cached against the data version) for this cycle's
            # threshold/flip-probability resolution.
            flip_learned = self.ledger.flip_stats() if _env_bool("Q15_V95_FLIP_RISK_TRACKING", True) else {"available": False}
            flip_sent = flip_failed = 0
            self._manip_candidates = []  # rebuilt every cycle by _process_flip_risk
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
                # Richer per-prediction UI fields: interval, grade, confidence,
                # explicit P(yes)/P(no) (already summing to ~1.0 from analyse_v95),
                # timestamp, time-remaining, stability trend, and interval expiry.
                stability = self._stability_marker(asset, checkpoint, analysis, now)
                analysis["stability"] = stability
                seconds_left = _seconds_remaining(snapshot, now)
                expired = _checkpoint_expired(checkpoint, seconds_left)
                analysis["interval"] = checkpoint
                analysis["expired"] = expired
                analysis["seconds_remaining"] = seconds_left
                snapshot["q15_v9_5_interval"] = checkpoint
                snapshot["q15_v9_5_confidence_grade"] = analysis.get("confidence_grade")
                snapshot["q15_v9_5_selected_probability"] = analysis.get("selected_probability")
                snapshot["q15_v9_5_prediction_timestamp"] = _iso_from_epoch(now)
                snapshot["q15_v9_5_seconds_remaining"] = seconds_left
                snapshot["q15_v9_5_stability"] = stability
                snapshot["q15_v9_5_expired"] = expired
                canonical = canonicals.get(asset)
                # prediction_available already implies core_valid, but spot can
                # still come from a thin public-composite fallback. An optional,
                # default-OFF floor keeps marginal-quality snapshots out of the
                # learning corpus so calibration trains on cleaner data.
                min_record_dq = _env_float("Q15_V95_MIN_RECORD_DATA_QUALITY", 0.0, 0.0, 1.0)
                record_ok = (
                    canonical is not None
                    and analysis.get("prediction_available")
                    and canonical.ticker
                    and float(analysis.get("data_quality") or 0.0) >= min_record_dq
                )
                if record_ok:
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
                        confidence_grade=analysis.get("confidence_grade"),
                        manipulation_suspected=bool((analysis.get("manipulation") or {}).get("suspected")),
                        manipulation_reason=(",".join((analysis.get("manipulation") or {}).get("reasons") or []) or None),
                        flip_risk_score=(analysis.get("flip_risk") or {}).get("score"),
                        flip_risk_confidence=(analysis.get("flip_risk") or {}).get("confidence"),
                        flip_evidence_count=(analysis.get("flip_risk") or {}).get("evidence_count"),
                    )
                    analysis["prediction_id"] = prediction_id
                    analysis["new_unique_prediction_recorded"] = inserted
                    # Flag (without mutating the graded prediction) when the live
                    # side drifts from the locked one before close — the stability
                    # / change-rate metric per interval.
                    self.ledger.note_prediction_revision(
                        ticker=canonical.ticker, checkpoint=checkpoint,
                        current_side=str(analysis["prediction_side"]),
                    )
                    # Flip-risk overlay: learned threshold, flip-probability, alert
                    # state machine + dashboard, and confirmed-flip detection. Sends
                    # are gated (dormant until a learned threshold exists); CONFIRMED
                    # flips are factual and send regardless. Never changes the call.
                    _fsent, _ffailed = self._process_flip_risk(
                        snapshot, asset, checkpoint, canonical.ticker, analysis,
                        flip_learned, notifier, now,
                    )
                    flip_sent += _fsent
                    flip_failed += _ffailed

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
                # Score fired flip warnings against whether the prediction flipped.
                if _env_bool("Q15_V95_FLIP_RISK_TRACKING", True):
                    self.ledger.reconcile_flip_warnings()
            ledger_status = self.ledger.status()
            # The recommended BEST ENTRY is rank #1 of the qualifying entries — the
            # single source of truth shared by the alert's top summary and detail.
            best_entry = _best_entry(analyses, ranking)
            top_entry_ticker = top_entry_close = top_entry_asset = top_entry_side = None
            if best_entry is not None:
                _be_asset, _be_a = best_entry
                top_entry_asset = _be_asset
                top_entry_side = str(_be_a.get("prediction_side") or "")
                _can = canonicals.get(_be_asset)
                if _can is not None and _can.ticker:
                    top_entry_ticker = _can.ticker
                    top_entry_close = _can.settlement_time
            followup_remaining = not (
                top_entry_ticker is not None
                and self.ledger.followup_already_sent(top_entry_ticker, checkpoint)
            )
            sent = failed = 0
            # COMPACT PANEL (default ON): one forward-looking V9.5 CHECK panel for
            # the top-ranked pick every checkpoint, with the immutable official
            # record written from the delivered Telegram message_id. The legacy
            # multi-asset entry-only alert is preserved under the flag for rollback.
            if _env_bool("Q15_V95_COMPACT_PANEL", True):
                deferred.suppress_all(generated_message=bool(ranking))
                sent, failed = self._send_compact_panel(
                    checkpoint, analyses, ranking, canonicals, parent_output, notifier, now,
                )
            else:
                message = build_v95_message(
                    checkpoint, analyses, ranking, ledger_status, result_events,
                    followup_remaining=followup_remaining,
                ) if ranking else None
                # Discard all parent V9.4 messages. V9.5 owns the final state machine.
                deferred.suppress_all(generated_message=message is not None)
                # Validation guard: never ship an alert whose top BEST ENTRY disagrees
                # with rank #1 of the detailed ranking.
                consistent = _best_entry_consistent(analyses, ranking)
                if not consistent:
                    logger.error("V9.5 best-entry mismatch — suppressing alert (top != detail rank #1)")
                # Entry-only delivery: when nothing qualifies as a recommended entry,
                # do not send the checkpoint alert at all. Flip / follow-up alerts are
                # separate and unaffected.
                entry_only = _env_bool("Q15_V95_SEND_ONLY_ON_ENTRY", True)
                no_entry_muted = entry_only and best_entry is None
                # One active prediction per timeframe: if a different, still-open
                # contract already holds this checkpoint's slot, do not push a second
                # prediction for the same time frame — leave the active one untouched.
                one_active = _env_bool("Q15_V95_ONE_ACTIVE_PER_TIMEFRAME", True)
                slot_locked = bool(
                    one_active and top_entry_ticker is not None
                    and self.ledger.pushed_slot_blocks(checkpoint, top_entry_ticker, now)
                )
                if message and consistent and not slot_locked and not no_entry_muted and self._decision_settled(checkpoint, analyses, ranking, parent_output, now):
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
                        # On a delivered entry recommendation, claim the timeframe slot,
                        # mark the prediction pushed (separate pushed accuracy), and arm
                        # the one follow-up check for this contract+interval.
                        if sent > 0 and top_entry_ticker is not None:
                            self.ledger.claim_pushed_slot(checkpoint, top_entry_ticker, top_entry_close, now)
                            self.ledger.mark_pushed(top_entry_ticker, checkpoint)
                            if (_env_bool("Q15_V95_ENTRY_FOLLOWUP_ENABLED", True)
                                    and checkpoint in _followup_checkpoints()):
                                self.ledger.arm_entry_followup(
                                    ticker=top_entry_ticker, checkpoint=checkpoint,
                                    asset=str(top_entry_asset or ""), side=str(top_entry_side or ""),
                                    now=now, delay=_env_float("Q15_V95_FOLLOWUP_DELAY_SECONDS", 120.0, 15.0, 600.0),
                                )
                    else:
                        self._telegram_suppressed_v95 += 1
                elif slot_locked or not consistent or no_entry_muted:
                    self._telegram_suppressed_v95 += 1
            # Manipulation alerts: only AFTER the normal check above was delivered,
            # only on high-probability findings that change its recommendation, and
            # combined into one concise alert. Detection ran all cycle regardless.
            ma_sent, ma_failed = self._dispatch_manipulation_alerts(checkpoint, notifier, now)
            flip_sent += ma_sent
            flip_failed += ma_failed
            # End-of-cycle recap: one close-out per contract that just settled.
            rc_sent, rc_failed = self._send_cycle_recaps(result_events, notifier, now)
            sent += rc_sent
            failed += rc_failed
            # Fire any due follow-up checks (exactly one per contract+interval).
            fu_sent, fu_failed = self._dispatch_entry_followups(canonicals, analyses, notifier, now)
            flip_sent += fu_sent
            flip_failed += fu_failed
            self._telegram_sent_v95 += sent + flip_sent
            self._telegram_failed_v95 += failed + flip_failed
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

    def _decision_settled(self, checkpoint: str, analyses: Mapping[str, Mapping[str, Any]],
                          ranking: Sequence[Mapping[str, Any]], snapshots: Mapping[str, Any],
                          now: float) -> bool:
        """Decide whether to emit the checkpoint alert this cycle.

        Two gates, so each alert lands once and on time:
          * **On the mark** — held until the clock reaches the checkpoint's named
            minute (15:00 / 10:00 / 7:00 remaining), so the "10M" alert fires at
            the 10-minute mark rather than at band entry (~11:00). Disable via
            ``Q15_V95_FIRE_AT_CHECKPOINT_MARK=false``.
          * **Made up its mind** — the top-3 (asset/side/entry) signature must
            repeat for ``Q15_V95_DECISION_STABILITY_CYCLES`` cycles, so leader/
            edge jitter no longer fires early.
        A fallback forces a single send as the band closes
        (``Q15_V95_DECISION_FORCE_MARGIN_SECONDS``) so a verdict that never fully
        settles, or a window first seen late, still yields exactly one alert.
        No-op (always settled) when single-alert gating is disabled.
        """
        if not _env_bool("Q15_V95_SINGLE_ALERT_PER_CHECKPOINT", True):
            return True
        window_id = int(now // 900)
        key = (checkpoint, window_id)
        signature = _decision_signature(analyses, ranking)
        entry = self._decision_stability.get(key)
        if entry and entry.get("signature") == signature:
            entry["count"] += 1
        else:
            entry = {"signature": signature, "count": 1}
            self._decision_stability[key] = entry
        # Keep the tracker bounded — drop windows more than two markets old.
        if len(self._decision_stability) > 32:
            for stale in [k for k in self._decision_stability if k[1] < window_id - 2]:
                self._decision_stability.pop(stale, None)
        required = int(_env_float("Q15_V95_DECISION_STABILITY_CYCLES", 3.0, 1.0, 120.0))
        stable = entry["count"] >= required

        times = [_seconds_remaining(s, now) for s in snapshots.values() if isinstance(s, Mapping)]
        times = [t for t in times if t is not None]
        if not times:
            return stable  # no clock available -> stability only
        seconds_left = max(times)
        # Auto-expire: once the clock passes this interval's end, stop alerting —
        # a 7M alert must not stay live until market close.
        if _checkpoint_expired(checkpoint, seconds_left):
            return False
        # Safety net: never let the band close without one alert.
        band_lower = _CHECKPOINT_BAND_LOWER.get(checkpoint, 0.0)
        force_margin = _env_float("Q15_V95_DECISION_FORCE_MARGIN_SECONDS", 60.0, 0.0, 300.0)
        if seconds_left <= band_lower + force_margin:
            return True
        # Hold until the clock reaches the named checkpoint minute.
        if _env_bool("Q15_V95_FIRE_AT_CHECKPOINT_MARK", True):
            target = _CHECKPOINT_TARGET_SECONDS.get(checkpoint)
            tol = _env_float("Q15_V95_CHECKPOINT_MARK_TOLERANCE_SECONDS", 15.0, 0.0, 120.0)
            if target is not None and seconds_left > target + tol:
                return False
        return stable

    def _stability_marker(self, asset: str, checkpoint: str, analysis: Mapping[str, Any], now: float) -> str | None:
        """Tag a prediction stable / strengthening / weakening / changed by
        comparing this cycle's side + confidence to the prior cycle's (per asset,
        checkpoint, market window). Returns None when no prediction is available.
        """
        if not analysis.get("prediction_available"):
            return None
        side = analysis.get("prediction_side")
        prob = _num(analysis.get("selected_probability"))
        if side is None or prob is None:
            return None
        window_id = int(now // 900)
        key = (str(asset), str(checkpoint), window_id)
        prev = self._prediction_trend.get(key)
        eps = _env_float("Q15_V95_TREND_EPSILON", 0.01, 0.0, 0.5)
        if prev is None:
            marker = "stable"
        elif prev.get("side") != side:
            marker = "changed"
        else:
            delta = float(prob) - float(prev.get("prob") or prob)
            marker = "strengthening" if delta > eps else "weakening" if delta < -eps else "stable"
        self._prediction_trend[key] = {"side": side, "prob": float(prob)}
        if len(self._prediction_trend) > 128:
            for stale in [k for k in self._prediction_trend if k[2] < window_id - 2]:
                self._prediction_trend.pop(stale, None)
        return marker

    def _send_compact_panel(self, checkpoint: str, analyses: Mapping[str, Any],
                            ranking: Sequence[Mapping[str, Any]], canonicals: Mapping[str, Any],
                            parent_output: Mapping[str, Any], notifier: Any,
                            now: float) -> tuple[int, int]:
        """Send the forward-looking compact panel for the top-ranked pick, once per
        checkpoint+window, and write the immutable OFFICIAL record from the
        delivered Telegram message_id.

        Records written on a real delivery (message_id present, sent before close):
          * 'interval' — the YES/NO call for this checkpoint (always);
          * 'entry'    — when the pick is an ENTRY_RECOMMENDED (also fires the
                         existing slot/pushed/follow-up accounting);
          * 'manipulation' — the gradable 'direction after' when a watch is flagged.
        A muted send returns no message_id, so it is handled (no retry) but never
        enters the official record. Returns (sent, failed)."""
        if not ranking:
            return 0, 0
        if not self._decision_settled(checkpoint, analyses, ranking, parent_output, now):
            return 0, 0
        top = ranking[0]
        asset = str(top.get("asset"))
        analysis = analyses.get(asset) or {}
        canon = canonicals.get(asset)
        ticker = (canon.ticker if canon is not None else analysis.get("ticker"))
        close_time = (canon.settlement_time if canon is not None else None)

        event_key, desired_state, fingerprint = _notification_identity(checkpoint, analyses, ranking, now)
        previous = self.ledger.notification_state(event_key)
        state = "ENTRY_WITHDRAWN" if previous == "ENTRY_RECOMMENDED" and desired_state != "ENTRY_RECOMMENDED" else desired_state
        permit_key = self.ledger.reserve_notification(
            event_key=event_key, checkpoint=checkpoint, state=state, fingerprint=fingerprint, now=now,
        )
        if not permit_key:
            # Could be an intentional dedup (same event already claimed this
            # window) or a transient ledger/store hiccup. Either way the send is
            # dropped this cycle; surface it (throttled) so a silent ledger
            # outage is visible instead of vanishing.
            self._telegram_suppressed_v95 += 1
            self._throttled_warn(
                f"reserve_notification:{event_key}",
                "v95 checkpoint alert not sent: reserve_notification returned no "
                "permit for event_key=%s checkpoint=%s state=%s (dedup or ledger "
                "unavailable; will retry next cycle)", event_key, checkpoint, state,
                now=now,
            )
            return 0, 0

        prior_cp = _PRIOR_CHECKPOINT.get(str(checkpoint).upper())
        prior_side = None
        if ticker and prior_cp:
            prior_side = (self.ledger.frozen_prediction(str(ticker), prior_cp) or {}).get("side")
        message = build_compact_checkpoint_panel(checkpoint, asset, analysis, prior_side)

        if hasattr(notifier, "send_with_result"):
            result = notifier.send_with_result(message)
        else:  # legacy/bare notifier: treat a truthy send as handled, no message_id
            ok = bool(notifier.send(message)) if notifier is not None else False
            result = {"ok": ok, "delivered": ok and getattr(notifier, "last_message_id", None) is not None,
                      "message_id": getattr(notifier, "last_message_id", None)}
        handled = bool(result.get("ok"))
        delivered = bool(result.get("delivered"))
        self.ledger.complete_notification(event_key=permit_key, success=handled, now=now)

        # Handled-but-not-delivered = the send was accepted yet produced no
        # message_id (muted, or a notifier that reports success without one).
        # No official record is written, so make the gap visible (throttled)
        # rather than letting an undelivered alert pass silently.
        if handled and not delivered:
            self._throttled_warn(
                f"handled_not_delivered:{checkpoint}",
                "v95 checkpoint alert handled but not delivered (no message_id) for "
                "asset=%s checkpoint=%s ticker=%s — no official record written",
                asset, checkpoint, ticker, now=now,
            )

        # Record what the normal/base check for this interval recommended and whether
        # it actually reached the owner. A manipulation alert is only allowed AFTER a
        # delivered normal check (gate condition 2); a muted/failed send leaves
        # delivered=False so no manipulation alert rides ahead of it.
        self._normal_check[str(checkpoint)] = manipulation_alert.NormalCheck(
            checkpoint=str(checkpoint), delivered=delivered, asset=asset,
            side=analysis.get("prediction_side"),
            action=str(analysis.get("trade_decision") or "") or None, at=now,
        )

        if delivered and ticker:
            mid = result.get("message_id")
            decision = str(analysis.get("trade_decision") or "")
            manip_prob = (analysis.get("flip_risk") or {}).get("score")
            self.ledger.record_sent_prediction(
                contract_id=str(ticker), asset=asset, interval=checkpoint, record_type="interval",
                predicted_side=analysis.get("prediction_side"), probability=analysis.get("yes_probability"),
                manipulation_probability=manip_prob, entry_decision=decision,
                sent_at=now, close_time=close_time, message_id=mid,
            )
            if decision == "ENTRY_RECOMMENDED":
                self.ledger.record_sent_prediction(
                    contract_id=str(ticker), asset=asset, interval=checkpoint, record_type="entry",
                    predicted_side=analysis.get("prediction_side"), probability=analysis.get("yes_probability"),
                    manipulation_probability=manip_prob, entry_decision=decision,
                    sent_at=now, close_time=close_time, message_id=mid,
                )
                self.ledger.claim_pushed_slot(checkpoint, str(ticker), close_time, now)
                self.ledger.mark_pushed(str(ticker), checkpoint)
                if (_env_bool("Q15_V95_ENTRY_FOLLOWUP_ENABLED", True)
                        and checkpoint in _followup_checkpoints()):
                    self.ledger.arm_entry_followup(
                        ticker=str(ticker), checkpoint=checkpoint, asset=asset,
                        side=str(analysis.get("prediction_side") or ""), now=now,
                        delay=_env_float("Q15_V95_FOLLOWUP_DELAY_SECONDS", 120.0, 15.0, 600.0),
                    )
            manip_side = _direction_after_side(analysis)
            if _panel_manipulation(analysis) and manip_side:
                self.ledger.record_sent_prediction(
                    contract_id=str(ticker), asset=asset, interval=checkpoint, record_type="manipulation",
                    predicted_side=manip_side, probability=analysis.get("yes_probability"),
                    manipulation_probability=manip_prob, entry_decision=decision,
                    sent_at=now, close_time=close_time, message_id=mid,
                )
        return (1 if delivered else 0), (0 if handled else 1)

    @staticmethod
    def _recap_close_label(close_time: Any) -> str:
        try:
            if close_time is None:
                return ""
            return datetime.fromtimestamp(float(close_time), tz=timezone.utc).strftime("%H:%M")
        except (TypeError, ValueError, OSError, OverflowError):
            return ""

    def _send_cycle_recaps(self, result_events: Sequence[Mapping[str, Any]],
                           notifier: Any, now: float) -> tuple[int, int]:
        """Fire the single END-OF-CYCLE recap for each contract that just settled.
        Deduped per ticker via a ``recap:<ticker>`` reservation; built only from
        what was officially delivered for that contract. Returns (sent, failed)."""
        if not _env_bool("Q15_V95_CYCLE_RECAP", True) or not result_events:
            return 0, 0
        sent = failed = 0
        seen: set[str] = set()
        for ev in result_events:
            ticker = str((ev or {}).get("ticker") or "")
            if not ticker or ticker in seen:
                continue
            seen.add(ticker)
            permit = self.ledger.reserve_notification(
                event_key=f"recap:{ticker}", checkpoint="RECAP",
                state="RESULT_RESOLVED", fingerprint=ticker, now=now,
            )
            if not permit:
                continue
            recap = self.ledger.contract_recap(ticker)
            if recap is None or not recap.get("intervals"):
                # Nothing was officially sent for this contract -> no recap; mark
                # handled so it is not retried every cycle.
                self.ledger.complete_notification(event_key=permit, success=True, now=now)
                continue
            message = panels_v95.build_cycle_recap(
                asset=recap["asset"], close_label=self._recap_close_label(recap.get("close_time")),
                result=recap["result"], intervals=recap["intervals"], flips=recap.get("flips"),
                entry_result=recap.get("entry"), manipulation_result=recap.get("manipulation"),
                official=self.ledger.official_scoreboard(),
            )
            if hasattr(notifier, "send_with_result"):
                res = notifier.send_with_result(message)
            else:
                ok = bool(notifier.send(message)) if notifier is not None else False
                res = {"ok": ok, "delivered": ok}
            self.ledger.complete_notification(event_key=permit, success=bool(res.get("ok")), now=now)
            if res.get("delivered"):
                sent += 1
            elif not res.get("ok"):
                failed += 1
        return sent, failed

    def _process_flip_risk(self, snapshot: MutableMapping[str, Any], asset: str, checkpoint: str,
                           ticker: str, analysis: Mapping[str, Any], flip_learned: Mapping[str, Any],
                           notifier: Any, now: float) -> tuple[int, int]:
        """Resolve threshold + flip-probability, run the alert state machine, stamp
        the dashboard block, and send HIGH FLIP RISK / CONFIRMED PREDICTION FLIP.

        Read-only: never touches the prediction. HIGH FLIP RISK is gated to fire
        only once a *learned* threshold exists (dormant-until-learned posture);
        CONFIRMED flips are factual and send regardless. Returns (sent, failed)."""
        ra = analysis.get("flip_risk_obj")
        if ra is None:
            return 0, 0
        side = str(analysis.get("prediction_side") or "").upper()
        direction = ra.direction_monitored or ""
        cp_dir = {}
        if flip_learned.get("available"):
            cp_dir = (flip_learned.get("by_checkpoint", {}).get(checkpoint, {}) or {}).get(direction, {}) or {}
        asset_stats = (cp_dir.get("by_asset", {}) or {}).get(asset)
        overall_stats = cp_dir.get("overall")
        threshold = flip_risk.resolve_threshold(
            asset_stats, overall_stats, asset=asset, checkpoint=checkpoint, direction=direction,
        )
        scope = asset_stats if (asset_stats and threshold.status == "Learned") else overall_stats
        buckets = (scope or {}).get("buckets")
        flip_prob = flip_risk.estimate_flip_probability(ra.score, buckets)
        # Reliability-aware "≥X% chance of being right" estimate for this risk level
        # (Wilson lower bound of the bucket flip-rate) + its sample size. The gate
        # keeps flips dormant until this clears Q15_V95_FLIP_MIN_HITRATE.
        flip_prob_lower, flip_samples = flip_risk.bucket_flip_reliability(ra.score, buckets)

        key = (str(asset), str(checkpoint), str(ticker))
        decision, new_state = flip_risk.evaluate_alert(
            risk=ra, threshold=threshold, flip_probability=flip_prob,
            prior=self._flip_alert_state.get(key), now=now, flip_prob_lower=flip_prob_lower,
        )
        self._flip_alert_state[key] = new_state
        if len(self._flip_alert_state) > 256:
            for k in list(self._flip_alert_state)[:64]:
                self._flip_alert_state.pop(k, None)

        snapshot["q15_v9_5_flip_threshold"] = threshold.threshold
        snapshot["q15_v9_5_flip_threshold_source"] = threshold.source
        snapshot["q15_v9_5_flip_threshold_status"] = threshold.status
        snapshot["q15_v9_5_flip_samples"] = threshold.samples
        snapshot["q15_v9_5_flip_probability"] = flip_prob
        snapshot["q15_v9_5_flip_hitrate_lower"] = flip_prob_lower
        snapshot["q15_v9_5_flip_hitrate_samples"] = flip_samples
        snapshot["q15_v9_5_flip_state"] = decision.state
        snapshot["q15_v9_5_flip_dashboard"] = flip_risk.dashboard_block(
            risk=ra, threshold=threshold, flip_probability=flip_prob,
            state=decision.state, persistence=decision.persistence,
            flip_prob_lower=flip_prob_lower, flip_samples=flip_samples,
        )

        # Gated manipulation-alert candidate (read-only): detection always runs;
        # the actual SEND is decided after the normal check, by the policy gate in
        # _dispatch_manipulation_alerts. We collect a candidate only when the risk
        # is at/above its learned threshold with confirming evidence; the
        # probability + confidence + "recommendation changed" + dedup gates are all
        # applied later so this never sends on its own.
        if (_env_bool("Q15_V95_MANIPULATION_ALERTS_ENABLED", True)
                and side in ("YES", "NO")
                and ra.score >= threshold.threshold
                and len(ra.evidence_categories) >= 1):
            new_side = "NO" if side == "YES" else "YES"
            orig_action = str(analysis.get("trade_decision") or "") or None
            new_action = ("stand down — do not enter (manipulation flip risk)"
                          if orig_action == "ENTRY_RECOMMENDED"
                          else f"outcome may settle {new_side}")
            self._manip_candidates.append(manipulation_alert.ManipCandidate(
                asset=str(asset), checkpoint=str(checkpoint), ticker=str(ticker),
                probability=flip_prob_lower, confidence=ra.confidence,
                evidence=[flip_risk.CATEGORY_LABELS.get(c, c) for c in ra.evidence_categories],
                original_side=side, original_action=orig_action,
                new_side=new_side, new_action=new_action,
            ))

        sent = failed = 0
        # Confirmed flip: a later frozen checkpoint side differs from the earlier
        # one for this contract — factual. Owner removed this Telegram alert UI, so
        # delivery is OFF by default; flip-risk is still tracked + on the dashboard.
        # Re-enable with Q15_V95_FLIP_CONFIRMED_ALERTS=true.
        prior_cp = {"10M": "15M", "7M": "10M"}.get(checkpoint)
        if prior_cp and _env_bool("Q15_V95_FLIP_CONFIRMED_ALERTS", False):
            prev = self.ledger.frozen_prediction(str(ticker), prior_cp) or {}
            cur = self.ledger.frozen_prediction(str(ticker), checkpoint) or {}
            prev_side, cur_side = prev.get("side"), cur.get("side")
            dkey = (str(ticker), prior_cp, str(checkpoint))
            if (prev_side in ("YES", "NO") and cur_side in ("YES", "NO") and prev_side != cur_side
                    and dkey not in self._flip_confirmed_sent):
                self._flip_confirmed_sent.add(dkey)
                msg = flip_risk.format_confirmed_flip(
                    asset=asset, checkpoint=checkpoint, previous_side=prev_side, new_side=cur_side,
                    risk_before=prev.get("flip_risk_score"), flip_prob_before=None,
                    evidence=(analysis.get("flip_risk") or {}).get("evidence_categories") or [],
                )
                s, f, _ = _BufferedNotifier(notifier).flush(msg)
                sent += s
                failed += f

        # HIGH FLIP RISK: owner removed this Telegram alert UI, so delivery is OFF
        # by default (re-enable with Q15_V95_FLIP_ALERTS_ENABLED=true). When on, it
        # stays dormant until a learned threshold exists.
        require_learned = _env_bool("Q15_V95_FLIP_ALERTS_REQUIRE_LEARNED", True)
        if (decision.should_send and _env_bool("Q15_V95_FLIP_ALERTS_ENABLED", False)
                and (threshold.status == "Learned" or not require_learned)):
            msg = flip_risk.format_high_flip_risk(
                asset=asset, checkpoint=checkpoint, current_side=side, risk=ra,
                threshold=threshold, flip_probability=flip_prob, persistence=decision.persistence,
                flip_prob_lower=flip_prob_lower, flip_samples=flip_samples,
            )
            s, f, _ = _BufferedNotifier(notifier).flush(msg)
            sent += s
            failed += f
            if s:
                self.ledger.record_flip_warning(
                    asset=asset, checkpoint=checkpoint, ticker=str(ticker), direction=direction,
                    risk_score=ra.score, flip_probability=flip_prob, confidence=ra.confidence, now=now,
                )
        return sent, failed

    def _dispatch_manipulation_alerts(self, checkpoint: str, notifier: Any,
                                      now: float) -> tuple[int, int]:
        """Apply the manipulation-alert policy and send at most ONE combined alert
        for the interval. Detection already ran (candidates were collected this
        cycle in _process_flip_risk); here we only decide what is pushed:

          1. high probability the manipulation actually occurs;
          2. the interval's normal check was already delivered;
          3. the finding recommends a DIFFERENT side / action than the normal check.

        Repetitive / unchanged / low-probability findings are dropped; everything
        that qualifies is combined into a single concise alert. Returns (sent,
        failed)."""
        candidates = list(getattr(self, "_manip_candidates", []))
        if not candidates or not _env_bool("Q15_V95_MANIPULATION_ALERTS_ENABLED", True):
            return 0, 0
        normal = self._normal_check.get(str(checkpoint))
        min_prob = _env_float("Q15_V95_MANIPULATION_ALERT_MIN_PROBABILITY", 0.70, 0.0, 1.0)
        min_conf = _env_float("Q15_V95_MANIPULATION_ALERT_MIN_CONFIDENCE", 40.0, 0.0, 100.0)
        qualifying = []
        for cand in candidates:
            if str(cand.checkpoint) != str(checkpoint):
                continue
            ok, reason = manipulation_alert.qualifies(
                cand, normal, min_probability=min_prob, min_confidence=min_conf,
                already_sent=self._manip_alert_sent,
            )
            if ok:
                qualifying.append(cand)
            else:
                logger.debug("manipulation alert gated for %s %s: %s", cand.asset, checkpoint, reason)
        if not qualifying:
            return 0, 0
        message = manipulation_alert.build_combined_alert(str(checkpoint), normal, qualifying)
        s, f, _ = _BufferedNotifier(notifier).flush(message)
        if s:
            for cand in qualifying:
                self._manip_alert_sent.add(
                    (cand.ticker, cand.checkpoint, manipulation_alert.ManipCandidate._norm(cand.new_side)))
            if len(self._manip_alert_sent) > 512:
                for k in list(self._manip_alert_sent)[:128]:
                    self._manip_alert_sent.discard(k)
        return s, f

    def _dispatch_entry_followups(self, canonicals: Mapping[str, Any], analyses: Mapping[str, Any],
                                  notifier: Any, now: float) -> tuple[int, int]:
        """Fire the single due follow-up per (contract, interval).

        Read-only re-check of a previously recommended entry: confirms whether it
        is still valid, whether the side changed, and advises hold / take-profit /
        avoid / exit. Each follow-up fires at most once and is then consumed, so no
        repeats. If the contract is no longer live this cycle the (now pointless)
        follow-up is consumed without sending. Returns (sent, failed)."""
        if not _env_bool("Q15_V95_ENTRY_FOLLOWUP_ENABLED", True):
            return 0, 0
        due = self.ledger.due_followups(now)
        if not due:
            return 0, 0
        by_ticker: dict[str, tuple[str, Mapping[str, Any]]] = {}
        for asset, can in (canonicals or {}).items():
            tk = getattr(can, "ticker", None)
            if can is not None and tk and asset in analyses:
                by_ticker[str(tk)] = (asset, analyses[asset])
        sent = failed = 0
        for f in due:
            ticker, cp = f["ticker"], f["checkpoint"]
            cur = by_ticker.get(ticker)
            if cur is None:
                self.ledger.mark_followup_sent(ticker, cp, now)  # contract gone; consume
                continue
            asset, a = cur
            msg = build_followup_message(cp, f.get("asset") or asset, f.get("side") or "", a)
            s, fl, _ = _BufferedNotifier(notifier).flush(msg)
            sent += s
            failed += fl
            self.ledger.mark_followup_sent(ticker, cp, now)
        return sent, failed

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

    def accuracy_report(self) -> dict[str, Any]:
        """Honest accuracy / promotion-readiness readout over the ledger metrics."""
        from .accuracy_report import build_accuracy_report
        return {"version": VERSION, "read_only": True, **build_accuracy_report(self.ledger.metrics())}

    def accuracy_summary(self) -> dict[str, Any]:
        """Compact one-glance accuracy headline for /api/health."""
        from .accuracy_report import build_accuracy_report, compact_summary
        return compact_summary(build_accuracy_report(self.ledger.metrics()))

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
            "feature_profile": feature_profile_health(),
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
    # V9.5 CHECK panels and the CYCLE CLOSED recap render their own clean layout;
    # never re-render them through the legacy reformatter chain.
    if "V9.5 CHECK" in message or "CYCLE CLOSED" in message:
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
    # Old-UI guard: by default the legacy v94 reformatter is disabled so no
    # message is ever re-rendered in the old layout — non-Q15 notifications (dip,
    # scalp, exit, etc.) pass through in the clean form their own module built.
    # Set Q15_V95_LEGACY_FALLBACK_FORMAT=true to restore the legacy reformatter.
    if _env_bool("Q15_V95_LEGACY_FALLBACK_FORMAT", False):
        return _format_v94_message(message)
    return message


__all__ = [
    "CanonicalSnapshot", "CheckpointPolicyV95", "FEATURE_SCHEMA_VERSION", "MODEL_VERSION",
    "READ_ONLY", "VERSION", "analyse_v95", "apply_v95_policy", "build_canonical_snapshot",
    "build_v95_message", "format_telegram_message", "rank_analyses",
]
