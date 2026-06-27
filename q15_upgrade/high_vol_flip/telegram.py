"""HVF Telegram delivery.

Uses the same bot token and, by default, the same chat id as Ultoim V2 paper
alerts. Delivery is still controlled by ``Q15_HVF_TELEGRAM_ENABLED`` and the HVF
message heading keeps it separate in the room.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger("high_vol_flip.telegram")

_API = "https://api.telegram.org/bot{token}/sendMessage"


class HighVolFlipTelegram:
    def __init__(self, chat_id: str, token: str | None = None, *,
                 enabled: bool = True, retries: int = 2,
                 sleep_seconds: float = 0.5) -> None:
        self.token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or ""
        self.enabled = bool(enabled and self.token and self.chat_id)
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
