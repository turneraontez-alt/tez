"""Frozen RTI settlement-probability shadow model.

The model treats the de-spread Kalshi probability as a strong prior and learns
only a regularized residual from point-in-time RTI/path evidence.  Artifacts are
plain JSON, cohort-specific (BTC vs non-BTC), and can only score closes strictly
after the artifact's historical-evaluation boundary.

This module deliberately contains no order or notification code.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import threading
from typing import Any, Mapping, Sequence

from .costs import rti_simulated_execution


FEATURE_SCHEMA_VERSION = "rti-probability-features-v2"
MODEL_FAMILY = "market-prior-residual-logit"
DEFAULT_ARTIFACT_PATH = "config/q15_rti_probability_v2.json"
V3_ARTIFACT_PATH = "config/q15_rti_probability_v3.json"
NEAR_ZERO_STD_AUDIT_THRESHOLD = 1e-8

NON_BTC_ASSETS = ("BNB", "DOGE", "ETH", "HYPE", "SOL", "XRP")
FEATURE_NAMES = (
    "final_side_yes",
    "yes_signed_distance_bps",
    "yes_side_move_bps",
    "yes_first_half_move_bps",
    "yes_second_half_move_bps",
    "yes_acceleration_bps",
    "log1p_path_range_bps",
    "log1p_realized_volatility_bps",
    "trend_efficiency",
    "yes_persistence_signal",
    "log1p_strike_crossings",
    "no_strike_crossing",
    "seconds_since_crossing_fraction",
    "yes_distance_to_remaining_volatility",
    "spot_imbalance",
    "spot_imbalance_missing",
    "kalshi_yes_depth_log_ratio",
    "kalshi_depth_ratio_missing",
    "log1p_total_kalshi_depth",
    "spread_cents",
    "market_distance_from_half",
    "range_to_remaining_volatility",
    *(f"asset_{asset.lower()}" for asset in NON_BTC_ASSETS),
)

_MANDATORY_PATH_FIELDS = (
    "rti_signed_distance_bps",
    "rti_side_move_bps",
    "rti_path_first_half_side_move_bps",
    "rti_path_second_half_side_move_bps",
    "rti_path_acceleration_bps",
    "rti_path_range_bps",
    "rti_path_realized_volatility_bps",
    "rti_path_trend_efficiency",
    "rti_path_persistence",
    "rti_path_strike_crossings",
    "rti_expected_remaining_volatility_bps",
    "rti_distance_to_remaining_volatility",
)


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _sigmoid(value: float) -> float:
    if value >= 0.0:
        z = math.exp(-min(value, 709.0))
        return 1.0 / (1.0 + z)
    z = math.exp(max(value, -709.0))
    return z / (1.0 + z)


def _logit(probability: float) -> float:
    p = _clip(probability, 1e-6, 1.0 - 1e-6)
    return math.log(p / (1.0 - p))


def _profile(row: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = row.get("threshold_json")
    if isinstance(raw, Mapping):
        return raw
    if not raw:
        return {}
    try:
        decoded = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, Mapping) else {}


def _value(row: Mapping[str, Any], profile: Mapping[str, Any], key: str) -> Any:
    value = row.get(key)
    return profile.get(key) if value is None else value


def _side(value: Any) -> str | None:
    side = str(value or "").upper()
    return side if side in {"YES", "NO"} else None


def _market_and_quotes(
    row: Mapping[str, Any], profile: Mapping[str, Any], rti_side: str,
) -> dict[str, Any] | None:
    side_ask = _num(row.get("entry_ask_cents"))
    opposite_ask = _num(_value(row, profile, "rti_opposite_ask_cents"))
    spread = _num(row.get("spread_cents"))
    side_depth = _num(
        row.get("depth_contracts")
        if row.get("depth_contracts") is not None
        else profile.get("depth_contracts")
    )
    opposite_depth = _num(_value(row, profile, "rti_opposite_depth_contracts"))
    if side_ask is None or side_depth is None or spread is None:
        return None
    if opposite_ask is None:
        # Binary complement: opposite ask = 100 - selected-side bid.
        opposite_ask = 100.0 - (float(side_ask) - float(spread))
    opposite_depth_available = opposite_depth is not None
    opposite_depth_value = float(opposite_depth or 0.0)
    if rti_side == "YES":
        yes_ask, no_ask = float(side_ask), float(opposite_ask)
        yes_depth, no_depth = float(side_depth), opposite_depth_value
        yes_depth_available, no_depth_available = True, opposite_depth_available
    else:
        yes_ask, no_ask = float(opposite_ask), float(side_ask)
        yes_depth, no_depth = opposite_depth_value, float(side_depth)
        yes_depth_available, no_depth_available = opposite_depth_available, True
    selected_side_mid = _num(
        _value(row, profile, "rti_market_mid_probability")
    )
    market_yes = (
        None
        if selected_side_mid is None
        else float(selected_side_mid)
        if rti_side == "YES"
        else 1.0 - float(selected_side_mid)
    )
    if market_yes is None:
        # NO ask = 100 - YES bid.  Mid the executable YES ask and implied YES bid.
        market_yes = (yes_ask + (100.0 - no_ask)) / 200.0
    if not 0.0 < float(market_yes) < 1.0:
        return None
    return {
        "market_yes": float(market_yes),
        "yes_ask": yes_ask,
        "no_ask": no_ask,
        "yes_depth": yes_depth,
        "no_depth": no_depth,
        "yes_depth_available": yes_depth_available,
        "no_depth_available": no_depth_available,
        "depth_ratio_missing": not (yes_depth_available and no_depth_available),
    }


def feature_vector(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deterministic, decision-time feature vector or an error."""
    profile = _profile(row)
    asset = str(row.get("asset") or profile.get("asset_cohort") or "").upper()
    rti_side = _side(_value(row, profile, "rti_side") or row.get("side"))
    if asset not in {"BTC", *NON_BTC_ASSETS}:
        return {"available": False, "error": "unsupported_asset"}
    if rti_side is None:
        return {"available": False, "error": "rti_side_missing"}
    quotes = _market_and_quotes(row, profile, rti_side)
    if quotes is None:
        return {"available": False, "error": "market_quote_or_depth_missing"}
    market_yes = float(quotes["market_yes"])
    yes_ask, no_ask = float(quotes["yes_ask"]), float(quotes["no_ask"])
    yes_depth, no_depth = float(quotes["yes_depth"]), float(quotes["no_depth"])
    values: dict[str, float] = {}
    for key in _MANDATORY_PATH_FIELDS:
        value = _num(_value(row, profile, key))
        if value is None:
            return {"available": False, "error": f"feature_missing:{key}"}
        values[key] = value

    direction = 1.0 if rti_side == "YES" else -1.0
    crossings = max(0.0, values["rti_path_strike_crossings"])
    seconds_since = _num(
        _value(row, profile, "rti_path_seconds_since_last_crossing")
    )
    no_crossing = 1.0 if crossings <= 0.0 else 0.0
    crossing_fraction = (
        1.0 if no_crossing else _clip(float(seconds_since or 0.0) / 61.0, 0.0, 1.0)
    )
    spot = _num(_value(row, profile, "spot_depth_imbalance"))
    expected_remaining = max(
        values["rti_expected_remaining_volatility_bps"], 1e-6
    )
    total_depth = max(0.0, yes_depth) + max(0.0, no_depth)
    spread = _num(row.get("spread_cents"))
    if spread is None:
        return {"available": False, "error": "spread_missing"}

    vector = [
        direction,
        _clip(direction * values["rti_signed_distance_bps"], -30.0, 30.0),
        _clip(direction * values["rti_side_move_bps"], -20.0, 20.0),
        _clip(direction * values["rti_path_first_half_side_move_bps"], -20.0, 20.0),
        _clip(direction * values["rti_path_second_half_side_move_bps"], -20.0, 20.0),
        _clip(direction * values["rti_path_acceleration_bps"], -30.0, 30.0),
        math.log1p(_clip(values["rti_path_range_bps"], 0.0, 100.0)),
        math.log1p(_clip(values["rti_path_realized_volatility_bps"], 0.0, 100.0)),
        _clip(values["rti_path_trend_efficiency"], 0.0, 1.0),
        direction * _clip(2.0 * values["rti_path_persistence"] - 1.0, -1.0, 1.0),
        math.log1p(_clip(crossings, 0.0, 30.0)),
        no_crossing,
        crossing_fraction,
        _clip(direction * values["rti_distance_to_remaining_volatility"], -5.0, 5.0),
        _clip(float(spot or 0.0), -1.0, 1.0),
        1.0 if spot is None else 0.0,
        0.0 if quotes["depth_ratio_missing"] else _clip(math.log((max(0.0, yes_depth) + 1.0) / (max(0.0, no_depth) + 1.0)), -8.0, 8.0),
        1.0 if quotes["depth_ratio_missing"] else 0.0,
        math.log1p(_clip(total_depth, 0.0, 1_000_000.0)),
        _clip(spread, 0.0, 10.0),
        abs(market_yes - 0.5),
        _clip(values["rti_path_range_bps"] / expected_remaining, 0.0, 10.0),
        *(1.0 if asset == candidate else 0.0 for candidate in NON_BTC_ASSETS),
    ]
    if len(vector) != len(FEATURE_NAMES):
        return {"available": False, "error": "feature_schema_length_mismatch"}
    return {
        "available": True,
        "asset": asset,
        "cohort": "BTC" if asset == "BTC" else "NON_BTC_TRANSFER",
        "feature_names": list(FEATURE_NAMES),
        "features": vector,
        "market_yes_probability": market_yes,
        "market_probability_semantics": (
            "YES probability; converted from selected-RTI-side midpoint"
        ),
        "yes_ask_cents": yes_ask,
        "no_ask_cents": no_ask,
        "yes_depth_contracts": yes_depth,
        "no_depth_contracts": no_depth,
        "yes_depth_available": bool(quotes["yes_depth_available"]),
        "no_depth_available": bool(quotes["no_depth_available"]),
    }


