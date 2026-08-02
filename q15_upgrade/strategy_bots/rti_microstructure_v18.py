"""Outcome-blind V18 strict-low-reversal selection rule.

The module only determines prospective eligibility. It cannot read outcomes,
fit a model, notify, promote, size a live order, or trade.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

from . import rti_microstructure_v18_identity as identity
from tools.q15_rti_microstructure_preregister import design_fingerprint


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROTOCOL = ROOT / identity.PROTOCOL_RELATIVE_PATH
NON_BTC_ASSETS = frozenset({"BNB", "DOGE", "ETH", "HYPE", "SOL", "XRP"})
ALL_ASSETS = NON_BTC_ASSETS | {"BTC"}
MAX_DATA_AGE_SECONDS = 3.0
MAX_TIMING_OFFSET_SECONDS = 2.0


def _num(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _profile(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("threshold_json")
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        value = json.loads(str(raw or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _value(
    row: Mapping[str, Any], profile: Mapping[str, Any], key: str,
) -> Any:
    value = row.get(key)
    return value if value is not None else profile.get(key)


def _flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"true", "yes", "1"}:
        return True
    if text in {"false", "no", "0"}:
        return False
    return None


def _reason_codes(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = None
        value = decoded if isinstance(decoded, list) else [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value]


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def load_protocol(path: Path = DEFAULT_PROTOCOL) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("v18_protocol_unreadable") from exc
    if not isinstance(value, Mapping):
        raise ValueError("v18_protocol_root_not_object")
    protocol = dict(value)
    population = dict(protocol.get("population") or {})
    rule = dict(protocol.get("frozen_rule") or {})
    collection = dict(protocol.get("collection") or {})
    if (
        design_fingerprint(protocol) != identity.PROTOCOL_SHA256
        or protocol.get("protocol_id") != identity.PROTOCOL_ID
        or protocol.get("design_id") != identity.DESIGN_ID
        or protocol.get("protocol_status")
        != "FROZEN_BEFORE_ANY_V18_PROSPECTIVE_OUTCOME_ACCESS"
        or float(population.get("prospective_after_close_time") or 0.0)
        != identity.PROSPECTIVE_AFTER_CLOSE_TIME
        or float(population.get("first_eligible_close_time") or 0.0)
        != identity.FIRST_ELIGIBLE_CLOSE_TIME
        or population.get("btc_labels_forbidden") is not True
        or rule.get("rule_version") != identity.RULE_VERSION
        or rule.get("strict_control_passed_required") is not True
        or rule.get("reversal_risk_policy_version") != identity.RISK_POLICY_VERSION
        or rule.get("reversal_risk_class_required") != "low"
        or rule.get("additional_threshold_tuning_allowed") is not False
        or collection.get("outcome_access_allowed_now") is not False
        or collection.get("model_fit_allowed") is not False
        or collection.get("probability_scoring_allowed") is not False
        or collection.get("paper_pick_artifact_allowed_now") is not False
        or collection.get("notifications_allowed_now") is not False
        or collection.get("telegram_allowed_now") is not False
        or collection.get("automatic_promotion_allowed") is not False
        or collection.get("real_trading_allowed") is not False
    ):
        raise ValueError("v18_protocol_identity_or_safety_invalid")
    return protocol


def evaluate_source_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the all-seven exact source using only decision-time evidence."""
    load_protocol()
    profile = _profile(row)
    close_time = _num(_value(row, profile, "close_time"))
    asset = str(
        _value(row, profile, "asset")
        or profile.get("asset_cohort")
        or ""
    ).upper()
    side = str(
        _value(row, profile, "side") or profile.get("rti_side") or ""
    ).upper()
    entry_ask = _num(_value(row, profile, "entry_ask_cents"))
    spread = _num(_value(row, profile, "spread_cents"))
    source_captured_at = _num(_value(row, profile, "source_captured_at"))
    evidence_as_of = _num(_value(row, profile, "evidence_as_of"))
    paper_only = _flag(_value(row, profile, "paper_only"))
    strict_passed = _flag(_value(row, profile, "passed"))
    risk_policy = str(_value(
        row, profile, "rti_risk_policy_version"
    ) or "")
    reversal_risk = str(_value(
        row, profile, "rti_reversal_risk_class"
    ) or "")
    path_status = str(_value(row, profile, "rti_path_status") or "")
    path_complete = _flag(_value(row, profile, "rti_path_complete"))
    expected_path_count = _num(_value(
        row, profile, "rti_path_expected_count"
    ))
    path_count = _num(_value(row, profile, "rti_path_count"))
    max_receive_age = _num(_value(
        row, profile, "rti_path_max_receive_age_s"
    ))
    decision_age = _num(_value(row, profile, "rti_decision_age_s"))
    stored_timing_offset = _num(_value(
        row, profile, "rti_timing_offset_s"
    ))
    path_evaluation_delay = _num(_value(
        row, profile, "rti_path_evaluation_delay_s"
    ))
    quote_age = _num(_value(row, profile, "quote_age_seconds"))
    evaluation_delay = (
        None
        if evidence_as_of is None or source_captured_at is None
        else evidence_as_of - source_captured_at
    )
    exact_offset = (
        None if close_time is None or source_captured_at is None
        else abs(source_captured_at - (close_time - 780.0))
    )
    failures = []
    if asset not in ALL_ASSETS:
        failures.append("SOURCE_ASSET_IDENTITY")
    if close_time is None or close_time <= identity.PROSPECTIVE_AFTER_CLOSE_TIME:
        failures.append("STRICTLY_PROSPECTIVE_CLOSE_REQUIRED")
    if str(_value(row, profile, "bot_name") or "") != "rti_path_13m":
        failures.append("SOURCE_BOT_IDENTITY")
    if str(_value(row, profile, "interval") or "") != "13M":
        failures.append("SOURCE_INTERVAL_IDENTITY")
    if str(_value(row, profile, "record_kind") or "") != "RTI_PATH_13M_PROSPECTIVE_EXACT":
        failures.append("SOURCE_RECORD_KIND_IDENTITY")
    if side not in {"YES", "NO"}:
        failures.append("RTI_SIDE_MISSING")
    if entry_ask is None or spread is None:
        failures.append("EXECUTABLE_QUOTE_MISSING")
    elif not 0.0 <= entry_ask <= 99.0 or spread < 0.0:
        failures.append("EXECUTABLE_QUOTE_INVALID")
    if exact_offset is None or exact_offset > 2.0:
        failures.append("EXACT_13M_TIMESTAMP")
    if evaluation_delay is None or evaluation_delay < -1e-6:
        failures.append("EVIDENCE_PRECEDES_CAPTURE")
    elif evaluation_delay > 2.0:
        failures.append("EVALUATION_NOT_FRESH")
    if paper_only is not True:
        failures.append("SOURCE_PAPER_ONLY")
    if risk_policy != identity.RISK_POLICY_VERSION:
        failures.append("RISK_POLICY_IDENTITY")
    if reversal_risk not in {"low", "medium", "high"}:
        failures.append("REVERSAL_RISK_MISSING")
    if (
        path_status != "ok"
        or path_complete is not True
        or expected_path_count != 61.0
        or path_count != 61.0
        or max_receive_age is None
        or decision_age is None
        or not -1e-6 <= max_receive_age <= MAX_DATA_AGE_SECONDS
        or not -1e-6 <= decision_age <= MAX_DATA_AGE_SECONDS
    ):
        failures.append("PATH_61_FRESH")
    if (
        stored_timing_offset is None
        or not -1e-6 <= stored_timing_offset <= MAX_TIMING_OFFSET_SECONDS
    ):
        failures.append("EXACT_TIMING")
    if (
        path_evaluation_delay is None
        or not -1e-6
        <= path_evaluation_delay
        <= MAX_TIMING_OFFSET_SECONDS
    ):
        failures.append("EVALUATION_WITHIN_2S")
    if quote_age is None or not -1e-6 <= quote_age <= MAX_DATA_AGE_SECONDS:
        failures.append("QUOTE_FRESH")
    evidence = {
        "id": int(row["id"]) if row.get("id") is not None else None,
        "ticker": str(_value(row, profile, "ticker") or ""),
        "asset": asset,
        "close_time": close_time,
        "side": side,
        "entry_ask_cents": entry_ask,
        "spread_cents": spread,
        "source_captured_at": source_captured_at,
        "evidence_as_of": evidence_as_of,
        "evaluation_delay_seconds": evaluation_delay,
        "exact_timing_offset_seconds": exact_offset,
        "stored_timing_offset_seconds": stored_timing_offset,
        "path_status": path_status,
        "path_complete": path_complete is True,
        "path_expected_count": expected_path_count,
        "path_count": path_count,
        "path_max_receive_age_seconds": max_receive_age,
        "path_decision_age_seconds": decision_age,
        "path_evaluation_delay_seconds": path_evaluation_delay,
        "quote_age_seconds": quote_age,
        "strict_control_passed": strict_passed is True,
        "strict_rule_version": str(_value(row, profile, "rule_version") or ""),
        "risk_policy_version": risk_policy,
        "reversal_risk_class": reversal_risk,
        "reversal_risk_reason_codes": _reason_codes(_value(
            row, profile, "rti_reversal_risk_reason_codes"
        )),
    }
    return {
        "design_id": identity.DESIGN_ID,
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "rule_version": "q15-rti-v18-all-seven-exact-source-quality-v1",
        "available": not failures,
        "eligible": not failures,
        "decision": "SOURCE_COMPLETE" if not failures else "SOURCE_INCOMPLETE",
        "failures": failures,
        "evidence": evidence,
        "feature_evidence_sha256": _canonical_sha256(evidence),
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }


