"""Outcome-blind integrity/readiness audit for the preregistered RTI path source."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots.rti_independent_path import (
    DERIVED_FEATURE_KEYS,
    MINIMUM_POINTS_PER_VENUE,
    validate_persisted_independent_path,
)
from q15_upgrade.strategy_bots.rti_independent_path_identity import (
    DESIGN_ID,
    DESIGN_SHA256,
    FIRST_ELIGIBLE_CLOSE_TIME,
)
from q15_upgrade.strategy_bots.rti_independent_path_geometry_identity import (
    PREREGISTERED_COMPLETE_WINDOWS,
    PROTOCOL_ID as GEOMETRY_PROTOCOL_ID,
    PROTOCOL_SHA256 as GEOMETRY_PROTOCOL_SHA256,
    REVIEW_WINDOWS as PINNED_GEOMETRY_REVIEW_WINDOWS,
)
from tools.q15_rti_microstructure_freeze import (
    FEATURE_SELECT_COLUMNS,
    OUTCOME_COLUMNS,
    load_feature_rows,
)
from tools.q15_rti_microstructure_preregister import design_fingerprint
from tools.q15_rti_independent_path_reference import (
    REFERENCE_VERSION,
    verify_reference_formulas,
)


AUDIT_VERSION = "q15-rti-independent-path-outcome-blind-audit-v7"
SELECTED_EVIDENCE_IDENTITY_VERSION = (
    "q15-rti-independent-path-selected-feature-evidence-sha256-v1"
)
CONTRACT_IDENTITY_VERSION = (
    "q15-rti-exact-kalshi-contract-identity-alignment-v1"
)
DEFAULT_DB = ROOT / "data" / "q15_strategy_bots_v3.sqlite3"
DEFAULT_DESIGN = ROOT / "config" / "q15_rti_independent_path_design_v1.json"
DEFAULT_GEOMETRY_PROTOCOL = (
    ROOT / "config"
    / "q15_rti_independent_path_geometry_review_protocol_v1.json"
)
DEFAULT_OUTPUT = ROOT / "reports" / "q15_rti_independent_path_audit_live"
EXPECTED_ASSETS = frozenset({"BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP"})
GEOMETRY_REVIEW_WINDOWS = PINNED_GEOMETRY_REVIEW_WINDOWS
GEOMETRY_MINIMUM_STD = 1e-12
HIGH_CORRELATION_THRESHOLD = 0.95
_TICKER_PATTERN = re.compile(
    r"^KX(?P<asset>BTC|ETH|SOL|XRP|DOGE|BNB|HYPE)15M-"
    r"(?P<year>\d{2})(?P<month>[A-Z]{3})(?P<day>\d{2})"
    r"(?P<hour>\d{2})(?P<minute>\d{2})-(?P<suffix>\d{1,2})$"
)
_MONTHS = {
    name: index for index, name in enumerate((
        "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
        "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
    ), start=1)
}
_EASTERN = ZoneInfo("America/New_York")


def validate_exact_contract_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    """Validate that one exact row names its own asset and close contract."""
    ticker = str(row.get("ticker") or "")
    asset = str(row.get("asset") or "").upper()
    close_time = _num(row.get("close_time"))
    match = _TICKER_PATTERN.fullmatch(ticker)
    errors: list[str] = []
    encoded_close_times: list[float] = []
    if match is None:
        errors.append("ticker_format_mismatch")
    else:
        groups = match.groupdict()
        if groups["asset"] != asset:
            errors.append("ticker_asset_mismatch")
        try:
            year = 2000 + int(groups["year"])
            month = _MONTHS[groups["month"]]
            day = int(groups["day"])
            hour = int(groups["hour"])
            minute = int(groups["minute"])
            if int(groups["suffix"]) != minute:
                errors.append("ticker_suffix_minute_mismatch")
            for fold in (0, 1):
                encoded_close_times.append(datetime(
                    year, month, day, hour, minute,
                    tzinfo=_EASTERN, fold=fold,
                ).timestamp())
        except (KeyError, TypeError, ValueError):
            errors.append("ticker_datetime_invalid")
        if close_time is None or not any(
            abs(float(close_time) - candidate) <= 1e-6
            for candidate in encoded_close_times
        ):
            errors.append("ticker_close_time_mismatch")
    return {
        "version": CONTRACT_IDENTITY_VERSION,
        "valid": not errors,
        "errors": errors,
        "ticker": ticker,
        "asset": asset,
        "close_time": close_time,
        "encoded_close_times": sorted(set(encoded_close_times)),
        "outcome_labels_read": False,
    }


def selected_feature_evidence_identity(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Commit to the exact outcome-free rows behind the geometry summary."""
    identity_keys = {
        "asset", "ticker", "side", "close_time", "source_captured_at",
        "evidence_as_of",
    }
    projected = []
    for row in rows:
        selected = {
            str(key): value
            for key, value in row.items()
            if str(key) in identity_keys
            or str(key).startswith("rti_independent_path_")
        }
        if OUTCOME_COLUMNS.intersection(selected):
            raise AssertionError(
                "selected_feature_evidence_identity_contains_outcome"
            )
        projected.append(selected)
    projected.sort(key=lambda row: (
        float(row.get("close_time") or 0.0),
        str(row.get("asset") or ""),
        str(row.get("ticker") or ""),
    ))
    encoded = json.dumps(
        projected, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    return {
        "version": SELECTED_EVIDENCE_IDENTITY_VERSION,
        "rows": len(projected),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
    }


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def validate_design(design: Mapping[str, Any]) -> None:
    if design.get("design_id") != DESIGN_ID:
        raise ValueError("independent_path_design_id_mismatch")
    if design_fingerprint(design) != DESIGN_SHA256:
        raise ValueError("independent_path_design_sha256_mismatch")
    if float(design.get("first_eligible_close_time") or 0.0) != (
        FIRST_ELIGIBLE_CLOSE_TIME
    ):
        raise ValueError("independent_path_boundary_mismatch")
    if design.get("outcome_labels_read_before_freeze") is not False:
        raise ValueError("independent_path_label_guard_missing")
    if design.get("historical_credit_allowed") is not False:
        raise ValueError("independent_path_historical_credit_guard_missing")
    if design.get("notification_eligible") is not False:
        raise ValueError("independent_path_notification_guard_missing")
    if design.get("real_trading_allowed") is not False:
        raise ValueError("independent_path_trading_guard_missing")
    features = list(design.get("fixed_candidate_features") or ())
    if len(features) != 5 or {
        str(item.get("source_key")) for item in features if isinstance(item, Mapping)
    } != set(DERIVED_FEATURE_KEYS):
        raise ValueError("independent_path_feature_binding_mismatch")


def load_geometry_protocol(
    path: Path = DEFAULT_GEOMETRY_PROTOCOL,
) -> dict[str, Any]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, Mapping):
        raise ValueError("independent_path_geometry_protocol_root_not_object")
    return dict(decoded)


