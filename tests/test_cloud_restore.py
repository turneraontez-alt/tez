from __future__ import annotations

from pathlib import Path
import sqlite3

from tools.cloud_restore import restore_backup
from tools.local_backup import create_backup


def _database(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
        connection.execute("INSERT INTO state VALUES (?)", (value,))
        connection.commit()
    finally:
        connection.close()


def _value(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        return str(connection.execute("SELECT value FROM state").fetchone()[0])
    finally:
        connection.close()


def test_restore_backup_verifies_and_retains_previous_state(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _database(source / "data" / "ledger.sqlite3", "cloud")
    _database(source / "q15_v91_state.sqlite3", "root-cloud")
    archive = create_backup(source, tmp_path / "bundles")

    state = tmp_path / "state"
    _database(state / "data" / "ledger.sqlite3", "old")
    _database(state / "q15_v91_state.sqlite3", "root-old")
    (state / "data" / "ledger.sqlite3-wal").write_bytes(b"stale")

    rollback = restore_backup(archive, state)

    assert _value(state / "data" / "ledger.sqlite3") == "cloud"
    assert _value(state / "q15_v91_state.sqlite3") == "root-cloud"
    assert not (state / "data" / "ledger.sqlite3-wal").exists()
    assert (rollback / "ledger.sqlite3-wal").read_bytes() == b"stale"
    assert _value(rollback / "ledger.sqlite3") == "old"
