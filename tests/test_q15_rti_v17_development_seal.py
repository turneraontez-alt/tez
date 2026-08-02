from __future__ import annotations

from copy import deepcopy

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v17 as v17
from q15_upgrade.strategy_bots import rti_microstructure_v17_identity as identity
from tools import q15_rti_v17_development_seal as seal


def test_protocol_is_frozen_and_outcome_blind():
    protocol = seal.load_protocol()
    assert protocol["protocol_id"] == identity.PROTOCOL_ID
    assert protocol["population"]["outcome_blind_complete_disjoint_windows_available_at_freeze"] == 244
    assert protocol["safety"]["outcome_access_allowed_now"] is False
    assert protocol["safety"]["real_trading_allowed"] is False


def test_projection_preserves_v16_prefix_and_rejects_outcomes(monkeypatch):
    raw = [{"id": 1, "asset": "ETH", "close_time": 1.0}]
    monkeypatch.setattr(
        seal.v15_seal,
        "_project_evidence",
        lambda rows: ([{"id": 1, "asset": "ETH", "close_time": 1.0}], 0),
    )
    monkeypatch.setattr(
        seal.v16_seal.v16,
        "feature_vector",
        lambda row: {"available": True, "features": [0.0] * 45},
    )
    monkeypatch.setattr(
        seal.v17,
        "feature_vector",
        lambda row: {
            "available": True,
            "feature_names": list(v17.FEATURE_NAMES),
            "features": [0.0] * 81,
        },
    )
    result = seal._project(raw)
    assert result[0]["v17_features"][:45] == result[0]["v16_features"]

    monkeypatch.setattr(
        seal.v15_seal,
        "_project_evidence",
        lambda rows: ([{"id": 1, "official_result": "YES"}], 0),
    )
    with pytest.raises(AssertionError, match="projection_contains_outcome"):
        seal._project(raw)


def test_seal_hash_tampering_fails():
    fake = {
        "seal_sha256": "0" * 64,
        "fold_manifest": {},
        "prior_population_exclusion": {},
    }
    with pytest.raises(ValueError, match="seal_invalid"):
        seal.validate_seal(fake)


def test_readiness_uses_only_strictly_future_windows(monkeypatch):
    boundary = identity.PROSPECTIVE_AFTER_CLOSE_TIME
    monkeypatch.setattr(
        seal,
        "_complete_windows",
        lambda rows: {boundary: [], boundary + 900: [], boundary + 1800: []},
    )
    result = seal.prospective_readiness([])
    assert result["successor_audit_complete_close_windows"] == 2
    assert result["calibration_windows_remaining"] == 58
    assert result["outcome_labels_read"] is False
