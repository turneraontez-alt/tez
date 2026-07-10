from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import zipfile

from tools.local_backup import create_backup


def _db(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sample(value TEXT)")
    conn.execute("INSERT INTO sample VALUES (?)", (value,))
    conn.commit()
    conn.close()


def test_backup_is_consistent_and_excludes_high_volume_by_default(tmp_path):
    repo = tmp_path / "repo"
    _db(repo / "data" / "q15_strategy_bots_v3.sqlite3", "keep")
    _db(repo / "data" / "q15_coinbase_adv_l2_v1.sqlite3", "rolling")
    (repo / ".env.local").write_text("SECRET_TOKEN=x\nSAFE_FLAG=true\n", encoding="utf-8")

    output = create_backup(
        repo,
        repo / "backups",
        now=datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc),
    )
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert "data/q15_strategy_bots_v3.sqlite3" in names
        assert "data/q15_coinbase_adv_l2_v1.sqlite3" not in names
        assert "config/.env.local" not in names
        assert "config/env_keys.txt" in names
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["format"] == "q15-local-backup-v2"
        assert len(manifest["databases"]) == 1

    restored = tmp_path / "restored.sqlite3"
    with zipfile.ZipFile(output) as archive:
        restored.write_bytes(archive.read("data/q15_strategy_bots_v3.sqlite3"))
    conn = sqlite3.connect(restored)
    assert conn.execute("SELECT value FROM sample").fetchone()[0] == "keep"
    conn.close()
