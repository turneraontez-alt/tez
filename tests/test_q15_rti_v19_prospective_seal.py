from __future__ import annotations

from copy import deepcopy
import json

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v19_audit_identity as audit_identity
from q15_upgrade.strategy_bots import rti_microstructure_v19_identity as identity
from tools import q15_rti_v19_prospective_seal as seal
from tools.q15_rti_microstructure_preregister import design_fingerprint


ASSETS = ("BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP")


def _windows(count: int = 160):
    output = {}
    parent_id = 1
    delayed_id = 100_001
    for index in range(count):
        close = identity.FIRST_ELIGIBLE_CLOSE_TIME + index * 900.0
        pairs = []
        for asset in ASSETS:
            parent = {
                "id": parent_id,
                "asset": asset,
                "close_time": close,
                "candidate": bool(index % 5 == 0 and asset == "ETH"),
                "control": asset != "BTC",
            }
            delayed = {
                "id": delayed_id,
                "asset": asset,
                "close_time": close,
                "threshold_json": {"rti_confirm_original_row_id": parent_id},
            }
            pairs.append((parent, delayed))
            parent_id += 1
            delayed_id += 1
        output[close] = pairs
    return output


def _patch_selection(monkeypatch, windows):
    monkeypatch.setattr(
        seal, "_matched_complete_windows", lambda _parents, _delayed: windows,
    )
    monkeypatch.setattr(
        seal.v18, "evaluate_row",
        lambda row: {"eligible": bool(row.get("control"))},
    )
    monkeypatch.setattr(
        seal.v19, "evaluate_pair",
        lambda parent, _delayed: {"eligible": bool(parent.get("candidate"))},
    )


def test_contract_is_hash_bound_outcome_blind_and_silent():
    contract = seal.load_contract()
    assert design_fingerprint(contract) == audit_identity.AUDIT_CONTRACT_SHA256
    assert contract["outcome_labels_used_to_create_contract"] is False
    assert contract["prospective_resolution_status_inspected_to_create_contract"] is False
    assert contract["label_access"]["exclusive_reservation_before_callback"] is True
    assert contract["result_policy"]["notifications_allowed_by_this_command"] is False
    assert contract["result_policy"]["real_trading_allowed"] is False


def test_prefix_is_earliest_and_never_splits_a_close(monkeypatch):
    windows = _windows()
    _patch_selection(monkeypatch, windows)
    selected = seal.select_prefix([], [])
    assert selected["status"] == seal.READY_STATUS
    assert len(selected["selected_close_times"]) == 150
    assert len(selected["candidate_pairs"]) == 30
    assert len(selected["control_pairs"]) == 150 * 6
    assert len(selected["source_pairs"]) == 150 * 7
    assert selected["selected_close_times"][-1] == sorted(windows)[149]


def test_not_ready_never_creates_a_partial_seal(monkeypatch):
    windows = _windows(149)
    _patch_selection(monkeypatch, windows)
    selected = seal.select_prefix([], [])
    assert selected["status"] == seal.NOT_READY_STATUS
    assert selected["future_complete_close_windows"] == 149
    assert selected["candidate_picks"] == 30
    assert selected["complete_close_windows_remaining"] == 1
    with pytest.raises(ValueError, match="population_not_ready"):
        seal.build_seal([], [])


def test_matched_window_requires_exactly_one_fresh_delayed_row(monkeypatch):
    close = identity.FIRST_ELIGIBLE_CLOSE_TIME
    parents = [
        {"id": index + 1, "asset": asset, "close_time": close}
        for index, asset in enumerate(ASSETS)
    ]
    delayed = [{
        "id": 101 + index,
        "asset": parent["asset"],
        "close_time": close,
        "threshold_json": {"rti_confirm_original_row_id": parent["id"]},
    } for index, parent in enumerate(parents)]
    monkeypatch.setattr(seal, "_complete_windows", lambda _rows: {close: parents})
    monkeypatch.setattr(
        seal.v19, "evaluate_delayed_source", lambda _parent, _delayed: {
            "available": True,
        },
    )
    assert tuple(seal._matched_complete_windows(parents, delayed)) == (close,)
    assert seal._matched_complete_windows(parents, delayed[:-1]) == {}
    assert seal._matched_complete_windows(parents, delayed + [dict(delayed[0], id=999)]) == {}


def test_build_and_reconstruct_bind_parent_delayed_control_and_source(monkeypatch):
    windows = _windows()
    _patch_selection(monkeypatch, windows)

    def project(pairs):
        return [{
            "parent_id": int(parent["id"]),
            "delayed_id": int(delayed["id"]),
            "asset": parent["asset"],
            "close_time": float(parent["close_time"]),
            "parent_sim_full_fill_supported": True,
            "delayed_sim_full_fill_supported": True,
            "delayed_sim_contracts": 10,
        } for parent, delayed in pairs]

    monkeypatch.setattr(seal, "_project_pairs", project)
    artifact = seal.build_seal([], [], generated_at="2026-08-01T12:00:00Z")
    seal.validate_seal(artifact)
    assert artifact["selected_complete_close_windows"] == 150
    assert artifact["selected_candidate_picks"] == 30
    assert artifact["selected_control_picks"] == 900
    assert artifact["selected_all_seven_parent_delayed_pairs"] == 1050
    reconstructed = seal.reconstruct_examples([], [], artifact)
    assert len(reconstructed["candidate"]) == 30
    assert len(reconstructed["control"]) == 900
    assert len(reconstructed["source"]) == 1050


def test_tampered_seal_and_contract_fail_closed(tmp_path, monkeypatch):
    contract = deepcopy(seal.load_contract())
    contract["gate"]["candidate_resolved_picks_minimum"] = 1
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contract), encoding="utf-8")
    with pytest.raises(ValueError, match="identity_or_safety"):
        seal.load_contract(path)

    windows = _windows()
    _patch_selection(monkeypatch, windows)
    monkeypatch.setattr(seal, "_project_pairs", lambda pairs: [{
        "parent_id": int(parent["id"]),
        "delayed_id": int(delayed["id"]),
        "asset": parent["asset"],
        "close_time": float(parent["close_time"]),
        "parent_sim_full_fill_supported": True,
        "delayed_sim_full_fill_supported": True,
        "delayed_sim_contracts": 10,
    } for parent, delayed in pairs])
    artifact = seal.build_seal([], [])
    artifact["selected_candidate_picks"] += 1
    with pytest.raises(ValueError, match="seal_invalid"):
        seal.validate_seal(artifact)


def test_exclusive_write_cannot_overwrite_existing_seal(tmp_path, monkeypatch):
    windows = _windows()
    _patch_selection(monkeypatch, windows)
    monkeypatch.setattr(seal, "_project_pairs", lambda pairs: [{
        "parent_id": int(parent["id"]),
        "delayed_id": int(delayed["id"]),
        "asset": parent["asset"],
        "close_time": float(parent["close_time"]),
        "parent_sim_full_fill_supported": True,
        "delayed_sim_full_fill_supported": True,
        "delayed_sim_contracts": 10,
    } for parent, delayed in pairs])
    artifact = seal.build_seal([], [])
    path = tmp_path / "seal.json"
    seal.write_exclusive(path, artifact)
    with pytest.raises(FileExistsError):
        seal.write_exclusive(path, artifact)
