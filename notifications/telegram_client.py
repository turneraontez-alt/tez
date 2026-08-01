"""Shared Telegram Bot API ``sendMessage`` client for book-level paper senders.

Single implementation of the delivery mechanics that were duplicated verbatim
across ``q15_upgrade/strategy_bots/telegram.py``,
``q15_upgrade/high_vol_flip/telegram.py`` and
``q15_upgrade/ultoim_v2/telegram.py``: endpoint URL build, HTML ``sendMessage``
POST with a timeout, bounded retry with a short sleep between attempts, and the
structured result dict. Each package keeps its own thin adapter class (public
API, env gates and constructor semantics unchanged) and delegates here.

The live champion path (``notifications/notifier.py`` + ``outbox_v9.py``) is
intentionally NOT built on this client — it has its own hardened delivery code.

Result contract (byte-identical to the historical per-package senders):

* muted (unconfigured/disabled)::

      {"ok": False, "delivered": False, "muted": True,
       "message_id": None, "error": "telegram_unconfigured"}

* delivered::

      {"ok": True, "delivered": True, "muted": False,
       "message_id": <int or None>, "error": None}

* failed after retries::

      {"ok": False, "delivered": False, "muted": False,
       "message_id": None, "error": "HTTP <code>" or "<ExcName>: <msg>"}

Dependency injection for tests: pass ``post`` (a ``requests.post``-compatible
callable) plus optionally ``error_types`` (exception classes the transport may
raise on a transient failure) and ``sleep``. When ``post`` is omitted the client
lazily imports ``requests`` inside ``send`` — exactly like the original senders —
so the packages stay importable without ``requests`` installed.
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable

API_URL_TEMPLATE = "https://api.telegram.org/bot{token}/sendMessage"

# ``requests`` builds its exception text from the request URL, so a transport
# failure echoes ".../bot<TOKEN>/sendMessage" verbatim. Match the token segment
# structurally so a token this client was NOT constructed with is still caught.
_BOT_TOKEN_URL_RE = re.compile(r"(/bot)[^/\s]+", re.IGNORECASE)


def redact_token(text: Any, token: str | None = None) -> Any:
    """Strip a Telegram bot token out of ``text`` before it is logged or stored.

    Two passes, because a leak has two shapes: the configured token appearing
    verbatim anywhere in the string, and a token embedded in an API URL inside a
    transport exception. The second pass is what makes this safe to apply to
    strings whose origin we do not control.

    Falsy input is returned unchanged so callers can pass an optional error
    through without a None check.
    """
    if not text:
        return text
    out = str(text)
    if token:
        out = out.replace(str(token), "***")
    return _BOT_TOKEN_URL_RE.sub(r"\1***", out)


def muted_result() -> dict[str, Any]:
    """The shared 'channel unconfigured' result (fresh dict per call)."""
    return {"ok": False, "delivered": False, "muted": True,
            "message_id": None, "error": "telegram_unconfigured"}


class TelegramSendClient:
    """Bounded-retry HTML ``sendMessage`` sender.

    Semantics are lifted unchanged from the three historical senders:

    * ``enabled`` is ``bool(enabled and token and chat_id)`` — missing
      credentials always mute, regardless of the flag.
    * ``send`` makes ``retries + 1`` attempts; a non-200 status or a transport
      exception is transient, with ``sleep_seconds`` between attempts
      (skippable via ``sleep_seconds=0.0`` in tests).
    * Payload: ``parse_mode="HTML"`` and ``disable_web_page_preview=True``.
    """

    def __init__(self, token: str | None, chat_id: str, *,
                 enabled: bool = True, retries: int = 2,
                 sleep_seconds: float = 0.5, timeout_seconds: float = 8,
                 post: Callable[..., Any] | None = None,
                 error_types: tuple[type[BaseException], ...] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(enabled and self.token and self.chat_id)
        self.retries = max(0, int(retries))
        self.sleep_seconds = max(0.0, float(sleep_seconds))
        self.timeout_seconds = timeout_seconds
        self._post = post
        self._error_types = error_types
        self._sleep = sleep

    def status(self) -> str:
        if self.enabled:
            return "configured"
        if self.token and not self.chat_id:
            return "missing_chat_id"
        if self.chat_id and not self.token:
            return "missing_token"
        return "disabled"

    def send(self, text: str) -> dict[str, Any]:
        """Returns {ok, delivered, muted, message_id, error}. ``muted`` means
        the channel is unconfigured (records still written, no delivery).
        Retries up to ``self.retries`` times on a transient failure."""
        if not self.enabled:
            return muted_result()
        post = self._post
        error_types = self._error_types
        if post is None:
            # Local import: keeps packages importable without requests.
            import requests

            post = requests.post
            if error_types is None:
                error_types = (requests.RequestException,)
        if error_types is None:
            # Injected transport with no declared error types: this send is a
            # deliberate delivery boundary — any transport failure is captured
            # into the structured result rather than propagated.
            error_types = (Exception,)

        last_error: str | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = post(
                    API_URL_TEMPLATE.format(token=self.token),
                    json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                    timeout=self.timeout_seconds,
                )
            except error_types as exc:
                # The exception text embeds the request URL (with the token) —
                # redact before it reaches the result dict, which callers persist
                # into their ledgers (and which those ledgers export off-box).
                last_error = redact_token(f"{type(exc).__name__}: {exc}", self.token)
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
            # Transient failure — back off briefly and retry (skippable in tests).
            if attempt < self.retries and self.sleep_seconds > 0:
                self._sleep(self.sleep_seconds)
        return {"ok": False, "delivered": False, "muted": False,
                "message_id": None, "error": last_error}
