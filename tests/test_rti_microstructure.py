from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from q15_upgrade.strategy_bots.rti_microstructure import (
    DESIGN_SHA256,
    FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION,
    feature_vector,
    model_feature_window_coverage,
)
from q15_upgrade.strategy_bots import rti_microstructure_v2
from q15_upgrade.strategy_bots import rti_microstructure_v3
from q15_upgrade.strategy_bots import rti_microstructure_v4
from q15_upgrade.strategy_bots import rti_microstructure_v5
from q15_upgrade.strategy_bots import rti_microstructure_v6
from q15_upgrade.strategy_bots import rti_microstructure_v7
from q15_upgrade.strategy_bots import rti_microstructure_v8
from q15_upgrade.strategy_bots import rti_microstructure_v9
from q15_upgrade.strategy_bots import rti_microstructure_v10
from q15_upgrade.strategy_bots import rti_microstructure_v11
from q15_upgrade.strategy_bots import rti_microstructure_v12
from q15_upgrade.strategy_bots.rti_microstructure_extension import (
    EXTENSION_SCHEMA_VERSION,
    REQUIRED_METRICS as EXTENSION_REQUIRED_METRICS,
    extension_window_coverage,
)
from tools.q15_rti_microstructure_preregister import design_fingerprint


def _row(**updates):
    row = {
        "asset": "ETH",
        "side": "YES",
        "rti_side": "YES",
        "close_time": 2_000.0,
        "entry_ask_cents": 55.0,
        "spread_cents": 1.0,
        "depth_contracts": 25.0,
        "rti_opposite_ask_cents": 46.0,
        "rti_opposite_depth_contracts": 30.0,
        "rti_market_mid_probability": 0.545,
        "rti_signed_distance_bps": 2.0,
        "rti_side_move_bps": 1.0,
        "rti_path_first_half_side_move_bps": 0.4,
        "rti_path_second_half_side_move_bps": 0.6,
        "rti_path_acceleration_bps": 0.2,
        "rti_path_range_bps": 3.0,
        "rti_path_realized_volatility_bps": 2.0,
        "rti_path_trend_efficiency": 0.5,
        "rti_path_persistence": 0.9,
        "rti_path_strike_crossings": 0,
        "rti_path_seconds_since_last_crossing": None,
        "rti_expected_remaining_volatility_bps": 10.0,
        "rti_distance_to_remaining_volatility": 0.2,
        "spot_depth_imbalance": 0.25,
        "kalshi_yes_microprice_edge_cents": 0.5,
        "kalshi_book_delta_pressure_yes_5s": 0.2,
        "kalshi_book_delta_pressure_yes_15s": 0.1,
        "kalshi_book_delta_pressure_yes_30s": -0.3,
        "kalshi_book_delta_pressure_yes_60s": -0.2,
        "kalshi_trade_imbalance_yes_5s": 0.4,
        "kalshi_trade_imbalance_yes_30s": -0.5,
        "kalshi_taker_yes_volume_5s": 3.0,
        "kalshi_taker_no_volume_5s": 1.0,
        "kalshi_taker_yes_volume_30s": 2.0,
        "kalshi_taker_no_volume_30s": 6.0,
        "kalshi_taker_yes_volume_15s": 5.0,
        "kalshi_taker_no_volume_15s": 3.0,
        "kalshi_taker_yes_volume_60s": 0.0,
        "kalshi_taker_no_volume_60s": 0.0,
        "kalshi_yes_best_depletion_30s": 10.0,
        "kalshi_no_best_depletion_30s": 40.0,
        "kalshi_yes_best_refill_30s": 30.0,
        "kalshi_no_best_refill_30s": 20.0,
        "spot_depth_trade_net_notional_15s": 99.0,
        "spot_depth_trade_net_notional_60s": -999.0,
    }
    row.update(updates)
    return row


def _v4_row(**updates):
    captured = 1220.1
    row = _row(
        kalshi_microstructure_schema_version="rti-exact-microstructure-v2",
        kalshi_microstructure_captured_at=captured,
        kalshi_microstructure_time_basis="local_received_at",
        kalshi_history_count_capped=False,
        kalshi_book_event_retention_seconds=90.0,
        kalshi_trade_retention_seconds=1200.0,
        kalshi_book_history_started_at=captured - 120.0,
        kalshi_trade_history_started_at=captured - 120.0,
        kalshi_book_history_seconds=120.0,
        kalshi_trade_history_seconds=120.0,
        **{
            f"kalshi_{kind}_window_complete_{horizon}s": True
            for kind in ("book", "trade", "microstructure")
            for horizon in (5, 15, 30, 60)
        },
    )
    row.update(updates)
    return row


def _extension_row(**updates):
    row = _v4_row(
        kalshi_microstructure_extension_schema_version=(
            EXTENSION_SCHEMA_VERSION
        ),
    )
    for horizon in (5, 15, 30, 60):
        row[f"kalshi_event_count_{horizon}s"] = 0.0
        row[f"kalshi_trade_count_{horizon}s"] = 0.0
        row[f"kalshi_trade_yes_vwap_cents_{horizon}s"] = None
        for metric in EXTENSION_REQUIRED_METRICS:
            row[f"kalshi_{metric}_{horizon}s"] = 0.0
    row.update(updates)
    return row


def _v5_row(**updates):
    close_time = rti_microstructure_v5.FIRST_ELIGIBLE_CLOSE_TIME
    captured = close_time - 780.0 + 0.1
    row = _extension_row(
        close_time=close_time,
        source_captured_at=captured,
        evidence_as_of=captured + 0.1,
        kalshi_microstructure_captured_at=captured,
        kalshi_book_history_started_at=captured - 120.0,
        kalshi_trade_history_started_at=captured - 120.0,
        kalshi_yes_microprice_cents=55.0,
    )
    row.update(updates)
    return row


def _v6_row(**updates):
    close_time = rti_microstructure_v6.FIRST_ELIGIBLE_CLOSE_TIME
    captured = close_time - 780.0 + 0.1
    spot_captured = captured + 0.05
    row = _v5_row(
        close_time=close_time,
        source_captured_at=captured,
        evidence_as_of=captured + 0.2,
        kalshi_microstructure_captured_at=captured,
        kalshi_book_history_started_at=captured - 120.0,
        kalshi_trade_history_started_at=captured - 120.0,
        spot_mid_path_schema_version="spot-mid-path-local-v1",
        spot_mid_path_time_basis="local_created_at",
        spot_mid_path_captured_at=spot_captured,
        spot_mid_history_started_at=spot_captured - 120.0,
        spot_mid_history_seconds=120.0,
        spot_mid_history_retention_seconds=180.0,
        spot_mid_record_interval_seconds=5.0,
        spot_mid_window_complete_15s=True,
        spot_mid_window_complete_60s=True,
        spot_mid_path_start_at_60s=spot_captured - 60.0,
        spot_mid_path_end_at_60s=spot_captured,
        spot_mid_path_max_gap_seconds_60s=5.0,
        spot_mid_change_bps_15s=2.0,
        spot_mid_change_bps_60s=5.0,
        spot_mid_range_bps_60s=7.0,
        spot_mid_realized_volatility_bps_60s=8.0,
        spot_mid_trend_efficiency_60s=0.4,
        rti_spot_lead_lag_schema_version="rti-spot-index-lead-lag-v1",
        rti_spot_lead_lag_status="ok",
        rti_spot_basis_bps=1.5,
        rti_spot_minus_index_momentum_bps_60s=3.0,
    )
    row.update(updates)
    return row


