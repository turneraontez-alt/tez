"""Freeze the numerically guarded RTI market-prior probability challenger.

Architecture and regularization are fixed in code.  There is no hyperparameter
search.  All same-close assets share a chronological fold; BTC and non-BTC are
fit independently.  Train is used for weights, calibration only for Platt
calibration, and the last historical fold is scored exactly once.  The emitted
artifact refuses to score any close at or before the latest inspected row.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots.costs import (
    KALSHI_Q15_FEE_SCHEDULE_VERSION,
    RTI_EXECUTION_COST_MODEL_VERSION,
    rti_simulated_execution,
)
from q15_upgrade.strategy_bots.rti_probability import (
    FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION,
    MODEL_FAMILY,
    artifact_fingerprint,
    feature_vector,
)
from tools.q15_rti_forward_drift_audit import _valid_exact_row


TRAIN_FRACTION = 0.60
CALIBRATION_FRACTION = 0.20
MODEL_L2 = 20.0
MODEL_LEARNING_RATE = 0.05
MODEL_ITERATIONS = 1500
CALIBRATION_L2 = 20.0
CALIBRATION_LEARNING_RATE = 0.05
CALIBRATION_ITERATIONS = 1000
STANDARDIZATION_MIN_STD = 1e-8
STANDARDIZATION_Z_CLIP = 6.0
STANDARDIZATION_MAX_ABS_Z = 8.0
CALIBRATION_MIN_SLOPE = 0.25
CALIBRATION_MAX_SLOPE = 4.0
CALIBRATION_MAX_ABS_INTERCEPT = 4.0
MIN_EXPECTED_VALUE_CENTS = 3.0
MAX_ASK_CENTS = 62.0
MAX_SPREAD_CENTS = 1.5
MIN_DEPTH_CONTRACTS = 10.0
SIM_CONTRACTS = 10
SLIPPAGE_CENTS = 2.0


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _logit(probability: float) -> float:
    p = _clip(probability, 1e-6, 1.0 - 1e-6)
    return math.log(p / (1.0 - p))


def _sigmoid_array(values: np.ndarray) -> np.ndarray:
    clipped = np.clip(values, -709.0, 709.0)
    return 1.0 / (1.0 + np.exp(-clipped))


def chronological_folds(close_times: Sequence[float]) -> dict[str, set[float]]:
    """Split unique closes once so cross-asset rows can never cross folds."""
    windows = sorted({float(value) for value in close_times})
    if len(windows) < 15:
        raise ValueError("at_least_15_unique_close_windows_required")
    train_end = max(1, min(len(windows) - 2, int(len(windows) * TRAIN_FRACTION)))
    calibration_end = max(
        train_end + 1,
        min(
            len(windows) - 1,
            int(len(windows) * (TRAIN_FRACTION + CALIBRATION_FRACTION)),
        ),
    )
    return {
        "train": set(windows[:train_end]),
        "calibration": set(windows[train_end:calibration_end]),
        "test": set(windows[calibration_end:]),
    }


def _window_weights(examples: Sequence[Mapping[str, Any]]) -> np.ndarray:
    counts = Counter(float(row["close_time"]) for row in examples)
    return np.asarray(
        [1.0 / counts[float(row["close_time"])] for row in examples],
        dtype=np.float64,
    )


def _matrix(examples: Sequence[Mapping[str, Any]]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    return (
        np.asarray([row["features"] for row in examples], dtype=np.float64),
        np.asarray([row["label_yes"] for row in examples], dtype=np.float64),
        np.asarray([row["market_yes_probability"] for row in examples], dtype=np.float64),
    )


def _fit_residual_logit(examples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    matrix, labels, market = _matrix(examples)
    sample_weight = _window_weights(examples)
    weight_total = float(sample_weight.sum()) or 1.0
    means = np.average(matrix, axis=0, weights=sample_weight)
    variances = np.average((matrix - means) ** 2, axis=0, weights=sample_weight)
    stds = np.sqrt(variances)
    stds = np.where(stds > STANDARDIZATION_MIN_STD, stds, 0.0)
    safe_stds = np.where(stds > STANDARDIZATION_MIN_STD, stds, 1.0)
    standardized = (matrix - means) / safe_stds
    standardized[:, stds <= STANDARDIZATION_MIN_STD] = 0.0
    standardized = np.clip(
        standardized, -STANDARDIZATION_Z_CLIP, STANDARDIZATION_Z_CLIP
    )
    weights = np.zeros(matrix.shape[1], dtype=np.float64)
    bias = 0.0
    offsets = np.asarray([_logit(value) for value in market], dtype=np.float64)
    for _ in range(MODEL_ITERATIONS):
        predictions = _sigmoid_array(offsets + bias + standardized @ weights)
        errors = (predictions - labels) * sample_weight
        gradient = (standardized.T @ errors + MODEL_L2 * weights) / weight_total
        bias_gradient = (float(errors.sum()) + (MODEL_L2 / 4.0) * bias) / weight_total
        weights -= MODEL_LEARNING_RATE * gradient
        bias -= MODEL_LEARNING_RATE * bias_gradient
    return {
        "means": means.tolist(),
        "stds": stds.tolist(),
        "weights": weights.tolist(),
        "bias": float(bias),
        "inactive_near_zero_variance_features": [
            FEATURE_NAMES[index]
            for index, std in enumerate(stds)
            if std <= STANDARDIZATION_MIN_STD
        ],
    }


def _raw_probabilities(model: Mapping[str, Any], examples: Sequence[Mapping[str, Any]]) -> list[float]:
    means = np.asarray(model["means"], dtype=np.float64)
    stds = np.asarray(model["stds"], dtype=np.float64)
    weights = np.asarray(model["weights"], dtype=np.float64)
    matrix, _labels, market = _matrix(examples)
    safe_stds = np.where(stds > STANDARDIZATION_MIN_STD, stds, 1.0)
    standardized = (matrix - means) / safe_stds
    standardized[:, stds <= STANDARDIZATION_MIN_STD] = 0.0
    standardized = np.clip(
        standardized, -STANDARDIZATION_Z_CLIP, STANDARDIZATION_Z_CLIP
    )
    offsets = np.asarray([_logit(value) for value in market], dtype=np.float64)
    return _sigmoid_array(offsets + float(model["bias"]) + standardized @ weights).tolist()


def _fit_platt(raw: Sequence[float], examples: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    if len(raw) < 10:
        return {"a": 1.0, "b": 0.0, "fitted": False}
    logits = np.asarray([_logit(value) for value in raw], dtype=np.float64)
    labels = np.asarray([row["label_yes"] for row in examples], dtype=np.float64)
    sample_weight = _window_weights(examples)
    weight_total = float(sample_weight.sum()) or 1.0
    a, b = 1.0, 0.0
    for _ in range(CALIBRATION_ITERATIONS):
        predictions = _sigmoid_array(a * logits + b)
        errors = (predictions - labels) * sample_weight
        gradient_a = (
            float(errors @ logits) + CALIBRATION_L2 * (a - 1.0)
        ) / weight_total
        gradient_b = (
            float(errors.sum()) + (CALIBRATION_L2 / 4.0) * b
        ) / weight_total
        a = _clip(
            a - CALIBRATION_LEARNING_RATE * gradient_a,
            CALIBRATION_MIN_SLOPE,
            CALIBRATION_MAX_SLOPE,
        )
        b = _clip(
            b - CALIBRATION_LEARNING_RATE * gradient_b,
            -CALIBRATION_MAX_ABS_INTERCEPT,
            CALIBRATION_MAX_ABS_INTERCEPT,
        )
    return {
        "a": float(a),
        "b": float(b),
        "fitted": True,
        "monotone_slope_constrained": True,
    }


def _calibrated(raw: Sequence[float], calibration: Mapping[str, Any]) -> list[float]:
    return [
        _clip(
            1.0
            / (
                1.0
                + math.exp(
                    -_clip(
                        float(calibration.get("a", 1.0)) * _logit(value)
                        + float(calibration.get("b", 0.0)),
                        -709.0,
                        709.0,
                    )
                )
            ),
            0.01,
            0.99,
        )
        for value in raw
    ]


def _fee_per_contract(ask: float, slippage: float = 0.0) -> float:
    execution = rti_simulated_execution(ask, SIM_CONTRACTS, slippage)
    return (
        0.0
        if execution is None
        else float(execution["fee_cents_per_contract"])
    )


def _entry(prob_yes: float, row: Mapping[str, Any]) -> dict[str, Any]:
    candidates = []
    for side, probability, ask_key, depth_key in (
        ("YES", prob_yes, "yes_ask_cents", "yes_depth_contracts"),
        ("NO", 1.0 - prob_yes, "no_ask_cents", "no_depth_contracts"),
    ):
        ask = float(row[ask_key])
        depth = float(row[depth_key])
        execution = rti_simulated_execution(
            ask,
            SIM_CONTRACTS,
            SLIPPAGE_CENTS,
        )
        if execution is None:
            continue
        fee = float(execution["fee_cents_per_contract"])
        fill = float(execution["simulated_fill_cents"])
        expected_value = probability * 100.0 - fill - fee
        candidates.append({
            "side": side,
            "probability": probability,
            "ask": ask,
            "simulated_fill_cents": fill,
            "depth": depth,
            "fee": fee,
            "expected_value": expected_value,
            "depth_available": bool(row[f"{side.lower()}_depth_available"]),
            "execution_cost_model_version": execution[
                "execution_cost_model_version"
            ],
            "fee_schedule_version": execution["fee_schedule_version"],
        })
    selected = max(candidates, key=lambda item: item["expected_value"])
    accepted = (
        selected["expected_value"] >= MIN_EXPECTED_VALUE_CENTS
        and selected["ask"] <= MAX_ASK_CENTS
        and float(row["spread_cents"]) <= MAX_SPREAD_CENTS
        and selected["depth_available"]
        and selected["depth"] >= MIN_DEPTH_CONTRACTS
    )
    return {**selected, "accepted": accepted}


def _wilson(correct: int, count: int) -> tuple[float | None, float | None]:
    if count <= 0:
        return None, None
    z = 1.96
    p = correct / count
    denominator = 1.0 + z * z / count
    center = (p + z * z / (2.0 * count)) / denominator
    half = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * count)) / count) / denominator
    return max(0.0, center - half), min(1.0, center + half)


def _metrics(examples: Sequence[Mapping[str, Any]], probs: Sequence[float]) -> dict[str, Any]:
    if not examples:
        return {"rows": 0, "picks": 0}
    labels = [int(row["label_yes"]) for row in examples]
    clipped = [_clip(value, 1e-6, 1.0 - 1e-6) for value in probs]
    market = [_clip(float(row["market_yes_probability"]), 1e-6, 1.0 - 1e-6) for row in examples]
    picks = []
    for row, probability in zip(examples, clipped):
        entry = _entry(probability, row)
        if not entry["accepted"]:
            continue
        correct = entry["side"] == ("YES" if row["label_yes"] else "NO")
        gross = 100.0 - entry["ask"] if correct else -entry["ask"]
        pnl = gross - entry["fee"] - SLIPPAGE_CENTS
        picks.append({**entry, "correct": correct, "pnl": pnl, "close_time": row["close_time"]})
    cumulative = peak = max_drawdown = 0.0
    for pick in sorted(picks, key=lambda item: float(item["close_time"])):
        cumulative += float(pick["pnl"])
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    pick_correct = sum(int(pick["correct"]) for pick in picks)
    wilson_low, wilson_high = _wilson(pick_correct, len(picks))
    return {
        "rows": len(examples),
        "close_windows": len({float(row["close_time"]) for row in examples}),
        "yes_rate": sum(labels) / len(labels),
        "accuracy": sum(int((probability >= 0.5) == bool(label)) for probability, label in zip(clipped, labels)) / len(labels),
        "brier": sum((probability - label) ** 2 for probability, label in zip(clipped, labels)) / len(labels),
        "log_loss": -sum(label * math.log(probability) + (1 - label) * math.log(1 - probability) for probability, label in zip(clipped, labels)) / len(labels),
        "market_brier": sum((probability - label) ** 2 for probability, label in zip(market, labels)) / len(labels),
        "market_log_loss": -sum(label * math.log(probability) + (1 - label) * math.log(1 - probability) for probability, label in zip(market, labels)) / len(labels),
        "picks": len(picks),
        "pick_frequency_per_window": len(picks) / max(1, len({float(row["close_time"]) for row in examples})),
        "pick_correct": pick_correct,
        "pick_accuracy": None if not picks else pick_correct / len(picks),
        "pick_wilson_95_low": wilson_low,
        "pick_wilson_95_high": wilson_high,
        "ten_contract_net_pnl_dollars": 10.0 * sum(float(pick["pnl"]) for pick in picks) / 100.0,
        "ev_cents_per_contract": None if not picks else sum(float(pick["pnl"]) for pick in picks) / len(picks),
        "max_drawdown_cents_per_contract": max_drawdown,
    }


def load_examples(strategy_db: str, max_close_time: float | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    conn = sqlite3.connect(f"file:{Path(strategy_db).resolve().as_posix()}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    rejected: Counter[str] = Counter()
    examples: list[dict[str, Any]] = []
    try:
        raw_rows = conn.execute(
            "SELECT * FROM strategy_bot_decisions WHERE bot_name='rti_path_13m' "
            "AND source_system='rti_path_13m' AND interval='13M' "
            "AND official_result IN ('YES','NO') ORDER BY close_time,id"
        ).fetchall()
        for source in raw_rows:
            source_dict = dict(source)
            valid, reason = _valid_exact_row(source_dict)
            if valid is None:
                rejected[str(reason or "invalid_exact_row")] += 1
                continue
            if max_close_time is not None and float(valid["close_time"]) > max_close_time:
                rejected["after_requested_freeze"] += 1
                continue
            vector = feature_vector(source_dict)
            if not vector.get("available"):
                rejected[str(vector.get("error") or "feature_unavailable")] += 1
                continue
            examples.append({
                "id": int(source_dict["id"]),
                "close_time": float(valid["close_time"]),
                "asset": str(vector["asset"]),
                "cohort": str(vector["cohort"]),
                "features": list(vector["features"]),
                "market_yes_probability": float(vector["market_yes_probability"]),
                "yes_ask_cents": float(vector["yes_ask_cents"]),
                "no_ask_cents": float(vector["no_ask_cents"]),
                "yes_depth_contracts": float(vector["yes_depth_contracts"]),
                "no_depth_contracts": float(vector["no_depth_contracts"]),
                "yes_depth_available": bool(vector["yes_depth_available"]),
                "no_depth_available": bool(vector["no_depth_available"]),
                "spread_cents": float(valid["spread"]),
                "label_yes": int(str(source_dict["official_result"]).upper() == "YES"),
            })
    finally:
        conn.close()
    return examples, {
        "source_rows": len(raw_rows),
        "usable_examples": len(examples),
        "rejected": sum(rejected.values()),
        "rejection_reasons": dict(rejected.most_common()),
    }


def inspection_boundary(strategy_db: str) -> float | None:
    connection = sqlite3.connect(
        f"file:{Path(strategy_db).resolve().as_posix()}?mode=ro",
        uri=True,
        timeout=30.0,
    )
    try:
        row = connection.execute(
            "SELECT MAX(close_time) FROM strategy_bot_decisions "
            "WHERE bot_name='rti_path_13m' AND source_system='rti_path_13m' "
            "AND interval='13M'"
        ).fetchone()
        return None if row is None else _num(row[0])
    finally:
        connection.close()


def freeze_artifact(
    examples: Sequence[Mapping[str, Any]],
    *,
    prospective_after_close_time: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    folds = chronological_folds([float(row["close_time"]) for row in examples])
    fold_by_close = {
        close_time: fold for fold, close_times in folds.items() for close_time in close_times
    }
    data_hash = hashlib.sha256(
        json.dumps(
            [
                [int(row["id"]), float(row["close_time"]), row["asset"], int(row["label_yes"]), row["features"]]
                for row in examples
            ],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    cohorts: dict[str, Any] = {}
    report_cohorts: dict[str, Any] = {}
    for cohort_name in ("BTC", "NON_BTC_TRANSFER"):
        cohort_examples = [row for row in examples if row["cohort"] == cohort_name]
        split = {
            fold: [row for row in cohort_examples if float(row["close_time"]) in folds[fold]]
            for fold in ("train", "calibration", "test")
        }
        if min(len(split["train"]), len(split["calibration"]), len(split["test"])) < 10:
            raise ValueError(f"insufficient_chronological_fold:{cohort_name}")
        model = _fit_residual_logit(split["train"])
        calibration_raw = _raw_probabilities(model, split["calibration"])
        calibration = _fit_platt(calibration_raw, split["calibration"])
        cohorts[cohort_name] = {
            **model,
            "calibration": calibration,
            "train_rows": len(split["train"]),
            "calibration_rows": len(split["calibration"]),
            "historical_test_rows": len(split["test"]),
        }
        fold_metrics: dict[str, Any] = {}
        for fold, fold_examples in split.items():
            raw = _raw_probabilities(model, fold_examples)
            probabilities = _calibrated(raw, calibration)
            fold_metrics[fold] = _metrics(fold_examples, probabilities)
        report_cohorts[cohort_name] = fold_metrics

    all_windows = sorted(fold_by_close)
    split_boundaries = {
        fold: {
            "first_close_time": min(folds[fold]),
            "last_close_time": max(folds[fold]),
            "close_windows": len(folds[fold]),
        }
        for fold in ("train", "calibration", "test")
    }
    prospective_boundary = max(
        max(all_windows),
        float(prospective_after_close_time or max(all_windows)),
    )
    artifact: dict[str, Any] = {
        "model_version": f"rti-probability-shadow-v3-{data_hash[:12]}",
        "model_family": MODEL_FAMILY,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "data_sha256": data_hash,
        "cohort_mixing_forbidden": True,
        "same_close_fold_isolation": True,
        "hyperparameter_search_performed": False,
        "historical_credit_allowed": False,
        "automatic_refit": False,
        "automatic_promotion": False,
        "paper_only": True,
        "prospective_after_close_time": prospective_boundary,
        "prospective_boundary_includes_unresolved_inspected_rows": True,
        "standardization_policy": {
            "min_std": STANDARDIZATION_MIN_STD,
            "z_clip": STANDARDIZATION_Z_CLIP,
            "max_abs_z_allowed": STANDARDIZATION_MAX_ABS_Z,
            "out_of_distribution_fails_entry": True,
        },
        "calibration_policy": {
            "monotone_slope_required": True,
            "min_slope": CALIBRATION_MIN_SLOPE,
            "max_slope": CALIBRATION_MAX_SLOPE,
            "max_abs_intercept": CALIBRATION_MAX_ABS_INTERCEPT,
        },
        "split_boundaries": split_boundaries,
        "fixed_training_config": {
            "train_fraction": TRAIN_FRACTION,
            "calibration_fraction": CALIBRATION_FRACTION,
            "model_l2": MODEL_L2,
            "model_learning_rate": MODEL_LEARNING_RATE,
            "model_iterations": MODEL_ITERATIONS,
            "calibration_l2": CALIBRATION_L2,
            "calibration_learning_rate": CALIBRATION_LEARNING_RATE,
            "calibration_iterations": CALIBRATION_ITERATIONS,
            "standardization_min_std": STANDARDIZATION_MIN_STD,
            "standardization_z_clip": STANDARDIZATION_Z_CLIP,
            "standardization_max_abs_z": STANDARDIZATION_MAX_ABS_Z,
            "calibration_min_slope": CALIBRATION_MIN_SLOPE,
            "calibration_max_slope": CALIBRATION_MAX_SLOPE,
        },
        "entry_policy": {
            "min_expected_value_cents_after_fee_slippage": MIN_EXPECTED_VALUE_CENTS,
            "max_ask_cents": MAX_ASK_CENTS,
            "max_spread_cents": MAX_SPREAD_CENTS,
            "min_depth_contracts": MIN_DEPTH_CONTRACTS,
            "sim_contracts": SIM_CONTRACTS,
            "slippage_cents_per_contract": SLIPPAGE_CENTS,
            "fee_schedule_version": KALSHI_Q15_FEE_SCHEDULE_VERSION,
            "execution_cost_model_version": RTI_EXECUTION_COST_MODEL_VERSION,
            "manual_review_bars": [30, 60, 150],
        },
        "cohorts": cohorts,
    }
    artifact["artifact_sha256"] = artifact_fingerprint(artifact)
    report = {
        "audit_version": "q15-rti-probability-freeze-v3",
        "model_version": artifact["model_version"],
        "data_sha256": data_hash,
        "artifact_sha256": artifact["artifact_sha256"],
        "prospective_after_close_time": artifact["prospective_after_close_time"],
        "same_close_fold_isolation": True,
        "cohort_mixing_forbidden": True,
        "hyperparameter_search_performed": False,
        "last_fold_scored_once": True,
        "last_fold_reusable_for_tuning": False,
        "historical_results_can_promote": False,
        "selected_after_v2_pre_outcome_numerical_ood_review": True,
        "prospective_boundary_includes_unresolved_inspected_rows": True,
        "cohorts": report_cohorts,
        "split_boundaries": split_boundaries,
    }
    return artifact, report


def render_markdown(report: Mapping[str, Any], integrity: Mapping[str, Any]) -> str:
    lines = [
        "# Q15 RTI Probability Challenger Freeze",
        "",
        "Mode: **PAPER shadow; silent; no historical promotion**",
        "",
        "| Cohort | Fold | Rows | Brier | Market Brier | Picks | Accuracy | 10-lot net |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for cohort, folds in report["cohorts"].items():
        for fold in ("train", "calibration", "test"):
            metrics = folds[fold]
            accuracy = metrics.get("pick_accuracy")
            lines.append(
                f"| {cohort} | {fold} | {metrics['rows']} | {metrics['brier']:.4f} | "
                f"{metrics['market_brier']:.4f} | {metrics['picks']} | "
                f"{'n/a' if accuracy is None else f'{100.0 * accuracy:.1f}%'} | "
                f"${metrics['ten_contract_net_pnl_dollars']:.2f} |"
            )
    lines.extend([
        "",
        "## Guardrails",
        "",
        f"- Usable exact examples: {integrity['usable_examples']}.",
        "- Every asset with the same close was assigned to the same chronological fold.",
        "- BTC and non-BTC weights/calibration are separate.",
        "- No hyperparameter search was performed.",
        "- The final historical fold was scored once and is now considered seen.",
        "- Live rows must close strictly after the latest inspected historical close.",
        "- Historical results cannot promote the challenger; manual review is at 30/60/150 prospective resolutions.",
    ])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-db", default="data/q15_strategy_bots_v3.sqlite3")
    parser.add_argument("--artifact", default="config/q15_rti_probability_v3.json")
    parser.add_argument("--report-dir", default="work/rti-probability-freeze-v3")
    parser.add_argument("--max-close-time", type=float)
    parser.add_argument("--prospective-after-close-time", type=float)
    args = parser.parse_args()
    examples, integrity = load_examples(args.strategy_db, args.max_close_time)
    inspected = inspection_boundary(args.strategy_db)
    boundary_values = [
        value
        for value in (inspected, args.prospective_after_close_time)
        if value is not None
    ]
    requested_boundary = (
        max(boundary_values)
        if boundary_values
        else max(float(row["close_time"]) for row in examples)
    )
    artifact, report = freeze_artifact(
        examples,
        prospective_after_close_time=requested_boundary,
    )
    report["integrity"] = integrity
    report["inspection_boundary_close_time"] = inspected
    artifact_path = Path(args.artifact)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "audit.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (report_dir / "audit.md").write_text(
        render_markdown(report, integrity), encoding="utf-8"
    )
    print(json.dumps({
        "artifact": str(artifact_path),
        "report": str(report_dir / "audit.json"),
        "model_version": artifact["model_version"],
        "prospective_after_close_time": artifact["prospective_after_close_time"],
        "usable_examples": integrity["usable_examples"],
        "test": {cohort: metrics["test"] for cohort, metrics in report["cohorts"].items()},
    }, indent=2))


if __name__ == "__main__":
    main()
