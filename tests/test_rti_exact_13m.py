from __future__ import annotations

import time

import pytest

from q15_upgrade.rti_exact_13m import ExactRTI13MSampler
from q15_upgrade.rti_path_13m import (
    RTI_POINT_IN_TIME_RISK_POLICY_VERSION,
    classify_rti_point_in_time_risk,
)
from q15_upgrade.strategy_bots.rules import (
    RTI_EXACT_MICROSTRUCTURE_SCHEMA_VERSION,
    rti_path_13m_rule_version,
)


class _Feed:
    def __init__(self):
        self.calls = []

    def get_microstructure(self, ticker, *, now, max_book_age):
        self.calls.append((ticker, now, max_book_age))
        return {
            "available": True,
            "reason": None,
            "book_age_seconds": 0.2,
            "microstructure_time_basis": "local_received_at",
            "microstructure_extension_schema_version": (
                "rti-exact-microstructure-extension-v1"
            ),
            "history_count_capped": False,
            "book_event_retention_seconds": 90.0,
            "trade_retention_seconds": 1200.0,
            "book_history_started_at": now - 120.0,
            "trade_history_started_at": now - 120.0,
            "book_history_seconds": 120.0,
            "trade_history_seconds": 120.0,
            **{
                f"{kind}_window_complete_{horizon}s": True
                for kind in ("book", "trade", "microstructure")
                for horizon in (5, 15, 30, 60)
            },
            "yes_bid_cents": 59.0,
            "yes_ask_cents": 60.0,
            "no_bid_cents": 40.0,
            "no_ask_cents": 41.0,
            "yes_bid_qty": 22.0,
            "yes_ask_qty": 31.0,
            "yes_microprice_cents": 59.4,
            "yes_microprice_edge_cents": -0.1,
            "event_count_5s": 3,
            "trade_count_5s": 1,
            "book_delta_pressure_yes_5s": -0.25,
            "trade_imbalance_yes_5s": -1.0,
            "taker_yes_volume_5s": 0.0,
            "taker_no_volume_5s": 4.0,
            "taker_net_yes_volume_5s": -4.0,
            "book_add_volume_yes_5s": 2.0,
            "book_remove_volume_yes_5s": 5.0,
            "book_add_volume_no_5s": 1.0,
            "book_remove_volume_no_5s": 0.0,
            "microprice_change_cents_5s": -0.2,
            "microprice_range_cents_5s": 0.4,
            "microprice_variation_cents_5s": 0.7,
            "microprice_trend_efficiency_5s": 2.0 / 7.0,
            "trade_yes_price_change_cents_5s": -1.0,
            "trade_yes_price_range_cents_5s": 2.0,
            "trade_yes_price_variation_cents_5s": 3.0,
            "trade_yes_price_trend_efficiency_5s": 1.0 / 3.0,
            "trade_yes_vwap_cents_5s": 58.5,
            "yes_best_depletion_5s": 5.0,
            "no_best_depletion_5s": 1.0,
            "yes_best_refill_5s": 2.0,
            "no_best_refill_5s": 0.0,
            "event_count_15s": 8,
            "trade_count_15s": 2,
            "book_delta_pressure_yes_15s": 0.2,
            "trade_imbalance_yes_15s": 0.5,
            "taker_yes_volume_15s": 6.0,
            "taker_no_volume_15s": 2.0,
            "taker_net_yes_volume_15s": 4.0,
            "event_count_30s": 12,
            "trade_count_30s": 3,
            "book_delta_pressure_yes_30s": 0.1,
            "trade_imbalance_yes_30s": 0.25,
            "taker_yes_volume_30s": 10.0,
            "taker_no_volume_30s": 6.0,
            "taker_net_yes_volume_30s": 4.0,
            "yes_best_depletion_30s": 5.0,
            "no_best_depletion_30s": 1.0,
            "yes_best_refill_30s": 2.0,
            "no_best_refill_30s": 0.0,
            "event_count_60s": 20,
            "trade_count_60s": 5,
            "book_delta_pressure_yes_60s": 0.05,
            "trade_imbalance_yes_60s": 0.2,
            "taker_yes_volume_60s": 12.0,
            "taker_no_volume_60s": 8.0,
            "taker_net_yes_volume_60s": 4.0,
            "yes_best_depletion_60s": 7.0,
            "no_best_depletion_60s": 2.0,
            "yes_best_refill_60s": 3.0,
            "no_best_refill_60s": 1.0,
        }


def _path(*, complete: bool):
    rows = [{"index_px": 2001.0 + i / 10.0} for i in range(61 if complete else 60)]
    return {
        "status": "ok" if complete else "missing",
        "missing_reason": None if complete else "settlement_index_path_incomplete",
        "index_id": "ETHUSD_RTI",
        "expected_count": 61,
        "count": len(rows),
        "complete": complete,
        "missing_seconds": [] if complete else [1020],
        "max_receive_age_s": 0.1,
        "decision_age_s": 0.3,
        "rows": rows,
    }


