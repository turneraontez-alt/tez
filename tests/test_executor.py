"""Tests for the opt-in live-order executor. Everything here runs in dry-run / pure
mode — no test ever touches the network or needs Kalshi keys."""
from __future__ import annotations

import pytest

from q15_upgrade.executor.config import ExecutorConfig
from q15_upgrade.executor.risk import Pick, PortfolioState, decide, apply_fill
from q15_upgrade.executor.executor import Executor
from q15_upgrade.executor import executor as executor_mod
from q15_upgrade.executor.trading_client import KalshiTradingClient


def _cfg(**over):
    # flat_stake_cents=0 pins these to the % sizing path (the production default is now FLAT
    # $75); the flat-mode behaviour has its own tests below. Overrides win.
    base = dict(enabled=True, dry_run=True, bankroll_cents=100_000,  # $1000
                per_pick_pct=0.04, max_picks_per_window=2, max_per_window_pct=0.08,
                daily_loss_limit_pct=0.20, daily_loss_limit_cents=0, max_open_positions=6,
                flat_stake_cents=0, min_price_cents=50, max_price_cents=85)
    base.update(over)
    return ExecutorConfig(**base)


def _pick(ticker="T-BTC", asset="BTC", side="NO", price=65, wk=1):
    return Pick(ticker=ticker, asset=asset, side=side, price_cents=price, window_key=wk)


# --------------------------------------------------------------------------- #
# risk.decide — sizing + every guard
# --------------------------------------------------------------------------- #
def test_decide_sizes_count_from_bankroll():
    cfg = _cfg()
    st = PortfolioState(bankroll_cents=100_000)
    d = decide(_pick(price=65), st, cfg)
    assert d.place is True
    assert d.stake_cents <= 4000                 # 4% of $1000
    assert d.count == 4000 // 65 == 61           # whole contracts at 65c
    assert d.limit_price_cents == 65


def test_decide_kill_switch_blocks():
    d = decide(_pick(), PortfolioState(bankroll_cents=100_000), _cfg(kill_switch=True))
    assert d.place is False and d.reason == "KILL"


def test_decide_wrong_side_blocks():
    d = decide(_pick(side="YES"), PortfolioState(bankroll_cents=100_000), _cfg())
    assert d.place is False and d.reason == "WRONG_SIDE"


def test_decide_price_band_blocks():
    st = PortfolioState(bankroll_cents=100_000)
    assert decide(_pick(price=40), st, _cfg()).reason == "PRICE_BAND"
    assert decide(_pick(price=90), st, _cfg()).reason == "PRICE_BAND"


def test_decide_daily_stop_blocks():
    cfg = _cfg()   # daily_loss_limit_cents=0 -> the %-based stop applies
    st = PortfolioState(bankroll_cents=100_000, day_start_bankroll_cents=100_000,
                        day_realized_pnl_cents=-20_001)   # past -20%
    assert decide(_pick(), st, cfg).reason == "DAILY_STOP"


def test_decide_absolute_stop_loss_blocks_at_dollar_limit():
    # $100 absolute stop: down $100 halts new entries; $99.99 still trades.
    cfg = _cfg(daily_loss_limit_cents=10_000)
    st = PortfolioState(bankroll_cents=34_000, day_start_bankroll_cents=34_000,
                        day_realized_pnl_cents=-10_000)        # exactly -$100
    assert decide(_pick(), st, cfg).reason == "DAILY_STOP"
    st_ok = PortfolioState(bankroll_cents=34_000, day_start_bankroll_cents=34_000,
                           day_realized_pnl_cents=-9_999)      # -$99.99
    assert decide(_pick(price=65), st_ok, cfg).place is True


def test_absolute_stop_loss_governs_over_pct():
    # With BOTH set, the absolute $100 governs: a -$80 day (past $100? no) still trades even
    # though it is past 20% of a tiny day-start; and -$100 stops even when under 20%.
    cfg = _cfg(daily_loss_limit_cents=10_000, daily_loss_limit_pct=0.20)
    # day-start $1000, down $100 = 10% (< 20%) but hits the absolute -> stop.
    st = PortfolioState(bankroll_cents=100_000, day_start_bankroll_cents=100_000,
                        day_realized_pnl_cents=-10_000)
    assert decide(_pick(), st, cfg).reason == "DAILY_STOP"
    # down $90 = 9% and under the $100 absolute -> trades (the % would NOT have stopped here either).
    st2 = PortfolioState(bankroll_cents=100_000, day_start_bankroll_cents=100_000,
                         day_realized_pnl_cents=-9_000)
    assert decide(_pick(price=65), st2, cfg).place is True


