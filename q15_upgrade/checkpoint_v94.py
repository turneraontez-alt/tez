"""Q15 V9.4 dual-window chart context.

Adds a rolling 30-minute context layer made from:
- the completed 15-minute window immediately before the active contract, and
- the elapsed/completed candles inside the active 15-minute contract.

The layer is deliberately a context/regime filter, not another vote and not a
probability calibrator. It never changes the selected YES/NO side, never places
orders, and never changes production weights from outcomes.
"""
from __future__ import annotations

import copy
import math
import os
import time
import re
import statistics
import threading
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

from .checkpoint_v93 import (
    CheckpointPolicyV93,
    format_telegram_message as _format_v93_message,
)

VERSION = "q15-v9.4-dual-window-context"
WINDOW_SECONDS = 15 * 60
DEFAULT_CANDLE_SECONDS = 5.0
READ_ONLY = True

_LATEST_LOCK = threading.RLock()
_LATEST_BY_ASSET: dict[str, dict[str, Any]] = {}


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, *, low: float | None = None, high: float | None = None) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = float(default)
    if not math.isfinite(value):
        value = float(default)
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


def _env_int(name: str, default: int, *, low: int | None = None, high: int | None = None) -> int:
    try:
        value = int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        value = int(default)
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return default
        parsed = float(value)
        return parsed if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_ts(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    text = str(value).strip()
    if not text:
        return None
    numeric = _num(text)
    if numeric is not None:
        return numeric
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _as_side(value: Any) -> str | None:
    side = str(value or "").strip().upper()
    return side if side in {"YES", "NO"} else None


def _selected_side(snapshot: Mapping[str, Any]) -> str | None:
    for key in (
        "entry_side",
        "selected_side",
        "calibrated_edge_side",
        "q15_selected_side",
        "side",
        "recommended_side",
    ):
        side = _as_side(snapshot.get(key))
        if side:
            return side
    direction = str(snapshot.get("pattern_direction") or "").lower()
    if direction == "bullish":
        return "YES"
    if direction == "bearish":
        return "NO"
    return None


def _asset_name(asset: Any, snapshot: Mapping[str, Any]) -> str:
    value = str(snapshot.get("asset") or asset or "UNKNOWN").strip().upper()
    return value or "UNKNOWN"


def _target(snapshot: Mapping[str, Any]) -> float | None:
    for key in ("target_value", "target", "floor_strike", "strike", "threshold"):
        value = _num(snapshot.get(key))
        if value is not None:
            return value
    return None


def _yes_is_higher(snapshot: Mapping[str, Any]) -> bool:
    target_type = str(snapshot.get("target_type") or snapshot.get("strike_type") or "greater_or_equal").lower()
    below_tokens = ("less", "below", "under", "lower")
    return not any(token in target_type for token in below_tokens)


def _candle_source(snapshot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for key in (
        "underlying_candles_5s",
        "underlying_candles",
        "spot_candles_5s",
        "spot_candles",
    ):
        value = snapshot.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, Mapping)]
    return []


def _normalize_candle(raw: Mapping[str, Any], now: float) -> dict[str, float] | None:
    start = None
    for key in ("start_time", "start", "timestamp", "time", "ts", "bucket"):
        start = _parse_ts(raw.get(key))
        if start is not None:
            break
    if start is None:
        return None

    end = None
    for key in ("end_time", "end", "close_time"):
        end = _parse_ts(raw.get(key))
        if end is not None:
            break
    if end is None:
        duration = _num(raw.get("duration"), DEFAULT_CANDLE_SECONDS) or DEFAULT_CANDLE_SECONDS
        end = start + _clamp(duration, 0.25, 300.0)
    if end <= start or end > now + 0.25:
        return None

    open_price = _num(raw.get("open"))
    high = _num(raw.get("high"))
    low = _num(raw.get("low"))
    close = _num(raw.get("close"))
    if None in (open_price, high, low, close):
        return None
    assert open_price is not None and high is not None and low is not None and close is not None
    if min(open_price, high, low, close) <= 0:
        return None
    high = max(high, open_price, close)
    low = min(low, open_price, close)
    if high < low:
        return None

    return {
        "start_time": float(start),
        "end_time": float(end),
        "open": float(open_price),
        "high": float(high),
        "low": float(low),
        "close": float(close),
        "volume": float(_num(raw.get("volume"), 0.0) or 0.0),
        "trade_count": float(_num(raw.get("trade_count"), 0.0) or 0.0),
    }


def _window_bounds(snapshot: Mapping[str, Any], now: float) -> tuple[float, float, float]:
    close_ts = None
    for key in ("close_time", "market_close", "contract_close", "expiration_time"):
        close_ts = _parse_ts(snapshot.get(key))
        if close_ts is not None:
            break
    if close_ts is None:
        seconds = _num(snapshot.get("seconds_remaining"))
        if seconds is not None and 0 <= seconds <= WINDOW_SECONDS + 180:
            close_ts = now + seconds
    if close_ts is None:
        current_start = math.floor(now / WINDOW_SECONDS) * WINDOW_SECONDS
        close_ts = current_start + WINDOW_SECONDS
    else:
        current_start = close_ts - WINDOW_SECONDS
        # Some feeds provide slightly non-boundary close timestamps. Snap only
        # when the correction is small; otherwise preserve the contract's times.
        rounded = round(current_start / WINDOW_SECONDS) * WINDOW_SECONDS
        if abs(current_start - rounded) <= 3.0:
            current_start = rounded
            close_ts = current_start + WINDOW_SECONDS
    previous_start = current_start - WINDOW_SECONDS
    return previous_start, current_start, close_ts


