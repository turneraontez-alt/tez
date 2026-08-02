"""Best-effort runtime hooks for the v3 filtered alert system."""
from __future__ import annotations

import copy
from dataclasses import replace
import json
import logging
import math
import os
import sqlite3
import threading
import time
from typing import Any, Mapping, Sequence

import cycle_watchdog

from ..lineage import lineage_stamp
from .btc_regime import enrich_btc_regime
from .drift_evidence import enrich_drift_evidence
from .kraken_l3_depth import enrich_kraken_l3
from .l2_depth import enrich_coinbase_l2
from .spot_depth import enrich_spot_depth
from .rti_probability import (
    V3_ARTIFACT_PATH,
    artifact_health as rti_probability_artifact_health,
    runtime_prediction as rti_probability_prediction,
)
from .rti_microstructure_runtime import (
    artifact_health as rti_v11_artifact_health,
    runtime_prediction as rti_v11_prediction,
)
from .rti_microstructure_v12_runtime import (
    artifact_health as rti_v12_artifact_health,
)
from .rti_microstructure_v4 import (
    DESIGN_ID as RTI_MICROSTRUCTURE_DESIGN_ID,
    DESIGN_SHA256 as RTI_MICROSTRUCTURE_DESIGN_SHA256,
    FEATURE_NAMES as RTI_MICROSTRUCTURE_FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION as RTI_MICROSTRUCTURE_FEATURE_SCHEMA_VERSION,
)
from .rti_microstructure_v5 import (
    DESIGN_ID as RTI_DYNAMICS_DESIGN_ID,
    DESIGN_SHA256 as RTI_DYNAMICS_DESIGN_SHA256,
    FEATURE_NAMES as RTI_DYNAMICS_FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION as RTI_DYNAMICS_FEATURE_SCHEMA_VERSION,
    FIRST_ELIGIBLE_CLOSE_TIME as RTI_DYNAMICS_FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME as RTI_DYNAMICS_PROSPECTIVE_AFTER_CLOSE_TIME,
)
from .rti_microstructure_v6 import (
    DESIGN_ID as RTI_LEAD_LAG_DESIGN_ID,
    DESIGN_SHA256 as RTI_LEAD_LAG_DESIGN_SHA256,
    FEATURE_NAMES as RTI_LEAD_LAG_FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION as RTI_LEAD_LAG_FEATURE_SCHEMA_VERSION,
    FIRST_ELIGIBLE_CLOSE_TIME as RTI_LEAD_LAG_FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME as RTI_LEAD_LAG_PROSPECTIVE_AFTER_CLOSE_TIME,
)
from .rti_microstructure_v7 import (
    DESIGN_ID as RTI_CROSS_VENUE_DESIGN_ID,
    DESIGN_SHA256 as RTI_CROSS_VENUE_DESIGN_SHA256,
    FEATURE_NAMES as RTI_CROSS_VENUE_FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION as RTI_CROSS_VENUE_FEATURE_SCHEMA_VERSION,
    FIRST_ELIGIBLE_CLOSE_TIME as RTI_CROSS_VENUE_FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME as RTI_CROSS_VENUE_PROSPECTIVE_AFTER_CLOSE_TIME,
)
from .rti_microstructure_v8 import (
    DESIGN_ID as RTI_INDEPENDENT_VENUE_DESIGN_ID,
    DESIGN_SHA256 as RTI_INDEPENDENT_VENUE_DESIGN_SHA256,
    FEATURE_NAMES as RTI_INDEPENDENT_VENUE_FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION as RTI_INDEPENDENT_VENUE_FEATURE_SCHEMA_VERSION,
    FIRST_ELIGIBLE_CLOSE_TIME as RTI_INDEPENDENT_VENUE_FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME as RTI_INDEPENDENT_VENUE_PROSPECTIVE_AFTER_CLOSE_TIME,
)
from .rti_microstructure_v9 import (
    DESIGN_ID as RTI_INDEPENDENT_MICROSTRUCTURE_DESIGN_ID,
    DESIGN_SHA256 as RTI_INDEPENDENT_MICROSTRUCTURE_DESIGN_SHA256,
    FEATURE_NAMES as RTI_INDEPENDENT_MICROSTRUCTURE_FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION as RTI_INDEPENDENT_MICROSTRUCTURE_FEATURE_SCHEMA_VERSION,
    FIRST_ELIGIBLE_CLOSE_TIME as RTI_INDEPENDENT_MICROSTRUCTURE_FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME as RTI_INDEPENDENT_MICROSTRUCTURE_PROSPECTIVE_AFTER_CLOSE_TIME,
)
from .rti_microstructure_v10 import (
    DESIGN_ID as RTI_COMPACT_MICROSTRUCTURE_DESIGN_ID,
    DESIGN_SHA256 as RTI_COMPACT_MICROSTRUCTURE_DESIGN_SHA256,
    FEATURE_NAMES as RTI_COMPACT_MICROSTRUCTURE_FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION as RTI_COMPACT_MICROSTRUCTURE_FEATURE_SCHEMA_VERSION,
    FIRST_ELIGIBLE_CLOSE_TIME as RTI_COMPACT_MICROSTRUCTURE_FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME as RTI_COMPACT_MICROSTRUCTURE_PROSPECTIVE_AFTER_CLOSE_TIME,
)
from .rti_microstructure_v11 import (
    DESIGN_ID as RTI_CROSS_ASSET_REGIME_DESIGN_ID,
    DESIGN_SHA256 as RTI_CROSS_ASSET_REGIME_DESIGN_SHA256,
    FEATURE_NAMES as RTI_CROSS_ASSET_REGIME_FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION as RTI_CROSS_ASSET_REGIME_FEATURE_SCHEMA_VERSION,
    FIRST_ELIGIBLE_CLOSE_TIME as RTI_CROSS_ASSET_REGIME_FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME as RTI_CROSS_ASSET_REGIME_PROSPECTIVE_AFTER_CLOSE_TIME,
)
from .rti_microstructure_v11_identity import (
    PROSPECTIVE_BOOTSTRAP_CLUSTER_KEY as RTI_V11_BOOTSTRAP_CLUSTER_KEY,
    PROSPECTIVE_BOOTSTRAP_CONFIDENCE_LEVEL as RTI_V11_BOOTSTRAP_CONFIDENCE_LEVEL,
    PROSPECTIVE_BOOTSTRAP_RANDOM_SEED as RTI_V11_BOOTSTRAP_RANDOM_SEED,
    PROSPECTIVE_BOOTSTRAP_RESAMPLES as RTI_V11_BOOTSTRAP_RESAMPLES,
    PROSPECTIVE_BOOTSTRAP_VERSION as RTI_V11_BOOTSTRAP_VERSION,
    PROSPECTIVE_MIN_MEAN_BRIER_IMPROVEMENT as RTI_V11_MIN_BRIER_IMPROVEMENT,
    PROSPECTIVE_MIN_MEAN_LOG_LOSS_IMPROVEMENT as RTI_V11_MIN_LOG_LOSS_IMPROVEMENT,
)
from .rti_microstructure_v12 import (
    DESIGN_ID as RTI_ORTHOGONAL_COMPACT_DESIGN_ID,
    DESIGN_SHA256 as RTI_ORTHOGONAL_COMPACT_DESIGN_SHA256,
    FEATURE_NAMES as RTI_ORTHOGONAL_COMPACT_FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION as RTI_ORTHOGONAL_COMPACT_FEATURE_SCHEMA_VERSION,
    FIRST_ELIGIBLE_CLOSE_TIME as RTI_ORTHOGONAL_COMPACT_FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME as RTI_ORTHOGONAL_COMPACT_PROSPECTIVE_AFTER_CLOSE_TIME,
)
from .rti_microstructure_v13 import (
    CALIBRATION_REPORTING_PROTOCOL_ID as RTI_V13_CALIBRATION_REPORTING_PROTOCOL_ID,
    CALIBRATION_REPORTING_PROTOCOL_SHA256 as RTI_V13_CALIBRATION_REPORTING_PROTOCOL_SHA256,
    COVARIATE_DRIFT_PROTOCOL_ID as RTI_V13_COVARIATE_DRIFT_PROTOCOL_ID,
    COVARIATE_DRIFT_PROTOCOL_SHA256 as RTI_V13_COVARIATE_DRIFT_PROTOCOL_SHA256,
    DESIGN_ID as RTI_COHORT_CONDITIONED_COMPACT_DESIGN_ID,
    DESIGN_SHA256 as RTI_COHORT_CONDITIONED_COMPACT_DESIGN_SHA256,
    FEATURE_NAMES as RTI_COHORT_CONDITIONED_COMPACT_FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION as RTI_COHORT_CONDITIONED_COMPACT_FEATURE_SCHEMA_VERSION,
    FIRST_ELIGIBLE_CLOSE_TIME as RTI_COHORT_CONDITIONED_COMPACT_FIRST_ELIGIBLE_CLOSE_TIME,
    GEOMETRY_REVIEW_PROTOCOL_ID as RTI_V13_GEOMETRY_REVIEW_PROTOCOL_ID,
    GEOMETRY_REVIEW_PROTOCOL_SHA256 as RTI_V13_GEOMETRY_REVIEW_PROTOCOL_SHA256,
    PROSPECTIVE_AFTER_CLOSE_TIME as RTI_COHORT_CONDITIONED_COMPACT_PROSPECTIVE_AFTER_CLOSE_TIME,
    REPORTING_PROTOCOL_ID as RTI_V13_REPORTING_PROTOCOL_ID,
    REPORTING_PROTOCOL_SHA256 as RTI_V13_REPORTING_PROTOCOL_SHA256,
    SELECTIVE_VALUE_CURVE_PROTOCOL_ID as RTI_V13_SELECTIVE_VALUE_CURVE_PROTOCOL_ID,
    SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256 as RTI_V13_SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256,
)
from .ledger import StrategyBotLedger
from .rules import (
    ACCEPTED,
    BOT_BASELINE,
    BOT_BNB_NO,
    BOT_BNB_YES_REVERSAL,
    BOT_CONFIDENCE_TIER,
    BOT_DEPTH_FORMULA_15M,
    BOT_FAV_10M,
    BOT_HVF_DEPTH_FLOW,
    BOT_HYPE_YES,
    BOT_RTI_PATH_13M,
    BOT_DRIFT_ACCURACY_V91,
    BOT_DRIFT_ASYMMETRIC_VOLUME,
    BOT_DRIFT_BALANCED_V95,
    BOT_DRIFT_CONSENSUS_FALLBACK,
    BOT_DRIFT_FLOW_SPREAD,
    BOT_DRIFT_ADDON,
    BOT_DRIFT_LATEQUAL,
    BOT_DRIFT_NO_EXPANSION,
    BOT_DRIFT_NO_MIRROR,
    BOT_THIRTEEN_M_SNIPER,
    BOT_PRECISION_13M,
    BOT_TOP_PICK_13M,
    BOT_WARN_FLIP,
    REJECTED,
    RESEARCH_ONLY,
    DRIFT_CORE_RULE_VERSION,
    RTI_PATH_13M_INDEX_IDS,
    RTI_PATH_13M_IMPULSE_CHALLENGER_ID,
    RTI_PATH_13M_IMPULSE_POLICY_VERSION,
    RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID,
    RTI_PATH_13M_PROBABILITY_V2_CHALLENGER_ID,
    RTI_PATH_13M_PROBABILITY_V3_CHALLENGER_ID,
    RTI_PATH_13M_RULE_VERSION,
    RTI_EXACT_MICROSTRUCTURE_EXTENSION_SCHEMA_VERSION,
    STRATEGY_VERSION,
    BotDecision,
    decisions_for_row,
    drift_addon_requal_decision,
    drift_accuracy_v91_shadow_decision,
    drift_asymmetric_volume_shadow_decision,
    drift_balanced_v95_shadow_decision,
    drift_consensus_fallback_shadow_decision,
    drift_flow_spread_13m_decision,
    drift_flow_spread_shadow_flow15_decision,
    drift_flow_spread_shadow_spread4_decision,
    drift_latequal_decision,
    drift_no_expansion_decision,
    drift_no_mirror_decision,
    precision13_sized_decision,
    rti_path_11m30_stability_decision,
    rti_path_12m_confirmation_decision,
    rti_path_12m30_confirmation_decision,
    rti_path_13m_decision,
    rti_path_13m_rule_version,
    source_side,
    top_pick_13m_decision,
    warn_flip_entry_decision,
)
from .telegram import (
    V3Telegram,
    build_drift_no_mirror_group_alert,
    build_v3_alert,
    build_v3_auto_mute_alert,
)

logger = logging.getLogger("strategy_bots.runtime")

_ledger: StrategyBotLedger | None = None
_telegram: V3Telegram | None = None
_drift_outbox: Any | None = None
_drift_outbox_identity: tuple[int, str] | None = None
_drift_reconcile_lock = threading.Lock()
_drift_reconcile_worker_lock = threading.Lock()
_drift_reconcile_worker: threading.Thread | None = None
_drift_reconcile_worker_state: dict[str, Any] = {
    "inflight": False,
    "submitted_at": None,
    "finished_at": None,
    "last_duration_seconds": None,
    "last_summary": None,
    "last_error": None,
}
_thirteen_m_stats_warning_logged = False
_thirteen_m_flow_warning_logged = False

DRIFT_DECISION_FEATURE_SCHEMA_VERSION = "drift-decision-evidence-v2"
V11_FIRST_FEATURE_REVIEW_WINDOWS = 30


def _v11_collection_readiness_headline(
    exact_feature_coverage: Mapping[str, Any],
) -> dict[str, Any]:
    """Expose only design-bound executable V11 windows in compact health."""
    raw = exact_feature_coverage.get(
        "cross_asset_regime_v11_model_readiness", {}
    )
    if not isinstance(raw, Mapping):
        raw = {}
    try:
        feature_count = int(raw.get("feature_count") or 0)
        windows = max(0, int(raw.get("complete_executable_close_windows") or 0))
        schema_windows = max(0, int(raw.get("schema_complete_close_windows") or 0))
        unavailable = max(0, int(raw.get("feature_unavailable_rows") or 0))
        timestamp_failures = max(
            0, int(raw.get("timestamp_alignment_failures") or 0)
        )
        unusable = max(0, int(raw.get("unusable_close_windows") or 0))
    except (TypeError, ValueError):
        feature_count = 0
        windows = schema_windows = unavailable = timestamp_failures = unusable = 0
    identity_ok = bool(
        raw.get("design_id") == RTI_CROSS_ASSET_REGIME_DESIGN_ID
        and raw.get("design_sha256") == RTI_CROSS_ASSET_REGIME_DESIGN_SHA256
        and raw.get("feature_schema_version")
        == RTI_CROSS_ASSET_REGIME_FEATURE_SCHEMA_VERSION
        and feature_count == len(RTI_CROSS_ASSET_REGIME_FEATURE_NAMES)
    )
    safety_ok = bool(
        raw.get("paper_only") is True
        and raw.get("notification_eligible") is False
        and raw.get("real_trading_allowed") is False
        and raw.get("readiness_uses_outcome_labels") is False
        and raw.get("model_fit_performed") is False
        and raw.get("artifact_emitted") is False
    )
    valid = identity_ok and safety_ok
    if not valid:
        windows = schema_windows = unavailable = timestamp_failures = unusable = 0
    cohorts = raw.get("cohorts", {})
    if not isinstance(cohorts, Mapping):
        cohorts = {}
    cohort_headline: dict[str, Any] = {}
    for cohort, minimum in (("NON_BTC_TRANSFER", 60), ("BTC", 150)):
        metrics = cohorts.get(cohort, {})
        if not isinstance(metrics, Mapping):
            metrics = {}
        try:
            reported_minimum = int(
                metrics.get("minimum_complete_close_windows") or 0
            )
        except (TypeError, ValueError):
            reported_minimum = 0
        cohort_valid = valid and reported_minimum == minimum
        remaining = max(0, minimum - windows) if cohort_valid else minimum
        cohort_headline[cohort] = {
            "minimum_complete_close_windows": minimum,
            "windows_remaining": remaining,
            "ready_for_locked_freeze": bool(cohort_valid and windows >= minimum),
        }
    return {
        "available": valid,
        "design_id": RTI_CROSS_ASSET_REGIME_DESIGN_ID,
        "design_sha256": RTI_CROSS_ASSET_REGIME_DESIGN_SHA256,
        "feature_schema_version": RTI_CROSS_ASSET_REGIME_FEATURE_SCHEMA_VERSION,
        "feature_count": len(RTI_CROSS_ASSET_REGIME_FEATURE_NAMES),
        "complete_executable_close_windows": windows,
        "schema_complete_close_windows": schema_windows,
        "feature_unavailable_rows": unavailable,
        "timestamp_alignment_failures": timestamp_failures,
        "unusable_close_windows": unusable,
        "first_feature_review_windows": V11_FIRST_FEATURE_REVIEW_WINDOWS,
        "windows_remaining_to_first_feature_review": max(
            0, V11_FIRST_FEATURE_REVIEW_WINDOWS - windows
        ),
        "first_feature_review_ready": bool(
            valid and windows >= V11_FIRST_FEATURE_REVIEW_WINDOWS
        ),
        "cohorts": cohort_headline,
        "paper_only": True,
        "readiness_uses_outcome_labels": False,
        "model_fit_performed": False,
        "artifact_emitted": False,
        "notification_eligible": False,
        "real_trading_allowed": False,
        "status": (
            "OUTCOME_BLIND_FIRST_FEATURE_REVIEW_READY"
            if valid and windows >= V11_FIRST_FEATURE_REVIEW_WINDOWS
            else "ACCUMULATING_EXECUTABLE_WINDOWS"
            if valid
            else "INVALID_OR_UNAVAILABLE_V11_READINESS"
        ),
    }


