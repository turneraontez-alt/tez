"""Background PAPER ADMIN monitor for independent-path geometry readiness."""
from __future__ import annotations

import os
import threading
from typing import Any

from .strategy_bots.rti_independent_path_geometry_identity import (
    PROTOCOL_ID as GEOMETRY_PROTOCOL_ID,
    PROTOCOL_SHA256 as GEOMETRY_PROTOCOL_SHA256,
)
from .strategy_bots.rti_independent_path_geometry_freeze_identity import (
    CONTRACT_ID as GEOMETRY_FREEZE_CONTRACT_ID,
    CONTRACT_SHA256 as GEOMETRY_FREEZE_CONTRACT_SHA256,
    DEFAULT_ARTIFACT_RELATIVE_PATH as GEOMETRY_FREEZE_ARTIFACT_PATH,
)
from .strategy_bots.rti_independent_path_successor_identity import (
    CHARTER_ID as SUCCESSOR_CHARTER_ID,
    CHARTER_SHA256 as SUCCESSOR_CHARTER_SHA256,
    EVALUATION_PROTOCOL_ID as SUCCESSOR_EVALUATION_PROTOCOL_ID,
    EVALUATION_PROTOCOL_SHA256 as SUCCESSOR_EVALUATION_PROTOCOL_SHA256,
    PROPOSED_DESIGN_ID as SUCCESSOR_PROPOSED_DESIGN_ID,
)
from .strategy_bots.rti_microstructure_v15_identity import (
    DESIGN_ID as SUCCESSOR_EXECUTABLE_DESIGN_ID,
    DESIGN_SHA256 as SUCCESSOR_EXECUTABLE_DESIGN_SHA256,
    EXECUTABLE_FEATURE_DESIGN_FROZEN as SUCCESSOR_EXECUTABLE_DESIGN_CREATED,
    MODEL_FIT_ALLOWED as SUCCESSOR_MODEL_FIT_PERFORMED,
    NOTIFICATION_ELIGIBLE as SUCCESSOR_NOTIFICATION_ELIGIBLE,
    OUTCOME_ACCESS_ALLOWED as SUCCESSOR_OUTCOME_ACCESS_ALLOWED,
    REAL_TRADING_ALLOWED as SUCCESSOR_REAL_TRADING_ALLOWED,
    RUNTIME_FEATURE_CONSTRUCTION_CONNECTED as SUCCESSOR_RUNTIME_FEATURE_CONSTRUCTION_CONNECTED,
    RUNTIME_SCORING_CONNECTED as SUCCESSOR_RUNTIME_SCORING_CONNECTED,
)
from .strategy_bots.rti_microstructure_v15_audit_identity import (
    AUDIT_SEAL_VERSION as SUCCESSOR_AUDIT_SEAL_VERSION,
    HISTORICAL_AUDIT_TOOLING_READY as SUCCESSOR_HISTORICAL_AUDIT_TOOLING_READY,
    PRETEST_RUNNER_VERSION as SUCCESSOR_PRETEST_RUNNER_VERSION,
    PRETEST_STATE_VERSION as SUCCESSOR_PRETEST_STATE_VERSION,
    REPORTING_PROTOCOL_ID as SUCCESSOR_REPORTING_PROTOCOL_ID,
    REPORTING_PROTOCOL_SHA256 as SUCCESSOR_REPORTING_PROTOCOL_SHA256,
    SETTLEMENT_EVIDENCE_SOURCE_ID as SUCCESSOR_SETTLEMENT_EVIDENCE_SOURCE_ID,
    SETTLEMENT_EVIDENCE_VERSION as SUCCESSOR_SETTLEMENT_EVIDENCE_VERSION,
    UNTOUCHED_TEST_RUNNER_VERSION as SUCCESSOR_UNTOUCHED_TEST_RUNNER_VERSION,
    UNTOUCHED_TEST_STATE_VERSION as SUCCESSOR_UNTOUCHED_TEST_STATE_VERSION,
    WALK_FORWARD_EVALUATOR_VERSION as SUCCESSOR_WALK_FORWARD_EVALUATOR_VERSION,
)
from .strategy_bots.rti_microstructure_v15_paper_identity import (
    ARTIFACT_VERSION as SUCCESSOR_PAPER_ARTIFACT_VERSION,
    AUTOMATIC_PROMOTION as SUCCESSOR_PAPER_AUTOMATIC_PROMOTION,
    LEDGER_VERSION as SUCCESSOR_PAPER_LEDGER_VERSION,
    NOTIFICATIONS_ENABLED as SUCCESSOR_PAPER_NOTIFICATIONS_ENABLED,
    PAPER_ARTIFACT_CREATED as SUCCESSOR_PAPER_ARTIFACT_CREATED,
    PROTOCOL_FROZEN as SUCCESSOR_PAPER_PROTOCOL_FROZEN,
    PROTOCOL_ID as SUCCESSOR_PAPER_PROTOCOL_ID,
    PROTOCOL_SHA256 as SUCCESSOR_PAPER_PROTOCOL_SHA256,
    REAL_TRADING_ALLOWED as SUCCESSOR_PAPER_REAL_TRADING_ALLOWED,
    RUNTIME_SCORING_CONNECTED as SUCCESSOR_PAPER_RUNTIME_SCORING_CONNECTED,
)
from .strategy_bots.rti_independent_path_degradation_identity import (
    EVALUATION_GRACE_SECONDS as DEGRADATION_EVALUATION_GRACE_SECONDS,
    FIRST_ELIGIBLE_CLOSE_TIME as DEGRADATION_FIRST_ELIGIBLE_CLOSE_TIME,
    POLICY_ID as DEGRADATION_POLICY_ID,
    POLICY_SHA256 as DEGRADATION_POLICY_SHA256,
    PROSPECTIVE_AFTER_CLOSE_TIME as DEGRADATION_PROSPECTIVE_AFTER_CLOSE_TIME,
)
from .strategy_bots.rti_scheduled_maintenance_identity import (
    SHA256 as SCHEDULED_MAINTENANCE_SHA256,
    VERSION as SCHEDULED_MAINTENANCE_VERSION,
)
from .v13_readiness_monitor import (
    CAPTURE_PROTECTED_AFTER_SECONDS,
    CAPTURE_PROTECTED_BEFORE_SECONDS,
    V13ReadinessMonitor,
)


