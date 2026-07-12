"""Best-effort runtime hooks for the v3 filtered alert system."""
from __future__ import annotations

from dataclasses import replace
import json
import logging
import math
import os
import threading
import time
from typing import Any, Mapping, Sequence

import cycle_watchdog

from ..lineage import lineage_stamp
from .btc_regime import enrich_btc_regime
from .drift_evidence import enrich_drift_evidence
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
    BOT_DRIFT_ACCURACY_V91,
    BOT_DRIFT_ASYMMETRIC_VOLUME,
    BOT_DRIFT_BALANCED_V95,
    BOT_DRIFT_FLOW_SPREAD,
    BOT_DRIFT_ADDON,
    BOT_DRIFT_LATEQUAL,
    BOT_DRIFT_NO_EXPANSION,
    BOT_DRIFT_NO_MIRROR,
    BOT_THIRTEEN_M_SNIPER,
    BOT_PRECISION_13M,
    BOT_TOP_PICK_13M,
    BOT_WARN_FLIP,
    REJECTED,
    RESEARCH_ONLY,
    STRATEGY_VERSION,
    BotDecision,
    decisions_for_row,
    drift_addon_requal_decision,
    drift_accuracy_v91_shadow_decision,
    drift_asymmetric_volume_shadow_decision,
    drift_balanced_v95_shadow_decision,
    drift_flow_spread_13m_decision,
    drift_flow_spread_shadow_flow15_decision,
    drift_flow_spread_shadow_spread4_decision,
    drift_latequal_decision,
    drift_no_expansion_decision,
    drift_no_mirror_decision,
    precision13_sized_decision,
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
_drift_outbox: Any | None = None
_drift_outbox_identity: tuple[int, str] | None = None
_drift_reconcile_lock = threading.Lock()
_thirteen_m_stats_warning_logged = False
_thirteen_m_flow_warning_logged = False

DRIFT_DECISION_FEATURE_SCHEMA_VERSION = "drift-decision-evidence-v1"


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _drift_lineage_config() -> dict[str, Any]:
    """Allow-listed policy/config inputs only; never hash credentials."""
    return {
        "accepted_assets": ["DOGE", "HYPE", "SOL", "XRP"],
        "ask_min_cents": 60.0,
        "ask_max_cents": 73.0,
        "core_distance_max": 3e-5,
        "asymmetric_distance_max": 1e-4,
        "asymmetric_distance_ask_min": 65.0,
        "flip_max": 30.0,
        "spread_max_cents": 2.0,
        "v91_full_path_yes_fraction_min": 0.75,
        "v95_required_side": "YES",
        "review_bars": [30, 60, 150],
        "spot_snapshot_max_age_seconds": os.environ.get(
            "Q15_V3_DRIFT_SPOT_SNAPSHOT_MAX_AGE_SECONDS", "15"
        ),
        "spot_book_max_age_seconds": os.environ.get(
            "Q15_V3_DRIFT_SPOT_BOOK_MAX_AGE_SECONDS", "15"
        ),
        "spot_trade_max_age_seconds": os.environ.get(
            "Q15_V3_DRIFT_SPOT_TRADE_MAX_AGE_SECONDS", "15"
        ),
    }


def _drift_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return "{}"


