"""Executor POSITION LIFECYCLE — the gap between "we sent an order" and "we know
what we hold".

Each test here pins one way the executor could previously leave a real, funded
position stranded: unowned, un-exitable, or trading again after its stop. All of
it runs stubbed — no network, no Kalshi keys, no wall-clock dependence.
"""
from __future__ import annotations

from q15_upgrade.executor.config import ExecutorConfig
from q15_upgrade.executor.executor import Executor
from q15_upgrade.executor import executor as executor_mod
from q15_upgrade.executor.trading_client import KalshiTradingClient


def _cfg(**over):
    base = dict(enabled=True, dry_run=False, bankroll_cents=100_000,
                flat_stake_cents=7500, max_picks_per_window=2, min_price_cents=50,
                max_price_cents=85, daily_loss_limit_cents=0, daily_loss_limit_pct=0,
                max_open_positions=6, record_orders=False)
    base.update(over)
    return ExecutorConfig(**base)


class _StubSigner:
    """Stand-in for KalshiSigner — no keys, no crypto, just the header shape."""

    available = True
    error = None

    def sign(self, method, path):
        return {"KALSHI-ACCESS-KEY": "test", "KALSHI-ACCESS-SIGNATURE": "sig",
                "KALSHI-ACCESS-TIMESTAMP": "0"}


class _LifecycleClient:
    """Stub broker: scriptable place/cancel outcomes; records every call."""

    def __init__(self, place_results=None, cancel_result=None, balance=None,
                 dry_run=False):
        self._place = list(place_results or [])
        self._cancel = cancel_result or {"ok": True}
        self._balance = balance
        # The real client reports dry_run on the response, and the store records
        # `mode` from it — so the stub must echo it for rehydrate tests to be honest.
        self._dry_run = bool(dry_run)
        self.placed = []
        self.cancelled = []

    def get_balance_cents(self):
        return self._balance

    def place_order(self, **kw):
        self.placed.append(kw)
        if self._place:
            return self._place.pop(0)
        return {"ok": True, "dry_run": self._dry_run,
                "data": {"order": {"order_id": "auto", "status": "resting",
                                   "fill_count": 0}}}

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return self._cancel


def _fire(ex, ticker="T-BTC", wk=1, price=65):
    return ex.on_fire({"ticker": ticker, "asset": "BTC", "predicted_side": "NO",
                       "entry_ask_cents": price, "window_key": wk, "interval": "10M"})


# --------------------------------------------------------------------------- #
# 1. An ambiguous POST failure must BOOK the exposure, not free the budget.
# --------------------------------------------------------------------------- #

def test_uncertain_order_books_the_exposure():
    """A read timeout may mean the order landed. Treating it as 'not placed' left a
    real position untracked AND released the window budget for the next pick."""
    client = _LifecycleClient(place_results=[
        {"ok": False, "uncertain": True, "error": "request_failed: ReadTimeout"}])
    ex = Executor(_cfg(max_picks_per_window=1), client=client)

    result = _fire(ex)

    assert result["placed"] is False
    assert result["reason"] == "ORDER_UNCERTAIN"
    assert result["booked"] is True
    # The exposure is owned: ticker held, window budget consumed.
    assert "T-BTC" in ex.state.open_tickers
    assert ex.state.window_count.get(1) == 1
    assert ex.state.window_committed_cents.get(1, 0) > 0
    # ...so a second pick in the SAME window is refused, not double-sized.
    assert _fire(ex, ticker="T-ETH")["reason"] == "WINDOW_FULL"


def test_definite_rejection_does_not_book_exposure():
    """A clean 4xx IS proof the order did not land — it must not consume budget."""
    client = _LifecycleClient(place_results=[
        {"ok": False, "status": 400, "error": "insufficient balance"}])
    ex = Executor(_cfg(), client=client)

    result = _fire(ex)

    assert result["reason"] == "ORDER_FAILED"
    assert result.get("booked") is not True
    assert ex.state.open_tickers == frozenset()
    assert ex.state.window_count.get(1, 0) == 0


def test_connect_timeout_is_certain_not_sent():
    """ConnectTimeout means the socket never opened, so the order cannot have landed."""
    import requests

    class _Sess:
        def request(self, *a, **k):
            raise requests.exceptions.ConnectTimeout("no route")

    cli = KalshiTradingClient(_cfg(), signer=_StubSigner(), session=_Sess())
    res = cli._request("POST", "/portfolio/events/orders", {})

    assert res["ok"] is False and res["uncertain"] is False


