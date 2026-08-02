from __future__ import annotations

import json
from pathlib import Path

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v16 as v16
from q15_upgrade.strategy_bots import rti_microstructure_v17 as v17
from q15_upgrade.strategy_bots import rti_microstructure_v17_identity as identity
from tools.q15_rti_microstructure_preregister import design_fingerprint


ROOT = Path(__file__).resolve().parents[1]


def _base() -> dict:
    return {
        "available": True,
        "feature_names": list(v16.FEATURE_NAMES),
        "features": [0.1] * len(v16.FEATURE_NAMES),
        "market_yes_probability": 0.55,
        "outcome_labels_read": False,
    }


def _row() -> dict:
    row = {
        "asset": "ETH",
        "close_time": identity.PROSPECTIVE_AFTER_CLOSE_TIME + 900,
        "kalshi_microstructure_schema_version": v17.SOURCE_SCHEMA,
        "kalshi_microstructure_extension_schema_version": v17.EXTENSION_SCHEMA_VERSION,
        "kalshi_microstructure_time_basis": v17.TIME_BASIS,
        "kalshi_history_count_capped": False,
    }
    for horizon in v17.HORIZONS:
        row[f"kalshi_microstructure_window_complete_{horizon}s"] = True
        for metric in (
            "microprice_change_cents", "trade_yes_price_change_cents",
            "trade_imbalance_yes", "book_delta_pressure_yes",
            "yes_best_refill", "yes_best_depletion", "no_best_refill",
            "no_best_depletion", "book_add_volume_yes",
            "book_remove_volume_yes", "book_add_volume_no",
            "book_remove_volume_no", "trade_count", "event_count",
        ):
            row[f"kalshi_{metric}_{horizon}s"] = float(horizon) / 10.0
    row["kalshi_microprice_range_cents_60s"] = 8.0
    row["kalshi_microprice_variation_cents_60s"] = 20.0
    row["kalshi_microprice_trend_efficiency_60s"] = 0.4
    return row


def test_v17_protocol_identity_population_and_safety_are_frozen():
    protocol = json.loads(
        (ROOT / identity.PROTOCOL_RELATIVE_PATH).read_text(encoding="utf-8")
    )
    assert design_fingerprint(protocol) == identity.PROTOCOL_SHA256
    assert protocol["population"]["outcome_blind_complete_disjoint_windows_available_at_freeze"] == 244
    assert protocol["population"]["development_close_windows"] == 240
    assert protocol["population"]["unselected_historical_complete_windows"] == 4
    assert protocol["safety"] == {
        "feature_construction_may_run": True,
        "outcome_access_allowed_now": False,
        "model_fit_allowed_now": False,
        "probability_scoring_allowed_now": False,
        "paper_artifact_allowed_now": False,
        "notifications_allowed_now": False,
        "automatic_promotion_allowed": False,
        "real_trading_allowed": False,
    }


def test_v17_preserves_v16_and_builds_only_bounded_dynamics(monkeypatch):
    monkeypatch.setattr(v17.v16, "feature_vector", lambda row: _base())
    result = v17.feature_vector(_row())
    assert result["available"] is True
    assert result["features"][:45] == _base()["features"]
    assert tuple(result["feature_names"]) == v17.FEATURE_NAMES
    assert len(result["features"]) == 81
    assert all(-1.0 <= value <= 1.0 for value in result["features"][45:])
    assert result["outcome_labels_read"] is False
    assert result["model_fit_performed"] is False
    assert result["probability_scoring_performed"] is False
    assert result["notification_eligible"] is False
    assert result["real_trading_allowed"] is False


def test_v17_outcomes_cannot_change_features(monkeypatch):
    monkeypatch.setattr(v17.v16, "feature_vector", lambda row: _base())
    yes = {**_row(), "official_result": "YES", "label_yes": 1}
    no = {**_row(), "official_result": "NO", "label_yes": 0}
    assert v17.feature_vector(yes)["features"] == v17.feature_vector(no)["features"]


@pytest.mark.parametrize(
    "key,value,error",
    [
        ("kalshi_microstructure_extension_schema_version", "wrong", "identity_invalid"),
        ("kalshi_history_count_capped", True, "identity_invalid"),
        ("kalshi_microstructure_window_complete_30s", False, "window_incomplete_30s"),
        ("kalshi_microprice_change_cents_15s", None, "missing"),
    ],
)
def test_v17_extension_failures_are_closed(monkeypatch, key, value, error):
    monkeypatch.setattr(v17.v16, "feature_vector", lambda row: _base())
    row = _row()
    row[key] = value
    result = v17.feature_vector(row)
    assert result["available"] is False
    assert error in result["error"]
