"""Kalshi crypto settlement-index collector.

Kalshi's current 15-minute crypto market metadata says each market settles from
CF Benchmarks' corresponding Real Time Index (RTI): ``rules_primary`` names the
asset index (for example BRTI / ETHUSDRTI), and ``rules_secondary`` says the
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
    "ETH": "ETHUSDRTI",
    "SOL": "SOLUSDRTI",
    "XRP": "XRPUSDRTI",
    "DOGE": "DOGEUSDRTI",
    "BNB": "BNBUSDRTI",
    "HYPE": "HYPEUSDRTI",
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
            out[asset] = index_id
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
        self._latest_lock = threading.Lock()
        self._queue: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=_queue_size())
        self._stop = threading.Event()
        self._writer: threading.Thread | None = None
        self._ws_thread: threading.Thread | None = None
        self._last_error: str | None = None
        self._last_message_at: float | None = None
        self._connected = False
        self._dropped_rows = 0
        self._records_written = 0
        self._init_db()

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        try:
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
                        self._handle_message(raw)
            except Exception as exc:  # noqa: BLE001 - collector boundary
                self._last_error = str(exc)[:300]
                logger.warning("Settlement index reconnecting after error: %s", exc)
            finally:
                self._connected = False
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 1.8)

    def _handle_message(self, raw: str | bytes) -> None:
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return
        if not isinstance(payload, Mapping) or payload.get("type") != "cfbenchmarks_value":
            return
        msg = payload.get("msg")
        if not isinstance(msg, Mapping):
            return
        index_id = str(msg.get("index_id") or "").upper()
        asset = self.index_to_asset.get(index_id)
        if not asset:
            return
        upstream = _extract_raw_payload(msg)
        px = _num(upstream.get("value") if upstream else None)
        source_ts = _ms_to_s(upstream.get("time") if upstream else None)
        if px is None:
            px = _num((msg.get("avg_60s_data") or {}).get("value"))
        if source_ts is None:
            source_ts = _ms_to_s(msg.get("received_at"))
        if px is None:
            return
        now = time.time()
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
            self._last_message_at = now
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            self._dropped_rows += 1

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

    def context(self, asset: str, *, spot_px: float | None = None, now: float | None = None) -> dict[str, Any]:
        row = self.latest(asset, now=now)
        if row is None:
            return {"index_px": None, "basis_cents": None, "index_age_s": None}
        index_px = _num(row.get("index_px"))
        spot = _num(spot_px)
        basis = None if index_px is None or spot is None else (spot - index_px) * 100.0
        return {
            "index_px": index_px,
            "basis_cents": basis,
            "index_age_s": row.get("index_age_s"),
        }

    def health(self) -> dict[str, Any]:
        now = time.time()
        with self._latest_lock:
            ages = {
                asset: round(max(0.0, now - float(row.get("ts") or now)), 3)
                for asset, row in self._latest.items()
            }
            last_message_at = self._last_message_at
        latest_age = min(ages.values()) if ages else None
        return {
            "enabled": self.enabled,
            "read_only": True,
            "have_ws": self.have_ws,
            "connected": self._connected,
            "db_path": self.db_path,
            "index_ids": dict(self.index_ids),
            "last_error": self._last_error,
            "last_message_age_seconds": None if last_message_at is None else round(max(0.0, now - last_message_at), 3),
            "latest_age_seconds": latest_age,
            "age_by_asset_seconds": ages,
            "records_written": self._records_written,
            "dropped_rows": self._dropped_rows,
            "queue_size": self._queue.qsize(),
            "stale_seconds": self.stale_seconds,
            "status": "connected" if self._connected else ("disabled" if not self.enabled else "starting_or_disconnected"),
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
            return {"index_px": None, "basis_cents": None, "index_age_s": None}
        return feed.context(asset, spot_px=spot_px, now=now)
    except Exception:
        logger.debug("settlement index context unavailable", exc_info=True)
        return {"index_px": None, "basis_cents": None, "index_age_s": None}


def settlement_index_health() -> dict[str, Any]:
    try:
        return get_settlement_index_feed().health()
    except Exception as exc:  # noqa: BLE001 - health must not fail app health
        return {"enabled": _enabled(), "read_only": True, "error": f"{type(exc).__name__}: {exc}"}
