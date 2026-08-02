from __future__ import annotations

import json
import math
from pathlib import Path
import sqlite3

import pytest

from tools import q15_rti_microstructure_freeze as freeze


def _design():
    return json.loads(freeze.DEFAULT_DESIGN.read_text(encoding="utf-8"))


def _v11_design():
    return json.loads(
        (Path("config") / "q15_rti_microstructure_design_v11.json").read_text(
            encoding="utf-8"
        )
    )


def _v12_design():
    return json.loads(
        (Path("config") / "q15_rti_microstructure_design_v12.json").read_text(
            encoding="utf-8"
        )
    )


def _v13_design():
    return json.loads(
        (Path("config") / "q15_rti_microstructure_design_v13.json").read_text(
            encoding="utf-8"
        )
    )


def test_freeze_tool_resolves_the_frozen_v5_feature_runtime():
    design = json.loads(
        (Path("config") / "q15_rti_microstructure_design_v5.json").read_text(
            encoding="utf-8"
        )
    )
    runtime = freeze._feature_runtime(design)
    assert runtime.DESIGN_ID == design["design_id"]
    assert tuple(runtime.FEATURE_NAMES) == tuple(design["feature_names"])


def test_freeze_tool_resolves_the_frozen_v6_feature_runtime():
    design = json.loads(
        (Path("config") / "q15_rti_microstructure_design_v6.json").read_text(
            encoding="utf-8"
        )
    )
    runtime = freeze._feature_runtime(design)
    assert runtime.DESIGN_ID == design["design_id"]
    assert tuple(runtime.FEATURE_NAMES) == tuple(design["feature_names"])


def test_freeze_tool_resolves_the_frozen_v7_feature_runtime():
    design = json.loads(
        (Path("config") / "q15_rti_microstructure_design_v7.json").read_text(
            encoding="utf-8"
        )
    )
    runtime = freeze._feature_runtime(design)
    assert runtime.DESIGN_ID == design["design_id"]
    assert tuple(runtime.FEATURE_NAMES) == tuple(design["feature_names"])


def test_freeze_tool_resolves_the_frozen_v8_feature_runtime():
    design = json.loads(
        (Path("config") / "q15_rti_microstructure_design_v8.json").read_text(
            encoding="utf-8"
        )
    )
    runtime = freeze._feature_runtime(design)
    assert runtime.DESIGN_ID == design["design_id"]
    assert tuple(runtime.FEATURE_NAMES) == tuple(design["feature_names"])


def test_freeze_tool_resolves_the_frozen_v9_feature_runtime():
    design = json.loads(
        (Path("config") / "q15_rti_microstructure_design_v9.json").read_text(
            encoding="utf-8"
        )
    )
    runtime = freeze._feature_runtime(design)
    assert runtime.DESIGN_ID == design["design_id"]
    assert tuple(runtime.FEATURE_NAMES) == tuple(design["feature_names"])


def test_freeze_tool_resolves_the_frozen_v10_feature_runtime():
    design = json.loads(
        (Path("config") / "q15_rti_microstructure_design_v10.json").read_text(
            encoding="utf-8"
        )
    )
    runtime = freeze._feature_runtime(design)
    assert runtime.DESIGN_ID == design["design_id"]
    assert tuple(runtime.FEATURE_NAMES) == tuple(design["feature_names"])


def test_freeze_tool_resolves_the_frozen_v11_feature_runtime():
    design = json.loads(
        (Path("config") / "q15_rti_microstructure_design_v11.json").read_text(
            encoding="utf-8"
        )
    )
    runtime = freeze._feature_runtime(design)
    assert runtime.DESIGN_ID == design["design_id"]
    assert tuple(runtime.FEATURE_NAMES) == tuple(design["feature_names"])


def test_freeze_tool_resolves_the_frozen_v12_feature_runtime():
    design = _v12_design()
    runtime = freeze._feature_runtime(design)
    assert runtime.DESIGN_ID == design["design_id"]
    assert tuple(runtime.FEATURE_NAMES) == tuple(design["feature_names"])


def test_freeze_tool_resolves_the_frozen_v13_feature_runtime():
    design = _v13_design()
    runtime = freeze._feature_runtime(design)
    assert runtime.DESIGN_ID == design["design_id"]
    assert tuple(runtime.FEATURE_NAMES) == tuple(design["feature_names"])


def test_prepare_uses_design_scoped_feature_readiness(monkeypatch):
    design = _v11_design()
    seen = {}

    class Runtime:
        FEATURE_NAMES = tuple(design["feature_names"])

        @staticmethod
        def model_feature_window_coverage(rows):
            assert rows == []
            return {
                "complete_model_feature_close_windows": 60,
                "model_feature_timestamp_failures": [],
                "model_feature_unavailable_rows": [],
                "unusable_model_feature_close_windows": [],
            }

    monkeypatch.setattr(freeze, "_feature_runtime", lambda raw: Runtime())
    monkeypatch.setattr(
        freeze,
        "build_report",
        lambda rows, source_schema: {
            "complete_microstructure_close_windows": 1,
            "timestamp_alignment_failures": ["pre-design-source-gap"],
        },
    )

    def capture_readiness(raw_design, coverage):
        seen.update(coverage)
        raise RuntimeError("captured_design_scoped_readiness")

    monkeypatch.setattr(freeze, "build_readiness", capture_readiness)
    with pytest.raises(RuntimeError, match="captured_design_scoped_readiness"):
        freeze.prepare_unlabeled_examples([], design, "NON_BTC_TRANSFER")

    assert seen["complete_model_feature_close_windows"] == 60
    assert seen["model_feature_timestamp_failures"] == []


def test_prepare_uses_preserved_quote_spread_when_compact_schema_omits_it(
    monkeypatch,
):
    assets = tuple(sorted(freeze.COHORT_ASSETS["NON_BTC_TRANSFER"]))
    rows = [
        {"id": index + 1, "asset": asset, "side": "YES", "close_time": 100.0}
        for index, asset in enumerate(assets)
    ]

    class Runtime:
        FEATURE_NAMES = ("compact_signal",)

        @staticmethod
        def model_feature_window_coverage(raw_rows):
            return {"complete_model_feature_close_windows": 60}

        @staticmethod
        def feature_vector(row):
            return {
                "available": True,
                "features": [1.0],
                "market_yes_probability": 0.55,
                "yes_ask_cents": 44.0,
                "no_ask_cents": 57.0,
                "yes_depth_contracts": 20.0,
                "no_depth_contracts": 20.0,
                "yes_depth_available": True,
                "no_depth_available": True,
                "spread_cents": 1.25,
            }

    design = {
        "source_schema": "test",
        "cohorts": {"NON_BTC_TRANSFER": {"minimum_complete_close_windows": 1}},
    }
    monkeypatch.setattr(freeze, "_feature_runtime", lambda raw: Runtime())
    monkeypatch.setattr(freeze, "build_report", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        freeze,
        "build_readiness",
        lambda *args, **kwargs: {
            "cohorts": {"NON_BTC_TRANSFER": {"ready_for_locked_freeze": True}}
        },
    )
    monkeypatch.setattr(
        freeze, "_complete_window_rows", lambda *args, **kwargs: {100.0: rows}
    )
    examples, windows = freeze.prepare_unlabeled_examples(
        rows, design, "NON_BTC_TRANSFER",
    )
    assert windows == (100.0,)
    assert len(examples) == 6
    assert {row["spread_cents"] for row in examples} == {1.25}
    assert {row["side"] for row in examples} == {"YES"}


def _coverage(windows: int):
    return {
        "complete_microstructure_v1_close_windows": windows,
        "timestamp_alignment_failures": [],
        "cross_asset_partial_schema_windows": [],
        "incomplete_microstructure_v1_close_windows": [],
    }


