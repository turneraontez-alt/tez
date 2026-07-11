from __future__ import annotations

from q15_upgrade.strategy_bots import runtime
from q15_upgrade.strategy_bots.rules import (
    ACCEPTED,
    BOT_DRIFT_NO_EXPANSION,
    REJECTED,
    RESEARCH_ONLY,
    STRATEGY_VERSION,
    drift_no_expansion_decision,
)


def _row(**over):
    base = {
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
        "close_time": 2780.0,
        "predicted_side": "NO",
        "entry_ask_cents": 67.0,
        "spread_cents": 3.0,
        "depth_contracts": 100.0,
        "distance_sigma": 1e-5,
        "flip_probability": 20.0,
        "spot_depth_status": "ok",
        "spot_depth_trade_net_notional_60s": -500.0,
    }
    base.update(over)
    return base


class _Telegram:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, message: str):
        self.sent.append(message)
        return {
            "delivered": True,
            "muted": False,
            "message_id": len(self.sent),
            "error": None,
        }


def test_no_expansion_decision_paths_and_asset_bands(monkeypatch):
    monkeypatch.setenv("Q15_V3_DRIFT_NO_EXPANSION", "true")

    flow = drift_no_expansion_decision(_row())
    assert flow is not None and flow.decision_status == ACCEPTED
    assert flow.bot_name == BOT_DRIFT_NO_EXPANSION
    assert flow.threshold_profile["gate_path"] == "FLOW_60S_NEGATIVE"

    spread = drift_no_expansion_decision(_row(
        asset="HYPE",
        entry_ask_cents=63.0,
        spread_cents=2.0,
        spot_depth_trade_net_notional_60s=100.0,
    ))
    assert spread is not None and spread.decision_status == ACCEPTED
    assert spread.threshold_profile["gate_path"] == "SPREAD_LTE_2"

    rejected = drift_no_expansion_decision(_row(
        asset="DOGE",
        entry_ask_cents=67.0,
        spread_cents=3.0,
        spot_depth_trade_net_notional_60s=100.0,
    ))
    assert rejected is not None and rejected.decision_status == REJECTED

    stale = drift_no_expansion_decision(_row(
        spread_cents=3.0,
        spot_depth_status="stale",
    ))
    assert stale is not None and stale.decision_status == RESEARCH_ONLY

    assert drift_no_expansion_decision(_row(asset="SOL")) is None
    assert drift_no_expansion_decision(_row(asset="HYPE", entry_ask_cents=65.0)) is None
    assert drift_no_expansion_decision(_row(asset="DOGE", entry_ask_cents=64.0)) is None
    assert drift_no_expansion_decision(_row(distance_sigma=4e-5)) is None
    assert drift_no_expansion_decision(_row(flip_probability=31.0)) is None


def test_no_expansion_runtime_groups_only_accepted_rows(tmp_path, monkeypatch):
    telegram = _Telegram()
    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_STRATEGY_BOTS_DB", str(tmp_path / "v3.sqlite3"))
    monkeypatch.setenv("Q15_V3_TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("Q15_V3_DRIFT_NO_EXPANSION", "true")
    monkeypatch.setenv("Q15_V3_DRIFT_NO_EXPANSION_NOTIFY", "true")
    runtime._ledger = None
    runtime._telegram = telegram
    monkeypatch.setattr(runtime, "enrich_spot_depth", lambda row: dict(row))
    monkeypatch.setattr(runtime, "_enrich_source_row", lambda row, **_: dict(row))

    rows = [
        _row(ticker="XRP-FLOW"),
        _row(
            asset="HYPE", ticker="HYPE-SPREAD", entry_ask_cents=63.0,
            spread_cents=2.0, spot_depth_trade_net_notional_60s=100.0,
        ),
        _row(
            asset="DOGE", ticker="DOGE-REJECT", entry_ask_cents=67.0,
            spread_cents=3.0, spot_depth_trade_net_notional_60s=100.0,
        ),
        _row(
            ticker="XRP-STALE", spread_cents=3.0, spot_depth_status="stale",
        ),
    ]
    row_ids = runtime.record_drift_no_expansion_window(rows)
    assert len(row_ids) == 4
    assert runtime.record_drift_no_expansion_window(rows) == []
    assert len(telegram.sent) == 1
    text = telegram.sent[0]
    assert "DRIFT NO EXPANSION | PAPER" in text
    assert "XRP NO @ 67c" in text
    assert "HYPE NO @ 63c" in text
    assert "DOGE-REJECT" not in text
    assert "XRP-STALE" not in text
    assert "negative 60s spot flow" in text
    assert "&lt;=2c fallback" in text

    ledger = runtime.get_ledger()
    recorded = [
        row for row in ledger.rows(STRATEGY_VERSION)
        if row["bot_name"] == BOT_DRIFT_NO_EXPANSION
    ]
    assert [row["decision_status"] for row in recorded] == [
        ACCEPTED, ACCEPTED, REJECTED, RESEARCH_ONLY,
    ]
    assert [row["notification_status"] for row in recorded] == [
        "SENT", "SENT", None, None,
    ]
    scoreboard = ledger.scoreboard(STRATEGY_VERSION, min_n=1)["drift_system"]
    assert scoreboard["no_expansion"]["rows"] == 4
    assert scoreboard["no_expansion_accepted"]["rows"] == 2
    assert scoreboard["no_expansion_by_status"]["ACCEPTED"]["rows"] == 2
