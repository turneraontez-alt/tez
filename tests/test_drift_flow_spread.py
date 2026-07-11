from __future__ import annotations

import sqlite3

from q15_upgrade.strategy_bots import runtime
from q15_upgrade.strategy_bots.rules import (
    ACCEPTED,
    BOT_DRIFT_13M,
    BOT_DRIFT_FLOW_SPREAD,
    REJECTED,
    RESEARCH_ONLY,
    STRATEGY_VERSION,
    drift_flow_spread_13m_decision,
)
from q15_upgrade.strategy_bots.spot_depth import enrich_spot_depth


def _row(**over):
    base = {
        "created_at": 100.0,
        "model_version": "interval-research-v1",
        "record_kind": "DRIFT_PICK_13M",
        "asset": "XRP",
        "ticker": "KXXRP15M-FLOW",
        "interval": "13M",
        "window_key": 77,
        "close_time": 900.0,
        "predicted_side": "YES",
        "entry_ask_cents": 65.0,
        "spread_cents": 3.0,
        "depth_contracts": 100.0,
        "drift_spread_weight": 1.5,
        "drift_session_weight": 1.0,
        "drift_stack_weight": 1.5,
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


def test_spot_enrichment_is_point_in_time_and_corrects_old_coinbase_side(
    tmp_path, monkeypatch
):
    db = tmp_path / "spot.sqlite3"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE spot_depth_snapshots ("
            "created_at REAL, asset TEXT, provider TEXT, source TEXT, "
            "book_age_seconds REAL, trade_age_seconds REAL, trade_side_semantics TEXT, "
            "best_bid REAL, best_ask REAL, mid REAL, spread_bps REAL, "
            "trade_buy_qty_5s REAL, trade_sell_qty_5s REAL, "
            "trade_buy_notional_5s REAL, trade_sell_notional_5s REAL, "
            "trade_buy_qty_15s REAL, trade_sell_qty_15s REAL, "
            "trade_buy_notional_15s REAL, trade_sell_notional_15s REAL, "
            "trade_buy_qty_60s REAL, trade_sell_qty_60s REAL, "
            "trade_buy_notional_60s REAL, trade_sell_notional_60s REAL, "
            "last_trade_side TEXT)"
        )
        values = (
            90.0, "XRP", "coinbase", "coinbase XRP-USD", 3.0, 4.0, None,
            1.0, 1.01, 1.005, 1.0,
            1.0, 2.0, 10.0, 20.0,
            3.0, 4.0, 30.0, 40.0,
            5.0, 8.0, 100.0, 400.0, "buy",
        )
        conn.execute(
            "INSERT INTO spot_depth_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
        # This later snapshot must not leak backward into the t=100 decision.
        conn.execute(
            "INSERT INTO spot_depth_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (101.0, *values[1:]),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("Q15_SPOT_DEPTH_DB", str(db))
    out = enrich_spot_depth(_row())

    assert out["spot_depth_status"] == "ok"
    assert out["spot_depth_snapshot_age_seconds"] == 10.0
    assert out["spot_depth_trade_net_notional_60s"] == 300.0
    assert out["spot_depth_trade_buy_notional_60s"] == 400.0
    assert out["spot_depth_trade_sell_notional_60s"] == 100.0
    assert out["spot_depth_last_trade_side"] == "sell"


def test_drift_flow_spread_decision_paths(monkeypatch):
    monkeypatch.setenv("Q15_V3_DRIFT_FLOW_SPREAD", "true")

    flow = drift_flow_spread_13m_decision(
        _row(
            spread_cents=5.0,
            spot_depth_status="ok",
            spot_depth_trade_net_notional_60s=1.0,
        )
    )
    assert flow is not None and flow.decision_status == ACCEPTED
    assert flow.bot_name == BOT_DRIFT_FLOW_SPREAD
    assert flow.threshold_profile["gate_path"] == "FLOW_60S_POSITIVE"

    spread = drift_flow_spread_13m_decision(
        _row(
            spread_cents=2.0,
            spot_depth_status="ok",
            spot_depth_trade_net_notional_60s=-500.0,
        )
    )
    assert spread is not None and spread.decision_status == ACCEPTED
    assert spread.threshold_profile["gate_path"] == "SPREAD_LTE_2"

    rejected = drift_flow_spread_13m_decision(
        _row(
            spread_cents=3.0,
            spot_depth_status="ok",
            spot_depth_trade_net_notional_60s=0.0,
        )
    )
    assert rejected is not None and rejected.decision_status == REJECTED
    assert "DRIFT_SPOT_FLOW_60S_NONPOSITIVE" in rejected.reason_codes

    inconclusive = drift_flow_spread_13m_decision(
        _row(
            spread_cents=3.0,
            spot_depth_status="stale",
            spot_depth_trade_net_notional_60s=100.0,
        )
    )
    assert inconclusive is not None and inconclusive.decision_status == RESEARCH_ONLY
    assert "DRIFT_SPOT_FLOW_MISSING_OR_STALE" in inconclusive.reason_codes


def test_runtime_records_every_gate_path_but_notifies_only_accepted(
    tmp_path, monkeypatch
):
    telegram = _Telegram()
    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_STRATEGY_BOTS_DB", str(tmp_path / "v3.sqlite3"))
    monkeypatch.setenv("Q15_V3_TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("Q15_V3_DRIFT_FLOW_SPREAD", "true")
    monkeypatch.setenv("Q15_V3_DRIFT_FLOW_SPREAD_NOTIFY", "true")
    runtime._ledger = None
    runtime._telegram = telegram
    monkeypatch.setattr(runtime, "_enrich_source_row", lambda row, **_: dict(row))
    monkeypatch.setattr(runtime, "enrich_spot_depth", lambda row: dict(row))

    rows = [
        _row(
            ticker="FLOW", window_key=1, spot_depth_status="ok",
            spot_depth_trade_net_notional_60s=100.0, spread_cents=5.0,
        ),
        _row(
            ticker="SPREAD", window_key=2, spot_depth_status="ok",
            spot_depth_trade_net_notional_60s=-100.0, spread_cents=2.0,
        ),
        _row(
            ticker="REJECT", window_key=3, spot_depth_status="ok",
            spot_depth_trade_net_notional_60s=-100.0, spread_cents=3.0,
        ),
        _row(
            ticker="STALE", window_key=4, spot_depth_status="stale",
            spot_depth_trade_net_notional_60s=100.0, spread_cents=3.0,
        ),
    ]
    ids = [runtime.record_drift_pick_row(row) for row in rows]
    assert all(row_id is not None for row_id in ids)
    assert len(telegram.sent) == 2
    assert "DRIFT FLOW CONFIRMED 13M" in telegram.sent[0]
    assert "Confirmed: 60s spot flow" in telegram.sent[0]
    assert "Confirmed: Kalshi spread 2c" in telegram.sent[1]

    ledger = runtime.get_ledger()
    recorded = ledger.rows(STRATEGY_VERSION)
    confirmed = [r for r in recorded if r["bot_name"] == BOT_DRIFT_FLOW_SPREAD]
    assert [r["decision_status"] for r in confirmed] == [
        ACCEPTED, ACCEPTED, REJECTED, RESEARCH_ONLY,
    ]
    assert not [r for r in recorded if r["bot_name"] == BOT_DRIFT_13M]
    assert confirmed[0]["spot_depth_trade_net_notional_60s"] == 100.0

    scoreboard = ledger.scoreboard(STRATEGY_VERSION, min_n=1)
    assert scoreboard["drift_system"]["flow_spread_13m"]["rows"] == 4
    assert scoreboard["drift_system"]["raw_13m_legacy_shadow"]["rows"] == 0
