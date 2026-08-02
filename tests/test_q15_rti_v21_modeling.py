from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v21_features as features
from q15_upgrade.strategy_bots import rti_microstructure_v21_identity as identity
from tools import q15_rti_v21_feature_seal as seal
from tools import q15_rti_v21_modeling as modeling


ASSETS = ("BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP")
EASTERN = ZoneInfo("America/New_York")


def _ticker(asset: str, close_time: float) -> str:
    close = datetime.fromtimestamp(close_time, tz=EASTERN)
    return (
        f"KX{asset}15M-{close:%y}{close:%b}".upper()
        + f"{close:%d%H%M}-{close:%M}"
    )


def _row(window_index: int, asset_index: int, close_time: float) -> tuple[dict, int]:
    asset = ASSETS[asset_index]
    parent_id = window_index * 100 + asset_index + 1
    intermediate_id = 1_000_000 + parent_id
    delayed_id = 2_000_000 + parent_id
    side = "YES" if (window_index + asset_index) % 2 == 0 else "NO"
    signal_label = 1 if (window_index * 3 + asset_index) % 5 in {0, 1, 2} else 0
    label = (
        1 - signal_label
        if 105 <= window_index <= 129 and window_index % 5 == 0
        else signal_label
    )
    values = [0.0] * identity.FEATURE_COUNT
    values[0] = 1.0 if side == "YES" else 0.0
    signal_amplitude = 2.0
    values[1] = (
        signal_amplitude if signal_label else -signal_amplitude
    ) + (asset_index - 3) / 50.0
    values[2] = (window_index % 13) / 13.0
    values[45] = 0.50
    values[53] = (
        signal_amplitude / 2.0 if signal_label else -signal_amplitude / 2.0
    ) + (window_index % 3) / 20.0
    asset_feature = {
        "BNB": 46, "DOGE": 47, "ETH": 48,
        "HYPE": 49, "SOL": 50, "XRP": 51,
    }.get(asset)
    if asset_feature is not None:
        values[asset_feature] = 1.0
    ticker = _ticker(asset, close_time)
    base_hash = seal._canonical_sha256({
        "window": window_index, "asset": asset, "base": True,
    })
    intermediate_hash = seal._canonical_sha256({
        "window": window_index, "asset": asset, "stage": 30,
    })
    delayed_hash = seal._canonical_sha256({
        "window": window_index, "asset": asset, "stage": 60,
    })
    feature_hash = seal._canonical_sha256({
        "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
        "parent_id": parent_id,
        "intermediate_id": intermediate_id,
        "delayed_id": delayed_id,
        "asset": asset,
        "ticker": ticker,
        "close_time": close_time,
        "side": side,
        "base_feature_evidence_sha256": base_hash,
        "feature_names": list(features.FEATURE_NAMES),
        "features": values,
    })
    cohort = "BTC" if asset == "BTC" else "NON_BTC_TRANSFER"
    execution_supported = not (
        (asset == "BNB" and 90 <= window_index <= 104)
        or (
            asset == "HYPE"
            and (
                110 <= window_index <= 114
                or 140 <= window_index <= 144
                or 160 <= window_index <= 164
            )
        )
    )
    depth = 20.0 if execution_supported else 4.0
    source_hash = seal._canonical_sha256({
        "parent_id": parent_id,
        "intermediate_id": intermediate_id,
        "delayed_id": delayed_id,
        "asset": asset,
        "cohort": cohort,
        "ticker": ticker,
        "close_time": close_time,
        "side": side,
        "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
        "feature_evidence_sha256": feature_hash,
        "base_feature_evidence_sha256": base_hash,
        "intermediate_source_evidence_sha256": intermediate_hash,
        "delayed_source_evidence_sha256": delayed_hash,
        "feature_count": identity.FEATURE_COUNT,
        "execution_supported": execution_supported,
        "entry_ask_cents": 40.0,
        "spread_cents": 1.0,
        "depth_contracts": depth,
        "sim_contracts": 10.0,
    })
    v18_hash = seal._canonical_sha256({"parent": parent_id, "benchmark": "v18"})
    v19_hash = seal._canonical_sha256({"parent": parent_id, "benchmark": "v19"})
    benchmarks = {
        "matched_v18_eligible": False,
        "matched_v18_feature_evidence_sha256": v18_hash,
        "matched_v19_eligible": False,
        "matched_v19_feature_evidence_sha256": v19_hash,
        "v20_base_feature_evidence_sha256": base_hash,
    }
    return ({
        "parent_id": parent_id,
        "intermediate_id": intermediate_id,
        "delayed_id": delayed_id,
        "asset": asset,
        "cohort": cohort,
        "ticker": ticker,
        "close_time": close_time,
        "side": side,
        "execution_supported": execution_supported,
        "entry_ask_cents": 40.0,
        "spread_cents": 1.0,
        "depth_contracts": depth,
        "sim_contracts": 10.0,
        "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
        "feature_names_sha256": identity.FEATURE_NAMES_SHA256,
        "intermediate_source_evidence_sha256": intermediate_hash,
        "delayed_source_evidence_sha256": delayed_hash,
        "feature_evidence_sha256": feature_hash,
        "source_feature_evidence_sha256": source_hash,
        **benchmarks,
        "matched_benchmark_evidence_sha256": seal._canonical_sha256(benchmarks),
        "features": values,
    }, label)


