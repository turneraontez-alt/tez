#!/usr/bin/env python3
"""V2 (ultoim_v2) account auditor — sure-fire, self-checking P&L + EV report.

Read-only auditor of the V2 paper-trading system. It NEVER writes to, migrates,
or mutates the live ledgers (``ultoim_v2_predictions`` / ``interval_captures``);
it only opens them read-only and, when ``--save-snapshot`` is passed, writes its
own snapshot JSON. It reconciles every canonical trade's P&L three independent
ways, computes EV per trade in ``Decimal``, dedups to exactly one canonical row
per (ticker, window_key) at the 10M-then-7M checkpoint (no look-ahead, no
duplicate 15-minute windows), and shows a clear before/after diff against the
prior snapshot.

Why a separate tool: other ad-hoc reports get the numbers wrong. This one
derives the same money math the live ``resolve()`` uses (mirrored, never called),
cross-checks it against the chart store, and flags every discrepancy — so the
account total is provably correct rather than asserted.

Design / safety:
  * STRICTLY READ-ONLY on every live ledger. SQLite is opened with
    ``mode=ro`` (file URI) so even a stray DDL/UPDATE cannot run. The live
    ``resolve()`` methods (which UPDATE+commit) are never invoked.
  * All money / EV is computed in ``Decimal`` (cents). Stored values were
    written as floats by the live path, so reconciliation uses a >0.01¢
    tolerance rather than bit-exact equality.
  * Chart-data extension reuses the REAL gate
    (``q15_upgrade.ultoim_v2.gate.evaluate`` + ``UltoimV2Config.from_env()``) —
    V2's CURRENT rules, never re-implemented — and is kept in a SEPARATE
    "would-have" book, never merged into confirmed account P&L.
  * Empty / missing DB → ``available: False``; thin data → ``INSUFFICIENT``.
    Never crashes, never fabricates a number.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Generous precision so a long Σ of cent values never loses a sub-cent digit.
getcontext().prec = 50

# --------------------------------------------------------------------------- #
# Constants / defaults
# --------------------------------------------------------------------------- #
DEFAULT_V2_DB = "data/q15_ultoim_v2_v1.sqlite3"
DEFAULT_CAPTURES_DB = "data/q15_interval_research_v1.sqlite3"
DEFAULT_SNAPSHOT = "data/v2_audit_snapshot.json"
DEFAULT_MODEL_VERSION = "ultoim-v2"
# The interval_captures store has its OWN model_version namespace, independent of
# the V2 ledger's: the live interval-research ledger records under
# "interval-research-v1" (q15_upgrade/interval_research/config.py), NOT "ultoim-v2".
# The chart cross-check and the chart-replay extension MUST query captures with this,
# or they silently match zero rows.
DEFAULT_CAPTURES_MODEL_VERSION = "interval-research-v1"
# The delivered checkpoints, highest-priority first. 15M is skipped/weak and
# 12M/11M are research-only in V2 — they are NOT delivered trades, so they are
# excluded from the canonical grading set by default.
DEFAULT_GRADE_INTERVALS = ("10M", "7M")
DEFAULT_MIN_SCOREBOARD_N = 30

# Reconciliation tolerance. Stored P&L was computed in float by the live
# ``resolve()``; our Decimal mirror must agree within a hundredth of a cent.
PNL_TOLERANCE = Decimal("0.01")

_HUNDRED = Decimal("100")
_ZERO = Decimal("0")

# Columns we read from a V2 prediction row. Read-only SELECT * gives us all of
# them; this list documents the load-bearing set.
_V2_FIELDS = (
    "id", "model_version", "asset", "ticker", "interval", "window_key",
    "fired", "predicted_side", "selected_probability",
    "calibrated_yes_probability", "conservative_probability", "net_edge_cents",
    "entry_ask_cents", "total_cost_cents", "spread_cents", "distance_sigma",
    "close_time", "official_result", "correct", "hypothetical_pnl_cents",
    "stake_multiplier",
)


# --------------------------------------------------------------------------- #
# C5 config fingerprint — every gate/runner-relevant field and its C5 value.
# The live owner preset is C5 (pinned in .replit); it is field-for-field identical
# to the UltoimV2Config dataclass defaults today, so a bare from_env() reproduces
# strict C5. This dict makes that provable rather than assumed: each report AND
# snapshot stamps the RESOLVED value of these fields plus is_strict_c5, and a stray
# Q15_ULTOIM_V2_* env override that changes a replayed gate rule is surfaced LOUDLY
# (a WARNING listing every divergent field) instead of silently altering the book.
# --------------------------------------------------------------------------- #
C5_PINS: dict[str, Any] = {
    "no_only": False,
    "min_confidence": 0.55,
    "ask_lo": 50.0,
    "ask_hi": 72.0,
    "min_edge_cents": 2.0,
    "no_edge_waive": False,
    "expensive_no_enabled": True,
    "expensive_no_ask_hi": 78.0,
    "cap_7m_ask": False,
    "distance_gate_enabled": True,
    "distance_pin_sigma": 0.15,
    "btc_confirm_enabled": True,
    "btc_confirm_margin": 0.15,
    "btc_confirm_margin_yes": 0.10,
    "require_inverse_edge": True,
    "deliver_12m": False,
    "skip_15m": True,
    "skip_7m": False,
    "deliver_top_n": 3,
    "deliver_by_reward_risk": True,
    "skip_12m_unless_min": True,
    "min_triggers_12m": 3,
    "double_10m_on_min": True,
    "min_triggers_10m": 3,
    "double_stake": 2,
    "enable_11m": True,
    "enable_12m": True,
}


def config_fingerprint(cfg: Any) -> dict[str, Any]:
    """Read the C5-pinned fields from the cfg actually used. Missing attributes
    (an older cfg shape) surface as None so a divergence is reported, never
    silently treated as a match."""
    return {name: getattr(cfg, name, None) for name in C5_PINS}


def _pin_equal(resolved: Any, pinned: Any) -> bool:
    """Compare a resolved cfg value to its C5 pin. Numerics compare by Decimal
    value (so 0.10 == Decimal('0.1') and 3 == 3.0), bools/strings by equality.
    bool is NOT a numeric here (True must not equal 1)."""
    if isinstance(resolved, bool) or isinstance(pinned, bool):
        return resolved == pinned
    rd, pd = _dec(resolved), _dec(pinned)
    if rd is not None and pd is not None:
        return rd == pd
    return resolved == pinned


def resolve_fingerprint(cfg: Any) -> dict[str, Any]:
    """Resolve the config fingerprint and decide strict-C5 status. Returns the
    stamped fingerprint, ``is_strict_c5`` (resolved == every C5 pin), and the list
    of divergent fields (each {field, resolved, pinned})."""
    fp = config_fingerprint(cfg)
    divergences: list[dict[str, Any]] = []
    for name, pinned in C5_PINS.items():
        resolved = fp.get(name)
        if not _pin_equal(resolved, pinned):
            divergences.append({"field": name, "resolved": resolved, "pinned": pinned})
    return {
        "fingerprint": fp,
        "is_strict_c5": not divergences,
        "divergences": divergences,
    }


def cfg_from_pins() -> Any:
    """Build a config object pinned to the C5 values regardless of ambient env
    (``--strict-c5``). Constructs the real ``UltoimV2Config`` then overrides every
    C5-pinned field, so the replay is provably strict-C5 even when the host
    environment exports a Q15_ULTOIM_V2_* override. The dataclass is frozen, so
    ``object.__setattr__`` is used to set the pinned fields."""
    from q15_upgrade.ultoim_v2.config import UltoimV2Config
    cfg = UltoimV2Config()
    for name, pinned in C5_PINS.items():
        object.__setattr__(cfg, name, pinned)
    return cfg


# --------------------------------------------------------------------------- #
# Decimal helpers
# --------------------------------------------------------------------------- #
def _dec(value: Any) -> Decimal | None:
    """Coerce a stored numeric to Decimal via its string form (so the float's
    decimal repr, not its binary tail, is what we reason about), or None.
    Rejects bool and non-finite values."""
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (ValueError, ArithmeticError, TypeError):
        return None
    if not d.is_finite():
        return None
    return d


def _stake(value: Any) -> Decimal:
    """Stake multiplier, defaulting to 1 (matches the live ledger's
    ``stake_multiplier if not None else 1``). A zero/negative/garbage stake
    falls back to 1 to mirror the column DEFAULT, never to silently zero P&L."""
    d = _dec(value)
    if d is None or d <= _ZERO:
        return Decimal("1")
    return d


def _f(d: Decimal | None) -> float | None:
    """JSON-serialisable float view of a Decimal (None passes through)."""
    return None if d is None else float(d)


# --------------------------------------------------------------------------- #
# Money / EV math (the mirror of resolve(), never calling it)
# --------------------------------------------------------------------------- #
def recompute_pnl(correct: int, ask: Decimal, stake: Decimal) -> Decimal:
    """Decimal mirror of the live realized-P&L formula:
    ``((100 - a) if correct else (-a)) * stake`` (ledger.py:462-463)."""
    unit = (_HUNDRED - ask) if correct else (-ask)
    return unit * stake


def side_p_win(predicted_side: str, calibrated_yes: Decimal) -> Decimal:
    """p_win for the chosen side from the calibrated YES probability:
    YES → cal, NO → 1 - cal (AUDIT_SPEC.md:47-48)."""
    if predicted_side == "YES":
        return calibrated_yes
    return Decimal("1") - calibrated_yes


def compute_ev(predicted_side: str, calibrated_yes: Decimal | None,
               ask: Decimal | None, stake: Decimal) -> Decimal | None:
    """EV per trade at the decision checkpoint, in Decimal cents.

    ``EV = (p_win*100 - a) * stake``. Asserts the equivalent expansion
    ``(p_win*(100-a) + (1-p_win)*(-a)) * stake`` agrees exactly (same Decimal
    ``p_win`` feeds both forms, so they are bit-identical). Returns None when the
    side, calibrated probability, or ask is missing — never fabricates an EV.
    """
    side = (predicted_side or "").upper()
    if side not in ("YES", "NO") or calibrated_yes is None or ask is None:
        return None
    p_win = side_p_win(side, calibrated_yes)
    form1 = (p_win * _HUNDRED - ask) * stake
    form2 = (p_win * (_HUNDRED - ask) + (Decimal("1") - p_win) * (-ask)) * stake
    if form1 != form2:  # pragma: no cover - algebraic identity in Decimal
        raise AssertionError(
            f"EV forms disagree: {form1} != {form2} "
            f"(side={side}, p_win={p_win}, ask={ask}, stake={stake})"
        )
    return form1


def _wilson(right: int, n: int, z: float = 1.96) -> tuple[float | None, float | None, float | None]:
    """Wilson score interval — mirrors ledger._wilson so the audit's win-rate CI
    matches the live scoreboard's."""
    if n <= 0:
        return None, None, None
    p = right / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return p, centre - half, centre + half


