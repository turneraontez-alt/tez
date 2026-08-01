from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import pytest

from tools.q15_rti_microstructure_preregister import (
    DEFAULT_DESIGN,
    DEFAULT_DESIGN_V2,
    DEFAULT_DESIGN_V3,
    DEFAULT_DESIGN_V4,
    DEFAULT_DESIGN_V5,
    DEFAULT_DESIGN_V6,
    DEFAULT_DESIGN_V7,
    DEFAULT_DESIGN_V8,
    DEFAULT_DESIGN_V9,
    DEFAULT_DESIGN_V10,
    DEFAULT_DESIGN_V11,
    DEFAULT_DESIGN_V12,
    EXPECTED_DESIGN_SHA256,
    EXPECTED_DESIGN_SHA256_V2,
    EXPECTED_DESIGN_SHA256_V3,
    EXPECTED_DESIGN_SHA256_V4,
    EXPECTED_DESIGN_SHA256_V5,
    EXPECTED_DESIGN_SHA256_V6,
    EXPECTED_DESIGN_SHA256_V7,
    EXPECTED_DESIGN_SHA256_V8,
    EXPECTED_DESIGN_SHA256_V9,
    EXPECTED_DESIGN_SHA256_V10,
    EXPECTED_DESIGN_SHA256_V11,
    EXPECTED_DESIGN_SHA256_V12,
    build_readiness,
    design_fingerprint,
    validate_design,
)
from tools.q15_rti_feature_coverage_audit import _load_rows


def _design():
    return json.loads(DEFAULT_DESIGN.read_text(encoding="utf-8"))


def _coverage(
    windows: int,
    *,
    timestamp_failures: int = 0,
    partial_windows: int = 0,
    incomplete_windows: int = 0,
):
    return {
        "complete_microstructure_v1_close_windows": windows,
        "timestamp_alignment_failures": [object()] * timestamp_failures,
        "cross_asset_partial_schema_windows": [object()] * partial_windows,
        "incomplete_microstructure_v1_close_windows": [object()] * incomplete_windows,
    }


