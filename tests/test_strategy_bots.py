from __future__ import annotations

import math

from q15_upgrade.strategy_bots import runtime
from q15_upgrade.strategy_bots.ledger import StrategyBotLedger, net_pnl_cents
from q15_upgrade.strategy_bots.rules import (
    ACCEPTED,
    BOT_BASELINE,
    BOT_BNB_NO,
    BOT_BNB_YES_REVERSAL,
    BOT_CONFIDENCE_TIER,
    BOT_HYPE_YES,
    BOT_MOREFIRE_BTC,
    BOT_NINE_MINUTE,
    REJECTED,
    RESEARCH_ONLY,
    STRATEGY_VERSION,
    bnb_no_confirmation_decision,
    bnb_yes_reversal_decision,
    confidence_tier_decision,
    hype_yes_confirmation_decision,
    morefire_btc_confirmed_decision,
    nine_minute_delivery_decision,
    yes_alt_veto,
)
from q15_upgrade.strategy_bots.telegram import V3Telegram, build_v3_alert


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


def test_bnb_no_rejects_positive_60s_spot_net_notional():
    d = bnb_no_confirmation_decision(_row(
        spot_depth_trade_net_notional_60s=1.0,
        spot_depth_trade_net_qty_60s=-1.0,
        kalshi_taker_net_yes_volume_15s=-1.0,
        spot_depth_imbalance=-0.03,
        spot_depth_trade_sell_notional_15s=80.0,
        spot_depth_trade_net_notional_15s=-50.0,
    ))

    assert d is not None
    assert d.decision_status == REJECTED
    assert "BNB_NO_VETO_SPOT_NET_NOTIONAL_60S_POSITIVE" in d.reason_codes


def test_bnb_no_rejects_kalshi_taker_yes_contradiction():
    d = bnb_no_confirmation_decision(_row(
        kalshi_taker_net_yes_volume_15s=10.0,
        spot_depth_trade_net_notional_60s=-10.0,
        spot_depth_trade_net_qty_60s=-1.0,
        spot_depth_imbalance=-0.03,
        spot_depth_trade_sell_notional_15s=80.0,
        spot_depth_trade_net_notional_15s=-50.0,
    ))

    assert d is not None
    assert d.decision_status == REJECTED
    assert "BNB_NO_VETO_KALSHI_TAKER_YES_GE_10" in d.reason_codes


def test_bnb_no_requires_two_bearish_confirmations():
    d = bnb_no_confirmation_decision(_row(
        spot_depth_trade_sell_notional_15s=41.0,
        spot_depth_imbalance=-0.001,
        spot_depth_trade_net_notional_60s=-5.0,
        spot_depth_trade_net_qty_60s=-0.1,
        kalshi_taker_net_yes_volume_15s=1.0,
    ))

    assert d is not None
    assert d.decision_status == REJECTED
    assert "BNB_NO_BEARISH_SCORE_LT_2" in d.reason_codes


def test_bnb_no_accepts_without_veto_and_two_bearish_confirmations():
    d = bnb_no_confirmation_decision(_row(
        spot_depth_trade_sell_notional_15s=41.0,
        spot_depth_imbalance=-0.021,
        spot_depth_trade_net_notional_60s=-1.0,
        spot_depth_trade_net_qty_60s=-0.1,
        kalshi_taker_net_yes_volume_15s=-1.0,
    ))

    assert d is not None
    assert d.decision_status == ACCEPTED
    assert "SELL_NOTIONAL_15S_GE_40" in d.reason_codes
    assert "SPOT_IMBALANCE_LE_NEG_0_02" in d.reason_codes