def _predict_cohort(
    cohort: Mapping[str, Any],
    features: Sequence[float],
    market: float,
    standardization_policy: Mapping[str, Any] | None = None,
) -> tuple[float, float, dict[str, Any]]:
    means = [float(value) for value in cohort.get("means", ())]
    stds = [float(value) for value in cohort.get("stds", ())]
    weights = [float(value) for value in cohort.get("weights", ())]
    if not (len(features) == len(means) == len(stds) == len(weights) == len(FEATURE_NAMES)):
        raise ValueError("artifact_feature_length_mismatch")
    policy = dict(standardization_policy or {})
    min_std = max(0.0, float(_num(policy.get("min_std")) or 0.0))
    z_clip = _num(policy.get("z_clip"))
    max_abs_z_allowed = _num(policy.get("max_abs_z_allowed"))
    preclip = [
        0.0
        if stds[i] <= min_std
        else (float(features[i]) - means[i]) / stds[i]
        for i in range(len(features))
    ]
    standardized = [
        value
        if z_clip is None or z_clip <= 0.0
        else _clip(value, -z_clip, z_clip)
        for value in preclip
    ]
    max_abs_z = max((abs(value) for value in preclip), default=0.0)
    inactive = [
        FEATURE_NAMES[i] for i, std in enumerate(stds) if std <= min_std
    ]
    score = _logit(market) + float(cohort.get("bias", 0.0)) + sum(
        weights[i] * standardized[i] for i in range(len(weights))
    )
    raw = _clip(_sigmoid(score), 0.01, 0.99)
    calibration = cohort.get("calibration") or {}
    calibrated = _sigmoid(
        float(calibration.get("a", 1.0)) * _logit(raw)
        + float(calibration.get("b", 0.0))
    )
    diagnostics = {
        "min_std": min_std,
        "z_clip": z_clip,
        "max_abs_z_allowed": max_abs_z_allowed,
        "max_abs_z_preclip": max_abs_z,
        "inactive_near_zero_variance_features": inactive,
        "out_of_distribution": bool(
            max_abs_z_allowed is not None
            and max_abs_z > max_abs_z_allowed
        ),
    }
    return raw, _clip(calibrated, 0.01, 0.99), diagnostics