def _median_interval(candles: list[dict[str, float]]) -> float:
    if len(candles) < 2:
        if candles:
            return _clamp(candles[0]["end_time"] - candles[0]["start_time"], 0.25, 60.0)
        return DEFAULT_CANDLE_SECONDS
    gaps = [
        b["start_time"] - a["start_time"]
        for a, b in zip(candles, candles[1:])
        if 0.25 <= b["start_time"] - a["start_time"] <= 60.0
    ]
    return _clamp(statistics.median(gaps) if gaps else DEFAULT_CANDLE_SECONDS, 0.25, 60.0)


def _linear_slope(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2.0
    y_mean = sum(values) / n
    denominator = sum((index - x_mean) ** 2 for index in range(n))
    if denominator <= 0:
        return 0.0
    return sum((index - x_mean) * (value - y_mean) for index, value in enumerate(values)) / denominator


def summarize_window(
    candles: Iterable[Mapping[str, Any]],
    *,
    start: float,
    end: float,
    now: float,
    target: float | None,
    yes_is_higher: bool,
) -> dict[str, Any]:
    rows = [dict(row) for row in candles if start <= float(row["start_time"]) < end and float(row["end_time"]) <= min(end, now) + 0.25]
    rows.sort(key=lambda row: (row["start_time"], row["end_time"]))
    deduped: dict[float, dict[str, Any]] = {round(float(row["start_time"]), 6): row for row in rows}
    rows = sorted(deduped.values(), key=lambda row: row["start_time"])
    elapsed = max(0.0, min(end, now) - start)
    interval = _median_interval(rows)
    expected = max(1, int(math.ceil(elapsed / interval))) if elapsed > 0 else 0
    coverage = min(1.0, len(rows) / expected) if expected else 0.0

    base: dict[str, Any] = {
        "start_time": _iso(start),
        "end_time": _iso(end),
        "elapsed_seconds": round(elapsed, 3),
        "candle_count": len(rows),
        "estimated_interval_seconds": round(interval, 3),
        "expected_candle_count": expected,
        "coverage": round(coverage, 4),
        "complete": bool(now >= end and coverage >= 0.60),
        "available": bool(rows),
    }
    if not rows:
        base.update({
            "open": None,
            "high": None,
            "low": None,
            "close": None,
            "return_bps": None,
            "range_bps": None,
            "close_location": None,
            "wick_balance": None,
            "slope_score": None,
            "target_occupancy": None,
            "direction_score": None,
            "direction": "INSUFFICIENT",
        })
        return base

    open_price = float(rows[0]["open"])
    close_price = float(rows[-1]["close"])
    high = max(float(row["high"]) for row in rows)
    low = min(float(row["low"]) for row in rows)
    total_range = max(high - low, max(open_price, close_price) * 1e-9)
    return_bps = (close_price - open_price) / open_price * 10000.0
    range_bps = total_range / open_price * 10000.0
    body_score = _clamp((close_price - open_price) / total_range, -1.0, 1.0)
    close_location = _clamp(((close_price - low) / total_range) * 2.0 - 1.0, -1.0, 1.0)

    closes = [float(row["close"]) for row in rows]
    slope_per_candle = _linear_slope(closes)
    slope_score = _clamp(slope_per_candle * max(1, len(closes) - 1) / total_range, -1.0, 1.0)

    lower_wicks = 0.0
    upper_wicks = 0.0
    for row in rows:
        row_open = float(row["open"])
        row_close = float(row["close"])
        lower_wicks += max(0.0, min(row_open, row_close) - float(row["low"]))
        upper_wicks += max(0.0, float(row["high"]) - max(row_open, row_close))
    wick_total = lower_wicks + upper_wicks
    wick_balance = (lower_wicks - upper_wicks) / wick_total if wick_total > 0 else 0.0

    target_occupancy = None
    target_score = 0.0
    if target is not None:
        above_fraction = sum(1 for value in closes if value >= target) / len(closes)
        target_occupancy = above_fraction if yes_is_higher else 1.0 - above_fraction
        target_score = target_occupancy * 2.0 - 1.0

    raw_score = (
        0.34 * body_score
        + 0.24 * close_location
        + 0.20 * slope_score
        + 0.14 * wick_balance
        + 0.08 * target_score
    )
    # Sparse partial windows are still useful, but their direction is shrunk
    # toward neutral instead of pretending that a few candles equal 15 minutes.
    reliability = _clamp(coverage, 0.0, 1.0) * _clamp(len(rows) / 12.0, 0.20, 1.0)
    direction_score = _clamp(raw_score * reliability, -1.0, 1.0)
    direction = "BULLISH" if direction_score >= 0.12 else "BEARISH" if direction_score <= -0.12 else "NEUTRAL"

    base.update({
        "open": round(open_price, 10),
        "high": round(high, 10),
        "low": round(low, 10),
        "close": round(close_price, 10),
        "return_bps": round(return_bps, 4),
        "range_bps": round(range_bps, 4),
        "close_location": round(close_location, 4),
        "wick_balance": round(wick_balance, 4),
        "slope_score": round(slope_score, 4),
        "target_occupancy": None if target_occupancy is None else round(target_occupancy, 4),
        "direction_score": round(direction_score, 4),
        "direction": direction,
    })
    return base


def _side_score(direction_score: float | None, side: str | None, yes_is_higher: bool) -> float | None:
    if direction_score is None or side not in {"YES", "NO"}:
        return None
    yes_score = direction_score if yes_is_higher else -direction_score
    return yes_score if side == "YES" else -yes_score


def analyse_dual_window_context(
    snapshot: Mapping[str, Any],
    candles: Iterable[Mapping[str, Any]],
    now: float,
    *,
    min_previous_coverage: float = 0.65,
    min_previous_candles: int = 90,
    min_current_candles: int = 3,
    pass_score: float = 0.18,
    veto_score: float = -0.45,
    current_veto_score: float = -0.65,
) -> dict[str, Any]:
    previous_start, current_start, close_ts = _window_bounds(snapshot, now)
    target = _target(snapshot)
    higher = _yes_is_higher(snapshot)
    side = _selected_side(snapshot)
    rows = [dict(row) for row in candles]

    previous = summarize_window(
        rows,
        start=previous_start,
        end=current_start,
        now=now,
        target=target,
        yes_is_higher=higher,
    )
    current = summarize_window(
        rows,
        start=current_start,
        end=close_ts,
        now=now,
        target=target,
        yes_is_higher=higher,
    )

    previous_ready = (
        previous["candle_count"] >= min_previous_candles
        and previous["coverage"] >= min_previous_coverage
    )
    current_ready = current["candle_count"] >= min_current_candles and current["coverage"] >= 0.35
    previous_side = _side_score(previous.get("direction_score"), side, higher)
    current_side = _side_score(current.get("direction_score"), side, higher)

    elapsed_current = max(0.0, min(WINDOW_SECONDS, now - current_start))
    current_weight = _clamp(elapsed_current / 600.0, 0.10, 0.65)
    previous_weight = 1.0 - current_weight

    combined = None
    if previous_ready and current_ready and previous_side is not None and current_side is not None:
        combined = previous_weight * previous_side + current_weight * current_side
    elif previous_ready and previous_side is not None and current_side is not None and current["candle_count"] > 0:
        # Not enough current-window evidence for PASS, but retain a shrunken
        # diagnostic score so the endpoint can show which way context leans.
        combined = 0.85 * previous_side + 0.15 * current_side
    elif previous_ready and previous_side is not None:
        combined = 0.80 * previous_side
    elif current_ready and current_side is not None:
        combined = 0.65 * current_side

    if previous_side is None or current_side is None:
        relation = "INSUFFICIENT"
    elif previous_side >= 0.12 and current_side >= 0.12:
        relation = "CONTINUATION_WITH_SIDE"
    elif previous_side <= -0.12 and current_side <= -0.12:
        relation = "CONTINUATION_AGAINST_SIDE"
    elif previous_side >= 0.12 and current_side <= -0.12:
        relation = "CURRENT_REVERSAL_AGAINST_SIDE"
    elif previous_side <= -0.12 and current_side >= 0.12:
        relation = "CURRENT_REVERSAL_TOWARD_SIDE"
    else:
        relation = "MIXED"

    blockers: list[str] = []
    if side is None:
        blockers.append("selected_side_unavailable")
    if not previous_ready:
        blockers.append("previous_15m_incomplete")
    if not current_ready:
        blockers.append("current_15m_insufficient")

    strong_current_opposition = bool(current_ready and current_side is not None and current_side <= current_veto_score)
    if strong_current_opposition:
        status = "VETO"
        reason = "strong_current_15m_reversal_against_selected_side"
    elif combined is not None and combined <= veto_score:
        status = "VETO"
        reason = "combined_30m_context_opposes_selected_side"
    elif previous_ready and current_ready and combined is not None and combined >= pass_score:
        status = "PASS"
        reason = "previous_and_current_15m_context_support_selected_side"
    elif blockers:
        status = "INSUFFICIENT"
        reason = blockers[0]
    else:
        status = "WATCH"
        reason = "30m_context_mixed_or_not_strong_enough"

    return {
        "version": VERSION,
        "read_only": True,
        "selected_side": side,
        "yes_is_higher": higher,
        "target": target,
        "contract_previous_start": _iso(previous_start),
        "contract_current_start": _iso(current_start),
        "contract_close": _iso(close_ts),
        "previous_15m": previous,
        "current_15m": current,
        "previous_side_support": None if previous_side is None else round(previous_side, 4),
        "current_side_support": None if current_side is None else round(current_side, 4),
        "previous_weight": round(previous_weight, 4),
        "current_weight": round(current_weight, 4),
        "combined_side_support": None if combined is None else round(combined, 4),
        "relation": relation,
        "status": status,
        "reason": reason,
        "blockers": blockers,
        "strong_current_opposition": strong_current_opposition,
        "entry_context_pass": status == "PASS",
        "context_is_vote": False,
        "changes_selected_side": False,
    }


def _existing_confirmed(snapshot: Mapping[str, Any]) -> bool:
    state = str(snapshot.get("decision_state") or snapshot.get("entry_status") or "").upper()
    if state not in {"ENTRY CONFIRMED", "REVERSAL WARNING", "ACTIVE"}:
        return False
    return bool(
        snapshot.get("entry_confirmed_at")
        or snapshot.get("frozen_entry_price") is not None
        or (_num(snapshot.get("entry_age_seconds"), 0.0) or 0.0) > 1.0
        or str(snapshot.get("position_state") or "").upper() == "ACTIVE"
    )


def _append_unique_list(snapshot: dict[str, Any], key: str, item: str) -> None:
    value = snapshot.get(key)
    if isinstance(value, list):
        if item not in value:
            value.append(item)
    elif value is None:
        snapshot[key] = [item]


class _BufferedNotifier:
    """Capture checkpoint sends until V9.4 has applied its context gate.

    This is important because V9.3 may emit its Telegram message from inside
    ``run_cycle``. Returning success to the parent preserves its one-shot latch,
    while the real durable outbox receives the same message and idempotency key
    only after V9.4 has decided whether the entry headline must be downgraded.
    """

    def __init__(self, real: Any) -> None:
        self.real = real
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.real, "enabled", True))

    def send(self, *args: Any, **kwargs: Any) -> bool:
        self.calls.append((args, dict(kwargs)))
        return True

    def __getattr__(self, name: str) -> Any:
        return getattr(self.real, name)

    def flush(self, contexts: Mapping[str, Mapping[str, Any]]) -> tuple[int, int]:
        sent = 0
        failed = 0
        sender = getattr(self.real, "send", None)
        if not callable(sender):
            return 0, len(self.calls)
        for args, kwargs in self.calls:
            gated_args = args
            if args:
                gated_args = (_rewrite_gated_message(args[0], contexts), *args[1:])
            try:
                accepted = sender(*gated_args, **kwargs)
                if accepted is False:
                    failed += 1
                else:
                    sent += 1
            except Exception:
                failed += 1
        self.calls.clear()
        return sent, failed


