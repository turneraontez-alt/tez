"""HVF Telegram delivery.

Uses the same bot token and, by default, the same chat id as Ultoim V2 paper
alerts. Delivery is still controlled by ``Q15_HVF_TELEGRAM_ENABLED`` and the HVF
message heading keeps it separate in the room.

Delivery mechanics live in the shared ``notifications.telegram_client``; this
class is a thin adapter that keeps the historical public API and result shapes.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from notifications.telegram_client import TelegramSendClient

logger = logging.getLogger("high_vol_flip.telegram")


class HighVolFlipTelegram:
    def __init__(self, chat_id: str, token: str | None = None, *,
                 enabled: bool = True, retries: int = 2,
                 sleep_seconds: float = 0.5) -> None:
        self._client = TelegramSendClient(
            token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN"),
            chat_id or "",
            enabled=enabled,
            retries=retries,
            sleep_seconds=sleep_seconds,
        )
        self.token = self._client.token
        self.chat_id = self._client.chat_id
        self.enabled = self._client.enabled
        self.retries = self._client.retries
        self.sleep_seconds = self._client.sleep_seconds

    def status(self) -> str:
        return self._client.status()

    def send(self, text: str) -> dict[str, Any]:
        return self._client.send(text)
