"""Outcome-blind coverage audit for the delayed RTI feature reservoir."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots import rti_delayed_feature_reservoir_identity as identity
from q15_upgrade.strategy_bots.rules import (
    KALSHI_FLOW_KEYS,
    RTI_DELAYED_SPOT_RESERVOIR_KEYS,
)
from tools.q15_rti_microstructure_preregister import design_fingerprint
from tools.q15_rti_v17_development_seal import DEFAULT_DB
from tools.q15_rti_v19_readiness import load_delayed_feature_rows_after


ASSETS = frozenset({"BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP"})
CORE_EXECUTION_KEYS = (
    "quote_age_seconds",
    "quote_age_source",
    "quote_evidence_source",
    "rti_confirm_original_row_id",
    "rti_confirm_original_side",
    "rti_confirm_side",
    "rti_confirm_target_at",
    "rti_confirm_delay_seconds",
    "rti_confirm_quote_captured_at",
    "rti_confirm_evaluated_at",
    "rti_confirm_path_status",
    "rti_confirm_path_missing_reason",
    "rti_confirm_path_complete",
    "rti_confirm_path_expected_count",
    "rti_confirm_path_count",
    "rti_confirm_path_max_receive_age_s",
    "rti_confirm_path_decision_age_s",
    "rti_confirm_original_end_px",
    "rti_confirm_end_px",
    "rti_confirm_continuation_bps",
    "rti_confirm_signed_distance_bps",
    "sim_contracts",
    "sim_full_fill_supported",
)
REQUIRED_PERSISTED_KEYS = tuple(dict.fromkeys((
    *CORE_EXECUTION_KEYS,
    *KALSHI_FLOW_KEYS,
    *RTI_DELAYED_SPOT_RESERVOIR_KEYS,
    "delayed_feature_reservoir_version",
    "delayed_feature_reservoir_record_only",
    "delayed_feature_reservoir_used_for_decision",
)))

KALSHI_COMPLETE_HORIZONS = (5, 15, 30, 60)
SPOT_COMPLETE_HORIZONS = (15, 60)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _feature_quality_failures(
    profile: Mapping[str, Any], *, expected_path_count: int = 61,
    expected_delay_seconds: int = 60,
) -> list[str]:
    """Fail closed when a persisted feature shell contains no usable evidence."""
    failures: list[str] = []
    target = profile.get("rti_confirm_target_at")
    quote_captured = profile.get("rti_confirm_quote_captured_at")
    evaluated = profile.get("rti_confirm_evaluated_at")
    quote_age = profile.get("quote_age_seconds")
    if (
        not all(_is_number(value) for value in (
            target, quote_captured, evaluated, quote_age,
        ))
        or not _is_number(profile.get("rti_confirm_delay_seconds"))
        or float(profile.get("rti_confirm_delay_seconds"))
        != float(expected_delay_seconds)
        or float(quote_captured) < float(target)
        or float(quote_captured) - float(target) > 2.0
        or float(evaluated) < float(quote_captured)
        or float(evaluated) - float(target) > 2.0
        or float(quote_age) < 0.0
        or float(quote_age) > 3.0
        or profile.get("quote_age_source") not in {
            "kalshi_rest_snapshot_received_at", "kalshi_ws_exact_sampler",
        }
        or profile.get("quote_evidence_source") not in {
            "kalshi_official_rest_orderbook",
            "kalshi_official_websocket_book",
        }
    ):
        failures.append("OFFICIAL_CONFIRMATION_QUOTE_OR_TIMING_INVALID")

    path_max_age = profile.get("rti_confirm_path_max_receive_age_s")
    path_decision_age = profile.get("rti_confirm_path_decision_age_s")
    if (
        profile.get("rti_confirm_path_status") != "ok"
        or profile.get("rti_confirm_path_missing_reason") not in {None, ""}
        or profile.get("rti_confirm_path_complete") is not True
        or not _is_number(profile.get("rti_confirm_path_expected_count"))
        or float(profile.get("rti_confirm_path_expected_count"))
        != float(expected_path_count)
        or not _is_number(profile.get("rti_confirm_path_count"))
        or float(profile.get("rti_confirm_path_count"))
        != float(expected_path_count)
        or not _is_number(path_max_age)
        or not _is_number(path_decision_age)
        or not 0.0 <= float(path_max_age) <= 3.0
        or not 0.0 <= float(path_decision_age) <= 3.0
    ):
        failures.append("FRESH_61_SECOND_RTI_CONFIRMATION_PATH_MISSING")

    original_side = str(profile.get("rti_confirm_original_side") or "").upper()
    confirmation_side = str(profile.get("rti_confirm_side") or "").upper()
    signed_distance = profile.get("rti_confirm_signed_distance_bps")
    if (
        original_side not in {"YES", "NO"}
        or confirmation_side not in {"YES", "NO"}
        or not all(_is_number(profile.get(key)) for key in (
            "rti_confirm_original_end_px", "rti_confirm_end_px",
            "rti_confirm_continuation_bps", "rti_confirm_signed_distance_bps",
        ))
    ):
        failures.append("RTI_CONFIRMATION_VALUES_MISSING")
    else:
        expected_side = (
            ("YES" if float(signed_distance) >= 0.0 else "NO")
            if original_side == "YES"
            else ("NO" if float(signed_distance) > 0.0 else "YES")
        )
        if confirmation_side != expected_side:
            failures.append("RTI_CONFIRMATION_SIDE_DISTANCE_CONTRADICTION")

    if not str(profile.get("kalshi_microstructure_extension_schema_version") or ""):
        failures.append("KALSHI_MICROSTRUCTURE_SOURCE_MISSING")
    if profile.get("kalshi_microstructure_evidence_source") != (
        "kalshi_official_websocket_history"
    ):
        failures.append("KALSHI_MICROSTRUCTURE_EVIDENCE_SOURCE_INVALID")
    transport_age = profile.get("kalshi_microstructure_transport_age_seconds")
    if (
        profile.get("kalshi_microstructure_transport_connected") is not True
        or not _is_number(transport_age)
        or float(transport_age) < 0.0
        or float(transport_age) > 3.0
    ):
        failures.append("KALSHI_MICROSTRUCTURE_TRANSPORT_STALE")
    if not str(profile.get("kalshi_microstructure_time_basis") or "") or not _is_number(
        profile.get("kalshi_microstructure_captured_at")
    ):
        failures.append("KALSHI_MICROSTRUCTURE_TIMESTAMP_MISSING")
    else:
        target = profile.get("rti_confirm_target_at")
        captured = profile.get("kalshi_microstructure_captured_at")
        evaluated = profile.get("rti_confirm_evaluated_at")
        if (
            not _is_number(target)
            or not _is_number(evaluated)
            or float(captured) < float(target) - 1.0
            or float(captured) > float(target) + 2.0
            or float(captured) > float(evaluated)
        ):
            failures.append("KALSHI_MICROSTRUCTURE_TIMESTAMP_MISALIGNED")
    if any(
        profile.get(f"kalshi_microstructure_window_complete_{horizon}s") is not True
        for horizon in KALSHI_COMPLETE_HORIZONS
    ):
        failures.append("KALSHI_MICROSTRUCTURE_WINDOW_INCOMPLETE")
    if any(
        not _is_number(profile.get(f"kalshi_book_delta_pressure_yes_{horizon}s"))
        or not _is_number(profile.get(f"kalshi_trade_imbalance_yes_{horizon}s"))
        for horizon in KALSHI_COMPLETE_HORIZONS
    ):
        failures.append("KALSHI_FLOW_VALUES_MISSING")

    if profile.get("spot_depth_status") != "ok" or not str(
        profile.get("spot_depth_source") or ""
    ):
        failures.append("SPOT_DEPTH_SOURCE_UNUSABLE")
    spot_evidence_as_of = profile.get("rti_spot_evidence_as_of")
    spot_snapshot_at = profile.get("rti_spot_snapshot_created_at")
    spot_received_at = profile.get("rti_spot_book_received_at")
    spot_book_age = profile.get("rti_spot_book_age_s")
    spot_source_at = profile.get("rti_spot_book_source_at")
    spot_source_age = profile.get("rti_spot_book_source_age_s")
    if not all(_is_number(value) for value in (
        spot_evidence_as_of,
        spot_snapshot_at,
        spot_received_at,
        spot_book_age,
        spot_source_at,
        spot_source_age,
    )):
        failures.append("SPOT_TIMESTAMP_LINEAGE_MISSING")
    elif (
        float(spot_received_at) > float(spot_snapshot_at)
        or float(spot_snapshot_at) > float(spot_evidence_as_of)
        or float(spot_book_age) < 0.0
        or float(spot_book_age) > 2.0
        or abs(float(spot_source_age)) > 5.0
        or abs(
            (float(spot_snapshot_at) - float(spot_received_at))
            - float(spot_book_age)
        ) > 0.1
    ):
        failures.append("SPOT_TIMESTAMP_LINEAGE_MISALIGNED")
    if (
        profile.get("spot_fast_mid_path_schema_version")
        != "spot-fast-mid-path-local-observed-v1"
        or profile.get("spot_fast_mid_path_time_basis")
        != "local_received_or_captured_at"
    ):
        failures.append("SPOT_FAST_MID_PATH_IDENTITY_INVALID")
    if any(
        profile.get(f"spot_fast_mid_window_complete_{horizon}s") is not True
        for horizon in SPOT_COMPLETE_HORIZONS
    ):
        failures.append("SPOT_FAST_MID_PATH_INCOMPLETE")
    if any(
        not _is_number(profile.get(key))
        for key in (
            "spot_depth_mid",
            "spot_depth_imbalance",
            "spot_depth_trade_net_notional_5s",
            "spot_depth_trade_net_notional_15s",
            "spot_depth_trade_net_notional_60s",
            "spot_fast_mid_change_bps_15s",
            "spot_fast_mid_change_bps_60s",
        )
    ):
        failures.append("SPOT_FEATURE_VALUES_MISSING")
    return failures


def _profile(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("threshold_json")
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        value = json.loads(str(raw or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def load_protocol() -> dict[str, Any]:
    path = ROOT / identity.PROTOCOL_RELATIVE_PATH
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("delayed_feature_reservoir_protocol_unreadable") from exc
    if not isinstance(value, Mapping):
        raise ValueError("delayed_feature_reservoir_protocol_root_not_object")
    protocol = dict(value)
    population = dict(protocol.get("population") or {})
    kalshi_features = dict(dict(protocol.get("feature_groups") or {}).get(
        "kalshi_book_and_trade_flow"
    ) or {})
    spot_features = dict(dict(protocol.get("feature_groups") or {}).get(
        "spot_depth_and_aggressive_flow"
    ) or {})
    execution_features = dict(dict(protocol.get("feature_groups") or {}).get(
        "execution_and_lineage"
    ) or {})
    usage = dict(protocol.get("usage") or {})
    future = dict(protocol.get("future_evaluation") or {})
    if (
        design_fingerprint(protocol) != identity.PROTOCOL_SHA256
        or protocol.get("protocol_id") != identity.PROTOCOL_ID
        or protocol.get("protocol_status")
        != "FROZEN_BEFORE_ANY_RESERVOIR_OUTCOME_ACCESS"
        or set(population.get("assets") or ()) != ASSETS
        or population.get("record_kind") != "RTI_PATH_12M_CONFIRM_PROSPECTIVE"
        or population.get("interval") != "12M"
        or float(population.get("prospective_after_close_time") or 0.0)
        != identity.PROSPECTIVE_AFTER_CLOSE_TIME
        or float(population.get("first_eligible_close_time") or 0.0)
        != identity.FIRST_ELIGIBLE_CLOSE_TIME
        or population.get("historical_backfill_allowed") is not False
        or kalshi_features.get("official_websocket_history_source_required")
        is not True
        or kalshi_features.get("websocket_transport_freshness_retained")
        is not True
        or kalshi_features.get(
            "rest_execution_quote_may_be_paired_with_separately_timestamped_websocket_history"
        ) is not True
        or spot_features.get("local_receive_time_is_freshness_authority") is not True
        or spot_features.get("exchange_timestamp_retained_for_provenance") is not True
        or spot_features.get(
            "exchange_source_freshness_required_independently_of_local_receipt"
        ) is not True
        or spot_features.get("queued_old_source_update_fails_closed") is not True
        or spot_features.get(
            "incremental_cached_top_of_book_required_for_batch_feed"
        ) is not True
        or spot_features.get("event_driven_local_observation_path_required") is not True
        or spot_features.get("one_second_local_observation_sampler_required") is not True
        or spot_features.get(
            "one_second_sampler_runs_in_dedicated_native_thread_required"
        ) is not True
        or spot_features.get("legacy_control_path_unchanged") is not True
        or execution_features.get(
            "delayed_persistence_isolated_from_exact_scheduler_required"
        ) is not True
        or execution_features.get(
            "delayed_sources_hash_bound_in_separate_wal_spool_required"
        ) is not True
        or execution_features.get(
            "strategy_ledger_writes_forbidden_before_95_seconds_after_13m"
        ) is not True
        or execution_features.get(
            "spool_restart_recovery_and_interval_dedup_required"
        ) is not True
        or execution_features.get(
            "full_health_graph_blocked_from_75s_before_through_100s_after_13m"
        ) is not True
        or execution_features.get(
            "local_restart_guard_covers_through_100s_after_13m"
        ) is not True
        or execution_features.get(
            "bounded_persistent_quote_worker_pool_required"
        ) is not True
        or execution_features.get(
            "thread_local_read_only_http_connection_reuse_required"
        ) is not True
        or usage.get("record_only") is not True
        or usage.get("used_by_v18") is not False
        or usage.get("used_by_v19") is not False
        or usage.get("changes_existing_candidate_eligibility") is not False
        or any(usage.get(key) is not False for key in (
            "outcome_access_allowed", "model_fit_allowed",
            "threshold_selection_allowed", "probability_scoring_allowed",
            "paper_pick_artifact_allowed", "notifications_allowed",
            "telegram_allowed", "automatic_promotion_allowed",
            "real_trading_allowed",
        ))
        or future.get("chronological_walk_forward_required") is not True
        or future.get("same_close_cross_asset_rows_may_not_cross_folds") is not True
        or future.get("untouched_test_required") is not True
        or future.get("historical_or_retrospective_feature_imputation_forbidden")
        is not True
        or protocol.get("outcome_labels_used_to_create_protocol") is not False
        or protocol.get("prospective_resolution_status_inspected_to_create_protocol")
        is not False
    ):
        raise ValueError("delayed_feature_reservoir_protocol_identity_or_safety_invalid")
    return protocol


def build_readiness(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    load_protocol()
    grouped: dict[float, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        try:
            close_time = float(row["close_time"])
        except (KeyError, TypeError, ValueError):
            continue
        if (
            close_time > identity.PROSPECTIVE_AFTER_CLOSE_TIME
            and str(row.get("interval") or "").upper() == "12M"
            and str(row.get("record_kind") or "").upper()
            == "RTI_PATH_12M_CONFIRM_PROSPECTIVE"
        ):
            grouped[close_time].append(row)
    geometry = {
        close: window for close, window in sorted(grouped.items())
        if len(window) == 7
        and {str(row.get("asset") or "").upper() for row in window} == ASSETS
    }
    missing_keys: Counter[str] = Counter()
    non_null: Counter[str] = Counter()
    rows_by_asset: Counter[str] = Counter()
    identity_failures: Counter[str] = Counter()
    feature_failures: Counter[str] = Counter()
    identity_complete_windows = 0
    feature_complete_windows = 0
    window_health: dict[float, dict[str, Any]] = {}
    credited_rows = []
    for close_time, window in geometry.items():
        window_valid = True
        feature_window_valid = True
        for row in window:
            asset = str(row.get("asset") or "").upper()
            rows_by_asset[asset] += 1
            profile = _profile(row)
            absent = [key for key in REQUIRED_PERSISTED_KEYS if key not in profile]
            missing_keys.update(absent)
            for key in REQUIRED_PERSISTED_KEYS:
                if profile.get(key) is not None:
                    non_null[key] += 1
            failures = []
            if profile.get("delayed_feature_reservoir_version") != identity.RESERVOIR_VERSION:
                failures.append("RESERVOIR_VERSION")
            if profile.get("delayed_feature_reservoir_record_only") is not True:
                failures.append("RECORD_ONLY_IDENTITY")
            if profile.get("delayed_feature_reservoir_used_for_decision") is not False:
                failures.append("NOT_USED_FOR_DECISION_IDENTITY")
            if absent:
                failures.append("PERSISTED_SCHEMA_INCOMPLETE")
            identity_failures.update(failures)
            if failures:
                window_valid = False
            quality_failures = _feature_quality_failures(profile)
            feature_failures.update(quality_failures)
            if quality_failures:
                feature_window_valid = False
            credited_rows.append({
                "id": int(row["id"]),
                "asset": asset,
                "close_time": close_time,
                "identity_failures": failures,
                "feature_quality_failures": quality_failures,
            })
        if window_valid:
            identity_complete_windows += 1
        if window_valid and feature_window_valid:
            feature_complete_windows += 1
        window_health[close_time] = {
            "identity_complete": window_valid,
            "feature_complete": window_valid and feature_window_valid,
            "feature_quality_failures": sorted({
                failure
                for row in credited_rows[-len(window):]
                for failure in row["feature_quality_failures"]
            }),
        }
    row_count = len(credited_rows)
    latest_close = max(geometry) if geometry else None
    latest_health = window_health.get(latest_close, {})
    consecutive_latest_usable = 0
    for close_time in sorted(window_health, reverse=True):
        if window_health[close_time]["feature_complete"] is not True:
            break
        consecutive_latest_usable += 1
    return {
        "readiness_version": "q15-rti-delayed-feature-reservoir-readiness-v1",
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "prospective_after_close_time": identity.PROSPECTIVE_AFTER_CLOSE_TIME,
        "first_eligible_close_time": identity.FIRST_ELIGIBLE_CLOSE_TIME,
        "all_seven_geometry_close_windows": len(geometry),
        "identity_and_schema_complete_close_windows": identity_complete_windows,
        "usable_feature_complete_close_windows": feature_complete_windows,
        "rows_audited": row_count,
        "rows_by_asset": dict(sorted(rows_by_asset.items())),
        "required_persisted_keys": len(REQUIRED_PERSISTED_KEYS),
        "missing_required_key_counts": dict(sorted(missing_keys.items())),
        "identity_failure_counts": dict(sorted(identity_failures.items())),
        "feature_quality_failure_counts": dict(sorted(feature_failures.items())),
        "non_null_coverage": {
            key: {
                "rows": non_null[key],
                "rate": non_null[key] / row_count if row_count else 0.0,
            }
            for key in REQUIRED_PERSISTED_KEYS
        },
        "latest_close_time": latest_close,
        "latest_window_identity_complete": latest_health.get("identity_complete"),
        "latest_window_feature_complete": latest_health.get("feature_complete"),
        "latest_window_feature_quality_failures": latest_health.get(
            "feature_quality_failures", []
        ),
        "consecutive_latest_usable_feature_complete_close_windows": (
            consecutive_latest_usable
        ),
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "threshold_selection_performed": False,
        "probability_scoring_performed": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
        "status": (
            "COLLECTING_OUTCOME_BLIND_FEATURE_RESERVOIR"
            if identity_complete_windows == len(geometry)
            and feature_complete_windows == len(geometry)
            else "FEATURE_RESERVOIR_INTEGRITY_FAILURE"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-db", default=str(DEFAULT_DB))
    args = parser.parse_args()
    rows = load_delayed_feature_rows_after(
        Path(args.strategy_db), identity.PROSPECTIVE_AFTER_CLOSE_TIME,
    )
    print(json.dumps(
        build_readiness(rows), indent=2, sort_keys=True, allow_nan=False,
    ))


if __name__ == "__main__":
    main()
