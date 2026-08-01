from __future__ import annotations

import json
import math

import pytest

from q15_upgrade.strategy_bots.costs import (
    KALSHI_Q15_FEE_SCHEDULE_VERSION,
    RTI_EXECUTION_COST_MODEL_VERSION,
    kalshi_order_fee_cents,
    rti_simulated_execution,
    rti_simulated_net_pnl_cents,
)
from q15_upgrade.strategy_bots.ledger import StrategyBotLedger
from q15_upgrade.strategy_bots.rules import (
    BOT_RTI_PATH_13M,
    RTI_PATH_13M_IMPULSE_CHALLENGER_ID,
)


def _settled_row(**overrides):
    row = {
        "id": 1,
        "created_at": 100.0,
        "close_time": 200.0,
        "side": "YES",
        "official_result": "YES",
        "correct": 1,
        "entry_ask_cents": 60.0,
        # Value produced by the superseded quote-fee-plus-detached-slip path.
        "hypothetical_pnl_cents": 36.32,
        "threshold_json": json.dumps({
            "sim_contracts": 10,
            "slippage_cents_per_contract": 2.0,
        }),
    }
    row.update(overrides)
    return row


def test_q15_standard_fee_matches_published_centicent_examples():
    assert kalshi_order_fee_cents(60.0, 10) == pytest.approx(16.8)
    assert kalshi_order_fee_cents(60.0, 100) == pytest.approx(168.0)
    assert kalshi_order_fee_cents(0.0, 10) == 0.0
    assert kalshi_order_fee_cents(100.0, 10) == 0.0


@pytest.mark.parametrize(
    ("price_cents", "published_fee_cents_for_100"),
    [
        (1, 7), (5, 34), (10, 63), (15, 90), (20, 112), (25, 132),
        (30, 147), (35, 160), (40, 168), (45, 174), (50, 175),
        (55, 174), (60, 168), (65, 160), (70, 147), (75, 132),
        (80, 112), (85, 90), (90, 63), (95, 34), (99, 7),
    ],
)
def test_q15_fee_matches_every_published_100_contract_table_row(
    price_cents, published_fee_cents_for_100,
):
    # Official Kalshi fee schedule, effective July 7, 2026, pages 3-4.  The
    # governing formula is centicent-precise; the table displays that result
    # rounded up to the next whole cent (for example 173.25c as $1.74).
    exact_centicent_fee = kalshi_order_fee_cents(price_cents, 100)
    assert exact_centicent_fee is not None
    assert math.ceil(exact_centicent_fee - 1e-9) == (
        published_fee_cents_for_100
    )


def test_rti_slippage_is_a_fee_bearing_fill_price():
    execution = rti_simulated_execution(60.0, 10, 2.0)
    assert execution is not None
    assert execution["simulated_fill_cents"] == pytest.approx(62.0)
    assert execution["fee_cents_per_order"] == pytest.approx(16.5)
    assert execution["fee_cents_per_contract"] == pytest.approx(1.65)
    assert execution["fee_slippage_breakeven_rate"] == pytest.approx(0.6365)
    assert execution["fee_schedule_version"] == KALSHI_Q15_FEE_SCHEDULE_VERSION
    assert (
        execution["execution_cost_model_version"]
        == RTI_EXECUTION_COST_MODEL_VERSION
    )
    assert rti_simulated_net_pnl_cents(60.0, True, 10, 2.0) == pytest.approx(
        36.35
    )
    assert rti_simulated_net_pnl_cents(60.0, False, 10, 2.0) == pytest.approx(
        -63.65
    )


def test_rti_aggregate_reconstructs_current_costs_without_mutating_stored_pnl():
    metrics = StrategyBotLedger._agg([_settled_row()], min_n=30)
    assert metrics["resolved"] == 1
    assert metrics["pnl_scoreable_resolved"] == 1
    assert metrics["unscoreable_resolved"] == 0
    assert metrics["cost_evidence_complete"] is True
    assert metrics["fee_adjusted_net_pnl_cents"] == pytest.approx(36.35)
    assert metrics["stored_net_pnl_cents"] == pytest.approx(36.32)
    assert metrics["cost_audit_delta_cents"] == pytest.approx(0.03)
    assert metrics["avg_fee_adjusted_breakeven_rate"] == pytest.approx(0.6168)
    assert metrics["avg_fee_slippage_adjusted_breakeven_rate"] == pytest.approx(
        0.6365
    )


def test_rti_aggregate_fails_cost_evidence_closed_when_entry_is_missing():
    metrics = StrategyBotLedger._agg(
        [_settled_row(entry_ask_cents=None, hypothetical_pnl_cents=None)],
        min_n=30,
    )
    assert metrics["resolved"] == 1
    assert metrics["pnl_scoreable_resolved"] == 0
    assert metrics["unscoreable_resolved"] == 1
    assert metrics["cost_evidence_complete"] is False
    assert metrics["fee_adjusted_net_pnl_cents"] is None


def test_rti_aggregate_detects_stored_label_mismatch_and_fails_closed():
    metrics = StrategyBotLedger._agg([_settled_row(correct=0)], min_n=30)
    assert metrics["correct"] == 1
    assert metrics["accuracy"] == 1.0
    assert metrics["label_integrity_failures"] == 1
    assert metrics["cost_evidence_complete"] is False


def test_same_side_challenger_uses_its_own_entry_quote_not_parent_quote():
    row = _settled_row(
        asset="BTC",
        ticker="KXBTC15M-TEST",
        interval="13M",
        bot_name=BOT_RTI_PATH_13M,
        decision_status="REJECTED",
        threshold_json=json.dumps({
            "sim_contracts": 10,
            "slippage_cents_per_contract": 2.0,
            "challengers": {
                RTI_PATH_13M_IMPULSE_CHALLENGER_ID: {
                    "accepted": True,
                    "side_override": "YES",
                    "entry_ask_cents": 50.0,
                    "notification_eligible": True,
                    "criteria": {"policy_version": "test-policy"},
                }
            },
        }),
    )
    system = StrategyBotLedger._rti_path_challenger_system([row], min_n=30)
    metrics = system["books"][RTI_PATH_13M_IMPULSE_CHALLENGER_ID]["overall"]
    # A 50c quote + 2c slippage gives a 52c fill and a 1.748c fee.
    assert metrics["fee_adjusted_net_pnl_cents"] == pytest.approx(46.252)
    assert metrics["avg_fee_slippage_adjusted_breakeven_rate"] == pytest.approx(
        0.53748
    )
