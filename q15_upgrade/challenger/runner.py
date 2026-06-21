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

    @staticmethod
    def _pick_str(entry) -> str:
        """One pick as fixed-width 'ASSET SIDE mark' (13 cols), so the two model
        columns stay aligned in the monospace card."""
        asset, side, correct = entry
        return f"{str(asset)[:4]:<4} {side:<3} {'ok' if correct else 'X'}".ljust(13)

    @staticmethod
    def _acc_pct(accuracy) -> str:
        return "—" if accuracy is None else f"{round(accuracy * 100)}%"

    def report_message(self, top_k: int = 3) -> str:
        """A single clean card: only the title is bright (bold); everything else
        sits in one monospace block so it reads as one aligned, dim panel."""
        mv = self.config.model_version
        rk = self.ledger.ranked_comparison(model_version=mv, top_k=top_k)
        if not rk["n_cases"]:
            return f"{CHALLENGER_REPORT_MARKER} — no settled shadow cases yet"
        ch, nv = rk["challenger"], rk["native"]

        body: list[str] = [
            "Shadow test · read-only · never trades",
            "Picks ranked by confidence; #1 = most confident.",
            "Correct = the pick's side matched the result.",
        ]

        # Latest window — only ranks that actually had a pick (no empty dashes).
        win = self.ledger.latest_window_cases(model_version=mv, top_k=top_k)
        if win["close"]:
            when = time.strftime("%H:%M", time.gmtime(win["close"]))
            body += ["", f"LAST WINDOW · {when} UTC"]
            for cp, picks in sorted(win["checkpoints"].items()):
                cps, nps = picks["challenger"], picks["native"]
                body.append(f"{cp:<4} {'Shadow':<13}{'Yours'}")
                for i in range(max(len(cps), len(nps))):
                    c = self._pick_str(cps[i]) if i < len(cps) else "".ljust(13)
                    n = self._pick_str(nps[i]) if i < len(nps) else ""
                    body.append(f" #{i+1}  {c}{n}".rstrip())

        # End-result calls at 15M & 10M, per asset — did both models call the
        # final outcome right as the window counted down? (Y/N + right/wrong.)
        er = self.ledger.latest_window_end_results(model_version=mv, checkpoints=("15M", "10M"))
        if er["assets"]:
            cps = list(er["checkpoints"])
            w = len(cps) * 4
            body += ["", "END-RESULT CALL · 15M & 10M",
                     "(side Y/N · + right · - wrong)",
                     f"{'':<5}{'Res':<4}{'Shadow':<{w}} {'Yours':<{w}}",
                     f"{'':<5}{'':<4}" + "".join(f"{cp:<4}" for cp in cps)
                     + " " + "".join(f"{cp:<4}" for cp in cps)]

            def cell(entry) -> str:
                if not entry:
                    return "-".ljust(4)
                side, hit = entry
                return f"{side[0]}{'+' if hit else '-'}".ljust(4)

            for a in er["assets"]:
                res = a["official"][0]
                sh = "".join(cell(a["checkpoints"].get(cp, {}).get("challenger")) for cp in cps)
                nv_ = "".join(cell(a["checkpoints"].get(cp, {}).get("native")) for cp in cps)
                body.append(f"{a['asset'][:4]:<5}{res:<4}{sh} {nv_}".rstrip())

        # Running totals — hit (correct/total) + accuracy, both models side by side.
        body += ["", f"TOTALS · {rk['n_cases']} cases",
                 f"{'':<6}{'Shadow':>12}{'Yours':>14}",
                 f"{'':<6}{'hit':>6}{'acc':>6}{'hit':>8}{'acc':>6}"]

        def row(label, cd, nd):
            ch_hit = f"{cd['correct']}/{cd['correct'] + cd['wrong']}"
            nv_hit = f"{nd['correct']}/{nd['correct'] + nd['wrong']}"
            return (f"{label:<6}{ch_hit:>6}{self._acc_pct(cd['accuracy']):>6}"
                    f"{nv_hit:>8}{self._acc_pct(nd['accuracy']):>6}")

        for k in range(1, top_k + 1):
            body.append(row(f"Top-{k}", ch[f"rank{k}"], nv[f"rank{k}"]))
        body.append(row("All", ch["overall"], nv["overall"]))

        # Winner + learning state, in plain words.
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
