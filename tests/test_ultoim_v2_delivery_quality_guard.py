"""Tests for the Ultoim V2 empirical delivery-quality guard.

The 2026-07-01 local ledger review found delivered V2 losses clustered in three slices:
HYPE/SOL assets, ask<60c entries (39.4% acc / -1,047c settled, n=66), and spread>=3c
(-407c). ``delivery_quality_guard_enabled`` suppresses DELIVERY (``fired``) and the
yes_notify surface for those slices ONLY -- ``research_fired`` is UNCHANGED so blocked
slices keep grading and can earn their way back on fresh settled data.

Per the repo's default-OFF standard for model-behavior changes, the guard defaults OFF in
code; the live Repl opts in via Q15_ULTOIM_V2_DELIVERY_QUALITY_GUARD=true in ``.replit``.

Deterministic: pure ``gate.evaluate`` calls. BTC-confirm and inverse-edge are pinned OFF so
the ONLY variable under test is the guard; the base NO candidate passes the base gates.
"""
from __future__ import annotations

from q15_upgrade.ultoim_v2 import gate
from q15_upgrade.ultoim_v2.config import UltoimV2Config


def _cfg(**over):
    base = dict(enabled=True, btc_confirm_enabled=False, require_inverse_edge=False)
    base.update(over)
    return UltoimV2Config(**base)


def _no(**over):
    # prob 0.80 @ ask 72 clears every BASE gate (edge/conf/ask band) so the only
    # variable under test is the guard itself.
    cand = {
        "predicted_side": "NO",
        "asset": "BTC",
        "selected_probability": 0.80,
        "entry_ask_cents": 72,
        "spread_cents": 1.0,
        "total_cost_cents": 2,
    }
    cand.update(over)
    return cand


# 1. DEFAULT OFF (repo standard: model-behavior changes ship default-OFF).
def test_guard_default_off():
    assert UltoimV2Config().delivery_quality_guard_enabled is False


# 2. OFF -> byte-identical: a cheap-ask HYPE wide-spread NO still fires as before.
def test_guard_off_is_byte_identical():
    cand = _no(asset="HYPE", entry_ask_cents=55, spread_cents=4.0)
    v = gate.evaluate(cand, _cfg(delivery_quality_guard_enabled=False), interval="10M")
    assert v["fired"] is True
    for code in ("DELIVERY_ASSET_BLOCK", "DELIVERY_ASK_BELOW_PROVEN_MIN",
                 "DELIVERY_SPREAD_TOO_WIDE"):
        assert code not in v["reason_codes"]


# 3. ON: blocked asset (HYPE/SOL) is suppressed from DELIVERY but still RESEARCHES.
def test_guard_blocks_hype_and_sol_delivery_only():
    cfg = _cfg(delivery_quality_guard_enabled=True)
    for asset in ("HYPE", "SOL"):
        v = gate.evaluate(_no(asset=asset), cfg, interval="10M")
        assert v["fired"] is False
        assert v["research_fired"] is True           # measurement keeps accruing
        assert "DELIVERY_ASSET_BLOCK" in v["reason_codes"]


# 4. ON: ask below the proven 60c floor is blocked for ANY asset.
def test_guard_blocks_cheap_ask():
    v = gate.evaluate(_no(asset="BTC", entry_ask_cents=55),
                      _cfg(delivery_quality_guard_enabled=True), interval="10M")
    assert v["fired"] is False
    assert v["research_fired"] is True
    assert "DELIVERY_ASK_BELOW_PROVEN_MIN" in v["reason_codes"]
    # boundary: exactly 60c passes the ask leg
    v = gate.evaluate(_no(asset="BTC", entry_ask_cents=60),
                      _cfg(delivery_quality_guard_enabled=True), interval="10M")
    assert "DELIVERY_ASK_BELOW_PROVEN_MIN" not in v["reason_codes"]


# 5. ON: spread >= 3c is blocked (inclusive boundary); tight spread passes.
def test_guard_blocks_wide_spread():
    cfg = _cfg(delivery_quality_guard_enabled=True)
    v = gate.evaluate(_no(spread_cents=3.0), cfg, interval="10M")
    assert v["fired"] is False
    assert "DELIVERY_SPREAD_TOO_WIDE" in v["reason_codes"]
    v = gate.evaluate(_no(spread_cents=2.9), cfg, interval="10M")
    assert v["fired"] is True
    assert "DELIVERY_SPREAD_TOO_WIDE" not in v["reason_codes"]


# 6. ON: a clean candidate (good asset, ask>=60, tight spread) fires normally.
def test_guard_on_clean_candidate_fires():
    v = gate.evaluate(_no(), _cfg(delivery_quality_guard_enabled=True), interval="10M")
    assert v["fired"] is True


# 7. ON: the guard also suppresses the paper yes_notify surface (documented scope).
def test_guard_blocks_yes_notify_too():
    cfg = _cfg(delivery_quality_guard_enabled=True, yes_notify_enabled=True,
               btc_confirm_enabled=True)
    yes = {"predicted_side": "YES", "asset": "HYPE", "selected_probability": 0.86,
           "calibrated_yes_probability": 0.85, "market_implied_yes_probability": 0.80,
           "entry_ask_cents": 86, "spread_cents": 1.0, "total_cost_cents": 2}
    v = gate.evaluate(yes, cfg, interval="7M", btc_yes_prob=0.70)
    assert v["yes_notify"] is False
    assert "DELIVERY_ASSET_BLOCK" in v["reason_codes"]


# 8. ON: missing spread fails open on the spread leg (only ask/asset can block then).
def test_guard_missing_spread_fails_open():
    v = gate.evaluate(_no(spread_cents=None), _cfg(delivery_quality_guard_enabled=True),
                      interval="10M")
    assert v["fired"] is True
    assert "DELIVERY_SPREAD_TOO_WIDE" not in v["reason_codes"]
