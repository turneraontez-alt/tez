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
                flat_stake_cents=0, stake_ladder_cents=(), min_price_cents=50, max_price_cents=85,
                record_orders=False)  # order recording off by default in tests; its own tests enable it
    base.update(over)
    return ExecutorConfig(**base)


def _pick(ticker="T-BTC", asset="BTC", side="NO", price=65, wk=1,
          btc_lean=None, prior_breadth=None, stake_multiplier=1):
    return Pick(ticker=ticker, asset=asset, side=side, price_cents=price, window_key=wk,
                btc_lean=btc_lean, prior_breadth=prior_breadth, stake_multiplier=stake_multiplier)


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


def test_on_fire_reports_latency():
    """Observability: on_fire returns balance/order latency (ms) and the snapshot->order age, so
    the live fire->ack timing can be MEASURED rather than guessed. Deterministic (no real sleep)."""
    import time as _time
    class _RecClient:
        def get_balance_cents(self): return None
        def place_order(self, **kw): return {"ok": True, "dry_run": True}
    ex = Executor(_cfg(enabled=True, dry_run=True, bankroll_cents=100_000, flat_stake_cents=7500),
                  client=_RecClient())
    r = ex.on_fire({"ticker": "T-BTC", "asset": "BTC", "predicted_side": "NO",
                    "entry_ask_cents": 65, "window_key": 1, "fired_at": _time.time()})
    assert r["placed"] is True
    assert isinstance(r["order_latency_ms"], (int, float)) and r["order_latency_ms"] >= 0
    assert isinstance(r["balance_latency_ms"], (int, float)) and r["balance_latency_ms"] >= 0
    assert r["snapshot_age_ms"] is not None and r["snapshot_age_ms"] >= 0
    # no fired_at stamped -> snapshot_age is gracefully None (still places).
    r2 = ex.on_fire({"ticker": "T-ETH", "asset": "ETH", "predicted_side": "NO",
                     "entry_ask_cents": 65, "window_key": 2})
    assert r2["placed"] is True and r2["snapshot_age_ms"] is None


# --------------------------------------------------------------------------- #
# order/fill recording — classify_fill + ExecutorStore (answers "how many missed")
# --------------------------------------------------------------------------- #
def test_classify_fill_against_real_kalshi_shapes():
    from q15_upgrade.executor.store import classify_fill
    # fully filled (nested order, fill_count >= requested)
    assert classify_fill({"ok": True, "data": {"order": {"order_id": "o1", "status": "executed",
                          "fill_count": 100}}}, 100) == ("FILLED", 100, "o1")
    # partial
    assert classify_fill({"ok": True, "data": {"order": {"fill_count": 40}}}, 100)[0] == "PARTIAL"
    # rested = unfilled at placement (flat shape, fill_count 0, resting)
    assert classify_fill({"ok": True, "data": {"order_id": "o2", "status": "resting",
                          "fill_count": 0}}, 100) == ("RESTED", 0, "o2")
    # canceled with 0 fills
    assert classify_fill({"ok": True, "data": {"status": "canceled", "fill_count": 0}}, 5)[0] == "CANCELED"
    # http failure / dry-run / unrecognized
    assert classify_fill({"ok": False, "error": "boom"}, 5)[0] == "FAILED"
    assert classify_fill({"ok": True, "dry_run": True}, 5)[0] == "DRY_RUN"
    assert classify_fill({"ok": True, "data": {"weird": 1}}, 5)[0] == "UNKNOWN"


def test_executor_store_records_and_summarizes(tmp_path):
    from q15_upgrade.executor.store import ExecutorStore
    db = str(tmp_path / "orders.sqlite3")
    s = ExecutorStore(db)
    s.record(action="entry", ticker="A", fill_status="FILLED", stake_cents=9945, requested_count=153)
    s.record(action="entry", ticker="B", fill_status="RESTED", stake_cents=7475, requested_count=115)
    s.record(action="entry", ticker="C", fill_status="RESTED", stake_cents=7475, requested_count=115)
    summ = s.fill_summary()
    assert summ["total"] == 3
    assert summ["filled"] == 1 and summ["missed"] == 2     # 2 RESTED = missed at placement
    assert summ["fill_rate"] == 1 / 3                       # 1 filled of 3 live orders


