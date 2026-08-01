from __future__ import annotations

import json
from pathlib import Path

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_runtime as runtime
from q15_upgrade.strategy_bots import rti_microstructure_v11 as v11
from q15_upgrade.strategy_bots import rti_microstructure_v12 as v12
from q15_upgrade.strategy_bots import rti_microstructure_v12_runtime as v12_runtime


def _features(
    *, cohort: str = "BTC", first_value: float = 0.0,
    feature_runtime=v11,
):
    values = [0.0] * len(feature_runtime.FEATURE_NAMES)
    values[0] = first_value
    asset = "BTC" if cohort == "BTC" else "ETH"
    return {
        "available": True,
        "asset": asset,
        "cohort": cohort,
        "feature_names": list(feature_runtime.FEATURE_NAMES),
        "features": values,
        "market_yes_probability": 0.55,
        "yes_ask_cents": 44.0,
        "no_ask_cents": 57.0,
        "yes_depth_contracts": 20.0,
        "no_depth_contracts": 20.0,
        "yes_depth_available": True,
        "no_depth_available": True,
        "spread_cents": 1.0,
    }


def _artifact(
    *, cohort: str = "BTC", means=None, stds=None, weights=None,
    feature_runtime=v11, runtime_module=runtime,
):
    width = len(feature_runtime.FEATURE_NAMES)
    data_sha256 = "a" * 64
    payload = {
        "model_version": (
            f"rti-microstructure-paper-{feature_runtime.DESIGN_ID}-{data_sha256[:12]}"
        ),
        "model_family": runtime_module.MODEL_FAMILY,
        "feature_schema_version": feature_runtime.FEATURE_SCHEMA_VERSION,
        "feature_names": list(feature_runtime.FEATURE_NAMES),
        "design_id": feature_runtime.DESIGN_ID,
        "design_sha256": feature_runtime.DESIGN_SHA256,
        "created_at": "2026-07-22T05:00:00Z",
        "data_sha256": data_sha256,
        "test_state_version": runtime_module.EXPECTED_TEST_STATE_VERSION,
        "test_state_sha256": "b" * 64,
        "test_metrics_sha256": "c" * 64,
        "untouched_test_status": (
            "PASSED_UNTOUCHED_TEST_PAPER_ARTIFACT_ONLY"
        ),
        "cohort": cohort,
        "paper_only": True,
        "notification_eligible": False,
        "real_trading_allowed": False,
        "automatic_refit": False,
        "automatic_promotion": False,
        "historical_credit_allowed": False,
        "prospective_after_close_time": feature_runtime.FIRST_ELIGIBLE_CLOSE_TIME,
        "same_close_fold_isolation": True,
        "window_equal_weighting_verified": True,
        "optimizer_numerical_integrity_verified": True,
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
        "walk_forward_required_before_untouched_test": True,
        "walk_forward_gate_passed": True,
        "test_scored_once": True,
        "model": {
            "means": list(means if means is not None else [0.0] * width),
            "stds": list(stds if stds is not None else [1.0] * width),
            "weights": list(weights if weights is not None else [0.0] * width),
            "bias": 0.0,
            "window_weighting": dict(
                runtime_module.EXPECTED_WINDOW_WEIGHTING[cohort]
            ),
            "optimizer": {
                "version": "q15-fixed-residual-gradient-descent-audit-v1",
                "iterations": 2000,
                "learning_rate": 0.03,
                "model_l2": 150.0,
                "residual_logit_scale": 0.2,
                "initial_regularized_objective": 0.7,
                "final_regularized_objective": 0.6,
                "regularized_objective_improvement": 0.1,
                "final_max_abs_gradient": 0.01,
                "all_values_finite": True,
                "final_objective_not_worse": True,
                "numerical_integrity_verified": True,
            },
            "inactive_near_zero_variance_features": [],
        },
        "fixed_training_config": dict(runtime_module.EXPECTED_TRAINING_CONFIG),
        "entry_policy": dict(runtime_module.EXPECTED_ENTRY_POLICY),
    }
    payload["artifact_sha256"] = runtime_module.artifact_fingerprint(payload)
    return payload


def _write(path: Path, artifact):
    path.write_text(json.dumps(artifact), encoding="utf-8")
    runtime.reset_artifact_cache()


