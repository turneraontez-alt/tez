"""Ultoim V2 — background orchestrator (read-only paper entry alerts).

Owns a single daemon worker thread. The live ~1s loop only calls ``observe()``
(synchronously extracts compact per-asset fields and enqueues), ``reconcile()``
(throttled enqueue), and ``maybe_send_recap()`` (throttled). All gating, recording,
SQLite writes, and Telegram delivery happen on the worker, so the live loop never
blocks. DEFAULT-OFF: ``get_runner()`` returns ``None`` unless
``Q15_ULTOIM_V2_ENABLED=true``.

A V2 failure must NEVER disturb the live cycle — every public entry point is
exception-isolated, and the worker logs+continues on any job error.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Mapping

from . import gate, panel
from .config import INTERVAL_MARKS, UltoimV2Config, is_enabled
from .ledger import UltoimV2Ledger, _window_key
from .telegram import UltoimV2Telegram

logger = logging.getLogger("ultoim_v2.runner")


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _resolved_result(market: Any) -> str | None:
    """The immutable YES/NO outcome from a Kalshi market mapping, or None."""
    if not isinstance(market, Mapping):
        return None
    result = str(market.get("result") or "").upper()
    return result if result in ("YES", "NO") else None


def _regime_directional(market_implied_yes: float | None) -> str:
    """Best-effort live proxy for the settlement-window directional regime, from the
    market-implied YES probability available at decision time. UNVALIDATED — it is a
    coarse live stand-in, not a measured regime: >0.6 -> YES_PRONE, <0.4 -> NO_PRONE,
    else BALANCED."""
    if market_implied_yes is None:
        return "BALANCED"
    if market_implied_yes > 0.6:
        return "YES_PRONE"
    if market_implied_yes < 0.4:
        return "NO_PRONE"
    return "BALANCED"


class UltoimV2Runner:
    def __init__(self, config: UltoimV2Config) -> None:
        self.config = config
        self.ledger = UltoimV2Ledger(config.db_path)
        self.ledger.ensure_reset_marker(config.model_version, time.time())
        self.session_id = self.ledger.session_id()
        self.telegram = UltoimV2Telegram(config.telegram_chat_id)
        self._jobs: "queue.Queue[tuple[str, dict]]" = queue.Queue(maxsize=512)
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._last_reconcile_at = 0.0
        self._last_recap_at = 0.0

    # -- worker plumbing ------------------------------------------------------
    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._worker_loop, name="ultoim-v2", daemon=True
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
                elif kind == "recap":
                    self._recap_sync(**kw)
            except Exception:  # noqa: BLE001 - worker boundary: log + keep running
                logger.exception("ultoim_v2 worker job failed (ignored)")
            finally:
                self._jobs.task_done()

    # -- observe (live loop -> enqueue) --------------------------------------
    def observe(self, *, analyses: Mapping[str, Mapping[str, Any]],
                canonicals: Mapping[str, Any], now: float) -> None:
        """Extract compact, race-free per-asset fields synchronously and enqueue.
        Never raises into the live loop."""
        candidates: list[dict[str, Any]] = []
        for asset, analysis in (analyses or {}).items():
            canonical = (canonicals or {}).get(asset)
            if canonical is None or not isinstance(analysis, Mapping):
                continue
            secs = _num(getattr(canonical, "seconds_remaining", None))
            ticker = getattr(canonical, "ticker", None)
            if not ticker or secs is None:
                continue
            if not analysis.get("prediction_available"):
                continue
            quote = analysis.get("quote") or {}
            signals = analysis.get("shadow_signals") or {}
            flip = analysis.get("flip_risk") or {}
            manip = analysis.get("manipulation") or {}
            costs = analysis.get("costs") or {}
            regime = analysis.get("regime") or {}
            close_time = getattr(canonical, "settlement_time", None)
            total_cost = costs.get("total_cost_cents")
            if total_cost is None:
                total_cost = costs.get("total_cents")
            candidates.append({
                "asset": str(asset),
                "ticker": str(ticker),
                "seconds_remaining": float(secs),
                "close_time": float(close_time) if close_time is not None else None,
                "predicted_side": str(analysis.get("prediction_side") or "").upper(),
                "selected_probability": analysis.get("selected_probability"),
                "calibrated_yes_probability": analysis.get("yes_probability"),
                "conservative_probability": analysis.get("conservative_probability"),
                "market_implied_yes_probability": analysis.get("market_implied_yes_probability"),
                "raw_yes_probability": analysis.get("raw_yes_probability"),
                "net_edge_cents": analysis.get("net_edge_cents"),
                "entry_ask_cents": (analysis.get("entry_ask_cents")
                                    if analysis.get("entry_ask_cents") is not None
                                    else quote.get("ask_cents")),
                "fee_cents": costs.get("fee_cents"),
                "total_cost_cents": total_cost,
                "spread_cents": quote.get("spread_cents"),
                "depth_contracts": quote.get("depth_contracts"),
                "quote_age_seconds": quote.get("quote_age_seconds"),
                "spot_stale_age_seconds": self._spot_stale_age(canonical, analysis),
                "distance_sigma": regime.get("distance_sigma"),
                "regime_name": regime.get("name"),
                "data_quality": analysis.get("data_quality"),
                "evidence_quality": analysis.get("evidence_quality"),
                "manipulation_suspected": bool(manip.get("suspected")),
                "flip_probability": flip.get("score"),
                "order_flow_persistence": signals.get("order_flow_persistence"),
                "book_resiliency": signals.get("book_resiliency"),
                "prediction_stability": signals.get("prediction_stability"),
                "snapshot_id": analysis.get("snapshot_id"),
            })
        if not candidates:
            return
        try:
            self._ensure_worker()
            self._jobs.put_nowait(("observe", {"candidates": candidates, "now": now}))
        except queue.Full:
            logger.debug("ultoim_v2 observe queue full; cycle dropped")

    @staticmethod
    def _spot_stale_age(canonical: Any, analysis: Mapping[str, Any]) -> float | None:
        """Best-effort spot-staleness age in seconds. The champion does not expose a
        single canonical 'spot stale age', so try a few known shapes; None when
        unavailable (treated as not-stale, i.e. no STALE_FEED abstain)."""
        for key in ("spot_stale_age_seconds", "stale_age_seconds", "spot_age_seconds"):
            v = _num(analysis.get(key))
            if v is not None:
                return v
        feed_ages = getattr(canonical, "feed_ages", None)
        if isinstance(feed_ages, Mapping):
            v = _num(feed_ages.get("spot") or feed_ages.get("core"))
            if v is not None:
                return v
        return None

    def _observe_sync(self, *, candidates: list[dict[str, Any]], now: float) -> None:
        cfg = self.config
        mv = cfg.model_version
        # Group candidates by their 15-minute settlement window.
        windows: dict[int, list[dict[str, Any]]] = {}
        for cand in candidates:
            wk = _window_key(cand.get("close_time"), now)
            windows.setdefault(wk, []).append(cand)
        for window_key, cands in windows.items():
            for interval, mark in INTERVAL_MARKS.items():
                in_band = [c for c in cands
                           if (mark - cfg.mark_band_seconds) <= c["seconds_remaining"] <= mark]
                if not in_band:
                    continue
                if self.ledger.report_locked(mv, interval, window_key):
                    continue
                if not self.ledger.lock_report(mv, interval, window_key, now):
                    continue
                self._decide_interval(interval, mark, window_key, in_band, now)

    def _decide_interval(self, interval: str, mark: int, window_key: int,
                         cands: list[dict[str, Any]], now: float) -> None:
        cfg = self.config
        evaluated: list[dict[str, Any]] = []
        for cand in cands:
            verdict = gate.evaluate(cand, cfg)
            stale = _num(cand.get("spot_stale_age_seconds"))
            abstained_stale = False
            if verdict["fired"] and stale is not None and stale > cfg.max_spot_stale_seconds:
                # FRESHNESS: a would-be fire on a stale spot does not fire; record an
                # abstain row tagged STALE_FEED instead of alerting on a bad feed.
                verdict = dict(verdict)
                verdict["fired"] = False
                reasons = list(verdict.get("reason_codes") or [])
                if "STALE_FEED" not in reasons:
                    reasons.append("STALE_FEED")
                verdict["reason_codes"] = reasons
                abstained_stale = True
            evaluated.append({"cand": cand, "verdict": verdict, "stale": abstained_stale})

        # -- DELIVERED path (unchanged): the chosen candidate, NO-only alert.
        fired = [e for e in evaluated if e["verdict"]["fired"]]
        chosen = None
        if fired:
            chosen = max(fired, key=lambda e: _num(e["verdict"]["net_edge_cents"]) or -1e9)
        elif evaluated:
            chosen = max(evaluated, key=lambda e: _num(e["verdict"]["net_edge_cents"]) or -1e9)
        delivered_ticker = None
        if chosen is not None:
            delivered_ticker = str(chosen["cand"].get("ticker") or "")
            self._record_and_maybe_alert(chosen, interval, mark, window_key, now)

        # -- RESEARCH-YES path: record the best YES candidate so YES-prone windows
        # finally produce gradeable data. Never alerted; never claims the alert
        # lock; skipped when it's the same contract already recorded above (the
        # UNIQUE(model_version,ticker,interval) constraint would reject it anyway).
        if cfg.record_research_yes:
            yes = [e for e in evaluated
                   if str(e["cand"].get("predicted_side") or "").upper() == "YES"]
            if yes:
                best_yes = max(yes, key=lambda e: _num(e["verdict"]["net_edge_cents"]) or -1e9)
                if str(best_yes["cand"].get("ticker") or "") != delivered_ticker:
                    self._record_research_yes(best_yes, interval, mark, window_key, now)

    def _build_row(self, cand: dict[str, Any], verdict: dict[str, Any], interval: str,
                   mark: int, window_key: int, now: float, *, record_kind: str,
                   delivery_status: str) -> dict[str, Any]:
        cfg = self.config
        sel = _num(cand.get("selected_probability"))
        ask = _num(cand.get("entry_ask_cents"))
        cost = _num(cand.get("total_cost_cents")) or 0.0
        display = None
        if sel is not None and ask is not None:
            display = gate.display_entry(sel, cost, ask, cfg)
        regime_dir = _regime_directional(_num(cand.get("market_implied_yes_probability")))
        return {
            "created_at": now, "model_version": cfg.model_version,
            "asset": cand.get("asset"), "ticker": cand.get("ticker"),
            "interval": interval, "window_key": window_key, "mark_seconds": float(mark),
            "fired": 1 if verdict["fired"] else 0,
            "predicted_side": cand.get("predicted_side"),
            "selected_probability": cand.get("selected_probability"),
            "calibrated_yes_probability": cand.get("calibrated_yes_probability"),
            "conservative_probability": cand.get("conservative_probability"),
            "market_implied_yes_probability": cand.get("market_implied_yes_probability"),
            "raw_yes_probability": cand.get("raw_yes_probability"),
            "net_edge_cents": verdict.get("net_edge_cents"),
            "entry_ask_cents": cand.get("entry_ask_cents"),
            "best_entry_cents": display,
            "fee_cents": cand.get("fee_cents"),
            "total_cost_cents": cand.get("total_cost_cents"),
            "spread_cents": cand.get("spread_cents"),
            "depth_contracts": cand.get("depth_contracts"),
            "quote_age_seconds": cand.get("quote_age_seconds"),
            "spot_stale_age_seconds": cand.get("spot_stale_age_seconds"),
            "distance_sigma": cand.get("distance_sigma"),
            "regime_name": cand.get("regime_name"),
            "regime_directional": regime_dir,
            "data_quality": cand.get("data_quality"),
            "evidence_quality": cand.get("evidence_quality"),
            "manipulation_suspected": 1 if cand.get("manipulation_suspected") else 0,
            "flip_probability": cand.get("flip_probability"),
            "order_flow_persistence": cand.get("order_flow_persistence"),
            "book_resiliency": cand.get("book_resiliency"),
            "prediction_stability": cand.get("prediction_stability"),
            "gate_a_pass": 1 if verdict.get("gate_a") else 0,
            "gate_b_pass": 1 if verdict.get("gate_b") else 0,
            "gate_c_pass": 1 if verdict.get("gate_c") else 0,
            "reason_codes": ",".join(verdict.get("reason_codes") or []) or None,
            "gate_min_conf": cfg.min_confidence,
            "gate_ask_lo": cfg.ask_lo,
            "gate_ask_hi": cfg.ask_hi,
            "gate_min_edge": cfg.min_edge_cents,
            "close_time": cand.get("close_time"),
            "snapshot_id": cand.get("snapshot_id"),
            "session_id": self.session_id,
            "delivery_status": delivery_status,
            "record_kind": record_kind,
            "research_fired": 1 if verdict.get("research_fired") else 0,
            "_best_entry_cents": display,
        }

    def _record_and_maybe_alert(self, chosen: dict[str, Any], interval: str, mark: int,
                                window_key: int, now: float) -> None:
        cfg = self.config
        cand = chosen["cand"]
        verdict = chosen["verdict"]
        row = self._build_row(cand, verdict, interval, mark, window_key, now,
                              record_kind="DELIVERED_CANDIDATE", delivery_status="PENDING")
        display = row.pop("_best_entry_cents", None)
        row_id = self.ledger.record_decision(row)
        if row_id is None:
            return

        # ALERT: only a fired row, and only the first time this contract is alerted
        # in this window (one alert per contract per window, across checkpoints).
        if not verdict["fired"]:
            self.ledger.mark_delivery(row_id, "RECORDED", None, None)
            return
        ticker = str(cand.get("ticker") or "")
        if not self.ledger.claim_alert(cfg.model_version, ticker, window_key, now):
            self.ledger.mark_delivery(row_id, "RECORDED", None, "alert_already_sent")
            return
        alert_row = dict(row)
        alert_row["best_entry_cents"] = display
        summary = self._alert_summary()
        text = panel.build_entry_alert(alert_row, summary, cfg)
        result = self.telegram.send(text)
        if result.get("delivered"):
            status, mid = "SENT", result.get("message_id")
        elif result.get("muted"):
            status, mid = "MUTED", None
        else:
            status, mid = "DELIVERY_FAILED", None
        self.ledger.set_report_message(cfg.model_version, interval, window_key, mid)
        self.ledger.mark_delivery(row_id, status, mid, result.get("error"))

    def _record_research_yes(self, entry: dict[str, Any], interval: str, mark: int,
                             window_key: int, now: float) -> None:
        """Record a YES-side candidate as RESEARCH-ONLY: never delivered, never
        claims the alert lock, fired forced to 0 (delivery is NO-only). It exists
        solely so YES-prone windows accrue gradeable data for the validation
        module. ``research_fired`` carries the side-agnostic gate result."""
        cand = entry["cand"]
        verdict = dict(entry["verdict"])
        verdict["fired"] = False  # YES never delivers, regardless of gate_a
        row = self._build_row(cand, verdict, interval, mark, window_key, now,
                              record_kind="RESEARCH_YES", delivery_status="RESEARCH")
        row.pop("_best_entry_cents", None)
        self.ledger.record_decision(row)

    def _alert_summary(self) -> dict[str, Any]:
        """Compact scoreboard facts for the entry card's caveat — derived, never
        hardcoded, so the card stops claiming '0 YES-prone' once data exists."""
        cfg = self.config
        sb = self.ledger.scoreboard(cfg.model_version, min_n=cfg.min_scoreboard_n)
        regimes = sb.get("by_regime_directional") or {}
        n_regimes = sum(1 for r in regimes.values() if int((r or {}).get("n") or 0) > 0)
        yes_prone_n = int((regimes.get("YES_PRONE") or {}).get("n") or 0)
        return {
            "resolved": sb.get("resolved"),
            "n_regimes": n_regimes,
            "yes_prone_n": yes_prone_n,
        }

    # -- reconcile (settlement grading) --------------------------------------
    def reconcile(self, now: float, resolver: Any) -> None:
        """Enqueue a settlement reconcile, throttled. ``resolver`` is the shared
        Kalshi market cache (``get_market(ticker) -> market``). Never raises."""
        try:
            if now - self._last_reconcile_at < self.config.reconcile_every_seconds:
                return
            self._last_reconcile_at = now
            self._ensure_worker()
            self._jobs.put_nowait(("reconcile", {"resolver": resolver, "now": now}))
        except queue.Full:
            logger.debug("ultoim_v2 reconcile queue full; skipped")
        except Exception:  # noqa: BLE001 - never break the loop
            logger.warning("ultoim_v2 reconcile enqueue failed", exc_info=True)

    def _reconcile_sync(self, *, resolver: Any, now: float) -> None:
        get_market = getattr(resolver, "get_market", None)
        if not callable(get_market):
            return
        for ticker in self.ledger.unresolved_closed(self.config.model_version, now):
            try:
                market = get_market(ticker)
            except Exception:  # noqa: BLE001 - one bad fetch must not stop the pass
                logger.debug("ultoim_v2 resolve fetch failed for %s", ticker, exc_info=True)
                continue
            result = _resolved_result(market)
            if result is not None:
                self.ledger.resolve(self.config.model_version, ticker, result, now)

    # -- periodic research recap ----------------------------------------------
    def maybe_send_recap(self, now: float) -> None:
        """Enqueue a research recap, throttled by ``recap_every_seconds``. Never
        raises into the live loop."""
        try:
            if now - self._last_recap_at < self.config.recap_every_seconds:
                return
            self._last_recap_at = now
            self._ensure_worker()
            self._jobs.put_nowait(("recap", {"now": now}))
        except queue.Full:
            logger.debug("ultoim_v2 recap queue full; skipped")
        except Exception:  # noqa: BLE001 - never break the loop
            logger.warning("ultoim_v2 recap enqueue failed", exc_info=True)

    def _recap_sync(self, *, now: float) -> None:
        try:
            mv = self.config.model_version
            sb = self.ledger.scoreboard(mv, min_n=self.config.min_scoreboard_n)
            recent = self.ledger.recent_rows(mv, limit=10)
            losses = self.ledger.loss_rows(mv, limit=10)
            text = panel.build_recap(sb, recent, losses, self.config)
            self.telegram.send(text)
        except Exception:  # noqa: BLE001 - a recap must never raise
            logger.warning("ultoim_v2 recap build/send failed (ignored)", exc_info=True)

    # -- read-only status -----------------------------------------------------
    def scoreboard(self) -> dict[str, Any]:
        return self.ledger.scoreboard(
            self.config.model_version, min_n=self.config.min_scoreboard_n)


_runner: UltoimV2Runner | None = None
_runner_lock = threading.Lock()


def get_runner() -> UltoimV2Runner | None:
    """Singleton Ultoim V2 runner, or None when disabled. Safe to call every cycle."""
    if not is_enabled():
        return None
    global _runner
    if _runner is None:
        with _runner_lock:
            if _runner is None:
                try:
                    _runner = UltoimV2Runner(UltoimV2Config.from_env())
                except Exception:  # noqa: BLE001 - never break the loop on init failure
                    logger.exception("ultoim_v2 runner init failed; disabling")
                    return None
    return _runner


def reset_runner_for_tests() -> None:
    global _runner
    with _runner_lock:
        _runner = None
