from __future__ import annotations

import copy
import json
import sqlite3

import pytest

from q15_upgrade.strategy_bots.rti_independent_path_geometry_freeze_identity import (
    CONTRACT_ID,
    CONTRACT_SHA256,
)
from tools import q15_rti_independent_path_geometry_freeze as freeze
from tools import q15_rti_microstructure_freeze as feature_freeze
from tools.q15_rti_microstructure_preregister import design_fingerprint


def _cohort(rows: int) -> dict:
    return {
        "rows": rows,
        "feature_count": 5,
        "finite": True,
        "active_feature_count": 5,
        "numerical_rank": 5,
        "condition_number_nonzero_subspace": 2.0,
        "maximum_absolute_correlation": 0.4,
        "exact_signed_duplicate_pairs": [],
    }


def _source_quality(rows: int) -> dict:
    return {
        "status": "PASS_ALL_CREDITED_COMPLETE_ROWS",
        "credited_complete_rows": rows,
        "evidence_parse_failures": 0,
        "integrity_breaches": 0,
        "minimum_integrity_margin_seconds": 2.5,
        "outcome_labels_read": False,
        "source_thresholds_from_frozen_design": True,
        "thresholds_selected_from_outcomes": False,
        "selected_feature_evidence_identity": {
            "version": freeze.SELECTED_EVIDENCE_IDENTITY_VERSION,
            "rows": rows,
            "sha256": f"{rows:064x}",
            "outcome_columns_selected": False,
            "outcome_labels_read": False,
        },
        "contract_identity": {
            "version": freeze.CONTRACT_IDENTITY_VERSION,
            "rows": rows,
            "mismatch_rows": 0,
            "ticker_asset_alignment_required": True,
            "ticker_close_time_alignment_required": True,
            "dst_fold_safe": True,
            "outcome_labels_read": False,
        },
        "venues": {"coinbase": {"rows": rows}, "kraken": {"rows": rows}},
    }


def _report(windows: int = 30, *, passed: bool = True) -> dict:
    selected = min(windows, 30)
    decision = (
        "PASS_CONTINUE_PROSPECTIVE_COLLECTION_NO_MODEL"
        if passed else "GEOMETRY_REVIEW_REQUIRED_NO_AUTOMATIC_ACTION"
    )
    report = {
        "design_id": freeze.DESIGN_ID,
        "design_sha256": freeze.DESIGN_SHA256,
        "complete_seven_asset_close_windows": windows,
        "geometry_review_evidence": {
            "selection": "EARLIEST_30_COMPLETE_RECONSTRUCTABLE_WINDOWS",
            "complete_close_windows": selected,
            "complete_close_times": [
                float(freeze.FIRST_ELIGIBLE_CLOSE_TIME + 900 * i)
                for i in range(selected)
            ],
            "rows": selected * 7,
            "cohorts": {
                "ALL_SEVEN": _cohort(selected * 7),
                "BTC": _cohort(selected),
                "NON_BTC_TRANSFER": _cohort(selected * 6),
            },
            "source_quality": _source_quality(selected * 7),
            "outcome_columns_selected": False,
            "outcome_labels_read": False,
            "model_fit_performed": False,
        },
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "automatic_scoring": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }
    if not passed:
        report["geometry_review_evidence"]["cohorts"]["BTC"][
            "maximum_absolute_correlation"
        ] = 0.99
    report["geometry_review"] = freeze._expected_review_from_evidence(
        report["geometry_review_evidence"]
    )
    assert report["geometry_review"]["status"] == (
        decision if windows >= 30 else "WAITING_FOR_30_COMPLETE_WINDOWS"
    )
    return report


def test_freeze_contract_is_frozen_before_review_and_valid():
    contract = freeze.load_contract()
    assert contract["contract_id"] == CONTRACT_ID
    assert design_fingerprint(contract) == CONTRACT_SHA256
    assert contract["evidence_available_at_preregistration"][
        "complete_reconstructable_close_windows"
    ] == 18
    assert contract["evidence_available_at_preregistration"][
        "outcome_labels_read"
    ] is False
    freeze.validate_contract(contract)


