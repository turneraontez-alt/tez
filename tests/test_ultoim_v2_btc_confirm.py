"""Tests for the Ultoim V2 cross-asset BTC-confirmation gate (now ON by default).

The BTC-confirmation gate suppresses paper DELIVERY (never measurement) unless BTC's
contemporaneous calibrated P(YES) at the checkpoint DECISIVELY agrees with the
candidate's side: a NO needs btc_yes_prob <= 0.5 - margin, a YES needs >= 0.5 + margin.
The mushy middle (BTC pinned near its strike) and the disagreeing side are blocked.
It FAILS OPEN when btc_yes_prob is None (no cross-asset context), and it is the lever
that makes the YES harvest safe (no_only=False admits YES only on a decisive BTC YES).

Deterministic: pure ``gate.evaluate`` calls, no network / clock / sleep. The base
candidate PASSES the three base gates (selected_probability=0.62, entry_ask_cents=55,
total_cost_cents=2 -> net edge 5.0). Every cfg PINS require_inverse_edge=False so the only
gate under test is the BTC-confirmation one (the live default ENFORCES inverse-edge, which
would otherwise block this positive-edge base candidate). Runs at the 10M mark so the
15M-only distance gate and the NO-only expensive-NO admit never interfere. ``research_fired``
stays True throughout — the recap keeps measuring blocked candidates.
"""
from __future__ import annotations

from q15_upgrade.ultoim_v2 import gate
from q15_upgrade.ultoim_v2.config import UltoimV2Config


def _cfg(**over):
    """Config under test: pin require_inverse_edge OFF so BTC-confirm is the only variable."""
    return UltoimV2Config(enabled=True, require_inverse_edge=False, **over)


def _cand(side="NO", **over):
    cand = {
        "predicted_side": side,
        "selected_probability": 0.62,
        "entry_ask_cents": 55,
        "total_cost_cents": 2,
    }
    cand.update(over)
    return cand


# 1. DEFAULT ON: the gate is enabled by default (the 86.2% live config).
def test_btc_confirm_is_default_on():
    assert UltoimV2Config().btc_confirm_enabled is True
    assert UltoimV2Config().btc_confirm_margin == 0.15


# 2. Explicitly OFF -> byte-identical: BTC's lean is ignored, the candidate fires.
def test_btc_confirm_explicit_off_ignores_btc_lean():
    cfg = _cfg(btc_confirm_enabled=False)
    v = gate.evaluate(_cand(), cfg, interval="10M", btc_yes_prob=0.95)
    assert v["fired"] is True
    assert "BTC_UNCONFIRMED" not in v["reason_codes"]


# 3. NO + BTC decisively NO (cy <= 0.5 - margin) -> fires.
def test_btc_confirm_no_with_decisive_btc_no_fires():
    cfg = _cfg(btc_confirm_enabled=True)
    v = gate.evaluate(_cand("NO"), cfg, interval="10M", btc_yes_prob=0.30)
    assert v["fired"] is True
    assert "BTC_UNCONFIRMED" not in v["reason_codes"]


# 4. NO + BTC MUSHY (pinned near its strike) -> delivery suppressed.
def test_btc_confirm_no_with_mushy_btc_blocks_delivery_only():
    cfg = _cfg(btc_confirm_enabled=True)
    v = gate.evaluate(_cand("NO"), cfg, interval="10M", btc_yes_prob=0.45)
    assert v["fired"] is False
    assert v["research_fired"] is True               # measurement keeps accruing
    assert "BTC_UNCONFIRMED" in v["reason_codes"]


# 5. NO + BTC decisively YES (disagrees) -> delivery suppressed.
def test_btc_confirm_no_with_disagreeing_btc_blocks():
    cfg = _cfg(btc_confirm_enabled=True)
    v = gate.evaluate(_cand("NO"), cfg, interval="10M", btc_yes_prob=0.70)
    assert v["fired"] is False
    assert v["research_fired"] is True
    assert "BTC_UNCONFIRMED" in v["reason_codes"]


# 6. YES harvest: no_only off + BTC decisively YES -> the YES delivers.
def test_btc_confirm_yes_harvest_with_decisive_btc_yes_fires():
    cfg = _cfg(btc_confirm_enabled=True, no_only=False)
    v = gate.evaluate(_cand("YES"), cfg, interval="10M", btc_yes_prob=0.72)
    assert v["fired"] is True
    assert "BTC_UNCONFIRMED" not in v["reason_codes"]
    assert "WRONG_SIDE_YES" not in v["reason_codes"]


# 7. YES harvest, BTC MUSHY -> the raw-YES coin-flip is suppressed.
def test_btc_confirm_yes_with_mushy_btc_blocks():
    cfg = _cfg(btc_confirm_enabled=True, no_only=False)
    v = gate.evaluate(_cand("YES"), cfg, interval="10M", btc_yes_prob=0.55)
    assert v["fired"] is False
    assert v["research_fired"] is True
    assert "BTC_UNCONFIRMED" in v["reason_codes"]


# 8. Enabled but no BTC context (None) -> FAIL OPEN.
def test_btc_confirm_fail_open_on_missing_btc():
    cfg = _cfg(btc_confirm_enabled=True)
    v = gate.evaluate(_cand("NO"), cfg, interval="10M", btc_yes_prob=None)
    assert v["fired"] is True
    assert "BTC_UNCONFIRMED" not in v["reason_codes"]


# 9. Boundaries: cy exactly at 0.5 -/+ margin counts as confirmed (inclusive).
def test_btc_confirm_boundaries_are_inclusive():
    cfg = _cfg(btc_confirm_enabled=True, no_only=False)
    assert cfg.btc_confirm_margin == 0.15
    v_no = gate.evaluate(_cand("NO"), cfg, interval="10M", btc_yes_prob=0.35)
    assert v_no["fired"] is True
    assert "BTC_UNCONFIRMED" not in v_no["reason_codes"]
    v_yes = gate.evaluate(_cand("YES"), cfg, interval="10M", btc_yes_prob=0.65)
    assert v_yes["fired"] is True
    assert "BTC_UNCONFIRMED" not in v_yes["reason_codes"]


# 10. The decisiveness margin is configurable.
def test_btc_confirm_margin_is_configurable():
    cfg = _cfg(btc_confirm_enabled=True, btc_confirm_margin=0.05)
    v = gate.evaluate(_cand("NO"), cfg, interval="10M", btc_yes_prob=0.40)
    assert v["fired"] is True
    assert "BTC_UNCONFIRMED" not in v["reason_codes"]
