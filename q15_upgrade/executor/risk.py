"""Ultoim V2 EXECUTOR — risk manager (PURE, no I/O, fully unit-testable).

``decide(pick, state, cfg)`` is the ONE place that turns a fire signal + the current
portfolio snapshot into an order size — or a refusal. It is a pure function so every
guard is deterministically testable. All money is INTEGER CENTS (no float drift).

Guards (a refusal short-circuits with a single reason code):
  KILL            — kill switch on
  WRONG_SIDE      — not the NO side (no_only)
  INTERVAL_BLOCKED— interval not in the (optional) allowlist
  PRICE_BAND      — price outside [min,max]
  ENTRY_ASK_FLOOR — entry ask below the (optional) GLOBAL ask floor (applies to ALL picks)
  YES_PROB_FLOOR  — YES bot: v2 P(YES) below min_yes_prob (or missing) — inert when min_yes_prob=0
  BTC_LEAN_FLOOR  — YES bot: BTC not bullish enough (btc_lean < min_btc_lean, or missing) — inert at 0
  ASSET_EXCLUDED  — YES bot: asset in excluded_assets (e.g. BNB) — inert when the set is empty
  DAILY_STOP      — day realized P&L past the loss limit (circuit breaker)
  MAX_OPEN        — already at max open positions
  DUP_TICKER      — already hold / already traded this ticker+window
  WINDOW_FULL     — already placed max picks for this window
  SECOND_PICK_ASK_FLOOR — a 2nd-or-later pick in a window below the (optional) ask floor
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
    stake_multiplier: int = 1  # conviction size factor from v2 (2 on >=3 co-triggering 10M); 1 = normal
    # Cross-asset gate inputs (from v2, at-or-before this pick's decision; None when unavailable):
    btc_lean: float | None = None        # BTC's contemporaneous market-implied P(YES) this window
    prior_breadth: float | None = None   # fraction of the complex that settled YES in window_key-1
    yes_prob: float | None = None        # v2 market-implied P(YES) for THIS contract (live YES bot gate)


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
    # GLOBAL entry ask floor — applies to EVERY pick (incl. the 1st), unlike second_pick_min_ask.
    # Default 0 => `price >= 0` always true => byte-identical. When set, refuse a fired NO whose
    # entry ask is below the floor (the net-negative cheap-NO zone; recommended 58, default-OFF).
    floor = int(getattr(cfg, "min_entry_ask", 0) or 0)
    if price < floor:
        return Decision(False, "ENTRY_ASK_FLOOR")

    # BTC CROSS-ASSET GATE (owner LIVE, cfg.btc_gate_enabled default ON). The alts co-move with
    # BTC, so an alt-NO is unreliable when BTC is contemporaneously bullish (pick.btc_lean high)
    # OR the complex is risk-on (pick.prior_breadth high, the prior window settled mostly YES).
    # Suppress the entry in that regime — EXCEPT a >=3-co-trigger 10M CONVICTION window
    # (stake_multiplier > 1): those run ~100% and are protected by the defensive exit, so they
    # stay at full size. Only acts on a PRESENT signal (a missing lean/breadth never gates), and
    # uses BTC's at-or-before-decision read (no same-window-settlement lookahead). Backtest
    # (in-sample ~2 days) lifted kept-book accuracy and P&L. Disable instantly with
    # Q15_EXEC_BTC_GATE=false — no code change.
    if (getattr(cfg, "btc_gate_enabled", False) and side == "NO"
            and int(getattr(pick, "stake_multiplier", 1) or 1) <= 1):
        lean = getattr(pick, "btc_lean", None)
        brd = getattr(pick, "prior_breadth", None)
        lean_thr = float(getattr(cfg, "btc_gate_lean", 0.5) or 0.5)
        brd_thr = float(getattr(cfg, "btc_gate_breadth", 0.5) or 0.5)
        if (lean is not None and float(lean) >= lean_thr) or (brd is not None and float(brd) >= brd_thr):
            return Decision(False, "BTC_GATE")

    # YES-SIDE ADMISSION GATES — used ONLY by the separate live "YES bot" executor instance
    # (q15_upgrade.executor.config.yes_config_from_env). INERT by default: min_yes_prob and
    # min_btc_lean default 0.0 and excluded_assets defaults empty, so for the NO executor (and
    # any default ExecutorConfig) all three short-circuit to no-ops and decide() is byte-identical.
    # They FAIL CLOSED — a missing required signal REFUSES the entry: we never buy YES without a
    # confirmed v2 P(YES) (yes_prob) and a confirmed contemporaneously-bullish BTC read (btc_lean).
    min_pyes = float(getattr(cfg, "min_yes_prob", 0.0) or 0.0)
    if min_pyes > 0.0:
        yp = getattr(pick, "yes_prob", None)
        if yp is None or float(yp) < min_pyes:
            return Decision(False, "YES_PROB_FLOOR")
    min_lean = float(getattr(cfg, "min_btc_lean", 0.0) or 0.0)
    if min_lean > 0.0:
        bl = getattr(pick, "btc_lean", None)
        if bl is None or float(bl) < min_lean:
            return Decision(False, "BTC_LEAN_FLOOR")
    excluded = getattr(cfg, "excluded_assets", frozenset()) or frozenset()
    if excluded and (pick.asset or "").upper() in excluded:
        return Decision(False, "ASSET_EXCLUDED")

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
    # CONDITIONAL 2nd-pick ask floor: the first pick of a window is unchanged; any 2nd-or-later pick
    # must clear the floor (the proven expensive-NO band). Default floor 0 => `price >= 0` always
    # true => byte-identical. Never loosens the WINDOW_FULL cap above (checked first).
    second_floor = int(getattr(cfg, "second_pick_min_ask", 0) or 0)
    if state.window_count.get(wk, 0) >= 1 and price < second_floor:
        return Decision(False, "SECOND_PICK_ASK_FLOOR")

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
    # NOTE: max_per_window_pct is deliberately NOT applied in the flat / per-interval branches.
    # In those modes the window budget IS stake * max_picks by design, and the per-interval
    # override is documented to supersede the flat stake and the per-pick cap — clamping it to a
    # % of bankroll would silently shrink the owner's chosen concentration. The %-of-bankroll
    # ceiling is scoped to percentage sizing; see the field's docstring in config.py.
    # CONVICTION sizing (owner: "first $150, extras double"): a >=3-co-trigger pick carries
    # stake_multiplier>1 from v2. The LEAD pick of a window always stays at the base stake;
    # only the 2nd-or-later pick of a >=3 window is sized up by that factor (window_count is
    # 0 for the first pick). We scale the per-window budget so the up-sized extra isn't clamped
    # by the correlation guard — but NOT the hard per-pick ceiling, which stays an ABSOLUTE cap
    # (the owner sets it to the intended doubled size, e.g. $300, so a runaway flat can't blow
    # past it). bankroll still binds below. NOTE: leverage — a conviction window commits more on
    # correlated co-settlers than a normal window.
    mult = int(getattr(pick, "stake_multiplier", 1) or 1)
    is_extra_pick = state.window_count.get(wk, 0) >= 1
    if not getattr(cfg, "conviction_sizing", True) or mult < 1 or not is_extra_pick:
        mult = 1
    if mult > 1:
        stake *= mult
        window_cap *= mult
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

    # Resolve the price we will ACTUALLY pay before sizing. A buy goes out at the limit, not at
    # the signalled ask, so sizing off `price` while posting at `price + limit_offset_cents`
    # spent count*offset more than the stake — quietly breaching max_stake_per_pick_cents (the
    # "absolute cap on one trade's risk") and under-crediting window_committed_cents by the same
    # amount, which let a window fill past its nominal ceiling.
    limit = price + int(cfg.limit_offset_cents)
    limit = max(1, min(99, limit))
    # never let the offset push the limit outside the sanity band
    limit = max(cfg.min_price_cents, min(cfg.max_price_cents, limit))

    count = stake // limit   # whole contracts only (integer cents), at the price we pay
    if count < 1:
        return Decision(False, "SIZE_TOO_SMALL")

    # Book the true worst-case cash out (a full fill at the limit) so the per-window budget and
    # the ledger both reflect money actually committed.
    return Decision(True, "OK", count=count, limit_price_cents=limit, stake_cents=count * limit)


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


def prune_settled(state: PortfolioState, current_window_key: int) -> PortfolioState:
    """Release positions whose 15-min settlement window has already passed (``window_key`` strictly
    older than the current pick's). Those contracts settled on the exchange, so they must NOT keep
    counting toward the open-position / per-window / dup-ticker caps.

    Without this the OPTIMISTIC in-memory ``open_count`` / ``open_tickers`` only ever GROW (apply_fill
    adds; only the rare defensive ``apply_exit`` removes) — so ``open_count`` pins at
    ``max_open_positions`` and every fired asset trips ``DUP_TICKER`` after its first entry, silently
    halting all new entries until a restart. window_key = close_time//900 is monotonic with time, so
    any window strictly older than the incoming pick's has settled. Pure; returns a NEW state."""
    keep = {wk for wk in state.window_tickers if wk >= int(current_window_key)}
    if len(keep) == len(state.window_tickers):
        return state
    kept: set[str] = set()
    for wk in keep:
        kept |= set(state.window_tickers.get(wk, frozenset()))
    return PortfolioState(
        bankroll_cents=state.bankroll_cents,
        day_start_bankroll_cents=state.day_start_bankroll_cents,
        day_realized_pnl_cents=state.day_realized_pnl_cents,
        open_count=len(kept),
        open_tickers=frozenset(kept),
        positions={t: c for t, c in state.positions.items() if t in kept},
        window_count={wk: v for wk, v in state.window_count.items() if wk in keep},
        window_committed_cents={wk: v for wk, v in state.window_committed_cents.items() if wk in keep},
        window_tickers={wk: v for wk, v in state.window_tickers.items() if wk in keep},
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
