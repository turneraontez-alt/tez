from __future__ import annotations

import gzip
from pathlib import Path

from tools.storage_maintenance import maintain


def test_maintenance_compresses_rotated_diagnostics_and_bounds_backups(tmp_path):
    repo = tmp_path / "repo"
    data = repo / "data"
    backups = repo / "work" / "local-run" / "backups"
    data.mkdir(parents=True)
    backups.mkdir(parents=True)
    old = data / "q15_v7_diagnostic_events.123.jsonl"
    old.write_text("event\n" * 100, encoding="utf-8")
    old_time = 1_700_000_000.0
    import os
    os.utime(old, (old_time, old_time))
    for index in range(4):
        backup = backups / f"q15-data-2026070{index}-000000.zip"
        backup.write_bytes(bytes([index]))
        os.utime(backup, (old_time + index, old_time + index))

    report = maintain(repo, keep_backups=2, now=old_time + 3600.0)
    archive = old.with_suffix(".jsonl.gz")
    assert not old.exists() and archive.exists()
    with gzip.open(archive, "rt", encoding="utf-8") as handle:
        assert handle.readline() == "event\n"
    assert len(list(backups.glob("q15-data-*.zip"))) == 2
    assert report["bytes_reclaimed"] > 0
