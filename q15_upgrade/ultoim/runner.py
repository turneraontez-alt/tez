"""Ultoim Build — background orchestrator.

Owns a single daemon worker thread. The live ~1s loop only calls ``observe()``
(synchronously extracts compact per-asset fields and enqueues) and
``reconcile()`` (throttled enqueue). All ranking, grading, flip research, SQLite
writes, and Telegram delivery happen on the worker, so the live loop never
blocks. Default-ON (read-only collector); ``get_runner()`` returns None only when
``Q15_ULTOIM_ENABLED=false``.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Mapping

from . import flip_research, panel, ranker
from .config import INTERVAL_MARKS, UltoimConfig, is_enabled
from .ledger import UltoimLedger, _window_key
from .telegram import UltoimTelegram

logger = logging.getLogger("ultoim.runner")


def _resolved_result(market: Any) -> str | None:
    """The immutable YES/NO outcome from a Kalshi market mapping, or None."""
    if not isinstance(market, Mapping):
        return None
    result = str(market.get("result") or "").upper()
    return result if result in ("YES", "NO") else None


class UltoimRunner:
    def __init__(self, config: UltoimConfig) -> None:
        self.config = config
        self.ledger = UltoimLedger(config.db_path)
        self.ledger.ensure_reset_marker(config.model_version, time.time())
        self.telegram = UltoimTelegram(config.telegram_chat_id)
        self._jobs: "queue.Queue[tuple[str, dict]]" = queue.Queue(maxsize=512)
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._last_reconcile_at = 0.0

    # -- worker plumbing ------------------------------------------------------
    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._worker_loop, name="ultoim-build", daemon=True
                )
                self._worker.start()

    def _worker_loop(self) -> None:
        while True:
            kind, kw = self._jobs.get()
            try:
                if kind == "observe":
                    self._observe_sync(**kw)
                elif kind == "reconcile":
                    self._reconcile_sync(**kw)
            except Exception:  # noqa: BLE001 - worker boundary: log + keep running
                logger.exception("ultoim worker job failed (ignored)")
            finally:
                self._jobs.task_done()

    # -- observe (live loop -> enqueue) --------------------------------------
    def observe(self, *, analyses: Mapping[str, Mapping[str, Any]],
                canonicals: Mapping[str, Any], now: float) -> None:
        """Extract compact, race-free per-asset fields synchronously and enqueue.
        Never raises into the live loop."""
        candidates: list[dict[str, Any]] = []
        for asset, analysis in analyses.items():
            canonical = canonicals.get(asset)
            if canonical is None or not isinstance(analysis, Mapping):
                continue
            secs = getattr(canonical, "seconds_remaining", None)
            ticker = getattr(canonical, "ticker", None)
            if not ticker or secs is None:
                continue
            if not analysis.get("prediction_available"):
                continue
            quote = analysis.get("quote") or {}
            signals = analysis.get("shadow_signals") or {}
            flip = analysis.get("flip_risk") or {}
            manip = analysis.get("manipulation") or {}
            close_time = getattr(canonical, "settlement_time", None)
            candidates.append({
                "asset": str(asset),
                "ticker": str(ticker),
                "seconds_remaining": float(secs),
                "close_time": float(close_time) if close_time is not None else None,
                "predicted_side": str(analysis.get("prediction_side") or "").upper(),
                "selected_probability": analysis.get("selected_probability"),
                "conservative_probability": analysis.get("conservative_probability"),
                "calibrated_yes_probability": analysis.get("yes_probability"),
                "challenger_yes_probability": analysis.get("challenger_yes_probability"),
                "baseline_yes_probability": analysis.get("baseline_yes_probability"),
                "data_quality": analysis.get("data_quality"),
                "evidence_quality": analysis.get("evidence_quality"),
                "net_edge_cents": analysis.get("net_edge_cents"),
                "entry_ask_cents": quote.get("ask_cents"),
                "manipulation_suspected": bool(manip.get("suspected")),
                "flip_risk_score": flip.get("score"),
                "order_flow_persistence": signals.get("order_flow_persistence"),
                "book_resiliency": signals.get("book_resiliency"),
                "prediction_stability": signals.get("prediction_stability"),
            })
        if not candidates:
            return
        try:
            self._ensure_worker()
            self._jobs.put_nowait(("observe", {"candidates": candidates, "now": now}))
        except queue.Full:
            logger.debug("ultoim observe queue full; cycle dropped")

    def _observe_sync(self, *, candidates: list[dict[str, Any]], now: float) -> None:
        # Group candidates by their 15-minute settlement window.
        windows: dict[int, list[dict[str, Any]]] = {}
        for cand in candidates:
            wk = _window_key(cand.get("close_time"), now)
            windows.setdefault(wk, []).append(cand)
        for window_key, cands in windows.items():
            for interval, mark in INTERVAL_MARKS.items():
                in_band = [c for c in cands
                           if (mark - self.config.mark_band_seconds) <= c["seconds_remaining"] <= mark]
                if not in_band:
                    continue
                if self.ledger.report_locked(self.config.model_version, interval, window_key):
                    continue
                self._fire_report(interval, window_key, cands, now)

    def _fire_report(self, interval: str, window_key: int,
                     cands: list[dict[str, Any]], now: float) -> None:
        # Score + grade every candidate in the window, then value-rank the top-3.
        scored: list[dict[str, Any]] = []
        for cand in cands:
            flip_prob = flip_research.ensemble_flip_probability(cand)
            pick = dict(cand)
            pick["flip_probability"] = flip_prob
            pick["quality_score"] = ranker.quality_score(cand, flip_prob, self.config)
            pick.update(flip_research.flip_fields(cand["predicted_side"], flip_prob, self.config))
            sel = cand.get("selected_probability")
            pick["outcome_pct"] = round(float(sel) * 100.0, 1) if sel is not None else None
            scored.append(pick)
        top = ranker.value_rank(scored, self.config)
        if not top:
            return
        # Claim the lock FIRST (atomic dedup): only the first fire records/sends.
        if not self.ledger.lock_report(self.config.model_version, interval, window_key, now):
            return
        recorded: list[tuple[int, dict[str, Any]]] = []
        for pick in top:
            row = {
                "created_at": now, "model_version": self.config.model_version,
                "asset": pick["asset"], "ticker": pick["ticker"], "interval": interval,
                "window_key": window_key, "rank": pick["rank"],
                "predicted_side": pick["predicted_side"], "outcome_pct": pick.get("outcome_pct"),
                "selected_yes_probability": pick.get("calibrated_yes_probability"),
                "conservative_probability": pick.get("conservative_probability"),
                "net_edge_cents": pick.get("net_edge_cents"),
                "entry_ask_cents": pick.get("entry_ask_cents"),
                "quality_score": pick.get("quality_score"),
                "confidence_grade": pick.get("confidence_grade"),
                "data_quality": pick.get("data_quality"),
                "evidence_quality": pick.get("evidence_quality"),
                "model_agreement": ranker.model_agreement(
                    ranker.agreement_probs(pick, self.config)),
                "manipulation_suspected": 1 if pick.get("manipulation_suspected") else 0,
                "flip_probability": pick.get("flip_probability"),
                "flip_decision": pick.get("flip_decision"),
                "original_predicted_side": pick.get("original_predicted_side"),
                "expected_flip_side": pick.get("expected_flip_side"),
                "order_flow_persistence": pick.get("order_flow_persistence"),
                "book_resiliency": pick.get("book_resiliency"),
                "prediction_stability": pick.get("prediction_stability"),
                "close_time": pick.get("close_time"), "snapshot_id": f"{interval}@{int(now)}",
                "delivery_status": "PENDING",
            }
            pid = self.ledger.record_pick(row)
            if pid is not None:
                recorded.append((pid, pick))
        # Deliver to Ultoim's own channel (worker thread; never blocks live loop).
        text = panel.build_report(interval, top)
        result = self.telegram.send(text)
        if result.get("delivered"):
            status, mid = "SENT", result.get("message_id")
        elif result.get("muted"):
            status, mid = "MUTED", None
        else:
            status, mid = "DELIVERY_FAILED", None
        self.ledger.set_report_message(self.config.model_version, interval, window_key, mid)
        for pid, _ in recorded:
            self.ledger.mark_delivery(pid, status, mid, result.get("error"))

    # -- reconcile (settlement grading) --------------------------------------
    def reconcile(self, now: float, resolver: Any) -> None:
        """Enqueue a settlement reconcile, throttled. ``resolver`` is the shared
        Kalshi market cache (``get_market(ticker) -> market``)."""
        if now - self._last_reconcile_at < self.config.reconcile_every_seconds:
            return
        self._last_reconcile_at = now
        try:
            self._ensure_worker()
            self._jobs.put_nowait(("reconcile", {"resolver": resolver, "now": now}))
        except queue.Full:
            logger.debug("ultoim reconcile queue full; skipped")

    def _reconcile_sync(self, *, resolver: Any, now: float) -> None:
        get_market = getattr(resolver, "get_market", None)
        if not callable(get_market):
            return
        for ticker in self.ledger.unresolved_closed(self.config.model_version, now):
            try:
                market = get_market(ticker)
            except Exception:  # noqa: BLE001 - one bad fetch must not stop the pass
                logger.debug("ultoim resolve fetch failed for %s", ticker, exc_info=True)
                continue
            result = _resolved_result(market)
            if result is not None:
                self.ledger.resolve(self.config.model_version, ticker, result, now)

    # -- read-only status -----------------------------------------------------
    def scoreboard(self) -> dict[str, Any]:
        return self.ledger.scoreboard(self.config.model_version)


_runner: UltoimRunner | None = None
_runner_lock = threading.Lock()


def get_runner() -> UltoimRunner | None:
    """Singleton Ultoim runner, or None when disabled. Safe to call every cycle."""
    if not is_enabled():
        return None
    global _runner
    if _runner is None:
        with _runner_lock:
            if _runner is None:
                try:
                    _runner = UltoimRunner(UltoimConfig.from_env())
                except Exception:  # noqa: BLE001 - never break the loop on init failure
                    logger.exception("ultoim runner init failed; disabling")
                    return None
    return _runner


def reset_runner_for_tests() -> None:
    global _runner
    with _runner_lock:
        _runner = None