def test_preregistered_design_is_valid_and_cryptographically_pinned():
    design = _design()
    validate_design(design)
    assert design_fingerprint(design) == EXPECTED_DESIGN_SHA256
    assert design_fingerprint(json.loads(json.dumps(design))) == EXPECTED_DESIGN_SHA256

    v2 = json.loads(DEFAULT_DESIGN_V2.read_text(encoding="utf-8"))
    validate_design(v2)
    assert design_fingerprint(v2) == EXPECTED_DESIGN_SHA256_V2
    assert v2["outcome_labels_used_for_change"] is False

    v3 = json.loads(DEFAULT_DESIGN_V3.read_text(encoding="utf-8"))
    validate_design(v3)
    assert design_fingerprint(v3) == EXPECTED_DESIGN_SHA256_V3
    assert v3["outcome_labels_used_for_change"] is False

    v4 = json.loads(DEFAULT_DESIGN_V4.read_text(encoding="utf-8"))
    validate_design(v4)
    assert design_fingerprint(v4) == EXPECTED_DESIGN_SHA256_V4
    assert v4["source_schema"] == "rti-exact-microstructure-v2"
    assert v4["prior_schema_rows_credited"] is False
    assert v4["outcome_labels_used_for_change"] is False

    v5 = json.loads(DEFAULT_DESIGN_V5.read_text(encoding="utf-8"))
    validate_design(v5)
    assert design_fingerprint(v5) == EXPECTED_DESIGN_SHA256_V5
    assert v5["source_extension_schema"] == (
        "rti-exact-microstructure-extension-v1"
    )
    assert v5["pre_freeze_extension_rows_credited"] is False
    assert v5["outcome_labels_used_for_change"] is False

    v6 = json.loads(DEFAULT_DESIGN_V6.read_text(encoding="utf-8"))
    validate_design(v6)
    assert design_fingerprint(v6) == EXPECTED_DESIGN_SHA256_V6
    assert v6["source_spot_path_schema"] == "spot-mid-path-local-v1"
    assert v6["pre_freeze_spot_path_rows_credited"] is False
    assert v6["outcome_labels_used_for_change"] is False

    v7 = json.loads(DEFAULT_DESIGN_V7.read_text(encoding="utf-8"))
    validate_design(v7)
    assert design_fingerprint(v7) == EXPECTED_DESIGN_SHA256_V7
    assert v7["source_cross_venue_schema"] == (
        "rti-cross-venue-consensus-v1"
    )
    assert v7["pre_freeze_cross_venue_rows_credited"] is False
    assert v7["outcome_labels_used_for_change"] is False

    v8 = json.loads(DEFAULT_DESIGN_V8.read_text(encoding="utf-8"))
    validate_design(v8)
    assert design_fingerprint(v8) == EXPECTED_DESIGN_SHA256_V8
    assert v8["source_independent_venue_schema"] == (
        "rti-independent-venue-consensus-v1"
    )
    assert v8["pre_freeze_independent_venue_rows_credited"] is False
    assert v8["outcome_labels_used_for_change"] is False

    v9 = json.loads(DEFAULT_DESIGN_V9.read_text(encoding="utf-8"))
    validate_design(v9)
    assert design_fingerprint(v9) == EXPECTED_DESIGN_SHA256_V9
    assert v9["source_independent_microstructure_schema"] == (
        "rti-independent-venue-microstructure-v2"
    )
    assert v9["pre_freeze_independent_microstructure_rows_credited"] is False
    assert v9["ambiguous_kraken_deletes_as_trades_forbidden"] is True
    assert v9["outcome_labels_used_for_change"] is False

    v10 = json.loads(DEFAULT_DESIGN_V10.read_text(encoding="utf-8"))
    validate_design(v10)
    assert design_fingerprint(v10) == EXPECTED_DESIGN_SHA256_V10
    assert v10["source_feature_review_complete_windows"] == 30
    assert set(v10["removed_exact_duplicate_features"]) == {
        "kalshi_queue_pressure_yes_5s",
        "kalshi_queue_pressure_yes_30s",
    }
    assert v10["information_loss_from_removal"] is False
    assert v10["outcome_labels_used_for_change"] is False

    v11 = json.loads(DEFAULT_DESIGN_V11.read_text(encoding="utf-8"))
    validate_design(v11)
    assert design_fingerprint(v11) == EXPECTED_DESIGN_SHA256_V11
    assert v11["source_cross_asset_schema"] == "rti-cross-asset-regime-v1"
    assert v11["pre_freeze_cross_asset_rows_credited"] is False
    assert v11["hypothesis_selected_from_outcomes"] is False
    assert v11["outcome_labels_used_for_change"] is False

    v12 = json.loads(DEFAULT_DESIGN_V12.read_text(encoding="utf-8"))
    validate_design(v12)
    assert design_fingerprint(v12) == EXPECTED_DESIGN_SHA256_V12
    assert v12["source_feature_review_design_id"] == v11["design_id"]
    assert v12["source_feature_review_complete_windows"] == 38
    assert v12["projected_feature_count"] == 20
    assert v12["pre_v12_inspected_rows_credited"] is False
    assert v12["performance_metrics_inspected_for_change"] is False
    assert v12["outcome_labels_used_for_change"] is False


def test_v4_source_integrity_guards_are_pinned_and_fail_closed():
    design = json.loads(DEFAULT_DESIGN_V4.read_text(encoding="utf-8"))
    for path, value in (
        (("post_fix_history_only",), False),
        (("prior_schema_rows_credited",), True),
        (("source_time_basis",), "exchange_ts"),
        (("required_source_integrity", "count_cap_must_be_disabled"), False),
        (
            (
                "required_source_integrity",
                "continuity_resets_on_snapshot_or_reconnect",
            ),
            False,
        ),
    ):
        changed = deepcopy(design)
        target = changed
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with pytest.raises(ValueError):
            validate_design(changed)


