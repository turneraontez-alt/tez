from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Iterable, List, Sequence, Tuple


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _logit(p: float) -> float:
    p = min(0.999999, max(0.000001, p))
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


@dataclass
class ProbabilityEstimate:
    probability_yes: float | None
    confidence: float
    method: str
    sigma_per_sqrt_second: float | None
    history_points: int
    market_mid: float | None
    raw_probability: float | None
    momentum_z: float


class UnderlyingHistory:
    """Continuous underlying history that survives 15-minute market rollovers."""

    def __init__(self, retention_seconds: int = 3600):
        self.retention_seconds = retention_seconds
        self._points: Dict[float, float] = {}

    def update(self, candles: Sequence[dict] | None, current_price: float | None, now: float) -> None:
        # Q15_V5_HISTORY_INTEGRITY: forming candles have future end_time values and
        # must not enter historical volatility or momentum calculations.
        for candle in candles or []:
            try:
                end_ts = float(candle.get("end_time"))
                price = float(candle.get("close"))
            except (TypeError, ValueError):
                continue
            if end_ts <= now + 1e-6 and price > 0:
                self._points[end_ts] = price

        if current_price is not None:
            try:
                price = float(current_price)
                if price > 0:
                    self._points[float(now)] = price
            except (TypeError, ValueError):
                pass

        cutoff = now - self.retention_seconds
        for ts in [ts for ts in self._points if ts < cutoff or ts > now + 1e-6]:
            self._points.pop(ts, None)

    def points(self, now: float, lookback: float | None = None) -> List[Tuple[float, float]]:
        cutoff = -float("inf") if lookback is None else now - lookback
        return sorted(
            (ts, price)
            for ts, price in self._points.items()
            if cutoff <= ts <= now + 1e-6
        )

    def return_pct(self, now: float, seconds: float) -> float | None:
        points = self.points(now, max(seconds * 2.0, seconds + 30.0))
        if len(points) < 2:
            return None
        current_ts, current = points[-1]
        target_ts = now - seconds
        prior = min(points[:-1], key=lambda item: abs(item[0] - target_ts), default=None)
        if not prior or prior[1] <= 0:
            return None
        return (current - prior[1]) / prior[1] * 100.0


