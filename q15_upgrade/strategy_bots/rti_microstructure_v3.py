"""Outcome-blind de-duplicated RTI microstructure feature design v3.

V2 revealed that BTC's 5,000-event capture cap made its 30s and 60s book
pressure identical.  V3 preserves both earlier manifests and replaces only the
60s pressure column with an independently varying 15s taker-flow horizon.
"""
from __future__ import annotations

from typing import Any, Mapping

from . import rti_microstructure as v1


FEATURE_SCHEMA_VERSION = "rti-probability-microstructure-features-v3"
MODEL_FAMILY = v1.MODEL_FAMILY
DESIGN_ID = "q15-rti-market-residual-microstructure-v3"
DESIGN_SHA256 = "c7c33dd0e05b2ca3711c5ae0d097350a944b2602f18cf4d217c5b35b8409b3fc"
NON_BTC_ASSETS = v1.NON_BTC_ASSETS
EXPECTED_ASSETS = v1.EXPECTED_ASSETS
FEATURE_NAMES = (
    *v1.FEATURE_NAMES[:20],
    "kalshi_yes_microprice_edge_cents",
    "kalshi_book_delta_pressure_yes_5s",
    "kalshi_book_delta_pressure_yes_15s",
    "kalshi_book_delta_pressure_yes_30s",
    "kalshi_trade_imbalance_yes_5s",
    "kalshi_trade_imbalance_yes_30s",
    "kalshi_taker_imbalance_yes_15s",
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
    for horizon in (5, 15, 30):
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
    taker_values = []
    for horizon in (15, 60):
        value, missing = v1._taker_imbalance(row, profile, horizon)
        taker_values.append(value)
        micro_missing = micro_missing or missing
    best_flow, best_flow_missing = v1._best_level_flow_pressure(
        row, profile, 30,
    )
    micro_missing = bool(micro_missing or best_flow_missing)
    vector = [
        *(original_values[name] for name in v1.FEATURE_NAMES[:20]),
        microprice,
        *book_values,
        *trade_values,
        *taker_values,
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
