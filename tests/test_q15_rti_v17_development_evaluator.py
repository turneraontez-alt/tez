from __future__ import annotations

from copy import deepcopy

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v14 as v14
from q15_upgrade.strategy_bots import rti_microstructure_v15 as v15
from q15_upgrade.strategy_bots import rti_microstructure_v16 as v16
from q15_upgrade.strategy_bots import rti_microstructure_v17 as v17
from q15_upgrade.strategy_bots import rti_microstructure_v17_audit_identity as identity
from tools import q15_rti_v17_development_evaluator as evaluator
from tools.q15_rti_microstructure_preregister import design_fingerprint


ASSETS = ("BNB", "DOGE", "ETH", "HYPE", "SOL", "XRP")


def _rows() -> list[dict]:
    rows = []
    row_id = 1
    for window in range(240):
        close_time = float(1_800_000_000 + window * 900)
        for asset_index, asset in enumerate(ASSETS):
            label = (window + asset_index) % 2
            base14 = [0.0] * len(v14.FEATURE_NAMES)
            base15 = [*base14, *([0.0] * 5)]
            base16 = [*base15, *([0.0] * 20)]
            candidate = [*base16, *([0.0] * 36)]
            rows.append({
                "id": row_id,
                "asset": asset,
                "close_time": close_time,
                "label_yes": label,
                "market_yes_probability": 0.55 if label else 0.45,
                "v14_feature_names": list(v14.FEATURE_NAMES),
                "v14_features": base14,
                "v15_feature_names": list(v15.FEATURE_NAMES),
                "v15_features": base15,
                "v16_feature_names": list(v16.FEATURE_NAMES),
                "v16_features": base16,
                "v17_feature_names": list(v17.FEATURE_NAMES),
                "v17_features": candidate,
            })
            row_id += 1
    return rows


def test_evaluator_contract_is_frozen_before_labels():
    contract = evaluator.load_contract()
    assert design_fingerprint(contract) == identity.EVALUATOR_CONTRACT_SHA256
    assert contract["outcome_labels_used_to_create_contract"] is False
    assert contract["label_access"]["btc_labels_forbidden"] is True
    assert contract["result_policy"]["notifications_allowed"] is False
    assert contract["result_policy"]["real_trading_allowed"] is False
    assert contract["trust_selection"]["fixed_factor_grid_by_architecture"]["V17"] == [
        0.0, 0.1, 0.25, 0.5, 0.75, 1.0,
    ]


def test_example_validation_rejects_btc_and_feature_tampering():
    rows = _rows()
    rows[0]["asset"] = "BTC"
    with pytest.raises(ValueError, match="same_close_asset_leakage"):
        evaluator._validate_examples(rows)

    rows = _rows()
    rows[0]["v17_features"][0] = 1.0
    with pytest.raises(ValueError, match="example_invalid"):
        evaluator._validate_examples(rows)


def test_clustered_bootstrap_is_deterministic():
    rows = _rows()[-180:]
    candidate = [0.8 if row["label_yes"] else 0.2 for row in rows]
    comparator = [float(row["market_yes_probability"]) for row in rows]
    first = evaluator.paired_comparator_bootstrap(
        rows, candidate, comparator, comparator_name="MARKET", seed=123,
        resamples=10000, confidence_level=0.9,
    )
    second = evaluator.paired_comparator_bootstrap(
        rows, candidate, comparator, comparator_name="MARKET", seed=123,
        resamples=10000, confidence_level=0.9,
    )
    assert first == second
    assert first["close_windows"] == 30
    assert first["loss_delta_direction"] == "V17_MINUS_MARKET"
    assert first["brier_delta"]["one_sided_upper"] < 0.0


def test_walk_forward_uses_identical_rows_and_never_future_partitions(monkeypatch):
    rows = _rows()

    def fake_predict(
        train, validation, *, feature_key, feature_names_key, config, trust_protocol,
    ):
        probability = {
            "v17_features": (0.80, 0.20),
            "v16_features": (0.64, 0.36),
            "v15_features": (0.62, 0.38),
            "v14_features": (0.60, 0.40),
        }[feature_key]
        predicted = [
            probability[0] if row["label_yes"] else probability[1]
            for row in validation
        ]
        return predicted, {
            "selected_factor": 1.0,
            "outer_validation_labels_used_for_selection": False,
            "calibration_labels_used_for_selection": False,
            "untouched_test_labels_used_for_selection": False,
        }, 0

    monkeypatch.setattr(evaluator, "_predict", fake_predict)
    monkeypatch.setattr(
        evaluator,
        "_architecture_configs",
        lambda protocol: {"V17": {}, "V16": {}, "V15": {}, "V14": {}},
    )
    result = evaluator.evaluate_development(
        rows,
        contract=evaluator.load_contract(),
        protocol=evaluator.load_protocol(),
    )
    assert result["gate_met"] is True
    assert result["walk_forward_validation_close_windows"] == 120
    assert result["walk_forward_validation_rows"] == 720
    assert result["future_calibration_rows_used"] == 0
    assert result["future_test_rows_used"] == 0
    assert result["candidate_market_v16_v15_v14_identical_rows"] is True
    assert result["accuracy_is_report_only"] is True
    assert result["notification_eligible"] is False
    assert result["real_trading_allowed"] is False


def test_supplied_contract_tampering_fails_before_evaluation():
    contract = deepcopy(evaluator.load_contract())
    contract["aggregate_comparison"][
        "candidate_minus_market_brier_mean_must_be_at_most"
    ] = 0.0
    with pytest.raises(ValueError, match="supplied_contract_identity"):
        evaluator.evaluate_development(
            _rows(), contract=contract, protocol=evaluator.load_protocol(),
        )
