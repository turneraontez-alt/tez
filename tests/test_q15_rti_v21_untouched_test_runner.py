from __future__ import annotations

import json

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v21_audit_identity as audit_identity
from tools import q15_rti_v21_modeling as modeling
from tools import q15_rti_v21_pretest_runner as pretest
from tools import q15_rti_v21_untouched_test_runner as runner
from test_q15_rti_v21_modeling import _population
from test_q15_rti_v21_pretest_runner import _fake_modeled, _settlement_labels


def _test_settlements(seal, labels):
    output = {}
    for row in seal["rows"]:
        if row["partition"] != modeling.TEST_PARTITION:
            continue
        row_id = int(row["parent_id"])
        survival = int(labels[row_id])
        output[row_id] = survival if row["side"] == "YES" else 1 - survival
    return output


def _passing_pretest(tmp_path, monkeypatch):
    seal, labels = _population()
    reservation = tmp_path / "pretest-reservation.json"
    monkeypatch.setattr(
        modeling, "evaluate_pretest",
        lambda payload, survival: _fake_modeled(payload, survival, passed=True),
    )
    result = pretest.run_pretest_once(
        seal=seal,
        reservation_path=reservation,
        confirmation=audit_identity.PRETEST_CONFIRMATION,
        read_settlement_yes_labels=lambda _ids: _settlement_labels(seal, labels),
        require_label_evidence=False,
    )
    assert result["status"] == pretest.PASS_STATUS
    return seal, labels, reservation


def _fake_test_report(seal, survival, bundle, *, passed=True):
    assert set(int(value) for value in survival) == {
        int(row["parent_id"]) for row in seal["rows"]
        if row["partition"] == modeling.TEST_PARTITION
    }
    assert bundle["feature_seal_sha256"] == seal["seal_sha256"]
    return {
        "evaluator_contract_sha256": audit_identity.EVALUATOR_CONTRACT_SHA256,
        "feature_seal_sha256": seal["seal_sha256"],
        "untouched_test_label_rows": 175,
        "untouched_test_labels_read": True,
        "untouched_test_scoring_performed": True,
        "historical_gate_met": passed,
    }


def test_v21_test_confirmation_required_before_reservation_or_callback(
    tmp_path, monkeypatch,
):
    seal, _labels, pretest_reservation = _passing_pretest(tmp_path, monkeypatch)
    reservation = tmp_path / "test-reservation.json"
    called = False

    def callback(_ids):
        nonlocal called
        called = True
        return {}

    with pytest.raises(ValueError, match="explicit_one_shot_confirmation"):
        runner.run_untouched_test_once(
            seal=seal,
            pretest_reservation_path=pretest_reservation,
            reservation_path=reservation,
            confirmation="WRONG",
            read_settlement_yes_labels=callback,
            require_label_evidence=False,
        )
    assert called is False
    assert reservation.exists() is False


def test_v21_test_callback_gets_only_exact_test_ids_and_replay_never_rereads(
    tmp_path, monkeypatch,
):
    seal, labels, pretest_reservation = _passing_pretest(tmp_path, monkeypatch)
    reservation = tmp_path / "test-reservation.json"
    expected = {
        int(row["parent_id"]) for row in seal["rows"]
        if row["partition"] == modeling.TEST_PARTITION
    }
    observed = None
    monkeypatch.setattr(
        modeling, "evaluate_untouched_test",
        lambda payload, survival, bundle: _fake_test_report(
            payload, survival, bundle, passed=False,
        ),
    )

    def callback(ids):
        nonlocal observed
        observed = set(int(value) for value in ids)
        return _test_settlements(seal, labels)

    first = runner.run_untouched_test_once(
        seal=seal,
        pretest_reservation_path=pretest_reservation,
        reservation_path=reservation,
        confirmation=audit_identity.UNTOUCHED_TEST_CONFIRMATION,
        read_settlement_yes_labels=callback,
        require_label_evidence=False,
    )
    assert first["status"] == runner.REJECT_STATUS
    assert observed == expected
    assert len(observed) == 175
    result = first["result"]
    assert result["model_refit_performed"] is False
    assert result["recalibration_performed"] is False
    assert result["margin_selection_performed"] is False
    assert result["manual_paper_challenger_eligible"] is False
    replay = runner.run_untouched_test_once(
        seal=seal,
        pretest_reservation_path=pretest_reservation,
        reservation_path=reservation,
        confirmation=audit_identity.UNTOUCHED_TEST_CONFIRMATION,
        read_settlement_yes_labels=lambda _ids: pytest.fail("labels reread"),
        require_label_evidence=False,
    )
    assert replay["status"] == "ALREADY_FINALIZED_NO_REREAD"
    assert replay["untouched_test_labels_read_this_call"] is False


