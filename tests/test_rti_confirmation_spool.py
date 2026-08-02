from __future__ import annotations

import sqlite3

import pytest

from q15_upgrade.rti_confirmation_spool import RTIConfirmationSpool


def _source(value: int = 1):
    return {
        "record_kind": "RTI_PATH_12M_CONFIRM_PROSPECTIVE",
        "ticker": "KXETH-SPOOL",
        "value": value,
    }


def test_spool_is_durable_hash_verified_and_release_gated(tmp_path):
    path = tmp_path / "confirm-spool.sqlite3"
    spool = RTIConfirmationSpool(path)
    assert spool.enqueue(
        dedupe_key="KXETH-SPOOL|confirm-60",
        ticker="KXETH-SPOOL",
        policy_id="confirm-60",
        interval="12M",
        close_time=1800.0,
        target_at=1080.0,
        release_at=1115.0,
        source=_source(),
        now=1080.1,
    ) is True
    assert spool.next_ready(now=1114.999) is None
    ready = spool.next_ready(now=1115.0)
    assert ready is not None
    assert ready["source"] == _source()
    assert spool.pending_intervals(
        ticker="KXETH-SPOOL", close_time=1800.0
    ) == {"12M"}
    status = spool.status()
    assert status["pending"] == 1
    assert status["journal_mode"] == "wal"
    assert status["busy_timeout_ms"] == 250
    assert status["outcome_fields_present"] is False

    spool.close()
    reopened = RTIConfirmationSpool(path)
    assert reopened.next_ready(now=1115.0)["source"] == _source()
    reopened.mark_completed(ready["id"])
    assert reopened.status()["pending"] == 0
    reopened.close()


def test_spool_rejects_dedupe_key_with_different_source(tmp_path):
    spool = RTIConfirmationSpool(tmp_path / "identity.sqlite3")
    kwargs = dict(
        dedupe_key="KXETH-SPOOL|confirm-60",
        ticker="KXETH-SPOOL",
        policy_id="confirm-60",
        interval="12M",
        close_time=1800.0,
        target_at=1080.0,
        release_at=1115.0,
        now=1080.1,
    )
    assert spool.enqueue(source=_source(1), **kwargs) is True
    assert spool.enqueue(source=_source(1), **kwargs) is False
    with pytest.raises(ValueError, match="identity_mismatch"):
        spool.enqueue(source=_source(2), **kwargs)
    spool.close()


def test_spool_detects_source_tampering(tmp_path):
    path = tmp_path / "tampered.sqlite3"
    spool = RTIConfirmationSpool(path)
    spool.enqueue(
        dedupe_key="KXETH-SPOOL|confirm-60",
        ticker="KXETH-SPOOL",
        policy_id="confirm-60",
        interval="12M",
        close_time=1800.0,
        target_at=1080.0,
        release_at=1115.0,
        source=_source(),
        now=1080.1,
    )
    spool.close()
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE rti_confirmation_spool SET source_json='{}'"
    )
    connection.commit()
    connection.close()

    reopened = RTIConfirmationSpool(path)
    with pytest.raises(ValueError, match="source_hash_mismatch"):
        reopened.next_ready(now=1115.0)
    reopened.close()

