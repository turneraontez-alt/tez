from __future__ import annotations

import sqlite3

import pytest

from q15_upgrade.strategy_bots import costs
from q15_upgrade.strategy_bots import rti_microstructure_v21_paper_ledger as ledger_module


ARTIFACT_SHA = "a" * 64
ARTIFACT_CREATED_AT = 1000.0
FIRST_CLOSE = 1800.0


def _ledger(tmp_path, cohort="NON_BTC_TRANSFER"):
    return ledger_module.V21PaperLedger(
        tmp_path / f"{cohort}.sqlite3",
        cohort=cohort,
        artifact_sha256=ARTIFACT_SHA,
        artifact_created_at_unix=ARTIFACT_CREATED_AT,
        prospective_after_close_time=FIRST_CLOSE,
    )


def _decision(
    *, asset="ETH", close_time=FIRST_CLOSE, status=ledger_module.ACCEPTED_PAPER,
    parent_id=1, intermediate_id=2, delayed_id=3, side="YES",
):
    decision_timestamp = close_time - 720.0
    return {
        "cohort": "BTC" if asset == "BTC" else "NON_BTC_TRANSFER",
        "asset": asset,
        "ticker": f"KX{asset}15M-TEST",
        "close_time": close_time,
        "decision_timestamp": decision_timestamp,
        "created_at": decision_timestamp + 0.5,
        "parent_id": parent_id,
        "intermediate_id": intermediate_id,
        "delayed_id": delayed_id,
        "source_feature_evidence_sha256": "b" * 64,
        "feature_evidence_sha256": "c" * 64,
        "feature_vector_sha256": "d" * 64,
        "market_side_probability": 0.55,
        "candidate_survival_probability": 0.68,
        "v20_ablation_survival_probability": 0.60,
        "side": side,
        "decision_status": status,
        "reason_codes": ["EDGE_PASSED", "FULL_FILL_SUPPORTED"],
        "selected_margin": 0.03,
        "entry_ask_cents": 54.0,
        "spread_cents": 1.0,
        "displayed_depth_contracts": 15.0,
        "simulated_contracts": 10,
        "slippage_cents_per_contract": 2.0,
        "fee_schedule_version": costs.KALSHI_Q15_FEE_SCHEDULE_VERSION,
    }


def test_v21_paper_ledger_rejects_historical_wrong_cohort_and_fake_fill(tmp_path):
    ledger = _ledger(tmp_path)
    historical = _decision(close_time=FIRST_CLOSE - 900.0)
    with pytest.raises(ValueError, match="not_prospective"):
        ledger.insert_decision(historical)

    wrong_cohort = _decision(asset="BTC")
    with pytest.raises(ValueError, match="cohort_asset_invalid"):
        ledger.insert_decision(wrong_cohort)

    shallow = _decision()
    shallow["displayed_depth_contracts"] = 9.99
    with pytest.raises(ValueError, match="accepted_fill_invalid"):
        ledger.insert_decision(shallow)

    weak_edge = _decision()
    weak_edge["candidate_survival_probability"] = 0.58
    with pytest.raises(ValueError, match="accepted_edge_invalid"):
        ledger.insert_decision(weak_edge)

    late_backfill = _decision()
    late_backfill["created_at"] = late_backfill["decision_timestamp"] + 31.0
    with pytest.raises(ValueError, match="historical_insert_forbidden"):
        ledger.insert_decision(late_backfill)
    assert ledger.health(now=FIRST_CLOSE - 1)["decision_status_counts"] == {}


