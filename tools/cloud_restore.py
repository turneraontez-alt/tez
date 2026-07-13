#!/usr/bin/env python3
"""Restore a verified Q15 migration bundle with rollback copies."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
import zipfile


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _destination(state_dir: Path, member: str) -> Path:
    relative = PurePosixPath(member)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe backup path: {member}")
    if len(relative.parts) != 2 or relative.parts[0] not in {"data", "root"}:
        raise ValueError(f"unexpected backup path: {member}")
    if relative.suffix != ".sqlite3":
        raise ValueError(f"unexpected backup file type: {member}")
    if relative.parts[0] == "root":
        return state_dir / relative.name
    return state_dir / "data" / relative.name


def _quick_check(path: Path) -> None:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        row = connection.execute("PRAGMA quick_check").fetchone()
    finally:
        connection.close()
    if not row or str(row[0]).lower() != "ok":
        raise RuntimeError(f"SQLite quick_check failed for {path.name}: {row}")


def restore_backup(archive: Path, state_dir: Path) -> Path:
    archive = archive.resolve()
    state_dir = state_dir.resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "data").mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    rollback_dir = state_dir / "restore-rollback" / stamp

    with zipfile.ZipFile(archive) as source:
        manifest = json.loads(source.read("manifest.json"))
        if manifest.get("format") != "q15-local-backup-v2":
            raise ValueError("unsupported Q15 backup format")
        entries = manifest.get("databases")
        if not isinstance(entries, list) or not entries:
            raise ValueError("backup contains no databases")

        with tempfile.TemporaryDirectory(prefix="q15_restore_", dir=state_dir) as tmp:
            staging = Path(tmp)
            planned: list[tuple[Path, Path]] = []
            for entry in entries:
                member = str(entry["path"])
                target = _destination(state_dir, member)
                staged = staging / target.name
                with source.open(member) as src, staged.open("wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
                if _sha256(staged) != str(entry["sha256"]).lower():
                    raise RuntimeError(f"checksum mismatch for {member}")
                _quick_check(staged)
                planned.append((staged, target))

            rollback_dir.mkdir(parents=True, exist_ok=False)
            moved: list[tuple[Path, Path]] = []
            installed: list[Path] = []
            try:
                for _staged, target in planned:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    for existing in (target, Path(f"{target}-wal"), Path(f"{target}-shm")):
                        if existing.exists():
                            saved = rollback_dir / existing.name
                            if saved.exists():
                                saved = rollback_dir / f"{target.parent.name}-{existing.name}"
                            os.replace(existing, saved)
                            moved.append((saved, existing))
                for staged, target in planned:
                    os.replace(staged, target)
                    installed.append(target)
            except Exception:
                for target in reversed(installed):
                    target.unlink(missing_ok=True)
                for saved, original in reversed(moved):
                    os.replace(saved, original)
                raise

    return rollback_dir


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/q15"))
    args = parser.parse_args()
    rollback = restore_backup(args.archive, args.state_dir)
    print(f"restore complete; prior files retained in {rollback}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