def _confirmation_path(*, complete: bool, seconds: int = 30):
    count = seconds + 1 if complete else seconds
    rows = [
        {"index_px": 2007.0 + i / 10.0}
        for i in range(count)
    ]
    return {
        "status": "ok" if complete else "missing",
        "missing_reason": None if complete else "settlement_index_path_incomplete",
        "index_id": "ETHUSD_RTI",
        "expected_count": seconds + 1,
        "count": len(rows),
        "complete": complete,
        "missing_seconds": [] if complete else [1050],
        "max_receive_age_s": 0.1,
        "decision_age_s": 0.3,
        "rows": rows,
    }


def test_point_in_time_risk_taxonomy_is_diagnostic_and_fails_unknown():
    high = classify_rti_point_in_time_risk({
        "rti_path_strike_crossings": 4,
        "rti_path_acceleration_bps": -0.2,
        "rti_path_seconds_since_last_crossing": 5.0,
        "rti_path_persistence": 0.7,
        "rti_signed_distance_bps": 0.4,
        "rti_distance_to_remaining_volatility": 0.03,
        "entry_ask_cents": 49.0,
    })
    assert high["rti_risk_policy_version"] == (
        RTI_POINT_IN_TIME_RISK_POLICY_VERSION
    )
    assert high["rti_reversal_risk_class"] == "high"
    assert high["rti_settlement_average_risk_class"] == "high"
    assert high["rti_path_regime_class"] == "choppy"
    assert high["rti_market_agreement_class"] == "disagrees_under_50"
    assert high["rti_risk_notification_eligible"] is False
    assert high["rti_risk_historical_credit_allowed"] is False

    low = classify_rti_point_in_time_risk({
        "rti_path_strike_crossings": 0,
        "rti_path_acceleration_bps": 0.2,
        "rti_path_persistence": 0.98,
        "rti_signed_distance_bps": 2.0,
        "rti_distance_to_remaining_volatility": 0.2,
        "entry_ask_cents": 58.0,
    })
    assert low["rti_reversal_risk_class"] == "low"
    assert low["rti_settlement_average_risk_class"] == "low"
    assert low["rti_path_regime_class"] == "persistent"
    assert low["rti_market_agreement_class"] == "confirms_55_plus"

    unknown = classify_rti_point_in_time_risk({})
    assert unknown["rti_reversal_risk_class"] == "unknown"
    assert unknown["rti_settlement_average_risk_class"] == "unknown"
    assert unknown["rti_path_regime_class"] == "unknown"
    assert unknown["rti_market_agreement_class"] == "unknown"


