"""Non-blocking-from-network, paper-only runner for Q15 MarketLead."""
from __future__ import annotations

import copy
import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..lineage import stable_config_hash
from .config import MarketLeadConfig
from .features import (
    FEATURE_SCHEMA_VERSION,
    LEAD_LAG_RULE_VERSION,
    MarketLeadFeatureEngine,
    feature_lineage_config,
)
from .ledger import MarketLeadLedger

logger = logging.getLogger(__name__)
_SOURCE_ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _audit_implementation_hash() -> str:
    """Hash the local Python pipeline that can affect capture or scoring."""
    marketlead_dir = Path(__file__).resolve().parent
    project_root = marketlead_dir.parents[1]
    paths = sorted(
        {
            *project_root.glob("*.py"),
            *(project_root / "q15_upgrade").rglob("*.py"),
        },
        key=lambda path: path.relative_to(project_root).as_posix(),
    )
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(project_root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


@dataclass(frozen=True)
class _Candidate:
    ticker: str
    seconds_remaining: float
    observed_at: float
    row: dict[str, Any]


@dataclass
class _PendingExecution:
    ticker: str
    side: str
    signal_at: float
    limit_cents: float
    touch_at: float | None = None
    touch_price_cents: float | None = None
    completed: set[int] | None = None

    def __post_init__(self) -> None:
        if self.completed is None:
            self.completed = set()


class MarketLeadRunner:
    """Collect prospective 13M evidence without making a trade decision."""

    def __init__(
        self,
        config: MarketLeadConfig | None = None,
        *,
        microstructure_provider: Callable[..., Mapping[str, Any]] | None = None,
        index_provider: Callable[..., Mapping[str, Any]] | None = None,
        market_source_provider: Callable[..., Mapping[str, Any]] | None = None,
        notification_sender: Callable[..., Mapping[str, Any]] | None = None,
        notification_status_provider: Callable[[str], str | None] | None = None,
    ):
        self.config = config or MarketLeadConfig.from_env()
        self.ledger = MarketLeadLedger(self.config.db_path)
        self.engine = MarketLeadFeatureEngine(self.config)
        self._audit_rule_config = self._build_audit_rule_config()
        self._audit_registration = self.ledger.register_audit_rule(
            self.config.v3_rule_version,
            self._audit_rule_config,
        )
        self._lock = threading.Lock()
        self._last: dict[str, _Candidate] = {}
        self._pending: dict[str, _PendingExecution] = {}
        self._source_status: dict[str, dict[str, Any]] = {}
        self._source_status_checked_at = 0.0
        self._microstructure_provider = microstructure_provider
        self._index_provider = index_provider
        self._market_source_provider = market_source_provider
        self._notification_sender = (
            notification_sender or self._default_notification_sender
        )
        self._notification_status_provider = (
            notification_status_provider or self._default_notification_status
        )
        self._restore_pending()
        if self.config.v3_notify_enabled:
            self._reconcile_notifications(time.time())
            self._retry_due_notifications(time.time())

    def _build_audit_rule_config(self) -> dict[str, Any]:
        """Canonical rule and scoring policy frozen under ``v3_rule_version``."""
        feature_config = feature_lineage_config(self.config)
        return {
            "audit_protocol": "prospective-atomic-independent-windows-v1",
            "system_version": self.config.system_version,
            "implementation_hash": _audit_implementation_hash(),
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "feature_config_hash": stable_config_hash(feature_config),
            "lead_lag_rule_version": LEAD_LAG_RULE_VERSION,
            "checkpoint_seconds": self.config.mark_seconds,
            "capture_policy": {
                "mark_band_seconds": self.config.mark_band_seconds,
                "crossing_max_seconds": self.config.crossing_max_seconds,
                "crossing_max_offset_seconds": (
                    self.config.crossing_max_offset_seconds
                ),
                "one_observation_per_ticker_and_checkpoint": True,
            },
            "qualification": {
                "evidence_status": "READY",
                "lead_lag_candidate": True,
                "kalshi_available": True,
                "predicted_sides": ["NO", "YES"],
                "kalshi_max_age_seconds": self.config.kalshi_stale_seconds,
                "minimum_proxy_sources": self.config.min_proxy_sources,
                "minimum_venue_sources": self.config.min_venue_sources,
                "proxy_distance_side_bps_min": (
                    self.config.v3_min_proxy_distance_bps
                ),
                "venue_impulse_side_min": self.config.v3_min_venue_impulse,
                "kalshi_pressure_side_max": self.config.v3_max_kalshi_pressure,
            },
            "feature_inputs": feature_config,
            "scoring": {
                "unit": "independent_15_minute_window",
                "window_win_requires_all_qualified_assets_correct": True,
                "block_windows": self.config.audit_block_windows,
                "minimum_complete_blocks": self.config.audit_min_blocks,
                "accuracy_each_complete_block": self.config.audit_accuracy_min,
                "positive_gross_pnl_each_complete_block": True,
                "window_wilson_95_low_min": self.config.audit_wilson_lb_min,
                "backfill_allowed": False,
            },
        }

    def _restore_pending(self) -> None:
        for row in self.ledger.pending_execution_rows():
            completed = {
                horizon for horizon in (5, 15, 30)
                if row.get(f"markout_side_{horizon}s_cents") is not None
            }
            self._pending[str(row["ticker"])] = _PendingExecution(
                ticker=str(row["ticker"]),
                side=str(row.get("predicted_side") or "").upper(),
                signal_at=float(row.get("observed_at") or 0.0),
                limit_cents=float(row.get("paper_limit_cents") or self.config.paper_limit_cents),
                touch_at=_num(row.get("paper_limit_touch_at")),
                touch_price_cents=_num(row.get("paper_touch_price_cents")),
                completed=completed,
            )

    def _default_microstructure(self, ticker: str, now: float) -> Mapping[str, Any]:
        from ..ws_client import get_feed

        return get_feed().get_microstructure(
            ticker,
            now=now,
            max_book_age=self.config.kalshi_stale_seconds,
            paper_limit_cents=self.config.paper_limit_cents,
        )

    @staticmethod
    def _default_notification_sender(
        text: str, *, idempotency_key: str, expires_at: float
    ) -> Mapping[str, Any]:
        from ..strategy_bots.runtime import enqueue_v3_outbox_notification

        return enqueue_v3_outbox_notification(
            text,
            idempotency_key=idempotency_key,
            expires_at=expires_at,
        )

    @staticmethod
    def _default_notification_status(idempotency_key: str) -> str | None:
        from ..strategy_bots.runtime import v3_outbox_notification_status

        return v3_outbox_notification_status(idempotency_key)

    def _audit_evaluation(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """Make and label the qualification decision before an outcome exists."""
        reasons: list[str] = []
        if str(row.get("evidence_status") or "").upper() != "READY":
            reasons.append("EVIDENCE_NOT_READY")
        if int(row.get("lead_lag_candidate") or 0) != 1:
            reasons.append("LEAD_LAG_NOT_CANDIDATE")
        if int(row.get("kalshi_available") or 0) != 1:
            reasons.append("KALSHI_UNAVAILABLE")
        if str(row.get("predicted_side") or "").upper() not in {"YES", "NO"}:
            reasons.append("PREDICTED_SIDE_INVALID")
        book_age = _num(row.get("kalshi_book_age_seconds"))
        event_age = _num(row.get("kalshi_event_age_seconds"))
        proxy_distance = _num(row.get("proxy_distance_side_bps"))
        venue_impulse = _num(row.get("venue_impulse_side"))
        kalshi_pressure = _num(row.get("kalshi_pressure_side"))
        maximum = max(0.0, self.config.kalshi_stale_seconds)
        if book_age is None:
            reasons.append("KALSHI_BOOK_AGE_MISSING")
        elif book_age > maximum:
            reasons.append("KALSHI_BOOK_STALE")
        if event_age is None:
            reasons.append("KALSHI_EVENT_AGE_MISSING")
        elif event_age > maximum:
            reasons.append("KALSHI_EVENT_STALE")
        if (
            int(row.get("rti_proxy_source_count") or 0)
            < self.config.min_proxy_sources
        ):
            reasons.append("PROXY_SOURCE_COUNT_BELOW_MIN")
        if (
            int(row.get("venue_source_count") or 0)
            < self.config.min_venue_sources
        ):
            reasons.append("VENUE_SOURCE_COUNT_BELOW_MIN")
        if proxy_distance is None:
            reasons.append("PROXY_DISTANCE_MISSING")
        elif proxy_distance < self.config.v3_min_proxy_distance_bps:
            reasons.append("PROXY_DISTANCE_BELOW_MIN")
        if venue_impulse is None:
            reasons.append("VENUE_IMPULSE_MISSING")
        elif venue_impulse < self.config.v3_min_venue_impulse:
            reasons.append("VENUE_IMPULSE_BELOW_MIN")
        if kalshi_pressure is None:
            reasons.append("KALSHI_PRESSURE_MISSING")
        elif kalshi_pressure > self.config.v3_max_kalshi_pressure:
            reasons.append("KALSHI_PRESSURE_ABOVE_MAX")
        return {
            "rule_version": self.config.v3_rule_version,
            "config_hash": self._audit_registration.get("config_hash"),
            "qualified": not reasons,
            "reason_codes": reasons,
            "created_at": time.time(),
        }

    def _notification_eligible(
        self, row: Mapping[str, Any], audit: Mapping[str, Any]
    ) -> bool:
        del row
        return bool(
            self.config.v3_notify_enabled
            and self._audit_registration.get("valid")
            and audit.get("qualified")
            and not self._notification_guard()["auto_muted"]
        )

    def _notification_guard(self) -> dict[str, Any]:
        performance = self.ledger.notification_performance(
            self.config.v3_rule_version,
            lookback=self.config.v3_guard_lookback,
        )
        resolved = int(performance.get("resolved") or 0)
        accuracy = _num(performance.get("accuracy"))
        minimum_resolved = max(1, self.config.v3_guard_min_resolved)
        accuracy_min = min(1.0, max(0.0, self.config.v3_guard_accuracy_min))
        auto_muted = bool(
            resolved >= minimum_resolved
            and accuracy is not None
            and accuracy < accuracy_min
        )
        return {
            **performance,
            "rule_version": self.config.v3_rule_version,
            "lookback": max(1, self.config.v3_guard_lookback),
            "minimum_resolved": minimum_resolved,
            "accuracy_min": accuracy_min,
            "auto_muted": auto_muted,
        }

    def _notification_record(
        self, row: Mapping[str, Any], audit: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        if not self._notification_eligible(row, audit):
            return None
        observed_at = _num(row.get("observed_at"))
        expires_at = _num(row.get("close_time"))
        seconds_remaining = _num(row.get("seconds_remaining"))
        if observed_at is None:
            return None
        if expires_at is None and seconds_remaining is not None:
            expires_at = observed_at + max(0.0, seconds_remaining)
        if expires_at is None or expires_at <= observed_at:
            return None
        try:
            from ..strategy_bots.telegram import build_marketlead_alert

            payload = build_marketlead_alert({
                **row,
                "notification_rule_version": self.config.v3_rule_version,
                "notification_proxy_distance_min": (
                    self.config.v3_min_proxy_distance_bps
                ),
                "notification_venue_impulse_min": (
                    self.config.v3_min_venue_impulse
                ),
                "notification_kalshi_pressure_max": (
                    self.config.v3_max_kalshi_pressure
                ),
            })
        except Exception:
            logger.warning("marketlead V3 alert rendering failed", exc_info=True)
            return None
        key = ":".join((
            str(self.config.system_version),
            "marketlead",
            "v3",
            str(self.config.v3_rule_version),
            str(row.get("asset") or "").upper(),
            str(int(row.get("window_key") or 0)),
            str(int(row.get("mark_seconds") or self.config.mark_seconds)),
        ))
        return {
            "notification_key": key,
            "payload": payload,
            "expires_at": expires_at,
            "created_at": observed_at,
        }

    @staticmethod
    def _notification_delivery_status(result: Mapping[str, Any]) -> str:
        outbox_status = str(result.get("outbox_status") or "").upper()
        if result.get("delivered") or outbox_status == "SENT":
            return "SENT"
        if result.get("muted"):
            return "MUTED"
        if outbox_status in {"PENDING", "SENDING", "FAILED_RETRYABLE"}:
            return "QUEUED_RETRY"
        if outbox_status in {"EXPIRED", "DEAD_LETTER"}:
            return outbox_status
        error = str(result.get("error") or "")
        if error == "outbox:EXPIRED":
            return "EXPIRED"
        return "DELIVERY_FAILED"

    def _deliver_notification(self, row: Mapping[str, Any], now: float) -> None:
        key = str(row.get("notification_key") or "")
        payload = str(row.get("payload") or "")
        expires_at = _num(row.get("expires_at"))
        if not key or not payload or expires_at is None:
            return
        if now >= expires_at:
            result: Mapping[str, Any] = {
                "outbox_status": "EXPIRED",
                "error": "outbox:EXPIRED",
            }
        else:
            try:
                raw = self._notification_sender(
                    payload,
                    idempotency_key=key,
                    expires_at=expires_at,
                )
                result = raw if isinstance(raw, Mapping) else {
                    "delivered": bool(raw),
                    "error": None if raw else "notification_sender_failed",
                }
            except Exception as exc:
                result = {
                    "delivered": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
        status = self._notification_delivery_status(result)
        attempt_count = int(row.get("attempt_count") or 0) + 1
        retry_at = None
        if status == "DELIVERY_FAILED":
            base = max(0.0, self.config.v3_notify_retry_seconds)
            retry_at = now + min(300.0, base * (2 ** min(attempt_count - 1, 4)))
        self.ledger.mark_notification_attempt(
            key,
            status=status,
            attempted_at=now,
            next_attempt_at=retry_at,
            last_error=(None if status == "SENT" else str(result.get("error") or "") or None),
        )

    def _retry_due_notifications(self, now: float) -> None:
        if not self.config.v3_notify_enabled:
            return
        for row in self.ledger.due_notifications(now, limit=25):
            self._deliver_notification(row, now)

    def _reconcile_notifications(self, now: float) -> None:
        if not self.config.v3_notify_enabled:
            return
        for row in self.ledger.notifications_to_reconcile(limit=100):
            key = str(row.get("notification_key") or "")
            if not key:
                continue
            try:
                status = str(self._notification_status_provider(key) or "").upper()
            except Exception:
                logger.debug("marketlead V3 status lookup failed", exc_info=True)
                continue
            if status not in {"SENT", "EXPIRED", "DEAD_LETTER"}:
                continue
            self.ledger.reconcile_notification(
                key,
                status=status,
                updated_at=now,
                last_error=None if status == "SENT" else f"outbox:{status}",
            )

    @staticmethod
    def _default_index(asset: str, spot: float | None, now: float) -> Mapping[str, Any]:
        from settlement_index import settlement_index_context

        return settlement_index_context(asset, spot_px=spot, now=now)

    @staticmethod
    def _default_market_sources(asset: str, now: float) -> Mapping[str, Any]:
        from .live_sources import live_market_sources

        return live_market_sources(asset, now)

    def _live_sources(self, asset: str, now: float) -> Mapping[str, Any]:
        provider = self._market_source_provider or self._default_market_sources
        try:
            result = provider(asset, now)
        except Exception as exc:
            result = {
                "sources": {},
                "diagnostics": {
                    "provider": {"status": f"error:{type(exc).__name__}"}
                },
            }
        status_now = (
            max(now, time.time())
            if self._market_source_provider is None and now > 1_000_000_000
            else now
        )
        sources = result.get("sources") if isinstance(result, Mapping) else {}
        source_rows: dict[str, Any] = {}
        if isinstance(sources, Mapping):
            for name, source in sources.items():
                if not isinstance(source, Mapping):
                    continue
                timestamp = _num(source.get("timestamp"))
                price = _num(source.get("price"))
                age = None if timestamp is None else max(0.0, status_now - timestamp)
                spread = _num(source.get("spread_bps"))
                transport = str(source.get("transport") or "")
                transport_connected = bool(source.get("transport_connected"))
                message_age = _num(source.get("transport_message_age_seconds"))
                rejected: list[str] = []
                if timestamp is None or price is None or price <= 0:
                    rejected.append("missing_price_or_timestamp")
                elif timestamp > status_now + self.config.source_future_tolerance_seconds:
                    rejected.append("future_timestamp")
                elif age is not None and age > self.config.source_stale_seconds:
                    rejected.append("stale")
                if not transport.startswith("websocket_"):
                    rejected.append("not_live_websocket")
                if not transport_connected:
                    rejected.append("transport_disconnected")
                if (
                    message_age is None
                    or message_age > self.config.transport_stale_seconds
                ):
                    rejected.append("transport_message_stale")
                if spread is None or spread > self.config.max_source_spread_bps:
                    rejected.append("spread_invalid_or_wide")
                source_rows[str(name)] = {
                    "timestamp": timestamp,
                    "age_seconds": age,
                    "price": price,
                    "transport": transport or None,
                    "transport_connected": transport_connected,
                    "transport_message_age_seconds": message_age,
                    "book_update_age_seconds": _num(
                        source.get("book_update_age_seconds")
                    ),
                    "spread_bps": spread,
                    "eligible": not rejected,
                    "rejected_reasons": rejected,
                }
        proxy_rows = [
            row
            for name, row in source_rows.items()
            if name.lower() in self.config.proxy_sources and row["eligible"]
        ]
        timestamps = [float(row["timestamp"]) for row in proxy_rows]
        prices = [float(row["price"]) for row in proxy_rows]
        timestamp_spread = (
            max(timestamps) - min(timestamps) if len(timestamps) >= 2 else None
        )
        proxy_mid = sum(prices) / len(prices) if prices else None
        price_dispersion = (
            (max(prices) - min(prices)) / proxy_mid * 10_000.0
            if len(prices) >= 2 and proxy_mid
            else None
        )
        proxy_ready = (
            len(proxy_rows) >= self.config.min_proxy_sources
            and timestamp_spread is not None
            and timestamp_spread <= self.config.sync_tolerance_seconds
            and price_dispersion is not None
            and price_dispersion <= self.config.max_proxy_dispersion_bps
        )
        diagnostics = result.get("diagnostics") if isinstance(result, Mapping) else {}
        self._source_status[str(asset).upper()] = {
            "checked_at": status_now,
            "sources": source_rows,
            "proxy_ready": proxy_ready,
            "proxy_source_count": len(proxy_rows),
            "timestamp_spread_seconds": timestamp_spread,
            "price_dispersion_bps": price_dispersion,
            "diagnostics": dict(diagnostics) if isinstance(diagnostics, Mapping) else {},
        }
        return result if isinstance(result, Mapping) else {}

    def _providers(
        self, asset: str, ticker: str, spot: float | None, now: float
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        micro = self._microstructure_provider or self._default_microstructure
        index = self._index_provider or self._default_index
        try:
            kalshi = micro(ticker, now=now)
        except Exception as exc:  # evidence failure must remain explicit
            kalshi = {"available": False, "reason": f"provider_error:{type(exc).__name__}"}
        try:
            official = index(asset, spot, now)
        except Exception as exc:
            official = {
                "index_px": None,
                "index_status": "error",
                "index_missing_reason": f"provider_error:{type(exc).__name__}",
            }
        return official, kalshi

    @staticmethod
    def _side_quote(kalshi: Mapping[str, Any], side: str) -> tuple[float | None, float | None]:
        yes_ask = _num(kalshi.get("yes_ask_cents"))
        no_ask = _num(kalshi.get("no_ask_cents"))
        yes_mid = _num(kalshi.get("yes_mid_cents"))
        if side == "YES":
            return yes_ask, yes_mid
        return no_ask, None if yes_mid is None else 100.0 - yes_mid

    def _track_execution(self, ticker: str, kalshi: Mapping[str, Any], now: float) -> None:
        pending = self._pending.get(ticker)
        if pending is None or not kalshi.get("available"):
            return
        ask, side_mid = self._side_quote(kalshi, pending.side)
        if pending.touch_at is None and ask is not None and ask <= pending.limit_cents:
            if self.ledger.record_touch(
                self.config.system_version,
                ticker,
                touched_at=now,
                price_cents=ask,
            ):
                pending.touch_at = now
                pending.touch_price_cents = ask
        if pending.touch_at is not None and pending.touch_price_cents is not None:
            elapsed = now - pending.touch_at
            for horizon in (5, 15, 30):
                if horizon in pending.completed or elapsed < horizon or side_mid is None:
                    continue
                if self.ledger.record_markout(
                    self.config.system_version,
                    ticker,
                    horizon,
                    side_mid - pending.touch_price_cents,
                ):
                    pending.completed.add(horizon)
            if pending.completed == {5, 15, 30}:
                self.ledger.set_execution_status(
                    self.config.system_version, ticker, "COMPLETE"
                )
                self._pending.pop(ticker, None)
        elif now - pending.signal_at > 60.0:
            self.ledger.set_execution_status(
                self.config.system_version, ticker, "EXPIRED"
            )
            self._pending.pop(ticker, None)

    def _expire_stale_pending(self, now: float) -> None:
        for ticker, pending in list(self._pending.items()):
            basis = pending.touch_at if pending.touch_at is not None else pending.signal_at
            maximum_age = 120.0 if pending.touch_at is not None else 60.0
            if now - basis <= maximum_age:
                continue
            self.ledger.set_execution_status(
                self.config.system_version, ticker, "EXPIRED"
            )
            self._pending.pop(ticker, None)

    def _select(self, asset: str, current: _Candidate) -> _Candidate | None:
        previous = self._last.get(asset)
        self._last[asset] = current
        mark = float(self.config.mark_seconds)
        band = max(0.0, self.config.mark_band_seconds)
        if mark - band <= current.seconds_remaining <= mark:
            return current
        if previous is None or previous.ticker != current.ticker:
            return None
        wall_gap = current.observed_at - previous.observed_at
        clock_drop = previous.seconds_remaining - current.seconds_remaining
        crossed = (
            0 < wall_gap <= self.config.crossing_max_seconds
            and 0 < clock_drop <= self.config.crossing_max_seconds
            and previous.seconds_remaining > mark >= current.seconds_remaining
        )
        if not crossed:
            return None
        selected = min(
            (previous, current),
            key=lambda candidate: abs(candidate.seconds_remaining - mark),
        )
        if abs(selected.seconds_remaining - mark) > self.config.crossing_max_offset_seconds:
            return None
        return selected

    def observe(
        self,
        *,
        analyses: Mapping[str, Mapping[str, Any]],
        canonicals: Mapping[str, Any],
        now: float,
    ) -> None:
        if not self.config.enabled or not self.ledger.available:
            return
        try:
            with self._lock:
                cycle_now = (
                    max(now, time.time())
                    if self._market_source_provider is None and now > 1_000_000_000
                    else now
                )
                self._reconcile_notifications(cycle_now)
                self._retry_due_notifications(cycle_now)
                self._expire_stale_pending(cycle_now)
                for asset, canonical in (canonicals or {}).items():
                    ticker = str(getattr(canonical, "ticker", "") or "")
                    observed_now = (
                        max(now, time.time())
                        if self._market_source_provider is None and now > 1_000_000_000
                        else now
                    )
                    settlement_time = _num(getattr(canonical, "settlement_time", None))
                    seconds = (
                        max(0.0, settlement_time - observed_now)
                        if settlement_time is not None and observed_now > 1_000_000_000
                        else _num(getattr(canonical, "seconds_remaining", None))
                    )
                    if not ticker or seconds is None:
                        continue
                    history_start = self.config.mark_seconds + max(
                        60.0, self.config.history_seconds
                    )
                    history_end = self.config.mark_seconds - max(
                        self.config.crossing_max_seconds,
                        self.config.mark_band_seconds,
                    )
                    if seconds > history_start or seconds < history_end:
                        self._last.pop(str(asset), None)
                        continue
                    analysis = analyses.get(asset) if isinstance(analyses, Mapping) else None
                    analysis = analysis if isinstance(analysis, Mapping) else {}
                    live_sources = self._live_sources(str(asset), observed_now)
                    if self._market_source_provider is None and now > 1_000_000_000:
                        observed_now = max(observed_now, time.time())
                        if settlement_time is not None:
                            seconds = max(0.0, settlement_time - observed_now)
                    spot = _num(getattr(canonical, "spot", None))
                    near_mark = abs(seconds - self.config.mark_seconds) <= max(
                        self.config.crossing_max_seconds,
                        self.config.mark_band_seconds,
                    )
                    if near_mark:
                        official, kalshi = self._providers(
                            str(asset), ticker, spot, observed_now
                        )
                        self._track_execution(ticker, kalshi, observed_now)
                    else:
                        official = {"index_px": None, "index_status": "not_sampled"}
                        kalshi = {"available": False, "reason": "not_sampled"}
                    row = self.engine.build(
                        asset=str(asset),
                        analysis=analysis,
                        canonical=canonical,
                        now=observed_now,
                        official_index=official,
                        kalshi=kalshi,
                        live_sources=live_sources,
                        seconds_remaining=seconds,
                    )
                    candidate = _Candidate(ticker, seconds, observed_now, row)
                    if not near_mark:
                        continue
                    selected = self._select(str(asset), candidate)
                    if selected is not None:
                        audit = self._audit_evaluation(selected.row)
                        audit_record = (
                            audit if self._audit_registration.get("valid") else None
                        )
                        notification = self._notification_record(selected.row, audit)
                        if self.ledger.record(
                            selected.row,
                            notification=notification,
                            audit=audit_record,
                        ):
                            touched = bool(selected.row.get("paper_limit_touched"))
                            self._pending[selected.ticker] = _PendingExecution(
                                ticker=selected.ticker,
                                side=str(selected.row.get("predicted_side") or "").upper(),
                                signal_at=float(selected.row["observed_at"]),
                                limit_cents=float(selected.row.get("paper_limit_cents") or self.config.paper_limit_cents),
                                touch_at=(
                                    _num(selected.row.get("paper_limit_touch_at")) if touched else None
                                ),
                                touch_price_cents=(
                                    _num(selected.row.get("paper_touch_price_cents")) if touched else None
                                ),
                            )
                            if notification is not None:
                                self._deliver_notification(notification, observed_now)
        except Exception:
            logger.debug("marketlead observe failed (ignored)", exc_info=True)

    def resolve_settled(
        self, result_events: Sequence[Mapping[str, Any]] | None, now: float
    ) -> int:
        if not self.config.enabled or not result_events:
            return 0
        resolved = 0
        try:
            for event in result_events:
                if not isinstance(event, Mapping):
                    continue
                ticker = event.get("ticker") or event.get("contract")
                result = event.get("result") or event.get("official_result")
                if ticker and result:
                    resolved += self.ledger.resolve(
                        self.config.system_version, str(ticker), str(result), now
                    )
        except Exception:
            logger.debug("marketlead resolve failed (ignored)", exc_info=True)
        return resolved

    def source_health(self) -> dict[str, Any]:
        """Refresh and summarize the live inputs without touching the ledger.

        The feed watchdog calls this every cycle, so it deliberately avoids the
        aggregate SQLite count performed by :meth:`status`.  The reported age is
        the worst source/transport/book age across all tracked assets.
        """
        with self._lock:
            wall_now = time.time()
            if wall_now - self._source_status_checked_at >= 1.0:
                for asset in _SOURCE_ASSETS:
                    self._live_sources(asset, wall_now)
                self._source_status_checked_at = wall_now
            source_status = copy.deepcopy(self._source_status)
        ages: list[float] = []
        ready_assets = 0
        for status in source_status.values():
            if not isinstance(status, Mapping):
                continue
            if status.get("proxy_ready"):
                ready_assets += 1
            sources = status.get("sources")
            if not isinstance(sources, Mapping):
                continue
            for source in sources.values():
                if not isinstance(source, Mapping):
                    continue
                for key in (
                    "age_seconds",
                    "transport_message_age_seconds",
                    "book_update_age_seconds",
                ):
                    age = _num(source.get(key))
                    if age is not None:
                        ages.append(max(0.0, age))
        return {
            "enabled": self.config.enabled,
            "latest_age_seconds": max(ages) if ages else None,
            "ready_assets": ready_assets,
            "tracked_assets": len(_SOURCE_ASSETS),
            "source_status": source_status,
        }

    def status(self) -> dict[str, Any]:
        source_health = self.source_health()
        notification_guard = self._notification_guard()
        prospective_audit = self.ledger.prospective_audit_report(
            self.config.v3_rule_version,
            block_windows=self.config.audit_block_windows,
            min_blocks=self.config.audit_min_blocks,
            accuracy_min=self.config.audit_accuracy_min,
            wilson_lb_min=self.config.audit_wilson_lb_min,
        )
        return {
            "system": "Q15 MarketLead",
            "system_version": self.config.system_version,
            "paper_only": True,
            "notifies": (
                self.config.v3_notify_enabled
                and bool(self._audit_registration.get("valid"))
                and not notification_guard["auto_muted"]
            ),
            "notification_configured": self.config.v3_notify_enabled,
            "trades": False,
            "notification_filter": {
                "destination": "v3_telegram",
                "rule_version": self.config.v3_rule_version,
                "checkpoint_seconds": self.config.mark_seconds,
                "evidence_status": "READY",
                "lead_lag_candidate": True,
                "kalshi_max_age_seconds": self.config.kalshi_stale_seconds,
                "proxy_distance_side_bps_min": (
                    self.config.v3_min_proxy_distance_bps
                ),
                "venue_impulse_side_min": self.config.v3_min_venue_impulse,
                "kalshi_pressure_side_max": self.config.v3_max_kalshi_pressure,
                "durable_outbox_required": True,
            },
            "notification_guard": notification_guard,
            "audit_registration": dict(self._audit_registration),
            "prospective_audit": prospective_audit,
            "source_requirements": {
                "stale_seconds": self.config.source_stale_seconds,
                "kalshi_stale_seconds": self.config.kalshi_stale_seconds,
                "transport_stale_seconds": self.config.transport_stale_seconds,
                "future_tolerance_seconds": (
                    self.config.source_future_tolerance_seconds
                ),
                "sync_tolerance_seconds": self.config.sync_tolerance_seconds,
                "max_source_spread_bps": self.config.max_source_spread_bps,
                "max_proxy_dispersion_bps": self.config.max_proxy_dispersion_bps,
            },
            "source_freshness_age_seconds": source_health["latest_age_seconds"],
            "source_ready_assets": source_health["ready_assets"],
            "source_status": source_health["source_status"],
            **self.ledger.status(),
        }


_runner: MarketLeadRunner | None = None
_runner_lock = threading.Lock()


def get_runner() -> MarketLeadRunner | None:
    global _runner
    if _runner is not None:
        return _runner
    with _runner_lock:
        if _runner is not None:
            return _runner
        config = MarketLeadConfig.from_env()
        if not config.enabled:
            return None
        _runner = MarketLeadRunner(config)
        return _runner


def reset_runner() -> None:
    global _runner
    _runner = None


__all__ = ["MarketLeadRunner", "get_runner", "reset_runner"]
