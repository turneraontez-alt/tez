"""Shadow entry-economics — a read-only A/B of a more cost-aware, selective entry
gate, run BESIDE the live one so both can be judged on the same settled outcomes.

The review found the system is ~67% right on direction yet loses money: the live
gate enters whenever ``net_edge = P*100 - ask - cost >= required_edge`` (4-6c),
but ``cost`` ignores slippage + adverse selection and the gate is insensitive to
how thin the edge is. This module proposes a stricter gate and lets you SEE which
one would have made more money — it never changes the live recommendation.

Both gates are evaluated on identical, already-settled rows (same side, same
realized outcome), so the comparison isolates the ONE thing that differs: which
trades each gate lets in.

  * CONTROL (replicates production exactly): enter iff ``net_edge >= required_edge``.
  * SHADOW (the proposal): charge the extra real costs the live model omits
    (slippage + adverse selection), require the edge to clear a hard no-trade
    floor, and require a risk-adjusted EV ``net_edge - k*sigma >= 0`` (sigma is the
    binary outcome's cent-stdev, so a 55% edge is penalised more than a 70% one).
    The shadow's realized P&L is also charged those extra modelled cents, so it
    must EARN its selectivity — it gets no free advantage.

Pure logic (no DB, no network); the ledger feeds it resolved rows and the hourly
report renders the comparison. Default-ON but read-only.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


# Production required-edge defaults, mirrored so CONTROL == the live price gate.
_REQUIRED_EDGE_DEFAULT = {"15M": 4.0, "10M": 6.0, "7M": 6.0}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def required_edge_for(checkpoint: str) -> float:
    """The live required-edge for a checkpoint — same default + same env var the
    production gate reads, so the control arm matches production exactly."""
    cp = str(checkpoint).upper()
    base = _REQUIRED_EDGE_DEFAULT.get(cp, 4.0)
    return _env_float(f"Q15_V95_{cp}_REQUIRED_EDGE_CENTS", base)


@dataclass
class EconConfig:
    enabled: bool = True
    slippage_cents: float = 0.5            # extra cost the live model omits
    adverse_selection_cents: float = 0.25  # extra cost the live model omits
    no_trade_zone_cents: float = 1.0       # hard floor: never enter a razor-thin edge
    risk_aversion_k: float = 0.04          # penalty per cent of outcome stdev

    @classmethod
    def from_env(cls) -> "EconConfig":
        def _b(name, d):
            v = os.environ.get(name)
            return d if v is None else v.strip().lower() not in {"0", "false", "no", "off"}
        return cls(
            enabled=_b("Q15_SHADOW_ECON_ENABLED", True),
            slippage_cents=_env_float("Q15_SHADOW_ECON_SLIPPAGE_CENTS", 0.5),
            adverse_selection_cents=_env_float("Q15_SHADOW_ECON_ADVERSE_CENTS", 0.25),
            no_trade_zone_cents=_env_float("Q15_SHADOW_ECON_NO_TRADE_ZONE_CENTS", 1.0),
            risk_aversion_k=_env_float("Q15_SHADOW_ECON_RISK_K", 0.04),
        )

    @property
    def extra_cost_cents(self) -> float:
        """The cents the shadow charges on top of the live cost estimate."""
        return self.slippage_cents + self.adverse_selection_cents


@dataclass
class GateDecision:
    enter: bool
    net_edge_cents: float | None       # P*100 - ask - cost (gate's own cost basis)
    risk_adjusted_ev: float | None     # net_edge - k*sigma (shadow only; None for control)
    reason: str


def _sigma_cents(prob: float) -> float:
    """Cent-stdev of the binary payoff at probability ``prob`` (max at 0.5)."""
    p = min(max(float(prob), 0.0), 1.0)
    return 100.0 * math.sqrt(p * (1.0 - p))


def control_gate(prob: float | None, ask_cents: float | None, cost_cents: float,
                 required_edge: float) -> GateDecision:
    """Exactly the live production price gate: enter iff net_edge >= required_edge."""
    if prob is None or ask_cents is None:
        return GateDecision(False, None, None, "no_executable_ask")
    net = prob * 100.0 - ask_cents - cost_cents
    return GateDecision(net >= required_edge, net, None,
                        "enter" if net >= required_edge else "edge_below_required")


def shadow_gate(prob: float | None, ask_cents: float | None, cost_cents: float,
                required_edge: float, cfg: EconConfig) -> GateDecision:
    """The proposed stricter gate: charge omitted costs, enforce a no-trade floor,
    and require a non-negative risk-adjusted EV."""
    if prob is None or ask_cents is None:
        return GateDecision(False, None, None, "no_executable_ask")
    net = prob * 100.0 - ask_cents - (cost_cents + cfg.extra_cost_cents)
    risk_adj = net - cfg.risk_aversion_k * _sigma_cents(prob)
    if net < required_edge:
        return GateDecision(False, net, risk_adj, "edge_below_required")
    if net < cfg.no_trade_zone_cents:
        return GateDecision(False, net, risk_adj, "inside_no_trade_zone")
    if risk_adj < 0.0:
        return GateDecision(False, net, risk_adj, "risk_adjusted_ev_negative")
    return GateDecision(True, net, risk_adj, "enter")


@dataclass
class ArmResult:
    n_entries: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl_cents: float = 0.0

    def add(self, correct: bool, pnl: float) -> None:
        self.n_entries += 1
        if correct:
            self.wins += 1
        else:
            self.losses += 1
        self.total_pnl_cents += pnl

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.n_entries if self.n_entries else None

    @property
    def avg_pnl_cents(self) -> float | None:
        return self.total_pnl_cents / self.n_entries if self.n_entries else None


@dataclass
class Comparison:
    rows_considered: int
    control: ArmResult
    shadow: ArmResult
    cfg: EconConfig

    @property
    def pnl_delta_cents(self) -> float:
        return self.shadow.total_pnl_cents - self.control.total_pnl_cents


def compare(rows: Sequence[Mapping[str, Any]], cfg: EconConfig | None = None) -> Comparison:
    """Run both gates over resolved rows and tally P&L on identical outcomes.

    Each row needs: ``checkpoint``, ``prob`` (conservative probability),
    ``ask_cents`` (executable ask), ``cost_cents`` (live cost estimate),
    ``correct`` (bool), ``realized_cents`` (the live paper P&L for the actual side).

    Control P&L uses ``realized_cents`` as-is. Shadow P&L subtracts the extra
    modelled cents per entry, so the shadow must overcome its own stricter cost
    assumptions — it is never flattered."""
    cfg = cfg or EconConfig()
    control, shadow = ArmResult(), ArmResult()
    considered = 0
    for r in rows:
        ask = r.get("ask_cents")
        realized = r.get("realized_cents")
        prob = r.get("prob")
        if ask is None or realized is None or prob is None:
            continue
        considered += 1
        req = required_edge_for(r.get("checkpoint", ""))
        cost = float(r.get("cost_cents") or 0.0)
        correct = bool(r.get("correct"))
        cd = control_gate(prob, ask, cost, req)
        if cd.enter:
            control.add(correct, float(realized))
        sd = shadow_gate(prob, ask, cost, req, cfg)
        if sd.enter:
            shadow.add(correct, float(realized) - cfg.extra_cost_cents)
    return Comparison(rows_considered=considered, control=control, shadow=shadow, cfg=cfg)


def _c(x: float | None, signed: bool = False) -> str:
    if x is None:
        return "—"
    return (f"{x:+.0f}¢" if signed else f"{x:.0f}¢")


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{round(x * 100)}%"


def build_report_lines(cmp: Comparison) -> list[str]:
    """Compact, honest side-by-side for the hourly report."""
    cfg = cmp.cfg
    lines = [
        "ENTRY-ECONOMICS SHADOW (read-only A/B)",
        (f"Same settled trades, two gates. Shadow charges +{cfg.extra_cost_cents:.2f}¢ "
         f"costs, a {cfg.no_trade_zone_cents:.0f}¢ floor & risk-adjusted EV."),
        f"{'':<10}{'Trades':>7}{'Win%':>6}{'P/L':>8}{'Avg':>7}",
    ]

    def row(label, arm):
        return (f"{label:<10}{arm.n_entries:>7}{_pct(arm.win_rate):>6}"
                f"{_c(arm.total_pnl_cents, signed=True):>8}{_c(arm.avg_pnl_cents, signed=True):>7}")
    lines.append(row("Live", cmp.control))
    lines.append(row("Shadow", cmp.shadow))
    # Verdict.
    if cmp.control.n_entries == 0 and cmp.shadow.n_entries == 0:
        lines.append("No qualifying entries settled yet.")
    else:
        d = cmp.pnl_delta_cents
        verb = "more" if d >= 0 else "less"
        skipped = cmp.control.n_entries - cmp.shadow.n_entries
        if skipped > 0:
            trades = f"took {skipped} fewer trade{'s' if skipped != 1 else ''}"
        elif skipped < 0:
            trades = f"took {-skipped} more trade{'s' if skipped != -1 else ''}"
        else:
            trades = "took the same trades"
        lines.append(
            f"Shadow {trades}, made {_c(abs(d))} {verb} "
            f"({cmp.rows_considered} settled considered).")
        lines.append("Read-only — live recommendations are unchanged.")
    return lines
