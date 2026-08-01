"""Kalshi crypto settlement-index collector.

Kalshi's current 15-minute crypto market metadata says each market settles from
CF Benchmarks' corresponding Real Time Index (RTI): ``rules_primary`` names the
asset index (for example BRTI / ETHUSD_RTI), and ``rules_secondary`` says the
final value is the average of 60 RTI prices collected in the last minute before
expiration, rounded per asset. Kalshi's public help article also states that
crypto contracts are settled by averaging 60 seconds of CFB RTIs. The Kalshi
CF Benchmarks value-feed docs describe the authenticated read-only websocket
channel used here and its ``avg_60s_data`` / final-minute average fields:
https://docs.kalshi.com/websockets/cfbenchmarks-value

This module only subscribes/GETs reference data. It has no order methods and is
default-OFF behind ``Q15_FEED_SETTLE_INDEX``. When the authenticated feed is not
available, callers get ``None`` fields instead of a spot-price substitute.
"""
from __future__ import annotations

import asyncio
from collections import deque
import inspect
import json
import logging
import math
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from kalshi_auth import KalshiSigner
from q15_upgrade.ws_client import DEMO_URL, PROD_URL, WS_PATH

logger = logging.getLogger(__name__)

try:
    import websockets

    _HAVE_WS = True
except Exception:  # pragma: no cover - optional dependency guard
    websockets = None
    _HAVE_WS = False


DEFAULT_INDEX_IDS = {
    "BTC": "BRTI",
    "ETH": "ETHUSD_RTI",
    "SOL": "SOLUSD_RTI",
    "XRP": "XRPUSD_RTI",
    "DOGE": "DOGEUSD_RTI",
    "BNB": "BNBUSD_RTI",
    "HYPE": "HYPEUSD_RTI",
}

