from __future__ import annotations

import hashlib
import json
from pathlib import Path

from q15_upgrade.strategy_bots import rti_microstructure_v13_identity as v13
from q15_upgrade.strategy_bots import rti_microstructure_v13 as feature_v13
from tools import q15_rti_microstructure_freeze as freeze
from tools import q15_rti_microstructure_preregister as preregister


ROOT = Path(__file__).resolve().parents[1]
CHARTER_PATH = ROOT / "config" / "q15_rti_v13_successor_preregistration.json"
DESIGN_PATH = ROOT / "config" / "q15_rti_microstructure_design_v13.json"
PROTOCOL_PATH = ROOT / "config" / "q15_rti_v13_walk_forward_protocol.json"
GEOMETRY_PROTOCOL_PATH = (
    ROOT / "config" / "q15_rti_v13_geometry_review_protocol.json"
)
DRIFT_PROTOCOL_PATH = (
    ROOT / "config" / "q15_rti_v13_covariate_drift_protocol.json"
)
REPORTING_PROTOCOL_PATH = ROOT / "config" / "q15_rti_v13_reporting_protocol.json"
CALIBRATION_PROTOCOL_PATH = (
    ROOT / "config" / "q15_rti_v13_calibration_reporting_protocol.json"
)
VALUE_CURVE_PROTOCOL_PATH = (
    ROOT / "config" / "q15_rti_v13_selective_value_curve_protocol.json"
)


def _canonical_sha256(value) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_v13_successor_charter_identity_and_boundary_are_immutable():
    charter = json.loads(CHARTER_PATH.read_text(encoding="utf-8"))
    assert charter["charter_id"] == v13.CHARTER_ID
    assert _canonical_sha256(charter) == v13.CHARTER_SHA256
    decision = charter["manual_successor_decision"]
    assert decision["proposed_design_id"] == v13.PROPOSED_DESIGN_ID
    assert decision["prospective_after_close_time"] == (
        v13.CHARTER_PROSPECTIVE_AFTER_CLOSE_TIME
    )
    assert decision["first_eligible_close_time"] == (
        v13.CHARTER_FIRST_ELIGIBLE_CLOSE_TIME
    )
    assert decision["prospective_after_close_time"] == (
        charter["source_review_evidence"]["last_close_time"]
    )
    assert decision["historical_credit_allowed"] is False
    assert decision["v11_and_v12_remain_frozen_parallel_controls"] is True


def test_v13_charter_matches_predeclared_geometry_trigger_only():
    charter = json.loads(CHARTER_PATH.read_text(encoding="utf-8"))
    evidence = charter["source_review_evidence"]
    decision = charter["manual_successor_decision"]
    assert evidence["outcome_labels_read"] is False
    assert evidence["model_fit_performed"] is False
    assert evidence["performance_metrics_inspected"] is False
    assert evidence["timestamp_alignment_failures"] == 0
    assert evidence["btc_alias_pair_absolute_correlation"] >= 0.95
    assert evidence["non_btc_pairs_at_or_above_0_95"] == 0
    assert decision["selection_basis"] == (
        "PREDECLARED_OUTCOME_BLIND_BTC_ALIAS_TRIGGER_ONLY"
    )
    assert decision["replacement_formula"] == (
        "0 for BTC; otherwise preserve "
        "cross_asset_btc_minus_non_btc_median_60s"
    )
    assert decision["all_other_v12_feature_formulas_unchanged"] is True
    assert decision["all_v12_training_hyperparameters_unchanged"] is True


def test_v13_charter_keeps_drift_report_only_and_every_activation_off():
    charter = json.loads(CHARTER_PATH.read_text(encoding="utf-8"))
    drift = charter["volatility_drift_decision"]
    assert drift["feature_removed_or_changed_now"] is False
    assert drift["absolute_volatility_feature_retained_unchanged"] is True
    assert drift["mandatory_repeat_review_at_60_complete_v13_windows"] is True
    assert charter["paper_only"] is True
    for key in (
        "notification_eligible",
        "automatic_design_creation_allowed",
        "automatic_refit",
        "automatic_activation",
        "automatic_promotion",
        "real_trading_allowed",
    ):
        assert charter[key] is False
    assert v13.EXECUTABLE_DESIGN_FROZEN is True
    assert v13.RUNTIME_SCORING_CONNECTED is False
    assert v13.NOTIFICATION_ELIGIBLE is False
    assert v13.REAL_TRADING_ALLOWED is False


def test_opened_fold_evidence_cannot_credit_or_open_v12():
    charter = json.loads(CHARTER_PATH.read_text(encoding="utf-8"))
    opened = charter["v11_opened_fold_development_evidence"]
    assert opened["opened_pretest_rows"] == 288
    assert opened["untouched_test_rows_used"] == 0
    assert opened["v11_untouched_test_labels_read"] is False
    assert opened["v12_receives_historical_credit"] is False
    assert opened["nested_safe_blend_selected_factors"] == [0.0, 0.0, 0.0]
    requirements = charter["required_before_executable_v13_design"]
    assert requirements["no_v12_outcome_labels_may_be_read_before_v12_readiness"]
    assert requirements["no_v13_outcome_labels_may_be_read_before_v13_readiness"]
    assert requirements["manual_activation_only"]