def _examples(windows: int = 60):
    rows = []
    for window in range(windows):
        close = 2_000.0 + 900.0 * window
        for asset_index, asset in enumerate(sorted(freeze.COHORT_ASSETS["NON_BTC_TRANSFER"])):
            row_id = window * 10 + asset_index + 1
            rows.append({
                "id": row_id,
                "close_time": close,
                "asset": asset,
                "cohort": "NON_BTC_TRANSFER",
                "features": [float((row_id + index) % 5) / 5.0 for index in range(len(freeze.FEATURE_NAMES))],
                "market_yes_probability": 0.45 if row_id % 2 else 0.55,
                "yes_ask_cents": 44.0,
                "no_ask_cents": 57.0,
                "yes_depth_contracts": 20.0,
                "no_depth_contracts": 20.0,
                "yes_depth_available": True,
                "no_depth_available": True,
                "spread_cents": 1.0,
            })
    return rows


def test_preregistered_folds_are_36_12_12_and_never_split_a_close():
    close_times = [2_000.0 + window * 900.0 for window in range(60) for _ in range(7)]
    folds = freeze.chronological_folds(close_times, _design())
    assert {name: len(values) for name, values in folds.items()} == {
        "train": 36,
        "calibration": 12,
        "test": 12,
    }
    assert set(folds["train"]).isdisjoint(folds["calibration"])
    assert set(folds["train"]).isdisjoint(folds["test"])
    assert set(folds["calibration"]).isdisjoint(folds["test"])


@pytest.mark.parametrize(
    ("cohort", "windows", "initial", "block"),
    (("BTC", 120, 60, 20), ("NON_BTC_TRANSFER", 48, 24, 8)),
)
def test_v11_walk_forward_protocol_is_design_bound_and_expanding(
    cohort, windows, initial, block,
):
    design = _v11_design()
    protocol = freeze.walk_forward_protocol_for_design(design)
    assert protocol is not None
    freeze.validate_walk_forward_protocol(protocol, design)
    assert freeze.evaluation_protocol_fingerprint(protocol) == (
        freeze.EXPECTED_V11_WALK_FORWARD_PROTOCOL_SHA256
    )
    assert protocol["protocol_id"].endswith("evaluation-v2")
    assert protocol["paired_close_window_bootstrap"] == {
        "version": "q15-rti-paired-close-window-bootstrap-v1",
        "cluster_key": "close_time",
        "resamples": 5000,
        "confidence_level": 0.9,
        "random_seed": 2026072201,
        "minimum_mean_brier_improvement": 0.001,
        "minimum_mean_log_loss_improvement": 0.001,
        "model_minus_market_loss_delta": True,
        "same_close_assets_resampled_together": True,
        "one_sided_upper_bound_reported": True,
        "two_sided_interval_reported": True,
    }
    close_times = tuple(2_000.0 + 900.0 * index for index in range(windows))
    folds = freeze.expanding_walk_forward_folds(
        close_times, protocol, cohort,
    )
    assert len(folds) == 3
    assert [len(fold["train"]) for fold in folds] == [
        initial, initial + block, initial + 2 * block,
    ]
    assert [len(fold["validation"]) for fold in folds] == [block] * 3
    validation = [value for fold in folds for value in fold["validation"]]
    assert tuple(validation) == close_times[initial:]
    assert len(validation) == len(set(validation))
    assert all(max(fold["train"]) < min(fold["validation"]) for fold in folds)


def test_v11_walk_forward_protocol_rejects_test_access_or_design_drift():
    design = _v11_design()
    protocol = dict(freeze.walk_forward_protocol_for_design(design) or {})
    poisoned = json.loads(json.dumps(protocol))
    poisoned["gate"]["untouched_test_may_be_read_when_not_met"] = True
    with pytest.raises(ValueError, match="walk_forward_test_read_guard_missing"):
        freeze.validate_walk_forward_protocol(poisoned, design)
    poisoned = json.loads(json.dumps(protocol))
    poisoned["cohorts"]["BTC"]["validation_block_windows"] = 19
    with pytest.raises(ValueError, match="walk_forward_pretest_coverage_mismatch"):
        freeze.validate_walk_forward_protocol(poisoned, design)
    poisoned = json.loads(json.dumps(protocol))
    poisoned["paired_close_window_bootstrap"]["random_seed"] += 1
    with pytest.raises(ValueError, match="walk_forward_bootstrap_seed_mismatch"):
        freeze.validate_walk_forward_protocol(poisoned, design)


def test_v11_reporting_protocol_was_frozen_before_outcome_review():
    design = _v11_design()
    protocol = freeze.reporting_protocol_for_design(design)
    assert protocol is not None
    freeze.validate_reporting_protocol(protocol, design)
    assert protocol["protocol_status"] == (
        "PREREGISTERED_BEFORE_ANY_V11_OUTCOME_REVIEW"
    )
    assert protocol["outcome_labels_used_for_protocol"] is False
    assert protocol["performance_metrics_inspected_before_preregistration"] is False
    assert protocol[
        "changes_features_model_hyperparameters_entry_policy_or_gates"
    ] is False
    assert freeze.reporting_protocol_fingerprint(protocol) == (
        freeze.EXPECTED_V11_REPORTING_PROTOCOL_SHA256
    )
    assert tuple(protocol["dimensions"]) == (
        "asset",
        "rti_side",
        "absolute_distance_tier",
        "realized_volatility_tier",
        "market_regime",
    )
    poisoned = json.loads(json.dumps(protocol))
    poisoned["changes_features_model_hyperparameters_entry_policy_or_gates"] = True
    with pytest.raises(
        ValueError, match="reporting_protocol_required_false_flag_missing",
    ):
        freeze.validate_reporting_protocol(poisoned, design)


def test_v11_calibration_reporting_protocol_was_frozen_before_outcome_review():
    design = _v11_design()
    protocol = freeze.calibration_reporting_protocol_for_design(design)
    assert protocol is not None
    freeze.validate_calibration_reporting_protocol(protocol, design)
    assert protocol["protocol_status"] == (
        "PREREGISTERED_BEFORE_ANY_V11_OUTCOME_REVIEW"
    )
    assert protocol["outcome_labels_used_for_protocol"] is False
    assert protocol["performance_metrics_inspected_before_preregistration"] is False
    assert protocol[
        "changes_features_model_hyperparameters_entry_policy_or_gates"
    ] is False
    assert freeze.calibration_reporting_protocol_fingerprint(protocol) == (
        freeze.EXPECTED_V11_CALIBRATION_REPORTING_PROTOCOL_SHA256
    )
    assert [row["label"] for row in protocol["probability_bins"]] == [
        "0.00_to_lt_0.20",
        "0.20_to_lt_0.35",
        "0.35_to_lt_0.50",
        "0.50_to_lt_0.65",
        "0.65_to_lt_0.80",
        "0.80_to_1.00",
    ]
    poisoned = json.loads(json.dumps(protocol))
    poisoned["probability_bins"][2]["maximum_exclusive"] = 0.55
    assert freeze.calibration_reporting_protocol_fingerprint(poisoned) != (
        freeze.EXPECTED_V11_CALIBRATION_REPORTING_PROTOCOL_SHA256
    )
    with pytest.raises(
        ValueError, match="calibration_reporting_bins_invalid",
    ):
        freeze.validate_calibration_reporting_protocol(poisoned, design)


def test_v11_selective_value_curve_protocol_was_frozen_before_outcome_review():
    design = _v11_design()
    protocol = freeze.selective_value_curve_protocol_for_design(design)
    assert protocol is not None
    freeze.validate_selective_value_curve_protocol(protocol, design)
    assert protocol["outcome_labels_used_for_protocol"] is False
    assert protocol["performance_metrics_inspected_before_preregistration"] is False
    assert protocol["fixed_expected_value_thresholds_cents"] == [
        0.0, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0,
    ]
    assert protocol["frozen_entry_policy_threshold_cents"] == 3.0
    assert freeze.selective_value_curve_protocol_fingerprint(protocol) == (
        freeze.EXPECTED_V11_SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256
    )
    poisoned = json.loads(json.dumps(protocol))
    poisoned["fixed_expected_value_thresholds_cents"][1] = 0.5
    assert freeze.selective_value_curve_protocol_fingerprint(poisoned) != (
        freeze.EXPECTED_V11_SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256
    )
    with pytest.raises(ValueError, match="selective_value_curve_thresholds_invalid"):
        freeze.validate_selective_value_curve_protocol(poisoned, design)