def test_exact_sampler_freezes_quote_once_then_waits_for_official_end_second():
    feed = _Feed()
    path_calls = []
    recorded = []

    def read_path(asset, **kwargs):
        path_calls.append((asset, kwargs))
        return _path(complete=kwargs["now"] >= 1020.3)

    def record(row):
        recorded.append(dict(row))
        return 77

    sampler = ExactRTI13MSampler(
        enabled=True,
        poll_seconds=0.05,
        max_timing_offset_s=2.0,
        feed=feed,
        path_reader=read_path,
        spot_reader=lambda asset, **kwargs: {
            "created_at": 1018.0,
            "book_age_seconds": 0.1,
            "trade_age_seconds": 0.2,
            "source": "coinbase ETH-USD",
            "depth_imbalance": 0.4,
            "trade_buy_notional_15s": 1200.0,
            "trade_sell_notional_15s": 700.0,
            "spot_mid_path_schema_version": "spot-mid-path-local-v1",
            "spot_mid_path_time_basis": "local_created_at",
            "spot_mid_path_captured_at": 1018.0,
            "spot_mid_history_started_at": 900.0,
            "spot_mid_history_seconds": 118.0,
            "spot_mid_history_retention_seconds": 180.0,
            "spot_mid_record_interval_seconds": 5.0,
            "spot_mid_window_complete_60s": True,
            "spot_mid_path_start_at_60s": 958.0,
            "spot_mid_path_end_at_60s": 1018.0,
            "spot_mid_path_max_gap_seconds_60s": 5.0,
            "spot_mid_start_60s": 2000.0,
            "spot_mid_end_60s": 2008.0,
        },
        recorder=record,
    )
    assert sampler.register_market(
        asset="ETH",
        ticker="KXETH-EXACT",
        close_time=1800.0,
        strike=2000.0,
        now=1000.0,
    )

    sampler.tick(1019.9)
    assert feed.calls == []
    sampler.tick(1020.0)
    assert len(feed.calls) == 1
    assert recorded == []
    sampler.tick(1020.3)
    sampler.tick(1020.4)

    assert len(feed.calls) == 1
    assert len(path_calls) == 2
    assert len(recorded) == 1
    row = recorded[0]
    assert row["capture_mode"] == "kalshi_ws_exact_13m"
    assert row["model_version"] == rti_path_13m_rule_version("ETH")
    assert row["rti_timing_offset_s"] == 0.0
    assert abs(row["rti_path_evaluation_delay_s"] - 0.3) < 1e-9
    assert abs(row["rti_storage_delay_s"] - 0.3) < 1e-9
    assert row["quote_age_seconds"] == 0.2
    assert row["quote_age_source"] == "kalshi_ws_exact_sampler"
    assert row["rti_side"] == "YES"
    assert row["entry_ask_cents"] == 60.0
    assert row["spread_cents"] == 1.0
    assert row["depth_contracts"] == 31.0
    assert row["rti_market_mid_probability"] == 0.595
    assert row["rti_opposite_side"] == "NO"
    assert row["rti_opposite_ask_cents"] == 41.0
    assert row["rti_opposite_depth_contracts"] == 22.0
    assert row["kalshi_yes_microprice_cents"] == 59.4
    assert row["kalshi_yes_microprice_edge_cents"] == -0.1
    assert row["kalshi_microstructure_schema_version"] == (
        RTI_EXACT_MICROSTRUCTURE_SCHEMA_VERSION
    )
    assert row["kalshi_microstructure_time_basis"] == "local_received_at"
    assert row["kalshi_history_count_capped"] is False
    assert row["kalshi_microstructure_extension_schema_version"] == (
        "rti-exact-microstructure-extension-v1"
    )
    assert row["kalshi_book_history_seconds"] == 120.0
    assert row["kalshi_trade_history_seconds"] == 120.0
    assert row["kalshi_book_window_complete_30s"] is True
    assert row["kalshi_trade_window_complete_60s"] is True
    assert row["kalshi_microstructure_window_complete_60s"] is True
    assert row["kalshi_book_add_volume_yes_5s"] == 2.0
    assert row["kalshi_book_remove_volume_yes_5s"] == 5.0
    assert row["kalshi_microprice_change_cents_5s"] == -0.2
    assert row["kalshi_microprice_variation_cents_5s"] == 0.7
    assert row["kalshi_trade_yes_price_change_cents_5s"] == -1.0
    assert row["kalshi_trade_yes_vwap_cents_5s"] == 58.5
    assert row["kalshi_event_count_5s"] == 3.0
    assert row["kalshi_book_delta_pressure_yes_5s"] == -0.25
    assert row["kalshi_trade_imbalance_yes_15s"] == 0.5
    assert row["kalshi_taker_yes_volume_15s"] == 6.0
    assert row["kalshi_taker_no_volume_15s"] == 2.0
    assert row["kalshi_taker_net_yes_volume_15s"] == 4.0
    assert row["kalshi_yes_best_depletion_5s"] == 5.0
    assert row["rti_path_complete"] is True
    assert row["spot_depth_status"] == "ok"
    assert abs(row["rti_spot_snapshot_age_s"] - 2.0) < 1e-9
    assert row["rti_spot_book_age_s"] == 0.1
    assert row["spot_depth_imbalance"] == 0.4
    assert row["spot_depth_trade_net_notional_15s"] == 500.0
    assert row["spot_mid_window_complete_60s"] is True
    assert row["rti_spot_lead_lag_status"] == "ok"
    assert row["rti_spot_basis_bps"] == pytest.approx(
        (2008.0 / 2007.0 - 1.0) * 10_000.0
    )
    assert row["rti_spot_basis_start_60s_bps"] == pytest.approx(
        (2000.0 / 2001.0 - 1.0) * 10_000.0
    )
    assert row["rti_spot_minus_index_momentum_bps_60s"] == pytest.approx(
        (2008.0 / 2000.0 - 1.0) * 10_000.0
        - (2007.0 / 2001.0 - 1.0) * 10_000.0
    )
    assert row["rti_signed_distance_bps"] > 0.0
    assert row["rti_path_trend_efficiency"] == 1.0
    assert row["rti_path_second_half_side_move_bps"] > 0.0
    assert row["rti_path_strike_crossings"] == 0
    assert row["rti_risk_policy_version"] == (
        RTI_POINT_IN_TIME_RISK_POLICY_VERSION
    )
    assert row["rti_reversal_risk_class"] in {"low", "medium", "high"}
    assert row["rti_settlement_average_risk_class"] in {
        "low", "medium", "high"
    }
    assert row["rti_risk_notification_eligible"] is False
    assert path_calls[0][1]["start_ts"] == 960.0
    assert path_calls[0][1]["end_ts"] == 1020.0
    health = sampler.health()
    assert health["quote_captures"] == 1
    assert health["decisions_recorded"] == 1
    assert health["missed_deadlines"] == 0
    assert health["spot_context_ok"] == 1
    assert health["spot_context_missing"] == 0
    assert health["cross_asset_ok"] == 0
    assert health["cross_asset_missing"] == 1
    path_source = health["independent_path_source"]
    assert path_source["paper_only"] is True
    assert path_source["outcome_labels_read"] is False
    assert path_source["model_fit_performed"] is False
    assert path_source["notification_eligible"] is False
    assert path_source["automatic_promotion"] is False
    assert path_source["real_trading_allowed"] is False
    assert health["spot_confirm_challenger"]["notification_eligible"] is False


