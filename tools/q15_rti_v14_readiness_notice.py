"""Outcome-blind, idempotent V14 milestone notices."""
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
from q15_upgrade.strategy_bots import rti_microstructure_v14 as v14
from q15_upgrade.strategy_bots.telegram import V3Telegram
from tools.q15_rti_feature_coverage_audit import build_report
from tools.q15_rti_microstructure_feature_audit import soft_input_integrity
from tools.q15_rti_microstructure_freeze import load_feature_rows
from tools.q15_rti_microstructure_preregister import (
    DEFAULT_DB,
    build_readiness,
    design_fingerprint,
    validate_design,
)


NOTICE_VERSION = "q15-v14-readiness-milestone-notice-v1"
DEFAULT_DESIGN = ROOT / "config" / "q15_rti_microstructure_design_v14.json"
MILESTONES = {
    "GEOMETRY_30": {
        "complete_windows": 30,
        "cohort": None,
        "headline": "OUTCOME-BLIND GEOMETRY REVIEW READY",
        "action": "Run the locked 30-window V14 geometry/integrity audit; do not open outcomes.",
    },
    "NON_BTC_60": {
        "complete_windows": 60,
        "cohort": "NON_BTC_TRANSFER",
        "headline": "NON-BTC NESTED WALK-FORWARD AUDIT READY",
        "action": "Run the manual locked nested-trust non-BTC audit.",
    },
    "BTC_150": {
        "complete_windows": 150,
        "cohort": "BTC",
        "headline": "BTC NESTED WALK-FORWARD AUDIT READY",
        "action": "Run the manual locked nested-trust BTC audit; keep test labels sealed until prior gates pass.",
    },
}
IDEMPOTENCY_KEYS = {
    name: f"{NOTICE_VERSION}:{name}:{raw['complete_windows']}:{v14.DESIGN_SHA256}"
    for name, raw in MILESTONES.items()
}


@dataclass(frozen=True)
class _DisabledStore:
    enabled: bool = False


def build_outcome_blind_snapshot(
    *, design_path: Path = DEFAULT_DESIGN, database_path: Path = DEFAULT_DB,
) -> dict[str, Any]:
    design = json.loads(design_path.read_text(encoding="utf-8"))
    if not isinstance(design, Mapping):
        raise ValueError("v14_readiness_design_root_not_object")
    validate_design(design)
    if design.get("design_id") != v14.DESIGN_ID or (
        design_fingerprint(design) != v14.DESIGN_SHA256
    ):
        raise ValueError("v14_readiness_design_binding_mismatch")
    rows = load_feature_rows(database_path)
    coverage = build_report(
        rows, source_schema=str(design.get("source_schema") or ""),
    )
    model_coverage = v14.model_feature_window_coverage(rows)
    coverage.update(model_coverage)
    readiness = build_readiness(design, coverage)
    complete_times = {
        float(value)
        for value in model_coverage["model_feature_complete_close_times"]
    }
    examples = []
    for row in rows:
        try:
            close_time = float(row.get("close_time"))
        except (TypeError, ValueError):
            continue
        if close_time not in complete_times:
            continue
        vector = v14.feature_vector(row)
        if not vector.get("available"):
            raise ValueError("v14_readiness_complete_row_became_unavailable")
        examples.append({
            "id": int(row["id"]),
            "asset": str(row.get("asset") or "").upper(),
            "close_time": close_time,
            "features": [float(value) for value in vector["features"]],
        })
    integrity = soft_input_integrity(examples, v14.FEATURE_NAMES)
    cohorts = dict(readiness.get("cohorts") or {})
    return {
        "notice_version": NOTICE_VERSION,
        "design_id": v14.DESIGN_ID,
        "design_sha256": v14.DESIGN_SHA256,
        "evaluation_protocol_sha256": v14.EVALUATION_PROTOCOL_SHA256,
        "reporting_protocol_sha256": v14.REPORTING_PROTOCOL_SHA256,
        "calibration_reporting_protocol_sha256": (
            v14.CALIBRATION_REPORTING_PROTOCOL_SHA256
        ),
        "selective_value_curve_protocol_sha256": (
            v14.SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256
        ),
        "complete_executable_close_windows": int(
            readiness.get("complete_microstructure_close_windows") or 0
        ),
        "coverage_clean": bool(readiness.get("coverage_clean")),
        "timestamp_alignment_failures": int(
            readiness.get("model_feature_timestamp_failures") or 0
        ),
        "soft_input_integrity": integrity,
        "cohort_readiness": {
            cohort: {
                "minimum_complete_close_windows": int(
                    dict(raw).get("minimum_complete_close_windows") or 0
                ),
                "windows_remaining": int(dict(raw).get("windows_remaining") or 0),
                "ready_for_locked_freeze": bool(
                    dict(raw).get("ready_for_locked_freeze")
                ),
            }
            for cohort, raw in cohorts.items()
        },
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "artifact_emitted": False,
        "automatic_scoring": False,
        "automatic_promotion": False,
        "notification_is_trade_signal": False,
        "real_trading_allowed": False,
    }


