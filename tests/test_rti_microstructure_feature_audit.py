from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from tools.q15_rti_microstructure_feature_audit import (
    EXPECTED_V12_COVARIATE_DRIFT_PROTOCOL_SHA256,
    EXPECTED_V12_GEOMETRY_REVIEW_PROTOCOL_SHA256,
    EXPECTED_V13_COVARIATE_DRIFT_PROTOCOL_SHA256,
    EXPECTED_V13_GEOMETRY_REVIEW_PROTOCOL_SHA256,
    build_feature_audit,
    correlation_diagnostics,
    decision_timing_integrity,
    feature_statistics,
    geometry_review_protocol_fingerprint,
    matrix_geometry_diagnostics,
    soft_input_integrity,
    v12_covariate_drift_protocol,
    v12_covariate_drift_review,
    v12_geometry_review,
    v12_geometry_review_protocol,
    validate_v12_covariate_drift_protocol,
    validate_v12_geometry_review_protocol,
    v13_covariate_drift_protocol,
    v13_covariate_drift_review,
    v13_geometry_review,
    v13_geometry_review_protocol,
    validate_v13_covariate_drift_protocol,
    validate_v13_geometry_review_protocol,
)
from tools.q15_rti_microstructure_preregister import DEFAULT_DESIGN
from tools.q15_rti_microstructure_preregister import DEFAULT_DESIGN_V2
from tools.q15_rti_microstructure_preregister import DEFAULT_DESIGN_V3
from tools.q15_rti_microstructure_preregister import DEFAULT_DESIGN_V12
from tools.q15_rti_microstructure_preregister import DEFAULT_DESIGN_V13
from tools.q15_rti_microstructure_preregister import DEFAULT_DESIGN_V14


def _design():
    return json.loads(DEFAULT_DESIGN.read_text(encoding="utf-8"))


def _row(asset: str, close_time: float, row_id: int) -> dict:
    target = close_time - 780.0 + 0.1
    profile = {
        "rti_side": "YES",
        "rti_market_mid_probability": 0.545,
        "rti_opposite_ask_cents": 46.0,
        "rti_opposite_depth_contracts": 30.0,
        "rti_signed_distance_bps": 2.0 + row_id / 100.0,
        "rti_side_move_bps": 1.0,
        "rti_path_first_half_side_move_bps": 0.4,
        "rti_path_second_half_side_move_bps": 0.6,
        "rti_path_acceleration_bps": 0.2,
        "rti_path_range_bps": 3.0,
        "rti_path_realized_volatility_bps": 2.0,
        "rti_path_trend_efficiency": 0.5,
        "rti_path_persistence": 0.9,
        "rti_path_strike_crossings": 0,
        "rti_path_seconds_since_last_crossing": None,
        "rti_expected_remaining_volatility_bps": 10.0,
        "rti_distance_to_remaining_volatility": 0.2,
        "spot_depth_imbalance": 0.25,
        "spot_depth_trade_net_notional_15s": 99.0 + row_id,
        "spot_depth_trade_net_notional_60s": -999.0 - row_id,
        "kalshi_yes_microprice_edge_cents": 0.5,
        "kalshi_book_delta_pressure_yes_5s": 0.2,
        "kalshi_book_delta_pressure_yes_15s": 0.1 + row_id / 1000.0,
        "kalshi_book_delta_pressure_yes_30s": -0.3,
        "kalshi_book_delta_pressure_yes_60s": -0.2 + row_id / 2000.0,
        "kalshi_trade_imbalance_yes_5s": 0.4,
        "kalshi_trade_imbalance_yes_30s": -0.5,
        "kalshi_taker_yes_volume_5s": 3.0,
        "kalshi_taker_no_volume_5s": 1.0,
        "kalshi_taker_yes_volume_30s": 2.0,
        "kalshi_taker_no_volume_30s": 6.0,
        "kalshi_taker_yes_volume_15s": 3.0 + row_id / 10.0,
        "kalshi_taker_no_volume_15s": 2.0,
        "kalshi_taker_yes_volume_60s": 4.0,
        "kalshi_taker_no_volume_60s": 2.0,
        "kalshi_yes_best_depletion_30s": 10.0,
        "kalshi_no_best_depletion_30s": 40.0,
        "kalshi_yes_best_refill_30s": 30.0,
        "kalshi_no_best_refill_30s": 20.0,
        "quote_captured_at": target,
        "kalshi_microstructure_captured_at": target,
        "rti_evaluated_at": target + 0.1,
    }
    return {
        "id": row_id,
        "bot_name": "rti_path_13m",
        "source_system": "rti_path_13m",
        "record_kind": "RTI_PATH_13M_PROSPECTIVE_EXACT",
        "interval": "13M",
        "ticker": f"KX{asset}-{int(close_time)}",
        "asset": asset,
        "side": "YES",
        "close_time": close_time,
        "entry_ask_cents": 55.0,
        "spread_cents": 1.0,
        "depth_contracts": 25.0,
        "source_captured_at": target,
        "evidence_as_of": target + 0.1,
        "kalshi_microstructure_schema_version": "rti-exact-microstructure-v1",
        "kalshi_microstructure_captured_at": target,
        "threshold_json": json.dumps(profile),
    }