def _population() -> tuple[dict, dict[int, int]]:
    windows = []
    labels = {}
    for window_index in range(identity.MINIMUM_COMPLETE_CLOSE_WINDOWS):
        close_time = identity.FIRST_ELIGIBLE_CLOSE_TIME + window_index * 900.0
        rows = []
        for asset_index in range(7):
            row, label = _row(window_index, asset_index, close_time)
            rows.append(row)
            labels[int(row["parent_id"])] = label
        windows.append({"close_time": close_time, "rows": rows})
    return seal.build_seal(windows), labels


def test_v21_model_contract_expands_exact_frozen_candidate_grids():
    contract = modeling.load_contract()
    assert len(modeling._candidate_specs("NON_BTC_TRANSFER", contract)) == 28
    assert len(modeling._candidate_specs("BTC", contract)) == 4
    assert contract["preprocessing"]["probability_clip"] == [0.000001, 0.999999]
    assert contract["probability_calibration"]["tol"] == 0.00000001
    assert contract["partitions"]["probability_calibration"] == [105, 129]
    assert contract["partitions"]["execution_policy_selection"] == [130, 154]
    assert contract["untouched_test"]["required_gates"] == [
        "FEE_SLIPPAGE_ADJUSTED_PNL_STRICTLY_POSITIVE",
        "CLOSE_CLUSTER_BOOTSTRAP_20TH_PERCENTILE_MEAN_PNL_STRICTLY_POSITIVE",
        "WILSON_95_LOWER_STRICTLY_EXCEEDS_AVERAGE_BREAK_EVEN",
        "ALL_ROW_LOG_LOSS_STRICTLY_BEATS_12M_MARKET",
        "ALL_ROW_BRIER_STRICTLY_BEATS_12M_MARKET",
        "ALL_ROW_LOG_LOSS_STRICTLY_BEATS_V20_FEATURE_MAP_ABLATION",
        "ALL_ROW_BRIER_STRICTLY_BEATS_V20_FEATURE_MAP_ABLATION",
        "MAXIMUM_DRAWDOWN_PER_PICK_STRICTLY_BELOW_ALL_SOURCE_EXECUTABLE_CONTROL",
        "FROZEN_TEST_VOLUME_AND_SIDE_MINIMA",
    ]
    assert contract["fee_schedule_verification"]["required_fee_type"] == "quadratic"
    assert contract["v20_feature_map_ablation"]["feature_count"] == 52
    assert contract["v20_feature_map_ablation"][
        "selection_uses_only_train_internal_walk_forward"
    ] is True
    assert contract["v20_feature_map_ablation"][
        "selected_ablation_spec_may_differ_from_v21"
    ] is True


def test_v21_pretest_label_scope_excludes_untouched_test_exactly():
    feature_seal, labels = _population()
    pretest_ids = modeling.required_pretest_label_ids(feature_seal)
    pretest_labels = {row_id: labels[row_id] for row_id in pretest_ids}
    rows = modeling._labeled_pretest_rows(feature_seal, pretest_labels)
    assert len(rows) == 155 * 7
    assert {
        int(row["parent_id"]) for row in rows if "label_survives" in row
    } == pretest_ids
    assert not any(row["partition"] == modeling.TEST_PARTITION for row in rows)
    nonexecuted_calibration_id = next(
        int(row["parent_id"]) for row in feature_seal["rows"]
        if row["partition"] == modeling.CALIBRATION_PARTITION
        and row["execution_supported"] is False
    )
    nonexecuted_policy_id = next(
        int(row["parent_id"]) for row in feature_seal["rows"]
        if row["partition"] == modeling.POLICY_PARTITION
        and row["execution_supported"] is False
    )
    assert nonexecuted_calibration_id in pretest_ids
    assert nonexecuted_policy_id not in pretest_ids

    test_id = next(
        int(row["parent_id"]) for row in feature_seal["rows"]
        if row["partition"] == modeling.TEST_PARTITION
    )
    contaminated = dict(pretest_labels)
    contaminated[test_id] = labels[test_id]
    with pytest.raises(ValueError, match="label_identity_invalid"):
        modeling._labeled_pretest_rows(feature_seal, contaminated)

    contaminated = dict(pretest_labels)
    contaminated[nonexecuted_policy_id] = labels[nonexecuted_policy_id]
    with pytest.raises(ValueError, match="label_identity_invalid"):
        modeling._labeled_pretest_rows(feature_seal, contaminated)


