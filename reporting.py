"""Hourly Telegram performance report (read-only).

Once per clock hour the bot posts its track record, led by the canonical V9.5
prediction ledger: accuracy and realized paper P&L by interval (15M/10M/7M),
pick rank (#1/#2/#3) and asset, plus the actually-sent alert record and the
scalp line. Delivery is deduped across the dev + prod instances via
store.claim_event so the chat only ever gets one report per hour.
"""
import logging
import os
from datetime import datetime, timezone, timedelta

try:  # stdlib on 3.9+; needs the `tzdata` wheel on a bare container.
    from zoneinfo import ZoneInfo
    _EASTERN = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - fallback if tz database is missing
    _EASTERN = None

logger = logging.getLogger(__name__)

DISCLAIMER = "Paper monitor \u2014 not financial advice \u2014 no orders are placed."


def _env_int(name, default, low, high):
    try:
        return max(low, min(high, int(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


def _eastern_header():
    """Top-of-hour label in US Eastern time (auto EST/EDT), e.g. '3:00 PM EDT'.

    Falls back to a fixed EST offset only if the tz database is unavailable.
    """
    if _EASTERN is not None:
        local = datetime.now(_EASTERN)
    else:  # pragma: no cover - exercised only without tzdata
        local = datetime.now(timezone(timedelta(hours=-5), "EST"))
    return local.strftime("%I:00 %p %Z").lstrip("0")


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
    def _sb_row(label, d, placeholder=False):
        """One aligned monospace row.

        Returns None for an empty bucket unless ``placeholder`` is set, in which
        case a zeroed "awaiting data" row is rendered — used for the checkpoint
        group so 15M/10M/7M are always visible even before they settle.
        """
        n = (d or {}).get("n") or 0
        if not n:
            if placeholder:
                return f"{label:<8}{'0-0':>5}{'—':>6}{'—':>7}"
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

        by_cp, by_asset = sb.get("by_checkpoint", {}), sb.get("by_asset", {})
        # Rank record for the primary (10M) interval on its own — the #1/#2/#3 pick
        # judged within 10M rather than blended across every interval.
        rank_10m = (sb.get("rank_by_checkpoint", {}) or {}).get("10M", {})
        # Each group is (optional section header, rows). placeholder=True keeps the
        # 10M rank rows visible (0-0 — —) before they settle.
        groups = [
            (None, [self._sb_row(cp, by_cp.get(cp), placeholder=True) for cp in ("15M", "10M", "7M")]),
            ("10M RANK PERFORMANCE", [self._sb_row(f"#{k} pick", rank_10m.get(k), placeholder=True) for k in ("1", "2", "3")]),
            (None, [self._sb_row(a, d) for a, d in sorted(
                ((a, d) for a, d in by_asset.items() if (d or {}).get("n")),
                key=lambda kv: kv[1]["n"], reverse=True)[:5]]),
        ]
        body = [f"{'':<8}{'W-L':>5}{'Acc':>6}{'P/L':>7}"]
        for header, group in groups:
            rows = [r for r in group if r]
            if rows:
                body.append("")
                if header:
                    body.append(header)
                body.extend(rows)
        table = ["<b>Track record</b> (paper, after fees)", "<pre>", *body, "</pre>"]
        # Only mention the footnote if some bucket is actually flagged thin.
        if any("*" in (r or "") for _header, group in groups for r in group):
            table.append("<i>* under 10 settled — not yet reliable</i>")
        return [headline, ""] + table + self._manipulation_lines(sb)

    @staticmethod
    def _manipulation_lines(sb):
        """Compact "is the model worse when manipulation is suspected?" summary.

        Suspected-vs-clean accuracy + P/L, then a per-tell line (pin / absorption /
        divergence) so the lowest-accuracy tell — the one most often preceding a
        flip — stands out. Empty until something has settled under a flag."""
        bm = sb.get("by_manipulation") or {}
        susp, clean = bm.get("suspected") or {}, bm.get("clean") or {}
        if not (susp.get("n") or 0):
            return []

        def _acc(d):
            a = d.get("accuracy")
            return f"{a * 100:.0f}%" if isinstance(a, (int, float)) else "-"

        def _pnl(d):
            rt = d.get("realized_total_cents")
            return f" {rt:+.0f}¢" if (isinstance(rt, (int, float)) and d.get("pnl_n")) else ""

        head = (f"⚠ <b>Manipulation watch</b> — suspected {susp.get('n', 0)} · "
                f"{_acc(susp)} right{_pnl(susp)} vs clean {clean.get('n', 0)} · {_acc(clean)} right")
        out = ["", head]
        by_reason = bm.get("by_reason") or {}
        if by_reason:
            parts = [f"{r} {d.get('right', 0)}-{d.get('wrong', 0)} {_acc(d)}" for r, d in by_reason.items()]
            out.append("by tell: " + " · ".join(parts))
        return out

    def _flip_warning_lines(self):
        """MANIPULATION WARNING PERFORMANCE — precision, detection rate, advance
        time and P&L of fired HIGH FLIP RISK warnings, plus the learned flip-rate
        calibration by score bucket. Empty until warnings have been reconciled."""
        ledger = getattr(self, "v95_ledger", None)
        if ledger is None:
            return []
        try:
            perf = ledger.flip_warning_performance()
            stats = ledger.flip_stats()
        except Exception as e:
            logger.warning(f"flip performance fetch failed: {e}")
            return []
        o = perf.get("overall") or {}
        out = []
        if o.get("alerts"):
            def _s(v, suffix=""):
                return f"{v}{suffix}" if isinstance(v, (int, float)) else "—"
            adv = o.get("avg_advance_seconds")
            adv_s = f"{int(adv)//60}m {int(adv)%60:02d}s" if isinstance(adv, (int, float)) else "—"
            pnl = o.get("realized_total_cents")
            out += [
                "", "⚠ <b>MANIPULATION WARNING PERFORMANCE</b>",
                f"Alerts: {o.get('alerts', 0)} · {o.get('correct', 0)} correct / {o.get('false', 0)} false "
                f"· precision {_pct(o.get('precision'))}",
                f"Flips: {o.get('detected', 0)}/{o.get('actual_flips', 0)} detected "
                f"({_pct(o.get('detection_rate'))}) · {o.get('missed', 0)} missed",
                f"Avg advance {adv_s} · P/L {pnl:+.0f}¢" if isinstance(pnl, (int, float)) else f"Avg advance {adv_s}",
            ]
        # Learned flip-rate curve for the primary interval (10M), both directions,
        # so you can see what risk score has historically preceded a flip.
        if stats.get("available"):
            for direction in ("NO → YES", "YES → NO"):
                scope = (stats.get("by_checkpoint", {}).get("10M", {}) or {}).get(direction, {}).get("overall", {})
                buckets = scope.get("buckets") or {}
                if scope.get("samples"):
                    curve = " · ".join(
                        f"{lbl}:{_pct(b['flip_rate'])}"
                        for lbl, b in buckets.items() if b.get("n")
                    )
                    out += ["", f"10M {direction} flip-rate by risk ({scope.get('samples')} obs): {curve or '—'}"]
        return out

    def maybe_send(self, now):
        if not self.cfg.hourly_report_enabled or not self.notifier.enabled:
            return
        utc = datetime.now(timezone.utc)
        hour = utc.strftime("%Y%m%d%H")
        first_call = self._last_hour is None
        if not first_call and hour == self._last_hour:
            return
        self._last_hour = hour
        # On (re)start, only catch up the current hour while we're still near the
        # top of it — otherwise wait for the next boundary so a late restart never
        # sends a stale, badly-delayed report. The cross-instance claim below keeps
        # this idempotent. (Was: always skip the first hour after start, which on a
        # frequently-restarting host could delay/drop reports.)
        if first_call and utc.minute >= _env_int("Q15_HOURLY_CATCHUP_MINUTES", 5, 0, 59):
            return
        if not self.store.claim_event(f"hourly:{hour}"):
            return  # another instance already sent this hour's report
        try:
            self.notifier.send(self.build_report())
            # Surface how far past the hour delivery actually happened, so a
            # recurring lateness (e.g. a sleeping/ restarting host) is visible.
            logger.info("Hourly report sent for %s at :%02d past the hour", hour, utc.minute)
        except Exception as e:
            logger.error(f"hourly report failed: {e}")

    # -- report body ----------------------------------------------------
    def build_report(self):
        hh = _eastern_header()
        lines = [f"\U0001f4ca <b>Hourly Report \u2014 {hh}</b>"]

        # Canonical record: the V9.5 prediction ledger (P&L, CIs, regime-aware).
        lines.extend(self._scoreboard_table())

        # Flip-risk warning performance (precision / detection / advance / P&L).
        lines.extend(self._flip_warning_lines())

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
