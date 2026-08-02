from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time

import pytest

import coinbase_adv_l2
import kraken_l3
import q15_upgrade.marketlead.runner as marketlead_runner_module
from coinbase_adv_l2 import CoinbaseAdvancedL2Collector
from kraken_l3 import KrakenL3Collector
from q15_upgrade.marketlead.config import MarketLeadConfig
from q15_upgrade.marketlead.features import MarketLeadFeatureEngine
from q15_upgrade.marketlead.ledger import MarketLeadLedger
from q15_upgrade.marketlead.live_sources import live_market_sources
from q15_upgrade.marketlead.runner import MarketLeadRunner
from q15_upgrade.strategy_bots.telegram import build_marketlead_alert
from q15_upgrade.strategy_bots.rules import (
    RTI_EXACT_MICROSTRUCTURE_EXTENSION_SCHEMA_VERSION,
)
from q15_upgrade.ws_client import (
    MICROSTRUCTURE_BOOK_EVENT_RETENTION_SECONDS,
    MICROSTRUCTURE_EXTENSION_SCHEMA_VERSION,
    MICROSTRUCTURE_TIME_BASIS,
    KalshiWebSocketFeed,
)
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
        "transport_connected": True,
        "transport_message_age_seconds": 0.0,
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


def _lagging_kalshi(*, age=0.2):
    return {
        **_kalshi(),
        "book_age_seconds": age,
        "event_age_seconds": age,
        "yes_microprice_edge_cents": -0.4,
        "book_delta_pressure_yes_15s": -0.5,
        "trade_imbalance_yes_15s": -0.6,
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
        min_venue_sources=2,
        source_stale_seconds=20.0,
        transport_stale_seconds=10.0,
        source_future_tolerance_seconds=0.5,
        max_source_spread_bps=50.0,
        sync_tolerance_seconds=10.0,
        max_proxy_dispersion_bps=25.0,
        require_live_proxy_sources=False,
        history_seconds=300.0,
    )
    values.update(kwargs)
    return MarketLeadConfig(**values)


def test_kalshi_event_microstructure_tracks_pressure_and_microprice(monkeypatch):
    assert MICROSTRUCTURE_EXTENSION_SCHEMA_VERSION == (
        RTI_EXACT_MICROSTRUCTURE_EXTENSION_SCHEMA_VERSION
    )
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
    assert metrics["yes_fill_10x2c"]["full_fill_supported"] is True
    assert metrics["yes_fill_10x2c"]["vwap_cents"] == 68.0
    assert metrics["no_fill_10x2c"]["full_fill_supported"] is True
    assert metrics["no_fill_10x2c"]["vwap_cents"] == 34.0
    assert metrics["book_delta_pressure_yes_5s"] == pytest.approx(1.0)
    assert metrics["trade_imbalance_yes_15s"] == pytest.approx(1.0)
    assert metrics["event_count_5s"] == 1
    assert metrics["taker_yes_volume_15s"] == pytest.approx(4.0)
    assert metrics["taker_no_volume_15s"] == pytest.approx(0.0)
    assert metrics["taker_net_yes_volume_15s"] == pytest.approx(4.0)
    assert metrics["microstructure_extension_schema_version"] == (
        MICROSTRUCTURE_EXTENSION_SCHEMA_VERSION
    )
    assert metrics["book_add_volume_yes_5s"] == pytest.approx(5.0)
    assert metrics["book_remove_volume_yes_5s"] == pytest.approx(0.0)
    assert metrics["microprice_change_cents_5s"] == pytest.approx(1.0 / 6.0)
    assert metrics["microprice_range_cents_5s"] == pytest.approx(1.0 / 6.0)
    assert metrics["microprice_variation_cents_5s"] == pytest.approx(1.0 / 6.0)
    assert metrics["microprice_trend_efficiency_5s"] == pytest.approx(1.0)
    assert metrics["trade_yes_price_change_cents_5s"] == pytest.approx(0.0)
    assert metrics["trade_yes_price_range_cents_5s"] == pytest.approx(0.0)
    assert metrics["trade_yes_vwap_cents_5s"] == pytest.approx(67.0)


def test_kalshi_book_removes_float_dust_instead_of_creating_ghost_best_bid(monkeypatch):
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_ACCESS_KEY", raising=False)
    feed = KalshiWebSocketFeed()
    now = 1_900_000_000.0
    feed._handle_book_snapshot(
        {
            "market_ticker": "KXDUST",
            "yes_dollars": [["0.9900", "0.1"], ["0.6000", "12"]],
            "no_dollars": [["0.3800", "15"]],
        },
        now,
    )
    for delta in ("0.2", "-0.3"):
        feed._handle_book_delta(
            {
                "market_ticker": "KXDUST",
                "side": "yes",
                "price_dollars": "0.9900",
                "delta_fp": delta,
            },
            now + 1,
        )

    metrics = feed.get_microstructure("KXDUST", now=now + 1.1)
    assert metrics["available"] is True
    assert metrics["yes_bid_cents"] == 60.0
    assert metrics["yes_ask_cents"] == 62.0
    assert metrics["yes_bid_qty"] == 12.0