def test_fill_summary_partitions_entry_vs_exit(tmp_path):
    """The preflight --fills diagnostic answers 'did a defensive SELL fire and fill?' by
    calling fill_summary(action='exit'). Verify the action filter partitions cleanly so the
    exit row count is never contaminated by entry buys (and vice-versa)."""
    from q15_upgrade.executor.store import ExecutorStore
    s = ExecutorStore(str(tmp_path / "orders.sqlite3"))
    s.record(action="entry", ticker="A", fill_status="FILLED", stake_cents=10000, requested_count=153)
    s.record(action="entry", ticker="B", fill_status="RESTED", stake_cents=7475, requested_count=115)
    s.record(action="exit",  ticker="A", fill_status="FILLED", stake_cents=0, requested_count=153)
    assert s.fill_summary()["total"] == 3                       # aggregate lumps both
    entries = s.fill_summary(action="entry")
    exits = s.fill_summary(action="exit")
    assert entries["total"] == 2 and entries["filled"] == 1 and entries["missed"] == 1
    assert exits["total"] == 1 and exits["filled"] == 1 and exits["missed"] == 0
    # a store with no exits yet must report exactly zero (the 'NO exit-sell orders' branch)
    empty = ExecutorStore(str(tmp_path / "empty.sqlite3"))
    empty.record(action="entry", ticker="Z", fill_status="FILLED", stake_cents=10000, requested_count=1)
    assert empty.fill_summary(action="exit")["total"] == 0


