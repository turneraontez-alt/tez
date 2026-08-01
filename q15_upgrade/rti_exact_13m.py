"""Independent exact-time sampler for the RTI Path 13M paper system.

The champion refresh cycle can take several seconds.  This read-only worker is
therefore deliberately separate: it freezes the live Kalshi websocket book at
the real ``close - 780s`` boundary, then waits briefly for the official RTI
sample for that second before handing immutable evidence to the V3 paper ledger.
It has no order-placement surface and never changes the champion model.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from q15_upgrade.strategy_bots.rules import (
    RTI_PATH_13M_DELAYED_CONFIRM_60S_CHALLENGER_ID,
    RTI_PATH_13M_DELAYED_CONFIRM_60S_EXPECTED_COUNT,
    RTI_PATH_13M_DELAYED_CONFIRM_60S_POLICY_VERSION,
    RTI_PATH_13M_DELAYED_CONFIRM_60S_SECONDS,
    RTI_PATH_13M_DELAYED_CONFIRM_90S_CHALLENGER_ID,
    RTI_PATH_13M_DELAYED_CONFIRM_90S_EXPECTED_COUNT,
    RTI_PATH_13M_DELAYED_CONFIRM_90S_POLICY_VERSION,
    RTI_PATH_13M_DELAYED_CONFIRM_90S_SECONDS,
    RTI_PATH_13M_DELAYED_CONFIRM_CHALLENGER_ID,
    RTI_PATH_13M_DELAYED_CONFIRM_EXPECTED_COUNT,
    RTI_PATH_13M_DELAYED_CONFIRM_POLICY_VERSION,
    RTI_PATH_13M_DELAYED_CONFIRM_SECONDS,
    RTI_PATH_13M_SPOT_BOOK_MAX_AGE_SECONDS,
    RTI_PATH_13M_SPOT_BOOK_MIN_AGE_SECONDS,
    RTI_PATH_13M_SPOT_CONFIRM_CHALLENGER_ID,
    RTI_PATH_13M_SPOT_SNAPSHOT_MAX_AGE_SECONDS,
    RTI_EXACT_MICROSTRUCTURE_SCHEMA_VERSION,
    SPOT_MID_PATH_KEYS,
)
from q15_upgrade.strategy_bots.rti_cross_venue import (
    capture_rti_cross_venue,
)
from q15_upgrade.strategy_bots.rti_independent_path_identity import (
    DESIGN_ID as RTI_INDEPENDENT_PATH_DESIGN_ID,
    DESIGN_SHA256 as RTI_INDEPENDENT_PATH_DESIGN_SHA256,
    FIRST_ELIGIBLE_CLOSE_TIME as RTI_INDEPENDENT_PATH_FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME as RTI_INDEPENDENT_PATH_PROSPECTIVE_AFTER_CLOSE_TIME,
)

logger = logging.getLogger(__name__)
RTI_SPOT_LEAD_LAG_SCHEMA_VERSION = "rti-spot-index-lead-lag-v1"


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return min(high, max(low, value))


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class _Market:
    asset: str
    ticker: str
    close_time: float
    strike: float
    registered_at: float

    @property
    def decision_time(self) -> float:
        return self.close_time - 780.0


@dataclass
class _Pending:
    market: _Market
    captured_at: float
    quote: dict[str, Any]
    spot_context: dict[str, Any] | None = None
    evidence_completed_at: float | None = None
    last_path: dict[str, Any] | None = None
    last_record_attempt: float = 0.0


@dataclass(frozen=True)
class _ConfirmationPolicy:
    challenger_id: str
    policy_version: str
    delay_seconds: float
    expected_count: int
    interval: str
    record_kind: str
    capture_mode: str


_CONFIRMATION_POLICIES = (
    _ConfirmationPolicy(
        RTI_PATH_13M_DELAYED_CONFIRM_CHALLENGER_ID,
        RTI_PATH_13M_DELAYED_CONFIRM_POLICY_VERSION,
        RTI_PATH_13M_DELAYED_CONFIRM_SECONDS,
        RTI_PATH_13M_DELAYED_CONFIRM_EXPECTED_COUNT,
        "12M30S",
        "RTI_PATH_12M30_CONFIRM_PROSPECTIVE",
        "kalshi_ws_delayed_confirm_30s",
    ),
    _ConfirmationPolicy(
        RTI_PATH_13M_DELAYED_CONFIRM_60S_CHALLENGER_ID,
        RTI_PATH_13M_DELAYED_CONFIRM_60S_POLICY_VERSION,
        RTI_PATH_13M_DELAYED_CONFIRM_60S_SECONDS,
        RTI_PATH_13M_DELAYED_CONFIRM_60S_EXPECTED_COUNT,
        "12M",
        "RTI_PATH_12M_CONFIRM_PROSPECTIVE",
        "kalshi_ws_delayed_confirm_60s",
    ),
    _ConfirmationPolicy(
        RTI_PATH_13M_DELAYED_CONFIRM_90S_CHALLENGER_ID,
        RTI_PATH_13M_DELAYED_CONFIRM_90S_POLICY_VERSION,
        RTI_PATH_13M_DELAYED_CONFIRM_90S_SECONDS,
        RTI_PATH_13M_DELAYED_CONFIRM_90S_EXPECTED_COUNT,
        "11M30S",
        "RTI_PATH_11M30_STABILITY_PROSPECTIVE",
        "kalshi_ws_delayed_stability_90s",
    ),
)


@dataclass
class _ConfirmationPending:
    policy: _ConfirmationPolicy
    market: _Market
    original_source: dict[str, Any]
    original_row_id: int
    original_strict_accepted: bool
    target_at: float
    captured_at: float | None = None
    quote: dict[str, Any] | None = None
    spot_context: dict[str, Any] | None = None
    evidence_completed_at: float | None = None
    last_path: dict[str, Any] | None = None
    last_record_attempt: float = 0.0


class ExactRTI13MSampler:
    """Freeze exact-time evidence independently of the slow analysis loop."""

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        poll_seconds: float | None = None,
        max_timing_offset_s: float | None = None,
        feed: Any | None = None,
        path_reader: Callable[..., Mapping[str, Any]] | None = None,
        spot_reader: Callable[..., Mapping[str, Any] | None] | None = None,
        cross_venue_reader: Callable[..., Mapping[str, Any]] | None = None,
        recorder: Callable[[Mapping[str, Any]], int | None] | None = None,
        confirmation_recorder: (
            Callable[[Mapping[str, Any]], int | None] | None
        ) = None,
        confirmation_recovery_reader: (
            Callable[..., Mapping[str, Any] | None] | None
        ) = None,
    ):
        self.enabled = (
            _bool("Q15_V3_RTI_EXACT_SAMPLER", False)
            if enabled is None else bool(enabled)
        )
        self.poll_seconds = (
            _float("Q15_V3_RTI_EXACT_POLL_SECONDS", 0.05, 0.01, 0.5)
            if poll_seconds is None else max(0.01, float(poll_seconds))
        )
        self.max_timing_offset_s = (
            _float("Q15_V3_RTI_EXACT_MAX_OFFSET_SECONDS", 2.0, 0.25, 2.0)
            if max_timing_offset_s is None
            else min(2.0, max(0.25, float(max_timing_offset_s)))
        )
        self._feed = feed
        self._path_reader = path_reader
        self._spot_reader = spot_reader
        self._spot_reader_is_live_capture = spot_reader is None
        self._cross_venue_reader = cross_venue_reader
        self._recorder = recorder
        self._confirmation_recorder = confirmation_recorder
        self._confirmation_recovery_reader = confirmation_recovery_reader
        self._confirmation_recovery_enabled = (
            confirmation_recovery_reader is not None or recorder is None
        )
        self._lock = threading.RLock()
        self._markets: dict[str, _Market] = {}
        self._pending: dict[str, _Pending] = {}
        self._quote_retry_pending: dict[str, int] = {}
        self._confirmation_pending: dict[
            tuple[str, str], _ConfirmationPending
        ] = {}
        self._confirmation_quote_retry_pending: dict[
            tuple[str, str], int
        ] = {}
        self._done: dict[str, float] = {}
        self._confirmation_recovery_checked: set[str] = set()
        self._confirmation_recovery_last_attempt: dict[str, float] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._quote_captures = 0
        self._quote_retry_attempts = 0
        self._quote_retry_successes = 0
        self._quote_retry_exhausted = 0
        self._quote_retry_drain_cycles = 0
        self._recent_missed_tickers: list[str] = []
        self._recent_retry_exhausted_tickers: list[str] = []
        self._last_quote_failure_reason_by_ticker: dict[str, str] = {}
        self._decisions_recorded = 0
        self._missed_deadlines = 0
        self._record_failures = 0
        self._spot_context_ok = 0
        self._spot_context_missing = 0
        self._cross_venue_ok = 0
        self._cross_venue_missing = 0
        self._cross_asset_ok = 0
        self._cross_asset_missing = 0
        self._independent_path_ok = 0
        self._independent_path_missing = 0
        self._confirmation_quote_captures = 0
        self._confirmation_quote_retry_attempts = 0
        self._confirmation_quote_retry_successes = 0
        self._confirmation_quote_retry_exhausted = 0
        self._confirmation_decisions_recorded = 0
        self._confirmation_missed_deadlines = 0
        self._confirmation_record_failures = 0
        self._confirmation_spot_context_ok = 0
        self._confirmation_spot_context_missing = 0
        self._confirmation_recovered_parents = 0
        self._confirmation_recovered_stages = 0
        self._confirmation_recovery_failures = 0
        self._last_confirmation_recovery_at: float | None = None
        self._last_confirmation_capture_at: float | None = None
        self._last_confirmation_recorded_at: float | None = None
        self._last_confirmation_error: str | None = None
        self._last_capture_at: float | None = None
        self._last_recorded_at: float | None = None
        self._last_timing_offset_s: float | None = None
        self._last_error: str | None = None

    def start(self) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="q15-rti-exact-13m",
                daemon=True,
            )
            self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def register_market(
        self,
        *,
        asset: str,
        ticker: str,
        close_time: float | None,
        strike: float | None,
        now: float | None = None,
    ) -> bool:
        if not self.enabled:
            return False
        asset_key = str(asset or "").upper()
        ticker_key = str(ticker or "")
        close = _num(close_time)
        strike_value = _num(strike)
        if (
            asset_key not in {"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE"}
            or not ticker_key
            or close is None
            or strike_value is None
        ):
            return False
        current = time.time() if now is None else float(now)
        market = _Market(asset_key, ticker_key, close, strike_value, current)
        # Recover a durable parent before publishing this market to the exact
        # worker.  Otherwise the worker can observe an already-past decision
        # timestamp in the small gap before recovery marks the ticker done and
        # incorrectly count a restart-time miss.
        self._recover_confirmation_schedule(market, current=current)
        with self._lock:
            prior = self._markets.get(asset_key)
            self._markets[asset_key] = market
            if prior is not None and prior.ticker != ticker_key:
                self._pending.pop(prior.ticker, None)
                self._quote_retry_pending.pop(prior.ticker, None)
                self._last_quote_failure_reason_by_ticker.pop(
                    prior.ticker, None
                )
            if len(self._done) > 2048:
                cutoff = current - 86400.0
                self._done = {
                    ticker: recorded_at
                    for ticker, recorded_at in self._done.items()
                    if recorded_at >= cutoff
                }
        return True

    def _get_feed(self) -> Any:
        if self._feed is None:
            from q15_upgrade.ws_client import get_feed

            self._feed = get_feed()
        return self._feed

    def _read_path(self, market: _Market, now: float) -> dict[str, Any]:
        reader = self._path_reader
        if reader is None:
            from settlement_index import settlement_index_path

            reader = settlement_index_path
            self._path_reader = reader
        return dict(reader(
            market.asset,
            start_ts=market.decision_time - 60.0,
            end_ts=market.decision_time,
            now=now,
            max_age_s=2.0,
        ))

    def _read_confirmation_path(
        self,
        pending: _ConfirmationPending,
        now: float,
    ) -> dict[str, Any]:
        reader = self._path_reader
        if reader is None:
            from settlement_index import settlement_index_path

            reader = settlement_index_path
            self._path_reader = reader
        return dict(reader(
            pending.market.asset,
            start_ts=pending.market.decision_time,
            end_ts=pending.target_at,
            now=now,
            max_age_s=2.0,
        ))

    def _capture_quote(self, market: _Market, now: float) -> dict[str, Any]:
        try:
            raw = self._get_feed().get_microstructure(
                market.ticker,
                now=now,
                max_book_age=2.0,
            )
            quote = dict(raw) if isinstance(raw, Mapping) else {}
        except Exception as exc:  # noqa: BLE001 - missing evidence fails closed
            quote = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
        quote["captured_at"] = now
        return quote

    @staticmethod
    def _quote_has_executable_book(quote: Mapping[str, Any]) -> bool:
        return bool(quote.get("available")) and all(
            _num(quote.get(key)) is not None
            for key in (
                "yes_bid_cents", "yes_ask_cents", "no_bid_cents",
                "no_ask_cents", "yes_bid_qty", "yes_ask_qty",
            )
        )

    @staticmethod
    def _quote_failure_reason(quote: Mapping[str, Any]) -> str:
        reason = str(quote.get("reason") or "executable_book_missing")
        return reason[:200]

    @staticmethod
    def _append_recent(values: list[str], ticker: str) -> None:
        values.append(str(ticker))
        if len(values) > 64:
            del values[:-64]

    def _capture_primary_quote_attempt(
        self,
        market: _Market,
        capture_at: float,
    ) -> tuple[str, _Pending | None]:
        """Attempt one exact quote without doing any downstream enrichment."""
        decision_time = market.decision_time
        with self._lock:
            if market.ticker in self._done:
                return "done", None
            if market.ticker in self._pending:
                return "captured", None
        if capture_at < decision_time:
            return "not_due", None
        if capture_at - decision_time > self.max_timing_offset_s:
            with self._lock:
                if market.ticker not in self._done:
                    if self._quote_retry_pending.pop(
                        market.ticker, None
                    ) is not None:
                        self._quote_retry_exhausted += 1
                        self._append_recent(
                            self._recent_retry_exhausted_tickers,
                            market.ticker,
                        )
                    self._done[market.ticker] = capture_at
                    self._missed_deadlines += 1
                    self._append_recent(
                        self._recent_missed_tickers, market.ticker
                    )
                    self._last_error = "exact_quote_capture_deadline_missed"
            return "missed", None

        quote = self._capture_quote(market, capture_at)
        retry_cutoff_s = max(
            0.0,
            self.max_timing_offset_s
            - min(0.25, max(0.10, self.poll_seconds * 2.0)),
        )
        quote_usable = self._quote_has_executable_book(quote)
        if not quote_usable:
            with self._lock:
                self._last_quote_failure_reason_by_ticker[market.ticker] = (
                    self._quote_failure_reason(quote)
                )
        if not quote_usable and capture_at - decision_time < retry_cutoff_s:
            with self._lock:
                self._quote_retry_pending[market.ticker] = (
                    self._quote_retry_pending.get(market.ticker, 0) + 1
                )
                self._quote_retry_attempts += 1
            return "retry", None

        with self._lock:
            prior_retries = self._quote_retry_pending.pop(market.ticker, 0)
            if prior_retries:
                if quote_usable:
                    self._quote_retry_successes += 1
                    self._last_quote_failure_reason_by_ticker.pop(
                        market.ticker, None
                    )
                else:
                    self._quote_retry_exhausted += 1
                    self._append_recent(
                        self._recent_retry_exhausted_tickers,
                        market.ticker,
                    )
        pending = _Pending(
            market=market,
            captured_at=capture_at,
            quote=quote,
        )
        with self._lock:
            if market.ticker in self._done:
                return "done", None
            self._pending[market.ticker] = pending
            self._quote_captures += 1
            self._last_capture_at = capture_at
            self._last_timing_offset_s = capture_at - decision_time
        return "captured", pending

    def _drain_primary_quote_retries(
        self,
        markets: list[_Market],
        newly_captured: list[_Pending],
    ) -> None:
        """Retry all due books before slower evidence work can consume time."""
        by_ticker = {market.ticker: market for market in markets}
        while not self._stop.is_set():
            with self._lock:
                retry_markets = [
                    by_ticker[ticker]
                    for ticker in self._quote_retry_pending
                    if ticker in by_ticker
                    and ticker not in self._done
                    and ticker not in self._pending
                ]
            if not retry_markets:
                return
            self._stop.wait(min(self.poll_seconds, 0.05))
            with self._lock:
                self._quote_retry_drain_cycles += 1
            for market in retry_markets:
                _, pending = self._capture_primary_quote_attempt(
                    market, time.time()
                )
                if pending is not None:
                    newly_captured.append(pending)

    def _capture_spot_context(
        self,
        pending: _Pending | _ConfirmationPending,
        *,
        evidence_as_of: float,
        confirmation: bool = False,
    ) -> dict[str, Any]:
        reader = self._spot_reader
        if reader is None:
            from spot_depth import capture_current_spot_depth

            reader = capture_current_spot_depth
            self._spot_reader = reader
        try:
            raw = reader(
                pending.market.asset,
                max_age=RTI_PATH_13M_SPOT_SNAPSHOT_MAX_AGE_SECONDS,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raw = None
            missing_reason = f"{type(exc).__name__}: {exc}"[:200]
        else:
            missing_reason = "spot_depth_snapshot_missing" if raw is None else None
        snapshot = dict(raw) if isinstance(raw, Mapping) else {}
        created_at = _num(snapshot.get("created_at"))
        observed_at = (
            time.time() if self._spot_reader_is_live_capture else evidence_as_of
        )
        snapshot_age = (
            None if created_at is None else observed_at - created_at
        )
        book_age = _num(snapshot.get("book_age_seconds"))
        if snapshot and created_at is None:
            missing_reason = "spot_depth_created_at_missing"
        elif snapshot_age is not None and snapshot_age < 0.0:
            missing_reason = "spot_depth_snapshot_after_decision"
        elif (
            snapshot_age is not None
            and snapshot_age > RTI_PATH_13M_SPOT_SNAPSHOT_MAX_AGE_SECONDS
        ):
            missing_reason = "spot_depth_snapshot_stale"
        elif book_age is None:
            missing_reason = "spot_depth_book_age_missing"
        elif book_age < RTI_PATH_13M_SPOT_BOOK_MIN_AGE_SECONDS:
            missing_reason = "spot_depth_book_clock_skew"
        elif book_age > RTI_PATH_13M_SPOT_BOOK_MAX_AGE_SECONDS:
            missing_reason = "spot_depth_book_stale"
        status = "ok" if snapshot and missing_reason is None else "missing"
        with self._lock:
            if confirmation and status == "ok":
                self._confirmation_spot_context_ok += 1
            elif confirmation:
                self._confirmation_spot_context_missing += 1
            elif status == "ok":
                self._spot_context_ok += 1
            else:
                self._spot_context_missing += 1

        def value(key: str) -> Any:
            return snapshot.get(key) if status == "ok" else None

        def net_notional(suffix: str) -> float | None:
            buy = _num(value(f"trade_buy_notional_{suffix}"))
            sell = _num(value(f"trade_sell_notional_{suffix}"))
            if buy is None or sell is None:
                return None
            return buy - sell

        return {
            "rti_spot_evidence_as_of": observed_at,
            "rti_spot_snapshot_created_at": created_at,
            "rti_spot_snapshot_age_s": snapshot_age,
            "rti_spot_book_age_s": book_age,
            "spot_depth_status": status,
            "spot_depth_missing_reason": missing_reason,
            "spot_depth_source": value("source"),
            "spot_depth_age_seconds": snapshot_age,
            "spot_depth_trade_age_seconds": _num(value("trade_age_seconds")),
            "spot_depth_best_bid": _num(value("best_bid")),
            "spot_depth_best_ask": _num(value("best_ask")),
            "spot_depth_mid": _num(value("mid")),
            "spot_depth_spread_bps": _num(value("spread_bps")),
            "spot_depth_bid_depth_top": _num(value("bid_depth_top")),
            "spot_depth_ask_depth_top": _num(value("ask_depth_top")),
            "spot_depth_bid_depth_levels": _num(value("bid_depth_levels")),
            "spot_depth_ask_depth_levels": _num(value("ask_depth_levels")),
            "spot_depth_bid_notional_levels": _num(value("bid_notional_levels")),
            "spot_depth_ask_notional_levels": _num(value("ask_notional_levels")),
            "spot_depth_imbalance": _num(value("depth_imbalance")),
            **{
                f"spot_depth_trade_{field}_{suffix}": _num(
                    value(f"trade_{field}_{suffix}")
                )
                for suffix in ("5s", "15s", "60s")
                for field in (
                    "buy_qty", "sell_qty", "net_qty",
                    "buy_notional", "sell_notional",
                )
            },
            **{
                f"spot_depth_trade_net_notional_{suffix}": net_notional(suffix)
                for suffix in ("5s", "15s", "60s")
            },
            "spot_depth_last_trade_price": _num(value("last_trade_price")),
            "spot_depth_last_trade_side": value("last_trade_side"),
            "spot_depth_last_trade_size": _num(value("last_trade_size")),
            **{key: value(key) for key in SPOT_MID_PATH_KEYS},
        }

    def _capture_cross_venue_context(
        self,
        pending: _Pending,
        spot: Mapping[str, Any],
    ) -> dict[str, Any]:
        reader = self._cross_venue_reader
        if reader is None and not self._spot_reader_is_live_capture:
            result = {
                "rti_cross_venue_schema_version": (
                    "rti-cross-venue-consensus-v1"
                ),
                "rti_cross_venue_time_basis": "local_created_at",
                "rti_cross_venue_status": "missing",
                "rti_cross_venue_missing_reason": (
                    "cross_venue_reader_not_injected"
                ),
                "rti_cross_venue_evidence_cutoff_at": pending.captured_at,
            }
            with self._lock:
                self._cross_venue_missing += 1
                self._cross_asset_missing += 1
            return result
        reader = reader or capture_rti_cross_venue
        try:
            raw = reader(
                pending.market.asset,
                captured_at=pending.captured_at,
                primary_mid=spot.get("spot_depth_mid"),
                primary_change_bps_15s=spot.get("spot_mid_change_bps_15s"),
                primary_change_bps_60s=spot.get("spot_mid_change_bps_60s"),
                primary_source=spot.get("spot_depth_source"),
            )
            result = dict(raw) if isinstance(raw, Mapping) else {}
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            result = {
                "rti_cross_venue_schema_version": (
                    "rti-cross-venue-consensus-v1"
                ),
                "rti_cross_venue_time_basis": "local_created_at",
                "rti_cross_venue_status": "missing",
                "rti_cross_venue_missing_reason": (
                    f"{type(exc).__name__}: {exc}"[:200]
                ),
                "rti_cross_venue_evidence_cutoff_at": pending.captured_at,
            }
        with self._lock:
            if result.get("rti_cross_venue_status") == "ok":
                self._cross_venue_ok += 1
            else:
                self._cross_venue_missing += 1
            if result.get("rti_cross_asset_status") == "ok":
                self._cross_asset_ok += 1
            else:
                self._cross_asset_missing += 1
            if result.get("rti_independent_path_status") == "ok":
                self._independent_path_ok += 1
            else:
                self._independent_path_missing += 1
        return result

    @staticmethod
    def _rti_spot_lead_lag(
        features: Mapping[str, Any], spot: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Freeze decision-time spot-versus-index lead/lag evidence."""
        base = {
            "rti_spot_lead_lag_schema_version": (
                RTI_SPOT_LEAD_LAG_SCHEMA_VERSION
            ),
            "rti_spot_lead_lag_status": "missing",
            "rti_spot_lead_lag_missing_reason": None,
            "rti_spot_basis_bps": None,
            "rti_spot_basis_start_60s_bps": None,
            "rti_spot_basis_change_60s_bps": None,
            "rti_index_move_bps_60s": None,
            "rti_spot_move_bps_60s": None,
            "rti_spot_minus_index_momentum_bps_60s": None,
        }
        reasons = []
        if spot.get("spot_depth_status") != "ok":
            reasons.append("SPOT_CONTEXT_MISSING")
        if spot.get("spot_mid_path_schema_version") != (
            "spot-mid-path-local-v1"
        ):
            reasons.append("SPOT_PATH_SCHEMA_MISMATCH")
        if spot.get("spot_mid_path_time_basis") != "local_created_at":
            reasons.append("SPOT_PATH_TIME_BASIS_MISMATCH")
        if spot.get("spot_mid_window_complete_60s") is not True:
            reasons.append("SPOT_PATH_60S_INCOMPLETE")
        captured = _num(spot.get("spot_mid_path_captured_at"))
        evidence_as_of = _num(spot.get("rti_spot_evidence_as_of"))
        history_started = _num(spot.get("spot_mid_history_started_at"))
        history_seconds = _num(spot.get("spot_mid_history_seconds"))
        retention = _num(spot.get("spot_mid_history_retention_seconds"))
        interval = _num(spot.get("spot_mid_record_interval_seconds"))
        path_start_at = _num(spot.get("spot_mid_path_start_at_60s"))
        path_end_at = _num(spot.get("spot_mid_path_end_at_60s"))
        max_gap = _num(spot.get("spot_mid_path_max_gap_seconds_60s"))
        if any(value is None for value in (
            captured, evidence_as_of, history_started, history_seconds,
            retention, interval, path_start_at, path_end_at, max_gap,
        )):
            reasons.append("SPOT_PATH_TIMESTAMP_EVIDENCE_MISSING")
        else:
            if captured > evidence_as_of or evidence_as_of - captured > 3.0:
                reasons.append("SPOT_PATH_CAPTURE_NOT_DECISION_TIME")
            if history_seconds < 60.0 or retention < 60.0:
                reasons.append("SPOT_PATH_HISTORY_BELOW_60S")
            if history_started > captured - 60.0:
                reasons.append("SPOT_PATH_HISTORY_START_CONTRADICTION")
            if path_start_at > captured - 60.0:
                reasons.append("SPOT_PATH_WINDOW_START_CONTRADICTION")
            if abs(path_end_at - captured) > 1e-6:
                reasons.append("SPOT_PATH_WINDOW_END_CONTRADICTION")
            if interval <= 0.0 or max_gap > max(3.0, interval * 2.0):
                reasons.append("SPOT_PATH_CONTINUITY_CONTRADICTION")
        index_start = _num(features.get("rti_path_start_px"))
        index_end = _num(features.get("rti_path_end_px"))
        spot_start = _num(spot.get("spot_mid_start_60s"))
        spot_end = _num(spot.get("spot_mid_end_60s"))
        if any(value is None or value <= 0.0 for value in (
            index_start, index_end, spot_start, spot_end,
        )):
            reasons.append("PRICE_INPUT_MISSING")
        if reasons:
            return {
                **base,
                "rti_spot_lead_lag_missing_reason": ",".join(reasons),
            }
        index_move = (index_end / index_start - 1.0) * 10_000.0
        spot_move = (spot_end / spot_start - 1.0) * 10_000.0
        basis_start = (spot_start / index_start - 1.0) * 10_000.0
        basis_end = (spot_end / index_end - 1.0) * 10_000.0
        return {
            **base,
            "rti_spot_lead_lag_status": "ok",
            "rti_spot_basis_bps": basis_end,
            "rti_spot_basis_start_60s_bps": basis_start,
            "rti_spot_basis_change_60s_bps": basis_end - basis_start,
            "rti_index_move_bps_60s": index_move,
            "rti_spot_move_bps_60s": spot_move,
            "rti_spot_minus_index_momentum_bps_60s": (
                spot_move - index_move
            ),
        }

    @staticmethod
    def _side_quote(quote: Mapping[str, Any], side: str | None) -> dict[str, Any]:
        yes_bid = _num(quote.get("yes_bid_cents"))
        yes_ask = _num(quote.get("yes_ask_cents"))
        no_bid = _num(quote.get("no_bid_cents"))
        no_ask = _num(quote.get("no_ask_cents"))
        yes_bid_depth = _num(quote.get("yes_bid_qty"))
        yes_ask_depth = _num(quote.get("yes_ask_qty"))
        no_bid_depth = yes_ask_depth
        no_ask_depth = yes_bid_depth
        if side == "YES":
            bid, ask, depth = yes_bid, yes_ask, yes_ask_depth
            opposite_side = "NO"
            opposite_ask = no_ask
            opposite_depth = no_ask_depth
        elif side == "NO":
            bid, ask, depth = no_bid, no_ask, no_ask_depth
            opposite_side = "YES"
            opposite_ask = yes_ask
            opposite_depth = yes_ask_depth
        else:
            bid = ask = depth = None
            opposite_side = None
            opposite_ask = None
            opposite_depth = None
        spread = None if bid is None or ask is None else ask - bid
        market_mid = None if bid is None or ask is None else (bid + ask) / 2.0
        microstructure = {
            "kalshi_microstructure_schema_version": (
                RTI_EXACT_MICROSTRUCTURE_SCHEMA_VERSION
            ),
            "kalshi_microstructure_extension_schema_version": quote.get(
                "microstructure_extension_schema_version"
            ),
            "kalshi_microstructure_captured_at": _num(quote.get("captured_at")),
            "kalshi_microstructure_time_basis": quote.get(
                "microstructure_time_basis"
            ),
            "kalshi_history_count_capped": (
                bool(quote.get("history_count_capped"))
                if "history_count_capped" in quote
                else None
            ),
            "kalshi_book_event_retention_seconds": _num(
                quote.get("book_event_retention_seconds")
            ),
            "kalshi_trade_retention_seconds": _num(
                quote.get("trade_retention_seconds")
            ),
            "kalshi_book_history_started_at": _num(
                quote.get("book_history_started_at")
            ),
            "kalshi_trade_history_started_at": _num(
                quote.get("trade_history_started_at")
            ),
            "kalshi_book_history_seconds": _num(
                quote.get("book_history_seconds")
            ),
            "kalshi_trade_history_seconds": _num(
                quote.get("trade_history_seconds")
            ),
            "kalshi_yes_microprice_cents": _num(
                quote.get("yes_microprice_cents")
            ),
            "kalshi_yes_microprice_edge_cents": _num(
                quote.get("yes_microprice_edge_cents")
            ),
        }
        for horizon in (5, 15, 30, 60):
            suffix = f"_{horizon}s"
            for metric in (
                "book_window_complete",
                "trade_window_complete",
                "microstructure_window_complete",
            ):
                key = f"{metric}{suffix}"
                microstructure[f"kalshi_{key}"] = (
                    bool(quote.get(key)) if key in quote else None
                )
            for metric in (
                "event_count",
                "trade_count",
                "book_delta_pressure_yes",
                "trade_imbalance_yes",
                "taker_yes_volume",
                "taker_no_volume",
                "taker_net_yes_volume",
                "yes_best_depletion",
                "no_best_depletion",
                "yes_best_refill",
                "no_best_refill",
            ):
                microstructure[f"kalshi_{metric}{suffix}"] = _num(
                    quote.get(f"{metric}{suffix}")
                )
            for metric in (
                "book_add_volume_yes",
                "book_remove_volume_yes",
                "book_add_volume_no",
                "book_remove_volume_no",
                "microprice_change_cents",
                "microprice_range_cents",
                "microprice_variation_cents",
                "microprice_trend_efficiency",
                "trade_yes_price_change_cents",
                "trade_yes_price_range_cents",
                "trade_yes_price_variation_cents",
                "trade_yes_price_trend_efficiency",
                "trade_yes_vwap_cents",
            ):
                microstructure[f"kalshi_{metric}{suffix}"] = _num(
                    quote.get(f"{metric}{suffix}")
                )
        return {
            "rti_quote_source_side": side,
            "rti_quote_inverted": False,
            "entry_ask_cents": ask,
            "yes_bid_cents": bid,
            "yes_ask_cents": ask,
            "spread_cents": spread,
            "depth_contracts": depth,
            "rti_market_mid_probability": (
                None if market_mid is None else market_mid / 100.0
            ),
            "rti_opposite_side": opposite_side,
            "rti_opposite_ask_cents": opposite_ask,
            "rti_opposite_depth_contracts": opposite_depth,
            "quote_age_seconds": _num(quote.get("book_age_seconds")),
            "quote_age_source": "kalshi_ws_exact_sampler",
            "kalshi_depth_status": "ok" if quote.get("available") else "missing",
            "kalshi_depth_missing_reason": quote.get("reason"),
            "yes_bid_depth_contracts": yes_bid_depth,
            "yes_ask_depth_contracts": yes_ask_depth,
            "no_bid_depth_contracts": no_bid_depth,
            "no_ask_depth_contracts": no_ask_depth,
            **microstructure,
        }

    def _build_source(
        self,
        pending: _Pending,
        path: Mapping[str, Any],
        *,
        evidence_as_of: float,
        recorded_at: float,
    ) -> dict[str, Any]:
        from q15_upgrade.rti_path_13m import (
            build_rti_path_features,
            classify_rti_point_in_time_risk,
        )
        from q15_upgrade.strategy_bots.rules import rti_path_13m_rule_version

        market = pending.market
        features = build_rti_path_features(path, strike=market.strike)
        spot = dict(pending.spot_context or {})
        cross_venue = self._capture_cross_venue_context(pending, spot)
        evidence_as_of = max(
            evidence_as_of,
            _num(spot.get("rti_spot_evidence_as_of")) or evidence_as_of,
        )
        side = str(features.get("rti_side") or "").upper() or None
        quote = self._side_quote(pending.quote, side)
        lead_lag = self._rti_spot_lead_lag(features, spot)
        risk = classify_rti_point_in_time_risk({**features, **quote})
        version = rti_path_13m_rule_version(market.asset)
        return {
            "created_at": recorded_at,
            "source_captured_at": pending.captured_at,
            "quote_captured_at": pending.captured_at,
            "rti_evaluated_at": evidence_as_of,
            "capture_mode": "kalshi_ws_exact_13m",
            "model_version": version,
            "asset": market.asset,
            "ticker": market.ticker,
            "interval": "13M",
            "window_key": int(market.close_time // 900),
            "close_time": market.close_time,
            "record_kind": "RTI_PATH_13M_PROSPECTIVE_EXACT",
            "rule_code": version,
            "delivery_status": "PAPER_PROSPECTIVE",
            "predicted_side": side,
            "rti_timing_offset_s": pending.captured_at - market.decision_time,
            "rti_path_evaluation_delay_s": evidence_as_of - market.decision_time,
            "rti_storage_delay_s": recorded_at - market.decision_time,
            **features,
            **quote,
            **spot,
            **lead_lag,
            **cross_venue,
            **risk,
        }

    def _build_confirmation_source(
        self,
        pending: _ConfirmationPending,
        path: Mapping[str, Any],
        *,
        evidence_as_of: float,
        recorded_at: float,
    ) -> dict[str, Any]:
        market = pending.market
        original = pending.original_source
        original_side = str(original.get("rti_side") or "").upper()
        if original_side not in {"YES", "NO"}:
            original_side = ""
        raw_rows = path.get("rows")
        rows = (
            raw_rows
            if isinstance(raw_rows, list)
            else list(raw_rows)
            if isinstance(raw_rows, tuple)
            else []
        )
        prices = [
            price
            for row in rows
            if isinstance(row, Mapping)
            for price in [_num(row.get("index_px"))]
            if price is not None
        ]
        end_px = prices[-1] if prices else None
        original_end_px = _num(original.get("rti_path_end_px"))
        confirmation_side = (
            None
            if end_px is None
            else "YES" if end_px >= market.strike else "NO"
        )
        sign = 1.0 if original_side == "YES" else -1.0 if original_side == "NO" else None
        continuation_bps = (
            None
            if sign is None
            or end_px is None
            or original_end_px is None
            or original_end_px == 0.0
            else sign * (end_px / original_end_px - 1.0) * 10_000.0
        )
        signed_distance_bps = (
            None
            if sign is None or end_px is None or market.strike == 0.0
            else sign * (end_px / market.strike - 1.0) * 10_000.0
        )
        spot = dict(pending.spot_context or {})
        evidence_as_of = max(
            evidence_as_of,
            _num(spot.get("rti_spot_evidence_as_of")) or evidence_as_of,
        )
        quote = self._side_quote(pending.quote or {}, original_side or None)
        return {
            "created_at": recorded_at,
            "source_captured_at": pending.captured_at,
            "evidence_as_of": evidence_as_of,
            "quote_captured_at": pending.captured_at,
            "model_version": original.get("model_version"),
            "asset": market.asset,
            "ticker": market.ticker,
            "interval": pending.policy.interval,
            "window_key": int(market.close_time // 900),
            "close_time": market.close_time,
            "record_kind": pending.policy.record_kind,
            "rule_code": pending.policy.policy_version,
            "delivery_status": "PAPER_RESEARCH_RECORD_ONLY",
            "predicted_side": original_side or None,
            "capture_mode": pending.policy.capture_mode,
            "rti_index_id": path.get("index_id"),
            "rti_confirm_target_at": pending.target_at,
            "rti_confirm_delay_seconds": pending.policy.delay_seconds,
            "rti_confirm_quote_captured_at": pending.captured_at,
            "rti_confirm_evaluated_at": evidence_as_of,
            "rti_confirm_recorded_at": recorded_at,
            "rti_confirm_timing_offset_s": (
                None
                if pending.captured_at is None
                else pending.captured_at - pending.target_at
            ),
            "rti_confirm_evaluation_delay_s": evidence_as_of - pending.target_at,
            "rti_confirm_storage_delay_s": recorded_at - pending.target_at,
            "rti_confirm_original_row_id": pending.original_row_id,
            "rti_confirm_original_strict_accepted": (
                pending.original_strict_accepted
            ),
            "rti_confirm_original_side": original_side or None,
            "rti_confirm_original_end_px": original_end_px,
            "rti_confirm_side": confirmation_side,
            "rti_confirm_end_px": end_px,
            "rti_confirm_continuation_bps": continuation_bps,
            "rti_confirm_signed_distance_bps": signed_distance_bps,
            "rti_confirm_path_status": path.get("status"),
            "rti_confirm_path_missing_reason": path.get("missing_reason"),
            "rti_confirm_path_expected_count": path.get("expected_count"),
            "rti_confirm_path_count": path.get("count"),
            "rti_confirm_path_complete": bool(path.get("complete")),
            "rti_confirm_path_missing_seconds": path.get("missing_seconds"),
            "rti_confirm_path_max_receive_age_s": path.get("max_receive_age_s"),
            "rti_confirm_path_decision_age_s": path.get("decision_age_s"),
            **quote,
            **spot,
        }

    def _record(self, source: Mapping[str, Any]) -> int | None:
        recorder = self._recorder
        if recorder is None:
            from q15_upgrade.strategy_bots.runtime import record_rti_path_13m_row

            recorder = record_rti_path_13m_row
            self._recorder = recorder
        return recorder(source)

    def _record_confirmation(self, source: Mapping[str, Any]) -> int | None:
        recorder = self._confirmation_recorder
        if recorder is None:
            from q15_upgrade.strategy_bots.runtime import (
                record_rti_delayed_confirmation_row,
            )

            recorder = record_rti_delayed_confirmation_row
            self._confirmation_recorder = recorder
        return recorder(source)

    @staticmethod
    def _confirmation_key(
        pending: _ConfirmationPending,
    ) -> tuple[str, str]:
        return pending.market.ticker, pending.policy.challenger_id

    def _confirmation_pending_labels(self) -> list[str]:
        return sorted(
            f"{pending.market.ticker}@+{int(pending.policy.delay_seconds)}s"
            for pending in self._confirmation_pending.values()
        )

    def _schedule_confirmation_stages(
        self,
        *,
        market: _Market,
        original_source: Mapping[str, Any],
        original_row_id: int,
        original_strict_accepted: bool,
        completed_intervals: set[str] | None = None,
    ) -> int:
        completed = completed_intervals or set()
        scheduled = 0
        with self._lock:
            for policy in _CONFIRMATION_POLICIES:
                if policy.interval in completed:
                    continue
                confirmation = _ConfirmationPending(
                    policy=policy,
                    market=market,
                    original_source=dict(original_source),
                    original_row_id=int(original_row_id),
                    original_strict_accepted=bool(original_strict_accepted),
                    target_at=market.decision_time + policy.delay_seconds,
                )
                key = self._confirmation_key(confirmation)
                if key not in self._confirmation_pending:
                    self._confirmation_pending[key] = confirmation
                    scheduled += 1
        return scheduled

    def _recover_confirmation_schedule(
        self,
        market: _Market,
        *,
        current: float,
    ) -> None:
        """Recreate missing delayed stages from a durable exact parent."""
        if (
            not self._confirmation_recovery_enabled
            or current < market.decision_time
            or current >= market.close_time
        ):
            return
        with self._lock:
            if (
                market.ticker in self._confirmation_recovery_checked
                or market.ticker in self._done
                or market.ticker in self._pending
                or any(key[0] == market.ticker for key in self._confirmation_pending)
            ):
                return
            last_attempt = self._confirmation_recovery_last_attempt.get(
                market.ticker
            )
            if last_attempt is not None and current - last_attempt < 1.0:
                return
            self._confirmation_recovery_last_attempt[market.ticker] = current
        reader = self._confirmation_recovery_reader
        if reader is None:
            from q15_upgrade.strategy_bots.runtime import (
                rti_delayed_confirmation_recovery_state,
            )

            reader = rti_delayed_confirmation_recovery_state
            self._confirmation_recovery_reader = reader
        try:
            raw_state = reader(
                ticker=market.ticker,
                close_time=market.close_time,
            )
            state = dict(raw_state) if isinstance(raw_state, Mapping) else None
        except Exception as exc:  # noqa: BLE001 - recovery retries fail closed
            with self._lock:
                self._confirmation_recovery_failures += 1
                self._last_confirmation_error = (
                    f"confirmation_recovery:{type(exc).__name__}: {exc}"
                )
            logger.warning(
                "delayed RTI confirmation recovery read failed", exc_info=True
            )
            return
        if state is None:
            # A normal exact capture can still be committing during the first
            # second.  Retry briefly, then stop querying an absent parent.
            if current - market.decision_time > 5.0:
                with self._lock:
                    self._confirmation_recovery_checked.add(market.ticker)
            return
        original_source = state.get("original_source")
        row_id = int(_num(state.get("parent_row_id")) or 0)
        if not isinstance(original_source, Mapping) or row_id <= 0:
            with self._lock:
                self._confirmation_recovery_failures += 1
                self._confirmation_recovery_checked.add(market.ticker)
                self._last_confirmation_error = (
                    "confirmation_recovery_invalid_parent_state"
                )
            return
        completed = {
            str(interval or "").upper()
            for interval in (state.get("completed_intervals") or ())
        }
        scheduled = self._schedule_confirmation_stages(
            market=market,
            original_source=original_source,
            original_row_id=row_id,
            original_strict_accepted=bool(
                state.get("parent_strict_accepted")
            ),
            completed_intervals=completed,
        )
        with self._lock:
            # The durable parent proves the exact stage already completed in a
            # prior process, so do not count it as a fresh deadline miss.
            self._done[market.ticker] = current
            self._pending.pop(market.ticker, None)
            self._confirmation_recovery_checked.add(market.ticker)
            self._confirmation_recovered_parents += 1
            self._confirmation_recovered_stages += scheduled
            self._last_confirmation_recovery_at = current
            self._last_confirmation_error = None

    def _schedule_confirmation(
        self,
        *,
        pending: _Pending,
        source: Mapping[str, Any],
        row_id: int,
    ) -> None:
        from q15_upgrade.strategy_bots.rules import (
            ACCEPTED,
            rti_path_13m_decision,
        )

        original_strict_accepted = (
            rti_path_13m_decision(source).decision_status == ACCEPTED
        )
        self._schedule_confirmation_stages(
            market=pending.market,
            original_source=source,
            original_row_id=row_id,
            original_strict_accepted=original_strict_accepted,
        )

    def _tick_confirmations(self, *, realtime: bool, current: float) -> None:
        """Capture all delayed quotes, then paths, then research rows."""
        with self._lock:
            confirmations = list(self._confirmation_pending.values())

        # Phase C1a: freeze every newly due Kalshi quote before any spot/path
        # call can delay another asset's executable entry evidence.
        newly_captured: list[_ConfirmationPending] = []
        forced_missing: list[tuple[_ConfirmationPending, dict[str, Any], float]] = []
        for pending in confirmations:
            if pending.captured_at is not None:
                continue
            capture_at = time.time() if realtime else current
            if capture_at < pending.target_at:
                continue
            confirmation_key = self._confirmation_key(pending)
            if capture_at - pending.target_at > self.max_timing_offset_s:
                pending.captured_at = capture_at
                pending.quote = {
                    "available": False,
                    "reason": "delayed_confirmation_quote_deadline_missed",
                    "captured_at": capture_at,
                }
                missing_path = {
                    "status": "missing",
                    "missing_reason": "delayed_confirmation_deadline_missed",
                    "index_id": None,
                    "expected_count": pending.policy.expected_count,
                    "count": 0,
                    "complete": False,
                    "missing_seconds": [],
                    "max_receive_age_s": None,
                    "decision_age_s": None,
                    "rows": [],
                }
                with self._lock:
                    if self._confirmation_quote_retry_pending.pop(
                        confirmation_key, None
                    ) is not None:
                        self._confirmation_quote_retry_exhausted += 1
                    self._confirmation_missed_deadlines += 1
                    self._last_confirmation_error = (
                        "delayed_confirmation_quote_deadline_missed"
                    )
                forced_missing.append((pending, missing_path, capture_at))
                continue
            quote = self._capture_quote(pending.market, capture_at)
            retry_cutoff_s = max(
                0.0,
                self.max_timing_offset_s
                - min(0.25, max(0.10, self.poll_seconds * 2.0)),
            )
            quote_usable = self._quote_has_executable_book(quote)
            if (
                not quote_usable
                and capture_at - pending.target_at < retry_cutoff_s
            ):
                with self._lock:
                    self._confirmation_quote_retry_pending[confirmation_key] = (
                        self._confirmation_quote_retry_pending.get(
                            confirmation_key, 0
                        ) + 1
                    )
                    self._confirmation_quote_retry_attempts += 1
                continue
            pending.captured_at = capture_at
            pending.quote = quote
            with self._lock:
                prior_retries = self._confirmation_quote_retry_pending.pop(
                    confirmation_key, 0
                )
                if prior_retries:
                    if quote_usable:
                        self._confirmation_quote_retry_successes += 1
                    else:
                        self._confirmation_quote_retry_exhausted += 1
            newly_captured.append(pending)
            with self._lock:
                self._confirmation_quote_captures += 1
                self._last_confirmation_capture_at = capture_at

        # Phase C1b: quote timestamps are immutable; now freeze spot context.
        for pending in newly_captured:
            pending.spot_context = self._capture_spot_context(
                pending,
                evidence_as_of=pending.captured_at or pending.target_at,
                confirmation=True,
            )

        # Phase C2: read every due RTI path before the first SQLite write.
        ready = list(forced_missing)
        with self._lock:
            confirmations = list(self._confirmation_pending.values())
        for pending in confirmations:
            if pending.captured_at is None or any(
                forced is pending for forced, _, _ in forced_missing
            ):
                continue
            path_read_at = time.time() if realtime else current
            try:
                path = self._read_confirmation_path(pending, path_read_at)
                pending.last_path = path
                should_record = bool(path.get("complete")) or (
                    path_read_at - pending.target_at >= self.max_timing_offset_s
                )
                if not should_record:
                    continue
                if path_read_at - pending.last_record_attempt < 0.25:
                    continue
                pending.last_record_attempt = path_read_at
                evidence_as_of = max(
                    pending.captured_at,
                    path_read_at,
                    _num(
                        (pending.spot_context or {}).get("rti_spot_evidence_as_of")
                    )
                    or path_read_at,
                )
                pending.evidence_completed_at = evidence_as_of
                ready.append((pending, path, evidence_as_of))
            except Exception as exc:  # noqa: BLE001 - worker must stay alive
                with self._lock:
                    self._last_confirmation_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "delayed RTI confirmation path read failed", exc_info=True
                )

        # Phase C3: all entry/path evidence above is frozen before persistence.
        for pending, path, evidence_as_of in ready:
            try:
                recorded_at = time.time() if realtime else current
                source = self._build_confirmation_source(
                    pending,
                    path,
                    evidence_as_of=evidence_as_of,
                    recorded_at=recorded_at,
                )
                row_id = self._record_confirmation(source)
                if row_id is None:
                    with self._lock:
                        self._confirmation_record_failures += 1
                        self._last_confirmation_error = (
                            "delayed_confirmation_record_returned_none"
                        )
                    continue
                with self._lock:
                    self._confirmation_pending.pop(
                        self._confirmation_key(pending), None
                    )
                    self._confirmation_decisions_recorded += 1
                    self._last_confirmation_recorded_at = recorded_at
                    self._last_confirmation_error = None
            except Exception as exc:  # noqa: BLE001 - worker must stay alive
                with self._lock:
                    self._last_confirmation_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "delayed RTI confirmation record failed", exc_info=True
                )

    def tick(self, now: float | None = None) -> None:
        """Run one deterministic scheduler iteration (public for tests)."""
        if not self.enabled:
            return
        realtime = now is None
        current = time.time() if realtime else float(now)
        with self._lock:
            markets = list(self._markets.values())

        # Phase 1: freeze every due market's quote and spot context before any
        # path read, ledger enrichment, SQLite write, or Telegram enqueue can
        # delay a later asset.  This preserves a real per-asset capture time.
        newly_captured_primary: list[_Pending] = []
        for market in markets:
            capture_at = time.time() if realtime else current
            _, pending = self._capture_primary_quote_attempt(
                market, capture_at
            )
            if pending is not None:
                newly_captured_primary.append(pending)

        # A stale/one-sided book gets the full bounded retry interval before
        # any slower spot, path, SQLite, or Telegram work can occupy this
        # worker.  Deterministic tests with an injected ``now`` retain their
        # explicit one-attempt-per-tick semantics.
        if realtime:
            self._drain_primary_quote_retries(
                markets, newly_captured_primary
            )

        # Quotes for the complete chronological fold are frozen first.  Spot
        # capture may touch multiple venues and must not age a later asset's
        # Kalshi entry timestamp.
        for pending in newly_captured_primary:
            pending.spot_context = self._capture_spot_context(
                pending,
                evidence_as_of=pending.captured_at,
            )

        # Phase 2: read every pending exact path before any row is persisted.
        # Path-reader/SQLite latency for one asset cannot age the evidence for
        # the remaining six assets.
        ready: list[tuple[_Pending, dict[str, Any], float]] = []
        for market in markets:
            with self._lock:
                if market.ticker in self._done:
                    continue
                pending = self._pending.get(market.ticker)
            if pending is None:
                continue
            decision_time = market.decision_time
            path_read_at = time.time() if realtime else current
            try:
                path = self._read_path(market, path_read_at)
                pending.last_path = path
                should_record = bool(path.get("complete")) or (
                    path_read_at - decision_time >= self.max_timing_offset_s
                )
                if not should_record:
                    continue
                if path_read_at - pending.last_record_attempt < 0.25:
                    continue
                pending.last_record_attempt = path_read_at
                evidence_as_of = max(
                    pending.captured_at,
                    path_read_at,
                    _num(
                        (pending.spot_context or {}).get("rti_spot_evidence_as_of")
                    )
                    or path_read_at,
                )
                pending.evidence_completed_at = evidence_as_of
                ready.append((pending, path, evidence_as_of))
            except Exception as exc:  # noqa: BLE001 - worker must stay alive
                with self._lock:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("exact RTI 13M sampler path read failed", exc_info=True)

        # Phase 3: persistence may be slow, but all market evidence above is
        # already immutable and carries its genuine point-in-time timestamps.
        for pending, path, evidence_as_of in ready:
            market = pending.market
            try:
                recorded_at = time.time() if realtime else current
                source = self._build_source(
                    pending,
                    path,
                    evidence_as_of=evidence_as_of,
                    recorded_at=recorded_at,
                )
                row_id = self._record(source)
                if row_id is None:
                    with self._lock:
                        self._record_failures += 1
                        self._last_error = "exact_sampler_record_returned_none"
                    continue
                self._schedule_confirmation(
                    pending=pending,
                    source=source,
                    row_id=int(row_id),
                )
                with self._lock:
                    self._done[market.ticker] = recorded_at
                    self._pending.pop(market.ticker, None)
                    self._decisions_recorded += 1
                    self._last_recorded_at = recorded_at
                    self._last_error = None
            except Exception as exc:  # noqa: BLE001 - worker must stay alive
                with self._lock:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("exact RTI 13M sampler tick failed", exc_info=True)

        self._tick_confirmations(realtime=realtime, current=current)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.poll_seconds)

    def health(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            markets = list(self._markets.values())
            thread_alive = bool(self._thread and self._thread.is_alive())
            return {
                "enabled": self.enabled,
                "read_only": True,
                "paper_only": True,
                "thread_alive": thread_alive,
                "poll_seconds": self.poll_seconds,
                "max_timing_offset_seconds": self.max_timing_offset_s,
                "registered_assets": sorted(m.asset for m in markets),
                "pending_tickers": sorted(self._pending),
                "delayed_confirmation_pending_tickers": (
                    self._confirmation_pending_labels()
                ),
                "seconds_to_decision_by_asset": {
                    m.asset: round(m.decision_time - now, 3) for m in markets
                },
                "quote_captures": self._quote_captures,
                "quote_retry_attempts": self._quote_retry_attempts,
                "quote_retry_successes": self._quote_retry_successes,
                "quote_retry_exhausted": self._quote_retry_exhausted,
                "quote_retry_drain_cycles": self._quote_retry_drain_cycles,
                "quote_retry_pending_tickers": sorted(
                    self._quote_retry_pending
                ),
                "recent_retry_exhausted_tickers": list(
                    self._recent_retry_exhausted_tickers
                ),
                "recent_missed_tickers": list(
                    self._recent_missed_tickers
                ),
                "last_quote_failure_reason_by_ticker": dict(
                    self._last_quote_failure_reason_by_ticker
                ),
                "decisions_recorded": self._decisions_recorded,
                "missed_deadlines": self._missed_deadlines,
                "record_failures": self._record_failures,
                "spot_context_ok": self._spot_context_ok,
                "spot_context_missing": self._spot_context_missing,
                "cross_venue_ok": self._cross_venue_ok,
                "cross_venue_missing": self._cross_venue_missing,
                "cross_asset_ok": self._cross_asset_ok,
                "cross_asset_missing": self._cross_asset_missing,
                "independent_path_ok": self._independent_path_ok,
                "independent_path_missing": self._independent_path_missing,
                "independent_path_source": {
                    "design_id": RTI_INDEPENDENT_PATH_DESIGN_ID,
                    "design_sha256": RTI_INDEPENDENT_PATH_DESIGN_SHA256,
                    "prospective_after_close_time": (
                        RTI_INDEPENDENT_PATH_PROSPECTIVE_AFTER_CLOSE_TIME
                    ),
                    "first_eligible_close_time": (
                        RTI_INDEPENDENT_PATH_FIRST_ELIGIBLE_CLOSE_TIME
                    ),
                    "paper_only": True,
                    "outcome_labels_read": False,
                    "model_fit_performed": False,
                    "notification_eligible": False,
                    "automatic_scoring": False,
                    "automatic_promotion": False,
                    "real_trading_allowed": False,
                    "captures_ok": self._independent_path_ok,
                    "captures_missing": self._independent_path_missing,
                },
                "delayed_confirmation": {
                    "id": RTI_PATH_13M_DELAYED_CONFIRM_CHALLENGER_ID,
                    "policy_version": RTI_PATH_13M_DELAYED_CONFIRM_POLICY_VERSION,
                    "delay_seconds": RTI_PATH_13M_DELAYED_CONFIRM_SECONDS,
                    "paper_only": True,
                    "notification_eligible": False,
                    "historical_credit_allowed": False,
                    "manual_review_bars": [30, 60, 150],
                    "policies": [
                        {
                            "id": policy.challenger_id,
                            "policy_version": policy.policy_version,
                            "delay_seconds": policy.delay_seconds,
                            "expected_path_count": policy.expected_count,
                            "interval": policy.interval,
                        }
                        for policy in _CONFIRMATION_POLICIES
                    ],
                    "pending_tickers": self._confirmation_pending_labels(),
                    "quote_captures": self._confirmation_quote_captures,
                    "quote_retry_attempts": (
                        self._confirmation_quote_retry_attempts
                    ),
                    "quote_retry_successes": (
                        self._confirmation_quote_retry_successes
                    ),
                    "quote_retry_exhausted": (
                        self._confirmation_quote_retry_exhausted
                    ),
                    "quote_retry_pending_tickers": sorted(
                        f"{ticker}@{policy_id}"
                        for ticker, policy_id in (
                            self._confirmation_quote_retry_pending
                        )
                    ),
                    "decisions_recorded": self._confirmation_decisions_recorded,
                    "missed_deadlines": self._confirmation_missed_deadlines,
                    "record_failures": self._confirmation_record_failures,
                    "spot_context_ok": self._confirmation_spot_context_ok,
                    "spot_context_missing": (
                        self._confirmation_spot_context_missing
                    ),
                    "restart_recovery_enabled": (
                        self._confirmation_recovery_enabled
                    ),
                    "recovered_parents": (
                        self._confirmation_recovered_parents
                    ),
                    "recovered_stages": self._confirmation_recovered_stages,
                    "recovery_failures": (
                        self._confirmation_recovery_failures
                    ),
                    "last_recovery_at": self._last_confirmation_recovery_at,
                    "last_capture_at": self._last_confirmation_capture_at,
                    "last_recorded_at": self._last_confirmation_recorded_at,
                    "last_error": self._last_confirmation_error,
                },
                "spot_confirm_challenger": {
                    "id": RTI_PATH_13M_SPOT_CONFIRM_CHALLENGER_ID,
                    "paper_only": True,
                    "notification_eligible": False,
                    "review_bars": [30, 60, 150],
                },
                "last_capture_at": self._last_capture_at,
                "last_recorded_at": self._last_recorded_at,
                "last_timing_offset_seconds": self._last_timing_offset_s,
                "last_error": self._last_error,
            }


_sampler: ExactRTI13MSampler | None = None
_sampler_lock = threading.Lock()


def get_exact_rti_13m_sampler() -> ExactRTI13MSampler:
    global _sampler
    with _sampler_lock:
        if _sampler is None:
            _sampler = ExactRTI13MSampler()
        return _sampler


def start_exact_rti_13m_sampler() -> bool:
    return get_exact_rti_13m_sampler().start()


def register_exact_rti_13m_market(**kwargs: Any) -> bool:
    return get_exact_rti_13m_sampler().register_market(**kwargs)


def exact_rti_13m_health() -> dict[str, Any]:
    sampler = _sampler
    if sampler is None:
        return {
            "enabled": _bool("Q15_V3_RTI_EXACT_SAMPLER", False),
            "read_only": True,
            "paper_only": True,
            "thread_alive": False,
            "registered_assets": [],
        }
    return sampler.health()


def reset_exact_rti_13m_sampler() -> None:
    """Test hook."""
    global _sampler
    with _sampler_lock:
        if _sampler is not None:
            _sampler.stop()
        _sampler = None
