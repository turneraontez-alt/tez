"""Pure in-memory V15 walk-forward evaluation against market and frozen V14.

This module accepts already-authorized labeled examples.  It has no database,
settlement, artifact-writer, notification, promotion, or trading capability.
It keeps each close window intact, fits BTC and non-BTC separately, selects
residual trust only inside each outer training period, and compares V15 with
both the point-in-time Kalshi prior and a frozen-V14 refit on identical rows.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots import rti_microstructure_v14 as v14
from q15_upgrade.strategy_bots import rti_microstructure_v15 as v15
from q15_upgrade.strategy_bots import (
    rti_microstructure_v15_audit_identity as audit_identity,
)
from q15_upgrade.strategy_bots.rti_microstructure_v15_identity import (
    DESIGN_ID,
    DESIGN_SHA256,
    EVALUATION_PROTOCOL_ID,
    EVALUATION_PROTOCOL_SHA256,
)
from tools.q15_rti_microstructure_freeze import (
    apply_residual_trust,
    fit_residual_model,
    predict_probabilities,
    select_residual_trust_factor,
)
from tools.q15_rti_microstructure_preregister import design_fingerprint


EVALUATOR_VERSION = audit_identity.WALK_FORWARD_EVALUATOR_VERSION
COHORT_ASSETS = {
    "NON_BTC_TRANSFER": frozenset(
        {"BNB", "DOGE", "ETH", "HYPE", "SOL", "XRP"}
    ),
    "BTC": frozenset({"BTC"}),
}


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _clip_probability(value: Any) -> float:
    probability = float(value)
    if not math.isfinite(probability):
        raise ValueError("v15_walk_forward_probability_nonfinite")
    return max(1e-6, min(1.0 - 1e-6, probability))


def _losses(
    rows: Sequence[Mapping[str, Any]],
    probabilities: Sequence[float],
) -> tuple[np.ndarray, np.ndarray]:
    if not rows or len(rows) != len(probabilities):
        raise ValueError("v15_walk_forward_loss_geometry_invalid")
    labels = np.asarray(
        [int(row["label_yes"]) for row in rows], dtype=np.float64,
    )
    if not np.isin(labels, (0.0, 1.0)).all():
        raise ValueError("v15_walk_forward_label_invalid")
    values = np.asarray(
        [_clip_probability(value) for value in probabilities],
        dtype=np.float64,
    )
    brier = np.square(values - labels)
    log_loss = -(
        labels * np.log(values) + (1.0 - labels) * np.log(1.0 - values)
    )
    return brier, log_loss


def _accuracy(
    rows: Sequence[Mapping[str, Any]],
    probabilities: Sequence[float],
) -> dict[str, Any]:
    labels = [int(row["label_yes"]) for row in rows]
    correct = sum(
        (float(probability) >= 0.5) == bool(label)
        for probability, label in zip(probabilities, labels)
    )
    count = len(labels)
    if count <= 0:
        raise ValueError("v15_walk_forward_accuracy_empty")
    z = 1.959963984540054
    rate = correct / count
    denominator = 1.0 + z * z / count
    centre = rate + z * z / (2.0 * count)
    margin = z * math.sqrt(
        rate * (1.0 - rate) / count + z * z / (4.0 * count * count)
    )
    return {
        "correct": correct,
        "accuracy": rate,
        "wilson_95_low": max(0.0, (centre - margin) / denominator),
        "wilson_95_high": min(1.0, (centre + margin) / denominator),
    }


def proper_scores(
    rows: Sequence[Mapping[str, Any]],
    probabilities: Sequence[float],
) -> dict[str, Any]:
    brier, log_loss = _losses(rows, probabilities)
    return {
        "rows": len(rows),
        "close_windows": len({
            float(row["close_time"]) for row in rows
        }),
        **_accuracy(rows, probabilities),
        "brier_score": float(brier.mean()),
        "log_loss": float(log_loss.mean()),
    }


def paired_comparator_bootstrap(
    rows: Sequence[Mapping[str, Any]],
    candidate_probabilities: Sequence[float],
    comparator_probabilities: Sequence[float],
    *,
    seed: int,
    resamples: int = 5000,
    confidence_level: float = 0.9,
    comparator_name: str,
) -> dict[str, Any]:
    if (
        len(rows) != len(candidate_probabilities)
        or len(rows) != len(comparator_probabilities)
        or not rows
    ):
        raise ValueError("v15_walk_forward_bootstrap_geometry_invalid")
    if resamples != 5000 or confidence_level != 0.9:
        raise ValueError("v15_walk_forward_bootstrap_contract_mismatch")
    candidate_brier, candidate_log = _losses(
        rows, candidate_probabilities,
    )
    comparator_brier, comparator_log = _losses(
        rows, comparator_probabilities,
    )
    clustered: dict[float, list[tuple[float, float]]] = defaultdict(list)
    for index, row in enumerate(rows):
        close_time = float(row["close_time"])
        clustered[close_time].append((
            float(candidate_brier[index] - comparator_brier[index]),
            float(candidate_log[index] - comparator_log[index]),
        ))
    close_times = tuple(sorted(clustered))
    window_deltas = np.asarray([
        np.asarray(clustered[close_time], dtype=np.float64).mean(axis=0)
        for close_time in close_times
    ])
    if not np.isfinite(window_deltas).all():
        raise ValueError("v15_walk_forward_bootstrap_nonfinite")
    rng = np.random.default_rng(int(seed))
    indexes = rng.integers(
        0,
        len(close_times),
        size=(resamples, len(close_times)),
        endpoint=False,
    )
    means = window_deltas[indexes].mean(axis=1)
    alpha = 1.0 - confidence_level

    def summary(column: int) -> dict[str, float]:
        values = means[:, column]
        return {
            "observed_mean_delta": float(window_deltas[:, column].mean()),
            "two_sided_lower": float(
                np.quantile(values, alpha / 2.0)
            ),
            "two_sided_upper": float(
                np.quantile(values, 1.0 - alpha / 2.0)
            ),
            "one_sided_upper": float(
                np.quantile(values, confidence_level)
            ),
            "bootstrap_probability_delta_below_zero": float(
                np.mean(values < 0.0)
            ),
        }

    return {
        "version": "q15-rti-paired-close-window-bootstrap-v1",
        "cluster_key": "close_time",
        "close_windows": len(close_times),
        "rows": len(rows),
        "resamples": resamples,
        "confidence_level": confidence_level,
        "random_seed": int(seed),
        "same_close_assets_resampled_together": True,
        "loss_delta_direction": f"V15_MINUS_{comparator_name}",
        "brier_delta": summary(0),
        "log_loss_delta": summary(1),
    }


def _validate_examples(
    examples: Sequence[Mapping[str, Any]],
    cohort: str,
    expected_windows: int,
) -> tuple[float, ...]:
    if cohort not in COHORT_ASSETS:
        raise ValueError("v15_walk_forward_unsupported_cohort")
    if len({int(row["id"]) for row in examples}) != len(examples):
        raise ValueError("v15_walk_forward_duplicate_row_id")
    windows = tuple(sorted({
        float(row["close_time"]) for row in examples
    }))
    if len(windows) != expected_windows:
        raise ValueError("v15_walk_forward_window_count_mismatch")
    expected_assets = COHORT_ASSETS[cohort]
    for close_time in windows:
        window_rows = [
            row for row in examples
            if float(row["close_time"]) == close_time
        ]
        assets = {
            str(row.get("asset") or "").upper() for row in window_rows
        }
        if (
            len(window_rows) != len(expected_assets)
            or assets != expected_assets
        ):
            raise ValueError("v15_walk_forward_same_close_asset_leakage")
        for row in window_rows:
            if (
                tuple(row.get("v15_feature_names") or ())
                != v15.FEATURE_NAMES
                or tuple(row.get("v14_feature_names") or ())
                != v14.FEATURE_NAMES
                or len(row.get("v15_features") or ()) != 25
                or len(row.get("v14_features") or ()) != 20
                or [float(value) for value in row["v15_features"][:20]]
                != [float(value) for value in row["v14_features"]]
                or int(row.get("label_yes", -1)) not in {0, 1}
                or not 0.0 < float(row["market_yes_probability"]) < 1.0
            ):
                raise ValueError("v15_walk_forward_example_invalid")
    return windows


def _with_features(
    rows: Sequence[Mapping[str, Any]],
    key: str,
) -> list[dict[str, Any]]:
    names_key = {
        "v15_features": "v15_feature_names",
        "v14_features": "v14_feature_names",
    }.get(key)
    if names_key is None:
        raise ValueError("v15_walk_forward_feature_key_invalid")
    return [
        {
            **dict(row),
            "features": [float(value) for value in row[key]],
            "feature_names": list(row[names_key]),
        }
        for row in rows
    ]


def _trust_protocol(
    design: Mapping[str, Any],
    protocol: Mapping[str, Any],
) -> dict[str, Any]:
    combination = dict(design["prediction_combination"])
    return {
        "residual_trust_selection": {
            "architecture": combination["architecture"],
            "fixed_factor_grid": list(combination["fixed_factor_grid"]),
            "fallback_factor": float(combination["fallback_factor"]),
            "factor_zero_is_exact_market_prior": True,
            "bootstrap": {
                "version": "q15-rti-paired-close-window-bootstrap-v1",
                "cluster_key": "close_time",
                "resamples": 5000,
                "confidence_level": 0.9,
                "random_seed": 2026072202,
                "minimum_mean_brier_improvement": 0.0,
                "minimum_mean_log_loss_improvement": 0.0,
                "same_close_assets_resampled_together": True,
            },
            "inner_folds": {
                cohort: {
                    "initial_train_windows": int(
                        dict(protocol["cohorts"][cohort])[
                            "inner_initial_train_windows"
                        ]
                    ),
                    "validation_block_windows": int(
                        dict(protocol["cohorts"][cohort])[
                            "inner_validation_block_windows"
                        ]
                    ),
                }
                for cohort in COHORT_ASSETS
            },
        },
    }


def _predict_one_architecture(
    train: Sequence[Mapping[str, Any]],
    validation: Sequence[Mapping[str, Any]],
    *,
    feature_key: str,
    config: Mapping[str, Any],
    trust_protocol: Mapping[str, Any],
    cohort: str,
) -> tuple[list[float], dict[str, Any], int]:
    training = _with_features(train, feature_key)
    evaluation = _with_features(validation, feature_key)
    trust = select_residual_trust_factor(
        training, config, trust_protocol, cohort,
    )
    if (
        trust.get("outer_validation_labels_used_for_selection") is not False
        or trust.get("calibration_labels_used_for_selection") is not False
        or trust.get("untouched_test_labels_used_for_selection") is not False
    ):
        raise ValueError("v15_walk_forward_trust_label_leakage")
    model = fit_residual_model(training, config)
    base, diagnostics = predict_probabilities(model, evaluation, config)
    probabilities = apply_residual_trust(evaluation, base, trust)
    return (
        [float(value) for value in probabilities],
        dict(trust),
        sum(bool(item["out_of_distribution"]) for item in diagnostics),
    )


def _outer_folds(
    windows: Sequence[float],
    protocol: Mapping[str, Any],
    cohort: str,
) -> list[dict[str, Any]]:
    rule = dict(protocol["cohorts"][cohort])
    initial = int(rule["initial_train_windows"])
    block = int(rule["validation_block_windows"])
    count = int(rule["walk_forward_fold_count"])
    if len(windows) != initial + block * count:
        raise ValueError("v15_walk_forward_pretest_geometry_invalid")
    output = []
    for index in range(count):
        start = initial + index * block
        train = tuple(float(value) for value in windows[:start])
        validation = tuple(
            float(value) for value in windows[start:start + block]
        )
        if not validation or max(train) >= min(validation):
            raise ValueError("v15_walk_forward_outer_chronology_invalid")
        output.append({
            "fold": index + 1,
            "train": train,
            "validation": validation,
        })
    return output


def _comparison(
    rows: Sequence[Mapping[str, Any]],
    candidate: Sequence[float],
    comparator: Sequence[float],
    *,
    comparator_name: str,
    seed: int,
) -> dict[str, Any]:
    candidate_scores = proper_scores(rows, candidate)
    comparator_scores = proper_scores(rows, comparator)
    bootstrap = paired_comparator_bootstrap(
        rows,
        candidate,
        comparator,
        seed=seed,
        comparator_name=comparator_name,
    )
    return {
        "candidate_brier_score": candidate_scores["brier_score"],
        "comparator_brier_score": comparator_scores["brier_score"],
        "candidate_minus_comparator_brier": (
            candidate_scores["brier_score"]
            - comparator_scores["brier_score"]
        ),
        "candidate_log_loss": candidate_scores["log_loss"],
        "comparator_log_loss": comparator_scores["log_loss"],
        "candidate_minus_comparator_log_loss": (
            candidate_scores["log_loss"]
            - comparator_scores["log_loss"]
        ),
        "paired_close_window_bootstrap": bootstrap,
    }


def _row_ids_sha256(rows: Sequence[Mapping[str, Any]]) -> str:
    return _canonical_sha256(sorted(int(row["id"]) for row in rows))


def evaluate_walk_forward(
    labeled_pretest: Sequence[Mapping[str, Any]],
    *,
    cohort: str,
    design: Mapping[str, Any],
    protocol: Mapping[str, Any],
) -> dict[str, Any]:
    if cohort not in COHORT_ASSETS:
        raise ValueError("v15_walk_forward_unsupported_cohort")
    if (
        design.get("design_id") != DESIGN_ID
        or design_fingerprint(design) != DESIGN_SHA256
        or protocol.get("protocol_id") != EVALUATION_PROTOCOL_ID
        or design_fingerprint(protocol) != EVALUATION_PROTOCOL_SHA256
    ):
        raise ValueError("v15_walk_forward_contract_identity_mismatch")
    rule = dict(protocol["cohorts"][cohort])
    expected_windows = (
        int(rule["initial_train_windows"])
        + int(rule["validation_block_windows"])
        * int(rule["walk_forward_fold_count"])
    )
    windows = _validate_examples(
        labeled_pretest, cohort, expected_windows,
    )
    folds = _outer_folds(windows, protocol, cohort)
    config = dict(design["fixed_training_config"])
    trust_protocol = _trust_protocol(design, protocol)
    fold_reports = []
    aggregate_rows: list[Mapping[str, Any]] = []
    aggregate_candidate: list[float] = []
    aggregate_v14: list[float] = []
    aggregate_market: list[float] = []
    for fold in folds:
        train_times = set(fold["train"])
        validation_times = set(fold["validation"])
        train = [
            row for row in labeled_pretest
            if float(row["close_time"]) in train_times
        ]
        validation = [
            row for row in labeled_pretest
            if float(row["close_time"]) in validation_times
        ]
        candidate, candidate_trust, candidate_ood = (
            _predict_one_architecture(
                train,
                validation,
                feature_key="v15_features",
                config=config,
                trust_protocol=trust_protocol,
                cohort=cohort,
            )
        )
        control, control_trust, control_ood = _predict_one_architecture(
            train,
            validation,
            feature_key="v14_features",
            config=config,
            trust_protocol=trust_protocol,
            cohort=cohort,
        )
        market = [
            float(row["market_yes_probability"]) for row in validation
        ]
        vs_market = _comparison(
            validation,
            candidate,
            market,
            comparator_name="MARKET",
            seed=int(dict(protocol["paired_close_window_bootstrap"])[
                "candidate_minus_market_random_seed"
            ]),
        )
        vs_v14 = _comparison(
            validation,
            candidate,
            control,
            comparator_name="V14",
            seed=int(dict(protocol["paired_close_window_bootstrap"])[
                "candidate_minus_v14_random_seed"
            ]),
        )
        fold_reports.append({
            "fold": int(fold["fold"]),
            "train_close_windows": len(train_times),
            "validation_close_windows": len(validation_times),
            "train_last_close_time": max(train_times),
            "validation_first_close_time": min(validation_times),
            "candidate_selected_residual_trust_factor": (
                candidate_trust["selected_factor"]
            ),
            "v14_selected_residual_trust_factor": (
                control_trust["selected_factor"]
            ),
            "candidate_out_of_distribution_rows": candidate_ood,
            "v14_out_of_distribution_rows": control_ood,
            "candidate_scores": proper_scores(validation, candidate),
            "v14_scores": proper_scores(validation, control),
            "market_scores": proper_scores(validation, market),
            "candidate_vs_market": vs_market,
            "candidate_vs_v14": vs_v14,
            "candidate_not_worse_than_market": bool(
                vs_market["candidate_minus_comparator_brier"] <= 0.0
                and vs_market["candidate_minus_comparator_log_loss"] <= 0.0
            ),
            "candidate_not_worse_than_v14": bool(
                vs_v14["candidate_minus_comparator_brier"] <= 0.0
                and vs_v14["candidate_minus_comparator_log_loss"] <= 0.0
            ),
            "outer_validation_labels_used_for_trust_selection": False,
        })
        aggregate_rows.extend(validation)
        aggregate_candidate.extend(candidate)
        aggregate_v14.extend(control)
        aggregate_market.extend(market)

    bootstrap = dict(protocol["paired_close_window_bootstrap"])
    gate = dict(protocol["walk_forward_gate"])
    aggregate_market_comparison = _comparison(
        aggregate_rows,
        aggregate_candidate,
        aggregate_market,
        comparator_name="MARKET",
        seed=int(bootstrap["candidate_minus_market_random_seed"]),
    )
    aggregate_v14_comparison = _comparison(
        aggregate_rows,
        aggregate_candidate,
        aggregate_v14,
        comparator_name="V14",
        seed=int(bootstrap["candidate_minus_v14_random_seed"]),
    )
    market_bootstrap = aggregate_market_comparison[
        "paired_close_window_bootstrap"
    ]
    v14_bootstrap = aggregate_v14_comparison[
        "paired_close_window_bootstrap"
    ]
    development_count = int(rule["development_train_windows"])
    calibration_count = int(rule["calibration_windows"])
    calibration_partition_times = tuple(
        windows[development_count:development_count + calibration_count]
    )
    calibration_partition_set = set(calibration_partition_times)
    calibration_overlap_rows = [
        row for row in aggregate_rows
        if float(row["close_time"]) in calibration_partition_set
    ]
    if (
        len(calibration_partition_times) != calibration_count
        or {
            float(row["close_time"]) for row in calibration_overlap_rows
        } != calibration_partition_set
    ):
        raise ValueError(
            "v15_walk_forward_calibration_overlap_geometry_invalid"
        )
    checks = {
        "candidate_brier_beats_market": (
            aggregate_market_comparison[
                "candidate_minus_comparator_brier"
            ] < 0.0
        ),
        "candidate_log_loss_beats_market": (
            aggregate_market_comparison[
                "candidate_minus_comparator_log_loss"
            ] < 0.0
        ),
        "candidate_brier_beats_v14": (
            aggregate_v14_comparison[
                "candidate_minus_comparator_brier"
            ] < 0.0
        ),
        "candidate_log_loss_beats_v14": (
            aggregate_v14_comparison[
                "candidate_minus_comparator_log_loss"
            ] < 0.0
        ),
        "every_fold_not_worse_vs_market": all(
            bool(fold["candidate_not_worse_than_market"])
            for fold in fold_reports
        ),
        "every_fold_not_worse_vs_v14": all(
            bool(fold["candidate_not_worse_than_v14"])
            for fold in fold_reports
        ),
        "market_brier_mean_effect_floor": (
            aggregate_market_comparison[
                "candidate_minus_comparator_brier"
            ] <= float(gate[
                "aggregate_candidate_minus_market_brier_mean_must_be_at_most"
            ])
        ),
        "market_log_loss_mean_effect_floor": (
            aggregate_market_comparison[
                "candidate_minus_comparator_log_loss"
            ] <= float(gate[
                "aggregate_candidate_minus_market_log_loss_mean_must_be_at_most"
            ])
        ),
        "market_brier_bootstrap_effect_floor": (
            float(market_bootstrap["brier_delta"]["one_sided_upper"])
            <= float(gate[
                "aggregate_candidate_minus_market_bootstrap_upper_must_be_at_most"
            ])
        ),
        "market_log_loss_bootstrap_effect_floor": (
            float(market_bootstrap["log_loss_delta"]["one_sided_upper"])
            <= float(gate[
                "aggregate_candidate_minus_market_bootstrap_upper_must_be_at_most"
            ])
        ),
        "v14_brier_mean_effect_floor": (
            aggregate_v14_comparison[
                "candidate_minus_comparator_brier"
            ] <= float(gate[
                "aggregate_candidate_minus_v14_brier_mean_must_be_at_most"
            ])
        ),
        "v14_log_loss_mean_effect_floor": (
            aggregate_v14_comparison[
                "candidate_minus_comparator_log_loss"
            ] <= float(gate[
                "aggregate_candidate_minus_v14_log_loss_mean_must_be_at_most"
            ])
        ),
        "v14_brier_bootstrap_upper_below_zero": (
            float(v14_bootstrap["brier_delta"]["one_sided_upper"]) < 0.0
        ),
        "v14_log_loss_bootstrap_upper_below_zero": (
            float(v14_bootstrap["log_loss_delta"]["one_sided_upper"]) < 0.0
        ),
    }
    return {
        "evaluator_version": EVALUATOR_VERSION,
        "design_id": DESIGN_ID,
        "design_sha256": DESIGN_SHA256,
        "evaluation_protocol_id": EVALUATION_PROTOCOL_ID,
        "evaluation_protocol_sha256": EVALUATION_PROTOCOL_SHA256,
        "cohort": cohort,
        "rows": len(labeled_pretest),
        "close_windows": len(windows),
        "candidate_market_v14_identical_rows": True,
        "btc_and_non_btc_pooled": False,
        "same_close_assets_share_fold": True,
        "temporary_models_are_deployable": False,
        "untouched_test_rows_used": 0,
        "walk_forward_validation_overlaps_calibration_partition": True,
        "calibration_overlap_is_not_independent_confirmation": True,
        "only_untouched_test_is_independent_final_confirmation": True,
        "calibration_overlap_close_windows": len(
            calibration_partition_times
        ),
        "calibration_overlap_rows": len(calibration_overlap_rows),
        "calibration_overlap_close_times_sha256": _canonical_sha256(
            calibration_partition_times
        ),
        "calibration_overlap_row_ids_sha256": _row_ids_sha256(
            calibration_overlap_rows
        ),
        "accuracy_is_report_only": True,
        "outcome_labels_read": True,
        "model_fit_performed": True,
        "probability_scoring_performed": True,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
        "folds": fold_reports,
        "aggregate": {
            "candidate_scores": proper_scores(
                aggregate_rows, aggregate_candidate,
            ),
            "v14_scores": proper_scores(aggregate_rows, aggregate_v14),
            "market_scores": proper_scores(
                aggregate_rows, aggregate_market,
            ),
            "candidate_vs_market": aggregate_market_comparison,
            "candidate_vs_v14": aggregate_v14_comparison,
        },
        "gate_checks": checks,
        "gate_met": all(checks.values()),
        "failure_result": (
            None
            if all(checks.values())
            else "NO_V15_ARTIFACT_OR_PAPER_CHALLENGER"
        ),
        "input_rows_sha256": _row_ids_sha256(labeled_pretest),
        "input_close_times_sha256": _canonical_sha256(windows),
        "evaluation_rows_sha256": _canonical_sha256(sorted(
            int(row["id"]) for row in aggregate_rows
        )),
    }


def evaluate_calibration(
    labeled_pretest: Sequence[Mapping[str, Any]],
    *,
    cohort: str,
    design: Mapping[str, Any],
    protocol: Mapping[str, Any],
    walk_forward_report: Mapping[str, Any],
) -> dict[str, Any]:
    if cohort not in COHORT_ASSETS:
        raise ValueError("v15_walk_forward_unsupported_cohort")
    if (
        walk_forward_report.get("evaluator_version") != EVALUATOR_VERSION
        or walk_forward_report.get("design_id") != DESIGN_ID
        or walk_forward_report.get("design_sha256") != DESIGN_SHA256
        or walk_forward_report.get("evaluation_protocol_id")
        != EVALUATION_PROTOCOL_ID
        or walk_forward_report.get("evaluation_protocol_sha256")
        != EVALUATION_PROTOCOL_SHA256
        or walk_forward_report.get("cohort") != cohort
        or walk_forward_report.get("gate_met") is not True
        or walk_forward_report.get("input_rows_sha256")
        != _row_ids_sha256(labeled_pretest)
    ):
        raise ValueError("v15_calibration_walk_forward_gate_missing_or_mismatch")
    if (
        design.get("design_id") != DESIGN_ID
        or design_fingerprint(design) != DESIGN_SHA256
        or protocol.get("protocol_id") != EVALUATION_PROTOCOL_ID
        or design_fingerprint(protocol) != EVALUATION_PROTOCOL_SHA256
    ):
        raise ValueError("v15_calibration_contract_identity_mismatch")
    rule = dict(protocol["cohorts"][cohort])
    development_count = int(rule["development_train_windows"])
    calibration_count = int(rule["calibration_windows"])
    pretest_count = development_count + calibration_count
    windows = _validate_examples(
        labeled_pretest, cohort, pretest_count,
    )
    development_times = set(windows[:development_count])
    calibration_times = tuple(windows[development_count:])
    development = [
        row for row in labeled_pretest
        if float(row["close_time"]) in development_times
    ]
    calibration = [
        row for row in labeled_pretest
        if float(row["close_time"]) in set(calibration_times)
    ]
    if (
        walk_forward_report.get(
            "walk_forward_validation_overlaps_calibration_partition"
        ) is not True
        or walk_forward_report.get(
            "calibration_overlap_is_not_independent_confirmation"
        ) is not True
        or walk_forward_report.get(
            "only_untouched_test_is_independent_final_confirmation"
        ) is not True
        or int(
            walk_forward_report.get(
                "calibration_overlap_close_windows", -1
            )
        ) != calibration_count
        or int(
            walk_forward_report.get("calibration_overlap_rows", -1)
        ) != len(calibration)
        or walk_forward_report.get(
            "calibration_overlap_close_times_sha256"
        ) != _canonical_sha256(calibration_times)
        or walk_forward_report.get(
            "calibration_overlap_row_ids_sha256"
        ) != _row_ids_sha256(calibration)
    ):
        raise ValueError(
            "v15_calibration_walk_forward_overlap_disclosure_mismatch"
        )
    config = dict(design["fixed_training_config"])
    trust_protocol = _trust_protocol(design, protocol)
    candidate, candidate_trust, candidate_ood = _predict_one_architecture(
        development,
        calibration,
        feature_key="v15_features",
        config=config,
        trust_protocol=trust_protocol,
        cohort=cohort,
    )
    control, control_trust, control_ood = _predict_one_architecture(
        development,
        calibration,
        feature_key="v14_features",
        config=config,
        trust_protocol=trust_protocol,
        cohort=cohort,
    )
    market = [
        float(row["market_yes_probability"]) for row in calibration
    ]
    bootstrap = dict(protocol["paired_close_window_bootstrap"])
    vs_market = _comparison(
        calibration,
        candidate,
        market,
        comparator_name="MARKET",
        seed=int(bootstrap["candidate_minus_market_random_seed"]),
    )
    vs_v14 = _comparison(
        calibration,
        candidate,
        control,
        comparator_name="V14",
        seed=int(bootstrap["candidate_minus_v14_random_seed"]),
    )
    split = len(calibration_times) // 2
    half_times = (
        set(calibration_times[:split]),
        set(calibration_times[split:]),
    )
    halves = []
    for index, times in enumerate(half_times, start=1):
        indexes = [
            offset for offset, row in enumerate(calibration)
            if float(row["close_time"]) in times
        ]
        half_rows = [calibration[offset] for offset in indexes]
        half_candidate = [candidate[offset] for offset in indexes]
        half_control = [control[offset] for offset in indexes]
        half_market = [market[offset] for offset in indexes]
        half_market_comparison = _comparison(
            half_rows,
            half_candidate,
            half_market,
            comparator_name="MARKET",
            seed=int(bootstrap["candidate_minus_market_random_seed"]),
        )
        half_v14_comparison = _comparison(
            half_rows,
            half_candidate,
            half_control,
            comparator_name="V14",
            seed=int(bootstrap["candidate_minus_v14_random_seed"]),
        )
        halves.append({
            "half": index,
            "close_windows": len(times),
            "rows": len(half_rows),
            "candidate_vs_market": half_market_comparison,
            "candidate_vs_v14": half_v14_comparison,
            "not_worse_vs_market": bool(
                half_market_comparison[
                    "candidate_minus_comparator_brier"
                ] <= 0.0
                and half_market_comparison[
                    "candidate_minus_comparator_log_loss"
                ] <= 0.0
            ),
            "not_worse_vs_v14": bool(
                half_v14_comparison[
                    "candidate_minus_comparator_brier"
                ] <= 0.0
                and half_v14_comparison[
                    "candidate_minus_comparator_log_loss"
                ] <= 0.0
            ),
        })
    gate = dict(protocol["walk_forward_gate"])
    market_bootstrap = vs_market["paired_close_window_bootstrap"]
    v14_bootstrap = vs_v14["paired_close_window_bootstrap"]
    checks = {
        "walk_forward_gate_passed": True,
        "market_brier_mean_effect_floor": (
            vs_market["candidate_minus_comparator_brier"]
            <= float(gate[
                "aggregate_candidate_minus_market_brier_mean_must_be_at_most"
            ])
        ),
        "market_log_loss_mean_effect_floor": (
            vs_market["candidate_minus_comparator_log_loss"]
            <= float(gate[
                "aggregate_candidate_minus_market_log_loss_mean_must_be_at_most"
            ])
        ),
        "market_brier_bootstrap_effect_floor": (
            float(market_bootstrap["brier_delta"]["one_sided_upper"])
            <= float(gate[
                "aggregate_candidate_minus_market_bootstrap_upper_must_be_at_most"
            ])
        ),
        "market_log_loss_bootstrap_effect_floor": (
            float(market_bootstrap["log_loss_delta"]["one_sided_upper"])
            <= float(gate[
                "aggregate_candidate_minus_market_bootstrap_upper_must_be_at_most"
            ])
        ),
        "v14_brier_mean_effect_floor": (
            vs_v14["candidate_minus_comparator_brier"]
            <= float(gate[
                "aggregate_candidate_minus_v14_brier_mean_must_be_at_most"
            ])
        ),
        "v14_log_loss_mean_effect_floor": (
            vs_v14["candidate_minus_comparator_log_loss"]
            <= float(gate[
                "aggregate_candidate_minus_v14_log_loss_mean_must_be_at_most"
            ])
        ),
        "v14_brier_bootstrap_upper_below_zero": (
            float(v14_bootstrap["brier_delta"]["one_sided_upper"]) < 0.0
        ),
        "v14_log_loss_bootstrap_upper_below_zero": (
            float(v14_bootstrap["log_loss_delta"]["one_sided_upper"]) < 0.0
        ),
        "first_half_not_worse_vs_either": bool(
            halves[0]["not_worse_vs_market"]
            and halves[0]["not_worse_vs_v14"]
        ),
        "second_half_not_worse_vs_either": bool(
            halves[1]["not_worse_vs_market"]
            and halves[1]["not_worse_vs_v14"]
        ),
    }
    passed = all(checks.values())
    final_candidate_trust = None
    final_v14_trust = None
    if passed:
        final_candidate_trust = select_residual_trust_factor(
            _with_features(labeled_pretest, "v15_features"),
            config,
            trust_protocol,
            cohort,
        )
        final_v14_trust = select_residual_trust_factor(
            _with_features(labeled_pretest, "v14_features"),
            config,
            trust_protocol,
            cohort,
        )
        for selection in (final_candidate_trust, final_v14_trust):
            if (
                selection.get(
                    "outer_validation_labels_used_for_selection"
                ) is not False
                or selection.get(
                    "untouched_test_labels_used_for_selection"
                ) is not False
            ):
                raise ValueError("v15_calibration_final_trust_label_leakage")
    return {
        "evaluator_version": EVALUATOR_VERSION,
        "stage": "CALIBRATION",
        "design_id": DESIGN_ID,
        "design_sha256": DESIGN_SHA256,
        "evaluation_protocol_id": EVALUATION_PROTOCOL_ID,
        "evaluation_protocol_sha256": EVALUATION_PROTOCOL_SHA256,
        "cohort": cohort,
        "development_close_windows": development_count,
        "calibration_close_windows": calibration_count,
        "calibration_rows": len(calibration),
        "candidate_market_v14_identical_rows": True,
        "btc_and_non_btc_pooled": False,
        "untouched_test_rows_used": 0,
        "calibration_rows_were_already_walk_forward_validation_rows": True,
        "calibration_is_not_independent_confirmation": True,
        "only_untouched_test_is_independent_final_confirmation": True,
        "walk_forward_overlap_close_times_sha256": _canonical_sha256(
            calibration_times
        ),
        "walk_forward_overlap_row_ids_sha256": _row_ids_sha256(
            calibration
        ),
        "accuracy_is_report_only": True,
        "outcome_labels_read": True,
        "model_fit_performed": True,
        "final_model_fit_performed": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
        "candidate_selected_development_trust_factor": (
            candidate_trust["selected_factor"]
        ),
        "v14_selected_development_trust_factor": (
            control_trust["selected_factor"]
        ),
        "candidate_out_of_distribution_rows": candidate_ood,
        "v14_out_of_distribution_rows": control_ood,
        "candidate_scores": proper_scores(calibration, candidate),
        "v14_scores": proper_scores(calibration, control),
        "market_scores": proper_scores(calibration, market),
        "candidate_vs_market": vs_market,
        "candidate_vs_v14": vs_v14,
        "chronological_halves": halves,
        "gate_checks": checks,
        "gate_met": passed,
        "failure_result": (
            None if passed else "NO_V15_ARTIFACT_OR_PAPER_CHALLENGER"
        ),
        "final_candidate_trust_selection": final_candidate_trust,
        "final_v14_trust_selection": final_v14_trust,
        "calibration_labels_used_for_final_factor_selection": passed,
        "untouched_test_labels_used_for_final_factor_selection": False,
        "input_rows_sha256": _row_ids_sha256(labeled_pretest),
        "calibration_rows_sha256": _row_ids_sha256(calibration),
    }
