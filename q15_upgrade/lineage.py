"""Small, dependency-free helpers for immutable local feature lineage.

The runtime may replay a durable source capture after a restart.  Callers should
therefore persist both the source lineage stamped at capture time and the
decision lineage stamped when a downstream policy evaluates that capture.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Mapping


def _clean(value: Any) -> Any:
    """Return a deterministic, JSON-safe value without exposing secrets."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _clean(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, (set, frozenset)):
        values = [_clean(item) for item in value]
        return sorted(values, key=lambda item: json.dumps(item, sort_keys=True, default=str))
    return str(value)


def stable_config_hash(values: Mapping[str, Any], *, length: int = 16) -> str:
    """Hash an allow-listed configuration mapping, never the entire environment."""
    payload = json.dumps(_clean(values), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[: max(8, int(length))]


@lru_cache(maxsize=1)
def build_sha() -> str:
    """Return an exact local build identity, including a dirty-tree fingerprint."""
    explicit = str(os.environ.get("Q15_BUILD_SHA") or "").strip()
    if explicit:
        return explicit[:64]
    try:
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
        value = completed.stdout.strip()
        if completed.returncode == 0 and value:
            status = subprocess.run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                cwd=str(root),
                check=False,
                capture_output=True,
                timeout=2.0,
            )
            relevant_lines: list[str] = []
            untracked: list[Path] = []
            for raw_line in status.stdout.decode("utf-8", errors="replace").splitlines():
                path_text = raw_line[3:].strip().strip('"') if len(raw_line) >= 4 else ""
                normalized = path_text.replace("\\", "/")
                suffix = Path(normalized).suffix.lower()
                relevant = (
                    suffix in {".py", ".json", ".ps1", ".md", ".toml", ".yaml", ".yml"}
                    or Path(normalized).name in {".env.example", ".env.local.example", "requirements.txt"}
                )
                if not relevant or normalized.startswith(("outputs/", "data/", "work/")):
                    continue
                relevant_lines.append(raw_line)
                if raw_line.startswith("?? "):
                    untracked.append(root / normalized)
            if relevant_lines:
                digest = hashlib.sha256("\n".join(relevant_lines).encode("utf-8"))
                diff = subprocess.run(
                    ["git", "diff", "--no-ext-diff", "--binary", "HEAD"],
                    cwd=str(root),
                    check=False,
                    capture_output=True,
                    timeout=2.0,
                )
                digest.update(diff.stdout)
                for path in sorted(untracked, key=lambda item: str(item)):
                    try:
                        digest.update(str(path.relative_to(root)).encode("utf-8"))
                        digest.update(path.read_bytes())
                    except OSError:
                        continue
                return f"{value[:12]}-dirty-{digest.hexdigest()[:12]}"
            return value[:64]
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def lineage_stamp(
    *,
    feature_schema_version: str,
    config: Mapping[str, Any],
) -> dict[str, str]:
    return {
        "build_sha": build_sha(),
        "config_hash": stable_config_hash(config),
        "feature_schema_version": str(feature_schema_version),
    }
