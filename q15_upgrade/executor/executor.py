"""Ultoim V2 EXECUTOR — orchestration: fire signal -> risk check -> order.

The ``Executor`` holds the live portfolio snapshot, runs each v2 delivered NO pick
through the pure risk manager, and (dry-run or live) places the order via the trading
client. It is the seam the app wires v2 fires into. Default-OFF via ``get_executor()``.

Idempotency: orders carry a deterministic ``client_order_id`` keyed on (window, ticker,
kind), so a duplicated fire or a retry cannot double-place.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Mapping

from .config import ExecutorConfig
from .risk import Pick, PortfolioState, decide, apply_fill, apply_exit, prune_settled
from .trading_client import KalshiTradingClient

logger = logging.getLogger("q15.executor")


def _coid(window_key: Any, ticker: str, kind: str) -> str:
    return f"v2x-{window_key}-{ticker}-{kind}"


def _opt_float(v: Any) -> float | None:
    """Coerce an optional numeric (BTC gate input) to float, or None if absent/bad."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class Executor:
    def __init__(self, cfg: ExecutorConfig | None = None, client: Any | None = None):
        self.cfg = cfg or ExecutorConfig.from_env()
        self.client = client or KalshiTradingClient(self.cfg)
        # Durable order/fill recorder (default ON; best-effort — never blocks an order).
        self.store = None
        if getattr(self.cfg, "record_orders", False):
            try:
                from .store import ExecutorStore
                self.store = ExecutorStore(self.cfg.orders_db_path)
            except Exception:  # noqa: BLE001 - recording is optional; never block the executor
                logger.exception("executor store init failed (order recording disabled)")
                self.store = None
        live_bal = None
        if self.cfg.enabled and not self.cfg.dry_run:
            live_bal = self.client.get_balance_cents()
        bankroll = self._initial_bankroll(live_bal)
        # Day-start balance is the stop-loss reference: the REAL balance when live, else the
        # configured/dry-run bankroll. The stop measures realized loss as a drop from this.
        self._day_start_balance = live_bal if live_bal is not None else bankroll
        self.state = PortfolioState(
            bankroll_cents=bankroll,
            day_start_bankroll_cents=self._day_start_balance,
        )
        logger.info("executor init: %s | bankroll=%dc", self.cfg.safety_summary(), bankroll)

    def _initial_bankroll(self, live_bal: int | None = None) -> int:
        if self.cfg.bankroll_cents > 0:
            return self.cfg.bankroll_cents
        # live mode with no fixed bankroll: use the account balance read at init
        if live_bal is not None:
            return live_bal
        return 0

    def _refresh_daily_pnl(self) -> None:
        """Update the day's realized P&L from the LIVE balance so the stop-loss can fire.
        Cash-drawdown basis (realized = current_balance - day_start_balance): while a position
        is still open the staked cash reads as a loss, so this errs toward STOPPING — the safe
        direction for a stop. No-op in dry-run/disabled (no real balance moves)."""
        if self.cfg.dry_run or not self.cfg.enabled:
            return
        # The balance read ONLY feeds the daily stop. When the stop is fully disabled (both the
        # absolute and % limits are 0) skip it entirely — removes a network round-trip from the
        # order path AND decouples the bot from the shared account balance, so the owner's manual
        # trades can never pause it. Owner-chosen: no daily circuit breaker.
        if (int(getattr(self.cfg, "daily_loss_limit_cents", 0) or 0) <= 0
                and float(getattr(self.cfg, "daily_loss_limit_pct", 0) or 0) <= 0):
            return
        bal = self.client.get_balance_cents()
        if bal is None:
            return
        from dataclasses import replace
        self.state = replace(self.state, day_realized_pnl_cents=int(bal) - int(self._day_start_balance))

    def on_fire(self, pick: Mapping[str, Any]) -> dict[str, Any]:
        """Handle one v2 delivered NO pick. Returns a result dict (never raises on a
        normal refusal). ``pick`` needs: ticker, asset, predicted_side, entry/limit
        price (cents), window_key."""
        if not self.cfg.enabled:
            return {"placed": False, "reason": "DISABLED"}
        # Release positions whose 15-min window has already settled, so the optimistic in-memory
        # open_count / open_tickers can't grow forever and pin at MAX_OPEN / block every ticker with
        # DUP_TICKER (the cause of the bot silently halting after ~max_open entries). Uses the
        # incoming pick's window; strictly-older windows have settled on the exchange.
        try:
            prune_wk = int(pick.get("window_key"))
        except (TypeError, ValueError):
            prune_wk = None
        if prune_wk is not None:
            self.state = prune_settled(self.state, prune_wk)
        # Latency instrumentation — so fire->ack timing on the live book can be MEASURED, not
        # guessed. fired_at is the cycle wall-clock the pick was decided at; snapshot_age below =
        # how stale the quoted ask is by the time the order lands (worker queue + alert + the
        # balance GET + the order POST). Pure observability; never changes the order.
        fired_at = pick.get("fired_at")
        _t0 = time.perf_counter()
        self._refresh_daily_pnl()   # pull live realized P&L so the stop-loss can gate this entry
        _bal_ms = (time.perf_counter() - _t0) * 1000.0
        # Band-check and limit on the SAME price the v2 gate admitted on (entry_ask_cents). The
        # gate keys its [ask_lo, ask_hi] admission on entry_ask_cents; if the executor instead
        # banded on best_entry_cents (the optimistic "or lower" display price, ~1.7c below the
        # ask and sometimes across a band boundary) the two layers could disagree — the gate
        # admits while the executor refuses on PRICE_BAND, or vice versa (42/189 fired rows
        # differ). entry_ask_cents is also the marketable price that actually fills a buy.
        # An explicit entry_price_cents override still wins; best_entry_cents is the last resort.
        price = pick.get("entry_price_cents")
        if price is None:
            price = pick.get("entry_ask_cents") or pick.get("best_entry_cents")
        try:
            p = Pick(
                ticker=str(pick.get("ticker") or ""),
                asset=str(pick.get("asset") or ""),
                side=str(pick.get("predicted_side") or pick.get("side") or "NO"),
                price_cents=int(round(float(price))) if price is not None else -1,
                window_key=int(pick.get("window_key")),
                interval=str(pick.get("interval") or ""),
                stake_multiplier=int(pick.get("stake_multiplier") or 1),
                btc_lean=_opt_float(pick.get("btc_lean")),
                prior_breadth=_opt_float(pick.get("prior_breadth")),
            )
        except (TypeError, ValueError):
            return {"placed": False, "reason": "BAD_PICK"}

        d = decide(p, self.state, self.cfg)
        if not d.place:
            logger.info("skip %s w%s: %s", p.ticker, p.window_key, d.reason)
            return {"placed": False, "reason": d.reason}

        _t1 = time.perf_counter()
        res = self.client.place_order(
            ticker=p.ticker, side="no", count=d.count,
            price_cents=d.limit_price_cents, action="buy",
            client_order_id=_coid(p.window_key, p.ticker, "entry"),
        )
        _order_ms = (time.perf_counter() - _t1) * 1000.0
        _age_ms = None
        if fired_at is not None:
            try:
                _age_ms = (time.time() - float(fired_at)) * 1000.0
            except (TypeError, ValueError):
                _age_ms = None
        # Record the placement + raw response + (immediate) fill classification, so "how many
        # orders missed" is answerable. Best-effort; runs for failures and dry-run too.
        fill_status, filled_count = (None, None)
        if self.store is not None:
            fill_status, filled_count = self.store.record_order_result(
                action="entry", pick=p, decision=d, res=res, age_ms=_age_ms,
                bal_ms=_bal_ms, order_ms=_order_ms,
                client_order_id=_coid(p.window_key, p.ticker, "entry"))
        if not res.get("ok"):
            logger.error("order FAILED %s: %s", p.ticker, res.get("error"))
            return {"placed": False, "reason": "ORDER_FAILED", "detail": res,
                    "balance_latency_ms": round(_bal_ms, 1), "order_latency_ms": round(_order_ms, 1),
                    "fill_status": fill_status}

        # Commit the position to the snapshot (so window/open caps see it). NOTE: still OPTIMISTIC
        # (books on a successful PLACE, not a confirmed FILL); fill_status now records when that
        # assumption diverges, pending a validated reconcile before we change the bookkeeping.
        self.state = apply_fill(self.state, p, d)
        mode = "dry-run" if res.get("dry_run") else "LIVE"
        _age_str = f"{_age_ms:.0f}ms" if _age_ms is not None else "n/a"
        logger.info("executor timing %s w%s: snapshot_age=%s balance=%.0fms order=%.0fms total=%.0fms fill=%s",
                    p.ticker, p.window_key, _age_str, _bal_ms, _order_ms, _bal_ms + _order_ms, fill_status)
        logger.info("placed[%s] %s x%d @ %dc (stake %dc)", mode, p.ticker, d.count,
                    d.limit_price_cents, d.stake_cents)
        return {"placed": True, "mode": mode, "ticker": p.ticker, "count": d.count,
                "limit_price_cents": d.limit_price_cents, "stake_cents": d.stake_cents,
                "dry_run": bool(res.get("dry_run")), "order": res,
                "balance_latency_ms": round(_bal_ms, 1), "order_latency_ms": round(_order_ms, 1),
                "snapshot_age_ms": (round(_age_ms, 1) if _age_ms is not None else None),
                "fill_status": fill_status, "filled_count": filled_count}

    def on_exit(self, ticker: str, window_key: Any, exit_price_cents: int) -> dict[str, Any]:
        """Defensive-exit: SELL an open NO position at ``exit_price_cents``. The kill
        switch blocks even exits (you can always close manually); everything else allows
        them — closing risk is never gated by the daily/window caps."""
        if not self.cfg.enabled:
            return {"placed": False, "reason": "DISABLED"}
        count = self.state.positions.get(ticker, 0)
        if count < 1:
            return {"placed": False, "reason": "NO_POSITION"}
        # Price the close to actually fill: it goes out immediate-or-cancel (reduce_only), so a
        # mid-priced sell would cancel unfilled. Sell exit_limit_offset_cents UNDER the estimated
        # exit value to cross the resting bid. Clamp to a tradeable cent (the side/price mapper
        # clamps the wire price into [1,99] as well).
        limit_px = max(1, int(exit_price_cents) - int(self.cfg.exit_limit_offset_cents))
        res = self.client.place_order(
            ticker=ticker, side="no", count=count, price_cents=limit_px,
            action="sell", client_order_id=_coid(window_key, ticker, "exit"),
        )
        fill_status = None
        if self.store is not None:
            from types import SimpleNamespace
            _pick = SimpleNamespace(ticker=ticker, asset="", interval="", window_key=window_key)
            _dec = SimpleNamespace(count=count, limit_price_cents=limit_px, stake_cents=None)
            fill_status, _ = self.store.record_order_result(
                action="exit", pick=_pick, decision=_dec, res=res, age_ms=None,
                bal_ms=None, order_ms=None, client_order_id=_coid(window_key, ticker, "exit"))
        if res.get("ok"):
            self.state = apply_exit(self.state, ticker)   # close it out of the snapshot
        return {"placed": bool(res.get("ok")), "count": count,
                "mode": "dry-run" if res.get("dry_run") else "LIVE", "order": res,
                "fill_status": fill_status}


_executor: Executor | None = None


def get_executor() -> Executor | None:
    """Return the singleton executor, or None when disabled (default)."""
    global _executor
    cfg = ExecutorConfig.from_env()
    if not cfg.enabled:
        return None
    if _executor is None:
        _executor = Executor(cfg)
    return _executor


def reset_executor_for_tests() -> None:
    global _executor
    _executor = None
