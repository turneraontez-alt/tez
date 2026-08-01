"""Fail-closed runtime bridge for locked RTI microstructure paper artifacts.

The offline freeze emits one artifact per transfer cohort.  This module can
validate and score those artifacts, but it deliberately has no notification,
order, automatic-promotion, or artifact-creation surface.  Missing artifacts
are the expected state until the preregistered freeze and one-shot test gates
have passed and an operator later installs them manually.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import threading
from typing import Any, Mapping, Sequence

from .costs import (
    KALSHI_Q15_FEE_SCHEDULE_VERSION,
    RTI_EXECUTION_COST_MODEL_VERSION,
    rti_simulated_execution,
)
from . import rti_microstructure_v11 as v11
from . import rti_microstructure_v12 as v12


COHORTS = frozenset({"BTC", "NON_BTC_TRANSFER"})
MODEL_FAMILY = "regularized_market_prior_residual_logit"
EXPECTED_TEST_STATE_VERSION = "q15-rti-untouched-test-state-v2"
DEFAULT_ARTIFACT_PATHS = {
    "BTC": Path("config/q15_rti_microstructure_v11_btc_paper.json"),
    "NON_BTC_TRANSFER": Path(
        "config/q15_rti_microstructure_v11_non_btc_paper.json"
    ),
}
ARTIFACT_ENV = {
    "BTC": "Q15_RTI_MICROSTRUCTURE_V11_BTC_ARTIFACT",
    "NON_BTC_TRANSFER": "Q15_RTI_MICROSTRUCTURE_V11_NON_BTC_ARTIFACT",
}
V12_DEFAULT_ARTIFACT_PATHS = {
    "BTC": Path("config/q15_rti_microstructure_v12_btc_paper.json"),
    "NON_BTC_TRANSFER": Path(
        "config/q15_rti_microstructure_v12_non_btc_paper.json"
    ),
}
V12_ARTIFACT_ENV = {
    "BTC": "Q15_RTI_MICROSTRUCTURE_V12_BTC_ARTIFACT",
    "NON_BTC_TRANSFER": "Q15_RTI_MICROSTRUCTURE_V12_NON_BTC_ARTIFACT",
}
_ARTIFACT_PATHS_BY_DESIGN = {
    v11.DESIGN_ID: DEFAULT_ARTIFACT_PATHS,
    v12.DESIGN_ID: V12_DEFAULT_ARTIFACT_PATHS,
}
_ARTIFACT_ENV_BY_DESIGN = {
    v11.DESIGN_ID: ARTIFACT_ENV,
    v12.DESIGN_ID: V12_ARTIFACT_ENV,
}
EXPECTED_TRAINING_CONFIG = {
    "model_l2": 150.0,
    "model_learning_rate": 0.03,
    "model_iterations": 2000,
    "standardization_min_std": 1e-8,
    "standardization_z_clip": 5.0,
    "out_of_distribution_max_abs_z": 8.0,
    "residual_logit_scale": 0.2,
    "hyperparameter_search_performed": False,
    "missing_numeric_value": 0.0,
    "explicit_missing_indicators": True,
    "window_equal_weighting": True,
}
EXPECTED_ENTRY_POLICY = {
    "minimum_expected_value_cents_after_costs": 3.0,
    "maximum_ask_cents": 62.0,
    "maximum_spread_cents": 1.5,
    "minimum_displayed_depth_contracts": 10.0,
    "simulation_contracts": 10,
    "official_kalshi_fees": True,
    "slippage_cents_per_contract": 2.0,
    "fee_schedule_version": KALSHI_Q15_FEE_SCHEDULE_VERSION,
    "execution_cost_model_version": RTI_EXECUTION_COST_MODEL_VERSION,
}
EXPECTED_WINDOW_WEIGHTING = {
    "BTC": {
        "version": "q15-close-window-equal-weight-v1",
        "rows": 90,
        "close_windows": 90,
        "minimum_rows_per_close_window": 1,
        "maximum_rows_per_close_window": 1,
        "total_sample_weight": 90.0,
        "minimum_close_window_weight": 1.0,
        "maximum_close_window_weight": 1.0,
        "every_close_window_total_weight_one": True,
    },
    "NON_BTC_TRANSFER": {
        "version": "q15-close-window-equal-weight-v1",
        "rows": 216,
        "close_windows": 36,
        "minimum_rows_per_close_window": 6,
        "maximum_rows_per_close_window": 6,
        "total_sample_weight": 36.0,
        "minimum_close_window_weight": 1.0,
        "maximum_close_window_weight": 1.0,
        "every_close_window_total_weight_one": True,
    },
}


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _logit(probability: float) -> float:
    bounded = _clip(probability, 1e-6, 1.0 - 1e-6)
    return math.log(bounded / (1.0 - bounded))


def _sigmoid(value: float) -> float:
    if value >= 0.0:
        z = math.exp(-min(value, 709.0))
        return 1.0 / (1.0 + z)
    z = math.exp(max(value, -709.0))
    return z / (1.0 + z)


def artifact_fingerprint(artifact: Mapping[str, Any]) -> str:
    payload = dict(artifact)
    payload.pop("artifact_sha256", None)
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _runtime_version(feature_runtime: Any) -> str:
    if feature_runtime is v11:
        return "v11"
    if feature_runtime is v12:
        return "v12"
    raise ValueError("unsupported_microstructure_runtime")


def _error(feature_runtime: Any, suffix: str) -> str:
    return f"{_runtime_version(feature_runtime)}_{suffix}"


def artifact_path(cohort: str, *, feature_runtime: Any = v11) -> Path:
    cohort_key = str(cohort or "").upper()
    if cohort_key not in COHORTS:
        raise ValueError(_error(feature_runtime, "runtime_cohort_invalid"))
    paths = _ARTIFACT_PATHS_BY_DESIGN.get(feature_runtime.DESIGN_ID)
    env_names = _ARTIFACT_ENV_BY_DESIGN.get(feature_runtime.DESIGN_ID)
    if paths is None or env_names is None:
        raise ValueError("unsupported_microstructure_runtime")
    return Path(
        os.environ.get(env_names[cohort_key]) or paths[cohort_key]
    )


def _exact_mapping(
    actual: Any, expected: Mapping[str, Any], error: str,
) -> None:
    if not isinstance(actual, Mapping) or dict(actual) != dict(expected):
        raise ValueError(error)


def validate_artifact(
    artifact: Mapping[str, Any], expected_cohort: str,
    *, feature_runtime: Any = v11,
) -> None:
    cohort = str(expected_cohort or "").upper()
    if cohort not in COHORTS or artifact.get("cohort") != cohort:
        raise ValueError(_error(feature_runtime, "artifact_cohort_mismatch"))
    data_sha = str(artifact.get("data_sha256") or "")
    if len(data_sha) != 64 or any(
        character not in "0123456789abcdef" for character in data_sha
    ):
        raise ValueError(_error(feature_runtime, "artifact_data_sha_invalid"))
    for key in ("test_state_sha256", "test_metrics_sha256"):
        value = str(artifact.get(key) or "")
        if len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise ValueError(_error(feature_runtime, f"artifact_{key}_invalid"))
    if artifact.get("test_state_version") != EXPECTED_TEST_STATE_VERSION:
        raise ValueError(_error(
            feature_runtime, "artifact_test_state_version_mismatch"
        ))
    if artifact.get("untouched_test_status") != (
        "PASSED_UNTOUCHED_TEST_PAPER_ARTIFACT_ONLY"
    ):
        raise ValueError(_error(
            feature_runtime, "artifact_untouched_test_status_mismatch"
        ))
    expected_model_version = (
        f"rti-microstructure-paper-{feature_runtime.DESIGN_ID}-{data_sha[:12]}"
    )
    if artifact.get("model_version") != expected_model_version:
        raise ValueError(_error(feature_runtime, "artifact_model_version_mismatch"))
    if artifact.get("model_family") != MODEL_FAMILY:
        raise ValueError(_error(feature_runtime, "artifact_model_family_mismatch"))
    if artifact.get("feature_schema_version") != feature_runtime.FEATURE_SCHEMA_VERSION:
        raise ValueError(_error(feature_runtime, "artifact_feature_schema_mismatch"))
    if tuple(artifact.get("feature_names") or ()) != feature_runtime.FEATURE_NAMES:
        raise ValueError(_error(feature_runtime, "artifact_feature_names_mismatch"))
    if artifact.get("design_id") != feature_runtime.DESIGN_ID:
        raise ValueError(_error(feature_runtime, "artifact_design_id_mismatch"))
    if artifact.get("design_sha256") != feature_runtime.DESIGN_SHA256:
        raise ValueError(_error(feature_runtime, "artifact_design_sha_mismatch"))
    if artifact.get("walk_forward_protocol_id") != (
        feature_runtime.EVALUATION_PROTOCOL_ID
    ):
        raise ValueError(_error(feature_runtime, "artifact_protocol_id_mismatch"))
    if artifact.get("walk_forward_protocol_sha256") != (
        feature_runtime.EVALUATION_PROTOCOL_SHA256
    ):
        raise ValueError(_error(feature_runtime, "artifact_protocol_sha_mismatch"))
    if feature_runtime is v11:
        if artifact.get("reporting_protocol_id") != v11.REPORTING_PROTOCOL_ID:
            raise ValueError(_error(
                feature_runtime, "artifact_reporting_protocol_id_mismatch"
            ))
        if artifact.get("reporting_protocol_sha256") != (
            v11.REPORTING_PROTOCOL_SHA256
        ):
            raise ValueError(_error(
                feature_runtime, "artifact_reporting_protocol_sha_mismatch"
            ))
        if artifact.get("fixed_subgroup_reporting_required") is not True:
            raise ValueError(_error(
                feature_runtime, "artifact_reporting_protocol_guard_missing"
            ))
        if artifact.get("calibration_reporting_protocol_id") != (
            v11.CALIBRATION_REPORTING_PROTOCOL_ID
        ):
            raise ValueError(_error(
                feature_runtime,
                "artifact_calibration_reporting_protocol_id_mismatch",
            ))
        if artifact.get("calibration_reporting_protocol_sha256") != (
            v11.CALIBRATION_REPORTING_PROTOCOL_SHA256
        ):
            raise ValueError(_error(
                feature_runtime,
                "artifact_calibration_reporting_protocol_sha_mismatch",
            ))
        if artifact.get("fixed_calibration_reporting_required") is not True:
            raise ValueError(_error(
                feature_runtime,
                "artifact_calibration_reporting_protocol_guard_missing",
            ))
        if artifact.get("selective_value_curve_protocol_id") != (
            v11.SELECTIVE_VALUE_CURVE_PROTOCOL_ID
        ):
            raise ValueError(_error(
                feature_runtime,
                "artifact_selective_value_curve_protocol_id_mismatch",
            ))
        if artifact.get("selective_value_curve_protocol_sha256") != (
            v11.SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256
        ):
            raise ValueError(_error(
                feature_runtime,
                "artifact_selective_value_curve_protocol_sha_mismatch",
            ))
        if artifact.get("fixed_selective_value_curve_required") is not True:
            raise ValueError(_error(
                feature_runtime,
                "artifact_selective_value_curve_protocol_guard_missing",
            ))
    required_true = (
        "paper_only",
        "same_close_fold_isolation",
        "window_equal_weighting_verified",
        "optimizer_numerical_integrity_verified",
        "walk_forward_required_before_untouched_test",
        "walk_forward_gate_passed",
        "test_scored_once",
    )
    required_false = (
        "notification_eligible",
        "real_trading_allowed",
        "automatic_refit",
        "automatic_promotion",
        "historical_credit_allowed",
    )
    if any(artifact.get(key) is not True for key in required_true):
        raise ValueError(_error(
            feature_runtime, "artifact_required_true_guard_missing"
        ))
    if any(artifact.get(key) is not False for key in required_false):
        raise ValueError(_error(
            feature_runtime, "artifact_required_false_guard_missing"
        ))
    boundary = _num(artifact.get("prospective_after_close_time"))
    if boundary is None or boundary < feature_runtime.FIRST_ELIGIBLE_CLOSE_TIME:
        raise ValueError(_error(
            feature_runtime, "artifact_prospective_boundary_invalid"
        ))
    _exact_mapping(
        artifact.get("fixed_training_config"),
        EXPECTED_TRAINING_CONFIG,
        _error(feature_runtime, "artifact_training_config_mismatch"),
    )
    _exact_mapping(
        artifact.get("entry_policy"),
        EXPECTED_ENTRY_POLICY,
        _error(feature_runtime, "artifact_entry_policy_mismatch"),
    )
    model = artifact.get("model")
    if not isinstance(model, Mapping):
        raise ValueError(_error(feature_runtime, "artifact_model_missing"))
    _exact_mapping(
        model.get("window_weighting"),
        EXPECTED_WINDOW_WEIGHTING[cohort],
        _error(feature_runtime, "artifact_window_weighting_mismatch"),
    )
    optimizer = model.get("optimizer")
    if not isinstance(optimizer, Mapping):
        raise ValueError(_error(
            feature_runtime, "artifact_optimizer_diagnostics_missing"
        ))
    if (
        optimizer.get("version")
        != "q15-fixed-residual-gradient-descent-audit-v1"
        or optimizer.get("iterations")
        != int(EXPECTED_TRAINING_CONFIG["model_iterations"])
        or _num(optimizer.get("learning_rate"))
        != float(EXPECTED_TRAINING_CONFIG["model_learning_rate"])
        or _num(optimizer.get("model_l2"))
        != float(EXPECTED_TRAINING_CONFIG["model_l2"])
        or _num(optimizer.get("residual_logit_scale"))
        != float(EXPECTED_TRAINING_CONFIG["residual_logit_scale"])
        or optimizer.get("all_values_finite") is not True
        or optimizer.get("final_objective_not_worse") is not True
        or optimizer.get("numerical_integrity_verified") is not True
    ):
        raise ValueError(_error(
            feature_runtime, "artifact_optimizer_diagnostics_mismatch"
        ))
    initial_objective = _num(
        optimizer.get("initial_regularized_objective")
    )
    final_objective = _num(
        optimizer.get("final_regularized_objective")
    )
    improvement = _num(
        optimizer.get("regularized_objective_improvement")
    )
    final_gradient = _num(optimizer.get("final_max_abs_gradient"))
    if (
        initial_objective is None
        or final_objective is None
        or improvement is None
        or final_gradient is None
        or initial_objective < 0.0
        or final_objective < 0.0
        or final_gradient < 0.0
        or final_objective > initial_objective + 1e-12
        or improvement < -1e-12
        or abs(improvement - (initial_objective - final_objective)) > 1e-9
    ):
        raise ValueError(_error(
            feature_runtime, "artifact_optimizer_numerical_integrity_invalid"
        ))
    width = len(feature_runtime.FEATURE_NAMES)
    arrays: dict[str, list[float]] = {}
    for key in ("means", "stds", "weights"):
        raw = model.get(key)
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise ValueError(_error(
                feature_runtime, f"artifact_model_{key}_missing"
            ))
        values = [float(value) for value in raw]
        if len(values) != width or not all(math.isfinite(value) for value in values):
            raise ValueError(_error(
                feature_runtime, f"artifact_model_{key}_invalid"
            ))
        arrays[key] = values
    bias = _num(model.get("bias"))
    if bias is None or any(value < 0.0 for value in arrays["stds"]):
        raise ValueError(_error(
            feature_runtime, "artifact_model_numerical_invalid"
        ))
    min_std = float(EXPECTED_TRAINING_CONFIG["standardization_min_std"])
    if any(
        std <= min_std and abs(weight) > 1e-12
        for std, weight in zip(arrays["stds"], arrays["weights"])
    ):
        raise ValueError(_error(
            feature_runtime, "artifact_inactive_feature_has_weight"
        ))
    expected_sha = str(artifact.get("artifact_sha256") or "")
    if not expected_sha or expected_sha != artifact_fingerprint(artifact):
        raise ValueError(_error(feature_runtime, "artifact_fingerprint_mismatch"))


_cache_lock = threading.Lock()
_artifact_cache: dict[tuple[str, str, str], tuple[int, Mapping[str, Any]]] = {}


def reset_artifact_cache() -> None:
    with _cache_lock:
        _artifact_cache.clear()


def load_artifact(
    cohort: str, path: str | Path | None = None,
    *, feature_runtime: Any = v11,
) -> Mapping[str, Any]:
    cohort_key = str(cohort or "").upper()
    target = (
        Path(path) if path is not None
        else artifact_path(cohort_key, feature_runtime=feature_runtime)
    )
    resolved = str(target.resolve())
    cache_key = (feature_runtime.DESIGN_ID, resolved, cohort_key)
    stat = target.stat()
    with _cache_lock:
        cached = _artifact_cache.get(cache_key)
        if cached is not None and cached[0] == stat.st_mtime_ns:
            return cached[1]
    decoded = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(decoded, Mapping):
        raise ValueError(_error(feature_runtime, "artifact_root_not_object"))
    validate_artifact(
        decoded, cohort_key, feature_runtime=feature_runtime
    )
    with _cache_lock:
        _artifact_cache[cache_key] = (stat.st_mtime_ns, decoded)
    return decoded


def _entry_recommendation(
    features: Mapping[str, Any], probability_yes: float,
) -> dict[str, Any] | None:
    policy = EXPECTED_ENTRY_POLICY
    spread = float(features["spread_cents"])
    if spread > float(policy["maximum_spread_cents"]):
        return None
    candidates = []
    for side, probability, ask_key, depth_key, available_key in (
        ("YES", probability_yes, "yes_ask_cents", "yes_depth_contracts", "yes_depth_available"),
        ("NO", 1.0 - probability_yes, "no_ask_cents", "no_depth_contracts", "no_depth_available"),
    ):
        if not bool(features[available_key]):
            continue
        ask = float(features[ask_key])
        depth = float(features[depth_key])
        if ask > float(policy["maximum_ask_cents"]):
            continue
        if depth < float(policy["minimum_displayed_depth_contracts"]):
            continue
        execution = rti_simulated_execution(
            ask,
            int(policy["simulation_contracts"]),
            float(policy["slippage_cents_per_contract"]),
        )
        if execution is None:
            continue
        fill = float(execution["simulated_fill_cents"])
        fee = float(execution["fee_cents_per_contract"])
        ev = probability * 100.0 - fill - fee
        if ev >= float(policy["minimum_expected_value_cents_after_costs"]):
            candidates.append((ev, side, ask, fill, fee, probability, depth))
    if not candidates:
        return None
    ev, side, ask, fill, fee, probability, depth = max(candidates)
    return {
        "side": side,
        "win_probability": probability,
        "ask_cents": ask,
        "displayed_depth_contracts": depth,
        "simulated_fill_cents": fill,
        "fee_cents_per_contract": fee,
        "expected_value_cents_per_contract": ev,
        "simulation_contracts": int(policy["simulation_contracts"]),
        "slippage_cents_per_contract": float(
            policy["slippage_cents_per_contract"]
        ),
        "fee_schedule_version": KALSHI_Q15_FEE_SCHEDULE_VERSION,
        "execution_cost_model_version": RTI_EXECUTION_COST_MODEL_VERSION,
        "paper_only": True,
        "notification_eligible": False,
    }


def runtime_prediction(
    row: Mapping[str, Any], path: str | Path | None = None,
    *, feature_runtime: Any = v11,
) -> dict[str, Any]:
    features = feature_runtime.feature_vector(row)
    if not features.get("available"):
        return {**features, "paper_only": True, "notification_eligible": False}
    cohort = str(features["cohort"])
    try:
        artifact = load_artifact(
            cohort, path, feature_runtime=feature_runtime
        )
    except FileNotFoundError:
        return {
            **features,
            "available": False,
            "error": _error(feature_runtime, "artifact_missing"),
            "paper_only": True,
            "notification_eligible": False,
        }
    except Exception as exc:
        return {
            **features,
            "available": False,
            "error": (
                f"{_error(feature_runtime, 'artifact_invalid')}:"
                f"{type(exc).__name__}:{exc}"
            ),
            "paper_only": True,
            "notification_eligible": False,
        }
    close_time = _num(row.get("close_time"))
    boundary = float(artifact["prospective_after_close_time"])
    prospective = close_time is not None and close_time > boundary
    model = artifact["model"]
    config = EXPECTED_TRAINING_CONFIG
    values = [float(value) for value in features["features"]]
    means = [float(value) for value in model["means"]]
    stds = [float(value) for value in model["stds"]]
    weights = [float(value) for value in model["weights"]]
    min_std = float(config["standardization_min_std"])
    preclip = [
        0.0 if std <= min_std else (value - mean) / std
        for value, mean, std in zip(values, means, stds)
    ]
    max_abs_z = max((abs(value) for value in preclip), default=0.0)
    out_of_distribution = max_abs_z > float(
        config["out_of_distribution_max_abs_z"]
    )
    market = float(features["market_yes_probability"])
    if out_of_distribution:
        probability = market
    else:
        z_clip = float(config["standardization_z_clip"])
        standardized = [_clip(value, -z_clip, z_clip) for value in preclip]
        residual = float(model["bias"]) + sum(
            weight * value for weight, value in zip(weights, standardized)
        )
        probability = _sigmoid(
            _logit(market)
            + float(config["residual_logit_scale"]) * residual
        )
    probability = _clip(probability, 0.01, 0.99)
    recommendation = (
        None
        if not prospective or out_of_distribution
        else _entry_recommendation(features, probability)
    )
    return {
        **features,
        "available": True,
        "prospective": prospective,
        "prospective_after_close_time": boundary,
        "model_version": artifact.get("model_version"),
        "artifact_sha256": artifact.get("artifact_sha256"),
        "test_state_version": artifact.get("test_state_version"),
        "test_state_sha256": artifact.get("test_state_sha256"),
        "test_metrics_sha256": artifact.get("test_metrics_sha256"),
        "untouched_test_status": artifact.get("untouched_test_status"),
        "design_id": feature_runtime.DESIGN_ID,
        "design_sha256": feature_runtime.DESIGN_SHA256,
        "walk_forward_protocol_id": feature_runtime.EVALUATION_PROTOCOL_ID,
        "walk_forward_protocol_sha256": (
            feature_runtime.EVALUATION_PROTOCOL_SHA256
        ),
        **(
            {
                "reporting_protocol_id": v11.REPORTING_PROTOCOL_ID,
                "reporting_protocol_sha256": v11.REPORTING_PROTOCOL_SHA256,
                "fixed_subgroup_reporting_required": True,
                "calibration_reporting_protocol_id": (
                    v11.CALIBRATION_REPORTING_PROTOCOL_ID
                ),
                "calibration_reporting_protocol_sha256": (
                    v11.CALIBRATION_REPORTING_PROTOCOL_SHA256
                ),
                "fixed_calibration_reporting_required": True,
                "selective_value_curve_protocol_id": (
                    v11.SELECTIVE_VALUE_CURVE_PROTOCOL_ID
                ),
                "selective_value_curve_protocol_sha256": (
                    v11.SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256
                ),
                "fixed_selective_value_curve_required": True,
            }
            if feature_runtime is v11
            else {}
        ),
        "yes_probability": probability,
        "market_yes_probability": market,
        "max_abs_z_preclip": max_abs_z,
        "out_of_distribution": out_of_distribution,
        "market_fallback_used": out_of_distribution,
        "entry_recommendation": recommendation,
        "paper_only": True,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
        "manual_activation_required": True,
        "historical_credit_allowed": False,
    }


def artifact_health(
    cohort: str, path: str | Path | None = None,
    *, feature_runtime: Any = v11,
) -> dict[str, Any]:
    cohort_key = str(cohort or "").upper()
    target = (
        Path(path) if path is not None
        else artifact_path(cohort_key, feature_runtime=feature_runtime)
    )
    try:
        artifact = load_artifact(
            cohort_key, target, feature_runtime=feature_runtime
        )
        return {
            "available": True,
            "path": str(target),
            "cohort": cohort_key,
            "model_version": artifact.get("model_version"),
            "artifact_sha256": artifact.get("artifact_sha256"),
            "test_state_version": artifact.get("test_state_version"),
            "test_state_sha256": artifact.get("test_state_sha256"),
            "test_metrics_sha256": artifact.get("test_metrics_sha256"),
            "untouched_test_status": artifact.get("untouched_test_status"),
            "prospective_after_close_time": artifact.get(
                "prospective_after_close_time"
            ),
            "design_id": feature_runtime.DESIGN_ID,
            "design_sha256": feature_runtime.DESIGN_SHA256,
            "walk_forward_protocol_id": feature_runtime.EVALUATION_PROTOCOL_ID,
            "walk_forward_protocol_sha256": (
                feature_runtime.EVALUATION_PROTOCOL_SHA256
            ),
            **(
                {
                    "reporting_protocol_id": v11.REPORTING_PROTOCOL_ID,
                    "reporting_protocol_sha256": v11.REPORTING_PROTOCOL_SHA256,
                    "fixed_subgroup_reporting_required": True,
                    "calibration_reporting_protocol_id": (
                        v11.CALIBRATION_REPORTING_PROTOCOL_ID
                    ),
                    "calibration_reporting_protocol_sha256": (
                        v11.CALIBRATION_REPORTING_PROTOCOL_SHA256
                    ),
                    "fixed_calibration_reporting_required": True,
                    "selective_value_curve_protocol_id": (
                        v11.SELECTIVE_VALUE_CURVE_PROTOCOL_ID
                    ),
                    "selective_value_curve_protocol_sha256": (
                        v11.SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256
                    ),
                    "fixed_selective_value_curve_required": True,
                }
                if feature_runtime is v11
                else {}
            ),
            "paper_only": True,
            "notification_eligible": False,
            "automatic_promotion": False,
            "real_trading_allowed": False,
            "manual_activation_required": True,
            "runtime_prediction_eligible": True,
            "status": "ARTIFACT_READY_DORMANT_MANUAL_ACTIVATION_REQUIRED",
        }
    except FileNotFoundError:
        error = "artifact_missing"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    return {
        "available": False,
        "path": str(target),
        "cohort": cohort_key,
        "error": error,
        "design_id": feature_runtime.DESIGN_ID,
        "design_sha256": feature_runtime.DESIGN_SHA256,
        "walk_forward_protocol_id": feature_runtime.EVALUATION_PROTOCOL_ID,
        "walk_forward_protocol_sha256": (
            feature_runtime.EVALUATION_PROTOCOL_SHA256
        ),
        **(
            {
                "reporting_protocol_id": v11.REPORTING_PROTOCOL_ID,
                "reporting_protocol_sha256": v11.REPORTING_PROTOCOL_SHA256,
                "fixed_subgroup_reporting_required": True,
                "calibration_reporting_protocol_id": (
                    v11.CALIBRATION_REPORTING_PROTOCOL_ID
                ),
                "calibration_reporting_protocol_sha256": (
                    v11.CALIBRATION_REPORTING_PROTOCOL_SHA256
                ),
                "fixed_calibration_reporting_required": True,
                "selective_value_curve_protocol_id": (
                    v11.SELECTIVE_VALUE_CURVE_PROTOCOL_ID
                ),
                "selective_value_curve_protocol_sha256": (
                    v11.SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256
                ),
                "fixed_selective_value_curve_required": True,
            }
            if feature_runtime is v11
            else {}
        ),
        "paper_only": True,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
        "manual_activation_required": True,
        "runtime_prediction_eligible": False,
        "status": "WAITING_FOR_LOCKED_ARTIFACT",
    }
