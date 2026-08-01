from __future__ import annotations

import copy
from datetime import datetime
import inspect
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v14 as v14
from q15_upgrade.strategy_bots import rti_microstructure_v15 as v15
from tools import q15_rti_v15_audit_seal as seal
from tools import q15_rti_v15_pretest as pretest
from tools import q15_rti_v15_pretest_command as pretest_command
from tools import q15_rti_v15_untouched_test as once
from tools import q15_rti_v15_untouched_test_command as test_command
from tools import q15_rti_v15_walk_forward as walk

EASTERN = ZoneInfo("America/New_York")
ASSETS = ("BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP")
NON_BTC = frozenset({"BNB", "DOGE", "ETH", "HYPE", "SOL", "XRP"})


def _ticker(asset: str, close_time: float) -> str:
    timestamp = datetime.fromtimestamp(close_time, EASTERN)
    return f"KX{asset}15M-{timestamp:%y%b%d%H%M}-{timestamp.minute}".upper()


def _rows(windows: int = 60) -> list[dict]:
    output = []
    row_id = 0
    for window in range(windows):
        close = v15.FIRST_ELIGIBLE_CLOSE_TIME + 900.0 * window
        captured = close - 780.0 + 0.25
        for asset_index, asset in enumerate(ASSETS):
            row_id += 1
            output.append({
                "id": row_id,
                "bot_name": "rti_path_13m",
                "record_kind": "RTI_PATH_13M_PROSPECTIVE_EXACT",
                "interval": "13M",
                "ticker": _ticker(asset, close),
                "asset": asset,
                "side": "YES",
                "close_time": close,
                "source_captured_at": captured,
                "evidence_as_of": captured + 0.5,
                "kalshi_microstructure_captured_at": captured,
                "kalshi_microstructure_schema_version": v15.SOURCE_SCHEMA,
                "entry_ask_cents": 50.0,
                "spread_cents": 1.0,
                "threshold_json": {
                    "rti_opposite_ask_cents": 51.0,
                    "rti_market_mid_probability": 0.495,
                },
                "synthetic_signal": int((window + asset_index) % 2 == 0),
            })
    return output


def _patch_feature_projection(monkeypatch, *, spread: float = 1.0) -> None:
    def candidate(row):
        signal = float(row["synthetic_signal"])
        base = [signal, *([0.0] * 19)]
        return {
            "available": True,
            "feature_names": list(v15.FEATURE_NAMES),
            "features": [*base, 0.1, 0.2, 0.3, 0.4, 0.5],
            "market_yes_probability": 0.495,
            "yes_ask_cents": 50.0,
            "no_ask_cents": 51.0,
            "yes_depth_contracts": 100.0,
            "no_depth_contracts": 100.0,
            "yes_depth_available": True,
            "no_depth_available": True,
            "source_path_evidence_sha256": f"{int(row['id']):064x}",
        }

    def control(row):
        signal = float(row["synthetic_signal"])
        return {
            "available": True,
            "feature_names": list(v14.FEATURE_NAMES),
            "features": [signal, *([0.0] * 19)],
        }

    monkeypatch.setattr(seal.v15, "feature_vector", candidate)
    monkeypatch.setattr(seal.v14, "feature_vector", control)


def _patch_models(monkeypatch, *, candidate_strength: float = 0.85, control_strength: float = 0.65) -> None:
    def select(training, config, protocol, cohort):
        return {
            "architecture": "nested_chronological_safe_residual_trust_v1",
            "selected_factor": 1.0,
            "market_fallback_selected": False,
            "outer_validation_labels_used_for_selection": False,
            "calibration_labels_used_for_selection": False,
            "untouched_test_labels_used_for_selection": False,
        }

    def fit(training, config):
        return {"feature_count": len(training[0]["features"]), "rows": len(training)}

    def predict(model, rows, config):
        strength = candidate_strength if int(model["feature_count"]) == 25 else control_strength
        probabilities = [
            strength if float(row["features"][0]) >= 0.5 else 1.0 - strength
            for row in rows
        ]
        return probabilities, [{"out_of_distribution": False} for _ in rows]

    def apply(rows, base, trust):
        return [float(value) for value in base]

    monkeypatch.setattr(walk, "select_residual_trust_factor", select)
    monkeypatch.setattr(walk, "fit_residual_model", fit)
    monkeypatch.setattr(walk, "predict_probabilities", predict)
    monkeypatch.setattr(walk, "apply_residual_trust", apply)
    monkeypatch.setattr(once, "fit_residual_model", fit)
    monkeypatch.setattr(once, "predict_probabilities", predict)
    monkeypatch.setattr(once, "apply_residual_trust", apply)


