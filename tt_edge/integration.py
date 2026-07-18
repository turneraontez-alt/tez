"""Run the TT-Edge autoscan INSIDE the Q15 monitor process.

The owner's deploy routine is `git pull` + restart the app — nothing else.
So the pick loop rides along: ``app.py`` calls :func:`start_autoscan` at
startup (same guarded pattern as every other subsystem) and a daemon thread
runs the fetch -> grade -> scan -> alert cycle on its interval. Inside the
app process the Q15 Telegram credentials are already present, so the
default fallback delivers picks to the operator's existing channel with
zero configuration.

Self-bootstrapping (gated by TT_EDGE_AUTO_INSTALL, default on):

* missing ``playwright`` package -> one-time ``pip install playwright``;
* missing Chromium -> one-time ``python -m playwright install chromium``
  (idempotent and fast when already present);
* empty bankroll -> seeded from TT_EDGE_BANKROLL_INIT_DOLLARS (default
  65.00 — the spec's launch tier; a PAPER value, logged loudly).

Containment: this module must NEVER take the monitor down. Every cycle runs
behind a deliberate top-level boundary that logs and continues; a failed
bootstrap degrades to cache-only cycles with a periodic error instead of
crashing. Kill switch: TT_EDGE_AUTOSCAN_ENABLED=false. Do not also run the
standalone ``jobs/autoscan.py`` against the same DB — one loop per DB.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

_STARTED = threading.Event()   # process-wide once-guard


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def autoscan_enabled() -> bool:
    return _flag("TT_EDGE_AUTOSCAN_ENABLED", True)


def auto_install_enabled() -> bool:
    return _flag("TT_EDGE_AUTO_INSTALL", True)


def ensure_playwright(*, run: Callable = subprocess.run) -> bool:
    """Best-effort Playwright bootstrap. True = importable (browser download
    is attempted but a failure there still returns True — launch may work
    via PLAYWRIGHT_CHROMIUM_EXECUTABLE or an existing install)."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        if not auto_install_enabled():
            logger.error("TT-Edge: playwright missing and TT_EDGE_AUTO_INSTALL"
                         " is off — autoscan will run cache-only")
            return False
        logger.warning("TT-Edge: installing playwright (one-time)...")
        try:
            result = run([sys.executable, "-m", "pip", "install", "playwright"],
                         capture_output=True, timeout=900)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.error("TT-Edge: pip install playwright failed: %s", exc)
            return False
        if result.returncode != 0:
            logger.error("TT-Edge: pip install playwright failed (rc=%s)",
                         result.returncode)
            return False
        try:
            import playwright  # noqa: F401
        except ImportError:
            logger.error("TT-Edge: playwright still not importable after "
                         "install")
            return False
    if auto_install_enabled() and \
            not os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"):
        # Idempotent; a no-op when Chromium is already present.
        try:
            result = run([sys.executable, "-m", "playwright", "install",
                          "chromium"], capture_output=True, timeout=1800)
            if result.returncode != 0:
                logger.warning("TT-Edge: chromium install rc=%s — will still "
                               "try to launch", result.returncode)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("TT-Edge: chromium install failed: %s — will "
                           "still try to launch", exc)
    return True


def ensure_bankroll(repo, environ=os.environ) -> int:
    """Seed the PAPER bankroll on first run; never touch an existing one."""
    cents = repo.get_bankroll_cents()
    if cents is not None:
        return cents
    from tt_edge.jobs.bankroll import dollars_to_cents
    raw = environ.get("TT_EDGE_BANKROLL_INIT_DOLLARS", "65.00")
    try:
        cents = dollars_to_cents(raw)
    except ValueError:
        logger.error("TT-Edge: bad TT_EDGE_BANKROLL_INIT_DOLLARS=%r; "
                     "seeding $65.00", raw)
        cents = 6500
    repo.set_bankroll_cents(cents, datetime.now(timezone.utc))
    logger.warning("TT-Edge: seeded PAPER bankroll at $%.2f (adjust with "
                   "TT_EDGE_BANKROLL_INIT_DOLLARS or the bankroll CLI)",
                   cents / 100)
    return cents


