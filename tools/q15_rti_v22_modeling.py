"""Frozen V22 train/calibration/policy modeling with no data access.

The caller must reserve and verify the exact non-test labels first.  This module
has no SQLite, network, Telegram, paper-ledger, promotion, or order capability.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence
import warnings

import numpy as np
import sklearn
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import LogisticRegression


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots import rti_microstructure_v22 as v22
from q15_upgrade.strategy_bots import rti_microstructure_v22_top_book_features as v22_features
from q15_upgrade.strategy_bots import rti_microstructure_v22_identity as identity
from q15_upgrade.strategy_bots.costs import (
    rti_simulated_execution,
    rti_simulated_net_pnl_cents,
)
from tools import q15_rti_v22_feature_seal as feature_seal


COHORTS = ("NON_BTC_TRANSFER", "BTC")
TRAIN_PARTITION = "TRAIN"
CALIBRATION_PARTITION = "PROBABILITY_CALIBRATION"
POLICY_PARTITION = "EXECUTION_POLICY_SELECTION"
TEST_PARTITION = "UNTOUCHED_TEST"
MARKET_FEATURE = "delayed_market_side_probability"
MODELING_VERSION = "q15-rti-v22-disjoint-pretest-modeling-v1"


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_contract(path: Path | None = None) -> dict[str, Any]:
    contract = v22.load_evaluator_contract(
        v22.DEFAULT_EVALUATOR if path is None else path
    )
    dependency = dict(contract["dependency_contract"])
    if sklearn.__version__ != dependency["exact_version"]:
        raise ValueError("v22_evaluator_scikit_learn_version_mismatch")
    return contract


def _candidate_specs(cohort: str, contract: Mapping[str, Any]) -> list[dict[str, Any]]:
    if cohort not in COHORTS:
        raise ValueError("v22_model_cohort_invalid")
    groups = list(dict(contract["candidate_models"])[cohort])
    output = []
    rank = 1
    for group in groups:
        family = str(group["family"])
        if family == "ELASTIC_NET_LOGISTIC":
            for c in group["C_grid"]:
                for l1_ratio in group["l1_ratio_grid"]:
                    output.append({
                        "family": family,
                        "C": float(c),
                        "l1_ratio": float(l1_ratio),
                        "complexity_rank": rank,
                    })
                    rank += 1
        elif family == "HIST_GRADIENT_BOOSTING":
            for learning_rate in group["learning_rate_grid"]:
                for leaves in group["max_leaf_nodes_grid"]:
                    for minimum in group["min_samples_leaf_grid"]:
                        for l2 in group["l2_regularization_grid"]:
                            output.append({
                                "family": family,
                                "learning_rate": float(learning_rate),
                                "max_leaf_nodes": int(leaves),
                                "min_samples_leaf": int(minimum),
                                "l2_regularization": float(l2),
                                "complexity_rank": rank,
                            })
                            rank += 1
        elif family == "RIDGE_LOGISTIC":
            for c in group["C_grid"]:
                output.append({
                    "family": family,
                    "C": float(c),
                    "complexity_rank": rank,
                })
                rank += 1
        else:
            raise ValueError("v22_model_family_invalid")
    expected = 28 if cohort == "NON_BTC_TRANSFER" else 4
    if len(output) != expected:
        raise ValueError("v22_model_candidate_grid_invalid")
    return output


def _model_id(spec: Mapping[str, Any]) -> str:
    family = str(spec["family"])
    if family == "ELASTIC_NET_LOGISTIC":
        return (
            f"{family}:C={float(spec['C']):.12g}:"
            f"l1_ratio={float(spec['l1_ratio']):.12g}"
        )
    if family == "RIDGE_LOGISTIC":
        return f"{family}:C={float(spec['C']):.12g}"
    if family == "HIST_GRADIENT_BOOSTING":
        return (
            f"{family}:learning_rate={float(spec['learning_rate']):.12g}:"
            f"max_leaf_nodes={int(spec['max_leaf_nodes'])}:"
            f"min_samples_leaf={int(spec['min_samples_leaf'])}:"
            f"l2_regularization={float(spec['l2_regularization']):.12g}"
        )
    raise ValueError("v22_model_family_invalid")


def _clip_probability(
    values: np.ndarray, contract: Mapping[str, Any],
) -> np.ndarray:
    low, high = dict(contract["preprocessing"])["probability_clip"]
    return np.clip(np.asarray(values, dtype=float), float(low), float(high))


def _matrix(
    rows: Sequence[Mapping[str, Any]],
    feature_indices: Sequence[int] | None = None,
) -> np.ndarray:
    try:
        matrix = np.asarray([list(row["features"]) for row in rows], dtype=float)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("v22_model_feature_matrix_invalid") from exc
    if (
        matrix.ndim != 2
        or matrix.shape != (len(rows), identity.FEATURE_COUNT)
        or not np.isfinite(matrix).all()
    ):
        raise ValueError("v22_model_feature_matrix_invalid")
    if feature_indices is None:
        return matrix
    try:
        indices = tuple(int(value) for value in feature_indices)
    except (TypeError, ValueError) as exc:
        raise ValueError("v22_model_feature_indices_invalid") from exc
    if (
        not indices
        or len(set(indices)) != len(indices)
        or min(indices) < 0
        or max(indices) >= identity.FEATURE_COUNT
    ):
        raise ValueError("v22_model_feature_indices_invalid")
    return matrix[:, indices]


def _labels(rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
    values = []
    for row in rows:
        value = row.get("label_survives")
        if isinstance(value, bool) or value not in (0, 1):
            raise ValueError("v22_model_label_invalid")
        values.append(int(value))
    return np.asarray(values, dtype=int)


def _fit_scaler(matrix: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if matrix.ndim != 2 or matrix.shape[1] <= 0:
        raise ValueError("v22_model_matrix_geometry_invalid")
    if not np.isfinite(matrix).all():
        raise ValueError("v22_model_matrix_nonfinite")
    center = np.median(matrix, axis=0)
    low = np.quantile(matrix, 0.25, axis=0, method="linear")
    high = np.quantile(matrix, 0.75, axis=0, method="linear")
    scale = high - low
    scale[~np.isfinite(scale) | (scale == 0.0)] = 1.0
    return center, scale


def _family_contract(spec: Mapping[str, Any], contract: Mapping[str, Any]) -> dict[str, Any]:
    family = str(spec["family"])
    matches = [
        dict(item) for groups in dict(contract["candidate_models"]).values()
        for item in groups if item["family"] == family
    ]
    if not matches:
        raise ValueError("v22_model_family_contract_missing")
    return matches[0]


def _build_estimator(spec: Mapping[str, Any], contract: Mapping[str, Any]) -> Any:
    family = str(spec["family"])
    config = _family_contract(spec, contract)
    seed = int(dict(contract["dependency_contract"])["deterministic_random_seed"])
    if family == "ELASTIC_NET_LOGISTIC":
        return LogisticRegression(
            C=float(spec["C"]),
            l1_ratio=float(spec["l1_ratio"]),
            penalty="elasticnet",
            solver=str(config["solver"]),
            class_weight=config["class_weight"],
            fit_intercept=bool(config["fit_intercept"]),
            tol=float(config["tol"]),
            max_iter=int(config["max_iter"]),
            random_state=seed,
            n_jobs=1,
        )
    if family == "RIDGE_LOGISTIC":
        return LogisticRegression(
            C=float(spec["C"]),
            penalty="l2",
            solver=str(config["solver"]),
            class_weight=config["class_weight"],
            fit_intercept=bool(config["fit_intercept"]),
            tol=float(config["tol"]),
            max_iter=int(config["max_iter"]),
            random_state=seed,
            n_jobs=1,
        )
    if family == "HIST_GRADIENT_BOOSTING":
        return HistGradientBoostingClassifier(
            learning_rate=float(spec["learning_rate"]),
            max_leaf_nodes=int(spec["max_leaf_nodes"]),
            min_samples_leaf=int(spec["min_samples_leaf"]),
            l2_regularization=float(spec["l2_regularization"]),
            max_iter=int(config["max_iter"]),
            max_bins=int(config["max_bins"]),
            early_stopping=bool(config["early_stopping"]),
            random_state=seed,
        )
    raise ValueError("v22_model_family_invalid")


def _fit(
    spec: Mapping[str, Any], rows: Sequence[Mapping[str, Any]],
    contract: Mapping[str, Any],
    *, feature_indices: Sequence[int] | None = None,
) -> dict[str, Any]:
    indices = (
        tuple(range(identity.FEATURE_COUNT))
        if feature_indices is None else tuple(int(value) for value in feature_indices)
    )
    matrix = _matrix(rows, indices)
    labels = _labels(rows)
    if len(set(labels.tolist())) != 2:
        raise ValueError("v22_model_training_single_class")
    center, scale = _fit_scaler(matrix)
    estimator = _build_estimator(spec, contract)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ConvergenceWarning)
        estimator.fit((matrix - center) / scale, labels)
    if any(issubclass(item.category, ConvergenceWarning) for item in caught):
        raise ValueError("v22_model_fit_did_not_converge")
    return {
        "spec": dict(spec),
        "model_id": _model_id(spec),
        "feature_indices": indices,
        "center": center,
        "scale": scale,
        "estimator": estimator,
    }


def _predict(
    fitted: Mapping[str, Any], rows: Sequence[Mapping[str, Any]],
    contract: Mapping[str, Any],
) -> np.ndarray:
    indices = tuple(fitted.get("feature_indices") or ())
    if not indices:
        raise ValueError("v22_model_fitted_feature_indices_missing")
    matrix = _matrix(rows, indices)
    probabilities = fitted["estimator"].predict_proba(
        (matrix - fitted["center"]) / fitted["scale"]
    )[:, 1]
    return _clip_probability(probabilities, contract)


def _proper_scores(labels: np.ndarray, probabilities: np.ndarray) -> dict[str, float]:
    if len(labels) == 0 or len(labels) != len(probabilities):
        raise ValueError("v22_model_score_geometry_invalid")
    return {
        "log_loss": float(-np.mean(
            labels * np.log(probabilities)
            + (1 - labels) * np.log(1.0 - probabilities)
        )),
        "brier_score": float(np.mean(np.square(probabilities - labels))),
    }


def _cluster_proper_scores(
    rows: Sequence[Mapping[str, Any]],
    labels: np.ndarray,
    probabilities: np.ndarray,
) -> dict[str, float]:
    """Give each close cluster equal weight even when executability varies."""
    if len(rows) == 0 or len(rows) != len(labels) or len(rows) != len(probabilities):
        raise ValueError("v22_model_cluster_score_geometry_invalid")
    losses: dict[float, list[float]] = defaultdict(list)
    briers: dict[float, list[float]] = defaultdict(list)
    for row, label, probability in zip(rows, labels, probabilities, strict=True):
        close = float(row["close_time"])
        value = float(probability)
        target = int(label)
        losses[close].append(-(
            target * math.log(value) + (1 - target) * math.log(1.0 - value)
        ))
        briers[close].append((value - target) ** 2)
    return {
        "log_loss": float(np.mean([
            np.mean(values) for _, values in sorted(losses.items())
        ])),
        "brier_score": float(np.mean([
            np.mean(values) for _, values in sorted(briers.items())
        ])),
        "complete_close_clusters": len(losses),
    }


def _rows_for_closes(
    rows: Sequence[Mapping[str, Any]], closes: set[float],
) -> list[dict[str, Any]]:
    return [dict(row) for row in rows if float(row["close_time"]) in closes]


def _market_probabilities(
    rows: Sequence[Mapping[str, Any]], contract: Mapping[str, Any],
) -> np.ndarray:
    index = v22_features.FEATURE_NAMES.index(MARKET_FEATURE)
    return _clip_probability(np.asarray([
        float(row["features"][index]) for row in rows
    ]), contract)


def _candidate_walk_forward(
    rows: Sequence[Mapping[str, Any]], spec: Mapping[str, Any], cohort: str,
    contract: Mapping[str, Any],
    *, feature_indices: Sequence[int] | None = None,
) -> dict[str, Any]:
    normalized_indices = (
        tuple(range(identity.FEATURE_COUNT))
        if feature_indices is None else tuple(int(index) for index in feature_indices)
    )
    if (
        not normalized_indices
        or len(set(normalized_indices)) != len(normalized_indices)
        or min(normalized_indices) < 0
        or max(normalized_indices) >= identity.FEATURE_COUNT
    ):
        raise ValueError("v22_model_feature_indices_invalid")
    train_closes = sorted({
        float(row["close_time"]) for row in rows
        if row["partition"] == TRAIN_PARTITION
    })
    if len(train_closes) != identity.TRAIN_CLOSE_WINDOWS:
        raise ValueError("v22_model_train_window_geometry_invalid")
    pooled_exec_labels = []
    pooled_exec_predictions = []
    pooled_exec_market = []
    pooled_exec_rows = []
    pooled_all_labels = []
    pooled_all_predictions = []
    pooled_all_rows = []
    fold_reports = []
    minimum_key = (
        "minimum_executable_rows_each_validation_fold_non_btc"
        if cohort == "NON_BTC_TRANSFER"
        else "minimum_executable_rows_each_validation_fold_btc"
    )
    minimum = int(dict(contract["model_selection"])[minimum_key])
    for fold_index, fold in enumerate(contract["internal_walk_forward_folds"]):
        train_bounds = fold["train"]
        validation_bounds = fold["validation"]
        train_set = set(train_closes[int(train_bounds[0]):int(train_bounds[1]) + 1])
        validation_set = set(
            train_closes[int(validation_bounds[0]):int(validation_bounds[1]) + 1]
        )
        if (
            len(train_set) != int(train_bounds[1]) - int(train_bounds[0]) + 1
            or len(validation_set)
            != int(validation_bounds[1]) - int(validation_bounds[0]) + 1
            or train_set.intersection(validation_set)
            or max(train_set) >= min(validation_set)
        ):
            raise ValueError("v22_model_fold_chronology_invalid")
        fit_rows = _rows_for_closes(rows, train_set)
        all_validation_rows = _rows_for_closes(rows, validation_set)
        executable_rows = [
            row for row in all_validation_rows
            if row.get("execution_supported") is True
        ]
        if len(executable_rows) < minimum:
            raise ValueError("v22_model_executable_validation_minimum_not_met")
        fitted = _fit(
            spec, fit_rows, contract, feature_indices=normalized_indices,
        )
        all_predictions = _predict(fitted, all_validation_rows, contract)
        executable_predictions = _predict(fitted, executable_rows, contract)
        all_labels = _labels(all_validation_rows)
        executable_labels = _labels(executable_rows)
        executable_market = _market_probabilities(executable_rows, contract)
        executable_scores = _cluster_proper_scores(
            executable_rows, executable_labels, executable_predictions,
        )
        market_scores = _cluster_proper_scores(
            executable_rows, executable_labels, executable_market,
        )
        all_scores = _cluster_proper_scores(
            all_validation_rows, all_labels, all_predictions,
        )
        pooled_exec_labels.extend(executable_labels.tolist())
        pooled_exec_predictions.extend(executable_predictions.tolist())
        pooled_exec_market.extend(executable_market.tolist())
        pooled_exec_rows.extend(executable_rows)
        pooled_all_labels.extend(all_labels.tolist())
        pooled_all_predictions.extend(all_predictions.tolist())
        pooled_all_rows.extend(all_validation_rows)
        fold_reports.append({
            "fold": fold_index + 1,
            "train_close_windows": len(train_set),
            "validation_close_windows": len(validation_set),
            "train_rows": len(fit_rows),
            "validation_feature_rows": len(all_validation_rows),
            "validation_executable_rows": len(executable_rows),
            "train_first_close_time": min(train_set),
            "train_last_close_time": max(train_set),
            "validation_first_close_time": min(validation_set),
            "validation_last_close_time": max(validation_set),
            "executable_log_loss": executable_scores["log_loss"],
            "executable_brier_score": executable_scores["brier_score"],
            "executable_market_log_loss": market_scores["log_loss"],
            "executable_market_brier_score": market_scores["brier_score"],
            "all_feature_rows_log_loss": all_scores["log_loss"],
            "all_feature_rows_brier_score": all_scores["brier_score"],
            "scaler_center_sha256": _canonical_sha256(fitted["center"].tolist()),
            "scaler_scale_sha256": _canonical_sha256(fitted["scale"].tolist()),
        })
    exec_labels = np.asarray(pooled_exec_labels, dtype=int)
    exec_predictions = np.asarray(pooled_exec_predictions, dtype=float)
    exec_market = np.asarray(pooled_exec_market, dtype=float)
    all_labels = np.asarray(pooled_all_labels, dtype=int)
    all_predictions = np.asarray(pooled_all_predictions, dtype=float)
    exec_scores = _cluster_proper_scores(
        pooled_exec_rows, exec_labels, exec_predictions,
    )
    market_scores = _cluster_proper_scores(
        pooled_exec_rows, exec_labels, exec_market,
    )
    all_scores = _cluster_proper_scores(
        pooled_all_rows, all_labels, all_predictions,
    )
    return {
        "model_id": _model_id(spec),
        "spec": dict(spec),
        "complexity_rank": int(spec["complexity_rank"]),
        "feature_count": len(normalized_indices),
        "feature_indices_sha256": _canonical_sha256(normalized_indices),
        "status": "VALID",
        "pooled_executable_validation_rows": len(exec_labels),
        "executable_log_loss": exec_scores["log_loss"],
        "executable_brier_score": exec_scores["brier_score"],
        "executable_market_log_loss": market_scores["log_loss"],
        "executable_market_brier_score": market_scores["brier_score"],
        "all_feature_rows_validation_rows": len(all_labels),
        "all_feature_rows_log_loss": all_scores["log_loss"],
        "all_feature_rows_brier_score": all_scores["brier_score"],
        "folds": fold_reports,
    }


def _fit_platt(
    base_probabilities: np.ndarray, labels: np.ndarray,
    contract: Mapping[str, Any],
) -> LogisticRegression:
    if len(set(labels.tolist())) != 2:
        raise ValueError("v22_platt_calibration_single_class")
    config = dict(contract["probability_calibration"])
    logits = np.log(
        _clip_probability(base_probabilities, contract)
        / (1.0 - _clip_probability(base_probabilities, contract))
    ).reshape(-1, 1)
    calibrator = LogisticRegression(
        C=float(config["C"]),
        penalty="l2",
        solver=str(config["solver"]),
        fit_intercept=bool(config["fit_intercept"]),
        tol=float(config["tol"]),
        max_iter=int(config["max_iter"]),
        random_state=int(dict(contract["dependency_contract"])[
            "deterministic_random_seed"
        ]),
        n_jobs=1,
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ConvergenceWarning)
        calibrator.fit(logits, labels)
    if any(issubclass(item.category, ConvergenceWarning) for item in caught):
        raise ValueError("v22_platt_fit_did_not_converge")
    return calibrator


def _platt_predict(
    calibrator: LogisticRegression, base_probabilities: np.ndarray,
    contract: Mapping[str, Any],
) -> np.ndarray:
    probabilities = _clip_probability(base_probabilities, contract)
    logits = np.log(probabilities / (1.0 - probabilities)).reshape(-1, 1)
    return _clip_probability(calibrator.predict_proba(logits)[:, 1], contract)


def _calibrated_predict(
    calibrator: Any, base_probabilities: np.ndarray,
    contract: Mapping[str, Any],
) -> np.ndarray:
    if isinstance(calibrator, Mapping):
        if dict(calibrator) != {"method": "IDENTITY"}:
            raise ValueError("v22_calibrator_identity_invalid")
        return _clip_probability(base_probabilities, contract)
    return _platt_predict(calibrator, base_probabilities, contract)


def _select_calibrator_on_policy(
    *, base_probabilities: np.ndarray, platt_calibrator: LogisticRegression,
    labels: np.ndarray, market_probabilities: np.ndarray,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    candidates = []
    for method, calibrator in (
        ("IDENTITY", {"method": "IDENTITY"}),
        ("L2_REGULARIZED_PLATT_ON_LOGIT", platt_calibrator),
    ):
        probabilities = _calibrated_predict(
            calibrator, base_probabilities, contract,
        )
        candidates.append({
            "method": method,
            "calibrator": calibrator,
            "probabilities": probabilities,
            "scores": _proper_scores(labels, probabilities),
        })
    selected = min(candidates, key=lambda item: (
        float(item["scores"]["log_loss"]),
        float(item["scores"]["brier_score"]),
        str(item["method"]),
    ))
    market_scores = _proper_scores(labels, market_probabilities)
    checks = {
        "selected_calibrator_log_loss_strictly_beats_12m_market": (
            selected["scores"]["log_loss"] < market_scores["log_loss"]
        ),
        "selected_calibrator_brier_strictly_beats_12m_market": (
            selected["scores"]["brier_score"] < market_scores["brier_score"]
        ),
    }
    return {
        "selected_method": selected["method"],
        "selected_calibrator": selected["calibrator"],
        "selected_probabilities": selected["probabilities"],
        "selected_scores": selected["scores"],
        "market_scores": market_scores,
        "candidate_scores": {
            item["method"]: item["scores"] for item in candidates
        },
        "gate_checks": checks,
        "gate_met": all(checks.values()),
    }


def _bootstrap_lower_mean_pnl(
    picks: Sequence[Mapping[str, Any]], *, seed: int,
    contract: Mapping[str, Any],
) -> float:
    clusters: dict[float, list[float]] = defaultdict(list)
    for row in picks:
        clusters[float(row["close_time"])].append(float(row["pnl_cents_10"]))
    values = list(clusters.values())
    if not values:
        raise ValueError("v22_bootstrap_no_picks")
    config = dict(contract["bootstrap"])
    rng = np.random.default_rng(seed)
    samples = np.empty(int(config["resamples"]), dtype=float)
    for index in range(len(samples)):
        selected = rng.integers(0, len(values), size=len(values))
        sample = [pnl for cluster in selected for pnl in values[int(cluster)]]
        samples[index] = float(np.mean(sample))
    return float(np.quantile(
        samples, float(config["policy_lower_percentile"]),
        method=str(config["quantile_method"]),
    ))


def _margin_report(
    rows: Sequence[Mapping[str, Any]], probabilities: np.ndarray,
    margin: float, cohort: str, contract: Mapping[str, Any],
) -> dict[str, Any]:
    policy = dict(contract["execution_policy_selection"])
    picks = []
    for row, probability in zip(rows, probabilities, strict=True):
        if row.get("execution_supported") is not True or row.get("sim_contracts") != 10.0:
            raise ValueError("v22_policy_fake_or_partial_fill_forbidden")
        execution = rti_simulated_execution(row["entry_ask_cents"], 10, 2.0)
        if execution is None:
            raise ValueError("v22_policy_execution_evidence_invalid")
        break_even = float(execution["fee_slippage_breakeven_rate"])
        edge = float(probability) - break_even
        if edge < margin:
            continue
        pnl = rti_simulated_net_pnl_cents(
            row["entry_ask_cents"], bool(int(row["label_survives"])), 10, 2.0,
        )
        if pnl is None:
            raise ValueError("v22_policy_execution_evidence_invalid")
        picks.append({
            "parent_id": int(row["parent_id"]),
            "close_time": float(row["close_time"]),
            "asset": str(row["asset"]),
            "side": str(row["side"]),
            "probability": float(probability),
            "break_even_probability": break_even,
            "edge": edge,
            "correct": int(row["label_survives"]),
            "pnl_cents_10": float(pnl) * 10,
        })
    yes_picks = sum(row["side"] == "YES" for row in picks)
    no_picks = sum(row["side"] == "NO" for row in picks)
    minimum = int(policy[
        "minimum_picks_non_btc" if cohort == "NON_BTC_TRANSFER"
        else "minimum_picks_btc"
    ])
    side_minimum = int(policy["minimum_picks_each_side"])
    volume_gate = (
        len(picks) >= minimum
        and yes_picks >= side_minimum
        and no_picks >= side_minimum
    )
    lower = None
    if picks:
        lower = _bootstrap_lower_mean_pnl(
            picks,
            seed=int(dict(contract["bootstrap"])["random_seed"])
            + (0 if cohort == "NON_BTC_TRANSFER" else 10_000)
            + int(round(margin * 100)),
            contract=contract,
        )
    pnl_values = [float(row["pnl_cents_10"]) for row in picks]
    observed_mean = float(np.mean(pnl_values)) if pnl_values else None
    return {
        "margin": float(margin),
        "executable_rows_considered": len(rows),
        "picks": len(picks),
        "yes_picks": yes_picks,
        "no_picks": no_picks,
        "trade_frequency_per_complete_window": len(picks)
        / identity.EXECUTION_POLICY_SELECTION_CLOSE_WINDOWS,
        "accuracy": (
            sum(row["correct"] for row in picks) / len(picks) if picks else None
        ),
        "fee_slippage_adjusted_pnl_cents_10_contracts": sum(pnl_values),
        "observed_mean_pnl_cents_per_10_contract_pick": observed_mean,
        "bootstrap_20th_percentile_mean_pnl_cents_per_10_contract_pick": lower,
        "volume_and_side_gate_met": volume_gate,
        "positive_bootstrap_lower_gate_met": bool(lower is not None and lower > 0.0),
        "gate_met": bool(volume_gate and lower is not None and lower > 0.0),
        "pick_parent_ids_sha256": _canonical_sha256(tuple(sorted(
            row["parent_id"] for row in picks
        ))),
    }


def _labeled_pretest_rows(
    seal: Mapping[str, Any], labels: Mapping[int, int],
) -> list[dict[str, Any]]:
    feature_seal.validate_seal(seal)
    partitions = {TRAIN_PARTITION, CALIBRATION_PARTITION, POLICY_PARTITION}
    rows = [dict(row) for row in seal["rows"] if row["partition"] in partitions]
    expected_ids = required_pretest_label_ids(seal)
    normalized = {}
    for raw_id, value in labels.items():
        try:
            row_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("v22_pretest_label_identity_invalid") from exc
        if isinstance(value, bool) or value not in (0, 1):
            raise ValueError("v22_pretest_label_invalid")
        if row_id in normalized:
            raise ValueError("v22_pretest_label_identity_invalid")
        normalized[row_id] = int(value)
    if set(normalized) != expected_ids or len(rows) != 155 * 7:
        raise ValueError("v22_pretest_label_identity_invalid")
    return [
        (
            {**row, "label_survives": normalized[int(row["parent_id"])]}
            if int(row["parent_id"]) in normalized else row
        )
        for row in rows
    ]


def required_pretest_label_ids(seal: Mapping[str, Any]) -> set[int]:
    """Minimize label access to rows actually consumed before untouched test."""
    feature_seal.validate_seal(seal)
    return {
        int(row["parent_id"])
        for row in seal["rows"]
        if row["partition"] in {TRAIN_PARTITION, CALIBRATION_PARTITION}
        or (
            row["partition"] == POLICY_PARTITION
            and row.get("execution_supported") is True
        )
    }


def evaluate_cohort(
    rows: Sequence[Mapping[str, Any]], cohort: str,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    if cohort not in COHORTS:
        raise ValueError("v22_model_cohort_invalid")
    cohort_rows = [dict(row) for row in rows if row["cohort"] == cohort]
    train_rows = [row for row in cohort_rows if row["partition"] == TRAIN_PARTITION]
    calibration_all = [
        row for row in cohort_rows if row["partition"] == CALIBRATION_PARTITION
    ]
    policy_all = [row for row in cohort_rows if row["partition"] == POLICY_PARTITION]
    expected_per_window = 6 if cohort == "NON_BTC_TRANSFER" else 1
    expected = {
        TRAIN_PARTITION: identity.TRAIN_CLOSE_WINDOWS,
        CALIBRATION_PARTITION: identity.PROBABILITY_CALIBRATION_CLOSE_WINDOWS,
        POLICY_PARTITION: identity.EXECUTION_POLICY_SELECTION_CLOSE_WINDOWS,
    }
    by_partition = {
        TRAIN_PARTITION: train_rows,
        CALIBRATION_PARTITION: calibration_all,
        POLICY_PARTITION: policy_all,
    }
    if any(
        len(by_partition[name]) != windows * expected_per_window
        or len({float(row["close_time"]) for row in by_partition[name]}) != windows
        for name, windows in expected.items()
    ):
        raise ValueError("v22_model_cohort_geometry_invalid")
    candidate_reports = []
    for spec in _candidate_specs(cohort, contract):
        try:
            candidate_reports.append(
                _candidate_walk_forward(cohort_rows, spec, cohort, contract)
            )
        except ValueError as exc:
            candidate_reports.append({
                "model_id": _model_id(spec),
                "spec": dict(spec),
                "complexity_rank": int(spec["complexity_rank"]),
                "status": "INVALID",
                "failure": str(exc),
            })
    valid = [item for item in candidate_reports if item["status"] == "VALID"]
    if not valid:
        return {
            "report": {
                "cohort": cohort,
                "status": "NO_VALID_INTERNAL_WALK_FORWARD_CANDIDATE",
                "candidate_reports": candidate_reports,
                "internal_walk_forward_gate_met": False,
                "probability_calibration_gate_met": False,
                "execution_policy_gate_met": False,
                "pretest_gate_met": False,
            },
            "artifact": None,
        }
    selected = min(valid, key=lambda item: (
        float(item["executable_log_loss"]),
        float(item["executable_brier_score"]),
        float(item["all_feature_rows_log_loss"]),
        int(item["complexity_rank"]),
        str(item["model_id"]),
    ))
    internal_gate = (
        float(selected["executable_log_loss"])
        < float(selected["executable_market_log_loss"])
    )
    if not internal_gate:
        return {
            "report": {
                "cohort": cohort,
                "status": "INTERNAL_WALK_FORWARD_MARKET_GATE_FAILED",
                "candidate_reports": candidate_reports,
                "selected_model_id": selected["model_id"],
                "selected_model": selected,
                "internal_walk_forward_gate_met": False,
                "probability_calibration_gate_met": False,
                "execution_policy_gate_met": False,
                "pretest_gate_met": False,
            },
            "artifact": None,
        }
    fitted = _fit(dict(selected["spec"]), train_rows, contract)
    calibration_exec_diagnostic = [
        row for row in calibration_all if row.get("execution_supported") is True
    ]
    calibration_config = dict(contract["probability_calibration"])
    calibration_minimum = int(calibration_config[
        "minimum_rows_non_btc" if cohort == "NON_BTC_TRANSFER"
        else "minimum_rows_btc"
    ])
    if len(calibration_all) < calibration_minimum:
        return {
            "report": {
                "cohort": cohort,
                "status": "PROBABILITY_CALIBRATION_EXECUTABLE_VOLUME_FAILED",
                "candidate_reports": candidate_reports,
                "selected_model_id": selected["model_id"],
                "selected_model": selected,
                "internal_walk_forward_gate_met": True,
                "probability_calibration_gate_met": False,
                "execution_policy_gate_met": False,
                "pretest_gate_met": False,
            },
            "artifact": None,
        }
    base_calibration = _predict(fitted, calibration_all, contract)
    calibration_labels = _labels(calibration_all)
    try:
        calibrator = _fit_platt(base_calibration, calibration_labels, contract)
    except ValueError as exc:
        return {
            "report": {
                "cohort": cohort,
                "status": "PROBABILITY_CALIBRATION_FAILED",
                "failure": str(exc),
                "candidate_reports": candidate_reports,
                "selected_model_id": selected["model_id"],
                "selected_model": selected,
                "internal_walk_forward_gate_met": True,
                "probability_calibration_gate_met": False,
                "execution_policy_gate_met": False,
                "pretest_gate_met": False,
            },
            "artifact": None,
        }
    calibrated = _platt_predict(calibrator, base_calibration, contract)
    base_scores = _proper_scores(calibration_labels, base_calibration)
    calibrated_scores = _proper_scores(calibration_labels, calibrated)
    # These are transparent diagnostics only.  A calibrator cannot honestly
    # validate itself on the same rows used to fit it; the frozen gate below
    # uses only the disjoint executable policy partition.
    ablation_config = dict(contract["base_feature_ablation"])
    ablation_bounds = list(ablation_config["feature_indices_zero_based"])
    ablation_indices = tuple(range(
        int(ablation_bounds[0]), int(ablation_bounds[1]) + 1,
    ))
    if len(ablation_indices) != int(ablation_config["feature_count"]):
        raise ValueError("v22_base_ablation_feature_geometry_invalid")
    ablation_candidate_reports = []
    for spec in _candidate_specs(cohort, contract):
        try:
            ablation_candidate_reports.append(_candidate_walk_forward(
                cohort_rows, spec, cohort, contract,
                feature_indices=ablation_indices,
            ))
        except ValueError as exc:
            ablation_candidate_reports.append({
                "model_id": _model_id(spec),
                "spec": dict(spec),
                "complexity_rank": int(spec["complexity_rank"]),
                "feature_count": len(ablation_indices),
                "feature_indices_sha256": _canonical_sha256(ablation_indices),
                "status": "INVALID",
                "failure": str(exc),
            })
    valid_ablation = [
        item for item in ablation_candidate_reports
        if item["status"] == "VALID"
    ]
    if not valid_ablation:
        return {
            "report": {
                "cohort": cohort,
                "status": "BASE_FEATURE_ABLATION_WALK_FORWARD_SELECTION_FAILED",
                "candidate_reports": candidate_reports,
                "selected_model_id": selected["model_id"],
                "selected_model": selected,
                "internal_walk_forward_gate_met": True,
                "base_feature_ablation_candidate_reports": (
                    ablation_candidate_reports
                ),
                "base_feature_ablation_available": False,
                "probability_calibration_gate_met": False,
                "execution_policy_gate_met": False,
                "pretest_gate_met": False,
            },
            "artifact": None,
        }
    ablation_selected = min(valid_ablation, key=lambda item: (
        float(item["executable_log_loss"]),
        float(item["executable_brier_score"]),
        float(item["all_feature_rows_log_loss"]),
        int(item["complexity_rank"]),
        str(item["model_id"]),
    ))
    try:
        ablation_fitted = _fit(
            dict(ablation_selected["spec"]), train_rows, contract,
            feature_indices=ablation_indices,
        )
        ablation_base_calibration = _predict(
            ablation_fitted, calibration_all, contract,
        )
        ablation_calibrator = _fit_platt(
            ablation_base_calibration, calibration_labels, contract,
        )
        ablation_calibrated = _platt_predict(
            ablation_calibrator, ablation_base_calibration, contract,
        )
        ablation_calibration_scores = _proper_scores(
            calibration_labels, ablation_calibrated,
        )
    except ValueError as exc:
        return {
            "report": {
                "cohort": cohort,
                "status": "BASE_FEATURE_ABLATION_FAILED",
                "failure": str(exc),
                "candidate_reports": candidate_reports,
                "selected_model_id": selected["model_id"],
                "selected_model": selected,
                "internal_walk_forward_gate_met": True,
                "calibration_feature_rows_used_for_fit": len(calibration_all),
                "calibration_executable_rows_diagnostic": len(
                    calibration_exec_diagnostic
                ),
                "calibration_base_scores_in_sample_diagnostic": base_scores,
                "calibration_regularized_platt_scores_in_sample_diagnostic": (
                    calibrated_scores
                ),
                "base_feature_ablation_candidate_reports": (
                    ablation_candidate_reports
                ),
                "base_feature_ablation_selected_model_id": (
                    ablation_selected["model_id"]
                ),
                "probability_calibration_gate_met": False,
                "base_feature_ablation_available": False,
                "execution_policy_gate_met": False,
                "pretest_gate_met": False,
            },
            "artifact": None,
        }
    policy_exec = [row for row in policy_all if row.get("execution_supported") is True]
    if not policy_exec:
        return {
            "report": {
                "cohort": cohort,
                "status": "DISJOINT_EXECUTION_POLICY_VOLUME_FAILED",
                "candidate_reports": candidate_reports,
                "selected_model_id": selected["model_id"],
                "selected_model": selected,
                "internal_walk_forward_gate_met": True,
                "calibration_feature_rows_used_for_fit": len(calibration_all),
                "calibration_executable_rows_diagnostic": len(
                    calibration_exec_diagnostic
                ),
                "calibration_base_scores_in_sample_diagnostic": base_scores,
                "calibration_regularized_platt_scores_in_sample_diagnostic": (
                    calibrated_scores
                ),
                "probability_calibration_gate_met": False,
                "base_feature_ablation_available": True,
                "base_feature_ablation_calibration_scores": (
                    ablation_calibration_scores
                ),
                "execution_policy_gate_met": False,
                "pretest_gate_met": False,
            },
            "artifact": None,
        }
    policy_labels = _labels(policy_exec)
    policy_base_probabilities = _predict(fitted, policy_exec, contract)
    policy_market_probabilities = _market_probabilities(policy_exec, contract)
    calibration_selection = _select_calibrator_on_policy(
        base_probabilities=policy_base_probabilities,
        platt_calibrator=calibrator,
        labels=policy_labels,
        market_probabilities=policy_market_probabilities,
        contract=contract,
    )
    policy_probabilities = calibration_selection["selected_probabilities"]
    calibration_gate = calibration_selection["gate_met"]
    if not calibration_gate:
        return {
            "report": {
                "cohort": cohort,
                "status": "DISJOINT_POLICY_CALIBRATION_PROPER_SCORE_GATE_FAILED",
                "candidate_reports": candidate_reports,
                "selected_model_id": selected["model_id"],
                "selected_model": selected,
                "internal_walk_forward_gate_met": True,
                "calibration_feature_rows_used_for_fit": len(calibration_all),
                "calibration_executable_rows_diagnostic": len(
                    calibration_exec_diagnostic
                ),
                "calibration_base_scores_in_sample_diagnostic": base_scores,
                "calibration_regularized_platt_scores_in_sample_diagnostic": (
                    calibrated_scores
                ),
                "calibration_in_sample_scores_used_for_gate": False,
                "policy_executable_rows": len(policy_exec),
                "policy_calibrator_candidate_scores": calibration_selection[
                    "candidate_scores"
                ],
                "selected_calibrator_method": calibration_selection[
                    "selected_method"
                ],
                "policy_selected_calibrator_scores": calibration_selection[
                    "selected_scores"
                ],
                "policy_market_scores": calibration_selection["market_scores"],
                "probability_calibration_gate_checks": calibration_selection[
                    "gate_checks"
                ],
                "probability_calibration_gate_met": False,
                "execution_policy_gate_met": False,
                "pretest_gate_met": False,
            },
            "artifact": None,
        }
    ablation_policy_base = _predict(ablation_fitted, policy_exec, contract)
    ablation_selection = _select_calibrator_on_policy(
        base_probabilities=ablation_policy_base,
        platt_calibrator=ablation_calibrator,
        labels=policy_labels,
        market_probabilities=policy_market_probabilities,
        contract=contract,
    )
    margin_reports = [
        _margin_report(policy_exec, policy_probabilities, float(margin), cohort, contract)
        for margin in dict(contract["execution_policy_selection"])["edge_margin_grid"]
    ]
    passing = [item for item in margin_reports if item["gate_met"]]
    selected_margin = None
    if passing:
        selected_margin = max(passing, key=lambda item: (
            float(item[
                "bootstrap_20th_percentile_mean_pnl_cents_per_10_contract_pick"
            ]),
            float(item["observed_mean_pnl_cents_per_10_contract_pick"]),
            int(item["picks"]),
            -float(item["margin"]),
        ))
    policy_gate = selected_margin is not None
    report = {
        "cohort": cohort,
        "status": (
            "PRETEST_GATES_PASSED_UNTOUCHED_TEST_STILL_SEALED"
            if policy_gate else "DISJOINT_EXECUTION_POLICY_GATE_FAILED"
        ),
        "candidate_reports": candidate_reports,
        "selected_model_id": selected["model_id"],
        "selected_model": selected,
        "internal_walk_forward_gate_met": True,
        "train_rows": len(train_rows),
        "train_complete_close_windows": identity.TRAIN_CLOSE_WINDOWS,
        "calibration_feature_rows": len(calibration_all),
        "calibration_feature_rows_used_for_fit": len(calibration_all),
        "calibration_executable_rows_diagnostic": len(
            calibration_exec_diagnostic
        ),
        "calibration_complete_close_windows": (
            identity.PROBABILITY_CALIBRATION_CLOSE_WINDOWS
        ),
        "calibration_base_scores_in_sample_diagnostic": base_scores,
        "calibration_regularized_platt_scores_in_sample_diagnostic": (
            calibrated_scores
        ),
        "calibration_in_sample_scores_used_for_gate": False,
        "calibration_labels_used_for_policy_selection": False,
        "policy_calibrator_candidate_scores": calibration_selection[
            "candidate_scores"
        ],
        "selected_calibrator_method": calibration_selection["selected_method"],
        "policy_selected_calibrator_scores": calibration_selection[
            "selected_scores"
        ],
        "policy_market_scores": calibration_selection["market_scores"],
        "probability_calibration_gate_checks": calibration_selection[
            "gate_checks"
        ],
        "probability_calibration_gate_met": True,
        "platt_intercept": float(calibrator.intercept_[0]),
        "platt_slope": float(calibrator.coef_[0, 0]),
        "base_feature_ablation_available": True,
        "base_feature_ablation_feature_count": len(ablation_indices),
        "base_feature_ablation_feature_indices_sha256": _canonical_sha256(
            ablation_indices
        ),
        "base_feature_ablation_candidate_reports": (
            ablation_candidate_reports
        ),
        "base_feature_ablation_selected_model_id": (
            ablation_selected["model_id"]
        ),
        "base_feature_ablation_selected_model": ablation_selected,
        "base_feature_ablation_selected_independently": True,
        "base_feature_ablation_calibration_scores": (
            ablation_calibration_scores
        ),
        "base_feature_ablation_selected_calibrator_method": (
            ablation_selection["selected_method"]
        ),
        "base_feature_ablation_policy_calibrator_candidate_scores": (
            ablation_selection["candidate_scores"]
        ),
        "base_feature_ablation_used_for_v22_model_selection_calibration_or_policy_selection": False,
        "policy_feature_rows": len(policy_all),
        "policy_executable_rows": len(policy_exec),
        "policy_complete_close_windows": identity.EXECUTION_POLICY_SELECTION_CLOSE_WINDOWS,
        "policy_labels_used_for_model_fit_or_calibrator_fit": False,
        "policy_labels_used_for_disjoint_calibration_gate": True,
        "margin_reports": margin_reports,
        "selected_margin": selected_margin,
        "execution_policy_gate_met": policy_gate,
        "pretest_gate_met": policy_gate,
    }
    return {
        "report": report,
        "artifact": (
            {
                "cohort": cohort,
                "selected_spec": dict(selected["spec"]),
                "selected_model_id": selected["model_id"],
                "base_model": fitted,
                "platt_calibrator": calibration_selection[
                    "selected_calibrator"
                ],
                "selected_calibrator_method": calibration_selection[
                    "selected_method"
                ],
                "base_feature_ablation_base_model": ablation_fitted,
                "base_feature_ablation_selected_spec": dict(
                    ablation_selected["spec"]
                ),
                "base_feature_ablation_selected_model_id": (
                    ablation_selected["model_id"]
                ),
                "base_feature_ablation_platt_calibrator": (
                    ablation_selection["selected_calibrator"]
                ),
                "base_feature_ablation_selected_calibrator_method": (
                    ablation_selection["selected_method"]
                ),
                "selected_margin": float(selected_margin["margin"]),
            }
            if policy_gate else None
        ),
    }


def evaluate_pretest(
    seal: Mapping[str, Any], train_calibration_policy_labels: Mapping[int, int],
    *, contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    frozen = load_contract()
    contract = dict(frozen if contract is None else contract)
    if contract != frozen:
        raise ValueError("v22_evaluator_contract_override_forbidden")
    rows = _labeled_pretest_rows(seal, train_calibration_policy_labels)
    cohort_results = {
        cohort: evaluate_cohort(rows, cohort, contract) for cohort in COHORTS
    }
    reports = {cohort: cohort_results[cohort]["report"] for cohort in COHORTS}
    artifacts = {cohort: cohort_results[cohort]["artifact"] for cohort in COHORTS}
    gate_met = all(reports[cohort].get("pretest_gate_met") is True for cohort in COHORTS)
    label_pairs = tuple(sorted(
        (int(row_id), int(value))
        for row_id, value in train_calibration_policy_labels.items()
    ))
    report = {
        "modeling_version": MODELING_VERSION,
        "evaluator_contract_id": identity.EVALUATOR_CONTRACT_ID,
        "evaluator_contract_sha256": identity.EVALUATOR_CONTRACT_SHA256,
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "feature_seal_sha256": seal["seal_sha256"],
        "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
        "feature_names_sha256": identity.FEATURE_NAMES_SHA256,
        "train_calibration_policy_label_rows": len(label_pairs),
        "train_calibration_policy_label_ids_sha256": _canonical_sha256(tuple(
            row_id for row_id, _label in label_pairs
        )),
        "train_calibration_policy_labels_sha256": _canonical_sha256(label_pairs),
        "calibration_and_policy_partitions_disjoint": True,
        "cohorts": reports,
        "pretest_gate_met": gate_met,
        "status": (
            "V22_PRETEST_GATES_PASSED_UNTOUCHED_TEST_MAY_BE_MANUALLY_OPENED_ONCE"
            if gate_met else "V22_PRETEST_GATE_FAILED_UNTOUCHED_TEST_REMAINS_SEALED"
        ),
        "outcome_labels_read": True,
        "untouched_test_labels_read": False,
        "model_fit_performed": True,
        "probability_scoring_performed": True,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "telegram_allowed": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }
    return {"report": report, "artifacts": artifacts}


def _wilson(successes: int, total: int, z: float) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    probability = successes / total
    denominator = 1.0 + z * z / total
    center = (probability + z * z / (2.0 * total)) / denominator
    half = z * math.sqrt(
        probability * (1.0 - probability) / total
        + z * z / (4.0 * total * total)
    ) / denominator
    return max(0.0, center - half), min(1.0, center + half)


def _maximum_drawdown(records: Sequence[Mapping[str, Any]]) -> float:
    cumulative = 0.0
    peak = 0.0
    maximum = 0.0
    for row in sorted(records, key=lambda item: (
        float(item["close_time"]), str(item["asset"]), int(item["parent_id"]),
    )):
        cumulative += float(row["pnl_cents_10"])
        peak = max(peak, cumulative)
        maximum = max(maximum, peak - cumulative)
    return maximum


def _trade_metrics(
    records: Sequence[Mapping[str, Any]], complete_windows: int,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    count = len(records)
    correct = sum(int(row["correct"]) for row in records)
    low, high = _wilson(
        correct, count, float(dict(contract["untouched_test"])["wilson_z"]),
    )
    pnl = sum(float(row["pnl_cents_10"]) for row in records)
    maximum_drawdown = _maximum_drawdown(records)
    return {
        "picks": count,
        "yes_picks": sum(str(row["side"]) == "YES" for row in records),
        "no_picks": sum(str(row["side"]) == "NO" for row in records),
        "correct": correct,
        "accuracy": correct / count if count else None,
        "wilson_95_low": low,
        "wilson_95_high": high,
        "trade_frequency_per_complete_window": count / complete_windows,
        "average_fee_slippage_adjusted_break_even": (
            sum(float(row["break_even_probability"]) for row in records) / count
            if count else None
        ),
        "fee_slippage_adjusted_pnl_cents_10_contracts": pnl,
        "ev_cents_per_10_contract_pick": pnl / count if count else None,
        "maximum_drawdown_cents_10_contracts": maximum_drawdown,
        "maximum_drawdown_cents_per_pick": (
            maximum_drawdown / count if count else None
        ),
        "parent_ids_sha256": _canonical_sha256(tuple(sorted(
            int(row["parent_id"]) for row in records
        ))),
    }


def _accuracy_only_metrics(
    rows: Sequence[Mapping[str, Any]], complete_windows: int,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    count = len(rows)
    correct = sum(int(row["label_survives"]) for row in rows)
    low, high = _wilson(
        correct, count, float(dict(contract["untouched_test"])["wilson_z"]),
    )
    return {
        "picks": count,
        "yes_picks": sum(str(row["side"]) == "YES" for row in rows),
        "no_picks": sum(str(row["side"]) == "NO" for row in rows),
        "correct": correct,
        "accuracy": correct / count if count else None,
        "wilson_95_low": low,
        "wilson_95_high": high,
        "trade_frequency_per_complete_window": count / complete_windows,
        "pnl_reported": False,
        "pnl_not_reported_reason": (
            "MATCHED_FROZEN_V21_FEATURE_AVAILABILITY_DIAGNOSTIC_ONLY"
        ),
        "parent_ids_sha256": _canonical_sha256(tuple(sorted(
            int(row["parent_id"]) for row in rows
        ))),
    }


def _trade_record(
    row: Mapping[str, Any], probability: float,
) -> dict[str, Any]:
    if row.get("execution_supported") is not True or row.get("sim_contracts") != 10.0:
        raise ValueError("v22_untouched_test_fake_or_partial_fill_forbidden")
    execution = rti_simulated_execution(row["entry_ask_cents"], 10, 2.0)
    pnl = rti_simulated_net_pnl_cents(
        row["entry_ask_cents"], bool(int(row["label_survives"])), 10, 2.0,
    )
    if execution is None or pnl is None:
        raise ValueError("v22_untouched_test_execution_invalid")
    return {
        **dict(row),
        "probability": float(probability),
        "simulated_fill_cents": float(execution["simulated_fill_cents"]),
        "break_even_probability": float(
            execution["fee_slippage_breakeven_rate"]
        ),
        "correct": int(row["label_survives"]),
        "pnl_cents_10": float(pnl) * 10.0,
    }


def _cluster_bootstrap_report(
    records: Sequence[Mapping[str, Any]], *, seed: int,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    if not records:
        return {
            "clusters": 0,
            "resamples": 0,
            "mean_pnl_cents_per_pick_interval_95": [None, None],
            "accuracy_minus_break_even_interval_95": [None, None],
            "mean_pnl_cents_per_pick_20th_percentile": None,
        }
    grouped: dict[float, list[Mapping[str, Any]]] = defaultdict(list)
    for row in records:
        grouped[float(row["close_time"])].append(row)
    clusters = list(grouped.values())
    config = dict(contract["untouched_test"])
    resamples = int(config["bootstrap_resamples"])
    low_q, high_q = config["bootstrap_quantiles"]
    rng = np.random.default_rng(seed)
    pnl_samples = np.empty(resamples, dtype=float)
    accuracy_edge_samples = np.empty(resamples, dtype=float)
    for index in range(resamples):
        selected = rng.integers(0, len(clusters), size=len(clusters))
        sample = [
            row for cluster_index in selected
            for row in clusters[int(cluster_index)]
        ]
        pnl_samples[index] = float(np.mean([
            float(row["pnl_cents_10"]) for row in sample
        ]))
        accuracy_edge_samples[index] = float(np.mean([
            float(row["correct"]) - float(row["break_even_probability"])
            for row in sample
        ]))
    method = str(dict(contract["bootstrap"])["quantile_method"])
    return {
        "clusters": len(clusters),
        "resamples": resamples,
        "mean_pnl_cents_per_pick_interval_95": [
            float(np.quantile(pnl_samples, low_q, method=method)),
            float(np.quantile(pnl_samples, high_q, method=method)),
        ],
        "accuracy_minus_break_even_interval_95": [
            float(np.quantile(accuracy_edge_samples, low_q, method=method)),
            float(np.quantile(accuracy_edge_samples, high_q, method=method)),
        ],
        "mean_pnl_cents_per_pick_20th_percentile": float(np.quantile(
            pnl_samples, float(config["bootstrap_policy_lower_quantile"]),
            method=method,
        )),
    }


def _tier(value: float, tiers: Sequence[Mapping[str, Any]]) -> str:
    for tier in tiers:
        minimum = tier.get("minimum_inclusive")
        maximum = tier.get("maximum_exclusive")
        if (
            (minimum is None or value >= float(minimum))
            and (maximum is None or value < float(maximum))
        ):
            return str(tier["name"])
    raise ValueError("v22_untouched_test_subgroup_tier_invalid")


def _subgroup_values(
    row: Mapping[str, Any], probability: float,
    contract: Mapping[str, Any],
) -> dict[str, str]:
    config = dict(contract["reporting_subgroups"])
    feature_map = dict(zip(
        v22_features.FEATURE_NAMES, row["features"], strict=True,
    ))
    efficiency = float(feature_map["rest_mid_path_trend_efficiency"])
    side_move = float(feature_map["side_rest_mid_return_90s_bps"])
    regime = (
        "CHOP" if efficiency < 0.30
        else "TREND_ALIGNED" if efficiency >= 0.50 and side_move > 0.0
        else "MIXED"
    )
    return {
        "ASSET": str(row["asset"]),
        "RTI_SIDE": str(row["side"]),
        "ABSOLUTE_DISTANCE_TIER": _tier(
            abs(float(feature_map["delayed_distance_bps"])),
            config["distance_absolute_bps_tiers"],
        ),
        "VOLATILITY_TIER": _tier(
            math.expm1(max(0.0, float(
                feature_map["log1p_parent_realized_volatility_bps"]
            ))),
            config["volatility_raw_bps_tiers"],
        ),
        "MARKET_REGIME": regime,
        "REST_IMBALANCE_PERSISTENCE_TIER": _tier(
            float(feature_map["rest_top_imbalance_side_persistence"]),
            config["rest_imbalance_persistence_tiers"],
        ),
        "REST_SPREAD_TIER": _tier(
            math.expm1(max(0.0, float(
                feature_map["log1p_rest_spread_bps_max"]
            ))),
            config["rest_spread_bps_tiers"],
        ),
        "REST_PATH_CURVATURE_TIER": _tier(
            float(feature_map["side_rest_mid_acceleration_30v90_bps"]),
            config["rest_path_curvature_bps_tiers"],
        ),
    }


def _subgroup_report(
    records: Sequence[Mapping[str, Any]], complete_windows: int,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    output = {}
    keys = (
        "ASSET", "RTI_SIDE", "ABSOLUTE_DISTANCE_TIER", "VOLATILITY_TIER",
        "MARKET_REGIME", "REST_IMBALANCE_PERSISTENCE_TIER",
        "REST_SPREAD_TIER", "REST_PATH_CURVATURE_TIER",
    )
    for key in keys:
        grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in records:
            grouped[str(row["subgroups"][key])].append(row)
        output[key] = {
            name: _trade_metrics(values, complete_windows, contract)
            for name, values in sorted(grouped.items())
        }
    return output


def _score_untouched_cohort(
    rows: Sequence[Mapping[str, Any]], cohort: str,
    artifact: Mapping[str, Any], contract: Mapping[str, Any],
) -> dict[str, Any]:
    cohort_rows = [dict(row) for row in rows if row["cohort"] == cohort]
    complete_windows = identity.UNTOUCHED_TEST_CLOSE_WINDOWS
    expected_rows = complete_windows * (6 if cohort == "NON_BTC_TRANSFER" else 1)
    required_artifact_keys = {
        "base_model", "platt_calibrator", "selected_margin",
        "selected_spec", "selected_model_id", "selected_calibrator_method",
        "base_feature_ablation_base_model",
        "base_feature_ablation_selected_spec",
        "base_feature_ablation_selected_model_id",
        "base_feature_ablation_platt_calibrator",
        "base_feature_ablation_selected_calibrator_method",
    }
    base_model = artifact.get("base_model")
    ablation_model = artifact.get("base_feature_ablation_base_model")
    if (
        len(cohort_rows) != expected_rows
        or len({float(row["close_time"]) for row in cohort_rows}) != complete_windows
        or artifact.get("cohort") != cohort
        or not required_artifact_keys.issubset(artifact)
        or artifact.get("selected_calibrator_method")
        not in {"IDENTITY", "L2_REGULARIZED_PLATT_ON_LOGIT"}
        or artifact.get("base_feature_ablation_selected_calibrator_method")
        not in {"IDENTITY", "L2_REGULARIZED_PLATT_ON_LOGIT"}
        or not isinstance(base_model, Mapping)
        or not isinstance(ablation_model, Mapping)
        or artifact.get("selected_model_id") != base_model.get("model_id")
        or dict(artifact.get("selected_spec") or {})
        != dict(base_model.get("spec") or {})
        or artifact.get("base_feature_ablation_selected_model_id")
        != ablation_model.get("model_id")
        or dict(artifact.get("base_feature_ablation_selected_spec") or {})
        != dict(ablation_model.get("spec") or {})
        or tuple(ablation_model.get("feature_indices") or ())
        != tuple(range(int(dict(contract["base_feature_ablation"])[
            "feature_count"
        ])))
        or (
            artifact.get("selected_calibrator_method") == "IDENTITY"
        ) != isinstance(artifact.get("platt_calibrator"), Mapping)
        or (
            artifact.get("base_feature_ablation_selected_calibrator_method")
            == "IDENTITY"
        ) != isinstance(
            artifact.get("base_feature_ablation_platt_calibrator"), Mapping
        )
    ):
        raise ValueError("v22_untouched_test_cohort_geometry_invalid")
    base_probabilities = _predict(artifact["base_model"], cohort_rows, contract)
    probabilities = _calibrated_predict(
        artifact["platt_calibrator"], base_probabilities, contract,
    )
    ablation_probabilities = _calibrated_predict(
        artifact["base_feature_ablation_platt_calibrator"],
        _predict(
            artifact["base_feature_ablation_base_model"], cohort_rows, contract,
        ),
        contract,
    )
    labels = _labels(cohort_rows)
    market_probabilities = _market_probabilities(cohort_rows, contract)
    model_scores = _proper_scores(labels, probabilities)
    market_scores = _proper_scores(labels, market_probabilities)
    ablation_scores = _proper_scores(labels, ablation_probabilities)
    executable_records = []
    candidate_records = []
    rejected_records = []
    margin = float(artifact["selected_margin"])
    for row, probability in zip(cohort_rows, probabilities, strict=True):
        if row.get("execution_supported") is not True:
            continue
        record = _trade_record(row, float(probability))
        record["subgroups"] = _subgroup_values(row, float(probability), contract)
        record["edge"] = float(probability) - float(
            record["break_even_probability"]
        )
        executable_records.append(record)
        if record["edge"] >= margin:
            candidate_records.append(record)
        else:
            rejected_records.append(record)
    matched_v21_rows = [
        row for row in cohort_rows
        if row.get("matched_frozen_v21_eligible") is True
    ]
    candidate_metrics = _trade_metrics(
        candidate_records, complete_windows, contract,
    )
    control_metrics = _trade_metrics(
        executable_records, complete_windows, contract,
    )
    test_config = dict(contract["untouched_test"])
    minimum = int(test_config[
        "minimum_picks_non_btc" if cohort == "NON_BTC_TRANSFER"
        else "minimum_picks_btc"
    ])
    seed = int(test_config[
        "non_btc_random_seed" if cohort == "NON_BTC_TRANSFER"
        else "btc_random_seed"
    ])
    candidate_bootstrap = _cluster_bootstrap_report(
        candidate_records, seed=seed, contract=contract,
    )
    checks = {
        "fee_slippage_adjusted_pnl_strictly_positive": (
            candidate_metrics["fee_slippage_adjusted_pnl_cents_10_contracts"]
            > 0.0
        ),
        "close_cluster_bootstrap_20th_percentile_mean_pnl_strictly_positive": bool(
            candidate_bootstrap[
                "mean_pnl_cents_per_pick_20th_percentile"
            ] is not None
            and candidate_bootstrap[
                "mean_pnl_cents_per_pick_20th_percentile"
            ] > 0.0
        ),
        "wilson_95_lower_strictly_exceeds_average_break_even": bool(
            candidate_metrics["wilson_95_low"] is not None
            and candidate_metrics["average_fee_slippage_adjusted_break_even"]
            is not None
            and candidate_metrics["wilson_95_low"]
            > candidate_metrics["average_fee_slippage_adjusted_break_even"]
        ),
        "all_row_log_loss_strictly_beats_12m_market": (
            model_scores["log_loss"] < market_scores["log_loss"]
        ),
        "all_row_brier_strictly_beats_12m_market": (
            model_scores["brier_score"] < market_scores["brier_score"]
        ),
        "all_row_log_loss_strictly_beats_base_feature_ablation": (
            model_scores["log_loss"] < ablation_scores["log_loss"]
        ),
        "all_row_brier_strictly_beats_base_feature_ablation": (
            model_scores["brier_score"] < ablation_scores["brier_score"]
        ),
        "maximum_drawdown_per_pick_strictly_below_all_source_executable_control": bool(
            candidate_metrics["maximum_drawdown_cents_per_pick"] is not None
            and control_metrics["maximum_drawdown_cents_per_pick"] is not None
            and candidate_metrics["maximum_drawdown_cents_per_pick"]
            < control_metrics["maximum_drawdown_cents_per_pick"]
        ),
        "frozen_test_volume_and_side_minima": (
            candidate_metrics["picks"] >= minimum
            and candidate_metrics["yes_picks"]
            >= int(test_config["minimum_picks_each_side"])
            and candidate_metrics["no_picks"]
            >= int(test_config["minimum_picks_each_side"])
        ),
    }
    return {
        "cohort": cohort,
        "selected_model_id": str(artifact["selected_model_id"]),
        "selected_margin": margin,
        "complete_close_windows": complete_windows,
        "all_feature_complete_rows": len(cohort_rows),
        "row_level_executable_rows": len(executable_records),
        "nonexecutable_rows_without_trade_or_pnl": (
            len(cohort_rows) - len(executable_records)
        ),
        "model_probability_metrics": {
            **model_scores,
            "market_log_loss": market_scores["log_loss"],
            "market_brier_score": market_scores["brier_score"],
            "base_feature_ablation_log_loss": ablation_scores["log_loss"],
            "base_feature_ablation_brier_score": ablation_scores["brier_score"],
            "selected_calibrator_method": str(
                artifact["selected_calibrator_method"]
            ),
            "base_feature_ablation_selected_calibrator_method": str(
                artifact["base_feature_ablation_selected_calibrator_method"]
            ),
            "frozen_pretest_platt_intercept": (
                float(artifact["platt_calibrator"].intercept_[0])
                if artifact["selected_calibrator_method"]
                == "L2_REGULARIZED_PLATT_ON_LOGIT" else None
            ),
            "frozen_pretest_platt_slope": (
                float(artifact["platt_calibrator"].coef_[0, 0])
                if artifact["selected_calibrator_method"]
                == "L2_REGULARIZED_PLATT_ON_LOGIT" else None
            ),
        },
        "candidate": {
            "metrics": candidate_metrics,
            "close_cluster_bootstrap": candidate_bootstrap,
            "subgroups": _subgroup_report(
                candidate_records, complete_windows, contract,
            ),
        },
        "all_source_complete_12m_side_follow_control": {
            "metrics": control_metrics,
        },
        "matched_frozen_v21_feature_complete_diagnostic": {
            "metrics": _accuracy_only_metrics(
                matched_v21_rows, complete_windows, contract,
            ),
        },
        "rejected_trade_counterfactual": {
            "metrics": _trade_metrics(
                rejected_records, complete_windows, contract,
            ),
        },
        "gate_checks": checks,
        "gate_met": all(checks.values()),
        "base_feature_ablation_used_for_v22_gates": True,
        "model_refit_performed": False,
        "recalibration_performed": False,
        "margin_selection_performed": False,
    }


def evaluate_untouched_test(
    seal: Mapping[str, Any], survival_labels: Mapping[int, int],
    bundle: Mapping[str, Any], *, contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    frozen = load_contract()
    contract = dict(frozen if contract is None else contract)
    if contract != frozen:
        raise ValueError("v22_untouched_test_contract_override_forbidden")
    feature_seal.validate_seal(seal)
    test_rows = [
        dict(row) for row in seal["rows"] if row["partition"] == TEST_PARTITION
    ]
    expected_ids = {int(row["parent_id"]) for row in test_rows}
    normalized_labels = {}
    for raw_id, value in survival_labels.items():
        try:
            row_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("v22_untouched_test_label_identity_invalid") from exc
        if (
            row_id in normalized_labels
            or isinstance(value, bool)
            or value not in (0, 1)
        ):
            raise ValueError("v22_untouched_test_label_identity_invalid")
        normalized_labels[row_id] = int(value)
    supplied_ids = set(normalized_labels)
    if (
        supplied_ids != expected_ids
        or len(test_rows) != identity.UNTOUCHED_TEST_CLOSE_WINDOWS * 7
        or bundle.get("feature_seal_sha256") != seal["seal_sha256"]
        or bundle.get("evaluator_contract_sha256")
        != identity.EVALUATOR_CONTRACT_SHA256
        or bundle.get("protocol_sha256") != identity.PROTOCOL_SHA256
        or set(bundle.get("cohorts") or {}) != set(COHORTS)
    ):
        raise ValueError("v22_untouched_test_input_identity_invalid")
    labeled = []
    for row in test_rows:
        value = normalized_labels[int(row["parent_id"])]
        labeled.append({**row, "label_survives": int(value)})
    cohorts = {
        cohort: _score_untouched_cohort(
            labeled, cohort, bundle["cohorts"][cohort], contract,
        ) for cohort in COHORTS
    }
    passed = all(report["gate_met"] for report in cohorts.values())
    label_pairs = tuple(sorted(
        (int(row_id), int(value)) for row_id, value in normalized_labels.items()
    ))
    return {
        "modeling_version": MODELING_VERSION,
        "evaluator_contract_id": identity.EVALUATOR_CONTRACT_ID,
        "evaluator_contract_sha256": identity.EVALUATOR_CONTRACT_SHA256,
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "feature_seal_sha256": seal["seal_sha256"],
        "untouched_test_label_rows": len(label_pairs),
        "untouched_test_label_ids_sha256": _canonical_sha256(tuple(
            row_id for row_id, _label in label_pairs
        )),
        "cohorts": cohorts,
        "historical_gate_met": passed,
        "status": (
            "V22_HISTORICAL_GATES_PASSED_MANUAL_PAPER_CONSIDERATION_ONLY"
            if passed else "V22_UNTOUCHED_TEST_GATE_FAILED_NO_PAPER_CHALLENGER"
        ),
        "independent_final_historical_confirmation": True,
        "test_guided_refit_recalibration_or_margin_selection": False,
        "untouched_test_labels_read": True,
        "untouched_test_scoring_performed": True,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "telegram_allowed": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }
