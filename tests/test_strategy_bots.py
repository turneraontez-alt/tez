from __future__ import annotations

import math

from q15_upgrade.strategy_bots import runtime
from q15_upgrade.strategy_bots.ledger import StrategyBotLedger, net_pnl_cents
from q15_upgrade.strategy_bots.rules import (
    ACCEPTED,
    BOT_BNB_NO,
    BOT_HYPE_YES,
    BOT_MOREFIRE_BTC,
    REJECTED,
    RESEARCH_ONLY,
    STRATEGY_VERSION,
    bnb_no_confirmation_decision,
    hype_yes_confirmation_decision,
    morefire_btc_confirmed_decision,
)
from q15_upgrade.strategy_bots.telegram import V3Telegram


def _row(**over):
    base = {
        "created_at": 1000.0,
        "model_version": "ultoim-v2",
        "asset": "BNB",
        "ticker": "KXBNB-1",
        "interval": "10M",
        "window_key": 10,
        "close_time": 1500.0,
        "predicted_side": "NO",
        "entry_ask_cents": 60.0,
        "spread_cents": 2.0,
        "depth_contracts": 900.0,
        "yes_ask_depth_contracts": 200.0,
        "no_ask_depth_contracts": 400.0,
        "delivery_status": "MUTED",
        "record_kind": "DELIVERED_CANDIDATE",
        "reason_codes": "TEST",
    }
    base.update(over)
    return base


def test_bnb_no_rejects_tiny_negative_imbalance():
    d = bnb_no_confirmation_decision(_row(
        spot_depth_imbalance=-0.002,
        spot_depth_trade_sell_notional_15s=0.0,
    ))

    assert d is not None
    assert d.bot_name == BOT_BNB_NO
    assert d.decision_status == REJECTED
    assert "TINY_NEGATIVE_IMBALANCE" in d.reason_codes


def test_bnb_no_accepts_real_sell_pressure_or_real_negative_imbalance():
    by_sell = bnb_no_confirmation_decision(_row(
        spot_depth_imbalance=0.01,
        spot_depth_trade_sell_notional_15s=41.0,
    ))
    by_book = bnb_no_confirmation_decision(_row(
        spot_depth_imbalance=-0.021,
        spot_depth_trade_sell_notional_15s=0.0,
    ))

    assert by_sell is not None and by_sell.decision_status == ACCEPTED
    assert "SELL_NOTIONAL_15S_GE_40" in by_sell.reason_codes
    assert by_book is not None and by_book.decision_status == ACCEPTED
    assert "SPOT_IMBALANCE_LE_NEG_0_02" in by_book.reason_codes


def test_hype_yes_requires_spot_and_kalshi_confirmation_with_missing_taker():
    weak = hype_yes_confirmation_decision(_row(
        asset="HYPE",
        ticker="KXHYPE-1",
        predicted_side="YES",
        spot_depth_imbalance=0.02,
        spot_depth_trade_net_qty_60s=10.0,
        yes_ask_depth_contracts=300.0,
        kalshi_taker_net_yes_volume_15s=None,
    ))
    strong = hype_yes_confirmation_decision(_row(
        asset="HYPE",
        ticker="KXHYPE-2",
        predicted_side="YES",
        spot_depth_imbalance=0.02,
        spot_depth_trade_net_qty_60s=40.0,
        yes_ask_depth_contracts=300.0,
        kalshi_taker_net_yes_volume_15s=None,
    ))

    assert weak is not None and weak.decision_status == REJECTED
    assert "TAKER_MISSING_WITH_WEAK_CONFIRM" in weak.reason_codes
    assert strong is not None and strong.decision_status == ACCEPTED
    assert "TAKER_FLOW_MISSING_STRONGER_CONFIRM_REQUIRED" in strong.reason_codes