def test_executor_store_reconciles_final_orders_by_uuid_client_id(tmp_path):
    import sqlite3

    from q15_upgrade.executor.store import ExecutorStore
    from q15_upgrade.executor.trading_client import _coid_uuid

    raw_client_order_id = "v2x-1-T-BTC-entry"
    s = ExecutorStore(str(tmp_path / "orders.sqlite3"))
    s.record(
        action="entry",
        ticker="T-BTC",
        client_order_id=raw_client_order_id,
        requested_count=10,
        filled_count=0,
        fill_status="RESTED",
        stake_cents=600,
    )

    summary = s.reconcile_orders([
        {
            "order_id": "broker-order-1",
            "client_order_id": _coid_uuid(raw_client_order_id),
            "status": "executed",
            "fill_count": 10,
            "remaining_count": 0,
            "average_fill_price": "0.4000",
            "average_fee_paid": "0.0000",
        }
    ], now=123.0)

    assert summary == {"input": 1, "matched": 1, "updated": 1, "unmatched": 0}
    final = s.final_fill_summary(action="entry")
    assert final["total"] == 1
    assert final["filled"] == 1
    assert final["fill_rate"] == 1.0
    conn = sqlite3.connect(str(tmp_path / "orders.sqlite3"))
    try:
        row = conn.execute(
            "SELECT final_status, final_fill_status, final_filled_count, final_reconciled_at "
            "FROM executor_orders"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("executed", "FILLED", 10, 123.0)


def test_executor_exit_counterfactual_compares_exit_to_hold():
    from q15_upgrade.executor.reporting import reconstruct_order_pnl

    rows = [
        {
            "id": 1,
            "created_at": 1.0,
            "action": "entry",
            "ticker": "T-BTC",
            "client_order_id": "v2x-1-T-BTC-entry",
            "fill_status": "FILLED",
            "filled_count": 10,
            "response_json": '{"average_fill_price":"0.4000","average_fee_paid":"0.0000"}',
        },
        {
            "id": 2,
            "created_at": 2.0,
            "action": "exit",
            "ticker": "T-BTC",
            "client_order_id": "v2x-1-T-BTC-exit",
            "fill_status": "FILLED",
            "filled_count": 10,
            "response_json": '{"average_fill_price":"0.7500","average_fee_paid":"0.0000"}',
        },
    ]

    report = reconstruct_order_pnl(rows, {"T-BTC": "NO"})

    assert report["summary"]["exit_pnl_cents"] == -350.0
    assert report["summary"]["total_pnl_cents"] == -350.0
    closed = report["closed_trades"][0]
    assert closed["entry_price_cents"] == 60.0
    assert closed["exit_price_cents"] == 25.0
    assert closed["hold_pnl_cents"] == 400.0
    assert closed["exit_minus_hold_cents"] == -750.0


def test_on_fire_records_order_to_store(tmp_path):
    """End-to-end: a fire with recording on persists one row with a fill classification."""
    from q15_upgrade.executor.store import ExecutorStore
    db = str(tmp_path / "orders.sqlite3")
    class _RecClient:
        def get_balance_cents(self): return None
        def place_order(self, **kw):
            return {"ok": True, "dry_run": False, "data": {"order": {"order_id": "z9",
                    "status": "resting", "fill_count": 0}}}   # a MISS (rested, unfilled)
    ex = Executor(_cfg(enabled=True, dry_run=False, bankroll_cents=100_000, flat_stake_cents=7500,
                       record_orders=True, orders_db_path=db), client=_RecClient())
    r = ex.on_fire({"ticker": "T-BTC", "asset": "BTC", "predicted_side": "NO",
                    "entry_ask_cents": 65, "window_key": 1, "interval": "10M"})
    assert r["placed"] is True and r["fill_status"] == "RESTED"
    summ = ExecutorStore(db).fill_summary()
    assert summ["total"] == 1 and summ["missed"] == 1       # the miss was recorded


def test_decide_no_daily_stop_when_both_limits_zero():
    """Owner-chosen: both limits 0 -> the daily stop never fires, even after a huge loss."""
    cfg = _cfg(daily_loss_limit_cents=0, daily_loss_limit_pct=0)
    st = PortfolioState(bankroll_cents=100_000, day_start_bankroll_cents=100_000,
                        day_realized_pnl_cents=-999_999)   # would trip ANY stop
    assert decide(_pick(price=65), st, cfg).place is True


def test_stop_disabled_skips_balance_read():
    """With the stop off, on_fire does NOT read the account balance (it only fed the stop) — so the
    bot is decoupled from the shared account and a manual trade can't pause it."""
    calls = {"n": 0}
    class _C:
        def get_balance_cents(self): calls["n"] += 1; return 50_000
        def place_order(self, **kw): return {"ok": True, "dry_run": False}
    ex = Executor(_cfg(enabled=True, dry_run=False, bankroll_cents=100_000, flat_stake_cents=7500,
                       daily_loss_limit_cents=0, daily_loss_limit_pct=0), client=_C())
    calls["n"] = 0   # ignore the init read; count only on_fire
    from dataclasses import replace
    ex.state = replace(ex.state, day_realized_pnl_cents=-1_000_000)   # would trip a stop if any existed
    r = ex.on_fire({"ticker": "T-BTC", "asset": "BTC", "predicted_side": "NO",
                    "entry_ask_cents": 65, "window_key": 1})
    assert r["placed"] is True and calls["n"] == 0


def test_safety_summary_shows_no_stop_when_disabled():
    assert "NO daily stop" in _cfg(daily_loss_limit_cents=0, daily_loss_limit_pct=0).safety_summary()


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


def test_prune_settled_releases_old_windows():
    from q15_upgrade.executor.risk import prune_settled
    st = PortfolioState(bankroll_cents=100_000, open_count=2,
                        open_tickers=frozenset({"T-BTC", "T-ETH"}),
                        positions={"T-BTC": 10, "T-ETH": 10},
                        window_count={5: 1, 6: 1}, window_committed_cents={5: 7500, 6: 7500},
                        window_tickers={5: frozenset({"T-BTC"}), 6: frozenset({"T-ETH"})})
    # a fire arrives for window 7 -> windows 5 and 6 have settled -> both released.
    pruned = prune_settled(st, 7)
    assert pruned.open_count == 0 and pruned.open_tickers == frozenset()
    assert pruned.window_tickers == {}
    # a fire for window 6 keeps window 6 (still open), releases only the older window 5.
    p2 = prune_settled(st, 6)
    assert p2.open_count == 1 and p2.open_tickers == frozenset({"T-ETH"})


def test_bot_keeps_trading_after_windows_settle():
    """Regression for the live halt: without pruning, optimistic apply_fill made open_count climb to
    MAX_OPEN and every ticker hit DUP_TICKER, silently stopping the bot after ~6 entries. With
    per-window pruning, each new settlement window releases the prior (settled) positions, so the bot
    trades INDEFINITELY — way past max_open_positions — instead of pinning at the cap."""
    cfg = _cfg(flat_stake_cents=7500, max_open_positions=6, max_picks_per_window=1)
    class _Rec:
        def get_balance_cents(self): return None
        def place_order(self, **kw): return {"ok": True, "dry_run": True}
    ex = Executor(cfg, client=_Rec())
    assets = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE"]
    # 14 entries across 14 increasing windows -> well past the old cap of 6; every one must place.
    for i in range(14):
        a = assets[i % len(assets)]
        r = ex.on_fire({"ticker": f"T-{a}", "asset": a, "predicted_side": "NO",
                        "entry_ask_cents": 65, "window_key": 1000 + i})
        assert r["placed"] is True and r.get("reason") != "MAX_OPEN", \
            f"entry {i} (window {1000+i}) was blocked: {r.get('reason')}"
    # open exposure reflects only the CURRENT window, never the lifetime total.
    assert ex.state.open_count == 1
    # a ticker that traded in an OLD window trades again now (the stale DUP_TICKER is gone).
    r = ex.on_fire({"ticker": "T-BTC", "asset": "BTC", "predicted_side": "NO",
                    "entry_ask_cents": 65, "window_key": 2000})
    assert r["placed"] is True


def test_same_window_caps_still_apply():
    """Pruning must NOT loosen the WITHIN-window caps: concurrent positions in the SAME (current)
    window still count toward MAX_OPEN and the per-window pick cap."""
    cfg = _cfg(flat_stake_cents=7500, max_open_positions=2, max_picks_per_window=5)
    class _Rec:
        def get_balance_cents(self): return None
        def place_order(self, **kw): return {"ok": True, "dry_run": True}
    ex = Executor(cfg, client=_Rec())
    assert ex.on_fire({"ticker": "T-A", "asset": "A", "predicted_side": "NO",
                       "entry_ask_cents": 65, "window_key": 9})["placed"] is True
    assert ex.on_fire({"ticker": "T-B", "asset": "B", "predicted_side": "NO",
                       "entry_ask_cents": 65, "window_key": 9})["placed"] is True
    # third concurrent position in the SAME window 9 -> MAX_OPEN (2) still binds.
    third = ex.on_fire({"ticker": "T-C", "asset": "C", "predicted_side": "NO",
                        "entry_ask_cents": 65, "window_key": 9})
    assert third["placed"] is False and third["reason"] == "MAX_OPEN"


def test_bot_trades_independently_of_external_manual_positions():
    """The bot can take a trade even if the OWNER manually holds a position on that ticker. The
    DUP_TICKER / MAX_OPEN / WINDOW caps are scoped to the bot's OWN in-memory state (open_tickers /
    open_count / window_count), which ONLY the executor's own fills populate — a manual position is
    never synced here, so it cannot block a bot entry. (The shared-account DAILY STOP is the only
    place a manual trade interacts; tracked separately.)"""
    cfg = _cfg(flat_stake_cents=7500)
    # bot holds nothing of its own -> places, regardless of any manual position the owner holds.
    st = PortfolioState(bankroll_cents=100_000)
    assert decide(_pick(ticker="T-BTC"), st, cfg).place is True
    # the cap fires ONLY on the bot's OWN recorded position, never an external one.
    st_own = PortfolioState(bankroll_cents=100_000, open_tickers=frozenset({"T-BTC"}))
    assert decide(_pick(ticker="T-BTC"), st_own, cfg).reason == "DUP_TICKER"


def test_decide_dup_ticker_blocks():
    cfg = _cfg()
    st = PortfolioState(bankroll_cents=100_000, open_tickers=frozenset({"T-BTC"}))
    assert decide(_pick(ticker="T-BTC"), st, cfg).reason == "DUP_TICKER"


def test_decide_window_full_blocks():
    cfg = _cfg(max_picks_per_window=2)
    st = PortfolioState(bankroll_cents=100_000, window_count={1: 2})
    assert decide(_pick(wk=1), st, cfg).reason == "WINDOW_FULL"


def test_second_pick_floor_default_off_is_byte_identical():
    # second_pick_min_ask defaults to 0 -> the gate `price >= 0` is always true, so a 2nd pick
    # places regardless of its ask (when max_picks_per_window>=2). One pick already in window 1.
    cfg = _cfg(max_picks_per_window=2)  # second_pick_min_ask unset -> 0
    assert cfg.second_pick_min_ask == 0
    st = PortfolioState(bankroll_cents=100_000, window_count={1: 1},
                        window_committed_cents={1: 4000})
    d = decide(_pick(wk=1, price=50), st, cfg)  # cheap 2nd pick still allowed
    assert d.place is True


def test_second_pick_floor_blocks_cheap_second_pick():
    # With the floor at 60c: a 2nd pick below 60 is refused SECOND_PICK_ASK_FLOOR, but the FIRST
    # pick of a window at that same low ask still places (the floor only gates picks beyond the 1st).
    cfg = _cfg(max_picks_per_window=2, second_pick_min_ask=60)
    # first pick of an empty window at ask 55 -> places (not gated)
    st_first = PortfolioState(bankroll_cents=100_000)
    assert decide(_pick(wk=1, price=55), st_first, cfg).place is True
    # second pick of the same window at ask 55 -> refused by the floor
    st_second = PortfolioState(bankroll_cents=100_000, window_count={1: 1},
                               window_committed_cents={1: 4000})
    assert decide(_pick(wk=1, price=55), st_second, cfg).reason == "SECOND_PICK_ASK_FLOOR"
    # second pick at/above the floor -> places
    assert decide(_pick(wk=1, price=60), st_second, cfg).place is True


def test_second_pick_floor_does_not_loosen_window_cap():
    # max_picks_per_window=1 still blocks the 2nd with WINDOW_FULL even if it clears the ask floor —
    # the new gate is checked AFTER WINDOW_FULL and never overrides the hard cap.
    cfg = _cfg(max_picks_per_window=1, second_pick_min_ask=60)
    st = PortfolioState(bankroll_cents=100_000, window_count={1: 1})
    assert decide(_pick(wk=1, price=70), st, cfg).reason == "WINDOW_FULL"


def test_entry_ask_floor_default_off_is_byte_identical():
    # min_entry_ask defaults to 0 -> the gate `price >= 0` is always true, so even a 52c FIRST pick
    # places. Default-OFF must be byte-identical to having no floor.
    cfg = _cfg()  # min_entry_ask unset -> 0
    assert cfg.min_entry_ask == 0
    st = PortfolioState(bankroll_cents=100_000)
    d = decide(_pick(wk=1, price=52), st, cfg)  # cheap first pick still placed
    assert d.place is True


def test_entry_ask_floor_blocks_cheap_first_pick():
    # With the GLOBAL floor at 58c: a pick below 58 is refused ENTRY_ASK_FLOOR, while a pick at the
    # floor (and above) places. Unlike second_pick_min_ask, this gates the FIRST pick of a window.
    cfg = _cfg(min_entry_ask=58)
    st = PortfolioState(bankroll_cents=100_000)
    assert decide(_pick(wk=1, price=55), st, cfg).reason == "ENTRY_ASK_FLOOR"
    assert decide(_pick(wk=1, price=57), st, cfg).reason == "ENTRY_ASK_FLOOR"
    assert decide(_pick(wk=1, price=58), st, cfg).place is True   # at the floor -> places
    assert decide(_pick(wk=1, price=65), st, cfg).place is True   # above the floor -> places


def test_entry_ask_floor_applies_to_first_pick_unlike_second_pick_floor():
    # Distinguishes the global floor from second_pick_min_ask: with NO second-pick floor set but a
    # global floor of 58, the very FIRST pick of an empty window below 58 is refused — proving the
    # global floor is not the 2nd-pick-only gate.
    cfg = _cfg(min_entry_ask=58, second_pick_min_ask=0)
    st = PortfolioState(bankroll_cents=100_000)  # empty window, this is the 1st pick
    assert st.window_count.get(1, 0) == 0
    assert decide(_pick(wk=1, price=55), st, cfg).reason == "ENTRY_ASK_FLOOR"


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
# Owner default sizing: per-window LADDER $275 (1st/best pick) / $150 (2nd), 2 picks/window
# --------------------------------------------------------------------------- #
def test_owner_default_executor_sizing_is_ladder_275_150_two_picks():
    cfg = ExecutorConfig()      # production defaults
    assert cfg.stake_ladder_cents == (27500, 15000)  # $275 on the 1st/best pick, $150 on the 2nd
    assert cfg.max_picks_per_window == 2             # up to two picks per settlement window
    # flat / per-pick-cap stay as the documented FALLBACK used only when the ladder is disabled.
    assert cfg.flat_stake_cents == 7500
    assert cfg.max_stake_per_pick_cents == 7500


def test_flat_stake_overrides_pct_sizing():
    # flat $75 ignores the 4% (=$40 on $1000) — stakes the fixed amount instead.
    cfg = _cfg(flat_stake_cents=7500)
    st = PortfolioState(bankroll_cents=100_000)
    d = decide(_pick(price=65), st, cfg)
    assert d.place is True
    assert d.stake_cents == (7500 // 65) * 65     # $75 worth of whole contracts
    assert d.count == 7500 // 65 == 115


def test_stake_by_interval_default_empty_is_flat():
    """Rec #7: default empty stake_by_interval -> every interval stakes flat $75 (byte-identical)."""
    cfg = ExecutorConfig()
    assert cfg.stake_by_interval == {}
    cfg2 = _cfg(flat_stake_cents=7500)
    st = PortfolioState(bankroll_cents=100_000)
    d = decide(Pick("T-BTC", "BTC", "NO", 65, 1, interval="10M"), st, cfg2)
    assert d.count == 7500 // 65 == 115            # flat $75 regardless of interval


def test_stake_by_interval_upsizes_10m_only():
    """Rec #7: 10M=$100 override stakes ~$100 on 10M (above the $75 flat AND the $75 per-pick cap)
    while 7M stays at flat $75. Concentrates capital on the proven +EV 10M engine."""
    cfg = _cfg(flat_stake_cents=7500, max_stake_per_pick_cents=7500, max_picks_per_window=1,
               stake_by_interval={"10M": 10_000})
    st = PortfolioState(bankroll_cents=100_000)
    d10 = decide(Pick("T-BTC", "BTC", "NO", 65, 1, interval="10M"), st, cfg)
    assert d10.place is True and d10.stake_cents == (10_000 // 65) * 65   # ~$100 of whole contracts
    assert d10.count == 10_000 // 65 == 153                              # above the $75 cap
    d7 = decide(Pick("T-ETH", "ETH", "NO", 65, 2, interval="7M"), st, cfg)
    assert d7.count == 7500 // 65 == 115                                  # 7M unchanged at flat $75


def test_stake_by_interval_still_gated_by_daily_stop():
    """Rec #7: a bigger 10M stake cannot bypass the $100 circuit breaker — the daily stop is
    checked BEFORE sizing, so a -$100 day refuses the $100 10M entry too."""
    cfg = _cfg(flat_stake_cents=7500, daily_loss_limit_cents=10_000, stake_by_interval={"10M": 10_000})
    st = PortfolioState(bankroll_cents=34_000, day_start_bankroll_cents=34_000,
                        day_realized_pnl_cents=-10_000)        # exactly -$100
    assert decide(Pick("T-BTC", "BTC", "NO", 65, 1, interval="10M"), st, cfg).reason == "DAILY_STOP"


def test_stake_by_interval_parses_env(monkeypatch):
    monkeypatch.setenv("Q15_EXEC_STAKE_BY_INTERVAL", "10m:10000, 7M:5000 ,bad,x:y")
    assert ExecutorConfig().stake_by_interval == {"10M": 10_000, "7M": 5_000}


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


def test_window_cap_survives_restart(tmp_path):
    """Durability fix: the per-window cap must survive a process restart. After 2 entries in a
    window, a fresh Executor sharing the same orders store rehydrates the count and still refuses a
    3rd — a restart can no longer reset window_count to 0 and over-place (the 4-in-one-window bug).
    Applies to BOTH books (the fix lives in the shared Executor.__init__)."""
    db = str(tmp_path / "orders.sqlite3")
    over = dict(record_orders=True, orders_db_path=db, max_picks_per_window=2)
    ex = Executor(_cfg(**over), client=_StubClient())
    for tk in ("T-ETH-A", "T-ETH-B"):
        assert ex.on_fire({"ticker": tk, "asset": "ETH", "predicted_side": "NO",
                           "entry_price_cents": 70, "window_key": 42})["placed"] is True
    assert ex.state.window_count.get(42) == 2
    # Simulate a RESTART: a brand-new Executor pointed at the SAME durable orders store.
    ex2 = Executor(_cfg(**over), client=_StubClient())
    assert ex2.state.window_count.get(42) == 2                       # rehydrated from the store
    assert "T-ETH-A" in ex2.state.window_tickers.get(42, frozenset())
    r = ex2.on_fire({"ticker": "T-ETH-C", "asset": "ETH", "predicted_side": "NO",
                     "entry_price_cents": 70, "window_key": 42})
    assert r["placed"] is False and r["reason"] == "WINDOW_FULL"     # cap survived the restart
    # a DIFFERENT (later) window is unaffected:
    assert ex2.on_fire({"ticker": "T-ETH-D", "asset": "ETH", "predicted_side": "NO",
                        "entry_price_cents": 70, "window_key": 43})["placed"] is True


def test_window_cap_rehydrate_noop_without_store():
    """When order recording is off (store is None) rehydrate is a no-op — byte-identical to before."""
    ex = Executor(_cfg(record_orders=False), client=_StubClient())
    assert ex.state.window_count == {} and ex.state.window_tickers == {}


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


# --------------------------------------------------------------------------- #
# LIVE "YES BOT" — the separate, isolated YES executor (config + gates + side)
# --------------------------------------------------------------------------- #
from q15_upgrade.executor.config import yes_config_from_env  # noqa: E402
from q15_upgrade.executor.executor import get_yes_executor    # noqa: E402


def _yescfg(**over):
    """Config mirroring yes_config_from_env's defaults, but constructed directly (no env) so the
    YES gates are exercised deterministically. flat $150/pick, 2/window, band 50-99, the 3 gates on."""
    base = dict(enabled=True, dry_run=True, bankroll_cents=100_000, no_only=False,
                flat_stake_cents=15000, max_stake_per_pick_cents=15000, max_picks_per_window=2,
                stake_ladder_cents=(),
                conviction_sizing=False, daily_loss_limit_cents=0, daily_loss_limit_pct=0.0,
                max_open_positions=6, min_price_cents=50, max_price_cents=99,
                limit_offset_cents=1, allowed_intervals=frozenset({"10M"}), btc_gate_enabled=False,
                min_yes_prob=0.55, min_btc_lean=0.55, excluded_assets=frozenset({"BNB"}),
                record_orders=False)
    base.update(over)
    return ExecutorConfig(**base)


def _yespick(ticker="T-SOL", asset="SOL", price=60, wk=1, yes_prob=0.60, btc_lean=0.60, interval="10M"):
    return Pick(ticker=ticker, asset=asset, side="YES", price_cents=price, window_key=wk,
                interval=interval, yes_prob=yes_prob, btc_lean=btc_lean)


def test_yes_fields_inert_by_default_on_no_config():
    """The 3 new ExecutorConfig fields default INERT so the NO executor is byte-identical."""
    c = ExecutorConfig()
    assert c.min_yes_prob == 0.0 and c.min_btc_lean == 0.0 and c.excluded_assets == frozenset()
    assert c.no_only is True


def test_no_decide_byte_identical_with_inert_yes_fields():
    """A NO pick under a default cfg must NOT trip any of the new YES gates, and existing
    reason codes are unchanged. In particular a NO pick on BNB / with yes_prob unset is fine."""
    cfg = _cfg()  # NO config: min_yes_prob=0, min_btc_lean=0, excluded_assets empty
    st = PortfolioState(bankroll_cents=100_000)
    assert decide(_pick(price=65), st, cfg).reason == "OK"
    assert decide(_pick(price=90), st, cfg).reason == "PRICE_BAND"      # above band, unchanged
    # NO on BNB with no yes_prob/btc_lean -> NOT ASSET_EXCLUDED / YES_PROB_FLOOR / BTC_LEAN_FLOOR:
    d = decide(_pick(asset="BNB", price=65), st, cfg)
    assert d.reason == "OK" and d.place is True


def test_yes_gate_admits_qualifying_pick():
    assert decide(_yespick(), PortfolioState(bankroll_cents=100_000), _yescfg()).reason == "OK"


def test_yes_gate_pyes_floor_blocks_low_and_missing():
    st = PortfolioState(bankroll_cents=100_000)
    assert decide(_yespick(yes_prob=0.50), st, _yescfg()).reason == "YES_PROB_FLOOR"
    assert decide(_yespick(yes_prob=None), st, _yescfg()).reason == "YES_PROB_FLOOR"  # fail-closed


def test_yes_gate_btc_lean_floor_blocks_bearish_and_missing():
    st = PortfolioState(bankroll_cents=100_000)
    assert decide(_yespick(btc_lean=0.50), st, _yescfg()).reason == "BTC_LEAN_FLOOR"
    assert decide(_yespick(btc_lean=None), st, _yescfg()).reason == "BTC_LEAN_FLOOR"  # fail-closed


def test_yes_gate_excludes_bnb():
    st = PortfolioState(bankroll_cents=100_000)
    assert decide(_yespick(asset="BNB", ticker="T-BNB"), st, _yescfg()).reason == "ASSET_EXCLUDED"


def test_yes_gate_keeps_high_favorite_in_band():
    """The 96% set ran 56-97c; the YES ask is the BUY price, so a 96c favourite must clear the
    50-99 band (a 50-85 band would have silently dropped the winners)."""
    d = decide(_yespick(price=96, yes_prob=0.95), PortfolioState(bankroll_cents=100_000), _yescfg())
    assert d.place is True and d.reason == "OK"
    assert d.limit_price_cents == 97  # ask 96 + 1 offset, inside band


def test_yes_executor_places_side_yes_with_distinct_coid():
    ex = Executor(_yescfg(), client=_StubClient())
    r = ex.on_fire({"ticker": "T-SOL", "asset": "SOL", "predicted_side": "YES",
                    "entry_ask_cents": 60, "window_key": 1, "interval": "10M",
                    "yes_prob": 0.60, "btc_lean": 0.60})
    assert r["placed"] is True
    o = ex.client.orders[0]
    assert o["side"] == "yes" and o["action"] == "buy"
    assert o["client_order_id"].startswith("v2xy-")          # distinct from the NO "v2x-" prefix
    # and the NO executor still uses the plain "v2x-" prefix (no collision at the exchange):
    no_ex = _exec()
    no_ex.on_fire({"ticker": "T-SOL", "asset": "SOL", "predicted_side": "NO",
                   "entry_price_cents": 60, "window_key": 1})
    no_coid = no_ex.client.orders[0]["client_order_id"]
    assert no_coid.startswith("v2x-") and not no_coid.startswith("v2xy-")


def test_yes_executor_isolated_from_no_state():
    """A YES on_fire must NOT mutate the NO executor's portfolio (separate instances/state)."""
    no_ex = _exec()
    yes_ex = Executor(_yescfg(), client=_StubClient())
    yes_ex.on_fire({"ticker": "T-SOL", "asset": "SOL", "predicted_side": "YES",
                    "entry_ask_cents": 60, "window_key": 1, "interval": "10M",
                    "yes_prob": 0.60, "btc_lean": 0.60})
    assert yes_ex.state.open_count == 1
    assert no_ex.state.open_count == 0 and no_ex.state.open_tickers == frozenset()


def test_yes_dry_run_does_not_inherit_no_live_flag(monkeypatch):
    """SAFETY (audit fix #1): the YES bot's dry_run is read ONLY from Q15_EXEC_YES_DRY_RUN — it
    must default TRUE even when the NO book's Q15_EXEC_DRY_RUN is 'false'."""
    monkeypatch.setenv("Q15_EXEC_DRY_RUN", "false")
    monkeypatch.delenv("Q15_EXEC_YES_DRY_RUN", raising=False)
    assert yes_config_from_env().dry_run is True


def test_get_yes_executor_none_when_disabled(monkeypatch):
    monkeypatch.delenv("Q15_EXEC_YES_ENABLED", raising=False)
    executor_mod.reset_executor_for_tests()
    assert get_yes_executor() is None


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


# --------------------------------------------------------------------------- #
# Defensive-exit order construction. The live bug: a reduce_only close was sent
# good-till-canceled, which Kalshi rejects 400 ("reduce_only can only be used with
# IoC orders") — so 100% of defensive sells failed and no position ever closed.
# --------------------------------------------------------------------------- #
def test_reduce_only_sell_is_ioc_buy_stays_gtc():
    cli = KalshiTradingClient(_cfg(dry_run=True), signer=_StubSigner(), session=_BoomSession())
    buy = cli.place_order(ticker="T-BTC", side="no", count=10, price_cents=65,
                          action="buy", client_order_id="v2x-1-T-BTC-entry")["would_place"]
    assert buy["reduce_only"] is False
    assert buy["time_in_force"] == "good_till_canceled"
    sell = cli.place_order(ticker="T-BTC", side="no", count=10, price_cents=30,
                           action="sell", client_order_id="v2x-1-T-BTC-exit")["would_place"]
    # Kalshi requires reduce_only orders be immediate-or-cancel; GTC reduce_only is rejected 400.
    assert sell["reduce_only"] is True
    assert sell["time_in_force"] == "immediate_or_cancel"


def test_exit_prices_under_fair_value_to_cross_the_bid():
    ex = _exec(exit_limit_offset_cents=3)
    ex.on_fire({"ticker": "T-BTC", "asset": "BTC", "predicted_side": "NO",
                "entry_price_cents": 60, "window_key": 1})
    r = ex.on_exit("T-BTC", 1, 30)                 # estimated exit value 30c
    assert r["placed"] is True
    last = ex.client.orders[-1]
    assert last["action"] == "sell"
    assert last["price_cents"] == 27               # 30 - 3 offset -> crosses the resting bid


def test_exit_offset_clamps_to_one_cent():
    ex = _exec(exit_limit_offset_cents=50)
    ex.on_fire({"ticker": "T-BTC", "asset": "BTC", "predicted_side": "NO",
                "entry_price_cents": 60, "window_key": 1})
    ex.on_exit("T-BTC", 1, 2)                       # 2 - 50 -> clamp to 1, never 0/negative
    assert ex.client.orders[-1]["price_cents"] == 1


def test_exit_offset_zero_keeps_fair_value():
    ex = _exec(exit_limit_offset_cents=0)
    ex.on_fire({"ticker": "T-BTC", "asset": "BTC", "predicted_side": "NO",
                "entry_price_cents": 60, "window_key": 1})
    ex.on_exit("T-BTC", 1, 30)
    assert ex.client.orders[-1]["price_cents"] == 30


def test_exit_limit_offset_default_is_three(monkeypatch):
    monkeypatch.delenv("Q15_EXEC_EXIT_LIMIT_OFFSET_CENTS", raising=False)
    assert ExecutorConfig().exit_limit_offset_cents == 3
    monkeypatch.setenv("Q15_EXEC_EXIT_LIMIT_OFFSET_CENTS", "5")
    assert ExecutorConfig().exit_limit_offset_cents == 5


# --------------------------------------------------------------------------- #
# BTC cross-asset gate — suppress alt-NO when BTC is bullish / complex risk-on,
# EXCEPT >=3-co-trigger 10M conviction windows (stake_multiplier>1). Owner LIVE.
# --------------------------------------------------------------------------- #
def _st(bankroll=100_000):
    return PortfolioState(bankroll_cents=bankroll)


def test_btc_gate_suppresses_on_bullish_lean():
    d = decide(_pick(price=65, btc_lean=0.60), _st(), _cfg())
    assert d.place is False and d.reason == "BTC_GATE"


def test_btc_gate_suppresses_on_risk_on_breadth():
    # breadth alone trips it even when BTC's own lean is unavailable
    d = decide(_pick(price=65, btc_lean=None, prior_breadth=0.60), _st(), _cfg())
    assert d.place is False and d.reason == "BTC_GATE"


def test_btc_gate_keeps_when_bearish():
    d = decide(_pick(price=65, btc_lean=0.30, prior_breadth=0.20), _st(), _cfg())
    assert d.place is True


def test_btc_gate_inert_without_signal():
    # no BTC lean and no breadth -> the gate cannot act, entry proceeds
    d = decide(_pick(price=65, btc_lean=None, prior_breadth=None), _st(), _cfg())
    assert d.place is True


def test_btc_gate_exempts_10m_conviction():
    # a >=3-co-trigger 10M conviction pick (stake_multiplier>1) runs even when BTC is very bullish
    d = decide(_pick(price=65, btc_lean=0.95, prior_breadth=0.95, stake_multiplier=2), _st(), _cfg())
    assert d.place is True


def test_btc_gate_disabled_passes_through():
    d = decide(_pick(price=65, btc_lean=0.95), _st(), _cfg(btc_gate_enabled=False))
    assert d.place is True


def test_btc_gate_threshold_is_configurable():
    # raise the lean bar above the signal -> no longer gates
    assert decide(_pick(price=65, btc_lean=0.55), _st(), _cfg()).reason == "BTC_GATE"
    assert decide(_pick(price=65, btc_lean=0.55), _st(), _cfg(btc_gate_lean=0.60)).place is True


def test_btc_gate_config_defaults(monkeypatch):
    for k in ("Q15_EXEC_BTC_GATE", "Q15_EXEC_BTC_GATE_LEAN", "Q15_EXEC_BTC_GATE_BREADTH"):
        monkeypatch.delenv(k, raising=False)
    cfg = ExecutorConfig()
    assert cfg.btc_gate_enabled is True          # owner: live, default ON
    assert cfg.btc_gate_lean == 0.5 and cfg.btc_gate_breadth == 0.5
    monkeypatch.setenv("Q15_EXEC_BTC_GATE", "false")
    assert ExecutorConfig().btc_gate_enabled is False   # kill switch