def test_executor_stop_loss_fires_from_live_balance():
    # Live balance falls $100 below the day-start -> the next entry is refused (STOP/DAILY_STOP),
    # and NO order is placed. Stub returns $340 at init, $240 on the refresh read.
    class _BalClient:
        def __init__(self, bals): self._b = list(bals); self.orders = []
        def get_balance_cents(self):
            return self._b.pop(0) if len(self._b) > 1 else self._b[0]
        def place_order(self, **kw):
            self.orders.append(kw); return {"ok": True, "dry_run": False}
    ex = Executor(_cfg(enabled=True, dry_run=False, bankroll_cents=0,
                       flat_stake_cents=7500, daily_loss_limit_cents=10_000),
                  client=_BalClient([34_000, 24_000]))   # init reads 34000; refresh reads 24000
    r = ex.on_fire({"ticker": "T-BTC", "asset": "BTC", "predicted_side": "NO",
                    "entry_price_cents": 65, "window_key": 1})
    assert r["placed"] is False and r["reason"] == "DAILY_STOP"
    assert ex.client.orders == []                         # nothing was placed after the stop


def test_on_fire_bands_on_entry_ask_not_best_entry():
    """Rec #2 alignment: the executor must band/fill on entry_ask_cents (the field the v2 gate
    admitted on), not best_entry_cents. Here best_entry_cents=48 is BELOW the executor's 50c
    floor while the gate-admitted entry_ask_cents=55 is inside the band — keying on the ask makes
    the gate-admitted set and the executor-accepted set agree (before the fix this was refused)."""
    class _RecClient:
        def __init__(self): self.orders = []
        def get_balance_cents(self): return None
        def place_order(self, **kw):
            self.orders.append(kw); return {"ok": True, "dry_run": True}
    ex = Executor(_cfg(enabled=True, dry_run=True, bankroll_cents=100_000,
                       flat_stake_cents=7500, min_price_cents=50, max_price_cents=85),
                  client=_RecClient())
    r = ex.on_fire({"ticker": "T-BTC", "asset": "BTC", "predicted_side": "NO",
                    "best_entry_cents": 48, "entry_ask_cents": 55, "window_key": 1})
    assert r["placed"] is True
    assert r["limit_price_cents"] == 55                  # banded/limited on the ask, not 48
    assert ex.client.orders and ex.client.orders[0]["price_cents"] == 55
    # entry_price_cents (explicit override) still wins when provided.
    ex2 = Executor(_cfg(enabled=True, dry_run=True, bankroll_cents=100_000,
                        flat_stake_cents=7500, min_price_cents=50, max_price_cents=85),
                   client=_RecClient())
    r2 = ex2.on_fire({"ticker": "T-ETH", "asset": "ETH", "predicted_side": "NO",
                      "entry_price_cents": 60, "best_entry_cents": 48, "entry_ask_cents": 55,
                      "window_key": 2})
    assert r2["placed"] is True and r2["limit_price_cents"] == 60


def test_decide_max_open_blocks():
    cfg = _cfg(max_open_positions=2)
    st = PortfolioState(bankroll_cents=100_000, open_count=2)
    assert decide(_pick(), st, cfg).reason == "MAX_OPEN"


def test_interval_allowlist_default_allows_all():
    """Rec #3: default empty allowlist is byte-identical — any interval (or none) places."""
    cfg = _cfg()
    assert cfg.allowed_intervals == frozenset()
    st = PortfolioState(bankroll_cents=100_000)
    assert decide(Pick("T-BTC", "BTC", "NO", 65, 1, interval="15M"), st, cfg).place is True
    assert decide(Pick("T-BTC", "BTC", "NO", 65, 1, interval=""), st, cfg).place is True