def _case(
    monkeypatch,
    *,
    spread: float = 1.0,
    candidate_strength: float = 0.85,
    control_strength: float = 0.65,
) -> dict:
    _patch_feature_projection(monkeypatch, spread=spread)
    _patch_models(
        monkeypatch,
        candidate_strength=candidate_strength,
        control_strength=control_strength,
    )
    design, v14_design, charter, protocol, geometry, geometry_sha = seal._load_inputs()
    raw = _rows()
    for row in raw:
        row["spread_cents"] = spread
    audit = seal.build_audit_seal(
        raw,
        cohort="NON_BTC_TRANSFER",
        design=design,
        v14_design=v14_design,
        charter=charter,
        protocol=protocol,
        geometry_artifact=geometry,
        geometry_artifact_file_sha256=geometry_sha,
        generated_at="2026-07-23T07:00:00+00:00",
    )
    selected = [row for row in raw if row["asset"] in NON_BTC]
    projected, failures = seal._project_evidence(selected)
    assert failures == 0
    close_times = sorted({float(row["close_time"]) for row in projected})
    pretest_times = set(close_times[:48])
    test_times = set(close_times[48:])
    labels = {int(row["id"]): int(row["synthetic_signal"]) for row in selected}
    pretest = [
        {**row, "label_yes": labels[int(row["id"])]}
        for row in projected
        if float(row["close_time"]) in pretest_times
    ]
    walk_report = walk.evaluate_walk_forward(
        pretest, cohort="NON_BTC_TRANSFER", design=design, protocol=protocol,
    )
    calibration = walk.evaluate_calibration(
        pretest,
        cohort="NON_BTC_TRANSFER",
        design=design,
        protocol=protocol,
        walk_forward_report=walk_report,
    )
    return {
        "seal": audit,
        "selected": selected,
        "pretest_labels": {int(row["id"]): labels[int(row["id"])] for row in pretest},
        "test_labels": {
            int(row["id"]): labels[int(row["id"])]
            for row in projected
            if float(row["close_time"]) in test_times
        },
        "walk": walk_report,
        "calibration": calibration,
        "design": design,
        "protocol": protocol,
        "reporting": once.load_reporting_protocol(),
    }


def _run(
    case: dict,
    path: Path,
    reader,
    *,
    confirmation: str = once.CONFIRMATION_PHRASE,
    require_label_evidence: bool = False,
):
    return once.run_untouched_test_once(
        seal=case["seal"],
        selected_feature_rows=case["selected"],
        pretest_labels=case["pretest_labels"],
        supplied_walk_forward_report=case["walk"],
        supplied_calibration_report=case["calibration"],
        design=case["design"],
        protocol=case["protocol"],
        reporting_protocol=case["reporting"],
        cohort="NON_BTC_TRANSFER",
        reservation_path=path,
        confirmation=confirmation,
        read_untouched_test_labels=reader,
        require_label_evidence=require_label_evidence,
        timestamp="2026-07-23T07:05:00+00:00",
    )


def test_one_shot_pass_is_append_only_and_second_call_never_reads_labels(monkeypatch, tmp_path):
    case = _case(monkeypatch)
    calls = []

    def reader(ids):
        calls.append(tuple(ids))
        return case["test_labels"]

    path = tmp_path / "reservation.json"
    first = _run(case, path, reader)
    assert first["status"] == once.PASS_STATUS
    assert first["untouched_test_labels_read_this_call"] is True
    assert len(calls) == 1
    assert path.exists()
    assert once.result_path_for(path).exists()
    report = first["result"]["report"]
    assert report["gate_met"] is True
    assert report["rows"] == 72
    assert report["close_windows"] == 12
    assert report["candidate_scores"]["accuracy"] == 1.0
    assert report["candidate_scores"]["brier_score"] < report["v14_scores"]["brier_score"]
    assert report["economics"]["candidate"]["picks"] == 72
    assert report["economics"]["candidate"]["ten_contract_net_pnl_dollars"] > 0.0
    assert set(report["subgroup_reporting"]["subgroups"]) == {
        "asset", "rti_side", "absolute_distance_tier", "realized_volatility_tier",
        "market_regime", "path_depth_agreement", "path_spread_stress_tier",
    }
    assert report["paper_artifact_created"] is False
    assert report["automatic_promotion"] is False
    assert report["real_trading_allowed"] is False

    second = _run(case, path, lambda ids: (_ for _ in ()).throw(AssertionError("rescore")))
    assert second["status"] == "ALREADY_FINALIZED_NO_RESCORE"
    assert second["untouched_test_labels_read_this_call"] is False
    assert len(calls) == 1


def test_untouched_test_required_authoritative_evidence_is_bound(
    monkeypatch, tmp_path,
):
    case = _case(monkeypatch)
    path = tmp_path / "verified-test-reservation.json"
    first = _run(
        case,
        path,
        lambda ids: _verified_labels(case, case["test_labels"]),
        require_label_evidence=True,
    )
    result = first["result"]
    assert result["label_read_evidence"]["verification_status"] == (
        pretest_command.label_evidence.PASS_STATUS
    )
    assert result["label_read_evidence_sha256"] == (
        result["label_read_evidence"]["evidence_sha256"]
    )


def test_untouched_test_required_authoritative_evidence_rejects_plain_labels(
    monkeypatch, tmp_path,
):
    case = _case(monkeypatch)
    path = tmp_path / "missing-test-evidence-reservation.json"
    with pytest.raises(ValueError, match="label_evidence_required"):
        _run(
            case,
            path,
            lambda ids: case["test_labels"],
            require_label_evidence=True,
        )
    assert path.exists()
    assert not once.result_path_for(path).exists()


