"""Q15 V7 professional diagnostics, trade-liveness, and shadow-policy layer.

This module is deliberately read-only. It never creates, modifies, or submits
an exchange order. It measures the full decision funnel, separates raw polling
cycles from unique contract/checkpoint decisions, diagnoses zero-trade systems,
tracks near misses and state transitions, produces a shadow challenger, and
professionalizes hourly Telegram reports.

The shadow challenger is observational by default and cannot override the live
production decision. It exists to identify bugs, contradictory gates, and
threshold bottlenecks before any strategy behavior is changed.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from collections import Counter, OrderedDict, defaultdict, deque
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Mapping

VERSION = "q15-v7-professional-liveness"
READ_ONLY = True

_ENGINE_LOCK = threading.RLock()
_ENGINE: "ProfessionalV7Engine | None" = None

STAGES = (
    "contracts_discovered",
    "valid_settlement_metadata",
    "sufficient_underlying_data",
    "valid_kalshi_quotes",
    "usable_orderbook_data",
    "acceptable_data_freshness",
    "directional_prediction",
    "outside_neutral_zone",
    "model_disagreement_pass",
    "chart_quality_pass",
    "data_quality_pass",
    "confirmation_pass",
    "follow_through_pass",
    "spread_pass",
    "depth_pass",
    "executable_price_pass",
    "positive_gross_edge",
    "positive_net_edge",
    "conservative_edge_pass",
    "maximum_entry_pass",
    "watch_state",
    "armed_state",
    "entry_recommended",
)

ALLOWED_TRANSITIONS = {
    "OBSERVING": {"OBSERVING", "CANDIDATE", "WATCH", "DATA_STALE", "INVALID", "RESOLVED"},
    "CANDIDATE": {"CANDIDATE", "WATCH", "ARMED", "OBSERVING", "DATA_STALE", "INVALID", "RESOLVED"},
    "WATCH": {"WATCH", "ARMED", "CANDIDATE", "OBSERVING", "DATA_STALE", "INVALID", "RESOLVED"},
    "ARMED": {"ARMED", "ENTRY_RECOMMENDED", "WATCH", "DATA_STALE", "INVALID", "RESOLVED"},
    "ENTRY_RECOMMENDED": {"ENTRY_RECOMMENDED", "SIMULATED_POSITION", "HOLD", "EXIT_RECOMMENDED", "RESOLVED", "INVALID"},
    "SIMULATED_POSITION": {"SIMULATED_POSITION", "HOLD", "EXIT_RECOMMENDED", "RESOLVED", "INVALID"},
    "HOLD": {"HOLD", "EXIT_RECOMMENDED", "RESOLVED", "INVALID"},
    "EXIT_RECOMMENDED": {"EXIT_RECOMMENDED", "RESOLVED", "HOLD", "INVALID"},
    "DATA_STALE": {"DATA_STALE", "OBSERVING", "CANDIDATE", "WATCH", "INVALID", "RESOLVED"},
    "INVALID": {"INVALID", "OBSERVING", "RESOLVED"},
    "RESOLVED": {"RESOLVED"},
}

REASON_LABELS = {
    "no_directional_candidate": "No stable directional candidate",
    "probability_unavailable": "Directional probability unavailable",
    "two_prediction_focus_gate": "15M and 10M predictions did not produce a valid consensus",
    "executable_ask_unavailable": "Executable ask unavailable",
    "spread_too_wide": "Spread exceeds the allowed maximum",
    "thin_executable_depth": "Executable order-book depth is insufficient",
    "data_unreliable": "Market data is stale, incomplete, or invalid",
    "calibrated_edge_gate": "Conservative executable edge gate failed",
    "model_disagreement_high": "Model disagreement is too high",
    "orderbook_not_confirmed": "Order-book pressure lacks persistence or independent confirmation",
    "waiting_for_next_candle_follow_through": "Waiting for the next candle to confirm follow-through",
    "confirmation_candle_crossed_setup_extreme": "Confirmation candle already crossed the setup extreme",
    "next_candle_did_not_continue": "The next candle did not continue the setup",
    "market_not_live": "Market is not live",
    "overpriced": "Executable contract price exceeds estimated value",
    "reversal_risk": "Reversal risk is elevated",
    "shadow_only": "Shadow challenger only; production is unchanged",
}


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return bool(default)
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float) -> float:
    parsed = _num(os.getenv(name), default)
    return float(default if parsed is None else parsed)


def _int_env(name: str, default: int) -> int:
    parsed = _num(os.getenv(name), default)
    return int(default if parsed is None else parsed)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def normalize_probability(value: Any) -> float | None:
    """Return a probability in [0, 1], accepting decimal or percentage input."""
    parsed = _num(value)
    if parsed is None:
        return None
    if 1.0 < parsed <= 100.0:
        parsed /= 100.0
    if not 0.0 <= parsed <= 1.0:
        return None
    return float(parsed)


def normalize_cents(value: Any) -> float | None:
    """Return a contract price in cents, accepting 0-1 or 0-100 input."""
    parsed = _num(value)
    if parsed is None:
        return None
    if 0.0 <= parsed <= 1.0:
        parsed *= 100.0
    if not 0.0 <= parsed <= 100.0:
        return None
    return float(parsed)


def normalize_edge_cents(value: Any) -> float | None:
    """Return an edge in cents without guessing percentages above one."""
    parsed = _num(value)
    if parsed is None:
        return None
    return float(parsed)


def _nested(snapshot: Mapping[str, Any], dotted: str) -> Any:
    current: Any = snapshot
    for part in dotted.split("."):
        current = current.get(part) if isinstance(current, Mapping) else None
    return current


def _first(snapshot: Mapping[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        value = _nested(snapshot, name) if "." in name else snapshot.get(name)
        if value is not None:
            return value
    return None


def _first_num(snapshot: Mapping[str, Any], names: Iterable[str]) -> float | None:
    return _num(_first(snapshot, names))


def _first_prob(snapshot: Mapping[str, Any], names: Iterable[str]) -> float | None:
    return normalize_probability(_first(snapshot, names))


def _side(snapshot: Mapping[str, Any]) -> str | None:
    for key in (
        "calibrated_edge_side",
        "q15_final_result",
        "q15_final_side",
        "q15_consensus_side",
        "entry_side",
        "predicted_result",
        "end_prediction.predicted_result",
    ):
        value = _nested(snapshot, key) if "." in key else snapshot.get(key)
        token = str(value or "").strip().upper()
        if token in {"YES", "NO"}:
            return token
    direction = str(snapshot.get("pattern_direction") or "").strip().lower()
    if direction == "bullish":
        return "YES"
    if direction == "bearish":
        return "NO"
    return None


def _side_quotes(snapshot: Mapping[str, Any], side: str | None) -> tuple[float | None, float | None]:
    yes_bid = normalize_cents(snapshot.get("yes_bid"))
    yes_ask = normalize_cents(snapshot.get("yes_ask"))
    no_bid = normalize_cents(snapshot.get("no_bid"))
    no_ask = normalize_cents(snapshot.get("no_ask"))
    if no_bid is None and yes_ask is not None:
        no_bid = 100.0 - yes_ask
    if no_ask is None and yes_bid is not None:
        no_ask = 100.0 - yes_bid
    return (yes_bid, yes_ask) if side == "YES" else (no_bid, no_ask) if side == "NO" else (None, None)


def _contract_id(snapshot: Mapping[str, Any], asset: str) -> str:
    value = _first(snapshot, ("contract_id", "ticker", "market_ticker", "event_ticker", "market_id"))
    return str(value or asset or "unknown")


def _checkpoint(snapshot: Mapping[str, Any]) -> str:
    explicit = str(_first(snapshot, ("checkpoint", "q15_checkpoint", "prediction_checkpoint", "window_label")) or "").upper()
    if "15" in explicit:
        return "15M"
    if "10" in explicit:
        return "10M"
    seconds = _first_num(snapshot, ("seconds_remaining", "time_remaining_seconds"))
    if seconds is None:
        return "LIVE"
    if 720 <= seconds <= 930:
        return "15M"
    if 540 <= seconds <= 660:
        return "10M"
    return "LIVE"


def _model_version(snapshot: Mapping[str, Any]) -> str:
    return str(_first(snapshot, (
        "calibrated_edge_version",
        "decision_version",
        "model_version",
        "policy_version",
    )) or "unknown")


def _data_quality(snapshot: Mapping[str, Any]) -> float | None:
    value = _first_prob(snapshot, (
        "model_data_quality",
        "model_data_quality_score",
        "data_quality",
        "data_quality_score",
        "v5_data_quality",
    ))
    return value


def _chart_quality(snapshot: Mapping[str, Any]) -> float | None:
    return _first_prob(snapshot, (
        "chart_quality",
        "chart_quality_score",
        "end_prediction.chart_quality",
        "end_prediction.quality",
    ))


def _independent_count(snapshot: Mapping[str, Any]) -> int:
    direct = _first_num(snapshot, ("independent_group_count", "confirmation_group_count", "groups_confirmed"))
    if direct is not None:
        return max(0, int(direct))
    raw = snapshot.get("confirmation_groups") or snapshot.get("confirmations") or []
    if isinstance(raw, Mapping):
        return sum(1 for value in raw.values() if value)
    if isinstance(raw, (list, tuple, set)):
        return len(raw)
    return 0


def _blockers(snapshot: Mapping[str, Any]) -> list[str]:
    values: list[Any] = []
    for key in ("entry_blockers", "calibrated_edge_blockers", "rejection_reasons", "blockers"):
        raw = snapshot.get(key)
        if isinstance(raw, str):
            values.extend(part.strip() for part in raw.split(",") if part.strip())
        elif isinstance(raw, (list, tuple, set)):
            values.extend(raw)
    output: list[str] = []
    for value in values:
        token = str(value or "").strip()
        if token and token not in output:
            output.append(token)
    return output


def humanize_reason(reason: Any) -> str:
    token = str(reason or "").strip()
    if token in REASON_LABELS:
        return REASON_LABELS[token]
    edge_match = re.fullmatch(r"edge_below_required_?([0-9.]+)c?", token)
    if edge_match:
        return f"Conservative edge is below the required +{float(edge_match.group(1)):.1f}¢"
    return token.replace("_", " ").strip().capitalize()


def _state(snapshot: Mapping[str, Any]) -> str:
    token = str(_first(snapshot, ("decision_state", "entry_status", "state")) or "").upper()
    if snapshot.get("market_state") in {"resolved", "settled", "closed"} or snapshot.get("resolved") is True:
        return "RESOLVED"
    if snapshot.get("v5_data_valid") is False or snapshot.get("data_stale") is True:
        return "DATA_STALE"
    if "EXIT" in token:
        return "EXIT_RECOMMENDED"
    if "HOLD" in token:
        return "HOLD"
    if "ENTRY CONFIRMED" in token or "SIMULATED" in token:
        return "SIMULATED_POSITION"
    if "ENTRY" in token or snapshot.get("calibrated_edge_new_entry_allowed") is True:
        return "ENTRY_RECOMMENDED"
    if "ARMED" in token:
        return "ARMED"
    if "WATCH" in token:
        return "WATCH"
    if "CANDIDATE" in token:
        return "CANDIDATE"
    if "INVALID" in token:
        return "INVALID"
    return "OBSERVING"


def _threshold_distance(snapshot: Mapping[str, Any]) -> dict[str, float | None]:
    side = _side(snapshot)
    _, ask = _side_quotes(snapshot, side)
    spread = _first_num(snapshot, ("spread", "spread_cents"))
    depth = _first_num(snapshot, ("orderbook_microstructure.executable_depth", "executable_depth", "ask_depth"))
    edge = _first_num(snapshot, ("conservative_edge_after_costs_cents", "trade_quality.conservative_edge_after_costs_cents"))
    required = _first_num(snapshot, ("trade_quality.required_edge_cents", "required_edge", "required_edge_cents"))
    max_entry = _first_num(snapshot, ("calibrated_max_entry_price", "trade_quality.maximum_entry_cents", "maximum_entry_cents"))
    min_depth = _float_env("Q15_MIN_EXECUTABLE_DEPTH", 5.0)
    max_spread = _float_env("Q15_CALIBRATED_MAX_SPREAD_CENTS", 3.0)
    min_groups = _int_env("Q15_MIN_CONFIRMATION_GROUPS", 3)
    groups = _independent_count(snapshot)
    return {
        "edge_shortfall_cents": None if edge is None or required is None else round(required - edge, 4),
        "spread_excess_cents": None if spread is None else round(spread - max_spread, 4),
        "depth_shortfall": None if depth is None else round(min_depth - depth, 4),
        "confirmation_group_shortfall": round(float(min_groups - groups), 4),
        "entry_price_excess_cents": None if ask is None or max_entry is None else round(ask - max_entry, 4),
    }


def _sanity_issues(snapshot: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    for name in ("raw_side_probability", "conservative_side_probability", "market_implied_side_probability"):
        raw = snapshot.get(name)
        if raw is not None and normalize_probability(raw) is None:
            issues.append(f"invalid_probability:{name}")
    for name in ("yes_bid", "yes_ask", "no_bid", "no_ask", "calibrated_max_entry_price"):
        raw = snapshot.get(name)
        if raw is not None and normalize_cents(raw) is None:
            issues.append(f"invalid_cents:{name}")
    yes_bid = normalize_cents(snapshot.get("yes_bid"))
    yes_ask = normalize_cents(snapshot.get("yes_ask"))
    if yes_bid is not None and yes_ask is not None and yes_bid > yes_ask:
        issues.append("crossed_yes_book")
    no_bid = normalize_cents(snapshot.get("no_bid"))
    no_ask = normalize_cents(snapshot.get("no_ask"))
    if no_bid is not None and no_ask is not None and no_bid > no_ask:
        issues.append("crossed_no_book")
    if yes_ask is not None and no_bid is not None and abs((yes_ask + no_bid) - 100.0) > 2.1:
        issues.append("yes_no_quote_conversion_inconsistent")
    return issues


def _stage_evaluation(snapshot: Mapping[str, Any], asset: str) -> tuple[list[str], dict[str, str]]:
    passed: list[str] = ["contracts_discovered"]
    failures: dict[str, str] = {}
    side = _side(snapshot)
    contract = _contract_id(snapshot, asset)
    seconds = _first_num(snapshot, ("seconds_remaining", "time_remaining_seconds"))
    raw_p = _first_prob(snapshot, ("raw_side_probability", "trade_quality.raw_side_probability"))
    conservative_p = _first_prob(snapshot, ("conservative_side_probability", "trade_quality.conservative_side_probability"))
    bid, ask = _side_quotes(snapshot, side)
    spread = _first_num(snapshot, ("spread", "spread_cents"))
    if spread is None and bid is not None and ask is not None:
        spread = ask - bid
    depth = _first_num(snapshot, ("orderbook_microstructure.executable_depth", "executable_depth", "ask_depth"))
    disagreement = str(snapshot.get("model_disagreement") or "UNKNOWN").upper()
    chart_quality = _chart_quality(snapshot)
    data_quality = _data_quality(snapshot)
    groups = _independent_count(snapshot)
    book_persistent = bool(_first(snapshot, ("orderbook_microstructure.book_persistent", "book_persistent")))
    flow_skew = _first_num(snapshot, ("orderbook_microstructure.flow_skew_side", "flow_skew_side")) or 0.0
    gross_edge = None if raw_p is None or ask is None else raw_p * 100.0 - ask
    point_edge = _first_num(snapshot, ("point_edge_after_costs_cents", "trade_quality.point_edge_after_costs_cents"))
    conservative_edge = _first_num(snapshot, ("conservative_edge_after_costs_cents", "trade_quality.conservative_edge_after_costs_cents"))
    required = _first_num(snapshot, ("trade_quality.required_edge_cents", "required_edge", "required_edge_cents"))
    if required is None:
        required = _float_env("Q15_CONSERVATIVE_MIN_EDGE_CENTS", 4.0)
    max_entry = _first_num(snapshot, ("calibrated_max_entry_price", "trade_quality.maximum_entry_cents", "maximum_entry_cents"))
    blockers = _blockers(snapshot)

    def check(stage: str, condition: bool, reason: str) -> None:
        if condition:
            passed.append(stage)
        else:
            failures[stage] = reason

    check("valid_settlement_metadata", bool(contract and contract != "unknown" and seconds is not None), "missing contract identifier or remaining time")
    underlying_available = any(snapshot.get(key) is not None for key in (
        "underlying_current", "underlying_price", "spot_price", "target_distance_pct", "candles", "ohlcv"
    ))
    check("sufficient_underlying_data", underlying_available, "underlying price/candle data unavailable")
    check("valid_kalshi_quotes", bid is not None and ask is not None, "selected-side bid/ask unavailable")
    check("usable_orderbook_data", depth is not None or groups > 0 or book_persistent, "order-book depth/confirmation unavailable")
    data_fresh = snapshot.get("v5_data_valid") is not False and snapshot.get("data_stale") is not True and snapshot.get("market_state") in {None, "live", "open"}
    check("acceptable_data_freshness", data_fresh, "market data stale, invalid, or market not live")
    check("directional_prediction", side in {"YES", "NO"}, "no selected YES/NO side")
    min_directional = _float_env("Q15_V7_MIN_DIRECTIONAL_PROBABILITY", 0.55)
    check("outside_neutral_zone", raw_p is not None and raw_p >= min_directional, f"side probability below {min_directional:.0%}")
    check("model_disagreement_pass", disagreement != "HIGH", "high model disagreement")
    min_chart = _float_env("Q15_V7_MIN_CHART_QUALITY", 0.0)
    check("chart_quality_pass", chart_quality is None or chart_quality >= min_chart, "chart quality below configured floor")
    min_data = _float_env("Q15_V7_MIN_DATA_QUALITY", 0.50)
    check("data_quality_pass", data_quality is None or data_quality >= min_data, "data quality below configured floor")
    min_groups = _int_env("Q15_MIN_CONFIRMATION_GROUPS", 3)
    confirmation_ok = groups >= min_groups or book_persistent or abs(flow_skew) >= 0.20
    check("confirmation_pass", confirmation_ok, "insufficient independent confirmation")
    follow_blockers = {
        "waiting_for_next_candle_follow_through",
        "confirmation_candle_crossed_setup_extreme",
        "next_candle_did_not_continue",
    }
    check("follow_through_pass", not any(reason in follow_blockers for reason in blockers), "follow-through gate failed")
    max_spread = _float_env("Q15_CALIBRATED_MAX_SPREAD_CENTS", 3.0)
    check("spread_pass", spread is not None and spread <= max_spread, f"spread above {max_spread:.1f}¢ or unavailable")
    min_depth = _float_env("Q15_MIN_EXECUTABLE_DEPTH", 5.0)
    check("depth_pass", depth is not None and depth >= min_depth, f"depth below {min_depth:g} contracts or unavailable")
    check("executable_price_pass", ask is not None, "executable ask unavailable")
    check("positive_gross_edge", gross_edge is not None and gross_edge > 0.0, "gross edge is non-positive or unavailable")
    check("positive_net_edge", point_edge is not None and point_edge > 0.0, "point-estimate net edge is non-positive or unavailable")
    check("conservative_edge_pass", conservative_edge is not None and conservative_edge >= required, f"conservative edge below +{required:.1f}¢")
    check("maximum_entry_pass", ask is not None and max_entry is not None and ask <= max_entry, "ask exceeds maximum acceptable entry")
    state = _state(snapshot)
    check("watch_state", state in {"WATCH", "ARMED", "ENTRY_RECOMMENDED", "SIMULATED_POSITION", "HOLD", "EXIT_RECOMMENDED"}, "candidate never reached WATCH")
    check("armed_state", state in {"ARMED", "ENTRY_RECOMMENDED", "SIMULATED_POSITION", "HOLD", "EXIT_RECOMMENDED"}, "candidate never reached ARMED")
    live_entry = snapshot.get("calibrated_edge_new_entry_allowed") is True or state in {"ENTRY_RECOMMENDED", "SIMULATED_POSITION", "HOLD", "EXIT_RECOMMENDED"}
    check("entry_recommended", live_entry, "production entry was not recommended")
    return passed, failures


def _coefficient_rewrite(line: str) -> str:
    match = re.search(r"coefficient\s*([+-]?\d+(?:\.\d+)?)", line, flags=re.I)
    if not match:
        return line
    value = float(match.group(1))
    if value > 0:
        suffix = " — associated with higher observed loss risk"
    elif value < 0:
        suffix = " — associated with lower observed loss risk"
    else:
        suffix = " — no observed directional association"
    return line if suffix in line else line.rstrip() + suffix


class ProfessionalV7Engine:
    """Measure trade liveness and maintain a non-live shadow challenger."""

    def __init__(self, store=None, notifier=None, config=None, learner=None):
        self.store = store
        self.notifier = notifier
        self.config = config
        self.learner = learner
        self.enabled = _bool_env("Q15_V7_DIAGNOSTICS_ENABLED", True)
        self.shadow_enabled = _bool_env("Q15_V7_SHADOW_CHALLENGER", True)
        self.apply_challenger = False  # hard-coded safety: this module never changes live decisions
        self.max_records = max(100, _int_env("Q15_V7_MAX_DECISION_RECORDS", 5000))
        self.zero_trade_min_decisions = max(2, _int_env("Q15_V7_ZERO_TRADE_MIN_DECISIONS", 20))
        self.shadow_base_edge = max(0.0, _float_env("Q15_V7_SHADOW_BASE_EDGE_CENTS", 4.0))
        self.shadow_max_spread = max(0.1, _float_env("Q15_V7_SHADOW_MAX_SPREAD_CENTS", 3.0))
        self.shadow_min_depth = max(0.0, _float_env("Q15_V7_SHADOW_MIN_DEPTH", 5.0))
        self.shadow_min_groups = max(1, _int_env("Q15_V7_SHADOW_MIN_GROUPS", 2))
        self.persist = _bool_env("Q15_V7_DIAGNOSTIC_JOURNAL", True)
        self.journal_path = Path(os.getenv("Q15_V7_DIAGNOSTIC_JOURNAL_PATH", "data/q15_v7_diagnostic_events.jsonl"))
        self._lock = threading.RLock()
        self._records: OrderedDict[str, dict] = OrderedDict()
        self._latest_by_asset: dict[str, dict] = {}
        self._raw_cycles = 0
        self._raw_asset_snapshots = 0
        self._unique_contracts: set[str] = set()
        self._states: dict[str, str] = {}
        self._transitions: deque[dict] = deque(maxlen=500)
        self._invalid_transitions: deque[dict] = deque(maxlen=200)
        self._journaled_hashes: dict[str, str] = {}
        self._started_at = time.time()
        self._last_observed_at = 0.0
        self._self_test = self.run_liveness_self_test()
        with _ENGINE_LOCK:
            global _ENGINE
            _ENGINE = self

    def _decision_key(self, asset: str, snapshot: Mapping[str, Any]) -> str:
        return "|".join((
            _contract_id(snapshot, asset),
            _checkpoint(snapshot),
            _model_version(snapshot),
        ))

    def _shadow_policy(self, snapshot: Mapping[str, Any]) -> dict:
        side = _side(snapshot)
        bid, ask = _side_quotes(snapshot, side)
        conservative_edge = _first_num(snapshot, ("conservative_edge_after_costs_cents", "trade_quality.conservative_edge_after_costs_cents"))
        spread = _first_num(snapshot, ("spread", "spread_cents"))
        if spread is None and bid is not None and ask is not None:
            spread = ask - bid
        depth = _first_num(snapshot, ("orderbook_microstructure.executable_depth", "executable_depth", "ask_depth"))
        groups = _independent_count(snapshot)
        persistent = bool(_first(snapshot, ("orderbook_microstructure.book_persistent", "book_persistent")))
        flow_skew = _first_num(snapshot, ("orderbook_microstructure.flow_skew_side", "flow_skew_side")) or 0.0
        disagreement = str(snapshot.get("model_disagreement") or "UNKNOWN").upper()
        q15_agreement = str(snapshot.get("q15_prediction_agreement") or "").upper()
        q15_final = str(snapshot.get("q15_final_result") or "").upper()
        seconds = _first_num(snapshot, ("seconds_remaining", "time_remaining_seconds"))
        after_10m = seconds is not None and seconds <= 610
        penalty = _first_num(snapshot, ("calibration_evidence.sampling_penalty_pp", "calibration_evidence.data_quality_penalty_pp")) or 0.0
        adaptive_edge = self.shadow_base_edge + max(0.0, (spread or 0.0) - 1.0) * 0.25 + min(2.0, penalty * 0.10)
        blockers: list[str] = []
        if side not in {"YES", "NO"}:
            blockers.append("no_directional_candidate")
        if after_10m and q15_agreement and q15_agreement != "SAME":
            blockers.append("two_prediction_focus_gate")
        if after_10m and q15_final == "UNCERTAIN":
            blockers.append("two_prediction_focus_gate")
        if snapshot.get("q15_new_entry_allowed") is False:
            blockers.append("two_prediction_focus_gate")
        if snapshot.get("v5_data_valid") is False or snapshot.get("data_stale") is True:
            blockers.append("data_unreliable")
        if ask is None:
            blockers.append("executable_ask_unavailable")
        if spread is None or spread > self.shadow_max_spread:
            blockers.append("spread_too_wide")
        if depth is None or depth < self.shadow_min_depth:
            blockers.append("thin_executable_depth")
        if conservative_edge is None or conservative_edge < adaptive_edge:
            blockers.append(f"edge_below_required_{adaptive_edge:.1f}c")
        confirmation_ok = groups >= self.shadow_min_groups and (persistent or abs(flow_skew) >= 0.15 or groups >= 3)
        if not confirmation_ok:
            blockers.append("orderbook_not_confirmed")
        if disagreement == "HIGH" and not (
            conservative_edge is not None and conservative_edge >= adaptive_edge + 3.0 and groups >= 3
        ):
            blockers.append("model_disagreement_high")
        blockers = list(dict.fromkeys(blockers))
        return {
            "version": VERSION,
            "ready": not blockers,
            "decision": "ENTRY_RECOMMENDED" if not blockers else "NO_TRADE",
            "side": side,
            "adaptive_required_edge_cents": round(adaptive_edge, 4),
            "conservative_edge_cents": conservative_edge,
            "executable_ask_cents": ask,
            "spread_cents": spread,
            "depth": depth,
            "independent_groups": groups,
            "book_persistent": persistent,
            "flow_skew_side": round(flow_skew, 4),
            "blockers": blockers,
            "blockers_human": [humanize_reason(item) for item in blockers],
            "shadow_only": True,
            "production_changed": False,
        }

    def _record(self, asset: str, snapshot: Mapping[str, Any], now: float) -> dict:
        key = self._decision_key(asset, snapshot)
        stages, failures = _stage_evaluation(snapshot, asset)
        contract = _contract_id(snapshot, asset)
        state = _state(snapshot)
        previous_state = self._states.get(contract)
        transition_valid = True
        if previous_state and state != previous_state:
            transition_valid = state in ALLOWED_TRANSITIONS.get(previous_state, {state})
            transition = {
                "at": now,
                "contract_id": contract,
                "asset": asset,
                "from": previous_state,
                "to": state,
                "valid": transition_valid,
            }
            self._transitions.append(transition)
            if not transition_valid:
                self._invalid_transitions.append(transition)
        self._states[contract] = state
        production_ready = snapshot.get("calibrated_edge_new_entry_allowed") is True or state == "ENTRY_RECOMMENDED"
        shadow = self._shadow_policy(snapshot) if self.shadow_enabled else {
            "ready": False,
            "decision": "DISABLED",
            "blockers": ["shadow_only"],
            "blockers_human": [humanize_reason("shadow_only")],
            "shadow_only": True,
            "production_changed": False,
        }
        distances = _threshold_distance(snapshot)
        blockers = _blockers(snapshot)
        sanity = _sanity_issues(snapshot)
        record = {
            "decision_key": key,
            "contract_id": contract,
            "asset": asset,
            "checkpoint": _checkpoint(snapshot),
            "model_version": _model_version(snapshot),
            "first_seen_at": self._records.get(key, {}).get("first_seen_at", now),
            "updated_at": now,
            "seconds_remaining": _first_num(snapshot, ("seconds_remaining", "time_remaining_seconds")),
            "state": state,
            "previous_state": previous_state,
            "transition_valid": transition_valid,
            "side": _side(snapshot),
            "raw_side_probability": _first_prob(snapshot, ("raw_side_probability", "trade_quality.raw_side_probability")),
            "conservative_side_probability": _first_prob(snapshot, ("conservative_side_probability", "trade_quality.conservative_side_probability")),
            "market_implied_side_probability": _first_prob(snapshot, ("market_implied_side_probability", "trade_quality.market_implied_side_probability")),
            "executable_ask_cents": _side_quotes(snapshot, _side(snapshot))[1],
            "gross_edge_cents": None,
            "point_edge_cents": _first_num(snapshot, ("point_edge_after_costs_cents", "trade_quality.point_edge_after_costs_cents")),
            "conservative_edge_cents": _first_num(snapshot, ("conservative_edge_after_costs_cents", "trade_quality.conservative_edge_after_costs_cents")),
            "required_edge_cents": _first_num(snapshot, ("trade_quality.required_edge_cents", "required_edge", "required_edge_cents")),
            "spread_cents": _first_num(snapshot, ("spread", "spread_cents")),
            "depth": _first_num(snapshot, ("orderbook_microstructure.executable_depth", "executable_depth", "ask_depth")),
            "independent_groups": _independent_count(snapshot),
            "model_disagreement": str(snapshot.get("model_disagreement") or "UNKNOWN").upper(),
            "production_ready": bool(production_ready),
            "shadow": shadow,
            "decision_changed_by_shadow": bool(shadow.get("ready")) != bool(production_ready),
            "passed_stages": stages,
            "failed_stages": failures,
            "blockers": blockers,
            "blockers_human": [humanize_reason(item) for item in blockers],
            "threshold_distance": distances,
            "sanity_issues": sanity,
            "paper_only": True,
        }
        raw_p = record["raw_side_probability"]
        ask = record["executable_ask_cents"]
        if raw_p is not None and ask is not None:
            record["gross_edge_cents"] = round(raw_p * 100.0 - ask, 4)
        return record

    def _journal(self, record: Mapping[str, Any]) -> None:
        if not self.persist:
            return
        key = str(record.get("decision_key"))
        fingerprint = json.dumps({
            "state": record.get("state"),
            "production_ready": record.get("production_ready"),
            "shadow_ready": (record.get("shadow") or {}).get("ready"),
            "blockers": record.get("blockers"),
            "failed_stages": record.get("failed_stages"),
        }, sort_keys=True, default=str)
        if self._journaled_hashes.get(key) == fingerprint:
            return
        self._journaled_hashes[key] = fingerprint
        try:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            if self.journal_path.exists() and self.journal_path.stat().st_size > 20_000_000:
                rotated = self.journal_path.with_suffix(f".{int(time.time())}.jsonl")
                self.journal_path.replace(rotated)
            with self.journal_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(dict(record), sort_keys=True, default=str) + "\n")
        except OSError:
            # Diagnostics must never crash the market-monitoring loop.
            pass

    def observe_all(self, snapshots: dict, now: float, ws_health: dict | None = None) -> dict:
        if not self.enabled:
            return snapshots
        with self._lock:
            self._raw_cycles += 1
            self._last_observed_at = now
            for asset, snapshot in (snapshots or {}).items():
                if not isinstance(snapshot, Mapping):
                    continue
                self._raw_asset_snapshots += 1
                normalized_asset = str(snapshot.get("asset") or asset or "UNKNOWN").upper()
                contract = _contract_id(snapshot, normalized_asset)
                self._unique_contracts.add(contract)
                record = self._record(normalized_asset, snapshot, now)
                key = record["decision_key"]
                self._records[key] = record
                self._records.move_to_end(key)
                while len(self._records) > self.max_records:
                    self._records.popitem(last=False)
                self._latest_by_asset[normalized_asset] = deepcopy(record)
                self._journal(record)
                # Add observational fields only; never override live decisions.
                if isinstance(snapshot, dict):
                    snapshot["q15_v7_version"] = VERSION
                    snapshot["q15_v7_shadow_decision"] = record["shadow"]["decision"]
                    snapshot["q15_v7_shadow_ready"] = record["shadow"]["ready"]
                    snapshot["q15_v7_shadow_blockers"] = record["shadow"]["blockers"]
                    snapshot["q15_v7_decision_key"] = key
                    snapshot["q15_v7_transition_valid"] = record["transition_valid"]
                    snapshot["q15_v7_sanity_issues"] = record["sanity_issues"]
        return snapshots

    def _funnel(self, records: list[dict]) -> list[dict]:
        output: list[dict] = []
        prior_count = len(records)
        for index, stage in enumerate(STAGES):
            required_prefix = set(STAGES[: index + 1])
            count = sum(
                1 for record in records
                if required_prefix.issubset(set(record.get("passed_stages", [])))
            )
            denominator = len(records) if index == 0 else prior_count
            output.append({
                "stage": stage,
                "passed": count,
                "entered_stage": denominator,
                "pass_rate": None if denominator <= 0 else round(count / denominator, 4),
                "overall_rate": None if not records else round(count / len(records), 4),
            })
            prior_count = count
        return output

    def _top_blockers(self, records: list[dict], limit: int = 12) -> list[dict]:
        counter: Counter[str] = Counter()
        assets: dict[str, Counter[str]] = defaultdict(Counter)
        checkpoints: dict[str, Counter[str]] = defaultdict(Counter)
        for record in records:
            for blocker in record.get("blockers", []):
                counter[blocker] += 1
                assets[blocker][record.get("asset") or "UNKNOWN"] += 1
                checkpoints[blocker][record.get("checkpoint") or "UNKNOWN"] += 1
            for stage, reason in record.get("failed_stages", {}).items():
                synthetic = f"stage:{stage}"
                counter[synthetic] += 1
                assets[synthetic][record.get("asset") or "UNKNOWN"] += 1
                checkpoints[synthetic][record.get("checkpoint") or "UNKNOWN"] += 1
        total = max(1, len(records))
        return [
            {
                "reason": reason,
                "human": humanize_reason(reason.replace("stage:", "")) if not reason.startswith("stage:") else reason.replace("stage:", "").replace("_", " ").title(),
                "count": count,
                "decision_rate": round(count / total, 4),
                "top_assets": assets[reason].most_common(3),
                "top_checkpoints": checkpoints[reason].most_common(3),
            }
            for reason, count in counter.most_common(limit)
        ]

    def near_misses(self, limit: int = 20) -> list[dict]:
        with self._lock:
            records = [deepcopy(value) for value in self._records.values() if not value.get("production_ready")]
        scored: list[tuple[float, dict]] = []
        for record in records:
            distances = record.get("threshold_distance") or {}
            score = 0.0
            known = 0
            for value in distances.values():
                if value is None:
                    continue
                known += 1
                score += max(0.0, float(value))
            score += len(record.get("blockers") or []) * 2.0
            score += len(record.get("failed_stages") or {}) * 0.25
            if known:
                scored.append((score, record))
        scored.sort(key=lambda item: item[0])
        return [dict(record, near_miss_score=round(score, 4)) for score, record in scored[:max(1, limit)]]

    def gate_ablation(self) -> list[dict]:
        with self._lock:
            records = [deepcopy(value) for value in self._records.values()]
        blocker_sets = [set(record.get("blockers") or []) for record in records if not record.get("production_ready")]
        all_blockers = Counter(blocker for blockers in blocker_sets for blocker in blockers)
        output: list[dict] = []
        for blocker, count in all_blockers.most_common():
            additional = sum(1 for blockers in blocker_sets if blockers == {blocker})
            output.append({
                "gate": blocker,
                "human": humanize_reason(blocker),
                "blocked_decisions": count,
                "additional_entries_if_only_gate_removed": additional,
                "outcome_performance_available": False,
                "warning": "Candidate count is not evidence of profitability; validate on resolved out-of-sample contracts.",
            })
        return output

    def threshold_sensitivity(self) -> dict:
        with self._lock:
            records = [deepcopy(value) for value in self._records.values()]
        edge_grid = [0.0, 2.0, 4.0, 6.0, 8.0]
        spread_grid = [1.0, 2.0, 3.0, 4.0]
        edge_rows = []
        for threshold in edge_grid:
            eligible = sum(
                1 for record in records
                if record.get("conservative_edge_cents") is not None
                and float(record["conservative_edge_cents"]) >= threshold
            )
            edge_rows.append({"threshold_cents": threshold, "eligible_decisions": eligible})
        spread_rows = []
        for threshold in spread_grid:
            eligible = sum(
                1 for record in records
                if record.get("spread_cents") is not None
                and float(record["spread_cents"]) <= threshold
            )
            spread_rows.append({"maximum_spread_cents": threshold, "eligible_decisions": eligible})
        return {
            "edge_threshold_grid": edge_rows,
            "spread_threshold_grid": spread_rows,
            "resolved_outcome_metrics_available": False,
            "warning": "This live diagnostic only measures candidate flow. Use historical resolved records for expectancy, calibration, and drawdown.",
        }

    def liveness_diagnosis(self) -> dict:
        with self._lock:
            records = [deepcopy(value) for value in self._records.values()]
        production_entries = sum(1 for row in records if row.get("production_ready"))
        shadow_entries = sum(1 for row in records if (row.get("shadow") or {}).get("ready"))
        top = self._top_blockers(records, 5)
        enough = len(records) >= self.zero_trade_min_decisions
        if production_entries > 0:
            status = "LIVE_ENTRY_PATH_OBSERVED"
            conclusion = "The production system has demonstrated that it can recommend an entry."
        elif not enough:
            status = "INSUFFICIENT_OBSERVATIONS"
            conclusion = "There are not yet enough unique contract/checkpoint decisions to classify zero trades as a defect."
        elif shadow_entries > 0:
            status = "PRODUCTION_OVERCONSTRAINED_OR_BLOCKED"
            conclusion = "The shadow challenger found qualifying opportunities while production found none; inspect the dominant production gates and state transitions."
        else:
            status = "NO_EXECUTABLE_EDGE_OBSERVED_OR_SHARED_DATA_DEFECT"
            conclusion = "Neither production nor the conservative shadow challenger found a valid entry. This may be legitimate abstention or a shared data/probability defect."
        return {
            "status": status,
            "conclusion": conclusion,
            "unique_decisions": len(records),
            "minimum_decisions_before_warning": self.zero_trade_min_decisions,
            "production_entries": production_entries,
            "shadow_entries": shadow_entries,
            "top_bottlenecks": top,
            "synthetic_liveness_test": deepcopy(self._self_test),
            "apply_challenger": False,
            "read_only": True,
        }

    def run_liveness_self_test(self) -> dict:
        base = {
            "asset": "ETH",
            "ticker": "SYNTHETIC-YES",
            "market_state": "live",
            "seconds_remaining": 590,
            "q15_prediction_agreement": "SAME",
            "q15_final_result": "YES",
            "q15_new_entry_allowed": True,
            "calibrated_edge_side": "YES",
            "conservative_edge_after_costs_cents": 13.0,
            "yes_bid": 60.0,
            "yes_ask": 61.0,
            "no_bid": 39.0,
            "no_ask": 40.0,
            "spread": 1.0,
            "orderbook_microstructure": {
                "executable_depth": 20.0,
                "book_persistent": True,
                "flow_skew_side": 0.40,
            },
            "independent_group_count": 3,
            "model_disagreement": "LOW",
            "v5_data_valid": True,
        }
        yes = self._shadow_policy(base)
        no_case = dict(base)
        no_case.update({
            "ticker": "SYNTHETIC-NO",
            "q15_final_result": "NO",
            "calibrated_edge_side": "NO",
            "yes_bid": 39.0,
            "yes_ask": 40.0,
            "no_bid": 60.0,
            "no_ask": 61.0,
        })
        no_case["orderbook_microstructure"] = dict(base["orderbook_microstructure"])
        no_case["orderbook_microstructure"]["flow_skew_side"] = 0.35
        no_result = self._shadow_policy(no_case)
        negative = dict(base)
        negative["ticker"] = "SYNTHETIC-NEGATIVE"
        negative["conservative_edge_after_costs_cents"] = -2.0
        negative_result = self._shadow_policy(negative)
        return {
            "yes_case": {"passed": bool(yes.get("ready")), "result": yes},
            "no_case": {"passed": bool(no_result.get("ready")), "result": no_result},
            "negative_edge_case": {"passed": not bool(negative_result.get("ready")), "result": negative_result},
            "all_passed": bool(yes.get("ready") and no_result.get("ready") and not negative_result.get("ready")),
        }

    def hourly_report(self) -> str:
        summary = self.summary()
        diagnosis = summary["liveness"]
        funnel = summary["decision_funnel"]
        last_stage = next((row for row in funnel if row["stage"] == "entry_recommended"), None)
        bottleneck = (diagnosis.get("top_bottlenecks") or [{}])[0]
        lines = [
            "📊 <b>Q15 V7 OPERATIONAL REPORT</b>",
            f"Runtime: {summary['runtime_seconds'] / 3600.0:.1f}h | raw cycles: {summary['raw_cycles']}",
            f"Unique contracts: {summary['unique_contracts']} | unique checkpoint decisions: {summary['unique_decisions']}",
            f"Production entries: {diagnosis['production_entries']} | shadow-ready: {diagnosis['shadow_entries']}",
            f"Trade-liveness status: <b>{diagnosis['status']}</b>",
        ]
        if bottleneck:
            lines.append(f"Main bottleneck: {bottleneck.get('human')} ({bottleneck.get('count', 0)} decisions)")
        if last_stage:
            lines.append(f"Decision funnel reaching entry: {last_stage.get('passed', 0)}/{last_stage.get('entered_stage', 0)}")
        lines.extend([
            "Shadow challenger is observational only; production decisions are unchanged.",
            "Probabilistic paper monitor — no orders are placed.",
        ])
        return "\n".join(lines)

    def summary(self) -> dict:
        with self._lock:
            records = [deepcopy(value) for value in self._records.values()]
            latest = deepcopy(self._latest_by_asset)
            transitions = list(deepcopy(self._transitions))
            invalid = list(deepcopy(self._invalid_transitions))
            raw_cycles = self._raw_cycles
            raw_asset_snapshots = self._raw_asset_snapshots
            unique_contracts = len(self._unique_contracts)
            last_observed = self._last_observed_at
        production_entries = sum(1 for row in records if row.get("production_ready"))
        shadow_entries = sum(1 for row in records if (row.get("shadow") or {}).get("ready"))
        return {
            "version": VERSION,
            "enabled": self.enabled,
            "read_only": True,
            "shadow_only": True,
            "production_behavior_changed": False,
            "runtime_seconds": round(time.time() - self._started_at, 3),
            "last_observed_at": last_observed,
            "raw_cycles": raw_cycles,
            "raw_asset_snapshots": raw_asset_snapshots,
            "unique_contracts": unique_contracts,
            "unique_decisions": len(records),
            "production_entries": production_entries,
            "shadow_entries": shadow_entries,
            "decision_funnel": self._funnel(records),
            "top_blockers": self._top_blockers(records),
            "liveness": self.liveness_diagnosis(),
            "state_transitions": transitions[-50:],
            "invalid_state_transitions": invalid[-50:],
            "near_misses": self.near_misses(10),
            "gate_ablation": self.gate_ablation(),
            "threshold_sensitivity": self.threshold_sensitivity(),
            "latest_by_asset": latest,
            "journal_path": str(self.journal_path),
            "journal_enabled": self.persist,
        }

    def health(self) -> dict:
        diagnosis = self.liveness_diagnosis()
        return {
            "version": VERSION,
            "enabled": self.enabled,
            "read_only": True,
            "shadow_only": True,
            "raw_cycles": self._raw_cycles,
            "unique_contracts": len(self._unique_contracts),
            "unique_decisions": len(self._records),
            "production_entries": diagnosis["production_entries"],
            "shadow_entries": diagnosis["shadow_entries"],
            "liveness_status": diagnosis["status"],
            "synthetic_entry_path_passed": bool(self._self_test.get("all_passed")),
        }


def professionalize_telegram_message(text: Any) -> str:
    """Correct hourly-report wording and append zero-trade diagnostics."""
    message = str(text or "")
    if "HOURLY REPORT" not in message.upper():
        return message
    message = message.replace(
        "Factors associated with higher loss risk:",
        "Outcome associations (direction depends on target coding):",
    )
    message = message.replace(
        "Learning adjustments active:",
        "Shadow adjustments under evaluation — not applied to live alerts:",
    )
    message = message.replace("[SHADOW SUPPRESS]", "[SHADOW ONLY — production unchanged]")
    lines = [_coefficient_rewrite(line) if "coefficient" in line.lower() else line for line in message.splitlines()]
    message = "\n".join(lines)
    with _ENGINE_LOCK:
        engine = _ENGINE
    if engine is None:
        return message
    diagnosis = engine.liveness_diagnosis()
    no_resolutions = bool(re.search(r"Last hour:\s*0W/0L.*?\(n=0\)", message, flags=re.I))
    compact_empty = _bool_env("Q15_V7_COMPACT_EMPTY_HOURLY", True)
    if no_resolutions and compact_empty:
        overall = next((line for line in lines if line.strip().lower().startswith("overall:")), None)
        realized = next((line for line in lines if line.strip().lower().startswith("realized:")), None)
        compact = [
            "📊 <b>Hourly operational status</b>",
            "No new resolved contracts this hour.",
        ]
        if overall:
            compact.append(overall)
        if realized:
            compact.append(realized)
        compact.extend([
            f"Unique contracts: {len(engine._unique_contracts)} | unique checkpoint decisions: {len(engine._records)}",
            f"Production entries: {diagnosis['production_entries']} | shadow-ready: {diagnosis['shadow_entries']}",
            f"Trade-liveness: <b>{diagnosis['status']}</b>",
        ])
        top = diagnosis.get("top_bottlenecks") or []
        if top:
            compact.append(f"Main bottleneck: {top[0].get('human')} ({top[0].get('count', 0)})")
        compact.extend([
            "Shadow learning is observational; no live thresholds were changed.",
            "Read-only monitor — no orders are placed.",
        ])
        return "\n".join(compact)[:4000]

    appendix = [
        "",
        "<b>V7 TRADE-LIVENESS AUDIT</b>",
        f"Unique checkpoint decisions: {diagnosis['unique_decisions']}",
        f"Production entries: {diagnosis['production_entries']} | shadow-ready: {diagnosis['shadow_entries']}",
        f"Status: <b>{diagnosis['status']}</b>",
    ]
    top = diagnosis.get("top_bottlenecks") or []
    if top:
        appendix.append(f"Main bottleneck: {top[0].get('human')} ({top[0].get('count', 0)})")
    appendix.append("Shadow challenger is not permitted to suppress or create production entries.")
    combined = message + "\n" + "\n".join(appendix)
    return combined if len(combined) <= 4000 else combined[:3990] + "…"
