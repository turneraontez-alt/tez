"""Ultoim Build — a separate, read-only research reporting system.

Runs beside the live V9.5 champion. It never trades and never modifies the live
system's predictions / records / Telegram channel. ON by default as a read-only
collector — but MUTED (records, never delivers) until its own Telegram channel is
set; ``Q15_ULTOIM_ENABLED=false`` makes the app byte-identical. It reuses the
champion's frozen per-asset analysis to publish three value-ranked final-outcome
picks at the 12M / 10M / 7M marks — with a multi-factor A/B/C grade and an
active-research flip/stability estimate — into its own database and Telegram
channel.

Wiring (both guarded + default-OFF):
  * ``checkpoint_v95.run_cycle`` calls ``get_runner().observe(...)`` once per cycle.
  * ``app.py`` calls ``get_runner().reconcile(now, market_cache)`` to grade
    settled picks against the shared Kalshi result cache.
"""
from __future__ import annotations

from .config import UltoimConfig, is_enabled
from .runner import UltoimRunner, get_runner

__all__ = ["UltoimConfig", "UltoimRunner", "get_runner", "is_enabled"]
