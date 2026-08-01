from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v14 as v14
from q15_upgrade.strategy_bots import rti_microstructure_v14_identity as identity
from tools import q15_rti_microstructure_freeze as freeze
from tools import q15_rti_microstructure_preregister as preregister


ROOT = Path(__file__).resolve().parents[1]
CHARTER = ROOT / "config" / "q15_rti_v14_successor_preregistration.json"
DESIGN = ROOT / "config" / "q15_rti_microstructure_design_v14.json"
PROTOCOL = ROOT / "config" / "q15_rti_v14_walk_forward_protocol.json"
REPORTING = ROOT / "config" / "q15_rti_v14_reporting_protocol.json"
CALIBRATION = ROOT / "config" / "q15_rti_v14_calibration_reporting_protocol.json"
VALUE_CURVE = ROOT / "config" / "q15_rti_v14_selective_value_curve_protocol.json"
NON_BTC = ("BNB", "DOGE", "ETH", "HYPE", "SOL", "XRP")


def _sha(value) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def _examples(*, inverse: bool = False, windows: int = 24):
    rows = []
    row_id = 0
    for window in range(windows):
        close = 10_000.0 + 900.0 * window
        for asset_index, asset in enumerate(NON_BTC):
            row_id += 1
            label = int((window + asset_index) % 2 == 0)
            predicted_yes = (0.8 if label else 0.2)
            if inverse:
                predicted_yes = 1.0 - predicted_yes
            rows.append({
                "id": row_id,
                "asset": asset,
                "close_time": close,
                "features": [float(window), float(asset_index)],
                "feature_names": ["window", "asset"],
                "market_yes_probability": 0.5,
                "label_yes": label,
                "synthetic_base_probability": predicted_yes,
            })
    return rows


def test_v14_preregistration_hashes_and_boundaries_are_immutable():
    charter = json.loads(CHARTER.read_text(encoding="utf-8"))
    design = json.loads(DESIGN.read_text(encoding="utf-8"))
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    assert _sha(charter) == identity.CHARTER_SHA256
    assert _sha(design) == identity.DESIGN_SHA256
    assert _sha(protocol) == identity.EVALUATION_PROTOCOL_SHA256
    assert design["prospective_after_close_time"] == 1784742300.0
    assert design["first_eligible_close_time"] == 1784743200.0
    assert design["source_v13_outcome_labels_read"] is False
    assert design["opened_v11_untouched_test_used"] is False
    preregister.validate_design(design)
    freeze.validate_walk_forward_protocol(protocol, design)


def test_v14_reporting_chain_is_frozen_before_any_outcome_review():
    design = json.loads(DESIGN.read_text(encoding="utf-8"))
    reporting = json.loads(REPORTING.read_text(encoding="utf-8"))
    calibration = json.loads(CALIBRATION.read_text(encoding="utf-8"))
    value_curve = json.loads(VALUE_CURVE.read_text(encoding="utf-8"))
    assert _sha(reporting) == identity.REPORTING_PROTOCOL_SHA256
    assert _sha(calibration) == identity.CALIBRATION_REPORTING_PROTOCOL_SHA256
    assert _sha(value_curve) == identity.SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256
    assert reporting["outcome_labels_used_for_protocol"] is False
    assert calibration["outcome_labels_used_for_protocol"] is False
    assert value_curve["outcome_labels_used_for_protocol"] is False
    freeze.validate_reporting_protocol(reporting, design)
    freeze.validate_calibration_reporting_protocol(calibration, design)
    freeze.validate_selective_value_curve_protocol(value_curve, design)


def test_v14_changes_no_v13_feature_formula(monkeypatch):
    base = {
        "available": True,
        "features": [float(index) for index in range(len(v14.FEATURE_NAMES))],
        "feature_names": list(v14.FEATURE_NAMES),
        "market_yes_probability": 0.61,
    }
    monkeypatch.setattr(v14.v13, "feature_vector", lambda row: dict(base))
    old = v14.feature_vector({
        "asset": "BTC", "close_time": v14.PROSPECTIVE_AFTER_CLOSE_TIME,
    })
    assert old == {"available": False, "error": "pre_v14_prospective_boundary"}
    new = v14.feature_vector({
        "asset": "BTC", "close_time": v14.FIRST_ELIGIBLE_CLOSE_TIME,
    })
    assert new["features"] == base["features"]
    assert new["feature_names"] == base["feature_names"]
    assert new["feature_formulas_identical_to_v13"] is True
    assert new["design_id"] == identity.DESIGN_ID


