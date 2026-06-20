from __future__ import annotations

import math
import os
import time

import pytest

from q15_upgrade.end_predictor import EndResultPredictor
from q15_upgrade.learning import LearningEngine, _decision_hash
from q15_upgrade.learning_config import LearningV2Config
from q15_upgrade.learning_math import (
    effective_sample_size,
    fit_temperature_bias,
    grouped_chronological_split,
    probability_clip,
    time_decay_weights,
)


class DisabledStore:
    enabled = False


class FakeDB:
    def __init__(self):
        self.enabled = True
        self.last_error = None
        self.decisions = {}
        self.end_predictions = {}
        self.scalps = {}
        self.segments = {}
        self.models = []
        self.events = []

    def migrate(self):
        return True

    def insert_decision(self, row):
        self.decisions.setdefault(row["decision_key"], dict(row))
        return True

    def insert_end_prediction(self, row):
        self.end_predictions.setdefault(row["prediction_key"], dict(row))
        return True

    def pending_decisions(self, limit=1200):
        return []

    def pending_end_predictions(self, limit=2000):
        return []

    def settled_decisions(self, limit=10000):
        return []

    def settled_scalps(self, limit=10000):
        return []

    def load_active_model(self, model_type):
        return None

    def load_segment_adjustments(self):
        return list(self.segments.values())

    def upsert_segment(self, row):
        self.segments[row["segment_key"]] = dict(row)
        return True

    def insert_model_version(self, row):
        self.models.append(dict(row))
        return True

    def activate_model(self, model_type, version):
        return True

    def record_event(self, event_type, version, details):
        self.events.append((event_type, version, details))
        return True

    def counts(self):
        return {
            "decisions": len(self.decisions),
            "settled_decisions": 0,
            "shadow_decisions": sum(1 for row in self.decisions.values() if row.get("shadow")),
            "scalps": len(self.scalps),
            "settled_scalps": 0,
            "end_predictions": len(self.end_predictions),
        }

    def insert_scalp(self, row):
        self.scalps.setdefault(row["scalp_key"], dict(row))
        return True

    def settle_scalp(self, key, row):
        self.scalps.setdefault(key, {}).update(row)
        return True

    def _execute(self, sql, params=None):
        return True

    def _query(self, sql, params=None):
        return []


def candles(start=100.0, step=0.02, n=50):
    result = []
    price = start
    for index in range(n):
        prior = price
        price += step
        result.append({
            "start_time": index * 5,
            "open": prior,
            "high": max(prior, price) + 0.01,
            "low": min(prior, price) - 0.01,
            "close": price,
        })
    return result


def base_snapshot():
    return {
        "asset": "BTC",
        "ticker": "TEST-BTC",
        "market_state": "live",
        "decision_state": "ENTRY CONFIRMED",
        "entry_status": "ENTRY CONFIRMED",
        "entry_side": "YES",
        "pattern_direction": "bullish",
        "pattern_detected": "target reclaim",
        "estimated_fair_probability": 0.68,
        "win_prob": 0.68,
        "entry_ask": 58.0,
        "entry_fee": 2.0,
        "estimated_edge": 8.0,
        "required_edge": 5.0,
        "max_entry_price": 61.0,
        "spread": 1.0,
        "yes_ask_qty": 50.0,
        "confirmation_groups": {"price_action": {"active": True}},
        "confirmation_group_count": 3,
        "confirmation_score": 65.0,
        "model_confidence": 0.8,
        "seconds_remaining": 300,
        "underlying_current": 100.8,
        "target_value": 100.0,
        "target_distance_pct": 0.8,
        "invalidation_level": 99.8,
        "model_sigma_per_sqrt_second": 0.0001,
        "underlying_candles_5s": candles(),
        "yes_bid": 57.0,
        "yes_ask": 58.0,
        "close_time": "2026-06-18T20:00:00+00:00",
        "data_warning": False,
        "data_age_seconds": 1.0,
    }