def _artifact_numerical_issues(
    artifact: Mapping[str, Any],
) -> list[dict[str, Any]]:
    issues = []
    policy = dict(artifact.get("standardization_policy") or {})
    min_std = max(0.0, float(_num(policy.get("min_std")) or 0.0))
    effective_threshold = max(min_std, NEAR_ZERO_STD_AUDIT_THRESHOLD)
    for cohort_name, raw_cohort in dict(artifact.get("cohorts") or {}).items():
        if not isinstance(raw_cohort, Mapping):
            continue
        stds = list(raw_cohort.get("stds") or ())
        weights = list(raw_cohort.get("weights") or ())
        for name, std_raw, weight_raw in zip(FEATURE_NAMES, stds, weights):
            std = abs(float(std_raw))
            weight = float(weight_raw)
            if 0.0 < std < effective_threshold and abs(weight) > 1e-12:
                issues.append({
                    "cohort": str(cohort_name),
                    "feature": name,
                    "std": std,
                    "weight": weight,
                    "issue": "NEAR_ZERO_VARIANCE_WITH_ACTIVE_WEIGHT",
                })
    return issues


def _fee_per_contract(
    ask_cents: float,
    contracts: int,
    slippage_cents_per_contract: float = 0.0,
) -> float:
    execution = rti_simulated_execution(
        ask_cents,
        contracts,
        slippage_cents_per_contract,
    )
    return (
        0.0
        if execution is None
        else float(execution["fee_cents_per_contract"])
    )