def _drift_num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _with_drift_lineage_and_grade(row: Mapping[str, Any]) -> dict[str, Any]:
    """Stamp one point-in-time Drift row without manufacturing missing evidence."""
    out = dict(row)
    stamp = lineage_stamp(
        feature_schema_version=DRIFT_DECISION_FEATURE_SCHEMA_VERSION,
        config=_drift_lineage_config(),
    )
    out.update(stamp)
    out.setdefault("source_captured_at", out.get("created_at"))
    out["evidence_as_of"] = out.get("created_at")

    availability = out.get("drift_evidence_availability")
    if not isinstance(availability, Mapping):
        availability = {}
    availability = {str(key): value for key, value in availability.items()}
    ages = out.get("drift_evidence_ages")
    if not isinstance(ages, Mapping):
        ages = {}
    ages = {str(key): value for key, value in ages.items()}
    index_status = str(out.get("index_status") or "missing").lower()
    availability["settlement_index"] = {
        "available": index_status == "ok",
        "status": index_status,
        "missing_reason": out.get("index_missing_reason"),
        "index_id": out.get("index_id"),
        "source_at": out.get("index_source_ts"),
    }
    ages["settlement_index"] = out.get("index_age_s")
    kalshi_status = str(out.get("kalshi_depth_status") or "missing").lower()
    availability["kalshi_13m"] = {
        "available": kalshi_status == "ok",
        "status": kalshi_status,
        "missing_reason": out.get("kalshi_depth_missing_reason"),
        "retry_used": out.get("kalshi_depth_retry_used"),
    }
    ages["kalshi_quote"] = out.get("quote_age_seconds")
    spot_status = str(out.get("spot_depth_status") or "missing").lower()
    availability["spot_current"] = {
        "available": spot_status == "ok",
        "status": spot_status,
        "missing_reason": out.get("spot_depth_missing_reason"),
    }
    ages["spot_snapshot"] = out.get("spot_depth_snapshot_age_seconds")
    ages["spot_book"] = out.get("spot_depth_age_seconds")
    ages["spot_trade"] = out.get("spot_depth_trade_age_seconds")
    out["drift_evidence_availability"] = availability
    out["drift_evidence_ages"] = ages
    out["feature_availability_json"] = _drift_json(availability)
    out["feature_age_json"] = _drift_json(ages)

    ask = _drift_num(out.get("entry_ask_cents"))
    distance = _drift_num(out.get("distance_sigma"))
    flip = _drift_num(out.get("flip_probability"))
    spread = _drift_num(out.get("spread_cents"))
    core_complete = all(value is not None for value in (ask, distance, flip, spread))
    out["data_complete"] = bool(core_complete)

    v91 = _drift_num(out.get("drift_v91_yes_fraction_all"))
    v95_side = str(out.get("drift_v95_15m_side") or "").upper()
    v95_flow = _drift_num(out.get("drift_v95_15m_flow_score"))
    btc_side = str(out.get("drift_btc_15m_side") or "").upper()
    breadth = out.get("drift_asymmetric_breadth")
    flow_coverage = _drift_num(out.get("drift_flow_coverage"))
    full_complete = (
        core_complete
        and bool(out.get("drift_evidence_complete"))
        and v91 is not None
        and v95_side in {"YES", "NO"}
        and v95_flow is not None
        and btc_side in {"YES", "NO"}
        and breadth is not None
        and flow_coverage is not None
        and flow_coverage >= 1.0
    )
    out["full_feature_complete"] = bool(full_complete)
    spot_available = spot_status in {
        "ok", "stale", "missing"
    }
    if full_complete:
        cohort = "FULL_FEATURE"
    elif spot_available:
        cohort = "FLOW_ENABLED"
    else:
        cohort = "CORE_ONLY"
    out["feature_cohort"] = cohort

    reasons: list[str] = []
    if not full_complete:
        grade = "INCOMPLETE"
        reasons.append("DRIFT_FULL_FEATURES_INCOMPLETE")
    else:
        v91_pass = bool(v91 is not None and v91 >= 0.75)
        v95_pass = v95_side == "YES"
        btc_contradicts = str(out.get("asset") or "").upper() in {"SOL", "XRP"} and btc_side == "NO"
        if v91_pass:
            reasons.append("DRIFT_V91_FULL_PATH_PASS")
        else:
            reasons.append("DRIFT_V91_FULL_PATH_LOW")
        if v95_pass:
            reasons.append("DRIFT_V95_15M_AGREES")
        else:
            reasons.append("DRIFT_V95_15M_CONTRADICTS")
        if btc_contradicts:
            reasons.append("DRIFT_BTC_15M_CONTRADICTS")
        grade = "A" if v91_pass and v95_pass and not btc_contradicts else (
            "B" if (v91_pass or v95_pass) and not btc_contradicts else "C"
        )
    out["evidence_grade"] = grade
    out["evidence_reason_codes"] = ",".join(reasons)
    evidence_bundle = {
        "as_of": out.get("evidence_as_of"),
        "availability": dict(availability),
        "ages": dict(ages),
        "grade": grade,
        "reason_codes": reasons,
        "v91_yes_fraction_all": v91,
        "v95_15m_side": v95_side or None,
        "v95_15m_flow_score": v95_flow,
        "btc_15m_side": btc_side or None,
        "core_breadth": out.get("drift_core_breadth"),
        "asymmetric_breadth": breadth,
        "flow": {
            "1m": out.get("drift_flow_1m"),
            "3m": out.get("drift_flow_3m"),
            "5m": out.get("drift_flow_5m"),
            "13m": out.get("drift_flow_13m"),
            "positive_bucket_fraction": out.get("drift_flow_positive_bucket_fraction"),
            "sign_flips": out.get("drift_flow_sign_flips"),
            "persistence": out.get("drift_flow_persistence"),
            "coverage": flow_coverage,
        },
    }
    out["drift_evidence"] = evidence_bundle
    out["drift_evidence_json"] = _drift_json(evidence_bundle)
    return out


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


