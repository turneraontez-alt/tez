"""Best-effort runtime hooks for the v3 filtered alert system."""
from __future__ import annotations

from dataclasses import replace
import logging
import os
from typing import Any, Mapping

from .ledger import StrategyBotLedger
from .rules import (
    ACCEPTED,
    BOT_BASELINE,
    BOT_HYPE_YES,
    REJECTED,
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


def db_path() -> str:
    return os.environ.get("Q15_STRATEGY_BOTS_DB") or "data/q15_strategy_bots_v3.sqlite3"


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
        count = 0
        for decision in decisions_for_row(row, source_system=source_system, btc_context=btc_context):
            stamped = _with_duplicate_window_guard(ledger, decision, row)
            row_id = ledger.record_decision(stamped, row, source_system=source_system)
            if row_id is not None:
                count += 1
                _maybe_notify(ledger, row_id, stamped)
        return count
    except Exception:  # noqa: BLE001 - non-critical side ledger
        logger.warning("v3 strategy-bot record failed (ignored)", exc_info=True)
        return 0


def _maybe_notify(ledger: StrategyBotLedger, row_id: int, decision: BotDecision) -> None:
    if decision.bot_name == BOT_BASELINE or decision.decision_status != ACCEPTED:
        return
    try:
        recorded = ledger.row_by_id(row_id)
        if recorded is None:
            return
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