def test_v21_paper_decision_is_idempotent_immutable_and_outbox_is_durable(tmp_path):
    ledger = _ledger(tmp_path)
    decision = _decision()
    inserted = ledger.insert_decision(decision, notify=True)
    assert inserted["decision_status"] == ledger_module.ACCEPTED_PAPER
    replay = ledger.insert_decision(decision, notify=True)
    assert replay["decision_key"] == inserted["decision_key"]
    assert ledger.health(now=FIRST_CLOSE - 1)["duplicate_decision_attempt_count"] == 1

    changed = dict(decision)
    changed["candidate_survival_probability"] = 0.69
    with pytest.raises(ValueError, match="duplicate_decision_conflict"):
        ledger.insert_decision(changed, notify=True)
    assert ledger.health(now=FIRST_CLOSE - 1)["decision_identity_conflict_count"] == 1

    with sqlite3.connect(ledger.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable_decision_mutation"):
            connection.execute(
                "UPDATE decisions SET side='NO' WHERE decision_key=?",
                (inserted["decision_key"],),
            )

    claims = ledger.claim_notifications(owner="worker-a", now=1100.0)
    assert len(claims) == 1
    assert claims[0]["status"] == "QUEUED"
    assert claims[0]["message"].startswith("V21 PAPER\n")
    assert "paper_only_no_real_order: true" in claims[0]["message"]
    assert ledger.claim_notifications(owner="worker-b", now=1101.0) == []
    sent = ledger.complete_notification(
        inserted["decision_key"], owner="worker-a", message_id="telegram-1",
        notified_at=1102.0,
    )
    assert sent["status"] == "SENT"
    assert ledger.complete_notification(
        inserted["decision_key"], owner="worker-a", message_id="telegram-1",
        notified_at=1103.0,
    )["status"] == "SENT"
    with pytest.raises(ValueError, match="terminal_conflict"):
        ledger.complete_notification(
            inserted["decision_key"], owner="worker-a", message_id="different",
            notified_at=1103.0,
        )


def test_v21_paper_outbox_expires_and_dead_letters_without_duplicate_send(tmp_path):
    ledger = _ledger(tmp_path)
    first = ledger.insert_decision(_decision(parent_id=10, intermediate_id=11, delayed_id=12), notify=True)
    assert ledger.claim_notifications(owner="late", now=FIRST_CLOSE + 1) == []
    health = ledger.health(now=FIRST_CLOSE + 1)
    assert health["notification_state_counts"] == {"EXPIRED": 1}

    second_decision = _decision(
        close_time=FIRST_CLOSE + 900, parent_id=20, intermediate_id=21,
        delayed_id=22,
    )
    second = ledger.insert_decision(second_decision, notify=True)
    claim = ledger.claim_notifications(owner="worker", now=2000.0)
    assert [row["decision_key"] for row in claim] == [second["decision_key"]]
    assert ledger.fail_notification(
        second["decision_key"], owner="worker", error="network", max_attempts=1,
    )["status"] == "DEAD_LETTER"
    assert ledger.claim_notifications(owner="worker", now=2001.0) == []
    assert first["decision_key"] != second["decision_key"]


def test_v21_paper_settlement_is_authoritative_compare_and_set_with_real_costs(tmp_path):
    ledger = _ledger(tmp_path)
    accepted = ledger.insert_decision(_decision(), notify=False)
    graded = ledger.grade_decision(
        accepted["decision_key"], result="YES", market_status="finalized",
        returned_ticker=accepted["ticker"], returned_close_time=FIRST_CLOSE,
        fetched_at=FIRST_CLOSE + 2.0,
    )
    expected_per_contract = costs.rti_simulated_net_pnl_cents(54.0, True, 10, 2.0)
    assert graded["correct"] == 1
    assert graded["fee_slippage_adjusted_pnl_cents"] == pytest.approx(
        expected_per_contract * 10,
    )
    # A later fetch of the same final result is idempotent and cannot replace
    # the original evidence timestamp/hash.
    replay = ledger.grade_decision(
        accepted["decision_key"], result="YES", market_status="finalized",
        returned_ticker=accepted["ticker"], returned_close_time=FIRST_CLOSE,
        fetched_at=FIRST_CLOSE + 30.0,
    )
    assert replay["settled_at"] == graded["settled_at"]
    assert replay["settlement_evidence_sha256"] == graded["settlement_evidence_sha256"]
    with pytest.raises(ValueError, match="overwrite_forbidden"):
        ledger.grade_decision(
            accepted["decision_key"], result="NO", market_status="finalized",
            returned_ticker=accepted["ticker"], returned_close_time=FIRST_CLOSE,
            fetched_at=FIRST_CLOSE + 40.0,
        )
    assert ledger.health(now=FIRST_CLOSE + 50)["settlement_conflict_count"] == 1

    rejected_row = _decision(
        status="REJECTED_EDGE_POLICY", parent_id=30, intermediate_id=31,
        delayed_id=32,
    )
    rejected = ledger.insert_decision(rejected_row)
    rejected_grade = ledger.grade_decision(
        rejected["decision_key"], result="NO", market_status="finalized",
        returned_ticker=rejected["ticker"], returned_close_time=FIRST_CLOSE,
        fetched_at=FIRST_CLOSE + 3.0,
    )
    assert rejected_grade["correct"] == 0
    assert rejected_grade["fee_slippage_adjusted_pnl_cents"] is None
    health = ledger.health(now=FIRST_CLOSE + 100)
    assert health["resolved_accepted_pick_count"] == 1
    assert health["next_manual_review_bar"] == 30
    assert health["paper_only"] is True
    assert health["automatic_promotion"] is False
    assert health["real_trading_allowed"] is False


def test_v21_paper_ledger_metadata_is_insert_once(tmp_path):
    ledger = _ledger(tmp_path)
    same = _ledger(tmp_path)
    assert same.health(now=0)["artifact_sha256"] == ARTIFACT_SHA
    with pytest.raises(ValueError, match="metadata_conflict"):
        ledger_module.V21PaperLedger(
            ledger.path,
            cohort="NON_BTC_TRANSFER",
            artifact_sha256="f" * 64,
            artifact_created_at_unix=ARTIFACT_CREATED_AT,
            prospective_after_close_time=FIRST_CLOSE,
        )
