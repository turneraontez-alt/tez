"""Ultoim V2 EXECUTOR — orchestration: fire signal -> risk check -> order.

The ``Executor`` holds the live portfolio snapshot, runs each v2 delivered NO pick
through the pure risk manager, and (dry-run or live) places the order via the trading
client. It is the seam the app wires v2 fires into. Default-OFF via ``get_executor()``.

Idempotency: orders carry a deterministic ``client_order_id`` keyed on (window, ticker,
kind), so a duplicated fire or a retry cannot double-place.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from .config import ExecutorConfig
from .risk import Pick, PortfolioState, decide, apply_fill, apply_exit
from .trading_client import KalshiTradingClient

logger = logging.getLogger("q15.executor")


def _coid(window_key: Any, ticker: str, kind: str) -> str:
    return f"v2x-{window_key}-{ticker}-{kind}"


class Executor:
    def __init__(self, cfg: ExecutorConfig | None = None, client: Any | None = None):
        self.cfg = cfg or ExecutorConfig.from_env()
        self.client = client or KalshiTradingClient(self.cfg)
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
        self._refresh_daily_pnl()   # pull live realized P&L so the stop-loss can gate this entry
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
            )
        except (TypeError, ValueError):
            return {"placed": False, "reason": "BAD_PICK"}

        d = decide(p, self.state, self.cfg)
        if not d.place:
            logger.info("skip %s w%s: %s", p.ticker, p.window_key, d.reason)
            return {"placed": False, "reason": d.reason}

        res = self.client.place_order(
            ticker=p.ticker, side="no", count=d.count,
            price_cents=d.limit_price_cents, action="buy",
            client_order_id=_coid(p.window_key, p.ticker, "entry"),
        )
        if not res.get("ok"):
            logger.error("order FAILED %s: %s", p.ticker, res.get("error"))
            return {"placed": False, "reason": "ORDER_FAILED", "detail": res}

        # Commit the position to the snapshot (so window/open caps see it).
        self.state = apply_fill(self.state, p, d)
        mode = "dry-run" if res.get("dry_run") else "LIVE"
        logger.info("placed[%s] %s x%d @ %dc (stake %dc)", mode, p.ticker, d.count,
                    d.limit_price_cents, d.stake_cents)
        return {"placed": True, "mode": mode, "ticker": p.ticker, "count": d.count,
                "limit_price_cents": d.limit_price_cents, "stake_cents": d.stake_cents,
                "dry_run": bool(res.get("dry_run")), "order": res}

    def on_exit(self, ticker: str, window_key: Any, exit_price_cents: int) -> dict[str, Any]:
        """Defensive-exit: SELL an open NO position at ``exit_price_cents``. The kill
        switch blocks even exits (you can always close manually); everything else allows
        them — closing risk is never gated by the daily/window caps."""
        if not self.cfg.enabled:
            return {"placed": False, "reason": "DISABLED"}
        count = self.state.positions.get(ticker, 0)
        if count < 1:
            return {"placed": False, "reason": "NO_POSITION"}
        res = self.client.place_order(
            ticker=ticker, side="no", count=count, price_cents=int(exit_price_cents),
            action="sell", client_order_id=_coid(window_key, ticker, "exit"),
        )
        if res.get("ok"):
            self.state = apply_exit(self.state, ticker)   # close it out of the snapshot
        return {"placed": bool(res.get("ok")), "count": count,
                "mode": "dry-run" if res.get("dry_run") else "LIVE", "order": res}


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
