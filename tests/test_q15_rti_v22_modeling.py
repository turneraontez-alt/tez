from __future__ import annotations

from copy import deepcopy
import math

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v22_identity as identity
from q15_upgrade.strategy_bots import rti_microstructure_v22_top_book_features as features
from tests.test_q15_rti_v22_feature_seal import _windows
from tools import q15_rti_v22_feature_seal as feature_seal
from tools import q15_rti_v22_modeling as modeling


def _population() -> tuple[dict, dict[int, int]]:
    windows = _windows()
    labels = {}
    indices = {name: index for index, name in enumerate(features.FEATURE_NAMES)}
    for window_index, window in enumerate(windows):
        for asset_index, row in enumerate(window["rows"]):
            label = int((window_index * 3 + asset_index * 2) % 7 < 4)
            labels[int(row["parent_id"])] = label
            values = [0.0] * identity.FEATURE_COUNT
            values[indices["side_is_yes"]] = 1.0 if row["side"] == "YES" else 0.0
            values[indices["parent_distance_bps"]] = (
                (asset_index - 3) / 10.0 + (window_index % 5) / 20.0
            )
            values[indices["parent_distance_to_remaining_volatility"]] = 1.0
            values[indices["log1p_parent_realized_volatility_bps"]] = math.log1p(2.0)
            values[indices["delayed_market_side_probability"]] = (
                0.55 if label else 0.45
            )
            asset_name = f"asset_{str(row['asset']).lower()}"
            if asset_name in indices:
                values[indices[asset_name]] = 1.0
            aligned = 0.9 if label else -0.9
            for name in (
                "side_rest_top_imbalance_13m",
                "side_rest_top_imbalance_12m30s",
                "side_rest_top_imbalance_12m",
                "side_rest_top_imbalance_11m30s",
            ):
                values[indices[name]] = aligned
            values[indices["side_rest_top_imbalance_mean"]] = aligned
            values[indices["side_rest_top_imbalance_min"]] = aligned
            values[indices["rest_top_imbalance_side_persistence"]] = (
                1.0 if label else 0.0
            )
            for name in (
                "log1p_rest_spread_bps_13m",
                "log1p_rest_spread_bps_12m30s",
                "log1p_rest_spread_bps_12m",
                "log1p_rest_spread_bps_11m30s",
                "log1p_rest_spread_bps_mean",
                "log1p_rest_spread_bps_max",
            ):
                values[indices[name]] = math.log1p(1.0)
            values[indices["side_rest_mid_return_30s_bps"]] = aligned
            values[indices["side_rest_mid_return_60s_bps"]] = aligned * 2.0
            values[indices["side_rest_mid_return_90s_bps"]] = aligned * 3.0
            values[indices["side_rest_mid_acceleration_30v90_bps"]] = (
                aligned
            )
            values[indices["log1p_rest_mid_path_range_bps"]] = math.log1p(3.0)
            values[indices["rest_mid_path_trend_efficiency"]] = 0.8
            row["features"] = values
            core = {
                "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
                "parent_id": row["parent_id"],
                "asset": row["asset"],
                "ticker": row["ticker"],
                "close_time": row["close_time"],
                "side": row["side"],
                "parent_source_evidence_sha256": row[
                    "parent_source_evidence_sha256"
                ],
                "intermediate_source_evidence_sha256": row[
                    "intermediate_source_evidence_sha256"
                ],
                "delayed_source_evidence_sha256": row[
                    "delayed_source_evidence_sha256"
                ],
                "rest_evidence_sha256_by_stage": row[
                    "rest_evidence_sha256_by_stage"
                ],
                "feature_names": list(features.FEATURE_NAMES),
                "features": values,
            }
            row["feature_evidence_sha256"] = feature_seal._sha256(core)
    return feature_seal.build_seal(windows), labels


