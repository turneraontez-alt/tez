from __future__ import annotations

import os
import tempfile

import pytest

from q15_upgrade.marketlead.config import MarketLeadConfig
from q15_upgrade.marketlead.features import MarketLeadFeatureEngine
from q15_upgrade.marketlead.runner import MarketLeadRunner
from q15_upgrade.ws_client import KalshiWebSocketFeed
from tools.q15_marketlead_report import build_report


class _Canonical:
    def __init__(self, *, ticker="KXBTC-TEST", seconds=780.0, public=None):
        self.ticker = ticker
        self.seconds_remaining = seconds
        self.settlement_time = 1_900_000_800.0
        self.spot = 101.0
        self.threshold = 100.0
        self.yes_is_higher = True
        self.public = public or {}


def _source(price, timestamp, flow=0.5, microprice_bps=1.0):
    return {
        "price": price,
        "timestamp": timestamp,
        "quality": 1.0,
        "flow": {"imbalance": flow},
        "book": {"imbalance": flow / 2.0, "microprice_bps": microprice_bps},
    }


def _public(now, prices=(100.0, 100.1, 99.9)):
    return {
        "fetched_at": now,
        "sources": {
            "coinbase": _source(prices[0], now),
            "kraken": _source(prices[1], now),
            "okx": _source(prices[2], now),
        },
    }


def _analysis(side="YES"):
    return {
        "prediction_side": side,
        "entry_ask_cents": 68.0,
        "quote": {"ask_cents": 68.0, "spread_cents": 2.0},
    }


def _kalshi():
    return {
        "available": True,
        "book_age_seconds": 0.2,
        "event_age_seconds": 0.1,
        "yes_bid_cents": 66.0,
        "yes_ask_cents": 68.0,
        "yes_microprice_cents": 67.4,
        "yes_microprice_edge_cents": 0.4,
        "book_delta_pressure_yes_5s": 0.4,
        "book_delta_pressure_yes_15s": 0.5,
        "book_delta_pressure_yes_30s": 0.3,
        "trade_imbalance_yes_15s": 0.6,
    }


def _config(path, **kwargs):
    values = dict(
        enabled=True,
        db_path=path,
        system_version="marketlead-test",
        mark_seconds=780,
        mark_band_seconds=25.0,
        crossing_max_seconds=90.0,
        crossing_max_offset_seconds=45.0,
        min_proxy_sources=2,
        source_stale_seconds=20.0,
        sync_tolerance_seconds=10.0,
        history_seconds=300.0,
    )
    values.update(kwargs)
    return MarketLeadConfig(**values)


def test_kalshi_event_microstructure_tracks_pressure_and_microprice(monkeypatch):
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_ACCESS_KEY", raising=False)
    feed = KalshiWebSocketFeed()
    now = 1_900_000_000.0
    feed._handle_book_snapshot(
        {
            "market_ticker": "KXBTC",
            "yes_dollars": [["0.6600", "10"]],
            "no_dollars": [["0.3200", "30"]],
        },
        now,
    )
    feed._handle_book_delta(
        {
            "market_ticker": "KXBTC",
            "side": "yes",
            "price_dollars": "0.6600",
            "delta_fp": "5",
            "ts_ms": int((now + 1) * 1000),
        },
        now + 1,
    )
    feed._handle_trade(
        {
            "market_ticker": "KXBTC",
            "yes_price_dollars": "0.6700",
            "count_fp": "4",
            "taker_side": "yes",
            "ts_ms": int((now + 1) * 1000),
        },
        now + 1,
    )
    metrics = feed.get_microstructure("KXBTC", now=now + 2)
    assert metrics["available"] is True
    assert metrics["yes_bid_cents"] == 66.0
    assert metrics["yes_ask_cents"] == 68.0
    assert metrics["yes_microprice_edge_cents"] == pytest.approx(-1 / 3)
    assert metrics["book_delta_pressure_yes_5s"] == pytest.approx(1.0)
    assert metrics["trade_imbalance_yes_15s"] == pytest.approx(1.0)
    assert metrics["event_count_5s"] == 1


def test_feature_engine_builds_ready_joint_alignment(tmp_path):
    config = _config(str(tmp_path / "marketlead.sqlite3"))
    engine = MarketLeadFeatureEngine(config)
    first = _Canonical(public=_public(100.0, (100.0, 100.0, 100.0)))
    engine.build(
        asset="BTC",
        analysis=_analysis(),
        canonical=first,
        now=100.0,
        official_index={"index_px": 100.0},
        kalshi=_kalshi(),
    )
    second = _Canonical(public=_public(115.0, (100.3, 100.4, 100.2)))
    row = engine.build(
        asset="BTC",
        analysis=_analysis(),
        canonical=second,
        now=115.0,
        official_index={"index_px": 100.1},
        kalshi=_kalshi(),
    )
    assert row["evidence_status"] == "READY"
    assert row["rti_proxy_price"] == pytest.approx(100.35)
    assert row["rti_proxy_source_count"] == 2
    assert "okx" not in row["rti_proxy_sources_json"]
    assert row["proxy_lead_side_bps"] > 0
    assert row["venue_impulse_side"] > 0
    assert row["venue_aligned_fraction"] == pytest.approx(1.0)
    assert row["kalshi_pressure_side"] > 0
    assert row["joint_alignment"] == 1