class ProbabilityModel:
    def __init__(self, settings):
        self.settings = settings
        self.histories: Dict[str, UnderlyingHistory] = {}

    def update_history(self, asset: str, snapshot: dict, now: float) -> None:
        history = self.histories.setdefault(asset, UnderlyingHistory())
        history.update(
            snapshot.get("underlying_candles_5s"),
            snapshot.get("underlying_current"),
            now,
        )

    def broader_returns(self, now: float, seconds: float = 60.0) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for asset, history in self.histories.items():
            change = history.return_pct(now, seconds)
            if change is not None:
                out[asset] = change
        return out

    def estimate(self, asset: str, snapshot: dict, now: float) -> ProbabilityEstimate:
        underlying = self._num(snapshot.get("underlying_current"))
        target = self._num(snapshot.get("target_value"))
        seconds = self._num(snapshot.get("seconds_remaining"))
        market_mid = self._market_mid(snapshot)
        history = self.histories.setdefault(asset, UnderlyingHistory())
        points = history.points(now, 1800.0)

        if not underlying or not target or not seconds or seconds <= 0 or len(points) < 8:
            return ProbabilityEstimate(
                probability_yes=None,
                confidence=min(0.30, len(points) / 30.0),
                method="insufficient continuous underlying history",
                sigma_per_sqrt_second=None,
                history_points=len(points),
                market_mid=market_mid,
                raw_probability=None,
                momentum_z=0.0,
            )

        sigma, sigma_quality = self._blended_sigma(points, now)
        if sigma is None or sigma <= 0:
            return ProbabilityEstimate(
                probability_yes=None,
                confidence=0.25,
                method="volatility unavailable",
                sigma_per_sqrt_second=None,
                history_points=len(points),
                market_mid=market_mid,
                raw_probability=None,
                momentum_z=0.0,
            )

        total_sigma = sigma * math.sqrt(seconds)
        if total_sigma <= 1e-10:
            return ProbabilityEstimate(None, 0.2, "volatility too small", sigma, len(points), market_mid, None, 0.0)

        z_distance = math.log(underlying / target) / total_sigma
        momentum_z = self._momentum_adjustment(history, now, seconds, total_sigma)
        raw_above = _phi(z_distance + momentum_z)

        target_type = str(snapshot.get("target_type") or "greater_or_equal")
        raw_yes = raw_above if target_type == "greater_or_equal" else 1.0 - raw_above

        # V9_MARKET_INDEPENDENT_MODEL: keep this forecast independent.
        # The Kalshi midpoint is applied exactly once by calibrated_edge.py.
        probability = raw_yes

        calibration = self.settings.calibration.get(asset, self.settings.calibration.get("global", {}))
        if isinstance(calibration, dict):
            try:
                temperature = max(0.25, float(calibration.get("temperature", 1.0)))
                bias = float(calibration.get("bias", 0.0))
                probability = _sigmoid(_logit(probability) / temperature + bias)
            except (TypeError, ValueError):
                pass

        probability = min(0.999, max(0.001, probability))
        history_quality = min(1.0, len(points) / 90.0)
        feed_age = self._snapshot_age(snapshot, now)
        freshness = 1.0 if feed_age <= 2.0 else max(0.0, 1.0 - (feed_age - 2.0) / 8.0)
        confidence = max(0.0, min(1.0, 0.15 + 0.45 * history_quality + 0.30 * sigma_quality + 0.10 * freshness))

        return ProbabilityEstimate(
            probability_yes=probability,
            confidence=confidence,
            method="multi-horizon EWMA lognormal + bounded momentum (market-independent)",
            sigma_per_sqrt_second=sigma,
            history_points=len(points),
            market_mid=market_mid,
            raw_probability=raw_yes,
            momentum_z=momentum_z,
        )

    def _blended_sigma(self, points: Sequence[Tuple[float, float]], now: float) -> Tuple[float | None, float]:
        # Q15_V5_VOLATILITY: overlapping windows are not counted as independent
        # evidence. Blend one responsive estimate with one slower stabilizer.
        recent = [(ts, price) for ts, price in points if now - 180.0 <= ts <= now]
        stable = [(ts, price) for ts, price in points if now - 900.0 <= ts <= now]
        recent_sigma = self._ewma_sigma(recent, decay=0.88)
        stable_sigma = self._ewma_sigma(stable, decay=0.96)

        if recent_sigma is None and stable_sigma is None:
            return None, 0.0
        if recent_sigma is None:
            sigma = stable_sigma
        elif stable_sigma is None:
            sigma = recent_sigma
        else:
            sigma = 0.70 * recent_sigma + 0.30 * stable_sigma

        sigma = min(0.02, max(1e-7, float(sigma)))
        quality = min(1.0, max(0, len(stable) - 1) / 60.0)
        if stable:
            newest_age = max(0.0, now - stable[-1][0])
            quality *= max(0.0, 1.0 - newest_age / 15.0)
        return sigma, quality

    @staticmethod
    def _ewma_sigma(points: Sequence[Tuple[float, float]], decay: float = 0.90) -> float | None:
        if len(points) < 5:
            return None
        standardized: List[float] = []
        for (t0, p0), (t1, p1) in zip(points, points[1:]):
            dt = t1 - t0
            if dt <= 0 or p0 <= 0 or p1 <= 0:
                continue
            standardized.append(math.log(p1 / p0) / math.sqrt(dt))
        if len(standardized) < 4:
            return None
        weights = []
        value = 1.0
        for _ in standardized[::-1]:
            weights.append(value)
            value *= decay
        weights.reverse()
        total = sum(weights)
        mean = sum(w * r for w, r in zip(weights, standardized)) / total
        variance = sum(w * (r - mean) ** 2 for w, r in zip(weights, standardized)) / total
        return math.sqrt(max(variance, 0.0))

    def _momentum_adjustment(self, history: UnderlyingHistory, now: float, seconds_remaining: float, total_sigma: float) -> float:
        changes = []
        for lookback, weight in ((15.0, 0.55), (60.0, 0.30), (180.0, 0.15)):
            points = history.points(now, lookback + 10.0)
            if len(points) < 2:
                continue
            start = points[0][1]
            end = points[-1][1]
            elapsed = max(1.0, points[-1][0] - points[0][0])
            if start > 0 and end > 0:
                drift_to_close = math.log(end / start) / elapsed * min(seconds_remaining, 120.0)
                changes.append((drift_to_close / total_sigma, weight))
        if not changes:
            return 0.0
        total_weight = sum(weight for _, weight in changes)
        z = sum(value * weight for value, weight in changes) / total_weight
        cap = max(0.0, self.settings.momentum_z_cap)
        return max(-cap, min(cap, z))

    @staticmethod
    def _market_mid(snapshot: dict) -> float | None:
        bid = ProbabilityModel._num(snapshot.get("yes_bid"))
        ask = ProbabilityModel._num(snapshot.get("yes_ask"))
        if bid is None or ask is None:
            return None
        return min(0.999, max(0.001, (bid + ask) / 200.0))

    @staticmethod
    def _snapshot_age(snapshot: dict, now: float) -> float:
        raw = snapshot.get("data_age_seconds")
        if raw is not None:
            try:
                return max(0.0, float(raw))
            except (TypeError, ValueError):
                pass
        return 0.0

    @staticmethod
    def _num(value) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
