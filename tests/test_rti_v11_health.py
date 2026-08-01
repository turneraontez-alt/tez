from __future__ import annotations

from copy import deepcopy

from q15_upgrade.strategy_bots import runtime


def test_v11_headline_uses_only_design_bound_executable_windows():
    coverage = runtime._empty_rti_exact_feature_coverage()
    raw = coverage["cross_asset_regime_v11_model_readiness"]
    raw["complete_executable_close_windows"] = 27
    raw["schema_complete_close_windows"] = 29
    raw["cohorts"]["NON_BTC_TRANSFER"]["windows_remaining"] = 999
    raw["cohorts"]["BTC"]["windows_remaining"] = 999

    headline = runtime._v11_collection_readiness_headline(coverage)
    assert headline["available"] is True
    assert headline["complete_executable_close_windows"] == 27
    assert headline["schema_complete_close_windows"] == 29
    assert headline["windows_remaining_to_first_feature_review"] == 3
    assert headline["first_feature_review_ready"] is False
    assert headline["cohorts"]["NON_BTC_TRANSFER"]["windows_remaining"] == 33
    assert headline["cohorts"]["BTC"]["windows_remaining"] == 123


def test_v11_headline_fails_closed_on_identity_or_safety_tampering():
    clean = runtime._empty_rti_exact_feature_coverage()
    for key, value in (
        ("design_sha256", "0" * 64),
        ("readiness_uses_outcome_labels", True),
        ("model_fit_performed", True),
        ("notification_eligible", True),
    ):
        coverage = deepcopy(clean)
        raw = coverage["cross_asset_regime_v11_model_readiness"]
        raw["complete_executable_close_windows"] = 999
        raw[key] = value
        headline = runtime._v11_collection_readiness_headline(coverage)
        assert headline["available"] is False
        assert headline["complete_executable_close_windows"] == 0
        assert headline["first_feature_review_ready"] is False
        assert headline["status"] == "INVALID_OR_UNAVAILABLE_V11_READINESS"


def test_v11_headline_fails_closed_on_malformed_numbers():
    coverage = runtime._empty_rti_exact_feature_coverage()
    raw = coverage["cross_asset_regime_v11_model_readiness"]
    raw["complete_executable_close_windows"] = "not-a-number"
    raw["cohorts"]["BTC"]["minimum_complete_close_windows"] = []
    headline = runtime._v11_collection_readiness_headline(coverage)
    assert headline["available"] is False
    assert headline["complete_executable_close_windows"] == 0
    assert headline["cohorts"]["BTC"]["ready_for_locked_freeze"] is False
