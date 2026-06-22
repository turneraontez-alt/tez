"""Read-only entry-economics runner (entry-econ-v1).

A thin, exception-safe singleton wired beside the production loop. It evaluates
the entry economics for an analysis dict, optionally persists the evaluation to
its own ledger, and hands back the compact panel + a snapshot field bundle. It
never raises into the production path and never changes a prediction or the live
entry gate.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Mapping

from .config import EntryEconConfig
from .engine import EntryEconomicsResult, evaluate_analysis
from .ledger import EntryLedger
from .panel import render_panel

logger = logging.getLogger(__name__)


class EntryEconRunner:
    def __init__(self, config: EntryEconConfig | None = None):
        self.config = config or EntryEconConfig.from_env()
        self._ledger: EntryLedger | None = None
        self._lock = threading.Lock()
        if self.config.ledger_enabled:
            try:
                self._ledger = EntryLedger(self.config.db_path)
            except Exception:
                logger.exception("entry-econ ledger init failed (continuing without persistence)")
                self._ledger = None

    def evaluate(self, analysis: Mapping[str, Any], *, contract: str | None = None,
                 close_time: float | None = None, snapshot_id: str | None = None,
                 **eval_kwargs) -> EntryEconomicsResult | None:
        """Evaluate one analysis dict; record it if the ledger is enabled. Returns
        the result (None only on a hard internal error, never raising)."""
        try:
            result = evaluate_analysis(analysis, self.config, **eval_kwargs)
        except Exception:
            logger.exception("entry-econ evaluate failed (ignored)")
            return None
        if self._ledger is not None:
            try:
                with self._lock:
                    self._ledger.record_evaluation(
                        result, contract=contract, close_time=close_time,
                        model_version=self.config.model_version, snapshot_id=snapshot_id)
            except Exception:
                logger.exception("entry-econ record failed (ignored)")
        return result

    def panel(self, result: EntryEconomicsResult | None) -> str | None:
        if result is None:
            return None
        try:
            return render_panel(result)
        except Exception:
            logger.exception("entry-econ panel failed (ignored)")
            return None

    def mark_enter_sent(self, contract: str, checkpoint: str, **kw) -> bool:
        if self._ledger is None:
            return False
        try:
            with self._lock:
                return self._ledger.mark_enter_sent(
                    str(contract), str(checkpoint), model_version=self.config.model_version, **kw)
        except Exception:
            logger.exception("entry-econ mark_enter_sent failed (ignored)")
            return False

    def resolve(self, contract: str, checkpoint: str, official_result: str,
                resolved_at: float | None = None) -> bool:
        if self._ledger is None:
            return False
        try:
            with self._lock:
                return self._ledger.resolve(
                    str(contract), str(checkpoint), str(official_result),
                    model_version=self.config.model_version, resolved_at=resolved_at)
        except Exception:
            logger.exception("entry-econ resolve failed (ignored)")
            return False

    def scoreboard(self) -> dict[str, Any]:
        if self._ledger is None:
            return {"official_entries": 0, "ledger": "disabled"}
        try:
            with self._lock:
                out = self._ledger.entry_scoreboard(self.config.model_version)
                out["decision_counts"] = self._ledger.decision_counts(self.config.model_version)
                out["background"] = self._ledger.background_studies(self.config.model_version)
                return out
        except Exception:
            logger.exception("entry-econ scoreboard failed (ignored)")
            return {"official_entries": 0, "ledger": "error"}


# ---- module singleton (built only when enabled) ----
_runner: EntryEconRunner | None = None
_runner_lock = threading.Lock()
_enabled_cache: bool | None = None


def is_enabled() -> bool:
    global _enabled_cache
    if _enabled_cache is None:
        _enabled_cache = EntryEconConfig.from_env().enabled
    return _enabled_cache


def get_runner() -> EntryEconRunner | None:
    if not is_enabled():
        return None
    global _runner
    if _runner is None:
        with _runner_lock:
            if _runner is None:
                try:
                    _runner = EntryEconRunner()
                except Exception:
                    logger.exception("entry-econ runner init failed (disabled)")
                    return None
    return _runner