def test_interval_allowlist_blocks_non_listed_interval():
    """Rec #3: with {10M,7M} set, a 10M fire places but a 15M/12M fire is refused — a backstop
    so a v2 gating regression cannot leak a structurally -EV early-interval order to real money."""
    cfg = _cfg(allowed_intervals=frozenset({"10M", "7M"}))
    st = PortfolioState(bankroll_cents=100_000)
    assert decide(Pick("T-BTC", "BTC", "NO", 65, 1, interval="10M"), st, cfg).place is True
    assert decide(Pick("T-BTC", "BTC", "NO", 65, 1, interval="7M"), st, cfg).place is True
    assert decide(Pick("T-BTC", "BTC", "NO", 65, 1, interval="15M"), st, cfg).reason == "INTERVAL_BLOCKED"
    assert decide(Pick("T-BTC", "BTC", "NO", 65, 1, interval="12M"), st, cfg).reason == "INTERVAL_BLOCKED"
    # an unknown/missing interval is also refused when an allowlist is in force (fail-closed).
    assert decide(Pick("T-BTC", "BTC", "NO", 65, 1, interval=""), st, cfg).reason == "INTERVAL_BLOCKED"


def test_interval_allowlist_parses_env(monkeypatch):
    monkeypatch.setenv("Q15_EXEC_ALLOWED_INTERVALS", "10m, 7M ")
    assert ExecutorConfig().allowed_intervals == frozenset({"10M", "7M"})


def test_decide_dup_ticker_blocks():
    cfg = _cfg()
    st = PortfolioState(bankroll_cents=100_000, open_tickers=frozenset({"T-BTC"}))
    assert decide(_pick(ticker="T-BTC"), st, cfg).reason == "DUP_TICKER"


def test_decide_window_full_blocks():
    cfg = _cfg(max_picks_per_window=2)
    st = PortfolioState(bankroll_cents=100_000, window_count={1: 2})
    assert decide(_pick(wk=1), st, cfg).reason == "WINDOW_FULL"


def test_decide_window_cap_clamps_stake():
    # per_pick 4% = 4000c, but only 1000c of the 8000c window cap remains -> clamp.
    cfg = _cfg()
    st = PortfolioState(bankroll_cents=100_000, window_committed_cents={1: 7000})
    d = decide(_pick(wk=1, price=50), st, cfg)
    assert d.place is True and d.stake_cents <= 1000
    assert d.count == 1000 // 50 == 20


def test_decide_per_pick_dollar_cap_binds():
    # $5000 bankroll, 4% = $200/pick, but the hard per-pick cap wins. Pin the cap to $50
    # here (production default is now $75) so the binding mechanism is tested at a fixed value.
    cfg = _cfg(bankroll_cents=500_000, max_stake_per_pick_cents=5000)
    st = PortfolioState(bankroll_cents=500_000)
    d = decide(_pick(price=65), st, cfg)
    assert d.place is True
    assert d.stake_cents <= 5000                 # never more than the $50 cap risked
    assert d.count == 5000 // 65 == 76


def test_decide_size_too_small_blocks():
    # tiny bankroll: 4% of $5 = 20c, below one 65c contract -> refuse.
    cfg = _cfg(bankroll_cents=500)
    st = PortfolioState(bankroll_cents=500)
    assert decide(_pick(price=65), st, cfg).reason == "SIZE_TOO_SMALL"


def test_decide_no_bankroll_blocks():
    assert decide(_pick(), PortfolioState(bankroll_cents=0), _cfg(bankroll_cents=0)).reason == "BANKROLL"


def test_apply_fill_updates_state():
    cfg = _cfg()
    st = PortfolioState(bankroll_cents=100_000)
    p = _pick(wk=1)
    d = decide(p, st, cfg)
    st2 = apply_fill(st, p, d)
    assert st2.open_count == 1
    assert "T-BTC" in st2.open_tickers
    assert st2.window_count[1] == 1
    assert st2.window_committed_cents[1] == d.stake_cents