def precision13_notify_enabled() -> bool:
    return _bool("Q15_V3_PRECISION_13M_NOTIFY", True)


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


def drift_outbox_enabled() -> bool:
    """Explicit opt-in for the dedicated Drift delivery outbox."""
    return _bool("Q15_V3_DRIFT_OUTBOX_ENABLED", False)


def drift_outbox_path() -> str:
    return (
        os.environ.get("Q15_V3_DRIFT_OUTBOX_DB")
        or "data/q15_drift_telegram_outbox.sqlite3"
    )


def reset_drift_outbox() -> None:
    """Close and drop the optional outbox (test/reconfiguration hook)."""
    global _drift_outbox, _drift_outbox_identity
    current = _drift_outbox
    _drift_outbox = None
    _drift_outbox_identity = None
    if current is not None:
        try:
            current.close()
        except Exception:  # noqa: BLE001 - shutdown must remain best-effort
            logger.debug("v3 Drift outbox close failed", exc_info=True)


def get_drift_outbox() -> Any | None:
    """Return the dedicated retry outbox only under explicit opt-in.

    The V3 Telegram gate/credentials remain authoritative.  When that channel
    is muted we preserve the normal MUTED result instead of accumulating rows
    that can never be delivered.
    """
    global _drift_outbox, _drift_outbox_identity
    if not drift_outbox_enabled():
        if _drift_outbox is not None:
            reset_drift_outbox()
        return None
    telegram = get_telegram()
    if not bool(getattr(telegram, "enabled", False)):
        return None
    path = drift_outbox_path()
    identity = (id(telegram), path)
    if _drift_outbox is not None and _drift_outbox_identity == identity:
        return _drift_outbox
    reset_drift_outbox()
    try:
        from notifications.outbox_v9 import ReliableTelegramOutbox

        _drift_outbox = ReliableTelegramOutbox(
            None, telegram, sqlite_path=path,
        )
        _drift_outbox_identity = identity
    except Exception:  # noqa: BLE001 - optional rail must never block recording
        logger.warning("v3 Drift outbox unavailable; delivery remains fail-closed", exc_info=True)
        _drift_outbox = None
        _drift_outbox_identity = None
    return _drift_outbox


