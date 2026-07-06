"""Interval-timing research — live-loop observer (read-only, default-OFF).

Mirrors the Ultoim observer: invoked every ~1s from checkpoint_v95.run_cycle, it
captures the champion's frozen analysis at each of the eight marks (band around
each mark), deduped per (ticker, interval). It NEVER trades, sends, or alters the
champion. ``resolve_settled`` attaches official outcomes from the same settlement
events the champion already produces.
"""
from __future__ import annotations

import logging
import os
import threading
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


class IntervalResearchRunner:
    def __init__(self, config: IntervalResearchConfig | None = None):
        self.config = config or IntervalResearchConfig.from_env()
        self.ledger = IntervalResearchLedger(self.config.db_path)
        self._lock = threading.Lock()
        # top-pick-13M one-shot state: window_key -> decided-at timestamp
        self._top_pick_done: dict[int, float] = {}

    @property
    def model_version(self) -> str:
        return self.config.model_version

    def observe(self, *, analyses: Mapping[str, Mapping[str, Any]],
                canonicals: Mapping[str, Any], now: float) -> None:
        """Capture every (asset, interval) whose seconds_remaining is in-band.

        Read-only and exception-isolated: a capture failure must never disturb the
        live cycle. Dedup is by the ledger's unique (model_version, ticker, interval)
        key, so repeated in-band cycles are no-ops after the first."""
        if not self.config.enabled or not self.ledger.available:
            return
        band = self.config.mark_band_seconds
        mv = self.config.model_version
        try:
            with self._lock:
                for asset, canonical in (canonicals or {}).items():
                    sr = _num(getattr(canonical, "seconds_remaining", None))
                    if sr is None:
                        continue
                    ticker = getattr(canonical, "ticker", None)
                    wk = window_key(getattr(canonical, "settlement_time", None), now)
                    analysis = analyses.get(asset) if isinstance(analyses, Mapping) else None
                    analysis = analysis if isinstance(analysis, Mapping) else {}
                    for interval, mark in INTERVAL_MARKS.items():
                        if not (mark - band) <= sr <= mark:
                            continue
                        if ticker and self.ledger.has_row(mv, str(ticker), interval):
                            continue
                        reason = missing_reason(analysis, canonical, self.config.min_data_quality)
                        if reason is not None:
                            # CONTRACT_NOT_MAPPED has no ticker to key — unrecordable.
                            if ticker:
                                self.ledger.record_missing(
                                    model_version=mv, ticker=str(ticker), asset=str(asset),
                                    interval=interval, reason=reason, window_key=wk, captured_at=now)
                            continue
                        capture_analysis = dict(analysis)
                        try:
                            from settlement_index import settlement_index_context

                            capture_analysis["settlement_index"] = settlement_index_context(
                                str(asset), spot_px=getattr(canonical, "spot", None), now=now)
                        except Exception:  # noqa: BLE001 - record-only context must not break capture
                            capture_analysis["settlement_index"] = {
                                "index_px": None, "basis_cents": None, "index_age_s": None}
                        row = build_capture_row(
                            model_version=mv, interval=interval, mark_seconds=mark,
                            asset=str(asset), analysis=capture_analysis, canonical=canonical,
                            window_key=wk, now=now)
                        if self.ledger.record_capture(row):
                            self._feed_v3_marks(row, capture_analysis)
                self._maybe_top_pick_13m(canonicals, now)
        except Exception:  # never break the live loop
            logger.debug("interval-research observe failed (ignored)", exc_info=True)

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
