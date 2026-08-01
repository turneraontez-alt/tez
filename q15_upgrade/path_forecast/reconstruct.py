"""Reconstruct point-in-time path examples from durable local collectors.

Every feature is computed from observations at or before the requested
checkpoint. Labels and trajectory targets use later observations and are kept
outside the feature vector. Cross-asset rows sharing a close time are grouped
by the trainer so a chronological split cannot leak one asset's future into
another asset's training data.
"""
from __future__ import annotations

from dataclasses import dataclass
import gzip
import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np


FEATURE_SCHEMA_VERSION = "q15-path-forecast-features-v1"
LABEL_POLICY_VERSION = "q15-path-archetypes-frozen-20260715-v1"
CHECKPOINT_SECONDS = (780, 600, 420, 300, 120)
MIN_OBSERVED_SECONDS = 60.0
TRAJECTORY_FRACTIONS = (0.25, 0.50, 0.75, 1.00)
ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
ARCHETYPES = (
    "chop",
    "dip_recovery",
    "hard_reversal",
    "spike_fade",
    "steady_down",
    "steady_up",
)

_BASE_FEATURES = (
    "checkpoint_fraction",
    "observed_seconds",
    "decision_age_seconds",
    "max_observed_gap_seconds",
    "distance_to_strike_bps",
    "return_from_open_bps",
    "runup_from_open_bps",
    "drawdown_from_open_bps",
    "slope_15s_bps_per_min",
    "slope_30s_bps_per_min",
    "slope_60s_bps_per_min",
    "slope_120s_bps_per_min",
    "vol_30s_bps_sqrt_min",
    "vol_60s_bps_sqrt_min",
    "vol_120s_bps_sqrt_min",
    "range_30s_bps",
    "range_60s_bps",
    "range_120s_bps",
    "strike_cross_count",
    "fraction_above_strike",
    "seconds_since_strike_cross",
    "yes_mid",
    "yes_move_15s",
    "yes_move_30s",
    "yes_move_60s",
    "yes_move_120s",
    "yes_vol_60s",
    "yes_distance_from_50",
    "rti_market_side_agreement",
)
FEATURE_NAMES = _BASE_FEATURES + tuple(f"asset_{asset}" for asset in ASSETS)


@dataclass(frozen=True)
class PathMetadata:
    asset: str
    close_time: float
    ticker: str
    target_px: float
    official_result: str | None


@dataclass(frozen=True)
class PathExample:
    asset: str
    close_time: float
    ticker: str
    checkpoint_seconds: int
    decision_time: float
    target_px: float
    current_px: float
    current_yes_mid: float | None
    features: np.ndarray
    archetype: str
    official_yes: int
    strike_crossed: int
    turn_delay_seconds: float | None
    trajectory_returns_bps: np.ndarray
    label_scale_bps: float


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def canonical_close_time(value: float) -> float:
    """Map small runtime close-time offsets to the canonical 15-minute close."""
    return float(int(math.floor((float(value) + 450.0) / 900.0)) * 900)