def test_v13_reporting_protocol_chain_is_frozen_before_any_outcome_review():
    design = _v13_design()
    reporting = freeze.reporting_protocol_for_design(design)
    calibration = freeze.calibration_reporting_protocol_for_design(design)
    curve = freeze.selective_value_curve_protocol_for_design(design)
    assert reporting is not None
    assert calibration is not None
    assert curve is not None
    freeze.validate_reporting_protocol(reporting, design)
    freeze.validate_calibration_reporting_protocol(calibration, design)
    freeze.validate_selective_value_curve_protocol(curve, design)
    assert reporting["protocol_status"] == (
        "PREREGISTERED_BEFORE_ANY_V13_OUTCOME_REVIEW"
    )
    assert freeze.reporting_protocol_fingerprint(reporting) == (
        freeze.EXPECTED_V13_REPORTING_PROTOCOL_SHA256
    )
    assert freeze.calibration_reporting_protocol_fingerprint(calibration) == (
        freeze.EXPECTED_V13_CALIBRATION_REPORTING_PROTOCOL_SHA256
    )
    assert freeze.selective_value_curve_protocol_fingerprint(curve) == (
        freeze.EXPECTED_V13_SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256
    )
    assert calibration["reported_for"] == [
        "v13_model", "kalshi_market_prior",
    ]
    assert curve["all_other_entry_rules_remain_fixed"] == {
        "maximum_ask_cents": 62.0,
        "maximum_spread_cents": 1.5,
        "minimum_displayed_depth_contracts": 10.0,
        "simulation_contracts": 10,
        "official_kalshi_fees": True,
        "slippage_cents_per_contract": 2.0,
    }
    sensitivity = reporting["known_loss_sensitivity"]
    assert sensitivity["known_losses_may_not_be_removed_from_any_v13_metric"]
    assert sensitivity[
        "no_post_boundary_row_may_be_excluded_by_similarity_to_a_known_loss"
    ]
    for protocol in (reporting, calibration, curve):
        assert protocol["outcome_labels_used_for_protocol"] is False
        assert protocol["performance_metrics_inspected_before_preregistration"] is False
        assert protocol["notification_eligible"] is False
        assert protocol["real_trading_allowed"] is False

    poisoned = json.loads(json.dumps(reporting))
    poisoned["known_loss_sensitivity"][
        "known_losses_may_not_be_removed_from_any_v13_metric"
    ] = False
    with pytest.raises(
        ValueError, match="reporting_protocol_known_loss_guard_missing",
    ):
        freeze.validate_reporting_protocol(poisoned, design)


def _v13_reporting_examples():
    names = list(_v13_design()["feature_names"])
    indexes = {name: names.index(name) for name in names}
    specifications = (
        # probability, label, stored side, distance, rv, median, breadth
        (0.8, 1, "YES", 0.5, 0.5, 2.0, 0.7),
        (0.2, 0, "NO", 2.0, 1.5, -2.0, 0.1),
        (0.5, 1, "YES", 4.0, 3.0, -2.0, -0.7),
        (0.5, 0, "YES", 12.0, 9.0, 0.0, 0.2),
    )
    rows = []
    probabilities = []
    for index, spec in enumerate(specifications):
        probability, label, side, distance, rv, median, breadth = spec
        values = [0.0] * len(names)
        values[indexes["yes_signed_distance_bps"]] = distance
        values[indexes["log1p_realized_volatility_bps"]] = math.log1p(rv)
        values[indexes["cross_asset_median_momentum_60s"]] = median
        values[indexes["cross_asset_breadth_signed_60s"]] = breadth
        rows.append({
            "id": index + 101,
            "close_time": 20_000.0 + 900.0 * index,
            "asset": ("BNB", "DOGE", "ETH", "HYPE")[index],
            "side": side,
            "cohort": "NON_BTC_TRANSFER",
            "feature_names": names,
            "features": values,
            "label_yes": label,
            "market_yes_probability": 0.5,
            "yes_ask_cents": 44.0 if index < 2 else 60.0,
            "no_ask_cents": 57.0 if index < 2 else 60.0,
            "yes_depth_contracts": 20.0,
            "no_depth_contracts": 20.0,
            "yes_depth_available": True,
            "no_depth_available": True,
            "spread_cents": 1.0,
        })
        probabilities.append(probability)
    return rows, probabilities


def test_v13_reporting_uses_stored_side_and_emits_full_fixed_economics():
    design = _v13_design()
    examples, probabilities = _v13_reporting_examples()
    reporting = freeze.reporting_protocol_for_design(design)
    calibration = freeze.calibration_reporting_protocol_for_design(design)
    curve = freeze.selective_value_curve_protocol_for_design(design)
    metrics = freeze.test_metrics(
        examples,
        probabilities,
        [{"out_of_distribution": False} for _ in examples],
        design["entry_policy"],
        reporting_protocol=reporting,
        calibration_reporting_protocol=calibration,
        selective_value_curve_protocol=curve,
    )
    subgroup = metrics["fixed_subgroup_reporting"]
    sides = subgroup["dimensions"]["rti_side"]["observed_slices"]
    assert sides["YES"]["rows"] == 3
    assert sides["NO"]["rows"] == 1
    assert subgroup["protocol_sha256"] == (
        freeze.EXPECTED_V13_REPORTING_PROTOCOL_SHA256
    )
    calibration_report = metrics["fixed_calibration_reporting"]
    assert "v13_model" in calibration_report["sources"]
    assert "v11_model" not in calibration_report["sources"]
    assert calibration_report["historical_calibration_cannot_promote"] is True
    value_curve = metrics["fixed_selective_value_curve"]
    assert value_curve["frozen_entry_policy_threshold"] == "ev_ge_3c"
    assert value_curve["historical_curve_cannot_promote"] is True
    assert value_curve["threshold_selection_from_test_forbidden"] is True
    assert metrics["fee_schedule_version"] == (
        freeze.KALSHI_Q15_FEE_SCHEDULE_VERSION
    )
    assert metrics["execution_cost_model_version"] == (
        freeze.RTI_EXECUTION_COST_MODEL_VERSION
    )
    freeze.validate_fixed_subgroup_metrics(metrics, examples, reporting)
    freeze.validate_fixed_calibration_metrics(
        metrics, examples, probabilities, calibration,
    )
    freeze.validate_fixed_selective_value_curve(
        metrics, examples, probabilities, design["entry_policy"], curve,
    )

    missing_side = [dict(row) for row in examples]
    missing_side[0].pop("side")
    with pytest.raises(ValueError, match="reporting_stored_side_invalid"):
        freeze.fixed_subgroup_report(
            missing_side,
            probabilities,
            [{"out_of_distribution": False} for _ in examples],
            design["entry_policy"],
            reporting,
        )


def _v11_reporting_examples():
    names = list(_v11_design()["feature_names"])
    indexes = {name: names.index(name) for name in names}
    specifications = (
        # probability, label, side sign, distance, rv, median, breadth, depth
        (0.8, 1, 1.0, 0.5, 0.5, 2.0, 0.7, 20.0),
        (0.2, 0, -1.0, 2.0, 1.5, -2.0, 0.1, 20.0),
        (0.5, 1, 1.0, 4.0, 3.0, -2.0, -0.7, 20.0),
        (0.5, 0, 1.0, 12.0, 9.0, 0.0, 0.2, 5.0),
    )
    rows = []
    assets = ("BNB", "DOGE", "ETH", "HYPE")
    probabilities = []
    for index, spec in enumerate(specifications):
        probability, label, side, distance, rv, median, breadth, depth = spec
        values = [0.0] * len(names)
        values[indexes["final_side_yes"]] = side
        values[indexes["yes_signed_distance_bps"]] = distance
        values[indexes["log1p_realized_volatility_bps"]] = math.log1p(rv)
        values[indexes["cross_asset_median_momentum_60s"]] = median
        values[indexes["cross_asset_breadth_signed_60s"]] = breadth
        values[indexes["spread_cents"]] = 1.0
        rows.append({
            "id": index + 1,
            "close_time": 10_000.0 + 900.0 * index,
            "asset": assets[index],
            "cohort": "NON_BTC_TRANSFER",
            "feature_names": names,
            "features": values,
            "label_yes": label,
            "market_yes_probability": 0.5,
            "yes_ask_cents": 44.0 if index < 2 else 60.0,
            "no_ask_cents": 57.0 if index < 2 else 60.0,
            "yes_depth_contracts": depth,
            "no_depth_contracts": depth,
            "yes_depth_available": True,
            "no_depth_available": True,
            "spread_cents": 1.0,
        })
        probabilities.append(probability)
    return rows, probabilities


