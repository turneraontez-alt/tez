"""Tests for the Ultoim V2 DEFAULT-OFF distance gate.

The distance gate ABSTAINS (suppresses paper DELIVERY, never measurement) on 15M
NEAR-strike NO candidates (|distance_sigma| < distance_pin_sigma), where cross-ledger
evidence shows the NO side loses. Scope is 15M-NO-only: the 10M/7M near-strike NO
buckets and the YES side are deliberately untouched.

Deterministic: pure ``gate.evaluate`` calls, no network / clock / sleep. Every case
uses realistic PASSING base values (selected_probability=0.62, entry_ask_cents=55,
total_cost_cents=2 -> net edge 5.0¢ >= 2.0¢) so gate_b/gate_c always pass and the ONLY
variable under test is the distance gate. ``research_fired`` (gate_b AND gate_c) must
stay True throughout — the recap keeps measuring these candidates.
"""
from __future__ import annotations

from q15_upgrade.ultoim_v2 import gate
from q15_upgrade.ultoim_v2.config import UltoimV2Config


def _cand(**over):
    """A clean PASSING NO candidate; gate_b/gate_c pass on the base values."""
    cand = {
        "predicted_side": "NO",
        "selected_probability": 0.62,
        "entry_ask_cents": 55,
        "total_cost_cents": 2,
    }
    cand.update(over)
    return cand


# --------------------------------------------------------------------------- #
# 1. DEFAULT-OFF: byte-identical — near-strike 15M NO still fires.
# --------------------------------------------------------------------------- #
def test_distance_gate_default_off_fires_near_strike():
    cfg = UltoimV2Config(enabled=True)  # distance_gate_enabled defaults False
    assert cfg.distance_gate_enabled is False
    v = gate.evaluate(_cand(distance_sigma=0.05), cfg, interval="15M")
    assert v["fired"] is True
    assert v["research_fired"] is True
    assert "NEAR_STRIKE_PIN" not in v["reason_codes"]


# --------------------------------------------------------------------------- #
# 2. Enabled, 15M, NO, NEAR the strike -> ABSTAIN (delivery only).
# --------------------------------------------------------------------------- #
def test_distance_gate_enabled_15m_no_near_abstains():
    cfg = UltoimV2Config(enabled=True, distance_gate_enabled=True)
    v = gate.evaluate(_cand(distance_sigma=0.05), cfg, interval="15M")
    assert v["fired"] is False
    # research_fired UNCHANGED — measurement keeps accruing.
    assert v["research_fired"] is True
    assert "NEAR_STRIKE_PIN" in v["reason_codes"]


# --------------------------------------------------------------------------- #
# 3. Enabled, 15M, NO, FAR from the strike -> fires (not a pin).
# --------------------------------------------------------------------------- #
def test_distance_gate_enabled_15m_no_far_fires():
    cfg = UltoimV2Config(enabled=True, distance_gate_enabled=True)
    v = gate.evaluate(_cand(distance_sigma=0.30), cfg, interval="15M")
    assert v["fired"] is True
    assert v["research_fired"] is True
    assert "NEAR_STRIKE_PIN" not in v["reason_codes"]


# --------------------------------------------------------------------------- #
# 4. Enabled, 10M, NO, NEAR -> fires (scope is 15M-only; 10M untouched).
# --------------------------------------------------------------------------- #
def test_distance_gate_scope_is_15m_only_10m_near_fires():
    cfg = UltoimV2Config(enabled=True, distance_gate_enabled=True)
    v = gate.evaluate(_cand(distance_sigma=0.05), cfg, interval="10M")
    assert v["fired"] is True
    assert v["research_fired"] is True
    assert "NEAR_STRIKE_PIN" not in v["reason_codes"]


# --------------------------------------------------------------------------- #
# 5. Enabled, 15M, YES, NEAR -> blocked by SIDE, not the distance gate (NO-only).
# --------------------------------------------------------------------------- #
def test_distance_gate_is_no_only_yes_near_blocked_by_side():
    cfg = UltoimV2Config(enabled=True, distance_gate_enabled=True)
    v = gate.evaluate(_cand(predicted_side="YES", distance_sigma=0.05), cfg, interval="15M")
    assert v["fired"] is False  # blocked, but because it is YES
    assert "WRONG_SIDE_YES" in v["reason_codes"]
    assert "NEAR_STRIKE_PIN" not in v["reason_codes"]
    # YES still measures (side-agnostic research verdict).
    assert v["research_fired"] is True


# --------------------------------------------------------------------------- #
# 6. Enabled, 15M, NO, distance_sigma None -> fail-open (cannot prove "near").
# --------------------------------------------------------------------------- #
def test_distance_gate_fail_open_on_missing_distance():
    cfg = UltoimV2Config(enabled=True, distance_gate_enabled=True)
    v = gate.evaluate(_cand(distance_sigma=None), cfg, interval="15M")
    assert v["fired"] is True
    assert v["research_fired"] is True
    assert "NEAR_STRIKE_PIN" not in v["reason_codes"]


# --------------------------------------------------------------------------- #
# 7. Boundary: distance_sigma == pin -> FAR (strict <), fires.
# --------------------------------------------------------------------------- #
def test_distance_gate_boundary_equal_pin_is_far():
    cfg = UltoimV2Config(enabled=True, distance_gate_enabled=True)
    assert cfg.distance_pin_sigma == 0.15
    v = gate.evaluate(_cand(distance_sigma=0.15), cfg, interval="15M")
    assert v["fired"] is True
    assert v["research_fired"] is True
    assert "NEAR_STRIKE_PIN" not in v["reason_codes"]
