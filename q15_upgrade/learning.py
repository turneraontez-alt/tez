from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, Iterable, List

from .end_predictor import EndResultPredictor
from .learning_config import LearningV2Config
from .learning_math import (
    bootstrap_mean_difference,
    clamp,
    effective_sample_size,
    fit_regularized_logistic,
    fit_temperature_bias,
    logistic_predict,
    log_loss,
    parse_timestamp,
    probability_clip,
    time_decay_weights,
    weighted_mean,
    weighted_standard_error,
)
from .learning_store import LearningStore
from .v5_hardening import collapse_learning_rows, current_feature_drift_breaker

logger = logging.getLogger(__name__)

SETTLED_STATUSES = {"settled", "finalized", "determined", "closed"}


def _f(value, default=None):
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _json(value, default):
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return default
    try:
        parsed = json.loads(value)
        return parsed
    except Exception:
        return default


def _time_bucket(seconds):
    seconds = _f(seconds)
    if seconds is None:
        return "unknown"
    if seconds >= 720:
        return "12-15m"
    if seconds >= 360:
        return "6-12m"
    if seconds >= 120:
        return "2-6m"
    if seconds >= 60:
        return "60-120s"
    return "<60s"


def _price_bucket(price):
    price = _f(price)
    if price is None:
        return "unknown"
    if price < 25:
        return "0-25c"
    if price < 50:
        return "25-50c"
    if price < 75:
        return "50-75c"
    return "75-100c"


def _volatility_regime(snapshot: dict) -> str:
    sigma = _f(snapshot.get("model_sigma_per_sqrt_second"), 0.0)
    if sigma <= 0:
        return "unknown"
    if sigma < 0.00004:
        return "low"
    if sigma < 0.00012:
        return "normal"
    return "high"


def _decision_hash(*parts) -> str:
    payload = "|".join(str(part or "") for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:40]


def _market_close_key(row: dict) -> str:
    return str(row.get("market_close") or row.get("decision_timestamp") or "")


