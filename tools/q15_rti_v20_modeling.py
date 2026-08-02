"""Frozen V20 train/calibration modeling with no database or label access.

The caller must first create and validate the exclusive feature seal, reserve
the exact TRAIN/CALIBRATION identities, and obtain authoritative labels.  This
module accepts only those already-verified binary survival labels.  It has no
network, SQLite, Telegram, paper-ledger, promotion, or order capability.
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

from q15_upgrade.strategy_bots import rti_microstructure_v20 as v20
from q15_upgrade.strategy_bots import rti_microstructure_v20_audit_identity as audit_identity
from q15_upgrade.strategy_bots import rti_microstructure_v20_features as v20_features
from q15_upgrade.strategy_bots import rti_microstructure_v20_identity as identity
from q15_upgrade.strategy_bots.costs import (
    KALSHI_Q15_FEE_SCHEDULE_VERSION,
    RTI_EXECUTION_COST_MODEL_VERSION,
    rti_simulated_execution,
    rti_simulated_net_pnl_cents,
)
from tools import q15_rti_v20_feature_seal as feature_seal
from tools.q15_rti_microstructure_preregister import design_fingerprint


DEFAULT_CONTRACT = ROOT / audit_identity.EVALUATOR_CONTRACT_RELATIVE_PATH
COHORTS = ("NON_BTC_TRANSFER", "BTC")
TRAIN_PARTITION = "TRAIN"
CALIBRATION_PARTITION = "CALIBRATION"
TEST_PARTITION = "UNTOUCHED_TEST"


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _grid_values(specs: Sequence[Mapping[str, Any]], family: str) -> set[tuple]:
    output = set()
    for spec in specs:
        if spec.get("family") != family:
            continue
        if family == "ELASTIC_NET_LOGISTIC":
            output.add((float(spec["C"]), float(spec["l1_ratio"])))
        elif family == "RIDGE_LOGISTIC":
            output.add((float(spec["C"]),))
        elif family == "HIST_GRADIENT_BOOSTING":
            output.add((
                float(spec["learning_rate"]),
                int(spec["max_leaf_nodes"]),
                int(spec["min_samples_leaf"]),
                float(spec["l2_regularization"]),
            ))
    return output


def load_contract(path: Path = DEFAULT_CONTRACT) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("v20_evaluator_contract_unreadable") from exc
    if not isinstance(value, Mapping):
        raise ValueError("v20_evaluator_contract_root_not_object")
    contract = dict(value)
    v20.load_protocol()
    runtime = dict(contract.get("runtime") or {})
    partitions = dict(contract.get("partitions") or {})
    preprocessing = dict(contract.get("preprocessing") or {})
    walk = dict(contract.get("internal_walk_forward") or {})
    candidates = dict(contract.get("candidates") or {})
    estimator_parameters = dict(contract.get("estimator_parameters") or {})
    post = dict(contract.get("post_selection") or {})
    execution = dict(contract.get("selective_execution") or {})
    test_scoring = dict(contract.get("untouched_test_scoring") or {})
    subgroups = dict(contract.get("reporting_subgroups") or {})
    safety = dict(contract.get("safety") or {})
    protocol = v20.load_protocol()
    protocol_models = dict(protocol["model_families"])
    non_btc_protocol = {
        str(item["name"]): dict(item)
        for item in protocol_models["non_btc_candidates"]
    }
    btc_protocol = dict(protocol_models["btc_candidates"][0])
    non_btc_specs = list(candidates.get("NON_BTC_TRANSFER") or ())
    btc_specs = list(candidates.get("BTC") or ())
    expected_elastic = {
        (float(c), float(l1))
        for c in non_btc_protocol["ELASTIC_NET_LOGISTIC"]["C_grid"]
        for l1 in non_btc_protocol["ELASTIC_NET_LOGISTIC"]["l1_ratio_grid"]
    }
    hgb = non_btc_protocol["HIST_GRADIENT_BOOSTING"]
    expected_hgb = {
        (float(rate), int(leaves), int(min_leaf), float(l2))
        for rate in hgb["learning_rate_grid"]
        for leaves in hgb["max_leaf_nodes_grid"]
        for min_leaf in hgb["min_samples_leaf_grid"]
        for l2 in hgb["l2_regularization_grid"]
    }
    expected_btc = {(float(c),) for c in btc_protocol["C_grid"]}
    folds = list(walk.get("folds") or ())
    expected_folds = [
        ([0, 29], [30, 44]),
        ([0, 44], [45, 59]),
        ([0, 59], [60, 74]),
        ([0, 74], [75, 89]),
    ]
    actual_folds = [
        (
            list(item.get("train_window_indices_inclusive") or ()),
            list(item.get("validation_window_indices_inclusive") or ()),
        )
        for item in folds
    ]
    ranks = [
        int(spec.get("complexity_rank") or 0)
        for spec in non_btc_specs + btc_specs
    ]
    if (
        design_fingerprint(contract) != audit_identity.EVALUATOR_CONTRACT_SHA256
        or contract.get("contract_id") != audit_identity.EVALUATOR_CONTRACT_ID
        or contract.get("contract_status")
        != "FROZEN_BEFORE_ANY_V20_LABEL_ACCESS_OR_MODEL_FIT"
        or contract.get("protocol_id") != identity.PROTOCOL_ID
        or contract.get("protocol_sha256") != identity.PROTOCOL_SHA256
        or contract.get("feature_builder_version")
        != identity.FEATURE_BUILDER_VERSION
        or int(contract.get("feature_count") or 0) != identity.FEATURE_COUNT
        or contract.get("feature_names_sha256") != identity.FEATURE_NAMES_SHA256
        or runtime.get("scikit_learn_exact") != "1.9.0"
        or sklearn.__version__ != runtime.get("scikit_learn_exact")
        or runtime.get("single_process") is not True
        or int(runtime.get("estimator_threads") or 0) != 1
        or int(runtime.get("random_seed") or -1) != 1520
        or int(partitions.get("train_complete_close_windows") or 0)
        != identity.TRAIN_CLOSE_WINDOWS
        or int(partitions.get("calibration_complete_close_windows") or 0)
        != identity.CALIBRATION_CLOSE_WINDOWS
        or int(partitions.get("untouched_test_complete_close_windows") or 0)
        != identity.UNTOUCHED_TEST_CLOSE_WINDOWS
        or partitions.get("same_close_assets_never_cross_partitions") is not True
        or tuple(partitions.get("cohorts_evaluated_separately") or ()) != COHORTS
        or partitions.get("cross_cohort_model_or_gate_pooling_forbidden")
        is not True
        or preprocessing.get("fit_on_each_training_fold_only") is not True
        or preprocessing.get("center") != "COLUMN_MEDIAN"
        or preprocessing.get("scale")
        != "COLUMN_Q75_LINEAR_MINUS_Q25_LINEAR"
        or preprocessing.get("calibration_or_test_statistics_forbidden")
        is not True
        or actual_folds != expected_folds
        or walk.get("selection_metric")
        != "POOLED_VALIDATION_ROW_LOG_LOSS"
        or walk.get("first_tie_breaker")
        != "POOLED_VALIDATION_ROW_BRIER_SCORE"
        or walk.get("market_probability_feature")
        != "delayed_market_side_probability"
        or _grid_values(non_btc_specs, "ELASTIC_NET_LOGISTIC")
        != expected_elastic
        or _grid_values(non_btc_specs, "HIST_GRADIENT_BOOSTING")
        != expected_hgb
        or _grid_values(btc_specs, "RIDGE_LOGISTIC") != expected_btc
        or len(non_btc_specs) != 28
        or len(btc_specs) != 4
        or any(rank <= 0 for rank in ranks)
        or len({
            int(spec["complexity_rank"]) for spec in non_btc_specs
        }) != len(non_btc_specs)
        or len({
            int(spec["complexity_rank"]) for spec in btc_specs
        }) != len(btc_specs)
        or set(estimator_parameters)
        != {
            "ELASTIC_NET_LOGISTIC",
            "RIDGE_LOGISTIC",
            "HIST_GRADIENT_BOOSTING",
        }
        or post.get("base_model_refit_partition")
        != "ALL_90_TRAIN_WINDOWS_ONLY"
        or post.get("platt_fit_partition")
        != "ALL_30_CALIBRATION_WINDOWS_ONLY"
        or post.get("calibration_metrics_are_in_sample_and_not_independent_confirmation")
        is not True
        or int(execution.get("contracts") or 0) != 10
        or float(execution.get("slippage_cents_per_contract") or -1.0) != 2.0
        or execution.get("fee_schedule_version")
        != KALSHI_Q15_FEE_SCHEDULE_VERSION
        or execution.get("execution_cost_model_version")
        != RTI_EXECUTION_COST_MODEL_VERSION
        or list(execution.get("edge_margin_grid") or ())
        != [0.0, 0.02, 0.04, 0.06]
        or int(execution.get("bootstrap_resamples") or 0) != 5000
        or float(execution.get("bootstrap_quantile") or -1.0) != 0.2
        or execution.get("bootstrap_quantile_method") != "linear"
        or execution.get("bootstrap_cluster") != "CLOSE_TIME"
        or execution.get("bootstrap_population")
        != "UNIQUE_CLOSE_TIMES_WITH_AT_LEAST_ONE_PICK"
        or test_scoring.get("model_probability_metrics_population")
        != "ALL_SOURCE_COMPLETE_COHORT_ROWS"
        or test_scoring.get("selective_trade_population")
        != "CALIBRATED_EDGE_AT_OR_ABOVE_FROZEN_SELECTED_MARGIN"
        or test_scoring.get("market_probability_feature")
        != "delayed_market_side_probability"
        or int(test_scoring.get("cluster_bootstrap_resamples") or 0) != 5000
        or list(test_scoring.get("cluster_bootstrap_two_sided_quantiles") or ())
        != [0.025, 0.975]
        or float(test_scoring.get("wilson_z") or 0.0)
        != 1.959963984540054
        or len(test_scoring.get("required_gates") or ()) != 6
        or subgroups.get("report_only_never_used_for_selection_or_gates")
        is not True
        or subgroups.get("distance_feature") != "delayed_distance_bps"
        or subgroups.get("volatility_feature")
        != "log1p_spot_fast_volatility_60s_bps"
        or subgroups.get("settlement_average_risk_feature")
        != "parent_distance_to_remaining_volatility"
        or safety.get("feature_seal_required_before_labels") is not True
        or safety.get("model_fit_before_exclusive_label_reservation_forbidden")
        is not True
        or safety.get("untouched_test_labels_read_during_pretest_forbidden")
        is not True
        or safety.get("test_guided_refit_recalibration_margin_or_model_selection_forbidden")
        is not True
        or safety.get("automatic_promotion") is not False
        or safety.get("real_trading_allowed") is not False
    ):
        raise ValueError("v20_evaluator_contract_identity_or_safety_invalid")
    return contract


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
    raise ValueError("v20_model_family_invalid")


def _clip_probability(values: np.ndarray, contract: Mapping[str, Any]) -> np.ndarray:
    low, high = dict(contract["internal_walk_forward"])["probability_clip"]
    return np.clip(np.asarray(values, dtype=float), float(low), float(high))


def _fit_scaler(matrix: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if matrix.ndim != 2 or matrix.shape[1] != identity.FEATURE_COUNT:
        raise ValueError("v20_model_matrix_geometry_invalid")
    if not np.isfinite(matrix).all():
        raise ValueError("v20_model_matrix_nonfinite")
    center = np.median(matrix, axis=0)
    low = np.quantile(matrix, 0.25, axis=0, method="linear")
    high = np.quantile(matrix, 0.75, axis=0, method="linear")
    scale = high - low
    scale[~np.isfinite(scale) | (scale == 0.0)] = 1.0
    return center, scale


def _matrix(rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
    try:
        matrix = np.asarray([list(row["features"]) for row in rows], dtype=float)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("v20_model_feature_matrix_invalid") from exc
    if (
        matrix.ndim != 2
        or matrix.shape != (len(rows), identity.FEATURE_COUNT)
        or not np.isfinite(matrix).all()
    ):
        raise ValueError("v20_model_feature_matrix_invalid")
    return matrix


def _labels(rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
    values = []
    for row in rows:
        value = row.get("label_survives")
        if isinstance(value, bool) or value not in (0, 1):
            raise ValueError("v20_model_label_invalid")
        values.append(int(value))
    return np.asarray(values, dtype=int)


def _build_estimator(
    spec: Mapping[str, Any], contract: Mapping[str, Any],
) -> Any:
    family = str(spec["family"])
    parameters = dict(dict(contract["estimator_parameters"])[family])
    if family == "ELASTIC_NET_LOGISTIC":
        return LogisticRegression(
            C=float(spec["C"]),
            l1_ratio=float(spec["l1_ratio"]),
            **parameters,
        )
    if family == "RIDGE_LOGISTIC":
        return LogisticRegression(C=float(spec["C"]), **parameters)
    if family == "HIST_GRADIENT_BOOSTING":
        return HistGradientBoostingClassifier(
            learning_rate=float(spec["learning_rate"]),
            max_leaf_nodes=int(spec["max_leaf_nodes"]),
            min_samples_leaf=int(spec["min_samples_leaf"]),
            l2_regularization=float(spec["l2_regularization"]),
            **parameters,
        )
    raise ValueError("v20_model_family_invalid")


def _fit(
    spec: Mapping[str, Any], rows: Sequence[Mapping[str, Any]],
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    matrix = _matrix(rows)
    labels = _labels(rows)
    if len(set(labels.tolist())) != 2:
        raise ValueError("v20_model_training_single_class")
    center, scale = _fit_scaler(matrix)
    estimator = _build_estimator(spec, contract)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ConvergenceWarning)
        estimator.fit((matrix - center) / scale, labels)
    if any(issubclass(item.category, ConvergenceWarning) for item in caught):
        raise ValueError("v20_model_fit_did_not_converge")
    return {
        "spec": dict(spec),
        "model_id": _model_id(spec),
        "center": center,
        "scale": scale,
        "estimator": estimator,
    }


def _predict(
    fitted: Mapping[str, Any], rows: Sequence[Mapping[str, Any]],
    contract: Mapping[str, Any],
) -> np.ndarray:
    matrix = _matrix(rows)
    probabilities = fitted["estimator"].predict_proba(
        (matrix - fitted["center"]) / fitted["scale"]
    )[:, 1]
    return _clip_probability(probabilities, contract)


def _proper_scores(labels: np.ndarray, probabilities: np.ndarray) -> dict[str, float]:
    if len(labels) == 0 or len(labels) != len(probabilities):
        raise ValueError("v20_model_score_geometry_invalid")
    log_loss = -np.mean(
        labels * np.log(probabilities)
        + (1 - labels) * np.log(1.0 - probabilities)
    )
    brier = np.mean(np.square(probabilities - labels))
    return {"log_loss": float(log_loss), "brier_score": float(brier)}


def _rows_for_closes(
    rows: Sequence[Mapping[str, Any]], closes: set[float],
) -> list[dict[str, Any]]:
    return [dict(row) for row in rows if float(row["close_time"]) in closes]


def _candidate_walk_forward(
    rows: Sequence[Mapping[str, Any]],
    spec: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    train_closes = sorted({
        float(row["close_time"])
        for row in rows if row["partition"] == TRAIN_PARTITION
    })
    folds = list(dict(contract["internal_walk_forward"])["folds"])
    pooled_labels = []
    pooled_predictions = []
    pooled_market = []
    fold_reports = []
    market_index = v20_features.FEATURE_NAMES.index(
        dict(contract["internal_walk_forward"])["market_probability_feature"]
    )
    for fold_index, fold in enumerate(folds):
        train_bounds = fold["train_window_indices_inclusive"]
        validation_bounds = fold["validation_window_indices_inclusive"]
        train_set = set(train_closes[
            int(train_bounds[0]):int(train_bounds[1]) + 1
        ])
        validation_set = set(train_closes[
            int(validation_bounds[0]):int(validation_bounds[1]) + 1
        ])
        if (
            len(train_set) != int(train_bounds[1]) - int(train_bounds[0]) + 1
            or len(validation_set)
            != int(validation_bounds[1]) - int(validation_bounds[0]) + 1
            or train_set.intersection(validation_set)
        ):
            raise ValueError("v20_model_fold_chronology_invalid")
        fit_rows = _rows_for_closes(rows, train_set)
        validation_rows = _rows_for_closes(rows, validation_set)
        fitted = _fit(spec, fit_rows, contract)
        probabilities = _predict(fitted, validation_rows, contract)
        labels = _labels(validation_rows)
        market = _clip_probability(np.asarray([
            float(row["features"][market_index]) for row in validation_rows
        ]), contract)
        scores = _proper_scores(labels, probabilities)
        market_scores = _proper_scores(labels, market)
        pooled_labels.extend(labels.tolist())
        pooled_predictions.extend(probabilities.tolist())
        pooled_market.extend(market.tolist())
        fold_reports.append({
            "fold": fold_index + 1,
            "train_close_windows": len(train_set),
            "validation_close_windows": len(validation_set),
            "train_rows": len(fit_rows),
            "validation_rows": len(validation_rows),
            "train_first_close_time": min(train_set),
            "train_last_close_time": max(train_set),
            "validation_first_close_time": min(validation_set),
            "validation_last_close_time": max(validation_set),
            **scores,
            "market_log_loss": market_scores["log_loss"],
            "market_brier_score": market_scores["brier_score"],
            "scaler_center_sha256": _canonical_sha256(
                fitted["center"].tolist()
            ),
            "scaler_scale_sha256": _canonical_sha256(
                fitted["scale"].tolist()
            ),
        })
    labels_array = np.asarray(pooled_labels, dtype=int)
    predictions_array = np.asarray(pooled_predictions, dtype=float)
    market_array = np.asarray(pooled_market, dtype=float)
    scores = _proper_scores(labels_array, predictions_array)
    market_scores = _proper_scores(labels_array, market_array)
    return {
        "model_id": _model_id(spec),
        "spec": dict(spec),
        "complexity_rank": int(spec["complexity_rank"]),
        "status": "VALID",
        "pooled_validation_rows": len(labels_array),
        **scores,
        "market_log_loss": market_scores["log_loss"],
        "market_brier_score": market_scores["brier_score"],
        "folds": fold_reports,
    }


def _fit_platt(
    base_probabilities: np.ndarray,
    labels: np.ndarray,
    contract: Mapping[str, Any],
) -> LogisticRegression:
    if len(set(labels.tolist())) != 2:
        raise ValueError("v20_platt_calibration_single_class")
    probabilities = _clip_probability(base_probabilities, contract)
    logits = np.log(probabilities / (1.0 - probabilities)).reshape(-1, 1)
    post = dict(contract["post_selection"])
    calibrator = LogisticRegression(
        solver=str(post["platt_solver"]),
        penalty=post["platt_penalty"],
        max_iter=int(post["platt_max_iter"]),
        tol=float(post["platt_tol"]),
        random_state=int(post["platt_random_state"]),
        n_jobs=1,
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ConvergenceWarning)
        calibrator.fit(logits, labels)
    if any(issubclass(item.category, ConvergenceWarning) for item in caught):
        raise ValueError("v20_platt_fit_did_not_converge")
    return calibrator


def _platt_predict(
    calibrator: LogisticRegression,
    base_probabilities: np.ndarray,
    contract: Mapping[str, Any],
) -> np.ndarray:
    probabilities = _clip_probability(base_probabilities, contract)
    logits = np.log(probabilities / (1.0 - probabilities)).reshape(-1, 1)
    return _clip_probability(calibrator.predict_proba(logits)[:, 1], contract)


def _bootstrap_lower_mean_pnl(
    picks: Sequence[Mapping[str, Any]],
    *,
    resamples: int,
    quantile: float,
    seed: int,
) -> float:
    clusters: dict[float, list[float]] = defaultdict(list)
    for row in picks:
        clusters[float(row["close_time"])].append(float(row["pnl_cents_10"]))
    cluster_values = list(clusters.values())
    if not cluster_values:
        raise ValueError("v20_bootstrap_no_picks")
    rng = np.random.default_rng(seed)
    samples = np.empty(resamples, dtype=float)
    cluster_count = len(cluster_values)
    for index in range(resamples):
        selected = rng.integers(0, cluster_count, size=cluster_count)
        values = [
            pnl
            for cluster_index in selected
            for pnl in cluster_values[int(cluster_index)]
        ]
        samples[index] = float(np.mean(values))
    return float(np.quantile(samples, quantile, method="linear"))


def _margin_report(
    rows: Sequence[Mapping[str, Any]],
    probabilities: np.ndarray,
    margin: float,
    cohort: str,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    execution_contract = dict(contract["selective_execution"])
    contracts = int(execution_contract["contracts"])
    slippage = float(execution_contract["slippage_cents_per_contract"])
    picks = []
    for row, probability in zip(rows, probabilities, strict=True):
        execution = rti_simulated_execution(
            row["entry_ask_cents"], contracts, slippage,
        )
        if execution is None or row.get("sim_full_fill_supported") is not True:
            raise ValueError("v20_calibration_execution_evidence_invalid")
        break_even = float(execution["fee_slippage_breakeven_rate"])
        if float(probability) - break_even < margin:
            continue
        pnl_per_contract = rti_simulated_net_pnl_cents(
            row["entry_ask_cents"],
            bool(int(row["label_survives"])),
            contracts,
            slippage,
        )
        if pnl_per_contract is None:
            raise ValueError("v20_calibration_execution_evidence_invalid")
        picks.append({
            "parent_id": int(row["parent_id"]),
            "close_time": float(row["close_time"]),
            "asset": str(row["asset"]),
            "side": str(row["side"]),
            "probability": float(probability),
            "break_even_probability": break_even,
            "edge": float(probability) - break_even,
            "correct": int(row["label_survives"]),
            "pnl_cents_10": float(pnl_per_contract) * contracts,
        })
    yes_picks = sum(row["side"] == "YES" for row in picks)
    no_picks = sum(row["side"] == "NO" for row in picks)
    minimum = int(execution_contract[
        "non_btc_minimum_picks"
        if cohort == "NON_BTC_TRANSFER"
        else "btc_minimum_picks"
    ])
    count_gate = (
        len(picks) >= minimum
        and yes_picks >= int(execution_contract["minimum_yes_picks"])
        and no_picks >= int(execution_contract["minimum_no_picks"])
    )
    lower = None
    if picks:
        lower = _bootstrap_lower_mean_pnl(
            picks,
            resamples=int(execution_contract["bootstrap_resamples"]),
            quantile=float(execution_contract["bootstrap_quantile"]),
            seed=int(execution_contract[
                "non_btc_random_seed"
                if cohort == "NON_BTC_TRANSFER"
                else "btc_random_seed"
            ]) + int(round(margin * 100)),
        )
    observed_mean = (
        float(np.mean([row["pnl_cents_10"] for row in picks]))
        if picks else None
    )
    return {
        "margin": float(margin),
        "picks": len(picks),
        "yes_picks": yes_picks,
        "no_picks": no_picks,
        "trade_frequency_per_complete_window": len(picks)
        / identity.CALIBRATION_CLOSE_WINDOWS,
        "accuracy": (
            sum(row["correct"] for row in picks) / len(picks)
            if picks else None
        ),
        "fee_slippage_adjusted_pnl_cents_10_contracts": (
            sum(row["pnl_cents_10"] for row in picks) if picks else 0.0
        ),
        "observed_mean_pnl_cents_per_10_contract_pick": observed_mean,
        "bootstrap_20th_percentile_mean_pnl_cents_per_10_contract_pick": lower,
        "volume_and_side_gate_met": count_gate,
        "positive_bootstrap_lower_gate_met": bool(
            lower is not None and lower > 0.0
        ),
        "gate_met": bool(count_gate and lower is not None and lower > 0.0),
        "pick_parent_ids_sha256": _canonical_sha256(tuple(sorted(
            row["parent_id"] for row in picks
        ))),
    }


def _labeled_pretest_rows(
    seal: Mapping[str, Any], labels: Mapping[int, int],
) -> list[dict[str, Any]]:
    feature_seal.validate_seal(seal)
    rows = [
        dict(row) for row in seal["rows"]
        if row["partition"] in {TRAIN_PARTITION, CALIBRATION_PARTITION}
    ]
    expected_ids = {int(row["parent_id"]) for row in rows}
    provided_ids = set()
    normalized = {}
    for key, value in labels.items():
        try:
            row_id = int(key)
        except (TypeError, ValueError) as exc:
            raise ValueError("v20_pretest_label_identity_invalid") from exc
        if isinstance(value, bool) or value not in (0, 1):
            raise ValueError("v20_pretest_label_invalid")
        provided_ids.add(row_id)
        normalized[row_id] = int(value)
    if provided_ids != expected_ids or len(rows) != 120 * 7:
        raise ValueError("v20_pretest_label_identity_invalid")
    return [{**row, "label_survives": normalized[int(row["parent_id"])]} for row in rows]


def evaluate_cohort(
    rows: Sequence[Mapping[str, Any]],
    cohort: str,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    if cohort not in COHORTS:
        raise ValueError("v20_model_cohort_invalid")
    cohort_rows = [dict(row) for row in rows if row["cohort"] == cohort]
    train_rows = [row for row in cohort_rows if row["partition"] == TRAIN_PARTITION]
    calibration_rows = [
        row for row in cohort_rows if row["partition"] == CALIBRATION_PARTITION
    ]
    expected_per_window = 6 if cohort == "NON_BTC_TRANSFER" else 1
    if (
        len(train_rows) != identity.TRAIN_CLOSE_WINDOWS * expected_per_window
        or len(calibration_rows)
        != identity.CALIBRATION_CLOSE_WINDOWS * expected_per_window
        or len({float(row["close_time"]) for row in train_rows})
        != identity.TRAIN_CLOSE_WINDOWS
        or len({float(row["close_time"]) for row in calibration_rows})
        != identity.CALIBRATION_CLOSE_WINDOWS
    ):
        raise ValueError("v20_model_cohort_geometry_invalid")
    candidate_reports = []
    for spec in dict(contract["candidates"])[cohort]:
        try:
            candidate_reports.append(
                _candidate_walk_forward(cohort_rows, spec, contract)
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
                "calibration_gate_met": False,
                "pretest_gate_met": False,
            },
            "artifact": None,
        }
    selected = min(valid, key=lambda item: (
        float(item["log_loss"]),
        float(item["brier_score"]),
        int(item["complexity_rank"]),
        str(item["model_id"]),
    ))
    internal_gate = float(selected["log_loss"]) < float(selected["market_log_loss"])
    if not internal_gate:
        return {
            "report": {
                "cohort": cohort,
                "status": "INTERNAL_WALK_FORWARD_MARKET_GATE_FAILED",
                "candidate_reports": candidate_reports,
                "selected_model_id": selected["model_id"],
                "selected_model": selected,
                "internal_walk_forward_gate_met": False,
                "calibration_gate_met": False,
                "pretest_gate_met": False,
            },
            "artifact": None,
        }
    selected_spec = dict(selected["spec"])
    fitted = _fit(selected_spec, train_rows, contract)
    base_calibration_probabilities = _predict(fitted, calibration_rows, contract)
    calibration_labels = _labels(calibration_rows)
    try:
        calibrator = _fit_platt(
            base_calibration_probabilities, calibration_labels, contract,
        )
    except ValueError as exc:
        return {
            "report": {
                "cohort": cohort,
                "status": "PLATT_CALIBRATION_FAILED",
                "failure": str(exc),
                "candidate_reports": candidate_reports,
                "selected_model_id": selected["model_id"],
                "selected_model": selected,
                "internal_walk_forward_gate_met": True,
                "calibration_gate_met": False,
                "pretest_gate_met": False,
            },
            "artifact": None,
        }
    calibrated_probabilities = _platt_predict(
        calibrator, base_calibration_probabilities, contract,
    )
    base_scores = _proper_scores(
        calibration_labels, base_calibration_probabilities,
    )
    calibrated_scores = _proper_scores(
        calibration_labels, calibrated_probabilities,
    )
    margin_reports = [
        _margin_report(
            calibration_rows,
            calibrated_probabilities,
            float(margin),
            cohort,
            contract,
        )
        for margin in dict(contract["selective_execution"])["edge_margin_grid"]
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
    calibration_gate = selected_margin is not None
    return {
        "report": {
            "cohort": cohort,
            "status": (
                "PRETEST_GATES_PASSED_UNTOUCHED_TEST_STILL_SEALED"
                if calibration_gate
                else "CALIBRATION_SELECTIVE_POLICY_GATE_FAILED"
            ),
            "candidate_reports": candidate_reports,
            "selected_model_id": selected["model_id"],
            "selected_model": selected,
            "internal_walk_forward_gate_met": True,
            "train_rows": len(train_rows),
            "train_complete_close_windows": identity.TRAIN_CLOSE_WINDOWS,
            "calibration_rows": len(calibration_rows),
            "calibration_complete_close_windows": identity.CALIBRATION_CLOSE_WINDOWS,
            "calibration_base_scores": base_scores,
            "calibration_in_sample_platt_scores": calibrated_scores,
            "calibration_in_sample_not_independent_confirmation": True,
            "platt_intercept": float(calibrator.intercept_[0]),
            "platt_slope": float(calibrator.coef_[0, 0]),
            "margin_reports": margin_reports,
            "selected_margin": selected_margin,
            "calibration_gate_met": calibration_gate,
            "pretest_gate_met": calibration_gate,
        },
        "artifact": (
            {
                "cohort": cohort,
                "selected_spec": selected_spec,
                "selected_model_id": selected["model_id"],
                "base_model": fitted,
                "platt_calibrator": calibrator,
                "selected_margin": float(selected_margin["margin"]),
            }
            if calibration_gate else None
        ),
    }


def evaluate_pretest(
    seal: Mapping[str, Any],
    train_calibration_labels: Mapping[int, int],
    *,
    contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fit frozen V20 candidates after an external one-shot label reservation."""
    contract = dict(contract or load_contract())
    if contract != load_contract():
        raise ValueError("v20_evaluator_contract_override_forbidden")
    rows = _labeled_pretest_rows(seal, train_calibration_labels)
    cohort_results = {
        cohort: evaluate_cohort(rows, cohort, contract) for cohort in COHORTS
    }
    reports = {
        cohort: cohort_results[cohort]["report"] for cohort in COHORTS
    }
    artifacts = {
        cohort: cohort_results[cohort]["artifact"] for cohort in COHORTS
    }
    gate_met = all(
        reports[cohort].get("pretest_gate_met") is True for cohort in COHORTS
    )
    label_pairs = tuple(sorted(
        (int(row_id), int(value))
        for row_id, value in train_calibration_labels.items()
    ))
    report = {
        "modeling_version": audit_identity.MODELING_VERSION,
        "evaluator_contract_id": audit_identity.EVALUATOR_CONTRACT_ID,
        "evaluator_contract_sha256": audit_identity.EVALUATOR_CONTRACT_SHA256,
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "feature_seal_sha256": seal["seal_sha256"],
        "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
        "feature_names_sha256": identity.FEATURE_NAMES_SHA256,
        "train_calibration_label_rows": len(label_pairs),
        "train_calibration_label_ids_sha256": _canonical_sha256(tuple(
            row_id for row_id, _label in label_pairs
        )),
        "train_calibration_labels_sha256": _canonical_sha256(label_pairs),
        "cohorts": reports,
        "pretest_gate_met": gate_met,
        "status": (
            "V20_PRETEST_GATES_PASSED_UNTOUCHED_TEST_MAY_BE_MANUALLY_OPENED_ONCE"
            if gate_met
            else "V20_PRETEST_GATE_FAILED_UNTOUCHED_TEST_REMAINS_SEALED"
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
