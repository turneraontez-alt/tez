"""Nonblocking official spot REST top-of-book evidence reservoir.

This collector is deliberately isolated from every RTI decision and feature
builder.  The exact sampler only submits immutable market identities after its
existing stage quote and spot context are frozen.  Worker threads then make a
new public REST request and persist normalized, hash-bound evidence to a
separate WAL database.  There are no outcome, score, alert, or order surfaces.
"""
from __future__ import annotations

import atexit
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import queue
import sqlite3
import threading
import time
from typing import Any, Callable, Mapping

import requests

from q15_upgrade.strategy_bots import (
    rti_spot_rest_top_book_reservoir_identity as identity,
)
logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / identity.DATABASE_RELATIVE_PATH
ASSETS = frozenset({"BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP"})
STAGE_DELAY_SECONDS = {
    "13M": 0.0,
    "12M30S": 30.0,
    "12M": 60.0,
    "11M30S": 90.0,
}
DEPTH_SCOPE = "OFFICIAL_TOP_OF_BOOK_LEVEL_1"
EVIDENCE_COLUMNS = (
    "protocol_id", "protocol_sha256", "schema_version", "submitted_at",
    "request_started_at", "received_at", "target_at",
    "request_start_offset_seconds", "response_latency_seconds",
    "receive_offset_seconds", "asset", "ticker", "close_time", "stage",
    "provider", "symbol", "quote_currency", "depth_scope", "status",
    "failure_reason", "http_status", "source_timestamp",
    "source_mutation_age_seconds", "source_sequence", "best_bid", "bid_size",
    "best_ask", "ask_size", "mid", "spread_bps", "top_imbalance",
)
_EXPECTED_DB_COLUMNS = frozenset({
    "id", "created_at", "evidence_json", "evidence_sha256",
    *EVIDENCE_COLUMNS,
})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spot_rest_top_book (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    protocol_id TEXT NOT NULL,
    protocol_sha256 TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    created_at REAL NOT NULL,
    submitted_at REAL NOT NULL,
    request_started_at REAL NOT NULL,
    received_at REAL NOT NULL,
    target_at REAL NOT NULL,
    request_start_offset_seconds REAL NOT NULL,
    response_latency_seconds REAL NOT NULL,
    receive_offset_seconds REAL NOT NULL,
    asset TEXT NOT NULL,
    ticker TEXT NOT NULL,
    close_time REAL NOT NULL,
    stage TEXT NOT NULL,
    provider TEXT NOT NULL,
    symbol TEXT NOT NULL,
    quote_currency TEXT NOT NULL,
    depth_scope TEXT NOT NULL,
    status TEXT NOT NULL,
    failure_reason TEXT,
    http_status INTEGER,
    source_timestamp REAL,
    source_mutation_age_seconds REAL,
    source_sequence TEXT,
    best_bid REAL,
    bid_size REAL,
    best_ask REAL,
    ask_size REAL,
    mid REAL,
    spread_bps REAL,
    top_imbalance REAL,
    evidence_json TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    UNIQUE(ticker, close_time, stage)
);
CREATE INDEX IF NOT EXISTS idx_spot_rest_book_close_stage
    ON spot_rest_top_book(close_time, stage, asset);