def test_kalshi_microstructure_uses_genuine_time_horizons_above_5000_events(
    monkeypatch,
):
    """Busy BTC windows must never collapse to the most recent 5,000 rows."""
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_ACCESS_KEY", raising=False)
    feed = KalshiWebSocketFeed()
    ticker = "KXBTC-HIGH-ACTIVITY"
    now = 1_900_000_100.0
    feed._handle_book_snapshot(
        {
            "market_ticker": ticker,
            "yes_dollars": [["0.5500", "10"]],
            "no_dollars": [["0.4400", "10"]],
        },
        now - 70.0,
    )
    with feed._lock:
        feed._books[ticker]["updated_at"] = now
        events = feed._book_events[ticker]
        for index in range(6001):
            received_at = now - 60.0 + index * 0.01
            events.append({
                # Deliberately unusable exchange time: window membership must
                # use the timestamp at which the decision process saw the row.
                "ts": now + 600.0,
                "received_at": received_at,
                "side": "no" if index < 3000 else "yes",
                "delta": 1.0,
                "at_best_before": False,
                "at_best_after": False,
            })
        # Future decision evidence must not leak into a point-in-time feature.
        events.append({
            "ts": now - 1.0,
            "received_at": now + 1.0,
            "side": "yes",
            "delta": 1.0,
            "at_best_before": False,
            "at_best_after": False,
        })

    metrics = feed.get_microstructure(ticker, now=now)

    assert feed._book_events[ticker].maxlen is None
    assert metrics["microstructure_time_basis"] == MICROSTRUCTURE_TIME_BASIS
    assert metrics["history_count_capped"] is False
    assert metrics["book_window_complete_60s"] is True
    assert metrics["trade_window_complete_60s"] is True
    assert metrics["microstructure_window_complete_60s"] is True
    assert metrics["event_count_60s"] == 6001
    assert metrics["event_count_30s"] == 3001
    assert metrics["event_count_5s"] == 501
    assert metrics["book_delta_pressure_yes_30s"] == pytest.approx(1.0)
    assert metrics["book_delta_pressure_yes_60s"] == pytest.approx(1.0 / 6001.0)
    assert metrics["trade_imbalance_yes_60s"] == pytest.approx(0.0)
    health = feed.health()["microstructure_history"]
    assert health["count_capped"] is False
    assert health["time_basis"] == "local_received_at"
    assert health["extension_schema_version"] == (
        MICROSTRUCTURE_EXTENSION_SCHEMA_VERSION
    )
    assert health["buffers"][ticker]["book_event_rows"] == 6002


def test_kalshi_microstructure_prunes_by_receive_time_not_count(monkeypatch):
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_ACCESS_KEY", raising=False)
    feed = KalshiWebSocketFeed()
    ticker = "KXBTC-TIME-RETENTION"
    now = 1_900_000_200.0
    feed._handle_book_snapshot(
        {
            "market_ticker": ticker,
            "yes_dollars": [["0.5500", "10"]],
            "no_dollars": [["0.4400", "10"]],
        },
        now - 100.0,
    )
    with feed._lock:
        feed._books[ticker]["updated_at"] = now
        for received_at, microprice in (
            (now - 95.0, 49.0),
            (now - 10.0, 55.5),
        ):
            feed._book_events[ticker].append({
                "ts": received_at,
                "received_at": received_at,
                "side": "yes",
                "delta": 1.0,
                "at_best_before": False,
                "at_best_after": False,
                "yes_microprice_after_cents": microprice,
            })

    metrics = feed.get_microstructure(ticker, now=now)

    assert MICROSTRUCTURE_BOOK_EVENT_RETENTION_SECONDS == 90.0
    assert len(feed._book_events[ticker]) == 1
    assert metrics["event_count_60s"] == 1
    assert feed._book_retention_baseline_microprice[ticker] == pytest.approx(49.0)
    assert metrics["microprice_change_cents_60s"] == pytest.approx(6.5)
    assert metrics["microprice_range_cents_60s"] == pytest.approx(6.5)


def test_kalshi_microstructure_history_fails_closed_until_rewarmed(monkeypatch):
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_ACCESS_KEY", raising=False)
    feed = KalshiWebSocketFeed()
    ticker = "KXBTC-RECONNECT"
    now = 1_900_000_300.0
    snapshot = {
        "market_ticker": ticker,
        "yes_dollars": [["0.5500", "10"]],
        "no_dollars": [["0.4400", "10"]],
    }
    feed._handle_book_snapshot(snapshot, now)

    cold = feed.get_microstructure(ticker, now=now + 59.0, max_book_age=1000.0)
    warm = feed.get_microstructure(ticker, now=now + 60.1, max_book_age=1000.0)
    assert cold["microstructure_window_complete_60s"] is False
    assert warm["microstructure_window_complete_60s"] is True
    assert warm["microprice_change_cents_60s"] == pytest.approx(0.0)
    assert warm["microprice_variation_cents_60s"] == pytest.approx(0.0)
    assert warm["book_add_volume_yes_60s"] == pytest.approx(0.0)
    assert warm["book_remove_volume_no_60s"] == pytest.approx(0.0)
    assert warm["trade_yes_price_change_cents_60s"] == pytest.approx(0.0)
    assert warm["trade_yes_vwap_cents_60s"] is None

    with feed._lock:
        feed._invalidate_microstructure_history_locked()
    invalidated = feed.get_microstructure(
        ticker, now=now + 61.0, max_book_age=1000.0,
    )
    assert invalidated["book_history_started_at"] is None
    assert invalidated["microstructure_window_complete_5s"] is False

    feed._handle_book_snapshot(snapshot, now + 62.0)
    rewarmed = feed.get_microstructure(
        ticker, now=now + 122.1, max_book_age=1000.0,
    )
    assert rewarmed["book_window_complete_60s"] is True
    assert rewarmed["trade_window_complete_60s"] is True


