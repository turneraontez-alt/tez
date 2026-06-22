"""Ultoim Build — the compact Telegram report (pure formatter).

Deliberately minimal: medal, asset, side, grade, and the outcome %. No evidence
lists, formulas, feature analysis, or flip internals — those live in the DB for
research only.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def build_report(interval: str, picks: Sequence[Mapping[str, Any]]) -> str:
    """Render the ranked picks (already ordered, rank/grade stamped) as HTML."""
    lines: list[str] = [f"<b>ULTOIM BUILD · {interval}</b>", ""]
    for pick in picks:
        medal = _MEDALS.get(int(pick.get("rank") or 0), "")
        asset = str(pick.get("asset") or "")
        side = str(pick.get("predicted_side") or "")
        grade = str(pick.get("confidence_grade") or "C")
        pct = pick.get("outcome_pct")
        pct_txt = f"{round(float(pct))}%" if pct is not None else "—"
        lines.append(f"{medal} {asset} {side} — Grade {grade}")
        lines.append(f"Outcome: {pct_txt}")
        lines.append("")
    lines.append("Research mode · read-only")
    return "\n".join(lines)