_INDEX_ID_ALIASES = {
    index_id.replace("_", ""): index_id
    for index_id in DEFAULT_INDEX_IDS.values()
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settlement_index_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    asset TEXT NOT NULL,
    index_id TEXT NOT NULL,
    index_px REAL NOT NULL,
    index_age_s REAL NOT NULL,
    avg_60s_px REAL,
    avg_60s_window_size INTEGER,
    final_minute_avg_px REAL,
    final_minute_window_size INTEGER,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_settlement_index_asset_ts
    ON settlement_index_ticks(asset, ts);
"""


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


def _enabled() -> bool:
    return _env_bool("Q15_FEED_SETTLE_INDEX", False)


def _db_path() -> str:
    return os.environ.get("Q15_FEED_SETTLE_INDEX_DB", "data/q15_settlement_index_v1.sqlite3")


def _stale_seconds() -> float:
    return _env_float("Q15_FEED_SETTLE_INDEX_STALE_SECONDS", 5.0, minimum=0.5)


def _queue_size() -> int:
    try:
        return max(16, int(float(os.environ.get("Q15_FEED_SETTLE_INDEX_QUEUE", "4096"))))
    except (TypeError, ValueError):
        return 4096


def _configured_index_ids() -> dict[str, str]:
    """Parse ``ASSET=INDEX`` pairs, falling back to rulebook-derived defaults."""
    out = dict(DEFAULT_INDEX_IDS)
    raw = os.environ.get("Q15_FEED_SETTLE_INDEX_IDS", "").strip()
    if not raw:
        return out
    for part in raw.split(","):
        if "=" not in part:
            continue
        asset, index_id = (piece.strip().upper() for piece in part.split("=", 1))
        if asset and index_id:
            # Accept the pre-July-2026 local spelling without underscores, but
            # always send Kalshi's canonical CF Benchmarks identifier.
            out[asset] = _INDEX_ID_ALIASES.get(index_id, index_id)
    return out


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _ms_to_s(value: Any) -> float | None:
    raw = _num(value)
    if raw is None:
        return None
    while raw > 10_000_000_000:
        raw /= 1000.0
    return raw


def _extract_raw_payload(msg: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = msg.get("data")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, Mapping):
                return parsed
        except json.JSONDecodeError:
            return {}
    return raw if isinstance(raw, Mapping) else {}


def _safe_json(data: Any) -> str:
    try:
        return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return "{}"


class SettlementIndexCollector:
    """Read-only CF Benchmarks RTI subscriber with SQLite persistence."""

    def __init__(self, *, db_path: str | None = None, index_ids: Mapping[str, str] | None = None) -> None:
        self.db_path = db_path or _db_path()
        self.index_ids = {str(k).upper(): str(v).upper() for k, v in (index_ids or _configured_index_ids()).items()}
        self.index_to_asset = {v: k for k, v in self.index_ids.items()}
        self.enabled = _enabled()
        self.have_ws = bool(_HAVE_WS)
        self.stale_seconds = _stale_seconds()
        self._latest: dict[str, dict[str, Any]] = {}
        # The strict 13M RTI rule needs the complete decision-time path. Keep it
        # in memory so SQLite writer lag can never make a fresh path look absent.
        # A cold restart intentionally starts empty and therefore fails closed.
        self._history: dict[str, deque[dict[str, Any]]] = {
            asset: deque(maxlen=900) for asset in self.index_ids
        }
        self._latest_lock = threading.Lock()
        self._queue: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=_queue_size())
        self._stop = threading.Event()
        self._writer: threading.Thread | None = None
        self._ws_thread: threading.Thread | None = None
        self._last_error: str | None = None
        self._last_message_at: float | None = None
        self._last_tick_at: float | None = None
        self._started_at: float | None = None
        self._connected_at: float | None = None
        self._subscription_sid: int | None = None
        self._subscription_confirmed = False
        self._available_index_ids: set[str] = set()
        self._subscription_error: str | None = None
        self._messages_by_asset = {asset: 0 for asset in self.index_ids}
        self._records_by_asset = {asset: 0 for asset in self.index_ids}
        self._connected = False
        self._dropped_rows = 0
        self._records_written = 0
        self._init_db()

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        try:
            # WAL rather than the default rollback journal. This store is multi-GB and
            # written continuously, so in "delete" mode it churns a sibling `-journal`
            # file beside it on every transaction — and a file-sync client (this tree
            # lives under OneDrive by default) can capture the database and that
            # transient journal out of step, leaving SQLite to recover against a journal
            # that does not match the header. WAL also lets readers run without blocking
            # the collector. Best-effort: filesystems that cannot do WAL keep their mode.
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                logger.debug("settlement index: WAL unavailable; keeping default journal mode")
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def start(self) -> bool:
        if not self.enabled:
            return False
        if not self.have_ws:
            self._last_error = "websockets_dependency_missing"
            logger.warning("Settlement index collector disabled: websockets dependency missing")
            return False
        signer = KalshiSigner()
        if not signer.available:
            self._last_error = "kalshi_auth_unavailable"
            logger.warning("Settlement index collector not started: Kalshi websocket auth unavailable")
            return False
        self._stop.clear()
        self._started_at = time.time()
        if self._writer is None or not self._writer.is_alive():
            self._writer = threading.Thread(target=self._writer_loop, name="settlement-index-writer", daemon=True)
            self._writer.start()
        if self._ws_thread is None or not self._ws_thread.is_alive():
            self._ws_thread = threading.Thread(target=self._ws_thread_main, name="settlement-index-ws", daemon=True)
            self._ws_thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _ws_headers(self) -> dict[str, str]:
        return KalshiSigner().sign("GET", WS_PATH)

    def _ws_thread_main(self) -> None:
        try:
            asyncio.run(self._run_forever())
        except RuntimeError as exc:
            self._last_error = str(exc)[:200]
            logger.warning("Settlement index websocket loop stopped: %s", exc)

    async def _connect(self, headers: Mapping[str, str]):
        header_kw = (
            "additional_headers"
            if "additional_headers" in inspect.signature(websockets.connect).parameters
            else "extra_headers"
        )
        url = os.environ.get("KALSHI_WS_URL") or (
            DEMO_URL if os.environ.get("KALSHI_ENV", "production").lower() == "demo" else PROD_URL
        )
        return websockets.connect(
            url,
            **{
                header_kw: dict(headers),
                "ping_interval": 20,
                "ping_timeout": 20,
                "close_timeout": 5,
                "max_queue": 1000,
            },
        )

    async def _run_forever(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                headers = self._ws_headers()
                connector = await self._connect(headers)
                async with connector as socket:
                    self._connected = True
                    self._connected_at = time.time()
                    self._subscription_sid = None
                    self._subscription_confirmed = False
                    self._subscription_error = None
                    self._last_error = None
                    await socket.send(json.dumps({
                        "id": 1,
                        "cmd": "subscribe",
                        "params": {
                            "channels": ["cfbenchmarks_value"],
                            "index_ids": sorted(set(self.index_ids.values())),
                        },
                    }))
                    backoff = 1.0
                    while not self._stop.is_set():
                        try:
                            raw = await asyncio.wait_for(socket.recv(), timeout=2.0)
                        except asyncio.TimeoutError:
                            continue
                        payload = self._handle_message(raw)
                        if (
                            isinstance(payload, Mapping)
                            and payload.get("type") == "subscribed"
                            and payload.get("id") == 1
                        ):
                            msg = payload.get("msg")
                            sid = msg.get("sid") if isinstance(msg, Mapping) else None
                            if sid is not None:
                                await socket.send(json.dumps({
                                    "id": 2,
                                    "cmd": "update_subscription",
                                    "params": {"sid": sid, "action": "indexlist"},
                                }))
            except Exception as exc:  # noqa: BLE001 - collector boundary
                self._last_error = str(exc)[:300]
                logger.warning("Settlement index reconnecting after error: %s", exc)
            finally:
                self._connected = False
                self._subscription_confirmed = False
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 1.8)

    def _handle_message(self, raw: str | bytes) -> Mapping[str, Any] | None:
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, Mapping):
            return None
        now = time.time()
        self._last_message_at = now
        message_type = payload.get("type")
        if message_type == "subscribed":
            msg = payload.get("msg")
            if isinstance(msg, Mapping) and msg.get("channel") == "cfbenchmarks_value":
                try:
                    self._subscription_sid = int(msg.get("sid"))
                except (TypeError, ValueError):
                    self._subscription_sid = None
                self._subscription_confirmed = self._subscription_sid is not None
            return payload
        if message_type == "cfbenchmarks_value_indexlist":
            msg = payload.get("msg")
            raw_ids = msg.get("index_ids") if isinstance(msg, Mapping) else None
            if isinstance(raw_ids, list):
                self._available_index_ids = {
                    str(index_id).strip().upper()
                    for index_id in raw_ids
                    if str(index_id).strip()
                }
                unsupported = sorted(set(self.index_ids.values()) - self._available_index_ids)
                self._subscription_error = (
                    "unsupported_index_ids:" + ",".join(unsupported)
                    if unsupported else None
                )
            return payload
        if message_type == "error":
            msg = payload.get("msg")
            code = msg.get("code") if isinstance(msg, Mapping) else None
            detail = msg.get("msg") if isinstance(msg, Mapping) else msg
            self._subscription_error = f"websocket_error_{code}:{detail}"[:300]
            self._last_error = self._subscription_error
            return payload
        if message_type != "cfbenchmarks_value":
            return payload
        msg = payload.get("msg")
        if not isinstance(msg, Mapping):
            return payload
        index_id = str(msg.get("index_id") or "").upper()
        asset = self.index_to_asset.get(index_id)
        if not asset:
            return payload
        upstream = _extract_raw_payload(msg)
        px = _num(upstream.get("value") if upstream else None)
        source_ts = _ms_to_s(upstream.get("time") if upstream else None)
        if px is None:
            px = _num((msg.get("avg_60s_data") or {}).get("value"))
        if source_ts is None:
            source_ts = _ms_to_s(msg.get("received_at"))
        if px is None:
            return payload
        ts = source_ts or now
        avg_60 = msg.get("avg_60s_data") if isinstance(msg.get("avg_60s_data"), Mapping) else {}
        final_avg = (
            msg.get("last_60s_windowed_average_15min")
            if isinstance(msg.get("last_60s_windowed_average_15min"), Mapping)
            else {}
        )
        row = {
            "ts": ts,
            "asset": asset,
            "index_id": index_id,
            "index_px": px,
            "index_age_s": max(0.0, now - ts),
            "avg_60s_px": _num(avg_60.get("value")),
            "avg_60s_window_size": int(_num(avg_60.get("window_size")) or 0) if avg_60 else None,
            "final_minute_avg_px": _num(final_avg.get("value")),
            "final_minute_window_size": int(_num(final_avg.get("window_size")) or 0) if final_avg else None,
            "raw_json": _safe_json(msg),
        }
        with self._latest_lock:
            self._latest[asset] = dict(row)
            self._history.setdefault(asset, deque(maxlen=900)).append(dict(row))
            self._last_tick_at = now
            self._messages_by_asset[asset] = self._messages_by_asset.get(asset, 0) + 1
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            self._dropped_rows += 1
        return payload

    def _writer_loop(self) -> None:
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        try:
            while not self._stop.is_set():
                try:
                    row = self._queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                try:
                    conn.execute(
                        "INSERT INTO settlement_index_ticks("
                        "ts,asset,index_id,index_px,index_age_s,avg_60s_px,"
                        "avg_60s_window_size,final_minute_avg_px,final_minute_window_size,raw_json"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            row.get("ts"),
                            row.get("asset"),
                            row.get("index_id"),
                            row.get("index_px"),
                            row.get("index_age_s"),
                            row.get("avg_60s_px"),
                            row.get("avg_60s_window_size"),
                            row.get("final_minute_avg_px"),
                            row.get("final_minute_window_size"),
                            row.get("raw_json"),
                        ),
                    )
                    conn.commit()
                    self._records_written += 1
                    asset = str(row.get("asset") or "").upper()
                    self._records_by_asset[asset] = self._records_by_asset.get(asset, 0) + 1
                except sqlite3.Error as exc:
                    self._last_error = str(exc)[:200]
                    logger.warning("Settlement index recorder error: %s", exc)
                finally:
                    self._queue.task_done()
        finally:
            conn.close()

    def _writer_loop_once_for_tests(self) -> bool:
        """Drain one queued row synchronously for deterministic unit tests."""
        try:
            row = self._queue.get_nowait()
        except queue.Empty:
            return False
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        try:
            conn.execute(
                "INSERT INTO settlement_index_ticks("
                "ts,asset,index_id,index_px,index_age_s,avg_60s_px,"
                "avg_60s_window_size,final_minute_avg_px,final_minute_window_size,raw_json"
                ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    row.get("ts"),
                    row.get("asset"),
                    row.get("index_id"),
                    row.get("index_px"),
                    row.get("index_age_s"),
                    row.get("avg_60s_px"),
                    row.get("avg_60s_window_size"),
                    row.get("final_minute_avg_px"),
                    row.get("final_minute_window_size"),
                    row.get("raw_json"),
                ),
            )
            conn.commit()
            self._records_written += 1
            asset = str(row.get("asset") or "").upper()
            self._records_by_asset[asset] = self._records_by_asset.get(asset, 0) + 1
            return True
        finally:
            conn.close()
            self._queue.task_done()

    def latest(self, asset: str, *, now: float | None = None, max_age_s: float | None = None) -> dict[str, Any] | None:
        current = time.time() if now is None else float(now)
        limit = self.stale_seconds if max_age_s is None else float(max_age_s)
        with self._latest_lock:
            row = dict(self._latest.get(str(asset).upper()) or {})
        if not row:
            return None
        age = current - float(row.get("ts") or current)
        if age > limit:
            return None
        row["index_age_s"] = max(0.0, age)
        return row

    def path(
        self,
        asset: str,
        *,
        start_ts: float,
        end_ts: float,
        now: float | None = None,
        max_age_s: float = 2.0,
    ) -> dict[str, Any]:
        """Return an exact one-row-per-second RTI path, failing closed.

        Source timestamps are assigned to their nearest whole-second sample.
        Duplicate updates for one second are harmless; the freshest received
        sample wins. Missing seconds, stale-at-receipt rows, and a stale final
        decision tick are all explicit failures.
        """
        asset_key = str(asset).upper()
        current = time.time() if now is None else float(now)
        start_second = int(round(float(start_ts)))
        end_second = int(round(float(end_ts)))
        expected_seconds = list(range(start_second, end_second + 1))
        base = {
            "asset": asset_key,
            "index_id": self.index_ids.get(asset_key),
            "start_ts": float(start_second),
            "end_ts": float(end_second),
            "expected_count": len(expected_seconds),
            "count": 0,
            "complete": False,
            "rows": [],
            "missing_seconds": expected_seconds,
            "max_receive_age_s": None,
            "decision_age_s": None,
            "status": "missing",
            "missing_reason": None,
        }
        if not self.enabled:
            return {**base, "status": "disabled", "missing_reason": "settlement_index_disabled"}
        if asset_key not in self.index_ids:
            return {**base, "missing_reason": "settlement_index_asset_unconfigured"}
        if end_second < start_second:
            return {**base, "status": "error", "missing_reason": "settlement_index_path_range_invalid"}
        with self._latest_lock:
            history = [dict(row) for row in self._history.get(asset_key, ())]
        by_second: dict[int, dict[str, Any]] = {}
        for row in history:
            source_ts = _num(row.get("ts"))
            if source_ts is None:
                continue
            second = int(round(source_ts))
            if second < start_second or second > end_second:
                continue
            prior = by_second.get(second)
            prior_age = _num(prior.get("index_age_s")) if prior else None
            row_age = _num(row.get("index_age_s"))
            if prior is None or (row_age is not None and (prior_age is None or row_age < prior_age)):
                by_second[second] = row
        missing = [second for second in expected_seconds if second not in by_second]
        rows = [by_second[second] for second in expected_seconds if second in by_second]
        receive_ages = [_num(row.get("index_age_s")) for row in rows]
        receive_ages = [age for age in receive_ages if age is not None]
        max_receive_age = max(receive_ages) if receive_ages else None
        last_ts = _num(rows[-1].get("ts")) if rows else None
        decision_age = None if last_ts is None else max(0.0, current - last_ts)
        result = {
            **base,
            "index_id": rows[-1].get("index_id") if rows else base["index_id"],
            "count": len(rows),
            "rows": rows,
            "missing_seconds": missing,
            "max_receive_age_s": max_receive_age,
            "decision_age_s": decision_age,
        }
        if missing:
            return {**result, "missing_reason": "settlement_index_path_incomplete"}
        if len(receive_ages) != len(rows) or max_receive_age is None or max_receive_age > max_age_s:
            return {**result, "status": "stale", "missing_reason": "settlement_index_path_receive_stale"}
        if decision_age is None or decision_age > max_age_s:
            return {**result, "status": "stale", "missing_reason": "settlement_index_path_decision_stale"}
        return {**result, "complete": True, "status": "ok", "missing_reason": None}

    def context(self, asset: str, *, spot_px: float | None = None, now: float | None = None) -> dict[str, Any]:
        asset_key = str(asset).upper()
        current = time.time() if now is None else float(now)
        with self._latest_lock:
            raw = dict(self._latest.get(asset_key) or {})
        base = {
            "index_px": None,
            "basis_cents": None,
            "index_age_s": None,
            "index_status": "missing",
            "index_missing_reason": None,
            "index_id": self.index_ids.get(asset_key),
            "index_source_ts": None,
        }
        if not self.enabled:
            return {**base, "index_status": "disabled", "index_missing_reason": "settlement_index_disabled"}
        if asset_key not in self.index_ids:
            return {**base, "index_missing_reason": "settlement_index_asset_unconfigured"}
        if not raw:
            return {**base, "index_missing_reason": "settlement_index_tick_missing"}
        source_ts = _num(raw.get("ts"))
        age = None if source_ts is None else max(0.0, current - source_ts)
        if age is None or age > self.stale_seconds:
            return {
                **base,
                "index_age_s": age,
                "index_status": "stale",
                "index_missing_reason": "settlement_index_tick_stale",
                "index_id": raw.get("index_id") or base["index_id"],
                "index_source_ts": source_ts,
            }
        row = dict(raw)
        row["index_age_s"] = age
        index_px = _num(row.get("index_px"))
        spot = _num(spot_px)
        basis = None if index_px is None or spot is None else (spot - index_px) * 100.0
        return {
            "index_px": index_px,
            "basis_cents": basis,
            "index_age_s": row.get("index_age_s"),
            "index_status": "ok" if index_px is not None else "missing",
            "index_missing_reason": None if index_px is not None else "settlement_index_value_missing",
            "index_id": row.get("index_id") or base["index_id"],
            "index_source_ts": source_ts,
        }

    def health(self) -> dict[str, Any]:
        now = time.time()
        with self._latest_lock:
            ages = {}
            for asset in self.index_ids:
                row = self._latest.get(asset)
                if row:
                    ages[asset] = round(max(0.0, now - float(row.get("ts") or now)), 3)
            last_message_at = self._last_message_at
            last_tick_at = self._last_tick_at
        configured_assets = sorted(self.index_ids)
        missing_assets = sorted(set(configured_assets) - set(ages))
        stale_assets = sorted(asset for asset, age in ages.items() if age > self.stale_seconds)
        fresh_assets = sorted(asset for asset, age in ages.items() if age <= self.stale_seconds)
        unsupported_assets = (
            sorted(
                asset
                for asset, index_id in self.index_ids.items()
                if index_id not in self._available_index_ids
            )
            if self._available_index_ids else []
        )
        all_assets_ready = not missing_assets and not stale_assets and not unsupported_assets
        freshest_age = min(ages.values()) if ages else None
        latest_age = max(ages.values()) if ages else None
        coverage_anchor = self._connected_at or self._started_at
        coverage_gap_age = (
            max(0.0, now - coverage_anchor)
            if coverage_anchor is not None and (missing_assets or unsupported_assets)
            else None
        )
        watchdog_candidates = [age for age in (latest_age, coverage_gap_age) if age is not None]
        watchdog_age = max(watchdog_candidates) if watchdog_candidates else None
        if not self.enabled:
            status = "disabled"
        elif not self._connected:
            status = "starting_or_disconnected"
        elif not self._subscription_confirmed:
            status = "subscription_pending"
        elif unsupported_assets:
            status = "degraded_unsupported"
        elif missing_assets:
            status = "degraded_missing"
        elif stale_assets:
            status = "degraded_stale"
        else:
            status = "connected"
        return {
            "enabled": self.enabled,
            "read_only": True,
            "have_ws": self.have_ws,
            "connected": self._connected,
            "db_path": self.db_path,
            "index_ids": dict(self.index_ids),
            "available_index_ids": sorted(self._available_index_ids),
            "subscription_sid": self._subscription_sid,
            "subscription_confirmed": self._subscription_confirmed,
            "subscription_error": self._subscription_error,
            "last_error": self._last_error,
            "last_message_age_seconds": None if last_message_at is None else round(max(0.0, now - last_message_at), 3),
            "last_tick_age_seconds": None if last_tick_at is None else round(max(0.0, now - last_tick_at), 3),
            "latest_age_seconds": latest_age,
            "freshest_age_seconds": freshest_age,
            "watchdog_age_seconds": None if watchdog_age is None else round(watchdog_age, 3),
            "age_by_asset_seconds": ages,
            "configured_assets": configured_assets,
            "fresh_assets": fresh_assets,
            "missing_assets": missing_assets,
            "stale_assets": stale_assets,
            "unsupported_assets": unsupported_assets,
            "all_assets_ready": all_assets_ready,
            "fresh_coverage_ratio": round(len(fresh_assets) / len(configured_assets), 4) if configured_assets else 1.0,
            "messages_by_asset": dict(self._messages_by_asset),
            "records_written_by_asset": dict(self._records_by_asset),
            "records_written": self._records_written,
            "dropped_rows": self._dropped_rows,
            "queue_size": self._queue.qsize(),
            "stale_seconds": self.stale_seconds,
            "status": status,
        }


_feed: SettlementIndexCollector | None = None
_feed_lock = threading.Lock()


def get_settlement_index_feed() -> SettlementIndexCollector:
    global _feed
    with _feed_lock:
        if _feed is None:
            _feed = SettlementIndexCollector()
        return _feed


def start_settlement_index() -> bool:
    try:
        return get_settlement_index_feed().start()
    except Exception as exc:  # noqa: BLE001 - startup boundary
        logger.warning("Settlement index collector start failed: %s", exc)
        return False


def settlement_index_context(asset: str, *, spot_px: float | None = None, now: float | None = None) -> dict[str, Any]:
    try:
        feed = get_settlement_index_feed()
        if not feed.enabled:
            return {
                "index_px": None, "basis_cents": None, "index_age_s": None,
                "index_status": "disabled", "index_missing_reason": "settlement_index_disabled",
                "index_id": None, "index_source_ts": None,
            }
        return feed.context(asset, spot_px=spot_px, now=now)
    except Exception:
        logger.debug("settlement index context unavailable", exc_info=True)
        return {
            "index_px": None, "basis_cents": None, "index_age_s": None,
            "index_status": "error", "index_missing_reason": "settlement_index_context_error",
            "index_id": None, "index_source_ts": None,
        }


def settlement_index_path(
    asset: str,
    *,
    start_ts: float,
    end_ts: float,
    now: float | None = None,
    max_age_s: float = 2.0,
) -> dict[str, Any]:
    """Process-wide strict RTI path accessor used by prospective paper rules."""
    try:
        return get_settlement_index_feed().path(
            asset,
            start_ts=start_ts,
            end_ts=end_ts,
            now=now,
            max_age_s=max_age_s,
        )
    except Exception:
        logger.debug("settlement index path unavailable", exc_info=True)
        return {
            "asset": str(asset).upper(),
            "index_id": None,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "expected_count": int(round(end_ts)) - int(round(start_ts)) + 1,
            "count": 0,
            "complete": False,
            "rows": [],
            "missing_seconds": [],
            "max_receive_age_s": None,
            "decision_age_s": None,
            "status": "error",
            "missing_reason": "settlement_index_path_error",
        }


def settlement_index_health() -> dict[str, Any]:
    try:
        return get_settlement_index_feed().health()
    except Exception as exc:  # noqa: BLE001 - health must not fail app health
        return {"enabled": _enabled(), "read_only": True, "error": f"{type(exc).__name__}: {exc}"}