def test_quiet_stale_book_keeps_flow_history_but_not_execution_quote(monkeypatch):
    """Book mutation age must not erase continuously observed zero-flow history."""
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_ACCESS_KEY", raising=False)
    feed = KalshiWebSocketFeed()
    ticker = "KXBTC-QUIET-HISTORY"
    now = 1_900_000_500.0
    feed._handle_book_snapshot({
        "market_ticker": ticker,
        "yes_dollars": [["0.5500", "10"]],
        "no_dollars": [["0.4400", "10"]],
    }, now - 70.0)
    with feed._lock:
        feed._connected = True
        feed._last_message_at = now - 0.1

    metrics = feed.get_microstructure(ticker, now=now, max_book_age=2.0)

    assert metrics["available"] is False
    assert metrics["reason"] == "book_stale"
    assert metrics["microstructure_evidence_source"] == (
        "kalshi_official_websocket_history"
    )
    assert metrics["microstructure_transport_connected"] is True
    assert metrics["microstructure_transport_age_seconds"] == pytest.approx(0.1)
    assert metrics["microstructure_window_complete_60s"] is True
    assert metrics["book_delta_pressure_yes_60s"] == pytest.approx(0.0)
    assert metrics["trade_imbalance_yes_60s"] == pytest.approx(0.0)


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
    assert row["lead_lag_candidate"] == 0


def test_feature_engine_builds_external_lead_kalshi_lag_candidate(tmp_path):
    config = _config(str(tmp_path / "marketlead.sqlite3"))
    engine = MarketLeadFeatureEngine(config)
    engine.build(
        asset="BTC",
        analysis=_analysis(),
        canonical=_Canonical(public=_public(100.0, (100.0, 100.0, 100.0))),
        now=100.0,
        official_index={"index_px": 100.0},
        kalshi=_kalshi(),
    )
    lagging_kalshi = {
        **_kalshi(),
        "yes_microprice_edge_cents": -0.4,
        "book_delta_pressure_yes_15s": -0.5,
        "trade_imbalance_yes_15s": -0.6,
    }
    row = engine.build(
        asset="BTC",
        analysis=_analysis(),
        canonical=_Canonical(public=_public(115.0, (100.3, 100.4, 100.2))),
        now=115.0,
        official_index={"index_px": 100.1},
        kalshi=lagging_kalshi,
    )
    payload = json.loads(row["features_json"])

    assert row["evidence_status"] == "READY"
    assert row["proxy_distance_side_bps"] > 0
    assert row["venue_impulse_side"] > 0
    assert row["kalshi_pressure_side"] <= -0.10
    assert row["joint_alignment"] == 0
    assert row["lead_lag_candidate"] == 1
    assert payload["candidate"]["lead_lag_candidate"] is True
    assert payload["candidate"]["rule_version"] == "external-lead-kalshi-lag-v1"


def test_feature_engine_strict_freshness_rejects_stale_kalshi_events(tmp_path):
    config = _config(
        str(tmp_path / "marketlead.sqlite3"), kalshi_stale_seconds=3.0
    )
    row = MarketLeadFeatureEngine(config).build(
        asset="BTC",
        analysis=_analysis(),
        canonical=_Canonical(public=_public(100.0, (100.2, 100.3, 100.1))),
        now=100.0,
        official_index={"index_px": 100.0},
        kalshi=_lagging_kalshi(age=3.01),
    )
    payload = json.loads(row["features_json"])

    assert row["evidence_status"] == "PARTIAL"
    assert row["lead_lag_candidate"] == 0
    assert "KALSHI_EVENTS_STALE" in row["missing_reasons_json"]
    assert payload["kalshi_freshness"]["fresh"] is False


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


def test_feature_engine_prefers_fresh_live_cluster_and_drops_rest_outlier(tmp_path):
    config = _config(
        str(tmp_path / "marketlead.sqlite3"),
        source_stale_seconds=3.0,
        sync_tolerance_seconds=2.0,
        require_live_proxy_sources=True,
    )
    engine = MarketLeadFeatureEngine(config)

    def live(now, coinbase, kraken):
        return {
            "sources": {
                "coinbase": {
                    **_source(coinbase, now, flow=0.4),
                    "transport": "websocket_l2",
                    "book_update_age_seconds": 30.0,
                    "best_bid": coinbase - 0.01,
                    "best_ask": coinbase + 0.01,
                },
                "kraken": {
                    **_source(kraken, now - 0.4, flow=0.3),
                    "transport": "websocket_l3",
                    "book_update_age_seconds": 20.0,
                    "best_bid": kraken - 0.01,
                    "best_ask": kraken + 0.01,
                },
            },
            "diagnostics": {"coinbase": {"status": "ok"}, "kraken": {"status": "ok"}},
        }

    engine.build(
        asset="BNB",
        analysis=_analysis(),
        canonical=_Canonical(public={"sources": {"okx": _source(99.0, 97.2)}}),
        now=100.0,
        official_index={"index_px": 100.0},
        kalshi=_kalshi(),
        live_sources=live(100.0, 100.0, 100.05),
    )
    row = engine.build(
        asset="BNB",
        analysis=_analysis(),
        canonical=_Canonical(public={"sources": {"okx": _source(99.0, 112.2)}}),
        now=115.0,
        official_index={"index_px": 100.1},
        kalshi=_kalshi(),
        live_sources=live(115.0, 100.3, 100.35),
    )
    payload = json.loads(row["features_json"])

    assert row["evidence_status"] == "READY"
    assert row["rti_proxy_source_count"] == 2
    assert payload["source_selection"]["venue_cluster"] == ["kraken", "coinbase"]
    assert "okx" not in payload["source_selection"]["venue_cluster"]
    assert all(
        source["transport"].startswith("websocket_")
        for source in payload["venue"]["sources"]
    )


