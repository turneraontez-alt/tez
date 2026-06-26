"""Executor conviction sizing (owner: "first $150, extras double").

A v2 pick from a >=3-co-trigger 10M window carries stake_multiplier=2. The executor
sizes the LEAD pick of a window at the base stake and doubles only the 2nd-or-later
pick of that window. Default ON (Q15_EXEC_CONVICTION_SIZING); the doubled extra is
admitted by the hard per-pick cap (which scales with the multiplier).

All dollar math is integer cents; every test is pure / dry-run (no network).
"""

from __future__ import annotations

from q15_upgrade.executor.config import ExecutorConfig
from q15_upgrade.executor.executor import Executor
from q15_upgrade.executor.risk import Pick, PortfolioState, decide


def _cfg(**over):
    base = dict(
        enabled=True, dry_run=True, bankroll_cents=1_000_000,
        flat_stake_cents=15000, max_stake_per_pick_cents=30000, conviction_sizing=True,
        stake_by_interval={}, stake_ladder_cents=(), max_picks_per_window=2, max_per_window_pct=1.0,
        daily_loss_limit_pct=0.0, daily_loss_limit_cents=0, max_open_positions=6,
        min_price_cents=50, max_price_cents=85, record_orders=False,
    )
    base.update(over)
    return ExecutorConfig(**base)


def _state(window_count=0, committed=0, wk=7):
    return PortfolioState(bankroll_cents=1_000_000,
                          window_count={wk: window_count},
                          window_committed_cents={wk: committed})


def _pick(mult=1, wk=7, ticker="T", price=60):
    return Pick(ticker=ticker, asset="BTC", side="NO", price_cents=price,
                window_key=wk, interval="10M", stake_multiplier=mult)


# --------------------------------------------------------------------------- #
# pure decide() money math
# --------------------------------------------------------------------------- #
def test_lead_pick_never_doubles_even_in_conviction_window():
    # First pick of the window (window_count=0): stays at the $150 base despite mult=2.
    d = decide(_pick(mult=2), _state(window_count=0), _cfg())
    assert d.place and d.stake_cents == 15000


def test_extra_pick_doubles_in_conviction_window():
    # 2nd pick (window_count=1) of a 3+ window with $150 already committed: doubles to $300.
    d = decide(_pick(mult=2, ticker="T2"), _state(window_count=1, committed=15000), _cfg())
    assert d.place and d.stake_cents == 30000


def test_extra_pick_not_doubled_when_not_conviction():
    # 2nd pick in a normal (1-2 co-trigger) window carries mult=1: stays $150.
    d = decide(_pick(mult=1, ticker="T2"), _state(window_count=1, committed=15000), _cfg())
    assert d.place and d.stake_cents == 15000


def test_conviction_sizing_off_keeps_flat():
    d = decide(_pick(mult=2, ticker="T2"),
               _state(window_count=1, committed=15000), _cfg(conviction_sizing=False))
    assert d.place and d.stake_cents == 15000


def test_doubled_extra_clamped_by_per_pick_cap():
    # The hard per-pick cap is ABSOLUTE (not scaled by the multiplier): with a $200 cap a
    # doubled extra ($300 wanted) is clamped to <=$200 — here 333 whole contracts @ 60c = $199.80.
    d = decide(_pick(mult=2, ticker="T2"),
               _state(window_count=1, committed=15000), _cfg(max_stake_per_pick_cents=20000))
    assert d.place and d.count == 333 and d.stake_cents == 19980 and d.stake_cents <= 20000


def test_conviction_window_total_is_lead_plus_doubled():
    cfg = _cfg()
    # Lead pick: $150.
    d1 = decide(_pick(mult=2, ticker="A"), _state(window_count=0, committed=0), cfg)
    # Extra pick after the lead committed $150: $300.
    d2 = decide(_pick(mult=2, ticker="B"), _state(window_count=1, committed=d1.stake_cents), cfg)
    assert (d1.stake_cents, d2.stake_cents) == (15000, 30000)
    assert d1.stake_cents + d2.stake_cents == 45000   # $450 window total


