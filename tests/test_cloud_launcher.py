from __future__ import annotations

from pathlib import Path

import pytest

from tools.cloud_launcher import load_environment, read_env_file


def test_cloud_launcher_uses_local_env_rules_and_last_file_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXISTING", "kept")
    local = tmp_path / "q15.env"
    local.write_text(
        "# comment\n"
        "PORT = 8000\n"
        "QUOTED=\"value with spaces\"\n"
        "PRIVATE_KEY='line1\\nline2'\n"
        "Q15_EXEC_KILL=false\n",
        encoding="utf-8",
    )
    safety = tmp_path / "safety.env"
    safety.write_text("Q15_EXEC_KILL=true\n", encoding="utf-8")

    environment = load_environment([local, safety])

    assert environment["EXISTING"] == "kept"
    assert environment["PORT"] == "8000"
    assert environment["QUOTED"] == "value with spaces"
    assert environment["PRIVATE_KEY"] == "line1\\nline2"
    assert environment["Q15_EXEC_KILL"] == "true"


def test_cloud_launcher_rejects_invalid_keys(tmp_path: Path) -> None:
    path = tmp_path / "bad.env"
    path.write_text("NOT-A-KEY=value\n", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid environment key"):
        read_env_file(path)