def test_morefire_btc_confirmed_accepts_only_with_btc_support():
    row = _row(
        asset="SOL",
        ticker="KXSOL-1",
        predicted_side=None,
        predicted_outcome="YES",
        rule_code="HVF_MORE_FIRE_STRICT",
        record_kind="MORE_FIRE_STRICT_ALERT",
    )
    accepted = morefire_btc_confirmed_decision(row, {
        "ticker": "KXBTC-1",
        "depth_contracts": 1300.0,
        "yes_mid_cents": 56.0,
        "no_mid_cents": 44.0,
        "dominant_side": "YES",
        "predicted_side": "YES",
        "model_yes_probability": 0.61,
    })
    weak = morefire_btc_confirmed_decision(row, {
        "ticker": "KXBTC-1",
        "depth_contracts": 800.0,
        "yes_mid_cents": 49.0,
        "no_mid_cents": 51.0,
        "dominant_side": "NO",
        "predicted_side": "NO",
        "model_yes_probability": 0.44,
    })

    assert accepted is not None and accepted.decision_status == ACCEPTED
    assert "BTC_DEPTH_GE_1225" in accepted.reason_codes
    assert weak is not None and weak.decision_status == RESEARCH_ONLY
    assert "BTC_DEPTH_WEAK_OR_MISSING" in weak.reason_codes
    assert "BTC_DOMINANT_SIDE_NO" in weak.reason_codes


def test_ledger_resolves_skipped_rows_and_scoreboard(tmp_path):
    led = StrategyBotLedger(tmp_path / "v3.sqlite3")
    decision = bnb_no_confirmation_decision(_row(
        spot_depth_trade_sell_notional_15s=45.0,
    ))
    assert decision is not None
    row_id = led.record_decision(decision, _row(
        spot_depth_trade_sell_notional_15s=45.0,
    ), source_system="ultoim_v2")
    assert row_id is not None

    assert led.resolve(
        source_system="ultoim_v2",
        source_model_version="ultoim-v2",
        ticker="KXBNB-1",
        official_result="NO",
        now=1600.0,
    ) == 1
    rows = led.rows(STRATEGY_VERSION)
    assert rows[0]["official_result"] == "NO"
    assert rows[0]["correct"] == 1
    assert math.isclose(
        rows[0]["hypothetical_pnl_cents"],
        net_pnl_cents(60.0, True),
    )

    sb = led.scoreboard(STRATEGY_VERSION, min_n=2)
    assert sb["by_bot"][BOT_BNB_NO]["resolved"] == 1
    assert sb["by_bot"][BOT_BNB_NO]["provisional"] is True


def test_runtime_suppresses_duplicate_hype_window_and_marks_muted_notification(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_STRATEGY_BOTS_DB", str(tmp_path / "v3.sqlite3"))
    monkeypatch.setenv("Q15_V3_TELEGRAM_ENABLED", "false")
    runtime._ledger = None
    runtime._telegram = None

    first = _row(
        asset="HYPE",
        ticker="KXHYPE-1",
        predicted_side="YES",
        record_kind="RESEARCH_YES",
        delivery_status="RESEARCH",
        spot_depth_trade_net_qty_60s=45.0,
        yes_ask_depth_contracts=320.0,
        kalshi_taker_net_yes_volume_15s=None,
    )
    second = dict(first, ticker="KXHYPE-2")

    assert runtime.record_source_row(first, source_system="ultoim_v2") == 2
    assert runtime.record_source_row(second, source_system="ultoim_v2") == 2

    led = runtime.get_ledger()
    assert led is not None
    hype_rows = [
        r for r in led.rows(STRATEGY_VERSION)
        if r["bot_name"] == BOT_HYPE_YES
    ]
    assert [r["decision_status"] for r in hype_rows] == [ACCEPTED, REJECTED]
    assert hype_rows[0]["notification_status"] == "MUTED"
    assert "DUPLICATE_HYPE_WINDOW_EXPOSURE" in hype_rows[1]["reason_codes"]


def test_v3_telegram_requires_dedicated_chat(monkeypatch):
    monkeypatch.setenv("Q15_V3_TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "old-room")
    monkeypatch.setenv("Q15_ULTOIM_V2_TELEGRAM_CHAT_ID", "v2-room")
    monkeypatch.delenv("Q15_V3_TELEGRAM_CHAT_ID", raising=False)

    muted = V3Telegram()

    assert muted.chat_id == ""
    assert muted.enabled is False

    monkeypatch.setenv("Q15_V3_TELEGRAM_CHAT_ID", "v3-room")
    dedicated = V3Telegram()

    assert dedicated.chat_id == "v3-room"
    assert dedicated.enabled is True