def test_fixed_subgroup_report_partitions_rows_and_never_fakes_rejected_fills():
    examples, probabilities = _v11_reporting_examples()
    protocol = freeze.reporting_protocol_for_design(_v11_design())
    metrics = freeze.test_metrics(
        examples,
        probabilities,
        [{"out_of_distribution": False} for _ in examples],
        _v11_design()["entry_policy"],
        reporting_protocol=protocol,
    )
    rejected = metrics["rejected_counterfactual"]
    assert metrics["picks"] == 2
    assert rejected["rejected_rows"] == 2
    assert rejected["predicted_side_accuracy"] == 0.5
    assert rejected["quote_executable_rows"] == 1
    assert rejected["non_executable_rows"] == 1
    assert rejected["paper_counterfactual_only"] is True
    assert rejected["never_claimed_as_fill"] is True
    assert rejected["rejection_reason_counts"] == {
        "DISPLAYED_DEPTH_BELOW_MINIMUM": 1,
        "EXPECTED_VALUE_BELOW_MINIMUM": 2,
    }
    reporting = metrics["fixed_subgroup_reporting"]
    assert reporting["protocol_sha256"] == (
        freeze.EXPECTED_V11_REPORTING_PROTOCOL_SHA256
    )
    assert reporting["changes_deployment_gate"] is False
    assert reporting["cohort"] == "NON_BTC_TRANSFER"
    for dimension in reporting["dimensions"].values():
        assert dimension["partition_rows"] == len(examples)
        assert sum(
            row["rows"] for row in dimension["observed_slices"].values()
        ) == len(examples)
    regimes = reporting["dimensions"]["market_regime"]["observed_slices"]
    assert set(regimes) == {
        "BROAD_ALIGNED",
        "THIN_OR_ISOLATED_ALIGNED",
        "BROAD_OPPOSED",
        "MIXED_OR_FLAT",
    }


def test_fixed_calibration_report_compares_v11_with_kalshi_and_reports_empty_bins():
    examples, probabilities = _v11_reporting_examples()
    protocol = freeze.calibration_reporting_protocol_for_design(_v11_design())
    metrics = freeze.test_metrics(
        examples,
        probabilities,
        [{"out_of_distribution": False} for _ in examples],
        _v11_design()["entry_policy"],
        calibration_reporting_protocol=protocol,
    )
    calibration = metrics["fixed_calibration_reporting"]
    model = calibration["sources"]["v11_model"]
    market = calibration["sources"]["kalshi_market_prior"]
    assert model["expected_calibration_error"] == pytest.approx(0.1)
    assert model["maximum_calibration_error"] == pytest.approx(0.2)
    assert model["binned_reliability"] == pytest.approx(0.02)
    assert model["binned_resolution"] == pytest.approx(0.125)
    assert model["calibration_bias_probability_minus_observed"] == 0.0
    assert market["expected_calibration_error"] == 0.0
    assert market["maximum_calibration_error"] == 0.0
    assert calibration["comparisons"][
        "model_minus_market_expected_calibration_error"
    ] == pytest.approx(0.1)
    assert len(model["bins"]) == 6
    assert len(model["empty_bins"]) == 3
    assert calibration["historical_calibration_cannot_promote"] is True
    assert calibration["promotional_claim_allowed"] is False
    freeze.validate_fixed_calibration_metrics(
        metrics, examples, probabilities, protocol,
    )

    tampered = json.loads(json.dumps(metrics))
    tampered["fixed_calibration_reporting"]["sources"]["v11_model"][
        "expected_calibration_error"
    ] = 0.0
    with pytest.raises(ValueError, match="fixed_calibration_reporting_mismatch"):
        freeze.validate_fixed_calibration_metrics(
            tampered, examples, probabilities, protocol,
        )

    mixed = [dict(row) for row in examples]
    mixed[-1]["cohort"] = "BTC"
    with pytest.raises(
        ValueError, match="calibration_reporting_cohort_mixing_forbidden",
    ):
        freeze.fixed_calibration_report(mixed, probabilities, protocol)


def test_fixed_selective_value_curve_is_monotonic_and_never_selects_threshold():
    examples, _ = _v11_reporting_examples()
    examples = [dict(row) for row in examples]
    probabilities = [0.8, 0.6, 0.7, 0.8]
    asks = [44.0, 55.0, 60.0, 60.0]
    for row, ask in zip(examples, asks):
        row["yes_ask_cents"] = ask
        row["no_ask_cents"] = ask
        row["yes_depth_contracts"] = 20.0
        row["no_depth_contracts"] = 20.0
    protocol = freeze.selective_value_curve_protocol_for_design(_v11_design())
    metrics = freeze.test_metrics(
        examples,
        probabilities,
        [{"out_of_distribution": False} for _ in examples],
        _v11_design()["entry_policy"],
        selective_value_curve_protocol=protocol,
    )
    curve = metrics["fixed_selective_value_curve"]
    counts = [
        curve["thresholds"][key]["picks"]
        for key in curve["threshold_order"]
    ]
    assert counts == sorted(counts, reverse=True)
    assert counts[0] > counts[-1]
    assert curve["frozen_entry_policy_threshold"] == "ev_ge_3c"
    assert curve["thresholds"]["ev_ge_3c"]["picks"] == metrics["picks"]
    assert curve["higher_threshold_pick_set_subset_verified"] is True
    assert curve["never_claimed_as_historical_fills"] is True
    assert curve["historical_curve_cannot_promote"] is True
    assert curve["threshold_selection_from_test_forbidden"] is True
    freeze.validate_fixed_selective_value_curve(
        metrics,
        examples,
        probabilities,
        _v11_design()["entry_policy"],
        protocol,
    )

    tampered = json.loads(json.dumps(metrics))
    tampered["fixed_selective_value_curve"]["thresholds"]["ev_ge_8c"][
        "picks"
    ] += 1
    with pytest.raises(ValueError, match="fixed_selective_value_curve_mismatch"):
        freeze.validate_fixed_selective_value_curve(
            tampered,
            examples,
            probabilities,
            _v11_design()["entry_policy"],
            protocol,
        )

    mixed = [dict(row) for row in examples]
    mixed[-1]["cohort"] = "BTC"
    with pytest.raises(
        ValueError, match="selective_value_curve_cohort_mixing_forbidden",
    ):
        freeze.fixed_selective_value_curve(
            mixed,
            probabilities,
            _v11_design()["entry_policy"],
            protocol,
        )


def test_fixed_subgroup_report_rejects_cohort_mixing_and_protocol_tampering():
    examples, probabilities = _v11_reporting_examples()
    protocol = freeze.reporting_protocol_for_design(_v11_design())
    mixed = [dict(row) for row in examples]
    mixed[-1]["cohort"] = "BTC"
    with pytest.raises(ValueError, match="reporting_cohort_mixing_forbidden"):
        freeze.fixed_subgroup_report(
            mixed,
            probabilities,
            [{"out_of_distribution": False} for _ in mixed],
            _v11_design()["entry_policy"],
            protocol,
        )
    tampered = json.loads(json.dumps(protocol))
    tampered["dimensions"]["absolute_distance_tier"]["bins"][0][
        "maximum_exclusive"
    ] = 2.0
    with pytest.raises(ValueError, match="reporting_protocol_fingerprint_mismatch"):
        freeze.fixed_subgroup_report(
            examples,
            probabilities,
            [{"out_of_distribution": False} for _ in examples],
            _v11_design()["entry_policy"],
            tampered,
        )


