from __future__ import annotations

import copy
import inspect
from pathlib import Path

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v14 as v14
from q15_upgrade.strategy_bots import rti_microstructure_v15 as v15
from tools import q15_rti_v15_design_binding as binding
from tools import q15_rti_v15_walk_forward as walk
from tools.q15_rti_microstructure_preregister import design_fingerprint


COHORT_COUNTS = {
    "NON_BTC_TRANSFER": (
        ("BNB", "DOGE", "ETH", "HYPE", "SOL", "XRP"),
        48,
    ),
    "BTC": (("BTC",), 120),
}


def _inputs():
    design = binding._load(
        binding.DEFAULT_DESIGN, "test_v15_design_root_not_object",
    )
    protocol = binding._load(
        binding.DEFAULT_PROTOCOL, "test_v15_protocol_root_not_object",
    )
    return design, protocol


def _examples(cohort: str) -> list[dict]:
    assets, windows = COHORT_COUNTS[cohort]
    output = []
    row_id = 0
    for window in range(windows):
        close = 10_000.0 + 900.0 * window
        for asset_index, asset in enumerate(assets):
            row_id += 1
            label = int((window + asset_index) % 2 == 0)
            base = [float(label), *([0.0] * 19)]
            output.append({
                "id": row_id,
                "close_time": close,
                "asset": asset,
                "label_yes": label,
                "market_yes_probability": 0.5,
                "v14_feature_names": list(v14.FEATURE_NAMES),
                "v14_features": base,
                "v15_feature_names": list(v15.FEATURE_NAMES),
                "v15_features": [*base, 0.1, 0.2, 0.3, 0.4, 0.5],
            })
    return output


def _patch_predictors(monkeypatch, *, candidate, control, factor=1.0):
    trust_calls = []

    def select(training, config, protocol, cohort):
        trust_calls.append({
            "cohort": cohort,
            "feature_count": len(training[0]["features"]),
            "max_close_time": max(
                float(row["close_time"]) for row in training
            ),
            "row_ids": tuple(sorted(int(row["id"]) for row in training)),
        })
        return {
            "architecture": "nested_chronological_safe_residual_trust_v1",
            "selected_factor": factor,
            "market_fallback_selected": factor == 0.0,
            "outer_validation_labels_used_for_selection": False,
            "calibration_labels_used_for_selection": False,
            "untouched_test_labels_used_for_selection": False,
        }

    def fit(training, config):
        return {"feature_count": len(training[0]["features"])}

    def predict(model, rows, config):
        strength = (
            candidate if int(model["feature_count"]) == 25 else control
        )
        probabilities = [
            strength if float(row["features"][0]) >= 0.5 else 1.0 - strength
            for row in rows
        ]
        return probabilities, [
            {"out_of_distribution": False} for _ in rows
        ]

    monkeypatch.setattr(walk, "select_residual_trust_factor", select)
    monkeypatch.setattr(walk, "fit_residual_model", fit)
    monkeypatch.setattr(walk, "predict_probabilities", predict)
    return trust_calls


def test_strong_candidate_passes_both_market_and_v14_gates(monkeypatch):
    design, protocol = _inputs()
    trust_calls = _patch_predictors(
        monkeypatch, candidate=0.75, control=0.65,
    )
    report = walk.evaluate_walk_forward(
        _examples("NON_BTC_TRANSFER"),
        cohort="NON_BTC_TRANSFER",
        design=design,
        protocol=protocol,
    )
    assert report["gate_met"] is True
    assert report["failure_result"] is None
    assert report["rows"] == 288
    assert report["close_windows"] == 48
    assert len(report["folds"]) == 3
    assert report["candidate_market_v14_identical_rows"] is True
    assert report["btc_and_non_btc_pooled"] is False
    assert report["same_close_assets_share_fold"] is True
    assert report["temporary_models_are_deployable"] is False
    assert report["untouched_test_rows_used"] == 0
    assert report[
        "walk_forward_validation_overlaps_calibration_partition"
    ] is True
    assert report[
        "calibration_overlap_is_not_independent_confirmation"
    ] is True
    assert report[
        "only_untouched_test_is_independent_final_confirmation"
    ] is True
    assert report["calibration_overlap_close_windows"] == 12
    assert report["calibration_overlap_rows"] == 72
    assert report["accuracy_is_report_only"] is True
    assert report["paper_artifact_created"] is False
    assert report["notification_eligible"] is False
    assert report["automatic_promotion"] is False
    assert report["real_trading_allowed"] is False
    assert all(report["gate_checks"].values())
    assert len(trust_calls) == 6
    assert [call["feature_count"] for call in trust_calls] == [
        25, 20, 25, 20, 25, 20,
    ]
    for fold, candidate_call, control_call in zip(
        report["folds"], trust_calls[::2], trust_calls[1::2],
    ):
        assert candidate_call["row_ids"] == control_call["row_ids"]
        assert candidate_call["max_close_time"] < (
            fold["validation_first_close_time"]
        )
        assert fold[
            "outer_validation_labels_used_for_trust_selection"
        ] is False


