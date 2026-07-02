"""Ultoim V2 — isolated Telegram sender (paper/research channel).

Reuses the live system's bot token (``TELEGRAM_BOT_TOKEN``) but a SEPARATE chat
(``Q15_ULTOIM_V2_TELEGRAM_CHAT_ID``), so V2 paper entry cards land in their own
channel and never touch the live feed. Shares no state with
``notifications/notifier.py``. Degrades to "muted" (records but does not
deliver) when the chat id is unset.

Adds a bounded retry over ``UltoimTelegram``: up to ``retries`` extra attempts on
a RequestException / non-200, with a short sleep between. ``sleep_seconds`` is
overridable (set 0.0 in tests so retries never block).

Delivery mechanics live in the shared ``notifications.telegram_client``; this
class is a thin adapter that keeps the historical public API and result shapes.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from notifications.telegram_client import TelegramSendClient

logger = logging.getLogger("ultoim_v2.telegram")


class UltoimV2Telegram:
    def __init__(self, chat_id: str, token: str | None = None, *,
                 retries: int = 2, sleep_seconds: float = 0.5) -> None:
        self._client = TelegramSendClient(
            token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN"),
            chat_id or "",
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
        """Returns {ok, delivered, muted, message_id, error}. ``muted`` means the
        channel is unconfigured (research records still written, no delivery).
        Retries up to ``self.retries`` times on a transient failure."""
        return self._client.send(text)

    def send_with_result(self, text: str) -> dict[str, Any]:
        """Notifier-compatible rich result used by the persistent outbox."""
        return self.send(text)
