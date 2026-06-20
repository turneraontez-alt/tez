from __future__ import annotations

import html
import logging
import math
import os
import re
import statistics
import time
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from q15_upgrade.checkpoint_v92 import (
    DISCLAIMER,
    VERSION as V92_VERSION,
    CheckpointPolicyV92,
    _LATEST,
    _LATEST_LOCK,
    _candidate_order,
    _canonical_p_yes,
    _checkpoint,
    _contract_key,
    _env_float,
    _fmt_cents,
    _fmt_pct,
    _human_status,
    _num,
    _prob,
    _side_probability,
    _strip_disclaimers,
)

VERSION = "q15-v9.3-wick-flow-value"
_LOG = logging.getLogger(__name__)
_ACTIVE_POLICY: "CheckpointPolicyV93 | None" = None


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _epoch(value: Any) -> float | None:
    number = _num(value)
    if number is None:
        return None
    number = float(number)
    if number > 10_000_000_000:
        number /= 1000.0
    return number


@dataclass(frozen=True)
class Candle:
    open: float
    high: float
    low: float
    close: float
    timestamp: float | None = None

    @property
    def range(self) -> float:
        return max(0.0, self.high - self.low)

    @property
    def body_high(self) -> float:
        return max(self.open, self.close)

    @property
    def body_low(self) -> float:
        return min(self.open, self.close)

    @property
    def upper_wick(self) -> float:
        return max(0.0, self.high - self.body_high)

    @property
    def lower_wick(self) -> float:
        return max(0.0, self.body_low - self.low)

    @property
    def upper_wick_ratio(self) -> float:
        return self.upper_wick / self.range if self.range > 0 else 0.0

    @property
    def lower_wick_ratio(self) -> float:
        return self.lower_wick / self.range if self.range > 0 else 0.0

    @property
    def close_location(self) -> float:
        return (self.close - self.low) / self.range if self.range > 0 else 0.5


@dataclass(frozen=True)
class WickAnalysis:
    side: str
    status: str
    score: float
    setup_pass: bool
    rejection_pass: bool
    follow_through_pass: bool
    rejection_wick_ratio: float
    close_location: float
    sweep_reclaim: bool
    opposite_wick_veto: bool
    completed_candles: int
    candle_source: str
    completion_basis: str
    reason: str

    @property
    def passed(self) -> bool:
        return self.status in {"SUPPORTIVE", "STRONG"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "status": self.status,
            "score": round(self.score, 6),
            "setup_pass": self.setup_pass,
            "rejection_pass": self.rejection_pass,
            "follow_through_pass": self.follow_through_pass,
            "rejection_wick_ratio": round(self.rejection_wick_ratio, 6),
            "close_location": round(self.close_location, 6),
            "sweep_reclaim": self.sweep_reclaim,
            "opposite_wick_veto": self.opposite_wick_veto,
            "completed_candles": self.completed_candles,
            "candle_source": self.candle_source,
            "completion_basis": self.completion_basis,
            "reason": self.reason,
            "passed": self.passed,
        }


@dataclass(frozen=True)
class FlowSignal:
    name: str
    source_key: str
    signed_value: float | None
    side_value: float | None
    status: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source_key": self.source_key,
            "signed_value": self.signed_value,
            "side_value": self.side_value,
            "status": self.status,
        }


@dataclass(frozen=True)
class FlowAnalysis:
    side: str
    status: str
    supportive_count: int
    opposing_count: int
    available_count: int
    required_supportive: int
    signals: tuple[FlowSignal, ...]

    @property
    def passed(self) -> bool:
        return self.status == "SUPPORTIVE"

    def as_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "status": self.status,
            "supportive_count": self.supportive_count,
            "opposing_count": self.opposing_count,
            "available_count": self.available_count,
            "required_supportive": self.required_supportive,
            "signals": [signal.as_dict() for signal in self.signals],
            "passed": self.passed,
        }


def _normalise_candle(item: Any) -> Candle | None:
    if isinstance(item, Mapping):
        open_value = _num(item.get("open", item.get("o")))
        high_value = _num(item.get("high", item.get("h")))
        low_value = _num(item.get("low", item.get("l")))
        close_value = _num(item.get("close", item.get("c")))
        timestamp = _epoch(
            item.get(
                "close_time",
                item.get(
                    "end_time",
                    item.get("timestamp", item.get("time", item.get("t", item.get("open_time")))),
                ),
            )
        )
    elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and len(item) >= 5:
        # Common OHLCV forms: [timestamp, open, high, low, close, ...]
        timestamp = _epoch(item[0])
        open_value = _num(item[1])
        high_value = _num(item[2])
        low_value = _num(item[3])
        close_value = _num(item[4])
    else:
        return None

    if None in {open_value, high_value, low_value, close_value}:
        return None
    candle = Candle(
        open=float(open_value),
        high=float(high_value),
        low=float(low_value),
        close=float(close_value),
        timestamp=timestamp,
    )
    if not all(math.isfinite(value) for value in (candle.open, candle.high, candle.low, candle.close)):
        return None
    if candle.high < max(candle.open, candle.close) or candle.low > min(candle.open, candle.close):
        return None
    if candle.high <= candle.low:
        return None
    return candle


def _explicitly_closed(item: Any) -> bool | None:
    if not isinstance(item, Mapping):
        return None
    for key in ("closed", "is_closed", "complete", "completed", "final", "is_final"):
        if key in item:
            value = item.get(key)
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "closed", "complete", "final"}
            return bool(value)
    return None


def _candidate_candle_lists(snapshot: Mapping[str, Any]) -> Iterable[tuple[str, Any, bool]]:
    preferred = (
        "completed_candles",
        "recent_completed_candles",
        "spot_completed_candles",
        "underlying_completed_candles",
        "closed_candles",
    )
    generic = (
        "candles",
        "recent_candles",
        "spot_candles",
        "underlying_candles",
        # Live pipeline emits underlying (spot) candle series under resolution
        # suffixes; wick price-action must read the underlying, not the
        # contract-price candles.
        "underlying_candles_5s",
        "underlying_candles_15s",
        "underlying_candles_1s",
        "spot_candles_5s",
        "spot_candles_15s",
        "chart_candles",
        "klines",
        "ohlcv",
    )
    containers: list[Mapping[str, Any]] = [snapshot]
    for key in ("chart", "market_data", "underlying", "features", "price_action"):
        value = snapshot.get(key)
        if isinstance(value, Mapping):
            containers.append(value)

    for container in containers:
        for key in preferred:
            if key in container:
                yield key, container.get(key), True
        for key in generic:
            if key in container:
                yield key, container.get(key), False