def test_v21_walk_forward_primary_scores_ignore_never_trained_nonfills():
    feature_seal, labels = _population()
    pretest_ids = modeling.required_pretest_label_ids(feature_seal)
    rows = modeling._labeled_pretest_rows(
        feature_seal, {row_id: labels[row_id] for row_id in pretest_ids},
    )
    non_btc = [row for row in rows if row["cohort"] == "NON_BTC_TRANSFER"]
    contract = modeling.load_contract()
    spec = modeling._candidate_specs("NON_BTC_TRANSFER", contract)[2]
    first = modeling._candidate_walk_forward(
        non_btc, spec, "NON_BTC_TRANSFER", contract,
    )
    ablation = modeling._candidate_walk_forward(
        non_btc, spec, "NON_BTC_TRANSFER", contract,
        feature_indices=tuple(range(52)),
    )
    assert first["feature_count"] == identity.FEATURE_COUNT
    assert ablation["feature_count"] == 52
    assert ablation["feature_indices_sha256"] != first["feature_indices_sha256"]
    mutated = deepcopy(non_btc)
    for row in mutated:
        if (
            row["partition"] == modeling.TRAIN_PARTITION
            and row["execution_supported"] is False
        ):
            row["label_survives"] = 1 - int(row["label_survives"])
    second = modeling._candidate_walk_forward(
        mutated, spec, "NON_BTC_TRANSFER", contract,
    )
    assert first["executable_log_loss"] == pytest.approx(
        second["executable_log_loss"], abs=1e-12
    )
    assert first["executable_brier_score"] == pytest.approx(
        second["executable_brier_score"], abs=1e-12
    )
    assert first["all_feature_rows_log_loss"] != pytest.approx(
        second["all_feature_rows_log_loss"], abs=1e-8
    )