def test_v21_interrupted_test_reservation_is_permanently_ambiguous(
    tmp_path, monkeypatch,
):
    seal, _labels, pretest_reservation = _passing_pretest(tmp_path, monkeypatch)
    reservation = tmp_path / "test-reservation.json"

    def failing(_ids):
        raise RuntimeError("simulated test interruption")

    with pytest.raises(RuntimeError, match="test interruption"):
        runner.run_untouched_test_once(
            seal=seal,
            pretest_reservation_path=pretest_reservation,
            reservation_path=reservation,
            confirmation=audit_identity.UNTOUCHED_TEST_CONFIRMATION,
            read_settlement_yes_labels=failing,
            require_label_evidence=False,
        )
    replay = runner.run_untouched_test_once(
        seal=seal,
        pretest_reservation_path=pretest_reservation,
        reservation_path=reservation,
        confirmation=audit_identity.UNTOUCHED_TEST_CONFIRMATION,
        read_settlement_yes_labels=lambda _ids: pytest.fail("labels reread"),
        require_label_evidence=False,
    )
    assert replay["status"] == "AMBIGUOUS_RESERVED_NO_REREAD"


def test_v21_test_result_semantic_tamper_fails_with_recomputed_state_hash(
    tmp_path, monkeypatch,
):
    seal, labels, pretest_reservation = _passing_pretest(tmp_path, monkeypatch)
    reservation = tmp_path / "test-reservation.json"
    monkeypatch.setattr(
        modeling, "evaluate_untouched_test",
        lambda payload, survival, bundle: _fake_test_report(
            payload, survival, bundle, passed=True,
        ),
    )
    runner.run_untouched_test_once(
        seal=seal,
        pretest_reservation_path=pretest_reservation,
        reservation_path=reservation,
        confirmation=audit_identity.UNTOUCHED_TEST_CONFIRMATION,
        read_settlement_yes_labels=lambda _ids: _test_settlements(seal, labels),
        require_label_evidence=False,
    )
    result_path = runner.result_path_for(reservation)
    stored = json.loads(result_path.read_text(encoding="utf-8"))
    stored["survival_label_rows"][0]["label_survives"] ^= 1
    pairs = sorted(
        [int(row["parent_id"]), int(row["label_survives"])]
        for row in stored["survival_label_rows"]
    )
    stored["survival_labels_sha256"] = runner._canonical_sha256(pairs)
    stored = runner._sealed(stored)
    result_path.write_text(json.dumps(stored), encoding="utf-8")
    with pytest.raises(ValueError, match="result_invalid"):
        runner.run_untouched_test_once(
            seal=seal,
            pretest_reservation_path=pretest_reservation,
            reservation_path=reservation,
            confirmation=audit_identity.UNTOUCHED_TEST_CONFIRMATION,
            read_settlement_yes_labels=lambda _ids: pytest.fail("labels reread"),
            require_label_evidence=False,
        )


def test_v21_complete_synthetic_one_shot_pipeline_never_promotes(
    tmp_path,
):
    seal, labels = _population()
    pretest_reservation = tmp_path / "pretest-reservation.json"
    pretest_result = pretest.run_pretest_once(
        seal=seal,
        reservation_path=pretest_reservation,
        confirmation=audit_identity.PRETEST_CONFIRMATION,
        read_settlement_yes_labels=lambda _ids: _settlement_labels(seal, labels),
        require_label_evidence=False,
    )
    assert pretest_result["status"] == pretest.PASS_STATUS
    test_result = runner.run_untouched_test_once(
        seal=seal,
        pretest_reservation_path=pretest_reservation,
        reservation_path=tmp_path / "test-reservation.json",
        confirmation=audit_identity.UNTOUCHED_TEST_CONFIRMATION,
        read_settlement_yes_labels=lambda _ids: _test_settlements(seal, labels),
        require_label_evidence=False,
    )
    assert test_result["status"] in {runner.PASS_STATUS, runner.REJECT_STATUS}
    result = test_result["result"]
    assert result["untouched_test_report"]["untouched_test_label_rows"] == 175
    assert result["model_refit_performed"] is False
    assert result["recalibration_performed"] is False
    assert result["margin_selection_performed"] is False
    assert result["paper_artifact_created"] is False
    assert result["notification_eligible"] is False
    assert result["automatic_promotion"] is False
    assert result["real_trading_allowed"] is False
