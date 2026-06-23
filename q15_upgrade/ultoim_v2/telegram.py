"""Ultoim V2 — isolated Telegram sender (paper/research channel).

Reuses the live system's bot token (``TELEGRAM_BOT_TOKEN``) but a SEPARATE chat
(``Q15_ULTOIM_V2_TELEGRAM_CHAT_ID``), so V2 paper entry cards land in their own
channel and never touch the live feed. Self-contained (its own POST) so it shares
no state with ``notifications/notifier.py``. Degrades to "muted" (records but does
not deliver) when the chat id is unset.

Adds a bounded retry over ``UltoimTelegram``: up to ``retries`` extra attempts on
a RequestException / non-200, with a short sleep between. ``sleep_seconds`` is
overridable (set 0.0 in tests so retries never block).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger("ultoim_v2.telegram")

_API = "https://api.telegram.org/bot{token}/sendMessage"


class UltoimV2Telegram:
    def __init__(self, chat_id: str, token: str | None = None, *,
                 retries: int = 2, sleep_seconds: float = 0.5) -> None:
        self.token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or ""
        self.enabled = bool(self.token and self.chat_id)
        self.retries = max(0, int(retries))
        self.sleep_seconds = max(0.0, float(sleep_seconds))

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
        channel is unconfigured (research records still written, no delivery).
        Retries up to ``self.retries`` times on a transient failure."""
        if not self.enabled:
            return {"ok": False, "delivered": False, "muted": True,
                    "message_id": None, "error": "telegram_unconfigured"}
        import requests  # local import: keeps the package importable without requests

        last_error: str | None = None
        attempts = self.retries + 1
        for attempt in range(attempts):
            try:
                resp = requests.post(
                    _API.format(token=self.token),
                    json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                          "disable_web_page_preview": True},
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
            # transient failure — back off briefly and retry (skippable in tests)
            if attempt < attempts - 1 and self.sleep_seconds > 0:
                time.sleep(self.sleep_seconds)
        return {"ok": False, "delivered": False, "muted": False,
                "message_id": None, "error": last_error}
