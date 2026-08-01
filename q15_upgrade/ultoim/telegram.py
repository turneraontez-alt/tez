"""Ultoim Build — isolated Telegram sender.

Reuses the live system's bot token (``TELEGRAM_BOT_TOKEN``) but a SEPARATE chat
(``Q15_ULTOIM_TELEGRAM_CHAT_ID``), so Ultoim reports land in their own channel
and never touch the live feed. Self-contained (its own POST) so it shares no
state with ``notifications/notifier.py``. Degrades to "muted" (records but does
not deliver) when the chat id is unset.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("ultoim.telegram")

_API = "https://api.telegram.org/bot{token}/sendMessage"


class UltoimTelegram:
    def __init__(self, chat_id: str, token: str | None = None) -> None:
        self.token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or ""
        self.enabled = bool(self.token and self.chat_id)

    def status(self) -> str:
        if self.enabled:
            return "configured"
        if self.token and not self.chat_id:
            return "missing_chat_id"
        if self.chat_id and not self.token:
            return "missing_token"
        return "disabled"

    def send(self, text: str) -> dict[str, Any]:
        """Returns {ok, delivered, muted, message_id, error}. ``muted`` means the
        channel is unconfigured (research records still written, no delivery)."""
        if not self.enabled:
            return {"ok": False, "delivered": False, "muted": True,
                    "message_id": None, "error": "telegram_unconfigured"}
        import requests  # local import: keeps the package importable without requests

        try:
            resp = requests.post(
                _API.format(token=self.token),
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
                timeout=8,
            )
        except requests.RequestException as exc:
            # The exception text embeds the request URL (with the token) — redact
            # before it reaches the result dict, which callers persist.
            from notifications.telegram_client import redact_token

            return {"ok": False, "delivered": False, "muted": False,
                    "message_id": None,
                    "error": redact_token(f"{type(exc).__name__}: {exc}", self.token)}
        if resp.status_code != 200:
            return {"ok": False, "delivered": False, "muted": False,
                    "message_id": None, "error": f"HTTP {resp.status_code}"}
        message_id = None
        try:
            message_id = int(resp.json()["result"]["message_id"])
        except (ValueError, KeyError, TypeError):
            message_id = None
        return {"ok": True, "delivered": True, "muted": False,
                "message_id": message_id, "error": None}
