from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import zipfile

import pytest

from tools.local_backup import create_backup, verify_backup_archive


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
    (repo / "config").mkdir()
    (repo / "config" / "q15_rti_v22_evaluator_contract.json").write_text(
        '{"frozen":true}\n', encoding="utf-8",
    )
    (repo / "tools").mkdir()
    (repo / "tools" / "q15_rti_v22_readiness.py").write_text(
        "OUTCOME_ACCESS_ALLOWED = False\n", encoding="utf-8",
    )
    watchdog = repo / "work" / "local-run" / "q15-collector-watchdog-v1.json"
    watchdog.parent.mkdir(parents=True)
    watchdog.write_text('{"baseline_missed_deadlines":7}\n', encoding="utf-8")

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
        assert "support/config/q15_rti_v22_evaluator_contract.json" in names
        assert "support/tools/q15_rti_v22_readiness.py" in names
        assert (
            "support/work/local-run/q15-collector-watchdog-v1.json" in names
        )
        assert not any(".env.local" in name for name in names)
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["format"] == "q15-local-backup-v2"
        assert len(manifest["databases"]) == 1
        assert len(manifest["support_files"]) == 3
        for item in manifest["support_files"]:
            assert hashlib.sha256(archive.read(item["path"])).hexdigest() == item[
                "sha256"
            ]

    restored = tmp_path / "restored.sqlite3"
    with zipfile.ZipFile(output) as archive:
        restored.write_bytes(archive.read("data/q15_strategy_bots_v3.sqlite3"))
    conn = sqlite3.connect(restored)
    assert conn.execute("SELECT value FROM sample").fetchone()[0] == "keep"
    conn.close()

    verified = verify_backup_archive(output)
    assert verified["database_count"] == 1
    assert verified["support_file_count"] == 3


def test_critical_backup_keeps_rti_evidence_and_omits_reproducible_state(tmp_path):
    repo = tmp_path / "repo"
    _db(repo / "data" / "q15_strategy_bots_v3.sqlite3", "features")
    _db(repo / "data" / "q15_rti_spot_rest_top_book_v2.sqlite3", "rest")
    _db(repo / "data" / "q15_settlement_index_v1.sqlite3", "reproducible")

    output = create_backup(repo, repo / "backups", critical_only=True)
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert "data/q15_strategy_bots_v3.sqlite3" in names
        assert "data/q15_rti_spot_rest_top_book_v2.sqlite3" in names
        assert "data/q15_settlement_index_v1.sqlite3" not in names
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["critical_only"] is True


def test_backup_verifier_rejects_unmanifested_archive_members(tmp_path):
    repo = tmp_path / "repo"
    _db(repo / "data" / "q15_strategy_bots_v3.sqlite3", "features")
    output = create_backup(repo, repo / "backups", critical_only=True)
    with zipfile.ZipFile(output, "a") as archive:
        archive.writestr("unexpected.txt", "tamper")
    with pytest.raises(RuntimeError, match="members do not match manifest"):
        verify_backup_archive(output)
