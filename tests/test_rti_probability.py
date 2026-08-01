from __future__ import annotations

import json

import pytest

from q15_upgrade.strategy_bots.rti_probability import (
    FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION,
    MODEL_FAMILY,
    artifact_fingerprint,
    artifact_health,
    feature_vector,
    reset_artifact_cache,
    runtime_prediction,
    validate_artifact,
)
from tools.q15_rti_probability_freeze import chronological_folds, _fit_platt


def _row(**updates):
    row = {
        "asset": "BTC",
        "side": "YES",
        "rti_side": "YES",
        "close_time": 2_000.0,
        "entry_ask_cents": 55.0,
        "spread_cents": 1.0,
        "depth_contracts": 25.0,
        "rti_opposite_ask_cents": 46.0,
        "rti_opposite_depth_contracts": 30.0,
        "rti_market_mid_probability": 0.545,
        "rti_signed_distance_bps": 2.0,
        "rti_side_move_bps": 1.0,
        "rti_path_first_half_side_move_bps": 0.4,
        "rti_path_second_half_side_move_bps": 0.6,
        "rti_path_acceleration_bps": 0.2,
        "rti_path_range_bps": 3.0,
        "rti_path_realized_volatility_bps": 2.0,
        "rti_path_trend_efficiency": 0.5,
        "rti_path_persistence": 0.9,
        "rti_path_strike_crossings": 0,
        "rti_path_seconds_since_last_crossing": None,
        "rti_expected_remaining_volatility_bps": 10.0,
        "rti_distance_to_remaining_volatility": 0.2,
        "spot_depth_imbalance": 0.25,
    }
    row.update(updates)
    return row


def _artifact(boundary=1_999.0):
    count = len(FEATURE_NAMES)
    artifact = {
        "model_version": "test-rti-probability-v1",
        "model_family": MODEL_FAMILY,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "prospective_after_close_time": boundary,
        "entry_policy": {
            "sim_contracts": 10,
            "slippage_cents_per_contract": 2.0,
        },
        "cohorts": {
            name: {
                "means": [0.0] * count,
                "stds": [1.0] * count,
                "weights": [0.0] * count,
                "bias": 0.0,
                "calibration": {"a": 1.0, "b": 0.0},
            }
            for name in ("BTC", "NON_BTC_TRANSFER")
        },
    }
    artifact["artifact_sha256"] = artifact_fingerprint(artifact)
    return artifact


def test_feature_vector_uses_market_prior_and_point_in_time_path():
    result = feature_vector(_row())
    assert result["available"] is True
    assert result["cohort"] == "BTC"
    assert result["market_yes_probability"] == pytest.approx(0.545)
    values = dict(zip(FEATURE_NAMES, result["features"]))
    assert values["yes_signed_distance_bps"] == pytest.approx(2.0)
    assert values["yes_side_move_bps"] == pytest.approx(1.0)
    assert values["yes_persistence_signal"] == pytest.approx(0.8)
    assert values["no_strike_crossing"] == 1.0


def test_feature_vector_marks_missing_opposite_depth_and_never_invents_it():
    result = feature_vector(
        _row(rti_opposite_ask_cents=None, rti_opposite_depth_contracts=None)
    )
    assert result["available"] is True
    # Derived from binary complement: 100 - (55 YES ask - 1 spread).
    assert result["no_ask_cents"] == pytest.approx(46.0)
    assert result["no_depth_contracts"] == 0.0
    assert result["no_depth_available"] is False
    values = dict(zip(FEATURE_NAMES, result["features"]))
    assert values["kalshi_depth_ratio_missing"] == 1.0
    assert values["kalshi_yes_depth_log_ratio"] == 0.0


def test_no_rti_midpoint_is_converted_to_yes_probability_and_quotes():
    result = feature_vector(_row(
        side="NO",
        rti_side="NO",
        rti_market_mid_probability=0.545,
        rti_signed_distance_bps=-2.0,
        rti_side_move_bps=-1.0,
        rti_path_first_half_side_move_bps=-0.4,
        rti_path_second_half_side_move_bps=-0.6,
        rti_path_acceleration_bps=-0.2,
    ))
    assert result["available"] is True
    assert result["market_yes_probability"] == pytest.approx(0.455)
    assert result["yes_ask_cents"] == pytest.approx(46.0)
    assert result["no_ask_cents"] == pytest.approx(55.0)
    assert result["yes_depth_contracts"] == pytest.approx(30.0)
    assert result["no_depth_contracts"] == pytest.approx(25.0)


