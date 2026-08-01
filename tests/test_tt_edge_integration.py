"""In-app autoscan integration: env gating, bootstrap behavior, bankroll
auto-seed, and cycle-failure containment (the monitor must never die from a
TT-Edge bug)."""
import subprocess
import threading

import pytest

from tests import tt_edge_fixtures as fx
from tt_edge import integration


class TestGating:
    def test_disabled_by_conftest_env(self):
        # The autouse fixture forces the flag off for every test.
        assert integration.autoscan_enabled() is False
        assert integration.start_autoscan() is False

    def test_enabled_flag_values(self, monkeypatch):
        for raw, expected in (("true", True), ("1", True), ("on", True),
                              ("false", False), ("0", False), ("no", False)):
            monkeypatch.setenv("TT_EDGE_AUTOSCAN_ENABLED", raw)
            assert integration.autoscan_enabled() is expected
        monkeypatch.delenv("TT_EDGE_AUTOSCAN_ENABLED")
        # Production default is OFF (cloud-first: the hourly Routine owns
        # every league; an unconfigured app must not double-alert).
        assert integration.autoscan_enabled() is False


def _playwright_installed() -> bool:
    """Is the real playwright package importable here?"""
    import importlib.util

    try:
        return importlib.util.find_spec("playwright") is not None
    except (ImportError, ValueError):
        return False


# Two tests below assert the HAPPY path, which needs playwright genuinely
# importable. It is on the cloud box but not in every local venv, and a missing
# optional scraper dependency is an environment fact, not a regression — skip
# rather than fail. The negative-path test above stubs the import and always runs.
requires_playwright = pytest.mark.skipif(
    not _playwright_installed(),
    reason="playwright is not installed in this environment")


class TestEnsurePlaywright:
    def test_no_install_when_disabled_and_missing(self, monkeypatch):
        real_import = __builtins__["__import__"] if isinstance(
            __builtins__, dict) else __builtins__.__import__

        def fake_import(name, *args, **kwargs):
            if name == "playwright":
                raise ImportError("nope")
            return real_import(name, *args, **kwargs)

        monkeypatch.setenv("TT_EDGE_AUTO_INSTALL", "false")
        monkeypatch.setattr("builtins.__import__", fake_import)
        calls = []
        assert integration.ensure_playwright(run=lambda *a, **k:
                                             calls.append(a)) is False
        assert calls == []

    @requires_playwright
    def test_importable_returns_true_without_subprocess_when_exe_set(
            self, monkeypatch):
        # playwright IS importable in this environment; with an explicit
        # executable override no chromium download is attempted.
        monkeypatch.setenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "/opt/x")
        calls = []
        assert integration.ensure_playwright(
            run=lambda *a, **k: calls.append(a)) is True
        assert calls == []

    @requires_playwright
    def test_chromium_install_attempted_when_enabled(self, monkeypatch):
        monkeypatch.setenv("TT_EDGE_AUTO_INSTALL", "true")
        monkeypatch.delenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE", raising=False)
        commands = []

        def fake_run(cmd, **kwargs):
            commands.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        assert integration.ensure_playwright(run=fake_run) is True
        assert any("chromium" in " ".join(map(str, cmd)) for cmd in commands)


class TestEnsureBankroll:
    def test_seeds_default_once(self):
        repo = fx.make_repo()
        assert integration.ensure_bankroll(repo, environ={}) == 6500
        assert repo.get_bankroll_cents() == 6500

    def test_env_override_and_existing_untouched(self):
        repo = fx.make_repo()
        cents = integration.ensure_bankroll(
            repo, environ={"TT_EDGE_BANKROLL_INIT_DOLLARS": "120.00"})
        assert cents == 12000
        # A later call with a different env never reseeds.
        assert integration.ensure_bankroll(
            repo, environ={"TT_EDGE_BANKROLL_INIT_DOLLARS": "5.00"}) == 12000

    def test_bad_env_falls_back_to_default(self):
        repo = fx.make_repo()
        assert integration.ensure_bankroll(
            repo, environ={"TT_EDGE_BANKROLL_INIT_DOLLARS": "lots"}) == 6500


class TestLoopContainment:
    def _run_loop(self, monkeypatch, cycle, cycles=2):
        """Drive autoscan_loop deterministically: stop after N waits."""
        monkeypatch.setenv("TT_EDGE_DATABASE_URL", ":memory:")
        stop = threading.Event()
        waits = []
        original_wait = stop.wait

        def counting_wait(timeout=None):
            waits.append(timeout)
            if len(waits) >= cycles:
                stop.set()
            return True

        monkeypatch.setattr(stop, "wait", counting_wait)
        integration.autoscan_loop(stop, cycle=cycle, ensure=lambda: False)
        return waits

    def test_cycle_exception_does_not_kill_loop(self, monkeypatch):
        calls = []

        def exploding_cycle(config, sender, data_dir, fetch_ok):
            calls.append(fetch_ok)
            raise RuntimeError("boom")

        waits = self._run_loop(monkeypatch, exploding_cycle, cycles=2)
        assert len(calls) == 2            # loop survived the first explosion
        assert waits == [1800, 1800]

    def test_cycle_receives_config_and_fetch_flag(self, monkeypatch):
        seen = {}

        def observing_cycle(config, sender, data_dir, fetch_ok):
            seen["book"] = config.book
            seen["fetch_ok"] = fetch_ok
            seen["sender_source"] = sender.source

        self._run_loop(monkeypatch, observing_cycle, cycles=1)
        assert seen["book"] == "sofascore"     # autoscan book default
        assert seen["fetch_ok"] is False       # ensure() stubbed to False

    def test_invalid_config_returns_instead_of_raising(self, monkeypatch):
        monkeypatch.setenv("TT_EDGE_MIN_EDGE", "not-a-number")
        stop = threading.Event()
        integration.autoscan_loop(stop, cycle=lambda *a: None,
                                  ensure=lambda: False)   # must not raise
