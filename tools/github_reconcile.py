#!/usr/bin/env python3
"""One-shot reconcile: merge GitHub `main` into local, then push.

Run once (as a workflow process, where git writes are permitted) to fold any
GitHub-only commits into the local history without losing local work, then push
the merged result. Safe by design: on a merge conflict it aborts cleanly and
reports the conflicting files instead of leaving the repo half-merged.

The token is read from the environment, used inline, and masked in all output.
"""
from __future__ import annotations

import os
import subprocess
import sys

REPO = os.environ.get("GITHUB_RELAY_REPO", "turneraontez-alt/tez")
BRANCH = os.environ.get("GITHUB_RELAY_BRANCH", "main")
WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _token() -> str | None:
    return os.environ.get("GH_PUSH_TOKEN") or os.environ.get("GITHUB_TOKEN")


def _mask(text: str, token: str | None) -> str:
    return text.replace(token, "***") if token and token in text else text


def _git(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=WORKDIR, capture_output=True, text=True)


def _log(msg: str) -> None:
    print(f"[reconcile] {msg}", flush=True)


def main() -> int:
    token = _token()
    if not token:
        _log("No GH token in environment; cannot reconcile.")
        return 1
    url = f"https://x-access-token:{token}@github.com/{REPO}.git"

    current = _git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    if current != BRANCH:
        _log(f"Refusing to reconcile: checked-out branch is '{current}', not '{BRANCH}'.")
        return 1

    _log(f"Fetching github.com/{REPO} {BRANCH} ...")
    f = _git(["-c", "credential.helper=", "fetch", url, BRANCH])
    if f.returncode != 0:
        _log("fetch failed: " + _mask((f.stdout + f.stderr).strip(), token))
        return 1

    _log("Merging remote into local (no-edit) ...")
    m = _git([
        "-c", "user.name=github-relay",
        "-c", "user.email=github-relay@users.noreply.github.com",
        "merge", "--no-edit", "FETCH_HEAD",
    ])
    out = _mask((m.stdout + m.stderr).strip(), token)
    if m.returncode != 0:
        conflicts = _git(["diff", "--name-only", "--diff-filter=U"]).stdout.strip()
        _git(["merge", "--abort"])
        _log("MERGE CONFLICT — aborted, repo unchanged. Conflicting files:\n" + conflicts)
        _log("Detail: " + out)
        return 2
    _log("merge ok: " + (out or "(fast-forward / up to date)"))

    _log("Pushing merged history to GitHub ...")
    p = _git(["-c", "credential.helper=", "push", url, f"HEAD:refs/heads/{BRANCH}"])
    pout = _mask((p.stdout + p.stderr).strip(), token)
    if p.returncode != 0:
        _log("push failed: " + pout)
        return 1
    head = _git(["rev-parse", "HEAD"]).stdout.strip()[:8]
    _log(f"DONE — GitHub {REPO}@{BRANCH} now in sync at {head}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