def test_statistics_detect_tiny_nonzero_variance_and_nonfinite_values():
    names = ("stable", "tiny", "bad")
    matrix = (
        (1.0, 1.0, 0.0),
        (1.0, 1.0 + 1e-10, math.inf),
        (1.0, 1.0 - 1e-10, 2.0),
    )
    stats = feature_statistics(names, matrix, minimum_std=1e-8)
    assert stats["stable"]["constant"] is True
    assert stats["tiny"]["tiny_nonzero_variance"] is True
    assert stats["bad"]["nonfinite"] == 1


def test_correlation_audit_finds_signed_duplicates_but_skips_constants():
    names = ("x", "same", "opposite", "constant")
    matrix = tuple((x, x, -x, 1.0) for x in (1.0, 2.0, 3.0, 4.0))
    stats = feature_statistics(names, matrix, minimum_std=1e-8)
    audit = correlation_diagnostics(
        names, matrix, stats, minimum_std=1e-8,
    )
    relationships = {
        (row["left"], row["right"]): row["relationship"]
        for row in audit["exact_signed_duplicate_pairs"]
    }
    assert relationships[("x", "same")] == "same"
    assert relationships[("x", "opposite")] == "opposite"
    assert all("constant" not in (row["left"], row["right"])
               for row in audit["high_absolute_correlation_pairs"])


def test_geometry_audit_finds_multivariate_rank_deficiency():
    names = ("x", "y", "x_plus_y", "constant")
    matrix = tuple(
        (x, y, x + y, 1.0)
        for x, y in ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (2.0, 1.0))
    )
    stats = feature_statistics(names, matrix, minimum_std=1e-8)
    geometry = matrix_geometry_diagnostics(
        names, matrix, stats, minimum_std=1e-8,
    )
    assert geometry["finite"] is True
    assert geometry["active_feature_count"] == 3
    assert geometry["inactive_feature_count"] == 1
    assert geometry["numerical_rank"] == 2
    assert geometry["rank_deficiency_vs_active_features"] == 1
    assert 1.0 <= geometry["stable_rank"] <= 2.0
    assert geometry["condition_number_nonzero_subspace"] >= 1.0


def test_soft_input_integrity_exposes_neutralized_rows_without_changing_credit():
    examples = (
        {
            "id": 1, "asset": "BTC", "close_time": 2_000.0,
            "features": [1.0, 0.0, 0.0],
        },
        {
            "id": 2, "asset": "BNB", "close_time": 2_000.0,
            "features": [2.0, 1.0, 0.0],
        },
        {
            "id": 3, "asset": "ETH", "close_time": 2_900.0,
            "features": [3.0, 0.0, 1.0],
        },
    )
    report = soft_input_integrity(
        examples, ("signal", "spot_flow_missing", "vwap_missing_30s"),
    )
    assert report["status"] == "SOFT_DEGRADATION_PRESENT"
    assert report["fully_observed_rows"] == 1
    assert report["soft_degraded_rows"] == 2
    assert report["fully_observed_close_windows"] == 0
    assert report["soft_degraded_close_windows"] == 2
    assert report["degraded_by_asset"] == {"BNB": 1, "ETH": 1}
    assert report["degraded_by_reason"] == {
        "spot_flow_missing": 1,
        "vwap_missing_30s": 1,
    }
    assert report["changes_executable_window_eligibility"] is False
    assert report["changes_readiness_credit"] is False
    assert report["outcome_labels_read"] is False


