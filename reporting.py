"""Hourly Telegram performance report (read-only).

Once per clock hour the bot posts how it is doing: overall hit-rate (real +
paper), the most recent hour's hit-rate, realized P&L, a breakdown of WHY the
losing calls lost (attributed loss reasons), any learning adjustments now in
force, and the scalp record. Delivery is deduped across the dev + prod
instances via store.claim_event so the chat only ever gets one report per hour.
"""
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DISCLAIMER = "Paper monitor \u2014 not financial advice \u2014 no orders are placed."


def _pct(x):
    return f"{x * 100:.0f}%" if isinstance(x, (int, float)) else "n/a"


class HourlyReporter:
    def __init__(self, store, notifier, config, perf, learner, scalp, v95_ledger=None):
        self.store = store
        self.notifier = notifier
        self.cfg = config
        self.perf = perf
        self.learner = learner
        self.scalp = scalp
        # Optional: the V9.5 prediction ledger, for the interval/rank scoreboard.
        # Set after construction in app.py since the ledger is built later.
        self.v95_ledger = v95_ledger
        self._last_hour = None

    @staticmethod
    def _sb_row(label, d):
        """One aligned monospace row, or None if the bucket has no settled rows."""
        n = (d or {}).get("n") or 0
        if not n:
            return None
        wl = f"{d.get('right', 0)}-{d.get('wrong', 0)}"
        acc = d.get("accuracy")
        acc_s = f"{acc * 100:.0f}%" if isinstance(acc, (int, float)) else "-"
        rt = d.get("realized_total_cents")
        pnl = f"{rt:+.0f}¢" if (isinstance(rt, (int, float)) and d.get("pnl_n")) else "-"
        flag = " *" if d.get("low_n") else ""
        return f"{label:<8}{wl:>5}{acc_s:>6}{pnl:>7}{flag}"

    def _scoreboard_table(self):
        """Canonical track record: a compact monospace table by interval, rank, asset."""
        ledger = getattr(self, "v95_ledger", None)
        if ledger is None:
            return []
        try:
            sb = ledger.scoreboard()
        except Exception as e:
            logger.warning(f"scoreboard fetch failed: {e}")
            return []
        overall = sb.get("overall", {}) if sb.get("available") else {}
        if (overall.get("n") or 0) <= 0:
            return ["No settled predictions yet — building history."]

        acc = overall.get("accuracy")
        acc_s = f"{acc * 100:.0f}%" if isinstance(acc, (int, float)) else "n/a"
        pnl = overall.get("realized_total_cents")
        pnl_s = f" · P/L {pnl:+.0f}¢" if (isinstance(pnl, (int, float)) and overall.get("pnl_n")) else ""
        headline = f"Settled {overall['n']} · {acc_s} right{pnl_s}"

        by_cp, by_rank, by_asset = sb.get("by_checkpoint", {}), sb.get("by_rank", {}), sb.get("by_asset", {})
        groups = [
            [self._sb_row(cp, by_cp.get(cp)) for cp in ("15M", "10M", "7M")],
            [self._sb_row(f"#{k} pick", by_rank.get(k)) for k in ("1", "2", "3")],
            [self._sb_row(a, d) for a, d in sorted(
                ((a, d) for a, d in by_asset.items() if (d or {}).get("n")),
                key=lambda kv: kv[1]["n"], reverse=True)[:5]],
        ]
        body = [f"{'':<8}{'W-L':>5}{'Acc':>6}{'P/L':>7}"]
        for group in groups:
            rows = [r for r in group if r]
            if rows:
                body.append("")
                body.extend(rows)
        table = ["<b>Track record</b> (paper, after fees)", "<pre>", *body, "</pre>"]
        # Only mention the footnote if some bucket is actually flagged thin.
        if any("*" in (r or "") for group in groups for r in group):
            table.append("<i>* under 10 settled — not yet reliable</i>")
        return [headline, ""] + table

    def maybe_send(self, now):
        if not self.cfg.hourly_report_enabled or not self.notifier.enabled:
            return
        hour = datetime.now(timezone.utc).strftime("%Y%m%d%H")
        if self._last_hour is None:
            # don't fire mid-hour on (re)start; wait for the next boundary
            self._last_hour = hour
            return
        if hour == self._last_hour:
            return
        self._last_hour = hour
        if not self.store.claim_event(f"hourly:{hour}"):
            return  # another instance already sent this hour's report
        try:
            self.notifier.send(self.build_report())
        except Exception as e:
            logger.error(f"hourly report failed: {e}")

    # -- report body ----------------------------------------------------
    def _last_hour_stats(self):
        rows = self.store.query(
            "SELECT outcome, COUNT(*) AS n FROM signals "
            "WHERE settled_at >= NOW() - INTERVAL '1 hour' "
            "AND side IN ('YES','NO') AND outcome IN ('win','loss') "
            "AND state IN ('ENTRY CONFIRMED','ENTRY CANDIDATE','WATCH') "
            "GROUP BY outcome"
        )
        wins = losses = 0
        for r in rows:
            if r["outcome"] == "win":
                wins = int(r["n"])
            elif r["outcome"] == "loss":
                losses = int(r["n"])
        n = wins + losses
        return {"wins": wins, "losses": losses, "n": n,
                "win_rate": (wins / n) if n else None}

    def build_report(self):
        hh = datetime.now(timezone.utc).strftime("%H:00 UTC")
        lines = [f"\U0001f4ca <b>Hourly Report \u2014 {hh}</b>"]

        # Canonical record: the V9.5 prediction ledger (P&L, CIs, regime-aware).
        lines.extend(self._scoreboard_table())

        # Actually-sent alerts (real-money proxy), kept distinct and one line.
        try:
            stats = self.perf.stats()
        except Exception:
            stats = {}
        if stats.get("available"):
            rr = stats.get("real_record") or {}
            if rr.get("n"):
                tot = stats.get("total_realized_return")
                tail = f" \u00b7 realized {tot:+.0f}\u00a2" if isinstance(tot, (int, float)) else ""
                lines.append(f"\nSent alerts: {rr['wins']}W/{rr['losses']}L ({_pct(rr.get('win_rate'))}){tail}")

        # Scalp record, one line.
        try:
            sr = self.scalp.record()
            if sr["total"] or sr["open"]:
                lines.append(
                    f"\u26a1 Scalps: {sr['wins']}W/{sr['losses']}L "
                    f"({_pct(sr['win_rate'])}, {sr['total_realized_cents']:+.0f}\u00a2, {sr['open']} open)"
                )
        except Exception:
            pass

        lines.append(f"\n<i>{DISCLAIMER}</i>")
        return "\n".join(lines)