def test_contract_tampering_fails_before_semantic_use():
    contract = freeze.load_contract()
    tampered = copy.deepcopy(contract)
    tampered["trigger"]["selected_close_windows_must_equal"] = 29
    with pytest.raises(ValueError, match="geometry_freeze_contract_sha256_mismatch"):
        freeze.validate_contract(tampered)


def test_feature_projection_cannot_select_outcomes():
    assert freeze.OUTCOME_COLUMNS.isdisjoint(freeze.FEATURE_SELECT_COLUMNS)


def test_sqlite_authorizer_denies_outcome_column_reads():
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE evidence (id INTEGER, official_result TEXT)"
        )
        connection.execute(
            "INSERT INTO evidence (id, official_result) VALUES (1, 'YES')"
        )
        connection.set_authorizer(
            feature_freeze._feature_only_sqlite_authorizer
        )
        assert connection.execute("SELECT id FROM evidence").fetchone() == (1,)
        with pytest.raises(sqlite3.DatabaseError, match="prohibited"):
            connection.execute(
                "SELECT official_result FROM evidence"
            ).fetchall()
    finally:
        connection.close()


def test_waiting_review_cannot_create_directory_or_artifact(tmp_path):
    artifact = tmp_path / "nested" / "geometry-review.json"
    result = freeze.freeze_report(_report(29), artifact, dry_run=False)
    assert result["status"] == "WAITING_FOR_30_COMPLETE_WINDOWS"
    assert result["artifact_written"] is False
    assert result["windows_remaining"] == 1
    assert not artifact.exists()
    assert not artifact.parent.exists()


def test_ready_dry_run_hashes_exact_first_30_without_writing(tmp_path):
    artifact = tmp_path / "geometry-review.json"
    result = freeze.freeze_report(
        _report(35), artifact, dry_run=True,
        frozen_at="2026-07-23T01:00:00Z",
    )
    assert result["status"] == "READY_DRY_RUN_NO_ARTIFACT_WRITTEN"
    assert result["artifact_written"] is False
    assert len(result["payload_sha256"]) == 64
    assert result["decision"] == "PASS_CONTINUE_PROSPECTIVE_COLLECTION_NO_MODEL"
    assert not artifact.exists()


def test_pass_payload_continues_collection_without_model_or_outcomes():
    payload = freeze.build_payload(
        _report(30, passed=True), frozen_at="2026-07-23T01:00:00Z",
    )
    assert payload["selected_rows"] == 210
    assert len(payload["selected_complete_close_times"]) == 30
    assert payload["decision"] == "PASS_CONTINUE_PROSPECTIVE_COLLECTION_NO_MODEL"
    assert payload["consequences"]["continue_to_non_btc_60_and_btc_150"] is True
    assert payload["consequences"]["outcome_access_allowed_at_30"] is False
    assert payload["consequences"]["model_fit_allowed_at_30"] is False
    assert payload["outcome_labels_read"] is False
    assert payload["model_fit_performed"] is False


def test_failure_payload_requires_manual_diagnosis_and_no_auto_change():
    payload = freeze.build_payload(
        _report(30, passed=False), frozen_at="2026-07-23T01:00:00Z",
    )
    assert payload["decision"] == "GEOMETRY_REVIEW_REQUIRED_NO_AUTOMATIC_ACTION"
    consequences = payload["consequences"]
    assert consequences["continue_to_non_btc_60_and_btc_150"] is False
    assert consequences["manual_diagnosis_required"] is True
    assert consequences["automatic_feature_or_threshold_change_allowed"] is False
    assert consequences["automatic_activation_allowed"] is False
    assert consequences["automatic_promotion_allowed"] is False
    assert consequences["real_trading_allowed"] is False