def engine_with_fake_db(monkeypatch=None):
    engine = LearningEngine(DisabledStore(), object())
    engine.db = FakeDB()
    engine._db_ready = True
    engine._migration_attempted = True
    return engine


def test_probability_clip():
    assert probability_clip(-5) == 0.01
    assert probability_clip(5) == 0.99
    assert probability_clip(None) is None


def test_time_decay_effective_sample():
    now = time.time()
    weights = time_decay_weights([now - 86400 * i for i in range(10)], 3, now)
    assert weights[0] > weights[-1]
    assert 1 < effective_sample_size(weights) < 10


def test_grouped_chronological_split_keeps_market_window_together():
    rows = []
    for index in range(8):
        close = f"2026-06-{10 + index:02d}T00:00:00+00:00"
        rows.extend([{"market_close": close, "asset": "BTC"}, {"market_close": close, "asset": "ETH"}])
    train, validation = grouped_chronological_split(rows, 0.25)
    train_closes = {row["market_close"] for row in train}
    validation_closes = {row["market_close"] for row in validation}
    assert train_closes.isdisjoint(validation_closes)


def test_calibration_fit_on_overconfident_synthetic_data():
    rows = []
    for index in range(240):
        raw = 0.8 if index % 2 == 0 else 0.2
        # Actual rate is only 65/35, so raw forecasts are overconfident.
        if raw > 0.5:
            outcome = "win" if index % 20 < 13 else "loss"
        else:
            outcome = "win" if index % 20 < 7 else "loss"
        rows.append({
            "asset": "BTC" if index % 3 else "ETH",
            "raw_win_probability": raw,
            "outcome": outcome,
            "decision_timestamp": f"2026-05-{1 + index // 12:02d}T00:{index % 60:02d}:00+00:00",
            "market_close": f"2026-05-{1 + index // 12:02d}T00:00:00+00:00",
        })
    fit = fit_temperature_bias(rows, half_life_days=0, asset_min_effective_n=1000)
    assert fit is not None
    raw_loss = fit.validation_metrics["raw"]["log_loss"]
    calibrated_loss = fit.validation_metrics["calibrated"]["log_loss"]
    assert calibrated_loss <= raw_loss + 0.02
    assert fit.global_temperature > 0


def test_chart_predictor_bullish_result():
    config = LearningV2Config()
    prediction = EndResultPredictor(config).predict(base_snapshot())
    assert prediction.predicted_result in {"YES", "UNCERTAIN"}
    assert prediction.chart_yes_probability is not None
    assert prediction.predicted_final_underlying is not None
    assert prediction.version == "q15-chart-end-v2"


def test_chart_predictor_unavailable_without_target():
    snapshot = base_snapshot()
    snapshot["target_value"] = None
    prediction = EndResultPredictor(LearningV2Config()).predict(snapshot)
    assert prediction.predicted_result == "UNAVAILABLE"


def test_canonical_decision_deduplicates():
    engine = engine_with_fake_db()
    snapshot = base_snapshot()
    now = time.time()
    engine.enrich_and_observe({"BTC": snapshot}, now, {"connected": True, "last_message_at": now})
    engine.enrich_and_observe({"BTC": snapshot}, now + 1, {"connected": True, "last_message_at": now + 1})
    actual = [row for row in engine.db.decisions.values() if row["strategy"] == "settlement"]
    assert len(actual) == 1


def test_suppressed_shadow_decision_is_created():
    engine = engine_with_fake_db()
    snapshot = base_snapshot()
    snapshot["decision_state"] = "WATCH"
    snapshot["entry_status"] = "WATCH"
    snapshot["learned_suppressed"] = True
    snapshot["entry_blockers"] = ["learned_segment_suppression"]
    now = time.time()
    engine.enrich_and_observe({"BTC": snapshot}, now, {"connected": True, "last_message_at": now})
    rows = list(engine.db.decisions.values())
    assert any(row["strategy"] == "shadow:suppressed_recovery" and row["shadow"] for row in rows)


