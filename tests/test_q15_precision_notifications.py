from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from q15_upgrade.strategy_bots import runtime
from q15_upgrade.strategy_bots.rules import BOT_PRECISION_13M, STRATEGY_VERSION


class _Telegram:
    enabled = True

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str):
        self.sent.append(text)
        return {
            "ok": True,
            "delivered": True,
            "muted": False,
            "message_id": len(self.sent),
            "error": None,
        }


@pytest.fixture(autouse=True)
def _reset_runtime_state():
    if runtime._ledger is not None:
        runtime._ledger.close()
    runtime._ledger = None
    runtime._telegram = None
    yield
    if runtime._ledger is not None:
        runtime._ledger.close()
    runtime._ledger = None
    runtime._telegram = None


def _row(**over):
    row = {
        "created_at": 18 * 3600,
        "model_version": "interval-research-v1",
        "policy_version": "q15-13m-selective-precision-v2",
        "record_kind": "PRECISION_13M_SIZED_V2",
        "delivery_status": "PAPER_PRECISION_13M_SIZED",
        "asset": "SOL",
        "ticker": "KXSOL15M-PRECISION",
        "interval": "13M",
        "window_key": 77,
        "close_time": 4_102_444_800.0,
        "predicted_side": "YES",
        "entry_ask_cents": 65.0,
        "spread_cents": 3.0,
        "distance_from_strike": 1e-5,
        "probability_13m": 0.70,
        "probability_15m": 0.67,
        "probability_change": 0.03,
        "window_mean_probability": 0.65,
        "yes_breadth": 7,
        "btc_probability": 0.62,
        "side_probability": 0.70,
        "disagreement": 0.05,
        "disagreement_weight": 1.5,
        "spread_weight": 1.5,
        "session_weight": 1.33,
        "stack_weight": 1.5,
        "recommended_size_weight": 2.25,
        "near_strike_quality": True,
    }
    row.update(over)
    return row


def _configure(tmp_path: Path, monkeypatch, telegram: _Telegram) -> None:
    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_STRATEGY_BOTS_DB", str(tmp_path / "strategy.sqlite3"))
    monkeypatch.setenv("Q15_V3_TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("Q15_V3_PRECISION_13M", "true")
    monkeypatch.setenv("Q15_V3_PRECISION_13M_NOTIFY", "true")
    runtime._telegram = telegram


def test_precision_notification_is_independent_and_deduplicated(tmp_path, monkeypatch):
    telegram = _Telegram()
    _configure(tmp_path, monkeypatch, telegram)
    drift = Mock(side_effect=AssertionError("precision must not call Drift"))
    monkeypatch.setattr(runtime, "record_drift_pick_row", drift)

    row_id = runtime.record_precision13_row(_row())
    assert row_id is not None
    assert len(telegram.sent) == 1
    assert "V3 PRECISION 13M" in telegram.sent[0]
    assert "2.25x" in telegram.sent[0]
    assert "existing Drift alerts are unchanged" in telegram.sent[0]
    assert drift.call_count == 0

    recorded = runtime.get_ledger().row_by_id(row_id)
    assert recorded["bot_name"] == BOT_PRECISION_13M
    assert recorded["source_system"] == "precision13_shadow"
    assert recorded["notification_status"] == "SENT"

    assert runtime.resolve(
        source_system="precision13_shadow",
        source_model_version="interval-research-v1",
        ticker="KXSOL15M-PRECISION",
        official_result="YES",
        now=200.0,
    ) == 1
    settled = runtime.get_ledger().row_by_id(row_id)
    assert settled["correct"] == 1
    assert settled["hypothetical_pnl_cents"] == 33.0

    assert runtime.record_precision13_row(_row(entry_ask_cents=66.0)) is None
    assert len(telegram.sent) == 1
    assert drift.call_count == 0


def test_precision_notification_flag_does_not_affect_recording(tmp_path, monkeypatch):
    telegram = _Telegram()
    _configure(tmp_path, monkeypatch, telegram)
    monkeypatch.setenv("Q15_V3_PRECISION_13M_NOTIFY", "false")

    row_id = runtime.record_precision13_row(_row())
    assert row_id is not None
    assert telegram.sent == []
    rows = runtime.get_ledger().rows(STRATEGY_VERSION)
    assert len(rows) == 1
    assert rows[0]["bot_name"] == BOT_PRECISION_13M
    assert rows[0]["notification_status"] is None