def _v7_row(**updates):
    close_time = rti_microstructure_v7.FIRST_ELIGIBLE_CLOSE_TIME
    captured = close_time - 780.0 + 0.1
    spot_captured = captured + 0.05
    row = _v6_row(
        close_time=close_time,
        source_captured_at=captured,
        evidence_as_of=captured + 0.2,
        kalshi_microstructure_captured_at=captured,
        kalshi_book_history_started_at=captured - 120.0,
        kalshi_trade_history_started_at=captured - 120.0,
        spot_mid_path_captured_at=spot_captured,
        spot_mid_history_started_at=spot_captured - 120.0,
        spot_mid_path_start_at_60s=spot_captured - 60.0,
        spot_mid_path_end_at_60s=spot_captured,
        rti_cross_venue_schema_version="rti-cross-venue-consensus-v1",
        rti_cross_venue_time_basis="local_created_at",
        rti_cross_venue_status="ok",
        rti_cross_venue_evidence_cutoff_at=captured,
        rti_cross_venue_max_lag_seconds=10.0,
        rti_cross_venue_available_count=2,
        rti_cross_venue_consensus_change_bps_15s=2.5,
        rti_cross_venue_consensus_change_bps_60s=4.0,
        rti_cross_venue_primary_minus_consensus_bps_60s=1.0,
        rti_cross_venue_momentum_spread_bps_60s=3.0,
        rti_cross_venue_direction_agreement_60s=1.0,
        rti_cross_venue_current_divergence_bps=2.0,
        rti_cross_venue_primary_basis_bps=-1.5,
    )
    for venue, mid in (("coinbase", 100.0), ("kraken", 100.02)):
        row[f"rti_cross_venue_{venue}_status"] = "ok"
        row[f"rti_cross_venue_{venue}_snapshot_created_at"] = captured - 1.0
        row[f"rti_cross_venue_{venue}_snapshot_age_seconds"] = 1.0
        row[f"rti_cross_venue_{venue}_message_age_seconds"] = 1.2
        row[f"rti_cross_venue_{venue}_mid"] = mid
        for horizon in (15, 60):
            row[f"rti_cross_venue_{venue}_start_created_at_{horizon}s"] = (
                captured - horizon - 1.0
            )
            row[f"rti_cross_venue_{venue}_start_age_seconds_{horizon}s"] = 1.0
            row[f"rti_cross_venue_{venue}_start_mid_{horizon}s"] = mid - 0.01
            row[f"rti_cross_venue_{venue}_change_bps_{horizon}s"] = 1.0
    row.update(updates)
    return row


def _v8_row(**updates):
    close = rti_microstructure_v8.FIRST_ELIGIBLE_CLOSE_TIME
    captured = close - 780.0 + 0.1
    row = _v5_row(
        close_time=close,
        source_captured_at=captured,
        evidence_as_of=captured + 0.2,
        kalshi_microstructure_captured_at=captured,
        kalshi_book_history_started_at=captured - 120.0,
        kalshi_trade_history_started_at=captured - 120.0,
        rti_path_start_px=100.0,
        rti_path_end_px=100.02,
        rti_independent_venue_schema_version=(
            "rti-independent-venue-consensus-v1"
        ),
        rti_independent_venue_time_basis="local_created_at",
        rti_independent_venue_status="ok",
        rti_independent_venue_evidence_cutoff_at=captured,
        rti_independent_venue_max_lag_seconds=10.0,
        rti_independent_venue_available_count=2,
        rti_independent_venue_consensus_mid=100.04,
        rti_independent_venue_consensus_start_mid_60s=100.0,
        rti_independent_venue_consensus_change_bps_15s=2.0,
        rti_independent_venue_consensus_change_bps_60s=4.0,
        rti_independent_venue_momentum_spread_bps_60s=3.0,
        rti_independent_venue_direction_agreement_60s=1.0,
        rti_independent_venue_current_divergence_bps=2.0,
    )
    for venue, mid in (("coinbase", 100.03), ("kraken", 100.05)):
        row[f"rti_independent_venue_{venue}_status"] = "ok"
        row[f"rti_independent_venue_{venue}_snapshot_created_at"] = captured - 1.0
        row[f"rti_independent_venue_{venue}_snapshot_age_seconds"] = 1.0
        row[f"rti_independent_venue_{venue}_message_age_seconds"] = 1.2
        row[f"rti_independent_venue_{venue}_mid"] = mid
        for horizon in (15, 60):
            row[f"rti_independent_venue_{venue}_start_created_at_{horizon}s"] = captured - horizon - 1.0
            row[f"rti_independent_venue_{venue}_start_age_seconds_{horizon}s"] = 1.0
            row[f"rti_independent_venue_{venue}_start_mid_{horizon}s"] = mid - 0.04
            row[f"rti_independent_venue_{venue}_change_bps_{horizon}s"] = 4.0
    row.update(updates)
    return row


def _v9_row(**updates):
    close = rti_microstructure_v9.FIRST_ELIGIBLE_CLOSE_TIME
    captured = close - 780.0 + 0.1
    row = _v8_row(
        close_time=close,
        source_captured_at=captured,
        evidence_as_of=captured + 0.2,
        kalshi_microstructure_captured_at=captured,
        kalshi_book_history_started_at=captured - 120.0,
        kalshi_trade_history_started_at=captured - 120.0,
        rti_independent_venue_evidence_cutoff_at=captured,
        rti_independent_microstructure_schema_version=(
            "rti-independent-venue-microstructure-v2"
        ),
        rti_independent_microstructure_time_basis="local_created_at",
        rti_independent_microstructure_status="ok",
        rti_independent_microstructure_evidence_cutoff_at=captured,
        rti_independent_microstructure_max_lag_seconds=10.0,
        rti_independent_microstructure_available_count=2,
        rti_independent_microstructure_mean_depth_imbalance=0.3,
        rti_independent_microstructure_depth_imbalance_disagreement=0.2,
        rti_independent_microstructure_mean_depth_imbalance_change_60s=0.3,
        rti_independent_microstructure_mean_spread_bps=3.0,
        rti_independent_microstructure_max_spread_bps=4.0,
        rti_independent_microstructure_coinbase_remove_share_15s=0.25,
        rti_independent_microstructure_kraken_delete_share_15s=0.25,
        rti_independent_microstructure_kraken_partial_fill_aggressor_imbalance_60s=0.5,
        rti_independent_microstructure_kraken_partial_fill_notional_60s=80.0,
        rti_independent_microstructure_kraken_partial_fill_observed_60s=1.0,
        rti_independent_microstructure_kraken_partial_fill_flow_schema_version=(
            "kraken-l3-partial-fill-flow-v1"
        ),
    )
    for venue in ("coinbase", "kraken"):
        row[f"rti_independent_venue_{venue}_snapshot_created_at"] = captured - 1.0
        for horizon in (15, 60):
            row[f"rti_independent_venue_{venue}_start_created_at_{horizon}s"] = (
                captured - horizon - 1.0
            )
        prefix = f"rti_independent_microstructure_{venue}"
        row[f"{prefix}_status"] = "ok"
        row[f"{prefix}_snapshot_created_at"] = captured - 1.0
        row[f"{prefix}_snapshot_age_seconds"] = 1.0
        row[f"{prefix}_message_age_seconds"] = 1.2
        row[f"{prefix}_start_created_at_60s"] = captured - 61.0
        row[f"{prefix}_start_age_seconds_60s"] = 1.0
        row[f"{prefix}_start_message_age_seconds_60s"] = 1.2
        row[f"{prefix}_summary_level_limit"] = 10.0
        row[f"{prefix}_start_summary_level_limit_60s"] = 10.0
    row.update({
        "rti_independent_microstructure_coinbase_bid_notional_levels": 1500.0,
        "rti_independent_microstructure_coinbase_ask_notional_levels": 1000.0,
        "rti_independent_microstructure_kraken_bid_notional_levels": 800.0,
        "rti_independent_microstructure_kraken_ask_notional_levels": 700.0,
    })
    row.update(updates)
    return row