def _rewrite_gated_message(text: Any, contexts: Mapping[str, Mapping[str, Any]]) -> str:
    message = str(text or "")
    upper = message.upper()
    entry_tokens = ("ENTRY RECOMMENDED", "ENTRY_RECOMMENDED", "ENTRY SIGNAL", "PAPER SIGNAL")
    if not any(token in upper for token in entry_tokens):
        return message

    mentioned = [asset for asset in contexts if re.search(rf"\b{re.escape(asset)}\b", upper)]
    relevant = mentioned or (list(contexts) if len(contexts) == 1 else [])
    if not relevant:
        return message
    statuses = [str(contexts[asset].get("status") or "INSUFFICIENT").upper() for asset in relevant]
    if all(status == "PASS" for status in statuses):
        return message

    veto = any(status == "VETO" for status in statuses)
    header = (
        "🛑 V9.4 NO TRADE — 30M CONTEXT VETO\n"
        if veto
        else "👀 V9.4 WATCH — 30M CONTEXT NOT READY\n"
    )
    replacement = "NO TRADE" if veto else "WATCH"
    for token in entry_tokens:
        message = re.sub(re.escape(token), replacement, message, flags=re.I)
    return header + message


def _value_failed(snapshot: Mapping[str, Any]) -> bool:
    status = str(
        snapshot.get("q15_v93_decision")
        or snapshot.get("three_gate_decision")
        or snapshot.get("entry_decision")
        or ""
    ).upper()
    if "PRICE_NOT_ATTRACTIVE" in status or "VALUE FAIL" in status:
        return True
    value_gate = snapshot.get("value_gate")
    if isinstance(value_gate, Mapping):
        state = str(value_gate.get("status") or value_gate.get("decision") or "").upper()
        return state in {"FAIL", "FAILED", "NO"}
    return snapshot.get("value_pass") is False or snapshot.get("q15_v93_value_pass") is False


