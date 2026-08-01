"""Durable, design-bound output helpers for RTI research artifacts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping


BINDING_FILENAME = "design-binding.json"
BINDING_VERSION = "q15-rti-design-bound-output-v1"


def _validated_identity(design_id: Any, design_sha256: Any) -> tuple[str, str]:
    identity = str(design_id or "")
    fingerprint = str(design_sha256 or "")
    if not identity:
        raise ValueError("output_design_id_missing")
    if len(fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in fingerprint
    ):
        raise ValueError("output_design_sha256_invalid")
    return identity, fingerprint


def _read_mapping(path: Path) -> Mapping[str, Any]:
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"output_json_invalid:{path.name}") from exc
    if not isinstance(decoded, Mapping):
        raise ValueError(f"output_json_not_object:{path.name}")
    return decoded


def _assert_evidence_matches(
    directory: Path, design_id: str, design_sha256: str,
) -> None:
    for path in sorted(directory.glob("*.json")):
        if path.name == BINDING_FILENAME:
            continue
        payload = _read_mapping(path)
        evidence_sha = payload.get("design_sha256")
        if evidence_sha is None:
            raise ValueError(f"output_json_design_sha_missing:{path.name}")
        if evidence_sha != design_sha256:
            raise ValueError(f"output_design_sha_mismatch:{path.name}")
        evidence_id = payload.get("design_id")
        if evidence_id is not None and evidence_id != design_id:
            raise ValueError(f"output_design_id_mismatch:{path.name}")


def bind_design_output_directory(
    directory: Path,
    *,
    design_id: Any,
    design_sha256: Any,
) -> Mapping[str, Any]:
    """Exclusively bind a report directory to one immutable design.

    Legacy report directories are accepted only when every existing JSON file
    already carries the requested design hash.  Once the binding exists, a
    different design fails before it can reserve a test score or overwrite a
    report.
    """
    identity, fingerprint = _validated_identity(design_id, design_sha256)
    directory.mkdir(parents=True, exist_ok=True)
    expected = {
        "binding_version": BINDING_VERSION,
        "design_id": identity,
        "design_sha256": fingerprint,
    }
    binding_path = directory / BINDING_FILENAME
    if binding_path.exists():
        if dict(_read_mapping(binding_path)) != expected:
            raise ValueError("output_directory_design_binding_mismatch")
        _assert_evidence_matches(directory, identity, fingerprint)
        return expected

    _assert_evidence_matches(directory, identity, fingerprint)
    try:
        with binding_path.open("x", encoding="utf-8") as handle:
            json.dump(expected, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if dict(_read_mapping(binding_path)) != expected:
            raise ValueError("output_directory_design_binding_mismatch")
    _assert_evidence_matches(directory, identity, fingerprint)
    return expected


def atomic_write_text(path: Path, text: str) -> None:
    """Replace a report atomically after its complete contents reach disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(dict(payload), indent=2, sort_keys=True) + "\n",
    )