def _v10_row(**updates):
    close = rti_microstructure_v10.FIRST_ELIGIBLE_CLOSE_TIME
    captured = close - 780.0 + 0.1
    row = _v9_row(
        close_time=close,
        source_captured_at=captured,
        evidence_as_of=captured + 0.2,
        kalshi_microstructure_captured_at=captured,
        kalshi_book_history_started_at=captured - 120.0,
        kalshi_trade_history_started_at=captured - 120.0,
        rti_independent_venue_evidence_cutoff_at=captured,
        rti_independent_microstructure_evidence_cutoff_at=captured,
    )
    for venue in ("coinbase", "kraken"):
        row[f"rti_independent_venue_{venue}_snapshot_created_at"] = captured - 1.0
        for horizon in (15, 60):
            row[f"rti_independent_venue_{venue}_start_created_at_{horizon}s"] = (
                captured - horizon - 1.0
            )
        prefix = f"rti_independent_microstructure_{venue}"
        row[f"{prefix}_snapshot_created_at"] = captured - 1.0
        row[f"{prefix}_start_created_at_60s"] = captured - 61.0
    row.update(updates)
    return row


def _v11_row(**updates):
    close = rti_microstructure_v11.FIRST_ELIGIBLE_CLOSE_TIME
    captured = close - 780.0 + 0.1
    row = _v10_row(
        close_time=close,
        source_captured_at=captured,
        evidence_as_of=captured + 0.2,
        kalshi_microstructure_captured_at=captured,
        kalshi_book_history_started_at=captured - 120.0,
        kalshi_trade_history_started_at=captured - 120.0,
        rti_independent_venue_evidence_cutoff_at=captured,
        rti_independent_microstructure_evidence_cutoff_at=captured,
        rti_cross_asset_schema_version="rti-cross-asset-regime-v1",
        rti_cross_asset_time_basis="local_created_at",
        rti_cross_asset_status="ok",
        rti_cross_asset_evidence_cutoff_at=captured,
        rti_cross_asset_max_lag_seconds=10.0,
        rti_cross_asset_required_asset_count=7,
        rti_cross_asset_available_asset_count=7,
        rti_cross_asset_latest_snapshot_created_at=captured - 1.0,
        rti_cross_asset_max_snapshot_age_seconds=1.0,
        rti_cross_asset_max_message_age_seconds=1.2,
    )
    for venue in ("coinbase", "kraken"):
        row[f"rti_independent_venue_{venue}_snapshot_created_at"] = captured - 1.0
        for horizon in (15, 60):
            row[f"rti_independent_venue_{venue}_start_created_at_{horizon}s"] = (
                captured - horizon - 1.0
            )
        prefix = f"rti_independent_microstructure_{venue}"
        row[f"{prefix}_snapshot_created_at"] = captured - 1.0
        row[f"{prefix}_start_created_at_60s"] = captured - 61.0
    asset_moves = {
        "bnb": -3.0, "btc": -2.0, "doge": -1.0, "eth": 0.0,
        "hype": 1.0, "sol": 2.0, "xrp": 3.0,
    }
    for horizon in (15, 60):
        row.update({
            f"rti_cross_asset_latest_start_created_at_{horizon}s": (
                captured - horizon - 1.0
            ),
            f"rti_cross_asset_max_start_age_seconds_{horizon}s": 1.0,
            f"rti_cross_asset_max_start_message_age_seconds_{horizon}s": 1.2,
            f"rti_cross_asset_median_momentum_bps_{horizon}s": 0.0,
            f"rti_cross_asset_breadth_signed_{horizon}s": 0.0,
            f"rti_cross_asset_dispersion_mad_bps_{horizon}s": 2.0,
            f"rti_cross_asset_btc_minus_non_btc_median_bps_{horizon}s": -2.5,
            f"rti_cross_asset_asset_centered_rank_{horizon}s": 0.0,
            f"rti_cross_asset_asset_btc_direction_agreement_{horizon}s": 0.5,
        })
        for asset, move in asset_moves.items():
            row[f"rti_cross_asset_{asset}_consensus_change_bps_{horizon}s"] = move
            row[f"rti_cross_asset_coinbase_{asset}_change_bps_{horizon}s"] = move
            row[f"rti_cross_asset_kraken_{asset}_change_bps_{horizon}s"] = move
    row.update(updates)
    return row


def _v12_row(**updates):
    close = rti_microstructure_v12.FIRST_ELIGIBLE_CLOSE_TIME
    captured = close - 780.0 + 0.1
    row = _v11_row(
        close_time=close,
        source_captured_at=captured,
        evidence_as_of=captured + 0.2,
        quote_captured_at=captured,
        kalshi_microstructure_captured_at=captured,
        kalshi_book_history_started_at=captured - 120.0,
        kalshi_trade_history_started_at=captured - 120.0,
        rti_evaluated_at=captured + 0.2,
        rti_independent_venue_evidence_cutoff_at=captured,
        rti_independent_microstructure_evidence_cutoff_at=captured,
        rti_cross_asset_evidence_cutoff_at=captured,
        rti_cross_asset_latest_snapshot_created_at=captured - 1.0,
    )
    for venue in ("coinbase", "kraken"):
        row[f"rti_independent_venue_{venue}_snapshot_created_at"] = captured - 1.0
        for horizon in (15, 60):
            row[f"rti_independent_venue_{venue}_start_created_at_{horizon}s"] = (
                captured - horizon - 1.0
            )
        prefix = f"rti_independent_microstructure_{venue}"
        row[f"{prefix}_snapshot_created_at"] = captured - 1.0
        row[f"{prefix}_start_created_at_60s"] = captured - 61.0
    for horizon in (15, 60):
        row[f"rti_cross_asset_latest_start_created_at_{horizon}s"] = (
            captured - horizon - 1.0
        )
    row.update(updates)
    asset_moves = {
        "BNB": -3.0, "BTC": -2.0, "DOGE": -1.0, "ETH": 0.0,
        "HYPE": 1.0, "SOL": 2.0, "XRP": 3.0,
    }
    target_move = asset_moves[str(row["asset"]).upper()]
    centered_rank = (
        sum(value < target_move for value in asset_moves.values())
        - sum(value > target_move for value in asset_moves.values())
    ) / 6.0
    agreement = (
        0.5 if target_move == 0.0
        else 1.0 if target_move < 0.0
        else 0.0
    )
    for horizon in (15, 60):
        row[f"rti_cross_asset_asset_centered_rank_{horizon}s"] = centered_rank
        row[
            f"rti_cross_asset_asset_btc_direction_agreement_{horizon}s"
        ] = agreement
    return row


def test_feature_names_and_fingerprint_match_the_preregistered_manifest():
    path = Path("config/q15_rti_microstructure_design_v1.json")
    design = json.loads(path.read_text(encoding="utf-8"))
    assert tuple(design["feature_names"]) == FEATURE_NAMES
    assert design["feature_schema_version"] == FEATURE_SCHEMA_VERSION
    assert design_fingerprint(design) == DESIGN_SHA256