def test_bnb_yes_reversal_fires_research_only_for_ultoim_vetoed_no():
    row = _row(
        reason_codes="ASK_ABOVE_BAND,EDGE_BELOW_MIN",
        spot_depth_imbalance=0.02,
        spot_depth_trade_net_notional_60s=60.0,
        spot_depth_trade_net_qty_60s=0.1,
        spot_depth_trade_net_notional_15s=5.0,
        kalshi_taker_net_yes_volume_15s=11.0,
        yes_ask_cents=24.0,
    )
    no_decision = bnb_no_confirmation_decision(row)
    reversal = bnb_yes_reversal_decision(
        row,
        source_system="ultoim_v2",
        no_decision=no_decision,
    )

    assert reversal is not None
    assert reversal.bot_name == BOT_BNB_YES_REVERSAL
    assert reversal.decision_status == RESEARCH_ONLY
    assert reversal.side_override == "YES"
    assert reversal.original_source_side == "NO"
    assert reversal.entry_ask_cents == 24.0
    assert "BNB_YES_REVERSAL_SPOT_NET_NOTIONAL_60S_GE_50" in reversal.reason_codes
    assert "BNB_NO_VETO_SPOT_NET_NOTIONAL_60S_POSITIVE" in reversal.reason_codes


def test_bnb_yes_reversal_derives_yes_entry_from_no_ask_and_spread():
    row = _row(
        reason_codes="EXPENSIVE_NO_ADMIT",
        entry_ask_cents=84.0,
        spread_cents=2.0,
        spot_depth_imbalance=-0.03,
        spot_depth_trade_net_notional_60s=60.0,
        spot_depth_trade_net_qty_60s=0.1,
    )
    no_decision = bnb_no_confirmation_decision(row)
    reversal = bnb_yes_reversal_decision(
        row,
        source_system="ultoim_v2",
        no_decision=no_decision,
    )

    assert reversal is not None
    assert reversal.entry_ask_cents == 18.0
    assert "BNB_YES_REVERSAL_ENTRY_ESTIMATED_FROM_NO_SPREAD" in reversal.reason_codes


def test_bnb_yes_reversal_does_not_fire_for_hvf_bnb_rows():
    row = _row(
        reason_codes="HVF_OWN_NO_FLASH",
        rule_code="HVF_OWN_NO_FLASH",
        record_kind="HIGH_VOL_FLIP_ALERT",
        spot_depth_imbalance=0.02,
        spot_depth_trade_net_notional_60s=60.0,
        spot_depth_trade_net_qty_60s=0.1,
    )
    no_decision = bnb_no_confirmation_decision(row)

    assert bnb_yes_reversal_decision(
        row,
        source_system="high_vol_flip",
        no_decision=no_decision,
    ) is None


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


def test_confidence_tier_prioritizes_a_over_b():
    d = confidence_tier_decision(
        _row(asset="BTC", ticker="KXBTC-1", predicted_side="YES", entry_ask_cents=80.0),
        source_system="ultoim_v2",
    )

    assert d.bot_name == BOT_CONFIDENCE_TIER
    assert d.decision_status == ACCEPTED
    assert d.tier == "A"
    assert "V3_TIER_A_STRICT_7_HIGH_CONFIDENCE" in d.reason_codes
    assert not any("V3_TIER_B" in code for code in d.reason_codes)


def test_confidence_tier_b_volume_expansion():
    d = confidence_tier_decision(
        _row(asset="BTC", ticker="KXBTC-1", predicted_side="NO", entry_ask_cents=63.0),
        source_system="ultoim_v2",
    )

    assert d.decision_status == ACCEPTED
    assert d.tier == "B"
    assert "V3_TIER_B_VOLUME_EXPANSION" in d.reason_codes


def test_confidence_tier_c_is_research_only():
    d = confidence_tier_decision(
        _row(
            asset="XRP",
            ticker="KXXRP-1",
            predicted_side="NO",
            entry_ask_cents=77.0,
            reason_codes="EXPENSIVE_NO_ADMIT,RISK_LOW",
        ),
        source_system="ultoim_v2",
    )

    assert d.decision_status == RESEARCH_ONLY
    assert d.tier == "C"
    assert "V3_TIER_C_RESEARCH_ONLY" in d.reason_codes


