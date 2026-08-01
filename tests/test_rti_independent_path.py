from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

import pytest

from q15_upgrade.strategy_bots import rti_independent_path as path_runtime
from q15_upgrade.strategy_bots.rti_independent_path import (
    KRAKEN_PARTIAL_FILL_FLOW_SCHEMA_VERSION,
    PERSISTED_KEYS,
    SCHEMA_VERSION,
    TIME_BASIS,
    capture_rti_independent_path,
    validate_persisted_independent_path,
)
from q15_upgrade.strategy_bots.rules import SPOT_DEPTH_KEYS
from q15_upgrade.strategy_bots.rti_independent_path_identity import (
    DESIGN_ID,
    DESIGN_SHA256,
    FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME,
)
from q15_upgrade.strategy_bots.rti_independent_path_geometry_identity import (
    PROTOCOL_ID as GEOMETRY_PROTOCOL_ID,
    PROTOCOL_SHA256 as GEOMETRY_PROTOCOL_SHA256,
)
from tools.q15_rti_microstructure_preregister import design_fingerprint
from tools.q15_rti_independent_path_reference import (
    REFERENCE_VERSION,
    verify_reference_formulas,
)
from tools.q15_rti_independent_path_audit import (
    CONTRACT_IDENTITY_VERSION,
    build_report,
    evaluate_geometry_review,
    feature_geometry_report,
    load_geometry_protocol,
    selected_feature_evidence_identity,
    source_quality_report,
    validate_exact_contract_identity,
    validate_geometry_protocol,
)


ROOT = Path(__file__).resolve().parents[1]


def _ticker(asset: str, close_time: float) -> str:
    stamp = datetime.fromtimestamp(
        close_time, ZoneInfo("America/New_York")
    ).strftime("%y%b%d%H%M").upper()
    return f"KX{asset}15M-{stamp}-{int(stamp[-2:])}"


def test_path_design_identity_and_boundaries_are_frozen():
    design = json.loads(
        (ROOT / "config" / "q15_rti_independent_path_design_v1.json").read_text()
    )
    assert design["design_id"] == DESIGN_ID
    assert design_fingerprint(design) == DESIGN_SHA256
    assert design["prospective_after_close_time"] == PROSPECTIVE_AFTER_CLOSE_TIME
    assert design["first_eligible_close_time"] == FIRST_ELIGIBLE_CLOSE_TIME
    assert FIRST_ELIGIBLE_CLOSE_TIME == PROSPECTIVE_AFTER_CLOSE_TIME + 900.0
    assert design["outcome_labels_read_before_freeze"] is False
    assert design["source_v14_outcome_labels_read"] is False
    assert design["benchmark_capture_receive_credit"] is False
    assert design["historical_credit_allowed"] is False
    assert design["notification_eligible"] is False
    assert design["real_trading_allowed"] is False


def test_path_geometry_review_protocol_is_frozen_before_milestone():
    protocol = load_geometry_protocol()
    assert protocol["protocol_id"] == GEOMETRY_PROTOCOL_ID
    assert design_fingerprint(protocol) == GEOMETRY_PROTOCOL_SHA256
    validate_geometry_protocol(protocol)
    assert protocol["evidence_available_at_preregistration"][
        "complete_reconstructable_close_windows"
    ] == 12
    assert protocol["evidence_available_at_preregistration"][
        "outcome_labels_read"
    ] is False
    assert protocol["pass_policy"][
        "model_fit_allowed_at_30_windows"
    ] is False
    tampered = json.loads(json.dumps(protocol))
    tampered["fixed_checks"]["pairwise_absolute_correlation_ceiling"] = 0.99
    with pytest.raises(
        ValueError, match="independent_path_geometry_protocol_sha256_mismatch"
    ):
        validate_geometry_protocol(tampered)


