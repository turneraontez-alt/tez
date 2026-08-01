from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time

import pytest

from notifications.outbox_v9 import ReliableTelegramOutbox
from q15_upgrade.v11_readiness_monitor import V11ReadinessMonitor
from tools import q15_rti_microstructure_freeze as freeze
from tools import q15_rti_v11_readiness_notice as notice


def _ready_snapshot(**overrides):
    snapshot = {
        "notice_version": notice.NOTICE_VERSION,
        "design_id": notice.v11.DESIGN_ID,
        "design_sha256": notice.v11.DESIGN_SHA256,
        "cohort": notice.COHORT,
        "complete_executable_close_windows": 60,
        "minimum_complete_close_windows": 60,
        "windows_remaining": 0,
        "ready_for_locked_freeze": True,
        "coverage_clean": True,
        "timestamp_alignment_failures": 0,
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "artifact_emitted": False,
        "automatic_scoring": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }
    snapshot.update(overrides)
    return snapshot


def test_feature_projection_cannot_include_outcomes():
    assert freeze.OUTCOME_COLUMNS.isdisjoint(freeze.FEATURE_SELECT_COLUMNS)


def test_snapshot_builder_uses_feature_only_loader(monkeypatch, tmp_path):
    loaded = []

    def fake_load(path: Path):
        loaded.append(path)
        return [{"id": 1, "asset": "ETH"}]

    class Runtime:
        @staticmethod
        def model_feature_window_coverage(rows):
            assert rows == [{"id": 1, "asset": "ETH"}]
            return {"model_feature_timestamp_failures": 0}

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
                notice.COHORT: {
                    "minimum_complete_close_windows": 60,
                    "windows_remaining": 0,
                    "ready_for_locked_freeze": True,
                }
            },
        },
    )
    db = tmp_path / "features.sqlite3"
    snapshot = notice.build_outcome_blind_snapshot(database_path=db)

    assert loaded == [db]
    assert notice.snapshot_is_notice_ready(snapshot)
    assert snapshot["outcome_columns_selected"] is False
    assert snapshot["outcome_labels_read"] is False
    assert snapshot["model_fit_performed"] is False
    assert snapshot["artifact_emitted"] is False


@pytest.mark.parametrize(
    ("override", "value"),
    [
        ("complete_executable_close_windows", 59),
        ("minimum_complete_close_windows", 59),
        ("windows_remaining", 1),
        ("ready_for_locked_freeze", False),
        ("coverage_clean", False),
        ("timestamp_alignment_failures", 1),
        ("outcome_columns_selected", True),
        ("outcome_labels_read", True),
        ("model_fit_performed", True),
        ("artifact_emitted", True),
        ("automatic_scoring", True),
        ("automatic_promotion", True),
        ("real_trading_allowed", True),
        ("design_sha256", "wrong"),
        ("cohort", "BTC"),
    ],
)
def test_partial_or_unsafe_snapshot_never_sends(override, value):
    calls = []

    def sender(*args, **kwargs):
        calls.append((args, kwargs))
        return {"ok": True, "delivered": True}

    result = notice.send_notice_if_ready(
        _ready_snapshot(**{override: value}), sender, now=1000.0
    )
    assert result["status"] == "WAITING_FOR_COMPLETE_WINDOWS"
    assert result["notice_attempted"] is False
    assert calls == []


def test_ready_notice_is_explicitly_paper_administrative_and_sealed():
    sent = []

    def sender(text, **kwargs):
        sent.append((text, kwargs))
        return {"ok": True, "delivered": True, "message_id": 11}

    result = notice.send_notice_if_ready(_ready_snapshot(), sender, now=1000.0)
    assert result["status"] == "READY_NOTICE_ACCEPTED"
    assert result["notice_attempted"] is True
    text, kwargs = sent[0]
    assert "PAPER ADMIN" in text
    assert "SEALED / unread" in text
    assert "manual one-shot" in text
    assert "not a trade signal" in text
    assert kwargs["idempotency_key"] == notice.IDEMPOTENCY_KEY
    assert kwargs["expires_at"] == 1000.0 + 30.0 * 86400.0


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
            "message_id": 123,
        }


def test_repeated_ready_checks_deliver_only_once(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_V9_OUTBOX_WORKER", "false")
    monkeypatch.setenv("Q15_V9_DISABLE_NETWORK", "false")
    raw = _RawTelegram()
    outbox = ReliableTelegramOutbox(
        _DisabledStore(), raw, sqlite_path=str(tmp_path / "outbox.sqlite3")
    )
    try:
        now = time.time()
        first = notice.send_notice_if_ready(
            _ready_snapshot(), outbox.send_with_result, now=now
        )
        second = notice.send_notice_if_ready(
            _ready_snapshot(), outbox.send_with_result, now=now + 1.0
        )
        assert first["status"] == "READY_NOTICE_ACCEPTED"
        assert second["status"] == "READY_NOTICE_ACCEPTED"
        assert len(raw.sent) == 1
        assert len(outbox.rows()) == 1
        assert outbox.rows()[0]["status"] == "SENT"
    finally:
        outbox.close()


def test_background_monitor_does_not_construct_sender_while_waiting():
    factory_calls = []
    monitor = V11ReadinessMonitor(
        enabled=True,
        snapshot_builder=lambda: _ready_snapshot(
            complete_executable_close_windows=59,
            windows_remaining=1,
            ready_for_locked_freeze=False,
        ),
        sender_factory=lambda: factory_calls.append(True),
    )

    result = monitor.check_once()
    health = monitor.health()
    assert result["status"] == "WAITING_FOR_COMPLETE_WINDOWS"
    assert factory_calls == []
    assert health["checks"] == 1
    assert health["windows_remaining"] == 1
    assert health["outcome_labels_read"] is False
    assert health["automatic_scoring"] is False
    assert health["real_trading_allowed"] is False


def test_background_monitor_reuses_one_sender_and_marks_delivery_complete():
    factory_calls = []
    sent = []

    def factory():
        factory_calls.append(True)

        def sender(text, **kwargs):
            sent.append((text, kwargs))
            return {
                "ok": True,
                "delivered": True,
                "muted": False,
                "message_id": 99,
            }

        return sender

    monitor = V11ReadinessMonitor(
        enabled=True,
        snapshot_builder=_ready_snapshot,
        sender_factory=factory,
    )
    first = monitor.check_once()
    second = monitor.check_once()

    assert first["status"] == "READY_NOTICE_ACCEPTED"
    assert second["status"] == "READY_NOTICE_ACCEPTED"
    assert len(factory_calls) == 1
    assert len(sent) == 2  # Outbox-level key, not this injected sender, deduplicates.
    assert monitor.health()["completed"] is True
