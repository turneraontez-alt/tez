from __future__ import annotations

from copy import deepcopy

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v18_audit_identity as audit_identity
from q15_upgrade.strategy_bots import rti_microstructure_v18_identity as identity
from tools import q15_rti_v18_readiness as readiness
from tools import q15_rti_v18_prospective_seal as seal
from tools.q15_rti_microstructure_preregister import design_fingerprint


ASSETS = ("BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP")


def _windows(count: int = 160) -> dict[float, list[dict]]:
    output = {}
    row_id = 1
    for index in range(count):
        close = identity.FIRST_ELIGIBLE_CLOSE_TIME + index * 900.0
        rows = []
        for asset in ASSETS:
            candidate = bool(index % 5 == 0 and asset == "ETH")
            rows.append({
                "id": row_id,
                "asset": asset,
                "close_time": close,
                "candidate": candidate,
                "control": asset != "BTC",
            })
            row_id += 1
        output[close] = rows
    return output


def _patch_selection(monkeypatch, windows):
    monkeypatch.setattr(seal, "_complete_windows", lambda rows: windows)
    monkeypatch.setattr(
        seal.v18, "evaluate_row",
        lambda row: {"eligible": bool(row.get("candidate"))},
    )
    monkeypatch.setattr(
        seal.v18, "evaluate_strict_control_row",
        lambda row: {"eligible": bool(row.get("control"))},
    )


def test_contract_frozen_before_prospective_outcomes():
    contract = seal.load_contract()
    assert design_fingerprint(contract) == audit_identity.AUDIT_CONTRACT_SHA256
    assert contract["outcome_labels_used_to_create_contract"] is False
    assert contract["prospective_resolution_status_inspected_to_create_contract"] is False
    assert contract["result_policy"]["notifications_allowed_by_this_command"] is False
    assert contract["result_policy"]["real_trading_allowed"] is False


def test_prefix_is_earliest_and_never_splits_same_close(monkeypatch):
    windows = _windows()
    _patch_selection(monkeypatch, windows)
    selected = seal.select_prefix([])
    assert selected["status"] == seal.READY_STATUS
    assert len(selected["selected_close_times"]) == 150
    assert len(selected["candidate_rows"]) == 30
    assert len(selected["control_rows"]) == 150 * 6
    assert len(selected["source_rows"]) == 150 * 7
    assert selected["selected_close_times"][-1] == sorted(windows)[149]


def test_not_ready_never_creates_partial_selection(monkeypatch):
    windows = _windows(149)
    _patch_selection(monkeypatch, windows)
    selected = seal.select_prefix([])
    assert selected["status"] == seal.NOT_READY_STATUS
    assert selected["future_complete_close_windows"] == 149
    assert selected["candidate_picks"] == 30
    assert selected["complete_close_windows_remaining"] == 1
    with pytest.raises(ValueError, match="population_not_ready"):
        seal.build_seal([])


def test_complete_windows_use_v18_source_quality_not_v17_model_features(monkeypatch):
    windows = _windows(2)
    rows = [row for window in windows.values() for row in window]
    for row in rows:
        row.update({
            "bot_name": "rti_path_13m",
            "interval": "13M",
            "record_kind": "RTI_PATH_13M_PROSPECTIVE_EXACT",
            "source_complete": True,
        })
    rows[-1]["source_complete"] = False
    monkeypatch.setattr(
        seal, "validate_exact_contract_identity", lambda row: {"valid": True},
    )
    monkeypatch.setattr(
        seal.v18, "evaluate_source_row",
        lambda row: {"available": bool(row["source_complete"])},
    )
    complete = seal._complete_windows(rows)
    assert tuple(complete) == (identity.FIRST_ELIGIBLE_CLOSE_TIME,)


