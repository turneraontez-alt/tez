from __future__ import annotations

from copy import deepcopy

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v18_identity as v18_identity
from q15_upgrade.strategy_bots import rti_microstructure_v19_identity as v19_identity
from tools import q15_rti_v19_first_review_evaluator as evaluator


def _rows() -> list[dict]:
    rows = []
    for parent_id in range(1, 61):
        side = "YES" if parent_id % 2 else "NO"
        candidate = parent_id <= 30
        correct = candidate and parent_id not in {5, 20}
        label_yes = int((side == "YES") == correct)
        rows.append({
            "parent_id": parent_id,
            "delayed_id": 1000 + parent_id,
            "asset": ("ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")[(parent_id - 1) % 6],
            "close_time": float(1_900_000_000 + (parent_id - 1) * 900),
            "side": side,
            "parent_entry_ask_cents": 55.0,
            "delayed_entry_ask_cents": 40.0,
            "parent_sim_full_fill_supported": True,
            "delayed_sim_full_fill_supported": True,
            "delayed_sim_contracts": 10,
            "absolute_distance_tier": "DISTANCE_1_TO_UNDER_3_BPS",
            "realized_volatility_tier": "VOLATILITY_1_TO_UNDER_3_BPS",
            "reversal_risk_class": "low" if candidate else "medium",
            "settlement_average_risk_class": "low",
            "path_regime_class": "trend",
            "label_yes": label_yes,
        })
    return rows


def _seal(rows: list[dict]) -> dict:
    candidate = tuple((row["parent_id"], row["delayed_id"]) for row in rows[:30])
    control = tuple(row["parent_id"] for row in rows)
    return {
        "protocol_id": v19_identity.PROTOCOL_ID,
        "protocol_sha256": v19_identity.PROTOCOL_SHA256,
        "parent_protocol_id": v18_identity.PROTOCOL_ID,
        "parent_protocol_sha256": v18_identity.PROTOCOL_SHA256,
        "seal_sha256": "synthetic-seal",
        "selected_complete_close_windows": 150,
        "selected_candidate_pair_ids_sha256": evaluator._canonical_sha256(candidate),
        "selected_control_parent_ids_sha256": evaluator._canonical_sha256(control),
    }


def test_cost_model_uses_distinct_timestamped_entries_and_official_fee():
    row = _rows()[0]
    candidate = evaluator._scored(row, entry="candidate_12m")
    control = evaluator._scored(row, entry="control_13m")
    assert candidate["fill_cents"] == 42.0
    assert control["fill_cents"] == 57.0
    assert candidate["fee_cents_per_contract"] > 0.0
    assert candidate["break_even_probability"] > 0.42


def test_first_review_reports_all_controls_and_passes_strong_data(monkeypatch):
    rows = _rows()
    artifact = _seal(rows)
    monkeypatch.setattr(evaluator.prospective_seal, "validate_seal", lambda value: None)
    pairs = [(row["parent_id"], row["delayed_id"]) for row in rows[:30]]
    report = evaluator.evaluate_first_review(
        rows, candidate_pair_ids=pairs, seal=artifact,
    )
    assert report["gate_met"] is True
    assert report["candidate"]["metrics"]["picks"] == 30
    assert report["v18_parent_control"]["metrics"]["picks"] == 60
    assert report["rejected_parent_control_counterfactual"]["metrics"]["picks"] == 30
    assert report["candidate"]["metrics"]["accuracy"] > report[
        "v18_parent_control"
    ]["metrics"]["accuracy"]
    assert report["candidate"]["clustered_bootstrap"][
        "mean_pnl_cents_10_contracts"
    ]["one_sided_lower_90"] > 0.0
    assert "by_distance_tier" in report["candidate"]["subgroups"]
    assert "by_volatility_tier" in report["candidate"]["subgroups"]
    assert report["notification_eligible"] is False
    assert report["real_trading_allowed"] is False


def test_candidate_pair_identity_tampering_fails(monkeypatch):
    rows = _rows()
    artifact = _seal(rows)
    monkeypatch.setattr(evaluator.prospective_seal, "validate_seal", lambda value: None)
    pairs = [(row["parent_id"], row["delayed_id"]) for row in rows[1:30]]
    with pytest.raises(ValueError, match="input_identity"):
        evaluator.evaluate_first_review(
            rows, candidate_pair_ids=pairs, seal=artifact,
        )


def test_missing_full_fill_evidence_fails_closed():
    row = _rows()[0]
    row["delayed_sim_full_fill_supported"] = False
    with pytest.raises(ValueError, match="execution_evidence"):
        evaluator._scored(row, entry="candidate_12m")


def test_supplied_contract_tampering_fails(monkeypatch):
    rows = _rows()
    artifact = _seal(rows)
    contract = deepcopy(evaluator.prospective_seal.load_contract())
    contract["gate"]["candidate_resolved_picks_minimum"] = 1
    monkeypatch.setattr(evaluator.prospective_seal, "validate_seal", lambda value: None)
    pairs = [(row["parent_id"], row["delayed_id"]) for row in rows[:30]]
    with pytest.raises(ValueError, match="contract_identity"):
        evaluator.evaluate_first_review(
            rows, candidate_pair_ids=pairs, seal=artifact, contract=contract,
        )


def test_cluster_bootstrap_preserves_pick_weighting():
    rows = _rows()[:3]
    rows[0]["close_time"] = 1_900_000_000.0
    rows[1]["close_time"] = 1_900_000_000.0
    rows[2]["close_time"] = 1_900_000_900.0
    scored = [evaluator._scored(row, entry="candidate_12m") for row in rows]
    expected_pnl = sum(
        float(row["pnl_cents_10_contracts"]) for row in scored
    ) / len(scored)
    report = evaluator.clustered_bootstrap(rows, resamples=10000, seed=7)
    assert report["mean_pnl_cents_10_contracts"]["observed_mean"] == pytest.approx(
        expected_pnl
    )
