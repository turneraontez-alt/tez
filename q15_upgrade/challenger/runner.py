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
from ..timez import TZ_NAME, fmt_eastern, fmt_eastern_hm

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
                features: Mapping[str, Any], quote: Mapping[str, Any],
                snapshot_id: str | None = None,
                native_predicted_side: str | None = None,
                native_prob_yes: float | None = None,
                native_rank: int | None = None) -> None:
        try:
            snap = _build_snapshot(features, quote)
            pred = self.predictor.predict(snap)
            # Cold start (model not yet trained): mirror the champion's calibrated
            # probability so the comparison starts at parity and then diverges as the
            # shadow learns. Until it has trained the shadow has no independent edge,
            # so it must NOT parrot the raw market quote (the stored quote is not a
            # YES-calibrated probability) — doing so made the untrained shadow look
            # anti-predictive. Mirror whenever untrained, regardless of the quote.
            if (not getattr(self.predictor.model, "fitted", False)
                    and control_prob_yes is not None):
                pred.prob_yes = round(float(control_prob_yes), 6)
                pred.prob_no = round(1.0 - float(control_prob_yes), 6)
            calib = getattr(self.predictor.calibrator, "name", "identity")
            # Shadow scores from EXACTLY the champion's frozen features + quote +
            # created_at for this interval, and the shared snapshot_id is stamped on
            # the paired row — so both systems demonstrably ran on the same snapshot,
            # same information cutoff, same prediction time. No newer data is fetched.
            self.ledger.record(
                pred, asset=asset, contract=ticker, checkpoint=checkpoint,
                control_prob_yes=control_prob_yes,
                native_predicted_side=native_predicted_side,
                native_prob_yes=native_prob_yes, native_rank=native_rank,
                created_at=created_at,
                close_time=close_time, model_version=self.config.model_version,
                lineage=lineage_record(self.config, calibrator_version=calib),
                snapshot_id=snapshot_id,
            )
        except Exception:
            logger.exception("challenger shadow observe failed (ignored)")

    def mark_native_sent(self, ticker: str, checkpoint: str) -> bool:
        """Record that Your System's prediction for (ticker, checkpoint) was
        delivered before close, so it counts in the visible record. Called from
        the production panel-send path on a real Telegram delivery; never raises
        into that path. Returns True if a background row was promoted to sent."""
        try:
            return self.ledger.mark_native_sent(str(ticker), str(checkpoint),
                                                model_version=self.config.model_version)
        except Exception:
            logger.exception("challenger shadow mark_native_sent failed (ignored)")
            return False

    def mark_native_delivery_failed(self, ticker: str, checkpoint: str, error: str) -> bool:
        """Record that Your System generated this pick but the official send failed.
        The row stays background (out of the visible totals) with the exact error
        preserved. Never raises into the production send path."""
        try:
            return self.ledger.mark_native_delivery_failed(
                str(ticker), str(checkpoint), str(error),
                model_version=self.config.model_version)
        except Exception:
            logger.exception("challenger shadow mark_native_delivery_failed failed (ignored)")
            return False

    def mark_native_pending(self, ticker: str, checkpoint: str, delivery_key: str) -> bool:
        """Tag a generated pick with its official report's outbox key (durably
        queued, outcome TBD). Never raises into the production send path."""
        try:
            return self.ledger.mark_native_pending(
                str(ticker), str(checkpoint), str(delivery_key),
                model_version=self.config.model_version)
        except Exception:
            logger.exception("challenger shadow mark_native_pending failed (ignored)")
            return False

    def reconcile_native_delivery(self, status_lookup: Any) -> dict:
        """Resolve pending native picks from the outbox's true delivery status.
        Never raises into the production path."""
        try:
            return self.ledger.reconcile_native_delivery(
                status_lookup, model_version=self.config.model_version)
        except Exception:
            logger.exception("challenger shadow reconcile_native_delivery failed (ignored)")
            return {"promoted": 0, "failed": 0}

    def delivery_audit(self) -> dict:
        try:
            return self.ledger.delivery_audit(model_version=self.config.model_version)
        except Exception:
            logger.exception("challenger shadow delivery_audit failed (ignored)")
            return {"SENT": 0, "DELIVERY_FAILED": 0, "PENDING": 0}

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
        return self.ledger.comparison(model_version=self.config.model_version,
                                      native_sent_only=self.config.native_sent_only)

    def ranked(self, top_k: int = 3) -> dict:
        return self.ledger.ranked_comparison(model_version=self.config.model_version, top_k=top_k,
                                             native_sent_only=self.config.native_sent_only)

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
        sent_only = self.config.native_sent_only
        cps = list(REPORT_CHECKPOINTS)
        rk = self.ledger.ranked_comparison(model_version=mv, top_k=top_k, native_sent_only=sent_only)

        # All visible timestamps are Eastern (America/Detroit), DST-aware: the label
        # reads EDT in summer, EST in winter — never a fixed offset. UTC stays
        # internal (storage, contract matching); only the DISPLAY is converted.
        reset_s = fmt_eastern(self.reset_at)  # e.g. "2026-06-21 14:53 EDT"
        body: list[str] = [
            "Shadow test · read-only · never trades",
            "Picks ranked by confidence; #1 = most confident",
            "Correct = the predicted side matched the final result",
            f"Comparison reset: {reset_s}",
            "Synchronization: Same snapshot and prediction time",
            f"Time zone: {TZ_NAME}",
        ]

        if not rk["n_cases"]:
            # Post-reset, pre-data: the exact required banner. Both systems start at
            # 0W–0L | N/A and only predictions created after reset_at will count.
            body += ["", "No settled cases since reset yet.",
                     "Shadow: 0W–0L | N/A", "Your System: 0W–0L | N/A"]
            archived = self.ledger.archived_versions(mv)
            if archived:
                body += ["", f"Pre-reset archive (PRE-SYNCHRONIZED-RESET, excluded): "
                             f"{', '.join(archived)}"]
            return (f"<b>{CHALLENGER_REPORT_MARKER} vs YOUR SYSTEM</b>\n"
                    f"<pre>{chr(10).join(body)}</pre>")

        # ---- LAST WINDOW: three ranked picks per interval, side by side ----
        win = self.ledger.latest_window_cases(model_version=mv, top_k=top_k, native_sent_only=sent_only)
        if win["close"]:
            when = fmt_eastern_hm(win["close"])  # "HH:MM EDT" / "HH:MM EST"
            body += ["", f"LAST WINDOW · {when}"]
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

        # ---- END-RESULT CALL: ranked #1/#2/#3 picks at EVERY interval (15M/10M/7M),
        # Shadow vs Your System, graded against the settled result (✓/✗). It "tries its
        # best on each one": each interval is filled from its OWN most-recent settled
        # window that best fills the ranks (prefer a window with >= top_k graded picks,
        # else the fullest), so 10M/7M no longer go blank just because their latest
        # settled window is older than 15M's, and #2/#3 fill whenever a recent window
        # had that many assets. A slot only reads — when no settled pick exists for it
        # at all. Each interval is labelled with its own window time (they can differ). ----
        grid = self.ledger.best_filled_window_cases(model_version=mv, top_k=top_k,
                                                    native_sent_only=sent_only)
        if grid["checkpoints"]:
            body += ["", "END-RESULT CALL · 15M, 10M & 7M",
                     "✓ = correct | ✗ = wrong | — = no pick"]
            for cp in cps:
                slot = grid["checkpoints"].get(cp) or {}
                cpk = slot.get("challenger") or []
                nvk = slot.get("native") or []
                when = fmt_eastern_hm(slot["close"]) if slot.get("close") else None
                body += ["", f"{cp} · {when}" if when else cp]
                for i in range(top_k):
                    c = self._pick(cpk[i] if i < len(cpk) else None)
                    n = self._pick(nvk[i] if i < len(nvk) else None)
                    body.append(f"#{i + 1} Shadow: {c} | Yours: {n}")

        # ---- ALL-TIME RANK RESULTS: per rank/interval + combined totals ----
        rbc = self.ledger.ranked_by_checkpoint(model_version=mv, top_k=top_k, checkpoints=tuple(cps),
                                               native_sent_only=sent_only)

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

        # Your System delivery audit — explains an empty "Yours" record honestly:
        # SENT picks count; DELIVERY_FAILED were generated but the send failed (kept
        # as background with the error); PENDING are generated but not yet sent/closed.
        audit = self.ledger.delivery_audit(model_version=mv)
        body += [f"Your System delivery: {audit['SENT']} sent · "
                 f"{audit['DELIVERY_FAILED']} failed · {audit['PENDING']} pending"]
        if audit["SENT"] == 0 and (audit["DELIVERY_FAILED"] or audit["PENDING"]):
            body += ["Yours shows — because no official report has been delivered yet "
                     "(picks generated; delivery not confirmed)."]

        archived = self.ledger.archived_versions(mv)
        if archived:
            body += ["", f"Pre-reset archive (PRE-SYNCHRONIZED-RESET, excluded): "
                         f"{', '.join(archived)}"]

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