def test_valid_locked_artifact_scores_only_prospective_paper_rows(
    monkeypatch, tmp_path: Path,
):
    feature_payload = _features()
    monkeypatch.setattr(runtime.v11, "feature_vector", lambda row: feature_payload)
    path = tmp_path / "btc.json"
    _write(path, _artifact())
    result = runtime.runtime_prediction(
        {"close_time": v11.FIRST_ELIGIBLE_CLOSE_TIME + 900.0}, path,
    )
    assert result["available"] is True
    assert result["prospective"] is True
    assert result["test_state_version"] == runtime.EXPECTED_TEST_STATE_VERSION
    assert result["test_state_sha256"] == "b" * 64
    assert result["test_metrics_sha256"] == "c" * 64
    assert result["untouched_test_status"] == (
        "PASSED_UNTOUCHED_TEST_PAPER_ARTIFACT_ONLY"
    )
    assert result["yes_probability"] == pytest.approx(0.55)
    assert result["entry_recommendation"]["side"] == "YES"
    assert result["entry_recommendation"]["simulated_fill_cents"] == 46.0
    assert result["entry_recommendation"]["paper_only"] is True
    assert result["entry_recommendation"]["notification_eligible"] is False
    assert result["manual_activation_required"] is True
    assert result["real_trading_allowed"] is False
    assert result["notification_eligible"] is False

    boundary = runtime.runtime_prediction(
        {"close_time": v11.FIRST_ELIGIBLE_CLOSE_TIME}, path,
    )
    assert boundary["available"] is True
    assert boundary["prospective"] is False
    assert boundary["entry_recommendation"] is None


