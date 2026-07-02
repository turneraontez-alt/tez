"""Interval-timing research — live-loop observer (read-only, default-OFF).

Mirrors the Ultoim observer: invoked every ~1s from checkpoint_v95.run_cycle, it
captures the champion's frozen analysis at each of the eight marks (band around
each mark), deduped per (ticker, interval). It NEVER trades, sends, or alters the
champion. ``resolve_settled`` attaches official outcomes from the same settlement
events the champion already produces.
"""
from __future__ import annotations

import logging
import os
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


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _analysis_value(analysis: Mapping[str, Any], quote: Mapping[str, Any], key: str) -> Any:
    if quote.get(key) is not None:
        return quote.get(key)
    return analysis.get(key)


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
                        if self.ledger.record_capture(row):
                            self._feed_thirteen_m_sniper(row, capture_analysis)
        except Exception:  # never break the live loop
            logger.debug("interval-research observe failed (ignored)", exc_info=True)

    def _feed_thirteen_m_sniper(self, row: Mapping[str, Any],
                                analysis: Mapping[str, Any]) -> None:
        """Forward new 13M captures into the V3 source-row runtime when explicitly enabled."""
        if not _env_bool("Q15_V3_13M_SNIPER_FEED", False):
            return
        if str(row.get("interval") or "").upper() != "13M":
            return
        quote = _mapping(analysis.get("quote"))
        source_row = {
            "created_at": row.get("captured_at"),
            "model_version": row.get("model_version"),
            "asset": row.get("asset"),
            "ticker": row.get("ticker"),
            "interval": "13M",
            "window_key": row.get("window_key"),
            "close_time": row.get("close_time"),
            "record_kind": "INTERVAL_RESEARCH_13M",
            "delivery_status": "RECORDED",
            "predicted_side": row.get("predicted_side"),
            "calibrated_yes_probability": row.get("calibrated_yes_probability"),
            "raw_yes_probability": row.get("raw_yes_probability"),
            "conservative_probability": row.get("conservative_probability"),
            "selected_probability": _analysis_value(analysis, quote, "selected_probability"),
            "market_implied_yes_probability": _analysis_value(
                analysis, quote, "market_implied_yes_probability"),
            "flip_probability": row.get("flip_probability"),
            "entry_ask_cents": row.get("entry_ask_cents"),
            "yes_bid_cents": row.get("yes_bid_cents"),
            "yes_ask_cents": row.get("yes_ask_cents"),
            "no_ask_cents": _analysis_value(analysis, quote, "no_ask_cents"),
            "spread_cents": row.get("spread_cents"),
            "depth_contracts": row.get("depth_contracts"),
            "spot_depth_trade_net_notional_60s": _analysis_value(
                analysis, quote, "spot_depth_trade_net_notional_60s"),
        }
        try:
            from q15_upgrade.strategy_bots import runtime as strategy_bots_runtime

            strategy_bots_runtime.record_source_row(source_row, source_system="ultoim_v2")
        except Exception:  # noqa: BLE001 - optional research feed must never break capture
            logger.warning("interval-research 13M sniper feed failed (ignored)", exc_info=True)

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
