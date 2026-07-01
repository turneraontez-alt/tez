from __future__ import annotations

import json
import math
import sqlite3

from q15_upgrade.strategy_bots import runtime
from q15_upgrade.strategy_bots.ledger import StrategyBotLedger, net_pnl_cents
from q15_upgrade.strategy_bots.rules import (
    ACCEPTED,
    BOT_BNB_NO,
    BOT_BNB_YES_REVERSAL,
    BOT_BTC_REGIME,
    BOT_CONFIDENCE_TIER,
    BOT_HYPE_YES,
    BOT_HVF_DEPTH_FLOW,
    BOT_MOREFIRE_BTC,
    REJECTED,
    RESEARCH_ONLY,
    STRATEGY_VERSION,
    btc_regime_context_probe_decision,
    bnb_no_confirmation_decision,
    bnb_yes_reversal_decision,
    confidence_tier_decision,
    hvf_depth_flow_wrapper_decision,
    hype_yes_confirmation_decision,
    morefire_btc_confirmed_decision,
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

    assert runtime.record_source_row(first, source_system="ultoim_v2") == 4
    assert runtime.record_source_row(second, source_system="ultoim_v2") == 4

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

    assert runtime.record_source_row(row, source_system="ultoim_v2") == 3
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

    assert runtime.record_source_row(row, source_system="high_vol_flip", btc_context=btc) == 5
    led = runtime.get_ledger()
    assert led is not None
    rows = led.rows(STRATEGY_VERSION)
    wrapper = next(r for r in rows if r["bot_name"] == BOT_HVF_DEPTH_FLOW)
    morefire = next(r for r in rows if r["bot_name"] == BOT_MOREFIRE_BTC)

    assert wrapper["decision_status"] == ACCEPTED
    assert wrapper["notification_status"] == "MUTED"
    assert morefire["decision_status"] == ACCEPTED
    assert morefire["notification_status"] is None


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
    assert runtime.record_source_row(row, source_system="ultoim_v2") == 4
    led = runtime.get_ledger()
    assert led is not None
    hype = next(r for r in led.rows(STRATEGY_VERSION) if r["bot_name"] == BOT_HYPE_YES)
    assert hype["decision_status"] == ACCEPTED
    assert hype["notification_status"] is None
    assert runtime._telegram.sent == []


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

    assert runtime.record_source_row(row, source_system="ultoim_v2") == 4
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

    assert runtime.record_source_row(row, source_system="ultoim_v2") == 4
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
