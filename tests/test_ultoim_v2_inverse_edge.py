"""Tests for the Ultoim V2 DEFAULT-OFF inverse-edge DELIVERY gate.

For these binaries the model's stated net edge is INVERSE near the strike: positive-edge
(cheap, near-strike) entries are coin-flips that lose, while negative-edge (expensive,
market-confident) entries win. The inverse-edge gate suppresses paper DELIVERY (never
measurement) on any candidate whose stated net edge is >= 0. It is distinct from the
expensive-NO admit (which ADMITS the > ask_hi band) -- this REMOVES the positive-edge tail.

Deterministic: pure ``gate.evaluate`` calls, no network / clock / sleep. ``research_fired``
must stay True throughout — the recap keeps measuring blocked candidates.
"""
from __future__ import annotations

from q15_upgrade.ultoim_v2 import gate
from q15_upgrade.ultoim_v2.config import UltoimV2Config


def _cand(**over):
    """A clean PASSING NO candidate; net edge = 0.62*100 - 55 - 2 = 5.0 (positive)."""
    cand = {
        "predicted_side": "NO",
        "selected_probability": 0.62,
        "entry_ask_cents": 55,
        "total_cost_cents": 2,
    }
    cand.update(over)
    return cand


# --------------------------------------------------------------------------- #
# 1. DEFAULT OFF -> byte-identical: a positive-edge NO still fires.
# --------------------------------------------------------------------------- #
def test_inverse_edge_explicit_off_positive_edge_fires():
    cfg = UltoimV2Config(enabled=True, require_inverse_edge=False)
    assert cfg.require_inverse_edge is False
    v = gate.evaluate(_cand(), cfg, interval="10M")
    assert v["fired"] is True
    assert "POSITIVE_EDGE_BLOCK" not in v["reason_codes"]


# --------------------------------------------------------------------------- #
# 2. Enabled, POSITIVE edge -> delivery suppressed (measurement unchanged).
# --------------------------------------------------------------------------- #
def test_inverse_edge_blocks_positive_edge_delivery_only():
    cfg = UltoimV2Config(enabled=True, require_inverse_edge=True)
    v = gate.evaluate(_cand(), cfg, interval="10M")
    assert v["net_edge_cents"] == 5.0
    assert v["fired"] is False
    assert v["research_fired"] is True               # measurement keeps accruing
    assert "POSITIVE_EDGE_BLOCK" in v["reason_codes"]


# --------------------------------------------------------------------------- #
# 3. Enabled, NEGATIVE edge (the expensive-NO winners) -> fires.
#    ask 75 is in (ask_hi 72, expensive_no_ask_hi 78], so the expensive-NO admit
#    waives gate_c; net edge = 62 - 75 - 2 = -15 (< 0) -> NOT blocked.
# --------------------------------------------------------------------------- #
def test_inverse_edge_passes_negative_edge():
    cfg = UltoimV2Config(enabled=True, require_inverse_edge=True)
    v = gate.evaluate(_cand(entry_ask_cents=75), cfg, interval="10M")
    assert v["expensive_no"] is True
    assert v["net_edge_cents"] < 0
    assert v["fired"] is True
    assert "POSITIVE_EDGE_BLOCK" not in v["reason_codes"]


# --------------------------------------------------------------------------- #
# 4. A barely-positive edge is still non-negative -> blocked. no_edge_waive passes
#    gate_c so the ONLY variable under test is the inverse-edge gate. (An exactly-zero
#    edge is unreachable in float and immaterial to the strategy, so the test asserts
#    behaviour on a small clearly-positive edge: net edge = 0.60*100 - 57 - 2 = 1.0.)
# --------------------------------------------------------------------------- #
def test_inverse_edge_blocks_small_positive_edge():
    cfg = UltoimV2Config(enabled=True, require_inverse_edge=True, no_edge_waive=True)
    v = gate.evaluate(_cand(selected_probability=0.60, entry_ask_cents=57), cfg, interval="10M")
    assert v["net_edge_cents"] > 0
    assert v["fired"] is False
    assert v["research_fired"] is True
    assert "POSITIVE_EDGE_BLOCK" in v["reason_codes"]


# --------------------------------------------------------------------------- #
# 5. Missing data short-circuits before the inverse-edge gate (no false block).
# --------------------------------------------------------------------------- #
def test_inverse_edge_missing_data_short_circuits():
    cfg = UltoimV2Config(enabled=True, require_inverse_edge=True)
    v = gate.evaluate(_cand(entry_ask_cents=None), cfg, interval="10M")
    assert v["fired"] is False
    assert v["reason_codes"] == ["MISSING_DATA"]
    assert "POSITIVE_EDGE_BLOCK" not in v["reason_codes"]
