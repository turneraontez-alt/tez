from __future__ import annotations

from copy import deepcopy
import json

import pytest

from q15_upgrade.strategy_bots import rti_delayed_feature_reservoir_identity as identity
from tools import q15_rti_delayed_feature_reservoir_geometry as geometry
from tools import q15_rti_delayed_feature_reservoir_readiness as readiness
from tools.q15_rti_microstructure_preregister import design_fingerprint


ASSETS = ("BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP")


def _rows() -> list[dict]:
    close = identity.FIRST_ELIGIBLE_CLOSE_TIME
    rows = []
    for row_id, asset in enumerate(ASSETS, start=1):
        profile = {key: None for key in readiness.REQUIRED_PERSISTED_KEYS}
        profile.update({
            "delayed_feature_reservoir_version": identity.RESERVOIR_VERSION,
            "delayed_feature_reservoir_record_only": True,
            "delayed_feature_reservoir_used_for_decision": False,
            "quote_age_source": "kalshi_ws_exact_sampler",
            "quote_evidence_source": "kalshi_official_websocket_book",
            "quote_age_seconds": 0.1,
            "rti_confirm_original_row_id": 10_000 + row_id,
            "rti_confirm_original_side": "YES",
            "rti_confirm_side": "YES",
            "rti_confirm_target_at": close - 720.0,
            "rti_confirm_delay_seconds": 60.0,
            "rti_confirm_quote_captured_at": close - 719.9,
            "rti_confirm_evaluated_at": close - 719.8,
            "rti_confirm_path_status": "ok",
            "rti_confirm_path_missing_reason": None,
            "rti_confirm_path_complete": True,
            "rti_confirm_path_expected_count": 61,
            "rti_confirm_path_count": 61,
            "rti_confirm_path_max_receive_age_s": 0.1,
            "rti_confirm_path_decision_age_s": 0.2,
            "rti_confirm_original_end_px": 100.0,
            "rti_confirm_end_px": 101.0,
            "rti_confirm_continuation_bps": 100.0,
            "rti_confirm_signed_distance_bps": 2.0,
            "sim_contracts": 10,
            "sim_full_fill_supported": True,
            "kalshi_microstructure_extension_schema_version": "test-extension-v1",
            "kalshi_microstructure_time_basis": "local_receive_time",
            "kalshi_microstructure_captured_at": close - 719.9,
            "kalshi_microstructure_evidence_source": (
                "kalshi_official_websocket_history"
            ),
            "kalshi_microstructure_transport_connected": True,
            "kalshi_microstructure_transport_age_seconds": 0.1,
            "kalshi_microstructure_book_age_seconds": 30.0,
            "spot_depth_status": "ok",
            "spot_depth_source": "test-spot-source",
            "rti_spot_evidence_as_of": close - 719.7,
            "rti_spot_snapshot_created_at": close - 719.8,
            "rti_spot_snapshot_age_s": 0.1,
            "rti_spot_book_age_s": 0.2,
            "rti_spot_book_source_at": close - 719.6,
            "rti_spot_book_received_at": close - 720.0,
            "rti_spot_book_source_age_s": -0.2,
            "spot_depth_mid": 100.0,
            "spot_depth_imbalance": 0.2,
            "spot_depth_trade_net_notional_5s": 100.0,
            "spot_depth_trade_net_notional_15s": 250.0,
            "spot_depth_trade_net_notional_60s": 500.0,
            "spot_mid_change_bps_15s": 1.0,
            "spot_mid_change_bps_60s": 2.0,
            "spot_fast_mid_path_schema_version": (
                "spot-fast-mid-path-local-observed-v1"
            ),
            "spot_fast_mid_path_time_basis": (
                "local_received_or_captured_at"
            ),
            "spot_fast_mid_change_bps_15s": 1.0,
            "spot_fast_mid_change_bps_60s": 2.0,
        })
        for horizon in readiness.KALSHI_COMPLETE_HORIZONS:
            profile[f"kalshi_microstructure_window_complete_{horizon}s"] = True
            profile[f"kalshi_book_delta_pressure_yes_{horizon}s"] = 0.1
            profile[f"kalshi_trade_imbalance_yes_{horizon}s"] = 0.25
        for horizon in readiness.SPOT_COMPLETE_HORIZONS:
            profile[f"spot_mid_window_complete_{horizon}s"] = True
            profile[f"spot_fast_mid_window_complete_{horizon}s"] = True
        rows.append({
            "id": row_id,
            "asset": asset,
            "close_time": close,
            "interval": "12M",
            "record_kind": "RTI_PATH_12M_CONFIRM_PROSPECTIVE",
            "threshold_json": profile,
        })
    return rows