def test_v5_boundary_and_extension_guards_are_pinned_and_fail_closed():
    design = json.loads(DEFAULT_DESIGN_V5.read_text(encoding="utf-8"))
    for path, value in (
        (("source_extension_schema",), "wrong"),
        (("pre_freeze_extension_rows_credited",), True),
        (("source_feature_audit_only_before_freeze",), False),
        (("prospective_after_close_time",), 0.0),
        (("first_eligible_close_time",), 0.0),
        (
            (
                "required_source_integrity",
                "required_complete_extension_horizons_seconds",
            ),
            [5, 30],
        ),
    ):
        changed = deepcopy(design)
        target = changed
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with pytest.raises(ValueError):
            validate_design(changed)


def test_v6_spot_path_and_boundary_guards_are_pinned_and_fail_closed():
    design = json.loads(DEFAULT_DESIGN_V6.read_text(encoding="utf-8"))
    for path, value in (
        (("source_spot_path_schema",), "wrong"),
        (("source_lead_lag_schema",), "wrong"),
        (("source_spot_time_basis",), "exchange_ts"),
        (("pre_freeze_spot_path_rows_credited",), True),
        (("prospective_after_close_time",), 0.0),
        (("first_eligible_close_time",), 0.0),
        (
            (
                "required_source_integrity",
                "required_complete_spot_mid_horizons_seconds",
            ),
            [60],
        ),
        (("required_source_integrity", "spot_mid_retention_minimum_seconds"), 30.0),
    ):
        changed = deepcopy(design)
        target = changed
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with pytest.raises(ValueError):
            validate_design(changed)


def test_v7_cross_venue_and_boundary_guards_are_pinned_and_fail_closed():
    design = json.loads(DEFAULT_DESIGN_V7.read_text(encoding="utf-8"))
    for path, value in (
        (("source_cross_venue_schema",), "wrong"),
        (("source_cross_venue_time_basis",), "exchange_ts"),
        (("pre_freeze_cross_venue_rows_credited",), True),
        (("prospective_after_close_time",), 0.0),
        (("first_eligible_close_time",), 0.0),
        (("required_source_integrity", "cross_venue_required_count"), 1),
        (("required_source_integrity", "cross_venue_max_lag_seconds"), 30.0),
        (
            (
                "required_source_integrity",
                "required_complete_cross_venue_horizons_seconds",
            ),
            [60],
        ),
        (
            (
                "required_source_integrity",
                "cross_venue_future_snapshots_forbidden",
            ),
            False,
        ),
    ):
        changed = deepcopy(design)
        target = changed
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with pytest.raises(ValueError):
            validate_design(changed)


def test_v9_microstructure_flow_and_boundary_guards_fail_closed():
    design = json.loads(DEFAULT_DESIGN_V9.read_text(encoding="utf-8"))
    for path, value in (
        (("source_independent_microstructure_schema",), "wrong"),
        (("source_independent_microstructure_time_basis",), "exchange_ts"),
        (("source_kraken_partial_fill_flow_schema",), "ambiguous-v0"),
        (("pre_freeze_independent_microstructure_rows_credited",), True),
        (("ambiguous_kraken_deletes_as_trades_forbidden",), False),
        (("prospective_after_close_time",), 0.0),
        (("first_eligible_close_time",), 0.0),
        (
            (
                "required_source_integrity",
                "independent_microstructure_current_and_60s_start_required",
            ),
            False,
        ),
        (
            (
                "required_source_integrity",
                "ambiguous_kraken_delete_flow_forbidden",
            ),
            False,
        ),
        (
            (
                "required_source_integrity",
                "independent_microstructure_depth_summary_levels",
            ),
            250,
        ),
    ):
        changed = deepcopy(design)
        target = changed
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with pytest.raises(ValueError):
            validate_design(changed)


