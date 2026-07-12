"""Safety contract for the frozen Drift Flow/Spread deployment policy.

These tests deliberately exercise the pure decision layer.  The accepted core
and the two relaxed cohorts must therefore stay separable even when an operator
sets legacy research/environment knobs to permissive values.
"""
from __future__ import annotations

import json

import pytest

from q15_upgrade.strategy_bots.rules import (
    ACCEPTED,
    BOT_DRIFT_FLOW_SPREAD,
    BOT_DRIFT_FLOW_SPREAD_SHADOW_FLOW15,
    BOT_DRIFT_FLOW_SPREAD_SHADOW_SPREAD4,
    RESEARCH_ONLY,
    drift_addon_requal_decision,
    drift_flow_spread_13m_decision,
    drift_flow_spread_shadow_flow15_decision,
    drift_flow_spread_shadow_spread4_decision,
    drift_latequal_decision,
)


FROZEN_ASSETS = ("DOGE", "HYPE", "SOL", "XRP")


@pytest.fixture(autouse=True)
def _enable_drift_policy(monkeypatch):
    monkeypatch.setenv("Q15_V3_DRIFT_FLOW_SPREAD", "true")
    monkeypatch.setenv("Q15_V3_DRIFT_ADDON", "true")
    monkeypatch.setenv("Q15_V3_DRIFT_LATEQUAL", "true")


def _row(**over):
    base = {
        "created_at": 100.0,
        "model_version": "interval-research-v1",
        "record_kind": "DRIFT_PICK_13M",
        "asset": "XRP",
        "ticker": "KXXRP15M-POLICY",
        "interval": "13M",
        "window_key": 77,
        "close_time": 1_000.0,
        "predicted_side": "YES",
        "entry_ask_cents": 65.0,
        "distance_sigma": 1e-5,
        # Keep the source spelling too: a source adapter must not be able to
        # evade the frozen check merely by changing which alias it supplies.
        "distance_from_strike": 1e-5,
        "flip_probability": 20.0,
        "spread_cents": 3.0,
        "spot_depth_status": "ok",
        "spot_depth_snapshot_age_seconds": 0.0,
        "spot_depth_age_seconds": 0.0,
        "spot_depth_trade_age_seconds": 0.0,
        "spot_depth_raw_book_age_seconds": 0.0,
        "spot_depth_raw_trade_age_seconds": 0.0,
        "spot_depth_trade_net_notional_15s": -10.0,
        "spot_depth_trade_net_notional_60s": 10.0,
    }
    base.update(over)
    return base


def _checkpoint_row(**over):
    base = {
        "created_at": 200.0,
        "model_version": "interval-research-v1",
        "record_kind": "DRIFT_ADDON_REQUAL",
        "asset": "XRP",
        "ticker": "KXXRP15M-CHECKPOINT",
        "interval": "12M",
        "window_key": 88,
        "close_time": 1_000.0,
        "predicted_side": "YES",
        "entry_ask_cents": 66.0,
        "spread_cents": 2.0,
        "distance_sigma": 1e-5,
        "flip_probability": 20.0,
        "drift_base_ask_cents": 64.0,
        "drift_ask13_cents": None,
        "drift_rule_version": "drift-addon-requal-12m-7m-v1",
    }
    base.update(over)
    return base


def _not_accepted(decision) -> bool:
    return decision is None or decision.decision_status != ACCEPTED


def _policy_text(decision) -> str:
    return (
        ",".join(decision.reason_codes)
        + " "
        + json.dumps(dict(decision.threshold_profile), sort_keys=True)
    ).upper()


@pytest.mark.parametrize("asset", FROZEN_ASSETS)
def test_frozen_core_accepts_only_whitelisted_assets_at_exact_boundaries(asset):
    decision = drift_flow_spread_13m_decision(
        _row(
            asset=asset,
            entry_ask_cents=60.0,
            distance_sigma=3e-5,
            distance_from_strike=3e-5,
            flip_probability=30.0,
            spread_cents=3.0,
            spot_depth_snapshot_age_seconds=15.0,
            spot_depth_age_seconds=15.0,
            spot_depth_trade_age_seconds=15.0,
            spot_depth_trade_net_notional_60s=0.01,
        )
    )
    assert decision is not None
    assert decision.bot_name == BOT_DRIFT_FLOW_SPREAD
    assert decision.decision_status == ACCEPTED

    upper = drift_flow_spread_13m_decision(
        _row(asset=asset, entry_ask_cents=73.0, spread_cents=2.0)
    )
    assert upper is not None and upper.decision_status == ACCEPTED