def test_rti_spot_lead_lag_fails_closed_on_timestamp_contradiction():
    features = {"rti_path_start_px": 100.0, "rti_path_end_px": 101.0}
    spot = {
        "spot_depth_status": "ok",
        "spot_mid_path_schema_version": "spot-mid-path-local-v1",
        "spot_mid_path_time_basis": "local_created_at",
        "spot_mid_window_complete_60s": True,
        "spot_mid_path_captured_at": 1000.0,
        "rti_spot_evidence_as_of": 1000.1,
        "spot_mid_history_started_at": 900.0,
        "spot_mid_history_seconds": 100.0,
        "spot_mid_history_retention_seconds": 180.0,
        "spot_mid_record_interval_seconds": 5.0,
        "spot_mid_path_start_at_60s": 940.0,
        "spot_mid_path_end_at_60s": 999.0,
        "spot_mid_path_max_gap_seconds_60s": 5.0,
        "spot_mid_start_60s": 100.2,
        "spot_mid_end_60s": 101.3,
    }
    result = ExactRTI13MSampler._rti_spot_lead_lag(features, spot)
    assert result["rti_spot_lead_lag_status"] == "missing"
    assert result["rti_spot_basis_bps"] is None
    assert "SPOT_PATH_WINDOW_END_CONTRADICTION" in (
        result["rti_spot_lead_lag_missing_reason"]
    )


def test_exact_sampler_fails_closed_when_first_wake_is_after_deadline():
    recorded = []
    sampler = ExactRTI13MSampler(
        enabled=True,
        max_timing_offset_s=2.0,
        feed=_Feed(),
        path_reader=lambda *args, **kwargs: _path(complete=True),
        spot_reader=lambda *args, **kwargs: None,
        recorder=lambda row: recorded.append(dict(row)) or 1,
    )
    sampler.register_market(
        asset="BTC",
        ticker="KXBTC-MISSED",
        close_time=1800.0,
        strike=64000.0,
        now=1000.0,
    )
    sampler.tick(1022.01)
    sampler.tick(1022.2)

    assert recorded == []
    health = sampler.health()
    assert health["missed_deadlines"] == 1
    assert health["decisions_recorded"] == 0
    assert health["last_error"] == "exact_quote_capture_deadline_missed"
    assert health["recent_missed_tickers"] == ["KXBTC-MISSED"]


def test_exact_sampler_retries_a_stale_book_and_freezes_the_fresh_quote_time():
    class StaleThenFresh(_Feed):
        def get_microstructure(self, ticker, *, now, max_book_age):
            if not self.calls:
                self.calls.append((ticker, now, max_book_age))
                return {
                    "available": False,
                    "reason": "book_stale",
                    "book_age_seconds": 2.4,
                }
            return super().get_microstructure(
                ticker, now=now, max_book_age=max_book_age
            )

    feed = StaleThenFresh()
    recorded = []
    sampler = ExactRTI13MSampler(
        enabled=True,
        poll_seconds=0.05,
        max_timing_offset_s=2.0,
        feed=feed,
        path_reader=lambda *args, **kwargs: _path(complete=True),
        spot_reader=lambda *args, **kwargs: None,
        recorder=lambda row: recorded.append(dict(row)) or 1,
    )
    sampler.register_market(
        asset="ETH",
        ticker="KXETH-RETRY",
        close_time=1800.0,
        strike=2000.0,
        now=1000.0,
    )
    sampler.tick(1020.0)
    assert recorded == []
    assert sampler.health()["quote_retry_pending_tickers"] == ["KXETH-RETRY"]

    sampler.tick(1020.2)
    assert len(recorded) == 1
    assert recorded[0]["entry_ask_cents"] == 60.0
    assert recorded[0]["rti_timing_offset_s"] == pytest.approx(0.2)
    health = sampler.health()
    assert health["quote_retry_attempts"] == 1
    assert health["quote_retry_successes"] == 1
    assert health["quote_retry_exhausted"] == 0
    assert health["missed_deadlines"] == 0


def test_exact_sampler_persists_missing_quote_before_deadline_when_retries_exhaust():
    class AlwaysStale(_Feed):
        def get_microstructure(self, ticker, *, now, max_book_age):
            self.calls.append((ticker, now, max_book_age))
            return {
                "available": False,
                "reason": "book_stale",
                "book_age_seconds": 3.0,
            }

    feed = AlwaysStale()
    recorded = []
    sampler = ExactRTI13MSampler(
        enabled=True,
        poll_seconds=0.05,
        max_timing_offset_s=2.0,
        feed=feed,
        path_reader=lambda *args, **kwargs: _path(complete=True),
        spot_reader=lambda *args, **kwargs: None,
        recorder=lambda row: recorded.append(dict(row)) or 1,
    )
    sampler.register_market(
        asset="ETH",
        ticker="KXETH-RETRY-EXHAUST",
        close_time=1800.0,
        strike=2000.0,
        now=1000.0,
    )
    sampler.tick(1020.0)
    sampler.tick(1021.0)
    sampler.tick(1021.91)
    assert len(recorded) == 1
    assert recorded[0]["entry_ask_cents"] is None
    assert recorded[0]["kalshi_depth_status"] == "missing"
    assert recorded[0]["kalshi_depth_missing_reason"] == "book_stale"
    assert recorded[0]["rti_timing_offset_s"] == pytest.approx(1.91)
    health = sampler.health()
    assert health["quote_retry_attempts"] == 2
    assert health["quote_retry_successes"] == 0
    assert health["quote_retry_exhausted"] == 1
    assert health["missed_deadlines"] == 0
    assert health["recent_retry_exhausted_tickers"] == [
        "KXETH-RETRY-EXHAUST"
    ]
    assert health["last_quote_failure_reason_by_ticker"] == {
        "KXETH-RETRY-EXHAUST": "book_stale"
    }


