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


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


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


def _signed_cents(value: Any) -> str:
    v = _num(value)
    return "?" if v is None else f"{v:+.1f}c"


def _depth_ratio(row: Mapping[str, Any]) -> float | None:
    direct = _num(row.get("selected_depth_ratio"))
    if direct is not None:
        return direct
    side = str(row.get("selected_side") or row.get("predicted_outcome") or "").upper()
    if side == "YES":
        bid_depth = _num(row.get("yes_bid_depth_contracts"))
        ask_depth = _num(row.get("yes_ask_depth_contracts"))
    elif side == "NO":
        bid_depth = _num(row.get("no_bid_depth_contracts"))
        ask_depth = _num(row.get("no_ask_depth_contracts"))
    else:
        return None
    if bid_depth is None or ask_depth is None or ask_depth <= 0.0:
        return None
    return bid_depth / ask_depth


def _depth_line(row: Mapping[str, Any]) -> str:
    side = str(row.get("selected_side") or row.get("predicted_outcome") or "").upper()
    if side == "YES":
        bid_depth = row.get("yes_bid_depth_contracts")
        ask_depth = row.get("yes_ask_depth_contracts")
    elif side == "NO":
        bid_depth = row.get("no_bid_depth_contracts")
        ask_depth = row.get("no_ask_depth_contracts")
    else:
        bid_depth = ask_depth = None
    ratio = _depth_ratio(row)
    ratio_txt = "missing" if ratio is None else f"{ratio:.2f}"
    if bid_depth is None and ask_depth is None:
        return f"Depth ratio: {ratio_txt}"
    return (
        f"Depth ratio: {ratio_txt} "
        f"(bid {_cents(bid_depth)} / ask {_cents(ask_depth)})"
    )


def build_alert(row: Mapping[str, Any]) -> str:
    title = row.get("alert_title") or "HIGH VOLATILITY FLIP"
    interval = _esc(row.get("interval"))
    asset = _esc(row.get("asset"))
    side = _esc(str(row.get("predicted_outcome") or "").upper())
    header = f"<b>HVF | {interval} | PAPER ALERT</b>"
    lines = [
        f"{_esc(title)} - paper only, NOT a live call. No orders placed.",
        "",
        f"BEST HVF - {asset} {side}",
        f"Ticker: {_esc(row.get('ticker'))}",
        f"Rule: {_esc(row.get('rule_name'))}",
        f"Interval: {interval} | Window: {_esc(row.get('window_key'))}",
        f"Time left: {_remaining(row.get('seconds_remaining'))}",
        "Selected bid/ask: "
        f"{_cents(row.get('selected_bid_cents'))}/{_cents(row.get('selected_ask_cents'))}",
        _depth_line(row),
        f"Model YES: {_prob(row.get('model_yes_probability'))}",
    ]
    if row.get("selected_mid_jump_cents") is not None:
        lines.append(f"Selected mid jump: {_signed_cents(row.get('selected_mid_jump_cents'))}")
    if row.get("btc_dominant_side"):
        btc = (
            f"BTC {_esc(row.get('btc_dominant_side'))} "
            f"{_cents(row.get('btc_selected_bid_cents'))}/"
            f"{_cents(row.get('btc_selected_ask_cents'))}"
        )
        if row.get("btc_jump_cents") is not None:
            btc += f" | jump {_signed_cents(row.get('btc_jump_cents'))}"
        lines.append(f"BTC context: {btc}")
    lines.append("Paper-only: tracking performance, no trade placed")
    return header + "\n<pre>" + "\n".join(lines) + "</pre>"
