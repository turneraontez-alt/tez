"""HVF alert card formatting."""
from __future__ import annotations

import html
from typing import Any, Mapping


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=False)


def _cents(value: Any) -> str:
    try:
        if value is None:
            return "?"
        v = float(value)
    except (TypeError, ValueError):
        return "?"
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.1f}"


def _prob(value: Any) -> str:
    try:
        if value is None:
            return "?"
        return f"{float(value) * 100.0:.1f}%"
    except (TypeError, ValueError):
        return "?"


def _remaining(seconds: Any) -> str:
    try:
        total = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        return "?"
    return f"{total // 60}m {total % 60:02d}s"


def build_alert(row: Mapping[str, Any]) -> str:
    title = row.get("alert_title") or "HIGH VOLATILITY FLIP"
    lines = [
        f"<b>{_esc(title)}</b>",
        f"Asset: <b>{_esc(row.get('asset'))}</b>",
        f"Predicted outcome: <b>{_esc(row.get('predicted_outcome'))}</b>",
        f"Rule: {_esc(row.get('rule_name'))}",
        f"Interval: {_esc(row.get('interval'))}",
        f"Time remaining: {_remaining(row.get('seconds_remaining'))}",
        "Selected-side bid/ask: "
        f"{_cents(row.get('selected_bid_cents'))}/{_cents(row.get('selected_ask_cents'))}",
        f"Model YES probability: {_prob(row.get('model_yes_probability'))}",
    ]
    if row.get("selected_mid_jump_cents") is not None:
        lines.append(f"Selected-side mid jump: {_cents(row.get('selected_mid_jump_cents'))}c")
    if row.get("depth_contracts") is not None:
        lines.append(f"Ask depth: {_cents(row.get('depth_contracts'))} contracts")
    if row.get("btc_dominant_side"):
        btc = (
            f"BTC {_esc(row.get('btc_dominant_side'))} "
            f"{_cents(row.get('btc_selected_bid_cents'))}/"
            f"{_cents(row.get('btc_selected_ask_cents'))}"
        )
        if row.get("btc_jump_cents") is not None:
            btc += f", jump {_cents(row.get('btc_jump_cents'))}c"
        lines.append(f"BTC context: {btc}")
    lines.append("Paper-only: tracking performance, no trade placed")
    return "\n".join(lines)
