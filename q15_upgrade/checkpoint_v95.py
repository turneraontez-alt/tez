"""Q15 V9.5 canonical snapshot, calibrated ensemble, and champion/challenger policy.

This release is deliberately read-only.  It produces a directional probability
whenever core data is valid, then evaluates whether the current Kalshi quote is
actually executable and attractive.  Production coefficients are frozen; only
a bounded shadow challenger learns from unique, officially settled 15-minute
predictions.
"""
from __future__ import annotations

import copy
import atexit
import hashlib
import json
import logging
import math
import os
import statistics
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
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
from . import flip_decision
from . import flip_risk
from . import shadow_factors as cross_asset
from . import shadow_economics
from . import shadow_signals
from .money import clamp_price_cents, round_edge_cents
from notifications import manipulation_alert
from notifications import panels_v95
from .fast_candles import fast_canonical_candles
from .ledger_v95 import (
    CHAMPION_WEIGHTS,
    FEATURE_SCHEMA_VERSION,
    MODEL_VERSION,
    V95Ledger,
)
from .market_data_v95 import PublicMarketDataHub

VERSION = "q15-v9.5.2-runtime-activation-data-bridge-v1"
_HEALTH_SUMMARY_CACHE_TTL_SECONDS = 15.0
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


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
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


def _shadow_vol_per_min(volatility: Mapping[str, Any] | None) -> float | None:
    """Per-minute fractional volatility for the shadow challenger's features.

    ``_robust_volatility`` reports sigma per sqrt-second; volatility scales with
    sqrt-time, so per-minute sigma = sigma_per_sqrt_second * sqrt(60). Returns
    None when unavailable so the challenger falls back to its own estimate.
    """
    if not isinstance(volatility, Mapping):
        return None
    sigma = volatility.get("sigma_per_sqrt_second")
    try:
        sigma = float(sigma)
    except (TypeError, ValueError):
        return None
    return sigma * math.sqrt(60.0) if sigma > 0 else None


def _window_return_with_cadence(
    candles: Sequence[Mapping[str, float]],
    seconds: float,
    cadence: float,
) -> float | None:
    if len(candles) < 2:
        return None
    count = max(2, int(round(seconds / cadence)) + 1)
    subset = candles[-min(len(candles), count):]
    start = _num(subset[0].get("close"))
    end = _num(subset[-1].get("close"))
    if start is None or end is None or start <= 0 or end <= 0:
        return None
    return math.log(end / start)


def _multi_horizon_returns(
    canonical: CanonicalSnapshot,
    cadence_seconds: float | None = None,
) -> dict[str, float | None]:
    # Coordinate contract: `_window_return` yields LOG returns (candle space); the
    # public feed quotes SIMPLE fractional returns (e.g. 0.012 == +1.2%), which we
    # lift into log space with log1p before blending. A value outside (-1, 1) can't
    # be a plausible short-horizon fractional return (it's likely percent-scaled or
    # already-log from a feed change) and would make log1p raise/-inf, so it is
    # dropped rather than trusted — the candle return then stands alone.
    try:
        cadence = float(cadence_seconds) if cadence_seconds is not None else _cadence(canonical.candles)
    except (TypeError, ValueError):
        cadence = _cadence(canonical.candles)
    if not math.isfinite(cadence) or cadence <= 0:
        cadence = _cadence(canonical.candles)
    result = {
        f"return_{seconds}s": _window_return_with_cadence(canonical.candles, float(seconds), cadence)
        for seconds in (5, 15, 30, 60, 180, 900, 1800)
    }
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
    aliases = (
        ("yes_ask_size", "yes_ask_qty", "yes_offer_size",
         "yes_depth_at_ask", "yes_ask_depth_contracts")
        if side == "YES"
        else ("no_ask_size", "no_ask_qty", "no_offer_size",
              "no_depth_at_ask", "no_ask_depth_contracts")
    )
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


def _spot_depth_quote_fields(snapshot: Mapping[str, Any], observed_at: float) -> dict[str, Any]:
    """Record-only actual-coin depth context, aligned to the Kalshi decision row.

    ``spot_depth`` is produced by the optional public exchange collector. These
    fields are never used by the live gate here; they simply make later research
    easy: each V2/HVF row can compare Kalshi contract depth against actual-coin
    book imbalance and trade pressure at the same decision time.
    """
    depth = snapshot.get("spot_depth")
    status = snapshot.get("spot_depth_status")
    missing_reason = snapshot.get("spot_depth_missing_reason")
    base = {
        "spot_depth_status": status or ("ok" if isinstance(depth, Mapping) else "missing"),
        "spot_depth_missing_reason": missing_reason,
    }
    if not isinstance(depth, Mapping):
        return base

    def val(key: str) -> Any:
        return depth.get(key)

    def net_notional(suffix: str) -> float | None:
        buy = _num(depth.get(f"trade_buy_notional_{suffix}"))
        sell = _num(depth.get(f"trade_sell_notional_{suffix}"))
        if buy is None or sell is None:
            return None
        return buy - sell

    created = _num(depth.get("created_at"))
    snapshot_age = max(0.0, observed_at - created) if created is not None else None
    book_age = _num(depth.get("book_age_seconds"))
    trade_age = _num(depth.get("trade_age_seconds"))
    return {
        **base,
        "spot_depth_status": "ok",
        "spot_depth_missing_reason": None,
        "spot_depth_source": val("source"),
        "spot_depth_age_seconds": (
            (snapshot_age or 0.0) + book_age if book_age is not None else snapshot_age
        ),
        "spot_depth_trade_age_seconds": (
            (snapshot_age or 0.0) + trade_age if trade_age is not None else None
        ),
        "spot_depth_best_bid": val("best_bid"),
        "spot_depth_best_ask": val("best_ask"),
        "spot_depth_mid": val("mid"),
        "spot_depth_spread_bps": val("spread_bps"),
        "spot_depth_bid_depth_top": val("bid_depth_top"),
        "spot_depth_ask_depth_top": val("ask_depth_top"),
        "spot_depth_bid_depth_levels": val("bid_depth_levels"),
        "spot_depth_ask_depth_levels": val("ask_depth_levels"),
        "spot_depth_bid_notional_levels": val("bid_notional_levels"),
        "spot_depth_ask_notional_levels": val("ask_notional_levels"),
        "spot_depth_imbalance": val("depth_imbalance"),
        "spot_depth_trade_buy_qty_5s": val("trade_buy_qty_5s"),
        "spot_depth_trade_sell_qty_5s": val("trade_sell_qty_5s"),
        "spot_depth_trade_net_qty_5s": val("trade_net_qty_5s"),
        "spot_depth_trade_buy_notional_5s": val("trade_buy_notional_5s"),
        "spot_depth_trade_sell_notional_5s": val("trade_sell_notional_5s"),
        "spot_depth_trade_net_notional_5s": net_notional("5s"),
        "spot_depth_trade_buy_qty_15s": val("trade_buy_qty_15s"),
        "spot_depth_trade_sell_qty_15s": val("trade_sell_qty_15s"),
        "spot_depth_trade_net_qty_15s": val("trade_net_qty_15s"),
        "spot_depth_trade_buy_notional_15s": val("trade_buy_notional_15s"),
        "spot_depth_trade_sell_notional_15s": val("trade_sell_notional_15s"),
        "spot_depth_trade_net_notional_15s": net_notional("15s"),
        "spot_depth_trade_buy_qty_60s": val("trade_buy_qty_60s"),
        "spot_depth_trade_sell_qty_60s": val("trade_sell_qty_60s"),
        "spot_depth_trade_net_qty_60s": val("trade_net_qty_60s"),
        "spot_depth_trade_buy_notional_60s": val("trade_buy_notional_60s"),
        "spot_depth_trade_sell_notional_60s": val("trade_sell_notional_60s"),
        "spot_depth_trade_net_notional_60s": net_notional("60s"),
        "spot_depth_last_trade_price": val("last_trade_price"),
        "spot_depth_last_trade_side": val("last_trade_side"),
        "spot_depth_last_trade_size": val("last_trade_size"),
    }


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


def _coinflip_confidence_shrink(calibrated_yes: float) -> tuple[float, dict[str, Any]]:
    """Near-coin-flip over-confidence guard (DEFAULT OFF — no live change).

    The settled record shows the champion is over-confident in the near-coin-flip
    band: calibrated P(YES) in ~[0.50,0.60) realises YES only ~27% of the time, so
    those leans carry no real directional edge. Outside the band the model is well
    calibrated — P(YES) >= 0.60 went 17/17 and the strong-NO calls went ~19/21 — so
    a fix must touch ONLY the coin-flip zone.

    When enabled (``Q15_V95_COINFLIP_SHRINK_STRENGTH`` > 0) this pulls the
    probability toward 0.5, strongest at the coin flip and tapering to zero at the
    edge of the band (``Q15_V95_COINFLIP_SHRINK_BAND``, default +/-0.10 around 0.5).
    Because ``strength`` and ``weight`` are both in [0,1] the result can never cross
    0.5, so the predicted SIDE never changes: every previously-correct pick keeps
    its side and its win/loss. The effect is purely a calibration / Brier
    improvement (less over-confident probabilities in the noise band).

    This mirrors the ``coinflip_fade`` shadow A/B signal. It stays OFF until that
    out-of-sample A/B confirms the lift, per the frozen-champion / manual-promotion
    policy. Returns ``(maybe_shrunk_yes, info)``.
    """
    strength = _env_float("Q15_V95_COINFLIP_SHRINK_STRENGTH", 0.0, 0.0, 1.0)
    if strength <= 0.0:
        return calibrated_yes, {"active": False}
    band = _env_float("Q15_V95_COINFLIP_SHRINK_BAND", 0.10, 0.01, 0.25)
    distance = abs(calibrated_yes - 0.5)
    weight = _clamp(1.0 - distance / band, 0.0, 1.0)  # 1 at the coin flip, 0 at/beyond the band edge
    shrunk = _clamp(calibrated_yes - strength * weight * (calibrated_yes - 0.5), 0.01, 0.99)
    return shrunk, {
        "active": True, "strength": round(strength, 3), "band": round(band, 3),
        "weight": round(weight, 3), "from": round(calibrated_yes, 4), "to": round(shrunk, 4),
    }


# --- Suspected price-manipulation tracking (read-only) -----------------------
# A composite "are large players pushing the price around?" suspicion, built
# ENTIRELY from tells the engine already computed this cycle. It never changes
# the prediction or the edge — it is observational only.
#
# Three independent tells near a binary strike, each contributes one "reason":
#   ABSORPTION — aggressive taker flow eaten by resting orders without price
#                moving (someone defending a level). The ONLY directional tell,
#                so it also yields a ``lean`` (the side a flip would go toward).
#   PIN        — price stapled to the strike with repeated crossings; the
#                outcome is unstable and prone to flip at settlement.
#   DIVERGENCE — one public venue (e.g. Coinbase vs OKX) pushed off the others'
#                consensus by at least the configured basis-point band.
MANIPULATION_TELL_ABSORPTION = "ABSORPTION"
MANIPULATION_TELL_PIN = "PIN"
MANIPULATION_TELL_DIVERGENCE = "DIVERGENCE"

# Regime names (mirrors the labels produced by ``_regime``) that imply a tell.
_REGIME_THRESHOLD_PIN = "THRESHOLD_PIN"
_REGIME_EXCHANGE_DIVERGENCE = "EXCHANGE_DIVERGENCE"

# Score model (all on a 0..1 "fraction of tells present" scale):
#   score = (#tells / TOTAL_TELLS) + ABSORPTION_BONUS (if absorption fired),
#           capped at SCORE_MAX and rounded to SCORE_DECIMALS.
# The absorption bonus weights the score toward the one directional flip tell.
_MANIPULATION_TELLS_TOTAL = 3  # ABSORPTION + PIN + DIVERGENCE
_MANIPULATION_ABSORPTION_SCORE_BONUS = 0.34
_MANIPULATION_SCORE_MAX = 1.0
_MANIPULATION_SCORE_DECIMALS = 4

# Default cross-venue divergence band, in basis points, at/above which the
# DIVERGENCE tell fires. Overridable globally or per asset (see below).
_MANIPULATION_DEFAULT_DIVERGENCE_BPS = 35.0

_MANIPULATION_REASON_PHRASES = {
    MANIPULATION_TELL_ABSORPTION: "order-wall absorption",
    MANIPULATION_TELL_PIN: "strike pin (outcome unstable)",
    MANIPULATION_TELL_DIVERGENCE: "cross-exchange divergence",
}