"""


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _sha(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _source_timestamp(value: Any) -> float | None:
    number = _num(value)
    if number is not None:
        if number > 10_000_000_000.0:
            number /= 1000.0
        return number
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def _same(left: Any, right: Any) -> bool:
    left_num = _num(left)
    right_num = _num(right)
    if left_num is not None or right_num is not None:
        return (
            left_num is not None
            and right_num is not None
            and abs(left_num - right_num) <= 1e-9
        )
    return left == right


def load_protocol(path: Path | None = None) -> dict[str, Any]:
    target = (
        ROOT / identity.PROTOCOL_RELATIVE_PATH if path is None else Path(path)
    )
    try:
        protocol = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("spot_rest_book_protocol_unreadable") from exc
    boundary = dict(protocol.get("prospective_boundary") or {})
    capture = dict(protocol.get("capture_contract") or {})
    storage = dict(protocol.get("storage_contract") or {})
    noninterference = dict(protocol.get("noninterference") or {})
    usage = dict(protocol.get("usage") or {})
    v1_exclusion = dict(protocol.get("v1_terminal_exclusion") or {})
    forbidden_usage = (
        "outcome_access_allowed",
        "label_access_allowed",
        "model_fit_allowed",
        "probability_scoring_allowed",
        "threshold_selection_allowed",
        "paper_artifact_allowed",
        "notifications_allowed",
        "automatic_promotion_allowed",
        "real_trading_allowed",
    )
    frozen_sources = {
        asset: (
            str(values.get("provider") or ""),
            str(values.get("symbol") or ""),
            str(values.get("quote_currency") or ""),
        )
        for asset, values in dict(
            capture.get("source_identity_by_asset") or {}
        ).items()
        if isinstance(values, Mapping)
    }
    frozen_requests = {
        provider: (
            str(values.get("method") or ""),
            str(values.get("url_template") or ""),
            tuple(sorted(
                (str(key), str(value))
                for key, value in dict(values.get("query") or {}).items()
            )),
        )
        for provider, values in dict(
            capture.get("request_contract_by_provider") or {}
        ).items()
        if isinstance(values, Mapping)
    }
    expected_requests = {
        provider: (method, url, tuple(sorted(query)))
        for provider, (method, url, query) in identity.REQUEST_CONTRACTS.items()
    }
    if (
        _sha(protocol) != identity.PROTOCOL_SHA256
        or protocol.get("protocol_id") != identity.PROTOCOL_ID
        or protocol.get("protocol_status")
        != "FROZEN_BEFORE_FIRST_V2_ELIGIBLE_REST_BOOK_EVIDENCE"
        or protocol.get("eligible_v2_rows_before_freeze") != 0
        or v1_exclusion.get("protocol_sha256")
        != "291fb660cc05135704b8983f0644ce3253bb9de407bb7feb4d32f36436ee104c"
        or v1_exclusion.get("row_count") != 28
        or v1_exclusion.get("rows_receive_v2_credit") is not False
        or v1_exclusion.get("outcomes_inspected") is not False
        or _num(boundary.get("strictly_after_close_time"))
        != identity.PROSPECTIVE_AFTER_CLOSE_TIME
        or _num(boundary.get("first_eligible_close_time"))
        != identity.FIRST_ELIGIBLE_CLOSE_TIME
        or boundary.get("historical_backfill_allowed") is not False
        or capture.get("schema_version") != identity.SCHEMA_VERSION
        or set(capture.get("assets") or ()) != ASSETS
        or frozen_sources != dict(identity.SOURCE_IDENTITIES)
        or frozen_requests != expected_requests
        or tuple(capture.get("stages") or ()) != tuple(STAGE_DELAY_SECONDS)
        or _num(capture.get("maximum_request_start_offset_seconds"))
        != identity.MAX_REQUEST_START_OFFSET_SECONDS
        or _num(capture.get("maximum_response_latency_seconds"))
        != identity.MAX_RESPONSE_LATENCY_SECONDS
        or _num(capture.get("maximum_receive_offset_seconds"))
        != identity.MAX_RECEIVE_OFFSET_SECONDS
        or _num(capture.get("maximum_exchange_clock_lead_seconds"))
        != identity.MAX_EXCHANGE_CLOCK_LEAD_SECONDS
        or capture.get("local_request_and_receive_clock_is_freshness_authority")
        is not True
        or capture.get("exchange_timestamp_never_establishes_feature_availability")
        is not True
        or storage.get("database") != identity.DATABASE_RELATIVE_PATH
        or storage.get("outcome_columns_forbidden") is not True
        or noninterference.get("submit_is_nonblocking") is not True
        or noninterference.get("used_by_v21") is not False
        or noninterference.get("changes_existing_decisions") is not False
        or usage.get("record_only") is not True
        or any(usage.get(key) is not False for key in forbidden_usage)
    ):
        raise ValueError("spot_rest_book_protocol_identity_or_safety_invalid")
    return protocol


@dataclass(frozen=True)
class _Job:
    asset: str
    ticker: str
    close_time: float
    stage: str
    target_at: float
    submitted_at: float
    provider: str
    symbol: str
    quote_currency: str

    @property
    def key(self) -> tuple[str, float, str]:
        return self.ticker, self.close_time, self.stage


class _HTTPStatusError(RuntimeError):
    def __init__(self, provider: str, status: int):
        super().__init__(f"{provider}_http_{status}")
        self.http_status = int(status)


class SpotRESTTopBookReservoir:
    """Bounded asynchronous capture and immutable WAL persistence."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        db_path: str | Path = DEFAULT_DB,
        worker_count: int = 7,
        request_get: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.perf_counter,
    ):
        self.enabled = bool(enabled)
        self.db_path = Path(db_path)
        self.worker_count = max(1, min(7, int(worker_count)))
        self._request_get = request_get or requests.get
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._queue: queue.Queue[_Job] = queue.Queue(maxsize=128)
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._threads: list[threading.Thread] = []
        self._active_keys: set[tuple[str, float, str]] = set()
        self._completed_keys: set[tuple[str, float, str]] = set()
        self._started = False
        self._protocol_valid = False
        self._submitted = 0
        self._accepted_rows = 0
        self._failed_rows = 0
        self._rejected = 0
        self._duplicate_submissions = 0
        self._identity_conflicts = 0
        self._last_capture_at: float | None = None
        self._last_success_at: float | None = None
        self._last_error: str | None = None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=2.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=2000")
        return conn

    def _initialize_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(spot_rest_top_book)")
            }
            if columns != _EXPECTED_DB_COLUMNS:
                raise ValueError("spot_rest_book_database_schema_invalid")
            unique_identities = set()
            for index_row in conn.execute("PRAGMA index_list(spot_rest_top_book)"):
                if not bool(index_row[2]):
                    continue
                index_name = str(index_row[1]).replace("'", "''")
                unique_identities.add(tuple(
                    str(info[2])
                    for info in conn.execute(
                        f"PRAGMA index_info('{index_name}')"
                    )
                ))
            if ("ticker", "close_time", "stage") not in unique_identities:
                raise ValueError(
                    "spot_rest_book_unique_identity_constraint_missing"
                )
            rows = conn.execute(
                f"SELECT {','.join(EVIDENCE_COLUMNS)},evidence_json,"
                "evidence_sha256 FROM spot_rest_top_book"
            ).fetchall()
        for row in rows:
            raw_json = str(row["evidence_json"] or "")
            try:
                evidence = json.loads(raw_json)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "existing_spot_rest_book_evidence_invalid"
                ) from exc
            canonical = _canonical(evidence) if isinstance(evidence, Mapping) else ""
            if (
                not isinstance(evidence, Mapping)
                or set(evidence) != set(EVIDENCE_COLUMNS)
                or canonical != raw_json
                or hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                != row["evidence_sha256"]
                or any(
                    not _same(row[column], evidence.get(column))
                    for column in EVIDENCE_COLUMNS
                )
                or evidence.get("protocol_id") != identity.PROTOCOL_ID
                or evidence.get("protocol_sha256") != identity.PROTOCOL_SHA256
                or evidence.get("schema_version") != identity.SCHEMA_VERSION
            ):
                raise ValueError("existing_spot_rest_book_evidence_invalid")
        self._completed_keys = {
            (str(row["ticker"]), float(row["close_time"]), str(row["stage"]))
            for row in rows
        }
        self._accepted_rows = sum(row["status"] == "OK" for row in rows)
        self._failed_rows = sum(row["status"] != "OK" for row in rows)

    def start(self) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            if self._started and all(thread.is_alive() for thread in self._threads):
                return True
            try:
                load_protocol()
                self._initialize_db()
            except Exception as exc:
                self._protocol_valid = False
                self._last_error = f"{type(exc).__name__}:{exc}"[:200]
                raise
            self._protocol_valid = True
            self._stop.clear()
            self._threads = [
                threading.Thread(
                    target=self._run_worker,
                    name=f"q15-spot-rest-book-{index + 1}",
                    daemon=True,
                )
                for index in range(self.worker_count)
            ]
            for thread in self._threads:
                thread.start()
            self._started = True
        return True

    def stop(self) -> None:
        self._stop.set()

    @staticmethod
    def _expected_target(close_time: float, stage: str) -> float:
        return close_time - 780.0 + STAGE_DELAY_SECONDS[stage]

    def submit(
        self,
        *,
        asset: str,
        ticker: str,
        close_time: float,
        stage: str,
        target_at: float,
        submitted_at: float | None = None,
    ) -> bool:
        """Queue a point-in-time request without network or database I/O."""
        if not self.enabled or not self._started or not self._protocol_valid:
            return False
        asset_key = str(asset or "").upper()
        ticker_key = str(ticker or "")
        stage_key = str(stage or "").upper()
        close = _num(close_time)
        target = _num(target_at)
        submitted = self._clock() if submitted_at is None else _num(submitted_at)
        source = identity.SOURCE_IDENTITIES.get(asset_key)
        # Expected calls before the frozen boundary are inert, not operational
        # errors.  They receive no row and do not pollute rejection health.
        if close is not None and close <= identity.PROSPECTIVE_AFTER_CLOSE_TIME:
            return False
        if (
            asset_key not in ASSETS
            or not ticker_key
            or stage_key not in STAGE_DELAY_SECONDS
            or close is None
            or target is None
            or submitted is None
            or source is None
            or abs(target - self._expected_target(close, stage_key)) > 1e-6
            or submitted < target
            or submitted - target > identity.MAX_REQUEST_START_OFFSET_SECONDS
        ):
            with self._lock:
                self._rejected += 1
            return False
        provider, symbol, quote_currency = source
        job = _Job(
            asset=asset_key,
            ticker=ticker_key,
            close_time=close,
            stage=stage_key,
            target_at=target,
            submitted_at=submitted,
            provider=str(provider),
            symbol=str(symbol),
            quote_currency=str(quote_currency),
        )
        with self._lock:
            if job.key in self._active_keys or job.key in self._completed_keys:
                self._duplicate_submissions += 1
                return False
            self._active_keys.add(job.key)
            try:
                self._queue.put_nowait(job)
            except queue.Full:
                self._active_keys.discard(job.key)
                self._rejected += 1
                self._last_error = "spot_rest_book_queue_full"
                return False
            self._submitted += 1
        return True

    @staticmethod
    def _top(levels: Any) -> tuple[float | None, float | None]:
        if not isinstance(levels, list) or not levels:
            return None, None
        level = levels[0]
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            return None, None
        return _num(level[0]), _num(level[1])

    def _fetch(self, job: _Job) -> tuple[dict[str, Any], int | None]:
        method, url_template, query_template = identity.REQUEST_CONTRACTS[
            job.provider
        ]
        if method != "GET":
            raise ValueError("unsupported_spot_request_method")
        url = url_template.format(symbol=job.symbol)
        params = {
            key: value.format(symbol=job.symbol)
            for key, value in query_template
        }
        if job.provider == "coinbase":
            response = self._request_get(
                url,
                params=params,
                timeout=(0.35, 1.5),
                headers={"User-Agent": "q15-rti-research-reservoir/1"},
                allow_redirects=False,
            )
            status = int(response.status_code)
            if status != 200:
                raise _HTTPStatusError("coinbase", status)
            raw = response.json()
            if not isinstance(raw, Mapping):
                raise ValueError("coinbase_response_not_object")
            bid, bid_size = self._top(raw.get("bids"))
            ask, ask_size = self._top(raw.get("asks"))
            return {
                "best_bid": bid,
                "bid_size": bid_size,
                "best_ask": ask,
                "ask_size": ask_size,
                "source_timestamp": _source_timestamp(raw.get("time")),
                "source_sequence": raw.get("sequence"),
            }, status
        if job.provider == "okx":
            response = self._request_get(
                url,
                params=params,
                timeout=(0.35, 1.5),
                headers={"User-Agent": "q15-rti-research-reservoir/1"},
                allow_redirects=False,
            )
            status = int(response.status_code)
            if status != 200:
                raise _HTTPStatusError("okx", status)
            raw = response.json()
            data = raw.get("data") if isinstance(raw, Mapping) else None
            if (
                not isinstance(raw, Mapping)
                or str(raw.get("code")) != "0"
                or not isinstance(data, list)
                or not data
                or not isinstance(data[0], Mapping)
            ):
                raise ValueError("okx_response_invalid")
            book = data[0]
            bid, bid_size = self._top(book.get("bids"))
            ask, ask_size = self._top(book.get("asks"))
            return {
                "best_bid": bid,
                "bid_size": bid_size,
                "best_ask": ask,
                "ask_size": ask_size,
                "source_timestamp": _source_timestamp(book.get("ts")),
                "source_sequence": book.get("seqId"),
            }, status
        raise ValueError("unsupported_spot_provider")

    def _capture(self, job: _Job) -> dict[str, Any]:
        started = self._clock()
        monotonic_started = self._monotonic_clock()
        parsed: dict[str, Any] = {}
        http_status: int | None = None
        failure_reason: str | None = None
        if (
            started < job.target_at
            or started - job.target_at
            > identity.MAX_REQUEST_START_OFFSET_SECONDS
        ):
            failure_reason = "REQUEST_START_OUTSIDE_FROZEN_WINDOW"
        else:
            try:
                parsed, http_status = self._fetch(job)
            except Exception as exc:  # noqa: BLE001 - retain failed evidence
                if isinstance(exc, _HTTPStatusError):
                    http_status = exc.http_status
                failure_reason = f"{type(exc).__name__}:{exc}"[:200]
        received = self._clock()
        monotonic_received = self._monotonic_clock()
        latency = received - started
        monotonic_latency = monotonic_received - monotonic_started
        start_offset = started - job.target_at
        receive_offset = received - job.target_at
        bid = _num(parsed.get("best_bid"))
        bid_size = _num(parsed.get("bid_size"))
        ask = _num(parsed.get("best_ask"))
        ask_size = _num(parsed.get("ask_size"))
        source_ts = _num(parsed.get("source_timestamp"))
        mutation_age = None if source_ts is None else received - source_ts
        if failure_reason is None and (
            latency < 0.0
            or monotonic_latency < 0.0
            or abs(latency - monotonic_latency) > 0.1
        ):
            failure_reason = "LOCAL_CLOCK_DISCONTINUITY"
        if (
            failure_reason is None
            and monotonic_latency > identity.MAX_RESPONSE_LATENCY_SECONDS
        ):
            failure_reason = "RESPONSE_LATENCY_EXCEEDED"
        if failure_reason is None and receive_offset > identity.MAX_RECEIVE_OFFSET_SECONDS:
            failure_reason = "RECEIVE_OFFSET_EXCEEDED"
        if (
            failure_reason is None
            and source_ts is not None
            and mutation_age < -identity.MAX_EXCHANGE_CLOCK_LEAD_SECONDS
        ):
            failure_reason = "SOURCE_TIMESTAMP_IN_FUTURE"
        if failure_reason is None and (
            bid is None
            or ask is None
            or bid_size is None
            or ask_size is None
            or bid <= 0.0
            or ask <= 0.0
            or bid_size <= 0.0
            or ask_size <= 0.0
            or bid > ask
        ):
            failure_reason = "TWO_SIDED_UNCROSSED_BOOK_REQUIRED"
        mid = None if bid is None or ask is None else (bid + ask) / 2.0
        spread_bps = (
            None
            if mid is None or mid <= 0.0
            else (ask - bid) / mid * 10_000.0
        )
        top_imbalance = (
            None
            if bid_size is None or ask_size is None or bid_size + ask_size <= 0.0
            else (bid_size - ask_size) / (bid_size + ask_size)
        )
        evidence = {
            "protocol_id": identity.PROTOCOL_ID,
            "protocol_sha256": identity.PROTOCOL_SHA256,
            "schema_version": identity.SCHEMA_VERSION,
            "submitted_at": job.submitted_at,
            "request_started_at": started,
            "received_at": received,
            "target_at": job.target_at,
            "request_start_offset_seconds": start_offset,
            "response_latency_seconds": latency,
            "receive_offset_seconds": receive_offset,
            "asset": job.asset,
            "ticker": job.ticker,
            "close_time": job.close_time,
            "stage": job.stage,
            "provider": job.provider,
            "symbol": job.symbol,
            "quote_currency": job.quote_currency,
            "depth_scope": DEPTH_SCOPE,
            "status": "OK" if failure_reason is None else "FAILED",
            "failure_reason": failure_reason,
            "http_status": http_status,
            "source_timestamp": source_ts,
            "source_mutation_age_seconds": mutation_age,
            "source_sequence": (
                None
                if parsed.get("source_sequence") is None
                else str(parsed.get("source_sequence"))
            ),
            "best_bid": bid,
            "bid_size": bid_size,
            "best_ask": ask,
            "ask_size": ask_size,
            "mid": mid,
            "spread_bps": spread_bps,
            "top_imbalance": top_imbalance,
        }
        return evidence

    def _persist(self, evidence: Mapping[str, Any]) -> bool:
        evidence_json = _canonical(evidence)
        evidence_sha = hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
        columns = [*EVIDENCE_COLUMNS, "created_at", "evidence_json", "evidence_sha256"]
        values = [evidence.get(key) for key in EVIDENCE_COLUMNS] + [
            self._clock(), evidence_json, evidence_sha,
        ]
        with self._connect() as conn:
            cursor = conn.execute(
                f"INSERT OR IGNORE INTO spot_rest_top_book "
                f"({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                values,
            )
            if cursor.rowcount == 1:
                return True
            existing = conn.execute(
                "SELECT evidence_sha256 FROM spot_rest_top_book "
                "WHERE ticker=? AND close_time=? AND stage=?",
                (
                    evidence.get("ticker"), evidence.get("close_time"),
                    evidence.get("stage"),
                ),
            ).fetchone()
        if existing is None or str(existing[0]) != evidence_sha:
            with self._lock:
                self._identity_conflicts += 1
                self._last_error = "spot_rest_book_identity_conflict"
        return False

    def _run_worker(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            try:
                job = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                evidence = self._capture(job)
                inserted = self._persist(evidence)
                with self._lock:
                    if inserted:
                        self._completed_keys.add(job.key)
                        if evidence.get("status") == "OK":
                            self._accepted_rows += 1
                            self._last_success_at = _num(evidence.get("received_at"))
                        else:
                            self._failed_rows += 1
                        self._last_capture_at = _num(evidence.get("received_at"))
                        self._last_error = evidence.get("failure_reason")
            except Exception as exc:  # noqa: BLE001 - worker remains alive
                with self._lock:
                    self._last_error = f"{type(exc).__name__}:{exc}"[:200]
                logger.warning("spot REST book reservoir worker failed", exc_info=True)
            finally:
                with self._lock:
                    self._active_keys.discard(job.key)
                self._queue.task_done()

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "started": self._started,
                "protocol_valid": self._protocol_valid,
                "protocol_id": identity.PROTOCOL_ID,
                "protocol_sha256": identity.PROTOCOL_SHA256,
                "schema_version": identity.SCHEMA_VERSION,
                "db_path": str(self.db_path),
                "prospective_after_close_time": identity.PROSPECTIVE_AFTER_CLOSE_TIME,
                "first_eligible_close_time": identity.FIRST_ELIGIBLE_CLOSE_TIME,
                "worker_threads_alive": sum(t.is_alive() for t in self._threads),
                "worker_count": self.worker_count,
                "queue_depth": self._queue.qsize(),
                "active_requests": len(self._active_keys),
                "submitted": self._submitted,
                "accepted_rows": self._accepted_rows,
                "failed_rows": self._failed_rows,
                "rejected_submissions": self._rejected,
                "duplicate_submissions": self._duplicate_submissions,
                "identity_conflicts": self._identity_conflicts,
                "last_capture_at": self._last_capture_at,
                "last_success_at": self._last_success_at,
                "last_error": self._last_error,
                "record_only": True,
                "used_by_v21": False,
                "outcome_labels_read": False,
                "model_fit_performed": False,
                "notification_eligible": False,
                "real_trading_allowed": False,
            }