def _flatten_candle_container(value: Any) -> Any:
    if isinstance(value, Mapping):
        for key in ("1m", "60s", "60", "minute", "one_minute", "data", "items", "candles"):
            nested = value.get(key)
            if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
                return nested
        sequences = [v for v in value.values() if isinstance(v, Sequence) and not isinstance(v, (str, bytes))]
        return max(sequences, key=len) if sequences else None
    return value


def extract_completed_candles(
    snapshot: Mapping[str, Any],
    now: float | None = None,
) -> tuple[list[Candle], str, str]:
    """Return the latest completed candles, failing closed on malformed OHLC data."""
    now = float(now or time.time())
    best: tuple[list[Candle], str, str] = ([], "unavailable", "unavailable")
    for source, raw_value, source_declares_complete in _candidate_candle_lists(snapshot):
        raw = _flatten_candle_container(raw_value)
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            continue
        candles: list[tuple[Candle, bool | None]] = []
        for item in raw:
            candle = _normalise_candle(item)
            if candle is None:
                continue
            candles.append((candle, _explicitly_closed(item)))
        if not candles:
            continue

        any_explicit = any(flag is not None for _, flag in candles)
        if source_declares_complete:
            accepted = [candle for candle, flag in candles if flag is not False]
            basis = "source_declares_completed"
        elif any_explicit:
            accepted = [candle for candle, flag in candles if flag is True]
            basis = "explicit_completion_flag"
        else:
            # Market-data adapters normally expose only closed historical candles.
            # When no flag exists, preserve the source order and label the assumption.
            accepted = [candle for candle, _ in candles]
            basis = "adapter_assumed_completed"

        accepted.sort(key=lambda candle: candle.timestamp if candle.timestamp is not None else 0.0)
        if len(accepted) > len(best[0]):
            best = (accepted, source, basis)
    return best


def _reference_level(snapshot: Mapping[str, Any], candles: Sequence[Candle]) -> float | None:
    keys = (
        "settlement_threshold",
        "contract_threshold",
        "threshold",
        "strike_price",
        "strike",
        "target_price",
        "target_value",
        "target",
        "reference_price",
    )
    values: list[float] = []
    for key in keys:
        value = _num(snapshot.get(key))
        if value is not None:
            values.append(float(value))
    if not values or not candles:
        return None
    midpoint = sum(c.close for c in candles) / len(candles)
    max_range = max(c.range for c in candles)
    plausible = [value for value in values if abs(value - midpoint) <= max(max_range * 10.0, abs(midpoint) * 0.05)]
    return min(plausible, key=lambda value: abs(value - midpoint)) if plausible else None


def analyse_wick_sequence(
    snapshot: Mapping[str, Any],
    side: str,
    *,
    now: float | None = None,
    minimum_wick_ratio: float = 0.30,
    supportive_score: float = 0.65,
    strong_score: float = 0.80,
    opposite_veto_ratio: float = 0.45,
) -> WickAnalysis:
    side = str(side or "UNCERTAIN").upper()
    candles, source, completion_basis = extract_completed_candles(snapshot, now)
    if side not in {"YES", "NO"}:
        return WickAnalysis(side, "DIRECTION_UNCERTAIN", 0.0, False, False, False, 0.0, 0.5, False, False, len(candles), source, completion_basis, "no_selected_side")
    if len(candles) < 3:
        return WickAnalysis(side, "INSUFFICIENT_CANDLES", 0.0, False, False, False, 0.0, 0.5, False, False, len(candles), source, completion_basis, "need_three_completed_candles")

    setup, rejection, follow = candles[-3:]
    level = _reference_level(snapshot, (setup, rejection, follow))
    if side == "YES":
        wick_ratio = rejection.lower_wick_ratio
        side_close = rejection.close_location
        prior_sweep = rejection.low < setup.low and rejection.close > setup.low
        level_sweep = bool(level is not None and rejection.low < level < rejection.close)
        setup_pass = rejection.low <= setup.low or level_sweep
        rejection_pass = wick_ratio >= minimum_wick_ratio and side_close >= 0.60
        holds_extreme = follow.low >= rejection.low
        directional_close = follow.close >= follow.open
        progresses = follow.close >= rejection.close or follow.close >= (rejection.high + rejection.low) / 2.0
        follow_score = (float(holds_extreme) + float(directional_close) + float(progresses)) / 3.0
        follow_pass = holds_extreme and (directional_close or progresses) and follow_score >= 2.0 / 3.0
        opposite_veto = follow.upper_wick_ratio >= opposite_veto_ratio and follow.close_location < 0.50
        close_quality = side_close
    else:
        wick_ratio = rejection.upper_wick_ratio
        side_close = 1.0 - rejection.close_location
        prior_sweep = rejection.high > setup.high and rejection.close < setup.high
        level_sweep = bool(level is not None and rejection.high > level > rejection.close)
        setup_pass = rejection.high >= setup.high or level_sweep
        rejection_pass = wick_ratio >= minimum_wick_ratio and rejection.close_location <= 0.40
        holds_extreme = follow.high <= rejection.high
        directional_close = follow.close <= follow.open
        progresses = follow.close <= rejection.close or follow.close <= (rejection.high + rejection.low) / 2.0
        follow_score = (float(holds_extreme) + float(directional_close) + float(progresses)) / 3.0
        follow_pass = holds_extreme and (directional_close or progresses) and follow_score >= 2.0 / 3.0
        opposite_veto = follow.lower_wick_ratio >= opposite_veto_ratio and follow.close_location > 0.50
        close_quality = side_close

    sweep_reclaim = prior_sweep or level_sweep
    wick_quality = _clamp((wick_ratio - 0.15) / 0.35)
    sweep_score = 1.0 if sweep_reclaim else 0.50 if setup_pass else 0.0
    score = _clamp(
        0.40 * wick_quality
        + 0.25 * _clamp(close_quality)
        + 0.20 * sweep_score
        + 0.15 * follow_score
    )

    if opposite_veto:
        status = "OPPOSING"
        reason = "latest_completed_candle_has_strong_opposite_wick"
    elif setup_pass and rejection_pass and follow_pass and score >= strong_score:
        status = "STRONG"
        reason = "setup_rejection_and_follow_through_confirmed"
    elif setup_pass and rejection_pass and follow_pass and score >= supportive_score:
        status = "SUPPORTIVE"
        reason = "wick_sequence_supports_selected_side"
    elif score < 0.45:
        status = "OPPOSING"
        reason = "wick_sequence_does_not_support_selected_side"
    else:
        status = "NEUTRAL"
        reason = "partial_wick_sequence_waiting_for_confirmation"

    return WickAnalysis(
        side=side,
        status=status,
        score=score,
        setup_pass=setup_pass,
        rejection_pass=rejection_pass,
        follow_through_pass=follow_pass,
        rejection_wick_ratio=wick_ratio,
        close_location=rejection.close_location,
        sweep_reclaim=sweep_reclaim,
        opposite_wick_veto=opposite_veto,
        completed_candles=len(candles),
        candle_source=source,
        completion_basis=completion_basis,
        reason=reason,
    )


