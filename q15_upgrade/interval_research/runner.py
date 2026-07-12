"""Interval-timing research — live-loop observer (read-only, default-OFF).

Mirrors the Ultoim observer: invoked once per refresh from checkpoint_v95.run_cycle, it
captures the champion's frozen analysis at each of the eight marks (band around
each mark), deduped per (ticker, interval). It NEVER trades, sends, or alters the
champion. ``resolve_settled`` attaches official outcomes from the same settlement
events the champion already produces.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class _PreparedCapture:
    """Compact point-in-time capture candidate retained across one slow cycle."""

    asset: str
    ticker: str
    window_key: int
    interval: str
    mark_seconds: int
    seconds_remaining: float
    observed_at: float
    close_time: float | None
    missing_reason: str | None
    row: dict[str, Any] | None
    capture_analysis: dict[str, Any]


@dataclass
class _PendingDriftWindow:
    """Bounded cross-asset barrier for one 13M Drift slate."""

    first_seen_at: float
    fallback_close: float | None
    fallback_at: float
    expected_tickers: set[str] = field(default_factory=set)
    accounted_tickers: set[str] = field(default_factory=set)


class IntervalResearchRunner:
    def __init__(self, config: IntervalResearchConfig | None = None):
        self.config = config or IntervalResearchConfig.from_env()
        self.ledger = IntervalResearchLedger(self.config.db_path)
        self._lock = threading.Lock()
        # top-pick-13M one-shot state: window_key -> decided-at timestamp
        self._top_pick_done: dict[int, float] = {}
        # At most one compact 13M candidate per asset.  A same-contract downward
        # threshold crossing can choose the nearer endpoint when a 30-80s refresh
        # skips the normal capture band.  This state is intentionally in-memory:
        # a cold restart below the band must not fabricate an unseen 13M point.
        self._last_13m_candidate: dict[str, _PreparedCapture] = {}
        # A staggered refresh can put one mapped contract below 780s while
        # another is still above it. Accumulate behind a bounded barrier so
        # Drift never freezes the first partial slate.
        self._pending_drift_13m: dict[int, _PendingDriftWindow] = {}
        self._drift_13m_done: dict[int, float] = {}
        # Restart repair runs once per process unless a new source-capture event
        # explicitly asks for another idempotent checkpoint replay.
        self._drift_checkpoint_replayed: set[tuple[str, int]] = set()
        self._drift_strategy_reconciled = False

    @property
    def model_version(self) -> str:
        return self.config.model_version

    def _prepare_capture(
        self,
        *,
        asset: str,
        analysis: Mapping[str, Any],
        canonical: Any,
        ticker: str,
        window_key_value: int,
        interval: str,
        mark_seconds: int,
        seconds_remaining: float,
        observed_at: float,
    ) -> _PreparedCapture:
        """Freeze the compact fields needed if a later cycle crosses a mark.

        Settlement-index context is captured now as well.  Looking it up only
        after a later crossing would pair an older quote/model snapshot with a
        newer index observation.
        """
        reason = missing_reason(analysis, canonical, self.config.min_data_quality)
        capture_analysis = dict(analysis)
        row: dict[str, Any] | None = None
        if reason is None:
            try:
                from settlement_index import settlement_index_context

                capture_analysis["settlement_index"] = settlement_index_context(
                    asset, spot_px=getattr(canonical, "spot", None), now=observed_at
                )
            except Exception:  # noqa: BLE001 - record-only context must not break capture
                capture_analysis["settlement_index"] = {
                    "index_px": None,
                    "basis_cents": None,
                    "index_age_s": None,
                    "index_status": "error",
                    "index_missing_reason": "settlement_index_context_error",
                    "index_id": None,
                    "index_source_ts": None,
                }
            row = build_capture_row(
                model_version=self.config.model_version,
                interval=interval,
                mark_seconds=mark_seconds,
                asset=asset,
                analysis=capture_analysis,
                canonical=canonical,
                window_key=window_key_value,
                now=observed_at,
            )
        return _PreparedCapture(
            asset=asset,
            ticker=ticker,
            window_key=window_key_value,
            interval=interval,
            mark_seconds=mark_seconds,
            seconds_remaining=seconds_remaining,
            observed_at=observed_at,
            close_time=_num(getattr(canonical, "settlement_time", None)),
            missing_reason=reason,
            row=row,
            capture_analysis=capture_analysis,
        )

    def _record_prepared(self, candidate: _PreparedCapture) -> bool:
        """Persist one prepared capture/missing row; return whether it was new."""
        if self.ledger.has_row(
            self.config.model_version, candidate.ticker, candidate.interval
        ):
            return False
        if candidate.missing_reason is not None:
            return self.ledger.record_missing(
                model_version=self.config.model_version,
                ticker=candidate.ticker,
                asset=candidate.asset,
                interval=candidate.interval,
                reason=candidate.missing_reason,
                window_key=candidate.window_key,
                captured_at=candidate.observed_at,
            )
        if candidate.row is None:
            return False
        wrote = self.ledger.record_capture(candidate.row)
        if wrote:
            self._feed_v3_marks(candidate.row, candidate.capture_analysis)
        return wrote

    def _select_13m_capture(
        self, asset: str, current: _PreparedCapture
    ) -> _PreparedCapture | None:
        """Select the normal in-band point or the nearer endpoint of a crossing."""
        previous = self._last_13m_candidate.get(asset)
        self._last_13m_candidate[asset] = current
        mark = float(INTERVAL_MARKS["13M"])
        band = max(0.0, float(self.config.mark_band_seconds))
        if mark - band <= current.seconds_remaining <= mark:
            return current
        if previous is None:
            return None

        max_span = max(0.0, float(self.config.crossing_max_seconds))
        max_offset = max(0.0, float(self.config.crossing_max_offset_seconds))
        wall_gap = current.observed_at - previous.observed_at
        clock_drop = previous.seconds_remaining - current.seconds_remaining
        crossed = (
            previous.ticker == current.ticker
            and previous.window_key == current.window_key
            and 0.0 < wall_gap <= max_span
            and 0.0 < clock_drop <= max_span
            and previous.seconds_remaining > mark >= current.seconds_remaining
        )
        if not crossed:
            return None
        # Prefer the current endpoint on an exact tie.  Both the source row and
        # downstream point-in-time enrichment keep the selected endpoint's real
        # timestamp; nothing is relabelled with the nominal threshold time.
        selected = min(
            (current, previous),
            key=lambda item: abs(item.seconds_remaining - mark),
        )
        if abs(selected.seconds_remaining - mark) > max_offset:
            return None
        return selected

    @staticmethod
    def _slate_close_time(
        slate: Sequence[Mapping[str, Any]], fallback: float | None
    ) -> float | None:
        for row in slate:
            value = _num(row.get("close_time"))
            if value is not None:
                return value
        return fallback

    def observe(self, *, analyses: Mapping[str, Mapping[str, Any]],
                canonicals: Mapping[str, Any], now: float) -> None:
        """Capture every (asset, interval) whose seconds_remaining is in-band.

        Read-only and exception-isolated: a capture failure must never disturb the
        live cycle. Dedup is by the ledger's unique (model_version, ticker, interval)
        key, so repeated in-band cycles are no-ops after the first."""
        if not self.config.enabled or not self.ledger.available:
            return
        band = max(0.0, float(self.config.mark_band_seconds))
        try:
            with self._lock:
                drift_13m_targets: dict[int, tuple[float | None, float]] = {}
                drift_checkpoint_targets: dict[
                    tuple[str, int], tuple[float | None, float]
                ] = {}
                drift_checkpoint_intervals = {
                    interval for interval, _ in self._DRIFT_CKPT_MARKS
                }
                for asset, canonical in (canonicals or {}).items():
                    sr = _num(getattr(canonical, "seconds_remaining", None))
                    if sr is None:
                        continue
                    ticker = getattr(canonical, "ticker", None)
                    wk = window_key(getattr(canonical, "settlement_time", None), now)
                    analysis = analyses.get(asset) if isinstance(analyses, Mapping) else None
                    analysis = analysis if isinstance(analysis, Mapping) else {}

                    # 13M is crossing-aware. Retain only the immediately prior
                    # compact point, then choose the endpoint nearest 780s when a
                    # fresh same-market cycle skips the normal capture band.
                    mark_13m = INTERVAL_MARKS["13M"]
                    max_span = max(0.0, float(self.config.crossing_max_seconds))
                    in_13m_neighbourhood = (
                        (mark_13m - band) <= sr <= mark_13m
                        or abs(sr - mark_13m) <= max_span
                    )
                    if ticker and in_13m_neighbourhood:
                        current_13m = self._prepare_capture(
                            asset=str(asset), analysis=analysis, canonical=canonical,
                            ticker=str(ticker), window_key_value=wk, interval="13M",
                            mark_seconds=mark_13m, seconds_remaining=sr, observed_at=now,
                        )
                        selected_13m = self._select_13m_capture(str(asset), current_13m)
                        if selected_13m is not None:
                            self._record_prepared(selected_13m)
                            drift_13m_targets.setdefault(
                                selected_13m.window_key,
                                (selected_13m.close_time, selected_13m.observed_at),
                            )
                    elif ticker:
                        self._last_13m_candidate.pop(str(asset), None)

                    for interval, mark in INTERVAL_MARKS.items():
                        if interval == "13M":
                            continue
                        if not (mark - band) <= sr <= mark or not ticker:
                            continue
                        candidate = self._prepare_capture(
                            asset=str(asset), analysis=analysis, canonical=canonical,
                            ticker=str(ticker), window_key_value=wk, interval=interval,
                            mark_seconds=mark, seconds_remaining=sr, observed_at=now,
                        )
                        self._record_prepared(candidate)
                        if interval in drift_checkpoint_intervals:
                            drift_checkpoint_targets.setdefault(
                                (interval, wk), (candidate.close_time, now)
                            )
                self._maybe_top_pick_13m(canonicals, now)
                self._maybe_drift_shadow(canonicals, now, drift_13m_targets)
                self._maybe_drift_checkpoints(
                    canonicals, now, drift_checkpoint_targets
                )
        except Exception:  # never break the live loop
            logger.debug("interval-research observe failed (ignored)", exc_info=True)

    def _maybe_drift_shadow(
        self,
        canonicals: Mapping[str, Any],
        now: float,
        forced_targets: Mapping[int, tuple[float | None, float]] | None = None,
    ) -> None:
        """Feed a complete 13M slate after a bounded cross-asset barrier.

        A refresh can be staggered: SOL may already be at 776s while XRP is at
        781s.  The first source row is durable immediately, but Drift waits
        until every mapped ticker seen for that window has reached the mark and
        has either a capture/missing row, or is already cold-late below the
        capture band.  A disappeared ticker is released by the bounded timeout.

        Restart repair is source-ledger driven for the whole active window. It
        may replay a real persisted slate long after the old 45-second band,
        but it never manufactures a source capture.
        """
        mark = float(INTERVAL_MARKS["13M"])
        band = max(0.0, float(self.config.mark_band_seconds))
        max_wait = max(0.0, float(self.config.crossing_max_seconds))

        current_by_window: dict[int, list[tuple[str, float, float | None]]] = {}
        for canonical in (canonicals or {}).values():
            ticker = getattr(canonical, "ticker", None)
            sr = _num(getattr(canonical, "seconds_remaining", None))
            if not ticker or sr is None:
                continue
            wk = window_key(getattr(canonical, "settlement_time", None), now)
            current_by_window.setdefault(wk, []).append(
                (
                    str(ticker),
                    sr,
                    _num(getattr(canonical, "settlement_time", None)),
                )
            )

        targets = dict(forced_targets or {})
        # A durable source slate is enough to request replay anywhere in the
        # still-active market.  Merely being late is never enough to create one.
        for wk, current in current_by_window.items():
            if not any(0.0 < sr <= mark for _, sr, _ in current):
                continue
            persisted = self.ledger.captures_for_window(
                self.config.model_version, "13M", wk
            )
            if not persisted:
                continue
            close_time = next((close for _, _, close in current if close is not None), None)
            targets.setdefault(wk, (close_time, now))
        for wk, pending in self._pending_drift_13m.items():
            targets.setdefault(wk, (pending.fallback_close, pending.fallback_at))

        # Keep the small in-memory dedup maps bounded to recent windows.
        for wk, done_at in list(self._drift_13m_done.items()):
            if now - done_at > 1800.0:
                self._drift_13m_done.pop(wk, None)
        try:
            from q15_upgrade.drift_shadow import get_recorder

            rec = get_recorder()
        except Exception:  # noqa: BLE001 - optional research recorder
            rec = None
        for wk, (fallback_close, fallback_at) in targets.items():
            if wk in self._drift_13m_done:
                continue
            pending = self._pending_drift_13m.get(wk)
            if pending is None:
                pending = _PendingDriftWindow(
                    first_seen_at=now,
                    fallback_close=fallback_close,
                    fallback_at=fallback_at,
                )
                self._pending_drift_13m[wk] = pending
            elif pending.fallback_close is None and fallback_close is not None:
                pending.fallback_close = fallback_close

            expired = (
                pending.fallback_close is not None
                and now >= pending.fallback_close
            ) or now - pending.first_seen_at > max(1800.0, max_wait)
            if expired:
                self._pending_drift_13m.pop(wk, None)
                self._drift_13m_done[wk] = now
                continue

            for ticker, sr, _ in current_by_window.get(wk, ()):
                pending.expected_tickers.add(ticker)
                if sr > mark:
                    continue
                if self.ledger.has_row(self.config.model_version, ticker, "13M"):
                    pending.accounted_tickers.add(ticker)
                elif sr < mark - band:
                    # A cold process already below the capture band has no
                    # observed crossing to recover. Account it honestly rather
                    # than fabricating a 13M row or blocking a real replay.
                    pending.accounted_tickers.add(ticker)

            barrier_ready = pending.expected_tickers.issubset(
                pending.accounted_tickers
            )
            timed_out = now - pending.first_seen_at >= max_wait
            if not barrier_ready and not timed_out:
                continue

            slate = self.ledger.captures_for_window(
                self.config.model_version, "13M", wk
            )
            if not slate:
                # Missing-only/accounted windows have nothing for Drift to
                # score, but they are complete and must not loop forever.
                self._pending_drift_13m.pop(wk, None)
                self._drift_13m_done[wk] = now
                continue
            if rec is None:
                continue
            close_time = self._slate_close_time(slate, pending.fallback_close)
            rec.observe_window(
                model_version=self.config.model_version,
                window_key=wk,
                close_time=close_time,
                slate=slate,
                now=pending.fallback_at,
            )
            # Replay every source row, not only rows inserted by this call. A
            # crash after Drift commit but before the strategy adapter is then
            # repaired safely by the downstream durable claims.
            self._alert_drift_picks(rec, wk, pending.fallback_at, replay_all=True)
            self._alert_drift_asymmetric_candidates(
                rec, slate, wk, pending.fallback_at
            )
            self._alert_drift_no_expansion(
                rec, wk, pending.fallback_at, replay_all=True
            )
            self._pending_drift_13m.pop(wk, None)
            self._drift_13m_done[wk] = now

    def _alert_drift_picks(
        self, rec: Any, wk: int, now: float, *, replay_all: bool = False
    ) -> None:
        """Deliver a V3-channel card for each pick the recorder just wrote.

        The recorder stays record-only; this adapter reads its new rows and
        hands them to the strategy-bots delivery rail (durable per-(window,
        ticker) claim there, so restarts/re-observations never double-send).
        Never raises into the live loop."""
        try:
            if replay_all and hasattr(rec, "picks_for_window"):
                picks = rec.picks_for_window(self.config.model_version, wk)
            else:
                picks = rec.picks_recorded_at(self.config.model_version, wk, now)
            if not picks:
                return
            book = (rec.scoreboard() or {}).get("book_volume_60_73_all", {})
            ledger = getattr(self, "ledger", None)
            capture_rows = (
                ledger.captures_for_window(self.config.model_version, "13M", wk)
                if ledger is not None and hasattr(ledger, "captures_for_window")
                else []
            )
            captures = {
                str(item.get("ticker") or ""): item for item in capture_rows
            }
            wins = None
            if book.get("n_resolved") and book.get("win_rate") is not None:
                wins = round(float(book["win_rate"]) * int(book["n_resolved"]))
            from q15_upgrade.strategy_bots import runtime as strategy_bots_runtime
            for p in picks:
                capture = captures.get(str(p.get("ticker") or ""), {})
                source = {
                    "created_at": (
                        p.get("created_at")
                        if p.get("created_at") is not None else now
                    ),
                    "model_version": self.config.model_version,
                    "record_kind": "DRIFT_PICK_13M",
                    "asset": p.get("asset"),
                    "ticker": p.get("ticker"),
                    "interval": "13M",
                    "window_key": wk,
                    "close_time": p.get("close_time"),
                    "predicted_side": p.get("side"),
                    "distance_sigma": p.get("distance_sigma"),
                    "flip_probability": p.get("flip_probability"),
                    "entry_ask_cents": p.get("ask_cents"),
                    "spread_cents": p.get("spread_cents"),
                    "depth_contracts": p.get("depth_contracts"),
                    "drift_spread_weight": p.get("spread_weight"),
                    "drift_session_weight": p.get("session_weight"),
                    "drift_stack_weight": p.get("stack_weight"),
                    "drift_disagreement": p.get("disagreement"),
                    "drift_pick_rank": p.get("pick_rank"),
                    "drift_slate_n": p.get("slate_n"),
                    "drift_book_n_resolved": book.get("n_resolved"),
                    "drift_book_wins": wins,
                    "drift_book_total_pnl_cents": book.get("total_pnl_cents"),
                    "drift_book_status": book.get("status"),
                    "drift_book_verdict_n": 60,
                    "drift_candidate_lane": "FROZEN_CORE_SOURCE",
                    "source_build_sha": capture.get("build_sha"),
                    "source_config_hash": capture.get("config_hash"),
                    "source_features_version": (
                        capture.get("feature_schema_version")
                        or p.get("features_version")
                    ),
                }
                for key in (
                    "seconds_remaining", "quote_age_seconds",
                    "yes_bid_depth_contracts", "yes_ask_depth_contracts",
                    "no_bid_depth_contracts", "no_ask_depth_contracts",
                    "kalshi_depth_status", "kalshi_depth_missing_reason",
                    "kalshi_depth_retry_used", "kalshi_taker_yes_volume_15s",
                    "kalshi_taker_no_volume_15s",
                    "kalshi_taker_net_yes_volume_15s", "index_px",
                    "basis_cents", "index_age_s", "index_status",
                    "index_missing_reason", "index_id", "index_source_ts",
                ):
                    source[key] = capture.get(key)
                strategy_bots_runtime.record_drift_pick_row(source)
        except Exception:  # noqa: BLE001 - alerting must never break the loop
            logger.debug("drift pick alert failed (ignored)", exc_info=True)

    @staticmethod
    def _drift_num(value: Any) -> float | None:
        try:
            if value is None or isinstance(value, bool):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def _alert_drift_asymmetric_candidates(
        self,
        rec: Any,
        slate: Sequence[Mapping[str, Any]],
        wk: int,
        now: float,
    ) -> None:
        """Forward the incremental ask-conditioned distance envelope.

        The frozen recorder remains untouched.  Only 65--73c candidates in the
        incremental ``3e-5 < distance <= 1e-4`` slice are forwarded here; core
        rows already reach the same shadow policies through ``_alert_drift_picks``.
        All downstream variants are RESEARCH_ONLY and notification-disabled.
        """
        try:
            eligible: list[dict[str, Any]] = []
            for raw in slate or ():
                row = dict(raw)
                asset = str(row.get("asset") or "").upper()
                side = str(row.get("predicted_side") or "").upper()
                ask = self._drift_num(row.get("yes_ask_cents"))
                distance = self._drift_num(row.get("distance_from_strike"))
                flip = self._drift_num(row.get("flip_probability"))
                if (
                    asset not in {"DOGE", "HYPE", "SOL", "XRP"}
                    or side != "YES"
                    or ask is None
                    or not 65.0 <= ask <= 73.0
                    or distance is None
                    or not 3e-5 < distance <= 1e-4
                    or flip is None
                    or not 0.0 <= flip <= 30.0
                ):
                    continue
                cal = self._drift_num(row.get("calibrated_yes_probability"))
                row["_drift_disagreement"] = (
                    cal - ask / 100.0 if cal is not None else -9.0
                )
                eligible.append(row)
            if not eligible:
                return
            eligible.sort(
                key=lambda row: float(row.get("_drift_disagreement") or -9.0),
                reverse=True,
            )
            book = (rec.scoreboard() or {}).get("book_volume_60_73_all", {})
            wins = None
            if book.get("n_resolved") and book.get("win_rate") is not None:
                wins = round(float(book["win_rate"]) * int(book["n_resolved"]))
            from q15_upgrade.strategy_bots import runtime as strategy_bots_runtime

            for rank, row in enumerate(eligible, start=1):
                captured_at = self._drift_num(row.get("captured_at"))
                spread = self._drift_num(row.get("spread_cents"))
                hour = time.gmtime(captured_at or now).tm_hour
                spread_weight = rec.spread_weight(spread) if hasattr(rec, "spread_weight") else 1.0
                session_weight = rec.session_weight(hour) if hasattr(rec, "session_weight") else 1.0
                stack_weight = rec.stack_weight(spread, hour) if hasattr(rec, "stack_weight") else 1.0
                source = {
                    "created_at": captured_at if captured_at is not None else now,
                    "model_version": self.config.model_version,
                    "record_kind": "DRIFT_PICK_13M",
                    "delivery_status": "PAPER_DRIFT_ASYMMETRIC_SOURCE",
                    "asset": row.get("asset"),
                    "ticker": row.get("ticker"),
                    "interval": "13M",
                    "window_key": wk,
                    "close_time": row.get("close_time"),
                    "seconds_remaining": row.get("seconds_remaining"),
                    "predicted_side": "YES",
                    "distance_sigma": row.get("distance_from_strike"),
                    "flip_probability": row.get("flip_probability"),
                    "entry_ask_cents": row.get("yes_ask_cents"),
                    "spread_cents": row.get("spread_cents"),
                    "depth_contracts": row.get("depth_contracts"),
                    "drift_spread_weight": spread_weight,
                    "drift_session_weight": session_weight,
                    "drift_stack_weight": stack_weight,
                    "drift_disagreement": row.get("_drift_disagreement"),
                    "drift_pick_rank": rank,
                    "drift_slate_n": len(eligible),
                    "drift_book_n_resolved": book.get("n_resolved"),
                    "drift_book_wins": wins,
                    "drift_book_total_pnl_cents": book.get("total_pnl_cents"),
                    "drift_book_status": book.get("status"),
                    "drift_book_verdict_n": 60,
                    "drift_candidate_lane": "ASYMMETRIC_INCREMENTAL_SOURCE",
                    "source_build_sha": row.get("build_sha"),
                    "source_config_hash": row.get("config_hash"),
                    "source_features_version": row.get("feature_schema_version"),
                }
                for key in (
                    "quote_age_seconds", "yes_bid_depth_contracts",
                    "yes_ask_depth_contracts", "no_bid_depth_contracts",
                    "no_ask_depth_contracts", "kalshi_depth_status",
                    "kalshi_depth_missing_reason", "kalshi_depth_retry_used",
                    "kalshi_taker_yes_volume_15s",
                    "kalshi_taker_no_volume_15s",
                    "kalshi_taker_net_yes_volume_15s", "index_px",
                    "basis_cents", "index_age_s", "index_status",
                    "index_missing_reason", "index_id", "index_source_ts",
                ):
                    source[key] = row.get(key)
                strategy_bots_runtime.record_drift_pick_row(source)
        except Exception:  # noqa: BLE001 - shadow capture cannot break the loop
            logger.debug("drift asymmetric shadow feed failed (ignored)", exc_info=True)

    def _alert_drift_no_expansion(
        self, rec: Any, wk: int, now: float, *, replay_all: bool = False
    ) -> None:
        """Forward the NO candidate envelope for flow/spread confirmation."""
        try:
            if replay_all and hasattr(rec, "no_mirror_rows_for_window"):
                rows = rec.no_mirror_rows_for_window(self.config.model_version, wk)
            else:
                rows = rec.no_mirror_rows_recorded_at(
                    self.config.model_version, wk, now
                )
            if not rows:
                return
            from q15_upgrade.strategy_bots import runtime as strategy_bots_runtime
            payloads = []
            for row in rows:
                source_at = _num(row.get("created_at"))
                if source_at is None:
                    source_at = now
                payloads.append({
                    "created_at": source_at,
                    "model_version": self.config.model_version,
                    "record_kind": "DRIFT_NO_EXPANSION",
                    "rule_code": "DRIFT_NO_EXPANSION_FLOW_SPREAD_V1",
                    "reason_codes": row.get("reason_codes"),
                    "drift_no_tags": row.get("reason_codes"),
                    "delivery_status": "PAPER_DRIFT_NO_EXPANSION",
                    "asset": row.get("asset"),
                    "ticker": row.get("ticker"),
                    "interval": "13M",
                    "window_key": wk,
                    "close_time": row.get("close_time"),
                    "seconds_remaining": (
                        float(row["close_time"]) - source_at
                        if row.get("close_time") is not None and source_at is not None
                        else None
                    ),
                    "predicted_side": "NO",
                    "entry_ask_cents": row.get("ask_cents"),
                    "spread_cents": row.get("spread_cents"),
                    "depth_contracts": row.get("depth_contracts"),
                    "distance_sigma": row.get("distance_sigma"),
                    "flip_probability": row.get("flip_probability"),
                    "drift_btc_side_at_capture": row.get("btc_side"),
                })
            strategy_bots_runtime.record_drift_no_expansion_window(payloads)
        except Exception:  # noqa: BLE001 - research alert must never break capture
            logger.debug("drift NO expansion alert failed (ignored)", exc_info=True)

    # Drift-shadow v3 later checkpoints. Source-capture events are primary;
    # the bounded bands below exist only to repair a persisted current-window
    # slate after process restart.
    _DRIFT_CKPT_MARKS = (("12M", 720), ("11M", 660), ("10M", 600),
                         ("9M", 540), ("8M", 480), ("7M", 420))

    def _maybe_drift_checkpoints(
        self,
        canonicals: Mapping[str, Any],
        now: float,
        forced_targets: Mapping[
            tuple[str, int], tuple[float | None, float]
        ] | None = None,
    ) -> None:
        """Feed the drift shadow's v3 record-only tracks (re-qualification
        add-ons and late-qualifier entries) at the 12M..7M marks. Never trades
        or notifies; idempotent per (window, ticker) inside the recorder."""
        try:
            from q15_upgrade.drift_shadow import get_recorder
        except Exception:  # noqa: BLE001 - optional research recorder
            return
        rec = get_recorder()
        if rec is None or not hasattr(rec, "observe_checkpoint"):
            return

        targets = dict(forced_targets or {})
        forced_keys = set(targets)
        # Re-drive any real persisted checkpoint slate after its mark while the
        # contract is still active. This repairs a restart outside the old
        # 45-second band without fabricating a checkpoint source row.
        for canonical in (canonicals or {}).values():
            sr = _num(getattr(canonical, "seconds_remaining", None))
            if sr is None or sr <= 0.0:
                continue
            wk = window_key(getattr(canonical, "settlement_time", None), now)
            close_time = _num(getattr(canonical, "settlement_time", None))
            for interval, mark in self._DRIFT_CKPT_MARKS:
                if sr > mark:
                    continue
                key = (interval, wk)
                if key in self._drift_checkpoint_replayed and key not in forced_keys:
                    continue
                slate = self.ledger.captures_for_window(
                    self.config.model_version, interval, wk
                )
                if not slate:
                    continue
                targets.setdefault(
                    key,
                    (close_time, now),
                )

        for (interval, wk), (fallback_close, fallback_at) in targets.items():
            slate = self.ledger.captures_for_window(
                self.config.model_version, interval, wk
            )
            if not slate:
                continue
            close_time = self._slate_close_time(slate, fallback_close)
            rec.observe_checkpoint(
                model_version=self.config.model_version,
                window_key=wk,
                interval=interval,
                close_time=close_time,
                slate=slate,
                now=fallback_at,
            )
            self._alert_drift_checkpoint_rows(
                rec, wk, interval, fallback_at, replay_all=True
            )
            self._drift_checkpoint_replayed.add((interval, wk))

    def _alert_drift_checkpoint_rows(
        self,
        rec: Any,
        window_key_value: int,
        interval: str,
        now: float,
        *,
        replay_all: bool = False,
    ) -> None:
        """Send newly recorded checkpoint tracks through the paper V3 rail."""
        try:
            if replay_all and hasattr(rec, "checkpoint_rows_for_window"):
                rows = rec.checkpoint_rows_for_window(
                    self.config.model_version, window_key_value, interval
                )
            else:
                rows = rec.checkpoint_rows_recorded_at(
                    self.config.model_version, window_key_value, interval, now
                )
            if not rows:
                return
            scoreboard = rec.scoreboard() or {}
            from q15_upgrade.strategy_bots import runtime as strategy_bots_runtime

            for row in rows:
                source_at = _num(row.get("created_at"))
                if source_at is None:
                    source_at = now
                kind = str(row.get("record_kind") or "")
                if kind == "DRIFT_ADDON_REQUAL":
                    book = scoreboard.get("book_addon_requal", {})
                    delivery_status = "PAPER_ADDON"
                    rule_version = "drift-addon-requal-12m-7m-v1"
                    independent = False
                elif kind == "DRIFT_LATEQUAL":
                    book = scoreboard.get("book_latequal", {})
                    delivery_status = "RESEARCH"
                    rule_version = "drift-latequal-12m-11m-v1"
                    independent = True
                else:
                    continue
                wins = None
                if book.get("n_resolved") and book.get("win_rate") is not None:
                    wins = round(float(book["win_rate"]) * int(book["n_resolved"]))
                strategy_bots_runtime.record_drift_checkpoint_row({
                    "created_at": source_at,
                    "model_version": self.config.model_version,
                    "record_kind": kind,
                    "delivery_status": delivery_status,
                    "asset": row.get("asset"),
                    "ticker": row.get("ticker"),
                    "interval": interval,
                    "window_key": window_key_value,
                    "close_time": row.get("close_time"),
                    "seconds_remaining": (
                        float(row["close_time"]) - source_at
                        if row.get("close_time") is not None else None
                    ),
                    "predicted_side": row.get("side") or "YES",
                    "entry_ask_cents": row.get("ask_cents"),
                    "spread_cents": row.get("spread_cents"),
                    "depth_contracts": row.get("depth_contracts"),
                    "drift_base_ask_cents": row.get("base_ask_cents"),
                    "drift_ask13_cents": row.get("ask13_cents"),
                    "drift_disagreement": row.get("disagreement"),
                    "drift_rule_version": rule_version,
                    "drift_independent_pick": independent,
                    "drift_track_n_resolved": book.get("n_resolved"),
                    "drift_track_wins": wins,
                    "drift_track_total_pnl_cents": book.get("total_pnl_cents"),
                    "drift_track_status": book.get("status"),
                    "drift_track_verdict_n": 40,
                })
        except Exception:  # noqa: BLE001 - alerting must never break capture
            logger.debug("drift checkpoint alert failed (ignored)", exc_info=True)

    # -- best trade 13M: one pick per 15m window (owner-requested, always fires) --
    # Fires once per window shortly after the 13M captures land (sr 740-770).
    # Ranking is PROFIT-first: measured per-price-bucket EV at the 13M mark
    # (oracle-graded tape, Jun-23..Jul-05, n=969 cycles), then market extremity
    # |candidate ask - 50| (74.5% top-1 accuracy), then model conviction. The
    # 85-90c favorite band is the only ~breakeven-or-better cell at 13M; all
    # other buckets are measured negative and the card says so per pick.
    _BUCKET_EV_13M = (
        (85.0, 90.0, 0.35),    # favorite band — best measured cell at 13M
        (60.0, 70.0, -0.98),
        (70.0, 80.0, -1.05),
        (90.0, 101.0, -2.50),
        (80.0, 85.0, -3.32),
        (0.0, 60.0, -3.52),
    )

    @classmethod
    def _bucket_ev(cls, ask: float) -> float:
        for lo, hi, ev in cls._BUCKET_EV_13M:
            if lo <= ask < hi:
                return ev
        return -3.52

    # v3.1 grade (2026-07-06): the ranking is untouched; the card is labeled by
    # its measured cell so the owner trades the good cells and skips the stable
    # loser. Receipts (n=1078, chronological halves):
    #   SKIP  (BTC/ETH pick, or ask outside fav/60-80): -2.90c/tr, h1 -2.96 / h2 -2.84
    #   TRADE (alt in the 85-90c favorite band):        +1.21c/tr
    #   CAUTION (alt at 60-80c fallback):               +1.55c/tr (decays h2)
    # BTC/ETH are graded SKIP at any price: their books are efficiently priced
    # (win rate below breakeven in every bucket; negative in all four quarters).
    @staticmethod
    def _skip_assets() -> set[str]:
        raw = os.environ.get("Q15_V3_TOP_PICK_SKIP_ASSETS", "BTC,ETH")
        return {part.strip().upper() for part in raw.split(",") if part.strip()}

    @classmethod
    def _pick_grade(cls, asset: str, ask: float) -> tuple[str, str]:
        if str(asset).upper() in cls._skip_assets():
            return ("SKIP", "MAJOR_EFFICIENT_BOOK")
        if 85.0 <= ask < 90.0:
            return ("TRADE", "ALT_FAVORITE_BAND")
        if 60.0 <= ask < 80.0:
            return ("CAUTION", "ALT_FALLBACK_BAND")
        return ("SKIP", "OUT_OF_MEASURED_BANDS")

    def _maybe_top_pick_13m(self, canonicals: Mapping[str, Any], now: float) -> None:
        if not _env_bool("Q15_V3_TOP_PICK_13M", True):
            return
        min_assets = max(2, int(float(os.environ.get("Q15_V3_TOP_PICK_13M_MIN_ASSETS", "3") or 3)))
        # prune old one-shot keys so the dict cannot leak
        cutoff = now - 1800.0
        for wk in [k for k, ts in self._top_pick_done.items() if ts < cutoff]:
            self._top_pick_done.pop(wk, None)
        for canonical in (canonicals or {}).values():
            sr = _num(getattr(canonical, "seconds_remaining", None))
            # HARD REQUIREMENT (owner, 2026-07-06): exactly one pick per 15m window.
            # Two-phase fire: PRIMARY in [740, 770] once min_assets have scored;
            # FALLBACK in [600, 740) with whatever slate exists (>=1) so a thin or
            # late slate can never silently skip a window. Below 600 with zero
            # scorable captures, a NO-PICK data-gap notice keeps the cadence
            # visible instead of silent.
            if sr is None or not (600.0 <= sr <= 770.0):
                continue
            phase = "PRIMARY" if sr >= 740.0 else "FALLBACK"
            wk = window_key(getattr(canonical, "settlement_time", None), now)
            if wk is None or wk in self._top_pick_done:
                continue
            slate = self.ledger.captures_for_window(self.config.model_version, "13M", wk)
            slate = [s for s in slate if s.get("yes_ask_cents") is not None
                     and str(s.get("predicted_side") or "").upper() in {"YES", "NO"}]
            if phase == "PRIMARY" and len(slate) < min_assets:
                continue  # stragglers may still land; retry next cycle inside the band
            if phase == "FALLBACK" and not slate:
                if sr < 620.0:
                    # deadline: nothing scorable this window — emit the gap notice once
                    self._top_pick_done[wk] = now
                    try:
                        from q15_upgrade.strategy_bots import runtime as strategy_bots_runtime

                        strategy_bots_runtime.send_top_pick_gap_notice(
                            window_key=wk,
                            close_time=_num(getattr(canonical, "settlement_time", None)),
                        )
                    except Exception:  # noqa: BLE001 - notice must never break capture
                        logger.warning("top-pick gap notice failed (ignored)", exc_info=True)
                continue
            ranked = sorted(
                slate,
                key=lambda s: (
                    -self._bucket_ev(float(s["yes_ask_cents"])),
                    -abs(float(s["yes_ask_cents"]) - 50.0),
                    -abs(float(s.get("calibrated_yes_probability") or 0.5) - 0.5),
                ),
            )
            top = ranked[0]
            second = ranked[1] if len(ranked) > 1 else None
            self._top_pick_done[wk] = now
            ask = top.get("entry_ask_cents")
            if ask is None:
                ask = top.get("yes_ask_cents")
            source_row = {
                "created_at": now,
                "model_version": self.config.model_version,
                "asset": top.get("asset"),
                "ticker": top.get("ticker"),
                "interval": "13M",
                "window_key": wk,
                "close_time": top.get("close_time"),
                "record_kind": "TOP_PICK_13M",
                "delivery_status": "RECORDED",
                "predicted_side": str(top.get("predicted_side") or "").upper(),
                "calibrated_yes_probability": top.get("calibrated_yes_probability"),
                "entry_ask_cents": ask,
                "spread_cents": top.get("spread_cents"),
                "top_pick_slate_n": len(slate),
                "top_pick_extremity": abs(float(top["yes_ask_cents"]) - 50.0),
                "top_pick_runner_up_asset": second.get("asset") if second else None,
                "top_pick_runner_up_extremity": (
                    abs(float(second["yes_ask_cents"]) - 50.0) if second else None
                ),
                "top_pick_bucket_ev_cents": self._bucket_ev(float(top["yes_ask_cents"])),
                "top_pick_fav_band": 85.0 <= float(top["yes_ask_cents"]) < 90.0,
                "top_pick_phase": phase,
            }
            grade, grade_reason = self._pick_grade(str(top.get("asset")), float(top["yes_ask_cents"]))
            source_row["top_pick_grade"] = grade
            source_row["top_pick_grade_reason"] = grade_reason
            try:
                from q15_upgrade.strategy_bots import runtime as strategy_bots_runtime

                strategy_bots_runtime.record_top_pick_row(source_row)
            except Exception:  # noqa: BLE001 - optional display book must never break capture
                logger.warning("interval-research top-pick feed failed (ignored)", exc_info=True)

    # Per-interval V3 feed flags: 13M feeds the sniper (default OFF, owner-enabled
    # in env), 10M feeds the fav_10m favorite-band book (default ON by owner
    # directive 2026-07-05: "make everything on by default").
    _V3_FEED_FLAGS = {"13M": ("Q15_V3_13M_SNIPER_FEED", False),
                      "10M": ("Q15_V3_FAV10M_FEED", True)}

    def _feed_v3_marks(self, row: Mapping[str, Any],
                       analysis: Mapping[str, Any]) -> None:
        """Forward new mark captures into the V3 source-row runtime when enabled."""
        interval = str(row.get("interval") or "").upper()
        flag = self._V3_FEED_FLAGS.get(interval)
        if flag is None or not _env_bool(flag[0], flag[1]):
            return
        quote = _mapping(analysis.get("quote"))
        source_row = {
            "created_at": row.get("captured_at"),
            "model_version": row.get("model_version"),
            "asset": row.get("asset"),
            "ticker": row.get("ticker"),
            "interval": interval,
            "window_key": row.get("window_key"),
            "close_time": row.get("close_time"),
            "record_kind": f"INTERVAL_RESEARCH_{interval}",
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
            logger.warning("interval-research %s v3 feed failed (ignored)", interval, exc_info=True)

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
        rec = None
        try:
            from q15_upgrade.drift_shadow import get_recorder
            rec = get_recorder()
            if rec is not None:
                rec.resolve(result_events, now)
        except Exception:  # noqa: BLE001 - drift recorder must never break resolve
            logger.debug("drift-shadow resolve failed (ignored)", exc_info=True)
        try:
            from q15_upgrade.strategy_bots import runtime as strategy_bots_runtime
            current_events = []
            for event in result_events:
                if not isinstance(event, Mapping):
                    continue
                ticker = event.get("ticker") or event.get("contract")
                result = event.get("result") or event.get("official_result")
                if ticker and result:
                    current_events.append({
                        "model_version": self.config.model_version,
                        "ticker": str(ticker),
                        "official_result": str(result),
                        "resolved_at": now,
                    })
            strategy_bots_runtime.reconcile_drift_settlements(current_events)
            if (
                not self._drift_strategy_reconciled
                and rec is not None
                and strategy_bots_runtime.enabled()
            ):
                strategy_bots_runtime.reconcile_drift_settlements(rec.resolved_events())
                self._drift_strategy_reconciled = True
        except Exception:  # noqa: BLE001 - side-ledger repair must never block resolve
            logger.debug("drift strategy settlement reconcile failed (ignored)", exc_info=True)
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