def _databases(tmp_path, *, gap: bool = False, message_age: float = 0.2):
    coinbase = tmp_path / "coinbase.sqlite3"
    kraken = tmp_path / "kraken.sqlite3"
    cb = sqlite3.connect(coinbase)
    cb.execute(
        "CREATE TABLE coinbase_adv_l2_snapshots ("
        "id INTEGER PRIMARY KEY, created_at REAL, product_id TEXT, "
        "last_message_age_seconds REAL, spread_bps REAL, "
        "depth_imbalance REAL, bid_notional_levels REAL, "
        "ask_notional_levels REAL, summary_level_limit REAL, "
        "update_count_5s REAL, remove_count_5s REAL, "
        "update_count_15s REAL, remove_count_15s REAL, "
        "update_count_60s REAL, remove_count_60s REAL)"
    )
    kr = sqlite3.connect(kraken)
    kr.execute(
        "CREATE TABLE kraken_l3_summaries ("
        "id INTEGER PRIMARY KEY, created_at REAL, symbol TEXT, "
        "last_message_age_seconds REAL, spread_bps REAL, "
        "depth_imbalance REAL, bid_notional_levels REAL, "
        "ask_notional_levels REAL, summary_level_limit REAL, "
        "add_count_5s REAL, update_count_5s REAL, delete_count_5s REAL, "
        "trade_count_5s REAL, cancel_to_add_5s REAL, "
        "add_count_15s REAL, update_count_15s REAL, delete_count_15s REAL, "
        "trade_count_15s REAL, cancel_to_add_15s REAL, "
        "add_count_60s REAL, update_count_60s REAL, delete_count_60s REAL, "
        "trade_count_60s REAL, cancel_to_add_60s REAL, "
        "matched_buy_notional_60s REAL, matched_sell_notional_60s REAL, "
        "partial_fill_flow_schema_version TEXT)"
    )
    timestamps = list(range(940, 1001, 5))
    if gap:
        timestamps = [value for value in timestamps if not 965 <= value <= 980]
    for timestamp in timestamps:
        cb_imbalance = -0.2 if timestamp < 970 else 0.4
        cb_spread = 3.0 if timestamp == 990 else 1.0
        cb.execute(
            "INSERT INTO coinbase_adv_l2_snapshots "
            "(created_at,product_id,last_message_age_seconds,spread_bps,"
            "depth_imbalance,bid_notional_levels,ask_notional_levels,"
            "summary_level_limit,update_count_5s,remove_count_5s,"
            "update_count_15s,remove_count_15s,update_count_60s,"
            "remove_count_60s) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                timestamp, "BTC-USD", message_age, cb_spread, cb_imbalance,
                1000.0, 900.0, 10.0, 10.0, 2.0, 30.0, 5.0, 100.0, 20.0,
            ),
        )
        kr_imbalance = -0.4 if timestamp < 970 else 0.2
        buy, sell = ((90.0, 10.0) if timestamp < 985 else (10.0, 90.0))
        kr.execute(
            "INSERT INTO kraken_l3_summaries "
            "(created_at,symbol,last_message_age_seconds,spread_bps,"
            "depth_imbalance,bid_notional_levels,ask_notional_levels,"
            "summary_level_limit,add_count_5s,update_count_5s,"
            "delete_count_5s,trade_count_5s,cancel_to_add_5s,"
            "add_count_15s,update_count_15s,delete_count_15s,"
            "trade_count_15s,cancel_to_add_15s,add_count_60s,"
            "update_count_60s,delete_count_60s,trade_count_60s,"
            "cancel_to_add_60s,matched_buy_notional_60s,"
            "matched_sell_notional_60s,partial_fill_flow_schema_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                timestamp, "BTC/USD", message_age, 2.0, kr_imbalance,
                800.0, 700.0, 10.0, 8.0, 3.0, 2.0, 1.0, 0.25,
                25.0, 8.0, 6.0, 3.0, 0.24, 90.0, 25.0, 20.0, 8.0,
                0.22, buy, sell, KRAKEN_PARTIAL_FILL_FLOW_SCHEMA_VERSION,
            ),
        )
    # Adversarial post-cutoff rows must never affect the evidence or features.
    cb.execute(
        "INSERT INTO coinbase_adv_l2_snapshots "
        "(created_at,product_id,last_message_age_seconds,spread_bps,"
        "depth_imbalance,bid_notional_levels,ask_notional_levels,"
        "summary_level_limit,update_count_5s,remove_count_5s,"
        "update_count_15s,remove_count_15s,update_count_60s,"
        "remove_count_60s) VALUES (1000.5,'BTC-USD',0,999,1,1,1,10,1,1,1,1,1,1)"
    )
    cb.commit()
    kr.commit()
    cb.close()
    kr.close()
    return coinbase, kraken


