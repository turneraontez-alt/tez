"""Shared pytest fixtures.

Ultoim Build defaults ON in production (a read-only research collector), but a
research overlay must never make the suite nondeterministic or write a stray DB
/ spawn a worker during unrelated ``run_cycle`` tests. This autouse fixture
disables it for every test by default; tests that specifically exercise Ultoim
construct their own runner with a temp DB, and the default-value test overrides
this with ``monkeypatch.delenv`` to verify the real production default.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _ultoim_off_in_tests(monkeypatch):
    monkeypatch.setenv("Q15_ULTOIM_ENABLED", "false")
    try:
        from q15_upgrade.ultoim import config as _cfg
        _cfg.reset_enabled_cache()
    except Exception:  # pragma: no cover - package may be absent in a partial env
        pass
    yield
    try:
        from q15_upgrade.ultoim import config as _cfg
        _cfg.reset_enabled_cache()
    except Exception:  # pragma: no cover
        pass
