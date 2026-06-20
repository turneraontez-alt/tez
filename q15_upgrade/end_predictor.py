from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Sequence

from .learning_math import clamp, probability_clip


def _num(value, default=None):
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _phi(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _candle_close(candle: dict) -> float | None:
    return _num(candle.get("close"))


def _candle_time(candle: dict, fallback: float) -> float:
    for key in ("start_time", "ts", "timestamp", "bucket"):
        value = candle.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
    return fallback


def _series(candles: Sequence[dict]) -> list[tuple[float, float]]:
    result = []
    for index, candle in enumerate(candles or []):
        close = _candle_close(candle)
        if close is None or close <= 0:
            continue
        result.append((_candle_time(candle, index * 5.0), close))
    result.sort(key=lambda item: item[0])
    return result


def _linear_slope(points: Sequence[tuple[float, float]]) -> float:
    if len(points) < 3:
        return 0.0
    origin = points[0][0]
    xs = [point[0] - origin for point in points]
    ys = [math.log(point[1]) for point in points]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator <= 1e-12:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator


def _log_return_sigma(points: Sequence[tuple[float, float]]) -> float:
    if len(points) < 4:
        return 0.0
    per_second = []
    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        dt = max(0.25, t1 - t0)
        if p0 > 0 and p1 > 0:
            per_second.append(math.log(p1 / p0) / math.sqrt(dt))
    if len(per_second) < 3:
        return 0.0
    mean = sum(per_second) / len(per_second)
    variance = sum((value - mean) ** 2 for value in per_second) / max(1, len(per_second) - 1)
    return math.sqrt(max(0.0, variance))


def _ema(values: Sequence[float], period: int) -> float | None:
    if not values:
        return None
    alpha = 2.0 / (max(1, period) + 1.0)
    value = float(values[0])
    for item in values[1:]:
        value = alpha * float(item) + (1.0 - alpha) * value
    return value


def _rsi(values: Sequence[float], period: int = 14) -> float | None:
    if len(values) < period + 1:
        return None
    gains = []
    losses = []
    for prior, current in zip(values[-(period + 1):-1], values[-period:]):
        change = current - prior
        gains.append(max(0.0, change))
        losses.append(max(0.0, -change))
    average_gain = sum(gains) / period
    average_loss = sum(losses) / period
    if average_loss <= 1e-12:
        return 100.0
    relative_strength = average_gain / average_loss
    return 100.0 - 100.0 / (1.0 + relative_strength)


@dataclass
class EndPrediction:
    version: str
    predicted_result: str
    chart_yes_probability: float | None
    blended_yes_probability: float | None
    confidence: float
    predicted_final_underlying: float | None
    predicted_range_low: float | None
    predicted_range_high: float | None
    current_underlying: float | None
    target_value: float | None
    distance_to_target_pct: float | None
    trend_30s_pct: float | None
    trend_60s_pct: float | None
    trend_180s_pct: float | None
    fast_ema: float | None
    slow_ema: float | None
    rsi_14: float | None
    volatility_per_sqrt_second: float | None
    chart_bias: str
    agreement_score: float
    history_points: int
    seconds_remaining: float | None
    warning: str

    def as_dict(self) -> dict:
        return asdict(self)


class EndResultPredictor:
    """Chart-based settlement predictor.

    This estimates whether the underlying finishes above or below the strike at
    market close. It is deliberately separate from the entry/scalp decision and
    never claims certainty.
    """

    VERSION = "q15-chart-end-v2"

    def __init__(self, config):
        self.config = config

    def predict(self, snapshot: dict) -> EndPrediction:
        candles = snapshot.get("underlying_candles_5s") or []
        points = _series(candles)
        current = _num(snapshot.get("underlying_current"))
        target = _num(snapshot.get("target_value"))
        seconds = _num(snapshot.get("seconds_remaining"))
        market_mid = self._market_mid(snapshot)
        if current is None and points:
            current = points[-1][1]

        if current is None or target is None or current <= 0 or target <= 0 or not points:
            return EndPrediction(
                version=self.VERSION,
                predicted_result="UNAVAILABLE",
                chart_yes_probability=None,
                blended_yes_probability=None,
                confidence=0.0,
                predicted_final_underlying=None,
                predicted_range_low=None,
                predicted_range_high=None,
                current_underlying=current,
                target_value=target,
                distance_to_target_pct=None,
                trend_30s_pct=None,
                trend_60s_pct=None,
                trend_180s_pct=None,
                fast_ema=None,
                slow_ema=None,
                rsi_14=None,
                volatility_per_sqrt_second=None,
                chart_bias="unavailable",
                agreement_score=0.0,
                history_points=len(points),
                seconds_remaining=seconds,
                warning="Insufficient chart history or strike data; not a trading instruction.",
            )

        closes = [value for _, value in points]
        slopes = {}
        trends = {}
        for label, window in (("30s", 30.0), ("60s", 60.0), ("180s", 180.0)):
            subset = self._window(points, window)
            slope = _linear_slope(subset)
            slopes[label] = slope
            elapsed = max(1.0, subset[-1][0] - subset[0][0]) if len(subset) >= 2 else window
            trends[label] = (math.exp(slope * elapsed) - 1.0) * 100.0

        weighted_slope = 0.50 * slopes["30s"] + 0.32 * slopes["60s"] + 0.18 * slopes["180s"]
        sigma = _log_return_sigma(self._window(points, 180.0))
        remaining = max(1.0, seconds or 1.0)

        fast_ema = _ema(closes[-30:], 6)
        slow_ema = _ema(closes[-60:], 18)
        rsi = _rsi(closes, 14)
        ema_bias = 0.0
        if fast_ema and slow_ema and slow_ema > 0:
            ema_bias = math.log(fast_ema / slow_ema) / max(remaining, 30.0)
        # Q15_V5_DIMENSIONAL_DRIFT: sigma is per sqrt(second), while
        # slope is per second. Convert bounded z-bias into drift units.
        rsi_z = 0.0
        if rsi is not None:
            rsi_z = clamp((rsi - 50.0) / 25.0, -1.0, 1.0) * 0.12
        rsi_bias = rsi_z * sigma / math.sqrt(remaining)

        raw_drift = weighted_slope + ema_bias + rsi_bias
        max_drift = 0.65 * sigma / math.sqrt(remaining)
        projected_drift = clamp(raw_drift, -max_drift, max_drift)
        projected_final = current * math.exp(projected_drift * remaining)
        total_sigma = max(1e-8, sigma * math.sqrt(remaining))
        z = math.log(projected_final / target) / total_sigma
        chart_probability = clamp(_phi(z), 0.01, 0.99)

        blend = float(getattr(self.config, "end_prediction_market_blend", 0.15))
        blended = chart_probability
        if market_mid is not None:
            blended = clamp((1.0 - blend) * chart_probability + blend * market_mid, 0.01, 0.99)

        range_low = projected_final * math.exp(-1.2816 * total_sigma)
        range_high = projected_final * math.exp(1.2816 * total_sigma)
        distance_pct = (current - target) / target * 100.0

        directional_votes = [
            1 if slopes["30s"] > 0 else -1 if slopes["30s"] < 0 else 0,
            1 if slopes["60s"] > 0 else -1 if slopes["60s"] < 0 else 0,
            1 if slopes["180s"] > 0 else -1 if slopes["180s"] < 0 else 0,
            1 if fast_ema and slow_ema and fast_ema > slow_ema else -1 if fast_ema and slow_ema and fast_ema < slow_ema else 0,
            1 if rsi is not None and rsi > 55 else -1 if rsi is not None and rsi < 45 else 0,
        ]
        nonzero = [vote for vote in directional_votes if vote]
        agreement = abs(sum(nonzero)) / len(nonzero) if nonzero else 0.0
        history_quality = clamp(len(points) / 36.0, 0.0, 1.0)
        feed_age = _num(snapshot.get("data_age_seconds"))
        freshness = 1.0 if feed_age is None or feed_age <= 2.0 else clamp(1.0 - (feed_age - 2.0) / 8.0, 0.0, 1.0)
        # Extremeness is not evidence of reliability.
        confidence = clamp(0.45 * history_quality + 0.40 * agreement + 0.15 * freshness, 0.0, 1.0)

        low = float(getattr(self.config, "end_prediction_uncertain_low", 0.42))
        high = float(getattr(self.config, "end_prediction_uncertain_high", 0.58))
        if blended >= high:
            result = "YES"
        elif blended <= low:
            result = "NO"
        else:
            result = "UNCERTAIN"

        slope_threshold = 0.08 * sigma / math.sqrt(remaining)
        if weighted_slope > slope_threshold:
            chart_bias = "bullish"
        elif weighted_slope < -slope_threshold:
            chart_bias = "bearish"
        else:
            chart_bias = "neutral"

        return EndPrediction(
            version=self.VERSION,
            predicted_result=result,
            chart_yes_probability=round(chart_probability, 5),
            blended_yes_probability=round(blended, 5),
            confidence=round(confidence, 3),
            predicted_final_underlying=round(projected_final, 8),
            predicted_range_low=round(range_low, 8),
            predicted_range_high=round(range_high, 8),
            current_underlying=round(current, 8),
            target_value=round(target, 8),
            distance_to_target_pct=round(distance_pct, 5),
            trend_30s_pct=round(trends["30s"], 5),
            trend_60s_pct=round(trends["60s"], 5),
            trend_180s_pct=round(trends["180s"], 5),
            fast_ema=round(fast_ema, 8) if fast_ema is not None else None,
            slow_ema=round(slow_ema, 8) if slow_ema is not None else None,
            rsi_14=round(rsi, 3) if rsi is not None else None,
            volatility_per_sqrt_second=round(sigma, 10),
            chart_bias=chart_bias,
            agreement_score=round(agreement, 3),
            history_points=len(points),
            seconds_remaining=seconds,
            warning="Probabilistic chart estimate only; it can be wrong and does not guarantee settlement.",
        )

    @staticmethod
    def _window(points: Sequence[tuple[float, float]], seconds: float) -> list[tuple[float, float]]:
        if not points:
            return []
        cutoff = points[-1][0] - seconds
        subset = [point for point in points if point[0] >= cutoff]
        return subset if len(subset) >= 3 else list(points[-min(len(points), 12):])

    @staticmethod
    def _market_mid(snapshot: dict) -> float | None:
        bid = _num(snapshot.get("yes_bid"))
        ask = _num(snapshot.get("yes_ask"))
        if bid is None or ask is None:
            return None
        return probability_clip((bid + ask) / 200.0)
