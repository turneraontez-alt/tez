from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade import ws_client


def test_connect_uses_additional_headers_when_supported(monkeypatch):
    captured = {}

    def fake_connect(url, *, additional_headers, **kwargs):
        captured["url"] = url
        captured["headers"] = additional_headers
        return "connector"

    monkeypatch.setattr(ws_client.websockets, "connect", fake_connect)
    feed = ws_client.KalshiWebSocketFeed()

    out = asyncio.run(feed._connect({"A": "B"}))

    assert out == "connector"
    assert captured["headers"] == {"A": "B"}


def test_connect_uses_extra_headers_for_older_websockets(monkeypatch):
    captured = {}

    def fake_connect(url, *, extra_headers, **kwargs):
        captured["url"] = url
        captured["headers"] = extra_headers
        return "connector"

    monkeypatch.setattr(ws_client.websockets, "connect", fake_connect)
    feed = ws_client.KalshiWebSocketFeed()

    out = asyncio.run(feed._connect({"A": "B"}))

    assert out == "connector"
    assert captured["headers"] == {"A": "B"}
