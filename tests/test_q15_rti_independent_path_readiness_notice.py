from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import time

import pytest

from notifications.outbox_v9 import ReliableTelegramOutbox
from q15_upgrade.independent_path_readiness_monitor import (
    IndependentPathReadinessMonitor,
)
from tools import q15_rti_independent_path_readiness_notice as notice
from tools import q15_rti_microstructure_freeze as freeze


def _cohort(rows: int) -> dict:
    return {
        "rows": rows,
        "feature_count": 5,
        "finite": True,
        "active_feature_count": 5,
        "numerical_rank": 5,
        "stable_rank": 3.2,
        "condition_number_nonzero_subspace": 1.8,
        "maximum_absolute_correlation": 0.3,
        "exact_signed_duplicate_count": 0,
    }


def _source_quality(rows: int) -> dict:
    metric = {"n": rows, "min": 1.0, "p10": 1.2, "median": 2.0, "p90": 3.0, "max": 4.0}
    venue = {
        "rows": rows,
        "point_count": metric,
        "end_effective_age_seconds": metric,
        "max_gap_seconds": metric,
        "max_message_age_seconds": metric,
        "minimum_integrity_margin_seconds": metric,
    }
    return {
        "status": "PASS_ALL_CREDITED_COMPLETE_ROWS",
        "credited_complete_rows": rows,
        "source_thresholds_from_frozen_design": True,
        "thresholds_selected_from_outcomes": False,
        "outcome_labels_read": False,
        "evidence_parse_failures": 0,
        "integrity_breaches": 0,
        "minimum_integrity_margin_seconds": 1.0,
        "venues": {"coinbase": venue, "kraken": venue},
    }


def _selected_evidence_identity(windows: int) -> dict:
    rows = min(windows, 30) * 7
    return {
        "version": notice.SELECTED_EVIDENCE_IDENTITY_VERSION,
        "rows": rows,
        "sha256": f"{rows:064x}",
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
    }


def _selected_contract_identity(windows: int) -> dict:
    return {
        "version": notice.CONTRACT_IDENTITY_VERSION,
        "rows": min(windows, 30) * 7,
        "mismatch_rows": 0,
        "ticker_asset_alignment_required": True,
        "ticker_close_time_alignment_required": True,
        "dst_fold_safe": True,
        "outcome_labels_read": False,
    }


def _geometry_review(windows: int) -> dict:
    ready = windows >= 30
    return {
        "protocol_id": notice.GEOMETRY_PROTOCOL_ID,
        "protocol_sha256": notice.GEOMETRY_PROTOCOL_SHA256,
        "review_window": 30,
        "review_ready": ready,
        "status": (
            "PASS_CONTINUE_PROSPECTIVE_COLLECTION_NO_MODEL"
            if ready else "WAITING_FOR_30_COMPLETE_WINDOWS"
        ),
        "checks": {},
        "failed_checks": [],
        "all_checks_met": ready,
        "pass_does_not_authorize_model_fit": True,
        "pass_does_not_authorize_outcome_access": True,
        "automatic_action_allowed": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
    }
