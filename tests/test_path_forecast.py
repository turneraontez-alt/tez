from __future__ import annotations

from dataclasses import replace
import gzip
import json
import math
import sqlite3

import numpy as np
import pytest

from q15_upgrade.path_forecast.ledger import PathForecastLedger
from q15_upgrade.path_forecast.model import PathForecastModel, train_and_audit
from q15_upgrade.path_forecast.reconstruct import (
    ARCHETYPES,
    FEATURE_NAMES,
    PathExample,
    build_live_features,
    label_future_path,
)
from q15_upgrade.path_forecast.runtime import PathForecastRunner


def _point(ts: float, px: float, yes_mid: float = 50.0):
    return {"ts": ts, "px": px, "yes_mid": yes_mid}


def test_live_features_are_point_in_time_and_ignore_future_rows():
    close = 1_800.0
    decision = close - 780.0
    observed = [_point(900.0 + idx * 10.0, 100.0 + idx * 0.001) for idx in range(13)]
    future = [_point(decision + 10.0, 150.0, 99.0)]
    first = build_live_features(
        asset="BTC",
        close_time=close,
        checkpoint_seconds=780,
        target_px=100.0,
        points=observed,
    )[0]
    second = build_live_features(
        asset="BTC",
        close_time=close,
        checkpoint_seconds=780,
        target_px=100.0,
        points=[*observed, *future],
    )[0]
    np.testing.assert_allclose(first, second, equal_nan=True)


def test_live_features_require_a_minute_of_observed_history():
    close = 1_800.0
    decision = close - 780.0
    points = [_point(decision - 35.0 + idx * 5.0, 100.0) for idx in range(8)]
    with pytest.raises(ValueError, match="observed path duration"):
        build_live_features(
            asset="BTC",
            close_time=close,
            checkpoint_seconds=780,
            target_px=100.0,
            points=points,
        )


def test_frozen_path_labels_detect_recovery_and_hard_reversal():
    observed_flat = [_point(float(idx * 10), 100.0) for idx in range(13)]
    future_recovery = [
        _point(130.0, 99.95),
        _point(140.0, 99.97),
        _point(150.0, 100.01),
        _point(160.0, 100.01),
    ]
    label, crossed, turn, trajectory, scale = label_future_path(
        observed_points=observed_flat,
        future_points=future_recovery,
        decision_time=120.0,
        close_time=160.0,
        target_px=100.0,
    )
    assert label == "dip_recovery"
    assert crossed == 1
    assert turn == 10.0
    assert trajectory.shape == (4,)
    assert scale >= 2.0

    observed_up = [_point(float(idx * 10), 100.0 + idx * 0.01) for idx in range(13)]
    future_down = [
        _point(130.0, 100.05),
        _point(140.0, 100.00),
        _point(150.0, 99.95),
        _point(160.0, 99.94),
    ]
    label, _, turn, _, _ = label_future_path(
        observed_points=observed_up,
        future_points=future_down,
        decision_time=120.0,
        close_time=160.0,
        target_px=100.0,
    )
    assert label == "hard_reversal"
    assert turn is not None


def _synthetic_examples() -> list[PathExample]:
    rng = np.random.default_rng(42)
    examples: list[PathExample] = []
    for window in range(60):
        close = 100_000.0 + window * 900.0
        for class_idx, archetype in enumerate(ARCHETYPES):
            features = rng.normal(0.0, 0.15, len(FEATURE_NAMES))
            features[class_idx] += 3.0
            official_yes = class_idx % 2
            crossed = int(class_idx in {1, 2, 3})
            trajectory = np.asarray([
                class_idx - 2.5,
                2.0 * (class_idx - 2.5),
                3.0 * (class_idx - 2.5),
                4.0 * (class_idx - 2.5),
            ])
            examples.append(PathExample(
                asset=("BTC", "ETH", "SOL", "XRP", "DOGE", "HYPE")[class_idx],
                close_time=close,
                ticker=f"T-{window}-{class_idx}",
                checkpoint_seconds=780,
                decision_time=close - 780.0,
                target_px=100.0,
                current_px=100.1 if official_yes else 99.9,
                current_yes_mid=70.0 if official_yes else 30.0,
                features=features,
                archetype=archetype,
                official_yes=official_yes,
                strike_crossed=crossed,
                turn_delay_seconds=60.0 + class_idx if crossed else None,
                trajectory_returns_bps=trajectory,
                label_scale_bps=2.0,
            ))
    return examples


def test_model_training_serialization_and_prediction_are_deterministic(tmp_path):
    model, report = train_and_audit(_synthetic_examples())
    assert report["split"]["test_windows"] == 12
    assert report["archetype"]["accuracy"] > 0.9
    path = tmp_path / "model.npz"
    model.save(str(path))
    loaded = PathForecastModel.load(str(path))
    vector = _synthetic_examples()[0].features
    assert loaded.predict(vector) == model.predict(vector)
    prediction = loaded.predict(vector)
    assert math.isclose(sum(prediction["archetype_probabilities"].values()), 1.0)
    assert prediction["paper_only"] is True


