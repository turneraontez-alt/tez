"""Deterministic NumPy baseline for path archetype and trajectory forecasts."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from .reconstruct import (
    ARCHETYPES,
    FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION,
    LABEL_POLICY_VERSION,
    TRAJECTORY_FRACTIONS,
    PathExample,
    examples_to_arrays,
)


MODEL_VERSION = "q15-path-forecast-lda-ridge-v1"


def _softmax(logits: np.ndarray) -> np.ndarray:
    values = np.asarray(logits, dtype=float)
    values = values - np.max(values, axis=1, keepdims=True)
    exp = np.exp(np.clip(values, -700.0, 700.0))
    return exp / np.sum(exp, axis=1, keepdims=True)


def _log_loss(y_index: np.ndarray, probability: np.ndarray) -> float:
    clipped = np.clip(probability[np.arange(len(y_index)), y_index], 1e-12, 1.0)
    return float(-np.mean(np.log(clipped)))


def _multiclass_brier(y_index: np.ndarray, probability: np.ndarray) -> float:
    truth = np.zeros_like(probability)
    truth[np.arange(len(y_index)), y_index] = 1.0
    return float(np.mean(np.sum((probability - truth) ** 2, axis=1)))


@dataclass(frozen=True)
class LinearDiscriminant:
    classes: tuple[str, ...]
    coefficient: np.ndarray
    intercept: np.ndarray
    temperature: float

    @classmethod
    def fit(cls, X: np.ndarray, y: np.ndarray, *, shrinkage: float = 0.25) -> "LinearDiscriminant":
        labels = tuple(sorted(str(value) for value in np.unique(y)))
        if len(labels) < 2:
            raise ValueError("classifier requires at least two classes")
        means: list[np.ndarray] = []
        priors: list[float] = []
        centered: list[np.ndarray] = []
        string_y = np.asarray([str(value) for value in y], dtype=str)
        for label in labels:
            rows = X[string_y == label]
            if not len(rows):
                raise ValueError(f"empty class: {label}")
            mean = np.mean(rows, axis=0)
            means.append(mean)
            priors.append(len(rows) / len(X))
            centered.append(rows - mean)
        pooled = np.vstack(centered)
        denominator = max(1, len(X) - len(labels))
        covariance = pooled.T @ pooled / denominator
        diagonal = np.diag(np.diag(covariance))
        regularized = (1.0 - shrinkage) * covariance + shrinkage * diagonal
        regularized += np.eye(X.shape[1], dtype=float) * 1e-5
        inverse = np.linalg.pinv(regularized, hermitian=True)
        mean_matrix = np.vstack(means)
        coefficient = mean_matrix @ inverse
        intercept = np.asarray([
            -0.5 * float(mean @ inverse @ mean) + math.log(max(prior, 1e-12))
            for mean, prior in zip(means, priors)
        ], dtype=float)
        return cls(labels, coefficient, intercept, 1.0)

    def logits(self, X: np.ndarray) -> np.ndarray:
        return np.asarray(X, dtype=float) @ self.coefficient.T + self.intercept

    def probabilities(self, X: np.ndarray) -> np.ndarray:
        return _softmax(self.logits(X) / max(0.05, float(self.temperature)))

    def calibrated(self, X: np.ndarray, y: np.ndarray) -> "LinearDiscriminant":
        string_y = np.asarray([str(value) for value in y], dtype=str)
        mapping = {label: idx for idx, label in enumerate(self.classes)}
        valid = np.asarray([value in mapping for value in string_y], dtype=bool)
        if not np.any(valid):
            return self
        logits = self.logits(X[valid])
        indexes = np.asarray([mapping[value] for value in string_y[valid]], dtype=int)
        candidates = np.geomspace(0.25, 4.0, 81)
        losses = [_log_loss(indexes, _softmax(logits / value)) for value in candidates]
        temperature = float(candidates[int(np.argmin(losses))])
        return LinearDiscriminant(self.classes, self.coefficient, self.intercept, temperature)


@dataclass(frozen=True)
class PathForecastModel:
    feature_names: tuple[str, ...]
    imputer: np.ndarray
    mean: np.ndarray
    scale: np.ndarray
    archetype_model: LinearDiscriminant
    settlement_model: LinearDiscriminant
    crossing_model: LinearDiscriminant
    trajectory_weight: np.ndarray
    trajectory_residual_quantiles: np.ndarray
    turn_weight: np.ndarray
    turn_residual_quantiles: np.ndarray
    trained_at_close_time: float
    audit_summary: Mapping[str, Any]

    def _transform(self, X: np.ndarray) -> np.ndarray:
        values = np.asarray(X, dtype=float)
        if values.ndim == 1:
            values = values.reshape(1, -1)
        if values.shape[1] != len(self.feature_names):
            raise ValueError("feature vector does not match model schema")
        filled = np.where(np.isfinite(values), values, self.imputer)
        return (filled - self.mean) / self.scale

    @staticmethod
    def _ridge_fit(X: np.ndarray, y: np.ndarray, regularization: float) -> np.ndarray:
        design = np.column_stack([X, np.ones(len(X), dtype=float)])
        penalty = np.eye(design.shape[1], dtype=float) * regularization
        penalty[-1, -1] = 0.0
        return np.linalg.solve(design.T @ design + penalty, design.T @ y)

    @staticmethod
    def _ridge_predict(X: np.ndarray, weight: np.ndarray) -> np.ndarray:
        design = np.column_stack([X, np.ones(len(X), dtype=float)])
        return design @ weight

    def predict(self, feature_vector: np.ndarray) -> dict[str, Any]:
        X = self._transform(feature_vector)
        archetype_probability = self.archetype_model.probabilities(X)[0]
        settlement_probability = self.settlement_model.probabilities(X)[0]
        crossing_probability = self.crossing_model.probabilities(X)[0]
        settlement_map = {label: value for label, value in zip(self.settlement_model.classes, settlement_probability)}
        crossing_map = {label: value for label, value in zip(self.crossing_model.classes, crossing_probability)}
        center = self._ridge_predict(X, self.trajectory_weight)[0]
        trajectory: list[dict[str, float]] = []
        for idx, fraction in enumerate(TRAJECTORY_FRACTIONS):
            q10 = float(center[idx] + self.trajectory_residual_quantiles[0, idx])
            q50 = float(center[idx] + self.trajectory_residual_quantiles[1, idx])
            q90 = float(center[idx] + self.trajectory_residual_quantiles[2, idx])
            ordered = sorted((q10, q50, q90))
            trajectory.append({
                "fraction_remaining_path": float(fraction),
                "return_bps_q10": ordered[0],
                "return_bps_q50": ordered[1],
                "return_bps_q90": ordered[2],
            })
        turn_log_center = float(self._ridge_predict(X, self.turn_weight)[0])
        turn_values = [
            max(0.0, math.expm1(turn_log_center + float(residual)))
            for residual in self.turn_residual_quantiles
        ]
        turn_values.sort()
        archetype_map = {
            label: float(value)
            for label, value in zip(self.archetype_model.classes, archetype_probability)
        }
        top = max(archetype_map, key=archetype_map.get)
        return {
            "model_version": MODEL_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "label_policy_version": LABEL_POLICY_VERSION,
            "paper_only": True,
            "top_archetype": top,
            "top_archetype_probability": archetype_map[top],
            "archetype_probabilities": archetype_map,
            "settlement_yes_probability": float(settlement_map.get("1", 0.0)),
            "strike_cross_probability": float(crossing_map.get("1", 0.0)),
            "turn_delay_seconds_q10": turn_values[0],
            "turn_delay_seconds_q50": turn_values[1],
            "turn_delay_seconds_q90": turn_values[2],
            "trajectory": trajectory,
        }

    def save(self, path: str) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        metadata = {
            "model_version": MODEL_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "label_policy_version": LABEL_POLICY_VERSION,
            "feature_names": list(self.feature_names),
            "archetype_classes": list(self.archetype_model.classes),
            "settlement_classes": list(self.settlement_model.classes),
            "crossing_classes": list(self.crossing_model.classes),
            "trained_at_close_time": self.trained_at_close_time,
            "audit_summary": dict(self.audit_summary),
        }
        np.savez_compressed(
            target,
            metadata=np.asarray(json.dumps(metadata, sort_keys=True)),
            imputer=self.imputer,
            mean=self.mean,
            scale=self.scale,
            archetype_coefficient=self.archetype_model.coefficient,
            archetype_intercept=self.archetype_model.intercept,
            archetype_temperature=np.asarray(self.archetype_model.temperature),
            settlement_coefficient=self.settlement_model.coefficient,
            settlement_intercept=self.settlement_model.intercept,
            settlement_temperature=np.asarray(self.settlement_model.temperature),
            crossing_coefficient=self.crossing_model.coefficient,
            crossing_intercept=self.crossing_model.intercept,
            crossing_temperature=np.asarray(self.crossing_model.temperature),
            trajectory_weight=self.trajectory_weight,
            trajectory_residual_quantiles=self.trajectory_residual_quantiles,
            turn_weight=self.turn_weight,
            turn_residual_quantiles=self.turn_residual_quantiles,
        )

    @classmethod
    def load(cls, path: str) -> "PathForecastModel":
        source = Path(path)
        if not source.exists():
            raise FileNotFoundError(f"path forecast model not found: {source}")
        with np.load(source, allow_pickle=False) as data:
            metadata = json.loads(str(data["metadata"].item()))
            if metadata.get("model_version") != MODEL_VERSION:
                raise ValueError("path forecast model version mismatch")
            if metadata.get("feature_schema_version") != FEATURE_SCHEMA_VERSION:
                raise ValueError("path forecast feature schema mismatch")
            return cls(
                feature_names=tuple(metadata["feature_names"]),
                imputer=np.asarray(data["imputer"], dtype=float),
                mean=np.asarray(data["mean"], dtype=float),
                scale=np.asarray(data["scale"], dtype=float),
                archetype_model=LinearDiscriminant(
                    tuple(metadata["archetype_classes"]),
                    np.asarray(data["archetype_coefficient"], dtype=float),
                    np.asarray(data["archetype_intercept"], dtype=float),
                    float(data["archetype_temperature"].item()),
                ),
                settlement_model=LinearDiscriminant(
                    tuple(metadata["settlement_classes"]),
                    np.asarray(data["settlement_coefficient"], dtype=float),
                    np.asarray(data["settlement_intercept"], dtype=float),
                    float(data["settlement_temperature"].item()),
                ),
                crossing_model=LinearDiscriminant(
                    tuple(metadata["crossing_classes"]),
                    np.asarray(data["crossing_coefficient"], dtype=float),
                    np.asarray(data["crossing_intercept"], dtype=float),
                    float(data["crossing_temperature"].item()),
                ),
                trajectory_weight=np.asarray(data["trajectory_weight"], dtype=float),
                trajectory_residual_quantiles=np.asarray(data["trajectory_residual_quantiles"], dtype=float),
                turn_weight=np.asarray(data["turn_weight"], dtype=float),
                turn_residual_quantiles=np.asarray(data["turn_residual_quantiles"], dtype=float),
                trained_at_close_time=float(metadata["trained_at_close_time"]),
                audit_summary=metadata.get("audit_summary") or {},
            )


def _fit_imputer_scaler(X: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    with np.errstate(all="ignore"):
        imputer = np.nanmedian(np.where(np.isfinite(X), X, np.nan), axis=0)
    imputer = np.where(np.isfinite(imputer), imputer, 0.0)
    filled = np.where(np.isfinite(X), X, imputer)
    mean = np.mean(filled, axis=0)
    scale = np.std(filled, axis=0)
    scale = np.where(scale > 1e-9, scale, 1.0)
    return imputer, mean, scale, (filled - mean) / scale


def _class_metrics(model: LinearDiscriminant, X: np.ndarray, y: np.ndarray) -> dict[str, Any]:
    probability = model.probabilities(X)
    string_y = np.asarray([str(value) for value in y], dtype=str)
    mapping = {label: idx for idx, label in enumerate(model.classes)}
    indexes = np.asarray([mapping.get(value, -1) for value in string_y], dtype=int)
    valid = indexes >= 0
    if not np.any(valid):
        return {"n": 0}
    probability = probability[valid]
    indexes = indexes[valid]
    predicted = np.argmax(probability, axis=1)
    recalls: list[float] = []
    for idx in range(len(model.classes)):
        mask = indexes == idx
        if np.any(mask):
            recalls.append(float(np.mean(predicted[mask] == idx)))
    return {
        "n": int(len(indexes)),
        "accuracy": float(np.mean(predicted == indexes)),
        "macro_recall": float(np.mean(recalls)) if recalls else None,
        "log_loss": _log_loss(indexes, probability),
        "brier": _multiclass_brier(indexes, probability),
    }


def _confusion(model: LinearDiscriminant, X: np.ndarray, y: np.ndarray) -> dict[str, dict[str, int]]:
    probability = model.probabilities(X)
    predicted = [model.classes[idx] for idx in np.argmax(probability, axis=1)]
    out = {label: {candidate: 0 for candidate in model.classes} for label in model.classes}
    for actual, guess in zip((str(value) for value in y), predicted):
        if actual in out:
            out[actual][guess] += 1
    return out


def _chronological_masks(close_times: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, float]]:
    windows = np.unique(close_times)
    if len(windows) < 30:
        raise ValueError("at least 30 unique windows are required")
    train_end = max(1, int(len(windows) * 0.60))
    calibration_end = max(train_end + 1, int(len(windows) * 0.80))
    calibration_end = min(calibration_end, len(windows) - 1)
    train_windows = set(windows[:train_end].tolist())
    calibration_windows = set(windows[train_end:calibration_end].tolist())
    test_windows = set(windows[calibration_end:].tolist())
    train = np.asarray([value in train_windows for value in close_times], dtype=bool)
    calibration = np.asarray([value in calibration_windows for value in close_times], dtype=bool)
    test = np.asarray([value in test_windows for value in close_times], dtype=bool)
    boundaries = {
        "train_last_close": float(windows[train_end - 1]),
        "calibration_first_close": float(windows[train_end]),
        "calibration_last_close": float(windows[calibration_end - 1]),
        "test_first_close": float(windows[calibration_end]),
        "test_last_close": float(windows[-1]),
    }
    return train, calibration, test, boundaries


def train_and_audit(
    examples: Sequence[PathExample],
    *,
    coverage: Mapping[str, Any] | None = None,
) -> tuple[PathForecastModel, dict[str, Any]]:
    arrays = examples_to_arrays(examples)
    train, calibration, test, boundaries = _chronological_masks(arrays["close_time"])
    imputer, mean, scale, X_train = _fit_imputer_scaler(arrays["X"][train])

    def transform(values: np.ndarray) -> np.ndarray:
        filled = np.where(np.isfinite(values), values, imputer)
        return (filled - mean) / scale

    X_calibration = transform(arrays["X"][calibration])
    X_test = transform(arrays["X"][test])
    archetype = LinearDiscriminant.fit(X_train, arrays["archetype"][train]).calibrated(
        X_calibration, arrays["archetype"][calibration]
    )
    settlement = LinearDiscriminant.fit(X_train, arrays["official_yes"][train]).calibrated(
        X_calibration, arrays["official_yes"][calibration]
    )
    crossing = LinearDiscriminant.fit(X_train, arrays["strike_crossed"][train]).calibrated(
        X_calibration, arrays["strike_crossed"][calibration]
    )
    trajectory_weight = PathForecastModel._ridge_fit(
        X_train, arrays["trajectory"][train], regularization=8.0
    )
    trajectory_calibration = PathForecastModel._ridge_predict(X_calibration, trajectory_weight)
    trajectory_residual = arrays["trajectory"][calibration] - trajectory_calibration
    trajectory_quantiles = np.quantile(trajectory_residual, (0.10, 0.50, 0.90), axis=0)

    turn_train = train & np.isfinite(arrays["turn_delay"])
    turn_calibration = calibration & np.isfinite(arrays["turn_delay"])
    if int(np.sum(turn_train)) >= 50:
        turn_weight = PathForecastModel._ridge_fit(
            transform(arrays["X"][turn_train]),
            np.log1p(arrays["turn_delay"][turn_train]),
            regularization=8.0,
        )
        if int(np.sum(turn_calibration)) >= 20:
            center = PathForecastModel._ridge_predict(transform(arrays["X"][turn_calibration]), turn_weight)
            residual = np.log1p(arrays["turn_delay"][turn_calibration]) - center
            turn_quantiles = np.quantile(residual, (0.10, 0.50, 0.90))
        else:
            turn_quantiles = np.zeros(3, dtype=float)
    else:
        median = float(np.nanmedian(arrays["turn_delay"][train])) if np.any(turn_train) else 0.0
        turn_weight = np.zeros(X_train.shape[1] + 1, dtype=float)
        turn_weight[-1] = math.log1p(max(0.0, median))
        turn_quantiles = np.zeros(3, dtype=float)

    trajectory_test = PathForecastModel._ridge_predict(X_test, trajectory_weight)
    trajectory_truth = arrays["trajectory"][test]
    model_mae = np.mean(np.abs(trajectory_truth - trajectory_test), axis=0)
    random_walk_mae = np.mean(np.abs(trajectory_truth), axis=0)
    lower = trajectory_test + trajectory_quantiles[0]
    upper = trajectory_test + trajectory_quantiles[2]
    interval_coverage = np.mean((trajectory_truth >= lower) & (trajectory_truth <= upper), axis=0)

    settlement_probability = settlement.probabilities(X_test)
    settlement_yes_idx = settlement.classes.index("1")
    settlement_yes = settlement_probability[:, settlement_yes_idx]
    settlement_truth = arrays["official_yes"][test]
    market_probability = np.where(
        np.isfinite(arrays["current_yes_mid"][test]),
        np.clip(arrays["current_yes_mid"][test] / 100.0, 0.01, 0.99),
        0.5,
    )
    rti_side = arrays["current_rti_yes"][test]
    settlement_model_metrics = _class_metrics(settlement, X_test, settlement_truth)
    baseline = {
        "kalshi_market_accuracy": float(np.mean((market_probability >= 0.5) == settlement_truth)),
        "kalshi_market_brier": float(np.mean((market_probability - settlement_truth) ** 2)),
        "current_rti_side_accuracy": float(np.mean(rti_side == settlement_truth)),
        "archetype_majority_accuracy": float(
            max(np.mean(arrays["archetype"][test] == label) for label in np.unique(arrays["archetype"][train]))
        ),
        "trajectory_random_walk_mae_bps": [float(value) for value in random_walk_mae],
    }
    archetype_metrics = _class_metrics(archetype, X_test, arrays["archetype"][test])
    crossing_metrics = _class_metrics(crossing, X_test, arrays["strike_crossed"][test])

    def grouped_metrics(values: np.ndarray) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for value in sorted(np.unique(values[test]).tolist(), key=str):
            subset_all = test & (values == value)
            subset_X = transform(arrays["X"][subset_all])
            if not len(subset_X):
                continue
            path_metric = _class_metrics(archetype, subset_X, arrays["archetype"][subset_all])
            settle_metric = _class_metrics(settlement, subset_X, arrays["official_yes"][subset_all])
            path_metric["settlement_accuracy"] = settle_metric.get("accuracy")
            path_metric["settlement_brier"] = settle_metric.get("brier")
            out[str(value)] = path_metric
        return out

    counts = {label: int(np.sum(arrays["archetype"][train] == label)) for label in ARCHETYPES}
    data_ready = (
        int(np.sum(test)) >= 500
        and len(np.unique(arrays["close_time"][test])) >= 100
        and all(value >= 25 for value in counts.values())
    )
    trajectory_improved = bool(float(np.mean(model_mae)) < float(np.mean(random_walk_mae)))
    settlement_improved = bool(
        float(settlement_model_metrics["accuracy"]) > baseline["kalshi_market_accuracy"]
        and float(np.mean((settlement_yes - settlement_truth) ** 2)) < baseline["kalshi_market_brier"]
    )
    report: dict[str, Any] = {
        "model_version": MODEL_VERSION,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "label_policy_version": LABEL_POLICY_VERSION,
        "paper_only": True,
        "split": {
            "method": "chronological_grouped_by_close_time_60_20_20",
            "train_examples": int(np.sum(train)),
            "calibration_examples": int(np.sum(calibration)),
            "test_examples": int(np.sum(test)),
            "train_windows": int(len(np.unique(arrays["close_time"][train]))),
            "calibration_windows": int(len(np.unique(arrays["close_time"][calibration]))),
            "test_windows": int(len(np.unique(arrays["close_time"][test]))),
            **boundaries,
        },
        "coverage": dict(coverage or {}),
        "archetype": {
            **archetype_metrics,
            "temperature": archetype.temperature,
            "train_class_counts": counts,
            "confusion": _confusion(archetype, X_test, arrays["archetype"][test]),
        },
        "settlement": {
            **settlement_model_metrics,
            "binary_brier": float(np.mean((settlement_yes - settlement_truth) ** 2)),
            "temperature": settlement.temperature,
        },
        "strike_cross": {**crossing_metrics, "temperature": crossing.temperature},
        "trajectory": {
            "fractions": list(TRAJECTORY_FRACTIONS),
            "model_mae_bps": [float(value) for value in model_mae],
            "random_walk_mae_bps": [float(value) for value in random_walk_mae],
            "interval_80_coverage": [float(value) for value in interval_coverage],
        },
        "baselines": baseline,
        "by_checkpoint": grouped_metrics(arrays["checkpoint"]),
        "by_asset": grouped_metrics(arrays["asset"]),
        "decision": {
            "forward_shadow_eligible": data_ready,
            "notification_eligible": False,
            "trading_eligible": False,
            "trajectory_improved_vs_random_walk": trajectory_improved,
            "settlement_improved_vs_kalshi_market": settlement_improved,
            "promotion_requires_new_forward_review": True,
        },
    }
    audit_summary = {
        "forward_shadow_eligible": data_ready,
        "notification_eligible": False,
        "test_examples": int(np.sum(test)),
        "test_windows": int(len(np.unique(arrays["close_time"][test]))),
        "archetype_accuracy": archetype_metrics.get("accuracy"),
        "settlement_accuracy": settlement_model_metrics.get("accuracy"),
        "trajectory_improved_vs_random_walk": trajectory_improved,
    }
    model = PathForecastModel(
        feature_names=tuple(FEATURE_NAMES),
        imputer=imputer,
        mean=mean,
        scale=scale,
        archetype_model=archetype,
        settlement_model=settlement,
        crossing_model=crossing,
        trajectory_weight=trajectory_weight,
        trajectory_residual_quantiles=trajectory_quantiles,
        turn_weight=np.asarray(turn_weight, dtype=float),
        turn_residual_quantiles=np.asarray(turn_quantiles, dtype=float),
        trained_at_close_time=float(np.max(arrays["close_time"][train])),
        audit_summary=audit_summary,
    )
    return model, report
