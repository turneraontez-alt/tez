from __future__ import annotations

from dataclasses import replace
import json
import math
import sqlite3

import cycle_watchdog
import pytest
import q15_upgrade.strategy_bots.ledger as strategy_ledger_module

from q15_upgrade.rti_path_13m import RTI_POINT_IN_TIME_RISK_POLICY_VERSION
from q15_upgrade.strategy_bots import runtime
from q15_upgrade.strategy_bots import rti_microstructure_v11 as micro_v11
from q15_upgrade.strategy_bots.costs import RTI_EXECUTION_COST_MODEL_VERSION
from q15_upgrade.strategy_bots.ledger import (
    StrategyBotLedger,
    kalshi_order_fee_cents,
    net_pnl_cents,
)
from q15_upgrade.strategy_bots.rules import (
    ACCEPTED,
    BOT_BNB_NO,
    BOT_BNB_YES_REVERSAL,
    BOT_BTC_REGIME,
    BOT_CONFIDENCE_TIER,
    BOT_DEPTH_FORMULA_15M,
    BOT_HYPE_YES,
    BOT_HVF_DEPTH_FLOW,
    BOT_MOREFIRE_BTC,
    BOT_MOREFIRE_NO_ENTRY_PRICE,
    BOT_RTI_PATH_13M,
    BOT_THIRTEEN_M_SNIPER,
    REJECTED,
    RESEARCH_ONLY,
    STRATEGY_VERSION,
    RTI_PATH_13M_INDEX_IDS,
    RTI_PATH_13M_RULE_VERSION,
    RTI_PATH_13M_RULE_VERSIONS,
    RTI_PATH_13M_FEE_SCHEDULE_VERSION,
    RTI_EXACT_MICROSTRUCTURE_SCHEMA_VERSION,
    RTI_PATH_13M_COUNTERTREND_CHALLENGER_ID,
    RTI_PATH_13M_COUNTERTREND_POLICY_VERSION,
    RTI_PATH_13M_DELAYED_CONFIRM_60S_CHALLENGER_ID,
    RTI_PATH_13M_DELAYED_CONFIRM_60S_POLICY_VERSION,
    RTI_PATH_13M_DELAYED_CONFIRM_90S_CHALLENGER_ID,
    RTI_PATH_13M_DELAYED_CONFIRM_90S_POLICY_VERSION,
    RTI_PATH_13M_DELAYED_CONFIRM_CHALLENGER_ID,
    RTI_PATH_13M_DELAYED_CONFIRM_POLICY_VERSION,
    RTI_PATH_13M_DELAYED_FLIP_60S_CHALLENGER_ID,
    RTI_PATH_13M_DELAYED_FLIP_60S_POLICY_VERSION,
    RTI_PATH_13M_IMPULSE_CHALLENGER_ID,
    RTI_PATH_13M_IMPULSE_POLICY_VERSION,
    RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID,
    RTI_PATH_13M_MICROSTRUCTURE_V11_POLICY_VERSION,
    RTI_PATH_13M_PROBABILITY_V2_CHALLENGER_ID,
    RTI_PATH_13M_PROBABILITY_V2_POLICY_VERSION,
    RTI_PATH_13M_PROBABILITY_V3_CHALLENGER_ID,
    RTI_PATH_13M_PROBABILITY_V3_POLICY_VERSION,
    RTI_PATH_13M_SPOT_CONFIRM_CHALLENGER_ID,
    RTI_PATH_13M_SPOT_CONFIRM_POLICY_VERSION,
    btc_regime_context_probe_decision,
    baseline_decision,
    bnb_no_confirmation_decision,
    bnb_yes_reversal_decision,
    confidence_tier_decision,
    depth_formula_15m_research_decision,
    hvf_depth_flow_wrapper_decision,
    hype_yes_confirmation_decision,
    morefire_btc_confirmed_decision,
    morefire_no_entry_price_probe_decision,
    rti_path_11m30_stability_decision,
    rti_path_12m_confirmation_decision,
    rti_path_12m30_confirmation_decision,
    rti_path_13m_decision,
    rti_path_13m_rule_version,
    thirteen_m_sniper_decision,
)
from q15_upgrade.strategy_bots.btc_regime import enrich_btc_regime
from q15_upgrade.strategy_bots.l2_depth import enrich_coinbase_l2
from q15_upgrade.strategy_bots.kraken_l3_depth import enrich_kraken_l3
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


def _book_levels(start: float, qty: float, *, step: float = 1.0, count: int = 12):
    return [[start + i * step, qty] for i in range(count)]