# --------------------------------------------------------------------------- #
# Read-only DB access
# --------------------------------------------------------------------------- #
def _open_ro(db_path: str) -> sqlite3.Connection | None:
    """Open a SQLite DB strictly read-only (``mode=ro`` URI). Returns None if the
    file does not exist or cannot be opened — the auditor degrades to
    ``available: False`` rather than creating or migrating a DB."""
    p = Path(db_path)
    if not p.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=15.0)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _fetch_v2_rows(conn: sqlite3.Connection, model_version: str,
                   *, delivered_only: bool = False) -> list[dict[str, Any]]:
    """Every prediction row for the model (read-only). Plain dicts so nothing
    downstream depends on a live cursor. ``delivered_only`` restricts to the
    actually-sent (alerted) book — the owner's actionable account — instead of all
    gate-passing ``fired`` rows."""
    q = "SELECT * FROM ultoim_v2_predictions WHERE model_version=?"
    if delivered_only:
        q += " AND delivery_status='SENT'"
    q += " ORDER BY window_key ASC, ticker ASC, interval ASC, id ASC"
    rows = conn.execute(q, (model_version,)).fetchall()
    return [dict(r) for r in rows]


def _fetch_capture_rows(conn: sqlite3.Connection, model_version: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM interval_captures WHERE model_version=? "
        "ORDER BY window_key ASC, ticker ASC, interval ASC, id ASC",
        (model_version,),
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Canonical trade selection
# --------------------------------------------------------------------------- #
@dataclass
class CanonicalTrade:
    """One delivered trade = one (ticker, window_key) graded at its canonical
    checkpoint. Carries the reconciliation result and per-trade EV."""
    model_version: str
    ticker: str
    asset: str | None
    window_key: int
    interval: str
    predicted_side: str | None
    official_result: str | None
    resolved: bool
    correct_stored: int | None
    side_matches_result: bool | None
    ask_cents: Decimal | None
    stake: Decimal
    stored_pnl: Decimal | None
    recomputed_pnl: Decimal | None
    chart_unit_pnl: Decimal | None
    calibrated_yes: Decimal | None
    net_edge_cents: Decimal | None
    ev: Decimal | None
    edge_realization: Decimal | None
    flags: list[str] = field(default_factory=list)

    def key(self) -> str:
        return trade_key(self.model_version, self.ticker, self.window_key, self.interval)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key(),
            "model_version": self.model_version,
            "ticker": self.ticker,
            "asset": self.asset,
            "window_key": self.window_key,
            "interval": self.interval,
            "predicted_side": self.predicted_side,
            "official_result": self.official_result,
            "resolved": self.resolved,
            "correct_stored": self.correct_stored,
            "side_matches_result": self.side_matches_result,
            "ask_cents": _f(self.ask_cents),
            "stake": _f(self.stake),
            "stored_pnl_cents": _f(self.stored_pnl),
            "recomputed_pnl_cents": _f(self.recomputed_pnl),
            "chart_unit_pnl_cents": _f(self.chart_unit_pnl),
            "calibrated_yes_probability": _f(self.calibrated_yes),
            "net_edge_cents": _f(self.net_edge_cents),
            "ev_cents": _f(self.ev),
            "edge_realization_cents": _f(self.edge_realization),
            "flags": list(self.flags),
        }


def trade_key(model_version: str, ticker: str, window_key: int, interval: str) -> str:
    """Stable per-trade key for the snapshot diff:
    (model_version, ticker, window_key, interval)."""
    return f"{model_version}|{ticker}|{int(window_key)}|{interval}"


