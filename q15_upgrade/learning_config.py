from __future__ import annotations

import os
from dataclasses import asdict, dataclass


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return int(default)


@dataclass(frozen=True)
class LearningV2Config:
    enabled: bool = _bool("Q15_LEARNING_V2_ENABLED", False)
    shadow: bool = _bool("Q15_LEARNING_V2_SHADOW", True)
    calibration_enabled: bool = _bool("Q15_CALIBRATION_ENABLED", True)
    apply_calibration_live: bool = _bool("Q15_APPLY_CALIBRATION_TO_LIVE", False)
    calibration_min_effective_n: int = _int("Q15_CALIBRATION_MIN_EFFECTIVE_N", 50)
    asset_calibration_min_effective_n: int = _int("Q15_ASSET_CALIBRATION_MIN_EFFECTIVE_N", 100)
    calibration_l2: float = _float("Q15_CALIBRATION_L2", 1.0)
    calibration_learning_rate: float = _float("Q15_CALIBRATION_LEARNING_RATE", 0.03)
    calibration_steps: int = _int("Q15_CALIBRATION_STEPS", 800)

    decay_half_life_days: float = _float("Q15_DECAY_HALF_LIFE_DAYS", 14.0)
    decay_half_life_autoselect: bool = _bool("Q15_DECAY_HALF_LIFE_AUTOSELECT", True)
    decay_candidates_days: tuple[float, ...] = (3.0, 7.0, 14.0, 30.0, 0.0)

    suppress_ev_cents: float = _float("Q15_SUPPRESS_EV_CENTS", -1.0)
    recover_ev_cents: float = _float("Q15_RECOVER_EV_CENTS", 0.5)
    recovery_min_effective_n: int = _int("Q15_RECOVERY_MIN_EFFECTIVE_N", 30)
    recovery_consecutive_periods: int = _int("Q15_RECOVERY_CONSECUTIVE_PERIODS", 2)

    max_threshold_change_per_update_cents: float = _float(
        "Q15_MAX_THRESHOLD_CHANGE_PER_UPDATE_CENTS", 0.5
    )
    max_threshold_deviation_cents: float = _float("Q15_MAX_THRESHOLD_DEVIATION_CENTS", 3.0)
    min_hours_between_promotions: float = _float("Q15_MIN_HOURS_BETWEEN_PROMOTIONS", 24.0)
    challenger_min_effective_n: int = _int("Q15_CHALLENGER_MIN_EFFECTIVE_N", 50)

    walk_forward_train_days: int = _int("Q15_WALK_FORWARD_TRAIN_DAYS", 30)
    walk_forward_validation_days: int = _int("Q15_WALK_FORWARD_VALIDATION_DAYS", 3)
    walk_forward_test_days: int = _int("Q15_WALK_FORWARD_TEST_DAYS", 3)

    scalp_learning_enabled: bool = _bool("Q15_SCALP_LEARNING_ENABLED", True)
    scalp_target_cents: float = max(10.0, _float("Q15_SCALP_TARGET_CENTS", 10.0))
    scalp_min_target_probability: float = _float("Q15_SCALP_MIN_TARGET_PROBABILITY", 0.62)
    scalp_min_expected_ev_cents: float = _float("Q15_SCALP_MIN_EXPECTED_EV_CENTS", 1.0)
    scalp_model_min_effective_n: int = _int("Q15_SCALP_MODEL_MIN_EFFECTIVE_N", 50)

    circuit_breakers_enabled: bool = _bool("Q15_LEARNING_CIRCUIT_BREAKERS", True)
    max_learning_data_age_seconds: float = _float("Q15_LEARNING_MAX_DATA_AGE_SECONDS", 8.0)
    max_ws_silence_seconds: float = _float("Q15_LEARNING_MAX_WS_SILENCE_SECONDS", 15.0)
    max_feature_drift_psi: float = _float("Q15_MAX_FEATURE_DRIFT_PSI", 0.25)

    decision_observation_interval_seconds: int = _int("Q15_DECISION_OBSERVATION_INTERVAL_SECONDS", 30)
    reconcile_interval_seconds: int = _int("Q15_LEARNING_RECONCILE_INTERVAL_SECONDS", 30)
    fit_interval_seconds: int = _int("Q15_LEARNING_FIT_INTERVAL_SECONDS", 900)
    max_reconcile_calls: int = _int("Q15_LEARNING_MAX_RECONCILE_CALLS", 12)

    feature_schema_version: str = os.environ.get("Q15_FEATURE_SCHEMA_VERSION", "q15-features-v4")
    policy_version: str = os.environ.get("Q15_POLICY_VERSION", "q15-v4-learning-shadow")
    model_version: str = os.environ.get("Q15_MODEL_VERSION", "q15-chart-settlement-v1")
    code_commit: str = os.environ.get("REPLIT_GIT_COMMIT", os.environ.get("GIT_COMMIT", "unknown"))

    end_prediction_enabled: bool = _bool("Q15_END_PREDICTION_ENABLED", True)
    end_prediction_uncertain_low: float = _float("Q15_END_PREDICTION_UNCERTAIN_LOW", 0.42)
    end_prediction_uncertain_high: float = _float("Q15_END_PREDICTION_UNCERTAIN_HIGH", 0.58)
    end_prediction_market_blend: float = min(
        0.35, max(0.0, _float("Q15_END_PREDICTION_MARKET_BLEND", 0.15))
    )

    threshold_confidence_k: float = _float("Q15_THRESHOLD_CONFIDENCE_K", 40.0)
    min_effective_n_for_adjustment: float = _float("Q15_MIN_EFFECTIVE_N_FOR_ADJUSTMENT", 20.0)
    rollback_min_effective_n: float = _float("Q15_ROLLBACK_MIN_EFFECTIVE_N", 40.0)
    rollback_bootstrap_samples: int = _int("Q15_ROLLBACK_BOOTSTRAP_SAMPLES", 400)

    def public_dict(self) -> dict:
        data = asdict(self)
        data["decay_candidates_days"] = list(self.decay_candidates_days)
        return data