def _evaluate_row(
    row: Mapping[str, Any], *, require_low_reversal: bool,
) -> dict[str, Any]:
    """Return frozen prospective eligibility using decision-time fields only."""
    source = evaluate_source_row(row)
    evidence = dict(source["evidence"])
    asset = str(evidence["asset"])
    side = str(evidence["side"])
    failures = list(source["failures"])
    if asset not in NON_BTC_ASSETS:
        failures.append("NON_BTC_COHORT_REQUIRED")
    if evidence["strict_control_passed"] is not True:
        failures.append("STRICT_CONTROL_NOT_PASSED")
    if require_low_reversal and evidence["reversal_risk_class"] != "low":
        failures.append("REVERSAL_RISK_NOT_LOW")
    return {
        "design_id": identity.DESIGN_ID,
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "rule_version": (
            identity.RULE_VERSION
            if require_low_reversal
            else "q15-rti-frozen-strict-control-v3"
        ),
        "available": source["available"] is True and asset in NON_BTC_ASSETS,
        "eligible": not failures,
        "decision": side if not failures else "ABSTAIN",
        "failures": failures,
        "evidence": evidence,
        "feature_evidence_sha256": _canonical_sha256(evidence),
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }


def evaluate_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate the frozen V18 candidate (strict plus low reversal risk)."""
    return _evaluate_row(row, require_low_reversal=True)


def evaluate_strict_control_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate the unchanged strict control on the identical future source."""
    return _evaluate_row(row, require_low_reversal=False)