# --------------------------------------------------------------------------- #
# FLAT sizing mode (owner default: $75 flat / pick, 1 pick/window)
# --------------------------------------------------------------------------- #
def test_owner_default_executor_sizing_is_flat_75_one_pick():
    cfg = ExecutorConfig()      # production defaults
    assert cfg.flat_stake_cents == 7500          # $75 flat per pick
    assert cfg.max_picks_per_window == 1         # single best pick per window
    assert cfg.max_stake_per_pick_cents == 7500  # hard cap matches


def test_flat_stake_overrides_pct_sizing():
    # flat $75 ignores the 4% (=$40 on $1000) — stakes the fixed amount instead.
    cfg = _cfg(flat_stake_cents=7500)
    st = PortfolioState(bankroll_cents=100_000)
    d = decide(_pick(price=65), st, cfg)
    assert d.place is True
    assert d.stake_cents == (7500 // 65) * 65     # $75 worth of whole contracts
    assert d.count == 7500 // 65 == 115


def test_flat_stake_one_pick_per_window_blocks_second():
    cfg = _cfg(flat_stake_cents=7500, max_picks_per_window=1)
    st = PortfolioState(bankroll_cents=100_000)
    p = _pick(wk=1)
    d = decide(p, st, cfg)
    assert d.place is True and d.count == 115
    st2 = apply_fill(st, p, d)
    # a second pick in the same window is refused (1 pick/window).
    assert decide(_pick(ticker="T-ETH", wk=1), st2, cfg).reason == "WINDOW_FULL"


def test_flat_stake_window_budget_allows_two_when_maxpicks_two():
    # flat window budget = flat * max_picks; with max_picks=2 a second $75 pick fits.
    cfg = _cfg(flat_stake_cents=7500, max_picks_per_window=2)
    st = PortfolioState(bankroll_cents=100_000, window_committed_cents={1: 7475},
                        window_count={1: 1})
    d = decide(_pick(ticker="T-ETH", wk=1, price=65), st, cfg)
    assert d.place is True and d.count == 115     # second flat $75 still fits in the $150 budget


def test_flat_stake_clamped_to_bankroll_and_hard_cap():
    # bankroll only $50 -> can't stake the full flat $75; clamps to bankroll.
    cfg = _cfg(flat_stake_cents=7500, bankroll_cents=5000)
    d = decide(_pick(price=65), PortfolioState(bankroll_cents=5000), cfg)
    assert d.place is True and d.stake_cents <= 5000
    # flat above the hard per-pick cap is clamped to the cap.
    cfg2 = _cfg(flat_stake_cents=20000, max_stake_per_pick_cents=7500)
    d2 = decide(_pick(price=65), PortfolioState(bankroll_cents=100_000), cfg2)
    assert d2.place is True and d2.stake_cents <= 7500


# --------------------------------------------------------------------------- #
# Executor — dry-run end to end (NO network)
# --------------------------------------------------------------------------- #
class _StubClient:
    def __init__(self): self.orders = []
    def get_balance_cents(self): return None
    def place_order(self, **kw):
        self.orders.append(kw)
        return {"ok": True, "dry_run": True, "would_place": kw}


def _exec(**over):
    return Executor(_cfg(**over), client=_StubClient())


def test_executor_dry_run_places_and_updates_state():
    ex = _exec()
    r = ex.on_fire({"ticker": "T-BTC", "asset": "BTC", "predicted_side": "NO",
                    "entry_price_cents": 65, "window_key": 1})
    assert r["placed"] is True and r["dry_run"] is True and r["count"] == 61
    assert ex.state.open_count == 1 and "T-BTC" in ex.state.open_tickers
    assert ex.client.orders[0]["action"] == "buy" and ex.client.orders[0]["side"] == "no"


def test_executor_window_cap_blocks_third_pick():
    ex = _exec()
    for tk in ("T-BTC", "T-ETH"):
        assert ex.on_fire({"ticker": tk, "asset": tk[2:], "predicted_side": "NO",
                           "entry_price_cents": 60, "window_key": 1})["placed"] is True
    r = ex.on_fire({"ticker": "T-SOL", "asset": "SOL", "predicted_side": "NO",
                    "entry_price_cents": 60, "window_key": 1})
    assert r["placed"] is False and r["reason"] == "WINDOW_FULL"
    assert len(ex.client.orders) == 2


def test_executor_dup_ticker_blocked():
    ex = _exec()
    base = {"ticker": "T-BTC", "asset": "BTC", "predicted_side": "NO",
            "entry_price_cents": 60, "window_key": 1}
    assert ex.on_fire(base)["placed"] is True
    assert ex.on_fire(dict(base))["reason"] == "DUP_TICKER"


def test_executor_yes_side_never_executes():
    ex = _exec()
    r = ex.on_fire({"ticker": "T-BTC", "asset": "BTC", "predicted_side": "YES",
                    "entry_price_cents": 60, "window_key": 1})
    assert r["placed"] is False and r["reason"] == "WRONG_SIDE"
    assert ex.client.orders == []


def test_executor_disabled_blocks_and_factory_returns_none(monkeypatch):
    ex = Executor(_cfg(enabled=False), client=_StubClient())
    assert ex.on_fire({"ticker": "T-BTC", "window_key": 1, "entry_price_cents": 60})["reason"] == "DISABLED"
    monkeypatch.delenv("Q15_EXEC_ENABLED", raising=False)
    executor_mod.reset_executor_for_tests()
    assert executor_mod.get_executor() is None


def test_executor_exit_sells_full_position_and_closes_out():
    ex = _exec()
    assert ex.on_exit("T-BTC", 1, 30)["reason"] == "NO_POSITION"
    ex.on_fire({"ticker": "T-BTC", "asset": "BTC", "predicted_side": "NO",
                "entry_price_cents": 60, "window_key": 1})
    held = ex.state.positions["T-BTC"]               # the count it bought
    assert held == 4000 // 60 == 66
    r = ex.on_exit("T-BTC", 1, 30)
    assert r["placed"] is True and r["count"] == held  # sells the WHOLE position, not 1
    assert ex.client.orders[-1]["action"] == "sell" and ex.client.orders[-1]["count"] == held
    # position is closed out -> a second exit finds nothing.
    assert "T-BTC" not in ex.state.positions
    assert ex.on_exit("T-BTC", 1, 30)["reason"] == "NO_POSITION"


# --------------------------------------------------------------------------- #
# Trading client — dry-run NEVER touches the network or the signer
# --------------------------------------------------------------------------- #
class _BoomSession:
    def request(self, *a, **k):  # would raise if ever called
        raise AssertionError("network must NOT be touched in dry-run")


class _StubSigner:
    available = False
    error = "test"
    def sign(self, method, path): raise AssertionError("signer must NOT be used in dry-run")


def test_v2_side_price_mapping():
    from q15_upgrade.executor.trading_client import _v2_side_price
    assert _v2_side_price("yes", "buy", 65) == ("bid", "0.6500")
    assert _v2_side_price("no", "buy", 65) == ("ask", "0.3500")    # buy NO@65 == sell YES@35
    assert _v2_side_price("no", "sell", 30) == ("bid", "0.7000")   # sell NO@30 == buy YES@70


def test_trading_client_dry_run_logs_no_network():
    import uuid as _uuid
    cli = KalshiTradingClient(_cfg(dry_run=True), signer=_StubSigner(), session=_BoomSession())
    ready, why = cli.live_ready
    assert ready is False and why == "dry-run"
    r = cli.place_order(ticker="T-BTC", side="no", count=10, price_cents=65,
                        client_order_id="v2x-1-T-BTC-entry")
    assert r["ok"] is True and r["dry_run"] is True
    wp = r["would_place"]
    # V2 schema: bid/ask side, dollar-string price, string count, UUID client_order_id.
    assert wp["side"] == "ask" and wp["price"] == "0.3500" and wp["count"] == "10.00"
    _uuid.UUID(wp["client_order_id"])   # valid UUID (raises if not)


def test_trading_client_live_ready_requires_all_gates():
    sess = _BoomSession()
    # enabled + not-dry-run but kill switch on -> not ready
    cli = KalshiTradingClient(_cfg(dry_run=False, kill_switch=True), signer=_StubSigner(), session=sess)
    assert cli.live_ready[0] is False and cli.live_ready[1] == "kill switch on"
    # signer unavailable also blocks
    cli2 = KalshiTradingClient(_cfg(dry_run=False), signer=_StubSigner(), session=sess)
    assert cli2.live_ready[0] is False
