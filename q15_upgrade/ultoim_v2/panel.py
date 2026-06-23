"""Ultoim V2 — pure Telegram formatters (paper entry card + research recap).

The entry card deliberately mirrors the live champion's "BEST ENTRY" card grammar
(bold header OUTSIDE a <pre> block, body inside one <pre> panel) so it reads the
same way — but it is unmistakably a PAPER / research signal and it MUST NOT carry
any of the live formatter / suppression markers ("V9.5 CHECK", "ENTRY
RECOMMENDED", "Hourly Report —", "TOP 3 PICKS"). A test asserts their absence.
"""
from __future__ import annotations

import html
from typing import Any, Mapping, Sequence


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct1(prob: Any) -> str:
    p = _num(prob)
    return "—" if p is None else f"{p * 100:.1f}%"


def _signed_cents(value: Any) -> str:
    v = _num(value)
    return "—" if v is None else f"{v:+.1f}¢"


def _esc(text: Any) -> str:
    return html.escape(str(text if text is not None else ""))


def build_entry_alert(pick: Mapping[str, Any], scoreboard_summary: Mapping[str, Any],
                      cfg: Any) -> str:
    """Render one paper BEST ENTRY card. ``pick`` is the fired decision row;
    ``scoreboard_summary`` supplies the settled-N caveat."""
    asset = _esc(pick.get("asset"))
    side = _esc(str(pick.get("predicted_side") or "").upper())
    ticker = _esc(pick.get("ticker"))
    interval = _esc(pick.get("interval"))
    window = _esc(pick.get("window_key"))
    sel = pick.get("selected_probability")
    ask = _num(pick.get("entry_ask_cents"))
    display = pick.get("best_entry_cents")
    net_edge = pick.get("net_edge_cents")
    resolved = int((scoreboard_summary or {}).get("resolved") or 0)

    ask_txt = "—" if ask is None else f"{ask:.0f}¢"
    display_txt = "—" if display is None else f"{int(display)}¢"

    header = f"🧪 <b>ULTOIM V2 · {interval} · PAPER ENTRY</b>"
    body = [
        "RESEARCH SIGNAL — paper only, NOT a live call. No orders placed.",
        "",
        f"🏆 BEST ENTRY — {asset} {side}",
        f"Ticker: {ticker}",
        f"Interval: {interval} · Window: {window}",
        f"Confidence: {_pct1(sel)}",
        f"Best entry: {display_txt} or lower",
        f"Current ask: {ask_txt}",
        f"Net edge: {_signed_cents(net_edge)}",
        f"Unvalidated · N={resolved} settled · 1 regime · 0 YES-prone · CI wide",
        "Ultoim V2 · research/paper · not advice · no orders placed",
    ]
    return header + "\n<pre>" + "\n".join(body) + "</pre>"


def _accuracy_line(agg: Mapping[str, Any], min_n: int) -> str:
    n = int(agg.get("n") or 0)
    right = int(agg.get("right") or 0)
    wrong = int(agg.get("wrong") or 0)
    if n < min_n:
        return f"W-L {right}-{wrong} · INSUFFICIENT DATA (N<{min_n})"
    acc = agg.get("accuracy")
    lo = agg.get("ci_low")
    hi = agg.get("ci_high")
    acc_txt = "—" if acc is None else f"{acc * 100:.1f}%"
    ci_txt = "" if lo is None or hi is None else f" [{lo * 100:.0f}–{hi * 100:.0f}%]"
    return f"W-L {right}-{wrong} · acc {acc_txt}{ci_txt}"


def build_recap(scoreboard: Mapping[str, Any], recent_picks: Sequence[Mapping[str, Any]],
                loss_rows: Sequence[Mapping[str, Any]], cfg: Any) -> str:
    """Render the periodic research recap. NOT an "Hourly Report —" (that marker
    reroutes the live reformatters); this is its own monospace research card."""
    min_n = int((scoreboard or {}).get("min_n") or getattr(cfg, "min_scoreboard_n", 30))
    overall = (scoreboard or {}).get("overall") or {}
    resolved = int((scoreboard or {}).get("resolved") or 0)
    total = int((scoreboard or {}).get("total_recorded") or 0)
    delivery = (scoreboard or {}).get("delivery_counts") or {}
    pending = int(delivery.get("PENDING") or 0) + int(delivery.get("RECORDED") or 0)

    pnl_total = overall.get("pnl_total_cents")
    pnl_avg = overall.get("pnl_avg_cents")
    roi_txt = "—" if pnl_avg is None else f"{pnl_avg:+.2f}¢/entry"
    base = overall.get("base_rate")
    edge = overall.get("edge_over_base")
    base_txt = "—" if base is None else f"{base * 100:.1f}%"
    edge_txt = "—" if edge is None else f"{edge * 100:+.1f}pp"

    header = "🧪 <b>ULTOIM V2 — RESEARCH RECAP</b>"
    body: list[str] = [
        "Paper-only research overlay · no orders placed.",
        "",
        f"Settled: {resolved} · recorded: {total} · pending: {pending}",
        _accuracy_line(overall, min_n),
        f"ROI: {roi_txt} · total P&L {_signed_cents(pnl_total)}",
        f"Base rate: {base_txt} ({_esc(overall.get('base_rate_side') or '—')}) · "
        f"edge over base: {edge_txt}",
        "",
        "By interval:",
    ]
    by_interval = (scoreboard or {}).get("by_interval") or {}
    for iv in ("15M", "10M", "7M"):
        agg = by_interval.get(iv) or {}
        body.append(f"  {iv:>3}: {_accuracy_line(agg, min_n)}")

    body.append("")
    body.append("Recent picks:")
    if recent_picks:
        for p in list(recent_picks)[:5]:
            fired = "FIRED" if int(p.get("fired") or 0) == 1 else "abstain"
            body.append(
                f"  {_esc(p.get('asset')):<5} {_esc(str(p.get('predicted_side') or '').upper()):<3} "
                f"{_esc(p.get('interval')):>3} ask {_esc(_fmt_ask(p.get('entry_ask_cents')))} · {fired}"
            )
    else:
        body.append("  (none yet)")

    body.append("")
    body.append("Recent losses (for review):")
    if loss_rows:
        for r in list(loss_rows)[:5]:
            body.append(
                f"  {_esc(r.get('asset')):<5} {_esc(str(r.get('predicted_side') or '').upper()):<3} "
                f"{_esc(r.get('interval')):>3} ask {_esc(_fmt_ask(r.get('entry_ask_cents')))}"
            )
    else:
        body.append("  (none)")

    body.append("")
    body.append("Ultoim V2 · research/paper · not advice · no orders placed")
    return header + "\n<pre>" + "\n".join(body) + "</pre>"


def _fmt_ask(value: Any) -> str:
    v = _num(value)
    return "—" if v is None else f"{v:.0f}¢"
