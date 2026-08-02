#!/usr/bin/env python3
"""Create a consistent, compressed, bounded-size local Q15 backup."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import zipfile


HIGH_VOLUME_DATABASES = {
    "q15_coinbase_adv_l2_v1.sqlite3",
    "q15_kraken_l3_v1.sqlite3",
    "q15_spot_depth_v1.sqlite3",
    "q15_spot_l3_v1.sqlite3",
}
CRITICAL_DATABASES = {
    "q15_strategy_bots_v3.sqlite3",
    "q15_rti_spot_rest_top_book_v2.sqlite3",
    "q15_rti_confirmation_spool_v1.sqlite3",
    "q15_telegram_outbox.sqlite3",
    "q15_drift_telegram_outbox.sqlite3",
    "q15_executor_orders_v1.sqlite3",
    "q15_executor_yes_orders_v1.sqlite3",
    "q15_v91_state.sqlite3",
}

SUPPORT_SOURCE_RULES = {
    "config": {".json", ".md", ".txt"},
    "q15_upgrade": {".py"},
    "routes": {".py"},
    "scripts/local": {".ps1", ".psm1", ".txt"},
    "tests": {".py"},
    "tools": {".py"},
}
EXPLICIT_SUPPORT_FILES = {
    "app.py",
    "HANDOFF.md",
    "requirements.txt",
    ".env.local.example",
}
RUNTIME_SUPPORT_FILES = {
    "work/local-run/q15-collector-watchdog-v1.json",
    "reports/q15_rti_v22_feature_seal.json",
}
ALLOWED_JUNCTION_RUNTIME_FILES = {
    "work/local-run/q15-collector-watchdog-v1.json",
}
RUNTIME_SUPPORT_DIRECTORIES = {
    "reports/q15_rti_v22_audit": {".json", ".joblib"},
}
MAX_SUPPORT_FILE_BYTES = 100 * 1024 * 1024
MAX_SUPPORT_TOTAL_BYTES = 512 * 1024 * 1024


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


def _archive_member_sha256(archive: zipfile.ZipFile, member: str) -> str:
    digest = hashlib.sha256()
    with archive.open(member) as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_backup_archive(path: Path) -> dict[str, object]:
    """Verify ZIP geometry, CRCs, sizes, and every manifest checksum."""
    path = Path(path)
    with zipfile.ZipFile(path) as archive:
        members = archive.namelist()
        if len(members) != len(set(members)):
            raise RuntimeError("Backup archive contains duplicate members")
        bad_member = archive.testzip()
        if bad_member is not None:
            raise RuntimeError(f"Backup ZIP CRC failed: {bad_member}")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("format") != "q15-local-backup-v2":
            raise RuntimeError("Backup manifest format is invalid")
        databases = manifest.get("databases")
        support_files = manifest.get("support_files")
        if not isinstance(databases, list) or not databases:
            raise RuntimeError("Backup manifest has no databases")
        if not isinstance(support_files, list):
            raise RuntimeError("Backup manifest support files are invalid")
        expected = {
            "manifest.json", "RESTORE.txt", "config/env_keys.txt",
            *(str(item.get("path") or "") for item in databases),
            *(str(item.get("path") or "") for item in support_files),
        }
        if manifest.get("secret_env_included") is True:
            expected.add("config/.env.local")
        if set(members) != expected:
            raise RuntimeError("Backup archive members do not match manifest")
        for item in [*databases, *support_files]:
            member = str(item.get("path") or "")
            if not member or member.startswith("/") or ".." in Path(member).parts:
                raise RuntimeError(f"Backup manifest member is unsafe: {member}")
            info = archive.getinfo(member)
            if info.file_size != int(item.get("snapshot_bytes") or -1):
                raise RuntimeError(f"Backup member size mismatch: {member}")
            if _archive_member_sha256(archive, member) != str(
                item.get("sha256") or ""
            ).lower():
                raise RuntimeError(f"Backup member checksum mismatch: {member}")
    return {
        "path": str(path.resolve()),
        "archive_bytes": path.stat().st_size,
        "database_count": len(databases),
        "support_file_count": len(support_files),
        "critical_only": manifest.get("critical_only") is True,
        "include_secrets": manifest.get("include_secrets") is True,
    }


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


def _support_files(repo: Path) -> list[Path]:
    """Return a bounded, secret-free snapshot of code and audit contracts."""
    candidates: set[Path] = set()
    for relative in EXPLICIT_SUPPORT_FILES | RUNTIME_SUPPORT_FILES:
        candidate = repo / relative
        if candidate.is_file() and not candidate.is_symlink():
            candidates.add(candidate)
    for relative, suffixes in {
        **SUPPORT_SOURCE_RULES,
        **RUNTIME_SUPPORT_DIRECTORIES,
    }.items():
        base = repo / relative
        if not base.is_dir() or base.is_symlink():
            continue
        for candidate in base.rglob("*"):
            if (
                candidate.is_file()
                and not candidate.is_symlink()
                and candidate.suffix.lower() in suffixes
                and "__pycache__" not in candidate.parts
            ):
                candidates.add(candidate)

    output: list[Path] = []
    total = 0
    for candidate in sorted(candidates):
        lexical = candidate.absolute()
        relative = lexical.relative_to(repo)
        resolved = candidate.resolve()
        try:
            resolved.relative_to(repo)
        except ValueError as exc:
            if relative.as_posix() not in ALLOWED_JUNCTION_RUNTIME_FILES:
                raise RuntimeError(
                    f"Support file escapes repository: {candidate}"
                ) from exc
        size = resolved.stat().st_size
        if size > MAX_SUPPORT_FILE_BYTES:
            raise RuntimeError(f"Support file exceeds backup bound: {candidate}")
        total += size
        if total > MAX_SUPPORT_TOTAL_BYTES:
            raise RuntimeError("Support-file backup exceeds aggregate bound")
        output.append(lexical)
    return output


def create_backup(
    repo: Path,
    destination_dir: Path,
    *,
    include_high_volume: bool = False,
    include_secrets: bool = False,
    critical_only: bool = False,
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
    if critical_only:
        databases = [db for db in databases if db.name in CRITICAL_DATABASES]
    if not include_high_volume:
        databases = [db for db in databases if db.name not in HIGH_VOLUME_DATABASES]
    if not databases:
        raise RuntimeError("No SQLite databases found to back up")

    manifest: dict[str, object] = {
        "created_at": now.astimezone(timezone.utc).isoformat(),
        "format": "q15-local-backup-v2",
        "include_high_volume": include_high_volume,
        "include_secrets": include_secrets,
        "secret_env_included": bool(
            include_secrets and (repo / ".env.local").is_file()
        ),
        "critical_only": critical_only,
        "databases": [],
        "support_files": [],
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

            for source in _support_files(repo):
                relative = source.relative_to(repo)
                snapshot = snapshot_root / "support" / relative
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, snapshot)
                manifest["support_files"].append({
                    "path": (Path("support") / relative).as_posix(),
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
                for item in manifest["support_files"]:
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
                    "by default because they are rolling, reproducible collectors. "
                    "The support/ tree preserves the current source, tests, immutable "
                    "research contracts, watchdog state, and any V22 audit artifacts; "
                    "review those files before restoring them over a working checkout.\n",
                )
                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                )
        verify_backup_archive(temp_zip)
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
    parser.add_argument("--critical-only", action="store_true")
    args = parser.parse_args()
    path = create_backup(
        args.repo,
        args.destination,
        include_high_volume=args.include_high_volume,
        include_secrets=args.include_secrets,
        critical_only=args.critical_only,
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