def test_runtime_prediction_enforces_prospective_boundary(tmp_path):
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text(json.dumps(_artifact()), encoding="utf-8")
    reset_artifact_cache()
    future = runtime_prediction(_row(close_time=2_000.0), artifact_path)
    boundary = runtime_prediction(_row(close_time=1_999.0), artifact_path)
    assert future["available"] is True
    assert future["prospective"] is True
    assert future["raw_yes_probability"] == pytest.approx(0.545)
    assert future["calibrated_yes_probability"] == pytest.approx(0.545)
    recommendation = future["entry_recommendation"]
    assert recommendation["side"] in {"YES", "NO"}
    assert recommendation["slippage_cents_per_contract"] == pytest.approx(2.0)
    assert recommendation["simulated_fill_cents"] == pytest.approx(
        recommendation["ask_cents"] + 2.0
    )
    assert recommendation["execution_cost_model_version"].endswith("-v2")
    assert recommendation["fee_schedule_version"] == (
        "kalshi-fee-schedule-20260707"
    )
    assert len(recommendation["candidates"]) == 2
    assert boundary["prospective"] is False
    assert boundary["historical_credit_allowed"] is False


def test_v3_disables_near_zero_variance_feature_and_reports_ood_guard(tmp_path):
    artifact = _artifact()
    artifact["model_version"] = "rti-probability-shadow-v3-test"
    artifact["standardization_policy"] = {
        "min_std": 1e-8,
        "z_clip": 6.0,
        "max_abs_z_allowed": 8.0,
        "out_of_distribution_fails_entry": True,
    }
    artifact["calibration_policy"] = {
        "monotone_slope_required": True,
        "min_slope": 0.25,
        "max_slope": 4.0,
        "max_abs_intercept": 4.0,
    }
    index = FEATURE_NAMES.index("kalshi_depth_ratio_missing")
    for cohort in artifact["cohorts"].values():
        cohort["means"][index] = 1.0
        cohort["stds"][index] = 2.4e-15
        cohort["weights"][index] = -0.25
    artifact["artifact_sha256"] = artifact_fingerprint(artifact)
    path = tmp_path / "v3.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")
    reset_artifact_cache()
    result = runtime_prediction(_row(), path)
    assert result["available"] is True
    assert result["raw_yes_probability"] == pytest.approx(0.545)
    assert result["out_of_distribution"] is False
    assert "kalshi_depth_ratio_missing" in result["standardization"][
        "inactive_near_zero_variance_features"
    ]


def test_v3_platt_calibration_cannot_invert_probability_ranking():
    raw = [0.9 if index < 10 else 0.1 for index in range(20)]
    examples = [
        {"close_time": float(index), "label_yes": int(index >= 10)}
        for index in range(20)
    ]
    calibration = _fit_platt(raw, examples)
    assert calibration["a"] >= 0.25
    assert calibration["monotone_slope_constrained"] is True


def test_artifact_health_quarantines_active_near_zero_variance_weight(tmp_path):
    artifact = _artifact()
    index = FEATURE_NAMES.index("kalshi_depth_ratio_missing")
    artifact["cohorts"]["NON_BTC_TRANSFER"]["stds"][index] = 2.4e-15
    artifact["cohorts"]["NON_BTC_TRANSFER"]["weights"][index] = -0.25
    artifact["artifact_sha256"] = artifact_fingerprint(artifact)
    path = tmp_path / "unstable-v2.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")
    reset_artifact_cache()
    health = artifact_health(path)
    assert health["status"] == "QUARANTINED_NUMERICAL_OOD"
    assert health["promotion_eligible"] is False
    assert health["numerical_issues"][0]["feature"] == (
        "kalshi_depth_ratio_missing"
    )


def test_artifact_fingerprint_rejects_tampering():
    artifact = _artifact()
    artifact["cohorts"]["BTC"]["bias"] = 1.0
    with pytest.raises(ValueError, match="fingerprint"):
        validate_artifact(artifact)


def test_chronological_fold_keeps_every_same_close_together():
    folds = chronological_folds([float(close) for close in range(20) for _ in range(7)])
    assert {name: len(values) for name, values in folds.items()} == {
        "train": 12,
        "calibration": 4,
        "test": 4,
    }
    assert not (folds["train"] & folds["calibration"])
    assert not (folds["train"] & folds["test"])
    assert not (folds["calibration"] & folds["test"])