def test_v13_design_and_walk_forward_protocol_are_immutable_and_fail_closed():
    design = json.loads(DESIGN_PATH.read_text(encoding="utf-8"))
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    assert _canonical_sha256(design) == v13.DESIGN_SHA256
    assert _canonical_sha256(protocol) == v13.EVALUATION_PROTOCOL_SHA256
    preregister.validate_design(design)
    freeze.validate_walk_forward_protocol(protocol, design)
    assert freeze.walk_forward_protocol_for_design(design) == protocol
    assert design["prospective_after_close_time"] == (
        v13.PROSPECTIVE_AFTER_CLOSE_TIME
    )
    assert design["first_eligible_close_time"] == v13.FIRST_ELIGIBLE_CLOSE_TIME
    assert design["implementation_boundary_stricter_than_charter"] is True
    assert design["excluded_charter_first_candidate_close_time"] == (
        v13.CHARTER_FIRST_ELIGIBLE_CLOSE_TIME
    )
    assert design["historical_credit_allowed"] is False
    assert design["notification_eligible"] is False
    assert design["real_trading_allowed"] is False


def test_v13_outcome_blind_review_protocol_hashes_are_pinned():
    geometry = json.loads(GEOMETRY_PROTOCOL_PATH.read_text(encoding="utf-8"))
    drift = json.loads(DRIFT_PROTOCOL_PATH.read_text(encoding="utf-8"))
    assert _canonical_sha256(geometry) == v13.GEOMETRY_REVIEW_PROTOCOL_SHA256
    assert _canonical_sha256(drift) == v13.COVARIATE_DRIFT_PROTOCOL_SHA256
    assert geometry["outcome_labels_forbidden"] is True
    assert drift["outcome_labels_forbidden"] is True
    assert geometry["notification_eligible"] is False
    assert drift["notification_eligible"] is False


def test_v13_outcome_reporting_protocol_chain_hashes_are_pinned_before_labels():
    reporting = json.loads(REPORTING_PROTOCOL_PATH.read_text(encoding="utf-8"))
    calibration = json.loads(
        CALIBRATION_PROTOCOL_PATH.read_text(encoding="utf-8")
    )
    value_curve = json.loads(
        VALUE_CURVE_PROTOCOL_PATH.read_text(encoding="utf-8")
    )
    assert _canonical_sha256(reporting) == v13.REPORTING_PROTOCOL_SHA256
    assert _canonical_sha256(calibration) == (
        v13.CALIBRATION_REPORTING_PROTOCOL_SHA256
    )
    assert _canonical_sha256(value_curve) == (
        v13.SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256
    )
    assert calibration["applies_to_subgroup_reporting_protocol_sha256"] == (
        v13.REPORTING_PROTOCOL_SHA256
    )
    assert value_curve[
        "applies_to_calibration_reporting_protocol_sha256"
    ] == v13.CALIBRATION_REPORTING_PROTOCOL_SHA256
    for protocol in (reporting, calibration, value_curve):
        assert protocol["outcome_labels_used_for_protocol"] is False
        assert protocol["notification_eligible"] is False
        assert protocol["real_trading_allowed"] is False


def test_v13_changes_exactly_one_v12_feature_name():
    design = json.loads(DESIGN_PATH.read_text(encoding="utf-8"))
    replacement = design["replacement_policy"]
    assert set(feature_v13.FEATURE_NAMES) - {
        feature_v13.COHORT_CONDITIONED_FEATURE
    } == set(feature_v13.v12.FEATURE_NAMES) - {
        feature_v13.REPLACED_FEATURE
    }
    assert replacement["replaced_feature_name"] == feature_v13.REPLACED_FEATURE
    assert replacement["replacement_feature_name"] == (
        feature_v13.COHORT_CONDITIONED_FEATURE
    )
    assert design["fixed_training_config"] == json.loads(
        (ROOT / "config" / "q15_rti_microstructure_design_v12.json").read_text(
            encoding="utf-8"
        )
    )["fixed_training_config"]


def test_v13_boundary_and_cohort_conditioning(monkeypatch):
    base_features = [float(index + 1) for index in range(20)]
    gap_index = feature_v13.v12.FEATURE_NAMES.index(
        feature_v13.REPLACED_FEATURE
    )
    base_features[gap_index] = 7.25

    def _base(row):
        return {
            "available": True,
            "features": list(base_features),
            "feature_names": list(feature_v13.v12.FEATURE_NAMES),
        }

    monkeypatch.setattr(feature_v13.v12, "feature_vector", _base)
    old = feature_v13.feature_vector({
        "asset": "BTC", "close_time": v13.PROSPECTIVE_AFTER_CLOSE_TIME,
    })
    assert old == {"available": False, "error": "pre_v13_prospective_boundary"}

    btc = feature_v13.feature_vector({
        "asset": "BTC", "close_time": v13.FIRST_ELIGIBLE_CLOSE_TIME,
    })
    non_btc = feature_v13.feature_vector({
        "asset": "ETH", "close_time": v13.FIRST_ELIGIBLE_CLOSE_TIME,
    })
    new_index = feature_v13.FEATURE_NAMES.index(
        feature_v13.COHORT_CONDITIONED_FEATURE
    )
    assert btc["available"] is True
    assert btc["features"][new_index] == 0.0
    assert non_btc["features"][new_index] == 7.25
    assert btc["design_id"] == v13.DESIGN_ID
    assert btc["design_sha256"] == v13.DESIGN_SHA256
