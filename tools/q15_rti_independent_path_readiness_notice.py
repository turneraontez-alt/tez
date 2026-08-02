"""Outcome-blind, idempotent readiness notice for independent RTI paths."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from notifications.outbox_v9 import ReliableTelegramOutbox
from q15_upgrade.strategy_bots.rti_independent_path_identity import (
    DESIGN_ID,
    DESIGN_SHA256,
)
from q15_upgrade.strategy_bots.rti_independent_path_geometry_identity import (
    PROTOCOL_ID as GEOMETRY_PROTOCOL_ID,
    PROTOCOL_SHA256 as GEOMETRY_PROTOCOL_SHA256,
)
from q15_upgrade.strategy_bots.rti_independent_path_geometry_freeze_identity import (
    CONTRACT_ID as GEOMETRY_FREEZE_CONTRACT_ID,
    CONTRACT_SHA256 as GEOMETRY_FREEZE_CONTRACT_SHA256,
)
from q15_upgrade.strategy_bots.rti_independent_path_successor_identity import (
    CHARTER_ID as SUCCESSOR_CHARTER_ID,
    CHARTER_SHA256 as SUCCESSOR_CHARTER_SHA256,
    EVALUATION_PROTOCOL_ID as SUCCESSOR_EVALUATION_PROTOCOL_ID,
    EVALUATION_PROTOCOL_SHA256 as SUCCESSOR_EVALUATION_PROTOCOL_SHA256,
    PROPOSED_DESIGN_ID as SUCCESSOR_PROPOSED_DESIGN_ID,
)
from q15_upgrade.strategy_bots.rti_microstructure_v15_identity import (
    DESIGN_ID as SUCCESSOR_EXECUTABLE_DESIGN_ID,
    DESIGN_SHA256 as SUCCESSOR_EXECUTABLE_DESIGN_SHA256,
    EXECUTABLE_FEATURE_DESIGN_FROZEN as SUCCESSOR_EXECUTABLE_DESIGN_CREATED,
    MODEL_FIT_ALLOWED as SUCCESSOR_MODEL_FIT_ALLOWED,
    NOTIFICATION_ELIGIBLE as SUCCESSOR_NOTIFICATION_ELIGIBLE,
    OUTCOME_ACCESS_ALLOWED as SUCCESSOR_OUTCOME_ACCESS_ALLOWED,
    REAL_TRADING_ALLOWED as SUCCESSOR_REAL_TRADING_ALLOWED,
    RUNTIME_FEATURE_CONSTRUCTION_CONNECTED as SUCCESSOR_RUNTIME_FEATURE_CONSTRUCTION_CONNECTED,
    RUNTIME_SCORING_CONNECTED as SUCCESSOR_RUNTIME_SCORING_CONNECTED,
)
from q15_upgrade.strategy_bots.rti_scheduled_maintenance_identity import (
    SHA256 as SCHEDULED_MAINTENANCE_SHA256,
    VERSION as SCHEDULED_MAINTENANCE_VERSION,
)
from q15_upgrade.strategy_bots.rti_independent_path_degradation_identity import (
    EVALUATION_GRACE_SECONDS as DEGRADATION_EVALUATION_GRACE_SECONDS,
    EXACT_DECISION_LEAD_SECONDS,
    FIRST_ELIGIBLE_CLOSE_TIME as DEGRADATION_FIRST_ELIGIBLE_CLOSE_TIME,
    POLICY_ID as DEGRADATION_POLICY_ID,
    POLICY_SHA256 as DEGRADATION_POLICY_SHA256,
    PROSPECTIVE_AFTER_CLOSE_TIME as DEGRADATION_PROSPECTIVE_AFTER_CLOSE_TIME,
    Q15_CLOSE_CADENCE_SECONDS,
)
from q15_upgrade.strategy_bots.telegram import V3Telegram
from tools.q15_rti_independent_path_audit import (
    AUDIT_VERSION,
    CONTRACT_IDENTITY_VERSION,
    DEFAULT_DB,
    DEFAULT_DESIGN,
    EXPECTED_ASSETS,
    REFERENCE_VERSION,
    SELECTED_EVIDENCE_IDENTITY_VERSION,
    build_report,
    validate_design,
)
from tools.q15_rti_microstructure_freeze import load_feature_rows_after
from tools.q15_rti_microstructure_preregister import design_fingerprint
from tools.q15_rti_v15_design_binding import (
    validate_files as validate_v15_design_binding_files,
)
from tools.q15_rti_v15_audit_seal import complete_audit_windows


NOTICE_VERSION = "q15-independent-path-readiness-milestone-notice-v1"
DEFAULT_DEGRADATION_POLICY = (
    ROOT / "config" / "q15_rti_independent_path_degradation_notice_v1.json"
)
DEFAULT_SCHEDULED_MAINTENANCE = (
    ROOT / "config" / "q15_rti_scheduled_maintenance.json"
)
MILESTONES = {
    "GEOMETRY_30": {
        "complete_windows": 30,
        "window_counter": "SOURCE_PATH",
        "headline": "INDEPENDENT PATH GEOMETRY REVIEW READY",
        "action": (
            "Run the locked outcome-blind five-feature geometry review; "
            "do not open settlement outcomes."
        ),
        "requires_geometry_pass": False,
    },
    "NON_BTC_60": {
        "complete_windows": 60,
        "window_counter": "V15_AUDIT",
        "headline": "V15 NON-BTC LOCKED AUDIT READY",
        "action": (
            "Verify the bound executable feature design, then run the one-shot "
            "NON_BTC_TRANSFER walk-forward gate; keep BTC outcomes sealed."
        ),
        "requires_geometry_pass": True,
    },
    "BTC_150": {
        "complete_windows": 150,
        "window_counter": "V15_AUDIT",
        "headline": "V15 BTC LOCKED AUDIT READY",
        "action": (
            "Verify the bound executable feature design, then run the one-shot "
            "BTC walk-forward gate without pooling non-BTC rows."
        ),
        "requires_geometry_pass": True,
    },
}


def _valid_sha256(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(
        character in "0123456789abcdef" for character in text.lower()
    )
IDEMPOTENCY_KEYS = {
    name: (
        f"{NOTICE_VERSION}:{name}:{raw['complete_windows']}:"
        f"{DESIGN_SHA256}:{GEOMETRY_PROTOCOL_SHA256}:"
        f"{GEOMETRY_FREEZE_CONTRACT_SHA256}:"
        f"{SUCCESSOR_CHARTER_SHA256}:{SUCCESSOR_EVALUATION_PROTOCOL_SHA256}"
    )
    for name, raw in MILESTONES.items()
}


@dataclass(frozen=True)
class _DisabledStore:
    enabled: bool = False


def load_degradation_policy(
    path: Path = DEFAULT_DEGRADATION_POLICY,
) -> dict[str, Any]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, Mapping):
        raise ValueError("independent_path_degradation_policy_root_not_object")
    return dict(decoded)


def validate_degradation_policy(policy: Mapping[str, Any]) -> None:
    if design_fingerprint(policy) != DEGRADATION_POLICY_SHA256:
        raise ValueError("independent_path_degradation_policy_sha256_mismatch")
    if (
        policy.get("notice_policy_id") != DEGRADATION_POLICY_ID
        or policy.get("policy_status")
        != "PREREGISTERED_BEFORE_FIRST_ELIGIBLE_WINDOW"
        or policy.get("applies_to_source_design_id") != DESIGN_ID
        or policy.get("applies_to_source_design_sha256") != DESIGN_SHA256
        or float(policy.get("prospective_after_close_time") or 0.0)
        != DEGRADATION_PROSPECTIVE_AFTER_CLOSE_TIME
        or float(policy.get("first_eligible_close_time") or 0.0)
        != DEGRADATION_FIRST_ELIGIBLE_CLOSE_TIME
        or policy.get("historical_incomplete_windows_receive_notices") is not False
    ):
        raise ValueError("independent_path_degradation_policy_identity_mismatch")
    trigger = policy.get("trigger")
    if not isinstance(trigger, Mapping) or (
        trigger.get("same_seven_assets_expected_per_close") is not True
        or int(trigger.get("valid_reconstructable_rows_required") or 0) != 7
        or trigger.get("missing_asset_or_invalid_evidence_triggers") is not True
        or trigger.get("complete_window_does_not_trigger") is not True
        or trigger.get("outcome_or_settlement_state_is_not_consulted") is not True
    ):
        raise ValueError("independent_path_degradation_policy_trigger_invalid")
    delivery = policy.get("delivery")
    if not isinstance(delivery, Mapping) or (
        delivery.get("channel") != "V3_TELEGRAM"
        or delivery.get("label") != "PAPER ADMIN"
        or delivery.get("one_idempotency_key_per_close") is not True
        or delivery.get("idempotency_key_includes_policy_sha256") is not True
        or int(delivery.get("expiration_seconds") or 0) != 31536000
        or delivery.get("notification_is_trade_signal") is not False
    ):
        raise ValueError("independent_path_degradation_delivery_invalid")
    effect = policy.get("effect")
    if not isinstance(effect, Mapping) or (
        effect.get("window_remains_excluded_from_credit") is not True
        or effect.get("backfill_allowed") is not False
    ):
        raise ValueError("independent_path_degradation_effect_invalid")
    for key in (
        "automatic_source_threshold_change_allowed",
        "automatic_feature_change_allowed",
        "automatic_model_change_allowed",
        "automatic_restart_allowed",
        "automatic_trading_action_allowed",
    ):
        if effect.get(key) is not False:
            raise ValueError("independent_path_degradation_automatic_action_guard")
    for key, expected in (
        ("outcome_columns_forbidden", True),
        ("outcome_labels_read", False),
        ("model_fit_performed", False),
        ("automatic_scoring", False),
        ("automatic_promotion", False),
        ("real_trading_allowed", False),
    ):
        if policy.get(key) is not expected:
            raise ValueError("independent_path_degradation_safety_guard_missing")


def load_scheduled_maintenance(
    path: Path = DEFAULT_SCHEDULED_MAINTENANCE,
) -> dict[str, Any]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, Mapping):
        raise ValueError("independent_path_maintenance_root_not_object")
    result = dict(decoded)
    if (
        design_fingerprint(result) != SCHEDULED_MAINTENANCE_SHA256
        or result.get("version") != SCHEDULED_MAINTENANCE_VERSION
        or result.get("source") != "user_confirmed_local_maintenance"
        or result.get("outcome_labels_used") is not False
        or result.get("changes_audit_credit") is not False
        or result.get(
            "changes_features_models_thresholds_or_gates"
        ) is not False
    ):
        raise ValueError("independent_path_maintenance_identity_invalid")
    windows = result.get("windows")
    if not isinstance(windows, list):
        raise ValueError("independent_path_maintenance_windows_invalid")
    previous_end = None
    for raw in windows:
        if not isinstance(raw, Mapping):
            raise ValueError("independent_path_maintenance_window_invalid")
        try:
            start = float(raw["start_close_time_inclusive"])
            end = float(raw["end_close_time_inclusive"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "independent_path_maintenance_window_invalid"
            ) from exc
        if (
            not start.is_integer()
            or not end.is_integer()
            or start < DEGRADATION_FIRST_ELIGIBLE_CLOSE_TIME
            or end < start
            or int(start) % Q15_CLOSE_CADENCE_SECONDS != 0
            or int(end) % Q15_CLOSE_CADENCE_SECONDS != 0
            or (
                previous_end is not None
                and start <= previous_end
            )
            or not str(raw.get("reason") or "")
            or raw.get("audit_credit_allowed") is not False
        ):
            raise ValueError("independent_path_maintenance_window_invalid")
        previous_end = end
    return result


def _scheduled_maintenance_for_close(
    close_time: float, maintenance: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    for raw in list(maintenance.get("windows") or ()):
        start = float(raw["start_close_time_inclusive"])
        end = float(raw["end_close_time_inclusive"])
        if start <= float(close_time) <= end:
            return raw
    return None


def _geometry_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    raw = dict(report.get("outcome_blind_geometry") or {})
    cohorts = {}
    for cohort, value in dict(raw.get("cohorts") or {}).items():
        value = dict(value or {})
        cohorts[str(cohort)] = {
            "rows": int(value.get("rows") or 0),
            "feature_count": int(value.get("feature_count") or 0),
            "finite": value.get("finite") is True,
            "active_feature_count": int(value.get("active_feature_count") or 0),
            "numerical_rank": int(value.get("numerical_rank") or 0),
            "stable_rank": value.get("stable_rank"),
            "condition_number_nonzero_subspace": value.get(
                "condition_number_nonzero_subspace"
            ),
            "maximum_absolute_correlation": value.get(
                "maximum_absolute_correlation"
            ),
            "exact_signed_duplicate_count": len(
                list(value.get("exact_signed_duplicate_pairs") or ())
            ),
        }
    return {
        "status": raw.get("status"),
        "review_window": int(raw.get("review_window") or 0),
        "thresholds_selected_from_outcomes": raw.get(
            "thresholds_selected_from_outcomes"
        ),
        "feature_selection_performed": raw.get("feature_selection_performed"),
        "cohorts": cohorts,
    }


def _source_quality_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    raw = dict(report.get("source_quality") or {})
    venues = {}
    for venue, value in dict(raw.get("venues") or {}).items():
        value = dict(value or {})
        metrics = dict(value.get("metrics") or {})
        venues[str(venue)] = {
            "rows": int(value.get("rows") or 0),
            "point_count": dict(metrics.get("point_count") or {}),
            "end_effective_age_seconds": dict(
                metrics.get("end_effective_age_seconds") or {}
            ),
            "max_gap_seconds": dict(metrics.get("max_gap_seconds") or {}),
            "max_message_age_seconds": dict(
                metrics.get("max_message_age_seconds") or {}
            ),
            "minimum_integrity_margin_seconds": dict(
                metrics.get("minimum_integrity_margin_seconds") or {}
            ),
        }
    return {
        "status": raw.get("status"),
        "credited_complete_rows": int(raw.get("credited_complete_rows") or 0),
        "source_thresholds_from_frozen_design": raw.get(
            "source_thresholds_from_frozen_design"
        ),
        "thresholds_selected_from_outcomes": raw.get(
            "thresholds_selected_from_outcomes"
        ),
        "outcome_labels_read": raw.get("outcome_labels_read"),
        "evidence_parse_failures": int(raw.get("evidence_parse_failures") or 0),
        "integrity_breaches": int(raw.get("integrity_breaches") or 0),
        "minimum_integrity_margin_seconds": raw.get(
            "minimum_integrity_margin_seconds"
        ),
        "venues": venues,
    }


def _geometry_review_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    raw = dict(report.get("geometry_review") or {})
    return {
        "protocol_id": raw.get("protocol_id"),
        "protocol_sha256": raw.get("protocol_sha256"),
        "review_window": int(raw.get("review_window") or 0),
        "review_ready": raw.get("review_ready"),
        "status": raw.get("status"),
        "checks": dict(raw.get("checks") or {}),
        "failed_checks": list(raw.get("failed_checks") or ()),
        "all_checks_met": raw.get("all_checks_met"),
        "pass_does_not_authorize_model_fit": raw.get(
            "pass_does_not_authorize_model_fit"
        ),
        "pass_does_not_authorize_outcome_access": raw.get(
            "pass_does_not_authorize_outcome_access"
        ),
        "automatic_action_allowed": raw.get("automatic_action_allowed"),
        "outcome_labels_read": raw.get("outcome_labels_read"),
        "model_fit_performed": raw.get("model_fit_performed"),
    }


def _expected_due_close_times(now: float) -> list[float]:
    latest_due = int(
        (
            float(now)
            + EXACT_DECISION_LEAD_SECONDS
            - DEGRADATION_EVALUATION_GRACE_SECONDS
        ) // Q15_CLOSE_CADENCE_SECONDS
    ) * Q15_CLOSE_CADENCE_SECONDS
    first = int(DEGRADATION_FIRST_ELIGIBLE_CLOSE_TIME)
    if latest_due < first:
        return []
    return [
        float(close_time)
        for close_time in range(
            first, latest_due + 1, Q15_CLOSE_CADENCE_SECONDS
        )
    ]


def build_outcome_blind_snapshot(
    *, design_path: Path = DEFAULT_DESIGN, database_path: Path = DEFAULT_DB,
    degradation_policy_path: Path = DEFAULT_DEGRADATION_POLICY,
    scheduled_maintenance_path: Path = DEFAULT_SCHEDULED_MAINTENANCE,
    now: float | None = None,
) -> dict[str, Any]:
    design = json.loads(design_path.read_text(encoding="utf-8"))
    if not isinstance(design, Mapping):
        raise ValueError("independent_path_readiness_design_root_not_object")
    validate_design(design)
    v15_binding = validate_v15_design_binding_files()
    degradation_policy = load_degradation_policy(degradation_policy_path)
    validate_degradation_policy(degradation_policy)
    scheduled_maintenance = load_scheduled_maintenance(
        scheduled_maintenance_path
    )
    # The independent-path design forbids pre-freeze credit.  Push its frozen
    # boundary into the outcome-denied SQL loader; otherwise this periodic
    # monitor materializes hundreds of thousands of wide historical rows and
    # can block the exact sampler behind the GIL.
    rows = load_feature_rows_after(
        database_path, float(design["prospective_after_close_time"]),
    )
    report = build_report(rows, design)
    geometry = _geometry_summary(report)
    source_quality = _source_quality_summary(report)
    geometry_review = _geometry_review_summary(report)
    review_evidence = dict(report.get("geometry_review_evidence") or {})
    review_source_quality = dict(review_evidence.get("source_quality") or {})
    selected_evidence_identity = dict(
        review_source_quality.get("selected_feature_evidence_identity") or {}
    )
    selected_contract_identity = dict(
        review_source_quality.get("contract_identity") or {}
    )
    complete = int(report.get("complete_seven_asset_close_windows") or 0)
    v15_audit_complete = len(complete_audit_windows(rows))
    all_seven = dict(dict(geometry.get("cohorts") or {}).get("ALL_SEVEN") or {})
    complete_rows = int(all_seven.get("rows") or 0)
    reconstructable = bool(
        complete_rows == complete * 7
        and all_seven.get("finite") is True
        and int(all_seven.get("feature_count") or 0) == 5
    )
    incomplete_windows = [
        dict(value or {})
        for value in list(report.get("incomplete_windows") or ())
    ]
    prospective_degradation_events = []
    scheduled_maintenance_events = []
    historical_incomplete_windows_ignored = 0
    for raw in incomplete_windows:
        close_time = raw.get("close_time")
        try:
            close_time = float(close_time)
        except (TypeError, ValueError):
            historical_incomplete_windows_ignored += 1
            continue
        if close_time < DEGRADATION_FIRST_ELIGIBLE_CLOSE_TIME:
            historical_incomplete_windows_ignored += 1
            continue
        event = {
            "close_time": close_time,
            "valid_row_count": int(raw.get("valid_row_count") or 0),
            "missing_assets": sorted({
                str(asset).upper()
                for asset in list(raw.get("missing_assets") or ())
            }),
            "source_missing_reasons": {
                str(asset).upper(): str(reason)
                for asset, reason in dict(
                    raw.get("source_missing_reasons") or {}
                ).items()
                if reason not in (None, "")
            },
        }
        maintenance = _scheduled_maintenance_for_close(
            close_time, scheduled_maintenance,
        )
        if maintenance is None:
            prospective_degradation_events.append(event)
        else:
            scheduled_maintenance_events.append({
                **event,
                "maintenance_reason": str(maintenance["reason"]),
                "audit_credit_allowed": False,
            })
    prospective_degradation_events.sort(key=lambda value: value["close_time"])
    scheduled_maintenance_events.sort(
        key=lambda value: value["close_time"]
    )
    observed_close_times = {
        float(value)
        for value in list(report.get("complete_close_times") or ())
    }
    observed_close_times.update(
        float(event["close_time"])
        for event in prospective_degradation_events
    )
    observed_close_times.update(
        float(event["close_time"])
        for event in scheduled_maintenance_events
    )
    expected_due_close_times = _expected_due_close_times(
        time.time() if now is None else float(now)
    )
    missing_due_close_times = [
        close_time for close_time in expected_due_close_times
        if close_time not in observed_close_times
    ]
    entirely_missing_due_close_times = []
    for close_time in missing_due_close_times:
        event = {
            "close_time": close_time,
            "valid_row_count": 0,
            "missing_assets": sorted(EXPECTED_ASSETS),
            "source_missing_reasons": {
                asset: "no_strategy_row_recorded_after_exact_decision"
                for asset in sorted(EXPECTED_ASSETS)
            },
        }
        maintenance = _scheduled_maintenance_for_close(
            close_time, scheduled_maintenance,
        )
        if maintenance is None:
            entirely_missing_due_close_times.append(close_time)
            prospective_degradation_events.append(event)
        else:
            scheduled_maintenance_events.append({
                **event,
                "maintenance_reason": str(maintenance["reason"]),
                "audit_credit_allowed": False,
            })
    prospective_degradation_events.sort(key=lambda value: value["close_time"])
    scheduled_maintenance_events.sort(
        key=lambda value: value["close_time"]
    )
    return {
        "notice_version": NOTICE_VERSION,
        "audit_version": report.get("audit_version"),
        "reference_formula_verifier_version": report.get(
            "reference_formula_verifier_version"
        ),
        "reference_formula_mismatch_rows": int(
            report.get("reference_formula_mismatch_rows") or 0
        ),
        "design_id": DESIGN_ID,
        "design_sha256": DESIGN_SHA256,
        "complete_executable_close_windows": complete,
        "complete_reconstructable_close_windows": complete,
        "complete_reconstructable_rows": complete_rows,
        "successor_audit_complete_close_windows": v15_audit_complete,
        "successor_audit_complete_rows": v15_audit_complete * 7,
        "successor_audit_feature_ineligible_source_windows": (
            complete - v15_audit_complete
        ),
        "successor_audit_population_outcome_labels_read": False,
        "successor_audit_population_model_fit_performed": False,
        "eligible_close_windows": int(report.get("eligible_close_windows") or 0),
        "eligible_rows": int(report.get("eligible_rows") or 0),
        "valid_rows": int(report.get("valid_rows") or 0),
        "invalid_rows_excluded_from_credit": int(report.get("invalid_rows") or 0),
        "complete_window_evidence_reconstructable": reconstructable,
        "geometry": geometry,
        "geometry_review": geometry_review,
        "geometry_review_selected_feature_evidence_identity": (
            selected_evidence_identity
        ),
        "geometry_review_contract_identity": selected_contract_identity,
        "source_quality": source_quality,
        "degradation_notice_policy_id": DEGRADATION_POLICY_ID,
        "degradation_notice_policy_sha256": DEGRADATION_POLICY_SHA256,
        "degradation_notice_prospective_after_close_time": (
            DEGRADATION_PROSPECTIVE_AFTER_CLOSE_TIME
        ),
        "degradation_notice_first_eligible_close_time": (
            DEGRADATION_FIRST_ELIGIBLE_CLOSE_TIME
        ),
        "successor_charter_id": SUCCESSOR_CHARTER_ID,
        "successor_charter_sha256": SUCCESSOR_CHARTER_SHA256,
        "successor_proposed_design_id": SUCCESSOR_PROPOSED_DESIGN_ID,
        "successor_evaluation_protocol_id": SUCCESSOR_EVALUATION_PROTOCOL_ID,
        "successor_evaluation_protocol_sha256": (
            SUCCESSOR_EVALUATION_PROTOCOL_SHA256
        ),
        "successor_executable_design_created": (
            SUCCESSOR_EXECUTABLE_DESIGN_CREATED
        ),
        "successor_executable_design_id": SUCCESSOR_EXECUTABLE_DESIGN_ID,
        "successor_executable_design_sha256": (
            SUCCESSOR_EXECUTABLE_DESIGN_SHA256
        ),
        "successor_design_binding_status": v15_binding.get("status"),
        "successor_geometry_payload_sha256": v15_binding.get(
            "geometry_payload_sha256"
        ),
        "successor_runtime_feature_construction_connected": (
            SUCCESSOR_RUNTIME_FEATURE_CONSTRUCTION_CONNECTED
        ),
        "successor_outcome_access_allowed": SUCCESSOR_OUTCOME_ACCESS_ALLOWED,
        "successor_model_fit_performed": SUCCESSOR_MODEL_FIT_ALLOWED,
        "successor_runtime_scoring_connected": (
            SUCCESSOR_RUNTIME_SCORING_CONNECTED
        ),
        "successor_notification_eligible": SUCCESSOR_NOTIFICATION_ELIGIBLE,
        "successor_automatic_promotion": False,
        "successor_real_trading_allowed": SUCCESSOR_REAL_TRADING_ALLOWED,
        "geometry_freeze_contract_id": GEOMETRY_FREEZE_CONTRACT_ID,
        "geometry_freeze_contract_sha256": GEOMETRY_FREEZE_CONTRACT_SHA256,
        "geometry_freeze_manual_command_only": True,
        "geometry_freeze_background_write_allowed": False,
        "degradation_notice_evaluation_grace_seconds": (
            DEGRADATION_EVALUATION_GRACE_SECONDS
        ),
        "expected_due_close_count": len(expected_due_close_times),
        "entirely_missing_due_close_count": len(
            entirely_missing_due_close_times
        ),
        "scheduled_maintenance_version": SCHEDULED_MAINTENANCE_VERSION,
        "scheduled_maintenance_sha256": SCHEDULED_MAINTENANCE_SHA256,
        "scheduled_maintenance_windows": len(
            list(scheduled_maintenance.get("windows") or ())
        ),
        "scheduled_maintenance_due_close_count": len(
            scheduled_maintenance_events
        ),
        "scheduled_maintenance_events": scheduled_maintenance_events,
        "scheduled_maintenance_receives_audit_credit": False,
        "prospective_degradation_events": prospective_degradation_events,
        "prospective_degradation_event_count": len(
            prospective_degradation_events
        ),
        "historical_incomplete_windows_ignored": (
            historical_incomplete_windows_ignored
        ),
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "feature_selection_performed": False,
        "thresholds_selected_from_outcomes": False,
        "artifact_emitted": False,
        "automatic_scoring": False,
        "automatic_promotion": False,
        "notification_is_trade_signal": False,
        "real_trading_allowed": False,
    }


def _snapshot_safe(snapshot: Mapping[str, Any]) -> bool:
    geometry = dict(snapshot.get("geometry") or {})
    cohorts = dict(geometry.get("cohorts") or {})
    source_quality = dict(snapshot.get("source_quality") or {})
    geometry_review = dict(snapshot.get("geometry_review") or {})
    evidence_identity = dict(
        snapshot.get(
            "geometry_review_selected_feature_evidence_identity"
        ) or {}
    )
    contract_identity = dict(
        snapshot.get("geometry_review_contract_identity") or {}
    )
    complete_windows = int(
        snapshot.get("complete_reconstructable_close_windows") or 0
    )
    successor_audit_windows = int(
        snapshot.get("successor_audit_complete_close_windows") or 0
    )
    return bool(
        snapshot.get("notice_version") == NOTICE_VERSION
        and snapshot.get("audit_version") == AUDIT_VERSION
        and snapshot.get("reference_formula_verifier_version")
        == REFERENCE_VERSION
        and int(snapshot.get("reference_formula_mismatch_rows") or 0) == 0
        and snapshot.get("design_id") == DESIGN_ID
        and snapshot.get("design_sha256") == DESIGN_SHA256
        and snapshot.get("degradation_notice_policy_id")
        == DEGRADATION_POLICY_ID
        and snapshot.get("degradation_notice_policy_sha256")
        == DEGRADATION_POLICY_SHA256
        and float(
            snapshot.get("degradation_notice_prospective_after_close_time")
            or 0.0
        ) == DEGRADATION_PROSPECTIVE_AFTER_CLOSE_TIME
        and float(
            snapshot.get("degradation_notice_first_eligible_close_time")
            or 0.0
        ) == DEGRADATION_FIRST_ELIGIBLE_CLOSE_TIME
        and snapshot.get("successor_charter_id") == SUCCESSOR_CHARTER_ID
        and snapshot.get("successor_charter_sha256")
        == SUCCESSOR_CHARTER_SHA256
        and snapshot.get("successor_proposed_design_id")
        == SUCCESSOR_PROPOSED_DESIGN_ID
        and snapshot.get("successor_evaluation_protocol_id")
        == SUCCESSOR_EVALUATION_PROTOCOL_ID
        and snapshot.get("successor_evaluation_protocol_sha256")
        == SUCCESSOR_EVALUATION_PROTOCOL_SHA256
        and snapshot.get("successor_executable_design_created") is True
        and snapshot.get("successor_executable_design_id")
        == SUCCESSOR_EXECUTABLE_DESIGN_ID
        and snapshot.get("successor_executable_design_sha256")
        == SUCCESSOR_EXECUTABLE_DESIGN_SHA256
        and snapshot.get("successor_design_binding_status")
        == "V15_EXECUTABLE_FEATURE_DESIGN_BOUND_AND_VERIFIED"
        and _valid_sha256(snapshot.get("successor_geometry_payload_sha256"))
        and snapshot.get(
            "successor_runtime_feature_construction_connected"
        ) is True
        and snapshot.get("successor_outcome_access_allowed") is False
        and snapshot.get("successor_model_fit_performed") is False
        and snapshot.get("successor_runtime_scoring_connected") is False
        and snapshot.get("successor_notification_eligible") is False
        and snapshot.get("successor_automatic_promotion") is False
        and snapshot.get("successor_real_trading_allowed") is False
        and 0 <= successor_audit_windows <= complete_windows
        and int(snapshot.get("successor_audit_complete_rows") or 0)
        == successor_audit_windows * 7
        and int(
            snapshot.get(
                "successor_audit_feature_ineligible_source_windows"
            ) or 0
        ) == complete_windows - successor_audit_windows
        and snapshot.get(
            "successor_audit_population_outcome_labels_read"
        ) is False
        and snapshot.get(
            "successor_audit_population_model_fit_performed"
        ) is False
        and snapshot.get("geometry_freeze_contract_id")
        == GEOMETRY_FREEZE_CONTRACT_ID
        and snapshot.get("geometry_freeze_contract_sha256")
        == GEOMETRY_FREEZE_CONTRACT_SHA256
        and snapshot.get("geometry_freeze_manual_command_only") is True
        and snapshot.get("geometry_freeze_background_write_allowed") is False
        and int(
            snapshot.get("degradation_notice_evaluation_grace_seconds") or -1
        ) == DEGRADATION_EVALUATION_GRACE_SECONDS
        and geometry_review.get("protocol_id") == GEOMETRY_PROTOCOL_ID
        and geometry_review.get("protocol_sha256") == GEOMETRY_PROTOCOL_SHA256
        and int(geometry_review.get("review_window") or 0) == 30
        and geometry_review.get("pass_does_not_authorize_model_fit") is True
        and geometry_review.get("pass_does_not_authorize_outcome_access")
        is True
        and geometry_review.get("automatic_action_allowed") is False
        and geometry_review.get("outcome_labels_read") is False
        and geometry_review.get("model_fit_performed") is False
        and evidence_identity.get("version")
        == SELECTED_EVIDENCE_IDENTITY_VERSION
        and int(evidence_identity.get("rows") or 0)
        == min(complete_windows, 30) * 7
        and _valid_sha256(evidence_identity.get("sha256"))
        and evidence_identity.get("outcome_columns_selected") is False
        and evidence_identity.get("outcome_labels_read") is False
        and contract_identity.get("version") == CONTRACT_IDENTITY_VERSION
        and int(contract_identity.get("rows") or 0)
        == min(complete_windows, 30) * 7
        and int(contract_identity.get("mismatch_rows") or 0) == 0
        and contract_identity.get("ticker_asset_alignment_required") is True
        and contract_identity.get("ticker_close_time_alignment_required") is True
        and contract_identity.get("dst_fold_safe") is True
        and contract_identity.get("outcome_labels_read") is False
        and snapshot.get("complete_window_evidence_reconstructable") is True
        and int(geometry.get("review_window") or 0) == 30
        and geometry.get("thresholds_selected_from_outcomes") is False
        and geometry.get("feature_selection_performed") is False
        and set(cohorts) == {"ALL_SEVEN", "BTC", "NON_BTC_TRANSFER"}
        and all(
            dict(value).get("finite") is True
            and int(dict(value).get("feature_count") or 0) == 5
            for value in cohorts.values()
        )
        and source_quality.get("status") == "PASS_ALL_CREDITED_COMPLETE_ROWS"
        and int(source_quality.get("credited_complete_rows") or 0)
        == int(snapshot.get("complete_reconstructable_rows") or 0)
        and source_quality.get("source_thresholds_from_frozen_design") is True
        and source_quality.get("thresholds_selected_from_outcomes") is False
        and source_quality.get("outcome_labels_read") is False
        and int(source_quality.get("evidence_parse_failures") or 0) == 0
        and int(source_quality.get("integrity_breaches") or 0) == 0
        and set(dict(source_quality.get("venues") or {})) == {
            "coinbase", "kraken",
        }
        and _scheduled_maintenance_snapshot_safe(snapshot)
        and snapshot.get("outcome_columns_selected") is False
        and snapshot.get("outcome_labels_read") is False
        and snapshot.get("model_fit_performed") is False
        and snapshot.get("feature_selection_performed") is False
        and snapshot.get("thresholds_selected_from_outcomes") is False
        and snapshot.get("artifact_emitted") is False
        and snapshot.get("automatic_scoring") is False
        and snapshot.get("automatic_promotion") is False
        and snapshot.get("notification_is_trade_signal") is False
        and snapshot.get("real_trading_allowed") is False
    )


def _scheduled_maintenance_snapshot_safe(
    snapshot: Mapping[str, Any],
) -> bool:
    events = list(snapshot.get("scheduled_maintenance_events") or ())
    if not (
        snapshot.get("scheduled_maintenance_version")
        == SCHEDULED_MAINTENANCE_VERSION
        and snapshot.get("scheduled_maintenance_sha256")
        == SCHEDULED_MAINTENANCE_SHA256
        and int(snapshot.get("scheduled_maintenance_windows") or 0) >= 0
        and int(
            snapshot.get("scheduled_maintenance_due_close_count") or 0
        ) == len(events)
        and snapshot.get(
            "scheduled_maintenance_receives_audit_credit"
        ) is False
    ):
        return False
    maintenance = load_scheduled_maintenance()
    seen: set[float] = set()
    degradation_times = {
        float(event["close_time"])
        for event in list(
            snapshot.get("prospective_degradation_events") or ()
        )
    }
    for raw in events:
        if not isinstance(raw, Mapping):
            return False
        try:
            close_time = float(raw["close_time"])
        except (KeyError, TypeError, ValueError):
            return False
        window = _scheduled_maintenance_for_close(
            close_time, maintenance,
        )
        if (
            close_time in seen
            or close_time in degradation_times
            or window is None
            or raw.get("maintenance_reason") != window.get("reason")
            or raw.get("audit_credit_allowed") is not False
        ):
            return False
        seen.add(close_time)
    return True


def _degradation_snapshot_safe(snapshot: Mapping[str, Any]) -> bool:
    events = list(snapshot.get("prospective_degradation_events") or ())
    if not (
        snapshot.get("notice_version") == NOTICE_VERSION
        and snapshot.get("design_id") == DESIGN_ID
        and snapshot.get("design_sha256") == DESIGN_SHA256
        and snapshot.get("degradation_notice_policy_id")
        == DEGRADATION_POLICY_ID
        and snapshot.get("degradation_notice_policy_sha256")
        == DEGRADATION_POLICY_SHA256
        and float(
            snapshot.get("degradation_notice_prospective_after_close_time")
            or 0.0
        ) == DEGRADATION_PROSPECTIVE_AFTER_CLOSE_TIME
        and float(
            snapshot.get("degradation_notice_first_eligible_close_time")
            or 0.0
        ) == DEGRADATION_FIRST_ELIGIBLE_CLOSE_TIME
        and int(
            snapshot.get("degradation_notice_evaluation_grace_seconds") or -1
        ) == DEGRADATION_EVALUATION_GRACE_SECONDS
        and int(snapshot.get("expected_due_close_count") or 0) >= 0
        and int(snapshot.get("entirely_missing_due_close_count") or 0) >= 0
        and int(snapshot.get("prospective_degradation_event_count") or 0)
        == len(events)
        and int(snapshot.get("historical_incomplete_windows_ignored") or 0)
        >= 0
        and _scheduled_maintenance_snapshot_safe(snapshot)
        and snapshot.get("outcome_columns_selected") is False
        and snapshot.get("outcome_labels_read") is False
        and snapshot.get("model_fit_performed") is False
        and snapshot.get("feature_selection_performed") is False
        and snapshot.get("thresholds_selected_from_outcomes") is False
        and snapshot.get("artifact_emitted") is False
        and snapshot.get("automatic_scoring") is False
        and snapshot.get("automatic_promotion") is False
        and snapshot.get("notification_is_trade_signal") is False
        and snapshot.get("real_trading_allowed") is False
    ):
        return False
    seen: set[float] = set()
    for raw in events:
        if not isinstance(raw, Mapping):
            return False
        try:
            close_time = float(raw.get("close_time"))
            valid_row_count = int(raw.get("valid_row_count"))
        except (TypeError, ValueError):
            return False
        missing_assets = {
            str(asset).upper()
            for asset in list(raw.get("missing_assets") or ())
        }
        reasons = raw.get("source_missing_reasons")
        if (
            close_time < DEGRADATION_FIRST_ELIGIBLE_CLOSE_TIME
            or close_time in seen
            or not 0 <= valid_row_count <= 7
            or not missing_assets.issubset(EXPECTED_ASSETS)
            or (valid_row_count == 7 and not missing_assets)
            or not isinstance(reasons, Mapping)
            or not set(str(key).upper() for key in reasons).issubset(
                EXPECTED_ASSETS
            )
        ):
            return False
        seen.add(close_time)
    return True


def ready_degradation_events(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not _degradation_snapshot_safe(snapshot):
        return []
    return [
        dict(value)
        for value in list(snapshot.get("prospective_degradation_events") or ())
    ]


def milestone_is_ready(snapshot: Mapping[str, Any], milestone: str) -> bool:
    definition = MILESTONES.get(milestone)
    geometry_review = dict(snapshot.get("geometry_review") or {})
    geometry_status = geometry_review.get("status")
    geometry_ready = bool(
        geometry_review.get("review_ready") is True
        and geometry_status in {
            "PASS_CONTINUE_PROSPECTIVE_COLLECTION_NO_MODEL",
            "GEOMETRY_REVIEW_REQUIRED_NO_AUTOMATIC_ACTION",
        }
    )
    geometry_requirement_met = bool(
        geometry_status == "PASS_CONTINUE_PROSPECTIVE_COLLECTION_NO_MODEL"
        if definition and definition.get("requires_geometry_pass")
        else geometry_ready
    )
    counter = (
        "successor_audit_complete_close_windows"
        if definition
        and definition.get("window_counter") == "V15_AUDIT"
        else "complete_reconstructable_close_windows"
    )
    return bool(
        definition is not None
        and _snapshot_safe(snapshot)
        and geometry_requirement_met
        and int(snapshot.get(counter) or 0)
        >= int(definition["complete_windows"])
    )


def ready_milestones(snapshot: Mapping[str, Any]) -> list[str]:
    return [name for name in MILESTONES if milestone_is_ready(snapshot, name)]


def _format_number(value: Any, digits: int = 3) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "n/a"


def readiness_message(snapshot: Mapping[str, Any], milestone: str) -> str:
    definition = MILESTONES[milestone]
    source_windows = int(snapshot["complete_reconstructable_close_windows"])
    windows = int(
        snapshot["successor_audit_complete_close_windows"]
        if definition.get("window_counter") == "V15_AUDIT"
        else source_windows
    )
    required = int(definition["complete_windows"])
    cohorts = dict(dict(snapshot.get("geometry") or {}).get("cohorts") or {})
    source_quality = dict(snapshot.get("source_quality") or {})
    geometry_review = dict(snapshot.get("geometry_review") or {})
    geometry_lines = []
    for cohort in ("ALL_SEVEN", "NON_BTC_TRANSFER", "BTC"):
        raw = dict(cohorts.get(cohort) or {})
        geometry_lines.append(
            f"{cohort}: active {int(raw.get('active_feature_count') or 0)}/5; "
            f"rank {int(raw.get('numerical_rank') or 0)}; "
            f"condition {_format_number(raw.get('condition_number_nonzero_subspace'))}"
        )
    return "\n".join((
        f"<b>V3 RTI {definition['headline']} | PAPER ADMIN</b>",
        (
            f"V15-auditable seven-asset windows: {windows}/{required}; "
            f"source-complete: {source_windows}"
            if definition.get("window_counter") == "V15_AUDIT"
            else f"Reconstructable seven-asset windows: {windows}/{required}"
        ),
        f"Valid evidence rows: {int(snapshot.get('valid_rows') or 0)}; "
        f"invalid rows excluded from credit: {int(snapshot.get('invalid_rows_excluded_from_credit') or 0)}",
        f"Frozen source-quality gates: {source_quality.get('status')}; "
        f"worst margin {_format_number(source_quality.get('minimum_integrity_margin_seconds'))}s",
        f"Frozen geometry review: {geometry_review.get('status')}; "
        f"failed checks: {len(list(geometry_review.get('failed_checks') or ()))}",
        *geometry_lines,
        f"Milestone: {milestone}",
        "Outcome labels: SEALED / unread",
        "Model fit, feature selection, and scoring: NOT RUN",
        "Artifact/promotion/trading: DISABLED",
        str(definition["action"]),
        f"Design: <code>{DESIGN_ID}</code>",
        f"Geometry protocol: <code>{GEOMETRY_PROTOCOL_ID}</code>",
        f"Immutable freeze contract: <code>{GEOMETRY_FREEZE_CONTRACT_ID}</code>",
        f"Successor protocol: <code>{SUCCESSOR_EVALUATION_PROTOCOL_ID}</code>",
        "This is an administrative readiness notice, not a trade signal.",
    ))


def degradation_message(
    snapshot: Mapping[str, Any], event: Mapping[str, Any],
) -> str:
    close_time = float(event["close_time"])
    close_utc = datetime.fromtimestamp(close_time, tz=timezone.utc).isoformat()
    missing_assets = [
        str(asset).upper() for asset in list(event.get("missing_assets") or ())
    ]
    reasons = {
        str(asset).upper(): str(reason)
        for asset, reason in dict(
            event.get("source_missing_reasons") or {}
        ).items()
    }
    reason_lines = [
        f"{escape(asset)}: {escape(reasons.get(asset, 'no_valid_row'))}"
        for asset in missing_assets
    ]
    if not reason_lines:
        reason_lines = [
            f"{escape(asset)}: {escape(reason)}"
            for asset, reason in sorted(reasons.items())
        ] or ["unknown: incomplete seven-asset reconstruction"]
    return "\n".join((
        "<b>V3 RTI PATH SOURCE DEGRADED | PAPER ADMIN</b>",
        f"Close: <code>{int(close_time)}</code> ({escape(close_utc)})",
        f"Valid reconstructable rows: {int(event.get('valid_row_count') or 0)}/7",
        "Missing assets: " + (
            ", ".join(escape(asset) for asset in missing_assets) or "none listed"
        ),
        "Source reasons: " + "; ".join(reason_lines),
        "Window remains excluded from readiness credit; backfill is forbidden.",
        "Outcome labels: SEALED / unread",
        "Model, feature, threshold, restart, promotion, and trading actions: NONE",
        f"Source design: <code>{DESIGN_ID}</code>",
        f"Notice policy: <code>{DEGRADATION_POLICY_ID}</code>",
        "This is a PAPER administrative source-health notice, not a trade signal.",
    ))


def send_ready_milestones(
    snapshot: Mapping[str, Any], sender: Callable[..., Mapping[str, Any]],
    *, now: float | None = None,
) -> dict[str, Any]:
    ready = ready_milestones(snapshot)
    if not ready:
        return {
            "status": "WAITING_FOR_MILESTONE",
            "notice_attempted": False,
            "ready_milestones": [],
            "deliveries": {},
        }
    current = time.time() if now is None else float(now)
    deliveries = {
        name: dict(sender(
            readiness_message(snapshot, name),
            idempotency_key=IDEMPOTENCY_KEYS[name],
            expires_at=current + 365.0 * 86400.0,
        ))
        for name in ready
    }
    return {
        "status": "READY_MILESTONES_PROCESSED",
        "notice_attempted": True,
        "ready_milestones": ready,
        "deliveries": deliveries,
    }


def send_degradation_notices(
    snapshot: Mapping[str, Any], sender: Callable[..., Mapping[str, Any]],
    *, now: float | None = None,
) -> dict[str, Any]:
    events = ready_degradation_events(snapshot)
    if not events:
        return {
            "status": "NO_PROSPECTIVE_DEGRADATION",
            "notice_attempted": False,
            "ready_close_times": [],
            "deliveries": {},
        }
    current = time.time() if now is None else float(now)
    deliveries = {}
    for event in events:
        close_time = int(float(event["close_time"]))
        key = (
            f"{DEGRADATION_POLICY_ID}:{DEGRADATION_POLICY_SHA256}:"
            f"{close_time}"
        )
        deliveries[str(close_time)] = dict(sender(
            degradation_message(snapshot, event),
            idempotency_key=key,
            expires_at=current + 31536000.0,
        ))
    return {
        "status": "PROSPECTIVE_DEGRADATION_PROCESSED",
        "notice_attempted": True,
        "ready_close_times": [
            int(float(event["close_time"])) for event in events
        ],
        "deliveries": deliveries,
    }


def _default_sender() -> Callable[..., Mapping[str, Any]]:
    raw = V3Telegram()
    outbox = ReliableTelegramOutbox(
        _DisabledStore(), raw,
        sqlite_path=os.environ.get(
            "Q15_V9_OUTBOX_SQLITE_PATH", "data/q15_telegram_outbox.sqlite3",
        ),
    )
    return outbox.send_with_result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--design", default=str(DEFAULT_DESIGN))
    parser.add_argument("--strategy-db", default=str(DEFAULT_DB))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    snapshot = build_outcome_blind_snapshot(
        design_path=Path(args.design), database_path=Path(args.strategy_db),
    )
    ready = ready_milestones(snapshot)
    degradation_events = ready_degradation_events(snapshot)
    result = (
        {
            "status": "READY_MILESTONES_DRY_RUN" if ready else "WAITING_FOR_MILESTONE",
            "notice_attempted": False,
            "ready_milestones": ready,
            "deliveries": {},
        }
        if args.dry_run else send_ready_milestones(snapshot, _default_sender())
    )
    degradation_result = (
        {
            "status": (
                "PROSPECTIVE_DEGRADATION_DRY_RUN"
                if degradation_events else "NO_PROSPECTIVE_DEGRADATION"
            ),
            "notice_attempted": False,
            "ready_close_times": [
                int(float(event["close_time"]))
                for event in degradation_events
            ],
            "deliveries": {},
        }
        if args.dry_run
        else send_degradation_notices(snapshot, _default_sender())
    )
    print(json.dumps({
        **snapshot,
        **result,
        "degradation_notice": degradation_result,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