def test_candidate_that_beats_market_but_loses_to_v14_is_rejected(
    monkeypatch,
):
    design, protocol = _inputs()
    _patch_predictors(monkeypatch, candidate=0.65, control=0.75)
    report = walk.evaluate_walk_forward(
        _examples("NON_BTC_TRANSFER"),
        cohort="NON_BTC_TRANSFER",
        design=design,
        protocol=protocol,
    )
    assert report["gate_checks"]["candidate_brier_beats_market"] is True
    assert report["gate_checks"]["candidate_log_loss_beats_market"] is True
    assert report["gate_checks"]["candidate_brier_beats_v14"] is False
    assert report["gate_checks"]["candidate_log_loss_beats_v14"] is False
    assert report["gate_met"] is False
    assert report["failure_result"] == (
        "NO_V15_ARTIFACT_OR_PAPER_CHALLENGER"
    )


def test_market_fallback_factor_cannot_fake_an_improvement(monkeypatch):
    design, protocol = _inputs()
    _patch_predictors(
        monkeypatch, candidate=0.9, control=0.8, factor=0.0,
    )
    report = walk.evaluate_walk_forward(
        _examples("NON_BTC_TRANSFER"),
        cohort="NON_BTC_TRANSFER",
        design=design,
        protocol=protocol,
    )
    assert report["aggregate"]["candidate_scores"]["brier_score"] == (
        report["aggregate"]["market_scores"]["brier_score"]
    )
    assert report["gate_checks"]["candidate_brier_beats_market"] is False
    assert report["gate_met"] is False


def test_btc_uses_its_own_120_window_pretest_and_never_non_btc(
    monkeypatch,
):
    design, protocol = _inputs()
    _patch_predictors(monkeypatch, candidate=0.75, control=0.65)
    report = walk.evaluate_walk_forward(
        _examples("BTC"),
        cohort="BTC",
        design=design,
        protocol=protocol,
    )
    assert report["rows"] == 120
    assert report["close_windows"] == 120
    assert [
        fold["train_close_windows"] for fold in report["folds"]
    ] == [60, 80, 100]
    assert [
        fold["validation_close_windows"] for fold in report["folds"]
    ] == [20, 20, 20]
    assert report["gate_met"] is True


