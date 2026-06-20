"""Public Kalshi REST client (no auth required).

Exposes market discovery, market detail, orderbook snapshots, and public
trades. A token-bucket limiter keeps us safely under Kalshi's ~30 req/sec
public cap while allowing the ~1/sec refresh cadence across all assets.

NOTE: A future authenticated WebSocket source (see ws_client.py) can replace
the polling here without changing the analysis layer.
"""
import time
import threading
import logging
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"


class TokenBucket:
    """Thread-safe token bucket for rate limiting concurrent requests."""

    def __init__(self, rate, capacity=None):
        self.rate = float(rate)
        self.capacity = float(capacity if capacity is not None else rate)
        self.tokens = self.capacity
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self, n=1):
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= n:
                    self.tokens -= n
                    return
                wait = (n - self.tokens) / self.rate
            time.sleep(wait)


def parse_ts(ts_str):
    """ISO8601 (with optional fractional seconds + Z) -> epoch float."""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def dollars_to_cents(d):
    if d is None:
        return None
    try:
        return round(float(d) * 100)
    except (TypeError, ValueError):
        return None


class KalshiClient:
    def __init__(self, base_url=BASE_URL, rate=18.0, capacity=18):
        self.base = base_url
        self.bucket = TokenBucket(rate, capacity)

    def _get(self, path, params=None, retries=1, timeout=(3.05, 4)):
        # Per-cycle calls default to a single fast attempt (the ~1s refresh
        # loop is itself the retry); the bounded (connect, read) timeout stops
        # one hung upstream from blowing the cycle deadline.
        url = f"{self.base}{path}"
        for attempt in range(retries):
            self.bucket.acquire()
            try:
                # Plain requests (no shared Session) for thread-safety under
                # the refresh loop's ThreadPoolExecutor.
                resp = requests.get(url, params=params, timeout=timeout,
                                    headers={"Accept": "application/json"})
                if resp.status_code == 429:
                    wait = min(30, (2 ** attempt) * 2)
                    logger.warning(f"Rate limited on {path}; waiting {wait}s")
                    time.sleep(wait)
                    continue
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.RequestException as e:
                if attempt < retries - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                logger.warning(f"Request error on {path}: {e}")
                return None
        return None

    # -- discovery -------------------------------------------------------
    def discover(self, series_ticker, limit=100, min_close_ts=None,
                 max_close_ts=None, status=None):
        """List markets for a series.

        NOTE: 15-min crypto markets are NOT exposed under status=open; that
        filter returns empty even when a market is trading. We instead query
        by close-time window (no status) and let the caller pick the soonest
        future market. A market with floor_strike set is open/live; one with
        floor_strike None is created but not yet opened ("upcoming").
        """
        params = {"series_ticker": series_ticker, "limit": limit}
        if status:
            params["status"] = status
        if min_close_ts is not None:
            params["min_close_ts"] = int(min_close_ts)
        if max_close_ts is not None:
            params["max_close_ts"] = int(max_close_ts)
        # Discovery runs every ~30s, not per cycle, so it can afford retries
        # and a longer read budget than the per-cycle orderbook/trade calls.
        data = self._get("/markets", params, retries=3, timeout=(3.05, 8))
        if not data:
            return []
        return data.get("markets", [])

    def get_market(self, ticker):
        data = self._get(f"/markets/{ticker}")
        return data.get("market") if data else None

    def get_orderbook(self, ticker):
        data = self._get(f"/markets/{ticker}/orderbook")
        return data.get("orderbook_fp") if data else None

    # -- public trades ---------------------------------------------------
    def get_trades(self, ticker, min_ts=None, limit=1000):
        """Return normalized trades newer than min_ts (epoch seconds).

        Kalshi returns newest-first. We request a single page sized to cover
        the refresh window; dedupe by trade_id happens upstream.
        """
        params = {"ticker": ticker, "limit": limit}
        if min_ts is not None:
            params["min_ts"] = int(min_ts)
        data = self._get("/markets/trades", params)
        if not data:
            return []
        out = []
        for t in data.get("trades", []):
            out.append(self._norm_trade(t))
        return [t for t in out if t["ts"] is not None and t["yes_cents"] is not None]

    @staticmethod
    def _norm_trade(t):
        yes_cents = dollars_to_cents(t.get("yes_price_dollars"))
        if yes_cents is None and t.get("yes_price") is not None:
            raw = t["yes_price"]
            try:
                yes_cents = round(float(raw) * 100) if float(raw) <= 1.0 else int(raw)
            except (TypeError, ValueError):
                yes_cents = None
        try:
            count = float(t.get("count_fp", t.get("count", 0)) or 0)
        except (TypeError, ValueError):
            count = 0.0
        return {
            "id": t.get("trade_id"),
            "ts": parse_ts(t.get("created_time")),
            "created_time": t.get("created_time", ""),
            "yes_cents": yes_cents,
            "count": count,
            "taker_side": t.get("taker_side"),
            "is_block": bool(t.get("is_block_trade", False)),
        }
