"""Tests for the Ultoim V2 expensive-NO admit band (gate.py).

The band (DEFAULT ON, Q15_ULTOIM_V2_EXPENSIVE_NO): on a NON-15M interval, a NO
candidate with ask in (ask_hi, expensive_no_ask_hi] is admitted EVEN with a sub-min
stated edge — it lifts the ask ceiling AND waives the edge gate for that band only.
The confidence floor and ask_lo still apply, and 15M / the YES side are never touched.

Cross-ledger basis: for NO the model's stated edge is inverse (the best NO entries
carry negative edge) and that band wins ~84% over n=893 (v1+v95+v2), net-positive
after the expensive losses. Capped at 85 because the (85,100] slice is net-negative.
With the flag off behaviour is byte-identical to the plain three-gate verdict.

Deterministic: pure ``gate.evaluate`` / ``gate.display_entry`` calls — no network,
clock, or sleep.
"""
from __future__ import annotations

import pytest

from q15_upgrade.ultoim_v2 import gate
from q15_upgrade.ultoim_v2.config import UltoimV2Config


def _cfg(**over):
    # Real defaults: ask_lo=50, ask_hi=72, min_confidence=0.55, min_edge_cents=2.0,
    # expensive_no_enabled=True, expensive_no_ask_hi=85.0. Pin no_edge_waive=False AND
    # cap_7m_ask=False so these tests isolate the expensive-NO / edge-gate path (both are
    # owner-default ON; the 7M-cap-overrides-expensive behaviour has its own dedicated test).
    base = dict(enabled=True, no_edge_waive=False, cap_7m_ask=False)
    base.update(over)
    return UltoimV2Config(**base)


def _no(ask, sel, cost=0.0):
    return {"predicted_side": "NO", "selected_probability": sel,
            "entry_ask_cents": ask, "total_cost_cents": cost}


def test_admits_expensive_no_with_negative_edge():
    # ask 80, sel 0.78, cost 0 -> net edge = 78 - 80 = -2 (< min_edge 2). Without the
    # band this is double-blocked (ASK_ABOVE_BAND + EDGE_BELOW_MIN); the band admits it.
    v = gate.evaluate(_no(80.0, 0.78), _cfg(), interval="10M")
    assert v["fired"] is True
    assert v["gate_b"] is True and v["gate_c"] is True and v["expensive_no"] is True
    assert v["net_edge_cents"] == pytest.approx(-2.0)
    assert "EXPENSIVE_NO_ADMIT" in v["reason_codes"]
    assert "ASK_ABOVE_BAND" not in v["reason_codes"]
    assert "EDGE_BELOW_MIN" not in v["reason_codes"]


def test_confidence_floor_still_applies_in_band():
    # sel 0.50 < min_confidence 0.55: the band lifts the ceiling/edge but NOT the
    # confidence floor, so gate_b fails and it does not fire.
    v = gate.evaluate(_no(80.0, 0.50), _cfg(), interval="10M")
    assert v["fired"] is False
    assert "CONF_BELOW_MIN" in v["reason_codes"]
    assert v["expensive_no"] is True and v["gate_b"] is False


def test_ceiling_boundary_85_inclusive_above_blocked():
    # ask exactly 85 (the extended ceiling) is admitted; just above is blocked.
    at = gate.evaluate(_no(85.0, 0.84), _cfg(), interval="7M")  # edge 84-85 = -1
    assert at["expensive_no"] is True and at["fired"] is True
    above = gate.evaluate(_no(85.01, 0.90), _cfg(), interval="7M")
    assert above["expensive_no"] is False and above["fired"] is False
    assert "ASK_ABOVE_BAND" in above["reason_codes"]


def test_lower_boundary_72_is_normal_band_not_expensive():
    # ask exactly ask_hi (72) is the NORMAL band (ask must be strictly > ask_hi to be
    # expensive): it fires on its own edge, with no expensive flag/marker.
    v = gate.evaluate(_no(72.0, 0.80), _cfg(), interval="10M")  # edge 80-72 = 8
    assert v["fired"] is True and v["expensive_no"] is False
    assert "EXPENSIVE_NO_ADMIT" not in v["reason_codes"]


