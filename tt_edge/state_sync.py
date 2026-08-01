"""Persist the TT-Edge SQLite state across ephemeral cloud sessions via an
orphan git branch (default ``tt-edge-state``).

Cloud containers are wiped between sessions; the claims/bankroll ledger
must not be. Mirroring the repo's ``learning-snapshots`` pattern: the DB is
gzipped and force-pushed as a SINGLE orphan commit (no history bloat), and
restored at the start of the next cycle. The branch is invisible to the
GitHub relay (which syncs only main) and ignored by the branch pruner
(which deletes only ``origin/claude/*``).

Git plumbing (hash-object -> mktree -> commit-tree -> push) — no checkout,
no index pollution, safe to run inside the working repo.
"""
from __future__ import annotations

import gzip
import logging
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_BRANCH = "tt-edge-state"
_STATE_FILE = "tt_edge.sqlite3.gz"
_GIT_TIMEOUT_S = 120


def _git(repo_root: Path, *args: str, input_bytes: bytes | None = None
         ) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo_root, input=input_bytes,
                          capture_output=True, timeout=_GIT_TIMEOUT_S)


def restore_state(db_path: Path, repo_root: Path,
                  branch: str = DEFAULT_BRANCH) -> bool:
    """Fetch the state branch and unpack the DB. False (not an error) when
    the branch doesn't exist yet — first cycle ever — or the DB already
    exists locally (a warm container's file wins over the snapshot)."""
    if db_path.exists():
        logger.info("state: local DB present at %s; not restoring", db_path)
        return False
    fetch = _git(repo_root, "fetch", "origin", branch)
    if fetch.returncode != 0:
        logger.info("state: no %s branch yet (first cycle?)", branch)
        return False
    show = _git(repo_root, "show", f"origin/{branch}:{_STATE_FILE}")
    if show.returncode != 0:
        logger.warning("state: branch %s exists but lacks %s", branch,
                       _STATE_FILE)
        return False
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(gzip.decompress(show.stdout))
    logger.info("state: restored %s from origin/%s (%d bytes)", db_path,
                branch, db_path.stat().st_size)
    return True


def push_state(db_path: Path, repo_root: Path,
               branch: str = DEFAULT_BRANCH, *, label: str = "") -> bool:
    """Gzip the DB and force-push it as a single orphan commit. Never
    raises — a failed push loses one cycle's durability, not the cycle."""
    if not db_path.exists():
        logger.warning("state: nothing to push; %s missing", db_path)
        return False
    try:
        with tempfile.NamedTemporaryFile(suffix=".gz") as handle:
            handle.write(gzip.compress(db_path.read_bytes()))
            handle.flush()
            blob = _git(repo_root, "hash-object", "-w", handle.name)
        if blob.returncode != 0:
            raise RuntimeError(blob.stderr.decode(errors="replace"))
        blob_sha = blob.stdout.decode().strip()
        tree = _git(repo_root, "mktree",
                    input_bytes=f"100644 blob {blob_sha}\t{_STATE_FILE}\n"
                    .encode())
        if tree.returncode != 0:
            raise RuntimeError(tree.stderr.decode(errors="replace"))
        tree_sha = tree.stdout.decode().strip()
        commit = _git(repo_root, "-c", "user.name=tt-edge",
                      "-c", "user.email=tt-edge@localhost", "commit-tree",
                      tree_sha, "-m", f"tt-edge state {label}".strip())
        if commit.returncode != 0:
            raise RuntimeError(commit.stderr.decode(errors="replace"))
        commit_sha = commit.stdout.decode().strip()
        push = _git(repo_root, "push", "-f", "origin",
                    f"{commit_sha}:refs/heads/{branch}")
        if push.returncode != 0:
            raise RuntimeError(push.stderr.decode(errors="replace"))
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        logger.error("state: push to %s failed: %s", branch, exc)
        return False
    logger.info("state: pushed %s -> origin/%s", db_path, branch)
    return True