@pytest.mark.parametrize(
    "override",
    [
        {"predicted_side": "NO"},
        {"entry_ask_cents": 59.999},
        {"entry_ask_cents": 73.001},
        {"distance_sigma": 3.0001e-5, "distance_from_strike": 3.0001e-5},
        {"flip_probability": 30.001},
    ],
)
def test_frozen_core_structural_limits_cannot_be_crossed(override):
    assert _not_accepted(drift_flow_spread_13m_decision(_row(**override)))


@pytest.mark.parametrize(
    "age_field",
    [
        "spot_depth_snapshot_age_seconds",
        "spot_depth_age_seconds",
        "spot_depth_trade_age_seconds",
    ],
)
def test_positive_flow_must_be_fresh_at_every_effective_age_gate(age_field):
    row = _row(
        spread_cents=3.0,
        spot_depth_trade_net_notional_60s=100.0,
        **{age_field: 15.001},
    )
    assert _not_accepted(drift_flow_spread_13m_decision(row))


def test_tight_spread_fallback_is_the_only_branch_independent_of_flow_freshness():
    accepted = drift_flow_spread_13m_decision(
        _row(
            spread_cents=2.0,
            spot_depth_status="stale",
            spot_depth_snapshot_age_seconds=99.0,
            spot_depth_age_seconds=99.0,
            spot_depth_trade_age_seconds=99.0,
            spot_depth_trade_net_notional_60s=-100.0,
        )
    )
    assert accepted is not None and accepted.decision_status == ACCEPTED

    rejected = drift_flow_spread_13m_decision(
        _row(
            spread_cents=2.001,
            spot_depth_status="stale",
            spot_depth_snapshot_age_seconds=99.0,
            spot_depth_age_seconds=99.0,
            spot_depth_trade_age_seconds=99.0,
            spot_depth_trade_net_notional_60s=100.0,
        )
    )
    assert _not_accepted(rejected)


@pytest.mark.parametrize("asset", ["BNB", "ADA"])
def test_bnb_and_unknown_assets_are_quarantined_as_research(asset):
    decision = drift_flow_spread_13m_decision(_row(asset=asset))
    assert decision is not None
    assert decision.decision_status == RESEARCH_ONLY
    text = _policy_text(decision)
    assert "QUARANT" in text or "OUTSIDE_FROZEN_CORE" in text
    if asset == "BNB":
        assert "BNB" in text


def test_hostile_legacy_env_overrides_cannot_relax_core(monkeypatch):
    hostile = {
        "Q15_V3_DRIFT_FLOW_SPREAD_MAX_CENTS": "99",
        "Q15_V3_DRIFT_SPOT_SNAPSHOT_MAX_AGE_SECONDS": "999",
        "Q15_V3_DRIFT_SPOT_BOOK_MAX_AGE_SECONDS": "999",
        "Q15_V3_DRIFT_SPOT_TRADE_MAX_AGE_SECONDS": "999",
        "Q15_DRIFT_SHADOW_ASK_LO": "0",
        "Q15_DRIFT_SHADOW_ASK_HI": "100",
        "Q15_DRIFT_SHADOW_DIST_MAX": "99",
        "Q15_DRIFT_SHADOW_FLIP_MAX": "100",
        "Q15_DRIFT_SHADOW_SIDE": "NO",
    }
    for name, value in hostile.items():
        monkeypatch.setenv(name, value)

    # The old configurable spread maximum must not turn a 3c/nonpositive-flow
    # row into an accepted core pick.
    spread_relax = drift_flow_spread_13m_decision(
        _row(spread_cents=3.0, spot_depth_trade_net_notional_60s=0.0)
    )
    assert _not_accepted(spread_relax)

    # Nor may permissive freshness or source-envelope knobs admit stale or
    # structurally out-of-policy rows.
    stale = drift_flow_spread_13m_decision(
        _row(
            spread_cents=3.0,
            spot_depth_snapshot_age_seconds=16.0,
            spot_depth_age_seconds=16.0,
            spot_depth_trade_age_seconds=16.0,
            spot_depth_trade_net_notional_60s=100.0,
        )
    )
    assert _not_accepted(stale)
    assert _not_accepted(
        drift_flow_spread_13m_decision(
            _row(
                distance_sigma=4e-5,
                distance_from_strike=4e-5,
                flip_probability=31.0,
                spread_cents=2.0,
            )
        )
    )