def apply_context_gate(snapshot: dict[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    """Attach V9.4 fields and gate only fresh entries.

    Existing confirmed positions are not invalidated by this context layer.
    V9.3 remains responsible for management/exit behavior.
    """
    status = str(context.get("status") or "INSUFFICIENT").upper()
    value_failed = _value_failed(snapshot)
    confirmed = _existing_confirmed(snapshot)

    if value_failed:
        decision = "PRICE_NOT_ATTRACTIVE"
        entry_allowed = False
    elif status == "VETO":
        decision = "30M_CONTEXT_VETO"
        entry_allowed = False
    elif status == "PASS":
        # This does not independently recommend entry. It says the extra chart
        # context is compatible with the V9.3 wick/flow/value result.
        inherited = str(
            snapshot.get("q15_v93_decision")
            or snapshot.get("three_gate_decision")
            or snapshot.get("entry_decision")
            or snapshot.get("decision_state")
            or "WATCH"
        ).upper().replace(" ", "_")
        decision = inherited
        entry_allowed = True
    else:
        decision = "WATCH_30M_CONTEXT"
        entry_allowed = False

    snapshot["q15_v9_4_version"] = VERSION
    snapshot["q15_v9_4_context"] = copy.deepcopy(dict(context))
    snapshot["q15_v9_4_context_status"] = status
    snapshot["q15_v9_4_context_relation"] = context.get("relation")
    snapshot["q15_v9_4_context_score"] = context.get("combined_side_support")
    snapshot["q15_v9_4_previous_15m"] = copy.deepcopy(context.get("previous_15m"))
    snapshot["q15_v9_4_current_15m"] = copy.deepcopy(context.get("current_15m"))
    snapshot["q15_v9_4_decision"] = decision
    snapshot["q15_v9_4_entry_allowed"] = bool(entry_allowed)
    snapshot["q15_v9_4_existing_position_unchanged"] = bool(confirmed)
    snapshot["q15_v9_4_read_only"] = True

    if not entry_allowed and not confirmed:
        blocker = str(context.get("reason") or "30m_context_not_ready")
        for key in (
            "q15_new_entry_allowed",
            "calibrated_edge_new_entry_allowed",
            "new_entry_allowed",
            "entry_allowed",
            "q15_v93_entry_allowed",
        ):
            if key in snapshot:
                snapshot[key] = False
        # Always expose the V9.4 gate even when older modules used different
        # names. Downstream code can key off this canonical field.
        snapshot["q15_new_entry_allowed"] = False
        for key in ("entry_blockers", "blockers", "q15_entry_blockers"):
            _append_unique_list(snapshot, key, f"q15_v94:{blocker}")
        _append_unique_list(snapshot, "monitor_warnings", "30-minute chart context blocked a new entry")

        # Do not rewrite active management states. For an unconfirmed fresh
        # recommendation, normalize only the outward state so old formatter or
        # signal layers cannot still announce a buy.
        current_state = str(snapshot.get("decision_state") or "").upper()
        if current_state in {"ENTRY RECOMMENDED", "ENTRY_RECOMMENDED", "ENTRY CANDIDATE", "READY"}:
            snapshot["decision_state"] = "WATCH" if status != "VETO" else "NO TRADE"
        current_entry = str(snapshot.get("entry_status") or "").upper()
        if current_entry in {"ENTRY RECOMMENDED", "ENTRY_RECOMMENDED", "ENTRY CANDIDATE", "READY"}:
            snapshot["entry_status"] = "WATCH" if status != "VETO" else "NO TRADE"

    return snapshot


class _PersistentCacheConnection(sqlite3.Connection):
    """SQLite connection whose ``close()`` is a no-op so the context-candle cache
    reuses one connection instead of re-opening the file on every cycle.
    ``_persist_candles`` runs per asset every cycle; re-opening the file on
    Replit's networked ``data/`` disk dominated that cost. Reuse is serialized by
    the cache's lock. ``with conn:`` transactions still commit/roll back."""

    def close(self) -> None:  # noqa: D401 - intentional no-op for connection reuse
        pass


class CheckpointPolicyV94(CheckpointPolicyV93):
    """V9.3 policy plus rolling prior/current 15-minute chart context."""

    VERSION = VERSION

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.enabled = _env_bool("Q15_V94_CONTEXT_ENABLED", True)
        self.cache_seconds = _env_float("Q15_V94_CACHE_SECONDS", 2400.0, low=1900.0, high=7200.0)
        self.min_previous_coverage = _env_float("Q15_V94_MIN_PREVIOUS_COVERAGE", 0.65, low=0.40, high=1.0)
        self.min_previous_candles = _env_int("Q15_V94_MIN_PREVIOUS_CANDLES", 90, low=30, high=180)
        self.min_current_candles = _env_int("Q15_V94_MIN_CURRENT_CANDLES", 3, low=1, high=60)
        self.pass_score = _env_float("Q15_V94_PASS_SCORE", 0.18, low=0.05, high=0.80)
        self.veto_score = _env_float("Q15_V94_VETO_SCORE", -0.45, low=-0.95, high=-0.10)
        self.current_veto_score = _env_float("Q15_V94_CURRENT_VETO_SCORE", -0.65, low=-0.98, high=-0.20)
        self.persist_cache = _env_bool("Q15_V94_CACHE_PERSIST", True)
        self.cache_db_path = os.environ.get("Q15_V94_CACHE_DB_PATH", "data/q15_v94_context.sqlite3")
        self._context_lock = threading.RLock()
        self._cache_db_lock = threading.RLock()
        self._cache_shared_connection: sqlite3.Connection | None = None
        self._candle_cache: dict[str, dict[float, dict[str, float]]] = {}
        self._loaded_cache_assets: set[str] = set()
        self._cache_db_error: str | None = None
        self._latest_context: dict[str, dict[str, Any]] = {}
        self._cycles = 0
        self._errors = 0
        self._last_error: str | None = None
        self._last_cycle_at: float | None = None
        self._telegram_buffered = 0
        self._telegram_flushed = 0
        self._telegram_flush_failures = 0
        self._init_cache_db()

    def _cache_connection(self) -> sqlite3.Connection:
        # Reuse one persistent connection (serialized by self._cache_db_lock at
        # every call site) instead of re-opening the file each cycle — the file
        # open is the dominant cost on Replit's networked disk and
        # _persist_candles runs per asset every cycle.
        connection = self._cache_shared_connection
        if connection is not None:
            return connection
        path = Path(self.cache_db_path)
        if path.parent and str(path.parent) not in {"", "."}:
            path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(path), timeout=3.0, check_same_thread=False,
            factory=_PersistentCacheConnection,
        )
        connection.execute("PRAGMA busy_timeout=3000")
        connection.execute("PRAGMA journal_mode=WAL")
        self._cache_shared_connection = connection
        return connection

    def _init_cache_db(self) -> None:
        if not self.persist_cache:
            return
        try:
            with self._cache_db_lock, self._cache_connection() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS q15_v94_candles (
                        asset TEXT NOT NULL,
                        start_time REAL NOT NULL,
                        end_time REAL NOT NULL,
                        open REAL NOT NULL,
                        high REAL NOT NULL,
                        low REAL NOT NULL,
                        close REAL NOT NULL,
                        volume REAL NOT NULL DEFAULT 0,
                        trade_count REAL NOT NULL DEFAULT 0,
                        PRIMARY KEY (asset, start_time)
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS q15_v94_candles_end_idx "
                    "ON q15_v94_candles(asset, end_time)"
                )
            self._cache_db_error = None
        except Exception as exc:
            self._cache_db_error = f"{type(exc).__name__}: {exc}"

    def _load_persistent_cache(self, asset: str, now: float) -> None:
        if not self.persist_cache or asset in self._loaded_cache_assets:
            return
        cutoff = now - self.cache_seconds
        try:
            with self._cache_db_lock, self._cache_connection() as connection:
                rows = connection.execute(
                    "SELECT start_time, end_time, open, high, low, close, volume, trade_count "
                    "FROM q15_v94_candles WHERE asset=? AND end_time>=? ORDER BY start_time",
                    (asset, cutoff),
                ).fetchall()
            with self._context_lock:
                cache = self._candle_cache.setdefault(asset, {})
                for row in rows:
                    candle = {
                        "start_time": float(row[0]),
                        "end_time": float(row[1]),
                        "open": float(row[2]),
                        "high": float(row[3]),
                        "low": float(row[4]),
                        "close": float(row[5]),
                        "volume": float(row[6]),
                        "trade_count": float(row[7]),
                    }
                    cache[round(candle["start_time"], 6)] = candle
                self._loaded_cache_assets.add(asset)
            self._cache_db_error = None
        except Exception as exc:
            self._cache_db_error = f"{type(exc).__name__}: {exc}"
            with self._context_lock:
                self._loaded_cache_assets.add(asset)

    def _persist_candles(self, asset: str, candles: list[dict[str, float]], now: float) -> None:
        if not self.persist_cache or not candles:
            return
        cutoff = now - self.cache_seconds
        try:
            values = [
                (
                    asset, row["start_time"], row["end_time"], row["open"],
                    row["high"], row["low"], row["close"],
                    row.get("volume", 0.0), row.get("trade_count", 0.0),
                )
                for row in candles
            ]
            with self._cache_db_lock, self._cache_connection() as connection:
                connection.executemany(
                    "INSERT OR REPLACE INTO q15_v94_candles "
                    "(asset,start_time,end_time,open,high,low,close,volume,trade_count) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    values,
                )
                connection.execute(
                    "DELETE FROM q15_v94_candles WHERE asset=? AND end_time<?",
                    (asset, cutoff),
                )
            self._cache_db_error = None
        except Exception as exc:
            self._cache_db_error = f"{type(exc).__name__}: {exc}"

    def _ingest(self, snapshots: Mapping[str, Any], now: float) -> None:
        cutoff = now - self.cache_seconds
        for asset_key, raw_snapshot in snapshots.items():
            if not isinstance(raw_snapshot, Mapping):
                continue
            asset = _asset_name(asset_key, raw_snapshot)
            self._load_persistent_cache(asset, now)
            new_candles: list[dict[str, float]] = []
            with self._context_lock:
                cache = self._candle_cache.setdefault(asset, {})
                for raw in _candle_source(raw_snapshot):
                    candle = _normalize_candle(raw, now)
                    if candle is not None:
                        key = round(candle["start_time"], 6)
                        prior = cache.get(key)
                        if prior != candle:
                            cache[key] = candle
                            new_candles.append(candle)
                stale = [key for key, candle in cache.items() if candle["end_time"] < cutoff]
                for key in stale:
                    cache.pop(key, None)
            self._persist_candles(asset, new_candles, now)

    def _candles(self, asset: str) -> list[dict[str, float]]:
        with self._context_lock:
            # Candle rows are flat dicts of immutable floats, so dict() gives the
            # same independent copy as deepcopy at a fraction of the cost. This
            # runs ~4x/cycle over the full per-asset history, so it matters.
            return [dict(row) for _, row in sorted(self._candle_cache.get(asset, {}).items())]

    def run_cycle(
        self,
        snapshots: Dict[str, dict],
        now: float,
        ws_health: Mapping[str, Any] | None,
        focus_manager: Any,
        calibrated_edge: Any,
        notifier: Any,
    ) -> Dict[str, dict]:
        self._ingest(snapshots, now)
        buffered_notifier = _BufferedNotifier(notifier)
        _ct = getattr(self, "_chain_timing", None)
        _t0 = time.monotonic()
        enriched = super().run_cycle(
            snapshots, now, ws_health, focus_manager, calibrated_edge, buffered_notifier
        )
        if isinstance(_ct, dict):
            _ct["v91_run_cycle"] = round(time.monotonic() - _t0, 3)
        _t_v94 = time.monotonic()
        self._telegram_buffered += len(buffered_notifier.calls)
        self._ingest(enriched, now)
        if not self.enabled:
            sent, failed = buffered_notifier.flush({})
            self._telegram_flushed += sent
            self._telegram_flush_failures += failed
            return enriched

        output: Dict[str, dict] = {}
        cycle_contexts: dict[str, dict[str, Any]] = {}
        try:
            for asset_key, raw_snapshot in enriched.items():
                if not isinstance(raw_snapshot, Mapping):
                    continue
                snapshot = copy.deepcopy(dict(raw_snapshot))
                asset = _asset_name(asset_key, snapshot)
                context = analyse_dual_window_context(
                    snapshot,
                    self._candles(asset),
                    now,
                    min_previous_coverage=self.min_previous_coverage,
                    min_previous_candles=self.min_previous_candles,
                    min_current_candles=self.min_current_candles,
                    pass_score=self.pass_score,
                    veto_score=self.veto_score,
                    current_veto_score=self.current_veto_score,
                )
                apply_context_gate(snapshot, context)
                output[asset_key] = snapshot
                with self._context_lock:
                    self._latest_context[asset] = copy.deepcopy(context)
                with _LATEST_LOCK:
                    _LATEST_BY_ASSET[asset] = copy.deepcopy(context)
                cycle_contexts[asset] = copy.deepcopy(context)
            if isinstance(_ct, dict):
                _ct["v94_own_work"] = round(time.monotonic() - _t_v94, 3)
            sent, failed = buffered_notifier.flush(cycle_contexts)
            self._telegram_flushed += sent
            self._telegram_flush_failures += failed
            self._cycles += 1
            self._last_cycle_at = now
            self._last_error = None if failed == 0 else f"telegram_flush_failures:{failed}"
            return output
        except Exception as exc:
            # Fail closed for all new entries if the context layer itself fails.
            self._errors += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            failed: Dict[str, dict] = {}
            for asset_key, raw_snapshot in enriched.items():
                if not isinstance(raw_snapshot, Mapping):
                    continue
                snapshot = copy.deepcopy(dict(raw_snapshot))
                context = {
                    "version": VERSION,
                    "read_only": True,
                    "selected_side": _selected_side(snapshot),
                    "status": "INSUFFICIENT",
                    "relation": "ERROR",
                    "reason": "dual_window_context_error",
                    "blockers": ["dual_window_context_error"],
                    "combined_side_support": None,
                    "previous_15m": None,
                    "current_15m": None,
                    "entry_context_pass": False,
                    "context_is_vote": False,
                    "changes_selected_side": False,
                }
                failed[asset_key] = apply_context_gate(snapshot, context)
                cycle_contexts[_asset_name(asset_key, snapshot)] = context
            sent, flush_failed = buffered_notifier.flush(cycle_contexts)
            self._telegram_flushed += sent
            self._telegram_flush_failures += flush_failed
            return failed

    def context_status(self) -> dict[str, Any]:
        with self._context_lock:
            rows = [
                {"asset": asset, **copy.deepcopy(context)}
                for asset, context in sorted(self._latest_context.items())
            ]
        return {
            "version": VERSION,
            "enabled": self.enabled,
            "read_only": True,
            "window_design": "previous_completed_15m_plus_current_elapsed_15m",
            "context_is_vote": False,
            "changes_selected_side": False,
            "assets": rows,
        }

    def health(self) -> dict[str, Any]:
        try:
            parent = super().health()
        except Exception as exc:  # pragma: no cover - defensive across older V9.3 builds
            parent = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
        with self._context_lock:
            cached = sum(len(rows) for rows in self._candle_cache.values())
            assets = len(self._latest_context)
        return {
            "version": VERSION,
            "enabled": self.enabled,
            "read_only": True,
            "cycles": self._cycles,
            "errors": self._errors,
            "last_error": self._last_error,
            "last_cycle_at": _iso(self._last_cycle_at),
            "assets_with_context": assets,
            "cached_completed_candles": cached,
            "persistent_cache_enabled": self.persist_cache,
            "persistent_cache_path": self.cache_db_path if self.persist_cache else None,
            "persistent_cache_error": self._cache_db_error,
            "telegram_messages_buffered": self._telegram_buffered,
            "telegram_messages_flushed": self._telegram_flushed,
            "telegram_flush_failures": self._telegram_flush_failures,
            "telegram_gate_before_delivery": True,
            "previous_window_required": True,
            "current_window_required": True,
            "context_is_vote": False,
            "automatic_learning": False,
            "automatic_threshold_changes": False,
            "parent_v93": parent,
        }

    def diagnostics(self) -> dict[str, Any]:
        try:
            parent = super().diagnostics()
        except Exception as exc:  # pragma: no cover
            parent = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
        return {
            **self.health(),
            "settings": {
                "cache_seconds": self.cache_seconds,
                "cache_persist": self.persist_cache,
                "cache_db_path": self.cache_db_path if self.persist_cache else None,
                "min_previous_coverage": self.min_previous_coverage,
                "min_previous_candles": self.min_previous_candles,
                "min_current_candles": self.min_current_candles,
                "pass_score": self.pass_score,
                "veto_score": self.veto_score,
                "current_veto_score": self.current_veto_score,
            },
            "decision_rule": {
                "context_pass": "allows V9.3 wick/flow/value decision to stand",
                "context_watch": "blocks only a fresh entry",
                "context_veto": "blocks only a fresh entry",
                "value_fail": "PRICE_NOT_ATTRACTIVE remains authoritative",
                "existing_confirmed_position": "not invalidated by V9.4",
            },
            "latest": self.context_status(),
            "parent_v93_diagnostics": parent,
        }

    def decision_stats(self) -> dict[str, Any]:
        try:
            parent = super().decision_stats()
        except Exception as exc:  # pragma: no cover
            parent = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
        with self._context_lock:
            counts = {"PASS": 0, "WATCH": 0, "VETO": 0, "INSUFFICIENT": 0}
            for context in self._latest_context.values():
                status = str(context.get("status") or "INSUFFICIENT").upper()
                counts[status] = counts.get(status, 0) + 1
        return {
            "version": VERSION,
            "read_only": True,
            "current_cycle_context_counts": counts,
            "resolved_v94_outcomes_available": False,
            "note": "V9.4 context outcomes are version-scoped and must not be mixed with older or synthetic records.",
            "parent_v93_decision_stats": parent,
        }

    # Backward-compatible endpoint called by the old V9.3 route after app.py
    # switches the object from checkpoint_v93 to checkpoint_v94.
    def wick_status(self) -> dict[str, Any]:
        try:
            parent = super().wick_status()
        except Exception as exc:  # pragma: no cover
            parent = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
        return {
            "version": VERSION,
            "read_only": True,
            "parent_v93_wick_analysis": parent,
            "dual_window_context": self.context_status(),
        }


def _assets_in_message(text: str) -> list[str]:
    upper = text.upper()
    with _LATEST_LOCK:
        assets = list(_LATEST_BY_ASSET)
    found: list[tuple[int, str]] = []
    for asset in assets:
        match = re.search(rf"\b{re.escape(asset)}\b", upper)
        if match:
            found.append((match.start(), asset))
    return [asset for _, asset in sorted(found)]


def _fmt_score(value: Any) -> str:
    parsed = _num(value)
    return "n/a" if parsed is None else f"{parsed:+.2f}"


def format_telegram_message(text: Any) -> str:
    """Apply V9.3 formatting, then append the V9.4 30-minute context."""
    message = _format_v93_message(text)
    if "30M CHART CONTEXT — PRIOR 15M + CURRENT 15M" in message:
        return message
    trigger = any(token in message.upper() for token in (
        "15M EARLY CHECK",
        "10M FINAL CHECK",
        "7M FINAL CHECK",
        "7M CHECK",
        "ENTRY RECOMMENDED",
        "ENTRY SIGNAL",
        "WATCH",
        "NO TRADE",
        "PRICE NOT ATTRACTIVE",
    ))
    if not trigger:
        return message

    assets = _assets_in_message(message)[:3]
    with _LATEST_LOCK:
        rows = [(asset, copy.deepcopy(_LATEST_BY_ASSET.get(asset))) for asset in assets]
    rows = [(asset, row) for asset, row in rows if row]
    if not rows:
        return message

    lines = ["", "", "<b>30M CHART CONTEXT — PRIOR 15M + CURRENT 15M</b>"]
    for index, (asset, row) in enumerate(rows, 1):
        previous = row.get("previous_15m") or {}
        current = row.get("current_15m") or {}
        side = row.get("selected_side") or "?"
        lines.extend([
            f"{index}. <b>{asset} {side}</b> — {row.get('status', 'INSUFFICIENT')} | {row.get('relation', 'INSUFFICIENT')}",
            f"Prior 15M: {previous.get('direction', 'INSUFFICIENT')} ({previous.get('candle_count', 0)} candles, {float(previous.get('coverage', 0.0)) * 100:.0f}% coverage)",
            f"Current 15M: {current.get('direction', 'INSUFFICIENT')} ({current.get('candle_count', 0)} candles, {float(current.get('coverage', 0.0)) * 100:.0f}% coverage)",
            f"Selected-side context score: <b>{_fmt_score(row.get('combined_side_support'))}</b> — {row.get('reason', 'n/a')}",
        ])
    lines.append("<i>Context filter only; it is not an extra vote and does not change the selected side. Read-only — no orders are placed.</i>")
    return message + "\n".join(lines)