def _walk_mapping(value: Any, depth: int = 0) -> Iterable[tuple[str, Any]]:
    if depth > 3 or not isinstance(value, Mapping):
        return
    for key, item in value.items():
        yield str(key), item
        if isinstance(item, Mapping):
            yield from _walk_mapping(item, depth + 1)


def _find_value(snapshot: Mapping[str, Any], keys: Sequence[str]) -> tuple[str, Any] | None:
    wanted = {key.lower() for key in keys}
    for key, value in _walk_mapping(snapshot):
        if key.lower() in wanted and value is not None:
            return key, value
    return None


def _normalise_signed(value: Any, *, ratio: bool = False) -> float | None:
    if isinstance(value, str):
        text = value.strip().upper()
        if text in {"YES", "BULLISH", "BUY", "SUPPORTIVE", "UP"}:
            return 1.0
        if text in {"NO", "BEARISH", "SELL", "OPPOSING", "DOWN"}:
            return -1.0
        if text in {"NEUTRAL", "FLAT", "NONE", "UNKNOWN"}:
            return 0.0
    number = _num(value)
    if number is None or not math.isfinite(float(number)):
        return None
    number = float(number)
    if ratio:
        if abs(number) > 1.0 and abs(number) <= 100.0:
            number /= 100.0
        if 0.0 <= number <= 1.0:
            number = 2.0 * number - 1.0
    elif abs(number) > 1.0 and abs(number) <= 100.0:
        number /= 100.0
    return _clamp(number, -1.0, 1.0)


def _underlying_momentum(snapshot: Mapping[str, Any]) -> float | None:
    """Volatility-normalised directional momentum from underlying spot candles.

    Returns a YES-positive score in ``[-1, 1]`` (rising underlying -> positive),
    or ``None`` when there are too few candles to be meaningful.  Self-calibrates
    on recent realised volatility so no fixed price-scale constant is required.
    """
    points: list[tuple[float, float]] = []
    for key in (
        "underlying_candles_5s",
        "underlying_candles_15s",
        "underlying_candles",
        "spot_candles_5s",
        "spot_candles",
    ):
        value = snapshot.get(key)
        if value is None:
            continue
        raw = _flatten_candle_container(value)
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            continue
        candidate: list[tuple[float, float]] = []
        for item in raw:
            candle = _normalise_candle(item)
            if candle is not None and candle.close > 0:
                candidate.append((candle.timestamp if candle.timestamp is not None else 0.0, candle.close))
        # Prefer the first source with enough candles, but keep the best
        # partial source so a sparse high-res series can't shadow a usable one.
        if len(candidate) > len(points):
            points = candidate
        if len(points) >= 4:
            break
    if len(points) < 4:
        return None
    points.sort(key=lambda point: point[0])
    closes = [close for _, close in points][-24:]
    returns = [
        (closes[i] - closes[i - 1]) / closes[i - 1]
        for i in range(1, len(closes))
        if closes[i - 1] > 0
    ]
    if len(returns) < 3 or closes[0] <= 0:
        return None
    drift = (closes[-1] - closes[0]) / closes[0]
    volatility = statistics.pstdev(returns)
    if volatility <= 1e-9:
        return _clamp(drift / 0.0010, -1.0, 1.0)
    tstat = drift / (volatility * math.sqrt(len(returns)))
    return _clamp(tstat / 2.0, -1.0, 1.0)


def _derived_flow_inputs(snapshot: Mapping[str, Any]) -> dict[str, float]:
    """Reconstruct the canonical flow scalars from raw pipeline fields.

    The live pipeline stores executed-flow volumes, order-book imbalance and the
    underlying candle series under their own names; it never emits the
    pre-computed ``executed_flow_skew`` / ``order_book_imbalance`` /
    ``spot_momentum`` scalars that :func:`analyse_flow` searches for.  Without
    these fallbacks every flow signal reports ``UNAVAILABLE`` and the gate is
    permanently ``INSUFFICIENT_DATA``.  Mirrors the raw fields already consumed
    by ``calibrated_edge._microstructure_residual``.  All scores are YES-positive
    and clamped to ``[-1, 1]``.  Read-only: derives, never mutates the snapshot.
    """
    derived: dict[str, float] = {}

    yes_vol = _num(snapshot.get("taker_yes_volume_15s"))
    if yes_vol is None:
        yes_vol = _num(snapshot.get("aggressive_yes_volume"))
    no_vol = _num(snapshot.get("taker_no_volume_15s"))
    if no_vol is None:
        no_vol = _num(snapshot.get("aggressive_no_volume"))
    if yes_vol is not None and no_vol is not None:
        total = float(yes_vol) + float(no_vol)
        if total > 0:
            derived["executed_flow_skew"] = _clamp((float(yes_vol) - float(no_vol)) / total, -1.0, 1.0)

    book = _num(snapshot.get("orderbook_imbalance"))
    if book is not None:
        book = float(book)
        if abs(book) > 1.0 and abs(book) <= 100.0:
            book /= 100.0
        derived["order_book_imbalance"] = _clamp(book, -1.0, 1.0)

    momentum = _underlying_momentum(snapshot)
    if momentum is not None:
        derived["spot_momentum"] = momentum

    return derived