def test_ood_row_falls_back_to_market_and_cannot_recommend(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setattr(
        runtime.v11, "feature_vector", lambda row: _features(first_value=100.0),
    )
    path = tmp_path / "btc.json"
    _write(path, _artifact())
    result = runtime.runtime_prediction(
        {"close_time": v11.FIRST_ELIGIBLE_CLOSE_TIME + 900.0}, path,
    )
    assert result["available"] is True
    assert result["out_of_distribution"] is True
    assert result["market_fallback_used"] is True
    assert result["yes_probability"] == pytest.approx(0.55)
    assert result["entry_recommendation"] is None


@pytest.mark.parametrize(
    ("mutate", "error"),
    (
        (
            lambda artifact: artifact.__setitem__(
                "walk_forward_protocol_sha256", "0" * 64,
            ),
            "v11_artifact_protocol_sha_mismatch",
        ),
        (
            lambda artifact: artifact.__setitem__(
                "reporting_protocol_sha256", "0" * 64,
            ),
            "v11_artifact_reporting_protocol_sha_mismatch",
        ),
        (
            lambda artifact: artifact.__setitem__(
                "fixed_subgroup_reporting_required", False,
            ),
            "v11_artifact_reporting_protocol_guard_missing",
        ),
        (
            lambda artifact: artifact.__setitem__(
                "calibration_reporting_protocol_sha256", "0" * 64,
            ),
            "v11_artifact_calibration_reporting_protocol_sha_mismatch",
        ),
        (
            lambda artifact: artifact.__setitem__(
                "fixed_calibration_reporting_required", False,
            ),
            "v11_artifact_calibration_reporting_protocol_guard_missing",
        ),
        (
            lambda artifact: artifact.__setitem__(
                "selective_value_curve_protocol_sha256", "0" * 64,
            ),
            "v11_artifact_selective_value_curve_protocol_sha_mismatch",
        ),
        (
            lambda artifact: artifact.__setitem__(
                "fixed_selective_value_curve_required", False,
            ),
            "v11_artifact_selective_value_curve_protocol_guard_missing",
        ),
        (
            lambda artifact: artifact["entry_policy"].__setitem__(
                "execution_cost_model_version", "old",
            ),
            "v11_artifact_entry_policy_mismatch",
        ),
        (
            lambda artifact: artifact.__setitem__("notification_eligible", True),
            "v11_artifact_required_false_guard_missing",
        ),
        (
            lambda artifact: artifact.__setitem__("walk_forward_gate_passed", False),
            "v11_artifact_required_true_guard_missing",
        ),
        (
            lambda artifact: artifact.__setitem__(
                "walk_forward_required_before_untouched_test", False,
            ),
            "v11_artifact_required_true_guard_missing",
        ),
        (
            lambda artifact: artifact.__setitem__(
                "model_version",
                f"rti-microstructure-paper-{v11.DESIGN_ID}-wrongdatahash",
            ),
            "v11_artifact_model_version_mismatch",
        ),
        (
            lambda artifact: artifact["model"]["window_weighting"].__setitem__(
                "total_sample_weight", 216.0,
            ),
            "v11_artifact_window_weighting_mismatch",
        ),
        (
            lambda artifact: artifact["model"]["optimizer"].__setitem__(
                "final_regularized_objective", 0.8,
            ),
            "v11_artifact_optimizer_numerical_integrity_invalid",
        ),
        (
            lambda artifact: artifact.__setitem__(
                "optimizer_numerical_integrity_verified", False,
            ),
            "v11_artifact_required_true_guard_missing",
        ),
        (
            lambda artifact: artifact.__setitem__(
                "test_state_sha256", "not-a-hash",
            ),
            "v11_artifact_test_state_sha256_invalid",
        ),
        (
            lambda artifact: artifact.__setitem__(
                "untouched_test_status", "REJECTED_ON_UNTOUCHED_TEST",
            ),
            "v11_artifact_untouched_test_status_mismatch",
        ),
    ),
)
def test_artifact_lineage_and_safety_tampering_fails_closed(mutate, error):
    artifact = _artifact()
    mutate(artifact)
    artifact["artifact_sha256"] = runtime.artifact_fingerprint(artifact)
    with pytest.raises(ValueError, match=error):
        runtime.validate_artifact(artifact, "BTC")


def test_artifact_fingerprint_and_inactive_feature_weight_are_required():
    artifact = _artifact()
    artifact["artifact_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="v11_artifact_fingerprint_mismatch"):
        runtime.validate_artifact(artifact, "BTC")

    stds = [1.0] * len(v11.FEATURE_NAMES)
    weights = [0.0] * len(v11.FEATURE_NAMES)
    stds[3] = 0.0
    weights[3] = 0.1
    artifact = _artifact(stds=stds, weights=weights)
    with pytest.raises(ValueError, match="v11_artifact_inactive_feature_has_weight"):
        runtime.validate_artifact(artifact, "BTC")


def test_cross_cohort_artifact_and_missing_artifact_are_unavailable(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setattr(
        runtime.v11,
        "feature_vector",
        lambda row: _features(cohort="NON_BTC_TRANSFER"),
    )
    btc_path = tmp_path / "btc.json"
    _write(btc_path, _artifact(cohort="BTC"))
    result = runtime.runtime_prediction(
        {"close_time": v11.FIRST_ELIGIBLE_CLOSE_TIME + 900.0}, btc_path,
    )
    assert result["available"] is False
    assert "v11_artifact_cohort_mismatch" in result["error"]
    assert result["notification_eligible"] is False

    missing = runtime.artifact_health("BTC", tmp_path / "missing.json")
    assert missing["available"] is False
    assert missing["status"] == "WAITING_FOR_LOCKED_ARTIFACT"
    assert missing["runtime_prediction_eligible"] is False
    assert missing["notification_eligible"] is False


def test_artifact_cache_cannot_reuse_one_path_across_cohorts(tmp_path: Path):
    path = tmp_path / "one-cohort-only.json"
    _write(path, _artifact(cohort="BTC"))
    assert runtime.load_artifact("BTC", path)["cohort"] == "BTC"
    with pytest.raises(ValueError, match="v11_artifact_cohort_mismatch"):
        runtime.load_artifact("NON_BTC_TRANSFER", path)


def test_valid_artifact_health_is_dormant_and_never_promotion_eligible(
    tmp_path: Path,
):
    path = tmp_path / "btc.json"
    artifact = _artifact()
    _write(path, artifact)
    health = runtime.artifact_health("BTC", path)
    assert health["available"] is True
    assert health["artifact_sha256"] == artifact["artifact_sha256"]
    assert health["test_state_sha256"] == artifact["test_state_sha256"]
    assert health["test_metrics_sha256"] == artifact["test_metrics_sha256"]
    assert health["runtime_prediction_eligible"] is True
    assert health["status"] == (
        "ARTIFACT_READY_DORMANT_MANUAL_ACTIVATION_REQUIRED"
    )
    assert health["manual_activation_required"] is True
    assert health["notification_eligible"] is False
    assert health["automatic_promotion"] is False
    assert health["real_trading_allowed"] is False


def test_valid_v12_artifact_scores_only_prospective_paper_rows(
    monkeypatch, tmp_path: Path,
):
    feature_payload = _features(feature_runtime=v12)
    monkeypatch.setattr(v12, "feature_vector", lambda row: feature_payload)
    path = tmp_path / "v12-btc.json"
    artifact = _artifact(feature_runtime=v12, runtime_module=v12_runtime)
    _write(path, artifact)

    result = v12_runtime.runtime_prediction(
        {"close_time": v12.FIRST_ELIGIBLE_CLOSE_TIME + 900.0}, path,
    )
    assert result["available"] is True
    assert result["prospective"] is True
    assert result["design_id"] == v12.DESIGN_ID
    assert result["design_sha256"] == v12.DESIGN_SHA256
    assert result["walk_forward_protocol_id"] == v12.EVALUATION_PROTOCOL_ID
    assert result["walk_forward_protocol_sha256"] == (
        v12.EVALUATION_PROTOCOL_SHA256
    )
    assert result["yes_probability"] == pytest.approx(0.55)
    assert result["entry_recommendation"]["paper_only"] is True
    assert result["entry_recommendation"]["notification_eligible"] is False
    assert result["manual_activation_required"] is True
    assert result["notification_eligible"] is False
    assert result["automatic_promotion"] is False
    assert result["real_trading_allowed"] is False

    boundary = v12_runtime.runtime_prediction(
        {"close_time": v12.FIRST_ELIGIBLE_CLOSE_TIME}, path,
    )
    assert boundary["available"] is True
    assert boundary["prospective"] is False
    assert boundary["entry_recommendation"] is None


@pytest.mark.parametrize(
    ("mutate", "error"),
    (
        (
            lambda artifact: artifact.__setitem__(
                "walk_forward_protocol_sha256", "0" * 64,
            ),
            "v12_artifact_protocol_sha_mismatch",
        ),
        (
            lambda artifact: artifact.__setitem__(
                "notification_eligible", True,
            ),
            "v12_artifact_required_false_guard_missing",
        ),
        (
            lambda artifact: artifact.__setitem__(
                "prospective_after_close_time",
                v12.FIRST_ELIGIBLE_CLOSE_TIME - 1.0,
            ),
            "v12_artifact_prospective_boundary_invalid",
        ),
    ),
)
def test_v12_artifact_lineage_and_safety_tampering_fails_closed(
    mutate, error,
):
    artifact = _artifact(feature_runtime=v12, runtime_module=v12_runtime)
    mutate(artifact)
    artifact["artifact_sha256"] = v12_runtime.artifact_fingerprint(artifact)
    with pytest.raises(ValueError, match=error):
        v12_runtime.validate_artifact(artifact, "BTC")


def test_v12_rejects_v11_artifact_and_cache_is_design_scoped(
    tmp_path: Path,
):
    path = tmp_path / "one-design-only.json"
    _write(path, _artifact())
    assert runtime.load_artifact("BTC", path)["design_id"] == v11.DESIGN_ID
    with pytest.raises(ValueError, match="v12_artifact_model_version_mismatch"):
        v12_runtime.load_artifact("BTC", path)


def test_v12_missing_artifact_health_is_dormant_and_fail_closed(
    tmp_path: Path,
):
    health = v12_runtime.artifact_health(
        "NON_BTC_TRANSFER", tmp_path / "missing-v12.json",
    )
    assert health["available"] is False
    assert health["status"] == "WAITING_FOR_LOCKED_ARTIFACT"
    assert health["design_id"] == v12.DESIGN_ID
    assert health["design_sha256"] == v12.DESIGN_SHA256
    assert health["walk_forward_protocol_id"] == v12.EVALUATION_PROTOCOL_ID
    assert health["walk_forward_protocol_sha256"] == (
        v12.EVALUATION_PROTOCOL_SHA256
    )
    assert health["runtime_prediction_eligible"] is False
    assert health["notification_eligible"] is False
    assert health["automatic_promotion"] is False
    assert health["real_trading_allowed"] is False


def test_v11_and_v12_artifact_environment_paths_are_isolated(
    monkeypatch, tmp_path: Path,
):
    v11_path = tmp_path / "v11.json"
    v12_path = tmp_path / "v12.json"
    monkeypatch.setenv("Q15_RTI_MICROSTRUCTURE_V11_BTC_ARTIFACT", str(v11_path))
    monkeypatch.setenv("Q15_RTI_MICROSTRUCTURE_V12_BTC_ARTIFACT", str(v12_path))
    assert runtime.artifact_path("BTC") == v11_path
    assert v12_runtime.artifact_path("BTC") == v12_path