def test_fixed_subgroup_metrics_validator_rejects_missing_or_tampered_reports():
    examples, probabilities = _v11_reporting_examples()
    protocol = freeze.reporting_protocol_for_design(_v11_design())
    metrics = freeze.test_metrics(
        examples,
        probabilities,
        [{"out_of_distribution": False} for _ in examples],
        _v11_design()["entry_policy"],
        reporting_protocol=protocol,
    )
    freeze.validate_fixed_subgroup_metrics(metrics, examples, protocol)

    missing = dict(metrics)
    missing.pop("fixed_subgroup_reporting")
    with pytest.raises(ValueError, match="fixed_subgroup_reporting_missing"):
        freeze.validate_fixed_subgroup_metrics(missing, examples, protocol)

    tampered = json.loads(json.dumps(metrics))
    tampered["fixed_subgroup_reporting"]["dimensions"]["asset"][
        "partition_rows"
    ] -= 1
    with pytest.raises(
        ValueError, match="fixed_subgroup_reporting_row_partition_invalid",
    ):
        freeze.validate_fixed_subgroup_metrics(tampered, examples, protocol)

    unsafe = json.loads(json.dumps(metrics))
    unsafe["fixed_subgroup_reporting"]["dimensions"]["asset"][
        "observed_slices"
    ]["BNB"]["rejected_counterfactual"]["never_claimed_as_fill"] = False
    with pytest.raises(
        ValueError,
        match="fixed_subgroup_reporting_counterfactual_guard_missing",
    ):
        freeze.validate_fixed_subgroup_metrics(unsafe, examples, protocol)


def test_v12_walk_forward_protocol_was_pinned_before_outcome_review():
    design = _v12_design()
    protocol = freeze.walk_forward_protocol_for_design(design)
    assert protocol is not None
    freeze.validate_walk_forward_protocol(protocol, design)
    assert protocol["protocol_status"] == (
        "PREREGISTERED_BEFORE_ANY_V12_OUTCOME_REVIEW"
    )
    assert protocol["performance_metrics_inspected_before_preregistration"] is False
    assert protocol["applies_to_design_sha256"] == freeze.design_fingerprint(
        design
    )
    assert freeze.evaluation_protocol_fingerprint(protocol) == (
        freeze.EXPECTED_V12_WALK_FORWARD_PROTOCOL_SHA256
    )
    for cohort, initial, block in (
        ("BTC", 60, 20),
        ("NON_BTC_TRANSFER", 24, 8),
    ):
        minimum = design["cohorts"][cohort]["minimum_complete_close_windows"]
        pretest = int(minimum * 0.8)
        close_times = tuple(
            2_000.0 + 900.0 * index for index in range(pretest)
        )
        folds = freeze.expanding_walk_forward_folds(
            close_times, protocol, cohort,
        )
        assert len(folds) == 3
        assert [len(fold["train"]) for fold in folds] == [
            initial, initial + block, initial + 2 * block,
        ]
    poisoned = json.loads(json.dumps(protocol))
    poisoned["cohorts"]["BTC"]["validation_block_windows"] = 19
    with pytest.raises(ValueError, match="walk_forward_pretest_coverage_mismatch"):
        freeze.validate_walk_forward_protocol(poisoned, design)
    poisoned = json.loads(json.dumps(protocol))
    poisoned["paired_close_window_bootstrap"]["random_seed"] += 1
    with pytest.raises(ValueError, match="walk_forward_bootstrap_seed_mismatch"):
        freeze.validate_walk_forward_protocol(poisoned, design)


def test_paired_bootstrap_is_deterministic_and_clusters_same_close_assets():
    protocol = freeze.walk_forward_protocol_for_design(_v11_design())
    config = protocol["paired_close_window_bootstrap"]
    single = []
    single_probabilities = []
    replicated = []
    replicated_probabilities = []
    for window in range(8):
        label = window % 2
        probability = 0.8 if label else 0.2
        base = {
            "id": window + 1,
            "close_time": 10_000.0 + 900.0 * window,
            "asset": "BTC",
            "label_yes": label,
            "market_yes_probability": 0.55 if label else 0.45,
        }
        single.append(base)
        single_probabilities.append(probability)
        for copy_index in range(6):
            replicated.append({
                **base,
                "id": 100 * window + copy_index + 1,
                "asset": sorted(freeze.COHORT_ASSETS["NON_BTC_TRANSFER"])[
                    copy_index
                ],
            })
            replicated_probabilities.append(probability)
    first = freeze._proper_scores(
        single,
        single_probabilities,
        bootstrap_config=config,
    )["paired_close_window_bootstrap"]
    second = freeze._proper_scores(
        single,
        single_probabilities,
        bootstrap_config=config,
    )["paired_close_window_bootstrap"]
    clustered = freeze._proper_scores(
        replicated,
        replicated_probabilities,
        bootstrap_config=config,
    )["paired_close_window_bootstrap"]
    assert first == second
    assert first["close_windows"] == clustered["close_windows"] == 8
    assert first["rows"] == 8
    assert clustered["rows"] == 48
    assert first["brier_delta"] == clustered["brier_delta"]
    assert first["log_loss_delta"] == clustered["log_loss_delta"]
    assert first["brier_delta"]["one_sided_upper"] < 0.0
    assert first["log_loss_delta"]["one_sided_upper"] < 0.0


def test_calibration_bootstrap_rejects_no_skill_even_when_scores_tie():
    protocol = freeze.walk_forward_protocol_for_design(_v11_design())
    config = protocol["paired_close_window_bootstrap"]
    examples = []
    market = []
    for window in range(12):
        label = window % 2
        probability = 0.55 if label else 0.45
        examples.append({
            "id": window + 1,
            "close_time": float(window),
            "label_yes": label,
            "market_yes_probability": probability,
        })
        market.append(probability)
    report = freeze.calibration_gate(
        examples,
        market,
        bootstrap_config=config,
    )
    assert report["strict_overall_improvement"] is False
    assert report["paired_close_window_uncertainty_met"] is False
    assert report["met"] is False


def test_calibration_bootstrap_rejects_uniform_but_immaterial_skill():
    protocol = freeze.walk_forward_protocol_for_design(_v11_design())
    config = protocol["paired_close_window_bootstrap"]
    examples = []
    candidate = []
    for window in range(12):
        label = window % 2
        examples.append({
            "id": window + 1,
            "close_time": float(window),
            "label_yes": label,
            "market_yes_probability": 0.5,
        })
        candidate.append(0.5001 if label else 0.4999)
    report = freeze.calibration_gate(
        examples,
        candidate,
        bootstrap_config=config,
    )
    assert report["strict_overall_improvement"] is True
    assert report["both_halves_not_worse"] is True
    assert report["overall"]["paired_close_window_bootstrap"][
        "brier_delta"
    ]["one_sided_upper"] < 0.0
    assert report["paired_close_window_uncertainty_met"] is False
    assert report["met"] is False


def test_walk_forward_gate_scores_each_next_block_without_test_rows(monkeypatch):
    examples = _examples(48)
    for row in examples:
        row["label_yes"] = int(row["id"] % 2 == 0)
    design = _v11_design()
    protocol = freeze.walk_forward_protocol_for_design(design)
    fits = []
    monkeypatch.setattr(
        freeze,
        "fit_residual_model",
        lambda rows, config: fits.append(tuple(row["id"] for row in rows)) or {},
    )
    monkeypatch.setattr(
        freeze,
        "predict_probabilities",
        lambda model, rows, config: (
            [0.8 if row["label_yes"] else 0.2 for row in rows],
            [{"out_of_distribution": False} for _ in rows],
        ),
    )
    gate = freeze.expanding_walk_forward_gate(
        examples,
        design["fixed_training_config"],
        protocol,
        "NON_BTC_TRANSFER",
    )
    assert gate["met"] is True
    assert gate["temporary_model_fits"] == 3
    assert gate["temporary_models_are_deployable"] is False
    assert gate["untouched_test_rows_used"] == 0
    assert [fold["train_close_windows"] for fold in gate["folds"]] == [24, 32, 40]
    assert [fold["validation_close_windows"] for fold in gate["folds"]] == [8, 8, 8]
    assert len(fits) == 3


def test_walk_forward_gate_fails_closed_on_partial_same_close(monkeypatch):
    examples = _examples(48)
    for row in examples:
        row["label_yes"] = int(row["id"] % 2 == 0)
    examples.pop()
    design = _v11_design()
    protocol = freeze.walk_forward_protocol_for_design(design)
    with pytest.raises(ValueError, match="walk_forward_cohort_rows_incomplete"):
        freeze.expanding_walk_forward_gate(
            examples,
            design["fixed_training_config"],
            protocol,
            "NON_BTC_TRANSFER",
        )


