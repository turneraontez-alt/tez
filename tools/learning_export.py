#!/usr/bin/env python3
"""Learning-snapshot export: publish the live learning records to GitHub.

Runs as a long-lived background workflow on the Repl (sibling to the GitHub
relay). Once an hour it:

  1. Takes a *consistent* read-only snapshot of every SQLite learning ledger
     under ``data/`` (via the SQLite online-backup API — never a torn file copy
     and never a write to the live DB).
  2. Builds a curated ``learning_snapshot.json`` (the v95 + challenger
     scoreboards, official W/L, the 5-feature shadow A/B, timing experiment,
     flip performance, and raw per-table row counts) plus gzipped copies of the
     raw DBs, so a review session can either eyeball the JSON or run arbitrary
     matched-row SQL against the real ledgers.
  3. Force-pushes the bundle to a DEDICATED branch (``learning-snapshots`` by
     default) so a session can ``git fetch origin learning-snapshots`` and audit
     the system against real data instead of an empty seed DB.

Why a dedicated branch, not ``main``:
- The GitHub Relay two-way-syncs ONLY ``main`` every ~20s. A file the Repl
  regenerates and commits to ``main`` is exactly what forced ``health_snapshot``
  to be gitignored ("tracking it makes the GitHub Relay conflict on every sync
  and stall deploys"). A side branch is invisible to the relay, so this can
  never stall a deploy or churn ``main``'s history.

Design / safety notes (mirrors ``tools/github_relay.py``):
- The GitHub token is read from the environment at run time and injected into
  the push URL in-memory only. It is never written to .git/config, never
  committed, and is masked in all log output.
- The branch is built entirely with git plumbing (hash-object / a TEMP index /
  write-tree / commit-tree) and pushed by raw commit SHA. It NEVER checks out
  the branch, touches HEAD, the working tree, or the app's real index — so it is
  safe to run inside the same checkout as the live app.
- Each cycle publishes a single orphan commit (``--force``), so the branch never
  accumulates history / binary bloat; a fetch always gets just the latest state.
- Refuses to ever target ``main`` / ``master`` / ``HEAD``.
- Reads the live DBs read-only; all heavy aggregation runs on the throwaway
  backup copy, so a scoreboard method that migrates schema can't touch the live
  ledger.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# sqlite3 is only needed for the backup/read paths; import lazily-friendly here.
import sqlite3

REPO = os.environ.get("LEARNING_EXPORT_REPO") or os.environ.get(
    "GITHUB_RELAY_REPO", "turneraontez-alt/tez"
)
BRANCH = os.environ.get("LEARNING_EXPORT_BRANCH", "learning-snapshots")
INTERVAL = max(60, int(os.environ.get("LEARNING_EXPORT_INTERVAL", "3600")))
WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# When launched as `python3 tools/learning_export.py`, Python puts the script's
# dir (tools/) on sys.path[0] — NOT the repo root — so the lazy
# `from q15_upgrade import ...` used by the scoreboard builders can't resolve.
# Put the repo root on the path explicitly so curated scoreboards build no
# matter how the worker is launched.
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)
DATA_DIR = os.environ.get("LEARNING_EXPORT_DATA_DIR") or os.environ.get(
    "Q15_DATA_DIR", "data"
)

PROTECTED_BRANCHES = {"main", "master", "HEAD", ""}
IDENT_NAME = "learning-export"
IDENT_EMAIL = "learning-export@users.noreply.github.com"
SCHEMA_VERSION = 1

# Canonical ledger locations, so curated scoreboards are attached to the right
# DB even if an operator points a ledger at a non-default path.
_LEDGER_ENV_DEFAULTS = {
    "v95": ("Q15_V95_LEDGER_DB", "data/q15_v95_ledger_v1.sqlite3"),
    "challenger": ("Q15_CHALLENGER_DB", "data/q15_challenger_shadow_v1.sqlite3"),
    "polymarket": ("Q15_POLYMARKET_DB", "data/q15_polymarket_shadow_v1.sqlite3"),
    "spot_depth": ("Q15_SPOT_DEPTH_DB", "data/q15_spot_depth_v1.sqlite3"),
    "strategy_bots": ("Q15_STRATEGY_BOTS_DB", "data/q15_strategy_bots_v3.sqlite3"),
}


class _Result:
    """Lightweight, str-decoded view of a completed git subprocess."""

    __slots__ = ("returncode", "stdout", "stderr")

    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _log(msg: str) -> None:
    print(f"[learning-export] {msg}", flush=True)


def _token() -> str | None:
    return os.environ.get("GH_PUSH_TOKEN") or os.environ.get("GITHUB_TOKEN")


def _mask(text: str, token: str | None) -> str:
    if token and token in text:
        text = text.replace(token, "***")
    return text


def _git_executable() -> str:
    configured = os.environ.get("GIT_EXE") or os.environ.get("Q15_LOCAL_GIT")
    if configured:
        configured_path = Path(configured)
        if configured_path.is_file():
            return str(configured_path)

    found = shutil.which("git")
    if found:
        return found

    bundled = (
        Path.home()
        / ".cache"
        / "codex-runtimes"
        / "codex-primary-runtime"
        / "dependencies"
        / "native"
        / "git"
        / "cmd"
        / "git.exe"
    )
    if bundled.is_file():
        return str(bundled)
    return "git"


def _git(
    args: list[str],
    *,
    input_bytes: bytes | None = None,
    env: dict[str, str] | None = None,
    workdir: str | None = None,
) -> _Result:
    """Run git in binary mode (binary stdin for blobs) and decode for callers."""
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [_git_executable(), *args],
        cwd=workdir or WORKDIR,
        input=input_bytes,
        capture_output=True,
        env=env,
    )
    return _Result(
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


def _url(repo: str, token: str) -> str:
    return f"https://x-access-token:{token}@github.com/{repo}.git"


def _api_request(
    method: str,
    path: str,
    token: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, str]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "tez-learning-export",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        "https://api.github.com" + path,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        return 0, str(exc)


def _head_commit(workdir: str) -> str | None:
    try:
        res = _git(["rev-parse", "--short", "HEAD"], workdir=workdir)
    except (FileNotFoundError, OSError):
        return None
    return res.stdout.strip() if res.returncode == 0 else None


def _read_build_info(workdir: str) -> dict[str, Any] | None:
    path = Path(workdir) / "build_info.json"
    live_head = _head_commit(workdir)
    if not path.is_file():
        if not live_head:
            return None
        return {
            "commit": live_head,
            "running_commit": live_head,
            "matches_checkout": True,
            "build_info_stale": False,
        }
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _log(f"build_info.json unreadable: {type(exc).__name__}: {exc}")
        if not live_head:
            return None
        return {
            "commit": live_head,
            "running_commit": live_head,
            "matches_checkout": True,
            "build_info_stale": False,
        }
    if not isinstance(info, dict):
        return None
    stamped_commit = info.get("commit")
    stale = bool(live_head and stamped_commit and stamped_commit != live_head)
    info["running_commit"] = live_head
    info["matches_checkout"] = bool(live_head and live_head == stamped_commit)
    info["build_info_stale"] = stale
    info["stale_build_info_commit"] = stamped_commit if stale else None
    return info


def _ledger_basename(role: str) -> str:
    env, default = _LEDGER_ENV_DEFAULTS[role]
    return Path(os.environ.get(env) or default).name


def _configured_db_paths() -> list[Path]:
    return [
        Path(os.environ[env])
        for env, default in _LEDGER_ENV_DEFAULTS.values()
        if os.environ.get(env)
    ]


def _db_files(data_dir: str | os.PathLike[str]) -> list[Path]:
    """Every learning DB to export: ``data/*.sqlite3`` plus any explicitly
    configured ledger path that lives outside ``data_dir``. De-duped by real
    path, returned in a stable order."""
    seen: dict[Path, Path] = {}
    base = Path(data_dir)
    if base.is_dir():
        for path in sorted(base.glob("*.sqlite3")):
            seen.setdefault(path.resolve(), path)
    for path in _configured_db_paths():
        if path.is_file():
            seen.setdefault(path.resolve(), path)
    return [seen[key] for key in sorted(seen, key=lambda p: p.name)]


def _backup_db(src: Path) -> Path | None:
    """Consistent read-only snapshot of a live SQLite DB via the online-backup
    API. Returns a temp-file path the caller must delete, or None on failure."""
    if not src.is_file():
        return None
    fd, tmp = tempfile.mkstemp(prefix="lexport_", suffix=".sqlite3")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30.0)
        try:
            dest = sqlite3.connect(tmp_path)
            try:
                source.backup(dest)
            finally:
                dest.close()
        finally:
            source.close()
        return tmp_path
    except sqlite3.Error as exc:
        _log(f"backup failed for {src.name}: {type(exc).__name__}: {exc}")
        _safe_unlink(tmp_path)
        return None


def _safe_unlink(path: Path | None) -> None:
    if path is None:
        return
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        for attempt in range(6):
            try:
                candidate.unlink()
                break
            except FileNotFoundError:
                break
            except OSError as exc:
                if attempt < 5:
                    time.sleep(0.2)
                    continue
                _log(f"could not remove temp {candidate.name}: {type(exc).__name__}: {exc}")


def _db_row_counts(db_path: Path) -> dict[str, int]:
    """Row count for every user table, read-only."""
    counts: dict[str, int] = {}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for table in tables:
            # Names come from sqlite_master (trusted catalog), not user input;
            # double-quote-wrap so unusual-but-valid identifiers still parse.
            counts[table] = conn.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0]
    finally:
        conn.close()
    return counts


def _guard(label: str, fn: Callable[[], Any]) -> Any:
    """Run a scoreboard method, capturing a failure into the report instead of
    aborting the whole snapshot. Deliberate diagnostic boundary: logs + records
    the error and recovers."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - boundary: record + log + continue
        _log(f"scoreboard '{label}' failed: {type(exc).__name__}: {exc}")
        return {"error": f"{type(exc).__name__}: {exc}"}


def _v95_scoreboards(backup_path: Path) -> dict[str, Any]:
    from q15_upgrade import ledger_v95

    ledger = ledger_v95.V95Ledger(backup_path)
    try:
        return {
            "scoreboard": _guard("v95.scoreboard", ledger.scoreboard),
            "official_scoreboard": _guard(
                "v95.official_scoreboard", ledger.official_scoreboard
            ),
            "shadow_signal_experiment": _guard(
                "v95.shadow_signal_experiment", ledger.shadow_signal_experiment
            ),
            "timing_experiment_scoreboard": _guard(
                "v95.timing_experiment_scoreboard", ledger.timing_experiment_scoreboard
            ),
            "flip_warning_performance": _guard(
                "v95.flip_warning_performance", ledger.flip_warning_performance
            ),
        }
    finally:
        connection = getattr(ledger, "_shared_connection", None)
        if connection is not None:
            sqlite3.Connection.close(connection)
            ledger._shared_connection = None


def _challenger_scoreboards(backup_path: Path) -> dict[str, Any]:
    from q15_upgrade.challenger.ledger import ShadowLedger

    ledger = ShadowLedger(str(backup_path))
    try:
        return {"scoreboard": _guard("challenger.scoreboard", ledger.scoreboard)}
    finally:
        ledger.close()


def _strategy_bot_scoreboards(backup_path: Path) -> dict[str, Any]:
    from q15_upgrade.strategy_bots.ledger import StrategyBotLedger
    from q15_upgrade.strategy_bots.rules import STRATEGY_VERSION

    ledger = StrategyBotLedger(str(backup_path))
    try:
        return {"scoreboard": _guard(
            "strategy_bots.scoreboard",
            lambda: ledger.scoreboard(STRATEGY_VERSION),
        )}
    finally:
        ledger.close()


def build_snapshot(
    data_dir: str | os.PathLike[str],
    *,
    now: datetime,
    head_commit: str | None,
    build_info: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Return ``(snapshot_json, artifacts)``.

    ``artifacts`` maps a repo-relative path (e.g. ``dbs/foo.sqlite3.gz``) to the
    bytes to publish. Pure with respect to the live DBs: it only ever reads them
    via the online-backup API.
    """
    artifacts: dict[str, bytes] = {}
    databases: dict[str, Any] = {}
    scoreboards: dict[str, Any] = {}

    v95_name = _ledger_basename("v95")
    challenger_name = _ledger_basename("challenger")
    strategy_bots_name = _ledger_basename("strategy_bots")

    for src in _db_files(data_dir):
        backup = _backup_db(src)
        if backup is None:
            databases[src.name] = {"error": "backup_failed"}
            continue
        try:
            raw = backup.read_bytes()
            gz = gzip.compress(raw, mtime=0)  # mtime=0 -> stable bytes for clean diffs
            rel = f"dbs/{src.name}.gz"
            artifacts[rel] = gz
            databases[src.name] = {
                "artifact": rel,
                "db_bytes": len(raw),
                "gz_bytes": len(gz),
                "gz_sha256": hashlib.sha256(gz).hexdigest(),
                "row_counts": _db_row_counts(backup),
            }
            # Guard the whole scoreboard build (incl. import/constructor) so a
            # scoreboard failure degrades to an error note but still ships the
            # raw gz DB + row counts — the most valuable artifacts for an audit.
            if src.name == v95_name:
                scoreboards["v95"] = _guard("v95", lambda b=backup: _v95_scoreboards(b))
            elif src.name == challenger_name:
                scoreboards["challenger"] = _guard(
                    "challenger", lambda b=backup: _challenger_scoreboards(b)
                )
            elif src.name == strategy_bots_name:
                scoreboards["strategy_bots"] = _guard(
                    "strategy_bots", lambda b=backup: _strategy_bot_scoreboards(b)
                )
        finally:
            _safe_unlink(backup)

    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.astimezone(timezone.utc).isoformat(),
        "git_commit": head_commit,
        "build_info": build_info,
        "data_dir": str(data_dir),
        "databases": databases,
        "scoreboards": scoreboards,
    }
    return snapshot, artifacts


def _readme_bytes(snapshot: dict[str, Any]) -> bytes:
    dbs = ", ".join(sorted(snapshot.get("databases", {}))) or "(none)"
    text = (
        "# Learning snapshots (auto-generated, do not edit)\n\n"
        "This branch is force-pushed hourly by `tools/learning_export.py` on the\n"
        "Repl. It is NOT part of `main` and is invisible to the GitHub Relay.\n\n"
        f"- Generated at: {snapshot.get('generated_at')}\n"
        f"- From commit: {snapshot.get('git_commit')}\n"
        f"- Databases: {dbs}\n\n"
        "## Consume\n\n"
        "```bash\n"
        "git fetch origin learning-snapshots\n"
        "git show origin/learning-snapshots:learning_snapshot.json | less\n"
        "# raw ledgers for matched-row SQL:\n"
        "mkdir -p /tmp/ledgers\n"
        "git show origin/learning-snapshots:dbs/q15_v95_ledger_v1.sqlite3.gz \\\n"
        "  | gunzip > /tmp/ledgers/q15_v95_ledger_v1.sqlite3\n"
        "```\n"
    )
    return text.encode("utf-8")


def _snapshot_files(artifacts: dict[str, bytes], snapshot: dict[str, Any]) -> dict[str, bytes]:
    files: dict[str, bytes] = dict(artifacts)
    files["learning_snapshot.json"] = (
        json.dumps(snapshot, indent=2, sort_keys=True, default=str).encode("utf-8")
        + b"\n"
    )
    files["README.md"] = _readme_bytes(snapshot)
    return files


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _can_publish_with_git(workdir: str) -> bool:
    return shutil.which("git") is not None and (Path(workdir) / ".git").exists()


def publish(
    artifacts: dict[str, bytes],
    snapshot: dict[str, Any],
    *,
    remote_url: str,
    branch: str,
    workdir: str,
    token: str | None = None,
    git: Callable[..., _Result] = _git,
) -> tuple[bool, str]:
    """Force-push the snapshot as a single orphan commit on ``branch``.

    Built entirely with plumbing against a TEMP index, so HEAD / the working
    tree / the real index are never touched. Returns ``(ok, masked_detail)``.
    """
    if branch in PROTECTED_BRANCHES:
        raise ValueError(f"refusing to publish snapshots to protected branch {branch!r}")

    files = _snapshot_files(artifacts, snapshot)

    with tempfile.TemporaryDirectory(prefix="lexport_idx_") as tmpdir:
        env = {**os.environ, "GIT_INDEX_FILE": os.path.join(tmpdir, "index")}

        for relpath, data in sorted(files.items()):
            res = git(["hash-object", "-w", "--stdin"], input_bytes=data, workdir=workdir)
            if res.returncode != 0:
                return False, _mask(
                    f"hash-object failed for {relpath}: {res.stderr.strip()}", token
                )
            blob = res.stdout.strip()
            res = git(
                ["update-index", "--add", "--cacheinfo", f"100644,{blob},{relpath}"],
                env=env,
                workdir=workdir,
            )
            if res.returncode != 0:
                return False, _mask(
                    f"update-index failed for {relpath}: {res.stderr.strip()}", token
                )

        res = git(["write-tree"], env=env, workdir=workdir)
        if res.returncode != 0:
            return False, _mask("write-tree failed: " + res.stderr.strip(), token)
        tree = res.stdout.strip()

        message = f"learning snapshot {snapshot.get('generated_at')}"
        res = git(
            [
                "-c", f"user.name={IDENT_NAME}",
                "-c", f"user.email={IDENT_EMAIL}",
                "commit-tree", tree, "-m", message,
            ],
            env=env,
            workdir=workdir,
        )
        if res.returncode != 0:
            return False, _mask("commit-tree failed: " + res.stderr.strip(), token)
        commit = res.stdout.strip()

        res = git(
            [
                "-c", "credential.helper=",
                "push", "--force", remote_url, f"{commit}:refs/heads/{branch}",
            ],
            workdir=workdir,
        )
        ok = res.returncode == 0
        return ok, _mask((res.stdout + res.stderr).strip(), token)


def _api_failure(action: str, status: int, body: str, token: str) -> str:
    detail = body.strip() or "(empty response)"
    if len(detail) > 1000:
        detail = detail[:1000] + "..."
    return _mask(f"{action} failed via GitHub API ({status}): {detail}", token)


def publish_via_github_api(
    artifacts: dict[str, bytes],
    snapshot: dict[str, Any],
    *,
    repo: str,
    branch: str,
    token: str,
    api_request: Callable[
        [str, str, str, dict[str, Any] | None],
        tuple[int, str],
    ] = _api_request,
) -> tuple[bool, str]:
    """Publish a snapshot without requiring a local git checkout.

    Replit deployments may have the app data and GitHub token but not the
    original ``.git`` directory. The Git Data API gives us the same orphan
    commit + forced branch update semantics as the local plumbing path.
    """
    if branch in PROTECTED_BRANCHES:
        raise ValueError(f"refusing to publish snapshots to protected branch {branch!r}")

    repo_path = urllib.parse.quote(repo.strip("/"), safe="/")
    branch_path = urllib.parse.quote(branch, safe="/")
    tree: list[dict[str, str]] = []

    for relpath, data in sorted(_snapshot_files(artifacts, snapshot).items()):
        payload = {
            "content": base64.b64encode(data).decode("ascii"),
            "encoding": "base64",
        }
        status, body = api_request("POST", f"/repos/{repo_path}/git/blobs", token, payload)
        if status not in {200, 201}:
            return False, _api_failure(f"create blob {relpath}", status, body, token)
        try:
            blob_sha = json.loads(body)["sha"]
        except (KeyError, json.JSONDecodeError) as exc:
            return False, _api_failure(
                f"parse blob {relpath} response ({type(exc).__name__})",
                status,
                body,
                token,
            )
        tree.append({"path": relpath, "mode": "100644", "type": "blob", "sha": blob_sha})

    status, body = api_request("POST", f"/repos/{repo_path}/git/trees", token, {"tree": tree})
    if status not in {200, 201}:
        return False, _api_failure("create tree", status, body, token)
    try:
        tree_sha = json.loads(body)["sha"]
    except (KeyError, json.JSONDecodeError) as exc:
        return False, _api_failure(f"parse tree response ({type(exc).__name__})", status, body, token)

    message = f"learning snapshot {snapshot.get('generated_at')}"
    status, body = api_request(
        "POST",
        f"/repos/{repo_path}/git/commits",
        token,
        {"message": message, "tree": tree_sha},
    )
    if status not in {200, 201}:
        return False, _api_failure("create commit", status, body, token)
    try:
        commit_sha = json.loads(body)["sha"]
    except (KeyError, json.JSONDecodeError) as exc:
        return False, _api_failure(
            f"parse commit response ({type(exc).__name__})",
            status,
            body,
            token,
        )

    status, body = api_request(
        "PATCH",
        f"/repos/{repo_path}/git/refs/heads/{branch_path}",
        token,
        {"sha": commit_sha, "force": True},
    )
    if status == 404:
        status, body = api_request(
            "POST",
            f"/repos/{repo_path}/git/refs",
            token,
            {"ref": f"refs/heads/{branch}", "sha": commit_sha},
        )
    if status not in {200, 201}:
        return False, _api_failure(f"update branch {branch}", status, body, token)

    return True, f"github api published {commit_sha[:8]} to {branch}"


def run_once(
    *,
    data_dir: str,
    repo: str,
    branch: str,
    token: str,
    workdir: str,
    now: datetime | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    snapshot, artifacts = build_snapshot(
        data_dir,
        now=now,
        head_commit=_head_commit(workdir),
        build_info=_read_build_info(workdir),
    )
    force_api = _truthy(os.environ.get("LEARNING_EXPORT_USE_GITHUB_API"))
    use_api = force_api or not _can_publish_with_git(workdir)
    if use_api:
        ok, detail = publish_via_github_api(
            artifacts,
            snapshot,
            repo=repo,
            branch=branch,
            token=token,
        )
    else:
        try:
            ok, detail = publish(
                artifacts,
                snapshot,
                remote_url=_url(repo, token),
                branch=branch,
                workdir=workdir,
                token=token,
            )
        except (FileNotFoundError, OSError):
            ok, detail = publish_via_github_api(
                artifacts,
                snapshot,
                repo=repo,
                branch=branch,
                token=token,
            )
    return ok, detail, snapshot


def main() -> int:
    token = _token()
    if not token:
        _log("No GH_PUSH_TOKEN / GITHUB_TOKEN in environment; learning export cannot run.")
        return 1
    if BRANCH in PROTECTED_BRANCHES:
        _log(f"refusing to run: target branch '{BRANCH}' is protected.")
        return 2

    _log(
        f"Hourly learning export -> github.com/{REPO} branch '{BRANCH}' "
        f"every {INTERVAL}s (data dir: {DATA_DIR})"
    )
    while True:
        try:
            ok, detail, snapshot = run_once(
                data_dir=DATA_DIR,
                repo=REPO,
                branch=BRANCH,
                token=token,
                workdir=WORKDIR,
            )
            if ok:
                ndb = len(snapshot.get("databases", {}))
                _log(f"published snapshot {snapshot.get('generated_at')} ({ndb} db(s))")
            else:
                _log("publish failed (will retry next cycle): " + detail)
        except Exception as exc:  # noqa: BLE001 - process boundary: log + recover
            _log(f"unexpected error: {_mask(str(exc), token)}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
