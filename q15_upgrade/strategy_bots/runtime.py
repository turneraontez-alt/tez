"""Best-effort runtime hooks for the v3 filtered alert system."""
from __future__ import annotations

from dataclasses import replace
import logging
import os
from typing import Any, Mapping

from .btc_regime import enrich_btc_regime
from .kraken_l3_depth import enrich_kraken_l3
from .l2_depth import enrich_coinbase_l2
from .ledger import StrategyBotLedger
from .rules import (
    ACCEPTED,
    BOT_BASELINE,
    BOT_BNB_NO,
    BOT_BNB_YES_REVERSAL,
    BOT_CONFIDENCE_TIER,
    BOT_DEPTH_FORMULA_15M,
    BOT_HVF_DEPTH_FLOW,
    BOT_HYPE_YES,
    REJECTED,
    RESEARCH_ONLY,
    STRATEGY_VERSION,
    BotDecision,
    decisions_for_row,
    source_side,
)
from .telegram import V3Telegram, build_v3_alert

logger = logging.getLogger("strategy_bots.runtime")

_ledger: StrategyBotLedger | None = None
_telegram: V3Telegram | None = None


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
        or decision.bot_name == BOT_BASELINE
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
        enriched_row = _enrich_source_row(row, btc_context=btc_context)
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
    if hvf_wrapper_only_notifications() and decision.bot_name != BOT_HVF_DEPTH_FLOW and not depth_formula_research:
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
        result = get_telegram().send(build_v3_alert(recorded))
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


def scoreboard() -> dict[str, Any]:
    ledger = get_ledger()
    if ledger is None:
        return {"available": False, "strategy_version": STRATEGY_VERSION, "enabled": False}
    return ledger.scoreboard(STRATEGY_VERSION)