def _select_canonical(rows: Sequence[Mapping[str, Any]],
                      grade_intervals: Sequence[str]) -> tuple[dict[str, Any] | None, list[str]]:
    """From all fired rows of one TICKER, pick the canonical checkpoint by
    ``grade_intervals`` priority (default 10M then 7M). Returns (row, extra
    flags). Flags a ticker that has fired rows at MULTIPLE gradeable intervals
    (a conflicting-candidate window — still resolved by priority, never
    double-counted) and a ticker whose fired rows carry DIFFERENT stored
    ``window_key`` values (``WINDOW_KEY_DIVERGENCE``: a single Kalshi 15-minute
    contract is window-unique by construction, so divergent stored keys are a
    live-path artifact — see ``_window_key`` falling back to ``int(now//900)``
    when ``close_time`` is None — that must NOT split one contract into two
    trades or double-count its window)."""
    priority = {iv: i for i, iv in enumerate(grade_intervals)}
    candidates = [r for r in rows
                  if int(r.get("fired") or 0) == 1
                  and str(r.get("interval") or "") in priority]
    if not candidates:
        return None, []
    intervals_present = {str(r["interval"]) for r in candidates}
    flags: list[str] = []
    if len(intervals_present) > 1:
        flags.append("MULTI_INTERVAL_WINDOW")
    window_keys_present = {int(r["window_key"]) for r in candidates
                           if r.get("window_key") is not None}
    if len(window_keys_present) > 1:
        flags.append("WINDOW_KEY_DIVERGENCE")
    # Lowest priority index wins; tie-break deterministically on row id.
    chosen = min(candidates, key=lambda r: (priority[str(r["interval"])], int(r.get("id") or 0)))
    return chosen, flags


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #
def _chart_unit_pnl(capture_index: Mapping[tuple, Mapping[str, Any]],
                    ticker: str, interval: str) -> Decimal | None:
    """The matching ``interval_captures`` row's realized_pnl_cents for the same
    (ticker, interval). interval_captures uses stake=1, so it IS the per-unit
    value. None when no matching resolved chart row exists or its P&L is null."""
    row = capture_index.get((ticker, interval))
    if row is None:
        return None
    if row.get("official_result") is None:
        return None
    return _dec(row.get("realized_pnl_cents"))


def reconcile_trade(row: Mapping[str, Any], model_version: str,
                    grade_intervals: Sequence[str],
                    capture_index: Mapping[tuple, Mapping[str, Any]],
                    extra_flags: Sequence[str] = ()) -> CanonicalTrade:
    """Build a fully reconciled CanonicalTrade from the chosen canonical row.

    Computes P&L three ways (stored / recomputed / chart), EV in Decimal, and
    flags every discrepancy per the spec. Pure — no DB access, no mutation."""
    ticker = str(row.get("ticker") or "")
    interval = str(row.get("interval") or "")
    window_key = int(row.get("window_key") or 0)
    side = (str(row.get("predicted_side") or "") or None)
    side_u = (side or "").upper()
    official = row.get("official_result")
    official_u = (str(official) or "").upper() if official is not None else None
    resolved = official is not None

    ask = _dec(row.get("entry_ask_cents"))
    stake = _stake(row.get("stake_multiplier"))
    calibrated_yes = _dec(row.get("calibrated_yes_probability"))
    net_edge = _dec(row.get("net_edge_cents"))
    stored_pnl = _dec(row.get("hypothetical_pnl_cents"))
    correct_stored = row.get("correct")
    correct_stored = None if correct_stored is None else int(correct_stored)

    flags: list[str] = list(extra_flags)

    # side==result truth (only meaningful once resolved with a real side).
    side_matches: bool | None = None
    if resolved and side_u in ("YES", "NO") and official_u in ("YES", "NO"):
        side_matches = (side_u == official_u)

    # recomputed P&L (mirror of resolve()): needs a graded correctness + ask.
    recomputed_pnl: Decimal | None = None
    if resolved and ask is not None and side_matches is not None:
        recomputed_pnl = recompute_pnl(1 if side_matches else 0, ask, stake)

    # chart per-unit P&L for cross-check.
    chart_unit = _chart_unit_pnl(capture_index, ticker, interval)

    # EV (decision-time, independent of settlement).
    ev = compute_ev(side_u, calibrated_yes, ask, stake)

    # edge realization = realized - EV (only when both known).
    edge_realization: Decimal | None = None
    if recomputed_pnl is not None and ev is not None:
        edge_realization = recomputed_pnl - ev

    # ---- DISCREPANCY FLAGS ------------------------------------------------- #
    if resolved and ask is None:
        flags.append("MISSING_ASK")
    if resolved and stake_is_null(row):
        flags.append("MISSING_STAKE")
    if resolved and side_matches is not None and correct_stored is not None:
        if int(side_matches) != correct_stored:
            flags.append("CORRECT_MISMATCH")
    if resolved and correct_stored is None:
        flags.append("RESOLVED_BUT_UNGRADED")
    if (not resolved) and is_closed(row):
        flags.append("CLOSED_BUT_UNRESOLVED")
    # stored vs recomputed
    if stored_pnl is not None and recomputed_pnl is not None:
        if abs(stored_pnl - recomputed_pnl) > PNL_TOLERANCE:
            flags.append("PNL_STORED_MISMATCH")
    elif resolved and ask is not None and stored_pnl is None and recomputed_pnl is not None:
        flags.append("PNL_STORED_MISSING")
    # chart vs ledger per-unit (ledger per-unit = recomputed / stake)
    if chart_unit is not None and recomputed_pnl is not None and stake != _ZERO:
        ledger_unit = recomputed_pnl / stake
        if abs(chart_unit - ledger_unit) > PNL_TOLERANCE:
            flags.append("CHART_PNL_MISMATCH")

    return CanonicalTrade(
        model_version=model_version,
        ticker=ticker,
        asset=(str(row.get("asset")) if row.get("asset") is not None else None),
        window_key=window_key,
        interval=interval,
        predicted_side=(side_u or None),
        official_result=(official_u or None) if resolved else None,
        resolved=resolved,
        correct_stored=correct_stored,
        side_matches_result=side_matches,
        ask_cents=ask,
        stake=stake,
        stored_pnl=stored_pnl,
        recomputed_pnl=recomputed_pnl,
        chart_unit_pnl=chart_unit,
        calibrated_yes=calibrated_yes,
        net_edge_cents=net_edge,
        ev=ev,
        edge_realization=edge_realization,
        flags=flags,
    )


def stake_is_null(row: Mapping[str, Any]) -> bool:
    return _dec(row.get("stake_multiplier")) is None


def is_closed(row: Mapping[str, Any]) -> bool:
    """A row is 'closed' (its window has settled in clock terms) if it carries a
    close_time — used to flag closed-but-unresolved rows. We do not consult the
    wall clock (read-only, deterministic): a present close_time with no
    official_result is the actionable signal."""
    return row.get("close_time") is not None


# --------------------------------------------------------------------------- #
# Top-level canonical-trade load
# --------------------------------------------------------------------------- #
def load_canonical_trades(v2_rows: Sequence[Mapping[str, Any]],
                          capture_rows: Sequence[Mapping[str, Any]],
                          model_version: str,
                          grade_intervals: Sequence[str]) -> list[CanonicalTrade]:
    """Dedup all fired rows to one canonical trade per TICKER at the configured
    checkpoint priority, then reconcile each. No look-ahead: grading uses ONLY the
    chosen checkpoint's side vs result.

    Grouping is by ``ticker`` ALONE (not (ticker, window_key)): a Kalshi 15-minute
    contract is window-unique by construction and its checkpoints always share a
    ticker via ``UNIQUE(model_version, ticker, interval)``. Grouping by window_key
    too would split one contract whose 10M and 7M rows carry DIVERGENT stored
    window_keys (a live-path artifact when ``close_time`` is None and ``_window_key``
    falls back to ``int(now // 900)`` across a 900s boundary) into TWO trades,
    double-counting one 15-minute window and its P&L — exactly the dup the spec
    forbids. Divergent stored keys are surfaced via ``WINDOW_KEY_DIVERGENCE``."""
    # Index chart rows by (ticker, interval) for O(1) cross-check.
    capture_index: dict[tuple, dict[str, Any]] = {}
    for c in capture_rows:
        key = (str(c.get("ticker") or ""), str(c.get("interval") or ""))
        # First write wins (UNIQUE(model_version,ticker,interval) means one row,
        # but be defensive against a duplicated import).
        capture_index.setdefault(key, dict(c))

    # Group prediction rows by ticker (a contract's checkpoints share a ticker).
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for r in v2_rows:
        ticker = str(r.get("ticker") or "")
        if not ticker:
            continue
        if r.get("window_key") is None:
            continue
        groups.setdefault(ticker, []).append(r)

    trades: list[CanonicalTrade] = []
    for _ticker, rows in sorted(groups.items()):
        chosen, extra = _select_canonical(rows, grade_intervals)
        if chosen is None:
            continue
        trades.append(reconcile_trade(chosen, model_version, grade_intervals,
                                      capture_index, extra_flags=extra))
    # Deterministic ordering by the chosen row's representative window then ticker.
    trades.sort(key=lambda t: (t.window_key, t.ticker))
    return trades