def test_feature_engine_fails_closed_with_missing_lanes(tmp_path):
    config = _config(str(tmp_path / "marketlead.sqlite3"))
    engine = MarketLeadFeatureEngine(config)
    row = engine.build(
        asset="BTC",
        analysis=_analysis(),
        canonical=_Canonical(public={}),
        now=100.0,
        official_index={},
        kalshi={"available": False, "reason": "missing"},
    )
    assert row["evidence_status"] == "PARTIAL"
    assert row["joint_alignment"] == 0
    assert "RTI_PROXY_INCOMPLETE" in row["missing_reasons_json"]
    assert "KALSHI_EVENTS_INCOMPLETE" in row["missing_reasons_json"]


def test_proxy_direction_remains_usable_when_official_gap_is_unavailable(tmp_path):
    config = _config(str(tmp_path / "marketlead.sqlite3"))
    engine = MarketLeadFeatureEngine(config)
    engine.build(
        asset="SOL",
        analysis=_analysis(),
        canonical=_Canonical(public=_public(100.0, (100.2, 100.3, 75.0))),
        now=100.0,
        official_index={},
        kalshi=_kalshi(),
    )
    row = engine.build(
        asset="SOL",
        analysis=_analysis(),
        canonical=_Canonical(public=_public(115.0, (100.5, 100.6, 70.0))),
        now=115.0,
        official_index={},
        kalshi=_kalshi(),
    )
    assert row["evidence_status"] == "READY"
    assert row["proxy_lead_side_bps"] is None
    assert row["proxy_distance_side_bps"] > 0
    assert row["joint_alignment"] == 1
    assert "OFFICIAL_INDEX_GAP_UNAVAILABLE" in row["limitations_json"]


def test_runner_records_once_and_resolves_without_delivery_surface(tmp_path):
    db_path = str(tmp_path / "marketlead.sqlite3")
    runner = MarketLeadRunner(
        _config(db_path),
        microstructure_provider=lambda ticker, now=None: _kalshi(),
        index_provider=lambda asset, spot, now: {"index_px": 100.0},
    )
    canonical = _Canonical(public=_public(100.0, (100.2, 100.3, 100.1)))
    runner.observe(
        analyses={"BTC": _analysis()},
        canonicals={"BTC": canonical},
        now=100.0,
    )
    runner.observe(
        analyses={"BTC": _analysis()},
        canonicals={"BTC": canonical},
        now=101.0,
    )
    rows = runner.ledger.rows()
    assert len(rows) == 1
    assert rows[0]["evidence_status"] == "READY"
    assert runner.resolve_settled(
        [{"ticker": canonical.ticker, "result": "YES"}], 200.0
    ) == 1
    resolved = runner.ledger.rows()[0]
    assert resolved["correct"] == 1
    assert resolved["realized_pnl_cents"] == pytest.approx(32.0)
    status = runner.status()
    assert status["paper_only"] is True
    assert status["notifies"] is False
    assert status["trades"] is False
    report = build_report(db_path)
    assert report["target_status"] == "COLLECTING"
    assert report["coverage"]["observations"] == 1
    assert report["candidate_overall"]["resolved"] == 1


def test_runner_captures_nearest_endpoint_on_fresh_crossing(tmp_path):
    runner = MarketLeadRunner(
        _config(str(tmp_path / "marketlead.sqlite3")),
        microstructure_provider=lambda ticker, now=None: _kalshi(),
        index_provider=lambda asset, spot, now: {"index_px": 100.0},
    )
    runner.observe(
        analyses={"BTC": _analysis()},
        canonicals={"BTC": _Canonical(seconds=800.0, public=_public(100.0))},
        now=100.0,
    )
    runner.observe(
        analyses={"BTC": _analysis()},
        canonicals={"BTC": _Canonical(seconds=770.0, public=_public(120.0, (100.2, 100.3, 100.1)))},
        now=120.0,
    )
    rows = runner.ledger.rows()
    assert len(rows) == 1
    assert rows[0]["seconds_remaining"] == pytest.approx(770.0)


def test_runner_records_paper_touch_and_side_markouts(tmp_path):
    def microstructure(ticker, now=None):
        elapsed = float(now or 0.0) - 100.0
        yes_mid = 67.0 - max(0.0, elapsed) / 10.0
        return {
            **_kalshi(),
            "yes_mid_cents": yes_mid,
            "no_ask_cents": 34.0,
            "yes_bid_queue_at_or_above_limit": 12.0,
            "no_bid_queue_at_or_above_limit": 3.0,
        }

    runner = MarketLeadRunner(
        _config(str(tmp_path / "marketlead.sqlite3")),
        microstructure_provider=microstructure,
        index_provider=lambda asset, spot, now: {"index_px": 100.0},
    )
    canonical = _Canonical(seconds=780.0, public=_public(100.0))
    runner.observe(
        analyses={"BTC": _analysis()}, canonicals={"BTC": canonical}, now=100.0
    )
    for elapsed in (5.0, 15.0, 30.0):
        canonical.seconds_remaining = 780.0 - elapsed
        canonical.public = _public(100.0 + elapsed)
        runner.observe(
            analyses={"BTC": _analysis()},
            canonicals={"BTC": canonical},
            now=100.0 + elapsed,
        )
    row = runner.ledger.rows()[0]
    assert row["paper_limit_touched"] == 1
    assert row["paper_touch_price_cents"] == pytest.approx(68.0)
    assert row["paper_queue_ahead_contracts"] == pytest.approx(12.0)
    assert row["markout_side_5s_cents"] == pytest.approx(-1.5)
    assert row["markout_side_15s_cents"] == pytest.approx(-2.5)
    assert row["markout_side_30s_cents"] == pytest.approx(-4.0)
    assert row["execution_status"] == "COMPLETE"
