from __future__ import annotations

from q15_upgrade.strategy_bots.rules import (
    BOT_RTI_PATH_13M,
    RTI_EXACT_MICROSTRUCTURE_SCHEMA_VERSION,
)
import json

from tools.q15_rti_feature_coverage_audit import (
    SAFE_FEATURE_PROFILE_KEYS,
    build_report,
    feature_only_sql_projection,
    materialize_feature_only_row,
    sanitize_feature_profile,
)


ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")


def test_feature_profile_allow_list_drops_nested_outcome_derived_values():
    poisoned = {
        "rti_side": "YES",
        "rti_signed_distance_bps": 2.5,
        "resolved_accuracy": 0.99,
        "resolved_correct": 999,
        "resolved_net_pnl_cents_per_contract": 9999.0,
        "official_result": "YES",
        "hypothetical_pnl_cents": 9999.0,
    }
    sanitized = sanitize_feature_profile(poisoned)
    assert sanitized == {
        "rti_side": "YES",
        "rti_signed_distance_bps": 2.5,
    }
    assert not {
        "resolved_accuracy", "resolved_correct",
        "resolved_net_pnl_cents_per_contract", "official_result",
        "hypothetical_pnl_cents",
    }.intersection(SAFE_FEATURE_PROFILE_KEYS)


def test_sql_projection_materializes_only_safe_profile_aliases():
    expressions, aliases = feature_only_sql_projection({
        "threshold_json", "rti_signed_distance_bps",
    })
    assert expressions
    assert "threshold_json" not in [expression.strip() for expression in expressions]
    assert "rti_side" in aliases
    raw = {
        "id": 1,
        "rti_signed_distance_bps": 2.5,
        aliases["rti_side"]: "YES",
        "__not_selected_outcome": 999.0,
    }
    # The real SQL query never selects the adversarial field; emulate that
    # projection by passing only actual selected aliases and base columns.
    selected = {
        "id": raw["id"],
        "rti_signed_distance_bps": raw["rti_signed_distance_bps"],
        aliases["rti_side"]: raw[aliases["rti_side"]],
    }
    row = materialize_feature_only_row(selected, aliases)
    assert json.loads(row["threshold_json"]) == {"rti_side": "YES"}
    assert "__not_selected_outcome" not in row


def _row(asset: str, *, close: float = 2000.0, exact_offset: float = 0.2):
    captured = close - 780.0 + exact_offset
    return {
        "id": int(close) * 10 + ASSETS.index(asset),
        "bot_name": BOT_RTI_PATH_13M,
        "interval": "13M",
        "record_kind": "RTI_PATH_13M_PROSPECTIVE_EXACT",
        "ticker": f"KX{asset}-FEATURES",
        "asset": asset,
        "close_time": close,
        "source_captured_at": captured,
        "evidence_as_of": captured + 0.3,
        "kalshi_microstructure_schema_version": (
            RTI_EXACT_MICROSTRUCTURE_SCHEMA_VERSION
        ),
        "kalshi_microstructure_captured_at": captured,
        "kalshi_event_count_5s": 0.0,
        "kalshi_trade_count_15s": 0.0,
        "kalshi_taker_net_yes_volume_15s": 0.0,
    }


def test_feature_audit_counts_zero_activity_as_captured_and_keeps_close_fold():
    report = build_report([_row("BTC"), _row("ETH")])
    assert report["outcome_labels_read"] is False
    assert report["microstructure_source"]["rows"] == 2
    assert report["microstructure_v2"]["rows"] == 2
    assert report["microstructure_source"]["rates"]["kalshi_event_count_5s"] == 1.0
    assert report["timestamp_alignment_failures"] == []
    assert report["cross_asset_partial_schema_windows"] == []
    assert report["microstructure_v1_close_windows"] == 1
    assert report["complete_microstructure_v1_close_windows"] == 0
    assert report["complete_windows_required_before_first_feature_review"] == 30
    assert report["rows_required_before_first_feature_review"] == 30


def test_feature_audit_fails_timestamp_and_partial_schema_closed():
    invalid = _row("BTC", exact_offset=2.1)
    legacy = _row("ETH")
    legacy["kalshi_microstructure_schema_version"] = None
    report = build_report([invalid, legacy])
    assert report["ready_for_modeling"] is False
    assert report["timestamp_alignment_failures"][0]["reasons"] == [
        "NOT_EXACT_13M"
    ]
    assert report["cross_asset_partial_schema_windows"] == [{
        "close_time": 2000.0,
        "all_exact_rows": 2,
        "source_schema_rows": 1,
    }]


def test_feature_audit_readiness_counts_independent_complete_windows_not_rows():
    first_review_rows = [
        _row(asset, close=2000.0 + window * 900.0)
        for window in range(30)
        for asset in ASSETS
    ]
    first_review = build_report(first_review_rows)
    assert first_review["microstructure_source"]["rows"] == 210
    assert first_review["complete_microstructure_v1_close_windows"] == 30
    assert first_review["ready_for_first_feature_review"] is True
    assert first_review["ready_for_modeling"] is False
    assert first_review["complete_windows_required_before_modeling"] == 30

    modeling_rows = [
        _row(asset, close=2000.0 + window * 900.0)
        for window in range(60)
        for asset in ASSETS
    ]
    modeling = build_report(modeling_rows)
    assert modeling["complete_microstructure_v1_close_windows"] == 60
    assert modeling["ready_for_first_feature_review"] is True
    assert modeling["ready_for_modeling"] is True
    assert modeling["complete_windows_required_before_modeling"] == 0
