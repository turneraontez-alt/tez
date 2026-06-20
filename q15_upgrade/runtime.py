from __future__ import annotations
from .window_focus import adaptive_confirmation_ok

import copy
import math
from datetime import datetime
from typing import Dict

from .config import Q15Settings
from .confirmations import ConfirmationEngine
from .followthrough import FollowThroughTracker
from .model import ProbabilityModel
from .precision import estimated_fee_cents, max_entry_price_cents, vwap_for_quantity


class Q15Runtime:
    """Single source of truth for setup confirmation, entry, and exit state.

    V3 deliberately separates *entry gates* from *exit gates*. Fast-moving
    order-book/trade-flow confirmations may qualify an entry, but their normal
    flicker cannot immediately invalidate a position after an alert.
    """

    def __init__(self, legacy_config=None, learner=None, settings: Q15Settings | None = None):
        self.settings = settings or Q15Settings()
        self.legacy_config = legacy_config
        self.learner = learner
        self.model = ProbabilityModel(self.settings)
        self.follow = FollowThroughTracker()
        self.confirm = ConfirmationEngine()
        self._state: Dict[str, dict] = {}

    def enrich_all(self, snapshots: Dict[str, dict], now: float, ws_health=None) -> Dict[str, dict]:
        for asset, snapshot in snapshots.items():
            if isinstance(snapshot, dict) and snapshot.get("market_state") == "live":
                self.model.update_history(asset, snapshot, now)
        broader_returns = self.model.broader_returns(now, 60.0)

        enriched: Dict[str, dict] = {}
        for asset, snapshot in snapshots.items():
            if not isinstance(snapshot, dict):
                continue
            record = copy.deepcopy(snapshot)
            if record.get("market_state") == "live":
                record = self._enrich_one(asset, record, now, ws_health or {}, broader_returns)
            enriched[asset] = record
        return enriched

    def _enrich_one(self, asset: str, snap: dict, now: float, ws_health: dict, broader_returns: Dict[str, float]) -> dict:
        settings = self.settings
        ticker = snap.get("ticker")
        state = self._asset_state(asset, ticker)

        follow = self.follow.evaluate(asset, snap, now)
        observed_direction = snap.get("pattern_direction")
        if observed_direction not in {"bullish", "bearish"}:
            observed_direction = None
        if follow.follow_through:
            observed_direction = follow.active_direction
        observed_setup = follow.setup_name if follow.follow_through else snap.get("pattern_detected")
        observed_side = "YES" if observed_direction == "bullish" else "NO" if observed_direction == "bearish" else None
        setup_exists = observed_direction in {"bullish", "bearish"}

        active_entry = state.get("confirmed_at") is not None and state.get("confirmed_side") in {"YES", "NO"}
        if active_entry:
            side = state["confirmed_side"]
            direction = state.get("confirmed_direction") or ("bullish" if side == "YES" else "bearish")
            setup_name = state.get("confirmed_setup") or observed_setup
        else:
            side = observed_side
            direction = observed_direction
            setup_name = observed_setup

        estimate = self.model.estimate(asset, snap, now)
        raw_fair_yes = estimate.probability_yes
        fair_yes = raw_fair_yes
        raw_win_prob = self._side_probability(raw_fair_yes, side)
        calibration_info = {
            "raw_probability": raw_win_prob,
            "calibrated_probability": raw_win_prob,
            "version": "identity",
            "apply_live": False,
        }
        if self.learner and hasattr(self.learner, "calibrate_probability"):
            try:
                calibration_info = self.learner.calibrate_probability(asset, raw_win_prob, side)
            except Exception:
                pass
        win_prob = raw_win_prob
        if calibration_info.get("apply_live") and calibration_info.get("calibrated_probability") is not None:
            win_prob = float(calibration_info["calibrated_probability"])
            fair_yes = win_prob if side == "YES" else (1.0 - win_prob) if side == "NO" else raw_fair_yes

        confirmations = self.confirm.evaluate(
            asset=asset,
            snapshot=snap,
            direction=direction,
            follow_through=bool(follow.follow_through and observed_direction == direction),
            broader_returns=broader_returns,
            now=now,
        )

        seconds = self._num(snap.get("seconds_remaining"))
        spread = self._num(snap.get("spread"))
        learned = self._learned_adjustment(asset, seconds, setup_name, side)
        required_edge = settings.min_edge_for(int(seconds) if seconds is not None else None)
        if spread is not None and spread > 2.0:
            required_edge += (spread - 2.0) * settings.spread_penalty_per_cent
        required_edge += float(learned.get("edge_adjustment", 0.0) or 0.0)
        required_edge = max(2.0, round(required_edge, 2))

        economics = self._economics(snap, side, win_prob, required_edge)
        executable_ask = economics["ask"]
        filled = economics["filled"]
        full_size_available = economics["full_size_available"]
        fee_per_contract = economics["fee"]
        break_even = economics["break_even"]
        edge = economics["edge"]
        max_buy_price = economics["max_buy_price"]
        depth = economics["depth"]

        data_age = self._data_age(snap, now)
        max_age = settings.max_ws_age_seconds if ws_health.get("connected") else settings.max_rest_age_seconds
        data_warning = data_age > max_age

        entry_blockers = self._entry_blockers(
            snap=snap,
            follow=follow,
            side=side,
            seconds=seconds,
            confirmations=confirmations,
            estimate=estimate,
            win_prob=win_prob,
            edge=edge,
            required_edge=required_edge,
            spread=spread,
            depth=depth,
            full_size_available=full_size_available,
            data_warning=data_warning,
            learned=learned,
        )

        decision = "NO TRADE"
        exit_reason = None
        monitor_warnings = []
        clear_confirmed_after_output = False

        if active_entry:
            hard_exit_reason = self._hard_exit_reason(
                snap=snap,
                state=state,
                observed_side=observed_side,
                observed_follow_through=follow.follow_through,
            )

            if data_warning:
                monitor_warnings.append("data_stale_hold_last_valid_read")
            if confirmations.active_group_count < settings.min_confirmation_groups:
                monitor_warnings.append("confirmation_groups_weakened")
            if confirmations.total_score < settings.min_confirmation_score:
                monitor_warnings.append("confirmation_score_weakened")
            if spread is None or spread > settings.max_spread_cents:
                monitor_warnings.append("spread_not_suitable_for_new_entry")
            if depth is None or depth < settings.min_depth_contracts:
                monitor_warnings.append("book_depth_weakened")

            soft_exit_reasons = []
            # Missing/stale model data is a warning, not an automatic sell signal.
            if not data_warning and win_prob is not None and win_prob < settings.exit_win_probability:
                soft_exit_reasons.append(f"win_probability_below_{settings.exit_win_probability:.2f}")
            if not data_warning and edge is not None and edge < settings.exit_edge_cents:
                soft_exit_reasons.append(f"net_edge_below_{settings.exit_edge_cents:.1f}c")

            if hard_exit_reason:
                decision = "ENTRY INVALIDATED"
                exit_reason = hard_exit_reason
                clear_confirmed_after_output = True
            else:
                entry_age = max(0.0, now - float(state.get("confirmed_at") or now))
                if soft_exit_reasons:
                    if state.get("soft_exit_since") is None:
                        state["soft_exit_since"] = now
                    soft_age = now - state["soft_exit_since"]
                    monitor_warnings.extend(soft_exit_reasons)
                    if (
                        entry_age >= settings.minimum_hold_seconds
                        and soft_age >= settings.soft_exit_persistence_seconds
                    ):
                        decision = "ENTRY INVALIDATED"
                        exit_reason = "sustained_" + "+".join(soft_exit_reasons)
                        clear_confirmed_after_output = True
                    else:
                        decision = "ENTRY CONFIRMED"
                else:
                    state["soft_exit_since"] = None
                    decision = "ENTRY CONFIRMED"

        else:
            candidate_ok = not entry_blockers
            if candidate_ok:
                if state.get("candidate_side") != side or state.get("candidate_since") is None:
                    state["candidate_side"] = side
                    state["candidate_since"] = now
                    state["candidate_samples"] = 1
                else:
                    state["candidate_samples"] = int(state.get("candidate_samples", 0)) + 1
                state["candidate_last_ok"] = now
            else:
                last_ok = state.get("candidate_last_ok")
                within_grace = (
                    state.get("candidate_since") is not None
                    and last_ok is not None
                    and now - last_ok <= settings.candidate_grace_seconds
                    and side == state.get("candidate_side")
                )
                if not within_grace:
                    self._reset_candidate(state)

            last_ok = state.get("candidate_last_ok")
            within_grace = (
                state.get("candidate_since") is not None
                and last_ok is not None
                and now - last_ok <= settings.candidate_grace_seconds
                and side == state.get("candidate_side")
            )
            effective_candidate = candidate_ok or within_grace

            if effective_candidate:
                stable_for = now - float(state.get("candidate_since") or now)
                sample_count = int(state.get("candidate_samples", 0))
                persisted = (
                    stable_for >= settings.candidate_persistence_seconds
                    and sample_count >= settings.entry_stability_samples
                )
                if now < state.get("cooldown_until", 0.0):
                    decision = "WATCH"
                    if "cooldown" not in entry_blockers:
                        entry_blockers.append("cooldown")
                elif persisted:
                    decision = "ENTRY CONFIRMED"
                    state["confirmed_at"] = now
                    state["confirmed_side"] = side
                    state["confirmed_direction"] = direction
                    state["confirmed_setup"] = setup_name
                    state["confirmed_invalidation"] = self._num(snap.get("invalidation_level"))
                    state["soft_exit_since"] = None
                else:
                    decision = "ENTRY CANDIDATE"
            elif setup_exists:
                decision = "WATCH"
            else:
                decision = "NO TRADE"

        if decision == "ENTRY INVALIDATED":
            state["cooldown_until"] = now + settings.cooldown_seconds
            state["last_exit_reason"] = exit_reason

        state["decision_state"] = decision
        state["last_direction"] = direction

        entry_age_seconds = None
        if state.get("confirmed_at") is not None:
            entry_age_seconds = round(max(0.0, now - state["confirmed_at"]), 2)

        snap.update({
            "decision_version": "q15-v4-learning-safety",
            "decision_state": decision,
            "entry_status": decision,
            "entry_side": side,
            "pattern_direction": direction or "none",
            "pattern_detected": setup_name or snap.get("pattern_detected") or "none",
            "follow_through_confirmed": follow.follow_through,
            "follow_through_reason": follow.reason,
            "estimated_fair_probability": round(fair_yes, 5) if fair_yes is not None else None,
            "raw_fair_probability": round(raw_fair_yes, 5) if raw_fair_yes is not None else None,
            "raw_side_win_probability": round(raw_win_prob, 5) if raw_win_prob is not None else None,
            "calibrated_side_win_probability": round(calibration_info.get("calibrated_probability"), 5) if calibration_info.get("calibrated_probability") is not None else None,
            "calibration_version": calibration_info.get("version") or "identity",
            "calibration_applied_live": bool(calibration_info.get("apply_live")),
            "raw_model_probability": round(estimate.raw_probability, 5) if estimate.raw_probability is not None else None,
            "fair_probability_method": estimate.method,
            # This measures data/model-input quality, not empirical accuracy.
            "model_confidence": round(estimate.confidence, 3),
            "model_quality_not_accuracy": True,
            "model_sigma_per_sqrt_second": estimate.sigma_per_sqrt_second,
            "model_history_points": estimate.history_points,
            "market_mid_probability": estimate.market_mid,
            "momentum_z_adjustment": round(estimate.momentum_z, 4),
            "win_prob": round(win_prob, 5) if win_prob is not None else None,
            "entry_ask": round(executable_ask, 4) if executable_ask is not None else None,
            "entry_vwap_size": settings.entry_size_contracts,
            "entry_vwap_filled": round(filled, 2),
            "entry_fee": round(fee_per_contract, 4) if fee_per_contract is not None else None,
            "fee_adjusted_break_even": round(break_even, 4) if break_even is not None else None,
            "estimated_edge": edge,
            "required_edge": required_edge,
            "max_entry_price": max_buy_price,
            "confirmation_groups": confirmations.as_dict()["groups"],
            "confirmation_reasons": confirmations.reasons,
            "confirmation_count": confirmations.active_group_count,
            "confirmation_group_count": confirmations.active_group_count,
            "confirmation_score": confirmations.total_score,
            "is_confirmed": decision == "ENTRY CONFIRMED",
            "entry_blockers": entry_blockers,
            "monitor_warnings": monitor_warnings,
            "exit_reason": exit_reason,
            "entry_age_seconds": entry_age_seconds,
            "entry_stability_samples": int(state.get("candidate_samples", 0)),
            "entry_stability_required_samples": settings.entry_stability_samples,
            "entry_stability_required_seconds": settings.candidate_persistence_seconds,
            "position_state": "ACTIVE" if decision == "ENTRY CONFIRMED" and state.get("confirmed_at") else "INACTIVE",
            "data_warning": data_warning,
            "learned_suppressed": bool(learned.get("suppressed")),
            "learning_adjustment": float(learned.get("edge_adjustment", 0.0) or 0.0),
            "data_age_seconds": round(data_age, 3),
            "ws_connected": bool(ws_health.get("connected")),
            "broader_returns_60s": broader_returns,
        })

        old_score = self._num(snap.get("opportunity_score"), 0.0)
        edge_score = max(0.0, edge or 0.0) * 4.0
        snap["opportunity_score"] = round(max(old_score, confirmations.total_score + edge_score), 1)

        if clear_confirmed_after_output:
            self._clear_confirmed(state)
            self._reset_candidate(state)

        return snap

    def _entry_blockers_without_two_window(
        self,
        *,
        snap,
        follow,
        side,
        seconds,
        confirmations,
        estimate,
        win_prob,
        edge,
        required_edge,
        spread,
        depth,
        full_size_available,
        data_warning,
        learned,
    ) -> list[str]:
        s = self.settings
        blockers: list[str] = []
        if snap.get("market_state") != "live":
            blockers.append("market_not_live")
        if side is None:
            blockers.append("no_directional_setup")
        if not follow.follow_through:
            blockers.append("waiting_for_next_candle_follow_through")
        if follow.invalidated:
            blockers.append(follow.reason or "setup_invalidated")
        if seconds is None or seconds < s.min_new_entry_seconds:
            blockers.append(f"too_late_under_{s.min_new_entry_seconds}s")

        # Adaptive confirmation tier. A setup with the full group count uses the
        # base thresholds; a setup with fewer groups (down to adaptive_min_groups)
        # may still qualify under a stricter score / edge / model-quality bar.
        groups = confirmations.active_group_count
        score = confirmations.total_score
        if groups >= s.min_confirmation_groups:
            req_score = s.min_confirmation_score
            req_extra_edge = 0.0
            req_model = s.min_model_confidence
        elif s.adaptive_confirmation_enabled and groups >= s.adaptive_min_groups:
            req_score = s.adaptive_min_score
            req_extra_edge = s.adaptive_extra_edge_cents
            req_model = max(s.min_model_confidence, s.adaptive_min_model_quality)
        else:
            req_score = None
            req_extra_edge = 0.0
            req_model = s.min_model_confidence
        effective_required_edge = round(required_edge + req_extra_edge, 2)

        if req_score is None:
            min_groups = s.adaptive_min_groups if s.adaptive_confirmation_enabled else s.min_confirmation_groups
            blockers.append(f"need_{min_groups}_independent_groups")
        elif score < req_score:
            blockers.append(f"confirmation_score_below_{req_score:g}")
        if estimate.probability_yes is None:
            blockers.append("fair_probability_unavailable")
        if estimate.confidence < req_model:
            blockers.append(f"model_quality_below_{req_model:.2f}")
        if win_prob is None or win_prob < s.min_win_probability:
            blockers.append(f"win_probability_below_{s.min_win_probability:.2f}")
        if edge is None or edge < effective_required_edge:
            blockers.append(f"edge_below_required_{effective_required_edge:.1f}c")
        if spread is None or spread > s.max_spread_cents:
            blockers.append("spread_too_wide")
        if spread is not None and spread > 2.0 and (depth is None or depth < s.wide_spread_min_depth):
            blockers.append("insufficient_depth_for_wide_spread")
        if depth is None or depth < s.min_depth_contracts:
            blockers.append("thin_best_ask_depth")
        if not full_size_available:
            blockers.append("insufficient_depth_for_configured_size")
        if data_warning:
            blockers.append("stale_snapshot")
        if learned.get("suppressed"):
            blockers.append("learned_segment_suppression")
        return blockers

    # Q15_TWO_WINDOW_ENTRY_GATE
    def _entry_blockers(self, *args, **kwargs):
        snap = kwargs.get("snap") or {}
        if snap.get("q15_new_entry_allowed") is False:
            return ["two_prediction_focus_gate"]
        blockers = list(self._entry_blockers_without_two_window(*args, **kwargs) or [])
        if adaptive_confirmation_ok(
            snapshot=snap,
            confirmations=kwargs.get("confirmations"),
            estimate=kwargs.get("estimate"),
            edge=kwargs.get("edge"),
            required_edge=kwargs.get("required_edge"),
            settings=getattr(self, "settings", None),
        ):
            blockers = [
                blocker for blocker in blockers
                if not (
                    "confirmation" in str(blocker).lower()
                    and ("group" in str(blocker).lower() or "score" in str(blocker).lower())
                )
            ]
        return blockers

    def _hard_exit_reason(self, *, snap, state, observed_side, observed_follow_through) -> str | None:
        confirmed_side = state.get("confirmed_side")
        underlying = self._num(snap.get("underlying_current"))
        invalidation = state.get("confirmed_invalidation")
        if invalidation is None:
            invalidation = self._num(snap.get("invalidation_level"))
        if underlying is not None and invalidation is not None:
            if confirmed_side == "YES" and underlying < invalidation:
                return "price_crossed_frozen_invalidation_level"
            if confirmed_side == "NO" and underlying > invalidation:
                return "price_crossed_frozen_invalidation_level"
        if (
            observed_follow_through
            and observed_side in {"YES", "NO"}
            and observed_side != confirmed_side
        ):
            return "opposite_setup_confirmed"
        return None

    def _economics(self, snap: dict, side: str | None, win_prob: float | None, required_edge: float) -> dict:
        s = self.settings
        ask_levels = snap.get("yes_ask_levels") if side == "YES" else snap.get("no_ask_levels") if side == "NO" else None
        best_ask = self._num(snap.get("yes_ask")) if side == "YES" else self._num(snap.get("no_ask")) if side == "NO" else None
        vwap, filled = vwap_for_quantity(ask_levels, s.entry_size_contracts)
        executable_ask = vwap if vwap is not None and filled >= s.entry_size_contracts - 1e-9 else best_ask
        full_size_available = bool(
            executable_ask is not None and (not ask_levels or filled >= s.entry_size_contracts - 1e-9)
        )
        fee_per_contract = break_even = edge = max_buy_price = None
        if executable_ask is not None and win_prob is not None:
            total_fee = estimated_fee_cents(executable_ask, s.entry_size_contracts, s.fee_rate)
            fee_per_contract = total_fee / max(s.entry_size_contracts, 1e-9)
            break_even = executable_ask + fee_per_contract
            fair_side_cents = win_prob * 100.0
            edge = round(fair_side_cents - break_even, 2)
            tick = self._num(snap.get("price_tick_cents"), s.default_tick_cents)
            max_buy_price = max_entry_price_cents(
                fair_side_cents,
                required_edge,
                contracts=s.entry_size_contracts,
                fee_rate=s.fee_rate,
                tick_cents=tick,
            )
        depth = self._num(snap.get("yes_ask_qty")) if side == "YES" else self._num(snap.get("no_ask_qty")) if side == "NO" else None
        if ask_levels:
            try:
                depth = sum(
                    float(level[1])
                    for level in ask_levels
                    if len(level) >= 2 and float(level[0]) <= float(executable_ask or 0) + 0.0001
                )
            except (TypeError, ValueError):
                pass
        return {
            "ask": executable_ask,
            "filled": filled,
            "full_size_available": full_size_available,
            "fee": fee_per_contract,
            "break_even": break_even,
            "edge": edge,
            "max_buy_price": max_buy_price,
            "depth": depth,
        }

    @staticmethod
    def _side_probability(fair_yes, side):
        if fair_yes is None or side not in {"YES", "NO"}:
            return None
        return fair_yes if side == "YES" else 1.0 - fair_yes

    def _asset_state(self, asset: str, ticker: str | None) -> dict:
        state = self._state.get(asset)
        if state is None or state.get("ticker") != ticker:
            state = {
                "ticker": ticker,
                "candidate_since": None,
                "candidate_last_ok": None,
                "candidate_side": None,
                "candidate_samples": 0,
                "decision_state": "NO TRADE",
                "cooldown_until": 0.0,
                "confirmed_at": None,
                "confirmed_side": None,
                "confirmed_direction": None,
                "confirmed_setup": None,
                "confirmed_invalidation": None,
                "soft_exit_since": None,
                "last_exit_reason": None,
            }
            self._state[asset] = state
        return state

    @staticmethod
    def _reset_candidate(state: dict) -> None:
        state["candidate_since"] = None
        state["candidate_last_ok"] = None
        state["candidate_side"] = None
        state["candidate_samples"] = 0

    @staticmethod
    def _clear_confirmed(state: dict) -> None:
        state["confirmed_at"] = None
        state["confirmed_side"] = None
        state["confirmed_direction"] = None
        state["confirmed_setup"] = None
        state["confirmed_invalidation"] = None
        state["soft_exit_since"] = None

    def _learned_adjustment(self, asset, seconds, signal_type, side):
        if not self.learner or not side:
            return {"edge_adjustment": 0.0, "suppressed": False, "reasons": []}
        bucket = self._time_bucket(seconds)
        try:
            return self.learner.adjustment_for(asset, bucket, signal_type, side)
        except Exception:
            return {"edge_adjustment": 0.0, "suppressed": False, "reasons": []}

    @staticmethod
    def _time_bucket(seconds):
        if seconds is None:
            return "unknown"
        if seconds >= 720:
            return "12-15m"
        if seconds >= 360:
            return "6-12m"
        if seconds >= 120:
            return "2-6m"
        return "60-120s" if seconds >= 60 else "<60s"

    @staticmethod
    def _data_age(snapshot: dict, now: float) -> float:
        value = snapshot.get("last_updated")
        if not value:
            return 0.0
        try:
            return max(0.0, now - datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
        except Exception:
            return 0.0

    @staticmethod
    def _num(value, default=None):
        try:
            if value is None:
                return default
            value = float(value)
            return value if math.isfinite(value) else default
        except (TypeError, ValueError):
            return default


def attach_orderbook_levels(snapshot: dict, parsed_orderbook: dict, market: dict | None = None) -> dict:
    """Attach depth and pricing metadata that the original AssetEngine omitted."""
    for key in ("yes_bid_levels", "yes_ask_levels", "no_bid_levels", "no_ask_levels"):
        snapshot[key] = parsed_orderbook.get(key) or []
    if market:
        snapshot["target_type"] = market.get("strike_type", snapshot.get("target_type", "greater_or_equal"))
        structure = market.get("price_level_structure")
        snapshot["price_level_structure"] = structure
        ask = snapshot.get("yes_ask")
        if structure == "deci_cent" or (structure == "tapered_deci_cent" and ask is not None and (ask < 10 or ask > 90)):
            snapshot["price_tick_cents"] = 0.1
        else:
            snapshot["price_tick_cents"] = 1.0 if structure == "linear_cent" else 0.1
    return snapshot
