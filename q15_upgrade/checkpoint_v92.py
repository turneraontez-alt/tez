from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from q15_upgrade.checkpoint_v91 import (
    ASSETS,
    DISCLAIMER,
    VERSION as V91_VERSION,
    CheckpointPolicyV91,
    RollingMetrics,
    _LATEST,
    _LATEST_LOCK,
    _candidate_order,
    _canonical_p_yes,
    _checkpoint,
    _contract_key,
    _env_float,
    _env_int,
    _fmt_cents,
    _fmt_pct,
    _human_status,
    _num,
    _prob,
    _side_probability,
    _strip_disclaimers,
)

VERSION = "q15-v9.2-sequential-consensus"
_LOG = logging.getLogger(__name__)
_ACTIVE_POLICY: "CheckpointPolicyV92 | None" = None


@dataclass(frozen=True)
class RelationshipAssessment:
    relation: str
    penalty_cents: float
    blocks_entry: bool
    prior_side: str
    current_side: str
    prior_strength: float | None
    current_strength: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "relation": self.relation,
            "penalty_cents": round(self.penalty_cents, 4),
            "blocks_entry": self.blocks_entry,
            "prior_side": self.prior_side,
            "current_side": self.current_side,
            "prior_strength": self.prior_strength,
            "current_strength": self.current_strength,
        }


def _side_strength(p_yes: Any, side: str) -> float | None:
    probability = _prob(p_yes)
    if probability is None:
        return None
    return probability if side == "YES" else 1.0 - probability if side == "NO" else None


def _rolling_state(
    rolling: Mapping[str, Any],
    *,
    watch_min_votes: int,
    ready_min_votes: int,
    minimum_agreement: float,
) -> str:
    directional = int(_num(rolling.get("directional_votes"), 0) or 0)
    yes = int(_num(rolling.get("yes_votes"), 0) or 0)
    no = int(_num(rolling.get("no_votes"), 0) or 0)
    selected = str(rolling.get("selected_side") or ("YES" if yes > no else "NO" if no > yes else "UNCERTAIN")).upper()
    agreement = float(_num(rolling.get("directional_agreement"), 0.0) or 0.0)

    if selected not in {"YES", "NO"}:
        return "DIRECTION_UNCERTAIN"
    if directional < watch_min_votes:
        return "INSUFFICIENT_EVIDENCE"
    if agreement < minimum_agreement:
        return "DIRECTIONAL_AGREEMENT_LOW"
    if directional < ready_min_votes:
        return "WATCH"
    return "DIRECTION_VALID"


