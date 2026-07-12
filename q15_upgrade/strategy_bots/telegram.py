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
        "drift_asymmetric_volume_shadow": "Drift Asymmetric Volume Shadow",
        "drift_balanced_v95_shadow": "Drift Balanced V95 Shadow",
        "drift_accuracy_v91_shadow": "Drift Accuracy V91 Shadow",
        "drift_addon_requal": "Drift Requalification Add-On",
        "drift_latequal_12m_11m": "Drift Late Qualifier",
        "drift_no_mirror": "Drift NO Mirror",
        "drift_no_expansion": "Drift NO Expansion",
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
        "drift_asymmetric_volume_shadow",
        "drift_balanced_v95_shadow",
        "drift_accuracy_v91_shadow",
    }


def _is_drift_addon(row: Mapping[str, Any]) -> bool:
    return str(row.get("bot_name") or "") == "drift_addon_requal"


def _is_drift_latequal(row: Mapping[str, Any]) -> bool:
    return str(row.get("bot_name") or "") == "drift_latequal_12m_11m"


def _is_drift_no_mirror(row: Mapping[str, Any]) -> bool:
    return str(row.get("bot_name") or "") == "drift_no_mirror"


def _is_drift_no_expansion(row: Mapping[str, Any]) -> bool:
    return str(row.get("bot_name") or "") == "drift_no_expansion"


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
    path = str(
        thresholds.get("confirmation_gate_path")
        or thresholds.get("gate_path")
        or ""
    )
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
    if path == "FLOW_15S_POSITIVE":
        return "Research gate: fresh positive 15s flow only"
    if path == "FLOW_15S_AND_SPREAD":
        return f"Research gate: fresh positive 15s flow + spread {spread_text}"
    if path == "FLOW_MISSING_OR_STALE":
        return f"Gate: research only - flow missing/stale; spread {spread_text}"
    if path == "STRUCTURE_MISSING":
        return "Gate: research only - structural evidence missing"
    if path == "STRUCTURE_OUTSIDE_FROZEN_CORE":
        return "Gate: rejected - outside frozen structure"
    if path.startswith("FLOW_NONPOSITIVE_AND_SPREAD_GT_"):
        return f"Gate: rejected - nonpositive flow; spread {spread_text}"
    if path == "ASYMMETRIC_VOLUME_SHADOW":
        return "Research cohort: asymmetric price-distance volume"
    if path == "BALANCED_V95_15M_YES_SHADOW":
        return "Research cohort: asymmetric + V95 15M YES agreement"
    if path == "ACCURACY_V91_FULL_PATH_75_SHADOW":
        return "Research cohort: asymmetric + V91 YES/all >=75%"
    return None


def _drift_value(
    row: Mapping[str, Any], thresholds: Mapping[str, Any], *keys: str
) -> Any:
    for source in (thresholds, row):
        for key in keys:
            if key in source and source.get(key) is not None:
                return source.get(key)
    return None


def _drift_plain(value: Any, suffix: str = "", digits: int = 1) -> str:
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return "n/a"
    text = f"{number:.{digits}f}".rstrip("0").rstrip(".")
    return text + suffix


