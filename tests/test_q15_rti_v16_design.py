from __future__ import annotations

import json
from pathlib import Path

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v15 as v15
from q15_upgrade.strategy_bots import rti_microstructure_v16 as v16
from q15_upgrade.strategy_bots import rti_microstructure_v16_identity as identity
from tools.q15_rti_microstructure_preregister import design_fingerprint


ROOT = Path(__file__).resolve().parents[1]


def _base(asset: str = "HYPE") -> dict:
    values = [float(index + 1) / 10.0 for index in range(25)]
    return {
        "available": True,
        "asset": asset,
        "feature_names": list(v15.FEATURE_NAMES),
        "features": values,
        "market_yes_probability": 0.61,
        "outcome_labels_read": False,
    }


def _row(asset: str = "HYPE") -> dict:
    return {
        "asset": asset,
        "close_time": identity.PROSPECTIVE_AFTER_CLOSE_TIME + 900.0,
    }


def test_v16_protocol_identity_and_safety_are_frozen():
    protocol = json.loads(
        (ROOT / identity.PROTOCOL_RELATIVE_PATH).read_text(encoding="utf-8")
    )
    assert design_fingerprint(protocol) == identity.PROTOCOL_SHA256
    assert protocol["design_id"] == identity.DESIGN_ID
    assert protocol["protocol_id"] == identity.PROTOCOL_ID
    assert protocol["safety"] == {
        "feature_construction_may_run": True,
        "outcome_access_allowed_now": False,
        "model_fit_allowed_now": False,
        "probability_scoring_allowed_now": False,
        "paper_artifact_allowed_now": False,
        "notifications_allowed_now": False,
        "automatic_promotion_allowed": False,
        "real_trading_allowed": False,
        "manual_prospective_reviews": [30, 60, 150],
    }


def test_v16_preserves_v15_and_builds_fixed_bounded_interactions(monkeypatch):
    monkeypatch.setattr(v16.v15, "feature_vector", lambda row: _base("HYPE"))
    result = v16.feature_vector(_row("HYPE"))
    assert result["available"] is True
    assert result["features"][:25] == _base()["features"]
    assert tuple(result["feature_names"]) == v16.FEATURE_NAMES
    assert len(result["features"]) == 45
    assert result["features"][25:30] == [0.0, 0.0, 1.0, 0.0, 0.0]
    assert all(-1.0 <= value <= 1.0 for value in result["features"][30:])
    assert result["outcome_labels_read"] is False
    assert result["model_fit_performed"] is False
    assert result["probability_scoring_performed"] is False
    assert result["notification_eligible"] is False
    assert result["real_trading_allowed"] is False


def test_v16_outcomes_cannot_change_features(monkeypatch):
    monkeypatch.setattr(v16.v15, "feature_vector", lambda row: _base("ETH"))
    yes = {**_row("ETH"), "official_result": "YES", "label_yes": 1}
    no = {**_row("ETH"), "official_result": "NO", "label_yes": 0}
    assert v16.feature_vector(yes)["features"] == v16.feature_vector(no)["features"]


def test_v16_boundary_fails_before_calling_v15(monkeypatch):
    called = False

    def base(row):
        nonlocal called
        called = True
        return _base()

    monkeypatch.setattr(v16.v15, "feature_vector", base)
    result = v16.feature_vector({
        "asset": "HYPE",
        "close_time": identity.FEATURE_SOURCE_AFTER_CLOSE_TIME,
    })
    assert result == {
        "available": False,
        "error": "pre_v16_feature_source_boundary",
    }
    assert called is False


@pytest.mark.parametrize("asset", ["", "ADA", None])
def test_v16_unknown_asset_fails_closed(monkeypatch, asset):
    monkeypatch.setattr(v16.v15, "feature_vector", lambda row: _base())
    result = v16.feature_vector(_row(asset))
    assert result == {"available": False, "error": "asset_missing_or_unsupported"}