def test_decision_timing_integrity_reports_exact_capture_headroom():
    report = decision_timing_integrity(({
        "id": 1,
        "asset": "BTC",
        "close_time": 2_000.0,
        "source_captured_at": 1_220.25,
        "evidence_as_of": 1_220.40,
    },))
    assert report["status"] == "OK"
    assert report["exact_capture_offset_seconds"] == {
        "minimum": 0.25, "maximum": 0.25,
    }
    assert report["evidence_assembly_lag_seconds"]["maximum"] == pytest.approx(
        0.15
    )
    assert report["violations"] == []
    assert report["outcome_labels_read"] is False


def test_complete_fold_audit_is_preview_only_and_ignores_outcomes():
    assets = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
    rows = []
    row_id = 0
    for close_time in (2_000.0, 2_900.0):
        for asset in assets:
            row_id += 1
            rows.append(_row(asset, close_time, row_id))
    baseline = build_feature_audit(rows, _design())
    poisoned_rows = []
    for row in rows:
        profile = json.loads(row["threshold_json"])
        profile.update({
            "resolved_accuracy": 0.999,
            "resolved_correct": 999,
            "resolved_net_pnl_cents_per_contract": 9999.0,
            "official_result": "YES",
            "hypothetical_pnl_cents": 9999.0,
        })
        poisoned_rows.append({
            **row,
            "threshold_json": json.dumps(profile),
            "official_result": "YES",
            "correct": 1,
            "hypothetical_pnl_cents": 999.0,
        })
    poisoned = build_feature_audit(poisoned_rows, _design())

    assert baseline["status"] == "PREVIEW_ONLY_BEFORE_30_WINDOWS"
    assert baseline["complete_executable_close_windows"] == 2
    assert baseline["windows_remaining_to_first_review"] == 28
    assert baseline["outcome_labels_read"] is False
    assert baseline["model_fit_performed"] is False
    assert baseline["artifact_emitted"] is False
    assert baseline["feature_profile_allow_list_enforced"] is True
    assert baseline["raw_threshold_json_selected_by_cli_loader"] is False
    assert baseline["soft_input_integrity"]["status"] == (
        "ALL_RETAINED_INPUTS_OBSERVED"
    )
    assert baseline["decision_timing_integrity"]["status"] == "OK"
    assert baseline["cohorts"]["BTC"]["rows"] == 2
    assert baseline["cohorts"]["NON_BTC_TRANSFER"]["rows"] == 12
    assert baseline["cohorts"]["BTC"]["matrix_geometry"]["rows"] == 2
    assert baseline["cohorts"]["BTC"]["preregistered_fit_capacity"][
        "projected_train_windows"
    ] == 90
    assert baseline["cohorts"]["NON_BTC_TRANSFER"][
        "preregistered_fit_capacity"
    ]["projected_train_windows"] == 36
    assert baseline["cohorts"] == poisoned["cohorts"]
    assert set(baseline["cohorts"]["BTC"]["constant_features"]).issuperset({
        "asset_bnb", "asset_doge", "asset_eth", "asset_hype", "asset_sol",
        "asset_xrp",
    })


def test_feature_width_mismatch_fails_closed():
    with pytest.raises(ValueError, match="feature_matrix_width_mismatch"):
        feature_statistics(("a", "b"), ((1.0,),), minimum_std=1e-8)