def test_v2_manifest_replaces_only_the_two_exact_duplicate_features():
    path = Path("config/q15_rti_microstructure_design_v2.json")
    design = json.loads(path.read_text(encoding="utf-8"))
    assert tuple(design["feature_names"]) == rti_microstructure_v2.FEATURE_NAMES
    assert design_fingerprint(design) == rti_microstructure_v2.DESIGN_SHA256
    assert "kalshi_taker_imbalance_yes_5s" not in design["feature_names"]
    assert "kalshi_taker_imbalance_yes_30s" not in design["feature_names"]
    assert "kalshi_book_delta_pressure_yes_15s" in design["feature_names"]
    assert "kalshi_book_delta_pressure_yes_60s" in design["feature_names"]
    assert design["outcome_labels_used_for_change"] is False


def test_v2_vector_preserves_width_and_uses_independent_book_horizons():
    result = rti_microstructure_v2.feature_vector(_row())
    assert result["available"] is True
    values = dict(zip(rti_microstructure_v2.FEATURE_NAMES, result["features"]))
    assert len(values) == 33
    assert values["kalshi_book_delta_pressure_yes_15s"] == pytest.approx(0.1)
    assert values["kalshi_book_delta_pressure_yes_60s"] == pytest.approx(-0.2)
    assert values["kalshi_taker_imbalance_yes_60s"] == 0.0
    assert values["kalshi_microstructure_missing"] == 0.0


def test_v3_manifest_and_vector_remove_the_btc_event_cap_duplicate():
    path = Path("config/q15_rti_microstructure_design_v3.json")
    design = json.loads(path.read_text(encoding="utf-8"))
    assert tuple(design["feature_names"]) == rti_microstructure_v3.FEATURE_NAMES
    assert design_fingerprint(design) == rti_microstructure_v3.DESIGN_SHA256
    assert "kalshi_book_delta_pressure_yes_60s" not in design["feature_names"]
    assert "kalshi_taker_imbalance_yes_15s" in design["feature_names"]
    assert design["outcome_labels_used_for_change"] is False
    result = rti_microstructure_v3.feature_vector(_row())
    values = dict(zip(rti_microstructure_v3.FEATURE_NAMES, result["features"]))
    assert len(values) == 33
    assert values["kalshi_taker_imbalance_yes_15s"] == pytest.approx(0.25)
    assert values["kalshi_microstructure_missing"] == 0.0


def test_v4_manifest_is_pinned_to_post_fix_receive_time_rows_only():
    path = Path("config/q15_rti_microstructure_design_v4.json")
    design = json.loads(path.read_text(encoding="utf-8"))
    assert tuple(design["feature_names"]) == rti_microstructure_v4.FEATURE_NAMES
    assert design_fingerprint(design) == rti_microstructure_v4.DESIGN_SHA256
    assert design["source_schema"] == "rti-exact-microstructure-v2"
    assert design["source_time_basis"] == "local_received_at"
    assert design["post_fix_history_only"] is True
    assert design["prior_schema_rows_credited"] is False
    assert design["outcome_labels_used_for_change"] is False


def test_v4_vector_requires_independently_proven_complete_horizons():
    result = rti_microstructure_v4.feature_vector(_v4_row())
    assert result["available"] is True
    values = dict(zip(rti_microstructure_v4.FEATURE_NAMES, result["features"]))
    assert len(values) == 32
    assert "kalshi_microstructure_missing" not in values
    assert values["kalshi_book_delta_pressure_yes_15s"] == pytest.approx(0.1)
    assert values["kalshi_taker_imbalance_yes_15s"] == pytest.approx(0.25)

    capped = rti_microstructure_v4.feature_vector(_v4_row(
        kalshi_history_count_capped=True,
    ))
    assert capped == {
        "available": False,
        "error": "count_cap_not_explicitly_disabled",
    }
    incomplete = rti_microstructure_v4.feature_vector(_v4_row(
        kalshi_book_window_complete_30s=False,
    ))
    assert incomplete == {
        "available": False,
        "error": "book_window_incomplete_30s",
    }
    contradicted = rti_microstructure_v4.feature_vector(_v4_row(
        kalshi_book_history_started_at=1170.1,
    ))
    assert contradicted == {
        "available": False,
        "error": "microstructure_history_timestamp_contradiction",
    }
    missing = rti_microstructure_v4.feature_vector(_v4_row(
        kalshi_trade_imbalance_yes_30s=None,
    ))
    assert missing == {
        "available": False,
        "error": "required_feature_missing:kalshi_trade_imbalance_yes_30s",
    }


def test_v4_outcome_fields_cannot_change_features():
    original = rti_microstructure_v4.feature_vector(_v4_row())
    poisoned = rti_microstructure_v4.feature_vector(_v4_row(
        official_result="NO",
        correct=0,
        hypothetical_pnl_cents=-55.0,
    ))
    assert poisoned["available"] is True
    assert poisoned["features"] == original["features"]


def test_v5_manifest_is_pinned_beyond_the_outcome_blind_boundary():
    path = Path("config/q15_rti_microstructure_design_v5.json")
    design = json.loads(path.read_text(encoding="utf-8"))
    assert tuple(design["feature_names"]) == rti_microstructure_v5.FEATURE_NAMES
    assert design_fingerprint(design) == rti_microstructure_v5.DESIGN_SHA256
    assert design["source_extension_schema"] == EXTENSION_SCHEMA_VERSION
    assert design["pre_freeze_extension_rows_credited"] is False
    assert design["outcome_labels_used_for_change"] is False
    assert design["prospective_after_close_time"] == (
        rti_microstructure_v5.PROSPECTIVE_AFTER_CLOSE_TIME
    )
    assert design["first_eligible_close_time"] == (
        rti_microstructure_v5.FIRST_ELIGIBLE_CLOSE_TIME
    )


def test_v5_vector_compresses_queue_microprice_and_trade_dynamics():
    row = _v5_row(
        kalshi_book_add_volume_yes_5s=40.0,
        kalshi_book_remove_volume_yes_5s=10.0,
        kalshi_book_add_volume_no_5s=20.0,
        kalshi_book_remove_volume_no_5s=30.0,
        kalshi_book_add_volume_yes_30s=100.0,
        kalshi_book_remove_volume_yes_30s=100.0,
        kalshi_book_add_volume_no_30s=100.0,
        kalshi_book_remove_volume_no_30s=100.0,
        kalshi_microprice_change_cents_5s=5.0,
        kalshi_microprice_change_cents_30s=6.0,
        kalshi_microprice_trend_efficiency_30s=0.5,
        kalshi_microprice_range_cents_30s=9.0,
        kalshi_trade_yes_price_change_cents_5s=-5.0,
        kalshi_trade_yes_price_change_cents_30s=3.0,
        kalshi_trade_yes_price_trend_efficiency_30s=0.25,
        kalshi_event_count_30s=90.0,
        kalshi_trade_count_30s=10.0,
        kalshi_trade_yes_vwap_cents_30s=49.0,
    )
    result = rti_microstructure_v5.feature_vector(row)
    assert result["available"] is True
    values = dict(zip(rti_microstructure_v5.FEATURE_NAMES, result["features"]))
    assert len(values) == 46
    assert values["kalshi_queue_pressure_yes_5s"] == pytest.approx(0.4)
    assert values["kalshi_queue_pressure_yes_30s"] == 0.0
    assert values["kalshi_queue_pressure_acceleration_yes"] == pytest.approx(0.4)
    assert values["kalshi_microprice_velocity_yes_5s"] == 1.0
    assert values["kalshi_microprice_velocity_yes_30s"] == pytest.approx(0.2)
    assert values["kalshi_microprice_velocity_acceleration_yes"] == pytest.approx(0.8)
    assert values["kalshi_microprice_directional_efficiency_yes_30s"] == 0.5
    assert values["kalshi_log1p_microprice_range_cents_30s"] == pytest.approx(math.log(10.0))
    assert values["kalshi_trade_price_velocity_yes_30s"] == pytest.approx(0.1)
    assert values["kalshi_trade_price_velocity_acceleration_yes"] == pytest.approx(-1.1)
    assert values["kalshi_trade_price_directional_efficiency_yes_30s"] == 0.25
    assert values["kalshi_microprice_minus_trade_vwap_cents_30s"] == 6.0
    assert values["kalshi_trade_share_of_updates_30s"] == pytest.approx(0.1)
    assert values["kalshi_trade_vwap_missing_30s"] == 0.0


