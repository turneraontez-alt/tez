"""Repo-root ``.env`` / ``.env.local`` loading for the tt_edge CLIs.

The Q15 app inherits its secrets from however the operator launches it; a
tt_edge job started in a fresh terminal would not. Every job CLI calls
:func:`bootstrap_env` first, which loads ``.env.local`` then ``.env`` from
the repo root with SETDEFAULT semantics — a variable already exported in
the real environment always wins, and ``.env.local`` (loaded first) wins
over ``.env``. Values are never logged: these files hold tokens.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import MutableMapping

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_env_file(path: Path,
                  environ: MutableMapping[str, str] = os.environ) -> int:
    """Load KEY=VALUE lines (``export`` prefix and surrounding quotes are
    tolerated; blank lines and ``#`` comments skipped). Existing keys are
    never overwritten. Returns how many keys were newly set."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    loaded = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].lstrip()
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in environ:
            environ[key] = value
            loaded += 1
    return loaded


def bootstrap_env(environ: MutableMapping[str, str] = os.environ) -> int:
    """Load ``.env.local`` then ``.env`` from the repo root (first hit per
    key wins; the process environment always wins over both).

    NO-OP UNDER PYTEST when operating on the real process environment. Importing
    or exercising any tt_edge job used to splice the owner's LIVE ``.env.local``
    into ``os.environ`` for the rest of the session — roughly 24 ``Q15_ULTOIM_V2_*``
    values among them — so every Q15 test that ran afterwards was silently
    evaluated against production config instead of the defaults it asserts. That
    is invisible when the two suites run apart and breaks 31 tests when they run
    together. Tests that want the loader call ``load_env_file`` with an explicit
    mapping, which is unaffected; passing a non-``os.environ`` mapping here also
    still works, so only the global-mutation path is suppressed.
    """
    if environ is os.environ and "PYTEST_CURRENT_TEST" in os.environ:
        return 0
    loaded = 0
    for name in (".env.local", ".env"):
        loaded += load_env_file(REPO_ROOT / name, environ)
    return loaded