def test_read_timeout_is_flagged_uncertain():
    import requests

    class _Sess:
        def request(self, *a, **k):
            raise requests.exceptions.ReadTimeout("ack never arrived")

    cli = KalshiTradingClient(_cfg(), signer=_StubSigner(), session=_Sess())
    res = cli._request("POST", "/portfolio/events/orders", {})

    assert res["ok"] is False and res["uncertain"] is True


def test_classify_fill_labels_uncertain_distinctly():
    """UNCERTAIN must stay distinct from FAILED so reconciliation can find these rows."""
    from q15_upgrade.executor.store import classify_fill, UNCERTAIN, FAILED

    assert classify_fill({"ok": False, "uncertain": True}, 10)[0] == UNCERTAIN
    assert classify_fill({"ok": False}, 10)[0] == FAILED


# --------------------------------------------------------------------------- #
# 2. Positions must survive a restart, or the defensive exit cannot fire.
# --------------------------------------------------------------------------- #

def test_positions_rehydrate_after_restart_so_exit_can_fire(tmp_path):
    """Before: a restart left positions={} and every defensive close returned
    NO_POSITION while a real position rode to settlement."""
    db = str(tmp_path / "orders.sqlite3")
    cfg = _cfg(record_orders=True, orders_db_path=db)
    first = _LifecycleClient(place_results=[
        {"ok": True, "dry_run": False,
         "data": {"order": {"order_id": "abc123", "status": "resting",
                            "fill_count": 0}}}])
    ex = Executor(cfg, client=first)
    assert _fire(ex)["placed"] is True
    assert ex.state.positions.get("T-BTC", 0) > 0

    # Process restart: a brand-new Executor against the SAME store.
    reborn = Executor(cfg, client=_LifecycleClient())

    assert reborn.state.positions.get("T-BTC", 0) > 0, "position lost across restart"
    assert "T-BTC" in reborn.state.open_tickers
    assert reborn.state.open_count == 1
    # The entry's order id came back too, so the exit can cancel it.
    assert reborn._entry_orders.get((1, "T-BTC")) == "abc123"

    out = reborn.on_exit("T-BTC", 1, 40)
    assert out["placed"] is True and out["count"] > 0


def test_dry_run_positions_do_not_rehydrate_into_a_live_book(tmp_path):
    """Flipping DRY_RUN off must not inherit simulated positions as real ones."""
    db = str(tmp_path / "orders.sqlite3")
    paper = Executor(_cfg(dry_run=True, record_orders=True, orders_db_path=db),
                     client=_LifecycleClient(dry_run=True))
    assert _fire(paper)["placed"] is True
    assert paper.state.positions.get("T-BTC", 0) > 0

    live = Executor(_cfg(dry_run=False, record_orders=True, orders_db_path=db),
                    client=_LifecycleClient())

    assert live.state.positions == {}, "dry-run position leaked into the live book"
    assert live.state.open_count == 0


def test_rehydrate_preserves_the_per_window_cap(tmp_path):
    """The pre-existing cap guarantee must not regress now that positions ride along."""
    db = str(tmp_path / "orders.sqlite3")
    cfg = _cfg(max_picks_per_window=1, record_orders=True, orders_db_path=db)
    ex = Executor(cfg, client=_LifecycleClient())
    assert _fire(ex)["placed"] is True

    reborn = Executor(cfg, client=_LifecycleClient())

    assert reborn.state.window_count.get(1) == 1
    assert _fire(reborn, ticker="T-ETH")["reason"] == "WINDOW_FULL"


# --------------------------------------------------------------------------- #
# 3. A defensive exit must CANCEL the resting GTC entry, not only sell.
# --------------------------------------------------------------------------- #

def test_exit_cancels_resting_entry_before_selling():
    """The entry is GTC. Selling alone leaves it working, so it fills into the very
    move the exit was escaping — while the bot books itself flat."""
    client = _LifecycleClient(place_results=[
        {"ok": True, "dry_run": False,
         "data": {"order": {"order_id": "resting-1", "status": "resting",
                            "fill_count": 0}}},
        {"ok": True, "dry_run": False, "data": {"order": {"order_id": "sell-1"}}}])
    ex = Executor(_cfg(), client=client)
    _fire(ex)

    out = ex.on_exit("T-BTC", 1, 40)

    assert client.cancelled == ["resting-1"], "resting entry was not cancelled"
    assert out["placed"] is True and out["cancel"]["ok"] is True
    assert client.placed[-1]["action"] == "sell"
    assert "T-BTC" not in ex.state.open_tickers


