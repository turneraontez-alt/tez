"""Best-effort runtime hooks for the v3 filtered alert system."""
from __future__ import annotations

from dataclasses import replace
import logging
import os
from typing import Any, Mapping, Sequence

import cycle_watchdog

from .btc_regime import enrich_btc_regime
from .kraken_l3_depth import enrich_kraken_l3
from .l2_depth import enrich_coinbase_l2
from .spot_depth import enrich_spot_depth
from .ledger import StrategyBotLedger
from .rules import (
    ACCEPTED,
    BOT_BASELINE,
    BOT_BNB_NO,
    BOT_BNB_YES_REVERSAL,
    BOT_CONFIDENCE_TIER,
    BOT_DEPTH_FORMULA_15M,
    BOT_FAV_10M,
    BOT_HVF_DEPTH_FLOW,
    BOT_HYPE_YES,
    BOT_DRIFT_FLOW_SPREAD,
    BOT_DRIFT_ADDON,
    BOT_DRIFT_LATEQUAL,
    BOT_DRIFT_NO_EXPANSION,
    BOT_DRIFT_NO_MIRROR,
    BOT_THIRTEEN_M_SNIPER,
    BOT_TOP_PICK_13M,
    BOT_WARN_FLIP,
    REJECTED,
    RESEARCH_ONLY,
    STRATEGY_VERSION,
    BotDecision,
    decisions_for_row,
    drift_addon_requal_decision,
    drift_flow_spread_13m_decision,
    drift_latequal_decision,
    drift_no_expansion_decision,
    drift_no_mirror_decision,
    source_side,
    top_pick_13m_decision,
    warn_flip_entry_decision,
)
from .telegram import (
    V3Telegram,
    build_drift_no_expansion_group_alert,
    build_drift_no_mirror_group_alert,
    build_v3_alert,
    build_v3_auto_mute_alert,
)

logger = logging.getLogger("strategy_bots.runtime")

_ledger: StrategyBotLedger | None = None
_telegram: V3Telegram | None = None
_thirteen_m_stats_warning_logged = False
_thirteen_m_flow_warning_logged = False


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def enabled() -> bool:
    return _bool("Q15_STRATEGY_BOTS_ENABLED", False)


def allow_duplicate_hype_windows() -> bool:
    return _bool("Q15_V3_HYPE_ALLOW_DUPLICATE_WINDOW", False)


def telegram_enabled() -> bool:
    return _bool("Q15_V3_TELEGRAM_ENABLED", False)


def research_telegram_enabled() -> bool:
    return _bool("Q15_V3_RESEARCH_TELEGRAM_ENABLED", False)


def depth_formula_telegram_enabled() -> bool:
    return _bool("Q15_V3_DEPTH_FORMULA_TELEGRAM_ENABLED", True)


def thirteen_m_sniper_notify_enabled() -> bool:
    return _bool("Q15_V3_13M_SNIPER_NOTIFY", False)


# The three 2026-07-05 books default ON (owner directive: "make everything on by
# default"). Delivery still requires the V3 Telegram channel itself to be enabled.
def warn_flip_notify_enabled() -> bool:
    return _bool("Q15_V3_WARN_FLIP_NOTIFY", True)


def fav_10m_notify_enabled() -> bool:
    return _bool("Q15_V3_FAV10M_NOTIFY", True)


def top_pick_notify_enabled() -> bool:
    return _bool("Q15_V3_TOP_PICK_13M_NOTIFY", True)


def drift_notify_enabled() -> bool:
    # Legacy compatibility flag. Raw Drift is shadow-only and this route is no
    # longer called by record_drift_pick_row.
    return _bool("Q15_V3_DRIFT_13M_NOTIFY", False)


def drift_flow_spread_notify_enabled() -> bool:
    return _bool("Q15_V3_DRIFT_FLOW_SPREAD_NOTIFY", True)


def drift_addon_notify_enabled() -> bool:
    return _bool("Q15_V3_DRIFT_ADDON_NOTIFY", False)


def drift_latequal_notify_enabled() -> bool:
    return _bool("Q15_V3_DRIFT_LATEQUAL_NOTIFY", False)


def drift_no_mirror_notify_enabled() -> bool:
    return _bool("Q15_V3_DRIFT_NO_MIRROR_NOTIFY", False)


def drift_no_expansion_notify_enabled() -> bool:
    return _bool("Q15_V3_DRIFT_NO_EXPANSION_NOTIFY", True)


def suppress_owned_source_notifications() -> bool:
    return _bool("Q15_V3_SUPPRESS_OWNED_SOURCE_NOTIFICATIONS", False)


