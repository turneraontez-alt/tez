"""Research-only Drift evidence cohorts and their truthful Telegram card."""
from __future__ import annotations

import json

import pytest

from q15_upgrade.strategy_bots.rules import (
    ACCEPTED,
    BOT_DRIFT_ACCURACY_V91,
    BOT_DRIFT_ASYMMETRIC_VOLUME,
    BOT_DRIFT_BALANCED_V95,
    BOT_DRIFT_FLOW_SPREAD,
    RESEARCH_ONLY,
    drift_accuracy_v91_shadow_decision,
    drift_asymmetric_volume_shadow_decision,
    drift_balanced_v95_shadow_decision,
    drift_flow_spread_13m_decision,
)
from q15_upgrade.strategy_bots.telegram import build_drift_pick_alert


def _row(**over):
    base = {
        "created_at": 1_000.0,
        "record_kind": "DRIFT_PICK_13M",
        "model_version": "interval-research-v1",
        "asset": "XRP",
        "ticker": "KXXRP15M-EVIDENCE",
        "interval": "13M",
        "predicted_side": "YES",
        "entry_ask_cents": 65.0,
        "distance_sigma": 4e-5,
        "flip_probability": 20.0,
        # A tight spread is the frozen policy's confirmation fallback even when
        # spot flow is stale.  Evidence cohorts must use this same OR branch.
        "spread_cents": 2.0,
        "spot_depth_status": "stale",
        "spot_depth_snapshot_age_seconds": 99.0,
        "spot_depth_age_seconds": 99.0,
        "spot_depth_trade_age_seconds": 99.0,
        "spot_depth_trade_net_notional_60s": -10.0,
        "drift_v91_yes_fraction_all": 0.75,
        "drift_v91_yes_fraction_directional": 0.5,
        "drift_v91_observation_count": 12,
        "drift_v95_15m_side": "YES",
        "drift_v95_15m_flow_score": 0.25,
        "drift_v95_15m_age_seconds": 118.0,
        "drift_btc_15m_side": "YES",
        "drift_btc_15m_age_seconds": 119.0,
        "drift_core_breadth": 2,
        "drift_asymmetric_breadth": 3,
        "drift_flow_1m": 10.0,
        "drift_flow_3m": 15.0,
        "drift_flow_5m": 20.0,
        "drift_flow_13m": 70.0,
        "drift_flow_coverage": 1.0,
        "evidence_grade": "A",
        "feature_cohort": "FULL_FEATURE",
        "build_sha": "0123456789abcdef",
        "index_status": "missing",
        "index_missing_reason": "settlement_index_tick_stale",
        "kalshi_depth_status": "ok",
    }
    base.update(over)
    return base


@pytest.mark.parametrize(
    ("ask", "distance", "band", "incremental"),
    [
        (60.0, 3e-5, "ASK_60_64", False),
        (64.0, 3e-5, "ASK_60_64", False),
        (65.0, 1e-4, "ASK_65_73", True),
        (73.0, 1e-4, "ASK_65_73", True),
    ],
)
def test_asymmetric_volume_exact_boundaries_are_research_only(
    ask, distance, band, incremental
):
    decision = drift_asymmetric_volume_shadow_decision(
        _row(entry_ask_cents=ask, distance_sigma=distance)
    )

    assert decision is not None
    assert decision.bot_name == BOT_DRIFT_ASYMMETRIC_VOLUME
    assert decision.decision_status == RESEARCH_ONLY
    profile = decision.threshold_profile
    assert profile["price_distance_band"] == band
    assert profile["distance_sigma"] == distance
    assert profile["incremental_to_core"] is incremental
    assert profile["gate_path"] == "SPREAD_LTE_2"
    assert profile["cohort_gate_path"] == "ASYMMETRIC_VOLUME_SHADOW"
    assert profile["review_bars"] == [30, 60, 150]
    assert profile["passed"] is False
    assert profile["never_notify"] is True
    assert profile["notification_eligible"] is False