def _drift_fraction(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        return "n/a"
    return f"{number * 100.0:.1f}%".replace(".0%", "%")


def _drift_bool(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return "n/a"


def _drift_signed(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if not math.isfinite(number):
        return "n/a"
    return f"{number:+,.0f}"


def _drift_short_sha(value: Any) -> str:
    if value is None:
        return "n/a"
    text = str(value).strip()
    return text[:8] if text else "n/a"


def _drift_feed_status(status: Any, missing_reason: Any) -> str:
    status_text = str(status) if status is not None else "n/a"
    if missing_reason is None:
        return status_text
    reason_text = str(missing_reason).strip()
    return f"{status_text} ({reason_text})" if reason_text else status_text


def _drift_evidence_lines(
    row: Mapping[str, Any], thresholds: Mapping[str, Any]
) -> list[str]:
    """Compact, honest evidence block shared by live and research Drift cards."""
    grade = _drift_value(
        row,
        thresholds,
        "drift_evidence_grade",
        "evidence_grade",
        "v95_15m_grade",
        "confidence_grade",
    )
    snapshot_age = _drift_value(row, thresholds, "spot_depth_snapshot_age_seconds")
    book_age = _drift_value(row, thresholds, "spot_depth_age_seconds")
    trade_age = _drift_value(row, thresholds, "spot_depth_trade_age_seconds")
    spot_status_raw = _drift_value(row, thresholds, "spot_depth_status")
    spot_status = str(spot_status_raw).lower() if spot_status_raw is not None else "n/a"
    if spot_status == "ok":
        spot_status = "fresh"

    v91_fraction = _drift_value(
        row, thresholds, "v91_full_path_yes_fraction", "drift_v91_yes_fraction_all"
    )
    v91_directional = _drift_value(
        row,
        thresholds,
        "v91_full_path_directional_fraction",
        "drift_v91_yes_fraction_directional",
    )
    v91_observations = _drift_value(
        row, thresholds, "v91_observation_count", "drift_v91_observation_count"
    )
    v91_yes = _drift_value(row, thresholds, "v91_full_path_yes_count")
    v91_all = _drift_value(row, thresholds, "v91_full_path_all_count")
    v91_counts = (
        f"{_drift_plain(v91_yes, digits=0)}/{_drift_plain(v91_all, digits=0)}"
        if v91_yes is not None or v91_all is not None else "n/a"
    )

    v95_side = _drift_value(
        row,
        thresholds,
        "v95_15m_side",
        "v95_15m_predicted_side",
        "drift_v95_15m_side",
    )
    v95_flow_score = _drift_value(
        row, thresholds, "v95_15m_flow_score", "drift_v95_15m_flow_score"
    )
    v95_flow = _drift_value(
        row, thresholds, "v95_15m_flow_60s", "v95_15m_spot_flow_60s"
    )
    v95_age = _drift_value(
        row, thresholds, "v95_15m_age_seconds", "drift_v95_15m_age_seconds"
    )
    v95_status = _drift_value(row, thresholds, "v95_15m_status")
    v95_status_text = str(v95_status) if v95_status is not None else "n/a"

    btc_side = _drift_value(
        row,
        thresholds,
        "btc_side",
        "drift_btc_15m_side",
        "btc_dominant_side",
        "btc_model_predicted_side",
        "btc_v95_predicted_side",
    )
    btc_grade = _drift_value(row, thresholds, "btc_grade", "btc_v95_grade")
    btc_age = _drift_value(
        row, thresholds, "drift_btc_15m_age_seconds", "btc_15m_age_seconds"
    )
    core_breadth = _drift_value(
        row,
        thresholds,
        "drift_core_breadth",
        "core_breadth",
    )
    asymmetric_breadth = _drift_value(
        row,
        thresholds,
        "drift_asymmetric_breadth",
        "asymmetric_breadth",
    )
    flow_1m = _drift_value(row, thresholds, "drift_flow_1m", "drift_flow_60s")
    flow_3m = _drift_value(row, thresholds, "drift_flow_3m")
    flow_5m = _drift_value(row, thresholds, "drift_flow_5m")
    flow_13m = _drift_value(row, thresholds, "drift_flow_13m")
    flow_coverage = _drift_value(row, thresholds, "drift_flow_coverage")
    cohort = _drift_value(row, thresholds, "cohort", "cohort_name")
    feature_cohort = _drift_value(row, thresholds, "feature_cohort")
    policy_build = _drift_value(row, thresholds, "build", "rule_version")
    build_sha = _drift_value(row, thresholds, "build_sha", "decision_build_sha")
    incremental = _drift_value(row, thresholds, "incremental_to_core")
    review = _drift_value(row, thresholds, "review_stage")
    index_status = _drift_value(row, thresholds, "index_status")
    index_missing = _drift_value(row, thresholds, "index_missing_reason")
    kalshi_status = _drift_value(row, thresholds, "kalshi_depth_status")
    kalshi_missing = _drift_value(row, thresholds, "kalshi_depth_missing_reason")

    if v95_flow_score is not None:
        v95_flow_text = f"score {_drift_plain(v95_flow_score)}"
    else:
        try:
            v95_flow_text = f"${float(v95_flow):+,.0f}" if v95_flow is not None else "n/a"
        except (TypeError, ValueError):
            v95_flow_text = "n/a"
    return [
        (
            f"Evidence: grade {str(grade) if grade is not None else 'n/a'} | "
            f"ages snap/book/trade "
            f"{_drift_plain(snapshot_age, 's')}/"
            f"{_drift_plain(book_age, 's')}/"
            f"{_drift_plain(trade_age, 's')} ({spot_status})"
        ),
        (
            f"V91: YES/all {_drift_fraction(v91_fraction)} ({v91_counts}) "
            f"dir {_drift_fraction(v91_directional)} n {_drift_plain(v91_observations, digits=0)} | "
            f"V95 15M: {str(v95_side) if v95_side is not None else 'n/a'} "
            f"flow {v95_flow_text} age {_drift_plain(v95_age, 's')} ({v95_status_text})"
        ),
        (
            f"BTC: {str(btc_side) if btc_side is not None else 'n/a'} "
            f"grade {str(btc_grade) if btc_grade is not None else 'n/a'} "
            f"age {_drift_plain(btc_age, 's')} | "
            f"breadth core/asym "
            f"{_drift_plain(core_breadth, digits=0)}/"
            f"{_drift_plain(asymmetric_breadth, digits=0)}"
        ),
        (
            f"Flow 1/3/5/13m: {_drift_signed(flow_1m)}/"
            f"{_drift_signed(flow_3m)}/{_drift_signed(flow_5m)}/"
            f"{_drift_signed(flow_13m)} | coverage {_drift_fraction(flow_coverage)}"
        ),
        (
            f"Cohort: feature {str(feature_cohort) if feature_cohort is not None else 'n/a'} "
            f"| policy {str(cohort) if cohort is not None else 'n/a'} | "
            f"build {_drift_short_sha(build_sha)}/"
            f"{str(policy_build) if policy_build is not None else 'n/a'} | "
            f"incremental {_drift_bool(incremental)} | "
            f"review {str(review) if review is not None else 'n/a'}"
        ),
        (
            f"Feeds: index {_drift_feed_status(index_status, index_missing)} | "
            f"Kalshi {_drift_feed_status(kalshi_status, kalshi_missing)}"
        ),
    ]


def _drift_header(
    row: Mapping[str, Any], thresholds: Mapping[str, Any], banner: str
) -> str:
    bot_name = str(row.get("bot_name") or "")
    status = str(row.get("decision_status") or "").upper()
    path = str(thresholds.get("gate_path") or "")
    cohort_labels = {
        "drift_asymmetric_volume_shadow": "ASYMMETRIC VOLUME",
        "drift_balanced_v95_shadow": "BALANCED V95",
        "drift_accuracy_v91_shadow": "ACCURACY V91",
    }
    if bot_name in cohort_labels or status == "RESEARCH_ONLY":
        label = cohort_labels.get(bot_name) or "FLOW/SPREAD"
        return f"🧪 <b>DRIFT RESEARCH 13M · {label}</b>"
    if status == "REJECTED":
        return "⛔ <b>DRIFT REJECTED 13M</b>"
    if bot_name == "drift_flow_spread_13m" and status in {"", "ACCEPTED"}:
        title = {
            "FLOW_AND_SPREAD": "DRIFT FLOW + SPREAD CONFIRMED 13M",
            "FLOW_60S_POSITIVE": "DRIFT FLOW CONFIRMED 13M",
            "SPREAD_LTE_2": "DRIFT TIGHT-SPREAD FALLBACK 13M",
        }.get(path)
        if title:
            return f"🌊 <b>{title} · {html.escape(banner)}</b>"
    return f"🌊 <b>DRIFT PICK 13M · {html.escape(banner)}</b>"


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
    n = int(float(
        thresholds.get("book_n_resolved")
        or thresholds.get("cohort_n_resolved")
        or 0
    ))
    wins = thresholds.get("book_wins")
    if wins is None:
        wins = thresholds.get("cohort_wins")
    pnl = thresholds.get("book_total_pnl_cents")
    if pnl is None:
        pnl = thresholds.get("cohort_total_pnl_cents")
    verdict_n = int(float(
        thresholds.get("book_verdict_n")
        or thresholds.get("cohort_verdict_n")
        or 60
    ))
    review_bars = thresholds.get("review_bars")
    if isinstance(review_bars, (list, tuple)) and len(review_bars) >= 3:
        review_text = (
            f"reviews n={int(review_bars[0])}/{int(review_bars[1])} "
            f"· promotion n={int(review_bars[2])}"
        )
    else:
        review_text = f"verdict at n={verdict_n}"
    if n > 0 and wins is not None:
        w = int(float(wins))
        book_line = (
            f"Book: {w}W-{n - w}L · {float(pnl):+.0f}¢ · {review_text}"
            if pnl is not None else f"Book: {w}W-{n - w}L · {review_text}"
        )
    else:
        book_line = f"Book: no resolved picks yet · {review_text}"
    header = _drift_header(row, thresholds, banner)
    bot_name = str(row.get("bot_name") or "")
    status = str(row.get("decision_status") or "").upper()
    is_research = status == "RESEARCH_ONLY" or bot_name in {
        "drift_asymmetric_volume_shadow",
        "drift_balanced_v95_shadow",
        "drift_accuracy_v91_shadow",
    }
    if is_research:
        mode_line = "RESEARCH ONLY — counterfactual cohort; never notification eligible."
        action = "TRACK YES"
    elif status == "REJECTED":
        mode_line = "REJECTED — diagnostic record only; no order or notification."
        action = "TRACK YES"
    else:
        mode_line = "PAPER SIGNAL — record-only book; you trade it manually or not at all."
        action = "BUY YES"
    sizing_reason = _drift_sizing_reason(
        thresholds.get("spread_cents"), thresholds.get("session_weight")
    ) or "n/a"
    confirmation = _drift_confirmation_line(thresholds)
    if is_research and confirmation and confirmation.startswith("Confirmed:"):
        confirmation = confirmation.replace(
            "Confirmed:", "Counterfactual confirmation:", 1
        )
    body = [mode_line, ""]
    if confirmation:
        body.append(confirmation)
    body.extend([
        f"{action} — {asset} @ {_plain_whole(ask)}¢ · breakeven {breakeven}",
        *_drift_evidence_lines(row, thresholds),
        f"Sizing: {sizing_reason}",
        _drift_fill_line_plain(thresholds.get("depth_contracts"), ask),
        "Size guide: 25–50 contracts comfort · ~100 max",
        book_line,
        f"Ticker: {str(row.get('ticker') or '')}",
        "Drift Shadow · paper/research · no orders placed",
    ])
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


def build_drift_no_expansion_group_alert(rows: Sequence[Mapping[str, Any]]) -> str:
    """One compact card containing accepted NO-expansion picks for a window."""
    candidates = [row for row in rows if _is_drift_no_expansion(row)]
    if not candidates:
        return "<b>DRIFT NO EXPANSION | PAPER</b>\nNo confirmed candidates."
    first = candidates[0]
    window_key = first.get("window_key")
    body = [
        "FLOW/SPREAD CONFIRMED - no order is placed.",
        f"Window: {window_key if window_key is not None else 'n/a'} | "
        f"Candidates: {len(candidates)}",
        "",
    ]
    for row in candidates:
        thresholds = _thresholds(row)
        path = str(thresholds.get("gate_path") or "")
        flow = thresholds.get("spot_depth_trade_net_notional_60s")
        spread = row.get("spread_cents")
        try:
            flow_text = f"${float(flow):+,.0f}"
        except (TypeError, ValueError):
            flow_text = "n/a"
        if path == "FLOW_AND_SPREAD":
            confirmation = (
                f"negative 60s flow {flow_text} + spread {_plain_whole(spread)}c"
            )
        elif path == "FLOW_60S_NEGATIVE":
            confirmation = f"negative 60s spot flow {flow_text}"
        else:
            confirmation = f"Kalshi spread {_plain_whole(spread)}c (<=2c fallback)"
        body.extend([
            (
                f"{str(row.get('asset') or '')} NO @ "
                f"{_plain_whole(row.get('entry_ask_cents'))}c | "
                f"spread {_plain_whole(spread)}c"
            ),
            f"Confirmed: {confirmation}",
            f"Ticker: {str(row.get('ticker') or '')}",
        ])
    body.extend([
        "",
        _drift_track_book_line(_thresholds(first)),
        "Bands: XRP 60-69c | HYPE 60-64c | DOGE 65-69c",
        "Separate from Drift YES | paper tracking only",
    ])
    header = "<b>DRIFT NO EXPANSION | PAPER</b>"
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
    if _is_drift_no_expansion(row):
        return build_drift_no_expansion_group_alert([row])
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

    def send_with_result(self, text: str) -> dict[str, Any]:
        """Rich outbox-compatible delivery result.

        The shared client already returns the full delivery contract; exposing it
        explicitly prevents retry adapters from treating a non-empty failure dict
        as a successful legacy boolean send.
        """
        return self._client.send(text)

    def send(self, text: str) -> dict[str, Any]:
        return self.send_with_result(text)