def test_undefined_cancel_add_ratio_with_zero_denominator_is_not_missing(tmp_path):
    coinbase, kraken = _databases(tmp_path)
    connection = sqlite3.connect(kraken)
    connection.execute(
        "UPDATE kraken_l3_summaries SET add_count_5s=0,delete_count_5s=0,"
        "cancel_to_add_5s=NULL WHERE created_at=995"
    )
    connection.commit()
    connection.close()
    row = capture_rti_independent_path(
        "BTC", captured_at=1000.0, coinbase_db=str(coinbase),
        kraken_db=str(kraken), max_gap_seconds=10.0,
    )
    assert row["rti_independent_path_status"] == "ok"
    evidence = json.loads(row["rti_independent_path_evidence_json"])
    point = next(
        item for item in evidence["venues"]["kraken"]
        if item["created_at"] == 995.0
    )
    assert point["cancel_to_add_5s"] is None
    assert validate_persisted_independent_path(row)["valid"] is True


def test_path_is_reconstructable_point_in_time_and_excludes_future(tmp_path):
    coinbase, kraken = _databases(tmp_path)
    row = capture_rti_independent_path(
        "BTC", captured_at=1000.0, coinbase_db=str(coinbase),
        kraken_db=str(kraken), max_gap_seconds=10.0,
    )

    assert row["rti_independent_path_schema_version"] == SCHEMA_VERSION
    assert row["rti_independent_path_time_basis"] == TIME_BASIS
    assert row["rti_independent_path_design_id"] == DESIGN_ID
    assert row["rti_independent_path_design_sha256"] == DESIGN_SHA256
    assert row["rti_independent_path_prospective_after_close_time"] == (
        PROSPECTIVE_AFTER_CLOSE_TIME
    )
    assert row["rti_independent_path_first_eligible_close_time"] == (
        FIRST_ELIGIBLE_CLOSE_TIME
    )
    assert row["rti_independent_path_status"] == "ok"
    assert row["rti_independent_path_available_count"] == 2
    assert row["rti_independent_path_coinbase_point_count"] == 13
    assert row["rti_independent_path_kraken_point_count"] == 13
    assert row["rti_independent_path_coinbase_max_spread_bps_60s"] == 3.0
    assert row[
        "rti_independent_path_mean_depth_imbalance_half_delta_60s"
    ] == pytest.approx(0.6)
    assert row[
        "rti_independent_path_depth_direction_agreement_60s"
    ] == 0.0
    assert row[
        "rti_independent_path_kraken_partial_fill_imbalance_acceleration_60s"
    ] == pytest.approx(1.6)
    assert row[
        "rti_independent_path_log1p_max_spread_stress_ratio_60s"
    ] > 0.0
    evidence = row["rti_independent_path_evidence_json"]
    assert hashlib.sha256(evidence.encode()).hexdigest() == row[
        "rti_independent_path_evidence_sha256"
    ]
    decoded = json.loads(evidence)
    assert decoded["captured_at"] == 1000.0
    assert max(
        point["created_at"]
        for venue in decoded["venues"].values()
        for point in venue
    ) <= 1000.0
    assert set(PERSISTED_KEYS).issubset(SPOT_DEPTH_KEYS)
    assert set(row).issubset(PERSISTED_KEYS)
    verified = validate_persisted_independent_path({
        **row,
        "asset": "BTC",
        "source_captured_at": 1000.0,
        "close_time": FIRST_ELIGIBLE_CLOSE_TIME,
    })
    assert verified["valid"] is True
    assert verified["errors"] == []
    assert verified["outcome_labels_read"] is False
    assert verified["prospective_credit_eligible"] is True


def test_persisted_path_verifier_detects_feature_and_evidence_tampering(tmp_path):
    coinbase, kraken = _databases(tmp_path)
    row = capture_rti_independent_path(
        "BTC", captured_at=1000.0, coinbase_db=str(coinbase),
        kraken_db=str(kraken), max_gap_seconds=10.0,
    )
    tampered_feature = {
        **row,
        "asset": "BTC",
        "source_captured_at": 1000.0,
        "rti_independent_path_mean_depth_imbalance_60s": 0.99,
    }
    feature_result = validate_persisted_independent_path(tampered_feature)
    assert feature_result["valid"] is False
    assert any(
        error.startswith("stored_feature_mismatch:")
        for error in feature_result["errors"]
    )

    tampered_evidence = {
        **row,
        "asset": "BTC",
        "source_captured_at": 1000.0,
        "rti_independent_path_evidence_json": (
            row["rti_independent_path_evidence_json"] + " "
        ),
    }
    evidence_result = validate_persisted_independent_path(tampered_evidence)
    assert evidence_result["valid"] is False
    assert "evidence_sha256_mismatch" in evidence_result["errors"]