def _snapshot_safe(snapshot: Mapping[str, Any]) -> bool:
    return bool(
        snapshot.get("notice_version") == NOTICE_VERSION
        and snapshot.get("design_id") == v14.DESIGN_ID
        and snapshot.get("design_sha256") == v14.DESIGN_SHA256
        and snapshot.get("evaluation_protocol_sha256")
        == v14.EVALUATION_PROTOCOL_SHA256
        and snapshot.get("reporting_protocol_sha256")
        == v14.REPORTING_PROTOCOL_SHA256
        and snapshot.get("calibration_reporting_protocol_sha256")
        == v14.CALIBRATION_REPORTING_PROTOCOL_SHA256
        and snapshot.get("selective_value_curve_protocol_sha256")
        == v14.SELECTIVE_VALUE_CURVE_PROTOCOL_SHA256
        and snapshot.get("coverage_clean") is True
        and int(snapshot.get("timestamp_alignment_failures") or 0) == 0
        and snapshot.get("outcome_columns_selected") is False
        and snapshot.get("outcome_labels_read") is False
        and snapshot.get("model_fit_performed") is False
        and snapshot.get("artifact_emitted") is False
        and snapshot.get("automatic_scoring") is False
        and snapshot.get("automatic_promotion") is False
        and snapshot.get("notification_is_trade_signal") is False
        and snapshot.get("real_trading_allowed") is False
    )


def milestone_is_ready(snapshot: Mapping[str, Any], milestone: str) -> bool:
    definition = MILESTONES.get(milestone)
    if definition is None or not _snapshot_safe(snapshot):
        return False
    required = int(definition["complete_windows"])
    if int(snapshot.get("complete_executable_close_windows") or 0) < required:
        return False
    cohort = definition.get("cohort")
    if cohort is None:
        return True
    raw = dict(dict(snapshot.get("cohort_readiness") or {}).get(cohort) or {})
    return bool(
        int(raw.get("minimum_complete_close_windows") or 0) == required
        and int(raw.get("windows_remaining") or 0) == 0
        and raw.get("ready_for_locked_freeze") is True
    )


def ready_milestones(snapshot: Mapping[str, Any]) -> list[str]:
    return [name for name in MILESTONES if milestone_is_ready(snapshot, name)]


def readiness_message(snapshot: Mapping[str, Any], milestone: str) -> str:
    definition = MILESTONES[milestone]
    windows = int(snapshot["complete_executable_close_windows"])
    required = int(definition["complete_windows"])
    integrity = dict(snapshot.get("soft_input_integrity") or {})
    return "\n".join((
        f"<b>V3 V14 {definition['headline']} | PAPER ADMIN</b>",
        f"Complete seven-asset windows: {windows}/{required}",
        f"Fully observed windows: {int(integrity.get('fully_observed_close_windows', windows))}/{windows}; neutralized-input rows: {int(integrity.get('soft_degraded_rows', 0))}",
        f"Milestone: {milestone}",
        "Outcome labels: SEALED / unread",
        "Model fit and scoring: NOT RUN",
        "Artifact/promotion/trading: DISABLED",
        str(definition["action"]),
        f"Design: <code>{v14.DESIGN_ID}</code>",
        "This is an administrative readiness notice, not a trade signal.",
    ))


def send_ready_milestones(
    snapshot: Mapping[str, Any], sender: Callable[..., Mapping[str, Any]],
    *, now: float | None = None,
) -> dict[str, Any]:
    ready = ready_milestones(snapshot)
    if not ready:
        return {
            "status": "WAITING_FOR_MILESTONE",
            "notice_attempted": False,
            "ready_milestones": [],
            "deliveries": {},
        }
    current = time.time() if now is None else float(now)
    deliveries = {
        name: dict(sender(
            readiness_message(snapshot, name),
            idempotency_key=IDEMPOTENCY_KEYS[name],
            expires_at=current + 365.0 * 86400.0,
        ))
        for name in ready
    }
    return {
        "status": "READY_MILESTONES_PROCESSED",
        "notice_attempted": True,
        "ready_milestones": ready,
        "deliveries": deliveries,
    }


def _default_sender() -> Callable[..., Mapping[str, Any]]:
    raw = V3Telegram()
    outbox = ReliableTelegramOutbox(
        _DisabledStore(), raw,
        sqlite_path=os.environ.get(
            "Q15_V9_OUTBOX_SQLITE_PATH", "data/q15_telegram_outbox.sqlite3",
        ),
    )
    return outbox.send_with_result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--design", default=str(DEFAULT_DESIGN))
    parser.add_argument("--strategy-db", default=str(DEFAULT_DB))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    snapshot = build_outcome_blind_snapshot(
        design_path=Path(args.design), database_path=Path(args.strategy_db),
    )
    if args.dry_run:
        result = {
            "status": (
                "READY_MILESTONES_DRY_RUN"
                if ready_milestones(snapshot) else "WAITING_FOR_MILESTONE"
            ),
            "notice_attempted": False,
            "ready_milestones": ready_milestones(snapshot),
            "deliveries": {},
        }
    else:
        result = send_ready_milestones(snapshot, _default_sender())
    print(json.dumps({**snapshot, **result}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
