from __future__ import annotations

from pathlib import Path

import pytest

from q15_upgrade.v14_readiness_monitor import V14ReadinessMonitor
from tools import q15_rti_microstructure_freeze as freeze
from tools import q15_rti_v14_readiness_notice as notice


def _snapshot(windows: int = 30, **overrides):
    snapshot = {
        "notice_version": notice.NOTICE_VERSION,
        "design_id": notice.v14.DESIGN_ID,
        "design_sha256": notice.v14.DESIGN_SHA256,
        "evaluation_protocol_sha256": notice.v14.EVALUATION_PROTOCOL_SHA256,
        "reporting_protocol_sha256": notice.v14.REPORTING_PROTOCOL_SHA256,
        "calibration_reporting_protocol_sha256": (
            notice.v14.CALIBRATION_REPORTING_PROTOCOL_SHA256
        ),
        "selective_value_curve_protocol_sha256": (
            notice.v14.SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256
        ),
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


def test_v14_notice_projection_cannot_include_outcomes():
    assert freeze.OUTCOME_COLUMNS.isdisjoint(freeze.FEATURE_SELECT_COLUMNS)


def test_v14_snapshot_builder_uses_only_feature_loader(monkeypatch, tmp_path):
    loaded = []

    def fake_load(path: Path):
        loaded.append(path)
        return []

    monkeypatch.setattr(notice, "load_feature_rows", fake_load)
    monkeypatch.setattr(
        notice, "build_report", lambda rows, source_schema: {"sentinel": True}
    )
    monkeypatch.setattr(
        notice.v14,
        "model_feature_window_coverage",
        lambda rows: {
            "model_feature_timestamp_failures": [],
            "model_feature_complete_close_times": [],
        },
    )
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
    assert snapshot["outcome_labels_read"] is False
    assert snapshot["model_fit_performed"] is False
    assert snapshot["artifact_emitted"] is False


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
        ("outcome_labels_read", True),
        ("model_fit_performed", True),
        ("artifact_emitted", True),
        ("automatic_scoring", True),
        ("automatic_promotion", True),
        ("notification_is_trade_signal", True),
        ("real_trading_allowed", True),
        ("design_sha256", "wrong"),
        ("evaluation_protocol_sha256", "wrong"),
        ("reporting_protocol_sha256", "wrong"),
        ("calibration_reporting_protocol_sha256", "wrong"),
        ("selective_value_curve_protocol_sha256", "wrong"),
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
        assert "V14" in message
        assert "PAPER ADMIN" in message
        assert "SEALED / unread" in message
        assert "DISABLED" in message
        assert "not a trade signal" in message


def test_monitor_waits_without_sender_then_tracks_ready_gates():
    factory_calls = []
    waiting = V14ReadinessMonitor(
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

    ready = V14ReadinessMonitor(
        enabled=True,
        snapshot_builder=lambda: _snapshot(60),
        sender_factory=factory,
    )
    ready.check_once()
    health = ready.health()
    assert len(sent) == 2
    assert health["completed_milestones"] == ["GEOMETRY_30", "NON_BTC_60"]
    assert health["pending_milestones"] == ["BTC_150"]
    assert health["notification_is_trade_signal"] is False
    assert health["outcome_labels_read"] is False
    assert health["automatic_scoring"] is False
    assert health["real_trading_allowed"] is False