# --------------------------------------------------------------------------- #
# Account rollups
# --------------------------------------------------------------------------- #
def _agg_cell(trades: Sequence[CanonicalTrade], min_n: int) -> dict[str, Any]:
    """Aggregate one cell (overall / by interval / by asset / by side). Uses the
    recomputed (Decimal) P&L as the authoritative account number, with EV and
    staked totals. Only graded trades with a recomputed P&L count toward money."""
    graded = [t for t in trades if t.recomputed_pnl is not None]
    n = len(graded)
    right = sum(1 for t in graded if t.side_matches_result)
    pnl_total = _ZERO
    ev_total = _ZERO
    staked_total = _ZERO
    ev_n = 0
    for t in graded:
        pnl_total += t.recomputed_pnl
        if t.ask_cents is not None:
            staked_total += t.ask_cents * t.stake
        if t.ev is not None:
            ev_total += t.ev
            ev_n += 1
    p, lo, hi = _wilson(right, n)
    roi = None
    if staked_total != _ZERO:
        roi = float(pnl_total / staked_total)
    edge_gap = None
    if ev_n == n and n > 0:
        edge_gap = float((pnl_total - ev_total))
    return {
        "n": n,
        "right": right,
        "wrong": n - right,
        "win_rate": p,
        "win_rate_ci_low": lo,
        "win_rate_ci_high": hi,
        "insufficient": n < min_n,
        "pnl_total_cents": _f(pnl_total),
        "ev_total_cents": _f(ev_total) if ev_n else None,
        "ev_n": ev_n,
        "staked_cents": _f(staked_total),
        "roi": roi,
        "edge_realization_cents": edge_gap,
    }


def aggregate_account(trades: Sequence[CanonicalTrade], min_n: int) -> dict[str, Any]:
    """Account rollups: overall, by interval, by asset, by side. Labels any cell
    with n < min_n INSUFFICIENT (raw count preserved)."""
    by_interval: dict[str, Any] = {}
    intervals = sorted({t.interval for t in trades})
    for iv in intervals:
        by_interval[iv] = _agg_cell([t for t in trades if t.interval == iv], min_n)

    by_asset: dict[str, Any] = {}
    assets = sorted({(t.asset or t.ticker) for t in trades})
    for a in assets:
        by_asset[a] = _agg_cell([t for t in trades if (t.asset or t.ticker) == a], min_n)

    by_side: dict[str, Any] = {}
    for sd in ("NO", "YES"):
        by_side[sd] = _agg_cell([t for t in trades if (t.predicted_side or "") == sd], min_n)

    return {
        "overall": _agg_cell(list(trades), min_n),
        "by_interval": by_interval,
        "by_asset": by_asset,
        "by_side": by_side,
        "trade_count": len(trades),
        "resolved_count": sum(1 for t in trades if t.resolved),
        "flagged_count": sum(1 for t in trades if t.flags),
    }


def _trade_pwin(t: CanonicalTrade) -> Decimal | None:
    """Chosen-side win probability for a trade (for 'highest confidence' per-window
    selection). None when the calibrated prob or side is missing."""
    if t.calibrated_yes is None or (t.predicted_side or "") not in ("YES", "NO"):
        return None
    return side_p_win(t.predicted_side, t.calibrated_yes)


def aggregate_per_window(trades: Sequence[CanonicalTrade], min_n: int) -> dict[str, Any]:
    """One-buy-per-15-minute-window account. Across all assets a 15-min window
    yields several candidates, but the owner takes ONE buy — so the per-ticker
    rollup over-counts (n is windows, not ticker-trades). This collapses to one
    trade per ``window_key`` and, because the owner selects MANUALLY, reports the
    ENVELOPE (best / worst realized pick per window) plus the by-rule reference
    points (cheapest ask, most expensive, highest confidence) rather than a single
    false-precision number. Only graded trades count."""
    graded = [t for t in trades if t.recomputed_pnl is not None]
    windows: dict[int, list[CanonicalTrade]] = {}
    for t in graded:
        windows.setdefault(t.window_key, []).append(t)
    for ws in windows.values():  # deterministic tie-break
        ws.sort(key=lambda t: t.ticker)

    def _pick(keyfn, *, want_max: bool) -> list[CanonicalTrade]:
        picks: list[CanonicalTrade] = []
        for ws in windows.values():
            cand = [t for t in ws if keyfn(t) is not None] or ws
            picks.append((max if want_max else min)(cand, key=keyfn))
        return picks

    rules = {
        "best_case": _pick(lambda t: t.recomputed_pnl, want_max=True),
        "worst_case": _pick(lambda t: t.recomputed_pnl, want_max=False),
        "cheapest_ask": _pick(lambda t: t.ask_cents, want_max=False),
        "most_expensive_ask": _pick(lambda t: t.ask_cents, want_max=True),
        "highest_confidence": _pick(_trade_pwin, want_max=True),
    }
    out: dict[str, Any] = {"windows": len(windows), "min_n": min_n}
    for name, picks in rules.items():
        out[name] = _agg_cell(picks, min_n)
    return out


def asset_flag_summary(trades: Sequence[CanonicalTrade], asset: str) -> dict[str, Any]:
    """Always-on drag callout for one asset (the owner keeps HYPE in the book but
    wants its drag flagged on every audit). Reports the asset's standalone graded
    book, the windows it appears in, and the book with that asset removed."""
    a = (asset or "").upper()
    hits = [t for t in trades
            if (t.asset or t.ticker or "").upper() == a and t.recomputed_pnl is not None]
    without = [t for t in trades if (t.asset or t.ticker or "").upper() != a]
    cell = _agg_cell(hits, 0)
    return {
        "asset": a,
        "n_trades": cell["n"],
        "n_windows": len({t.window_key for t in hits}),
        "win_rate": cell["win_rate"],
        "pnl_total_cents": cell["pnl_total_cents"],
        "book_excluding_asset_cents": _agg_cell(without, 0)["pnl_total_cents"],
    }


def collect_discrepancies(trades: Sequence[CanonicalTrade]) -> list[dict[str, Any]]:
    """Every flagged trade as a compact record — the deliverable that answers
    'are the numbers right?'."""
    out = []
    for t in trades:
        if t.flags:
            out.append({"key": t.key(), "ticker": t.ticker,
                        "window_key": t.window_key, "interval": t.interval,
                        "flags": list(t.flags)})
    return out


# --------------------------------------------------------------------------- #
# Chart-data extension (modeled "would-have" book — never confirmed account P&L)
# --------------------------------------------------------------------------- #
def _delivered_window_intervals(trades: Sequence[CanonicalTrade]) -> set[str]:
    """The set of TICKERS V2 already delivered (a contract is window-unique, so a
    ticker that V2 fired is not an 'extension' regardless of which stored
    window_key a chart row for it happens to carry)."""
    return {t.ticker for t in trades}