def _v12_collection_readiness_headline(
    exact_feature_coverage: Mapping[str, Any],
) -> dict[str, Any]:
    """Expose only design-bound executable V12 windows in compact health."""
    raw = exact_feature_coverage.get(
        "orthogonal_compact_v12_model_readiness", {}
    )
    if not isinstance(raw, Mapping):
        raw = {}
    try:
        feature_count = int(raw.get("feature_count") or 0)
        windows = max(0, int(raw.get("complete_executable_close_windows") or 0))
        schema_windows = max(0, int(raw.get("schema_complete_close_windows") or 0))
        unavailable = max(0, int(raw.get("feature_unavailable_rows") or 0))
        timestamp_failures = max(
            0, int(raw.get("timestamp_alignment_failures") or 0)
        )
        unusable = max(0, int(raw.get("unusable_close_windows") or 0))
    except (TypeError, ValueError):
        feature_count = 0
        windows = schema_windows = unavailable = timestamp_failures = unusable = 0
    identity_ok = bool(
        raw.get("design_id") == RTI_ORTHOGONAL_COMPACT_DESIGN_ID
        and raw.get("design_sha256") == RTI_ORTHOGONAL_COMPACT_DESIGN_SHA256
        and raw.get("feature_schema_version")
        == RTI_ORTHOGONAL_COMPACT_FEATURE_SCHEMA_VERSION
        and feature_count == len(RTI_ORTHOGONAL_COMPACT_FEATURE_NAMES)
    )
    safety_ok = bool(
        raw.get("paper_only") is True
        and raw.get("notification_eligible") is False
        and raw.get("real_trading_allowed") is False
        and raw.get("readiness_uses_outcome_labels") is False
        and raw.get("model_fit_performed") is False
        and raw.get("artifact_emitted") is False
    )
    valid = identity_ok and safety_ok
    if not valid:
        windows = schema_windows = unavailable = timestamp_failures = unusable = 0
    cohorts = raw.get("cohorts", {})
    if not isinstance(cohorts, Mapping):
        cohorts = {}
    cohort_headline: dict[str, Any] = {}
    for cohort, minimum in (("NON_BTC_TRANSFER", 60), ("BTC", 150)):
        metrics = cohorts.get(cohort, {})
        if not isinstance(metrics, Mapping):
            metrics = {}
        try:
            reported_minimum = int(
                metrics.get("minimum_complete_close_windows") or 0
            )
        except (TypeError, ValueError):
            reported_minimum = 0
        cohort_valid = valid and reported_minimum == minimum
        cohort_headline[cohort] = {
            "minimum_complete_close_windows": minimum,
            "windows_remaining": (
                max(0, minimum - windows) if cohort_valid else minimum
            ),
            "ready_for_locked_freeze": bool(
                cohort_valid and windows >= minimum
            ),
        }
    return {
        "available": valid,
        "design_id": RTI_ORTHOGONAL_COMPACT_DESIGN_ID,
        "design_sha256": RTI_ORTHOGONAL_COMPACT_DESIGN_SHA256,
        "feature_schema_version": (
            RTI_ORTHOGONAL_COMPACT_FEATURE_SCHEMA_VERSION
        ),
        "feature_count": len(RTI_ORTHOGONAL_COMPACT_FEATURE_NAMES),
        "prospective_after_close_time": (
            RTI_ORTHOGONAL_COMPACT_PROSPECTIVE_AFTER_CLOSE_TIME
        ),
        "first_eligible_close_time": (
            RTI_ORTHOGONAL_COMPACT_FIRST_ELIGIBLE_CLOSE_TIME
        ),
        "complete_executable_close_windows": windows,
        "schema_complete_close_windows": schema_windows,
        "feature_unavailable_rows": unavailable,
        "timestamp_alignment_failures": timestamp_failures,
        "unusable_close_windows": unusable,
        "first_feature_review_windows": V11_FIRST_FEATURE_REVIEW_WINDOWS,
        "windows_remaining_to_first_feature_review": max(
            0, V11_FIRST_FEATURE_REVIEW_WINDOWS - windows
        ),
        "first_feature_review_ready": bool(
            valid and windows >= V11_FIRST_FEATURE_REVIEW_WINDOWS
        ),
        "cohorts": cohort_headline,
        "paper_only": True,
        "readiness_uses_outcome_labels": False,
        "model_fit_performed": False,
        "artifact_emitted": False,
        "notification_eligible": False,
        "real_trading_allowed": False,
        "v11_remains_frozen_parallel_control": True,
        "status": (
            "OUTCOME_BLIND_FIRST_FEATURE_REVIEW_READY"
            if valid and windows >= V11_FIRST_FEATURE_REVIEW_WINDOWS
            else "ACCUMULATING_EXECUTABLE_WINDOWS"
            if valid
            else "INVALID_OR_UNAVAILABLE_V12_READINESS"
        ),
    }


def _v13_collection_readiness_headline(
    exact_feature_coverage: Mapping[str, Any],
) -> dict[str, Any]:
    """Expose fail-closed prospective V13 feature collection only."""
    raw = exact_feature_coverage.get(
        "cohort_conditioned_compact_v13_model_readiness", {}
    )
    if not isinstance(raw, Mapping):
        raw = {}
    try:
        feature_count = int(raw.get("feature_count") or 0)
        windows = max(0, int(raw.get("complete_executable_close_windows") or 0))
        schema_windows = max(0, int(raw.get("schema_complete_close_windows") or 0))
        unavailable = max(0, int(raw.get("feature_unavailable_rows") or 0))
        timestamp_failures = max(
            0, int(raw.get("timestamp_alignment_failures") or 0)
        )
        unusable = max(0, int(raw.get("unusable_close_windows") or 0))
    except (TypeError, ValueError):
        feature_count = 0
        windows = schema_windows = unavailable = timestamp_failures = unusable = 0
    identity_ok = bool(
        raw.get("design_id") == RTI_COHORT_CONDITIONED_COMPACT_DESIGN_ID
        and raw.get("design_sha256")
        == RTI_COHORT_CONDITIONED_COMPACT_DESIGN_SHA256
        and raw.get("feature_schema_version")
        == RTI_COHORT_CONDITIONED_COMPACT_FEATURE_SCHEMA_VERSION
        and feature_count == len(RTI_COHORT_CONDITIONED_COMPACT_FEATURE_NAMES)
    )
    safety_ok = bool(
        raw.get("paper_only") is True
        and raw.get("notification_eligible") is False
        and raw.get("real_trading_allowed") is False
        and raw.get("readiness_uses_outcome_labels") is False
        and raw.get("model_fit_performed") is False
        and raw.get("artifact_emitted") is False
        and raw.get("v11_and_v12_remain_frozen_parallel_controls") is True
    )
    valid = identity_ok and safety_ok
    if not valid:
        windows = schema_windows = unavailable = timestamp_failures = unusable = 0
    cohorts = raw.get("cohorts", {})
    if not isinstance(cohorts, Mapping):
        cohorts = {}
    cohort_headline: dict[str, Any] = {}
    for cohort, minimum in (("NON_BTC_TRANSFER", 60), ("BTC", 150)):
        metrics = cohorts.get(cohort, {})
        if not isinstance(metrics, Mapping):
            metrics = {}
        try:
            reported_minimum = int(
                metrics.get("minimum_complete_close_windows") or 0
            )
        except (TypeError, ValueError):
            reported_minimum = 0
        cohort_valid = valid and reported_minimum == minimum
        cohort_headline[cohort] = {
            "minimum_complete_close_windows": minimum,
            "windows_remaining": (
                max(0, minimum - windows) if cohort_valid else minimum
            ),
            "ready_for_locked_freeze": bool(
                cohort_valid and windows >= minimum
            ),
        }
    return {
        "available": valid,
        "design_id": RTI_COHORT_CONDITIONED_COMPACT_DESIGN_ID,
        "design_sha256": RTI_COHORT_CONDITIONED_COMPACT_DESIGN_SHA256,
        "feature_schema_version": (
            RTI_COHORT_CONDITIONED_COMPACT_FEATURE_SCHEMA_VERSION
        ),
        "feature_count": len(RTI_COHORT_CONDITIONED_COMPACT_FEATURE_NAMES),
        "prospective_after_close_time": (
            RTI_COHORT_CONDITIONED_COMPACT_PROSPECTIVE_AFTER_CLOSE_TIME
        ),
        "first_eligible_close_time": (
            RTI_COHORT_CONDITIONED_COMPACT_FIRST_ELIGIBLE_CLOSE_TIME
        ),
        "complete_executable_close_windows": windows,
        "schema_complete_close_windows": schema_windows,
        "feature_unavailable_rows": unavailable,
        "timestamp_alignment_failures": timestamp_failures,
        "unusable_close_windows": unusable,
        "first_outcome_blind_review_windows": 60,
        "windows_remaining_to_first_outcome_blind_review": max(0, 60 - windows),
        "first_outcome_blind_review_ready": bool(valid and windows >= 60),
        "geometry_review_protocol_id": RTI_V13_GEOMETRY_REVIEW_PROTOCOL_ID,
        "geometry_review_protocol_sha256": (
            RTI_V13_GEOMETRY_REVIEW_PROTOCOL_SHA256
        ),
        "geometry_review_windows": 30,
        "windows_remaining_to_geometry_review": max(0, 30 - windows),
        "geometry_review_ready": bool(valid and windows >= 30),
        "covariate_drift_protocol_id": (
            RTI_V13_COVARIATE_DRIFT_PROTOCOL_ID
        ),
        "covariate_drift_protocol_sha256": (
            RTI_V13_COVARIATE_DRIFT_PROTOCOL_SHA256
        ),
        "covariate_drift_review_windows": 60,
        "windows_remaining_to_covariate_drift_review": max(0, 60 - windows),
        "covariate_drift_review_ready": bool(valid and windows >= 60),
        "subgroup_reporting_protocol_id": RTI_V13_REPORTING_PROTOCOL_ID,
        "subgroup_reporting_protocol_sha256": RTI_V13_REPORTING_PROTOCOL_SHA256,
        "calibration_reporting_protocol_id": (
            RTI_V13_CALIBRATION_REPORTING_PROTOCOL_ID
        ),
        "calibration_reporting_protocol_sha256": (
            RTI_V13_CALIBRATION_REPORTING_PROTOCOL_SHA256
        ),
        "selective_value_curve_protocol_id": (
            RTI_V13_SELECTIVE_VALUE_CURVE_PROTOCOL_ID
        ),
        "selective_value_curve_protocol_sha256": (
            RTI_V13_SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256
        ),
        "performance_reporting_outcome_labels_read": False,
        "performance_reporting_changes_deployment_gate": False,
        "cohorts": cohort_headline,
        "paper_only": True,
        "readiness_uses_outcome_labels": False,
        "model_fit_performed": False,
        "artifact_emitted": False,
        "runtime_scoring_connected": False,
        "notification_eligible": False,
        "real_trading_allowed": False,
        "historical_credit_allowed": False,
        "v11_and_v12_remain_frozen_parallel_controls": True,
        "status": (
            "OUTCOME_BLIND_60_WINDOW_REVIEW_READY"
            if valid and windows >= 60
            else "ACCUMULATING_EXECUTABLE_WINDOWS"
            if valid
            else "INVALID_OR_UNAVAILABLE_V13_READINESS"
        ),
    }


def _empty_rti_exact_feature_coverage() -> dict[str, Any]:
    readiness = {
        "design_id": RTI_MICROSTRUCTURE_DESIGN_ID,
        "design_sha256": RTI_MICROSTRUCTURE_DESIGN_SHA256,
        "feature_schema_version": RTI_MICROSTRUCTURE_FEATURE_SCHEMA_VERSION,
        "feature_count": len(RTI_MICROSTRUCTURE_FEATURE_NAMES),
        "paper_only": True,
        "notification_eligible": False,
        "real_trading_allowed": False,
        "automatic_refit": False,
        "automatic_promotion": False,
        "readiness_uses_outcome_labels": False,
        "model_fit_performed": False,
        "artifact_emitted": False,
        "schema_complete_close_windows": 0,
        "complete_executable_close_windows": 0,
        "unusable_close_windows": 0,
        "feature_unavailable_rows": 0,
        "timestamp_alignment_failures": 0,
        "timestamp_integrity_clean": True,
        "cohorts": {
            "NON_BTC_TRANSFER": {
                "minimum_complete_close_windows": 60,
                "windows_remaining": 60,
                "ready_for_locked_freeze": False,
            },
            "BTC": {
                "minimum_complete_close_windows": 150,
                "windows_remaining": 150,
                "ready_for_locked_freeze": False,
            },
        },
        "ready_for_any_locked_freeze": False,
        "status": "WAITING_FOR_COMPLETE_EXECUTABLE_WINDOWS",
    }
    dynamics_readiness = {
        "design_id": RTI_DYNAMICS_DESIGN_ID,
        "design_sha256": RTI_DYNAMICS_DESIGN_SHA256,
        "feature_schema_version": RTI_DYNAMICS_FEATURE_SCHEMA_VERSION,
        "feature_count": len(RTI_DYNAMICS_FEATURE_NAMES),
        "prospective_after_close_time": RTI_DYNAMICS_PROSPECTIVE_AFTER_CLOSE_TIME,
        "first_eligible_close_time": RTI_DYNAMICS_FIRST_ELIGIBLE_CLOSE_TIME,
        "paper_only": True,
        "notification_eligible": False,
        "real_trading_allowed": False,
        "automatic_refit": False,
        "automatic_promotion": False,
        "readiness_uses_outcome_labels": False,
        "model_fit_performed": False,
        "artifact_emitted": False,
        "schema_complete_close_windows": 0,
        "complete_executable_close_windows": 0,
        "unusable_close_windows": 0,
        "feature_unavailable_rows": 0,
        "timestamp_alignment_failures": 0,
        "timestamp_integrity_clean": True,
        "cohorts": {
            "NON_BTC_TRANSFER": {
                "minimum_complete_close_windows": 60,
                "windows_remaining": 60,
                "ready_for_locked_freeze": False,
            },
            "BTC": {
                "minimum_complete_close_windows": 150,
                "windows_remaining": 150,
                "ready_for_locked_freeze": False,
            },
        },
        "ready_for_any_locked_freeze": False,
        "status": "WAITING_FOR_COMPLETE_EXECUTABLE_WINDOWS",
    }
    lead_lag_readiness = {
        "design_id": RTI_LEAD_LAG_DESIGN_ID,
        "design_sha256": RTI_LEAD_LAG_DESIGN_SHA256,
        "feature_schema_version": RTI_LEAD_LAG_FEATURE_SCHEMA_VERSION,
        "feature_count": len(RTI_LEAD_LAG_FEATURE_NAMES),
        "prospective_after_close_time": RTI_LEAD_LAG_PROSPECTIVE_AFTER_CLOSE_TIME,
        "first_eligible_close_time": RTI_LEAD_LAG_FIRST_ELIGIBLE_CLOSE_TIME,
        "paper_only": True,
        "notification_eligible": False,
        "real_trading_allowed": False,
        "automatic_refit": False,
        "automatic_promotion": False,
        "readiness_uses_outcome_labels": False,
        "model_fit_performed": False,
        "artifact_emitted": False,
        "schema_complete_close_windows": 0,
        "complete_executable_close_windows": 0,
        "unusable_close_windows": 0,
        "feature_unavailable_rows": 0,
        "timestamp_alignment_failures": 0,
        "timestamp_integrity_clean": True,
        "cohorts": {
            "NON_BTC_TRANSFER": {
                "minimum_complete_close_windows": 60,
                "windows_remaining": 60,
                "ready_for_locked_freeze": False,
            },
            "BTC": {
                "minimum_complete_close_windows": 150,
                "windows_remaining": 150,
                "ready_for_locked_freeze": False,
            },
        },
        "ready_for_any_locked_freeze": False,
        "status": "WAITING_FOR_COMPLETE_EXECUTABLE_WINDOWS",
    }
    cross_venue_readiness = {
        "design_id": RTI_CROSS_VENUE_DESIGN_ID,
        "design_sha256": RTI_CROSS_VENUE_DESIGN_SHA256,
        "feature_schema_version": RTI_CROSS_VENUE_FEATURE_SCHEMA_VERSION,
        "feature_count": len(RTI_CROSS_VENUE_FEATURE_NAMES),
        "prospective_after_close_time": RTI_CROSS_VENUE_PROSPECTIVE_AFTER_CLOSE_TIME,
        "first_eligible_close_time": RTI_CROSS_VENUE_FIRST_ELIGIBLE_CLOSE_TIME,
        "paper_only": True,
        "notification_eligible": False,
        "real_trading_allowed": False,
        "automatic_refit": False,
        "automatic_promotion": False,
        "readiness_uses_outcome_labels": False,
        "model_fit_performed": False,
        "artifact_emitted": False,
        "schema_complete_close_windows": 0,
        "complete_executable_close_windows": 0,
        "unusable_close_windows": 0,
        "feature_unavailable_rows": 0,
        "timestamp_alignment_failures": 0,
        "timestamp_integrity_clean": True,
        "cohorts": {
            "NON_BTC_TRANSFER": {
                "minimum_complete_close_windows": 60,
                "windows_remaining": 60,
                "ready_for_locked_freeze": False,
            },
            "BTC": {
                "minimum_complete_close_windows": 150,
                "windows_remaining": 150,
                "ready_for_locked_freeze": False,
            },
        },
        "ready_for_any_locked_freeze": False,
        "status": "WAITING_FOR_COMPLETE_EXECUTABLE_WINDOWS",
    }
    independent_venue_readiness = {
        **cross_venue_readiness,
        "design_id": RTI_INDEPENDENT_VENUE_DESIGN_ID,
        "design_sha256": RTI_INDEPENDENT_VENUE_DESIGN_SHA256,
        "feature_schema_version": RTI_INDEPENDENT_VENUE_FEATURE_SCHEMA_VERSION,
        "feature_count": len(RTI_INDEPENDENT_VENUE_FEATURE_NAMES),
        "prospective_after_close_time": RTI_INDEPENDENT_VENUE_PROSPECTIVE_AFTER_CLOSE_TIME,
        "first_eligible_close_time": RTI_INDEPENDENT_VENUE_FIRST_ELIGIBLE_CLOSE_TIME,
    }
    independent_microstructure_readiness = {
        **independent_venue_readiness,
        "design_id": RTI_INDEPENDENT_MICROSTRUCTURE_DESIGN_ID,
        "design_sha256": RTI_INDEPENDENT_MICROSTRUCTURE_DESIGN_SHA256,
        "feature_schema_version": (
            RTI_INDEPENDENT_MICROSTRUCTURE_FEATURE_SCHEMA_VERSION
        ),
        "feature_count": len(RTI_INDEPENDENT_MICROSTRUCTURE_FEATURE_NAMES),
        "prospective_after_close_time": (
            RTI_INDEPENDENT_MICROSTRUCTURE_PROSPECTIVE_AFTER_CLOSE_TIME
        ),
        "first_eligible_close_time": (
            RTI_INDEPENDENT_MICROSTRUCTURE_FIRST_ELIGIBLE_CLOSE_TIME
        ),
    }
    compact_microstructure_readiness = {
        **independent_microstructure_readiness,
        "design_id": RTI_COMPACT_MICROSTRUCTURE_DESIGN_ID,
        "design_sha256": RTI_COMPACT_MICROSTRUCTURE_DESIGN_SHA256,
        "feature_schema_version": RTI_COMPACT_MICROSTRUCTURE_FEATURE_SCHEMA_VERSION,
        "feature_count": len(RTI_COMPACT_MICROSTRUCTURE_FEATURE_NAMES),
        "prospective_after_close_time": RTI_COMPACT_MICROSTRUCTURE_PROSPECTIVE_AFTER_CLOSE_TIME,
        "first_eligible_close_time": RTI_COMPACT_MICROSTRUCTURE_FIRST_ELIGIBLE_CLOSE_TIME,
    }
    cross_asset_regime_readiness = {
        **compact_microstructure_readiness,
        "design_id": RTI_CROSS_ASSET_REGIME_DESIGN_ID,
        "design_sha256": RTI_CROSS_ASSET_REGIME_DESIGN_SHA256,
        "feature_schema_version": RTI_CROSS_ASSET_REGIME_FEATURE_SCHEMA_VERSION,
        "feature_count": len(RTI_CROSS_ASSET_REGIME_FEATURE_NAMES),
        "prospective_after_close_time": RTI_CROSS_ASSET_REGIME_PROSPECTIVE_AFTER_CLOSE_TIME,
        "first_eligible_close_time": RTI_CROSS_ASSET_REGIME_FIRST_ELIGIBLE_CLOSE_TIME,
    }
    orthogonal_compact_readiness = {
        **cross_asset_regime_readiness,
        "design_id": RTI_ORTHOGONAL_COMPACT_DESIGN_ID,
        "design_sha256": RTI_ORTHOGONAL_COMPACT_DESIGN_SHA256,
        "feature_schema_version": RTI_ORTHOGONAL_COMPACT_FEATURE_SCHEMA_VERSION,
        "feature_count": len(RTI_ORTHOGONAL_COMPACT_FEATURE_NAMES),
        "prospective_after_close_time": (
            RTI_ORTHOGONAL_COMPACT_PROSPECTIVE_AFTER_CLOSE_TIME
        ),
        "first_eligible_close_time": (
            RTI_ORTHOGONAL_COMPACT_FIRST_ELIGIBLE_CLOSE_TIME
        ),
    }
    cohort_conditioned_compact_readiness = {
        **orthogonal_compact_readiness,
        "design_id": RTI_COHORT_CONDITIONED_COMPACT_DESIGN_ID,
        "design_sha256": RTI_COHORT_CONDITIONED_COMPACT_DESIGN_SHA256,
        "feature_schema_version": (
            RTI_COHORT_CONDITIONED_COMPACT_FEATURE_SCHEMA_VERSION
        ),
        "feature_count": len(RTI_COHORT_CONDITIONED_COMPACT_FEATURE_NAMES),
        "prospective_after_close_time": (
            RTI_COHORT_CONDITIONED_COMPACT_PROSPECTIVE_AFTER_CLOSE_TIME
        ),
        "first_eligible_close_time": (
            RTI_COHORT_CONDITIONED_COMPACT_FIRST_ELIGIBLE_CLOSE_TIME
        ),
        "v11_and_v12_remain_frozen_parallel_controls": True,
    }
    return {
        "dynamics_extension_v1": {
            "extension_schema_version": (
                RTI_EXACT_MICROSTRUCTURE_EXTENSION_SCHEMA_VERSION
            ),
            "schema_complete_close_windows": 0,
            "complete_executable_close_windows": 0,
            "unusable_close_windows": 0,
            "feature_unavailable_rows": 0,
            "outcome_labels_read": False,
            "model_fit_performed": False,
            "notification_eligible": False,
            "paper_only": True,
        },
        "model_feature_v1": {
            "schema_complete_close_windows": 0,
            "complete_executable_close_windows": 0,
            "unusable_close_windows": 0,
            "feature_unavailable_rows": 0,
            "timestamp_alignment_failures": 0,
        },
        "model_feature_v2": {
            "schema_complete_close_windows": 0,
            "complete_executable_close_windows": 0,
            "unusable_close_windows": 0,
            "feature_unavailable_rows": 0,
            "timestamp_alignment_failures": 0,
        },
        "model_feature_v3": {
            "schema_complete_close_windows": 0,
            "complete_executable_close_windows": 0,
            "unusable_close_windows": 0,
            "feature_unavailable_rows": 0,
            "timestamp_alignment_failures": 0,
            "primary_preregistered_design": False,
        },
        "model_feature_v4": {
            "schema_complete_close_windows": 0,
            "complete_executable_close_windows": 0,
            "unusable_close_windows": 0,
            "feature_unavailable_rows": 0,
            "timestamp_alignment_failures": 0,
            "primary_preregistered_design": True,
        },
        "model_feature_v5": {
            "schema_complete_close_windows": 0,
            "complete_executable_close_windows": 0,
            "unusable_close_windows": 0,
            "feature_unavailable_rows": 0,
            "timestamp_alignment_failures": 0,
            "primary_preregistered_design": False,
            "next_preregistered_design": False,
        },
        "model_feature_v6": {
            "schema_complete_close_windows": 0,
            "complete_executable_close_windows": 0,
            "unusable_close_windows": 0,
            "feature_unavailable_rows": 0,
            "timestamp_alignment_failures": 0,
            "primary_preregistered_design": False,
            "next_preregistered_design": False,
        },
        "model_feature_v7": {
            "schema_complete_close_windows": 0,
            "complete_executable_close_windows": 0,
            "unusable_close_windows": 0,
            "feature_unavailable_rows": 0,
            "timestamp_alignment_failures": 0,
            "primary_preregistered_design": False,
            "next_preregistered_design": False,
        },
        "model_feature_v8": {
            "schema_complete_close_windows": 0,
            "complete_executable_close_windows": 0,
            "unusable_close_windows": 0,
            "feature_unavailable_rows": 0,
            "timestamp_alignment_failures": 0,
            "primary_preregistered_design": False,
            "next_preregistered_design": False,
        },
        "model_feature_v9": {
            "schema_complete_close_windows": 0,
            "complete_executable_close_windows": 0,
            "unusable_close_windows": 0,
            "feature_unavailable_rows": 0,
            "timestamp_alignment_failures": 0,
            "primary_preregistered_design": False,
            "next_preregistered_design": False,
        },
        "model_feature_v10": {
            "schema_complete_close_windows": 0,
            "complete_executable_close_windows": 0,
            "unusable_close_windows": 0,
            "feature_unavailable_rows": 0,
            "timestamp_alignment_failures": 0,
            "primary_preregistered_design": False,
            "next_preregistered_design": False,
        },
        "model_feature_v11": {
            "schema_complete_close_windows": 0,
            "complete_executable_close_windows": 0,
            "unusable_close_windows": 0,
            "feature_unavailable_rows": 0,
            "timestamp_alignment_failures": 0,
            "primary_preregistered_design": False,
            "next_preregistered_design": False,
        },
        "model_feature_v12": {
            "schema_complete_close_windows": 0,
            "complete_executable_close_windows": 0,
            "unusable_close_windows": 0,
            "feature_unavailable_rows": 0,
            "timestamp_alignment_failures": 0,
            "primary_preregistered_design": False,
            "next_preregistered_design": False,
        },
        "model_feature_v13": {
            "schema_complete_close_windows": 0,
            "complete_executable_close_windows": 0,
            "unusable_close_windows": 0,
            "feature_unavailable_rows": 0,
            "timestamp_alignment_failures": 0,
            "primary_preregistered_design": False,
            "next_preregistered_design": True,
        },
        "preregistered_model_readiness": readiness,
        "dynamics_v5_model_readiness": dynamics_readiness,
        "lead_lag_v6_model_readiness": lead_lag_readiness,
        "cross_venue_v7_model_readiness": cross_venue_readiness,
        "independent_venue_v8_model_readiness": independent_venue_readiness,
        "independent_microstructure_v9_model_readiness": (
            independent_microstructure_readiness
        ),
        "independent_microstructure_compact_v10_model_readiness": (
            compact_microstructure_readiness
        ),
        "cross_asset_regime_v11_model_readiness": cross_asset_regime_readiness,
        "orthogonal_compact_v12_model_readiness": orthogonal_compact_readiness,
        "cohort_conditioned_compact_v13_model_readiness": (
            cohort_conditioned_compact_readiness
        ),
    }


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _drift_lineage_config() -> dict[str, Any]:
    """Allow-listed policy/config inputs only; never hash credentials."""
    return {
        "accepted_assets": ["DOGE", "HYPE", "SOL", "XRP"],
        "ask_min_cents": 60.0,
        "ask_max_cents": 73.0,
        "core_distance_max": 3e-5,
        "asymmetric_distance_max": 1e-4,
        "asymmetric_distance_ask_min": 65.0,
        "flip_max": 30.0,
        "spread_max_cents": 2.0,
        "live_requires_fresh_positive_60s_flow": True,
        "live_spread_only_eligible": False,
        "consensus_fallback_sources": ["interval_15m", "v95_15m", "btc_15m"],
        "consensus_fallback_required_agreements": 2,
        "v91_full_path_yes_fraction_min": 0.75,
        "v95_required_side": "YES",
        "review_bars": [30, 60, 150],
        "spot_snapshot_max_age_seconds": os.environ.get(
            "Q15_V3_DRIFT_SPOT_SNAPSHOT_MAX_AGE_SECONDS", "15"
        ),
        "spot_book_max_age_seconds": os.environ.get(
            "Q15_V3_DRIFT_SPOT_BOOK_MAX_AGE_SECONDS", "15"
        ),
        "spot_trade_max_age_seconds": os.environ.get(
            "Q15_V3_DRIFT_SPOT_TRADE_MAX_AGE_SECONDS", "15"
        ),
    }


