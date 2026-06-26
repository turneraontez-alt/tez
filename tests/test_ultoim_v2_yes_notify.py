"""Tests for the Ultoim V2 PAPER YES-notification gate (``yes_notify``).

When ``yes_notify_enabled`` is ON, a high-conviction YES -- V2 ``calibrated_yes`` >=
hiconv_yes_cal_min (0.70) AND market-implied YES >= hiconv_yes_market_min (0.60) -- that BTC
contemporaneously CONFIRMS (btc_yes_prob >= 0.5 + btc_confirm_margin_yes) delivers a Telegram
NOTIFICATION. It is a SEPARATE verdict signal from ``fired`` (the NO delivery path is byte-identical),
and it is PAPER ONLY -- the runner delivers it through an isolated block and ``_maybe_execute`` is
hard NO-only, so a YES never reaches a real order. Default OFF -> ``yes_notify`` is never set.

Deterministic: pure ``gate.evaluate`` calls, no network / clock / sleep. Runs at 10M so the 15M
distance gate never interferes. The hiconv YES sit ABOVE the NO ask band (~86c), which is exactly
why ``yes_notify`` admits on the conviction criteria rather than the NO-tuned ask ceiling.
"""
from __future__ import annotations

from q15_upgrade.ultoim_v2 import gate
from q15_upgrade.ultoim_v2.config import UltoimV2Config


def _cfg(**over):
    """Config with the paper YES-notify path ON and BTC-confirm ON (the live posture)."""
    base = dict(enabled=True, yes_notify_enabled=True, btc_confirm_enabled=True, no_only=False)
    base.update(over)
    return UltoimV2Config(**base)


def _yes(**over):
    """A high-conviction YES at the ~86c favorite price (above the NO ask band)."""
    cand = {
        "predicted_side": "YES",
        "selected_probability": 0.86,
        "calibrated_yes_probability": 0.85,
        "market_implied_yes_probability": 0.80,
        "entry_ask_cents": 86,
        "total_cost_cents": 2,
    }
    cand.update(over)
    return cand


# 1. DEFAULT OFF: the paper YES-notify path is off by default.
def test_yes_notify_default_off():
    assert UltoimV2Config().yes_notify_enabled is False


# 2. hiconv YES + BTC decisively YES -> notifies, but NEVER sets the NO-path ``fired``.
def test_yes_notify_hiconv_with_decisive_btc_yes():
    v = gate.evaluate(_yes(), _cfg(), interval="10M", btc_yes_prob=0.70)
    assert v["yes_notify"] is True
    assert v["fired"] is False                       # YES never rides the NO delivery path
    assert "YES_NOTIFY" in v["reason_codes"]


# 3. hiconv YES but BTC MUSHY (pinned) -> the raw-YES coin-flip is suppressed.
def test_yes_notify_blocked_when_btc_mushy():
    v = gate.evaluate(_yes(), _cfg(), interval="10M", btc_yes_prob=0.55)
    assert v["yes_notify"] is False
    assert "BTC_UNCONFIRMED" in v["reason_codes"]


# 4. Not high-conviction enough on the MODEL side -> no notify.
def test_yes_notify_requires_calibrated_floor():
    v = gate.evaluate(_yes(calibrated_yes_probability=0.65), _cfg(), interval="10M", btc_yes_prob=0.70)
    assert v["yes_notify"] is False


# 5. Not high-conviction enough on the MARKET side -> no notify (don't fight the market).
def test_yes_notify_requires_market_floor():
    v = gate.evaluate(_yes(market_implied_yes_probability=0.55), _cfg(), interval="10M", btc_yes_prob=0.70)
    assert v["yes_notify"] is False


# 6. Flag OFF -> no notify even on a textbook hiconv + BTC-confirmed YES.
def test_yes_notify_off_never_notifies():
    v = gate.evaluate(_yes(), _cfg(yes_notify_enabled=False), interval="10M", btc_yes_prob=0.70)
    assert v["yes_notify"] is False
    assert "YES_NOTIFY" not in v["reason_codes"]


# 7. A NO candidate never trips the YES path.
def test_yes_notify_never_on_no_side():
    no = {"predicted_side": "NO", "selected_probability": 0.62,
          "entry_ask_cents": 55, "total_cost_cents": 2}
    v = gate.evaluate(no, _cfg(), interval="10M", btc_yes_prob=0.30)
    assert v["yes_notify"] is False


# 8. Confidence floor still applies to the YES notify (a sub-min-conf YES is not delivered).
def test_yes_notify_respects_confidence_floor():
    v = gate.evaluate(_yes(selected_probability=0.40), _cfg(), interval="10M", btc_yes_prob=0.70)
    assert v["yes_notify"] is False


# 9. FAIL-CLOSED when the calibrated/market probabilities are missing.
def test_yes_notify_fail_closed_on_missing_probs():
    v = gate.evaluate(_yes(calibrated_yes_probability=None, market_implied_yes_probability=None),
                      _cfg(), interval="10M", btc_yes_prob=0.70)
    assert v["yes_notify"] is False
