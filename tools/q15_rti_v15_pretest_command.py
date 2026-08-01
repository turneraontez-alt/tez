"""Manual command for the sealed V15 train/calibration evaluation.

The command reconstructs the frozen outcome-free evidence first.  Its
read-only SQLite callback is invoked by ``run_pretest_once`` only after the
append-only reservation exists, and it queries exactly the authorized
train/calibration row IDs.  It never queries the untouched-test IDs.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Callable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import q15_rti_v15_audit_seal as audit_seal
from tools import q15_rti_v15_pretest as pretest
from tools import q15_rti_v15_label_evidence as label_evidence
from tools.q15_rti_microstructure_freeze import load_feature_rows
from q15_upgrade.kalshi_rest import BASE_URL, KalshiClient, parse_ts


DEFAULT_STATE_DIR = ROOT / "reports" / "q15_rti_v15_audit_runs"


def load_ready_seal(path: Path) -> dict[str, Any]:
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("v15_pretest_command_seal_unreadable") from exc
    if not isinstance(decoded, Mapping):
        raise ValueError("v15_pretest_command_seal_root_not_object")
    seal = dict(decoded)
    audit_seal.validate_audit_seal(seal)
    if seal.get("status") != audit_seal.READY_STATUS:
        raise ValueError("v15_pretest_command_seal_not_ready")
    return seal


def select_sealed_feature_rows(
    rows: Sequence[Mapping[str, Any]],
    seal: Mapping[str, Any],
) -> list[dict[str, Any]]:
    cohort = str(seal["cohort"])
    minimum = int(seal["minimum_complete_close_windows"])
    complete = audit_seal._complete_windows(rows)
    selected_times = tuple(sorted(complete)[:minimum])
    if (
        len(selected_times) != minimum
        or audit_seal.canonical_sha256(selected_times)
        != seal.get("selected_close_times_sha256")
    ):
        raise ValueError("v15_pretest_command_selected_window_identity_mismatch")
    assets = audit_seal.COHORT_ASSETS[cohort]
    return [
        dict(row)
        for close_time in selected_times
        for row in complete[close_time]
        if str(row.get("asset") or "").upper() in assets
    ]


class SQLitePretestLabelReader:
    """Read only the exact row IDs authorized by the pretest reservation."""

    def __init__(
        self,
        database_path: Path,
        *,
        expected_rows: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        if expected_rows is None:
            self.expected_rows = None
        else:
            materialized = list(expected_rows)
            try:
                expected = {
                    int(row["id"]): {
                    "ticker": str(row.get("ticker") or ""),
                    "asset": str(row.get("asset") or "").upper(),
                    "close_time": float(row["close_time"]),
                    }
                    for row in materialized
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    "v15_pretest_command_expected_contracts_invalid"
                ) from exc
            if (
                len(expected) != len(materialized)
                or any(
                    not item["ticker"]
                    or not item["asset"]
                    or not math.isfinite(item["close_time"])
                    for item in expected.values()
                )
            ):
                raise ValueError(
                    "v15_pretest_command_expected_contracts_invalid"
                )
            self.expected_rows = expected

    def read_contract_records(
        self, row_ids: Sequence[int],
    ) -> dict[int, dict[str, Any]]:
        requested = tuple(sorted(int(value) for value in row_ids))
        if not requested or len(set(requested)) != len(requested):
            raise ValueError("v15_pretest_command_label_ids_invalid")
        if (
            self.expected_rows is not None
            and not set(requested).issubset(self.expected_rows)
        ):
            raise ValueError(
                "v15_pretest_command_label_id_not_in_sealed_evidence"
            )
        uri = self.database_path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=30.0) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            placeholders = ",".join("?" for _ in requested)
            records = connection.execute(
                "SELECT id, ticker, asset, close_time, "
                "official_result, resolved_at "
                "FROM strategy_bot_decisions "
                f"WHERE id IN ({placeholders})",
                requested,
            ).fetchall()
        by_id = {int(record["id"]): record for record in records}
        if set(by_id) != set(requested):
            raise ValueError("v15_pretest_command_label_rows_missing")
        verified: dict[int, dict[str, Any]] = {}
        for row_id in requested:
            record = by_id[row_id]
            try:
                close_time = float(record["close_time"])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "v15_pretest_command_label_contract_mismatch"
                ) from exc
            if self.expected_rows is not None:
                expected = self.expected_rows[row_id]
                if (
                    str(record["ticker"] or "") != expected["ticker"]
                    or str(record["asset"] or "").upper()
                    != expected["asset"]
                    or abs(close_time - expected["close_time"]) > 1e-6
                ):
                    raise ValueError(
                        "v15_pretest_command_label_contract_mismatch"
                    )
            raw_resolved_at = record["resolved_at"]
            try:
                resolved_at = (
                    None
                    if raw_resolved_at is None
                    else float(raw_resolved_at)
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "v15_pretest_command_label_unresolved_or_invalid"
                ) from exc
            verified[row_id] = {
                "id": row_id,
                "ticker": str(record["ticker"] or ""),
                "asset": str(record["asset"] or "").upper(),
                "close_time": close_time,
                "official_result": (
                    str(record["official_result"] or "").upper() or None
                ),
                "resolved_at": resolved_at,
            }
        return verified

    def __call__(self, row_ids: Sequence[int]) -> dict[int, int]:
        records = self.read_contract_records(row_ids)
        labels: dict[int, int] = {}
        for row_id, record in records.items():
            result = record["official_result"]
            resolved_at = record["resolved_at"]
            if (
                result not in {"YES", "NO"}
                or resolved_at is None
                or resolved_at + 1e-6 < float(record["close_time"])
            ):
                raise ValueError(
                    "v15_pretest_command_label_unresolved_or_invalid"
                )
            labels[row_id] = 1 if result == "YES" else 0
        return labels


class KalshiVerifiedSQLiteLabelReader:
    """Cross-check sealed local labels against fresh official Kalshi results.

    The callback is invoked only after the stage's exclusive reservation has
    been written.  It asks Kalshi for exactly the distinct contracts belonging
    to the authorized row IDs and returns evidence that the audit runner binds
    into its append-only result.
    """

    def __init__(
        self,
        database_path: Path,
        *,
        expected_rows: Sequence[Mapping[str, Any]],
        get_market: Callable[[str], Mapping[str, Any] | None] | None = None,
        source_base_url: str = BASE_URL,
        fetch_attempts: int = 3,
    ) -> None:
        self.local_reader = SQLitePretestLabelReader(
            database_path,
            expected_rows=expected_rows,
        )
        if self.local_reader.expected_rows is None:
            raise ValueError(
                "v15_pretest_command_expected_contracts_required"
            )
        self.expected_rows = self.local_reader.expected_rows
        self.get_market = (
            get_market
            if get_market is not None
            else KalshiClient().get_market
        )
        self.source_base_url = str(source_base_url).rstrip("/")
        self.fetch_attempts = max(1, min(int(fetch_attempts), 5))

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _fetch_market(self, ticker: str) -> Mapping[str, Any] | None:
        last_error: Exception | None = None
        for attempt in range(self.fetch_attempts):
            try:
                market = self.get_market(ticker)
            except Exception as exc:  # network/client failure is fail-closed
                last_error = exc
                market = None
            if isinstance(market, Mapping):
                return market
            if attempt + 1 < self.fetch_attempts:
                time.sleep(0.25 * (attempt + 1))
        if last_error is not None:
            raise ValueError(
                "v15_pretest_command_kalshi_market_unavailable"
            ) from last_error
        return None

    def __call__(
        self, row_ids: Sequence[int],
    ) -> label_evidence.VerifiedLabelMapping:
        requested = tuple(sorted(int(value) for value in row_ids))
        local_records = self.local_reader.read_contract_records(requested)
        labels: dict[int, int] = {}
        grouped: dict[str, list[int]] = {}
        for row_id in requested:
            expected = self.expected_rows[row_id]
            grouped.setdefault(str(expected["ticker"]), []).append(row_id)

        started_at = self._now_iso()
        contracts: list[dict[str, Any]] = []
        for ticker in sorted(grouped):
            ids = sorted(grouped[ticker])
            local_labels: set[int] = set()
            local_unresolved_ids: list[int] = []
            local_invalid_ids: list[int] = []
            local_valid_ids: list[int] = []
            for row_id in ids:
                local = local_records[row_id]
                result = local["official_result"]
                resolved_at = local["resolved_at"]
                if result in {"YES", "NO"}:
                    local_labels.add(1 if result == "YES" else 0)
                    if (
                        resolved_at is None
                        or float(resolved_at) + 1e-6
                        < float(local["close_time"])
                    ):
                        local_invalid_ids.append(row_id)
                    else:
                        local_valid_ids.append(row_id)
                elif result is None and resolved_at is None:
                    local_unresolved_ids.append(row_id)
                else:
                    local_invalid_ids.append(row_id)
            close_times = {
                float(self.expected_rows[row_id]["close_time"])
                for row_id in ids
            }
            if len(local_labels) > 1 or len(close_times) != 1:
                raise ValueError(
                    "v15_pretest_command_local_contract_conflict"
                )
            fetched_at = self._now_iso()
            market = self._fetch_market(ticker)
            if not isinstance(market, Mapping):
                raise ValueError(
                    "v15_pretest_command_kalshi_market_unavailable"
                )
            returned_ticker = str(market.get("ticker") or "").strip()
            result = str(market.get("result") or "").strip().upper()
            status = str(market.get("status") or "").strip().lower()
            api_close_time = parse_ts(market.get("close_time"))
            expected_close_time = next(iter(close_times))
            if returned_ticker != ticker:
                raise ValueError(
                    "v15_pretest_command_kalshi_contract_mismatch"
                )
            if result not in {"YES", "NO"} or status != "finalized":
                raise ValueError(
                    "v15_pretest_command_kalshi_not_final"
                )
            if (
                api_close_time is None
                or abs(float(api_close_time) - expected_close_time) > 1.0
            ):
                raise ValueError(
                    "v15_pretest_command_kalshi_close_time_mismatch"
                )
            result_yes = 1 if result == "YES" else 0
            if local_labels and result_yes != next(iter(local_labels)):
                raise ValueError(
                    "v15_pretest_command_kalshi_label_mismatch"
                )
            for row_id in ids:
                labels[row_id] = result_yes
            if not local_unresolved_ids and not local_invalid_ids:
                local_cache_status = "MATCHED"
            elif (
                len(local_unresolved_ids) == len(ids)
                and not local_invalid_ids
            ):
                local_cache_status = "UNRESOLVED_API_AUTHORITY"
            else:
                local_cache_status = "DEGRADED_LOCAL_CACHE_API_AUTHORITY"
            contracts.append({
                "ticker": ticker,
                "row_ids": ids,
                "result_yes": result_yes,
                "status": status,
                "expected_close_time": expected_close_time,
                "kalshi_close_time": float(api_close_time),
                "kalshi_settled_time": parse_ts(
                    market.get("settlement_ts")
                    or market.get("settled_time")
                ),
                "kalshi_expiration_time": parse_ts(
                    market.get("expiration_time")
                ),
                "local_cache_status": local_cache_status,
                "local_resolved_row_count": len(local_valid_ids),
                "local_unresolved_row_count": len(local_unresolved_ids),
                "local_invalid_row_count": len(local_invalid_ids),
                "local_resolved_labels_match_api": True,
                "fetched_at": fetched_at,
            })

        label_pairs = sorted(
            [int(row_id), int(label)]
            for row_id, label in labels.items()
        )
        evidence = label_evidence.seal_evidence({
            "evidence_version": label_evidence.EVIDENCE_VERSION,
            "verification_status": label_evidence.PASS_STATUS,
            "source_id": label_evidence.SOURCE_ID,
            "source_base_url": self.source_base_url,
            "verification_started_at": started_at,
            "verification_completed_at": self._now_iso(),
            "row_count": len(requested),
            "unique_contracts": len(contracts),
            "requested_row_ids_sha256": (
                label_evidence.canonical_sha256(requested)
            ),
            "labels_sha256": (
                label_evidence.canonical_sha256(label_pairs)
            ),
            "requested_contracts_sha256": (
                label_evidence.canonical_sha256(
                    tuple(sorted(grouped))
                )
            ),
            "contracts": contracts,
        })
        return label_evidence.VerifiedLabelMapping(labels, evidence)


def default_reservation_path(cohort: str) -> Path:
    return (
        DEFAULT_STATE_DIR
        / str(cohort).lower()
        / "pretest-reservation.json"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cohort", choices=tuple(audit_seal.COHORT_ASSETS))
    parser.add_argument("--seal", required=True)
    parser.add_argument(
        "--strategy-db", default=str(audit_seal.DEFAULT_DB),
    )
    parser.add_argument("--reservation")
    parser.add_argument("--confirmation", required=True)
    args = parser.parse_args()

    seal = load_ready_seal(Path(args.seal))
    cohort = str(seal["cohort"])
    if args.cohort is not None and args.cohort != cohort:
        raise ValueError("v15_pretest_command_cohort_mismatch")
    (
        design,
        _v14_design,
        _charter,
        protocol,
        _artifact,
        _artifact_sha,
    ) = audit_seal._load_inputs()
    database_path = Path(args.strategy_db)
    selected = select_sealed_feature_rows(
        load_feature_rows(database_path),
        seal,
    )
    reservation_path = (
        Path(args.reservation)
        if args.reservation
        else default_reservation_path(cohort)
    )
    result = pretest.run_pretest_once(
        seal=seal,
        selected_feature_rows=selected,
        design=design,
        protocol=protocol,
        cohort=cohort,
        reservation_path=reservation_path,
        confirmation=args.confirmation,
        read_pretest_labels=KalshiVerifiedSQLiteLabelReader(
            database_path,
            expected_rows=selected,
        ),
        require_label_evidence=True,
    )
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
