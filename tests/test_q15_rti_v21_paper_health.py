from __future__ import annotations

from tools import q15_rti_v21_paper_artifact as artifact
from tools import q15_rti_v21_paper_health as health


def _collection():
    return {
        "status": "COLLECTING_V21_PROSPECTIVE_FEATURES_NO_OUTCOMES",
        "v21_feature_complete_close_windows": 0,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "real_trading_allowed": False,
    }


def _bindings():
    return {
        "source_protocol_sha256": artifact.identity.PROTOCOL_SHA256,
        "evaluator_contract_sha256": artifact.identity.EVALUATOR_CONTRACT_SHA256,
        "paper_deployment_protocol_sha256": artifact.paper_identity.PROTOCOL_SHA256,
        "feature_names_sha256": artifact.identity.FEATURE_NAMES_SHA256,
        "feature_seal_sha256": "1" * 64,
        "pretest_reservation_state_sha256": "2" * 64,
        "pretest_result_state_sha256": "3" * 64,
        "pretest_report_sha256": "4" * 64,
        "audit_model_bundle_sha256": "5" * 64,
        "untouched_test_reservation_state_sha256": "6" * 64,
        "untouched_test_result_state_sha256": "7" * 64,
        "untouched_test_report_sha256": "8" * 64,
    }


def _reservation(output, timestamp="2026-08-01T17:20:00+00:00"):
    created = 1785604800.0
    return artifact._write_state_exclusive(
        output / "artifact.reservation.json",
        artifact._expected_reservation(
            bindings=_bindings(),
            created_at=timestamp,
            created_at_unix=created,
            prospective_after_close_time=artifact._first_complete_close_after_load(created),
        ),
    )


def test_v21_paper_health_is_dormant_and_writes_nothing_without_artifact(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(health, "_collection_health", lambda _path: _collection())
    output = tmp_path / "artifacts"
    result = health.build_health(
        strategy_db=tmp_path / "unused.sqlite3", artifact_dir=output,
        ledger_root=tmp_path,
    )
    assert result["status"] == "DORMANT_AWAITING_PASSING_HISTORICAL_AUDIT"
    assert result["artifact_created"] is False
    assert result["runtime_scoring_connected"] is False
    assert result["notifications_enabled"] is False
    assert result["automatic_promotion"] is False
    assert result["real_trading_allowed"] is False
    assert output.exists() is False


def test_v21_paper_health_fails_closed_on_orphan_or_ambiguous_artifacts(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(health, "_collection_health", lambda _path: _collection())
    orphan = tmp_path / "orphan"
    orphan.mkdir()
    (orphan / "BTC.joblib").write_bytes(b"orphan")
    result = health.build_health(
        strategy_db=tmp_path / "unused.sqlite3", artifact_dir=orphan,
        ledger_root=tmp_path,
    )
    assert result["status"] == "INVALID_ARTIFACT_STATE_FAIL_CLOSED"

    ambiguous = tmp_path / "ambiguous"
    _reservation(ambiguous)
    result = health.build_health(
        strategy_db=tmp_path / "unused.sqlite3", artifact_dir=ambiguous,
        ledger_root=tmp_path,
    )
    assert result["status"] == "AMBIGUOUS_ARTIFACT_RESERVATION_FAIL_CLOSED"
    assert result["artifact_created"] is False


def test_v21_valid_artifact_health_never_activates_runtime_or_notifications(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(health, "_collection_health", lambda _path: _collection())
    output = tmp_path / "artifacts"
    reservation = _reservation(output)
    for cohort in artifact.COHORT_ASSETS:
        (output / f"{cohort}.joblib").write_bytes(b"placeholder")
    result_state = artifact._write_state_exclusive(
        output / "artifact.result.json",
        {
            "state_version": artifact.STATE_VERSION,
            "status": artifact.FINAL_STATUS,
            "reservation_state_sha256": reservation["state_sha256"],
            "artifacts": {},
            "outcome_labels_read_by_artifact_command": False,
            "model_fit_performed": False,
            "recalibration_performed": False,
            "margin_selection_performed": False,
            "runtime_scoring_connected": False,
            "notifications_enabled": False,
            "automatic_promotion": False,
            "real_trading_allowed": False,
        },
    )
    monkeypatch.setattr(artifact, "_validate_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        health.joblib, "load",
        lambda _path: {
            "created_at_unix": reservation["created_at_unix"],
            "prospective_after_close_time": reservation["prospective_after_close_time"],
        },
    )
    result = health.build_health(
        strategy_db=tmp_path / "unused.sqlite3", artifact_dir=output,
        ledger_root=tmp_path / "missing-ledgers",
    )
    assert result["status"] == "PAPER_ARTIFACT_VALID_NOT_RUNTIME_CONNECTED"
    assert result["artifact_result_state_sha256"] == result_state["state_sha256"]
    assert all(
        item["status"] == "NOT_CREATED_AWAITING_MANUAL_RUNTIME_ACTIVATION"
        for item in result["cohort_ledgers"].values()
    )
    assert result["notifications_enabled"] is False
    assert result["real_trading_allowed"] is False
