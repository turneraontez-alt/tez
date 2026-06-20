import math
from q15_upgrade.precision import (
    estimated_fee_cents, vwap_for_quantity, floor_to_tick, max_entry_price_cents,
)


def test_fee_zero_at_extremes():
    assert estimated_fee_cents(0, 10) == 0
    assert estimated_fee_cents(100, 10) == 0      # p*(1-p)=0 at the bounds


def test_fee_peaks_at_midpoint():
    assert estimated_fee_cents(50, 10) > estimated_fee_cents(20, 10) > 0


def test_vwap_walks_the_book():
    vwap, filled = vwap_for_quantity([[40, 5], [41, 5], [42, 100]], 10)
    assert filled == 10 and math.isclose(vwap, 40.5)


def test_vwap_partial_fill():
    vwap, filled = vwap_for_quantity([[40, 3]], 10)
    assert filled == 3 and vwap == 40


def test_vwap_empty():
    assert vwap_for_quantity(None, 10) == (None, 0.0)
    assert vwap_for_quantity([], 10) == (None, 0.0)


def test_floor_to_tick():
    assert math.isclose(floor_to_tick(40.37, 0.1), 40.3)
    assert math.isclose(floor_to_tick(40.0, 1.0), 40.0)


def test_max_entry_preserves_edge():
    fair, edge = 60.0, 5.0
    p = max_entry_price_cents(fair, edge, contracts=10)
    net = fair - p - estimated_fee_cents(p, 10) / 10
    assert net >= edge - 0.2                # within one tick of the requirement
    assert p <= fair


def test_max_entry_invalid():
    assert max_entry_price_cents(0, 5) is None
