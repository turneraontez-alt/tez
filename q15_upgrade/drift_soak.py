"""Read-only operational soak audit for the paper-only Drift system.

The audit intentionally opens every SQLite database with ``mode=ro`` and never
imports the live ledger/outbox classes (their constructors run migrations).
It measures only operational correctness; it does not score strategy returns or
change any strategy, delivery, or execution state.
"""
from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


WINDOW_SECONDS = 900
DRIFT_MARK_SECONDS = 780
MARK_OFFSET_SECONDS = WINDOW_SECONDS - DRIFT_MARK_SECONDS
DEFAULT_ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
FRESHNESS_GATED_BOTS = ("drift_flow_spread_13m", "drift_no_expansion")

DEFAULT_MINIMUM_HOURS = 24.0
DEFAULT_TARGET_HOURS = 48.0
DEFAULT_COVERAGE_PERCENT = 98.0
DEFAULT_FRESHNESS_MAX_SECONDS = 15.0
DEFAULT_CYCLE_MAX_SECONDS = 90.0
DEFAULT_MARK_FINALIZATION_SECONDS = 90.0
DEFAULT_SETTLEMENT_GRACE_SECONDS = 900.0

_LOG_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:,\d{3})?")
_SLOW_CYCLE = re.compile(r"\bSlow cycle:\s*([0-9]+(?:\.[0-9]+)?)s\b")


