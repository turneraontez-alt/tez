from __future__ import annotations

from copy import deepcopy

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v18 as v18
from q15_upgrade.strategy_bots import rti_microstructure_v18_identity as identity
from tools.q15_rti_microstructure_preregister import design_fingerprint


def _row() -> dict:
    close_time = identity.FIRST_ELIGIBLE_CLOSE_TIME
    captured = close_time - 780.0 + 0.25
    return {
        "id": 1,
        "ticker": "KXETH15M-TEST",
        "bot_name": "rti_path_13m",
        "record_kind": "RTI_PATH_13M_PROSPECTIVE_EXACT",
        "interval": "13M",
        "asset": "ETH",
        "close_time": close_time,
        "side": "YES",
        "entry_ask_cents": 58.0,
        "spread_cents": 1.0,
        "source_captured_at": captured,
        "evidence_as_of": captured,
        "threshold_json": {
            "asset_cohort": "ETH",
            "rti_side": "YES",
            "paper_only": True,
            "passed": True,
            "rule_version": "eth-rti-path-13m-62c-transfer-exact-v3",
            "rti_risk_policy_version": identity.RISK_POLICY_VERSION,
            "rti_reversal_risk_class": "low",
            "rti_reversal_risk_reason_codes": [],
            "rti_path_status": "ok",
            "rti_path_complete": True,
            "rti_path_expected_count": 61,
            "rti_path_count": 61,
            "rti_path_max_receive_age_s": 0.1,
            "rti_decision_age_s": 0.2,
            "rti_timing_offset_s": 0.25,
            "rti_path_evaluation_delay_s": 0.05,
            "quote_age_seconds": 0.1,
        },
    }


def test_protocol_is_frozen_and_discloses_development_selection():
    protocol = v18.load_protocol()
    assert design_fingerprint(protocol) == identity.PROTOCOL_SHA256
    assert protocol["development_selection_disclosure"][
        "candidate_rule_selected_using_v17_development_labels"
    ] is True
    assert protocol["development_selection_disclosure"][
        "exploratory_robustness_screen_passed"
    ] is False
    assert protocol["collection"]["notifications_allowed_now"] is False
    assert protocol["collection"]["real_trading_allowed"] is False


def test_frozen_rule_selects_only_strict_low_reversal_rows():
    result = v18.evaluate_row(_row())
    assert result["eligible"] is True
    assert result["decision"] == "YES"
    assert result["outcome_labels_read"] is False
    assert result["notification_eligible"] is False
    assert result["real_trading_allowed"] is False

    row = _row()
    row["threshold_json"]["rti_reversal_risk_class"] = "medium"
    result = v18.evaluate_row(row)
    assert result["eligible"] is False
    assert result["decision"] == "ABSTAIN"
    assert result["failures"] == ["REVERSAL_RISK_NOT_LOW"]
    control = v18.evaluate_strict_control_row(row)
    assert control["eligible"] is True
    assert control["decision"] == "YES"

    row = _row()
    row["threshold_json"]["passed"] = False
    result = v18.evaluate_row(row)
    assert result["eligible"] is False
    assert "STRICT_CONTROL_NOT_PASSED" in result["failures"]


def test_timestamp_and_cohort_leakage_fail_closed():
    row = _row()
    row["evidence_as_of"] = row["source_captured_at"] - 0.01
    assert "EVIDENCE_PRECEDES_CAPTURE" in v18.evaluate_row(row)["failures"]

    row = _row()
    row["evidence_as_of"] = row["source_captured_at"] + 2.01
    assert "EVALUATION_NOT_FRESH" in v18.evaluate_row(row)["failures"]

    row = _row()
    row["asset"] = "BTC"
    result = v18.evaluate_row(row)
    assert result["eligible"] is False
    assert "NON_BTC_COHORT_REQUIRED" in result["failures"]

    row = _row()
    row["close_time"] = identity.PROSPECTIVE_AFTER_CLOSE_TIME
    result = v18.evaluate_row(row)
    assert result["eligible"] is False
    assert "STRICTLY_PROSPECTIVE_CLOSE_REQUIRED" in result["failures"]


def test_safe_loader_shaped_profile_preserves_frozen_selection():
    row = _row()
    row["evidence_as_of"] = row["source_captured_at"] + 0.05
    row["threshold_json"]["paper_only"] = 1
    row["threshold_json"]["passed"] = 1
    row["threshold_json"]["rti_reversal_risk_reason_codes"] = "[]"
    result = v18.evaluate_row(row)
    assert result["eligible"] is True
    assert result["evidence"]["evaluation_delay_seconds"] == pytest.approx(0.05)
    assert result["evidence"]["reversal_risk_reason_codes"] == []


def test_source_quality_is_all_asset_and_fails_incomplete_path():
    row = _row()
    row["asset"] = "BTC"
    row["threshold_json"]["asset_cohort"] = "BTC"
    assert v18.evaluate_source_row(row)["available"] is True
    assert "NON_BTC_COHORT_REQUIRED" in v18.evaluate_row(row)["failures"]

    row = _row()
    row["threshold_json"]["rti_path_complete"] = False
    source = v18.evaluate_source_row(row)
    assert source["available"] is False
    assert "PATH_61_FRESH" in source["failures"]


def test_tampered_protocol_fails(tmp_path):
    protocol = deepcopy(v18.load_protocol())
    protocol["collection"]["notifications_allowed_now"] = True
    assert design_fingerprint(protocol) != identity.PROTOCOL_SHA256
    path = tmp_path / "tampered.json"
    path.write_text(__import__("json").dumps(protocol), encoding="utf-8")
    with pytest.raises(ValueError, match="identity_or_safety"):
        v18.load_protocol(path)
