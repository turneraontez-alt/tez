"""Tests for the hourly learning-snapshot exporter (``tools/learning_export.py``).

Deterministic: no network, no live exchanges. The publish path pushes to a
*local bare repo* standing in for GitHub, so the full plumbing flow is exercised
for real without a remote.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tools import learning_export as lx


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _git(workdir: Path, *args: str, env: dict | None = None) -> str:
    res = subprocess.run(
        ["git", *args], cwd=workdir, capture_output=True, text=True, env=env
    )
    assert res.returncode == 0, f"git {args} failed: {res.stderr}"
    return res.stdout.strip()


def _make_v95_db(path: Path) -> None:
    """Create a real, schema-complete (empty) v95 ledger at ``path``."""
    from q15_upgrade import ledger_v95

    path.parent.mkdir(parents=True, exist_ok=True)
    ledger = ledger_v95.V95Ledger(path)
    # Touch a read method so the schema is materialised on disk.
    ledger.scoreboard()


def _point_ledgers_at(monkeypatch, data_dir: Path) -> None:
    """Make the configured ledger paths line up with a temp data dir so the
    real repo ``data/`` is never swept into a test snapshot."""
    monkeypatch.setenv("Q15_V95_LEDGER_DB", str(data_dir / "q15_v95_ledger_v1.sqlite3"))
    monkeypatch.setenv(
        "Q15_CHALLENGER_DB", str(data_dir / "q15_challenger_shadow_v1.sqlite3")
    )
    monkeypatch.setenv(
        "Q15_POLYMARKET_DB", str(data_dir / "q15_polymarket_shadow_v1.sqlite3")
    )
    monkeypatch.setenv("Q15_SPOT_DEPTH_DB", str(data_dir / "q15_spot_depth_v1.sqlite3"))


# --------------------------------------------------------------------------- #
# build_snapshot
# --------------------------------------------------------------------------- #
def test_build_snapshot_structure_and_gzip(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    _point_ledgers_at(monkeypatch, data_dir)
    _make_v95_db(data_dir / "q15_v95_ledger_v1.sqlite3")

    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    snap, artifacts = lx.build_snapshot(
        data_dir, now=now, head_commit="abc1234", build_info={"branch": "main"}
    )

    assert snap["schema_version"] == lx.SCHEMA_VERSION
    assert snap["generated_at"] == "2026-06-22T12:00:00+00:00"
    assert snap["git_commit"] == "abc1234"
    assert snap["build_info"] == {"branch": "main"}

    db_meta = snap["databases"]["q15_v95_ledger_v1.sqlite3"]
    assert "predictions" in db_meta["row_counts"]
    assert db_meta["row_counts"]["predictions"] == 0
    assert db_meta["artifact"] == "dbs/q15_v95_ledger_v1.sqlite3.gz"
    assert db_meta["gz_sha256"] == hashlib.sha256(
        artifacts["dbs/q15_v95_ledger_v1.sqlite3.gz"]
    ).hexdigest()

    # curated scoreboards attached to the right DB
    assert snap["scoreboards"]["v95"]["scoreboard"]["available"] is True
    assert "official_scoreboard" in snap["scoreboards"]["v95"]

    # gz artifact round-trips to a valid sqlite file
    raw = gzip.decompress(artifacts["dbs/q15_v95_ledger_v1.sqlite3.gz"])
    assert raw[:16] == b"SQLite format 3\x00"


def test_build_snapshot_handles_empty_data_dir(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _point_ledgers_at(monkeypatch, data_dir)  # configured paths don't exist either

    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    snap, artifacts = lx.build_snapshot(
        data_dir, now=now, head_commit=None, build_info=None
    )
    assert snap["databases"] == {}
    assert snap["scoreboards"] == {}
    assert artifacts == {}


def test_build_snapshot_does_not_mutate_source(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    _point_ledgers_at(monkeypatch, data_dir)
    src = data_dir / "q15_v95_ledger_v1.sqlite3"
    _make_v95_db(src)
    # Flush WAL into the main file so its bytes are stable for the comparison.
    flush = subprocess.run(
        ["python3", "-c",
         f"import sqlite3;c=sqlite3.connect({str(src)!r});"
         "c.execute('PRAGMA wal_checkpoint(TRUNCATE)');c.close()"],
        capture_output=True, text=True,
    )
    assert flush.returncode == 0, flush.stderr

    before = hashlib.sha256(src.read_bytes()).hexdigest()
    lx.build_snapshot(
        data_dir,
        now=datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc),
        head_commit=None,
        build_info=None,
    )
    after = hashlib.sha256(src.read_bytes()).hexdigest()
    assert before == after, "build_snapshot must not write to the live DB"


# --------------------------------------------------------------------------- #
# publish
# --------------------------------------------------------------------------- #
def _init_work_and_remote(tmp_path: Path) -> tuple[Path, Path]:
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "app.txt").write_text("live app\n")
    _git(work, "add", "app.txt")
    _git(work, "commit", "-q", "-m", "initial")

    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    return work, remote


def test_publish_pushes_orphan_and_leaves_worktree_untouched(tmp_path):
    work, remote = _init_work_and_remote(tmp_path)
    head_before = _git(work, "rev-parse", "HEAD")
    branch_before = _git(work, "rev-parse", "--abbrev-ref", "HEAD")

    gz = gzip.compress(b"SQLite format 3\x00 fake", mtime=0)
    snap = {"generated_at": "2026-06-22T12:00:00+00:00", "git_commit": "abc",
            "databases": {"db.sqlite3": {}}}
    ok, detail = lx.publish(
        {"dbs/db.sqlite3.gz": gz}, snap,
        remote_url=str(remote), branch="learning-snapshots", workdir=str(work),
    )
    assert ok, detail

    # working tree / HEAD / branch completely untouched
    assert _git(work, "rev-parse", "HEAD") == head_before
    assert _git(work, "rev-parse", "--abbrev-ref", "HEAD") == branch_before
    assert _git(work, "status", "--porcelain") == ""
    # no local snapshot branch ref was created
    local_branches = _git(work, "branch", "--list", "learning-snapshots")
    assert local_branches == ""

    # remote received exactly the expected tree
    names = _git(remote, "ls-tree", "-r", "--name-only", "learning-snapshots").split()
    assert set(names) == {"learning_snapshot.json", "README.md", "dbs/db.sqlite3.gz"}

    pushed_json = _git(remote, "show", "learning-snapshots:learning_snapshot.json")
    assert json.loads(pushed_json)["generated_at"] == "2026-06-22T12:00:00+00:00"

    pushed_gz = subprocess.run(
        ["git", "cat-file", "blob", "learning-snapshots:dbs/db.sqlite3.gz"],
        cwd=remote, capture_output=True,
    ).stdout
    assert gzip.decompress(pushed_gz) == b"SQLite format 3\x00 fake"


def test_publish_force_push_keeps_single_commit(tmp_path):
    work, remote = _init_work_and_remote(tmp_path)
    snap1 = {"generated_at": "2026-06-22T12:00:00+00:00", "databases": {}}
    snap2 = {"generated_at": "2026-06-22T13:00:00+00:00", "databases": {}}

    ok1, d1 = lx.publish({}, snap1, remote_url=str(remote),
                         branch="learning-snapshots", workdir=str(work))
    ok2, d2 = lx.publish({}, snap2, remote_url=str(remote),
                         branch="learning-snapshots", workdir=str(work))
    assert ok1 and ok2, (d1, d2)

    # rolling orphan commit -> the branch never accumulates history
    count = _git(remote, "rev-list", "--count", "learning-snapshots")
    assert count == "1"
    latest = _git(remote, "show", "learning-snapshots:learning_snapshot.json")
    assert json.loads(latest)["generated_at"] == "2026-06-22T13:00:00+00:00"


@pytest.mark.parametrize("branch", ["main", "master", "HEAD", ""])
def test_publish_refuses_protected_branch(tmp_path, branch):
    with pytest.raises(ValueError, match="protected branch"):
        lx.publish({}, {"generated_at": "x"}, remote_url="unused",
                   branch=branch, workdir=str(tmp_path))


# --------------------------------------------------------------------------- #
# auth / safety
# --------------------------------------------------------------------------- #
def test_mask_hides_token():
    masked = lx._mask("https://x-access-token:SECRET@github.com/x.git", "SECRET")
    assert "SECRET" not in masked
    assert masked == "https://x-access-token:***@github.com/x.git"
    assert lx._mask("no token here", None) == "no token here"


def test_main_without_token_does_not_run(monkeypatch):
    monkeypatch.delenv("GH_PUSH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert lx.main() == 1


def test_url_embeds_token():
    assert lx._url("owner/repo", "Tk") == "https://x-access-token:Tk@github.com/owner/repo.git"


def test_scoreboards_build_when_launched_as_script(tmp_path, monkeypatch):
    """Regression: when run as ``python3 tools/learning_export.py`` the repo root
    is NOT on sys.path, so the lazy ``from q15_upgrade import ...`` used by the
    scoreboard builders would fail and scoreboards would come back as errors.
    The module must put the repo root on the path itself. Exercised in a
    subprocess whose cwd is OUTSIDE the repo (so only the module's own fix can
    make q15_upgrade importable)."""
    data_dir = tmp_path / "data"
    _point_ledgers_at(monkeypatch, data_dir)
    _make_v95_db(data_dir / "q15_v95_ledger_v1.sqlite3")

    module_path = Path(lx.__file__).resolve()
    driver = (
        "import importlib.util, json, sys\n"
        f"spec = importlib.util.spec_from_file_location('lx', {str(module_path)!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "from datetime import datetime, timezone\n"
        f"snap, _ = m.build_snapshot({str(data_dir)!r}, "
        "now=datetime(2026,6,22,tzinfo=timezone.utc), head_commit=None, build_info=None)\n"
        "print(json.dumps(snap['scoreboards']))\n"
    )
    # cwd OUTSIDE the repo; PYTHONPATH cleared so nothing else leaks the repo in.
    env = {k: v for k, v in __import__("os").environ.items() if k != "PYTHONPATH"}
    res = subprocess.run(
        [sys.executable, "-c", driver], cwd=tmp_path, capture_output=True,
        text=True, env=env,
    )
    assert res.returncode == 0, res.stderr
    scoreboards = json.loads(res.stdout.strip().splitlines()[-1])
    assert "error" not in scoreboards.get("v95", {}), scoreboards
    assert scoreboards["v95"]["scoreboard"]["available"] is True

