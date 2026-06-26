"""Tests for the C5 risk tier (LOW ~95% / HIGH ~81%) label + the asymmetric YES margin.

The risk tier is a RECORD-ONLY label stamped on reason_codes (RISK_LOW / RISK_HIGH) and
surfaced on the entry card: LOW when BTC clears the side's cutoff by >= risk_low_btc_margin
AND the book is tight (spread < risk_low_max_spread); else HIGH (borderline BTC or wide
spread — the prime turn / early-exit set). It never gates or sizes by itself.
"""
from __future__ import annotations

from q15_upgrade.ultoim_v2 import gate, panel
from q15_upgrade.ultoim_v2.config import UltoimV2Config


def _cfg(**over):
    # BTC-confirm on; inverse-edge off so the positive-edge base candidate isn't blocked.
    return UltoimV2Config(enabled=True, btc_confirm_enabled=True, require_inverse_edge=False, **over)


def _cand(side="NO", spread=1.0, **over):
    c = {"predicted_side": side, "selected_probability": 0.62, "entry_ask_cents": 55,
         "total_cost_cents": 2, "spread_cents": spread}
    c.update(over)
    return c


# --- asymmetric YES margin: YES confirms at the 0.60 band (NO keeps the strict 0.15) ---
def test_asymmetric_yes_margin_admits_060_band():
    cfg = _cfg(no_only=False)                       # margin_yes default 0.10 -> YES cutoff 0.60
    assert gate.evaluate(_cand("YES"), cfg, interval="10M", btc_yes_prob=0.62)["fired"] is True
    # tightening margin_yes back to 0.15 (cutoff 0.65) blocks the same 0.62:
    cfg15 = _cfg(no_only=False, btc_confirm_margin_yes=0.15)
    assert gate.evaluate(_cand("YES"), cfg15, interval="10M", btc_yes_prob=0.62)["fired"] is False


# --- tier classification ---
def test_risk_low_decisive_and_tight():
    v = gate.evaluate(_cand("NO", spread=1.0), _cfg(), interval="10M", btc_yes_prob=0.20)
    assert v["risk_tier"] == "LOW" and "RISK_LOW" in v["reason_codes"]


def test_risk_high_borderline_btc():
    # BTC 0.30 is past the 0.35 cutoff by only 0.05 (< 0.10) -> borderline -> HIGH.
    v = gate.evaluate(_cand("NO", spread=1.0), _cfg(), interval="10M", btc_yes_prob=0.30)
    assert v["risk_tier"] == "HIGH" and "RISK_HIGH" in v["reason_codes"]


def test_risk_high_wide_spread_even_if_btc_decisive():
    v = gate.evaluate(_cand("NO", spread=5.0), _cfg(), interval="10M", btc_yes_prob=0.20)
    assert v["risk_tier"] == "HIGH" and "RISK_HIGH" in v["reason_codes"]


def test_risk_low_yes_side():
    v = gate.evaluate(_cand("YES", spread=1.0), _cfg(no_only=False), interval="10M", btc_yes_prob=0.75)
    assert v["risk_tier"] == "LOW" and "RISK_LOW" in v["reason_codes"]


def test_missing_spread_treated_tight():
    v = gate.evaluate(_cand("NO", spread=None), _cfg(), interval="10M", btc_yes_prob=0.20)
    assert v["risk_tier"] == "LOW"


def test_no_tier_when_btc_confirm_off():
    cfg = UltoimV2Config(enabled=True, btc_confirm_enabled=False, require_inverse_edge=False)
    v = gate.evaluate(_cand("NO"), cfg, interval="10M", btc_yes_prob=0.20)
    assert v["risk_tier"] is None
    assert "RISK_LOW" not in v["reason_codes"] and "RISK_HIGH" not in v["reason_codes"]


def test_no_tier_on_btc_unconfirmed_entry():
    v = gate.evaluate(_cand("NO"), _cfg(), interval="10M", btc_yes_prob=0.45)  # mushy -> blocked
    assert v["risk_tier"] is None and "BTC_UNCONFIRMED" in v["reason_codes"]


# --- the entry card surfaces the label ---
def _pick(rc, side="NO", interval="10M"):
    return {"asset": "ETH", "predicted_side": side, "ticker": "KXETH", "interval": interval,
            "window_key": 1, "selected_probability": 0.85, "entry_ask_cents": 74,
            "best_entry_cents": 74, "net_edge_cents": -3.0, "reason_codes": rc}


def test_panel_renders_risk_low():
    card = panel.build_entry_alert(_pick("EXPENSIVE_NO_ADMIT,RISK_LOW"), {"resolved": 100}, UltoimV2Config())
    assert "Risk: Low" in card


def test_panel_renders_risk_high_on_7m():
    card = panel.build_entry_alert(_pick("RISK_HIGH", interval="7M"), {"resolved": 100}, UltoimV2Config())
    assert "Risk: High" in card and "7M" in card


def test_panel_no_risk_line_without_marker():
    card = panel.build_entry_alert(_pick("EXPENSIVE_NO_ADMIT"), {"resolved": 100}, UltoimV2Config())
    assert "Risk:" not in card
