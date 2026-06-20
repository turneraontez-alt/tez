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

DISCLAIMER = ("Model-based estimate, not financial advice. "
              "Read-only monitor \u2014 no orders are placed.")


def _pct(x):
    return f"{x * 100:.0f}%" if isinstance(x, (int, float)) else "n/a"


def _rec(r):
    return f"{r['wins']}W/{r['losses']}L \u2014 {_pct(r['win_rate'])} (n={r['n']})"


def _wl(d):
    """Format a {right,wrong,n,accuracy,low_n} scoreboard bucket, or None if empty."""
    n = (d or {}).get("n") or 0
    if not n:
        return None
    acc = d.get("accuracy")
    pct = f"{acc * 100:.0f}%" if isinstance(acc, (int, float)) else "n/a"
    parts = [pct]
    # Realized paper P&L after fees, when any priced entries resolved.
    rt = d.get("realized_total_cents")
    if isinstance(rt, (int, float)) and d.get("pnl_n"):
        parts.append(f"{rt:+.0f}¢")
    # Flag thin samples so a 2-of-2 = 100% bucket is not over-read.
    if d.get("low_n"):
        parts.append("low n")
    return f"{d.get('right', 0)}W/{d.get('wrong', 0)}L ({', '.join(parts)})"


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

    def _scoreboard_lines(self):
        """Right/wrong record by interval (15M/10M/7M) and by pick rank (#1/#2/#3)."""
        ledger = getattr(self, "v95_ledger", None)
        if ledger is None:
            return []
        try:
            sb = ledger.scoreboard()
        except Exception as e:
            logger.warning(f"scoreboard fetch failed: {e}")
            return []
        if not sb.get("available") or (sb.get("overall", {}).get("n") or 0) <= 0:
            return []
        lines = ["\n\U0001f4c8 <b>Track record (V9.5 paper ledger)</b>"]
        by_cp = sb.get("by_checkpoint", {})
        interval = [f"{cp}: {_wl(by_cp.get(cp))}" for cp in ("15M", "10M", "7M") if _wl(by_cp.get(cp))]
        if interval:
            lines.append("By interval — " + "  ·  ".join(interval))
        by_rank = sb.get("by_rank", {})
        ranked = [f"{label}: {_wl(by_rank.get(key))}"
                  for key, label in (("1", "#1"), ("2", "#2"), ("3", "#3")) if _wl(by_rank.get(key))]
        if ranked:
            lines.append("By pick rank — " + "  ·  ".join(ranked))
        # By asset, busiest first, capped so the line stays readable.
        by_asset = sb.get("by_asset", {})
        asset_items = sorted(
            ((a, d) for a, d in by_asset.items() if (d or {}).get("n")),
            key=lambda kv: kv[1]["n"], reverse=True,
        )[:6]
        asset_parts = [f"{a}: {_wl(d)}" for a, d in asset_items if _wl(d)]
        if asset_parts:
            lines.append("By asset — " + "  ·  ".join(asset_parts))
        return lines

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
        stats = self.perf.stats()
        lines = [f"\U0001f4ca <b>Hourly Report \u2014 {hh}</b>"]

        if not stats.get("available"):
            lines.append("No performance data yet (persistence disabled).")
            lines.append(f"<i>{DISCLAIMER}</i>")
            return "\n".join(lines)

        overall_n = stats["settled_entries"]
        lines.append(
            f"Overall: {stats['wins']}W/{stats['losses']}L \u2014 "
            f"{_pct(stats['win_rate'])} (n={overall_n}, {stats['pending_entries']} pending)"
        )
        lines.append(f"\u2022 Real alerts: {_rec(stats['real_record'])}")
        lines.append(f"\u2022 Paper (untraded): {_rec(stats['paper_record'])}")

        lh = self._last_hour_stats()
        lines.append(
            f"Last hour: {lh['wins']}W/{lh['losses']}L \u2014 "
            f"{_pct(lh['win_rate'])} (n={lh['n']})"
        )
        avg = stats.get("avg_realized_return")
        tot = stats.get("total_realized_return")
        if avg is not None:
            lines.append(f"Realized: {avg:+.1f}\u00a2 avg, {tot:+.1f}\u00a2 total")

        # factors associated with loss risk (association, not causation)
        reasons = self.learner.loss_reason_summary(24)
        if reasons:
            lines.append("\n<b>Factors associated with higher loss risk:</b>")
            for reason, cnt in list(reasons.items())[:6]:
                lines.append(f"\u2022 {reason.replace('_', ' ')}: coefficient {float(cnt):+.3f}")

        # learning adjustments in force
        adj = self.learner.active_adjustments()
        if adj:
            lines.append("\n<b>Learning adjustments active:</b>")
            for a in adj[:6]:
                live_suppressed = bool(a.get("suppressed"))
                shadow_suppressed = bool(a.get("shadow_suppressed"))
                tag = " [SUPPRESSED]" if live_suppressed else " [SHADOW SUPPRESS]" if shadow_suppressed else ""
                pen = a.get("edge_penalty", a.get("edge_adjustment", 0)) or 0
                shadow_pen = a.get("shadow_edge_adjustment", pen) or 0
                post = _pct(a.get("posterior_win_rate"))
                mode = "live" if abs(float(pen)) > 0 else "shadow"
                shown = pen if mode == "live" else shadow_pen
                lines.append(
                    f"\u2022 {a.get('segment')} {shown:+.1f}pp ({mode}; post {post}, n={a.get('n')}){tag}"
                )

        # scalp record
        try:
            sr = self.scalp.record()
            if sr["total"] or sr["open"]:
                lines.append(
                    f"\n\u26a1 Scalps: {sr['wins']}W/{sr['losses']}L \u2014 "
                    f"{_pct(sr['win_rate'])} (n={sr['total']}, "
                    f"{sr['total_realized_cents']:+.1f}\u00a2, {sr['open']} open)"
                )
        except Exception:
            pass

        lines.extend(self._scoreboard_lines())

        lines.append(f"\n<i>{DISCLAIMER}</i>")
        return "\n".join(lines)
