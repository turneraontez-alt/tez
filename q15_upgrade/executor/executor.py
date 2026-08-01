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
from datetime import datetime, timezone
from typing import Any, Mapping

from .config import ExecutorConfig, yes_config_from_env
from .risk import Pick, PortfolioState, decide, apply_fill, apply_exit, prune_settled
from .trading_client import KalshiTradingClient

logger = logging.getLogger("q15.executor")

# Durable meta keys for the daily circuit breaker. Without these the day-start
# reference is re-read from the (already drawn-down) balance on every restart,
# so hitting the stop and bouncing the process silently re-arms trading.
_META_DAY_DATE = "daily_stop_day_utc"
_META_DAY_START_BALANCE = "daily_stop_day_start_balance_cents"


def _utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _coid(window_key: Any, ticker: str, kind: str, prefix: str = "v2x") -> str:
    # The prefix namespaces the idempotency key PER EXECUTOR (NO="v2x", YES="v2xy") so a NO and a
    # YES order on the SAME contract+window can never collide at Kalshi (the exchange dedupes on
    # client_order_id, which separate order DBs would NOT protect against).
    return f"{prefix}-{window_key}-{ticker}-{kind}"


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
        # RESTART SAFETY: if the store already holds a reference for TODAY (UTC), reuse it —
        # re-reading the drawn-down balance here is what turned a $100/day cap into $100/restart.
        default_ref = live_bal if live_bal is not None else bankroll
        self._day_date = _utc_today()
        self._day_start_balance = self._load_day_reference(default_ref)
        self.state = PortfolioState(
            bankroll_cents=bankroll,
            day_start_bankroll_cents=self._day_start_balance,
        )
        # (window_key, ticker) -> broker order_id of the entry we placed. Lets a
        # defensive exit CANCEL a still-resting entry instead of only selling
        # against it (a GTC buy left working fills after we think we are flat).
        self._entry_orders: dict[tuple[int, str], str] = {}
        # Side this executor trades. The NO executor (no_only=True) buys/sells "no"; the YES bot
        # (no_only=False) trades "yes". Drives on_exit and the client_order_id namespace, so the
        # two instances are wire-isolated at the exchange.
        self.entry_side = "no" if self.cfg.no_only else "yes"
        self.coid_prefix = "v2x" if self.cfg.no_only else "v2xy"
        # DURABILITY: window_count/window_tickers (the per-window cap + dup-ticker guard) live ONLY
        # in self.state (in-memory) and would reset to 0 on a process restart — so a restart
        # mid-window could admit MORE than max_picks_per_window entries in one settlement window.
        # Rehydrate them from the durable orders store so the cap survives a restart. Best-effort:
        # if recording is off or the store is empty/unavailable, behaviour is unchanged.
        self._rehydrate_window_cap()
        logger.info("executor init: %s | bankroll=%dc", self.cfg.safety_summary(), bankroll)

    def _load_day_reference(self, default_ref: int) -> int:
        """Return the daily-stop reference balance for TODAY (UTC).

        Reuses a stored reference when it belongs to today, so a restart cannot
        clear the day's drawdown and re-arm entries. On a new UTC day (or with no
        store) the current balance becomes the new reference and is persisted."""
        if self.store is not None:
            stored_date = self.store.get_meta(_META_DAY_DATE)
            stored_ref = self.store.get_meta(_META_DAY_START_BALANCE)
            if stored_date == self._day_date and stored_ref is not None:
                try:
                    ref = int(float(stored_ref))
                except (TypeError, ValueError):
                    ref = None
                if ref is not None:
                    logger.info(
                        "executor daily-stop reference restored for %s: %dc "
                        "(restart does NOT reset the day's loss)", self._day_date, ref)
                    return ref
        self._persist_day_reference(int(default_ref))
        return int(default_ref)

    def _persist_day_reference(self, ref_cents: int) -> None:
        if self.store is None:
            return
        self.store.set_meta(_META_DAY_DATE, self._day_date)
        self.store.set_meta(_META_DAY_START_BALANCE, str(int(ref_cents)))

    def _rehydrate_window_cap(self) -> None:
        """Seed window_count/window_tickers AND positions/open_tickers from the orders store so
        the per-window cap, the dup-ticker guard and — critically — the ability to EXIT survive a
        process restart.

        Without the position half, ``state.positions`` came back empty and every defensive close
        refused with NO_POSITION while a real, funded position rode to settlement. The rehydrated
        count is the REQUESTED count (an upper bound on what actually filled), which is safe
        because a close is sent ``reduce_only``: Kalshi clamps it to the position genuinely held,
        so overstating can never flip us short. Older windows are pruned on the next on_fire by
        prune_settled. Never raises."""
        if self.store is None:
            return
        # Only inherit placements made in the SAME posture we are running in now: a book
        # flipped from dry-run to live must not resurrect simulated positions as real ones.
        can_place_live = (self.cfg.enabled and not self.cfg.dry_run
                          and not self.cfg.kill_switch)
        mode = "LIVE" if can_place_live else "dry-run"
        try:
            entries = self.store.recent_entry_orders(since_seconds=7200.0, mode=mode)
        except Exception:  # noqa: BLE001 - best-effort; never block init
            logger.exception("executor rehydrate failed (state falls back to in-memory)")
            return
        if not entries:
            return
        from dataclasses import replace

        wt: dict[int, set[str]] = {}
        positions: dict[str, int] = {}
        for row in entries:
            wk = row["window_key"]
            ticker = row["ticker"]
            wt.setdefault(wk, set()).add(ticker)
            # Rows are oldest-first, so the newest placement for a ticker wins.
            # Use the REQUESTED count, not the filled count: a resting GTC entry
            # records filled_count=0 at POST time yet can still fill later in the
            # window, so filled_count would rehydrate a live position as flat.
            # Requested is the upper bound, and reduce_only clamps the close to
            # whatever is genuinely held.
            count = int(row.get("requested_count") or row.get("filled_count") or 0)
            if count > 0:
                positions[ticker] = count
            if row.get("order_id"):
                self._entry_orders[(wk, ticker)] = str(row["order_id"])
        if not wt:
            return
        window_tickers = {wk: frozenset(tks) for wk, tks in wt.items()}
        window_count = {wk: len(tks) for wk, tks in window_tickers.items()}
        open_tickers = frozenset(positions)
        self.state = replace(
            self.state,
            window_count=window_count,
            window_tickers=window_tickers,
            positions=positions,
            open_tickers=open_tickers,
            open_count=len(open_tickers),
        )
        logger.info(
            "executor rehydrated from store: per-window cap %s; %d open position(s) %s; "
            "%d entry order id(s) for cancel-on-exit",
            {wk: n for wk, n in sorted(window_count.items())},
            len(open_tickers), sorted(open_tickers), len(self._entry_orders))

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
        # DAY ROLLOVER: without this the reference captured at __init__ never moves, so the
        # "daily" stop is really a since-process-start stop — a process up for days stays frozen
        # at DAILY_STOP on a loss that is no longer today's. Re-base at the UTC date change.
        today = _utc_today()
        if today != self._day_date:
            logger.info("executor daily-stop rollover %s -> %s: reference %dc -> %dc",
                        self._day_date, today, self._day_start_balance, int(bal))
            self._day_date = today
            self._day_start_balance = int(bal)
            self._persist_day_reference(self._day_start_balance)
            self.state = replace(self.state, day_start_bankroll_cents=self._day_start_balance)
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
                yes_prob=_opt_float(pick.get("yes_prob")),
            )
        except (TypeError, ValueError):
            return {"placed": False, "reason": "BAD_PICK"}

        d = decide(p, self.state, self.cfg)
        if not d.place:
            logger.info("skip %s w%s: %s", p.ticker, p.window_key, d.reason)
            return {"placed": False, "reason": d.reason}

        _t1 = time.perf_counter()
        # Side is the PICK's side (NO picks carry "NO" -> "no", byte-identical to the prior
        # hardcoded value; a YES bot pick carries "YES" -> "yes"). coid prefix is per-executor.
        res = self.client.place_order(
            ticker=p.ticker, side=(p.side or self.entry_side).lower(), count=d.count,
            price_cents=d.limit_price_cents, action="buy",
            client_order_id=_coid(p.window_key, p.ticker, "entry", self.coid_prefix),
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
                client_order_id=_coid(p.window_key, p.ticker, "entry", self.coid_prefix))
        if not res.get("ok"):
            if res.get("uncertain"):
                # The POST failed in a way that does NOT prove rejection (read timeout,
                # mid-flight drop) — Kalshi may have accepted and filled it. Treating this
                # as "not placed" left a real position untracked AND released the window's
                # risk budget, so the next co-settling pick doubled the intended exposure.
                # Book it: consume the budget, own the ticker, and surface it for reconcile.
                self.state = apply_fill(self.state, p, d)
                logger.error(
                    "order UNCERTAIN %s w%s (%s) — booking the exposure; reconcile against "
                    "the account before trusting the position", p.ticker, p.window_key,
                    res.get("error"))
                return {"placed": False, "reason": "ORDER_UNCERTAIN", "uncertain": True,
                        "booked": True, "detail": res, "ticker": p.ticker, "count": d.count,
                        "balance_latency_ms": round(_bal_ms, 1),
                        "order_latency_ms": round(_order_ms, 1), "fill_status": fill_status}
            logger.error("order FAILED %s: %s", p.ticker, res.get("error"))
            return {"placed": False, "reason": "ORDER_FAILED", "detail": res,
                    "balance_latency_ms": round(_bal_ms, 1), "order_latency_ms": round(_order_ms, 1),
                    "fill_status": fill_status}

        # Commit the position to the snapshot (so window/open caps see it). NOTE: still OPTIMISTIC
        # (books on a successful PLACE, not a confirmed FILL); fill_status now records when that
        # assumption diverges, pending a validated reconcile before we change the bookkeeping.
        self.state = apply_fill(self.state, p, d)
        # Remember the broker order id so a defensive exit can CANCEL a still-resting
        # entry. A GTC buy left working keeps filling after the exit thinks we are flat.
        entry_order_id = self._order_id_of(res)
        entry_key = self._entry_key(p.ticker, p.window_key)
        if entry_order_id and entry_key is not None:
            self._entry_orders[entry_key] = entry_order_id
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

    @staticmethod
    def _order_id_of(res: Mapping[str, Any]) -> str | None:
        """Pull the broker order id out of a place_order response (None in dry-run)."""
        try:
            from .store import _inner_order
            order = _inner_order((res or {}).get("data"))
            oid = order.get("order_id")
            if oid is None:
                oid = order.get("id")
            return None if oid is None else str(oid)
        except Exception:  # noqa: BLE001 - id extraction must never break the order path
            return None

    @staticmethod
    def _entry_key(ticker: str, window_key: Any) -> tuple[int, str] | None:
        """(window_key, ticker) key for _entry_orders, or None if the window is unusable."""
        try:
            return (int(window_key), str(ticker))
        except (TypeError, ValueError):
            return None

    def _cancel_resting_entry(self, ticker: str, window_key: Any) -> dict[str, Any] | None:
        """Cancel the entry order for (window, ticker) if we know its id.

        An entry is GTC so it can still be RESTING when the defensive exit fires. Selling
        alone does not remove it: the IoC reduce_only sell fills 0 against a position that
        does not exist yet, we book ourselves flat, and the original buy then fills into the
        adverse move we were trying to escape. Cancelling first closes that hole. Cancelling
        an already-filled order is harmless — the broker just reports it cannot be cancelled."""
        key = self._entry_key(ticker, window_key)
        order_id = self._entry_orders.get(key) if key is not None else None
        if not order_id:
            return None
        try:
            res = self.client.cancel_order(order_id)
        except Exception:  # noqa: BLE001 - an exit must proceed even if the cancel errors
            logger.exception("entry cancel raised for %s (%s); continuing to sell",
                             ticker, order_id)
            return {"ok": False, "error": "cancel_raised"}
        if res.get("ok"):
            logger.info("cancelled resting entry %s for %s before exit", order_id, ticker)
        else:
            # Expected when the entry already filled — not an error condition.
            logger.info("entry cancel for %s (%s) not accepted: %s",
                        ticker, order_id, res.get("error"))
        return res

    def on_exit(self, ticker: str, window_key: Any, exit_price_cents: int) -> dict[str, Any]:
        """Defensive-exit: CANCEL any still-resting entry, then SELL the open position at
        ``exit_price_cents``. The kill switch blocks even exits (you can always close
        manually); everything else allows them — closing risk is never gated by the
        daily/window caps."""
        if not self.cfg.enabled:
            return {"placed": False, "reason": "DISABLED"}
        # Cancel BEFORE selling, and do it even when we believe we hold nothing: after a
        # restart the in-memory position is whatever the store rehydrated, and a resting
        # GTC buy must come off the book regardless.
        cancel_res = self._cancel_resting_entry(ticker, window_key)
        count = self.state.positions.get(ticker, 0)
        if count < 1:
            # Nothing to sell, but a cancel may still have pulled a working entry — say so
            # rather than reporting a bare NO_POSITION the caller discards.
            reason = "NO_POSITION_ENTRY_CANCELLED" if cancel_res and cancel_res.get("ok") \
                else "NO_POSITION"
            logger.warning("exit for %s w%s: %s (no in-memory position to sell)",
                           ticker, window_key, reason)
            return {"placed": False, "reason": reason, "cancel": cancel_res}
        # Price the close to actually fill: it goes out immediate-or-cancel (reduce_only), so a
        # mid-priced sell would cancel unfilled. Sell exit_limit_offset_cents UNDER the estimated
        # exit value to cross the resting bid. Clamp to a tradeable cent (the side/price mapper
        # clamps the wire price into [1,99] as well).
        limit_px = max(1, int(exit_price_cents) - int(self.cfg.exit_limit_offset_cents))
        res = self.client.place_order(
            ticker=ticker, side=self.entry_side, count=count, price_cents=limit_px,
            action="sell", client_order_id=_coid(window_key, ticker, "exit", self.coid_prefix),
        )
        fill_status = None
        if self.store is not None:
            from types import SimpleNamespace
            _pick = SimpleNamespace(ticker=ticker, asset="", interval="", window_key=window_key)
            _dec = SimpleNamespace(count=count, limit_price_cents=limit_px, stake_cents=None)
            fill_status, _ = self.store.record_order_result(
                action="exit", pick=_pick, decision=_dec, res=res, age_ms=None,
                bal_ms=None, order_ms=None,
                client_order_id=_coid(window_key, ticker, "exit", self.coid_prefix))
        if res.get("ok"):
            self.state = apply_exit(self.state, ticker)   # close it out of the snapshot
            key = self._entry_key(ticker, window_key)
            if key is not None:
                self._entry_orders.pop(key, None)
        return {"placed": bool(res.get("ok")), "count": count,
                "mode": "dry-run" if res.get("dry_run") else "LIVE", "order": res,
                "cancel": cancel_res, "fill_status": fill_status}


