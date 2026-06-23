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

from . import validate


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
    summary = scoreboard_summary or {}
    resolved = int(summary.get("resolved") or 0)
    # DERIVED, never hardcoded: the caveat must stop claiming "0 YES-prone" the
    # moment YES-prone data exists (the old literal would silently lie).
    n_regimes = int(summary.get("n_regimes") or 0)
    yes_prone_n = int(summary.get("yes_prone_n") or 0)
    regimes_txt = f"{n_regimes} regime{'' if n_regimes == 1 else 's'}" if n_regimes else "0 regimes"

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
        f"Unvalidated · N={resolved} settled · {regimes_txt} · "
        f"{yes_prone_n} YES-prone · CI wide",
        "Ultoim V2 · research/paper · not advice · no orders placed",
    ]
    return header + "\n<pre>" + "\n".join(body) + "</pre>"


def build_exit_warning(warning: Mapping[str, Any], scoreboard: Mapping[str, Any],
                       cfg: Any) -> str:
    """Render a defensive-exit / flip warning: the model has reversed on a pick it
    suggested earlier, with the evidence behind the reversal and the live historical
    reliability of such warnings (how it learns). Marker-safe: contains none of the
    live suppression/routing strings."""
    asset = _esc(warning.get("asset"))
    ticker = _esc(warning.get("ticker"))
    entry_side = _esc(str(warning.get("entry_side") or "").upper())
    flipped_to = _esc(str(warning.get("flipped_to_side") or "").upper())
    entry_ask = _num(warning.get("entry_ask_cents"))
    entry_interval = _esc(warning.get("entry_interval"))
    secs = _num(warning.get("warn_seconds_remaining"))
    flip_yes = warning.get("flip_calibrated_yes")
    flip_sel = warning.get("flip_selected_probability")
    exit_val = _num(warning.get("exit_value_cents"))
    cycles = int(warning.get("confirm_cycles") or 0)
    span = _num(warning.get("confirm_span_seconds"))
    sb = scoreboard or {}

    mins_txt = "—" if secs is None else f"{secs / 60:.1f}m left"
    entry_ask_txt = "—" if entry_ask is None else f"{entry_ask:.0f}¢"
    exit_txt = "—" if exit_val is None else f"{exit_val:.0f}¢"
    span_txt = "—" if span is None else f"{span:.0f}s"
    # flip_probability is stored on a 0..100 scale; show as a fraction.
    fp = _num(warning.get("flip_probability"))
    flip_txt = "—" if fp is None else f"{fp / 100.0 * 100:.0f}%"

    header = "⚠️ <b>ULTOIM V2 · EXIT WARNING</b>"
    body = [
        "RESEARCH SIGNAL — paper only. The model REVERSED on a pick it suggested.",
        "",
        f"📉 RECONSIDER — {asset} {entry_side}",
        f"Ticker: {ticker}",
        f"Suggested earlier: {entry_side} @ {entry_ask_txt} ({entry_interval})",
        f"Now ({mins_txt}): model flipped to {flipped_to} · P(YES) {_pct1(flip_yes)}"
        f" · conviction {_pct1(flip_sel)}",
        f"Why (not a spike): held {cycles}× over {span_txt} · flip-risk {flip_txt}",
        f"Sell-to-close ≈ {exit_txt} (recover this vs 0 if it settles against you)",
        _exit_reliability_line(sb, cfg),
        "Ultoim V2 · research/paper · not advice · no orders placed",
    ]
    return header + "\n<pre>" + "\n".join(body) + "</pre>"