def test_written_artifact_is_valid_and_idempotent(tmp_path):
    path = tmp_path / "geometry-review.json"
    report = _report(30)
    first = freeze.freeze_report(
        report, path, frozen_at="2026-07-23T01:00:00Z",
    )
    second = freeze.freeze_report(report, path)
    assert first["status"] == "IMMUTABLE_GEOMETRY_ARTIFACT_WRITTEN"
    assert first["artifact_written"] is True
    assert second["status"] == "EXISTING_IMMUTABLE_ARTIFACT_VERIFIED"
    assert second["artifact_written"] is False
    assert second["payload_sha256"] == first["payload_sha256"]
    decoded = json.loads(path.read_text(encoding="utf-8"))
    freeze.validate_artifact(decoded)


def test_existing_artifact_fails_if_first_30_evidence_changes(tmp_path):
    path = tmp_path / "geometry-review.json"
    report = _report(30)
    freeze.freeze_report(report, path, frozen_at="2026-07-23T01:00:00Z")
    changed = copy.deepcopy(report)
    changed["geometry_review_evidence"]["cohorts"]["BTC"][
        "maximum_absolute_correlation"
    ] = 0.9
    with pytest.raises(
        ValueError, match="geometry_freeze_existing_artifact_evidence_mismatch"
    ):
        freeze.freeze_report(changed, path)


def test_existing_artifact_fails_if_exact_feature_evidence_hash_changes(tmp_path):
    path = tmp_path / "geometry-review.json"
    report = _report(30)
    freeze.freeze_report(report, path, frozen_at="2026-07-23T01:00:00Z")
    changed = copy.deepcopy(report)
    changed["geometry_review_evidence"]["source_quality"][
        "selected_feature_evidence_identity"
    ]["sha256"] = "f" * 64
    with pytest.raises(
        ValueError, match="geometry_freeze_existing_artifact_evidence_mismatch"
    ):
        freeze.freeze_report(changed, path)


def test_rehashed_safety_tamper_still_fails_semantic_validation():
    payload = freeze.build_payload(
        _report(30), frozen_at="2026-07-23T01:00:00Z",
    )
    artifact = freeze.wrap_payload(payload)
    tampered = copy.deepcopy(artifact)
    tampered["payload"]["model_fit_performed"] = True
    tampered["payload_sha256"] = freeze.canonical_sha256(tampered["payload"])
    with pytest.raises(ValueError, match="geometry_freeze_artifact_payload_invalid"):
        freeze.validate_artifact(tampered)


def test_rehashed_forged_pass_fails_independent_review_recomputation():
    payload = freeze.build_payload(
        _report(30, passed=False), frozen_at="2026-07-23T01:00:00Z",
    )
    payload["decision"] = "PASS_CONTINUE_PROSPECTIVE_COLLECTION_NO_MODEL"
    payload["geometry_review"]["status"] = payload["decision"]
    payload["consequences"]["continue_to_non_btc_60_and_btc_150"] = True
    payload["consequences"]["manual_diagnosis_required"] = False
    artifact = freeze.wrap_payload(payload)
    with pytest.raises(
        ValueError,
        match="geometry_freeze_artifact_review_recomputation_mismatch",
    ):
        freeze.validate_artifact(artifact)


def test_rehashed_duplicate_selected_close_time_fails_semantic_validation():
    payload = freeze.build_payload(
        _report(30), frozen_at="2026-07-23T01:00:00Z",
    )
    payload["selected_complete_close_times"][1] = (
        payload["selected_complete_close_times"][0]
    )
    payload["selected_close_times_sha256"] = freeze.canonical_sha256(
        payload["selected_complete_close_times"]
    )
    artifact = freeze.wrap_payload(payload)
    with pytest.raises(ValueError, match="geometry_freeze_artifact_payload_invalid"):
        freeze.validate_artifact(artifact)