def analyse_flow(
    snapshot: Mapping[str, Any],
    side: str,
    *,
    support_threshold: float = 0.10,
    oppose_threshold: float = -0.15,
    strong_oppose_threshold: float = -0.50,
    required_supportive: int = 2,
) -> FlowAnalysis:
    side = str(side or "UNCERTAIN").upper()
    sign = 1.0 if side == "YES" else -1.0 if side == "NO" else 0.0
    groups = (
        (
            "executed_flow",
            (
                "executed_flow_skew",
                "trade_flow_skew",
                "aggressive_flow_skew",
                "taker_flow_imbalance",
                "net_aggressive_flow",
                "buy_sell_imbalance",
                "executed_buy_sell_imbalance",
                "aggressive_buy_ratio",
                "taker_buy_ratio",
                "executed_buy_ratio",
            ),
        ),
        (
            "persistent_order_book",
            (
                "orderbook_persistence",
                "book_imbalance_persistence",
                "persistent_book_imbalance",
                "persistent_orderbook_imbalance",
                "order_book_imbalance",
                "book_pressure",
                "microprice_bias",
                "yes_book_share",
            ),
        ),
        (
            "spot_momentum",
            (
                "spot_momentum",
                "underlying_momentum",
                "return_1m",
                "short_momentum",
                "price_momentum",
                "trend_score",
                "spot_return",
            ),
        ),
    )
    signals: list[FlowSignal] = []
    supportive = opposing = available = 0
    strong_opposing = False
    ratio_keys = {
        "aggressive_buy_ratio",
        "taker_buy_ratio",
        "executed_buy_ratio",
        "yes_book_share",
    }
    derived = _derived_flow_inputs(snapshot)
    for name, keys in groups:
        found = _find_value(snapshot, keys)
        if found is None and derived:
            found = _find_value(derived, keys)
        if found is None:
            signals.append(FlowSignal(name, "unavailable", None, None, "UNAVAILABLE"))
            continue
        source_key, raw = found
        signed = _normalise_signed(raw, ratio=source_key.lower() in ratio_keys)
        if signed is None:
            signals.append(FlowSignal(name, source_key, None, None, "UNAVAILABLE"))
            continue
        side_value = signed * sign
        available += 1
        if side_value >= support_threshold:
            status = "SUPPORTIVE"
            supportive += 1
        elif side_value <= oppose_threshold:
            status = "OPPOSING"
            opposing += 1
            strong_opposing = strong_opposing or side_value <= strong_oppose_threshold
        else:
            status = "NEUTRAL"
        signals.append(FlowSignal(name, source_key, round(signed, 6), round(side_value, 6), status))

    needed = min(max(1, required_supportive), available) if available else required_supportive
    if side not in {"YES", "NO"}:
        status = "DIRECTION_UNCERTAIN"
    elif available < 2:
        status = "INSUFFICIENT_DATA"
    elif strong_opposing or opposing >= 2:
        status = "OPPOSING"
    elif supportive >= needed and opposing == 0:
        status = "SUPPORTIVE"
    else:
        status = "NEUTRAL"
    return FlowAnalysis(side, status, supportive, opposing, available, needed, tuple(signals))


def decide_three_gate(
    *,
    side_valid: bool,
    data_status: str,
    strong_checkpoint_conflict: bool,
    value_available: bool,
    value_pass: bool,
    wick_status: str,
    flow_status: str,
) -> str:
    """Pure three-requirement decision function used by runtime and tests."""
    if not side_valid:
        return "DIRECTION_UNCERTAIN"
    if data_status in {"DATA_STALE", "INVALID_MARKET"}:
        return data_status
    if strong_checkpoint_conflict:
        return "CHECKPOINT_CONFLICT"
    if not value_available:
        return "ECONOMICS_UNAVAILABLE"
    if not value_pass:
        return "PRICE_NOT_ATTRACTIVE"
    if wick_status == "OPPOSING":
        return "WICK_VETO"
    if wick_status in {"INSUFFICIENT_CANDLES", "DIRECTION_UNCERTAIN"}:
        return "WATCH_PRICE_ACTION_DATA"
    if flow_status in {"INSUFFICIENT_DATA", "DIRECTION_UNCERTAIN"}:
        return "WATCH_FLOW_DATA"
    wick_pass = wick_status in {"SUPPORTIVE", "STRONG"}
    flow_pass = flow_status == "SUPPORTIVE"
    if wick_pass and flow_pass:
        return "READY"
    if wick_pass or flow_pass:
        return "WATCH"
    return "DIRECTION_UNCONFIRMED"


