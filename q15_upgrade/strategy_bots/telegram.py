"""Telegram notifications for Filtered Alert System v3."""
from __future__ import annotations

import html
import json
import logging
import math
import os
from typing import Any, Mapping, Sequence

from notifications.telegram_client import TelegramSendClient

logger = logging.getLogger("strategy_bots.telegram")


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _fmt(value: Any, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    try:
        v = float(value)
        if abs(v) >= 100:
            text = f"{v:.0f}"
        elif abs(v) >= 10:
            text = f"{v:.1f}"
        else:
            text = f"{v:.3f}".rstrip("0").rstrip(".")
        return html.escape(text + suffix)
    except (TypeError, ValueError):
        return html.escape(str(value))


def _bot_label(name: str) -> str:
    return {
        "v3_confidence_tier": "V3 Confidence Tier",
        "bnb_no_confirmation": "BNB NO Confirmation",
        "bnb_yes_reversal": "BNB YES Reversal",
        "hype_yes_confirmation": "HYPE YES Confirmation",
        "morefire_btc_confirmed": "MoreFire BTC-Confirmed",
        "hvf_depth_flow_wrapper": "HVF Depth/Flow Wrapper",
        "v3_15m_depth_formula_research": "15M Depth Formula Research",
        "thirteen_m_sniper": "13M Early Sniper",
        "warn_flip_entry": "Warn-Flip Entry",
        "fav_10m": "Favorite 10M",
        "top_pick_13m": "Top Pick 13M",
        "drift_13m": "Drift Pick 13M",
        "drift_flow_spread_13m": "Drift Flow/Spread 13M",
        "drift_addon_requal": "Drift Requalification Add-On",
        "drift_latequal_12m_11m": "Drift Late Qualifier",
        "drift_no_mirror": "Drift NO Mirror",
        "baseline_control": "Baseline Control",
    }.get(name, name)


def _metric_parts(row: Mapping[str, Any], specs: list[tuple[str, str, str]]) -> str:
    parts: list[str] = []
    for label, key, suffix in specs:
        if row.get(key) is not None:
            parts.append(f"{label} {_fmt(row.get(key), suffix)}")
    return ", ".join(parts)


def _is_bnb_combined(row: Mapping[str, Any]) -> bool:
    return str(row.get("bot_name") or "") in {"bnb_no_confirmation", "bnb_yes_reversal"}


def _is_confidence_tier(row: Mapping[str, Any]) -> bool:
    return str(row.get("bot_name") or "") == "v3_confidence_tier"


def _is_hvf_wrapper(row: Mapping[str, Any]) -> bool:
    return str(row.get("bot_name") or "") == "hvf_depth_flow_wrapper"


def _is_depth_formula(row: Mapping[str, Any]) -> bool:
    return str(row.get("bot_name") or "") == "v3_15m_depth_formula_research"


def _is_thirteen_m_sniper(row: Mapping[str, Any]) -> bool:
    return str(row.get("bot_name") or "") == "thirteen_m_sniper"


def _thresholds(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("threshold_json")
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _pct(value: Any) -> str:
    try:
        return _fmt(float(value) * 100.0, "%")
    except (TypeError, ValueError):
        return "n/a"


def _ratio_text(numerator: Any, denominator: Any) -> str | None:
    try:
        n = float(numerator)
        d = float(denominator)
    except (TypeError, ValueError):
        return None
    if d <= 0:
        return None
    return _fmt(n / d)


def _degraded_line(row: Mapping[str, Any]) -> str | None:
    if not row.get("feed_degraded"):
        return None
    feeds = str(row.get("degraded_feeds") or "feed").replace(",", ", ")
    return f"<b>DEGRADED</b>: stale feed freshness ({html.escape(feeds)})"


def _bnb_action(row: Mapping[str, Any]) -> str:
    bot = str(row.get("bot_name") or "")
    status = str(row.get("decision_status") or "").upper()
    reasons = str(row.get("reason_codes") or "")
    if bot == "bnb_yes_reversal":
        return "RESEARCH YES REVERSAL"
    if bot == "bnb_no_confirmation" and "BNB_NO_VETO_" in reasons:
        return "VETO BNB NO"
    if bot == "bnb_no_confirmation" and status == "REJECTED":
        return "REJECT BNB NO"
    if bot == "bnb_no_confirmation" and (status == "ACCEPTED" or not status):
        return "TAKE BNB NO"
    return status or "TRACK BNB"


def build_bnb_combined_alert(row: Mapping[str, Any]) -> str:
    reasons = str(row.get("reason_codes") or "").replace(",", ", ")
    parts = [
        "<b>V3 BNB COMBINED DECISION</b>",
        f"Action: {html.escape(_bnb_action(row))}",
        (
            f"{html.escape(str(row.get('asset') or 'BNB'))} "
            f"{html.escape(str(row.get('side') or ''))} "
            f"{html.escape(str(row.get('interval') or ''))}"
        ),
        f"Bot: {html.escape(_bot_label(str(row.get('bot_name') or '')))}",
        f"Rule: {html.escape(str(row.get('source_rule') or 'UNKNOWN'))}",
        f"Ticker: <code>{html.escape(str(row.get('ticker') or ''))}</code>",
    ]
    degraded = _degraded_line(row)
    if degraded:
        parts.insert(1, degraded)
    if row.get("entry_ask_cents") is not None or row.get("spread_cents") is not None:
        entry = _metric_parts(row, [
            ("ask", "entry_ask_cents", "c"),
            ("spread", "spread_cents", "c"),
        ])
        if entry:
            parts.append(f"Entry: {entry}")
    kalshi = _metric_parts(row, [
        ("depth", "depth_contracts", ""),
        ("YES ask depth", "yes_ask_depth_contracts", ""),
        ("NO ask depth", "no_ask_depth_contracts", ""),
        ("taker net YES 15s", "kalshi_taker_net_yes_volume_15s", ""),
    ])
    if kalshi:
        parts.append(f"Kalshi: {kalshi}")
    spot = _metric_parts(row, [
        ("imb", "spot_depth_imbalance", ""),
        ("sell15", "spot_depth_trade_sell_notional_15s", ""),
        ("net15$", "spot_depth_trade_net_notional_15s", ""),
        ("net60$", "spot_depth_trade_net_notional_60s", ""),
        ("net60qty", "spot_depth_trade_net_qty_60s", ""),
    ])
    if spot:
        parts.append(f"Spot: {spot}")
    if row.get("original_source_side"):
        parts.append(f"Original side: {html.escape(str(row.get('original_source_side')))}")
    parts.append(f"Reasons: {html.escape(reasons)}")
    mode = (
        "research-only tracking"
        if str(row.get("bot_name") or "") == "bnb_yes_reversal"
        else "paper/research tracking"
    )
    parts.append(f"Mode: {mode}")
    return "\n".join(parts)


def build_thirteen_m_sniper_alert(row: Mapping[str, Any]) -> str:
    thresholds = _thresholds(row)
    reasons = str(row.get("reason_codes") or "").replace(",", ", ")
    side = row.get("side") or thresholds.get("model_side") or ""
    price = row.get("entry_ask_cents")
    if price is None:
        price = thresholds.get("model_side_ask_cents")
    probability = thresholds.get("ev_win_probability")
    if probability is None:
        probability = thresholds.get("model_side_probability")
    ev = thresholds.get("ev_cents")
    resolved_n = int(float(thresholds.get("resolved_n") or 0))
    acc = thresholds.get("resolved_accuracy")
    provisional = f"PROVISIONAL (n={resolved_n}, acc={_pct(acc)})"
    parts = [
        "<b>V3 13M EARLY</b>",
        (
            f"{html.escape(str(row.get('asset') or ''))} "
            f"{html.escape(str(side))} "
            f"{html.escape(str(row.get('interval') or '13M'))}"
        ),
        f"Ticker: <code>{html.escape(str(row.get('ticker') or ''))}</code>",
        (
            "Entry: "
            f"{_fmt(price, 'c')} model-side ask, "
            f"prob {_pct(probability)}, "
            f"EV {_fmt(ev, 'c')}"
        ),
        provisional,
    ]
    degraded = _degraded_line(row)
    if degraded:
        parts.insert(1, degraded)
    flow = row.get("spot_depth_trade_net_notional_60s")
    p70 = thresholds.get("spot_depth_trade_net_notional_60s_abs_p70")
    if flow is not None or p70 is not None:
        parts.append(f"Flow: net60$ {_fmt(flow)}, p70 {_fmt(p70)}")
    if thresholds.get("auto_mute_active"):
        parts.append("Notify guard: auto-muted; recording continues")
    parts.append(f"Reasons: {html.escape(reasons)}")
    parts.append("Mode: paper/read-only alert; no executor route")
    return "\n".join(parts)


def _whole(value: Any, suffix: str = "") -> str:
    """Compact numeric: whole numbers render without a decimal (67 -> "67c")."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if abs(v - round(v)) < 0.05:
        return html.escape(f"{int(round(v))}{suffix}")
    return html.escape(f"{v:.1f}{suffix}")


def _book_stats_line(thresholds: Mapping[str, Any]) -> str | None:
    """`Book: 12W-2L · acc 85.7% · WLB 71.2%` from the resolved-stats profile."""
    try:
        n = int(float(thresholds.get("resolved_n") or 0))
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return "Book: no resolved trades yet (prior-based EV)"
    try:
        correct = int(float(thresholds.get("resolved_correct")))
    except (TypeError, ValueError):
        acc = thresholds.get("resolved_accuracy")
        try:
            correct = int(round(float(acc) * n)) if acc is not None else None
        except (TypeError, ValueError):
            correct = None
    acc_text = _pct(thresholds.get("resolved_accuracy"))
    lb_text = _pct(thresholds.get("resolved_wilson_lb"))
    record = f"{correct}W-{n - correct}L" if correct is not None else f"n={n}"
    return f"Book: {record} · acc {acc_text} · WLB {lb_text}"


def build_warn_flip_alert(row: Mapping[str, Any]) -> str:
    """Book 1 alert — action-first UI: the BUY line is the message."""
    thresholds = _thresholds(row)
    side = str(row.get("side") or thresholds.get("flip_side") or "").upper()
    asset = str(row.get("asset") or "")
    ask = row.get("entry_ask_cents")
    if ask is None:
        ask = thresholds.get("flip_side_ask_cents")
    chase = thresholds.get("chase_max_cents")
    tier = str(thresholds.get("tier") or "")
    warn_sr = thresholds.get("warn_seconds_remaining")
    cycles = thresholds.get("confirm_cycles")
    span = thresholds.get("confirm_span_seconds")
    ev = thresholds.get("ev_cents")
    band_lo = thresholds.get("band_lo_cents")
    band_hi = thresholds.get("band_hi_cents")
    parts = [
        f"\U0001f3af <b>V3 WARN-FLIP ENTRY</b> — {html.escape(asset)} {html.escape(side)}",
        f"<b>BUY {html.escape(side)} @ {_whole(ask, 'c')}</b>"
        + (f" · chase ≤ {_whole(chase, 'c')}" if chase is not None else ""),
    ]
    tier_bits = []
    if tier:
        band_text = (
            f"{_whole(band_lo, '')}-{_whole(band_hi, 'c')}"
            if band_lo is not None and band_hi is not None
            else ""
        )
        tier_bits.append(f"Tier: {html.escape(tier)}" + (f" ({band_text} band)" if band_text else ""))
    if warn_sr is not None:
        tier_bits.append(f"⏱ {_whole(warn_sr, 's')} left")
    if tier_bits:
        parts.append(" · ".join(tier_bits))
    if cycles is not None or span is not None:
        parts.append(f"Confirmed flip: {_whole(cycles)} cycles / {_whole(span, 's')}")
    book = _book_stats_line(thresholds)
    if book:
        parts.append(book)
    if ev is not None:
        prior = str(thresholds.get("ev_prior_source") or "")
        suffix = " (prior)" if prior == "discovery_n58" else ""
        parts.append(f"EV ≈ {_whole(ev, 'c')}/contract after fees{suffix}")
    degraded = _degraded_line(row)
    if degraded:
        parts.insert(1, degraded)
    if row.get("original_source_side"):
        parts.append(
            f"Flipped from: {html.escape(str(row.get('original_source_side')))} entry"
        )
    parts.append(f"Ticker: <code>{html.escape(str(row.get('ticker') or ''))}</code>")
    parts.append("Mode: paper alert — enter manually, size to visible depth")
    return "\n".join(parts)


def build_fav_10m_alert(row: Mapping[str, Any]) -> str:
    """Book 2 alert — same action-first UI, shadow-test banner until promoted."""
    thresholds = _thresholds(row)
    side = str(row.get("side") or thresholds.get("favorite_side") or "").upper()
    asset = str(row.get("asset") or "")
    ask = row.get("entry_ask_cents")
    if ask is None:
        ask = thresholds.get("favorite_side_ask_cents")
    ev = thresholds.get("ev_cents")
    band_lo = thresholds.get("band_lo_cents")
    band_hi = thresholds.get("band_hi_cents")
    parts = [
        f"⭐ <b>V3 FAVORITE 10M</b> — {html.escape(asset)} {html.escape(side)}",
        f"<b>BUY {html.escape(side)} @ {_whole(ask, 'c')}</b> · chase ≤ {_whole(_chase_max(ask), 'c')}",
        (
            f"Band: {_whole(band_lo, '')}-{_whole(band_hi, 'c')} favorite · 10M mark"
            if band_lo is not None and band_hi is not None
            else "Band: favorite · 10M mark"
        ),
    ]
    book = _book_stats_line(thresholds)
    if book:
        parts.append(book)
    if ev is not None:
        prior = str(thresholds.get("ev_prior_source") or "")
        suffix = " (backtest prior)" if prior == "backtest_n656" else ""
        parts.append(f"EV ≈ {_whole(ev, 'c')}/contract after fees{suffix}")
    degraded = _degraded_line(row)
    if degraded:
        parts.insert(1, degraded)
    parts.append(f"Ticker: <code>{html.escape(str(row.get('ticker') or ''))}</code>")
    parts.append("Mode: paper alert — forward test; half size vs warn-flip")
    return "\n".join(parts)


def _chase_max(ask: Any) -> float | None:
    try:
        return float(ask) + 1.0
    except (TypeError, ValueError):
        return None


def _is_top_pick(row: Mapping[str, Any]) -> bool:
    return str(row.get("bot_name") or "") == "top_pick_13m"


def build_top_pick_alert(row: Mapping[str, Any]) -> str:
    """Best-trade card — the one pick per 15m window, ranked by measured profit.

    Fires every cycle by owner request; the cell line reports the pick's measured
    per-bucket economics honestly (only the 85-90c favorite cell is ~breakeven+
    at 13M — everything else is a fallback with a small expected cost).
    """
    thresholds = _thresholds(row)
    side = str(row.get("side") or thresholds.get("pick_side") or "").upper()
    asset = str(row.get("asset") or "")
    ask = row.get("entry_ask_cents")
    if ask is None:
        ask = thresholds.get("pick_ask_cents")
    cal = thresholds.get("calibrated_yes_probability")
    model_side_prob = None
    try:
        if cal is not None:
            model_side_prob = float(cal) if side == "YES" else 1.0 - float(cal)
    except (TypeError, ValueError):
        model_side_prob = None
    slate_n = thresholds.get("slate_n")
    parts = [
        f"\U0001f3c6 <b>V3 BEST TRADE 13M</b> — {html.escape(asset)} {html.escape(side)}",
        f"<b>BUY {html.escape(side)} @ {_whole(ask, 'c')}</b> · chase ≤ {_whole(_chase_max(ask), 'c')}"
        + (
            f" · model {_whole(model_side_prob * 100.0, '%')}"
            if model_side_prob is not None
            else ""
        ),
    ]
    grade = str(thresholds.get("grade") or "").upper()
    if grade == "TRADE":
        parts.append("Grade: ✅ TRADE — alt favorite band (best measured cell)")
    elif grade == "CAUTION":
        parts.append("Grade: ⚠️ CAUTION — fallback cell, thin edge; smallest size")
    elif grade == "SKIP":
        reason = str(thresholds.get("grade_reason") or "")
        why = (
            "BTC/ETH books are efficiently priced (−3c/tr historical)"
            if reason == "MAJOR_EFFICIENT_BOOK"
            else "outside measured bands"
        )
        parts.append(f"Grade: ⛔ SKIP — {why}; shown for the record, do not trade")
    pick_bits = []
    if slate_n is not None:
        pick_bits.append(f"Best of {_whole(slate_n)} this cycle")
    if str(thresholds.get("pick_phase") or "").upper() == "FALLBACK":
        pick_bits.append("late/thin slate (fallback fire)")
    bucket_ev = thresholds.get("bucket_ev_cents")
    if thresholds.get("fav_band"):
        pick_bits.append("cell: FAV-BAND (best measured cell at 13M)")
    elif bucket_ev is not None:
        pick_bits.append(
            f"cell: FALLBACK (best available; measured {_whole(bucket_ev, 'c')}/tr — expect small losses)"
        )
    if pick_bits:
        parts.append(" · ".join(pick_bits))
    book = _book_stats_line(thresholds)
    if book:
        parts.append(book)
    degraded = _degraded_line(row)
    if degraded:
        parts.insert(1, degraded)
    parts.append(f"Ticker: <code>{html.escape(str(row.get('ticker') or ''))}</code>")
    parts.append(
        "Mode: one pick every cycle by request — size SMALL; "
        "\U0001f3af warn-flip cards outrank this whenever they fire"
    )
    return "\n".join(parts)


def _is_drift_pick(row: Mapping[str, Any]) -> bool:
    return str(row.get("bot_name") or "") in {
        "drift_13m",
        "drift_flow_spread_13m",
    }


def _is_drift_addon(row: Mapping[str, Any]) -> bool:
    return str(row.get("bot_name") or "") == "drift_addon_requal"


def _is_drift_latequal(row: Mapping[str, Any]) -> bool:
    return str(row.get("bot_name") or "") == "drift_latequal_12m_11m"


def _is_drift_no_mirror(row: Mapping[str, Any]) -> bool:
    return str(row.get("bot_name") or "") == "drift_no_mirror"


def _drift_size_banner(stack: Any) -> str:
    """One-glance size grade from the stacked tilt weight."""
    try:
        w = float(stack)
    except (TypeError, ValueError):
        return "✅ NORMAL SIZE"
    if w >= 1.25:
        return f"⭐ FULL SIZE ({w:.2g}×)"
    if w < 0.75:
        return f"🔉 HALF SIZE ({w:.2g}×)"
    return f"✅ NORMAL SIZE ({w:.2g}×)"


def _drift_sizing_reason(sp: Any, sess_w: Any) -> str:
    bits: list[str] = []
    try:
        s = float(sp)
        if s <= 2:
            bits.append(f"spread {s:.0f}¢ → tight (MM-priced)")
        elif s <= 4:
            bits.append(f"spread {s:.0f}¢ → upsize")
        else:
            bits.append(f"spread {s:.0f}¢ → downsize")
    except (TypeError, ValueError):
        pass
    try:
        w = float(sess_w)
        if w > 1.0:
            bits.append("US hours → upsize")
        elif w < 0.8:
            bits.append("EU hours → downsize")
        else:
            bits.append("overnight → neutral")
    except (TypeError, ValueError):
        pass
    return " · ".join(bits)


def _drift_confirmation_line(thresholds: Mapping[str, Any]) -> str | None:
    path = str(thresholds.get("gate_path") or "")
    flow = thresholds.get("spot_depth_trade_net_notional_60s")
    spread = thresholds.get("spread_cents")
    try:
        flow_text = f"${float(flow):+,.0f}"
    except (TypeError, ValueError):
        flow_text = "n/a"
    try:
        spread_text = f"{float(spread):.0f}c"
    except (TypeError, ValueError):
        spread_text = "n/a"
    if path == "FLOW_AND_SPREAD":
        return f"Confirmed: 60s spot flow {flow_text} + tight spread {spread_text}"
    if path == "FLOW_60S_POSITIVE":
        return f"Confirmed: 60s spot flow {flow_text} (positive)"
    if path == "SPREAD_LTE_2":
        return f"Confirmed: Kalshi spread {spread_text} (<=2c fallback)"
    return None


def build_drift_pick_alert(row: Mapping[str, Any]) -> str:
    """Drift Shadow base-book pick — ultoim_v2 panel grammar (bold header
    outside a <pre> block, body inside one <pre>), owner-approved card layout.
    MUST NOT carry live formatter/suppression markers ("V9.5 CHECK",
    "ENTRY RECOMMENDED", "NO ENTRY YET", "Hourly Report —", "TOP 3 PICKS")."""
    thresholds = _thresholds(row)
    asset = str(row.get("asset") or "")
    ask = row.get("entry_ask_cents")
    if ask is None:
        ask = thresholds.get("pick_ask_cents")
    fee = None
    try:
        p = float(ask) / 100.0
        fee = math.ceil(7.0 * p * (1.0 - p))
    except (TypeError, ValueError):
        pass
    breakeven = (
        f"{(float(ask) + fee):.0f}%" if fee is not None else "n/a"
    )
    banner = _drift_size_banner(thresholds.get("stack_weight"))
    n = int(float(thresholds.get("book_n_resolved") or 0))
    wins = thresholds.get("book_wins")
    pnl = thresholds.get("book_total_pnl_cents")
    verdict_n = int(float(thresholds.get("book_verdict_n") or 60))
    if n > 0 and wins is not None:
        w = int(float(wins))
        book_line = (
            f"Book: {w}W-{n - w}L · {float(pnl):+.0f}¢ · verdict at n={verdict_n}"
            if pnl is not None else f"Book: {w}W-{n - w}L · verdict at n={verdict_n}"
        )
    else:
        book_line = f"Book: no resolved picks yet · verdict at n={verdict_n}"
    header = f"🌊 <b>DRIFT PICK 13M · {html.escape(banner)}</b>"
    body = [
        "PAPER SIGNAL — record-only book; you trade it manually or not at all.",
        "",
        f"BUY YES — {asset} @ {_plain_whole(ask)}¢ · breakeven {breakeven}",
        f"Sizing: {_drift_sizing_reason(thresholds.get('spread_cents'), thresholds.get('session_weight'))}",
        _drift_fill_line_plain(thresholds.get("depth_contracts"), ask),
        "Size guide: 25–50 contracts comfort · ~100 max",
        book_line,
        f"Ticker: {str(row.get('ticker') or '')}",
        "Drift Shadow · paper/research · no orders placed",
    ]
    confirmation = _drift_confirmation_line(thresholds)
    if confirmation:
        if str(row.get("bot_name") or "") == "drift_flow_spread_13m":
            header = header.replace("DRIFT PICK 13M", "DRIFT FLOW CONFIRMED 13M")
        body.insert(2, confirmation)
    return header + "\n<pre>" + "\n".join(html.escape(part) for part in body) + "</pre>"


def _drift_track_book_line(thresholds: Mapping[str, Any]) -> str:
    n = int(float(thresholds.get("track_n_resolved") or 0))
    wins = thresholds.get("track_wins")
    pnl = thresholds.get("track_total_pnl_cents")
    verdict_n = int(float(thresholds.get("track_verdict_n") or 40))
    if n <= 0 or wins is None:
        return f"Track: no resolved entries yet | first verdict at n={verdict_n}"
    w = int(float(wins))
    pnl_text = f" | {float(pnl):+.0f}c" if pnl is not None else ""
    return f"Track: {w}W-{n - w}L{pnl_text} | first verdict at n={verdict_n}"


def build_drift_addon_alert(row: Mapping[str, Any]) -> str:
    """Paper add-on card; explicitly excluded from independent-pick accuracy."""
    thresholds = _thresholds(row)
    asset = str(row.get("asset") or "")
    interval = str(row.get("interval") or "")
    ask = row.get("entry_ask_cents")
    base_ask = thresholds.get("base_ask_cents")
    header = f"<b>DRIFT ADD-ON {html.escape(interval)} | PAPER</b>"
    body = [
        "CORRELATED ADD-ON - the original YES pick re-passed every frozen gate.",
        "",
        f"ADD YES - {asset} @ {_plain_whole(ask)}c",
        f"Base entry: {_plain_whole(base_ask)}c | requalified: {interval}",
        "Risk: add at most 0.5x; total window exposure cap is 1.5x.",
        "Split that cap across assets when several picks share the same window.",
        "Accounting: correlated exposure, NOT an independent accuracy sample.",
        _drift_track_book_line(thresholds),
        f"Ticker: {str(row.get('ticker') or '')}",
        "Paper/research only | no order placed",
    ]
    return header + "\n<pre>" + "\n".join(html.escape(part) for part in body) + "</pre>"


def build_drift_latequal_alert(row: Mapping[str, Any]) -> str:
    """Research-only card for a clean sub-60c 13M row repriced by 12M/11M."""
    thresholds = _thresholds(row)
    asset = str(row.get("asset") or "")
    interval = str(row.get("interval") or "")
    ask = row.get("entry_ask_cents")
    ask13 = thresholds.get("ask13_cents")
    header = f"<b>DRIFT LATE QUAL {interval} | RESEARCH ONLY</b>"
    body = [
        "NEW INDEPENDENT PAPER PICK - clean at 13M, then repriced into 60-73c.",
        "",
        f"BUY YES - {asset} @ {_plain_whole(ask)}c",
        f"13M watch price: {_plain_whole(ask13)}c | qualified: {interval}",
        "Only 12M and 11M qualify; the losing 10M extension is disabled.",
        "Keep research-only until the frozen forward sample reaches its bar.",
        _drift_track_book_line(thresholds),
        f"Ticker: {str(row.get('ticker') or '')}",
        "Research only | no order placed",
    ]
    return header + "\n<pre>" + "\n".join(html.escape(part) for part in body) + "</pre>"


def build_drift_no_mirror_group_alert(rows: Sequence[Mapping[str, Any]]) -> str:
    """One compact research card for every positive NO candidate in a window."""
    candidates = [row for row in rows if _is_drift_no_mirror(row)]
    if not candidates:
        return "<b>DRIFT NO WATCH \u2014 RESEARCH ONLY</b>\nNo qualifying candidates."
    first = candidates[0]
    thresholds = _thresholds(first)
    window_key = first.get("window_key")
    body = [
        "VISUAL WATCH ONLY - no order is placed.",
        f"Window: {window_key if window_key is not None else 'n/a'} | "
        f"Candidates: {len(candidates)}",
        "",
    ]
    highlight_codes = {"MID_PRICE_65_69", "TIGHT_SPREAD", "BTC_AGREES_NO"}
    for row in candidates:
        tags = [
            code.strip()
            for code in str(row.get("reason_codes") or "").split(",")
            if code.strip() in highlight_codes
        ]
        tag_text = ", ".join(tags) if tags else "POSITIVE_FILTER"
        body.extend([
            (
                f"{str(row.get('asset') or '')} NO @ "
                f"{_plain_whole(row.get('entry_ask_cents'))}c | "
                f"spread {_plain_whole(row.get('spread_cents'))}c"
            ),
            f"Tags: {tag_text}",
            f"Ticker: {str(row.get('ticker') or '')}",
        ])
    body.extend([
        "",
        _drift_track_book_line(thresholds),
        "Excluded entirely: BNB, DOGE, and untagged NO candidates.",
        "Separate from Drift YES | prospective research only",
    ])
    header = "<b>DRIFT NO WATCH \u2014 RESEARCH ONLY</b>"
    degraded = _degraded_line(first)
    if degraded:
        header += "\n" + degraded
    return header + "\n<pre>" + "\n".join(html.escape(part) for part in body) + "</pre>"


def _plain_whole(value: Any) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return f"{int(round(v))}" if abs(v - round(v)) < 0.05 else f"{v:.1f}"


def _drift_fill_line_plain(depth: Any, ask: Any) -> str:
    """Unescaped variant for inside the <pre> body (escaping happens at join)."""
    ask_txt = f"{_plain_whole(ask)}¢"
    try:
        d = float(depth)
    except (TypeError, ValueError):
        return f"Fill: depth unknown → rest at {ask_txt}, chase +1¢ only if unfilled"
    if d >= 50:
        return f"Fill: depth {d:.0f} @ ask → rest at {ask_txt}, don't pay up"
    try:
        chase = f"{_plain_whole(float(ask) + 1.0)}¢"
    except (TypeError, ValueError):
        chase = "+1¢"
    return f"Fill: thin book ({d:.0f} @ ask) → pay {chase} now to get filled"


def _is_warn_flip(row: Mapping[str, Any]) -> bool:
    return str(row.get("bot_name") or "") == "warn_flip_entry"


def _is_fav_10m(row: Mapping[str, Any]) -> bool:
    return str(row.get("bot_name") or "") == "fav_10m"


def build_v3_auto_mute_alert(row: Mapping[str, Any], *, header: str | None = None) -> str:
    thresholds = _thresholds(row)
    resolved_n = int(float(thresholds.get("resolved_n") or 0))
    acc = thresholds.get("resolved_accuracy")
    lb = thresholds.get("resolved_wilson_lb")
    parts = [
        f"<b>{html.escape(header)}</b>" if header else "<b>V3 13M EARLY AUTO-MUTED</b>",
        (
            "Notify guard tripped: "
            f"n={resolved_n}, acc={_pct(acc)}, Wilson LB={_pct(lb)}"
        ),
        "Recording continues; Telegram alerts are muted until thresholds recover or config changes.",
    ]
    degraded = _degraded_line(row)
    if degraded:
        parts.insert(1, degraded)
    return "\n".join(parts)


def build_v3_alert(row: Mapping[str, Any]) -> str:
    if _is_bnb_combined(row):
        return build_bnb_combined_alert(row)
    if _is_thirteen_m_sniper(row):
        return build_thirteen_m_sniper_alert(row)
    if _is_warn_flip(row):
        return build_warn_flip_alert(row)
    if _is_fav_10m(row):
        return build_fav_10m_alert(row)
    if _is_top_pick(row):
        return build_top_pick_alert(row)
    if _is_drift_addon(row):
        return build_drift_addon_alert(row)
    if _is_drift_latequal(row):
        return build_drift_latequal_alert(row)
    if _is_drift_no_mirror(row):
        return build_drift_no_mirror_group_alert([row])
    if _is_drift_pick(row):
        return build_drift_pick_alert(row)

    reasons = str(row.get("reason_codes") or "").replace(",", ", ")
    is_reversal = str(row.get("bot_name") or "") == "bnb_yes_reversal"
    tier = str(row.get("tier") or "").upper()
    if _is_confidence_tier(row):
        header = {
            "A": "<b>V3 TIER A / STRICT HIGH-CONFIDENCE</b>",
            "B": "<b>V3 TIER B / VOLUME EXPANSION</b>",
            "C": "<b>V3 TIER C / RESEARCH ONLY</b>",
        }.get(tier, "<b>V3 CONFIDENCE TIER</b>")
    elif _is_hvf_wrapper(row):
        header = "<b>V3 HVF DEPTH/FLOW PICK</b>"
    elif _is_depth_formula(row):
        header = "<b>V3 15M DEPTH FORMULA / RESEARCH</b>"
    else:
        header = "<b>V3 RESEARCH YES REVERSAL</b>" if is_reversal else "<b>V3 FILTERED PICK</b>"
    parts = [
        header,
        (
            f"{html.escape(str(row.get('asset') or ''))} "
            f"{html.escape(str(row.get('side') or ''))} "
            f"{html.escape(str(row.get('interval') or ''))}"
        ),
        f"Bot: {html.escape(_bot_label(str(row.get('bot_name') or '')))}",
        f"Rule: {html.escape(str(row.get('source_rule') or 'UNKNOWN'))}",
        f"Ticker: <code>{html.escape(str(row.get('ticker') or ''))}</code>",
        (
            "Entry: "
            f"{_fmt(row.get('entry_ask_cents'), 'c')} ask, "
            f"{_fmt(row.get('spread_cents'), 'c')} spread"
        ),
    ]
    degraded = _degraded_line(row)
    if degraded:
        parts.insert(1, degraded)
    kalshi = _metric_parts(row, [
        ("depth", "depth_contracts", ""),
        ("NO bid depth", "no_bid_depth_contracts", ""),
        ("NO ask depth", "no_ask_depth_contracts", ""),
        ("YES ask depth", "yes_ask_depth_contracts", ""),
        ("taker net YES 15s", "kalshi_taker_net_yes_volume_15s", ""),
    ])
    if kalshi:
        parts.append(f"Kalshi: {kalshi}")
    if _is_depth_formula(row):
        ratio = _ratio_text(row.get("no_bid_depth_contracts"), row.get("depth_contracts"))
        if ratio is not None:
            parts.append(f"Depth formula: NO bid / selected ask depth ratio {ratio}")
    spot = _metric_parts(row, [
        ("imb", "spot_depth_imbalance", ""),
        ("sell15", "spot_depth_trade_sell_notional_15s", ""),
        ("net60qty", "spot_depth_trade_net_qty_60s", ""),
        ("net60$", "spot_depth_trade_net_notional_60s", ""),
    ])
    if spot:
        parts.append(f"Spot: {spot}")
    coinbase = _metric_parts(row, [
        ("top12", "coinbase_l2_top_12_imbalance_notional", ""),
        ("top60", "coinbase_l2_top_60_imbalance_notional", ""),
        ("top250", "coinbase_l2_top_250_imbalance_notional", ""),
    ])
    if coinbase:
        parts.append(f"Coinbase L2: {coinbase}")
    btc = _metric_parts(row, [
        ("depth", "btc_depth_contracts", ""),
        ("pressure", "btc_book_pressure_cents", "c"),
    ])
    if row.get("btc_dominant_side"):
        btc = (btc + ", " if btc else "") + f"side {html.escape(str(row.get('btc_dominant_side')))}"
    if btc:
        parts.append(f"BTC: {btc}")
    if row.get("original_source_side"):
        parts.append(f"Original side: {html.escape(str(row.get('original_source_side')))}")
    if tier:
        parts.append(f"Tier: {html.escape(tier)}")
    parts.append(f"Reasons: {html.escape(reasons)}")
    if tier == "C" or _is_depth_formula(row):
        parts.append("Mode: research-only tracking")
    else:
        parts.append("Mode: research-only tracking" if is_reversal else "Mode: paper/research tracking")
    return "\n".join(parts)


class V3Telegram:
    """Thin adapter over the shared ``notifications.telegram_client``.

    Keeps the historical env resolution: dedicated chat id
    (``Q15_V3_TELEGRAM_CHAT_ID`` — never the live room) and the
    ``Q15_V3_TELEGRAM_ENABLED`` gate, which defaults OFF.
    """

    def __init__(self, *, token: str | None = None, chat_id: str | None = None,
                 enabled: bool | None = None, retries: int = 2,
                 sleep_seconds: float = 0.5) -> None:
        active = _bool("Q15_V3_TELEGRAM_ENABLED", False) if enabled is None else bool(enabled)
        self._client = TelegramSendClient(
            token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN"),
            (
                chat_id
                if chat_id is not None
                else (os.environ.get("Q15_V3_TELEGRAM_CHAT_ID") or "")
            ),
            enabled=active,
            retries=retries,
            sleep_seconds=sleep_seconds,
        )
        self.token = self._client.token
        self.chat_id = self._client.chat_id
        self.enabled = self._client.enabled
        self.retries = self._client.retries
        self.sleep_seconds = self._client.sleep_seconds

    def send(self, text: str) -> dict[str, Any]:
        return self._client.send(text)
