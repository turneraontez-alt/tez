from __future__ import annotations

import json

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v20_audit_identity as audit_identity
from tools import q15_rti_v20_pretest_runner as pretest
from tools import q15_rti_v20_untouched_test_runner as runner
from test_q15_rti_v20_modeling import _sealed_population
from test_q15_rti_v20_pretest_runner import _settlement_labels


@pytest.fixture(scope="module")
def passing_pretest(tmp_path_factory):
    payload, labels = _sealed_population()
    directory = tmp_path_factory.mktemp("v20-passing-pretest")
    reservation = directory / "pretest-reservation.json"
    result = pretest.run_pretest_once(
        seal=payload,
        reservation_path=reservation,
        confirmation=audit_identity.PRETEST_CONFIRMATION,
        read_settlement_yes_labels=lambda _ids: _settlement_labels(
            payload, labels
        ),
        require_label_evidence=False,
        timestamp="2026-08-01T16:00:00Z",
    )
    assert result["status"] == pretest.PASS_STATUS
    return payload, labels, reservation


def _test_settlements(payload, labels):
    output = {}
    for row in payload["rows"]:
        if row["partition"] != "UNTOUCHED_TEST":
            continue
        survival = labels[int(row["parent_id"])]
        output[int(row["parent_id"])] = (
            survival if row["side"] == "YES" else 1 - survival
        )
    return output


def test_confirmation_is_required_before_test_reservation_or_label_callback(
    tmp_path, passing_pretest,
):
    payload, labels, pretest_reservation = passing_pretest
    reservation = tmp_path / "test-reservation.json"
    called = False

    def callback(_ids):
        nonlocal called
        called = True
        return _test_settlements(payload, labels)

    with pytest.raises(ValueError, match="explicit_one_shot_confirmation"):
        runner.run_untouched_test_once(
            seal=payload,
            pretest_reservation_path=pretest_reservation,
            reservation_path=reservation,
            confirmation="WRONG",
            read_settlement_yes_labels=callback,
            require_label_evidence=False,
        )
    assert called is False
    assert reservation.exists() is False


def test_reserved_interruption_is_permanently_ambiguous_and_never_rereads(
    tmp_path, passing_pretest,
):
    payload, _labels, pretest_reservation = passing_pretest
    reservation = tmp_path / "test-reservation.json"
    calls = 0

    def failing_callback(_ids):
        nonlocal calls
        calls += 1
        raise RuntimeError("simulated settlement interruption")

    with pytest.raises(RuntimeError, match="settlement interruption"):
        runner.run_untouched_test_once(
            seal=payload,
            pretest_reservation_path=pretest_reservation,
            reservation_path=reservation,
            confirmation=audit_identity.UNTOUCHED_TEST_CONFIRMATION,
            read_settlement_yes_labels=failing_callback,
            require_label_evidence=False,
        )
    assert calls == 1
    replay = runner.run_untouched_test_once(
        seal=payload,
        pretest_reservation_path=pretest_reservation,
        reservation_path=reservation,
        confirmation=audit_identity.UNTOUCHED_TEST_CONFIRMATION,
        read_settlement_yes_labels=lambda _ids: pytest.fail("labels reread"),
        require_label_evidence=False,
    )
    assert replay["status"] == "AMBIGUOUS_RESERVED_NO_REREAD"
    assert replay["untouched_test_labels_read_this_call"] is False
    assert calls == 1


def test_untouched_test_scores_once_without_refit_and_reports_all_benchmarks(
    tmp_path, passing_pretest,
):
    payload, labels, pretest_reservation = passing_pretest
    reservation = tmp_path / "test-reservation.json"
    calls = 0

    def callback(ids):
        nonlocal calls
        calls += 1
        assert len(ids) == 210
        return _test_settlements(payload, labels)

    first = runner.run_untouched_test_once(
        seal=payload,
        pretest_reservation_path=pretest_reservation,
        reservation_path=reservation,
        confirmation=audit_identity.UNTOUCHED_TEST_CONFIRMATION,
        read_settlement_yes_labels=callback,
        require_label_evidence=False,
        timestamp="2026-08-01T17:00:00Z",
    )
    assert first["status"] == runner.PASS_STATUS
    result = first["result"]
    assert result["manual_paper_challenger_eligible"] is True
    assert result["model_refit_performed"] is False
    assert result["recalibration_performed"] is False
    assert result["margin_selection_performed"] is False
    assert result["paper_artifact_created"] is False
    report = result["untouched_test_report"]
    assert report["independent_final_historical_confirmation"] is True
    assert report["historical_gate_met"] is True
    for cohort in ("NON_BTC_TRANSFER", "BTC"):
        cohort_report = report["cohorts"][cohort]
        assert cohort_report["gate_met"] is True
        assert set(cohort_report["candidate"]["subgroups"]) == {
            "ASSET", "RTI_SIDE", "DISTANCE_TIER", "VOLATILITY_TIER",
            "MARKET_REGIME", "REVERSAL_RISK", "SETTLEMENT_AVERAGE_RISK",
        }
        assert "all_source_complete_12m_side_follow_control" in cohort_report
        assert "matched_v18_selection" in cohort_report
        assert "matched_v19_selection" in cohort_report
        assert "rejected_trade_counterfactual" in cohort_report
    assert calls == 1

    replay = runner.run_untouched_test_once(
        seal=payload,
        pretest_reservation_path=pretest_reservation,
        reservation_path=reservation,
        confirmation=audit_identity.UNTOUCHED_TEST_CONFIRMATION,
        read_settlement_yes_labels=lambda _ids: pytest.fail("labels reread"),
        require_label_evidence=False,
    )
    assert replay["status"] == "ALREADY_FINALIZED_NO_REREAD"
    assert replay["untouched_test_labels_read_this_call"] is False
    assert calls == 1


def test_semantic_result_tamper_fails_even_with_recomputed_state_hash(
    tmp_path, passing_pretest,
):
    payload, labels, pretest_reservation = passing_pretest
    reservation = tmp_path / "test-reservation.json"
    runner.run_untouched_test_once(
        seal=payload,
        pretest_reservation_path=pretest_reservation,
        reservation_path=reservation,
        confirmation=audit_identity.UNTOUCHED_TEST_CONFIRMATION,
        read_settlement_yes_labels=lambda _ids: _test_settlements(
            payload, labels
        ),
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
    stored.pop("state_sha256")
    stored["state_sha256"] = runner._canonical_sha256(stored)
    result_path.write_text(json.dumps(stored), encoding="utf-8")
    with pytest.raises(ValueError, match="result_invalid"):
        runner.run_untouched_test_once(
            seal=payload,
            pretest_reservation_path=pretest_reservation,
            reservation_path=reservation,
            confirmation=audit_identity.UNTOUCHED_TEST_CONFIRMATION,
            read_settlement_yes_labels=lambda _ids: pytest.fail("labels reread"),
            require_label_evidence=False,
        )
