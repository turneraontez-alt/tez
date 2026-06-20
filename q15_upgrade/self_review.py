"""Final 10m prediction self-review.

Strictly read-only / paper-only. This module never submits, modifies, or cancels
orders. When a final 10-minute prediction settles against the real Kalshi
result it:

  1. grades the prediction ``correct`` / ``wrong`` / ``void``;
  2. attributes the outcome to a small, FIXED vocabulary of structured reason
     tags (a failure mode for wrong predictions, a success pattern for wins),
     read from the features captured at decision time;
  3. persists a post-mortem row plus an aggregated win/loss pattern rollup; and
  4. auto-adapts a few internal *decision thresholds* (bounded, reversible, and
     gated by the learning circuit breakers) when a failure mode that maps to a
     threshold keeps repeating.

Auto-adaptation only tightens or relaxes the in-memory ``FocusSettings``
thresholds that gate which predictions are surfaced. There is no order
execution to touch. Adjustments are clamped to safe ranges, recorded as events,
and reverted automatically when they do not improve subsequent outcomes.

The authoritative state lives in memory (so the logic is fully exercised even
when no database is attached, e.g. in unit tests) and is mirrored to / hydrated
from Postgres so reviews, patterns, and active adaptations survive restarts.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

VERSION = "q15-final10m-self-review-v1"
DISCLAIMER = "Self-review of paper predictions only. Read-only monitor — no orders are placed."

# ---------------------------------------------------------------------------
# Fixed, documented attribution vocabulary.
# ---------------------------------------------------------------------------
# Failure modes attributed to a WRONG final 10m prediction.
FAILURE_MODES = (
    "model_overconfident",   # the fair-value model component strongly backed the (wrong) side
    "distance_misleading",   # the price-distance component strongly backed the (wrong) side
    "flow_contradicted",     # order flow / microstructure pointed the other way and was right
    "weak_confirmation",     # too few confirmation groups / low confirmation score at decision
    "low_consensus",         # rolling-vote consensus barely cleared its threshold
    "thin_evidence",         # very small rolling-vote sample backed the call
    "ignored_conflict",      # the 10m call flipped away from the 15m call and the flip was wrong
    "drift_active",          # a learning circuit breaker (feature drift) was active at decision
    "unclassified_loss",     # none of the above matched
)
# Success patterns attributed to a CORRECT final 10m prediction.
SUCCESS_PATTERNS = (
    "decisive_model",        # the model component decisively (and correctly) backed the side
    "high_consensus",        # rolling-vote consensus comfortably above its threshold
    "flow_aligned",          # order flow / microstructure agreed with the winning side
    "strong_confirmation",   # >=3 confirmation groups with a strong score
    "clean_agreement",       # 15m and 10m agreed on the winning side
    "unclassified_win",      # none of the above matched
)

# Each adaptable failure mode maps one-to-one to exactly one decision threshold.
# Keeping the mapping one-to-one avoids two failure modes fighting over the same
# knob. Failure modes that lack a single safe knob (distance_misleading,
# flow_contradicted, weak_confirmation, ignored_conflict, drift_active) are
# recorded for diagnostics but never drive an auto-adaptation.
#   attr: FocusSettings attribute to nudge
#   step: bounded step per nudge (toward "tighter")
#   span: how far above baseline the value may travel
#   hard_max: absolute clamp regardless of baseline
#   integer: whether the setting is an int
ADAPT_RULES: Dict[str, dict] = {
    "model_overconfident": {"attr": "initial_side_probability", "step": 0.01, "span": 0.12, "hard_max": 0.75, "integer": False},
    "low_consensus": {"attr": "initial_consensus", "step": 0.01, "span": 0.12, "hard_max": 0.90, "integer": False},
    "thin_evidence": {"attr": "min_vote_samples", "step": 1, "span": 4, "hard_max": 60, "integer": True},
}


def _iso(epoch: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(epoch, timezone.utc) if epoch is not None else datetime.now(timezone.utc)
    return dt.isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _num(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or isinstance(value, bool):
            return default
        parsed = float(value)
        return parsed
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class SelfReviewEngine:
    """Grade, diagnose, persist, and (optionally) auto-adapt from final 10m predictions."""

    def __init__(self, store=None, settings=None):
        self.store = store
        self.settings = settings
        self._lock = threading.RLock()
        self._db_ready = False
        self._migration_attempted = False
        self._last_error: Optional[str] = None
        self._reviews: Deque[dict] = deque(maxlen=200)
        # pattern_key -> {reason, kind, n, wins, losses, last_seen}
        self._patterns: Dict[str, dict] = {}
        # setting attr -> adaptation state
        self._adapt: Dict[str, dict] = {}
        self._events: Deque[dict] = deque(maxlen=200)
        self._seen_keys: set = set()
        self._init_adapt_state()
        self._migrate()
        self._hydrate()

    # ----------------------------- config helpers ---------------------------
    def _flag(self, name: str, default):
        return getattr(self.settings, name, default) if self.settings is not None else default

    @property
    def enabled(self) -> bool:
        return bool(self._flag("self_review_enabled", True))

    @property
    def auto_adapt_enabled(self) -> bool:
        return bool(self._flag("self_review_auto_adapt", True))

    def _init_adapt_state(self) -> None:
        for reason, rule in ADAPT_RULES.items():
            attr = rule["attr"]
            baseline = _num(getattr(self.settings, attr, None)) if self.settings is not None else None
            if baseline is None:
                continue
            self._adapt[attr] = {
                "setting": attr,
                "reason": reason,
                "baseline": baseline,
                "value": baseline,
                "step": rule["step"],
                "min_value": baseline,
                "max_value": min(rule["hard_max"], baseline + rule["span"]),
                "integer": rule["integer"],
                "losses_since": 0,
                "samples_since": 0,
                "in_probation": False,
                "pre_nudge_loss_rate": 0.0,
                "post_nudge_losses": 0,
                "post_nudge_samples": 0,
                "decayed_samples": 0,
                "last_action": "init",
                "updated_at": _iso(),
            }

    # ----------------------------- public review ----------------------------
    def review(
        self,
        *,
        ticker: Optional[str],
        asset: Optional[str],
        market_close: Any,
        prediction: Optional[Mapping[str, Any]],
        agreement: Optional[str],
        decision_state: Optional[str],
        actual_result: str,
        now: Optional[float] = None,
        breaker_active: bool = False,
        prior_side: Optional[str] = None,
    ) -> dict:
        """Grade and diagnose one final 10m prediction. Returns the post-mortem."""
        now = time.time() if now is None else now
        prediction = dict(prediction or {})
        actual = str(actual_result or "").upper()
        side = str(prediction.get("side") or "UNCERTAIN").upper()
        features = dict(prediction.get("features") or {})
        decision_breaker = bool(features.get("circuit_breaker_active") or breaker_active)

        outcome, reason_tags, primary = self._diagnose(
            side=side, actual=actual, prediction=prediction, features=features,
            prior_side=prior_side, decision_breaker=decision_breaker,
        )
        record = {
            "review_key": f"{ticker}|{VERSION}",
            "ticker": ticker,
            "asset": asset,
            "market_close": market_close,
            "predicted_side": side,
            "blended_yes_probability": _num(prediction.get("yes_probability")),
            "side_probability": _num(prediction.get("side_probability")),
            "agreement": (agreement or "").upper() or None,
            "decision_state": (decision_state or "").upper() or None,
            "actual_result": actual,
            "outcome": outcome,
            "reason_tags": reason_tags,
            "primary_reason": primary,
            "feature_values": self._key_features(features, prediction),
            "circuit_breaker_active": decision_breaker,
            "created_at": _iso(now),
        }
        with self._lock:
            if record["review_key"] in self._seen_keys:
                return record
            self._seen_keys.add(record["review_key"])
            self._reviews.appendleft(record)
            self._update_patterns(outcome, reason_tags, now)
            self._persist_review(record)
            if outcome in {"correct", "wrong"}:
                # Gate on the breaker state both at decision time (frozen into the
                # features) and right now, so a loss produced under drift never
                # drives a threshold change.
                self._auto_adapt(outcome, reason_tags, now, breaker_active or decision_breaker)
        return record

    # ----------------------------- attribution ------------------------------
    def _diagnose(self, *, side, actual, prediction, features, prior_side, decision_breaker):
        if actual not in {"YES", "NO"}:
            return "void", ["void_market"], "void_market"
        if side not in {"YES", "NO"}:
            return "void", ["no_actionable_prediction"], "no_actionable_prediction"

        correct = side == actual
        positive = side == "YES"
        components = features.get("components") or {}

        def comp(name):
            return _num(components.get(name))

        def aligned(value, strong=False):
            if value is None:
                return False
            if positive:
                return value >= (0.62 if strong else 0.55)
            return value <= (0.38 if strong else 0.45)

        def opposed(value):
            if value is None:
                return False
            return value <= 0.45 if positive else value >= 0.55

        consensus = _num(prediction.get("consensus"))
        required_consensus = _num(prediction.get("required_consensus"))
        sample_count = _num(prediction.get("sample_count"))
        groups = _num(features.get("confirmation_groups"))
        score = _num(features.get("confirmation_score"))
        flow_skew = _num(features.get("flow_skew"))
        min_samples = _num(self._flag("min_vote_samples", 5), 5) or 5

        if correct:
            tags: List[str] = []
            if aligned(comp("model"), strong=True):
                tags.append("decisive_model")
            if consensus is not None and required_consensus is not None and consensus >= required_consensus + 0.08:
                tags.append("high_consensus")
            flow_dir_ok = aligned(comp("microstructure")) or (
                flow_skew is not None and ((positive and flow_skew > 0.05) or (not positive and flow_skew < -0.05))
            )
            if flow_dir_ok:
                tags.append("flow_aligned")
            if (groups or 0) >= 3 and (score or 0) >= 65:
                tags.append("strong_confirmation")
            if prior_side in {"YES", "NO"} and prior_side == side:
                tags.append("clean_agreement")
            if not tags:
                tags.append("unclassified_win")
            tags = [t for t in SUCCESS_PATTERNS if t in tags]
            return "correct", tags, tags[0]

        # wrong
        tags = []
        if aligned(comp("model"), strong=True):
            tags.append("model_overconfident")
        if aligned(comp("distance"), strong=True):
            tags.append("distance_misleading")
        flow_against = opposed(comp("microstructure")) or (
            flow_skew is not None and ((positive and flow_skew < -0.05) or (not positive and flow_skew > 0.05))
        )
        if flow_against:
            tags.append("flow_contradicted")
        if (groups is not None and groups < 3) or (score is not None and score < 60):
            tags.append("weak_confirmation")
        if consensus is not None and required_consensus is not None and consensus < required_consensus + 0.05:
            tags.append("low_consensus")
        if sample_count is not None and sample_count < min_samples * 1.5:
            tags.append("thin_evidence")
        if prior_side in {"YES", "NO"} and prior_side != side:
            tags.append("ignored_conflict")
        if decision_breaker:
            tags.append("drift_active")
        if not tags:
            tags.append("unclassified_loss")
        tags = [t for t in FAILURE_MODES if t in tags]
        return "wrong", tags, tags[0]

    @staticmethod
    def _key_features(features: Mapping[str, Any], prediction: Mapping[str, Any]) -> dict:
        components = features.get("components") or {}
        return {
            "model": _num(components.get("model")),
            "distance": _num(components.get("distance")),
            "chart": _num(components.get("chart")),
            "microstructure": _num(components.get("microstructure")),
            "component_agreement": _num(features.get("component_agreement")),
            "flow_skew": _num(features.get("flow_skew")),
            "orderbook_imbalance": _num(features.get("orderbook_imbalance")),
            "target_distance_pct": _num(features.get("target_distance_pct")),
            "model_quality": _num(features.get("model_quality")),
            "rsi_14": _num(features.get("rsi_14")),
            "confirmation_groups": _num(features.get("confirmation_groups")),
            "confirmation_score": _num(features.get("confirmation_score")),
            "consensus": _num(prediction.get("consensus")),
            "required_consensus": _num(prediction.get("required_consensus")),
            "sample_count": _num(prediction.get("sample_count")),
        }

    # ----------------------------- rollup -----------------------------------
    def _update_patterns(self, outcome: str, reason_tags: List[str], now: float) -> None:
        if outcome not in {"correct", "wrong"}:
            return
        kind = "win" if outcome == "correct" else "loss"
        for reason in reason_tags:
            key = f"{kind}|{reason}"
            row = self._patterns.get(key)
            if row is None:
                row = {"pattern_key": key, "reason": reason, "kind": kind, "n": 0, "wins": 0, "losses": 0, "last_seen": None}
                self._patterns[key] = row
            row["n"] += 1
            if kind == "win":
                row["wins"] += 1
            else:
                row["losses"] += 1
            row["last_seen"] = _iso(now)
            self._persist_pattern(row)

    # ----------------------------- auto-adapt -------------------------------
    def _auto_adapt(self, outcome: str, reason_tags: List[str], now: float, breaker_active: bool) -> None:
        wrong = outcome == "wrong"
        for attr, state in self._adapt.items():
            reason = state["reason"]
            state["samples_since"] += 1
            attributed = wrong and reason in reason_tags
            if attributed:
                state["losses_since"] += 1
            if state["in_probation"]:
                state["post_nudge_samples"] += 1
                if attributed:
                    state["post_nudge_losses"] += 1

            if not self.auto_adapt_enabled or breaker_active:
                # Gate: never adapt while a circuit breaker is active. Counters
                # keep accumulating so a later (breaker-clear) review can act.
                continue

            min_samples = int(_num(self._flag("self_review_adapt_min_samples", 8), 8) or 8)
            rate_threshold = float(_num(self._flag("self_review_adapt_loss_rate", 0.55), 0.55) or 0.55)
            eval_window = int(_num(self._flag("self_review_adapt_eval_window", 8), 8) or 8)
            decay_window = int(_num(self._flag("self_review_adapt_decay_window", 30), 30) or 30)

            if state["in_probation"]:
                self._evaluate_probation(state, eval_window, now)
                continue

            loss_rate = state["losses_since"] / state["samples_since"] if state["samples_since"] else 0.0
            if (
                state["losses_since"] >= min_samples
                and loss_rate >= rate_threshold
                and not self._at_tight_bound(state)
            ):
                self._nudge(state, loss_rate, now)
            elif (
                state["value"] != state["baseline"]
                and state["samples_since"] >= decay_window
                and loss_rate < rate_threshold / 2.0
            ):
                self._decay(state, now)

    def _at_tight_bound(self, state: dict) -> bool:
        return state["value"] >= state["max_value"] - 1e-9

    def _apply_value(self, state: dict, value: float) -> float:
        value = _clamp(value, state["min_value"], state["max_value"])
        if state["integer"]:
            value = int(round(value))
        state["value"] = value
        if self.settings is not None:
            # FocusSettings is a frozen dataclass; bypass the freeze to apply the
            # bounded live adaptation in place.
            object.__setattr__(self.settings, state["setting"], value)
        return value

    def _nudge(self, state: dict, loss_rate: float, now: float) -> None:
        old = state["value"]
        new = self._apply_value(state, state["value"] + state["step"])
        state["pre_nudge_loss_rate"] = loss_rate
        state["in_probation"] = True
        state["post_nudge_losses"] = 0
        state["post_nudge_samples"] = 0
        state["losses_since"] = 0
        state["samples_since"] = 0
        state["decayed_samples"] = 0
        state["last_action"] = "nudge"
        self._record_event(state, old, new, "nudge", f"{state['reason']} loss_rate={loss_rate:.2f}", now)

    def _evaluate_probation(self, state: dict, eval_window: int, now: float) -> None:
        if state["post_nudge_samples"] < eval_window:
            return
        post_rate = state["post_nudge_losses"] / state["post_nudge_samples"] if state["post_nudge_samples"] else 0.0
        if post_rate < state["pre_nudge_loss_rate"]:
            # The nudge helped: keep it, leave probation.
            state["in_probation"] = False
            state["last_action"] = "kept"
            state["losses_since"] = 0
            state["samples_since"] = 0
            self._record_event(state, state["value"], state["value"], "kept",
                               f"post_rate={post_rate:.2f}<pre={state['pre_nudge_loss_rate']:.2f}", now)
        else:
            # The nudge did not help: revert one step toward baseline.
            old = state["value"]
            new = self._apply_value(state, state["value"] - state["step"])
            state["in_probation"] = False
            state["last_action"] = "revert"
            state["losses_since"] = 0
            state["samples_since"] = 0
            self._record_event(state, old, new, "revert",
                               f"post_rate={post_rate:.2f}>=pre={state['pre_nudge_loss_rate']:.2f}", now)

    def _decay(self, state: dict, now: float) -> None:
        old = state["value"]
        toward = state["baseline"]
        step = state["step"] if toward >= state["value"] else -state["step"]
        new = self._apply_value(state, state["value"] + step)
        if (step > 0 and new > toward) or (step < 0 and new < toward):
            new = self._apply_value(state, toward)
        state["losses_since"] = 0
        state["samples_since"] = 0
        state["last_action"] = "decay"
        self._record_event(state, old, new, "decay", "outcomes_improved_relaxing_toward_baseline", now)

    def _record_event(self, state: dict, old: float, new: float, action: str, reason: str, now: float) -> None:
        state["updated_at"] = _iso(now)
        event = {
            "setting": state["setting"],
            "action": action,
            "old_value": old,
            "new_value": new,
            "delta": round(new - old, 6),
            "reason": reason,
            "trigger_pattern": state["reason"],
            "created_at": _iso(now),
        }
        self._events.appendleft(event)
        self._persist_event(event)
        self._persist_adapt_state(state)
        logger.info("self-review threshold %s %s %.4f->%.4f (%s)",
                    state["setting"], action, old, new, reason)

    # ----------------------------- status -----------------------------------
    def status(self, limit: int = 50) -> dict:
        with self._lock:
            adaptations = []
            for state in self._adapt.values():
                adaptations.append({
                    "setting": state["setting"],
                    "trigger_pattern": state["reason"],
                    "baseline": state["baseline"],
                    "current_value": state["value"],
                    "delta_from_baseline": round(state["value"] - state["baseline"], 6),
                    "min_value": state["min_value"],
                    "max_value": state["max_value"],
                    "in_probation": state["in_probation"],
                    "last_action": state["last_action"],
                    "losses_since_action": state["losses_since"],
                    "samples_since_action": state["samples_since"],
                    "active": state["value"] != state["baseline"],
                    "updated_at": state["updated_at"],
                })
            return {
                "version": VERSION,
                "read_only": True,
                "enabled": self.enabled,
                "auto_adapt_enabled": self.auto_adapt_enabled,
                "database_ready": self._db_ready,
                "last_error": self._last_error,
                "counts": {
                    "reviews": len(self._reviews),
                    "patterns": len(self._patterns),
                    "active_adaptations": sum(1 for a in adaptations if a["active"]),
                },
                "recent_reviews": list(self._reviews)[:limit],
                "patterns": sorted(self._patterns.values(), key=lambda r: r["n"], reverse=True),
                "adaptations": adaptations,
                "recent_adaptation_events": list(self._events)[:limit],
                "warning": DISCLAIMER,
            }

    # ----------------------------- persistence ------------------------------
    def _has_store(self) -> bool:
        return bool(self.store is not None and getattr(self.store, "enabled", False) and hasattr(self.store, "execute"))

    def _execute(self, sql: str, params: tuple = ()) -> bool:
        if not self._has_store():
            return False
        return bool(self.store.execute(sql, params))

    def _query(self, sql: str, params: tuple = ()) -> List[dict]:
        if not self.store or not hasattr(self.store, "query") or not getattr(self.store, "enabled", False):
            return []
        return self.store.query(sql, params) or []

    def _migrate(self) -> None:
        if self._migration_attempted:
            return
        self._migration_attempted = True
        if not self._has_store():
            return
        statements = [
            """
            CREATE TABLE IF NOT EXISTS q15_final10m_reviews (
                review_key TEXT PRIMARY KEY,
                ticker TEXT,
                asset TEXT,
                market_close TIMESTAMPTZ,
                predicted_side TEXT,
                blended_yes_probability DOUBLE PRECISION,
                side_probability DOUBLE PRECISION,
                agreement TEXT,
                decision_state TEXT,
                actual_result TEXT,
                outcome TEXT NOT NULL,
                reason_tags JSONB,
                primary_reason TEXT,
                feature_values JSONB,
                circuit_breaker_active BOOLEAN,
                model_version TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS q15_final10m_reviews_created ON q15_final10m_reviews(created_at DESC)",
            """
            CREATE TABLE IF NOT EXISTS q15_final10m_patterns (
                pattern_key TEXT PRIMARY KEY,
                reason TEXT NOT NULL,
                kind TEXT NOT NULL,
                n INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                last_seen TIMESTAMPTZ
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS q15_threshold_adapt_state (
                setting TEXT PRIMARY KEY,
                trigger_pattern TEXT NOT NULL,
                baseline DOUBLE PRECISION NOT NULL,
                value DOUBLE PRECISION NOT NULL,
                min_value DOUBLE PRECISION NOT NULL,
                max_value DOUBLE PRECISION NOT NULL,
                in_probation BOOLEAN NOT NULL DEFAULT FALSE,
                last_action TEXT,
                updated_at TIMESTAMPTZ NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS q15_threshold_adapt_events (
                id BIGSERIAL PRIMARY KEY,
                setting TEXT NOT NULL,
                action TEXT NOT NULL,
                old_value DOUBLE PRECISION,
                new_value DOUBLE PRECISION,
                delta DOUBLE PRECISION,
                reason TEXT,
                trigger_pattern TEXT,
                created_at TIMESTAMPTZ NOT NULL
            )
            """,
        ]
        try:
            for sql in statements:
                if not self.store.execute(sql):
                    raise RuntimeError("self-review migration statement returned false")
            self._db_ready = True
        except Exception as exc:
            self._last_error = f"self-review migration failed: {exc}"
            logger.warning(self._last_error)

    def _persist_review(self, record: Mapping[str, Any]) -> None:
        if not self._db_ready:
            return
        self._execute(
            "INSERT INTO q15_final10m_reviews (review_key,ticker,asset,market_close,predicted_side,"
            "blended_yes_probability,side_probability,agreement,decision_state,actual_result,outcome,"
            "reason_tags,primary_reason,feature_values,circuit_breaker_active,model_version,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s,%s) "
            "ON CONFLICT (review_key) DO NOTHING",
            (
                record["review_key"], record["ticker"], record["asset"], record["market_close"],
                record["predicted_side"], record["blended_yes_probability"], record["side_probability"],
                record["agreement"], record["decision_state"], record["actual_result"], record["outcome"],
                _json(record["reason_tags"]), record["primary_reason"], _json(record["feature_values"]),
                record["circuit_breaker_active"], VERSION, record["created_at"],
            ),
        )

    def _persist_pattern(self, row: Mapping[str, Any]) -> None:
        if not self._db_ready:
            return
        self._execute(
            "INSERT INTO q15_final10m_patterns (pattern_key,reason,kind,n,wins,losses,last_seen) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (pattern_key) DO UPDATE SET "
            "n=EXCLUDED.n, wins=EXCLUDED.wins, losses=EXCLUDED.losses, last_seen=EXCLUDED.last_seen",
            (row["pattern_key"], row["reason"], row["kind"], row["n"], row["wins"], row["losses"], row["last_seen"]),
        )

    def _persist_adapt_state(self, state: Mapping[str, Any]) -> None:
        if not self._db_ready:
            return
        self._execute(
            "INSERT INTO q15_threshold_adapt_state (setting,trigger_pattern,baseline,value,min_value,"
            "max_value,in_probation,last_action,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (setting) DO UPDATE SET value=EXCLUDED.value, in_probation=EXCLUDED.in_probation, "
            "last_action=EXCLUDED.last_action, updated_at=EXCLUDED.updated_at",
            (state["setting"], state["reason"], state["baseline"], state["value"], state["min_value"],
             state["max_value"], state["in_probation"], state["last_action"], state["updated_at"]),
        )

    def _persist_event(self, event: Mapping[str, Any]) -> None:
        if not self._db_ready:
            return
        self._execute(
            "INSERT INTO q15_threshold_adapt_events (setting,action,old_value,new_value,delta,reason,"
            "trigger_pattern,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (event["setting"], event["action"], event["old_value"], event["new_value"], event["delta"],
             event["reason"], event["trigger_pattern"], event["created_at"]),
        )

    def _hydrate(self) -> None:
        if not self._db_ready:
            return
        try:
            for row in self._query("SELECT pattern_key,reason,kind,n,wins,losses,last_seen FROM q15_final10m_patterns"):
                self._patterns[row["pattern_key"]] = {
                    "pattern_key": row["pattern_key"], "reason": row["reason"], "kind": row["kind"],
                    "n": int(row.get("n") or 0), "wins": int(row.get("wins") or 0),
                    "losses": int(row.get("losses") or 0), "last_seen": str(row.get("last_seen")) if row.get("last_seen") else None,
                }
            for row in self._query("SELECT setting,value,in_probation,last_action,updated_at FROM q15_threshold_adapt_state"):
                state = self._adapt.get(row["setting"])
                if state is None:
                    continue
                value = _num(row.get("value"))
                if value is None:
                    continue
                # Re-apply the persisted (bounded) adaptation so nudges survive restart.
                self._apply_value(state, value)
                state["in_probation"] = bool(row.get("in_probation"))
                state["last_action"] = row.get("last_action") or state["last_action"]
                state["updated_at"] = str(row.get("updated_at")) if row.get("updated_at") else state["updated_at"]
            for row in self._query(
                "SELECT review_key FROM q15_final10m_reviews ORDER BY created_at DESC LIMIT 500"
            ):
                self._seen_keys.add(row["review_key"])
            # Most-recent first into a bounded deque so the API reflects history
            # after a restart. appendleft on rows in DESC order keeps newest at front.
            review_rows = self._query(
                "SELECT review_key,ticker,asset,market_close,predicted_side,blended_yes_probability,"
                "side_probability,agreement,decision_state,actual_result,outcome,reason_tags,"
                "primary_reason,feature_values,circuit_breaker_active,created_at "
                "FROM q15_final10m_reviews ORDER BY created_at ASC LIMIT %s",
                (self._reviews.maxlen,),
            )
            for row in review_rows:
                self._reviews.appendleft({
                    "review_key": row.get("review_key"), "ticker": row.get("ticker"),
                    "asset": row.get("asset"), "market_close": str(row.get("market_close")) if row.get("market_close") else None,
                    "predicted_side": row.get("predicted_side"),
                    "blended_yes_probability": _num(row.get("blended_yes_probability")),
                    "side_probability": _num(row.get("side_probability")),
                    "agreement": row.get("agreement"), "decision_state": row.get("decision_state"),
                    "actual_result": row.get("actual_result"), "outcome": row.get("outcome"),
                    "reason_tags": row.get("reason_tags") or [], "primary_reason": row.get("primary_reason"),
                    "feature_values": row.get("feature_values") or {},
                    "circuit_breaker_active": bool(row.get("circuit_breaker_active")),
                    "created_at": str(row.get("created_at")) if row.get("created_at") else None,
                })
            event_rows = self._query(
                "SELECT setting,action,old_value,new_value,delta,reason,trigger_pattern,created_at "
                "FROM q15_threshold_adapt_events ORDER BY created_at ASC LIMIT %s",
                (self._events.maxlen,),
            )
            for row in event_rows:
                self._events.appendleft({
                    "setting": row.get("setting"), "action": row.get("action"),
                    "old_value": _num(row.get("old_value")), "new_value": _num(row.get("new_value")),
                    "delta": _num(row.get("delta")), "reason": row.get("reason"),
                    "trigger_pattern": row.get("trigger_pattern"),
                    "created_at": str(row.get("created_at")) if row.get("created_at") else None,
                })
        except Exception as exc:
            self._last_error = f"self-review hydration failed: {exc}"
            logger.warning(self._last_error)
