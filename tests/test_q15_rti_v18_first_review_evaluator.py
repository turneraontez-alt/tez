from __future__ import annotations

from copy import deepcopy

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v18_audit_identity as identity
from q15_upgrade.strategy_bots import rti_microstructure_v18_identity as v18_identity
from tools import q15_rti_v18_first_review_evaluator as evaluator


def _rows() -> list[dict]:
    rows = []
    for row_id in range(1, 61):
        side = "YES" if row_id % 2 else "NO"
        candidate = row_id <= 30
        correct = candidate and row_id not in {5, 10, 15, 20, 25}
        label_yes = int((side == "YES") == correct)
        rows.append({
            "id": row_id,
            "asset": ("ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")[(row_id - 1) % 6],
            "close_time": float(1_900_000_000 + (row_id - 1) * 900),
            "side": side,
            "entry_ask_cents": 50.0,
            "sim_full_fill_supported": True,
            "reversal_risk_class": "low" if candidate else "medium",
            "settlement_average_risk_class": "low",
            "path_regime_class": "trend",
            "label_yes": label_yes,
        })
    return rows


def _seal(rows: list[dict]) -> dict:
    candidate = tuple(range(1, 31))
    control = tuple(range(1, 61))
    return {
        "protocol_id": v18_identity.PROTOCOL_ID,
        "protocol_sha256": v18_identity.PROTOCOL_SHA256,
        "seal_sha256": "synthetic-seal",
        "selected_complete_close_windows": 150,
        "selected_candidate_row_ids_sha256": evaluator._canonical_sha256(candidate),
        "selected_control_row_ids_sha256": evaluator._canonical_sha256(control),
    }


def test_cost_model_uses_two_cent_slippage_and_official_fee():
    row = _rows()[0]
    scored = evaluator._scored(row)
    assert scored["fill_cents"] == 52.0
    assert scored["fee_cents_per_contract"] > 0.0
    assert scored["break_even_probability"] > 0.52


def test_first_review_reports_control_counterfactual_and_passes_strong_data(monkeypatch):
    rows = _rows()
    artifact = _seal(rows)
    monkeypatch.setattr(evaluator.prospective_seal, "validate_seal", lambda value: None)
    report = evaluator.evaluate_first_review(
        rows, candidate_ids=range(1, 31), seal=artifact,
    )
    assert report["gate_met"] is True
    assert report["candidate"]["metrics"]["picks"] == 30
    assert report["strict_control"]["metrics"]["picks"] == 60
    assert report["rejected_trade_counterfactual"]["metrics"]["picks"] == 30
    assert report["candidate"]["metrics"]["accuracy"] > report[
        "strict_control"
    ]["metrics"]["accuracy"]
    assert report["candidate"]["clustered_bootstrap"][
        "mean_pnl_cents_10_contracts"
    ]["one_sided_lower_90"] > 0.0
    assert report["candidate"]["clustered_bootstrap"][
        "pick_level_mean_recomputed_after_cluster_resampling"
    ] is True
    assert report["notification_eligible"] is False
    assert report["real_trading_allowed"] is False


def test_candidate_identity_tampering_fails(monkeypatch):
    rows = _rows()
    artifact = _seal(rows)
    monkeypatch.setattr(evaluator.prospective_seal, "validate_seal", lambda value: None)
    with pytest.raises(ValueError, match="input_identity"):
        evaluator.evaluate_first_review(
            rows, candidate_ids=range(2, 31), seal=artifact,
        )


def test_supplied_contract_tampering_fails(monkeypatch):
    rows = _rows()
    artifact = _seal(rows)
    contract = deepcopy(evaluator.prospective_seal.load_contract())
    contract["gate"]["candidate_resolved_picks_minimum"] = 1
    monkeypatch.setattr(evaluator.prospective_seal, "validate_seal", lambda value: None)
    with pytest.raises(ValueError, match="contract_identity"):
        evaluator.evaluate_first_review(
            rows, candidate_ids=range(1, 31), seal=artifact, contract=contract,
        )


def test_cluster_bootstrap_observed_mean_preserves_pick_weighting():
    rows = _rows()[:3]
    rows[0]["close_time"] = 1_900_000_000.0
    rows[1]["close_time"] = 1_900_000_000.0
    rows[2]["close_time"] = 1_900_000_900.0
    scored = [evaluator._scored(row) for row in rows]
    expected_pnl = sum(
        float(row["pnl_cents_10_contracts"]) for row in scored
    ) / len(scored)
    expected_edge = sum(
        (1.0 if row["correct"] else 0.0)
        - float(row["break_even_probability"])
        for row in scored
    ) / len(scored)
    report = evaluator.clustered_bootstrap(rows, resamples=10000, seed=7)
    assert report["mean_pnl_cents_10_contracts"]["observed_mean"] == pytest.approx(
        expected_pnl
    )
    assert report["accuracy_minus_break_even"]["observed_mean"] == pytest.approx(
        expected_edge
    )
