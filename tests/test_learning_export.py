"""Tests for the hourly learning-snapshot exporter (``tools/learning_export.py``).

Deterministic: no network, no live exchanges. The publish path pushes to a
*local bare repo* standing in for GitHub, so the full plumbing flow is exercised
for real without a remote.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from tools import learning_export as lx


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _git(workdir: Path, *args: str, env: dict | None = None) -> str:
    res = subprocess.run(
        [lx._git_executable(), *args],
        cwd=workdir,
        capture_output=True,
        text=True,
        env=env,
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
    monkeypatch.setenv("Q15_STRATEGY_BOTS_DB", str(data_dir / "q15_strategy_bots_v3.sqlite3"))
    monkeypatch.setenv(
        "Q15_FEED_SETTLE_INDEX_DB", str(data_dir / "q15_settlement_index_v1.sqlite3")
    )
    monkeypatch.setenv(
        "Q15_FEED_LADDER_DB", str(data_dir / "q15_ladder_probe_v1.sqlite3")
    )
    monkeypatch.setenv(
        "Q15_FEED_MARKET_ACTIVITY_DB", str(data_dir / "q15_market_activity_v1.sqlite3")
    )
    monkeypatch.setenv(
        "Q15_FEED_PATH_RECORDER_DB", str(data_dir / "q15_path_recorder_v1.sqlite3")
    )
    monkeypatch.setenv("Q15_FEED_LIQ_DB", str(data_dir / "q15_liq_feed_v1.sqlite3"))
    monkeypatch.setenv(
        "Q15_STRANGLE_SHADOW_DB", str(data_dir / "q15_strangle_shadow_v1.sqlite3")
    )


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


def test_build_snapshot_exports_new_collector_dbs(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _point_ledgers_at(monkeypatch, data_dir)
    monkeypatch.setenv("LEARNING_EXPORT_RAW_DB_EXCLUDE_NAMES", "")

    specs = {
        "q15_settlement_index_v1.sqlite3": "settlement_index_ticks",
        "q15_ladder_probe_v1.sqlite3": "ladder_captures",
        "q15_market_activity_v1.sqlite3": "market_activity_samples",
        "q15_path_recorder_v1.sqlite3": "window_paths",
        "q15_liq_feed_v1.sqlite3": "liquidation_events",
        "q15_strangle_shadow_v1.sqlite3": "strangle_windows",
    }
    for filename, table in specs.items():
        conn = sqlite3.connect(data_dir / filename)
        try:
            conn.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, payload TEXT)')
            conn.execute(f'INSERT INTO "{table}" (payload) VALUES (?)', ("ok",))
            conn.commit()
        finally:
            conn.close()

    snap, artifacts = lx.build_snapshot(
        data_dir,
        now=datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc),
        head_commit=None,
        build_info=None,
    )

    for filename, table in specs.items():
        meta = snap["databases"][filename]
        assert meta["row_counts"][table] == 1
        assert meta["artifact"] == f"dbs/{filename}.gz"
        assert f"dbs/{filename}.gz" in artifacts


def test_build_snapshot_skips_oversized_raw_db_artifact(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _point_ledgers_at(monkeypatch, data_dir)
    monkeypatch.setenv("LEARNING_EXPORT_RAW_DB_EXCLUDE_NAMES", "")
    monkeypatch.setenv("LEARNING_EXPORT_MAX_ARTIFACT_BYTES", "200")

    db = data_dir / "q15_coinbase_adv_l2_v1.sqlite3"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE payloads (payload BLOB)")
        conn.execute("INSERT INTO payloads VALUES (?)", (os.urandom(8192),))
        conn.commit()
    finally:
        conn.close()

    snap, artifacts = lx.build_snapshot(
        data_dir,
        now=datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc),
        head_commit=None,
        build_info=None,
    )

    meta = snap["databases"]["q15_coinbase_adv_l2_v1.sqlite3"]
    assert meta["artifact"] is None
    assert meta["artifact_skipped"] is True
    assert meta["artifact_skipped_reason"] == "gz_bytes_exceed_limit"
    assert meta["artifact_max_bytes"] == 200
    assert meta["gz_bytes"] > 200
    assert meta["row_counts"]["payloads"] == 1
    assert "dbs/q15_coinbase_adv_l2_v1.sqlite3.gz" not in artifacts


def test_build_snapshot_excludes_high_volume_raw_db_without_backup_or_gzip(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _point_ledgers_at(monkeypatch, data_dir)

    db = data_dir / "q15_coinbase_adv_l2_v1.sqlite3"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE payloads (payload BLOB)")
        conn.execute("INSERT INTO payloads VALUES (?)", (b"small",))
        conn.commit()
    finally:
        conn.close()

    with patch.object(lx, "_backup_db", side_effect=AssertionError("must not copy excluded DB")):
        snap, artifacts = lx.build_snapshot(
            data_dir,
            now=datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc),
            head_commit=None,
            build_info=None,
        )

    meta = snap["databases"]["q15_coinbase_adv_l2_v1.sqlite3"]
    assert meta["artifact"] is None
    assert meta["artifact_skipped"] is True
    assert meta["artifact_skipped_reason"] == "raw_artifact_excluded"
    assert meta["gz_bytes"] is None
    assert meta["gz_sha256"] is None
    assert meta["row_counts"]["payloads"] == 1
    assert artifacts == {}


def test_build_snapshot_streams_raw_database_compression(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _point_ledgers_at(monkeypatch, data_dir)
    monkeypatch.setenv("LEARNING_EXPORT_RAW_DB_EXCLUDE_NAMES", "")

    db = data_dir / "ordinary.sqlite3"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE payloads (payload BLOB)")
        conn.execute("INSERT INTO payloads VALUES (?)", (os.urandom(1024 * 1024),))
        conn.commit()
    finally:
        conn.close()

    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(path):
        if path.name.startswith("lexport_") and path.suffix == ".sqlite3":
            raise AssertionError("raw SQLite backup must be streamed")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    snap, artifacts = lx.build_snapshot(
        data_dir,
        now=datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc),
        head_commit=None,
        build_info=None,
    )

    rel = "dbs/ordinary.sqlite3.gz"
    assert snap["databases"]["ordinary.sqlite3"]["artifact"] == rel
    assert gzip.decompress(artifacts[rel])[:16] == b"SQLite format 3\x00"


def test_default_raw_exclusions_include_settlement_index():
    assert "q15_settlement_index_v1.sqlite3" in lx._raw_artifact_exclude_names()
    assert "q15_strategy_bots_v3.sqlite3" in lx._raw_artifact_exclude_names()


def test_strategy_export_does_not_materialize_frozen_json(tmp_path, monkeypatch):
    from q15_upgrade.strategy_bots.ledger import StrategyBotLedger
    from q15_upgrade.strategy_bots.rules import confidence_tier_decision

    data_dir = tmp_path / "data"
    _point_ledgers_at(monkeypatch, data_dir)
    db = data_dir / "q15_strategy_bots_v3.sqlite3"
    ledger = StrategyBotLedger(db)
    row = {
        "created_at": 1000.0,
        "model_version": "ultoim-v2",
        "asset": "BTC",
        "ticker": "KXBTC-1",
        "interval": "10M",
        "window_key": 10,
        "predicted_side": "YES",
        "entry_ask_cents": 80.0,
        "spread_cents": 2.0,
        "delivery_status": "MUTED",
        "record_kind": "DELIVERED_CANDIDATE",
        "reason_codes": "TEST",
    }
    decision = confidence_tier_decision(row, source_system="ultoim_v2")
    assert ledger.record_decision(
        decision, row, source_system="ultoim_v2",
    ) is not None
    ledger.close()
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE strategy_bot_decisions SET threshold_json=?",
            (json.dumps({"large": "x" * (2 * 1024 * 1024)}),),
        )
        conn.commit()
    finally:
        conn.close()

    with patch.object(
        StrategyBotLedger,
        "scoreboard",
        side_effect=AssertionError("full scoreboard must not run in exporter"),
    ):
        result = lx._strategy_bot_scoreboards(db)["scoreboard"]

    assert result["export_mode"] == "MEMORY_BOUNDED_COMPACT_SQL_V1"
    assert result["by_tier"]["A"]["rows"] == 1


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
        [sys.executable, "-c",
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
    object_files_before = {
        path.relative_to(work / ".git" / "objects")
        for path in (work / ".git" / "objects").rglob("*")
        if path.is_file()
    }

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
    object_files_after = {
        path.relative_to(work / ".git" / "objects")
        for path in (work / ".git" / "objects").rglob("*")
        if path.is_file()
    }
    assert object_files_after == object_files_before, (
        "snapshot publishing must not add blobs to the live Git object database"
    )

    # remote received exactly the expected tree
    names = _git(remote, "ls-tree", "-r", "--name-only", "learning-snapshots").split()
    assert set(names) == {"learning_snapshot.json", "README.md", "dbs/db.sqlite3.gz"}

    pushed_json = _git(remote, "show", "learning-snapshots:learning_snapshot.json")
    assert json.loads(pushed_json)["generated_at"] == "2026-06-22T12:00:00+00:00"

    pushed_gz = subprocess.run(
        [lx._git_executable(), "cat-file", "blob", "learning-snapshots:dbs/db.sqlite3.gz"],
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


@pytest.mark.parametrize("detail", [
    "remote: Invalid username or token.",
    "fatal: Authentication failed for repo",
    '{"message":"Bad credentials"}',
])
def test_auth_failures_are_terminal(detail):
    assert lx._is_auth_failure(detail) is True


def test_non_auth_publish_failure_is_retryable():
    assert lx._is_auth_failure("temporary network timeout") is False


def test_git_executable_prefers_configured_git(tmp_path, monkeypatch):
    fake_git = tmp_path / "git.exe"
    fake_git.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_EXE", str(fake_git))
    monkeypatch.setenv("Q15_LOCAL_GIT", str(tmp_path / "other-git.exe"))

    assert lx._git_executable() == str(fake_git)


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


def test_strategy_bot_tier_scoreboard_exports(tmp_path, monkeypatch):
    from q15_upgrade.strategy_bots.ledger import StrategyBotLedger
    from q15_upgrade.strategy_bots.rules import confidence_tier_decision

    data_dir = tmp_path / "data"
    _point_ledgers_at(monkeypatch, data_dir)
    db = data_dir / "q15_strategy_bots_v3.sqlite3"
    ledger = StrategyBotLedger(db)
    row = {
        "created_at": 1000.0,
        "model_version": "ultoim-v2",
        "asset": "BTC",
        "ticker": "KXBTC-1",
        "interval": "10M",
        "window_key": 10,
        "predicted_side": "YES",
        "entry_ask_cents": 80.0,
        "spread_cents": 2.0,
        "delivery_status": "MUTED",
        "record_kind": "DELIVERED_CANDIDATE",
        "reason_codes": "TEST",
    }
    decision = confidence_tier_decision(row, source_system="ultoim_v2")
    assert ledger.record_decision(decision, row, source_system="ultoim_v2") is not None
    ledger.close()

    snap, _ = lx.build_snapshot(
        data_dir,
        now=datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc),
        head_commit="abc1234",
        build_info=None,
    )

    strategy = snap["scoreboards"]["strategy_bots"]["scoreboard"]
    assert strategy["by_tier"]["A"]["rows"] == 1
    assert "data_coverage" in strategy