def test_exit_with_no_position_still_cancels_the_working_entry():
    """Even believing we hold nothing, a working GTC buy must come off the book."""
    client = _LifecycleClient()
    ex = Executor(_cfg(), client=client)
    ex._entry_orders[(7, "T-SOL")] = "ghost-9"

    out = ex.on_exit("T-SOL", 7, 40)

    assert client.cancelled == ["ghost-9"]
    assert out["placed"] is False
    assert out["reason"] == "NO_POSITION_ENTRY_CANCELLED"


def test_exit_proceeds_when_cancel_is_rejected():
    """A cancel that errors (typically: the entry already filled) must not block the sell."""
    client = _LifecycleClient(cancel_result={"ok": False, "error": "order already filled"})
    ex = Executor(_cfg(), client=client)
    _fire(ex)

    out = ex.on_exit("T-BTC", 1, 40)

    assert client.cancelled and out["placed"] is True
    assert client.placed[-1]["action"] == "sell"


def test_exit_survives_a_cancel_that_raises():
    """An exit is the last line of defence — a throwing cancel must not abort it."""
    class _Boom(_LifecycleClient):
        def cancel_order(self, order_id):
            raise RuntimeError("broker unreachable")

    client = _Boom()
    ex = Executor(_cfg(), client=client)
    _fire(ex)

    out = ex.on_exit("T-BTC", 1, 40)

    assert out["placed"] is True
    assert client.placed[-1]["action"] == "sell"


# --------------------------------------------------------------------------- #
# 4. The daily stop must be a DAY, and must survive a restart.
# --------------------------------------------------------------------------- #

def test_daily_stop_reference_survives_restart(tmp_path):
    """Hit the stop, bounce the process, and it must STILL hold — otherwise the
    $100/day cap is really a $100/restart cap."""
    db = str(tmp_path / "orders.sqlite3")
    cfg = _cfg(bankroll_cents=0, daily_loss_limit_cents=10_000,
               record_orders=True, orders_db_path=db)
    # Day opens at $500.
    ex = Executor(cfg, client=_LifecycleClient(balance=50_000))
    assert ex._day_start_balance == 50_000

    # Restart with the balance already down $100.
    reborn = Executor(cfg, client=_LifecycleClient(balance=40_000))

    assert reborn._day_start_balance == 50_000, "restart re-armed the daily stop"
    result = _fire(reborn)
    assert result["placed"] is False and result["reason"] == "DAILY_STOP"


def test_daily_stop_rolls_over_on_a_new_utc_day(tmp_path, monkeypatch):
    """A process up for days must not stay frozen on a loss that is no longer today's."""
    db = str(tmp_path / "orders.sqlite3")
    cfg = _cfg(bankroll_cents=0, daily_loss_limit_cents=10_000,
               record_orders=True, orders_db_path=db)
    ex = Executor(cfg, client=_LifecycleClient(balance=50_000))
    ex._day_date = "2026-07-30"          # the reference was set yesterday
    ex._day_start_balance = 50_000

    monkeypatch.setattr(executor_mod, "_utc_today", lambda: "2026-07-31")
    ex.client = _LifecycleClient(balance=40_000)   # $100 below YESTERDAY's open
    ex._refresh_daily_pnl()

    assert ex._day_date == "2026-07-31"
    assert ex._day_start_balance == 40_000         # re-based to today's opening balance
    assert ex.state.day_realized_pnl_cents == 0    # today's realized loss is zero
    assert _fire(ex)["placed"] is True             # so trading resumes


def test_daily_stop_still_fires_within_the_same_day(tmp_path):
    """Rollover must not weaken the stop while the day is unchanged."""
    db = str(tmp_path / "orders.sqlite3")
    cfg = _cfg(bankroll_cents=0, daily_loss_limit_cents=10_000,
               record_orders=True, orders_db_path=db)
    ex = Executor(cfg, client=_LifecycleClient(balance=50_000))
    ex.client = _LifecycleClient(balance=39_000)   # down $110 today

    assert _fire(ex)["reason"] == "DAILY_STOP"


# --------------------------------------------------------------------------- #
# 5. Size off the price we actually PAY, not the signalled ask.
# --------------------------------------------------------------------------- #