def _load_metadata(db_path: str) -> dict[tuple[str, float], PathMetadata]:
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"metadata database not found: {path}")
    conn = sqlite3.connect(str(path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    selected: dict[tuple[str, float], tuple[float, PathMetadata]] = {}
    try:
        rows = conn.execute(
            "SELECT asset, close_time, created_at, ticker, quote_json, official_result "
            "FROM predictions WHERE checkpoint='15M' ORDER BY created_at"
        )
        for row in rows:
            asset = str(row["asset"] or "").upper()
            close = _finite(row["close_time"])
            created_at = _finite(row["created_at"])
            if asset not in ASSETS or close is None or created_at is None:
                continue
            try:
                quote = json.loads(str(row["quote_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            target = _finite(quote.get("target") if isinstance(quote, Mapping) else None)
            if target is None or target <= 0.0:
                continue
            official = str(row["official_result"] or "").upper()
            official = official if official in {"YES", "NO"} else None
            canonical = canonical_close_time(close)
            key = (asset, canonical)
            prior = selected.get(key)
            item = PathMetadata(
                asset=asset,
                close_time=canonical,
                ticker=str(row["ticker"] or ""),
                target_px=target,
                official_result=(
                    official
                    if official is not None or prior is None
                    else prior[1].official_result
                ),
            )
            if prior is None or created_at >= prior[0]:
                selected[key] = (created_at, item)
    finally:
        conn.close()
    return {key: value[1] for key, value in selected.items()}


def _clean_points(blob: bytes) -> list[dict[str, float | str | None]]:
    try:
        raw = json.loads(gzip.decompress(blob))
    except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid compressed path payload: {exc}") from exc
    if not isinstance(raw, list):
        raise ValueError("path payload must be a list")
    by_ts: dict[float, dict[str, float | str | None]] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        ts = _finite(item.get("ts"))
        px = _finite(item.get("px"))
        if ts is None or px is None or px <= 0.0:
            continue
        yes_mid = _finite(item.get("yes_mid"))
        if yes_mid is not None and not 0.0 <= yes_mid <= 100.0:
            yes_mid = None
        by_ts[ts] = {
            "ts": ts,
            "px": px,
            "yes_mid": yes_mid,
            "px_source": str(item.get("px_source") or ""),
        }
    return [by_ts[ts] for ts in sorted(by_ts)]


def _window(points: Sequence[Mapping[str, Any]], end_ts: float, seconds: float) -> list[Mapping[str, Any]]:
    start = end_ts - float(seconds)
    return [point for point in points if start <= float(point["ts"]) <= end_ts]


def _return_bps(start: float, end: float) -> float:
    return 10_000.0 * (float(end) / float(start) - 1.0)


def _slope(points: Sequence[Mapping[str, Any]], end_ts: float, seconds: int) -> float:
    rows = _window(points, end_ts, seconds)
    if len(rows) < 2:
        return math.nan
    elapsed = float(rows[-1]["ts"]) - float(rows[0]["ts"])
    if elapsed <= 0.0:
        return math.nan
    return _return_bps(float(rows[0]["px"]), float(rows[-1]["px"])) / (elapsed / 60.0)


def _volatility(points: Sequence[Mapping[str, Any]], end_ts: float, seconds: int) -> float:
    rows = _window(points, end_ts, seconds)
    rates: list[float] = []
    for left, right in zip(rows, rows[1:]):
        elapsed = float(right["ts"]) - float(left["ts"])
        if elapsed <= 0.0:
            continue
        change = _return_bps(float(left["px"]), float(right["px"]))
        rates.append(change / math.sqrt(elapsed))
    if len(rates) < 2:
        return math.nan
    return float(np.std(np.asarray(rates, dtype=float), ddof=1) * math.sqrt(60.0))


def _range(points: Sequence[Mapping[str, Any]], end_ts: float, seconds: int) -> float:
    rows = _window(points, end_ts, seconds)
    if len(rows) < 2:
        return math.nan
    values = [float(row["px"]) for row in rows]
    return _return_bps(min(values), max(values))


def _last_yes(points: Sequence[Mapping[str, Any]]) -> float | None:
    for point in reversed(points):
        value = _finite(point.get("yes_mid"))
        if value is not None:
            return value
    return None


def _yes_move(points: Sequence[Mapping[str, Any]], end_ts: float, seconds: int) -> float:
    rows = _window(points, end_ts, seconds)
    values = [(float(row["ts"]), _finite(row.get("yes_mid"))) for row in rows]
    values = [(ts, value) for ts, value in values if value is not None]
    if len(values) < 2:
        return math.nan
    return float(values[-1][1]) - float(values[0][1])


def _yes_vol(points: Sequence[Mapping[str, Any]], end_ts: float, seconds: int) -> float:
    rows = _window(points, end_ts, seconds)
    values = [float(value) for value in (_finite(row.get("yes_mid")) for row in rows) if value is not None]
    if len(values) < 3:
        return math.nan
    return float(np.std(np.diff(np.asarray(values, dtype=float)), ddof=1))


def _crossing_stats(points: Sequence[Mapping[str, Any]], target: float, decision_ts: float) -> tuple[int, float, float]:
    sides = [float(point["px"]) >= target for point in points]
    crosses = [idx for idx in range(1, len(sides)) if sides[idx] != sides[idx - 1]]
    fraction_above = float(sum(sides) / len(sides)) if sides else math.nan
    since = decision_ts - float(points[crosses[-1]]["ts"]) if crosses else decision_ts - float(points[0]["ts"])
    return len(crosses), fraction_above, max(0.0, since)


def build_feature_vector(
    *,
    asset: str,
    checkpoint_seconds: int,
    decision_time: float,
    target_px: float,
    observed_points: Sequence[Mapping[str, Any]],
) -> tuple[np.ndarray, float, float | None, dict[str, float]]:
    """Build an immutable feature vector using observed points only."""
    if asset not in ASSETS:
        raise ValueError(f"unsupported asset: {asset}")
    if len(observed_points) < 2:
        raise ValueError("at least two observed points are required")
    current = observed_points[-1]
    current_px = float(current["px"])
    open_px = float(observed_points[0]["px"])
    observed_values = [float(point["px"]) for point in observed_points]
    times = [float(point["ts"]) for point in observed_points]
    gaps = np.diff(np.asarray(times, dtype=float))
    crosses, fraction_above, since_cross = _crossing_stats(observed_points, target_px, decision_time)
    yes_mid = _last_yes(observed_points)
    rti_side = current_px >= target_px
    market_side = None if yes_mid is None else yes_mid >= 50.0
    agreement = math.nan if market_side is None else (1.0 if market_side == rti_side else 0.0)
    values = [
        float(checkpoint_seconds) / 900.0,
        times[-1] - times[0],
        max(0.0, decision_time - times[-1]),
        float(np.max(gaps)) if gaps.size else 0.0,
        _return_bps(target_px, current_px),
        _return_bps(open_px, current_px),
        _return_bps(open_px, max(observed_values)),
        _return_bps(open_px, min(observed_values)),
        *[_slope(observed_points, decision_time, seconds) for seconds in (15, 30, 60, 120)],
        *[_volatility(observed_points, decision_time, seconds) for seconds in (30, 60, 120)],
        *[_range(observed_points, decision_time, seconds) for seconds in (30, 60, 120)],
        float(crosses),
        fraction_above,
        since_cross,
        math.nan if yes_mid is None else yes_mid,
        *[_yes_move(observed_points, decision_time, seconds) for seconds in (15, 30, 60, 120)],
        _yes_vol(observed_points, decision_time, 60),
        math.nan if yes_mid is None else yes_mid - 50.0,
        agreement,
        *[1.0 if asset == candidate else 0.0 for candidate in ASSETS],
    ]
    if len(values) != len(FEATURE_NAMES):
        raise RuntimeError("feature schema length mismatch")
    diagnostics = {
        "observed_seconds": times[-1] - times[0],
        "decision_age_seconds": max(0.0, decision_time - times[-1]),
        "max_observed_gap_seconds": float(np.max(gaps)) if gaps.size else 0.0,
    }
    return np.asarray(values, dtype=float), current_px, yes_mid, diagnostics


def _label_scale(observed_points: Sequence[Mapping[str, Any]], remaining_seconds: float) -> float:
    rates: list[float] = []
    for left, right in zip(observed_points, observed_points[1:]):
        elapsed = float(right["ts"]) - float(left["ts"])
        if elapsed <= 0.0:
            continue
        rates.append(_return_bps(float(left["px"]), float(right["px"])) / math.sqrt(elapsed))
    if len(rates) < 3:
        return 2.0
    median = float(np.median(rates))
    mad = float(np.median(np.abs(np.asarray(rates, dtype=float) - median)))
    robust_sigma = 1.4826 * mad
    return max(2.0, robust_sigma * math.sqrt(min(180.0, max(60.0, remaining_seconds))))


def _interpolate_returns(
    points: Sequence[Mapping[str, Any]],
    *,
    decision_time: float,
    close_time: float,
    current_px: float,
) -> np.ndarray:
    times = np.asarray([float(point["ts"]) for point in points], dtype=float)
    prices = np.asarray([float(point["px"]) for point in points], dtype=float)
    targets = np.asarray(
        [decision_time + fraction * (close_time - decision_time) for fraction in TRAJECTORY_FRACTIONS],
        dtype=float,
    )
    interpolated = np.interp(targets, times, prices)
    return 10_000.0 * (interpolated / current_px - 1.0)


def label_future_path(
    *,
    observed_points: Sequence[Mapping[str, Any]],
    future_points: Sequence[Mapping[str, Any]],
    decision_time: float,
    close_time: float,
    target_px: float,
) -> tuple[str, int, float | None, np.ndarray, float]:
    """Apply the frozen archetype policy to future observations."""
    if not observed_points or not future_points:
        raise ValueError("observed and future points are required")
    current_px = float(observed_points[-1]["px"])
    returns = np.asarray([_return_bps(current_px, float(point["px"])) for point in future_points], dtype=float)
    scale = _label_scale(observed_points, close_time - decision_time)
    terminal = float(returns[-1])
    minimum = float(np.min(returns))
    maximum = float(np.max(returns))
    observed_slope = _slope(observed_points, decision_time, 60)
    trend_threshold = 0.25 * scale
    observed_direction = 1 if observed_slope >= trend_threshold else (-1 if observed_slope <= -trend_threshold else 0)

    turn_delay: float | None = None
    if observed_direction > 0 and terminal <= -scale:
        archetype = "hard_reversal"
        trigger = next((idx for idx, value in enumerate(returns) if value <= -0.5 * scale), int(np.argmin(returns)))
        turn_delay = max(0.0, float(future_points[trigger]["ts"]) - decision_time)
    elif observed_direction < 0 and terminal >= scale:
        archetype = "hard_reversal"
        trigger = next((idx for idx, value in enumerate(returns) if value >= 0.5 * scale), int(np.argmax(returns)))
        turn_delay = max(0.0, float(future_points[trigger]["ts"]) - decision_time)
    elif minimum <= -scale and terminal - minimum >= scale and terminal >= -0.25 * scale:
        archetype = "dip_recovery"
        turn_delay = max(0.0, float(future_points[int(np.argmin(returns))]["ts"]) - decision_time)
    elif maximum >= scale and maximum - terminal >= scale and terminal <= 0.25 * scale:
        archetype = "spike_fade"
        turn_delay = max(0.0, float(future_points[int(np.argmax(returns))]["ts"]) - decision_time)
    elif terminal >= scale and minimum > -0.5 * scale:
        archetype = "steady_up"
    elif terminal <= -scale and maximum < 0.5 * scale:
        archetype = "steady_down"
    else:
        archetype = "chop"

    current_side = current_px >= target_px
    strike_crossed = int(any((float(point["px"]) >= target_px) != current_side for point in future_points))
    trajectory = _interpolate_returns(
        [observed_points[-1], *future_points],
        decision_time=decision_time,
        close_time=close_time,
        current_px=current_px,
    )
    return archetype, strike_crossed, turn_delay, trajectory, scale


def build_live_features(
    *,
    asset: str,
    close_time: float,
    checkpoint_seconds: int,
    target_px: float,
    points: Sequence[Mapping[str, Any]],
    max_decision_age_seconds: float = 12.0,
    max_gap_seconds: float = 30.0,
    min_observed_seconds: float = MIN_OBSERVED_SECONDS,
) -> tuple[np.ndarray, float, float | None, dict[str, float]]:
    """Build the exact feature contract used by offline reconstruction."""
    decision_time = close_time - float(checkpoint_seconds)
    observed = [point for point in points if float(point["ts"]) <= decision_time]
    if len(observed) < 8:
        raise ValueError("insufficient observed path points")
    vector, current_px, yes_mid, diagnostics = build_feature_vector(
        asset=asset,
        checkpoint_seconds=checkpoint_seconds,
        decision_time=decision_time,
        target_px=target_px,
        observed_points=observed,
    )
    if diagnostics["decision_age_seconds"] > max_decision_age_seconds:
        raise ValueError("checkpoint observation is stale")
    if diagnostics["observed_seconds"] < min_observed_seconds:
        raise ValueError("insufficient observed path duration")
    if diagnostics["max_observed_gap_seconds"] > max_gap_seconds:
        raise ValueError("observed path contains an excessive gap")
    return vector, current_px, yes_mid, diagnostics


def reconstruct_examples(
    *,
    path_db: str,
    metadata_db: str,
    checkpoints: Iterable[int] = CHECKPOINT_SECONDS,
    max_decision_age_seconds: float = 12.0,
    max_end_age_seconds: float = 15.0,
    max_gap_seconds: float = 30.0,
    min_observed_seconds: float = MIN_OBSERVED_SECONDS,
) -> tuple[list[PathExample], dict[str, Any]]:
    """Reconstruct historical examples and return explicit rejection counts."""
    metadata = _load_metadata(metadata_db)
    path = Path(path_db)
    if not path.exists():
        raise FileNotFoundError(f"path database not found: {path}")
    conn = sqlite3.connect(str(path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    examples: list[PathExample] = []
    rejected: dict[str, int] = {}
    rows_seen = 0

    def reject(reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1

    try:
        for row in conn.execute(
            "SELECT asset, close_time, point_count, path_json_gz FROM window_paths ORDER BY close_time, asset"
        ):
            rows_seen += 1
            asset = str(row["asset"] or "").upper()
            close = canonical_close_time(float(row["close_time"]))
            meta = metadata.get((asset, close))
            if meta is None:
                reject("metadata_missing")
                continue
            if meta.official_result not in {"YES", "NO"}:
                reject("official_result_missing")
                continue
            try:
                points = _clean_points(row["path_json_gz"])
            except ValueError:
                reject("path_payload_invalid")
                continue
            if len(points) < 10:
                reject("path_too_short")
                continue
            end_age = close - float(points[-1]["ts"])
            if end_age < -2.0 or end_age > max_end_age_seconds:
                reject("path_end_stale")
                continue
            for checkpoint in checkpoints:
                decision_time = close - float(checkpoint)
                observed = [point for point in points if float(point["ts"]) <= decision_time]
                future = [point for point in points if float(point["ts"]) > decision_time]
                if len(observed) < 8 or len(future) < 8:
                    reject(f"checkpoint_{checkpoint}_coverage")
                    continue
                try:
                    vector, current_px, yes_mid, diagnostics = build_feature_vector(
                        asset=asset,
                        checkpoint_seconds=int(checkpoint),
                        decision_time=decision_time,
                        target_px=meta.target_px,
                        observed_points=observed,
                    )
                except ValueError:
                    reject(f"checkpoint_{checkpoint}_feature_invalid")
                    continue
                if diagnostics["decision_age_seconds"] > max_decision_age_seconds:
                    reject(f"checkpoint_{checkpoint}_stale")
                    continue
                if diagnostics["observed_seconds"] < min_observed_seconds:
                    reject(f"checkpoint_{checkpoint}_observed_duration")
                    continue
                if diagnostics["max_observed_gap_seconds"] > max_gap_seconds:
                    reject(f"checkpoint_{checkpoint}_gap")
                    continue
                try:
                    archetype, crossed, turn_delay, trajectory, scale = label_future_path(
                        observed_points=observed,
                        future_points=future,
                        decision_time=decision_time,
                        close_time=close,
                        target_px=meta.target_px,
                    )
                except ValueError:
                    reject(f"checkpoint_{checkpoint}_label_invalid")
                    continue
                examples.append(PathExample(
                    asset=asset,
                    close_time=close,
                    ticker=meta.ticker,
                    checkpoint_seconds=int(checkpoint),
                    decision_time=decision_time,
                    target_px=meta.target_px,
                    current_px=current_px,
                    current_yes_mid=yes_mid,
                    features=vector,
                    archetype=archetype,
                    official_yes=1 if meta.official_result == "YES" else 0,
                    strike_crossed=crossed,
                    turn_delay_seconds=turn_delay,
                    trajectory_returns_bps=trajectory,
                    label_scale_bps=scale,
                ))
    finally:
        conn.close()
    coverage = {
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "label_policy_version": LABEL_POLICY_VERSION,
        "path_rows_seen": rows_seen,
        "metadata_rows": len(metadata),
        "examples": len(examples),
        "unique_windows": len({example.close_time for example in examples}),
        "by_asset": {asset: sum(example.asset == asset for example in examples) for asset in ASSETS},
        "by_checkpoint": {
            str(checkpoint): sum(example.checkpoint_seconds == checkpoint for example in examples)
            for checkpoint in checkpoints
        },
        "rejected": dict(sorted(rejected.items())),
    }
    return examples, coverage


def examples_to_arrays(examples: Sequence[PathExample]) -> dict[str, np.ndarray]:
    if not examples:
        raise ValueError("no path examples supplied")
    return {
        "X": np.vstack([example.features for example in examples]),
        "archetype": np.asarray([example.archetype for example in examples], dtype=str),
        "official_yes": np.asarray([example.official_yes for example in examples], dtype=int),
        "strike_crossed": np.asarray([example.strike_crossed for example in examples], dtype=int),
        "turn_delay": np.asarray([
            math.nan if example.turn_delay_seconds is None else example.turn_delay_seconds
            for example in examples
        ], dtype=float),
        "trajectory": np.vstack([example.trajectory_returns_bps for example in examples]),
        "close_time": np.asarray([example.close_time for example in examples], dtype=float),
        "checkpoint": np.asarray([example.checkpoint_seconds for example in examples], dtype=int),
        "asset": np.asarray([example.asset for example in examples], dtype=str),
        "current_yes_mid": np.asarray([
            math.nan if example.current_yes_mid is None else example.current_yes_mid
            for example in examples
        ], dtype=float),
        "current_rti_yes": np.asarray([
            int(example.current_px >= example.target_px) for example in examples
        ], dtype=int),
    }