def test_shadow_mode_does_not_apply_adjustment():
    engine = engine_with_fake_db()
    engine.params = {
        "asset:BTC": {
            "segment_key": "asset:BTC",
            "active_edge_adjustment": 2.0,
            "suppressed": True,
        }
    }
    result = engine.adjustment_for("BTC", "2-6m", "target reclaim", "YES")
    assert result["edge_adjustment"] == 0
    assert result["suppressed"] is False
    assert result["shadow_recommendations"]


def test_calibration_is_identity_without_active_model():
    engine = engine_with_fake_db()
    result = engine.calibrate_probability("BTC", 0.7, "YES")
    assert result["calibrated_probability"] == pytest.approx(0.7)
    assert result["apply_live"] is False


def test_scalp_requires_probability_and_positive_ev():
    engine = engine_with_fake_db()
    strong = engine.assess_scalp({
        "target_net_cents": 10,
        "spread": 1,
        "reversal_score": 92,
        "observed_extension": 20,
        "time_remaining": 300,
        "entry_price": 45,
        "stop_price": 39,
        "estimated_fee": 2,
    })
    weak = engine.assess_scalp({
        "target_net_cents": 10,
        "spread": 3,
        "reversal_score": 70,
        "observed_extension": 10,
        "time_remaining": 70,
        "entry_price": 45,
        "stop_price": 39,
        "estimated_fee": 2,
    })
    assert strong["target_probability"] > weak["target_probability"]
    assert strong["expected_net_ev_cents"] > weak["expected_net_ev_cents"]
    assert "not a guarantee" in strong["warning"]


def test_scalp_timeout_is_recorded_as_censored():
    engine = engine_with_fake_db()
    sig = {
        "ticker": "TEST",
        "asset": "BTC",
        "side": "YES",
        "entry": 50,
        "entry_exec": 50,
        "target": 60,
        "stop": 44,
        "fee": 2,
        "spread": 1,
        "secs": 300,
        "opened_at": 1000,
        "peak": 55,
        "trough": 47,
        "learning_assessment": engine.assess_scalp({
            "target_net_cents": 10, "spread": 1, "reversal_score": 90,
            "observed_extension": 18, "time_remaining": 300,
            "entry_price": 50, "stop_price": 44, "estimated_fee": 2,
        }),
        "learning_features": {},
    }
    engine.record_scalp_open(sig, 1000)
    engine.record_scalp_resolution(sig, 1100, 52, "exit_before_close", 0)
    row = engine.db.scalps[sig["learning_scalp_key"]]
    assert row["expired_without_target_or_stop"] is True
    assert row["target_hit"] is False
    assert row["stop_hit"] is False


def test_stale_data_activates_learning_circuit_breaker():
    engine = engine_with_fake_db()
    snapshot = base_snapshot()
    snapshot["data_age_seconds"] = 99
    now = time.time()
    engine.enrich_and_observe({"BTC": snapshot}, now, {"connected": True, "last_message_at": now})
    assert "snapshot_data_stale" in engine.health_summary()["circuit_breakers"]


def test_end_prediction_is_exposed_in_snapshot():
    engine = engine_with_fake_db()
    snapshot = base_snapshot()
    now = time.time()
    result = engine.enrich_and_observe({"BTC": snapshot}, now, {"connected": True, "last_message_at": now})
    assert "end_prediction" in result["BTC"]
    assert "end_prediction_result" in result["BTC"]


def test_decision_hash_stable():
    assert _decision_hash("a", "b") == _decision_hash("a", "b")
    assert _decision_hash("a", "b") != _decision_hash("b", "a")


def test_module_contains_no_order_submission_api():
    import inspect
    import q15_upgrade.learning as learning_module

    source = inspect.getsource(learning_module).lower()
    forbidden = ["place_order(", "submit_order(", "create_order("]
    assert not any(token in source for token in forbidden)