def test_exact_sampler_freezes_all_due_assets_before_any_persistence():
    events = []

    class OrderedFeed(_Feed):
        def get_microstructure(self, ticker, *, now, max_book_age):
            events.append(("quote", ticker))
            return super().get_microstructure(
                ticker, now=now, max_book_age=max_book_age
            )

    def read_path(asset, **kwargs):
        events.append(("path", asset))
        return _path(complete=True)

    def record(row):
        events.append(("record", row["asset"]))
        return len(events)

    sampler = ExactRTI13MSampler(
        enabled=True,
        feed=OrderedFeed(),
        path_reader=read_path,
        spot_reader=lambda *args, **kwargs: None,
        recorder=record,
    )
    for asset in ("BTC", "ETH"):
        assert sampler.register_market(
            asset=asset,
            ticker=f"KX{asset}-BATCH",
            close_time=1800.0,
            strike=2000.0,
            now=1000.0,
        )

    sampler.tick(1020.0)

    first_record = next(i for i, event in enumerate(events) if event[0] == "record")
    assert events[:2] == [
        ("quote", "KXBTC-BATCH"),
        ("quote", "KXETH-BATCH"),
    ]
    assert all(event[0] in {"quote", "path"} for event in events[:first_record])
    assert [event for event in events if event[0] == "record"] == [
        ("record", "BTC"),
        ("record", "ETH"),
    ]


def test_exact_sampler_freezes_all_quotes_before_any_spot_capture():
    events = []

    class OrderedFeed(_Feed):
        def get_microstructure(self, ticker, *, now, max_book_age):
            events.append(("quote", ticker))
            return super().get_microstructure(
                ticker, now=now, max_book_age=max_book_age
            )

    def spot(asset, **kwargs):
        events.append(("spot", asset))
        return None

    sampler = ExactRTI13MSampler(
        enabled=True,
        feed=OrderedFeed(),
        path_reader=lambda *args, **kwargs: _path(complete=True),
        spot_reader=spot,
        recorder=lambda row: 1 if row["asset"] == "BTC" else 2,
        confirmation_recorder=lambda row: 3,
    )
    for asset in ("BTC", "ETH"):
        sampler.register_market(
            asset=asset,
            ticker=f"KX{asset}-QUOTE-FIRST",
            close_time=1800.0,
            strike=2000.0,
            now=1000.0,
        )
    sampler.tick(1020.0)
    assert events[:4] == [
        ("quote", "KXBTC-QUOTE-FIRST"),
        ("quote", "KXETH-QUOTE-FIRST"),
        ("spot", "BTC"),
        ("spot", "ETH"),
    ]


def test_realtime_sampler_drains_all_quote_retries_before_spot_or_path_work():
    events = []

    class OneStaleBook(_Feed):
        def __init__(self):
            super().__init__()
            self.attempts = {}

        def get_microstructure(self, ticker, *, now, max_book_age):
            events.append(("quote", ticker))
            self.attempts[ticker] = self.attempts.get(ticker, 0) + 1
            if ticker == "KXETH-DRAIN" and self.attempts[ticker] == 1:
                self.calls.append((ticker, now, max_book_age))
                return {
                    "available": False,
                    "reason": "book_stale",
                    "book_age_seconds": 2.2,
                }
            return super().get_microstructure(
                ticker, now=now, max_book_age=max_book_age
            )

    def spot(asset, **kwargs):
        events.append(("spot", asset))
        return None

    def path(asset, **kwargs):
        events.append(("path", asset))
        return _path(complete=True)

    recorded = []
    feed = OneStaleBook()
    sampler = ExactRTI13MSampler(
        enabled=True,
        poll_seconds=0.01,
        max_timing_offset_s=2.0,
        feed=feed,
        path_reader=path,
        spot_reader=spot,
        recorder=lambda row: recorded.append(dict(row)) or len(recorded),
    )
    decision_time = time.time() - 0.01
    for asset in ("BTC", "ETH"):
        sampler.register_market(
            asset=asset,
            ticker=f"KX{asset}-DRAIN",
            close_time=decision_time + 780.0,
            strike=2000.0,
            now=decision_time - 60.0,
        )

    sampler.tick()

    first_downstream = next(
        index for index, event in enumerate(events)
        if event[0] in {"spot", "path"}
    )
    assert events[:first_downstream] == [
        ("quote", "KXBTC-DRAIN"),
        ("quote", "KXETH-DRAIN"),
        ("quote", "KXETH-DRAIN"),
    ]
    assert len(recorded) == 2
    health = sampler.health()
    assert health["quote_captures"] == 2
    assert health["quote_retry_attempts"] == 1
    assert health["quote_retry_successes"] == 1
    assert health["quote_retry_exhausted"] == 0
    assert health["quote_retry_drain_cycles"] >= 1
    assert health["missed_deadlines"] == 0
    assert health["recent_missed_tickers"] == []
    assert health["last_quote_failure_reason_by_ticker"] == {}