def _geometry_rows(*, constant_signal: str | None = None) -> list[dict]:
    rows = []
    for window_index in range(3):
        for asset_index, source in enumerate(_rows()):
            row = deepcopy(source)
            row["id"] = window_index * 100 + int(source["id"])
            row["close_time"] = (
                identity.FIRST_ELIGIBLE_CLOSE_TIME + 900.0 * window_index
            )
            profile = dict(row["threshold_json"])
            for key, value in tuple(profile.items()):
                if (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and key not in geometry.EXPECTED_CONSTANT_NUMERIC_KEYS
                    and key != constant_signal
                ):
                    profile[key] = (
                        float(value) + window_index + asset_index / 100.0
                    )
            profile.update({
                "kalshi_book_event_retention_seconds": 90.0,
                "kalshi_trade_retention_seconds": 1200.0,
                "spot_mid_history_retention_seconds": 180.0,
                "spot_mid_record_interval_seconds": 5.0,
                "spot_fast_mid_history_retention_seconds": 180.0,
                "spot_fast_mid_record_interval_seconds": 1.0,
            })
            row["threshold_json"] = profile
            rows.append(row)
    return rows


def test_protocol_is_hash_bound_outcome_blind_and_record_only():
    protocol = readiness.load_protocol()
    assert design_fingerprint(protocol) == identity.PROTOCOL_SHA256
    assert protocol["usage"]["record_only"] is True
    assert protocol["usage"]["used_by_v19"] is False
    assert protocol["usage"]["outcome_access_allowed"] is False
    assert protocol["usage"]["notifications_allowed"] is False
    assert protocol["usage"]["real_trading_allowed"] is False


def test_complete_all_seven_window_gets_schema_credit_without_outcomes():
    report = readiness.build_readiness(_rows())
    assert report["all_seven_geometry_close_windows"] == 1
    assert report["identity_and_schema_complete_close_windows"] == 1
    assert report["usable_feature_complete_close_windows"] == 1
    assert report["rows_audited"] == 7
    assert report["missing_required_key_counts"] == {}
    assert report["identity_failure_counts"] == {}
    assert report["feature_quality_failure_counts"] == {}
    assert report["latest_window_identity_complete"] is True
    assert report["latest_window_feature_complete"] is True
    assert report["latest_window_feature_quality_failures"] == []
    assert report[
        "consecutive_latest_usable_feature_complete_close_windows"
    ] == 1
    assert report["non_null_coverage"]["kalshi_trade_imbalance_yes_60s"][
        "rate"
    ] == 1.0
    assert report["outcome_columns_selected"] is False
    assert report["outcome_labels_read"] is False
    assert report["model_fit_performed"] is False
    assert report["threshold_selection_performed"] is False


def test_missing_persisted_feature_or_identity_fails_entire_window():
    rows = _rows()
    profile = dict(rows[0]["threshold_json"])
    profile.pop("kalshi_trade_imbalance_yes_60s")
    profile["delayed_feature_reservoir_used_for_decision"] = True
    rows[0]["threshold_json"] = profile
    report = readiness.build_readiness(rows)
    assert report["all_seven_geometry_close_windows"] == 1
    assert report["identity_and_schema_complete_close_windows"] == 0
    assert report["missing_required_key_counts"][
        "kalshi_trade_imbalance_yes_60s"
    ] == 1
    assert report["identity_failure_counts"]["PERSISTED_SCHEMA_INCOMPLETE"] == 1
    assert report["identity_failure_counts"][
        "NOT_USED_FOR_DECISION_IDENTITY"
    ] == 1
    assert report["status"] == "FEATURE_RESERVOIR_INTEGRITY_FAILURE"


def test_null_feature_shell_is_not_credited_as_usable_evidence():
    rows = _rows()
    profile = dict(rows[0]["threshold_json"])
    profile["kalshi_microstructure_extension_schema_version"] = None
    profile["kalshi_trade_imbalance_yes_60s"] = None
    profile["spot_depth_status"] = "missing"
    profile["spot_depth_missing_reason"] = "spot_depth_book_stale"
    profile["spot_depth_trade_net_notional_60s"] = None
    profile["spot_fast_mid_change_bps_60s"] = None
    rows[0]["threshold_json"] = profile

    report = readiness.build_readiness(rows)
    assert report["identity_and_schema_complete_close_windows"] == 1
    assert report["usable_feature_complete_close_windows"] == 0
    assert report["feature_quality_failure_counts"][
        "KALSHI_MICROSTRUCTURE_SOURCE_MISSING"
    ] == 1
    assert report["feature_quality_failure_counts"][
        "KALSHI_FLOW_VALUES_MISSING"
    ] == 1
    assert report["feature_quality_failure_counts"][
        "SPOT_DEPTH_SOURCE_UNUSABLE"
    ] == 1
    assert report["feature_quality_failure_counts"][
        "SPOT_FEATURE_VALUES_MISSING"
    ] == 1
    assert report["status"] == "FEATURE_RESERVOIR_INTEGRITY_FAILURE"


