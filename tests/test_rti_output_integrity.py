from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import q15_rti_output_integrity as integrity


DESIGN_A = "q15-design-a"
SHA_A = "a" * 64
DESIGN_B = "q15-design-b"
SHA_B = "b" * 64


def _json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_legacy_output_is_bound_only_when_every_json_matches(tmp_path: Path):
    output = tmp_path / "audit"
    output.mkdir()
    _json(output / "audit.json", {
        "design_id": DESIGN_A,
        "design_sha256": SHA_A,
    })
    binding = integrity.bind_design_output_directory(
        output, design_id=DESIGN_A, design_sha256=SHA_A,
    )
    assert binding["design_id"] == DESIGN_A
    assert json.loads(
        (output / integrity.BINDING_FILENAME).read_text(encoding="utf-8")
    ) == binding

    with pytest.raises(
        ValueError, match="output_directory_design_binding_mismatch",
    ):
        integrity.bind_design_output_directory(
            output, design_id=DESIGN_B, design_sha256=SHA_B,
        )


def test_cross_design_legacy_report_cannot_claim_directory(tmp_path: Path):
    output = tmp_path / "freeze"
    output.mkdir()
    _json(output / "btc-report.json", {
        "design_id": DESIGN_A,
        "design_sha256": SHA_A,
    })
    with pytest.raises(ValueError, match="output_design_sha_mismatch"):
        integrity.bind_design_output_directory(
            output, design_id=DESIGN_B, design_sha256=SHA_B,
        )
    assert not (output / integrity.BINDING_FILENAME).exists()


def test_unbound_json_fails_closed(tmp_path: Path):
    output = tmp_path / "mixed"
    output.mkdir()
    _json(output / "mystery.json", {"status": "looks-valid"})
    with pytest.raises(ValueError, match="output_json_design_sha_missing"):
        integrity.bind_design_output_directory(
            output, design_id=DESIGN_A, design_sha256=SHA_A,
        )


def test_atomic_write_preserves_old_report_and_cleans_temp_on_failure(
    tmp_path: Path, monkeypatch,
):
    target = tmp_path / "audit.json"
    target.write_text("old-complete-report", encoding="utf-8")

    def fail_replace(source, destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(integrity.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        integrity.atomic_write_text(target, "new-complete-report")
    assert target.read_text(encoding="utf-8") == "old-complete-report"
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_json_write_emits_complete_parseable_object(tmp_path: Path):
    target = tmp_path / "report.json"
    integrity.atomic_write_json(target, {
        "design_id": DESIGN_A,
        "design_sha256": SHA_A,
        "rows": 27,
    })
    assert json.loads(target.read_text(encoding="utf-8"))["rows"] == 27
    assert list(tmp_path.glob("*.tmp")) == []