def test_v5_fails_closed_and_never_reads_outcomes():
    clean = rti_microstructure_v5.feature_vector(_v5_row())
    assert clean["available"] is True
    poisoned = rti_microstructure_v5.feature_vector(_v5_row(
        official_result="NO",
        correct=0,
        hypothetical_pnl_cents=-999.0,
    ))
    assert poisoned["features"] == clean["features"]

    assert rti_microstructure_v5.feature_vector(_v5_row(
        close_time=rti_microstructure_v5.PROSPECTIVE_AFTER_CLOSE_TIME,
    )) == {"available": False, "error": "pre_v5_prospective_boundary"}
    assert rti_microstructure_v5.feature_vector(_v5_row(
        kalshi_microstructure_extension_schema_version="wrong",
    )) == {"available": False, "error": "extension_schema_mismatch"}
    missing = rti_microstructure_v5.feature_vector(_v5_row(
        kalshi_microprice_variation_cents_15s=None,
    ))
    assert missing == {
        "available": False,
        "error": "required_extension_feature_missing:microprice_variation_cents_15s",
    }
    missing_vwap = rti_microstructure_v5.feature_vector(_v5_row(
        kalshi_trade_count_5s=1.0,
        kalshi_trade_yes_vwap_cents_5s=None,
    ))
    assert missing_vwap == {
        "available": False,
        "error": "trade_vwap_missing_with_trades_5s",
    }


def test_v5_coverage_credits_only_post_boundary_complete_folds():
    assets = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
    rows = [
        _v5_row(
            id=index + 1,
            asset=asset,
            bot_name="rti_path_13m",
            interval="13M",
            record_kind="RTI_PATH_13M_PROSPECTIVE_EXACT",
        )
        for index, asset in enumerate(assets)
    ]
    prefreeze = [dict(
        row,
        id=int(row["id"]) + 20,
        close_time=rti_microstructure_v5.PROSPECTIVE_AFTER_CLOSE_TIME,
    ) for row in rows]
    clean = rti_microstructure_v5.model_feature_window_coverage([
        *prefreeze, *rows,
    ])
    assert clean["schema_complete_model_candidate_close_windows"] == 1
    assert clean["complete_model_feature_close_windows"] == 1
    assert clean["model_feature_unavailable_rows"] == []

    rows[0]["kalshi_microstructure_extension_schema_version"] = "wrong"
    incomplete = rti_microstructure_v5.model_feature_window_coverage(rows)
    assert incomplete["schema_complete_model_candidate_close_windows"] == 0


def test_v6_manifest_is_pinned_to_post_boundary_local_spot_paths():
    path = Path("config/q15_rti_microstructure_design_v6.json")
    design = json.loads(path.read_text(encoding="utf-8"))
    assert tuple(design["feature_names"]) == rti_microstructure_v6.FEATURE_NAMES
    assert design_fingerprint(design) == rti_microstructure_v6.DESIGN_SHA256
    assert design["source_spot_path_schema"] == "spot-mid-path-local-v1"
    assert design["source_lead_lag_schema"] == "rti-spot-index-lead-lag-v1"
    assert design["pre_freeze_spot_path_rows_credited"] is False
    assert design["outcome_labels_used_for_change"] is False


def test_v6_vector_adds_compact_spot_index_lead_lag_features():
    result = rti_microstructure_v6.feature_vector(_v6_row())
    assert result["available"] is True
    values = dict(zip(rti_microstructure_v6.FEATURE_NAMES, result["features"]))
    assert len(values) == 53
    assert values["spot_index_basis_current_bps"] == 1.5
    assert values["spot_minus_index_momentum_60s_bps"] == 3.0
    assert values["spot_mid_momentum_15s_bps"] == 2.0
    assert values["spot_mid_momentum_60s_bps"] == 5.0
    assert values["log1p_spot_mid_range_bps_60s"] == pytest.approx(math.log(8.0))
    assert values[
        "log1p_spot_mid_realized_volatility_bps_60s"
    ] == pytest.approx(math.log(9.0))
    assert values["spot_mid_trend_efficiency_60s"] == 0.4


def test_v6_is_outcome_blind_and_fails_closed_on_spot_path_integrity():
    clean = rti_microstructure_v6.feature_vector(_v6_row())
    poisoned = rti_microstructure_v6.feature_vector(_v6_row(
        official_result="NO", correct=0, hypothetical_pnl_cents=-999.0,
    ))
    assert poisoned["features"] == clean["features"]
    assert rti_microstructure_v6.feature_vector(_v6_row(
        close_time=rti_microstructure_v6.PROSPECTIVE_AFTER_CLOSE_TIME,
    )) == {"available": False, "error": "pre_v6_prospective_boundary"}
    assert rti_microstructure_v6.feature_vector(_v6_row(
        spot_mid_path_schema_version="wrong",
    )) == {"available": False, "error": "spot_path_schema_mismatch"}
    contradicted = rti_microstructure_v6.feature_vector(_v6_row(
        spot_mid_path_end_at_60s=0.0,
    ))
    assert contradicted == {
        "available": False,
        "error": "spot_path_window_end_contradiction",
    }
    unavailable = rti_microstructure_v6.feature_vector(_v6_row(
        rti_spot_lead_lag_status="missing",
    ))
    assert unavailable == {
        "available": False,
        "error": "lead_lag_status_not_ok",
    }


def test_v6_coverage_never_credits_preboundary_or_partial_folds():
    assets = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
    rows = [
        _v6_row(
            id=index + 1,
            asset=asset,
            bot_name="rti_path_13m",
            interval="13M",
            record_kind="RTI_PATH_13M_PROSPECTIVE_EXACT",
        )
        for index, asset in enumerate(assets)
    ]
    prefreeze = [dict(
        row,
        id=int(row["id"]) + 20,
        close_time=rti_microstructure_v6.PROSPECTIVE_AFTER_CLOSE_TIME,
    ) for row in rows]
    clean = rti_microstructure_v6.model_feature_window_coverage([
        *prefreeze, *rows,
    ])
    assert clean["schema_complete_model_candidate_close_windows"] == 1
    assert clean["complete_model_feature_close_windows"] == 1
    assert clean["model_feature_unavailable_rows"] == []
    rows.pop()
    partial = rti_microstructure_v6.model_feature_window_coverage(rows)
    assert partial["schema_complete_model_candidate_close_windows"] == 0


