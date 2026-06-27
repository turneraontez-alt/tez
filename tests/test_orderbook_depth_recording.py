from q15_upgrade.orderbook import parse_orderbook
from q15_upgrade.runtime import attach_orderbook_levels


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
