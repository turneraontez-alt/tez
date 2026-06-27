"""High Volatility Flip paper-alert runner."""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Mapping, Sequence

from . import panel
from .config import HighVolFlipConfig, INTERVAL_MARKS, window_key
from .ledger import HighVolFlipLedger, kalshi_fee_cents
from .rules import evaluate_rules, extract_candidate
from .telegram import HighVolFlipTelegram

logger = logging.getLogger("high_vol_flip.runner")


def _resolved_result(market: Any) -> str | None:
    if not isinstance(market, Mapping):
        return None
    result = str(market.get("result") or market.get("official_result") or "").upper()
    return result if result in {"YES", "NO"} else None


class HighVolFlipRunner:
    def __init__(self, config: HighVolFlipConfig) -> None:
        self.config = config
        self.ledger = HighVolFlipLedger(config.db_path)
        self.telegram = HighVolFlipTelegram(
            config.telegram_chat_id,
            enabled=config.telegram_enabled,
        )
        self._jobs: "queue.Queue[tuple[str, dict[str, Any]]]" = queue.Queue(maxsize=512)
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._last_reconcile_at = 0.0
        self._last_btc_by_window: dict[int, dict[str, Any]] = {}

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._worker_loop, name="high-vol-flip", daemon=True
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
            except Exception:
                logger.exception("high_vol_flip worker job failed (ignored)")
            finally:
                self._jobs.task_done()

    def observe(self, *, analyses: Mapping[str, Mapping[str, Any]],
                canonicals: Mapping[str, Any], now: float) -> None:
        """Extract compact candidates and enqueue rule evaluation. Never raises."""
        cfg = self.config
        if not cfg.enabled:
            return
        candidates: list[dict[str, Any]] = []
        for asset, analysis in (analyses or {}).items():
            asset_key = str(asset or "").upper()
            if asset_key != "BTC" and asset_key not in cfg.assets:
                continue
            try:
                cand = extract_candidate(asset_key, analysis, (canonicals or {}).get(asset))
            except Exception:
                logger.debug("HVF candidate extraction failed for %s", asset_key, exc_info=True)
                continue
            if cand is None:
                continue
            dq = cand.get("data_quality")
            if dq is not None and float(dq) < cfg.min_data_quality:
                continue
            candidates.append(cand)
        if not candidates:
            return
        try:
            self._ensure_worker()
            self._jobs.put_nowait(("observe", {"candidates": candidates, "now": now}))
        except queue.Full:
            logger.debug("high_vol_flip observe queue full; cycle dropped")
        except Exception:
            logger.warning("high_vol_flip observe enqueue failed", exc_info=True)

    def _observe_sync(self, *, candidates: list[dict[str, Any]], now: float) -> None:
        cfg = self.config
        windows: dict[int, list[dict[str, Any]]] = {}
        for cand in candidates:
            wk = window_key(cand.get("close_time"), now)
            windows.setdefault(wk, []).append(cand)
        self._prune_btc_state(now)
        for wk, cands in windows.items():
            for interval, mark in INTERVAL_MARKS.items():
                if interval not in cfg.intervals:
                    continue
                in_band = [
                    c for c in cands
                    if (mark - cfg.mark_band_seconds) <= float(c["seconds_remaining"]) <= mark
                ]
                if not in_band:
                    continue
                btc = next((c for c in in_band if c.get("asset") == "BTC"), None)
                prev_rec = self._last_btc_by_window.get(wk)
                prev_btc = None
                if prev_rec is not None and prev_rec.get("interval") != interval:
                    prev_btc = prev_rec.get("candidate")
                for cand in in_band:
                    asset = str(cand.get("asset") or "").upper()
                    if asset == "BTC" or asset not in cfg.assets:
                        continue
                    decision = evaluate_rules(cand, btc, prev_btc, cfg)
                    if decision is None:
                        continue
                    row = self._build_row(cand, decision, interval, wk, now)
                    row_id = self.ledger.record_alert(row)
                    if row_id is None:
                        continue
                    result = self.telegram.send(panel.build_alert(row))
                    if result.get("delivered"):
                        status, message_id = "SENT", result.get("message_id")
                    elif result.get("muted"):
                        status, message_id = "MUTED", None
                    else:
                        status, message_id = "DELIVERY_FAILED", None
                    self.ledger.mark_delivery(row_id, status, message_id, result.get("error"))
                if btc is not None:
                    existing = self._last_btc_by_window.get(wk)
                    if existing is None or existing.get("interval") != interval:
                        self._last_btc_by_window[wk] = {
                            "interval": interval,
                            "candidate": dict(btc),
                            "updated_at": now,
                        }

    def _build_row(self, cand: Mapping[str, Any], decision: Mapping[str, Any],
                   interval: str, wk: int, now: float) -> dict[str, Any]:
        entry_ask = decision.get("entry_ask_cents")
        reason = str(decision.get("reason_code") or "")
        return {
            "created_at": now,
            "model_version": self.config.model_version,
            "record_kind": "HIGH_VOL_FLIP_ALERT",
            "asset": cand.get("asset"),
            "ticker": cand.get("ticker"),
            "interval": interval,
            "window_key": wk,
            "close_time": cand.get("close_time"),
            "seconds_remaining": cand.get("seconds_remaining"),
            "predicted_outcome": decision.get("predicted_outcome"),
            "model_predicted_side": cand.get("predicted_side"),
            "rule_code": reason,
            "rule_name": decision.get("rule"),
            "reason_codes": reason,
            "selected_side": decision.get("selected_side"),
            "selected_bid_cents": decision.get("selected_bid_cents"),
            "selected_ask_cents": decision.get("selected_ask_cents"),
            "yes_bid_cents": cand.get("yes_bid_cents"),
            "yes_ask_cents": cand.get("yes_ask_cents"),
            "no_bid_cents": cand.get("no_bid_cents"),
            "no_ask_cents": cand.get("no_ask_cents"),
            "spread_cents": cand.get("spread_cents"),
            "depth_contracts": cand.get("depth_contracts"),
            "model_yes_probability": cand.get("model_yes_probability"),
            "raw_yes_probability": cand.get("raw_yes_probability"),
            "calibrated_yes_probability": cand.get("calibrated_yes_probability"),
            "conservative_probability": cand.get("conservative_probability"),
            "data_quality": cand.get("data_quality"),
            "btc_ticker": decision.get("btc_ticker"),
            "btc_dominant_side": decision.get("btc_dominant_side"),
            "btc_dominant_mid_cents": decision.get("btc_dominant_mid_cents"),
            "btc_selected_bid_cents": decision.get("btc_selected_bid_cents"),
            "btc_selected_ask_cents": decision.get("btc_selected_ask_cents"),
            "btc_yes_mid_cents": decision.get("btc_yes_mid_cents"),
            "btc_no_mid_cents": decision.get("btc_no_mid_cents"),
            "btc_jump_cents": decision.get("btc_jump_cents"),
            "entry_ask_cents": entry_ask,
            "entry_fee_cents": kalshi_fee_cents(entry_ask),
            "paper_only": True,
            "delivery_status": "PENDING",
        }

    def _prune_btc_state(self, now: float) -> None:
        cutoff = now - 7200.0
        stale = [wk for wk, rec in self._last_btc_by_window.items()
                 if float(rec.get("updated_at") or 0.0) < cutoff]
        for wk in stale:
            self._last_btc_by_window.pop(wk, None)

    def resolve_settled(self, result_events: Sequence[Mapping[str, Any]] | None,
                        now: float) -> int:
        if not self.config.enabled or not result_events:
            return 0
        total = 0
        for ev in result_events:
            if not isinstance(ev, Mapping):
                continue
            ticker = ev.get("ticker") or ev.get("contract")
            result = ev.get("result") or ev.get("official_result")
            if ticker and result:
                total += self.ledger.resolve(
                    self.config.model_version, str(ticker), str(result), now
                )
        return total

    def reconcile(self, now: float, resolver: Any) -> None:
        try:
            if now - self._last_reconcile_at < self.config.reconcile_every_seconds:
                return
            self._last_reconcile_at = now
            self._ensure_worker()
            self._jobs.put_nowait(("reconcile", {"resolver": resolver, "now": now}))
        except queue.Full:
            logger.debug("high_vol_flip reconcile queue full; skipped")
        except Exception:
            logger.warning("high_vol_flip reconcile enqueue failed", exc_info=True)

    def _reconcile_sync(self, *, resolver: Any, now: float) -> None:
        get_market = getattr(resolver, "get_market", None)
        if not callable(get_market):
            return
        for ticker in self.ledger.unresolved_closed(self.config.model_version, now):
            try:
                market = get_market(ticker)
            except Exception:
                logger.debug("HVF resolve fetch failed for %s", ticker, exc_info=True)
                continue
            result = _resolved_result(market)
            if result is not None:
                self.ledger.resolve(self.config.model_version, ticker, result, now)

    def scoreboard(self) -> dict[str, Any]:
        return self.ledger.scoreboard(self.config.model_version)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "model_version": self.config.model_version,
            "paper_only": True,
            "telegram": self.telegram.status(),
            "assets": sorted(self.config.assets),
            "intervals": sorted(self.config.intervals),
        }


_runner: HighVolFlipRunner | None = None
_runner_lock = threading.Lock()


def get_runner() -> HighVolFlipRunner | None:
    global _runner
    if _runner is not None:
        return _runner
    with _runner_lock:
        if _runner is not None:
            return _runner
        cfg = HighVolFlipConfig.from_env()
        if not cfg.enabled:
            return None
        _runner = HighVolFlipRunner(cfg)
        return _runner


def reset_runner() -> None:
    global _runner
    _runner = None
