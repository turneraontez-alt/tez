from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, List

import requests

from .precision import dollars_to_cents, normalized_taker_side

logger = logging.getLogger(__name__)
BASE_URL = "https://external-api.kalshi.com/trade-api/v2"

# Hard cap on how long a single 429 backoff may sleep, so a hostile or
# misconfigured ``Retry-After`` cannot stall the ~1s refresh loop.
_MAX_RETRY_AFTER_SECONDS = 8.0


def _retry_after_seconds(response, fallback: float) -> float:
    """Honor a ``Retry-After`` header (delta-seconds or HTTP-date), capped.

    Falls back to ``fallback`` when the header is absent or unparseable.
    Always clamped to ``[0, _MAX_RETRY_AFTER_SECONDS]`` so a bad value can't
    block the cycle indefinitely.
    """
    raw = response.headers.get("Retry-After") if response is not None else None
    wait = fallback
    if raw:
        try:
            wait = float(raw)
        except (TypeError, ValueError):
            try:
                from email.utils import parsedate_to_datetime

                when = parsedate_to_datetime(raw)
                if when is not None:
                    wait = (when.timestamp() - time.time())
            except Exception:
                wait = fallback
    return max(0.0, min(_MAX_RETRY_AFTER_SECONDS, wait))


class TokenBucket:
    def __init__(self, rate: float, capacity: float | None = None):
        self.rate = float(rate)
        self.capacity = float(capacity if capacity is not None else rate)
        self.tokens = self.capacity
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self, amount: float = 1.0) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= amount:
                    self.tokens -= amount
                    return
                wait = (amount - self.tokens) / self.rate
            time.sleep(max(0.001, wait))


def parse_ts(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        value = float(value)
        return value / 1000.0 if value > 10_000_000_000 else value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


class KalshiClient:
    """REST fallback client with fixed-point prices and canonical trade sides."""

    def __init__(self, base_url: str = BASE_URL, rate: float = 18.0, capacity: int = 18):
        self.base = base_url.rstrip("/")
        self.bucket = TokenBucket(rate, capacity)
        # One read-only HTTP session per calling thread. The exact sampler
        # keeps a bounded worker pool alive across 13M/+30/+60/+90 stages, so
        # these sessions reuse TLS connections without sharing mutable Session
        # state across threads.
        self._thread_local = threading.local()

    def _session(self) -> requests.Session:
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"Accept": "application/json"})
            self._thread_local.session = session
        return session

    def _get(self, path: str, params=None, retries: int = 1, timeout=(3.05, 4)):
        url = f"{self.base}{path}"
        for attempt in range(max(1, retries)):
            self.bucket.acquire()
            try:
                response = self._session().get(
                    url, params=params, timeout=timeout,
                )
                if response.status_code == 429:
                    if attempt + 1 < retries:
                        # Honor the server's Retry-After when present, else fall
                        # back to bounded exponential backoff. Capped so a large
                        # Retry-After can't stall the refresh loop.
                        wait = _retry_after_seconds(response, min(8.0, 0.5 * (2 ** attempt)))
                        time.sleep(wait)
                        continue
                    logger.warning("Kalshi REST %s rate-limited (429); giving up after %d attempt(s)", path, retries)
                    return None
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                return response.json()
            except requests.RequestException as exc:
                if attempt + 1 < retries:
                    time.sleep(0.4 * (attempt + 1))
                    continue
                logger.warning("Kalshi REST %s failed: %s", path, exc)
                return None
        return None

    def discover(self, series_ticker, limit=100, min_close_ts=None, max_close_ts=None, status=None):
        params: Dict[str, Any] = {"series_ticker": series_ticker, "limit": limit}
        if status:
            params["status"] = status
        if min_close_ts is not None:
            params["min_close_ts"] = int(min_close_ts)
        if max_close_ts is not None:
            params["max_close_ts"] = int(max_close_ts)
        data = self._get("/markets", params, retries=3, timeout=(3.05, 8))
        return data.get("markets", []) if data else []

    def get_market(self, ticker):
        data = self._get(f"/markets/{ticker}")
        return data.get("market") if data else None

    def get_series(self, ticker):
        """Return official series metadata, including fee type/multiplier."""
        data = self._get(f"/series/{ticker}", retries=3)
        return data.get("series") if data else None

    def get_orderbook(self, ticker, timeout=(3.05, 4)):
        data = self._get(f"/markets/{ticker}/orderbook", timeout=timeout)
        if not data:
            return None
        return data.get("orderbook_fp") or data.get("orderbook")

    def get_trades(self, ticker, min_ts=None, limit=1000):
        params: Dict[str, Any] = {"ticker": ticker, "limit": limit}
        if min_ts is not None:
            params["min_ts"] = int(min_ts)
        data = self._get("/markets/trades", params)
        if not data:
            return []
        rows = [self._norm_trade(row) for row in data.get("trades", [])]
        return [row for row in rows if row.get("id") and row.get("ts") is not None and row.get("yes_cents") is not None]

    @staticmethod
    def _norm_trade(record: dict) -> dict:
        yes_cents = dollars_to_cents(record.get("yes_price_dollars"))
        if yes_cents is None and record.get("yes_price") is not None:
            try:
                raw = float(record["yes_price"])
                yes_cents = raw * 100.0 if raw <= 1.0 else raw
            except (TypeError, ValueError):
                yes_cents = None
        try:
            count = float(record.get("count_fp", record.get("count", 0)) or 0)
        except (TypeError, ValueError):
            count = 0.0
        ts = (
            parse_ts(record.get("created_time"))
            or parse_ts(record.get("ts_ms"))
            or parse_ts(record.get("ts"))
        )
        return {
            "id": record.get("trade_id") or record.get("id"),
            "ts": ts,
            "created_time": record.get("created_time", ""),
            "yes_cents": round(float(yes_cents), 4) if yes_cents is not None else None,
            "count": count,
            "taker_side": normalized_taker_side(record),
            "is_block": bool(record.get("is_block_trade", False)),
        }
