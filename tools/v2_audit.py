#!/usr/bin/env python3
"""Foolproof, auto-updating audit for the ``ultoim_v2`` paper-trading system.

READ-ONLY. This tool only READS the v2 ledger SQLite file. It never writes a row,
never touches the ledger, never places / modifies / cancels an order. It is a pure
analysis CLI: point it at the ledger and it prints (or emits JSON) a full audit of
every config gate the system runs.

================================================================================
EXTENSIBILITY CONTRACT — the two things a future maintainer needs to know
================================================================================

1. ADD A CONFIG FIELD  ->  zero edits here.
   The "ACTIVE CONFIG" section reflects whatever ``UltoimV2Config`` currently has.
   It instantiates the dataclass and iterates ``dataclasses.fields(...)``, so a new
   field appears automatically with its value and best-effort env var name. You do
   not touch this file when ``config.py`` grows a field.

2. ADD A GATE  ->  exactly ONE line.
   Append one ``Gate(name, side, predicate)`` to the module-level ``GATES`` list
   (see the big comment directly above it). Every report section that walks gates
   — the per-gate marginal table, the JSON — picks it up automatically. No other
   edit is required to register a gate's marginal effect.

================================================================================
VERIFIED DATA SEMANTICS (established once; not re-derived here)
================================================================================
- Table: ``ultoim_v2_predictions``. Default DB ``data/q15_ultoim_v2_v1.sqlite3``;
  fall back to ``/tmp/ledgers/q15_ultoim_v2_v1.sqlite3``; override with ``--db``.
- SETTLED iff ``correct IS NOT NULL`` (1=win, 0=loss). DELIVERED iff ``fired=1``.
  Research iff ``research_fired=1``.
- ``entry_ask_cents`` is the CHOSEN-SIDE cost in cents (NO cost for a NO row, YES
  cost for a YES row) — the price paid. So gross payoff = (100 - ask) on a win,
  -ask on a loss.
- ``hypothetical_pnl_cents`` is GROSS of fees and is NOT trusted for the headline.
  We recompute NET ourselves: NET = gross - fee, where the Kalshi per-contract fee
  is ``ceil(0.07 * P * (1 - P) * 100)`` cents with ``P = entry_ask_cents / 100``.
- BTC-confirm context: a lookup of BTC's ``calibrated_yes_probability`` keyed by
  ``(round(close_time), window_key)`` with fallback ``(round(close_time), interval)``.
  A NO is BTC-confirmed iff ``btc_cy <= 0.5 - 0.15``; a YES iff ``btc_cy >= 0.5 +
  0.10``; FAIL-OPEN (confirmed) when no BTC context exists; a BTC row confirms itself.
"""
from __future__ import annotations

import argparse
import ast
import dataclasses
import functools
import json
import math
import os
import random
import sqlite3
import statistics
import sys
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# --------------------------------------------------------------------------- #
# constants — the BTC-confirm thresholds and the cumulative-stack conjunctions
# --------------------------------------------------------------------------- #
BTC_NO_MARGIN = 0.15            # NO confirmed iff btc_cy <= 0.5 - 0.15 = 0.35
BTC_YES_MARGIN = 0.10           # YES confirmed iff btc_cy >= 0.5 + 0.10 = 0.60
ASK_BAND_LO = 50.0
ASK_BAND_HI = 85.0
EXPENSIVE_NO_LO = 72.0          # expensive-NO admit band is (72, 78]
EXPENSIVE_NO_HI = 78.0
HICONV_YES_CAL_MIN = 0.70
HICONV_YES_MARKET_MIN = 0.60

DEFAULT_SEED = 1234
DEFAULT_RESAMPLES = 3000

DEFAULT_DB_PRIMARY = os.path.join(ROOT, "data", "q15_ultoim_v2_v1.sqlite3")
DEFAULT_DB_FALLBACK = "/tmp/ledgers/q15_ultoim_v2_v1.sqlite3"

# The cumulative ("C5") conjunctions applied per side, in order. These are GATE
# NAMES drawn from the GATES registry below, so the stack stays in sync with the
# gate definitions (one source of truth for each predicate).
NO_STACK = ("inverse_edge_no", "btc_confirm", "ask_band", "skip_7m_no")
YES_STACK = ("hiconv_yes", "btc_confirm", "ask_band")


