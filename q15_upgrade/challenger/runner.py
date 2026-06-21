"""ShadowRunner — live, read-only shadow execution beside the production champion.

Wired into the v95 ledger's record/resolve hooks (gated by Q15_CHALLENGER_ENABLED).
For each newly recorded production prediction it records a paired challenger
prediction; when the contract settles it grades the shadow, RE-TRAINS from its own
resolved history ("learns as it goes"), and at each 15-minute window boundary
prepares a compact accuracy comparison (challenger vs the current system) for
Telegram. It never executes a trade and never affects production output.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Mapping

from .config import ChallengerConfig
from .harness import train_predictor
from .ledger import ShadowLedger
from .lineage import lineage_record

logger = logging.getLogger(__name__)

CHALLENGER_REPORT_MARKER = "CHALLENGER SHADOW"


def _build_snapshot(features: Mapping[str, Any], quote: Mapping[str, Any]) -> dict:
    """Reconstruct a decision-time snapshot from the v95 feature_values + quote.

    ``features`` is the champion's 9 signal values; ``quote`` carries the Kalshi
    bid/ask. Only decision-time fields are used (no outcome data).
    """
    snap: dict[str, Any] = {}
    if isinstance(features, Mapping):
        signals = {}
        for k, v in features.items():
            if isinstance(v, Mapping):
                v = v.get("value")
            signals[k] = v
        snap["feature_values"] = signals
    q = dict(quote or {})
    for src, dst in (("yes_bid_cents", "yes_bid"), ("yes_ask_cents", "yes_ask"),
                     ("no_bid_cents", "no_bid"), ("no_ask_cents", "no_ask"),
                     ("bid_cents", "yes_bid"), ("ask_cents", "yes_ask")):
        if q.get(src) is not None and dst not in snap:
            snap[dst] = q[src]
    for k in ("spot", "target", "seconds_remaining", "market_implied_prob",
              "data_quality", "volatility_per_min", "book_imbalance", "depth_contracts"):
        if q.get(k) is not None:
            snap[k] = q[k]
    return snap


class ShadowRunner:
    def __init__(self, config: ChallengerConfig | None = None):
        self.config = config or ChallengerConfig.from_env()
        self.ledger = ShadowLedger(self.config.db_path)
        self.predictor, self.info = self._train()
        self._resolved_since_refit = 0
        self._last_report_window: int | None = None
        self._pending_report: str | None = None
        self._lock = threading.Lock()

    # ---- training ----
    def _train(self):
        ts, feats, y = self.ledger.training_samples(model_version=self.config.model_version)
        return train_predictor(ts, feats, y, self.config)

    def _maybe_refit(self) -> None:
        if self.config.refit_every <= 0:
            return
        if self._resolved_since_refit >= self.config.refit_every:
            self._resolved_since_refit = 0
            predictor, info = self._train()
            if info.get("fitted"):
                self.predictor, self.info = predictor, info
                logger.info("challenger shadow refit: %s", info)

    # ---- live hooks (never raise into production) ----
    def observe(self, *, ticker: str, asset: str, checkpoint: str, created_at: float,
                close_time: float | None, control_prob_yes: float | None,
                features: Mapping[str, Any], quote: Mapping[str, Any]) -> None:
        try:
            snap = _build_snapshot(features, quote)
            pred = self.predictor.predict(snap)
            # Cold start (untrained, no market quote): mirror the champion so the
            # comparison starts at parity and then diverges as the shadow learns.
            if (not getattr(self.predictor.model, "fitted", False)
                    and pred.market_yes_prob is None and control_prob_yes is not None):
                pred.prob_yes = round(float(control_prob_yes), 6)
                pred.prob_no = round(1.0 - float(control_prob_yes), 6)
            calib = getattr(self.predictor.calibrator, "name", "identity")
            self.ledger.record(
                pred, asset=asset, contract=ticker, checkpoint=checkpoint,
                control_prob_yes=control_prob_yes, created_at=created_at,
                model_version=self.config.model_version,
                lineage=lineage_record(self.config, calibrator_version=calib),
            )
        except Exception:
            logger.exception("challenger shadow observe failed (ignored)")

    def resolve(self, ticker: str, checkpoint: str, official_result: str,
                resolved_at: float | None = None) -> None:
        try:
            ok = self.ledger.resolve(ticker, checkpoint, official_result,
                                     settled_at=resolved_at, model_version=self.config.model_version)
            if not ok:
                return
            self._resolved_since_refit += 1
            self._maybe_refit()
            self._mark_window_report(resolved_at)
        except Exception:
            logger.exception("challenger shadow resolve failed (ignored)")

    # ---- per-15-min reporting ----
    def _mark_window_report(self, when: float | None) -> None:
        if not self.config.report_enabled:
            return
        window = int((when or time.time()) // 900)
        with self._lock:
            if window != self._last_report_window:
                self._last_report_window = window
                self._pending_report = self.report_message()

    def drain_report(self) -> str | None:
        with self._lock:
            r, self._pending_report = self._pending_report, None
            return r

    def comparison(self) -> dict:
        return self.ledger.comparison(model_version=self.config.model_version)

    def report_message(self) -> str:
        cmp = self.comparison()
        o = cmp["overall"]
        if not o["n"]:
            return f"{CHALLENGER_REPORT_MARKER} — no settled shadow predictions yet"
        def line(label, d):
            ch = "n/a" if d["challenger_accuracy"] is None else f"{d['challenger_accuracy']*100:.1f}%"
            cu = "n/a" if d["current_accuracy"] is None else f"{d['current_accuracy']*100:.1f}%"
            return f"{label:<7} challenger {ch:>6}   current {cu:>6}   (n={d['n']})"
        lines = [f"<b>{CHALLENGER_REPORT_MARKER}</b> — accuracy (read-only, not trading)",
                 "<pre>",
                 line("overall", o)]
        for cp, d in cmp["by_checkpoint"].items():
            lines.append(line(cp, d))
        lines.append("</pre>")
        trained = "yes" if self.info.get("fitted") else f"no ({self.info.get('reason','cold start')})"
        lines.append(f"learning: {trained} · model={self.config.model_version}")
        return "\n".join(lines)


# ---- module singleton (built only when enabled) ----
_runner: ShadowRunner | None = None
_runner_lock = threading.Lock()
_enabled_cache: bool | None = None


def is_enabled() -> bool:
    global _enabled_cache
    if _enabled_cache is None:
        _enabled_cache = ChallengerConfig.from_env().enabled
    return _enabled_cache


def get_runner() -> ShadowRunner | None:
    if not is_enabled():
        return None
    global _runner
    if _runner is None:
        with _runner_lock:
            if _runner is None:
                try:
                    _runner = ShadowRunner()
                except Exception:
                    logger.exception("challenger shadow runner init failed (disabled)")
                    return None
    return _runner
