"""Q15 V9 canonical probability and executable-edge layer.

Read-only: this module never submits, modifies, or cancels an exchange order.
It makes the Kalshi midpoint a single baseline, applies an independently-built
residual once, and exposes one canonical economics calculation everywhere.
"""
from __future__ import annotations

import math
import os
import re
import threading
import time
from collections import defaultdict, deque
from copy import deepcopy
from typing import Any, Iterable, Mapping

VERSION = "q15-v9-edge-proof-alert-reliability"
ECONOMICS_VERSION = "q15-v9-canonical-economics"
PROBABILITY_VERSION = "q15-v9-market-baseline-plus-independent-residual"
DISCLAIMER = "Probabilistic paper estimate only; it can be wrong. Read-only monitor — no orders are placed."

_LATEST_LOCK = threading.RLock()
_LATEST_BY_ASSET: dict[str, dict[str, Any]] = {}


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _env_float(name: str, default: float) -> float:
    return float(_num(os.getenv(name), default))


def _env_int(name: str, default: int) -> int:
    return int(_num(os.getenv(name), default))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _prob(value: Any) -> float | None:
    number = _num(value)
    if number is None:
        return None
    if 1.0 < number <= 100.0:
        number /= 100.0
    if not 0.0 <= number <= 1.0:
        return None
    return _clamp(number, 0.001, 0.999)


def _logit(probability: float) -> float:
    p = _clamp(probability, 0.001, 0.999)
    return math.log(p / (1.0 - p))


def _sigmoid(value: float) -> float:
    if value >= 0:
        exp_neg = math.exp(-min(value, 60.0))
        return 1.0 / (1.0 + exp_neg)
    exp_pos = math.exp(max(value, -60.0))
    return exp_pos / (1.0 + exp_pos)