def test_feature_engine_rejects_stale_or_dispersed_live_proxy(tmp_path):
    config = _config(
        str(tmp_path / "marketlead.sqlite3"),
        source_stale_seconds=3.0,
        sync_tolerance_seconds=2.0,
        max_proxy_dispersion_bps=25.0,
        require_live_proxy_sources=True,
    )
    engine = MarketLeadFeatureEngine(config)
    base = {
        "coinbase": {
            **_source(100.0, 100.0),
            "transport": "websocket_l2",
            "best_bid": 99.99,
            "best_ask": 100.01,
        },
        "kraken": {
            **_source(100.1, 96.0),
            "transport": "websocket_l3",
            "best_bid": 100.09,
            "best_ask": 100.11,
        },
    }
    stale = engine.build(
        asset="XRP",
        analysis=_analysis(),
        canonical=_Canonical(public={}),
        now=100.0,
        official_index={"index_px": 100.0},
        kalshi=_kalshi(),
        live_sources={"sources": base},
    )
    dispersed_sources = {
        "coinbase": {
            **_source(100.0, 115.0),
            "transport": "websocket_l2",
            "best_bid": 99.99,
            "best_ask": 100.01,
        },
        "kraken": {
            **_source(101.0, 114.8),
            "transport": "websocket_l3",
            "best_bid": 100.99,
            "best_ask": 101.01,
        },
    }
    dispersed = engine.build(
        asset="XRP",
        analysis=_analysis(),
        canonical=_Canonical(public={}),
        now=115.0,
        official_index={"index_px": 100.0},
        kalshi=_kalshi(),
        live_sources={"sources": dispersed_sources},
    )

    assert stale["evidence_status"] == "PARTIAL"
    assert stale["rti_proxy_source_count"] == 1
    assert "RTI_PROXY_INCOMPLETE" in stale["missing_reasons_json"]
    assert dispersed["evidence_status"] == "PARTIAL"
    assert dispersed["rti_proxy_price"] is None
    assert "RTI_PROXY_DISPERSION_HIGH" in dispersed["missing_reasons_json"]


def test_feature_engine_rejects_future_or_wide_live_books(tmp_path):
    config = _config(
        str(tmp_path / "marketlead.sqlite3"),
        source_stale_seconds=3.0,
        source_future_tolerance_seconds=0.5,
        max_source_spread_bps=50.0,
        require_live_proxy_sources=True,
    )
    engine = MarketLeadFeatureEngine(config)
    sources = {
        "coinbase": {
            **_source(100.0, 101.0),
            "transport": "websocket_l2",
            "best_bid": 99.99,
            "best_ask": 100.01,
        },
        "kraken": {
            **_source(100.0, 100.0),
            "transport": "websocket_l3",
            "best_bid": 99.0,
            "best_ask": 101.0,
        },
    }

    row = engine.build(
        asset="HYPE",
        analysis=_analysis(),
        canonical=_Canonical(public={}),
        now=100.0,
        official_index={"index_px": 100.0},
        kalshi=_kalshi(),
        live_sources={"sources": sources},
    )
    payload = json.loads(row["features_json"])

    assert row["evidence_status"] == "PARTIAL"
    assert row["rti_proxy_source_count"] == 0
    assert payload["source_selection"]["fresh_sources"] == []


def test_feature_engine_rejects_live_transport_beyond_silence_ceiling(tmp_path):
    config = _config(
        str(tmp_path / "marketlead.sqlite3"),
        source_stale_seconds=3.0,
        transport_stale_seconds=10.0,
        require_live_proxy_sources=True,
    )
    sources = {
        "coinbase": {
            **_source(100.0, 100.0),
            "transport": "websocket_l2",
            "transport_message_age_seconds": 10.1,
            "best_bid": 99.99,
            "best_ask": 100.01,
        },
        "kraken": {
            **_source(100.05, 100.0),
            "transport": "websocket_l3",
            "best_bid": 100.04,
            "best_ask": 100.06,
        },
    }

    row = MarketLeadFeatureEngine(config).build(
        asset="BTC",
        analysis=_analysis(),
        canonical=_Canonical(public={}),
        now=100.0,
        official_index={"index_px": 100.0},
        kalshi=_kalshi(),
        live_sources={"sources": sources},
    )

    assert row["evidence_status"] == "PARTIAL"
    assert row["rti_proxy_source_count"] == 1