class CheckpointPolicyV93(CheckpointPolicyV92):
    """Wick-aware three-gate policy: price action, independent flow, executable value."""

    def __init__(self, store: Any = None, sqlite_path: str | None = None):
        super().__init__(store=store, sqlite_path=sqlite_path)
        self.minimum_wick_ratio = _clamp(_env_float("Q15_V93_MIN_WICK_RATIO", 0.30), 0.10, 0.80)
        self.supportive_wick_score = _clamp(_env_float("Q15_V93_SUPPORTIVE_WICK_SCORE", 0.65), 0.40, 0.95)
        self.strong_wick_score = max(self.supportive_wick_score, _clamp(_env_float("Q15_V93_STRONG_WICK_SCORE", 0.80), 0.50, 0.99))
        self.opposite_wick_veto_ratio = _clamp(_env_float("Q15_V93_OPPOSITE_WICK_VETO_RATIO", 0.45), 0.25, 0.90)
        self.flow_support_threshold = _clamp(_env_float("Q15_V93_FLOW_SUPPORT_THRESHOLD", 0.10), 0.0, 0.80)
        self.flow_oppose_threshold = -_clamp(abs(_env_float("Q15_V93_FLOW_OPPOSE_THRESHOLD", -0.15)), 0.01, 0.90)
        self.required_flow_confirmations = max(1, min(3, int(_env_float("Q15_V93_REQUIRED_FLOW_CONFIRMATIONS", 2))))
        self.minimum_side_probability = _clamp(_env_float("Q15_V93_MIN_SIDE_PROBABILITY", 0.58), 0.50, 0.90)
        self._migrate_v93()
        global _ACTIVE_POLICY
        _ACTIVE_POLICY = self

    def _migrate_v93(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS q15_v93_decisions (
                decision_key TEXT PRIMARY KEY,
                contract_key TEXT NOT NULL,
                asset TEXT NOT NULL,
                checkpoint TEXT NOT NULL,
                selected_side TEXT NOT NULL,
                wick_status TEXT NOT NULL,
                wick_score DOUBLE PRECISION,
                flow_status TEXT NOT NULL,
                flow_supportive_count INTEGER,
                value_pass INTEGER NOT NULL,
                checkpoint_relation TEXT NOT NULL,
                decision TEXT NOT NULL,
                conservative_edge_cents DOUBLE PRECISION,
                adjusted_required_edge_cents DOUBLE PRECISION,
                created_at DOUBLE PRECISION NOT NULL,
                model_version TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS q15_v93_decision_lookup ON q15_v93_decisions(model_version,checkpoint,decision)",
        ]
        if self.persistence.pg:
            for sql in statements:
                if not bool(self.persistence.store.execute(sql)):
                    raise RuntimeError("q15_v93_decision_migration_failed")
            return
        with self.persistence._lock, self.persistence._sqlite() as conn:
            for sql in statements:
                conn.execute(sql)
            conn.commit()

    def pre_enrich_all(self, snapshots: Mapping[str, dict], now: float) -> dict[str, dict]:
        prepared = super().pre_enrich_all(snapshots, now)
        output: dict[str, dict] = {}
        for asset, original in prepared.items():
            snap = dict(original)
            side = str(snap.get("calibrated_edge_side") or snap.get("q15_v91_frozen_side") or snap.get("q15_v91_current_side") or "UNCERTAIN").upper()
            wick = analyse_wick_sequence(
                snap,
                side,
                now=now,
                minimum_wick_ratio=self.minimum_wick_ratio,
                supportive_score=self.supportive_wick_score,
                strong_score=self.strong_wick_score,
                opposite_veto_ratio=self.opposite_wick_veto_ratio,
            )
            flow = analyse_flow(
                snap,
                side,
                support_threshold=self.flow_support_threshold,
                oppose_threshold=self.flow_oppose_threshold,
                required_supportive=self.required_flow_confirmations,
            )
            snap.update({
                "q15_v93_version": VERSION,
                "q15_v93_wick": wick.as_dict(),
                "q15_v93_flow": flow.as_dict(),
                "q15_v93_price_action_pass": wick.passed,
                "q15_v93_flow_pass": flow.passed,
                # V9.3 replaces rolling-vote eligibility with the three-candle price-action sequence.
                "q15_v92_direction_valid": side in {"YES", "NO"},
            })
            output[asset] = snap
        return output

    def _v93_values(self, asset: str, snapshot: Mapping[str, Any], now: float) -> tuple[Any, ...]:
        contract = str(snapshot.get("q15_v91_contract_key") or _contract_key(asset, snapshot))
        checkpoint = str(snapshot.get("q15_v91_checkpoint") or _checkpoint(snapshot))
        key = f"{contract}|{asset}|{checkpoint}|{VERSION}"
        wick = snapshot.get("q15_v93_wick") or {}
        flow = snapshot.get("q15_v93_flow") or {}
        return (
            key,
            contract,
            asset,
            checkpoint,
            str(snapshot.get("calibrated_edge_side") or snapshot.get("q15_v91_frozen_side") or "UNCERTAIN"),
            str(wick.get("status") or "UNKNOWN"),
            _num(wick.get("score")),
            str(flow.get("status") or "UNKNOWN"),
            int(flow.get("supportive_count") or 0),
            1 if bool(snapshot.get("q15_v93_value_pass")) else 0,
            str(snapshot.get("q15_v92_checkpoint_relation") or "UNKNOWN"),
            str(snapshot.get("q15_v93_decision") or "UNKNOWN"),
            _num(snapshot.get("q15_v93_conservative_edge_cents")),
            _num(snapshot.get("q15_v93_adjusted_required_edge_cents")),
            float(now),
            VERSION,
        )

    def _record_v93(self, asset: str, snapshot: Mapping[str, Any], now: float) -> None:
        self._record_v93_many([self._v93_values(asset, snapshot, now)])

    def _record_v93_many(self, rows: Sequence[tuple[Any, ...]]) -> None:
        """Insert many v93 decisions in one round-trip. Equivalent to N
        _record_v93 calls (ON CONFLICT/OR IGNORE dedups per row)."""
        rows = list(rows)
        if not rows:
            return
        cols = (
            "(decision_key,contract_key,asset,checkpoint,selected_side,wick_status,wick_score,"
            "flow_status,flow_supportive_count,value_pass,checkpoint_relation,decision,"
            "conservative_edge_cents,adjusted_required_edge_cents,created_at,model_version)"
        )
        if self.persistence.pg:
            one = "(" + ",".join(["%s"] * 16) + ")"
            placeholders = ",".join([one] * len(rows))
            flat = [v for tup in rows for v in tup]
            self.persistence.store.execute(
                f"INSERT INTO q15_v93_decisions {cols} VALUES {placeholders} "
                "ON CONFLICT (decision_key) DO NOTHING",
                flat,
            )
            return
        with self.persistence._lock, self.persistence._sqlite() as conn:
            conn.executemany(
                f"INSERT OR IGNORE INTO q15_v93_decisions {cols} "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            conn.commit()

    def finalize_all(self, snapshots: Mapping[str, dict], now: float) -> dict[str, dict]:
        parent = super().finalize_all(snapshots, now)
        output: dict[str, dict] = {}
        decision_rows: list[tuple[Any, ...]] = []
        for asset, original in parent.items():
            snap = dict(original)
            side = str(snap.get("calibrated_edge_side") or snap.get("q15_v91_frozen_side") or "UNCERTAIN").upper()
            p_yes = _canonical_p_yes(snap)
            side_probability = _prob(snap.get("calibrated_side_probability"))
            if side_probability is None:
                side_probability = _side_probability(p_yes, side)
            # Recompute after calibrated_edge.enrich_all so executed-flow and
            # persistent-book fields added during the current cycle are visible.
            wick_result = analyse_wick_sequence(
                snap,
                side,
                now=now,
                minimum_wick_ratio=self.minimum_wick_ratio,
                supportive_score=self.supportive_wick_score,
                strong_score=self.strong_wick_score,
                opposite_veto_ratio=self.opposite_wick_veto_ratio,
            )
            flow_result = analyse_flow(
                snap,
                side,
                support_threshold=self.flow_support_threshold,
                oppose_threshold=self.flow_oppose_threshold,
                required_supportive=self.required_flow_confirmations,
            )
            wick = wick_result.as_dict()
            flow = flow_result.as_dict()
            snap["q15_v93_wick"] = wick
            snap["q15_v93_flow"] = flow
            snap["q15_v93_price_action_pass"] = wick_result.passed
            snap["q15_v93_flow_pass"] = flow_result.passed
            economics = snap.get("canonical_economics") or {}
            conservative_edge = _num(economics.get("conservative_net_edge_cents", snap.get("conservative_edge_after_costs_cents")))
            base_required = float(_num((snap.get("trade_quality") or {}).get("required_edge_cents"), snap.get("q15_v92_base_required_edge_cents")) or 0.0)
            sequence_penalty = float(_num(snap.get("q15_v92_sequence_penalty_cents"), 0.0) or 0.0)
            adjusted_required = base_required + sequence_penalty
            relation = str(snap.get("q15_v92_checkpoint_relation") or "UNKNOWN").upper()
            strong_conflict = bool(snap.get("q15_v92_sequence_blocks_entry"))
            upstream_status = str(snap.get("calibrated_edge_status") or "UNKNOWN").upper()
            side_valid = side in {"YES", "NO"} and side_probability is not None and side_probability >= self.minimum_side_probability
            value_available = conservative_edge is not None
            value_pass = bool(value_available and conservative_edge >= adjusted_required)
            decision = decide_three_gate(
                side_valid=side_valid,
                data_status=upstream_status,
                strong_checkpoint_conflict=strong_conflict,
                value_available=value_available,
                value_pass=value_pass,
                wick_status=str(wick.get("status") or "INSUFFICIENT_CANDLES").upper(),
                flow_status=str(flow.get("status") or "INSUFFICIENT_DATA").upper(),
            )
            allowed = decision == "READY"
            blockers: list[str] = []
            if not side_valid:
                blockers.append("DIRECTION_PROBABILITY_BELOW_MINIMUM")
            if strong_conflict:
                blockers.append(relation)
            if not value_available:
                blockers.append("ECONOMICS_UNAVAILABLE")
            elif not value_pass:
                blockers.append("PRICE_NOT_ATTRACTIVE")
            if not bool(wick.get("passed")):
                blockers.append(str(wick.get("status") or "WICK_UNAVAILABLE"))
            if not bool(flow.get("passed")):
                blockers.append(str(flow.get("status") or "FLOW_UNAVAILABLE"))

            snap.update({
                "q15_v93_decision": decision,
                "q15_v93_entry_allowed": allowed,
                "q15_v93_blockers": list(dict.fromkeys(blockers)),
                "q15_v93_value_pass": value_pass,
                "q15_v93_conservative_edge_cents": conservative_edge,
                "q15_v93_base_required_edge_cents": base_required,
                "q15_v93_adjusted_required_edge_cents": adjusted_required,
                "q15_v93_three_requirements": {
                    "wick_price_action": bool(wick.get("passed")),
                    "independent_flow": bool(flow.get("passed")),
                    "executable_value": value_pass,
                },
                "q15_new_entry_allowed": allowed,
                "calibrated_edge_new_entry_allowed": allowed,
                "q15_v92_entry_allowed": allowed,
                "q15_v92_decision": decision,
                "q15_v91_decision": decision,
            })
            with _LATEST_LOCK:
                _LATEST[asset] = deepcopy(snap)
            decision_rows.append(self._v93_values(asset, snap, now))
            output[asset] = snap
        # One bulk write for every asset's v93 decision instead of one per asset.
        self._record_v93_many(decision_rows)
        return output

    def _query_stats(self) -> list[dict[str, Any]]:
        if self.persistence.pg:
            return list(self.persistence.store.query(
                "SELECT checkpoint,decision,wick_status,flow_status,value_pass,COUNT(*) AS n FROM q15_v93_decisions WHERE model_version=%s GROUP BY checkpoint,decision,wick_status,flow_status,value_pass",
                (VERSION,),
            ) or [])
        with self.persistence._lock, self.persistence._sqlite() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT checkpoint,decision,wick_status,flow_status,value_pass,COUNT(*) AS n FROM q15_v93_decisions WHERE model_version=? GROUP BY checkpoint,decision,wick_status,flow_status,value_pass",
                (VERSION,),
            ).fetchall()]

    def decision_stats(self) -> dict[str, Any]:
        decisions: Counter[str] = Counter()
        checkpoints: Counter[str] = Counter()
        wick_states: Counter[str] = Counter()
        flow_states: Counter[str] = Counter()
        value_states: Counter[str] = Counter()
        for row in self._query_stats():
            count = int(row.get("n") or 0)
            decisions[str(row.get("decision") or "UNKNOWN")] += count
            checkpoints[str(row.get("checkpoint") or "UNKNOWN")] += count
            wick_states[str(row.get("wick_status") or "UNKNOWN")] += count
            flow_states[str(row.get("flow_status") or "UNKNOWN")] += count
            value_states["PASS" if int(row.get("value_pass") or 0) else "FAIL"] += count
        blocker_candidates = {
            "WICK_PRICE_ACTION": sum(v for k, v in wick_states.items() if k not in {"SUPPORTIVE", "STRONG"}),
            "INDEPENDENT_FLOW": sum(v for k, v in flow_states.items() if k != "SUPPORTIVE"),
            "EXECUTABLE_VALUE": value_states.get("FAIL", 0),
        }
        bottleneck = max(blocker_candidates.items(), key=lambda item: item[1], default=("NONE", 0))
        return {
            "version": VERSION,
            "total_unique_decisions": sum(decisions.values()),
            "checkpoint_counts": dict(checkpoints),
            "decision_counts": dict(decisions),
            "wick_status_counts": dict(wick_states),
            "flow_status_counts": dict(flow_states),
            "value_gate_counts": dict(value_states),
            "ready_entries": decisions.get("READY", 0),
            "watch_decisions": sum(v for k, v in decisions.items() if k.startswith("WATCH") or k == "WATCH"),
            "main_bottleneck": {"reason": bottleneck[0], "count": bottleneck[1]},
        }

    def wick_status(self) -> dict[str, Any]:
        with _LATEST_LOCK:
            assets = {
                asset: {
                    "checkpoint": snap.get("q15_v91_checkpoint"),
                    "side": snap.get("calibrated_edge_side") or snap.get("q15_v91_frozen_side"),
                    "wick": snap.get("q15_v93_wick"),
                    "flow": snap.get("q15_v93_flow"),
                    "three_requirements": snap.get("q15_v93_three_requirements"),
                    "decision": snap.get("q15_v93_decision"),
                    "entry_allowed": snap.get("q15_v93_entry_allowed"),
                }
                for asset, snap in _LATEST.items()
            }
        return {"version": VERSION, "assets": assets, "read_only": True}

    def health(self) -> dict[str, Any]:
        return {
            **super().health(),
            "version": VERSION,
            "entry_requirements": ["wick_price_action", "independent_flow", "executable_value"],
            "rolling_vote_gate_removed": True,
            "three_completed_candle_sequence": True,
            "opposite_wick_veto": True,
            "read_only": True,
            "current_version_stats": self.decision_stats(),
        }

    def diagnostics(self) -> dict[str, Any]:
        return {
            **super().diagnostics(),
            "version": VERSION,
            "configuration_v93": {
                "minimum_wick_ratio": self.minimum_wick_ratio,
                "supportive_wick_score": self.supportive_wick_score,
                "strong_wick_score": self.strong_wick_score,
                "opposite_wick_veto_ratio": self.opposite_wick_veto_ratio,
                "flow_support_threshold": self.flow_support_threshold,
                "flow_oppose_threshold": self.flow_oppose_threshold,
                "required_flow_confirmations": self.required_flow_confirmations,
                "minimum_side_probability": self.minimum_side_probability,
            },
            "current_version_stats": self.decision_stats(),
        }


