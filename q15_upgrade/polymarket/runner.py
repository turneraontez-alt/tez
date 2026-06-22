"""PolymarketRunner — live, read-only orchestration for the up/down shadow.

Wired into the v95 checkpoint loop (gated by Q15_POLYMARKET_ENABLED). For each
configured asset at each 15M/10M/7M checkpoint it: discovers the matching
Polymarket up/down market, reads its order book, computes OUR model's P(up) from
the SAME frozen snapshot the champion used, and records the paired row. After the
window closes a throttled reconcile pass grades both our model and the market
against Polymarket's own resolution. It never trades and never affects production.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

from .client import PolymarketClient, PolymarketMarket, updown_slug
from .config import PolymarketConfig
from .ledger import PolymarketLedger, REPORT_CHECKPOINTS, window_id
from .probability import updown_probability
from ..timez import TZ_NAME, fmt_eastern, fmt_eastern_hm

logger = logging.getLogger(__name__)

POLYMARKET_REPORT_MARKER = "POLYMARKET SHADOW"


class PolymarketRunner:
    def __init__(self, config: PolymarketConfig | None = None, client: Any = None):
        self.config = config or PolymarketConfig.from_env()
        self.ledger = PolymarketLedger(self.config.db_path)
        self.reset_at = self.ledger.reset_marker(self.config.model_version, self.config.reset_at)
        self.client = client or PolymarketClient(
            clob_base=self.config.clob_base, gamma_base=self.config.gamma_base,
            timeout=self.config.http_timeout)
        self._lock = threading.Lock()
        # In-memory guards so the 1s loop does at most one discovery per
        # (asset, window) and one record per (asset, window, checkpoint).
        self._market_cache: dict[tuple[str, int], PolymarketMarket | None] = {}
        self._locked: set[tuple[str, int, str]] = set()
        self._last_reconcile_at = 0.0
        self._last_report_window: int | None = None
        self._pending_report: str | None = None
        # All network + DB work runs on ONE background worker so the live ~1s loop
        # never blocks on a Polymarket HTTP call. observe()/reconcile() only enqueue.
        self._jobs: "queue.Queue[tuple[str, dict]]" = queue.Queue(maxsize=512)
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()

    # ---- background worker ----
    def _ensure_worker(self) -> None:
        if self._worker is not None:
            return
        with self._worker_lock:
            if self._worker is None:
                t = threading.Thread(target=self._worker_loop, name="polymarket-shadow",
                                     daemon=True)
                self._worker = t
                t.start()

    def _worker_loop(self) -> None:
        while True:
            kind, kw = self._jobs.get()
            try:
                if kind == "observe":
                    self._observe_sync(**kw)
                elif kind == "reconcile":
                    self._reconcile_sync(**kw)
            except Exception:
                logger.exception("polymarket worker job failed (ignored)")
            finally:
                self._jobs.task_done()

    # ---- live hooks (never raise / never block the loop) ----
    def observe(self, *, asset: str, checkpoint: str, spot: float | None,
                sigma_per_sqrt_second: float | None, drift_per_second: float | None,
                seconds_remaining: float | None, close_time: float | None,
                snapshot_id: str | None = None, now: float | None = None) -> None:
        """Enqueue an observation for the worker. Cheap, non-blocking; a full queue
        or an unconfigured asset is a silent no-op."""
        try:
            wid = window_id(close_time)
            if wid is None or asset.upper() not in self.config.assets:
                return
            if (asset.upper(), wid, str(checkpoint)) in self._locked:
                return  # best-effort fast-path (worker re-checks authoritatively)
            self._ensure_worker()
            self._jobs.put_nowait(("observe", dict(
                asset=asset, checkpoint=checkpoint, spot=spot,
                sigma_per_sqrt_second=sigma_per_sqrt_second, drift_per_second=drift_per_second,
                seconds_remaining=seconds_remaining, close_time=close_time,
                snapshot_id=snapshot_id, now=now)))
        except queue.Full:
            logger.debug("polymarket observe queue full — dropping job")
        except Exception:
            logger.exception("polymarket observe enqueue failed (ignored)")

    def _observe_sync(self, *, asset: str, checkpoint: str, spot: float | None,
                      sigma_per_sqrt_second: float | None, drift_per_second: float | None,
                      seconds_remaining: float | None, close_time: float | None,
                      snapshot_id: str | None = None, now: float | None = None) -> None:
        try:
            if asset.upper() not in self.config.assets:
                return
            wid = window_id(close_time)
            if wid is None:
                return
            key = (asset.upper(), wid, str(checkpoint))
            if key in self._locked:
                return
            market = self._discover(asset.upper(), wid, close_time)
            if market is None:
                self._locked.add(key)   # no Polymarket contract this window — stop retrying
                return
            book = self.client.book_metrics(market)
            prob_up = updown_probability(
                spot=spot, open_price=market.open_price,
                seconds_remaining=seconds_remaining,
                sigma_per_sqrt_second=sigma_per_sqrt_second,
                drift_per_second=drift_per_second,
                max_drift_fraction_of_sigma=self.config.max_drift_fraction_of_sigma,
            )
            self.ledger.record(
                model_version=self.config.model_version, asset=asset.upper(),
                checkpoint=str(checkpoint), close_time=close_time,
                condition_id=market.condition_id, yes_token_id=market.yes_token_id,
                spot=spot, open_price=market.open_price, seconds_remaining=seconds_remaining,
                model_prob_up=prob_up, market_prob_up=book.market_prob_up,
                poly_yes_bid=book.yes_bid, poly_yes_ask=book.yes_ask,
                poly_no_bid=book.no_bid, poly_no_ask=book.no_ask,
                poly_spread=book.spread, poly_depth=book.depth,
                poly_last_trade=book.last_trade, snapshot_id=snapshot_id, created_at=now,
            )
            self._locked.add(key)
        except Exception:
            logger.exception("polymarket shadow observe failed (ignored)")

    def _discover(self, asset: str, wid: int, close_time: float | None) -> PolymarketMarket | None:
        cache_key = (asset, wid)
        if cache_key in self._market_cache:
            return self._market_cache[cache_key]
        market = None
        try:
            if close_time is not None:
                market = self.client.find_updown_market(asset, float(close_time))
        except Exception:
            logger.exception("polymarket discovery failed (ignored)")
            market = None
        self._market_cache[cache_key] = market
        return market

    # ---- settlement reconcile (throttled; enqueued to the worker) ----
    def reconcile(self, now: float | None = None) -> None:
        """Throttled, non-blocking: enqueue a settlement-reconcile pass at most
        once per ``reconcile_every_seconds``."""
        now = now if now is not None else time.time()
        if now - self._last_reconcile_at < self.config.reconcile_every_seconds:
            return
        self._last_reconcile_at = now
        try:
            self._ensure_worker()
            self._jobs.put_nowait(("reconcile", {"now": now}))
        except queue.Full:
            logger.debug("polymarket reconcile queue full — dropping job")
        except Exception:
            logger.exception("polymarket reconcile enqueue failed (ignored)")

    def _reconcile_sync(self, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        resolved = 0
        try:
            for item in self.ledger.unresolved_closed(self.config.model_version, now=now):
                cond = item["condition_id"]
                asset = str(item["asset"] or "")
                close_time = item["close_time"]
                if not cond or close_time is None:
                    continue
                market = PolymarketMarket(
                    asset=asset, slug=updown_slug(asset, float(close_time) - 900.0),
                    condition_id=str(cond), yes_token_id=None, no_token_id=None,
                    open_price=None, window_open=float(close_time) - 900.0,
                    window_close=float(close_time))
                result = self.client.resolved_result(market)
                if result in ("UP", "DOWN"):
                    n = self.ledger.resolve(str(cond), result, settled_at=now,
                                            model_version=self.config.model_version)
                    if n:
                        resolved += n
                        self._mark_window_report(close_time)
        except Exception:
            logger.exception("polymarket reconcile failed (ignored)")
        return resolved

    # ---- reporting ----
    def _mark_window_report(self, when: float | None) -> None:
        if not self.config.report_enabled:
            return
        wid = window_id(when)
        with self._lock:
            if wid is not None and wid != self._last_report_window:
                self._last_report_window = wid
                self._pending_report = self.report_message()

    def drain_report(self) -> str | None:
        with self._lock:
            r, self._pending_report = self._pending_report, None
            return r

    def report_message(self) -> str:
        return build_polymarket_panel(
            reset_at=self.reset_at,
            latest=self.ledger.latest_window(self.config.model_version),
            accuracy=self.ledger.accuracy_by_checkpoint(self.config.model_version),
            archived=self.ledger.archived_versions(self.config.model_version),
        )


# --------------------------------------------------------------------------- #
#  Pure formatter (no I/O) — the compact POLYMARKET SHADOW card                #
# --------------------------------------------------------------------------- #
def _cents(v: Any) -> str:
    try:
        return "—" if v is None else f"{float(v):.0f}¢"
    except (TypeError, ValueError):
        return "—"


def _our_side_prob(side: str | None, prob_up: float | None) -> float | None:
    """P assigned to OUR predicted side (UP -> p, DOWN -> 1-p)."""
    if side is None or prob_up is None:
        return None
    return float(prob_up) if side == "UP" else 1.0 - float(prob_up)


def _wl(d: dict) -> str:
    c, w = d.get("correct", 0), d.get("wrong", 0)
    acc = d.get("accuracy")
    return f"{c}W–{w}L {('N/A' if acc is None else str(round(acc * 100)) + '%')}"


def build_polymarket_panel(*, reset_at: float, latest: dict, accuracy: dict,
                           archived: list[str] | None = None) -> str:
    """Render the compact 'POLYMARKET SHADOW' card from ledger data. Pure."""
    body: list[str] = [
        "Up/Down shadow · read-only · never trades",
        "Our model vs Polymarket market · same contract",
        f"Comparison reset: {fmt_eastern(reset_at)}",
        f"Time zone: {TZ_NAME}",
    ]

    cps = latest.get("checkpoints") or {}
    if not cps:
        body += ["", "Polymarket: NO MATCHING MARKET"]
        return f"<b>{POLYMARKET_REPORT_MARKER}</b>\n<pre>{chr(10).join(body)}</pre>"

    when = fmt_eastern_hm(latest["close_time"]) if latest.get("close_time") else None
    body += ["", f"LAST WINDOW · {when}" if when else "LAST WINDOW"]
    edges: list[int] = []
    agree: list[bool] = []
    for cp in REPORT_CHECKPOINTS:
        slot = cps.get(cp)
        if not slot:
            body.append(f"{cp}: —")
            continue
        side = slot.get("model_side") or "—"
        our_p = _our_side_prob(slot.get("model_side"), slot.get("model_prob_up"))
        pct = "—" if our_p is None else f"{round(our_p * 100)}%"
        ask = slot.get("our_side_ask_cents")
        body.append(f"{cp}: {side} {pct} | Market {_cents(ask)}")
        if our_p is not None and ask is not None:
            edges.append(round(our_p * 100) - round(float(ask)))  # +ve => market cheaper than fair
        if slot.get("model_side") and slot.get("market_side"):
            agree.append(slot["model_side"] == slot["market_side"])

    best_edge = max(edges) if edges else None
    if best_edge is not None and best_edge > 0:
        body += ["", f"Pricing: Polymarket better by {best_edge}¢"]
    elif best_edge is not None:
        body += ["", "Pricing: no entry edge vs our fair value"]
    if agree:
        body.append(f"Cross-market agreement: {'YES' if all(agree) else 'MIXED'}")
    body.append("Status: LEARNING ONLY")

    by_cp = accuracy.get("by_checkpoint") or {}
    if accuracy.get("n"):
        body += ["", "ALL-TIME (resolved)"]
        for cp in REPORT_CHECKPOINTS:
            m = by_cp.get(cp) or {}
            body.append(f"{cp}: model {_wl(m.get('model', {}))} | market {_wl(m.get('market', {}))}")
    body.append(f"Sample: {accuracy.get('n', 0)}")

    if archived:
        body += ["", f"Archive (excluded): {', '.join(archived)}"]
    return f"<b>{POLYMARKET_REPORT_MARKER}</b>\n<pre>{chr(10).join(body)}</pre>"


# ---- module singleton (built only when enabled) ----
_runner: PolymarketRunner | None = None
_runner_lock = threading.Lock()
_enabled_cache: bool | None = None


def is_enabled() -> bool:
    global _enabled_cache
    if _enabled_cache is None:
        _enabled_cache = PolymarketConfig.from_env().enabled
    return _enabled_cache


def get_runner() -> PolymarketRunner | None:
    if not is_enabled():
        return None
    global _runner
    if _runner is None:
        with _runner_lock:
            if _runner is None:
                try:
                    _runner = PolymarketRunner()
                except Exception:
                    logger.exception("polymarket shadow runner init failed (disabled)")
                    return None
    return _runner
