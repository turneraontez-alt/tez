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


def _stake_map(name: str) -> dict:
    """Parse "INTERVAL:CENTS[,INTERVAL:CENTS...]" (e.g. "10M:10000,7M:5000") into
    {interval -> cents}. Empty / malformed entries are skipped; default empty = no override."""
    out: dict[str, int] = {}
    for part in (os.environ.get(name, "") or "").split(","):
        part = part.strip()
        if ":" not in part:
            continue
        key, _, val = part.partition(":")
        try:
            cents = int(float(val))
        except (TypeError, ValueError):
            continue
        if cents > 0:
            out[key.strip().upper()] = cents
    return out


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

    # --- Sizing. Owner rule (current): FLAT $75 per pick, 1 pick/window. The % fields below are
    # the FALLBACK used only when flat_stake_cents=0 (then ~4%/pick, ~8%/window). ---
    # Bankroll the % sizing is computed against, in CENTS. If 0, the executor reads the
    # live Kalshi balance instead (live mode only); a fixed value is safer for testing.
    bankroll_cents: int = field(default_factory=lambda: _int("Q15_EXEC_BANKROLL_CENTS", 0))
    per_pick_pct: float = field(default_factory=lambda: _float("Q15_EXEC_PER_PICK_PCT", 0.04))
    max_picks_per_window: int = field(default_factory=lambda: _int("Q15_EXEC_MAX_PICKS_PER_WINDOW", 1))
    # CONDITIONAL gate on the 2nd-or-later pick in a window: it must have ask (price) >= this many
    # cents, else it is refused (SECOND_PICK_ASK_FLOOR). The FIRST pick of a window is never gated.
    # DEFAULT 0 = OFF / no condition (the gate is `price >= 0`, always true -> byte-identical). Pairs
    # with max_picks_per_window>1: when a 2nd pick is allowed at all, this keeps it in the proven
    # expensive-NO band. On the v2 ledger the 2nd-cheapest-ask pick is +EV overall (78.7%, +8.9c/bet,
    # n=47) but its edge lives entirely at ask>=60 (82.9%, +10.7c/bet, n=41); the sub-60 cheap-NO band
    # is the loss zone (50%, -3.5c/bet, n=6). 60 is the clean cut. Set Q15_EXEC_SECOND_PICK_MIN_ASK.
    second_pick_min_ask: int = field(default_factory=lambda: _int("Q15_EXEC_SECOND_PICK_MIN_ASK", 0))
    # Hard ceiling on TOTAL stake committed to one (settlement) window, as a fraction of
    # bankroll — the correlation guard (picks in a window co-settle ~76%). Never exceeded
    # even if per_pick_pct * max_picks would.
    max_per_window_pct: float = field(default_factory=lambda: _float("Q15_EXEC_MAX_PER_WINDOW_PCT", 0.08))
    # Stop opening NEW entries once the day's realized P&L is down this fraction of the
    # day-start bankroll (the daily circuit breaker, %-based). Exits still allowed.
    daily_loss_limit_pct: float = field(default_factory=lambda: _float("Q15_EXEC_DAILY_LOSS_LIMIT_PCT", 0.20))
    # ABSOLUTE-dollar stop loss in CENTS. When > 0 it GOVERNS the daily stop (the % above is
    # ignored): halt new entries once the day's realized loss reaches this many cents. Owner
    # default $100 (10000c). Fed from the live balance (executor._refresh_daily_pnl); exits are
    # never blocked. 0 = use the %-based limit instead. Set Q15_EXEC_DAILY_LOSS_LIMIT_CENTS.
    daily_loss_limit_cents: int = field(default_factory=lambda: _int("Q15_EXEC_DAILY_LOSS_LIMIT_CENTS", 10000))
    max_open_positions: int = field(default_factory=lambda: _int("Q15_EXEC_MAX_OPEN_POSITIONS", 6))
    # HARD per-pick stake ceiling in CENTS — the absolute most one trade can risk, on top of
    # the % sizing. Default $75 (owner-set; raised from $50): even with a big bankroll, no
    # single contract order risks more than this. NOTE: with per_pick_pct=4% this cap only
    # BINDS once bankroll exceeds ~$1,875 (4% * 1875 = $75); below that the % sizing is the
    # smaller, binding limit. 0 = no cap (rely on per_pick_pct only).
    max_stake_per_pick_cents: int = field(default_factory=lambda: _int("Q15_EXEC_MAX_STAKE_PER_PICK_CENTS", 7500))
    # FLAT per-pick stake in CENTS. When > 0 it OVERRIDES the % sizing: every pick stakes exactly
    # this fixed dollar amount (still bounded by the bankroll on hand and the hard per-pick cap
    # above; the per-window budget becomes flat * max_picks_per_window). Owner default $75 with
    # max_picks_per_window=1 -> a flat $75 on the single best pick each window. Set
    # Q15_EXEC_FLAT_STAKE_CENTS=0 to fall back to per_pick_pct % sizing.
    flat_stake_cents: int = field(default_factory=lambda: _int("Q15_EXEC_FLAT_STAKE_CENTS", 7500))
    # INTERVAL-CONDITIONED stake override in CENTS, e.g. "10M:10000" -> $100 on 10M picks. DEFAULT
    # empty = flat_stake_cents for every interval (byte-identical). When set for a pick's interval,
    # that amount is BOTH the stake AND the per-pick ceiling for that interval (it supersedes
    # flat_stake_cents and max_stake_per_pick_cents) — concentrating capital on the proven +EV
    # interval (10M: 80.6%/+1171c, n=103; the only interval with a positive margin over breakeven).
    # Still bounded by the bankroll on hand and the $100 daily stop (checked BEFORE sizing), so a
    # bigger 10M stake cannot bypass the circuit breaker. NOTE: a single losing 10M NO at $100 is
    # exactly the -$100 daily stop in one trade — deliberate concentration, gated. Intervals not
    # listed keep flat_stake_cents. Set Q15_EXEC_STAKE_BY_INTERVAL.
    stake_by_interval: dict = field(default_factory=lambda: _stake_map("Q15_EXEC_STAKE_BY_INTERVAL"))
    # CONVICTION sizing. DEFAULT ON. When a v2 pick carries stake_multiplier>1 (a >=3-co-trigger
    # 10M window), size up the 2nd-or-later pick of that window by the multiplier — the LEAD pick
    # of every window always stays at the base stake (owner: "first $150, extras double"). The
    # hard per-pick cap must be high enough to admit the doubled extra (else it clamps). Leverage
    # on thin (~3-day) data — set Q15_EXEC_CONVICTION_SIZING=false to size every pick flat.
    conviction_sizing: bool = field(
        default_factory=lambda: _bool("Q15_EXEC_CONVICTION_SIZING", True)
    )

    # GLOBAL entry ask floor (applies to EVERY pick, incl. the 1st — distinct from
    # second_pick_min_ask which gates only 2nd-or-later picks, and from the min_price_cents sanity
    # band). DEFAULT 0 = OFF (the gate is `price >= 0`, always true -> byte-identical). When > 0,
    # refuse any fired NO whose entry ask < this many cents (ENTRY_ASK_FLOOR). Data: full-sample,
    # fired NO at ask<60 is net-NEGATIVE (n=43, 46% win, -300c) and a flat floor 60 lifts the kept
    # book +300c full-sample. BUT that benefit is concentrated in the single June-24 19-23 UTC
    # regime evening; out-of-evening the cheap band is POSITIVE (ask<60 +196c/30) and every floor
    # REDUCES the kept book. The only evening-INDEPENDENT loss band is [55,58) (-146c/8, thin), and
    # 10M cheap NO is break-even. Recommended floor if used: 58. Verdict on the lever was WEAK, so
    # this stays default-OFF; a regime/breadth circuit-breaker — not a price floor — is the real
    # lever for the bad windows. Set Q15_EXEC_MIN_ENTRY_ASK.
    min_entry_ask: int = field(default_factory=lambda: _int("Q15_EXEC_MIN_ENTRY_ASK", 0))

    # --- Order sanity band (refuse anything outside it — defence in depth vs a bad signal) ---
    min_price_cents: int = field(default_factory=lambda: _int("Q15_EXEC_MIN_PRICE_CENTS", 50))
    max_price_cents: int = field(default_factory=lambda: _int("Q15_EXEC_MAX_PRICE_CENTS", 85))
    # Only the NO side is traded (matches v2). YES fires are never executed.
    no_only: bool = field(default_factory=lambda: _bool("Q15_EXEC_NO_ONLY", True))
    # Interval allowlist — DEFAULT empty = ALLOW-ALL (byte-identical). A defence-in-depth
    # backstop independent of v2's own skip_15m / research_only gating: if set (e.g.
    # "10M,7M"), the executor REFUSES a fire whose interval is not listed, so a regression in
    # v2's interval gating cannot leak a structurally -EV 15M/12M order onto real money. The
    # interval is informational only — it NEVER enters the client_order_id (idempotency keys on
    # window+ticker+kind), so adding it cannot break dedup. Set Q15_EXEC_ALLOWED_INTERVALS.
    allowed_intervals: frozenset = field(
        default_factory=lambda: frozenset(
            s.strip().upper() for s in os.environ.get("Q15_EXEC_ALLOWED_INTERVALS", "").split(",")
            if s.strip()))
    # Limit orders by default (you set the price). Limit at the signalled ask + this offset
    # (0 = pay the ask; negative = try to fill cheaper, may not fill).
    limit_offset_cents: int = field(default_factory=lambda: _int("Q15_EXEC_LIMIT_OFFSET_CENTS", 0))
    # Defensive-EXIT marketability. A close is sent immediate-or-cancel (see trading_client), so it
    # only fills against liquidity ALREADY resting. The estimated exit value is ~mid, which sits BELOW
    # the resting ask, so a mid-priced IoC close would usually cancel UNFILLED — leaving us in the very
    # position we wanted out of. Sell this many cents UNDER the estimated exit value to cross the bid
    # and actually close. The exit fires only on an ~84%-correct decisive flip (live n=51), so the
    # certainty of getting out is worth a few cents of give-up. 0 = price at the raw exit value (may
    # not fill). Set Q15_EXEC_EXIT_LIMIT_OFFSET_CENTS.
    exit_limit_offset_cents: int = field(default_factory=lambda: _int("Q15_EXEC_EXIT_LIMIT_OFFSET_CENTS", 3))
    # BTC CROSS-ASSET ENTRY GATE — owner LIVE, default ON. Suppress an alt-NO entry when BTC is
    # contemporaneously bullish (btc_lean >= btc_gate_lean) OR the complex is risk-on (prior-window
    # breadth >= btc_gate_breadth) — the regime where alt-NO fails because the alts co-move up with
    # BTC. EXEMPTS >=3-co-trigger 10M conviction windows (stake_multiplier>1), which are ~100%
    # accurate and protected by the defensive exit, so they run at full size. Only acts on a present
    # signal; no same-window-settlement lookahead. Backtest (in-sample ~2 days, ~560 windows): lifts
    # kept-book accuracy and P&L. KILL SWITCH: set Q15_EXEC_BTC_GATE=false to disable instantly.
    btc_gate_enabled: bool = field(default_factory=lambda: _bool("Q15_EXEC_BTC_GATE", True))
    btc_gate_lean: float = field(default_factory=lambda: _float("Q15_EXEC_BTC_GATE_LEAN", 0.5))
    btc_gate_breadth: float = field(default_factory=lambda: _float("Q15_EXEC_BTC_GATE_BREADTH", 0.5))
    # YES-SIDE ADMISSION GATES — consumed by risk.decide(), set ONLY by the separate live "YES bot"
    # config (yes_config_from_env). INERT plain defaults (0.0/0.0/empty) and DELIBERATELY NOT read
    # from env here, so a default ExecutorConfig() (the NO executor) always has these off regardless
    # of any Q15_EXEC_YES_* env — keeping NO decide() byte-identical and preventing env cross-read.
    min_yes_prob: float = 0.0          # require v2 P(YES) >= this (YES bot: 0.55); 0 = inert
    min_btc_lean: float = 0.0          # require BTC contemporaneously bullish, lean >= this (0.55); 0 = inert
    excluded_assets: frozenset = frozenset()   # refuse these assets outright (YES bot: {"BNB"}); empty = inert
    # ORDER RECORDING (default ON when the executor runs). Persists every placement + the RAW
    # broker response + a best-effort fill classification to its own sqlite, so "how many orders
    # missed (placed-but-unfilled)" is answerable from our side and the optimistic in-memory fill
    # is observable. Read/append only; a store failure never disrupts an order. Set
    # Q15_EXEC_RECORD_ORDERS=false to disable.
    record_orders: bool = field(default_factory=lambda: _bool("Q15_EXEC_RECORD_ORDERS", True))
    orders_db_path: str = field(
        default_factory=lambda: os.environ.get("Q15_EXEC_ORDERS_DB")
        or "data/q15_executor_orders_v1.sqlite3")

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
        cap = f"${self.max_stake_per_pick_cents/100:.0f}/pick cap" if self.max_stake_per_pick_cents > 0 else "no per-pick cap"
        if self.flat_stake_cents > 0:
            size = f"FLAT ${self.flat_stake_cents/100:.0f}/pick ({cap})"
        else:
            size = f"{self.per_pick_pct*100:.0f}%/pick ({cap})"
        if self.stake_by_interval:
            by = ",".join(f"{k}=${v/100:.0f}" for k, v in sorted(self.stake_by_interval.items()))
            size += f"; by-interval {by}"
        if self.daily_loss_limit_cents > 0:
            stop = f"stop -${self.daily_loss_limit_cents/100:.0f}"
        elif self.daily_loss_limit_pct > 0:
            stop = f"daily-stop -{self.daily_loss_limit_pct*100:.0f}%"
        else:
            stop = "NO daily stop (no circuit breaker)"
        gate = (f"BTC-gate ON (lean>={self.btc_gate_lean:.2f}/breadth>={self.btc_gate_breadth:.2f}, "
                f"conviction-exempt)" if self.btc_gate_enabled else "BTC-gate OFF")
        return (f"EXECUTOR ENABLED — {mode}; size {size}, "
                f"<= {self.max_picks_per_window} pick(s)/window, <= {self.max_per_window_pct*100:.0f}%/window, "
                f"{stop}; {gate}")