def test_missing_confirmation_rti_path_cannot_receive_usable_credit():
    rows = _rows()
    profile = dict(rows[0]["threshold_json"])
    profile.update({
        "rti_confirm_path_status": "missing",
        "rti_confirm_path_missing_reason": "delayed_confirmation_deadline_missed",
        "rti_confirm_path_complete": False,
        "rti_confirm_path_count": 0,
        "rti_confirm_path_max_receive_age_s": None,
        "rti_confirm_path_decision_age_s": None,
        "rti_confirm_end_px": None,
        "rti_confirm_continuation_bps": None,
        "rti_confirm_signed_distance_bps": None,
        "rti_confirm_side": None,
    })
    rows[0]["threshold_json"] = profile
    report = readiness.build_readiness(rows)
    assert report["usable_feature_complete_close_windows"] == 0
    assert report["feature_quality_failure_counts"][
        "FRESH_61_SECOND_RTI_CONFIRMATION_PATH_MISSING"
    ] == 1
    assert report["feature_quality_failure_counts"][
        "RTI_CONFIRMATION_VALUES_MISSING"
    ] == 1
    assert report["latest_window_identity_complete"] is True
    assert report["latest_window_feature_complete"] is False
    assert "FRESH_61_SECOND_RTI_CONFIRMATION_PATH_MISSING" in report[
        "latest_window_feature_quality_failures"
    ]
    assert report[
        "consecutive_latest_usable_feature_complete_close_windows"
    ] == 0


def test_latest_window_health_does_not_hide_older_integrity_failure():
    older = _rows()
    bad_profile = dict(older[0]["threshold_json"])
    bad_profile["rti_confirm_path_status"] = "missing"
    bad_profile["rti_confirm_path_complete"] = False
    bad_profile["rti_confirm_path_count"] = 0
    older[0]["threshold_json"] = bad_profile
    newer = deepcopy(_rows())
    for row in newer:
        row["id"] += 100
        row["close_time"] += 900.0
    report = readiness.build_readiness(older + newer)
    assert report["all_seven_geometry_close_windows"] == 2
    assert report["usable_feature_complete_close_windows"] == 1
    assert report["latest_window_feature_complete"] is True
    assert report[
        "consecutive_latest_usable_feature_complete_close_windows"
    ] == 1
    assert report["status"] == "FEATURE_RESERVOIR_INTEGRITY_FAILURE"


def test_partial_cross_asset_window_receives_no_credit():
    report = readiness.build_readiness(_rows()[:-1])
    assert report["all_seven_geometry_close_windows"] == 0
    assert report["identity_and_schema_complete_close_windows"] == 0
    assert report["usable_feature_complete_close_windows"] == 0
    assert report["rows_audited"] == 0


def test_exchange_clock_cannot_make_local_spot_freshness_negative():
    rows = _rows()
    profile = dict(rows[0]["threshold_json"])
    profile["rti_spot_book_age_s"] = -0.2
    rows[0]["threshold_json"] = profile

    report = readiness.build_readiness(rows)
    assert report["usable_feature_complete_close_windows"] == 0
    assert report["feature_quality_failure_counts"][
        "SPOT_TIMESTAMP_LINEAGE_MISALIGNED"
    ] == 1


def test_outcome_blind_geometry_accepts_only_expected_constants():
    report = geometry.build_geometry(_geometry_rows())
    assert report["complete_close_windows"] == 3
    assert report["rows_audited"] == 21
    assert report["variable_numeric_features"] > 0
    assert report["temporally_variable_numeric_features"] > 0
    assert report["unexpected_constant_numeric_features"] == []
    assert "rti_confirm_path_count" in report[
        "expected_constant_numeric_features"
    ]
    assert report["outcome_columns_selected"] is False
    assert report["outcome_labels_read"] is False
    assert report["model_fit_performed"] is False
    assert report["status"] == "FEATURE_OBSERVABILITY_OK"


def test_outcome_blind_geometry_warns_on_dead_candidate_signal():
    report = geometry.build_geometry(
        _geometry_rows(constant_signal="spot_depth_mid")
    )
    assert "spot_depth_mid" in report[
        "unexpected_constant_numeric_features"
    ]
    assert report["status"] == "FEATURE_OBSERVABILITY_WARNING"


def test_tampered_protocol_fails_closed(tmp_path, monkeypatch):
    protocol = deepcopy(readiness.load_protocol())
    protocol["usage"]["used_by_v19"] = True
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(protocol), encoding="utf-8")
    monkeypatch.setattr(identity, "PROTOCOL_RELATIVE_PATH", str(path))
    with pytest.raises(ValueError, match="identity_or_safety"):
        readiness.load_protocol()
