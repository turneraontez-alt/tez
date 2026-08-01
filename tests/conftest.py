"""Shared pytest fixtures.

Ultoim Build defaults ON in production (a read-only research collector), but a
research overlay must never make the suite nondeterministic or write a stray DB
/ spawn a worker during unrelated ``run_cycle`` tests. This autouse fixture
disables it for every test by default; tests that specifically exercise Ultoim
construct their own runner with a temp DB, and the default-value test overrides
this with ``monkeypatch.delenv`` to verify the real production default.
"""
from __future__ import annotations

import os
import tempfile

import pytest


_TEMPORARY_DIRECTORY = tempfile.TemporaryDirectory


@pytest.fixture(autouse=True)
def _tt_edge_autoscan_off_in_tests(monkeypatch):
    """TT-Edge autoscan defaults ON in production (inside app.py startup) but
    must never spawn its browser/Telegram thread from a test import; tests
    that exercise it call the integration functions directly."""
    monkeypatch.setenv("TT_EDGE_AUTOSCAN_ENABLED", "false")
    monkeypatch.setenv("TT_EDGE_AUTO_INSTALL", "false")
    yield


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


@pytest.fixture(autouse=True)
def _marketlead_off_in_unrelated_tests(monkeypatch):
    """Keep generic run-cycle tests out of the live prospective audit DB.

    MarketLead unit tests construct an explicit runner with a temporary DB, so
    disabling only the process-global ``get_runner`` path preserves their
    coverage while preventing test-created rule registrations or observations.
    """
    monkeypatch.setenv("Q15_MARKETLEAD_ENABLED", "false")
    try:
        from q15_upgrade.marketlead.runner import reset_runner

        reset_runner()
    except Exception:  # pragma: no cover - package may be absent in a partial env
        pass
    yield
    try:
        from q15_upgrade.marketlead.runner import reset_runner

        reset_runner()
    except Exception:  # pragma: no cover
        pass


@pytest.fixture(autouse=True)
def _windows_tempdir_ignores_sqlite_cleanup_locks(monkeypatch):
    """Keep Windows local pytest runs from failing on open SQLite temp files.

    Several legacy v94/v95 ledgers intentionally reuse one SQLite connection for
    process-life performance, and Linux can unlink those files during temp-dir
    cleanup. Windows cannot. This affects only the test harness on Windows.
    """
    if os.name != "nt":
        yield
        return

    class _WindowsTemporaryDirectory(_TEMPORARY_DIRECTORY):
        def __init__(self, *args, **kwargs):
            kwargs["ignore_cleanup_errors"] = True
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(tempfile, "TemporaryDirectory", _WindowsTemporaryDirectory)
    yield
