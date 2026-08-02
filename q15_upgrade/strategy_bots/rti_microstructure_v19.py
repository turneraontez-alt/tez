"""Outcome-blind V19 low-reversal plus fresh-60s selector.

V19 is a silent prospective study.  It consumes the unchanged V18 parent and
the independently captured 12-minute confirmation row.  It cannot read an
outcome, fit, notify, promote, or trade.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

from . import rti_microstructure_v18 as v18
from . import rti_microstructure_v19_identity as identity
from tools.q15_rti_microstructure_preregister import design_fingerprint


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROTOCOL = ROOT / identity.PROTOCOL_RELATIVE_PATH
MAX_DATA_AGE_SECONDS = 3.0
MAX_TIMING_OFFSET_SECONDS = 2.0
TARGET_SECONDS_BEFORE_CLOSE = 720.0
EXPECTED_PATH_COUNT = 61
ALLOWED_QUOTE_AGE_SOURCES = frozenset({
    "kalshi_rest_snapshot_received_at",
    "kalshi_ws_exact_sampler",
})
ALLOWED_QUOTE_EVIDENCE_SOURCES = frozenset({
    "kalshi_official_rest_orderbook",
    "kalshi_official_websocket_book",
})


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


def _value(row: Mapping[str, Any], profile: Mapping[str, Any], key: str) -> Any:
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


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def load_protocol(path: Path = DEFAULT_PROTOCOL) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("v19_protocol_unreadable") from exc
    if not isinstance(value, Mapping):
        raise ValueError("v19_protocol_root_not_object")
    protocol = dict(value)
    amendment = dict(
        protocol.get("outcome_blind_source_identity_amendment") or {}
    )
    population = dict(protocol.get("population") or {})
    rule = dict(protocol.get("frozen_rule") or {})
    collection = dict(protocol.get("collection") or {})
    if (
        design_fingerprint(protocol) != identity.PROTOCOL_SHA256
        or protocol.get("protocol_id") != identity.PROTOCOL_ID
        or protocol.get("design_id") != identity.DESIGN_ID
        or protocol.get("protocol_status")
        != "FROZEN_BEFORE_ANY_V19_PROSPECTIVE_OUTCOME_ACCESS"
        or float(population.get("prospective_after_close_time") or 0.0)
        != identity.PROSPECTIVE_AFTER_CLOSE_TIME
        or float(population.get("first_eligible_close_time") or 0.0)
        != identity.FIRST_ELIGIBLE_CLOSE_TIME
        or population.get("btc_labels_forbidden") is not True
        or rule.get("rule_version") != identity.RULE_VERSION
        or rule.get("parent_v18_eligible_required") is not True
        or rule.get("official_rti_side_must_remain_same") is not True
        or rule.get("new_61_second_rti_path_required") is not True
        or rule.get("new_quote_required") is not True
        or rule.get("reused_13m_entry_quote_forbidden") is not True
        or rule.get("delayed_record_kind_required")
        != "RTI_PATH_12M_CONFIRM_PROSPECTIVE"
        or set(rule.get("allowed_quote_age_sources") or ())
        != ALLOWED_QUOTE_AGE_SOURCES
        or set(rule.get("allowed_quote_evidence_sources") or ())
        != ALLOWED_QUOTE_EVIDENCE_SOURCES
        or rule.get("evaluation_delay_must_match_evaluated_at_minus_target_at")
        is not True
        or float(rule.get("new_ask_max_cents") or 0.0) != 62.0
        or float(rule.get("new_spread_max_cents") or 0.0) != 1.5
        or float(rule.get("new_depth_min_contracts") or 0.0) != 10.0
        or int(rule.get("sim_contracts") or 0) != 10
        or rule.get("explicit_full_fill_support_required") is not True
        or rule.get("additional_threshold_tuning_allowed") is not False
        or collection.get("outcome_access_allowed_now") is not False
        or collection.get("model_fit_allowed") is not False
        or collection.get("probability_scoring_allowed") is not False
        or collection.get("paper_pick_artifact_allowed_now") is not False
        or collection.get("notifications_allowed_now") is not False
        or collection.get("telegram_allowed_now") is not False
        or collection.get("automatic_promotion_allowed") is not False
        or collection.get("real_trading_allowed") is not False
        or amendment.get("prospective_outcomes_or_resolution_status_inspected")
        is not False
        or amendment.get("entry_side_price_spread_depth_or_risk_threshold_changed")
        is not False
        or amendment.get("historical_credit_claimed") is not False
    ):
        raise ValueError("v19_protocol_identity_or_safety_invalid")
    return protocol


def evaluate_delayed_source(
    parent_row: Mapping[str, Any], delayed_row: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate lineage and 12m evidence without applying the V19 trade rule."""
    load_protocol()
    parent_source = v18.evaluate_source_row(parent_row)
    parent_evidence = dict(parent_source["evidence"])
    profile = _profile(delayed_row)
    close_time = _num(_value(delayed_row, profile, "close_time"))
    parent_close = _num(parent_evidence.get("close_time"))
    parent_id = int(_num(parent_evidence.get("id")) or 0)
    linked_parent_id = int(_num(profile.get("rti_confirm_original_row_id")) or 0)
    asset = str(_value(delayed_row, profile, "asset") or "").upper()
    ticker = str(_value(delayed_row, profile, "ticker") or "")
    side = str(_value(delayed_row, profile, "side") or "").upper()
    original_side = str(profile.get("rti_confirm_original_side") or "").upper()
    confirmation_side = str(profile.get("rti_confirm_side") or "").upper()
    target_at = _num(profile.get("rti_confirm_target_at"))
    quote_captured_at = _num(profile.get("rti_confirm_quote_captured_at"))
    evaluated_at = _num(profile.get("rti_confirm_evaluated_at"))
    timing_offset = _num(profile.get("rti_confirm_timing_offset_s"))
    evaluation_delay = _num(profile.get("rti_confirm_evaluation_delay_s"))
    delay_seconds = _num(profile.get("rti_confirm_delay_seconds"))
    path_count = _num(profile.get("rti_confirm_path_count"))
    expected_count = _num(profile.get("rti_confirm_path_expected_count"))
    path_complete = _flag(profile.get("rti_confirm_path_complete"))
    path_max_age = _num(profile.get("rti_confirm_path_max_receive_age_s"))
    path_decision_age = _num(profile.get("rti_confirm_path_decision_age_s"))
    quote_age = _num(_value(delayed_row, profile, "quote_age_seconds"))
    quote_age_source = str(
        _value(delayed_row, profile, "quote_age_source") or ""
    )
    quote_evidence_source = str(
        _value(delayed_row, profile, "quote_evidence_source") or ""
    )
    ask = _num(_value(delayed_row, profile, "entry_ask_cents"))
    spread = _num(_value(delayed_row, profile, "spread_cents"))
    depth = _num(_value(delayed_row, profile, "depth_contracts"))
    sim_contracts = _num(profile.get("sim_contracts"))
    sim_full_fill_supported = _flag(profile.get("sim_full_fill_supported"))
    expected_target = None if close_time is None else close_time - TARGET_SECONDS_BEFORE_CLOSE
    capture_gap = (
        None if quote_captured_at is None or parent_evidence.get("source_captured_at") is None
        else quote_captured_at - float(parent_evidence["source_captured_at"])
    )
    failures = []
    if parent_source["available"] is not True:
        failures.append("PARENT_SOURCE_INCOMPLETE")
    if close_time is None or close_time <= identity.PROSPECTIVE_AFTER_CLOSE_TIME:
        failures.append("STRICTLY_PROSPECTIVE_CLOSE_REQUIRED")
    if str(_value(delayed_row, profile, "bot_name") or "") != "rti_path_13m":
        failures.append("DELAYED_BOT_IDENTITY")
    if (
        str(_value(delayed_row, profile, "record_kind") or "").upper()
        != "RTI_PATH_12M_CONFIRM_PROSPECTIVE"
    ):
        failures.append("DELAYED_RECORD_KIND_IDENTITY")
    if str(_value(delayed_row, profile, "interval") or "").upper() != "12M":
        failures.append("DELAYED_INTERVAL_IDENTITY")
    if (
        asset != parent_evidence.get("asset")
        or ticker != parent_evidence.get("ticker")
        or close_time != parent_close
        or linked_parent_id != parent_id
    ):
        failures.append("PARENT_CONTRACT_IDENTITY")
    if delay_seconds != 60.0:
        failures.append("DELAY_IDENTITY")
    if expected_target is None or target_at is None or abs(target_at - expected_target) > 1e-6:
        failures.append("TARGET_TIMESTAMP_IDENTITY")
    if (
        quote_captured_at is None or target_at is None
        or quote_captured_at < target_at - 1e-6
        or quote_captured_at - target_at > MAX_TIMING_OFFSET_SECONDS
        or timing_offset is None
        or not -1e-6 <= timing_offset <= MAX_TIMING_OFFSET_SECONDS
        or abs(timing_offset - (quote_captured_at - target_at)) > 0.05
    ):
        failures.append("FRESH_60S_CAPTURE_TIMING")
    if (
        evaluated_at is None or quote_captured_at is None
        or evaluated_at < quote_captured_at - 1e-6
        or evaluated_at - quote_captured_at > MAX_TIMING_OFFSET_SECONDS
        or evaluation_delay is None
        or not -1e-6 <= evaluation_delay <= MAX_TIMING_OFFSET_SECONDS
        or target_at is None
        or abs(evaluation_delay - (evaluated_at - target_at)) > 0.05
    ):
        failures.append("FRESH_60S_EVALUATION_TIMING")
    if capture_gap is None or capture_gap < 58.0:
        failures.append("NEW_QUOTE_NOT_INDEPENDENT_OF_13M")
    if (
        path_complete is not True
        or path_count != float(EXPECTED_PATH_COUNT)
        or expected_count != float(EXPECTED_PATH_COUNT)
        or path_max_age is None
        or path_decision_age is None
        or not -1e-6 <= path_max_age <= MAX_DATA_AGE_SECONDS
        or not -1e-6 <= path_decision_age <= MAX_DATA_AGE_SECONDS
    ):
        failures.append("FRESH_61_SECOND_RTI_PATH")
    if quote_age is None or not -1e-6 <= quote_age <= MAX_DATA_AGE_SECONDS:
        failures.append("FRESH_NEW_QUOTE")
    if (
        quote_age_source not in ALLOWED_QUOTE_AGE_SOURCES
        or quote_evidence_source not in ALLOWED_QUOTE_EVIDENCE_SOURCES
    ):
        failures.append("OFFICIAL_NEW_QUOTE_SOURCE_IDENTITY")
    if side not in {"YES", "NO"} or original_side not in {"YES", "NO"}:
        failures.append("SIDE_IDENTITY_MISSING")
    if ask is None or spread is None or depth is None:
        failures.append("EXECUTABLE_NEW_BOOK_MISSING")
    if _flag(_value(delayed_row, profile, "paper_only")) is not True:
        failures.append("DELAYED_ROW_NOT_PAPER_ONLY")
    evidence = {
        "parent_id": parent_id,
        "delayed_id": int(delayed_row["id"]) if delayed_row.get("id") is not None else None,
        "asset": asset,
        "ticker": ticker,
        "close_time": close_time,
        "parent_side": str(parent_evidence.get("side") or ""),
        "delayed_side": side,
        "original_side": original_side,
        "confirmation_side": confirmation_side,
        "original_strict_accepted": _flag(
            profile.get("rti_confirm_original_strict_accepted")
        ) is True,
        "target_at": target_at,
        "quote_captured_at": quote_captured_at,
        "evaluated_at": evaluated_at,
        "timing_offset_seconds": timing_offset,
        "evaluation_delay_seconds": evaluation_delay,
        "capture_gap_from_parent_seconds": capture_gap,
        "path_complete": path_complete is True,
        "path_count": path_count,
        "path_expected_count": expected_count,
        "path_max_receive_age_seconds": path_max_age,
        "path_decision_age_seconds": path_decision_age,
        "quote_age_seconds": quote_age,
        "quote_age_source": quote_age_source,
        "quote_evidence_source": quote_evidence_source,
        "entry_ask_cents": ask,
        "spread_cents": spread,
        "depth_contracts": depth,
        "sim_contracts": sim_contracts,
        "sim_full_fill_supported": sim_full_fill_supported is True,
        "parent_feature_evidence_sha256": parent_source[
            "feature_evidence_sha256"
        ],
    }
    return {
        "available": not failures,
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


def evaluate_pair(
    parent_row: Mapping[str, Any], delayed_row: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply the frozen V19 rule to one lineage-validated parent/stage pair."""
    source = evaluate_delayed_source(parent_row, delayed_row)
    parent = v18.evaluate_row(parent_row)
    evidence = dict(source["evidence"])
    failures = list(source["failures"])
    if parent["eligible"] is not True:
        failures.append("PARENT_V18_NOT_ELIGIBLE")
    if evidence.get("original_strict_accepted") is not True:
        failures.append("ORIGINAL_STRICT_IDENTITY")
    parent_side = str(evidence.get("parent_side") or "")
    if (
        evidence.get("original_side") != parent_side
        or evidence.get("confirmation_side") != parent_side
        or evidence.get("delayed_side") != parent_side
    ):
        failures.append("OFFICIAL_RTI_SIDE_DID_NOT_REMAIN_SAME")
    ask = _num(evidence.get("entry_ask_cents"))
    spread = _num(evidence.get("spread_cents"))
    depth = _num(evidence.get("depth_contracts"))
    if ask is None or ask > 62.0:
        failures.append("NEW_ASK_MAX_62")
    if spread is None or not 0.0 <= spread <= 1.5:
        failures.append("NEW_SPREAD_0_TO_1_5")
    if depth is None or depth < 10.0:
        failures.append("NEW_DEPTH_SUPPORTS_10")
    if (
        evidence.get("sim_contracts") != 10.0
        or evidence.get("sim_full_fill_supported") is not True
    ):
        failures.append("NEW_BOOK_FULL_FILL_NOT_SUPPORTED")
    return {
        "design_id": identity.DESIGN_ID,
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "rule_version": identity.RULE_VERSION,
        "available": source["available"] is True,
        "eligible": not failures,
        "decision": parent_side if not failures else "ABSTAIN",
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