def test_coinbase_l2_enrichment_uses_only_past_snapshots(tmp_path, monkeypatch):
    db = tmp_path / "coinbase_l2.sqlite3"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE coinbase_adv_l2_snapshots ("
            "created_at REAL, product_id TEXT, bid_levels_json TEXT, ask_levels_json TEXT, "
            "best_bid REAL, best_ask REAL, mid REAL, spread_bps REAL)"
        )
        conn.execute(
            "INSERT INTO coinbase_adv_l2_snapshots VALUES (?,?,?,?,?,?,?,?)",
            (
                90.0,
                "BTC-USD",
                json.dumps(_book_levels(100.0, 2.0, step=-1.0)),
                json.dumps(_book_levels(101.0, 1.0)),
                100.0,
                101.0,
                100.5,
                99.5,
            ),
        )
        conn.execute(
            "INSERT INTO coinbase_adv_l2_snapshots VALUES (?,?,?,?,?,?,?,?)",
            (
                101.0,
                "BTC-USD",
                json.dumps(_book_levels(100.0, 1.0, step=-1.0)),
                json.dumps(_book_levels(101.0, 5.0)),
                100.0,
                101.0,
                100.5,
                99.5,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("Q15_COINBASE_ADV_L2_DB", str(db))
    monkeypatch.setenv("Q15_V3_COINBASE_L2_MAX_AGE_SECONDS", "20")
    monkeypatch.setenv("Q15_CHALLENGER_SHADOW_DB", str(tmp_path / "missing_shadow.sqlite3"))

    out = enrich_coinbase_l2(_row(asset="BTC", predicted_side="YES", created_at=100.0))

    assert out["coinbase_l2_status"] == "ok"
    assert out["coinbase_l2_snapshot_created_at"] == 90.0
    assert out["coinbase_l2_age_seconds"] == 10.0
    assert out["coinbase_l2_top_12_imbalance_notional"] > 0


def test_kraken_l3_enrichment_uses_only_past_snapshots(tmp_path, monkeypatch):
    db = tmp_path / "kraken_l3.sqlite3"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE kraken_l3_summaries ("
            "created_at REAL, symbol TEXT, best_bid REAL, best_ask REAL, depth_imbalance REAL)"
        )
        conn.execute(
            "INSERT INTO kraken_l3_summaries VALUES (?,?,?,?,?)",
            (90.0, "BTC/USD", 100.0, 101.0, 0.25),
        )
        conn.execute(
            "INSERT INTO kraken_l3_summaries VALUES (?,?,?,?,?)",
            (101.0, "BTC/USD", 100.0, 101.0, -0.80),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("Q15_KRAKEN_L3_DB", str(db))
    monkeypatch.setenv("Q15_V3_KRAKEN_L3_MAX_AGE_SECONDS", "20")

    out = enrich_kraken_l3(_row(asset="BTC", predicted_side="YES", created_at=100.0))

    assert out["kraken_l3_status"] == "ok"
    assert out["kraken_l3_snapshot_created_at"] == 90.0
    assert out["kraken_l3_age_seconds"] == 10.0
    assert out["kraken_l3_depth_imbalance"] == 0.25


def test_btc_regime_enrichment_stamps_alt_rows_point_in_time(tmp_path, monkeypatch):
    spot_db = tmp_path / "spot.sqlite3"
    conn = sqlite3.connect(spot_db)
    try:
        conn.execute(
            "CREATE TABLE spot_depth_snapshots ("
            "created_at REAL, asset TEXT, depth_imbalance REAL, "
            "trade_buy_notional_15s REAL, trade_sell_notional_15s REAL, "
            "trade_buy_notional_60s REAL, trade_sell_notional_60s REAL)"
        )
        conn.execute(
            "INSERT INTO spot_depth_snapshots VALUES (?,?,?,?,?,?,?)",
            (90.0, "BTC", 0.20, 2000.0, 500.0, 8000.0, 1000.0),
        )
        conn.execute(
            "INSERT INTO spot_depth_snapshots VALUES (?,?,?,?,?,?,?)",
            (101.0, "BTC", -0.80, 0.0, 5000.0, 0.0, 9000.0),
        )
        conn.commit()
    finally:
        conn.close()

    l2_db = tmp_path / "coinbase_l2.sqlite3"
    conn = sqlite3.connect(l2_db)
    try:
        conn.execute(
            "CREATE TABLE coinbase_adv_l2_snapshots ("
            "created_at REAL, product_id TEXT, bid_levels_json TEXT, ask_levels_json TEXT)"
        )
        conn.execute(
            "INSERT INTO coinbase_adv_l2_snapshots VALUES (?,?,?,?)",
            (
                90.0,
                "BTC-USD",
                json.dumps(_book_levels(100.0, 5.0, step=-1.0, count=260)),
                json.dumps(_book_levels(101.0, 1.0, count=260)),
            ),
        )
        conn.execute(
            "INSERT INTO coinbase_adv_l2_snapshots VALUES (?,?,?,?)",
            (
                101.0,
                "BTC-USD",
                json.dumps(_book_levels(100.0, 1.0, step=-1.0, count=260)),
                json.dumps(_book_levels(101.0, 9.0, count=260)),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("Q15_SPOT_DEPTH_DB", str(spot_db))
    monkeypatch.setenv("Q15_COINBASE_ADV_L2_DB", str(l2_db))
    monkeypatch.setenv("Q15_KRAKEN_L3_DB", str(tmp_path / "missing_l3.sqlite3"))
    monkeypatch.setenv("Q15_V95_LEDGER_DB", str(tmp_path / "missing_v95.sqlite3"))
    monkeypatch.setenv("Q15_V3_BTC_REGIME_MAX_AGE_SECONDS", "20")

    out = enrich_btc_regime(_row(asset="ETH", predicted_side="YES", created_at=100.0))

    assert out["btc_regime"] == "BULLISH"
    assert out["btc_regime_agreement"] == "AGREES"
    assert out["btc_spot_trade_net_notional_60s"] == 7000.0
    assert out["btc_coinbase_l2_top_60_imbalance_notional"] > 0
    assert "spot60:YES" in out["btc_regime_vote_detail"]


def test_btc_regime_probe_is_research_only_and_labels_doge_chop():
    d = btc_regime_context_probe_decision(
        _row(
            asset="DOGE",
            predicted_side="YES",
            btc_regime="CHOP",
            btc_regime_agreement="CHOP",
            btc_regime_vote_yes=3,
            btc_regime_vote_no=3,
            spot_depth_trade_net_notional_60s=10.0,
            coinbase_l2_top_12_imbalance_notional=0.0,
        ),
        source_system="ultoim_v2",
    )

    assert d is not None
    assert d.bot_name == BOT_BTC_REGIME
    assert d.decision_status == RESEARCH_ONLY
    assert "BTC_REGIME_CHOP_WOULD_DOWNGRADE_RESEARCH_ONLY" in d.reason_codes
    assert "BTC_REGIME_DOGE_REQUIRES_NON_CHOP_NOT_MET" in d.reason_codes


def test_strategy_bot_ledger_stores_btc_regime_fields(tmp_path):
    led = StrategyBotLedger(tmp_path / "v3.sqlite3")
    row = _row(
        asset="BNB",
        predicted_side="NO",
        btc_regime="BEARISH",
        btc_regime_agreement="AGREES",
        btc_regime_vote_yes=2,
        btc_regime_vote_no=6,
        btc_coinbase_l2_top_60_imbalance_notional=-0.18,
        btc_spot_trade_net_notional_60s=-25000.0,
        btc_v95_grade="A",
        btc_kraken_l3_depth_imbalance=-0.25,
    )
    d = btc_regime_context_probe_decision(row, source_system="ultoim_v2")

    assert d is not None
    row_id = led.record_decision(d, row, source_system="ultoim_v2")
    assert row_id is not None
    stored = led.row_by_id(row_id)
    assert stored is not None
    assert stored["bot_name"] == BOT_BTC_REGIME
    assert stored["decision_status"] == RESEARCH_ONLY
    assert stored["btc_regime"] == "BEARISH"
    assert stored["btc_coinbase_l2_top_60_imbalance_notional"] == -0.18
    assert stored["btc_v95_grade"] == "A"


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
    assert strong is not None and strong.decision_status == RESEARCH_ONLY
    assert "TAKER_FLOW_MISSING_STRONGER_CONFIRM_REQUIRED" in strong.reason_codes
    assert "V3_POSITIVE_EV_GATE_HYPE_YES_RESEARCH_ONLY" in strong.reason_codes


def test_hype_yes_override_restores_acceptance(monkeypatch):
    monkeypatch.setenv("Q15_V3_HYPE_YES_ACCEPT_ENABLED", "true")

    d = hype_yes_confirmation_decision(_row(
        asset="HYPE",
        ticker="KXHYPE-OVERRIDE",
        predicted_side="YES",
        spot_depth_imbalance=0.02,
        spot_depth_trade_net_qty_60s=40.0,
        yes_ask_depth_contracts=300.0,
        kalshi_taker_net_yes_volume_15s=None,
    ))

    assert d is not None
    assert d.decision_status == ACCEPTED
    assert "V3_POSITIVE_EV_GATE_HYPE_YES_ALLOWED_BY_OVERRIDE" in d.reason_codes


def test_morefire_btc_confirmed_research_only_even_with_btc_support():
    row = _row(
        asset="SOL",
        ticker="KXSOL-1",
        predicted_side=None,
        predicted_outcome="YES",
        rule_code="HVF_MORE_FIRE_STRICT",
        record_kind="MORE_FIRE_STRICT_ALERT",
    )
    gated = morefire_btc_confirmed_decision(row, {
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

    assert gated is not None and gated.decision_status == RESEARCH_ONLY
    assert "BTC_DEPTH_GE_1225" in gated.reason_codes
    assert "V3_POSITIVE_EV_GATE_MOREFIRE_RESEARCH_ONLY" in gated.reason_codes
    assert weak is not None and weak.decision_status == RESEARCH_ONLY
    assert "BTC_DEPTH_WEAK_OR_MISSING" in weak.reason_codes
    assert "BTC_DOMINANT_SIDE_NO_WARNING" in weak.reason_codes
    assert "BTC_MODEL_MARKET_CONTRA_WARNING" in weak.reason_codes


def test_morefire_btc_override_restores_acceptance(monkeypatch):
    monkeypatch.setenv("Q15_V3_MOREFIRE_ACCEPT_ENABLED", "true")
    row = _row(
        asset="SOL",
        ticker="KXSOL-OVERRIDE",
        predicted_side=None,
        predicted_outcome="YES",
        rule_code="HVF_MORE_FIRE_STRICT",
        record_kind="MORE_FIRE_STRICT_ALERT",
    )
    d = morefire_btc_confirmed_decision(row, {
        "ticker": "KXBTC-1",
        "depth_contracts": 1300.0,
        "yes_mid_cents": 56.0,
        "no_mid_cents": 44.0,
        "dominant_side": "YES",
        "predicted_side": "YES",
        "model_yes_probability": 0.61,
    })

    assert d is not None
    assert d.decision_status == ACCEPTED
    assert "V3_POSITIVE_EV_GATE_MOREFIRE_ALLOWED_BY_OVERRIDE" in d.reason_codes


@pytest.mark.parametrize("entry_ask", [10.0, 75.0, 99.0])
def test_morefire_no_entry_price_probe_ignores_entry_ask(entry_ask):
    d = morefire_no_entry_price_probe_decision(
        _row(
            asset="SOL",
            ticker=f"KXSOL-NO-PRICE-{entry_ask}",
            interval="10M",
            predicted_side=None,
            predicted_outcome="YES",
            rule_code="HVF_MORE_FIRE_STRICT",
            record_kind="MORE_FIRE_STRICT_ALERT",
            entry_ask_cents=entry_ask,
            spot_depth_status="ok",
            spot_depth_trade_age_seconds=2.0,
            spot_depth_trade_net_notional_15s=1200.0,
            kalshi_taker_net_yes_volume_15s=-10.0,
            kraken_l3_status="ok",
            kraken_l3_age_seconds=3.0,
            kraken_l3_depth_imbalance=0.12,
        ),
        source_system="high_vol_flip",
    )

    assert d is not None
    assert d.bot_name == BOT_MOREFIRE_NO_ENTRY_PRICE
    assert d.decision_status == RESEARCH_ONLY
    assert d.threshold_profile["entry_ask_gate"] is None
    assert d.threshold_profile["entry_ask_used_by_probe"] is False


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {"spot_depth_trade_net_notional_15s": 0.0},
            "MOREFIRE_NO_ENTRY_PRICE_SPOT15_NOT_POSITIVE",
        ),
        (
            {"kalshi_taker_net_yes_volume_15s": 1.0},
            "MOREFIRE_NO_ENTRY_PRICE_KALSHI_TAKER_NET_YES_POSITIVE",
        ),
        (
            {"kraken_l3_depth_imbalance": 0.0},
            "MOREFIRE_NO_ENTRY_PRICE_KRAKEN_IMBALANCE_NOT_POSITIVE",
        ),
        (
            {"interval": "7M"},
            "MOREFIRE_NO_ENTRY_PRICE_INTERVAL_NOT_8_10_12M",
        ),
    ],
)
def test_morefire_no_entry_price_probe_rejects_missing_confirmation(overrides, reason):
    row = _row(
        asset="XRP",
        ticker="KXXRP-NO-PRICE-REJECT",
        interval="12M",
        predicted_side=None,
        predicted_outcome="YES",
        rule_code="HVF_MORE_FIRE_STRICT",
        record_kind="MORE_FIRE_STRICT_ALERT",
        spot_depth_status="ok",
        spot_depth_trade_age_seconds=2.0,
        spot_depth_trade_net_notional_15s=500.0,
        kalshi_taker_net_yes_volume_15s=-5.0,
        kraken_l3_status="ok",
        kraken_l3_age_seconds=3.0,
        kraken_l3_depth_imbalance=0.20,
    )
    row.update(overrides)

    d = morefire_no_entry_price_probe_decision(
        row,
        source_system="high_vol_flip",
    )

    assert d is not None
    assert d.decision_status == REJECTED
    assert reason in d.reason_codes


def test_morefire_btc_contra_only_hardens_when_local_depth_flow_contradicts():
    row = _row(
        asset="SOL",
        ticker="KXSOL-1",
        predicted_side=None,
        predicted_outcome="YES",
        rule_code="HVF_MORE_FIRE_STRICT",
        record_kind="MORE_FIRE_STRICT_ALERT",
        spot_depth_trade_net_notional_60s=-5000.0,
        coinbase_l2_top_12_imbalance_notional=-0.20,
        coinbase_l2_top_60_imbalance_notional=-0.20,
    )
    d = morefire_btc_confirmed_decision(row, {
        "ticker": "KXBTC-1",
        "depth_contracts": 1300.0,
        "yes_mid_cents": 56.0,
        "no_mid_cents": 44.0,
        "dominant_side": "NO",
        "predicted_side": "NO",
        "model_yes_probability": 0.44,
    })

    assert d is not None
    assert d.decision_status == RESEARCH_ONLY
    assert "BTC_DOMINANT_SIDE_NO_WITH_LOCAL_CONTRA" in d.reason_codes
    assert "BTC_MODEL_MARKET_CONTRA_WITH_LOCAL_CONTRA" in d.reason_codes
    assert "LOCAL_DEPTH_FLOW_CONTRA" in d.reason_codes


def test_hvf_depth_flow_wrapper_rejects_morefire_spot60_contra():
    d = hvf_depth_flow_wrapper_decision(
        _row(
            asset="SOL",
            ticker="KXSOL-1",
            predicted_side=None,
            predicted_outcome="YES",
            rule_code="HVF_MORE_FIRE_STRICT",
            record_kind="MORE_FIRE_STRICT_ALERT",
            selected_depth_ratio=4.0,
            spot_depth_trade_net_notional_60s=-1200.0,
            kalshi_taker_net_yes_volume_15s=0.0,
        ),
        source_system="high_vol_flip",
    )

    assert d is not None
    assert d.bot_name == BOT_HVF_DEPTH_FLOW
    assert d.decision_status == REJECTED
    assert "HVF_WRAPPER_MOREFIRE_REJECT_SPOT60_NO" in d.reason_codes


def test_hvf_depth_flow_wrapper_gates_morefire_taker_yes_crowd(monkeypatch):
    monkeypatch.setenv("Q15_V3_MOREFIRE_ACCEPT_ENABLED", "true")

    d = hvf_depth_flow_wrapper_decision(
        _row(
            asset="ETH",
            ticker="KXETH-1",
            predicted_side=None,
            predicted_outcome="YES",
            rule_code="HVF_MORE_FIRE_STRICT",
            record_kind="MORE_FIRE_STRICT_ALERT",
            selected_depth_ratio=4.0,
            spot_depth_trade_net_notional_60s=0.0,
            kalshi_taker_net_yes_volume_15s=275.0,
            coinbase_l2_top_12_imbalance_notional=0.20,
            coinbase_l2_top_60_imbalance_notional=0.20,
            coinbase_l2_top_250_imbalance_notional=0.20,
        ),
        source_system="high_vol_flip",
    )

    assert d is not None
    assert d.decision_status == RESEARCH_ONLY
    assert "HVF_WRAPPER_YES_AUDIT_VETO" in d.reason_codes
    assert "HVF_WRAPPER_YES_TAKER_CROWD_VETO" in d.reason_codes


def test_hvf_depth_flow_wrapper_vetoes_yes_spot_imbalance_contra():
    d = hvf_depth_flow_wrapper_decision(
        _row(
            asset="ETH",
            ticker="KXETH-SPOTIMB",
            predicted_side=None,
            predicted_outcome="YES",
            rule_code="HVF_OWN_STRONG_SELECTED",
            record_kind="HIGH_VOL_FLIP_ALERT",
            spot_depth_imbalance=-0.03,
            spot_depth_trade_net_notional_60s=100.0,
            kalshi_taker_net_yes_volume_15s=0.0,
        ),
        source_system="high_vol_flip",
    )

    assert d is not None
    assert d.decision_status == RESEARCH_ONLY
    assert "HVF_WRAPPER_YES_SPOT_IMB_CONTRA_VETO" in d.reason_codes


def test_hvf_depth_flow_wrapper_vetoes_yes_kalshi_book_contra():
    d = hvf_depth_flow_wrapper_decision(
        _row(
            asset="ETH",
            ticker="KXETH-KBOOK",
            predicted_side=None,
            predicted_outcome="YES",
            rule_code="HVF_OWN_STRONG_SELECTED",
            record_kind="HIGH_VOL_FLIP_ALERT",
            yes_bid_depth_contracts=100.0,
            no_bid_depth_contracts=300.0,
            spot_depth_trade_net_notional_60s=100.0,
            kalshi_taker_net_yes_volume_15s=0.0,
        ),
        source_system="high_vol_flip",
    )

    assert d is not None
    assert d.decision_status == RESEARCH_ONLY
    assert "HVF_WRAPPER_YES_KALSHI_BOOK_CONTRA_VETO" in d.reason_codes


def test_hvf_depth_flow_wrapper_rejects_morefire_top250_contra():
    d = hvf_depth_flow_wrapper_decision(
        _row(
            asset="ETH",
            ticker="KXETH-TOP250",
            predicted_side=None,
            predicted_outcome="YES",
            rule_code="HVF_MORE_FIRE_STRICT",
            record_kind="MORE_FIRE_STRICT_ALERT",
            selected_depth_ratio=30.0,
            spot_depth_trade_net_notional_60s=1200.0,
            kalshi_taker_net_yes_volume_15s=-50.0,
            coinbase_l2_top_12_imbalance_notional=0.70,
            coinbase_l2_top_60_imbalance_notional=0.48,
            coinbase_l2_top_250_imbalance_notional=-0.13,
        ),
        source_system="high_vol_flip",
    )

    assert d is not None
    assert d.decision_status == REJECTED
    assert "HVF_WRAPPER_MOREFIRE_REJECT_COINBASE_TOP250_NO" in d.reason_codes


def test_hvf_depth_flow_wrapper_researches_morefire_shallow_l2_contra():
    d = hvf_depth_flow_wrapper_decision(
        _row(
            asset="XRP",
            ticker="KXXRP-SHALLOW",
            predicted_side=None,
            predicted_outcome="YES",
            rule_code="HVF_MORE_FIRE_STRICT",
            record_kind="MORE_FIRE_STRICT_ALERT",
            selected_depth_ratio=6.0,
            spot_depth_trade_net_notional_60s=800.0,
            kalshi_taker_net_yes_volume_15s=20.0,
            coinbase_l2_top_12_imbalance_notional=0.20,
            coinbase_l2_top_60_imbalance_notional=-0.12,
            coinbase_l2_top_250_imbalance_notional=0.20,
        ),
        source_system="high_vol_flip",
    )

    assert d is not None
    assert d.decision_status == RESEARCH_ONLY
    assert "HVF_WRAPPER_MOREFIRE_RESEARCH_COINBASE_TOP60_NO" in d.reason_codes
    assert "HVF_WRAPPER_MOREFIRE_RESEARCH_ONLY_SHALLOW_L2_CONTRA" in d.reason_codes


def test_hvf_depth_flow_wrapper_gates_clean_morefire():
    d = hvf_depth_flow_wrapper_decision(
        _row(
            asset="XRP",
            ticker="KXXRP-1",
            predicted_side=None,
            predicted_outcome="YES",
            rule_code="HVF_MORE_FIRE_STRICT",
            record_kind="MORE_FIRE_STRICT_ALERT",
            selected_depth_ratio=4.0,
            spot_depth_trade_net_notional_60s=200.0,
            kalshi_taker_net_yes_volume_15s=-50.0,
            coinbase_l2_top_12_imbalance_notional=0.20,
            coinbase_l2_top_60_imbalance_notional=0.20,
            coinbase_l2_top_250_imbalance_notional=0.20,
        ),
        source_system="high_vol_flip",
    )

    assert d is not None
    assert d.decision_status == RESEARCH_ONLY
    assert "V3_POSITIVE_EV_GATE_MOREFIRE_RESEARCH_ONLY" in d.reason_codes


def test_hvf_depth_flow_wrapper_morefire_override_accepts_clean_candidate(monkeypatch):
    monkeypatch.setenv("Q15_V3_MOREFIRE_ACCEPT_ENABLED", "true")
    d = hvf_depth_flow_wrapper_decision(
        _row(
            asset="XRP",
            ticker="KXXRP-1-OVERRIDE",
            predicted_side=None,
            predicted_outcome="YES",
            rule_code="HVF_MORE_FIRE_STRICT",
            record_kind="MORE_FIRE_STRICT_ALERT",
            selected_depth_ratio=4.0,
            spot_depth_trade_net_notional_60s=200.0,
            kalshi_taker_net_yes_volume_15s=-50.0,
            coinbase_l2_top_12_imbalance_notional=0.20,
            coinbase_l2_top_60_imbalance_notional=0.20,
            coinbase_l2_top_250_imbalance_notional=0.20,
        ),
        source_system="high_vol_flip",
    )

    assert d is not None
    assert d.decision_status == ACCEPTED
    assert "HVF_WRAPPER_MOREFIRE_ACCEPT_NO_SPOT_TAKER_CONTRA" in d.reason_codes
    assert "V3_POSITIVE_EV_GATE_MOREFIRE_ALLOWED_BY_OVERRIDE" in d.reason_codes


def test_hvf_depth_flow_wrapper_downgrades_morefire_missing_required_data():
    d = hvf_depth_flow_wrapper_decision(
        _row(
            asset="XRP",
            ticker="KXXRP-2",
            predicted_side=None,
            predicted_outcome="YES",
            rule_code="HVF_MORE_FIRE_STRICT",
            record_kind="MORE_FIRE_STRICT_ALERT",
            selected_depth_ratio=None,
            spot_depth_trade_net_notional_60s=None,
            kalshi_taker_net_yes_volume_15s=None,
        ),
        source_system="high_vol_flip",
    )

    assert d is not None
    assert d.decision_status == RESEARCH_ONLY
    assert "HVF_WRAPPER_MOREFIRE_RESEARCH_ONLY_MISSING_DATA" in d.reason_codes


def test_hvf_depth_flow_wrapper_downgrades_own_strong_spot60_contra():
    d = hvf_depth_flow_wrapper_decision(
        _row(
            asset="ETH",
            ticker="KXETH-2",
            predicted_side=None,
            predicted_outcome="YES",
            rule_code="HVF_OWN_STRONG_SELECTED",
            record_kind="HIGH_VOL_FLIP_ALERT",
            spot_depth_trade_net_notional_60s=-900.0,
            kalshi_taker_net_yes_volume_15s=0.0,
        ),
        source_system="high_vol_flip",
    )

    assert d is not None
    assert d.decision_status == RESEARCH_ONLY
    assert "HVF_WRAPPER_OWN_STRONG_RESEARCH_SPOT60_CONTRA_NO" in d.reason_codes


def test_hvf_depth_flow_wrapper_accepts_own_strong_top12_recovery(monkeypatch):
    monkeypatch.setenv("Q15_V3_HVF_YES_CONTRA_VETO_ENABLED", "false")

    d = hvf_depth_flow_wrapper_decision(
        _row(
            asset="SOL",
            ticker="KXSOL-RECOVERY",
            predicted_side=None,
            predicted_outcome="YES",
            rule_code="HVF_OWN_STRONG_SELECTED",
            record_kind="HIGH_VOL_FLIP_ALERT",
            entry_ask_cents=82.0,
            spot_depth_trade_net_notional_60s=-900.0,
            kalshi_taker_net_yes_volume_15s=-300.0,
            coinbase_l2_top_12_imbalance_notional=0.25,
        ),
        source_system="high_vol_flip",
    )

    assert d is not None
    assert d.decision_status == ACCEPTED
    assert "HVF_WRAPPER_OWN_STRONG_ACCEPT_TOP12_RECOVERY_YES" in d.reason_codes
    assert "V3_POSITIVE_EV_GATE_HVF_OWN_STRONG_ALLOWED" in d.reason_codes


def test_hvf_depth_flow_wrapper_keeps_own_strong_research_when_recovery_expensive():
    d = hvf_depth_flow_wrapper_decision(
        _row(
            asset="ETH",
            ticker="KXETH-EXPENSIVE-RECOVERY",
            predicted_side=None,
            predicted_outcome="YES",
            rule_code="HVF_OWN_STRONG_SELECTED",
            record_kind="HIGH_VOL_FLIP_ALERT",
            entry_ask_cents=95.0,
            spot_depth_trade_net_notional_60s=-900.0,
            kalshi_taker_net_yes_volume_15s=0.0,
            coinbase_l2_top_12_imbalance_notional=0.25,
        ),
        source_system="high_vol_flip",
    )

    assert d is not None
    assert d.decision_status == RESEARCH_ONLY
    assert "HVF_WRAPPER_OWN_STRONG_TOP12_RECOVERY_ENTRY_TOO_EXPENSIVE" in d.reason_codes


def test_hvf_depth_flow_wrapper_gates_eth_12m_own_strong():
    d = hvf_depth_flow_wrapper_decision(
        _row(
            asset="ETH",
            ticker="KXETH-12M-OWN-STRONG-GATE",
            interval="12M",
            predicted_side=None,
            predicted_outcome="YES",
            rule_code="HVF_OWN_STRONG_SELECTED",
            record_kind="HIGH_VOL_FLIP_ALERT",
            spot_depth_trade_net_notional_60s=-900.0,
            spot_depth_trade_net_notional_15s=40.0,
            kalshi_taker_net_yes_volume_15s=0.0,
        ),
        source_system="high_vol_flip",
    )

    assert d is not None
    assert d.decision_status == RESEARCH_ONLY
    assert "V3_POSITIVE_EV_GATE_HVF_OWN_STRONG_ETH_12M_RESEARCH_ONLY" in d.reason_codes
    assert "V3_HVF_OWN_STRONG_BACKGROUND_RESEARCH_ONLY" in d.reason_codes
    assert "HVF_REPAIR_ETH_12M_SPOT60_CONTRA" in d.reason_codes
    assert "HVF_REPAIR_ETH_12M_SPOT15_AGREE" in d.reason_codes


def test_hvf_depth_flow_wrapper_gates_sol_12m_no_own_strong():
    d = hvf_depth_flow_wrapper_decision(
        _row(
            asset="SOL",
            ticker="KXSOL-12M-NO-OWN-STRONG-GATE",
            interval="12M",
            predicted_side=None,
            predicted_outcome="NO",
            rule_code="HVF_OWN_STRONG_SELECTED",
            record_kind="HIGH_VOL_FLIP_ALERT",
            spot_depth_trade_net_notional_60s=0.0,
            kalshi_taker_net_yes_volume_15s=300.0,
        ),
        source_system="high_vol_flip",
    )

    assert d is not None
    assert d.decision_status == RESEARCH_ONLY
    assert "V3_POSITIVE_EV_GATE_HVF_OWN_STRONG_SOL_12M_NO_RESEARCH_ONLY" in d.reason_codes
    assert "V3_HVF_OWN_STRONG_BACKGROUND_RESEARCH_ONLY" in d.reason_codes
    assert "HVF_REPAIR_SOL_12M_NO_TAKER_CONTRA" in d.reason_codes
    assert "HVF_REPAIR_SOL_12M_NO_REPEAT_WINDOW_UNAVAILABLE" in d.reason_codes


def test_hvf_depth_flow_wrapper_buckets_sol_12m_no_taker_not_contra():
    d = hvf_depth_flow_wrapper_decision(
        _row(
            asset="SOL",
            ticker="KXSOL-12M-NO-OWN-STRONG-NOT-CONTRA",
            interval="12M",
            predicted_side=None,
            predicted_outcome="NO",
            rule_code="HVF_OWN_STRONG_SELECTED",
            record_kind="HIGH_VOL_FLIP_ALERT",
            spot_depth_trade_net_notional_60s=0.0,
            kalshi_taker_net_yes_volume_15s=-300.0,
        ),
        source_system="high_vol_flip",
    )

    assert d is not None
    assert d.decision_status == RESEARCH_ONLY
    assert "HVF_REPAIR_SOL_12M_NO_TAKER_NOT_CONTRA" in d.reason_codes
    assert "HVF_REPAIR_SOL_12M_NO_TAKER_AGREE" in d.reason_codes


def test_hvf_depth_flow_wrapper_allows_eth_9m_yes_own_strong():
    d = hvf_depth_flow_wrapper_decision(
        _row(
            asset="ETH",
            ticker="KXETH-9M-YES-OWN-STRONG",
            interval="9M",
            predicted_side=None,
            predicted_outcome="YES",
            rule_code="HVF_OWN_STRONG_SELECTED",
            record_kind="HIGH_VOL_FLIP_ALERT",
            spot_depth_trade_net_notional_60s=0.0,
            kalshi_taker_net_yes_volume_15s=0.0,
        ),
        source_system="high_vol_flip",
    )

    assert d is not None
    assert d.decision_status == ACCEPTED
    assert "V3_POSITIVE_EV_GATE_HVF_OWN_STRONG_ALLOWED" in d.reason_codes


def test_hvf_depth_flow_wrapper_interval_gate_can_be_disabled(monkeypatch):
    monkeypatch.setenv("Q15_V3_HVF_OWN_STRONG_INTERVAL_GATE_ENABLED", "false")
    d = hvf_depth_flow_wrapper_decision(
        _row(
            asset="ETH",
            ticker="KXETH-12M-OWN-STRONG-GATE-DISABLED",
            interval="12M",
            predicted_side=None,
            predicted_outcome="YES",
            rule_code="HVF_OWN_STRONG_SELECTED",
            record_kind="HIGH_VOL_FLIP_ALERT",
            spot_depth_trade_net_notional_60s=0.0,
            kalshi_taker_net_yes_volume_15s=0.0,
        ),
        source_system="high_vol_flip",
    )

    assert d is not None
    assert d.decision_status == ACCEPTED
    assert "V3_POSITIVE_EV_GATE_HVF_OWN_STRONG_ALLOWED" in d.reason_codes


def test_hvf_depth_flow_wrapper_gates_xrp_no_flash():
    d = hvf_depth_flow_wrapper_decision(
        _row(
            asset="XRP",
            ticker="KXXRP-NO-FLASH-GATE",
            predicted_side="NO",
            rule_code="HVF_OWN_NO_FLASH",
            record_kind="HIGH_VOL_FLIP_ALERT",
            spot_depth_trade_net_notional_60s=0.0,
            kalshi_taker_net_yes_volume_15s=0.0,
        ),
        source_system="high_vol_flip",
    )

    assert d is not None
    assert d.decision_status == RESEARCH_ONLY
    assert "V3_POSITIVE_EV_GATE_HVF_XRP_NO_FLASH_RESEARCH_ONLY" in d.reason_codes


def test_confidence_tier_prioritizes_a_over_b():
    d = confidence_tier_decision(
        _row(
            asset="BTC",
            ticker="KXBTC-1",
            predicted_side="YES",
            entry_ask_cents=80.0,
            coinbase_l2_status="ok",
            coinbase_l2_top_12_imbalance_notional=0.20,
        ),
        source_system="ultoim_v2",
    )

    assert d.bot_name == BOT_CONFIDENCE_TIER
    assert d.decision_status == ACCEPTED
    assert d.tier == "A"
    assert "V3_TIER_A_STRICT_7_HIGH_CONFIDENCE" in d.reason_codes
    assert not any("V3_TIER_B" in code for code in d.reason_codes)


def test_confidence_tier_a_missing_top12_is_research_only():
    d = confidence_tier_decision(
        _row(asset="BTC", ticker="KXBTC-1", predicted_side="YES", entry_ask_cents=80.0),
        source_system="ultoim_v2",
    )

    assert d.bot_name == BOT_CONFIDENCE_TIER
    assert d.decision_status == RESEARCH_ONLY
    assert d.tier == "A"
    assert "V3_TIER_A_RESEARCH_ONLY_TOP12_MISSING" in d.reason_codes


def test_confidence_tier_rejects_clear_coinbase_top12_contradiction(monkeypatch):
    monkeypatch.setenv("Q15_V3_COINBASE_TOP12_CONTRA_REJECT_ENABLED", "true")
    d = confidence_tier_decision(
        _row(
            asset="BTC",
            ticker="KXBTC-1",
            predicted_side="YES",
            entry_ask_cents=80.0,
            coinbase_l2_status="ok",
            coinbase_l2_top_12_imbalance_notional=-0.20,
        ),
        source_system="ultoim_v2",
    )

    assert d.bot_name == BOT_CONFIDENCE_TIER
    assert d.decision_status == REJECTED
    assert d.tier == "A"
    assert "V3_TIER_A_CONTRADICTED_BY_COINBASE_TOP12_NO" in d.reason_codes
    assert "V3_TIER_A_REJECTED_BY_COINBASE_TOP12_CONTRA" in d.reason_codes


def test_confidence_tier_marks_coinbase_top12_confirmation():
    d = confidence_tier_decision(
        _row(
            asset="BTC",
            ticker="KXBTC-1",
            predicted_side="YES",
            entry_ask_cents=80.0,
            coinbase_l2_status="ok",
            coinbase_l2_top_12_imbalance_notional=0.20,
        ),
        source_system="ultoim_v2",
    )

    assert d.decision_status == ACCEPTED
    assert d.tier == "A"
    assert "V3_TIER_A_CONFIRMED_COINBASE_TOP12_YES" in d.reason_codes


def test_confidence_tier_a_rejects_top250_contradiction():
    d = confidence_tier_decision(
        _row(
            asset="BTC",
            ticker="KXBTC-1",
            predicted_side="NO",
            entry_ask_cents=80.0,
            coinbase_l2_status="ok",
            coinbase_l2_top_12_imbalance_notional=-0.20,
            coinbase_l2_top_250_imbalance_notional=0.30,
        ),
        source_system="ultoim_v2",
    )

    assert d.decision_status == REJECTED
    assert d.tier == "A"
    assert "V3_TIER_A_CONFIRMED_COINBASE_TOP12_NO" in d.reason_codes
    assert "V3_TIER_A_VETO_COINBASE_TOP250_YES" in d.reason_codes
    assert "V3_TIER_A_REJECTED_BY_COMBINED_CONTRA" in d.reason_codes


def test_confidence_tier_a_rejects_spot_flow_and_kalshi_taker_contradiction():
    d = confidence_tier_decision(
        _row(
            asset="BTC",
            ticker="KXBTC-1",
            predicted_side="NO",
            entry_ask_cents=80.0,
            coinbase_l2_status="ok",
            coinbase_l2_top_12_imbalance_notional=-0.20,
            spot_depth_trade_net_notional_60s=1200.0,
            kalshi_taker_net_yes_volume_15s=300.0,
        ),
        source_system="ultoim_v2",
    )

    assert d.decision_status == REJECTED
    assert "V3_TIER_A_VETO_SPOT_FLOW_60S_YES" in d.reason_codes
    assert "V3_TIER_A_VETO_KALSHI_TAKER_15S_YES" in d.reason_codes
    assert "V3_TIER_A_REJECTED_BY_COMBINED_CONTRA" in d.reason_codes


def test_confidence_tier_a_warns_on_kraken_l3_contradiction_by_default():
    d = confidence_tier_decision(
        _row(
            asset="BTC",
            ticker="KXBTC-1",
            predicted_side="NO",
            entry_ask_cents=80.0,
            coinbase_l2_status="ok",
            coinbase_l2_top_12_imbalance_notional=-0.20,
            kraken_l3_depth_imbalance=0.25,
        ),
        source_system="ultoim_v2",
    )

    assert d.decision_status == ACCEPTED
    assert "V3_TIER_A_WARN_KRAKEN_L3_YES" in d.reason_codes
    assert "V3_TIER_A_REJECTED_BY_COMBINED_CONTRA" not in d.reason_codes


def test_confidence_tier_a_can_hard_veto_kraken_l3_contradiction(monkeypatch):
    monkeypatch.setenv("Q15_V3_KRAKEN_L3_HARD_VETO_ENABLED", "true")
    d = confidence_tier_decision(
        _row(
            asset="BTC",
            ticker="KXBTC-1",
            predicted_side="NO",
            entry_ask_cents=80.0,
            coinbase_l2_status="ok",
            coinbase_l2_top_12_imbalance_notional=-0.20,
            kraken_l3_depth_imbalance=0.25,
        ),
        source_system="ultoim_v2",
    )

    assert d.decision_status == REJECTED
    assert "V3_TIER_A_VETO_KRAKEN_L3_YES" in d.reason_codes
    assert "V3_TIER_A_REJECTED_BY_COMBINED_CONTRA" in d.reason_codes


def test_confidence_tier_b_volume_expansion():
    d = confidence_tier_decision(
        _row(
            asset="BTC",
            ticker="KXBTC-1",
            predicted_side="NO",
            entry_ask_cents=63.0,
            coinbase_l2_status="ok",
            coinbase_l2_top_12_imbalance_notional=-0.20,
        ),
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


def test_confidence_tier_c_top12_contradiction_stays_research_only():
    d = confidence_tier_decision(
        _row(
            asset="BNB",
            ticker="KXBNB-1",
            predicted_side="NO",
            entry_ask_cents=78.0,
            reason_codes="EXPENSIVE_NO_ADMIT",
            coinbase_l2_status="ok",
            coinbase_l2_top_12_imbalance_notional=0.25,
        ),
        source_system="ultoim_v2",
    )

    assert d.decision_status == RESEARCH_ONLY
    assert d.tier == "C"
    assert "V3_TIER_C_CONTRADICTED_BY_COINBASE_TOP12_YES" in d.reason_codes
    assert "V3_TIER_C_RESEARCH_ONLY_TOP12_CONTRA" in d.reason_codes


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
        coinbase_l2_status="ok",
        coinbase_l2_top_12_imbalance_notional=0.25,
        coinbase_l2_top_60_imbalance_notional=0.10,
        coinbase_l2_top_250_imbalance_notional=0.05,
        coinbase_l2_distance_to_target_bps=12.5,
        kraken_l3_status="ok",
        kraken_l3_depth_imbalance=0.04,
        kraken_l3_cancel_to_add_15s=0.5,
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
    tier_system = sb["tier_confirmation_system"]["tier_a"]
    assert tier_system["accepted_confirmed"]["rows"] == 1
    assert tier_system["rejected_vetoed"]["rows"] == 0
    coverage = sb["data_coverage"]["by_source_asset_tier"]["ultoim_v2|BTC|A"]
    assert coverage["counts"]["entry_ask"] == 1
    assert coverage["counts"]["kalshi_depth"] == 1
    assert coverage["counts"]["kalshi_taker_flow"] == 1
    assert coverage["counts"]["spot_depth"] == 1
    assert coverage["counts"]["spot_trade_flow_15s"] == 1
    assert coverage["counts"]["spot_trade_flow_60s"] == 1
    assert coverage["counts"]["coinbase_l2_ok"] == 1
    assert coverage["counts"]["coinbase_l2_top12"] == 1
    assert coverage["counts"]["coinbase_l2_top60"] == 1
    assert coverage["counts"]["coinbase_l2_top250"] == 1
    assert coverage["counts"]["coinbase_l2_depth_to_target"] == 1
    assert coverage["counts"]["kraken_l3_ok"] == 1
    assert coverage["counts"]["kraken_l3_depth"] == 1
    assert coverage["counts"]["kraken_l3_book_churn"] == 1
    assert coverage["counts"]["settlement"] == 1


def test_scoreboard_includes_positive_ev_gate_breakouts(tmp_path):
    led = StrategyBotLedger(tmp_path / "v3.sqlite3")
    blocked_row = _row(
        asset="HYPE",
        ticker="KXHYPE-GATE",
        predicted_side="YES",
        spot_depth_trade_net_qty_60s=45.0,
        yes_ask_depth_contracts=320.0,
        kalshi_taker_net_yes_volume_15s=None,
    )
    blocked = hype_yes_confirmation_decision(blocked_row)
    allowed_row = _row(
        asset="SOL",
        ticker="KXSOL-GATE",
        predicted_side=None,
        predicted_outcome="YES",
        rule_code="HVF_OWN_STRONG_SELECTED",
        record_kind="HIGH_VOL_FLIP_ALERT",
        spot_depth_trade_net_notional_60s=200.0,
        kalshi_taker_net_yes_volume_15s=0.0,
    )
    allowed = hvf_depth_flow_wrapper_decision(
        allowed_row,
        source_system="high_vol_flip",
    )

    assert blocked is not None
    assert blocked.decision_status == RESEARCH_ONLY
    assert allowed is not None
    assert allowed.decision_status == ACCEPTED
    assert led.record_decision(blocked, blocked_row, source_system="ultoim_v2") is not None
    assert led.record_decision(allowed, allowed_row, source_system="high_vol_flip") is not None
    assert led.resolve(
        source_system="ultoim_v2",
        source_model_version="ultoim-v2",
        ticker="KXHYPE-GATE",
        official_result="YES",
        now=1600.0,
    ) == 1
    assert led.resolve(
        source_system="high_vol_flip",
        source_model_version="ultoim-v2",
        ticker="KXSOL-GATE",
        official_result="YES",
        now=1600.0,
    ) == 1

    gate = led.scoreboard(STRATEGY_VERSION, min_n=1)["positive_ev_gate"]
    assert gate["all"]["rows"] == 2
    assert gate["research_blocks"]["rows"] == 1
    assert gate["allowed_candidates"]["rows"] == 1
    assert gate["by_bot_rule_status"]["hype_yes_confirmation|TEST|RESEARCH_ONLY"]["rows"] == 1
    assert (
        gate["by_bot_rule_status"]
        ["hvf_depth_flow_wrapper|HVF_OWN_STRONG_SELECTED|ACCEPTED"]["rows"]
        == 1
    )


def test_tier_confirmation_scoreboard_tracks_saved_losses_and_skipped_winners(tmp_path):
    led = StrategyBotLedger(tmp_path / "v3.sqlite3")
    saved_loss = _row(
        asset="BTC",
        ticker="KXBTC-SAVED",
        predicted_side="YES",
        entry_ask_cents=80.0,
        coinbase_l2_status="ok",
        coinbase_l2_top_12_imbalance_notional=-0.20,
    )
    skipped_winner = _row(
        asset="BTC",
        ticker="KXBTC-SKIPPED",
        predicted_side="YES",
        entry_ask_cents=80.0,
        coinbase_l2_status="ok",
        coinbase_l2_top_12_imbalance_notional=0.20,
        coinbase_l2_top_250_imbalance_notional=-0.20,
    )
    for row in (saved_loss, skipped_winner):
        decision = confidence_tier_decision(row, source_system="ultoim_v2")
        assert decision.decision_status == REJECTED
        assert led.record_decision(decision, row, source_system="ultoim_v2") is not None

    assert led.resolve(
        source_system="ultoim_v2",
        source_model_version="ultoim-v2",
        ticker="KXBTC-SAVED",
        official_result="NO",
        now=1600.0,
    ) == 1
    assert led.resolve(
        source_system="ultoim_v2",
        source_model_version="ultoim-v2",
        ticker="KXBTC-SKIPPED",
        official_result="YES",
        now=1600.0,
    ) == 1

    tier_a = led.scoreboard(STRATEGY_VERSION, min_n=1)["tier_confirmation_system"]["tier_a"]
    assert tier_a["rejected_vetoed"]["rows"] == 2
    assert tier_a["rejected_by_top12"]["rows"] == 1
    assert tier_a["rejected_by_combined_contra"]["rows"] == 1
    assert tier_a["veto_saved_losses"]["rows"] == 1
    assert tier_a["veto_skipped_winners"]["rows"] == 1


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
    monkeypatch.setenv("Q15_V3_HYPE_YES_ACCEPT_ENABLED", "true")
    monkeypatch.setenv("Q15_V3_EMPIRICAL_DELIVERY_GUARD", "false")
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

    assert runtime.record_source_row(first, source_system="ultoim_v2") == 5
    assert runtime.record_source_row(second, source_system="ultoim_v2") == 5

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


def test_v3_hvf_wrapper_alert_is_labeled_as_active_depth_flow_pick():
    text = build_v3_alert({
        "asset": "SOL",
        "side": "YES",
        "interval": "10M",
        "bot_name": BOT_HVF_DEPTH_FLOW,
        "source_rule": "HVF_MORE_FIRE_STRICT",
        "ticker": "KXSOL-1",
        "entry_ask_cents": 65.0,
        "spread_cents": 2.0,
        "kalshi_taker_net_yes_volume_15s": -25.0,
        "spot_depth_trade_net_notional_60s": 200.0,
        "coinbase_l2_top_12_imbalance_notional": 0.12,
        "coinbase_l2_top_60_imbalance_notional": -0.03,
        "reason_codes": "HVF_WRAPPER_MOREFIRE_ACCEPT_NO_SPOT_TAKER_CONTRA",
    })

    assert "V3 HVF DEPTH/FLOW PICK" in text
    assert "HVF Depth/Flow Wrapper" in text
    assert "Coinbase L2:" in text
    assert "V3 FILTERED PICK" not in text
    assert "n/a" not in text


def test_15m_depth_formula_records_research_only_pass():
    row = _row(
        asset="XRP",
        ticker="KXXRP-15M-DEPTH",
        interval="15M",
        predicted_side="NO",
        entry_ask_cents=54.0,
        spread_cents=2.0,
        depth_contracts=40.0,
        no_bid_depth_contracts=16.0,
        no_ask_depth_contracts=40.0,
    )

    decision = depth_formula_15m_research_decision(row)

    assert decision is not None
    assert decision.bot_name == BOT_DEPTH_FORMULA_15M
    assert decision.decision_status == RESEARCH_ONLY
    assert "V3_15M_DEPTH_FORMULA_BID_ASK_RATIO_GTE_0_25" in decision.reason_codes
    assert decision.threshold_profile["selected_bid_to_ask_depth_ratio"] == 0.4
    assert decision.threshold_profile["passed"] is True


def test_15m_depth_formula_rejects_missing_bid_support():
    row = _row(
        asset="XRP",
        ticker="KXXRP-15M-DEPTH-MISSING",
        interval="15M",
        predicted_side="NO",
        entry_ask_cents=54.0,
        spread_cents=2.0,
        depth_contracts=40.0,
        no_bid_depth_contracts=None,
    )

    decision = depth_formula_15m_research_decision(row)

    assert decision is not None
    assert decision.decision_status == REJECTED
    assert "V3_15M_DEPTH_FORMULA_SELECTED_BID_DEPTH_MISSING" in decision.reason_codes
    assert decision.threshold_profile["passed"] is False


def test_v3_depth_formula_alert_is_labeled_research():
    text = build_v3_alert({
        "asset": "XRP",
        "side": "NO",
        "interval": "15M",
        "bot_name": BOT_DEPTH_FORMULA_15M,
        "source_rule": "DEPTH_FORMULA_RESEARCH",
        "ticker": "KXXRP-15M-DEPTH",
        "entry_ask_cents": 54.0,
        "spread_cents": 2.0,
        "depth_contracts": 40.0,
        "no_bid_depth_contracts": 16.0,
        "no_ask_depth_contracts": 40.0,
        "reason_codes": "V3_15M_DEPTH_FORMULA_RESEARCH_EVAL",
    })

    assert "V3 15M DEPTH FORMULA / RESEARCH" in text
    assert "15M Depth Formula Research" in text
    assert "NO bid / selected ask depth ratio 0.4" in text
    assert "Mode: research-only tracking" in text
    assert "n/a" not in text


def _thirteen_row(**over):
    base = _row(
        asset="BTC",
        ticker="KXBTC-13M",
        interval="13M",
        predicted_side="YES",
        calibrated_yes_probability=0.65,
        entry_ask_cents=55.0,
        spread_cents=2.0,
        flip_probability=20.0,
        spot_depth_trade_net_notional_60s=50.0,
        spot_depth_trade_net_notional_60s_abs_p70=100.0,
        manipulation_suspected=False,
    )
    base.update(over)
    return base


def _rti_path_row(**over):
    asset = str(over.pop("asset", "BTC")).upper()
    rule_version = over.pop("model_version", rti_path_13m_rule_version(asset))
    index_id = over.pop("rti_index_id", RTI_PATH_13M_INDEX_IDS.get(asset))
    ticker = over.pop("ticker", f"KX{asset}-RTI-13M")
    entry_ask = float(over.get("entry_ask_cents", 60.0))
    spread = float(over.get("spread_cents", 1.0))
    opposite_side = "NO"
    opposite_ask = 100.0 - entry_ask + spread
    base = _row(
        model_version=rule_version,
        asset=asset,
        ticker=ticker,
        interval="13M",
        window_key=5000,
        record_kind="RTI_PATH_13M_PROSPECTIVE",
        rule_code=rule_version,
        delivery_status="PAPER_PROSPECTIVE",
        predicted_side="YES",
        rti_index_id=index_id,
        rti_path_status="ok",
        rti_path_expected_count=61,
        rti_path_count=61,
        rti_path_complete=True,
        rti_path_max_receive_age_s=0.2,
        rti_decision_age_s=0.5,
        rti_strike=68000.0,
        rti_path_start_px=68001.0,
        rti_path_end_px=68010.0,
        rti_14m_side="YES",
        rti_side="YES",
        rti_same_side_14m=True,
        rti_path_persistence=0.90,
        rti_side_move=9.0,
        rti_side_move_bps=1.32,
        rti_timing_offset_s=0.5,
        rti_path_evaluation_delay_s=0.5,
        quote_age_seconds=0.4,
        entry_ask_cents=60.0,
        spread_cents=1.0,
        depth_contracts=25.0,
        rti_market_mid_probability=(entry_ask - spread / 2.0) / 100.0,
        rti_opposite_side=opposite_side,
        rti_opposite_ask_cents=opposite_ask,
        rti_opposite_depth_contracts=30.0,
        rti_risk_policy_version=RTI_POINT_IN_TIME_RISK_POLICY_VERSION,
        rti_reversal_risk_class="low",
        rti_reversal_risk_reason_codes=["NO_CROSSING_OR_DECELERATION"],
        rti_settlement_average_risk_class="low",
        rti_settlement_average_risk_reason_codes=[
            "MARGIN_ABOVE_FROZEN_RISK_BANDS"
        ],
        rti_path_regime_class="persistent",
        rti_market_agreement_class="confirms_55_plus",
        rti_risk_notification_eligible=False,
        rti_risk_historical_credit_allowed=False,
    )
    base.update(over)
    return base


def _rti_impulse_row(**over):
    asset = over.pop("asset", "BTC")
    base = _rti_path_row(
        asset=asset,
        rti_signed_distance_bps=1.47,
        rti_absolute_distance_bps=1.47,
        rti_path_range_bps=1.65,
        rti_path_realized_volatility_bps=1.10,
        rti_path_trend_efficiency=0.80,
        rti_path_first_half_side_move_bps=0.55,
        rti_path_second_half_side_move_bps=0.77,
        rti_path_acceleration_bps=0.22,
        rti_path_strike_crossings=0,
        rti_path_seconds_since_last_crossing=None,
        rti_expected_remaining_volatility_bps=3.97,
        rti_distance_to_remaining_volatility=0.37,
        rti_spot_snapshot_created_at=999.5,
        rti_spot_snapshot_age_s=0.5,
        rti_spot_book_age_s=0.2,
        spot_depth_status="ok",
        spot_depth_imbalance=0.25,
    )
    base.update(over)
    return base


def _rti_microstructure_v11_shadow(**over):
    asset = str(over.pop("asset", "BTC")).upper()
    probability = float(over.pop("yes_probability", 0.46))
    side = str(over.pop("side", "NO")).upper()
    base = {
        "available": True,
        "prospective": True,
        "prospective_after_close_time": 2000.0,
        "model_version": (
            f"rti-microstructure-paper-{micro_v11.DESIGN_ID}-abc123def456"
        ),
        "artifact_sha256": "a" * 64,
        "test_state_version": "q15-rti-untouched-test-state-v2",
        "test_state_sha256": "b" * 64,
        "test_metrics_sha256": "c" * 64,
        "untouched_test_status": (
            "PASSED_UNTOUCHED_TEST_PAPER_ARTIFACT_ONLY"
        ),
        "design_id": micro_v11.DESIGN_ID,
        "design_sha256": micro_v11.DESIGN_SHA256,
        "walk_forward_protocol_id": micro_v11.EVALUATION_PROTOCOL_ID,
        "walk_forward_protocol_sha256": (
            micro_v11.EVALUATION_PROTOCOL_SHA256
        ),
        "cohort": "BTC" if asset == "BTC" else "NON_BTC_TRANSFER",
        "yes_probability": probability,
        "market_yes_probability": 0.59,
        "max_abs_z_preclip": 2.1,
        "out_of_distribution": False,
        "paper_only": True,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
        "manual_activation_required": True,
        "historical_credit_allowed": False,
        "entry_recommendation": {
            "side": side,
            "win_probability": 1.0 - probability if side == "NO" else probability,
            "ask_cents": 41.0,
            "displayed_depth_contracts": 30.0,
            "simulated_fill_cents": 43.0,
            "fee_cents_per_contract": 1.8,
            "expected_value_cents_per_contract": 10.0,
            "simulation_contracts": 10,
            "slippage_cents_per_contract": 2.0,
            "fee_schedule_version": RTI_PATH_13M_FEE_SCHEDULE_VERSION,
            "execution_cost_model_version": RTI_EXECUTION_COST_MODEL_VERSION,
            "paper_only": True,
            "notification_eligible": False,
        },
    }
    base.update(over)
    return base


def _rti_delayed_confirm_row(**over):
    asset = str(over.pop("asset", "BTC")).upper()
    rule_version = rti_path_13m_rule_version(asset)
    base = _row(
        created_at=1050.4,
        model_version=rule_version,
        asset=asset,
        ticker=f"KX{asset}-RTI-DELAYED",
        interval="12M30S",
        window_key=5000,
        close_time=1800.0,
        record_kind="RTI_PATH_12M30_CONFIRM_PROSPECTIVE",
        rule_code=RTI_PATH_13M_DELAYED_CONFIRM_POLICY_VERSION,
        delivery_status="PAPER_RESEARCH_RECORD_ONLY",
        predicted_side="YES",
        rti_index_id=RTI_PATH_13M_INDEX_IDS[asset],
        rti_confirm_target_at=1050.0,
        rti_confirm_quote_captured_at=1050.1,
        rti_confirm_evaluated_at=1050.3,
        rti_confirm_recorded_at=1050.4,
        rti_confirm_timing_offset_s=0.1,
        rti_confirm_evaluation_delay_s=0.3,
        rti_confirm_storage_delay_s=0.4,
        rti_confirm_original_row_id=77,
        rti_confirm_original_strict_accepted=True,
        rti_confirm_original_side="YES",
        rti_confirm_original_end_px=68010.0,
        rti_confirm_side="YES",
        rti_confirm_end_px=68020.0,
        rti_confirm_continuation_bps=1.47,
        rti_confirm_signed_distance_bps=2.94,
        rti_confirm_path_status="ok",
        rti_confirm_path_missing_reason=None,
        rti_confirm_path_expected_count=31,
        rti_confirm_path_count=31,
        rti_confirm_path_complete=True,
        rti_confirm_path_missing_seconds=[],
        rti_confirm_path_max_receive_age_s=0.2,
        rti_confirm_path_decision_age_s=0.3,
        quote_age_seconds=0.2,
        quote_age_source="kalshi_ws_exact_sampler",
        entry_ask_cents=57.0,
        spread_cents=1.0,
        depth_contracts=25.0,
        kalshi_depth_status="ok",
        rti_opposite_side="NO",
        rti_opposite_ask_cents=43.0,
        rti_opposite_depth_contracts=22.0,
    )
    base.update(over)
    return base


def _rti_delayed_confirm_60s_row(**over):
    base = _rti_delayed_confirm_row()
    base.update({
        "created_at": 1080.4,
        "interval": "12M",
        "record_kind": "RTI_PATH_12M_CONFIRM_PROSPECTIVE",
        "rule_code": RTI_PATH_13M_DELAYED_CONFIRM_60S_POLICY_VERSION,
        "rti_confirm_target_at": 1080.0,
        "rti_confirm_quote_captured_at": 1080.1,
        "rti_confirm_evaluated_at": 1080.3,
        "rti_confirm_recorded_at": 1080.4,
        "rti_confirm_delay_seconds": 60.0,
        "rti_confirm_path_expected_count": 61,
        "rti_confirm_path_count": 61,
    })
    base.update(over)
    return base


def _rti_delayed_stability_90s_row(**over):
    base = _rti_delayed_confirm_row()
    base.update({
        "created_at": 1110.4,
        "interval": "11M30S",
        "record_kind": "RTI_PATH_11M30_STABILITY_PROSPECTIVE",
        "rule_code": RTI_PATH_13M_DELAYED_CONFIRM_90S_POLICY_VERSION,
        "rti_confirm_target_at": 1110.0,
        "rti_confirm_quote_captured_at": 1110.1,
        "rti_confirm_evaluated_at": 1110.3,
        "rti_confirm_recorded_at": 1110.4,
        "rti_confirm_delay_seconds": 90.0,
        "rti_confirm_path_expected_count": 91,
        "rti_confirm_path_count": 91,
    })
    base.update(over)
    return base


def test_rti_path_13m_frozen_gate_and_fail_closed_matrix():
    accepted = rti_path_13m_decision(_rti_path_row())
    assert accepted.bot_name == BOT_RTI_PATH_13M
    assert accepted.decision_status == ACCEPTED
    assert accepted.threshold_profile["rule_version"] == RTI_PATH_13M_RULE_VERSION
    assert accepted.threshold_profile["sim_contracts"] == 10
    assert accepted.threshold_profile["slippage_cents_per_contract"] == 2.0
    assert (
        accepted.threshold_profile["fee_schedule_version"]
        == RTI_PATH_13M_FEE_SCHEDULE_VERSION
    )

    for override, reason in (
        ({"rti_path_count": 60, "rti_path_complete": False}, "RTI_PATH_INCOMPLETE"),
        ({"rti_decision_age_s": 2.1}, "RTI_PATH_STALE"),
        ({"rti_timing_offset_s": 2.1}, "CAPTURE_NOT_EXACT_13M"),
        ({"rti_14m_side": "NO", "rti_same_side_14m": False}, "RTI_14M_SIDE_MISMATCH"),
        ({"rti_path_persistence": 0.79}, "RTI_PERSISTENCE_BELOW_80"),
        ({"rti_side_move": -0.01}, "RTI_MOMENTUM_NEGATIVE"),
        ({"quote_age_seconds": None}, "QUOTE_STALE_OR_MISSING"),
        ({"entry_ask_cents": 62.1}, "RTI_SIDE_ASK_ABOVE_62"),
        ({"spread_cents": -0.1}, "SPREAD_CROSSED"),
        ({"spread_cents": 1.6}, "SPREAD_ABOVE_1_5"),
    ):
        decision = rti_path_13m_decision(_rti_path_row(**override))
        assert decision.decision_status == REJECTED
        assert reason in decision.reason_codes


@pytest.mark.parametrize("asset", tuple(RTI_PATH_13M_INDEX_IDS))
def test_rti_path_13m_accepts_each_official_index_in_isolated_version(asset):
    decision = rti_path_13m_decision(_rti_path_row(asset=asset))
    assert decision.decision_status == ACCEPTED
    assert decision.threshold_profile["asset_cohort"] == asset
    assert decision.threshold_profile["index_required"] == RTI_PATH_13M_INDEX_IDS[asset]
    assert decision.threshold_profile["rule_version"] == RTI_PATH_13M_RULE_VERSIONS[asset]
    assert decision.threshold_profile["historically_validated"] is (asset == "BTC")
    assert decision.threshold_profile["prospective_transfer"] is (asset != "BTC")


def test_rti_path_preregistered_volume_challengers_compensate_for_wider_spread():
    strong = rti_path_13m_decision(_rti_path_row(
        spread_cents=2.0,
        rti_path_persistence=0.95,
        entry_ask_cents=60.0,
    ))
    assert strong.decision_status == REJECTED
    strong_books = strong.threshold_profile["challengers"]
    assert strong_books["strong_path_wide_v1"]["accepted"] is True
    assert strong_books["value_price_wide_v1"]["accepted"] is False
    assert strong_books["strong_path_wide_v1"]["notification_eligible"] is False

    value = rti_path_13m_decision(_rti_path_row(
        spread_cents=2.0,
        rti_path_persistence=0.85,
        entry_ask_cents=58.0,
    ))
    value_books = value.threshold_profile["challengers"]
    assert value_books["strong_path_wide_v1"]["accepted"] is False
    assert value_books["value_price_wide_v1"]["accepted"] is True

    stale = rti_path_13m_decision(_rti_path_row(
        spread_cents=2.0,
        rti_path_persistence=0.99,
        entry_ask_cents=55.0,
        quote_age_seconds=2.1,
    ))
    assert not any(
        challenger["accepted"]
        for challenger in stale.threshold_profile["challengers"].values()
    )


def test_rti_path_impulse_challenger_rejects_parked_thin_or_opposed_paths():
    accepted = rti_path_13m_decision(_rti_impulse_row())
    impulse = accepted.threshold_profile["challengers"][
        RTI_PATH_13M_IMPULSE_CHALLENGER_ID
    ]
    assert accepted.decision_status == ACCEPTED
    assert impulse["accepted"] is True
    assert impulse["notification_eligible"] is True
    assert impulse["criteria"]["policy_version"] == RTI_PATH_13M_IMPULSE_POLICY_VERSION
    assert impulse["criteria"]["historical_credit_allowed"] is False

    for override, failure in (
        ({"rti_side_move_bps": 0.49}, "MOVE_MIN_0_5_BPS"),
        ({"rti_signed_distance_bps": 0.99}, "DISTANCE_MIN_1_BPS"),
        ({"rti_path_trend_efficiency": 0.24}, "TREND_EFFICIENCY_MIN_0_25"),
        ({"rti_path_second_half_side_move_bps": -0.01}, "SECOND_HALF_NOT_FADING"),
        ({"rti_path_strike_crossings": 2}, "STRIKE_CROSSINGS_MAX_1"),
        ({"rti_path_strike_crossings": 1,
          "rti_path_seconds_since_last_crossing": 19.9}, "LAST_CROSSING_CLEAR_20S"),
        ({"depth_contracts": 9.99}, "DISPLAYED_DEPTH_SUPPORTS_10"),
        ({"spot_depth_imbalance": -0.251}, "SPOT_NOT_STRONGLY_OPPOSED"),
        ({"spot_depth_status": "missing"}, "SPOT_STATUS_OK"),
    ):
        candidate = rti_path_13m_decision(
            _rti_impulse_row(**override)
        ).threshold_profile["challengers"][RTI_PATH_13M_IMPULSE_CHALLENGER_ID]
        assert candidate["accepted"] is False
        assert failure in candidate["failures"]


def test_rti_path_countertrend_value_freezes_opposite_quote_without_notifying():
    decision = rti_path_13m_decision(_rti_path_row())
    candidate = decision.threshold_profile["challengers"][
        RTI_PATH_13M_COUNTERTREND_CHALLENGER_ID
    ]
    assert candidate["accepted"] is True
    assert candidate["side_override"] == "NO"
    assert candidate["entry_ask_cents"] == 41.0
    assert candidate["displayed_depth_contracts"] == 30.0
    assert candidate["notification_eligible"] is False
    assert candidate["criteria"]["policy_version"] == (
        RTI_PATH_13M_COUNTERTREND_POLICY_VERSION
    )
    assert candidate["criteria"]["historical_credit_allowed"] is False

    for override, failure in (
        ({"rti_opposite_ask_cents": 40.9}, "OPPOSITE_ASK_MIN_41"),
        ({"rti_opposite_ask_cents": 50.1}, "OPPOSITE_ASK_MAX_50"),
        ({"rti_opposite_depth_contracts": 9.9}, "OPPOSITE_DEPTH_SUPPORTS_10"),
        ({"spread_cents": 2.1}, "SPREAD_MAX_2"),
    ):
        challenger = rti_path_13m_decision(
            _rti_path_row(**override)
        ).threshold_profile["challengers"][RTI_PATH_13M_COUNTERTREND_CHALLENGER_ID]
        assert challenger["accepted"] is False
        assert failure in challenger["failures"]


def test_rti_probability_v2_is_prospective_value_only_and_silent():
    shadow = {
        "available": True,
        "prospective": True,
        "prospective_after_close_time": 900.0,
        "model_version": "rti-probability-shadow-v2-test",
        "artifact_sha256": "abc123",
        "cohort": "BTC",
        "market_yes_probability": 0.59,
        "raw_yes_probability": 0.47,
        "calibrated_yes_probability": 0.46,
        "entry_recommendation": {
            "side": "NO",
            "ask_cents": 41.0,
            "depth_contracts": 30.0,
            "depth_available": True,
            "win_probability": 0.54,
            "expected_value_cents_per_contract": 8.2,
            "fee_cents_per_contract": 1.7,
        },
    }
    decision = rti_path_13m_decision(
        _rti_path_row(rti_probability_shadow_v2=shadow)
    )
    candidate = decision.threshold_profile["challengers"][
        RTI_PATH_13M_PROBABILITY_V2_CHALLENGER_ID
    ]
    assert candidate["accepted"] is True
    assert candidate["side_override"] == "NO"
    assert candidate["entry_ask_cents"] == 41.0
    assert candidate["notification_eligible"] is False
    assert candidate["historical_credit_allowed"] is False
    assert candidate["criteria"]["policy_version"] == (
        RTI_PATH_13M_PROBABILITY_V2_POLICY_VERSION
    )

    for update, failure in (
        ({"prospective": False}, "MODEL_V2_PROSPECTIVE_AFTER_FREEZE"),
        ({"artifact_sha256": None}, "MODEL_V2_ARTIFACT_FINGERPRINTED"),
        (
            {"entry_recommendation": {
                **shadow["entry_recommendation"],
                "expected_value_cents_per_contract": 2.99,
            }},
            "MODEL_V2_EV_AFTER_COSTS_MIN_3C",
        ),
        (
            {"entry_recommendation": {
                **shadow["entry_recommendation"],
                "depth_available": False,
            }},
            "MODEL_V2_DEPTH_CAPTURED",
        ),
    ):
        rejected_shadow = {**shadow, **update}
        rejected = rti_path_13m_decision(
            _rti_path_row(rti_probability_shadow_v2=rejected_shadow)
        ).threshold_profile["challengers"][
            RTI_PATH_13M_PROBABILITY_V2_CHALLENGER_ID
        ]
        assert rejected["accepted"] is False
        assert failure in rejected["failures"]


def test_rti_probability_v3_requires_numerical_guards_and_rejects_ood():
    shadow = {
        "available": True,
        "prospective": True,
        "prospective_after_close_time": 2000.0,
        "model_version": "rti-probability-shadow-v3-test",
        "artifact_sha256": "v3abc123",
        "cohort": "BTC",
        "market_yes_probability": 0.59,
        "raw_yes_probability": 0.47,
        "calibrated_yes_probability": 0.46,
        "out_of_distribution": False,
        "standardization": {
            "max_abs_z_preclip": 2.2,
            "out_of_distribution": False,
        },
        "standardization_policy": {
            "min_std": 1e-8,
            "z_clip": 6.0,
            "max_abs_z_allowed": 8.0,
            "out_of_distribution_fails_entry": True,
        },
        "calibration_policy": {
            "monotone_slope_required": True,
            "min_slope": 0.25,
        },
        "entry_recommendation": {
            "side": "NO",
            "ask_cents": 41.0,
            "depth_contracts": 30.0,
            "depth_available": True,
            "win_probability": 0.54,
            "expected_value_cents_per_contract": 8.2,
            "fee_cents_per_contract": 1.7,
        },
    }
    decision = rti_path_13m_decision(
        _rti_path_row(rti_probability_shadow_v3=shadow)
    )
    candidate = decision.threshold_profile["challengers"][
        RTI_PATH_13M_PROBABILITY_V3_CHALLENGER_ID
    ]
    assert candidate["accepted"] is True
    assert candidate["side_override"] == "NO"
    assert candidate["entry_ask_cents"] == 41.0
    assert candidate["notification_eligible"] is False
    assert candidate["historical_credit_allowed"] is False
    assert candidate["criteria"]["policy_version"] == (
        RTI_PATH_13M_PROBABILITY_V3_POLICY_VERSION
    )
    assert candidate["criteria"]["out_of_distribution_fails_entry"] is True

    ood = {
        **shadow,
        "out_of_distribution": True,
        "standardization": {
            **shadow["standardization"],
            "max_abs_z_preclip": 9.0,
            "out_of_distribution": True,
        },
    }
    rejected = rti_path_13m_decision(
        _rti_path_row(rti_probability_shadow_v3=ood)
    ).threshold_profile["challengers"][
        RTI_PATH_13M_PROBABILITY_V3_CHALLENGER_ID
    ]
    assert rejected["accepted"] is False
    assert "MODEL_V3_NOT_OUT_OF_DISTRIBUTION" in rejected["failures"]

    nonmonotone = {
        **shadow,
        "calibration_policy": {"monotone_slope_required": False},
    }
    rejected = rti_path_13m_decision(
        _rti_path_row(rti_probability_shadow_v3=nonmonotone)
    ).threshold_profile["challengers"][
        RTI_PATH_13M_PROBABILITY_V3_CHALLENGER_ID
    ]
    assert rejected["accepted"] is False
    assert "MODEL_V3_MONOTONE_CALIBRATION" in rejected["failures"]


def test_rti_microstructure_v11_requires_full_locked_lineage_and_stays_silent():
    shadow = _rti_microstructure_v11_shadow()
    decision = rti_path_13m_decision(_rti_path_row(
        close_time=2500.0,
        rti_microstructure_shadow_v11=shadow,
    ))
    candidate = decision.threshold_profile["challengers"][
        RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID
    ]
    assert candidate["accepted"] is True
    assert candidate["side_override"] == "NO"
    assert candidate["entry_ask_cents"] == 41.0
    assert candidate["notification_eligible"] is False
    assert candidate["automatic_promotion"] is False
    assert candidate["real_trading_allowed"] is False
    assert candidate["historical_credit_allowed"] is False
    assert candidate["manual_promotion_only"] is True
    assert candidate["criteria"]["policy_version"] == (
        RTI_PATH_13M_MICROSTRUCTURE_V11_POLICY_VERSION
    )
    assert candidate["evidence"]["test_state_sha256"] == "b" * 64

    for update, failure in (
        ({"prospective_after_close_time": 2500.0}, "MODEL_V11_PROSPECTIVE_AFTER_LOCK"),
        ({"test_state_sha256": "bad"}, "MODEL_V11_TEST_STATE_FINGERPRINTED"),
        ({"untouched_test_status": "REJECTED_ON_UNTOUCHED_TEST"}, "MODEL_V11_TEST_STATE_FINALIZED"),
        ({"design_sha256": "0" * 64}, "MODEL_V11_DESIGN_BOUND"),
        ({"walk_forward_protocol_sha256": "0" * 64}, "MODEL_V11_PROTOCOL_BOUND"),
        ({"out_of_distribution": True}, "MODEL_V11_NOT_OUT_OF_DISTRIBUTION"),
        ({"notification_eligible": True}, "MODEL_V11_NOTIFICATION_DISABLED"),
    ):
        rejected = rti_path_13m_decision(_rti_path_row(
            close_time=2500.0,
            rti_microstructure_shadow_v11={**shadow, **update},
        )).threshold_profile["challengers"][
            RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID
        ]
        assert rejected["accepted"] is False
        assert failure in rejected["failures"]


def test_rti_delayed_confirmation_uses_new_quote_and_fails_closed():
    row = _rti_delayed_confirm_row(entry_ask_cents=57.0)
    decision = rti_path_12m30_confirmation_decision(row)
    challenger = decision.threshold_profile["challengers"][
        RTI_PATH_13M_DELAYED_CONFIRM_CHALLENGER_ID
    ]
    assert decision.decision_status == ACCEPTED
    assert decision.entry_ask_cents == 57.0
    assert challenger["entry_ask_cents"] == 57.0
    assert challenger["accepted"] is True
    assert challenger["notification_eligible"] is False
    assert challenger["criteria"]["policy_version"] == (
        RTI_PATH_13M_DELAYED_CONFIRM_POLICY_VERSION
    )
    assert challenger["criteria"]["reused_13m_entry_quote_forbidden"] is True

    for override, failure in (
        ({"rti_confirm_original_strict_accepted": False},
         "ORIGINAL_STRICT_CONTROL_ACCEPTED"),
        ({"rti_confirm_side": "NO"}, "RTI_SIDE_STILL_CONFIRMS"),
        ({"rti_confirm_timing_offset_s": 2.01}, "CAPTURE_WITHIN_2S"),
        ({"rti_confirm_evaluation_delay_s": 2.01}, "EVALUATION_WITHIN_2S"),
        ({"quote_age_seconds": 2.01}, "NEW_QUOTE_FRESH"),
        ({"entry_ask_cents": 62.01}, "NEW_ASK_MAX_62"),
        ({"spread_cents": 1.51}, "NEW_SPREAD_MAX_1_5"),
        ({"depth_contracts": 9.99}, "NEW_DEPTH_SUPPORTS_10"),
        ({"rti_confirm_path_count": 30}, "CONFIRM_PATH_31_FRESH"),
    ):
        rejected = rti_path_12m30_confirmation_decision(
            _rti_delayed_confirm_row(**override)
        )
        book = rejected.threshold_profile["challengers"][
            RTI_PATH_13M_DELAYED_CONFIRM_CHALLENGER_ID
        ]
        assert rejected.decision_status == REJECTED
        assert book["accepted"] is False
        assert failure in book["failures"]


def test_rti_delayed_60s_confirmation_is_independent_and_fails_closed():
    row = _rti_delayed_confirm_60s_row(entry_ask_cents=55.0)
    decision = rti_path_12m_confirmation_decision(row)
    challenger = decision.threshold_profile["challengers"][
        RTI_PATH_13M_DELAYED_CONFIRM_60S_CHALLENGER_ID
    ]
    assert decision.decision_status == ACCEPTED
    assert decision.entry_ask_cents == 55.0
    assert challenger["accepted"] is True
    assert challenger["notification_eligible"] is False
    assert challenger["criteria"]["delay_seconds"] == 60.0
    assert challenger["criteria"]["policy_version"] == (
        RTI_PATH_13M_DELAYED_CONFIRM_60S_POLICY_VERSION
    )

    rejected = rti_path_12m_confirmation_decision(
        _rti_delayed_confirm_60s_row(rti_confirm_path_count=60)
    )
    rejected_book = rejected.threshold_profile["challengers"][
        RTI_PATH_13M_DELAYED_CONFIRM_60S_CHALLENGER_ID
    ]
    assert rejected.decision_status == REJECTED
    assert rejected_book["accepted"] is False
    assert "CONFIRM_PATH_61_FRESH" in rejected_book["failures"]


def test_rti_delayed_90s_stability_is_fresh_silent_and_post_loss_selected():
    decision = rti_path_11m30_stability_decision(
        _rti_delayed_stability_90s_row(entry_ask_cents=54.0)
    )
    challenger = decision.threshold_profile["challengers"][
        RTI_PATH_13M_DELAYED_CONFIRM_90S_CHALLENGER_ID
    ]
    assert decision.decision_status == ACCEPTED
    assert decision.entry_ask_cents == 54.0
    assert challenger["accepted"] is True
    assert challenger["notification_eligible"] is False
    assert challenger["historical_credit_allowed"] is False
    assert challenger["criteria"]["delay_seconds"] == 90.0
    assert challenger["criteria"]["selected_after_forward_60s_loss_review"] is True
    assert challenger["criteria"][
        "reused_13m_30s_or_60s_quote_forbidden"
    ] is True

    rejected = rti_path_11m30_stability_decision(
        _rti_delayed_stability_90s_row(rti_confirm_path_count=90)
    )
    rejected_book = rejected.threshold_profile["challengers"][
        RTI_PATH_13M_DELAYED_CONFIRM_90S_CHALLENGER_ID
    ]
    assert rejected.decision_status == REJECTED
    assert "CONFIRM_PATH_91_FRESH" in rejected_book["failures"]


def test_rti_delayed_60s_hard_flip_uses_new_opposite_quote_and_is_silent():
    decision = rti_path_12m_confirmation_decision(
        _rti_delayed_confirm_60s_row(
            rti_confirm_side="NO",
            rti_opposite_side="NO",
            rti_opposite_ask_cents=44.0,
            rti_opposite_depth_contracts=20.0,
        )
    )
    continuation = decision.threshold_profile["challengers"][
        RTI_PATH_13M_DELAYED_CONFIRM_60S_CHALLENGER_ID
    ]
    flip = decision.threshold_profile["challengers"][
        RTI_PATH_13M_DELAYED_FLIP_60S_CHALLENGER_ID
    ]
    assert decision.decision_status == REJECTED
    assert continuation["accepted"] is False
    assert flip["accepted"] is True
    assert flip["side_override"] == "NO"
    assert flip["entry_ask_cents"] == 44.0
    assert flip["notification_eligible"] is False
    assert flip["historical_credit_allowed"] is False
    assert flip["criteria"]["reused_13m_or_30s_quote_forbidden"] is True
    assert flip["criteria"]["policy_version"] == (
        RTI_PATH_13M_DELAYED_FLIP_60S_POLICY_VERSION
    )

    for overrides, failure in (
        ({"rti_confirm_side": "YES"}, "RTI_SIDE_FLIPPED_AT_60S"),
        ({"rti_opposite_side": "YES"}, "FLIP_QUOTE_SIDE_MATCHES_RTI"),
        ({"rti_opposite_ask_cents": 62.01}, "FLIP_ASK_MAX_62"),
        ({"rti_opposite_depth_contracts": 9.99}, "FLIP_DEPTH_SUPPORTS_10"),
    ):
        candidate = rti_path_12m_confirmation_decision(
            _rti_delayed_confirm_60s_row(
                **{"rti_confirm_side": "NO", **overrides},
            )
        ).threshold_profile["challengers"][
            RTI_PATH_13M_DELAYED_FLIP_60S_CHALLENGER_ID
        ]
        assert candidate["accepted"] is False
        assert failure in candidate["failures"]


def test_rti_path_spot_book_confirm_is_isolated_shadow_and_fails_closed():
    aligned = rti_path_13m_decision(_rti_path_row(
        rti_spot_snapshot_created_at=999.0,
        rti_spot_snapshot_age_s=1.0,
        rti_spot_book_age_s=0.2,
        spot_depth_status="ok",
        spot_depth_imbalance=0.25,
    ))
    shadow = aligned.threshold_profile["challengers"][
        RTI_PATH_13M_SPOT_CONFIRM_CHALLENGER_ID
    ]
    assert aligned.decision_status == ACCEPTED
    assert shadow["accepted"] is True
    assert shadow["notification_eligible"] is False
    assert shadow["criteria"]["policy_version"] == RTI_PATH_13M_SPOT_CONFIRM_POLICY_VERSION
    assert shadow["promotion_status"] == "ACCRUING_TO_30"

    for override, failure in (
        ({"rti_spot_snapshot_age_s": -0.01}, "SPOT_SNAPSHOT_AS_OF_DECISION"),
        ({"rti_spot_snapshot_age_s": 3.01}, "SPOT_SNAPSHOT_FRESH_3S"),
        ({"rti_spot_book_age_s": 2.01}, "SPOT_BOOK_FRESH_2S"),
        ({"rti_spot_book_age_s": -3.01}, "SPOT_BOOK_FRESH_2S"),
        ({"spot_depth_imbalance": -0.25}, "SPOT_BOOK_ALIGNS_RTI"),
        ({"spot_depth_imbalance": None}, "SPOT_IMBALANCE_AVAILABLE"),
    ):
        evidence = {
            "rti_spot_snapshot_created_at": 999.0,
            "rti_spot_snapshot_age_s": 1.0,
            "rti_spot_book_age_s": 0.2,
            "spot_depth_status": "ok",
            "spot_depth_imbalance": 0.25,
        }
        evidence.update(override)
        row = _rti_path_row(
            **evidence,
        )
        candidate = rti_path_13m_decision(row).threshold_profile["challengers"][
            RTI_PATH_13M_SPOT_CONFIRM_CHALLENGER_ID
        ]
        assert candidate["accepted"] is False
        assert failure in candidate["failures"]


def test_rti_path_challenger_scoreboard_uses_frozen_point_in_time_verdicts(tmp_path):
    led = StrategyBotLedger(str(tmp_path / "rti-challengers.sqlite3"))
    try:
        strong_row = _rti_path_row(
            ticker="KXBTC-STRONG-WIDE",
            spread_cents=2.0,
            rti_path_persistence=0.95,
            entry_ask_cents=60.0,
        )
        value_row = _rti_path_row(
            ticker="KXBTC-VALUE-WIDE",
            spread_cents=2.0,
            rti_path_persistence=0.85,
            entry_ask_cents=58.0,
        )
        impulse_row = _rti_impulse_row(ticker="KXBTC-IMPULSE")
        for row in (strong_row, value_row, impulse_row):
            decision = rti_path_13m_decision(row)
            assert led.record_decision(
                decision, row, source_system="rti_path_13m"
            ) is not None

        research = led.scoreboard(min_n=1)["rti_path_challengers"]
        assert research["notification_eligible"] is True
        assert research["review"]["manual_promotion_only"] is True
        assert research["books"]["strong_path_wide_v1"]["overall"]["rows"] == 2
        assert research["books"]["value_price_wide_v1"]["overall"]["rows"] == 1
        assert research["books"][RTI_PATH_13M_SPOT_CONFIRM_CHALLENGER_ID][
            "overall"
        ]["rows"] == 1
        assert research["books"][RTI_PATH_13M_SPOT_CONFIRM_CHALLENGER_ID][
            "notification_eligible"
        ] is False
        assert research["books"][RTI_PATH_13M_IMPULSE_CHALLENGER_ID][
            "overall"
        ]["rows"] == 1
        assert research["books"][RTI_PATH_13M_IMPULSE_CHALLENGER_ID][
            "notification_eligible"
        ] is True
        assert research["books"][RTI_PATH_13M_IMPULSE_CHALLENGER_ID][
            "policy_version"
        ] == RTI_PATH_13M_IMPULSE_POLICY_VERSION
        assert research["books"][RTI_PATH_13M_IMPULSE_CHALLENGER_ID][
            "evaluated"
        ] == 3
        assert research["books"][RTI_PATH_13M_IMPULSE_CHALLENGER_ID][
            "qualified"
        ] == 1
        assert research["books"][RTI_PATH_13M_COUNTERTREND_CHALLENGER_ID][
            "overall"
        ]["rows"] == 3
        assert research["books"][RTI_PATH_13M_COUNTERTREND_CHALLENGER_ID][
            "notification_eligible"
        ] is False
        compact = led.rti_path_challenger_scoreboard(min_n=30)
        assert compact["books"]["strong_path_wide_v1"]["overall"]["rows"] == 2
        assert compact["books"][RTI_PATH_13M_SPOT_CONFIRM_CHALLENGER_ID][
            "overall"
        ]["rows"] == 1
        assert compact["books"][RTI_PATH_13M_IMPULSE_CHALLENGER_ID][
            "overall"
        ]["rows"] == 1
        risk = compact["point_in_time_risk_diagnostics"]
        assert risk["historical_credit_allowed"] is False
        assert risk["notification_eligible"] is False
        assert risk["policy_versions"] == [
            RTI_POINT_IN_TIME_RISK_POLICY_VERSION
        ]
        assert risk["labeled_exact_rows"] == 3
        assert risk["strict_accepted_labeled_rows"] == 1
        assert risk["all_exact_rows"]["by_reversal_risk"]["low"][
            "rows"
        ] == 3
        assert risk["strict_control_rows"]["by_settlement_average_risk"][
            "low"
        ]["rows"] == 1
        assert compact["review"]["manual_promotion_only"] is True
    finally:
        led.close()


def test_rti_scoreboard_cache_is_copy_safe_and_invalidates_on_insert(
    tmp_path, monkeypatch,
):
    led = StrategyBotLedger(str(tmp_path / "rti-scoreboard-cache.sqlite3"))
    calls = []

    def fake_system(rows, min_n, **_kwargs):
        calls.append((len(rows), min_n))
        return {"marker": [], "row_count": len(rows)}

    monkeypatch.setattr(led, "_rti_path_challenger_system", fake_system)
    try:
        first = led.rti_path_challenger_scoreboard(min_n=30)
        first["marker"].append("caller-mutation")
        second = led.rti_path_challenger_scoreboard(min_n=30)
        assert second == {"marker": [], "row_count": 0}
        assert calls == [(0, 30)]

        row = _rti_path_row(ticker="KXBTC-CACHE-INVALIDATE")
        decision = rti_path_13m_decision(row)
        assert led.record_decision(
            decision, row, source_system="rti_path_13m"
        ) is not None
        third = led.rti_path_challenger_scoreboard(min_n=30)
        assert third["row_count"] == 1
        assert calls == [(0, 30), (1, 30)]
    finally:
        led.close()


def test_rti_countertrend_scoreboard_grades_opposite_side_and_quote(tmp_path):
    led = StrategyBotLedger(str(tmp_path / "rti-countertrend.sqlite3"))
    try:
        row = _rti_path_row(ticker="KXBTC-COUNTERTREND")
        decision = rti_path_13m_decision(row)
        assert led.record_decision(
            decision, row, source_system="rti_path_13m"
        ) is not None
        assert led.resolve(
            source_system="rti_path_13m",
            source_model_version=RTI_PATH_13M_RULE_VERSION,
            ticker=row["ticker"],
            official_result="NO",
            now=2000.0,
        ) == 1
        book = led.rti_path_challenger_scoreboard(min_n=1)["books"][
            RTI_PATH_13M_COUNTERTREND_CHALLENGER_ID
        ]["overall"]
        assert book["resolved"] == 1
        assert book["correct"] == 1
        assert book["accuracy"] == 1.0
        assert book["fee_adjusted_net_pnl_cents"] > 0.0
    finally:
        led.close()


def test_rti_probability_v2_scoreboard_grades_selected_side_and_frozen_quote(
    tmp_path,
):
    led = StrategyBotLedger(str(tmp_path / "rti-probability-v2.sqlite3"))
    try:
        shadow = {
            "available": True,
            "prospective": True,
            "prospective_after_close_time": 900.0,
            "model_version": "rti-probability-shadow-v2-test",
            "artifact_sha256": "abc123",
            "cohort": "BTC",
            "market_yes_probability": 0.59,
            "raw_yes_probability": 0.47,
            "calibrated_yes_probability": 0.46,
            "entry_recommendation": {
                "side": "NO",
                "ask_cents": 41.0,
                "depth_contracts": 30.0,
                "depth_available": True,
                "win_probability": 0.54,
                "expected_value_cents_per_contract": 8.2,
                "fee_cents_per_contract": 1.7,
            },
        }
        row = _rti_path_row(
            ticker="KXBTC-PROBABILITY-V2",
            rti_probability_shadow_v2=shadow,
        )
        decision = rti_path_13m_decision(row)
        assert led.record_decision(
            decision, row, source_system="rti_path_13m"
        ) is not None
        assert led.resolve(
            source_system="rti_path_13m",
            source_model_version=RTI_PATH_13M_RULE_VERSION,
            ticker=row["ticker"],
            official_result="NO",
            now=2000.0,
        ) == 1
        details = led.rti_path_challenger_scoreboard(min_n=1)["books"][
            RTI_PATH_13M_PROBABILITY_V2_CHALLENGER_ID
        ]
        assert details["policy_version"] == (
            RTI_PATH_13M_PROBABILITY_V2_POLICY_VERSION
        )
        assert details["notification_eligible"] is False
        assert details["overall"]["resolved"] == 1
        assert details["overall"]["correct"] == 1
        assert details["overall"]["fee_adjusted_net_pnl_cents"] > 0.0
    finally:
        led.close()


def test_rti_probability_v3_scoreboard_is_separate_from_quarantined_v2(tmp_path):
    led = StrategyBotLedger(str(tmp_path / "rti-probability-v3.sqlite3"))
    try:
        shadow = {
            "available": True,
            "prospective": True,
            "prospective_after_close_time": 2000.0,
            "model_version": "rti-probability-shadow-v3-test",
            "artifact_sha256": "v3abc123",
            "cohort": "BTC",
            "market_yes_probability": 0.59,
            "raw_yes_probability": 0.47,
            "calibrated_yes_probability": 0.46,
            "out_of_distribution": False,
            "standardization": {
                "max_abs_z_preclip": 2.2,
                "out_of_distribution": False,
            },
            "standardization_policy": {
                "min_std": 1e-8,
                "z_clip": 6.0,
                "max_abs_z_allowed": 8.0,
            },
            "calibration_policy": {"monotone_slope_required": True},
            "entry_recommendation": {
                "side": "NO",
                "ask_cents": 41.0,
                "depth_contracts": 30.0,
                "depth_available": True,
                "win_probability": 0.54,
                "expected_value_cents_per_contract": 8.2,
                "fee_cents_per_contract": 1.7,
            },
        }
        row = _rti_path_row(
            ticker="KXBTC-PROBABILITY-V3",
            rti_probability_shadow_v3=shadow,
        )
        decision = rti_path_13m_decision(row)
        assert led.record_decision(
            decision, row, source_system="rti_path_13m"
        ) is not None
        assert led.resolve(
            source_system="rti_path_13m",
            source_model_version=RTI_PATH_13M_RULE_VERSION,
            ticker=row["ticker"],
            official_result="NO",
            now=3000.0,
        ) == 1
        books = led.rti_path_challenger_scoreboard(min_n=1)["books"]
        v3 = books[RTI_PATH_13M_PROBABILITY_V3_CHALLENGER_ID]
        assert v3["policy_version"] == RTI_PATH_13M_PROBABILITY_V3_POLICY_VERSION
        assert v3["notification_eligible"] is False
        assert v3["overall"]["resolved"] == 1
        assert v3["overall"]["correct"] == 1
        assert v3["overall"]["fee_adjusted_net_pnl_cents"] > 0.0
        assert books[RTI_PATH_13M_PROBABILITY_V2_CHALLENGER_ID][
            "overall"
        ]["resolved"] == 0
    finally:
        led.close()


def test_rti_probability_scorecards_use_every_point_in_time_prediction(tmp_path):
    led = StrategyBotLedger(str(tmp_path / "rti-probability-scorecards.sqlite3"))

    def _shadow(
        version: int,
        *,
        asset: str,
        probability: float,
        market_probability: float,
        cutoff: float,
        accepted: bool,
    ) -> dict:
        side = "YES" if probability >= 0.5 else "NO"
        shadow = {
            "available": True,
            "prospective": True,
            "prospective_after_close_time": cutoff,
            "model_version": f"rti-probability-shadow-v{version}-scorecard-test",
            "artifact_sha256": f"v{version}-scorecard-artifact",
            "cohort": "BTC" if asset == "BTC" else "NON_BTC_TRANSFER",
            "market_yes_probability": market_probability,
            "raw_yes_probability": probability,
            "calibrated_yes_probability": probability,
            "out_of_distribution": False,
            "entry_recommendation": {
                "side": side,
                "ask_cents": 40.0,
                "depth_contracts": 30.0,
                "depth_available": True,
                "win_probability": max(probability, 1.0 - probability),
                "expected_value_cents_per_contract": 8.0 if accepted else -1.0,
                "fee_cents_per_contract": 1.5,
            },
        }
        if version == 3:
            shadow.update({
                "standardization": {
                    "max_abs_z_preclip": 2.0,
                    "out_of_distribution": False,
                },
                "standardization_policy": {
                    "min_std": 1e-8,
                    "z_clip": 6.0,
                    "max_abs_z_allowed": 8.0,
                },
                "calibration_policy": {"monotone_slope_required": True},
            })
        return shadow

    cases = (
        ("BTC", 2000.0, 0.80, 0.70, 0.60, "YES", True),
        ("ETH", 2000.0, 0.20, 0.60, 0.60, "NO", False),
        # This deliberately bad prediction predates the stored freeze cutoff.
        ("SOL", 900.0, 0.99, 0.90, 0.10, "NO", True),
        # A prospective but unresolved prediction must not enter proper scores.
        ("XRP", 3000.0, 0.70, 0.65, 0.40, None, True),
    )
    try:
        for index, (
            asset,
            close_time,
            v3_probability,
            market_probability,
            v2_probability,
            result,
            accepted,
        ) in enumerate(cases):
            ticker = f"KX{asset}-PROB-SCORE-{index}"
            row = _rti_path_row(
                asset=asset,
                ticker=ticker,
                close_time=close_time,
                window_key=100 + index,
                rti_probability_shadow_v2=_shadow(
                    2,
                    asset=asset,
                    probability=v2_probability,
                    market_probability=market_probability,
                    cutoff=1000.0,
                    accepted=accepted,
                ),
                rti_probability_shadow_v3=_shadow(
                    3,
                    asset=asset,
                    probability=v3_probability,
                    market_probability=market_probability,
                    cutoff=1000.0,
                    accepted=accepted,
                ),
            )
            assert led.record_decision(
                rti_path_13m_decision(row),
                row,
                source_system="rti_path_13m",
            ) is not None
            if result is not None:
                assert led.resolve(
                    source_system="rti_path_13m",
                    source_model_version=rti_path_13m_rule_version(asset),
                    ticker=ticker,
                    official_result=result,
                    now=4000.0 + index,
                ) == 1

        scorecards = led.rti_path_challenger_scoreboard(min_n=30)[
            "probability_scorecards"
        ]
        v3 = scorecards[RTI_PATH_13M_PROBABILITY_V3_CHALLENGER_ID]
        assert v3["evaluated_evidence_rows"] == 4
        assert v3["scoreable_resolved_rows"] == 2
        assert v3["accepted_rows_scored"] == 1
        assert v3["rejected_rows_scored"] == 1
        assert v3["excluded"] == {
            "pre_or_at_freeze_cutoff": 1,
            "unresolved": 1,
        }
        assert v3["overall"]["n"] == 2
        assert v3["overall"]["correct"] == 2
        assert v3["overall"]["accuracy"] == 1.0
        assert v3["overall"]["brier_score"] == pytest.approx(0.04)
        assert v3["overall"]["market_brier_score"] == pytest.approx(0.225)
        assert v3["overall"]["brier_skill_vs_market"] == pytest.approx(
            1.0 - (0.04 / 0.225)
        )
        assert v3["overall"]["market_accuracy"] == 0.5
        assert v3["overall"]["accuracy_delta_vs_market"] == 0.5
        assert v3["overall"]["close_windows"] == 1
        assert v3["overall"]["partial_close_windows"] == 1
        assert v3["by_transfer_cohort"]["BTC"]["n"] == 1
        assert v3["by_transfer_cohort"]["NON_BTC_TRANSFER"]["n"] == 1
        assert v3["by_asset"]["BTC"]["accuracy"] == 1.0
        assert v3["by_asset"]["ETH"]["accuracy"] == 1.0
        assert v3["evidence_integrity"]["single_model_version"] is True
        assert v3["evidence_integrity"]["single_artifact_sha256"] is True
        assert v3["promotion_prohibited"] is False

        v2 = scorecards[RTI_PATH_13M_PROBABILITY_V2_CHALLENGER_ID]
        assert v2["overall"]["n"] == 2
        assert v2["overall"]["accuracy"] == 0.5
        assert v2["promotion_prohibited"] is True
        assert v2["evidence_integrity"]["observed_model_versions"] == [
            "rti-probability-shadow-v2-scorecard-test"
        ]
    finally:
        led.close()


def _passing_v11_close_window_bootstrap(rows=30, close_windows=5):
    return {
        "available": True,
        "version": runtime.RTI_V11_BOOTSTRAP_VERSION,
        "cluster_key": runtime.RTI_V11_BOOTSTRAP_CLUSTER_KEY,
        "resamples": runtime.RTI_V11_BOOTSTRAP_RESAMPLES,
        "confidence_level": runtime.RTI_V11_BOOTSTRAP_CONFIDENCE_LEVEL,
        "random_seed": runtime.RTI_V11_BOOTSTRAP_RANDOM_SEED,
        "same_close_assets_resampled_together": True,
        "within_close_assets_equal_weighted": True,
        "close_windows_equal_weighted": True,
        "loss_delta_direction": "MODEL_MINUS_MARKET",
        "minimum_mean_brier_improvement": (
            runtime.RTI_V11_MIN_BRIER_IMPROVEMENT
        ),
        "minimum_mean_log_loss_improvement": (
            runtime.RTI_V11_MIN_LOG_LOSS_IMPROVEMENT
        ),
        "rows": rows,
        "close_windows": close_windows,
        "brier_delta": {
            "one_sided_upper": -0.01,
        },
        "log_loss_delta": {
            "one_sided_upper": -0.02,
        },
        "gate_met": True,
    }


def test_rti_probability_skill_gate_requires_both_proper_scores_and_pairs():
    passing = runtime._rti_probability_skill_gate({
        "n": 30,
        "market_n": 30,
        "brier_score": 0.20,
        "market_brier_score": 0.22,
        "brier_skill_vs_market": 1.0 - (0.20 / 0.22),
        "log_loss": 0.58,
        "market_log_loss": 0.61,
        "log_loss_delta_vs_market": -0.03,
    })
    assert passing["met"] is True
    assert passing["paired_complete"] is True
    assert passing["brier_improved"] is True
    assert passing["log_loss_improved"] is True

    for override in (
        {"n": 29, "market_n": 29},
        {"market_n": 29},
        {"brier_score": 0.23},
        {"log_loss": 0.62},
    ):
        values = {
            "n": 30,
            "market_n": 30,
            "brier_score": 0.20,
            "market_brier_score": 0.22,
            "log_loss": 0.58,
            "market_log_loss": 0.61,
        }
        values.update(override)
        assert runtime._rti_probability_skill_gate(values)["met"] is False


def test_rti_v11_probability_skill_gate_requires_locked_clustered_bounds():
    metrics = {
        "n": 30,
        "market_n": 30,
        "brier_score": 0.12,
        "market_brier_score": 0.20,
        "log_loss": 0.38,
        "market_log_loss": 0.55,
    }
    missing = runtime._rti_probability_skill_gate(
        metrics, require_clustered_uncertainty=True,
    )
    assert missing["brier_improved"] is True
    assert missing["log_loss_improved"] is True
    assert missing["clustered_uncertainty_required"] is True
    assert missing["clustered_uncertainty_met"] is False
    assert missing["met"] is False

    metrics["paired_close_window_bootstrap"] = (
        _passing_v11_close_window_bootstrap()
    )
    passing = runtime._rti_probability_skill_gate(
        metrics, require_clustered_uncertainty=True,
    )
    assert passing["clustered_uncertainty_met"] is True
    assert passing["met"] is True

    for key, value in (
        ("resamples", runtime.RTI_V11_BOOTSTRAP_RESAMPLES - 1),
        ("random_seed", runtime.RTI_V11_BOOTSTRAP_RANDOM_SEED + 1),
        ("minimum_mean_brier_improvement", 0.0),
    ):
        tampered = dict(metrics)
        tampered_bootstrap = dict(metrics["paired_close_window_bootstrap"])
        tampered_bootstrap[key] = value
        tampered["paired_close_window_bootstrap"] = tampered_bootstrap
        assert runtime._rti_probability_skill_gate(
            tampered, require_clustered_uncertainty=True,
        )["met"] is False


def test_rti_v11_close_window_bootstrap_is_deterministic_and_cluster_safe():
    assets = ("ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
    rows = []
    for window in range(8):
        label = float(window % 2)
        model_probability = 0.8 if label else 0.2
        market_probability = 0.6 if label else 0.4
        for asset in assets:
            rows.append({
                "asset": asset,
                "close_time": 1000.0 + (window * 900.0),
                "label_yes": label,
                "yes_probability": model_probability,
                "market_yes_probability": market_probability,
            })

    first = StrategyBotLedger._paired_probability_close_window_bootstrap(rows)
    second = StrategyBotLedger._paired_probability_close_window_bootstrap(rows)
    duplicated = StrategyBotLedger._paired_probability_close_window_bootstrap(
        [row for source in rows for row in (source, dict(source))]
    )
    assert first == second
    assert first["available"] is True
    assert first["rows"] == 48
    assert first["close_windows"] == 8
    assert first["gate_met"] is True
    assert duplicated["rows"] == 96
    assert duplicated["close_windows"] == 8
    assert duplicated["brier_delta"] == first["brier_delta"]
    assert duplicated["log_loss_delta"] == first["log_loss_delta"]


def test_rti_v11_close_window_bootstrap_rejects_tiny_or_zero_skill():
    zero_skill_rows = [{
        "asset": "BTC",
        "close_time": 1000.0 + (window * 900.0),
        "label_yes": 1.0,
        "yes_probability": 0.6,
        "market_yes_probability": 0.6,
    } for window in range(30)]
    zero_skill = StrategyBotLedger._paired_probability_close_window_bootstrap(
        zero_skill_rows
    )
    assert zero_skill["brier_delta"]["observed_mean_delta"] == 0.0
    assert zero_skill["gate_met"] is False

    tiny_skill_rows = [
        {**row, "yes_probability": 0.6001} for row in zero_skill_rows
    ]
    tiny_skill = StrategyBotLedger._paired_probability_close_window_bootstrap(
        tiny_skill_rows
    )
    assert tiny_skill["brier_delta"]["observed_mean_delta"] < 0.0
    assert tiny_skill["log_loss_delta"]["observed_mean_delta"] < 0.0
    assert tiny_skill["gate_met"] is False


def test_rti_exact_microstructure_is_durable_in_columns_and_profile(tmp_path):
    led = StrategyBotLedger(str(tmp_path / "rti-microstructure.sqlite3"))
    try:
        row = _rti_path_row(
            ticker="KXBTC-RTI-MICRO",
            record_kind="RTI_PATH_13M_PROSPECTIVE_EXACT",
            kalshi_yes_microprice_cents=59.4,
            kalshi_yes_microprice_edge_cents=-0.1,
            kalshi_microstructure_schema_version=(
                RTI_EXACT_MICROSTRUCTURE_SCHEMA_VERSION
            ),
            kalshi_microstructure_captured_at=1000.0,
            kalshi_microstructure_time_basis="local_received_at",
            kalshi_microstructure_extension_schema_version=(
                "rti-exact-microstructure-extension-v1"
            ),
            kalshi_history_count_capped=False,
            kalshi_book_event_retention_seconds=90.0,
            kalshi_trade_retention_seconds=1200.0,
            kalshi_book_history_started_at=880.0,
            kalshi_trade_history_started_at=880.0,
            kalshi_book_history_seconds=120.0,
            kalshi_trade_history_seconds=120.0,
            kalshi_book_window_complete_5s=True,
            kalshi_trade_window_complete_15s=True,
            kalshi_microstructure_window_complete_60s=True,
            kalshi_book_add_volume_yes_5s=12.0,
            kalshi_book_remove_volume_no_5s=7.0,
            kalshi_microprice_change_cents_5s=0.4,
            kalshi_microprice_variation_cents_5s=0.8,
            kalshi_trade_yes_price_change_cents_5s=-1.0,
            kalshi_trade_yes_vwap_cents_5s=58.5,
            rti_evaluated_at=1000.2,
            kalshi_event_count_5s=8.0,
            kalshi_trade_count_15s=3.0,
            kalshi_book_delta_pressure_yes_5s=-0.25,
            kalshi_trade_imbalance_yes_15s=0.5,
            kalshi_taker_yes_volume_15s=6.0,
            kalshi_taker_no_volume_15s=2.0,
            kalshi_taker_net_yes_volume_15s=4.0,
            kalshi_yes_best_depletion_5s=5.0,
            kalshi_no_best_refill_5s=7.0,
            spot_mid_path_schema_version="spot-mid-path-local-v1",
            spot_mid_path_time_basis="local_created_at",
            spot_mid_path_captured_at=1000.0,
            spot_mid_history_started_at=880.0,
            spot_mid_history_seconds=120.0,
            spot_mid_history_retention_seconds=180.0,
            spot_mid_record_interval_seconds=5.0,
            spot_mid_window_complete_60s=True,
            spot_mid_path_start_at_60s=940.0,
            spot_mid_path_end_at_60s=1000.0,
            spot_mid_path_max_gap_seconds_60s=5.0,
            spot_mid_start_60s=67990.0,
            spot_mid_end_60s=68011.0,
            spot_mid_change_bps_60s=3.09,
            spot_mid_range_bps_60s=3.50,
            spot_mid_realized_volatility_bps_60s=2.20,
            spot_mid_trend_efficiency_60s=0.75,
            rti_spot_lead_lag_schema_version=(
                "rti-spot-index-lead-lag-v1"
            ),
            rti_spot_lead_lag_status="ok",
            rti_spot_basis_bps=0.15,
            rti_spot_basis_start_60s_bps=-1.62,
            rti_spot_basis_change_60s_bps=1.77,
            rti_index_move_bps_60s=1.32,
            rti_spot_move_bps_60s=3.09,
            rti_spot_minus_index_momentum_bps_60s=1.77,
        )
        decision = rti_path_13m_decision(row)
        row_id = led.record_decision(
            decision, row, source_system="rti_path_13m"
        )
        assert row_id is not None
        stored = led.row_by_id(row_id)
        assert stored is not None
        assert stored["kalshi_yes_microprice_cents"] == pytest.approx(59.4)
        assert stored["kalshi_microstructure_time_basis"] == (
            "local_received_at"
        )
        assert stored["kalshi_microstructure_extension_schema_version"] == (
            "rti-exact-microstructure-extension-v1"
        )
        assert stored["kalshi_history_count_capped"] == 0
        assert stored["kalshi_book_window_complete_5s"] == 1
        assert stored["kalshi_trade_window_complete_15s"] == 1
        assert stored["kalshi_microstructure_window_complete_60s"] == 1
        assert stored["kalshi_book_add_volume_yes_5s"] == pytest.approx(12.0)
        assert stored["kalshi_book_remove_volume_no_5s"] == pytest.approx(7.0)
        assert stored["kalshi_microprice_change_cents_5s"] == pytest.approx(0.4)
        assert stored["kalshi_microprice_variation_cents_5s"] == pytest.approx(0.8)
        assert stored["kalshi_trade_yes_price_change_cents_5s"] == pytest.approx(-1.0)
        assert stored["kalshi_trade_yes_vwap_cents_5s"] == pytest.approx(58.5)
        assert stored["evidence_as_of"] == pytest.approx(1000.2)
        assert stored["kalshi_book_delta_pressure_yes_5s"] == pytest.approx(-0.25)
        assert stored["kalshi_trade_imbalance_yes_15s"] == pytest.approx(0.5)
        assert stored["kalshi_taker_net_yes_volume_15s"] == pytest.approx(4.0)
        assert stored["kalshi_no_best_refill_5s"] == pytest.approx(7.0)
        assert stored["spot_mid_path_schema_version"] == (
            "spot-mid-path-local-v1"
        )
        assert stored["spot_mid_window_complete_60s"] == 1
        assert stored["spot_mid_path_max_gap_seconds_60s"] == pytest.approx(5.0)
        assert stored["spot_mid_change_bps_60s"] == pytest.approx(3.09)
        assert stored["rti_spot_lead_lag_status"] == "ok"
        assert stored[
            "rti_spot_minus_index_momentum_bps_60s"
        ] == pytest.approx(1.77)
        profile = json.loads(stored["threshold_json"])
        assert profile["kalshi_yes_microprice_edge_cents"] == pytest.approx(-0.1)
        assert profile["kalshi_event_count_5s"] == pytest.approx(8.0)
        assert profile["kalshi_yes_best_depletion_5s"] == pytest.approx(5.0)
        coverage = led.rti_path_challenger_scoreboard(min_n=1)[
            "exact_feature_coverage"
        ]
        assert coverage["historical_backfill_allowed"] is False
        assert coverage["microstructure_v2"]["rows"] == 1
        assert coverage["microstructure_v2"]["rates"][
            "kalshi_event_window_5s"
        ] == 1.0
        assert coverage["microstructure_v2"]["rates"][
            "kalshi_trade_imbalance_15s"
        ] == 1.0
        assert coverage["microstructure_v2_by_asset"]["BTC"]["rows"] == 1
        assert coverage["microstructure_extension_v1"]["rows"] == 1
        assert coverage["microstructure_extension_v1_by_asset"]["BTC"][
            "rows"
        ] == 1
        assert coverage["dynamics_extension_v1"][
            "complete_executable_close_windows"
        ] == 0
        assert coverage["dynamics_extension_v1"]["outcome_labels_read"] is False
        assert coverage["model_feature_v1"] == {
            "schema_complete_close_windows": 0,
            "complete_executable_close_windows": 0,
            "unusable_close_windows": 0,
            "feature_unavailable_rows": 0,
            "timestamp_alignment_failures": 0,
        }
        assert "model_feature_v2" in coverage
        assert coverage["model_feature_v3"]["primary_preregistered_design"] is False
        assert coverage["model_feature_v4"]["primary_preregistered_design"] is True
        assert coverage["model_feature_v5"]["next_preregistered_design"] is False
        assert coverage["model_feature_v5"][
            "complete_executable_close_windows"
        ] == 0
        dynamics_readiness = coverage["dynamics_v5_model_readiness"]
        assert dynamics_readiness["design_id"] == (
            "q15-rti-market-residual-dynamics-v5"
        )
        assert dynamics_readiness["readiness_uses_outcome_labels"] is False
        assert dynamics_readiness["model_fit_performed"] is False
        assert dynamics_readiness["cohorts"]["NON_BTC_TRANSFER"][
            "windows_remaining"
        ] == 60
        assert dynamics_readiness["cohorts"]["BTC"][
            "windows_remaining"
        ] == 150
        assert coverage["model_feature_v6"]["next_preregistered_design"] is False
        assert coverage["model_feature_v7"]["next_preregistered_design"] is False
        assert coverage["model_feature_v8"]["next_preregistered_design"] is False
        assert coverage["model_feature_v9"]["next_preregistered_design"] is False
        assert coverage["model_feature_v10"]["next_preregistered_design"] is False
        assert coverage["model_feature_v11"]["next_preregistered_design"] is False
        assert coverage["model_feature_v12"]["next_preregistered_design"] is False
        assert coverage["model_feature_v13"]["next_preregistered_design"] is True
        assert coverage["model_feature_v6"][
            "complete_executable_close_windows"
        ] == 0
        lead_lag_readiness = coverage["lead_lag_v6_model_readiness"]
        assert lead_lag_readiness["design_id"] == (
            "q15-rti-market-residual-lead-lag-v6"
        )
        assert lead_lag_readiness["readiness_uses_outcome_labels"] is False
        assert lead_lag_readiness["model_fit_performed"] is False
        cross_venue_readiness = coverage["cross_venue_v7_model_readiness"]
        assert cross_venue_readiness["feature_count"] == 60
        assert cross_venue_readiness["readiness_uses_outcome_labels"] is False
        assert cross_venue_readiness["model_fit_performed"] is False
        independent_venue_readiness = coverage[
            "independent_venue_v8_model_readiness"
        ]
        assert independent_venue_readiness["feature_count"] == 53
        assert independent_venue_readiness[
            "readiness_uses_outcome_labels"
        ] is False
        assert independent_venue_readiness["model_fit_performed"] is False
        independent_microstructure_readiness = coverage[
            "independent_microstructure_v9_model_readiness"
        ]
        assert independent_microstructure_readiness["feature_count"] == 65
        assert independent_microstructure_readiness[
            "readiness_uses_outcome_labels"
        ] is False
        assert independent_microstructure_readiness[
            "model_fit_performed"
        ] is False
        compact_readiness = coverage[
            "independent_microstructure_compact_v10_model_readiness"
        ]
        assert compact_readiness["feature_count"] == 63
        assert compact_readiness["readiness_uses_outcome_labels"] is False
        assert compact_readiness["model_fit_performed"] is False
        cross_asset_readiness = coverage[
            "cross_asset_regime_v11_model_readiness"
        ]
        assert cross_asset_readiness["feature_count"] == 71
        assert cross_asset_readiness["readiness_uses_outcome_labels"] is False
        assert cross_asset_readiness["model_fit_performed"] is False
        compact_v12_readiness = coverage[
            "orthogonal_compact_v12_model_readiness"
        ]
        assert compact_v12_readiness["feature_count"] == 20
        assert compact_v12_readiness["readiness_uses_outcome_labels"] is False
        assert compact_v12_readiness["model_fit_performed"] is False
        compact_v13_readiness = coverage[
            "cohort_conditioned_compact_v13_model_readiness"
        ]
        assert compact_v13_readiness["feature_count"] == 20
        assert compact_v13_readiness["readiness_uses_outcome_labels"] is False
        assert compact_v13_readiness["model_fit_performed"] is False
        assert compact_v13_readiness[
            "v11_and_v12_remain_frozen_parallel_controls"
        ] is True
        assert lead_lag_readiness["cohorts"]["NON_BTC_TRANSFER"][
            "windows_remaining"
        ] == 60
        assert lead_lag_readiness["cohorts"]["BTC"][
            "windows_remaining"
        ] == 150
        readiness = coverage["preregistered_model_readiness"]
        assert readiness["design_id"] == (
            "q15-rti-market-residual-microstructure-v4"
        )
        assert readiness["readiness_uses_outcome_labels"] is False
        assert readiness["model_fit_performed"] is False
        assert readiness["artifact_emitted"] is False
        assert readiness["ready_for_any_locked_freeze"] is False
        assert readiness["cohorts"]["NON_BTC_TRANSFER"][
            "windows_remaining"
        ] == 60
        assert readiness["cohorts"]["BTC"]["windows_remaining"] == 150
    finally:
        led.close()


def test_rti_v4_runtime_readiness_never_hides_timestamp_failure(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        strategy_ledger_module,
        "v4_model_feature_window_coverage",
        lambda rows: {
            "schema_complete_model_candidate_close_windows": 150,
            "complete_model_feature_close_windows": 150,
            "unusable_model_feature_close_windows": [],
            "model_feature_unavailable_rows": [],
            "model_feature_timestamp_failures": [{
                "error": "timestamp_alignment_failure",
            }],
        },
    )
    led = StrategyBotLedger(str(tmp_path / "timestamp-gate.sqlite3"))
    try:
        readiness = led.rti_path_challenger_scoreboard(min_n=1)[
            "exact_feature_coverage"
        ]["preregistered_model_readiness"]
        assert readiness["timestamp_integrity_clean"] is False
        assert readiness["ready_for_any_locked_freeze"] is False
        assert readiness["status"] == (
            "TIMESTAMP_INTEGRITY_FAILURE_REVIEW_REQUIRED"
        )
        assert all(
            cohort["ready_for_locked_freeze"] is False
            for cohort in readiness["cohorts"].values()
        )
    finally:
        led.close()


def test_rti_delayed_confirmation_scoreboard_uses_later_fill(tmp_path):
    led = StrategyBotLedger(str(tmp_path / "rti-delayed-confirm.sqlite3"))
    try:
        row = _rti_delayed_confirm_row(entry_ask_cents=57.0)
        decision = rti_path_12m30_confirmation_decision(row)
        assert led.record_decision(
            decision, row, source_system="rti_path_13m"
        ) is not None
        assert led.resolve(
            source_system="rti_path_13m",
            source_model_version=rti_path_13m_rule_version("BTC"),
            ticker=row["ticker"],
            official_result="YES",
            now=2000.0,
        ) == 1
        rejected_row = _rti_delayed_confirm_row(
            ticker="KXBTC-RTI-DELAYED-REJECT",
            rti_confirm_side="NO",
        )
        rejected_decision = rti_path_12m30_confirmation_decision(rejected_row)
        assert rejected_decision.decision_status == REJECTED
        assert led.record_decision(
            rejected_decision,
            rejected_row,
            source_system="rti_path_13m",
        ) is not None
        assert led.resolve(
            source_system="rti_path_13m",
            source_model_version=rti_path_13m_rule_version("BTC"),
            ticker=rejected_row["ticker"],
            official_result="NO",
            now=2000.0,
        ) == 1
        details = led.rti_path_challenger_scoreboard(min_n=1)["books"][
            RTI_PATH_13M_DELAYED_CONFIRM_CHALLENGER_ID
        ]
        assert details["policy_version"] == (
            RTI_PATH_13M_DELAYED_CONFIRM_POLICY_VERSION
        )
        assert details["notification_eligible"] is False
        assert details["evaluated"] == 2
        assert details["qualified"] == 1
        assert details["rejected"] == 1
        assert details["overall"]["resolved"] == 1
        assert details["overall"]["correct"] == 1
        assert details["overall"]["fee_adjusted_net_pnl_cents"] > 0.0
        rejected = details["rejected_counterfactual"]
        assert rejected["resolved"] == 1
        assert rejected["correct"] == 0
        assert rejected["fee_adjusted_net_pnl_cents"] < 0.0
    finally:
        led.close()


def test_rti_delayed_60s_scoreboard_and_matched_audit_are_isolated(tmp_path):
    led = StrategyBotLedger(str(tmp_path / "rti-delayed-60s.sqlite3"))
    try:
        ticker = "KXBTC-RTI-DELAYED-60S"
        parent = _rti_path_row(
            ticker=ticker,
            close_time=1800.0,
            window_key=2,
            entry_ask_cents=60.0,
        )
        parent_decision = rti_path_13m_decision(parent)
        parent_id = led.record_decision(
            parent_decision, parent, source_system="rti_path_13m"
        )
        assert parent_id is not None
        delayed = _rti_delayed_confirm_60s_row(
            ticker=ticker,
            close_time=1800.0,
            window_key=2,
            entry_ask_cents=55.0,
            rti_confirm_original_row_id=parent_id,
        )
        delayed_decision = rti_path_12m_confirmation_decision(delayed)
        assert delayed_decision.decision_status == ACCEPTED
        legacy_profile = dict(delayed_decision.threshold_profile)
        legacy_challengers = dict(legacy_profile["challengers"])
        legacy_challengers.pop(RTI_PATH_13M_DELAYED_FLIP_60S_CHALLENGER_ID)
        legacy_profile["challengers"] = legacy_challengers
        legacy_profile.pop("delayed_flip_policy_version", None)
        delayed_decision = replace(
            delayed_decision, threshold_profile=legacy_profile
        )
        assert led.record_decision(
            delayed_decision, delayed, source_system="rti_path_13m"
        ) is not None
        assert led.resolve(
            source_system="rti_path_13m",
            source_model_version=rti_path_13m_rule_version("BTC"),
            ticker=ticker,
            official_result="YES",
            now=2000.0,
        ) == 2

        scoreboard = led.rti_path_challenger_scoreboard(min_n=1)
        book = scoreboard["books"][
            RTI_PATH_13M_DELAYED_CONFIRM_60S_CHALLENGER_ID
        ]
        assert book["policy_version"] == (
            RTI_PATH_13M_DELAYED_CONFIRM_60S_POLICY_VERSION
        )
        assert book["evaluated"] == 1
        assert book["qualified"] == 1
        assert book["overall"]["resolved"] == 1
        assert book["overall"]["correct"] == 1
        assert scoreboard["delayed_confirmation_matched"]["overall"][
            "pairs"
        ] == 0
        matched = scoreboard["delayed_confirmation_60s_matched"]
        assert matched["challenger_id"] == (
            RTI_PATH_13M_DELAYED_CONFIRM_60S_CHALLENGER_ID
        )
        assert matched["invalid_parent_links"] == 0
        assert matched["overall"]["pairs"] == 1
        assert matched["overall"]["delayed_taken"] == 1
        assert matched["overall"]["avg_taken_ask_change_cents"] == -5.0
        flip_matched = scoreboard["delayed_flip_60s_matched"]
        assert flip_matched["invalid_parent_links"] == 0
        assert flip_matched["pre_policy_parent_rows_excluded"] == 1
        assert flip_matched["overall"]["pairs"] == 0
    finally:
        led.close()


def test_rti_delayed_60s_flip_scoreboard_prices_flipped_side(
    tmp_path, monkeypatch,
):
    led = StrategyBotLedger(str(tmp_path / "rti-delayed-flip-60s.sqlite3"))
    try:
        ticker = "KXBTC-RTI-FLIP-60S"
        parent = _rti_path_row(
            ticker=ticker,
            close_time=1800.0,
            window_key=2,
            entry_ask_cents=60.0,
        )
        parent_id = led.record_decision(
            rti_path_13m_decision(parent),
            parent,
            source_system="rti_path_13m",
        )
        assert parent_id is not None
        delayed = _rti_delayed_confirm_60s_row(
            ticker=ticker,
            close_time=1800.0,
            window_key=2,
            rti_confirm_original_row_id=parent_id,
            rti_confirm_side="NO",
            rti_opposite_side="NO",
            rti_opposite_ask_cents=44.0,
            rti_opposite_depth_contracts=20.0,
        )
        decision = rti_path_12m_confirmation_decision(delayed)
        assert decision.decision_status == REJECTED
        assert led.record_decision(
            decision, delayed, source_system="rti_path_13m"
        ) is not None
        assert led.resolve(
            source_system="rti_path_13m",
            source_model_version=rti_path_13m_rule_version("BTC"),
            ticker=ticker,
            official_result="NO",
            now=2000.0,
        ) == 2

        scoreboard = led.rti_path_challenger_scoreboard(min_n=1)
        flip = scoreboard["books"][
            RTI_PATH_13M_DELAYED_FLIP_60S_CHALLENGER_ID
        ]
        assert flip["policy_version"] == (
            RTI_PATH_13M_DELAYED_FLIP_60S_POLICY_VERSION
        )
        assert flip["notification_eligible"] is False
        assert flip["evaluated"] == 1
        assert flip["qualified"] == 1
        assert flip["overall"]["resolved"] == 1
        assert flip["overall"]["correct"] == 1
        assert flip["overall"]["fee_adjusted_net_pnl_cents"] > 0.0
        matched = scoreboard["delayed_flip_60s_matched"]
        assert matched["invalid_parent_links"] == 0
        assert matched["overall"]["pairs"] == 1
        assert matched["overall"]["delayed_taken"] == 1
        assert matched["overall"]["saved_losses"] == 0
        assert matched["overall"]["ten_contract_incremental_pnl_dollars"] > 0

        monkeypatch.setattr(runtime, "get_ledger", lambda: led)
        health_book = runtime.rti_path_13m_challenger_health()[
            "research_books"
        ][RTI_PATH_13M_DELAYED_FLIP_60S_CHALLENGER_ID]
        assert health_book["resolved"] == 1
        assert health_book["correct"] == 1
        assert health_book["wilson_95_low"] is not None
        assert health_book["avg_fee_slippage_adjusted_breakeven_rate"] is not None
        assert health_book["next_manual_review_bar"] == 30
        assert health_book["resolved_until_next_review"] == 29
        assert health_book["highest_review_bar_reached"] is None
        assert health_book["automatic_promotion"] is False
        assert health_book["pooled_promotion_criteria_ignored"] is True
        assert health_book["any_cohort_promotion_criteria_met"] is False
        assert health_book["by_transfer_cohort"]["BTC"][
            "promotion_criteria_met"
        ] is False
    finally:
        led.close()


def test_rti_delayed_confirmation_ladder_compares_same_parent_stages(tmp_path):
    led = StrategyBotLedger(str(tmp_path / "rti-delayed-ladder.sqlite3"))
    try:
        ticker = "KXBTC-RTI-LADDER"
        parent = _rti_path_row(
            ticker=ticker,
            close_time=1800.0,
            window_key=2,
            entry_ask_cents=60.0,
        )
        parent_id = led.record_decision(
            rti_path_13m_decision(parent),
            parent,
            source_system="rti_path_13m",
        )
        assert parent_id is not None
        confirm_30 = _rti_delayed_confirm_row(
            ticker=ticker,
            close_time=1800.0,
            window_key=2,
            entry_ask_cents=57.0,
            rti_confirm_original_row_id=parent_id,
        )
        assert led.record_decision(
            rti_path_12m30_confirmation_decision(confirm_30),
            confirm_30,
            source_system="rti_path_13m",
        ) is not None
        confirm_60 = _rti_delayed_confirm_60s_row(
            ticker=ticker,
            close_time=1800.0,
            window_key=2,
            rti_confirm_original_row_id=parent_id,
            rti_confirm_side="NO",
            rti_opposite_side="NO",
            rti_opposite_ask_cents=44.0,
            rti_opposite_depth_contracts=20.0,
        )
        assert led.record_decision(
            rti_path_12m_confirmation_decision(confirm_60),
            confirm_60,
            source_system="rti_path_13m",
        ) is not None
        assert led.resolve(
            source_system="rti_path_13m",
            source_model_version=rti_path_13m_rule_version("BTC"),
            ticker=ticker,
            official_result="NO",
            now=2000.0,
        ) == 3

        ladder = led.rti_path_challenger_scoreboard(min_n=1)[
            "delayed_confirmation_ladder"
        ]
        assert ladder["lineage"] == {
            "invalid_30s_parent_links": 0,
            "invalid_60s_parent_links": 0,
            "invalid_90s_parent_links": 0,
            "invalid_flip_parent_links": 0,
            "pre_90s_policy_parent_rows_excluded": 0,
            "pre_flip_policy_parent_rows_excluded": 0,
        }
        common = ladder["common_30s_60s"]
        assert common["parents"] == 1
        assert common["resolved_parents"] == 1
        assert common["ten_contract_control_pnl_dollars"] < 0
        assert common["stages"]["confirm_30"]["taken"] == 1
        assert common["stages"]["confirm_30"][
            "ten_contract_policy_pnl_dollars"
        ] < 0
        assert common["stages"]["confirm_60"]["taken"] == 0
        assert common["stages"]["confirm_60"][
            "ten_contract_policy_pnl_dollars"
        ] == 0.0
        assert common["stages"]["flip_60"]["taken"] == 1
        assert common["stages"]["flip_60"]["correct_taken"] == 1
        assert common["stages"]["flip_60"][
            "ten_contract_policy_pnl_dollars"
        ] > 0
        assert common["stages"]["flip_60"][
            "ten_contract_incremental_vs_control_dollars"
        ] > 0
        transition = ladder["by_transition"][
            "30_TAKEN__60_REJECTED__90_NOT_EVALUATED__FLIP_TAKEN"
        ]
        assert transition["parents"] == 1
    finally:
        led.close()


def test_rti_delayed_restart_recovery_reads_parent_and_completed_stages(
    tmp_path, monkeypatch,
):
    led = StrategyBotLedger(str(tmp_path / "rti-recovery.sqlite3"))
    try:
        ticker = "KXBTC-RTI-RECOVERY"
        parent = _rti_path_row(
            ticker=ticker,
            close_time=1800.0,
            window_key=2,
            rti_path_end_px=68010.0,
        )
        parent_decision = rti_path_13m_decision(parent)
        parent_id = led.record_decision(
            parent_decision, parent, source_system="rti_path_13m"
        )
        assert parent_id is not None
        delayed = _rti_delayed_confirm_row(
            ticker=ticker,
            close_time=1800.0,
            window_key=2,
            rti_confirm_original_row_id=parent_id,
        )
        assert led.record_decision(
            rti_path_12m30_confirmation_decision(delayed),
            delayed,
            source_system="rti_path_13m",
        ) is not None
        delayed_90 = _rti_delayed_stability_90s_row(
            ticker=ticker,
            close_time=1800.0,
            window_key=2,
            rti_confirm_original_row_id=parent_id,
        )
        assert led.record_decision(
            rti_path_11m30_stability_decision(delayed_90),
            delayed_90,
            source_system="rti_path_13m",
        ) is not None
        monkeypatch.setattr(runtime, "get_ledger", lambda: led)

        state = runtime.rti_delayed_confirmation_recovery_state(
            ticker=ticker, close_time=1800.0
        )
        assert state is not None
        assert state["parent_row_id"] == parent_id
        assert state["parent_strict_accepted"] is True
        assert state["completed_intervals"] == ["11M30S", "12M30S"]
        assert state["original_source"]["model_version"] == (
            rti_path_13m_rule_version("BTC")
        )
        assert state["original_source"]["rti_side"] == "YES"
        assert state["original_source"]["rti_path_end_px"] == 68010.0
    finally:
        led.close()


def test_rti_delayed_matched_audit_counts_saves_skips_and_incremental_pnl(
    tmp_path,
):
    led = StrategyBotLedger(str(tmp_path / "rti-delayed-matched.sqlite3"))
    try:
        def record_pair(ticker, *, delayed_overrides, official):
            parent = _rti_path_row(
                ticker=ticker,
                close_time=1800.0,
                window_key=2,
                entry_ask_cents=60.0,
            )
            parent_decision = rti_path_13m_decision(parent)
            assert parent_decision.decision_status == ACCEPTED
            parent_id = led.record_decision(
                parent_decision, parent, source_system="rti_path_13m"
            )
            assert parent_id is not None
            delayed = _rti_delayed_confirm_row(
                ticker=ticker,
                close_time=1800.0,
                window_key=2,
                rti_confirm_original_row_id=parent_id,
                **delayed_overrides,
            )
            delayed_decision = rti_path_12m30_confirmation_decision(delayed)
            assert led.record_decision(
                delayed_decision, delayed, source_system="rti_path_13m"
            ) is not None
            assert led.resolve(
                source_system="rti_path_13m",
                source_model_version=rti_path_13m_rule_version("BTC"),
                ticker=ticker,
                official_result=official,
                now=2000.0,
            ) == 2
            return delayed_decision.decision_status

        assert record_pair(
            "KXBTC-MATCHED-TAKEN",
            delayed_overrides={"entry_ask_cents": 57.0},
            official="YES",
        ) == ACCEPTED
        assert record_pair(
            "KXBTC-MATCHED-SAVED",
            delayed_overrides={
                "entry_ask_cents": 57.0,
                "rti_confirm_side": "NO",
            },
            official="NO",
        ) == REJECTED
        assert record_pair(
            "KXBTC-MATCHED-SKIPPED",
            delayed_overrides={"entry_ask_cents": 70.0},
            official="YES",
        ) == REJECTED

        matched = led.rti_path_challenger_scoreboard(min_n=1)[
            "delayed_confirmation_matched"
        ]
        assert matched["invalid_parent_links"] == 0
        assert matched["historical_credit_allowed"] is False
        overall = matched["overall"]
        assert overall["pairs"] == 3
        assert overall["resolved_pairs"] == 3
        assert overall["unresolved_pairs"] == 0
        assert overall["delayed_taken"] == 1
        assert overall["delayed_rejected"] == 2
        assert overall["saved_losses"] == 1
        assert overall["skipped_winners"] == 1
        assert overall["control"]["resolved"] == 3
        assert overall["control"]["correct"] == 2
        assert overall["delayed_taken_book"]["resolved"] == 1
        assert overall["delayed_taken_book"]["correct"] == 1
        assert overall["avg_ask_change_cents"] == pytest.approx(4.0 / 3.0)
        assert overall["avg_taken_ask_change_cents"] == pytest.approx(-3.0)
        assert overall["incremental_net_pnl_cents"] == pytest.approx(
            overall["delayed_policy_net_pnl_cents"]
            - overall["control_net_pnl_cents"]
        )
        assert matched["by_transfer_cohort"]["BTC"]["pairs"] == 3
        assert matched["by_transfer_cohort"]["NON_BTC_TRANSFER"]["pairs"] == 0
        assert matched["by_reversal_risk"]["low"]["pairs"] == 3
        assert matched["by_settlement_average_risk"]["low"]["pairs"] == 3
    finally:
        led.close()


def test_rti_path_13m_rejects_wrong_asset_index_and_version():
    wrong_index = rti_path_13m_decision(_rti_path_row(asset="ETH", rti_index_id="BRTI"))
    assert wrong_index.decision_status == REJECTED
    assert "INDEX_NOT_OFFICIAL_RTI" in wrong_index.reason_codes

    wrong_version = rti_path_13m_decision(
        _rti_path_row(asset="SOL", model_version=RTI_PATH_13M_RULE_VERSION)
    )
    assert wrong_version.decision_status == REJECTED
    assert "RULE_VERSION_MISMATCH" in wrong_version.reason_codes


def test_rti_path_13m_ledger_uses_10_lot_fee_and_slippage(tmp_path):
    assert kalshi_order_fee_cents(60.0, 10) == pytest.approx(16.8)
    led = StrategyBotLedger(tmp_path / "rti.sqlite3")
    try:
        row = _rti_path_row()
        decision = rti_path_13m_decision(row)
        assert led.record_decision(decision, row, source_system="rti_path_13m") is not None
        assert led.resolve(
            source_system="rti_path_13m",
            source_model_version=RTI_PATH_13M_RULE_VERSION,
            ticker=row["ticker"],
            official_result="YES",
            now=2000.0,
        ) == 1
        stored = [r for r in led.rows() if r["bot_name"] == BOT_RTI_PATH_13M][0]
        # The 2c adverse fill is 62c; its 10-lot fee is 16.5c total.
        assert stored["hypothetical_pnl_cents"] == pytest.approx(36.35)
    finally:
        led.close()


def test_rti_contract_resolution_repairs_pending_side_ledger(tmp_path):
    led = StrategyBotLedger(tmp_path / "rti-contract-resolve.sqlite3")
    try:
        row = _rti_path_row(close_time=1500.0)
        decision = rti_path_13m_decision(row)
        assert led.record_decision(
            decision, row, source_system="rti_path_13m"
        ) is not None
        assert led.unresolved_rti_tickers(now=1600.0) == [row["ticker"]]

        assert led.resolve_ticker(
            ticker=row["ticker"], official_result="YES", now=1601.0
        ) == 1
        assert led.unresolved_rti_tickers(now=1602.0) == []
        stored = [
            value for value in led.rows()
            if value["bot_name"] == BOT_RTI_PATH_13M
        ][0]
        assert stored["official_result"] == "YES"
        assert stored["resolved_at"] == 1601.0
        assert stored["correct"] == 1
        # Replays are idempotent and cannot overwrite a settled label.
        assert led.resolve_ticker(
            ticker=row["ticker"], official_result="NO", now=1700.0
        ) == 0
        assert led.rows()[0]["official_result"] == "YES"
    finally:
        led.close()


def test_rti_resolution_timestamp_cannot_predate_contract_close(tmp_path):
    led = StrategyBotLedger(tmp_path / "rti-resolution-time.sqlite3")
    try:
        row = _rti_path_row(close_time=1500.0)
        decision = rti_path_13m_decision(row)
        assert led.record_decision(
            decision, row, source_system="rti_path_13m"
        ) is not None
        assert led.resolve_ticker(
            ticker=row["ticker"], official_result="YES", now=1497.5
        ) == 1
        stored = [
            value for value in led.rows()
            if value["bot_name"] == BOT_RTI_PATH_13M
        ][0]
        assert stored["resolved_at"] == 1500.0
    finally:
        led.close()


def test_rti_path_13m_scoreboards_are_version_isolated_by_asset(tmp_path):
    led = StrategyBotLedger(tmp_path / "rti-isolated.sqlite3")
    try:
        for asset, result in (("BTC", "YES"), ("ETH", "NO")):
            row = _rti_path_row(asset=asset)
            decision = rti_path_13m_decision(row)
            assert led.record_decision(
                decision, row, source_system="rti_path_13m"
            ) is not None
            assert led.resolve(
                source_system="rti_path_13m",
                source_model_version=RTI_PATH_13M_RULE_VERSIONS[asset],
                ticker=row["ticker"],
                official_result=result,
                now=2000.0,
            ) == 1

        btc = led.bot_accepted_resolved_stats(
            BOT_RTI_PATH_13M,
            threshold_rule_version=RTI_PATH_13M_RULE_VERSIONS["BTC"],
        )
        eth = led.bot_accepted_resolved_stats(
            BOT_RTI_PATH_13M,
            threshold_rule_version=RTI_PATH_13M_RULE_VERSIONS["ETH"],
        )
        assert (btc["n"], btc["correct"]) == (1, 1)
        assert (eth["n"], eth["correct"]) == (1, 0)
    finally:
        led.close()


def test_rti_path_13m_alert_is_explicitly_paper_only():
    decision = rti_path_13m_decision(_rti_path_row())
    led_row = {
        **_rti_path_row(),
        "bot_name": BOT_RTI_PATH_13M,
        "side": "YES",
        "threshold_json": json.dumps(decision.threshold_profile),
    }
    text = build_v3_alert(led_row)
    assert "V3 BTC RTI CONTROL 13M | PAPER" in text
    assert "BUY YES @ 60c simulated" in text
    assert "61/61 seconds" in text
    assert RTI_PATH_13M_RULE_VERSION in text
    assert "no order placed" in text


def test_spot_confirm_match_is_labeled_as_unpromoted_paper_shadow():
    row = _rti_path_row(
        rti_spot_snapshot_created_at=999.0,
        rti_spot_snapshot_age_s=1.0,
        rti_spot_book_age_s=0.2,
        spot_depth_status="ok",
        spot_depth_imbalance=0.25,
    )
    decision = rti_path_13m_decision(row)
    text = build_v3_alert({
        **row,
        "bot_name": BOT_RTI_PATH_13M,
        "side": "YES",
        "decision_status": ACCEPTED,
        "threshold_json": json.dumps(decision.threshold_profile),
    })
    assert "V3 BTC RTI CONTROL 13M | PAPER" in text
    assert "UNPROMOTED PAPER SHADOW" in text
    assert "fresh spot-book confirmation" in text
    assert RTI_PATH_13M_SPOT_CONFIRM_POLICY_VERSION in text
    assert "no order placed" in text


def test_non_btc_rti_alert_is_labeled_unvalidated_transfer_cohort():
    row = _rti_path_row(asset="ETH")
    decision = rti_path_13m_decision(row)
    text = build_v3_alert({
        **row,
        "bot_name": BOT_RTI_PATH_13M,
        "side": "YES",
        "threshold_json": json.dumps(decision.threshold_profile),
    })
    assert "V3 ETH RTI CONTROL 13M | PAPER" in text
    assert "ETHUSD_RTI path" in text
    assert "TRANSFER COHORT" in text
    assert "no historical validation yet" in text


def test_rti_path_13m_runtime_enqueues_accepted_v3_notification(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_STRATEGY_BOTS_DB", str(tmp_path / "v3.sqlite3"))
    monkeypatch.setenv("Q15_V3_RTI_PATH_13M", "true")
    monkeypatch.setenv("Q15_V3_RTI_PATH_13M_NOTIFY", "true")
    runtime._ledger = None
    calls = []

    def _enqueue(text, *, idempotency_key, expires_at):
        calls.append((text, idempotency_key, expires_at))
        return {
            "delivered": False,
            "muted": False,
            "message_id": None,
            "error": None,
            "outbox_status": "PENDING",
        }

    monkeypatch.setattr(runtime, "enqueue_v3_outbox_notification", _enqueue)
    row_id = runtime.record_rti_path_13m_row(_rti_impulse_row(close_time=2500.0))
    assert row_id is not None
    assert len(calls) == 1
    assert "V3 BTC RTI IMPULSE 13M | PAPER" in calls[0][0]
    assert "UNPROMOTED PAPER CHALLENGER" in calls[0][0]
    assert RTI_PATH_13M_IMPULSE_POLICY_VERSION in calls[0][0]
    assert RTI_PATH_13M_RULE_VERSION in calls[0][1]
    led = runtime.get_ledger()
    assert led is not None
    stored = led.row_by_id(row_id)
    assert stored is not None
    assert stored["notification_status"] == "QUEUED_RETRY"
    led.close()
    runtime._ledger = None


def test_rti_path_13m_strict_control_records_without_notifying(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_STRATEGY_BOTS_DB", str(tmp_path / "v3-control.sqlite3"))
    monkeypatch.setenv("Q15_V3_RTI_PATH_13M", "true")
    monkeypatch.setenv("Q15_V3_RTI_PATH_13M_NOTIFY", "true")
    runtime._ledger = None
    calls = []

    monkeypatch.setattr(
        runtime,
        "enqueue_v3_outbox_notification",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    row_id = runtime.record_rti_path_13m_row(_rti_path_row(close_time=2500.0))
    assert row_id is not None
    assert calls == []
    led = runtime.get_ledger()
    assert led is not None
    stored = led.row_by_id(row_id)
    assert stored is not None
    assert stored["decision_status"] == ACCEPTED
    assert stored["notification_status"] is None
    led.close()
    runtime._ledger = None


def test_rti_probability_v2_runtime_shadow_is_persisted_and_never_notifies(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_STRATEGY_BOTS_DB", str(tmp_path / "v3-probability.sqlite3"))
    monkeypatch.setenv("Q15_V3_RTI_PATH_13M", "true")
    monkeypatch.setenv("Q15_V3_RTI_PATH_13M_NOTIFY", "true")
    runtime._ledger = None
    calls = []
    shadow = {
        "available": True,
        "prospective": True,
        "prospective_after_close_time": 2000.0,
        "model_version": "rti-probability-shadow-v2-test",
        "artifact_sha256": "abc123",
        "cohort": "BTC",
        "market_yes_probability": 0.59,
        "raw_yes_probability": 0.47,
        "calibrated_yes_probability": 0.46,
        "entry_recommendation": {
            "side": "NO",
            "ask_cents": 41.0,
            "depth_contracts": 30.0,
            "depth_available": True,
            "win_probability": 0.54,
            "expected_value_cents_per_contract": 8.2,
            "fee_cents_per_contract": 1.7,
        },
    }
    monkeypatch.setattr(runtime, "rti_probability_prediction", lambda row: shadow)
    monkeypatch.setattr(
        runtime,
        "enqueue_v3_outbox_notification",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    row_id = runtime.record_rti_path_13m_row(_rti_path_row(close_time=2500.0))
    assert row_id is not None
    assert calls == []
    led = runtime.get_ledger()
    assert led is not None
    stored = led.row_by_id(row_id)
    assert stored is not None
    profile = json.loads(stored["threshold_json"])
    challenger = profile["challengers"][
        RTI_PATH_13M_PROBABILITY_V2_CHALLENGER_ID
    ]
    assert challenger["accepted"] is True
    assert challenger["side_override"] == "NO"
    assert challenger["notification_eligible"] is False
    led.close()
    runtime._ledger = None


def test_rti_probability_v3_runtime_shadow_is_persisted_and_never_notifies(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_STRATEGY_BOTS_DB", str(tmp_path / "v3-guarded.sqlite3"))
    monkeypatch.setenv("Q15_V3_RTI_PATH_13M", "true")
    monkeypatch.setenv("Q15_V3_RTI_PATH_13M_NOTIFY", "true")
    runtime._ledger = None
    calls = []
    v3_shadow = {
        "available": True,
        "prospective": True,
        "prospective_after_close_time": 2000.0,
        "model_version": "rti-probability-shadow-v3-test",
        "artifact_sha256": "v3abc123",
        "cohort": "BTC",
        "market_yes_probability": 0.59,
        "raw_yes_probability": 0.47,
        "calibrated_yes_probability": 0.46,
        "out_of_distribution": False,
        "standardization": {
            "max_abs_z_preclip": 2.2,
            "out_of_distribution": False,
        },
        "standardization_policy": {
            "min_std": 1e-8,
            "z_clip": 6.0,
            "max_abs_z_allowed": 8.0,
        },
        "calibration_policy": {"monotone_slope_required": True},
        "entry_recommendation": {
            "side": "NO",
            "ask_cents": 41.0,
            "depth_contracts": 30.0,
            "depth_available": True,
            "win_probability": 0.54,
            "expected_value_cents_per_contract": 8.2,
            "fee_cents_per_contract": 1.7,
        },
    }

    def predict(row, path=None):
        if path is not None:
            return v3_shadow
        return {"available": False, "prospective": False, "error": "v2_quarantined"}

    monkeypatch.setattr(runtime, "rti_probability_prediction", predict)
    monkeypatch.setattr(
        runtime,
        "enqueue_v3_outbox_notification",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    row_id = runtime.record_rti_path_13m_row(_rti_path_row(close_time=2500.0))
    assert row_id is not None
    assert calls == []
    led = runtime.get_ledger()
    assert led is not None
    stored = led.row_by_id(row_id)
    assert stored is not None
    profile = json.loads(stored["threshold_json"])
    v3 = profile["challengers"][RTI_PATH_13M_PROBABILITY_V3_CHALLENGER_ID]
    assert v3["accepted"] is True
    assert v3["side_override"] == "NO"
    assert v3["notification_eligible"] is False
    v2 = profile["challengers"][RTI_PATH_13M_PROBABILITY_V2_CHALLENGER_ID]
    assert v2["accepted"] is False
    led.close()
    runtime._ledger = None


def test_rti_v11_runtime_bridge_is_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_STRATEGY_BOTS_DB", str(tmp_path / "v11-off.sqlite3"))
    monkeypatch.setenv("Q15_V3_RTI_PATH_13M", "true")
    monkeypatch.delenv(
        "Q15_V3_RTI_MICROSTRUCTURE_V11_PAPER_RECORD", raising=False,
    )
    runtime._ledger = None
    monkeypatch.setattr(
        runtime,
        "rti_v11_prediction",
        lambda row: (_ for _ in ()).throw(
            AssertionError("disabled V11 bridge called model")
        ),
    )
    row_id = runtime.record_rti_path_13m_row(
        _rti_path_row(close_time=2500.0)
    )
    assert row_id is not None
    led = runtime.get_ledger()
    assert led is not None
    stored = led.row_by_id(row_id)
    assert stored is not None
    profile = json.loads(stored["threshold_json"])
    assert RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID not in (
        profile["challengers"]
    )
    led.close()
    runtime._ledger = None


def test_rti_v11_health_exposes_only_manual_record_activation(monkeypatch):
    monkeypatch.setenv(
        "Q15_V3_RTI_MICROSTRUCTURE_V11_PAPER_RECORD", "true",
    )
    monkeypatch.setattr(
        runtime,
        "rti_v11_artifact_health",
        lambda cohort: {
            "available": True,
            "cohort": cohort,
            "paper_only": True,
            "notification_eligible": False,
            "automatic_promotion": False,
            "real_trading_allowed": False,
        },
    )
    health = runtime._v11_locked_artifact_health("BTC")
    assert health["paper_record_enabled"] is True
    assert health["prospective_ledger_status"] == (
        "ENABLED_PROSPECTIVE_PAPER_RECORD_ONLY"
    )
    assert health["prospective_ledger_notification_eligible"] is False
    assert health["prospective_ledger_real_trading_allowed"] is False
    assert health["prospective_ledger_automatic_promotion"] is False


def test_rti_v11_opt_in_records_idempotent_settled_lineage_without_notification(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_STRATEGY_BOTS_DB", str(tmp_path / "v11-on.sqlite3"))
    monkeypatch.setenv("Q15_V3_RTI_PATH_13M", "true")
    monkeypatch.setenv("Q15_V3_RTI_PATH_13M_NOTIFY", "true")
    monkeypatch.setenv(
        "Q15_V3_RTI_MICROSTRUCTURE_V11_PAPER_RECORD", "true",
    )
    runtime._ledger = None
    calls = []
    shadow = _rti_microstructure_v11_shadow()
    monkeypatch.setattr(runtime, "rti_v11_prediction", lambda row: shadow)

    def probability_disabled(row, path=None):
        return {
            "available": False,
            "prospective": False,
            "error": "test_probability_disabled",
        }

    monkeypatch.setattr(runtime, "rti_probability_prediction", probability_disabled)
    monkeypatch.setattr(
        runtime,
        "enqueue_v3_outbox_notification",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    row = _rti_path_row(
        ticker="KXBTC-V11-PAPER",
        close_time=2500.0,
        window_key=9901,
    )
    first_id = runtime.record_rti_path_13m_row(row)
    second_id = runtime.record_rti_path_13m_row(row)
    assert first_id is not None
    assert second_id == first_id
    assert calls == []

    led = runtime.get_ledger()
    assert led is not None
    stored = led.row_by_id(first_id)
    assert stored is not None
    profile = json.loads(stored["threshold_json"])
    challenger = profile["challengers"][
        RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID
    ]
    assert challenger["accepted"] is True
    assert challenger["notification_eligible"] is False
    assert challenger["evidence"]["artifact_sha256"] == "a" * 64
    assert challenger["evidence"]["test_state_sha256"] == "b" * 64
    assert stored["notification_status"] is None
    assert len([
        stored_row for stored_row in led.rows()
        if stored_row["ticker"] == row["ticker"]
        and stored_row["bot_name"] == BOT_RTI_PATH_13M
    ]) == 1

    assert led.resolve(
        source_system="rti_path_13m",
        source_model_version=RTI_PATH_13M_RULE_VERSION,
        ticker=row["ticker"],
        official_result="NO",
        now=3000.0,
    ) == 1
    system = led.rti_path_challenger_scoreboard(min_n=1)
    book = system["books"][
        RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID
    ]
    assert book["policy_version"] == (
        RTI_PATH_13M_MICROSTRUCTURE_V11_POLICY_VERSION
    )
    assert book["notification_eligible"] is False
    assert book["evaluated"] == 1
    assert book["qualified"] == 1
    assert book["overall"]["resolved"] == 1
    assert book["overall"]["correct"] == 1
    assert book["overall"]["fee_adjusted_net_pnl_cents"] > 0.0
    scorecard = system["probability_scorecards"][
        RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID
    ]
    assert scorecard["stored_probability_field"] == "yes_probability"
    assert scorecard["scoreable_resolved_rows"] == 1
    assert scorecard["overall"]["n"] == 1
    assert scorecard["overall"]["correct"] == 1
    bootstrap = scorecard["overall"]["paired_close_window_bootstrap"]
    assert bootstrap["available"] is True
    assert bootstrap["rows"] == 1
    assert bootstrap["close_windows"] == 1
    assert bootstrap["same_close_assets_resampled_together"] is True
    assert scorecard["evidence_integrity"][
        "observed_test_state_sha256"
    ] == ["b" * 64]
    assert scorecard["evidence_integrity"][
        "single_test_state_sha256"
    ] is True
    assert scorecard["manual_promotion_only"] is True
    led.close()
    runtime._ledger = None


def test_rti_v11_lineage_gate_rejects_mixed_artifacts_within_cohort_only(
    tmp_path,
):
    led = StrategyBotLedger(str(tmp_path / "v11-lineage.sqlite3"))
    cases = (
        (
            "BTC", "KXBTC-V11-LINEAGE-1", 2500.0, 1,
            _rti_microstructure_v11_shadow(),
        ),
        (
            "BTC", "KXBTC-V11-LINEAGE-2", 2600.0, 2,
            _rti_microstructure_v11_shadow(
                artifact_sha256="d" * 64,
                test_state_sha256="e" * 64,
            ),
        ),
        (
            "ETH", "KXETH-V11-LINEAGE-1", 2700.0, 3,
            _rti_microstructure_v11_shadow(
                asset="ETH",
                artifact_sha256="f" * 64,
                test_state_sha256="1" * 64,
                test_metrics_sha256="2" * 64,
            ),
        ),
    )
    try:
        for asset, ticker, close_time, window_key, shadow in cases:
            row = _rti_path_row(
                asset=asset,
                ticker=ticker,
                close_time=close_time,
                window_key=window_key,
                rti_microstructure_shadow_v11=shadow,
            )
            decision = rti_path_13m_decision(row)
            assert decision.threshold_profile["challengers"][
                RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID
            ]["accepted"] is True
            assert led.record_decision(
                decision, row, source_system="rti_path_13m",
            ) is not None

        scorecard = led.rti_path_challenger_scoreboard(min_n=30)[
            "probability_scorecards"
        ][RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID]
        assert scorecard["scoreable_resolved_rows"] == 0
        btc = scorecard["prospective_lineage_by_transfer_cohort"]["BTC"]
        nonbtc = scorecard["prospective_lineage_by_transfer_cohort"][
            "NON_BTC_TRANSFER"
        ]
        assert btc["prospective_evidence_rows"] == 2
        assert btc["single_model_version"] is True
        assert btc["single_artifact_sha256"] is False
        assert btc["single_test_state_sha256"] is False
        assert btc["met"] is False
        assert nonbtc["prospective_evidence_rows"] == 1
        assert nonbtc["single_artifact_sha256"] is True
        assert nonbtc["single_test_state_sha256"] is True
        assert nonbtc["met"] is True

        btc_gate = runtime._rti_probability_lineage_gate(
            scorecard,
            cohort="BTC",
            challenger_id=RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID,
        )
        nonbtc_gate = runtime._rti_probability_lineage_gate(
            scorecard,
            cohort="NON_BTC_TRANSFER",
            challenger_id=RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID,
        )
        assert btc_gate["met"] is False
        assert "single_artifact_sha256" in btc_gate["failures"]
        assert "single_test_state_sha256" in btc_gate["failures"]
        assert nonbtc_gate["met"] is True
        assert nonbtc_gate["failures"] == []
    finally:
        led.close()


def test_rti_v11_health_cannot_pass_promotion_with_mixed_lineage():
    economics = {
        "rows": 30,
        "resolved": 30,
        "pnl_scoreable_resolved": 30,
        "unscoreable_resolved": 0,
        "cost_evidence_complete": True,
        "label_integrity_failures": 0,
        "correct": 27,
        "accuracy": 0.9,
        "wilson_95_low": 0.75,
        "wilson_95_high": 0.97,
        "avg_fee_adjusted_breakeven_rate": 0.58,
        "avg_fee_slippage_adjusted_breakeven_rate": 0.61,
        "fee_adjusted_net_pnl_cents": 500.0,
        "max_cumulative_drawdown_cents": 40.0,
        "provisional": False,
    }
    proper_scores = {
        "n": 30,
        "market_n": 30,
        "brier_score": 0.12,
        "market_brier_score": 0.20,
        "log_loss": 0.38,
        "market_log_loss": 0.55,
        "paired_close_window_bootstrap": (
            _passing_v11_close_window_bootstrap()
        ),
    }
    mixed_lineage = {
        "prospective_evidence_rows": 30,
        "single_model_version": True,
        "single_artifact_sha256": False,
        "artifact_sha256_valid": False,
        "single_test_state_sha256": False,
        "single_test_metrics_sha256": True,
        "test_state_sha256_valid": False,
        "test_metrics_sha256_valid": True,
        "evidence_cohort_matches_row_cohort": True,
        "v11_exact_test_design_protocol_lineage": True,
        "met": False,
        "observed_model_versions": ["one-model"],
        "observed_artifact_sha256": ["a" * 64, "d" * 64],
        "observed_test_state_sha256": ["b" * 64, "e" * 64],
    }
    scorecard = {
        "challenger_id": RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID,
        "point_in_time_stored_evidence_only": True,
        "historical_recomputation_allowed": False,
        "stored_probability_field": "yes_probability",
        "overall": proper_scores,
        "by_transfer_cohort": {
            "BTC": proper_scores,
            "NON_BTC_TRANSFER": {},
        },
        "prospective_lineage_by_transfer_cohort": {
            "BTC": mixed_lineage,
            "NON_BTC_TRANSFER": {"prospective_evidence_rows": 0},
        },
    }
    system = {
        "books": {
            RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID: {
                "policy_version": RTI_PATH_13M_MICROSTRUCTURE_V11_POLICY_VERSION,
                "notification_eligible": False,
                "evaluated": 30,
                "qualified": 30,
                "rejected": 0,
                "qualification_rate": 1.0,
                "overall": economics,
                "by_transfer_cohort": {
                    "BTC": economics,
                    "NON_BTC_TRANSFER": {},
                },
                "rejected_counterfactual": {},
            }
        },
        "probability_scorecards": {
            RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID: scorecard,
        },
        "exact_feature_coverage": {},
    }

    class _Ledger:
        def rti_path_challenger_scoreboard(self, *args, **kwargs):
            return system

    def _health():
        return runtime._rti_path_13m_challenger_health_with_ledger(
            ledger=_Ledger(),
            v2_model_health={},
            v3_model_health={},
            probability_models={},
            v11_locked_artifacts={},
            empty_v11_collection_readiness={},
            empty_probability_scorecards={},
            empty_exact_feature_coverage={},
        )

    health = _health()
    book = health["research_books"][
        RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID
    ]
    assert book["status"] == "LINEAGE_INTEGRITY_FAILED_REVIEW_REQUIRED"
    assert book["by_transfer_cohort"]["BTC"][
        "probability_skill_gate"
    ]["met"] is True
    assert book["by_transfer_cohort"]["BTC"][
        "lineage_integrity_gate"
    ]["met"] is False
    assert book["by_transfer_cohort"]["BTC"][
        "promotion_criteria_met"
    ] is False
    assert book["any_cohort_promotion_criteria_met"] is False

    valid = dict(mixed_lineage)
    valid.update({
        "single_artifact_sha256": True,
        "artifact_sha256_valid": True,
        "single_test_state_sha256": True,
        "test_state_sha256_valid": True,
        "met": True,
        "observed_artifact_sha256": ["a" * 64],
        "observed_test_state_sha256": ["b" * 64],
    })
    scorecard["prospective_lineage_by_transfer_cohort"]["BTC"] = valid
    saved_bootstrap = proper_scores.pop("paired_close_window_bootstrap")
    missing_uncertainty = _health()["research_books"][
        RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID
    ]
    assert missing_uncertainty["status"] == "ACTIVE_PAPER_RESEARCH"
    assert missing_uncertainty["by_transfer_cohort"]["BTC"][
        "probability_skill_gate"
    ]["clustered_uncertainty_met"] is False
    assert missing_uncertainty["by_transfer_cohort"]["BTC"][
        "promotion_criteria_met"
    ] is False
    proper_scores["paired_close_window_bootstrap"] = saved_bootstrap
    healthy = _health()["research_books"][
        RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID
    ]
    assert healthy["status"] == "ACTIVE_PAPER_RESEARCH"
    assert healthy["by_transfer_cohort"]["BTC"][
        "promotion_criteria_met"
    ] is True
    assert healthy["any_cohort_promotion_criteria_met"] is True


def test_rti_path_13m_runtime_enqueues_enabled_transfer_asset(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_STRATEGY_BOTS_DB", str(tmp_path / "v3-eth.sqlite3"))
    monkeypatch.setenv("Q15_V3_RTI_PATH_13M", "true")
    monkeypatch.setenv("Q15_V3_RTI_PATH_13M_NOTIFY", "true")
    monkeypatch.setenv("Q15_V3_RTI_PATH_13M_ASSETS", "ETH")
    runtime._ledger = None
    calls = []

    def _enqueue(text, *, idempotency_key, expires_at):
        calls.append((text, idempotency_key, expires_at))
        return {
            "delivered": False,
            "muted": False,
            "message_id": None,
            "error": None,
            "outbox_status": "PENDING",
        }

    monkeypatch.setattr(runtime, "enqueue_v3_outbox_notification", _enqueue)
    row = _rti_impulse_row(
        asset="ETH",
        model_version=RTI_PATH_13M_RULE_VERSIONS["ETH"],
        rti_index_id=RTI_PATH_13M_INDEX_IDS["ETH"],
        ticker="KXETH-RTI-13M",
        close_time=2500.0,
    )
    row_id = runtime.record_rti_path_13m_row(row)
    assert row_id is not None
    assert len(calls) == 1
    assert "V3 ETH RTI IMPULSE 13M | PAPER" in calls[0][0]
    assert RTI_PATH_13M_RULE_VERSIONS["ETH"] in calls[0][1]
    led = runtime.get_ledger()
    assert led is not None
    stored = led.row_by_id(row_id)
    assert stored is not None
    assert stored["source_model_version"] == RTI_PATH_13M_RULE_VERSIONS["ETH"]
    assert stored["notification_status"] == "QUEUED_RETRY"
    led.close()
    runtime._ledger = None


def test_rti_path_13m_runtime_mutes_old_wide_challenger_alerts(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_STRATEGY_BOTS_DB", str(tmp_path / "v3-challenger.sqlite3"))
    monkeypatch.setenv("Q15_V3_RTI_PATH_13M", "true")
    monkeypatch.setenv("Q15_V3_RTI_PATH_13M_NOTIFY", "true")
    runtime._ledger = None
    calls = []

    def _enqueue(text, *, idempotency_key, expires_at):
        calls.append((text, idempotency_key, expires_at))
        return {
            "delivered": False,
            "muted": False,
            "message_id": None,
            "error": None,
            "outbox_status": "PENDING",
        }

    monkeypatch.setattr(runtime, "enqueue_v3_outbox_notification", _enqueue)
    row_id = runtime.record_rti_path_13m_row(_rti_path_row(
        close_time=2500.0,
        entry_ask_cents=53.0,
        spread_cents=2.0,
        rti_path_persistence=1.0,
    ))
    assert row_id is not None
    assert calls == []
    led = runtime.get_ledger()
    assert led is not None
    stored = led.row_by_id(row_id)
    assert stored is not None
    assert stored["decision_status"] == REJECTED
    assert stored["notification_status"] is None
    led.close()
    runtime._ledger = None


def test_13m_sniper_gate_matrix(monkeypatch):
    monkeypatch.setenv("Q15_V3_13M_SNIPER", "true")

    accepted = thirteen_m_sniper_decision(_thirteen_row(), source_system="ultoim_v2")
    assert accepted is not None
    assert accepted.bot_name == BOT_THIRTEEN_M_SNIPER
    assert accepted.decision_status == ACCEPTED
    assert {"CONVICTION", "MARKET_ASLEEP", "FLIP_SAFE", "FLOW_OK", "EV_FLOOR"}.issubset(
        set(accepted.reason_codes)
    )

    cases = [
        (
            "conviction",
            _thirteen_row(calibrated_yes_probability=0.58, entry_ask_cents=50.0),
            "CONVICTION_BELOW_MIN",
        ),
        (
            "market",
            _thirteen_row(calibrated_yes_probability=0.70, entry_ask_cents=59.0),
            "MARKET_ALREADY_PRICED",
        ),
        ("flip", _thirteen_row(flip_probability=30.0), "FLIP_UNSAFE"),
        (
            "flow",
            _thirteen_row(spot_depth_trade_net_notional_60s=-1000.0,
                          spot_depth_trade_net_notional_60s_abs_p70=100.0),
            "FLOW_CONTRA_STRONG",
        ),
        (
            "ev",
            _thirteen_row(calibrated_yes_probability=0.62, entry_ask_cents=58.0),
            "EV_BELOW_FLOOR",
        ),
    ]
    for _name, row, reason in cases:
        decision = thirteen_m_sniper_decision(row, source_system="ultoim_v2")
        assert decision is not None
        assert decision.decision_status == REJECTED
        assert reason in decision.reason_codes
        other_failures = [
            code for code in decision.reason_codes
            if code in {
                "CONVICTION_BELOW_MIN",
                "MARKET_ALREADY_PRICED",
                "FLIP_UNSAFE",
                "FLOW_CONTRA_STRONG",
                "EV_BELOW_FLOOR",
            }
        ]
        assert other_failures == [reason]


def test_13m_sniper_missing_flow_fails_open_and_manipulation_is_stamped(monkeypatch):
    monkeypatch.setenv("Q15_V3_13M_SNIPER", "true")

    decision = thirteen_m_sniper_decision(
        _thirteen_row(
            spot_depth_trade_net_notional_60s=None,
            spot_depth_trade_net_notional_60s_abs_p70=None,
            manipulation_suspected=True,
        ),
        source_system="ultoim_v2",
    )

    assert decision is not None
    assert decision.decision_status == ACCEPTED
    assert "FLOW_OK_MISSING_FEED_FAIL_OPEN" in decision.reason_codes
    assert "MANIPULATION_SUSPECTED_ALLOWED" in decision.reason_codes


def test_13m_sniper_ev_uses_fee_math_and_empirical_wilson_lb(monkeypatch):
    monkeypatch.setenv("Q15_V3_13M_SNIPER", "true")

    floor = thirteen_m_sniper_decision(
        _thirteen_row(calibrated_yes_probability=0.63, entry_ask_cents=58.0),
        source_system="ultoim_v2",
    )
    assert floor is not None
    assert floor.decision_status == ACCEPTED
    assert floor.threshold_profile["kalshi_fee_cents"] == 2
    assert floor.threshold_profile["ev_cents"] == pytest.approx(3.0)

    empirical = thirteen_m_sniper_decision(
        _thirteen_row(
            calibrated_yes_probability=0.77,
            entry_ask_cents=55.0,
            thirteen_m_sniper_resolved_n=30,
            thirteen_m_sniper_wilson_lb=0.60,
        ),
        source_system="ultoim_v2",
    )
    assert empirical is not None
    assert empirical.decision_status == ACCEPTED
    assert "EV_USES_EMPIRICAL_WILSON_LB" in empirical.reason_codes
    assert empirical.threshold_profile["ev_win_probability"] == pytest.approx(0.60)
    assert empirical.threshold_profile["ev_cents"] == pytest.approx(3.0)


def test_13m_sniper_ledger_stats_and_flow_percentile_signatures(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_V3_13M_SNIPER", "true")
    led = StrategyBotLedger(tmp_path / "v3.sqlite3")
    try:
        for i, (ticker, result) in enumerate((("KX13-WIN", "YES"), ("KX13-LOSS", "NO"))):
            row = _thirteen_row(ticker=ticker, window_key=3000 + i, created_at=1000.0 + i)
            decision = thirteen_m_sniper_decision(row, source_system="ultoim_v2")
            assert decision is not None and decision.decision_status == ACCEPTED
            assert led.record_decision(decision, row, source_system="ultoim_v2") is not None
            led.resolve(
                source_system="ultoim_v2",
                source_model_version="ultoim-v2",
                ticker=ticker,
                official_result=result,
                now=2000.0 + i,
            )

        stats = led.bot_accepted_resolved_stats(BOT_THIRTEEN_M_SNIPER, STRATEGY_VERSION)
        assert stats["n"] == 2
        assert stats["correct"] == 1
        assert stats["accuracy"] == pytest.approx(0.5)
        assert stats["wilson_lb"] is not None

        for i, flow in enumerate((10.0, -20.0, 30.0, -40.0, 50.0)):
            row = _row(
                ticker=f"KXFLOW-{i}",
                window_key=4000 + i,
                created_at=500.0 + i,
                spot_depth_trade_net_notional_60s=flow,
            )
            assert led.record_decision(
                baseline_decision(row),
                row,
                source_system="ultoim_v2",
            ) is not None

        assert led.trailing_abs_flow_percentile(0.70, 5) == pytest.approx(40.0)
    finally:
        led.close()


def test_13m_sniper_context_failures_warn_once(caplog):
    class _BrokenLedger:
        def bot_accepted_resolved_stats(self, *args, **kwargs):
            raise AttributeError("stats missing")

        def trailing_abs_flow_percentile(self, *args, **kwargs):
            raise AttributeError("flow missing")

    runtime._thirteen_m_stats_warning_logged = False
    runtime._thirteen_m_flow_warning_logged = False
    caplog.set_level("WARNING", logger="strategy_bots.runtime")

    runtime._with_thirteen_m_sniper_context(_BrokenLedger(), _thirteen_row(created_at=123.0))
    runtime._with_thirteen_m_sniper_context(_BrokenLedger(), _thirteen_row(created_at=124.0))

    assert caplog.text.count("v3 13M sniper stats unavailable") == 1
    assert caplog.text.count("v3 13M sniper flow percentile unavailable") == 1


def test_13m_sniper_telegram_dedups_per_ticker_window(tmp_path, monkeypatch):
    class _Telegram:
        def __init__(self):
            self.sent = []

        def send(self, text):
            self.sent.append(text)
            return {"delivered": True, "muted": False, "message_id": len(self.sent), "error": None}

    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_STRATEGY_BOTS_DB", str(tmp_path / "v3.sqlite3"))
    monkeypatch.setenv("Q15_V3_TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("Q15_V3_13M_SNIPER", "true")
    monkeypatch.setenv("Q15_V3_13M_SNIPER_NOTIFY", "true")
    runtime._ledger = None
    runtime._telegram = _Telegram()

    row = _thirteen_row(window_key=1300, model_version="ultoim-v2")
    assert runtime.record_source_row(row, source_system="ultoim_v2") == 3
    assert runtime.record_source_row(row, source_system="ultoim_v2") == 0

    led = runtime.get_ledger()
    assert led is not None
    sniper = [r for r in led.rows(STRATEGY_VERSION) if r["bot_name"] == BOT_THIRTEEN_M_SNIPER]
    assert len(sniper) == 1
    assert sniper[0]["notification_status"] == "SENT"
    assert len(runtime._telegram.sent) == 1


def test_13m_sniper_alert_marker_safety():
    text = build_v3_alert({
        "asset": "BTC",
        "side": "YES",
        "interval": "13M",
        "bot_name": BOT_THIRTEEN_M_SNIPER,
        "source_rule": "RESEARCH_ONLY_MARK",
        "ticker": "KXBTC-13M",
        "entry_ask_cents": 55.0,
        "reason_codes": "V3_13M_SNIPER_EVAL,MARKET_ASLEEP,EV_FLOOR",
        "threshold_json": json.dumps({
            "model_side_probability": 0.65,
            "ev_cents": 8.0,
            "resolved_n": 12,
            "resolved_accuracy": 0.667,
            "spot_depth_trade_net_notional_60s_abs_p70": 100.0,
        }),
        "spot_depth_trade_net_notional_60s": 25.0,
    })

    assert "V3 13M EARLY" in text
    assert "PROVISIONAL (n=12, acc=66.7%)" in text
    assert "ENTRY RECOMMENDED" not in text
    assert "NO ENTRY YET" not in text
    assert "V9.5 CHECK" not in text
    assert "Hourly Report" not in text
    assert "Mode: paper/read-only alert; no executor route" in text
    assert "n/a" not in text


def test_13m_sniper_auto_mute_records_and_sends_notice_once(tmp_path, monkeypatch):
    class _Telegram:
        def __init__(self):
            self.sent = []

        def send(self, text):
            self.sent.append(text)
            return {"delivered": True, "muted": False, "message_id": len(self.sent), "error": None}

    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_STRATEGY_BOTS_DB", str(tmp_path / "v3.sqlite3"))
    monkeypatch.setenv("Q15_V3_TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("Q15_V3_13M_SNIPER", "true")
    monkeypatch.setenv("Q15_V3_13M_SNIPER_NOTIFY", "true")
    runtime._ledger = None
    runtime._telegram = _Telegram()

    first = _thirteen_row(
        ticker="KXBTC-MUTE-1",
        window_key=2001,
        thirteen_m_sniper_resolved_n=80,
        thirteen_m_sniper_accuracy=0.60,
        thirteen_m_sniper_wilson_lb=0.54,
        entry_ask_cents=49.0,
    )
    second = _thirteen_row(
        ticker="KXBTC-MUTE-2",
        window_key=2001,
        thirteen_m_sniper_resolved_n=80,
        thirteen_m_sniper_accuracy=0.60,
        thirteen_m_sniper_wilson_lb=0.54,
        entry_ask_cents=49.0,
    )

    runtime.record_source_row(first, source_system="ultoim_v2")
    runtime.record_source_row(second, source_system="ultoim_v2")

    led = runtime.get_ledger()
    assert led is not None
    sniper = [r for r in led.rows(STRATEGY_VERSION) if r["bot_name"] == BOT_THIRTEEN_M_SNIPER]
    assert [r["notification_status"] for r in sniper] == ["AUTO_MUTED", "AUTO_MUTED"]
    assert len(runtime._telegram.sent) == 1
    assert "V3 13M EARLY AUTO-MUTED" in runtime._telegram.sent[0]


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


def test_v3_alert_stamps_degraded_when_feed_watchdog_stale(tmp_path, monkeypatch):
    class _Telegram:
        def __init__(self):
            self.sent: list[str] = []

        def send(self, text):
            self.sent.append(text)
            return {"delivered": True, "muted": False, "message_id": 123, "error": None}

    cycle_watchdog.reset()
    cycle_watchdog.observe_feed_ages({"coinbase_adv_l2": 301.0}, now=1000.0)
    class _Ledger:
        def __init__(self, recorded):
            self.recorded = recorded
            self.marked = None

        def row_by_id(self, row_id):
            return self.recorded

        def mark_notification(self, row_id, *, status, message_id, error):
            self.marked = (row_id, status, message_id, error)

    runtime._telegram = _Telegram()

    row = _row(
        asset="BTC",
        ticker="KXBTC-DEGRADED",
        predicted_side="NO",
        entry_ask_cents=63.0,
        coinbase_l2_status="ok",
        coinbase_l2_top_12_imbalance_notional=-0.20,
        reason_codes="EXPENSIVE_NO_ADMIT",
    )
    decision = confidence_tier_decision(row, source_system="ultoim_v2")
    recorded = {
        **row,
        "bot_name": decision.bot_name,
        "decision_status": decision.decision_status,
        "tier": decision.tier,
        "source_rule": "EXPENSIVE_NO_ADMIT",
        "reason_codes": ",".join(decision.reason_codes),
    }
    ledger = _Ledger(recorded)

    runtime._maybe_notify(ledger, 1, decision)
    text = "\n".join(runtime._telegram.sent)
    assert "<b>DEGRADED</b>" in text
    assert "coinbase_adv_l2" in text
    assert "V3_DEGRADED_FEED_COINBASE_ADV_L2" in text
    assert ledger.marked == (1, "SENT", 123, None)
    cycle_watchdog.reset()


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

    assert runtime.record_source_row(row, source_system="ultoim_v2") == 4
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


def test_hvf_wrapper_owns_source_notification_even_when_rejected(monkeypatch):
    row = _row(
        asset="SOL",
        ticker="KXSOL-REJECT",
        predicted_outcome="YES",
        rule_code="HVF_MORE_FIRE_STRICT",
        record_kind="MORE_FIRE_STRICT_ALERT",
        selected_depth_ratio=5.0,
        spot_depth_trade_net_notional_60s=-1000.0,
        kalshi_taker_net_yes_volume_15s=0.0,
    )
    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_V3_SUPPRESS_OWNED_SOURCE_NOTIFICATIONS", "true")

    assert runtime.owns_source_notification(row, source_system="high_vol_flip") is True


def test_hvf_wrapper_is_only_hvf_v3_notification_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_STRATEGY_BOTS_DB", str(tmp_path / "v3.sqlite3"))
    monkeypatch.setenv("Q15_V3_TELEGRAM_ENABLED", "false")
    monkeypatch.setenv("Q15_V3_SUPPRESS_OWNED_SOURCE_NOTIFICATIONS", "true")
    monkeypatch.setenv("Q15_V3_MOREFIRE_ACCEPT_ENABLED", "true")
    monkeypatch.setenv("Q15_V3_EMPIRICAL_DELIVERY_GUARD", "false")
    runtime._ledger = None
    runtime._telegram = None

    row = _row(
        asset="SOL",
        ticker="KXSOL-HVF",
        predicted_outcome="YES",
        predicted_side=None,
        rule_code="HVF_MORE_FIRE_STRICT",
        record_kind="MORE_FIRE_STRICT_ALERT",
        model_version="high-vol-flip-test",
        selected_depth_ratio=5.0,
        spot_depth_trade_net_notional_60s=200.0,
        kalshi_taker_net_yes_volume_15s=-25.0,
        coinbase_l2_status="ok",
        coinbase_l2_top_12_imbalance_notional=0.20,
        coinbase_l2_top_60_imbalance_notional=0.20,
        coinbase_l2_top_250_imbalance_notional=0.20,
    )
    btc = {
        "ticker": "KXBTC-1",
        "depth_contracts": 1500.0,
        "yes_mid_cents": 60.0,
        "no_mid_cents": 40.0,
        "dominant_side": "YES",
        "predicted_side": "YES",
        "model_yes_probability": 0.62,
    }

    assert runtime.record_source_row(row, source_system="high_vol_flip", btc_context=btc) == 6
    led = runtime.get_ledger()
    assert led is not None
    rows = led.rows(STRATEGY_VERSION)
    wrapper = next(r for r in rows if r["bot_name"] == BOT_HVF_DEPTH_FLOW)
    morefire = next(r for r in rows if r["bot_name"] == BOT_MOREFIRE_BTC)
    no_entry_price = next(
        r for r in rows if r["bot_name"] == BOT_MOREFIRE_NO_ENTRY_PRICE
    )

    assert wrapper["decision_status"] == ACCEPTED
    assert wrapper["notification_status"] == "MUTED"
    assert morefire["decision_status"] == ACCEPTED
    assert morefire["notification_status"] is None
    assert no_entry_price["decision_status"] == REJECTED
    assert no_entry_price["notification_status"] is None


def test_hvf_wrapper_only_mode_mutes_generic_v3_notifications(tmp_path, monkeypatch):
    class _Telegram:
        def __init__(self):
            self.sent = []

        def send(self, text):
            self.sent.append(text)
            return {"delivered": True, "muted": False, "message_id": len(self.sent), "error": None}

    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_STRATEGY_BOTS_DB", str(tmp_path / "v3.sqlite3"))
    monkeypatch.setenv("Q15_V3_TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("Q15_V3_HVF_DEPTH_FLOW_NOTIFICATIONS_ONLY", "true")
    monkeypatch.setenv("Q15_V3_SUPPRESS_OWNED_SOURCE_NOTIFICATIONS", "true")
    monkeypatch.setenv("Q15_V3_HYPE_YES_ACCEPT_ENABLED", "true")
    monkeypatch.setenv("Q15_V3_EMPIRICAL_DELIVERY_GUARD", "false")
    runtime._ledger = None
    runtime._telegram = _Telegram()

    row = _row(
        asset="HYPE",
        ticker="KXHYPE-GENERIC",
        predicted_side="YES",
        spot_depth_trade_net_qty_60s=45.0,
        yes_ask_depth_contracts=320.0,
        kalshi_taker_net_yes_volume_15s=None,
    )

    assert runtime.owns_source_notification(row, source_system="ultoim_v2") is False
    assert runtime.record_source_row(row, source_system="ultoim_v2") == 5
    led = runtime.get_ledger()
    assert led is not None
    hype = next(r for r in led.rows(STRATEGY_VERSION) if r["bot_name"] == BOT_HYPE_YES)
    assert hype["decision_status"] == ACCEPTED
    assert hype["notification_status"] is None
    assert runtime._telegram.sent == []


def test_depth_formula_research_sends_in_v3_channel_without_owning_source(tmp_path, monkeypatch):
    class _Telegram:
        def __init__(self):
            self.sent = []

        def send(self, text):
            self.sent.append(text)
            return {"delivered": True, "muted": False, "message_id": len(self.sent), "error": None}

    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_STRATEGY_BOTS_DB", str(tmp_path / "v3.sqlite3"))
    monkeypatch.setenv("Q15_V3_TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("Q15_V3_HVF_DEPTH_FLOW_NOTIFICATIONS_ONLY", "true")
    monkeypatch.setenv("Q15_V3_DEPTH_FORMULA_TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("Q15_V3_SUPPRESS_OWNED_SOURCE_NOTIFICATIONS", "true")
    runtime._ledger = None
    runtime._telegram = _Telegram()

    row = _row(
        asset="XRP",
        ticker="KXXRP-15M-DEPTH-SEND",
        interval="15M",
        predicted_side="NO",
        entry_ask_cents=54.0,
        spread_cents=2.0,
        depth_contracts=40.0,
        no_bid_depth_contracts=16.0,
        no_ask_depth_contracts=40.0,
    )

    assert runtime.owns_source_notification(row, source_system="ultoim_v2") is False
    assert runtime.record_source_row(row, source_system="ultoim_v2") == 4
    led = runtime.get_ledger()
    assert led is not None
    depth = next(r for r in led.rows(STRATEGY_VERSION) if r["bot_name"] == BOT_DEPTH_FORMULA_15M)
    assert depth["decision_status"] == RESEARCH_ONLY
    assert depth["notification_status"] == "SENT"
    assert len(runtime._telegram.sent) == 1
    assert "V3 15M DEPTH FORMULA / RESEARCH" in runtime._telegram.sent[0]


def test_hvf_wrapper_only_mode_still_sends_hvf_depth_flow_alert(tmp_path, monkeypatch):
    class _Telegram:
        def __init__(self):
            self.sent = []

        def send(self, text):
            self.sent.append(text)
            return {"delivered": True, "muted": False, "message_id": len(self.sent), "error": None}

    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_STRATEGY_BOTS_DB", str(tmp_path / "v3.sqlite3"))
    monkeypatch.setenv("Q15_V3_TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("Q15_V3_HVF_DEPTH_FLOW_NOTIFICATIONS_ONLY", "true")
    monkeypatch.setenv("Q15_V3_SUPPRESS_OWNED_SOURCE_NOTIFICATIONS", "true")
    monkeypatch.setenv("Q15_V3_EMPIRICAL_DELIVERY_GUARD", "false")
    runtime._ledger = None
    runtime._telegram = _Telegram()

    row = _row(
        asset="SOL",
        ticker="KXSOL-HVF-SEND",
        predicted_outcome="YES",
        predicted_side=None,
        rule_code="HVF_OWN_STRONG_SELECTED",
        record_kind="HIGH_VOL_FLIP_ALERT",
        model_version="high-vol-flip-test",
        selected_depth_ratio=5.0,
        spot_depth_trade_net_notional_60s=200.0,
        kalshi_taker_net_yes_volume_15s=-25.0,
        coinbase_l2_top_12_imbalance_notional=0.20,
        coinbase_l2_top_60_imbalance_notional=0.20,
        coinbase_l2_top_250_imbalance_notional=0.20,
    )

    assert runtime.owns_source_notification(row, source_system="high_vol_flip") is True
    assert runtime.record_source_row(row, source_system="high_vol_flip") == 4
    led = runtime.get_ledger()
    assert led is not None
    wrapper = next(r for r in led.rows(STRATEGY_VERSION) if r["bot_name"] == BOT_HVF_DEPTH_FLOW)
    assert wrapper["decision_status"] == ACCEPTED
    assert wrapper["notification_status"] == "SENT"
    assert len(runtime._telegram.sent) == 1
    assert "V3 HVF DEPTH/FLOW PICK" in runtime._telegram.sent[0]


def test_empirical_delivery_guard_downgrades_yes_and_late_interval(tmp_path, monkeypatch):
    class _Telegram:
        def __init__(self):
            self.sent = []

        def send(self, text):
            self.sent.append(text)
            return {"delivered": True, "muted": False, "message_id": len(self.sent), "error": None}

    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_STRATEGY_BOTS_DB", str(tmp_path / "v3.sqlite3"))
    monkeypatch.setenv("Q15_V3_TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("Q15_V3_HYPE_YES_ACCEPT_ENABLED", "true")
    runtime._ledger = None
    runtime._telegram = _Telegram()

    row = _row(
        asset="HYPE",
        ticker="KXHYPE-GUARD",
        predicted_side="YES",
        interval="10M",
        spot_depth_trade_net_qty_60s=45.0,
        yes_ask_depth_contracts=320.0,
        kalshi_taker_net_yes_volume_15s=None,
    )

    assert runtime.record_source_row(row, source_system="ultoim_v2") == 5
    led = runtime.get_ledger()
    assert led is not None
    hype = next(r for r in led.rows(STRATEGY_VERSION) if r["bot_name"] == BOT_HYPE_YES)
    assert hype["decision_status"] == RESEARCH_ONLY
    assert "V3_EMPIRICAL_GUARD_YES_RESEARCH_ONLY" in hype["reason_codes"]
    assert "V3_EMPIRICAL_GUARD_INTERVAL_10M_RESEARCH_ONLY" in hype["reason_codes"]
    assert hype["notification_status"] is None
    assert runtime._telegram.sent == []


def test_empirical_delivery_guard_preserves_measured_positive_bnb_no(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_STRATEGY_BOTS_DB", str(tmp_path / "v3.sqlite3"))
    monkeypatch.setenv("Q15_V3_TELEGRAM_ENABLED", "false")
    runtime._ledger = None
    runtime._telegram = None

    row = _row(
        asset="BNB",
        ticker="KXBNB-GUARD",
        predicted_side="NO",
        interval="10M",
        spot_depth_trade_sell_notional_15s=41.0,
        spot_depth_imbalance=-0.021,
        spot_depth_trade_net_notional_60s=-1.0,
        spot_depth_trade_net_qty_60s=-0.1,
        kalshi_taker_net_yes_volume_15s=-1.0,
    )

    assert runtime.record_source_row(row, source_system="ultoim_v2") == 5
    led = runtime.get_ledger()
    assert led is not None
    bnb_no = next(r for r in led.rows(STRATEGY_VERSION) if r["bot_name"] == BOT_BNB_NO)
    assert bnb_no["decision_status"] == ACCEPTED
    assert "V3_EMPIRICAL_GUARD_INTERVAL_10M_RESEARCH_ONLY" not in bnb_no["reason_codes"]


def test_hvf_interval_gated_rows_stay_background(tmp_path, monkeypatch):
    class _Telegram:
        def __init__(self):
            self.sent = []

        def send(self, text):
            self.sent.append(text)
            return {"delivered": True, "muted": False, "message_id": len(self.sent), "error": None}

    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_STRATEGY_BOTS_DB", str(tmp_path / "v3.sqlite3"))
    monkeypatch.setenv("Q15_V3_TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("Q15_V3_HVF_DEPTH_FLOW_NOTIFICATIONS_ONLY", "true")
    monkeypatch.setenv("Q15_V3_SUPPRESS_OWNED_SOURCE_NOTIFICATIONS", "true")
    runtime._ledger = None
    runtime._telegram = _Telegram()

    row = _row(
        asset="ETH",
        ticker="KXETH-HVF-BACKGROUND",
        interval="12M",
        predicted_outcome="YES",
        predicted_side=None,
        rule_code="HVF_OWN_STRONG_SELECTED",
        record_kind="HIGH_VOL_FLIP_ALERT",
        model_version="high-vol-flip-test",
        spot_depth_trade_net_notional_60s=-900.0,
        spot_depth_trade_net_notional_15s=40.0,
        kalshi_taker_net_yes_volume_15s=0.0,
    )

    assert runtime.owns_source_notification(row, source_system="high_vol_flip") is True
    assert runtime.record_source_row(row, source_system="high_vol_flip") == 4
    led = runtime.get_ledger()
    assert led is not None
    wrapper = next(r for r in led.rows(STRATEGY_VERSION) if r["bot_name"] == BOT_HVF_DEPTH_FLOW)
    assert wrapper["decision_status"] == RESEARCH_ONLY
    assert wrapper["notification_status"] is None
    assert "V3_HVF_OWN_STRONG_BACKGROUND_RESEARCH_ONLY" in wrapper["reason_codes"]
    assert runtime._telegram.sent == []
