"""Interval-timing research — read-only economic analysis.

Pure functions over resolved capture rows (dicts from
``IntervalResearchLedger.resolved_rows``). They never touch the live system; they
produce the comparisons the research is meant to answer:

  * per-interval EXECUTABLE-price economics (accuracy, ask, net edge, entry rate,
    EV, ROI, drawdown) — keeping PREDICTION quality and TRADE value separate;
  * cohort split (full-history / partial / late-only) so a headline rate is never
    shown without its coverage + price context;
  * matched-cohort comparison (only contracts present at ALL compared intervals);
  * defensive-exit grading (was an exit warning early enough to preserve value?).

Everything returns INSUFFICIENT-style empty results until prospective data exists
— the module is honest about n=0 rather than inventing numbers.
"""
from __future__ import annotations

from statistics import mean
from typing import Any, Mapping, Sequence

from .config import INTERVAL_MARKS

_ORDER = list(INTERVAL_MARKS)  # 15M..7M, earliest -> latest


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _max_drawdown(pnls: Sequence[float]) -> float:
    """Largest peak-to-trough drop of the cumulative P&L curve (cents)."""
    cum = 0.0
    peak = 0.0
    dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return dd


def classify(accuracy: float | None, avg_ask: float | None) -> dict[str, str]:
    """Separate PREDICTION quality from TRADE value (never one combined score).

    Trade value keys off the crude expected gross edge = fair value (accuracy*100)
    minus the executable ask, so '97% accurate but 97c ask' lands as
    HIGH prediction / LOW trade value."""
    if accuracy is None:
        pred = "UNKNOWN"
    elif accuracy >= 0.70:
        pred = "HIGH"
    elif accuracy >= 0.60:
        pred = "MED"
    else:
        pred = "LOW"
    if accuracy is None or avg_ask is None:
        trade = "UNKNOWN"
    else:
        room = accuracy * 100.0 - avg_ask  # expected gross edge, cents
        trade = "HIGH" if room >= 8.0 else "MED" if room >= 3.0 else "LOW"
    return {"prediction_quality": pred, "trade_value": trade}


