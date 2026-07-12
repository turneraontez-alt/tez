from __future__ import annotations

import json
from types import SimpleNamespace

from q15_upgrade.lineage import build_sha
from q15_upgrade.strategy_bots import runtime
from q15_upgrade.interval_research.runner import IntervalResearchRunner
from q15_upgrade.strategy_bots.rules import (
    ACCEPTED,
    BOT_DRIFT_ACCURACY_V91,
    BOT_DRIFT_ASYMMETRIC_VOLUME,
    BOT_DRIFT_BALANCED_V95,
    BOT_DRIFT_FLOW_SPREAD,
    REJECTED,
    RESEARCH_ONLY,
    STRATEGY_VERSION,
)
from tests.test_drift_flow_spread import _row, _Telegram


def _full_evidence(row):
    out = dict(row)
    out.update({
        "drift_evidence_complete": True,
        "drift_v91_yes_fraction_all": 0.80,
        "drift_v91_yes_fraction_directional": 1.0,
        "drift_v91_observation_count": 10,
        "drift_v95_15m_side": "YES",
        "drift_v95_15m_flow_score": 0.25,
        "drift_v95_15m_age_seconds": 120.0,
        "drift_btc_15m_side": "YES",
        "drift_core_breadth": 2,
        "drift_asymmetric_breadth": 3,
        "drift_flow_1m": 100.0,
        "drift_flow_3m": 250.0,
        "drift_flow_5m": 400.0,
        "drift_flow_13m": 900.0,
        "drift_flow_positive_bucket_fraction": 0.77,
        "drift_flow_sign_flips": 2,
        "drift_flow_persistence": 0.62,
        "drift_flow_coverage": 1.0,
        "drift_evidence_availability": {
            name: {
                "available": True,
                "status": "ok",
                "missing_reason": None,
                "source_at": 99.0,
                "age_seconds": 1.0,
            }
            for name in (
                "interval_15m", "v91", "v95_15m", "btc_15m",
                "breadth_13m", "spot_flow",
            )
        },
        "drift_evidence_ages": {"v95_15m": 120.0, "spot_flow": 1.0},
    })
    return out