def _enabled() -> bool:
    # Historical milestones/degradation backfill are complete.  This heavy
    # in-process scan is opt-in after it held the GIL through a delayed-capture
    # deadline; current feed watchdogs cover live source health.
    return os.environ.get(
        "Q15_INDEPENDENT_PATH_READINESS_MONITOR", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}


def _interval_seconds() -> float:
    try:
        value = float(os.environ.get(
            "Q15_INDEPENDENT_PATH_READINESS_INTERVAL_SECONDS", "300"
        ))
    except (TypeError, ValueError):
        value = 300.0
    return min(3600.0, max(60.0, value))


class IndependentPathReadinessMonitor(V13ReadinessMonitor):
    NOTICE_MODULE = "tools.q15_rti_independent_path_readiness_notice"
    LOG_LABEL = "INDEPENDENT PATH"
    THREAD_NAME = "q15-independent-path-paper-readiness"
    STOP_AFTER_ALL_MILESTONES = False

    def __init__(self, **kwargs: Any) -> None:
        if "enabled" not in kwargs:
            kwargs["enabled"] = _enabled()
        if "interval_seconds" not in kwargs:
            kwargs["interval_seconds"] = _interval_seconds()
        super().__init__(**kwargs)
        self._completed_degradation_close_times: set[int] = set()
        self._last_degradation_result: dict[str, Any] | None = None
        self._degradation_notice_attempts = 0

    def check_once(self) -> dict[str, Any]:
        milestone_result = super().check_once()
        notice = self._notice_module()
        with self._lock:
            snapshot = dict(self._last_snapshot or {})
            completed = set(self._completed_degradation_close_times)
        events = [
            event for event in notice.ready_degradation_events(snapshot)
            if int(float(event["close_time"])) not in completed
        ]
        if events:
            if self._sender is None:
                factory = self._sender_factory or self._default_sender_factory
                self._sender = factory()
            pending_snapshot = {
                **snapshot,
                "prospective_degradation_events": events,
                "prospective_degradation_event_count": len(events),
            }
            degradation_result = notice.send_degradation_notices(
                pending_snapshot, self._sender,
            )
        else:
            degradation_result = {
                "status": "NO_PROSPECTIVE_DEGRADATION",
                "notice_attempted": False,
                "ready_close_times": [],
                "deliveries": {},
            }
        newly_completed = set()
        for close_time, raw in dict(
            degradation_result.get("deliveries") or {}
        ).items():
            delivery = dict(raw or {})
            if delivery.get("delivered") or delivery.get("muted"):
                newly_completed.add(int(close_time))
        with self._lock:
            self._completed_degradation_close_times.update(newly_completed)
            self._last_degradation_result = dict(degradation_result)
            if degradation_result.get("notice_attempted"):
                self._degradation_notice_attempts += 1
        return {
            **milestone_result,
            "degradation_notice": dict(degradation_result),
        }

    def health(self) -> dict[str, Any]:
        health = super().health()
        with self._lock:
            snapshot = dict(self._last_snapshot or {})
            degradation_result = dict(self._last_degradation_result or {})
            completed_degradation = sorted(
                self._completed_degradation_close_times
            )
            degradation_notice_attempts = self._degradation_notice_attempts
        geometry = dict(snapshot.get("geometry") or {})
        geometry_review = dict(snapshot.get("geometry_review") or {})
        source_quality = dict(snapshot.get("source_quality") or {})
        evidence_identity = dict(
            snapshot.get(
                "geometry_review_selected_feature_evidence_identity"
            ) or {}
        )
        contract_identity = dict(
            snapshot.get("geometry_review_contract_identity") or {}
        )
        for key in (
            "soft_input_integrity_status",
            "fully_observed_rows",
            "soft_degraded_rows",
            "fully_observed_close_windows",
            "soft_degraded_close_windows",
            "soft_degradation_by_asset",
            "soft_degradation_by_reason",
            "soft_degradation_changes_readiness_credit",
        ):
            health.pop(key, None)
        return {
            **health,
            "audit_version": snapshot.get("audit_version"),
            "reference_formula_verifier_version": snapshot.get(
                "reference_formula_verifier_version"
            ),
            "reference_formula_mismatch_rows": snapshot.get(
                "reference_formula_mismatch_rows"
            ),
            "source_design_id": snapshot.get("design_id"),
            "source_design_sha256": snapshot.get("design_sha256"),
            "complete_reconstructable_close_windows": snapshot.get(
                "complete_reconstructable_close_windows"
            ),
            "complete_reconstructable_rows": snapshot.get(
                "complete_reconstructable_rows"
            ),
            "successor_audit_complete_close_windows": snapshot.get(
                "successor_audit_complete_close_windows"
            ),
            "successor_audit_complete_rows": snapshot.get(
                "successor_audit_complete_rows"
            ),
            "successor_audit_feature_ineligible_source_windows": snapshot.get(
                "successor_audit_feature_ineligible_source_windows"
            ),
            "successor_audit_population_outcome_labels_read": snapshot.get(
                "successor_audit_population_outcome_labels_read"
            ),
            "successor_audit_population_model_fit_performed": snapshot.get(
                "successor_audit_population_model_fit_performed"
            ),
            "eligible_close_windows": snapshot.get("eligible_close_windows"),
            "valid_rows": snapshot.get("valid_rows"),
            "invalid_rows_excluded_from_credit": snapshot.get(
                "invalid_rows_excluded_from_credit"
            ),
            "complete_window_evidence_reconstructable": snapshot.get(
                "complete_window_evidence_reconstructable"
            ),
            "geometry_status": geometry.get("status"),
            "geometry_cohorts": geometry.get("cohorts", {}),
            "geometry_review_protocol_id": geometry_review.get("protocol_id"),
            "geometry_review_protocol_sha256": geometry_review.get(
                "protocol_sha256"
            ),
            "geometry_review_ready": geometry_review.get("review_ready"),
            "geometry_review_status": geometry_review.get("status"),
            "geometry_review_failed_checks": geometry_review.get(
                "failed_checks", []
            ),
            "geometry_review_all_checks_met": geometry_review.get(
                "all_checks_met"
            ),
            "geometry_review_selected_feature_evidence_identity_version": (
                evidence_identity.get("version")
            ),
            "geometry_review_selected_feature_evidence_rows": (
                evidence_identity.get("rows")
            ),
            "geometry_review_selected_feature_evidence_sha256": (
                evidence_identity.get("sha256")
            ),
            "geometry_review_selected_feature_evidence_outcome_columns_selected": (
                evidence_identity.get("outcome_columns_selected")
            ),
            "geometry_review_selected_feature_evidence_outcome_labels_read": (
                evidence_identity.get("outcome_labels_read")
            ),
            "geometry_review_contract_identity_version": (
                contract_identity.get("version")
            ),
            "geometry_review_contract_identity_rows": (
                contract_identity.get("rows")
            ),
            "geometry_review_contract_identity_mismatch_rows": (
                contract_identity.get("mismatch_rows")
            ),
            "geometry_review_contract_identity_dst_fold_safe": (
                contract_identity.get("dst_fold_safe")
            ),
            "geometry_review_contract_identity_outcome_labels_read": (
                contract_identity.get("outcome_labels_read")
            ),
            "source_quality_status": source_quality.get("status"),
            "source_quality_minimum_integrity_margin_seconds": (
                source_quality.get("minimum_integrity_margin_seconds")
            ),
            "source_quality_evidence_parse_failures": source_quality.get(
                "evidence_parse_failures"
            ),
            "source_quality_integrity_breaches": source_quality.get(
                "integrity_breaches"
            ),
            "source_quality_venues": source_quality.get("venues", {}),
            "degradation_notice_policy_id": snapshot.get(
                "degradation_notice_policy_id", DEGRADATION_POLICY_ID
            ),
            "degradation_notice_policy_sha256": snapshot.get(
                "degradation_notice_policy_sha256",
                DEGRADATION_POLICY_SHA256,
            ),
            "degradation_notice_prospective_after_close_time": snapshot.get(
                "degradation_notice_prospective_after_close_time",
                DEGRADATION_PROSPECTIVE_AFTER_CLOSE_TIME,
            ),
            "degradation_notice_first_eligible_close_time": snapshot.get(
                "degradation_notice_first_eligible_close_time",
                DEGRADATION_FIRST_ELIGIBLE_CLOSE_TIME,
            ),
            "degradation_notice_evaluation_grace_seconds": snapshot.get(
                "degradation_notice_evaluation_grace_seconds",
                DEGRADATION_EVALUATION_GRACE_SECONDS,
            ),
            "expected_due_close_count": snapshot.get(
                "expected_due_close_count", 0
            ),
            "entirely_missing_due_close_count": snapshot.get(
                "entirely_missing_due_close_count", 0
            ),
            "scheduled_maintenance_version": snapshot.get(
                "scheduled_maintenance_version",
                SCHEDULED_MAINTENANCE_VERSION,
            ),
            "scheduled_maintenance_sha256": snapshot.get(
                "scheduled_maintenance_sha256",
                SCHEDULED_MAINTENANCE_SHA256,
            ),
            "scheduled_maintenance_windows": snapshot.get(
                "scheduled_maintenance_windows", 0
            ),
            "scheduled_maintenance_due_close_count": snapshot.get(
                "scheduled_maintenance_due_close_count", 0
            ),
            "scheduled_maintenance_events": snapshot.get(
                "scheduled_maintenance_events", []
            ),
            "scheduled_maintenance_receives_audit_credit": snapshot.get(
                "scheduled_maintenance_receives_audit_credit", False
            ),
            "prospective_degradation_event_count": snapshot.get(
                "prospective_degradation_event_count", 0
            ),
            "prospective_degradation_events": snapshot.get(
                "prospective_degradation_events", []
            ),
            "historical_incomplete_windows_ignored": snapshot.get(
                "historical_incomplete_windows_ignored", 0
            ),
            "completed_degradation_close_times": completed_degradation,
            "degradation_notice_attempts": degradation_notice_attempts,
            "last_degradation_notice_status": degradation_result.get(
                "status"
            ),
            "last_degradation_notice_deliveries": degradation_result.get(
                "deliveries", {}
            ),
            "feature_selection_performed": False,
            "thresholds_selected_from_outcomes": False,
            "successor_charter_id": SUCCESSOR_CHARTER_ID,
            "successor_charter_sha256": SUCCESSOR_CHARTER_SHA256,
            "successor_proposed_design_id": SUCCESSOR_PROPOSED_DESIGN_ID,
            "successor_evaluation_protocol_id": (
                SUCCESSOR_EVALUATION_PROTOCOL_ID
            ),
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
            "successor_design_binding_status": snapshot.get(
                "successor_design_binding_status"
            ),
            "successor_geometry_payload_sha256": snapshot.get(
                "successor_geometry_payload_sha256"
            ),
            "successor_runtime_feature_construction_connected": (
                SUCCESSOR_RUNTIME_FEATURE_CONSTRUCTION_CONNECTED
            ),
            "successor_outcome_access_allowed": (
                SUCCESSOR_OUTCOME_ACCESS_ALLOWED
            ),
            "successor_model_fit_performed": SUCCESSOR_MODEL_FIT_PERFORMED,
            "successor_runtime_scoring_connected": (
                SUCCESSOR_RUNTIME_SCORING_CONNECTED
            ),
            "successor_historical_audit_tooling_ready": (
                SUCCESSOR_HISTORICAL_AUDIT_TOOLING_READY
            ),
            "successor_audit_seal_version": SUCCESSOR_AUDIT_SEAL_VERSION,
            "successor_walk_forward_evaluator_version": (
                SUCCESSOR_WALK_FORWARD_EVALUATOR_VERSION
            ),
            "successor_pretest_runner_version": (
                SUCCESSOR_PRETEST_RUNNER_VERSION
            ),
            "successor_pretest_state_version": (
                SUCCESSOR_PRETEST_STATE_VERSION
            ),
            "successor_untouched_test_runner_version": (
                SUCCESSOR_UNTOUCHED_TEST_RUNNER_VERSION
            ),
            "successor_untouched_test_state_version": (
                SUCCESSOR_UNTOUCHED_TEST_STATE_VERSION
            ),
            "successor_settlement_evidence_version": (
                SUCCESSOR_SETTLEMENT_EVIDENCE_VERSION
            ),
            "successor_settlement_evidence_source_id": (
                SUCCESSOR_SETTLEMENT_EVIDENCE_SOURCE_ID
            ),
            "successor_paper_protocol_frozen": (
                SUCCESSOR_PAPER_PROTOCOL_FROZEN
            ),
            "successor_paper_protocol_id": SUCCESSOR_PAPER_PROTOCOL_ID,
            "successor_paper_protocol_sha256": (
                SUCCESSOR_PAPER_PROTOCOL_SHA256
            ),
            "successor_paper_artifact_version": (
                SUCCESSOR_PAPER_ARTIFACT_VERSION
            ),
            "successor_paper_ledger_version": (
                SUCCESSOR_PAPER_LEDGER_VERSION
            ),
            "successor_paper_artifact_created": (
                SUCCESSOR_PAPER_ARTIFACT_CREATED
            ),
            "successor_paper_runtime_scoring_connected": (
                SUCCESSOR_PAPER_RUNTIME_SCORING_CONNECTED
            ),
            "successor_paper_notifications_enabled": (
                SUCCESSOR_PAPER_NOTIFICATIONS_ENABLED
            ),
            "successor_paper_automatic_promotion": (
                SUCCESSOR_PAPER_AUTOMATIC_PROMOTION
            ),
            "successor_paper_real_trading_allowed": (
                SUCCESSOR_PAPER_REAL_TRADING_ALLOWED
            ),
            "successor_reporting_protocol_id": (
                SUCCESSOR_REPORTING_PROTOCOL_ID
            ),
            "successor_reporting_protocol_sha256": (
                SUCCESSOR_REPORTING_PROTOCOL_SHA256
            ),
            "successor_notification_eligible": (
                SUCCESSOR_NOTIFICATION_ELIGIBLE
            ),
            "successor_automatic_promotion": False,
            "successor_real_trading_allowed": SUCCESSOR_REAL_TRADING_ALLOWED,
            "geometry_freeze_contract_id": GEOMETRY_FREEZE_CONTRACT_ID,
            "geometry_freeze_contract_sha256": GEOMETRY_FREEZE_CONTRACT_SHA256,
            "geometry_freeze_artifact_path": GEOMETRY_FREEZE_ARTIFACT_PATH,
            "geometry_freeze_manual_command_only": True,
            "geometry_freeze_background_write_allowed": False,
        }