def _btc_yes_by_window(capture_rows: Sequence[Mapping[str, Any]],
                       grade_intervals: Sequence[str]) -> dict[int, Decimal]:
    """Reconstruct BTC's per-window calibrated P(YES) from the chart store, so the
    replay can feed the live BTC-confirmation gate instead of always failing open.

    For each stored window_key, pick BTC's capture row at the highest-priority
    canonical interval present (10M before 7M) and read its
    ``calibrated_yes_probability``. Mirrors the live runner, which keys the
    BTC-confirmation gate on BTC's contemporaneous calibrated-yes per window
    (runner.py:262-271). A BTC row is identified by ``asset == 'BTC'`` (falling
    back to a ticker that starts with ``BTC``)."""
    priority = {iv: i for i, iv in enumerate(grade_intervals)}
    best: dict[int, tuple[int, Decimal]] = {}
    for c in capture_rows:
        iv = str(c.get("interval") or "")
        if iv not in priority:
            continue
        asset = str(c.get("asset") or "").upper()
        ticker = str(c.get("ticker") or "").upper()
        if asset != "BTC" and not ticker.startswith("BTC"):
            continue
        wk = c.get("window_key")
        if wk is None:
            continue
        cal = _dec(c.get("calibrated_yes_probability"))
        if cal is None:
            continue
        rank = priority[iv]
        cur = best.get(int(wk))
        if cur is None or rank < cur[0]:
            best[int(wk)] = (rank, cal)
    return {wk: cal for wk, (_rank, cal) in best.items()}


def extend_with_chart(capture_rows: Sequence[Mapping[str, Any]],
                      delivered: set[str],
                      cfg: Any, grade_intervals: Sequence[str],
                      gate_evaluate,
                      btc_yes_by_window: Mapping[int, Decimal] | None = None) -> dict[str, Any]:
    """For each TICKER at the canonical interval where V2 did NOT deliver a trade,
    replay the REAL gate against the chart row. If it WOULD have fired, add a
    deduped one-per-ticker 'would-have' trade with its own P&L+EV.

    Reuses V2's CURRENT rules via ``gate_evaluate`` (the real ``gate.evaluate``) +
    ``cfg`` (``UltoimV2Config.from_env()``). Derives the candidate's
    ``selected_probability`` from calibrated_yes + predicted_side (NO → 1-cal,
    YES → cal).

    Grouping is by ticker (same window-uniqueness invariant as the confirmed
    book) so a contract is never split across divergent stored window_keys.

    Per the spec's canonical-selection contract, the per-ticker chart rows are
    PRE-FILTERED to those carrying a usable prediction (side YES/NO + calibrated
    prob + ask) BEFORE the priority min(), so a higher-priority abstain row (a
    ``record_missing`` row with predicted_side NULL) never masks a valid
    lower-priority would-have. This mirrors ``_select_canonical``'s fired-only
    pre-filter.

    BTC confirmation: when ``btc_yes_by_window`` maps the contract's stored
    window_key to BTC's calibrated P(YES) at the canonical interval, that value
    is passed as ``btc_yes_prob`` (matching the live runner). The gate fails OPEN
    (``btc_yes_prob=None``) ONLY when no BTC capture exists for that window — the
    count of such fail-open candidates is surfaced so the over-fire is explicit,
    never silent.
    """
    priority = {iv: i for i, iv in enumerate(grade_intervals)}
    btc_yes_by_window = btc_yes_by_window or {}

    # Group capture rows by ticker, keep only gradeable intervals.
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for c in capture_rows:
        iv = str(c.get("interval") or "")
        if iv not in priority:
            continue
        ticker = str(c.get("ticker") or "")
        if not ticker:
            continue
        if c.get("window_key") is None:
            continue
        groups.setdefault(ticker, []).append(c)

    would_have: list[dict[str, Any]] = []
    btc_fail_open = 0  # would-have trades carrying no BTC context (gate failed open).
    for ticker, rows in sorted(groups.items()):
        if ticker in delivered:
            continue  # V2 already delivered this contract — not an extension.
        # Pre-filter to rows with a usable prediction BEFORE the priority min(),
        # so a higher-priority abstain (predicted_side NULL) cannot mask a valid
        # lower-priority would-have.
        cands = [r for r in rows
                 if str(r.get("predicted_side") or "").upper() in ("YES", "NO")
                 and _dec(r.get("calibrated_yes_probability")) is not None
                 and _dec(r.get("entry_ask_cents")) is not None]
        if not cands:
            continue  # nothing usable this contract; gate would MISSING_DATA.
        # Pick the canonical interval present, by priority.
        chosen = min(cands, key=lambda r: (priority[str(r["interval"])], int(r.get("id") or 0)))
        iv = str(chosen["interval"])
        side = str(chosen.get("predicted_side") or "").upper()
        cal = _dec(chosen.get("calibrated_yes_probability"))
        ask = _dec(chosen.get("entry_ask_cents"))
        wk = int(chosen["window_key"])
        sel = side_p_win(side, cal)
        candidate = {
            "predicted_side": side,
            "selected_probability": float(sel),
            "entry_ask_cents": float(ask),
            "total_cost_cents": _f(_dec(chosen.get("total_cost_cents"))) or 0.0,
            "distance_sigma": _f(_dec(chosen.get("distance_from_strike"))),
            "spread_cents": _f(_dec(chosen.get("spread_cents"))),
        }
        btc_yes = btc_yes_by_window.get(wk)
        btc_yes_prob = float(btc_yes) if btc_yes is not None else None
        verdict = gate_evaluate(candidate, cfg, interval=iv, btc_yes_prob=btc_yes_prob)
        if not verdict.get("fired"):
            continue
        if btc_yes_prob is None:
            btc_fail_open += 1
        stake = Decimal("1")
        ev = compute_ev(side, cal, ask, stake)
        resolved = chosen.get("official_result") is not None
        recomputed = None
        side_matches = None
        if resolved:
            official_u = str(chosen.get("official_result") or "").upper()
            if official_u in ("YES", "NO"):
                side_matches = (side == official_u)
                recomputed = recompute_pnl(1 if side_matches else 0, ask, stake)
        would_have.append({
            "ticker": ticker,
            "asset": (str(chosen.get("asset")) if chosen.get("asset") is not None else None),
            "window_key": wk,
            "interval": iv,
            "predicted_side": side,
            "official_result": (str(chosen.get("official_result")).upper()
                                if resolved else None),
            "resolved": resolved,
            "side_matches_result": side_matches,
            "ask_cents": _f(ask),
            "ev_cents": _f(ev),
            "modeled_pnl_cents": _f(recomputed),
            "net_edge_cents": _f(_dec(verdict.get("net_edge_cents"))),
            "btc_confirm_failed_open": btc_yes_prob is None,
        })

    would_have.sort(key=lambda w: (w["window_key"], w["ticker"]))
    graded = [w for w in would_have if w["modeled_pnl_cents"] is not None]
    pnl_total = sum((Decimal(str(w["modeled_pnl_cents"])) for w in graded), _ZERO)
    ev_total = sum((Decimal(str(w["ev_cents"])) for w in would_have
                    if w["ev_cents"] is not None), _ZERO)
    right = sum(1 for w in graded if w["side_matches_result"])
    p, lo, hi = _wilson(right, len(graded))
    return {
        "label": "MODELED would-have (chart replay via current gate, GATE-LEVEL "
                 "/ pre top-N selection — an UPPER BOUND on live deliveries) — "
                 "NOT confirmed account P&L",
        "trade_count": len(would_have),
        "resolved_count": len(graded),
        "btc_confirm_failed_open_count": btc_fail_open,
        "win_rate": p,
        "win_rate_ci_low": lo,
        "win_rate_ci_high": hi,
        "modeled_pnl_total_cents": _f(pnl_total),
        "ev_total_cents": _f(ev_total),
        "trades": would_have,
    }