def _no_manipulation() -> dict[str, Any]:
    """A fresh 'nothing flagged' result (fresh ``reasons`` list each call)."""
    return {"suspected": False, "reasons": [], "lean": None, "score": 0.0}


def _manipulation_divergence_threshold_bps(asset: str | None) -> float:
    """Cross-venue divergence band (basis points) at/above which DIVERGENCE fires.

    Defaults to ``Q15_V95_MANIPULATION_DIVERGENCE_BPS`` (35 bps). A per-asset
    override ``Q15_V95_MANIPULATION_DIVERGENCE_BPS_<ASSET>`` lets noisier assets
    (e.g. DOGE) use a wider band without changing the global default; when the
    per-asset variable is unset the behaviour is identical to the global one.
    """
    global_default = _env_float(
        "Q15_V95_MANIPULATION_DIVERGENCE_BPS",
        _MANIPULATION_DEFAULT_DIVERGENCE_BPS, 0.0, 500.0,
    )
    if not asset:
        return global_default
    override_var = f"Q15_V95_MANIPULATION_DIVERGENCE_BPS_{asset.strip().upper()}"
    if override_var in os.environ:
        return _env_float(override_var, global_default, 0.0, 500.0)
    return global_default


def _manipulation_pin_max_distance_sigma() -> float | None:
    """Optional stricter distance-sigma band for the observational PIN *tell*.

    The ``THRESHOLD_PIN`` *regime* (``_regime``) fires at ``distance_sigma <= 0.25``
    and feeds the FROZEN champion's uncertainty/probability — it is deliberately
    left untouched. This knob narrows only the read-only manipulation PIN *tell*:
    the live record shows PIN over-fires (it triggers on ~70% of markets at
    baseline accuracy, i.e. no edge), so an operator can require the price to sit
    even closer to the strike before the tell flags, without changing any
    prediction. Unset/<=0 (the default) => byte-identical legacy behaviour: the
    tell fires for every ``THRESHOLD_PIN`` regime. Recommended starting point for
    validation: ``0.15``.
    """
    raw = os.environ.get("Q15_V95_MANIPULATION_PIN_MAX_DISTANCE_SIGMA")
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0.0 else None


def _pin_tell_passes(
    distance_sigma: float | None, pin_max_distance_sigma: float | None
) -> bool:
    """Whether the PIN tell may fire given an optional stricter distance band.

    Default (no knob configured) => always True (legacy: fire on every
    ``THRESHOLD_PIN`` regime). When a band is set but the measurement is missing,
    keep the tell rather than silently drop it — tightening only suppresses PIN
    when we can positively confirm the price is outside the stricter band.
    """
    if pin_max_distance_sigma is None or distance_sigma is None:
        return True
    return distance_sigma <= pin_max_distance_sigma


def _absorption_lean(absorption: Mapping[str, Any]) -> str | None:
    """Side an absorbed order flow is likely to flip toward, or ``None``.

    Sign convention follows ``_absorption_feature``: positive aggressive flow
    that fails to lift price is bearish (leans ``NO``); negative flow that fails
    to push price down is bullish (leans ``YES``); zero/missing flow has no lean.
    """
    flow = _num(absorption.get("flow"), 0.0) or 0.0
    if flow > 0.0:
        return "NO"
    if flow < 0.0:
        return "YES"
    return None


def _collect_manipulation_tells(
    regime_name: str,
    absorption: Mapping[str, Any],
    exchange: Mapping[str, Any],
    divergence_threshold_bps: float,
    *,
    distance_sigma: float | None = None,
    pin_max_distance_sigma: float | None = None,
) -> tuple[list[str], str | None]:
    """Return ``(tells, lean)`` for the manipulation tells firing this cycle.

    ``tells`` is built in a fixed order (ABSORPTION, PIN, DIVERGENCE) so the
    downstream phrasing reads consistently; ``lean`` is set only by the
    directional ABSORPTION tell. ``divergence_threshold_bps`` is in basis points.
    ``pin_max_distance_sigma`` (with the cycle's ``distance_sigma``) optionally
    narrows the PIN tell below the regime's own band; both default to ``None`` so
    the tell fires for every ``THRESHOLD_PIN`` regime exactly as before.
    """
    tells: list[str] = []
    lean: str | None = None

    if absorption.get("available") and absorption.get("absorbed"):
        tells.append(MANIPULATION_TELL_ABSORPTION)
        lean = _absorption_lean(absorption)

    if regime_name == _REGIME_THRESHOLD_PIN and _pin_tell_passes(
        distance_sigma, pin_max_distance_sigma
    ):
        tells.append(MANIPULATION_TELL_PIN)

    divergence_bps = max(0.0, _num(exchange.get("divergence_bps"), 0.0) or 0.0)
    if regime_name == _REGIME_EXCHANGE_DIVERGENCE or divergence_bps >= divergence_threshold_bps:
        tells.append(MANIPULATION_TELL_DIVERGENCE)

    return tells, lean


def _manipulation_score(tells: Sequence[str]) -> float:
    """0..1 suspicion score for the firing ``tells`` (empty -> ``0.0``).

    Fraction of the three possible tells present, plus a fixed bonus when the
    directional ABSORPTION tell fired, capped at 1.0 and rounded.
    """
    if not tells:
        return 0.0
    fraction = len(tells) / _MANIPULATION_TELLS_TOTAL
    bonus = (_MANIPULATION_ABSORPTION_SCORE_BONUS
             if MANIPULATION_TELL_ABSORPTION in tells else 0.0)
    score = min(_MANIPULATION_SCORE_MAX, fraction + bonus)
    return round(score, _MANIPULATION_SCORE_DECIMALS)


def _manipulation_signal(regime: Mapping[str, Any], absorption: Mapping[str, Any],
                         exchange: Mapping[str, Any], *,
                         asset: str | None = None) -> dict[str, Any]:
    """Read-only suspicion that large players are pushing the price around.

    Pure, deterministic, side-effect free (apart from optional debug logging):
    given the same inputs it always returns the same ``{suspected, reasons,
    lean, score}`` dict. See the module-level notes above for the three tells.

    Configuration (all via env):
      * ``Q15_V95_MANIPULATION_TRACKING`` (default on) — master switch.
      * ``Q15_V95_MANIPULATION_MIN_SIGNALS`` (1..3, default 1) — how many tells
        must agree before the signal is flagged as suspected.
      * ``Q15_V95_MANIPULATION_DIVERGENCE_BPS`` (default 35) and the per-asset
        ``..._<ASSET>`` override — the DIVERGENCE band in basis points.
      * ``Q15_V95_MANIPULATION_PIN_MAX_DISTANCE_SIGMA`` (default unset) — optional
        stricter distance-sigma band for the PIN tell only; unset => unchanged.
    """
    if not _env_bool("Q15_V95_MANIPULATION_TRACKING", True):
        return _no_manipulation()

    regime_name = str((regime or {}).get("name") or "")
    threshold_bps = _manipulation_divergence_threshold_bps(asset)
    tells, lean = _collect_manipulation_tells(
        regime_name, absorption or {}, exchange or {}, threshold_bps,
        distance_sigma=_num((regime or {}).get("distance_sigma")),
        pin_max_distance_sigma=_manipulation_pin_max_distance_sigma())

    min_tells = int(_env_float("Q15_V95_MANIPULATION_MIN_SIGNALS", 1.0, 1.0, 3.0))
    if len(tells) < min_tells:
        return _no_manipulation()

    score = _manipulation_score(tells)
    logger.debug(
        "manipulation suspected asset=%s regime=%s tells=%s lean=%s score=%.4f "
        "divergence_threshold_bps=%.1f",
        asset, regime_name, tells, lean, score, threshold_bps,
    )
    return {"suspected": True, "reasons": tells, "lean": lean, "score": score}


def _manipulation_phrase(manip: Mapping[str, Any]) -> str:
    """Human-readable one-liner for a suspected-manipulation signal."""
    reasons = list(manip.get("reasons") or [])
    parts = [_MANIPULATION_REASON_PHRASES.get(r, r.lower()) for r in reasons]
    text = ", ".join(parts) if parts else "suspected"
    lean = manip.get("lean")
    if lean in ("YES", "NO"):
        text += f" · may flip → {lean}"
    return text


def _yes_decision_threshold(checkpoint: Any) -> float:
    """Calibrated-YES probability needed to call the YES side.

    Default 0.5 — the symmetric cut, byte-identical to the frozen behavior.

    Default-OFF experimental knob, scoped to the **15M** checkpoint, where the
    entire YES deficit lives: in the resolved ledger the champion's YES recall at
    15M is 0.385 vs NO 0.671, and it issues YES only ~35% of the time vs a ~46%
    settle-YES base rate — yet by 7M YES and NO are at parity. 10M and 7M always
    use the symmetric 0.5 cut. Lowering below 0.5 admits more 15M YES (raises
    recall, risks precision on the near-coin-flip 15M band, AUC≈0.5); raising
    above 0.5 abstains from weak 15M YES. The frozen model weights are unchanged
    — only the side-selection cut at this one checkpoint moves — so this is an
    observational lever to A/B before any promotion.
    """
    if str(checkpoint).upper() != "15M":
        return 0.5
    return _env_float("Q15_V95_YES_DECISION_THRESHOLD_15M", 0.5, 0.40, 0.60)


# The out-of-sample challenger beats the NO-leaning champion on YES recall in a
# specific matched-row pocket — the late checkpoints (10M/7M) on BNB/HYPE/XRP/
# DOGE (e.g. BNB +0.206, HYPE +0.124 YES recall vs v95). The challenger is
# GLOBALLY worse (Brier 0.214 vs 0.205) and must never drive a live decision, so
# this is recorded as an inactive marker by default and, even when enabled, stays
# observational: it only flags where the challenger would call YES while the
# champion leans NO, without touching the champion's side or the alert.
_CHALLENGER_YES_ASSIST_ASSETS = frozenset({"BNB", "HYPE", "XRP", "DOGE"})
_CHALLENGER_YES_ASSIST_CHECKPOINTS = frozenset({"10M", "7M"})