def _configure(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_STRATEGY_BOTS_ENABLED", "true")
    monkeypatch.setenv("Q15_STRATEGY_BOTS_DB", str(tmp_path / "v3.sqlite3"))
    monkeypatch.setenv("Q15_V3_TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("Q15_V3_DRIFT_FLOW_SPREAD", "true")
    monkeypatch.setenv("Q15_V3_DRIFT_FLOW_SPREAD_NOTIFY", "true")
    monkeypatch.setenv("Q15_V3_DRIFT_OUTBOX_ENABLED", "false")
    monkeypatch.setenv("Q15_BUILD_SHA", "test-build-sha")
    build_sha.cache_clear()
    runtime._ledger = None
    runtime._telegram = _Telegram()
    monkeypatch.setattr(runtime, "enrich_spot_depth", lambda row: dict(row))
    monkeypatch.setattr(runtime, "_enrich_source_row", lambda row, **_: dict(row))
    monkeypatch.setattr(runtime, "enrich_drift_evidence", _full_evidence)
    return runtime._telegram


def test_full_package_records_three_isolated_shadows_without_extra_telegram(
    tmp_path, monkeypatch
):
    telegram = _configure(tmp_path, monkeypatch)
    row = _row(
        spot_depth_status="ok",
        spot_depth_trade_net_notional_60s=100.0,
        spread_cents=1.0,
        source_build_sha="source-sha",
        source_config_hash="source-config",
        source_features_version="interval-capture-v2",
    )
    assert runtime.record_drift_pick_row(row) is not None
    assert len(telegram.sent) == 1
    assert "DRIFT FLOW + SPREAD CONFIRMED 13M" in telegram.sent[0]
    assert "V91: YES/all 80%" in telegram.sent[0]
    assert "V95 15M: YES" in telegram.sent[0]

    rows = runtime.get_ledger().rows(STRATEGY_VERSION)
    by_bot = {item["bot_name"]: item for item in rows}
    assert by_bot[BOT_DRIFT_FLOW_SPREAD]["decision_status"] == ACCEPTED
    for bot in (
        BOT_DRIFT_ASYMMETRIC_VOLUME,
        BOT_DRIFT_BALANCED_V95,
        BOT_DRIFT_ACCURACY_V91,
    ):
        assert by_bot[bot]["decision_status"] == RESEARCH_ONLY
        assert by_bot[bot]["notification_status"] is None

    live = by_bot[BOT_DRIFT_FLOW_SPREAD]
    assert live["build_sha"] == "test-build-sha"
    assert live["source_build_sha"] == "source-sha"
    assert live["feature_cohort"] == "FULL_FEATURE"
    assert live["full_feature_complete"] == 1
    assert live["evidence_grade"] == "A"
    assert json.loads(live["feature_availability_json"])["v91"]["status"] == "ok"
    build_sha.cache_clear()


def test_incremental_distance_never_widens_live_delivery(tmp_path, monkeypatch):
    telegram = _configure(tmp_path, monkeypatch)
    row = _row(
        ticker="ASYM",
        window_key=88,
        entry_ask_cents=68.0,
        distance_sigma=5e-5,
        spot_depth_status="ok",
        spot_depth_trade_net_notional_60s=100.0,
        spread_cents=1.0,
        drift_candidate_lane="ASYMMETRIC_INCREMENTAL_SOURCE",
    )
    assert runtime.record_drift_pick_row(row) is not None
    assert telegram.sent == []
    rows = runtime.get_ledger().rows(STRATEGY_VERSION)
    by_bot = {item["bot_name"]: item for item in rows}
    assert by_bot[BOT_DRIFT_FLOW_SPREAD]["decision_status"] == REJECTED
    assert by_bot[BOT_DRIFT_ASYMMETRIC_VOLUME]["decision_status"] == RESEARCH_ONLY
    assert by_bot[BOT_DRIFT_BALANCED_V95]["decision_status"] == RESEARCH_ONLY
    assert by_bot[BOT_DRIFT_ACCURACY_V91]["decision_status"] == RESEARCH_ONLY
    build_sha.cache_clear()


def test_interval_adapter_forwards_only_incremental_asymmetric_source(monkeypatch):
    runner = object.__new__(IntervalResearchRunner)
    runner.config = SimpleNamespace(model_version="interval-research-v1")

    class Recorder:
        @staticmethod
        def scoreboard():
            return {"book_volume_60_73_all": {"n_resolved": 10, "win_rate": 0.8}}

        @staticmethod
        def spread_weight(_spread):
            return 1.0

        @staticmethod
        def session_weight(_hour):
            return 1.0

        @staticmethod
        def stack_weight(_spread, _hour):
            return 1.0

    recorded = []
    monkeypatch.setattr(runtime, "record_drift_pick_row", lambda row: recorded.append(row))
    base = {
        "captured_at": 1_000.0,
        "asset": "SOL",
        "ticker": "SOL-WIDE",
        "predicted_side": "YES",
        "yes_ask_cents": 68.0,
        "calibrated_yes_probability": 0.75,
        "distance_from_strike": 5e-5,
        "flip_probability": 20.0,
        "spread_cents": 1.0,
        "depth_contracts": 20.0,
        "close_time": 1_780.0,
        "seconds_remaining": 780.0,
        "build_sha": "source-sha",
        "config_hash": "source-config",
        "feature_schema_version": "source-schema",
        "kalshi_depth_status": "ok",
    }
    slate = [
        base,
        {**base, "ticker": "LOW-ASK-WIDE", "yes_ask_cents": 64.0},
        {**base, "ticker": "CORE", "distance_from_strike": 2e-5},
        {**base, "ticker": "BNB", "asset": "BNB"},
    ]
    runner._alert_drift_asymmetric_candidates(Recorder(), slate, 7, 1_000.0)
    assert len(recorded) == 1
    assert recorded[0]["ticker"] == "SOL-WIDE"
    assert recorded[0]["drift_candidate_lane"] == "ASYMMETRIC_INCREMENTAL_SOURCE"
    assert recorded[0]["source_build_sha"] == "source-sha"
    assert recorded[0]["kalshi_depth_status"] == "ok"
