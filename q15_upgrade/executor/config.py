"""Ultoim V2 EXECUTOR — configuration.

This is a SEPARATE, DEFAULT-OFF, opt-in layer that turns v2's paper fire signals
into REAL Kalshi orders. v2 itself stays read-only; this package is the ONLY place
that can place/cancel a live order, and it is double-gated:

  * ``enabled`` (default False)  — the whole executor is inert unless set.
  * ``dry_run``  (default True)  — even when enabled, orders are LOGGED, not sent,
    until ``dry_run`` is explicitly turned off. So the safe progression is:
        enabled=false (nothing) -> enabled=true,dry_run=true (logs would-be orders)
        -> enabled=true,dry_run=false (LIVE money).

A ``kill_switch`` env (Q15_EXEC_KILL=true) hard-blocks ALL placement regardless of
the above — the panic button. Sizing defaults encode the owner's chosen rule:
2 picks/window at ~4% of bankroll each (a ~8% per-window cap), never 15%.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# Kalshi v2 trade API. Orders POST to {base}/portfolio/orders.
BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ExecutorConfig:
    # MASTER SWITCH — default OFF. With this unset the executor never loads a signer,
    # never opens a socket, never places anything.
    enabled: bool = field(default_factory=lambda: _bool("Q15_EXEC_ENABLED", False))
    # DRY RUN — default ON even when enabled. place_order LOGS the order it WOULD send
    # and returns a simulated ack; NO network call, NO real money. Set =false to go live.
    dry_run: bool = field(default_factory=lambda: _bool("Q15_EXEC_DRY_RUN", True))
    # PANIC BUTTON — when true, blocks ALL placement (entries AND exits) regardless of the
    # switches above. Leaves existing positions alone; just stops new orders.
    kill_switch: bool = field(default_factory=lambda: _bool("Q15_EXEC_KILL", False))
    base_url: str = field(default_factory=lambda: os.environ.get("Q15_EXEC_BASE_URL") or BASE_URL)

    # --- Sizing (owner rule: 2 picks/window @ ~4% each, ~8% per-window cap) ---
    # Bankroll the % sizing is computed against, in CENTS. If 0, the executor reads the
    # live Kalshi balance instead (live mode only); a fixed value is safer for testing.
    bankroll_cents: int = field(default_factory=lambda: _int("Q15_EXEC_BANKROLL_CENTS", 0))
    per_pick_pct: float = field(default_factory=lambda: _float("Q15_EXEC_PER_PICK_PCT", 0.04))
    max_picks_per_window: int = field(default_factory=lambda: _int("Q15_EXEC_MAX_PICKS_PER_WINDOW", 2))
    # Hard ceiling on TOTAL stake committed to one (settlement) window, as a fraction of
    # bankroll — the correlation guard (picks in a window co-settle ~76%). Never exceeded
    # even if per_pick_pct * max_picks would.
    max_per_window_pct: float = field(default_factory=lambda: _float("Q15_EXEC_MAX_PER_WINDOW_PCT", 0.08))
    # Stop opening NEW entries once the day's realized P&L is down this fraction of the
    # day-start bankroll (the daily circuit breaker). Exits still allowed.
    daily_loss_limit_pct: float = field(default_factory=lambda: _float("Q15_EXEC_DAILY_LOSS_LIMIT_PCT", 0.20))
    max_open_positions: int = field(default_factory=lambda: _int("Q15_EXEC_MAX_OPEN_POSITIONS", 6))

    # --- Order sanity band (refuse anything outside it — defence in depth vs a bad signal) ---
    min_price_cents: int = field(default_factory=lambda: _int("Q15_EXEC_MIN_PRICE_CENTS", 50))
    max_price_cents: int = field(default_factory=lambda: _int("Q15_EXEC_MAX_PRICE_CENTS", 85))
    # Only the NO side is traded (matches v2). YES fires are never executed.
    no_only: bool = field(default_factory=lambda: _bool("Q15_EXEC_NO_ONLY", True))
    # Limit orders by default (you set the price). Limit at the signalled ask + this offset
    # (0 = pay the ask; negative = try to fill cheaper, may not fill).
    limit_offset_cents: int = field(default_factory=lambda: _int("Q15_EXEC_LIMIT_OFFSET_CENTS", 0))

    @classmethod
    def from_env(cls) -> "ExecutorConfig":
        return cls()

    def safety_summary(self) -> str:
        """One-line human description of the current safety posture — logged at start."""
        if not self.enabled:
            return "EXECUTOR DISABLED (Q15_EXEC_ENABLED unset)"
        if self.kill_switch:
            return "EXECUTOR ENABLED but KILL SWITCH ON — no orders will be placed"
        mode = "DRY-RUN (orders logged, nothing sent)" if self.dry_run else "*** LIVE REAL MONEY ***"
        return (f"EXECUTOR ENABLED — {mode}; size {self.per_pick_pct*100:.0f}%/pick, "
                f"<= {self.max_picks_per_window} picks/window, <= {self.max_per_window_pct*100:.0f}%/window, "
                f"daily-stop -{self.daily_loss_limit_pct*100:.0f}%")