def test_missing_confirmation_fails_before_reservation_or_label_read(monkeypatch, tmp_path):
    case = _case(monkeypatch)
    path = tmp_path / "reservation.json"
    called = False

    def reader(ids):
        nonlocal called
        called = True
        return case["test_labels"]

    with pytest.raises(ValueError, match="explicit_one_shot_confirmation_required"):
        _run(case, path, reader, confirmation="YES")
    assert called is False
    assert not path.exists()


def test_crash_after_reservation_permanently_blocks_rescore(monkeypatch, tmp_path):
    case = _case(monkeypatch)
    path = tmp_path / "reservation.json"

    def crash(ids):
        raise RuntimeError("synthetic settlement read failure")

    with pytest.raises(RuntimeError, match="synthetic settlement"):
        _run(case, path, crash)
    assert path.exists()
    assert not once.result_path_for(path).exists()
    calls = 0

    def forbidden(ids):
        nonlocal calls
        calls += 1
        return case["test_labels"]

    recovered = _run(case, path, forbidden)
    assert recovered["status"] == "AMBIGUOUS_RESERVED_NO_RESCORE"
    assert recovered["untouched_test_labels_read_this_call"] is False
    assert calls == 0


def test_feature_or_prior_report_tamper_fails_before_reservation(monkeypatch, tmp_path):
    case = _case(monkeypatch)
    path = tmp_path / "reservation.json"
    tampered_rows = copy.deepcopy(case["selected"])
    tampered_rows[-1]["synthetic_signal"] = 1 - int(tampered_rows[-1]["synthetic_signal"])
    bad = dict(case)
    bad["selected"] = tampered_rows
    with pytest.raises(ValueError, match="selected_feature_evidence_mismatch"):
        _run(bad, path, lambda ids: case["test_labels"])
    assert not path.exists()

    tampered_report = copy.deepcopy(case["calibration"])
    tampered_report["candidate_scores"]["brier_score"] = 0.0
    bad = dict(case)
    bad["calibration"] = tampered_report
    with pytest.raises(ValueError, match="calibration_gate_or_report_mismatch"):
        _run(bad, path, lambda ids: case["test_labels"])
    assert not path.exists()


def test_economic_gate_rejects_when_spread_makes_every_quote_ineligible(monkeypatch, tmp_path):
    case = _case(monkeypatch, spread=2.0)
    outcome = _run(case, tmp_path / "reservation.json", lambda ids: case["test_labels"])
    report = outcome["result"]["report"]
    assert outcome["status"] == once.REJECT_STATUS
    assert report["candidate_scores"]["accuracy"] == 1.0
    assert report["economics"]["candidate"]["picks"] == 0
    assert report["gate_checks"]["minimum_simulated_picks"] is False
    assert report["gate_checks"]["positive_fee_slippage_adjusted_pnl"] is False
    assert report["failure_result"] == "NO_V15_ARTIFACT_OR_PAPER_CHALLENGER"