def initialize_drift_outbox() -> bool:
    """Start the optional Drift retry worker during application startup.

    No database or worker is created unless both the dedicated outbox and the
    credentialed V3 Telegram channel are enabled.
    """
    if not enabled() or not telegram_enabled() or not drift_outbox_enabled():
        return False
    telegram = get_telegram()
    if not bool(getattr(telegram, "enabled", False)):
        return False
    if get_drift_outbox() is None:
        return False
    # Credit terminal outcomes left by the previous process immediately.  The
    # worker may also finish a pending attempt concurrently; the periodic pass
    # in the live loop will pick up that transition without a source replay.
    summary = reconcile_drift_delivery_statuses()
    if summary["updated"]:
        logger.info(
            "reconciled %s Drift delivery outcomes at startup",
            summary["updated"],
        )
    return True


def _drift_delivery_key(
    bot_name: str,
    window_key: int,
    ticker: str | None = None,
    *,
    grouped: bool = False,
    strategy_version: str = STRATEGY_VERSION,
) -> str:
    scope = "group" if grouped else "row"
    parts = [str(strategy_version), "drift", str(bot_name), scope, str(int(window_key))]
    if ticker:
        parts.append(str(ticker))
    return ":".join(parts)


def _stored_drift_delivery_key(row: Mapping[str, Any]) -> str | None:
    """Rebuild the deterministic outbox key from one persisted decision."""
    bot_name = str(row.get("bot_name") or "")
    try:
        window_key = int(row.get("window_key"))
    except (TypeError, ValueError):
        return None
    grouped = bot_name in {BOT_DRIFT_NO_EXPANSION, BOT_DRIFT_NO_MIRROR}
    ticker = None if grouped else str(row.get("ticker") or "")
    if not bot_name or (not grouped and not ticker):
        return None
    return _drift_delivery_key(
        bot_name,
        window_key,
        ticker,
        grouped=grouped,
        strategy_version=str(row.get("strategy_version") or STRATEGY_VERSION),
    )


def reconcile_drift_delivery_statuses(limit: int = 100) -> dict[str, int]:
    """Copy terminal dedicated-outbox states into the strategy ledger.

    This is intentionally a local, read-mostly maintenance pass: it never
    creates an outbox, enqueues a payload, or calls Telegram.  Work is bounded
    by ``limit`` and terminal updates are committed as one ledger transaction,
    making the callable safe for startup, the live loop, or a health poll.
    """
    summary = {
        "scanned": 0,
        "keys_checked": 0,
        "updated": 0,
        "sent": 0,
        "expired": 0,
        "dead_letter": 0,
        "busy": 0,
    }
    if not _drift_reconcile_lock.acquire(blocking=False):
        summary["busy"] = 1
        return summary
    try:
        # Do not call get_drift_outbox() here: constructing it can start its
        # network worker.  Reconciliation only observes an already-initialized
        # outbox and therefore cannot itself send anything.
        outbox = _drift_outbox
        ledger = get_ledger()
        if outbox is None or ledger is None:
            return summary
        rows = ledger.drift_notifications_to_reconcile(limit=limit)
        summary["scanned"] = len(rows)
        status_cache: dict[str, str | None] = {}
        updates: list[tuple[int, str, str | None]] = []
        for row in rows:
            key = _stored_drift_delivery_key(row)
            if key is None:
                continue
            if key not in status_cache:
                status_cache[key] = outbox.status_by_key(key)
            terminal = str(status_cache[key] or "").upper()
            if terminal not in {"SENT", "EXPIRED", "DEAD_LETTER"}:
                continue
            error = None if terminal == "SENT" else f"outbox:{terminal}"
            updates.append((int(row["id"]), terminal, error))
            summary[terminal.lower()] += 1
        summary["keys_checked"] = len(status_cache)
        summary["updated"] = ledger.reconcile_drift_notification_terminals(updates)
        return summary
    except Exception:  # noqa: BLE001 - maintenance must never block the live loop
        logger.warning("v3 Drift delivery reconciliation failed (ignored)", exc_info=True)
        return summary
    finally:
        _drift_reconcile_lock.release()