def test_live_source_bridge_reads_bnb_from_running_singletons(tmp_path, monkeypatch):
    coinbase = CoinbaseAdvancedL2Collector(
        products=["BNB-USD"], db_path=str(tmp_path / "coinbase.sqlite3")
    )
    coinbase._connected = True
    coinbase.handle_message(json.dumps({
        "channel": "l2_data",
        "sequence_num": 7,
        "events": [{
            "type": "snapshot",
            "product_id": "BNB-USD",
            "updates": [
                {"side": "bid", "price_level": "574.0", "new_quantity": "4"},
                {"side": "offer", "price_level": "574.2", "new_quantity": "2"},
            ],
        }],
    }))
    kraken = KrakenL3Collector(
        symbols=["BNB/USD"], db_path=str(tmp_path / "kraken.sqlite3")
    )
    kraken._connected = True
    kraken.handle_message(json.dumps({
        "channel": "level3",
        "type": "snapshot",
        "data": [{
            "symbol": "BNB/USD",
            "checksum": 9,
            "bids": [{"order_id": "bid", "limit_price": 574.05, "order_qty": 3}],
            "asks": [{"order_id": "ask", "limit_price": 574.25, "order_qty": 2}],
        }],
    }))
    monkeypatch.setattr(coinbase_adv_l2, "_feed", coinbase)
    monkeypatch.setattr(kraken_l3, "_feed", kraken)

    result = live_market_sources(
        "BNB", now=max(coinbase._book_ts["BNB-USD"], kraken._book_ts["BNB/USD"])
    )

    assert set(result["sources"]) == {"coinbase", "kraken"}
    assert result["sources"]["coinbase"]["transport"] == "websocket_l2"
    assert result["sources"]["kraken"]["transport"] == "websocket_l3"
    assert result["sources"]["coinbase"]["book"]["microprice_bps"] is not None
    assert result["diagnostics"]["kraken"]["status"] == "ok"


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
    assert report["evaluation_kind"] == "PROSPECTIVE_IMMUTABLE_FIXED_BLOCK_AUDIT"
    assert report["promotion_eligible"] is False
    assert report["retrospective_diagnostic"]["promotion_eligible"] is False
    assert report["historical_candidate_metrics_promotion_eligible"] is False
    assert report["coverage"]["observations"] == 1
    assert report["candidate_overall"]["resolved"] == 0
    assert report["legacy_joint_alignment_overall"]["resolved"] == 1


def test_prospective_audit_collects_atomically_while_notifications_are_off(tmp_path):
    runner = MarketLeadRunner(
        _config(
            str(tmp_path / "marketlead.sqlite3"),
            v3_notify_enabled=False,
            kalshi_stale_seconds=3.0,
        ),
        microstructure_provider=lambda ticker, now=None: _lagging_kalshi(),
        index_provider=lambda asset, spot, now: {"index_px": 100.0},
        market_source_provider=lambda asset, now: {},
    )
    runner.observe(
        analyses={"BTC": _analysis()},
        canonicals={
            "BTC": _Canonical(public=_public(100.0, (100.2, 100.3, 100.1)))
        },
        now=100.0,
    )

    decisions = runner.ledger.audit_decision_rows(
        "marketlead-prospective-audit-v2"
    )
    assert len(decisions) == 1
    assert decisions[0]["qualified"] == 1
    assert json.loads(decisions[0]["reason_codes_json"]) == []
    assert decisions[0]["created_at"] >= runner._audit_registration["registered_at"]
    assert runner.ledger.notification_rows() == []
    audit = runner.status()["prospective_audit"]
    assert audit["prospective_only"] is True
    assert audit["backfill_allowed"] is False
    assert audit["decisions"] == 1
    assert audit["target_status"] == "COLLECTING"


def test_prospective_audit_records_rejection_reasons_without_alerting(tmp_path):
    runner = MarketLeadRunner(
        _config(
            str(tmp_path / "marketlead.sqlite3"),
            v3_notify_enabled=True,
            v3_min_proxy_distance_bps=999.0,
        ),
        microstructure_provider=lambda ticker, now=None: _lagging_kalshi(),
        index_provider=lambda asset, spot, now: {"index_px": 100.0},
        market_source_provider=lambda asset, now: {},
        notification_sender=lambda *args, **kwargs: pytest.fail(
            "rejected audit row must not alert"
        ),
    )
    runner.observe(
        analyses={"BTC": _analysis()},
        canonicals={
            "BTC": _Canonical(public=_public(100.0, (100.2, 100.3, 100.1)))
        },
        now=100.0,
    )

    decision = runner.ledger.audit_decision_rows()[0]
    assert decision["qualified"] == 0
    assert "PROXY_DISTANCE_BELOW_MIN" in json.loads(
        decision["reason_codes_json"]
    )
    report = runner.status()["prospective_audit"]
    assert report["qualified"] == 0
    assert report["reject_reason_counts"]["PROXY_DISTANCE_BELOW_MIN"] == 1


def test_new_rule_cannot_backfill_an_observation_captured_by_old_rule(tmp_path):
    db_path = str(tmp_path / "marketlead.sqlite3")
    providers = {
        "microstructure_provider": lambda ticker, now=None: _lagging_kalshi(),
        "index_provider": lambda asset, spot, now: {"index_px": 100.0},
        "market_source_provider": lambda asset, now: {},
    }
    first = MarketLeadRunner(
        _config(db_path, v3_rule_version="audit-rule-v1"), **providers
    )
    first.observe(
        analyses={"BTC": _analysis()},
        canonicals={
            "BTC": _Canonical(
                ticker="KXBTC-OLD",
                public=_public(100.0, (100.2, 100.3, 100.1)),
            )
        },
        now=100.0,
    )

    second = MarketLeadRunner(
        _config(db_path, v3_rule_version="audit-rule-v2"), **providers
    )
    second.observe(
        analyses={"BTC": _analysis()},
        canonicals={
            "BTC": _Canonical(
                ticker="KXBTC-OLD",
                public=_public(101.0, (100.2, 100.3, 100.1)),
            )
        },
        now=101.0,
    )
    assert second.ledger.audit_decision_rows("audit-rule-v2") == []

    second.observe(
        analyses={"BTC": _analysis()},
        canonicals={
            "BTC": _Canonical(
                ticker="KXBTC-NEW",
                public=_public(102.0, (100.2, 100.3, 100.1)),
            )
        },
        now=102.0,
    )
    decisions = second.ledger.audit_decision_rows("audit-rule-v2")
    assert [row["ticker"] for row in decisions] == ["KXBTC-NEW"]


