"""Post-fix RTI microstructure feature design with genuine time horizons.

V1-V3 remain frozen controls.  V4 only accepts the v2 exact-source schema,
whose book/trade windows are retained by local receive time rather than a
message-count cap.  It fails closed unless every required horizon is explicitly
complete and the continuity timestamps independently support that claim.

This module constructs features only.  It cannot read outcomes, fit, notify,
trade, refit, or promote a model.
"""
from __future__ import annotations

from typing import Any, Mapping

from . import rti_microstructure as v1
from . import rti_microstructure_v3 as v3


FEATURE_SCHEMA_VERSION = "rti-probability-microstructure-features-v4"
MODEL_FAMILY = v1.MODEL_FAMILY
DESIGN_ID = "q15-rti-market-residual-microstructure-v4"
DESIGN_SHA256 = "b2c240e2a29009b1475be79dd05631fb6ab4fa3bbe85fdc8a97ecf910b7cbee0"
SOURCE_SCHEMA = "rti-exact-microstructure-v2"
SOURCE_TIME_BASIS = "local_received_at"
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
    "spot_flow_missing",
)


def _flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and float(value) in {0.0, 1.0}:
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"true", "yes", "on"}:
        return True
    if text in {"false", "no", "off"}:
        return False
    return None


def _source_integrity_error(
    row: Mapping[str, Any], profile: Mapping[str, Any],
) -> str | None:
    value = lambda key: v1._value(row, profile, key)
    if str(value("kalshi_microstructure_schema_version") or "") != SOURCE_SCHEMA:
        return "source_schema_mismatch"
    if str(value("kalshi_microstructure_time_basis") or "") != SOURCE_TIME_BASIS:
        return "microstructure_time_basis_mismatch"
    if _flag(value("kalshi_history_count_capped")) is not False:
        return "count_cap_not_explicitly_disabled"

    for horizon in (5, 15, 30):
        if _flag(value(f"kalshi_book_window_complete_{horizon}s")) is not True:
            return f"book_window_incomplete_{horizon}s"
    for horizon in (5, 15, 30, 60):
        if _flag(value(f"kalshi_trade_window_complete_{horizon}s")) is not True:
            return f"trade_window_incomplete_{horizon}s"
    if _flag(value("kalshi_microstructure_window_complete_60s")) is not True:
        return "microstructure_window_incomplete_60s"

    captured = v1._num(value("kalshi_microstructure_captured_at"))
    book_started = v1._num(value("kalshi_book_history_started_at"))
    trade_started = v1._num(value("kalshi_trade_history_started_at"))
    book_seconds = v1._num(value("kalshi_book_history_seconds"))
    trade_seconds = v1._num(value("kalshi_trade_history_seconds"))
    book_retention = v1._num(value("kalshi_book_event_retention_seconds"))
    trade_retention = v1._num(value("kalshi_trade_retention_seconds"))
    if any(raw is None for raw in (
        captured,
        book_started,
        trade_started,
        book_seconds,
        trade_seconds,
        book_retention,
        trade_retention,
    )):
        return "microstructure_history_evidence_missing"
    if book_retention < 60.0 or trade_retention < 60.0:
        return "microstructure_retention_below_60s"
    if book_seconds < 60.0 or trade_seconds < 60.0:
        return "microstructure_history_below_60s"
    if book_started > captured - 60.0 or trade_started > captured - 60.0:
        return "microstructure_history_timestamp_contradiction"
    return None


def _required_value(
    row: Mapping[str, Any],
    profile: Mapping[str, Any],
    key: str,
    *,
    low: float,
    high: float,
) -> tuple[float | None, str | None]:
    value = v1._num(v1._value(row, profile, key))
    if value is None:
        return None, f"required_feature_missing:{key}"
    return v1._clip(value, low, high), None


def feature_vector(row: Mapping[str, Any]) -> dict[str, Any]:
    """Build the pinned 32-feature vector from post-fix evidence only."""
    profile = v1._profile(row)
    integrity_error = _source_integrity_error(row, profile)
    if integrity_error:
        return {"available": False, "error": integrity_error}

    original = v3.feature_vector(row)
    if not original.get("available"):
        return {
            "available": False,
            "error": f"base:{original.get('error') or 'unavailable'}",
        }
    original_values = dict(zip(v3.FEATURE_NAMES, original["features"]))

    required_specs = (
        ("kalshi_yes_microprice_edge_cents", -10.0, 10.0),
        ("kalshi_book_delta_pressure_yes_5s", -1.0, 1.0),
        ("kalshi_book_delta_pressure_yes_15s", -1.0, 1.0),
        ("kalshi_book_delta_pressure_yes_30s", -1.0, 1.0),
        ("kalshi_trade_imbalance_yes_5s", -1.0, 1.0),
        ("kalshi_trade_imbalance_yes_30s", -1.0, 1.0),
    )
    direct_values: list[float] = []
    for key, low, high in required_specs:
        value, error = _required_value(
            row, profile, key, low=low, high=high,
        )
        if error:
            return {"available": False, "error": error}
        direct_values.append(float(value))

    taker_values: list[float] = []
    for horizon in (15, 60):
        value, missing = v1._taker_imbalance(row, profile, horizon)
        if missing:
            return {
                "available": False,
                "error": f"required_taker_flow_missing:{horizon}s",
            }
        taker_values.append(float(value))
    best_flow, best_flow_missing = v1._best_level_flow_pressure(
        row, profile, 30,
    )
    if best_flow_missing:
        return {
            "available": False,
            "error": "required_best_level_flow_missing:30s",
        }

    vector = [
        *(original_values[name] for name in v1.FEATURE_NAMES[:20]),
        *direct_values,
        *taker_values,
        float(best_flow),
        original_values["spot_signed_log_net_notional_15s"],
        original_values["spot_signed_log_net_notional_60s"],
        original_values["spot_flow_missing"],
    ]
    if len(vector) != len(FEATURE_NAMES):
        return {"available": False, "error": "feature_schema_length_mismatch"}
    return {
        **original,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "features": vector,
        "microstructure_missing": False,
        "design_id": DESIGN_ID,
        "design_sha256": DESIGN_SHA256,
    }


def model_feature_window_coverage(
    rows: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
) -> dict[str, Any]:
    return v1.model_feature_window_coverage(
        rows,
        feature_builder=feature_vector,
        source_schema=SOURCE_SCHEMA,
    )