def _cycle_once(config, sender, data_dir: Path, fetch_ok: bool) -> None:
    """One fetch+grade+scan cycle with a fresh, thread-owned repo."""
    from tt_edge.db import repo as repo_mod
    from tt_edge.jobs.autoscan import cycle_dates, load_envelopes, run_cycle
    from tt_edge.scrapers import sofascore

    now = datetime.now(timezone.utc)
    fetch_dates, scan_dates = cycle_dates(now, config.autoscan_dates_forward)
    if fetch_ok:
        sofascore.fetch_bundle(
            fetch_dates, data_dir,
            tournament_keyword=config.tournament_keyword,
            provider_id=config.odds_provider_id,
            max_matches=config.autoscan_max_matches,
            request_gap_s=config.request_gap_ms / 1000.0,
            h2h_refresh_s=float(config.h2h_refresh_s))
    repo = repo_mod.connect(config.database_url)
    try:
        repo.apply_migrations()
        ensure_bankroll(repo)
        report = run_cycle(repo=repo, config=config,
                           envelopes=load_envelopes(data_dir),
                           scan_dates=scan_dates,
                           now=datetime.now(timezone.utc), sender=sender,
                           dry_run=False)
        if report.boards_scanned == 0:
            logger.error("TT-Edge: cycle saw no board — diagnose with "
                         "`python3 -m tt_edge.jobs.autoscan --probe`")
    finally:
        repo.close()


def autoscan_loop(stop: threading.Event, *,
                  cycle: Callable | None = None,
                  ensure: Callable[[], bool] | None = None) -> None:
    """The thread body. Every cycle is a deliberate containment boundary —
    the Q15 monitor must never go down because of TT-Edge."""
    from tt_edge.alerts.telegram import TTEdgeTelegram
    from tt_edge.config import load_config
    from tt_edge.envfile import bootstrap_env
    from tt_edge.scrapers import sofa_odds

    bootstrap_env()
    try:
        config = load_config()
    except Exception:  # noqa: BLE001 - boundary: bad env must not kill app
        logger.exception("TT-Edge: config invalid; autoscan not running")
        return
    if os.environ.get("TT_EDGE_BOOK") is None:
        config = dataclasses.replace(config, book=sofa_odds.BOOK)
    data_dir = Path(config.autoscan_data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    sender = TTEdgeTelegram.from_config(config)
    if sender.configured:
        logger.info("TT-Edge autoscan: telegram via %s, every %ds",
                    sender.source, config.autoscan_interval_s)
    else:
        logger.warning("TT-Edge autoscan: telegram MUTED (no TT_EDGE or Q15 "
                       "credentials) — records only, no alerts")
    fetch_ok = (ensure or ensure_playwright)()

    run_one = cycle or _cycle_once
    while not stop.is_set():
        try:
            run_one(config, sender, data_dir, fetch_ok)
        except Exception:  # noqa: BLE001 - boundary: log and keep cycling
            logger.exception("TT-Edge autoscan cycle failed; next cycle in "
                             "%ds", config.autoscan_interval_s)
        stop.wait(config.autoscan_interval_s)


def start_autoscan() -> bool:
    """Called by app.py at startup. Env-gated; once per process; never
    raises past its own logging."""
    if not autoscan_enabled():
        logger.info("TT-Edge autoscan disabled (TT_EDGE_AUTOSCAN_ENABLED)")
        return False
    if _STARTED.is_set():
        return True
    _STARTED.set()
    threading.Thread(target=autoscan_loop, args=(threading.Event(),),
                     daemon=True, name="tt-edge-autoscan").start()
    logger.info("TT-Edge autoscan thread started")
    return True