def _normalize_delivery_result(result: Any) -> dict[str, Any]:
    if isinstance(result, Mapping):
        return {
            "ok": bool(result.get("ok")),
            "delivered": bool(result.get("delivered")),
            "muted": bool(result.get("muted")),
            "message_id": result.get("message_id"),
            "error": result.get("error"),
        }
    delivered = bool(result)
    return {
        "ok": delivered,
        "delivered": delivered,
        "muted": False,
        "message_id": None,
        "error": None if delivered else "telegram_send_failed",
    }


def _send_drift_notification(
    text: str,
    *,
    idempotency_key: str,
    expires_at: float | None,
) -> dict[str, Any]:
    """Send directly or enqueue the exact rendered Drift payload for retry."""
    if expires_at is None:
        return {
            "ok": False,
            "delivered": False,
            "muted": False,
            "message_id": None,
            "error": "drift_expiry_missing",
        }
    try:
        expiry = float(expires_at)
    except (TypeError, ValueError):
        expiry = float("nan")
    if not math.isfinite(expiry):
        return {
            "ok": False,
            "delivered": False,
            "muted": False,
            "message_id": None,
            "error": "drift_expiry_invalid",
        }
    outbox = get_drift_outbox()
    if outbox is None:
        # Explicit outbox opt-in is a nonblocking safety contract.  Construction
        # failure must not silently turn the live refresh loop into a network
        # caller; a later replay can recover after the rail becomes available.
        if drift_outbox_enabled():
            return {
                "ok": False,
                "delivered": False,
                "muted": False,
                "message_id": None,
                "error": "outbox_unavailable",
            }
        if time.time() >= expiry:
            return {
                "ok": False,
                "delivered": False,
                "muted": False,
                "message_id": None,
                "error": "outbox:EXPIRED",
            }
        telegram = get_telegram()
        sender = getattr(telegram, "send_with_result", None)
        try:
            raw = sender(text) if callable(sender) else telegram.send(text)
        except Exception as exc:  # noqa: BLE001 - delivery cannot break recording
            return {
                "ok": False,
                "delivered": False,
                "muted": False,
                "message_id": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
        return _normalize_delivery_result(raw)

    # The live refresh loop is a producer only.  Persist now; the outbox worker
    # performs all network I/O so a slow Telegram request cannot stall capture.
    raw_result = outbox.enqueue_with_result(
        text, idempotency_key=idempotency_key, expires_at=expiry,
    )
    result = _normalize_delivery_result(raw_result)
    outbox_status = (
        raw_result.get("outbox_status")
        if isinstance(raw_result, Mapping)
        else None
    ) or outbox.status_by_key(idempotency_key)
    result["outbox_status"] = outbox_status
    if (
        not result.get("delivered")
        and not result.get("muted")
        and not result.get("error")
        and outbox_status
    ):
        result["error"] = f"outbox:{outbox_status}"
    return result


def _delivery_fields(result: Mapping[str, Any]) -> tuple[str, int | None, str | None]:
    if result.get("delivered") or result.get("outbox_status") == "SENT":
        return "SENT", result.get("message_id"), result.get("error")
    if result.get("muted"):
        return "MUTED", None, result.get("error")
    if result.get("outbox_status") in {"PENDING", "SENDING", "FAILED_RETRYABLE"}:
        return "QUEUED_RETRY", None, result.get("error")
    if result.get("outbox_status") in {"EXPIRED", "DEAD_LETTER"}:
        terminal = str(result["outbox_status"])
        return terminal, None, result.get("error") or f"outbox:{terminal}"
    return "DELIVERY_FAILED", None, result.get("error")


def _stored_decision(
    ledger: StrategyBotLedger,
    decision: BotDecision,
    source_row: Mapping[str, Any],
    *,
    source_system: str,
) -> tuple[int | None, dict[str, Any] | None]:
    """Insert a decision or retrieve its durable duplicate for recovery."""
    row_id = ledger.record_decision(
        decision, source_row, source_system=source_system,
    )
    if row_id is not None:
        return row_id, ledger.row_by_id(row_id)
    return None, ledger.row_for_decision(
        decision, source_row, source_system=source_system,
    )


def _notification_needs_delivery(
    ledger: StrategyBotLedger,
    row: Mapping[str, Any],
    *,
    idempotency_key: str,
) -> bool:
    """Return whether a stored row needs enqueue, reconciling worker outcomes."""
    status = row.get("notification_status")
    if status in {"SENT", "MUTED"}:
        return False
    if status == "QUEUED_RETRY":
        outbox = get_drift_outbox()
        if outbox is None:
            return False
        outbox_status = outbox.status_by_key(idempotency_key)
        if outbox_status == "SENT":
            ledger.mark_notification(
                int(row["id"]), status="SENT", message_id=None, error=None,
            )
        elif outbox_status in {"DEAD_LETTER", "EXPIRED"}:
            ledger.mark_notification(
                int(row["id"]),
                status=outbox_status,
                message_id=None,
                error=f"outbox:{outbox_status}",
            )
        # A queued row never creates a second delivery attempt from replay.  Its
        # deterministic outbox record (or terminal state) remains authoritative.
        return False
    return status is None or status == "DELIVERY_FAILED"


def _group_expiry(rows: Sequence[Mapping[str, Any]]) -> float | None:
    """Earliest settlement boundary, failing closed if any row lacks one."""
    expiries: list[float] = []
    for row in rows:
        try:
            value = float(row.get("close_time"))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value):
            return None
        expiries.append(value)
    return min(expiries) if expiries else None


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
    threshold_rule_version: str | None = None,
    decision_status: str = ACCEPTED,
) -> Mapping[str, Any]:
    """Inject a bot's resolved ACCEPTED stats so its rules can self-govern
    (empirical EV + auto-mute), mirroring the 13M sniper convention."""
    out = dict(row)
    try:
        if decision_status == ACCEPTED:
            stats = ledger.bot_accepted_resolved_stats(
                bot_name=bot_name,
                threshold_rule_version=threshold_rule_version,
            )
        else:
            stats = ledger.bot_resolved_stats(
                bot_name=bot_name,
                decision_status=decision_status,
                threshold_rule_version=threshold_rule_version,
            )
        out.setdefault(f"{prefix}_resolved_n", stats.get("n"))
        out.setdefault(f"{prefix}_correct", stats.get("correct"))
        out.setdefault(f"{prefix}_accuracy", stats.get("accuracy"))
        out.setdefault(f"{prefix}_wilson_lb", stats.get("wilson_lb"))
        out.setdefault(f"{prefix}_net_pnl_cents", stats.get("net_pnl_cents"))
        out.setdefault(f"{prefix}_wins", stats.get("correct"))
        out.setdefault(f"{prefix}_total_pnl_cents", stats.get("net_pnl_cents"))
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
        if wk is None or not ticker:
            return None
        source = dict(row)
        source.setdefault("delivery_status", "PAPER_DRIFT_FLOW_SPREAD")
        enriched = enrich_spot_depth(source)
        enriched = _enrich_source_row(enriched)
        enriched = enrich_drift_evidence(enriched)
        enriched = _with_drift_lineage_and_grade(enriched)
        enriched = _with_book_stats_context(
            ledger,
            enriched,
            bot_name=BOT_DRIFT_FLOW_SPREAD,
            prefix="drift_flow_spread",
            threshold_rule_version="drift-flow-spread-13m-frozen-v2",
        )
        for shadow_bot, prefix in (
            (BOT_DRIFT_ASYMMETRIC_VOLUME, "asymmetric_volume"),
            (BOT_DRIFT_BALANCED_V95, "balanced_v95"),
            (BOT_DRIFT_ACCURACY_V91, "accuracy_v91"),
        ):
            enriched = _with_book_stats_context(
                ledger,
                enriched,
                bot_name=shadow_bot,
                prefix=prefix,
                threshold_rule_version="drift-evidence-policy-v1",
                decision_status=RESEARCH_ONLY,
            )
        decision = drift_flow_spread_13m_decision(enriched)
        if decision is None:
            return None
        row_id, recorded = _stored_decision(
            ledger, decision, enriched, source_system="drift_shadow",
        )
        # These cohorts deliberately reuse the exact same point-in-time
        # enrichment and can never notify. They measure more research volume
        # without changing accepted exposure or the returned core identity.
        for shadow_decision_fn in (
            drift_flow_spread_shadow_spread4_decision,
            drift_flow_spread_shadow_flow15_decision,
            drift_asymmetric_volume_shadow_decision,
            drift_balanced_v95_shadow_decision,
            drift_accuracy_v91_shadow_decision,
        ):
            try:
                shadow_decision = shadow_decision_fn(enriched)
                if shadow_decision is not None:
                    _stored_decision(
                        ledger,
                        shadow_decision,
                        enriched,
                        source_system="drift_shadow",
                    )
            except Exception:  # noqa: BLE001 - shadows cannot block core delivery
                logger.warning("v3 Drift counterfactual record failed (ignored)", exc_info=True)
        if (
            recorded is None
            or decision.decision_status != ACCEPTED
            or recorded.get("decision_status") != ACCEPTED
            or not drift_flow_spread_notify_enabled()
        ):
            return row_id
        delivery_key = _drift_delivery_key(
            BOT_DRIFT_FLOW_SPREAD, int(wk), ticker,
        )
        if not _notification_needs_delivery(
            ledger, recorded, idempotency_key=delivery_key,
        ):
            return row_id
        payload = build_v3_alert(_with_feed_degraded_stamp(recorded))
        result = _send_drift_notification(
            payload,
            idempotency_key=delivery_key,
            expires_at=recorded.get("close_time"),
        )
        status, mid, error = _delivery_fields(result)
        ledger.mark_notification(
            int(recorded["id"]), status=status, message_id=mid, error=error,
        )
        return row_id
    except Exception:  # noqa: BLE001 - non-critical side ledger
        logger.warning("v3 drift-pick record failed (ignored)", exc_info=True)
        return None


