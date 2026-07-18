"""CLI jobs: :mod:`scan` (board -> analyze -> alert), :mod:`grade`
(results -> settle -> bankroll -> calibration), :mod:`odds_entry` (manual
odds stopgap), :mod:`bankroll` (seed/inspect the roll)."""
from __future__ import annotations

import logging


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")