def hvf_wrapper_only_notifications() -> bool:
    return _bool("Q15_V3_HVF_DEPTH_FLOW_NOTIFICATIONS_ONLY", False)


def empirical_delivery_guard_enabled() -> bool:
    return _bool("Q15_V3_EMPIRICAL_DELIVERY_GUARD", True)


def empirical_guard_late_intervals() -> set[str]:
    raw = os.environ.get("Q15_V3_EMPIRICAL_LATE_INTERVALS", "10M,11M,12M,13M,14M,15M")
    return {part.strip().upper() for part in raw.split(",") if part.strip()}


def db_path() -> str:
    return os.environ.get("Q15_STRATEGY_BOTS_DB") or "data/q15_strategy_bots_v3.sqlite3"


def _enrich_source_row(
    row: Mapping[str, Any],
    *,
    btc_context: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    enriched: Mapping[str, Any] = row
    try:
        enriched = enrich_coinbase_l2(enriched)
    except (OSError, ValueError) as exc:
        logger.warning("v3 Coinbase L2 enrichment skipped: %s", exc)
    except Exception:  # noqa: BLE001 - non-critical point-in-time feature path
        logger.warning("v3 Coinbase L2 enrichment failed", exc_info=True)
    try:
        enriched = enrich_kraken_l3(enriched)
    except (OSError, ValueError) as exc:
        logger.warning("v3 Kraken L3 enrichment skipped: %s", exc)
    except Exception:  # noqa: BLE001 - non-critical point-in-time feature path
        logger.warning("v3 Kraken L3 enrichment failed", exc_info=True)
    try:
        enriched = enrich_btc_regime(enriched, btc_context=btc_context)
    except (OSError, ValueError) as exc:
        logger.warning("v3 BTC regime enrichment skipped: %s", exc)
    except Exception:  # noqa: BLE001 - non-critical point-in-time feature path
        logger.warning("v3 BTC regime enrichment failed", exc_info=True)
    return enriched


def get_ledger() -> StrategyBotLedger | None:
    global _ledger
    if not enabled():
        return None
    if _ledger is None:
        _ledger = StrategyBotLedger(db_path())
    return _ledger


def get_telegram() -> V3Telegram:
    global _telegram
    if _telegram is None:
        _telegram = V3Telegram()
    return _telegram


def _with_thirteen_m_sniper_context(
    ledger: StrategyBotLedger,
    row: Mapping[str, Any],
) -> Mapping[str, Any]:
    global _thirteen_m_stats_warning_logged, _thirteen_m_flow_warning_logged
    if str(row.get("interval") or "").upper() != "13M":
        return row
    out = dict(row)
    try:
        stats = ledger.bot_accepted_resolved_stats(bot_name=BOT_THIRTEEN_M_SNIPER)
        out.setdefault("thirteen_m_sniper_resolved_n", stats.get("n"))
        out.setdefault("thirteen_m_sniper_correct", stats.get("correct"))
        out.setdefault("thirteen_m_sniper_accuracy", stats.get("accuracy"))
        out.setdefault("thirteen_m_sniper_wilson_lb", stats.get("wilson_lb"))
    except Exception:  # noqa: BLE001 - stats are advisory; recording must continue
        if not _thirteen_m_stats_warning_logged:
            logger.warning("v3 13M sniper stats unavailable", exc_info=True)
            _thirteen_m_stats_warning_logged = True
    try:
        flow_p70 = ledger.trailing_abs_flow_percentile(
            asset=str(row.get("asset") or "").upper() or None,
            created_before=float(row.get("created_at")) if row.get("created_at") is not None else None,
        )
        if flow_p70 is not None and out.get("spot_depth_trade_net_notional_60s_abs_p70") is None:
            out["spot_depth_trade_net_notional_60s_abs_p70"] = flow_p70
    except (TypeError, ValueError):
        logger.debug("v3 13M sniper flow percentile skipped for invalid created_at")
    except Exception:  # noqa: BLE001 - stats are advisory; recording must continue
        if not _thirteen_m_flow_warning_logged:
            logger.warning("v3 13M sniper flow percentile unavailable", exc_info=True)
            _thirteen_m_flow_warning_logged = True
    return out


_book_stats_warning_logged: set[str] = set()


def _with_book_stats_context(
    ledger: StrategyBotLedger,
    row: Mapping[str, Any],
    *,
    bot_name: str,
    prefix: str,
) -> Mapping[str, Any]:
    """Inject a bot's resolved ACCEPTED stats so its rules can self-govern
    (empirical EV + auto-mute), mirroring the 13M sniper convention."""
    out = dict(row)
    try:
        stats = ledger.bot_accepted_resolved_stats(bot_name=bot_name)
        out.setdefault(f"{prefix}_resolved_n", stats.get("n"))
        out.setdefault(f"{prefix}_correct", stats.get("correct"))
        out.setdefault(f"{prefix}_accuracy", stats.get("accuracy"))
        out.setdefault(f"{prefix}_wilson_lb", stats.get("wilson_lb"))
        out.setdefault(f"{prefix}_net_pnl_cents", stats.get("net_pnl_cents"))
    except Exception:  # noqa: BLE001 - stats are advisory; recording must continue
        if bot_name not in _book_stats_warning_logged:
            logger.warning("v3 %s stats unavailable", bot_name, exc_info=True)
            _book_stats_warning_logged.add(bot_name)
    return out


def _with_fav_10m_context(
    ledger: StrategyBotLedger,
    row: Mapping[str, Any],
) -> Mapping[str, Any]:
    if str(row.get("interval") or "").upper() != "10M":
        return row
    return _with_book_stats_context(ledger, row, bot_name=BOT_FAV_10M, prefix="fav_10m")


def _with_duplicate_window_guard(
    ledger: StrategyBotLedger,
    decision: BotDecision,
    row: Mapping[str, Any],
) -> BotDecision:
    if (
        decision.bot_name != BOT_HYPE_YES
        or decision.decision_status != ACCEPTED
        or allow_duplicate_hype_windows()
    ):
        return decision
    try:
        window_key = row.get("window_key")
        if window_key is None:
            return decision
        duplicate = ledger.has_accepted_window(
            bot_name=BOT_HYPE_YES,
            strategy_version=decision.strategy_version,
            asset="HYPE",
            side=source_side(row) or "YES",
            window_key=int(window_key),
            ticker=str(row.get("ticker") or ""),
        )
        if not duplicate:
            return decision
        return replace(
            decision,
            decision_status=REJECTED,
            reason_codes=tuple(decision.reason_codes) + ("DUPLICATE_HYPE_WINDOW_EXPOSURE",),
        )
    except Exception:  # noqa: BLE001 - duplicate guard must never block tracking
        logger.debug("v3 duplicate-window guard failed open", exc_info=True)
        return decision


def _with_empirical_delivery_guard(decision: BotDecision, row: Mapping[str, Any]) -> BotDecision:
    """Downgrade measured weak delivery slices to research while keeping full tracking."""
    if (
        not empirical_delivery_guard_enabled()
        or decision.decision_status != ACCEPTED
        or decision.bot_name in {BOT_BASELINE, BOT_THIRTEEN_M_SNIPER, BOT_FAV_10M, BOT_WARN_FLIP}
    ):
        return decision
    side = source_side(row)
    interval = str(row.get("interval") or "").upper()
    reasons: list[str] = []
    if side == "YES":
        reasons.append("V3_EMPIRICAL_GUARD_YES_RESEARCH_ONLY")
    if decision.bot_name != BOT_BNB_NO and interval in empirical_guard_late_intervals():
        reasons.append(f"V3_EMPIRICAL_GUARD_INTERVAL_{interval}_RESEARCH_ONLY")
    if not reasons:
        return decision
    return replace(
        decision,
        decision_status=RESEARCH_ONLY,
        reason_codes=tuple(decision.reason_codes) + tuple(reasons),
    )


def _with_feed_degraded_stamp(row: Mapping[str, Any]) -> dict[str, Any]:
    """Stamp outgoing Telegram text when a required data feed is stale."""
    out = dict(row)
    try:
        degraded = cycle_watchdog.degraded_feeds()
    except Exception:  # noqa: BLE001 - alert stamping must never block delivery
        logger.debug("v3 degraded-feed stamp skipped", exc_info=True)
        return out
    if not degraded:
        return out
    out["feed_degraded"] = True
    out["degraded_feeds"] = ",".join(degraded)
    existing = str(out.get("reason_codes") or "")
    codes = [code.strip() for code in existing.split(",") if code.strip()]
    for feed in degraded:
        suffix = "".join(ch if ch.isalnum() else "_" for ch in feed.upper()).strip("_")
        code = f"V3_DEGRADED_FEED_{suffix}"
        if code not in codes:
            codes.append(code)
    out["reason_codes"] = ",".join(codes)
    return out


def _maybe_send_auto_mute_notice(
    ledger: StrategyBotLedger,
    row: Mapping[str, Any],
    *,
    bot_name: str,
    notify_enabled: bool,
    header: str | None = None,
) -> None:
    if not notify_enabled:
        return
    key = f"{STRATEGY_VERSION}:{bot_name}:auto_mute_notice"
    try:
        if not ledger.claim_meta_once(key):
            return
        get_telegram().send(
            build_v3_auto_mute_alert(_with_feed_degraded_stamp(row), header=header)
        )
    except Exception:  # noqa: BLE001 - notice must never block tracking
        logger.warning("v3 %s auto-mute notice failed (ignored)", bot_name, exc_info=True)


def _maybe_send_thirteen_m_auto_mute_notice(
    ledger: StrategyBotLedger,
    row: Mapping[str, Any],
) -> None:
    _maybe_send_auto_mute_notice(
        ledger,
        row,
        bot_name=BOT_THIRTEEN_M_SNIPER,
        notify_enabled=thirteen_m_sniper_notify_enabled(),
    )


def record_source_row(
    row: Mapping[str, Any],
    *,
    source_system: str,
    btc_context: Mapping[str, Any] | None = None,
) -> int:
    """Record v3 bot decisions for one existing source row.

    Returns the number of bot rows inserted. All failures are swallowed by design:
    v3 must never break existing V2/HVF alert paths.
    """
    try:
        ledger = get_ledger()
        if ledger is None:
            return 0
        enriched_row = _with_fav_10m_context(
            ledger,
            _with_thirteen_m_sniper_context(
                ledger,
                _enrich_source_row(row, btc_context=btc_context),
            ),
        )
        count = 0
        for decision in decisions_for_row(enriched_row, source_system=source_system, btc_context=btc_context):
            stamped = _with_duplicate_window_guard(ledger, decision, enriched_row)
            stamped = _with_empirical_delivery_guard(stamped, enriched_row)
            row_id = ledger.record_decision(stamped, enriched_row, source_system=source_system)
            if row_id is not None:
                count += 1
                _maybe_notify(ledger, row_id, stamped)
        return count
    except Exception:  # noqa: BLE001 - non-critical side ledger
        logger.warning("v3 strategy-bot record failed (ignored)", exc_info=True)
        return 0


def record_exit_warning_row(row: Mapping[str, Any]) -> int | None:
    """Record + (optionally) alert one confirmed exit-warning flip (Book 1).

    Dedicated path: warn rows only run the warn_flip_entry bot, so the other
    books' populations stay clean. All failures are swallowed by design — this
    must never break the ultoim_v2 warning path that feeds it.
    """
    try:
        ledger = get_ledger()
        if ledger is None:
            return None
        enriched = _with_book_stats_context(
            ledger, row, bot_name=BOT_WARN_FLIP, prefix="warn_flip"
        )
        decision = warn_flip_entry_decision(enriched, source_system="ultoim_v2")
        if decision is None:
            return None
        row_id = ledger.record_decision(decision, enriched, source_system="ultoim_v2")
        if row_id is None:
            return None
        if decision.decision_status != ACCEPTED or not warn_flip_notify_enabled():
            return row_id
        if bool(decision.threshold_profile.get("auto_mute_active")):
            _maybe_send_auto_mute_notice(
                ledger,
                ledger.row_by_id(row_id) or dict(enriched),
                bot_name=BOT_WARN_FLIP,
                notify_enabled=warn_flip_notify_enabled(),
                header="V3 WARN-FLIP ENTRY AUTO-MUTED",
            )
            ledger.mark_notification(
                row_id, status="AUTO_MUTED", message_id=None,
                error="auto_mute_wilson_lb_lt_min",
            )
            return row_id
        recorded = ledger.row_by_id(row_id)
        if recorded is None:
            return row_id
        result = get_telegram().send(build_v3_alert(_with_feed_degraded_stamp(recorded)))
        if result.get("delivered"):
            status, mid = "SENT", result.get("message_id")
        elif result.get("muted"):
            status, mid = "MUTED", None
        else:
            status, mid = "DELIVERY_FAILED", None
        ledger.mark_notification(row_id, status=status, message_id=mid, error=result.get("error"))
        return row_id
    except Exception:  # noqa: BLE001 - non-critical side ledger
        logger.warning("v3 warn-flip record failed (ignored)", exc_info=True)
        return None


def record_top_pick_row(row: Mapping[str, Any]) -> int | None:
    """Record + (optionally) alert the window's single top pick at 13M.

    Display-only book: one row per 15m window (durable claim survives restarts),
    ACCEPTED always, never a trade signal. Failures are swallowed by design.
    """
    try:
        ledger = get_ledger()
        if ledger is None:
            return None
        wk = row.get("window_key")
        if wk is None or not ledger.claim_meta_once(
            f"{STRATEGY_VERSION}:{BOT_TOP_PICK_13M}:{int(wk)}"
        ):
            return None
        enriched = _with_book_stats_context(
            ledger, row, bot_name=BOT_TOP_PICK_13M, prefix="top_pick"
        )
        decision = top_pick_13m_decision(enriched)
        if decision is None:
            return None
        row_id = ledger.record_decision(decision, enriched, source_system="ultoim_v2")
        if row_id is None or not top_pick_notify_enabled():
            return row_id
        recorded = ledger.row_by_id(row_id)
        if recorded is None:
            return row_id
        result = get_telegram().send(build_v3_alert(_with_feed_degraded_stamp(recorded)))
        if result.get("delivered"):
            status, mid = "SENT", result.get("message_id")
        elif result.get("muted"):
            status, mid = "MUTED", None
        else:
            status, mid = "DELIVERY_FAILED", None
        ledger.mark_notification(row_id, status=status, message_id=mid, error=result.get("error"))
        return row_id
    except Exception:  # noqa: BLE001 - non-critical side ledger
        logger.warning("v3 top-pick record failed (ignored)", exc_info=True)
        return None


def record_drift_pick_row(row: Mapping[str, Any]) -> int | None:
    """Record one flow/spread decision and alert only confirmed Drift picks.

    Multi-pick book: dedup is per (window, ticker) — a window can carry several
    qualifying alts and each gets its own decision. The recorder itself stays
    the raw shadow/control. Rejected and inconclusive rows still settle here.
    """
    try:
        ledger = get_ledger()
        if ledger is None:
            return None
        wk = row.get("window_key")
        ticker = str(row.get("ticker") or "")
        if wk is None or not ticker or not ledger.claim_meta_once(
            f"{STRATEGY_VERSION}:{BOT_DRIFT_FLOW_SPREAD}:{int(wk)}:{ticker}"
        ):
            return None
        source = dict(row)
        source.setdefault("delivery_status", "PAPER_DRIFT_FLOW_SPREAD")
        enriched = enrich_spot_depth(source)
        enriched = _enrich_source_row(enriched)
        enriched = _with_book_stats_context(
            ledger,
            enriched,
            bot_name=BOT_DRIFT_FLOW_SPREAD,
            prefix="drift_flow_spread",
        )
        decision = drift_flow_spread_13m_decision(enriched)
        if decision is None:
            return None
        row_id = ledger.record_decision(decision, enriched, source_system="drift_shadow")
        if (
            row_id is None
            or decision.decision_status != ACCEPTED
            or not drift_flow_spread_notify_enabled()
        ):
            return row_id
        recorded = ledger.row_by_id(row_id)
        if recorded is None:
            return row_id
        result = get_telegram().send(build_v3_alert(_with_feed_degraded_stamp(recorded)))
        if result.get("delivered"):
            status, mid = "SENT", result.get("message_id")
        elif result.get("muted"):
            status, mid = "MUTED", None
        else:
            status, mid = "DELIVERY_FAILED", None
        ledger.mark_notification(row_id, status=status, message_id=mid, error=result.get("error"))
        return row_id
    except Exception:  # noqa: BLE001 - non-critical side ledger
        logger.warning("v3 drift-pick record failed (ignored)", exc_info=True)
        return None


def record_drift_checkpoint_row(row: Mapping[str, Any]) -> int | None:
    """Record and optionally notify one Drift add-on or late-qualifier row."""
    try:
        ledger = get_ledger()
        if ledger is None:
            return None
        kind = str(row.get("record_kind") or "")
        if kind == "DRIFT_ADDON_REQUAL":
            bot_name = BOT_DRIFT_ADDON
            decision_fn = drift_addon_requal_decision
            notify = drift_addon_notify_enabled()
        elif kind == "DRIFT_LATEQUAL":
            bot_name = BOT_DRIFT_LATEQUAL
            decision_fn = drift_latequal_decision
            notify = drift_latequal_notify_enabled()
        else:
            return None
        wk = row.get("window_key")
        ticker = str(row.get("ticker") or "")
        if wk is None or not ticker or not ledger.claim_meta_once(
            f"{STRATEGY_VERSION}:{bot_name}:{int(wk)}:{ticker}"
        ):
            return None
        enriched = _enrich_source_row(row)
        decision = decision_fn(enriched)
        if decision is None:
            return None
        row_id = ledger.record_decision(decision, enriched, source_system="drift_shadow")
        if row_id is None or not notify:
            return row_id
        recorded = ledger.row_by_id(row_id)
        if recorded is None:
            return row_id
        result = get_telegram().send(build_v3_alert(_with_feed_degraded_stamp(recorded)))
        if result.get("delivered"):
            status, mid = "SENT", result.get("message_id")
        elif result.get("muted"):
            status, mid = "MUTED", None
        else:
            status, mid = "DELIVERY_FAILED", None
        ledger.mark_notification(row_id, status=status, message_id=mid, error=result.get("error"))
        return row_id
    except Exception:  # noqa: BLE001 - checkpoint tracking must never break capture
        logger.warning("v3 drift checkpoint record failed (ignored)", exc_info=True)
        return None


def record_drift_no_mirror_window(rows: Sequence[Mapping[str, Any]]) -> list[int]:
    """Record filtered NO candidates and send one grouped research card.

    Every candidate remains an independent ledger row for settlement/PnL, but
    Telegram receives at most one compact card per 15-minute window.
    """
    try:
        ledger = get_ledger()
        if ledger is None:
            return []
        row_ids: list[int] = []
        recorded_rows: list[dict[str, Any]] = []
        window_key: int | None = None
        for row in rows:
            wk = row.get("window_key")
            ticker = str(row.get("ticker") or "")
            if wk is None or not ticker:
                continue
            wk_int = int(wk)
            if window_key is None:
                window_key = wk_int
            if wk_int != window_key:
                logger.warning("drift NO group contained multiple windows; skipping %s", ticker)
                continue
            if not ledger.claim_meta_once(
                f"{STRATEGY_VERSION}:{BOT_DRIFT_NO_MIRROR}:{wk_int}:{ticker}"
            ):
                continue
            enriched = _enrich_source_row(row)
            decision = drift_no_mirror_decision(enriched)
            if decision is None:
                continue
            row_id = ledger.record_decision(decision, enriched, source_system="drift_shadow")
            if row_id is None:
                continue
            recorded = ledger.row_by_id(row_id)
            if recorded is None:
                continue
            row_ids.append(row_id)
            recorded_rows.append(recorded)

        if not row_ids or window_key is None or not drift_no_mirror_notify_enabled():
            return row_ids
        if not ledger.claim_meta_once(
            f"{STRATEGY_VERSION}:{BOT_DRIFT_NO_MIRROR}:group-notify:{window_key}"
        ):
            return row_ids
        stamped = [_with_feed_degraded_stamp(row) for row in recorded_rows]
        result = get_telegram().send(build_drift_no_mirror_group_alert(stamped))
        if result.get("delivered"):
            status, mid = "SENT", result.get("message_id")
        elif result.get("muted"):
            status, mid = "MUTED", None
        else:
            status, mid = "DELIVERY_FAILED", None
        for row_id in row_ids:
            ledger.mark_notification(
                row_id,
                status=status,
                message_id=mid,
                error=result.get("error"),
            )
        return row_ids
    except Exception:  # noqa: BLE001 - research mirror must never break capture
        logger.warning("v3 drift NO mirror record failed (ignored)", exc_info=True)
        return []


def record_drift_no_expansion_window(rows: Sequence[Mapping[str, Any]]) -> list[int]:
    """Record every NO-expansion decision and group only accepted alerts."""
    try:
        ledger = get_ledger()
        if ledger is None:
            return []
        row_ids: list[int] = []
        accepted: list[tuple[int, dict[str, Any]]] = []
        window_key: int | None = None
        for row in rows:
            wk = row.get("window_key")
            ticker = str(row.get("ticker") or "")
            if wk is None or not ticker:
                continue
            wk_int = int(wk)
            if window_key is None:
                window_key = wk_int
            if wk_int != window_key:
                logger.warning("drift NO expansion mixed windows; skipping %s", ticker)
                continue
            if not ledger.claim_meta_once(
                f"{STRATEGY_VERSION}:{BOT_DRIFT_NO_EXPANSION}:{wk_int}:{ticker}"
            ):
                continue
            source = dict(row)
            source.setdefault("delivery_status", "PAPER_DRIFT_NO_EXPANSION")
            enriched = enrich_spot_depth(source)
            enriched = _enrich_source_row(enriched)
            enriched = _with_book_stats_context(
                ledger,
                enriched,
                bot_name=BOT_DRIFT_NO_EXPANSION,
                prefix="drift_no_expansion",
            )
            decision = drift_no_expansion_decision(enriched)
            if decision is None:
                continue
            row_id = ledger.record_decision(decision, enriched, source_system="drift_shadow")
            if row_id is None:
                continue
            row_ids.append(row_id)
            if decision.decision_status != ACCEPTED:
                continue
            recorded = ledger.row_by_id(row_id)
            if recorded is not None:
                accepted.append((row_id, recorded))

        if (
            not accepted
            or window_key is None
            or not drift_no_expansion_notify_enabled()
        ):
            return row_ids
        if not ledger.claim_meta_once(
            f"{STRATEGY_VERSION}:{BOT_DRIFT_NO_EXPANSION}:group-notify:{window_key}"
        ):
            return row_ids
        stamped = [_with_feed_degraded_stamp(row) for _, row in accepted]
        result = get_telegram().send(build_drift_no_expansion_group_alert(stamped))
        if result.get("delivered"):
            status, mid = "SENT", result.get("message_id")
        elif result.get("muted"):
            status, mid = "MUTED", None
        else:
            status, mid = "DELIVERY_FAILED", None
        for row_id, _ in accepted:
            ledger.mark_notification(
                row_id,
                status=status,
                message_id=mid,
                error=result.get("error"),
            )
        return row_ids
    except Exception:  # noqa: BLE001 - research expansion must never break capture
        logger.warning("v3 drift NO expansion record failed (ignored)", exc_info=True)
        return []


def send_top_pick_gap_notice(*, window_key: int, close_time: float | None = None) -> bool:
    """One 'NO PICK — data gap' card when a window produced nothing scorable.

    Keeps the owner's hard one-card-per-window cadence visible instead of
    silently skipping. Durable once-per-window claim; failures swallowed.
    """
    try:
        ledger = get_ledger()
        if ledger is None or not top_pick_notify_enabled():
            return False
        if not ledger.claim_meta_once(f"{STRATEGY_VERSION}:{BOT_TOP_PICK_13M}:gap:{int(window_key)}"):
            return False
        parts = [
            "\U0001f3c6 <b>V3 BEST TRADE 13M — NO PICK</b>",
            "No scorable capture reached the 13M mark for this window (data gap).",
            "Cadence guard: this card exists so a silent skip is impossible.",
        ]
        get_telegram().send("\n".join(parts))
        return True
    except Exception:  # noqa: BLE001 - notice must never block anything
        logger.warning("v3 top-pick gap notice failed (ignored)", exc_info=True)
        return False


def owns_source_notification(
    row: Mapping[str, Any],
    *,
    source_system: str,
    btc_context: Mapping[str, Any] | None = None,
) -> bool:
    """Whether V3 should own the operator-facing notification for this source row."""
    try:
        if not enabled():
            return False
        enriched_row = _enrich_source_row(row, btc_context=btc_context)
        asset = str(row.get("asset") or "").upper()
        if _bool("Q15_V3_SUPPRESS_OLD_BNB_NOTIFICATIONS", False) and asset == "BNB":
            return True
        if not suppress_owned_source_notifications():
            return False
        if hvf_wrapper_only_notifications() and source_system != "high_vol_flip":
            return False
        for decision in decisions_for_row(enriched_row, source_system=source_system, btc_context=btc_context):
            decision = _with_empirical_delivery_guard(decision, enriched_row)
            if source_system == "high_vol_flip" and decision.bot_name == BOT_HVF_DEPTH_FLOW:
                return True
            if decision.bot_name == BOT_BASELINE:
                continue
            if decision.decision_status == ACCEPTED:
                return True
            if (
                decision.bot_name in {BOT_BNB_YES_REVERSAL, BOT_CONFIDENCE_TIER}
                and decision.decision_status == RESEARCH_ONLY
            ):
                return True
        return False
    except Exception:  # noqa: BLE001 - fail open: old alert is safer than silence
        logger.debug("v3 owned-notification check failed open", exc_info=True)
        return False


def _maybe_notify(ledger: StrategyBotLedger, row_id: int, decision: BotDecision) -> None:
    try:
        recorded = ledger.row_by_id(row_id)
    except Exception:  # noqa: BLE001 - notification must never block tracking
        logger.warning("v3 strategy-bot notification lookup failed (ignored)", exc_info=True)
        return
    if recorded is None:
        return
    if (
        str(recorded.get("source_system") or "") == "high_vol_flip"
        and decision.bot_name not in {BOT_HVF_DEPTH_FLOW, BOT_DEPTH_FORMULA_15M}
    ):
        return
    reversal_research = (
        decision.bot_name == BOT_BNB_YES_REVERSAL
        and decision.decision_status == RESEARCH_ONLY
    )
    tier_research = (
        decision.bot_name == BOT_CONFIDENCE_TIER
        and decision.decision_status == RESEARCH_ONLY
        and research_telegram_enabled()
    )
    depth_formula_research = (
        decision.bot_name == BOT_DEPTH_FORMULA_15M
        and decision.decision_status == RESEARCH_ONLY
        and depth_formula_telegram_enabled()
    )
    thirteen_m_sniper_alert = (
        decision.bot_name == BOT_THIRTEEN_M_SNIPER
        and decision.decision_status == ACCEPTED
    )
    if thirteen_m_sniper_alert and not thirteen_m_sniper_notify_enabled():
        return
    if thirteen_m_sniper_alert and bool(decision.threshold_profile.get("auto_mute_active")):
        _maybe_send_thirteen_m_auto_mute_notice(ledger, recorded)
        try:
            ledger.mark_notification(
                row_id,
                status="AUTO_MUTED",
                message_id=None,
                error="auto_mute_wilson_lb_lt_min",
            )
        except Exception:  # noqa: BLE001 - notification status is best-effort
            logger.debug("v3 13M sniper auto-mute mark failed", exc_info=True)
        return
    fav_10m_alert = (
        decision.bot_name == BOT_FAV_10M
        and decision.decision_status == ACCEPTED
    )
    if fav_10m_alert and not fav_10m_notify_enabled():
        return
    if fav_10m_alert and bool(decision.threshold_profile.get("auto_mute_active")):
        _maybe_send_auto_mute_notice(
            ledger,
            recorded,
            bot_name=BOT_FAV_10M,
            notify_enabled=fav_10m_notify_enabled(),
            header="V3 FAVORITE 10M AUTO-MUTED",
        )
        try:
            ledger.mark_notification(
                row_id,
                status="AUTO_MUTED",
                message_id=None,
                error="auto_mute_wilson_lb_lt_min",
            )
        except Exception:  # noqa: BLE001 - notification status is best-effort
            logger.debug("v3 fav_10m auto-mute mark failed", exc_info=True)
        return
    if (
        hvf_wrapper_only_notifications()
        and decision.bot_name not in {BOT_HVF_DEPTH_FLOW, BOT_THIRTEEN_M_SNIPER, BOT_FAV_10M}
        and not depth_formula_research
    ):
        return
    if (
        decision.bot_name == BOT_BASELINE
        or (
            decision.decision_status != ACCEPTED
            and not reversal_research
            and not tier_research
            and not depth_formula_research
        )
    ):
        return
    try:
        result = get_telegram().send(build_v3_alert(_with_feed_degraded_stamp(recorded)))
        if result.get("delivered"):
            status, mid = "SENT", result.get("message_id")
        elif result.get("muted"):
            status, mid = "MUTED", None
        else:
            status, mid = "DELIVERY_FAILED", None
        ledger.mark_notification(
            row_id,
            status=status,
            message_id=mid,
            error=result.get("error"),
        )
    except Exception:  # noqa: BLE001 - notification must never block tracking
        logger.warning("v3 strategy-bot notification failed (ignored)", exc_info=True)


def resolve(
    *,
    source_system: str,
    source_model_version: str,
    ticker: str,
    official_result: str,
    now: float | None = None,
) -> int:
    try:
        ledger = get_ledger()
        if ledger is None:
            return 0
        return ledger.resolve(
            source_system=source_system,
            source_model_version=source_model_version,
            ticker=ticker,
            official_result=official_result,
            now=now,
        )
    except Exception:  # noqa: BLE001 - non-critical side ledger
        logger.warning("v3 strategy-bot resolve failed (ignored)", exc_info=True)
        return 0


def reconcile_drift_settlements(events: Any) -> int:
    """Backfill or grade Drift strategy rows from the authoritative Drift ledger."""
    total = 0
    if not events:
        return total
    for event in events:
        if not isinstance(event, Mapping):
            continue
        model_version = str(event.get("model_version") or "")
        ticker = str(event.get("ticker") or "")
        result = str(event.get("official_result") or "").upper()
        if not model_version or not ticker or result not in {"YES", "NO"}:
            continue
        total += resolve(
            source_system="drift_shadow",
            source_model_version=model_version,
            ticker=ticker,
            official_result=result,
            now=event.get("resolved_at"),
        )
    return total


def scoreboard() -> dict[str, Any]:
    ledger = get_ledger()
    if ledger is None:
        return {"available": False, "strategy_version": STRATEGY_VERSION, "enabled": False}
    return ledger.scoreboard(STRATEGY_VERSION)