def test_rehashed_reservation_safety_tamper_blocks_callback(monkeypatch, tmp_path):
    case = _case(monkeypatch)
    path = tmp_path / "reservation.json"

    def crash(ids):
        raise RuntimeError("leave reservation")

    with pytest.raises(RuntimeError, match="leave reservation"):
        _run(case, path, crash)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["automatic_promotion"] = True
    path.write_text(
        json.dumps(once._sealed(payload), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    called = False

    def forbidden(ids):
        nonlocal called
        called = True
        return case["test_labels"]

    with pytest.raises(ValueError, match="reservation_safety_invalid"):
        _run(case, path, forbidden)
    assert called is False


def test_reporting_protocol_tamper_fails_before_callback(monkeypatch, tmp_path):
    case = _case(monkeypatch)
    tampered = copy.deepcopy(case["reporting"])
    tampered["dimensions"]["path_depth_agreement"]["bins"][1][
        "maximum_exclusive"
    ] = 0.5
    case["reporting"] = tampered
    called = False

    def forbidden(ids):
        nonlocal called
        called = True
        return case["test_labels"]

    with pytest.raises(ValueError, match="reporting_protocol_identity_mismatch"):
        _run(case, tmp_path / "reservation.json", forbidden)
    assert called is False


def test_rehashed_final_result_safety_tamper_never_rescores(monkeypatch, tmp_path):
    case = _case(monkeypatch)
    path = tmp_path / "reservation.json"
    _run(case, path, lambda ids: case["test_labels"])
    result_path = once.result_path_for(path)
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    payload["automatic_promotion"] = True
    result_path.write_text(
        json.dumps(once._sealed(payload), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    called = False

    def forbidden(ids):
        nonlocal called
        called = True
        return case["test_labels"]

    with pytest.raises(ValueError, match="final_result_invalid"):
        _run(case, path, forbidden)
    assert called is False


def test_module_has_no_database_delivery_promotion_or_order_capability():
    parameters = inspect.signature(once.run_untouched_test_once).parameters
    assert "read_untouched_test_labels" in parameters
    source = Path(once.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "import sqlite3", "from sqlite3", "V3Telegram", "place_order(",
        "send_message(", "create_paper_artifact(", "official_result",
    ):
        assert forbidden not in source


def _run_pretest(
    case: dict,
    path: Path,
    reader,
    *,
    confirmation: str = pretest.CONFIRMATION_PHRASE,
    require_label_evidence: bool = False,
):
    return pretest.run_pretest_once(
        seal=case["seal"],
        selected_feature_rows=case["selected"],
        design=case["design"],
        protocol=case["protocol"],
        cohort="NON_BTC_TRANSFER",
        reservation_path=path,
        confirmation=confirmation,
        read_pretest_labels=reader,
        require_label_evidence=require_label_evidence,
        timestamp="2026-07-23T07:03:00+00:00",
    )


def _verified_labels(case: dict, labels: dict[int, int]):
    by_id = {int(row["id"]): row for row in case["selected"]}
    contracts = []
    for row_id, label in sorted(labels.items()):
        contracts.append({
            "ticker": str(by_id[int(row_id)]["ticker"]),
            "row_ids": [int(row_id)],
            "result_yes": int(label),
            "status": "finalized",
            "expected_close_time": float(by_id[int(row_id)]["close_time"]),
            "kalshi_close_time": float(by_id[int(row_id)]["close_time"]),
            "kalshi_settled_time": float(by_id[int(row_id)]["close_time"]) + 1,
            "kalshi_expiration_time": None,
            "local_cache_status": "MATCHED",
            "local_resolved_row_count": 1,
            "local_unresolved_row_count": 0,
            "local_invalid_row_count": 0,
            "local_resolved_labels_match_api": True,
            "fetched_at": "2026-07-23T07:02:00+00:00",
        })
    ids = tuple(sorted(int(value) for value in labels))
    pairs = sorted(
        [int(row_id), int(label)]
        for row_id, label in labels.items()
    )
    evidence = pretest_command.label_evidence.seal_evidence({
        "evidence_version": (
            pretest_command.label_evidence.EVIDENCE_VERSION
        ),
        "verification_status": (
            pretest_command.label_evidence.PASS_STATUS
        ),
        "source_id": pretest_command.label_evidence.SOURCE_ID,
        "source_base_url": "https://kalshi.invalid/v2",
        "verification_started_at": "2026-07-23T07:02:00+00:00",
        "verification_completed_at": "2026-07-23T07:02:01+00:00",
        "row_count": len(ids),
        "unique_contracts": len(contracts),
        "requested_row_ids_sha256": (
            pretest_command.label_evidence.canonical_sha256(ids)
        ),
        "labels_sha256": (
            pretest_command.label_evidence.canonical_sha256(pairs)
        ),
        "requested_contracts_sha256": (
            pretest_command.label_evidence.canonical_sha256(
                tuple(sorted(item["ticker"] for item in contracts))
            )
        ),
        "contracts": contracts,
    })
    return pretest_command.label_evidence.VerifiedLabelMapping(
        labels,
        evidence,
    )


def test_pretest_is_append_only_and_keeps_test_labels_sealed(
    monkeypatch, tmp_path,
):
    case = _case(monkeypatch)
    calls = []

    def reader(ids):
        assert path.exists()
        calls.append(tuple(ids))
        return case["pretest_labels"]

    path = tmp_path / "pretest-reservation.json"
    first = _run_pretest(case, path, reader)
    assert first["status"] == pretest.PASS_STATUS
    assert first["pretest_labels_read_this_call"] is True
    assert len(calls) == 1
    assert set(calls[0]) == set(case["pretest_labels"])
    assert set(calls[0]).isdisjoint(case["test_labels"])
    result = first["result"]
    assert result["walk_forward_report"]["gate_met"] is True
    assert result["calibration_report"]["gate_met"] is True
    assert len(result["pretest_label_rows"]) == 288
    assert {
        int(item["id"]) for item in result["pretest_label_rows"]
    } == set(case["pretest_labels"])
    assert {
        int(item["id"]) for item in result["pretest_label_rows"]
    }.isdisjoint(case["test_labels"])
    assert result["untouched_test_labels_read"] is False
    assert result["untouched_test_scoring_performed"] is False
    assert result["paper_artifact_created"] is False
    assert result["notification_eligible"] is False
    assert result["automatic_promotion"] is False
    assert result["real_trading_allowed"] is False

    second = _run_pretest(
        case,
        path,
        lambda ids: (_ for _ in ()).throw(
            AssertionError("pretest labels reread")
        ),
    )
    assert second["status"] == "ALREADY_FINALIZED_NO_REREAD"
    assert second["pretest_labels_read_this_call"] is False
    assert len(calls) == 1


def test_pretest_required_authoritative_evidence_is_bound_and_tamper_evident(
    monkeypatch, tmp_path,
):
    case = _case(monkeypatch)
    path = tmp_path / "verified-pretest-reservation.json"
    first = _run_pretest(
        case,
        path,
        lambda ids: _verified_labels(case, case["pretest_labels"]),
        require_label_evidence=True,
    )
    result = first["result"]
    assert result["label_read_evidence"]["verification_status"] == (
        pretest_command.label_evidence.PASS_STATUS
    )
    assert result["label_read_evidence_sha256"] == (
        result["label_read_evidence"]["evidence_sha256"]
    )

    result_path = pretest.result_path_for(path)
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    payload["label_read_evidence"]["contracts"][0]["result_yes"] ^= 1
    result_path.write_text(
        json.dumps(pretest._sealed(payload), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="final_result_invalid"):
        _run_pretest(
            case,
            path,
            lambda ids: (_ for _ in ()).throw(
                AssertionError("verified labels reread")
            ),
            require_label_evidence=True,
        )


def test_pretest_required_authoritative_evidence_rejects_plain_labels(
    monkeypatch, tmp_path,
):
    case = _case(monkeypatch)
    path = tmp_path / "missing-evidence-reservation.json"
    with pytest.raises(ValueError, match="label_evidence_required"):
        _run_pretest(
            case,
            path,
            lambda ids: case["pretest_labels"],
            require_label_evidence=True,
        )
    assert path.exists()
    assert not pretest.result_path_for(path).exists()


def test_pretest_requires_confirmation_before_reservation_or_callback(
    monkeypatch, tmp_path,
):
    case = _case(monkeypatch)
    path = tmp_path / "pretest-reservation.json"
    called = False

    def forbidden(ids):
        nonlocal called
        called = True
        return case["pretest_labels"]

    with pytest.raises(
        ValueError, match="explicit_one_shot_confirmation_required",
    ):
        _run_pretest(case, path, forbidden, confirmation="YES")
    assert called is False
    assert not path.exists()


def test_pretest_crash_after_reservation_permanently_blocks_reread(
    monkeypatch, tmp_path,
):
    case = _case(monkeypatch)
    path = tmp_path / "pretest-reservation.json"

    def crash(ids):
        raise RuntimeError("synthetic pretest label failure")

    with pytest.raises(RuntimeError, match="synthetic pretest"):
        _run_pretest(case, path, crash)
    assert path.exists()
    assert not pretest.result_path_for(path).exists()
    calls = 0

    def forbidden(ids):
        nonlocal calls
        calls += 1
        return case["pretest_labels"]

    recovered = _run_pretest(case, path, forbidden)
    assert recovered["status"] == "AMBIGUOUS_RESERVED_NO_REREAD"
    assert recovered["pretest_labels_read_this_call"] is False
    assert calls == 0


def test_pretest_walk_failure_never_runs_calibration_or_opens_test(
    monkeypatch, tmp_path,
):
    case = _case(monkeypatch)
    rejected = copy.deepcopy(case["walk"])
    rejected["gate_met"] = False
    rejected["failure_result"] = "NO_V15_ARTIFACT_OR_PAPER_CHALLENGER"
    monkeypatch.setattr(
        walk,
        "evaluate_walk_forward",
        lambda *args, **kwargs: copy.deepcopy(rejected),
    )
    outcome = _run_pretest(
        case,
        tmp_path / "pretest-reservation.json",
        lambda ids: case["pretest_labels"],
    )
    assert outcome["status"] == pretest.WALK_REJECT_STATUS
    result = outcome["result"]
    assert result["walk_forward_report"]["gate_met"] is False
    assert result["calibration_report"] is None
    assert result["untouched_test_labels_read"] is False
    assert result["paper_artifact_created"] is False


def test_pretest_calibration_failure_keeps_test_sealed(
    monkeypatch, tmp_path,
):
    case = _case(monkeypatch)
    rejected = copy.deepcopy(case["calibration"])
    rejected["gate_met"] = False
    rejected["failure_result"] = "NO_V15_ARTIFACT_OR_PAPER_CHALLENGER"
    monkeypatch.setattr(
        walk,
        "evaluate_calibration",
        lambda *args, **kwargs: copy.deepcopy(rejected),
    )
    outcome = _run_pretest(
        case,
        tmp_path / "pretest-reservation.json",
        lambda ids: case["pretest_labels"],
    )
    assert outcome["status"] == pretest.CALIBRATION_REJECT_STATUS
    assert outcome["result"]["calibration_report"]["gate_met"] is False
    assert outcome["result"]["untouched_test_labels_read"] is False
    assert outcome["result"]["paper_artifact_created"] is False


def test_pretest_rehashed_safety_tamper_blocks_callback(
    monkeypatch, tmp_path,
):
    case = _case(monkeypatch)
    path = tmp_path / "pretest-reservation.json"

    with pytest.raises(RuntimeError):
        _run_pretest(
            case,
            path,
            lambda ids: (_ for _ in ()).throw(
                RuntimeError("leave reservation")
            ),
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["untouched_test_labels_read"] = True
    path.write_text(
        json.dumps(pretest._sealed(payload), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    called = False

    def forbidden(ids):
        nonlocal called
        called = True
        return case["pretest_labels"]

    with pytest.raises(ValueError, match="reservation_safety_invalid"):
        _run_pretest(case, path, forbidden)
    assert called is False


def test_pretest_rehashed_final_label_tamper_never_rereads(
    monkeypatch, tmp_path,
):
    case = _case(monkeypatch)
    path = tmp_path / "pretest-reservation.json"
    _run_pretest(case, path, lambda ids: case["pretest_labels"])
    result_path = pretest.result_path_for(path)
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    payload["pretest_label_rows"][0]["label_yes"] = (
        1 - int(payload["pretest_label_rows"][0]["label_yes"])
    )
    result_path.write_text(
        json.dumps(pretest._sealed(payload), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    called = False

    def forbidden(ids):
        nonlocal called
        called = True
        return case["pretest_labels"]

    with pytest.raises(ValueError, match="final_result_invalid"):
        _run_pretest(case, path, forbidden)
    assert called is False


def test_pretest_module_has_no_database_delivery_or_order_capability():
    parameters = inspect.signature(pretest.run_pretest_once).parameters
    assert "read_pretest_labels" in parameters
    source = Path(pretest.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "import sqlite3",
        "from sqlite3",
        "V3Telegram",
        "place_order(",
        "send_message(",
        "create_paper_artifact(",
        "official_result",
    ):
        assert forbidden not in source


def test_pretest_sqlite_reader_queries_only_requested_resolved_ids(tmp_path):
    import sqlite3

    database = tmp_path / "labels.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE strategy_bot_decisions ("
            "id INTEGER PRIMARY KEY, ticker TEXT, asset TEXT, "
            "close_time REAL, official_result TEXT, resolved_at REAL)"
        )
        connection.executemany(
            "INSERT INTO strategy_bot_decisions VALUES (?, ?, ?, ?, ?, ?)",
            (
                (1, "KXONE", "ETH", 50.0, "YES", 100.0),
                (2, "KXTWO", "SOL", 50.0, "NO", 100.0),
                (3, "KXTHREE", "BTC", 50.0, "YES", 100.0),
                (4, "KXFOUR", "XRP", 50.0, None, None),
                (5, "KXFIVE", "BNB", 100.0, "YES", 99.0),
            ),
        )
    expected = [
        {"id": 1, "ticker": "KXONE", "asset": "ETH", "close_time": 50.0},
        {"id": 2, "ticker": "KXTWO", "asset": "SOL", "close_time": 50.0},
        {"id": 3, "ticker": "KXTHREE", "asset": "BTC", "close_time": 50.0},
        {"id": 4, "ticker": "KXFOUR", "asset": "XRP", "close_time": 50.0},
        {"id": 5, "ticker": "KXFIVE", "asset": "BNB", "close_time": 100.0},
    ]
    reader = pretest_command.SQLitePretestLabelReader(
        database,
        expected_rows=expected,
    )
    assert reader((2, 1)) == {1: 1, 2: 0}
    assert 3 not in reader((1,))
    with pytest.raises(ValueError, match="not_in_sealed_evidence"):
        reader((99,))
    with pytest.raises(ValueError, match="unresolved_or_invalid"):
        reader((4,))
    with pytest.raises(ValueError, match="unresolved_or_invalid"):
        reader((5,))
    with pytest.raises(ValueError, match="label_ids_invalid"):
        reader((1, 1))
    wrong_contract = copy.deepcopy(expected)
    wrong_contract[0]["ticker"] = "KXWRONG"
    mismatched = pretest_command.SQLitePretestLabelReader(
        database,
        expected_rows=wrong_contract,
    )
    with pytest.raises(ValueError, match="contract_mismatch"):
        mismatched((1,))
    with pytest.raises(ValueError, match="expected_contracts_invalid"):
        pretest_command.SQLitePretestLabelReader(
            database,
            expected_rows=[expected[0], expected[0]],
        )


def test_pretest_kalshi_verified_reader_fetches_only_authorized_contracts(
    tmp_path,
):
    import sqlite3

    database = tmp_path / "verified-labels.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE strategy_bot_decisions ("
            "id INTEGER PRIMARY KEY, ticker TEXT, asset TEXT, "
            "close_time REAL, official_result TEXT, resolved_at REAL)"
        )
        connection.executemany(
            "INSERT INTO strategy_bot_decisions VALUES (?, ?, ?, ?, ?, ?)",
            (
                (1, "KXONE", "ETH", 50.0, "YES", 100.0),
                (2, "KXTWO", "SOL", 50.0, "NO", 100.0),
                (3, "KXSEALEDTEST", "XRP", 50.0, "YES", 100.0),
            ),
        )
    expected = [
        {"id": 1, "ticker": "KXONE", "asset": "ETH", "close_time": 50.0},
        {"id": 2, "ticker": "KXTWO", "asset": "SOL", "close_time": 50.0},
        {
            "id": 3,
            "ticker": "KXSEALEDTEST",
            "asset": "XRP",
            "close_time": 50.0,
        },
    ]
    calls = []

    def get_market(ticker):
        calls.append(ticker)
        return {
            "ticker": ticker,
            "status": "finalized",
            "result": "YES" if ticker == "KXONE" else "NO",
            "close_time": "1970-01-01T00:00:50Z",
            "settled_time": "1970-01-01T00:01:40Z",
        }

    reader = pretest_command.KalshiVerifiedSQLiteLabelReader(
        database,
        expected_rows=expected,
        get_market=get_market,
        source_base_url="https://kalshi.invalid/v2",
    )
    labels = reader((2, 1))
    assert labels == {1: 1, 2: 0}
    assert calls == ["KXONE", "KXTWO"]
    assert "KXSEALEDTEST" not in calls
    evidence = pretest_command.label_evidence.validate_label_evidence(
        labels,
        labels,
        (1, 2),
        required=True,
        stage="pretest",
    )
    assert evidence["verification_status"] == (
        pretest_command.label_evidence.PASS_STATUS
    )
    assert evidence["unique_contracts"] == 2
    assert all(
        contract["local_cache_status"] == "MATCHED"
        for contract in evidence["contracts"]
    )
    tampered = copy.deepcopy(evidence)
    tampered["contracts"][0]["status"] = "settled"
    tampered = pretest_command.label_evidence.seal_evidence(tampered)
    invalid = pretest_command.label_evidence.VerifiedLabelMapping(
        labels,
        tampered,
    )
    with pytest.raises(ValueError, match="label_evidence_invalid"):
        pretest_command.label_evidence.validate_label_evidence(
            invalid,
            invalid,
            (1, 2),
            required=True,
            stage="pretest",
        )


@pytest.mark.parametrize(
    ("market_patch", "error"),
    (
        ({"result": "NO"}, "kalshi_label_mismatch"),
        ({"ticker": "KXWRONG"}, "kalshi_contract_mismatch"),
        ({"status": "closed"}, "kalshi_not_final"),
        ({"status": "settled"}, "kalshi_not_final"),
        (
            {"close_time": "1970-01-01T00:00:55Z"},
            "kalshi_close_time_mismatch",
        ),
    ),
)
def test_pretest_kalshi_verified_reader_fails_closed(
    tmp_path, market_patch, error,
):
    import sqlite3

    database = tmp_path / f"{error}.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE strategy_bot_decisions ("
            "id INTEGER PRIMARY KEY, ticker TEXT, asset TEXT, "
            "close_time REAL, official_result TEXT, resolved_at REAL)"
        )
        connection.execute(
            "INSERT INTO strategy_bot_decisions VALUES (?, ?, ?, ?, ?, ?)",
            (1, "KXONE", "ETH", 50.0, "YES", 100.0),
        )
    market = {
        "ticker": "KXONE",
        "status": "finalized",
        "result": "YES",
        "close_time": "1970-01-01T00:00:50Z",
    }
    market.update(market_patch)
    reader = pretest_command.KalshiVerifiedSQLiteLabelReader(
        database,
        expected_rows=[
            {
                "id": 1,
                "ticker": "KXONE",
                "asset": "ETH",
                "close_time": 50.0,
            },
        ],
        get_market=lambda ticker: market,
    )
    with pytest.raises(ValueError, match=error):
        reader((1,))


def test_pretest_kalshi_verified_reader_uses_finalized_api_when_local_unresolved(
    tmp_path,
):
    import sqlite3

    database = tmp_path / "api-authority.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE strategy_bot_decisions ("
            "id INTEGER PRIMARY KEY, ticker TEXT, asset TEXT, "
            "close_time REAL, official_result TEXT, resolved_at REAL)"
        )
        connection.execute(
            "INSERT INTO strategy_bot_decisions VALUES (?, ?, ?, ?, ?, ?)",
            (1, "KXONE", "XRP", 50.0, None, None),
        )
    reader = pretest_command.KalshiVerifiedSQLiteLabelReader(
        database,
        expected_rows=[
            {
                "id": 1,
                "ticker": "KXONE",
                "asset": "XRP",
                "close_time": 50.0,
            },
        ],
        get_market=lambda ticker: {
            "ticker": ticker,
            "status": "finalized",
            "result": "YES",
            "close_time": "1970-01-01T00:00:50Z",
            "settlement_ts": "1970-01-01T00:01:40Z",
        },
    )
    labels = reader((1,))
    assert labels == {1: 1}
    evidence = pretest_command.label_evidence.validate_label_evidence(
        labels,
        labels,
        (1,),
        required=True,
        stage="pretest",
    )
    assert evidence["contracts"][0]["local_cache_status"] == (
        "UNRESOLVED_API_AUTHORITY"
    )


def test_pretest_kalshi_verified_reader_uses_api_settlement_time_when_local_is_early(
    tmp_path,
):
    import sqlite3

    database = tmp_path / "early-local-time.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE strategy_bot_decisions ("
            "id INTEGER PRIMARY KEY, ticker TEXT, asset TEXT, "
            "close_time REAL, official_result TEXT, resolved_at REAL)"
        )
        connection.execute(
            "INSERT INTO strategy_bot_decisions VALUES (?, ?, ?, ?, ?, ?)",
            (1, "KXONE", "BNB", 50.0, "YES", 48.0),
        )
    reader = pretest_command.KalshiVerifiedSQLiteLabelReader(
        database,
        expected_rows=[
            {
                "id": 1,
                "ticker": "KXONE",
                "asset": "BNB",
                "close_time": 50.0,
            },
        ],
        get_market=lambda ticker: {
            "ticker": ticker,
            "status": "finalized",
            "result": "YES",
            "close_time": "1970-01-01T00:00:50Z",
            "settlement_ts": "1970-01-01T00:01:40Z",
        },
    )
    labels = reader((1,))
    evidence = pretest_command.label_evidence.validate_label_evidence(
        labels,
        labels,
        (1,),
        required=True,
        stage="pretest",
    )
    contract = evidence["contracts"][0]
    assert contract["local_cache_status"] == (
        "DEGRADED_LOCAL_CACHE_API_AUTHORITY"
    )
    assert contract["local_invalid_row_count"] == 1


def test_pretest_command_reconstructs_only_the_sealed_earliest_rows(
    monkeypatch,
):
    case = _case(monkeypatch)
    reconstructed = pretest_command.select_sealed_feature_rows(
        _rows(),
        case["seal"],
    )
    assert {int(row["id"]) for row in reconstructed} == {
        int(row["id"]) for row in case["selected"]
    }


def test_untouched_command_chains_only_from_passing_pretest_and_test_ids(
    monkeypatch, tmp_path,
):
    case = _case(monkeypatch)
    pretest_path = tmp_path / "pretest-reservation.json"
    _run_pretest(
        case,
        pretest_path,
        lambda ids: case["pretest_labels"],
    )
    test_path = tmp_path / "test-reservation.json"
    calls = []

    def reader(ids):
        assert test_path.exists()
        calls.append(tuple(ids))
        return case["test_labels"]

    first = test_command.run_verified_untouched_test_once(
        pretest_reservation_path=pretest_path,
        test_reservation_path=test_path,
        seal=case["seal"],
        selected_feature_rows=case["selected"],
        design=case["design"],
        protocol=case["protocol"],
        reporting_protocol=case["reporting"],
        cohort="NON_BTC_TRANSFER",
        confirmation=once.CONFIRMATION_PHRASE,
        read_untouched_test_labels=reader,
    )
    assert first["status"] == once.PASS_STATUS
    assert len(calls) == 1
    assert set(calls[0]) == set(case["test_labels"])
    assert set(calls[0]).isdisjoint(case["pretest_labels"])

    second = test_command.run_verified_untouched_test_once(
        pretest_reservation_path=pretest_path,
        test_reservation_path=test_path,
        seal=case["seal"],
        selected_feature_rows=case["selected"],
        design=case["design"],
        protocol=case["protocol"],
        reporting_protocol=case["reporting"],
        cohort="NON_BTC_TRANSFER",
        confirmation=once.CONFIRMATION_PHRASE,
        read_untouched_test_labels=lambda ids: (
            _ for _ in ()
        ).throw(AssertionError("test labels reread")),
    )
    assert second["status"] == "ALREADY_FINALIZED_NO_RESCORE"
    assert second["untouched_test_labels_read_this_call"] is False
    assert len(calls) == 1


def test_untouched_command_rejects_failed_pretest_before_test_callback(
    monkeypatch, tmp_path,
):
    case = _case(monkeypatch)
    rejected = copy.deepcopy(case["calibration"])
    rejected["gate_met"] = False
    rejected["failure_result"] = "NO_V15_ARTIFACT_OR_PAPER_CHALLENGER"
    monkeypatch.setattr(
        walk,
        "evaluate_calibration",
        lambda *args, **kwargs: copy.deepcopy(rejected),
    )
    pretest_path = tmp_path / "pretest-reservation.json"
    pretest_outcome = _run_pretest(
        case,
        pretest_path,
        lambda ids: case["pretest_labels"],
    )
    assert pretest_outcome["status"] == pretest.CALIBRATION_REJECT_STATUS
    called = False

    def forbidden(ids):
        nonlocal called
        called = True
        return case["test_labels"]

    with pytest.raises(ValueError, match="pretest_gates_not_passed"):
        test_command.run_verified_untouched_test_once(
            pretest_reservation_path=pretest_path,
            test_reservation_path=tmp_path / "test-reservation.json",
            seal=case["seal"],
            selected_feature_rows=case["selected"],
            design=case["design"],
            protocol=case["protocol"],
            reporting_protocol=case["reporting"],
            cohort="NON_BTC_TRANSFER",
            confirmation=once.CONFIRMATION_PHRASE,
            read_untouched_test_labels=forbidden,
        )
    assert called is False


def test_untouched_command_has_no_delivery_promotion_or_order_capability():
    source = Path(test_command.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "V3Telegram",
        "place_order(",
        "send_message(",
        "create_paper_artifact(",
    ):
        assert forbidden not in source
