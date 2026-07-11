from __future__ import annotations

from pathlib import Path
import time

import pytest

from notifications.outbox_v9 import ReliableTelegramOutbox
from q15_upgrade.strategy_bots import runtime
from q15_upgrade.strategy_bots.rules import (
    BOT_DRIFT_FLOW_SPREAD,
    BOT_DRIFT_NO_EXPANSION,
    STRATEGY_VERSION,
)
from q15_upgrade.strategy_bots.telegram import V3Telegram


class _Telegram:
    enabled = True
    token = "secret-token"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_with_result(self, text: str):
        self.sent.append(text)
        return {
            "ok": True,
            "delivered": True,
            "muted": False,
            "message_id": len(self.sent),
            "error": None,
        }

    def send(self, text: str):
        return self.send_with_result(text)


class _LegacyMappingFailure:
    enabled = True
    token = "secret-token"
    last_error = None

    def send(self, text: str):
        return {
            "ok": False,
            "delivered": False,
            "muted": False,
            "message_id": None,
            "error": "HTTP 503",
        }


@pytest.fixture(autouse=True)
def _reset_runtime_state():
    runtime.reset_drift_outbox()
    if runtime._ledger is not None:
        runtime._ledger.close()
    runtime._ledger = None
    runtime._telegram = None
    yield
    runtime.reset_drift_outbox()
    if runtime._ledger is not None:
        runtime._ledger.close()
    runtime._ledger = None
    runtime._telegram = None


def _flow_row(**over):
    row = {
        "created_at": 100.0,
        "model_version": "interval-research-v1",
        "record_kind": "DRIFT_PICK_13M",
        "asset": "XRP",
        "ticker": "KXXRP15M-FLOW",
        "interval": "13M",
        "window_key": 77,
        "close_time": 4_102_444_800.0,
        "predicted_side": "YES",
        "entry_ask_cents": 65.0,
        "spread_cents": 3.0,
        "depth_contracts": 100.0,
        "drift_spread_weight": 1.5,
        "drift_session_weight": 1.0,
        "drift_stack_weight": 1.5,
    }
    row.update(over)
    return row


def _no_row(**over):
    row = {
        "created_at": 2000.0,
        "model_version": "interval-research-v1",
        "record_kind": "DRIFT_NO_EXPANSION",
        "rule_code": "DRIFT_NO_EXPANSION_FLOW_SPREAD_V1",
        "reason_codes": "DRIFT_NO_EXPANSION_CANDIDATE,XRP_NO_60_69",
        "delivery_status": "PAPER_DRIFT_NO_EXPANSION",
        "asset": "XRP",
        "ticker": "KXXRP15M-NO-EXP",
        "interval": "13M",
        "window_key": 1200,
        "close_time": 4_102_444_800.0,
        "predicted_side": "NO",
        "entry_ask_cents": 67.0,
        "spread_cents": 3.0,
        "depth_contracts": 100.0,
        "distance_sigma": 1e-5,
        "flip_probability": 20.0,
        "spot_depth_status": "ok",
        "spot_depth_trade_net_notional_60s": -500.0,
    }
    row.update(over)
    return row


