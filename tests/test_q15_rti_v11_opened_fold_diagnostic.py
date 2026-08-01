from __future__ import annotations

import pytest

from tools import q15_rti_v11_opened_fold_diagnostic as diagnostic


def _rejected_report(**overrides):
    report = {
        "status": "REJECTED_ON_WALK_FORWARD_GATE",
        "outcome_labels_read": True,
        "untouched_test_labels_read": False,
        "walk_forward_gate": {"untouched_test_rows_used": 0},
    }
    report.update(overrides)
    return report


def test_locked_report_guard_accepts_only_sealed_walk_forward_rejection():
    diagnostic._assert_opened_pretest_only(_rejected_report())
    for override in (
        {"status": "PASSED_UNTOUCHED_TEST"},
        {"outcome_labels_read": False},
        {"untouched_test_labels_read": True},
        {"walk_forward_gate": {"untouched_test_rows_used": 1}},
    ):
        with pytest.raises(ValueError):
            diagnostic._assert_opened_pretest_only(_rejected_report(**override))


def test_zero_safe_blend_is_exact_market_fallback():
    for market in (0.01, 0.2, 0.5, 0.8, 0.99):
        assert diagnostic._blend_probability(market, 1.0 - market, 0.0) == (
            pytest.approx(market)
        )


def test_compact_reconstruction_never_calls_official_v12_vector(monkeypatch):
    names = tuple(diagnostic.freeze.feature_v11.FEATURE_NAMES)
    values = {name: 0.0 for name in names}
    values["independent_consensus_momentum_60s_bps"] = 7.0
    values["cross_asset_median_momentum_60s"] = 2.5

    def fake_v11(_row):
        return {
            "available": True,
            "features": [values[name] for name in names],
            "market_yes_probability": 0.61,
        }

    monkeypatch.setattr(
        diagnostic.feature_v12,
        "feature_vector",
        lambda row: (_ for _ in ()).throw(
            AssertionError("official V12 boundary must not be bypassed")
        ),
    )
    monkeypatch.setattr(diagnostic.freeze.feature_v11, "feature_vector", fake_v11)
    source = {1: {"id": 1}}
    output = diagnostic._compact_examples(
        [{"id": 1, "close_time": 1.0, "asset": "ETH"}], source
    )
    by_name = dict(zip(output[0]["feature_names"], output[0]["features"]))
    assert by_name[diagnostic.feature_v12.RELATIVE_MOMENTUM_FEATURE] == 4.5
    assert output[0]["market_yes_probability"] == 0.61