# --------------------------------------------------------------------------- #
# Snapshot diff (before / after)
# --------------------------------------------------------------------------- #
def build_snapshot(trades: Sequence[CanonicalTrade], account: Mapping[str, Any],
                   model_version: str, grade_intervals: Sequence[str],
                   config_fp: Mapping[str, Any] | None = None,
                   is_strict_c5: bool | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "model_version": model_version,
        "grade_intervals": list(grade_intervals),
        "config_fingerprint": dict(config_fp) if config_fp is not None else None,
        "is_strict_c5": is_strict_c5,
        "trades": {t.key(): t.as_dict() for t in trades},
        "account": account,
    }


def _num_delta(new: Any, old: Any) -> float | None:
    nn, oo = _dec(new), _dec(old)
    if nn is None or oo is None:
        return None
    return float(nn - oo)


def diff_against_snapshot(current: Mapping[str, Any],
                          prior: Mapping[str, Any] | None) -> dict[str, Any]:
    """Compute CHANGES SINCE LAST AUDIT: new trades, newly-resolved, regraded
    (correct flipped), P&L-changed (old→new), and aggregate deltas. Returns a
    structured diff; when there is no prior snapshot, everything is 'new'."""
    cur_trades: dict[str, Any] = dict(current.get("trades", {}))
    prior_trades: dict[str, Any] = dict(prior.get("trades", {})) if prior else {}

    new_trades: list[dict[str, Any]] = []
    newly_resolved: list[dict[str, Any]] = []
    regraded: list[dict[str, Any]] = []
    pnl_changed: list[dict[str, Any]] = []

    for key, t in cur_trades.items():
        old = prior_trades.get(key)
        if old is None:
            new_trades.append({"key": key, "ticker": t.get("ticker"),
                               "window_key": t.get("window_key"),
                               "interval": t.get("interval"),
                               "resolved": t.get("resolved"),
                               "pnl_cents": t.get("recomputed_pnl_cents")})
            continue
        if t.get("resolved") and not old.get("resolved"):
            newly_resolved.append({"key": key, "ticker": t.get("ticker"),
                                   "official_result": t.get("official_result"),
                                   "pnl_cents": t.get("recomputed_pnl_cents")})
        # regrade: correctness flipped between snapshots.
        if (old.get("side_matches_result") is not None
                and t.get("side_matches_result") is not None
                and old.get("side_matches_result") != t.get("side_matches_result")):
            regraded.append({"key": key, "ticker": t.get("ticker"),
                             "old_correct": old.get("side_matches_result"),
                             "new_correct": t.get("side_matches_result")})
        # P&L changed (numeric delta beyond tolerance).
        delta = _num_delta(t.get("recomputed_pnl_cents"), old.get("recomputed_pnl_cents"))
        if delta is not None and abs(Decimal(str(delta))) > PNL_TOLERANCE:
            pnl_changed.append({"key": key, "ticker": t.get("ticker"),
                                "old_pnl_cents": old.get("recomputed_pnl_cents"),
                                "new_pnl_cents": t.get("recomputed_pnl_cents"),
                                "delta_cents": delta})
        elif (old.get("recomputed_pnl_cents") is None) != (t.get("recomputed_pnl_cents") is None):
            pnl_changed.append({"key": key, "ticker": t.get("ticker"),
                                "old_pnl_cents": old.get("recomputed_pnl_cents"),
                                "new_pnl_cents": t.get("recomputed_pnl_cents"),
                                "delta_cents": None})

    removed = [k for k in prior_trades if k not in cur_trades]

    # aggregate deltas.
    cur_overall = current.get("account", {}).get("overall", {})
    old_overall = (prior.get("account", {}) if prior else {}).get("overall", {})
    agg_deltas = {
        "pnl_total_cents": _num_delta(cur_overall.get("pnl_total_cents"),
                                      old_overall.get("pnl_total_cents")),
        "ev_total_cents": _num_delta(cur_overall.get("ev_total_cents"),
                                     old_overall.get("ev_total_cents")),
        "staked_cents": _num_delta(cur_overall.get("staked_cents"),
                                   old_overall.get("staked_cents")),
        "roi_old": old_overall.get("roi"),
        "roi_new": cur_overall.get("roi"),
        "win_rate_old": old_overall.get("win_rate"),
        "win_rate_new": cur_overall.get("win_rate"),
        "n_old": old_overall.get("n"),
        "n_new": cur_overall.get("n"),
    }

    return {
        "has_prior": prior is not None,
        "new_trades": new_trades,
        "newly_resolved": newly_resolved,
        "regraded": regraded,
        "pnl_changed": pnl_changed,
        "removed_trades": removed,
        "aggregate_deltas": agg_deltas,
    }


def load_snapshot(path: str) -> dict[str, Any] | None:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_snapshot(path: str, snapshot: Mapping[str, Any]) -> None:
    p = Path(path)
    if p.parent and str(p.parent) not in ("", "."):
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(snapshot, indent=2, sort_keys=True, default=str) + "\n",
                 encoding="utf-8")


# --------------------------------------------------------------------------- #
# Audit orchestration
# --------------------------------------------------------------------------- #
def run_audit(*, db_path: str, captures_path: str, model_version: str,
              grade_intervals: Sequence[str], min_n: int,
              snapshot_path: str, extend: bool,
              captures_model_version: str = DEFAULT_CAPTURES_MODEL_VERSION,
              delivered_only: bool = False, per_window: bool = False,
              flag_asset: str = "",
              cfg: Any = None, gate_evaluate=None,
              strict_c5: bool = False) -> dict[str, Any]:
    """End-to-end read-only audit. Returns the full report dict. Never writes a
    snapshot (the CLI decides that on ``--save-snapshot``).

    The cfg whose gate/runner-relevant fields are stamped as the config
    fingerprint is the SAME cfg used for the chart replay: when ``strict_c5`` is
    set it is built from ``C5_PINS`` (ambient env ignored); otherwise it is the
    caller's ``cfg`` (or ``UltoimV2Config.from_env()``). ``is_strict_c5`` and the
    fingerprint are stamped on BOTH the report and the snapshot so a stray env
    override can never silently change the replayed rules without the report
    saying so."""
    conn = _open_ro(db_path)
    if conn is None or not _table_exists(conn, "ultoim_v2_predictions"):
        if conn is not None:
            conn.close()
        return {
            "available": False,
            "reason": f"V2 ledger not found or empty: {db_path}",
            "model_version": model_version,
            "db_path": db_path,
        }
    try:
        v2_rows = _fetch_v2_rows(conn, model_version, delivered_only=delivered_only)
    finally:
        conn.close()

    if not v2_rows:
        pop = "delivered (SENT)" if delivered_only else "fired"
        return {
            "available": False,
            "reason": f"no {pop} rows for model_version={model_version} in {db_path}",
            "model_version": model_version,
            "db_path": db_path,
        }

    # Chart store is optional — its absence only disables cross-check + extension.
    capture_rows: list[dict[str, Any]] = []
    captures_available = False
    cconn = _open_ro(captures_path)
    if cconn is not None and _table_exists(cconn, "interval_captures"):
        try:
            capture_rows = _fetch_capture_rows(cconn, captures_model_version)
            captures_available = True
        finally:
            cconn.close()
    elif cconn is not None:
        cconn.close()

    # Resolve the cfg whose rules govern the replay AND whose fields are stamped
    # as the fingerprint. --strict-c5 forces the C5 pins regardless of ambient env.
    if strict_c5:
        cfg = cfg_from_pins()
    elif cfg is None:
        from q15_upgrade.ultoim_v2.config import UltoimV2Config
        cfg = UltoimV2Config.from_env()
    fp = resolve_fingerprint(cfg)

    trades = load_canonical_trades(v2_rows, capture_rows, model_version, grade_intervals)
    account = aggregate_account(trades, min_n)
    discrepancies = collect_discrepancies(trades)
    snapshot = build_snapshot(trades, account, model_version, grade_intervals,
                              config_fp=fp["fingerprint"],
                              is_strict_c5=fp["is_strict_c5"])

    prior = load_snapshot(snapshot_path)
    diff = diff_against_snapshot(snapshot, prior)

    report: dict[str, Any] = {
        "available": True,
        "model_version": model_version,
        "db_path": db_path,
        "captures_path": captures_path if captures_available else None,
        "captures_available": captures_available,
        "captures_model_version": captures_model_version,
        "captures_rows_matched": len(capture_rows),
        "population": "delivered" if delivered_only else "fired",
        "grade_intervals": list(grade_intervals),
        "min_scoreboard_n": min_n,
        "trade_count": len(trades),
        "account": account,
        "discrepancies": discrepancies,
        "changes_since_last_audit": diff,
        "config_fingerprint": fp["fingerprint"],
        "is_strict_c5": fp["is_strict_c5"],
        "config_divergences": fp["divergences"],
        "strict_c5_forced": bool(strict_c5),
        "snapshot": snapshot,
        "trades": [t.as_dict() for t in trades],
    }

    # One-buy-per-15-min-window account (the owner takes one buy per window).
    if per_window:
        report["per_window_account"] = aggregate_per_window(trades, min_n)
    # Always-on drag callout for a flagged asset (e.g. HYPE: kept but flagged).
    if flag_asset:
        report["asset_flag"] = asset_flag_summary(trades, flag_asset)

    if extend:
        if not captures_available:
            report["chart_extension"] = {
                "available": False,
                "reason": f"captures DB not found: {captures_path}",
            }
        else:
            if gate_evaluate is None:
                from q15_upgrade.ultoim_v2.gate import evaluate as gate_evaluate
            delivered = _delivered_window_intervals(trades)
            btc_yes_by_window = _btc_yes_by_window(capture_rows, grade_intervals)
            report["chart_extension"] = {
                "available": True,
                **extend_with_chart(capture_rows, delivered, cfg,
                                    grade_intervals, gate_evaluate,
                                    btc_yes_by_window=btc_yes_by_window),
            }
    return report


