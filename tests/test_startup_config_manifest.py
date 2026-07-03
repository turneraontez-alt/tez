from __future__ import annotations

import json

import cycle_watchdog as cw
import startup_config_manifest as scm


def _manifest(tmp_path, payload):
    path = tmp_path / "expected_runtime_flags.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_runtime_flag_manifest_match(tmp_path):
    path = _manifest(
        tmp_path,
        {
            "Q15_STRANGLE_SHADOW": "true",
            "Q15_STRANGLE_SHADOW_OPEN_MARKS": "780,600,420",
        },
    )
    status = scm.evaluate(
        manifest_path=path,
        env={
            "Q15_STRANGLE_SHADOW": "yes",
            "Q15_STRANGLE_SHADOW_OPEN_MARKS": "780,600,420",
        },
        now=1000.0,
    )

    assert status["available"] is True
    assert status["ok"] is True
    assert status["mismatches"] == []
    assert scm.alert_message(status) is None


def test_runtime_flag_manifest_mismatch_alert_once_with_cooldown(tmp_path, monkeypatch):
    cw.reset()
    monkeypatch.setenv("Q15_HEARTBEAT_COOLDOWN_PATH", str(tmp_path / "cooldown.json"))
    monkeypatch.setenv("Q15_HEARTBEAT_PAGER_COOLDOWN_SECONDS", "600")
    path = _manifest(
        tmp_path,
        {
            "Q15_STRANGLE_SHADOW": "true",
            "Q15_STRANGLE_SHADOW_OPEN_MARKS": "780,600,420",
            "Q15_FEED_LIQ": "true",
        },
    )
    status = scm.evaluate(
        manifest_path=path,
        env={
            "Q15_STRANGLE_SHADOW": "false",
            "Q15_STRANGLE_SHADOW_OPEN_MARKS": "780",
        },
        now=1000.0,
    )
    sent: list[str] = []

    first = scm.maybe_send_alert(status, now=1000.0, send_fn=sent.append)
    second = scm.maybe_send_alert(status, now=1100.0, send_fn=sent.append)
    third = scm.maybe_send_alert(status, now=1701.0, send_fn=sent.append)

    assert first is not None
    assert second is None
    assert third is not None
    assert len(sent) == 2
    assert "Q15 OPS CONFIG MANIFEST" in first
    assert "Q15_STRANGLE_SHADOW" in first
    assert "Q15_FEED_LIQ" in first
    assert "V9.5 CHECK" not in first
    assert "ENTRY RECOMMENDED" not in first
    assert "NO ENTRY YET" not in first
    cw.reset()


def test_missing_runtime_flag_manifest_is_noop(tmp_path):
    status = scm.check_startup_config(
        manifest_path=tmp_path / "missing.json",
        env={},
        now=1000.0,
        send_alert=True,
        send_fn=lambda _msg: (_ for _ in ()).throw(AssertionError("should not send")),
    )

    assert status["available"] is False
    assert status["manifest_present"] is False
    assert status["ok"] is True
    assert status["mismatches"] == []