def test_v22_model_contract_expands_frozen_grids_and_base_ablation():
    contract = modeling.load_contract()
    assert len(modeling._candidate_specs("NON_BTC_TRANSFER", contract)) == 28
    assert len(modeling._candidate_specs("BTC", contract)) == 4
    assert contract["base_feature_ablation"]["feature_count"] == 62
    assert contract["partitions"]["probability_calibration"] == [105, 129]
    assert contract["partitions"]["execution_policy_selection"] == [130, 154]
    assert contract["untouched_test"]["one_shot_only"] is True
    assert contract["untouched_test"]["required_gates"] == [
        "FEE_SLIPPAGE_ADJUSTED_PNL_STRICTLY_POSITIVE",
        "CLOSE_CLUSTER_BOOTSTRAP_20TH_PERCENTILE_MEAN_PNL_STRICTLY_POSITIVE",
        "WILSON_95_LOWER_STRICTLY_EXCEEDS_AVERAGE_BREAK_EVEN",
        "ALL_ROW_LOG_LOSS_STRICTLY_BEATS_12M_MARKET",
        "ALL_ROW_BRIER_STRICTLY_BEATS_12M_MARKET",
        "ALL_ROW_LOG_LOSS_STRICTLY_BEATS_INDEPENDENT_62_FEATURE_BASE_ABLATION",
        "ALL_ROW_BRIER_STRICTLY_BEATS_INDEPENDENT_62_FEATURE_BASE_ABLATION",
        "MAXIMUM_DRAWDOWN_PER_PICK_STRICTLY_BELOW_ALL_SOURCE_EXECUTABLE_CONTROL",
        "FROZEN_TEST_VOLUME_AND_SIDE_MINIMA",
    ]
    assert contract["fee_schedule_verification"]["required_fee_type"] == "quadratic"


def test_v22_model_label_scope_excludes_test_and_nonexecutable_policy():
    seal, labels = _population()
    pretest_ids = modeling.required_pretest_label_ids(seal)
    assert len(pretest_ids) == 1050
    test_ids = {
        int(row["parent_id"]) for row in seal["rows"]
        if row["partition"] == modeling.TEST_PARTITION
    }
    assert len(test_ids) == 175
    assert not pretest_ids & test_ids
    nonexec_policy = {
        int(row["parent_id"]) for row in seal["rows"]
        if row["partition"] == modeling.POLICY_PARTITION
        and row["execution_supported"] is False
    }
    assert nonexec_policy
    assert not pretest_ids & nonexec_policy
    contaminated = {row_id: labels[row_id] for row_id in pretest_ids}
    contaminated[next(iter(test_ids))] = 1
    with pytest.raises(ValueError, match="label_identity_invalid"):
        modeling._labeled_pretest_rows(seal, contaminated)

    duplicate_alias = {row_id: labels[row_id] for row_id in pretest_ids}
    duplicate_id = next(iter(pretest_ids))
    duplicate_alias[str(duplicate_id)] = duplicate_alias[duplicate_id]
    with pytest.raises(ValueError, match="label_identity_invalid"):
        modeling._labeled_pretest_rows(seal, duplicate_alias)