def test_v2_outcome_blind_audit_removes_the_exact_duplicate_pairs():
    design = json.loads(DEFAULT_DESIGN_V2.read_text(encoding="utf-8"))
    assets = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
    rows = []
    row_id = 0
    for close_time in (2_000.0, 2_900.0, 3_800.0):
        for asset in assets:
            row_id += 1
            rows.append(_row(asset, close_time, row_id))
    report = build_feature_audit(rows, design)
    for cohort in report["cohorts"].values():
        pairs = cohort["correlation_diagnostics"]["exact_signed_duplicate_pairs"]
        assert not any(
            {pair["left"], pair["right"]} == {
                "kalshi_trade_imbalance_yes_5s",
                "kalshi_taker_imbalance_yes_5s",
            }
            for pair in pairs
        )
        assert not any(
            {pair["left"], pair["right"]} == {
                "kalshi_trade_imbalance_yes_30s",
                "kalshi_taker_imbalance_yes_30s",
            }
            for pair in pairs
        )
    assert report["outcome_labels_read"] is False


def test_v3_outcome_blind_audit_uses_the_final_de_duplicated_manifest():
    design = json.loads(DEFAULT_DESIGN_V3.read_text(encoding="utf-8"))
    assets = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
    rows = []
    row_id = 0
    for close_time in (2_000.0, 2_900.0, 3_800.0):
        for asset in assets:
            row_id += 1
            rows.append(_row(asset, close_time, row_id))
    report = build_feature_audit(rows, design)
    assert report["design_id"] == "q15-rti-market-residual-microstructure-v3"
    assert report["feature_count"] == 33
    assert report["outcome_labels_read"] is False
    assert report["model_fit_performed"] is False
    assert report["artifact_emitted"] is False


@pytest.mark.parametrize("version", (4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14))
def test_later_frozen_designs_have_outcome_blind_feature_audit_runtime(version):
    design = json.loads(Path(
        f"config/q15_rti_microstructure_design_v{version}.json"
    ).read_text(encoding="utf-8"))
    report = build_feature_audit([], design)
    assert report["design_id"] == design["design_id"]
    assert report["feature_count"] == len(design["feature_names"])
    assert report["outcome_labels_read"] is False
    assert report["model_fit_performed"] is False
    assert report["artifact_emitted"] is False


def _v12_geometry_report(windows: int, *, alias_correlation: float | None):
    alias_pairs = [] if alias_correlation is None else [{
        "left": "target_minus_cross_asset_median_momentum_60s_bps",
        "right": "cross_asset_btc_minus_non_btc_median_60s",
        "correlation": alias_correlation,
    }]
    return {
        "complete_executable_close_windows": windows,
        "timestamp_alignment_failures": 0,
        "cohorts": {
            "BTC": {
                "rows": windows,
                "independent_close_windows": windows,
                "nonfinite_counts": {},
                "correlation_diagnostics": {
                    "high_absolute_correlation_pairs": alias_pairs,
                    "exact_signed_duplicate_pairs": [],
                },
                "matrix_geometry": {
                    "condition_number_nonzero_subspace": 20.0,
                    "numerical_rank": 19,
                    "centered_rank_limit": 19,
                },
                "preregistered_fit_capacity": {
                    "projected_train_rows_per_currently_active_feature": 4.5,
                },
            },
            "NON_BTC_TRANSFER": {
                "rows": windows * 6,
                "independent_close_windows": windows,
                "nonfinite_counts": {},
                "correlation_diagnostics": {
                    "high_absolute_correlation_pairs": [],
                    "exact_signed_duplicate_pairs": [],
                },
                "matrix_geometry": {
                    "condition_number_nonzero_subspace": 12.0,
                    "rank_deficiency_vs_active_features": 0,
                },
                "preregistered_fit_capacity": {
                    "projected_train_rows_per_currently_active_feature": 11.0,
                },
            },
        },
    }


