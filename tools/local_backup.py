#!/usr/bin/env python3
"""Create a consistent, compressed, bounded-size local Q15 backup."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import zipfile


HIGH_VOLUME_DATABASES = {
    "q15_coinbase_adv_l2_v1.sqlite3",
    "q15_kraken_l3_v1.sqlite3",
    "q15_spot_depth_v1.sqlite3",
    "q15_spot_l3_v1.sqlite3",
}


def _sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=30.0)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst, pages=4096, sleep=0.01)
        result = dst.execute("PRAGMA quick_check").fetchone()
        if not result or str(result[0]).lower() != "ok":
            raise RuntimeError(f"SQLite quick_check failed for {source.name}: {result}")
    finally:
        dst.close()
        src.close()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _env_keys(path: Path) -> list[str]:
    if not path.is_file():
        return []
    keys: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key:
            keys.append(key)
    return sorted(set(keys))


def create_backup(
    repo: Path,
    destination_dir: Path,
    *,
    include_high_volume: bool = False,
    include_secrets: bool = False,
    now: datetime | None = None,
) -> Path:
    repo = repo.resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(timezone.utc)
    stamp = now.astimezone().strftime("%Y%m%d-%H%M%S")
    final_path = destination_dir / f"q15-data-{stamp}.zip"
    temp_zip = destination_dir / f".{final_path.name}.tmp"

    databases = sorted((repo / "data").glob("*.sqlite3"))
    root_state = repo / "q15_v91_state.sqlite3"
    if root_state.is_file():
        databases.append(root_state)
    if not include_high_volume:
        databases = [db for db in databases if db.name not in HIGH_VOLUME_DATABASES]
    if not databases:
        raise RuntimeError("No SQLite databases found to back up")

    manifest: dict[str, object] = {
        "created_at": now.astimezone(timezone.utc).isoformat(),
        "format": "q15-local-backup-v2",
        "include_high_volume": include_high_volume,
        "include_secrets": include_secrets,
        "databases": [],
    }
    try:
        with tempfile.TemporaryDirectory(prefix="q15_backup_") as tmp:
            snapshot_root = Path(tmp)
            for source in databases:
                relative = Path("root" if source.parent == repo else "data") / source.name
                snapshot = snapshot_root / relative
                _sqlite_backup(source, snapshot)
                manifest["databases"].append({
                    "path": relative.as_posix(),
                    "source_bytes": source.stat().st_size,
                    "snapshot_bytes": snapshot.stat().st_size,
                    "sha256": _sha256(snapshot),
                })

            with zipfile.ZipFile(
                temp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
            ) as archive:
                for item in manifest["databases"]:
                    relative = Path(str(item["path"]))
                    archive.write(snapshot_root / relative, relative.as_posix())
                env_file = repo / ".env.local"
                archive.writestr(
                    "config/env_keys.txt",
                    "\n".join(_env_keys(env_file)) + "\n",
                )
                if include_secrets and env_file.is_file():
                    archive.write(env_file, "config/.env.local")
                archive.writestr(
                    "RESTORE.txt",
                    "SQLite files were captured with the online backup API and passed "
                    "PRAGMA quick_check. High-volume market-depth databases are excluded "
                    "by default because they are rolling, reproducible collectors.\n",
                )
                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                )
        temp_zip.replace(final_path)
    except Exception:
        temp_zip.unlink(missing_ok=True)
        raise
    return final_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--include-high-volume", action="store_true")
    parser.add_argument("--include-secrets", action="store_true")
    args = parser.parse_args()
    path = create_backup(
        args.repo,
        args.destination,
        include_high_volume=args.include_high_volume,
        include_secrets=args.include_secrets,
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