def test_v11_cross_asset_lineage_and_timestamp_guards_fail_closed():
    design = json.loads(DEFAULT_DESIGN_V11.read_text(encoding="utf-8"))
    for path, value in (
        (("source_cross_asset_schema",), "wrong"),
        (("source_cross_asset_time_basis",), "exchange_ts"),
        (("pre_freeze_cross_asset_rows_credited",), True),
        (("hypothesis_selected_from_outcomes",), True),
        (("prospective_after_close_time",), 0.0),
        (("first_eligible_close_time",), 0.0),
        (("required_source_integrity", "cross_asset_required_assets"), ["BTC"]),
        (("required_source_integrity", "cross_asset_required_venues"), ["coinbase"]),
        (("required_source_integrity", "cross_asset_horizons_seconds"), [60]),
        (("required_source_integrity", "cross_asset_raw_moves_persisted"), False),
        (("required_source_integrity", "cross_asset_derived_values_recomputed_before_use"), False),
        (("required_source_integrity", "cross_asset_future_snapshots_forbidden"), False),
    ):
        changed = deepcopy(design)
        target = changed
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with pytest.raises(ValueError):
            validate_design(changed)


def test_v12_outcome_blind_projection_guards_fail_closed():
    design = json.loads(DEFAULT_DESIGN_V12.read_text(encoding="utf-8"))
    for path, value in (
        (("source_feature_review_design_id",), "wrong"),
        (("source_feature_review_complete_windows",), 37),
        (("source_feature_review_outcome_labels_read",), True),
        (("performance_metrics_inspected_for_change",), True),
        (("pre_v12_inspected_rows_credited",), True),
        (("v11_remains_frozen_parallel_control",), False),
        (("source_feature_count",), 70),
        (("projected_feature_count",), 21),
        (("projection_policy", "automatic_feature_selection"), True),
        (("projection_policy", "outcome_based_feature_selection"), True),
        (
            (
                "projection_policy",
                "target_relative_momentum_orthogonalized_against_broad_market",
            ),
            False,
        ),
    ):
        changed = deepcopy(design)
        target = changed
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with pytest.raises(ValueError):
            validate_design(changed)


def test_readiness_waits_at_21_windows_without_reading_labels_or_fitting():
    report = build_readiness(_design(), _coverage(21))
    assert report["status"] == "WAITING_FOR_COMPLETE_WINDOWS"
    assert report["outcome_labels_read"] is False
    assert report["model_fit_performed"] is False
    assert report["artifact_emitted"] is False
    assert report["ready_for_any_locked_freeze"] is False
    assert report["cohorts"]["NON_BTC_TRANSFER"]["windows_remaining"] == 39
    assert report["cohorts"]["BTC"]["windows_remaining"] == 129


def test_readiness_unlocks_cohorts_only_at_their_preregistered_window_counts():
    transfer = build_readiness(_design(), _coverage(60))
    assert transfer["cohorts"]["NON_BTC_TRANSFER"]["ready_for_locked_freeze"] is True
    assert transfer["cohorts"]["BTC"]["ready_for_locked_freeze"] is False
    assert transfer["ready_for_any_locked_freeze"] is True

    both = build_readiness(_design(), _coverage(150))
    assert both["cohorts"]["NON_BTC_TRANSFER"]["ready_for_locked_freeze"] is True
    assert both["cohorts"]["BTC"]["ready_for_locked_freeze"] is True


def test_readiness_counts_only_complete_executable_feature_windows():
    coverage = _coverage(60)
    coverage["complete_model_feature_close_windows"] = 58
    coverage["model_feature_unavailable_rows"] = [{}, {}]
    coverage["unusable_model_feature_close_windows"] = [{}, {}]
    report = build_readiness(_design(), coverage)
    assert report["schema_complete_microstructure_close_windows"] == 60
    assert report["complete_microstructure_close_windows"] == 58
    assert report["model_feature_unavailable_rows"] == 2
    assert report["unusable_model_feature_close_windows"] == 2
    assert report["cohorts"]["NON_BTC_TRANSFER"]["windows_remaining"] == 2
    assert report["ready_for_any_locked_freeze"] is False