@pytest.mark.parametrize(
    "override",
    [
        {"entry_ask_cents": 59.999},
        {"entry_ask_cents": 64.5},
        {"entry_ask_cents": 73.001},
        {"entry_ask_cents": 64.0, "distance_sigma": 3.0001e-5},
        {"entry_ask_cents": 65.0, "distance_sigma": 1.00001e-4},
        {"flip_probability": 30.001},
        {"predicted_side": "NO"},
        {"asset": "BNB"},
    ],
)
def test_asymmetric_volume_fails_closed_outside_preregistered_structure(override):
    assert drift_asymmetric_volume_shadow_decision(_row(**override)) is None


def test_all_evidence_cohorts_require_the_exact_frozen_confirmation_or():
    unconfirmed = _row(
        spread_cents=2.001,
        spot_depth_status="stale",
        spot_depth_snapshot_age_seconds=16.0,
        spot_depth_age_seconds=16.0,
        spot_depth_trade_age_seconds=16.0,
        spot_depth_trade_net_notional_60s=100.0,
    )
    assert drift_asymmetric_volume_shadow_decision(unconfirmed) is None
    assert drift_balanced_v95_shadow_decision(unconfirmed) is None
    assert drift_accuracy_v91_shadow_decision(unconfirmed) is None

    flow_confirmed = dict(
        spread_cents=5.0,
        spot_depth_status="ok",
        spot_depth_snapshot_age_seconds=15.0,
        spot_depth_age_seconds=15.0,
        spot_depth_trade_age_seconds=15.0,
        spot_depth_trade_net_notional_60s=0.01,
    )
    for rule in (
        drift_asymmetric_volume_shadow_decision,
        drift_balanced_v95_shadow_decision,
        drift_accuracy_v91_shadow_decision,
    ):
        decision = rule(_row(**flow_confirmed))
        assert decision is not None
        assert decision.decision_status == RESEARCH_ONLY
        assert decision.threshold_profile["gate_path"] == "FLOW_60S_POSITIVE"


def test_balanced_v95_requires_explicit_15m_yes_agreement():
    assert drift_balanced_v95_shadow_decision(
        _row(drift_v95_15m_side=None)
    ) is None
    assert drift_balanced_v95_shadow_decision(
        _row(drift_v95_15m_side="NO")
    ) is None
    # A generic side is ambiguous unless it carries an explicit 15M checkpoint.
    assert drift_balanced_v95_shadow_decision(
        _row(drift_v95_15m_side=None, v95_side="YES")
    ) is None

    decision = drift_balanced_v95_shadow_decision(
        _row(drift_v95_15m_side=None, v95_side="YES", v95_checkpoint="15M")
    )
    assert decision is not None
    assert decision.bot_name == BOT_DRIFT_BALANCED_V95
    assert decision.decision_status == RESEARCH_ONLY
    assert decision.threshold_profile["v95_15m_side"] == "YES"
    assert decision.threshold_profile["v95_15m_yes_agreement"] is True


def test_accuracy_v91_uses_yes_over_all_not_directional_fraction():
    boundary = drift_accuracy_v91_shadow_decision(
        _row(
            drift_v91_yes_fraction_all=0.75,
            drift_v91_yes_fraction_directional=0.01,
            drift_v91_observation_count=11,
        )
    )
    assert boundary is not None
    assert boundary.bot_name == BOT_DRIFT_ACCURACY_V91
    assert boundary.decision_status == RESEARCH_ONLY
    assert boundary.threshold_profile["v91_full_path_yes_fraction"] == 0.75
    assert boundary.threshold_profile["v91_observation_count"] == 11

    assert drift_accuracy_v91_shadow_decision(
        _row(
            drift_v91_yes_fraction_all=0.749,
            drift_v91_yes_fraction_directional=1.0,
        )
    ) is None
    assert drift_accuracy_v91_shadow_decision(
        _row(
            drift_v91_yes_fraction_all=None,
            drift_v91_yes_fraction_directional=1.0,
        )
    ) is None


def test_accuracy_v91_count_evidence_is_checked_for_consistency():
    counts_only = drift_accuracy_v91_shadow_decision(
        _row(
            drift_v91_yes_fraction_all=None,
            drift_v91_observation_count=None,
            v91_full_path_yes_count=3,
            v91_full_path_all_count=4,
        )
    )
    assert counts_only is not None
    assert counts_only.threshold_profile["v91_full_path_yes_fraction"] == 0.75

    contradictory = drift_accuracy_v91_shadow_decision(
        _row(
            drift_v91_yes_fraction_all=0.8,
            drift_v91_observation_count=None,
            v91_full_path_yes_count=3,
            v91_full_path_all_count=4,
        )
    )
    assert contradictory is None


