"""Durable one-shot V15 untouched-test evaluator.

The caller supplies frozen outcome-free source rows, already-authorized pretest
labels, sealed prior-stage reports, and one explicit test-label callback.  All
feature and quote evidence is reconstructed and hash-checked before an
append-only reservation.  An existing reservation can never invoke the test
callback again, including after a crash.  This module has no database,
settlement, Telegram, promotion, paper-runtime, or order capability.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Callable, Mapping, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots.costs import (
    KALSHI_Q15_FEE_SCHEDULE_VERSION,
    RTI_EXECUTION_COST_MODEL_VERSION,
)
from q15_upgrade.strategy_bots.rti_microstructure_v15_identity import (
    DESIGN_ID,
    DESIGN_SHA256,
    EVALUATION_PROTOCOL_ID,
    EVALUATION_PROTOCOL_SHA256,
)
from tools.q15_rti_v15_label_evidence import validate_label_evidence
from q15_upgrade.strategy_bots import (
    rti_microstructure_v15_audit_identity as audit_identity,
)
from tools import q15_rti_v15_audit_seal as audit_seal
from tools import q15_rti_v15_walk_forward as walk
from tools.q15_rti_microstructure_freeze import (
    _trade_path_metrics,
    apply_residual_trust,
    fit_residual_model,
    predict_probabilities,
)
from tools.q15_rti_microstructure_preregister import design_fingerprint

TEST_RUNNER_VERSION = audit_identity.UNTOUCHED_TEST_RUNNER_VERSION
TEST_STATE_VERSION = audit_identity.UNTOUCHED_TEST_STATE_VERSION
CONFIRMATION_PHRASE = "SCORE_V15_UNTOUCHED_TEST_ONCE"
RESERVED_STATUS = "TEST_SCORE_RESERVED"
PASS_STATUS = "PASSED_UNTOUCHED_TEST_HISTORICAL_GATES_ONLY"
REJECT_STATUS = "REJECTED_ON_UNTOUCHED_TEST"
FINAL_STATUSES = frozenset({PASS_STATUS, REJECT_STATUS})
DEFAULT_REPORTING_PROTOCOL = ROOT / "config" / "q15_rti_v15_reporting_protocol.json"
REPORTING_PROTOCOL_ID = audit_identity.REPORTING_PROTOCOL_ID
REPORTING_PROTOCOL_SHA256 = audit_identity.REPORTING_PROTOCOL_SHA256


def _canonical_sha256(value: Any) -> str:
    def normalize(item: Any) -> Any:
        if isinstance(item, np.ndarray):
            return [normalize(value) for value in item.tolist()]
        if isinstance(item, np.generic):
            return item.item()
        if isinstance(item, Mapping):
            return {str(key): normalize(value) for key, value in item.items()}
        if isinstance(item, (list, tuple)):
            return [normalize(value) for value in item]
        return item

    return hashlib.sha256(json.dumps(
        normalize(value), sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _now_iso(value: str | None = None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("v15_test_timestamp_timezone_missing")
    return parsed.isoformat()


def _sealed(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result.pop("state_sha256", None)
    result["test_state_version"] = TEST_STATE_VERSION
    result["state_sha256"] = _canonical_sha256(result)
    return result


def _validate_sealed(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    expected = str(result.get("state_sha256") or "")
    unsigned = {key: value for key, value in result.items() if key != "state_sha256"}
    if (
        result.get("test_state_version") != TEST_STATE_VERSION
        or not expected
        or expected != _canonical_sha256(unsigned)
    ):
        raise ValueError("v15_test_state_identity_or_fingerprint_invalid")
    return result


def _read_sealed(path: Path) -> dict[str, Any]:
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("v15_test_state_unreadable") from exc
    if not isinstance(decoded, Mapping):
        raise ValueError("v15_test_state_root_not_object")
    return _validate_sealed(decoded)


def _write_exclusive(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    sealed = _sealed(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(sealed, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return sealed


def result_path_for(reservation_path: Path) -> Path:
    suffix = reservation_path.suffix or ".json"
    return reservation_path.with_name(f"{reservation_path.stem}.result{suffix}")


def load_reporting_protocol(path: Path = DEFAULT_REPORTING_PROTOCOL) -> dict[str, Any]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, Mapping):
        raise ValueError("v15_test_reporting_protocol_root_not_object")
    result = dict(decoded)
    dimensions = {
        "asset", "rti_side", "absolute_distance_tier",
        "realized_volatility_tier", "market_regime",
        "path_depth_agreement", "path_spread_stress_tier",
    }
    if (
        result.get("protocol_id") != REPORTING_PROTOCOL_ID
        or design_fingerprint(result) != REPORTING_PROTOCOL_SHA256
        or result.get("applies_to_design_id") != DESIGN_ID
        or result.get("applies_to_design_sha256") != DESIGN_SHA256
        or result.get("applies_to_evaluation_protocol_id") != EVALUATION_PROTOCOL_ID
        or result.get("applies_to_evaluation_protocol_sha256") != EVALUATION_PROTOCOL_SHA256
        or result.get("outcome_labels_used_for_protocol") is not False
        or result.get("performance_metrics_inspected_before_preregistration") is not False
        or result.get("changes_features_model_hyperparameters_trust_entry_policy_or_gates") is not False
        or result.get("report_only") is not True
        or result.get("paper_only") is not True
        or result.get("notification_eligible") is not False
        or result.get("automatic_promotion") is not False
        or result.get("real_trading_allowed") is not False
        or result.get("cohort_pooling_forbidden") is not True
        or result.get("untouched_test_scored_once") is not True
        or int(result.get("calibration_bins") or 0) != 10
        or set(dict(result.get("dimensions") or {})) != dimensions
    ):
        raise ValueError("v15_test_reporting_protocol_invalid")
    return result


def _validate_design_protocol(design: Mapping[str, Any], protocol: Mapping[str, Any]) -> None:
    protocol_entry = dict(protocol.get("entry_policy") or {})
    if protocol_entry.pop("unchanged_from_v14", None) is not True:
        raise ValueError("v15_test_entry_policy_control_binding_missing")
    if (
        protocol_entry.pop("fake_fill_assumptions_forbidden", None) is not True
        or protocol_entry.pop("reused_quotes_forbidden", None) is not True
    ):
        raise ValueError("v15_test_execution_safety_binding_missing")
    if (
        design.get("design_id") != DESIGN_ID
        or design_fingerprint(design) != DESIGN_SHA256
        or protocol.get("protocol_id") != EVALUATION_PROTOCOL_ID
        or design_fingerprint(protocol) != EVALUATION_PROTOCOL_SHA256
        or dict(design.get("entry_policy") or {}) != protocol_entry
    ):
        raise ValueError("v15_test_design_or_protocol_identity_mismatch")


def _hash_ids(rows: Sequence[Mapping[str, Any]]) -> str:
    return audit_seal.canonical_sha256(tuple(sorted(int(row["id"]) for row in rows)))


def _hash_times(rows: Sequence[Mapping[str, Any]]) -> str:
    return audit_seal.canonical_sha256(tuple(sorted({float(row["close_time"]) for row in rows})))


def _prepare_projected_rows(
    selected_feature_rows: Sequence[Mapping[str, Any]],
    *, seal: Mapping[str, Any], cohort: str, protocol: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    audit_seal.validate_audit_seal(seal)
    if seal.get("status") != audit_seal.READY_STATUS or seal.get("cohort") != cohort:
        raise ValueError("v15_test_audit_seal_not_ready_for_cohort")
    projected, contract_failures = audit_seal._project_evidence(selected_feature_rows)
    if contract_failures:
        raise ValueError("v15_test_contract_identity_failure")
    if (
        audit_seal.canonical_sha256(projected) != seal.get("selected_feature_evidence_sha256")
        or _hash_ids(projected) != seal.get("selected_row_ids_sha256")
    ):
        raise ValueError("v15_test_selected_feature_evidence_mismatch")

    rule = dict(protocol["cohorts"][cohort])
    expected_assets = walk.COHORT_ASSETS[cohort]
    windows = tuple(sorted({float(row["close_time"]) for row in projected}))
    if len(windows) != int(rule["minimum_complete_close_windows"]):
        raise ValueError("v15_test_selected_window_count_mismatch")
    if len({int(row["id"]) for row in projected}) != len(projected):
        raise ValueError("v15_test_duplicate_row_id")
    for close_time in windows:
        rows = [row for row in projected if float(row["close_time"]) == close_time]
        if len(rows) != len(expected_assets) or {str(row["asset"]).upper() for row in rows} != expected_assets:
            raise ValueError("v15_test_same_close_asset_leakage")
        for row in rows:
            numbers = [
                *[float(value) for value in row["v15_features"]],
                float(row["market_yes_probability"]), float(row["yes_ask_cents"]),
                float(row["no_ask_cents"]), float(row["yes_depth_contracts"]),
                float(row["no_depth_contracts"]), float(row["spread_cents"]),
            ]
            if (
                tuple(row.get("v15_feature_names") or ()) != walk.v15.FEATURE_NAMES
                or tuple(row.get("v14_feature_names") or ()) != walk.v14.FEATURE_NAMES
                or len(row.get("v15_features") or ()) != 25
                or len(row.get("v14_features") or ()) != 20
                or [float(value) for value in row["v15_features"][:20]] != [float(value) for value in row["v14_features"]]
                or not all(math.isfinite(value) for value in numbers)
                or not 0.0 < float(row["market_yes_probability"]) < 1.0
                or str(row.get("side") or "").upper() not in {"YES", "NO"}
                or not isinstance(row.get("yes_depth_available"), bool)
                or not isinstance(row.get("no_depth_available"), bool)
                or row.get("contract_identity_valid") is not True
            ):
                raise ValueError("v15_test_projected_row_invalid")

    pretest_count = int(rule["development_train_windows"]) + int(rule["calibration_windows"])
    pretest_times = set(windows[:pretest_count])
    test_times = set(windows[pretest_count:])
    pretest = [row for row in projected if float(row["close_time"]) in pretest_times]
    test = [row for row in projected if float(row["close_time"]) in test_times]
    test_partition = dict(seal["partitions"])["untouched_test"]
    if (
        _hash_ids(pretest) != seal.get("train_calibration_row_ids_sha256")
        or _hash_times(pretest) != audit_seal.canonical_sha256(tuple(sorted(pretest_times)))
        or _hash_ids(test) != seal.get("untouched_test_row_ids_sha256")
        or _hash_times(test) != test_partition.get("close_times_sha256")
        or _hash_ids(test) != test_partition.get("row_ids_sha256")
        or max(pretest_times) >= min(test_times)
    ):
        raise ValueError("v15_test_partition_identity_or_chronology_mismatch")
    return projected, pretest, test

def _validated_labels(
    raw: Mapping[int, int], expected_ids: Sequence[int], *, stage: str,
) -> dict[int, int]:
    try:
        labels = {int(key): int(value) for key, value in raw.items()}
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"v15_test_{stage}_labels_invalid") from exc
    if set(labels) != set(int(value) for value in expected_ids) or any(
        value not in {0, 1} for value in labels.values()
    ):
        raise ValueError(f"v15_test_{stage}_labels_invalid")
    return labels


def _attach_labels(
    rows: Sequence[Mapping[str, Any]], labels: Mapping[int, int],
) -> list[dict[str, Any]]:
    return [
        {**dict(row), "label_yes": int(labels[int(row["id"])])}
        for row in rows
    ]


def _verify_prior_reports(
    pretest: Sequence[Mapping[str, Any]],
    *, cohort: str, design: Mapping[str, Any], protocol: Mapping[str, Any],
    supplied_walk: Mapping[str, Any], supplied_calibration: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    recomputed_walk = walk.evaluate_walk_forward(
        pretest, cohort=cohort, design=design, protocol=protocol,
    )
    if (
        recomputed_walk.get("gate_met") is not True
        or _canonical_sha256(recomputed_walk) != _canonical_sha256(dict(supplied_walk))
    ):
        raise ValueError("v15_test_walk_forward_gate_or_report_mismatch")
    recomputed_calibration = walk.evaluate_calibration(
        pretest, cohort=cohort, design=design, protocol=protocol,
        walk_forward_report=recomputed_walk,
    )
    if (
        recomputed_calibration.get("gate_met") is not True
        or _canonical_sha256(recomputed_calibration) != _canonical_sha256(dict(supplied_calibration))
    ):
        raise ValueError("v15_test_calibration_gate_or_report_mismatch")
    for key in ("final_candidate_trust_selection", "final_v14_trust_selection"):
        trust = recomputed_calibration.get(key)
        if (
            not isinstance(trust, Mapping)
            or trust.get("outer_validation_labels_used_for_selection") is not False
            or trust.get("untouched_test_labels_used_for_selection") is not False
        ):
            raise ValueError("v15_test_final_trust_leakage_or_missing")
    return recomputed_walk, recomputed_calibration


def _calibration_errors(
    rows: Sequence[Mapping[str, Any]], probabilities: Sequence[float], *, bins: int = 10,
) -> dict[str, float]:
    if len(rows) != len(probabilities) or not rows or bins != 10:
        raise ValueError("v15_test_calibration_geometry_invalid")
    groups: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for row, raw in zip(rows, probabilities):
        probability = max(0.0, min(1.0, float(raw)))
        groups[min(bins - 1, int(probability * bins))].append(
            (probability, int(row["label_yes"]))
        )
    errors = []
    weighted = 0.0
    for group in groups:
        if not group:
            continue
        error = abs(
            sum(value[0] for value in group) / len(group)
            - sum(value[1] for value in group) / len(group)
        )
        errors.append(error)
        weighted += error * len(group) / len(rows)
    return {
        "expected_calibration_error": weighted,
        "maximum_calibration_error": max(errors, default=0.0),
    }


def _scores(
    rows: Sequence[Mapping[str, Any]], probabilities: Sequence[float],
) -> dict[str, Any]:
    return {**walk.proper_scores(rows, probabilities), **_calibration_errors(rows, probabilities)}


def _economics(
    rows: Sequence[Mapping[str, Any]], probabilities: Sequence[float], policy: Mapping[str, Any],
) -> dict[str, Any]:
    result = _trade_path_metrics(rows, probabilities, policy)
    windows = len({float(row["close_time"]) for row in rows})
    days = max(1.0 / 96.0, windows / 96.0)
    return {
        **result,
        "observed_test_days_at_q15_cadence": days,
        "trades_per_day": float(result["picks"]) / days,
        "fee_schedule_version": KALSHI_Q15_FEE_SCHEDULE_VERSION,
        "execution_cost_model_version": RTI_EXECUTION_COST_MODEL_VERSION,
    }


def _bin_label(value: float, bins: Sequence[Mapping[str, Any]]) -> str:
    for item in bins:
        low = float(item["minimum_inclusive"])
        high = item.get("maximum_exclusive")
        if value >= low and (high is None or value < float(high)):
            return str(item["label"])
    return "OUTSIDE_FROZEN_BINS"


def _feature(row: Mapping[str, Any], name: str) -> float:
    names = tuple(row["v15_feature_names"])
    return float(row["v15_features"][names.index(name)])


def _dimension_label(
    row: Mapping[str, Any], dimension: str, rule: Mapping[str, Any],
) -> str:
    if dimension == "asset":
        return str(row["asset"]).upper()
    if dimension == "rti_side":
        return str(row["side"]).upper()
    if dimension == "market_regime":
        sign = 1.0 if str(row["side"]).upper() == "YES" else -1.0
        momentum = sign * _feature(row, str(rule["median_momentum_feature"]))
        breadth = sign * _feature(row, str(rule["breadth_feature"]))
        threshold = float(rule["broad_projected_breadth_threshold"])
        if momentum > 0.0 and breadth >= threshold:
            return "BROAD_ALIGNED"
        if momentum > 0.0:
            return "THIN_OR_ISOLATED_ALIGNED"
        if momentum < 0.0 and breadth <= -threshold:
            return "BROAD_OPPOSED"
        return "MIXED_OR_FLAT"
    value = _feature(row, str(rule["source_feature"]))
    transform = str(rule.get("transform") or "identity")
    if transform == "absolute_value":
        value = abs(value)
    elif transform == "expm1":
        value = math.expm1(value)
    elif transform != "identity":
        raise ValueError("v15_test_reporting_transform_invalid")
    return _bin_label(value, list(rule["bins"]))


def _subgroup_report(
    rows: Sequence[Mapping[str, Any]], candidate: Sequence[float],
    control: Sequence[float], market: Sequence[float], *,
    policy: Mapping[str, Any], reporting_protocol: Mapping[str, Any],
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for dimension, raw_rule in dict(reporting_protocol["dimensions"]).items():
        rule = dict(raw_rule)
        grouped: dict[str, list[int]] = defaultdict(list)
        for index, row in enumerate(rows):
            grouped[_dimension_label(row, dimension, rule)].append(index)
        slices = []
        for label in sorted(grouped):
            indexes = grouped[label]
            slice_rows = [rows[index] for index in indexes]
            slice_candidate = [candidate[index] for index in indexes]
            slice_control = [control[index] for index in indexes]
            slice_market = [market[index] for index in indexes]
            candidate_scores = _scores(slice_rows, slice_candidate)
            control_scores = _scores(slice_rows, slice_control)
            market_scores = _scores(slice_rows, slice_market)
            slices.append({
                "label": label,
                "rows": len(slice_rows),
                "close_windows": len({float(row["close_time"]) for row in slice_rows}),
                "candidate_scores": candidate_scores,
                "v14_scores": control_scores,
                "market_scores": market_scores,
                "candidate_minus_market_brier": candidate_scores["brier_score"] - market_scores["brier_score"],
                "candidate_minus_market_log_loss": candidate_scores["log_loss"] - market_scores["log_loss"],
                "candidate_minus_v14_brier": candidate_scores["brier_score"] - control_scores["brier_score"],
                "candidate_minus_v14_log_loss": candidate_scores["log_loss"] - control_scores["log_loss"],
                "candidate_economics": _economics(slice_rows, slice_candidate, policy),
            })
        output[dimension] = slices
    return {
        "reporting_protocol_id": REPORTING_PROTOCOL_ID,
        "reporting_protocol_sha256": REPORTING_PROTOCOL_SHA256,
        "report_only_not_a_selection_or_gate": True,
        "subgroups": output,
    }


def _validate_existing_reservation(
    reservation: Mapping[str, Any], expected_binding: Mapping[str, Any],
) -> None:
    if reservation.get("status") != RESERVED_STATUS:
        raise ValueError("v15_test_reservation_status_invalid")
    for key, value in expected_binding.items():
        if reservation.get(key) != value:
            raise ValueError(f"v15_test_reservation_binding_mismatch:{key}")
    if (
        reservation.get("untouched_test_labels_read") is not False
        or reservation.get("test_probability_scoring_performed") is not False
        or reservation.get("paper_artifact_created") is not False
        or reservation.get("notification_eligible") is not False
        or reservation.get("automatic_promotion") is not False
        or reservation.get("real_trading_allowed") is not False
    ):
        raise ValueError("v15_test_reservation_safety_invalid")


def _validate_existing_result(
    result: Mapping[str, Any], reservation: Mapping[str, Any],
) -> None:
    report = result.get("report")
    passed = result.get("status") == PASS_STATUS
    checks = (
        report.get("gate_checks") if isinstance(report, Mapping) else None
    )
    predictions = (
        report.get("prediction_rows")
        if isinstance(report, Mapping)
        else None
    )
    label_evidence = result.get("label_read_evidence")
    evidence_required = (
        reservation.get("label_evidence_required") is True
    )
    if evidence_required:
        if not isinstance(label_evidence, Mapping):
            raise ValueError("v15_test_final_result_invalid")
        try:
            prediction_labels = {
                int(row["id"]): int(row["label_yes"])
                for row in predictions or ()
            }
            validated_evidence = validate_label_evidence(
                type(
                    "_StoredVerifiedLabels",
                    (dict,),
                    {"audit_evidence": dict(label_evidence)},
                )(prediction_labels),
                prediction_labels,
                tuple(sorted(prediction_labels)),
                required=True,
                stage="untouched_test",
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("v15_test_final_result_invalid") from exc
        if (
            validated_evidence is None
            or result.get("label_read_evidence_sha256")
            != validated_evidence.get("evidence_sha256")
        ):
            raise ValueError("v15_test_final_result_invalid")
    elif (
        label_evidence is not None
        or result.get("label_read_evidence_sha256") is not None
    ):
        raise ValueError("v15_test_final_result_invalid")
    if (
        result.get("state_version") != TEST_STATE_VERSION
        or result.get("status") not in FINAL_STATUSES
        or result.get("reservation_state_sha256") != reservation.get("state_sha256")
        or result.get("untouched_test_labels_read_once") is not True
        or not isinstance(report, Mapping)
        or result.get("report_sha256") != _canonical_sha256(dict(report))
        or report.get("test_runner_version") != TEST_RUNNER_VERSION
        or report.get("stage") != "UNTOUCHED_TEST_ONE_SHOT"
        or report.get("design_id") != DESIGN_ID
        or report.get("design_sha256") != DESIGN_SHA256
        or report.get("evaluation_protocol_id") != EVALUATION_PROTOCOL_ID
        or report.get("evaluation_protocol_sha256")
        != EVALUATION_PROTOCOL_SHA256
        or report.get("reporting_protocol_id") != REPORTING_PROTOCOL_ID
        or report.get("reporting_protocol_sha256")
        != REPORTING_PROTOCOL_SHA256
        or report.get("cohort") != reservation.get("cohort")
        or report.get("test_row_ids_sha256")
        != reservation.get("untouched_test_row_ids_sha256")
        or not isinstance(checks, Mapping)
        or not checks
        or any(not isinstance(value, bool) for value in checks.values())
        or all(checks.values()) is not passed
        or report.get("gate_met") is not passed
        or report.get("manual_paper_challenger_creation_eligible")
        is not passed
        or report.get("failure_result")
        != (None if passed else "NO_V15_ARTIFACT_OR_PAPER_CHALLENGER")
        or report.get("historical_results_can_promote") is not False
        or report.get("paper_artifact_created") is not False
        or report.get("notification_eligible") is not False
        or report.get("automatic_promotion") is not False
        or report.get("real_trading_allowed") is not False
        or not isinstance(predictions, list)
        or len(predictions) != int(report.get("rows") or 0)
        or report.get("prediction_rows_sha256")
        != _canonical_sha256(predictions)
        or report.get("test_labels_sha256")
        != _canonical_sha256(sorted(
            [int(row["id"]), int(row["label_yes"])]
            for row in predictions
        ))
        or report.get("fee_schedule_version")
        != KALSHI_Q15_FEE_SCHEDULE_VERSION
        or report.get("execution_cost_model_version")
        != RTI_EXECUTION_COST_MODEL_VERSION
        or result.get("paper_artifact_created") is not False
        or result.get("notification_eligible") is not False
        or result.get("automatic_promotion") is not False
        or result.get("real_trading_allowed") is not False
    ):
        raise ValueError("v15_test_final_result_invalid")

def run_untouched_test_once(
    *, seal: Mapping[str, Any], selected_feature_rows: Sequence[Mapping[str, Any]],
    pretest_labels: Mapping[int, int],
    supplied_walk_forward_report: Mapping[str, Any],
    supplied_calibration_report: Mapping[str, Any],
    design: Mapping[str, Any], protocol: Mapping[str, Any],
    reporting_protocol: Mapping[str, Any], cohort: str,
    reservation_path: Path, confirmation: str,
    read_untouched_test_labels: Callable[[Sequence[int]], Mapping[int, int]],
    require_label_evidence: bool = False,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Score one sealed cohort once; existing reservations never rescore."""
    _validate_design_protocol(design, protocol)
    frozen_reporting = load_reporting_protocol(DEFAULT_REPORTING_PROTOCOL)
    if (
        design_fingerprint(reporting_protocol) != REPORTING_PROTOCOL_SHA256
        or dict(reporting_protocol) != frozen_reporting
    ):
        raise ValueError("v15_test_reporting_protocol_identity_mismatch")
    if cohort not in walk.COHORT_ASSETS:
        raise ValueError("v15_test_unsupported_cohort")

    _, pretest_unlabeled, test_unlabeled = _prepare_projected_rows(
        selected_feature_rows, seal=seal, cohort=cohort, protocol=protocol,
    )
    pretest_ids = tuple(sorted(int(row["id"]) for row in pretest_unlabeled))
    test_ids = tuple(sorted(int(row["id"]) for row in test_unlabeled))
    authorized_pretest_labels = _validated_labels(
        pretest_labels, pretest_ids, stage="pretest",
    )
    pretest = _attach_labels(pretest_unlabeled, authorized_pretest_labels)
    verified_walk, verified_calibration = _verify_prior_reports(
        pretest, cohort=cohort, design=design, protocol=protocol,
        supplied_walk=supplied_walk_forward_report,
        supplied_calibration=supplied_calibration_report,
    )

    config = dict(design["fixed_training_config"])
    candidate_trust = dict(verified_calibration["final_candidate_trust_selection"])
    control_trust = dict(verified_calibration["final_v14_trust_selection"])
    candidate_training = walk._with_features(pretest, "v15_features")
    control_training = walk._with_features(pretest, "v14_features")
    candidate_model = fit_residual_model(candidate_training, config)
    control_model = fit_residual_model(control_training, config)
    expected_binding = {
        "state_version": TEST_STATE_VERSION,
        "test_runner_version": TEST_RUNNER_VERSION,
        "design_id": DESIGN_ID,
        "design_sha256": DESIGN_SHA256,
        "evaluation_protocol_id": EVALUATION_PROTOCOL_ID,
        "evaluation_protocol_sha256": EVALUATION_PROTOCOL_SHA256,
        "reporting_protocol_id": REPORTING_PROTOCOL_ID,
        "reporting_protocol_sha256": REPORTING_PROTOCOL_SHA256,
        "audit_seal_sha256": str(seal["seal_sha256"]),
        "cohort": cohort,
        "selected_feature_evidence_sha256": str(seal["selected_feature_evidence_sha256"]),
        "pretest_row_ids_sha256": _hash_ids(pretest),
        "untouched_test_row_ids_sha256": _hash_ids(test_unlabeled),
        "untouched_test_close_times_sha256": _hash_times(test_unlabeled),
        "walk_forward_report_sha256": _canonical_sha256(verified_walk),
        "calibration_report_sha256": _canonical_sha256(verified_calibration),
        "candidate_trust_sha256": _canonical_sha256(candidate_trust),
        "v14_trust_sha256": _canonical_sha256(control_trust),
        "candidate_model_sha256": _canonical_sha256(candidate_model),
        "v14_model_sha256": _canonical_sha256(control_model),
        "entry_policy_sha256": _canonical_sha256(dict(protocol["entry_policy"])),
        "fee_schedule_version": KALSHI_Q15_FEE_SCHEDULE_VERSION,
        "execution_cost_model_version": RTI_EXECUTION_COST_MODEL_VERSION,
        "label_evidence_required": bool(require_label_evidence),
    }
    result_path = result_path_for(reservation_path)
    if reservation_path.exists():
        reservation = _read_sealed(reservation_path)
        _validate_existing_reservation(reservation, expected_binding)
        if result_path.exists():
            result = _read_sealed(result_path)
            _validate_existing_result(result, reservation)
            return {
                "status": "ALREADY_FINALIZED_NO_RESCORE",
                "untouched_test_labels_read_this_call": False,
                "reservation": reservation,
                "result": result,
            }
        return {
            "status": "AMBIGUOUS_RESERVED_NO_RESCORE",
            "untouched_test_labels_read_this_call": False,
            "reservation": reservation,
            "result": None,
        }
    if result_path.exists():
        raise ValueError("v15_test_result_exists_without_reservation")
    if confirmation != CONFIRMATION_PHRASE:
        raise ValueError("v15_test_explicit_one_shot_confirmation_required")

    reserved_at = _now_iso(timestamp)
    reservation = _write_exclusive(reservation_path, {
        **expected_binding,
        "status": RESERVED_STATUS,
        "reserved_at": reserved_at,
        "untouched_test_labels_read": False,
        "test_probability_scoring_performed": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    })

    candidate_test = walk._with_features(test_unlabeled, "v15_features")
    control_test = walk._with_features(test_unlabeled, "v14_features")
    candidate_base, candidate_diagnostics = predict_probabilities(
        candidate_model, candidate_test, config,
    )
    control_base, control_diagnostics = predict_probabilities(
        control_model, control_test, config,
    )
    candidate = [float(value) for value in apply_residual_trust(
        candidate_test, candidate_base, candidate_trust,
    )]
    control = [float(value) for value in apply_residual_trust(
        control_test, control_base, control_trust,
    )]
    market = [float(row["market_yes_probability"]) for row in test_unlabeled]

    raw_test_labels = read_untouched_test_labels(test_ids)
    test_labels = _validated_labels(raw_test_labels, test_ids, stage="untouched_test")
    label_evidence = validate_label_evidence(
        raw_test_labels,
        test_labels,
        test_ids,
        required=require_label_evidence,
        stage="untouched_test",
    )
    test = _attach_labels(test_unlabeled, test_labels)
    bootstrap = dict(protocol["paired_close_window_bootstrap"])
    vs_market = walk._comparison(
        test, candidate, market, comparator_name="MARKET",
        seed=int(bootstrap["candidate_minus_market_random_seed"]),
    )
    vs_v14 = walk._comparison(
        test, candidate, control, comparator_name="V14",
        seed=int(bootstrap["candidate_minus_v14_random_seed"]),
    )
    candidate_scores = _scores(test, candidate)
    control_scores = _scores(test, control)
    market_scores = _scores(test, market)
    policy = dict(protocol["entry_policy"])
    economics = {
        "candidate": _economics(test, candidate, policy),
        "v14": _economics(test, control, policy),
        "market": _economics(test, market, policy),
    }
    gate = dict(protocol["walk_forward_gate"])
    test_policy = dict(protocol["untouched_test_policy"])
    market_bootstrap = vs_market["paired_close_window_bootstrap"]
    v14_bootstrap = vs_v14["paired_close_window_bootstrap"]
    checks = {
        "candidate_brier_effect_floor_vs_market": (
            vs_market["candidate_minus_comparator_brier"]
            <= float(gate["aggregate_candidate_minus_market_brier_mean_must_be_at_most"])
        ),
        "candidate_log_loss_effect_floor_vs_market": (
            vs_market["candidate_minus_comparator_log_loss"]
            <= float(gate["aggregate_candidate_minus_market_log_loss_mean_must_be_at_most"])
        ),
        "market_brier_bootstrap_upper_effect_floor": (
            float(market_bootstrap["brier_delta"]["one_sided_upper"])
            <= float(gate["aggregate_candidate_minus_market_bootstrap_upper_must_be_at_most"])
        ),
        "market_log_loss_bootstrap_upper_effect_floor": (
            float(market_bootstrap["log_loss_delta"]["one_sided_upper"])
            <= float(gate["aggregate_candidate_minus_market_bootstrap_upper_must_be_at_most"])
        ),
        "candidate_brier_effect_floor_vs_v14": (
            vs_v14["candidate_minus_comparator_brier"]
            <= float(gate["aggregate_candidate_minus_v14_brier_mean_must_be_at_most"])
        ),
        "candidate_log_loss_effect_floor_vs_v14": (
            vs_v14["candidate_minus_comparator_log_loss"]
            <= float(gate["aggregate_candidate_minus_v14_log_loss_mean_must_be_at_most"])
        ),
        "v14_brier_bootstrap_upper_below_zero": (
            float(v14_bootstrap["brier_delta"]["one_sided_upper"]) < 0.0
        ),
        "v14_log_loss_bootstrap_upper_below_zero": (
            float(v14_bootstrap["log_loss_delta"]["one_sided_upper"]) < 0.0
        ),
        "minimum_simulated_picks": (
            int(economics["candidate"]["picks"]) >= int(test_policy["minimum_simulated_picks"])
        ),
        "positive_fee_slippage_adjusted_pnl": (
            float(economics["candidate"]["ten_contract_net_pnl_dollars"]) > 0.0
        ),
    }
    passed = all(checks.values())
    predictions = [
        {
            "id": int(row["id"]),
            "close_time": float(row["close_time"]),
            "asset": str(row["asset"]),
            "rti_side": str(row["side"]),
            "label_yes": int(row["label_yes"]),
            "candidate_yes_probability": candidate[index],
            "v14_yes_probability": control[index],
            "market_yes_probability": market[index],
            "candidate_out_of_distribution": bool(candidate_diagnostics[index]["out_of_distribution"]),
            "v14_out_of_distribution": bool(control_diagnostics[index]["out_of_distribution"]),
            "yes_ask_cents": float(row["yes_ask_cents"]),
            "no_ask_cents": float(row["no_ask_cents"]),
            "yes_depth_contracts": float(row["yes_depth_contracts"]),
            "no_depth_contracts": float(row["no_depth_contracts"]),
            "yes_depth_available": bool(row["yes_depth_available"]),
            "no_depth_available": bool(row["no_depth_available"]),
            "spread_cents": float(row["spread_cents"]),
        }
        for index, row in enumerate(test)
    ]
    report = {
        "test_runner_version": TEST_RUNNER_VERSION,
        "stage": "UNTOUCHED_TEST_ONE_SHOT",
        "design_id": DESIGN_ID,
        "design_sha256": DESIGN_SHA256,
        "evaluation_protocol_id": EVALUATION_PROTOCOL_ID,
        "evaluation_protocol_sha256": EVALUATION_PROTOCOL_SHA256,
        "reporting_protocol_id": REPORTING_PROTOCOL_ID,
        "reporting_protocol_sha256": REPORTING_PROTOCOL_SHA256,
        "cohort": cohort,
        "rows": len(test),
        "close_windows": len({float(row["close_time"]) for row in test}),
        "candidate_market_v14_identical_rows": True,
        "btc_and_non_btc_pooled": False,
        "same_close_assets_share_partition": True,
        "untouched_test_rows_used_for_selection": 0,
        "untouched_test_labels_used_for_factor_selection": False,
        "untouched_test_labels_read_once": True,
        "accuracy_is_report_only": True,
        "candidate_scores": candidate_scores,
        "v14_scores": control_scores,
        "market_scores": market_scores,
        "candidate_vs_market": vs_market,
        "candidate_vs_v14": vs_v14,
        "economics": economics,
        "subgroup_reporting": _subgroup_report(
            test, candidate, control, market,
            policy=policy, reporting_protocol=reporting_protocol,
        ),
        "gate_checks": checks,
        "gate_met": passed,
        "failure_result": None if passed else "NO_V15_ARTIFACT_OR_PAPER_CHALLENGER",
        "historical_results_can_promote": False,
        "manual_paper_challenger_creation_eligible": passed,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
        "test_row_ids_sha256": _hash_ids(test),
        "test_labels_sha256": _canonical_sha256(sorted(
            [int(row["id"]), int(row["label_yes"])] for row in test
        )),
        "prediction_rows_sha256": _canonical_sha256(predictions),
        "prediction_rows": predictions,
        "fee_schedule_version": KALSHI_Q15_FEE_SCHEDULE_VERSION,
        "execution_cost_model_version": RTI_EXECUTION_COST_MODEL_VERSION,
    }
    final_status = PASS_STATUS if passed else REJECT_STATUS
    result = _write_exclusive(result_path, {
        "state_version": TEST_STATE_VERSION,
        "status": final_status,
        "finalized_at": _now_iso(timestamp),
        "reservation_state_sha256": reservation["state_sha256"],
        "untouched_test_labels_read_once": True,
        "report_sha256": _canonical_sha256(report),
        "report": report,
        "label_read_evidence": label_evidence,
        "label_read_evidence_sha256": (
            label_evidence["evidence_sha256"]
            if label_evidence is not None
            else None
        ),
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    })
    _validate_existing_result(result, reservation)
    return {
        "status": final_status,
        "untouched_test_labels_read_this_call": True,
        "reservation": reservation,
        "result": result,
    }