def record_precision13_row(row: Mapping[str, Any]) -> int | None:
    """Record and notify one independently deduplicated precision 13M signal."""
    try:
        ledger = get_ledger()
        if ledger is None:
            return None
        wk = row.get("window_key")
        ticker = str(row.get("ticker") or "")
        if wk is None or not ticker:
            return None
        decision = precision13_sized_decision(row)
        if decision is None:
            return None
        row_id, recorded = _stored_decision(
            ledger, decision, row, source_system="precision13_shadow",
        )
        if (
            recorded is None
            or decision.decision_status != ACCEPTED
            or not precision13_notify_enabled()
        ):
            return row_id
        delivery_key = (
            f"{STRATEGY_VERSION}:precision13:{BOT_PRECISION_13M}:"
            f"row:{int(wk)}:{ticker}"
        )
        if not _notification_needs_delivery(
            ledger, recorded, idempotency_key=delivery_key,
        ):
            return row_id
        result = get_telegram().send(
            build_v3_alert(_with_feed_degraded_stamp(recorded))
        )
        if result.get("delivered"):
            status, message_id = "SENT", result.get("message_id")
        elif result.get("muted"):
            status, message_id = "MUTED", None
        else:
            status, message_id = "DELIVERY_FAILED", None
        ledger.mark_notification(
            int(recorded["id"]),
            status=status,
            message_id=message_id,
            error=result.get("error"),
        )
        return row_id
    except Exception:  # noqa: BLE001 - precision delivery cannot break capture
        logger.warning("v3 precision13 notification failed (ignored)", exc_info=True)
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
        if wk is None or not ticker:
            return None
        enriched = _enrich_source_row(row)
        decision = decision_fn(enriched)
        if decision is None:
            return None
        row_id, recorded = _stored_decision(
            ledger, decision, enriched, source_system="drift_shadow",
        )
        if (
            recorded is None
            or decision.decision_status != ACCEPTED
            or recorded.get("decision_status") != ACCEPTED
            or not notify
        ):
            return row_id
        delivery_key = _drift_delivery_key(bot_name, int(wk), ticker)
        if not _notification_needs_delivery(
            ledger, recorded, idempotency_key=delivery_key,
        ):
            return row_id
        payload = build_v3_alert(_with_feed_degraded_stamp(recorded))
        result = _send_drift_notification(
            payload,
            idempotency_key=delivery_key,
            expires_at=recorded.get("close_time"),
        )
        status, mid, error = _delivery_fields(result)
        ledger.mark_notification(
            int(recorded["id"]), status=status, message_id=mid, error=error,
        )
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
        seen_record_ids: set[int] = set()
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
            enriched = _enrich_source_row(row)
            decision = drift_no_mirror_decision(enriched)
            if decision is None:
                continue
            row_id, recorded = _stored_decision(
                ledger, decision, enriched, source_system="drift_shadow",
            )
            if row_id is not None:
                row_ids.append(row_id)
            if recorded is None:
                continue
            record_id = int(recorded["id"])
            if record_id not in seen_record_ids:
                seen_record_ids.add(record_id)
                recorded_rows.append(recorded)

        if (
            not recorded_rows
            or window_key is None
            or not drift_no_mirror_notify_enabled()
        ):
            return row_ids
        delivery_key = _drift_delivery_key(
            BOT_DRIFT_NO_MIRROR, window_key, grouped=True,
        )
        pending_rows = [
            row
            for row in recorded_rows
            if _notification_needs_delivery(
                ledger, row, idempotency_key=delivery_key,
            )
        ]
        if not pending_rows:
            return row_ids
        stamped = [_with_feed_degraded_stamp(row) for row in pending_rows]
        payload = build_drift_no_mirror_group_alert(stamped)
        result = _send_drift_notification(
            payload,
            idempotency_key=delivery_key,
            expires_at=_group_expiry(pending_rows),
        )
        status, mid, error = _delivery_fields(result)
        for recorded in pending_rows:
            ledger.mark_notification(
                int(recorded["id"]),
                status=status,
                message_id=mid,
                error=error,
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
        accepted: list[dict[str, Any]] = []
        seen_accepted_ids: set[int] = set()
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
            row_id, recorded = _stored_decision(
                ledger, decision, enriched, source_system="drift_shadow",
            )
            if row_id is not None:
                row_ids.append(row_id)
            if recorded is None or recorded.get("decision_status") != ACCEPTED:
                continue
            if int(recorded["id"]) not in seen_accepted_ids:
                seen_accepted_ids.add(int(recorded["id"]))
                accepted.append(recorded)

        if (
            not accepted
            or window_key is None
            or not drift_no_expansion_notify_enabled()
        ):
            return row_ids
        delivery_key = _drift_delivery_key(
            BOT_DRIFT_NO_EXPANSION, window_key, grouped=True,
        )
        pending_rows = [
            row
            for row in accepted
            if _notification_needs_delivery(
                ledger, row, idempotency_key=delivery_key,
            )
        ]
        if not pending_rows:
            return row_ids
        stamped = [_with_feed_degraded_stamp(row) for row in pending_rows]
        payload = build_drift_no_expansion_group_alert(stamped)
        result = _send_drift_notification(
            payload,
            idempotency_key=delivery_key,
            expires_at=_group_expiry(pending_rows),
        )
        status, mid, error = _delivery_fields(result)
        for recorded in pending_rows:
            ledger.mark_notification(
                int(recorded["id"]),
                status=status,
                message_id=mid,
                error=error,
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