def _snapshot(windows: int = 30, **overrides) -> dict:
    degradation_events = list(
        overrides.pop("prospective_degradation_events", [])
    )
    snapshot = {
        "notice_version": notice.NOTICE_VERSION,
        "audit_version": "q15-rti-independent-path-outcome-blind-audit-v7",
        "reference_formula_verifier_version": (
            "q15-rti-independent-path-reference-equations-v1"
        ),
        "reference_formula_mismatch_rows": 0,
        "design_id": notice.DESIGN_ID,
        "design_sha256": notice.DESIGN_SHA256,
        "complete_executable_close_windows": windows,
        "complete_reconstructable_close_windows": windows,
        "complete_reconstructable_rows": windows * 7,
        "successor_audit_complete_close_windows": windows,
        "successor_audit_complete_rows": windows * 7,
        "successor_audit_feature_ineligible_source_windows": 0,
        "successor_audit_population_outcome_labels_read": False,
        "successor_audit_population_model_fit_performed": False,
        "eligible_close_windows": windows + 2,
        "eligible_rows": windows * 7 + 4,
        "valid_rows": windows * 7,
        "invalid_rows_excluded_from_credit": 4,
        "complete_window_evidence_reconstructable": True,
        "geometry": {
            "status": (
                "READY_FOR_MANUAL_OUTCOME_BLIND_GEOMETRY_REVIEW"
                if windows >= 30 else "WAITING_FOR_30_COMPLETE_WINDOWS"
            ),
            "review_window": 30,
            "thresholds_selected_from_outcomes": False,
            "feature_selection_performed": False,
            "cohorts": {
                "ALL_SEVEN": _cohort(windows * 7),
                "BTC": _cohort(windows),
                "NON_BTC_TRANSFER": _cohort(windows * 6),
            },
        },
        "geometry_review": _geometry_review(windows),
        "geometry_review_selected_feature_evidence_identity": (
            _selected_evidence_identity(windows)
        ),
        "geometry_review_contract_identity": (
            _selected_contract_identity(windows)
        ),
        "source_quality": _source_quality(windows * 7),
        "degradation_notice_policy_id": notice.DEGRADATION_POLICY_ID,
        "degradation_notice_policy_sha256": (
            notice.DEGRADATION_POLICY_SHA256
        ),
        "degradation_notice_prospective_after_close_time": (
            notice.DEGRADATION_PROSPECTIVE_AFTER_CLOSE_TIME
        ),
        "degradation_notice_first_eligible_close_time": (
            notice.DEGRADATION_FIRST_ELIGIBLE_CLOSE_TIME
        ),
        "successor_charter_id": notice.SUCCESSOR_CHARTER_ID,
        "successor_charter_sha256": notice.SUCCESSOR_CHARTER_SHA256,
        "successor_proposed_design_id": notice.SUCCESSOR_PROPOSED_DESIGN_ID,
        "successor_evaluation_protocol_id": (
            notice.SUCCESSOR_EVALUATION_PROTOCOL_ID
        ),
        "successor_evaluation_protocol_sha256": (
            notice.SUCCESSOR_EVALUATION_PROTOCOL_SHA256
        ),
        "successor_executable_design_created": True,
        "successor_executable_design_id": (
            notice.SUCCESSOR_EXECUTABLE_DESIGN_ID
        ),
        "successor_executable_design_sha256": (
            notice.SUCCESSOR_EXECUTABLE_DESIGN_SHA256
        ),
        "successor_design_binding_status": (
            "V15_EXECUTABLE_FEATURE_DESIGN_BOUND_AND_VERIFIED"
        ),
        "successor_geometry_payload_sha256": "a" * 64,
        "successor_runtime_feature_construction_connected": True,
        "successor_outcome_access_allowed": False,
        "successor_model_fit_performed": False,
        "successor_runtime_scoring_connected": False,
        "successor_notification_eligible": False,
        "successor_automatic_promotion": False,
        "successor_real_trading_allowed": False,
        "geometry_freeze_contract_id": notice.GEOMETRY_FREEZE_CONTRACT_ID,
        "geometry_freeze_contract_sha256": (
            notice.GEOMETRY_FREEZE_CONTRACT_SHA256
        ),
        "geometry_freeze_manual_command_only": True,
        "geometry_freeze_background_write_allowed": False,
        "degradation_notice_evaluation_grace_seconds": (
            notice.DEGRADATION_EVALUATION_GRACE_SECONDS
        ),
        "expected_due_close_count": 0,
        "entirely_missing_due_close_count": 0,
        "scheduled_maintenance_version": (
            notice.SCHEDULED_MAINTENANCE_VERSION
        ),
        "scheduled_maintenance_sha256": (
            notice.SCHEDULED_MAINTENANCE_SHA256
        ),
        "scheduled_maintenance_windows": 1,
        "scheduled_maintenance_due_close_count": 0,
        "scheduled_maintenance_events": [],
        "scheduled_maintenance_receives_audit_credit": False,
        "prospective_degradation_events": degradation_events,
        "prospective_degradation_event_count": len(degradation_events),
        "historical_incomplete_windows_ignored": 0,
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
    snapshot.update(overrides)
    return snapshot


def _degradation_event(
    close_time: float | None = None, *, valid_row_count: int = 6,
) -> dict:
    return {
        "close_time": (
            notice.DEGRADATION_FIRST_ELIGIBLE_CLOSE_TIME
            if close_time is None else float(close_time)
        ),
        "valid_row_count": valid_row_count,
        "missing_assets": ["BTC"],
        "source_missing_reasons": {"BTC": "coinbase:path_continuity_gap"},
    }


def test_degradation_policy_identity_is_frozen_and_tamper_fails():
    policy = notice.load_degradation_policy()
    notice.validate_degradation_policy(policy)
    assert notice.design_fingerprint(policy) == (
        notice.DEGRADATION_POLICY_SHA256
    )
    tampered = json.loads(json.dumps(policy))
    tampered["delivery"]["expiration_seconds"] = 60
    with pytest.raises(
        ValueError, match="independent_path_degradation_policy_sha256_mismatch"
    ):
        notice.validate_degradation_policy(tampered)


def test_scheduled_maintenance_identity_is_frozen_and_tamper_fails(
    tmp_path,
):
    maintenance = notice.load_scheduled_maintenance()
    assert notice.design_fingerprint(maintenance) == (
        notice.SCHEDULED_MAINTENANCE_SHA256
    )
    assert maintenance["outcome_labels_used"] is False
    assert maintenance["changes_audit_credit"] is False
    assert all(
        window["audit_credit_allowed"] is False
        for window in maintenance["windows"]
    )
    tampered = json.loads(json.dumps(maintenance))
    tampered["windows"][0]["audit_credit_allowed"] = True
    path = tmp_path / "maintenance.json"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(
        ValueError, match="independent_path_maintenance_identity_invalid"
    ):
        notice.load_scheduled_maintenance(path)


def test_path_notice_projection_cannot_include_outcomes():
    assert freeze.OUTCOME_COLUMNS.isdisjoint(freeze.FEATURE_SELECT_COLUMNS)


def test_snapshot_builder_uses_reconstructable_feature_only_report(
    monkeypatch, tmp_path,
):
    loaded = []

    def fake_load(path: Path):
        loaded.append(path)
        return [{"feature_only": True}]

    monkeypatch.setattr(notice, "load_feature_rows", fake_load)
    monkeypatch.setattr(
        notice,
        "complete_audit_windows",
        lambda rows: {float(index): [] for index in range(29)},
    )
    monkeypatch.setattr(notice, "build_report", lambda rows, design: {
        "audit_version": notice.AUDIT_VERSION,
        "reference_formula_verifier_version": notice.REFERENCE_VERSION,
        "reference_formula_mismatch_rows": 0,
        "complete_seven_asset_close_windows": 30,
        "eligible_close_windows": 32,
        "eligible_rows": 214,
        "valid_rows": 210,
        "invalid_rows": 4,
        "outcome_blind_geometry": {
            "status": "READY_FOR_MANUAL_OUTCOME_BLIND_GEOMETRY_REVIEW",
            "review_window": 30,
            "thresholds_selected_from_outcomes": False,
            "feature_selection_performed": False,
            "cohorts": {
                "ALL_SEVEN": _cohort(210),
                "BTC": _cohort(30),
                "NON_BTC_TRANSFER": _cohort(180),
            },
        },
        "geometry_review": _geometry_review(30),
        "geometry_review_evidence": {
            "source_quality": {
                "selected_feature_evidence_identity": (
                    _selected_evidence_identity(30)
                ),
                "contract_identity": _selected_contract_identity(30),
            },
        },
        "source_quality": _source_quality(210),
        "incomplete_windows": [{
            "close_time": notice.DEGRADATION_FIRST_ELIGIBLE_CLOSE_TIME - 900,
            "valid_row_count": 6,
            "missing_assets": ["BTC"],
            "source_missing_reasons": {"BTC": "historical_gap"},
        }],
    })
    db = tmp_path / "features.sqlite3"
    snapshot = notice.build_outcome_blind_snapshot(
        database_path=db,
        now=(
            notice.DEGRADATION_FIRST_ELIGIBLE_CLOSE_TIME
            - notice.EXACT_DECISION_LEAD_SECONDS
            + notice.DEGRADATION_EVALUATION_GRACE_SECONDS
            - 1
        ),
    )
    assert loaded == [db]
    assert notice.ready_milestones(snapshot) == ["GEOMETRY_30"]
    assert snapshot["invalid_rows_excluded_from_credit"] == 4
    assert snapshot["successor_audit_complete_close_windows"] == 29
    assert snapshot["successor_audit_complete_rows"] == 29 * 7
    assert snapshot[
        "successor_audit_feature_ineligible_source_windows"
    ] == 1
    assert snapshot["outcome_labels_read"] is False
    assert snapshot["model_fit_performed"] is False
    assert snapshot["feature_selection_performed"] is False
    assert snapshot[
        "geometry_review_selected_feature_evidence_identity"
    ] == _selected_evidence_identity(30)
    assert snapshot["geometry_review_contract_identity"] == (
        _selected_contract_identity(30)
    )
    assert snapshot["prospective_degradation_event_count"] == 0
    assert snapshot["historical_incomplete_windows_ignored"] == 1


def test_expected_close_is_due_only_after_exact_capture_grace():
    decision_time = (
        notice.DEGRADATION_FIRST_ELIGIBLE_CLOSE_TIME
        - notice.EXACT_DECISION_LEAD_SECONDS
    )
    assert notice._expected_due_close_times(
        decision_time + notice.DEGRADATION_EVALUATION_GRACE_SECONDS - 0.001
    ) == []
    assert notice._expected_due_close_times(
        decision_time + notice.DEGRADATION_EVALUATION_GRACE_SECONDS
    ) == [notice.DEGRADATION_FIRST_ELIGIBLE_CLOSE_TIME]


def test_snapshot_builder_detects_an_entirely_missing_due_close(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(notice, "load_feature_rows", lambda path: [])
    monkeypatch.setattr(notice, "build_report", lambda rows, design: {
        "complete_seven_asset_close_windows": 0,
        "complete_close_times": [],
        "eligible_close_windows": 0,
        "eligible_rows": 0,
        "valid_rows": 0,
        "invalid_rows": 0,
        "incomplete_windows": [],
        "outcome_blind_geometry": {
            "status": "WAITING_FOR_30_COMPLETE_WINDOWS",
            "review_window": 30,
            "thresholds_selected_from_outcomes": False,
            "feature_selection_performed": False,
            "cohorts": {
                "ALL_SEVEN": _cohort(0),
                "BTC": _cohort(0),
                "NON_BTC_TRANSFER": _cohort(0),
            },
        },
        "geometry_review": _geometry_review(0),
        "source_quality": _source_quality(0),
    })
    decision_time = (
        notice.DEGRADATION_FIRST_ELIGIBLE_CLOSE_TIME
        - notice.EXACT_DECISION_LEAD_SECONDS
    )
    snapshot = notice.build_outcome_blind_snapshot(
        database_path=tmp_path / "features.sqlite3",
        now=decision_time + notice.DEGRADATION_EVALUATION_GRACE_SECONDS,
    )
    events = notice.ready_degradation_events(snapshot)
    assert snapshot["expected_due_close_count"] == 1
    assert snapshot["entirely_missing_due_close_count"] == 1
    assert len(events) == 1
    assert events[0]["valid_row_count"] == 0
    assert set(events[0]["missing_assets"]) == {
        "BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP",
    }
    assert set(events[0]["source_missing_reasons"].values()) == {
        "no_strategy_row_recorded_after_exact_decision"
    }


def test_user_confirmed_maintenance_is_excluded_without_audit_credit(
    monkeypatch, tmp_path,
):
    maintenance = notice.load_scheduled_maintenance()
    start = float(maintenance["windows"][0]["start_close_time_inclusive"])
    earlier = [
        value for value in notice._expected_due_close_times(
            start - notice.EXACT_DECISION_LEAD_SECONDS
            + notice.DEGRADATION_EVALUATION_GRACE_SECONDS
        )
        if value < start
    ]
    windows = len(earlier)
    monkeypatch.setattr(notice, "load_feature_rows", lambda path: [])
    monkeypatch.setattr(notice, "build_report", lambda rows, design: {
        "complete_seven_asset_close_windows": windows,
        "complete_close_times": earlier,
        "eligible_close_windows": windows,
        "eligible_rows": windows * 7,
        "valid_rows": windows * 7,
        "invalid_rows": 0,
        "incomplete_windows": [],
        "outcome_blind_geometry": {
            "status": "READY_FOR_MANUAL_OUTCOME_BLIND_GEOMETRY_REVIEW",
            "review_window": 30,
            "thresholds_selected_from_outcomes": False,
            "feature_selection_performed": False,
            "cohorts": {
                "ALL_SEVEN": _cohort(windows * 7),
                "BTC": _cohort(windows),
                "NON_BTC_TRANSFER": _cohort(windows * 6),
            },
        },
        "geometry_review": _geometry_review(windows),
        "source_quality": _source_quality(windows * 7),
    })
    snapshot = notice.build_outcome_blind_snapshot(
        database_path=tmp_path / "features.sqlite3",
        now=(
            start - notice.EXACT_DECISION_LEAD_SECONDS
            + notice.DEGRADATION_EVALUATION_GRACE_SECONDS
        ),
    )
    assert snapshot["entirely_missing_due_close_count"] == 0
    assert snapshot["prospective_degradation_event_count"] == 0
    assert snapshot["scheduled_maintenance_due_close_count"] == 1
    event = snapshot["scheduled_maintenance_events"][0]
    assert event["close_time"] == start
    assert event["maintenance_reason"] == "scheduled_maintenance"
    assert event["audit_credit_allowed"] is False
    assert snapshot["scheduled_maintenance_receives_audit_credit"] is False


@pytest.mark.parametrize(("windows", "ready"), [(29, False), (30, True), (31, True)])
def test_only_frozen_geometry_milestone_becomes_ready(windows, ready):
    assert notice.ready_milestones(_snapshot(windows)) == (
        ["GEOMETRY_30"] if ready else []
    )


@pytest.mark.parametrize(
    ("windows", "expected"),
    [
        (59, ["GEOMETRY_30"]),
        (60, ["GEOMETRY_30", "NON_BTC_60"]),
        (149, ["GEOMETRY_30", "NON_BTC_60"]),
        (150, ["GEOMETRY_30", "NON_BTC_60", "BTC_150"]),
    ],
)
def test_cohort_audit_milestones_are_separate_and_fixed(windows, expected):
    assert notice.ready_milestones(_snapshot(windows)) == expected


def test_v15_audit_milestones_use_stricter_feature_complete_counter():
    snapshot = _snapshot(
        60,
        successor_audit_complete_close_windows=59,
        successor_audit_complete_rows=59 * 7,
        successor_audit_feature_ineligible_source_windows=1,
    )
    assert notice.ready_milestones(snapshot) == ["GEOMETRY_30"]
    message = notice.readiness_message(snapshot, "NON_BTC_60")
    assert "V15-auditable seven-asset windows: 59/60" in message
    assert "source-complete: 60" in message


@pytest.mark.parametrize(
    ("override", "value"),
    [
        ("successor_audit_complete_close_windows", 61),
        ("successor_audit_complete_rows", 1),
        ("successor_audit_feature_ineligible_source_windows", -1),
        ("successor_audit_population_outcome_labels_read", True),
        ("successor_audit_population_model_fit_performed", True),
    ],
)
def test_unsafe_successor_audit_population_blocks_milestones(
    override, value,
):
    snapshot = _snapshot(60, **{override: value})
    assert notice.ready_milestones(snapshot) == []


def test_failed_geometry_allows_review_notice_but_blocks_later_audits():
    snapshot = _snapshot(150)
    snapshot["geometry_review"] = {
        **snapshot["geometry_review"],
        "status": "GEOMETRY_REVIEW_REQUIRED_NO_AUTOMATIC_ACTION",
        "all_checks_met": False,
        "failed_checks": ["all_pairwise_correlations_within_ceiling"],
    }
    assert notice.ready_milestones(snapshot) == ["GEOMETRY_30"]


@pytest.mark.parametrize(
    ("override", "value"),
    [
        ("complete_window_evidence_reconstructable", False),
        ("outcome_columns_selected", True),
        ("outcome_labels_read", True),
        ("model_fit_performed", True),
        ("feature_selection_performed", True),
        ("thresholds_selected_from_outcomes", True),
        ("artifact_emitted", True),
        ("automatic_scoring", True),
        ("automatic_promotion", True),
        ("notification_is_trade_signal", True),
        ("real_trading_allowed", True),
        ("audit_version", "wrong"),
        ("reference_formula_verifier_version", "wrong"),
        ("reference_formula_mismatch_rows", 1),
        ("design_sha256", "wrong"),
        ("successor_charter_sha256", "wrong"),
        ("successor_evaluation_protocol_sha256", "wrong"),
        ("successor_executable_design_created", False),
        ("successor_executable_design_sha256", "wrong"),
        ("successor_design_binding_status", "wrong"),
        ("successor_runtime_feature_construction_connected", False),
        ("successor_outcome_access_allowed", True),
        ("successor_model_fit_performed", True),
        ("successor_runtime_scoring_connected", True),
        ("successor_notification_eligible", True),
        ("successor_automatic_promotion", True),
        ("successor_real_trading_allowed", True),
        ("geometry_freeze_contract_sha256", "wrong"),
        ("geometry_freeze_manual_command_only", False),
        ("geometry_freeze_background_write_allowed", True),
    ],
)
def test_any_unsafe_evidence_blocks_notice(override, value):
    calls = []
    result = notice.send_ready_milestones(
        _snapshot(30, **{override: value}),
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    assert result["status"] == "WAITING_FOR_MILESTONE"
    assert result["notice_attempted"] is False
    assert calls == []


def test_rank_deficiency_does_not_hide_that_manual_review_is_ready():
    snapshot = _snapshot(30)
    snapshot["geometry"]["cohorts"]["BTC"]["numerical_rank"] = 4
    snapshot["geometry_review"] = {
        **snapshot["geometry_review"],
        "status": "GEOMETRY_REVIEW_REQUIRED_NO_AUTOMATIC_ACTION",
        "all_checks_met": False,
        "failed_checks": ["all_active_matrices_full_rank"],
    }
    assert notice.ready_milestones(snapshot) == ["GEOMETRY_30"]


def test_source_quality_breach_blocks_readiness_notice():
    snapshot = _snapshot(30)
    snapshot["source_quality"] = {
        **snapshot["source_quality"],
        "status": "SOURCE_QUALITY_REVIEW_REQUIRED",
        "integrity_breaches": 1,
    }
    assert notice.ready_milestones(snapshot) == []


def test_geometry_protocol_tampering_blocks_readiness_notice():
    snapshot = _snapshot(30)
    snapshot["geometry_review"] = {
        **snapshot["geometry_review"],
        "protocol_sha256": "wrong",
    }
    assert notice.ready_milestones(snapshot) == []


def test_selected_evidence_identity_tampering_blocks_readiness_notice():
    snapshot = _snapshot(30)
    snapshot["geometry_review_selected_feature_evidence_identity"] = {
        **snapshot["geometry_review_selected_feature_evidence_identity"],
        "sha256": "wrong",
    }
    assert notice.ready_milestones(snapshot) == []


def test_contract_identity_tampering_blocks_readiness_notice():
    snapshot = _snapshot(30)
    snapshot["geometry_review_contract_identity"] = {
        **snapshot["geometry_review_contract_identity"],
        "mismatch_rows": 1,
    }
    assert notice.ready_milestones(snapshot) == []


def test_message_and_delivery_are_paper_admin_and_idempotent():
    snapshot = _snapshot(30)
    message = notice.readiness_message(snapshot, "GEOMETRY_30")
    assert "PAPER ADMIN" in message
    assert "SEALED / unread" in message
    assert "NOT RUN" in message
    assert "not a trade signal" in message
    sent = []

    def sender(text, **kwargs):
        sent.append((text, kwargs))
        return {"ok": True, "delivered": True, "muted": False}

    result = notice.send_ready_milestones(snapshot, sender, now=1000.0)
    assert result["ready_milestones"] == ["GEOMETRY_30"]
    assert len(sent) == 1
    assert sent[0][1]["idempotency_key"] == notice.IDEMPOTENCY_KEYS[
        "GEOMETRY_30"
    ]


def test_non_btc_and_btc_messages_preserve_cohort_separation():
    snapshot = _snapshot(150)
    non_btc = notice.readiness_message(snapshot, "NON_BTC_60")
    btc = notice.readiness_message(snapshot, "BTC_150")
    assert "V15 NON-BTC" in non_btc
    assert "V15-auditable seven-asset windows: 150/60" in non_btc
    assert "keep BTC outcomes sealed" in non_btc
    assert notice.SUCCESSOR_EVALUATION_PROTOCOL_ID in non_btc
    assert notice.GEOMETRY_FREEZE_CONTRACT_ID in non_btc
    assert "V15 BTC" in btc
    assert "without pooling non-BTC rows" in btc
    assert "PAPER ADMIN" in non_btc
    assert "PAPER ADMIN" in btc
    assert "not a trade signal" in non_btc
    assert "not a trade signal" in btc


def test_complete_window_has_no_prospective_degradation_event():
    snapshot = _snapshot(29)
    assert notice.ready_degradation_events(snapshot) == []
    assert notice.send_degradation_notices(
        snapshot, lambda *args, **kwargs: None
    )["notice_attempted"] is False


def test_new_incomplete_window_is_ready_and_message_is_non_trading():
    event = _degradation_event()
    snapshot = _snapshot(
        29, prospective_degradation_events=[event],
    )
    assert notice.ready_degradation_events(snapshot) == [event]
    message = notice.degradation_message(snapshot, event)
    assert "PATH SOURCE DEGRADED | PAPER ADMIN" in message
    assert "6/7" in message
    assert "BTC: coinbase:path_continuity_gap" in message
    assert "excluded from readiness credit" in message
    assert "backfill is forbidden" in message
    assert "SEALED / unread" in message
    assert "not a trade signal" in message


@pytest.mark.parametrize(
    ("override", "value"),
    [
        ("degradation_notice_policy_sha256", "wrong"),
        ("outcome_labels_read", True),
        ("model_fit_performed", True),
        ("automatic_scoring", True),
        ("notification_is_trade_signal", True),
        ("real_trading_allowed", True),
    ],
)
def test_unsafe_snapshot_blocks_prospective_degradation_notice(override, value):
    snapshot = _snapshot(
        29, prospective_degradation_events=[_degradation_event()],
        **{override: value},
    )
    assert notice.ready_degradation_events(snapshot) == []


def test_historical_event_cannot_be_reintroduced_into_notice_snapshot():
    event = _degradation_event(
        notice.DEGRADATION_FIRST_ELIGIBLE_CLOSE_TIME - 900
    )
    snapshot = _snapshot(29, prospective_degradation_events=[event])
    assert notice.ready_degradation_events(snapshot) == []


def test_degradation_delivery_uses_policy_bound_idempotency_key():
    event = _degradation_event()
    snapshot = _snapshot(29, prospective_degradation_events=[event])
    sent = []

    def sender(text, **kwargs):
        sent.append((text, kwargs))
        return {"ok": True, "delivered": True, "muted": False}

    result = notice.send_degradation_notices(snapshot, sender, now=1000.0)
    close_time = int(event["close_time"])
    assert result["ready_close_times"] == [close_time]
    assert sent[0][1]["idempotency_key"] == (
        f"{notice.DEGRADATION_POLICY_ID}:"
        f"{notice.DEGRADATION_POLICY_SHA256}:{close_time}"
    )
    assert sent[0][1]["expires_at"] == 1000.0 + 31536000.0


@dataclass(frozen=True)
class _DisabledStore:
    enabled: bool = False


class _RawTelegram:
    enabled = True
    token = "test-token"

    def __init__(self):
        self.sent = []

    def send_with_result(self, text):
        self.sent.append(text)
        return {
            "ok": True,
            "delivered": True,
            "muted": False,
            "message_id": len(self.sent),
        }


def test_repeated_degradation_delivery_is_durable_and_idempotent(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("Q15_V9_OUTBOX_WORKER", "false")
    monkeypatch.setenv("Q15_V9_DISABLE_NETWORK", "false")
    raw = _RawTelegram()
    outbox = ReliableTelegramOutbox(
        _DisabledStore(), raw, sqlite_path=str(tmp_path / "outbox.sqlite3")
    )
    snapshot = _snapshot(
        29, prospective_degradation_events=[_degradation_event()]
    )
    try:
        now = time.time()
        first = notice.send_degradation_notices(
            snapshot, outbox.send_with_result, now=now
        )
        second = notice.send_degradation_notices(
            snapshot, outbox.send_with_result, now=now + 1.0
        )
        assert first["notice_attempted"] is True
        assert second["notice_attempted"] is True
        assert len(raw.sent) == 1
        assert len(outbox.rows()) == 1
        assert outbox.rows()[0]["status"] == "SENT"
    finally:
        outbox.close()


def test_monitor_waits_without_sender_then_exposes_safe_ready_health():
    factory_calls = []
    waiting = IndependentPathReadinessMonitor(
        enabled=True,
        snapshot_builder=lambda: _snapshot(29),
        sender_factory=lambda: factory_calls.append(True),
    )
    assert waiting.check_once()["status"] == "WAITING_FOR_MILESTONE"
    assert factory_calls == []

    sent = []

    def factory():
        factory_calls.append(True)

        def sender(text, **kwargs):
            sent.append((text, kwargs))
            return {"ok": True, "delivered": True, "muted": False}

        return sender

    ready = IndependentPathReadinessMonitor(
        enabled=True,
        snapshot_builder=lambda: _snapshot(30),
        sender_factory=factory,
    )
    ready.check_once()
    health = ready.health()
    assert len(sent) == 1
    assert health["completed_milestones"] == ["GEOMETRY_30"]
    assert health["complete_reconstructable_close_windows"] == 30
    assert health["invalid_rows_excluded_from_credit"] == 4
    assert health["audit_version"] == (
        "q15-rti-independent-path-outcome-blind-audit-v7"
    )
    assert health["reference_formula_verifier_version"] == (
        "q15-rti-independent-path-reference-equations-v1"
    )
    assert health["reference_formula_mismatch_rows"] == 0
    assert health["source_quality_status"] == (
        "PASS_ALL_CREDITED_COMPLETE_ROWS"
    )
    assert health["source_quality_integrity_breaches"] == 0
    assert health["geometry_review_protocol_sha256"] == (
        notice.GEOMETRY_PROTOCOL_SHA256
    )
    assert health["geometry_review_ready"] is True
    assert health["geometry_review_status"] == (
        "PASS_CONTINUE_PROSPECTIVE_COLLECTION_NO_MODEL"
    )
    assert health[
        "geometry_review_selected_feature_evidence_identity_version"
    ] == notice.SELECTED_EVIDENCE_IDENTITY_VERSION
    assert health["geometry_review_selected_feature_evidence_rows"] == 210
    assert health["geometry_review_selected_feature_evidence_sha256"] == (
        _selected_evidence_identity(30)["sha256"]
    )
    assert health[
        "geometry_review_selected_feature_evidence_outcome_columns_selected"
    ] is False
    assert health[
        "geometry_review_selected_feature_evidence_outcome_labels_read"
    ] is False
    assert health["geometry_review_contract_identity_version"] == (
        notice.CONTRACT_IDENTITY_VERSION
    )
    assert health["geometry_review_contract_identity_rows"] == 210
    assert health["geometry_review_contract_identity_mismatch_rows"] == 0
    assert health["geometry_review_contract_identity_dst_fold_safe"] is True
    assert health[
        "geometry_review_contract_identity_outcome_labels_read"
    ] is False
    assert health["notification_is_trade_signal"] is False
    assert health["outcome_labels_read"] is False
    assert health["automatic_scoring"] is False
    assert health["feature_selection_performed"] is False
    assert health["real_trading_allowed"] is False
    assert health["successor_executable_design_created"] is True
    assert health["successor_executable_design_id"] == (
        notice.SUCCESSOR_EXECUTABLE_DESIGN_ID
    )
    assert health["successor_executable_design_sha256"] == (
        notice.SUCCESSOR_EXECUTABLE_DESIGN_SHA256
    )
    assert health["successor_design_binding_status"] == (
        "V15_EXECUTABLE_FEATURE_DESIGN_BOUND_AND_VERIFIED"
    )
    assert health["successor_runtime_feature_construction_connected"] is True
    assert health["successor_outcome_access_allowed"] is False
    assert health["successor_model_fit_performed"] is False
    assert health["successor_runtime_scoring_connected"] is False
    assert health["successor_notification_eligible"] is False
    assert health["successor_real_trading_allowed"] is False


def test_monitor_sends_each_prospective_degradation_only_once():
    factory_calls = []
    sent = []
    snapshot = _snapshot(
        29, prospective_degradation_events=[_degradation_event()]
    )

    def factory():
        factory_calls.append(True)

        def sender(text, **kwargs):
            sent.append((text, kwargs))
            return {"ok": True, "delivered": True, "muted": False}

        return sender

    monitor = IndependentPathReadinessMonitor(
        enabled=True,
        snapshot_builder=lambda: snapshot,
        sender_factory=factory,
    )
    first = monitor.check_once()
    second = monitor.check_once()
    close_time = int(notice.DEGRADATION_FIRST_ELIGIBLE_CLOSE_TIME)
    assert first["degradation_notice"]["notice_attempted"] is True
    assert second["degradation_notice"]["notice_attempted"] is False
    assert len(factory_calls) == 1
    assert len(sent) == 1
    health = monitor.health()
    assert health["completed_degradation_close_times"] == [close_time]
    assert health["degradation_notice_attempts"] == 1
    assert health["prospective_degradation_event_count"] == 1
    assert health["outcome_labels_read"] is False
    assert health["automatic_scoring"] is False
    assert health["real_trading_allowed"] is False
