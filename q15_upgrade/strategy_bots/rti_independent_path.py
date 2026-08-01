"""Exact-cutoff, label-free Coinbase/Kraken microstructure path evidence.

The endpoint summaries used by the frozen RTI designs discard most of the
within-minute shape available in the local Coinbase L2 and Kraken L3 stores.
This reader freezes a compact 60-second path using only rows whose local
``created_at`` is at or before the immutable exact-13M cutoff.  It persists
the underlying selected rows and a canonical fingerprint so every derived
number can be reconstructed later.

This module only captures evidence.  It cannot read outcomes, fit a model,
notify, promote, or trade.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping, Sequence

from .rti_independent_path_identity import (
    DESIGN_ID,
    DESIGN_SHA256,
    FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME,
)


SCHEMA_VERSION = "rti-independent-venue-path-v1"
TIME_BASIS = "local_created_at"
KRAKEN_PARTIAL_FILL_FLOW_SCHEMA_VERSION = "kraken-l3-partial-fill-flow-v1"
HORIZON_SECONDS = 60.0
MINIMUM_POINTS_PER_VENUE = 8
SUMMARY_LEVEL_LIMIT = 10.0

_ASSET_SYMBOLS = {
    "BTC": ("BTC-USD", "BTC/USD"),
    "ETH": ("ETH-USD", "ETH/USD"),
    "SOL": ("SOL-USD", "SOL/USD"),
    "XRP": ("XRP-USD", "XRP/USD"),
    "DOGE": ("DOGE-USD", "DOGE/USD"),
    "BNB": ("BNB-USD", "BNB/USD"),
    "HYPE": ("HYPE-USD", "HYPE/USD"),
}

_COMMON_COLUMNS = (
    "created_at",
    "last_message_age_seconds",
    "spread_bps",
    "depth_imbalance",
    "bid_notional_levels",
    "ask_notional_levels",
    "summary_level_limit",
)
_COINBASE_COLUMNS = (
    "update_count_5s", "remove_count_5s",
    "update_count_15s", "remove_count_15s",
    "update_count_60s", "remove_count_60s",
)
_KRAKEN_COLUMNS = (
    "add_count_5s", "update_count_5s", "delete_count_5s",
    "trade_count_5s", "cancel_to_add_5s",
    "add_count_15s", "update_count_15s", "delete_count_15s",
    "trade_count_15s", "cancel_to_add_15s",
    "add_count_60s", "update_count_60s", "delete_count_60s",
    "trade_count_60s", "cancel_to_add_60s",
    "matched_buy_notional_60s", "matched_sell_notional_60s",
    "partial_fill_flow_schema_version",
)

DERIVED_FEATURE_KEYS = (
    "rti_independent_path_mean_depth_imbalance_60s",
    "rti_independent_path_mean_depth_imbalance_half_delta_60s",
    "rti_independent_path_depth_direction_agreement_60s",
    "rti_independent_path_log1p_max_spread_stress_ratio_60s",
    "rti_independent_path_kraken_partial_fill_imbalance_acceleration_60s",
)

_TOP_LEVEL_KEYS = (
    "rti_independent_path_design_id",
    "rti_independent_path_design_sha256",
    "rti_independent_path_prospective_after_close_time",
    "rti_independent_path_first_eligible_close_time",
    "rti_independent_path_schema_version",
    "rti_independent_path_time_basis",
    "rti_independent_path_status",
    "rti_independent_path_missing_reason",
    "rti_independent_path_evidence_cutoff_at",
    "rti_independent_path_horizon_seconds",
    "rti_independent_path_max_gap_seconds_allowed",
    "rti_independent_path_minimum_points_per_venue",
    "rti_independent_path_available_count",
    "rti_independent_path_evidence_json",
    "rti_independent_path_evidence_sha256",
    *DERIVED_FEATURE_KEYS,
)
_VENUE_METRICS = (
    "status", "missing_reason", "symbol", "point_count",
    "first_created_at", "last_created_at", "start_age_seconds",
    "end_age_seconds", "max_gap_seconds", "max_message_age_seconds",
    "mean_depth_imbalance_60s", "first_half_depth_imbalance_60s",
    "second_half_depth_imbalance_60s", "depth_imbalance_half_delta_60s",
    "signed_depth_persistence_60s", "mean_spread_bps_60s",
    "max_spread_bps_60s", "spread_stress_ratio_60s",
    "log1p_spread_stress_ratio_60s",
)
_KRAKEN_FLOW_METRICS = (
    "partial_fill_imbalance_prior_45s",
    "partial_fill_imbalance_last_15s",
    "partial_fill_imbalance_acceleration_60s",
    "partial_fill_observed_fraction_60s",
)

PERSISTED_KEYS = (
    *_TOP_LEVEL_KEYS,
    *(
        f"rti_independent_path_{venue}_{key}"
        for venue in ("coinbase", "kraken")
        for key in _VENUE_METRICS
    ),
    *(
        f"rti_independent_path_kraken_{key}"
        for key in _KRAKEN_FLOW_METRICS
    ),
)


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _empty(*, asset: str, captured_at: float | None, max_gap: float) -> dict[str, Any]:
    out: dict[str, Any] = {key: None for key in PERSISTED_KEYS}
    out.update({
        "rti_independent_path_design_id": DESIGN_ID,
        "rti_independent_path_design_sha256": DESIGN_SHA256,
        "rti_independent_path_prospective_after_close_time": (
            PROSPECTIVE_AFTER_CLOSE_TIME
        ),
        "rti_independent_path_first_eligible_close_time": (
            FIRST_ELIGIBLE_CLOSE_TIME
        ),
        "rti_independent_path_schema_version": SCHEMA_VERSION,
        "rti_independent_path_time_basis": TIME_BASIS,
        "rti_independent_path_status": "missing",
        "rti_independent_path_missing_reason": None,
        "rti_independent_path_evidence_cutoff_at": captured_at,
        "rti_independent_path_horizon_seconds": HORIZON_SECONDS,
        "rti_independent_path_max_gap_seconds_allowed": max_gap,
        "rti_independent_path_minimum_points_per_venue": (
            MINIMUM_POINTS_PER_VENUE
        ),
        "rti_independent_path_available_count": 0,
    })
    for venue in ("coinbase", "kraken"):
        out[f"rti_independent_path_{venue}_status"] = "missing"
        out[f"rti_independent_path_{venue}_symbol"] = (
            (_ASSET_SYMBOLS.get(asset) or (None, None))[0 if venue == "coinbase" else 1]
        )
    return out


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
    }


def _read_rows(
    *, db_path: str, table: str, symbol_column: str, symbol: str,
    cutoff: float, columns: Sequence[str],
) -> tuple[list[dict[str, Any]], str | None]:
    if not Path(db_path).exists():
        return [], "database_missing"
    start = cutoff - HORIZON_SECONDS
    try:
        connection = sqlite3.connect(
            f"file:{Path(db_path).resolve()}?mode=ro", uri=True, timeout=2.0,
        )
        connection.row_factory = sqlite3.Row
        try:
            available = _table_columns(connection, table)
            required = {symbol_column, *columns}
            if not required.issubset(available):
                missing = sorted(required - available)
                return [], f"schema_missing:{','.join(missing)}"
            selected = ",".join(columns)
            first = connection.execute(
                f"SELECT {selected} FROM {table} "
                f"WHERE {symbol_column}=? AND created_at<=? "
                "ORDER BY created_at DESC LIMIT 1",
                (symbol, start),
            ).fetchone()
            interior = connection.execute(
                f"SELECT {selected} FROM {table} "
                f"WHERE {symbol_column}=? AND created_at>? AND created_at<=? "
                "ORDER BY created_at",
                (symbol, start, cutoff),
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return [], f"query_error:{type(exc).__name__}"
    rows = ([] if first is None else [dict(first)]) + [dict(row) for row in interior]
    unique: dict[float, dict[str, Any]] = {}
    for row in rows:
        timestamp = _num(row.get("created_at"))
        if timestamp is not None:
            unique[timestamp] = row
    return [unique[key] for key in sorted(unique)], None


def _time_weighted(
    rows: Sequence[Mapping[str, Any]], value: Callable[[Mapping[str, Any]], float],
    *, start: float, end: float,
) -> float:
    if end <= start:
        raise ValueError("invalid_time_weighted_interval")
    active = rows[0]
    for row in rows:
        timestamp = float(row["created_at"])
        if timestamp <= start:
            active = row
        else:
            break
    cursor = start
    total = 0.0
    for row in rows:
        timestamp = float(row["created_at"])
        if timestamp <= start:
            continue
        if timestamp >= end:
            break
        total += value(active) * (timestamp - cursor)
        active = row
        cursor = timestamp
    total += value(active) * (end - cursor)
    return total / (end - start)


def _flow_imbalance(row: Mapping[str, Any]) -> float:
    resting_bid_filled = float(row["matched_buy_notional_60s"])
    resting_ask_filled = float(row["matched_sell_notional_60s"])
    total = resting_bid_filled + resting_ask_filled
    return 0.0 if total <= 0.0 else (
        (resting_ask_filled - resting_bid_filled) / total
    )


def _validate_and_summarize(
    rows: Sequence[Mapping[str, Any]], *, venue: str, symbol: str,
    cutoff: float, max_gap: float,
) -> tuple[dict[str, Any] | None, str | None, list[dict[str, Any]]]:
    if len(rows) < MINIMUM_POINTS_PER_VENUE:
        return None, "insufficient_points", []
    start = cutoff - HORIZON_SECONDS
    normalized: list[dict[str, Any]] = []
    for raw in rows:
        timestamp = _num(raw.get("created_at"))
        message_age = _num(raw.get("last_message_age_seconds"))
        spread = _num(raw.get("spread_bps"))
        imbalance = _num(raw.get("depth_imbalance"))
        bid_notional = _num(raw.get("bid_notional_levels"))
        ask_notional = _num(raw.get("ask_notional_levels"))
        level_limit = _num(raw.get("summary_level_limit"))
        if any(value is None for value in (
            timestamp, message_age, spread, imbalance,
            bid_notional, ask_notional, level_limit,
        )):
            return None, "common_metric_invalid", []
        assert timestamp is not None and message_age is not None
        assert spread is not None and imbalance is not None
        assert bid_notional is not None and ask_notional is not None
        assert level_limit is not None
        if timestamp > cutoff + 1e-6:
            return None, "future_snapshot", []
        if not 0.0 <= message_age <= max_gap:
            return None, "message_age_invalid", []
        if not 0.0 <= spread <= 10_000.0:
            return None, "spread_invalid", []
        if not -1.0 <= imbalance <= 1.0:
            return None, "depth_imbalance_invalid", []
        if bid_notional < 0.0 or ask_notional < 0.0 or (
            bid_notional + ask_notional <= 0.0
        ):
            return None, "depth_notional_invalid", []
        if level_limit != SUMMARY_LEVEL_LIMIT:
            return None, "summary_level_limit_mismatch", []
        row = {
            "created_at": timestamp,
            "last_message_age_seconds": message_age,
            "spread_bps": spread,
            "depth_imbalance": imbalance,
            "bid_notional_levels": bid_notional,
            "ask_notional_levels": ask_notional,
            "summary_level_limit": level_limit,
        }
        activity = _COINBASE_COLUMNS if venue == "coinbase" else _KRAKEN_COLUMNS
        for key in activity:
            value = raw.get(key)
            if key == "partial_fill_flow_schema_version":
                row[key] = None if value is None else str(value)
                continue
            number = _num(value)
            # Kraken correctly emits NULL for cancel/add when the denominator
            # is zero.  These ratios are retained only as reconstructable raw
            # context and are not one of the five frozen candidate features;
            # a mathematically undefined optional ratio must not invalidate
            # otherwise complete depth and partial-fill evidence.
            if key.startswith("cancel_to_add_") and number is None:
                add_key = key.replace("cancel_to_add_", "add_count_", 1)
                add_count = _num(raw.get(add_key))
                if add_count == 0.0:
                    row[key] = None
                    continue
            if number is None or number < 0.0:
                return None, f"activity_metric_invalid:{key}", []
            row[key] = number
        normalized.append(row)
    first = float(normalized[0]["created_at"])
    last = float(normalized[-1]["created_at"])
    start_age = start - first
    end_age = cutoff - last
    if start_age < -1e-6 or start_age > max_gap:
        return None, "start_snapshot_stale", []
    if end_age < -1e-6 or (
        end_age + float(normalized[-1]["last_message_age_seconds"]) > max_gap
    ):
        return None, "end_snapshot_stale", []
    interior_times = [
        min(cutoff, max(start, float(row["created_at"])))
        for row in normalized
    ]
    gaps = [right - left for left, right in zip(
        [start, *interior_times], [*interior_times, cutoff],
    )]
    max_observed_gap = max(gaps or [HORIZON_SECONDS])
    if max_observed_gap > max_gap + 1e-6:
        return None, "path_continuity_gap", []
    depth = lambda row: float(row["depth_imbalance"])
    spread_value = lambda row: float(row["spread_bps"])
    first_half = _time_weighted(
        normalized, depth, start=start, end=start + 30.0,
    )
    second_half = _time_weighted(
        normalized, depth, start=start + 30.0, end=cutoff,
    )
    mean_depth = _time_weighted(normalized, depth, start=start, end=cutoff)
    signed_persistence = _time_weighted(
        normalized,
        lambda row: (
            1.0 if depth(row) > 1e-12 else -1.0 if depth(row) < -1e-12 else 0.0
        ),
        start=start,
        end=cutoff,
    )
    mean_spread = _time_weighted(
        normalized, spread_value, start=start, end=cutoff,
    )
    max_spread = max(spread_value(row) for row in normalized)
    stress_ratio = max_spread / max(mean_spread, 1e-9)
    summary: dict[str, Any] = {
        "status": "ok",
        "missing_reason": None,
        "symbol": symbol,
        "point_count": len(normalized),
        "first_created_at": first,
        "last_created_at": last,
        "start_age_seconds": max(0.0, start_age),
        "end_age_seconds": max(0.0, end_age),
        "max_gap_seconds": max_observed_gap,
        "max_message_age_seconds": max(
            float(row["last_message_age_seconds"]) for row in normalized
        ),
        "mean_depth_imbalance_60s": mean_depth,
        "first_half_depth_imbalance_60s": first_half,
        "second_half_depth_imbalance_60s": second_half,
        "depth_imbalance_half_delta_60s": second_half - first_half,
        "signed_depth_persistence_60s": signed_persistence,
        "mean_spread_bps_60s": mean_spread,
        "max_spread_bps_60s": max_spread,
        "spread_stress_ratio_60s": stress_ratio,
        "log1p_spread_stress_ratio_60s": math.log1p(
            max(0.0, stress_ratio - 1.0)
        ),
    }
    if venue == "kraken":
        if normalized[-1].get("partial_fill_flow_schema_version") != (
            KRAKEN_PARTIAL_FILL_FLOW_SCHEMA_VERSION
        ) or any(
            row.get("partial_fill_flow_schema_version")
            != KRAKEN_PARTIAL_FILL_FLOW_SCHEMA_VERSION
            for row in normalized
        ):
            return None, "partial_fill_flow_schema_mismatch", []
        prior = _time_weighted(
            normalized, _flow_imbalance, start=start, end=cutoff - 15.0,
        )
        recent = _time_weighted(
            normalized, _flow_imbalance, start=cutoff - 15.0, end=cutoff,
        )
        observed_fraction = _time_weighted(
            normalized,
            lambda row: 1.0 if (
                float(row["matched_buy_notional_60s"])
                + float(row["matched_sell_notional_60s"])
            ) > 0.0 else 0.0,
            start=start,
            end=cutoff,
        )
        summary.update({
            "partial_fill_imbalance_prior_45s": prior,
            "partial_fill_imbalance_last_15s": recent,
            "partial_fill_imbalance_acceleration_60s": recent - prior,
            "partial_fill_observed_fraction_60s": observed_fraction,
        })
    return summary, None, normalized


def _direction_agreement(left: float, right: float) -> float:
    if abs(left) <= 1e-12 or abs(right) <= 1e-12:
        return 0.5
    return 1.0 if (left > 0.0) == (right > 0.0) else 0.0


def _combined_features(
    summaries: Mapping[str, Mapping[str, Any]],
) -> dict[str, float]:
    cb = summaries["coinbase"]
    kr = summaries["kraken"]
    return {
        "rti_independent_path_mean_depth_imbalance_60s": (
            float(cb["mean_depth_imbalance_60s"])
            + float(kr["mean_depth_imbalance_60s"])
        ) / 2.0,
        "rti_independent_path_mean_depth_imbalance_half_delta_60s": (
            float(cb["depth_imbalance_half_delta_60s"])
            + float(kr["depth_imbalance_half_delta_60s"])
        ) / 2.0,
        "rti_independent_path_depth_direction_agreement_60s": (
            _direction_agreement(
                float(cb["mean_depth_imbalance_60s"]),
                float(kr["mean_depth_imbalance_60s"]),
            )
        ),
        "rti_independent_path_log1p_max_spread_stress_ratio_60s": max(
            float(cb["log1p_spread_stress_ratio_60s"]),
            float(kr["log1p_spread_stress_ratio_60s"]),
        ),
        "rti_independent_path_kraken_partial_fill_imbalance_acceleration_60s": (
            float(kr["partial_fill_imbalance_acceleration_60s"])
        ),
    }


def _same(left: Any, right: Any) -> bool:
    left_number = _num(left)
    right_number = _num(right)
    return bool(
        left_number is not None
        and right_number is not None
        and math.isclose(
            left_number, right_number, rel_tol=1e-10, abs_tol=1e-10,
        )
    )


def validate_persisted_independent_path(row: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute every frozen summary from its canonical persisted evidence."""
    errors: list[str] = []
    if row.get("rti_independent_path_design_id") != DESIGN_ID:
        errors.append("design_id_mismatch")
    if row.get("rti_independent_path_design_sha256") != DESIGN_SHA256:
        errors.append("design_sha256_mismatch")
    if row.get("rti_independent_path_schema_version") != SCHEMA_VERSION:
        errors.append("schema_version_mismatch")
    if row.get("rti_independent_path_time_basis") != TIME_BASIS:
        errors.append("time_basis_mismatch")
    if row.get("rti_independent_path_status") != "ok":
        errors.append("source_status_not_ok")
    cutoff = _num(row.get("rti_independent_path_evidence_cutoff_at"))
    source_captured = _num(row.get("source_captured_at"))
    if cutoff is None:
        errors.append("cutoff_missing")
    elif source_captured is not None and not math.isclose(
        cutoff, source_captured, rel_tol=0.0, abs_tol=1e-6,
    ):
        errors.append("cutoff_not_source_captured_at")
    evidence_json = row.get("rti_independent_path_evidence_json")
    fingerprint = row.get("rti_independent_path_evidence_sha256")
    if not isinstance(evidence_json, str) or not evidence_json:
        errors.append("evidence_json_missing")
        payload: Mapping[str, Any] = {}
    else:
        actual = hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
        if actual != fingerprint:
            errors.append("evidence_sha256_mismatch")
        try:
            parsed = json.loads(evidence_json)
            payload = parsed if isinstance(parsed, Mapping) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
            errors.append("evidence_json_invalid")
    if payload:
        if payload.get("schema_version") != SCHEMA_VERSION:
            errors.append("payload_schema_mismatch")
        if payload.get("time_basis") != TIME_BASIS:
            errors.append("payload_time_basis_mismatch")
        if cutoff is None or not _same(payload.get("captured_at"), cutoff):
            errors.append("payload_cutoff_mismatch")
        asset = str(row.get("asset") or "").upper()
        if asset and payload.get("asset") != asset:
            errors.append("payload_asset_mismatch")
        max_gap = _num(payload.get("max_gap_seconds"))
        if max_gap is None or not 1.0 <= max_gap <= 15.0:
            errors.append("payload_max_gap_invalid")
        raw_venues = payload.get("venues")
        if not isinstance(raw_venues, Mapping) or set(raw_venues) != {
            "coinbase", "kraken",
        }:
            errors.append("payload_venues_invalid")
        elif cutoff is not None and max_gap is not None:
            summaries: dict[str, Mapping[str, Any]] = {}
            for venue in ("coinbase", "kraken"):
                raw_rows = raw_venues.get(venue)
                if not isinstance(raw_rows, list):
                    errors.append(f"payload_{venue}_rows_invalid")
                    continue
                symbol = str(
                    row.get(f"rti_independent_path_{venue}_symbol") or ""
                )
                summary, reason, _ = _validate_and_summarize(
                    raw_rows, venue=venue, symbol=symbol, cutoff=cutoff,
                    max_gap=max_gap,
                )
                if reason is not None or summary is None:
                    errors.append(f"payload_{venue}_invalid:{reason}")
                    continue
                summaries[venue] = summary
                metrics = (*_VENUE_METRICS, *(
                    _KRAKEN_FLOW_METRICS if venue == "kraken" else ()
                ))
                for metric in metrics:
                    expected = summary.get(metric)
                    stored = row.get(f"rti_independent_path_{venue}_{metric}")
                    if isinstance(expected, str) or expected is None:
                        if stored != expected:
                            errors.append(f"stored_{venue}_{metric}_mismatch")
                    elif not _same(stored, expected):
                        errors.append(f"stored_{venue}_{metric}_mismatch")
            if set(summaries) == {"coinbase", "kraken"}:
                for key, expected in _combined_features(summaries).items():
                    if not _same(row.get(key), expected):
                        errors.append(f"stored_feature_mismatch:{key}")
    close_time = _num(row.get("close_time"))
    prospective = bool(
        close_time is not None
        and close_time > PROSPECTIVE_AFTER_CLOSE_TIME
        and close_time >= FIRST_ELIGIBLE_CLOSE_TIME
    )
    return {
        "valid": not errors,
        "errors": errors,
        "outcome_labels_read": False,
        "prospective_credit_eligible": prospective,
        "design_id": DESIGN_ID,
        "design_sha256": DESIGN_SHA256,
    }


