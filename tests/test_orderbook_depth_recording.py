from q15_upgrade.orderbook import parse_orderbook
from q15_upgrade.runtime import attach_orderbook_levels
from q15_upgrade.checkpoint_v95 import _spot_depth_quote_fields


def test_attach_orderbook_levels_bridges_depth_aliases():
    parsed = parse_orderbook({
        "yes": [[0.60, 12]],
        "no": [[0.35, 7]],
    })

    snap = attach_orderbook_levels({}, parsed)

    assert snap["yes_bid_qty"] == 12
    assert snap["yes_ask_qty"] == 7
    assert snap["no_ask_qty"] == 12
    assert snap["yes_ask_size"] == 7
    assert snap["yes_ask_depth_contracts"] == 7
    assert snap["no_ask_depth_contracts"] == 12


def test_spot_depth_context_is_flattened_for_market_comparison():
    fields = _spot_depth_quote_fields({
        "spot_depth": {
            "created_at": 995.0,
            "source": "OKX HYPE-USDT",
            "book_age_seconds": 2.0,
            "trade_age_seconds": 3.0,
            "best_bid": 40.0,
            "best_ask": 40.1,
            "mid": 40.05,
            "spread_bps": 24.9,
            "bid_depth_top": 10.0,
            "ask_depth_top": 8.0,
            "bid_depth_levels": 15.0,
            "ask_depth_levels": 20.0,
            "bid_notional_levels": 601.0,
            "ask_notional_levels": 802.0,
            "depth_imbalance": -0.142857,
            "trade_buy_qty_5s": 0.5,
            "trade_sell_qty_5s": 0.1,
            "trade_net_qty_5s": 0.4,
            "trade_buy_notional_5s": 20.0,
            "trade_sell_notional_5s": 4.0,
            "trade_buy_qty_15s": 1.0,
            "trade_sell_qty_15s": 3.0,
            "trade_net_qty_15s": -2.0,
            "trade_buy_notional_15s": 40.0,
            "trade_sell_notional_15s": 120.0,
            "trade_buy_qty_60s": 2.0,
            "trade_sell_qty_60s": 7.0,
            "trade_net_qty_60s": -5.0,
            "trade_buy_notional_60s": 80.0,
            "trade_sell_notional_60s": 280.0,
            "last_trade_price": 40.2,
            "last_trade_side": "sell",
            "last_trade_size": 0.25,
        }
    }, observed_at=1000.0)

    assert fields["spot_depth_source"] == "OKX HYPE-USDT"
    assert fields["spot_depth_status"] == "ok"
    assert fields["spot_depth_missing_reason"] is None
    assert fields["spot_depth_age_seconds"] == 7.0
    assert fields["spot_depth_trade_age_seconds"] == 8.0
    assert fields["spot_depth_imbalance"] == -0.142857
    assert fields["spot_depth_trade_net_qty_5s"] == 0.4
    assert fields["spot_depth_trade_net_notional_5s"] == 16.0
    assert fields["spot_depth_trade_net_qty_15s"] == -2.0
    assert fields["spot_depth_trade_net_notional_15s"] == -80.0
    assert fields["spot_depth_trade_buy_qty_60s"] == 2.0
    assert fields["spot_depth_trade_sell_qty_60s"] == 7.0
    assert fields["spot_depth_trade_net_qty_60s"] == -5.0
    assert fields["spot_depth_trade_net_notional_60s"] == -200.0
    assert fields["spot_depth_last_trade_side"] == "sell"
    assert fields["spot_depth_last_trade_size"] == 0.25


def test_spot_depth_context_records_missing_status():
    fields = _spot_depth_quote_fields({
        "spot_depth_status": "missing",
        "spot_depth_missing_reason": "no_spot_book_yet",
    }, observed_at=1000.0)

    assert fields["spot_depth_status"] == "missing"
    assert fields["spot_depth_missing_reason"] == "no_spot_book_yet"
