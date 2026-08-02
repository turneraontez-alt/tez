"""Outcome-blind, idempotent V11 readiness notice.

This tool reads only the frozen feature projection.  It never selects an
outcome column, fits or scores a model, creates an artifact, promotes a rule,
or places an order.  Once the non-BTC 60-window gate is genuinely complete it
queues one administrative PAPER notice through the durable Telegram outbox.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from notifications.outbox_v9 import ReliableTelegramOutbox
from q15_upgrade.strategy_bots import rti_microstructure_v11 as v11
from q15_upgrade.strategy_bots.telegram import V3Telegram
from tools.q15_rti_feature_coverage_audit import build_report
from tools.q15_rti_microstructure_freeze import (
    _feature_runtime,
    load_feature_rows_after,
)
from tools.q15_rti_microstructure_preregister import (
    DEFAULT_DB,
    build_readiness,
    design_fingerprint,
    validate_design,
)


NOTICE_VERSION = "q15-v11-non-btc-readiness-notice-v1"
MINIMUM_WINDOWS = 60
COHORT = "NON_BTC_TRANSFER"
DEFAULT_DESIGN = ROOT / "config" / "q15_rti_microstructure_design_v11.json"
IDEMPOTENCY_KEY = (
    f"{NOTICE_VERSION}:{COHORT}:{MINIMUM_WINDOWS}:{v11.DESIGN_SHA256}"
)


@dataclass(frozen=True)
class _DisabledStore:
    enabled: bool = False


def build_outcome_blind_snapshot(
    *, design_path: Path = DEFAULT_DESIGN, database_path: Path = DEFAULT_DB,
) -> dict[str, Any]:
    design = json.loads(design_path.read_text(encoding="utf-8"))
    if not isinstance(design, Mapping):
        raise ValueError("v11_readiness_design_root_not_object")
    validate_design(design)
    if (
        design.get("design_id") != v11.DESIGN_ID
        or design_fingerprint(design) != v11.DESIGN_SHA256
    ):
        raise ValueError("v11_readiness_design_binding_mismatch")
    # This loader uses the audited feature-only SQL projection.  Outcome and
    # P/L columns are not selected from SQLite.
    feature_rows = load_feature_rows_after(
        database_path, float(design["prospective_after_close_time"]),
    )
    coverage = build_report(
        feature_rows, source_schema=str(design.get("source_schema") or ""),
    )
    coverage.update(
        _feature_runtime(design).model_feature_window_coverage(feature_rows)
    )
    readiness = build_readiness(design, coverage)
    cohort = dict(readiness.get("cohorts", {}).get(COHORT, {}))
    return {
        "notice_version": NOTICE_VERSION,
        "design_id": design["design_id"],
        "design_sha256": v11.DESIGN_SHA256,
        "cohort": COHORT,
        "complete_executable_close_windows": int(
            readiness.get("complete_microstructure_close_windows") or 0
        ),
        "minimum_complete_close_windows": int(
            cohort.get("minimum_complete_close_windows") or 0
        ),
        "windows_remaining": int(cohort.get("windows_remaining") or 0),
        "ready_for_locked_freeze": bool(
            cohort.get("ready_for_locked_freeze")
        ),
        "coverage_clean": bool(readiness.get("coverage_clean")),
        "timestamp_alignment_failures": int(
            readiness.get("model_feature_timestamp_failures") or 0
        ),
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "artifact_emitted": False,
        "automatic_scoring": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }


def snapshot_is_notice_ready(snapshot: Mapping[str, Any]) -> bool:
    return bool(
        snapshot.get("design_id") == v11.DESIGN_ID
        and snapshot.get("design_sha256") == v11.DESIGN_SHA256
        and snapshot.get("cohort") == COHORT
        and int(snapshot.get("minimum_complete_close_windows") or 0)
        == MINIMUM_WINDOWS
        and int(snapshot.get("complete_executable_close_windows") or 0)
        >= MINIMUM_WINDOWS
        and int(snapshot.get("windows_remaining") or 0) == 0
        and snapshot.get("ready_for_locked_freeze") is True
        and snapshot.get("coverage_clean") is True
        and int(snapshot.get("timestamp_alignment_failures") or 0) == 0
        and snapshot.get("outcome_columns_selected") is False
        and snapshot.get("outcome_labels_read") is False
        and snapshot.get("model_fit_performed") is False
        and snapshot.get("artifact_emitted") is False
        and snapshot.get("automatic_scoring") is False
        and snapshot.get("automatic_promotion") is False
        and snapshot.get("real_trading_allowed") is False
    )


def readiness_message(snapshot: Mapping[str, Any]) -> str:
    windows = int(snapshot["complete_executable_close_windows"])
    return "\n".join((
        "<b>V3 V11 NON-BTC AUDIT READY | PAPER ADMIN</b>",
        f"Complete clean seven-asset windows: {windows}/{MINIMUM_WINDOWS}",
        "Cohort: ETH, SOL, XRP, DOGE, BNB, HYPE (BTC remains separate)",
        "Outcome labels: SEALED / unread",
        "Model fit: NOT RUN",
        "Untouched test: NOT SCORED",
        "Artifact/promotion/trading: DISABLED",
        "Action required: manual one-shot walk-forward and untouched-test audit.",
        f"Design: <code>{v11.DESIGN_ID}</code>",
        "This is a readiness notice, not a trade signal.",
    ))


def send_notice_if_ready(
    snapshot: Mapping[str, Any],
    sender: Callable[..., Mapping[str, Any]],
    *,
    now: float | None = None,
) -> dict[str, Any]:
    if not snapshot_is_notice_ready(snapshot):
        return {
            "status": "WAITING_FOR_COMPLETE_WINDOWS",
            "notice_attempted": False,
            "idempotency_key": IDEMPOTENCY_KEY,
        }
    current = time.time() if now is None else float(now)
    result = dict(sender(
        readiness_message(snapshot),
        idempotency_key=IDEMPOTENCY_KEY,
        expires_at=current + 30.0 * 86400.0,
    ))
    return {
        "status": "READY_NOTICE_ACCEPTED" if result.get("ok") else (
            "READY_NOTICE_FAILED"
        ),
        "notice_attempted": True,
        "idempotency_key": IDEMPOTENCY_KEY,
        "delivery": result,
    }


def _default_sender() -> Callable[..., Mapping[str, Any]]:
    raw = V3Telegram()
    outbox = ReliableTelegramOutbox(
        _DisabledStore(),
        raw,
        sqlite_path=os.environ.get(
            "Q15_V9_OUTBOX_SQLITE_PATH", "data/q15_telegram_outbox.sqlite3",
        ),
    )
    return outbox.send_with_result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--design", default=str(DEFAULT_DESIGN))
    parser.add_argument("--strategy-db", default=str(DEFAULT_DB))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report readiness without constructing or calling Telegram.",
    )
    args = parser.parse_args()
    snapshot = build_outcome_blind_snapshot(
        design_path=Path(args.design), database_path=Path(args.strategy_db),
    )
    if args.dry_run:
        result = {
            "status": (
                "READY_MANUAL_AUDIT_REQUIRED"
                if snapshot_is_notice_ready(snapshot)
                else "WAITING_FOR_COMPLETE_WINDOWS"
            ),
            "notice_attempted": False,
            "idempotency_key": IDEMPOTENCY_KEY,
        }
    else:
        result = send_notice_if_ready(snapshot, _default_sender())
    print(json.dumps({**snapshot, **result}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
