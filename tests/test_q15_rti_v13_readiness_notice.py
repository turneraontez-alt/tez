from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time

import pytest

from notifications.outbox_v9 import ReliableTelegramOutbox
from q15_upgrade.v13_readiness_monitor import (
    CAPTURE_PROTECTED_AFTER_SECONDS,
    CAPTURE_PROTECTED_BEFORE_SECONDS,
    V13ReadinessMonitor,
    capture_protection_delay_seconds,
)
from tools import q15_rti_microstructure_freeze as freeze
from tools import q15_rti_v13_readiness_notice as notice


def _snapshot(windows: int = 30, **overrides):
    snapshot = {
        "notice_version": notice.NOTICE_VERSION,
        "design_id": notice.v13.DESIGN_ID,
        "design_sha256": notice.v13.DESIGN_SHA256,
        "complete_executable_close_windows": windows,
        "coverage_clean": True,
        "timestamp_alignment_failures": 0,
        "soft_input_integrity": {
            "status": "ALL_RETAINED_INPUTS_OBSERVED",
            "fully_observed_rows": windows * 7,
            "soft_degraded_rows": 0,
            "fully_observed_close_windows": windows,
            "soft_degraded_close_windows": 0,
            "degraded_by_asset": {},
            "degraded_by_reason": {},
            "changes_readiness_credit": False,
        },
        "cohort_readiness": {
            "NON_BTC_TRANSFER": {
                "minimum_complete_close_windows": 60,
                "windows_remaining": max(0, 60 - windows),
                "ready_for_locked_freeze": windows >= 60,
            },
            "BTC": {
                "minimum_complete_close_windows": 150,
                "windows_remaining": max(0, 150 - windows),
                "ready_for_locked_freeze": windows >= 150,
            },
        },
        "geometry_review_protocol_sha256": (
            notice.v13.GEOMETRY_REVIEW_PROTOCOL_SHA256
        ),
        "covariate_drift_protocol_sha256": (
            notice.v13.COVARIATE_DRIFT_PROTOCOL_SHA256
        ),
        "walk_forward_protocol_sha256": (
            notice.v13.EVALUATION_PROTOCOL_SHA256
        ),
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "artifact_emitted": False,
        "automatic_scoring": False,
        "automatic_promotion": False,
        "notification_is_trade_signal": False,
        "real_trading_allowed": False,
    }
    snapshot.update(overrides)
    return snapshot


def test_v13_notice_projection_cannot_include_outcomes():
    assert freeze.OUTCOME_COLUMNS.isdisjoint(freeze.FEATURE_SELECT_COLUMNS)


def test_v13_snapshot_builder_uses_only_feature_loader(monkeypatch, tmp_path):
    loaded = []

    def fake_load(path: Path):
        loaded.append(path)
        return [{"id": 1, "asset": "ETH"}]

    class Runtime:
        FEATURE_NAMES = ()

        @staticmethod
        def model_feature_window_coverage(rows):
            assert rows == [{"id": 1, "asset": "ETH"}]
            return {
                "model_feature_timestamp_failures": [],
                "model_feature_complete_close_times": [],
            }

    monkeypatch.setattr(notice, "load_feature_rows", fake_load)
    monkeypatch.setattr(
        notice, "build_report", lambda rows, source_schema: {"sentinel": True}
    )
    monkeypatch.setattr(notice, "_feature_runtime", lambda design: Runtime())
    monkeypatch.setattr(
        notice,
        "build_readiness",
        lambda design, coverage: {
            "complete_microstructure_close_windows": 60,
            "coverage_clean": True,
            "model_feature_timestamp_failures": 0,
            "cohorts": {
                "NON_BTC_TRANSFER": {
                    "minimum_complete_close_windows": 60,
                    "windows_remaining": 0,
                    "ready_for_locked_freeze": True,
                },
                "BTC": {
                    "minimum_complete_close_windows": 150,
                    "windows_remaining": 90,
                    "ready_for_locked_freeze": False,
                },
            },
        },
    )
    db = tmp_path / "features.sqlite3"
    snapshot = notice.build_outcome_blind_snapshot(database_path=db)
    assert loaded == [db]
    assert notice.ready_milestones(snapshot) == ["GEOMETRY_30", "NON_BTC_60"]
    assert snapshot["outcome_columns_selected"] is False
    assert snapshot["outcome_labels_read"] is False
    assert snapshot["model_fit_performed"] is False
    assert snapshot["artifact_emitted"] is False
    assert snapshot["soft_input_integrity"]["rows"] == 0
    assert snapshot["soft_input_integrity"]["outcome_labels_read"] is False


@pytest.mark.parametrize(
    ("windows", "expected"),
    [
        (29, []),
        (30, ["GEOMETRY_30"]),
        (59, ["GEOMETRY_30"]),
        (60, ["GEOMETRY_30", "NON_BTC_60"]),
        (149, ["GEOMETRY_30", "NON_BTC_60"]),
        (150, ["GEOMETRY_30", "NON_BTC_60", "BTC_150"]),
    ],
)
def test_only_fixed_clean_milestones_become_ready(windows, expected):
    assert notice.ready_milestones(_snapshot(windows)) == expected


