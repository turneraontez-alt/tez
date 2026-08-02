from __future__ import annotations

import copy
from pathlib import Path

import pytest

from q15_upgrade.strategy_bots import (
    rti_microstructure_v21_paper_identity as identity,
)
from tools import q15_rti_v21_paper_preregister as preregister
from tools.q15_rti_microstructure_preregister import design_fingerprint


def test_frozen_v21_paper_protocol_validates_outcome_blind():
    protocol = preregister.load_protocol()
    result = preregister.validate_protocol(protocol)
    assert design_fingerprint(protocol) == identity.PROTOCOL_SHA256
    assert result == {
        "status": "VALID_OUTCOME_BLIND_V21_PAPER_DEPLOYMENT_PROTOCOL",
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "artifact_version": identity.ARTIFACT_VERSION,
        "ledger_version": identity.LEDGER_VERSION,
        "review_bars": [30, 60, 150],
        "v21_eligible_rows_before_freeze": 0,
        "outcome_labels_read": False,
        "artifact_created": False,
        "runtime_scoring_connected": False,
        "notifications_enabled": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }


@pytest.mark.parametrize(
    ("section", "key", "value"),
    (
        ("outcome_blind_freeze_disclosure", "v21_eligible_rows_before_freeze", 1),
        ("activation_prerequisites", "historical_results_alone_can_promote", True),
        ("prospective_boundary", "historical_credit_allowed", True),
        ("artifact_manifest", "automatic_refit_forbidden", False),
        ("runtime_population", "cohort_mixing_forbidden", False),
        ("evidence_and_timestamp_gate", "reused_parent_or_intermediate_quote_for_decision_forbidden", False),
        ("probability_and_entry_policy", "slippage_cents_per_contract", 0.0),
        ("durable_ledger", "decision_identity_unique", False),
        ("telegram_delivery", "durable_outbox_required", False),
        ("settlement_grading", "conflicting_results_fail_closed", False),
        ("health_contract", "invalid_artifact_or_stale_source_disables_scoring_and_notification", False),
        ("prospective_reviews", "automatic_promotion", True),
        ("safety", "protocol_allows_real_trading", True),
    ),
)
def test_any_frozen_v21_paper_protocol_tamper_fails(section, key, value):
    protocol = copy.deepcopy(preregister.load_protocol())
    protocol[section][key] = value
    with pytest.raises(ValueError, match="sha256_mismatch"):
        preregister.validate_protocol(protocol)


def test_v21_paper_protocol_freezes_exact_ledger_review_and_entry_contract():
    protocol = preregister.load_protocol()
    boundary = protocol["prospective_boundary"]
    entry = protocol["probability_and_entry_policy"]
    ledger = protocol["durable_ledger"]
    reviews = protocol["prospective_reviews"]
    settlement = protocol["settlement_grading"]

    assert boundary["historical_credit_allowed"] is False
    assert boundary["partial_activation_window_forbidden"] is True
    assert entry["simulation_contracts"] == 10
    assert entry["slippage_cents_per_contract"] == 2.0
    assert entry["fake_or_partial_fill_assumptions_forbidden"] is True
    assert ledger["ledger_version"] == identity.LEDGER_VERSION
    assert ledger["one_ledger_per_cohort"] is True
    assert ledger["cross_cohort_rows_in_same_ledger_forbidden"] is True
    assert ledger["default_relative_paths"] == identity.DEFAULT_LEDGER_RELATIVE_PATHS
    assert ledger["decision_insert_precedes_notification_enqueue"] is True
    assert ledger["duplicate_decision_returns_existing_row_without_rescore"] is True
    assert ledger["historical_rows_may_not_be_inserted"] is True
    assert reviews["review_bars_resolved_accepted_picks"] == [30, 60, 150]
    assert reviews["review_population_may_not_be_subselected"] is True
    assert reviews["cohorts_reviewed_separately"] is True
    assert reviews["promotion_gates"]["candidate_log_loss_better_than_market"] is True
    assert reviews["promotion_gates"]["candidate_log_loss_better_than_v20_ablation"] is True
    assert settlement["source_id"] == "KALSHI_PUBLIC_MARKET_API"
    assert settlement["required_market_status"] == "finalized"
    assert settlement["settled_result_may_not_be_overwritten"] is True
    assert settlement["pnl_contracts"] == 10


def test_v21_paper_preregister_has_no_runtime_or_outcome_capability():
    source = Path(preregister.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "import sqlite3",
        "from sqlite3",
        "KalshiClient",
        "V3Telegram",
        "send_message(",
        "place_order(",
        "fit_model",
        "predict_probabilities",
        "SELECT official_result",
    ):
        assert forbidden not in source
    assert identity.PAPER_ARTIFACT_CREATED is False
    assert identity.RUNTIME_SCORING_CONNECTED is False
    assert identity.NOTIFICATIONS_ENABLED is False
    assert identity.AUTOMATIC_PROMOTION is False
    assert identity.REAL_TRADING_ALLOWED is False