def test_changed_threshold_requires_new_rule_version_and_fails_closed(tmp_path):
    db_path = str(tmp_path / "marketlead.sqlite3")
    first = MarketLeadRunner(
        _config(
            db_path,
            v3_rule_version="frozen-audit-v1",
            v3_min_proxy_distance_bps=5.0,
        )
    )
    assert first._audit_registration["valid"] is True

    deliveries = []
    changed = MarketLeadRunner(
        _config(
            db_path,
            v3_rule_version="frozen-audit-v1",
            v3_min_proxy_distance_bps=6.0,
            v3_notify_enabled=True,
        ),
        microstructure_provider=lambda ticker, now=None: _lagging_kalshi(),
        index_provider=lambda asset, spot, now: {"index_px": 100.0},
        market_source_provider=lambda asset, now: {},
        notification_sender=lambda text, **kwargs: deliveries.append(text) or {},
    )
    assert changed._audit_registration["valid"] is False
    assert changed._audit_registration["error"] == "immutable_rule_config_mismatch"
    changed.observe(
        analyses={"BTC": _analysis()},
        canonicals={
            "BTC": _Canonical(
                ticker="KXBTC-CHANGED",
                public=_public(100.0, (100.2, 100.3, 100.1)),
            )
        },
        now=100.0,
    )
    assert len(changed.ledger.rows()) == 1
    assert changed.ledger.audit_decision_rows("frozen-audit-v1") == []
    assert deliveries == []
    assert changed.status()["notifies"] is False


def test_prospective_audit_scores_correlated_assets_as_one_fixed_window(tmp_path):
    base = time.time()
    runner = MarketLeadRunner(
        _config(
            str(tmp_path / "marketlead.sqlite3"),
            v3_rule_version="fixed-block-audit-v1",
            audit_block_windows=2,
            audit_min_blocks=1,
            audit_accuracy_min=0.50,
            audit_wilson_lb_min=0.0,
            kalshi_stale_seconds=3.0,
        ),
        microstructure_provider=lambda ticker, now=None: _lagging_kalshi(),
        index_provider=lambda asset, spot, now: {"index_px": 100.0},
        market_source_provider=lambda asset, now: {},
    )
    tickers: list[tuple[str, str]] = []
    for offset in (0.0, 900.0):
        now = base + offset
        canonicals = {}
        for asset in ("BTC", "ETH"):
            ticker = f"KX{asset}-{int(offset)}"
            canonical = _Canonical(
                ticker=ticker,
                public=_public(now, (100.2, 100.3, 100.1)),
            )
            canonical.settlement_time = now + 780.0
            canonicals[asset] = canonical
            tickers.append((ticker, "NO" if offset == 0.0 and asset == "ETH" else "YES"))
        runner.observe(
            analyses={"BTC": _analysis(), "ETH": _analysis()},
            canonicals=canonicals,
            now=now,
        )
    runner.resolve_settled(
        [{"ticker": ticker, "result": result} for ticker, result in tickers],
        base + 1800.0,
    )

    report = runner.ledger.prospective_audit_report(
        "fixed-block-audit-v1",
        block_windows=99,
        min_blocks=99,
        accuracy_min=0.99,
        wilson_lb_min=0.99,
    )
    assert report["row_metrics"]["resolved"] == 4
    assert report["row_metrics"]["wins"] == 3
    assert report["window_metrics"]["resolved"] == 2
    assert report["window_metrics"]["wins"] == 1
    assert report["blocks"][0]["windows"] == 2
    assert report["blocks"][0]["complete"] is True
    assert report["requirements"]["block_windows"] == 2
    assert report["requirements"]["minimum_complete_blocks"] == 1
    assert report["requirements"]["accuracy_each_complete_block"] == 0.50
    assert report["target_status"] == "PASS"

    decision_id = runner.ledger.audit_decision_rows("fixed-block-audit-v1")[0]["id"]
    with sqlite3.connect(runner.config.db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE marketlead_audit_decisions SET qualified=0 WHERE id=?",
                (decision_id,),
            )
    with sqlite3.connect(runner.config.db_path) as connection:
        connection.execute(
            "UPDATE marketlead_observations SET config_hash='tampered' WHERE ticker=?",
            (tickers[0][0],),
        )
    assert runner.ledger.prospective_audit_report(
        "fixed-block-audit-v1"
    )["target_status"] == "INVALID"


