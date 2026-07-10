#!/usr/bin/env python3
"""Bound low-value local Q15 files without touching live SQLite databases."""
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
import shutil
import time


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def maintain(
    repo: Path,
    *,
    keep_backups: int = 7,
    diagnostic_days: float = 14.0,
    now: float | None = None,
) -> dict[str, object]:
    repo = repo.resolve()
    now = time.time() if now is None else float(now)
    before = _size(repo)
    compressed: list[str] = []
    removed: list[str] = []

    data_dir = repo / "data"
    for source in sorted(data_dir.glob("q15_v7_diagnostic_events.*.jsonl")):
        if now - source.stat().st_mtime < 600.0:
            continue
        destination = source.with_suffix(source.suffix + ".gz")
        temp = destination.with_suffix(destination.suffix + ".tmp")
        with source.open("rb") as src, gzip.open(temp, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        with gzip.open(temp, "rb") as check:
            while check.read(1024 * 1024):
                pass
        temp.replace(destination)
        source.unlink()
        compressed.append(str(source.relative_to(repo)))

    diagnostic_cutoff = now - max(1.0, diagnostic_days) * 86400.0
    for archive in sorted(data_dir.glob("q15_v7_diagnostic_events.*.jsonl.gz")):
        if archive.stat().st_mtime < diagnostic_cutoff:
            archive.unlink()
            removed.append(str(archive.relative_to(repo)))

    backup_dir = repo / "work" / "local-run" / "backups"
    backups = sorted(
        backup_dir.glob("q15-data-*.zip"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for backup in backups[max(1, int(keep_backups)):]:
        backup.unlink()
        removed.append(str(backup.relative_to(repo)))
    for temp in backup_dir.glob(".q15-data-*.tmp"):
        if now - temp.stat().st_mtime > 86400.0:
            temp.unlink()
            removed.append(str(temp.relative_to(repo)))

    log_dir = repo / "work" / "local-run" / "logs"
    log_cutoff = now - 14.0 * 86400.0
    for old_log in log_dir.glob("*.log.*"):
        if old_log.is_file() and old_log.stat().st_mtime < log_cutoff:
            old_log.unlink()
            removed.append(str(old_log.relative_to(repo)))

    after = _size(repo)
    git_bytes = _size(repo / ".git") if (repo / ".git").exists() else 0
    return {
        "repo": str(repo),
        "bytes_before": before,
        "bytes_after": after,
        "bytes_reclaimed": max(0, before - after),
        "git_bytes": git_bytes,
        "git_warning": git_bytes > 5 * 1024**3,
        "compressed": compressed,
        "removed": removed,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--keep-backups", type=int, default=7)
    parser.add_argument("--diagnostic-days", type=float, default=14.0)
    args = parser.parse_args()
    report = maintain(
        args.repo,
        keep_backups=args.keep_backups,
        diagnostic_days=args.diagnostic_days,
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
