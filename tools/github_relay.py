#!/usr/bin/env python3
"""GitHub relay: two-way sync between local `main` and GitHub.

Runs as a long-lived background workflow. Every cycle it:
  1. Fetches the GitHub branch.
  2. Pulls GitHub changes DOWN into the local project (fast-forward when the
     project is simply behind; a merge commit when both sides changed).
  3. Pushes local changes UP to GitHub.

So anything committed here flows to GitHub, and anything pushed on GitHub flows
back into the project automatically.

Design / safety notes:
- The GitHub token is read from the environment at run time and injected into
  the fetch/push URL in-memory only. It is never written to .git/config, never
  committed, and is masked in all log output.
- No git config / remote writes are performed (explicit authenticated URL +
  `-c credential.helper=`), so this does not trip the main-agent destructive-git
  guard.
- Never force-pushes. If both sides changed the SAME file, the merge would
  conflict; in that case the relay aborts the merge cleanly (project left
  untouched) and logs it, instead of guessing.
- Operates on the explicit local branch ref `refs/heads/main`, never a stray
  checked-out HEAD.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

REPO = os.environ.get("GITHUB_RELAY_REPO", "turneraontez-alt/tez")
BRANCH = os.environ.get("GITHUB_RELAY_BRANCH", "main")
INTERVAL = max(5, int(os.environ.get("GITHUB_RELAY_INTERVAL", "20")))
WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MERGE_IDENTITY = [
    "-c", "user.name=github-relay",
    "-c", "user.email=github-relay@users.noreply.github.com",
]

# Files where GitHub (codex) is the source of truth: if the ONLY merge conflict
# is in one of these, auto-resolve by taking GitHub's version ('-X theirs').
# .replit churns constantly because Replit's auto-checkpoint commits rewrite it
# locally while codex pushes config edits to the same lines.
AUTO_THEIRS_FILES = {".replit"}


def _token() -> str | None:
    return os.environ.get("GH_PUSH_TOKEN") or os.environ.get("GITHUB_TOKEN")


def _mask(text: str, token: str | None) -> str:
    if token and token in text:
        text = text.replace(token, "***")
    return text


def _log(msg: str) -> None:
    print(f"[relay] {msg}", flush=True)


# Durable status surface. The relay used to report a failed push ONLY by printing
# to its own stdout log, so an expired GH_PUSH_TOKEN looked exactly like healthy
# operation from anywhere else — the deploy path stopped working and nothing
# said so. Write the outcome of every cycle where /api/health can read it, and
# count consecutive failures so "briefly flaky" is distinguishable from "broken
# since Tuesday". Best-effort: status writing never affects relay behaviour.
STATUS_PATH = os.environ.get("GITHUB_RELAY_STATUS_PATH") or os.path.join(
    WORKDIR, "work", "local-run", "relay_status.json")

_status = {
    "last_push_ok_at": None,
    "last_push_error": None,
    "last_push_error_at": None,
    "consecutive_push_failures": 0,
    "local": None,
    "remote": None,
    "updated_at": None,
}


def _write_status(**fields) -> None:
    """Merge ``fields`` into the durable status file (never raises)."""
    import json

    _status.update(fields)
    _status["updated_at"] = time.time()
    try:
        os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
        tmp = STATUS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_status, fh)
        os.replace(tmp, STATUS_PATH)   # atomic; a reader never sees a partial file
    except (OSError, ValueError, TypeError):
        # Deliberate best-effort boundary: observability must never be able to
        # disturb the relay loop. ValueError/TypeError cover an unusable path
        # (e.g. an embedded null) which os.makedirs raises rather than OSError.
        pass


def _git(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=WORKDIR, capture_output=True, text=True)


def _rev(ref: str) -> str | None:
    res = _git(["rev-parse", "--verify", ref])
    return res.stdout.strip() if res.returncode == 0 else None


def _url(token: str) -> str:
    return f"https://x-access-token:{token}@github.com/{REPO}.git"


def _fetch(token: str) -> tuple[bool, str]:
    res = _git(["-c", "credential.helper=", "fetch", _url(token), BRANCH])
    return res.returncode == 0, _mask((res.stdout + res.stderr).strip(), token)


def _merge_remote(token: str) -> tuple[str, str]:
    """Integrate FETCH_HEAD into local. Returns (status, detail).

    status: 'ok' (merged / fast-forwarded / already up to date),
            'conflict' (aborted, project untouched),
            'error' (aborted, project untouched).

    When the ONLY conflicting paths are auto-resolvable config files
    (AUTO_THEIRS_FILES, e.g. .replit), GitHub is treated as the source of
    truth and the merge is retried with '-X theirs'. This clears the recurring
    churn between Replit's auto-checkpoint commits and codex's config pushes,
    while still hard-pausing on any real code conflict.
    """
    m = _git([*MERGE_IDENTITY, "merge", "--no-edit", "FETCH_HEAD"])
    out = _mask((m.stdout + m.stderr).strip(), token)
    if m.returncode == 0:
        return "ok", out
    conflicts = [
        c for c in _git(["diff", "--name-only", "--diff-filter=U"]).stdout.strip().splitlines() if c
    ]
    if conflicts and all(c in AUTO_THEIRS_FILES for c in conflicts):
        ab = _git(["merge", "--abort"])
        if ab.returncode != 0:
            return "error", "merge --abort failed before auto-resolve:\n" + _mask(
                (ab.stdout + ab.stderr).strip(), token
            )
        r = _git([*MERGE_IDENTITY, "merge", "--no-edit", "-X", "theirs", "FETCH_HEAD"])
        rout = _mask((r.stdout + r.stderr).strip(), token)
        if r.returncode == 0:
            return "ok", "auto-resolved (GitHub wins) " + ", ".join(conflicts) + "\n" + rout
        leftover = _git(["diff", "--name-only", "--diff-filter=U"]).stdout.strip()
        _git(["merge", "--abort"])
        if leftover:
            return "conflict", "auto-resolve failed; same file changed on both sides:\n" + leftover
        return "error", "auto-resolve merge failed:\n" + rout
    _git(["merge", "--abort"])
    if conflicts:
        return "conflict", "Same file changed on both sides:\n" + "\n".join(conflicts)
    return "error", out


def _push(token: str) -> tuple[bool, str]:
    res = _git([
        "-c", "credential.helper=",
        "push", _url(token), f"refs/heads/{BRANCH}:refs/heads/{BRANCH}",
    ])
    return res.returncode == 0, _mask((res.stdout + res.stderr).strip(), token)


def main() -> int:
    token = _token()
    if not token:
        _log("No GH_PUSH_TOKEN / GITHUB_TOKEN in environment; relay cannot run.")
        return 1

    _log(f"Two-way sync local '{BRANCH}' <-> github.com/{REPO} every {INTERVAL}s")
    conflict_logged: str | None = None  # remote sha we last warned a conflict for

    while True:
        try:
            local = _rev(f"refs/heads/{BRANCH}")
            if not local:
                _log(f"local branch '{BRANCH}' not found; waiting.")
                time.sleep(INTERVAL)
                continue

            ok, out = _fetch(token)
            if not ok:
                _log("fetch failed (will retry): " + out)
                time.sleep(INTERVAL)
                continue

            remote = _rev("FETCH_HEAD")

            # --- pull GitHub changes DOWN into the project -------------------
            if remote and remote != local:
                status, detail = _merge_remote(token)
                if status == "conflict":
                    if conflict_logged != remote:
                        _log(
                            "PAUSED: GitHub and the project changed the same file, "
                            "so I left the project untouched. Resolve it and I'll "
                            "resume. " + detail
                        )
                        conflict_logged = remote
                    time.sleep(INTERVAL)
                    continue
                if status == "error":
                    _log("merge skipped (will retry): " + detail)
                    time.sleep(INTERVAL)
                    continue
                conflict_logged = None
                new_local = _rev(f"refs/heads/{BRANCH}")
                if new_local and new_local != local:
                    _log(f"pulled GitHub changes -> {new_local[:8]}")
                    local = new_local

            # --- push local changes UP to GitHub ----------------------------
            if remote != local:
                pok, pout = _push(token)
                if pok:
                    _log(f"pushed {local[:8]}")
                    _write_status(last_push_ok_at=time.time(), last_push_error=None,
                                  consecutive_push_failures=0, local=local, remote=remote)
                elif "non-fast-forward" in pout.lower() or "fetch first" in pout.lower():
                    # Remote moved between fetch and push; next cycle reconciles.
                    pass
                else:
                    _log(f"push failed for {local[:8]}: {pout}")
                    fails = _status.get("consecutive_push_failures") or 0
                    _write_status(last_push_error=_mask(pout, token)[:400],
                                  last_push_error_at=time.time(),
                                  consecutive_push_failures=fails + 1,
                                  local=local, remote=remote)
            else:
                # Nothing to push: local and remote agree, which is the healthy
                # steady state. Clear any stale failure so a recovered relay does
                # not keep reporting an old error forever.
                if _status.get("consecutive_push_failures"):
                    _write_status(consecutive_push_failures=0, last_push_error=None,
                                  local=local, remote=remote)
        except Exception as exc:  # never let the relay die on a transient error
            _log(f"unexpected error: {_mask(str(exc), token)}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