def test_v12_geometry_review_protocol_is_frozen_before_30_windows():
    design = json.loads(DEFAULT_DESIGN_V12.read_text(encoding="utf-8"))
    protocol = v12_geometry_review_protocol()
    validate_v12_geometry_review_protocol(protocol, design)
    assert geometry_review_protocol_fingerprint(protocol) == (
        EXPECTED_V12_GEOMETRY_REVIEW_PROTOCOL_SHA256
    )
    assert protocol["evidence_available_at_preregistration"][
        "complete_executable_close_windows"
    ] == 15
    assert protocol["evidence_available_at_preregistration"][
        "outcome_labels_read"
    ] is False

    tampered = json.loads(json.dumps(protocol))
    tampered["fixed_checks"][
        "pairwise_absolute_correlation_ceiling"
    ] = 0.99
    with pytest.raises(
        ValueError, match="v12_geometry_review_protocol_fingerprint_mismatch",
    ):
        validate_v12_geometry_review_protocol(tampered, design)


def test_v12_geometry_review_waits_then_triggers_only_predeclared_successor():
    design = json.loads(DEFAULT_DESIGN_V12.read_text(encoding="utf-8"))
    protocol = v12_geometry_review_protocol()
    waiting = v12_geometry_review(
        _v12_geometry_report(15, alias_correlation=0.989), design, protocol,
    )
    assert waiting["status"] == "WAITING_FOR_30_COMPLETE_WINDOWS"
    assert waiting["successor_hypothesis_triggered"] is False

    triggered = v12_geometry_review(
        _v12_geometry_report(30, alias_correlation=0.97), design, protocol,
    )
    assert triggered["status"] == (
        "SUCCESSOR_HYPOTHESIS_TRIGGERED_MANUAL_PREREGISTRATION_REQUIRED"
    )
    assert triggered["successor_hypothesis_triggered"] is True
    assert triggered["historical_credit_allowed"] is False
    assert triggered["automatic_design_change_allowed"] is False
    assert triggered["notification_eligible"] is False
    assert triggered["real_trading_allowed"] is False

    passed = v12_geometry_review(
        _v12_geometry_report(30, alias_correlation=None), design, protocol,
    )
    assert passed["status"] == "GEOMETRY_REVIEW_PASSED_NO_SUCCESSOR"
    assert passed["successor_hypothesis_triggered"] is False


def _v12_drift_examples(*, windows: int = 30, shifted: bool = False):
    assets = ("BTC", "BNB", "DOGE", "ETH", "HYPE", "SOL", "XRP")
    rows = []
    for window in range(windows):
        close_time = 10_000.0 + 900.0 * window
        for asset_index, asset in enumerate(assets):
            cycle = -1.0 if window % 2 == 0 else 1.0
            shift = 10.0 if shifted and window >= windows // 2 else 0.0
            missing = 1.0 if shifted and window >= windows // 2 else 0.0
            rows.append({
                "asset": asset,
                "close_time": close_time,
                "features": [cycle + asset_index / 100.0 + shift, missing],
                "market_yes_probability": 0.5 + 0.01 * cycle,
            })
    return rows


def test_v12_covariate_drift_protocol_is_frozen_before_split_statistics():
    design = json.loads(DEFAULT_DESIGN_V12.read_text(encoding="utf-8"))
    protocol = v12_covariate_drift_protocol()
    validate_v12_covariate_drift_protocol(protocol, design)
    assert geometry_review_protocol_fingerprint(protocol) == (
        EXPECTED_V12_COVARIATE_DRIFT_PROTOCOL_SHA256
    )
    evidence = protocol["evidence_available_at_preregistration"]
    assert evidence["aggregate_feature_audit_complete_windows"] == 15
    assert evidence["chronological_split_statistics_inspected"] is False
    assert evidence["outcome_labels_read"] is False

    tampered = json.loads(json.dumps(protocol))
    tampered["fixed_metrics"][
        "absolute_standardized_mean_shift_maximum"
    ] = 2.0
    with pytest.raises(
        ValueError, match="v12_covariate_drift_protocol_fingerprint_mismatch",
    ):
        validate_v12_covariate_drift_protocol(tampered, design)