def test_calibration_passes_both_comparators_before_final_factor_reselection(
    monkeypatch,
):
    design, protocol = _inputs()
    trust_calls = _patch_predictors(
        monkeypatch, candidate=0.75, control=0.65,
    )
    rows = _examples("NON_BTC_TRANSFER")
    walk_report = walk.evaluate_walk_forward(
        rows,
        cohort="NON_BTC_TRANSFER",
        design=design,
        protocol=protocol,
    )
    report = walk.evaluate_calibration(
        rows,
        cohort="NON_BTC_TRANSFER",
        design=design,
        protocol=protocol,
        walk_forward_report=walk_report,
    )
    assert report["stage"] == "CALIBRATION"
    assert report["gate_met"] is True
    assert report["failure_result"] is None
    assert report["development_close_windows"] == 36
    assert report["calibration_close_windows"] == 12
    assert report["calibration_rows"] == 72
    assert report["untouched_test_rows_used"] == 0
    assert report[
        "calibration_rows_were_already_walk_forward_validation_rows"
    ] is True
    assert report["calibration_is_not_independent_confirmation"] is True
    assert report[
        "only_untouched_test_is_independent_final_confirmation"
    ] is True
    assert report["walk_forward_overlap_close_times_sha256"] == (
        walk_report["calibration_overlap_close_times_sha256"]
    )
    assert report["walk_forward_overlap_row_ids_sha256"] == (
        walk_report["calibration_overlap_row_ids_sha256"]
    )
    assert len(report["chronological_halves"]) == 2
    assert all(
        half["not_worse_vs_market"] and half["not_worse_vs_v14"]
        for half in report["chronological_halves"]
    )
    assert report["final_candidate_trust_selection"] is not None
    assert report["final_v14_trust_selection"] is not None
    assert report[
        "calibration_labels_used_for_final_factor_selection"
    ] is True
    assert report[
        "untouched_test_labels_used_for_final_factor_selection"
    ] is False
    assert report["final_model_fit_performed"] is False
    assert report["paper_artifact_created"] is False
    assert report["notification_eligible"] is False
    assert report["real_trading_allowed"] is False
    assert len(trust_calls) == 10


def test_calibration_half_failure_blocks_final_reselection(monkeypatch):
    design, protocol = _inputs()
    trust_calls = []

    def select(training, config, protocol, cohort):
        trust_calls.append(tuple(sorted(
            int(row["id"]) for row in training
        )))
        return {
            "architecture": "nested_chronological_safe_residual_trust_v1",
            "selected_factor": 1.0,
            "outer_validation_labels_used_for_selection": False,
            "calibration_labels_used_for_selection": False,
            "untouched_test_labels_used_for_selection": False,
        }

    monkeypatch.setattr(walk, "select_residual_trust_factor", select)
    monkeypatch.setattr(
        walk,
        "fit_residual_model",
        lambda training, config: {
            "feature_count": len(training[0]["features"])
        },
    )

    def predict(model, rows, config):
        probabilities = []
        for row in rows:
            label_signal = float(row["features"][0]) >= 0.5
            window = int((float(row["close_time"]) - 10_000.0) / 900.0)
            if int(model["feature_count"]) == 25:
                strength = 0.63 if window >= 42 else 0.9
            else:
                strength = 0.65
            probabilities.append(
                strength if label_signal else 1.0 - strength
            )
        return probabilities, [
            {"out_of_distribution": False} for _ in rows
        ]

    monkeypatch.setattr(walk, "predict_probabilities", predict)
    rows = _examples("NON_BTC_TRANSFER")
    walk_report = walk.evaluate_walk_forward(
        rows,
        cohort="NON_BTC_TRANSFER",
        design=design,
        protocol=protocol,
    )
    assert walk_report["gate_met"] is True
    report = walk.evaluate_calibration(
        rows,
        cohort="NON_BTC_TRANSFER",
        design=design,
        protocol=protocol,
        walk_forward_report=walk_report,
    )
    assert report["gate_checks"][
        "first_half_not_worse_vs_either"
    ] is True
    assert report["gate_checks"][
        "second_half_not_worse_vs_either"
    ] is False
    assert report["gate_met"] is False
    assert report["final_candidate_trust_selection"] is None
    assert report["final_v14_trust_selection"] is None
    assert report[
        "calibration_labels_used_for_final_factor_selection"
    ] is False
    assert report["failure_result"] == (
        "NO_V15_ARTIFACT_OR_PAPER_CHALLENGER"
    )
    assert len(trust_calls) == 8


def test_calibration_rejects_mismatched_walk_forward_rows(monkeypatch):
    design, protocol = _inputs()
    _patch_predictors(monkeypatch, candidate=0.75, control=0.65)
    rows = _examples("NON_BTC_TRANSFER")
    walk_report = walk.evaluate_walk_forward(
        rows,
        cohort="NON_BTC_TRANSFER",
        design=design,
        protocol=protocol,
    )
    tampered = copy.deepcopy(walk_report)
    tampered["input_rows_sha256"] = "f" * 64
    with pytest.raises(
        ValueError,
        match="v15_calibration_walk_forward_gate_missing_or_mismatch",
    ):
        walk.evaluate_calibration(
            rows,
            cohort="NON_BTC_TRANSFER",
            design=design,
            protocol=protocol,
            walk_forward_report=tampered,
        )


