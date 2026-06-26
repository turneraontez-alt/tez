"""Tests for the Ultoim V2 NO-only 7M delivery skip (``skip_7m_no_deliver``).

The faithful-C5 backtest showed 7M NO is a ~break-even coin-flip that DILUTES the real 10M NO edge,
while 7M YES stays strong. ``skip_7m_no_deliver`` suppresses DELIVERY (``fired``) of the NO side at
the 7M mark ONLY -- ``research_fired`` is UNCHANGED (the recap keeps grading 7M NO) and neither the
YES side nor any other interval is touched. A global ``skip_7m`` would kill the good YES-7M too, so
this is the surgical drop. Default OFF -> byte-identical.

Deterministic: pure ``gate.evaluate`` calls. BTC-confirm is pinned OFF and inverse-edge OFF so the
ONLY variable under test is the 7M-NO skip; the base NO candidate passes the three base gates.
"""
from __future__ import annotations

from q15_upgrade.ultoim_v2 import gate
from q15_upgrade.ultoim_v2.config import UltoimV2Config


def _cfg(**over):
    base = dict(enabled=True, btc_confirm_enabled=False, require_inverse_edge=False)
    base.update(over)
    return UltoimV2Config(**base)


def _no(**over):
    cand = {
        "predicted_side": "NO",
        "selected_probability": 0.62,
        "entry_ask_cents": 55,
        "total_cost_cents": 2,
    }
    cand.update(over)
    return cand


# 1. DEFAULT OFF.
def test_skip_7m_no_default_off():
    assert UltoimV2Config().skip_7m_no_deliver is False


# 2. ON: a NO at 7M is suppressed from DELIVERY but still RESEARCHES.
def test_skip_7m_no_suppresses_delivery_only():
    v = gate.evaluate(_no(), _cfg(skip_7m_no_deliver=True), interval="7M")
    assert v["fired"] is False
    assert v["research_fired"] is True               # measurement keeps accruing
    assert "SKIP_7M_NO" in v["reason_codes"]


# 3. ON: the NO at 10M is UNTOUCHED (the edge interval keeps firing).
def test_skip_7m_no_leaves_10m_no_untouched():
    v = gate.evaluate(_no(), _cfg(skip_7m_no_deliver=True), interval="10M")
    assert v["fired"] is True
    assert "SKIP_7M_NO" not in v["reason_codes"]


# 4. ON: a YES at 7M is UNTOUCHED by the NO-only skip (the strong YES-7M harvest survives).
def test_skip_7m_no_leaves_7m_yes_untouched():
    cfg = _cfg(skip_7m_no_deliver=True, yes_notify_enabled=True, btc_confirm_enabled=True)
    yes = {"predicted_side": "YES", "selected_probability": 0.86,
           "calibrated_yes_probability": 0.85, "market_implied_yes_probability": 0.80,
           "entry_ask_cents": 86, "total_cost_cents": 2}
    v = gate.evaluate(yes, cfg, interval="7M", btc_yes_prob=0.70)
    assert v["yes_notify"] is True
    assert "SKIP_7M_NO" not in v["reason_codes"]


# 5. OFF -> a NO at 7M fires as before (byte-identical).
def test_skip_7m_no_off_fires_7m_no():
    v = gate.evaluate(_no(), _cfg(skip_7m_no_deliver=False), interval="7M")
    assert v["fired"] is True
    assert "SKIP_7M_NO" not in v["reason_codes"]