def test_count_is_sized_off_the_limit_not_the_ask():
    """With a positive offset the order posts above the ask, so sizing off the ask
    spent count*offset more than the stake — breaching the hard per-pick cap."""
    from q15_upgrade.executor.risk import Pick, PortfolioState, decide

    cfg = _cfg(flat_stake_cents=15_000, max_stake_per_pick_cents=15_000,
               limit_offset_cents=1, max_picks_per_window=1)
    state = PortfolioState(bankroll_cents=100_000)

    d = decide(Pick("T-BTC", "BTC", "NO", 60, 1, interval="10M"), state, cfg)

    assert d.limit_price_cents == 61
    assert d.count == 15_000 // 61          # 245, not 250
    # Worst-case cash out at the limit stays inside the $150 per-pick cap.
    assert d.count * d.limit_price_cents <= 15_000
    assert d.stake_cents == d.count * d.limit_price_cents


def test_zero_offset_sizing_is_unchanged():
    """The common case (offset 0) must be byte-identical to the old behaviour."""
    from q15_upgrade.executor.risk import Pick, PortfolioState, decide

    cfg = _cfg(flat_stake_cents=7500, limit_offset_cents=0)
    d = decide(Pick("T-BTC", "BTC", "NO", 65, 1), PortfolioState(bankroll_cents=100_000), cfg)

    assert d.limit_price_cents == 65
    assert d.count == 7500 // 65
    assert d.stake_cents == d.count * 65


# --------------------------------------------------------------------------- #
# 6. The kill switch must be a LIVE switch, not a boot-time constant.
# --------------------------------------------------------------------------- #

def test_kill_switch_takes_effect_without_a_restart(monkeypatch):
    """Documented as a panic button that 'hard-blocks ALL placement regardless' — it
    used to need a process restart because the singleton froze its config at boot."""
    from q15_upgrade.executor.executor import get_executor, reset_executor_for_tests

    reset_executor_for_tests()
    monkeypatch.setenv("Q15_EXEC_ENABLED", "true")
    monkeypatch.setenv("Q15_EXEC_DRY_RUN", "true")
    monkeypatch.delenv("Q15_EXEC_KILL", raising=False)
    monkeypatch.setenv("Q15_EXEC_BANKROLL_CENTS", "100000")

    ex = get_executor()
    assert ex is not None and ex.cfg.kill_switch is False

    monkeypatch.setenv("Q15_EXEC_KILL", "true")   # panic, mid-session
    same = get_executor()

    assert same is ex, "expected the same singleton, not a rebuild"
    assert same.cfg.kill_switch is True
    assert _fire(same)["reason"] == "KILL"
    reset_executor_for_tests()


def test_safety_refresh_never_widens_risk(monkeypatch):
    """Only kill_switch/dry_run refresh; sizing stays pinned to boot values so a
    half-edited environment cannot resize a running book."""
    from q15_upgrade.executor.executor import get_executor, reset_executor_for_tests

    reset_executor_for_tests()
    monkeypatch.setenv("Q15_EXEC_ENABLED", "true")
    monkeypatch.setenv("Q15_EXEC_DRY_RUN", "true")
    monkeypatch.setenv("Q15_EXEC_FLAT_STAKE_CENTS", "7500")
    monkeypatch.setenv("Q15_EXEC_BANKROLL_CENTS", "100000")
    ex = get_executor()
    assert ex.cfg.flat_stake_cents == 7500

    monkeypatch.setenv("Q15_EXEC_FLAT_STAKE_CENTS", "999999")
    get_executor()

    assert ex.cfg.flat_stake_cents == 7500, "sizing changed under a running book"
    reset_executor_for_tests()


def test_day_reference_is_per_book(tmp_path):
    """NO and YES books use separate stores, so their day references cannot collide."""
    common = dict(bankroll_cents=0, daily_loss_limit_cents=10_000, record_orders=True)
    no_ex = Executor(_cfg(orders_db_path=str(tmp_path / "no.sqlite3"), **common),
                     client=_LifecycleClient(balance=50_000))
    yes_ex = Executor(_cfg(orders_db_path=str(tmp_path / "yes.sqlite3"),
                           no_only=False, **common),
                      client=_LifecycleClient(balance=90_000))

    assert no_ex._day_start_balance == 50_000
    assert yes_ex._day_start_balance == 90_000
