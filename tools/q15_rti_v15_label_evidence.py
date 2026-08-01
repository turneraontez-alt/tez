"""Outcome-source evidence shared by the manual V15 audit stages.

The audit runners remain unable to access databases or networks.  A command
may return ``VerifiedLabelMapping`` from its already-reserved label callback;
the runners then validate and bind the evidence into their append-only result.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping, Sequence

from q15_upgrade.strategy_bots.rti_microstructure_v15_audit_identity import (
    SETTLEMENT_EVIDENCE_SOURCE_ID,
    SETTLEMENT_EVIDENCE_VERSION,
)

EVIDENCE_VERSION = SETTLEMENT_EVIDENCE_VERSION
PASS_STATUS = "AUTHORITATIVE_KALSHI_SETTLEMENTS_VERIFIED"
SOURCE_ID = SETTLEMENT_EVIDENCE_SOURCE_ID


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def seal_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    output = dict(payload)
    output.pop("evidence_sha256", None)
    output["evidence_sha256"] = canonical_sha256(output)
    return output


class VerifiedLabelMapping(dict[int, int]):
    """Plain label mapping carrying independently verified source evidence."""

    def __init__(
        self,
        labels: Mapping[int, int],
        audit_evidence: Mapping[str, Any],
    ) -> None:
        super().__init__(
            (int(row_id), int(label))
            for row_id, label in labels.items()
        )
        self.audit_evidence = dict(audit_evidence)


def validate_label_evidence(
    raw_labels: Mapping[int, int],
    labels: Mapping[int, int],
    expected_ids: Sequence[int],
    *,
    required: bool,
    stage: str,
) -> dict[str, Any] | None:
    raw_evidence = getattr(raw_labels, "audit_evidence", None)
    if raw_evidence is None:
        if required:
            raise ValueError(f"v15_{stage}_label_evidence_required")
        return None
    if not isinstance(raw_evidence, Mapping):
        raise ValueError(f"v15_{stage}_label_evidence_invalid")
    evidence = dict(raw_evidence)
    supplied_hash = str(evidence.pop("evidence_sha256", ""))
    if supplied_hash != canonical_sha256(evidence):
        raise ValueError(f"v15_{stage}_label_evidence_sha256_invalid")
    evidence["evidence_sha256"] = supplied_hash

    ids = tuple(sorted(int(value) for value in expected_ids))
    label_pairs = sorted(
        [int(row_id), int(label)]
        for row_id, label in labels.items()
    )
    contracts = evidence.get("contracts")
    if not isinstance(contracts, list) or not contracts:
        raise ValueError(f"v15_{stage}_label_evidence_invalid")

    evidence_ids: list[int] = []
    evidence_pairs: list[list[int]] = []
    tickers: list[str] = []
    for contract in contracts:
        if not isinstance(contract, Mapping):
            raise ValueError(f"v15_{stage}_label_evidence_invalid")
        ticker = str(contract.get("ticker") or "").strip()
        row_ids = contract.get("row_ids")
        result_yes = contract.get("result_yes")
        status = str(contract.get("status") or "").strip().lower()
        local_cache_status = str(
            contract.get("local_cache_status") or ""
        ).strip()
        try:
            expected_close = float(contract["expected_close_time"])
            kalshi_close = float(contract["kalshi_close_time"])
            local_resolved_rows = int(
                contract["local_resolved_row_count"]
            )
            local_unresolved_rows = int(
                contract["local_unresolved_row_count"]
            )
            local_invalid_rows = int(
                contract["local_invalid_row_count"]
            )
            settled_time = (
                None
                if contract.get("kalshi_settled_time") is None
                else float(contract["kalshi_settled_time"])
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"v15_{stage}_label_evidence_invalid"
            ) from exc
        if (
            not ticker
            or not isinstance(row_ids, list)
            or not row_ids
            or result_yes not in {0, 1}
            or status != "finalized"
            or local_cache_status not in {
                "MATCHED",
                "UNRESOLVED_API_AUTHORITY",
                "DEGRADED_LOCAL_CACHE_API_AUTHORITY",
            }
            or local_resolved_rows < 0
            or local_unresolved_rows < 0
            or local_invalid_rows < 0
            or (
                local_resolved_rows
                + local_unresolved_rows
                + local_invalid_rows
                != len(row_ids)
            )
            or contract.get("local_resolved_labels_match_api") is not True
            or (
                local_cache_status == "MATCHED"
                and (
                    local_unresolved_rows != 0
                    or local_invalid_rows != 0
                )
            )
            or (
                local_cache_status == "UNRESOLVED_API_AUTHORITY"
                and (
                    local_resolved_rows != 0
                    or local_invalid_rows != 0
                )
            )
            or (
                local_cache_status == "DEGRADED_LOCAL_CACHE_API_AUTHORITY"
                and local_unresolved_rows == 0
                and local_invalid_rows == 0
            )
            or not math.isfinite(expected_close)
            or not math.isfinite(kalshi_close)
            or abs(expected_close - kalshi_close) > 1.0
            or (
                settled_time is not None
                and (
                    not math.isfinite(settled_time)
                    or settled_time + 1e-6 < kalshi_close
                )
            )
            or not str(contract.get("fetched_at") or "").strip()
        ):
            raise ValueError(f"v15_{stage}_label_evidence_invalid")
        tickers.append(ticker)
        for raw_id in row_ids:
            try:
                row_id = int(raw_id)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"v15_{stage}_label_evidence_invalid"
                ) from exc
            evidence_ids.append(row_id)
            evidence_pairs.append([row_id, int(result_yes)])

    if (
        evidence.get("evidence_version") != EVIDENCE_VERSION
        or evidence.get("verification_status") != PASS_STATUS
        or evidence.get("source_id") != SOURCE_ID
        or int(evidence.get("row_count") or -1) != len(ids)
        or int(evidence.get("unique_contracts") or -1) != len(contracts)
        or len(set(tickers)) != len(tickers)
        or tuple(sorted(evidence_ids)) != ids
        or len(set(evidence_ids)) != len(evidence_ids)
        or sorted(evidence_pairs) != label_pairs
        or evidence.get("requested_row_ids_sha256")
        != canonical_sha256(ids)
        or evidence.get("labels_sha256")
        != canonical_sha256(label_pairs)
        or evidence.get("requested_contracts_sha256")
        != canonical_sha256(tuple(sorted(tickers)))
    ):
        raise ValueError(f"v15_{stage}_label_evidence_invalid")
    return evidence