def test_exact_sampler_delayed_confirmation_uses_fresh_quote_and_path():
    class RepricingFeed(_Feed):
        def get_microstructure(self, ticker, *, now, max_book_age):
            quote = super().get_microstructure(
                ticker, now=now, max_book_age=max_book_age
            )
            if len(self.calls) >= 2:
                quote.update({
                    "yes_bid_cents": 56.0,
                    "yes_ask_cents": 57.0,
                    "no_bid_cents": 43.0,
                    "no_ask_cents": 44.0,
                })
            if len(self.calls) >= 3:
                quote.update({
                    "yes_bid_cents": 54.0,
                    "yes_ask_cents": 55.0,
                    "no_bid_cents": 45.0,
                    "no_ask_cents": 46.0,
                })
            if len(self.calls) >= 4:
                quote.update({
                    "yes_bid_cents": 52.0,
                    "yes_ask_cents": 53.0,
                    "no_bid_cents": 47.0,
                    "no_ask_cents": 48.0,
                })
            return quote

    feed = RepricingFeed()
    base_rows = []
    confirmation_rows = []
    spot_calls = []

    def read_path(asset, **kwargs):
        if kwargs["end_ts"] == 1020.0:
            return _path(complete=True)
        assert kwargs["start_ts"] == 1020.0
        if kwargs["end_ts"] == 1050.0:
            return _confirmation_path(complete=True)
        if kwargs["end_ts"] == 1080.0:
            return _confirmation_path(complete=True, seconds=60)
        assert kwargs["end_ts"] == 1110.0
        return _confirmation_path(complete=True, seconds=90)

    def read_spot(asset, **kwargs):
        spot_calls.append(asset)
        return {
            "created_at": (1020.0, 1050.0, 1080.0, 1110.0)[
                len(spot_calls) - 1
            ],
            "book_age_seconds": 0.1,
            "source": "coinbase ETH-USD",
            "depth_imbalance": 0.2,
        }

    sampler = ExactRTI13MSampler(
        enabled=True,
        feed=feed,
        path_reader=read_path,
        spot_reader=read_spot,
        recorder=lambda row: base_rows.append(dict(row)) or 77,
        confirmation_recorder=(
            lambda row: confirmation_rows.append(dict(row)) or 88
        ),
    )
    sampler.register_market(
        asset="ETH",
        ticker="KXETH-DELAYED",
        close_time=1800.0,
        strike=2000.0,
        now=1000.0,
    )

    sampler.tick(1020.0)
    sampler.tick(1049.9)
    assert len(base_rows) == 1
    assert confirmation_rows == []
    assert len(feed.calls) == 1

    sampler.tick(1050.0)
    assert len(feed.calls) == 2
    assert len(confirmation_rows) == 1
    row = confirmation_rows[0]
    assert row["interval"] == "12M30S"
    assert row["capture_mode"] == "kalshi_ws_delayed_confirm_30s"
    assert row["entry_ask_cents"] == 57.0
    assert row["entry_ask_cents"] != base_rows[0]["entry_ask_cents"]
    assert row["rti_confirm_original_row_id"] == 77
    assert row["rti_confirm_original_strict_accepted"] is True
    assert row["rti_confirm_timing_offset_s"] == 0.0
    assert row["rti_confirm_evaluation_delay_s"] == 0.0
    assert row["rti_confirm_path_expected_count"] == 31
    assert row["rti_confirm_path_count"] == 31
    assert row["rti_confirm_path_complete"] is True
    assert row["rti_confirm_side"] == "YES"
    assert sampler.health()["delayed_confirmation"]["pending_tickers"] == [
        "KXETH-DELAYED@+60s", "KXETH-DELAYED@+90s"
    ]

    sampler.tick(1080.0)
    assert len(feed.calls) == 3
    assert len(confirmation_rows) == 2
    row_60 = confirmation_rows[1]
    assert row_60["interval"] == "12M"
    assert row_60["capture_mode"] == "kalshi_ws_delayed_confirm_60s"
    assert row_60["entry_ask_cents"] == 55.0
    assert row_60["rti_confirm_delay_seconds"] == 60.0
    assert row_60["rti_confirm_path_expected_count"] == 61
    assert row_60["rti_confirm_path_count"] == 61
    assert row_60["rti_confirm_original_row_id"] == 77
    assert sampler.health()["delayed_confirmation"]["pending_tickers"] == [
        "KXETH-DELAYED@+90s"
    ]

    sampler.tick(1110.0)
    assert len(feed.calls) == 4
    assert len(confirmation_rows) == 3
    row_90 = confirmation_rows[2]
    assert row_90["interval"] == "11M30S"
    assert row_90["capture_mode"] == "kalshi_ws_delayed_stability_90s"
    assert row_90["entry_ask_cents"] == 53.0
    assert row_90["rti_confirm_delay_seconds"] == 90.0
    assert row_90["rti_confirm_path_expected_count"] == 91
    assert row_90["rti_confirm_path_count"] == 91
    assert row_90["rti_confirm_original_row_id"] == 77
    health = sampler.health()["delayed_confirmation"]
    assert health["quote_captures"] == 3
    assert health["decisions_recorded"] == 3
    assert health["record_failures"] == 0
    assert health["pending_tickers"] == []
    assert health["notification_eligible"] is False
    assert [policy["delay_seconds"] for policy in health["policies"]] == [
        30.0, 60.0, 90.0
    ]