def test_v7_manifest_and_vector_are_pinned_before_cross_venue_rows():
    design = json.loads(Path(
        "config/q15_rti_microstructure_design_v7.json"
    ).read_text(encoding="utf-8"))
    assert tuple(design["feature_names"]) == rti_microstructure_v7.FEATURE_NAMES
    assert design_fingerprint(design) == rti_microstructure_v7.DESIGN_SHA256
    assert design["pre_freeze_cross_venue_rows_credited"] is False
    assert design["outcome_labels_used_for_change"] is False

    result = rti_microstructure_v7.feature_vector(_v7_row())
    assert result["available"] is True
    values = dict(zip(rti_microstructure_v7.FEATURE_NAMES, result["features"]))
    assert len(values) == 60
    assert values["cross_venue_consensus_momentum_15s_bps"] == 2.5
    assert values["cross_venue_consensus_momentum_60s_bps"] == 4.0
    assert values["primary_minus_cross_venue_momentum_60s_bps"] == 1.0
    assert values[
        "log1p_cross_venue_momentum_spread_60s_bps"
    ] == pytest.approx(math.log(4.0))
    assert values["cross_venue_direction_agreement_60s"] == 1.0
    assert values[
        "log1p_cross_venue_current_divergence_bps"
    ] == pytest.approx(math.log(3.0))
    assert values["primary_cross_venue_basis_bps"] == -1.5


def test_v7_is_outcome_blind_and_fails_closed_on_timestamp_contradictions():
    clean = rti_microstructure_v7.feature_vector(_v7_row())
    poisoned = rti_microstructure_v7.feature_vector(_v7_row(
        official_result="NO", correct=0, hypothetical_pnl_cents=-999.0,
    ))
    assert poisoned["features"] == clean["features"]
    assert rti_microstructure_v7.feature_vector(_v7_row(
        close_time=rti_microstructure_v7.PROSPECTIVE_AFTER_CLOSE_TIME,
    )) == {"available": False, "error": "pre_v7_prospective_boundary"}
    assert rti_microstructure_v7.feature_vector(_v7_row(
        rti_cross_venue_coinbase_snapshot_created_at=9e12,
    )) == {"available": False, "error": "cross_venue_coinbase_future_endpoint"}
    assert rti_microstructure_v7.feature_vector(_v7_row(
        rti_cross_venue_status="missing",
    )) == {"available": False, "error": "cross_venue_status_not_ok"}


def test_v7_coverage_requires_a_complete_seven_asset_fold():
    assets = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
    rows = [
        _v7_row(
            id=index + 1,
            asset=asset,
            bot_name="rti_path_13m",
            interval="13M",
            record_kind="RTI_PATH_13M_PROSPECTIVE_EXACT",
        )
        for index, asset in enumerate(assets)
    ]
    clean = rti_microstructure_v7.model_feature_window_coverage(rows)
    assert clean["complete_model_feature_close_windows"] == 1
    rows.pop()
    partial = rti_microstructure_v7.model_feature_window_coverage(rows)
    assert partial["complete_model_feature_close_windows"] == 0


def test_v8_branches_from_v5_and_does_not_require_primary_spot_path():
    design = json.loads(Path(
        "config/q15_rti_microstructure_design_v8.json"
    ).read_text(encoding="utf-8"))
    assert tuple(design["feature_names"]) == rti_microstructure_v8.FEATURE_NAMES
    assert design_fingerprint(design) == rti_microstructure_v8.DESIGN_SHA256
    assert design["required_source_integrity"][
        "single_primary_spot_path_required"
    ] is False
    result = rti_microstructure_v8.feature_vector(_v8_row(
        spot_depth_status="missing",
        spot_mid_path_schema_version=None,
        rti_spot_lead_lag_schema_version=None,
    ))
    assert result["available"] is True
    values = dict(zip(rti_microstructure_v8.FEATURE_NAMES, result["features"]))
    assert len(values) == 53
    assert values["independent_index_basis_current_bps"] == pytest.approx(
        (100.04 - 100.02) / 100.02 * 10_000.0
    )
    assert values[
        "independent_minus_index_momentum_60s_bps"
    ] == pytest.approx(
        (100.04 - 100.0) / 100.0 * 10_000.0
        - (100.02 - 100.0) / 100.0 * 10_000.0
    )
    assert values["independent_consensus_momentum_15s_bps"] == 2.0
    assert values["independent_consensus_momentum_60s_bps"] == 4.0
    assert values[
        "log1p_independent_momentum_spread_60s_bps"
    ] == pytest.approx(math.log(4.0))


def test_v8_is_outcome_blind_and_rejects_future_independent_endpoints():
    clean = rti_microstructure_v8.feature_vector(_v8_row())
    poisoned = rti_microstructure_v8.feature_vector(_v8_row(
        official_result="NO", correct=0, hypothetical_pnl_cents=-999.0,
    ))
    assert poisoned["features"] == clean["features"]
    assert rti_microstructure_v8.feature_vector(_v8_row(
        close_time=rti_microstructure_v8.PROSPECTIVE_AFTER_CLOSE_TIME,
    )) == {"available": False, "error": "pre_v8_prospective_boundary"}
    assert rti_microstructure_v8.feature_vector(_v8_row(
        rti_independent_venue_kraken_snapshot_created_at=9e12,
    )) == {"available": False, "error": "independent_venue_kraken_future_endpoint"}


def test_v9_manifest_and_independent_microstructure_vector_are_pinned():
    design = json.loads(Path(
        "config/q15_rti_microstructure_design_v9.json"
    ).read_text(encoding="utf-8"))
    assert tuple(design["feature_names"]) == rti_microstructure_v9.FEATURE_NAMES
    assert design_fingerprint(design) == rti_microstructure_v9.DESIGN_SHA256
    assert design["outcome_labels_used_for_change"] is False
    assert design["ambiguous_kraken_deletes_as_trades_forbidden"] is True

    result = rti_microstructure_v9.feature_vector(_v9_row())
    assert result["available"] is True
    values = dict(zip(rti_microstructure_v9.FEATURE_NAMES, result["features"]))
    assert len(values) == 65
    assert values["independent_mean_depth_imbalance"] == 0.3
    assert values["independent_depth_imbalance_disagreement"] == 0.2
    assert values["independent_mean_depth_imbalance_change_60s"] == 0.3
    assert values["independent_mean_spread_bps"] == 3.0
    assert values["independent_max_spread_bps"] == 4.0
    assert values["coinbase_remove_share_15s"] == 0.25
    assert values["kraken_delete_share_15s"] == 0.25
    assert values[
        "kraken_partial_fill_aggressor_imbalance_60s"
    ] == 0.5
    assert values[
        "kraken_log1p_partial_fill_notional_60s"
    ] == pytest.approx(math.log(81.0))
    assert values["kraken_partial_fill_observed_60s"] == 1.0


def test_v9_is_outcome_blind_and_rejects_future_microstructure_endpoints():
    clean = rti_microstructure_v9.feature_vector(_v9_row())
    poisoned = rti_microstructure_v9.feature_vector(_v9_row(
        official_result="NO", correct=0, hypothetical_pnl_cents=-999.0,
    ))
    assert poisoned["features"] == clean["features"]
    assert rti_microstructure_v9.feature_vector(_v9_row(
        close_time=rti_microstructure_v9.PROSPECTIVE_AFTER_CLOSE_TIME,
    )) == {"available": False, "error": "pre_v9_prospective_boundary"}
    assert rti_microstructure_v9.feature_vector(_v9_row(
        rti_independent_microstructure_kraken_snapshot_created_at=9e12,
    )) == {
        "available": False,
        "error": "independent_microstructure_kraken_future_endpoint",
    }


