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
    base = dict(enabled=True, dry_run=True, bankroll_cents=100_000,  # $1000
                per_pick_pct=0.04, max_picks_per_window=2, max_per_window_pct=0.08,
                daily_loss_limit_pct=0.20, max_open_positions=6,
                min_price_cents=50, max_price_cents=85)
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
    cfg = _cfg()
    st = PortfolioState(bankroll_cents=100_000, day_start_bankroll_cents=100_000,
                        day_realized_pnl_cents=-20_001)   # past -20%
    assert decide(_pick(), st, cfg).reason == "DAILY_STOP"


def test_decide_max_open_blocks():
    cfg = _cfg(max_open_positions=2)
    st = PortfolioState(bankroll_cents=100_000, open_count=2)
    assert decide(_pick(), st, cfg).reason == "MAX_OPEN"


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
    # $5000 bankroll, 4% = $200/pick, but the $50 hard cap wins.
    cfg = _cfg(bankroll_cents=500_000)
    st = PortfolioState(bankroll_cents=500_000)
    d = decide(_pick(price=65), st, cfg)
    assert d.place is True
    assert d.stake_cents <= 5000                 # never more than $50 risked
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