def _nested(mapping: Mapping[str, Any], path: str) -> Any:
    current: Any = mapping
    for part in path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _first(mapping: Mapping[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = _nested(mapping, key) if "." in key else mapping.get(key)
        if value is not None:
            return value
    return None


def _first_prob(mapping: Mapping[str, Any], keys: Iterable[str]) -> float | None:
    for key in keys:
        value = _prob(_nested(mapping, key) if "." in key else mapping.get(key))
        if value is not None:
            return value
    return None


def _first_num(mapping: Mapping[str, Any], keys: Iterable[str]) -> float | None:
    for key in keys:
        value = _num(_nested(mapping, key) if "." in key else mapping.get(key))
        if value is not None:
            return value
    return None


def _normalize_price_cents(value: Any) -> float | None:
    number = _num(value)
    if number is None:
        return None
    if 0.0 <= number <= 1.0:
        number *= 100.0
    if not 0.0 <= number <= 100.0:
        return None
    return number


def _side_quotes(snapshot: Mapping[str, Any], side: str) -> tuple[float | None, float | None]:
    yes_bid = _normalize_price_cents(snapshot.get("yes_bid"))
    yes_ask = _normalize_price_cents(snapshot.get("yes_ask"))
    no_bid = _normalize_price_cents(snapshot.get("no_bid"))
    no_ask = _normalize_price_cents(snapshot.get("no_ask"))
    if no_bid is None and yes_ask is not None:
        no_bid = 100.0 - yes_ask
    if no_ask is None and yes_bid is not None:
        no_ask = 100.0 - yes_bid
    if yes_bid is None and no_ask is not None:
        yes_bid = 100.0 - no_ask
    if yes_ask is None and no_bid is not None:
        yes_ask = 100.0 - no_bid
    return (yes_bid, yes_ask) if side == "YES" else (no_bid, no_ask)


def market_baseline_probability(snapshot: Mapping[str, Any]) -> float | None:
    """Return the executable market's YES midpoint, used exactly once."""
    bid = _normalize_price_cents(snapshot.get("yes_bid"))
    ask = _normalize_price_cents(snapshot.get("yes_ask"))
    if bid is not None and ask is not None and bid <= ask:
        return _clamp((bid + ask) / 200.0, 0.001, 0.999)
    if ask is not None:
        return _clamp(ask / 100.0, 0.001, 0.999)
    if bid is not None:
        return _clamp(bid / 100.0, 0.001, 0.999)
    return None


def independent_signal_probability(snapshot: Mapping[str, Any]) -> float | None:
    """Choose a market-independent YES estimate.

    Blended/end-market fields are intentionally excluded.  `estimated_fair_probability`
    is accepted only because the installer removes the market anchor from model.py.
    """
    return _first_prob(snapshot, (
        "probability_model_raw_yes",
        "raw_model_yes_probability",
        "model_raw_probability_yes",
        "raw_probability_yes",
        "model_estimate.raw_probability",
        "end_prediction.chart_yes_probability",
        "end_prediction.raw_yes_probability",
        "chart_yes_probability",
        "end_prediction_yes_probability_raw",
        "estimated_fair_probability",
    ))


def _microstructure_residual(snapshot: Mapping[str, Any]) -> float:
    """Small bounded logit residual from independent flow/book evidence."""
    flow_yes = _first_num(snapshot, ("taker_yes_volume_15s", "aggressive_yes_volume")) or 0.0
    flow_no = _first_num(snapshot, ("taker_no_volume_15s", "aggressive_no_volume")) or 0.0
    flow_total = max(0.0, flow_yes + flow_no)
    flow_skew = (flow_yes - flow_no) / flow_total if flow_total > 0 else 0.0

    book = _first_num(snapshot, (
        "orderbook_imbalance",
        "orderbook_microstructure.imbalance_yes",
        "book_imbalance_yes",
    )) or 0.0
    if abs(book) > 1.0 and abs(book) <= 100.0:
        book /= 100.0
    book = _clamp(book, -1.0, 1.0)

    persistence = _first_num(snapshot, (
        "orderbook_persistence",
        "orderbook_microstructure.persistence_ratio",
    ))
    if persistence is None:
        persistent = bool(_first(snapshot, ("book_persistent", "orderbook_microstructure.book_persistent")))
        persistence = 1.0 if persistent else 0.0
    persistence = _clamp(float(persistence), 0.0, 1.0)

    max_abs = max(0.0, _env_float("Q15_V9_MICROSTRUCTURE_MAX_LOGIT", 0.30))
    residual = 0.18 * _clamp(flow_skew, -1.0, 1.0) + 0.12 * book * persistence
    return _clamp(residual, -max_abs, max_abs)


def canonical_probability(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Build one baseline-plus-residual probability pipeline."""
    market = market_baseline_probability(snapshot)
    independent = independent_signal_probability(snapshot)
    residual_weight = _clamp(_env_float("Q15_V9_INDEPENDENT_RESIDUAL_WEIGHT", 0.35), 0.0, 1.0)
    residual = 0.0
    if independent is not None:
        residual += residual_weight * _logit(independent)
    residual += _microstructure_residual(snapshot)

    if market is not None:
        uncalibrated = _sigmoid(_logit(market) + residual)
        method = "market baseline + independent residual"
    elif independent is not None:
        uncalibrated = independent
        method = "independent model only; market baseline unavailable"
    else:
        return {
            "available": False,
            "reason": "market_and_independent_probability_unavailable",
            "market_baseline_p_yes": market,
            "independent_signal_p_yes": independent,
            "residual_logit_correction": None,
            "uncalibrated_p_yes": None,
            "calibrated_p_yes": None,
            "method": "unavailable",
        }

    # Existing calibration is not trusted automatically. It can be applied only
    # when explicitly enabled and accompanied by a known calibration object.
    calibrated = uncalibrated
    calibration_applied = False
    if _env_bool("Q15_V9_APPLY_EXISTING_CALIBRATION", False):
        calibration = _first(snapshot, ("v9_calibration", "learning_assessment.v9_calibration"))
        if isinstance(calibration, Mapping):
            temperature = _num(calibration.get("temperature"), 1.0) or 1.0
            bias = _num(calibration.get("bias"), 0.0) or 0.0
            if 0.25 <= temperature <= 4.0:
                calibrated = _sigmoid(_logit(uncalibrated) / temperature + bias)
                calibration_applied = True

    return {
        "available": True,
        "reason": None,
        "market_baseline_p_yes": market,
        "independent_signal_p_yes": independent,
        "residual_logit_correction": round(residual, 8),
        "uncalibrated_p_yes": _clamp(uncalibrated, 0.001, 0.999),
        "calibrated_p_yes": _clamp(calibrated, 0.001, 0.999),
        "calibration_applied": calibration_applied,
        "method": method,
        "probability_version": PROBABILITY_VERSION,
    }


def _explicit_side(snapshot: Mapping[str, Any]) -> str | None:
    seconds = _first_num(snapshot, ("seconds_remaining", "time_remaining_seconds"))
    if seconds is not None and seconds <= 610:
        keys = (
            "q15_10m_prediction.side", "q15_10m_side", "prediction_10m.side",
            "q15_final_side", "q15_final_result", "q15_consensus_side", "entry_side",
        )
    else:
        keys = (
            "q15_15m_prediction.side", "q15_15m_side", "prediction_15m.side",
            "q15_consensus_side", "entry_side", "q15_final_side", "q15_final_result",
        )
    for key in keys:
        value = _nested(snapshot, key) if "." in key else snapshot.get(key)
        side = str(value or "").upper().strip()
        if side in {"YES", "NO"}:
            return side
    direction = str(snapshot.get("pattern_direction") or "").lower()
    return "YES" if direction == "bullish" else "NO" if direction == "bearish" else None


def directional_vote_metrics(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Separate agreement among directional votes from evidence coverage."""
    yes_votes = int(_first_num(snapshot, ("yes_votes", "rolling_yes_votes", "direction_votes.yes")) or 0)
    no_votes = int(_first_num(snapshot, ("no_votes", "rolling_no_votes", "direction_votes.no")) or 0)
    neutral_votes = int(_first_num(snapshot, ("neutral_votes", "rolling_neutral_votes", "direction_votes.neutral")) or 0)
    total_possible = int(_first_num(snapshot, ("total_possible_votes", "rolling_vote_count")) or 0)
    if total_possible <= 0:
        total_possible = yes_votes + no_votes + neutral_votes
    directional = yes_votes + no_votes
    agreement = max(yes_votes, no_votes) / directional if directional else 0.0
    coverage = directional / total_possible if total_possible else 0.0
    minimum = max(1, _env_int("Q15_V9_MIN_DIRECTIONAL_VOTES", 3))
    status = "OK" if directional >= minimum else "INSUFFICIENT_DIRECTIONAL_EVIDENCE"
    return {
        "yes_votes": yes_votes,
        "no_votes": no_votes,
        "neutral_votes": neutral_votes,
        "directional_votes": directional,
        "total_possible_votes": total_possible,
        "directional_agreement": round(agreement, 6),
        "evidence_coverage": round(coverage, 6),
        "status": status,
    }


def _fee_cents(price_cents: float, contracts: float = 1.0) -> float:
    p = _clamp(price_cents / 100.0, 0.0, 1.0)
    rate = max(0.0, _env_float("Q15_ESTIMATED_FEE_RATE", 0.07))
    return max(0.0, rate * contracts * p * (1.0 - p) * 100.0 / max(contracts, 1.0))


def calculate_trade_economics(
    *,
    side: str,
    calibrated_probability: float,
    conservative_probability: float,
    executable_entry_ask: float,
    estimated_fee: float,
    estimated_slippage: float,
    estimated_market_impact: float,
    required_edge_cents: float = 0.0,
) -> dict[str, Any]:
    """Canonical, side-aware edge calculation in cents per contract.

    The executable ask already contains the spread. Spread is therefore not
    deducted a second time.
    """
    if side not in {"YES", "NO"}:
        raise ValueError("side must be YES or NO")
    p = _prob(calibrated_probability)
    conservative = _prob(conservative_probability)
    ask = _normalize_price_cents(executable_entry_ask)
    if p is None or conservative is None or ask is None:
        raise ValueError("invalid probability or executable ask")
    fee = max(0.0, float(_num(estimated_fee, 0.0) or 0.0))
    slippage = max(0.0, float(_num(estimated_slippage, 0.0) or 0.0))
    impact = max(0.0, float(_num(estimated_market_impact, 0.0) or 0.0))
    total_cost = fee + slippage + impact
    gross = p * 100.0 - ask
    net = gross - total_cost
    conservative_net = conservative * 100.0 - ask - total_cost
    maximum_entry = conservative * 100.0 - total_cost - max(0.0, required_edge_cents)
    maximum_entry = _clamp(maximum_entry, 0.0, 100.0)
    ideal_high = max(0.0, maximum_entry - 1.0)
    ideal_low = max(0.0, ideal_high - 3.0)
    return {
        "economics_version": ECONOMICS_VERSION,
        "side": side,
        "calibrated_side_probability": round(p, 8),
        "conservative_side_probability": round(conservative, 8),
        "executable_entry_ask_cents": round(ask, 6),
        "gross_edge_cents": round(gross, 6),
        "estimated_fee_cents": round(fee, 6),
        "estimated_slippage_cents": round(slippage, 6),
        "estimated_market_impact_cents": round(impact, 6),
        "total_estimated_cost_cents": round(total_cost, 6),
        "net_edge_cents": round(net, 6),
        "conservative_net_edge_cents": round(conservative_net, 6),
        "maximum_entry_price_cents": round(maximum_entry, 6),
        "ideal_entry_low_cents": round(ideal_low, 6),
        "ideal_entry_high_cents": round(ideal_high, 6),
        "spread_deducted_separately": False,
    }


def _best_depth(snapshot: Mapping[str, Any], side: str) -> float:
    return float(_first_num(snapshot, (
        f"{side.lower()}_ask_qty",
        "orderbook_microstructure.executable_depth",
        "executable_depth",
        "ask_depth",
    )) or 0.0)


def _book_impact(snapshot: Mapping[str, Any], side: str, ask: float) -> float:
    levels = snapshot.get(f"{side.lower()}_ask_levels") or []
    quantity = max(1.0, _env_float("Q15_EXECUTION_SIM_CONTRACTS", 5.0))
    remaining = quantity
    cost = 0.0
    filled = 0.0
    parsed: list[tuple[float, float]] = []
    for level in levels:
        if isinstance(level, Mapping):
            price = _normalize_price_cents(level.get("price", level.get("price_cents")))
            qty = _num(level.get("quantity", level.get("qty", level.get("count"))))
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            price = _normalize_price_cents(level[0])
            qty = _num(level[1])
        else:
            continue
        if price is not None and qty is not None and qty > 0:
            parsed.append((price, qty))
    for price, qty in sorted(parsed):
        take = min(remaining, qty)
        cost += take * price
        filled += take
        remaining -= take
        if remaining <= 1e-9:
            break
    if filled <= 0:
        return 0.0
    vwap = cost / filled
    return max(0.0, vwap - ask)


def _checkpoint(snapshot: Mapping[str, Any]) -> str:
    seconds = _first_num(snapshot, ("seconds_remaining", "time_remaining_seconds"))
    if seconds is None:
        return "UNKNOWN"
    return "10M" if seconds <= 610 else "15M"


def _agreement(snapshot: Mapping[str, Any]) -> str:
    return str(_first(snapshot, (
        "q15_prediction_agreement", "agreement_15m_10m", "prediction_agreement"
    )) or "UNKNOWN").upper()


def _human_reason(code: str) -> str:
    mapping = {
        "direction_uncertain": "The directional model has not established a valid YES or NO thesis.",
        "insufficient_directional_evidence": "Too few directional votes are active; neutral votes are reported separately.",
        "price_not_attractive": "The executable ask is above the model's maximum acceptable entry.",
        "negative_conservative_edge": "Conservative expected value is below the required edge after all estimated costs.",
        "wait_for_confirmation": "Independent confirmation is not yet strong enough.",
        "wait_for_follow_through": "Waiting for completed-candle follow-through.",
        "data_stale": "Critical market data is stale or incomplete.",
        "prediction_conflict": "The saved 15-minute and current 10-minute predictions conflict.",
        "spread_too_wide": "The market spread exceeds the configured limit.",
        "insufficient_depth": "Executable depth is below the configured minimum.",
        "probability_unavailable": "A canonical probability could not be calculated.",
    }
    return mapping.get(code, code.replace("_", " ").capitalize())


def humanize_blockers(blockers: Iterable[str]) -> list[str]:
    return [_human_reason(str(item)) for item in blockers]


class CalibratedEdgeEngine:
    """Backward-compatible V6 class name implementing V9 semantics."""

    def __init__(self, store=None, notifier=None, config=None, learner=None):
        self.store = store
        self.notifier = notifier
        self.config = config
        self.learner = learner
        self.enabled = _env_bool("Q15_V9_ENABLED", True)
        self.gate_enabled = _env_bool("Q15_V9_ENTRY_GATE", True)
        self.required_edge = max(0.0, _env_float("Q15_V9_MIN_CONSERVATIVE_EDGE_CENTS", 4.0))
        self.uncertainty_penalty_pp = _clamp(_env_float("Q15_V9_CONSERVATIVE_PENALTY_PP", 6.0), 0.0, 25.0)
        self.max_spread = max(0.1, _env_float("Q15_V9_MAX_SPREAD_CENTS", 3.0))
        self.min_depth = max(0.0, _env_float("Q15_V9_MIN_EXECUTABLE_DEPTH", 5.0))
        self._lock = threading.RLock()
        self._latest: dict[str, dict[str, Any]] = {}
        self._cycles = 0

    def preview_all(self, snapshots: Mapping[str, dict], now: float, ws_health=None) -> dict[str, dict]:
        """Inject canonical probability before the two-window focus module runs."""
        output: dict[str, dict] = {}
        for asset, original in snapshots.items():
            if not isinstance(original, dict):
                continue
            snap = dict(original)
            probability = canonical_probability(snap)
            snap.update({
                "v9_probability_version": PROBABILITY_VERSION,
                "v9_market_baseline_p_yes": probability.get("market_baseline_p_yes"),
                "v9_independent_signal_p_yes": probability.get("independent_signal_p_yes"),
                "v9_residual_logit_correction": probability.get("residual_logit_correction"),
                "v9_uncalibrated_p_yes": probability.get("uncalibrated_p_yes"),
                "v9_calibrated_p_yes": probability.get("calibrated_p_yes"),
            })
            output[asset] = snap
        return output

    def enrich_all(self, snapshots: Mapping[str, dict], now: float, ws_health=None) -> dict[str, dict]:
        output: dict[str, dict] = {}
        for asset, original in snapshots.items():
            if not isinstance(original, dict):
                continue
            try:
                output[asset] = self._enrich_one(asset, original, now, ws_health or {})
            except Exception as exc:
                snap = dict(original)
                snap.update({
                    "calibrated_edge_version": VERSION,
                    "calibrated_edge_status": "INVALID_MARKET",
                    "calibrated_edge_entry_allowed": False,
                    "calibrated_edge_blockers": [f"v9_internal_error:{type(exc).__name__}"],
                })
                output[asset] = snap
        self._cycles += 1
        return output

    def _enrich_one(self, asset: str, original: Mapping[str, Any], now: float, ws_health: Mapping[str, Any]) -> dict:
        snap = deepcopy(dict(original))
        probability = canonical_probability(snap)
        side = _explicit_side(snap)
        votes = directional_vote_metrics(snap)
        checkpoint = _checkpoint(snap)
        blockers: list[str] = []

        data_age = _first_num(snap, ("data_age_seconds", "market_data_age_seconds"))
        stale_limit = max(1.0, _env_float("Q15_V9_MAX_DATA_AGE_SECONDS", 8.0))
        stale = data_age is not None and data_age > stale_limit
        if stale:
            blockers.append("data_stale")
        if side is None:
            blockers.append("direction_uncertain")
        if votes["total_possible_votes"] > 0 and votes["status"] != "OK":
            blockers.append("insufficient_directional_evidence")
        if not probability.get("available"):
            blockers.append("probability_unavailable")

        agreement = _agreement(snap)
        # Only the 10M checkpoint may require comparison with a saved 15M call.
        if checkpoint == "10M" and agreement in {"CONFLICT", "DISAGREE", "OPPOSITE"}:
            blockers.append("prediction_conflict")

        canonical_economics: dict[str, Any] | None = None
        status = "DIRECTION_UNCERTAIN" if side is None else "INSUFFICIENT_EVIDENCE"
        if side in {"YES", "NO"} and probability.get("calibrated_p_yes") is not None:
            p_yes = float(probability["calibrated_p_yes"])
            p_side = p_yes if side == "YES" else 1.0 - p_yes
            conservative_side = max(0.001, p_side - self.uncertainty_penalty_pp / 100.0)
            bid, ask = _side_quotes(snap, side)
            spread = None if bid is None or ask is None else max(0.0, ask - bid)
            depth = _best_depth(snap, side)
            if ask is None:
                blockers.append("price_not_attractive")
            else:
                fee = _fee_cents(ask)
                impact = _book_impact(snap, side, ask)
                # Ask already embeds spread. Slippage models latency/uncertainty only.
                slippage = max(0.10, _env_float("Q15_V9_MIN_SLIPPAGE_CENTS", 0.25), impact)
                canonical_economics = calculate_trade_economics(
                    side=side,
                    calibrated_probability=p_side,
                    conservative_probability=conservative_side,
                    executable_entry_ask=ask,
                    estimated_fee=fee,
                    estimated_slippage=slippage,
                    estimated_market_impact=impact,
                    required_edge_cents=self.required_edge,
                )
                if spread is not None and spread > self.max_spread:
                    blockers.append("spread_too_wide")
                if depth < self.min_depth:
                    blockers.append("insufficient_depth")
                if canonical_economics["conservative_net_edge_cents"] < self.required_edge:
                    blockers.append("negative_conservative_edge")
                if ask > canonical_economics["maximum_entry_price_cents"] + 1e-9:
                    blockers.append("price_not_attractive")

            confirmation_count = int(_first_num(snap, ("confirmation_group_count", "confirmation_count")) or 0)
            follow_through = bool(_first(snap, ("follow_through", "followthrough_valid", "confirmation_follow_through")))
            if confirmation_count and confirmation_count < 2:
                blockers.append("wait_for_confirmation")
            if _first(snap, ("waiting_for_next_candle_follow_through",)) is True:
                blockers.append("wait_for_follow_through")
            elif "waiting_for_next_candle_follow_through" in [str(x) for x in (snap.get("entry_blockers") or [])]:
                blockers.append("wait_for_follow_through")

            unique_blockers = list(dict.fromkeys(blockers))
            blockers = unique_blockers
            if stale:
                status = "DATA_STALE"
            elif "prediction_conflict" in blockers:
                status = "DIRECTION_UNCERTAIN"
            elif "insufficient_directional_evidence" in blockers:
                status = "INSUFFICIENT_EVIDENCE"
            elif "wait_for_confirmation" in blockers:
                status = "WAIT_FOR_CONFIRMATION"
            elif "wait_for_follow_through" in blockers:
                status = "WAIT_FOR_FOLLOW_THROUGH"
            elif any(item in blockers for item in ("negative_conservative_edge", "price_not_attractive", "spread_too_wide", "insufficient_depth")):
                status = "PRICE_NOT_ATTRACTIVE"
            elif not blockers:
                status = "READY"
            else:
                status = "INSUFFICIENT_EVIDENCE"

            market_yes = probability.get("market_baseline_p_yes")
            market_side = None if market_yes is None else (market_yes if side == "YES" else 1.0 - market_yes)
            snap.update({
                "calibrated_edge_side": side,
                "raw_side_probability": round(p_side, 8),
                "calibrated_side_probability": round(p_side, 8),
                "conservative_side_probability": round(conservative_side, 8),
                "market_implied_side_probability": None if market_side is None else round(market_side, 8),
                "calibrated_p_yes": round(p_yes, 8),
            })

        entry_allowed = bool(status == "READY" and self.gate_enabled)
        snap.update({
            "calibrated_edge_version": VERSION,
            "calibrated_edge_enabled": self.enabled,
            "calibrated_edge_side": side,
            "calibrated_edge_status": status,
            "trade_quality_status": status,
            "calibrated_edge_entry_allowed": entry_allowed,
            "calibrated_edge_new_entry_allowed": entry_allowed,
            "calibrated_edge_blockers": blockers,
            "calibrated_edge_blockers_human": humanize_blockers(blockers),
            "v9_checkpoint": checkpoint,
            "v9_prediction_agreement": agreement,
            "v9_directional_votes": votes,
            "v9_probability_version": PROBABILITY_VERSION,
            "v9_market_baseline_p_yes": probability.get("market_baseline_p_yes"),
            "v9_independent_signal_p_yes": probability.get("independent_signal_p_yes"),
            "v9_residual_logit_correction": probability.get("residual_logit_correction"),
            "v9_uncalibrated_p_yes": probability.get("uncalibrated_p_yes"),
            "v9_calibrated_p_yes": probability.get("calibrated_p_yes"),
            "v9_calibration_applied": probability.get("calibration_applied", False),
            "v9_out_of_sample_edge_proven": False,
            "economics_version": ECONOMICS_VERSION,
        })
        if canonical_economics is not None:
            snap["canonical_economics"] = canonical_economics
            snap["trade_quality"] = {
                **canonical_economics,
                "status": status,
                "blockers": blockers,
                "required_edge_cents": self.required_edge,
                "raw_side_probability": snap.get("raw_side_probability"),
                "conservative_side_probability": snap.get("conservative_side_probability"),
                "market_implied_side_probability": snap.get("market_implied_side_probability"),
                "calibrated_p_yes": snap.get("calibrated_p_yes"),
            }
            # Canonical backward-compatible aliases. `estimated_edge` now always
            # includes fee, slippage, and market impact.
            snap.update({
                "point_edge_after_costs_cents": canonical_economics["net_edge_cents"],
                "conservative_edge_after_costs_cents": canonical_economics["conservative_net_edge_cents"],
                "net_edge_cents": canonical_economics["net_edge_cents"],
                "estimated_edge": canonical_economics["net_edge_cents"],
                "estimated_fair_probability": probability.get("calibrated_p_yes"),
                "win_prob": snap.get("calibrated_side_probability"),
                "entry_ask": canonical_economics["executable_entry_ask_cents"],
                "entry_fee": canonical_economics["estimated_fee_cents"],
                "fee_adjusted_break_even": round((canonical_economics["executable_entry_ask_cents"] + canonical_economics["total_estimated_cost_cents"]) / 100.0, 8),
                "max_entry_price": canonical_economics["maximum_entry_price_cents"],
                "calibrated_max_entry_price": canonical_economics["maximum_entry_price_cents"],
            })

        with self._lock:
            self._latest[asset] = deepcopy(snap)
        with _LATEST_LOCK:
            _LATEST_BY_ASSET[asset] = deepcopy(snap)
        return snap

    def summary(self) -> dict[str, Any]:
        with self._lock:
            latest = deepcopy(self._latest)
        return {
            "version": VERSION,
            "probability_version": PROBABILITY_VERSION,
            "economics_version": ECONOMICS_VERSION,
            "enabled": self.enabled,
            "entry_gate_enabled": self.gate_enabled,
            "read_only": True,
            "out_of_sample_edge_proven": False,
            "cycles": self._cycles,
            "latest": latest,
        }

    def health(self) -> dict[str, Any]:
        return {
            "version": VERSION,
            "enabled": self.enabled,
            "read_only": True,
            "canonical_economics": True,
            "market_baseline_used_once": True,
            "live_parameter_updates": False,
        }


def _asset_from_message(message: str) -> str | None:
    upper = message.upper()
    for asset in ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE"):
        if re.search(rf"\b{asset}\b", upper):
            return asset
    return None


def augment_telegram_message(message: str) -> str:
    """Backward-compatible Telegram formatter with side-consistent V9 data."""
    # V9.5 checkpoint alerts own their full layout (the hourly-report-style
    # table). Leave them untouched so the clean table is not followed by an
    # appended "V9 TRADE QUALITY" block.
    if not isinstance(message, str) or "V9 TRADE QUALITY" in message or "V9.5 CHECK" in message:
        return message
    asset = _asset_from_message(message)
    with _LATEST_LOCK:
        snap = deepcopy(_LATEST_BY_ASSET.get(asset) if asset else None)
        if snap is None and _LATEST_BY_ASSET:
            # Multi-candidate reports are summarized compactly below.
            candidates = list(_LATEST_BY_ASSET.values())[:3]
        else:
            candidates = [snap] if snap else []
    if not candidates:
        return message

    lines = [message, "", "<b>V9 TRADE QUALITY</b>"]
    for index, candidate in enumerate(candidates, start=1):
        side = candidate.get("calibrated_edge_side")
        status = candidate.get("calibrated_edge_status") or "DIRECTION_UNCERTAIN"
        asset_name = candidate.get("asset") or asset or "?"
        prefix = f"{index}. " if len(candidates) > 1 else ""
        lines.append(f"{prefix}<b>{asset_name} {side or ''}</b> — {status}")
        economics = candidate.get("canonical_economics") or {}
        if economics:
            lines.append(
                "Calibrated P: "
                f"{float(economics.get('calibrated_side_probability', 0))*100:.1f}% | "
                f"conservative: {float(economics.get('conservative_side_probability', 0))*100:.1f}% | "
                f"ask: {float(economics.get('executable_entry_ask_cents', 0)):.2f}¢"
            )
            lines.append(
                f"Canonical net edge: {float(economics.get('net_edge_cents', 0)):+.2f}¢ | "
                f"conservative: {float(economics.get('conservative_net_edge_cents', 0)):+.2f}¢"
            )
            lines.append(
                "Costs: fee "
                f"{float(economics.get('estimated_fee_cents', 0)):.2f}¢ + "
                f"slippage {float(economics.get('estimated_slippage_cents', 0)):.2f}¢ + "
                f"impact {float(economics.get('estimated_market_impact_cents', 0)):.2f}¢"
            )
        reasons = candidate.get("calibrated_edge_blockers_human") or []
        if reasons:
            lines.append("Main blocker: " + str(reasons[0]))
        votes = candidate.get("v9_directional_votes") or {}
        if votes.get("total_possible_votes"):
            lines.append(
                f"Directional agreement: {float(votes.get('directional_agreement', 0))*100:.0f}% | "
                f"evidence coverage: {float(votes.get('evidence_coverage', 0))*100:.0f}%"
            )
    # Mention override only when it really occurred.
    if any(c.get("calibrated_edge_status") == "READY" for c in candidates) and "NO TRADE" in message.upper():
        lines.append("A READY trade-quality result did not override the checkpoint rejection.")
    lines.append(DISCLAIMER)
    return "\n".join(lines)
