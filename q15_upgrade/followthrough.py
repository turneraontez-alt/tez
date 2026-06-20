from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class PatternSetup:
    candle_start: float
    close: float
    direction: str
    name: str
    low: float | None = None
    high: float | None = None


@dataclass
class FollowThroughResult:
    active_direction: str | None
    setup_name: str | None
    follow_through: bool
    confirming_candle_start: float | None
    invalidated: bool
    reason: str | None


class FollowThroughTracker:
    """Confirms a prior pattern with the next completed candle.

    Unlike the old logic, the new candle does not need to print another copy
    of the same pattern.  It only needs to continue in the stored setup's
    direction and avoid a strong opposite signal.
    """

    def __init__(self):
        self._state: Dict[str, dict] = {}

    def evaluate(self, asset: str, snapshot: dict, now: float) -> FollowThroughResult:
        state = self._state.setdefault(asset, {
            "ticker": None,
            "last_completed_start": None,
            "pattern_on_last": None,
            "pending": None,
            "result_for_candle": None,
        })
        ticker = snapshot.get("ticker")
        # Keep pattern state across ticker rollover because the underlying price
        # action itself does not reset when a new 15-minute contract opens.
        state["ticker"] = ticker

        candle = self._latest_completed(snapshot, now)
        if not candle:
            return FollowThroughResult(None, None, False, None, False, "no_completed_underlying_candle")

        start = float(candle["start_time"])
        pattern = self._current_pattern(snapshot, candle)
        if state["last_completed_start"] is None:
            state["last_completed_start"] = start
            state["pattern_on_last"] = pattern
            return FollowThroughResult(None, None, False, None, False, "waiting_for_next_candle")

        if start != state["last_completed_start"]:
            state["pending"] = state.get("pattern_on_last")
            state["last_completed_start"] = start
            state["pattern_on_last"] = pattern
            state["result_for_candle"] = self._confirm(state.get("pending"), candle, pattern)
        else:
            # A pattern can be detected later while the same completed candle is
            # repeatedly evaluated.  Keep the strongest observation for use by
            # the next candle.
            if pattern is not None:
                state["pattern_on_last"] = pattern

        result = state.get("result_for_candle")
        if result is None:
            pending = state.get("pending")
            return FollowThroughResult(
                pending.direction if pending else None,
                pending.name if pending else None,
                False,
                start,
                False,
                "waiting_for_directional_close",
            )
        return result

    @staticmethod
    def _confirm(pending: PatternSetup | None, candle: dict, current_pattern: PatternSetup | None) -> FollowThroughResult:
        if pending is None:
            return FollowThroughResult(None, None, False, candle.get("start_time"), False, "no_prior_pattern")
        try:
            close = float(candle["close"])
        except (TypeError, ValueError, KeyError):
            return FollowThroughResult(pending.direction, pending.name, False, candle.get("start_time"), False, "missing_close")

        opposing = current_pattern is not None and current_pattern.direction != pending.direction
        moved = close - pending.close
        continued = (pending.direction == "bullish" and moved > 0) or (pending.direction == "bearish" and moved < 0)

        crossed_invalidation = False
        if pending.direction == "bullish" and pending.low is not None:
            crossed_invalidation = float(candle.get("low", close)) < pending.low
        elif pending.direction == "bearish" and pending.high is not None:
            crossed_invalidation = float(candle.get("high", close)) > pending.high

        invalidated = opposing or crossed_invalidation
        follow = continued and not invalidated
        reason = None
        if opposing:
            reason = "opposing_pattern_on_confirmation_candle"
        elif crossed_invalidation:
            reason = "confirmation_candle_crossed_setup_extreme"
        elif not continued:
            reason = "next_candle_did_not_continue"
        return FollowThroughResult(
            active_direction=pending.direction,
            setup_name=pending.name,
            follow_through=follow,
            confirming_candle_start=float(candle.get("start_time")),
            invalidated=invalidated,
            reason=reason,
        )

    @staticmethod
    def _latest_completed(snapshot: dict, now: float) -> dict | None:
        candidates = []
        for candle in snapshot.get("underlying_candles_5s") or []:
            try:
                end = float(candle.get("end_time", float(candle["start_time"]) + 5.0))
            except Exception:
                continue
            if end <= now + 0.05:
                candidates.append(candle)
        if not candidates:
            return None
        return max(candidates, key=lambda item: float(item.get("start_time", 0)))

    @staticmethod
    def _current_pattern(snapshot: dict, candle: dict) -> PatternSetup | None:
        direction = snapshot.get("pattern_direction")
        name = snapshot.get("pattern_detected")
        if direction not in {"bullish", "bearish"} or not name or name == "none":
            return None
        try:
            return PatternSetup(
                candle_start=float(candle["start_time"]),
                close=float(candle["close"]),
                direction=direction,
                name=str(name),
                low=float(candle["low"]) if candle.get("low") is not None else None,
                high=float(candle["high"]) if candle.get("high") is not None else None,
            )
        except (TypeError, ValueError, KeyError):
            return None