class SoakAuditError(ValueError):
    """Invalid audit input (as opposed to an unavailable read-only source)."""


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def parse_epoch(value: str | int | float) -> float:
    """Accept a Unix epoch or an ISO-8601 timestamp and return seconds UTC."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        out = float(value)
    else:
        text = str(value).strip()
        try:
            out = float(text)
        except ValueError:
            normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError as exc:
                raise SoakAuditError(
                    "start must be a Unix epoch or ISO-8601 timestamp"
                ) from exc
            if parsed.tzinfo is None:
                raise SoakAuditError("ISO-8601 start timestamps must include a timezone")
            out = parsed.timestamp()
    if not math.isfinite(out) or out < 0:
        raise SoakAuditError("start epoch must be a finite non-negative number")
    return out


def expected_window_keys(
    start_epoch: float,
    now_epoch: float,
    *,
    finalization_seconds: float = DEFAULT_MARK_FINALIZATION_SECONDS,
) -> list[int]:
    """Return 15-minute windows whose 13M mark is mature in the audit range.

    A market closing at the end of bucket ``k`` reaches its 13M mark 120 seconds
    after the bucket starts.  We wait ``finalization_seconds`` after that mark so
    the bounded cross-asset barrier can complete before coverage is judged.
    """
    start = float(start_epoch)
    now = float(now_epoch)
    finalization = max(0.0, float(finalization_seconds))
    if now < start:
        raise SoakAuditError("now_epoch cannot be earlier than start_epoch")
    first = math.ceil((start - MARK_OFFSET_SECONDS) / WINDOW_SECONDS)
    last = math.floor(
        (now - finalization - MARK_OFFSET_SECONDS) / WINDOW_SECONDS
    )
    if last < first:
        return []
    return list(range(int(first), int(last) + 1))


def _connect_read_only(path: str | Path) -> sqlite3.Connection:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(str(source))
    conn = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _unavailable(path: str | Path, exc: BaseException) -> dict[str, Any]:
    return {
        "available": False,
        "path": str(Path(path)),
        "error": f"{type(exc).__name__}: {exc}",
    }


def audit_capture_coverage(
    db_path: str | Path,
    *,
    start_epoch: float,
    now_epoch: float,
    model_version: str = "interval-research-v1",
    expected_assets: Sequence[str] = DEFAULT_ASSETS,
    finalization_seconds: float = DEFAULT_MARK_FINALIZATION_SECONDS,
) -> dict[str, Any]:
    """Audit complete, real 13M captures across the expected seven assets."""
    assets = tuple(dict.fromkeys(str(asset).upper() for asset in expected_assets))
    if not assets:
        raise SoakAuditError("expected_assets cannot be empty")
    keys = expected_window_keys(
        start_epoch, now_epoch, finalization_seconds=finalization_seconds
    )
    base: dict[str, Any] = {
        "path": str(Path(db_path)),
        "model_version": model_version,
        "expected_assets": list(assets),
        "mark_finalization_seconds": float(finalization_seconds),
        "expected_windows": len(keys),
        "expected_window_keys": keys,
    }
    try:
        with _connect_read_only(db_path) as conn:
            rows: list[sqlite3.Row] = []
            if keys:
                placeholders = ",".join("?" for _ in keys)
                rows = conn.execute(
                    "SELECT window_key, asset, predicted_side, missing_reason "
                    "FROM interval_captures WHERE model_version=? AND interval='13M' "
                    "AND captured_at>=? AND window_key IN (" + placeholders + ")",
                    (str(model_version), float(start_epoch), *keys),
                ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        return {**base, **_unavailable(db_path, exc)}

    expected = set(assets)
    captured_by_window: dict[int, set[str]] = {key: set() for key in keys}
    accounted_by_window: dict[int, set[str]] = {key: set() for key in keys}
    missing_reason_counts: Counter[str] = Counter()
    explicit_missing_slots: set[tuple[int, str]] = set()
    for row in rows:
        key = int(row["window_key"])
        asset = str(row["asset"] or "").upper()
        if key not in captured_by_window or asset not in expected:
            continue
        accounted_by_window[key].add(asset)
        if row["predicted_side"] is not None:
            captured_by_window[key].add(asset)
        else:
            explicit_missing_slots.add((key, asset))
            missing_reason_counts[str(row["missing_reason"] or "UNSPECIFIED")] += 1

    incomplete: list[dict[str, Any]] = []
    absent_rows = 0
    missing_rows = 0
    captured_rows = 0
    complete_windows = 0
    for key in keys:
        captured = captured_by_window[key]
        accounted = accounted_by_window[key]
        captured_rows += len(captured)
        missing_assets = sorted(expected - captured)
        absent_assets = sorted(expected - accounted)
        absent_rows += len(absent_assets)
        missing_rows += len(missing_assets)
        if not missing_assets:
            complete_windows += 1
        else:
            incomplete.append(
                {
                    "window_key": key,
                    "mark_at": _iso(key * WINDOW_SECONDS + MARK_OFFSET_SECONDS),
                    "captured_assets": sorted(captured),
                    "missing_assets": missing_assets,
                    "absent_assets": absent_assets,
                }
            )

    expected_windows = len(keys)
    expected_rows = expected_windows * len(assets)
    window_coverage = (
        100.0 if expected_windows == 0 else 100.0 * complete_windows / expected_windows
    )
    row_coverage = 100.0 if expected_rows == 0 else 100.0 * captured_rows / expected_rows
    return {
        **base,
        "available": True,
        "complete_windows": complete_windows,
        "incomplete_windows_count": len(incomplete),
        "complete_window_coverage_percent": round(window_coverage, 4),
        "expected_rows": expected_rows,
        "captured_rows": captured_rows,
        "missing_rows": missing_rows,
        "explicit_missing_rows": len(explicit_missing_slots),
        "absent_rows": absent_rows,
        "row_coverage_percent": round(row_coverage, 4),
        "missing_reason_counts": dict(sorted(missing_reason_counts.items())),
        "incomplete_windows": incomplete,
    }


def audit_effective_freshness(
    db_path: str | Path,
    *,
    start_epoch: float,
    max_age_seconds: float = DEFAULT_FRESHNESS_MAX_SECONDS,
    bot_names: Sequence[str] = FRESHNESS_GATED_BOTS,
) -> dict[str, Any]:
    """Find accepted freshness-gated Drift rows that escaped the effective gate."""
    bots = tuple(dict.fromkeys(str(name) for name in bot_names))
    base = {
        "path": str(Path(db_path)),
        "max_age_seconds": float(max_age_seconds),
        "bot_names": list(bots),
    }
    try:
        with _connect_read_only(db_path) as conn:
            placeholders = ",".join("?" for _ in bots)
            rows = conn.execute(
                "SELECT id, created_at, bot_name, asset, ticker, spot_depth_status, "
                "spot_depth_age_seconds, spot_depth_trade_age_seconds "
                "FROM strategy_bot_decisions WHERE source_system='drift_shadow' "
                "AND decision_status='ACCEPTED' AND created_at>=? AND bot_name IN ("
                + placeholders
                + ") ORDER BY id",
                (float(start_epoch), *bots),
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        return {**base, **_unavailable(db_path, exc)}

    limit = float(max_age_seconds)
    violations: list[dict[str, Any]] = []
    for row in rows:
        status = str(row["spot_depth_status"] or "").lower()
        book_age = row["spot_depth_age_seconds"]
        trade_age = row["spot_depth_trade_age_seconds"]
        reasons: list[str] = []
        if status != "ok":
            reasons.append("status_not_ok")
        if book_age is None:
            reasons.append("book_age_missing")
        elif float(book_age) > limit:
            reasons.append("book_age_over_limit")
        if trade_age is None:
            reasons.append("trade_age_missing")
        elif float(trade_age) > limit:
            reasons.append("trade_age_over_limit")
        if reasons:
            violations.append(
                {
                    "id": int(row["id"]),
                    "created_at": row["created_at"],
                    "bot_name": row["bot_name"],
                    "asset": row["asset"],
                    "ticker": row["ticker"],
                    "spot_depth_status": row["spot_depth_status"],
                    "effective_book_age_seconds": book_age,
                    "effective_trade_age_seconds": trade_age,
                    "reasons": reasons,
                }
            )
    return {
        **base,
        "available": True,
        "accepted_rows_audited": len(rows),
        "violations": len(violations),
        "violation_rows": violations,
    }


def audit_drift_outbox(
    db_path: str | Path, *, start_epoch: float
) -> dict[str, Any]:
    """Count Drift delivery outcomes without recovering or modifying the outbox."""
    base = {"path": str(Path(db_path))}
    try:
        with _connect_read_only(db_path) as conn:
            rows = conn.execute(
                "SELECT id, status, created_at, expires_at, last_error "
                "FROM q15_telegram_outbox WHERE created_at>=? ORDER BY id",
                (float(start_epoch),),
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        return {**base, **_unavailable(db_path, exc)}

    counts = Counter(str(row["status"] or "UNKNOWN").upper() for row in rows)
    terminal_failure_statuses = {"DEAD_LETTER", "EXPIRED"}
    pending_statuses = {"PENDING", "SENDING", "FAILED_RETRYABLE"}
    known_statuses = terminal_failure_statuses | pending_statuses | {"SENT"}
    failures = [dict(row) for row in rows if str(row["status"] or "").upper() in terminal_failure_statuses]
    pending = [dict(row) for row in rows if str(row["status"] or "").upper() in pending_statuses]
    unknown = [dict(row) for row in rows if str(row["status"] or "").upper() not in known_statuses]
    return {
        **base,
        "available": True,
        "messages": len(rows),
        "status_counts": dict(sorted(counts.items())),
        "sent": counts.get("SENT", 0),
        "pending": len(pending),
        "terminal_failures": len(failures),
        "unknown_statuses": len(unknown),
        "failure_rows": failures,
        "pending_rows": pending,
        "unknown_rows": unknown,
    }


def audit_overdue_settlements(
    db_path: str | Path,
    *,
    start_epoch: float,
    now_epoch: float,
    grace_seconds: float = DEFAULT_SETTLEMENT_GRACE_SECONDS,
) -> dict[str, Any]:
    """Find post-start Drift decisions still ungraded after their grace period."""
    base = {"path": str(Path(db_path)), "grace_seconds": float(grace_seconds)}
    try:
        with _connect_read_only(db_path) as conn:
            rows = conn.execute(
                "SELECT id, created_at, bot_name, decision_status, asset, ticker, close_time "
                "FROM strategy_bot_decisions WHERE source_system='drift_shadow' "
                "AND created_at>=? AND official_result IS NULL AND ("
                "(close_time IS NOT NULL AND close_time+?<=?) OR "
                "(close_time IS NULL AND created_at+900+?<=?)) ORDER BY id",
                (
                    float(start_epoch),
                    float(grace_seconds),
                    float(now_epoch),
                    float(grace_seconds),
                    float(now_epoch),
                ),
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        return {**base, **_unavailable(db_path, exc)}
    overdue = [dict(row) for row in rows]
    return {
        **base,
        "available": True,
        "overdue_decisions": len(overdue),
        "overdue_tickers": len({str(row["ticker"]) for row in rows}),
        "rows": overdue,
    }


def _line_epoch(line: str) -> float | None:
    match = _LOG_TIMESTAMP.match(line)
    if match is None:
        return None
    try:
        # The app logger writes local wall time.  ``timestamp()`` on the naive
        # parsed value asks the host timezone database for the offset at that
        # date (including DST), unlike reusing today's fixed UTC offset.
        parsed = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
        return parsed.timestamp()
    except ValueError:
        return None


def audit_cycle_log(
    log_path: str | Path,
    *,
    start_epoch: float,
    provided_max_seconds: float | None = None,
    log_start_slack_seconds: float = 60.0,
) -> dict[str, Any]:
    """Read max full-cycle duration from the current process log or an override."""
    if provided_max_seconds is not None:
        value = float(provided_max_seconds)
        if not math.isfinite(value) or value < 0:
            raise SoakAuditError("provided cycle max must be finite and non-negative")
        return {
            "available": True,
            "path": str(Path(log_path)),
            "source": "provided",
            "max_cycle_seconds": value,
            "slow_cycle_samples": None,
            "log_covers_start": None,
        }

    path = Path(log_path)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return _unavailable(path, exc)

    first_epoch: float | None = None
    last_epoch: float | None = None
    durations: list[float] = []
    for line in lines:
        line_epoch = _line_epoch(line)
        if line_epoch is not None:
            first_epoch = line_epoch if first_epoch is None else min(first_epoch, line_epoch)
            last_epoch = line_epoch if last_epoch is None else max(last_epoch, line_epoch)
        match = _SLOW_CYCLE.search(line)
        if match is not None and line_epoch is not None and line_epoch >= start_epoch:
            durations.append(float(match.group(1)))

    covers_start = bool(
        first_epoch is not None
        and first_epoch <= float(start_epoch) + max(0.0, float(log_start_slack_seconds))
    )
    if not covers_start:
        return {
            "available": False,
            "path": str(path),
            "source": "log",
            "error": "current log does not cover the requested soak start",
            "first_log_at": _iso(first_epoch),
            "last_log_at": _iso(last_epoch),
            "log_covers_start": False,
            "slow_cycle_samples": len(durations),
        }
    # Every full cycle above the watchdog threshold is logged. No matching line
    # therefore proves the max stayed below that (10s by default), safely below
    # the soak's 90s ceiling.
    return {
        "available": True,
        "path": str(path),
        "source": "log",
        "max_cycle_seconds": max(durations, default=0.0),
        "slow_cycle_samples": len(durations),
        "first_log_at": _iso(first_epoch),
        "last_log_at": _iso(last_epoch),
        "log_covers_start": True,
    }


def _check(
    *, available: bool, passed: bool, value: Any, threshold: Any, detail: str
) -> dict[str, Any]:
    return {
        "available": bool(available),
        "passed": bool(available and passed),
        "value": value,
        "threshold": threshold,
        "detail": detail,
    }


def build_soak_report(
    *,
    start_epoch: float,
    now_epoch: float,
    interval_db_path: str | Path,
    strategy_db_path: str | Path,
    outbox_db_path: str | Path,
    cycle_log_path: str | Path,
    model_version: str = "interval-research-v1",
    expected_assets: Sequence[str] = DEFAULT_ASSETS,
    provided_cycle_max_seconds: float | None = None,
    minimum_hours: float = DEFAULT_MINIMUM_HOURS,
    target_hours: float = DEFAULT_TARGET_HOURS,
    coverage_threshold_percent: float = DEFAULT_COVERAGE_PERCENT,
    freshness_max_seconds: float = DEFAULT_FRESHNESS_MAX_SECONDS,
    cycle_threshold_seconds: float = DEFAULT_CYCLE_MAX_SECONDS,
    mark_finalization_seconds: float = DEFAULT_MARK_FINALIZATION_SECONDS,
    settlement_grace_seconds: float = DEFAULT_SETTLEMENT_GRACE_SECONDS,
) -> dict[str, Any]:
    """Build the complete JSON-serializable operational soak report."""
    start = float(start_epoch)
    now = float(now_epoch)
    if now < start:
        raise SoakAuditError("now_epoch cannot be earlier than start_epoch")

    capture = audit_capture_coverage(
        interval_db_path,
        start_epoch=start,
        now_epoch=now,
        model_version=model_version,
        expected_assets=expected_assets,
        finalization_seconds=mark_finalization_seconds,
    )
    freshness = audit_effective_freshness(
        strategy_db_path,
        start_epoch=start,
        max_age_seconds=freshness_max_seconds,
    )
    delivery = audit_drift_outbox(outbox_db_path, start_epoch=start)
    settlements = audit_overdue_settlements(
        strategy_db_path,
        start_epoch=start,
        now_epoch=now,
        grace_seconds=settlement_grace_seconds,
    )
    cycles = audit_cycle_log(
        cycle_log_path,
        start_epoch=start,
        provided_max_seconds=provided_cycle_max_seconds,
    )

    coverage_value = capture.get("complete_window_coverage_percent")
    freshness_value = freshness.get("violations")
    delivery_value = (
        None
        if not delivery.get("available")
        else int(delivery.get("terminal_failures", 0))
        + int(delivery.get("unknown_statuses", 0))
    )
    settlement_value = settlements.get("overdue_decisions")
    cycle_value = cycles.get("max_cycle_seconds")
    checks = {
        "capture_coverage": _check(
            available=bool(capture.get("available")),
            passed=coverage_value is not None
            and float(coverage_value) >= float(coverage_threshold_percent),
            value=coverage_value,
            threshold=f">={float(coverage_threshold_percent):g}%",
            detail="complete 7-asset 13M windows / expected mature 13M marks",
        ),
        "effective_freshness": _check(
            available=bool(freshness.get("available")),
            passed=freshness_value == 0,
            value=freshness_value,
            threshold="0 violations",
            detail="accepted freshness-gated Drift decisions only",
        ),
        "delivery_failures": _check(
            available=bool(delivery.get("available")),
            passed=delivery_value == 0,
            value=delivery_value,
            threshold="0 terminal/unknown failures",
            detail="DEAD_LETTER and EXPIRED are terminal delivery failures; pending is reported separately",
        ),
        "overdue_settlements": _check(
            available=bool(settlements.get("available")),
            passed=settlement_value == 0,
            value=settlement_value,
            threshold="0 overdue decisions",
            detail=f"official result missing more than {float(settlement_grace_seconds):g}s after close",
        ),
        "max_cycle": _check(
            available=bool(cycles.get("available")),
            passed=cycle_value is not None
            and float(cycle_value) <= float(cycle_threshold_seconds),
            value=cycle_value,
            threshold=f"<={float(cycle_threshold_seconds):g}s",
            detail="maximum full refresh cycle from the current process log or supplied metric",
        ),
    }
    all_checks_passed = all(check["passed"] for check in checks.values())
    elapsed_seconds = max(0.0, now - start)
    elapsed_hours = elapsed_seconds / 3600.0
    if elapsed_hours < float(minimum_hours):
        status = "COLLECTING"
    else:
        status = "PASS" if all_checks_passed else "FAIL"

    return {
        "version": "drift-soak-v1",
        "read_only": True,
        "paper_only_audit": True,
        "status": status,
        "all_checks_passed_now": all_checks_passed,
        "start_epoch": start,
        "start_at": _iso(start),
        "generated_at_epoch": now,
        "generated_at": _iso(now),
        "elapsed_seconds": round(elapsed_seconds, 3),
        "elapsed_hours": round(elapsed_hours, 4),
        "minimum_hours": float(minimum_hours),
        "target_hours": float(target_hours),
        "minimum_remaining_hours": round(
            max(0.0, float(minimum_hours) - elapsed_hours), 4
        ),
        "target_remaining_hours": round(
            max(0.0, float(target_hours) - elapsed_hours), 4
        ),
        "target_status": "TARGET_MET"
        if elapsed_hours >= float(target_hours)
        else "TARGET_PENDING",
        "checks": checks,
        "capture": capture,
        "freshness": freshness,
        "delivery": delivery,
        "settlements": settlements,
        "cycles": cycles,
    }


def format_soak_report(report: dict[str, Any]) -> str:
    """Render a compact operator-readable report."""
    status = report.get("status", "UNKNOWN")
    lines = [
        f"Drift operational soak: {status}",
        (
            f"Elapsed {report.get('elapsed_hours', 0):.2f}h; "
            f"minimum {report.get('minimum_hours', 24):g}h; "
            f"target {report.get('target_hours', 48):g}h"
        ),
        f"Range {report.get('start_at')} -> {report.get('generated_at')}",
    ]
    for name, check in report.get("checks", {}).items():
        marker = "PASS" if check.get("passed") else "FAIL"
        if not check.get("available"):
            marker = "UNKNOWN"
        lines.append(
            f"[{marker}] {name}: {check.get('value')} (required {check.get('threshold')})"
        )
    capture = report.get("capture", {})
    lines.append(
        "Capture: "
        f"{capture.get('complete_windows', 0)}/{capture.get('expected_windows', 0)} "
        f"complete windows; {capture.get('captured_rows', 0)}/"
        f"{capture.get('expected_rows', 0)} rows; "
        f"{capture.get('missing_rows', 0)} missing"
    )
    delivery = report.get("delivery", {})
    lines.append(
        "Delivery: "
        f"{delivery.get('sent', 0)} sent; {delivery.get('pending', 0)} pending; "
        f"{delivery.get('terminal_failures', 0)} terminal failures"
    )
    if status == "COLLECTING":
        lines.append(
            f"Verdict unlocks in {report.get('minimum_remaining_hours', 0):.2f}h; "
            f"48h target in {report.get('target_remaining_hours', 0):.2f}h."
        )
    return "\n".join(lines)