def test_runner_queues_one_precision_v3_alert_with_durable_dedup(tmp_path):
    deliveries = []

    def sender(text, *, idempotency_key, expires_at):
        deliveries.append((text, idempotency_key, expires_at))
        return {"outbox_status": "PENDING", "delivered": False, "error": None}

    runner = MarketLeadRunner(
        _config(
            str(tmp_path / "marketlead.sqlite3"),
            v3_notify_enabled=True,
            kalshi_stale_seconds=3.0,
        ),
        microstructure_provider=lambda ticker, now=None: _lagging_kalshi(),
        index_provider=lambda asset, spot, now: {"index_px": 100.0},
        market_source_provider=lambda asset, now: {},
        notification_sender=sender,
        notification_status_provider=lambda key: "PENDING",
    )
    canonical = _Canonical(public=_public(100.0, (100.2, 100.3, 100.1)))

    runner.observe(
        analyses={"BTC": _analysis()}, canonicals={"BTC": canonical}, now=100.0
    )
    runner.observe(
        analyses={"BTC": _analysis()}, canonicals={"BTC": canonical}, now=101.0
    )

    assert len(deliveries) == 1
    text, key, expires_at = deliveries[0]
    assert key == (
        "marketlead-test:marketlead:v3:marketlead-prospective-audit-v2:"
        "BTC:2111112:780"
    )
    assert expires_at == canonical.settlement_time
    assert "V3 MARKETLEAD PROSPECTIVE AUDIT 13M" in text
    assert "marketlead-prospective-audit-v2" in text
    assert "prospective paper-only monitor; no order placed" in text
    notifications = runner.ledger.notification_rows()
    assert len(notifications) == 1
    assert notifications[0]["status"] == "QUEUED_RETRY"
    status = runner.status()
    assert status["notifies"] is True
    assert status["trades"] is False
    assert status["v3_notifications"]["counts"] == {"QUEUED_RETRY": 1}
    assert status["notification_filter"]["proxy_distance_side_bps_min"] == 5.0
    assert status["notification_filter"]["venue_impulse_side_min"] == 0.20
    assert status["notification_filter"]["kalshi_pressure_side_max"] == -0.20
    assert status["notification_guard"]["auto_muted"] is False


def test_runner_precision_gate_rejects_weak_candidate_but_keeps_data(tmp_path):
    deliveries = []
    runner = MarketLeadRunner(
        _config(
            str(tmp_path / "marketlead.sqlite3"),
            v3_notify_enabled=True,
            v3_min_proxy_distance_bps=999.0,
        ),
        microstructure_provider=lambda ticker, now=None: _lagging_kalshi(),
        index_provider=lambda asset, spot, now: {"index_px": 100.0},
        market_source_provider=lambda asset, now: {},
        notification_sender=lambda text, **kwargs: deliveries.append(text) or {},
        notification_status_provider=lambda key: None,
    )
    runner.observe(
        analyses={"BTC": _analysis()},
        canonicals={"BTC": _Canonical(public=_public(100.0, (100.2, 100.3, 100.1)))},
        now=100.0,
    )

    assert len(runner.ledger.rows()) == 1
    assert runner.ledger.rows()[0]["lead_lag_candidate"] == 1
    assert runner.ledger.notification_rows() == []
    assert deliveries == []


def test_runner_auto_mutes_precision_alerts_below_live_accuracy_bar(tmp_path):
    deliveries = []
    runner = MarketLeadRunner(
        _config(
            str(tmp_path / "marketlead.sqlite3"),
            v3_notify_enabled=True,
            v3_guard_min_resolved=8,
            v3_guard_accuracy_min=0.80,
        ),
        microstructure_provider=lambda ticker, now=None: _lagging_kalshi(),
        index_provider=lambda asset, spot, now: {"index_px": 100.0},
        market_source_provider=lambda asset, now: {},
        notification_sender=lambda text, **kwargs: deliveries.append(text) or {},
        notification_status_provider=lambda key: None,
    )
    runner.ledger.notification_performance = lambda *args, **kwargs: {
        "available": True,
        "resolved": 8,
        "wins": 6,
        "losses": 2,
        "accuracy": 0.75,
        "gross_pnl_cents": -10.0,
    }
    runner.observe(
        analyses={"BTC": _analysis()},
        canonicals={"BTC": _Canonical(public=_public(100.0, (100.2, 100.3, 100.1)))},
        now=100.0,
    )

    status = runner.status()
    assert status["notification_configured"] is True
    assert status["notifies"] is False
    assert status["notification_guard"]["auto_muted"] is True
    assert runner.ledger.notification_rows() == []
    assert deliveries == []


def test_runner_recovers_failed_v3_enqueue_after_restart(tmp_path):
    db_path = str(tmp_path / "marketlead.sqlite3")
    base = time.time()
    canonical = _Canonical(public=_public(base, (100.2, 100.3, 100.1)))
    canonical.settlement_time = base + 780.0
    config = _config(
        db_path,
        v3_notify_enabled=True,
        v3_notify_retry_seconds=0.0,
        kalshi_stale_seconds=3.0,
    )
    first = MarketLeadRunner(
        config,
        microstructure_provider=lambda ticker, now=None: _lagging_kalshi(),
        index_provider=lambda asset, spot, now: {"index_px": 100.0},
        market_source_provider=lambda asset, now: {},
        notification_sender=lambda *args, **kwargs: {
            "delivered": False,
            "error": "outbox_unavailable",
        },
        notification_status_provider=lambda key: None,
    )
    first.observe(
        analyses={"BTC": _analysis()}, canonicals={"BTC": canonical}, now=base
    )
    assert first.ledger.notification_rows()[0]["status"] == "DELIVERY_FAILED"

    recovered = []
    second = MarketLeadRunner(
        config,
        microstructure_provider=lambda ticker, now=None: _lagging_kalshi(),
        index_provider=lambda asset, spot, now: {"index_px": 100.0},
        market_source_provider=lambda asset, now: {},
        notification_sender=lambda text, **kwargs: (
            recovered.append(kwargs["idempotency_key"])
            or {"outbox_status": "PENDING", "delivered": False}
        ),
        notification_status_provider=lambda key: "PENDING",
    )
    expected_key = (
        "marketlead-test:marketlead:v3:marketlead-prospective-audit-v2:BTC:"
        f"{int(canonical.settlement_time // 900)}:780"
    )

    assert recovered == [expected_key]
    assert second.ledger.notification_rows()[0]["status"] == "QUEUED_RETRY"