def _configure_runtime(tmp_path: Path, monkeypatch, telegram: _Telegram) -> None:
    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_STRATEGY_BOTS_DB", str(tmp_path / "strategy.sqlite3"))
    monkeypatch.setenv("Q15_V3_TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("Q15_V3_DRIFT_FLOW_SPREAD", "true")
    monkeypatch.setenv("Q15_V3_DRIFT_FLOW_SPREAD_NOTIFY", "true")
    monkeypatch.setenv("Q15_V3_DRIFT_NO_EXPANSION", "true")
    monkeypatch.setenv("Q15_V3_DRIFT_NO_EXPANSION_NOTIFY", "true")
    runtime._telegram = telegram
    monkeypatch.setattr(
        runtime,
        "enrich_spot_depth",
        lambda row: dict(
            row,
            spot_depth_status="ok",
            spot_depth_trade_net_notional_60s=(
                -500.0 if str(row.get("predicted_side")) == "NO" else 500.0
            ),
        ),
    )
    monkeypatch.setattr(runtime, "_enrich_source_row", lambda row, **_: dict(row))


def _enable_outbox(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "drift-outbox.sqlite3"
    monkeypatch.setenv("Q15_V3_DRIFT_OUTBOX_ENABLED", "true")
    monkeypatch.setenv("Q15_V3_DRIFT_OUTBOX_DB", str(path))
    monkeypatch.setenv("Q15_V9_OUTBOX_WORKER", "false")
    monkeypatch.setenv("Q15_V9_DISABLE_NETWORK", "true")
    return path


def test_v3_telegram_exposes_rich_send_adapter():
    expected = {
        "ok": False,
        "delivered": False,
        "muted": False,
        "message_id": None,
        "error": "HTTP 503",
    }

    class Client:
        def send(self, text):
            return dict(expected)

    telegram = object.__new__(V3Telegram)
    telegram._client = Client()
    assert telegram.send_with_result("payload") == expected
    assert telegram.send("payload") == expected


def test_outbox_does_not_treat_nonempty_failure_mapping_as_delivered(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("Q15_V9_OUTBOX_WORKER", "false")
    monkeypatch.setenv("Q15_V9_DISABLE_NETWORK", "false")
    outbox = ReliableTelegramOutbox(
        None,
        _LegacyMappingFailure(),
        sqlite_path=str(tmp_path / "legacy.sqlite3"),
    )
    result = outbox.send_with_result("frozen", idempotency_key="legacy:failure")
    assert result["delivered"] is False
    assert outbox.rows()[0]["status"] == "FAILED_RETRYABLE"
    assert outbox.last_error == "HTTP 503"
    outbox.close()


def test_disabled_rule_does_not_burn_drift_row_identity(tmp_path, monkeypatch):
    telegram = _Telegram()
    _configure_runtime(tmp_path, monkeypatch, telegram)
    monkeypatch.setenv("Q15_V3_DRIFT_FLOW_SPREAD_NOTIFY", "false")
    monkeypatch.setenv("Q15_V3_DRIFT_FLOW_SPREAD", "false")

    assert runtime.record_drift_pick_row(_flow_row()) is None
    monkeypatch.setenv("Q15_V3_DRIFT_FLOW_SPREAD", "true")
    assert runtime.record_drift_pick_row(_flow_row()) is not None

    rows = runtime.get_ledger().rows(STRATEGY_VERSION)
    assert [row["bot_name"] for row in rows] == [BOT_DRIFT_FLOW_SPREAD]


def test_drift_outbox_queues_exact_payload_and_retries_once(tmp_path, monkeypatch):
    telegram = _Telegram()
    _configure_runtime(tmp_path, monkeypatch, telegram)
    _enable_outbox(tmp_path, monkeypatch)

    row_id = runtime.record_drift_pick_row(_flow_row())
    assert row_id is not None
    assert telegram.sent == []

    outbox = runtime.get_drift_outbox()
    queued = outbox.rows()
    assert len(queued) == 1
    assert queued[0]["status"] == "PENDING"
    assert queued[0]["idempotency_key"] == (
        f"{STRATEGY_VERSION}:drift:{BOT_DRIFT_FLOW_SPREAD}:row:77:KXXRP15M-FLOW"
    )
    frozen_payload = queued[0]["payload"]
    recorded = runtime.get_ledger().row_by_id(row_id)
    assert recorded["notification_status"] == "QUEUED_RETRY"

    # The retry transmits the exact persisted payload, not a rebuilt live view.
    outbox.network_disabled = False
    assert outbox._attempt(queued[0]["id"]) is True
    assert telegram.sent == [frozen_payload]
    assert outbox.rows()[0]["status"] == "SENT"

    # Replaying the source reconciles the ledger with the worker's terminal
    # outbox status without creating a second message.
    assert runtime.record_drift_pick_row(_flow_row()) is None
    assert runtime.get_ledger().row_by_id(row_id)["notification_status"] == "SENT"

    # DB uniqueness and the deterministic outbox key block duplicate rows/sends.
    assert runtime.record_drift_pick_row(_flow_row(entry_ask_cents=66.0)) is None
    assert len(outbox.rows()) == 1
    assert telegram.sent == [frozen_payload]


def test_no_expansion_group_uses_one_deterministic_outbox_message(
    tmp_path, monkeypatch
):
    telegram = _Telegram()
    _configure_runtime(tmp_path, monkeypatch, telegram)
    _enable_outbox(tmp_path, monkeypatch)

    row_ids = runtime.record_drift_no_expansion_window([
        _no_row(),
        _no_row(
            asset="HYPE",
            ticker="KXHYPE15M-NO-EXP",
            entry_ask_cents=63.0,
            spread_cents=2.0,
        ),
    ])
    assert len(row_ids) == 2
    assert telegram.sent == []

    outbox = runtime.get_drift_outbox()
    queued = outbox.rows()
    assert len(queued) == 1
    assert queued[0]["idempotency_key"] == (
        f"{STRATEGY_VERSION}:drift:{BOT_DRIFT_NO_EXPANSION}:group:1200"
    )
    assert "XRP NO" in queued[0]["payload"]
    assert "HYPE NO" in queued[0]["payload"]
    ledger_rows = runtime.get_ledger().rows(STRATEGY_VERSION)
    assert {row["notification_status"] for row in ledger_rows} == {"QUEUED_RETRY"}
    assert queued[0]["expires_at"] == 4_102_444_800.0


@pytest.mark.parametrize("persisted_status", [None, "DELIVERY_FAILED"])
def test_replayed_flow_row_recovers_committed_notification(
    tmp_path, monkeypatch, persisted_status
):
    telegram = _Telegram()
    _configure_runtime(tmp_path, monkeypatch, telegram)
    _enable_outbox(tmp_path, monkeypatch)

    # Simulate a process dying after the strategy row commit and before enqueue.
    monkeypatch.setenv("Q15_V3_DRIFT_FLOW_SPREAD_NOTIFY", "false")
    row_id = runtime.record_drift_pick_row(_flow_row())
    assert row_id is not None
    if persisted_status is not None:
        runtime.get_ledger().mark_notification(
            row_id,
            status=persisted_status,
            message_id=None,
            error="simulated crash boundary",
        )

    monkeypatch.setenv("Q15_V3_DRIFT_FLOW_SPREAD_NOTIFY", "true")
    # Public duplicate behavior stays unchanged, but delivery is recovered.
    assert runtime.record_drift_pick_row(_flow_row()) is None
    outbox = runtime.get_drift_outbox()
    assert len(outbox.rows()) == 1
    assert outbox.rows()[0]["status"] == "PENDING"
    assert runtime.get_ledger().row_by_id(row_id)["notification_status"] == "QUEUED_RETRY"
    assert runtime.record_drift_pick_row(_flow_row()) is None
    assert len(outbox.rows()) == 1


def test_replayed_group_recovers_rows_without_pre_enqueue_claim(
    tmp_path, monkeypatch
):
    telegram = _Telegram()
    _configure_runtime(tmp_path, monkeypatch, telegram)
    _enable_outbox(tmp_path, monkeypatch)
    rows = [
        _no_row(close_time=4_102_444_700.0),
        _no_row(
            asset="HYPE",
            ticker="KXHYPE15M-NO-EXP",
            entry_ask_cents=63.0,
            spread_cents=2.0,
            close_time=4_102_444_800.0,
        ),
    ]

    monkeypatch.setenv("Q15_V3_DRIFT_NO_EXPANSION_NOTIFY", "false")
    row_ids = runtime.record_drift_no_expansion_window(rows)
    assert len(row_ids) == 2
    assert all(
        runtime.get_ledger().row_by_id(row_id)["notification_status"] is None
        for row_id in row_ids
    )

    # A legacy claim left by a crashed build cannot suppress durable recovery;
    # the deterministic outbox key is the delivery claim now.
    assert runtime.get_ledger().claim_meta_once(
        f"{STRATEGY_VERSION}:{BOT_DRIFT_NO_EXPANSION}:group-notify:1200"
    )
    monkeypatch.setenv("Q15_V3_DRIFT_NO_EXPANSION_NOTIFY", "true")
    assert runtime.record_drift_no_expansion_window(rows) == []
    queued = runtime.get_drift_outbox().rows()
    assert len(queued) == 1
    assert queued[0]["status"] == "PENDING"
    assert queued[0]["expires_at"] == 4_102_444_700.0
    assert "XRP NO" in queued[0]["payload"]
    assert "HYPE NO" in queued[0]["payload"]


def test_enabled_outbox_enqueue_never_calls_notifier_synchronously(
    tmp_path, monkeypatch
):
    telegram = _Telegram()
    _configure_runtime(tmp_path, monkeypatch, telegram)
    _enable_outbox(tmp_path, monkeypatch)
    monkeypatch.setenv("Q15_V9_DISABLE_NETWORK", "false")

    row_id = runtime.record_drift_pick_row(_flow_row())
    assert row_id is not None
    assert telegram.sent == []
    assert runtime.get_drift_outbox().rows()[0]["status"] == "PENDING"


def test_startup_initialization_retries_pending_without_new_alert(
    tmp_path, monkeypatch
):
    telegram = _Telegram()
    _configure_runtime(tmp_path, monkeypatch, telegram)
    path = _enable_outbox(tmp_path, monkeypatch)

    seed = ReliableTelegramOutbox(None, telegram, sqlite_path=str(path))
    result = seed.enqueue_with_result(
        "persisted before restart",
        idempotency_key="startup:pending",
        expires_at=time.time() + 60.0,
    )
    assert result["outbox_status"] == "PENDING"
    seed.close()

    runtime.reset_drift_outbox()
    monkeypatch.setenv("Q15_V9_OUTBOX_WORKER", "true")
    monkeypatch.setenv("Q15_V9_DISABLE_NETWORK", "false")
    assert runtime.initialize_drift_outbox() is True
    deadline = time.monotonic() + 6.0
    while (
        runtime.get_drift_outbox().status_by_key("startup:pending") != "SENT"
        and time.monotonic() < deadline
    ):
        time.sleep(0.05)
    assert telegram.sent == ["persisted before restart"]
    assert runtime.get_drift_outbox().status_by_key("startup:pending") == "SENT"


def test_startup_initialization_respects_strategy_and_muted_gates(
    tmp_path, monkeypatch
):
    telegram = _Telegram()
    _configure_runtime(tmp_path, monkeypatch, telegram)
    path = _enable_outbox(tmp_path, monkeypatch)
    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "false")
    assert runtime.initialize_drift_outbox() is False
    assert not path.exists()

    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    telegram.enabled = False
    assert runtime.initialize_drift_outbox() is False
    assert not path.exists()


def test_outbox_expiry_blocks_stale_send_and_preserves_fresh_retry(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("Q15_V9_OUTBOX_WORKER", "false")
    monkeypatch.setenv("Q15_V9_DISABLE_NETWORK", "false")
    telegram = _Telegram()
    outbox = ReliableTelegramOutbox(
        None, telegram, sqlite_path=str(tmp_path / "expiry.sqlite3")
    )

    expired = outbox.enqueue_with_result(
        "stale", idempotency_key="expiry:stale", expires_at=time.time() - 1.0
    )
    assert expired["outbox_status"] == "EXPIRED"
    assert outbox._attempt(outbox.rows()[0]["id"]) is False
    assert telegram.sent == []

    fresh = outbox.enqueue_with_result(
        "fresh", idempotency_key="expiry:fresh", not_after=time.time() + 60.0
    )
    assert fresh["outbox_status"] == "PENDING"
    fresh_row = next(row for row in outbox.rows() if row["idempotency_key"] == "expiry:fresh")
    assert outbox._attempt(fresh_row["id"]) is True
    assert telegram.sent == ["fresh"]
    assert outbox.status_by_key("expiry:fresh") == "SENT"
    outbox.close()


def test_missing_drift_expiry_records_but_never_enqueues(tmp_path, monkeypatch):
    telegram = _Telegram()
    _configure_runtime(tmp_path, monkeypatch, telegram)
    _enable_outbox(tmp_path, monkeypatch)

    row_id = runtime.record_drift_pick_row(_flow_row(close_time=None))
    assert row_id is not None
    assert runtime.get_drift_outbox().rows() == []
    recorded = runtime.get_ledger().row_by_id(row_id)
    assert recorded["notification_status"] == "DELIVERY_FAILED"
    assert recorded["notification_error"] == "drift_expiry_missing"
    assert telegram.sent == []
