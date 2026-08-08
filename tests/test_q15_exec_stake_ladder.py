"""Executor per-window STAKE LADDER (owner: "$275 on the first/best pick, $150 on the second").

The bot surfaces the best-price entry first, so the LARGER bet rides it. The ladder sizes a
pick by its ORDER within the settlement window: 1st pick -> ladder[0] ($275), 2nd -> ladder[1]
($150). Each rung is both the stake AND that pick's hard per-pick ceiling; the per-window budget
is the sum of the rungs; conviction up-sizing never stacks on top. Applies to BOTH the NO book and
the live YES bot. All money is integer cents; every test is pure / dry-run (no network).
"""

from __future__ import annotations

from q15_upgrade.executor.config import ExecutorConfig, yes_config_from_env
from q15_upgrade.executor.executor import Executor
from q15_upgrade.executor.risk import Pick, PortfolioState, decide


def _cfg(**over):
    base = dict(
        enabled=True, dry_run=True, bankroll_cents=1_000_000,
        stake_ladder_cents=(27500, 15000), max_picks_per_window=2,
        max_per_window_cents=45000, flat_stake_cents=7500, max_stake_per_pick_cents=7500,
        conviction_sizing=True, daily_loss_limit_pct=0.0, daily_loss_limit_cents=0,
        max_open_positions=6, min_price_cents=50, max_price_cents=85, btc_gate_enabled=False,
        record_orders=False,
    )
    base.update(over)
    return ExecutorConfig(**base)


def _state(window_count=0, committed=0, wk=7):
    return PortfolioState(bankroll_cents=1_000_000,
                          window_count={wk: window_count},
                          window_committed_cents={wk: committed})


def _pick(wk=7, ticker="T", price=50, mult=1, interval="10M"):
    # price=50 divides both rungs evenly so stake_cents lands exactly on the rung.
    return Pick(ticker=ticker, asset="BTC", side="NO", price_cents=price,
                window_key=wk, interval=interval, stake_multiplier=mult)


# --------------------------------------------------------------------------- #
# defaults
# --------------------------------------------------------------------------- #
def test_default_ladder_is_275_then_150():
    assert ExecutorConfig().stake_ladder_cents == (27500, 15000)
    assert ExecutorConfig().max_picks_per_window == 2


def test_yes_bot_shares_the_same_ladder(monkeypatch):
    monkeypatch.delenv("Q15_EXEC_YES_STAKE_LADDER_CENTS", raising=False)
    assert yes_config_from_env().stake_ladder_cents == (27500, 15000)


# --------------------------------------------------------------------------- #
# pure decide() money math
# --------------------------------------------------------------------------- #
def test_first_pick_stakes_275():
    d = decide(_pick(), _state(window_count=0, committed=0), _cfg())
    assert d.place and d.stake_cents == 27500          # $275 on the lead/best pick


def test_second_pick_stakes_150():
    d = decide(_pick(ticker="T2"), _state(window_count=1, committed=27500), _cfg())
    assert d.place and d.stake_cents == 15000          # $150 on the second pick


def test_window_total_is_425_and_third_pick_refused():
    cfg = _cfg()
    d1 = decide(_pick(ticker="A"), _state(window_count=0, committed=0), cfg)
    d2 = decide(_pick(ticker="B"), _state(window_count=1, committed=d1.stake_cents), cfg)
    assert d1.stake_cents + d2.stake_cents == 42500    # $275 + $150 = $425 window total
    # max_picks_per_window=2 -> a 3rd pick is refused.
    d3 = decide(_pick(ticker="C"), _state(window_count=2, committed=42500), cfg)
    assert not d3.place and d3.reason == "WINDOW_FULL"


def test_ladder_rung_is_the_per_pick_ceiling():
    # The lead rung ($275) supersedes the $75 hard per-pick cap (max_stake_per_pick_cents=7500),
    # exactly as stake_by_interval does — otherwise the bet would clamp to $75.
    d = decide(_pick(), _state(window_count=0), _cfg(max_stake_per_pick_cents=7500))
    assert d.place and d.stake_cents == 27500