class LearningEngine:
    """Q15 V4 learning, calibration, shadow evaluation, and chart forecasting.

    The V4 learner uses immutable canonical decision events. It is shadow-only
    by default. Live calibration/threshold changes require explicit feature
    flags and out-of-sample promotion criteria.
    """

    VERSION = "q15-learning-v4"

    def __init__(self, store, cfg, notifier=None):
        self.store = store
        self.cfg = cfg
        self.notifier = notifier
        self.v4 = LearningV2Config()
        self.db = LearningStore(store)
        self.end_predictor = EndResultPredictor(self.v4)
        self.params: Dict[str, dict] = {}
        self.active_calibration: dict | None = None
        self.active_calibration_version: str | None = None
        self.challenger: dict | None = None
        self.active_scalp_model: dict | None = None
        self.active_scalp_model_version: str | None = None
        self.loss_risk_model: dict | None = None
        self.last_recompute_at = None
        self.last_reconcile_at = None
        self._last_recompute = 0.0
        self._last_reconcile = 0.0
        self._last_prediction_bucket: dict[str, int] = {}
        self._latest_predictions: dict[str, dict] = {}
        self._circuit_breakers: list[str] = []
        self._last_breaker_notice: tuple[str, ...] = ()
        self._db_ready = False
        self._migration_attempted = False
        self._promotion_history: list[dict] = []
        self._revert_history: list[dict] = []
        self._last_fit_metrics: dict = {}
        self._drift_metrics: dict = {}
        self._load()

    # ------------------------------------------------------------------
    # Compatibility with the existing Q15Runtime
    # ------------------------------------------------------------------
    @property
    def live_enabled(self) -> bool:
        return bool(self.v4.enabled and not self.v4.shadow)

    def adjustment_for(self, asset, bucket, signal_type, side):
        segments = self._segment_keys(asset, bucket, signal_type, side, None, None)
        matches = [self.params[key] for key in segments if key in self.params]
        recommendations = [
            {
                "segment": row.get("segment_key"),
                "adjustment": _f(row.get("active_edge_adjustment"), 0.0),
                "suppressed": bool(row.get("suppressed")),
            }
            for row in matches
            if _f(row.get("active_edge_adjustment"), 0.0) != 0 or row.get("suppressed")
        ]
        if not self.live_enabled:
            return {
                "edge_adjustment": 0.0,
                "suppressed": False,
                "reasons": ["learning_v4_shadow_only"] if recommendations else [],
                "segments": [item["segment"] for item in recommendations],
                "shadow_recommendations": recommendations,
                "learning_version": self.VERSION,
            }
        penalties = [
            _f(row.get("active_edge_adjustment"), 0.0)
            for row in matches
            if _f(row.get("active_edge_adjustment"), 0.0) > 0
        ]
        boosts = [
            _f(row.get("active_edge_adjustment"), 0.0)
            for row in matches
            if _f(row.get("active_edge_adjustment"), 0.0) < 0
        ]
        adjustment = max(penalties) if penalties else (min(boosts) if boosts else 0.0)
        suppressed = any(bool(row.get("suppressed")) for row in matches)
        reasons = []
        for row in matches:
            if row.get("suppressed"):
                reasons.append(f"{row.get('segment_key')}:suppressed")
        return {
            "edge_adjustment": round(adjustment, 2),
            "suppressed": suppressed,
            "reasons": reasons,
            "segments": [row.get("segment_key") for row in matches],
            "learning_version": self.VERSION,
        }

    def calibrate_probability(self, asset, probability, side=None) -> dict:
        raw = probability_clip(probability)
        if raw is None:
            return {
                "raw_probability": None,
                "calibrated_probability": None,
                "version": self.active_calibration_version,
                "apply_live": False,
            }
        calibrated = raw
        params = self.active_calibration or {}
        if params:
            try:
                from .learning_math import logit, sigmoid

                temperature = max(0.25, float(params.get("global_temperature", 1.0)))
                bias = float(params.get("global_bias", 0.0))
                asset_bias = float((params.get("asset_biases") or {}).get(str(asset), 0.0))
                calibrated = clamp(sigmoid(bias + asset_bias + logit(raw) / temperature), 0.01, 0.99)
            except Exception:
                calibrated = raw
        apply_live = bool(
            self.live_enabled
            and self.v4.apply_calibration_live
            and self.active_calibration_version
            and not self._circuit_breakers
        )
        return {
            "raw_probability": raw,
            "calibrated_probability": calibrated,
            "version": self.active_calibration_version or "identity",
            "apply_live": apply_live,
            "side": side,
        }

    # ------------------------------------------------------------------
    # Snapshot enrichment + canonical observations
    # ------------------------------------------------------------------
    def enrich_and_observe(self, snapshots: dict, now: float, ws_health=None) -> dict:
        ws_health = ws_health or {}
        self._ensure_migration()
        self._update_circuit_breakers(snapshots, now, ws_health)
        for asset, snapshot in snapshots.items():
            if not isinstance(snapshot, dict) or snapshot.get("market_state") != "live":
                continue
            if self.v4.end_prediction_enabled:
                prediction = self.end_predictor.predict(snapshot).as_dict()
                snapshot["end_prediction"] = prediction
                snapshot["end_prediction_result"] = prediction.get("predicted_result")
                snapshot["end_prediction_yes_probability"] = prediction.get("blended_yes_probability")
                snapshot["end_prediction_chart_probability"] = prediction.get("chart_yes_probability")
                snapshot["end_prediction_confidence"] = prediction.get("confidence")
                snapshot["predicted_final_underlying"] = prediction.get("predicted_final_underlying")
                snapshot["predicted_final_range_low"] = prediction.get("predicted_range_low")
                snapshot["predicted_final_range_high"] = prediction.get("predicted_range_high")
                self._latest_predictions[asset] = prediction
                self._record_end_prediction(asset, snapshot, prediction, now)
            snapshot["learning_version"] = self.VERSION
            snapshot["learning_shadow_only"] = not self.live_enabled
            snapshot["learning_circuit_breakers"] = list(self._circuit_breakers)
            self._observe_decision(asset, snapshot, now)
        return snapshots

    def _observe_decision(self, asset: str, snap: dict, now: float) -> None:
        if not self.db.enabled:
            return
        ticker = snap.get("ticker")
        side = snap.get("entry_side")
        if not ticker or side not in {"YES", "NO"}:
            return
        state = snap.get("decision_state") or snap.get("entry_status")
        blockers = set(snap.get("entry_blockers") or [])
        strategies: list[tuple[str, bool, bool, str | None]] = []
        if state == "ENTRY CONFIRMED":
            strategies.append(("settlement", False, False, None))

        suppression_blockers = {item for item in blockers if "suppression" in str(item)}
        if snap.get("learned_suppressed") and blockers and blockers == suppression_blockers:
            strategies.append(("shadow:suppressed_recovery", True, True, "learned_segment_suppression"))

        common_hard = {
            item for item in blockers
            if not str(item).startswith("edge_below_required")
            and not str(item).startswith("confirmation_score_below")
            and "suppression" not in str(item)
        }
        edge = _f(snap.get("estimated_edge"))
        required = _f(snap.get("required_edge"))
        score = _f(snap.get("confirmation_score"))
        if not common_hard and edge is not None and required is not None:
            for delta in (-1.0, 0.0, 1.0):
                if edge >= required + delta:
                    strategies.append((f"shadow:edge_{delta:+.0f}c", True, False, None))
        if not common_hard and score is not None:
            for threshold in (50.0, 55.0, 60.0):
                if score >= threshold:
                    strategies.append((f"shadow:score_{int(threshold)}", True, False, None))

        deduped = []
        seen = set()
        for strategy in strategies:
            if strategy[0] not in seen:
                deduped.append(strategy)
                seen.add(strategy[0])
        for strategy, shadow, suppressed, reason in deduped:
            row = self._decision_row(
                asset=asset,
                snap=snap,
                now=now,
                strategy=strategy,
                shadow=shadow,
                suppressed=suppressed,
                suppression_reason=reason,
            )
            self.db.insert_decision(row)

    def _decision_row(self, *, asset, snap, now, strategy, shadow, suppressed, suppression_reason):
        side = snap.get("entry_side")
        raw_side_probability = _f(snap.get("raw_side_win_probability"))
        if raw_side_probability is None:
            raw_side_probability = _f(snap.get("raw_win_prob"))
        if raw_side_probability is None:
            raw_side_probability = _f(snap.get("win_prob"))
        calibration = self.calibrate_probability(asset, raw_side_probability, side)
        calibrated = calibration.get("calibrated_probability")
        feature_snapshot = self._feature_snapshot(snap)
        market_close = snap.get("close_time")
        decision_key = _decision_hash(
            snap.get("ticker"), side, strategy, self.v4.policy_version, self.v4.feature_schema_version
        )
        depth = snap.get("yes_ask_qty") if side == "YES" else snap.get("no_ask_qty")
        return {
            "decision_key": decision_key,
            "ticker": snap.get("ticker"),
            "asset": asset,
            "side": side,
            "direction": snap.get("pattern_direction"),
            "strategy": strategy,
            "decision_timestamp": now,
            "market_close": market_close,
            "policy_version": self.v4.policy_version,
            "model_version": self.v4.model_version,
            "calibration_version": calibration.get("version"),
            "feature_schema_version": self.v4.feature_schema_version,
            "code_commit": self.v4.code_commit,
            "learning_corpus_cutoff": datetime.now(timezone.utc).isoformat(),
            "raw_win_probability": raw_side_probability,
            "calibrated_win_probability": calibrated,
            "executable_ask": _f(snap.get("entry_ask")),
            "executable_vwap": _f(snap.get("entry_ask")),
            "quantity": _f(snap.get("entry_vwap_size"), 1.0),
            "estimated_fee": _f(snap.get("entry_fee"), 0.0),
            "estimated_slippage": self._estimated_slippage(snap),
            "maximum_entry_price": _f(snap.get("max_entry_price")),
            "required_edge": _f(snap.get("required_edge")),
            "predicted_edge": _f(snap.get("estimated_edge")),
            "confirmation_groups": snap.get("confirmation_groups") or {},
            "confirmation_group_count": int(snap.get("confirmation_group_count") or 0),
            "confirmation_score": _f(snap.get("confirmation_score")),
            "model_data_quality_score": _f(snap.get("model_confidence")),
            "seconds_remaining": _f(snap.get("seconds_remaining")),
            "spread": _f(snap.get("spread")),
            "depth": _f(depth),
            "underlying_price": _f(snap.get("underlying_current")),
            "target": _f(snap.get("target_value")),
            "invalidation_level": _f(snap.get("invalidation_level")),
            "volatility_regime": _volatility_regime(snap),
            "feature_snapshot_json": feature_snapshot,
            "shadow": shadow,
            "suppressed": suppressed,
            "suppression_reason": suppression_reason,
            "alert_sent": False,
            "valid_for_learning": not bool(snap.get("data_warning")),
            "invalid_learning_reason": "data_warning_at_decision" if snap.get("data_warning") else None,
        }

    def record_alert_sent(self, evaluation: dict) -> None:
        if not self.db.enabled:
            return
        decision_key = _decision_hash(
            evaluation.get("ticker"), evaluation.get("side"), "settlement",
            self.v4.policy_version, self.v4.feature_schema_version,
        )
        self.db._execute(
            "UPDATE q15_decision_events SET alert_sent=TRUE WHERE decision_key=%s",
            (decision_key,),
        )

    # ------------------------------------------------------------------
    # End-result predictions
    # ------------------------------------------------------------------
    def _record_end_prediction(self, asset, snap, prediction, now):
        if not self.db.enabled or prediction.get("predicted_result") == "UNAVAILABLE":
            return
        interval = max(5, self.v4.decision_observation_interval_seconds)
        bucket = int(now // interval)
        ticker = snap.get("ticker")
        if not ticker:
            return
        memory_key = f"{ticker}:{bucket}"
        if self._last_prediction_bucket.get(ticker) == bucket:
            return
        self._last_prediction_bucket[ticker] = bucket
        self.db.insert_end_prediction({
            "prediction_key": _decision_hash(memory_key, prediction.get("version")),
            "ticker": ticker,
            "asset": asset,
            "prediction_timestamp": now,
            "market_close": snap.get("close_time"),
            "version": prediction.get("version"),
            "predicted_result": prediction.get("predicted_result"),
            "chart_yes_probability": prediction.get("chart_yes_probability"),
            "blended_yes_probability": prediction.get("blended_yes_probability"),
            "confidence": prediction.get("confidence"),
            "predicted_final_underlying": prediction.get("predicted_final_underlying"),
            "predicted_range_low": prediction.get("predicted_range_low"),
            "predicted_range_high": prediction.get("predicted_range_high"),
            "current_underlying": prediction.get("current_underlying"),
            "target_value": prediction.get("target_value"),
            "seconds_remaining": prediction.get("seconds_remaining"),
            "feature_json": prediction,
            "valid_for_learning": not bool(snap.get("data_warning")),
        })

    def end_predictions(self) -> dict:
        return {
            "version": self.end_predictor.VERSION,
            "warning": "Probabilistic chart estimates only; no result is guaranteed.",
            "predictions": dict(self._latest_predictions),
        }

    # ------------------------------------------------------------------
    # Settlement reconciliation
    # ------------------------------------------------------------------
    def reconcile(self, now: float, client) -> None:
        if not self.db.enabled or now - self._last_reconcile < self.v4.reconcile_interval_seconds:
            return
        self._last_reconcile = now
        decisions = self.db.pending_decisions()
        end_predictions = self.db.pending_end_predictions()
        by_ticker: dict[str, dict] = {}
        for row in decisions:
            by_ticker.setdefault(row.get("ticker"), {"decisions": [], "predictions": []})["decisions"].append(row)
        for row in end_predictions:
            by_ticker.setdefault(row.get("ticker"), {"decisions": [], "predictions": []})["predictions"].append(row)
        calls = 0
        for ticker, grouped in by_ticker.items():
            if not ticker or calls >= self.v4.max_reconcile_calls:
                break
            try:
                market = client.get_market(ticker)
            except Exception as exc:
                logger.warning("V4 reconcile get_market %s failed: %s", ticker, exc)
                continue
            calls += 1
            status = str((market or {}).get("status") or "").lower()
            result = str((market or {}).get("result") or "").lower()
            if status not in SETTLED_STATUSES and result not in {"yes", "no"}:
                continue
            for row in grouped["decisions"]:
                side = row.get("side")
                ask = _f(row.get("executable_ask"), 0.0)
                fee = _f(row.get("estimated_fee"), 0.0)
                if result in {"yes", "no"}:
                    won = (side == "YES" and result == "yes") or (side == "NO" and result == "no")
                    outcome = "win" if won else "loss"
                    pnl = (100.0 - ask - fee) if won else -(ask + fee)
                else:
                    outcome = "void"
                    pnl = 0.0
                self.db.settle_decision(row["id"], outcome, round(pnl, 4), "kalshi_market_result")
            for row in grouped["predictions"]:
                self.db.settle_end_prediction(row["id"], result if result in {"yes", "no"} else "void")
        self.last_reconcile_at = time.time()

    # ------------------------------------------------------------------
    # Calibration / threshold / model fitting
    # ------------------------------------------------------------------
    def recompute(self, now, force=False):
        interval = self.v4.fit_interval_seconds
        if not force and now - self._last_recompute < interval:
            return
        self._last_recompute = now
        self._ensure_migration()
        if not self.db.enabled:
            return
        rows = collapse_learning_rows(self.db.settled_decisions())
        if not rows:
            self.last_recompute_at = time.time()
            return
        self._drift_metrics = self._feature_drift(rows)
        if self.v4.circuit_breakers_enabled and self._drift_metrics.get("max_psi", 0.0) > self.v4.max_feature_drift_psi:
            if "feature_distribution_drift" not in self._circuit_breakers:
                self._circuit_breakers.append("feature_distribution_drift")
        self._fit_calibration(rows)
        self._fit_segments(rows)
        self._fit_loss_risk(rows)
        if self.v4.scalp_learning_enabled:
            self._fit_scalp_model(self.db.settled_scalps())
        self._maybe_revert(rows)
        self.last_recompute_at = time.time()

    def _fit_calibration(self, rows: list[dict]):
        if not self.v4.calibration_enabled:
            return
        candidates = self.v4.decay_candidates_days if self.v4.decay_half_life_autoselect else (
            self.v4.decay_half_life_days,
        )
        fits = []
        for half_life in candidates:
            fit = fit_temperature_bias(
                rows,
                half_life_days=half_life,
                l2=self.v4.calibration_l2,
                learning_rate=self.v4.calibration_learning_rate,
                steps=self.v4.calibration_steps,
                asset_min_effective_n=self.v4.asset_calibration_min_effective_n,
            )
            if fit and fit.effective_n >= self.v4.calibration_min_effective_n:
                validation = fit.validation_metrics.get("calibrated") or {}
                score = validation.get("log_loss")
                if score is not None:
                    fits.append((score, fit))
        if not fits:
            return
        _, best = min(fits, key=lambda item: item[0])
        metrics = {
            "train": best.train_metrics,
            "validation": best.validation_metrics,
            "effective_n": best.effective_n,
            "half_life_days": best.half_life_days,
        }
        version = f"cal-{int(time.time())}-{_decision_hash(best.as_params())[:8]}"
        active = self.db.load_active_model("settlement_calibration")
        parent = active.get("version") if active else None
        self.db.insert_model_version({
            "version": version,
            "model_type": "settlement_calibration",
            "status": "challenger",
            "champion_or_challenger": "challenger",
            "params_json": best.as_params(),
            "metrics_json": metrics,
            "feature_schema_version": self.v4.feature_schema_version,
            "policy_version": self.v4.policy_version,
            "learning_corpus_cutoff": datetime.now(timezone.utc).isoformat(),
            "rollback_parent_version": parent,
            "notes": "Chronological holdout; regularized global temperature+bias with asset bias shrinkage.",
        })
        self.challenger = {"version": version, "params": best.as_params(), "metrics": metrics}
        self._last_fit_metrics["calibration"] = self.challenger

        calibrated_metrics = (best.validation_metrics.get("calibrated") or {})
        raw_metrics = (best.validation_metrics.get("raw") or {})
        improvement = None
        if calibrated_metrics.get("log_loss") is not None and raw_metrics.get("log_loss") is not None:
            improvement = raw_metrics["log_loss"] - calibrated_metrics["log_loss"]
        promote = bool(
            self.live_enabled
            and self.v4.apply_calibration_live
            and not self._circuit_breakers
            and best.effective_n >= self.v4.challenger_min_effective_n
            and improvement is not None
            and improvement > 0.001
            and self._promotion_allowed()
        )
        if promote:
            self.db.activate_model("settlement_calibration", version)
            self.active_calibration = best.as_params()
            self.active_calibration_version = version
            event = {"version": version, "improvement_log_loss": improvement, "at": time.time()}
            self._promotion_history.insert(0, event)
            self._promotion_history = self._promotion_history[:20]
            self.db.record_event("model_promoted", version, event)

    def _fit_segments(self, rows: list[dict]):
        half_life = self._selected_half_life()
        timestamps = [row.get("decision_timestamp") for row in rows]
        weights = time_decay_weights(timestamps, half_life)
        accum: dict[str, list[tuple[dict, float]]] = defaultdict(list)
        for row, weight in zip(rows, weights):
            for key, dimension in self._segments_for_row(row):
                accum[key].append((row, weight))
        previous = {row.get("segment_key"): row for row in self.db.load_segment_adjustments()}
        updated = {}
        for key, pairs in accum.items():
            segment_rows = [pair[0] for pair in pairs]
            segment_weights = [pair[1] for pair in pairs]
            eff_n = effective_sample_size(segment_weights)
            outcomes = [1.0 if row.get("outcome") == "win" else 0.0 for row in segment_rows]
            predicted = [
                _f(row.get("calibrated_win_probability"), _f(row.get("raw_win_probability"), 0.5))
                for row in segment_rows
            ]
            pnls = [_f(row.get("realized_pnl"), 0.0) for row in segment_rows]
            win_rate = weighted_mean(outcomes, segment_weights)
            predicted_mean = weighted_mean(predicted, segment_weights)
            pnl_mean = weighted_mean(pnls, segment_weights)
            pnl_se = weighted_standard_error(pnls, segment_weights)
            lcb = pnl_mean - 1.645 * pnl_se if pnl_mean is not None and pnl_se is not None else pnl_mean
            gap = (predicted_mean - win_rate) if predicted_mean is not None and win_rate is not None else 0.0
            confidence = eff_n / (eff_n + self.v4.threshold_confidence_k)
            target = confidence * (10.0 * gap + max(0.0, -(pnl_mean or 0.0)) * 0.35)
            if pnl_mean is not None and pnl_mean > 1.5 and abs(gap) < 0.04:
                target -= confidence * min(1.0, pnl_mean / 4.0)
            target = clamp(
                target,
                -self.v4.max_threshold_deviation_cents,
                self.v4.max_threshold_deviation_cents,
            )
            old = previous.get(key) or {}
            old_active = _f(old.get("active_edge_adjustment"), 0.0)
            delta = clamp(
                target - old_active,
                -self.v4.max_threshold_change_per_update_cents,
                self.v4.max_threshold_change_per_update_cents,
            )
            active = clamp(
                old_active + delta,
                -self.v4.max_threshold_deviation_cents,
                self.v4.max_threshold_deviation_cents,
            )
            if eff_n < self.v4.min_effective_n_for_adjustment:
                active = old_active

            old_suppressed = bool(old.get("suppressed"))
            suppressed = old_suppressed
            recovery_streak = int(old.get("recovery_streak") or 0)
            reasons = []
            if eff_n >= self.v4.recovery_min_effective_n and lcb is not None and lcb < self.v4.suppress_ev_cents:
                suppressed = True
                recovery_streak = 0
                reasons.append("negative_ev_lower_bound")
            if old_suppressed:
                shadow_pairs = [(row, weight) for row, weight in pairs if bool(row.get("shadow"))]
                shadow_pnls = [_f(row.get("realized_pnl"), 0.0) for row, _ in shadow_pairs]
                shadow_weights = [weight for _, weight in shadow_pairs]
                shadow_eff = effective_sample_size(shadow_weights)
                shadow_mean = weighted_mean(shadow_pnls, shadow_weights)
                shadow_se = weighted_standard_error(shadow_pnls, shadow_weights)
                shadow_lcb = (
                    shadow_mean - 1.645 * shadow_se
                    if shadow_mean is not None and shadow_se is not None
                    else shadow_mean
                )
                if (
                    shadow_eff >= self.v4.recovery_min_effective_n
                    and shadow_lcb is not None
                    and shadow_lcb > self.v4.recover_ev_cents
                ):
                    recovery_streak += 1
                else:
                    recovery_streak = 0
                if recovery_streak >= self.v4.recovery_consecutive_periods:
                    suppressed = False
                    reasons.append("shadow_recovery_confirmed")
                shadow_metrics = {
                    "effective_n": round(shadow_eff, 3),
                    "mean_pnl": round(shadow_mean, 4) if shadow_mean is not None else None,
                    "ev_lower_bound": round(shadow_lcb, 4) if shadow_lcb is not None else None,
                }
            else:
                shadow_metrics = {}

            row = {
                "segment_key": key,
                "dimension": self._dimension_for_key(key),
                "n": len(segment_rows),
                "effective_n": round(eff_n, 3),
                "weighted_win_rate": round(win_rate, 5) if win_rate is not None else None,
                "weighted_predicted_probability": round(predicted_mean, 5) if predicted_mean is not None else None,
                "weighted_realized_pnl": round(pnl_mean, 5) if pnl_mean is not None else None,
                "ev_lower_bound": round(lcb, 5) if lcb is not None else None,
                "target_edge_adjustment": round(target, 3),
                "active_edge_adjustment": round(active, 3),
                "suppressed": suppressed,
                "recovery_streak": recovery_streak,
                "shadow_recovery_metrics": shadow_metrics,
                "reasons": reasons,
            }
            self.db.upsert_segment(row)
            updated[key] = row
        self.params = updated
        self._last_fit_metrics["segments"] = {
            "tracked": len(updated),
            "half_life_days": half_life,
            "live_application_enabled": self.live_enabled,
        }

    def _fit_loss_risk(self, rows: list[dict]):
        feature_names = [
            "predicted_edge", "spread", "confirmation_score", "seconds_remaining",
            "model_data_quality_score", "raw_win_probability", "executable_ask",
        ]
        transformed = []
        for row in rows:
            item = {name: _f(row.get(name), 0.0) for name in feature_names}
            item["binary_outcome"] = 1 if row.get("outcome") == "loss" else 0
            item["sample_weight"] = 1.0
            transformed.append(item)
        model = fit_regularized_logistic(transformed, feature_names, l2=1.0)
        if model:
            self.loss_risk_model = model
            self._last_fit_metrics["loss_risk"] = {
                "title": "Factors associated with higher loss risk",
                "not_causal": True,
                **model,
            }

    def _fit_scalp_model(self, rows: list[dict]):
        feature_names = [
            "reversal_score", "spread", "observed_extension", "time_remaining",
            "entry_price", "flow_skew", "target_distance",
        ]
        transformed = []
        for row in rows:
            features = _json(row.get("feature_snapshot_json"), {})
            if row.get("target_hit") is None or row.get("expired_without_target_or_stop"):
                continue
            item = {name: _f(features.get(name), 0.0) for name in feature_names}
            item["binary_outcome"] = 1 if row.get("target_hit") else 0
            item["sample_weight"] = 1.0
            transformed.append(item)
        model = fit_regularized_logistic(transformed, feature_names, l2=1.5)
        if not model:
            return
        version = f"scalp-{int(time.time())}-{_decision_hash(model.get('coefficients'))[:8]}"
        metrics = model.get("metrics") or {}
        self.db.insert_model_version({
            "version": version,
            "model_type": "scalp_target_first",
            "status": "challenger",
            "champion_or_challenger": "challenger",
            "params_json": model,
            "metrics_json": metrics,
            "feature_schema_version": self.v4.feature_schema_version,
            "policy_version": self.v4.policy_version,
            "learning_corpus_cutoff": datetime.now(timezone.utc).isoformat(),
            "rollback_parent_version": self.active_scalp_model_version,
            "notes": "Separate target-before-stop scalp model; timeout rows kept as censored.",
        })
        self._last_fit_metrics["scalp"] = {"version": version, "metrics": metrics}
        if (
            self.live_enabled
            and metrics.get("effective_n", 0) >= self.v4.scalp_model_min_effective_n
            and self._promotion_allowed()
            and not self._circuit_breakers
        ):
            self.db.activate_model("scalp_target_first", version)
            self.active_scalp_model = model
            self.active_scalp_model_version = version

    # ------------------------------------------------------------------
    # Scalp assessment and event capture
    # ------------------------------------------------------------------
    def assess_scalp(self, features: dict) -> dict:
        target = max(10.0, _f(features.get("target_net_cents"), self.v4.scalp_target_cents))
        spread = _f(features.get("spread"), 0.0)
        reversal = _f(features.get("reversal_score"), 0.0)
        extension = _f(features.get("observed_extension"), 0.0)
        seconds = _f(features.get("time_remaining"), 0.0)
        entry = _f(features.get("entry_price"), 50.0)
        stop = _f(features.get("stop_price"), max(1.0, entry - 6.0))
        fee = _f(features.get("estimated_fee"), 0.0)
        slippage = max(0.1, spread * 0.25)
        model_probability = logistic_predict(self.active_scalp_model, features)
        if model_probability is None:
            linear = (
                -0.55
                + 0.035 * (reversal - 70.0)
                + 0.055 * (extension - target)
                + 0.002 * max(0.0, seconds - 60.0)
                - 0.28 * spread
            )
            from .learning_math import sigmoid

            model_probability = clamp(sigmoid(linear), 0.05, 0.95)
        timeout_probability = clamp(0.18 - 0.001 * max(0.0, seconds - 60.0), 0.04, 0.25)
        stop_probability = clamp(1.0 - model_probability - timeout_probability, 0.02, 0.93)
        target_profit = target
        stop_loss = max(1.0, entry - stop) + fee
        expected_ev = model_probability * target_profit - stop_probability * stop_loss - slippage
        qualifies = bool(
            model_probability >= self.v4.scalp_min_target_probability
            and expected_ev >= self.v4.scalp_min_expected_ev_cents
            and target >= 10.0
        )
        return {
            "qualifies": qualifies,
            "target_probability": round(model_probability, 4),
            "stop_probability": round(stop_probability, 4),
            "timeout_probability": round(timeout_probability, 4),
            "expected_net_ev_cents": round(expected_ev, 3),
            "predicted_seconds_to_target": round(max(5.0, target / max(0.1, extension) * 30.0), 1),
            "predicted_max_favorable_excursion": round(max(target, extension * 0.65), 2),
            "predicted_max_adverse_excursion": round(stop_loss * stop_probability, 2),
            "model_version": self.active_scalp_model_version or "heuristic-scalp-v1",
            "warning": "A 10-cent move is a probabilistic target, not a guarantee.",
        }

    def record_scalp_open(self, sig: dict, now: float) -> None:
        if not self.db.enabled:
            return
        assessment = sig.get("learning_assessment") or {}
        features = sig.get("learning_features") or {}
        scalp_key = sig.get("learning_scalp_key") or _decision_hash(
            sig.get("ticker"), sig.get("side"), int(now), self.v4.policy_version
        )
        sig["learning_scalp_key"] = scalp_key
        self.db.insert_scalp({
            "scalp_key": scalp_key,
            "ticker": sig.get("ticker"),
            "asset": sig.get("asset"),
            "side": sig.get("side"),
            "entry_timestamp": now,
            "entry_price": sig.get("entry_exec") or sig.get("entry"),
            "executable_vwap": sig.get("entry_exec") or sig.get("entry"),
            "quantity": 1.0,
            "estimated_fee": sig.get("fee"),
            "estimated_slippage": max(0.1, _f(sig.get("spread"), 0.0) * 0.25),
            "target_price": sig.get("target"),
            "stop_price": sig.get("stop"),
            "time_remaining": sig.get("secs"),
            "predicted_target_probability": assessment.get("target_probability"),
            "predicted_stop_probability": assessment.get("stop_probability"),
            "predicted_seconds_to_target": assessment.get("predicted_seconds_to_target"),
            "predicted_max_favorable_excursion": assessment.get("predicted_max_favorable_excursion"),
            "predicted_max_adverse_excursion": assessment.get("predicted_max_adverse_excursion"),
            "model_version": assessment.get("model_version") or "heuristic-scalp-v1",
            "policy_version": self.v4.policy_version,
            "feature_schema_version": self.v4.feature_schema_version,
            "feature_snapshot_json": features,
            "shadow": not bool(sig.get("actionable")),
            "valid_for_learning": True,
        })

    def record_scalp_resolution(self, sig: dict, now: float, exit_price, exit_reason, realized) -> None:
        scalp_key = sig.get("learning_scalp_key")
        if not scalp_key:
            return
        entry = _f(sig.get("entry"), 0.0)
        peak = _f(sig.get("peak"), entry)
        adverse = max(0.0, entry - _f(sig.get("trough"), entry))
        self.db.settle_scalp(scalp_key, {
            "maximum_favorable_excursion": max(0.0, peak - entry),
            "maximum_adverse_excursion": adverse,
            "seconds_to_target": max(0.0, now - _f(sig.get("opened_at"), now)) if exit_reason == "take_profit" else None,
            "seconds_to_stop": max(0.0, now - _f(sig.get("opened_at"), now)) if exit_reason == "stop" else None,
            "exit_timestamp": now,
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "target_hit": exit_reason == "take_profit",
            "stop_hit": exit_reason == "stop",
            "expired_without_target_or_stop": exit_reason in {"rollover", "exit_before_close"},
            "realized_net_pnl": realized,
        })

    # ------------------------------------------------------------------
    # Circuit breakers / drift / rollback
    # ------------------------------------------------------------------
    def _update_circuit_breakers(self, snapshots: dict, now: float, ws_health: dict):
        reasons = []
        if self.v4.circuit_breakers_enabled:
            ages = [
                _f(snap.get("data_age_seconds"))
                for snap in snapshots.values()
                if isinstance(snap, dict) and snap.get("market_state") == "live"
            ]
            ages = [age for age in ages if age is not None]
            if ages and max(ages) > self.v4.max_learning_data_age_seconds:
                reasons.append("snapshot_data_stale")
            if ws_health.get("connected"):
                last_message = _f(ws_health.get("last_message_at"))
                if last_message is not None and now - last_message > self.v4.max_ws_silence_seconds:
                    reasons.append("websocket_message_gap")
            if self.db.last_error:
                reasons.append("learning_database_error")
        if current_feature_drift_breaker(self):
            reasons.append("feature_distribution_drift")
        self._circuit_breakers = sorted(set(reasons))
        current = tuple(self._circuit_breakers)
        if current and current != self._last_breaker_notice:
            self._last_breaker_notice = current
            self.db.record_event("circuit_breaker", None, {"reasons": list(current)})
            if self.notifier and getattr(self.notifier, "enabled", False):
                try:
                    self.notifier.send(
                        "⚠ <b>Q15 learning paused</b>\n" + ", ".join(current) +
                        "\nMonitoring remains read-only; the current champion is unchanged."
                    )
                except Exception:
                    pass

    def _feature_drift(self, rows: list[dict]) -> dict:
        if len(rows) < 80:
            return {"max_psi": 0.0, "features": {}, "reason": "insufficient_sample"}
        ordered = sorted(rows, key=lambda row: parse_timestamp(row.get("decision_timestamp")) or 0.0)
        split = max(40, int(len(ordered) * 0.75))
        baseline = ordered[:split]
        recent = ordered[split:]
        features = ["executable_ask", "spread", "confirmation_score", "raw_win_probability"]
        metrics = {}
        for feature in features:
            metrics[feature] = self._psi(
                [_f(row.get(feature)) for row in baseline],
                [_f(row.get(feature)) for row in recent],
            )
        return {"max_psi": max(metrics.values() or [0.0]), "features": metrics}

    @staticmethod
    def _psi(baseline: list, recent: list, bins: int = 8) -> float:
        baseline = sorted(value for value in baseline if value is not None)
        recent = [value for value in recent if value is not None]
        if len(baseline) < 20 or len(recent) < 10:
            return 0.0
        edges = []
        for index in range(1, bins):
            pos = min(len(baseline) - 1, int(index / bins * len(baseline)))
            edges.append(baseline[pos])
        def counts(values):
            result = [0] * bins
            for value in values:
                idx = 0
                while idx < len(edges) and value > edges[idx]:
                    idx += 1
                result[idx] += 1
            total = max(1, len(values))
            return [max(1e-4, count / total) for count in result]
        expected = counts(baseline)
        actual = counts(recent)
        return round(sum((a - e) * math.log(a / e) for a, e in zip(actual, expected)), 5)

    def _maybe_revert(self, rows: list[dict]):
        if not self.active_calibration_version or len(rows) < self.v4.rollback_min_effective_n:
            return
        active_rows = [row for row in rows if row.get("calibration_version") == self.active_calibration_version]
        if len(active_rows) < self.v4.rollback_min_effective_n:
            return
        model = self.db.load_active_model("settlement_calibration")
        if not model:
            return
        parent_version = model.get("rollback_parent_version")
        if not parent_version:
            return
        parent_rows = self.db._query(
            "SELECT params_json FROM q15_model_versions WHERE version=%s LIMIT 1", (parent_version,)
        )
        if not parent_rows:
            return
        parent = _json(parent_rows[0].get("params_json"), {})
        current = _json(model.get("params_json"), {})
        current_losses = []
        parent_losses = []
        for row in active_rows:
            raw = probability_clip(row.get("raw_win_probability"))
            if raw is None or row.get("outcome") not in {"win", "loss"}:
                continue
            outcome = 1 if row.get("outcome") == "win" else 0
            current_p = self._apply_calibration_params(current, row.get("asset"), raw)
            parent_p = self._apply_calibration_params(parent, row.get("asset"), raw)
            current_losses.append(-(outcome * math.log(current_p) + (1 - outcome) * math.log(1 - current_p)))
            parent_losses.append(-(outcome * math.log(parent_p) + (1 - outcome) * math.log(1 - parent_p)))
        evidence = bootstrap_mean_difference(
            current_losses, parent_losses, self.v4.rollback_bootstrap_samples
        )
        if evidence and evidence["ci_low"] > 0.0 and self.live_enabled:
            self.db.activate_model("settlement_calibration", parent_version)
            self.active_calibration = parent
            self.active_calibration_version = parent_version
            event = {"from": model.get("version"), "to": parent_version, "evidence": evidence, "at": time.time()}
            self._revert_history.insert(0, event)
            self._revert_history = self._revert_history[:20]
            self.db.record_event("model_reverted", parent_version, event)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def active_adjustments(self):
        rows = [
            {
                "segment": key,
                "n": row.get("n"),
                "effective_n": row.get("effective_n"),
                "avg_realized_pnl": row.get("weighted_realized_pnl"),
                "ev_lower_bound": row.get("ev_lower_bound"),
                "target_edge_adjustment": row.get("target_edge_adjustment"),
                "edge_adjustment": row.get("active_edge_adjustment") if self.live_enabled else 0.0,
                "edge_penalty": row.get("active_edge_adjustment") if self.live_enabled else 0.0,
                "shadow_edge_adjustment": row.get("active_edge_adjustment"),
                "posterior_win_rate": row.get("weighted_win_rate"),
                "suppressed": bool(row.get("suppressed")) if self.live_enabled else False,
                "shadow_suppressed": bool(row.get("suppressed")),
                "recovery_streak": row.get("recovery_streak"),
                "reasons": row.get("reasons") or [],
            }
            for key, row in self.params.items()
            if _f(row.get("active_edge_adjustment"), 0.0) != 0 or row.get("suppressed")
        ]
        rows.sort(key=lambda row: (row["shadow_suppressed"], abs(row["shadow_edge_adjustment"] or 0)), reverse=True)
        return rows

    def loss_reason_summary(self, hours=24):
        # Compatibility with reporting.py; V4 reports associations, not causes.
        model = self.loss_risk_model or {}
        coefficients = model.get("coefficients") or {}
        return {
            name: round(value, 4)
            for name, value in sorted(coefficients.items(), key=lambda item: abs(item[1]), reverse=True)
        }

    def summary(self):
        active_model = {
            "version": self.active_calibration_version or "identity",
            "params": self.active_calibration or {},
        }
        return {
            "enabled": self.v4.enabled,
            "shadow": self.v4.shadow or not self.v4.enabled,
            "version": self.VERSION,
            "feature_schema_version": self.v4.feature_schema_version,
            "policy_version": self.v4.policy_version,
            "active_model_version": self.v4.model_version,
            "active_calibration": active_model,
            "challenger": self.challenger,
            "active_scalp_model_version": self.active_scalp_model_version or "heuristic-scalp-v1",
            "segments_tracked": len(self.params),
            "active_adjustments": self.active_adjustments(),
            "factors_associated_with_higher_loss_risk": self._last_fit_metrics.get("loss_risk"),
            "calibration_metrics": self._last_fit_metrics.get("calibration"),
            "settlement_learner_status": self._last_fit_metrics.get("segments", {}),
            "scalp_learner_status": self._last_fit_metrics.get("scalp", {}),
            "effective_sample_count": self._effective_sample_summary(),
            "current_decay_half_life_days": self._selected_half_life(),
            "circuit_breakers": list(self._circuit_breakers),
            "feature_drift": self._drift_metrics,
            "latest_fit_time": self.last_recompute_at,
            "next_eligible_fit_time": (
                self._last_recompute + self.v4.fit_interval_seconds if self._last_recompute else None
            ),
            "last_reconcile_at": self.last_reconcile_at,
            "recent_promotions": self._promotion_history,
            "recent_reverts": self._revert_history,
            "database": {
                "enabled": self.db.enabled,
                "ready": self._db_ready,
                "last_error": self.db.last_error,
                "counts": self.db.counts() if self.db.enabled and self._db_ready else {},
            },
            "end_prediction": self.end_predictions(),
            "config": self.v4.public_dict(),
            "warning": "Learning and chart forecasts are probabilistic. Shadow mode does not prove profitability.",
        }

    def health_summary(self) -> dict:
        return {
            "version": self.VERSION,
            "enabled": self.v4.enabled,
            "shadow": self.v4.shadow or not self.v4.enabled,
            "database_ready": self._db_ready,
            "active_calibration_version": self.active_calibration_version or "identity",
            "active_scalp_model_version": self.active_scalp_model_version or "heuristic-scalp-v1",
            "circuit_breakers": list(self._circuit_breakers),
            "end_prediction_version": self.end_predictor.VERSION,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _load(self):
        self._ensure_migration()
        if not self.db.enabled:
            return
        active = self.db.load_active_model("settlement_calibration")
        if active:
            self.active_calibration = _json(active.get("params_json"), {})
            self.active_calibration_version = active.get("version")
        scalp = self.db.load_active_model("scalp_target_first")
        if scalp:
            self.active_scalp_model = _json(scalp.get("params_json"), {})
            self.active_scalp_model_version = scalp.get("version")
        for row in self.db.load_segment_adjustments():
            self.params[row.get("segment_key")] = row

    def _ensure_migration(self):
        if self._migration_attempted:
            return
        self._migration_attempted = True
        self._db_ready = self.db.migrate() if self.db.enabled else False

    def _feature_snapshot(self, snap: dict) -> dict:
        keys = [
            "asset", "ticker", "decision_state", "entry_side", "pattern_direction", "pattern_detected",
            "estimated_fair_probability", "raw_model_probability", "win_prob", "entry_ask", "entry_fee",
            "estimated_edge", "required_edge", "max_entry_price", "spread", "yes_ask_qty", "no_ask_qty",
            "confirmation_groups", "confirmation_group_count", "confirmation_score", "model_confidence",
            "seconds_remaining", "underlying_current", "target_value", "target_distance_pct",
            "invalidation_level", "model_sigma_per_sqrt_second", "broader_returns_60s", "end_prediction",
            "taker_yes_volume_15s", "taker_no_volume_15s", "data_age_seconds", "ws_connected",
        ]
        return {key: snap.get(key) for key in keys}

    @staticmethod
    def _estimated_slippage(snap: dict) -> float:
        spread = _f(snap.get("spread"), 0.0)
        filled = _f(snap.get("entry_vwap_filled"), 0.0)
        size = _f(snap.get("entry_vwap_size"), 1.0)
        depth_penalty = max(0.0, size - filled) / max(1.0, size)
        return round(max(0.05, spread * 0.25 + depth_penalty), 4)

    @staticmethod
    def _segment_keys(asset, bucket, signal_type, side, volatility, price_bucket):
        keys = ["global"]
        if asset:
            keys.append(f"asset:{asset}")
        if bucket:
            keys.append(f"time:{bucket}")
        if signal_type:
            keys.append(f"type:{signal_type}")
        if side:
            keys.append(f"side:{side}")
        if volatility:
            keys.append(f"vol:{volatility}")
        if price_bucket:
            keys.append(f"price:{price_bucket}")
        if asset and bucket:
            keys.append(f"asset_time:{asset}|{bucket}")
        if signal_type and bucket:
            keys.append(f"type_time:{signal_type}|{bucket}")
        if asset and volatility:
            keys.append(f"asset_vol:{asset}|{volatility}")
        if side and price_bucket:
            keys.append(f"side_price:{side}|{price_bucket}")
        return keys

    def _segments_for_row(self, row: dict):
        features = _json(row.get("feature_snapshot_json"), {})
        keys = self._segment_keys(
            row.get("asset"),
            _time_bucket(row.get("seconds_remaining")),
            features.get("pattern_detected") or row.get("strategy"),
            row.get("side"),
            row.get("volatility_regime"),
            _price_bucket(row.get("executable_ask")),
        )
        return [(key, self._dimension_for_key(key)) for key in keys]

    @staticmethod
    def _dimension_for_key(key: str) -> str:
        return key.split(":", 1)[0] if ":" in key else "global"

    def _selected_half_life(self) -> float:
        calibration = self._last_fit_metrics.get("calibration") or {}
        metrics = calibration.get("metrics") or {}
        return _f(metrics.get("half_life_days"), self.v4.decay_half_life_days)

    def _promotion_allowed(self) -> bool:
        if not self._promotion_history:
            return True
        latest = _f(self._promotion_history[0].get("at"), 0.0)
        return time.time() - latest >= self.v4.min_hours_between_promotions * 3600.0

    def _effective_sample_summary(self):
        rows = self.db.settled_decisions(limit=10000) if self.db.enabled and self._db_ready else []
        if not rows:
            return {"n": 0, "effective_n": 0.0}
        weights = time_decay_weights(
            [row.get("decision_timestamp") for row in rows], self._selected_half_life()
        )
        return {"n": len(rows), "effective_n": round(effective_sample_size(weights), 3)}

    @staticmethod
    def _apply_calibration_params(params: dict, asset, probability: float) -> float:
        from .learning_math import logit, sigmoid

        temperature = max(0.25, _f(params.get("global_temperature"), 1.0))
        bias = _f(params.get("global_bias"), 0.0)
        asset_bias = _f((params.get("asset_biases") or {}).get(str(asset)), 0.0)
        return clamp(sigmoid(bias + asset_bias + logit(probability) / temperature), 0.01, 0.99)