def test_readiness_reports_outcome_blind_source_and_rejection_health(monkeypatch):
    windows = _windows(2)
    rows = [row for window in windows.values() for row in window]
    complete = {close: list(window) for close, window in windows.items()}
    monkeypatch.setattr(readiness, "_complete_windows", lambda values: complete)

    def source(row):
        return {
            "available": True,
            "failures": [],
            "evidence": {
                "exact_timing_offset_seconds": 0.2,
                "evaluation_delay_seconds": 0.1,
                "path_max_receive_age_seconds": 0.3,
                "path_decision_age_seconds": 0.4,
                "quote_age_seconds": 0.5,
            },
        }

    monkeypatch.setattr(readiness.v18, "evaluate_source_row", source)
    monkeypatch.setattr(
        readiness.v18,
        "evaluate_strict_control_row",
        lambda row: {"eligible": row["asset"] == "ETH"},
    )
    monkeypatch.setattr(
        readiness.v18,
        "evaluate_row",
        lambda row: {
            "eligible": bool(row.get("candidate")),
            "decision": "YES",
            "failures": [] if row.get("candidate") else ["STRICT_CONTROL_NOT_PASSED"],
            "feature_evidence_sha256": str(row["id"]),
        },
    )
    report = readiness.build_readiness(rows)
    assert report["successor_audit_complete_close_windows"] == 2
    assert report["eligible_picks"] == 1
    assert report["strict_control_picks"] == 2
    assert report["candidate_failure_counts"]["STRICT_CONTROL_NOT_PASSED"] == 11
    assert report["source_health"]["complete_source_quality_close_windows"] == 2
    assert report["source_health"]["source_quality_incomplete_close_windows"] == 0
    assert report["source_health"]["maximum_quote_age_seconds"] == 0.5
    assert report["outcome_labels_read"] is False


def test_build_and_reconstruct_bind_candidate_control_and_source(monkeypatch):
    windows = _windows()
    _patch_selection(monkeypatch, windows)

    def project(rows):
        return [{
            "id": int(row["id"]),
            "asset": row["asset"],
            "close_time": float(row["close_time"]),
            "candidate": bool(row.get("candidate")),
            "control": bool(row.get("control")),
            "sim_full_fill_supported": True,
        } for row in rows]

    monkeypatch.setattr(seal, "_project", project)
    artifact = seal.build_seal([], generated_at="2026-08-02T00:00:00Z")
    seal.validate_seal(artifact)
    assert artifact["selected_complete_close_windows"] == 150
    assert artifact["selected_candidate_picks"] == 30
    assert artifact["selected_control_picks"] == 900
    reconstructed = seal.reconstruct_examples([], artifact)
    assert len(reconstructed["candidate"]) == 30
    assert len(reconstructed["control"]) == 900
    assert len(reconstructed["source"]) == 1050


def test_projection_uses_v18_source_evidence_and_numeric_safe_loader_flags(monkeypatch):
    row = {
        "id": 1,
        "ticker": "KXETH15M-26AUG010500-00",
        "asset": "ETH",
        "side": "YES",
        "close_time": identity.FIRST_ELIGIBLE_CLOSE_TIME,
        "source_captured_at": identity.FIRST_ELIGIBLE_CLOSE_TIME - 780.0,
        "evidence_as_of": identity.FIRST_ELIGIBLE_CLOSE_TIME - 779.9,
        "entry_ask_cents": 58.0,
        "spread_cents": 1.0,
        "depth_contracts": 20.0,
        "threshold_json": {
            "sim_full_fill_supported": 1,
            "rti_settlement_average_risk_class": "low",
            "rti_path_regime_class": "persistent",
            "rti_path_strike_crossings": 0,
            "rti_path_persistence": 1.0,
            "rti_path_trend_efficiency": 1.0,
            "rti_signed_distance_bps": 2.0,
        },
    }
    monkeypatch.setattr(
        seal, "validate_exact_contract_identity",
        lambda value: {"valid": True, "version": "contract-v1"},
    )
    monkeypatch.setattr(
        seal.v18, "evaluate_source_row",
        lambda value: {
            "available": True,
            "rule_version": "source-v1",
            "feature_evidence_sha256": "source-hash",
            "evidence": {
                "strict_rule_version": "strict-v3",
                "risk_policy_version": identity.RISK_POLICY_VERSION,
                "reversal_risk_class": "low",
            },
        },
    )
    monkeypatch.setattr(
        seal.v18, "evaluate_strict_control_row",
        lambda value: {"eligible": True, "feature_evidence_sha256": "control-hash"},
    )
    monkeypatch.setattr(
        seal.v18, "evaluate_row",
        lambda value: {"eligible": True, "feature_evidence_sha256": "candidate-hash"},
    )
    projected = seal._project([row])
    assert len(projected) == 1
    assert projected[0]["sim_full_fill_supported"] is True
    assert projected[0]["v18_source_quality_evidence_sha256"] == "source-hash"
    assert projected[0]["strict_control_eligible"] is True
    assert projected[0]["v18_candidate_eligible"] is True
    assert seal.OUTCOME_COLUMNS.isdisjoint(projected[0])


def test_tampered_contract_fails(tmp_path):
    contract = deepcopy(seal.load_contract())
    contract["prospective_selection"]["minimum_candidate_picks"] = 1
    path = tmp_path / "contract.json"
    path.write_text(__import__("json").dumps(contract), encoding="utf-8")
    with pytest.raises(ValueError, match="identity_or_safety"):
        seal.load_contract(path)
