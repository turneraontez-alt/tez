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

import json
import logging
import queue
import threading
import time
from typing import Any, Mapping

from .. import shadow_factors as cross_asset
from . import fifteen_min, gate, panel, screen, validate
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


def _hiconv_yes_pass(cand: Mapping[str, Any], cfg: Any) -> bool:
    """Whether a YES candidate clears the high-conviction criteria (RECORD-ONLY marker).
    The live 10M data showed YES only pays when V2 AND the market both strongly say YES on a
    near-strike pin (cal_yes >= ~0.70 won 17/20=85%; the 3 losers all had market-YES < 0.60 or
    HIGH_VOLATILITY). So: high model conviction AND market agreement AND THRESHOLD_PIN. Pure
    observability — never gates delivery (YES is NO-only)."""
    if not getattr(cfg, "hiconv_yes_record", True):
        return False
    cal = _num(cand.get("calibrated_yes_probability"))
    mkt = _num(cand.get("market_implied_yes_probability"))
    regime = str(cand.get("regime_name") or "").upper()
    return (cal is not None and cal >= cfg.hiconv_yes_cal_min
            and mkt is not None and mkt >= cfg.hiconv_yes_market_min
            and regime == "THRESHOLD_PIN")


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
        # Debounce state for the defensive-exit warning, keyed by (ticker, window).
        # Mutated ONLY on the worker thread (in _observe_sync), so no lock needed.
        self._exit_state: dict[tuple[str, int], dict[str, float]] = {}

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
        # Broad-market cross-asset flow (measure-first; default OFF). Computed ONCE
        # per cycle from the analyses already in hand — never read by the gate, so
        # it cannot change a fire decision. None when disabled or unavailable.
        market_flow: float | None = None
        if self.config.record_xflow:
            try:
                market_flow = _num(cross_asset.compute_market(analyses or {}).get("market_flow"))
            except Exception:  # noqa: BLE001 - record-only side metric must never break observe
                logger.debug("ultoim_v2 market_flow compute failed (ignored)", exc_info=True)
        candidates: list[dict[str, Any]] = []
        for asset, analysis in (analyses or {}).items():
            # Per-asset isolation: a single malformed analysis/canonical must NEVER drop
            # the whole cycle (honours this method's "never raises" contract in-module,
            # not just via the call-site catch).
            try:
                cand = self._extract_candidate(
                    asset, analysis, (canonicals or {}).get(asset), market_flow)
            except Exception:  # noqa: BLE001 - skip the bad asset, keep the rest of the cycle
                logger.debug("ultoim_v2 candidate extraction failed for %s (skipped)",
                             asset, exc_info=True)
                continue
            if cand is not None:
                candidates.append(cand)
        if not candidates:
            return
        try:
            self._ensure_worker()
            self._jobs.put_nowait(("observe", {"candidates": candidates, "now": now}))
        except queue.Full:
            logger.debug("ultoim_v2 observe queue full; cycle dropped")

    def _extract_candidate(self, asset: Any, analysis: Mapping[str, Any], canonical: Any,
                           market_flow: float | None) -> dict[str, Any] | None:
        """Pull one candidate's compact, race-free fields from the (read-only) analysis /
        canonical. Returns the candidate dict, or None to skip (missing canonical, not in
        a window yet, or no prediction available). Pure extraction — never gates; raises
        only on a genuinely malformed analysis, which ``observe`` isolates per-asset."""
        if canonical is None or not isinstance(analysis, Mapping):
            return None
        secs = _num(getattr(canonical, "seconds_remaining", None))
        ticker = getattr(canonical, "ticker", None)
        if not ticker or secs is None:
            return None
        if not analysis.get("prediction_available"):
            return None
        quote = analysis.get("quote") or {}
        signals = analysis.get("shadow_signals") or {}
        flip = analysis.get("flip_risk") or {}
        manip = analysis.get("manipulation") or {}
        costs = analysis.get("costs") or {}
        regime = analysis.get("regime") or {}
        # Pin-break shadow features (measure-first; never read by the gate):
        #  * pin_break_drift = the structural z_score — vol-normalised drift
        #    THROUGH the strike; the one signal that keeps directional content
        #    when distance_sigma -> 0 (the strike-pin regime where the losses live).
        #  * threshold_interaction = the champion's scalar threshold feature (the
        #    "failed-break" family); recorded record-only and validated later.
        structural = analysis.get("structural") or {}
        feature_values = analysis.get("feature_values") or {}
        close_time = getattr(canonical, "settlement_time", None)
        total_cost = costs.get("total_cost_cents")
        if total_cost is None:
            total_cost = costs.get("total_cents")
        return {
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
            "x_market_flow": market_flow,
            "pin_break_drift": structural.get("z_score"),
            "threshold_interaction": feature_values.get("threshold_interaction"),
            # Champion per-asset directional flow factor — the flow-against-NO abstain
            # candidate (record-only; see ledger.flow_research_scoreboard). Never gates.
            "champion_flow": feature_values.get("flow"),
            "snapshot_id": analysis.get("snapshot_id"),
        }

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
                # Skip the weak/money-losing 15M bin when configured (default OFF).
                # 10M/7M behaviour is untouched.
                if cfg.skip_15m and interval == "15M":
                    continue
                # Skip the 7M bin when configured (owner: 7M ran net-negative on a day; it is
                # ~break-even overall). Default OFF -> byte-identical. Exits are unaffected.
                if getattr(cfg, "skip_7m", False) and interval == "7M":
                    continue
                # 11M/12M are MEASURE-FIRST marks: inert unless explicitly enabled, so the
                # added INTERVAL_MARKS entries are byte-identical no-ops by default.
                if interval == "11M" and not cfg.enable_11m:
                    continue
                if interval == "12M" and not cfg.enable_12m:
                    continue
                in_band = [c for c in cands
                           if (mark - cfg.mark_band_seconds) <= c["seconds_remaining"] <= mark]
                if not in_band:
                    continue
                if self.ledger.report_locked(mv, interval, window_key):
                    continue
                if not self.ledger.lock_report(mv, interval, window_key, now):
                    continue
                self._decide_interval(interval, mark, window_key, in_band, now)

        # -- DEFENSIVE-EXIT pass: independent of the entry report-lock, runs every
        # cycle for contracts near close so the anti-spike debounce can accumulate.
        if cfg.exit_warnings_enabled:
            self._prune_exit_state(now)
            for cand in candidates:
                self._maybe_exit_warning(cand, _window_key(cand.get("close_time"), now), now)

    def _decide_interval(self, interval: str, mark: int, window_key: int,
                         cands: list[dict[str, Any]], now: float) -> None:
        cfg = self.config
        research_only = interval in cfg.research_only_intervals
        # 12M promotion: when deliver_12m is on, 12M leaves research-only and delivers like
        # 10M/7M (the gate then suppresses the cheap ask<floor_12m_ask slice via ASK_FLOOR_12M).
        if interval == "12M" and getattr(cfg, "deliver_12m", False):
            research_only = False
        evaluated: list[dict[str, Any]] = []
        for cand in cands:
            verdict = gate.evaluate(cand, cfg, interval=interval)
            stale = _num(cand.get("spot_stale_age_seconds"))
            abstained_stale = False
            # FRESHNESS: a would-be fire on a stale spot does not fire. Default is fail-OPEN —
            # an UNKNOWN staleness (None) is treated as fresh. With stale_fail_closed (gated,
            # default OFF) a None/missing age is treated as STALE so the live path never fires
            # blind to feed freshness.
            too_stale = stale is not None and stale > cfg.max_spot_stale_seconds
            unknown_stale = stale is None and getattr(cfg, "stale_fail_closed", False)
            if verdict["fired"] and (too_stale or unknown_stale):
                # record an abstain row tagged STALE_FEED instead of alerting on a bad feed.
                verdict = dict(verdict)
                verdict["fired"] = False
                reasons = list(verdict.get("reason_codes") or [])
                if "STALE_FEED" not in reasons:
                    reasons.append("STALE_FEED")
                verdict["reason_codes"] = reasons
                abstained_stale = True
            evaluated.append({"cand": cand, "verdict": verdict, "stale": abstained_stale})

        # -- CONVICTION rules, keyed on how many assets would FIRE a NO at this
        # (interval, window) THIS cycle (qualifiers). The count is known right now — all
        # co-settling assets are in-band together — so there is no look-ahead, and it is
        # independent of deliver_top_n. See config (skip_12m_unless_min / double_10m_on_min).
        qualifiers = sum(1 for e in evaluated if e["verdict"]["fired"])
        conviction_reason: str | None = None
        # Rule A: SKIP 12M delivery unless >=min assets co-trigger. Below the threshold the
        # 12M picks are downgraded to research (recorded + graded, but never alerted, fired,
        # or executed). Only meaningful when 12M is in delivery mode (deliver_12m on).
        if (interval == "12M" and not research_only and cfg.skip_12m_unless_min
                and qualifiers < cfg.min_triggers_12m):
            research_only = True
            conviction_reason = "SKIP_12M_UNDER_MIN"
        # Rule B: conviction-DOUBLE the 10M stake when >=min assets co-trigger. This is
        # leverage on the COUNT, not on correctness — it will sometimes double a losing
        # window (a market-wide YES sweep). Owner-chosen default-ON; staked P&L is tracked.
        stake = 1
        if (interval == "10M" and cfg.double_10m_on_min
                and qualifiers >= cfg.min_triggers_10m):
            stake = cfg.double_stake

        # -- MEASURE-FIRST path (11M/12M): record the would-FIRE favourites but NEVER
        # deliver. We force fired=False (no alert, no alert-lock claim) while leaving
        # research_fired intact so the row grades on settlement. We pick the would-fire
        # picks by the SAME top-N reward:risk rule as delivery -- NOT the max-net_edge one,
        # because net_edge is INVERSE for NO (the max-edge candidate is the losing longshot,
        # not the favourite we want to measure). Returns early: these marks never reach the
        # delivered or research-YES paths.
        if research_only:
            would = [e for e in evaluated if e["verdict"]["fired"]]
            if would:
                n = max(1, int(getattr(cfg, "deliver_top_n", 1) or 1))
                if getattr(cfg, "deliver_by_reward_risk", False):
                    ordered = sorted(would, key=lambda e: (
                        _num(e["cand"].get("entry_ask_cents"))
                        if _num(e["cand"].get("entry_ask_cents")) is not None else 1e9,
                        -(_num(e["verdict"]["net_edge_cents"]) or -1e9)))
                else:
                    ordered = sorted(
                        would, key=lambda e: -(_num(e["verdict"]["net_edge_cents"]) or -1e9))
                chosen = ordered[:n]
            elif evaluated:
                # nothing would fire -> record the single best for research (unchanged idiom).
                chosen = [max(evaluated,
                              key=lambda e: _num(e["verdict"]["net_edge_cents"]) or -1e9)]
            else:
                chosen = []
            for e in chosen:
                v = dict(e["verdict"])
                v["fired"] = False
                rc = list(v.get("reason_codes") or [])
                if "RESEARCH_ONLY_MARK" not in rc:
                    rc.append("RESEARCH_ONLY_MARK")
                if conviction_reason and conviction_reason not in rc:
                    rc.append(conviction_reason)
                v["reason_codes"] = rc
                self._record_and_maybe_alert({**e, "verdict": v}, interval, mark, window_key, now)
            return

        # -- DELIVERED path: the top-N candidates per (interval, window), NO-only alerts.
        # Default (deliver_top_n=1, by net-edge) is byte-identical to the prior single-
        # best rule. With deliver_top_n>1 / deliver_by_reward_risk it delivers the N best
        # by REWARD:RISK (cheapest NO ask = best payoff per $), which the settled record
        # found beats the single max-edge pick. One alert per CONTRACT per window still
        # holds (the alert lock); N>1 just widens how many of the co-settling assets fire.
        fired = [e for e in evaluated if e["verdict"]["fired"]]
        delivered_tickers: set[str] = set()
        if fired:
            n = max(1, int(getattr(cfg, "deliver_top_n", 1) or 1))
            if getattr(cfg, "deliver_by_reward_risk", False):
                # cheapest ask first (best reward:risk); net-edge breaks ties / missing ask.
                ordered = sorted(fired, key=lambda e: (
                    _num(e["cand"].get("entry_ask_cents"))
                    if _num(e["cand"].get("entry_ask_cents")) is not None else 1e9,
                    -(_num(e["verdict"]["net_edge_cents"]) or -1e9)))
            else:
                ordered = sorted(
                    fired, key=lambda e: -(_num(e["verdict"]["net_edge_cents"]) or -1e9))
            for e in ordered[:n]:
                delivered_tickers.add(str(e["cand"].get("ticker") or ""))
                self._record_and_maybe_alert({**e, "stake_multiplier": stake},
                                             interval, mark, window_key, now)
        elif evaluated:
            # nothing fired -> record the single best for research (unchanged).
            best = max(evaluated, key=lambda e: _num(e["verdict"]["net_edge_cents"]) or -1e9)
            delivered_tickers.add(str(best["cand"].get("ticker") or ""))
            self._record_and_maybe_alert(best, interval, mark, window_key, now)

        # -- RESEARCH-YES path: record the best YES candidate so YES-prone windows
        # finally produce gradeable data. Never alerted; never claims the alert
        # lock; skipped when it's the same contract already recorded above (the
        # UNIQUE(model_version,ticker,interval) constraint would reject it anyway).
        if cfg.record_research_yes:
            yes = [e for e in evaluated
                   if str(e["cand"].get("predicted_side") or "").upper() == "YES"]
            if yes:
                best_yes = max(yes, key=lambda e: _num(e["verdict"]["net_edge_cents"]) or -1e9)
                if str(best_yes["cand"].get("ticker") or "") not in delivered_tickers:
                    self._record_research_yes(best_yes, interval, mark, window_key, now)

    def _build_row(self, cand: dict[str, Any], verdict: dict[str, Any], interval: str,
                   mark: int, window_key: int, now: float, *, record_kind: str,
                   delivery_status: str, stake_multiplier: int = 1) -> dict[str, Any]:
        cfg = self.config
        sel = _num(cand.get("selected_probability"))
        ask = _num(cand.get("entry_ask_cents"))
        cost = _num(cand.get("total_cost_cents")) or 0.0
        display = None
        if sel is not None and ask is not None:
            display = gate.display_entry(sel, cost, ask, cfg,
                                         expensive_no=bool(verdict.get("expensive_no")))
        regime_dir = _regime_directional(_num(cand.get("market_implied_yes_probability")))
        # Record-only blowup SHADOW score, derived from decision-time fields already
        # on this candidate. Stamped on every recorded row; NEVER gates fire/size/alert
        # (record-only — see screen.py). Inputs come from ``cand`` so it stays pure.
        shadow = screen.shadow_features(cand)
        # Record-only 15M selective-entry SCREEN verdict (see fifteen_min.py). Stamped
        # on every row; all-None outside 15M-NO; NEVER read by the gate / fire path.
        s15 = fifteen_min.features(cand, interval, cfg)
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
            "x_market_flow": cand.get("x_market_flow"),
            "pin_break_drift": cand.get("pin_break_drift"),
            "threshold_interaction": cand.get("threshold_interaction"),
            "champion_flow": cand.get("champion_flow"),
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
            "conf_gap": shadow["conf_gap"],
            "blowup_risk": shadow["blowup_risk"],
            "screen_version": shadow["screen_version"],
            "s15_pass": s15["s15_pass"],
            "s15_codes": s15["s15_codes"],
            "s15_cal_drift": s15["s15_cal_drift"],
            "s15_version": s15["s15_version"],
            "stake_multiplier": int(stake_multiplier),
            "_best_entry_cents": display,
        }

    def _record_and_maybe_alert(self, chosen: dict[str, Any], interval: str, mark: int,
                                window_key: int, now: float) -> None:
        cfg = self.config
        cand = chosen["cand"]
        verdict = chosen["verdict"]
        stake = int(chosen.get("stake_multiplier", 1) or 1)
        row = self._build_row(cand, verdict, interval, mark, window_key, now,
                              record_kind="DELIVERED_CANDIDATE", delivery_status="PENDING",
                              stake_multiplier=stake)
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
        # OPT-IN EXECUTOR (default-OFF): hand this confirmed, fired NO delivery to the
        # live-order layer. get_executor() returns None unless Q15_EXEC_ENABLED is set, so
        # this is a byte-identical no-op by default; even when enabled it is dry-run by
        # default. v2 NEVER places an order itself — it only emits the pick. Failures here
        # are swallowed so the executor can never disrupt the read-only paper path.
        self._maybe_execute(cand, display, window_key, interval, now, stake_multiplier=stake)

    def _maybe_execute(self, cand: Mapping[str, Any], best_entry_cents: Any,
                       window_key: int, interval: str, now: float,
                       *, stake_multiplier: int = 1) -> None:
        try:
            from q15_upgrade.executor import get_executor
            ex = get_executor()
            if ex is None:
                return
            ex.on_fire({
                "ticker": cand.get("ticker"),
                "asset": cand.get("asset"),
                "predicted_side": cand.get("predicted_side"),
                "best_entry_cents": best_entry_cents,
                "entry_ask_cents": cand.get("entry_ask_cents"),
                "window_key": window_key,
                "interval": interval,   # backstops the executor's optional interval allowlist
                "fired_at": now,        # cycle wall-clock -> executor measures snapshot->order age
                "stake_multiplier": stake_multiplier,  # conviction size hint (2x on >=3 10M)
            })
        except Exception:  # never let execution disrupt the paper system
            logger.exception("executor on_fire hook failed (non-fatal)")

    def _record_research_yes(self, entry: dict[str, Any], interval: str, mark: int,
                             window_key: int, now: float) -> None:
        """Record a YES-side candidate as RESEARCH-ONLY: never delivered, never
        claims the alert lock, fired forced to 0 (delivery is NO-only). It exists
        solely so YES-prone windows accrue gradeable data for the validation
        module. ``research_fired`` carries the side-agnostic gate result."""
        cand = entry["cand"]
        verdict = dict(entry["verdict"])
        verdict["fired"] = False  # YES never delivers, regardless of gate_a
        # RECORD-ONLY high-conviction-YES marker: tag the rows that clear the (cal_yes / market-YES
        # / THRESHOLD_PIN) criteria so the would-deliver YES sliver accrues a gradeable, out-of-
        # sample record. Never changes delivery — YES stays NO-only.
        if _hiconv_yes_pass(cand, self.config):
            rc = list(verdict.get("reason_codes") or [])
            if "HICONV_YES" not in rc:
                rc.append("HICONV_YES")
            verdict["reason_codes"] = rc
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

    # -- defensive-exit / flip warning ----------------------------------------
    def _prune_exit_state(self, now: float) -> None:
        """Drop debounce keys older than two windows so the dict can't leak."""
        cutoff = now - 1800.0
        stale = [k for k, st in self._exit_state.items() if st["first_seen"] < cutoff]
        for k in stale:
            self._exit_state.pop(k, None)

    def _maybe_exit_warning(self, cand: dict[str, Any], window_key: int, now: float) -> None:
        """Fire a defensive-exit warning iff (A) a paper entry was SUGGESTED earlier
        in this window for this contract, AND (B) at/after the 7M mark the champion
        has FLIPPED to the opposite side, decisively (new-side prob >= min) AND
        sustained (>= confirm_cycles observations spanning >= confirm_seconds) — so a
        short-lived spike never triggers. One warning per (ticker, window)."""
        cfg = self.config
        secs = _num(cand.get("seconds_remaining"))
        if secs is None or secs > cfg.exit_watch_from_seconds:
            return  # only watch from the 7M mark (default 420s) onward to close
        ticker = str(cand.get("ticker") or "")
        if not ticker:
            return
        key = (ticker, window_key)
        if self.ledger.exit_warning_recorded(cfg.model_version, ticker, window_key):
            self._exit_state.pop(key, None)
            return
        entry = self.ledger.find_fired_entry(cfg.model_version, ticker, window_key)
        if entry is None:
            return  # only warn on a contract it actually suggested
        entry_side = str(entry.get("predicted_side") or "").upper()
        cur_side = str(cand.get("predicted_side") or "").upper()
        new_sel = _num(cand.get("selected_probability"))
        opposite = bool(cur_side) and bool(entry_side) and cur_side != entry_side
        decisive = new_sel is not None and new_sel >= cfg.exit_min_flip_conf
        if not (opposite and decisive):
            self._exit_state.pop(key, None)  # flip not real (yet) -> reset debounce
            return
        st = self._exit_state.get(key)
        if st is None:
            st = {"count": 0.0, "first_seen": now}
            self._exit_state[key] = st
        st["count"] += 1.0
        if st["count"] < cfg.exit_confirm_cycles or (now - st["first_seen"]) < cfg.exit_confirm_seconds:
            return  # anti-spike: not sustained long enough yet
        self._fire_exit_warning(cand, entry, window_key, now, st)
        self._exit_state.pop(key, None)

    def _fire_exit_warning(self, cand: dict[str, Any], entry: Mapping[str, Any],
                           window_key: int, now: float, st: Mapping[str, float]) -> None:
        cfg = self.config
        entry_side = str(entry.get("predicted_side") or "").upper()
        mkt_yes = _num(cand.get("market_implied_yes_probability"))
        # Sell-to-close value of the ENTRY side, estimated from the current
        # market-implied probability of that side (approximate — the real fill is
        # the live bid; this is the honest best estimate from available fields).
        exit_value = None
        if mkt_yes is not None:
            p_entry = (1.0 - mkt_yes) if entry_side == "NO" else mkt_yes
            exit_value = round(max(0.0, min(100.0, p_entry * 100.0)), 1)
        span = round(now - float(st["first_seen"]), 1)
        cycles = int(st["count"])
        evidence = {
            "entry_side": entry_side,
            "entry_ask_cents": entry.get("entry_ask_cents"),
            "entry_interval": entry.get("interval"),
            "flip_calibrated_yes": cand.get("calibrated_yes_probability"),
            "flip_selected_probability": cand.get("selected_probability"),
            "market_implied_yes": mkt_yes,
            "confirm_cycles": cycles,
            "confirm_span_seconds": span,
            "distance_sigma": cand.get("distance_sigma"),
            "manipulation_suspected": bool(cand.get("manipulation_suspected")),
            "flip_probability": cand.get("flip_probability"),
        }
        row = {
            "created_at": now, "model_version": cfg.model_version,
            "asset": cand.get("asset"), "ticker": cand.get("ticker"),
            "window_key": window_key,
            "entry_side": entry_side,
            "entry_ask_cents": entry.get("entry_ask_cents"),
            "entry_interval": entry.get("interval"),
            "entry_created_at": entry.get("created_at"),
            "warn_seconds_remaining": _num(cand.get("seconds_remaining")),
            "flipped_to_side": str(cand.get("predicted_side") or "").upper(),
            "flip_calibrated_yes": cand.get("calibrated_yes_probability"),
            "flip_selected_probability": cand.get("selected_probability"),
            "flip_market_implied_yes": mkt_yes,
            "confirm_cycles": cycles,
            "confirm_span_seconds": span,
            "exit_value_cents": exit_value,
            "evidence_json": json.dumps(evidence, default=str),
            "close_time": cand.get("close_time"),
            "session_id": self.session_id,
            "delivery_status": "PENDING",
        }
        warning_id = self.ledger.record_exit_warning(row)
        if warning_id is None:
            return  # already recorded (dedup / concurrent)
        sb = self.ledger.exit_warning_scoreboard(cfg.model_version, min_n=cfg.min_scoreboard_n)
        text = panel.build_exit_warning(row, sb, cfg)
        result = self.telegram.send(text)
        if result.get("delivered"):
            status, mid = "SENT", result.get("message_id")
        elif result.get("muted"):
            status, mid = "MUTED", None
        else:
            status, mid = "DELIVERY_FAILED", None
        self.ledger.mark_exit_delivery(warning_id, status, mid, result.get("error"))
        # OPT-IN EXECUTOR (default-OFF): on a confirmed flip warning, tell the live-order
        # layer to SELL the position (it no-ops if disabled / holds no position).
        try:
            from q15_upgrade.executor import get_executor
            ex = get_executor()
            if ex is not None and exit_value is not None:
                ex.on_exit(str(cand.get("ticker") or ""), window_key, int(round(exit_value)))
        except Exception:
            logger.exception("executor on_exit hook failed (non-fatal)")

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
        mv = self.config.model_version
        tickers = set(self.ledger.unresolved_closed(mv, now))
        tickers.update(self.ledger.unresolved_exit_warnings(mv, now))
        for ticker in tickers:
            try:
                market = get_market(ticker)
            except Exception:  # noqa: BLE001 - one bad fetch must not stop the pass
                logger.debug("ultoim_v2 resolve fetch failed for %s", ticker, exc_info=True)
                continue
            result = _resolved_result(market)
            if result is not None:
                self.ledger.resolve(mv, ticker, result, now)
                # Grade the exit warning too: was bailing the right call (entry lost)
                # or a false alarm (entry would have won)? This is how it learns.
                self.ledger.resolve_exit_warning(mv, ticker, result, now)

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
            sb["exit_warnings"] = self.ledger.exit_warning_scoreboard(
                mv, min_n=self.config.min_scoreboard_n)
            # Record-only blowup SHADOW diagnostics (never gates anything).
            sb["blowup_shadow"] = validate.screen_shadow_report(
                self.ledger.resolved_rows(mv), min_promote_n=self.config.min_promote_n)
            # Record-only research screens, surfaced for prospective measurement so the
            # would-fire edges accrue VISIBLY (never affect which entries fire): the 15M
            # selective screen and the transporting distance-to-strike feature.
            sb["s15_research"] = self.ledger.s15_research_scoreboard(mv)
            sb["distance_research"] = self.ledger.distance_research_scoreboard(
                mv, pin_sigma=self.config.distance_pin_sigma)
            sb["flow_research"] = self.ledger.flow_research_scoreboard(
                mv, flow_threshold=self.config.flow_against_no_threshold)
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