def test_v10_compact_manifest_preserves_every_nonduplicate_v9_value():
    design = json.loads(Path(
        "config/q15_rti_microstructure_design_v10.json"
    ).read_text(encoding="utf-8"))
    assert tuple(design["feature_names"]) == rti_microstructure_v10.FEATURE_NAMES
    assert design_fingerprint(design) == rti_microstructure_v10.DESIGN_SHA256
    assert design["source_feature_review_complete_windows"] == 30
    assert design["outcome_labels_used_for_change"] is False
    assert design["information_loss_from_removal"] is False
    assert set(design["removed_exact_duplicate_features"]) == set(
        rti_microstructure_v10.DROP_FEATURES
    )

    row = _v10_row()
    old = rti_microstructure_v9.feature_vector(row)
    compact = rti_microstructure_v10.feature_vector(row)
    assert old["available"] is compact["available"] is True
    assert len(compact["features"]) == 63
    old_by_name = dict(zip(rti_microstructure_v9.FEATURE_NAMES, old["features"]))
    assert compact["features"] == [
        old_by_name[name] for name in rti_microstructure_v10.FEATURE_NAMES
    ]
    assert not set(rti_microstructure_v10.FEATURE_NAMES) & set(
        rti_microstructure_v10.DROP_FEATURES
    )


def test_v10_is_outcome_blind_and_enforces_its_new_boundary():
    clean = rti_microstructure_v10.feature_vector(_v10_row())
    poisoned = rti_microstructure_v10.feature_vector(_v10_row(
        official_result="NO", correct=0, hypothetical_pnl_cents=-999.0,
    ))
    assert poisoned["features"] == clean["features"]
    assert rti_microstructure_v10.feature_vector(_v10_row(
        close_time=rti_microstructure_v10.PROSPECTIVE_AFTER_CLOSE_TIME,
    )) == {"available": False, "error": "pre_v10_prospective_boundary"}


def test_v11_cross_asset_manifest_and_vector_are_pinned_and_outcome_blind():
    design = json.loads(Path(
        "config/q15_rti_microstructure_design_v11.json"
    ).read_text(encoding="utf-8"))
    assert tuple(design["feature_names"]) == rti_microstructure_v11.FEATURE_NAMES
    assert design_fingerprint(design) == rti_microstructure_v11.DESIGN_SHA256
    assert design["outcome_labels_used_for_change"] is False
    assert design["hypothesis_selected_from_outcomes"] is False
    clean = rti_microstructure_v11.feature_vector(_v11_row())
    poisoned = rti_microstructure_v11.feature_vector(_v11_row(
        official_result="NO", correct=0, hypothetical_pnl_cents=-999.0,
    ))
    assert clean["available"] is True
    assert len(clean["features"]) == 71
    assert poisoned["features"] == clean["features"]
    values = dict(zip(rti_microstructure_v11.FEATURE_NAMES, clean["features"]))
    assert values["cross_asset_median_momentum_60s"] == 0.0
    assert values["cross_asset_breadth_signed_60s"] == 0.0
    assert values["log1p_cross_asset_dispersion_mad_60s"] == pytest.approx(
        math.log(3.0)
    )
    assert values["cross_asset_btc_minus_non_btc_median_60s"] == -2.5
    assert values["cross_asset_btc_direction_agreement_60s"] == 0.5


def test_v11_recomputes_cross_asset_derivatives_and_rejects_future_evidence():
    mismatch = rti_microstructure_v11.feature_vector(_v11_row(
        rti_cross_asset_median_momentum_bps_60s=99.0,
    ))
    assert mismatch == {
        "available": False,
        "error": "cross_asset_derived_mismatch:median_momentum_bps_60s",
    }
    future = rti_microstructure_v11.feature_vector(_v11_row(
        rti_cross_asset_latest_start_created_at_60s=9e12,
    ))
    assert future == {
        "available": False,
        "error": "cross_asset_future_start_60s",
    }
    consensus = rti_microstructure_v11.feature_vector(_v11_row(
        rti_cross_asset_coinbase_btc_change_bps_60s=5.0,
    ))
    assert consensus == {
        "available": False,
        "error": "cross_asset_consensus_mismatch:btc_60s",
    }


def test_v12_compact_manifest_and_projection_are_pinned_and_outcome_blind():
    design = json.loads(Path(
        "config/q15_rti_microstructure_design_v12.json"
    ).read_text(encoding="utf-8"))
    assert tuple(design["feature_names"]) == rti_microstructure_v12.FEATURE_NAMES
    assert design_fingerprint(design) == rti_microstructure_v12.DESIGN_SHA256
    assert design["outcome_labels_used_for_change"] is False
    assert design["performance_metrics_inspected_for_change"] is False
    assert design["v11_remains_frozen_parallel_control"] is True
    assert design["source_feature_review_complete_windows"] == 38

    clean = rti_microstructure_v12.feature_vector(_v12_row())
    poisoned = rti_microstructure_v12.feature_vector(_v12_row(
        official_result="NO", correct=0, hypothetical_pnl_cents=-999.0,
    ))
    assert clean["available"] is True
    assert len(clean["features"]) == 20
    assert poisoned["features"] == clean["features"]
    values = dict(zip(rti_microstructure_v12.FEATURE_NAMES, clean["features"]))
    v11_values = dict(zip(
        rti_microstructure_v11.FEATURE_NAMES,
        rti_microstructure_v11.feature_vector(_v12_row())["features"],
    ))
    assert values[
        "target_minus_cross_asset_median_momentum_60s_bps"
    ] == pytest.approx(
        v11_values["independent_consensus_momentum_60s_bps"]
        - v11_values["cross_asset_median_momentum_60s"]
    )
    assert rti_microstructure_v12.feature_vector(_v12_row(
        close_time=rti_microstructure_v12.PROSPECTIVE_AFTER_CLOSE_TIME,
    )) == {"available": False, "error": "pre_v12_prospective_boundary"}


def test_v12_coverage_never_credits_inspected_or_partial_windows():
    assets = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
    rows = [
        _v12_row(
            id=index + 1,
            asset=asset,
            bot_name="rti_path_13m",
            interval="13M",
            record_kind="RTI_PATH_13M_PROSPECTIVE_EXACT",
        )
        for index, asset in enumerate(assets)
    ]
    inspected = [dict(
        row,
        id=int(row["id"]) + 20,
        close_time=rti_microstructure_v12.PROSPECTIVE_AFTER_CLOSE_TIME,
    ) for row in rows]
    coverage = rti_microstructure_v12.model_feature_window_coverage([
        *inspected, *rows,
    ])
    assert coverage["schema_complete_model_candidate_close_windows"] == 1
    assert coverage["complete_model_feature_close_windows"] == 1
    assert coverage["model_feature_unavailable_rows"] == []

    rows[0]["rti_cross_asset_schema_version"] = "wrong"
    partial = rti_microstructure_v12.model_feature_window_coverage(rows)
    assert partial["complete_model_feature_close_windows"] == 0