def test_independent_reference_catches_shared_capture_verifier_bug(
    tmp_path, monkeypatch,
):
    coinbase, kraken = _databases(tmp_path)
    production_combiner = path_runtime._combined_features

    def wrong_combiner(summaries):
        values = production_combiner(summaries)
        values["rti_independent_path_mean_depth_imbalance_60s"] += 0.125
        return values

    monkeypatch.setattr(path_runtime, "_combined_features", wrong_combiner)
    row = path_runtime.capture_rti_independent_path(
        "BTC", captured_at=1000.0, coinbase_db=str(coinbase),
        kraken_db=str(kraken), max_gap_seconds=10.0,
    )
    completed = {
        **row,
        "asset": "BTC",
        "source_captured_at": 1000.0,
        "close_time": FIRST_ELIGIBLE_CLOSE_TIME,
    }
    # The deliberately broken production capture and production verifier
    # share the same combiner, so their common-mode check incorrectly passes.
    assert path_runtime.validate_persisted_independent_path(completed)[
        "valid"
    ] is True
    independent = verify_reference_formulas(completed)
    assert independent["reference_version"] == REFERENCE_VERSION
    assert independent["valid"] is False
    assert independent["errors"] == [
        "reference_formula_mismatch:"
        "rti_independent_path_mean_depth_imbalance_60s"
    ]
    report = build_report([completed], json.loads(
        (ROOT / "config" / "q15_rti_independent_path_design_v1.json").read_text()
    ))
    assert report["valid_rows"] == 0
    assert report["reference_formula_mismatch_rows"] == 1


def test_outcome_blind_audit_reports_reconstructable_rows_only(tmp_path):
    coinbase, kraken = _databases(tmp_path)
    row = capture_rti_independent_path(
        "BTC", captured_at=1000.0, coinbase_db=str(coinbase),
        kraken_db=str(kraken), max_gap_seconds=10.0,
    )
    design = json.loads(
        (ROOT / "config" / "q15_rti_independent_path_design_v1.json").read_text()
    )
    report = build_report([
        {
            **row,
                "id": 1,
                "asset": "BTC",
                "ticker": _ticker("BTC", FIRST_ELIGIBLE_CLOSE_TIME),
                "source_captured_at": 1000.0,
            "close_time": FIRST_ELIGIBLE_CLOSE_TIME,
        }
    ], design)
    assert report["eligible_rows"] == 1
    assert report["valid_rows"] == 1
    assert report["invalid_rows"] == 0
    assert report["complete_seven_asset_close_windows"] == 0
    assert report["outcome_columns_selected"] is False
    assert report["outcome_labels_read"] is False
    assert report["model_fit_performed"] is False
    assert report["notification_eligible"] is False
    assert report["real_trading_allowed"] is False
    assert report["outcome_blind_geometry"]["status"] == (
        "WAITING_FOR_30_COMPLETE_WINDOWS"
    )
    assert report["outcome_blind_geometry"]["cohorts"]["ALL_SEVEN"][
        "rows"
    ] == 0
    assert report["geometry_review"]["protocol_id"] == GEOMETRY_PROTOCOL_ID
    assert report["geometry_review"]["protocol_sha256"] == (
        GEOMETRY_PROTOCOL_SHA256
    )
    assert report["geometry_review"]["status"] == (
        "WAITING_FOR_30_COMPLETE_WINDOWS"
    )


def test_exact_contract_identity_aligns_asset_ticker_and_dst_safe_close():
    ordinary_close = FIRST_ELIGIBLE_CLOSE_TIME
    valid = validate_exact_contract_identity({
        "asset": "BTC",
        "ticker": _ticker("BTC", ordinary_close),
        "close_time": ordinary_close,
    })
    assert valid["version"] == CONTRACT_IDENTITY_VERSION
    assert valid["valid"] is True
    assert valid["errors"] == []
    assert valid["outcome_labels_read"] is False

    wrong_asset = validate_exact_contract_identity({
        "asset": "ETH",
        "ticker": _ticker("BTC", ordinary_close),
        "close_time": ordinary_close,
    })
    assert wrong_asset["errors"] == ["ticker_asset_mismatch"]

    wrong_close = validate_exact_contract_identity({
        "asset": "BTC",
        "ticker": _ticker("BTC", ordinary_close),
        "close_time": ordinary_close + 900.0,
    })
    assert wrong_close["errors"] == ["ticker_close_time_mismatch"]

    # 01:30 occurs twice on the 2026 fall-back day. The absolute close must
    # match either explicit fold rather than assuming the first occurrence.
    folded = datetime(
        2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"), fold=1,
    )
    dst_safe = validate_exact_contract_identity({
        "asset": "ETH",
        "ticker": "KXETH15M-26NOV010130-30",
        "close_time": folded.timestamp(),
    })
    assert dst_safe["valid"] is True