_reservoir: SpotRESTTopBookReservoir | None = None
_reservoir_lock = threading.Lock()


def _enabled() -> bool:
    raw = os.environ.get("Q15_RTI_SPOT_REST_RESERVOIR_ENABLED", "true")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def get_spot_rest_top_book_reservoir() -> SpotRESTTopBookReservoir:
    global _reservoir
    with _reservoir_lock:
        if _reservoir is None:
            db_path = os.environ.get("Q15_RTI_SPOT_REST_RESERVOIR_DB")
            _reservoir = SpotRESTTopBookReservoir(
                enabled=_enabled(), db_path=db_path or DEFAULT_DB
            )
        return _reservoir


def start_spot_rest_top_book_reservoir() -> bool:
    return get_spot_rest_top_book_reservoir().start()


def submit_spot_rest_top_book(**kwargs: Any) -> bool:
    reservoir = _reservoir
    if reservoir is None:
        return False
    return reservoir.submit(**kwargs)


def spot_rest_top_book_health() -> dict[str, Any]:
    reservoir = _reservoir
    if reservoir is None:
        return {
            "enabled": _enabled(),
            "started": False,
            "protocol_id": identity.PROTOCOL_ID,
            "protocol_sha256": identity.PROTOCOL_SHA256,
            "record_only": True,
            "used_by_v21": False,
            "outcome_labels_read": False,
            "model_fit_performed": False,
            "notification_eligible": False,
            "real_trading_allowed": False,
        }
    return reservoir.health()


def reset_spot_rest_top_book_reservoir() -> None:
    """Test hook."""
    global _reservoir
    with _reservoir_lock:
        if _reservoir is not None:
            _reservoir.stop()
        _reservoir = None


atexit.register(reset_spot_rest_top_book_reservoir)