def test_ladder_overrides_flat_and_by_interval():
    # Both flat_stake and a stake_by_interval override are present but the ladder wins (highest
    # precedence): the lead pick still stakes $275, not $75 (flat) or $100 (interval).
    cfg = _cfg(flat_stake_cents=7500, stake_by_interval={"10M": 10000})
    d = decide(_pick(interval="10M"), _state(window_count=0), cfg)
    assert d.place and d.stake_cents == 27500


def test_ladder_bypasses_conviction_doubling():
    # A 2nd pick carrying stake_multiplier=2 must NOT double on top of the ladder — it stays at the
    # 2nd rung ($150), because the ladder is self-contained per-pick sizing.
    d = decide(_pick(ticker="T2", mult=2), _state(window_count=1, committed=27500), _cfg())
    assert d.place and d.stake_cents == 15000


def test_pick_past_end_reuses_last_rung():
    # A 3rd pick (when picks/window allows it) reuses the last rung rather than indexing out.
    cfg = _cfg(max_picks_per_window=3, max_per_window_cents=0)  # no abs cap so the 3rd can place
    d = decide(_pick(ticker="C"), _state(window_count=2, committed=42500), cfg)
    assert d.place and d.stake_cents == 15000          # ladder[-1] = $150


def test_window_cap_clamps_ladder_sum():
    # The absolute $/window cap is still the final clamp: cap the window at $300 and the 2nd pick
    # is squeezed to the $25 left after the lead's $275 (then refused for too few contracts).
    cfg = _cfg(max_per_window_cents=30000)
    d2 = decide(_pick(ticker="B", price=50), _state(window_count=1, committed=27500), cfg)
    # only 2500c remain in the $300 window -> 50 contracts @ 50c = $25.
    assert d2.place and d2.stake_cents == 2500


# --------------------------------------------------------------------------- #
# env parsing + disable
# --------------------------------------------------------------------------- #
def test_ladder_parses_env(monkeypatch):
    monkeypatch.setenv("Q15_EXEC_STAKE_LADDER_CENTS", "30000, 20000 ,bad,0,x")
    assert ExecutorConfig().stake_ladder_cents == (30000, 20000)


def test_empty_ladder_disables_and_falls_back_to_flat(monkeypatch):
    monkeypatch.setenv("Q15_EXEC_STAKE_LADDER_CENTS", "")
    cfg = ExecutorConfig()
    assert cfg.stake_ladder_cents == ()
    # With the ladder off, sizing falls back to flat $75 (the documented fallback).
    d = decide(_pick(price=50), _state(window_count=0),
               _cfg(stake_ladder_cents=(), flat_stake_cents=7500, max_stake_per_pick_cents=7500))
    assert d.place and d.stake_cents == 7500


# --------------------------------------------------------------------------- #
# on_fire threads the ladder end-to-end (dry-run)
# --------------------------------------------------------------------------- #
class _StubClient:
    def __init__(self):
        self.orders = []

    def get_balance_cents(self):
        return None

    def place_order(self, **kw):
        self.orders.append(kw)
        return {"ok": True, "dry_run": True, "would_place": kw}


def test_on_fire_first_275_second_150():
    ex = Executor(_cfg(), client=_StubClient())
    base = {"asset": "BTC", "predicted_side": "NO", "entry_ask_cents": 50, "window_key": 7,
            "interval": "10M"}
    r1 = ex.on_fire({**base, "ticker": "T-A"})
    r2 = ex.on_fire({**base, "ticker": "T-B"})
    assert r1["placed"] and r1["stake_cents"] == 27500   # first/best pick $275
    assert r2["placed"] and r2["stake_cents"] == 15000   # second pick $150


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
