"""Non-blocking-from-network, paper-only runner for Q15 MarketLead."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .config import MarketLeadConfig
from .features import MarketLeadFeatureEngine
from .ledger import MarketLeadLedger

logger = logging.getLogger(__name__)


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


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
    ):
        self.config = config or MarketLeadConfig.from_env()
        self.ledger = MarketLeadLedger(self.config.db_path)
        self.engine = MarketLeadFeatureEngine(self.config)
        self._lock = threading.Lock()
        self._last: dict[str, _Candidate] = {}
        self._pending: dict[str, _PendingExecution] = {}
        self._microstructure_provider = microstructure_provider
        self._index_provider = index_provider
        self._restore_pending()

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
            ticker, now=now, paper_limit_cents=self.config.paper_limit_cents
        )

    @staticmethod
    def _default_index(asset: str, spot: float | None, now: float) -> Mapping[str, Any]:
        from settlement_index import settlement_index_context

        return settlement_index_context(asset, spot_px=spot, now=now)

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
                self._expire_stale_pending(now)
                for asset, canonical in (canonicals or {}).items():
                    ticker = str(getattr(canonical, "ticker", "") or "")
                    seconds = _num(getattr(canonical, "seconds_remaining", None))
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
                    spot = _num(getattr(canonical, "spot", None))
                    near_mark = abs(seconds - self.config.mark_seconds) <= max(
                        self.config.crossing_max_seconds,
                        self.config.mark_band_seconds,
                    )
                    if near_mark:
                        official, kalshi = self._providers(str(asset), ticker, spot, now)
                        self._track_execution(ticker, kalshi, now)
                    else:
                        official = {"index_px": None, "index_status": "not_sampled"}
                        kalshi = {"available": False, "reason": "not_sampled"}
                    row = self.engine.build(
                        asset=str(asset),
                        analysis=analysis,
                        canonical=canonical,
                        now=now,
                        official_index=official,
                        kalshi=kalshi,
                    )
                    candidate = _Candidate(ticker, seconds, now, row)
                    if not near_mark:
                        continue
                    selected = self._select(str(asset), candidate)
                    if selected is not None:
                        if self.ledger.record(selected.row):
                            touched = bool(selected.row.get("paper_limit_touched"))
                            self._pending[ticker] = _PendingExecution(
                                ticker=ticker,
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

    def status(self) -> dict[str, Any]:
        return {
            "system": "Q15 MarketLead",
            "system_version": self.config.system_version,
            "paper_only": True,
            "notifies": False,
            "trades": False,
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