def test_v12_covariate_drift_keeps_same_close_together_and_detects_shift():
    design = json.loads(DEFAULT_DESIGN_V12.read_text(encoding="utf-8"))
    protocol = v12_covariate_drift_protocol()
    stable = v12_covariate_drift_review(
        _v12_drift_examples(),
        ("signal", "spot_flow_missing"),
        design,
        protocol,
        timestamp_alignment_failures=0,
    )
    assert stable["status"] == "COVARIATE_STABILITY_REVIEW_PASSED"
    assert stable["same_close_assets_share_half"] is True
    assert stable["early_last_close_time"] < stable["late_first_close_time"]
    assert stable["cohorts"]["BTC"]["early_rows"] == 15
    assert stable["cohorts"]["NON_BTC_TRANSFER"]["early_rows"] == 90
    assert stable["drift_detected"] is False

    drifted = v12_covariate_drift_review(
        _v12_drift_examples(shifted=True),
        ("signal", "spot_flow_missing"),
        design,
        protocol,
        timestamp_alignment_failures=0,
    )
    assert drifted["status"] == (
        "COVARIATE_DRIFT_DETECTED_MANUAL_REVIEW_REQUIRED"
    )
    assert drifted["drift_detected"] is True
    assert "BTC:feature:signal" in drifted["observed_breaches"]
    assert "BTC:feature:spot_flow_missing" in drifted["observed_breaches"]
    missing = drifted["cohorts"]["BTC"]["feature_metrics"][
        "spot_flow_missing"
    ]
    assert missing["missing_rate_shift"] == 1.0
    assert missing["missing_rate_breach"] is True
    assert drifted["automatic_feature_removal_allowed"] is False
    assert drifted["automatic_threshold_change_allowed"] is False


def test_v12_covariate_drift_fails_closed_on_partial_close_or_timestamp_error():
    design = json.loads(DEFAULT_DESIGN_V12.read_text(encoding="utf-8"))
    protocol = v12_covariate_drift_protocol()
    partial = _v12_drift_examples()
    partial.pop()
    report = v12_covariate_drift_review(
        partial,
        ("signal", "spot_flow_missing"),
        design,
        protocol,
        timestamp_alignment_failures=0,
    )
    assert report["status"] == "FAIL_CLOSED_INTEGRITY"
    assert report["integrity_met"] is False
    assert report["drift_detected"] is False

    timestamp_failure = v12_covariate_drift_review(
        _v12_drift_examples(),
        ("signal", "spot_flow_missing"),
        design,
        protocol,
        timestamp_alignment_failures=1,
    )
    assert timestamp_failure["status"] == "FAIL_CLOSED_INTEGRITY"
    assert timestamp_failure["integrity_met"] is False


def _v13_geometry_report(windows: int, *, conditioned_max_abs: float = 0.0):
    conditioned = (
        "cross_asset_btc_minus_non_btc_median_non_btc_only_60s"
    )
    return {
        "complete_executable_close_windows": windows,
        "timestamp_alignment_failures": 0,
        "cohorts": {
            "BTC": {
                "rows": windows,
                "independent_close_windows": windows,
                "nonfinite_counts": {},
                "feature_statistics": {
                    conditioned: {
                        "constant": conditioned_max_abs == 0.0,
                        "max_abs": conditioned_max_abs,
                    },
                },
                "correlation_diagnostics": {
                    "high_absolute_correlation_pairs": [],
                    "exact_signed_duplicate_pairs": [],
                },
                "matrix_geometry": {
                    "active_feature_count": 18,
                    "rank_deficiency_vs_active_features": 0,
                    "condition_number_nonzero_subspace": 20.0,
                },
                "preregistered_fit_capacity": {
                    "projected_train_rows_per_currently_active_feature": 5.0,
                },
            },
            "NON_BTC_TRANSFER": {
                "rows": windows * 6,
                "independent_close_windows": windows,
                "nonfinite_counts": {},
                "correlation_diagnostics": {
                    "high_absolute_correlation_pairs": [],
                    "exact_signed_duplicate_pairs": [],
                },
                "matrix_geometry": {
                    "active_feature_count": 19,
                    "rank_deficiency_vs_active_features": 0,
                    "condition_number_nonzero_subspace": 12.0,
                },
                "preregistered_fit_capacity": {
                    "projected_train_rows_per_currently_active_feature": 11.0,
                },
            },
        },
    }