def _candidate_snapshot(asset: str) -> dict[str, Any]:
    with _LATEST_LOCK:
        return deepcopy(_LATEST.get(asset) or {})


def _pass_text(value: Any) -> str:
    return "PASS" if bool(value) else "FAIL"


def _flow_signal_text(signal: Mapping[str, Any]) -> str:
    return f"{str(signal.get('name') or 'signal').replace('_', ' ').title()}: {_human_status(str(signal.get('status') or 'UNAVAILABLE'))}"


def _format_checkpoint(message: str) -> str:
    base = re.split(r"\n\s*(?:<b>)?(?:PROFESSIONAL TRADE QUALITY|V9 TRADE QUALITY)", message, maxsplit=1, flags=re.I)[0]
    source_header = re.search(r"(?mi)^(.*(?:15M EARLY CHECK|15M CHECK|10M FINAL CHECK|10M CHECK|7M FINAL CHECK|7M CHECK).*)$", base)
    original_header = source_header.group(1).strip() if source_header else "Q15 CHECK"
    header_upper = original_header.upper()
    if "15M" in header_upper:
        checkpoint_name = "15M"
    elif "7M" in header_upper:
        checkpoint_name = "7M"
    else:
        checkpoint_name = "10M"
    candidates = _candidate_order(base)
    lines_by_candidate: list[str] = []
    decisions: list[str] = []
    ranking_bits: list[str] = []
    medals = ("🥇", "🥈", "🥉")

    for index, (asset, message_side) in enumerate(candidates, start=1):
        snap = _candidate_snapshot(asset)
        side = str(snap.get("calibrated_edge_side") or snap.get("q15_v91_frozen_side") or message_side).upper()
        badge = medals[index - 1] if index <= len(medals) else f"#{index}"
        safe_asset = html.escape(str(asset), quote=False)
        safe_side = html.escape(str(side), quote=False)
        ranking_bits.append(f"{badge} {safe_asset} {safe_side}")
        p_yes = _prob(snap.get("v9_calibrated_p_yes") or snap.get("calibrated_p_yes"))
        side_p = _prob(snap.get("calibrated_side_probability"))
        if side_p is None:
            side_p = _side_probability(p_yes, side)
        conservative = _prob(snap.get("conservative_side_probability"))
        economics = snap.get("canonical_economics") or {}
        ask = economics.get("executable_entry_ask_cents", snap.get("entry_ask"))
        costs = economics.get("total_estimated_cost_cents")
        edge = snap.get("q15_v93_conservative_edge_cents", economics.get("conservative_net_edge_cents"))
        required = snap.get("q15_v93_adjusted_required_edge_cents")
        wick = snap.get("q15_v93_wick") or {}
        flow = snap.get("q15_v93_flow") or {}
        requirements = snap.get("q15_v93_three_requirements") or {}
        decision = str(snap.get("q15_v93_decision") or "WATCH")
        decisions.append(decision)

        lines_by_candidate.extend([
            f"{badge} <b>{safe_asset} {safe_side}</b>",
            f"Overall end probability — YES {_fmt_pct(p_yes)} | NO {_fmt_pct(None if p_yes is None else 1.0 - p_yes)}",
            "<b>PRICE ACTION / WICKS</b>",
            f"Completed sequence: {min(int(wick.get('completed_candles') or 0), 3)}/3 | Wick score: {float(wick.get('score') or 0.0) * 100:.0f}%",
            f"Setup: {_pass_text(wick.get('setup_pass'))} | Rejection: {_pass_text(wick.get('rejection_pass'))} | Follow-through: {_pass_text(wick.get('follow_through_pass'))}",
            f"Rejection wick: {float(wick.get('rejection_wick_ratio') or 0.0) * 100:.0f}% of range | Opposite-wick veto: {'BLOCKED' if wick.get('opposite_wick_veto') else 'CLEAR'}",
            f"Price-action status: <b>{_human_status(str(wick.get('status') or 'UNAVAILABLE'))}</b>",
            "<b>INDEPENDENT FLOW</b>",
        ])
        for signal in list(flow.get("signals") or [])[:3]:
            if isinstance(signal, Mapping):
                lines_by_candidate.append(_flow_signal_text(signal))
        lines_by_candidate.extend([
            f"Flow confirmation: {int(flow.get('supportive_count') or 0)}/{int(flow.get('available_count') or 0)} supportive — <b>{_human_status(str(flow.get('status') or 'UNAVAILABLE'))}</b>",
            "<b>EXECUTABLE VALUE</b>",
            f"Model: {_fmt_pct(side_p)} | conservative: {_fmt_pct(conservative)}",
            f"{side} ask: {_fmt_cents(ask)} | estimated costs: {_fmt_cents(costs)}",
            f"Conservative net edge: <b>{_fmt_cents(edge, signed=True)}</b> | required: {_fmt_cents(required)}",
            f"Three requirements: Wicks {_pass_text(requirements.get('wick_price_action'))} | Flow {_pass_text(requirements.get('independent_flow'))} | Value {_pass_text(requirements.get('executable_value'))}",
        ])
        if checkpoint_name in ("10M", "7M"):
            lines_by_candidate.append(f"15M relationship: {_human_status(str(snap.get('q15_v92_checkpoint_relation') or 'UNKNOWN'))}")
        lines_by_candidate.append(f"Decision: <b>{_human_status(decision)}</b>")
        blockers = list(snap.get("q15_v93_blockers") or [])
        if blockers:
            lines_by_candidate.append("Main blocker: " + _human_status(str(blockers[0])))
        if decision.startswith("WATCH") or decision == "WATCH":
            missing: list[str] = []
            if not requirements.get("wick_price_action"):
                missing.append("complete a valid setup → wick rejection → follow-through sequence")
            if not requirements.get("independent_flow"):
                missing.append("gain two independent supportive flow signals")
            if not requirements.get("executable_value"):
                missing.append("retain positive conservative edge after costs")
            if missing:
                lines_by_candidate.append("Next trigger: " + "; ".join(missing) + ".")
        lines_by_candidate.append("")

    if any(decision == "READY" for decision in decisions):
        header = f"✅ {checkpoint_name} CHECK — ENTRY RECOMMENDED"
    elif any(decision.startswith("WATCH") or decision == "WATCH" for decision in decisions):
        header = f"👀 {checkpoint_name} CHECK — WATCH, NO ENTRY YET"
    else:
        header = f"🛑 {checkpoint_name} CHECK — NO TRADE"

    lines = [header]
    if ranking_bits:
        lines.append("<b>" + "  ·  ".join(ranking_bits) + "</b>")
    lines.append("")
    lines.extend(lines_by_candidate or ["No current candidate snapshots were available.", ""])
    lines.append(DISCLAIMER)
    result = "\n".join(lines).strip()
    return result if len(result) <= 4000 else result[:3990] + "…"