_executor: Executor | None = None
_yes_executor: Executor | None = None


def _refresh_safety_switches(ex: "Executor", cfg: ExecutorConfig) -> None:
    """Re-apply the two SAFETY switches from the environment onto a live executor.

    ExecutorConfig is frozen and the singleton captured it at boot, so setting
    Q15_EXEC_KILL=true on a running process used to do nothing — the documented
    "panic button ... hard-blocks ALL placement regardless" only took effect after
    a restart. Only kill_switch and dry_run are refreshed: they can exclusively
    make the executor SAFER, so re-reading them cannot widen risk mid-session.
    Sizing/gating fields deliberately stay pinned to boot values, so a half-edited
    environment can never change position sizing under a running book."""
    if ex.cfg.kill_switch == cfg.kill_switch and ex.cfg.dry_run == cfg.dry_run:
        return
    from dataclasses import replace
    logger.warning("executor safety switches changed: kill %s->%s, dry_run %s->%s",
                   ex.cfg.kill_switch, cfg.kill_switch, ex.cfg.dry_run, cfg.dry_run)
    ex.cfg = replace(ex.cfg, kill_switch=cfg.kill_switch, dry_run=cfg.dry_run)
    # The trading client holds its own reference and gates live_ready on it.
    client_cfg = getattr(ex.client, "cfg", None)
    if client_cfg is not None:
        try:
            ex.client.cfg = replace(client_cfg, kill_switch=cfg.kill_switch,
                                    dry_run=cfg.dry_run)
        except TypeError:  # a test double with a non-dataclass cfg
            pass


def get_executor() -> Executor | None:
    """Return the singleton NO executor, or None when disabled (default)."""
    global _executor
    cfg = ExecutorConfig.from_env()
    if not cfg.enabled:
        return None
    if _executor is None:
        _executor = Executor(cfg)
    else:
        _refresh_safety_switches(_executor, cfg)
    return _executor


def get_yes_executor() -> Executor | None:
    """Return the singleton live "YES bot" executor, or None when disabled (default).

    A SEPARATE instance from get_executor(): its own ExecutorConfig (yes_config_from_env, with
    no_only=False + the YES admission gates), its own PortfolioState, its own orders db, and its
    own client_order_id prefix ("v2xy") — so it shares NO mutable state with the NO executor and
    cannot collide with it at the exchange. Gated on Q15_EXEC_YES_ENABLED (default off)."""
    global _yes_executor
    cfg = yes_config_from_env()
    if not cfg.enabled:
        return None
    if _yes_executor is None:
        _yes_executor = Executor(cfg)
    else:
        _refresh_safety_switches(_yes_executor, cfg)
    return _yes_executor


def reset_executor_for_tests() -> None:
    global _executor, _yes_executor
    _executor = None
    _yes_executor = None