@pytest.mark.parametrize(
    ("override", "value"),
    [
        ("coverage_clean", False),
        ("timestamp_alignment_failures", 1),
        ("outcome_columns_selected", True),
        ("outcome_labels_read", True),
        ("model_fit_performed", True),
        ("artifact_emitted", True),
        ("automatic_scoring", True),
        ("automatic_promotion", True),
        ("notification_is_trade_signal", True),
        ("real_trading_allowed", True),
        ("design_sha256", "wrong"),
        ("geometry_review_protocol_sha256", "wrong"),
        ("covariate_drift_protocol_sha256", "wrong"),
        ("walk_forward_protocol_sha256", "wrong"),
    ],
)
def test_any_unsafe_evidence_blocks_every_notice(override, value):
    calls = []
    result = notice.send_ready_milestones(
        _snapshot(150, **{override: value}),
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    assert result["status"] == "WAITING_FOR_MILESTONE"
    assert result["notice_attempted"] is False
    assert calls == []


def test_messages_are_versioned_paper_admin_and_not_trade_signals():
    snapshot = _snapshot(150)
    for milestone in notice.MILESTONES:
        message = notice.readiness_message(snapshot, milestone)
        assert "V13" in message
        assert "PAPER ADMIN" in message
        assert "SEALED / unread" in message
        assert "DISABLED" in message
        assert "Fully observed windows" in message
        assert "not a trade signal" in message


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


def test_repeated_milestone_checks_are_idempotent_per_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_V9_OUTBOX_WORKER", "false")
    monkeypatch.setenv("Q15_V9_DISABLE_NETWORK", "false")
    raw = _RawTelegram()
    outbox = ReliableTelegramOutbox(
        _DisabledStore(), raw, sqlite_path=str(tmp_path / "outbox.sqlite3")
    )
    try:
        now = time.time()
        first = notice.send_ready_milestones(
            _snapshot(60), outbox.send_with_result, now=now
        )
        second = notice.send_ready_milestones(
            _snapshot(60), outbox.send_with_result, now=now + 1.0
        )
        assert first["ready_milestones"] == ["GEOMETRY_30", "NON_BTC_60"]
        assert second["ready_milestones"] == ["GEOMETRY_30", "NON_BTC_60"]
        assert len(raw.sent) == 2
        assert len(outbox.rows()) == 2
        assert {row["status"] for row in outbox.rows()} == {"SENT"}
    finally:
        outbox.close()


def test_monitor_waits_without_constructing_sender_and_tracks_partial_completion():
    factory_calls = []
    waiting = V13ReadinessMonitor(
        enabled=True,
        snapshot_builder=lambda: _snapshot(29),
        sender_factory=lambda: factory_calls.append(True),
    )
    result = waiting.check_once()
    assert result["status"] == "WAITING_FOR_MILESTONE"
    assert factory_calls == []
    assert waiting.health()["outcome_labels_read"] is False

    sent = []

    def factory():
        factory_calls.append(True)

        def sender(text, **kwargs):
            sent.append((text, kwargs))
            return {"ok": True, "delivered": True, "muted": False}

        return sender

    ready = V13ReadinessMonitor(
        enabled=True,
        snapshot_builder=lambda: _snapshot(60),
        sender_factory=factory,
    )
    ready.check_once()
    health = ready.health()
    assert len(sent) == 2
    assert health["completed_milestones"] == ["GEOMETRY_30", "NON_BTC_60"]
    assert health["pending_milestones"] == ["BTC_150"]
    assert health["all_milestones_completed"] is False
    assert health["notification_is_trade_signal"] is False
    assert health["automatic_scoring"] is False
    assert health["real_trading_allowed"] is False
    assert health["soft_input_integrity_status"] == (
        "ALL_RETAINED_INPUTS_OBSERVED"
    )
    assert health["soft_degraded_rows"] == 0
    assert health["soft_degradation_changes_readiness_credit"] is False


def test_readiness_scans_defer_outside_exact_capture_history():
    assert CAPTURE_PROTECTED_BEFORE_SECONDS == 130
    assert CAPTURE_PROTECTED_AFTER_SECONDS == 5
    # Exact capture is phase 120 modulo 900.  Phase 890 is 130 seconds before.
    assert capture_protection_delay_seconds(890.0) == 136.0
    assert capture_protection_delay_seconds(889.0) == 0.0
    assert capture_protection_delay_seconds(120.0) == 6.0
    assert capture_protection_delay_seconds(125.0) == 1.0
    assert capture_protection_delay_seconds(126.0) == 0.0
    monitor = V13ReadinessMonitor(
        enabled=True, snapshot_builder=lambda: _snapshot(29),
    )
    health = monitor.health()
    assert health["capture_protection_enabled"] is True
    assert health["capture_protected_before_seconds"] == 130
    assert health["capture_protected_after_seconds"] == 5
    assert health["capture_deferrals"] == 0
