from __future__ import annotations

from pathlib import Path

import joblib
import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v21_audit_identity as audit_identity
from tools import q15_rti_v21_modeling as modeling
from tools import q15_rti_v21_paper_artifact as artifact
from tools import q15_rti_v21_pretest_runner as pretest
from tools import q15_rti_v21_untouched_test_runner as untouched
from test_q15_rti_v21_modeling import _population
from test_q15_rti_v21_pretest_runner import _fake_modeled, _settlement_labels
from test_q15_rti_v21_untouched_test_runner import _fake_test_report, _test_settlements


def _modeled_with_artifacts(seal, survival):
    result = _fake_modeled(seal, survival, passed=True)
    result["artifacts"] = {
        cohort: {
            "cohort": cohort,
            "selected_spec": {"family": "frozen", "cohort": cohort},
            "selected_model_id": f"frozen-{cohort}",
            "base_model": {"kind": "base", "cohort": cohort},
            "platt_calibrator": {"method": "IDENTITY"},
            "selected_calibrator_method": "IDENTITY",
            "selected_margin": 0.025 if cohort == "NON_BTC_TRANSFER" else 0.04,
            "v20_feature_map_ablation_base_model": {
                "kind": "v20-base", "cohort": cohort,
            },
            "v20_feature_map_ablation_selected_spec": {
                "family": "v20-frozen", "cohort": cohort,
            },
            "v20_feature_map_ablation_selected_model_id": f"v20-frozen-{cohort}",
            "v20_feature_map_ablation_platt_calibrator": {"method": "IDENTITY"},
            "v20_feature_map_ablation_selected_calibrator_method": "IDENTITY",
        }
        for cohort in modeling.COHORTS
    }
    return result


def _historical_chain(tmp_path, monkeypatch, *, test_passed=True):
    seal, labels = _population()
    pretest_path = tmp_path / "audit" / "pretest-reservation.json"
    test_path = tmp_path / "audit" / "untouched-test-reservation.json"
    monkeypatch.setattr(modeling, "evaluate_pretest", _modeled_with_artifacts)
    pretest_result = pretest.run_pretest_once(
        seal=seal,
        reservation_path=pretest_path,
        confirmation=audit_identity.PRETEST_CONFIRMATION,
        read_settlement_yes_labels=lambda _ids: _settlement_labels(seal, labels),
        require_label_evidence=False,
    )
    assert pretest_result["status"] == pretest.PASS_STATUS
    monkeypatch.setattr(
        modeling,
        "evaluate_untouched_test",
        lambda payload, survival, bundle: _fake_test_report(
            payload, survival, bundle, passed=test_passed,
        ),
    )
    test_result = untouched.run_untouched_test_once(
        seal=seal,
        pretest_reservation_path=pretest_path,
        reservation_path=test_path,
        confirmation=audit_identity.UNTOUCHED_TEST_CONFIRMATION,
        read_settlement_yes_labels=lambda _ids: _test_settlements(seal, labels),
        require_label_evidence=False,
    )
    return seal, pretest_path, test_path, test_result


def test_v21_paper_artifact_requires_passing_untouched_test(tmp_path, monkeypatch):
    seal, pretest_path, test_path, result = _historical_chain(
        tmp_path, monkeypatch, test_passed=False,
    )
    assert result["status"] == untouched.REJECT_STATUS
    output = tmp_path / "paper"
    with pytest.raises(ValueError, match="historical_gate_not_passed"):
        artifact.create_paper_artifacts_once(
            seal=seal,
            pretest_reservation_path=pretest_path,
            untouched_reservation_path=test_path,
            output_dir=output,
            confirmation=artifact.CONFIRMATION,
        )
    assert output.exists() is False