def validate_geometry_protocol(protocol: Mapping[str, Any]) -> None:
    if design_fingerprint(protocol) != GEOMETRY_PROTOCOL_SHA256:
        raise ValueError("independent_path_geometry_protocol_sha256_mismatch")
    if (
        protocol.get("protocol_id") != GEOMETRY_PROTOCOL_ID
        or protocol.get("protocol_status")
        != "PREREGISTERED_AFTER_12_WINDOW_OUTCOME_BLIND_PREVIEW_BEFORE_30_WINDOW_REVIEW"
        or protocol.get("applies_to_source_design_id") != DESIGN_ID
        or protocol.get("applies_to_source_design_sha256") != DESIGN_SHA256
    ):
        raise ValueError("independent_path_geometry_protocol_identity_mismatch")
    evidence = protocol.get("evidence_available_at_preregistration")
    if not isinstance(evidence, Mapping) or (
        int(evidence.get("complete_reconstructable_close_windows") or -1)
        != PREREGISTERED_COMPLETE_WINDOWS
        or evidence.get("outcome_blind_feature_ranges_inspected") is not True
        or evidence.get("outcome_blind_geometry_inspected") is not True
        or evidence.get("outcome_blind_source_quality_inspected") is not True
        or evidence.get("outcome_labels_read") is not False
        or evidence.get("model_fit_performed") is not False
        or evidence.get("performance_metrics_inspected") is not False
    ):
        raise ValueError("independent_path_geometry_protocol_origin_invalid")
    origin = protocol.get("threshold_origin")
    if not isinstance(origin, Mapping) or (
        origin.get("uses_outcomes") is not False
        or origin.get("uses_performance_metrics") is not False
        or float(origin.get(
            "pairwise_correlation_ceiling_reuses_prior_frozen_v13_geometry_standard"
        ) or 0.0) != HIGH_CORRELATION_THRESHOLD
        or float(origin.get(
            "condition_number_ceiling_reuses_prior_frozen_v13_geometry_standard"
        ) or 0.0) != 50.0
        or origin.get("rank_duplicate_and_activity_checks_are_structural")
        is not True
    ):
        raise ValueError("independent_path_geometry_threshold_origin_invalid")
    trigger = protocol.get("review_trigger")
    if not isinstance(trigger, Mapping) or (
        int(trigger.get("complete_reconstructable_close_windows") or 0)
        != GEOMETRY_REVIEW_WINDOWS
        or trigger.get("same_seven_assets_required_per_close") is not True
        or trigger.get("partial_close_windows_forbidden") is not True
        or int(trigger.get("complete_all_seven_rows_required") or 0) != 210
        or int(trigger.get("complete_btc_rows_required") or 0) != 30
        or int(trigger.get("complete_non_btc_transfer_rows_required") or 0)
        != 180
        or trigger.get("source_quality_status_required")
        != "PASS_ALL_CREDITED_COMPLETE_ROWS"
        or int(trigger.get("evidence_parse_failures_must_equal", -1)) != 0
        or int(trigger.get("source_integrity_breaches_must_equal", -1)) != 0
    ):
        raise ValueError("independent_path_geometry_review_trigger_invalid")
    checks = protocol.get("fixed_checks")
    if not isinstance(checks, Mapping) or (
        int(checks.get("candidate_feature_count_must_equal") or 0) != 5
        or int(checks.get("all_seven_active_feature_count_must_equal") or 0)
        != 5
        or int(checks.get("btc_active_feature_count_must_equal") or 0) != 5
        or int(checks.get("non_btc_active_feature_count_must_equal") or 0)
        != 5
        or float(checks.get("pairwise_absolute_correlation_ceiling") or 0.0)
        != HIGH_CORRELATION_THRESHOLD
        or int(checks.get("exact_signed_duplicate_pairs_must_equal", -1)) != 0
        or float(checks.get(
            "condition_number_nonzero_subspace_maximum"
        ) or 0.0) != 50.0
        or int(checks.get(
            "rank_deficiency_vs_active_features_must_equal", -1
        )) != 0
        or checks.get(
            "minimum_source_integrity_margin_seconds_must_be_strictly_positive"
        ) is not True
        or checks.get("all_geometry_values_must_be_finite") is not True
    ):
        raise ValueError("independent_path_geometry_fixed_checks_invalid")
    pass_policy = protocol.get("pass_policy")
    failure_policy = protocol.get("failure_policy")
    if not isinstance(pass_policy, Mapping) or (
        pass_policy.get("status")
        != "PASS_CONTINUE_PROSPECTIVE_COLLECTION_NO_MODEL"
        or pass_policy.get("model_fit_allowed_at_30_windows") is not False
        or pass_policy.get("outcome_labels_may_be_opened_at_30_windows")
        is not False
        or pass_policy.get("continue_to_non_btc_60_and_btc_150_milestones")
        is not True
    ):
        raise ValueError("independent_path_geometry_pass_policy_invalid")
    if not isinstance(failure_policy, Mapping) or (
        failure_policy.get("status")
        != "GEOMETRY_REVIEW_REQUIRED_NO_AUTOMATIC_ACTION"
        or failure_policy.get("any_failed_check_requires_manual_diagnosis")
        is not True
        or failure_policy.get(
            "new_source_or_feature_design_requires_new_preregistration_and_boundary"
        ) is not True
        or failure_policy.get(
            "existing_source_rows_may_not_be_relabeled_for_a_successor"
        ) is not True
    ):
        raise ValueError("independent_path_geometry_failure_policy_invalid")
    for policy in (pass_policy, failure_policy):
        for key in (
            "automatic_feature_selection_allowed",
            "automatic_threshold_change_allowed",
            "automatic_refit_allowed",
            "automatic_activation_allowed",
            "automatic_promotion_allowed",
        ):
            if key in policy and policy.get(key) is not False:
                raise ValueError("independent_path_geometry_automatic_action_guard")
    for key, expected in (
        ("report_only", True),
        ("outcome_labels_forbidden", True),
        ("model_fit_forbidden", True),
        ("entry_policy_changes_forbidden", True),
        ("notification_is_trade_signal", False),
        ("real_trading_allowed", False),
    ):
        if protocol.get(key) is not expected:
            raise ValueError("independent_path_geometry_safety_guard_missing")