def _entry_recommendation(
    artifact: Mapping[str, Any],
    features: Mapping[str, Any],
    probability_yes: float,
) -> dict[str, Any]:
    policy = dict(artifact.get("entry_policy") or {})
    contracts = max(1, int(_num(policy.get("sim_contracts")) or 10))
    slippage = max(
        0.0, float(_num(policy.get("slippage_cents_per_contract")) or 0.0)
    )
    candidates = []
    for side, probability in (
        ("YES", probability_yes),
        ("NO", 1.0 - probability_yes),
    ):
        lower = side.lower()
        ask = float(features[f"{lower}_ask_cents"])
        depth = float(features[f"{lower}_depth_contracts"])
        depth_available = bool(features[f"{lower}_depth_available"])
        execution = rti_simulated_execution(ask, contracts, slippage)
        if execution is None:  # feature validation should make this unreachable
            continue
        fee = float(execution["fee_cents_per_contract"])
        fill = float(execution["simulated_fill_cents"])
        expected_value = probability * 100.0 - fill - fee
        candidates.append({
            "side": side,
            "win_probability": probability,
            "ask_cents": ask,
            "simulated_fill_cents": fill,
            "depth_contracts": depth,
            "depth_available": depth_available,
            "fee_cents_per_contract": fee,
            "slippage_cents_per_contract": slippage,
            "execution_cost_model_version": execution[
                "execution_cost_model_version"
            ],
            "fee_schedule_version": execution["fee_schedule_version"],
            "expected_value_cents_per_contract": expected_value,
        })
    selected = max(
        candidates, key=lambda candidate: candidate[
            "expected_value_cents_per_contract"
        ]
    )
    return {
        **selected,
        "candidates": candidates,
        "policy": policy,
    }