def _exit_reliability_line(sb: Mapping[str, Any], cfg: Any) -> str:
    """The learned reliability of these warnings — how it learns from its mistakes."""
    n = int((sb or {}).get("resolved") or 0)
    correct = int((sb or {}).get("correct") or 0)
    min_n = int(getattr(cfg, "min_scoreboard_n", 30) or 30)
    if n < min_n:
        return f"Track record: building, {correct}/{n} warnings correct so far"
    prec = (sb or {}).get("precision")
    avg = (sb or {}).get("avg_recovered_per_correct")
    prec_txt = "—" if prec is None else f"{prec * 100:.0f}%"
    avg_txt = "" if avg is None else f" · avg recover {avg:.0f}¢"
    return f"Track record: {prec_txt} of these flips lost ({correct}/{n}){avg_txt}"


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


def _promotion_line(scoreboard: Mapping[str, Any], min_promote_n: int) -> str:
    """One honest, significance-gated verdict line. Reads INSUFFICIENT below the
    promotion-N, NOT SEPARABLE when the Wilson lower bound doesn't clear the base
    rate, and BEATS only when it does (one-sided exact binomial p<0.05). A BEATS
    overall is qualified whenever the YES side or a YES-prone regime is still
    unproven, so the recap can never over-claim full promotion-readiness. Contains
    none of the live suppression/routing markers."""
    overall = (scoreboard or {}).get("overall") or {}
    verdict = validate.verdict_from_agg(overall, min_promote_n=min_promote_n)
    state = verdict["state"]
    if state == validate.BEATS:
        edge = overall.get("edge_over_base")
        edge_txt = "" if edge is None else f", +{edge * 100:.1f}pp"
        line = f"PROMOTION: BEATS BASE RATE (sig-tested{edge_txt}, N={verdict['n']})"
        by_side = (scoreboard or {}).get("by_side") or {}
        yes = by_side.get("YES") or {}
        by_regime = (scoreboard or {}).get("by_regime_directional") or {}
        yes_prone = by_regime.get("YES_PRONE") or {}
        if (int(yes.get("n") or 0) < min_promote_n
                or int(yes_prone.get("n") or 0) < min_promote_n):
            line += " · overall only — YES-side / YES-prone unproven"
        return line
    if state == validate.NOT_SEPARABLE:
        return "PROMOTION: NOT SEPARABLE FROM BASE RATE"
    return f"PROMOTION: INSUFFICIENT DATA (N<{min_promote_n})"


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

    min_promote_n = int(getattr(cfg, "min_promote_n", 0) or max(min_n, 50))

    header = "🧪 <b>ULTOIM V2 — RESEARCH RECAP</b>"
    body: list[str] = [
        "Paper-only research overlay · no orders placed.",
        "",
        f"Settled: {resolved} · recorded: {total} · pending: {pending}",
        _accuracy_line(overall, min_n),
        _promotion_line(scoreboard or {}, min_promote_n),
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

    # By side — NO is the delivered population; YES is research-only (never sent).
    by_side = (scoreboard or {}).get("by_side") or {}
    body.append("")
    body.append("By side:")
    body.append(f"  NO : {_accuracy_line(by_side.get('NO') or {}, min_n)}")
    body.append(f"  YES: {_accuracy_line(by_side.get('YES') or {}, min_n)} (research-only)")

    # By regime — the decision-time market-lean split (BALANCED shown as 'mixed').
    by_regime = (scoreboard or {}).get("by_regime_directional") or {}
    body.append("")
    body.append("By regime:")
    body.append(f"  NO-prone : {_accuracy_line(by_regime.get('NO_PRONE') or {}, min_n)}")
    body.append(f"  YES-prone: {_accuracy_line(by_regime.get('YES_PRONE') or {}, min_n)}")
    body.append(f"  mixed    : {_accuracy_line(by_regime.get('BALANCED') or {}, min_n)}")

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

    # Exit warnings — the defensive-flip learning record.
    ew = (scoreboard or {}).get("exit_warnings") or {}
    if int(ew.get("total") or 0) > 0:
        n = int(ew.get("resolved") or 0)
        correct = int(ew.get("correct") or 0)
        fa = int(ew.get("false_alarms") or 0)
        body.append("")
        body.append("Exit warnings (defensive flips):")
        if n < min_n:
            body.append(f"  {correct}/{n} correct · {fa} false alarms · building (N<{min_n})")
        else:
            prec = ew.get("precision")
            prec_txt = "—" if prec is None else f"{prec * 100:.0f}%"
            body.append(f"  precision {prec_txt} ({correct}/{n}) · {fa} false alarms")
        body.append(f"  recovered {_signed_cents(ew.get('recovered_cents'))} · "
                    f"forfeited {_signed_cents(ew.get('forfeited_cents'))} · "
                    f"net {_signed_cents(ew.get('net_cents'))}")

    # Blowup SHADOW screen — record-only diagnostics; emphatically NOT a live gate.
    shadow = (scoreboard or {}).get("blowup_shadow") or {}
    if shadow.get("available"):
        body.append("")
        body.append("Blowup screen (SHADOW · record-only · no effect on entries):")
        n_scored = int(shadow.get("n_scored") or 0)
        auc_v = shadow.get("blowup_auc")
        dist_auc = shadow.get("distance_auc")
        auc_txt = "—" if auc_v is None else f"{auc_v:.2f}"
        dist_txt = "—" if dist_auc is None else f"{dist_auc:.2f}"
        body.append(f"  N={n_scored} scored · blowup AUC {auc_txt} · "
                    f"distance AUC {dist_txt} · UNPROVEN")

    # 15M selective screen (s15) — record-only would-fire measurement, surfaced so the
    # edge accrues visibly. SHADOW: never affects which entries fire.
    s15 = (scoreboard or {}).get("s15_research") or {}
    if s15.get("available") and int((s15.get("book") or {}).get("n") or 0) > 0:
        body.append("")
        body.append("15M screen — s15 (SHADOW · record-only · no effect on entries):")
        body.append(f"  15M-NO book : {_screen_line(s15.get('book'))}")
        body.append(f"  would-fire  : {_screen_line(s15.get('gated'))}")
        body.append(f"   + cal-drift: {_screen_line(s15.get('gated_with_cal_drift'))}")

    # Distance-to-strike — the one record-only feature shown to transport. Measures the
    # candidate abstention: near-strike "pin" (would-abstain) vs far (would-keep).
    dist = (scoreboard or {}).get("distance_research") or {}
    if dist.get("available") and int((dist.get("book") or {}).get("n") or 0) > 0:
        pin = _num(dist.get("pin_sigma"))
        pin_txt = "—" if pin is None else f"{pin:.2f}"
        body.append("")
        body.append(f"Distance-to-strike (SHADOW · record-only · pin |sigma|<{pin_txt}):")
        body.append(f"  far  (keep): {_screen_line(dist.get('far'))}")
        body.append(f"  near (pin) : {_screen_line(dist.get('near_pin'))}")

    body.append("")
    body.append("Ultoim V2 · research/paper · not advice · no orders placed")
    return header + "\n<pre>" + "\n".join(body) + "</pre>"


def _screen_line(agg: Mapping[str, Any] | None) -> str:
    """One compact line for a record-only research-screen bucket: n, accuracy + Wilson
    CI, and per-pick P&L. Pure formatting; ``None``/empty -> 'n=0'."""
    a = agg or {}
    n = int(a.get("n") or 0)
    if n == 0:
        return "n=0"
    acc = a.get("accuracy")
    lo = a.get("ci_low")
    hi = a.get("ci_high")
    acc_txt = "—" if acc is None else f"{acc * 100:.0f}%"
    ci_txt = "" if lo is None or hi is None else f" [{lo * 100:.0f}–{hi * 100:.0f}]"
    avg = a.get("pnl_avg_cents")
    avg_txt = "—" if avg is None else f"{avg:+.1f}¢/pick"
    return f"n={n} · acc {acc_txt}{ci_txt} · {avg_txt}"


def _fmt_ask(value: Any) -> str:
    v = _num(value)
    return "—" if v is None else f"{v:.0f}¢"