def test_factor_zero_is_exact_market_probability():
    for market in (0.01, 0.2, 0.5, 0.8, 0.99):
        assert freeze.blend_residual_probability(
            market, 1.0 - market, 0.0,
        ) == pytest.approx(market, abs=0.0)


def test_nested_selector_uses_only_inner_oof_and_can_choose_signal(monkeypatch):
    design = json.loads(DESIGN.read_text(encoding="utf-8"))
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    monkeypatch.setattr(freeze, "fit_residual_model", lambda rows, config: {})
    monkeypatch.setattr(
        freeze,
        "predict_probabilities",
        lambda model, rows, config: (
            [float(row["synthetic_base_probability"]) for row in rows],
            [{"out_of_distribution": False} for _ in rows],
        ),
    )
    selected = freeze.select_residual_trust_factor(
        _examples(), design["fixed_training_config"], protocol,
        "NON_BTC_TRANSFER",
    )
    assert selected["selected_factor"] > 0.0
    assert selected["market_fallback_selected"] is False
    assert selected["outer_validation_labels_used_for_selection"] is False
    assert selected["calibration_labels_used_for_selection"] is False
    assert selected["untouched_test_labels_used_for_selection"] is False
    assert selected["inner_oof"]["oof_close_windows"] == 12


def test_nested_selector_falls_back_exactly_to_market_when_signal_harms(monkeypatch):
    design = json.loads(DESIGN.read_text(encoding="utf-8"))
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    monkeypatch.setattr(freeze, "fit_residual_model", lambda rows, config: {})
    monkeypatch.setattr(
        freeze,
        "predict_probabilities",
        lambda model, rows, config: (
            [float(row["synthetic_base_probability"]) for row in rows],
            [{"out_of_distribution": False} for _ in rows],
        ),
    )
    selected = freeze.select_residual_trust_factor(
        _examples(inverse=True), design["fixed_training_config"], protocol,
        "NON_BTC_TRANSFER",
    )
    assert selected["selected_factor"] == 0.0
    assert selected["market_fallback_selected"] is True
    assert not any(
        row["eligible"] for row in selected["candidates"]
        if row["factor"] > 0.0
    )


def test_outer_walk_forward_reselects_trust_inside_each_training_fold(monkeypatch):
    design = json.loads(DESIGN.read_text(encoding="utf-8"))
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    monkeypatch.setattr(freeze, "fit_residual_model", lambda rows, config: {})
    monkeypatch.setattr(
        freeze,
        "predict_probabilities",
        lambda model, rows, config: (
            [float(row["synthetic_base_probability"]) for row in rows],
            [{"out_of_distribution": False} for _ in rows],
        ),
    )
    report = freeze.expanding_walk_forward_gate(
        _examples(windows=48), design["fixed_training_config"], protocol,
        "NON_BTC_TRANSFER",
    )
    assert report["met"] is True
    assert [fold["fold"] for fold in report["folds"]] == [1, 2, 3]
    assert all(
        fold["selected_residual_trust_factor"] > 0.0
        for fold in report["folds"]
    )
    assert all(
        fold["residual_trust_selection"][
            "outer_validation_labels_used_for_selection"
        ] is False
        for fold in report["folds"]
    )


def test_inner_selector_rejects_partial_same_close_asset_set(monkeypatch):
    design = json.loads(DESIGN.read_text(encoding="utf-8"))
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    rows = _examples()[1:]
    with pytest.raises(
        ValueError, match="residual_trust_inner_same_close_asset_leakage",
    ):
        freeze.select_residual_trust_factor(
            rows, design["fixed_training_config"], protocol,
            "NON_BTC_TRANSFER",
        )