def test_v21_paper_artifact_manual_confirmation_precedes_any_write(
    tmp_path, monkeypatch,
):
    seal, pretest_path, test_path, result = _historical_chain(
        tmp_path, monkeypatch,
    )
    assert result["status"] == untouched.PASS_STATUS
    output = tmp_path / "paper"
    with pytest.raises(ValueError, match="manual_confirmation_required"):
        artifact.create_paper_artifacts_once(
            seal=seal,
            pretest_reservation_path=pretest_path,
            untouched_reservation_path=test_path,
            output_dir=output,
            confirmation="WRONG",
        )
    assert output.exists() is False


def test_v21_paper_artifacts_copy_frozen_models_and_stay_disconnected(
    tmp_path, monkeypatch,
):
    seal, pretest_path, test_path, _result = _historical_chain(
        tmp_path, monkeypatch,
    )
    output = tmp_path / "paper"
    created = artifact.create_paper_artifacts_once(
        seal=seal,
        pretest_reservation_path=pretest_path,
        untouched_reservation_path=test_path,
        output_dir=output,
        confirmation=artifact.CONFIRMATION,
        timestamp="2026-08-01T17:20:00Z",
    )
    assert created["status"] == artifact.FINAL_STATUS
    assert created["created"] is True
    for cohort, assets in artifact.COHORT_ASSETS.items():
        payload = joblib.load(output / f"{cohort}.joblib")
        assert payload["cohort"] == cohort
        assert payload["assets"] == assets
        assert payload["notification_label"] == "V21 PAPER"
        assert payload["paper_only"] is True
        assert payload["runtime_scoring_connected"] is False
        assert payload["notifications_enabled"] is False
        assert payload["automatic_promotion"] is False
        assert payload["real_trading_allowed"] is False
        assert payload["prospective_after_close_time"] - 720.0 > payload["created_at_unix"]
        assert payload["bindings"]["feature_seal_sha256"] == seal["seal_sha256"]
    replay = artifact.create_paper_artifacts_once(
        seal=seal,
        pretest_reservation_path=pretest_path,
        untouched_reservation_path=test_path,
        output_dir=output,
        confirmation=artifact.CONFIRMATION,
    )
    assert replay["status"] == "ALREADY_FINALIZED"
    assert replay["created"] is False


def test_v21_paper_artifact_tamper_and_ambiguous_recovery_fail_closed(
    tmp_path, monkeypatch,
):
    seal, pretest_path, test_path, _result = _historical_chain(
        tmp_path, monkeypatch,
    )
    output = tmp_path / "paper"
    artifact.create_paper_artifacts_once(
        seal=seal,
        pretest_reservation_path=pretest_path,
        untouched_reservation_path=test_path,
        output_dir=output,
        confirmation=artifact.CONFIRMATION,
    )
    (output / "artifact.result.json").unlink()
    replay = artifact.create_paper_artifacts_once(
        seal=seal,
        pretest_reservation_path=pretest_path,
        untouched_reservation_path=test_path,
        output_dir=output,
        confirmation=artifact.CONFIRMATION,
    )
    assert replay["status"] == "AMBIGUOUS_RESERVED_NO_RETRY"

    # A finalized artifact is also cryptographically bound to its bytes.
    output2 = tmp_path / "paper2"
    artifact.create_paper_artifacts_once(
        seal=seal,
        pretest_reservation_path=pretest_path,
        untouched_reservation_path=test_path,
        output_dir=output2,
        confirmation=artifact.CONFIRMATION,
    )
    with (output2 / "BTC.joblib").open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(ValueError, match="artifact_file_invalid"):
        artifact.create_paper_artifacts_once(
            seal=seal,
            pretest_reservation_path=pretest_path,
            untouched_reservation_path=test_path,
            output_dir=output2,
            confirmation=artifact.CONFIRMATION,
        )


def test_v21_paper_artifact_module_has_no_label_network_notify_or_order_route():
    source = Path(artifact.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "import sqlite3",
        "KalshiClient",
        "get_market(",
        "read_settlement_yes_labels",
        "TelegramSendClient",
        "send_message(",
        "place_order(",
        "fit_model(",
        "evaluate_pretest(",
        "evaluate_untouched_test(",
    ):
        assert forbidden not in source