def _format_hourly(message: str) -> str:
    stats = _ACTIVE_POLICY.decision_stats() if _ACTIVE_POLICY is not None else {
        "total_unique_decisions": 0,
        "checkpoint_counts": {},
        "decision_counts": {},
        "ready_entries": 0,
        "watch_decisions": 0,
        "main_bottleneck": {"reason": "NO_CURRENT_PROCESS_DATA", "count": 0},
    }
    text = _strip_disclaimers(str(message or ""))
    overall = re.search(r"Overall:\s*([^\n]+)", text, flags=re.I)
    realized = re.search(r"Realized:\s*([^\n•]+)", text, flags=re.I)
    checkpoints = stats.get("checkpoint_counts") or {}
    bottleneck = stats.get("main_bottleneck") or {}
    lines = [
        "📊 <b>Hourly Operational Status</b>",
        "No new resolved contracts this hour." if "NO NEW RESOLVED" in text.upper() else "",
        "",
        "<b>Current V9.3 three-gate flow</b>",
        f"Unique decisions: {stats.get('total_unique_decisions', 0)}",
        f"15M: {checkpoints.get('15M', 0)} | 10M: {checkpoints.get('10M', 0)}",
        f"WATCH: {stats.get('watch_decisions', 0)} | ENTRY_RECOMMENDED: {stats.get('ready_entries', 0)}",
        f"Main bottleneck: {_human_status(str(bottleneck.get('reason', 'NONE')))} ({int(bottleneck.get('count', 0) or 0)})",
        "Requirements: wick-aware price action + independent flow + positive executable value.",
        "",
        "<b>Historical / mixed-version results</b>",
        f"Overall: {overall.group(1).strip() if overall else 'n/a'}",
        f"Realized: {realized.group(1).strip() if realized else 'n/a'}",
        "Shadow learning remains observational; no live thresholds change from one result.",
        DISCLAIMER,
    ]
    return "\n".join(line for line in lines if line != "").strip()


def format_telegram_message(message: Any) -> str:
    text = str(message or "")
    upper = text.upper()
    if any(token in upper for token in ("15M EARLY CHECK", "15M CHECK", "10M FINAL CHECK", "10M CHECK", "7M FINAL CHECK", "7M CHECK")):
        return _format_checkpoint(text)
    if "HOURLY REPORT" in upper or "HOURLY OPERATIONAL STATUS" in upper:
        return _format_hourly(text)
    result = _strip_disclaimers(text)
    if result and DISCLAIMER not in result and any(token in upper for token in ("WATCH —", "ENTRY SIGNAL", "PAPER SIGNAL")):
        result += "\n" + DISCLAIMER
    return result if len(result) <= 4000 else result[:3990] + "…"
