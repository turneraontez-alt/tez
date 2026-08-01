"""Outcome-blind coverage checks for optional RTI path-dynamics evidence.

The extension is collected beside frozen microstructure v4 and cannot change a
decision.  It records richer raw dynamics for a later preregistered design,
while this module only verifies that complete seven-asset folds exist.  It has
no outcome, model, notification, order, or promotion surface.
"""
from __future__ import annotations

from collections import defaultdict
import math
from typing import Any, Mapping, Sequence

from .rti_microstructure import EXPECTED_ASSETS
from .rules import (
    RTI_EXACT_MICROSTRUCTURE_EXTENSION_SCHEMA_VERSION,
    RTI_EXACT_MICROSTRUCTURE_SCHEMA_V2,
)


EXTENSION_SCHEMA_VERSION = RTI_EXACT_MICROSTRUCTURE_EXTENSION_SCHEMA_VERSION
SOURCE_SCHEMA = RTI_EXACT_MICROSTRUCTURE_SCHEMA_V2
TIME_BASIS = "local_received_at"
HORIZONS = (5, 15, 30, 60)
REQUIRED_METRICS = (
    "book_add_volume_yes",
    "book_remove_volume_yes",
    "book_add_volume_no",
    "book_remove_volume_no",
    "microprice_change_cents",
    "microprice_range_cents",
    "microprice_variation_cents",
    "microprice_trend_efficiency",
    "trade_yes_price_change_cents",
    "trade_yes_price_range_cents",
    "trade_yes_price_variation_cents",
    "trade_yes_price_trend_efficiency",
)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and float(value) in {0.0, 1.0}:
        return bool(value)
    return None


def extension_window_coverage(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Count full, executable extension folds without reading outcomes."""
    grouped: dict[float, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if str(row.get("bot_name") or "") != "rti_path_13m":
            continue
        if str(row.get("interval") or "").upper() != "13M":
            continue
        if str(row.get("record_kind") or "").upper() != (
            "RTI_PATH_13M_PROSPECTIVE_EXACT"
        ):
            continue
        if row.get("kalshi_microstructure_extension_schema_version") != (
            EXTENSION_SCHEMA_VERSION
        ):
            continue
        close = _num(row.get("close_time"))
        if close is not None:
            grouped[close].append(row)

    schema_complete_times: list[float] = []
    complete_times: list[float] = []
    unavailable_rows: list[dict[str, Any]] = []
    unusable_windows: list[dict[str, Any]] = []
    for close, window_rows in sorted(grouped.items()):
        assets = {str(row.get("asset") or "").upper() for row in window_rows}
        if len(window_rows) != len(EXPECTED_ASSETS) or assets != EXPECTED_ASSETS:
            continue
        schema_complete_times.append(close)
        errors = []
        for row in window_rows:
            reasons = []
            if row.get("kalshi_microstructure_schema_version") != SOURCE_SCHEMA:
                reasons.append("SOURCE_SCHEMA_MISMATCH")
            if row.get("kalshi_microstructure_time_basis") != TIME_BASIS:
                reasons.append("TIME_BASIS_MISMATCH")
            if _flag(row.get("kalshi_history_count_capped")) is not False:
                reasons.append("COUNT_CAP_NOT_DISABLED")
            for horizon in HORIZONS:
                if _flag(row.get(
                    f"kalshi_microstructure_window_complete_{horizon}s"
                )) is not True:
                    reasons.append(f"WINDOW_INCOMPLETE_{horizon}S")
                for metric in REQUIRED_METRICS:
                    if _num(row.get(f"kalshi_{metric}_{horizon}s")) is None:
                        reasons.append(
                            f"REQUIRED_METRIC_MISSING:{metric}_{horizon}s"
                        )
                trade_count = _num(row.get(f"kalshi_trade_count_{horizon}s"))
                trade_vwap = _num(row.get(
                    f"kalshi_trade_yes_vwap_cents_{horizon}s"
                ))
                if trade_count is None:
                    reasons.append(f"TRADE_COUNT_MISSING_{horizon}S")
                elif trade_count > 0.0 and trade_vwap is None:
                    reasons.append(f"TRADE_VWAP_MISSING_{horizon}S")
            if reasons:
                error = {
                    "id": row.get("id"),
                    "close_time": close,
                    "asset": str(row.get("asset") or "").upper(),
                    "reasons": reasons,
                }
                unavailable_rows.append(error)
                errors.append(error)
        if errors:
            unusable_windows.append({
                "close_time": close,
                "unavailable_rows": errors,
            })
        else:
            complete_times.append(close)
    return {
        "extension_schema_version": EXTENSION_SCHEMA_VERSION,
        "source_schema": SOURCE_SCHEMA,
        "outcome_labels_read": False,
        "schema_complete_extension_close_windows": len(
            schema_complete_times
        ),
        "schema_complete_extension_close_times": schema_complete_times,
        "complete_extension_close_windows": len(complete_times),
        "complete_extension_close_times": complete_times,
        "extension_unavailable_rows": unavailable_rows,
        "unusable_extension_close_windows": unusable_windows,
    }