def test_spread4_shadow_widens_only_spread_and_is_never_accepted():
    incremental_row = _row(
        spread_cents=4.0,
        spot_depth_trade_net_notional_15s=-10.0,
        spot_depth_trade_net_notional_60s=-10.0,
    )
    assert _not_accepted(drift_flow_spread_13m_decision(incremental_row))
    shadow = drift_flow_spread_shadow_spread4_decision(incremental_row)
    assert shadow is not None
    assert shadow.bot_name == BOT_DRIFT_FLOW_SPREAD_SHADOW_SPREAD4
    assert shadow.decision_status == RESEARCH_ONLY
    assert shadow.threshold_profile["incremental_to_core"] is True

    core_overlap = drift_flow_spread_shadow_spread4_decision(
        _row(spread_cents=2.0, spot_depth_trade_net_notional_60s=-10.0)
    )
    assert core_overlap is not None
    assert core_overlap.decision_status == RESEARCH_ONLY
    assert core_overlap.threshold_profile["incremental_to_core"] is False

    outside_spread = drift_flow_spread_shadow_spread4_decision(
        _row(
            spread_cents=4.001,
            spot_depth_trade_net_notional_15s=-10.0,
            spot_depth_trade_net_notional_60s=-10.0,
        )
    )
    assert outside_spread is not None
    assert outside_spread.decision_status == RESEARCH_ONLY
    assert outside_spread.threshold_profile["would_accept_variant"] is False
    assert outside_spread.threshold_profile["incremental_to_core"] is False

    outside_structure = drift_flow_spread_shadow_spread4_decision(
        _row(
            distance_sigma=3.001e-5,
            distance_from_strike=3.001e-5,
            spread_cents=3.0,
            spot_depth_trade_net_notional_60s=-10.0,
        )
    )
    assert outside_structure is not None
    assert outside_structure.decision_status == RESEARCH_ONLY
    assert outside_structure.threshold_profile["would_accept_variant"] is False
    assert outside_structure.threshold_profile["incremental_to_core"] is False


def test_flow15_shadow_adds_only_fresh_positive_15s_flow_and_is_never_accepted():
    incremental_row = _row(
        spread_cents=5.0,
        spot_depth_trade_net_notional_15s=0.01,
        spot_depth_trade_net_notional_60s=-10.0,
    )
    assert _not_accepted(drift_flow_spread_13m_decision(incremental_row))
    shadow = drift_flow_spread_shadow_flow15_decision(incremental_row)
    assert shadow is not None
    assert shadow.bot_name == BOT_DRIFT_FLOW_SPREAD_SHADOW_FLOW15
    assert shadow.decision_status == RESEARCH_ONLY
    assert shadow.threshold_profile["incremental_to_core"] is True

    core_overlap = drift_flow_spread_shadow_flow15_decision(_row())
    assert core_overlap is not None
    assert core_overlap.decision_status == RESEARCH_ONLY
    assert core_overlap.threshold_profile["incremental_to_core"] is False

    zero_flow = drift_flow_spread_shadow_flow15_decision(
        _row(
            spread_cents=5.0,
            spot_depth_trade_net_notional_15s=0.0,
            spot_depth_trade_net_notional_60s=-10.0,
        )
    )
    assert zero_flow is not None
    assert zero_flow.decision_status == RESEARCH_ONLY
    assert zero_flow.threshold_profile["would_accept_variant"] is False
    assert zero_flow.threshold_profile["incremental_to_core"] is False

    stale_flow = drift_flow_spread_shadow_flow15_decision(
        _row(
            spread_cents=5.0,
            spot_depth_trade_net_notional_15s=10.0,
            spot_depth_trade_net_notional_60s=-10.0,
            spot_depth_trade_age_seconds=15.001,
        )
    )
    assert stale_flow is not None
    assert stale_flow.decision_status == RESEARCH_ONLY
    assert stale_flow.threshold_profile["would_accept_variant"] is False
    assert stale_flow.threshold_profile["incremental_to_core"] is False


def test_checkpoint_quarantine_is_interval_specific_and_latequal_stays_research():
    twelve = drift_addon_requal_decision(_checkpoint_row(interval="12M"))
    eleven = drift_addon_requal_decision(_checkpoint_row(interval="11M"))
    assert twelve is not None and twelve.decision_status == ACCEPTED
    assert eleven is not None and eleven.decision_status == RESEARCH_ONLY
    assert "11M" in _policy_text(eleven)

    for interval in ("12M", "11M"):
        late = drift_latequal_decision(
            _checkpoint_row(
                record_kind="DRIFT_LATEQUAL",
                interval=interval,
                drift_base_ask_cents=None,
                drift_ask13_cents=55.0,
                drift_rule_version="drift-latequal-12m-11m-v1",
            )
        )
        assert late is not None and late.decision_status == RESEARCH_ONLY
