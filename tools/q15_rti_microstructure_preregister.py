"""Outcome-blind readiness gate for the preregistered RTI micro model.

This command deliberately cannot fit a model or read settlement labels.  It
binds the fixed design manifest to the exact feature-coverage audit and reports
when a separate locked-freeze command may first be run.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.q15_rti_feature_coverage_audit import build_report, _load_rows
from q15_upgrade.strategy_bots.rti_microstructure import (
    DESIGN_ID as V1_DESIGN_ID,
    DESIGN_SHA256 as V1_DESIGN_SHA256,
    model_feature_window_coverage as v1_model_feature_window_coverage,
)
from q15_upgrade.strategy_bots.rti_microstructure_v2 import (
    DESIGN_ID as V2_DESIGN_ID,
    DESIGN_SHA256 as V2_DESIGN_SHA256,
    model_feature_window_coverage as v2_model_feature_window_coverage,
)
from q15_upgrade.strategy_bots.rti_microstructure_v3 import (
    DESIGN_ID as V3_DESIGN_ID,
    DESIGN_SHA256 as V3_DESIGN_SHA256,
    model_feature_window_coverage as v3_model_feature_window_coverage,
)
from q15_upgrade.strategy_bots.rti_microstructure_v4 import (
    DESIGN_ID as V4_DESIGN_ID,
    DESIGN_SHA256 as V4_DESIGN_SHA256,
    model_feature_window_coverage as v4_model_feature_window_coverage,
)
from q15_upgrade.strategy_bots.rti_microstructure_v5 import (
    DESIGN_ID as V5_DESIGN_ID,
    DESIGN_SHA256 as V5_DESIGN_SHA256,
    EXTENSION_SCHEMA_VERSION as V5_EXTENSION_SCHEMA_VERSION,
    FIRST_ELIGIBLE_CLOSE_TIME as V5_FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME as V5_PROSPECTIVE_AFTER_CLOSE_TIME,
    model_feature_window_coverage as v5_model_feature_window_coverage,
)
from q15_upgrade.strategy_bots.rti_microstructure_v6 import (
    DESIGN_ID as V6_DESIGN_ID,
    DESIGN_SHA256 as V6_DESIGN_SHA256,
    FIRST_ELIGIBLE_CLOSE_TIME as V6_FIRST_ELIGIBLE_CLOSE_TIME,
    LEAD_LAG_SCHEMA as V6_LEAD_LAG_SCHEMA,
    PROSPECTIVE_AFTER_CLOSE_TIME as V6_PROSPECTIVE_AFTER_CLOSE_TIME,
    SPOT_PATH_SCHEMA as V6_SPOT_PATH_SCHEMA,
    model_feature_window_coverage as v6_model_feature_window_coverage,
)
from q15_upgrade.strategy_bots.rti_microstructure_v7 import (
    CROSS_VENUE_SCHEMA as V7_CROSS_VENUE_SCHEMA,
    CROSS_VENUE_TIME_BASIS as V7_CROSS_VENUE_TIME_BASIS,
    DESIGN_ID as V7_DESIGN_ID,
    DESIGN_SHA256 as V7_DESIGN_SHA256,
    FIRST_ELIGIBLE_CLOSE_TIME as V7_FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME as V7_PROSPECTIVE_AFTER_CLOSE_TIME,
    model_feature_window_coverage as v7_model_feature_window_coverage,
)
from q15_upgrade.strategy_bots.rti_microstructure_v8 import (
    DESIGN_ID as V8_DESIGN_ID,
    DESIGN_SHA256 as V8_DESIGN_SHA256,
    FIRST_ELIGIBLE_CLOSE_TIME as V8_FIRST_ELIGIBLE_CLOSE_TIME,
    INDEPENDENT_SCHEMA_VERSION as V8_INDEPENDENT_SCHEMA_VERSION,
    INDEPENDENT_TIME_BASIS as V8_INDEPENDENT_TIME_BASIS,
    PROSPECTIVE_AFTER_CLOSE_TIME as V8_PROSPECTIVE_AFTER_CLOSE_TIME,
    model_feature_window_coverage as v8_model_feature_window_coverage,
)
from q15_upgrade.strategy_bots.rti_microstructure_v9 import (
    DESIGN_ID as V9_DESIGN_ID,
    DESIGN_SHA256 as V9_DESIGN_SHA256,
    FIRST_ELIGIBLE_CLOSE_TIME as V9_FIRST_ELIGIBLE_CLOSE_TIME,
    KRAKEN_PARTIAL_FILL_FLOW_SCHEMA_VERSION as V9_KRAKEN_FLOW_SCHEMA_VERSION,
    MICROSTRUCTURE_SCHEMA_VERSION as V9_MICROSTRUCTURE_SCHEMA_VERSION,
    MICROSTRUCTURE_TIME_BASIS as V9_MICROSTRUCTURE_TIME_BASIS,
    PROSPECTIVE_AFTER_CLOSE_TIME as V9_PROSPECTIVE_AFTER_CLOSE_TIME,
    model_feature_window_coverage as v9_model_feature_window_coverage,
)
from q15_upgrade.strategy_bots.rti_microstructure_v10 import (
    DESIGN_ID as V10_DESIGN_ID,
    DESIGN_SHA256 as V10_DESIGN_SHA256,
    DROP_FEATURES as V10_DROP_FEATURES,
    FIRST_ELIGIBLE_CLOSE_TIME as V10_FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME as V10_PROSPECTIVE_AFTER_CLOSE_TIME,
    model_feature_window_coverage as v10_model_feature_window_coverage,
)
from q15_upgrade.strategy_bots.rti_microstructure_v11 import (
    CROSS_ASSET_SCHEMA_VERSION as V11_CROSS_ASSET_SCHEMA_VERSION,
    CROSS_ASSET_TIME_BASIS as V11_CROSS_ASSET_TIME_BASIS,
    DESIGN_ID as V11_DESIGN_ID,
    DESIGN_SHA256 as V11_DESIGN_SHA256,
    FIRST_ELIGIBLE_CLOSE_TIME as V11_FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME as V11_PROSPECTIVE_AFTER_CLOSE_TIME,
    model_feature_window_coverage as v11_model_feature_window_coverage,
)
from q15_upgrade.strategy_bots.rti_microstructure_v12 import (
    DESIGN_ID as V12_DESIGN_ID,
    DESIGN_SHA256 as V12_DESIGN_SHA256,
    FEATURE_NAMES as V12_FEATURE_NAMES,
    FIRST_ELIGIBLE_CLOSE_TIME as V12_FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME as V12_PROSPECTIVE_AFTER_CLOSE_TIME,
    model_feature_window_coverage as v12_model_feature_window_coverage,
)
from q15_upgrade.strategy_bots.rti_microstructure_v13 import (
    COHORT_CONDITIONED_FEATURE as V13_COHORT_CONDITIONED_FEATURE,
    DESIGN_ID as V13_DESIGN_ID,
    DESIGN_SHA256 as V13_DESIGN_SHA256,
    FEATURE_NAMES as V13_FEATURE_NAMES,
    FIRST_ELIGIBLE_CLOSE_TIME as V13_FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME as V13_PROSPECTIVE_AFTER_CLOSE_TIME,
    REPLACED_FEATURE as V13_REPLACED_FEATURE,
    model_feature_window_coverage as v13_model_feature_window_coverage,
)
from q15_upgrade.strategy_bots.rti_microstructure_v14 import (
    DESIGN_ID as V14_DESIGN_ID,
    DESIGN_SHA256 as V14_DESIGN_SHA256,
    FEATURE_NAMES as V14_FEATURE_NAMES,
    FIRST_ELIGIBLE_CLOSE_TIME as V14_FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME as V14_PROSPECTIVE_AFTER_CLOSE_TIME,
    model_feature_window_coverage as v14_model_feature_window_coverage,
)
from q15_upgrade.strategy_bots import rti_microstructure_v14_identity as v14_identity
from tools.q15_rti_output_integrity import atomic_write_text


DEFAULT_DESIGN = ROOT / "config" / "q15_rti_microstructure_design_v1.json"
DEFAULT_DESIGN_V2 = ROOT / "config" / "q15_rti_microstructure_design_v2.json"
DEFAULT_DESIGN_V3 = ROOT / "config" / "q15_rti_microstructure_design_v3.json"
DEFAULT_DESIGN_V4 = ROOT / "config" / "q15_rti_microstructure_design_v4.json"
DEFAULT_DESIGN_V5 = ROOT / "config" / "q15_rti_microstructure_design_v5.json"
DEFAULT_DESIGN_V6 = ROOT / "config" / "q15_rti_microstructure_design_v6.json"
DEFAULT_DESIGN_V7 = ROOT / "config" / "q15_rti_microstructure_design_v7.json"
DEFAULT_DESIGN_V8 = ROOT / "config" / "q15_rti_microstructure_design_v8.json"
DEFAULT_DESIGN_V9 = ROOT / "config" / "q15_rti_microstructure_design_v9.json"
DEFAULT_DESIGN_V10 = ROOT / "config" / "q15_rti_microstructure_design_v10.json"
DEFAULT_DESIGN_V11 = ROOT / "config" / "q15_rti_microstructure_design_v11.json"
DEFAULT_DESIGN_V12 = ROOT / "config" / "q15_rti_microstructure_design_v12.json"
DEFAULT_DESIGN_V13 = ROOT / "config" / "q15_rti_microstructure_design_v13.json"
DEFAULT_DESIGN_V14 = ROOT / "config" / "q15_rti_microstructure_design_v14.json"
PRIMARY_DESIGN = DEFAULT_DESIGN_V4
DEFAULT_DB = ROOT / "data" / "q15_strategy_bots_v3.sqlite3"
EXPECTED_DESIGN_SHA256 = V1_DESIGN_SHA256
EXPECTED_DESIGN_SHA256_V2 = V2_DESIGN_SHA256
EXPECTED_DESIGN_SHA256_V3 = V3_DESIGN_SHA256
EXPECTED_DESIGN_SHA256_V4 = V4_DESIGN_SHA256
EXPECTED_DESIGN_SHA256_V5 = V5_DESIGN_SHA256
EXPECTED_DESIGN_SHA256_V6 = V6_DESIGN_SHA256
EXPECTED_DESIGN_SHA256_V7 = V7_DESIGN_SHA256
EXPECTED_DESIGN_SHA256_V8 = V8_DESIGN_SHA256
EXPECTED_DESIGN_SHA256_V9 = V9_DESIGN_SHA256
EXPECTED_DESIGN_SHA256_V10 = V10_DESIGN_SHA256
EXPECTED_DESIGN_SHA256_V11 = V11_DESIGN_SHA256
EXPECTED_DESIGN_SHA256_V12 = V12_DESIGN_SHA256
EXPECTED_DESIGN_SHA256_V13 = V13_DESIGN_SHA256
EXPECTED_DESIGN_SHA256_V14 = V14_DESIGN_SHA256
EXPECTED_DESIGN_SHA256_BY_ID = {
    V1_DESIGN_ID: V1_DESIGN_SHA256,
    V2_DESIGN_ID: V2_DESIGN_SHA256,
    V3_DESIGN_ID: V3_DESIGN_SHA256,
    V4_DESIGN_ID: V4_DESIGN_SHA256,
    V5_DESIGN_ID: V5_DESIGN_SHA256,
    V6_DESIGN_ID: V6_DESIGN_SHA256,
    V7_DESIGN_ID: V7_DESIGN_SHA256,
    V8_DESIGN_ID: V8_DESIGN_SHA256,
    V9_DESIGN_ID: V9_DESIGN_SHA256,
    V10_DESIGN_ID: V10_DESIGN_SHA256,
    V11_DESIGN_ID: V11_DESIGN_SHA256,
    V12_DESIGN_ID: V12_DESIGN_SHA256,
    V13_DESIGN_ID: V13_DESIGN_SHA256,
    V14_DESIGN_ID: V14_DESIGN_SHA256,
}
EXPECTED_SOURCE_SCHEMA_BY_ID = {
    V1_DESIGN_ID: "rti-exact-microstructure-v1",
    V2_DESIGN_ID: "rti-exact-microstructure-v1",
    V3_DESIGN_ID: "rti-exact-microstructure-v1",
    V4_DESIGN_ID: "rti-exact-microstructure-v2",
    V5_DESIGN_ID: "rti-exact-microstructure-v2",
    V6_DESIGN_ID: "rti-exact-microstructure-v2",
    V7_DESIGN_ID: "rti-exact-microstructure-v2",
    V8_DESIGN_ID: "rti-exact-microstructure-v2",
    V9_DESIGN_ID: "rti-exact-microstructure-v2",
    V10_DESIGN_ID: "rti-exact-microstructure-v2",
    V11_DESIGN_ID: "rti-exact-microstructure-v2",
    V12_DESIGN_ID: "rti-exact-microstructure-v2",
    V13_DESIGN_ID: "rti-exact-microstructure-v2",
    V14_DESIGN_ID: "rti-exact-microstructure-v2",
}
EXPECTED_NON_BTC_ASSETS = frozenset({"BNB", "DOGE", "ETH", "HYPE", "SOL", "XRP"})


def design_fingerprint(design: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(design),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_design(design: Mapping[str, Any]) -> None:
    if design.get("design_status") != "PREREGISTERED_BEFORE_MODEL_READINESS":
        raise ValueError("design_not_preregistered")
    if design.get("paper_only") is not True:
        raise ValueError("design_must_be_paper_only")
    if design.get("real_trading_allowed") is not False:
        raise ValueError("real_trading_must_be_forbidden")
    if design.get("automatic_promotion") is not False:
        raise ValueError("automatic_promotion_must_be_forbidden")
    if design.get("automatic_refit") is not False:
        raise ValueError("automatic_refit_must_be_forbidden")
    if design.get("notification_eligible") is not False:
        raise ValueError("notifications_must_be_forbidden")
    if design.get("historical_credit_allowed") is not False:
        raise ValueError("historical_credit_must_be_forbidden")
    if design.get("decision_interval") != "exact_13m":
        raise ValueError("decision_interval_must_be_exact_13m")
    design_id = str(design.get("design_id") or "")
    expected_source_schema = EXPECTED_SOURCE_SCHEMA_BY_ID.get(design_id)
    if expected_source_schema is None:
        raise ValueError("unsupported_design_id")
    if design.get("source_schema") != expected_source_schema:
        raise ValueError("source_schema_mismatch")
    if design_id in {
        V4_DESIGN_ID, V5_DESIGN_ID, V6_DESIGN_ID, V7_DESIGN_ID,
        V8_DESIGN_ID, V9_DESIGN_ID, V10_DESIGN_ID, V11_DESIGN_ID,
        V12_DESIGN_ID, V13_DESIGN_ID, V14_DESIGN_ID,
    }:
        if design.get("post_fix_history_only") is not True:
            raise ValueError("post_fix_history_guard_missing")
        if design.get("prior_schema_rows_credited") is not False:
            raise ValueError("prior_schema_credit_must_be_forbidden")
        if design.get("source_time_basis") != "local_received_at":
            raise ValueError("receive_time_basis_required")
        integrity = design.get("required_source_integrity")
        if not isinstance(integrity, Mapping):
            raise ValueError("required_source_integrity_missing")
        if integrity.get("count_cap_must_be_disabled") is not True:
            raise ValueError("count_cap_guard_missing")
        if integrity.get("continuity_resets_on_snapshot_or_reconnect") is not True:
            raise ValueError("continuity_reset_guard_missing")
        if list(integrity.get("required_complete_book_horizons_seconds") or ()) != [5, 15, 30]:
            raise ValueError("book_horizon_guard_mismatch")
        if list(integrity.get("required_complete_trade_horizons_seconds") or ()) != [5, 15, 30, 60]:
            raise ValueError("trade_horizon_guard_mismatch")
    if design_id == V5_DESIGN_ID:
        if design.get("source_extension_schema") != V5_EXTENSION_SCHEMA_VERSION:
            raise ValueError("extension_schema_mismatch")
        if design.get("pre_freeze_extension_rows_credited") is not False:
            raise ValueError("pre_freeze_extension_credit_must_be_forbidden")
        if design.get("source_feature_audit_only_before_freeze") is not True:
            raise ValueError("outcome_blind_design_guard_missing")
        if float(design.get("prospective_after_close_time") or 0.0) != (
            V5_PROSPECTIVE_AFTER_CLOSE_TIME
        ):
            raise ValueError("prospective_boundary_mismatch")
        if float(design.get("first_eligible_close_time") or 0.0) != (
            V5_FIRST_ELIGIBLE_CLOSE_TIME
        ):
            raise ValueError("first_eligible_close_mismatch")
        if list(integrity.get("required_complete_extension_horizons_seconds") or ()) != [5, 15, 30, 60]:
            raise ValueError("extension_horizon_guard_mismatch")
    if design_id == V6_DESIGN_ID:
        if design.get("source_extension_schema") != V5_EXTENSION_SCHEMA_VERSION:
            raise ValueError("extension_schema_mismatch")
        if design.get("source_spot_path_schema") != V6_SPOT_PATH_SCHEMA:
            raise ValueError("spot_path_schema_mismatch")
        if design.get("source_lead_lag_schema") != V6_LEAD_LAG_SCHEMA:
            raise ValueError("lead_lag_schema_mismatch")
        if design.get("source_spot_time_basis") != "local_created_at":
            raise ValueError("spot_receive_time_basis_required")
        if design.get("pre_freeze_spot_path_rows_credited") is not False:
            raise ValueError("pre_freeze_spot_credit_must_be_forbidden")
        if design.get("source_feature_audit_only_before_freeze") is not True:
            raise ValueError("outcome_blind_design_guard_missing")
        if float(design.get("prospective_after_close_time") or 0.0) != (
            V6_PROSPECTIVE_AFTER_CLOSE_TIME
        ):
            raise ValueError("prospective_boundary_mismatch")
        if float(design.get("first_eligible_close_time") or 0.0) != (
            V6_FIRST_ELIGIBLE_CLOSE_TIME
        ):
            raise ValueError("first_eligible_close_mismatch")
        if list(integrity.get("required_complete_extension_horizons_seconds") or ()) != [5, 15, 30, 60]:
            raise ValueError("extension_horizon_guard_mismatch")
        if list(integrity.get("required_complete_spot_mid_horizons_seconds") or ()) != [15, 60]:
            raise ValueError("spot_mid_horizon_guard_mismatch")
        if float(integrity.get("spot_mid_retention_minimum_seconds") or 0.0) < 60.0:
            raise ValueError("spot_mid_retention_guard_missing")
    if design_id == V7_DESIGN_ID:
        if design.get("source_extension_schema") != V5_EXTENSION_SCHEMA_VERSION:
            raise ValueError("extension_schema_mismatch")
        if design.get("source_spot_path_schema") != V6_SPOT_PATH_SCHEMA:
            raise ValueError("spot_path_schema_mismatch")
        if design.get("source_lead_lag_schema") != V6_LEAD_LAG_SCHEMA:
            raise ValueError("lead_lag_schema_mismatch")
        if design.get("source_cross_venue_schema") != V7_CROSS_VENUE_SCHEMA:
            raise ValueError("cross_venue_schema_mismatch")
        if design.get("source_cross_venue_time_basis") != V7_CROSS_VENUE_TIME_BASIS:
            raise ValueError("cross_venue_time_basis_mismatch")
        if design.get("pre_freeze_cross_venue_rows_credited") is not False:
            raise ValueError("pre_freeze_cross_venue_credit_must_be_forbidden")
        if design.get("source_feature_audit_only_before_freeze") is not True:
            raise ValueError("outcome_blind_design_guard_missing")
        if float(design.get("prospective_after_close_time") or 0.0) != (
            V7_PROSPECTIVE_AFTER_CLOSE_TIME
        ):
            raise ValueError("prospective_boundary_mismatch")
        if float(design.get("first_eligible_close_time") or 0.0) != (
            V7_FIRST_ELIGIBLE_CLOSE_TIME
        ):
            raise ValueError("first_eligible_close_mismatch")
        if list(integrity.get("required_complete_cross_venue_horizons_seconds") or ()) != [15, 60]:
            raise ValueError("cross_venue_horizon_guard_mismatch")
        if int(integrity.get("cross_venue_required_count") or 0) != 2:
            raise ValueError("cross_venue_count_guard_mismatch")
        if float(integrity.get("cross_venue_max_lag_seconds") or 0.0) > 15.0:
            raise ValueError("cross_venue_lag_guard_too_weak")
        if integrity.get("cross_venue_future_snapshots_forbidden") is not True:
            raise ValueError("cross_venue_future_guard_missing")
    if design_id == V8_DESIGN_ID:
        if design.get("source_extension_schema") != V5_EXTENSION_SCHEMA_VERSION:
            raise ValueError("extension_schema_mismatch")
        if design.get("source_independent_venue_schema") != V8_INDEPENDENT_SCHEMA_VERSION:
            raise ValueError("independent_venue_schema_mismatch")
        if design.get("source_independent_venue_time_basis") != V8_INDEPENDENT_TIME_BASIS:
            raise ValueError("independent_venue_time_basis_mismatch")
        if design.get("pre_freeze_independent_venue_rows_credited") is not False:
            raise ValueError("pre_freeze_independent_venue_credit_must_be_forbidden")
        if design.get("source_feature_audit_only_before_freeze") is not True:
            raise ValueError("outcome_blind_design_guard_missing")
        if float(design.get("prospective_after_close_time") or 0.0) != V8_PROSPECTIVE_AFTER_CLOSE_TIME:
            raise ValueError("prospective_boundary_mismatch")
        if float(design.get("first_eligible_close_time") or 0.0) != V8_FIRST_ELIGIBLE_CLOSE_TIME:
            raise ValueError("first_eligible_close_mismatch")
        if list(integrity.get("required_complete_independent_venue_horizons_seconds") or ()) != [15, 60]:
            raise ValueError("independent_venue_horizon_guard_mismatch")
        if int(integrity.get("independent_venue_required_count") or 0) != 2:
            raise ValueError("independent_venue_count_guard_mismatch")
        if float(integrity.get("independent_venue_max_lag_seconds") or 0.0) > 15.0:
            raise ValueError("independent_venue_lag_guard_too_weak")
        if integrity.get("independent_venue_future_snapshots_forbidden") is not True:
            raise ValueError("independent_venue_future_guard_missing")
        if integrity.get("single_primary_spot_path_required") is not False:
            raise ValueError("single_primary_spot_dependency_not_removed")
    if design_id in {
        V9_DESIGN_ID, V10_DESIGN_ID, V11_DESIGN_ID, V12_DESIGN_ID,
        V13_DESIGN_ID, V14_DESIGN_ID,
    }:
        if design.get("source_extension_schema") != V5_EXTENSION_SCHEMA_VERSION:
            raise ValueError("extension_schema_mismatch")
        if design.get("source_independent_venue_schema") != V8_INDEPENDENT_SCHEMA_VERSION:
            raise ValueError("independent_venue_schema_mismatch")
        if design.get("source_independent_venue_time_basis") != V8_INDEPENDENT_TIME_BASIS:
            raise ValueError("independent_venue_time_basis_mismatch")
        if design.get("source_independent_microstructure_schema") != V9_MICROSTRUCTURE_SCHEMA_VERSION:
            raise ValueError("independent_microstructure_schema_mismatch")
        if design.get("source_independent_microstructure_time_basis") != V9_MICROSTRUCTURE_TIME_BASIS:
            raise ValueError("independent_microstructure_time_basis_mismatch")
        if design.get("source_kraken_partial_fill_flow_schema") != V9_KRAKEN_FLOW_SCHEMA_VERSION:
            raise ValueError("kraken_partial_fill_schema_mismatch")
        if design.get("pre_freeze_independent_microstructure_rows_credited") is not False:
            raise ValueError("pre_freeze_independent_microstructure_credit_forbidden")
        if design.get("ambiguous_kraken_deletes_as_trades_forbidden") is not True:
            raise ValueError("ambiguous_delete_flow_guard_missing")
        if design.get("source_feature_audit_only_before_freeze") is not True:
            raise ValueError("outcome_blind_design_guard_missing")
        expected_boundary = {
            V9_DESIGN_ID: V9_PROSPECTIVE_AFTER_CLOSE_TIME,
            V10_DESIGN_ID: V10_PROSPECTIVE_AFTER_CLOSE_TIME,
            V11_DESIGN_ID: V11_PROSPECTIVE_AFTER_CLOSE_TIME,
            V12_DESIGN_ID: V12_PROSPECTIVE_AFTER_CLOSE_TIME,
            V13_DESIGN_ID: V13_PROSPECTIVE_AFTER_CLOSE_TIME,
            V14_DESIGN_ID: V14_PROSPECTIVE_AFTER_CLOSE_TIME,
        }[design_id]
        expected_first = {
            V9_DESIGN_ID: V9_FIRST_ELIGIBLE_CLOSE_TIME,
            V10_DESIGN_ID: V10_FIRST_ELIGIBLE_CLOSE_TIME,
            V11_DESIGN_ID: V11_FIRST_ELIGIBLE_CLOSE_TIME,
            V12_DESIGN_ID: V12_FIRST_ELIGIBLE_CLOSE_TIME,
            V13_DESIGN_ID: V13_FIRST_ELIGIBLE_CLOSE_TIME,
            V14_DESIGN_ID: V14_FIRST_ELIGIBLE_CLOSE_TIME,
        }[design_id]
        if float(design.get("prospective_after_close_time") or 0.0) != expected_boundary:
            raise ValueError("prospective_boundary_mismatch")
        if float(design.get("first_eligible_close_time") or 0.0) != expected_first:
            raise ValueError("first_eligible_close_mismatch")
        if list(integrity.get("required_complete_independent_venue_horizons_seconds") or ()) != [15, 60]:
            raise ValueError("independent_venue_horizon_guard_mismatch")
        if int(integrity.get("independent_venue_required_count") or 0) != 2:
            raise ValueError("independent_venue_count_guard_mismatch")
        if float(integrity.get("independent_venue_max_lag_seconds") or 0.0) > 15.0:
            raise ValueError("independent_venue_lag_guard_too_weak")
        if integrity.get("independent_venue_future_snapshots_forbidden") is not True:
            raise ValueError("independent_venue_future_guard_missing")
        if integrity.get("independent_microstructure_current_and_60s_start_required") is not True:
            raise ValueError("independent_microstructure_endpoint_guard_missing")
        if int(integrity.get("independent_microstructure_depth_summary_levels") or 0) != 10:
            raise ValueError("independent_microstructure_depth_limit_mismatch")
        if integrity.get("kraken_partial_fill_flow_schema_required") != V9_KRAKEN_FLOW_SCHEMA_VERSION:
            raise ValueError("kraken_partial_fill_integrity_guard_mismatch")
        if integrity.get("ambiguous_kraken_delete_flow_forbidden") is not True:
            raise ValueError("ambiguous_delete_integrity_guard_missing")
        if integrity.get("single_primary_spot_path_required") is not False:
            raise ValueError("single_primary_spot_dependency_not_removed")
        if design_id == V10_DESIGN_ID:
            if design.get("source_feature_review_design_id") != V8_DESIGN_ID:
                raise ValueError("source_feature_review_design_mismatch")
            if int(design.get("source_feature_review_complete_windows") or 0) != 30:
                raise ValueError("source_feature_review_window_count_mismatch")
            if set(design.get("removed_exact_duplicate_features") or ()) != set(V10_DROP_FEATURES):
                raise ValueError("removed_exact_duplicate_features_mismatch")
            if design.get("information_loss_from_removal") is not False:
                raise ValueError("information_loss_guard_missing")
        if design_id in {
            V11_DESIGN_ID, V12_DESIGN_ID, V13_DESIGN_ID, V14_DESIGN_ID,
        }:
            if design.get("source_cross_asset_schema") != V11_CROSS_ASSET_SCHEMA_VERSION:
                raise ValueError("cross_asset_schema_mismatch")
            if design.get("source_cross_asset_time_basis") != V11_CROSS_ASSET_TIME_BASIS:
                raise ValueError("cross_asset_time_basis_mismatch")
            if design.get("pre_freeze_cross_asset_rows_credited") is not False:
                raise ValueError("pre_freeze_cross_asset_credit_forbidden")
            if design_id != V14_DESIGN_ID and (
                design.get("hypothesis_selected_from_outcomes") is not False
            ):
                raise ValueError("outcome_selected_hypothesis_forbidden")
            if list(integrity.get("cross_asset_required_assets") or ()) != [
                "BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP",
            ]:
                raise ValueError("cross_asset_required_assets_mismatch")
            if list(integrity.get("cross_asset_required_venues") or ()) != [
                "coinbase", "kraken",
            ]:
                raise ValueError("cross_asset_required_venues_mismatch")
            if list(integrity.get("cross_asset_horizons_seconds") or ()) != [15, 60]:
                raise ValueError("cross_asset_horizons_mismatch")
            if integrity.get("cross_asset_raw_moves_persisted") is not True:
                raise ValueError("cross_asset_raw_moves_guard_missing")
            if integrity.get("cross_asset_derived_values_recomputed_before_use") is not True:
                raise ValueError("cross_asset_recompute_guard_missing")
            if integrity.get("cross_asset_future_snapshots_forbidden") is not True:
                raise ValueError("cross_asset_future_guard_missing")
        if design_id == V12_DESIGN_ID:
            if design.get("source_feature_review_design_id") != V11_DESIGN_ID:
                raise ValueError("v12_source_feature_review_design_mismatch")
            if int(design.get("source_feature_review_complete_windows") or 0) != 38:
                raise ValueError("v12_source_feature_review_window_count_mismatch")
            if design.get("source_feature_review_outcome_labels_read") is not False:
                raise ValueError("v12_source_feature_review_read_labels")
            if design.get("performance_metrics_inspected_for_change") is not False:
                raise ValueError("v12_performance_metric_selection_forbidden")
            if design.get("pre_v12_inspected_rows_credited") is not False:
                raise ValueError("v12_inspected_row_credit_forbidden")
            if design.get("v11_remains_frozen_parallel_control") is not True:
                raise ValueError("v12_v11_parallel_control_guard_missing")
            if int(design.get("source_feature_count") or 0) != 71:
                raise ValueError("v12_source_feature_count_mismatch")
            if int(design.get("projected_feature_count") or 0) != 20:
                raise ValueError("v12_projected_feature_count_mismatch")
            if tuple(design.get("feature_names") or ()) != V12_FEATURE_NAMES:
                raise ValueError("v12_feature_projection_mismatch")
            projection = design.get("projection_policy")
            if not isinstance(projection, Mapping):
                raise ValueError("v12_projection_policy_missing")
            if projection.get("outcome_based_feature_selection") is not False:
                raise ValueError("v12_outcome_feature_selection_forbidden")
            if projection.get("automatic_feature_selection") is not False:
                raise ValueError("v12_automatic_feature_selection_forbidden")
            if projection.get(
                "target_relative_momentum_orthogonalized_against_broad_market"
            ) is not True:
                raise ValueError("v12_relative_momentum_guard_missing")
        if design_id == V13_DESIGN_ID:
            if design.get("source_successor_charter_id") != (
                "q15-rti-v13-btc-alias-successor-preregistration-v1"
            ):
                raise ValueError("v13_successor_charter_id_mismatch")
            if design.get("source_successor_charter_sha256") != (
                "f55e3772f4b6bced8a2315c94d007bf35eac05b38a27391e071d4dd570abae78"
            ):
                raise ValueError("v13_successor_charter_sha_mismatch")
            if design.get("source_feature_review_design_id") != V12_DESIGN_ID:
                raise ValueError("v13_source_feature_review_design_mismatch")
            if int(design.get("source_feature_review_complete_windows") or 0) != 30:
                raise ValueError("v13_source_feature_review_window_count_mismatch")
            if design.get("source_feature_review_outcome_labels_read") is not False:
                raise ValueError("v13_source_feature_review_read_labels")
            if design.get("source_feature_review_model_fit_performed") is not False:
                raise ValueError("v13_source_feature_review_fit_forbidden")
            if design.get("performance_metrics_inspected_for_change") is not False:
                raise ValueError("v13_performance_metric_selection_forbidden")
            if design.get("pre_v13_inspected_rows_credited") is not False:
                raise ValueError("v13_inspected_row_credit_forbidden")
            if design.get("v11_and_v12_remain_frozen_parallel_controls") is not True:
                raise ValueError("v13_parallel_control_guard_missing")
            if int(design.get("source_feature_count") or 0) != 20 or (
                int(design.get("projected_feature_count") or 0) != 20
            ):
                raise ValueError("v13_feature_count_mismatch")
            if tuple(design.get("feature_names") or ()) != V13_FEATURE_NAMES:
                raise ValueError("v13_feature_projection_mismatch")
            replacement = design.get("replacement_policy")
            if not isinstance(replacement, Mapping):
                raise ValueError("v13_replacement_policy_missing")
            expected_replacement = {
                "selection_basis": "PREDECLARED_OUTCOME_BLIND_BTC_ALIAS_TRIGGER_ONLY",
                "replaced_feature_name": V13_REPLACED_FEATURE,
                "replacement_feature_name": V13_COHORT_CONDITIONED_FEATURE,
                "replacement_formula": (
                    "0 for BTC; otherwise preserve "
                    "cross_asset_btc_minus_non_btc_median_60s"
                ),
                "all_other_v12_feature_formulas_unchanged": True,
                "all_v12_training_hyperparameters_unchanged": True,
                "entry_policy_unchanged": True,
                "automatic_feature_selection": False,
                "outcome_based_feature_selection": False,
            }
            if any(
                replacement.get(key) != value
                for key, value in expected_replacement.items()
            ):
                raise ValueError("v13_replacement_policy_mismatch")
        if design_id == V14_DESIGN_ID:
            if design.get("source_successor_charter_id") != v14_identity.CHARTER_ID:
                raise ValueError("v14_successor_charter_id_mismatch")
            if design.get("source_successor_charter_sha256") != v14_identity.CHARTER_SHA256:
                raise ValueError("v14_successor_charter_sha_mismatch")
            if design.get("source_feature_design_id") != V13_DESIGN_ID or (
                design.get("source_feature_design_sha256") != V13_DESIGN_SHA256
            ):
                raise ValueError("v14_source_feature_binding_mismatch")
            for key in (
                "source_v13_outcome_labels_read",
                "source_v13_model_fit_performed",
                "source_v13_performance_metrics_inspected",
                "opened_v11_untouched_test_used",
                "pre_v14_inspected_rows_credited",
            ):
                if design.get(key) is not False:
                    raise ValueError(f"v14_false_guard_missing:{key}")
            for key in (
                "feature_formulas_unchanged_from_v13",
                "base_optimizer_unchanged_from_v13",
                "entry_policy_unchanged_from_v13",
                "v11_v12_v13_remain_frozen_parallel_controls",
            ):
                if design.get(key) is not True:
                    raise ValueError(f"v14_true_guard_missing:{key}")
            if tuple(design.get("feature_names") or ()) != V14_FEATURE_NAMES:
                raise ValueError("v14_feature_projection_mismatch")
            combination = design.get("prediction_combination")
            if not isinstance(combination, Mapping) or (
                combination.get("architecture")
                != "nested_chronological_safe_residual_trust_v1"
            ):
                raise ValueError("v14_prediction_combination_mismatch")
            if combination.get("fixed_factor_grid") != [
                0.0, 0.25, 0.5, 0.75, 1.0,
            ] or float(combination.get("fallback_factor", -1.0)) != 0.0:
                raise ValueError("v14_factor_grid_mismatch")
            for key in (
                "factor_zero_is_exact_market_prior",
                "selection_requires_observed_brier_improvement",
                "selection_requires_observed_log_loss_improvement",
                "selection_requires_paired_bootstrap_upper_below_zero",
                "outer_validation_labels_forbidden_for_factor_selection",
                "calibration_labels_forbidden_for_factor_selection",
                "untouched_test_labels_forbidden_for_factor_selection",
            ):
                if combination.get(key) is not True:
                    raise ValueError(f"v14_prediction_guard_missing:{key}")
    if design.get("market_prior") != (
        "point_in_time_despread_kalshi_yes_probability"
    ):
        raise ValueError("point_in_time_market_prior_required")
    if design.get("model_family") != (
        "regularized_market_prior_residual_logit"
    ):
        raise ValueError("model_family_mismatch")
    chronology = design.get("chronology")
    if not isinstance(chronology, Mapping):
        raise ValueError("chronology_missing")
    if chronology.get("same_close_assets_must_share_fold") is not True:
        raise ValueError("same_close_fold_isolation_missing")
    if chronology.get("outcome_labels_forbidden_before_readiness") is not True:
        raise ValueError("pre_readiness_label_guard_missing")
    if chronology.get("test_may_be_scored_once") is not True:
        raise ValueError("single_test_score_guard_missing")
    if chronology.get("partial_close_windows_forbidden") is not True:
        raise ValueError("partial_close_window_guard_missing")
    if chronology.get("timestamp_alignment_fail_closed") is not True:
        raise ValueError("timestamp_fail_closed_guard_missing")
    if chronology.get("test_may_not_tune_features_or_hyperparameters") is not True:
        raise ValueError("test_tuning_guard_missing")
    fractions = tuple(
        float(chronology.get(key) or 0.0)
        for key in (
            "train_fraction",
            "calibration_fraction",
            "untouched_test_fraction",
        )
    )
    if any(value <= 0.0 for value in fractions) or abs(sum(fractions) - 1.0) > 1e-12:
        raise ValueError("chronological_fold_fractions_invalid")
    feature_names = list(design.get("feature_names") or ())
    if not feature_names or len(feature_names) != len(set(feature_names)):
        raise ValueError("feature_names_missing_or_duplicated")
    cohorts = design.get("cohorts")
    if not isinstance(cohorts, Mapping):
        raise ValueError("cohorts_missing")
    if set(cohorts) != {"BTC", "NON_BTC_TRANSFER"}:
        raise ValueError("cohort_keys_mismatch")
    if set(dict(cohorts["NON_BTC_TRANSFER"]).get("assets") or ()) != (
        EXPECTED_NON_BTC_ASSETS
    ):
        raise ValueError("non_btc_asset_cohort_mismatch")
    if dict(cohorts["BTC"]).get("mixing_with_non_btc_forbidden") is not True:
        raise ValueError("btc_cohort_mixing_guard_missing")
    if dict(cohorts["NON_BTC_TRANSFER"]).get("mixing_with_btc_forbidden") is not True:
        raise ValueError("non_btc_cohort_mixing_guard_missing")
    btc_windows = int(
        dict(cohorts.get("BTC") or {}).get(
            "minimum_complete_close_windows", 0
        )
    )
    transfer_windows = int(
        dict(cohorts.get("NON_BTC_TRANSFER") or {}).get(
            "minimum_complete_close_windows", 0
        )
    )
    if btc_windows < 150 or transfer_windows < 60:
        raise ValueError("cohort_window_minimum_too_small")
    training = design.get("fixed_training_config")
    if not isinstance(training, Mapping):
        raise ValueError("fixed_training_config_missing")
    if training.get("hyperparameter_search_performed") is not False:
        raise ValueError("hyperparameter_search_must_be_disabled")
    if float(training.get("model_l2") or 0.0) <= 0.0:
        raise ValueError("positive_regularization_required")
    if not 0.0 < float(training.get("residual_logit_scale") or 0.0) <= 1.0:
        raise ValueError("residual_logit_scale_invalid")
    entry = design.get("entry_policy")
    if not isinstance(entry, Mapping):
        raise ValueError("entry_policy_missing")
    if entry.get("official_kalshi_fees") is not True:
        raise ValueError("official_fee_guard_missing")
    if float(entry.get("slippage_cents_per_contract") or 0.0) < 2.0:
        raise ValueError("slippage_assumption_too_small")
    review = design.get("prospective_review")
    if not isinstance(review, Mapping) or review.get("manual_only") is not True:
        raise ValueError("manual_review_guard_missing")
    if list(review.get("resolved_pick_bars") or ()) != [30, 60, 150]:
        raise ValueError("manual_review_bars_mismatch")
    expected_fingerprint = EXPECTED_DESIGN_SHA256_BY_ID.get(
        str(design.get("design_id") or "")
    )
    if expected_fingerprint is None:
        raise ValueError("unsupported_design_id")
    if design_fingerprint(design) != expected_fingerprint:
        raise ValueError("design_fingerprint_mismatch")


def model_feature_coverage_for_design(
    design: Mapping[str, Any], rows: list[Mapping[str, Any]],
) -> dict[str, Any]:
    design_id = str(design.get("design_id") or "")
    if design_id == V1_DESIGN_ID:
        return v1_model_feature_window_coverage(rows)
    if design_id == V2_DESIGN_ID:
        return v2_model_feature_window_coverage(rows)
    if design_id == V3_DESIGN_ID:
        return v3_model_feature_window_coverage(rows)
    if design_id == V4_DESIGN_ID:
        return v4_model_feature_window_coverage(rows)
    if design_id == V5_DESIGN_ID:
        return v5_model_feature_window_coverage(rows)
    if design_id == V6_DESIGN_ID:
        return v6_model_feature_window_coverage(rows)
    if design_id == V7_DESIGN_ID:
        return v7_model_feature_window_coverage(rows)
    if design_id == V8_DESIGN_ID:
        return v8_model_feature_window_coverage(rows)
    if design_id == V9_DESIGN_ID:
        return v9_model_feature_window_coverage(rows)
    if design_id == V10_DESIGN_ID:
        return v10_model_feature_window_coverage(rows)
    if design_id == V11_DESIGN_ID:
        return v11_model_feature_window_coverage(rows)
    if design_id == V12_DESIGN_ID:
        return v12_model_feature_window_coverage(rows)
    if design_id == V13_DESIGN_ID:
        return v13_model_feature_window_coverage(rows)
    if design_id == V14_DESIGN_ID:
        return v14_model_feature_window_coverage(rows)
    raise ValueError("unsupported_design_id")


def build_readiness(
    design: Mapping[str, Any],
    coverage: Mapping[str, Any],
) -> dict[str, Any]:
    validate_design(design)
    schema_complete_windows = int(
        coverage.get("complete_microstructure_close_windows")
        if coverage.get("complete_microstructure_close_windows") is not None
        else coverage.get("complete_microstructure_v1_close_windows") or 0
    )
    complete_windows = int(
        coverage.get("complete_model_feature_close_windows")
        if coverage.get("complete_model_feature_close_windows") is not None
        else schema_complete_windows
    )
    failures = len(coverage.get("timestamp_alignment_failures") or ())
    model_timestamp_failures = len(
        coverage.get("model_feature_timestamp_failures") or ()
    )
    partial = len(coverage.get("cross_asset_partial_schema_windows") or ())
    incomplete = len(
        coverage.get("incomplete_microstructure_close_windows")
        if coverage.get("incomplete_microstructure_close_windows") is not None
        else coverage.get("incomplete_microstructure_v1_close_windows") or ()
    )
    model_scoped_integrity = (
        coverage.get("complete_model_feature_close_windows") is not None
    )
    # Once a design supplies its boundary-aware model coverage, historical
    # source gaps outside that design's eligible period remain visible below
    # but cannot poison its readiness forever. Eligible timestamp corruption
    # is rechecked by the feature builder and always fails closed here.
    clean = (
        model_timestamp_failures == 0
        if model_scoped_integrity
        else (
            failures == 0
            and partial == 0
            and incomplete == 0
        )
    )
    cohort_status = {}
    for cohort, raw in dict(design["cohorts"]).items():
        minimum = int(
            dict(raw).get("minimum_complete_close_windows") or 0
        )
        cohort_status[str(cohort)] = {
            "minimum_complete_close_windows": minimum,
            "complete_close_windows": complete_windows,
            "windows_remaining": max(0, minimum - complete_windows),
            "ready_for_locked_freeze": bool(clean and complete_windows >= minimum),
        }
    any_ready = any(
        bool(status["ready_for_locked_freeze"])
        for status in cohort_status.values()
    )
    return {
        "audit_version": "q15-rti-microstructure-preregister-readiness-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "design_id": design.get("design_id"),
        "design_sha256": design_fingerprint(design),
        "feature_schema_version": design.get("feature_schema_version"),
        "feature_count": len(design.get("feature_names") or ()),
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "artifact_emitted": False,
        "paper_only": True,
        "schema_complete_microstructure_close_windows": schema_complete_windows,
        "complete_microstructure_close_windows": complete_windows,
        "model_feature_unavailable_rows": len(
            coverage.get("model_feature_unavailable_rows") or ()
        ),
        "model_feature_timestamp_failures": len(
            coverage.get("model_feature_timestamp_failures") or ()
        ),
        "unusable_model_feature_close_windows": len(
            coverage.get("unusable_model_feature_close_windows") or ()
        ),
        "timestamp_alignment_failures": failures,
        "partial_schema_windows": partial,
        "incomplete_seven_asset_windows": incomplete,
        "coverage_clean": clean,
        "readiness_integrity_scope": (
            "design_eligible_model_windows"
            if model_scoped_integrity
            else "source_schema_windows"
        ),
        "cohorts": cohort_status,
        "ready_for_any_locked_freeze": any_ready,
        "status": (
            "READY_FOR_SEPARATE_LOCKED_FREEZE"
            if any_ready
            else "WAITING_FOR_COMPLETE_WINDOWS"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--design",
        required=True,
        help=(
            "Explicit frozen design manifest. Required to avoid reporting "
            "an older preregistration through a stale default."
        ),
    )
    parser.add_argument("--strategy-db", default=str(DEFAULT_DB))
    parser.add_argument("--output")
    args = parser.parse_args()

    design = json.loads(Path(args.design).read_text(encoding="utf-8"))
    if not isinstance(design, Mapping):
        raise ValueError("design_root_not_object")
    # _load_rows has a fixed allow-list that intentionally omits
    # official_result/correct/P&L columns and opens SQLite in read-only mode.
    rows = _load_rows(Path(args.strategy_db))
    coverage = build_report(
        rows, source_schema=str(design.get("source_schema") or ""),
    )
    coverage.update(model_feature_coverage_for_design(design, rows))
    readiness = build_readiness(design, coverage)
    rendered = json.dumps(readiness, indent=2, sort_keys=True) + "\n"
    if args.output:
        target = Path(args.output)
        atomic_write_text(target, rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
