"""Shared Telegram client (notifications/telegram_client.py) + the three
book-level adapters that delegate to it (strategy_bots / high_vol_flip /
ultoim_v2).

The adapters' public APIs and result-dict shapes are a frozen compatibility
contract (Stage 4 refactor is behavior-preserving); these tests pin the shared
mechanics — success, HTTP error, transport exception, mute-when-unconfigured,
bounded retry with sleep, HTML parse_mode — and verify each adapter delegates
without changing its historical semantics.
"""
from __future__ import annotations

import sys
import types

import pytest

from notifications.telegram_client import (
    API_URL_TEMPLATE,
    TelegramSendClient,
    muted_result,
    redact_token,
)


class _Resp:
    def __init__(self, status_code=200, message_id=77, payload=None):
        self.status_code = status_code
        self._payload = (
            payload if payload is not None
            else {"ok": True, "result": {"message_id": message_id}}
        )

    def json(self):
        return self._payload


class _Post:
    """requests.post-compatible stub; scripts one outcome per attempt."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        outcome = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _client(post, **over):
    kw = dict(token="tok", chat_id="chat", sleep_seconds=0.0,
              post=post, error_types=(ValueError,))
    kw.update(over)
    return TelegramSendClient(kw.pop("token"), kw.pop("chat_id"), **kw)


# ---------------------------------------------------------------- success path

def test_send_success_result_shape_and_payload():
    post = _Post([_Resp(message_id=41)])
    client = _client(post)

    result = client.send("<b>hello</b>")

    assert result == {"ok": True, "delivered": True, "muted": False,
                      "message_id": 41, "error": None}
    assert len(post.calls) == 1
    call = post.calls[0]
    assert call["url"] == API_URL_TEMPLATE.format(token="tok")
    assert call["json"]["chat_id"] == "chat"
    assert call["json"]["text"] == "<b>hello</b>"
    assert call["json"]["parse_mode"] == "HTML"
    assert call["json"]["disable_web_page_preview"] is True
    assert call["timeout"] == 8


def test_send_success_with_unparseable_message_id():
    post = _Post([_Resp(payload={"ok": True, "result": {}})])
    result = _client(post).send("x")
    assert result == {"ok": True, "delivered": True, "muted": False,
                      "message_id": None, "error": None}


# --------------------------------------------------------------- failure paths

def test_http_error_exhausts_retries_and_reports_last_status():
    post = _Post([_Resp(status_code=500)])
    client = _client(post, retries=2)

    result = client.send("x")

    assert result == {"ok": False, "delivered": False, "muted": False,
                      "message_id": None, "error": "HTTP 500"}
    assert len(post.calls) == 3  # retries + 1 attempts


def test_transport_exception_captured_into_error_dict():
    post = _Post([ValueError("boom")])
    result = _client(post, retries=0).send("x")
    assert result == {"ok": False, "delivered": False, "muted": False,
                      "message_id": None, "error": "ValueError: boom"}


def test_undeclared_error_types_with_injected_transport_still_captured():
    post = _Post([RuntimeError("socket down")])
    client = TelegramSendClient("tok", "chat", retries=0, sleep_seconds=0.0,
                                post=post, error_types=None)
    result = client.send("x")
    assert result["ok"] is False
    assert result["error"] == "RuntimeError: socket down"


# ------------------------------------------------------- bot-token redaction
# A transport exception's text is built from the request URL, so it embeds
# ".../bot<TOKEN>/sendMessage". Callers persist result["error"] into their
# ledgers and those ledgers get exported off-box, so the token must never
# survive into the result dict.

_REAL_TOKEN = "123456789:AAFake_Token_Value_For_Test_Only"


def test_transport_exception_does_not_leak_token_into_result():
    exc = ValueError(
        "HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries "
        f"exceeded with url: /bot{_REAL_TOKEN}/sendMessage (Caused by "
        "NewConnectionError('failed to resolve'))"
    )
    result = _client(_Post([exc]), token=_REAL_TOKEN, retries=0).send("x")

    assert _REAL_TOKEN not in result["error"]
    assert "/bot***/sendMessage" in result["error"]
    assert result["error"].startswith("ValueError: ")


def test_redact_token_strips_a_token_the_client_was_not_built_with():
    # Defence in depth: a stale/rotated token in the string still gets scrubbed
    # even though it does not match the configured one.
    other = "987654321:BBSomeOtherBotToken"
    scrubbed = redact_token(f"ConnectionError: url /bot{other}/sendMessage", "tok")
    assert other not in scrubbed
    assert "/bot***/sendMessage" in scrubbed


@pytest.mark.parametrize("value", [None, "", 0])
def test_redact_token_passes_falsy_through_unchanged(value):
    assert redact_token(value, "tok") is value


def test_redact_token_replaces_verbatim_token_outside_a_url():
    assert redact_token(f"auth failed for {_REAL_TOKEN}", _REAL_TOKEN) == (
        "auth failed for ***"
    )


# ------------------------------------------------------------------ mute gates

@pytest.mark.parametrize("token,chat_id,enabled", [
    (None, "chat", True),      # missing token
    ("tok", "", True),         # missing chat id
    (None, "", True),          # nothing configured
    ("tok", "chat", False),    # explicitly disabled
])
def test_muted_when_unconfigured_or_disabled(token, chat_id, enabled):
    post = _Post([_Resp()])
    client = TelegramSendClient(token, chat_id, enabled=enabled,
                                sleep_seconds=0.0, post=post)

    result = client.send("x")

    assert result == {"ok": False, "delivered": False, "muted": True,
                      "message_id": None, "error": "telegram_unconfigured"}
    assert result == muted_result()
    assert post.calls == []  # no network attempt when muted


def test_status_strings():
    assert TelegramSendClient("tok", "chat").status() == "configured"
    assert TelegramSendClient("tok", "").status() == "missing_chat_id"
    assert TelegramSendClient(None, "chat").status() == "missing_token"
    assert TelegramSendClient(None, "").status() == "disabled"
    assert TelegramSendClient("tok", "chat", enabled=False).status() == "disabled"


# -------------------------------------------------------------- retry behavior

def test_retry_then_success():
    post = _Post([ValueError("transient"), _Resp(message_id=9)])
    result = _client(post, retries=2).send("x")
    assert result == {"ok": True, "delivered": True, "muted": False,
                      "message_id": 9, "error": None}
    assert len(post.calls) == 2  # stopped as soon as it succeeded


def test_retry_sleeps_between_attempts_but_not_after_last():
    sleeps = []
    post = _Post([_Resp(status_code=429)])
    client = TelegramSendClient("tok", "chat", retries=2, sleep_seconds=0.25,
                                post=post, error_types=(ValueError,),
                                sleep=sleeps.append)

    result = client.send("x")

    assert result["error"] == "HTTP 429"
    assert len(post.calls) == 3
    assert sleeps == [0.25, 0.25]  # between attempts only, never after the last


def test_zero_sleep_seconds_never_sleeps():
    sleeps = []
    post = _Post([_Resp(status_code=500)])
    client = TelegramSendClient("tok", "chat", retries=2, sleep_seconds=0.0,
                                post=post, error_types=(ValueError,),
                                sleep=sleeps.append)
    client.send("x")
    assert sleeps == []


def test_retries_and_sleep_clamped_to_non_negative():
    client = TelegramSendClient("tok", "chat", retries=-3, sleep_seconds=-1.0,
                                post=_Post([_Resp(status_code=500)]))
    assert client.retries == 0
    assert client.sleep_seconds == 0.0
    assert client.send("x")["error"] == "HTTP 500"


# ------------------------------------------------- adapter delegation contract

@pytest.fixture
def fake_requests(monkeypatch):
    """Stand-in ``requests`` module so the adapters' default transport (the
    client's lazy ``import requests``) is exercised without a network."""
    mod = types.ModuleType("requests")

    class RequestException(Exception):
        pass

    mod.RequestException = RequestException
    mod.calls = []

    def post(url, json=None, timeout=None):
        mod.calls.append({"url": url, "json": json, "timeout": timeout})
        return _Resp(message_id=5)

    mod.post = post
    monkeypatch.setitem(sys.modules, "requests", mod)
    return mod


