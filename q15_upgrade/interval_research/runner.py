"""Interval-timing research — live-loop observer (read-only, default-OFF).

Mirrors the Ultoim observer: invoked every ~1s from checkpoint_v95.run_cycle, it
captures the champion's frozen analysis at each of the eight marks (band around
each mark), deduped per (ticker, interval). It NEVER trades, sends, or alters the
champion. ``resolve_settled`` attaches official outcomes from the same settlement
events the champion already produces.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Mapping, Sequence

from .config import (INTERVAL_MARKS, IntervalResearchConfig, window_key)
from .capture import build_capture_row, missing_reason
from .ledger import IntervalResearchLedger

logger = logging.getLogger(__name__)


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


class IntervalResearchRunner:
    def __init__(self, config: IntervalResearchConfig | None = None):
        self.config = config or IntervalResearchConfig.from_env()
        self.ledger = IntervalResearchLedger(self.config.db_path)
        self._lock = threading.Lock()

    @property
    def model_version(self) -> str:
        return self.config.model_version

    def observe(self, *, analyses: Mapping[str, Mapping[str, Any]],
                canonicals: Mapping[str, Any], now: float) -> None:
        """Capture every (asset, interval) whose seconds_remaining is in-band.

        Read-only and exception-isolated: a capture failure must never disturb the
        live cycle. Dedup is by the ledger's unique (model_version, ticker, interval)
        key, so repeated in-band cycles are no-ops after the first."""
        if not self.config.enabled or not self.ledger.available:
            return
        band = self.config.mark_band_seconds
        mv = self.config.model_version
        try:
            with self._lock:
                for asset, canonical in (canonicals or {}).items():
                    sr = _num(getattr(canonical, "seconds_remaining", None))
                    if sr is None:
                        continue
                    ticker = getattr(canonical, "ticker", None)
                    wk = window_key(getattr(canonical, "settlement_time", None), now)
                    analysis = analyses.get(asset) if isinstance(analyses, Mapping) else None
                    analysis = analysis if isinstance(analysis, Mapping) else {}
                    for interval, mark in INTERVAL_MARKS.items():
                        if not (mark - band) <= sr <= mark:
                            continue
                        if ticker and self.ledger.has_row(mv, str(ticker), interval):
                            continue
                        reason = missing_reason(analysis, canonical, self.config.min_data_quality)
                        if reason is not None:
                            # CONTRACT_NOT_MAPPED has no ticker to key — unrecordable.
                            if ticker:
                                self.ledger.record_missing(
                                    model_version=mv, ticker=str(ticker), asset=str(asset),
                                    interval=interval, reason=reason, window_key=wk, captured_at=now)
                            continue
                        capture_analysis = dict(analysis)
                        try:
                            from settlement_index import settlement_index_context

                            capture_analysis["settlement_index"] = settlement_index_context(
                                str(asset), spot_px=getattr(canonical, "spot", None), now=now)
                        except Exception:  # noqa: BLE001 - record-only context must not break capture
                            capture_analysis["settlement_index"] = {
                                "index_px": None, "basis_cents": None, "index_age_s": None}
                        row = build_capture_row(
                            model_version=mv, interval=interval, mark_seconds=mark,
                            asset=str(asset), analysis=capture_analysis, canonical=canonical,
                            window_key=wk, now=now)
                        self.ledger.record_capture(row)
        except Exception:  # never break the live loop
            logger.debug("interval-research observe failed (ignored)", exc_info=True)

    def resolve_settled(self, result_events: Sequence[Mapping[str, Any]] | None,
                        now: float) -> int:
        """Resolve captures from the champion's settlement events (read-only)."""
        if not self.config.enabled or not self.ledger.available or not result_events:
            return 0
        n = 0
        try:
            for ev in result_events:
                if not isinstance(ev, Mapping):
                    continue
                ticker = ev.get("ticker") or ev.get("contract")
                result = ev.get("result") or ev.get("official_result")
                if ticker and result:
                    n += self.ledger.resolve(self.config.model_version, str(ticker), str(result), now)
        except Exception:
            logger.debug("interval-research resolve failed (ignored)", exc_info=True)
        return n


_runner: IntervalResearchRunner | None = None
_runner_lock = threading.Lock()
_checked = False


def get_runner() -> IntervalResearchRunner | None:
    """Process-wide singleton, created only when the feature is enabled. Returns
    None when disabled (default) so the call site is a cheap no-op."""
    global _runner, _checked
    if _runner is not None:
        return _runner
    with _runner_lock:
        if _runner is not None:
            return _runner
        cfg = IntervalResearchConfig.from_env()
        if not cfg.enabled:
            _checked = True
            return None
        _runner = IntervalResearchRunner(cfg)
        return _runner


def reset_runner() -> None:
    """Test hook: drop the singleton so a fresh config/env is picked up."""
    global _runner, _checked
    _runner = None
    _checked = False
