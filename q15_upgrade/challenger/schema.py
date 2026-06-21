"""Minimum contract-observation schema + validation (addendum Section B).

Defines the fields a contract observation SHOULD carry before model evaluation,
and validates an observation dict against them. Validation is non-destructive: it
reports missing/extra/problem fields; it does not fabricate values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

CONTRACT_OBSERVATION_FIELDS: tuple[str, ...] = (
    "contract_id", "platform", "asset", "underlying_symbol",
    "decision_timestamp_utc", "event_timestamp_utc", "receive_timestamp_utc",
    "processing_timestamp_utc", "prediction_persisted_timestamp_utc",
    "expiration_timestamp_utc", "target_price", "settlement_source",
    "settlement_method", "equality_rule",
    "underlying_bid", "underlying_ask", "underlying_mid", "underlying_last",
    "underlying_index_price", "underlying_mark_price",
    "yes_bid", "yes_ask", "no_bid", "no_ask",
    "contract_volume", "contract_liquidity", "quote_age",
    "control_probability", "control_action", "control_model_version",
    "feature_version", "data_quality_flags",
    "final_settlement_price", "final_outcome", "settlement_status",
    "hypothetical_entry_price", "hypothetical_fill_status",
    "estimated_fees", "estimated_slippage", "hypothetical_profit_or_loss",
)

UNDERLYING_RECORD_FIELDS: tuple[str, ...] = (
    "exchange", "symbol", "event_timestamp", "receive_timestamp",
    "processing_timestamp", "sequence_number", "bid", "ask", "last_trade",
    "trade_size", "trade_direction", "volume", "order_book", "data_quality_flags",
)

# Fields that must be present at minimum to grade a contract at all.
_CRITICAL = (
    "contract_id", "asset", "decision_timestamp_utc", "expiration_timestamp_utc",
    "target_price", "settlement_source", "yes_ask", "no_ask",
    "control_probability", "final_outcome", "settlement_status",
)


@dataclass
class ValidationResult:
    ok: bool
    missing_critical: list[str] = field(default_factory=list)
    missing_optional: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def validate_observation(obs: Mapping[str, Any],
                         fields: tuple[str, ...] = CONTRACT_OBSERVATION_FIELDS,
                         critical: tuple[str, ...] = _CRITICAL) -> ValidationResult:
    present = {k for k, v in obs.items() if v is not None}
    missing_critical = [f for f in critical if f not in present]
    missing_optional = [f for f in fields if f not in present and f not in critical]
    extra = [k for k in obs.keys() if k not in fields]
    problems: list[str] = []

    # Timestamp ordering sanity (only when present).
    dt = obs.get("decision_timestamp_utc")
    rt = obs.get("receive_timestamp_utc")
    if dt is not None and rt is not None and _num(rt) is not None and _num(dt) is not None:
        if _num(rt) > _num(dt):
            problems.append("receive_timestamp_after_decision (leakage)")
    exp = obs.get("expiration_timestamp_utc")
    if dt is not None and exp is not None and _num(exp) is not None and _num(dt) is not None:
        if _num(exp) < _num(dt):
            problems.append("expiration_before_decision")
    status = str(obs.get("settlement_status") or "").upper()
    if status and status not in {"VALID", "AMBIGUOUS", "DISPUTED", "CANCELLED",
                                 "REFUNDED", "MISSING", "CORRECTED"}:
        problems.append(f"unknown_settlement_status({status})")

    ok = not missing_critical and not problems
    return ValidationResult(ok, missing_critical, missing_optional, extra, problems)


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