class CheckpointPolicyV92(CheckpointPolicyV91):
    """Sequential checkpoint policy without a universal 15M/10M consensus veto."""

    def __init__(self, store: Any = None, sqlite_path: str | None = None):
        super().__init__(store=store, sqlite_path=sqlite_path)
        self.watch_min_votes = max(1, _env_int("Q15_V92_WATCH_MIN_DIRECTIONAL_VOTES", 2))
        self.ready_min_votes = max(
            self.watch_min_votes,
            _env_int("Q15_V92_READY_MIN_DIRECTIONAL_VOTES", 4),
        )
        self.strong_conflict_probability = max(
            0.50,
            min(0.95, _env_float("Q15_V92_STRONG_CONFLICT_PROBABILITY", 0.68)),
        )
        self.missing_prior_penalty_cents = max(
            0.0, _env_float("Q15_V92_MISSING_PRIOR_PENALTY_CENTS", 2.0)
        )
        self.uncertain_prior_penalty_cents = max(
            0.0, _env_float("Q15_V92_UNCERTAIN_PRIOR_PENALTY_CENTS", 2.0)
        )
        self.weak_conflict_penalty_cents = max(
            0.0, _env_float("Q15_V92_WEAK_CONFLICT_PENALTY_CENTS", 4.0)
        )
        self._migrate_v92()
        global _ACTIVE_POLICY
        _ACTIVE_POLICY = self

    def _migrate_v92(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS q15_v92_decisions (
                decision_key TEXT PRIMARY KEY,
                contract_key TEXT NOT NULL,
                asset TEXT NOT NULL,
                checkpoint TEXT NOT NULL,
                selected_side TEXT NOT NULL,
                evidence_state TEXT NOT NULL,
                checkpoint_relation TEXT NOT NULL,
                decision TEXT NOT NULL,
                p_yes DOUBLE PRECISION,
                conservative_edge_cents DOUBLE PRECISION,
                adjusted_required_edge_cents DOUBLE PRECISION,
                created_at DOUBLE PRECISION NOT NULL,
                model_version TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS q15_v92_decision_lookup "
            "ON q15_v92_decisions(model_version, checkpoint, decision)",
        ]
        if self.persistence.pg:
            for sql in statements:
                if not bool(self.persistence.store.execute(sql)):
                    raise RuntimeError("q15_v92_decision_migration_failed")
            return
        with self.persistence._lock, self.persistence._sqlite() as conn:
            for sql in statements:
                conn.execute(sql)
            conn.commit()

    def _classify(self, rolling: Mapping[str, Any]) -> str:
        return _rolling_state(
            rolling,
            watch_min_votes=self.watch_min_votes,
            ready_min_votes=self.ready_min_votes,
            minimum_agreement=self.min_agreement,
        )

    def _relationship(
        self,
        fifteen: Mapping[str, Any] | None,
        *,
        current_side: str,
        current_p_yes: Any,
        current_evidence_state: str,
    ) -> RelationshipAssessment:
        current_side = str(current_side or "UNCERTAIN").upper()
        current_strength = _side_strength(current_p_yes, current_side)
        if fifteen is None:
            return RelationshipAssessment(
                "MISSING_15M",
                self.missing_prior_penalty_cents,
                False,
                "MISSING",
                current_side,
                None,
                current_strength,
            )

        prior_side = str(fifteen.get("side") or "UNCERTAIN").upper()
        prior_p_yes = fifteen.get("p_yes")
        prior_rolling = fifteen.get("rolling") or {}
        prior_state = self._classify(prior_rolling) if prior_rolling else str(
            fifteen.get("evidence_status") or "UNCERTAIN"
        ).upper()
        prior_strength = _side_strength(prior_p_yes, prior_side)

        if prior_side not in {"YES", "NO"} or prior_state in {
            "INSUFFICIENT_EVIDENCE",
            "DIRECTION_UNCERTAIN",
            "DIRECTIONAL_AGREEMENT_LOW",
            "WATCH",
            "WARMUP_INCOMPLETE",
        }:
            return RelationshipAssessment(
                "UNCERTAIN_15M",
                self.uncertain_prior_penalty_cents,
                False,
                prior_side,
                current_side,
                prior_strength,
                current_strength,
            )

        if current_side not in {"YES", "NO"}:
            return RelationshipAssessment(
                "UNCERTAIN_10M",
                0.0,
                True,
                prior_side,
                current_side,
                prior_strength,
                current_strength,
            )

        if prior_side == current_side:
            return RelationshipAssessment(
                "SAME",
                0.0,
                False,
                prior_side,
                current_side,
                prior_strength,
                current_strength,
            )

        strong = bool(
            prior_state == "DIRECTION_VALID"
            and current_evidence_state == "DIRECTION_VALID"
            and prior_strength is not None
            and current_strength is not None
            and prior_strength >= self.strong_conflict_probability
            and current_strength >= self.strong_conflict_probability
        )
        return RelationshipAssessment(
            "STRONG_CONFLICT" if strong else "WEAK_CONFLICT",
            0.0 if strong else self.weak_conflict_penalty_cents,
            strong,
            prior_side,
            current_side,
            prior_strength,
            current_strength,
        )

    def pre_enrich_all(
        self,
        snapshots: Mapping[str, dict],
        now: float,
    ) -> dict[str, dict]:
        prepared = super().pre_enrich_all(snapshots, now)
        output: dict[str, dict] = {}
        for asset, original in prepared.items():
            snap = dict(original)
            rolling = dict(snap.get("q15_v91_rolling") or {})
            evidence_state = self._classify(rolling)
            rolling["v92_status"] = evidence_state
            checkpoint = str(snap.get("q15_v91_checkpoint") or _checkpoint(snap)).upper()
            contract = str(snap.get("q15_v91_contract_key") or _contract_key(asset, snap))
            current_side = str(
                snap.get("q15_v91_frozen_side")
                or snap.get("q15_v91_current_side")
                or "UNCERTAIN"
            ).upper()
            current_p_yes = _canonical_p_yes(snap)

            if checkpoint == "15M":
                relationship = RelationshipAssessment(
                    "EARLY_ONLY", 0.0, False, "NOT_APPLICABLE", current_side, None,
                    _side_strength(current_p_yes, current_side),
                )
            else:
                # Reuse the 15M prediction the v91 stage already read this
                # cycle (threaded via the snapshot) instead of a fresh
                # per-asset round-trip; fall back to a direct read if this
                # snapshot did not pass through v91 pre-enrich.
                if "q15_v91_fifteen_prediction" in snap:
                    fifteen = snap.get("q15_v91_fifteen_prediction")
                else:
                    fifteen = self.persistence.get_prediction(contract, asset, "15M")
                relationship = self._relationship(
                    fifteen,
                    current_side=current_side,
                    current_p_yes=current_p_yes,
                    current_evidence_state=evidence_state,
                )

            direction_valid = evidence_state == "DIRECTION_VALID" and current_side in {"YES", "NO"}
            snap.update({
                "q15_v92_version": VERSION,
                "q15_v92_evidence_state": evidence_state,
                "q15_v92_checkpoint_relation": relationship.relation,
                "q15_v92_relationship": relationship.as_dict(),
                "q15_v92_sequence_penalty_cents": relationship.penalty_cents,
                "q15_v92_sequence_blocks_entry": relationship.blocks_entry,
                "q15_v92_direction_valid": direction_valid,
                "q15_prediction_agreement": relationship.relation,
                "q15_final_result": current_side if current_side in {"YES", "NO"} else "UNCERTAIN",
                "q15_consensus_side": current_side if current_side in {"YES", "NO"} else None,
                "q15_new_entry_allowed": bool(direction_valid and not relationship.blocks_entry),
            })
            snap["q15_v91_rolling"] = rolling
            output[asset] = snap
        return output

    def _decision_values(self, asset: str, snapshot: Mapping[str, Any], now: float) -> tuple[Any, ...]:
        contract = str(snapshot.get("q15_v91_contract_key") or _contract_key(asset, snapshot))
        checkpoint = str(snapshot.get("q15_v91_checkpoint") or _checkpoint(snapshot))
        key = f"{contract}|{asset}|{checkpoint}|{VERSION}"
        return (
            key,
            contract,
            asset,
            checkpoint,
            str(snapshot.get("calibrated_edge_side") or snapshot.get("q15_v91_frozen_side") or "UNCERTAIN"),
            str(snapshot.get("q15_v92_evidence_state") or "UNKNOWN"),
            str(snapshot.get("q15_v92_checkpoint_relation") or "UNKNOWN"),
            str(snapshot.get("q15_v92_decision") or "UNKNOWN"),
            _canonical_p_yes(snapshot),
            _num(snapshot.get("q15_v92_conservative_edge_cents")),
            _num(snapshot.get("q15_v92_adjusted_required_edge_cents")),
            float(now),
            VERSION,
        )

    def _record_decision(self, asset: str, snapshot: Mapping[str, Any], now: float) -> None:
        self._record_decisions([self._decision_values(asset, snapshot, now)])

    def _record_decisions(self, rows: Sequence[tuple[Any, ...]]) -> None:
        """Insert many v92 decisions in one round-trip. Equivalent to N
        _record_decision calls (ON CONFLICT/OR IGNORE dedups per row)."""
        rows = list(rows)
        if not rows:
            return
        cols = (
            "(decision_key,contract_key,asset,checkpoint,selected_side,evidence_state,"
            "checkpoint_relation,decision,p_yes,conservative_edge_cents,"
            "adjusted_required_edge_cents,created_at,model_version)"
        )
        if self.persistence.pg:
            one = "(" + ",".join(["%s"] * 13) + ")"
            placeholders = ",".join([one] * len(rows))
            flat = [v for tup in rows for v in tup]
            self.persistence.store.execute(
                f"INSERT INTO q15_v92_decisions {cols} VALUES {placeholders} "
                "ON CONFLICT (decision_key) DO NOTHING",
                flat,
            )
            return
        with self.persistence._lock, self.persistence._sqlite() as conn:
            conn.executemany(
                f"INSERT OR IGNORE INTO q15_v92_decisions {cols} "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            conn.commit()

    def finalize_all(
        self,
        snapshots: Mapping[str, dict],
        now: float,
    ) -> dict[str, dict]:
        output: dict[str, dict] = {}
        decision_rows: list[tuple[Any, ...]] = []
        for asset, original in snapshots.items():
            if not isinstance(original, dict):
                continue
            snap = dict(original)
            side = str(
                snap.get("calibrated_edge_side")
                or snap.get("q15_v91_frozen_side")
                or "UNCERTAIN"
            ).upper()
            evidence_state = str(snap.get("q15_v92_evidence_state") or "INSUFFICIENT_EVIDENCE").upper()
            relation = str(snap.get("q15_v92_checkpoint_relation") or "UNKNOWN").upper()
            relationship = snap.get("q15_v92_relationship") or {}
            sequence_penalty = float(
                _num(snap.get("q15_v92_sequence_penalty_cents"), 0.0) or 0.0
            )
            sequence_block = bool(snap.get("q15_v92_sequence_blocks_entry"))
            trade_status = str(
                snap.get("calibrated_edge_status") or "INSUFFICIENT_EVIDENCE"
            ).upper()
            economics = snap.get("canonical_economics") or {}
            conservative_edge = _num(
                economics.get(
                    "conservative_net_edge_cents",
                    snap.get("conservative_edge_after_costs_cents"),
                )
            )
            required_edge = float(
                _num((snap.get("trade_quality") or {}).get("required_edge_cents"), 0.0)
                or 0.0
            )
            adjusted_required = required_edge + sequence_penalty

            blockers: list[str] = []
            if side not in {"YES", "NO"}:
                blockers.append("DIRECTION_UNCERTAIN")
            if evidence_state != "DIRECTION_VALID":
                blockers.append(evidence_state)
            if sequence_block:
                blockers.append(relation)
            if trade_status != "READY":
                blockers.append(trade_status)
            if conservative_edge is None:
                blockers.append("ECONOMICS_UNAVAILABLE")
            elif conservative_edge < adjusted_required:
                blockers.append("EDGE_BELOW_ADJUSTED_REQUIREMENT")

            if side not in {"YES", "NO"}:
                decision = "DIRECTION_UNCERTAIN"
            elif evidence_state == "WATCH":
                decision = "WATCH"
            elif evidence_state in {
                "INSUFFICIENT_EVIDENCE",
                "DIRECTIONAL_AGREEMENT_LOW",
                "DIRECTION_UNCERTAIN",
            }:
                decision = evidence_state
            elif sequence_block:
                decision = "CHECKPOINT_CONFLICT"
            elif conservative_edge is None:
                decision = "ECONOMICS_UNAVAILABLE"
            elif conservative_edge < adjusted_required:
                decision = "PRICE_NOT_ATTRACTIVE"
            elif trade_status == "READY":
                decision = "READY"
            elif trade_status in {
                "DATA_STALE",
                "INVALID_MARKET",
                "WAIT_FOR_CONFIRMATION",
                "WAIT_FOR_FOLLOW_THROUGH",
                "PRICE_NOT_ATTRACTIVE",
            }:
                decision = trade_status
            else:
                decision = "WATCH"

            allowed = decision == "READY"
            existing_state = str(snap.get("decision_state") or "").upper()
            if existing_state in {"ENTRY CONFIRMED", "REVERSAL WARNING"}:
                allowed = bool(snap.get("calibrated_edge_entry_allowed", True))

            snap.update({
                "q15_v92_decision": decision,
                "q15_v92_blockers": list(dict.fromkeys(blockers)),
                "q15_v92_entry_allowed": allowed,
                "q15_v92_conservative_edge_cents": conservative_edge,
                "q15_v92_base_required_edge_cents": required_edge,
                "q15_v92_adjusted_required_edge_cents": adjusted_required,
                "q15_v91_decision": decision,
                "q15_v91_blockers": list(dict.fromkeys(blockers)),
                "q15_new_entry_allowed": allowed,
                "calibrated_edge_new_entry_allowed": allowed,
            })
            with _LATEST_LOCK:
                _LATEST[asset] = deepcopy(snap)
            decision_rows.append(self._decision_values(asset, snap, now))
            output[asset] = snap
        # One bulk write for every asset's decision instead of one per asset.
        self._record_decisions(decision_rows)
        return output

    def _query_rows(self, sql_pg: str, sql_sqlite: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if self.persistence.pg:
            return list(self.persistence.store.query(sql_pg, params) or [])
        with self.persistence._lock, self.persistence._sqlite() as conn:
            return [dict(row) for row in conn.execute(sql_sqlite, params).fetchall()]

    def decision_stats(self) -> dict[str, Any]:
        if self.persistence.pg:
            rows = self._query_rows(
                "SELECT checkpoint,decision,checkpoint_relation,evidence_state,COUNT(*) AS n "
                "FROM q15_v92_decisions WHERE model_version=%s "
                "GROUP BY checkpoint,decision,checkpoint_relation,evidence_state",
                "",
                (VERSION,),
            )
        else:
            rows = self._query_rows(
                "",
                "SELECT checkpoint,decision,checkpoint_relation,evidence_state,COUNT(*) AS n "
                "FROM q15_v92_decisions WHERE model_version=? "
                "GROUP BY checkpoint,decision,checkpoint_relation,evidence_state",
                (VERSION,),
            )
        decisions: Counter[str] = Counter()
        checkpoints: Counter[str] = Counter()
        relations: Counter[str] = Counter()
        evidence: Counter[str] = Counter()
        for row in rows:
            count = int(row.get("n") or 0)
            decisions[str(row.get("decision") or "UNKNOWN")] += count
            checkpoints[str(row.get("checkpoint") or "UNKNOWN")] += count
            relations[str(row.get("checkpoint_relation") or "UNKNOWN")] += count
            evidence[str(row.get("evidence_state") or "UNKNOWN")] += count
        total = sum(decisions.values())
        ready = decisions.get("READY", 0)
        watch = decisions.get("WATCH", 0)
        candidates = {k: v for k, v in decisions.items() if k not in {"READY", "WATCH"}}
        bottleneck = max(candidates.items(), key=lambda item: item[1], default=("NONE", 0))
        return {
            "version": VERSION,
            "total_unique_decisions": total,
            "checkpoint_counts": dict(checkpoints),
            "decision_counts": dict(decisions),
            "relationship_counts": dict(relations),
            "evidence_counts": dict(evidence),
            "current_version_ready_entries": ready,
            "current_version_watch_decisions": watch,
            "current_live_entry_path": "OBSERVED" if ready > 0 else "NOT_YET_OBSERVED",
            "main_bottleneck": {"reason": bottleneck[0], "count": bottleneck[1]},
        }

    def health(self) -> dict[str, Any]:
        parent = super().health()
        return {
            **parent,
            "version": VERSION,
            "binary_cross_checkpoint_veto": False,
            "missing_15m_is_absolute_veto": False,
            "weak_conflict_is_absolute_veto": False,
            "strong_conflict_blocks_entry": True,
            "current_version_stats": self.decision_stats(),
        }

    def diagnostics(self) -> dict[str, Any]:
        parent = super().diagnostics()
        return {
            **parent,
            "version": VERSION,
            "configuration_v92": {
                "watch_min_directional_votes": self.watch_min_votes,
                "ready_min_directional_votes": self.ready_min_votes,
                "minimum_directional_agreement": self.min_agreement,
                "strong_conflict_probability": self.strong_conflict_probability,
                "missing_prior_penalty_cents": self.missing_prior_penalty_cents,
                "uncertain_prior_penalty_cents": self.uncertain_prior_penalty_cents,
                "weak_conflict_penalty_cents": self.weak_conflict_penalty_cents,
            },
            "current_version_stats": self.decision_stats(),
        }

    def consensus_status(self) -> dict[str, Any]:
        with _LATEST_LOCK:
            assets = {
                asset: {
                    "checkpoint": snap.get("q15_v91_checkpoint"),
                    "side": snap.get("calibrated_edge_side") or snap.get("q15_v91_frozen_side"),
                    "evidence_state": snap.get("q15_v92_evidence_state"),
                    "relationship": snap.get("q15_v92_checkpoint_relation"),
                    "relationship_detail": snap.get("q15_v92_relationship"),
                    "decision": snap.get("q15_v92_decision"),
                    "entry_allowed": snap.get("q15_v92_entry_allowed"),
                    "base_required_edge_cents": snap.get("q15_v92_base_required_edge_cents"),
                    "adjusted_required_edge_cents": snap.get("q15_v92_adjusted_required_edge_cents"),
                }
                for asset, snap in _LATEST.items()
            }
        return {
            "version": VERSION,
            "binary_consensus_veto": False,
            "assets": assets,
            "stats": self.decision_stats(),
            "read_only": True,
        }


def _candidate_snapshot(asset: str) -> dict[str, Any]:
    with _LATEST_LOCK:
        return deepcopy(_LATEST.get(asset) or {})


def _format_checkpoint(message: str) -> str:
    base = re.split(
        r"\n\s*(?:<b>)?(?:PROFESSIONAL TRADE QUALITY|V9 TRADE QUALITY)",
        message,
        maxsplit=1,
        flags=re.I,
    )[0]
    source_header = re.search(
        r"(?mi)^(.*(?:15M EARLY CHECK|10M FINAL CHECK).*)$",
        base,
    )
    original_header = source_header.group(1).strip() if source_header else "Q15 CHECK"
    candidates = _candidate_order(base)
    candidate_lines: list[str] = []
    decisions: list[str] = []
    checkpoint_name = "15M" if "15M EARLY CHECK" in original_header.upper() else "10M"

    for index, (asset, message_side) in enumerate(candidates, start=1):
        snap = _candidate_snapshot(asset)
        side = str(
            snap.get("calibrated_edge_side")
            or snap.get("q15_v91_frozen_side")
            or message_side
        ).upper()
        p_yes = _prob(snap.get("v9_calibrated_p_yes") or snap.get("calibrated_p_yes"))
        side_p = _prob(snap.get("calibrated_side_probability"))
        if side_p is None:
            side_p = _side_probability(p_yes, side)
        conservative = _prob(snap.get("conservative_side_probability"))
        economics = snap.get("canonical_economics") or {}
        ask = economics.get("executable_entry_ask_cents", snap.get("entry_ask"))
        costs = economics.get("total_estimated_cost_cents")
        edge = snap.get(
            "q15_v92_conservative_edge_cents",
            economics.get("conservative_net_edge_cents"),
        )
        base_required = snap.get("q15_v92_base_required_edge_cents")
        adjusted_required = snap.get("q15_v92_adjusted_required_edge_cents")
        rolling = snap.get("q15_v91_rolling") or {}
        directional = int(_num(rolling.get("directional_votes"), 0) or 0)
        window = max(1, _env_int("Q15_V91_ROLLING_WINDOW", 5))
        agreement = float(_num(rolling.get("directional_agreement"), 0.0) or 0.0)
        coverage = float(_num(rolling.get("evidence_coverage"), 0.0) or 0.0)
        evidence_state = str(snap.get("q15_v92_evidence_state") or "UNKNOWN")
        relation = str(snap.get("q15_v92_checkpoint_relation") or "UNKNOWN")
        decision = str(snap.get("q15_v92_decision") or "WATCH")
        blockers = list(snap.get("q15_v92_blockers") or [])
        decisions.append(decision)

        candidate_lines.append(f"{index}. <b>{asset} {side}</b>")
        candidate_lines.append(
            f"{checkpoint_name} probability: {_fmt_pct(side_p)} | "
            f"conservative: {_fmt_pct(conservative)}"
        )
        candidate_lines.append(
            f"{side} ask: {_fmt_cents(ask)} | costs: {_fmt_cents(costs)}"
        )
        candidate_lines.append(
            f"Conservative net edge: <b>{_fmt_cents(edge, signed=True)}</b> | "
            f"required: {_fmt_cents(base_required)}"
        )
        if _num(adjusted_required) is not None and _num(base_required) is not None and float(adjusted_required) > float(base_required):
            candidate_lines.append(
                f"Adjusted requirement: {_fmt_cents(adjusted_required)} "
                f"(checkpoint uncertainty penalty included)"
            )
        candidate_lines.append(
            f"Directional observations: {directional}/{window} | "
            f"agreement: {agreement * 100:.0f}% | coverage: {coverage * 100:.0f}%"
        )
        candidate_lines.append(f"Evidence: {_human_status(evidence_state)}")
        if checkpoint_name == "10M":
            candidate_lines.append(f"15M relationship: {_human_status(relation)}")
        candidate_lines.append(f"Decision: <b>{_human_status(decision)}</b>")
        if blockers:
            candidate_lines.append(
                "Main blocker: " + _human_status(str(blockers[0]))
            )
        if decision == "WATCH":
            candidate_lines.append(
                "Next trigger: collect enough directional observations while edge and price remain valid."
            )
        candidate_lines.append("")

    if any(decision == "READY" for decision in decisions):
        header = f"✅ {checkpoint_name} CHECK — ENTRY RECOMMENDED"
        result = "At least one candidate passed direction, evidence, and executable-value requirements."
    elif any(decision == "WATCH" for decision in decisions):
        header = f"👀 {checkpoint_name} CHECK — WATCH, NO ENTRY YET"
        result = "At least one candidate has a directional thesis but still needs stronger evidence or execution quality."
    else:
        header = f"🛑 {checkpoint_name} CHECK — NO TRADE"
        result = "No candidate passed the current direction, evidence, conflict, and executable-value requirements."

    lines = [header, ""]
    lines.extend(candidate_lines or ["No current candidate snapshots were available.", ""])
    if checkpoint_name == "15M":
        lines.append("15M is evaluated independently; no future 10M prediction is required.")
    else:
        lines.append("10M uses the current direction and treats 15M as a stability input, not a universal veto.")
    lines.append("Result: " + result)
    lines.append(DISCLAIMER)
    return "\n".join(lines).strip()


def _extract(pattern: str, text: str, default: str = "n/a") -> str:
    match = re.search(pattern, text, flags=re.I)
    return match.group(1).strip() if match else default


def _format_hourly(message: str) -> str:
    text = _strip_disclaimers(str(message or ""))
    overall = _extract(r"Overall:\s*([^\n]+)", text)
    realized = _extract(r"Realized:\s*([^\n•]+)", text)
    unique_contracts = _extract(r"Unique contracts:\s*(\d+)", text, "n/a")
    checkpoint_decisions = _extract(r"unique checkpoint decisions:\s*(\d+)", text, "n/a")
    unresolved = _extract(r"(?:unresolved records|pending)\s*[=:]\s*(\d+)", text, "n/a")
    no_new = "No new resolved contracts this hour." if "NO NEW RESOLVED" in text.upper() else ""

    stats = _ACTIVE_POLICY.decision_stats() if _ACTIVE_POLICY is not None else {
        "total_unique_decisions": 0,
        "checkpoint_counts": {},
        "decision_counts": {},
        "current_version_ready_entries": 0,
        "current_version_watch_decisions": 0,
        "current_live_entry_path": "NOT_YET_OBSERVED",
        "main_bottleneck": {"reason": "NO_CURRENT_PROCESS_DATA", "count": 0},
    }
    checkpoint_counts = stats.get("checkpoint_counts") or {}
    decision_counts = stats.get("decision_counts") or {}
    bottleneck = stats.get("main_bottleneck") or {}

    lines = ["📊 <b>Hourly Operational Status</b>"]
    if no_new:
        lines.append(no_new)
    lines.extend([
        "",
        "<b>Current V9.2 live flow</b>",
        f"Unique V9.2 decisions: {stats.get('total_unique_decisions', 0)}",
        f"15M: {checkpoint_counts.get('15M', 0)} | 10M: {checkpoint_counts.get('10M', 0)}",
        f"WATCH: {stats.get('current_version_watch_decisions', 0)} | "
        f"ENTRY_RECOMMENDED: {stats.get('current_version_ready_entries', 0)}",
        f"Current live entry path: {str(stats.get('current_live_entry_path', 'UNKNOWN')).replace('_', ' ')}",
        f"Main current-version bottleneck: "
        f"{_human_status(str(bottleneck.get('reason', 'NONE')))} "
        f"({int(bottleneck.get('count', 0) or 0)})",
        "",
        "<b>Historical / mixed-version results</b>",
        f"Overall: {overall}",
        f"Realized: {realized}",
        f"Unique contracts reported upstream: {unique_contracts}",
        f"Unique checkpoint decisions reported upstream: {checkpoint_decisions}",
        f"Raw or unresolved records reported upstream: {unresolved}",
        "Historical entry counts may include older model versions unless their database rows are version-scoped.",
        "",
        "<b>Interpretation</b>",
        "15M/10M agreement is no longer a universal veto.",
        "Missing or uncertain 15M history adds a penalty; weak conflict adds a larger penalty; only strong conflict blocks entry.",
        "Shadow learning remains observational and does not change live thresholds.",
        DISCLAIMER,
    ])
    return "\n".join(lines).strip()


def format_telegram_message(message: Any) -> str:
    text = str(message or "")
    upper = text.upper()
    if "15M EARLY CHECK" in upper or "10M FINAL CHECK" in upper:
        result = _format_checkpoint(text)
    elif "HOURLY REPORT" in upper or "HOURLY OPERATIONAL STATUS" in upper:
        result = _format_hourly(text)
    else:
        result = _strip_disclaimers(text)
        if result and DISCLAIMER not in result and any(
            token in upper for token in ("WATCH —", "ENTRY SIGNAL", "PAPER SIGNAL")
        ):
            result += "\n" + DISCLAIMER
    return result if len(result) <= 4000 else result[:3990] + "…"