def _assert_delivered_via_shared_client(sender, fake_requests):
    result = sender.send("hello")
    assert result == {"ok": True, "delivered": True, "muted": False,
                      "message_id": 5, "error": None}
    call = fake_requests.calls[-1]
    assert call["json"]["parse_mode"] == "HTML"
    assert call["timeout"] == 8


def test_ultoim_v2_adapter_delegates(fake_requests):
    from q15_upgrade.ultoim_v2.telegram import UltoimV2Telegram

    tg = UltoimV2Telegram("v2-room", "tok")
    assert (tg.enabled, tg.retries, tg.sleep_seconds) == (True, 2, 0.5)
    assert tg.status() == "configured"
    _assert_delivered_via_shared_client(tg, fake_requests)
    # Outbox-compatible alias preserved.
    assert tg.send_with_result("hello")["delivered"] is True


def test_ultoim_v2_adapter_muted_without_chat(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    from q15_upgrade.ultoim_v2.telegram import UltoimV2Telegram

    tg = UltoimV2Telegram("", "tok")
    assert tg.status() == "missing_chat_id"
    assert tg.send("x") == {"ok": False, "delivered": False, "muted": True,
                            "message_id": None, "error": "telegram_unconfigured"}


def test_high_vol_flip_adapter_delegates_and_respects_enabled_flag(fake_requests):
    from q15_upgrade.high_vol_flip.telegram import HighVolFlipTelegram

    tg = HighVolFlipTelegram("hvf-room", "tok", enabled=True)
    assert tg.status() == "configured"
    _assert_delivered_via_shared_client(tg, fake_requests)

    disabled = HighVolFlipTelegram("hvf-room", "tok", enabled=False)
    assert disabled.enabled is False
    assert disabled.status() == "disabled"
    assert disabled.send("x")["muted"] is True


def test_v3_adapter_env_gate_and_dedicated_chat(monkeypatch, fake_requests):
    from q15_upgrade.strategy_bots.telegram import V3Telegram

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("Q15_V3_TELEGRAM_CHAT_ID", "v3-room")

    # Gate defaults OFF: fully configured but still muted.
    monkeypatch.delenv("Q15_V3_TELEGRAM_ENABLED", raising=False)
    muted = V3Telegram()
    assert muted.enabled is False
    assert muted.send("x")["muted"] is True

    monkeypatch.setenv("Q15_V3_TELEGRAM_ENABLED", "true")
    live = V3Telegram()
    assert (live.enabled, live.chat_id) == (True, "v3-room")
    _assert_delivered_via_shared_client(live, fake_requests)
