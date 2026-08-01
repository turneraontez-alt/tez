"""The GitHub relay's push failures must be VISIBLE in health.

The relay reported a failed push only by printing to its own stdout log, so an
expired GH_PUSH_TOKEN looked exactly like healthy operation from anywhere else:
commits stopped reaching GitHub and nothing said so. These pin the durable
status file and its health projection.
"""
from __future__ import annotations

import json

from routes.api_core import _github_relay_status


def _write(tmp_path, payload):
    path = tmp_path / "relay_status.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_missing_status_file_is_unknown_not_failure(tmp_path, monkeypatch):
    """An older build or a relay that has not run yet must not read as broken."""
    monkeypatch.setenv("GITHUB_RELAY_STATUS_PATH", str(tmp_path / "nope.json"))

    status = _github_relay_status()

    assert status["ok"] is None
    assert status["state"] == "unknown"


def test_healthy_relay_reports_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_RELAY_STATUS_PATH", _write(tmp_path, {
        "consecutive_push_failures": 0, "last_push_ok_at": 1000.0,
        "last_push_error": None, "updated_at": 1000.0}))

    status = _github_relay_status()

    assert status["ok"] is True
    assert status["state"] == "ok"


def test_failing_push_is_surfaced(tmp_path, monkeypatch):
    """The exact condition that went unnoticed: auth rejected, repeatedly."""
    monkeypatch.setenv("GITHUB_RELAY_STATUS_PATH", _write(tmp_path, {
        "consecutive_push_failures": 37,
        "last_push_error": "Invalid username or token.",
        "last_push_error_at": 2000.0, "updated_at": 2000.0}))

    status = _github_relay_status()

    assert status["ok"] is False
    assert status["state"] == "failing"
    assert status["consecutive_push_failures"] == 37
    assert "Invalid username or token" in status["last_push_error"]


def test_malformed_status_does_not_break_health(tmp_path, monkeypatch):
    path = tmp_path / "relay_status.json"
    path.write_text("not json at all", encoding="utf-8")
    monkeypatch.setenv("GITHUB_RELAY_STATUS_PATH", str(path))

    status = _github_relay_status()

    assert status["ok"] is None and status["state"] == "unknown"


def test_relay_writes_status_atomically(tmp_path, monkeypatch):
    """The writer half: a reader must never observe a partial file."""
    import tools.github_relay as relay

    target = tmp_path / "nested" / "relay_status.json"
    monkeypatch.setattr(relay, "STATUS_PATH", str(target))
    monkeypatch.setattr(relay, "_status", dict(relay._status))

    relay._write_status(consecutive_push_failures=3, last_push_error="boom")

    assert target.exists()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["consecutive_push_failures"] == 3
    assert data["last_push_error"] == "boom"
    assert data["updated_at"] is not None
    assert not (tmp_path / "nested" / "relay_status.json.tmp").exists()


def test_status_write_never_raises_on_bad_path(monkeypatch):
    """Status writing is best-effort and must never disturb the relay loop."""
    import tools.github_relay as relay

    monkeypatch.setattr(relay, "STATUS_PATH", "\x00invalid\x00/relay.json")
    monkeypatch.setattr(relay, "_status", dict(relay._status))

    relay._write_status(consecutive_push_failures=1)   # must not raise
