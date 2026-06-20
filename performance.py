"""Performance tracking + settlement reconciliation.

Reconciles persisted directional signals against the official Kalshi market
result (read-only REST) and reports honest stats: win rate, realized return
after fees, breakdowns by asset / time-remaining / signal type, the worst
losing streak, and a calibration table (estimated win probability vs actual
win rate). Losing and unsettled signals are never hidden.

The corpus is side-aware: a NO position wins when the market resolves NO, so
its win probability is (1 - P_yes). It also learns 24/7 — every directional
signal is settled as a hypothetical ("paper") trade, even when no alert was
actually sent, so the bot keeps learning while you are not trading.
"""
import logging

logger = logging.getLogger(__name__)

SETTLED_STATUSES = ("settled", "finalized", "determined", "closed")


def _f(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _time_bucket(secs):
    if secs is None:
        return "unknown"
    if secs >= 720:
        return "12-15m"
    if secs >= 360:
        return "6-12m"
    if secs >= 120:
        return "2-6m"
    return "<2m"


def win_probability(row):
    """Side-aware win probability for a signal row.

    Prefer the stored win_prob (computed at signal time); fall back to the
    fair YES probability flipped for NO positions.
    """
    wp = _f(row.get("win_prob"))
    if wp is not None:
        return wp
    fp = _f(row.get("fair_prob"))
    if fp is None:
        return None
    return fp if row.get("side") == "YES" else (1.0 - fp)


class PerformanceTracker:
    def __init__(self, store, client, interval=30.0):
        self.store = store
        self.client = client
        self.interval = interval
        self._last_reconcile = 0.0

    # -- settlement -----------------------------------------------------
    def reconcile(self, now, max_calls=12):
        """Settle pending directional signals whose markets have resolved.

        One REST call per distinct ticker settles every pending row for that
        market (sent entries AND un-sent paper signals), so the learning corpus
        grows continuously without burning rate limit.
        """
        if not self.store.enabled:
            return
        if now - self._last_reconcile < self.interval:
            return
        self._last_reconcile = now
        pending = self.store.pending_settlements()
        if not pending:
            return

        by_ticker = {}
        for row in pending:
            t = row.get("ticker")
            if t:
                by_ticker.setdefault(t, []).append(row)

        calls = 0
        for ticker, rows in by_ticker.items():
            if calls >= max_calls:
                break
            try:
                m = self.client.get_market(ticker)
            except Exception as e:
                logger.warning(f"reconcile get_market {ticker}: {e}")
                continue
            calls += 1
            if not m:
                continue
            status = (m.get("status") or "").lower()
            result = (m.get("result") or "").lower()
            if status not in SETTLED_STATUSES and result not in ("yes", "no"):
                continue  # not resolved yet
            settled_n = 0
            for row in rows:
                side = row.get("side")
                ask = row.get("entry_ask") or 0
                fee = row.get("fee") or 0
                if result in ("yes", "no"):
                    win = (side == "YES" and result == "yes") or \
                          (side == "NO" and result == "no")
                    outcome = "win" if win else "loss"
                    realized = (100 - ask - fee) if win else -(ask + fee)
                else:
                    outcome = "void"
                    realized = 0
                self.store.update_outcome(row["id"], outcome, realized)
                settled_n += 1
            logger.info(f"settled {ticker} -> {result or 'void'} ({settled_n} rows)")

    # -- stats ----------------------------------------------------------
    def stats(self):
        if not self.store.enabled:
            return {"available": False, "reason": "persistence_disabled"}

        # Learning corpus: one settled hypothetical entry per (ticker, side).
        settled = self.store.representative_settled()
        pending_rows = self.store.query(
            "SELECT COUNT(*) AS n FROM signals WHERE outcome = 'pending' "
            "AND side IN ('YES','NO') "
            "AND state IN ('ENTRY CONFIRMED','ENTRY CANDIDATE','WATCH')"
        )
        pending = int(pending_rows[0]["n"]) if pending_rows else 0

        wins = [r for r in settled if r["outcome"] == "win"]
        losses = [r for r in settled if r["outcome"] == "loss"]
        n = len(settled)
        win_rate = (len(wins) / n) if n else None

        entries = [_f(r["entry_ask"]) for r in settled if r["entry_ask"] is not None]
        edges = [_f(r["edge"]) for r in settled if r["edge"] is not None]
        rets = [_f(r["realized_return"]) for r in settled if r["realized_return"] is not None]

        def _avg(xs):
            xs = [x for x in xs if x is not None]
            return round(sum(xs) / len(xs), 2) if xs else None

        def _record(subset):
            sw = sum(1 for r in subset if r["outcome"] == "win")
            sn = len(subset)
            return {
                "n": sn,
                "wins": sw,
                "losses": sn - sw,
                "win_rate": round(sw / sn, 3) if sn else None,
            }

        real = _record([r for r in settled if r.get("sent")])
        paper = _record([r for r in settled if not r.get("sent")])

        # breakdowns
        def _group(keyfn):
            g = {}
            for r in settled:
                k = keyfn(r)
                b = g.setdefault(k, {"n": 0, "wins": 0, "realized": 0.0})
                b["n"] += 1
                if r["outcome"] == "win":
                    b["wins"] += 1
                b["realized"] += _f(r["realized_return"]) or 0.0
            for k, b in g.items():
                b["win_rate"] = round(b["wins"] / b["n"], 3) if b["n"] else None
                b["realized"] = round(b["realized"], 2)
            return g

        by_asset = _group(lambda r: r["asset"])
        by_time = _group(lambda r: _time_bucket(r["seconds_remaining"]))
        by_type = _group(lambda r: r["signal_type"] or "unknown")

        # max losing streak (chronological)
        streak = cur = 0
        for r in settled:
            if r["outcome"] == "loss":
                cur += 1
                streak = max(streak, cur)
            else:
                cur = 0

        # calibration: SIDE-AWARE win prob bucketed into deciles vs actual
        bins = {}
        for r in settled:
            p = win_probability(r)
            if p is None:
                continue
            b = min(9, max(0, int(p * 10)))
            slot = bins.setdefault(b, {"n": 0, "wins": 0, "psum": 0.0})
            slot["n"] += 1
            slot["psum"] += p
            if r["outcome"] == "win":
                slot["wins"] += 1
        calib = []
        for b in sorted(bins):
            slot = bins[b]
            calib.append({
                "bucket": f"{b * 10}-{b * 10 + 10}%",
                "n": slot["n"],
                "avg_estimated_prob": round(slot["psum"] / slot["n"], 3),
                "actual_win_rate": round(slot["wins"] / slot["n"], 3),
            })

        return {
            "available": True,
            "total_alerts": self.store.count_alerts(),
            "settled_entries": n,
            "pending_entries": pending,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 3) if win_rate is not None else None,
            "real_record": real,
            "paper_record": paper,
            "avg_entry_price": _avg(entries),
            "avg_edge": _avg(edges),
            "avg_realized_return": _avg(rets),
            "total_realized_return": round(sum(r for r in rets), 2) if rets else 0,
            "max_losing_streak": streak,
            "by_asset": by_asset,
            "by_time_remaining": by_time,
            "by_signal_type": by_type,
            "calibration": calib,
        }