def test_microstructure_vector_is_deterministic_and_yes_oriented():
    result = feature_vector(_row())
    assert result["available"] is True
    assert result["cohort"] == "NON_BTC_TRANSFER"
    values = dict(zip(FEATURE_NAMES, result["features"]))
    assert values["asset_eth"] == 1.0
    assert values["kalshi_yes_microprice_edge_cents"] == pytest.approx(0.5)
    assert values["kalshi_taker_imbalance_yes_5s"] == pytest.approx(0.5)
    assert values["kalshi_taker_imbalance_yes_30s"] == pytest.approx(-0.5)
    assert values["kalshi_taker_imbalance_yes_60s"] == 0.0
    assert values["kalshi_best_level_flow_pressure_yes_30s"] == pytest.approx(0.4)
    assert values["spot_signed_log_net_notional_15s"] == pytest.approx(math.log(100.0))
    assert values["spot_signed_log_net_notional_60s"] == pytest.approx(-math.log(1000.0))
    assert values["kalshi_microstructure_missing"] == 0.0
    assert values["spot_flow_missing"] == 0.0


def test_zero_activity_is_neutral_evidence_not_missing_evidence():
    result = feature_vector(_row(
        kalshi_taker_yes_volume_5s=0.0,
        kalshi_taker_no_volume_5s=0.0,
        kalshi_yes_best_depletion_30s=0.0,
        kalshi_no_best_depletion_30s=0.0,
        kalshi_yes_best_refill_30s=0.0,
        kalshi_no_best_refill_30s=0.0,
    ))
    values = dict(zip(FEATURE_NAMES, result["features"]))
    assert values["kalshi_taker_imbalance_yes_5s"] == 0.0
    assert values["kalshi_best_level_flow_pressure_yes_30s"] == 0.0
    assert values["kalshi_microstructure_missing"] == 0.0


def test_missing_microstructure_and_spot_flow_are_explicitly_flagged():
    result = feature_vector(_row(
        kalshi_trade_imbalance_yes_5s=None,
        kalshi_taker_yes_volume_60s=None,
        spot_depth_trade_net_notional_15s=None,
    ))
    values = dict(zip(FEATURE_NAMES, result["features"]))
    assert values["kalshi_trade_imbalance_yes_5s"] == 0.0
    assert values["kalshi_taker_imbalance_yes_60s"] == 0.0
    assert values["spot_signed_log_net_notional_15s"] == 0.0
    assert values["kalshi_microstructure_missing"] == 1.0
    assert values["spot_flow_missing"] == 1.0


def test_outcome_fields_cannot_change_decision_time_features():
    original = feature_vector(_row())
    poisoned = feature_vector(_row(
        official_result="NO",
        correct=0,
        hypothetical_pnl_cents=-55.0,
    ))
    assert poisoned["features"] == original["features"]
    assert poisoned["market_yes_probability"] == original["market_yes_probability"]


def test_model_feature_window_requires_all_seven_executable_vectors():
    assets = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
    rows = []
    for index, asset in enumerate(assets):
        rows.append(_row(
            id=index + 1,
            asset=asset,
            bot_name="rti_path_13m",
            interval="13M",
            record_kind="RTI_PATH_13M_PROSPECTIVE_EXACT",
            kalshi_microstructure_schema_version="rti-exact-microstructure-v1",
            source_captured_at=1220.1,
            kalshi_microstructure_captured_at=1220.1,
            evidence_as_of=1220.2,
        ))
    clean = model_feature_window_coverage(rows)
    assert clean["complete_model_feature_close_windows"] == 1
    assert clean["model_feature_unavailable_rows"] == []

    for row in rows:
        row["evidence_as_of"] = None
        row["threshold_json"] = json.dumps({"rti_evaluated_at": 1220.2})
    recovered_legacy_evidence = model_feature_window_coverage(rows)
    assert recovered_legacy_evidence[
        "complete_model_feature_close_windows"
    ] == 1

    rows[-1]["entry_ask_cents"] = None
    rows[-1]["depth_contracts"] = None
    rows[-1]["spread_cents"] = None
    dirty = model_feature_window_coverage(rows)
    assert dirty["complete_model_feature_close_windows"] == 0
    assert dirty["model_feature_unavailable_rows"] == [{
        "id": 7,
        "close_time": 2_000.0,
        "asset": "HYPE",
        "error": "base:market_quote_or_depth_missing",
    }]

    rows[-1]["entry_ask_cents"] = 55.0
    rows[-1]["depth_contracts"] = 25.0
    rows[-1]["spread_cents"] = 1.0
    rows[-1]["kalshi_microstructure_captured_at"] = 1222.2
    bad_time = model_feature_window_coverage(rows)
    assert bad_time["complete_model_feature_close_windows"] == 0
    assert bad_time["model_feature_timestamp_failures"][0]["reasons"] == [
        "NOT_EXACT_13M",
        "QUOTE_SOURCE_TIMESTAMP_MISMATCH",
        "EVIDENCE_PRECEDES_CAPTURE",
    ]


def test_v4_coverage_never_credits_prefixed_or_incomplete_source_rows():
    assets = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
    rows = [
        _v4_row(
            id=index + 1,
            asset=asset,
            bot_name="rti_path_13m",
            interval="13M",
            record_kind="RTI_PATH_13M_PROSPECTIVE_EXACT",
            source_captured_at=1220.1,
            evidence_as_of=1220.2,
        )
        for index, asset in enumerate(assets)
    ]
    clean = rti_microstructure_v4.model_feature_window_coverage(rows)
    assert clean["schema_complete_model_candidate_close_windows"] == 1
    assert clean["complete_model_feature_close_windows"] == 1

    rows[0]["kalshi_microstructure_schema_version"] = (
        "rti-exact-microstructure-v1"
    )
    mixed_schema = rti_microstructure_v4.model_feature_window_coverage(rows)
    assert mixed_schema["schema_complete_model_candidate_close_windows"] == 0

    rows[0]["kalshi_microstructure_schema_version"] = (
        "rti-exact-microstructure-v2"
    )
    rows[0]["kalshi_trade_window_complete_60s"] = False
    incomplete = rti_microstructure_v4.model_feature_window_coverage(rows)
    assert incomplete["schema_complete_model_candidate_close_windows"] == 1
    assert incomplete["complete_model_feature_close_windows"] == 0
    assert incomplete["model_feature_unavailable_rows"][0]["error"] == (
        "trade_window_incomplete_60s"
    )


def test_dynamics_extension_coverage_is_outcome_blind_and_fail_closed():
    assets = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
    rows = [
        _extension_row(
            id=index + 1,
            asset=asset,
            bot_name="rti_path_13m",
            interval="13M",
            record_kind="RTI_PATH_13M_PROSPECTIVE_EXACT",
        )
        for index, asset in enumerate(assets)
    ]
    clean = extension_window_coverage(rows)
    assert clean["outcome_labels_read"] is False
    assert clean["schema_complete_extension_close_windows"] == 1
    assert clean["complete_extension_close_windows"] == 1
    assert clean["extension_unavailable_rows"] == []

    poisoned = [dict(row, official_result="YES", correct=1) for row in rows]
    assert extension_window_coverage(poisoned) == clean

    rows[0]["kalshi_microprice_variation_cents_30s"] = None
    missing = extension_window_coverage(rows)
    assert missing["schema_complete_extension_close_windows"] == 1
    assert missing["complete_extension_close_windows"] == 0
    assert missing["extension_unavailable_rows"][0]["reasons"] == [
        "REQUIRED_METRIC_MISSING:microprice_variation_cents_30s"
    ]

    rows[0]["kalshi_microprice_variation_cents_30s"] = 0.0
    rows[0]["kalshi_trade_count_5s"] = 1.0
    missing_vwap = extension_window_coverage(rows)
    assert missing_vwap["complete_extension_close_windows"] == 0
    assert missing_vwap["extension_unavailable_rows"][0]["reasons"] == [
        "TRADE_VWAP_MISSING_5S"
    ]