def test_delayed_confirmation_retries_stale_quote_then_freezes_fresh_timestamp():
    class DelayedStaleThenFresh(_Feed):
        def get_microstructure(self, ticker, *, now, max_book_age):
            if len(self.calls) == 1:
                self.calls.append((ticker, now, max_book_age))
                return {
                    "available": False,
                    "reason": "book_stale",
                    "book_age_seconds": 3.2,
                }
            return super().get_microstructure(
                ticker, now=now, max_book_age=max_book_age
            )

    feed = DelayedStaleThenFresh()
    confirmation_rows = []

    def read_path(asset, **kwargs):
        if kwargs["end_ts"] == 1020.0:
            return _path(complete=True)
        return _confirmation_path(complete=True)

    sampler = ExactRTI13MSampler(
        enabled=True,
        feed=feed,
        path_reader=read_path,
        spot_reader=lambda *args, **kwargs: None,
        recorder=lambda row: 77,
        confirmation_recorder=(
            lambda row: confirmation_rows.append(dict(row)) or 88
        ),
    )
    sampler.register_market(
        asset="ETH", ticker="KXETH-DELAYED-RETRY",
        close_time=1800.0, strike=2000.0, now=1000.0,
    )
    sampler.tick(1020.0)
    sampler.tick(1050.0)
    assert confirmation_rows == []
    health = sampler.health()["delayed_confirmation"]
    assert health["quote_retry_attempts"] == 1
    assert health["quote_retry_pending_tickers"] == [
        "KXETH-DELAYED-RETRY@rti_delayed_confirm_30s_v1"
    ]

    sampler.tick(1050.2)
    assert len(confirmation_rows) == 1
    assert confirmation_rows[0]["entry_ask_cents"] == 60.0
    assert confirmation_rows[0]["rti_confirm_timing_offset_s"] == pytest.approx(0.2)
    health = sampler.health()["delayed_confirmation"]
    assert health["quote_retry_attempts"] == 1
    assert health["quote_retry_successes"] == 1
    assert health["quote_retry_exhausted"] == 0
    assert health["quote_retry_pending_tickers"] == []
    assert health["missed_deadlines"] == 0


def test_delayed_confirmation_persists_missing_quote_when_retries_exhaust():
    class DelayedAlwaysStale(_Feed):
        def get_microstructure(self, ticker, *, now, max_book_age):
            if not self.calls:
                return super().get_microstructure(
                    ticker, now=now, max_book_age=max_book_age
                )
            self.calls.append((ticker, now, max_book_age))
            return {
                "available": False,
                "reason": "book_stale",
                "book_age_seconds": 3.5,
            }

    feed = DelayedAlwaysStale()
    confirmation_rows = []

    def read_path(asset, **kwargs):
        if kwargs["end_ts"] == 1020.0:
            return _path(complete=True)
        return _confirmation_path(complete=True)

    sampler = ExactRTI13MSampler(
        enabled=True,
        feed=feed,
        path_reader=read_path,
        spot_reader=lambda *args, **kwargs: None,
        recorder=lambda row: 77,
        confirmation_recorder=(
            lambda row: confirmation_rows.append(dict(row)) or 88
        ),
    )
    sampler.register_market(
        asset="ETH", ticker="KXETH-DELAYED-EXHAUST",
        close_time=1800.0, strike=2000.0, now=1000.0,
    )
    sampler.tick(1020.0)
    sampler.tick(1050.0)
    sampler.tick(1051.0)
    sampler.tick(1051.91)
    assert len(confirmation_rows) == 1
    assert confirmation_rows[0]["entry_ask_cents"] is None
    assert confirmation_rows[0]["kalshi_depth_status"] == "missing"
    assert confirmation_rows[0]["kalshi_depth_missing_reason"] == "book_stale"
    assert confirmation_rows[0]["rti_confirm_timing_offset_s"] == pytest.approx(1.91)
    health = sampler.health()["delayed_confirmation"]
    assert health["quote_retry_attempts"] == 2
    assert health["quote_retry_successes"] == 0
    assert health["quote_retry_exhausted"] == 1
    assert health["quote_retry_pending_tickers"] == []
    assert health["missed_deadlines"] == 0


