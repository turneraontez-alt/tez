"""High Volatility Flip paper-alert runner."""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Mapping, Sequence

from ..strategy_bots import runtime as strategy_bots_runtime
from . import panel
from .config import HighVolFlipConfig, INTERVAL_MARKS, window_key
from .ledger import HighVolFlipLedger, kalshi_fee_cents
from .rules import (
    RECORD_EARLY_FLIP_WATCH,
    evaluate_rules,
    extract_candidate,
    selected_depth_ratio,
)
from .telegram import HighVolFlipTelegram

logger = logging.getLogger("high_vol_flip.runner")

_RULE_PRIORITY = {
    "HVF_MORE_FIRE_STRICT": 760.0,
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


def _v3_owns_bnb_notification(config: Any, row: Mapping[str, Any]) -> bool:
    return (
        bool(getattr(config, "suppress_bnb_telegram_for_v3", False))
        and str(row.get("asset") or "").upper() == "BNB"
    )


class HighVolFlipRunner:
    def __init__(self, config: HighVolFlipConfig) -> None:
        self.config = config
        self.ledger = HighVolFlipLedger(config.db_path)
        self.telegram = HighVolFlipTelegram(
            config.telegram_chat_id,
            enabled=config.telegram_enabled and config.alert_telegram_enabled,
        )
        self._jobs: "queue.Queue[tuple[str, dict[str, Any]]]" = queue.Queue(maxsize=512)
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._last_reconcile_at = 0.0
        self._last_btc_by_window: dict[int, dict[str, Any]] = {}
        self._candidate_history_by_window: dict[int, dict[str, dict[str, dict[str, Any]]]] = {}

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
        excluded_assets = getattr(cfg, "excluded_assets", frozenset())
        for asset, analysis in (analyses or {}).items():
            asset_key = str(asset or "").upper()
            if asset_key != "BTC" and (asset_key not in cfg.assets or asset_key in excluded_assets):
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
            self._remember_interval_candidates(wk, cands, now)
            for interval, mark in self._active_interval_marks():
                if interval == "13M":
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
                watch_rows: list[dict[str, Any]] = []
                for cand in in_band:
                    asset = str(cand.get("asset") or "").upper()
                    if asset == "BTC" or asset not in cfg.assets or asset in cfg.excluded_assets:
                        continue
                    prev_asset = self._previous_candidate(wk, asset, interval)
                    decision = evaluate_rules(
                        cand, btc, prev_btc, cfg, interval=interval, prev_asset=prev_asset
                    )
                    if decision is None:
                        continue
                    row = self._build_row(cand, decision, interval, wk, now)
                    context_btc = btc or prev_btc
                    if context_btc is not None:
                        row["_btc_context"] = dict(context_btc)
                    if row.get("record_kind") == RECORD_EARLY_FLIP_WATCH:
                        watch_rows.append(row)
                    else:
                        rows.append(row)
                for row in watch_rows:
                    self._record_watch(row)
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
        if _v3_owns_bnb_notification(self.config, row):
            result = {
                "delivered": False,
                "muted": True,
                "message_id": None,
                "error": "v3_bnb_combined_owns_notification",
            }
        elif strategy_bots_runtime.owns_source_notification(
            row,
            source_system="high_vol_flip",
            btc_context=row.get("_btc_context"),
        ):
            result = {
                "delivered": False,
                "muted": True,
                "message_id": None,
                "error": "v3_owns_notification",
            }
        elif self.config.telegram_enabled and self.config.alert_telegram_enabled:
            result = self.telegram.send(panel.build_alert(row))
        else:
            result = {"delivered": False, "muted": True,
                      "message_id": None, "error": "telegram_disabled"}
        if result.get("delivered"):
            status, message_id = "SENT", result.get("message_id")
        elif result.get("muted"):
            status, message_id = "MUTED", None
        else:
            status, message_id = "DELIVERY_FAILED", None
        self.ledger.mark_delivery(row_id, status, message_id, result.get("error"))
        strategy_row = dict(row)
        strategy_row["delivery_status"] = status
        strategy_row["message_id"] = message_id
        strategy_row["delivery_error"] = result.get("error")
        strategy_bots_runtime.record_source_row(
            strategy_row,
            source_system="high_vol_flip",
            btc_context=strategy_row.get("_btc_context"),
        )

    def _record_watch(self, row: Mapping[str, Any]) -> None:
        self.ledger.record_watch(row)

    def _build_row(self, cand: Mapping[str, Any], decision: Mapping[str, Any],
                   interval: str, wk: int, now: float) -> dict[str, Any]:
        entry_ask = decision.get("entry_ask_cents")
        reason = str(decision.get("reason_code") or "")
        record_kind = decision.get("record_kind") or "HIGH_VOL_FLIP_ALERT"
        return {
            "created_at": now,
            "model_version": self.config.model_version,
            "record_kind": record_kind,
            "alert_title": decision.get("alert_title"),
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
            "selected_mid_cents": decision.get("selected_mid_cents"),
            "previous_interval": decision.get("previous_interval"),
            "previous_selected_mid_cents": decision.get("previous_selected_mid_cents"),
            "selected_mid_jump_cents": decision.get("selected_mid_jump_cents"),
            "yes_bid_cents": cand.get("yes_bid_cents"),
            "yes_ask_cents": cand.get("yes_ask_cents"),
            "no_bid_cents": cand.get("no_bid_cents"),
            "no_ask_cents": cand.get("no_ask_cents"),
            "spread_cents": cand.get("spread_cents"),
            "depth_contracts": cand.get("depth_contracts"),
            "yes_bid_depth_contracts": cand.get("yes_bid_depth_contracts"),
            "yes_ask_depth_contracts": cand.get("yes_ask_depth_contracts"),
            "no_bid_depth_contracts": cand.get("no_bid_depth_contracts"),
            "no_ask_depth_contracts": cand.get("no_ask_depth_contracts"),
            "selected_depth_ratio": selected_depth_ratio(cand, decision.get("selected_side")),
            "kalshi_depth_status": cand.get("kalshi_depth_status"),
            "kalshi_depth_missing_reason": cand.get("kalshi_depth_missing_reason"),
            "kalshi_depth_retry_used": 1 if cand.get("kalshi_depth_retry_used") else 0,
            "kalshi_taker_yes_volume_15s": cand.get("kalshi_taker_yes_volume_15s"),
            "kalshi_taker_no_volume_15s": cand.get("kalshi_taker_no_volume_15s"),
            "kalshi_taker_net_yes_volume_15s": cand.get("kalshi_taker_net_yes_volume_15s"),
            "spot_depth_status": cand.get("spot_depth_status"),
            "spot_depth_missing_reason": cand.get("spot_depth_missing_reason"),
            "spot_depth_source": cand.get("spot_depth_source"),
            "spot_depth_age_seconds": cand.get("spot_depth_age_seconds"),
            "spot_depth_trade_age_seconds": cand.get("spot_depth_trade_age_seconds"),
            "spot_depth_best_bid": cand.get("spot_depth_best_bid"),
            "spot_depth_best_ask": cand.get("spot_depth_best_ask"),
            "spot_depth_mid": cand.get("spot_depth_mid"),
            "spot_depth_spread_bps": cand.get("spot_depth_spread_bps"),
            "spot_depth_bid_depth_top": cand.get("spot_depth_bid_depth_top"),
            "spot_depth_ask_depth_top": cand.get("spot_depth_ask_depth_top"),
            "spot_depth_bid_depth_levels": cand.get("spot_depth_bid_depth_levels"),
            "spot_depth_ask_depth_levels": cand.get("spot_depth_ask_depth_levels"),
            "spot_depth_bid_notional_levels": cand.get("spot_depth_bid_notional_levels"),
            "spot_depth_ask_notional_levels": cand.get("spot_depth_ask_notional_levels"),
            "spot_depth_imbalance": cand.get("spot_depth_imbalance"),
            "spot_depth_trade_buy_qty_5s": cand.get("spot_depth_trade_buy_qty_5s"),
            "spot_depth_trade_sell_qty_5s": cand.get("spot_depth_trade_sell_qty_5s"),
            "spot_depth_trade_net_qty_5s": cand.get("spot_depth_trade_net_qty_5s"),
            "spot_depth_trade_buy_notional_5s": cand.get("spot_depth_trade_buy_notional_5s"),
            "spot_depth_trade_sell_notional_5s": cand.get("spot_depth_trade_sell_notional_5s"),
            "spot_depth_trade_net_notional_5s": cand.get("spot_depth_trade_net_notional_5s"),
            "spot_depth_trade_buy_qty_15s": cand.get("spot_depth_trade_buy_qty_15s"),
            "spot_depth_trade_sell_qty_15s": cand.get("spot_depth_trade_sell_qty_15s"),
            "spot_depth_trade_net_qty_15s": cand.get("spot_depth_trade_net_qty_15s"),
            "spot_depth_trade_buy_notional_15s": cand.get("spot_depth_trade_buy_notional_15s"),
            "spot_depth_trade_sell_notional_15s": cand.get("spot_depth_trade_sell_notional_15s"),
            "spot_depth_trade_net_notional_15s": cand.get("spot_depth_trade_net_notional_15s"),
            "spot_depth_trade_buy_qty_60s": cand.get("spot_depth_trade_buy_qty_60s"),
            "spot_depth_trade_sell_qty_60s": cand.get("spot_depth_trade_sell_qty_60s"),
            "spot_depth_trade_net_qty_60s": cand.get("spot_depth_trade_net_qty_60s"),
            "spot_depth_trade_buy_notional_60s": cand.get("spot_depth_trade_buy_notional_60s"),
            "spot_depth_trade_sell_notional_60s": cand.get("spot_depth_trade_sell_notional_60s"),
            "spot_depth_trade_net_notional_60s": cand.get("spot_depth_trade_net_notional_60s"),
            "spot_depth_last_trade_price": cand.get("spot_depth_last_trade_price"),
            "spot_depth_last_trade_side": cand.get("spot_depth_last_trade_side"),
            "spot_depth_last_trade_size": cand.get("spot_depth_last_trade_size"),
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
            "delivery_status": (
                "RECORDED" if record_kind == RECORD_EARLY_FLIP_WATCH else "PENDING"
            ),
        }

    def _prune_btc_state(self, now: float) -> None:
        cutoff = now - 7200.0
        stale = [wk for wk, rec in self._last_btc_by_window.items()
                 if float(rec.get("updated_at") or 0.0) < cutoff]
        for wk in stale:
            self._last_btc_by_window.pop(wk, None)
        stale_history = [
            wk for wk, asset_map in self._candidate_history_by_window.items()
            if not any(
                float(rec.get("updated_at") or 0.0) >= cutoff
                for interval_map in asset_map.values()
                for rec in interval_map.values()
            )
        ]
        for wk in stale_history:
            self._candidate_history_by_window.pop(wk, None)

    def _active_interval_marks(self) -> list[tuple[str, int]]:
        names = set(self.config.intervals)
        if getattr(self.config, "more_fire_strict_enabled", False):
            names.update(getattr(self.config, "more_fire_strict_intervals", frozenset()))
        # 13M is a history-only checkpoint for the 12M More-fire jump.
        names.add("13M")
        return [(iv, mark) for iv, mark in INTERVAL_MARKS.items() if iv in names]

    def _remember_interval_candidates(
        self, window_key_value: int, candidates: list[dict[str, Any]], now: float
    ) -> None:
        history = self._candidate_history_by_window.setdefault(window_key_value, {})
        for interval, mark in INTERVAL_MARKS.items():
            in_band = [
                c for c in candidates
                if (mark - self.config.mark_band_seconds) <= float(c["seconds_remaining"]) <= mark
            ]
            if not in_band:
                continue
            for cand in in_band:
                asset = str(cand.get("asset") or "").upper()
                if not asset:
                    continue
                stamped = dict(cand)
                stamped["_interval"] = interval
                history.setdefault(asset, {})[interval] = {
                    "candidate": stamped,
                    "updated_at": now,
                }

    def _previous_candidate(self, window_key_value: int, asset: str,
                            current_interval: str) -> dict[str, Any] | None:
        current_mark = INTERVAL_MARKS.get(current_interval)
        if current_mark is None:
            return None
        interval_map = self._candidate_history_by_window.get(window_key_value, {}).get(asset)
        if not interval_map:
            return None
        eligible: list[tuple[int, dict[str, Any]]] = []
        for interval, rec in interval_map.items():
            mark = INTERVAL_MARKS.get(interval)
            if mark is not None and mark > current_mark:
                cand = rec.get("candidate")
                if isinstance(cand, Mapping):
                    eligible.append((mark, dict(cand)))
        if not eligible:
            return None
        return min(eligible, key=lambda item: item[0])[1]

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
                strategy_bots_runtime.resolve(
                    source_system="high_vol_flip",
                    source_model_version=self.config.model_version,
                    ticker=str(ticker),
                    official_result=str(result),
                    now=now,
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
                strategy_bots_runtime.resolve(
                    source_system="high_vol_flip",
                    source_model_version=self.config.model_version,
                    ticker=ticker,
                    official_result=result,
                    now=now,
                )

    def scoreboard(self) -> dict[str, Any]:
        return self.ledger.scoreboard(self.config.model_version)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "model_version": self.config.model_version,
            "paper_only": True,
            "telegram": self.telegram.status(),
            "telegram_alerts_enabled": (
                self.config.telegram_enabled and self.config.alert_telegram_enabled
            ),
            "assets": sorted(self.config.assets),
            "excluded_assets": sorted(self.config.excluded_assets),
            "intervals": sorted(self.config.intervals),
            "depth_veto": {
                "enabled": self.config.depth_veto_enabled,
                "min_selected_bid_ask_ratio": (
                    self.config.depth_veto_min_selected_bid_ask_ratio
                ),
                "rule_codes": sorted(self.config.depth_veto_rule_codes),
            },
            "btc_follow_extreme": {
                "enabled": self.config.btc_follow_enabled,
                "assets": sorted(self.config.btc_follow_assets),
                "excluded_assets": sorted(self.config.btc_follow_excluded_assets),
                "mid_min": self.config.btc_follow_mid_min,
                "asset_mid_min": self.config.btc_follow_asset_mid_min,
            },
            "more_fire_strict": {
                "enabled": self.config.more_fire_strict_enabled,
                "assets": sorted(self.config.more_fire_strict_assets),
                "excluded_assets": sorted(self.config.more_fire_excluded_assets),
                "intervals": sorted(self.config.more_fire_strict_intervals),
                "yes_ask_lo": self.config.more_fire_yes_ask_lo,
                "yes_ask_hi": self.config.more_fire_yes_ask_hi,
                "min_yes_bid_ask_depth_ratio": (
                    self.config.more_fire_min_yes_bid_ask_depth_ratio
                ),
            },
            "early_watch": {
                "enabled": self.config.early_watch_enabled,
                "entry_enabled": self.config.early_entry_enabled,
                "assets": sorted(self.config.own_early_assets),
            },
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