def test_calibration_rejects_hidden_or_tampered_overlap_disclosure(
    monkeypatch,
):
    design, protocol = _inputs()
    _patch_predictors(monkeypatch, candidate=0.75, control=0.65)
    rows = _examples("NON_BTC_TRANSFER")
    walk_report = walk.evaluate_walk_forward(
        rows,
        cohort="NON_BTC_TRANSFER",
        design=design,
        protocol=protocol,
    )
    for field, value in (
        (
            "calibration_overlap_is_not_independent_confirmation",
            False,
        ),
        ("calibration_overlap_row_ids_sha256", "f" * 64),
    ):
        tampered = copy.deepcopy(walk_report)
        tampered[field] = value
        with pytest.raises(
            ValueError,
            match=(
                "v15_calibration_walk_forward_"
                "overlap_disclosure_mismatch"
            ),
        ):
            walk.evaluate_calibration(
                rows,
                cohort="NON_BTC_TRANSFER",
                design=design,
                protocol=protocol,
                walk_forward_report=tampered,
            )


def test_missing_same_close_asset_fails_before_any_fit(monkeypatch):
    design, protocol = _inputs()
    fit_calls = []
    monkeypatch.setattr(
        walk, "fit_residual_model",
        lambda *args, **kwargs: fit_calls.append(True),
    )
    rows = _examples("NON_BTC_TRANSFER")
    rows.pop()
    with pytest.raises(
        ValueError, match="v15_walk_forward_same_close_asset_leakage",
    ):
        walk.evaluate_walk_forward(
            rows,
            cohort="NON_BTC_TRANSFER",
            design=design,
            protocol=protocol,
        )
    assert fit_calls == []


def test_v14_base_feature_mismatch_fails_before_fit(monkeypatch):
    design, protocol = _inputs()
    rows = _examples("NON_BTC_TRANSFER")
    rows[0]["v15_features"][0] = 0.25
    with pytest.raises(ValueError, match="v15_walk_forward_example_invalid"):
        walk.evaluate_walk_forward(
            rows,
            cohort="NON_BTC_TRANSFER",
            design=design,
            protocol=protocol,
        )


def test_rehashed_protocol_tampering_is_not_accepted(monkeypatch):
    design, protocol = _inputs()
    tampered = copy.deepcopy(protocol)
    tampered["walk_forward_gate"][
        "aggregate_candidate_minus_v14_brier_mean_must_be_at_most"
    ] = 0.0
    assert design_fingerprint(tampered) != walk.EVALUATION_PROTOCOL_SHA256
    with pytest.raises(
        ValueError, match="v15_walk_forward_contract_identity_mismatch",
    ):
        walk.evaluate_walk_forward(
            _examples("NON_BTC_TRANSFER"),
            cohort="NON_BTC_TRANSFER",
            design=design,
            protocol=tampered,
        )


def test_walk_forward_module_has_no_database_or_delivery_capability():
    parameters = inspect.signature(walk.evaluate_walk_forward).parameters
    assert "database_path" not in parameters
    assert "read_labels" not in parameters
    source = Path(walk.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "official_result",
        "import sqlite3",
        "from sqlite3",
        "load_feature_rows",
        "V3Telegram",
        "place_order(",
        "atomic_write",
    ):
        assert forbidden not in source
def test_architecture_rows_bind_matching_feature_names():
    row = {
        "v15_features": [0.0] * len(v15.FEATURE_NAMES),
        "v15_feature_names": list(v15.FEATURE_NAMES),
        "v14_features": [0.0] * len(v14.FEATURE_NAMES),
        "v14_feature_names": list(v14.FEATURE_NAMES),
    }
    candidate = walk._with_features([row], "v15_features")[0]
    control = walk._with_features([row], "v14_features")[0]
    assert len(candidate["features"]) == len(candidate["feature_names"]) == 25
    assert len(control["features"]) == len(control["feature_names"]) == 20
    with pytest.raises(ValueError, match="feature_key_invalid"):
        walk._with_features([row], "unknown")