def test_exact_sampler_recovers_only_missing_delayed_stage_after_restart():
    feed = _Feed()
    confirmation_rows = []
    recovery_calls = []

    def recover(**kwargs):
        recovery_calls.append(dict(kwargs))
        return {
            "parent_row_id": 77,
            "parent_strict_accepted": True,
            "completed_intervals": ["12M30S", "11M30S"],
            "original_source": {
                "model_version": rti_path_13m_rule_version("ETH"),
                "rti_side": "YES",
                "rti_path_end_px": 2007.0,
            },
        }

    sampler = ExactRTI13MSampler(
        enabled=True,
        feed=feed,
        path_reader=lambda *args, **kwargs: _confirmation_path(
            complete=True, seconds=60
        ),
        spot_reader=lambda *args, **kwargs: {
            "created_at": 1080.0,
            "book_age_seconds": 0.1,
            "depth_imbalance": 0.2,
        },
        recorder=lambda row: pytest.fail("exact parent must not replay"),
        confirmation_recorder=(
            lambda row: confirmation_rows.append(dict(row)) or 88
        ),
        confirmation_recovery_reader=recover,
    )
    assert sampler.register_market(
        asset="ETH",
        ticker="KXETH-RECOVER",
        close_time=1800.0,
        strike=2000.0,
        now=1055.0,
    )
    assert recovery_calls == [{
        "ticker": "KXETH-RECOVER",
        "close_time": 1800.0,
    }]
    health = sampler.health()
    assert health["missed_deadlines"] == 0
    assert health["delayed_confirmation"]["recovered_parents"] == 1
    assert health["delayed_confirmation"]["recovered_stages"] == 1
    assert health["delayed_confirmation"]["pending_tickers"] == [
        "KXETH-RECOVER@+60s"
    ]

    # Re-registration is idempotent and cannot recreate completed +30s work.
    sampler.register_market(
        asset="ETH",
        ticker="KXETH-RECOVER",
        close_time=1800.0,
        strike=2000.0,
        now=1056.0,
    )
    assert len(recovery_calls) == 1
    sampler.tick(1080.0)
    assert len(feed.calls) == 1
    assert len(confirmation_rows) == 1
    recovered = confirmation_rows[0]
    assert recovered["interval"] == "12M"
    assert recovered["rti_confirm_original_row_id"] == 77
    assert recovered["rti_confirm_timing_offset_s"] == 0.0
    assert sampler.health()["delayed_confirmation"]["pending_tickers"] == []


def test_restart_recovery_finishes_before_market_is_visible_to_exact_worker():
    sampler = None

    def recover(**kwargs):
        # Simulate the worker running concurrently at the worst possible point.
        assert sampler is not None
        sampler.tick(1055.0)
        return {
            "parent_row_id": 77,
            "parent_strict_accepted": True,
            "completed_intervals": ["12M30S", "12M", "11M30S"],
            "original_source": {
                "model_version": rti_path_13m_rule_version("ETH"),
                "rti_side": "YES",
                "rti_path_end_px": 2007.0,
            },
        }

    sampler = ExactRTI13MSampler(
        enabled=True,
        feed=_Feed(),
        path_reader=lambda *args, **kwargs: pytest.fail(
            "durable exact parent must not be sampled again"
        ),
        recorder=lambda row: pytest.fail("durable exact parent must not replay"),
        confirmation_recovery_reader=recover,
    )
    assert sampler.register_market(
        asset="ETH",
        ticker="KXETH-RECOVERY-RACE",
        close_time=1800.0,
        strike=2000.0,
        now=1055.0,
    )
    sampler.tick(1055.1)
    health = sampler.health()
    assert health["missed_deadlines"] == 0
    assert health["decisions_recorded"] == 0
    assert health["delayed_confirmation"]["recovered_parents"] == 1


def test_exact_sampler_rejects_spot_snapshot_created_after_decision():
    recorded = []
    sampler = ExactRTI13MSampler(
        enabled=True,
        feed=_Feed(),
        path_reader=lambda *args, **kwargs: _path(complete=True),
        spot_reader=lambda *args, **kwargs: {
            "created_at": 1020.1,
            "book_age_seconds": 0.0,
            "depth_imbalance": 0.9,
        },
        recorder=lambda row: recorded.append(dict(row)) or 1,
    )
    sampler.register_market(
        asset="ETH",
        ticker="KXETH-FUTURE-SPOT",
        close_time=1800.0,
        strike=2000.0,
        now=1000.0,
    )
    sampler.tick(1020.0)

    assert len(recorded) == 1
    row = recorded[0]
    assert row["spot_depth_status"] == "missing"
    assert row["spot_depth_missing_reason"] == "spot_depth_snapshot_after_decision"
    assert row["spot_depth_imbalance"] is None
    assert sampler.health()["spot_context_missing"] == 1


def test_exact_sampler_freezes_independent_opposite_side_quote_and_depth():
    quote = ExactRTI13MSampler._side_quote({
        "available": True,
        "book_age_seconds": 0.1,
        "yes_bid_cents": 59.0,
        "yes_ask_cents": 60.0,
        "no_bid_cents": 40.0,
        "no_ask_cents": 41.0,
        "yes_bid_qty": 22.0,
        "yes_ask_qty": 31.0,
    }, "NO")
    assert quote["entry_ask_cents"] == 41.0
    assert quote["depth_contracts"] == 22.0
    assert quote["rti_opposite_side"] == "YES"
    assert quote["rti_opposite_ask_cents"] == 60.0
    assert quote["rti_opposite_depth_contracts"] == 31.0