_monitor: IndependentPathReadinessMonitor | None = None
_monitor_lock = threading.Lock()


def get_independent_path_readiness_monitor() -> IndependentPathReadinessMonitor:
    global _monitor
    with _monitor_lock:
        if _monitor is None:
            _monitor = IndependentPathReadinessMonitor()
        return _monitor


def start_independent_path_readiness_monitor() -> bool:
    return get_independent_path_readiness_monitor().start()


def independent_path_readiness_monitor_health() -> dict[str, Any]:
    monitor = _monitor
    if monitor is None:
        return {
            "enabled": _enabled(),
            "paper_only": True,
            "administrative_notices_only": True,
            "notification_is_trade_signal": False,
            "outcome_labels_read": False,
            "automatic_scoring": False,
            "automatic_promotion": False,
            "real_trading_allowed": False,
            "feature_selection_performed": False,
            "thresholds_selected_from_outcomes": False,
            "successor_charter_id": SUCCESSOR_CHARTER_ID,
            "successor_charter_sha256": SUCCESSOR_CHARTER_SHA256,
            "successor_proposed_design_id": SUCCESSOR_PROPOSED_DESIGN_ID,
            "successor_evaluation_protocol_id": (
                SUCCESSOR_EVALUATION_PROTOCOL_ID
            ),
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
            "successor_design_binding_status": None,
            "successor_geometry_payload_sha256": None,
            "successor_runtime_feature_construction_connected": (
                SUCCESSOR_RUNTIME_FEATURE_CONSTRUCTION_CONNECTED
            ),
            "successor_outcome_access_allowed": (
                SUCCESSOR_OUTCOME_ACCESS_ALLOWED
            ),
            "successor_model_fit_performed": SUCCESSOR_MODEL_FIT_PERFORMED,
            "successor_runtime_scoring_connected": (
                SUCCESSOR_RUNTIME_SCORING_CONNECTED
            ),
            "successor_historical_audit_tooling_ready": (
                SUCCESSOR_HISTORICAL_AUDIT_TOOLING_READY
            ),
            "successor_audit_seal_version": SUCCESSOR_AUDIT_SEAL_VERSION,
            "successor_walk_forward_evaluator_version": (
                SUCCESSOR_WALK_FORWARD_EVALUATOR_VERSION
            ),
            "successor_pretest_runner_version": (
                SUCCESSOR_PRETEST_RUNNER_VERSION
            ),
            "successor_pretest_state_version": (
                SUCCESSOR_PRETEST_STATE_VERSION
            ),
            "successor_untouched_test_runner_version": (
                SUCCESSOR_UNTOUCHED_TEST_RUNNER_VERSION
            ),
            "successor_untouched_test_state_version": (
                SUCCESSOR_UNTOUCHED_TEST_STATE_VERSION
            ),
            "successor_settlement_evidence_version": (
                SUCCESSOR_SETTLEMENT_EVIDENCE_VERSION
            ),
            "successor_settlement_evidence_source_id": (
                SUCCESSOR_SETTLEMENT_EVIDENCE_SOURCE_ID
            ),
            "successor_paper_protocol_frozen": (
                SUCCESSOR_PAPER_PROTOCOL_FROZEN
            ),
            "successor_paper_protocol_id": SUCCESSOR_PAPER_PROTOCOL_ID,
            "successor_paper_protocol_sha256": (
                SUCCESSOR_PAPER_PROTOCOL_SHA256
            ),
            "successor_paper_artifact_version": (
                SUCCESSOR_PAPER_ARTIFACT_VERSION
            ),
            "successor_paper_ledger_version": (
                SUCCESSOR_PAPER_LEDGER_VERSION
            ),
            "successor_paper_artifact_created": (
                SUCCESSOR_PAPER_ARTIFACT_CREATED
            ),
            "successor_paper_runtime_scoring_connected": (
                SUCCESSOR_PAPER_RUNTIME_SCORING_CONNECTED
            ),
            "successor_paper_notifications_enabled": (
                SUCCESSOR_PAPER_NOTIFICATIONS_ENABLED
            ),
            "successor_paper_automatic_promotion": (
                SUCCESSOR_PAPER_AUTOMATIC_PROMOTION
            ),
            "successor_paper_real_trading_allowed": (
                SUCCESSOR_PAPER_REAL_TRADING_ALLOWED
            ),
            "successor_reporting_protocol_id": (
                SUCCESSOR_REPORTING_PROTOCOL_ID
            ),
            "successor_reporting_protocol_sha256": (
                SUCCESSOR_REPORTING_PROTOCOL_SHA256
            ),
            "successor_notification_eligible": (
                SUCCESSOR_NOTIFICATION_ELIGIBLE
            ),
            "successor_automatic_promotion": False,
            "successor_real_trading_allowed": SUCCESSOR_REAL_TRADING_ALLOWED,
            "complete_reconstructable_close_windows": 0,
            "complete_reconstructable_rows": 0,
            "successor_audit_complete_close_windows": 0,
            "successor_audit_complete_rows": 0,
            "successor_audit_feature_ineligible_source_windows": 0,
            "successor_audit_population_outcome_labels_read": False,
            "successor_audit_population_model_fit_performed": False,
            "geometry_freeze_contract_id": GEOMETRY_FREEZE_CONTRACT_ID,
            "geometry_freeze_contract_sha256": GEOMETRY_FREEZE_CONTRACT_SHA256,
            "geometry_freeze_artifact_path": GEOMETRY_FREEZE_ARTIFACT_PATH,
            "geometry_freeze_manual_command_only": True,
            "geometry_freeze_background_write_allowed": False,
            "geometry_review_protocol_id": GEOMETRY_PROTOCOL_ID,
            "geometry_review_protocol_sha256": GEOMETRY_PROTOCOL_SHA256,
            "geometry_review_ready": False,
            "geometry_review_status": "WAITING_FOR_30_COMPLETE_WINDOWS",
            "degradation_notice_policy_id": DEGRADATION_POLICY_ID,
            "degradation_notice_policy_sha256": DEGRADATION_POLICY_SHA256,
            "degradation_notice_prospective_after_close_time": (
                DEGRADATION_PROSPECTIVE_AFTER_CLOSE_TIME
            ),
            "degradation_notice_first_eligible_close_time": (
                DEGRADATION_FIRST_ELIGIBLE_CLOSE_TIME
            ),
            "degradation_notice_evaluation_grace_seconds": (
                DEGRADATION_EVALUATION_GRACE_SECONDS
            ),
            "expected_due_close_count": 0,
            "entirely_missing_due_close_count": 0,
            "scheduled_maintenance_version": (
                SCHEDULED_MAINTENANCE_VERSION
            ),
            "scheduled_maintenance_sha256": SCHEDULED_MAINTENANCE_SHA256,
            "scheduled_maintenance_windows": 0,
            "scheduled_maintenance_due_close_count": 0,
            "scheduled_maintenance_events": [],
            "scheduled_maintenance_receives_audit_credit": False,
            "prospective_degradation_event_count": 0,
            "prospective_degradation_events": [],
            "historical_incomplete_windows_ignored": 0,
            "completed_degradation_close_times": [],
            "degradation_notice_attempts": 0,
            "last_degradation_notice_status": None,
            "last_degradation_notice_deliveries": {},
            "capture_protection_enabled": True,
            "capture_protected_before_seconds": (
                CAPTURE_PROTECTED_BEFORE_SECONDS
            ),
            "capture_protected_after_seconds": CAPTURE_PROTECTED_AFTER_SECONDS,
            "capture_deferrals": 0,
            "thread_alive": False,
            "all_milestones_completed": False,
            "completed_milestones": [],
            "pending_milestones": ["GEOMETRY_30", "NON_BTC_60", "BTC_150"],
            "checks": 0,
        }
    return monitor.health()


def reset_independent_path_readiness_monitor() -> None:
    global _monitor
    with _monitor_lock:
        if _monitor is not None:
            _monitor.stop()
        _monitor = None
