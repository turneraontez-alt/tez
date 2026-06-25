"""Ultoim V2 EXECUTOR — risk manager (PURE, no I/O, fully unit-testable).

``decide(pick, state, cfg)`` is the ONE place that turns a fire signal + the current
portfolio snapshot into an order size — or a refusal. It is a pure function so every
guard is deterministically testable. All money is INTEGER CENTS (no float drift).

Guards (a refusal short-circuits with a single reason code):
  KILL            — kill switch on
  WRONG_SIDE      — not the NO side (no_only)
  INTERVAL_BLOCKED— interval not in the (optional) allowlist
  PRICE_BAND      — price outside [min,max]
  DAILY_STOP      — day realized P&L past the loss limit (circuit breaker)
  MAX_OPEN        — already at max open positions
  DUP_TICKER      — already hold / already traded this ticker+window
  WINDOW_FULL     — already placed max picks for this window
  BANKROLL        — no bankroll to size against
  SIZE_TOO_SMALL  — computed count < 1 contract after the per-window cap
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Pick:
    """One fire signal handed to the executor (from a v2 delivered NO pick)."""
    ticker: str
    asset: str
    side: str            # "NO" / "YES"
    price_cents: int     # the signalled ask (what we'd pay per contract)
    window_key: int
    interval: str = ""   # "10M" / "7M" / ... — informational; gates the allowlist, NOT the coid


@dataclass(frozen=True)
class PortfolioState:
    """Snapshot the executor maintains; passed to decide() verbatim. Cents."""
    bankroll_cents: int = 0
    day_start_bankroll_cents: int = 0
    day_realized_pnl_cents: int = 0
    open_count: int = 0
    open_tickers: frozenset[str] = field(default_factory=frozenset)
    positions: dict[str, int] = field(default_factory=dict)               # ticker -> contracts held
    # per-window bookkeeping for the current (and recent) windows
    window_count: dict[int, int] = field(default_factory=dict)            # window_key -> picks placed
    window_committed_cents: dict[int, int] = field(default_factory=dict)  # window_key -> cents staked
    window_tickers: dict[int, frozenset[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    place: bool
    reason: str
    count: int = 0
    limit_price_cents: int = 0
    stake_cents: int = 0


def decide(pick: Pick, state: PortfolioState, cfg) -> Decision:
    side = (pick.side or "").upper()

    if cfg.kill_switch:
        return Decision(False, "KILL")
    if cfg.no_only and side != "NO":
        return Decision(False, "WRONG_SIDE")
    # Interval allowlist (default empty = allow-all -> this guard is inert). When set, refuse a
    # fire whose interval is not listed — a backstop against a structurally -EV 15M/12M order.
    allowed = getattr(cfg, "allowed_intervals", frozenset())
    if allowed and (pick.interval or "").upper() not in allowed:
        return Decision(False, "INTERVAL_BLOCKED")
    price = int(pick.price_cents)
    if not (cfg.min_price_cents <= price <= cfg.max_price_cents):
        return Decision(False, "PRICE_BAND")

    # Daily circuit breaker: stop NEW entries once down the limit on the day. An ABSOLUTE
    # dollar stop (daily_loss_limit_cents) GOVERNS when set; otherwise the % of day-start
    # bankroll. Exits are never gated by this (closing risk is always allowed).
    abs_cap = int(getattr(cfg, "daily_loss_limit_cents", 0) or 0)
    floor: int | None = None
    if abs_cap > 0:
        floor = -abs_cap
    elif cfg.daily_loss_limit_pct > 0 and state.day_start_bankroll_cents > 0:
        floor = -int(round(cfg.daily_loss_limit_pct * state.day_start_bankroll_cents))
    if floor is not None and state.day_realized_pnl_cents <= floor:
        return Decision(False, "DAILY_STOP")

    if state.open_count >= cfg.max_open_positions:
        return Decision(False, "MAX_OPEN")

    wk = int(pick.window_key)
    if pick.ticker in state.open_tickers or pick.ticker in state.window_tickers.get(wk, frozenset()):
        return Decision(False, "DUP_TICKER")
    if state.window_count.get(wk, 0) >= cfg.max_picks_per_window:
        return Decision(False, "WINDOW_FULL")

    bankroll = int(state.bankroll_cents)
    if bankroll <= 0:
        return Decision(False, "BANKROLL")

    # Per-pick stake. An INTERVAL override (stake_by_interval) wins first: that exact amount is the
    # stake AND the per-pick ceiling for this interval (it supersedes both flat and the hard cap),
    # so a 10M pick can stake $100 while the global cap stays $75. Else FLAT mode stakes a fixed
    # amount per pick; else size as a % of bankroll. The window total is then CLAMPED to the
    # per-window budget (the correlation guard — picks in a window co-settle).
    flat = int(getattr(cfg, "flat_stake_cents", 0) or 0)
    by_interval = getattr(cfg, "stake_by_interval", None) or {}
    interval_stake = by_interval.get((pick.interval or "").upper())
    per_pick_cap = int(getattr(cfg, "max_stake_per_pick_cents", 0) or 0)
    if interval_stake and int(interval_stake) > 0:
        stake = int(interval_stake)
        window_cap = int(interval_stake) * max(1, cfg.max_picks_per_window)
        per_pick_cap = int(interval_stake)   # the override is the ceiling for this interval
    elif flat > 0:
        stake = flat
        window_cap = flat * max(1, cfg.max_picks_per_window)
    else:
        stake = int(round(cfg.per_pick_pct * bankroll))
        window_cap = int(round(cfg.max_per_window_pct * bankroll))
    already = state.window_committed_cents.get(wk, 0)
    remaining = window_cap - already
    if remaining <= 0:
        return Decision(False, "WINDOW_FULL")
    stake = min(stake, remaining)
    # HARD per-pick dollar ceiling — the absolute cap on one trade's risk.
    if per_pick_cap > 0:
        stake = min(stake, per_pick_cap)
    # Never stake more than the bankroll actually on hand.
    stake = min(stake, bankroll)

    count = stake // price   # whole contracts only (integer cents)
    if count < 1:
        return Decision(False, "SIZE_TOO_SMALL")

    limit = price + int(cfg.limit_offset_cents)
    limit = max(1, min(99, limit))
    # never let the offset push the limit outside the sanity band
    limit = max(cfg.min_price_cents, min(cfg.max_price_cents, limit))

    return Decision(True, "OK", count=count, limit_price_cents=limit, stake_cents=count * price)


def apply_fill(state: PortfolioState, pick: Pick, decision: Decision) -> PortfolioState:
    """Return a NEW state reflecting a placed entry (pure; the executor swaps it in)."""
    wk = int(pick.window_key)
    return PortfolioState(
        bankroll_cents=state.bankroll_cents,
        day_start_bankroll_cents=state.day_start_bankroll_cents,
        day_realized_pnl_cents=state.day_realized_pnl_cents,
        open_count=state.open_count + 1,
        open_tickers=state.open_tickers | {pick.ticker},
        positions={**state.positions, pick.ticker: decision.count},
        window_count={**state.window_count, wk: state.window_count.get(wk, 0) + 1},
        window_committed_cents={**state.window_committed_cents,
                                wk: state.window_committed_cents.get(wk, 0) + decision.stake_cents},
        window_tickers={**state.window_tickers,
                        wk: state.window_tickers.get(wk, frozenset()) | {pick.ticker}},
    )


def apply_exit(state: PortfolioState, ticker: str) -> PortfolioState:
    """Return a NEW state with ``ticker`` closed out (after a sell)."""
    if ticker not in state.positions:
        return state
    return PortfolioState(
        bankroll_cents=state.bankroll_cents,
        day_start_bankroll_cents=state.day_start_bankroll_cents,
        day_realized_pnl_cents=state.day_realized_pnl_cents,
        open_count=max(0, state.open_count - 1),
        open_tickers=state.open_tickers - {ticker},
        positions={k: v for k, v in state.positions.items() if k != ticker},
        window_count=state.window_count,
        window_committed_cents=state.window_committed_cents,
        window_tickers=state.window_tickers,
    )