def test_geometry_is_label_blind_and_detects_redundant_path_fields():
    feature_names = (
        "rti_independent_path_mean_depth_imbalance_60s",
        "rti_independent_path_mean_depth_imbalance_half_delta_60s",
        "rti_independent_path_depth_direction_agreement_60s",
        "rti_independent_path_log1p_max_spread_stress_ratio_60s",
        "rti_independent_path_kraken_partial_fill_imbalance_acceleration_60s",
    )
    rows = []
    for index in range(8):
        first = float(index - 3)
        rows.append({
            feature_names[0]: first,
            feature_names[1]: first,
            feature_names[2]: float(index % 2),
            feature_names[3]: float(index * index + (index % 3)),
            feature_names[4]: float((index * 5) % 7 - 3),
            # Adversarial outcome fields must be irrelevant to geometry.
            "official_result": "YES" if index % 2 else "NO",
            "correct": index % 2,
        })
    report = feature_geometry_report(rows)
    assert report["rows"] == 8
    assert report["finite"] is True
    assert report["active_feature_count"] == 5
    assert report["numerical_rank"] == 4
    assert report["rank_deficiency_vs_active_features"] == 1
    assert report["maximum_absolute_correlation"] == pytest.approx(1.0)
    assert report["exact_signed_duplicate_pairs"] == [{
        "left": feature_names[0],
        "right": feature_names[1],
        "relationship": "same",
    }]
    assert report["outcome_labels_read"] is False
    assert report["model_fit_performed"] is False


def test_selected_feature_evidence_identity_is_order_stable_and_label_blind():
    rows = [{
        "asset": "BTC",
        "ticker": "KXBTC-ONE",
        "side": "YES",
        "close_time": 2000.0,
        "source_captured_at": 1220.0,
        "rti_independent_path_evidence_sha256": "a" * 64,
        "rti_independent_path_mean_depth_imbalance_60s": 0.25,
        "official_result": "YES",
        "correct": 1,
    }, {
        "asset": "ETH",
        "ticker": "KXETH-ONE",
        "side": "NO",
        "close_time": 2000.0,
        "source_captured_at": 1220.0,
        "rti_independent_path_evidence_sha256": "b" * 64,
        "rti_independent_path_mean_depth_imbalance_60s": -0.10,
        "official_result": "NO",
        "correct": 1,
    }]
    original = selected_feature_evidence_identity(rows)
    label_changed = selected_feature_evidence_identity([
        {**row, "official_result": "NO", "correct": 0}
        for row in reversed(rows)
    ])
    feature_changed = selected_feature_evidence_identity([
        rows[0],
        {
            **rows[1],
            "rti_independent_path_mean_depth_imbalance_60s": -0.11,
        },
    ])

    assert original["rows"] == 2
    assert original["outcome_columns_selected"] is False
    assert original["sha256"] == label_changed["sha256"]
    assert original["sha256"] != feature_changed["sha256"]


def test_source_quality_uses_frozen_integrity_margins_without_labels(tmp_path):
    coinbase, kraken = _databases(tmp_path)
    row = capture_rti_independent_path(
        "BTC", captured_at=1000.0, coinbase_db=str(coinbase),
        kraken_db=str(kraken), max_gap_seconds=10.0,
    )
    report = source_quality_report([{
        **row,
        "asset": "BTC",
        "official_result": "YES",
        "correct": 1,
    }])
    assert report["status"] == "PASS_ALL_CREDITED_COMPLETE_ROWS"
    assert report["credited_complete_rows"] == 1
    assert report["integrity_breaches"] == 0
    assert report["evidence_parse_failures"] == 0
    assert report["minimum_integrity_margin_seconds"] >= 0.0
    assert report["venues"]["coinbase"]["rows"] == 1
    assert report["venues"]["kraken"]["rows"] == 1
    assert report["source_thresholds_from_frozen_design"] is True
    assert report["thresholds_selected_from_outcomes"] is False
    assert report["outcome_labels_read"] is False
    assert source_quality_report([])["status"] == (
        "WAITING_FOR_CREDITED_COMPLETE_ROWS"
    )


