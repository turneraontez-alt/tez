"""De-duplicated point-in-time features for the preregistered RTI v2 study.

V1 is intentionally preserved.  This outcome-blind successor replaces two
exact duplicate taker-imbalance columns with independent 15-second and
60-second Kalshi book-pressure horizons before any outcome labels are used.
"""
from __future__ import annotations

from typing import Any, Mapping

from . import rti_microstructure as v1


FEATURE_SCHEMA_VERSION = "rti-probability-microstructure-features-v2"
MODEL_FAMILY = v1.MODEL_FAMILY
DESIGN_ID = "q15-rti-market-residual-microstructure-v2"
DESIGN_SHA256 = "0bfd8be1c12cd9179d001e7aa0b67712a9879331da35cc6ce922a90251335aad"
NON_BTC_ASSETS = v1.NON_BTC_ASSETS
EXPECTED_ASSETS = v1.EXPECTED_ASSETS
FEATURE_NAMES = (
    *v1.FEATURE_NAMES[:20],
    "kalshi_yes_microprice_edge_cents",
    "kalshi_book_delta_pressure_yes_5s",
    "kalshi_book_delta_pressure_yes_15s",
    "kalshi_book_delta_pressure_yes_30s",
    "kalshi_book_delta_pressure_yes_60s",
    "kalshi_trade_imbalance_yes_5s",
    "kalshi_trade_imbalance_yes_30s",
    "kalshi_taker_imbalance_yes_60s",
    "kalshi_best_level_flow_pressure_yes_30s",
    "spot_signed_log_net_notional_15s",
    "spot_signed_log_net_notional_60s",
    "kalshi_microstructure_missing",
    "spot_flow_missing",
)


def _direct_micro_value(
    row: Mapping[str, Any], profile: Mapping[str, Any], key: str,
) -> tuple[float, bool]:
    return v1._neutralized(row, profile, key, low=-1.0, high=1.0)


def feature_vector(row: Mapping[str, Any]) -> dict[str, Any]:
    """Build the pinned v2 vector without altering the frozen v1 builder."""
    original = v1.feature_vector(row)
    if not original.get("available"):
        return dict(original)
    original_values = dict(zip(v1.FEATURE_NAMES, original["features"]))
    profile = v1._profile(row)

    microprice, microprice_missing = v1._neutralized(
        row, profile, "kalshi_yes_microprice_edge_cents", low=-10.0, high=10.0,
    )
    book_values = []
    micro_missing = microprice_missing
    for horizon in (5, 15, 30, 60):
        value, missing = _direct_micro_value(
            row, profile, f"kalshi_book_delta_pressure_yes_{horizon}s",
        )
        book_values.append(value)
        micro_missing = micro_missing or missing
    trade_values = []
    for horizon in (5, 30):
        value, missing = _direct_micro_value(
            row, profile, f"kalshi_trade_imbalance_yes_{horizon}s",
        )
        trade_values.append(value)
        micro_missing = micro_missing or missing
    taker_60, taker_60_missing = v1._taker_imbalance(row, profile, 60)
    best_flow, best_flow_missing = v1._best_level_flow_pressure(
        row, profile, 30,
    )
    micro_missing = bool(
        micro_missing or taker_60_missing or best_flow_missing
    )

    vector = [
        *(original_values[name] for name in v1.FEATURE_NAMES[:20]),
        microprice,
        *book_values,
        *trade_values,
        taker_60,
        best_flow,
        original_values["spot_signed_log_net_notional_15s"],
        original_values["spot_signed_log_net_notional_60s"],
        1.0 if micro_missing else 0.0,
        original_values["spot_flow_missing"],
    ]
    if len(vector) != len(FEATURE_NAMES):
        return {"available": False, "error": "feature_schema_length_mismatch"}
    return {
        **original,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "features": vector,
        "microstructure_missing": micro_missing,
        "design_id": DESIGN_ID,
        "design_sha256": DESIGN_SHA256,
    }


def model_feature_window_coverage(
    rows: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
) -> dict[str, Any]:
    return v1.model_feature_window_coverage(
        rows, feature_builder=feature_vector,
    )
