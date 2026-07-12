from __future__ import annotations

import json
import os
import tempfile

import pytest

import coinbase_adv_l2
import kraken_l3
import q15_upgrade.marketlead.runner as marketlead_runner_module
from coinbase_adv_l2 import CoinbaseAdvancedL2Collector
from kraken_l3 import KrakenL3Collector
from q15_upgrade.marketlead.config import MarketLeadConfig
from q15_upgrade.marketlead.features import MarketLeadFeatureEngine
from q15_upgrade.marketlead.live_sources import live_market_sources
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
    assert report["coverage"]["observations"] == 1
    assert report["candidate_overall"]["resolved"] == 1


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
