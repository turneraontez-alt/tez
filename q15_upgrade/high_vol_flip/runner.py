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

_RULE_PRIORITY = {
    "HVF_HYPE_EARLY_BULLISH_FLIP": 700.0,
    "HVF_OWN_EARLY_FLIP": 680.0,
    "HVF_BTC_EARLY_FOLLOW_LAG": 650.0,
    "HVF_HYPE_BULLISH_FLASH": 520.0,
    "HVF_OWN_NO_FLASH": 500.0,
    "HVF_OWN_STRONG_SELECTED": 480.0,
    "HVF_BTC_FOLLOW_EXTREME": 460.0,
    "HVF_BTC_DIVERGENCE_ACCEL_WATCH": 430.0,
}


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
                rows: list[dict[str, Any]] = []
                for cand in in_band:
                    asset = str(cand.get("asset") or "").upper()
                    if asset == "BTC" or asset not in cfg.assets:
                        continue
                    decision = evaluate_rules(cand, btc, prev_btc, cfg)
                    if decision is None:
                        continue
                    rows.append(self._build_row(cand, decision, interval, wk, now))
                rows.sort(key=self._rank_row, reverse=True)
                remaining = cfg.max_alerts_per_window - self.ledger.alert_count(cfg.model_version, wk)
                if remaining > 0:
                    for row in rows[:remaining]:
                        self._record_and_send(row)
                if btc is not None:
                    existing = self._last_btc_by_window.get(wk)
                    if existing is None or existing.get("interval") != interval:
                        self._last_btc_by_window[wk] = {
                            "interval": interval,
                            "candidate": dict(btc),
                            "updated_at": now,
                        }

    @staticmethod
    def _rank_row(row: Mapping[str, Any]) -> float:
        rule = str(row.get("rule_code") or "")
        side = str(row.get("predicted_outcome") or "").upper()
        model_yes = row.get("model_yes_probability")
        try:
            yes_prob = float(model_yes) if model_yes is not None else 0.5
        except (TypeError, ValueError):
            yes_prob = 0.5
        side_prob = yes_prob if side == "YES" else 1.0 - yes_prob if side == "NO" else 0.5
        try:
            ask = float(row.get("selected_ask_cents"))
        except (TypeError, ValueError):
            ask = 100.0
        try:
            spread = float(row.get("spread_cents"))
        except (TypeError, ValueError):
            spread = 8.0
        early_price_bonus = max(0.0, 20.0 - abs(ask - 58.0))
        expensive_penalty = max(0.0, ask - 65.0) * 2.5
        return (
            _RULE_PRIORITY.get(rule, 0.0)
            + side_prob * 100.0
            + early_price_bonus
            - spread * 4.0
            - expensive_penalty
        )

    def _record_and_send(self, row: Mapping[str, Any]) -> None:
        row_id = self.ledger.record_alert(row)
        if row_id is None:
            return
        result = self.telegram.send(panel.build_alert(row))
        if result.get("delivered"):
            status, message_id = "SENT", result.get("message_id")
        elif result.get("muted"):
            status, message_id = "MUTED", None
        else:
            status, message_id = "DELIVERY_FAILED", None
        self.ledger.mark_delivery(row_id, status, message_id, result.get("error"))

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