# --------------------------------------------------------------------------- #
# Text rendering
# --------------------------------------------------------------------------- #
def _fmt_cents(v: Any) -> str:
    if v is None:
        return "—"
    return f"{float(v):+.2f}¢"


def _fmt_pct(v: Any) -> str:
    if v is None:
        return "—"
    return f"{float(v) * 100:.1f}%"


def render_report(report: Mapping[str, Any]) -> str:
    """Human-readable text report. Mirrors the spec's deliverable sections."""
    lines: list[str] = []
    if not report.get("available"):
        lines.append("V2 ACCOUNT AUDIT — unavailable")
        lines.append(f"  {report.get('reason', 'no data')}")
        return "\n".join(lines)

    acct = report["account"]
    overall = acct["overall"]
    lines.append("V2 ACCOUNT AUDIT — ultoim_v2")
    lines.append(f"  model_version: {report['model_version']}")
    lines.append(f"  population: {report.get('population', 'fired')}"
                 f"{' (SENT/alerted only — actionable book)' if report.get('population') == 'delivered' else ' (all gate-passing rows)'}")
    lines.append(f"  grade intervals (priority): {', '.join(report['grade_intervals'])}")

    # C5 config fingerprint — strict status + loud divergence warning.
    is_strict = report.get("is_strict_c5")
    if report.get("strict_c5_forced"):
        lines.append("  config: STRICT C5 (forced via --strict-c5; ambient env ignored)")
    elif is_strict:
        lines.append("  config: STRICT C5 (resolved config matches every C5 pin)")
    elif is_strict is False:
        divs = report.get("config_divergences") or []
        lines.append(f"  config: *** NOT STRICT C5 *** {len(divs)} field(s) diverge "
                     f"from the C5 pins — replayed rules differ from the owner preset:")
        for d in divs:
            lines.append(f"    WARNING: {d['field']} = {d['resolved']!r} "
                         f"(C5 pin {d['pinned']!r})")
    # Captures store (its own model_version namespace). Warn loudly if the DB is
    # present but zero rows matched — the chart cross-check / extension would be
    # silently no-op'd (the namespace-mismatch failure mode).
    if report.get("captures_available"):
        matched = report.get("captures_rows_matched", 0)
        cmv = report.get("captures_model_version")
        if matched:
            lines.append(f"  captures: {matched} rows (model_version={cmv})")
        else:
            lines.append(f"  captures: *** 0 rows matched model_version={cmv!r} *** "
                         f"chart cross-check + extension are INACTIVE — check "
                         f"--captures-model-version (V2 ledger and captures use "
                         f"DIFFERENT namespaces).")
    lines.append(f"  canonical trades: {report['trade_count']} "
                 f"(resolved {acct['resolved_count']})")
    lines.append("")
    lines.append("ACCOUNT (recomputed Decimal P&L — authoritative):")
    insuff = " [INSUFFICIENT]" if overall["insufficient"] else ""
    lines.append(f"  n={overall['n']}{insuff}  win={_fmt_pct(overall['win_rate'])} "
                 f"(CI {_fmt_pct(overall['win_rate_ci_low'])}–{_fmt_pct(overall['win_rate_ci_high'])})")
    lines.append(f"  realized P&L: {_fmt_cents(overall['pnl_total_cents'])}   "
                 f"EV: {_fmt_cents(overall['ev_total_cents'])}   "
                 f"staked: {_fmt_cents(overall['staked_cents'])}")
    roi = overall["roi"]
    lines.append(f"  ROI: {_fmt_pct(roi) if roi is not None else '—'}   "
                 f"edge realization (P&L−EV): {_fmt_cents(overall['edge_realization_cents'])}")
    lines.append("")
    lines.append("BY INTERVAL:")
    for iv, cell in acct["by_interval"].items():
        tag = " [INSUFFICIENT]" if cell["insufficient"] else ""
        lines.append(f"  {iv}: n={cell['n']}{tag}  win={_fmt_pct(cell['win_rate'])}  "
                     f"P&L={_fmt_cents(cell['pnl_total_cents'])}  ROI="
                     f"{_fmt_pct(cell['roi']) if cell['roi'] is not None else '—'}")
    lines.append("")
    lines.append("BY ASSET:")
    for a, cell in acct["by_asset"].items():
        tag = " [INSUFFICIENT]" if cell["insufficient"] else ""
        lines.append(f"  {a}: n={cell['n']}{tag}  win={_fmt_pct(cell['win_rate'])}  "
                     f"P&L={_fmt_cents(cell['pnl_total_cents'])}")
    lines.append("")
    lines.append("BY SIDE:")
    for sd, cell in acct["by_side"].items():
        tag = " [INSUFFICIENT]" if cell["insufficient"] else ""
        lines.append(f"  {sd}: n={cell['n']}{tag}  win={_fmt_pct(cell['win_rate'])}  "
                     f"P&L={_fmt_cents(cell['pnl_total_cents'])}")
    lines.append("")

    # One-buy-per-15-min-window account (manual pick -> show the envelope + rules).
    pw = report.get("per_window_account")
    if pw:
        lines.append("PER-WINDOW ACCOUNT (one buy per 15-min window; you pick manually):")
        lines.append(f"  windows: {pw['windows']}  (vs {report['trade_count']} per-asset trades)")
        order = [("best_case", "best-case pick"), ("highest_confidence", "highest-confidence"),
                 ("most_expensive_ask", "most-expensive ask"), ("cheapest_ask", "cheapest ask (C5 reward:risk)"),
                 ("worst_case", "worst-case pick")]
        for k, label in order:
            cell = pw.get(k) or {}
            tag = " [INSUFFICIENT]" if cell.get("insufficient") else ""
            lines.append(f"  {label:30s} n={cell.get('n')}{tag}  win={_fmt_pct(cell.get('win_rate'))}  "
                         f"P&L={_fmt_cents(cell.get('pnl_total_cents'))}  "
                         f"avg/win={_fmt_cents((cell.get('pnl_total_cents') or 0)/cell['n']) if cell.get('n') else '—'}")
        lines.append("  (best/worst bracket what a manual pick could achieve; the rules are reference points)")
        lines.append("")

    # Always-on flagged-asset drag callout (HYPE kept but flagged).
    af = report.get("asset_flag")
    if af and af.get("n_trades"):
        lines.append(f"FLAGGED ASSET — {af['asset']} (kept in book, drag shown):")
        lines.append(f"  n={af['n_trades']} trades / {af['n_windows']} windows  "
                     f"win={_fmt_pct(af['win_rate'])}  P&L={_fmt_cents(af['pnl_total_cents'])}")
        lines.append(f"  book WITHOUT {af['asset']}: {_fmt_cents(af['book_excluding_asset_cents'])} "
                     f"(vs {_fmt_cents(overall['pnl_total_cents'])} with it)")
        lines.append("")

    disc = report["discrepancies"]
    lines.append(f"DISCREPANCIES: {len(disc)}")
    for d in disc:
        lines.append(f"  {d['ticker']} w={d['window_key']} {d['interval']}: "
                     f"{', '.join(d['flags'])}")
    lines.append("")

    diff = report["changes_since_last_audit"]
    lines.append("CHANGES SINCE LAST AUDIT:")
    if not diff["has_prior"]:
        lines.append("  (no prior snapshot — baseline run)")
    lines.append(f"  new trades: {len(diff['new_trades'])}")
    lines.append(f"  newly resolved: {len(diff['newly_resolved'])}")
    lines.append(f"  regraded (flip): {len(diff['regraded'])}")
    for r in diff["regraded"]:
        lines.append(f"    {r['ticker']}: correct {r['old_correct']} → {r['new_correct']}")
    lines.append(f"  P&L changed: {len(diff['pnl_changed'])}")
    for c in diff["pnl_changed"]:
        lines.append(f"    {c['ticker']}: {_fmt_cents(c['old_pnl_cents'])} → "
                     f"{_fmt_cents(c['new_pnl_cents'])}")
    lines.append(f"  removed trades: {len(diff['removed_trades'])}")
    for k in diff["removed_trades"]:
        lines.append(f"    {k}")
    ad = diff["aggregate_deltas"]
    lines.append(f"  Δ P&L: {_fmt_cents(ad['pnl_total_cents'])}   "
                 f"Δ EV: {_fmt_cents(ad['ev_total_cents'])}   "
                 f"ROI {ad['roi_old']} → {ad['roi_new']}")

    if "chart_extension" in report:
        ce = report["chart_extension"]
        lines.append("")
        if not ce.get("available"):
            lines.append(f"CHART EXTENSION: unavailable ({ce.get('reason')})")
        else:
            lines.append("CHART EXTENSION (MODELED would-have, GATE-LEVEL / pre top-N "
                         "— UPPER BOUND on live deliveries; NOT confirmed account P&L):")
            lines.append(f"  would-have trades: {ce['trade_count']} "
                         f"(resolved {ce['resolved_count']})")
            lines.append(f"  modeled P&L: {_fmt_cents(ce['modeled_pnl_total_cents'])}   "
                         f"EV: {_fmt_cents(ce['ev_total_cents'])}   "
                         f"win: {_fmt_pct(ce['win_rate'])}")
            fo = ce.get("btc_confirm_failed_open_count")
            if fo:
                lines.append(f"  NOTE: {fo} would-have trade(s) carried NO BTC context "
                             f"(BTC-confirmation gate forced fail-open) — over-fire vs live")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _parse_intervals(raw: str) -> tuple[str, ...]:
    out = tuple(s.strip().upper() for s in raw.split(",") if s.strip())
    return out or DEFAULT_GRADE_INTERVALS


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="v2_audit",
        description="Read-only V2 (ultoim_v2) account P&L + EV auditor.",
    )
    p.add_argument("--db", default=os.environ.get("Q15_ULTOIM_V2_DB", DEFAULT_V2_DB),
                   help="V2 predictions SQLite DB (read-only).")
    p.add_argument("--captures",
                   default=os.environ.get("Q15_INTERVAL_RESEARCH_DB", DEFAULT_CAPTURES_DB),
                   help="interval_captures SQLite DB (read-only, optional).")
    p.add_argument("--snapshot", default=DEFAULT_SNAPSHOT,
                   help="Before/after snapshot JSON path.")
    p.add_argument("--model-version",
                   default=os.environ.get("Q15_ULTOIM_V2_MODEL_VERSION", DEFAULT_MODEL_VERSION),
                   help="V2 model_version to audit.")
    p.add_argument("--captures-model-version",
                   default=os.environ.get("Q15_INTERVAL_RESEARCH_MODEL_VERSION",
                                          DEFAULT_CAPTURES_MODEL_VERSION),
                   help="interval_captures model_version (its OWN namespace; "
                        "default interval-research-v1, NOT the V2 model_version).")
    p.add_argument("--grade-interval", default=",".join(DEFAULT_GRADE_INTERVALS),
                   help="Canonical checkpoint priority, comma-separated (default 10M,7M).")
    p.add_argument("--min-n", type=int, default=None,
                   help="INSUFFICIENT threshold (default = V2 min_scoreboard_n / 30).")
    p.add_argument("--delivered-only", action="store_true",
                   help="Audit only the SENT/alerted book (your actionable account) "
                        "instead of all gate-passing fired rows.")
    p.add_argument("--per-window", action="store_true",
                   help="Add the one-buy-per-15-min-window account (envelope + "
                        "selection rules) — the unit you actually trade.")
    p.add_argument("--flag-asset", default="HYPE",
                   help="Always surface this asset's drag separately (default HYPE; "
                        "empty to disable).")
    p.add_argument("--extend-with-chart", action="store_true",
                   help="Add a SEPARATE modeled would-have book via the real gate.")
    p.add_argument("--strict-c5", action="store_true",
                   help="Force the C5 gate/runner pins regardless of ambient "
                        "Q15_ULTOIM_V2_* env (provably-strict-C5 replay).")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    p.add_argument("--save-snapshot", action="store_true",
                   help="Persist the new snapshot (the ONLY thing this tool writes).")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    grade_intervals = _parse_intervals(args.grade_interval)

    min_n = args.min_n
    cfg = None
    if min_n is None or args.extend_with_chart:
        try:
            from q15_upgrade.ultoim_v2.config import UltoimV2Config
            cfg = UltoimV2Config.from_env()
            if min_n is None:
                min_n = cfg.min_scoreboard_n
        except ImportError:
            cfg = None
    if min_n is None:
        min_n = DEFAULT_MIN_SCOREBOARD_N

    report = run_audit(
        db_path=args.db,
        captures_path=args.captures,
        model_version=args.model_version,
        grade_intervals=grade_intervals,
        min_n=min_n,
        snapshot_path=args.snapshot,
        extend=args.extend_with_chart,
        captures_model_version=args.captures_model_version,
        delivered_only=args.delivered_only,
        per_window=args.per_window,
        flag_asset=args.flag_asset,
        cfg=None if args.strict_c5 else cfg,
        strict_c5=args.strict_c5,
    )

    if args.save_snapshot and report.get("available"):
        save_snapshot(args.snapshot, report["snapshot"])

    if args.json:
        printable = {k: v for k, v in report.items() if k != "snapshot"}
        print(json.dumps(printable, indent=2, sort_keys=True, default=str))
    else:
        print(render_report(report))
    return 0 if report.get("available") else 1


if __name__ == "__main__":
    sys.exit(main())