def test_v13_geometry_protocol_is_frozen_before_statistics_and_fails_closed():
    design = json.loads(DEFAULT_DESIGN_V13.read_text(encoding="utf-8"))
    protocol = v13_geometry_review_protocol()
    validate_v13_geometry_review_protocol(protocol, design)
    assert geometry_review_protocol_fingerprint(protocol) == (
        EXPECTED_V13_GEOMETRY_REVIEW_PROTOCOL_SHA256
    )
    evidence = protocol["evidence_available_at_preregistration"]
    assert evidence["complete_executable_close_windows"] == 1
    assert evidence["feature_statistics_inspected"] is False
    assert evidence["correlation_statistics_inspected"] is False
    assert evidence["outcome_labels_read"] is False

    waiting = v13_geometry_review(
        _v13_geometry_report(1), design, protocol,
    )
    assert waiting["status"] == "WAITING_FOR_30_COMPLETE_WINDOWS"
    assert waiting["review_ready"] is False

    passed = v13_geometry_review(
        _v13_geometry_report(30), design, protocol,
    )
    assert passed["status"] == "V13_GEOMETRY_REVIEW_PASSED"
    assert passed["all_checks_met"] is True
    assert passed["outcome_labels_read"] is False
    assert passed["automatic_design_change_allowed"] is False

    failed = v13_geometry_review(
        _v13_geometry_report(30, conditioned_max_abs=0.01), design, protocol,
    )
    assert failed["status"] == (
        "V13_GEOMETRY_REVIEW_REQUIRES_MANUAL_DIAGNOSIS"
    )
    assert failed["all_checks_met"] is False
    assert failed["automatic_feature_removal_allowed"] is False

    tampered = json.loads(json.dumps(protocol))
    tampered["fixed_checks"]["pairwise_absolute_correlation_ceiling"] = 0.99
    with pytest.raises(
        ValueError, match="v13_geometry_review_protocol_fingerprint_mismatch",
    ):
        validate_v13_geometry_review_protocol(tampered, design)


def test_v13_60_window_drift_protocol_waits_then_detects_without_actions():
    design = json.loads(DEFAULT_DESIGN_V13.read_text(encoding="utf-8"))
    protocol = v13_covariate_drift_protocol()
    validate_v13_covariate_drift_protocol(protocol, design)
    assert geometry_review_protocol_fingerprint(protocol) == (
        EXPECTED_V13_COVARIATE_DRIFT_PROTOCOL_SHA256
    )
    evidence = protocol["evidence_available_at_preregistration"]
    assert evidence["complete_executable_close_windows"] == 1
    assert evidence["chronological_split_statistics_inspected"] is False
    assert evidence["feature_statistics_inspected"] is False
    assert evidence["outcome_labels_read"] is False

    waiting = v13_covariate_drift_review(
        _v12_drift_examples(windows=30),
        ("signal", "spot_flow_missing"),
        design,
        protocol,
        timestamp_alignment_failures=0,
    )
    assert waiting["status"] == "WAITING_FOR_60_COMPLETE_WINDOWS"
    assert waiting["review_ready"] is False

    drifted = v13_covariate_drift_review(
        _v12_drift_examples(windows=60, shifted=True),
        ("signal", "spot_flow_missing"),
        design,
        protocol,
        timestamp_alignment_failures=0,
    )
    assert drifted["status"] == (
        "COVARIATE_DRIFT_DETECTED_MANUAL_REVIEW_REQUIRED"
    )
    assert drifted["cohorts"]["BTC"]["early_rows"] == 30
    assert drifted["cohorts"]["NON_BTC_TRANSFER"]["early_rows"] == 180
    assert drifted["automatic_threshold_change_allowed"] is False
    assert drifted["automatic_refit_allowed"] is False
    assert drifted["notification_eligible"] is False

    tampered = json.loads(json.dumps(protocol))
    tampered["fixed_metrics"][
        "absolute_standardized_mean_shift_maximum"
    ] = 2.0
    with pytest.raises(
        ValueError,
        match="v13_covariate_drift_protocol_fingerprint_mismatch",
    ):
        validate_v13_covariate_drift_protocol(tampered, design)