def test_path_forecast_ledger_is_idempotent(tmp_path):
    ledger = PathForecastLedger(str(tmp_path / "forecast.sqlite3"))
    try:
        prediction = {
            "model_version": "m1",
            "feature_schema_version": "f1",
            "top_archetype": "chop",
            "top_archetype_probability": 0.4,
            "settlement_yes_probability": 0.55,
            "strike_cross_probability": 0.3,
            "turn_delay_seconds_q10": 10.0,
            "turn_delay_seconds_q50": 20.0,
            "turn_delay_seconds_q90": 30.0,
        }
        first = ledger.record(
            created_at=1.0,
            asset="BTC",
            close_time=900.0,
            checkpoint_seconds=780,
            decision_time=120.0,
            captured_offset_seconds=0.1,
            target_px=100.0,
            current_px=100.1,
            current_yes_mid=55.0,
            prediction=prediction,
            feature_vector=[1.0, None],
        )
        second = ledger.record(
            created_at=2.0,
            asset="BTC",
            close_time=900.0,
            checkpoint_seconds=780,
            decision_time=120.0,
            captured_offset_seconds=0.2,
            target_px=100.0,
            current_px=100.2,
            current_yes_mid=60.0,
            prediction=prediction,
            feature_vector=[2.0, None],
        )
        assert first == (second[0], True)
        assert second[1] is False
        assert ledger.status()["rows"] == 1
        assert len(ledger.pending(before_close_time=901.0)) == 1
        assert ledger.resolve(
            first[0],
            official_result="YES",
            resolved_at=902.0,
            actual_archetype="steady_up",
            actual_strike_crossed=0,
            settlement_correct=1,
        ) is True
        assert ledger.resolve(
            first[0],
            official_result="NO",
            resolved_at=903.0,
            actual_archetype="steady_down",
            actual_strike_crossed=1,
            settlement_correct=0,
        ) is False
        assert ledger.pending(before_close_time=901.0) == []
        assert ledger.status()["resolved_rows"] == 1
    finally:
        ledger.close()


def test_runtime_records_one_paper_shadow_at_checkpoint(tmp_path):
    model, _ = train_and_audit(_synthetic_examples())
    model = replace(model, audit_summary={**model.audit_summary, "forward_shadow_eligible": True})
    model_path = tmp_path / "model.npz"
    db_path = tmp_path / "forward.sqlite3"
    model.save(str(model_path))
    runner = PathForecastRunner(
        model_path=str(model_path),
        db_path=str(db_path),
        enabled=True,
    )
    close = 9_000.0
    try:
        for remaining in range(900, 779, -5):
            runner.observe(
                asset="BTC",
                close_time=close,
                seconds_remaining=float(remaining),
                target_px=100.0,
                index_px=100.0 + (900 - remaining) * 0.0001,
                spot_px=None,
                yes_bid=49.0,
                yes_ask=51.0,
                now=close - remaining,
            )
        runner._queue.join()
        health = runner.health()
        assert health["rows"] == 1
        assert health["notification_eligible"] is False
        assert health["trading_eligible"] is False
        assert health["latest"]["checkpoint_seconds"] == 780
    finally:
        runner.stop()
        if runner._ledger is not None:
            runner._ledger.close()


def test_runtime_reconciles_forecast_with_durable_path_and_official_result(tmp_path):
    model, _ = train_and_audit(_synthetic_examples())
    model = replace(model, audit_summary={**model.audit_summary, "forward_shadow_eligible": True})
    model_path = tmp_path / "model.npz"
    forecast_db = tmp_path / "forward.sqlite3"
    path_db = tmp_path / "paths.sqlite3"
    metadata_db = tmp_path / "metadata.sqlite3"
    model.save(str(model_path))
    close = 9_000.0
    decision = close - 780.0
    points = [
        _point(ts, 100.0 + 0.002 * ((ts - decision) / 10.0))
        for ts in np.arange(decision - 120.0, close + 1.0, 10.0)
    ]
    with sqlite3.connect(path_db) as conn:
        conn.execute(
            "CREATE TABLE window_paths(asset TEXT,close_time REAL,path_json_gz BLOB)"
        )
        conn.execute(
            "INSERT INTO window_paths VALUES(?,?,?)",
            ("BTC", close, gzip.compress(json.dumps(points).encode("utf-8"))),
        )
    with sqlite3.connect(metadata_db) as conn:
        conn.execute(
            "CREATE TABLE predictions(asset TEXT,close_time REAL,official_result TEXT,resolved_at REAL)"
        )
        conn.execute(
            "INSERT INTO predictions VALUES(?,?,?,?)", ("BTC", close, "YES", close + 5.0)
        )
    runner = PathForecastRunner(
        model_path=str(model_path),
        db_path=str(forecast_db),
        enabled=True,
    )
    runner.path_db = str(path_db)
    runner.metadata_db = str(metadata_db)
    try:
        assert runner._ledger is not None
        prediction = model.predict(_synthetic_examples()[0].features)
        row_id, inserted = runner._ledger.record(
            created_at=decision,
            asset="BTC",
            close_time=close,
            checkpoint_seconds=780,
            decision_time=decision,
            captured_offset_seconds=0.0,
            target_px=100.0,
            current_px=100.0,
            current_yes_mid=50.0,
            prediction=prediction,
            feature_vector=[0.0] * len(FEATURE_NAMES),
        )
        assert row_id > 0 and inserted
        assert runner._reconcile_if_due(now=close + 30.0) == 1
        row = runner._ledger.rows(limit=1)[0]
        assert row["official_result"] == "YES"
        assert row["actual_archetype"] in ARCHETYPES
        assert row["settlement_correct"] in {0, 1}
        assert runner._reconcile_if_due(now=close + 61.0) == 0
    finally:
        runner.stop()
        if runner._ledger is not None:
            runner._ledger.close()
