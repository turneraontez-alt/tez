"""Entry Economics (entry-econ-v1) — a separate, read-only trade-quality system.

It answers ONE question per contract: "Even if the predicted side is correct, is
this contract currently worth buying at an executable price?" — and returns one
of ENTER / WAIT / SKIP with the full executable economics behind it.

It is decoupled from the directional model (which side will settle) and from the
frozen production entry gate. With ``Q15_ENTRY_ECON_ENABLED`` off, nothing here
runs; the optional entry-performance ledger writes only when
``Q15_ENTRY_ECON_LEDGER`` is on.

Public surface
--------------
- ``EntryEconConfig``           env-driven config (additive; pure compute on)
- ``evaluate_analysis``         v9.5 analysis dict -> EntryEconomicsResult
- ``EntryEconomicsResult``      decision + conservative prob + economics
- ``render_panel``              the compact Telegram panel
- ``EntryLedger``               separate entry-performance ledger
- ``conservative_probability``  the conservative-probability builder
- ``compute_economics``         the executable economics core
- ``kalshi_fee_cents``          the ceil-rounded Kalshi fee
- ``ENTER`` / ``WAIT`` / ``SKIP``
"""

from __future__ import annotations

from .config import ENTRY_ECONOMICS_VERSION, EntryEconConfig
from .conservative import ConservativeResult, conservative_probability
from .costs import CostBreakdown, FillEstimate, all_in_costs, estimate_fill, kalshi_fee_cents
from .decision import ENTER, SKIP, WAIT, EntryDecision, blocker_text, decide
from .economics import TradeEconomics, compute_economics
from .engine import EntryEconomicsResult, evaluate_analysis
from .ledger import EntryLedger
from .panel import render_panel

__all__ = [
    "ENTRY_ECONOMICS_VERSION", "EntryEconConfig",
    "ConservativeResult", "conservative_probability",
    "CostBreakdown", "FillEstimate", "all_in_costs", "estimate_fill", "kalshi_fee_cents",
    "ENTER", "WAIT", "SKIP", "EntryDecision", "blocker_text", "decide",
    "TradeEconomics", "compute_economics",
    "EntryEconomicsResult", "evaluate_analysis",
    "EntryLedger", "render_panel",
]
