"""Telegram notifier.

Reads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from Secrets only. Degrades
gracefully to "disabled" when not configured. The bot token NEVER appears in
logs, API responses, or error messages: every outbound string is redacted.
"""
import os
import logging

import requests
from q15_upgrade.calibrated_edge import augment_telegram_message  # Q15_V6_ALERT_AUGMENT

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        self.enabled = bool(self.token and self.chat_id)
        self.sent_count = 0
        self.last_error = None
        self.last_sent_at = None

    def status(self):
        if self.enabled:
            return "configured"
        if self.token and not self.chat_id:
            return "missing_chat_id"
        if self.chat_id and not self.token:
            return "missing_token"
        return "disabled"

    def _redact(self, s):
        """Strip the bot token from any string before it is logged/exposed."""
        if not s:
            return s
        s = str(s)
        if self.token:
            s = s.replace(self.token, "***")
        return s

    def send(self, text):
        if not self.enabled:
            return False
        text = augment_telegram_message(text)
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            resp = requests.post(
                url,
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=8,
            )
            if resp.status_code == 200:
                self.sent_count += 1
                import time as _t
                self.last_sent_at = _t.time()
                return True
            # Telegram's error body does not echo the token, but be defensive.
            self.last_error = self._redact(f"HTTP {resp.status_code}: {resp.text[:200]}")
            logger.warning("Telegram send failed: %s", self.last_error)
            return False
        except Exception as e:
            # requests exceptions can embed the full URL (with token) -> redact.
            self.last_error = self._redact(str(e))
            logger.warning("Telegram send error: %s", self.last_error)
            return False
