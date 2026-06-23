"""Ultoim V2 — a separate, default-OFF, read-only PAPER entry-alert system.

Runs beside the live V9.5 champion. It NEVER trades and never modifies the live
system's predictions / records / Telegram channel. DEFAULT-OFF: with the env unset
the app is byte-identical (``get_runner()`` returns ``None``). When enabled, it
reuses the champion's frozen per-asset analysis (read-only) to emit a single paper
"BEST ENTRY" research card per qualifying contract per 15-minute window — gated on
side / confidence / ask-band / net-edge — into its OWN database and (optionally)
its OWN Telegram channel, and learns from officially settled results.

Wiring (both guarded + default-OFF):
  * ``checkpoint_v95.run_cycle`` calls ``get_runner().observe(...)`` once per cycle.
  * ``app.py`` calls ``get_runner().reconcile(now, market_cache)`` and
    ``get_runner().maybe_send_recap(now)`` to grade settled rows and emit recaps.
"""
from __future__ import annotations

from .config import UltoimV2Config, is_enabled
from .runner import UltoimV2Runner, get_runner

__all__ = ["UltoimV2Config", "UltoimV2Runner", "get_runner", "is_enabled"]