def test_marketlead_card_is_monitoring_only():
    text = build_marketlead_alert({
        "asset": "BTC",
        "predicted_side": "YES",
        "ticker": "KXBTC-TEST",
        "entry_ask_cents": 68.0,
        "paper_limit_cents": 69.0,
        "paper_limit_touched": 1,
        "kalshi_book_age_seconds": 0.2,
        "kalshi_event_age_seconds": 0.1,
        "proxy_distance_side_bps": 4.2,
        "venue_impulse_side": 0.4,
        "rti_proxy_source_count": 2,
        "venue_source_count": 2,
        "kalshi_pressure_side": -0.3,
        "features_json": json.dumps({
            "candidate": {"kalshi_pressure_side_max": -0.10}
        }),
    })

    assert "PAPER WATCH YES" in text
    assert "no order placed" in text
    assert "BUY" not in text


def test_runner_exposes_live_source_freshness_status(tmp_path):
    def market_sources(asset, now):
        assert asset == "BTC"
        return {
            "sources": {
                "coinbase": {
                    **_source(100.0, now),
                    "transport": "websocket_l2",
                    "best_bid": 99.99,
                    "best_ask": 100.01,
                    "spread_bps": 2.0,
                },
                "kraken": {
                    **_source(100.05, now - 0.2),
                    "transport": "websocket_l3",
                    "best_bid": 100.04,
                    "best_ask": 100.06,
                    "spread_bps": 2.0,
                },
            },
            "diagnostics": {"coinbase": {"status": "ok"}, "kraken": {"status": "ok"}},
        }

    runner = MarketLeadRunner(
        _config(
            str(tmp_path / "marketlead.sqlite3"),
            require_live_proxy_sources=True,
            source_stale_seconds=3.0,
            sync_tolerance_seconds=2.0,
        ),
        microstructure_provider=lambda ticker, now=None: _kalshi(),
        index_provider=lambda asset, spot, now: {"index_px": 100.0},
        market_source_provider=market_sources,
    )
    runner.observe(
        analyses={"BTC": _analysis()},
        canonicals={"BTC": _Canonical(public={})},
        now=100.0,
    )

    status = runner.status()
    assert status["source_requirements"]["stale_seconds"] == 3.0
    assert (
        status["source_status"]["BTC"]["sources"]["coinbase"]["age_seconds"]
        == 0.0
    )
    assert status["source_status"]["BTC"]["sources"]["kraken"][
        "age_seconds"
    ] == pytest.approx(0.2)
    assert status["source_status"]["BTC"]["proxy_ready"] is True
    assert status["source_status"]["BTC"]["timestamp_spread_seconds"] == pytest.approx(0.2)
    assert runner.ledger.rows()[0]["evidence_status"] == "READY"


def test_runner_health_refreshes_default_sources_outside_capture_band(
    tmp_path, monkeypatch
):
    def market_sources(asset, now):
        return {
            "sources": {
                "coinbase": {
                    **_source(100.0, now),
                    "transport": "websocket_l2",
                    "best_bid": 99.99,
                    "best_ask": 100.01,
                }
            },
            "diagnostics": {"coinbase": {"status": "ok", "asset": asset}},
        }

    monkeypatch.setattr(
        MarketLeadRunner, "_default_market_sources", staticmethod(market_sources)
    )
    runner = MarketLeadRunner(_config(str(tmp_path / "marketlead.sqlite3")))

    status = runner.status()

    assert set(status["source_status"]) == {
        "BTC",
        "ETH",
        "SOL",
        "XRP",
        "DOGE",
        "BNB",
        "HYPE",
    }
    assert status["source_status"]["HYPE"]["sources"]["coinbase"][
        "transport"
    ] == "websocket_l2"


def test_runner_uses_post_read_wall_time_for_production_mark(tmp_path, monkeypatch):
    base = 1_900_000_000.0

    def market_sources(asset, now):
        return {
            "sources": {
                "coinbase": {
                    **_source(100.0, now - 0.1),
                    "transport": "websocket_l2",
                    "best_bid": 99.99,
                    "best_ask": 100.01,
                },
                "kraken": {
                    **_source(100.05, now - 0.2),
                    "transport": "websocket_l3",
                    "best_bid": 100.04,
                    "best_ask": 100.06,
                },
            }
        }

    monkeypatch.setattr(
        MarketLeadRunner, "_default_market_sources", staticmethod(market_sources)
    )
    monkeypatch.setattr(marketlead_runner_module.time, "time", lambda: base + 2.5)
    canonical = _Canonical(seconds=782.5, public={})
    canonical.settlement_time = base + 782.5
    runner = MarketLeadRunner(
        _config(
            str(tmp_path / "marketlead.sqlite3"),
            require_live_proxy_sources=True,
            source_stale_seconds=3.0,
            sync_tolerance_seconds=2.0,
        ),
        microstructure_provider=lambda ticker, now=None: _kalshi(),
        index_provider=lambda asset, spot, now: {"index_px": 100.0},
    )

    runner.observe(
        analyses={"BTC": _analysis()},
        canonicals={"BTC": canonical},
        now=base,
    )

    row = runner.ledger.rows()[0]
    assert row["observed_at"] == base + 2.5
    assert row["seconds_remaining"] == pytest.approx(780.0)
    assert row["evidence_status"] == "READY"


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