def _challenger_yes_assist(asset: Any, checkpoint: Any, champion_side: Any,
                           challenger_yes: float) -> dict[str, Any]:
    """Observational-only marker for the challenger-beats-champion-on-YES cells.

    Default-OFF (``Q15_V95_CHALLENGER_YES_ASSIST``). Returns ``{"active": False}``
    unless explicitly enabled AND the prediction falls in the validated pocket
    (10M/7M, BNB/HYPE/XRP/DOGE) where the champion leans NO but the challenger's
    probability says YES. It never changes ``predicted_side`` or the alert — it is
    surfaced in the snapshot purely so the disagreement can be tracked and the
    edge re-validated before any (gated, significance-tested) promotion.
    """
    if not _env_bool("Q15_V95_CHALLENGER_YES_ASSIST", False):
        return {"active": False}
    asset_u = str(asset).upper()
    checkpoint_u = str(checkpoint).upper()
    eligible = (
        asset_u in _CHALLENGER_YES_ASSIST_ASSETS
        and checkpoint_u in _CHALLENGER_YES_ASSIST_CHECKPOINTS
        and str(champion_side).upper() == "NO"
        and challenger_yes >= 0.5
    )
    if not eligible:
        return {"active": False}
    return {
        "active": True,
        "challenger_side": "YES",
        "champion_side": "NO",
        "challenger_yes_probability": round(float(challenger_yes), 4),
        "asset": asset_u,
        "checkpoint": checkpoint_u,
        "note": "challenger disagrees YES where champion leans NO (observational)",
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
    prof = _feature_profile_enabled()
    volatility = _timed(prof, "volatility", _robust_volatility, canonical)
    returns = _timed(prof, "returns", _multi_horizon_returns, canonical, volatility.get("cadence_seconds"))
    structural = _timed(prof, "structural", _structural_probability, canonical, volatility, returns)
    # Defense-in-depth: the structural base probability is the spine of the model
    # (every feature is an adjustment to its logit). If it failed to load — a state
    # the core-validity gate above normally prevents, but which a future feed/edge
    # case could reach — we must NOT feed thin volatility-derived features into the
    # ensemble and emit a confident-looking number. Fail closed to a prediction-only
    # degraded result instead, with the reason surfaced.
    if not structural.get("available") or structural.get("yes_probability") is None:
        return {
            **base_result, "trade_decision": "PREDICTION_ONLY",
            "main_blocker": "structural_model_unavailable",
            "yes_probability": None, "no_probability": None, "selected_probability": None,
            "conservative_probability": None, "data_quality": canonical.data_quality,
            "evidence_quality": 0.0, "evidence_coverage": 0.0, "low_evidence": True,
            "absent_features": [], "trade_quality": 0.0,
        }
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
    # Near-coin-flip over-confidence guard (DEFAULT OFF). Side-preserving by
    # construction (never crosses 0.5), so it cannot change any predicted side; it
    # only de-confidences the calibrated probability in the near-coin-flip noise
    # band when explicitly enabled. See _coinflip_confidence_shrink.
    calibrated_yes, coinflip_shrink = _coinflip_confidence_shrink(calibrated_yes)
    if coinflip_shrink.get("active"):
        market_anchor["coinflip_shrink"] = coinflip_shrink
    challenger_weights = _timed(prof, "challenger_weights", ledger.challenger_weights, canonical.checkpoint, regime.get("name")) if ledger else CHAMPION_WEIGHTS
    challenger_yes, challenger_contributions = _timed(prof, "model_challenger", _model_probability, structural, feature_values, feature_quality, challenger_weights, regime, data_quality)
    yes_threshold = _yes_decision_threshold(canonical.checkpoint)
    provisional_side = "YES" if calibrated_yes >= yes_threshold else "NO"
    pattern = _timed(prof, "pattern_similarity", ledger.pattern_similarity, feature_values, provisional_side, canonical.checkpoint) if ledger else {"active": False, "shadow_adjustment": 0.0}
    shadow_pattern_adjustment = float(pattern.get("shadow_adjustment") or 0.0)
    challenger_yes = _clamp(challenger_yes + (shadow_pattern_adjustment if provisional_side == "YES" else -shadow_pattern_adjustment), 0.01, 0.99)
    # Anchor the challenger identically so champion-vs-challenger compares weights, not anchoring.
    challenger_yes, _ = _market_anchored_probability(challenger_yes, market_implied_yes, data_quality, evidence_quality, anchor_strength)
    side = provisional_side
    # Observational only: flag where the out-of-sample challenger would call YES
    # while the champion leans NO, in the late-checkpoint/asset pocket where the
    # challenger's YES recall genuinely beats v95. Never alters `side` or the alert.
    challenger_yes_assist = _challenger_yes_assist(
        canonical.asset, canonical.checkpoint, side, challenger_yes)
    selected = calibrated_yes if side == "YES" else 1.0 - calibrated_yes
    uncertainty = 0.018 + (1.0 - data_quality) * 0.12 + float(regime.get("uncertainty", 0.08)) * 0.25
    divergence = _num(exchange_d.get("divergence_bps"), 0.0) or 0.0
    uncertainty += min(0.04, divergence / 1000.0)
    # Evidence coverage: a feature whose feed is absent has quality 0 and so
    # contributes NOTHING to the model logit (contribution = weight·value·quality)
    # — it is treated as missing, never as a neutral/zero signal that masquerades
    # as support. Coverage = the fraction of features actually backed by data
    # (quality at/above the floor). It is computed unconditionally so the alert
    # path can honestly flag a thin snapshot even when the haircut is disabled.
    coverage_floor = _env_float("Q15_V95_EVIDENCE_COVERAGE_FLOOR", 0.40, 0.0, 1.0)
    feature_absent = {name: bool(q < coverage_floor) for name, q in feature_quality.items()}
    covered = sum(1 for absent in feature_absent.values() if not absent)
    evidence_coverage = covered / max(1, len(feature_quality))
    absent_features = sorted(name for name, absent in feature_absent.items() if absent)
    # Evidence-coverage penalty: "insufficient evidence" must read as low
    # confidence, not as a clean neutral signal. Low coverage widens the
    # conservative haircut toward 0.5. Default 0.08 (moderate); set to 0.0 to
    # disable, up to 0.20 for a stronger thin-data haircut.
    coverage_penalty = _env_float("Q15_V95_EVIDENCE_COVERAGE_PENALTY", 0.08, 0.0, 0.20)
    if coverage_penalty > 0.0:
        uncertainty += coverage_penalty * (1.0 - evidence_coverage)
    # Low-evidence flag: coverage below this fraction means the prediction rests
    # on too few backed features to be read as well-supported. This is an
    # observability marker only — it never changes the (frozen) model output or
    # the entry gate; it surfaces in the snapshot and, when the default-OFF
    # Q15_V95_LOW_EVIDENCE_FLAG is enabled, as a compact note in the alert.
    low_evidence_min_coverage = _env_float("Q15_V95_LOW_EVIDENCE_MIN_COVERAGE", 0.50, 0.0, 1.0)
    low_evidence = evidence_coverage < low_evidence_min_coverage
    conservative = _clamp(selected - uncertainty, 0.01, 0.99)

    quote = _selected_quote(snapshot, side)
    costs = _estimated_costs(snapshot, quote)
    ask = _num(quote.get("ask_cents"))
    spread = _num(quote.get("spread_cents"))
    depth = _kalshi_depth(snapshot, side)
    yes_ask_depth = _kalshi_depth(snapshot, "YES")
    no_ask_depth = _kalshi_depth(snapshot, "NO")
    yes_bid_depth = _first_num(snapshot, ("yes_bid_qty", "yes_bid_size", "yes_bid_depth_contracts"))
    no_bid_depth = _first_num(snapshot, ("no_bid_qty", "no_bid_size", "no_bid_depth_contracts"))
    quote_ts = canonical.feed_timestamps.get("quote")
    quote_age = None if quote_ts is None else max(0.0, canonical.observed_at - quote_ts)
    spot_depth_fields = _spot_depth_quote_fields(snapshot, canonical.observed_at)
    kalshi_taker_yes_15s = _num(snapshot.get("taker_yes_volume_15s"))
    kalshi_taker_no_15s = _num(snapshot.get("taker_no_volume_15s"))
    kalshi_taker_net_yes_15s = (
        kalshi_taker_yes_15s - kalshi_taker_no_15s
        if kalshi_taker_yes_15s is not None and kalshi_taker_no_15s is not None
        else None
    )
    # Per-checkpoint gates. 7M defaults mirror 10M so adding the 7-minute tracker
    # does not change live entry behavior; both stay overridable via env.
    _checkpoint = canonical.checkpoint if canonical.checkpoint in ("10M", "15M", "7M") else "10M"
    _required_edge_default = {"10M": 6.0, "7M": 6.0, "15M": 4.0}.get(_checkpoint, 4.0)
    # 15M min-prob raised 0.58 -> 0.60 (the live record shows 15M is a coin flip,
    # so a 0.58 gate admits near-random picks). Overridable via env.
    _min_prob_default = {"10M": 0.60, "7M": 0.60, "15M": 0.60}.get(_checkpoint, 0.60)
    required_edge = _env_float(f"Q15_V95_{_checkpoint}_REQUIRED_EDGE_CENTS", _required_edge_default, 0.0, 25.0)
    minimum_probability = _env_float(f"Q15_V95_{_checkpoint}_MIN_PROBABILITY", _min_prob_default, 0.50, 0.90)
    # Volatility-aware edge bar (DEFAULT OFF). Scale the required edge up for
    # extreme-conviction (favourite) picks — those near 0/1 are hit hardest by
    # slippage/adverse selection, so they need a thicker cushion. At conservative
    # = 0.5 the factor is 1.0 (no change). required_edge *= 1 + k*(2c-1)^2.
    if _env_bool("Q15_V95_EDGE_VOLATILITY_SCALING", False):
        _vol_k = _env_float("Q15_V95_EDGE_VOLATILITY_K", 1.0, 0.0, 5.0)
        required_edge = required_edge * (1.0 + _vol_k * (2.0 * conservative - 1.0) ** 2)
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
    # Adverse-selection adder (DEFAULT 0.0 — no live change). The base cost model
    # assumes a fill at ask with half-spread slippage and omits adverse selection;
    # the shadow-economics A/B measures the ~1c gap. Set this once that delta
    # proves out, to stop the edge over-admitting thin-margin favourites.
    total_costs += _env_float("Q15_V95_EDGE_ADVERSE_SELECTION_CENTS", 0.0, 0.0, 25.0)
    # Canonical cent precision on the money path: net_edge is a signed delta
    # (legitimately negative, not range-bounded); ideal_entry is an absolute
    # price that must round-trip into Kalshi's [0, 100]¢ range. Both go through
    # q15_upgrade.money so the displayed/stored values never carry float noise
    # like "3.3299999¢" or an impossible ">100¢" entry level.
    net_edge = None if ask is None else round_edge_cents(conservative * 100.0 - ask - total_costs)
    ideal_entry = clamp_price_cents(conservative * 100.0 - total_costs - required_edge, context="ideal_entry")
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
        "challenger_yes_assist": challenger_yes_assist,
        "selected_probability": selected,
        "conservative_probability": conservative,
        "confidence_grade": grade,
        "data_quality": data_quality,
        "evidence_quality": evidence_quality,
        "evidence_coverage": evidence_coverage,
        "low_evidence": low_evidence,
        "absent_features": absent_features,
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
        "manipulation": _manipulation_signal(regime, absorption_d, exchange_d, asset=canonical.asset),
        "contributions": contributions,
        "challenger_contributions": challenger_contributions,
        "supporting_factors": supporting,
        "opposing_factors": opposing,
        "calibration": {**calibration, "production_enabled": production_calibration_enabled},
        "shadow_calibrated_yes_probability": shadow_calibrated_yes,
        "pattern_similarity": pattern,
        # Decision-time context is forwarded to the read-only shadow challenger
        # via the quote bundle. Without spot / strike / time-remaining / vol the
        # challenger's distance-to-target and time-decay features (the dominant
        # drivers of a 15-minute binary) extract as zeros, leaving it effectively
        # blind. These keys are additive — existing readers only use bid/ask/spread.
        "quote": {
            **quote, "ask_depth": depth, "quote_age_seconds": quote_age,
            "spot": canonical.spot, "target": canonical.threshold,
            "seconds_remaining": canonical.seconds_remaining,
            "volatility_per_min": _shadow_vol_per_min(volatility),
            "depth_contracts": depth, "data_quality": data_quality,
            "yes_bid_depth_contracts": yes_bid_depth,
            "yes_ask_depth_contracts": yes_ask_depth,
            "no_bid_depth_contracts": no_bid_depth,
            "no_ask_depth_contracts": no_ask_depth,
            "kalshi_depth_status": snapshot.get("kalshi_depth_status"),
            "kalshi_depth_missing_reason": snapshot.get("kalshi_depth_missing_reason"),
            "kalshi_depth_retry_used": snapshot.get("kalshi_depth_retry_used"),
            "kalshi_taker_yes_volume_15s": kalshi_taker_yes_15s,
            "kalshi_taker_no_volume_15s": kalshi_taker_no_15s,
            "kalshi_taker_net_yes_volume_15s": kalshi_taker_net_yes_15s,
            **spot_depth_fields,
        },
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
    snapshot["q15_v9_5_evidence_coverage"] = analysis.get("evidence_coverage")
    snapshot["q15_v9_5_low_evidence"] = analysis.get("low_evidence")
    snapshot["q15_v9_5_absent_features"] = analysis.get("absent_features")
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
    # Entry Economics (entry-econ-v1): a SEPARATE, read-only trade-quality system
    # answering "is this contract worth buying at an executable price?" with a
    # single ENTER / WAIT / SKIP and the executable economics behind it. It never
    # changes the prediction or the frozen live entry gate; the compact panel and
    # the decision are surfaced additively. The optional entry-performance ledger
    # only writes when Q15_ENTRY_ECON_LEDGER is enabled (off by default), so this
    # pure computation creates no files. Fully guarded — never raises into the loop.
    try:
        from .entry_economics.runner import get_runner as _ee_runner
        _ee = None
        _runner = _ee_runner()
        if _runner is not None:
            # The runner evaluates AND (only when Q15_ENTRY_ECON_LEDGER is enabled)
            # persists the evaluation to its own ledger — never the production tables.
            _ee = _runner.evaluate(analysis, contract=analysis.get("ticker"))
            _panel = _runner.panel(_ee)
        else:
            _ee = None
            _panel = None
        if _ee is not None:
            snapshot["q15_entry_econ_version"] = _ee.version
            snapshot["q15_entry_econ_decision"] = _ee.decision
            snapshot["q15_entry_econ_side"] = _ee.side
            snapshot["q15_entry_econ_main_blocker"] = _ee.main_blocker
            snapshot["q15_entry_econ_main_blocker_text"] = _ee.main_blocker_text
            if _ee.conservative is not None:
                snapshot["q15_entry_econ_conservative_probability"] = _ee.conservative.conservative_side_prob
            if _ee.economics is not None:
                snapshot["q15_entry_econ_net_edge_cents"] = _ee.economics.net_edge_cents
                snapshot["q15_entry_econ_max_entry_cents"] = _ee.economics.maximum_entry_price_cents
                snapshot["q15_entry_econ_recommended_low_cents"] = _ee.economics.recommended_entry_low_cents
                snapshot["q15_entry_econ_recommended_high_cents"] = _ee.economics.recommended_entry_high_cents
                snapshot["q15_entry_econ_break_even_prob"] = _ee.economics.break_even_prob
            snapshot["q15_entry_econ_panel"] = _panel
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
    # Rank by SKILL (default OFF): on the live record the confidence GRADE tracks
    # accuracy strongly (7M A 94% / B 80% / C 76% / D 54%) while net-edge order
    # does not — so when enabled, order by decision priority, then grade, then the
    # model's decisiveness (selected probability), then net edge. Default OFF keeps
    # the existing net-edge-first ordering (frozen-champion behaviour).
    rank_by_skill = _env_bool("Q15_V95_RANK_BY_SKILL", False)
    _grade_rank = {"A": 4, "B": 3, "C": 2, "D": 1}
    rows = []
    for asset, analysis in analyses.items():
        net_edge = float(analysis.get("net_edge_cents") if analysis.get("net_edge_cents") is not None else -999.0)
        if rank_by_skill:
            score = (
                priority.get(str(analysis.get("trade_decision")), 1),
                _grade_rank.get(str(analysis.get("confidence_grade") or "").upper(), 0),
                float(analysis.get("selected_probability") or 0.0),
                net_edge,
                float(analysis.get("data_quality") or 0.0),
            )
        else:
            score = (
                priority.get(str(analysis.get("trade_decision")), 1),
                net_edge,
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
                line = f"{asset}: {_manipulation_phrase(manip)}"
                # Highest-accuracy manipulation subset on record: a fresh tell that
                # first appears at the 7M close. Print the predicted side and the
                # LIVE historical hit-rate (+ sample) so NO (~97%) and YES (~85%)
                # are weighted differently; below the min sample, say "building".
                fnc = (analyses.get(asset, {}) or {}).get("fresh_manip_near_close")
                if isinstance(fnc, Mapping):
                    side = str(fnc.get("side") or "")
                    n = int(fnc.get("n") or 0)
                    right = int(fnc.get("right") or 0)
                    acc = fnc.get("accuracy")
                    min_n = int(_env_float("Q15_V95_SCOREBOARD_MIN_N", 10, 1, 1000))
                    if n >= min_n and acc is not None:
                        line += f" · 🎯 FRESH 7M·{side} — predicted {side} {float(acc) * 100:.1f}% right ({right}/{n})"
                    else:
                        line += f" · 🎯 FRESH 7M·{side} — high-confidence (building, n={n})"
                body.append(line)

    # Thin-evidence watch (default OFF): flag any top pick whose prediction rests
    # on too few data-backed features, so a confident-looking number is read with
    # the caveat that it is thinly supported. Observability only — the model and
    # entry gate are unchanged; the line is compact and never alters the markers.
    if _env_bool("Q15_V95_LOW_EVIDENCE_FLAG", False):
        thin = [
            (str(row["asset"]), analyses.get(str(row["asset"]), {}))
            for row in top
        ]
        thin = [
            (asset, a) for asset, a in thin
            if a.get("prediction_available") and a.get("low_evidence")
        ]
        if thin:
            body.append("")
            body.append("⚠ Thin evidence")
            for asset, a in thin:
                cov = _num(a.get("evidence_coverage"))
                cov_s = f"{cov * 100:.0f}%" if cov is not None else "n/a"
                missing = ", ".join(a.get("absent_features") or []) or "—"
                body.append(f"{asset}: {cov_s} backed · missing {missing}")

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


def _format_run_cycle_breakdown(timing: Mapping[str, Any], chain_timing: Mapping[str, Any]) -> str:
    """One-line attribution of a slow ``run_cycle``'s internal stages, for the log.

    The watchdog only names the opaque top-level ``run_cycle`` stage; the rich
    per-sub-stage split already lives in ``/api/health`` but is easy to miss at the
    exact slow moment. Logging it when a cycle is slow names the dominant sub-stage
    directly — the remote-Postgres parent chain (``v91_pre_enrich`` /
    ``v91_finalize_all``) vs the v95 analysis (``record`` / ``analyse``) vs the
    periodic settlement reconcile — so the real cause is visible without catching
    the health JSON live. Diagnostic only; it never changes a decision."""
    def _g(d: Mapping[str, Any], k: str) -> float:
        try:
            return float(d.get(k) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    parts = [
        f"total={_g(timing, 'total'):.2f}s",
        f"parent_chain={_g(timing, 'parent_chain'):.2f}s",
        f"v95_analysis={_g(timing, 'v95_analysis'):.2f}s",
    ]
    chain_bits = [
        f"{k}={_g(chain_timing, k):.2f}s"
        for k in ("v94_super_chain", "v91_pre_enrich", "v91_finalize_all")
        if k in chain_timing
    ]
    if chain_bits:
        parts.append("parent[" + " ".join(chain_bits) + "]")
    v95_sub = timing.get("v95_sub")
    if isinstance(v95_sub, Mapping):
        sub_bits = [
            f"{k}={_g(v95_sub, k):.2f}s"
            for k in ("record", "analyse", "build", "deepcopy")
            if k in v95_sub
        ]
        if sub_bits:
            parts.append("v95[" + " ".join(sub_bits) + "]")
    for k in ("market_reconcile", "signal_store_reconcile", "other"):
        if k in timing:
            parts.append(f"{k}={_g(timing, k):.2f}s")
    return " ".join(parts)


def _iso_from_epoch(epoch: float) -> str:
    """UTC ISO-8601 timestamp for a unix epoch (the prediction's wall-clock)."""
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return datetime.now(timezone.utc).isoformat()


def _eastern_label(epoch: float) -> str:
    """Visible Eastern-Time label (America/Detroit, EDT/EST) for a unix epoch.
    Display only — the stored ISO field stays UTC for DB/API consistency."""
    try:
        from .timez import fmt_eastern
        return fmt_eastern(float(epoch))
    except (TypeError, ValueError, OSError, OverflowError):
        from .timez import fmt_eastern
        return fmt_eastern(time.time())


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

# Visible entry trichotomy (ENTER / WAIT / SKIP) — deliberately separate from the
# final-outcome prediction. ENTER only on a live recommendation; WAIT when price
# is the sole blocker (the call is right, the price isn't); SKIP for every other
# not-yet-actionable state. A pick can stay YES/NO while reading SKIP.
_ENTRY_LABEL_BY_DECISION = {
    "ENTRY_RECOMMENDED": "ENTER",
    "WATCH_PRICE": "WAIT",
    "WATCH_CONFIDENCE": "SKIP",
    "WATCH_DATA_QUALITY": "SKIP",
    "WATCH_LIQUIDITY": "SKIP",
    "WATCH_TIME": "SKIP",
    "PREDICTION_ONLY": "SKIP",
    "AVOID_INVALID_DATA": "SKIP",
}

# One short, honest headline reason per dominant side-aligned feature.
_REASON_FEATURE_PHRASE = {
    "wick": "wick rejection favors {side}",
    "momentum": "momentum favors {side}",
    "flow": "order flow favors {side}",
    "book": "book imbalance favors {side}",
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


_RANKED_PICK_COUNT = 3    # the official interval report shows exactly three ranks
_PANEL_RISK_HIGH = 60.0   # 0..100 panel scale
_PANEL_RISK_MEDIUM = 35.0


def _panel_manipulation(analysis: Mapping[str, Any]) -> dict[str, Any] | None:
    """Map the read-only manipulation/flip signals into the panel's MANIPULATION
    block, or None when nothing is flagged. ``risk`` is a 0..100 number.

    The flip-risk score is already 0..100; the fallback manipulation score is on
    a 0..1 scale, so it is rescaled to 0..100 here to keep ``risk`` and the
    level thresholds (which are 0..100) in consistent units.
    """
    fr = analysis.get("flip_risk") or {}
    manip = analysis.get("manipulation") or {}
    score = _num(fr.get("score"))
    if score is None:
        manip_score = _num(manip.get("score"))  # 0..1 -> rescale to 0..100
        score = manip_score * 100.0 if manip_score is not None else None
    suspected = bool(manip.get("suspected")) or (score is not None and score >= _PANEL_RISK_HIGH)
    if not suspected or score is None:
        return None
    level = "HIGH" if score >= _PANEL_RISK_HIGH else "MEDIUM" if score >= _PANEL_RISK_MEDIUM else "LOW"
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


def _entry_score(analysis: Mapping[str, Any]) -> float | None:
    """0–100 composite ENTRY SCORE — a read-only SHADOW overlay (does NOT drive
    the live entry decision; the champion stays frozen). Combines signals the
    system already calculates with the documented weights
    (30 direction-confidence / 25 edge / 20 wick / 15 momentum / 10 manipulation),
    each mapped to 0..1 then weighted. Returns None when the prediction is
    unavailable so the panel shows '—' rather than a fabricated number.

    Component mapping (each clamped to 0..1):
      dir-conf = |selected_probability − 0.5| × 2   (decisiveness of the call)
      edge     = net_edge_cents / EDGE_CAP          (clamped ≥ 0)
      wick     = |wick feature|                     (price-action magnitude)
      momentum = |momentum feature|
      manip    = 1 − manipulation_risk/100          (less suspected = higher)
    """
    if not analysis.get("prediction_available"):
        return None
    sel = _num(analysis.get("selected_probability"))
    if sel is None:
        return None
    fv = analysis.get("feature_values") or {}
    edge_cap = _env_float("Q15_V95_ENTRY_SCORE_EDGE_CAP", 10.0, 1.0, 100.0)
    dir_conf = _clamp(abs(sel - 0.5) * 2.0, 0.0, 1.0)
    edge = _clamp((_num(analysis.get("net_edge_cents"), 0.0) or 0.0) / edge_cap, 0.0, 1.0)
    wick = _clamp(abs(_num(fv.get("wick"), 0.0) or 0.0), 0.0, 1.0)
    momentum = _clamp(abs(_num(fv.get("momentum"), 0.0) or 0.0), 0.0, 1.0)
    manip_block = _panel_manipulation(analysis) or {}
    manip_risk = _num(manip_block.get("risk"))
    manip = 1.0 - _clamp((manip_risk or 0.0) / 100.0, 0.0, 1.0)
    score = 30.0 * dir_conf + 25.0 * edge + 20.0 * wick + 15.0 * momentum + 10.0 * manip
    return round(_clamp(score, 0.0, 100.0), 1)


def _feature_status(value: float | None, side: str, *, pos: str, neg: str,
                    threshold: float = 0.05) -> str:
    """A short, honest qualitative tag for a signed feature relative to the
    predicted side: supportive when it leans the side's way, against when it
    opposes, neutral otherwise — with the raw value shown for transparency."""
    v = _num(value)
    if v is None:
        return "—"
    yes_lean = v if str(side).upper() == "YES" else -v
    tag = pos if yes_lean > threshold else neg if yes_lean < -threshold else "neutral"
    return f"{tag} ({v:+.2f})"


def _main_reason(analysis: Mapping[str, Any], side: str) -> str:
    """One short, honest headline reason for the final-outcome call: the strongest
    side-aligned feature this cycle, or a neutral 'most likely at close' when no
    single feature stands out. Never invents a number — purely qualitative."""
    s = str(side or "").upper()
    if s not in {"YES", "NO"}:
        return "prediction unavailable"
    fv = analysis.get("feature_values") or {}
    best_key: str | None = None
    best_mag = 0.10  # ignore near-zero / weak leans
    for key in ("wick", "momentum", "flow", "book"):
        v = _num(fv.get(key))
        if v is None:
            continue
        aligned = v if s == "YES" else -v   # YES-signed feature -> side-aligned magnitude
        if aligned > best_mag:
            best_key, best_mag = key, aligned
    if best_key is not None:
        return _REASON_FEATURE_PHRASE[best_key].format(side=s)
    return f"{s} most likely at close"


def _flip_target_side(analysis: Mapping[str, Any], side: str) -> str | None:
    """The opposite settled side a genuine flip would move toward ('NO → YES' =>
    'YES'), or None when there is no monitored opposite-side target."""
    fr = analysis.get("flip_risk") or {}
    monitored = str(fr.get("direction_monitored") or "")
    if "→" in monitored:
        tail = monitored.split("→")[-1].strip().upper()
        if tail in {"YES", "NO"} and tail != str(side).upper():
            return tail
    return None


def _extract_pick(rank: int, asset: str, analysis: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten one ranked analysis into the panel pick contract — the fields the
    compact official report shows plus the detail fields the record path / shadow
    overlays still read. Pure mapping — no I/O, no decisions."""
    side = str(analysis.get("prediction_side") or "")
    yes_p = _num(analysis.get("yes_probability"))
    no_p = None if yes_p is None else 1.0 - yes_p
    confidence = yes_p if side.upper() == "YES" else (None if yes_p is None else 1.0 - yes_p)
    manip = _panel_manipulation(analysis) or {}
    fv = analysis.get("feature_values") or {}
    quote = analysis.get("quote") or {}
    decision = str(analysis.get("trade_decision") or "")
    # Genuine-flip risk (0..100) is the flip-risk score; temporary-manipulation
    # risk (0..100) is the manipulation block's own 0..1 score rescaled, falling
    # back to the blended panel risk. The two are kept distinct on purpose.
    flip = analysis.get("flip_risk") or {}
    manip_block = analysis.get("manipulation") or {}
    manip_score01 = _num(manip_block.get("score"))
    manip_display = manip_score01 * 100.0 if manip_score01 is not None else _num(manip.get("risk"))
    cal = analysis.get("calibration") or {}
    sample = int(cal["rows"]) if _num(cal.get("rows")) is not None else None
    return {
        "rank": rank,
        "asset": asset,
        "side": side,
        "confidence": confidence,
        # The champion's A/B/C/D confidence grade (computed once in analyse_v95
        # from selected_probability + data_quality). Surfaced — never recomputed
        # — so the check panel shows the SAME grade the rest of the system uses.
        "confidence_grade": analysis.get("confidence_grade"),
        "yes_prob": yes_p,
        "no_prob": no_p,
        # compact decision block (headline pick)
        "flip_prob": _num(flip.get("score")),
        "flip_side": _flip_target_side(analysis, side),
        "manip_prob": manip_display,
        # strict FLIP CHECK (the only flip output shown to the owner): a YES/NO
        # decision against a learned, validated per-interval threshold.
        "flip_decision": str((analysis.get("flip_decision") or {}).get("decision") or "NO"),
        "flip_decision_probability": _num((analysis.get("flip_decision") or {}).get("flip_probability")),
        "flip_decision_threshold": _num((analysis.get("flip_decision") or {}).get("threshold")),
        "entry_label": _ENTRY_LABEL_BY_DECISION.get(decision, "SKIP"),
        "best_entry_max": _num(analysis.get("ideal_entry_cents")),
        "main_reason": _main_reason(analysis, side),
        "sample": sample,
        # detail fields retained for the record path + shadow overlays
        "entry_score": _entry_score(analysis),
        "manipulation_prob": _num(manip.get("risk")),
        "price_cents": _num(quote.get("ask_cents")),
        "rec_low": None,
        "rec_high": None,
        "max_cents": _num(analysis.get("ideal_entry_cents")),
        "wick_status": _feature_status(fv.get("wick"), side, pos="supportive", neg="against"),
        "flow_status": (f"flow {(_num(fv.get('flow')) or 0.0):+.2f} · "
                        f"mom {(_num(fv.get('momentum')) or 0.0):+.2f}"),
        "edge_cents": _num(analysis.get("net_edge_cents")),
        "decision": decision,
        "is_entry": _is_actionable_entry(analysis),
    }


def _build_ranked_picks(analyses: Mapping[str, Mapping[str, Any]],
                        ranking: Sequence[Mapping[str, Any]], top_k: int = 3) -> list[dict[str, Any]]:
    """Top-k picks in the existing executable-trade ranking order (the same order
    the detail renders), each fully extracted. Fewer than k available picks are
    left short so the panel shows '—' for the missing ranks."""
    picks: list[dict[str, Any]] = []
    for r in ranking:
        asset = str(r.get("asset"))
        analysis = analyses.get(asset) or {}
        if not analysis.get("prediction_available"):
            continue
        picks.append(_extract_pick(len(picks) + 1, asset, analysis))
        if len(picks) >= top_k:
            break
    return picks


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


def _interval_alerts_enabled(checkpoint: str) -> bool:
    """Whether a resolved checkpoint may DELIVER actionable alerts to the owner.

    On the live record the 15M checkpoint is a coin flip (~53%, Wilson CI
    includes 50%) and loses money per pick, so its alert delivery defaults OFF
    (set ``Q15_V95_15M_ALERTS_ENABLED=1`` to restore it). The prediction is
    still recorded observationally for learning and the timing experiment — only
    the Telegram delivery / official actionable record is suppressed. The
    skilled 10M/7M checkpoints always deliver."""
    if str(checkpoint).upper() == "15M":
        return _env_bool("Q15_V95_15M_ALERTS_ENABLED", False)
    return True


def _timing_experiment_marks() -> list[int]:
    """Extra time-to-close marks (seconds) at which to capture an OBSERVATIONAL
    timing-experiment prediction. Default is the full 15M→7M ladder —
    900/780/720/660/600/540/480/420s (15/13/12/11/10/9/8/7 min) — so the hourly
    report shows the WHOLE per-mark accuracy curve and we can MEASURE where it
    crosses into a usable edge, including the 10M→7M "knee" (9M/8M, previously
    untracked). An empty value disables collection."""
    raw = os.environ.get("Q15_V95_TIMING_EXPERIMENT_SECONDS")
    if raw is None:
        raw = "900,780,720,660,600,540,480,420"
    marks: list[int] = []
    for part in str(raw).replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(float(part))
        except (TypeError, ValueError):
            continue
        if 0 < value <= 1800 and value not in marks:
            marks.append(value)
    return marks


def _timing_experiment_band() -> float:
    """Half-width (seconds) of the capture window around each timing mark. Wide
    enough that a slow refresh cycle can't skip a mark, narrow enough not to
    overlap a neighbour."""
    return _env_float("Q15_V95_TIMING_EXPERIMENT_BAND_SECONDS", 6.0, 1.0, 60.0)


def _net_edge_gate_ok(analysis: Mapping[str, Any]) -> bool:
    """Net-edge-after-cost entry gate. DEFAULT OFF (Q15_V95_NET_EDGE_GATE_ENABLED).
    On the live record the system is accuracy-positive but P&L-negative (paying
    too much for favourites), so when enabled an entry must clear a minimum
    net edge (cents, after costs) to remain actionable — otherwise the pick is
    treated as NO-ENTRY. Default OFF keeps the frozen champion's behaviour."""
    if not _env_bool("Q15_V95_NET_EDGE_GATE_ENABLED", False):
        return True
    min_edge = _env_float("Q15_V95_NET_EDGE_GATE_MIN_CENTS", 0.0, -100.0, 100.0)
    edge = _num(analysis.get("net_edge_cents"))
    return edge is not None and edge >= min_edge


def _is_actionable_entry(analysis: Mapping[str, Any]) -> bool:
    """An entry is actionable only if the champion recommends it AND it clears
    the (default-OFF) net-edge gate."""
    return str(analysis.get("trade_decision") or "") == "ENTRY_RECOMMENDED" and _net_edge_gate_ok(analysis)


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
            # Optional freshness fence on the bridged public composite. The local
            # quote/edge path uses sub-30s data; a public flow/book that is much
            # older can mix a stale read into the same row. `_combine_public_signal`
            # already down-weights by age, but this is a hard cutoff for when the
            # operator wants the bridge to refuse outright-stale public data. Default
            # 0.0 = OFF (preserves current behaviour; staleness handled only by the
            # soft freshness weight). Read-only: only gates an evidence injection.
            bridge_max_public_age = _env_float("Q15_V95_BRIDGE_MAX_PUBLIC_AGE_SECONDS", 0.0, 0.0, 600.0)
            public_age = _num(public.get("age_seconds")) if isinstance(public, Mapping) else None
            public_too_stale = (
                bridge_max_public_age > 0.0 and public_age is not None
                and public_age > bridge_max_public_age
            )
            if public_too_stale:
                stats["public_stale_skipped"] = stats.get("public_stale_skipped", 0) + 1
            if isinstance(public, Mapping) and not public_too_stale:
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


def _send_with_optional_key(notifier: Any, message: str, idempotency_key: str) -> dict[str, Any]:
    """Call ``notifier.send_with_result`` with a stable idempotency key when the
    notifier supports one (the reliable outbox), falling back transparently for a
    bare notifier whose signature is ``send_with_result(text)``. The key lets the
    delivery be reconciled later from the outbox's true status."""
    try:
        return notifier.send_with_result(message, idempotency_key=idempotency_key)
    except TypeError:
        return notifier.send_with_result(message)


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
        # Hot-path DB guards. The prediction/timing/flip-decision tables are
        # insert-once per contract+checkpoint/mark; after the first write, later
        # cycles were still paying SQLite round-trips that could not change the
        # frozen record. These caches preserve the same first-write semantics while
        # keeping the ~1s loop off the disk on repeated snapshots.
        self._prediction_record_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._timing_observation_recorded: set[tuple[str, int]] = set()
        self._flip_decision_recorded: set[tuple[str, str]] = set()
        self._flip_threshold_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._reconcile_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="q15-v95-reconcile")
        self._reconcile_future: Future[dict[str, Any]] | None = None
        atexit.register(self._reconcile_executor.shutdown, wait=False)
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
        self._accuracy_summary_cache: dict[str, Any] | None = None
        self._accuracy_summary_cache_at = 0.0

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

    def _flip_threshold_selection(self, checkpoint: str, cfg: flip_decision.FlipConfig) -> dict[str, Any]:
        """Cached strict-flip threshold selection for the live loop.

        Selection depends only on resolved flip-decision history and config, not
        on the current open snapshot. The ledger bumps ``_data_version`` on
        settlement/learning updates, so caching by that version avoids rescanning
        historical rows every refresh cycle while still refreshing when new
        outcomes land.
        """
        default = {"validated": False, "threshold": 1.01}
        if not cfg.enabled:
            return dict(default)
        version = int(getattr(self.ledger, "_data_version", 0) or 0)
        key = (
            str(checkpoint),
            version,
            float(cfg.target_precision),
            float(cfg.train_fraction),
            int(cfg.min_total),
            int(cfg.min_yes_train),
            int(cfg.min_yes_test),
        )
        cached = self._flip_threshold_cache.get(key)
        if cached is not None:
            return dict(cached)
        try:
            selected = flip_decision.select_threshold(
                self.ledger.flip_decision_rows(checkpoint, resolved_only=True, post_reset_only=True),
                cfg,
            )
        except Exception as exc:  # never let calibration abort the cycle
            logger.debug("flip threshold selection skipped for %s: %s", checkpoint, exc)
            selected = dict(default)
        if len(self._flip_threshold_cache) > 12:
            self._flip_threshold_cache.clear()
        self._flip_threshold_cache[key] = dict(selected)
        return dict(selected)

    def _reconcile_job(self, get_market: Any, now: float) -> dict[str, Any]:
        """Run settlement reconciliation off the refresh-loop hot path."""
        result_events: list[Mapping[str, Any]] = []
        last_reconcile: dict[str, Any] = {}
        last_market_reconcile: dict[str, Any] = {}
        try:
            if self.signal_store is not None:
                last_reconcile = self.ledger.reconcile_from_signal_store(self.signal_store)
                result_events = list(last_reconcile.get("result_events") or [])
            if callable(get_market):
                max_calls = _env_int("Q15_V95_RECONCILE_MAX_CALLS", 6, 1, 1000)
                last_market_reconcile = self.ledger.reconcile_pending_from_market(
                    get_market,
                    now,
                    max_calls=max_calls,
                )
                result_events = list(last_market_reconcile.get("result_events") or []) + result_events
            if _env_bool("Q15_V95_FLIP_RISK_TRACKING", True):
                self.ledger.reconcile_flip_warnings()
            try:
                from q15_upgrade.interval_research.runner import get_runner as _ir_runner
                _irr = _ir_runner()
                if _irr is not None:
                    _irr.resolve_settled(result_events, now)
            except Exception:
                logger.debug("interval-research resolve skipped", exc_info=True)
            try:
                from q15_upgrade.marketlead.runner import get_runner as _marketlead_runner
                _marketlead = _marketlead_runner()
                if _marketlead is not None:
                    _marketlead.resolve_settled(result_events, now)
            except Exception:
                logger.debug("marketlead resolve skipped", exc_info=True)
            try:
                from q15_upgrade.high_vol_flip.runner import get_runner as _hvf_runner
                _hvf = _hvf_runner()
                if _hvf is not None:
                    _hvf.resolve_settled(result_events, now)
            except Exception:
                logger.debug("high_vol_flip resolve skipped", exc_info=True)
            return {
                "last_reconcile": last_reconcile,
                "last_market_reconcile": last_market_reconcile,
                "result_events": result_events[:20],
            }
        except Exception as exc:
            logger.debug("v95 reconcile job failed", exc_info=True)
            return {"error": f"{type(exc).__name__}: {exc}", "result_events": []}

    def _harvest_reconcile_job(self) -> list[Mapping[str, Any]]:
        future = self._reconcile_future
        if future is None or not future.done():
            return []
        self._reconcile_future = None
        try:
            result = future.result()
        except Exception as exc:
            self._last_market_reconcile = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
            logger.debug("v95 reconcile harvest failed", exc_info=True)
            return []
        if isinstance(result.get("last_reconcile"), Mapping):
            self._last_reconcile = dict(result["last_reconcile"])
        if isinstance(result.get("last_market_reconcile"), Mapping):
            self._last_market_reconcile = dict(result["last_market_reconcile"])
        if result.get("error"):
            self._last_market_reconcile = {"available": False, "reason": str(result.get("error"))}
        return list(result.get("result_events") or [])

    def _schedule_reconcile_job(self, get_market: Any, now: float) -> None:
        if self._reconcile_future is not None and not self._reconcile_future.done():
            return
        self._last_reconcile_at = now
        self._reconcile_future = self._reconcile_executor.submit(self._reconcile_job, get_market, now)

    def _maybe_send_resolution_stall_alert(self, notifier: Any, now: float) -> None:
        if not _env_bool("Q15_V95_RESOLUTION_STALL_ALERTS_ENABLED", True):
            return
        state = self.ledger.resolution_stall_alert_state(self._last_market_reconcile, now=now)
        if not state.get("send"):
            return
        message = (
            "<b>Q15 OPS GRADING STALL</b>\n"
            f"V9.5 grading has resolved 0 rows for {float(state.get('stalled_for_seconds') or 0.0):.0f}s.\n"
            f"Backlog: {int(state.get('unresolved_pastclose') or 0)} past-close rows "
            f"across {int(state.get('unresolved_pastclose_tickers') or 0)} tickers; "
            f"parked {int(state.get('parked') or 0)}.\n"
            "Run tools/backfill_resolutions.py on the live host and inspect parked tickers."
        )
        try:
            if hasattr(notifier, "send_with_result"):
                _send_with_optional_key(
                    notifier,
                    message,
                    "q15-v95-resolution-stall-ops-alert",
                )
            elif hasattr(notifier, "send"):
                notifier.send(message)
        except Exception:
            logger.warning("v95 resolution-stall ops alert send failed", exc_info=True)

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
            analyses, output, public_map, canonicals, _sub = self._analyse_cycle_assets(
                parent_output, checkpoint, now,
            )
            ranking = rank_analyses(analyses)
            flip_sent, flip_failed = self._record_cycle_predictions(
                checkpoint, analyses, output, canonicals, ranking, notifier, now, _sub,
            )
            _t["v95_analysis"] = round(time.monotonic() - _t0, 3)
            _t["v95_sub"] = {k: round(v, 3) for k, v in _sub.items()}
            self._dispatch_research_overlays(analyses, canonicals, now)
            result_events: list[Mapping[str, Any]] = []
            _t0 = time.monotonic()
            result_events = self._harvest_reconcile_job()
            self._maybe_send_resolution_stall_alert(notifier, now)
            if now - self._last_reconcile_at >= _env_float("Q15_V95_RECONCILE_INTERVAL_SECONDS", 30.0, 5.0, 300.0):
                # Settle closed markets directly from Kalshi in the background, so
                # grading/recaps keep up without blocking fresh predictions.
                self._schedule_reconcile_job(getattr(self.kalshi_client, "get_market", None), now)
            _t["market_reconcile"] = round(time.monotonic() - _t0, 3)
            sent, failed, deliver_alerts, ledger_status = self._deliver_checkpoint_alerts(
                checkpoint, analyses, ranking, canonicals, parent_output,
                result_events, deferred, notifier, now,
            )
            self._dispatch_post_cycle_alerts(
                checkpoint, deliver_alerts, result_events, canonicals, analyses,
                notifier, now, sent, failed, flip_sent, flip_failed,
            )
            self._finalize_cycle_state(
                checkpoint, analyses, ranking, public_map, ledger_status, _t, _rc_start, now,
            )
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

    def _analyse_cycle_assets(
        self, parent_output: dict[str, dict], checkpoint: str, now: float,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict], dict[str, dict[str, Any]],
               dict[str, Any], dict[str, float]]:
        """Per-asset v9.5 analysis over the parent chain output (pure extraction from run_cycle)."""
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
        return analyses, output, public_map, canonicals, _sub

    def _record_cycle_predictions(
        self, checkpoint: str, analyses: dict[str, dict[str, Any]], output: dict[str, dict],
        canonicals: dict[str, Any], ranking: list[dict[str, Any]], notifier: Any, now: float,
        _sub: dict[str, float],
    ) -> tuple[int, int]:
        """Rank-stamp each pick and record predictions/observations (pure extraction from run_cycle)."""
        ranks = {str(row["asset"]): int(row["rank"]) for row in ranking}
        # Learned flip stats (cached against the data version) for this cycle's
        # threshold/flip-probability resolution.
        flip_learned = self.ledger.flip_stats() if _env_bool("Q15_V95_FLIP_RISK_TRACKING", True) else {"available": False}
        flip_sent = flip_failed = 0
        self._manip_candidates = []  # rebuilt every cycle by _process_flip_risk
        _s_record = time.monotonic()
        # Broad-market / cross-asset context for this cycle, computed ONCE from
        # every analysis (read-only shadow factors for the factor lab; recorded
        # in an isolated column, never fed to the champion). Default-ON; the
        # flag is a pure rollback switch.
        shadow_market = (
            cross_asset.compute_market(analyses)
            if _env_bool("Q15_V95_SHADOW_FACTORS_ENABLED", True) else None
        )
        # Experimental shadow-signal config, resolved once per batch (default-OFF).
        _signals_cfg = shadow_signals.SignalConfig.from_env()
        # Strict flip-decision threshold for THIS interval, selected once per
        # cycle from completed post-reset history (chronological OOS). The
        # decision per pick uses it below; never tuned on the rows it grades.
        _flip_cfg = flip_decision.FlipConfig.from_env(checkpoint)
        _flip_sel = self._flip_threshold_selection(checkpoint, _flip_cfg)
        # ONE shared frozen-snapshot id for this interval's batch. Every asset
        # in this cycle is scored from the same `now` freeze and the same data,
        # and BOTH systems (champion + shadow) are recorded from this single
        # record_prediction call — so stamping one id here proves they ran on the
        # same snapshot, same contract list, same information cutoff, same
        # prediction time. The id is locked with the first (INSERT-OR-IGNORE)
        # write; later cycles in the band never overwrite it.
        snapshot_id = f"{checkpoint}@{int(now)}"
        _tmarks = _timing_experiment_marks()
        _tmark_band = _timing_experiment_band() if _tmarks else 0.0
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
            # Additive Eastern-Time display field for the dashboard (the ISO
            # field above stays UTC for storage/DB consistency / API parsing).
            snapshot["q15_v9_5_prediction_timestamp_eastern"] = _eastern_label(now)
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
                # Experimental shadow signals (default-OFF): computed from data
                # already on the analysis/canonical, recorded for the background
                # A/B only. Never touches the champion or the live probability;
                # a computation failure must not break the recording path.
                signals_row = None
                if _signals_cfg is not None and _signals_cfg.enabled:
                    try:
                        signals_row = shadow_signals.compute_signals(analysis, canonical, _signals_cfg)
                        analysis["shadow_signals"] = signals_row
                    except (TypeError, ValueError, KeyError, ArithmeticError) as exc:
                        logger.debug("shadow signal compute skipped for %s: %s", asset, exc)
                        signals_row = None
                prediction_cache_key = (str(canonical.ticker), str(checkpoint))
                prediction_cache = self._prediction_record_cache.get(prediction_cache_key)
                inserted = False
                if prediction_cache is not None:
                    prediction_id = str(
                        prediction_cache.get("prediction_id")
                        or f"{MODEL_VERSION}|{checkpoint}|{canonical.ticker}"
                    )
                else:
                    xfactors_row = (
                        cross_asset.for_asset(asset, analysis, shadow_market)
                        if shadow_market is not None else None
                    )
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
                        shadow_factors=xfactors_row,
                        shadow_signals=signals_row,
                        snapshot_id=snapshot_id,
                    )
                    original_side = str(analysis.get("prediction_side") or "").upper()
                    if not inserted:
                        frozen = self.ledger.frozen_prediction(canonical.ticker, checkpoint)
                        if isinstance(frozen, Mapping) and frozen.get("side"):
                            original_side = str(frozen.get("side") or "").upper()
                    prediction_cache = {
                        "prediction_id": prediction_id,
                        "original_side": original_side,
                        "close_time": canonical.settlement_time,
                        "revision_noted": False,
                    }
                    self._prediction_record_cache[prediction_cache_key] = prediction_cache
                    if len(self._prediction_record_cache) > 512:
                        cutoff = now - 1800.0
                        for _cache_key, _cache_value in list(self._prediction_record_cache.items()):
                            _close = _num(_cache_value.get("close_time"))
                            if _close is not None and _close < cutoff:
                                self._prediction_record_cache.pop(_cache_key, None)
                snapshot["q15_v9_5_snapshot_id"] = snapshot_id
                analysis["snapshot_id"] = snapshot_id
                analysis["prediction_id"] = prediction_id
                analysis["new_unique_prediction_recorded"] = inserted
                # Fresh-near-close manipulation tag (default-ON, observability
                # only). A manipulation tell that FIRST appears at the closing 7M
                # check (not seen at this contract's 15M/10M) is the highest-
                # accuracy manipulation subset on the live record — strongest on
                # the NO side (97.7%), present but weaker on YES (~85%). Surfaced
                # as an alert marker WITH the live per-side hit-rate so the owner
                # can weight NO vs YES; it NEVER alters the frozen probability,
                # edge, or entry decision. Point-in-time: the earlier checkpoints
                # are already recorded, so the lookup has no look-ahead.
                _fresh_side = str(analysis.get("prediction_side") or "").upper()
                if (checkpoint == "7M"
                        and _env_bool("Q15_V95_FRESH_MANIP_TAG", True)
                        and (analysis.get("manipulation") or {}).get("suspected")
                        and _fresh_side in ("YES", "NO")
                        and not self.ledger.manipulation_flagged_before(canonical.ticker, checkpoint)):
                    analysis["fresh_manip_near_close"] = self.ledger.fresh_near_close_rate(_fresh_side)
                # Flag (without mutating the graded prediction) when the live
                # side drifts from the locked one before close — the stability
                # / change-rate metric per interval.
                _current_side = str(analysis.get("prediction_side") or "").upper()
                _original_side = str((prediction_cache or {}).get("original_side") or "").upper()
                if (
                    _current_side in ("YES", "NO")
                    and _original_side in ("YES", "NO")
                    and _current_side != _original_side
                    and not (prediction_cache or {}).get("revision_noted")
                ):
                    self.ledger.note_prediction_revision(
                        ticker=canonical.ticker, checkpoint=checkpoint,
                        current_side=_current_side,
                    )
                    if prediction_cache is not None:
                        prediction_cache["revision_noted"] = True
                # Read-only Polymarket up/down shadow (default-OFF; never
                # affects production). Reuses the champion's frozen snapshot:
                # OUR P(up) is the same structural model re-thresholded at the
                # Polymarket window-open price. observe() only enqueues — the
                # HTTP/DB work happens on the shadow's worker, not this loop.
                try:
                    from q15_upgrade.polymarket.runner import get_runner as _poly_runner
                    _pr = _poly_runner()
                    if _pr is not None:
                        _vol = analysis.get("volatility") or {}
                        _struct = analysis.get("structural") or {}
                        _secs = canonical.seconds_remaining
                        _psd = _struct.get("projected_signed_drift")
                        _orient = 1.0 if canonical.yes_is_higher else -1.0
                        _drift = ((_orient * float(_psd) / _secs)
                                  if (_psd is not None and _secs) else 0.0)
                        _pr.observe(
                            asset=asset, checkpoint=checkpoint, spot=canonical.spot,
                            sigma_per_sqrt_second=_vol.get("sigma_per_sqrt_second"),
                            drift_per_second=_drift, seconds_remaining=_secs,
                            close_time=canonical.settlement_time,
                            snapshot_id=snapshot_id, now=now,
                        )
                except Exception:
                    logger.debug("polymarket shadow observe skipped", exc_info=True)
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
                # OBSERVATIONAL entry-timing experiment: when this cycle sits on
                # a configured extra mark (e.g. 13/12/11 min left) capture the
                # model's call there so we can later MEASURE which entry time
                # crosses into an edge. Never delivered; first write per
                # (contract, mark) wins; graded on settlement. Read-only.
                if _tmarks and seconds_left is not None:
                    for _mark in _tmarks:
                        if abs(seconds_left - _mark) <= _tmark_band:
                            _timing_key = (str(canonical.ticker), int(_mark))
                            if _timing_key not in self._timing_observation_recorded:
                                self.ledger.record_timing_observation(
                                    contract=canonical.ticker, mark_seconds=_mark, asset=asset,
                                    predicted_side=str(analysis.get("prediction_side") or "") or None,
                                    yes_probability=analysis.get("yes_probability"),
                                    selected_probability=analysis.get("selected_probability"),
                                    confidence_grade=analysis.get("confidence_grade"),
                                    created_at=now, close_time=canonical.settlement_time,
                                    snapshot_id=snapshot_id,
                                )
                                self._timing_observation_recorded.add(_timing_key)
                                if len(self._timing_observation_recorded) > 4096:
                                    self._timing_observation_recorded.clear()
                            break
                # STRICT FLIP DECISION (observational): compute the flip
                # probability for this pick, decide YES only when the interval
                # threshold is VALIDATED and the probability clears it, store
                # the decision BEFORE the outcome (one per contract+interval),
                # and stash it for the panel's FLIP CHECK block.
                if _flip_cfg.enabled:
                    # A flip-computation failure must never break the cycle or
                    # the recording path (mirrors the shadow-signal guard).
                    try:
                        _fp = flip_decision.flip_probability(analysis, checkpoint, _flip_cfg)
                        _prob = _fp["probability"]
                        _validated = bool(_flip_sel.get("validated"))
                        _operative_thr = float(_flip_sel.get("threshold", 1.01))
                        _shown_thr = _flip_sel.get("candidate_threshold")
                        if _shown_thr is None:
                            _shown_thr = _operative_thr
                        _decision = "YES" if (_validated and _prob > _operative_thr) else "NO"
                        analysis["flip_decision"] = {
                            "decision": _decision, "flip_probability": _prob,
                            "threshold": _shown_thr, "validated": _validated,
                        }
                        _flip_record_key = (str(canonical.ticker), str(checkpoint))
                        if _flip_record_key not in self._flip_decision_recorded:
                            self.ledger.record_flip_decision(
                                contract=canonical.ticker, checkpoint=checkpoint, asset=asset,
                                predicted_side=str(analysis.get("prediction_side") or "") or None,
                                flip_probability=_prob, threshold=_operative_thr,
                                decision=_decision, validated=_validated,
                                created_at=now, close_time=canonical.settlement_time,
                                snapshot_id=snapshot_id,
                            )
                            self._flip_decision_recorded.add(_flip_record_key)
                            if len(self._flip_decision_recorded) > 2048:
                                self._flip_decision_recorded.clear()
                    except (TypeError, ValueError, KeyError, ArithmeticError) as exc:
                        logger.debug("flip decision skipped for %s %s: %s", asset, checkpoint, exc)

        _sub["record"] = time.monotonic() - _s_record
        return flip_sent, flip_failed

    def _dispatch_research_overlays(self, analyses: dict[str, dict[str, Any]],
                                    canonicals: dict[str, Any], now: float) -> None:
        """Feed the read-only research overlays (pure extraction from run_cycle)."""
        # Q15 MarketLead prospective evidence collector. It derives synchronized
        # venue/index/Kalshi microstructure evidence into a separate DB. It has
        # no notification, policy, or execution surface.
        try:
            from q15_upgrade.marketlead.runner import get_runner as _marketlead_runner
            _marketlead = _marketlead_runner()
            if _marketlead is not None:
                _marketlead.observe(analyses=analyses, canonicals=canonicals, now=now)
        except Exception:
            logger.debug("marketlead observe skipped", exc_info=True)
        # Read-only Ultoim Build research overlay (default-OFF; SEPARATE DB +
        # Telegram channel; never affects production). Reuses the champion's
        # frozen per-asset analyses (with shadow_signals + flip_risk attached
        # above). observe() only extracts compact fields and enqueues — all
        # ranking/grading/DB/Telegram run on Ultoim's own worker thread.
        try:
            from q15_upgrade.ultoim.runner import get_runner as _ultoim_runner
            _ur = _ultoim_runner()
            if _ur is not None:
                _ur.observe(analyses=analyses, canonicals=canonicals, now=now)
        except Exception:
            logger.debug("ultoim observe skipped", exc_info=True)
        # Interval-timing research capture (read-only; default-OFF). Records the
        # frozen analysis at eight marks (15M..7M) into its own ledger for entry/
        # confirmation/defensive-timing study. Never trades, sends, or changes the
        # champion; a failure must not disturb the cycle.
        try:
            from q15_upgrade.interval_research.runner import get_runner as _ir_runner
            _irr = _ir_runner()
            if _irr is not None:
                _irr.observe(analyses=analyses, canonicals=canonicals, now=now)
        except Exception:
            logger.debug("interval-research observe skipped", exc_info=True)
        # Read-only Ultoim V2 paper entry-alert overlay (default-OFF; SEPARATE DB
        # + Telegram channel; never affects production). Reuses the champion's
        # frozen per-asset analyses; observe() only extracts compact fields and
        # enqueues — all gating/recording/DB/Telegram run on V2's own worker
        # thread. A V2 failure must never disturb the cycle.
        try:
            from q15_upgrade.ultoim_v2.runner import get_runner as _ultoim_v2_runner
            _u2r = _ultoim_v2_runner()
            if _u2r is not None:
                _u2r.observe(analyses=analyses, canonicals=canonicals, now=now)
        except Exception:
            logger.debug("ultoim_v2 observe skipped", exc_info=True)
        # High Volatility Flip paper alerts (separate ledger/model; may share
        # the V2 Telegram room by config). It never trades or changes V2.
        try:
            from q15_upgrade.high_vol_flip.runner import get_runner as _hvf_runner
            _hvf = _hvf_runner()
            if _hvf is not None:
                _hvf.observe(analyses=analyses, canonicals=canonicals, now=now)
        except Exception:
            logger.debug("high_vol_flip observe skipped", exc_info=True)

    def _deliver_checkpoint_alerts(
        self, checkpoint: str, analyses: dict[str, dict[str, Any]], ranking: list[dict[str, Any]],
        canonicals: dict[str, Any], parent_output: dict[str, dict],
        result_events: list[Mapping[str, Any]], deferred: _BufferedNotifier, notifier: Any,
        now: float,
    ) -> tuple[int, int, bool, dict[str, Any]]:
        """Resolve delivery gating and send the checkpoint alert/panel (pure extraction from run_cycle)."""
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
        # Whether this resolved checkpoint may DELIVER actionable alerts. 15M
        # delivery defaults OFF (coin-flip, loses money); the prediction was
        # still recorded above for learning. 10M/7M always deliver.
        deliver_alerts = _interval_alerts_enabled(checkpoint)
        # COMPACT PANEL (default ON): one forward-looking V9.5 CHECK panel for
        # the top-ranked pick every checkpoint, with the immutable official
        # record written from the delivered Telegram message_id. The legacy
        # multi-asset entry-only alert is preserved under the flag for rollback.
        if _env_bool("Q15_V95_COMPACT_PANEL", True):
            deferred.suppress_all(generated_message=bool(ranking))
            # RANKED PANEL (default ON): one locked official report per interval
            # carrying the top-3 ranked picks. Falls back to the single-pick
            # compact panel under the flag for rollback.
            if not deliver_alerts:
                sent, failed = 0, 0
            elif _env_bool("Q15_V95_RANKED_PANEL", True):
                sent, failed = self._send_ranked_panel(
                    checkpoint, analyses, ranking, canonicals, parent_output, notifier, now,
                )
            else:
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
            if deliver_alerts and message and consistent and not slot_locked and not no_entry_muted and self._decision_settled(checkpoint, analyses, ranking, parent_output, now):
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
        return sent, failed, deliver_alerts, ledger_status

    def _dispatch_post_cycle_alerts(
        self, checkpoint: str, deliver_alerts: bool, result_events: list[Mapping[str, Any]],
        canonicals: dict[str, Any], analyses: dict[str, dict[str, Any]], notifier: Any,
        now: float, sent: int, failed: int, flip_sent: int, flip_failed: int,
    ) -> None:
        """Manipulation/recap/follow-up dispatch and delivery counters (pure extraction from run_cycle)."""
        # Manipulation alerts: only AFTER the normal check above was delivered,
        # only on high-probability findings that change its recommendation, and
        # combined into one concise alert. Detection ran all cycle regardless.
        # Suppressed entirely on a non-delivering interval (e.g. 15M-off).
        if deliver_alerts:
            ma_sent, ma_failed = self._dispatch_manipulation_alerts(checkpoint, notifier, now)
        else:
            ma_sent, ma_failed = 0, 0
        flip_sent += ma_sent
        flip_failed += ma_failed
        # End-of-cycle recap: one close-out per contract that just settled.
        # Recaps report SETTLED results (not predictions) so they fire on every
        # interval regardless of the alert gate.
        rc_sent, rc_failed = self._send_cycle_recaps(result_events, notifier, now)
        sent += rc_sent
        failed += rc_failed
        # Fire any due follow-up checks (exactly one per contract+interval).
        if deliver_alerts:
            fu_sent, fu_failed = self._dispatch_entry_followups(canonicals, analyses, notifier, now)
        else:
            fu_sent, fu_failed = 0, 0
        flip_sent += fu_sent
        flip_failed += fu_failed
        self._telegram_sent_v95 += sent + flip_sent
        self._telegram_failed_v95 += failed + flip_failed
        self._cycles += 1
        # Reconcile Your System's Shadow-vs-Yours delivery record from the
        # outbox's TRUE status: an official report that failed its synchronous
        # attempt but was delivered by the background worker is now credited
        # SENT (no longer mis-scored as failed), and a pick is marked
        # DELIVERY_FAILED only when its report dead-letters. Cheap; once per
        # cycle; read-only wrt production; never raises into the loop.
        status_lookup = getattr(notifier, "status_by_key", None)
        if callable(status_lookup) and hasattr(self.ledger, "_shadow_reconcile_delivery"):
            self.ledger._shadow_reconcile_delivery(status_lookup)

    def _finalize_cycle_state(
        self, checkpoint: str, analyses: dict[str, dict[str, Any]], ranking: list[dict[str, Any]],
        public_map: dict[str, dict[str, Any]], ledger_status: dict[str, Any],
        _t: dict[str, Any], _rc_start: float, now: float,
    ) -> None:
        """Publish latest-state snapshots and cycle timing diagnostics (pure extraction from run_cycle)."""
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
        if _t["total"] >= threshold:
            # Surface the slow cycle's internal attribution in the LOGS — the
            # watchdog only names the opaque top-level run_cycle stage, and the
            # same breakdown in /api/health is easy to miss live. Throttled so a
            # sustained slow patch surfaces once per window, not every cycle.
            # Diagnostic only; never changes a decision.
            self._throttled_warn(
                "slow_run_cycle",
                "slow run_cycle %s",
                _format_run_cycle_breakdown(_t, getattr(self, "_chain_timing", {})),
                now=now,
            )

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

        # Stable outbox key so the native record can be credited from the outbox's
        # true delivery (sync OR background-worker retry), keyed to the same
        # window basis the lock uses.
        window_basis = close_time if close_time is not None else now
        report_key = f"v95-compact:{checkpoint}:{int(window_basis)}"
        if hasattr(notifier, "send_with_result"):
            result = _send_with_optional_key(notifier, message, report_key)
        else:  # legacy/bare notifier: treat a truthy send as handled, no message_id
            ok = bool(notifier.send(message)) if notifier is not None else False
            result = {"ok": ok, "delivered": ok and getattr(notifier, "last_message_id", None) is not None,
                      "message_id": getattr(notifier, "last_message_id", None)}
        handled = bool(result.get("ok"))
        delivered = bool(result.get("delivered"))
        self.ledger.complete_notification(event_key=permit_key, success=handled, now=now)
        # Durably queued but not synchronously delivered, and not a mute: tag the
        # top pick PENDING under this key so the per-cycle reconcile credits a
        # later worker delivery instead of leaving it silently uncounted.
        if handled and not delivered and not bool(result.get("muted")) and ticker:
            mark_pending = getattr(self.ledger, "_shadow_mark_pending", None)
            if callable(mark_pending):
                mark_pending(str(ticker), str(checkpoint), report_key)

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
            # Your System (native) counts in the visible Shadow-vs-Yours record
            # only once actually delivered before close — mark it here, on a real
            # Telegram delivery. Read-only wrt production.
            self.ledger._shadow_mark_sent(str(ticker), str(checkpoint))
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

    def _send_ranked_panel(self, checkpoint: str, analyses: Mapping[str, Any],
                           ranking: Sequence[Mapping[str, Any]], canonicals: Mapping[str, Any],
                           parent_output: Mapping[str, Any], notifier: Any,
                           now: float) -> tuple[int, int]:
        """Send the OFFICIAL interval report — exactly ONE per (interval, 15-min
        window), carrying the top-3 ranked picks with every field — then write the
        immutable official record for each delivered pick.

        Report-frequency lock (section 1): once an interval's report is delivered
        for a window it is locked; later cycles continue analysing in the background
        but never resend or replace it. The lock is CLAIMED before the send (so a
        second process can't double-send) and RELEASED only if the send did not
        deliver, so a muted/failed send retries next cycle. Returns (sent, failed).
        """
        if not ranking:
            return 0, 0
        if not self._decision_settled(checkpoint, analyses, ranking, parent_output, now):
            return 0, 0
        # Window close basis = the top-ranked pick's contract close (all assets in
        # the checkpoint share the 15-min window). This close time is the ONLY
        # stable key for the one-report-per-window lock: it is fixed for the whole
        # band regardless of wall-clock cycle. If it is unknown this cycle (the
        # canonical for the top asset has not been built yet), DO NOT send — a
        # ``None`` basis falls back to ``now // 900``, which buckets into a
        # different window than the real settlement time and lets a second report
        # fire once the canonical appears. Wait one cycle for the real close so the
        # interval is reported exactly once.
        top_asset = str(ranking[0].get("asset"))
        top_canon = canonicals.get(top_asset)
        window_close = top_canon.settlement_time if top_canon is not None else None
        if window_close is None:
            # The top pick's canonical isn't built yet, so we have no stable window
            # key and must wait a cycle. This is normally transient; log it (throttled
            # to once per 60s) so a top asset that PERSISTENTLY fails to produce a
            # canonical — and so keeps dropping out of the official report — is
            # visible instead of being silently skipped every cycle.
            last_log = getattr(self, "_ranked_skip_log_at", 0.0)
            if now - last_log >= 60.0:
                self._ranked_skip_log_at = now
                logger.info(
                    "%s official report deferred: no canonical/settlement_time for top asset %s yet",
                    checkpoint, top_asset,
                )
            return 0, 0

        # Backstop dedup: refuse to send the SAME interval's official report twice
        # within a minimum gap, regardless of the window key. Legitimate same-
        # interval reports are one per 15-min contract (~900s apart), so a gap below
        # that can only ever block a duplicate (an unstable window key from a
        # contract-mapping flip near the boundary, or a restart re-fire). Default
        # 600s; never blocks the next window's report.
        gap = _env_float("Q15_V95_REPORT_MIN_GAP_SECONDS", 600.0, 0.0, 870.0)
        if gap > 0:
            last_at = self.ledger.last_official_report_at(str(checkpoint))
            if last_at is not None and 0.0 <= (now - last_at) < gap:
                return 0, 0

        if self.ledger.report_locked(str(checkpoint), window_close, now):
            return 0, 0  # already officially reported this interval+window — no resend
        picks = _build_ranked_picks(analyses, ranking, top_k=_RANKED_PICK_COUNT)
        if not picks:
            return 0, 0

        # Claim the lock BEFORE sending (cross-process dedup). If another process
        # already claimed it this window, stand down.
        if not self.ledger.lock_official_report(str(checkpoint), window_close, now, message_id=None):
            return 0, 0

        message = panels_v95.build_ranked_checkpoint_panel(
            checkpoint=checkpoint, picks=picks, top_k=_RANKED_PICK_COUNT)
        # Deterministic outbox key for THIS official report (one per interval+window,
        # matching the once-per-window lock). It lets the Shadow-vs-Yours native
        # record be credited from the outbox's TRUE delivery — a sync OR a
        # background-worker retry — instead of only the synchronous first attempt,
        # which an async, retrying outbox routinely fails (rate limit / worker race).
        report_key = f"v95-official:{checkpoint}:{int(window_close)}"
        if hasattr(notifier, "send_with_result"):
            result = _send_with_optional_key(notifier, message, report_key)
        else:
            ok = bool(notifier.send(message)) if notifier is not None else False
            result = {"ok": ok, "delivered": ok and getattr(notifier, "last_message_id", None) is not None,
                      "message_id": getattr(notifier, "last_message_id", None)}
        handled = bool(result.get("ok"))
        delivered = bool(result.get("delivered"))

        if not delivered:
            # Not an official delivery. Release the claim ONLY when the send was an
            # intentional MUTE (nothing reached Telegram, so a retry is safe and may
            # succeed once config changes). On an ambiguous FAILURE (HTTP timeout /
            # 429 rate-limit) the message may well have reached Telegram, so we KEEP
            # the lock — releasing it caused a resend-every-cycle loop that the owner
            # saw as one duplicate report per minute. One attempt per window.
            if bool(result.get("muted")):
                self.ledger.unlock_official_report(str(checkpoint), window_close, now)
            else:
                # A non-mute, synchronously-undelivered send is NOT a failure: the
                # report is durably queued in the outbox and the background worker
                # will retry it (that worker is what actually delivers most reports).
                # Tag each generated pick PENDING under this report's key; the true
                # outcome — SENT on a real worker delivery, or DELIVERY_FAILED only
                # when the outbox dead-letters — is resolved by reconcile_native_delivery
                # each cycle. This is the fix for "0 sent · N failed" while reports
                # are in fact being received. Retry stays governed by the lock/min-gap.
                mark_pending = getattr(self.ledger, "_shadow_mark_pending", None)
                if callable(mark_pending):
                    for pick in picks:
                        p_asset = str(pick.get("asset"))
                        p_canon = canonicals.get(p_asset)
                        p_ticker = p_canon.ticker if p_canon is not None else (analyses.get(p_asset) or {}).get("ticker")
                        if p_ticker:
                            mark_pending(str(p_ticker), str(checkpoint), report_key)
            if handled:
                self._throttled_warn(
                    f"ranked_handled_not_delivered:{checkpoint}",
                    "v95 ranked report handled but not delivered (no message_id) for "
                    "checkpoint=%s — no official record written", checkpoint, now=now,
                )
            return 0, (0 if handled else 1)

        mid = result.get("message_id")
        # The report WAS delivered: the normal check for this interval reached the
        # owner (manipulation-alert gate condition 2). Headline = the top pick.
        top_analysis = analyses.get(top_asset) or {}
        self._normal_check[str(checkpoint)] = manipulation_alert.NormalCheck(
            checkpoint=str(checkpoint), delivered=True, asset=top_asset,
            side=top_analysis.get("prediction_side"),
            action=str(top_analysis.get("trade_decision") or "") or None, at=now,
        )

        entry_armed = False
        for pick in picks:
            asset = str(pick["asset"])
            analysis = analyses.get(asset) or {}
            canon = canonicals.get(asset)
            ticker = canon.ticker if canon is not None else analysis.get("ticker")
            close_time = canon.settlement_time if canon is not None else None
            if not ticker:
                continue
            decision = str(analysis.get("trade_decision") or "")
            manip_prob = pick.get("manipulation_prob")
            # Your System counts this delivered pick in the visible shadow record.
            self.ledger._shadow_mark_sent(str(ticker), str(checkpoint))
            self.ledger.record_sent_prediction(
                contract_id=str(ticker), asset=asset, interval=checkpoint, record_type="interval",
                predicted_side=analysis.get("prediction_side"), probability=analysis.get("yes_probability"),
                manipulation_probability=manip_prob, entry_decision=decision,
                sent_at=now, close_time=close_time, message_id=mid,
            )
            if _is_actionable_entry(analysis):
                self.ledger.record_sent_prediction(
                    contract_id=str(ticker), asset=asset, interval=checkpoint, record_type="entry",
                    predicted_side=analysis.get("prediction_side"), probability=analysis.get("yes_probability"),
                    manipulation_probability=manip_prob, entry_decision=decision,
                    sent_at=now, close_time=close_time, message_id=mid,
                )
                # One active entry per timeframe: arm slot/follow-up for the
                # highest-ranked entry only (the rest are recorded, not armed).
                if not entry_armed:
                    entry_armed = True
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
        return 1, 0

    @staticmethod
    def _recap_close_label(close_time: Any) -> str:
        try:
            if close_time is None:
                return ""
            # Visible close label in Eastern Time (EDT/EST), e.g. "14:30 EDT".
            from .timez import fmt_eastern_hm
            return fmt_eastern_hm(float(close_time))
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
        # Default OFF: on the live official record the delivered manipulation alert
        # was directionally WRONG (23.7% correct, n=59, Wilson CI excludes 50% on
        # the low side) — an anti-signal. Detection/tracking still runs for the
        # learning record; only the standalone alert delivery is suppressed.
        if not candidates or not _env_bool("Q15_V95_MANIPULATION_ALERTS_ENABLED", False):
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

    def shadow_signal_experiment(self) -> dict[str, Any]:
        """Background A/B for the five experimental signals: out-of-sample Brier
        change vs the champion, with significance. Read-only; promotion is manual."""
        return {"version": VERSION, "read_only": True, **self.ledger.shadow_signal_experiment()}

    def accuracy_report(self) -> dict[str, Any]:
        """Honest accuracy / promotion-readiness readout over the ledger metrics."""
        from .accuracy_report import build_accuracy_report
        return {"version": VERSION, "read_only": True, **build_accuracy_report(self.ledger.metrics())}

    def accuracy_summary(self) -> dict[str, Any]:
        """Compact one-glance accuracy headline for /api/health."""
        from .accuracy_report import build_accuracy_report, compact_summary
        cached = self._accuracy_summary_cache
        if cached is not None and (time.monotonic() - self._accuracy_summary_cache_at) < _HEALTH_SUMMARY_CACHE_TTL_SECONDS:
            return copy.deepcopy(cached)
        summary = compact_summary(build_accuracy_report(self.ledger.metrics()))
        self._accuracy_summary_cache = copy.deepcopy(summary)
        self._accuracy_summary_cache_at = time.monotonic()
        return summary

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

    def health_compact(
        self,
        *,
        ledger_status: Mapping[str, Any] | None = None,
        grading_status: Mapping[str, Any] | None = None,
        public_market_data: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fast health block for the top-level HTTP health endpoint.

        The full policy health walks every parent policy layer and re-reads the
        ledger. That is useful for diagnostics, but too expensive for the liveness
        endpoint while the live cycle is also holding ledger locks.
        """
        ledger = copy.deepcopy(ledger_status) if ledger_status is not None else self.ledger.status()
        grading = copy.deepcopy(grading_status) if grading_status is not None else self.ledger.reconcile_backlog_status()
        market_data = copy.deepcopy(public_market_data) if public_market_data is not None else self.market_data.health()
        try:
            from q15_upgrade.marketlead.runner import get_runner as _marketlead_runner

            marketlead_runner = _marketlead_runner()
            marketlead = marketlead_runner.status() if marketlead_runner is not None else {"enabled": False}
        except Exception as exc:
            marketlead = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
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
            "ledger": ledger,
            "grading": grading,
            "public_market_data": market_data,
            "marketlead": marketlead,
        }

    def health(self) -> dict[str, Any]:
        try:
            parent = super().health()
        except Exception as exc:
            parent = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
        return {
            **self.health_compact(),
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
            # Flip-risk / manipulation learning track: the learned flip-rate-by-risk
            # curves + thresholds (per checkpoint/direction/asset) and the fired-warning
            # performance. Read-only aggregates so a single snapshot captures BOTH the
            # manipulation-reliability scoreboard (metrics.scoreboard.by_manipulation)
            # and the flip-risk learning that decision_stats previously omitted.
            "flip_learning": {
                "stats": self.ledger.flip_stats(),
                "warning_performance": self.ledger.flip_warning_performance(),
            },
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
