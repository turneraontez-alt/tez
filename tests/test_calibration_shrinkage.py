"""Isotonic calibration must not let one settled contract dictate live confidence.

Raw PAVA on binary labels routinely ends in a block of a SINGLE observation whose
mean is exactly 0.0 or 1.0. ``_isotonic_predict`` clamps beyond the outermost
anchor, so that one block governs every raw probability past it — turning a raw
0.58 into a 0.99 on the strength of one contract.
"""
from __future__ import annotations

from q15_upgrade.ledger_v95 import _isotonic_fit, _isotonic_predict


def test_single_observation_tail_no_longer_pins_predictions_to_certainty():
    # A balanced body plus one lone YES at the top — the classic PAVA tail.
    pairs = [(0.20, 0.0), (0.30, 0.0), (0.40, 1.0), (0.45, 0.0),
             (0.50, 1.0), (0.55, 0.0), (0.60, 1.0)]

    anchors = _isotonic_fit(pairs)
    top = _isotonic_predict(anchors, 0.95)

    assert top < 0.95, "a one-sample tail still drives the map to certainty"
    assert top > 0.5, "shrinkage should not erase the signal entirely"


def test_raw_pava_behaviour_is_recoverable():
    """prior_weight=0 restores the exact previous behaviour, for A/B and replay."""
    pairs = [(0.20, 0.0), (0.30, 0.0), (0.60, 1.0)]

    anchors = _isotonic_fit(pairs, prior_weight=0.0)

    assert _isotonic_predict(anchors, 0.95) == 1.0


def test_large_blocks_are_essentially_unshrunk():
    """With real data the prior is negligible — this must not blunt a fitted map."""
    pairs = [(0.10, 0.0)] * 200 + [(0.90, 1.0)] * 200

    anchors = _isotonic_fit(pairs)

    assert _isotonic_predict(anchors, 0.90) > 0.95
    assert _isotonic_predict(anchors, 0.10) < 0.05


def test_map_stays_monotonic_after_shrinkage():
    """Unequal block counts can otherwise let shrinkage cross two adjacent blocks."""
    pairs = ([(0.10, 0.0)] * 50 + [(0.20, 1.0)] + [(0.30, 0.0)] * 3
             + [(0.40, 1.0)] * 40 + [(0.80, 1.0)])

    anchors = _isotonic_fit(pairs)

    ys = [y for _, y in anchors]
    assert ys == sorted(ys), "calibration map is not non-decreasing"
    probes = [i / 100.0 for i in range(0, 101)]
    mapped = [_isotonic_predict(anchors, p) for p in probes]
    assert mapped == sorted(mapped), "predicted curve is not non-decreasing"


def test_all_predictions_stay_in_range():
    pairs = [(0.05, 0.0), (0.5, 1.0), (0.95, 1.0)]

    anchors = _isotonic_fit(pairs)

    for p in (0.0, 0.01, 0.5, 0.99, 1.0):
        y = _isotonic_predict(anchors, p)
        assert 0.0 <= y <= 1.0


def test_empty_input_still_returns_empty():
    assert _isotonic_fit([]) == []
    assert _isotonic_predict([], 0.5) is None