def test_confidence_tier_rejects_non_matching_rows_but_records_none():
    d = confidence_tier_decision(
        _row(asset="SOL", ticker="KXSOL-1", predicted_side="YES", entry_ask_cents=52.0),
        source_system="ultoim_v2",
    )

    assert d.decision_status == REJECTED
    assert d.tier == "NONE"
    assert d.reason_codes == ("V3_CONFIDENCE_TIER_NO_MATCH",)


def test_ledger_resolves_skipped_rows_and_scoreboard(tmp_path):
    led = StrategyBotLedger(tmp_path / "v3.sqlite3")
    decision = bnb_no_confirmation_decision(_row(
        spot_depth_trade_sell_notional_15s=45.0,
        spot_depth_trade_net_qty_15s=-1.0,
        spot_depth_trade_net_notional_60s=-1.0,
        spot_depth_trade_net_qty_60s=-1.0,
    ))
    assert decision is not None
    row_id = led.record_decision(decision, _row(
        spot_depth_trade_sell_notional_15s=45.0,
        spot_depth_trade_net_qty_15s=-1.0,
        spot_depth_trade_net_notional_60s=-1.0,
        spot_depth_trade_net_qty_60s=-1.0,
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


def test_scoreboard_includes_tiers_and_data_coverage(tmp_path):
    led = StrategyBotLedger(tmp_path / "v3.sqlite3")
    row = _row(
        asset="BTC",
        ticker="KXBTC-1",
        predicted_side="YES",
        entry_ask_cents=80.0,
        spread_cents=2.0,
        yes_bid_depth_contracts=100.0,
        yes_ask_depth_contracts=200.0,
        kalshi_taker_net_yes_volume_15s=12.0,
        spot_depth_imbalance=0.25,
        spot_depth_trade_net_qty_15s=1.0,
        spot_depth_trade_net_notional_60s=100.0,
    )
    decision = confidence_tier_decision(row, source_system="ultoim_v2")

    assert led.record_decision(decision, row, source_system="ultoim_v2") is not None
    assert led.resolve(
        source_system="ultoim_v2",
        source_model_version="ultoim-v2",
        ticker="KXBTC-1",
        official_result="YES",
        now=1600.0,
    ) == 1

    sb = led.scoreboard(STRATEGY_VERSION, min_n=2)
    assert sb["by_tier"]["A"]["rows"] == 1
    assert sb["by_tier_source_asset_side_rule"]["A|ultoim_v2|BTC|YES|TEST"]["rows"] == 1
    coverage = sb["data_coverage"]["by_source_asset_tier"]["ultoim_v2|BTC|A"]
    assert coverage["counts"]["entry_ask"] == 1
    assert coverage["counts"]["kalshi_depth"] == 1
    assert coverage["counts"]["kalshi_taker_flow"] == 1
    assert coverage["counts"]["spot_depth"] == 1
    assert coverage["counts"]["spot_trade_flow_15s"] == 1
    assert coverage["counts"]["spot_trade_flow_60s"] == 1
    assert coverage["counts"]["settlement"] == 1


def test_scoreboard_includes_bnb_veto_and_yes_reversal_candidates(tmp_path):
    led = StrategyBotLedger(tmp_path / "v3.sqlite3")
    row = _row(
        reason_codes="EXPENSIVE_NO_ADMIT,RISK_LOW",
        spot_depth_imbalance=-0.02,
        spot_depth_trade_net_notional_60s=75.0,
        spot_depth_trade_net_qty_60s=0.2,
        yes_ask_cents=25.0,
    )
    no_decision = bnb_no_confirmation_decision(row)
    assert no_decision is not None
    reversal = bnb_yes_reversal_decision(
        row,
        source_system="ultoim_v2",
        no_decision=no_decision,
    )
    assert reversal is not None

    assert led.record_decision(no_decision, row, source_system="ultoim_v2") is not None
    assert led.record_decision(reversal, row, source_system="ultoim_v2") is not None
    assert led.resolve(
        source_system="ultoim_v2",
        source_model_version="ultoim-v2",
        ticker="KXBNB-1",
        official_result="YES",
        now=1600.0,
    ) == 2

    rows = led.rows(STRATEGY_VERSION)
    reversal_row = next(r for r in rows if r["bot_name"] == BOT_BNB_YES_REVERSAL)
    assert reversal_row["side"] == "YES"
    assert reversal_row["original_source_side"] == "NO"
    assert math.isclose(reversal_row["hypothetical_pnl_cents"], net_pnl_cents(25.0, True))

    bnb = led.scoreboard(STRATEGY_VERSION)["bnb_system"]
    assert bnb["bnb_no_vetoed"]["rows"] == 1
    assert bnb["bnb_yes_reversal_candidates"]["rows"] == 1
    assert bnb["no_veto_yes_would_have_won"]["rows"] == 1
    assert bnb["yes_reversal_won"]["rows"] == 1


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

    assert runtime.record_source_row(first, source_system="ultoim_v2") == 3
    assert runtime.record_source_row(second, source_system="ultoim_v2") == 3

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


def test_v3_alert_omits_missing_btc_context():
    text = build_v3_alert({
        "asset": "BNB",
        "side": "NO",
        "interval": "10M",
        "bot_name": BOT_BNB_NO,
        "source_rule": "EXPENSIVE_NO_ADMIT",
        "ticker": "KXBNB-1",
        "entry_ask_cents": 84.0,
        "spread_cents": 2.0,
        "depth_contracts": 37.88,
        "yes_ask_depth_contracts": 13.0,
        "kalshi_taker_net_yes_volume_15s": -0.06,
        "spot_depth_imbalance": -0.032,
        "spot_depth_trade_sell_notional_15s": 31.5,
        "spot_depth_trade_net_qty_60s": 6.67,
        "reason_codes": "SPOT_IMBALANCE_LE_NEG_0_02",
    })

    assert "n/a" not in text
    assert "V3 FILTERED PICK" not in text
    assert "V3 BNB COMBINED DECISION" in text
    assert "Action: TAKE BNB NO" in text
    assert "\nBTC:" not in text
    assert "Kalshi:" in text
    assert "Spot:" in text


def test_v3_bnb_reversal_alert_replaces_old_bnb_notification_style():
    text = build_v3_alert({
        "asset": "BNB",
        "side": "YES",
        "original_source_side": "NO",
        "interval": "10M",
        "bot_name": BOT_BNB_YES_REVERSAL,
        "source_rule": "EXPENSIVE_NO_ADMIT",
        "ticker": "KXBNB-1",
        "spread_cents": 2.0,
        "spot_depth_imbalance": -0.032,
        "spot_depth_trade_net_notional_60s": 3709.87,
        "spot_depth_trade_net_qty_60s": 6.67,
        "reason_codes": (
            "BNB_YES_REVERSAL_RESEARCH_ONLY,"
            "BNB_NO_VETO_SPOT_NET_NOTIONAL_60S_POSITIVE"
        ),
    })

    assert "V3 FILTERED PICK" not in text
    assert "V3 RESEARCH YES REVERSAL" not in text
    assert "V3 BNB COMBINED DECISION" in text
    assert "Action: RESEARCH YES REVERSAL" in text
    assert "Original side: NO" in text
    assert "Mode: research-only tracking" in text
    assert "n/a" not in text


def test_v3_alert_includes_btc_context_when_available():
    text = build_v3_alert({
        "asset": "ETH",
        "side": "YES",
        "interval": "7M",
        "bot_name": BOT_MOREFIRE_BTC,
        "source_rule": "HVF_MORE_FIRE_STRICT",
        "ticker": "KXETH-1",
        "entry_ask_cents": 65.0,
        "spread_cents": 2.0,
        "btc_depth_contracts": 3604.1,
        "btc_book_pressure_cents": 25.0,
        "btc_dominant_side": "YES",
        "reason_codes": "BTC_DEPTH_GE_1225",
    })

    assert "BTC: depth 3604, pressure 25.0c, side YES" in text
    assert "n/a" not in text


def test_v3_tier_b_alert_is_labeled_volume_expansion():
    text = build_v3_alert({
        "asset": "BTC",
        "side": "NO",
        "tier": "B",
        "interval": "10M",
        "bot_name": BOT_CONFIDENCE_TIER,
        "source_rule": "EXPENSIVE_NO_ADMIT",
        "ticker": "KXBTC-1",
        "entry_ask_cents": 63.0,
        "spread_cents": 2.0,
        "reason_codes": "V3_TIER_B_VOLUME_EXPANSION,V3_TIER_B_ULTOIM_BTC_NO_ASK_GE_62",
    })

    assert "V3 TIER B / VOLUME EXPANSION" in text
    assert "Tier: B" in text
    assert "Mode: paper/research tracking" in text
    assert "n/a" not in text


def test_tier_c_research_notification_requires_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_STRATEGY_BOTS_DB", str(tmp_path / "v3.sqlite3"))
    monkeypatch.setenv("Q15_V3_TELEGRAM_ENABLED", "false")
    monkeypatch.setenv("Q15_V3_RESEARCH_TELEGRAM_ENABLED", "false")
    runtime._ledger = None
    runtime._telegram = None

    row = _row(
        asset="XRP",
        ticker="KXXRP-1",
        predicted_side="NO",
        entry_ask_cents=77.0,
        reason_codes="EXPENSIVE_NO_ADMIT,RISK_LOW",
    )

    assert runtime.record_source_row(row, source_system="ultoim_v2") == 2
    led = runtime.get_ledger()
    assert led is not None
    tier = next(r for r in led.rows(STRATEGY_VERSION) if r["bot_name"] == BOT_CONFIDENCE_TIER)
    assert tier["decision_status"] == RESEARCH_ONLY
    assert tier["tier"] == "C"
    assert tier["notification_status"] is None


def test_v3_owned_source_notification_suppression_is_explicit(monkeypatch):
    row = _row(asset="BTC", ticker="KXBTC-1", predicted_side="YES", entry_ask_cents=80.0)
    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_V3_SUPPRESS_OWNED_SOURCE_NOTIFICATIONS", "false")

    assert runtime.owns_source_notification(row, source_system="ultoim_v2") is False

    monkeypatch.setenv("Q15_V3_SUPPRESS_OWNED_SOURCE_NOTIFICATIONS", "true")

    assert runtime.owns_source_notification(row, source_system="ultoim_v2") is True


# --- #1 lever: deliver 9M alerts (Q15_V3_DELIVER_9M) ---

def test_nine_minute_factory_accepts_live_9m_with_valid_side():
    d = nine_minute_delivery_decision(_row(interval="9M", predicted_side="NO"))
    assert d is not None
    assert d.bot_name == BOT_NINE_MINUTE
    assert d.decision_status == ACCEPTED
    assert "V3_9M_DELIVER" in d.reason_codes


def test_nine_minute_factory_skips_other_intervals_research_and_no_side():
    assert nine_minute_delivery_decision(_row(interval="10M", predicted_side="NO")) is None
    assert nine_minute_delivery_decision(_row(interval="12M", predicted_side="YES")) is None
    assert nine_minute_delivery_decision(
        _row(interval="9M", predicted_side="NO",
             record_kind="RESEARCH_YES", delivery_status="RESEARCH")
    ) is None
    assert nine_minute_delivery_decision(_row(interval="9M", predicted_side="MAYBE")) is None


def test_runtime_deliver_9m_adds_row_only_when_flag_on(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_STRATEGY_BOTS_DB", str(tmp_path / "v3.sqlite3"))
    monkeypatch.setenv("Q15_V3_TELEGRAM_ENABLED", "false")
    monkeypatch.delenv("Q15_V3_VETO_YES_ALTS", raising=False)
    runtime._ledger = None
    runtime._telegram = None

    row = _row(asset="BTC", ticker="KXBTC-9m", interval="9M",
               predicted_side="NO", entry_ask_cents=60.0)

    monkeypatch.setenv("Q15_V3_DELIVER_9M", "false")
    runtime.record_source_row(row, source_system="ultoim_v2")
    led = runtime.get_ledger()
    assert led is not None
    assert not [r for r in led.rows(STRATEGY_VERSION) if r["bot_name"] == BOT_NINE_MINUTE]

    monkeypatch.setenv("Q15_V3_DELIVER_9M", "true")
    runtime.record_source_row(dict(row, ticker="KXBTC-9m-2"), source_system="ultoim_v2")
    nine = [r for r in led.rows(STRATEGY_VERSION) if r["bot_name"] == BOT_NINE_MINUTE]
    assert len(nine) == 1
    assert nine[0]["decision_status"] == ACCEPTED
    assert nine[0]["interval"] == "9M"
    # other intervals are never muted by this lever
    assert nine_minute_delivery_decision(dict(row, interval="12M")) is None


# --- #3 lever: veto YES on SOL/ETH/XRP (Q15_V3_VETO_YES_ALTS) ---

def test_yes_alt_veto_flips_accepted_alt_yes_but_spares_control_and_no_side():
    row = _row(asset="SOL", ticker="KXSOL-y", predicted_side="YES", entry_ask_cents=70.0)
    accepted = confidence_tier_decision(row, source_system="high_vol_flip")
    assert accepted.decision_status == ACCEPTED  # SOL YES ask>=66 is Tier A

    vetoed = yes_alt_veto(accepted, row)
    assert vetoed.decision_status == REJECTED
    assert "V3_YES_ALT_VETO" in vetoed.reason_codes

    # NO side and non-alt assets untouched
    no_row = _row(asset="SOL", predicted_side="NO")
    no_dec = nine_minute_delivery_decision(dict(no_row, interval="9M"))
    assert yes_alt_veto(no_dec, no_row).decision_status == ACCEPTED
    btc_row = _row(asset="BTC", predicted_side="YES")
    btc_dec = nine_minute_delivery_decision(dict(btc_row, interval="9M"))
    assert yes_alt_veto(btc_dec, btc_row).decision_status == ACCEPTED


def test_yes_alt_veto_never_touches_baseline_control():
    row = _row(asset="ETH", predicted_side="YES")
    from q15_upgrade.strategy_bots.rules import baseline_decision
    base = baseline_decision(row)
    assert base.bot_name == BOT_BASELINE and base.decision_status == ACCEPTED
    assert yes_alt_veto(base, row).decision_status == ACCEPTED


def test_runtime_yes_alt_veto_rejects_tier_pick_when_flag_on(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_STRATEGY_BOTS_DB", str(tmp_path / "v3.sqlite3"))
    monkeypatch.setenv("Q15_V3_TELEGRAM_ENABLED", "false")
    monkeypatch.setenv("Q15_V3_DELIVER_9M", "false")
    monkeypatch.setenv("Q15_V3_VETO_YES_ALTS", "true")
    runtime._ledger = None
    runtime._telegram = None

    row = _row(asset="SOL", ticker="KXSOL-veto", predicted_side="YES", entry_ask_cents=70.0)
    runtime.record_source_row(row, source_system="high_vol_flip")
    led = runtime.get_ledger()
    assert led is not None
    rows = led.rows(STRATEGY_VERSION)
    tier = next(r for r in rows if r["bot_name"] == BOT_CONFIDENCE_TIER)
    assert tier["decision_status"] == REJECTED
    assert "V3_YES_ALT_VETO" in tier["reason_codes"]
    assert tier["notification_status"] is None
    # control arm stays ACCEPTED for honest measurement
    base = next(r for r in rows if r["bot_name"] == BOT_BASELINE)
    assert base["decision_status"] == ACCEPTED