# --------------------------------------------------------------------------- #
# P&L math — net per contract (fee-aware). This is the only P&L we headline.
# --------------------------------------------------------------------------- #
def kalshi_fee_cents(entry_ask_cents: float) -> int:
    """Kalshi per-contract fee in cents: ``ceil(0.07 * P * (1 - P) * 100)`` where
    ``P = entry_ask_cents / 100``. P is clamped to [0, 1] defensively."""
    p = float(entry_ask_cents) / 100.0
    p = min(1.0, max(0.0, p))
    return int(math.ceil(0.07 * p * (1.0 - p) * 100.0))


def net_pnl_cents(entry_ask_cents: float, correct: int) -> float:
    """NET P&L per contract in cents = gross - fee. Gross = (100 - ask) on a win,
    -ask on a loss; the fee is paid on every settled contract."""
    ask = float(entry_ask_cents)
    gross = (100.0 - ask) if int(correct) == 1 else (-ask)
    return gross - float(kalshi_fee_cents(ask))


# --------------------------------------------------------------------------- #
# row access — tolerate sqlite3.Row and plain dict alike (tests use dicts)
# --------------------------------------------------------------------------- #
def _get(row: Any, key: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _num(value: Any) -> Optional[float]:
    """Coerce to a finite float or None. Rejects bool so ``True`` never slips
    through a numeric comparison as ``1.0``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _side(row: Any) -> str:
    return str(_get(row, "predicted_side") or "").upper()


def _interval(row: Any) -> str:
    return str(_get(row, "interval") or "").upper()


def _asset(row: Any) -> str:
    return str(_get(row, "asset") or "").upper()


# --------------------------------------------------------------------------- #
# BTC-confirm context
# --------------------------------------------------------------------------- #
class BtcContext:
    """Lookup of BTC's calibrated P(YES) at each checkpoint, for the cross-asset
    BTC-confirmation gate. Keyed primarily by ``(round(close_time), window_key)``
    with a fallback on ``(round(close_time), interval)``. FAIL-OPEN: a missing key
    returns None and the gate treats None as confirmed."""

    def __init__(self, rows: Iterable[Any]) -> None:
        self._by_window: dict[tuple, float] = {}
        self._by_interval: dict[tuple, float] = {}
        for r in rows:
            if _asset(r) != "BTC":
                continue
            cy = _num(_get(r, "calibrated_yes_probability"))
            ct = _num(_get(r, "close_time"))
            if cy is None or ct is None:
                continue
            ctk = round(ct)
            wk = _get(r, "window_key")
            if wk is not None:
                self._by_window[(ctk, int(wk))] = cy
            self._by_interval[(ctk, _interval(r))] = cy

    def lookup(self, row: Any) -> Optional[float]:
        ct = _num(_get(row, "close_time"))
        if ct is None:
            return None
        ctk = round(ct)
        wk = _get(row, "window_key")
        if wk is not None:
            hit = self._by_window.get((ctk, int(wk)))
            if hit is not None:
                return hit
        return self._by_interval.get((ctk, _interval(row)))


def btc_confirmed(row: Any, btc_cy: Optional[float]) -> bool:
    """True when BTC decisively agrees with the row's side. A BTC row confirms
    itself (compare its own calibrated-yes against the cutoff). FAIL-OPEN: no BTC
    context (None) -> confirmed."""
    if _asset(row) == "BTC":
        btc_cy = _num(_get(row, "calibrated_yes_probability"))
    if btc_cy is None:
        return True
    side = _side(row)
    if side == "NO":
        return btc_cy <= 0.5 - BTC_NO_MARGIN
    if side == "YES":
        return btc_cy >= 0.5 + BTC_YES_MARGIN
    return True


# --------------------------------------------------------------------------- #
# GATES registry — THE EXTENSIBILITY CORE
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Gate:
    """One admit/reject rule. ``predicate(row, ctx) -> bool`` returns True when the
    gate ADMITS (keeps) the row. ``side`` scopes which rows the gate is meaningful
    for: 'NO', 'YES', or 'BOTH'. ``ctx`` carries shared lookups (the BTC context)."""
    name: str
    side: str                                   # 'NO' | 'YES' | 'BOTH'
    predicate: Callable[[Any, "AuditContext"], bool]


# To add a gate, append one Gate(...) here — the report picks it up automatically.
# A gate's predicate returns True to ADMIT (keep) the row; ``ctx`` exposes the
# shared BTC context via ``ctx.btc.lookup(row)``.
GATES: list[Gate] = [
    # NO: keep only the INVERSE-edge tail (the model's NO edge is inverse — the
    # negative-edge, market-confident entries are the winners).
    Gate("inverse_edge_no", "NO",
         lambda row, ctx: (_num(_get(row, "net_edge_cents")) is not None
                           and _num(_get(row, "net_edge_cents")) < 0)),
    # BOTH: BTC decisively agrees with the side (fail-open when no BTC context).
    Gate("btc_confirm", "BOTH",
         lambda row, ctx: btc_confirmed(row, ctx.btc.lookup(row))),
    # NO: informational expensive-NO admit band (72, 78].
    Gate("expensive_no_admit", "NO",
         lambda row, ctx: (_num(_get(row, "entry_ask_cents")) is not None
                           and EXPENSIVE_NO_LO < _num(_get(row, "entry_ask_cents"))
                           <= EXPENSIVE_NO_HI)),
    # YES: high-conviction YES — calibrated-yes high AND market agrees.
    Gate("hiconv_yes", "YES",
         lambda row, ctx: (_num(_get(row, "calibrated_yes_probability")) is not None
                           and _num(_get(row, "market_implied_yes_probability")) is not None
                           and _num(_get(row, "calibrated_yes_probability")) >= HICONV_YES_CAL_MIN
                           and _num(_get(row, "market_implied_yes_probability"))
                           >= HICONV_YES_MARKET_MIN)),
    # BOTH: ask within the sensible band [50, 85] cents.
    Gate("ask_band", "BOTH",
         lambda row, ctx: (_num(_get(row, "entry_ask_cents")) is not None
                           and ASK_BAND_LO <= _num(_get(row, "entry_ask_cents"))
                           <= ASK_BAND_HI)),
    # NO: drop the 7M checkpoint for NO (owner is dropping 7M NO).
    Gate("skip_7m_no", "NO", lambda row, ctx: _interval(row) != "7M"),
]

GATES_BY_NAME: dict[str, Gate] = {g.name: g for g in GATES}


@dataclass
class AuditContext:
    """Shared context handed to every gate predicate."""
    btc: BtcContext


# --------------------------------------------------------------------------- #
# statistics — bootstrap CI + a percentile helper
# --------------------------------------------------------------------------- #
def percentile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolation percentile of an ALREADY-SORTED sequence. ``q`` in
    [0, 100]. Matches numpy's default 'linear' method on the same input."""
    if not sorted_values:
        raise ValueError("percentile of empty sequence")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (q / 100.0) * (len(sorted_values) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return float(sorted_values[int(rank)])
    frac = rank - lo
    return float(sorted_values[lo]) * (1.0 - frac) + float(sorted_values[hi]) * frac


def bootstrap_mean_ci(values: Sequence[float], rng: random.Random,
                      resamples: int = DEFAULT_RESAMPLES,
                      alpha: float = 0.05) -> dict[str, Any]:
    """Bootstrap percentile CI for the MEAN of ``values``. Deterministic given
    ``rng`` (a seeded ``random.Random``). Returns mean, ci_low, ci_high, n.
    Empty -> all None; n==1 -> degenerate CI at the single value."""
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": None, "ci_low": None, "ci_high": None}
    mean = statistics.fmean(values)
    if n == 1:
        return {"n": 1, "mean": round(mean, 4),
                "ci_low": round(mean, 4), "ci_high": round(mean, 4)}
    means: list[float] = []
    vals = list(values)
    for _ in range(resamples):
        sample = [vals[rng.randrange(n)] for _ in range(n)]
        means.append(statistics.fmean(sample))
    means.sort()
    lo = percentile(means, 100.0 * (alpha / 2.0))
    hi = percentile(means, 100.0 * (1.0 - alpha / 2.0))
    return {"n": n, "mean": round(mean, 4),
            "ci_low": round(lo, 4), "ci_high": round(hi, 4)}


def win_rate(rows: Sequence[Any]) -> Optional[float]:
    settled = [r for r in rows if _num(_get(r, "correct")) is not None]
    if not settled:
        return None
    return sum(1 for r in settled if int(_get(r, "correct")) == 1) / len(settled)


def net_pnls(rows: Sequence[Any]) -> list[float]:
    """NET pnl/contract for each SETTLED row that has an ask. Recomputed; never
    trusts ``hypothetical_pnl_cents``."""
    out: list[float] = []
    for r in rows:
        correct = _get(r, "correct")
        ask = _num(_get(r, "entry_ask_cents"))
        if correct is None or ask is None:
            continue
        out.append(net_pnl_cents(ask, int(correct)))
    return out


def summarize(rows: Sequence[Any], rng: random.Random,
              resamples: int = DEFAULT_RESAMPLES) -> dict[str, Any]:
    """n / win rate / mean NET pnl/contract + bootstrap CI over a row population."""
    pnls = net_pnls(rows)
    boot = bootstrap_mean_ci(pnls, rng, resamples=resamples)
    wr = win_rate(rows)
    return {
        "n": len(pnls),
        "win_rate": round(wr, 4) if wr is not None else None,
        "net_pnl_mean": boot["mean"],
        "net_pnl_ci_low": boot["ci_low"],
        "net_pnl_ci_high": boot["ci_high"],
    }


# --------------------------------------------------------------------------- #
# gate application
# --------------------------------------------------------------------------- #
def side_population(rows: Sequence[Any], side: str) -> list[Any]:
    """Rows relevant to a gate's side: BOTH -> all; NO/YES -> that side only."""
    if side == "BOTH":
        return list(rows)
    return [r for r in rows if _side(r) == side]


def apply_gate(rows: Sequence[Any], gate: Gate, ctx: AuditContext) -> list[Any]:
    return [r for r in rows if gate.predicate(r, ctx)]


def apply_stack(rows: Sequence[Any], gate_names: Sequence[str],
                ctx: AuditContext) -> list[Any]:
    """Apply a conjunction of gates (by name), in order. Each gate keeps only the
    rows it admits; the result is the intersection."""
    kept = list(rows)
    for name in gate_names:
        gate = GATES_BY_NAME[name]
        kept = apply_gate(kept, gate, ctx)
    return kept


def complement(pop: Sequence[Any], kept: Sequence[Any]) -> list[Any]:
    """The rejected rows: pop minus kept, by OBJECT IDENTITY (not value equality),
    so two rows with identical content are never conflated."""
    kept_ids = {id(r) for r in kept}
    return [r for r in pop if id(r) not in kept_ids]


# --------------------------------------------------------------------------- #
# config introspection (auto-reflected)
# --------------------------------------------------------------------------- #
_ENV_HELPERS = {"_bool", "_float", "_str", "_int"}


@functools.lru_cache(maxsize=1)
def _env_names_from_source() -> dict[str, str]:
    """Map each UltoimV2Config field -> the TRUE env var string, parsed from config.py's AST.

    Reads the literal env name out of each field's ``default_factory`` (``_bool("Q15_...")`` /
    ``os.environ.get("Q15_...")``), so it auto-tracks new fields AND the ones whose env var is
    abbreviated or renamed (skip_7m_no_deliver -> Q15_ULTOIM_V2_SKIP_7M_NO; yes_notify_enabled ->
    Q15_ULTOIM_V2_YES_NOTIFY; yes_live_enabled -> Q15_EXEC_YES_ENABLED). Foolproof: a wrong derived
    name would otherwise silently point you at an env var the code never reads."""
    out: dict[str, str] = {}
    try:
        import q15_upgrade.ultoim_v2.config as cfgmod
        with open(cfgmod.__file__, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError, ValueError):
        return out
    cls = next((n for n in ast.walk(tree)
                if isinstance(n, ast.ClassDef) and n.name == "UltoimV2Config"), None)
    if cls is None:
        return out
    for node in cls.body:
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call) or not sub.args:
                continue
            func = sub.func
            is_helper = isinstance(func, ast.Name) and func.id in _ENV_HELPERS
            is_environ = isinstance(func, ast.Attribute) and func.attr == "get"
            a0 = sub.args[0]
            if (is_helper or is_environ) and isinstance(a0, ast.Constant) \
                    and isinstance(a0.value, str) and a0.value.startswith("Q15_"):
                out[node.target.id] = a0.value
                break
    return out


def _env_name(field_name: str) -> str:
    """True env var read from source; falls back to the conventional name for a literal-less field."""
    return _env_names_from_source().get(field_name, "Q15_ULTOIM_V2_" + field_name.upper())


def reflect_config() -> dict[str, Any]:
    """Instantiate ``UltoimV2Config`` and reflect every field: name, current value,
    best-effort env var. Zero edits needed when a field is added."""
    from q15_upgrade.ultoim_v2.config import UltoimV2Config

    cfg = UltoimV2Config()
    fields_out: list[dict[str, Any]] = []
    for f in dataclasses.fields(UltoimV2Config):
        value = getattr(cfg, f.name)
        if isinstance(value, frozenset):
            value_repr: Any = sorted(str(x) for x in value)
        else:
            value_repr = value
        fields_out.append({
            "name": f.name,
            "value": value_repr,
            "env": _env_name(f.name),
        })
    return {"class": "UltoimV2Config", "fields": fields_out}


# --------------------------------------------------------------------------- #
# populations
# --------------------------------------------------------------------------- #
def populations(rows: Sequence[Any]) -> dict[str, Any]:
    settled = [r for r in rows if _get(r, "correct") is not None]
    delivered = [r for r in rows if _num(_get(r, "fired")) == 1]
    research = [r for r in rows if _num(_get(r, "research_fired")) == 1]

    def by_side_counts(pop: Sequence[Any]) -> dict[str, int]:
        out = {"NO": 0, "YES": 0, "OTHER": 0}
        for r in pop:
            sd = _side(r)
            out[sd if sd in ("NO", "YES") else "OTHER"] += 1
        return out

    def by_interval_counts(pop: Sequence[Any]) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in pop:
            out[_interval(r)] = out.get(_interval(r), 0) + 1
        return dict(sorted(out.items()))

    return {
        "total": len(rows),
        "settled": len(settled),
        "delivered": len(delivered),
        "research": len(research),
        "by_side": by_side_counts(rows),
        "settled_by_side": by_side_counts(settled),
        "settled_by_interval": by_interval_counts(settled),
    }


# --------------------------------------------------------------------------- #
# per-gate marginal effect
# --------------------------------------------------------------------------- #
def per_gate_marginals(settled: Sequence[Any], ctx: AuditContext,
                       rng_factory: Callable[[], random.Random],
                       resamples: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for gate in GATES:
        pop = side_population(settled, gate.side)
        kept = apply_gate(pop, gate, ctx)
        rejected = complement(pop, kept)
        out.append({
            "name": gate.name,
            "side": gate.side,
            "population_n": len(pop),
            "kept": summarize(kept, rng_factory(), resamples),
            "rejected": summarize(rejected, rng_factory(), resamples),
        })
    return out


# --------------------------------------------------------------------------- #
# cumulative stack
# --------------------------------------------------------------------------- #
def cumulative_stack(settled: Sequence[Any], side: str, gate_names: Sequence[str],
                     ctx: AuditContext, rng_factory: Callable[[], random.Random],
                     resamples: int) -> dict[str, Any]:
    pop = side_population(settled, side)
    kept = apply_stack(pop, gate_names, ctx)
    overall = summarize(kept, rng_factory(), resamples)

    by_interval: dict[str, Any] = {}
    for iv in sorted({_interval(r) for r in kept}):
        by_interval[iv] = summarize([r for r in kept if _interval(r) == iv],
                                    rng_factory(), resamples)
    by_asset: dict[str, Any] = {}
    for asset in sorted({_asset(r) for r in kept}):
        by_asset[asset] = summarize([r for r in kept if _asset(r) == asset],
                                    rng_factory(), resamples)
    return {
        "side": side,
        "gates": list(gate_names),
        "overall": overall,
        "by_interval": by_interval,
        "by_asset": by_asset,
        "_kept_rows": kept,            # internal; stripped before JSON
    }


# --------------------------------------------------------------------------- #
# OOS robustness
# --------------------------------------------------------------------------- #
def oos_robustness(kept: Sequence[Any], rng_factory: Callable[[], random.Random],
                   resamples: int) -> dict[str, Any]:
    """Time-split (early 60% train / late 40% test, by created_at) + leave-one-
    asset-out, on a stack's KEPT population."""
    ordered = sorted(kept, key=lambda r: (_num(_get(r, "created_at")) or 0.0,
                                          _num(_get(r, "id")) or 0.0))
    n = len(ordered)
    cut = int(n * 0.6)
    train = ordered[:cut]
    test = ordered[cut:]
    time_split = {
        "train_early_60": summarize(train, rng_factory(), resamples),
        "test_late_40": summarize(test, rng_factory(), resamples),
    }

    assets = sorted({_asset(r) for r in ordered})
    per_asset: dict[str, Any] = {}
    drop_one: dict[str, Any] = {}
    for asset in assets:
        only = [r for r in ordered if _asset(r) == asset]
        rest = [r for r in ordered if _asset(r) != asset]
        per_asset[asset] = summarize(only, rng_factory(), resamples)
        drop_one[asset] = summarize(rest, rng_factory(), resamples)

    return {
        "time_split": time_split,
        "leave_one_asset_out": {"per_asset": per_asset, "drop_one_remaining": drop_one},
    }


# --------------------------------------------------------------------------- #
# headline verdict
# --------------------------------------------------------------------------- #
def headline_verdict(stack: Mapping[str, Any]) -> dict[str, Any]:
    """PASS = CI lower bound > 0 AND no single asset strongly negative.
    MARGINAL = positive mean but CI straddles 0. FAIL = mean <= 0 (or empty)."""
    overall = stack["overall"]
    mean = overall["net_pnl_mean"]
    ci_low = overall["net_pnl_ci_low"]
    by_asset = stack["by_asset"]
    # "strongly negative" single asset: a populated asset whose mean net pnl is
    # below -10 cents/contract (a material per-contract drag), with n>=5 so a
    # one-row asset cannot veto the verdict.
    strong_neg_asset = None
    for asset, agg in by_asset.items():
        if agg["n"] >= 5 and agg["net_pnl_mean"] is not None and agg["net_pnl_mean"] < -10.0:
            strong_neg_asset = asset
            break

    if mean is None or mean <= 0.0:
        tag = "FAIL"
    elif ci_low is not None and ci_low > 0.0 and strong_neg_asset is None:
        tag = "PASS"
    else:
        tag = "MARGINAL"
    return {
        "side": stack["side"],
        "n": overall["n"],
        "net_pnl_mean": mean,
        "net_pnl_ci_low": ci_low,
        "net_pnl_ci_high": overall["net_pnl_ci_high"],
        "strong_negative_asset": strong_neg_asset,
        "tag": tag,
    }


# --------------------------------------------------------------------------- #
# ledger loading
# --------------------------------------------------------------------------- #
def resolve_db_path(explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    if os.path.exists(DEFAULT_DB_PRIMARY):
        return DEFAULT_DB_PRIMARY
    return DEFAULT_DB_FALLBACK


def load_rows(db_path: str) -> list[dict[str, Any]]:
    """Read every ``ultoim_v2_predictions`` row as a plain dict. READ-ONLY: opens
    the DB read-only via a URI and never issues a write."""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"ledger not found: {db_path}")
    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM ultoim_v2_predictions").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# audit orchestration
# --------------------------------------------------------------------------- #
def run_audit(rows: Sequence[Any], *, side: str = "BOTH", seed: int = DEFAULT_SEED,
              resamples: int = DEFAULT_RESAMPLES) -> dict[str, Any]:
    """Build the full structured audit result. Deterministic given ``seed``: each
    bootstrap call gets a freshly-seeded ``random.Random`` derived from ``seed``
    plus a per-call counter, so identical inputs always yield identical CIs."""
    ctx = AuditContext(btc=BtcContext(rows))
    settled = [r for r in rows if _get(r, "correct") is not None]

    counter = {"n": 0}

    def rng_factory() -> random.Random:
        counter["n"] += 1
        # Derive a distinct, deterministic integer seed per bootstrap call so the
        # whole report is reproducible: a fixed top-level seed plus a per-call
        # counter mixed into one stable string seed.
        return random.Random(f"{seed}:{counter['n']}")

    result: dict[str, Any] = {
        "config": reflect_config(),
        "populations": populations(rows),
        "seed": seed,
        "resamples": resamples,
        "settled_total": len(settled),
    }

    if side in ("BOTH",):
        result["per_gate_marginals"] = per_gate_marginals(
            settled, ctx, rng_factory, resamples)
    else:
        # Side-scoped: only show gates meaningful for that side (plus BOTH gates).
        gates_for_side = [g for g in GATES if g.side in (side, "BOTH")]
        marg: list[dict[str, Any]] = []
        for gate in gates_for_side:
            pop = side_population(settled, gate.side)
            kept = apply_gate(pop, gate, ctx)
            rejected = complement(pop, kept)
            marg.append({
                "name": gate.name, "side": gate.side,
                "population_n": len(pop),
                "kept": summarize(kept, rng_factory(), resamples),
                "rejected": summarize(rejected, rng_factory(), resamples),
            })
        result["per_gate_marginals"] = marg

    stacks: dict[str, Any] = {}
    verdicts: dict[str, Any] = {}
    oos: dict[str, Any] = {}
    sides_to_run = ("NO", "YES") if side == "BOTH" else (side,)
    for sd in sides_to_run:
        names = NO_STACK if sd == "NO" else YES_STACK
        stack = cumulative_stack(settled, sd, names, ctx, rng_factory, resamples)
        verdicts[sd] = headline_verdict(stack)
        oos[sd] = oos_robustness(stack["_kept_rows"], rng_factory, resamples)
        stack.pop("_kept_rows", None)        # strip internal handle before output
        stacks[sd] = stack

    result["cumulative_stacks"] = stacks
    result["oos_robustness"] = oos
    result["headline_verdicts"] = verdicts
    return result


# --------------------------------------------------------------------------- #
# text rendering
# --------------------------------------------------------------------------- #
def _fmt(value: Any, width: int = 8, prec: int = 2) -> str:
    if value is None:
        return "n/a".rjust(width)
    return f"{value:>{width}.{prec}f}"


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "  n/a"
    return f"{value * 100:>4.0f}%"


def _summary_line(agg: Mapping[str, Any]) -> str:
    return (f"n={agg['n']:>4}  win={_fmt_pct(agg['win_rate'])}  "
            f"net={_fmt(agg['net_pnl_mean'])}c/ct  "
            f"CI[{_fmt(agg['net_pnl_ci_low'])},{_fmt(agg['net_pnl_ci_high'])}]")


def render_text(result: Mapping[str, Any]) -> str:
    L: list[str] = []
    add = L.append
    bar = "=" * 78

    add(bar)
    add("ULTOIM V2 — CONFIG-GATE AUDIT (read-only)")
    add(f"seed={result['seed']}  resamples={result['resamples']}  "
        f"settled={result['settled_total']}")
    add(bar)

    # 1. ACTIVE CONFIG
    add("")
    add("ACTIVE CONFIG (auto-reflected)")
    add("-" * 78)
    for f in result["config"]["fields"]:
        add(f"  {f['name']:<28} = {str(f['value']):<22}  [{f['env']}]")

    # 2. POPULATIONS
    pops = result["populations"]
    add("")
    add("POPULATIONS")
    add("-" * 78)
    add(f"  total={pops['total']}  settled={pops['settled']}  "
        f"delivered(fired)={pops['delivered']}  research={pops['research']}")
    add(f"  settled by side:     {pops['settled_by_side']}")
    add(f"  settled by interval: {pops['settled_by_interval']}")

    # 3. PER-GATE MARGINAL EFFECT
    add("")
    add("PER-GATE MARGINAL EFFECT (settled; KEPT = gate admits, REJECTED = complement)")
    add("-" * 78)
    for g in result["per_gate_marginals"]:
        add(f"  {g['name']} [{g['side']}]  (population n={g['population_n']})")
        add(f"      KEPT     {_summary_line(g['kept'])}")
        add(f"      REJECTED {_summary_line(g['rejected'])}")

    # 4. CUMULATIVE STACK
    add("")
    add("CUMULATIVE STACK (C5)")
    add("-" * 78)
    for sd, stack in result["cumulative_stacks"].items():
        add(f"  {sd} stack: {' & '.join(stack['gates'])}")
        add(f"      OVERALL  {_summary_line(stack['overall'])}")
        if stack["by_interval"]:
            add("      by interval:")
            for iv, agg in stack["by_interval"].items():
                add(f"        {iv:<4} {_summary_line(agg)}")
        if stack["by_asset"]:
            add("      by asset:")
            for asset, agg in stack["by_asset"].items():
                add(f"        {asset:<5}{_summary_line(agg)}")

    # 5. OOS ROBUSTNESS
    add("")
    add("OOS ROBUSTNESS")
    add("-" * 78)
    for sd, oos in result["oos_robustness"].items():
        add(f"  {sd} stack:")
        ts = oos["time_split"]
        train_clears = (ts['train_early_60']['net_pnl_ci_low'] is not None
                        and ts['train_early_60']['net_pnl_ci_low'] > 0)
        test_clears = (ts['test_late_40']['net_pnl_ci_low'] is not None
                       and ts['test_late_40']['net_pnl_ci_low'] > 0)
        add(f"      time-split train(early 60%) {_summary_line(ts['train_early_60'])}"
            f"  {'CI>0' if train_clears else 'CI<=0'}")
        add(f"      time-split test (late 40%) {_summary_line(ts['test_late_40'])}"
            f"  {'CI>0' if test_clears else 'CI<=0'}")
        loo = oos["leave_one_asset_out"]
        add("      leave-one-asset-out (asset alone | drop-one remaining):")
        for asset in loo["per_asset"]:
            alone = loo["per_asset"][asset]
            rest = loo["drop_one_remaining"][asset]
            rest_clears = (rest['net_pnl_ci_low'] is not None
                           and rest['net_pnl_ci_low'] > 0)
            add(f"        -{asset:<5} alone n={alone['n']:>3} "
                f"net={_fmt(alone['net_pnl_mean'])}c | drop {_summary_line(rest)}"
                f"  {'CI>0' if rest_clears else 'CI<=0'}")

    # 6. HEADLINE
    add("")
    add("HEADLINE VERDICT")
    add("-" * 78)
    for sd, v in result["headline_verdicts"].items():
        neg = (f"  (strong-neg asset: {v['strong_negative_asset']})"
               if v["strong_negative_asset"] else "")
        add(f"  {sd}: net={_fmt(v['net_pnl_mean'])}c/ct  "
            f"CI[{_fmt(v['net_pnl_ci_low'])},{_fmt(v['net_pnl_ci_high'])}]  "
            f"n={v['n']}  -> {v['tag']}{neg}")
    add(bar)
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="v2_audit.py",
        description="Read-only audit of the ultoim_v2 paper-trading system across "
                    "all its config gates. Never writes; never trades.")
    p.add_argument("--db", default=None,
                   help="path to the v2 ledger SQLite (default: "
                        "data/q15_ultoim_v2_v1.sqlite3, then "
                        "/tmp/ledgers/q15_ultoim_v2_v1.sqlite3)")
    p.add_argument("--side", choices=("NO", "YES", "BOTH"), default="BOTH",
                   help="restrict the audit to one side (default BOTH)")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED,
                   help=f"bootstrap RNG seed (default {DEFAULT_SEED}; fixed for "
                        "deterministic CIs)")
    p.add_argument("--resamples", type=int, default=DEFAULT_RESAMPLES,
                   help=f"bootstrap resample count (default {DEFAULT_RESAMPLES})")
    p.add_argument("--json", action="store_true",
                   help="emit the structured result as JSON instead of text")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = resolve_db_path(args.db)
    try:
        rows = load_rows(db_path)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except sqlite3.OperationalError as exc:
        print(f"error: cannot read ledger {db_path}: {exc}", file=sys.stderr)
        return 2

    result = run_audit(rows, side=args.side, seed=args.seed,
                       resamples=args.resamples)
    result["db_path"] = db_path
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        print(render_text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
