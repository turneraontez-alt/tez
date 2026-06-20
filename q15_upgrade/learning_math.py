from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

EPS = 1e-9


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def probability_clip(value: float | None, low: float = 0.01, high: float = 0.99) -> float | None:
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return clamp(value, low, high)


def logit(probability: float) -> float:
    p = clamp(float(probability), 0.01, 0.99)
    return math.log(p / (1.0 - p))


def sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def parse_timestamp(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def time_decay_weights(timestamps: Sequence, half_life_days: float, now_ts: float | None = None) -> list[float]:
    if not timestamps:
        return []
    parsed = [parse_timestamp(value) for value in timestamps]
    valid = [value for value in parsed if value is not None]
    if not valid:
        return [1.0] * len(timestamps)
    anchor = float(now_ts if now_ts is not None else max(valid))
    if half_life_days <= 0:
        return [1.0] * len(timestamps)
    half_life_seconds = half_life_days * 86400.0
    return [
        math.exp(-math.log(2.0) * max(0.0, anchor - (value if value is not None else anchor)) / half_life_seconds)
        for value in parsed
    ]


def effective_sample_size(weights: Sequence[float]) -> float:
    total = sum(max(0.0, float(w)) for w in weights)
    squared = sum(max(0.0, float(w)) ** 2 for w in weights)
    if squared <= EPS:
        return 0.0
    return total * total / squared


def weighted_mean(values: Sequence[float], weights: Sequence[float]) -> float | None:
    pairs = [(float(v), max(0.0, float(w))) for v, w in zip(values, weights) if v is not None]
    total = sum(w for _, w in pairs)
    if total <= EPS:
        return None
    return sum(v * w for v, w in pairs) / total


def weighted_variance(values: Sequence[float], weights: Sequence[float], mean: float | None = None) -> float | None:
    if mean is None:
        mean = weighted_mean(values, weights)
    if mean is None:
        return None
    pairs = [(float(v), max(0.0, float(w))) for v, w in zip(values, weights) if v is not None]
    total = sum(w for _, w in pairs)
    if total <= EPS:
        return None
    return sum(w * (v - mean) ** 2 for v, w in pairs) / total


def weighted_standard_error(values: Sequence[float], weights: Sequence[float]) -> float | None:
    mean = weighted_mean(values, weights)
    variance = weighted_variance(values, weights, mean)
    eff_n = effective_sample_size(weights)
    if mean is None or variance is None or eff_n <= 1:
        return None
    return math.sqrt(max(0.0, variance) / eff_n)


def log_loss(probabilities: Sequence[float], outcomes: Sequence[int], weights: Sequence[float] | None = None) -> float | None:
    if weights is None:
        weights = [1.0] * len(probabilities)
    total = 0.0
    denom = 0.0
    for p, y, w in zip(probabilities, outcomes, weights):
        p = probability_clip(p)
        if p is None:
            continue
        w = max(0.0, float(w))
        total += -w * (float(y) * math.log(p) + (1.0 - float(y)) * math.log(1.0 - p))
        denom += w
    return total / denom if denom > EPS else None


def brier_score(probabilities: Sequence[float], outcomes: Sequence[int], weights: Sequence[float] | None = None) -> float | None:
    if weights is None:
        weights = [1.0] * len(probabilities)
    total = 0.0
    denom = 0.0
    for p, y, w in zip(probabilities, outcomes, weights):
        p = probability_clip(p)
        if p is None:
            continue
        w = max(0.0, float(w))
        total += w * (p - float(y)) ** 2
        denom += w
    return total / denom if denom > EPS else None


def expected_calibration_error(
    probabilities: Sequence[float], outcomes: Sequence[int], weights: Sequence[float] | None = None, bins: int = 10
) -> tuple[float | None, list[dict]]:
    if weights is None:
        weights = [1.0] * len(probabilities)
    grouped = [dict(weight=0.0, p_sum=0.0, y_sum=0.0, n=0) for _ in range(max(2, bins))]
    total_weight = 0.0
    for p, y, w in zip(probabilities, outcomes, weights):
        p = probability_clip(p)
        if p is None:
            continue
        w = max(0.0, float(w))
        index = min(len(grouped) - 1, int(p * len(grouped)))
        bucket = grouped[index]
        bucket["weight"] += w
        bucket["p_sum"] += p * w
        bucket["y_sum"] += float(y) * w
        bucket["n"] += 1
        total_weight += w
    if total_weight <= EPS:
        return None, []
    ece = 0.0
    reliability = []
    for index, bucket in enumerate(grouped):
        if bucket["weight"] <= EPS:
            continue
        predicted = bucket["p_sum"] / bucket["weight"]
        realized = bucket["y_sum"] / bucket["weight"]
        ece += bucket["weight"] / total_weight * abs(predicted - realized)
        reliability.append({
            "bin_low": round(index / len(grouped), 2),
            "bin_high": round((index + 1) / len(grouped), 2),
            "n": bucket["n"],
            "effective_weight": round(bucket["weight"], 3),
            "predicted": round(predicted, 4),
            "realized": round(realized, 4),
            "gap": round(predicted - realized, 4),
        })
    return ece, reliability


def calibration_slope_intercept(probabilities: Sequence[float], outcomes: Sequence[int], weights: Sequence[float]) -> tuple[float | None, float | None]:
    xs = []
    ys = []
    ws = []
    for p, y, w in zip(probabilities, outcomes, weights):
        p = probability_clip(p)
        if p is None:
            continue
        xs.append(logit(p))
        ys.append(float(y))
        ws.append(max(0.0, float(w)))
    if len(xs) < 5 or sum(ws) <= EPS:
        return None, None
    bias = 0.0
    slope = 1.0
    learning_rate = 0.02
    regularization = 0.02
    denom = max(EPS, sum(ws))
    for _ in range(500):
        grad_bias = 0.0
        grad_slope = 0.0
        for x, y, w in zip(xs, ys, ws):
            pred = sigmoid(bias + slope * x)
            error = pred - y
            grad_bias += w * error
            grad_slope += w * error * x
        grad_bias /= denom
        grad_slope = grad_slope / denom + regularization * (slope - 1.0)
        bias -= learning_rate * grad_bias
        slope -= learning_rate * grad_slope
    return slope, bias


@dataclass
class CalibrationFit:
    global_bias: float
    global_temperature: float
    asset_biases: dict[str, float]
    train_metrics: dict
    validation_metrics: dict
    effective_n: float
    half_life_days: float

    def predict(self, asset: str | None, probability: float | None) -> float | None:
        p = probability_clip(probability)
        if p is None:
            return None
        asset_bias = self.asset_biases.get(str(asset), 0.0) if asset else 0.0
        transformed = self.global_bias + asset_bias + logit(p) / max(0.25, self.global_temperature)
        return clamp(sigmoid(transformed), 0.01, 0.99)

    def as_params(self) -> dict:
        return {
            "global_bias": round(self.global_bias, 8),
            "global_temperature": round(self.global_temperature, 8),
            "asset_biases": {key: round(value, 8) for key, value in self.asset_biases.items()},
            "half_life_days": self.half_life_days,
        }


def _metrics(probabilities: Sequence[float], outcomes: Sequence[int], weights: Sequence[float]) -> dict:
    ece, buckets = expected_calibration_error(probabilities, outcomes, weights)
    slope, intercept = calibration_slope_intercept(probabilities, outcomes, weights)
    return {
        "n": len(probabilities),
        "effective_n": round(effective_sample_size(weights), 3),
        "log_loss": _round(log_loss(probabilities, outcomes, weights)),
        "brier": _round(brier_score(probabilities, outcomes, weights)),
        "ece": _round(ece),
        "calibration_slope": _round(slope),
        "calibration_intercept": _round(intercept),
        "reliability": buckets,
    }


def _round(value, digits: int = 6):
    return round(float(value), digits) if value is not None and math.isfinite(float(value)) else None


def grouped_chronological_split(rows: Sequence[dict], validation_fraction: float = 0.25) -> tuple[list[dict], list[dict]]:
    if not rows:
        return [], []
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        key = str(row.get("market_close") or row.get("decision_timestamp") or "")
        grouped[key].append(row)
    ordered_keys = sorted(grouped, key=lambda key: parse_timestamp(key) or 0.0)
    if len(ordered_keys) < 2:
        return list(rows), []
    validation_groups = max(1, int(math.ceil(len(ordered_keys) * validation_fraction)))
    split = max(1, len(ordered_keys) - validation_groups)
    train_keys = set(ordered_keys[:split])
    validation_keys = set(ordered_keys[split:])
    train = [row for row in rows if str(row.get("market_close") or row.get("decision_timestamp") or "") in train_keys]
    validation = [row for row in rows if str(row.get("market_close") or row.get("decision_timestamp") or "") in validation_keys]
    return train, validation


def fit_temperature_bias(
    rows: Sequence[dict],
    *,
    half_life_days: float,
    l2: float = 1.0,
    learning_rate: float = 0.03,
    steps: int = 800,
    asset_min_effective_n: float = 100.0,
) -> CalibrationFit | None:
    cleaned = []
    for row in rows:
        p = probability_clip(row.get("raw_win_probability"))
        outcome = row.get("outcome")
        if p is None or outcome not in {"win", "loss", 1, 0, True, False}:
            continue
        cleaned.append({**row, "p": p, "y": 1 if outcome in {"win", 1, True} else 0})
    if len(cleaned) < 10:
        return None
    cleaned.sort(key=lambda row: parse_timestamp(row.get("decision_timestamp")) or 0.0)
    train, validation = grouped_chronological_split(cleaned)
    if len(train) < 8:
        return None
    weights = time_decay_weights([row.get("decision_timestamp") for row in train], half_life_days)
    assets = sorted({str(row.get("asset")) for row in train if row.get("asset")})
    asset_indices = {asset: index for index, asset in enumerate(assets)}
    asset_weights: dict[str, list[float]] = defaultdict(list)
    for row, weight in zip(train, weights):
        asset_weights[str(row.get("asset"))].append(weight)
    eligible_assets = {
        asset for asset, values in asset_weights.items() if effective_sample_size(values) >= asset_min_effective_n
    }

    bias = 0.0
    log_temperature = 0.0
    asset_biases = {asset: 0.0 for asset in eligible_assets}
    denom = max(EPS, sum(weights))
    for step in range(max(50, steps)):
        grad_bias = 0.0
        grad_log_temperature = 0.0
        grad_asset = {asset: 0.0 for asset in eligible_assets}
        temperature = clamp(math.exp(log_temperature), 0.25, 4.0)
        for row, weight in zip(train, weights):
            z = logit(row["p"])
            asset = str(row.get("asset"))
            local_bias = asset_biases.get(asset, 0.0)
            pred = sigmoid(bias + local_bias + z / temperature)
            error = pred - row["y"]
            grad_bias += weight * error
            grad_log_temperature += weight * error * (-z / temperature)
            if asset in grad_asset:
                grad_asset[asset] += weight * error
        grad_bias = grad_bias / denom + l2 * bias / max(10.0, denom)
        grad_log_temperature = grad_log_temperature / denom + l2 * log_temperature / max(10.0, denom)
        bias -= learning_rate * grad_bias
        log_temperature -= learning_rate * grad_log_temperature
        log_temperature = clamp(log_temperature, math.log(0.25), math.log(4.0))
        for asset in eligible_assets:
            local_denom = max(EPS, sum(asset_weights[asset]))
            gradient = grad_asset[asset] / local_denom + l2 * asset_biases[asset] / max(5.0, local_denom)
            asset_biases[asset] = clamp(asset_biases[asset] - learning_rate * gradient, -1.5, 1.5)
        if step and step % 200 == 0:
            learning_rate *= 0.7

    temperature = clamp(math.exp(log_temperature), 0.25, 4.0)
    fit = CalibrationFit(
        global_bias=bias,
        global_temperature=temperature,
        asset_biases=asset_biases,
        train_metrics={},
        validation_metrics={},
        effective_n=effective_sample_size(weights),
        half_life_days=half_life_days,
    )
    train_probs = [fit.predict(row.get("asset"), row["p"]) for row in train]
    train_outcomes = [row["y"] for row in train]
    fit.train_metrics = _metrics(train_probs, train_outcomes, weights)

    if validation:
        validation_weights = time_decay_weights(
            [row.get("decision_timestamp") for row in validation], half_life_days
        )
        calibrated = [fit.predict(row.get("asset"), row["p"]) for row in validation]
        raw = [row["p"] for row in validation]
        outcomes = [row["y"] for row in validation]
        fit.validation_metrics = {
            "calibrated": _metrics(calibrated, outcomes, validation_weights),
            "raw": _metrics(raw, outcomes, validation_weights),
        }
    return fit


def fit_regularized_logistic(
    rows: Sequence[dict],
    feature_names: Sequence[str],
    *,
    outcome_key: str = "binary_outcome",
    weight_key: str = "sample_weight",
    l2: float = 1.0,
    steps: int = 700,
    learning_rate: float = 0.04,
) -> dict | None:
    clean = []
    for row in rows:
        if row.get(outcome_key) not in {0, 1, False, True}:
            continue
        features = []
        valid = True
        for name in feature_names:
            try:
                value = float(row.get(name, 0.0) or 0.0)
            except (TypeError, ValueError):
                valid = False
                break
            if not math.isfinite(value):
                valid = False
                break
            features.append(value)
        if valid:
            clean.append((features, int(bool(row[outcome_key])), max(0.0, float(row.get(weight_key, 1.0) or 1.0))))
    if len(clean) < max(10, len(feature_names) * 2):
        return None
    means = []
    scales = []
    for index in range(len(feature_names)):
        values = [item[0][index] for item in clean]
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / max(1, len(values) - 1)
        means.append(mean)
        scales.append(max(1e-6, math.sqrt(variance)))
    standardized = []
    for features, outcome, weight in clean:
        standardized.append(([(value - means[i]) / scales[i] for i, value in enumerate(features)], outcome, weight))
    intercept = 0.0
    coefficients = [0.0] * len(feature_names)
    denom = max(EPS, sum(item[2] for item in standardized))
    for step in range(steps):
        grad_intercept = 0.0
        gradients = [0.0] * len(feature_names)
        for features, outcome, weight in standardized:
            linear = intercept + sum(c * x for c, x in zip(coefficients, features))
            error = sigmoid(linear) - outcome
            grad_intercept += weight * error
            for index, value in enumerate(features):
                gradients[index] += weight * error * value
        intercept -= learning_rate * grad_intercept / denom
        for index in range(len(coefficients)):
            gradient = gradients[index] / denom + l2 * coefficients[index] / max(10.0, denom)
            coefficients[index] -= learning_rate * gradient
        if step and step % 200 == 0:
            learning_rate *= 0.7
    probabilities = []
    outcomes = []
    weights = []
    for features, outcome, weight in standardized:
        probabilities.append(sigmoid(intercept + sum(c * x for c, x in zip(coefficients, features))))
        outcomes.append(outcome)
        weights.append(weight)
    return {
        "intercept": intercept,
        "coefficients": dict(zip(feature_names, coefficients)),
        "means": dict(zip(feature_names, means)),
        "scales": dict(zip(feature_names, scales)),
        "metrics": _metrics(probabilities, outcomes, weights),
    }


def logistic_predict(model: dict | None, features: dict) -> float | None:
    if not model:
        return None
    linear = float(model.get("intercept", 0.0))
    coefficients = model.get("coefficients") or {}
    means = model.get("means") or {}
    scales = model.get("scales") or {}
    for name, coefficient in coefficients.items():
        try:
            value = float(features.get(name, 0.0) or 0.0)
            scale = max(1e-6, float(scales.get(name, 1.0) or 1.0))
            standardized = (value - float(means.get(name, 0.0) or 0.0)) / scale
            linear += float(coefficient) * standardized
        except (TypeError, ValueError):
            continue
    return clamp(sigmoid(linear), 0.01, 0.99)


def bootstrap_mean_difference(
    a: Sequence[float], b: Sequence[float], samples: int = 400, seed: int = 17
) -> dict | None:
    pairs = [(float(x), float(y)) for x, y in zip(a, b) if x is not None and y is not None]
    if len(pairs) < 10:
        return None
    rng = random.Random(seed)
    differences = []
    for _ in range(max(100, samples)):
        draw = [pairs[rng.randrange(len(pairs))] for _ in range(len(pairs))]
        differences.append(sum(x - y for x, y in draw) / len(draw))
    differences.sort()
    low = differences[int(0.025 * (len(differences) - 1))]
    high = differences[int(0.975 * (len(differences) - 1))]
    mean = sum(differences) / len(differences)
    return {"mean_difference": mean, "ci_low": low, "ci_high": high}
