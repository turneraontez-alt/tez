"""Tests for tools/config_audit.py — the env-var inventory / coverage gate.

Groups:
* real-repo scan finds a known var (Q15_ULTOIM_V2_ENABLED) with its default;
* a synthetic module exercises every detection pattern (os.environ.get,
  os.getenv, environ subscript, helper idiom, known non-Q15 names), the
  directory exclusions, and unparseable-file warnings;
* the --check logic (documented passes, baselined passes, novel fails) on
  synthetic inputs only — the repo is never mutated;
* the real repo currently passes --check (the committed baseline is fresh).
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = REPO_ROOT / "tools" / "config_audit.py"

_spec = importlib.util.spec_from_file_location("config_audit", TOOL_PATH)
config_audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(config_audit)


@pytest.fixture(scope="module")
def repo_scan():
    variables, warnings = config_audit.scan_repo(REPO_ROOT)
    return variables, warnings


# ── (a) known var in the real repo ────────────────────────────────────────────


def test_scanner_finds_known_var_with_default(repo_scan):
    variables, _ = repo_scan
    assert "Q15_ULTOIM_V2_ENABLED" in variables
    entry = variables["Q15_ULTOIM_V2_ENABLED"]
    assert entry["default"] == "False"
    assert any(
        read.startswith("q15_upgrade/ultoim_v2/config.py:") for read in entry["reads"]
    )


def test_repo_scan_is_substantial_and_clean(repo_scan):
    variables, warnings = repo_scan
    # The repo reads hundreds of Q15_* vars; a collapse here means the scanner broke.
    assert len(variables) > 500
    assert warnings == []


# ── (b) synthetic module: every detection pattern ─────────────────────────────


SYNTHETIC = '''\
import os
from os import environ, getenv

A = os.environ.get("Q15_SYN_ALPHA", "abc")
B = os.getenv("Q15_SYN_BETA")
C = getenv("Q15_SYN_GAMMA", 7)
D = _bool("Q15_SYN_DELTA", True)
E = _csv_set("Q15_SYN_EPSILON", "BTC,ETH")
F = _int("SOME_RANDOM_NAME", 3)          # non-Q15, not a known name: ignored
G = _str("TELEGRAM_BOT_TOKEN", "")       # known non-Q15 name: found
H = os.environ["Q15_SYN_SUB"]            # subscript read
os.environ["Q15_SYN_WRITE"] = "x"        # write, not a read: ignored
I = environ.get("Q15_SYN_ZETA", default=1.5)
J = os.environ.get(A)                    # dynamic name: ignored
'''


@pytest.fixture()
def synthetic_root(tmp_path):
    (tmp_path / "mod.py").write_text(SYNTHETIC, encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "skip.py").write_text(
        'import os\nX = os.getenv("Q15_SYN_EXCLUDED")\n', encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "skip2.py").write_text(
        'import os\nX = os.getenv("Q15_SYN_EXCLUDED_TOO")\n', encoding="utf-8"
    )
    (tmp_path / "broken.py").write_text("def (\n", encoding="utf-8")
    return tmp_path


def test_synthetic_scan_finds_all_patterns(synthetic_root):
    variables, warnings = config_audit.scan_repo(synthetic_root)

    assert variables["Q15_SYN_ALPHA"]["default"] == "'abc'"
    assert variables["Q15_SYN_ALPHA"]["reads"] == ["mod.py:4"]
    assert variables["Q15_SYN_BETA"]["default"] is None
    assert variables["Q15_SYN_GAMMA"]["default"] == "7"
    assert variables["Q15_SYN_DELTA"]["default"] == "True"
    assert variables["Q15_SYN_EPSILON"]["default"] == "'BTC,ETH'"
    assert variables["TELEGRAM_BOT_TOKEN"]["default"] == "''"
    assert "Q15_SYN_SUB" in variables  # environ[...] Load
    assert variables["Q15_SYN_ZETA"]["default"] == "1.5"  # default= keyword

    assert "SOME_RANDOM_NAME" not in variables  # heuristic rejects unknown non-Q15
    assert "Q15_SYN_WRITE" not in variables  # subscript Store is not a read
    assert "Q15_SYN_EXCLUDED" not in variables  # data/ pruned
    assert "Q15_SYN_EXCLUDED_TOO" not in variables  # tests/ pruned

    assert len(warnings) == 1 and warnings[0].startswith("broken.py:")


# ── (c) --check logic on synthetic inputs ─────────────────────────────────────


def test_check_logic_documented_baselined_novel(tmp_path):
    documented = config_audit.load_documented_names(
        _write(tmp_path / ".env.example", "# Q15_DOCUMENTED=true  (commented counts)\n")
    )
    baseline = config_audit.load_baseline(
        _write(
            tmp_path / "config_baseline.json",
            json.dumps({"undocumented": ["Q15_BASELINED"]}),
        )
    )
    names = {"Q15_DOCUMENTED", "Q15_BASELINED", "Q15_NOVEL"}

    assert config_audit.compute_undocumented(names, documented, baseline) == ["Q15_NOVEL"]
    # Each accepted path individually:
    assert config_audit.compute_undocumented({"Q15_DOCUMENTED"}, documented, baseline) == []
    assert config_audit.compute_undocumented({"Q15_BASELINED"}, documented, baseline) == []


def test_documented_match_is_whole_token(tmp_path):
    documented = config_audit.load_documented_names(
        _write(tmp_path / ".env.example", "Q15_FOO_BAR=1\n")
    )
    assert "Q15_FOO_BAR" in documented
    assert "Q15_FOO" not in documented  # substring of a documented var != documented


def test_check_end_to_end_on_synthetic_root(tmp_path, capsys):
    (tmp_path / "mod.py").write_text(
        'import os\nA = os.getenv("Q15_SYN_NOVEL")\nB = os.getenv("Q15_SYN_KNOWN")\n',
        encoding="utf-8",
    )
    (tmp_path / ".env.example").write_text("# Q15_SYN_KNOWN=1\n", encoding="utf-8")
    (tmp_path / "tools").mkdir()

    rc = config_audit.main(["--root", str(tmp_path), "--check"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "Q15_SYN_NOVEL" in out
    assert "Q15_SYN_KNOWN" not in out.replace("Q15_SYN_KNOWN=1", "")

    # Baselining the debt makes --check pass without touching .env.example.
    assert config_audit.main(["--root", str(tmp_path), "--write-baseline"]) == 0
    baseline = json.loads(
        (tmp_path / "tools" / "config_baseline.json").read_text(encoding="utf-8")
    )
    assert baseline == {"undocumented": ["Q15_SYN_NOVEL"]}
    capsys.readouterr()
    assert config_audit.main(["--root", str(tmp_path), "--check"]) == 0


# ── (d) the real repo passes --check today ────────────────────────────────────


def test_real_repo_passes_check():
    result = subprocess.run(
        [sys.executable, str(TOOL_PATH), "--check"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_committed_baseline_is_sorted_unique():
    baseline = json.loads(
        (REPO_ROOT / "tools" / "config_baseline.json").read_text(encoding="utf-8")
    )
    names = baseline["undocumented"]
    assert names == sorted(names)
    assert len(names) == len(set(names))


# ── helpers ───────────────────────────────────────────────────────────────────


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path