@pytest.mark.parametrize(
    "dirty",
    (
        {"timestamp_failures": 1},
        {"partial_windows": 1},
        {"incomplete_windows": 1},
    ),
)
def test_readiness_fails_closed_on_timestamp_or_same_window_corruption(dirty):
    report = build_readiness(_design(), _coverage(150, **dirty))
    assert report["coverage_clean"] is False
    assert report["ready_for_any_locked_freeze"] is False
    assert all(
        status["ready_for_locked_freeze"] is False
        for status in report["cohorts"].values()
    )


def test_readiness_cannot_hide_model_timestamp_failure_behind_clean_count():
    coverage = _coverage(150)
    coverage["complete_model_feature_close_windows"] = 150
    coverage["model_feature_timestamp_failures"] = [{
        "error": "timestamp_alignment_failure",
    }]
    report = build_readiness(_design(), coverage)
    assert report["coverage_clean"] is False
    assert report["model_feature_timestamp_failures"] == 1
    assert report["ready_for_any_locked_freeze"] is False
    assert all(
        status["ready_for_locked_freeze"] is False
        for status in report["cohorts"].values()
    )


def test_model_scoped_readiness_reports_but_ignores_preboundary_source_gaps():
    coverage = _coverage(150, partial_windows=2, incomplete_windows=3)
    coverage["complete_model_feature_close_windows"] = 60
    coverage["model_feature_timestamp_failures"] = []
    report = build_readiness(_design(), coverage)
    assert report["partial_schema_windows"] == 2
    assert report["incomplete_seven_asset_windows"] == 3
    assert report["readiness_integrity_scope"] == (
        "design_eligible_model_windows"
    )
    assert report["coverage_clean"] is True
    assert report["cohorts"]["NON_BTC_TRANSFER"][
        "ready_for_locked_freeze"
    ] is True


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("automatic_promotion",), True),
        (("automatic_refit",), True),
        (("notification_eligible",), True),
        (("cohorts", "BTC", "minimum_complete_close_windows"), 20),
        (("fixed_training_config", "model_l2"), 0.0),
        (("entry_policy", "slippage_cents_per_contract"), 0.0),
    ),
)
def test_security_or_economic_design_changes_are_rejected(path, value):
    design = deepcopy(_design())
    target = design
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        validate_design(design)


def test_any_unregistered_feature_change_breaks_the_design_pin():
    design = deepcopy(_design())
    design["feature_names"].append("future_outcome_proxy")
    with pytest.raises(ValueError, match="design_fingerprint_mismatch"):
        validate_design(design)


def test_feature_loader_operates_on_a_database_with_no_outcome_columns(tmp_path: Path):
    database = tmp_path / "features_only.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """
            CREATE TABLE strategy_bot_decisions (
                id INTEGER PRIMARY KEY,
                bot_name TEXT,
                interval TEXT,
                record_kind TEXT,
                ticker TEXT,
                asset TEXT,
                close_time REAL,
                source_captured_at REAL,
                evidence_as_of REAL,
                threshold_json TEXT,
                kalshi_microstructure_schema_version TEXT,
                kalshi_microstructure_captured_at REAL,
                kalshi_event_count_5s REAL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO strategy_bot_decisions VALUES (
                1, 'rti_path_13m', '13M',
                'RTI_PATH_13M_PROSPECTIVE_EXACT', 'KXBTC-TEST', 'BTC',
                2000.0, 1220.1, 1220.2, '{}',
                'rti-exact-microstructure-v1', 1220.1, 0.0
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    rows = _load_rows(database)
    assert len(rows) == 1
    assert rows[0]["kalshi_event_count_5s"] == 0.0
    assert "official_result" not in rows[0]
    assert "correct" not in rows[0]
    assert "hypothetical_pnl_cents" not in rows[0]