# --------------------------------------------------------------------------- #
# absolute per-window dollar cap (owner: $450 per 15-minute settlement window)
# --------------------------------------------------------------------------- #
def test_default_per_window_cap_is_450():
    # The owner-set hard cap ships on by default at $450 (45000c).
    assert ExecutorConfig().max_per_window_cents == 45000


def test_window_cap_clamps_conviction_total_to_450():
    # Without the absolute cap the conviction budget would be flat($150) x 2 picks x mult(2) =
    # $600. The $450 cap is the FINAL clamp, so the extra pick is sized to fill exactly to $450.
    cfg = _cfg(max_per_window_cents=45000)
    d1 = decide(_pick(mult=2, ticker="A"), _state(window_count=0, committed=0), cfg)
    d2 = decide(_pick(mult=2, ticker="B"), _state(window_count=1, committed=d1.stake_cents), cfg)
    assert d1.stake_cents == 15000                       # lead pick unchanged at $150
    assert d1.stake_cents + d2.stake_cents == 45000      # window total clamped to the $450 cap
    # A 3rd pick is refused — the window is already full at the dollar cap.
    d3 = decide(_pick(mult=2, ticker="C"), _state(window_count=2, committed=45000), cfg)
    assert not d3.place and d3.reason == "WINDOW_FULL"


def test_window_cap_off_allows_full_conviction_budget():
    # With the absolute cap disabled (0), the per-window budget reverts to flat x picks x mult.
    cfg = _cfg(max_per_window_cents=0)
    d1 = decide(_pick(mult=2, ticker="A"), _state(window_count=0, committed=0), cfg)
    d2 = decide(_pick(mult=2, ticker="B"), _state(window_count=1, committed=d1.stake_cents), cfg)
    # Lead $150 + doubled extra $300, only bounded by the $300 per-pick cap from _cfg().
    assert d1.stake_cents + d2.stake_cents == 45000
    # Lower the per-pick cap so the extra is the binding constraint and the window total exceeds $450.
    cfg2 = _cfg(max_per_window_cents=0, max_stake_per_pick_cents=60000, flat_stake_cents=30000)
    e1 = decide(_pick(mult=2, ticker="A", price=60), _state(window_count=0, committed=0), cfg2)
    e2 = decide(_pick(mult=2, ticker="B", price=60), _state(window_count=1, committed=e1.stake_cents), cfg2)
    assert e1.stake_cents + e2.stake_cents == 90000      # $300 lead + $600 extra, no $450 clamp


# --------------------------------------------------------------------------- #
# config + Pick defaults
# --------------------------------------------------------------------------- #
def test_conviction_sizing_defaults_on():
    assert ExecutorConfig().conviction_sizing is True


def test_pick_stake_multiplier_defaults_to_one():
    assert Pick(ticker="T", asset="BTC", side="NO", price_cents=60, window_key=1).stake_multiplier == 1


# --------------------------------------------------------------------------- #
# on_fire threads stake_multiplier end-to-end (dry-run)
# --------------------------------------------------------------------------- #
class _StubClient:
    def __init__(self):
        self.orders = []

    def get_balance_cents(self):
        return None

    def place_order(self, **kw):
        self.orders.append(kw)
        return {"ok": True, "dry_run": True, "would_place": kw}


def test_on_fire_first_pick_150_second_pick_300():
    ex = Executor(_cfg(), client=_StubClient())
    base = {"asset": "BTC", "predicted_side": "NO", "entry_ask_cents": 60,
            "window_key": 7, "interval": "10M", "stake_multiplier": 2}
    r1 = ex.on_fire({**base, "ticker": "T-A"})
    r2 = ex.on_fire({**base, "ticker": "T-B"})
    assert r1["placed"] and r1["stake_cents"] == 15000   # lead pick $150
    assert r2["placed"] and r2["stake_cents"] == 30000   # extra pick doubled to $300


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
