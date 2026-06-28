"""Telegram notifications for Filtered Alert System v3."""
from __future__ import annotations

import html
import logging
import os
import time
from typing import Any, Mapping

logger = logging.getLogger("strategy_bots.telegram")

_API = "https://api.telegram.org/bot{token}/sendMessage"


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _fmt(value: Any, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    try:
        v = float(value)
        if abs(v) >= 100:
            text = f"{v:.0f}"
        elif abs(v) >= 10:
            text = f"{v:.1f}"
        else:
            text = f"{v:.3f}".rstrip("0").rstrip(".")
        return html.escape(text + suffix)
    except (TypeError, ValueError):
        return html.escape(str(value))


def _bot_label(name: str) -> str:
    return {
        "bnb_no_confirmation": "BNB NO Confirmation",
        "bnb_yes_reversal": "BNB YES Reversal",
        "hype_yes_confirmation": "HYPE YES Confirmation",
        "morefire_btc_confirmed": "MoreFire BTC-Confirmed",
        "baseline_control": "Baseline Control",
    }.get(name, name)


def _metric_parts(row: Mapping[str, Any], specs: list[tuple[str, str, str]]) -> str:
    parts: list[str] = []
    for label, key, suffix in specs:
        if row.get(key) is not None:
            parts.append(f"{label} {_fmt(row.get(key), suffix)}")
    return ", ".join(parts)


def _is_bnb_combined(row: Mapping[str, Any]) -> bool:
    return str(row.get("bot_name") or "") in {"bnb_no_confirmation", "bnb_yes_reversal"}


def _bnb_action(row: Mapping[str, Any]) -> str:
    bot = str(row.get("bot_name") or "")
    status = str(row.get("decision_status") or "").upper()
    reasons = str(row.get("reason_codes") or "")
    if bot == "bnb_yes_reversal":
        return "RESEARCH YES REVERSAL"
    if bot == "bnb_no_confirmation" and "BNB_NO_VETO_" in reasons:
        return "VETO BNB NO"
    if bot == "bnb_no_confirmation" and status == "REJECTED":
        return "REJECT BNB NO"
    if bot == "bnb_no_confirmation" and (status == "ACCEPTED" or not status):
        return "TAKE BNB NO"
    return status or "TRACK BNB"


def build_bnb_combined_alert(row: Mapping[str, Any]) -> str:
    reasons = str(row.get("reason_codes") or "").replace(",", ", ")
    parts = [
        "<b>V3 BNB COMBINED DECISION</b>",
        f"Action: {html.escape(_bnb_action(row))}",
        (
            f"{html.escape(str(row.get('asset') or 'BNB'))} "
            f"{html.escape(str(row.get('side') or ''))} "
            f"{html.escape(str(row.get('interval') or ''))}"
        ),
        f"Bot: {html.escape(_bot_label(str(row.get('bot_name') or '')))}",
        f"Rule: {html.escape(str(row.get('source_rule') or 'UNKNOWN'))}",
        f"Ticker: <code>{html.escape(str(row.get('ticker') or ''))}</code>",
    ]
    if row.get("entry_ask_cents") is not None or row.get("spread_cents") is not None:
        entry = _metric_parts(row, [
            ("ask", "entry_ask_cents", "c"),
            ("spread", "spread_cents", "c"),
        ])
        if entry:
            parts.append(f"Entry: {entry}")
    kalshi = _metric_parts(row, [
        ("depth", "depth_contracts", ""),
        ("YES ask depth", "yes_ask_depth_contracts", ""),
        ("NO ask depth", "no_ask_depth_contracts", ""),
        ("taker net YES 15s", "kalshi_taker_net_yes_volume_15s", ""),
    ])
    if kalshi:
        parts.append(f"Kalshi: {kalshi}")
    spot = _metric_parts(row, [
        ("imb", "spot_depth_imbalance", ""),
        ("sell15", "spot_depth_trade_sell_notional_15s", ""),
        ("net15$", "spot_depth_trade_net_notional_15s", ""),
        ("net60$", "spot_depth_trade_net_notional_60s", ""),
        ("net60qty", "spot_depth_trade_net_qty_60s", ""),
    ])
    if spot:
        parts.append(f"Spot: {spot}")
    if row.get("original_source_side"):
        parts.append(f"Original side: {html.escape(str(row.get('original_source_side')))}")
    parts.append(f"Reasons: {html.escape(reasons)}")
    mode = (
        "research-only tracking"
        if str(row.get("bot_name") or "") == "bnb_yes_reversal"
        else "paper/research tracking"
    )
    parts.append(f"Mode: {mode}")
    return "\n".join(parts)


def build_v3_alert(row: Mapping[str, Any]) -> str:
    if _is_bnb_combined(row):
        return build_bnb_combined_alert(row)

    reasons = str(row.get("reason_codes") or "").replace(",", ", ")
    is_reversal = str(row.get("bot_name") or "") == "bnb_yes_reversal"
    parts = [
        "<b>V3 RESEARCH YES REVERSAL</b>" if is_reversal else "<b>V3 FILTERED PICK</b>",
        (
            f"{html.escape(str(row.get('asset') or ''))} "
            f"{html.escape(str(row.get('side') or ''))} "
            f"{html.escape(str(row.get('interval') or ''))}"
        ),
        f"Bot: {html.escape(_bot_label(str(row.get('bot_name') or '')))}",
        f"Rule: {html.escape(str(row.get('source_rule') or 'UNKNOWN'))}",
        f"Ticker: <code>{html.escape(str(row.get('ticker') or ''))}</code>",
        (
            "Entry: "
            f"{_fmt(row.get('entry_ask_cents'), 'c')} ask, "
            f"{_fmt(row.get('spread_cents'), 'c')} spread"
        ),
    ]
    kalshi = _metric_parts(row, [
        ("depth", "depth_contracts", ""),
        ("YES ask depth", "yes_ask_depth_contracts", ""),
        ("taker net YES 15s", "kalshi_taker_net_yes_volume_15s", ""),
    ])
    if kalshi:
        parts.append(f"Kalshi: {kalshi}")
    spot = _metric_parts(row, [
        ("imb", "spot_depth_imbalance", ""),
        ("sell15", "spot_depth_trade_sell_notional_15s", ""),
        ("net60", "spot_depth_trade_net_qty_60s", ""),
    ])
    if spot:
        parts.append(f"Spot: {spot}")
    btc = _metric_parts(row, [
        ("depth", "btc_depth_contracts", ""),
        ("pressure", "btc_book_pressure_cents", "c"),
    ])
    if row.get("btc_dominant_side"):
        btc = (btc + ", " if btc else "") + f"side {html.escape(str(row.get('btc_dominant_side')))}"
    if btc:
        parts.append(f"BTC: {btc}")
    if row.get("original_source_side"):
        parts.append(f"Original side: {html.escape(str(row.get('original_source_side')))}")
    parts.append(f"Reasons: {html.escape(reasons)}")
    parts.append("Mode: research-only tracking" if is_reversal else "Mode: paper/research tracking")
    return "\n".join(parts)


class V3Telegram:
    def __init__(self, *, token: str | None = None, chat_id: str | None = None,
                 enabled: bool | None = None, retries: int = 2,
                 sleep_seconds: float = 0.5) -> None:
        self.token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = (
            chat_id
            if chat_id is not None
            else (os.environ.get("Q15_V3_TELEGRAM_CHAT_ID") or "")
        )
        active = _bool("Q15_V3_TELEGRAM_ENABLED", False) if enabled is None else bool(enabled)
        self.enabled = bool(active and self.token and self.chat_id)
        self.retries = max(0, int(retries))
        self.sleep_seconds = max(0.0, float(sleep_seconds))

    def send(self, text: str) -> dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "delivered": False, "muted": True,
                    "message_id": None, "error": "telegram_unconfigured"}
        import requests

        last_error: str | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = requests.post(
                    _API.format(token=self.token),
                    json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                    timeout=8,
                )
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 200:
                    message_id = None
                    try:
                        message_id = int(resp.json()["result"]["message_id"])
                    except (ValueError, KeyError, TypeError):
                        message_id = None
                    return {"ok": True, "delivered": True, "muted": False,
                            "message_id": message_id, "error": None}
                last_error = f"HTTP {resp.status_code}"
            if attempt < self.retries and self.sleep_seconds > 0:
                time.sleep(self.sleep_seconds)
        return {"ok": False, "delivered": False, "muted": False,
                "message_id": None, "error": last_error}