def _csv_set(name: str, default: str) -> frozenset:
    """Parse a CSV env (e.g. "BNB" or "10M,7M") into an upper-cased frozenset."""
    raw = os.environ.get(name)
    src = default if raw is None else raw
    return frozenset(s.strip().upper() for s in src.split(",") if s.strip())


def yes_config_from_env() -> ExecutorConfig:
    """Build the SEPARATE live "YES bot" executor config from the Q15_EXEC_YES_* env namespace.

    This is a DISTINCT ExecutorConfig instance (own state, own orders db, own client_order_id
    prefix via Executor) that never shares mutable state with the NO executor. Defaults encode the
    data-backed inverse-v2 10M YES rule: take a YES bet when v2 leans YES AND BTC is contemporaneously
    bullish (lean>=0.55) AND v2 P(YES)>=0.55 AND the asset is not excluded (BNB) — flat $150/pick,
    up to 2 picks/window ($300), NO conviction doubling.

    SAFETY: dry_run defaults TRUE and is read ONLY from Q15_EXEC_YES_DRY_RUN — it NEVER inherits the
    NO book's Q15_EXEC_DRY_RUN. Going live is a deliberate, separate flip of Q15_EXEC_YES_DRY_RUN=false.
    The kill switch honours EITHER the YES-specific or the global panic button.
    """
    return ExecutorConfig(
        enabled=_bool("Q15_EXEC_YES_ENABLED", False),
        dry_run=_bool("Q15_EXEC_YES_DRY_RUN", True),
        kill_switch=_bool("Q15_EXEC_YES_KILL", False) or _bool("Q15_EXEC_KILL", False),
        base_url=os.environ.get("Q15_EXEC_BASE_URL") or BASE_URL,
        no_only=False,                        # the whole point: YES is allowed
        # Sizing: flat $150/pick, up to 2/window ($300), hard $150 per-pick cap, NO doubling.
        bankroll_cents=_int("Q15_EXEC_YES_BANKROLL_CENTS", 0),   # 0 -> read live balance (live mode)
        flat_stake_cents=_int("Q15_EXEC_YES_FLAT_STAKE_CENTS", 15000),
        max_stake_per_pick_cents=_int("Q15_EXEC_YES_MAX_STAKE_PER_PICK_CENTS", 15000),
        max_picks_per_window=_int("Q15_EXEC_YES_MAX_PICKS_PER_WINDOW", 2),
        conviction_sizing=False,              # owner: "no double just yet"
        # Daily stop OFF (parity with the NO book's current posture); per-pick + per-window are the guards.
        daily_loss_limit_cents=_int("Q15_EXEC_YES_DAILY_LOSS_LIMIT_CENTS", 0),
        daily_loss_limit_pct=_float("Q15_EXEC_YES_DAILY_LOSS_LIMIT_PCT", 0.0),
        max_open_positions=_int("Q15_EXEC_YES_MAX_OPEN_POSITIONS", 6),
        # Price band wide enough for the YES favourites in the rule (the 96% set ran 56–97¢; a
        # 50–85 band would silently drop the 92–97¢ winners). YES ask is the buy price.
        min_price_cents=_int("Q15_EXEC_YES_MIN_PRICE_CENTS", 50),
        max_price_cents=_int("Q15_EXEC_YES_MAX_PRICE_CENTS", 99),
        limit_offset_cents=_int("Q15_EXEC_YES_LIMIT_OFFSET_CENTS", 1),
        # Defence-in-depth interval allowlist (the runner already only routes 10M here).
        allowed_intervals=_csv_set("Q15_EXEC_YES_ALLOWED_INTERVALS", "10M"),
        btc_gate_enabled=False,               # the NO-side BTC SUPPRESSION gate is irrelevant to YES
        # THE YES RULE — the three admission gates (fail-closed):
        min_yes_prob=_float("Q15_EXEC_YES_MIN_PYES", 0.55),
        min_btc_lean=_float("Q15_EXEC_YES_MIN_BTC_LEAN", 0.55),
        excluded_assets=_csv_set("Q15_EXEC_YES_EXCLUDE_ASSETS", "BNB"),
        record_orders=_bool("Q15_EXEC_YES_RECORD_ORDERS", True),
        orders_db_path=os.environ.get("Q15_EXEC_YES_ORDERS_DB") or "data/q15_executor_yes_orders_v1.sqlite3",
    )
