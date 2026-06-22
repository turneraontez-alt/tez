"""Compact Telegram panel for entry economics (entry-econ-v1).

Keeps the visible output tight; detailed formulas and diagnostics stay in the
snapshot / ledger. Matches the required layout:

    ENTRY ECONOMICS

    Decision: ENTER / WAIT / SKIP
    Side: SOL NO
    Current ask: 63¢
    Best entry: 57–60¢
    Maximum entry: 61¢
    Conservative probability: 72%
    Net edge: +9¢
    Main blocker: Price too high

When there is no usable executable price, renders the required terse line:

    Decision: SKIP — No reliable executable price
"""

from __future__ import annotations

from .decision import SKIP
from .engine import EntryEconomicsResult

HEADER = "ENTRY ECONOMICS"


def _cents(value, signed: bool = False) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{v:+.0f}¢" if signed else f"{v:.0f}¢"


def _pct(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{round(float(value) * 100)}%"
    except (TypeError, ValueError):
        return "—"


def _range(low, high) -> str:
    """Render an entry band like the spec's "57–60¢" (the ¢ appears once)."""
    try:
        lo = None if low is None else float(low)
        hi = None if high is None else float(high)
    except (TypeError, ValueError):
        return "—"
    if lo is None and hi is None:
        return "—"
    if lo is None:
        return f"{hi:.0f}¢"
    if hi is None:
        return f"{lo:.0f}¢"
    if round(lo) == round(hi):
        return f"{hi:.0f}¢"
    return f"{lo:.0f}–{hi:.0f}¢"


def render_panel(result: EntryEconomicsResult) -> str:
    """Render the compact panel for one contract's entry economics."""
    econ = result.economics
    # No reliable executable price → the required terse SKIP line.
    if econ is None or econ.executable_ask_cents is None or result.main_blocker == "no_executable_price":
        return f"{HEADER}\n\nDecision: SKIP — No reliable executable price"

    side_label = f"{result.asset or ''} {result.side or ''}".strip() or (result.side or "—")
    cons_prob = None if result.conservative is None else result.conservative.conservative_side_prob

    lines = [
        HEADER,
        "",
        f"Decision: {result.decision}",
        f"Side: {side_label}",
        f"Current ask: {_cents(econ.executable_ask_cents)}",
        f"Best entry: {_range(econ.recommended_entry_low_cents, econ.recommended_entry_high_cents)}",
        f"Maximum entry: {_cents(econ.maximum_entry_price_cents)}",
        f"Conservative probability: {_pct(cons_prob)}",
        f"Net edge: {_cents(econ.net_edge_cents, signed=True)}",
        f"Main blocker: {result.main_blocker_text if result.decision != 'ENTER' else '—'}",
    ]
    return "\n".join(lines)