def _drift_no_expansion_lineage_config() -> dict[str, Any]:
    return {
        "accepted_side": "NO",
        "asset_entry_bands_cents": {
            "XRP": [60.0, 69.0],
            "HYPE": [60.0, 64.0],
            "DOGE": [65.0, 69.0],
        },
        "distance_max": 3e-5,
        "flip_max": 30.0,
        "legacy_flow_max_exclusive": 0.0,
        "legacy_spread_max_cents": 2.0,
        "research_only": True,
        "notification_eligible": False,
        "review_bars": [30, 60, 150],
    }


def _drift_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return "{}"


def _drift_num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _with_drift_lineage_and_grade(
    row: Mapping[str, Any],
    *,
    expected_side: str | None = None,
    lineage_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Stamp one point-in-time Drift row without manufacturing missing evidence."""
    out = dict(row)
    candidate_side = str(expected_side or source_side(out) or "YES").upper()
    if candidate_side not in {"YES", "NO"}:
        candidate_side = "YES"
    config = dict(lineage_config or _drift_lineage_config())
    config["evidence_expected_side"] = candidate_side
    stamp = lineage_stamp(
        feature_schema_version=DRIFT_DECISION_FEATURE_SCHEMA_VERSION,
        config=config,
    )
    out.update(stamp)
    out.setdefault("source_captured_at", out.get("created_at"))
    out["evidence_as_of"] = out.get("created_at")

    availability = out.get("drift_evidence_availability")
    if not isinstance(availability, Mapping):
        availability = {}
    availability = {str(key): value for key, value in availability.items()}
    ages = out.get("drift_evidence_ages")
    if not isinstance(ages, Mapping):
        ages = {}
    ages = {str(key): value for key, value in ages.items()}
    index_status = str(out.get("index_status") or "missing").lower()
    availability["settlement_index"] = {
        "available": index_status == "ok",
        "status": index_status,
        "missing_reason": out.get("index_missing_reason"),
        "index_id": out.get("index_id"),
        "source_at": out.get("index_source_ts"),
    }
    ages["settlement_index"] = out.get("index_age_s")
    kalshi_status = str(out.get("kalshi_depth_status") or "missing").lower()
    availability["kalshi_13m"] = {
        "available": kalshi_status == "ok",
        "status": kalshi_status,
        "missing_reason": out.get("kalshi_depth_missing_reason"),
        "retry_used": out.get("kalshi_depth_retry_used"),
    }
    ages["kalshi_quote"] = out.get("quote_age_seconds")
    spot_status = str(out.get("spot_depth_status") or "missing").lower()
    availability["spot_current"] = {
        "available": spot_status == "ok",
        "status": spot_status,
        "missing_reason": out.get("spot_depth_missing_reason"),
    }
    ages["spot_snapshot"] = out.get("spot_depth_snapshot_age_seconds")
    ages["spot_book"] = out.get("spot_depth_age_seconds")
    ages["spot_trade"] = out.get("spot_depth_trade_age_seconds")
    out["drift_evidence_availability"] = availability
    out["drift_evidence_ages"] = ages
    out["feature_availability_json"] = _drift_json(availability)
    out["feature_age_json"] = _drift_json(ages)

    ask = _drift_num(out.get("entry_ask_cents"))
    distance = _drift_num(out.get("distance_sigma"))
    flip = _drift_num(out.get("flip_probability"))
    spread = _drift_num(out.get("spread_cents"))
    core_complete = all(value is not None for value in (ask, distance, flip, spread))
    out["data_complete"] = bool(core_complete)

    v91 = _drift_num(out.get("drift_v91_yes_fraction_all"))
    v95_side = str(out.get("drift_v95_15m_side") or "").upper()
    v95_flow = _drift_num(out.get("drift_v95_15m_flow_score"))
    btc_side = str(out.get("drift_btc_15m_side") or "").upper()
    breadth = out.get("drift_asymmetric_breadth")
    flow_coverage = _drift_num(out.get("drift_flow_coverage"))
    full_complete = (
        core_complete
        and bool(out.get("drift_evidence_complete"))
        and v91 is not None
        and v95_side in {"YES", "NO"}
        and v95_flow is not None
        and btc_side in {"YES", "NO"}
        and breadth is not None
        and flow_coverage is not None
        and flow_coverage >= 1.0
    )
    out["full_feature_complete"] = bool(full_complete)
    spot_available = spot_status in {
        "ok", "stale", "missing"
    }
    if full_complete:
        cohort = "FULL_FEATURE"
    elif spot_available:
        cohort = "FLOW_ENABLED"
    else:
        cohort = "CORE_ONLY"
    out["feature_cohort"] = cohort

    reasons: list[str] = []
    if not full_complete:
        grade = "INCOMPLETE"
        reasons.append("DRIFT_FULL_FEATURES_INCOMPLETE")
    else:
        v91_pass = bool(
            v91 is not None
            and (v91 >= 0.75 if candidate_side == "YES" else v91 <= 0.25)
        )
        v95_pass = v95_side == candidate_side
        btc_contradicts = (
            str(out.get("asset") or "").upper() in {"SOL", "XRP"}
            and btc_side in {"YES", "NO"}
            and btc_side != candidate_side
        )
        if v91_pass:
            reasons.append(
                "DRIFT_V91_FULL_PATH_PASS"
                if candidate_side == "YES"
                else "DRIFT_V91_FULL_PATH_AGREES_NO"
            )
        else:
            reasons.append(
                "DRIFT_V91_FULL_PATH_LOW"
                if candidate_side == "YES"
                else "DRIFT_V91_FULL_PATH_CONTRADICTS_NO"
            )
        if v95_pass:
            reasons.append(
                "DRIFT_V95_15M_AGREES"
                if candidate_side == "YES"
                else "DRIFT_V95_15M_AGREES_NO"
            )
        else:
            reasons.append(
                "DRIFT_V95_15M_CONTRADICTS"
                if candidate_side == "YES"
                else "DRIFT_V95_15M_CONTRADICTS_NO"
            )
        if btc_contradicts:
            reasons.append(f"DRIFT_BTC_15M_CONTRADICTS_{candidate_side}")
        grade = "A" if v91_pass and v95_pass and not btc_contradicts else (
            "B" if (v91_pass or v95_pass) and not btc_contradicts else "C"
        )
    out["evidence_grade"] = grade
    out["evidence_reason_codes"] = ",".join(reasons)
    evidence_bundle = {
        "as_of": out.get("evidence_as_of"),
        "expected_side": candidate_side,
        "availability": dict(availability),
        "ages": dict(ages),
        "grade": grade,
        "reason_codes": reasons,
        "v91_yes_fraction_all": v91,
        "v95_15m_side": v95_side or None,
        "v95_15m_flow_score": v95_flow,
        "btc_15m_side": btc_side or None,
        "core_breadth": out.get("drift_core_breadth"),
        "asymmetric_breadth": breadth,
        "flow": {
            "1m": out.get("drift_flow_1m"),
            "3m": out.get("drift_flow_3m"),
            "5m": out.get("drift_flow_5m"),
            "13m": out.get("drift_flow_13m"),
            "positive_bucket_fraction": out.get("drift_flow_positive_bucket_fraction"),
            "sign_flips": out.get("drift_flow_sign_flips"),
            "persistence": out.get("drift_flow_persistence"),
            "coverage": flow_coverage,
        },
    }
    out["drift_evidence"] = evidence_bundle
    out["drift_evidence_json"] = _drift_json(evidence_bundle)
    return out


def enabled() -> bool:
    return _bool("Q15_STRATEGY_BOTS_ENABLED", False)


def allow_duplicate_hype_windows() -> bool:
    return _bool("Q15_V3_HYPE_ALLOW_DUPLICATE_WINDOW", False)


def telegram_enabled() -> bool:
    return _bool("Q15_V3_TELEGRAM_ENABLED", False)


def research_telegram_enabled() -> bool:
    return _bool("Q15_V3_RESEARCH_TELEGRAM_ENABLED", False)


def depth_formula_telegram_enabled() -> bool:
    return _bool("Q15_V3_DEPTH_FORMULA_TELEGRAM_ENABLED", True)


def thirteen_m_sniper_notify_enabled() -> bool:
    return _bool("Q15_V3_13M_SNIPER_NOTIFY", False)


def rti_path_13m_enabled() -> bool:
    return _bool("Q15_V3_RTI_PATH_13M", False)


def rti_path_13m_notify_enabled() -> bool:
    return _bool("Q15_V3_RTI_PATH_13M_NOTIFY", False)


def rti_microstructure_v11_paper_record_enabled() -> bool:
    """Explicit manual opt-in for prospective V11 ledger evidence only."""
    return _bool("Q15_V3_RTI_MICROSTRUCTURE_V11_PAPER_RECORD", False)


def rti_path_13m_assets() -> set[str]:
    raw = os.environ.get("Q15_V3_RTI_PATH_13M_ASSETS", "BTC")
    requested = {part.strip().upper() for part in raw.split(",") if part.strip()}
    return requested.intersection(RTI_PATH_13M_INDEX_IDS)


# The three 2026-07-05 books default ON (owner directive: "make everything on by
# default"). Delivery still requires the V3 Telegram channel itself to be enabled.
def warn_flip_notify_enabled() -> bool:
    return _bool("Q15_V3_WARN_FLIP_NOTIFY", True)


def fav_10m_notify_enabled() -> bool:
    return _bool("Q15_V3_FAV10M_NOTIFY", True)


def top_pick_notify_enabled() -> bool:
    return _bool("Q15_V3_TOP_PICK_13M_NOTIFY", True)


def precision13_notify_enabled() -> bool:
    return _bool("Q15_V3_PRECISION_13M_NOTIFY", True)


def drift_notify_enabled() -> bool:
    # Legacy compatibility flag. Raw Drift is shadow-only and this route is no
    # longer called by record_drift_pick_row.
    return _bool("Q15_V3_DRIFT_13M_NOTIFY", False)


def drift_flow_spread_notify_enabled() -> bool:
    return _bool("Q15_V3_DRIFT_FLOW_SPREAD_NOTIFY", True)


def drift_addon_notify_enabled() -> bool:
    return _bool("Q15_V3_DRIFT_ADDON_NOTIFY", False)


def drift_latequal_notify_enabled() -> bool:
    return _bool("Q15_V3_DRIFT_LATEQUAL_NOTIFY", False)


def drift_no_mirror_notify_enabled() -> bool:
    return _bool("Q15_V3_DRIFT_NO_MIRROR_NOTIFY", False)


def drift_no_expansion_notify_enabled() -> bool:
    # Hard quarantine: retained for compatibility with callers/config audits,
    # but no environment value can make this provisional lane notify.
    return False


def suppress_owned_source_notifications() -> bool:
    return _bool("Q15_V3_SUPPRESS_OWNED_SOURCE_NOTIFICATIONS", False)


def hvf_wrapper_only_notifications() -> bool:
    return _bool("Q15_V3_HVF_DEPTH_FLOW_NOTIFICATIONS_ONLY", False)


def empirical_delivery_guard_enabled() -> bool:
    return _bool("Q15_V3_EMPIRICAL_DELIVERY_GUARD", True)


def empirical_guard_late_intervals() -> set[str]:
    raw = os.environ.get("Q15_V3_EMPIRICAL_LATE_INTERVALS", "10M,11M,12M,13M,14M,15M")
    return {part.strip().upper() for part in raw.split(",") if part.strip()}


def db_path() -> str:
    return os.environ.get("Q15_STRATEGY_BOTS_DB") or "data/q15_strategy_bots_v3.sqlite3"


def _enrich_source_row(
    row: Mapping[str, Any],
    *,
    btc_context: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    enriched: Mapping[str, Any] = row
    try:
        enriched = enrich_coinbase_l2(enriched)
    except (OSError, ValueError) as exc:
        logger.warning("v3 Coinbase L2 enrichment skipped: %s", exc)
    except Exception:  # noqa: BLE001 - non-critical point-in-time feature path
        logger.warning("v3 Coinbase L2 enrichment failed", exc_info=True)
    try:
        enriched = enrich_kraken_l3(enriched)
    except (OSError, ValueError) as exc:
        logger.warning("v3 Kraken L3 enrichment skipped: %s", exc)
    except Exception:  # noqa: BLE001 - non-critical point-in-time feature path
        logger.warning("v3 Kraken L3 enrichment failed", exc_info=True)
    try:
        enriched = enrich_btc_regime(enriched, btc_context=btc_context)
    except (OSError, ValueError) as exc:
        logger.warning("v3 BTC regime enrichment skipped: %s", exc)
    except Exception:  # noqa: BLE001 - non-critical point-in-time feature path
        logger.warning("v3 BTC regime enrichment failed", exc_info=True)
    return enriched


def get_ledger() -> StrategyBotLedger | None:
    global _ledger
    if not enabled():
        return None
    if _ledger is None:
        _ledger = StrategyBotLedger(db_path())
    return _ledger


def get_telegram() -> V3Telegram:
    global _telegram
    if _telegram is None:
        _telegram = V3Telegram()
    return _telegram


def drift_outbox_enabled() -> bool:
    """Explicit opt-in for the dedicated Drift delivery outbox."""
    return _bool("Q15_V3_DRIFT_OUTBOX_ENABLED", False)


def drift_outbox_path() -> str:
    return (
        os.environ.get("Q15_V3_DRIFT_OUTBOX_DB")
        or "data/q15_drift_telegram_outbox.sqlite3"
    )


def reset_drift_outbox() -> None:
    """Close and drop the optional outbox (test/reconfiguration hook)."""
    global _drift_outbox, _drift_outbox_identity
    current = _drift_outbox
    _drift_outbox = None
    _drift_outbox_identity = None
    if current is not None:
        try:
            current.close()
        except Exception:  # noqa: BLE001 - shutdown must remain best-effort
            logger.debug("v3 Drift outbox close failed", exc_info=True)


def get_drift_outbox() -> Any | None:
    """Return the dedicated retry outbox only under explicit opt-in.

    The V3 Telegram gate/credentials remain authoritative.  When that channel
    is muted we preserve the normal MUTED result instead of accumulating rows
    that can never be delivered.
    """
    global _drift_outbox, _drift_outbox_identity
    if not drift_outbox_enabled():
        if _drift_outbox is not None:
            reset_drift_outbox()
        return None
    telegram = get_telegram()
    if not bool(getattr(telegram, "enabled", False)):
        return None
    path = drift_outbox_path()
    identity = (id(telegram), path)
    if _drift_outbox is not None and _drift_outbox_identity == identity:
        return _drift_outbox
    reset_drift_outbox()
    try:
        from notifications.outbox_v9 import ReliableTelegramOutbox

        _drift_outbox = ReliableTelegramOutbox(
            None, telegram, sqlite_path=path,
        )
        _drift_outbox_identity = identity
    except Exception:  # noqa: BLE001 - optional rail must never block recording
        logger.warning("v3 Drift outbox unavailable; delivery remains fail-closed", exc_info=True)
        _drift_outbox = None
        _drift_outbox_identity = None
    return _drift_outbox


def initialize_drift_outbox() -> bool:
    """Start the optional Drift retry worker during application startup.

    No database or worker is created unless both the dedicated outbox and the
    credentialed V3 Telegram channel are enabled.
    """
    if not enabled() or not telegram_enabled() or not drift_outbox_enabled():
        return False
    telegram = get_telegram()
    if not bool(getattr(telegram, "enabled", False)):
        return False
    if get_drift_outbox() is None:
        return False
    # Credit terminal outcomes left by the previous process immediately.  The
    # worker may also finish a pending attempt concurrently; the periodic pass
    # in the live loop will pick up that transition without a source replay.
    summary = reconcile_drift_delivery_statuses()
    if summary["updated"]:
        logger.info(
            "reconciled %s Drift delivery outcomes at startup",
            summary["updated"],
        )
    return True


def _drift_delivery_key(
    bot_name: str,
    window_key: int,
    ticker: str | None = None,
    *,
    grouped: bool = False,
    strategy_version: str = STRATEGY_VERSION,
) -> str:
    scope = "group" if grouped else "row"
    parts = [str(strategy_version), "drift", str(bot_name), scope, str(int(window_key))]
    if ticker:
        parts.append(str(ticker))
    return ":".join(parts)


def _rti_path_13m_delivery_key(
    window_key: int,
    ticker: str,
    *,
    rule_version: str = RTI_PATH_13M_RULE_VERSION,
    strategy_version: str = STRATEGY_VERSION,
) -> str:
    return (
        f"{strategy_version}:rti_path_13m:{rule_version}:"
        f"{BOT_RTI_PATH_13M}:"
        f"row:{int(window_key)}:{ticker}"
    )


def _stored_drift_delivery_key(row: Mapping[str, Any]) -> str | None:
    """Rebuild the deterministic outbox key from one persisted decision."""
    bot_name = str(row.get("bot_name") or "")
    try:
        window_key = int(row.get("window_key"))
    except (TypeError, ValueError):
        return None
    if bot_name == BOT_RTI_PATH_13M:
        ticker = str(row.get("ticker") or "")
        if not ticker:
            return None
        return _rti_path_13m_delivery_key(
            window_key,
            ticker,
            rule_version=str(
                row.get("source_model_version") or RTI_PATH_13M_RULE_VERSION
            ),
            strategy_version=str(row.get("strategy_version") or STRATEGY_VERSION),
        )
    grouped = bot_name in {BOT_DRIFT_NO_EXPANSION, BOT_DRIFT_NO_MIRROR}
    ticker = None if grouped else str(row.get("ticker") or "")
    if not bot_name or (not grouped and not ticker):
        return None
    return _drift_delivery_key(
        bot_name,
        window_key,
        ticker,
        grouped=grouped,
        strategy_version=str(row.get("strategy_version") or STRATEGY_VERSION),
    )


def reconcile_drift_delivery_statuses(limit: int = 100) -> dict[str, int]:
    """Copy terminal dedicated-outbox states into the strategy ledger.

    This is intentionally a local, read-mostly maintenance pass: it never
    creates an outbox, enqueues a payload, or calls Telegram.  Work is bounded
    by ``limit`` and terminal updates are committed as one ledger transaction,
    making the callable safe for startup, the live loop, or a health poll.
    """
    summary = {
        "scanned": 0,
        "keys_checked": 0,
        "updated": 0,
        "sent": 0,
        "expired": 0,
        "dead_letter": 0,
        "busy": 0,
    }
    if not _drift_reconcile_lock.acquire(blocking=False):
        summary["busy"] = 1
        return summary
    try:
        # Do not call get_drift_outbox() here: constructing it can start its
        # network worker.  Reconciliation only observes an already-initialized
        # outbox and therefore cannot itself send anything.
        outbox = _drift_outbox
        ledger = get_ledger()
        if outbox is None or ledger is None:
            return summary
        rows = ledger.drift_notifications_to_reconcile(limit=limit)
        summary["scanned"] = len(rows)
        status_cache: dict[str, str | None] = {}
        updates: list[tuple[int, str, str | None]] = []
        for row in rows:
            key = _stored_drift_delivery_key(row)
            if key is None:
                continue
            if key not in status_cache:
                status_cache[key] = outbox.status_by_key(key)
            terminal = str(status_cache[key] or "").upper()
            if terminal not in {"SENT", "EXPIRED", "DEAD_LETTER"}:
                continue
            error = None if terminal == "SENT" else f"outbox:{terminal}"
            updates.append((int(row["id"]), terminal, error))
            summary[terminal.lower()] += 1
        summary["keys_checked"] = len(status_cache)
        summary["updated"] = ledger.reconcile_drift_notification_terminals(updates)
        return summary
    except Exception:  # noqa: BLE001 - maintenance must never block the live loop
        logger.warning("v3 Drift delivery reconciliation failed (ignored)", exc_info=True)
        return summary
    finally:
        _drift_reconcile_lock.release()


def request_drift_delivery_reconcile(limit: int = 100) -> dict[str, Any]:
    """Start one bounded local reconcile without blocking the capture loop.

    SQLite/OneDrive contention can make even a bounded reconciliation take
    minutes.  The exact RTI scheduler must never wait for that maintenance.
    At most one daemon worker is active; later refresh cycles only observe it.
    """
    global _drift_reconcile_worker
    bounded_limit = max(1, min(int(limit), 1000))
    submitted_at = time.time()
    with _drift_reconcile_worker_lock:
        current = _drift_reconcile_worker
        if current is not None and current.is_alive():
            return {
                "submitted": False,
                "inflight": True,
                "submitted_at": _drift_reconcile_worker_state[
                    "submitted_at"
                ],
            }

        def _run() -> None:
            started = time.time()
            summary = None
            error = None
            try:
                summary = reconcile_drift_delivery_statuses(bounded_limit)
            except Exception as exc:  # defensive: public callable is fail-safe
                error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "background Drift delivery reconciliation failed",
                    exc_info=True,
                )
            finished = time.time()
            with _drift_reconcile_worker_lock:
                _drift_reconcile_worker_state.update({
                    "inflight": False,
                    "finished_at": finished,
                    "last_duration_seconds": max(0.0, finished - started),
                    "last_summary": summary,
                    "last_error": error,
                })

        _drift_reconcile_worker_state.update({
            "inflight": True,
            "submitted_at": submitted_at,
            "finished_at": None,
            "last_duration_seconds": None,
            "last_error": None,
        })
        worker = threading.Thread(
            target=_run,
            name="q15-drift-delivery-reconcile",
            daemon=True,
        )
        _drift_reconcile_worker = worker
        worker.start()
        return {
            "submitted": True,
            "inflight": True,
            "submitted_at": submitted_at,
        }


def drift_delivery_reconcile_worker_health() -> dict[str, Any]:
    with _drift_reconcile_worker_lock:
        output = dict(_drift_reconcile_worker_state)
        current = _drift_reconcile_worker
        output["inflight"] = bool(current is not None and current.is_alive())
    submitted = output.get("submitted_at")
    output["inflight_seconds"] = (
        max(0.0, time.time() - float(submitted))
        if output["inflight"] and submitted is not None else 0.0
    )
    output["live_refresh_loop_blocking_allowed"] = False
    return output


def _normalize_delivery_result(result: Any) -> dict[str, Any]:
    if isinstance(result, Mapping):
        return {
            "ok": bool(result.get("ok")),
            "delivered": bool(result.get("delivered")),
            "muted": bool(result.get("muted")),
            "message_id": result.get("message_id"),
            "error": result.get("error"),
        }
    delivered = bool(result)
    return {
        "ok": delivered,
        "delivered": delivered,
        "muted": False,
        "message_id": None,
        "error": None if delivered else "telegram_send_failed",
    }


def enqueue_v3_outbox_notification(
    text: str,
    *,
    idempotency_key: str,
    expires_at: float,
) -> dict[str, Any]:
    """Persist a generic V3 message without ever doing network I/O inline.

    MarketLead uses this producer-only surface so its data collection loop can
    share the durable V3 delivery worker while remaining fail-closed if that
    worker is disabled or unavailable.
    """
    try:
        expiry = float(expires_at)
    except (TypeError, ValueError):
        expiry = float("nan")
    if not math.isfinite(expiry):
        return {
            "ok": False,
            "delivered": False,
            "muted": False,
            "message_id": None,
            "error": "v3_expiry_invalid",
        }
    if time.time() >= expiry:
        return {
            "ok": False,
            "delivered": False,
            "muted": False,
            "message_id": None,
            "error": "outbox:EXPIRED",
            "outbox_status": "EXPIRED",
        }
    if not telegram_enabled():
        return {
            "ok": False,
            "delivered": False,
            "muted": True,
            "message_id": None,
            "error": "v3_telegram_disabled",
        }
    if not drift_outbox_enabled():
        return {
            "ok": False,
            "delivered": False,
            "muted": False,
            "message_id": None,
            "error": "v3_outbox_required",
        }
    outbox = get_drift_outbox()
    if outbox is None:
        return {
            "ok": False,
            "delivered": False,
            "muted": False,
            "message_id": None,
            "error": "outbox_unavailable",
        }
    raw_result = outbox.enqueue_with_result(
        text,
        idempotency_key=str(idempotency_key),
        expires_at=expiry,
    )
    result = _normalize_delivery_result(raw_result)
    result["outbox_status"] = (
        raw_result.get("outbox_status")
        if isinstance(raw_result, Mapping)
        else None
    ) or outbox.status_by_key(str(idempotency_key))
    return result


def v3_outbox_notification_status(idempotency_key: str) -> str | None:
    """Read one status without constructing an outbox or starting a worker."""
    outbox = _drift_outbox
    if outbox is None:
        return None
    try:
        status = outbox.status_by_key(str(idempotency_key))
    except Exception:  # noqa: BLE001 - health reconciliation is best effort
        return None
    return None if status is None else str(status)


def _send_drift_notification(
    text: str,
    *,
    idempotency_key: str,
    expires_at: float | None,
) -> dict[str, Any]:
    """Send directly or enqueue the exact rendered Drift payload for retry."""
    if expires_at is None:
        return {
            "ok": False,
            "delivered": False,
            "muted": False,
            "message_id": None,
            "error": "drift_expiry_missing",
        }
    try:
        expiry = float(expires_at)
    except (TypeError, ValueError):
        expiry = float("nan")
    if not math.isfinite(expiry):
        return {
            "ok": False,
            "delivered": False,
            "muted": False,
            "message_id": None,
            "error": "drift_expiry_invalid",
        }
    outbox = get_drift_outbox()
    if outbox is None:
        # Explicit outbox opt-in is a nonblocking safety contract.  Construction
        # failure must not silently turn the live refresh loop into a network
        # caller; a later replay can recover after the rail becomes available.
        if drift_outbox_enabled():
            return {
                "ok": False,
                "delivered": False,
                "muted": False,
                "message_id": None,
                "error": "outbox_unavailable",
            }
        if time.time() >= expiry:
            return {
                "ok": False,
                "delivered": False,
                "muted": False,
                "message_id": None,
                "error": "outbox:EXPIRED",
            }
        telegram = get_telegram()
        sender = getattr(telegram, "send_with_result", None)
        try:
            raw = sender(text) if callable(sender) else telegram.send(text)
        except Exception as exc:  # noqa: BLE001 - delivery cannot break recording
            return {
                "ok": False,
                "delivered": False,
                "muted": False,
                "message_id": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
        return _normalize_delivery_result(raw)

    # The live refresh loop is a producer only.  Persist now; the outbox worker
    # performs all network I/O so a slow Telegram request cannot stall capture.
    raw_result = outbox.enqueue_with_result(
        text, idempotency_key=idempotency_key, expires_at=expiry,
    )
    result = _normalize_delivery_result(raw_result)
    outbox_status = (
        raw_result.get("outbox_status")
        if isinstance(raw_result, Mapping)
        else None
    ) or outbox.status_by_key(idempotency_key)
    result["outbox_status"] = outbox_status
    if (
        not result.get("delivered")
        and not result.get("muted")
        and not result.get("error")
        and outbox_status
    ):
        result["error"] = f"outbox:{outbox_status}"
    return result


def _delivery_fields(result: Mapping[str, Any]) -> tuple[str, int | None, str | None]:
    if result.get("delivered") or result.get("outbox_status") == "SENT":
        return "SENT", result.get("message_id"), result.get("error")
    if result.get("muted"):
        return "MUTED", None, result.get("error")
    if result.get("outbox_status") in {"PENDING", "SENDING", "FAILED_RETRYABLE"}:
        return "QUEUED_RETRY", None, result.get("error")
    if result.get("outbox_status") in {"EXPIRED", "DEAD_LETTER"}:
        terminal = str(result["outbox_status"])
        return terminal, None, result.get("error") or f"outbox:{terminal}"
    return "DELIVERY_FAILED", None, result.get("error")


def _stored_decision(
    ledger: StrategyBotLedger,
    decision: BotDecision,
    source_row: Mapping[str, Any],
    *,
    source_system: str,
) -> tuple[int | None, dict[str, Any] | None]:
    """Insert a decision or retrieve its durable duplicate for recovery."""
    row_id = ledger.record_decision(
        decision, source_row, source_system=source_system,
    )
    if row_id is not None:
        return row_id, ledger.row_by_id(row_id)
    return None, ledger.row_for_decision(
        decision, source_row, source_system=source_system,
    )


def _notification_needs_delivery(
    ledger: StrategyBotLedger,
    row: Mapping[str, Any],
    *,
    idempotency_key: str,
) -> bool:
    """Return whether a stored row needs enqueue, reconciling worker outcomes."""
    status = row.get("notification_status")
    if status in {"SENT", "MUTED"}:
        return False
    if status == "QUEUED_RETRY":
        outbox = get_drift_outbox()
        if outbox is None:
            return False
        outbox_status = outbox.status_by_key(idempotency_key)
        if outbox_status == "SENT":
            ledger.mark_notification(
                int(row["id"]), status="SENT", message_id=None, error=None,
            )
        elif outbox_status in {"DEAD_LETTER", "EXPIRED"}:
            ledger.mark_notification(
                int(row["id"]),
                status=outbox_status,
                message_id=None,
                error=f"outbox:{outbox_status}",
            )
        # A queued row never creates a second delivery attempt from replay.  Its
        # deterministic outbox record (or terminal state) remains authoritative.
        return False
    return status is None or status == "DELIVERY_FAILED"


def _group_expiry(rows: Sequence[Mapping[str, Any]]) -> float | None:
    """Earliest settlement boundary, failing closed if any row lacks one."""
    expiries: list[float] = []
    for row in rows:
        try:
            value = float(row.get("close_time"))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value):
            return None
        expiries.append(value)
    return min(expiries) if expiries else None


def _with_thirteen_m_sniper_context(
    ledger: StrategyBotLedger,
    row: Mapping[str, Any],
) -> Mapping[str, Any]:
    global _thirteen_m_stats_warning_logged, _thirteen_m_flow_warning_logged
    if str(row.get("interval") or "").upper() != "13M":
        return row
    out = dict(row)
    try:
        stats = ledger.bot_accepted_resolved_stats(bot_name=BOT_THIRTEEN_M_SNIPER)
        out.setdefault("thirteen_m_sniper_resolved_n", stats.get("n"))
        out.setdefault("thirteen_m_sniper_correct", stats.get("correct"))
        out.setdefault("thirteen_m_sniper_accuracy", stats.get("accuracy"))
        out.setdefault("thirteen_m_sniper_wilson_lb", stats.get("wilson_lb"))
    except Exception:  # noqa: BLE001 - stats are advisory; recording must continue
        if not _thirteen_m_stats_warning_logged:
            logger.warning("v3 13M sniper stats unavailable", exc_info=True)
            _thirteen_m_stats_warning_logged = True
    try:
        flow_p70 = ledger.trailing_abs_flow_percentile(
            asset=str(row.get("asset") or "").upper() or None,
            created_before=float(row.get("created_at")) if row.get("created_at") is not None else None,
        )
        if flow_p70 is not None and out.get("spot_depth_trade_net_notional_60s_abs_p70") is None:
            out["spot_depth_trade_net_notional_60s_abs_p70"] = flow_p70
    except (TypeError, ValueError):
        logger.debug("v3 13M sniper flow percentile skipped for invalid created_at")
    except Exception:  # noqa: BLE001 - stats are advisory; recording must continue
        if not _thirteen_m_flow_warning_logged:
            logger.warning("v3 13M sniper flow percentile unavailable", exc_info=True)
            _thirteen_m_flow_warning_logged = True
    return out


_book_stats_warning_logged: set[str] = set()


def _with_book_stats_context(
    ledger: StrategyBotLedger,
    row: Mapping[str, Any],
    *,
    bot_name: str,
    prefix: str,
    threshold_rule_version: str | None = None,
    decision_status: str = ACCEPTED,
) -> Mapping[str, Any]:
    """Inject a bot's resolved ACCEPTED stats so its rules can self-govern
    (empirical EV + auto-mute), mirroring the 13M sniper convention."""
    out = dict(row)
    try:
        if decision_status == ACCEPTED:
            stats = ledger.bot_accepted_resolved_stats(
                bot_name=bot_name,
                threshold_rule_version=threshold_rule_version,
            )
        else:
            stats = ledger.bot_resolved_stats(
                bot_name=bot_name,
                decision_status=decision_status,
                threshold_rule_version=threshold_rule_version,
            )
        out.setdefault(f"{prefix}_resolved_n", stats.get("n"))
        out.setdefault(f"{prefix}_correct", stats.get("correct"))
        out.setdefault(f"{prefix}_accuracy", stats.get("accuracy"))
        out.setdefault(f"{prefix}_wilson_lb", stats.get("wilson_lb"))
        out.setdefault(f"{prefix}_net_pnl_cents", stats.get("net_pnl_cents"))
        out.setdefault(f"{prefix}_wins", stats.get("correct"))
        out.setdefault(f"{prefix}_total_pnl_cents", stats.get("net_pnl_cents"))
    except Exception:  # noqa: BLE001 - stats are advisory; recording must continue
        if bot_name not in _book_stats_warning_logged:
            logger.warning("v3 %s stats unavailable", bot_name, exc_info=True)
            _book_stats_warning_logged.add(bot_name)
    return out


def _with_fav_10m_context(
    ledger: StrategyBotLedger,
    row: Mapping[str, Any],
) -> Mapping[str, Any]:
    if str(row.get("interval") or "").upper() != "10M":
        return row
    return _with_book_stats_context(ledger, row, bot_name=BOT_FAV_10M, prefix="fav_10m")


def _with_duplicate_window_guard(
    ledger: StrategyBotLedger,
    decision: BotDecision,
    row: Mapping[str, Any],
) -> BotDecision:
    if (
        decision.bot_name != BOT_HYPE_YES
        or decision.decision_status != ACCEPTED
        or allow_duplicate_hype_windows()
    ):
        return decision
    try:
        window_key = row.get("window_key")
        if window_key is None:
            return decision
        duplicate = ledger.has_accepted_window(
            bot_name=BOT_HYPE_YES,
            strategy_version=decision.strategy_version,
            asset="HYPE",
            side=source_side(row) or "YES",
            window_key=int(window_key),
            ticker=str(row.get("ticker") or ""),
        )
        if not duplicate:
            return decision
        return replace(
            decision,
            decision_status=REJECTED,
            reason_codes=tuple(decision.reason_codes) + ("DUPLICATE_HYPE_WINDOW_EXPOSURE",),
        )
    except Exception:  # noqa: BLE001 - duplicate guard must never block tracking
        logger.debug("v3 duplicate-window guard failed open", exc_info=True)
        return decision


def _with_empirical_delivery_guard(decision: BotDecision, row: Mapping[str, Any]) -> BotDecision:
    """Downgrade measured weak delivery slices to research while keeping full tracking."""
    if (
        not empirical_delivery_guard_enabled()
        or decision.decision_status != ACCEPTED
        or decision.bot_name in {BOT_BASELINE, BOT_THIRTEEN_M_SNIPER, BOT_FAV_10M, BOT_WARN_FLIP}
    ):
        return decision
    side = source_side(row)
    interval = str(row.get("interval") or "").upper()
    reasons: list[str] = []
    if side == "YES":
        reasons.append("V3_EMPIRICAL_GUARD_YES_RESEARCH_ONLY")
    if decision.bot_name != BOT_BNB_NO and interval in empirical_guard_late_intervals():
        reasons.append(f"V3_EMPIRICAL_GUARD_INTERVAL_{interval}_RESEARCH_ONLY")
    if not reasons:
        return decision
    return replace(
        decision,
        decision_status=RESEARCH_ONLY,
        reason_codes=tuple(decision.reason_codes) + tuple(reasons),
    )


def _with_feed_degraded_stamp(row: Mapping[str, Any]) -> dict[str, Any]:
    """Stamp outgoing Telegram text when a required data feed is stale."""
    out = dict(row)
    try:
        degraded = cycle_watchdog.degraded_feeds()
    except Exception:  # noqa: BLE001 - alert stamping must never block delivery
        logger.debug("v3 degraded-feed stamp skipped", exc_info=True)
        return out
    if not degraded:
        return out
    out["feed_degraded"] = True
    out["degraded_feeds"] = ",".join(degraded)
    existing = str(out.get("reason_codes") or "")
    codes = [code.strip() for code in existing.split(",") if code.strip()]
    for feed in degraded:
        suffix = "".join(ch if ch.isalnum() else "_" for ch in feed.upper()).strip("_")
        code = f"V3_DEGRADED_FEED_{suffix}"
        if code not in codes:
            codes.append(code)
    out["reason_codes"] = ",".join(codes)
    return out


def _maybe_send_auto_mute_notice(
    ledger: StrategyBotLedger,
    row: Mapping[str, Any],
    *,
    bot_name: str,
    notify_enabled: bool,
    header: str | None = None,
) -> None:
    if not notify_enabled:
        return
    key = f"{STRATEGY_VERSION}:{bot_name}:auto_mute_notice"
    try:
        if not ledger.claim_meta_once(key):
            return
        get_telegram().send(
            build_v3_auto_mute_alert(_with_feed_degraded_stamp(row), header=header)
        )
    except Exception:  # noqa: BLE001 - notice must never block tracking
        logger.warning("v3 %s auto-mute notice failed (ignored)", bot_name, exc_info=True)


def _maybe_send_thirteen_m_auto_mute_notice(
    ledger: StrategyBotLedger,
    row: Mapping[str, Any],
) -> None:
    _maybe_send_auto_mute_notice(
        ledger,
        row,
        bot_name=BOT_THIRTEEN_M_SNIPER,
        notify_enabled=thirteen_m_sniper_notify_enabled(),
    )


def record_source_row(
    row: Mapping[str, Any],
    *,
    source_system: str,
    btc_context: Mapping[str, Any] | None = None,
) -> int:
    """Record v3 bot decisions for one existing source row.

    Returns the number of bot rows inserted. All failures are swallowed by design:
    v3 must never break existing V2/HVF alert paths.
    """
    try:
        ledger = get_ledger()
        if ledger is None:
            return 0
        enriched_row = _with_fav_10m_context(
            ledger,
            _with_thirteen_m_sniper_context(
                ledger,
                _enrich_source_row(row, btc_context=btc_context),
            ),
        )
        count = 0
        for decision in decisions_for_row(enriched_row, source_system=source_system, btc_context=btc_context):
            stamped = _with_duplicate_window_guard(ledger, decision, enriched_row)
            stamped = _with_empirical_delivery_guard(stamped, enriched_row)
            row_id = ledger.record_decision(stamped, enriched_row, source_system=source_system)
            if row_id is not None:
                count += 1
                _maybe_notify(ledger, row_id, stamped)
        return count
    except Exception:  # noqa: BLE001 - non-critical side ledger
        logger.warning("v3 strategy-bot record failed (ignored)", exc_info=True)
        return 0


def record_exit_warning_row(row: Mapping[str, Any]) -> int | None:
    """Record + (optionally) alert one confirmed exit-warning flip (Book 1).

    Dedicated path: warn rows only run the warn_flip_entry bot, so the other
    books' populations stay clean. All failures are swallowed by design — this
    must never break the ultoim_v2 warning path that feeds it.
    """
    try:
        ledger = get_ledger()
        if ledger is None:
            return None
        enriched = _with_book_stats_context(
            ledger, row, bot_name=BOT_WARN_FLIP, prefix="warn_flip"
        )
        decision = warn_flip_entry_decision(enriched, source_system="ultoim_v2")
        if decision is None:
            return None
        row_id = ledger.record_decision(decision, enriched, source_system="ultoim_v2")
        if row_id is None:
            return None
        if decision.decision_status != ACCEPTED or not warn_flip_notify_enabled():
            return row_id
        if bool(decision.threshold_profile.get("auto_mute_active")):
            _maybe_send_auto_mute_notice(
                ledger,
                ledger.row_by_id(row_id) or dict(enriched),
                bot_name=BOT_WARN_FLIP,
                notify_enabled=warn_flip_notify_enabled(),
                header="V3 WARN-FLIP ENTRY AUTO-MUTED",
            )
            ledger.mark_notification(
                row_id, status="AUTO_MUTED", message_id=None,
                error="auto_mute_wilson_lb_lt_min",
            )
            return row_id
        recorded = ledger.row_by_id(row_id)
        if recorded is None:
            return row_id
        result = get_telegram().send(build_v3_alert(_with_feed_degraded_stamp(recorded)))
        if result.get("delivered"):
            status, mid = "SENT", result.get("message_id")
        elif result.get("muted"):
            status, mid = "MUTED", None
        else:
            status, mid = "DELIVERY_FAILED", None
        ledger.mark_notification(row_id, status=status, message_id=mid, error=result.get("error"))
        return row_id
    except Exception:  # noqa: BLE001 - non-critical side ledger
        logger.warning("v3 warn-flip record failed (ignored)", exc_info=True)
        return None


def record_top_pick_row(row: Mapping[str, Any]) -> int | None:
    """Record + (optionally) alert the window's single top pick at 13M.

    Display-only book: one row per 15m window (durable claim survives restarts),
    ACCEPTED always, never a trade signal. Failures are swallowed by design.
    """
    try:
        ledger = get_ledger()
        if ledger is None:
            return None
        wk = row.get("window_key")
        if wk is None or not ledger.claim_meta_once(
            f"{STRATEGY_VERSION}:{BOT_TOP_PICK_13M}:{int(wk)}"
        ):
            return None
        enriched = _with_book_stats_context(
            ledger, row, bot_name=BOT_TOP_PICK_13M, prefix="top_pick"
        )
        decision = top_pick_13m_decision(enriched)
        if decision is None:
            return None
        row_id = ledger.record_decision(decision, enriched, source_system="ultoim_v2")
        if row_id is None or not top_pick_notify_enabled():
            return row_id
        recorded = ledger.row_by_id(row_id)
        if recorded is None:
            return row_id
        result = get_telegram().send(build_v3_alert(_with_feed_degraded_stamp(recorded)))
        if result.get("delivered"):
            status, mid = "SENT", result.get("message_id")
        elif result.get("muted"):
            status, mid = "MUTED", None
        else:
            status, mid = "DELIVERY_FAILED", None
        ledger.mark_notification(row_id, status=status, message_id=mid, error=result.get("error"))
        return row_id
    except Exception:  # noqa: BLE001 - non-critical side ledger
        logger.warning("v3 top-pick record failed (ignored)", exc_info=True)
        return None


def record_rti_path_13m_row(row: Mapping[str, Any]) -> int | None:
    """Persist one isolated asset RTI cohort and notify accepted paper rows."""
    if not rti_path_13m_enabled():
        return None
    try:
        ledger = get_ledger()
        if ledger is None:
            return None
        wk = row.get("window_key")
        ticker = str(row.get("ticker") or "")
        asset = str(row.get("asset") or "").upper()
        rule_version = rti_path_13m_rule_version(asset)
        if (
            wk is None
            or not ticker
            or asset not in rti_path_13m_assets()
            or rule_version is None
        ):
            return None
        enriched = _with_book_stats_context(
            ledger,
            row,
            bot_name=BOT_RTI_PATH_13M,
            prefix="rti_path_13m",
            threshold_rule_version=rule_version,
        )
        enriched = dict(enriched)
        try:
            enriched["rti_probability_shadow_v2"] = (
                rti_probability_prediction(enriched)
            )
        except Exception as exc:  # noqa: BLE001 - model shadow fails closed
            enriched["rti_probability_shadow_v2"] = {
                "available": False,
                "prospective": False,
                "error": f"{type(exc).__name__}: {exc}",
                "historical_credit_allowed": False,
            }
        try:
            v3_artifact = os.environ.get(
                "Q15_RTI_PROBABILITY_V3_ARTIFACT"
            ) or V3_ARTIFACT_PATH
            enriched["rti_probability_shadow_v3"] = (
                rti_probability_prediction(enriched, v3_artifact)
            )
        except Exception as exc:  # noqa: BLE001 - model shadow fails closed
            enriched["rti_probability_shadow_v3"] = {
                "available": False,
                "prospective": False,
                "error": f"{type(exc).__name__}: {exc}",
                "historical_credit_allowed": False,
            }
        if rti_microstructure_v11_paper_record_enabled():
            try:
                enriched["rti_microstructure_shadow_v11"] = (
                    rti_v11_prediction(enriched)
                )
            except Exception as exc:  # noqa: BLE001 - V11 shadow fails closed
                enriched["rti_microstructure_shadow_v11"] = {
                    "available": False,
                    "prospective": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "paper_only": True,
                    "notification_eligible": False,
                    "automatic_promotion": False,
                    "real_trading_allowed": False,
                    "historical_credit_allowed": False,
                }
        decision = rti_path_13m_decision(enriched)
        row_id, recorded = _stored_decision(
            ledger,
            decision,
            enriched,
            source_system="rti_path_13m",
        )
        if recorded is None:
            return row_id
        durable_id = int(recorded["id"])
        challengers = decision.threshold_profile.get("challengers")
        notification_challengers = tuple(
            str(challenger_id)
            for challenger_id, challenger in (
                challengers.items() if isinstance(challengers, Mapping) else ()
            )
            if isinstance(challenger, Mapping)
            and challenger.get("accepted") is True
            and challenger.get("notification_eligible") is True
        )
        if (
            not notification_challengers
            or not rti_path_13m_notify_enabled()
        ):
            return durable_id
        delivery_key = _rti_path_13m_delivery_key(
            int(wk), ticker, rule_version=rule_version,
        )
        if not _notification_needs_delivery(
            ledger,
            recorded,
            idempotency_key=delivery_key,
        ):
            return durable_id
        result = enqueue_v3_outbox_notification(
            build_v3_alert(_with_feed_degraded_stamp(recorded)),
            idempotency_key=delivery_key,
            expires_at=float(recorded.get("close_time")),
        )
        status, message_id, error = _delivery_fields(result)
        ledger.mark_notification(
            durable_id,
            status=status,
            message_id=message_id,
            error=error,
        )
        return durable_id
    except Exception:  # noqa: BLE001 - paper monitor cannot break live capture
        logger.warning("v3 RTI path 13M record failed (ignored)", exc_info=True)
        return None


def record_rti_delayed_confirmation_row(
    row: Mapping[str, Any],
) -> int | None:
    """Persist a fresh-quote delayed RTI challenger without notification."""
    if not rti_path_13m_enabled():
        return None
    try:
        ledger = get_ledger()
        if ledger is None:
            return None
        wk = row.get("window_key")
        ticker = str(row.get("ticker") or "")
        asset = str(row.get("asset") or "").upper()
        if (
            wk is None
            or not ticker
            or asset not in rti_path_13m_assets()
            or rti_path_13m_rule_version(asset) is None
        ):
            return None
        interval = str(row.get("interval") or "").upper()
        if interval == "12M30S":
            decision = rti_path_12m30_confirmation_decision(row)
        elif interval == "12M":
            decision = rti_path_12m_confirmation_decision(row)
        elif interval == "11M30S":
            decision = rti_path_11m30_stability_decision(row)
        else:
            return None
        row_id, recorded = _stored_decision(
            ledger,
            decision,
            row,
            source_system="rti_path_13m",
        )
        # This forward experiment intentionally has no delivery surface.  The
        # durable row is settled by the same authoritative ticker resolver as
        # its frozen 13M parent and appears only in research health/scoreboards.
        return row_id if recorded is None else int(recorded["id"])
    except Exception:  # noqa: BLE001 - research cannot break exact capture
        logger.warning(
            "v3 RTI delayed confirmation record failed (ignored)", exc_info=True
        )
        return None


def record_rti_path_12m30_confirmation_row(
    row: Mapping[str, Any],
) -> int | None:
    """Compatibility wrapper for the frozen +30s challenger."""
    return record_rti_delayed_confirmation_row(row)


def rti_delayed_confirmation_recovery_state(
    *,
    ticker: str,
    close_time: float,
) -> dict[str, Any] | None:
    """Rebuild delayed-scheduler lineage from its durable exact parent."""
    ledger = get_ledger()
    if ledger is None:
        return None
    rows = ledger.rti_delayed_recovery_rows(
        ticker=str(ticker), close_time=float(close_time)
    )
    parents = [
        row for row in rows
        if str(row.get("interval") or "").upper() == "13M"
    ]
    if len(parents) != 1:
        return None
    parent = parents[0]
    asset = str(parent.get("asset") or "").upper()
    expected_version = rti_path_13m_rule_version(asset)
    if (
        expected_version is None
        or str(parent.get("source_model_version") or "") != expected_version
    ):
        return None
    raw_profile = parent.get("threshold_json")
    try:
        profile = json.loads(str(raw_profile)) if raw_profile else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        profile = {}
    if not isinstance(profile, Mapping):
        profile = {}
    original_side = str(
        profile.get("rti_side")
        or parent.get("original_source_side")
        or parent.get("side")
        or ""
    ).upper()
    if original_side not in {"YES", "NO"}:
        return None
    completed_intervals = sorted({
        str(row.get("interval") or "").upper()
        for row in rows
        if str(row.get("interval") or "").upper() in {
            "12M30S", "12M", "11M30S"
        }
    })
    return {
        "parent_row_id": int(parent["id"]),
        "parent_strict_accepted": parent.get("decision_status") == ACCEPTED,
        "completed_intervals": completed_intervals,
        "original_source": {
            "model_version": str(parent.get("source_model_version") or ""),
            "rti_side": original_side,
            "rti_path_end_px": profile.get("rti_path_end_px"),
        },
    }


def record_drift_pick_row(row: Mapping[str, Any]) -> int | None:
    """Record one flow/spread decision and alert only confirmed Drift picks.

    Multi-pick book: dedup is per (window, ticker) — a window can carry several
    qualifying alts and each gets its own decision. The recorder itself stays
    the raw shadow/control. Rejected and inconclusive rows still settle here.
    """
    try:
        ledger = get_ledger()
        if ledger is None:
            return None
        wk = row.get("window_key")
        ticker = str(row.get("ticker") or "")
        if wk is None or not ticker:
            return None
        source = dict(row)
        source.setdefault("delivery_status", "PAPER_DRIFT_FLOW_SPREAD")
        enriched = enrich_spot_depth(source)
        enriched = _enrich_source_row(enriched)
        enriched = enrich_drift_evidence(enriched)
        enriched = _with_drift_lineage_and_grade(enriched)
        enriched = _with_book_stats_context(
            ledger,
            enriched,
            bot_name=BOT_DRIFT_FLOW_SPREAD,
            prefix="drift_flow_spread",
            threshold_rule_version=DRIFT_CORE_RULE_VERSION,
        )
        for shadow_bot, prefix in (
            (BOT_DRIFT_ASYMMETRIC_VOLUME, "asymmetric_volume"),
            (BOT_DRIFT_BALANCED_V95, "balanced_v95"),
            (BOT_DRIFT_ACCURACY_V91, "accuracy_v91"),
            (BOT_DRIFT_CONSENSUS_FALLBACK, "consensus_fallback"),
        ):
            enriched = _with_book_stats_context(
                ledger,
                enriched,
                bot_name=shadow_bot,
                prefix=prefix,
                threshold_rule_version="drift-evidence-policy-v1",
                decision_status=RESEARCH_ONLY,
            )
        decision = drift_flow_spread_13m_decision(enriched)
        if decision is None:
            return None
        row_id, recorded = _stored_decision(
            ledger, decision, enriched, source_system="drift_shadow",
        )
        # These cohorts deliberately reuse the exact same point-in-time
        # enrichment and can never notify. They measure more research volume
        # without changing accepted exposure or the returned core identity.
        for shadow_decision_fn in (
            drift_flow_spread_shadow_spread4_decision,
            drift_flow_spread_shadow_flow15_decision,
            drift_asymmetric_volume_shadow_decision,
            drift_balanced_v95_shadow_decision,
            drift_accuracy_v91_shadow_decision,
            drift_consensus_fallback_shadow_decision,
        ):
            try:
                shadow_decision = shadow_decision_fn(enriched)
                if shadow_decision is not None:
                    _stored_decision(
                        ledger,
                        shadow_decision,
                        enriched,
                        source_system="drift_shadow",
                    )
            except Exception:  # noqa: BLE001 - shadows cannot block core delivery
                logger.warning("v3 Drift counterfactual record failed (ignored)", exc_info=True)
        if (
            recorded is None
            or decision.decision_status != ACCEPTED
            or recorded.get("decision_status") != ACCEPTED
            or not drift_flow_spread_notify_enabled()
        ):
            return row_id
        delivery_key = _drift_delivery_key(
            BOT_DRIFT_FLOW_SPREAD, int(wk), ticker,
        )
        if not _notification_needs_delivery(
            ledger, recorded, idempotency_key=delivery_key,
        ):
            return row_id
        payload = build_v3_alert(_with_feed_degraded_stamp(recorded))
        result = _send_drift_notification(
            payload,
            idempotency_key=delivery_key,
            expires_at=recorded.get("close_time"),
        )
        status, mid, error = _delivery_fields(result)
        ledger.mark_notification(
            int(recorded["id"]), status=status, message_id=mid, error=error,
        )
        return row_id
    except Exception:  # noqa: BLE001 - non-critical side ledger
        logger.warning("v3 drift-pick record failed (ignored)", exc_info=True)
        return None


def record_precision13_row(row: Mapping[str, Any]) -> int | None:
    """Record and notify one independently deduplicated precision 13M signal."""
    try:
        ledger = get_ledger()
        if ledger is None:
            return None
        wk = row.get("window_key")
        ticker = str(row.get("ticker") or "")
        if wk is None or not ticker:
            return None
        decision = precision13_sized_decision(row)
        if decision is None:
            return None
        row_id, recorded = _stored_decision(
            ledger, decision, row, source_system="precision13_shadow",
        )
        if (
            recorded is None
            or decision.decision_status != ACCEPTED
            or not precision13_notify_enabled()
        ):
            return row_id
        delivery_key = (
            f"{STRATEGY_VERSION}:precision13:{BOT_PRECISION_13M}:"
            f"row:{int(wk)}:{ticker}"
        )
        if not _notification_needs_delivery(
            ledger, recorded, idempotency_key=delivery_key,
        ):
            return row_id
        result = get_telegram().send(
            build_v3_alert(_with_feed_degraded_stamp(recorded))
        )
        if result.get("delivered"):
            status, message_id = "SENT", result.get("message_id")
        elif result.get("muted"):
            status, message_id = "MUTED", None
        else:
            status, message_id = "DELIVERY_FAILED", None
        ledger.mark_notification(
            int(recorded["id"]),
            status=status,
            message_id=message_id,
            error=result.get("error"),
        )
        return row_id
    except Exception:  # noqa: BLE001 - precision delivery cannot break capture
        logger.warning("v3 precision13 notification failed (ignored)", exc_info=True)
        return None


def record_drift_checkpoint_row(row: Mapping[str, Any]) -> int | None:
    """Record and optionally notify one Drift add-on or late-qualifier row."""
    try:
        ledger = get_ledger()
        if ledger is None:
            return None
        kind = str(row.get("record_kind") or "")
        if kind == "DRIFT_ADDON_REQUAL":
            bot_name = BOT_DRIFT_ADDON
            decision_fn = drift_addon_requal_decision
            notify = drift_addon_notify_enabled()
        elif kind == "DRIFT_LATEQUAL":
            bot_name = BOT_DRIFT_LATEQUAL
            decision_fn = drift_latequal_decision
            notify = drift_latequal_notify_enabled()
        else:
            return None
        wk = row.get("window_key")
        ticker = str(row.get("ticker") or "")
        if wk is None or not ticker:
            return None
        enriched = _enrich_source_row(row)
        decision = decision_fn(enriched)
        if decision is None:
            return None
        row_id, recorded = _stored_decision(
            ledger, decision, enriched, source_system="drift_shadow",
        )
        if (
            recorded is None
            or decision.decision_status != ACCEPTED
            or recorded.get("decision_status") != ACCEPTED
            or not notify
        ):
            return row_id
        delivery_key = _drift_delivery_key(bot_name, int(wk), ticker)
        if not _notification_needs_delivery(
            ledger, recorded, idempotency_key=delivery_key,
        ):
            return row_id
        payload = build_v3_alert(_with_feed_degraded_stamp(recorded))
        result = _send_drift_notification(
            payload,
            idempotency_key=delivery_key,
            expires_at=recorded.get("close_time"),
        )
        status, mid, error = _delivery_fields(result)
        ledger.mark_notification(
            int(recorded["id"]), status=status, message_id=mid, error=error,
        )
        return row_id
    except Exception:  # noqa: BLE001 - checkpoint tracking must never break capture
        logger.warning("v3 drift checkpoint record failed (ignored)", exc_info=True)
        return None


def record_drift_no_mirror_window(rows: Sequence[Mapping[str, Any]]) -> list[int]:
    """Record filtered NO candidates and send one grouped research card.

    Every candidate remains an independent ledger row for settlement/PnL, but
    Telegram receives at most one compact card per 15-minute window.
    """
    try:
        ledger = get_ledger()
        if ledger is None:
            return []
        row_ids: list[int] = []
        recorded_rows: list[dict[str, Any]] = []
        seen_record_ids: set[int] = set()
        window_key: int | None = None
        for row in rows:
            wk = row.get("window_key")
            ticker = str(row.get("ticker") or "")
            if wk is None or not ticker:
                continue
            wk_int = int(wk)
            if window_key is None:
                window_key = wk_int
            if wk_int != window_key:
                logger.warning("drift NO group contained multiple windows; skipping %s", ticker)
                continue
            enriched = _enrich_source_row(row)
            decision = drift_no_mirror_decision(enriched)
            if decision is None:
                continue
            row_id, recorded = _stored_decision(
                ledger, decision, enriched, source_system="drift_shadow",
            )
            if row_id is not None:
                row_ids.append(row_id)
            if recorded is None:
                continue
            record_id = int(recorded["id"])
            if record_id not in seen_record_ids:
                seen_record_ids.add(record_id)
                recorded_rows.append(recorded)

        if (
            not recorded_rows
            or window_key is None
            or not drift_no_mirror_notify_enabled()
        ):
            return row_ids
        delivery_key = _drift_delivery_key(
            BOT_DRIFT_NO_MIRROR, window_key, grouped=True,
        )
        pending_rows = [
            row
            for row in recorded_rows
            if _notification_needs_delivery(
                ledger, row, idempotency_key=delivery_key,
            )
        ]
        if not pending_rows:
            return row_ids
        stamped = [_with_feed_degraded_stamp(row) for row in pending_rows]
        payload = build_drift_no_mirror_group_alert(stamped)
        result = _send_drift_notification(
            payload,
            idempotency_key=delivery_key,
            expires_at=_group_expiry(pending_rows),
        )
        status, mid, error = _delivery_fields(result)
        for recorded in pending_rows:
            ledger.mark_notification(
                int(recorded["id"]),
                status=status,
                message_id=mid,
                error=error,
            )
        return row_ids
    except Exception:  # noqa: BLE001 - research mirror must never break capture
        logger.warning("v3 drift NO mirror record failed (ignored)", exc_info=True)
        return []


def record_drift_no_expansion_window(rows: Sequence[Mapping[str, Any]]) -> list[int]:
    """Record NO-expansion rows as silent, settlement-scored research only."""
    try:
        ledger = get_ledger()
        if ledger is None:
            return []
        row_ids: list[int] = []
        window_key: int | None = None
        for row in rows:
            wk = row.get("window_key")
            ticker = str(row.get("ticker") or "")
            if wk is None or not ticker:
                continue
            wk_int = int(wk)
            if window_key is None:
                window_key = wk_int
            if wk_int != window_key:
                logger.warning("drift NO expansion mixed windows; skipping %s", ticker)
                continue
            source = dict(row)
            source.setdefault("delivery_status", "PAPER_DRIFT_NO_EXPANSION")
            enriched = enrich_spot_depth(source)
            enriched = _enrich_source_row(enriched)
            enriched = enrich_drift_evidence(enriched)
            enriched = _with_drift_lineage_and_grade(
                enriched,
                expected_side="NO",
                lineage_config=_drift_no_expansion_lineage_config(),
            )
            enriched = _with_book_stats_context(
                ledger,
                enriched,
                bot_name=BOT_DRIFT_NO_EXPANSION,
                prefix="drift_no_expansion",
                threshold_rule_version="drift-no-expansion-13m-shadow-v2",
                decision_status=RESEARCH_ONLY,
            )
            decision = drift_no_expansion_decision(enriched)
            if decision is None:
                continue
            row_id, _recorded = _stored_decision(
                ledger, decision, enriched, source_system="drift_shadow",
            )
            if row_id is not None:
                row_ids.append(row_id)
        return row_ids
    except Exception:  # noqa: BLE001 - research expansion must never break capture
        logger.warning("v3 drift NO expansion record failed (ignored)", exc_info=True)
        return []


def send_top_pick_gap_notice(*, window_key: int, close_time: float | None = None) -> bool:
    """One 'NO PICK — data gap' card when a window produced nothing scorable.

    Keeps the owner's hard one-card-per-window cadence visible instead of
    silently skipping. Durable once-per-window claim; failures swallowed.
    """
    try:
        ledger = get_ledger()
        if ledger is None or not top_pick_notify_enabled():
            return False
        if not ledger.claim_meta_once(f"{STRATEGY_VERSION}:{BOT_TOP_PICK_13M}:gap:{int(window_key)}"):
            return False
        parts = [
            "\U0001f3c6 <b>V3 BEST TRADE 13M — NO PICK</b>",
            "No scorable capture reached the 13M mark for this window (data gap).",
            "Cadence guard: this card exists so a silent skip is impossible.",
        ]
        get_telegram().send("\n".join(parts))
        return True
    except Exception:  # noqa: BLE001 - notice must never block anything
        logger.warning("v3 top-pick gap notice failed (ignored)", exc_info=True)
        return False


def owns_source_notification(
    row: Mapping[str, Any],
    *,
    source_system: str,
    btc_context: Mapping[str, Any] | None = None,
) -> bool:
    """Whether V3 should own the operator-facing notification for this source row."""
    try:
        if not enabled():
            return False
        enriched_row = _enrich_source_row(row, btc_context=btc_context)
        asset = str(row.get("asset") or "").upper()
        if _bool("Q15_V3_SUPPRESS_OLD_BNB_NOTIFICATIONS", False) and asset == "BNB":
            return True
        if not suppress_owned_source_notifications():
            return False
        if hvf_wrapper_only_notifications() and source_system != "high_vol_flip":
            return False
        for decision in decisions_for_row(enriched_row, source_system=source_system, btc_context=btc_context):
            decision = _with_empirical_delivery_guard(decision, enriched_row)
            if source_system == "high_vol_flip" and decision.bot_name == BOT_HVF_DEPTH_FLOW:
                return True
            if decision.bot_name == BOT_BASELINE:
                continue
            if decision.decision_status == ACCEPTED:
                return True
            if (
                decision.bot_name in {BOT_BNB_YES_REVERSAL, BOT_CONFIDENCE_TIER}
                and decision.decision_status == RESEARCH_ONLY
            ):
                return True
        return False
    except Exception:  # noqa: BLE001 - fail open: old alert is safer than silence
        logger.debug("v3 owned-notification check failed open", exc_info=True)
        return False


def _maybe_notify(ledger: StrategyBotLedger, row_id: int, decision: BotDecision) -> None:
    try:
        recorded = ledger.row_by_id(row_id)
    except Exception:  # noqa: BLE001 - notification must never block tracking
        logger.warning("v3 strategy-bot notification lookup failed (ignored)", exc_info=True)
        return
    if recorded is None:
        return
    if (
        str(recorded.get("source_system") or "") == "high_vol_flip"
        and decision.bot_name not in {BOT_HVF_DEPTH_FLOW, BOT_DEPTH_FORMULA_15M}
    ):
        return
    reversal_research = (
        decision.bot_name == BOT_BNB_YES_REVERSAL
        and decision.decision_status == RESEARCH_ONLY
    )
    tier_research = (
        decision.bot_name == BOT_CONFIDENCE_TIER
        and decision.decision_status == RESEARCH_ONLY
        and research_telegram_enabled()
    )
    depth_formula_research = (
        decision.bot_name == BOT_DEPTH_FORMULA_15M
        and decision.decision_status == RESEARCH_ONLY
        and depth_formula_telegram_enabled()
    )
    thirteen_m_sniper_alert = (
        decision.bot_name == BOT_THIRTEEN_M_SNIPER
        and decision.decision_status == ACCEPTED
    )
    if thirteen_m_sniper_alert and not thirteen_m_sniper_notify_enabled():
        return
    if thirteen_m_sniper_alert and bool(decision.threshold_profile.get("auto_mute_active")):
        _maybe_send_thirteen_m_auto_mute_notice(ledger, recorded)
        try:
            ledger.mark_notification(
                row_id,
                status="AUTO_MUTED",
                message_id=None,
                error="auto_mute_wilson_lb_lt_min",
            )
        except Exception:  # noqa: BLE001 - notification status is best-effort
            logger.debug("v3 13M sniper auto-mute mark failed", exc_info=True)
        return
    fav_10m_alert = (
        decision.bot_name == BOT_FAV_10M
        and decision.decision_status == ACCEPTED
    )
    if fav_10m_alert and not fav_10m_notify_enabled():
        return
    if fav_10m_alert and bool(decision.threshold_profile.get("auto_mute_active")):
        _maybe_send_auto_mute_notice(
            ledger,
            recorded,
            bot_name=BOT_FAV_10M,
            notify_enabled=fav_10m_notify_enabled(),
            header="V3 FAVORITE 10M AUTO-MUTED",
        )
        try:
            ledger.mark_notification(
                row_id,
                status="AUTO_MUTED",
                message_id=None,
                error="auto_mute_wilson_lb_lt_min",
            )
        except Exception:  # noqa: BLE001 - notification status is best-effort
            logger.debug("v3 fav_10m auto-mute mark failed", exc_info=True)
        return
    if (
        hvf_wrapper_only_notifications()
        and decision.bot_name not in {BOT_HVF_DEPTH_FLOW, BOT_THIRTEEN_M_SNIPER, BOT_FAV_10M}
        and not depth_formula_research
    ):
        return
    if (
        decision.bot_name == BOT_BASELINE
        or (
            decision.decision_status != ACCEPTED
            and not reversal_research
            and not tier_research
            and not depth_formula_research
        )
    ):
        return
    try:
        result = get_telegram().send(build_v3_alert(_with_feed_degraded_stamp(recorded)))
        if result.get("delivered"):
            status, mid = "SENT", result.get("message_id")
        elif result.get("muted"):
            status, mid = "MUTED", None
        else:
            status, mid = "DELIVERY_FAILED", None
        ledger.mark_notification(
            row_id,
            status=status,
            message_id=mid,
            error=result.get("error"),
        )
    except Exception:  # noqa: BLE001 - notification must never block tracking
        logger.warning("v3 strategy-bot notification failed (ignored)", exc_info=True)


def resolve(
    *,
    source_system: str,
    source_model_version: str,
    ticker: str,
    official_result: str,
    now: float | None = None,
) -> int:
    try:
        ledger = get_ledger()
        if ledger is None:
            return 0
        return ledger.resolve(
            source_system=source_system,
            source_model_version=source_model_version,
            ticker=ticker,
            official_result=official_result,
            now=now,
        )
    except Exception:  # noqa: BLE001 - non-critical side ledger
        logger.warning("v3 strategy-bot resolve failed (ignored)", exc_info=True)
        return 0


def resolve_ticker(
    *,
    ticker: str,
    official_result: str,
    now: float | None = None,
) -> int:
    """Grade all pending strategy rows for one officially settled contract."""
    try:
        ledger = get_ledger()
        if ledger is None:
            return 0
        return ledger.resolve_ticker(
            ticker=ticker,
            official_result=official_result,
            now=now,
        )
    except Exception:  # noqa: BLE001 - non-critical side ledger
        logger.warning(
            "v3 strategy-bot contract settlement failed (ignored)",
            exc_info=True,
        )
        return 0


def unresolved_rti_tickers(
    *,
    now: float | None = None,
    limit: int = 500,
) -> list[str]:
    """Read-only bounded backlog used by the authoritative settlement lane."""
    try:
        ledger = get_ledger()
        if ledger is None:
            return []
        return ledger.unresolved_rti_tickers(now=now, limit=limit)
    except Exception:  # noqa: BLE001 - health repair cannot block capture
        logger.warning(
            "v3 strategy-bot settlement backlog read failed (ignored)",
            exc_info=True,
        )
        return []


def reconcile_drift_settlements(events: Any) -> int:
    """Backfill or grade Drift strategy rows from the authoritative Drift ledger."""
    total = 0
    if not events:
        return total
    for event in events:
        if not isinstance(event, Mapping):
            continue
        model_version = str(event.get("model_version") or "")
        ticker = str(event.get("ticker") or "")
        result = str(event.get("official_result") or "").upper()
        if not model_version or not ticker or result not in {"YES", "NO"}:
            continue
        total += resolve(
            source_system="drift_shadow",
            source_model_version=model_version,
            ticker=ticker,
            official_result=result,
            now=event.get("resolved_at"),
        )
    return total


def scoreboard() -> dict[str, Any]:
    ledger = get_ledger()
    if ledger is None:
        return {"available": False, "strategy_version": STRATEGY_VERSION, "enabled": False}
    return ledger.scoreboard(STRATEGY_VERSION)


def _rti_probability_skill_gate(
    metrics: Mapping[str, Any] | None,
    *,
    min_predictions: int = 30,
    require_clustered_uncertainty: bool = False,
) -> dict[str, Any]:
    """Require proper-score improvement before a probability model can promote."""
    values = dict(metrics or {})
    predictions = int(values.get("n") or 0)
    market_n = int(values.get("market_n") or 0)
    brier = _drift_num(values.get("brier_score"))
    market_brier = _drift_num(values.get("market_brier_score"))
    log_loss = _drift_num(values.get("log_loss"))
    market_log_loss = _drift_num(values.get("market_log_loss"))
    paired_complete = predictions > 0 and market_n == predictions
    brier_improved = bool(
        brier is not None
        and market_brier is not None
        and brier < market_brier
    )
    log_loss_improved = bool(
        log_loss is not None
        and market_log_loss is not None
        and log_loss < market_log_loss
    )
    raw_bootstrap = values.get("paired_close_window_bootstrap")
    bootstrap = (
        dict(raw_bootstrap) if isinstance(raw_bootstrap, Mapping) else {}
    )
    brier_delta = (
        dict(bootstrap.get("brier_delta", {}))
        if isinstance(bootstrap.get("brier_delta"), Mapping)
        else {}
    )
    log_loss_delta = (
        dict(bootstrap.get("log_loss_delta", {}))
        if isinstance(bootstrap.get("log_loss_delta"), Mapping)
        else {}
    )
    brier_upper = _drift_num(brier_delta.get("one_sided_upper"))
    log_loss_upper = _drift_num(log_loss_delta.get("one_sided_upper"))
    bootstrap_rows = int(bootstrap.get("rows") or 0)
    bootstrap_close_windows = int(bootstrap.get("close_windows") or 0)
    bootstrap_checks = {
        "available": bootstrap.get("available") is True,
        "version_exact": (
            bootstrap.get("version") == RTI_V11_BOOTSTRAP_VERSION
        ),
        "cluster_key_exact": (
            bootstrap.get("cluster_key") == RTI_V11_BOOTSTRAP_CLUSTER_KEY
        ),
        "resamples_exact": (
            bootstrap.get("resamples") == RTI_V11_BOOTSTRAP_RESAMPLES
        ),
        "confidence_level_exact": (
            bootstrap.get("confidence_level")
            == RTI_V11_BOOTSTRAP_CONFIDENCE_LEVEL
        ),
        "random_seed_exact": (
            bootstrap.get("random_seed") == RTI_V11_BOOTSTRAP_RANDOM_SEED
        ),
        "same_close_assets_resampled_together": (
            bootstrap.get("same_close_assets_resampled_together") is True
        ),
        "within_close_assets_equal_weighted": (
            bootstrap.get("within_close_assets_equal_weighted") is True
        ),
        "close_windows_equal_weighted": (
            bootstrap.get("close_windows_equal_weighted") is True
        ),
        "loss_delta_direction_exact": (
            bootstrap.get("loss_delta_direction") == "MODEL_MINUS_MARKET"
        ),
        "minimum_brier_improvement_exact": (
            bootstrap.get("minimum_mean_brier_improvement")
            == RTI_V11_MIN_BRIER_IMPROVEMENT
        ),
        "minimum_log_loss_improvement_exact": (
            bootstrap.get("minimum_mean_log_loss_improvement")
            == RTI_V11_MIN_LOG_LOSS_IMPROVEMENT
        ),
        "all_predictions_paired": (
            predictions > 0 and bootstrap_rows == predictions
        ),
        "close_windows_present": bootstrap_close_windows > 0,
        "brier_one_sided_bound_clears_floor": bool(
            brier_upper is not None
            and brier_upper <= -RTI_V11_MIN_BRIER_IMPROVEMENT
        ),
        "log_loss_one_sided_bound_clears_floor": bool(
            log_loss_upper is not None
            and log_loss_upper <= -RTI_V11_MIN_LOG_LOSS_IMPROVEMENT
        ),
        "bootstrap_gate_met": bootstrap.get("gate_met") is True,
    }
    clustered_uncertainty_met = all(bootstrap_checks.values())
    return {
        "required": True,
        "min_predictions": int(min_predictions),
        "predictions": predictions,
        "market_paired_predictions": market_n,
        "paired_complete": paired_complete,
        "brier_score": brier,
        "market_brier_score": market_brier,
        "brier_skill_vs_market": values.get("brier_skill_vs_market"),
        "brier_improved": brier_improved,
        "log_loss": log_loss,
        "market_log_loss": market_log_loss,
        "log_loss_delta_vs_market": values.get(
            "log_loss_delta_vs_market"
        ),
        "log_loss_improved": log_loss_improved,
        "clustered_uncertainty_required": bool(
            require_clustered_uncertainty
        ),
        "clustered_uncertainty_met": clustered_uncertainty_met,
        "clustered_uncertainty_checks": bootstrap_checks,
        "paired_close_window_bootstrap": bootstrap,
        "met": bool(
            predictions >= int(min_predictions)
            and paired_complete
            and brier_improved
            and log_loss_improved
            and (
                clustered_uncertainty_met
                if require_clustered_uncertainty
                else True
            )
        ),
    }


def _rti_probability_lineage_gate(
    scorecard: Mapping[str, Any],
    *,
    cohort: str,
    challenger_id: str,
) -> dict[str, Any]:
    """Require one immutable prospective lineage inside each cohort."""
    by_cohort = scorecard.get("prospective_lineage_by_transfer_cohort", {})
    summary = (
        dict(by_cohort.get(cohort, {}))
        if isinstance(by_cohort, Mapping)
        and isinstance(by_cohort.get(cohort), Mapping)
        else {}
    )
    expected_probability_field = (
        "yes_probability"
        if challenger_id == RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID
        else "calibrated_yes_probability"
    )
    checks = {
        "point_in_time_stored_evidence_only": scorecard.get(
            "point_in_time_stored_evidence_only"
        ) is True,
        "historical_recomputation_forbidden": scorecard.get(
            "historical_recomputation_allowed"
        ) is False,
        "stored_probability_field_exact": scorecard.get(
            "stored_probability_field"
        ) == expected_probability_field,
        "prospective_rows_present": int(
            summary.get("prospective_evidence_rows") or 0
        ) > 0,
        "single_model_version": summary.get("single_model_version") is True,
        "single_artifact_sha256": summary.get(
            "single_artifact_sha256"
        ) is True,
        "artifact_sha256_valid": summary.get("artifact_sha256_valid") is True,
        "evidence_cohort_matches_row_cohort": summary.get(
            "evidence_cohort_matches_row_cohort"
        ) is True,
        "lineage_summary_met": summary.get("met") is True,
    }
    if challenger_id == RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID:
        checks.update({
            "single_test_state_sha256": summary.get(
                "single_test_state_sha256"
            ) is True,
            "single_test_metrics_sha256": summary.get(
                "single_test_metrics_sha256"
            ) is True,
            "test_state_sha256_valid": summary.get(
                "test_state_sha256_valid"
            ) is True,
            "test_metrics_sha256_valid": summary.get(
                "test_metrics_sha256_valid"
            ) is True,
            "exact_test_design_protocol_lineage": summary.get(
                "v11_exact_test_design_protocol_lineage"
            ) is True,
        })
    failures = [name for name, passed in checks.items() if not passed]
    return {
        "met": not failures,
        "cohort": cohort,
        "challenger_id": challenger_id,
        "checks": checks,
        "failures": failures,
        "prospective_evidence_rows": int(
            summary.get("prospective_evidence_rows") or 0
        ),
        "observed_model_versions": summary.get(
            "observed_model_versions", []
        ),
        "observed_artifact_sha256": summary.get(
            "observed_artifact_sha256", []
        ),
        "observed_test_state_sha256": summary.get(
            "observed_test_state_sha256", []
        ),
    }


def _v11_locked_artifact_health(cohort: str) -> dict[str, Any]:
    health = dict(rti_v11_artifact_health(cohort))
    record_enabled = rti_microstructure_v11_paper_record_enabled()
    if not record_enabled:
        ledger_status = "DISABLED_MANUAL_ACTIVATION_REQUIRED"
    elif health.get("available") is True:
        ledger_status = "ENABLED_PROSPECTIVE_PAPER_RECORD_ONLY"
    else:
        ledger_status = "ENABLED_WAITING_FOR_LOCKED_ARTIFACT"
    return {
        **health,
        "paper_record_enabled": record_enabled,
        "prospective_ledger_status": ledger_status,
        "prospective_ledger_notification_eligible": False,
        "prospective_ledger_real_trading_allowed": False,
        "prospective_ledger_automatic_promotion": False,
    }


def _v12_locked_artifact_health(cohort: str) -> dict[str, Any]:
    """Expose validation readiness without an activation or recording path."""
    health = dict(rti_v12_artifact_health(cohort))
    return {
        **health,
        "paper_record_enabled": False,
        "prospective_ledger_status": (
            "DISABLED_COLLECTION_AND_TEST_GATES_REQUIRED"
        ),
        "prospective_ledger_notification_eligible": False,
        "prospective_ledger_real_trading_allowed": False,
        "prospective_ledger_automatic_promotion": False,
        "artifact_installation_manual": True,
        "runtime_scoring_connected": False,
    }


def rti_path_13m_challenger_health() -> dict[str, Any]:
    """Cheap live status for the notifying exact-13M impulse challenger."""
    v2_model_health = rti_probability_artifact_health()
    v3_artifact_health = rti_probability_artifact_health(
        os.environ.get("Q15_RTI_PROBABILITY_V3_ARTIFACT")
        or V3_ARTIFACT_PATH
    )
    v3_model_health = {
        **v3_artifact_health,
        "artifact_numerically_eligible": bool(
            v3_artifact_health.get("promotion_eligible")
        ),
        "promotion_eligible": False,
        "status": (
            "INVALID_ARTIFACT"
            if not v3_artifact_health.get("available")
            else "ACTIVE_PAPER_RESEARCH_SKILL_NOT_PROVEN"
        ),
        "performance_skill_gate": _rti_probability_skill_gate(None),
        "performance_skill_gate_by_cohort": {},
    }
    probability_models = {
        "v2_quarantined_control": v2_model_health,
        "v3_challenger": v3_model_health,
    }
    v11_locked_artifacts = {
        cohort: _v11_locked_artifact_health(cohort)
        for cohort in ("BTC", "NON_BTC_TRANSFER")
    }
    v12_locked_artifacts = {
        cohort: _v12_locked_artifact_health(cohort)
        for cohort in ("BTC", "NON_BTC_TRANSFER")
    }
    empty_probability_scorecards = {
        challenger_id: {
            "challenger_id": challenger_id,
            "available": False,
            "paper_only": True,
            "point_in_time_stored_evidence_only": True,
            "accepted_trade_filter_applied": False,
            "promotion_prohibited": challenger_id
            == RTI_PATH_13M_PROBABILITY_V2_CHALLENGER_ID,
            "evaluated_evidence_rows": 0,
            "scoreable_resolved_rows": 0,
        }
        for challenger_id in (
            RTI_PATH_13M_PROBABILITY_V2_CHALLENGER_ID,
            RTI_PATH_13M_PROBABILITY_V3_CHALLENGER_ID,
            RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID,
        )
    }
    empty_exact_feature_coverage = _empty_rti_exact_feature_coverage()
    empty_v11_collection_readiness = _v11_collection_readiness_headline(
        empty_exact_feature_coverage
    )
    ledger = get_ledger()
    if ledger is None:
        return {
            "available": False,
            "paper_only": True,
            "id": RTI_PATH_13M_IMPULSE_CHALLENGER_ID,
            "policy_version": RTI_PATH_13M_IMPULSE_POLICY_VERSION,
            "notification_eligible": True,
            "automatic_promotion": False,
            "historical_credit_allowed": False,
            "strict_control_unchanged": True,
            "probability_model": v2_model_health,
            "probability_models": probability_models,
            "probability_scorecards": empty_probability_scorecards,
            "v11_locked_artifacts": v11_locked_artifacts,
            "v12_locked_artifacts": v12_locked_artifacts,
            "v11_collection_readiness": empty_v11_collection_readiness,
            "v12_collection_readiness": _v12_collection_readiness_headline(
                empty_exact_feature_coverage
            ),
            "v13_collection_readiness": _v13_collection_readiness_headline(
                empty_exact_feature_coverage
            ),
            "exact_feature_coverage": empty_exact_feature_coverage,
        }
    return _rti_path_13m_challenger_health_with_ledger(
        ledger=ledger,
        v2_model_health=v2_model_health,
        v3_model_health=v3_model_health,
        probability_models=probability_models,
        v11_locked_artifacts=v11_locked_artifacts,
        v12_locked_artifacts=v12_locked_artifacts,
        empty_v11_collection_readiness=empty_v11_collection_readiness,
        empty_probability_scorecards=empty_probability_scorecards,
        empty_exact_feature_coverage=empty_exact_feature_coverage,
    )


def _reset_rti_health_snapshot_for_tests() -> None:
    global _rti_health_snapshot
    global _rti_health_snapshot_built_monotonic
    global _rti_health_snapshot_built_epoch
    global _rti_health_snapshot_refreshing
    global _rti_health_snapshot_error
    with _rti_health_snapshot_lock:
        _rti_health_snapshot = None
        _rti_health_snapshot_built_monotonic = 0.0
        _rti_health_snapshot_built_epoch = 0.0
        _rti_health_snapshot_refreshing = False
        _rti_health_snapshot_error = None
        _rti_health_snapshot_event.clear()


_RTI_HEALTH_SNAPSHOT_TTL_SECONDS = max(
    5.0,
    float(os.environ.get("Q15_RTI_HEALTH_SNAPSHOT_TTL_SECONDS", "60")),
)
_rti_health_snapshot_lock = threading.Lock()
_rti_health_snapshot: dict[str, Any] | None = None
_rti_health_snapshot_built_monotonic = 0.0
_rti_health_snapshot_built_epoch = 0.0
_rti_health_snapshot_refreshing = False
_rti_health_snapshot_error: str | None = None
_rti_health_snapshot_event = threading.Event()


def _rti_health_warming_snapshot() -> dict[str, Any]:
    """Return a complete fail-closed shape while the first snapshot builds."""
    v2 = rti_probability_artifact_health()
    v3_artifact = rti_probability_artifact_health(
        os.environ.get("Q15_RTI_PROBABILITY_V3_ARTIFACT")
        or V3_ARTIFACT_PATH
    )
    v3 = {
        **v3_artifact,
        "artifact_numerically_eligible": bool(
            v3_artifact.get("promotion_eligible")
        ),
        "promotion_eligible": False,
        "status": "SCOREBOARD_SNAPSHOT_WARMING",
        "performance_skill_gate": _rti_probability_skill_gate({}),
        "performance_skill_gate_by_cohort": {},
    }
    probability_scorecards = {
        challenger_id: {
            "challenger_id": challenger_id,
            "available": False,
            "paper_only": True,
            "point_in_time_stored_evidence_only": True,
            "accepted_trade_filter_applied": False,
            "promotion_prohibited": challenger_id
            == RTI_PATH_13M_PROBABILITY_V2_CHALLENGER_ID,
            "evaluated_evidence_rows": 0,
            "scoreable_resolved_rows": 0,
        }
        for challenger_id in (
            RTI_PATH_13M_PROBABILITY_V2_CHALLENGER_ID,
            RTI_PATH_13M_PROBABILITY_V3_CHALLENGER_ID,
            RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID,
        )
    }
    v11_locked_artifacts = {
        cohort: _v11_locked_artifact_health(cohort)
        for cohort in ("BTC", "NON_BTC_TRANSFER")
    }
    v12_locked_artifacts = {
        cohort: _v12_locked_artifact_health(cohort)
        for cohort in ("BTC", "NON_BTC_TRANSFER")
    }
    empty_exact_feature_coverage = _empty_rti_exact_feature_coverage()
    return {
        "available": False,
        "paper_only": True,
        "id": RTI_PATH_13M_IMPULSE_CHALLENGER_ID,
        "policy_version": RTI_PATH_13M_IMPULSE_POLICY_VERSION,
        "status": "SCOREBOARD_SNAPSHOT_WARMING",
        "notification_eligible": True,
        "automatic_promotion": False,
        "historical_credit_allowed": False,
        "strict_control_unchanged": True,
        "probability_model": v2,
        "probability_models": {
            "v2_quarantined_control": v2,
            "v3_challenger": v3,
        },
        "probability_scorecards": probability_scorecards,
        "v11_locked_artifacts": v11_locked_artifacts,
        "v12_locked_artifacts": v12_locked_artifacts,
        "v11_collection_readiness": _v11_collection_readiness_headline(
            empty_exact_feature_coverage
        ),
        "v12_collection_readiness": _v12_collection_readiness_headline(
            empty_exact_feature_coverage
        ),
        "v13_collection_readiness": _v13_collection_readiness_headline(
            empty_exact_feature_coverage
        ),
        "exact_feature_coverage": empty_exact_feature_coverage,
    }


def _refresh_rti_health_snapshot() -> None:
    global _rti_health_snapshot
    global _rti_health_snapshot_built_monotonic
    global _rti_health_snapshot_built_epoch
    global _rti_health_snapshot_refreshing
    global _rti_health_snapshot_error
    try:
        snapshot = rti_path_13m_challenger_health()
    except Exception as exc:  # noqa: BLE001 - health must retain last good truth
        with _rti_health_snapshot_lock:
            _rti_health_snapshot_error = f"{type(exc).__name__}: {exc}"
            _rti_health_snapshot_refreshing = False
            _rti_health_snapshot_event.set()
        return
    with _rti_health_snapshot_lock:
        _rti_health_snapshot = copy.deepcopy(snapshot)
        _rti_health_snapshot_built_monotonic = time.monotonic()
        _rti_health_snapshot_built_epoch = time.time()
        _rti_health_snapshot_error = None
        _rti_health_snapshot_refreshing = False
        _rti_health_snapshot_event.set()


def _decorate_rti_health_snapshot(
    snapshot: Mapping[str, Any],
    *,
    now_monotonic: float,
) -> dict[str, Any]:
    with _rti_health_snapshot_lock:
        age = (
            None
            if _rti_health_snapshot_built_monotonic <= 0.0
            else max(
                0.0,
                now_monotonic - _rti_health_snapshot_built_monotonic,
            )
        )
        built_at = _rti_health_snapshot_built_epoch or None
        refreshing = _rti_health_snapshot_refreshing
        error = _rti_health_snapshot_error
    out = copy.deepcopy(dict(snapshot))
    out["health_snapshot"] = {
        "generated_at": built_at,
        "age_seconds": age,
        "ttl_seconds": _RTI_HEALTH_SNAPSHOT_TTL_SECONDS,
        "stale": age is None or age >= _RTI_HEALTH_SNAPSHOT_TTL_SECONDS,
        "refreshing": refreshing,
        "last_refresh_error": error,
        "stale_while_revalidate": True,
    }
    return out


def rti_path_13m_challenger_health_cached() -> dict[str, Any]:
    """Bounded-latency health snapshot with background revalidation.

    The full RTI audit intentionally reconstructs thousands of point-in-time
    records.  It must never hold the operator's liveness endpoint hostage.  A
    stale, timestamped snapshot is therefore returned immediately while one
    daemon refreshes it; predictive decisions and collectors never read this
    cache.
    """
    global _rti_health_snapshot_refreshing
    now_monotonic = time.monotonic()
    start_refresh = False
    with _rti_health_snapshot_lock:
        snapshot = (
            None
            if _rti_health_snapshot is None
            else copy.deepcopy(_rti_health_snapshot)
        )
        age = (
            math.inf
            if _rti_health_snapshot_built_monotonic <= 0.0
            else now_monotonic - _rti_health_snapshot_built_monotonic
        )
        if age >= _RTI_HEALTH_SNAPSHOT_TTL_SECONDS and not (
            _rti_health_snapshot_refreshing
        ):
            _rti_health_snapshot_refreshing = True
            _rti_health_snapshot_event.clear()
            start_refresh = True
    if start_refresh:
        threading.Thread(
            target=_refresh_rti_health_snapshot,
            name="q15-rti-health-snapshot",
            daemon=True,
        ).start()
    if snapshot is None:
        # Give a tiny in-memory/test ledger a chance to produce the full shape,
        # but keep production health bounded even when the historical DB is big.
        _rti_health_snapshot_event.wait(timeout=0.25)
        with _rti_health_snapshot_lock:
            snapshot = (
                None
                if _rti_health_snapshot is None
                else copy.deepcopy(_rti_health_snapshot)
            )
    if snapshot is None:
        snapshot = _rti_health_warming_snapshot()
    return _decorate_rti_health_snapshot(
        snapshot,
        now_monotonic=time.monotonic(),
    )


def _rti_path_13m_challenger_health_with_ledger(
    *,
    ledger: StrategyBotLedger,
    v2_model_health: Mapping[str, Any],
    v3_model_health: Mapping[str, Any],
    probability_models: dict[str, Any],
    v11_locked_artifacts: Mapping[str, Any],
    empty_v11_collection_readiness: Mapping[str, Any],
    empty_probability_scorecards: Mapping[str, Any],
    empty_exact_feature_coverage: Mapping[str, Any],
    v12_locked_artifacts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        system = ledger.rti_path_challenger_scoreboard(STRATEGY_VERSION, min_n=30)
        probability_scorecards = dict(
            system.get("probability_scorecards", {})
        )
        v3_scorecard = dict(
            probability_scorecards.get(
                RTI_PATH_13M_PROBABILITY_V3_CHALLENGER_ID, {}
            )
        )
        v3_skill_overall = _rti_probability_skill_gate(
            dict(v3_scorecard.get("overall", {}))
        )
        v3_skill_by_cohort = {
            str(cohort): _rti_probability_skill_gate(dict(metrics))
            for cohort, metrics in dict(
                v3_scorecard.get("by_transfer_cohort", {})
            ).items()
            if isinstance(metrics, Mapping)
        }
        any_v3_skill = any(
            bool(gate.get("met"))
            for gate in v3_skill_by_cohort.values()
        )
        probability_models["v3_challenger"] = {
            **v3_model_health,
            "promotion_eligible": bool(
                v3_model_health.get("artifact_numerically_eligible")
                and any_v3_skill
            ),
            "status": (
                "INVALID_ARTIFACT"
                if not v3_model_health.get("available")
                else "PAPER_SKILL_GATE_PASSED_MANUAL_REVIEW_REQUIRED"
                if any_v3_skill
                else "ACTIVE_PAPER_RESEARCH_SKILL_NOT_PROVEN"
            ),
            "performance_skill_gate": v3_skill_overall,
            "performance_skill_gate_by_cohort": v3_skill_by_cohort,
        }
        book = dict(
            system.get("books", {}).get(RTI_PATH_13M_IMPULSE_CHALLENGER_ID, {})
        )
        overall = dict(book.get("overall", {}))
        resolved = int(overall.get("resolved") or 0)
        net_pnl = overall.get("fee_adjusted_net_pnl_cents")
        wilson_low = overall.get("wilson_95_low")
        fee_breakeven = overall.get("avg_fee_adjusted_breakeven_rate")
        by_transfer_cohort = dict(book.get("by_transfer_cohort", {}))

        def _criteria(metrics: Mapping[str, Any]) -> bool:
            cohort_resolved = int(metrics.get("resolved") or 0)
            cohort_pnl = metrics.get("fee_adjusted_net_pnl_cents")
            cohort_wilson = metrics.get("wilson_95_low")
            cohort_breakeven = metrics.get(
                "avg_fee_slippage_adjusted_breakeven_rate"
            )
            return bool(
                cohort_resolved >= 30
                and metrics.get("cost_evidence_complete") is True
                and cohort_pnl is not None
                and float(cohort_pnl) > 0.0
                and cohort_wilson is not None
                and cohort_breakeven is not None
                and float(cohort_wilson) > float(cohort_breakeven)
            )

        criteria_by_cohort = {
            cohort: _criteria(dict(metrics))
            for cohort, metrics in by_transfer_cohort.items()
            if isinstance(metrics, Mapping)
        }
        promotion_criteria_met = any(criteria_by_cohort.values())
        exact_feature_coverage = system.get(
            "exact_feature_coverage", empty_exact_feature_coverage
        )
        if not isinstance(exact_feature_coverage, Mapping):
            exact_feature_coverage = empty_exact_feature_coverage
        v11_collection_readiness = _v11_collection_readiness_headline(
            exact_feature_coverage
        )
        v12_collection_readiness = _v12_collection_readiness_headline(
            exact_feature_coverage
        )
        v13_collection_readiness = _v13_collection_readiness_headline(
            exact_feature_coverage
        )
        evaluated = int(book.get("evaluated") or 0)
        qualified = int(book.get("qualified") or 0)
        challenger_status = (
            "ZERO_VOLUME_REVIEW_REQUIRED"
            if evaluated >= 30 and qualified == 0
            else "ACTIVE"
            if qualified > 0
            else "ACCRUING"
        )
        review_bars = (30, 60, 150)

        def _compact_metrics(metrics: Mapping[str, Any]) -> dict[str, Any]:
            resolved_count = int(metrics.get("resolved") or 0)
            pnl_cents = metrics.get("fee_adjusted_net_pnl_cents")
            drawdown_cents = metrics.get("max_cumulative_drawdown_cents")
            return {
                "resolved": resolved_count,
                "pnl_scoreable_resolved": int(
                    metrics.get("pnl_scoreable_resolved") or 0
                ),
                "unscoreable_resolved": int(
                    metrics.get("unscoreable_resolved") or 0
                ),
                "cost_evidence_complete": bool(
                    metrics.get("cost_evidence_complete")
                ),
                "label_integrity_failures": int(
                    metrics.get("label_integrity_failures") or 0
                ),
                "correct": int(metrics.get("correct") or 0),
                "accuracy": metrics.get("accuracy"),
                "wilson_95_low": metrics.get("wilson_95_low"),
                "wilson_95_high": metrics.get("wilson_95_high"),
                "avg_fee_adjusted_breakeven_rate": metrics.get(
                    "avg_fee_adjusted_breakeven_rate"
                ),
                "avg_fee_slippage_adjusted_breakeven_rate": metrics.get(
                    "avg_fee_slippage_adjusted_breakeven_rate"
                ),
                "ten_contract_net_pnl_dollars": (
                    None
                    if pnl_cents is None
                    else float(pnl_cents) * 10.0 / 100.0
                ),
                "max_cumulative_drawdown_cents": drawdown_cents,
                "max_cumulative_drawdown_cents_per_contract": drawdown_cents,
                "ten_contract_max_drawdown_dollars": (
                    None
                    if drawdown_cents is None
                    else float(drawdown_cents) * 10.0 / 100.0
                ),
                "fee_schedule_version": metrics.get("fee_schedule_version"),
                "execution_cost_model_version": metrics.get(
                    "execution_cost_model_version"
                ),
                "provisional": metrics.get("provisional", True),
            }

        research_books: dict[str, Any] = {}
        for book_id, raw_details in dict(system.get("books", {})).items():
            if not isinstance(raw_details, Mapping):
                continue
            details = dict(raw_details)
            promotion_prohibited = (
                str(book_id) == RTI_PATH_13M_PROBABILITY_V2_CHALLENGER_ID
            )
            probability_book = str(book_id) in {
                RTI_PATH_13M_PROBABILITY_V2_CHALLENGER_ID,
                RTI_PATH_13M_PROBABILITY_V3_CHALLENGER_ID,
                RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID,
            }
            scorecard = dict(
                probability_scorecards.get(str(book_id), {})
            )
            scorecard_cohorts = dict(
                scorecard.get("by_transfer_cohort", {})
            )
            lineage_gates_by_cohort: dict[str, Any] = {}
            compact_overall = _compact_metrics(
                dict(details.get("overall", {}))
            )
            resolved_count = int(compact_overall["resolved"] or 0)
            cohort_details: dict[str, Any] = {}
            for cohort, metrics in dict(
                details.get("by_transfer_cohort", {})
            ).items():
                if not isinstance(metrics, Mapping):
                    continue
                compact_cohort = _compact_metrics(dict(metrics))
                probability_skill_gate = _rti_probability_skill_gate(
                    dict(scorecard_cohorts.get(str(cohort), {})),
                    require_clustered_uncertainty=(
                        str(book_id)
                        == RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID
                    ),
                )
                lineage_gate = _rti_probability_lineage_gate(
                    scorecard,
                    cohort=str(cohort),
                    challenger_id=str(book_id),
                )
                if probability_book:
                    lineage_gates_by_cohort[str(cohort)] = lineage_gate
                if probability_book:
                    compact_cohort["probability_skill_gate"] = (
                        probability_skill_gate
                    )
                    compact_cohort["lineage_integrity_gate"] = lineage_gate
                compact_cohort["promotion_criteria_met"] = (
                    False
                    if promotion_prohibited
                    else _criteria(metrics)
                    and (
                        bool(probability_skill_gate["met"])
                        if probability_book
                        else True
                    )
                    and (
                        bool(lineage_gate["met"])
                        if probability_book
                        else True
                    )
                )
                cohort_details[str(cohort)] = compact_cohort
            next_review = next(
                (bar for bar in review_bars if resolved_count < bar), None
            )
            reached = [bar for bar in review_bars if resolved_count >= bar]
            rejected = _compact_metrics(
                dict(details.get("rejected_counterfactual", {}))
            )
            evaluated_lineage_gates = [
                gate for gate in lineage_gates_by_cohort.values()
                if int(gate.get("prospective_evidence_rows") or 0) > 0
            ]
            lineage_integrity_failed = bool(
                probability_book
                and evaluated_lineage_gates
                and any(not bool(gate.get("met")) for gate in evaluated_lineage_gates)
            )
            research_books[str(book_id)] = {
                "policy_version": details.get("policy_version"),
                "notification_eligible": details.get(
                    "notification_eligible", False
                ),
                "automatic_promotion": False,
                "promotion_prohibited": promotion_prohibited,
                "probability_skill_gate_required": probability_book,
                "lineage_integrity_gate_required": probability_book,
                "lineage_integrity_by_cohort": lineage_gates_by_cohort,
                "probability_skill_gate": (
                    _rti_probability_skill_gate(
                        dict(scorecard.get("overall", {})),
                        require_clustered_uncertainty=(
                            str(book_id)
                            == RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID
                        ),
                    )
                    if probability_book
                    else None
                ),
                "status": (
                    "QUARANTINED_NUMERICAL_OOD"
                    if promotion_prohibited
                    else "LINEAGE_INTEGRITY_FAILED_REVIEW_REQUIRED"
                    if lineage_integrity_failed
                    else "ACTIVE_PAPER_RESEARCH"
                ),
                "historical_credit_allowed": False,
                "evaluated": int(details.get("evaluated") or 0),
                "qualified": int(details.get("qualified") or 0),
                "rejected": int(details.get("rejected") or 0),
                "qualification_rate": details.get("qualification_rate"),
                "failure_counts": details.get("failure_counts", {}),
                **compact_overall,
                "by_transfer_cohort": cohort_details,
                "cohort_mixing_for_promotion_forbidden": True,
                "pooled_promotion_criteria_ignored": True,
                "manual_review_bars": list(review_bars),
                "highest_review_bar_reached": (
                    None if not reached else max(reached)
                ),
                "next_manual_review_bar": next_review,
                "resolved_until_next_review": (
                    None
                    if next_review is None
                    else max(0, next_review - resolved_count)
                ),
                "any_cohort_promotion_criteria_met": any(
                    bool(metrics.get("promotion_criteria_met"))
                    for metrics in cohort_details.values()
                ),
                "rejected_counterfactual": rejected,
            }
        return {
            "available": True,
            "paper_only": True,
            "id": RTI_PATH_13M_IMPULSE_CHALLENGER_ID,
            "policy_version": book.get("policy_version"),
            "notification_eligible": True,
            "automatic_promotion": False,
            "manual_review_bars": [30, 60, 150],
            "rows": overall.get("rows", 0),
            "status": challenger_status,
            "evaluated": evaluated,
            "qualified": qualified,
            "qualification_rate": book.get("qualification_rate"),
            "failure_counts": book.get("failure_counts", {}),
            "last_evaluated_close_time": book.get("last_evaluated_close_time"),
            "resolved": resolved,
            "correct": overall.get("correct", 0),
            "accuracy": overall.get("accuracy"),
            "wilson_95_low": wilson_low,
            "avg_fee_adjusted_breakeven_rate": fee_breakeven,
            "avg_fee_slippage_adjusted_breakeven_rate": overall.get(
                "avg_fee_slippage_adjusted_breakeven_rate"
            ),
            "fee_slippage_adjusted_net_pnl_cents": overall.get(
                "fee_adjusted_net_pnl_cents"
            ),
            "ten_contract_net_pnl_dollars": (
                None if net_pnl is None else float(net_pnl) * 10.0 / 100.0
            ),
            "max_cumulative_drawdown_cents": overall.get(
                "max_cumulative_drawdown_cents"
            ),
            "ten_contract_max_drawdown_dollars": (
                None
                if overall.get("max_cumulative_drawdown_cents") is None
                else float(overall["max_cumulative_drawdown_cents"])
                * 10.0
                / 100.0
            ),
            "pnl_scoreable_resolved": overall.get("pnl_scoreable_resolved"),
            "unscoreable_resolved": overall.get("unscoreable_resolved"),
            "cost_evidence_complete": overall.get("cost_evidence_complete"),
            "label_integrity_failures": overall.get(
                "label_integrity_failures"
            ),
            "fee_schedule_version": overall.get("fee_schedule_version"),
            "execution_cost_model_version": overall.get(
                "execution_cost_model_version"
            ),
            "provisional": overall.get("provisional", True),
            "promotion_criteria_met": promotion_criteria_met,
            "promotion_criteria_by_cohort": criteria_by_cohort,
            "by_transfer_cohort": by_transfer_cohort,
            "cohort_mixing_for_promotion_forbidden": True,
            "research_books": research_books,
            "probability_model": v2_model_health,
            "probability_models": probability_models,
            "probability_scorecards": system.get(
                "probability_scorecards", empty_probability_scorecards
            ),
            "v11_locked_artifacts": dict(v11_locked_artifacts),
            "v12_locked_artifacts": dict(v12_locked_artifacts or {}),
            "v11_collection_readiness": v11_collection_readiness,
            "v12_collection_readiness": v12_collection_readiness,
            "v13_collection_readiness": v13_collection_readiness,
            "exact_feature_coverage": exact_feature_coverage,
            "point_in_time_risk_diagnostics": system.get(
                "point_in_time_risk_diagnostics", {}
            ),
            "delayed_confirmation_matched": system.get(
                "delayed_confirmation_matched", {}
            ),
            "delayed_confirmation_60s_matched": system.get(
                "delayed_confirmation_60s_matched", {}
            ),
            "delayed_flip_60s_matched": system.get(
                "delayed_flip_60s_matched", {}
            ),
            "delayed_confirmation_ladder": system.get(
                "delayed_confirmation_ladder", {}
            ),
            "historical_credit_allowed": False,
            "strict_control_unchanged": True,
        }
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        logger.warning("RTI path challenger health failed", exc_info=True)
        return {
            "available": False,
            "paper_only": True,
            "id": RTI_PATH_13M_IMPULSE_CHALLENGER_ID,
            "policy_version": RTI_PATH_13M_IMPULSE_POLICY_VERSION,
            "notification_eligible": True,
            "automatic_promotion": False,
            "historical_credit_allowed": False,
            "strict_control_unchanged": True,
            "error": f"{type(exc).__name__}: {exc}",
            "probability_model": v2_model_health,
            "probability_models": probability_models,
            "probability_scorecards": empty_probability_scorecards,
            "v11_locked_artifacts": dict(v11_locked_artifacts),
            "v12_locked_artifacts": dict(v12_locked_artifacts or {}),
            "v11_collection_readiness": dict(
                empty_v11_collection_readiness
            ),
            "v12_collection_readiness": _v12_collection_readiness_headline(
                empty_exact_feature_coverage
            ),
            "v13_collection_readiness": _v13_collection_readiness_headline(
                empty_exact_feature_coverage
            ),
            "exact_feature_coverage": empty_exact_feature_coverage,
        }
