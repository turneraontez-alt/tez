"""Tests for the Ultoim V2 DEFAULT-OFF cross-asset BTC-confirmation gate.

The BTC-confirmation gate suppresses paper DELIVERY (never measurement) unless BTC's
contemporaneous calibrated P(YES) at the checkpoint DECISIVELY agrees with the
candidate's side: a NO needs btc_yes_prob <= 0.5 - margin, a YES needs >= 0.5 + margin.
The mushy middle (BTC pinned near its strike) and the disagreeing side are blocked.
It FAILS OPEN when btc_yes_prob is None (no cross-asset context), and it is the lever
that makes the YES harvest safe (pair with no_only=false to admit YES only on a
decisive BTC YES).

Deterministic: pure ``gate.evaluate`` calls, no network / clock / sleep. The base
candidate PASSES the three base gates (selected_probability=0.62, entry_ask_cents=55,
total_cost_cents=2 -> net edge 5.0 >= 2.0), and every test runs at the 10M mark so the
15M-only distance gate and the NO-only expensive-NO admit never interfere. ``research_fired``
must stay True throughout — the recap keeps measuring blocked candidates.
"""
from __future__ import annotations

from q15_upgrade.ultoim_v2 import gate
from q15_upgrade.ultoim_v2.config import UltoimV2Config


def _cand(side="NO", **over):
    cand = {
        "predicted_side": side,
        "selected_probability": 0.62,
        "entry_ask_cents": 55,
        "total_cost_cents": 2,
    }
    cand.update(over)
    return cand


# --------------------------------------------------------------------------- #
# 1. DEFAULT OFF -> byte-identical: BTC's lean is ignored, the candidate fires.
# --------------------------------------------------------------------------- #
def test_btc_confirm_default_off_ignores_btc_lean():
    cfg = UltoimV2Config(enabled=True)
    assert cfg.btc_confirm_enabled is False
    # Even a flatly-disagreeing BTC lean cannot block when the gate is off.
    v = gate.evaluate(_cand(), cfg, interval="10M", btc_yes_prob=0.95)
    assert v["fired"] is True
    assert "BTC_UNCONFIRMED" not in v["reason_codes"]


# --------------------------------------------------------------------------- #
# 2. Enabled, NO, BTC decisively NO (cy below 0.5 - margin) -> fires.
# --------------------------------------------------------------------------- #
def test_btc_confirm_no_with_decisive_btc_no_fires():
    cfg = UltoimV2Config(enabled=True, btc_confirm_enabled=True)
    v = gate.evaluate(_cand("NO"), cfg, interval="10M", btc_yes_prob=0.30)
    assert v["fired"] is True
    assert "BTC_UNCONFIRMED" not in v["reason_codes"]


# --------------------------------------------------------------------------- #
# 3. Enabled, NO, BTC MUSHY (pinned near its strike) -> delivery suppressed.
# --------------------------------------------------------------------------- #
def test_btc_confirm_no_with_mushy_btc_blocks_delivery_only():
    cfg = UltoimV2Config(enabled=True, btc_confirm_enabled=True)
    v = gate.evaluate(_cand("NO"), cfg, interval="10M", btc_yes_prob=0.45)
    assert v["fired"] is False
    assert v["research_fired"] is True               # measurement keeps accruing
    assert "BTC_UNCONFIRMED" in v["reason_codes"]


# --------------------------------------------------------------------------- #
# 4. Enabled, NO, BTC decisively YES (disagrees) -> delivery suppressed.
# --------------------------------------------------------------------------- #
def test_btc_confirm_no_with_disagreeing_btc_blocks():
    cfg = UltoimV2Config(enabled=True, btc_confirm_enabled=True)
    v = gate.evaluate(_cand("NO"), cfg, interval="10M", btc_yes_prob=0.70)
    assert v["fired"] is False
    assert v["research_fired"] is True
    assert "BTC_UNCONFIRMED" in v["reason_codes"]


# --------------------------------------------------------------------------- #
# 5. YES harvest: no_only off + BTC decisively YES -> the YES delivers.
# --------------------------------------------------------------------------- #
def test_btc_confirm_yes_harvest_with_decisive_btc_yes_fires():
    cfg = UltoimV2Config(enabled=True, btc_confirm_enabled=True, no_only=False)
    v = gate.evaluate(_cand("YES"), cfg, interval="10M", btc_yes_prob=0.72)
    assert v["fired"] is True
    assert "BTC_UNCONFIRMED" not in v["reason_codes"]
    assert "WRONG_SIDE_YES" not in v["reason_codes"]


# --------------------------------------------------------------------------- #
# 6. YES harvest, BTC MUSHY -> the raw-YES coin-flip is suppressed.
# --------------------------------------------------------------------------- #
def test_btc_confirm_yes_with_mushy_btc_blocks():
    cfg = UltoimV2Config(enabled=True, btc_confirm_enabled=True, no_only=False)
    v = gate.evaluate(_cand("YES"), cfg, interval="10M", btc_yes_prob=0.55)
    assert v["fired"] is False
    assert v["research_fired"] is True
    assert "BTC_UNCONFIRMED" in v["reason_codes"]


# --------------------------------------------------------------------------- #
# 7. Enabled but no BTC context (None) -> FAIL OPEN (cannot prove "unconfirmed").
# --------------------------------------------------------------------------- #
def test_btc_confirm_fail_open_on_missing_btc():
    cfg = UltoimV2Config(enabled=True, btc_confirm_enabled=True)
    v = gate.evaluate(_cand("NO"), cfg, interval="10M", btc_yes_prob=None)
    assert v["fired"] is True
    assert "BTC_UNCONFIRMED" not in v["reason_codes"]


# --------------------------------------------------------------------------- #
# 8. Boundaries: cy exactly at 0.5 -/+ margin counts as confirmed (inclusive).
# --------------------------------------------------------------------------- #
def test_btc_confirm_boundaries_are_inclusive():
    cfg = UltoimV2Config(enabled=True, btc_confirm_enabled=True, no_only=False)
    assert cfg.btc_confirm_margin == 0.15
    # NO at exactly 0.35 (= 0.5 - 0.15) is confirmed.
    v_no = gate.evaluate(_cand("NO"), cfg, interval="10M", btc_yes_prob=0.35)
    assert v_no["fired"] is True
    assert "BTC_UNCONFIRMED" not in v_no["reason_codes"]
    # YES at exactly 0.65 (= 0.5 + 0.15) is confirmed.
    v_yes = gate.evaluate(_cand("YES"), cfg, interval="10M", btc_yes_prob=0.65)
    assert v_yes["fired"] is True
    assert "BTC_UNCONFIRMED" not in v_yes["reason_codes"]


# --------------------------------------------------------------------------- #
# 9. The decisiveness margin is configurable.
# --------------------------------------------------------------------------- #
def test_btc_confirm_margin_is_configurable():
    cfg = UltoimV2Config(enabled=True, btc_confirm_enabled=True, btc_confirm_margin=0.05)
    # cy 0.40 is mushy at the default 0.15 margin but decisive at 0.05 (0.40 <= 0.45).
    v = gate.evaluate(_cand("NO"), cfg, interval="10M", btc_yes_prob=0.40)
    assert v["fired"] is True
    assert "BTC_UNCONFIRMED" not in v["reason_codes"]