def artifact_fingerprint(artifact: Mapping[str, Any]) -> str:
    payload = dict(artifact)
    payload.pop("artifact_sha256", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_artifact(artifact: Mapping[str, Any]) -> None:
    if artifact.get("feature_schema_version") != FEATURE_SCHEMA_VERSION:
        raise ValueError("artifact_feature_schema_mismatch")
    if tuple(artifact.get("feature_names") or ()) != FEATURE_NAMES:
        raise ValueError("artifact_feature_names_mismatch")
    if artifact.get("model_family") != MODEL_FAMILY:
        raise ValueError("artifact_model_family_mismatch")
    if _num(artifact.get("prospective_after_close_time")) is None:
        raise ValueError("artifact_prospective_boundary_missing")
    cohorts = artifact.get("cohorts")
    if not isinstance(cohorts, Mapping):
        raise ValueError("artifact_cohorts_missing")
    for cohort_name in ("BTC", "NON_BTC_TRANSFER"):
        cohort = cohorts.get(cohort_name)
        if not isinstance(cohort, Mapping):
            raise ValueError(f"artifact_cohort_missing:{cohort_name}")
        for key in ("means", "stds", "weights"):
            if len(cohort.get(key) or ()) != len(FEATURE_NAMES):
                raise ValueError(f"artifact_{key}_length_mismatch:{cohort_name}")
    if str(artifact.get("model_version") or "").startswith(
        "rti-probability-shadow-v3-"
    ):
        standardization = dict(artifact.get("standardization_policy") or {})
        calibration_policy = dict(artifact.get("calibration_policy") or {})
        if float(_num(standardization.get("min_std")) or 0.0) < 1e-8:
            raise ValueError("artifact_v3_min_std_guard_missing")
        if float(_num(standardization.get("z_clip")) or 0.0) <= 0.0:
            raise ValueError("artifact_v3_z_clip_missing")
        if float(
            _num(standardization.get("max_abs_z_allowed")) or 0.0
        ) <= 0.0:
            raise ValueError("artifact_v3_ood_guard_missing")
        if calibration_policy.get("monotone_slope_required") is not True:
            raise ValueError("artifact_v3_monotone_calibration_missing")
        minimum_slope = float(_num(calibration_policy.get("min_slope")) or 0.0)
        for cohort_name in ("BTC", "NON_BTC_TRANSFER"):
            slope = float(
                _num(artifact["cohorts"][cohort_name]["calibration"].get("a"))
                or 0.0
            )
            if slope < minimum_slope:
                raise ValueError(
                    f"artifact_v3_nonmonotone_calibration:{cohort_name}"
                )
    expected = artifact.get("artifact_sha256")
    if expected and str(expected) != artifact_fingerprint(artifact):
        raise ValueError("artifact_fingerprint_mismatch")


_cache_lock = threading.Lock()
_artifact_cache: dict[str, Any] = {"path": None, "mtime_ns": None, "artifact": None, "error": None}


def reset_artifact_cache() -> None:
    with _cache_lock:
        _artifact_cache.update(path=None, mtime_ns=None, artifact=None, error=None)


def artifact_path() -> Path:
    return Path(os.environ.get("Q15_RTI_PROBABILITY_ARTIFACT") or DEFAULT_ARTIFACT_PATH)


def load_artifact(path: str | Path | None = None) -> Mapping[str, Any]:
    target = Path(path) if path is not None else artifact_path()
    resolved = str(target.resolve())
    stat = target.stat()
    with _cache_lock:
        if (
            _artifact_cache.get("path") == resolved
            and _artifact_cache.get("mtime_ns") == stat.st_mtime_ns
            and isinstance(_artifact_cache.get("artifact"), Mapping)
        ):
            return _artifact_cache["artifact"]
        try:
            decoded = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(decoded, Mapping):
                raise ValueError("artifact_root_not_object")
            validate_artifact(decoded)
        except Exception as exc:
            _artifact_cache.update(
                path=resolved, mtime_ns=stat.st_mtime_ns, artifact=None,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        _artifact_cache.update(
            path=resolved, mtime_ns=stat.st_mtime_ns, artifact=decoded, error=None,
        )
        return decoded


def runtime_prediction(row: Mapping[str, Any], path: str | Path | None = None) -> dict[str, Any]:
    features = feature_vector(row)
    if not features.get("available"):
        return features
    try:
        artifact = load_artifact(path)
    except FileNotFoundError:
        return {**features, "available": False, "error": "artifact_missing"}
    except Exception as exc:
        return {
            **features,
            "available": False,
            "error": f"artifact_invalid:{type(exc).__name__}:{exc}",
        }
    close_time = _num(row.get("close_time"))
    prospective_after = float(artifact["prospective_after_close_time"])
    prospective = close_time is not None and close_time > prospective_after
    cohort_name = str(features["cohort"])
    raw, calibrated, standardization = _predict_cohort(
        artifact["cohorts"][cohort_name],
        features["features"],
        float(features["market_yes_probability"]),
        artifact.get("standardization_policy"),
    )
    recommendation = _entry_recommendation(artifact, features, calibrated)
    return {
        **features,
        "available": True,
        "prospective": prospective,
        "prospective_after_close_time": prospective_after,
        "model_version": artifact.get("model_version"),
        "artifact_sha256": artifact.get("artifact_sha256"),
        "raw_yes_probability": raw,
        "calibrated_yes_probability": calibrated,
        "standardization": standardization,
        "out_of_distribution": standardization["out_of_distribution"],
        "entry_recommendation": recommendation,
        "standardization_policy": artifact.get("standardization_policy"),
        "calibration_policy": artifact.get("calibration_policy"),
        "historical_credit_allowed": False,
    }


def artifact_health(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else artifact_path()
    try:
        artifact = load_artifact(target)
        numerical_issues = _artifact_numerical_issues(artifact)
        return {
            "available": True,
            "path": str(target),
            "model_version": artifact.get("model_version"),
            "feature_schema_version": artifact.get("feature_schema_version"),
            "artifact_sha256": artifact.get("artifact_sha256"),
            "prospective_after_close_time": artifact.get("prospective_after_close_time"),
            "paper_only": True,
            "automatic_promotion": False,
            "notification_eligible": False,
            "numerical_issues": numerical_issues,
            "promotion_eligible": not numerical_issues,
            "status": (
                "QUARANTINED_NUMERICAL_OOD"
                if numerical_issues
                else "ACTIVE_PAPER_RESEARCH"
            ),
        }
    except Exception as exc:
        return {
            "available": False,
            "path": str(target),
            "error": f"{type(exc).__name__}: {exc}",
            "paper_only": True,
            "automatic_promotion": False,
            "notification_eligible": False,
        }