def feature_geometry_report(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Describe the five-dimensional evidence without labels or fitting.

    Geometry is computed only after the caller has reconstructed and validated
    every persisted path.  Centering and scaling are descriptive operations;
    no target, settlement field, model, threshold search, or feature selection
    is involved.
    """
    names = tuple(DERIVED_FEATURE_KEYS)
    matrix = np.asarray([
        [float(row[name]) for name in names]
        for row in rows
    ], dtype=np.float64)
    if not rows:
        matrix = np.empty((0, len(names)), dtype=np.float64)
    finite = bool(np.isfinite(matrix).all())
    statistics: dict[str, dict[str, Any]] = {}
    active_indexes: list[int] = []
    for index, name in enumerate(names):
        values = matrix[:, index] if len(matrix) else np.asarray([], dtype=float)
        if not len(values) or not bool(np.isfinite(values).all()):
            statistics[name] = {
                "n": int(len(values)),
                "mean": None,
                "population_std": None,
                "min": None,
                "max": None,
                "unique_6dp": 0,
                "active": False,
            }
            continue
        std = float(np.std(values))
        active = std > GEOMETRY_MINIMUM_STD
        if active:
            active_indexes.append(index)
        statistics[name] = {
            "n": int(len(values)),
            "mean": float(np.mean(values)),
            "population_std": std,
            "min": float(np.min(values)),
            "max": float(np.max(values)),
            "unique_6dp": len({round(float(value), 6) for value in values}),
            "active": active,
        }

    pairs: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    for left_offset, left in enumerate(active_indexes):
        left_values = matrix[:, left]
        for right in active_indexes[left_offset + 1:]:
            right_values = matrix[:, right]
            correlation = float(np.corrcoef(left_values, right_values)[0, 1])
            entry = {
                "left": names[left],
                "right": names[right],
                "correlation": correlation,
            }
            pairs.append(entry)
            same = bool(np.allclose(left_values, right_values, rtol=0.0, atol=1e-12))
            opposite = bool(np.allclose(left_values, -right_values, rtol=0.0, atol=1e-12))
            if same or opposite:
                duplicates.append({
                    "left": names[left],
                    "right": names[right],
                    "relationship": "same" if same else "opposite",
                })
    pairs.sort(key=lambda item: (
        -abs(float(item["correlation"])), item["left"], item["right"],
    ))

    singular_values: list[float] = []
    rank = 0
    stable_rank = None
    condition = None
    tolerance = None
    if len(matrix) and active_indexes and finite:
        active = matrix[:, active_indexes]
        active = (active - active.mean(axis=0)) / active.std(axis=0)
        active -= active.mean(axis=0, keepdims=True)
        singular = np.linalg.svd(active, compute_uv=False)
        singular_values = [float(value) for value in singular]
        largest = float(singular[0]) if singular.size else 0.0
        tolerance = float(max(active.shape) * np.finfo(np.float64).eps * largest)
        nonzero = singular[singular > tolerance]
        rank = int(nonzero.size)
        if largest > 0.0:
            stable_rank = float(np.square(singular).sum() / (largest * largest))
        if rank:
            condition = float(largest / float(nonzero[-1]))

    return {
        "rows": len(rows),
        "feature_count": len(names),
        "finite": finite,
        "minimum_active_population_std": GEOMETRY_MINIMUM_STD,
        "active_feature_count": len(active_indexes),
        "active_feature_names": [names[index] for index in active_indexes],
        "numerical_rank": rank,
        "rank_deficiency_vs_active_features": len(active_indexes) - rank,
        "stable_rank": stable_rank,
        "condition_number_nonzero_subspace": condition,
        "singular_value_tolerance": tolerance,
        "singular_values": singular_values,
        "maximum_absolute_correlation": (
            None if not pairs else abs(float(pairs[0]["correlation"]))
        ),
        "maximum_absolute_correlation_pair": None if not pairs else pairs[0],
        "high_absolute_correlation_threshold": HIGH_CORRELATION_THRESHOLD,
        "high_absolute_correlation_pairs": [
            item for item in pairs
            if abs(float(item["correlation"])) >= HIGH_CORRELATION_THRESHOLD
        ],
        "exact_signed_duplicate_pairs": duplicates,
        "feature_statistics": statistics,
        "outcome_labels_read": False,
        "model_fit_performed": False,
    }


def _distribution(values: Sequence[float]) -> dict[str, Any]:
    finite = np.asarray([
        float(value) for value in values if math.isfinite(float(value))
    ], dtype=np.float64)
    if not finite.size:
        return {
            "n": 0, "min": None, "p10": None, "median": None,
            "p90": None, "max": None,
        }
    return {
        "n": int(finite.size),
        "min": float(np.min(finite)),
        "p10": float(np.percentile(finite, 10)),
        "median": float(np.median(finite)),
        "p90": float(np.percentile(finite, 90)),
        "max": float(np.max(finite)),
    }


def source_quality_report(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize preregistered source-quality margins without labels."""
    by_venue: dict[str, dict[str, list[float]]] = {
        venue: defaultdict(list) for venue in ("coinbase", "kraken")
    }
    by_asset_margin: dict[str, list[float]] = defaultdict(list)
    evidence_parse_failures = 0
    integrity_breaches = 0
    for row in rows:
        try:
            evidence = json.loads(str(row["rti_independent_path_evidence_json"]))
            evidence_venues = dict(evidence["venues"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            evidence_parse_failures += 1
            continue
        allowed = _num(row.get("rti_independent_path_max_gap_seconds_allowed"))
        if allowed is None:
            integrity_breaches += 1
            continue
        asset = str(row.get("asset") or "").upper()
        row_margins = []
        for venue in ("coinbase", "kraken"):
            prefix = f"rti_independent_path_{venue}_"
            points = list(evidence_venues.get(venue) or ())
            latest_message_age = (
                _num(dict(points[-1]).get("last_message_age_seconds"))
                if points else None
            )
            values = {
                "point_count": _num(row.get(prefix + "point_count")),
                "start_age_seconds": _num(row.get(prefix + "start_age_seconds")),
                "end_age_seconds": _num(row.get(prefix + "end_age_seconds")),
                "max_gap_seconds": _num(row.get(prefix + "max_gap_seconds")),
                "max_message_age_seconds": _num(
                    row.get(prefix + "max_message_age_seconds")
                ),
                "latest_message_age_seconds": latest_message_age,
            }
            if any(value is None for value in values.values()):
                integrity_breaches += 1
                continue
            numeric = {key: float(value) for key, value in values.items()}
            end_effective = (
                numeric["end_age_seconds"]
                + numeric["latest_message_age_seconds"]
            )
            margin = allowed - max(
                numeric["start_age_seconds"],
                end_effective,
                numeric["max_gap_seconds"],
                numeric["max_message_age_seconds"],
            )
            metrics = by_venue[venue]
            for key, value in numeric.items():
                metrics[key].append(value)
            metrics["end_effective_age_seconds"].append(end_effective)
            metrics["minimum_integrity_margin_seconds"].append(margin)
            row_margins.append(margin)
            if (
                numeric["point_count"] < MINIMUM_POINTS_PER_VENUE
                or margin < -1e-6
            ):
                integrity_breaches += 1
        if len(row_margins) == 2:
            by_asset_margin[asset].append(min(row_margins))
    venue_report = {
        venue: {
            "rows": len(metrics["point_count"]),
            "metrics": {
                key: _distribution(values)
                for key, values in sorted(metrics.items())
            },
        }
        for venue, metrics in by_venue.items()
    }
    all_margins = [
        value
        for venue in venue_report.values()
        for value in [
            dict(dict(venue.get("metrics") or {}).get(
                "minimum_integrity_margin_seconds"
            ) or {}).get("min")
        ]
        if value is not None
    ]
    clean = bool(
        rows
        and evidence_parse_failures == 0
        and integrity_breaches == 0
        and all(
            int(raw.get("rows") or 0) == len(rows)
            for raw in venue_report.values()
        )
    )
    return {
        "status": (
            "PASS_ALL_CREDITED_COMPLETE_ROWS"
            if clean else
            "WAITING_FOR_CREDITED_COMPLETE_ROWS"
            if not rows else
            "SOURCE_QUALITY_REVIEW_REQUIRED"
        ),
        "credited_complete_rows": len(rows),
        "source_thresholds_from_frozen_design": True,
        "thresholds_selected_from_outcomes": False,
        "outcome_labels_read": False,
        "evidence_parse_failures": evidence_parse_failures,
        "integrity_breaches": integrity_breaches,
        "minimum_integrity_margin_seconds": (
            None if not all_margins else min(float(value) for value in all_margins)
        ),
        "venues": venue_report,
        "minimum_integrity_margin_by_asset": {
            asset: _distribution(values)
            for asset, values in sorted(by_asset_margin.items())
        },
    }


def evaluate_geometry_review(
    report: Mapping[str, Any], protocol: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply only the preregistered structural checks; never read labels."""
    validate_geometry_protocol(protocol)
    evidence = dict(report.get("geometry_review_evidence") or {})
    windows = int(evidence.get("complete_close_windows") or 0)
    cohorts = dict(evidence.get("cohorts") or {})
    source_quality = dict(evidence.get("source_quality") or {})
    checks_config = dict(protocol["fixed_checks"])
    trigger = dict(protocol["review_trigger"])
    expected_rows = {
        "ALL_SEVEN": int(trigger["complete_all_seven_rows_required"]),
        "BTC": int(trigger["complete_btc_rows_required"]),
        "NON_BTC_TRANSFER": int(
            trigger["complete_non_btc_transfer_rows_required"]
        ),
    }

    def _all_cohorts(predicate: Any) -> bool:
        return bool(
            set(cohorts) == set(expected_rows)
            and all(predicate(dict(cohorts[name])) for name in expected_rows)
        )

    checks = {
        "trigger_complete_window_count_exact": windows == GEOMETRY_REVIEW_WINDOWS,
        "trigger_complete_row_counts_exact": all(
            int(dict(cohorts.get(name) or {}).get("rows") or 0) == rows
            for name, rows in expected_rows.items()
        ),
        "source_quality_pass": bool(
            source_quality.get("status")
            == trigger["source_quality_status_required"]
            and int(source_quality.get("evidence_parse_failures") or 0) == 0
            and int(source_quality.get("integrity_breaches") or 0) == 0
        ),
        "source_integrity_margin_positive": bool(
            _num(source_quality.get("minimum_integrity_margin_seconds"))
            is not None
            and float(source_quality["minimum_integrity_margin_seconds"]) > 0.0
        ),
        "all_geometry_finite": _all_cohorts(
            lambda raw: raw.get("finite") is True
        ),
        "candidate_feature_count_exact": _all_cohorts(
            lambda raw: int(raw.get("feature_count") or 0)
            == int(checks_config["candidate_feature_count_must_equal"])
        ),
        "all_active_feature_counts_exact": bool(
            int(dict(cohorts.get("ALL_SEVEN") or {}).get(
                "active_feature_count"
            ) or 0) == int(checks_config[
                "all_seven_active_feature_count_must_equal"
            ])
            and int(dict(cohorts.get("BTC") or {}).get(
                "active_feature_count"
            ) or 0) == int(checks_config[
                "btc_active_feature_count_must_equal"
            ])
            and int(dict(cohorts.get("NON_BTC_TRANSFER") or {}).get(
                "active_feature_count"
            ) or 0) == int(checks_config[
                "non_btc_active_feature_count_must_equal"
            ])
        ),
        "all_pairwise_correlations_within_ceiling": _all_cohorts(
            lambda raw: (
                _num(raw.get("maximum_absolute_correlation")) is not None
                and float(raw["maximum_absolute_correlation"])
                <= float(checks_config[
                    "pairwise_absolute_correlation_ceiling"
                ])
            )
        ),
        "no_exact_signed_duplicates": _all_cohorts(
            lambda raw: len(list(raw.get("exact_signed_duplicate_pairs") or ()))
            == int(checks_config["exact_signed_duplicate_pairs_must_equal"])
        ),
        "all_condition_numbers_within_ceiling": _all_cohorts(
            lambda raw: (
                _num(raw.get("condition_number_nonzero_subspace")) is not None
                and float(raw["condition_number_nonzero_subspace"])
                <= float(checks_config[
                    "condition_number_nonzero_subspace_maximum"
                ])
            )
        ),
        "all_active_matrices_full_rank": _all_cohorts(
            lambda raw: int(raw.get("rank_deficiency_vs_active_features") or 0)
            == int(checks_config[
                "rank_deficiency_vs_active_features_must_equal"
            ])
        ),
    }
    ready = windows >= GEOMETRY_REVIEW_WINDOWS
    all_met = bool(all(checks.values()))
    status = (
        "WAITING_FOR_30_COMPLETE_WINDOWS"
        if not ready else
        str(protocol["pass_policy"]["status"])
        if all_met else
        str(protocol["failure_policy"]["status"])
    )
    return {
        "protocol_id": GEOMETRY_PROTOCOL_ID,
        "protocol_sha256": GEOMETRY_PROTOCOL_SHA256,
        "review_window": GEOMETRY_REVIEW_WINDOWS,
        "review_ready": ready,
        "status": status,
        "checks": checks,
        "failed_checks": sorted(
            name for name, passed in checks.items() if not passed
        ),
        "all_checks_met": all_met,
        "pass_does_not_authorize_model_fit": True,
        "pass_does_not_authorize_outcome_access": True,
        "automatic_action_allowed": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
    }


def build_report(
    rows: Sequence[Mapping[str, Any]], design: Mapping[str, Any],
    geometry_protocol: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    validate_design(design)
    protocol = (
        load_geometry_protocol()
        if geometry_protocol is None else dict(geometry_protocol)
    )
    validate_geometry_protocol(protocol)
    if OUTCOME_COLUMNS.intersection(FEATURE_SELECT_COLUMNS):
        raise AssertionError("independent_path_feature_projection_contains_outcome")
    eligible = [
        dict(row) for row in rows
        if _num(row.get("close_time")) is not None
        and float(row["close_time"]) >= FIRST_ELIGIBLE_CLOSE_TIME
    ]
    pre_boundary_captured = sum(
        row.get("rti_independent_path_status") == "ok"
        and _num(row.get("close_time")) is not None
        and float(row["close_time"]) < FIRST_ELIGIBLE_CLOSE_TIME
        for row in rows
    )
    validations: list[dict[str, Any]] = []
    valid_rows: list[dict[str, Any]] = []
    error_counts: Counter[str] = Counter()
    by_asset: dict[str, Counter[str]] = defaultdict(Counter)
    for row in eligible:
        validation = validate_persisted_independent_path(row)
        contract_identity = validate_exact_contract_identity(row)
        reference = (
            verify_reference_formulas(row)
            if validation["valid"]
            else {
                "valid": False,
                "errors": [],
                "reference_version": REFERENCE_VERSION,
            }
        )
        errors = [
            *validation["errors"], *reference["errors"],
            *contract_identity["errors"],
        ]
        asset = str(row.get("asset") or "").upper()
        entry = {
            "id": row.get("id"),
            "asset": asset,
            "close_time": _num(row.get("close_time")),
            "valid": bool(
                validation["valid"]
                and reference["valid"]
                and contract_identity["valid"]
            ),
            "errors": errors,
            "reference_formula_verified": bool(reference["valid"]),
            "contract_identity_verified": bool(contract_identity["valid"]),
            "source_missing_reason": row.get(
                "rti_independent_path_missing_reason"
            ),
            "coinbase_missing_reason": row.get(
                "rti_independent_path_coinbase_missing_reason"
            ),
            "kraken_missing_reason": row.get(
                "rti_independent_path_kraken_missing_reason"
            ),
        }
        validations.append(entry)
        by_asset[asset]["eligible_rows"] += 1
        if entry["valid"]:
            valid_rows.append(row)
            by_asset[asset]["valid_rows"] += 1
        else:
            by_asset[asset]["invalid_rows"] += 1
            error_counts.update(entry["errors"])
    grouped: dict[float, list[dict[str, Any]]] = defaultdict(list)
    for row in valid_rows:
        grouped[float(row["close_time"])].append(row)
    complete_times = []
    incomplete_windows = []
    all_eligible_times = sorted({
        float(row["close_time"]) for row in eligible
    })
    for close_time in all_eligible_times:
        window_rows = grouped.get(close_time, [])
        assets = [str(row.get("asset") or "").upper() for row in window_rows]
        if len(window_rows) == 7 and set(assets) == EXPECTED_ASSETS:
            complete_times.append(close_time)
        else:
            invalid_for_close = [
                entry for entry in validations
                if entry["close_time"] == close_time and not entry["valid"]
            ]
            incomplete_windows.append({
                "close_time": close_time,
                "valid_assets": sorted(set(assets)),
                "missing_assets": sorted(EXPECTED_ASSETS - set(assets)),
                "valid_row_count": len(window_rows),
                "source_missing_reasons": {
                    str(entry["asset"]): (
                        entry.get("source_missing_reason")
                        or entry.get("coinbase_missing_reason")
                        or entry.get("kraken_missing_reason")
                        or ",".join(entry.get("errors") or ())
                    )
                    for entry in invalid_for_close
                },
            })
    feature_ranges = {}
    for key in DERIVED_FEATURE_KEYS:
        values = [
            value for row in valid_rows
            for value in [_num(row.get(key))]
            if value is not None
        ]
        feature_ranges[key] = {
            "n": len(values),
            "min": min(values) if values else None,
            "max": max(values) if values else None,
        }
    complete_count = len(complete_times)
    complete_rows = [
        row for row in valid_rows
        if float(row["close_time"]) in set(complete_times)
    ]
    geometry = {
        "ALL_SEVEN": feature_geometry_report(complete_rows),
        "BTC": feature_geometry_report([
            row for row in complete_rows
            if str(row.get("asset") or "").upper() == "BTC"
        ]),
        "NON_BTC_TRANSFER": feature_geometry_report([
            row for row in complete_rows
            if str(row.get("asset") or "").upper() != "BTC"
        ]),
    }
    source_quality = source_quality_report(complete_rows)
    review_close_times = complete_times[:GEOMETRY_REVIEW_WINDOWS]
    review_close_time_set = set(review_close_times)
    review_rows = [
        row for row in complete_rows
        if float(row["close_time"]) in review_close_time_set
    ]
    review_geometry = {
        "ALL_SEVEN": feature_geometry_report(review_rows),
        "BTC": feature_geometry_report([
            row for row in review_rows
            if str(row.get("asset") or "").upper() == "BTC"
        ]),
        "NON_BTC_TRANSFER": feature_geometry_report([
            row for row in review_rows
            if str(row.get("asset") or "").upper() != "BTC"
        ]),
    }
    review_source_quality = source_quality_report(review_rows)
    review_source_quality["selected_feature_evidence_identity"] = (
        selected_feature_evidence_identity(review_rows)
    )
    review_source_quality["contract_identity"] = {
        "version": CONTRACT_IDENTITY_VERSION,
        "rows": len(review_rows),
        "mismatch_rows": 0,
        "ticker_asset_alignment_required": True,
        "ticker_close_time_alignment_required": True,
        "dst_fold_safe": True,
        "outcome_labels_read": False,
    }
    report = {
        "audit_version": AUDIT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "design_id": DESIGN_ID,
        "design_sha256": DESIGN_SHA256,
        "reference_formula_verifier_version": REFERENCE_VERSION,
        "reference_formula_mismatch_rows": sum(
            any(error.startswith("reference_formula_") for error in entry["errors"])
            for entry in validations
        ),
        "contract_identity_version": CONTRACT_IDENTITY_VERSION,
        "contract_identity_mismatch_rows": sum(
            not entry["contract_identity_verified"] for entry in validations
        ),
        "first_eligible_close_time": FIRST_ELIGIBLE_CLOSE_TIME,
        "paper_only": True,
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "artifact_emitted": False,
        "notification_eligible": False,
        "automatic_scoring": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
        "source_rows_scanned": len(rows),
        "pre_boundary_captured_rows_without_credit": pre_boundary_captured,
        "eligible_rows": len(eligible),
        "valid_rows": len(valid_rows),
        "invalid_rows": len(eligible) - len(valid_rows),
        "error_counts": dict(sorted(error_counts.items())),
        "rows_by_asset": {
            asset: dict(by_asset.get(asset, Counter()))
            for asset in sorted(EXPECTED_ASSETS)
        },
        "eligible_close_windows": len(all_eligible_times),
        "complete_seven_asset_close_windows": complete_count,
        "complete_close_times": complete_times,
        "incomplete_windows": incomplete_windows,
        "timestamp_and_reconstruction_integrity_clean": (
            len(eligible) == len(valid_rows) and not incomplete_windows
        ),
        "feature_ranges": feature_ranges,
        "outcome_blind_geometry": {
            "review_window": GEOMETRY_REVIEW_WINDOWS,
            "status": (
                "READY_FOR_MANUAL_OUTCOME_BLIND_GEOMETRY_REVIEW"
                if complete_count >= GEOMETRY_REVIEW_WINDOWS
                else "WAITING_FOR_30_COMPLETE_WINDOWS"
            ),
            "thresholds_selected_from_outcomes": False,
            "feature_selection_performed": False,
            "cohorts": geometry,
        },
        "source_quality": source_quality,
        "geometry_review_evidence": {
            "selection": "EARLIEST_30_COMPLETE_RECONSTRUCTABLE_WINDOWS",
            "complete_close_windows": len(review_close_times),
            "complete_close_times": review_close_times,
            "rows": len(review_rows),
            "cohorts": review_geometry,
            "source_quality": review_source_quality,
            "outcome_columns_selected": False,
            "outcome_labels_read": False,
            "model_fit_performed": False,
        },
        "readiness": {
            "geometry_30_windows_remaining": max(
                0, GEOMETRY_REVIEW_WINDOWS - complete_count,
            ),
            "non_btc_60_windows_remaining": max(0, 60 - complete_count),
            "btc_150_windows_remaining": max(0, 150 - complete_count),
        },
        "invalid_examples": [
            entry for entry in validations if not entry["valid"]
        ][:25],
    }
    report["geometry_review"] = evaluate_geometry_review(report, protocol)
    return report


def render_markdown(report: Mapping[str, Any]) -> str:
    readiness = dict(report["readiness"])
    return "\n".join((
        "# Q15 RTI independent path outcome-blind audit",
        "",
        f"- Design: `{report['design_id']}`",
        f"- Design SHA-256: `{report['design_sha256']}`",
        f"- Eligible rows: {report['eligible_rows']}",
        f"- Valid reconstructable rows: {report['valid_rows']}",
        f"- Invalid rows: {report['invalid_rows']}",
        f"- Complete seven-asset windows: {report['complete_seven_asset_close_windows']}",
        f"- Geometry-30 windows remaining: {readiness['geometry_30_windows_remaining']}",
        f"- Non-BTC-60 windows remaining: {readiness['non_btc_60_windows_remaining']}",
        f"- BTC-150 windows remaining: {readiness['btc_150_windows_remaining']}",
        f"- Integrity clean: {report['timestamp_and_reconstruction_integrity_clean']}",
        f"- Geometry status: {report['outcome_blind_geometry']['status']}",
        f"- Frozen geometry protocol: `{report['geometry_review']['protocol_id']}`",
        f"- Frozen geometry protocol SHA-256: `{report['geometry_review']['protocol_sha256']}`",
        f"- Frozen geometry review status: {report['geometry_review']['status']}",
        f"- Source quality: {report['source_quality']['status']}",
        f"- Worst integrity margin: {report['source_quality']['minimum_integrity_margin_seconds']}",
        "- Outcome labels read: false",
        "- Model fit / alert / promotion / trading: disabled",
        "",
        "This audit reconstructs the five frozen candidate fields from the persisted",
        "canonical pre-decision rows. It does not inspect settlement outcomes.",
    )) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-db", default=str(DEFAULT_DB))
    parser.add_argument("--design", default=str(DEFAULT_DESIGN))
    parser.add_argument(
        "--geometry-protocol", default=str(DEFAULT_GEOMETRY_PROTOCOL),
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    design = json.loads(Path(args.design).read_text(encoding="utf-8"))
    rows = load_feature_rows(Path(args.strategy_db))
    protocol = json.loads(
        Path(args.geometry_protocol).read_text(encoding="utf-8")
    )
    report = build_report(rows, design, protocol)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "audit.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    (output / "audit.md").write_text(
        render_markdown(report), encoding="utf-8",
    )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