def capture_rti_independent_path(
    asset: str, *, captured_at: float,
    coinbase_db: str | None = None, kraken_db: str | None = None,
    max_gap_seconds: float = 10.0,
) -> dict[str, Any]:
    """Freeze a reconstructable 60-second independent-venue path."""
    asset_key = str(asset or "").upper()
    captured = _num(captured_at)
    max_gap = max(1.0, min(15.0, float(max_gap_seconds)))
    out = _empty(asset=asset_key, captured_at=captured, max_gap=max_gap)
    symbols = _ASSET_SYMBOLS.get(asset_key)
    if captured is None or symbols is None:
        out["rti_independent_path_missing_reason"] = "invalid_asset_or_timestamp"
        return out
    sources = {
        "coinbase": {
            "db_path": coinbase_db or os.environ.get(
                "Q15_COINBASE_ADV_L2_DB", "data/q15_coinbase_adv_l2_v1.sqlite3",
            ),
            "table": "coinbase_adv_l2_snapshots",
            "symbol_column": "product_id",
            "symbol": symbols[0],
            "columns": (*_COMMON_COLUMNS, *_COINBASE_COLUMNS),
        },
        "kraken": {
            "db_path": kraken_db or os.environ.get(
                "Q15_KRAKEN_L3_DB", "data/q15_kraken_l3_v1.sqlite3",
            ),
            "table": "kraken_l3_summaries",
            "symbol_column": "symbol",
            "symbol": symbols[1],
            "columns": (*_COMMON_COLUMNS, *_KRAKEN_COLUMNS),
        },
    }
    summaries: dict[str, dict[str, Any]] = {}
    evidence: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    for venue, spec in sources.items():
        rows, error = _read_rows(
            db_path=str(spec["db_path"]), table=str(spec["table"]),
            symbol_column=str(spec["symbol_column"]), symbol=str(spec["symbol"]),
            cutoff=captured, columns=tuple(spec["columns"]),
        )
        summary = None
        normalized: list[dict[str, Any]] = []
        if error is None:
            summary, error, normalized = _validate_and_summarize(
                rows, venue=venue, symbol=str(spec["symbol"]), cutoff=captured,
                max_gap=max_gap,
            )
        if error is not None or summary is None:
            reason = error or "path_unavailable"
            out[f"rti_independent_path_{venue}_missing_reason"] = reason
            errors.append(f"{venue}:{reason}")
            continue
        summaries[venue] = summary
        evidence[venue] = normalized
        for key, value in summary.items():
            out[f"rti_independent_path_{venue}_{key}"] = value
    out["rti_independent_path_available_count"] = len(summaries)
    if errors or len(summaries) != 2:
        out["rti_independent_path_missing_reason"] = ";".join(errors)
        return out
    payload = {
        "schema_version": SCHEMA_VERSION,
        "time_basis": TIME_BASIS,
        "asset": asset_key,
        "captured_at": captured,
        "horizon_seconds": HORIZON_SECONDS,
        "max_gap_seconds": max_gap,
        "venues": evidence,
    }
    evidence_json = _canonical_json(payload)
    out.update({
        "rti_independent_path_status": "ok",
        "rti_independent_path_missing_reason": None,
        "rti_independent_path_evidence_json": evidence_json,
        "rti_independent_path_evidence_sha256": hashlib.sha256(
            evidence_json.encode("utf-8")
        ).hexdigest(),
        **_combined_features(summaries),
    })
    return out