def test_v22_policy_pnl_uses_official_fee_slippage_and_rejects_fake_fill():
    contract = modeling.load_contract()
    rows = [{
        "parent_id": index + 1,
        "close_time": float(index // 2),
        "asset": "ETH",
        "side": "YES" if index % 2 == 0 else "NO",
        "entry_ask_cents": 40.0,
        "sim_contracts": 10.0,
        "execution_supported": True,
        "label_survives": 1,
    } for index in range(30)]
    probabilities = modeling.np.full(30, 0.9)
    report = modeling._margin_report(
        rows, probabilities, 0.0, "NON_BTC_TRANSFER", contract,
    )
    expected_per_contract = modeling.rti_simulated_net_pnl_cents(
        40.0, True, 10, 2.0,
    )
    assert report["picks"] == 30
    assert report["gate_met"] is True
    assert report["fee_slippage_adjusted_pnl_cents_10_contracts"] == (
        pytest.approx(expected_per_contract * 10 * 30)
    )
    assert report[
        "bootstrap_20th_percentile_mean_pnl_cents_per_10_contract_pick"
    ] > 0.0
    rows[0]["execution_supported"] = False
    with pytest.raises(ValueError, match="fake_or_partial_fill"):
        modeling._margin_report(
            rows, probabilities, 0.0, "NON_BTC_TRANSFER", contract,
        )


def test_v22_policy_calibrator_selection_uses_disjoint_proper_scores():
    class BadPlatt:
        def predict_proba(self, logits):
            probabilities = modeling.np.full(len(logits), 0.5)
            return modeling.np.column_stack((1.0 - probabilities, probabilities))

    selected = modeling._select_calibrator_on_policy(
        base_probabilities=modeling.np.asarray([0.9, 0.1, 0.85, 0.15]),
        platt_calibrator=BadPlatt(),
        labels=modeling.np.asarray([1, 0, 1, 0]),
        market_probabilities=modeling.np.asarray([0.55, 0.45, 0.55, 0.45]),
        contract=modeling.load_contract(),
    )
    assert selected["selected_method"] == "IDENTITY"
    assert selected["gate_met"] is True


def test_v22_complete_synthetic_pretest_is_disjoint_and_never_promotes():
    seal, labels = _population()
    pretest_ids = modeling.required_pretest_label_ids(seal)
    result = modeling.evaluate_pretest(
        seal, {row_id: labels[row_id] for row_id in pretest_ids},
    )
    report = result["report"]
    assert report["train_calibration_policy_label_rows"] == 1050
    assert report["calibration_and_policy_partitions_disjoint"] is True
    assert report["pretest_gate_met"] is True
    assert all(result["artifacts"][cohort] is not None for cohort in modeling.COHORTS)
    assert all(
        report["cohorts"][cohort][
            "base_feature_ablation_selected_independently"
        ] is True
        and len(report["cohorts"][cohort][
            "base_feature_ablation_candidate_reports"
        ]) == (28 if cohort == "NON_BTC_TRANSFER" else 4)
        for cohort in modeling.COHORTS
    )
    assert report["untouched_test_labels_read"] is False
    assert report["paper_artifact_created"] is False
    assert report["notification_eligible"] is False
    assert report["automatic_promotion"] is False
    assert report["real_trading_allowed"] is False
    assert set(report["cohorts"]) == set(modeling.COHORTS)


def test_v22_untouched_rejects_duplicate_integer_alias_before_scoring():
    seal, labels = _population()
    test_ids = {
        int(row["parent_id"]) for row in seal["rows"]
        if row["partition"] == modeling.TEST_PARTITION
    }
    supplied = {row_id: labels[row_id] for row_id in test_ids}
    duplicate_id = next(iter(test_ids))
    supplied[str(duplicate_id)] = supplied[duplicate_id]
    with pytest.raises(ValueError, match="label_identity_invalid"):
        modeling.evaluate_untouched_test(seal, supplied, {})


def test_v22_untouched_test_uses_frozen_artifacts_without_refit_if_pretest_passes():
    seal, labels = _population()
    pretest_ids = modeling.required_pretest_label_ids(seal)
    pretest = modeling.evaluate_pretest(
        seal, {row_id: labels[row_id] for row_id in pretest_ids},
    )
    assert pretest["report"]["pretest_gate_met"] is True
    bundle = {
        "feature_seal_sha256": seal["seal_sha256"],
        "evaluator_contract_sha256": identity.EVALUATOR_CONTRACT_SHA256,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "cohorts": pretest["artifacts"],
    }
    test_ids = {
        int(row["parent_id"]) for row in seal["rows"]
        if row["partition"] == modeling.TEST_PARTITION
    }
    report = modeling.evaluate_untouched_test(
        seal, {row_id: labels[row_id] for row_id in test_ids}, bundle,
    )
    assert report["untouched_test_label_rows"] == 175
    assert report["independent_final_historical_confirmation"] is True
    assert report["test_guided_refit_recalibration_or_margin_selection"] is False
    assert report["paper_artifact_created"] is False
    assert report["automatic_promotion"] is False
    assert report["real_trading_allowed"] is False
    assert set(report["cohorts"]) == set(modeling.COHORTS)
    non_btc = report["cohorts"]["NON_BTC_TRANSFER"]
    assert set(non_btc["model_probability_metrics"]) >= {
        "market_log_loss", "market_brier_score",
        "base_feature_ablation_log_loss", "base_feature_ablation_brier_score",
    }
    assert non_btc["base_feature_ablation_used_for_v22_gates"] is True
    assert "maximum_drawdown_cents_per_pick" in non_btc["candidate"]["metrics"]
    assert set(non_btc["candidate"]["subgroups"]) == {
        "ASSET", "RTI_SIDE", "ABSOLUTE_DISTANCE_TIER", "VOLATILITY_TIER",
        "MARKET_REGIME", "REST_IMBALANCE_PERSISTENCE_TIER",
        "REST_SPREAD_TIER", "REST_PATH_CURVATURE_TIER",
    }
    assert "metrics" in non_btc["rejected_trade_counterfactual"]


def test_v22_untouched_bundle_identity_tamper_fails_before_scoring():
    seal, labels = _population()
    pretest_ids = modeling.required_pretest_label_ids(seal)
    pretest = modeling.evaluate_pretest(
        seal, {row_id: labels[row_id] for row_id in pretest_ids},
    )
    assert pretest["report"]["pretest_gate_met"] is True
    bundle = {
        "feature_seal_sha256": seal["seal_sha256"],
        "evaluator_contract_sha256": "0" * 64,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "cohorts": deepcopy(pretest["artifacts"]),
    }
    test_ids = {
        int(row["parent_id"]) for row in seal["rows"]
        if row["partition"] == modeling.TEST_PARTITION
    }
    with pytest.raises(ValueError, match="input_identity_invalid"):
        modeling.evaluate_untouched_test(
            seal, {row_id: labels[row_id] for row_id in test_ids}, bundle,
        )