def test_15m_excluded_from_band():
    v = gate.evaluate(_no(80.0, 0.78), _cfg(), interval="15M")
    assert v["expensive_no"] is False and v["fired"] is False
    assert "ASK_ABOVE_BAND" in v["reason_codes"]


def test_yes_side_excluded_ceiling_not_lifted():
    yes = {"predicted_side": "YES", "selected_probability": 0.90,
           "entry_ask_cents": 80.0, "total_cost_cents": 0.0}
    v = gate.evaluate(yes, _cfg(), interval="10M")
    assert v["expensive_no"] is False and v["fired"] is False
    assert v["research_fired"] is False                 # ceiling NOT lifted for YES
    assert "WRONG_SIDE_YES" in v["reason_codes"]
    assert "ASK_ABOVE_BAND" in v["reason_codes"]


def test_in_band_with_passing_edge_still_flagged():
    # An expensive NO whose edge already clears the bar still needs the ceiling lift
    # (ask > 72), so it is flagged and marked — but never gets EDGE_BELOW_MIN.
    v = gate.evaluate(_no(80.0, 0.95), _cfg(), interval="10M")  # edge 95-80 = 15
    assert v["fired"] is True and v["expensive_no"] is True
    assert "EXPENSIVE_NO_ADMIT" in v["reason_codes"]
    assert "EDGE_BELOW_MIN" not in v["reason_codes"]


def test_flag_off_is_byte_identical_to_plain_ceiling():
    cand = _no(80.0, 0.78)
    off = gate.evaluate(cand, _cfg(expensive_no_enabled=False), interval="10M")
    assert off["expensive_no"] is False and off["fired"] is False
    assert "ASK_ABOVE_BAND" in off["reason_codes"]
    assert "EDGE_BELOW_MIN" in off["reason_codes"]
    assert "EXPENSIVE_NO_ADMIT" not in off["reason_codes"]
    # ...and the same candidate fires with the band on.
    assert gate.evaluate(cand, _cfg(), interval="10M")["fired"] is True


def test_display_entry_shows_ask_for_band():
    cfg = _cfg()
    # Edge-based display would clamp to <=72 (never fill at 80); the band shows the ask.
    assert gate.display_entry(0.78, 0.0, 80.0, cfg, expensive_no=True) == 80
    assert gate.display_entry(0.84, 0.0, 85.0, cfg, expensive_no=True) == 85
    # Never above the extended ceiling, never below the floor.
    assert gate.display_entry(0.99, 0.0, 90.0, cfg, expensive_no=True) == 85
    # Default path unchanged: clamps to ask_hi=72.
    assert gate.display_entry(0.99, 0.0, 80.0, cfg) == 72


def test_verdict_best_entry_uses_extended_ceiling_in_band():
    # best_entry_cents in the verdict honours the extended ceiling for the band:
    # floor(0.84*100 - 0 - 2) = 82, within [50, 85] -> 82 (would be clamped to 72 off-band).
    v = gate.evaluate(_no(80.0, 0.84), _cfg(), interval="10M")
    assert v["best_entry_cents"] == 82
    off = gate.evaluate(_no(80.0, 0.84), _cfg(expensive_no_enabled=False), interval="10M")
    assert off["best_entry_cents"] == 72


def test_distance_gate_and_expensive_band_are_mutually_exclusive():
    # 15M near-strike NO is suppressed by the distance gate; the expensive band (non-15M)
    # never applies to it. A 10M near-strike NO in the expensive band is admitted (the
    # distance gate is 15M-only).
    near15 = gate.evaluate({**_no(80.0, 0.78), "distance_sigma": 0.05}, _cfg(), interval="15M")
    assert near15["expensive_no"] is False and near15["fired"] is False
    near10 = gate.evaluate({**_no(80.0, 0.78), "distance_sigma": 0.05}, _cfg(), interval="10M")
    assert near10["expensive_no"] is True and near10["fired"] is True
    assert "NEAR_STRIKE_PIN" not in near10["reason_codes"]