def test_v21_policy_scoring_rejects_fake_fill_and_clusters_pnl():
    contract = modeling.load_contract()
    rows = []
    probabilities = []
    for index in range(30):
        rows.append({
            "parent_id": index + 1,
            "close_time": float(index // 2),
            "asset": "ETH",
            "side": "YES" if index % 2 == 0 else "NO",
            "entry_ask_cents": 40.0,
            "sim_contracts": 10.0,
            "execution_supported": True,
            "label_survives": 1,
        })
        probabilities.append(0.9)
    report = modeling._margin_report(
        rows, modeling.np.asarray(probabilities), 0.0,
        "NON_BTC_TRANSFER", contract,
    )
    assert report["picks"] == 30
    assert report["gate_met"] is True
    expected_per_contract = modeling.rti_simulated_net_pnl_cents(
        40.0, True, 10, 2.0,
    )
    assert report["fee_slippage_adjusted_pnl_cents_10_contracts"] == pytest.approx(
        expected_per_contract * 10 * 30,
    )
    assert report["bootstrap_20th_percentile_mean_pnl_cents_per_10_contract_pick"] > 0

    rows[0]["execution_supported"] = False
    with pytest.raises(ValueError, match="fake_or_partial_fill"):
        modeling._margin_report(
            rows, modeling.np.asarray(probabilities), 0.0,
            "NON_BTC_TRANSFER", contract,
        )


def test_v21_disjoint_calibrator_selection_keeps_better_identity_model():
    class BadPlatt:
        def predict_proba(self, logits):
            probabilities = modeling.np.full(len(logits), 0.5)
            return modeling.np.column_stack((1.0 - probabilities, probabilities))

    contract = modeling.load_contract()
    labels = modeling.np.asarray([1, 0, 1, 0], dtype=int)
    base = modeling.np.asarray([0.90, 0.10, 0.85, 0.15], dtype=float)
    market = modeling.np.asarray([0.55, 0.45, 0.55, 0.45], dtype=float)
    selected = modeling._select_calibrator_on_policy(
        base_probabilities=base,
        platt_calibrator=BadPlatt(),
        labels=labels,
        market_probabilities=market,
        contract=contract,
    )
    assert selected["selected_method"] == "IDENTITY"
    assert selected["gate_met"] is True
    assert selected["selected_scores"]["log_loss"] < selected["candidate_scores"][
        "L2_REGULARIZED_PLATT_ON_LOGIT"
    ]["log_loss"]


def test_v21_complete_synthetic_pretest_never_opens_test_or_promotes():
    feature_seal, labels = _population()
    pretest_ids = modeling.required_pretest_label_ids(feature_seal)
    result = modeling.evaluate_pretest(
        feature_seal, {row_id: labels[row_id] for row_id in pretest_ids},
    )
    report = result["report"]
    assert report["train_calibration_policy_label_rows"] == 1080
    assert report["calibration_and_policy_partitions_disjoint"] is True
    assert report["pretest_gate_met"] is True
    assert all(
        report["cohorts"][cohort]["pretest_gate_met"] is True
        for cohort in modeling.COHORTS
    )
    assert all(result["artifacts"][cohort] is not None for cohort in modeling.COHORTS)
    assert all(
        "v20_feature_map_ablation_base_model" in result["artifacts"][cohort]
        and "v20_feature_map_ablation_selected_spec"
        in result["artifacts"][cohort]
        and "v20_feature_map_ablation_selected_model_id"
        in result["artifacts"][cohort]
        and "v20_feature_map_ablation_platt_calibrator"
        in result["artifacts"][cohort]
        and "selected_calibrator_method" in result["artifacts"][cohort]
        and "v20_feature_map_ablation_selected_calibrator_method"
        in result["artifacts"][cohort]
        for cohort in modeling.COHORTS
    )
    assert all(
        report["cohorts"][cohort][
            "v20_feature_map_ablation_selected_independently"
        ] is True
        and len(report["cohorts"][cohort][
            "v20_feature_map_ablation_candidate_reports"
        ]) == (28 if cohort == "NON_BTC_TRANSFER" else 4)
        for cohort in modeling.COHORTS
    )
    assert report["untouched_test_labels_read"] is False
    assert report["paper_artifact_created"] is False
    assert report["notification_eligible"] is False
    assert report["automatic_promotion"] is False
    assert report["real_trading_allowed"] is False


def test_v21_untouched_test_scores_only_test_once_with_all_frozen_benchmarks():
    feature_seal, labels = _population()
    pretest_ids = modeling.required_pretest_label_ids(feature_seal)
    pretest = modeling.evaluate_pretest(
        feature_seal, {row_id: labels[row_id] for row_id in pretest_ids},
    )
    bundle = {
        "feature_seal_sha256": feature_seal["seal_sha256"],
        "evaluator_contract_sha256": identity.EVALUATOR_CONTRACT_SHA256,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "cohorts": pretest["artifacts"],
    }
    test_ids = {
        int(row["parent_id"]) for row in feature_seal["rows"]
        if row["partition"] == modeling.TEST_PARTITION
    }
    report = modeling.evaluate_untouched_test(
        feature_seal, {row_id: labels[row_id] for row_id in test_ids}, bundle,
    )
    assert report["untouched_test_label_rows"] == 175
    assert report["independent_final_historical_confirmation"] is True
    assert report["test_guided_refit_recalibration_or_margin_selection"] is False
    assert report["paper_artifact_created"] is False
    assert report["automatic_promotion"] is False
    assert report["real_trading_allowed"] is False
    non_btc = report["cohorts"]["NON_BTC_TRANSFER"]
    assert non_btc["nonexecutable_rows_without_trade_or_pnl"] == 5
    assert set(non_btc["model_probability_metrics"]) >= {
        "market_log_loss", "market_brier_score",
        "v20_feature_map_ablation_log_loss",
        "v20_feature_map_ablation_brier_score",
    }
    assert non_btc["v20_feature_map_ablation_used_for_v21_gates"] is True
    assert non_btc["gate_checks"][
        "all_row_log_loss_strictly_beats_v20_feature_map_ablation"
    ] is False
    assert non_btc["gate_met"] is False
    assert "maximum_drawdown_cents_per_pick" in non_btc["candidate"]["metrics"]
    assert set(non_btc["candidate"]["subgroups"]) == {
        "ASSET", "RTI_SIDE", "DISTANCE_TIER", "VOLATILITY_TIER",
        "MARKET_REGIME", "REVERSAL_RISK", "SETTLEMENT_AVERAGE_RISK",
        "TRAJECTORY_CURVATURE_TIER",
    }

    contaminated = {row_id: labels[row_id] for row_id in test_ids}
    contaminated[next(iter(pretest_ids))] = 1
    with pytest.raises(ValueError, match="input_identity_invalid"):
        modeling.evaluate_untouched_test(feature_seal, contaminated, bundle)

    tampered_bundle = deepcopy(bundle)
    tampered_bundle["evaluator_contract_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="input_identity_invalid"):
        modeling.evaluate_untouched_test(
            feature_seal,
            {row_id: labels[row_id] for row_id in test_ids},
            tampered_bundle,
        )
