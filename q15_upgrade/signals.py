from __future__ import annotations

import logging
import threading

from .config import Q15Settings

logger = logging.getLogger(__name__)

ACTIVE_STATES = {"WATCH", "ENTRY CANDIDATE", "ENTRY CONFIRMED", "REVERSAL WARNING"}
DISCLAIMER = "Model-based estimate, not financial advice. Read-only monitor — no orders are placed."


class SignalEngine:
    """Alert/persistence layer for decisions already made by Q15Runtime.

    This class intentionally does not recompute confirmations or entry gates.
    That removes the old conflict where three modules each meant something
    different by "confirmed."
    """

    def __init__(self, config, store, notifier, learner=None):
        self.config = config
        self.settings = Q15Settings()
        self.store = store
        self.notifier = notifier
        self.learner = learner
        self.assets = {}
        self._lock = threading.Lock()
        self._active_signals = []
        self.alerts_generated = 0

    def _asset_state(self, asset, ticker):
        state = self.assets.get(asset)
        if state is None or state.get("ticker") != ticker:
            state = {
                "ticker": ticker,
                "last_state": "NO TRADE",
                "watch_sent": False,
                "confirmed_sent": {"YES": False, "NO": False},
                "last_alert_edge": {"YES": None, "NO": None},
                "had_entry": False,
            }
            self.assets[asset] = state
        return state

    def evaluate_all(self, snapshots, now, ws_health=None):
        evaluations = {}
        for asset, snapshot in snapshots.items():
            if not isinstance(snapshot, dict):
                continue
            try:
                evaluations[asset] = self._from_snapshot(asset, snapshot)
            except Exception as exc:
                logger.error("V2 signal evaluation failed for %s: %s", asset, exc)

        if self.settings.correlation_control:
            self._apply_correlation(evaluations)

        active = []
        for asset, evaluation in evaluations.items():
            try:
                self._finalize(asset, evaluation, now)
            except Exception as exc:
                logger.error("V2 signal finalize failed for %s: %s", asset, exc)
            if evaluation["state"] in ACTIVE_STATES:
                active.append(evaluation["public"])
        active.sort(key=lambda row: row.get("ranking_score", -999), reverse=True)
        with self._lock:
            self._active_signals = active

    def _from_snapshot(self, asset, snapshot):
        state = snapshot.get("decision_state") or snapshot.get("entry_status") or "NO TRADE"
        side = snapshot.get("entry_side")
        edge = snapshot.get("estimated_edge")
        confidence = snapshot.get("model_confidence")
        confirmation_score = snapshot.get("confirmation_score") or 0.0
        depth = snapshot.get("yes_ask_qty") if side == "YES" else snapshot.get("no_ask_qty") if side == "NO" else 0.0
        try:
            ranking = float(edge or 0.0) * max(0.1, float(confidence or 0.0)) + float(confirmation_score) / 10.0 + min(3.0, float(depth or 0.0) / 100.0)
        except (TypeError, ValueError):
            ranking = -999.0
        evaluation = {
            "asset": asset,
            "ticker": snapshot.get("ticker"),
            "state": state,
            "side": side,
            "direction": snapshot.get("pattern_direction"),
            "pattern": snapshot.get("pattern_detected"),
            "edge": edge,
            "fair_prob": snapshot.get("estimated_fair_probability"),
            "win_prob": snapshot.get("win_prob"),
            "model_confidence": confidence,
            "entry_ask": snapshot.get("entry_ask"),
            "max_entry_price": snapshot.get("max_entry_price"),
            "fee": snapshot.get("entry_fee"),
            "break_even": snapshot.get("fee_adjusted_break_even"),
            "spread": snapshot.get("spread"),
            "confirmations": snapshot.get("confirmation_reasons") or [],
            "confirmation_groups": snapshot.get("confirmation_groups") or {},
            "confirmation_count": int(snapshot.get("confirmation_group_count") or snapshot.get("confirmation_count") or 0),
            "confirmation_score": snapshot.get("confirmation_score"),
            "seconds": snapshot.get("seconds_remaining"),
            "data_age": snapshot.get("data_age_seconds"),
            "invalidation": snapshot.get("invalidation_level"),
            "blockers": snapshot.get("entry_blockers") or [],
            "monitor_warnings": snapshot.get("monitor_warnings") or [],
            "exit_reason": snapshot.get("exit_reason"),
            "entry_age_seconds": snapshot.get("entry_age_seconds"),
            "decision_version": snapshot.get("decision_version"),
            "end_prediction": snapshot.get("end_prediction"),
            "learning_version": snapshot.get("learning_version"),
            "required_edge": snapshot.get("required_edge"),
            "learning_adjustment": snapshot.get("learning_adjustment"),
            "learned_suppressed": snapshot.get("learned_suppressed", False),
            "target_distance_pct": snapshot.get("target_distance_pct"),
            "market_close": snapshot.get("close_time"),
            "entry_size": snapshot.get("entry_vwap_size"),
            "entry_filled": snapshot.get("entry_vwap_filled"),
            "is_alternative": False,
            "primary_asset": None,
            "alternatives": [],
            "ranking_score": round(ranking, 3),
        }
        evaluation["public"] = self._public(evaluation)
        return evaluation

    def _apply_correlation(self, evaluations):
        groups = {"bullish": [], "bearish": []}
        for evaluation in evaluations.values():
            if evaluation["state"] in {"ENTRY CANDIDATE", "ENTRY CONFIRMED"} and evaluation["direction"] in groups:
                groups[evaluation["direction"]].append(evaluation)
        for direction, members in groups.items():
            if len(members) < 2:
                continue
            members.sort(key=lambda row: row["ranking_score"], reverse=True)
            primary = members[0]
            primary["alternatives"] = [row["asset"] for row in members[1:]]
            for row in members[1:]:
                row["is_alternative"] = True
                row["primary_asset"] = primary["asset"]
            for row in members:
                row["public"] = self._public(row)

    def _finalize(self, asset, evaluation, now):
        state = self._asset_state(asset, evaluation["ticker"])
        current = evaluation["state"]
        previous = state["last_state"]
        side = evaluation["side"]
        sent = False
        suppressed_reason = None
        send_kind = None

        if current == "WATCH" and self.settings.send_watch_alerts:
            if not state["watch_sent"]:
                send_kind = "watch"
            else:
                suppressed_reason = "watch_already_sent"
        elif current == "ENTRY CONFIRMED":
            if evaluation["is_alternative"]:
                suppressed_reason = f"correlated_alternative:{evaluation['primary_asset']}"
            elif side not in {"YES", "NO"}:
                suppressed_reason = "missing_side"
            elif state["confirmed_sent"].get(side):
                previous_edge = state["last_alert_edge"].get(side)
                improvement = float(evaluation["edge"] or 0.0) - float(previous_edge or 0.0)
                repeat_threshold = float(getattr(self.config, "repeat_edge_improvement", 2.0))
                if improvement >= repeat_threshold:
                    send_kind = "entry"
                else:
                    suppressed_reason = "entry_already_sent"
            else:
                send_kind = "entry"
        elif current in {"ENTRY INVALIDATED", "REVERSAL WARNING"}:
            if self.settings.send_invalidation_alerts and state.get("had_entry"):
                send_kind = "invalidated"
            else:
                suppressed_reason = "no_alerted_entry_to_invalidate"

        transitioned = current != previous
        do_log = transitioned
        if send_kind:
            do_log = True
            # V9_LATCH_AFTER_DURABLE_QUEUE: latches are set after outbox acceptance.

            if getattr(self.config, "alerts_enabled", True) and getattr(self.notifier, "enabled", False):
                key = self._claim_key(send_kind, evaluation, now)
                if True:  # V9_OUTBOX_OWNS_DEDUP: atomic dedup occurs in persistent outbox
                    if send_kind == "entry":
                        evaluation["track_record"] = self.store.entry_record()
                        total = int((evaluation["track_record"] or {}).get("total") or 0)
                        evaluation["actionable"] = bool(
                            self.settings.actionable_alerts
                            and total >= self.settings.min_settled_for_actionable
                        )
                    if self.notifier.send(self._format(send_kind, evaluation), idempotency_key=key):
                        sent = True
                        self.alerts_generated += 1
                        if send_kind == "watch":
                            state["watch_sent"] = True
                        elif send_kind == "entry" and side in {"YES", "NO"}:
                            state["confirmed_sent"][side] = True
                            state["last_alert_edge"][side] = evaluation["edge"]
                            state["had_entry"] = True
                        if send_kind == "entry" and self.learner and hasattr(self.learner, "record_alert_sent"):
                            try:
                                self.learner.record_alert_sent(evaluation)
                            except Exception:
                                pass
                    else:
                        suppressed_reason = "telegram_send_failed"
                else:
                    suppressed_reason = "duplicate_cross_instance"
            else:
                suppressed_reason = "notifications_disabled"

        if current in {"ENTRY INVALIDATED", "REVERSAL WARNING"}:
            state["watch_sent"] = False
            state["confirmed_sent"] = {"YES": False, "NO": False}
            state["last_alert_edge"] = {"YES": None, "NO": None}
            state["had_entry"] = False

        if do_log:
            self.store.insert_signal({
                "asset": asset,
                "ticker": evaluation["ticker"],
                "market_close": evaluation["market_close"],
                "side": side,
                "state": current,
                "signal_type": evaluation["pattern"],
                "edge": evaluation["edge"],
                "fair_prob": evaluation["fair_prob"],
                "break_even": evaluation["break_even"],
                "entry_ask": evaluation["entry_ask"],
                "fee": evaluation["fee"],
                "spread": evaluation["spread"],
                "confirmations": evaluation["confirmations"],
                "confirmation_count": evaluation["confirmation_count"],
                "data_age": evaluation["data_age"],
                "seconds_remaining": evaluation["seconds"],
                "target_distance_pct": evaluation["target_distance_pct"],
                "sent": sent,
                "suppressed_reason": suppressed_reason,
                "correlation_group": evaluation["direction"] if evaluation["alternatives"] or evaluation["is_alternative"] else None,
                "is_alternative": evaluation["is_alternative"],
                "required_edge": evaluation["required_edge"],
                "learning_adjustment": evaluation["learning_adjustment"],
                "win_prob": evaluation["win_prob"],
            })

        state["last_state"] = current
        evaluation["public"] = self._public(evaluation)

    @staticmethod
    def _claim_key(kind, evaluation, now):
        side = evaluation.get("side") or "none"
        if kind == "entry":
            return f"q15v3:entry:{evaluation['ticker']}:{side}"
        if kind == "watch":
            return f"q15v3:watch:{evaluation['ticker']}:{side}"
        return f"q15v3:invalidate:{evaluation['ticker']}:{side}:{int(now // 10)}"

    def _public(self, evaluation):
        return {
            "asset": evaluation["asset"],
            "ticker": evaluation["ticker"],
            "state": evaluation["state"],
            "side": evaluation["side"],
            "direction": evaluation["direction"],
            "pattern": evaluation["pattern"],
            "edge": evaluation["edge"],
            "required_edge": evaluation["required_edge"],
            "estimated_fair_probability": evaluation["fair_prob"],
            "win_prob": evaluation["win_prob"],
            "model_confidence": evaluation["model_confidence"],
            "entry_ask": evaluation["entry_ask"],
            "max_entry_price": evaluation["max_entry_price"],
            "fee": evaluation["fee"],
            "break_even": evaluation["break_even"],
            "spread": evaluation["spread"],
            "confirmations": evaluation["confirmations"],
            "confirmation_groups": evaluation["confirmation_groups"],
            "confirmation_count": evaluation["confirmation_count"],
            "confirmation_score": evaluation["confirmation_score"],
            "seconds_remaining": evaluation["seconds"],
            "data_age_seconds": evaluation["data_age"],
            "invalidation_level": evaluation["invalidation"],
            "entry_blockers": evaluation["blockers"],
            "monitor_warnings": evaluation.get("monitor_warnings", []),
            "exit_reason": evaluation.get("exit_reason"),
            "entry_age_seconds": evaluation.get("entry_age_seconds"),
            "is_alternative": evaluation["is_alternative"],
            "primary_asset": evaluation["primary_asset"],
            "alternatives": evaluation["alternatives"],
            "ranking_score": evaluation["ranking_score"],
            "decision_version": evaluation.get("decision_version") or "q15-v4-learning-safety",
            "end_prediction": evaluation.get("end_prediction"),
            "learning_version": evaluation.get("learning_version") or "q15-learning-v4",
        }

    def _format(self, kind, evaluation):
        seconds = evaluation.get("seconds")
        if isinstance(seconds, (int, float)):
            minutes = f"{int(seconds) // 60}m{int(seconds) % 60:02d}s"
        else:
            minutes = "?"
        edge = evaluation.get("edge")
        fair = evaluation.get("fair_prob")
        win_prob = evaluation.get("win_prob")
        ask = evaluation.get("entry_ask")
        maximum = evaluation.get("max_entry_price")
        record = evaluation.get("track_record") or {}
        actionable = bool(evaluation.get("actionable", False))

        if kind == "entry":
            if actionable:
                header = f"🟢 <b>ENTRY SIGNAL — {evaluation['asset']} {evaluation['side']}</b>"
                if ask is not None and maximum is not None:
                    action = (
                        f"👉 <b>Preferred limit {ask:.2f}¢ or better</b> — "
                        f"absolute model max {maximum:.2f}¢"
                    )
                elif ask is not None:
                    action = f"👉 <b>Preferred limit near {ask:.2f}¢</b>"
                else:
                    action = f"👉 <b>Entry side: {evaluation['side']}</b>"
            else:
                header = f"🟡 <b>PAPER SIGNAL — {evaluation['asset']} {evaluation['side']}</b>"
                minimum = self.settings.min_settled_for_actionable
                action = (
                    "🧪 <b>Do not place a real-money trade from this alert.</b> "
                    f"Actionable mode requires Q15_ACTIONABLE_ALERTS=true and at least {minimum} settled signals."
                )
        elif kind == "watch":
            header = f"👀 <b>WATCH — {evaluation['asset']} {evaluation.get('side') or ''}</b>"
            blockers = ", ".join(evaluation.get("blockers", [])[:3]) or "waiting for full confirmation"
            action = f"⏳ Do not enter yet: {blockers}"
        else:
            header = f"⚠️ <b>EXIT / INVALIDATED — {evaluation['asset']}</b>"
            reason = evaluation.get("exit_reason") or "hard_or_sustained_exit_condition"
            action = f"The prior confirmed setup is invalid. Reason: <b>{reason}</b>."

        lines = [header, action]
        lines.append(f"Setup: {evaluation.get('pattern')} ({evaluation.get('direction')})")
        prediction = evaluation.get("end_prediction") or {}
        if prediction.get("predicted_result"):
            probability = prediction.get("blended_yes_probability")
            confidence = prediction.get("confidence")
            probability_text = f"{float(probability) * 100:.1f}% P(YES)" if probability is not None else "probability unavailable"
            confidence_text = f"{float(confidence) * 100:.0f}% chart quality" if confidence is not None else ""
            lines.append(
                f"Chart end estimate: {prediction.get('predicted_result')} — {probability_text} "
                f"{confidence_text} (not guaranteed)"
            )
        if edge is not None:
            lines.append(f"Net model edge: {edge:+.2f}¢ (entry requirement {evaluation.get('required_edge')}¢)")
        if fair is not None:
            lines.append(f"P(YES): {fair * 100:.1f}% | side estimate: {(win_prob or 0) * 100:.1f}%")
        lines.append(
            f"Independent groups: {evaluation.get('confirmation_count')}/4 | score {evaluation.get('confirmation_score')}"
        )
        lines.append(
            f"Model data-quality score: {(evaluation.get('model_confidence') or 0) * 100:.0f}% "
            f"(not historical accuracy) | spread {evaluation.get('spread')}¢"
        )
        if evaluation.get("monitor_warnings"):
            lines.append("Monitor warnings: " + ", ".join(evaluation["monitor_warnings"][:3]))
        if evaluation.get("entry_age_seconds") is not None:
            lines.append(f"Alert age: {float(evaluation['entry_age_seconds']):.0f}s")
        lines.append(f"Time left: {minutes}")
        if evaluation.get("alternatives"):
            lines.append("Correlated alternatives: " + ", ".join(evaluation["alternatives"]))
        if record.get("total"):
            lines.append(
                f"📊 Settled record: {record['wins']}✅ / {record['losses']}❌ "
                f"({record['win_rate'] * 100:.0f}%, n={record['total']}; small samples are unstable)"
            )
        dashboard = getattr(self.config, "dashboard_url", None)
        if dashboard:
            lines.append(f"Dashboard: {dashboard}")
        lines.append(f"<i>{DISCLAIMER}</i>")
        return "\n".join(lines)

    def get_active_signals(self):
        with self._lock:
            return list(self._active_signals)
