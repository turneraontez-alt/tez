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
        # Rank record per interval on its own — the #1/#2/#3 pick judged within that
        # checkpoint rather than blended across every interval.
        rank_by_cp = sb.get("rank_by_checkpoint", {}) or {}
        rank_15m = rank_by_cp.get("15M", {})
        rank_10m = rank_by_cp.get("10M", {})
        # Each group is (optional section header, rows). placeholder=True keeps the
        # rank rows visible (0-0 — —) before they settle.
        groups = [
            (None, [self._sb_row(cp, by_cp.get(cp), placeholder=True) for cp in ("15M", "10M", "7M")]),
            ("15M RANK PERFORMANCE", [self._sb_row(f"#{k} pick", rank_15m.get(k), placeholder=True) for k in ("1", "2", "3")]),
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
        # Plain text only — build_report wraps the whole report in one <pre> panel.
        table = ["Track record (paper, after fees)", *body]
        # Only mention the footnote if some bucket is actually flagged thin.
        if any("*" in (r or "") for _header, group in groups for r in group):
            table.append("* under 10 settled — not yet reliable")
        return [headline, ""] + table + self._pushed_vs_background_lines(sb) + self._manipulation_lines(sb)

    def _setup_scan_lines(self):
        """Compact, honest 10M setup-scan block for the hourly report. Reads only
        settled rows; runs the leakage-safe miner; reports whether anything holds
        up out-of-sample. Returns [] (silent) when disabled, no ledger, or no
        settled 10M rows yet — so it never adds noise before there is data."""
        if (os.environ.get("Q15_SETUP_MINER_ENABLED", "true") or "true").strip().lower() in {"0", "false", "no", "off"}:
            return []
        ledger = getattr(self, "v95_ledger", None)
        if ledger is None or not hasattr(ledger, "resolved_setup_rows"):
            return []
        try:
            from q15_upgrade import setup_miner
            rows = ledger.resolved_setup_rows("10M")
            if not rows:
                return []
            cfg = setup_miner.MineConfig.from_env("10M")
            report = setup_miner.mine(rows, cfg)
            return ["", *setup_miner.build_report_lines(report, cfg)]
        except Exception as e:
            logger.warning(f"setup scan skipped: {e}")
            return []

    def _shadow_economics_lines(self):
        """Compact live-vs-shadow entry-economics A/B for the hourly report. Reads
        only settled rows; never affects live decisions. Silent when disabled, no
        ledger, or no settled rows with an executable ask yet."""
        ledger = getattr(self, "v95_ledger", None)
        if ledger is None or not hasattr(ledger, "resolved_economics_rows"):
            return []
        try:
            from q15_upgrade import shadow_economics
            cfg = shadow_economics.EconConfig.from_env()
            if not cfg.enabled:
                return []
            rows = ledger.resolved_economics_rows()
            if not rows:
                return []
            cmp = shadow_economics.compare(rows, cfg)
            if cmp.rows_considered == 0:
                return []
            return ["", *shadow_economics.build_report_lines(cmp)]
        except Exception as e:
            logger.warning(f"shadow economics A/B skipped: {e}")
            return []

    def _shadow_signal_lines(self):
        """Compact experimental-signal A/B for the hourly report: per signal, the
        out-of-sample Brier change vs the champion, significance, and sample size.
        Read-only; never affects live decisions. Silent when disabled, no ledger,
        or not enough settled rows carrying recorded signals yet."""
        ledger = getattr(self, "v95_ledger", None)
        if ledger is None or not hasattr(ledger, "shadow_signal_experiment"):
            return []
        try:
            from q15_upgrade import shadow_signals
            cfg = shadow_signals.SignalConfig.from_env()
            if not cfg.enabled:
                return []
            rows = ledger.resolved_shadow_signal_rows()
            if not rows:
                return []
            scores = shadow_signals.evaluate(rows, cfg)
            return shadow_signals.build_report_lines(scores)
        except Exception as e:
            logger.warning(f"shadow signal A/B skipped: {e}")
            return []

    def _timing_experiment_lines(self):
        """Per-mark accuracy of the OBSERVATIONAL entry-timing experiment so the
        owner can SEE where (e.g. 13 vs 12 vs 11 min left vs the live 10M)
        accuracy crosses into an edge. Read-only; silent until rows resolve."""
        ledger = getattr(self, "v95_ledger", None)
        if ledger is None or not hasattr(ledger, "timing_experiment_scoreboard"):
            return []
        try:
            sb = ledger.timing_experiment_scoreboard()
        except Exception as e:
            logger.warning(f"timing experiment scoreboard skipped: {e}")
            return []
        by_mark = (sb or {}).get("by_mark") or {}
        rows = [(int(m), d) for m, d in by_mark.items() if (d.get("n") or 0) or (d.get("pending") or 0)]
        if not rows:
            return []
        out = ["", "Entry-timing experiment (observational — never traded)"]
        for mark, d in sorted(rows, key=lambda kv: kv[0], reverse=True):
            n = d.get("n") or 0
            acc = _pct(d.get("accuracy")) if n else "—"
            star = "*" if d.get("low_n") else ("✓" if d.get("ci_excludes_half") else "")
            pend = d.get("pending") or 0
            tail = f" (+{pend} pending)" if pend else ""
            out.append(f"{d.get('minutes', round(mark/60.0,1))}m left: {acc} ({n}){star}{tail}")
        out.append("* under 10 settled — not yet reliable")
        return out

    @staticmethod
    def _pushed_vs_background_lines(sb):
        """Two separate records: predictions actually PUSHED to the user vs every
        background observation. Background never inflates the pushed number."""
        bp = sb.get("by_pushed") or {}
        pushed, bg = bp.get("pushed") or {}, bp.get("background") or {}
        if not ((pushed.get("n") or 0) or (bg.get("n") or 0)):
            return []

        def _line(label, d):
            n = d.get("n") or 0
            if not n:
                return f"{label:<12}{'0-0':>6}{'—':>6}"
            acc = d.get("accuracy")
            acc_s = f"{acc * 100:.0f}%" if isinstance(acc, (int, float)) else "-"
            wl = f"{d.get('right', 0)}-{d.get('wrong', 0)}"
            return f"{label:<12}{wl:>6}{acc_s:>6}"

        return ["", "PUSHED vs BACKGROUND", f"{'':<12}{'W-L':>6}{'Acc':>6}",
                _line("Pushed", pushed), _line("Background", bg)]

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

        head = (f"⚠ Manipulation watch — suspected {susp.get('n', 0)} · "
                f"{_acc(susp)} right{_pnl(susp)} vs clean {clean.get('n', 0)} · {_acc(clean)} right")
        out = ["", head]
        by_reason = bm.get("by_reason") or {}
        if by_reason:
            parts = [f"{r} {d.get('right', 0)}-{d.get('wrong', 0)} {_acc(d)}" for r, d in by_reason.items()]
            out.append("by tell: " + " · ".join(parts))
        return out

    @staticmethod
    def _flip_row(label, agg, placeholder=False):
        """A flip-warning row mapped onto the SAME grid as ``_sb_row``:
        correct-false becomes W-L, precision becomes Acc, realized cents P/L."""
        a = agg or {}
        n = a.get("alerts") or 0
        if not n:
            if placeholder:
                return f"{label:<8}{'0-0':>5}{'—':>6}{'—':>7}"
            return None
        d = {
            "n": n,
            "right": a.get("correct", 0),
            "wrong": a.get("false", 0),
            "accuracy": a.get("precision"),
            "realized_total_cents": a.get("realized_total_cents"),
            "pnl_n": 1 if a.get("realized_total_cents") else 0,
            "low_n": n < 10,
        }
        return HourlyReporter._sb_row(label, d)

    @staticmethod
    def _flip_rate_curve(stats):
        """Learned flip-rate-by-risk for the primary (10M) interval, both
        directions, as an aligned mini-table (risk bucket · obs · flip rate) —
        same left-label/right-number grammar as the interval scoreboard."""
        if not stats.get("available"):
            return []
        out = []
        for direction in ("NO → YES", "YES → NO"):
            scope = (stats.get("by_checkpoint", {}).get("10M", {}) or {}).get(direction, {}).get("overall", {})
            rows = [(lbl, b) for lbl, b in (scope.get("buckets") or {}).items() if b.get("n")]
            if not rows:
                continue
            out += ["", f"LEARNED FLIP RATE · 10M {direction.replace(' ', '')} ({scope.get('samples')} obs)",
                    f"{'risk':<8}{'obs':>5}{'flip':>7}"]
            out += [f"{lbl:<8}{b['n']:>5}{_pct(b['flip_rate']):>7}" for lbl, b in rows]
        return out

    def _flip_scoreboard(self):
        """Flip-warning track record in the SAME aligned table the interval
        scoreboard uses: a W-L/Acc/P-L grid by checkpoint, direction and asset
        (precision stands in for accuracy, correct-false for W-L), followed by
        the learned flip-rate-by-risk curve. Empty until a warning settles."""
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
        if not o.get("alerts"):
            # Nothing fired. Distinguish "alert channel intentionally OFF" from
            # "armed but nothing tripped" so the empty record is never misread as
            # a detection failure (the raw perf shows 0 detected / N missed).
            curve = self._flip_rate_curve(stats)
            if not perf.get("alerts_enabled", False):
                note = ["", "⚠ FLIP WARNING PERFORMANCE",
                        "Flip-risk alerts disabled (tracked on dashboard only)"]
                return note + curve if curve else note
            return curve

        # Headline mirrors the interval headline; it carries the detection rate
        # and advance time, which don't fit the four-column grid.
        adv = o.get("avg_advance_seconds")
        adv_s = f" · warn {int(adv)//60}m{int(adv)%60:02d}s" if isinstance(adv, (int, float)) else ""
        headline = (f"Warned {o.get('alerts', 0)} · precision {_pct(o.get('precision'))} · "
                    f"caught {o.get('detected', 0)}/{o.get('actual_flips', 0)} "
                    f"({_pct(o.get('detection_rate'))}){adv_s}")

        by_cp = perf.get("by_checkpoint", {})
        by_dir = perf.get("by_direction", {})
        by_asset = perf.get("by_asset", {})
        groups = [
            (None, [self._flip_row(cp, by_cp.get(cp), placeholder=True) for cp in ("15M", "10M", "7M")]),
            ("BY DIRECTION", [self._flip_row(d.replace(" ", ""), by_dir.get(d)) for d in ("NO → YES", "YES → NO")]),
            ("BY ASSET", [self._flip_row(a, d) for a, d in sorted(
                ((a, d) for a, d in by_asset.items() if (d or {}).get("alerts")),
                key=lambda kv: kv[1]["alerts"], reverse=True)[:5]]),
        ]
        body = [f"{'':<8}{'W-L':>5}{'Acc':>6}{'P/L':>7}"]
        for header, group in groups:
            rows = [r for r in group if r]
            if rows:
                body.append("")
                if header:
                    body.append(header)
                body.extend(rows)
        out = ["", "⚠ FLIP WARNING PERFORMANCE", headline, "", *body]
        if o.get("missed"):
            out.append(f"Missed {o.get('missed')} flip(s) with no warning fired")
        # Same thin-sample footnote convention as the interval scoreboard.
        if any("*" in (r or "") for _h, group in groups for r in group):
            out.append("* under 10 fired — not yet reliable")
        return out + self._flip_rate_curve(stats)

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
        # The bold "Hourly Report \u2014" header MUST stay outside the panel: the
        # reformatters detect (and bypass) the canonical report by this header.
        # Everything else goes INSIDE one <pre> block so the whole report renders
        # as a single panel (matching the checkpoint alert).
        header = f"\U0001f4ca <b>Hourly Report \u2014 {hh}</b>"
        body = []

        # Canonical record: the V9.5 prediction ledger (P&L, CIs, regime-aware).
        body.extend(self._scoreboard_table())

        # Flip-warning track record, rendered in the same table as the intervals.
        body.extend(self._flip_scoreboard())

        # 10M setup scan: leakage-safe, out-of-sample-validated search for a
        # combination of decision-time conditions that predicts the outcome.
        # Self-silencing until enough settled rows exist to discover + validate.
        body.extend(self._setup_scan_lines())

        # Entry-economics shadow A/B: live price gate vs a stricter cost-aware gate,
        # graded on the same settled trades. Read-only; live recs are unchanged.
        body.extend(self._shadow_economics_lines())

        # Experimental-signal A/B: do the five background signals (flow persistence,
        # book resiliency, stability, low-noise, regime stability) improve the
        # probability out of sample? Read-only; default-OFF; promotion stays manual.
        body.extend(self._shadow_signal_lines())

        # Entry-timing experiment: observational accuracy by time-to-close mark
        # (13/12/11 min …), so the optimal entry time is measured, not guessed.
        body.extend(self._timing_experiment_lines())

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
                body.append(f"\nSent alerts: {rr['wins']}W/{rr['losses']}L ({_pct(rr.get('win_rate'))}){tail}")

        # Scalp record, one line.
        try:
            sr = self.scalp.record()
            if sr["total"] or sr["open"]:
                body.append(
                    f"\u26a1 Scalps: {sr['wins']}W/{sr['losses']}L "
                    f"({_pct(sr['win_rate'])}, {sr['total_realized_cents']:+.0f}\u00a2, {sr['open']} open)"
                )
        except Exception:
            pass

        body.append("")
        body.append(DISCLAIMER)
        return header + "\n<pre>\n" + "\n".join(body) + "\n</pre>"
