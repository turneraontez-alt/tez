from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

VERSION = "q15-v9.1-unified-checks-reports"
DISCLAIMER = "Probabilistic paper estimate only; it can be wrong. Read-only monitor — no orders are placed."
ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
_LOG = logging.getLogger(__name__)
_LATEST_LOCK = threading.RLock()
_LATEST: dict[str, dict[str, Any]] = {}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return default
        parsed = float(value)
        return parsed if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def _prob(value: Any) -> float | None:
    parsed = _num(value)
    if parsed is None:
        return None
    if 1.0 < parsed <= 100.0:
        parsed /= 100.0
    if not 0.0 <= parsed <= 1.0:
        return None
    return parsed


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _first(mapping: Mapping[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in mapping and mapping.get(key) is not None:
            return mapping.get(key)
    return None


def _seconds(snapshot: Mapping[str, Any]) -> float | None:
    return _num(_first(snapshot, ("seconds_remaining", "time_remaining_seconds", "secs_remaining")))


def _checkpoint(snapshot: Mapping[str, Any]) -> str:
    seconds = _seconds(snapshot)
    if seconds is None:
        return "UNKNOWN"
    return "10M" if seconds <= 610.0 else "15M"


def _contract_key(asset: str, snapshot: Mapping[str, Any]) -> str:
    raw = _first(snapshot, (
        "ticker", "contract_id", "market_ticker", "market_id", "event_ticker",
    ))
    if raw:
        return str(raw)
    close = _first(snapshot, (
        "close_time", "market_close", "expiration_time", "expected_expiration_time",
    ))
    if close:
        return f"{asset}:{close}"
    seconds = _seconds(snapshot)
    now = _num(snapshot.get("snapshot_timestamp"), time.time()) or time.time()
    if seconds is not None:
        inferred_close = int((now + seconds) // 900 * 900)
        return f"{asset}:inferred:{inferred_close}"
    return f"{asset}:unknown"


def _canonical_p_yes(snapshot: Mapping[str, Any]) -> float | None:
    return _prob(_first(snapshot, (
        "v9_calibrated_p_yes", "calibrated_p_yes", "v9_uncalibrated_p_yes",
        "estimated_fair_probability", "fair_probability", "p_yes",
    )))


def _direct_side(p_yes: float | None, threshold: float) -> str:
    if p_yes is None:
        return "UNCERTAIN"
    if p_yes >= threshold:
        return "YES"
    if p_yes <= 1.0 - threshold:
        return "NO"
    return "UNCERTAIN"


def _side_probability(p_yes: float | None, side: str) -> float | None:
    if p_yes is None:
        return None
    return p_yes if side == "YES" else 1.0 - p_yes if side == "NO" else None


def _human_status(status: str) -> str:
    return status.replace("_", " ").title()


@dataclass(frozen=True)
class RollingMetrics:
    yes: int
    no: int
    neutral: int
    total: int
    directional: int
    agreement: float
    coverage: float
    selected_side: str
    status: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "yes_votes": self.yes,
            "no_votes": self.no,
            "neutral_votes": self.neutral,
            "total_possible_votes": self.total,
            "directional_votes": self.directional,
            "directional_agreement": round(self.agreement, 6),
            "evidence_coverage": round(self.coverage, 6),
            "selected_side": self.selected_side,
            "status": self.status,
        }


class _Persistence:
    """Postgres-through-existing-store with durable SQLite fallback."""

    def __init__(self, store: Any = None, sqlite_path: str | Path | None = None):
        self.store = store
        self.pg = bool(store is not None and getattr(store, "enabled", False) and hasattr(store, "execute") and hasattr(store, "query"))
        self.sqlite_path = Path(sqlite_path or os.getenv("Q15_V91_SQLITE_PATH", "q15_v91_state.sqlite3"))
        self._lock = threading.RLock()
        self.last_error: str | None = None
        self._migrate()

    def _migrate(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS q15_v91_observations (
                observation_key TEXT PRIMARY KEY,
                contract_key TEXT NOT NULL,
                asset TEXT NOT NULL,
                bucket BIGINT NOT NULL,
                observed_at DOUBLE PRECISION NOT NULL,
                side TEXT NOT NULL,
                p_yes DOUBLE PRECISION,
                model_version TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS q15_v91_obs_lookup ON q15_v91_observations(contract_key, asset, bucket DESC)",
            """
            CREATE TABLE IF NOT EXISTS q15_v91_predictions (
                prediction_key TEXT PRIMARY KEY,
                contract_key TEXT NOT NULL,
                asset TEXT NOT NULL,
                checkpoint TEXT NOT NULL,
                side TEXT NOT NULL,
                p_yes DOUBLE PRECISION,
                evidence_status TEXT,
                rolling_json TEXT,
                created_at DOUBLE PRECISION NOT NULL,
                model_version TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS q15_v91_pred_lookup ON q15_v91_predictions(contract_key, asset, checkpoint)",
        ]
        if self.pg:
            ok = True
            for sql in statements:
                if not bool(self.store.execute(sql)):
                    ok = False
                    break
            if ok:
                return
            self.last_error = "postgres_migration_failed; using_sqlite_fallback"
            self.pg = False
        with self._sqlite() as conn:
            for sql in statements:
                conn.execute(sql)
            conn.commit()

    @contextmanager
    def _sqlite(self):
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.sqlite_path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def insert_observation(self, row: Mapping[str, Any]) -> bool:
        values = (
            row["observation_key"], row["contract_key"], row["asset"], int(row["bucket"]),
            float(row["observed_at"]), row["side"], row.get("p_yes"), VERSION,
        )
        if self.pg:
            return bool(self.store.execute(
                "INSERT INTO q15_v91_observations "
                "(observation_key,contract_key,asset,bucket,observed_at,side,p_yes,model_version) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (observation_key) DO NOTHING",
                values,
            ))
        with self._lock, self._sqlite() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO q15_v91_observations "
                "(observation_key,contract_key,asset,bucket,observed_at,side,p_yes,model_version) "
                "VALUES (?,?,?,?,?,?,?,?)", values,
            )
            conn.commit()
        return True

    def recent_observations(self, contract_key: str, asset: str, limit: int) -> list[dict[str, Any]]:
        if self.pg:
            return list(self.store.query(
                "SELECT observation_key,contract_key,asset,bucket,observed_at,side,p_yes,model_version "
                "FROM q15_v91_observations WHERE contract_key=%s AND asset=%s "
                "ORDER BY bucket DESC LIMIT %s", (contract_key, asset, limit),
            ) or [])
        with self._lock, self._sqlite() as conn:
            rows = conn.execute(
                "SELECT observation_key,contract_key,asset,bucket,observed_at,side,p_yes,model_version "
                "FROM q15_v91_observations WHERE contract_key=? AND asset=? "
                "ORDER BY bucket DESC LIMIT ?", (contract_key, asset, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def write_prediction(self, row: Mapping[str, Any]) -> None:
        """Freeze a prediction (write only). Splitting the write from the
        read-back lets the hot pre-enrich path reuse a single batched read
        instead of paying a round-trip per call."""
        values = (
            row["prediction_key"], row["contract_key"], row["asset"], row["checkpoint"],
            row["side"], row.get("p_yes"), row.get("evidence_status"),
            json.dumps(row.get("rolling") or {}, separators=(",", ":")),
            float(row["created_at"]), VERSION,
        )
        if self.pg:
            self.store.execute(
                "INSERT INTO q15_v91_predictions "
                "(prediction_key,contract_key,asset,checkpoint,side,p_yes,evidence_status,rolling_json,created_at,model_version) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (prediction_key) DO NOTHING",
                values,
            )
        else:
            with self._lock, self._sqlite() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO q15_v91_predictions "
                    "(prediction_key,contract_key,asset,checkpoint,side,p_yes,evidence_status,rolling_json,created_at,model_version) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)", values,
                )
                conn.commit()

    def freeze_prediction(self, row: Mapping[str, Any]) -> dict[str, Any]:
        self.write_prediction(row)
        return self.get_prediction(row["contract_key"], row["asset"], row["checkpoint"]) or dict(row)

    @staticmethod
    def _parse_prediction_row(row: Mapping[str, Any]) -> dict[str, Any]:
        out = dict(row)
        if isinstance(out.get("rolling_json"), str):
            try:
                out["rolling"] = json.loads(out["rolling_json"])
            except json.JSONDecodeError:
                out["rolling"] = {}
        return out

    def get_predictions_for(
        self, contract_key: str, asset: str, checkpoints: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        """Fetch several checkpoints' frozen predictions in one round-trip.
        Returns {checkpoint: row}. Equivalent to calling get_prediction per
        checkpoint but collapses N reads into a single query."""
        checkpoints = tuple(checkpoints)
        if not checkpoints:
            return {}
        if self.pg:
            placeholders = ",".join(["%s"] * len(checkpoints))
            rows = self.store.query(
                f"SELECT * FROM q15_v91_predictions WHERE contract_key=%s AND asset=%s "
                f"AND checkpoint IN ({placeholders})",
                (contract_key, asset, *checkpoints),
            ) or []
        else:
            with self._lock, self._sqlite() as conn:
                placeholders = ",".join(["?"] * len(checkpoints))
                rows = conn.execute(
                    f"SELECT * FROM q15_v91_predictions WHERE contract_key=? AND asset=? "
                    f"AND checkpoint IN ({placeholders})",
                    (contract_key, asset, *checkpoints),
                ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            parsed = self._parse_prediction_row(row)
            result[str(parsed.get("checkpoint"))] = parsed
        return result

    def get_prediction(self, contract_key: str, asset: str, checkpoint: str) -> dict[str, Any] | None:
        if self.pg:
            rows = self.store.query(
                "SELECT * FROM q15_v91_predictions WHERE contract_key=%s AND asset=%s AND checkpoint=%s LIMIT 1",
                (contract_key, asset, checkpoint),
            ) or []
            row = dict(rows[0]) if rows else None
        else:
            with self._lock, self._sqlite() as conn:
                found = conn.execute(
                    "SELECT * FROM q15_v91_predictions WHERE contract_key=? AND asset=? AND checkpoint=? LIMIT 1",
                    (contract_key, asset, checkpoint),
                ).fetchone()
                row = dict(found) if found else None
        if row and isinstance(row.get("rolling_json"), str):
            try:
                row["rolling"] = json.loads(row["rolling_json"])
            except json.JSONDecodeError:
                row["rolling"] = {}
        return row

    def counts(self) -> dict[str, Any]:
        if self.pg:
            obs = self.store.query("SELECT COUNT(*) AS n FROM q15_v91_observations") or []
            pred = self.store.query("SELECT COUNT(*) AS n FROM q15_v91_predictions") or []
            return {"observations": int(obs[0]["n"]) if obs else 0, "predictions": int(pred[0]["n"]) if pred else 0, "backend": "postgres"}
        with self._lock, self._sqlite() as conn:
            obs = conn.execute("SELECT COUNT(*) FROM q15_v91_observations").fetchone()[0]
            pred = conn.execute("SELECT COUNT(*) FROM q15_v91_predictions").fetchone()[0]
            return {"observations": int(obs), "predictions": int(pred), "backend": "sqlite"}


class CheckpointPolicyV91:
    """Persistent sequential 15M/10M policy and unified decision overlay."""

    def __init__(self, store: Any = None, sqlite_path: str | Path | None = None):
        self.window = max(3, _env_int("Q15_V91_ROLLING_WINDOW", 5))
        self.bucket_seconds = max(2, _env_int("Q15_V91_OBSERVATION_BUCKET_SECONDS", 12))
        self.side_threshold = _clamp(_env_float("Q15_V91_SIDE_THRESHOLD", 0.58), 0.51, 0.90)
        self.min_votes = max(1, _env_int("Q15_V91_MIN_DIRECTIONAL_VOTES", 3))
        self.min_agreement = _clamp(_env_float("Q15_V91_MIN_DIRECTIONAL_AGREEMENT", 0.72), 0.50, 1.0)
        self.persistence = _Persistence(store, sqlite_path)
        self._lock = threading.RLock()
        self._cycles = 0

    def _observe(self, asset: str, snapshot: Mapping[str, Any], now: float) -> tuple[str, float | None, RollingMetrics, str]:
        contract = _contract_key(asset, snapshot)
        p_yes = _canonical_p_yes(snapshot)
        current_side = _direct_side(p_yes, self.side_threshold)
        bucket = int(now // self.bucket_seconds)
        key = f"{contract}|{asset}|{bucket}|{VERSION}"
        self.persistence.insert_observation({
            "observation_key": key,
            "contract_key": f"{contract}|{_checkpoint(snapshot)}",
            "asset": asset,
            "bucket": bucket,
            "observed_at": now,
            "side": current_side,
            "p_yes": p_yes,
        })
        observation_scope = f"{contract}|{_checkpoint(snapshot)}"
        # Each checkpoint gets its own rolling evidence window. Otherwise the
        # first 10M observation would be contaminated by older 15M votes and
        # could freeze the wrong side before fresh 10M evidence arrives.
        rows = self.persistence.recent_observations(observation_scope, asset, self.window)
        sides = [str(row.get("side") or "UNCERTAIN").upper() for row in rows]
        yes = sides.count("YES")
        no = sides.count("NO")
        neutral = len(sides) - yes - no
        directional = yes + no
        agreement = max(yes, no) / directional if directional else 0.0
        coverage = directional / self.window
        selected = "YES" if yes > no else "NO" if no > yes else "UNCERTAIN"
        if len(rows) < self.window:
            status = "WARMUP_INCOMPLETE"
        elif directional < self.min_votes:
            status = "INSUFFICIENT_DIRECTIONAL_EVIDENCE"
        elif agreement < self.min_agreement:
            status = "DIRECTIONAL_AGREEMENT_LOW"
        elif selected == "UNCERTAIN":
            status = "DIRECTION_UNCERTAIN"
        else:
            status = "OK"
        metrics = RollingMetrics(yes, no, neutral, len(rows), directional, agreement, coverage, selected, status)
        return contract, p_yes, metrics, current_side

    def _prediction_side(self, current_side: str, rolling: RollingMetrics) -> str:
        if rolling.status == "OK" and rolling.selected_side in {"YES", "NO"}:
            return rolling.selected_side
        # Freeze a provisional 15M view even during warmup so the later 10M
        # comparison does not become impossible merely because the app started
        # close to the first checkpoint.
        return current_side if current_side in {"YES", "NO"} else "UNCERTAIN"

    def pre_enrich_all(self, snapshots: Mapping[str, dict], now: float) -> dict[str, dict]:
        output: dict[str, dict] = {}
        for asset, original in snapshots.items():
            if not isinstance(original, dict):
                continue
            snap = dict(original)
            contract, p_yes, rolling, current_side = self._observe(asset, snap, now)
            checkpoint = _checkpoint(snap)
            side = self._prediction_side(current_side, rolling)
            prediction_key = f"{contract}|{asset}|{checkpoint}|{VERSION}"
            constructed = {
                "prediction_key": prediction_key,
                "contract_key": contract,
                "asset": asset,
                "checkpoint": checkpoint,
                "side": side,
                "p_yes": p_yes,
                "evidence_status": rolling.status,
                "rolling": rolling.as_dict(),
                "created_at": now,
            }
            if checkpoint in {"15M", "10M"}:
                self.persistence.write_prediction(constructed)
            # One batched read covers the just-frozen checkpoint plus the prior
            # one, replacing the freeze read-back and the two separate
            # get_prediction calls (6 round-trips/asset -> 4).
            predictions = self.persistence.get_predictions_for(contract, asset, ("15M", "10M"))
            fifteen = predictions.get("15M")
            ten = predictions.get("10M")
            if checkpoint in {"15M", "10M"}:
                frozen = predictions.get(checkpoint) or dict(constructed)
            else:
                frozen = None
            frozen_side = str((frozen or {}).get("side") or side).upper()
            if checkpoint == "15M":
                agreement = "EARLY_ONLY"
                final_side = frozen_side
            elif fifteen is None:
                agreement = "MISSING_PRIOR_CHECKPOINT"
                final_side = "UNCERTAIN"
            else:
                side15 = str(fifteen.get("side") or "UNCERTAIN").upper()
                side10 = str((ten or frozen or {}).get("side") or frozen_side).upper()
                if side15 not in {"YES", "NO"} or side10 not in {"YES", "NO"}:
                    agreement = "UNCERTAIN"
                    final_side = "UNCERTAIN"
                elif side15 == side10:
                    agreement = "SAME"
                    final_side = side10
                else:
                    agreement = "CONFLICT"
                    final_side = "UNCERTAIN"
            evidence_ok = rolling.status == "OK"
            sequence_ok = checkpoint == "15M" or agreement == "SAME"
            snap.update({
                "q15_v91_version": VERSION,
                "q15_v91_contract_key": contract,
                "q15_v91_checkpoint": checkpoint,
                "q15_v91_current_side": current_side,
                "q15_v91_frozen_side": frozen_side,
                "q15_v91_rolling": rolling.as_dict(),
                "rolling_yes_votes": rolling.yes,
                "rolling_no_votes": rolling.no,
                "rolling_neutral_votes": rolling.neutral,
                "rolling_vote_count": self.window,
                "total_possible_votes": self.window,
                "q15_prediction_agreement": agreement,
                "q15_final_result": final_side,
                "q15_consensus_side": frozen_side if frozen_side in {"YES", "NO"} else None,
                "q15_new_entry_allowed": bool(evidence_ok and sequence_ok),
            })
            if checkpoint == "15M":
                snap["q15_15m_side"] = frozen_side
                snap["q15_15m_prediction"] = {"side": frozen_side, "p_yes": (frozen or {}).get("p_yes"), "evidence_status": (frozen or {}).get("evidence_status")}
            else:
                snap["q15_10m_side"] = frozen_side
                snap["q15_10m_prediction"] = {"side": frozen_side, "p_yes": (frozen or {}).get("p_yes"), "evidence_status": (frozen or {}).get("evidence_status")}
            output[asset] = snap
        self._cycles += 1
        return output

    def finalize_all(self, snapshots: Mapping[str, dict], now: float) -> dict[str, dict]:
        output: dict[str, dict] = {}
        for asset, original in snapshots.items():
            if not isinstance(original, dict):
                continue
            snap = dict(original)
            rolling = snap.get("q15_v91_rolling") or {}
            checkpoint = str(snap.get("q15_v91_checkpoint") or _checkpoint(snap))
            agreement = str(snap.get("q15_prediction_agreement") or "UNKNOWN").upper()
            side = str(snap.get("calibrated_edge_side") or snap.get("q15_v91_frozen_side") or "UNCERTAIN").upper()
            trade_status = str(snap.get("calibrated_edge_status") or "INSUFFICIENT_EVIDENCE").upper()
            economics = snap.get("canonical_economics") or {}
            conservative_edge = _num(economics.get("conservative_net_edge_cents"))
            required_edge = _num((snap.get("trade_quality") or {}).get("required_edge_cents"), 0.0) or 0.0
            blockers: list[str] = []
            if side not in {"YES", "NO"}:
                blockers.append("DIRECTION_UNCERTAIN")
            if str(rolling.get("status") or "") != "OK":
                blockers.append(str(rolling.get("status") or "INSUFFICIENT_EVIDENCE"))
            if checkpoint == "10M" and agreement != "SAME":
                blockers.append(agreement if agreement else "MISSING_PRIOR_CHECKPOINT")
            if trade_status != "READY":
                blockers.append(trade_status)
            if conservative_edge is not None and conservative_edge < required_edge:
                primary = "PRICE_NOT_ATTRACTIVE"
            elif "DATA_STALE" in blockers:
                primary = "DATA_STALE"
            elif "CONFLICT" in blockers or "MISSING_PRIOR_CHECKPOINT" in blockers:
                primary = "DIRECTION_UNCERTAIN"
            elif trade_status == "READY" and not blockers:
                primary = "READY"
            elif trade_status in {"PRICE_NOT_ATTRACTIVE", "WAIT_FOR_CONFIRMATION", "WAIT_FOR_FOLLOW_THROUGH"}:
                primary = trade_status
            elif str(rolling.get("status") or "") == "WARMUP_INCOMPLETE":
                primary = "INSUFFICIENT_EVIDENCE"
            else:
                primary = trade_status
            allowed = bool(primary == "READY")
            existing_state = str(snap.get("decision_state") or "").upper()
            if existing_state in {"ENTRY CONFIRMED", "REVERSAL WARNING"}:
                # V9.1 never invalidates an already confirmed position because
                # of a rolling-evidence or presentation-layer change.
                allowed = bool(snap.get("calibrated_edge_entry_allowed", True))
            snap.update({
                "q15_v91_decision": primary,
                "q15_v91_blockers": list(dict.fromkeys(blockers)),
                "q15_v91_entry_allowed": allowed,
                "q15_new_entry_allowed": allowed,
                "calibrated_edge_new_entry_allowed": allowed,
            })
            with _LATEST_LOCK:
                _LATEST[asset] = deepcopy(snap)
            output[asset] = snap
        return output

    def run_cycle(
        self,
        snapshots: Mapping[str, dict],
        now: float,
        ws_health: Mapping[str, Any],
        focus_manager: Any,
        calibrated_edge: Any,
        notifier: Any,
    ) -> dict[str, dict]:
        """Run the checkpoint stage and defer its Telegram send until current-cycle economics exist."""
        _ct = getattr(self, "_chain_timing", None)
        _t0 = time.monotonic()
        prepared = self.pre_enrich_all(snapshots, now)
        if isinstance(_ct, dict):
            _ct["v91_pre_enrich"] = round(time.monotonic() - _t0, 3)
        captured: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        original_send = getattr(notifier, "send", None)
        patched = False

        def intercept(*args: Any, **kwargs: Any) -> Any:
            text = str(args[0] if args else kwargs.get("text", ""))
            upper = text.upper()
            if "15M EARLY CHECK" in upper or "10M FINAL CHECK" in upper:
                captured.append((args, kwargs))
                return True
            return original_send(*args, **kwargs) if callable(original_send) else False

        if callable(original_send):
            try:
                setattr(notifier, "send", intercept)
                patched = True
            except (AttributeError, TypeError):
                _LOG.warning("V9.1 could not defer checkpoint notifier; continuing without buffering")
        completed = False
        result: dict[str, dict] = {}
        try:
            _t = time.monotonic()
            focused = focus_manager.update(prepared, now, ws_health)
            if isinstance(_ct, dict):
                _ct["v91_focus_update"] = round(time.monotonic() - _t, 3)
            _t = time.monotonic()
            enriched = calibrated_edge.enrich_all(focused, now, ws_health)
            if isinstance(_ct, dict):
                _ct["v91_calibrated_edge"] = round(time.monotonic() - _t, 3)
            _t = time.monotonic()
            result = self.finalize_all(enriched, now)
            if isinstance(_ct, dict):
                _ct["v91_finalize_all"] = round(time.monotonic() - _t, 3)
            completed = True
            return result
        finally:
            if patched:
                try:
                    setattr(notifier, "send", original_send)
                except (AttributeError, TypeError):
                    pass
            if callable(original_send):
                for args, kwargs in captured:
                    try:
                        original_send(*args, **kwargs)
                    except Exception:
                        _LOG.exception("V9.1 failed to release deferred checkpoint alert")
            if captured and not completed:
                _LOG.warning("V9.1 released %d raw checkpoint alert(s) after a failed cycle", len(captured))

    def health(self) -> dict[str, Any]:
        return {
            "version": VERSION,
            "enabled": True,
            "read_only": True,
            "persistent_rolling_observations": True,
            "15m_requires_10m": False,
            "10m_compares_saved_15m": True,
            "unified_formatter": True,
            "cycles": self._cycles,
            "storage": self.persistence.counts(),
        }

    def diagnostics(self) -> dict[str, Any]:
        with _LATEST_LOCK:
            latest = deepcopy(_LATEST)
        return {
            **self.health(),
            "configuration": {
                "rolling_window": self.window,
                "observation_bucket_seconds": self.bucket_seconds,
                "side_threshold": self.side_threshold,
                "minimum_directional_votes": self.min_votes,
                "minimum_directional_agreement": self.min_agreement,
            },
            "latest": latest,
        }

    def rolling_status(self) -> dict[str, Any]:
        with _LATEST_LOCK:
            rows = {asset: deepcopy(snap.get("q15_v91_rolling") or {}) for asset, snap in _LATEST.items()}
        return {"version": VERSION, "assets": rows, "read_only": True}

    def predictions_status(self) -> dict[str, Any]:
        with _LATEST_LOCK:
            rows = {
                asset: {
                    "contract_key": snap.get("q15_v91_contract_key"),
                    "checkpoint": snap.get("q15_v91_checkpoint"),
                    "15m": snap.get("q15_15m_prediction"),
                    "10m": snap.get("q15_10m_prediction"),
                    "agreement": snap.get("q15_prediction_agreement"),
                    "final_result": snap.get("q15_final_result"),
                    "decision": snap.get("q15_v91_decision"),
                }
                for asset, snap in _LATEST.items()
            }
        return {"version": VERSION, "assets": rows, "read_only": True}


def _candidate_order(message: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for match in re.finditer(r"(?mi)^\s*\d+\.\s+(BTC|ETH|SOL|XRP|DOGE|BNB|HYPE)\s+(YES|NO)\b", message):
        pair = (match.group(1).upper(), match.group(2).upper())
        if pair not in found:
            found.append(pair)
    if found:
        return found[:3]
    for asset in ASSETS:
        if re.search(rf"\b{asset}\b", message.upper()):
            with _LATEST_LOCK:
                snap = _LATEST.get(asset) or {}
            side = str(snap.get("calibrated_edge_side") or snap.get("q15_v91_frozen_side") or "UNCERTAIN").upper()
            found.append((asset, side))
    return found[:3]


def _fmt_pct(value: Any) -> str:
    p = _prob(value)
    return "n/a" if p is None else f"{p * 100:.1f}%"


def _fmt_cents(value: Any, signed: bool = False) -> str:
    parsed = _num(value)
    if parsed is None:
        return "n/a"
    return f"{parsed:+.2f}¢" if signed else f"{parsed:.2f}¢"


def _strip_disclaimers(text: str) -> str:
    lines = []
    for line in text.splitlines():
        plain = re.sub(r"<[^>]+>", "", line).strip()
        if "Probabilistic paper estimate only" in plain or "Read-only monitor" in plain:
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _format_checkpoint(message: str) -> str:
    # Replace, rather than append to, old V6/V9 report blocks.
    base = re.split(r"\n\s*(?:<b>)?(?:PROFESSIONAL TRADE QUALITY|V9 TRADE QUALITY)", message, maxsplit=1, flags=re.I)[0]
    header_match = re.search(r"(?mi)^(.*(?:15M EARLY CHECK|10M FINAL CHECK).*)$", base)
    header = header_match.group(1).strip() if header_match else "🛑 Q15 CHECK"
    candidates = _candidate_order(base)
    lines = [header, ""]
    any_ready = False
    any_value = False
    for index, (asset, message_side) in enumerate(candidates, start=1):
        with _LATEST_LOCK:
            snap = deepcopy(_LATEST.get(asset) or {})
        side = str(snap.get("calibrated_edge_side") or snap.get("q15_v91_frozen_side") or message_side).upper()
        p_yes = _prob(snap.get("v9_calibrated_p_yes") or snap.get("calibrated_p_yes"))
        side_p = _prob(snap.get("calibrated_side_probability"))
        if side_p is None:
            side_p = _side_probability(p_yes, side)
        conservative = _prob(snap.get("conservative_side_probability"))
        economics = snap.get("canonical_economics") or {}
        ask = economics.get("executable_entry_ask_cents", snap.get("entry_ask"))
        edge = economics.get("conservative_net_edge_cents", snap.get("conservative_edge_after_costs_cents"))
        max_entry = economics.get("maximum_entry_price_cents", snap.get("max_entry_price"))
        costs = economics.get("total_estimated_cost_cents")
        rolling = snap.get("q15_v91_rolling") or {}
        checkpoint = str(snap.get("q15_v91_checkpoint") or _checkpoint(snap))
        agreement = str(snap.get("q15_prediction_agreement") or "UNKNOWN")
        decision = str(snap.get("q15_v91_decision") or snap.get("calibrated_edge_status") or "INSUFFICIENT_EVIDENCE")
        any_ready = any_ready or decision == "READY"
        any_value = any_value or ((_num(edge) or -999.0) >= 0.0)
        lines.append(f"{index}. <b>{asset} {side}</b>")
        lines.append(f"{checkpoint} probability: {_fmt_pct(side_p)} | conservative: {_fmt_pct(conservative)}")
        lines.append(f"{side} ask: {_fmt_cents(ask)} | costs: {_fmt_cents(costs)}")
        lines.append(f"Conservative net edge: <b>{_fmt_cents(edge, signed=True)}</b> | max entry: {_fmt_cents(max_entry)}")
        observed = int(_num(rolling.get("total_possible_votes"), 0) or 0)
        window = max(observed, _env_int("Q15_V91_ROLLING_WINDOW", 5))
        lines.append(
            f"Rolling evidence: {observed}/{window} | directional agreement: "
            f"{float(_num(rolling.get('directional_agreement'), 0.0) or 0.0) * 100:.0f}% | "
            f"coverage: {float(_num(rolling.get('evidence_coverage'), 0.0) or 0.0) * 100:.0f}%"
        )
        if checkpoint == "15M":
            lines.append("Checkpoint sequence: 15M is evaluated independently; no future 10M vote is required.")
        else:
            lines.append(f"15M comparison: {agreement.replace('_', ' ')}")
        lines.append(f"Decision: <b>{_human_status(decision)}</b>")
        notes = snap.get("q15_v91_blockers") or []
        if notes:
            lines.append("Notes: " + "; ".join(_human_status(str(item)) for item in notes[:3]))
        lines.append("")
    if not candidates:
        lines.append("No current candidate snapshots were available for unified formatting.")
    elif any_ready:
        lines.append("Result: At least one candidate passed the unified V9.1 gate.")
    elif not any_value:
        lines.append("Result: No candidate offered positive executable value after estimated costs.")
    else:
        lines.append("Result: No candidate passed all directional, sequencing, and execution gates.")
    lines.append(DISCLAIMER)
    return "\n".join(lines).strip()


def _format_hourly(message: str) -> str:
    text = _strip_disclaimers(message)
    # Clarify the overloaded pending label.
    text = re.sub(r"\(n=(\d+),\s*(\d+)\s+pending\)", r"(resolved n=\1; unresolved records=\2)", text)
    coefficient_pattern = re.compile(r"(?mi)^\s*[•\-]\s*(.+?):\s*coefficient\s*([+-]?\d+(?:\.\d+)?)\s*$")
    coefficients = [(name.strip(), float(value)) for name, value in coefficient_pattern.findall(text)]
    if coefficients:
        text = re.sub(
            r"(?is)\n*<b>?Factors associated with higher loss risk:?</b>?\s*(?:\n\s*[•\-].*?coefficient\s*[+-]?\d+(?:\.\d+)?\s*)+",
            "",
            text,
        )
        higher = [(name, value) for name, value in coefficients if value > 0]
        lower = [(name, value) for name, value in coefficients if value < 0]
        section = ["", "<b>Exploratory outcome associations</b>"]
        if higher:
            section.append("Associated with higher observed loss risk:")
            section.extend(f"• {name}: {value:+.3f}" for name, value in higher)
        if lower:
            section.append("Associated with lower observed loss risk:")
            section.extend(f"• {name}: {value:+.3f}" for name, value in lower)
        section.append("Association does not prove causation; small samples can reverse coefficient signs.")
        text += "\n" + "\n".join(section)
    text = text.replace("<b>Learning adjustments active:</b>", "<b>Shadow adjustments under evaluation — not applied to production alerts:</b>")
    text = text.replace("Learning adjustments active:", "Shadow adjustments under evaluation — not applied to production alerts:")
    text = text.replace("[SHADOW SUPPRESS]", "[SHADOW-ONLY: would suppress]")
    text += "\n\nSample status: exploratory until enough uniquely resolved checkpoint predictions are available."
    text += "\n" + DISCLAIMER
    return text.strip()


def format_telegram_message(message: Any) -> str:
    """One formatter for checkpoint and hourly reports; never appends duplicates."""
    text = str(message or "")
    upper = text.upper()
    if "15M EARLY CHECK" in upper or "10M FINAL CHECK" in upper:
        result = _format_checkpoint(text)
    elif "HOURLY REPORT" in upper:
        result = _format_hourly(text)
    else:
        result = _strip_disclaimers(text)
        if result and DISCLAIMER not in result and any(token in upper for token in ("WATCH —", "ENTRY SIGNAL", "PAPER SIGNAL")):
            result += "\n" + DISCLAIMER
    return result if len(result) <= 4000 else result[:3990] + "…"