def _interval_stats(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    sided = [r for r in rows if r.get("predicted_side")]
    n = len(sided)
    if n == 0:
        return {"n": 0}
    acc = sum(1 for r in sided if r.get("correct") == 1) / n
    asks = [v for v in (_f(r.get("entry_ask_cents")) for r in sided) if v is not None]
    pnls = [v for v in (_f(r.get("realized_pnl_cents")) for r in sided) if v is not None]
    cons = [v for v in (_f(r.get("conservative_probability")) for r in sided) if v is not None]
    edges = [v for v in (_f(r.get("net_edge_cents")) for r in sided) if v is not None]
    rec = [r for r in sided if r.get("entry_recommended") == 1]
    rec_pnls = [v for v in (_f(r.get("realized_pnl_cents")) for r in rec) if v is not None]
    avg_ask = mean(asks) if asks else None
    blind_ev = mean(pnls) if pnls else None
    roi = (blind_ev / avg_ask * 100.0) if (blind_ev is not None and avg_ask) else None
    return {
        "n": n,
        "accuracy": round(acc, 4),
        "avg_conservative_prob": round(mean(cons), 4) if cons else None,
        "avg_executable_ask_cents": round(avg_ask, 2) if avg_ask is not None else None,
        "avg_net_edge_cents": round(mean(edges), 2) if edges else None,
        "entry_rate": round(len(rec) / n, 4),
        "recommended_entries": len(rec),
        "blind_ev_cents": round(blind_ev, 3) if blind_ev is not None else None,
        "recommended_ev_cents": round(mean(rec_pnls), 3) if rec_pnls else None,
        "roi_pct": round(roi, 2) if roi is not None else None,
        "max_drawdown_cents": round(_max_drawdown(pnls), 2) if pnls else None,
        "classification": classify(acc, avg_ask),
    }


def per_interval_economics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Executable-price economics for each interval present in ``rows``."""
    return {iv: _interval_stats([r for r in rows if r.get("interval") == iv]) for iv in _ORDER}


def _coverage(rows: Sequence[Mapping[str, Any]]) -> dict[str, set]:
    cov: dict[str, set] = {}
    for r in rows:
        tk = r.get("ticker")
        if tk and r.get("predicted_side"):
            cov.setdefault(str(tk), set()).add(r.get("interval"))
    return cov


def cohort_split(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Split contracts by interval coverage and report 7M economics per cohort, so
    a headline 7M rate is always shown with its coverage + price context.

      full       — captured at all eight marks
      partial    — captured at some (but not all) earlier marks
      late_only  — first/only capture was the closing 7M mark
    """
    cov = _coverage(rows)
    full_n = len(_ORDER)
    cohorts = {"full": set(), "partial": set(), "late_only": set()}
    for tk, ivs in cov.items():
        if ivs == set(_ORDER):
            cohorts["full"].add(tk)
        elif ivs == {"7M"}:
            cohorts["late_only"].add(tk)
        else:
            cohorts["partial"].add(tk)
    out: dict[str, Any] = {}
    for name, tickers in cohorts.items():
        seven = [r for r in rows if r.get("interval") == "7M" and str(r.get("ticker")) in tickers]
        out[name] = {"contracts": len(tickers), "seven_m": _interval_stats(seven)}
    return out


def matched_cohort_comparison(rows: Sequence[Mapping[str, Any]],
                              intervals: Sequence[str]) -> dict[str, Any]:
    """Compare intervals on ONLY the contracts captured at ALL of them — so 7M is
    never compared to 10M on unmatched populations."""
    intervals = [iv for iv in intervals if iv in INTERVAL_MARKS]
    cov = _coverage(rows)
    matched = {tk for tk, ivs in cov.items() if set(intervals).issubset(ivs)}
    by_interval = {
        iv: _interval_stats([r for r in rows if r.get("interval") == iv
                             and str(r.get("ticker")) in matched])
        for iv in intervals
    }
    return {"matched_contracts": len(matched), "intervals": intervals, "by_interval": by_interval}


def defensive_exit_grade(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Grade defensive-exit usefulness on contracts whose side CHANGED across the
    captured marks.

    For each such contract: the EARLIEST mark where the side flipped away from the
    15M/earliest call, the executable price still available there, the lead time
    over the 7M mark, and whether the flip was SUSTAINED to settlement (a true
    warning) or temporary noise (false). A correct-but-late warning (price already
    collapsed) is counted but flagged as economically late.
    """
    order_idx = {iv: i for i, iv in enumerate(_ORDER)}
    by_ticker: dict[str, list[Mapping[str, Any]]] = {}
    for r in rows:
        tk = r.get("ticker")
        if tk and r.get("predicted_side"):
            by_ticker.setdefault(str(tk), []).append(r)

    true_warn = false_warn = late_warn = 0
    leads: list[float] = []
    preserved: list[float] = []
    for caps in by_ticker.values():
        caps = sorted(caps, key=lambda r: order_idx.get(r.get("interval"), 99))
        if len(caps) < 2:
            continue
        first_side = str(caps[0].get("predicted_side") or "").upper()
        result = str(caps[0].get("official_result") or "").upper()
        flip = next((c for c in caps[1:] if str(c.get("predicted_side") or "").upper() != first_side), None)
        if flip is None:
            continue
        flipped_side = str(flip.get("predicted_side") or "").upper()
        sustained = result in {"YES", "NO"} and flipped_side == result
        # lead time over the 7M mark (minutes), using mark_seconds.
        warn_sec = _f(flip.get("mark_seconds")) or _f(flip.get("seconds_remaining"))
        lead_min = ((warn_sec - INTERVAL_MARKS["7M"]) / 60.0) if warn_sec is not None else None
        # value still recoverable for the ORIGINAL side at the warning mark
        ask = _f(flip.get("entry_ask_cents"))
        orig_value = (100.0 - ask) if ask is not None else None  # what holding the original side is worth-ish
        if sustained:
            true_warn += 1
            if lead_min is not None:
                leads.append(lead_min)
            if orig_value is not None:
                preserved.append(orig_value)
                if orig_value <= 5.0:  # already collapsed -> correct but economically late
                    late_warn += 1
        else:
            false_warn += 1
    total = true_warn + false_warn
    return {
        "contracts_with_flip": total,
        "true_warnings": true_warn,
        "false_warnings": false_warn,
        "economically_late": late_warn,
        "precision": round(true_warn / total, 4) if total else None,
        "avg_lead_minutes": round(mean(leads), 2) if leads else None,
        "avg_value_recoverable_cents": round(mean(preserved), 2) if preserved else None,
    }


def full_report(ledger: Any) -> dict[str, Any]:
    """Top-level research report (read-only). Returns INSUFFICIENT-style structures
    until prospective captures resolve."""
    rows = ledger.resolved_rows() if ledger is not None else []
    return {
        "available": bool(rows),
        "resolved_rows": len(rows),
        "counts": ledger.counts() if ledger is not None else {"available": False},
        "per_interval": per_interval_economics(rows),
        "cohorts": cohort_split(rows),
        "matched_10m_vs_7m": matched_cohort_comparison(rows, ["10M", "7M"]),
        "matched_all": matched_cohort_comparison(rows, _ORDER),
        "defensive_exit": defensive_exit_grade(rows),
    }