def _geometry_review_report(windows: int = 30) -> dict:
    def cohort(rows: int) -> dict:
        return {
            "rows": rows,
            "feature_count": 5,
            "finite": True,
            "active_feature_count": 5,
            "maximum_absolute_correlation": 0.7,
            "exact_signed_duplicate_pairs": [],
            "condition_number_nonzero_subspace": 4.0,
            "rank_deficiency_vs_active_features": 0,
        }

    return {
        # Deliberately larger than the evidence slice: review must remain
        # pinned to the earliest 30 complete windows when collection advances.
        "complete_seven_asset_close_windows": windows + 5,
        "geometry_review_evidence": {
            "complete_close_windows": windows,
            "cohorts": {
                "ALL_SEVEN": cohort(windows * 7),
                "BTC": cohort(windows),
                "NON_BTC_TRANSFER": cohort(windows * 6),
            },
            "source_quality": {
                "status": "PASS_ALL_CREDITED_COMPLETE_ROWS",
                "evidence_parse_failures": 0,
                "integrity_breaches": 0,
                "minimum_integrity_margin_seconds": 2.0,
            },
        },
    }


def test_frozen_geometry_review_passes_only_locked_first_30_slice():
    protocol = load_geometry_protocol()
    review = evaluate_geometry_review(_geometry_review_report(30), protocol)
    assert review["review_ready"] is True
    assert review["all_checks_met"] is True
    assert review["failed_checks"] == []
    assert review["status"] == "PASS_CONTINUE_PROSPECTIVE_COLLECTION_NO_MODEL"
    assert review["pass_does_not_authorize_model_fit"] is True
    assert review["pass_does_not_authorize_outcome_access"] is True
    assert review["automatic_action_allowed"] is False
    assert review["outcome_labels_read"] is False


def test_frozen_geometry_review_waits_and_fails_without_automatic_action():
    protocol = load_geometry_protocol()
    waiting = evaluate_geometry_review(_geometry_review_report(29), protocol)
    assert waiting["review_ready"] is False
    assert waiting["status"] == "WAITING_FOR_30_COMPLETE_WINDOWS"
    failing_report = _geometry_review_report(30)
    failing_report["geometry_review_evidence"]["cohorts"]["BTC"][
        "maximum_absolute_correlation"
    ] = 0.96
    failed = evaluate_geometry_review(failing_report, protocol)
    assert failed["review_ready"] is True
    assert failed["all_checks_met"] is False
    assert "all_pairwise_correlations_within_ceiling" in failed[
        "failed_checks"
    ]
    assert failed["status"] == (
        "GEOMETRY_REVIEW_REQUIRED_NO_AUTOMATIC_ACTION"
    )
    assert failed["automatic_action_allowed"] is False


def test_path_fails_closed_on_continuity_gap(tmp_path):
    coinbase, kraken = _databases(tmp_path, gap=True)
    row = capture_rti_independent_path(
        "BTC", captured_at=1000.0, coinbase_db=str(coinbase),
        kraken_db=str(kraken), max_gap_seconds=10.0,
    )
    assert row["rti_independent_path_status"] == "missing"
    assert "path_continuity_gap" in row["rti_independent_path_missing_reason"]
    assert row["rti_independent_path_evidence_json"] is None


def test_path_fails_closed_on_stale_transport(tmp_path):
    coinbase, kraken = _databases(tmp_path, message_age=11.0)
    row = capture_rti_independent_path(
        "BTC", captured_at=1000.0, coinbase_db=str(coinbase),
        kraken_db=str(kraken), max_gap_seconds=10.0,
    )
    assert row["rti_independent_path_status"] == "missing"
    assert "message_age_invalid" in row["rti_independent_path_missing_reason"]


def test_path_rejects_unversioned_kraken_partial_fill_flow(tmp_path):
    coinbase, kraken = _databases(tmp_path)
    connection = sqlite3.connect(kraken)
    connection.execute(
        "UPDATE kraken_l3_summaries SET partial_fill_flow_schema_version='legacy'"
    )
    connection.commit()
    connection.close()
    row = capture_rti_independent_path(
        "BTC", captured_at=1000.0, coinbase_db=str(coinbase),
        kraken_db=str(kraken), max_gap_seconds=10.0,
    )
    assert row["rti_independent_path_status"] == "missing"
    assert "partial_fill_flow_schema_mismatch" in row[
        "rti_independent_path_missing_reason"
    ]