def test_locked_freeze_cannot_call_any_label_reader_before_readiness(tmp_path: Path):
    calls = []
    artifact, report = freeze.run_locked_freeze(
        design=_design(),
        coverage=_coverage(23),
        feature_rows=[],
        cohort="NON_BTC_TRANSFER",
        prospective_boundary=99.0,
        read_labels=lambda ids: calls.append(("read", tuple(ids))) or {},
        labels_are_available=lambda ids: calls.append(("check", tuple(ids))) or False,
        confirm_score_untouched_test=True,
        test_state_path=tmp_path / "state.json",
    )
    assert artifact is None
    assert report["status"] == "WAITING_FOR_COMPLETE_WINDOWS"
    assert report["outcome_labels_read"] is False
    assert report["untouched_test_labels_read"] is False
    assert report["model_fit_performed"] is False
    assert calls == []


def test_feature_loader_selects_no_outcome_or_pnl_columns(tmp_path: Path):
    database = tmp_path / "ledger.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """
            CREATE TABLE strategy_bot_decisions (
                id INTEGER PRIMARY KEY, bot_name TEXT, source_system TEXT,
                record_kind TEXT, interval TEXT, ticker TEXT, asset TEXT,
                side TEXT, close_time REAL, entry_ask_cents REAL,
                spread_cents REAL, depth_contracts REAL,
                source_captured_at REAL, evidence_as_of REAL,
                threshold_json TEXT,
                kalshi_microstructure_schema_version TEXT,
                kalshi_microstructure_captured_at REAL,
                official_result TEXT, correct INTEGER,
                hypothetical_pnl_cents REAL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO strategy_bot_decisions VALUES (
                1, 'rti_path_13m', 'rti_path_13m',
                'RTI_PATH_13M_PROSPECTIVE_EXACT', '13M', 'KXBTC', 'BTC',
                'YES', 2000.0, 55.0, 1.0, 20.0, 1220.1, 1220.2,
                '{"rti_side":"YES","resolved_accuracy":0.99,"resolved_correct":999,"resolved_net_pnl_cents_per_contract":9999,"official_result":"YES","hypothetical_pnl_cents":9999}',
                'rti-exact-microstructure-v1', 1220.1, 'NO', 0, -55.0
            )
            """
        )
        connection.commit()
    finally:
        connection.close()
    rows = freeze.load_feature_rows(database)
    assert len(rows) == 1
    assert freeze.OUTCOME_COLUMNS.isdisjoint(rows[0])
    assert json.loads(rows[0]["threshold_json"]) == {"rti_side": "YES"}
    assert not any(key.startswith("__safe_feature_profile_") for key in rows[0])
    assert "threshold_json" not in freeze.FEATURE_SELECT_COLUMNS
    assert [row["id"] for row in freeze.load_feature_rows_after(
        database, 1999.9,
    )] == [1]
    assert freeze.load_feature_rows_after(database, 2000.0) == []
    with pytest.raises(ValueError, match="close_boundary_invalid"):
        freeze.load_feature_rows_after(database, float("nan"))


def test_regularized_fit_is_deterministic_and_ood_falls_back_to_market():
    examples = _examples(36)
    for row in examples:
        row["label_yes"] = int(row["id"] % 2 == 0)
    config = _design()["fixed_training_config"]
    first = freeze.fit_residual_model(examples, config)
    second = freeze.fit_residual_model(examples, config)
    assert first == second
    optimizer = first["optimizer"]
    assert optimizer["iterations"] == config["model_iterations"]
    assert optimizer["all_values_finite"] is True
    assert optimizer["final_objective_not_worse"] is True
    assert optimizer["numerical_integrity_verified"] is True
    assert optimizer["final_regularized_objective"] <= (
        optimizer["initial_regularized_objective"]
    )
    assert optimizer["regularized_objective_improvement"] == pytest.approx(
        optimizer["initial_regularized_objective"]
        - optimizer["final_regularized_objective"]
    )
    assert optimizer["final_max_abs_gradient"] >= 0.0
    normal, diagnostics = freeze.predict_probabilities(first, examples[:2], config)
    assert len(normal) == 2
    outlier = dict(examples[0])
    outlier["features"] = [1_000_000.0] * len(freeze.FEATURE_NAMES)
    probabilities, outlier_diagnostics = freeze.predict_probabilities(
        first, [outlier], config
    )
    assert outlier_diagnostics[0]["out_of_distribution"] is True
    assert probabilities[0] == pytest.approx(outlier["market_yes_probability"])
    assert all(row["out_of_distribution"] is False for row in diagnostics)


def test_training_gives_each_close_window_exactly_one_total_weight():
    examples = _examples(6)
    for row in examples:
        row["label_yes"] = int(row["id"] % 2 == 0)
    diagnostics = freeze.window_weight_diagnostics(examples)
    assert diagnostics == {
        "version": "q15-close-window-equal-weight-v1",
        "rows": 36,
        "close_windows": 6,
        "minimum_rows_per_close_window": 6,
        "maximum_rows_per_close_window": 6,
        "total_sample_weight": pytest.approx(6.0),
        "minimum_close_window_weight": pytest.approx(1.0),
        "maximum_close_window_weight": pytest.approx(1.0),
        "every_close_window_total_weight_one": True,
    }

    duplicated = []
    for row in examples:
        for copy_index in range(2):
            duplicated.append({
                **row,
                "id": int(row["id"]) * 10 + copy_index,
            })
    config = _design()["fixed_training_config"]
    base_model = freeze.fit_residual_model(examples, config)
    duplicated_model = freeze.fit_residual_model(duplicated, config)
    assert duplicated_model["means"] == pytest.approx(base_model["means"])
    assert duplicated_model["stds"] == pytest.approx(base_model["stds"])
    assert duplicated_model["weights"] == pytest.approx(base_model["weights"])
    assert duplicated_model["bias"] == pytest.approx(base_model["bias"])
    assert duplicated_model["window_weighting"][
        "every_close_window_total_weight_one"
    ] is True
    assert duplicated_model["window_weighting"][
        "total_sample_weight"
    ] == pytest.approx(6.0)


def test_training_fails_closed_if_window_equal_weighting_is_disabled():
    examples = _examples(2)
    for row in examples:
        row["label_yes"] = int(row["id"] % 2 == 0)
    config = dict(_design()["fixed_training_config"])
    config["window_equal_weighting"] = False
    with pytest.raises(ValueError, match="window_equal_weighting_required"):
        freeze.fit_residual_model(examples, config)


def test_training_fails_closed_on_nonfinite_optimizer_inputs():
    examples = _examples(2)
    for row in examples:
        row["label_yes"] = int(row["id"] % 2 == 0)
    examples[0]["features"][0] = float("nan")
    with pytest.raises(ValueError, match="optimizer_numerical_integrity_failed"):
        freeze.fit_residual_model(
            examples, _design()["fixed_training_config"]
        )


def test_calibration_gate_requires_both_scores_in_both_chronological_halves():
    examples = []
    good = []
    for window in range(12):
        label = window % 2
        examples.append({
            "id": window,
            "close_time": float(window),
            "label_yes": label,
            "market_yes_probability": 0.55 if label else 0.45,
        })
        good.append(0.8 if label else 0.2)
    passed = freeze.calibration_gate(examples, good)
    assert passed["met"] is True
    failed = freeze.calibration_gate(examples, good[:6] + [1.0 - p for p in good[6:]])
    assert failed["met"] is False
    assert failed["both_halves_not_worse"] is False


def test_passing_calibration_still_cannot_read_test_without_explicit_confirmation(
    monkeypatch, tmp_path: Path,
):
    examples = _examples()
    windows = tuple(sorted({row["close_time"] for row in examples}))
    calls = []
    monkeypatch.setattr(
        freeze,
        "prepare_unlabeled_examples",
        lambda rows, design, cohort: (examples, windows),
    )
    monkeypatch.setattr(
        freeze,
        "fit_residual_model",
        lambda rows, config: {
            "means": [0.0] * len(freeze.FEATURE_NAMES),
            "stds": [1.0] * len(freeze.FEATURE_NAMES),
            "weights": [0.0] * len(freeze.FEATURE_NAMES),
            "bias": 0.0,
        },
    )
    monkeypatch.setattr(
        freeze,
        "predict_probabilities",
        lambda model, rows, config: (
            [row["market_yes_probability"] for row in rows],
            [{"out_of_distribution": False} for _ in rows],
        ),
    )
    monkeypatch.setattr(
        freeze,
        "calibration_gate",
        lambda rows, probabilities: {"met": True},
    )

    def read_labels(ids):
        calls.append(tuple(ids))
        return {int(row_id): int(row_id % 2 == 0) for row_id in ids}

    artifact, report = freeze.run_locked_freeze(
        design=_design(),
        coverage=_coverage(60),
        feature_rows=[],
        cohort="NON_BTC_TRANSFER",
        prospective_boundary=max(windows),
        read_labels=read_labels,
        labels_are_available=lambda ids: True,
        confirm_score_untouched_test=False,
        test_state_path=tmp_path / "state.json",
    )
    folds = freeze.chronological_folds(windows, _design())
    test_ids = {
        int(row["id"]) for row in examples
        if row["close_time"] in set(folds["test"])
    }
    assert artifact is None
    assert report["status"] == "CALIBRATION_PASSED_AWAITING_EXPLICIT_TEST_SCORE"
    assert report["outcome_labels_read"] is True
    assert report["untouched_test_labels_read"] is False
    assert not test_ids.intersection(calls[0])
    assert not (tmp_path / "state.json").exists()


def test_v11_failed_walk_forward_gate_never_reads_untouched_test(
    monkeypatch, tmp_path: Path,
):
    design = _v11_design()
    examples = _examples(60)
    windows = tuple(sorted({row["close_time"] for row in examples}))
    calls = []
    monkeypatch.setattr(
        freeze,
        "build_readiness",
        lambda design, coverage: {
            "cohorts": {"NON_BTC_TRANSFER": {"ready_for_locked_freeze": True}}
        },
    )
    monkeypatch.setattr(
        freeze,
        "prepare_unlabeled_examples",
        lambda rows, design, cohort: (examples, windows),
    )
    monkeypatch.setattr(
        freeze,
        "expanding_walk_forward_gate",
        lambda rows, config, protocol, cohort: {
            "met": False,
            "temporary_model_fits": 3,
            "untouched_test_rows_used": 0,
        },
    )
    def forbidden_after_failed_walk_forward(*args, **kwargs):
        raise AssertionError("later_model_or_calibration_gate_was_called")

    monkeypatch.setattr(
        freeze, "fit_residual_model", forbidden_after_failed_walk_forward,
    )
    monkeypatch.setattr(
        freeze, "calibration_gate", forbidden_after_failed_walk_forward,
    )

    def read_labels(ids):
        calls.append(tuple(ids))
        return {int(row_id): int(row_id % 2 == 0) for row_id in ids}

    artifact, report = freeze.run_locked_freeze(
        design=design,
        coverage={},
        feature_rows=[],
        cohort="NON_BTC_TRANSFER",
        prospective_boundary=max(windows),
        read_labels=read_labels,
        labels_are_available=lambda ids: True,
        confirm_score_untouched_test=True,
        test_state_path=tmp_path / "state.json",
    )
    folds = freeze.chronological_folds(windows, design)
    test_ids = {
        int(row["id"]) for row in examples
        if row["close_time"] in set(folds["test"])
    }
    assert artifact is None
    assert report["status"] == "REJECTED_ON_WALK_FORWARD_GATE"
    assert report["outcome_labels_read"] is True
    assert report["model_fit_performed"] is True
    assert report["final_model_fit_performed"] is False
    assert report["walk_forward_gate"]["untouched_test_rows_used"] == 0
    assert not test_ids.intersection(calls[0])
    assert not (tmp_path / "state.json").exists()


def test_test_score_reservation_is_exclusive(tmp_path: Path):
    path = tmp_path / "state.json"
    state = freeze.reserve_test_score(
        path, {"status": "TEST_SCORE_RESERVED"},
    )
    assert freeze.load_test_state(path) == state
    assert state["test_state_version"] == freeze.TEST_STATE_VERSION
    assert state["test_state_sha256"] == freeze.test_state_fingerprint(state)
    with pytest.raises(FileExistsError):
        freeze.reserve_test_score(path, {"status": "TEST_SCORE_RESERVED"})


def _crash_recovery_model():
    width = len(freeze.FEATURE_NAMES)
    return {
        "means": [0.0] * width,
        "stds": [1.0] * width,
        "weights": [0.0] * width,
        "bias": 0.0,
        "window_weighting": {
            "every_close_window_total_weight_one": True,
        },
        "optimizer": {
            "numerical_integrity_verified": True,
        },
    }


def _install_passing_freeze_stubs(monkeypatch, examples, metrics):
    windows = tuple(sorted({float(row["close_time"]) for row in examples}))
    monkeypatch.setattr(
        freeze,
        "prepare_unlabeled_examples",
        lambda rows, design, cohort: (examples, windows),
    )
    monkeypatch.setattr(
        freeze,
        "fit_residual_model",
        lambda rows, config: _crash_recovery_model(),
    )
    monkeypatch.setattr(
        freeze,
        "predict_probabilities",
        lambda model, rows, config: (
            [0.9 if row.get("label_yes") else 0.1 for row in rows],
            [{"out_of_distribution": False} for _ in rows],
        ),
    )
    monkeypatch.setattr(
        freeze,
        "calibration_gate",
        lambda rows, probabilities: {"met": True},
    )
    monkeypatch.setattr(
        freeze,
        "test_metrics",
        lambda *args, **kwargs: dict(metrics),
    )
    return windows


def _passing_test_metrics():
    return {
        "brier_score": 0.1,
        "market_brier_score": 0.2,
        "log_loss": 0.2,
        "market_log_loss": 0.3,
        "picks": 100,
        "ten_contract_net_pnl_dollars": 12.5,
    }


def test_finalized_test_state_recovers_identical_artifact_without_rescore(
    monkeypatch, tmp_path: Path,
):
    examples = _examples()
    windows = _install_passing_freeze_stubs(
        monkeypatch, examples, _passing_test_metrics(),
    )
    folds = freeze.chronological_folds(windows, _design())
    test_ids = {
        int(row["id"]) for row in examples
        if float(row["close_time"]) in set(folds["test"])
    }
    first_reads = []

    def first_reader(ids):
        first_reads.append(tuple(ids))
        return {int(row_id): int(row_id % 2 == 0) for row_id in ids}

    state_path = tmp_path / "state.json"
    first_artifact, first_report = freeze.run_locked_freeze(
        design=_design(),
        coverage=_coverage(60),
        feature_rows=[],
        cohort="NON_BTC_TRANSFER",
        prospective_boundary=max(windows),
        read_labels=first_reader,
        labels_are_available=lambda ids: True,
        confirm_score_untouched_test=True,
        test_state_path=state_path,
    )
    assert first_artifact is not None
    assert first_report["untouched_test_labels_read"] is True
    assert any(test_ids.intersection(call) for call in first_reads)
    finalized = freeze.load_test_state(state_path)
    assert finalized["status"] == "PASSED_UNTOUCHED_TEST_PAPER_ARTIFACT_ONLY"
    assert finalized["test_metrics"] == _passing_test_metrics()
    assert first_artifact["test_state_sha256"] == finalized["test_state_sha256"]

    second_reads = []
    second_checks = []

    def second_reader(ids):
        assert not test_ids.intersection(ids), "untouched test was read twice"
        second_reads.append(tuple(ids))
        return {int(row_id): int(row_id % 2 == 0) for row_id in ids}

    def second_availability(ids):
        assert not test_ids.intersection(ids), "untouched test availability rechecked"
        second_checks.append(tuple(ids))
        return True

    recovered_artifact, recovered_report = freeze.run_locked_freeze(
        design=_design(),
        coverage=_coverage(60),
        feature_rows=[],
        cohort="NON_BTC_TRANSFER",
        prospective_boundary=max(windows) + 9_000.0,
        read_labels=second_reader,
        labels_are_available=second_availability,
        confirm_score_untouched_test=True,
        test_state_path=state_path,
    )
    assert second_reads and second_checks
    assert recovered_artifact == first_artifact
    assert recovered_report["status"] == (
        "RECOVERED_PASSED_UNTOUCHED_TEST_PAPER_ARTIFACT_ONLY"
    )
    assert recovered_report["untouched_test_labels_read"] is False
    assert recovered_report["untouched_test_labels_previously_scored"] is True
    assert recovered_report["recovered_from_finalized_test_state"] is True


def test_ambiguous_reservation_never_rescores_untouched_test(
    monkeypatch, tmp_path: Path,
):
    examples = _examples()
    windows = _install_passing_freeze_stubs(
        monkeypatch, examples, _passing_test_metrics(),
    )
    folds = freeze.chronological_folds(windows, _design())
    test_ids = {
        int(row["id"]) for row in examples
        if float(row["close_time"]) in set(folds["test"])
    }
    state_path = tmp_path / "state.json"
    artifact, _ = freeze.run_locked_freeze(
        design=_design(),
        coverage=_coverage(60),
        feature_rows=[],
        cohort="NON_BTC_TRANSFER",
        prospective_boundary=max(windows),
        read_labels=lambda ids: {
            int(row_id): int(row_id % 2 == 0) for row_id in ids
        },
        labels_are_available=lambda ids: True,
        confirm_score_untouched_test=True,
        test_state_path=state_path,
    )
    assert artifact is not None
    finalized = freeze.load_test_state(state_path)
    reservation = {
        key: value for key, value in finalized.items()
        if key not in {
            "test_state_sha256", "scored_at", "untouched_test_labels_read_once",
            "test_metrics", "test_metrics_sha256", "test_gate",
        }
    }
    reservation["status"] = "TEST_SCORE_RESERVED"
    freeze._update_test_state(state_path, reservation)

    def no_test_access(ids):
        assert not test_ids.intersection(ids), "ambiguous test was accessed"
        return {int(row_id): int(row_id % 2 == 0) for row_id in ids}

    recovered, report = freeze.run_locked_freeze(
        design=_design(),
        coverage=_coverage(60),
        feature_rows=[],
        cohort="NON_BTC_TRANSFER",
        prospective_boundary=max(windows),
        read_labels=no_test_access,
        labels_are_available=lambda ids: (
            not test_ids.intersection(ids)
        ),
        confirm_score_untouched_test=True,
        test_state_path=state_path,
    )
    assert recovered is None
    assert report["status"] == (
        "UNTOUCHED_TEST_SCORE_RESERVED_AMBIGUOUS_NO_RESCORE"
    )
    assert report["untouched_test_labels_read"] is False


def test_tampered_final_state_fails_before_untouched_test_access(
    monkeypatch, tmp_path: Path,
):
    examples = _examples()
    windows = _install_passing_freeze_stubs(
        monkeypatch, examples, _passing_test_metrics(),
    )
    folds = freeze.chronological_folds(windows, _design())
    test_ids = {
        int(row["id"]) for row in examples
        if float(row["close_time"]) in set(folds["test"])
    }
    state_path = tmp_path / "state.json"
    freeze.run_locked_freeze(
        design=_design(),
        coverage=_coverage(60),
        feature_rows=[],
        cohort="NON_BTC_TRANSFER",
        prospective_boundary=max(windows),
        read_labels=lambda ids: {
            int(row_id): int(row_id % 2 == 0) for row_id in ids
        },
        labels_are_available=lambda ids: True,
        confirm_score_untouched_test=True,
        test_state_path=state_path,
    )
    tampered = json.loads(state_path.read_text(encoding="utf-8"))
    tampered["test_metrics"]["ten_contract_net_pnl_dollars"] = 999_999.0
    state_path.write_text(json.dumps(tampered), encoding="utf-8")

    def no_test_access(ids):
        assert not test_ids.intersection(ids), "tampered state triggered test access"
        return {int(row_id): int(row_id % 2 == 0) for row_id in ids}

    with pytest.raises(
        ValueError, match="untouched_test_state_fingerprint_mismatch",
    ):
        freeze.run_locked_freeze(
            design=_design(),
            coverage=_coverage(60),
            feature_rows=[],
            cohort="NON_BTC_TRANSFER",
            prospective_boundary=max(windows),
            read_labels=no_test_access,
            labels_are_available=lambda ids: (
                not test_ids.intersection(ids)
            ),
            confirm_score_untouched_test=True,
            test_state_path=state_path,
        )


def test_rehashed_state_with_wrong_model_binding_fails_before_test_access(
    monkeypatch, tmp_path: Path,
):
    examples = _examples()
    windows = _install_passing_freeze_stubs(
        monkeypatch, examples, _passing_test_metrics(),
    )
    folds = freeze.chronological_folds(windows, _design())
    test_ids = {
        int(row["id"]) for row in examples
        if float(row["close_time"]) in set(folds["test"])
    }
    state_path = tmp_path / "state.json"
    freeze.run_locked_freeze(
        design=_design(),
        coverage=_coverage(60),
        feature_rows=[],
        cohort="NON_BTC_TRANSFER",
        prospective_boundary=max(windows),
        read_labels=lambda ids: {
            int(row_id): int(row_id % 2 == 0) for row_id in ids
        },
        labels_are_available=lambda ids: True,
        confirm_score_untouched_test=True,
        test_state_path=state_path,
    )
    rebound = freeze.load_test_state(state_path)
    rebound["model_sha256"] = "d" * 64
    freeze._update_test_state(state_path, rebound)

    def no_test_access(ids):
        assert not test_ids.intersection(ids), "bad binding triggered test access"
        return {int(row_id): int(row_id % 2 == 0) for row_id in ids}

    with pytest.raises(ValueError, match="untouched_test_state_model_sha_mismatch"):
        freeze.run_locked_freeze(
            design=_design(),
            coverage=_coverage(60),
            feature_rows=[],
            cohort="NON_BTC_TRANSFER",
            prospective_boundary=max(windows),
            read_labels=no_test_access,
            labels_are_available=lambda ids: not test_ids.intersection(ids),
            confirm_score_untouched_test=True,
            test_state_path=state_path,
        )


def test_finalized_rejection_recovers_without_rescore_or_artifact(
    monkeypatch, tmp_path: Path,
):
    metrics = _passing_test_metrics()
    metrics["ten_contract_net_pnl_dollars"] = -1.0
    examples = _examples()
    windows = _install_passing_freeze_stubs(monkeypatch, examples, metrics)
    folds = freeze.chronological_folds(windows, _design())
    test_ids = {
        int(row["id"]) for row in examples
        if float(row["close_time"]) in set(folds["test"])
    }
    state_path = tmp_path / "state.json"
    first_artifact, first_report = freeze.run_locked_freeze(
        design=_design(),
        coverage=_coverage(60),
        feature_rows=[],
        cohort="NON_BTC_TRANSFER",
        prospective_boundary=max(windows),
        read_labels=lambda ids: {
            int(row_id): int(row_id % 2 == 0) for row_id in ids
        },
        labels_are_available=lambda ids: True,
        confirm_score_untouched_test=True,
        test_state_path=state_path,
    )
    assert first_artifact is None
    assert first_report["status"] == "REJECTED_ON_UNTOUCHED_TEST"

    def no_test_access(ids):
        assert not test_ids.intersection(ids), "rejected test was read twice"
        return {int(row_id): int(row_id % 2 == 0) for row_id in ids}

    recovered_artifact, recovered_report = freeze.run_locked_freeze(
        design=_design(),
        coverage=_coverage(60),
        feature_rows=[],
        cohort="NON_BTC_TRANSFER",
        prospective_boundary=max(windows) + 9_000.0,
        read_labels=no_test_access,
        labels_are_available=lambda ids: not test_ids.intersection(ids),
        confirm_score_untouched_test=True,
        test_state_path=state_path,
    )
    assert recovered_artifact is None
    assert recovered_report["status"] == (
        "RECOVERED_REJECTED_ON_UNTOUCHED_TEST"
    )
    assert recovered_report["untouched_test_labels_read"] is False
    assert recovered_report["test_metrics"] == metrics
