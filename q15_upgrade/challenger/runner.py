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
from .ledger import REPORT_CHECKPOINTS, ShadowLedger
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
        # Stamp (or read) the reset instant for this model_version. Pre-reset rows
        # under other versions stay archived and are never scored into this record.
        self.reset_at = self.ledger.reset_marker(self.config.model_version, self.config.reset_at)
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
                close_time=close_time, model_version=self.config.model_version,
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

    def ranked(self, top_k: int = 3) -> dict:
        return self.ledger.ranked_comparison(model_version=self.config.model_version, top_k=top_k)

    # Only these three symbols are used in the graded cells (no bare X/+/-).
    _OK, _BAD, _NONE = "✓", "✗", "—"

    @classmethod
    def _mark(cls, correct) -> str:
        """✓ correct · ✗ wrong · — no official prediction."""
        if correct is None:
            return cls._NONE
        return cls._OK if correct else cls._BAD

    @classmethod
    def _pick(cls, entry) -> str:
        """A ranked pick as 'ASSET SIDE mark' (e.g. 'SOL NO ✓'); '—' if absent."""
        if not entry:
            return cls._NONE
        asset, side, correct = entry
        return f"{asset} {side} {cls._mark(correct)}"

    @classmethod
    def _wl(cls, d) -> str:
        """'3W–1L | 75%' (or '| N/A' before any settled case)."""
        c, w = d["correct"], d["wrong"]
        acc = d["accuracy"]
        acc_s = "N/A" if acc is None else f"{round(acc * 100)}%"
        return f"{c}W–{w}L | {acc_s}"

    def report_message(self, top_k: int = 3) -> str:
        """The Shadow-vs-Your-System card: one bold title + a single monospace
        block. Three ranked picks per interval, an end-result call graded across
        all three intervals, and all-time per-rank/per-interval records — all in
        ✓ / ✗ / — only. Scored strictly on predictions recorded under this
        model_version (post-reset)."""
        mv = self.config.model_version
        cps = list(REPORT_CHECKPOINTS)
        rk = self.ledger.ranked_comparison(model_version=mv, top_k=top_k)

        reset_s = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(self.reset_at))
        body: list[str] = [
            "Shadow test · read-only · never trades",
            "Picks ranked by confidence; #1 = most confident",
            "Correct = the predicted side matched the final result",
            f"Comparison reset: {reset_s}",
        ]

        if not rk["n_cases"]:
            body += ["", "No settled cases since reset yet.", "0W–0L | N/A"]
            return (f"<b>{CHALLENGER_REPORT_MARKER} vs YOUR SYSTEM</b>\n"
                    f"<pre>{chr(10).join(body)}</pre>")

        # ---- LAST WINDOW: three ranked picks per interval, side by side ----
        win = self.ledger.latest_window_cases(model_version=mv, top_k=top_k)
        if win["close"]:
            when = time.strftime("%H:%M", time.gmtime(win["close"]))
            body += ["", f"LAST WINDOW · {when} UTC"]
            for cp in cps:
                picks = win["checkpoints"].get(cp)
                if not picks:
                    continue
                cpk, nvk = picks["challenger"], picks["native"]
                body += ["", cp]
                for i in range(top_k):
                    c = self._pick(cpk[i] if i < len(cpk) else None)
                    n = self._pick(nvk[i] if i < len(nvk) else None)
                    body.append(f"#{i+1} Shadow: {c} | Yours: {n}")

        # ---- END-RESULT CALL: all three ranks across all three intervals ----
        er = self.ledger.latest_window_end_results(model_version=mv, checkpoints=tuple(cps))
        if er["assets"]:
            body += ["", "END-RESULT CALL · 15M, 10M & 7M",
                     "✓ = correct | ✗ = wrong | — = no official prediction"]

            def _interval_line(cp, entry) -> str:
                # An asset has at most one locked prediction per interval -> rank #1;
                # ranks #2/#3 never apply to a single asset, shown as —.
                if not entry:
                    return f"{cp}: #1 {self._NONE} | #2 {self._NONE} | #3 {self._NONE}"
                side, hit = entry
                return f"{cp}: #1 {side} {self._mark(hit)} | #2 {self._NONE} | #3 {self._NONE}"

            for a in er["assets"]:
                body += ["", f"{a['asset']} · RESULT {a['official']}", "Shadow:"]
                for cp in cps:
                    body.append(_interval_line(cp, a["checkpoints"].get(cp, {}).get("challenger")))
                body.append("Your System:")
                for cp in cps:
                    body.append(_interval_line(cp, a["checkpoints"].get(cp, {}).get("native")))

        # ---- ALL-TIME RANK RESULTS: per rank/interval + combined totals ----
        rbc = self.ledger.ranked_by_checkpoint(model_version=mv, top_k=top_k, checkpoints=tuple(cps))

        def _record_block(model_key, title):
            lines = ["", title]
            for cp in cps:
                lines.append(cp)
                m = rbc["by_checkpoint"][cp][model_key]
                for k in range(1, top_k + 1):
                    lines.append(f"#{k}: {self._wl(m[f'rank{k}'])}")
            return lines

        body += ["", "ALL-TIME RANK RESULTS"]
        body += _record_block("challenger", "SHADOW RECORD")
        body += _record_block("native", "YOUR SYSTEM RECORD")

        body += [""]
        for cp in cps:
            m = rbc["by_checkpoint"][cp]
            body += [f"{cp} TOTAL",
                     f"Shadow: {self._wl(m['challenger']['total'])}",
                     f"Yours: {self._wl(m['native']['total'])}"]

        # ---- Winner + learning state, in plain words ----
        ch, nv = rk["challenger"], rk["native"]
        cacc, nacc = ch["overall"]["accuracy"], nv["overall"]["accuracy"]
        if cacc is None or nacc is None:
            winner = "not enough data yet"
        elif cacc > nacc:
            winner = f"Shadow ahead ({round(cacc*100)}% vs {round(nacc*100)}%)"
        elif nacc > cacc:
            winner = f"Your system ahead ({round(nacc*100)}% vs {round(cacc*100)}%)"
        else:
            winner = f"tie ({round(cacc*100)}%)"
        learning = ("on (training on its own results)" if self.info.get("fitted")
                    else "warming up — need more settled cases")
        body += ["", f"Winner: {winner}", f"Learning: {learning}"]

        return (f"<b>{CHALLENGER_REPORT_MARKER} vs YOUR SYSTEM</b>\n"
                f"<pre>{chr(10).join(body)}</pre>")


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