def test_new_cohort_does_not_widen_the_frozen_live_decision(monkeypatch):
    monkeypatch.setenv("Q15_V3_DRIFT_FLOW_SPREAD", "true")
    row = _row(entry_ask_cents=65.0, distance_sigma=1e-4)

    shadow = drift_asymmetric_volume_shadow_decision(row)
    live = drift_flow_spread_13m_decision(row)

    assert shadow is not None and shadow.decision_status == RESEARCH_ONLY
    assert live is not None and live.decision_status != ACCEPTED


def _telegram_row(decision, source):
    return {
        **source,
        "bot_name": decision.bot_name,
        "decision_status": decision.decision_status,
        "threshold_json": json.dumps(dict(decision.threshold_profile)),
    }


def test_research_telegram_is_truthful_and_shows_canonical_evidence():
    source = _row(
        spread_cents=5.0,
        spot_depth_status="ok",
        spot_depth_snapshot_age_seconds=1.0,
        spot_depth_age_seconds=2.0,
        spot_depth_trade_age_seconds=3.0,
        spot_depth_trade_net_notional_60s=25.0,
    )
    decision = drift_accuracy_v91_shadow_decision(source)
    assert decision is not None

    text = build_drift_pick_alert(_telegram_row(decision, source))

    assert "DRIFT RESEARCH 13M · ACCURACY V91" in text
    assert "Counterfactual confirmation: 60s spot flow" in text
    assert "RESEARCH ONLY" in text and "never notification eligible" in text
    assert "TRACK YES" in text and "BUY YES" not in text
    assert "Evidence: grade A | ages snap/book/trade 1s/2s/3s (fresh)" in text
    assert "V91: YES/all 75%" in text
    assert "V95 15M: YES flow score 0.2 age 118s" in text
    assert "BTC: YES grade n/a age 119s | breadth core/asym 2/3" in text
    assert "Flow 1/3/5/13m: +10/+15/+20/+70 | coverage 100%" in text
    assert "Cohort: feature FULL_FEATURE | policy drift_accuracy_v91_shadow" in text
    assert "build 01234567/ACCURACY" in text
    assert "Feeds: index missing (settlement_index_tick_stale) | Kalshi ok" in text


def test_live_telegram_names_each_confirmation_path_and_missing_is_na():
    common = {
        "bot_name": BOT_DRIFT_FLOW_SPREAD,
        "decision_status": ACCEPTED,
        "asset": "XRP",
        "ticker": "KXXRP15M-LIVE",
        "entry_ask_cents": 65.0,
    }
    spread_text = build_drift_pick_alert({
        **common,
        "threshold_json": json.dumps({
            "gate_path": "SPREAD_LTE_2",
            "spread_cents": 2.0,
        }),
    })
    flow_text = build_drift_pick_alert({
        **common,
        "threshold_json": json.dumps({
            "gate_path": "FLOW_60S_POSITIVE",
            "spot_depth_trade_net_notional_60s": 10.0,
            "spread_cents": 5.0,
        }),
    })

    assert "DRIFT TIGHT-SPREAD FALLBACK 13M" in spread_text
    assert "DRIFT FLOW CONFIRMED 13M" not in spread_text
    assert "DRIFT FLOW CONFIRMED 13M" in flow_text
    assert "Evidence: grade n/a | ages snap/book/trade n/a/n/a/n/a (n/a)" in spread_text
    assert "V91: YES/all n/a (n/a) dir n/a n n/a" in spread_text
    assert "V95 15M: n/a flow n/a age n/a (n/a)" in spread_text
    assert "BTC: n/a grade n/a age n/a | breadth core/asym n/a/n/a" in spread_text
    assert "Flow 1/3/5/13m: n/a/n/a/n/a/n/a | coverage n/a" in spread_text
    assert "Feeds: index n/a | Kalshi n/a" in spread_text
    assert "grade pass" not in spread_text.lower()
