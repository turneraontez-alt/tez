"""Optional shared-token gate on the data surface.

Every route was unauthenticated while the server bound 0.0.0.0, and some disclose
real state — /api/q15-v9/telegram-outbox returns the verbatim text of recent
trading alerts, /api/health returns the full config. The gate is OFF by default
so local use is unchanged; these tests pin both postures.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("Q15_AUTOSTART_REFRESH", "0")

import app as app_module  # noqa: E402


@pytest.fixture()
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def test_api_is_open_when_no_token_is_configured(client, monkeypatch):
    """Default posture: byte-identical to before — no token, no gate."""
    monkeypatch.delenv("Q15_API_TOKEN", raising=False)

    assert client.get("/api/health").status_code != 401


def test_api_requires_the_token_when_configured(client, monkeypatch):
    monkeypatch.setenv("Q15_API_TOKEN", "s3cret")

    assert client.get("/api/health").status_code == 401


def test_bearer_header_is_accepted(client, monkeypatch):
    monkeypatch.setenv("Q15_API_TOKEN", "s3cret")

    resp = client.get("/api/health", headers={"Authorization": "Bearer s3cret"})

    assert resp.status_code != 401


def test_custom_header_is_accepted(client, monkeypatch):
    monkeypatch.setenv("Q15_API_TOKEN", "s3cret")

    resp = client.get("/api/health", headers={"X-Q15-Token": "s3cret"})

    assert resp.status_code != 401


def test_wrong_token_is_rejected(client, monkeypatch):
    monkeypatch.setenv("Q15_API_TOKEN", "s3cret")

    resp = client.get("/api/health", headers={"X-Q15-Token": "guess"})

    assert resp.status_code == 401


def test_outbox_endpoint_is_covered_by_the_gate(client, monkeypatch):
    """The endpoint that discloses alert bodies must not be reachable unauthenticated."""
    monkeypatch.setenv("Q15_API_TOKEN", "s3cret")

    assert client.get("/api/q15-v9/telegram-outbox").status_code == 401


def test_dashboard_stays_open(client, monkeypatch):
    """The HTML shell is deliberately not gated — no login flow exists to replace it."""
    monkeypatch.setenv("Q15_API_TOKEN", "s3cret")

    assert client.get("/").status_code != 401
