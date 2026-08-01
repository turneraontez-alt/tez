"""Retrospective diagnosis restricted to V11's already-opened pretest folds.

The locked V11 audit rejected on walk-forward before reading its untouched
test.  This tool reconstructs only those same train/calibration rows and
compares the frozen V11 feature architecture with the independently frozen V12
compact architecture.  It cannot read a V11 test label, emit an artifact,
promote a model, notify, or trade.  Results are development evidence only and
give V12 no historical credit.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots import rti_microstructure_v12 as feature_v12
from tools import q15_rti_microstructure_freeze as freeze
from tools.q15_rti_microstructure_preregister import DEFAULT_DB


DEFAULT_V11_DESIGN = ROOT / "config" / "q15_rti_microstructure_design_v11.json"
DEFAULT_V12_DESIGN = ROOT / "config" / "q15_rti_microstructure_design_v12.json"
DEFAULT_LOCKED_REPORT = (
    ROOT / "reports" / "q15_rti_v11_non_btc_freeze_20260722"
    / "non_btc_transfer-report.json"
)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"json_root_not_object:{path}")
    return dict(value)


def _assert_opened_pretest_only(report: Mapping[str, Any]) -> None:
    if report.get("status") != "REJECTED_ON_WALK_FORWARD_GATE":
        raise ValueError("v11_locked_report_not_walk_forward_rejection")
    if report.get("outcome_labels_read") is not True:
        raise ValueError("v11_pretest_labels_not_previously_opened")
    if report.get("untouched_test_labels_read") is not False:
        raise ValueError("v11_untouched_test_not_sealed")
    gate = report.get("walk_forward_gate")
    if not isinstance(gate, Mapping) or gate.get("untouched_test_rows_used") != 0:
        raise ValueError("v11_walk_forward_test_isolation_unproven")


def _attach_opened_labels(
    examples: Sequence[Mapping[str, Any]], labels: Mapping[int, int],
) -> list[dict[str, Any]]:
    output = []
    for row in examples:
        row_id = int(row["id"])
        if row_id not in labels:
            raise ValueError("opened_pretest_label_missing")
        output.append({**dict(row), "label_yes": int(labels[row_id])})
    return output


def _compact_examples(
    opened: Sequence[Mapping[str, Any]],
    raw_by_id: Mapping[int, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    output = []
    for row in opened:
        raw = raw_by_id.get(int(row["id"]))
        if raw is None:
            raise ValueError("compact_source_row_missing")
        # Do not call V12's official prospective feature vector: it correctly
        # rejects all pre-V12 rows.  For this explicitly retrospective
        # architecture diagnosis, reproduce only its frozen algebra from the
        # already-available V11 decision-time vector.  This can inform future
        # design work but can never count toward V12's prospective record.
        base = freeze.feature_v11.feature_vector(raw)
        if base.get("available") is not True:
            raise ValueError(
                f"compact_feature_unavailable:{row['id']}:{base.get('error')}"
            )
        by_name = dict(zip(freeze.feature_v11.FEATURE_NAMES, base["features"]))
        by_name[feature_v12.RELATIVE_MOMENTUM_FEATURE] = max(-400.0, min(
            400.0,
            float(by_name["independent_consensus_momentum_60s_bps"])
            - float(by_name["cross_asset_median_momentum_60s"]),
        ))
        output.append({
            **dict(row),
            "features": [float(by_name[name]) for name in feature_v12.FEATURE_NAMES],
            "feature_names": list(feature_v12.FEATURE_NAMES),
            "market_yes_probability": float(base["market_yes_probability"]),
        })
    return output


def _cosine(left: Sequence[float], right: Sequence[float]) -> float | None:
    a = np.asarray(left, dtype=np.float64)
    b = np.asarray(right, dtype=np.float64)
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denominator <= 0.0:
        return None
    return float((a @ b) / denominator)


def _architecture_diagnostic(
    examples: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    protocol: Mapping[str, Any],
    *,
    cohort: str,
) -> dict[str, Any]:
    close_times = tuple(sorted({float(row["close_time"]) for row in examples}))
    folds = freeze.expanding_walk_forward_folds(close_times, protocol, cohort)
    oof_rows: list[Mapping[str, Any]] = []
    oof_probabilities: list[float] = []
    models: list[dict[str, Any]] = []
    expected_assets = freeze.COHORT_ASSETS[cohort]
    for fold in folds:
        train_times = set(fold["train"])
        validation_times = set(fold["validation"])
        train = [
            row for row in examples if float(row["close_time"]) in train_times
        ]
        validation = [
            row for row in examples
            if float(row["close_time"]) in validation_times
        ]
        if len(validation) != len(validation_times) * len(expected_assets):
            raise ValueError("diagnostic_validation_rows_incomplete")
        model = freeze.fit_residual_model(train, config)
        probabilities, diagnostics = freeze.predict_probabilities(
            model, validation, config,
        )
        models.append(model)
        oof_rows.extend(validation)
        oof_probabilities.extend(probabilities)
        if any(bool(row["out_of_distribution"]) for row in diagnostics):
            raise ValueError("diagnostic_out_of_distribution_row")

    gate = freeze.expanding_walk_forward_gate(
        examples, config, protocol, cohort,
    )
    market_probabilities = [
        float(row["market_yes_probability"]) for row in oof_rows
    ]
    model_logits = np.asarray(
        [freeze._logit(value) for value in oof_probabilities],
        dtype=np.float64,
    )
    market_logits = np.asarray(
        [freeze._logit(value) for value in market_probabilities],
        dtype=np.float64,
    )
    corrections = model_logits - market_logits

    by_asset_rows: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_asset_probabilities: dict[str, list[float]] = defaultdict(list)
    for row, probability in zip(oof_rows, oof_probabilities):
        asset = str(row["asset"]).upper()
        by_asset_rows[asset].append(row)
        by_asset_probabilities[asset].append(probability)
    by_asset = {
        asset: freeze._proper_scores(
            by_asset_rows[asset], by_asset_probabilities[asset]
        )
        for asset in sorted(by_asset_rows)
    }

    feature_names = tuple(examples[0]["feature_names"])
    weight_matrix = np.asarray([model["weights"] for model in models])
    mean_abs_weights = np.mean(np.abs(weight_matrix), axis=0)
    top_indexes = np.argsort(-mean_abs_weights)[: min(12, len(feature_names))]
    cosine_values = []
    for left in range(len(models)):
        for right in range(left + 1, len(models)):
            value = _cosine(models[left]["weights"], models[right]["weights"])
            if value is not None:
                cosine_values.append(value)

    return {
        "feature_count": len(feature_names),
        "opened_close_windows": len(close_times),
        "oof_validation_close_windows": len({
            float(row["close_time"]) for row in oof_rows
        }),
        "oof_rows": len(oof_rows),
        "walk_forward_gate": gate,
        "logit_correction": {
            "mean": float(np.mean(corrections)),
            "mean_absolute": float(np.mean(np.abs(corrections))),
            "population_std": float(np.std(corrections)),
            "minimum": float(np.min(corrections)),
            "maximum": float(np.max(corrections)),
        },
        "coefficient_stability": {
            "fold_model_count": len(models),
            "pairwise_cosine_min": (
                None if not cosine_values else min(cosine_values)
            ),
            "pairwise_cosine_mean": (
                None if not cosine_values else float(np.mean(cosine_values))
            ),
            "pairwise_cosine_max": (
                None if not cosine_values else max(cosine_values)
            ),
            "top_mean_absolute_standardized_coefficients": [
                {
                    "feature": feature_names[int(index)],
                    "mean_absolute_weight": float(mean_abs_weights[int(index)]),
                    "fold_weights": [
                        float(model["weights"][int(index)]) for model in models
                    ],
                }
                for index in top_indexes
            ],
        },
        "by_asset": by_asset,
    }


def _blend_probability(market: float, model: float, factor: float) -> float:
    value = freeze._logit(market) + float(factor) * (
        freeze._logit(model) - freeze._logit(market)
    )
    return float(1.0 / (1.0 + math.exp(-max(-709.0, min(709.0, value)))))


def _score_deltas(
    examples: Sequence[Mapping[str, Any]], probabilities: Sequence[float],
) -> tuple[float, float]:
    scores = freeze._proper_scores(examples, probabilities)
    return (
        float(scores["brier_score"]) - float(scores["market_brier_score"]),
        float(scores["log_loss"]) - float(scores["market_log_loss"]),
    )


def _inner_oof_predictions(
    train: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    *,
    initial_windows: int = 12,
    block_windows: int = 4,
) -> tuple[list[Mapping[str, Any]], list[float]]:
    windows = tuple(sorted({float(row["close_time"]) for row in train}))
    if len(windows) <= initial_windows:
        return [], []
    rows: list[Mapping[str, Any]] = []
    probabilities: list[float] = []
    cursor = initial_windows
    while cursor < len(windows):
        validation_windows = set(windows[cursor:cursor + block_windows])
        training_windows = set(windows[:cursor])
        inner_train = [
            row for row in train if float(row["close_time"]) in training_windows
        ]
        inner_validation = [
            row for row in train
            if float(row["close_time"]) in validation_windows
        ]
        model = freeze.fit_residual_model(inner_train, config)
        predicted, diagnostics = freeze.predict_probabilities(
            model, inner_validation, config,
        )
        if any(bool(item["out_of_distribution"]) for item in diagnostics):
            raise ValueError("inner_safe_blend_out_of_distribution_row")
        rows.extend(inner_validation)
        probabilities.extend(predicted)
        cursor += block_windows
    return rows, probabilities


def _nested_safe_blend_diagnostic(
    examples: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    protocol: Mapping[str, Any],
    *,
    cohort: str,
) -> dict[str, Any]:
    """Select residual trust strictly inside each outer training period.

    The fixed grid includes zero, making the point-in-time Kalshi prior the
    fail-closed choice whenever inner chronological evidence does not improve
    both proper scores.  Outer validation labels never select their own blend.
    """
    factors = (0.0, 0.25, 0.5, 0.75, 1.0)
    close_times = tuple(sorted({float(row["close_time"]) for row in examples}))
    folds = freeze.expanding_walk_forward_folds(close_times, protocol, cohort)
    outer_rows: list[Mapping[str, Any]] = []
    outer_probabilities: list[float] = []
    fold_reports = []
    for fold in folds:
        train_times = set(fold["train"])
        validation_times = set(fold["validation"])
        train = [
            row for row in examples if float(row["close_time"]) in train_times
        ]
        validation = [
            row for row in examples
            if float(row["close_time"]) in validation_times
        ]
        inner_rows, inner_base = _inner_oof_predictions(train, config)
        candidates = []
        for factor in factors:
            blended = [
                _blend_probability(
                    float(row["market_yes_probability"]), probability, factor
                )
                for row, probability in zip(inner_rows, inner_base)
            ]
            brier_delta, log_delta = _score_deltas(inner_rows, blended)
            candidates.append({
                "factor": factor,
                "brier_delta_vs_market": brier_delta,
                "log_loss_delta_vs_market": log_delta,
                "improves_both": bool(brier_delta < 0.0 and log_delta < 0.0),
            })
        eligible = [row for row in candidates if row["improves_both"]]
        selected = min(
            eligible,
            key=lambda row: (
                row["brier_delta_vs_market"] + row["log_loss_delta_vs_market"],
                row["factor"],
            ),
            default=next(row for row in candidates if row["factor"] == 0.0),
        )
        model = freeze.fit_residual_model(train, config)
        base_probabilities, diagnostics = freeze.predict_probabilities(
            model, validation, config,
        )
        if any(bool(item["out_of_distribution"]) for item in diagnostics):
            raise ValueError("outer_safe_blend_out_of_distribution_row")
        blended_probabilities = [
            _blend_probability(
                float(row["market_yes_probability"]),
                probability,
                float(selected["factor"]),
            )
            for row, probability in zip(validation, base_probabilities)
        ]
        metrics = freeze._proper_scores(validation, blended_probabilities)
        fold_reports.append({
            "fold": int(fold["fold"]),
            "outer_train_close_windows": len(train_times),
            "outer_validation_close_windows": len(validation_times),
            "inner_oof_close_windows": len({
                float(row["close_time"]) for row in inner_rows
            }),
            "selected_factor": float(selected["factor"]),
            "selection_used_outer_validation_labels": False,
            "inner_candidates": candidates,
            "outer_metrics": metrics,
        })
        outer_rows.extend(validation)
        outer_probabilities.extend(blended_probabilities)
    aggregate = freeze._proper_scores(outer_rows, outer_probabilities)
    return {
        "architecture": "nested-chronological-safe-residual-blend-v1",
        "fixed_factor_grid": list(factors),
        "fallback_factor": 0.0,
        "selection_rule": (
            "inside_outer_train_only; require_brier_and_log_loss_improvement; "
            "otherwise_market_prior"
        ),
        "outer_validation_labels_used_for_selection": False,
        "untouched_test_rows_used": 0,
        "aggregate": aggregate,
        "folds": fold_reports,
    }


def build_report(
    *,
    strategy_db: Path = DEFAULT_DB,
    v11_design_path: Path = DEFAULT_V11_DESIGN,
    v12_design_path: Path = DEFAULT_V12_DESIGN,
    locked_report_path: Path = DEFAULT_LOCKED_REPORT,
) -> dict[str, Any]:
    locked_report = _load_json(locked_report_path)
    _assert_opened_pretest_only(locked_report)
    v11_design = _load_json(v11_design_path)
    v12_design = _load_json(v12_design_path)
    freeze.validate_design(v11_design)
    freeze.validate_design(v12_design)
    if v11_design.get("design_id") != freeze.feature_v11.DESIGN_ID:
        raise ValueError("diagnostic_v11_design_mismatch")
    if v12_design.get("design_id") != feature_v12.DESIGN_ID:
        raise ValueError("diagnostic_v12_design_mismatch")

    feature_rows = freeze.load_feature_rows(strategy_db)
    v11_examples, selected_windows = freeze.prepare_unlabeled_examples(
        feature_rows, v11_design, "NON_BTC_TRANSFER"
    )
    folds = freeze.chronological_folds(selected_windows, v11_design)
    opened_times = set(folds["train"]) | set(folds["calibration"])
    sealed_times = set(folds["test"])
    opened_unlabeled = [
        row for row in v11_examples
        if float(row["close_time"]) in opened_times
    ]
    sealed_ids = {
        int(row["id"]) for row in v11_examples
        if float(row["close_time"]) in sealed_times
    }
    opened_ids = {int(row["id"]) for row in opened_unlabeled}
    if opened_ids.intersection(sealed_ids):
        raise ValueError("diagnostic_fold_id_overlap")
    labels = freeze.load_labels(strategy_db, sorted(opened_ids))
    if set(labels) != opened_ids:
        raise ValueError("diagnostic_opened_labels_incomplete")
    opened_v11 = _attach_opened_labels(opened_unlabeled, labels)
    raw_by_id = {int(row["id"]): row for row in feature_rows}
    opened_v12 = _compact_examples(opened_v11, raw_by_id)
    protocol = freeze.walk_forward_protocol_for_design(v11_design)
    if protocol is None:
        raise ValueError("diagnostic_walk_forward_protocol_missing")

    v11_result = _architecture_diagnostic(
        opened_v11,
        dict(v11_design["fixed_training_config"]),
        protocol,
        cohort="NON_BTC_TRANSFER",
    )
    v12_result = _architecture_diagnostic(
        opened_v12,
        dict(v12_design["fixed_training_config"]),
        protocol,
        cohort="NON_BTC_TRANSFER",
    )
    v12_safe_blend = _nested_safe_blend_diagnostic(
        opened_v12,
        dict(v12_design["fixed_training_config"]),
        protocol,
        cohort="NON_BTC_TRANSFER",
    )
    return {
        "diagnostic_version": "q15-rti-v11-opened-fold-architecture-diagnostic-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "paper_only": True,
        "retrospective_development_only": True,
        "historical_credit_allowed": False,
        "automatic_promotion": False,
        "automatic_activation": False,
        "notification_eligible": False,
        "real_trading_allowed": False,
        "v11_design_id": v11_design["design_id"],
        "v12_design_id": v12_design["design_id"],
        "selected_v11_close_windows": len(selected_windows),
        "opened_pretest_close_windows": len(opened_times),
        "opened_pretest_rows": len(opened_v11),
        "untouched_test_close_windows": len(sealed_times),
        "untouched_test_rows": len(sealed_ids),
        "untouched_test_labels_read": False,
        "untouched_test_rows_used": 0,
        "v12_receives_historical_credit": False,
        "official_v12_prospective_feature_vector_called": False,
        "compact_projection_reconstructed_from_opened_v11_features": True,
        "comparison": {
            "V11_FROZEN_71_FEATURE_CONTROL": v11_result,
            "V12_FROZEN_20_FEATURE_COMPACT_RETROSPECTIVE": v12_result,
            "V12_COMPACT_NESTED_SAFE_BLEND_EXPLORATORY": v12_safe_blend,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-db", default=str(DEFAULT_DB))
    parser.add_argument("--v11-design", default=str(DEFAULT_V11_DESIGN))
    parser.add_argument("--v12-design", default=str(DEFAULT_V12_DESIGN))
    parser.add_argument("--locked-report", default=str(DEFAULT_LOCKED_REPORT))
    parser.add_argument(
        "--output",
        default=str(
            ROOT / "reports" / "q15_rti_v11_opened_fold_diagnostic.json"
        ),
    )
    args = parser.parse_args()
    report = build_report(
        strategy_db=Path(args.strategy_db),
        v11_design_path=Path(args.v11_design),
        v12_design_path=Path(args.v12_design),
        locked_report_path=Path(args.locked_report),
    )
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    freeze.atomic_write_json(target, report)
    summary = {
        "output": str(target),
        "opened_pretest_rows": report["opened_pretest_rows"],
        "untouched_test_rows_used": report["untouched_test_rows_used"],
        "v11_gate_met": report["comparison"][
            "V11_FROZEN_71_FEATURE_CONTROL"
        ]["walk_forward_gate"]["met"],
        "v12_compact_retrospective_gate_met": report["comparison"][
            "V12_FROZEN_20_FEATURE_COMPACT_RETROSPECTIVE"
        ]["walk_forward_gate"]["met"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
