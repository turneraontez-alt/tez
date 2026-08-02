"""Pure in-memory V17 development walk-forward evaluator.

Callers must supply already-authorized labels. This module has no database,
network, settlement, notification, artifact, promotion, or order capability.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots import rti_microstructure_v14 as v14
from q15_upgrade.strategy_bots import rti_microstructure_v15 as v15
from q15_upgrade.strategy_bots import rti_microstructure_v16 as v16
from q15_upgrade.strategy_bots import rti_microstructure_v17 as v17
from q15_upgrade.strategy_bots import rti_microstructure_v14_identity as v14_identity
from q15_upgrade.strategy_bots import rti_microstructure_v15_identity as v15_identity
from q15_upgrade.strategy_bots import rti_microstructure_v16_identity as v16_identity
from q15_upgrade.strategy_bots import rti_microstructure_v17_audit_identity as audit_identity
from q15_upgrade.strategy_bots import rti_microstructure_v17_identity as v17_identity
from tools import q15_rti_v16_development_evaluator as score_utils
from tools.q15_rti_microstructure_freeze import (
    apply_residual_trust,
    fit_residual_model,
    predict_probabilities,
    select_residual_trust_factor,
)
from tools.q15_rti_microstructure_preregister import design_fingerprint


COHORT = "NON_BTC_TRANSFER"
COHORT_ASSETS = frozenset({"BNB", "DOGE", "ETH", "HYPE", "SOL", "XRP"})
DEFAULT_CONTRACT = ROOT / audit_identity.EVALUATOR_CONTRACT_RELATIVE_PATH
DEFAULT_PROTOCOL = ROOT / v17_identity.PROTOCOL_RELATIVE_PATH
DEFAULT_V16_PROTOCOL = ROOT / v16_identity.PROTOCOL_RELATIVE_PATH
DEFAULT_V15_DESIGN = ROOT / "config" / "q15_rti_microstructure_design_v15.json"
DEFAULT_V14_DESIGN = ROOT / "config" / "q15_rti_microstructure_design_v14.json"
ARCHITECTURES = (
    ("V17", "v17_features", "v17_feature_names", v17.FEATURE_NAMES),
    ("V16", "v16_features", "v16_feature_names", v16.FEATURE_NAMES),
    ("V15", "v15_features", "v15_feature_names", v15.FEATURE_NAMES),
    ("V14", "v14_features", "v14_feature_names", v14.FEATURE_NAMES),
)


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _load(path: Path, error: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(error) from exc
    if not isinstance(value, Mapping):
        raise ValueError(error)
    return dict(value)


def load_contract(path: Path = DEFAULT_CONTRACT) -> dict[str, Any]:
    contract = _load(path, "v17_evaluator_contract_unreadable")
    folds = dict(contract.get("folds") or {})
    trust = dict(contract.get("trust_selection") or {})
    grids = dict(trust.get("fixed_factor_grid_by_architecture") or {})
    seeds = dict(trust.get("bootstrap_random_seed_by_architecture") or {})
    comparison = dict(contract.get("aggregate_comparison") or {})
    access = dict(contract.get("label_access") or {})
    result = dict(contract.get("result_policy") or {})
    control_grid = [0.0, 0.25, 0.5, 0.75, 1.0]
    if (
        design_fingerprint(contract) != audit_identity.EVALUATOR_CONTRACT_SHA256
        or contract.get("contract_id") != audit_identity.EVALUATOR_CONTRACT_ID
        or contract.get("contract_status")
        != "FROZEN_BEFORE_ANY_V17_DEVELOPMENT_LABEL_ACCESS"
        or contract.get("protocol_id") != v17_identity.PROTOCOL_ID
        or contract.get("protocol_sha256") != v17_identity.PROTOCOL_SHA256
        or contract.get("development_seal_sha256")
        != audit_identity.DEVELOPMENT_SEAL_SHA256
        or contract.get("cohort") != COHORT
        or set(contract.get("assets") or ()) != COHORT_ASSETS
        or int(contract.get("development_close_windows") or 0) != 240
        or int(contract.get("development_rows") or 0) != 1440
        or int(folds.get("initial_train_close_windows") or 0) != 120
        or int(folds.get("validation_block_close_windows") or 0) != 30
        or int(folds.get("outer_fold_count") or 0) != 4
        or int(folds.get("inner_initial_train_close_windows") or 0) != 60
        or int(folds.get("inner_validation_block_close_windows") or 0) != 30
        or grids.get("V17") != [0.0, 0.1, 0.25, 0.5, 0.75, 1.0]
        or any(grids.get(name) != control_grid for name in ("V16", "V15", "V14"))
        or set(seeds) != {"V17", "V16", "V15", "V14"}
        or int(trust.get("bootstrap_resamples") or 0) != 10000
        or float(trust.get("bootstrap_confidence_level") or 0.0) != 0.9
        or int(comparison.get("bootstrap_resamples") or 0) != 10000
        or float(comparison.get("bootstrap_confidence_level") or 0.0) != 0.9
        or access.get("confirmation_phrase") != audit_identity.CONFIRMATION_PHRASE
        or access.get("exclusive_reservation_before_callback") is not True
        or access.get("exact_sealed_row_ids_only") is not True
        or access.get("fresh_authoritative_kalshi_finalized_evidence_required")
        is not True
        or float(access.get("authoritative_verifier_max_requests_per_second") or 0.0)
        != 5.0
        or int(access.get("authoritative_verifier_capacity") or 0) != 1
        or int(access.get("fetch_attempts_per_contract") or 0) != 3
        or access.get("network_or_rate_limit_exhaustion_fails_closed") is not True
        or access.get("contract_ticker_asset_close_identity_required") is not True
        or access.get("local_cache_disagreement_fails_closed") is not True
        or access.get("btc_labels_forbidden") is not True
        or access.get("failed_or_interrupted_callback_is_permanently_ambiguous")
        is not True
        or access.get("reread_or_replay_forbidden") is not True
        or result.get("historical_development_result_can_promote") is not False
        or result.get("paper_artifact_allowed") is not False
        or result.get("notifications_allowed") is not False
        or result.get("automatic_promotion_allowed") is not False
        or result.get("real_trading_allowed") is not False
        or contract.get("outcome_labels_used_to_create_contract") is not False
        or contract.get("model_fit_performed_before_contract_freeze") is not False
        or contract.get("probability_scoring_performed_before_contract_freeze")
        is not False
    ):
        raise ValueError("v17_evaluator_contract_identity_or_safety_invalid")
    return contract


def load_protocol(path: Path = DEFAULT_PROTOCOL) -> dict[str, Any]:
    protocol = _load(path, "v17_evaluator_protocol_unreadable")
    if (
        design_fingerprint(protocol) != v17_identity.PROTOCOL_SHA256
        or protocol.get("protocol_id") != v17_identity.PROTOCOL_ID
        or protocol.get("design_id") != v17_identity.DESIGN_ID
    ):
        raise ValueError("v17_evaluator_protocol_identity_invalid")
    return protocol


def _architecture_configs(protocol: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    v16_protocol = _load(
        DEFAULT_V16_PROTOCOL, "v17_evaluator_v16_protocol_unreadable",
    )
    v15_design = _load(DEFAULT_V15_DESIGN, "v17_evaluator_v15_design_unreadable")
    v14_design = _load(DEFAULT_V14_DESIGN, "v17_evaluator_v14_design_unreadable")
    if (
        design_fingerprint(v16_protocol) != v16_identity.PROTOCOL_SHA256
        or v16_protocol.get("design_id") != v16_identity.DESIGN_ID
        or design_fingerprint(v15_design) != v15_identity.DESIGN_SHA256
        or v15_design.get("design_id") != v15_identity.DESIGN_ID
        or design_fingerprint(v14_design) != v14_identity.DESIGN_SHA256
        or v14_design.get("design_id") != v14_identity.DESIGN_ID
    ):
        raise ValueError("v17_evaluator_comparator_design_identity_invalid")
    return {
        "V17": dict(protocol["model"]),
        "V16": dict(v16_protocol["model"]),
        "V15": dict(v15_design["fixed_training_config"]),
        "V14": dict(v14_design["fixed_training_config"]),
    }


def _validate_examples(examples: Sequence[Mapping[str, Any]]) -> tuple[float, ...]:
    if len(examples) != 1440 or len({int(row["id"]) for row in examples}) != 1440:
        raise ValueError("v17_evaluator_row_geometry_invalid")
    windows = tuple(sorted({float(row["close_time"]) for row in examples}))
    if len(windows) != 240:
        raise ValueError("v17_evaluator_window_geometry_invalid")
    for close_time in windows:
        rows = [row for row in examples if float(row["close_time"]) == close_time]
        assets = {str(row.get("asset") or "").upper() for row in rows}
        if len(rows) != 6 or assets != COHORT_ASSETS:
            raise ValueError("v17_evaluator_same_close_asset_leakage")
        for row in rows:
            features: dict[str, list[float]] = {}
            for name, key, names_key, expected_names in ARCHITECTURES:
                values = [float(value) for value in row.get(key) or ()]
                if tuple(row.get(names_key) or ()) != expected_names:
                    raise ValueError(f"v17_evaluator_{name.lower()}_feature_names_invalid")
                if not np.isfinite(np.asarray(values, dtype=float)).all():
                    raise ValueError(f"v17_evaluator_{name.lower()}_features_nonfinite")
                features[name] = values
            if (
                len(features["V17"]) != 81
                or len(features["V16"]) != 45
                or len(features["V15"]) != 25
                or len(features["V14"]) != 20
                or features["V17"][:45] != features["V16"]
                or features["V16"][:25] != features["V15"]
                or features["V15"][:20] != features["V14"]
                or int(row.get("label_yes", -1)) not in {0, 1}
                or not 0.0 < float(row["market_yes_probability"]) < 1.0
                or str(row.get("asset") or "").upper() == "BTC"
            ):
                raise ValueError("v17_evaluator_example_invalid")
    return windows


def _with_features(
    rows: Sequence[Mapping[str, Any]], key: str, names_key: str,
) -> list[dict[str, Any]]:
    return [{
        **dict(row),
        "features": [float(value) for value in row[key]],
        "feature_names": list(row[names_key]),
    } for row in rows]


def _trust_protocol(contract: Mapping[str, Any], architecture: str) -> dict[str, Any]:
    trust = dict(contract["trust_selection"])
    grids = dict(trust["fixed_factor_grid_by_architecture"])
    seeds = dict(trust["bootstrap_random_seed_by_architecture"])
    return {
        "residual_trust_selection": {
            "architecture": f"v17_preregistered_{architecture.lower()}_inner_oof_trust_v1",
            "fixed_factor_grid": list(grids[architecture]),
            "fallback_factor": float(trust["fallback_factor"]),
            "bootstrap": {
                "version": "q15-rti-paired-close-window-bootstrap-v1",
                "cluster_key": "close_time",
                "resamples": int(trust["bootstrap_resamples"]),
                "confidence_level": float(trust["bootstrap_confidence_level"]),
                "random_seed": int(seeds[architecture]),
                "same_close_assets_resampled_together": True,
            },
            "inner_folds": {
                COHORT: {
                    "initial_train_windows": 60,
                    "validation_block_windows": 30,
                },
            },
        },
    }


def _predict(
    train: Sequence[Mapping[str, Any]],
    validation: Sequence[Mapping[str, Any]],
    *,
    feature_key: str,
    feature_names_key: str,
    config: Mapping[str, Any],
    trust_protocol: Mapping[str, Any],
) -> tuple[list[float], dict[str, Any], int]:
    training = _with_features(train, feature_key, feature_names_key)
    evaluation = _with_features(validation, feature_key, feature_names_key)
    trust = select_residual_trust_factor(training, config, trust_protocol, COHORT)
    if (
        trust.get("outer_validation_labels_used_for_selection") is not False
        or trust.get("calibration_labels_used_for_selection") is not False
        or trust.get("untouched_test_labels_used_for_selection") is not False
    ):
        raise ValueError("v17_evaluator_trust_label_leakage")
    model = fit_residual_model(training, config)
    base, diagnostics = predict_probabilities(model, evaluation, config)
    probabilities = apply_residual_trust(evaluation, base, trust)
    return (
        [float(value) for value in probabilities],
        dict(trust),
        sum(bool(item["out_of_distribution"]) for item in diagnostics),
    )


def paired_comparator_bootstrap(
    rows: Sequence[Mapping[str, Any]],
    candidate: Sequence[float],
    comparator: Sequence[float],
    *,
    comparator_name: str,
    seed: int,
    resamples: int,
    confidence_level: float,
) -> dict[str, Any]:
    candidate_brier, candidate_log = score_utils._losses(rows, candidate)
    comparator_brier, comparator_log = score_utils._losses(rows, comparator)
    clustered: dict[float, list[tuple[float, float]]] = defaultdict(list)
    for index, row in enumerate(rows):
        clustered[float(row["close_time"])].append((
            float(candidate_brier[index] - comparator_brier[index]),
            float(candidate_log[index] - comparator_log[index]),
        ))
    close_times = tuple(sorted(clustered))
    deltas = np.asarray([
        np.asarray(clustered[close_time], dtype=float).mean(axis=0)
        for close_time in close_times
    ])
    if (
        resamples != 10000
        or confidence_level != 0.9
        or not np.isfinite(deltas).all()
    ):
        raise ValueError("v17_evaluator_bootstrap_contract_invalid")
    rng = np.random.default_rng(int(seed))
    indexes = rng.integers(
        0, len(close_times), size=(resamples, len(close_times)), endpoint=False,
    )
    means = deltas[indexes].mean(axis=1)

    def summary(column: int) -> dict[str, float]:
        values = means[:, column]
        return {
            "observed_mean_delta": float(deltas[:, column].mean()),
            "two_sided_lower": float(np.quantile(values, 0.05)),
            "two_sided_upper": float(np.quantile(values, 0.95)),
            "one_sided_upper": float(np.quantile(values, 0.9)),
            "bootstrap_probability_delta_below_zero": float(np.mean(values < 0.0)),
        }

    return {
        "version": "q15-rti-v17-paired-close-window-bootstrap-v1",
        "cluster_key": "close_time",
        "close_windows": len(close_times),
        "rows": len(rows),
        "resamples": resamples,
        "confidence_level": confidence_level,
        "random_seed": int(seed),
        "same_close_assets_resampled_together": True,
        "loss_delta_direction": f"V17_MINUS_{comparator_name}",
        "brier_delta": summary(0),
        "log_loss_delta": summary(1),
    }


def _comparison(
    rows: Sequence[Mapping[str, Any]],
    candidate: Sequence[float],
    comparator: Sequence[float],
    *,
    comparator_name: str,
    seed: int,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    comparison = dict(contract["aggregate_comparison"])
    candidate_scores = score_utils.proper_scores(rows, candidate)
    comparator_scores = score_utils.proper_scores(rows, comparator)
    return {
        "candidate_scores": candidate_scores,
        "comparator_scores": comparator_scores,
        "candidate_minus_comparator_brier": (
            candidate_scores["brier_score"] - comparator_scores["brier_score"]
        ),
        "candidate_minus_comparator_log_loss": (
            candidate_scores["log_loss"] - comparator_scores["log_loss"]
        ),
        "paired_close_window_bootstrap": paired_comparator_bootstrap(
            rows,
            candidate,
            comparator,
            comparator_name=comparator_name,
            seed=seed,
            resamples=int(comparison["bootstrap_resamples"]),
            confidence_level=float(comparison["bootstrap_confidence_level"]),
        ),
    }


def _outer_folds(windows: Sequence[float]) -> list[dict[str, Any]]:
    values = tuple(float(value) for value in windows)
    output = []
    for index in range(4):
        start = 120 + index * 30
        train = values[:start]
        validation = values[start:start + 30]
        if len(validation) != 30 or max(train) >= min(validation):
            raise ValueError("v17_evaluator_outer_chronology_invalid")
        output.append({"fold": index + 1, "train": train, "validation": validation})
    return output


def evaluate_development(
    labeled_development: Sequence[Mapping[str, Any]],
    *,
    contract: Mapping[str, Any] | None = None,
    protocol: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    contract = dict(contract or load_contract())
    protocol = dict(protocol or load_protocol())
    if (
        design_fingerprint(contract) != audit_identity.EVALUATOR_CONTRACT_SHA256
        or design_fingerprint(protocol) != v17_identity.PROTOCOL_SHA256
    ):
        raise ValueError("v17_evaluator_supplied_contract_identity_invalid")
    windows = _validate_examples(labeled_development)
    configs = _architecture_configs(protocol)
    comparison = dict(contract["aggregate_comparison"])
    seeds = {
        "MARKET": int(comparison["candidate_minus_market_random_seed"]),
        "V16": int(comparison["candidate_minus_v16_random_seed"]),
        "V15": int(comparison["candidate_minus_v15_random_seed"]),
        "V14": int(comparison["candidate_minus_v14_random_seed"]),
    }
    aggregate_rows: list[Mapping[str, Any]] = []
    aggregate = {name: [] for name in ("V17", "V16", "V15", "V14", "MARKET")}
    folds = []
    for fold in _outer_folds(windows):
        train_times = set(fold["train"])
        validation_times = set(fold["validation"])
        train = [
            row for row in labeled_development
            if float(row["close_time"]) in train_times
        ]
        validation = [
            row for row in labeled_development
            if float(row["close_time"]) in validation_times
        ]
        predictions: dict[str, list[float]] = {}
        trusts: dict[str, dict[str, Any]] = {}
        ood: dict[str, int] = {}
        for name, key, names_key, _expected_names in ARCHITECTURES:
            predictions[name], trusts[name], ood[name] = _predict(
                train,
                validation,
                feature_key=key,
                feature_names_key=names_key,
                config=configs[name],
                trust_protocol=_trust_protocol(contract, name),
            )
        predictions["MARKET"] = [
            float(row["market_yes_probability"]) for row in validation
        ]
        comparisons = {
            name: _comparison(
                validation,
                predictions["V17"],
                predictions[name],
                comparator_name=name,
                seed=seeds[name],
                contract=contract,
            )
            for name in ("MARKET", "V16", "V15", "V14")
        }
        folds.append({
            "fold": int(fold["fold"]),
            "train_close_windows": len(train_times),
            "validation_close_windows": len(validation_times),
            "train_last_close_time": max(train_times),
            "validation_first_close_time": min(validation_times),
            "selected_residual_trust_factor": {
                name: float(trusts[name]["selected_factor"])
                for name in ("V17", "V16", "V15", "V14")
            },
            "out_of_distribution_rows": ood,
            "scores": {
                name: score_utils.proper_scores(validation, predictions[name])
                for name in ("V17", "V16", "V15", "V14", "MARKET")
            },
            "comparisons": comparisons,
            "candidate_not_worse_than_market": bool(
                comparisons["MARKET"]["candidate_minus_comparator_brier"] <= 0.0
                and comparisons["MARKET"]["candidate_minus_comparator_log_loss"] <= 0.0
            ),
            "candidate_not_worse_than_v16": bool(
                comparisons["V16"]["candidate_minus_comparator_brier"] <= 0.0
                and comparisons["V16"]["candidate_minus_comparator_log_loss"] <= 0.0
            ),
            "outer_validation_labels_used_for_trust_selection": False,
        })
        aggregate_rows.extend(validation)
        for name in aggregate:
            aggregate[name].extend(predictions[name])

    aggregate_comparisons = {
        name: _comparison(
            aggregate_rows,
            aggregate["V17"],
            aggregate[name],
            comparator_name=name,
            seed=seeds[name],
            contract=contract,
        )
        for name in ("MARKET", "V16", "V15", "V14")
    }
    checks: dict[str, bool] = {}
    for name, report in aggregate_comparisons.items():
        key = name.lower()
        checks[f"candidate_brier_beats_{key}"] = bool(
            report["candidate_minus_comparator_brier"] < 0.0
        )
        checks[f"candidate_log_loss_beats_{key}"] = bool(
            report["candidate_minus_comparator_log_loss"] < 0.0
        )
        bootstrap = report["paired_close_window_bootstrap"]
        checks[f"{key}_brier_bootstrap_upper_below_zero"] = bool(
            float(bootstrap["brier_delta"]["one_sided_upper"]) < 0.0
        )
        checks[f"{key}_log_loss_bootstrap_upper_below_zero"] = bool(
            float(bootstrap["log_loss_delta"]["one_sided_upper"]) < 0.0
        )
    checks.update({
        "every_fold_not_worse_vs_market": all(
            bool(fold["candidate_not_worse_than_market"]) for fold in folds
        ),
        "every_fold_not_worse_vs_v16": all(
            bool(fold["candidate_not_worse_than_v16"]) for fold in folds
        ),
        "market_brier_mean_effect_floor": bool(
            aggregate_comparisons["MARKET"]["candidate_minus_comparator_brier"]
            <= float(comparison["candidate_minus_market_brier_mean_must_be_at_most"])
        ),
        "market_log_loss_mean_effect_floor": bool(
            aggregate_comparisons["MARKET"]["candidate_minus_comparator_log_loss"]
            <= float(comparison["candidate_minus_market_log_loss_mean_must_be_at_most"])
        ),
        "v16_brier_mean_effect_floor": bool(
            aggregate_comparisons["V16"]["candidate_minus_comparator_brier"]
            <= float(comparison["candidate_minus_v16_brier_mean_must_be_at_most"])
        ),
        "v16_log_loss_mean_effect_floor": bool(
            aggregate_comparisons["V16"]["candidate_minus_comparator_log_loss"]
            <= float(comparison["candidate_minus_v16_log_loss_mean_must_be_at_most"])
        ),
    })
    passed = all(checks.values())
    prediction_rows = sorted([
        int(row["id"]),
        float(aggregate["V17"][index]),
        float(aggregate["V16"][index]),
        float(aggregate["V15"][index]),
        float(aggregate["V14"][index]),
        float(aggregate["MARKET"][index]),
    ] for index, row in enumerate(aggregate_rows))
    return {
        "evaluator_version": audit_identity.EVALUATOR_VERSION,
        "evaluator_contract_id": audit_identity.EVALUATOR_CONTRACT_ID,
        "evaluator_contract_sha256": audit_identity.EVALUATOR_CONTRACT_SHA256,
        "protocol_id": v17_identity.PROTOCOL_ID,
        "protocol_sha256": v17_identity.PROTOCOL_SHA256,
        "development_seal_sha256": audit_identity.DEVELOPMENT_SEAL_SHA256,
        "cohort": COHORT,
        "input_rows": len(labeled_development),
        "input_close_windows": len(windows),
        "walk_forward_validation_rows": len(aggregate_rows),
        "walk_forward_validation_close_windows": len({
            float(row["close_time"]) for row in aggregate_rows
        }),
        "candidate_market_v16_v15_v14_identical_rows": True,
        "same_close_assets_share_every_fold": True,
        "future_calibration_rows_used": 0,
        "future_test_rows_used": 0,
        "accuracy_is_report_only": True,
        "outcome_labels_read": True,
        "model_fit_performed": True,
        "probability_scoring_performed": True,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
        "folds": folds,
        "aggregate": {
            "scores": {
                name: score_utils.proper_scores(aggregate_rows, aggregate[name])
                for name in ("V17", "V16", "V15", "V14", "MARKET")
            },
            "comparisons": aggregate_comparisons,
        },
        "gate_checks": checks,
        "gate_met": passed,
        "failure_result": None if passed else "NO_V17_CALIBRATION_LABEL_ACCESS",
        "input_row_ids_sha256": _canonical_sha256(sorted(
            int(row["id"]) for row in labeled_development
        )),
        "input_close_times_sha256": _canonical_sha256(windows),
        "evaluation_row_ids_sha256": _canonical_sha256(sorted(
            int(row["id"]) for row in aggregate_rows
        )),
        "evaluation_predictions_sha256": _canonical_sha256(prediction_rows),
    }
