"""Public, read-only market-data enrichment for Q15 V9.5.

The collector is deliberately non-blocking from the decision loop.  It polls
public endpoints on background worker threads, retains the last good snapshot,
and exposes explicit freshness/source-health metadata.  Network failures never
become trading signals and never cause order placement.

Supported public sources:
* Coinbase Exchange: ticker, aggregated level-2 book, recent trades.
* Kraken Spot: ticker, grouped/depth book, recent trades.
* Deribit: perpetual ticker for BTC and ETH.

The module uses only the Python standard library.  No API keys are read or
required, and no private/order endpoints are present.
"""
from __future__ import annotations

import copy
import json
import math
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

VERSION = "q15-v9.5-public-market-data-v2"
READ_ONLY = True


@dataclass(frozen=True)
class AssetRoute:
    coinbase_product: str | None
    kraken_pair: str | None
    kraken_symbol: str | None
    deribit_instrument: str | None
    okx_instrument: str | None = None


ASSET_ROUTES: dict[str, AssetRoute] = {
    "BTC": AssetRoute("BTC-USD", "XBTUSD", "BTC/USD", "BTC-PERPETUAL", "BTC-USDT"),
    "XBT": AssetRoute("BTC-USD", "XBTUSD", "BTC/USD", "BTC-PERPETUAL", "BTC-USDT"),
    "ETH": AssetRoute("ETH-USD", "ETHUSD", "ETH/USD", "ETH-PERPETUAL", "ETH-USDT"),
    "SOL": AssetRoute("SOL-USD", "SOLUSD", "SOL/USD", None, "SOL-USDT"),
    "DOGE": AssetRoute("DOGE-USD", "XDGUSD", "DOGE/USD", None, "DOGE-USDT"),
    "XDG": AssetRoute("DOGE-USD", "XDGUSD", "DOGE/USD", None, "DOGE-USDT"),
    "XRP": AssetRoute("XRP-USD", "XRPUSD", "XRP/USD", None, "XRP-USDT"),
    "LTC": AssetRoute("LTC-USD", "LTCUSD", "LTC/USD", None, "LTC-USDT"),
    "BCH": AssetRoute("BCH-USD", "BCHUSD", "BCH/USD", None, "BCH-USDT"),
    "ADA": AssetRoute("ADA-USD", "ADAUSD", "ADA/USD", None, "ADA-USDT"),
    "AVAX": AssetRoute("AVAX-USD", "AVAXUSD", "AVAX/USD", None, "AVAX-USDT"),
    "LINK": AssetRoute("LINK-USD", "LINKUSD", "LINK/USD", None, "LINK-USDT"),
    # Coinbase/Kraken do not list BNB or HYPE in USD; both trade on OKX in USDT,
    # which is the same source the primary spot feed uses. Routing them to OKX
    # gives the enrichment hub real composite/flow/book data instead of logging
    # persistent source errors every cycle.
    "BNB": AssetRoute(None, None, None, None, "BNB-USDT"),
    "HYPE": AssetRoute(None, None, None, None, os.environ.get("Q15_V95_HYPE_OKX_INSTRUMENT", "HYPE-USDT")),
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    return max(low, min(high, value))


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return default
        parsed = float(value)
        return parsed if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def _normalize_epoch(parsed: float) -> float | None:
    if not math.isfinite(parsed):
        return None
    # Collapse milli/micro/nano-second epochs down to seconds. A genuine second
    # epoch (~1.7e9) is below the threshold and is never divided.
    while parsed > 10_000_000_000:
        parsed /= 1000.0
    return parsed


def _parse_ts(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _normalize_epoch(float(value))
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        numeric = _num(text)
        return None if numeric is None else _normalize_epoch(numeric)


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    return numerator / denominator if abs(denominator) > 1e-15 else default


def _weighted_book_metrics(
    bids: Sequence[Sequence[Any]],
    asks: Sequence[Sequence[Any]],
    *,
    levels: int = 10,
) -> dict[str, Any]:
    clean_bids: list[tuple[float, float]] = []
    clean_asks: list[tuple[float, float]] = []
    for row in bids[:levels]:
        if not isinstance(row, Sequence) or len(row) < 2:
            continue
        price, size = _num(row[0]), _num(row[1])
        if price is not None and size is not None and price > 0 and size >= 0:
            clean_bids.append((price, size))
    for row in asks[:levels]:
        if not isinstance(row, Sequence) or len(row) < 2:
            continue
        price, size = _num(row[0]), _num(row[1])
        if price is not None and size is not None and price > 0 and size >= 0:
            clean_asks.append((price, size))
    clean_bids.sort(key=lambda item: item[0], reverse=True)
    clean_asks.sort(key=lambda item: item[0])
    if not clean_bids or not clean_asks:
        return {
            "available": False,
            "bid_levels": len(clean_bids),
            "ask_levels": len(clean_asks),
        }

    best_bid, best_bid_size = clean_bids[0]
    best_ask, best_ask_size = clean_asks[0]
    mid = (best_bid + best_ask) / 2.0
    spread_bps = _safe_div(best_ask - best_bid, mid) * 10_000.0

    bid_weighted = 0.0
    ask_weighted = 0.0
    for index, (_, size) in enumerate(clean_bids):
        bid_weighted += size / (1.0 + index)
    for index, (_, size) in enumerate(clean_asks):
        ask_weighted += size / (1.0 + index)
    imbalance = _safe_div(bid_weighted - ask_weighted, bid_weighted + ask_weighted)
    microprice = _safe_div(
        best_ask * best_bid_size + best_bid * best_ask_size,
        best_bid_size + best_ask_size,
        mid,
    )
    microprice_bps = _safe_div(microprice - mid, mid) * 10_000.0
    return {
        "available": True,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread_bps": spread_bps,
        "best_bid_size": best_bid_size,
        "best_ask_size": best_ask_size,
        "bid_depth": sum(size for _, size in clean_bids),
        "ask_depth": sum(size for _, size in clean_asks),
        "imbalance": _clamp(imbalance, -1.0, 1.0),
        "microprice": microprice,
        "microprice_bps": microprice_bps,
        "bid_levels": len(clean_bids),
        "ask_levels": len(clean_asks),
    }


def _trade_flow(
    rows: Sequence[Any],
    *,
    side_index: int | None = None,
    price_index: int = 0,
    size_index: int = 1,
    time_index: int | None = None,
    side_is_maker: bool = False,
    now: float | None = None,
    horizon_seconds: float = 180.0,
) -> dict[str, Any]:
    now = now or time.time()
    buy_notional = 0.0
    sell_notional = 0.0
    accepted = 0
    newest: float | None = None
    oldest: float | None = None
    for raw in rows:
        if isinstance(raw, Mapping):
            price = _num(raw.get("price"))
            size = _num(raw.get("size") or raw.get("volume") or raw.get("qty"))
            side = str(raw.get("side") or raw.get("taker_side") or "").lower()
            timestamp = _parse_ts(raw.get("time") or raw.get("timestamp") or raw.get("ts"))
        elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            if len(raw) <= max(price_index, size_index, side_index or 0, time_index or 0):
                continue
            price = _num(raw[price_index])
            size = _num(raw[size_index])
            side = str(raw[side_index]).lower() if side_index is not None else ""
            timestamp = _parse_ts(raw[time_index]) if time_index is not None else None
        else:
            continue
        if price is None or size is None or price <= 0 or size <= 0:
            continue
        if timestamp is not None and now - timestamp > horizon_seconds:
            continue
        if side in {"b", "buy", "bid"}:
            aggressive_buy = not side_is_maker
        elif side in {"s", "sell", "ask"}:
            aggressive_buy = side_is_maker
        else:
            continue
        notional = price * size
        if aggressive_buy:
            buy_notional += notional
        else:
            sell_notional += notional
        accepted += 1
        if timestamp is not None:
            newest = timestamp if newest is None else max(newest, timestamp)
            oldest = timestamp if oldest is None else min(oldest, timestamp)
    total = buy_notional + sell_notional
    return {
        "available": total > 0,
        "imbalance": _clamp(_safe_div(buy_notional - sell_notional, total), -1.0, 1.0),
        "buy_notional": buy_notional,
        "sell_notional": sell_notional,
        "total_notional": total,
        "trade_count": accepted,
        "oldest_trade_at": _iso(oldest),
        "newest_trade_at": _iso(newest),
    }


class HttpJsonClient:
    """Tiny bounded public-HTTP client with explicit response-size limits."""

    def __init__(self, timeout: float, max_bytes: int) -> None:
        self.timeout = timeout
        self.max_bytes = max_bytes
        self._ssl_context = ssl.create_default_context()

    def get(self, base_url: str, params: Mapping[str, Any] | None = None) -> Any:
        if params:
            query = urllib.parse.urlencode({key: value for key, value in params.items() if value is not None})
            url = base_url + ("&" if "?" in base_url else "?") + query
        else:
            url = base_url
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "Q15-ReadOnly-Monitor/9.5",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self._ssl_context) as response:
                length = _num(response.headers.get("Content-Length"))
                if length is not None and length > self.max_bytes:
                    raise ValueError(f"response_too_large:{int(length)}")
                raw = response.read(self.max_bytes + 1)
                if len(raw) > self.max_bytes:
                    raise ValueError(f"response_too_large:>{self.max_bytes}")
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"http_{exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"network:{exc.reason}") from exc


class PublicMarketDataHub:
    """Background, stale-while-revalidate public market-data cache."""

    def __init__(self) -> None:
        self.enabled = _env_bool("Q15_V95_PUBLIC_DATA_ENABLED", True)
        self.refresh_seconds = _env_float("Q15_V95_PUBLIC_REFRESH_SECONDS", 5.0, 2.0, 60.0)
        self.stale_seconds = _env_float("Q15_V95_PUBLIC_STALE_SECONDS", 20.0, 5.0, 180.0)
        self.timeout_seconds = _env_float("Q15_V95_HTTP_TIMEOUT_SECONDS", 1.4, 0.35, 5.0)
        self.book_levels = _env_int("Q15_V95_BOOK_LEVELS", 10, 1, 25)
        self.trade_horizon_seconds = _env_float("Q15_V95_TRADE_HORIZON_SECONDS", 180.0, 15.0, 900.0)
        self.max_response_bytes = _env_int("Q15_V95_MAX_RESPONSE_BYTES", 4_000_000, 100_000, 12_000_000)
        self.max_workers = _env_int("Q15_V95_PUBLIC_WORKERS", 6, 1, 12)
        self.coinbase_enabled = _env_bool("Q15_V95_COINBASE_ENABLED", True)
        self.kraken_enabled = _env_bool("Q15_V95_KRAKEN_ENABLED", True)
        self.deribit_enabled = _env_bool("Q15_V95_DERIBIT_ENABLED", True)
        self.okx_enabled = _env_bool("Q15_V95_OKX_ENABLED", True)
        self._client = HttpJsonClient(self.timeout_seconds, self.max_response_bytes)
        self._executor: ThreadPoolExecutor | None = None
        self._lock = threading.RLock()
        self._cache: dict[str, dict[str, Any]] = {}
        self._requested_at: dict[str, float] = {}
        self._inflight: dict[str, Future[dict[str, Any]]] = {}
        self._price_history: dict[str, list[tuple[float, float]]] = {}
        self._derivative_history: dict[str, list[tuple[float, float, float]]] = {}
        self._requests = 0
        self._successes = 0
        self._failures = 0
        self._last_error: str | None = None
        # Per-asset circuit breaker: after N consecutive all-source failures stop
        # hammering that asset's feeds for a cooldown window (just retry less; the
        # snapshot still serves last-good/unavailable). threshold 0 disables it.
        self.breaker_threshold = _env_int("Q15_V95_PUBLIC_BREAKER_THRESHOLD", 5, 0, 1000)
        self.breaker_cooldown = _env_float("Q15_V95_PUBLIC_BREAKER_COOLDOWN_SECONDS", 30.0, 0.0, 600.0)
        self._consecutive_failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}

    def _pool(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self.max_workers,
                thread_name_prefix="q15-v95-public",
            )
        return self._executor

    def schedule(self, assets: Sequence[str], now: float | None = None) -> None:
        if not self.enabled:
            return
        now = now or time.time()
        for raw_asset in assets:
            asset = str(raw_asset or "").upper().strip()
            if asset not in ASSET_ROUTES:
                continue
            with self._lock:
                future = self._inflight.get(asset)
                if future is not None and not future.done():
                    continue
                if now < self._open_until.get(asset, 0.0):
                    continue  # circuit open: back off after repeated failures
                if now - self._requested_at.get(asset, 0.0) < self.refresh_seconds:
                    continue
                self._requested_at[asset] = now
                self._requests += 1
                future = self._pool().submit(self._fetch_asset, asset, now)
                self._inflight[asset] = future
                future.add_done_callback(lambda completed, selected=asset: self._complete(selected, completed))

    def _complete(self, asset: str, future: Future[dict[str, Any]]) -> None:
        try:
            result = future.result()
        except Exception as exc:  # defensive: _fetch_asset normally contains source errors
            result = {
                "version": VERSION,
                "asset": asset,
                "fetched_at": time.time(),
                "sources": {},
                "source_errors": {"collector": f"{type(exc).__name__}:{exc}"},
                "available": False,
            }
        with self._lock:
            self._inflight.pop(asset, None)
            if result.get("available"):
                self._cache[asset] = result
                self._successes += 1
                self._last_error = None
                self._consecutive_failures[asset] = 0
                self._open_until.pop(asset, None)
                price = _num(result.get("composite_price"))
                timestamp = _num(result.get("fetched_at"), time.time()) or time.time()
                if price is not None and price > 0:
                    history = self._price_history.setdefault(asset, [])
                    history.append((timestamp, price))
                    cutoff = timestamp - 600.0
                    self._price_history[asset] = [row for row in history if row[0] >= cutoff][-600:]
                derivative = result.get("derivatives")
                if isinstance(derivative, Mapping):
                    oi = _num(derivative.get("open_interest"))
                    mark = _num(derivative.get("mark_price"))
                    if oi is not None and mark is not None:
                        history = self._derivative_history.setdefault(asset, [])
                        history.append((timestamp, oi, mark))
                        cutoff = timestamp - 1800.0
                        self._derivative_history[asset] = [row for row in history if row[0] >= cutoff][-600:]
            else:
                self._failures += 1
                errors = result.get("source_errors") or {}
                self._last_error = ";".join(f"{k}:{v}" for k, v in errors.items())[:500] or "no_public_source"
                streak = self._consecutive_failures.get(asset, 0) + 1
                self._consecutive_failures[asset] = streak
                if self.breaker_threshold > 0 and streak >= self.breaker_threshold:
                    self._open_until[asset] = (_num(result.get("fetched_at"), time.time()) or time.time()) + self.breaker_cooldown

    def snapshot(self, asset: str, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        asset = str(asset or "").upper().strip()
        with self._lock:
            cached = copy.deepcopy(self._cache.get(asset))
            history = list(self._price_history.get(asset, []))
            derivative_history = list(self._derivative_history.get(asset, []))
            inflight = asset in self._inflight and not self._inflight[asset].done()
        if not cached:
            return {
                "version": VERSION,
                "asset": asset,
                "available": False,
                "fresh": False,
                "inflight": inflight,
                "age_seconds": None,
                "price_returns": {},
                "derivative_changes": {},
            }
        fetched_at = _num(cached.get("fetched_at"), now) or now
        cached["age_seconds"] = max(0.0, now - fetched_at)
        cached["fresh"] = cached["age_seconds"] <= self.stale_seconds
        cached["inflight"] = inflight
        cached["price_returns"] = self._history_returns(history, now)
        cached["derivative_changes"] = self._derivative_changes(derivative_history, now)
        return cached

    @staticmethod
    def _history_returns(history: Sequence[tuple[float, float]], now: float) -> dict[str, float | None]:
        if not history:
            return {}
        latest = history[-1][1]
        output: dict[str, float | None] = {}
        for horizon in (5, 15, 30, 60, 180):
            target = now - horizon
            eligible = [row for row in history if row[0] <= target]
            base = eligible[-1][1] if eligible else None
            output[f"return_{horizon}s"] = None if base is None or base <= 0 else latest / base - 1.0
        return output

    @staticmethod
    def _derivative_changes(
        history: Sequence[tuple[float, float, float]], now: float
    ) -> dict[str, float | None]:
        if not history:
            return {}
        _, latest_oi, latest_mark = history[-1]
        output: dict[str, float | None] = {}
        for horizon in (60, 180, 600):
            eligible = [row for row in history if row[0] <= now - horizon]
            if not eligible:
                output[f"open_interest_change_{horizon}s"] = None
                output[f"mark_return_{horizon}s"] = None
                continue
            _, base_oi, base_mark = eligible[-1]
            output[f"open_interest_change_{horizon}s"] = None if base_oi <= 0 else latest_oi / base_oi - 1.0
            output[f"mark_return_{horizon}s"] = None if base_mark <= 0 else latest_mark / base_mark - 1.0
        return output

    def _fetch_asset(self, asset: str, now: float) -> dict[str, Any]:
        route = ASSET_ROUTES[asset]
        sources: dict[str, Any] = {}
        errors: dict[str, str] = {}
        if self.coinbase_enabled and route.coinbase_product:
            try:
                sources["coinbase"] = self._fetch_coinbase(route.coinbase_product, now)
            except Exception as exc:
                errors["coinbase"] = f"{type(exc).__name__}:{exc}"
        if self.kraken_enabled and route.kraken_pair:
            try:
                sources["kraken"] = self._fetch_kraken(route.kraken_pair, route.kraken_symbol, now)
            except Exception as exc:
                errors["kraken"] = f"{type(exc).__name__}:{exc}"
        if self.okx_enabled and route.okx_instrument:
            try:
                sources["okx"] = self._fetch_okx(route.okx_instrument, now)
            except Exception as exc:
                errors["okx"] = f"{type(exc).__name__}:{exc}"
        derivatives: dict[str, Any] | None = None
        if self.deribit_enabled and route.deribit_instrument:
            try:
                derivatives = self._fetch_deribit(route.deribit_instrument, now)
            except Exception as exc:
                errors["deribit"] = f"{type(exc).__name__}:{exc}"

        prices: list[tuple[str, float, float]] = []
        for name, source in sources.items():
            price = _num(source.get("price"))
            freshness = _num(source.get("quality"), 0.0) or 0.0
            if price is not None and price > 0:
                prices.append((name, price, max(0.05, freshness)))
        composite = self._weighted_median(prices)
        divergence_bps = None
        if len(prices) >= 2 and composite:
            divergence_bps = max(abs(price / composite - 1.0) * 10_000.0 for _, price, _ in prices)
        source_agreement = 0.0
        if prices:
            source_agreement = _clamp(1.0 - (divergence_bps or 0.0) / 35.0, 0.0, 1.0)

        return {
            "version": VERSION,
            "asset": asset,
            "read_only": True,
            "fetched_at": now,
            "fetched_at_iso": _iso(now),
            "available": composite is not None,
            "composite_price": composite,
            "source_count": len(prices),
            "source_agreement": source_agreement,
            "divergence_bps": divergence_bps,
            "sources": sources,
            "derivatives": derivatives,
            "source_errors": errors,
        }

    @staticmethod
    def _weighted_median(prices: Sequence[tuple[str, float, float]]) -> float | None:
        if not prices:
            return None
        ordered = sorted(prices, key=lambda row: row[1])
        total = sum(weight for _, _, weight in ordered)
        running = 0.0
        for _, price, weight in ordered:
            running += weight
            if running >= total / 2.0:
                return price
        return ordered[-1][1]

    def _fetch_coinbase(self, product: str, now: float) -> dict[str, Any]:
        base = "https://api.exchange.coinbase.com/products/" + urllib.parse.quote(product, safe="-")
        ticker = self._client.get(base + "/ticker")
        book = self._client.get(base + "/book", {"level": 2})
        trades = self._client.get(base + "/trades", {"limit": 200})
        if not isinstance(ticker, Mapping) or not isinstance(book, Mapping) or not isinstance(trades, list):
            raise ValueError("unexpected_coinbase_payload")
        price = _num(ticker.get("price"))
        book_metrics = _weighted_book_metrics(
            book.get("bids") if isinstance(book.get("bids"), list) else [],
            book.get("asks") if isinstance(book.get("asks"), list) else [],
            levels=self.book_levels,
        )
        flow = _trade_flow(
            trades,
            side_is_maker=True,
            now=now,
            horizon_seconds=self.trade_horizon_seconds,
        )
        timestamp = _parse_ts(ticker.get("time")) or now
        if price is None and book_metrics.get("available"):
            price = _num(book_metrics.get("mid"))
        return {
            "source": "coinbase",
            "price": price,
            "timestamp": timestamp,
            "timestamp_iso": _iso(timestamp),
            "quality": 1.0 if price is not None and book_metrics.get("available") else 0.65,
            "book": book_metrics,
            "flow": flow,
        }

    def _fetch_kraken(self, pair: str, symbol: str | None, now: float) -> dict[str, Any]:
        base = "https://api.kraken.com/0/public"
        ticker_payload = self._client.get(base + "/Ticker", {"pair": pair})
        book_payload: Any
        try:
            book_payload = self._client.get(base + "/Depth", {"pair": pair, "count": self.book_levels})
        except Exception:
            book_payload = self._client.get(base + "/GroupedBook", {"symbol": symbol, "depth": self.book_levels})
        trades_payload = self._client.get(base + "/Trades", {"pair": pair})
        ticker_result = self._kraken_first_result(ticker_payload)
        book_result = self._kraken_first_result(book_payload)
        trades_result = self._kraken_first_result(trades_payload, ignore_keys={"last"})
        price = None
        if isinstance(ticker_result, Mapping):
            close = ticker_result.get("c")
            if isinstance(close, Sequence) and not isinstance(close, (str, bytes)) and close:
                price = _num(close[0])
            price = price or _num(ticker_result.get("last") or ticker_result.get("price"))
        bids = book_result.get("bids", []) if isinstance(book_result, Mapping) else []
        asks = book_result.get("asks", []) if isinstance(book_result, Mapping) else []
        book_metrics = _weighted_book_metrics(bids, asks, levels=self.book_levels)
        trade_rows = trades_result if isinstance(trades_result, list) else []
        flow = _trade_flow(
            trade_rows,
            side_index=3,
            price_index=0,
            size_index=1,
            time_index=2,
            side_is_maker=False,
            now=now,
            horizon_seconds=self.trade_horizon_seconds,
        )
        if price is None and book_metrics.get("available"):
            price = _num(book_metrics.get("mid"))
        return {
            "source": "kraken",
            "price": price,
            "timestamp": now,
            "timestamp_iso": _iso(now),
            "quality": 0.95 if price is not None and book_metrics.get("available") else 0.60,
            "book": book_metrics,
            "flow": flow,
        }

    @staticmethod
    def _kraken_first_result(payload: Any, ignore_keys: set[str] | None = None) -> Any:
        if not isinstance(payload, Mapping):
            raise ValueError("unexpected_kraken_payload")
        errors = payload.get("error")
        if isinstance(errors, list) and errors:
            raise RuntimeError("kraken:" + ",".join(map(str, errors)))
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise ValueError("kraken_result_missing")
        ignore = ignore_keys or set()
        for key, value in result.items():
            if key not in ignore:
                return value
        return None

    def _fetch_okx(self, instrument: str, now: float) -> dict[str, Any]:
        base = "https://www.okx.com/api/v5/market"
        ticker = self._client.get(base + "/ticker", {"instId": instrument})
        book = self._client.get(base + "/books", {"instId": instrument, "sz": self.book_levels})
        trades = self._client.get(base + "/trades", {"instId": instrument, "limit": 100})
        ticker_row = self._okx_first(ticker)
        book_row = self._okx_first(book)
        trade_rows = trades.get("data") if isinstance(trades, Mapping) else None
        if not isinstance(trade_rows, list):
            trade_rows = []
        price = _num(ticker_row.get("last")) if isinstance(ticker_row, Mapping) else None
        bids = book_row.get("bids", []) if isinstance(book_row, Mapping) else []
        asks = book_row.get("asks", []) if isinstance(book_row, Mapping) else []
        book_metrics = _weighted_book_metrics(bids, asks, levels=self.book_levels)
        # OKX trade rows use px/sz/side/ts; `side` is the aggressor (taker) side.
        normalized = [
            {"price": row.get("px"), "size": row.get("sz"), "side": row.get("side"), "ts": row.get("ts")}
            for row in trade_rows if isinstance(row, Mapping)
        ]
        flow = _trade_flow(
            normalized,
            side_is_maker=False,
            now=now,
            horizon_seconds=self.trade_horizon_seconds,
        )
        timestamp = _parse_ts(ticker_row.get("ts")) if isinstance(ticker_row, Mapping) else None
        timestamp = timestamp or now
        if price is None and book_metrics.get("available"):
            price = _num(book_metrics.get("mid"))
        return {
            "source": "okx",
            "instrument": instrument,
            "price": price,
            "timestamp": timestamp,
            "timestamp_iso": _iso(timestamp),
            "quality": 0.9 if price is not None and book_metrics.get("available") else 0.6,
            "book": book_metrics,
            "flow": flow,
        }

    @staticmethod
    def _okx_first(payload: Any) -> Any:
        if not isinstance(payload, Mapping):
            raise ValueError("unexpected_okx_payload")
        code = str(payload.get("code", "0"))
        if code not in {"0", ""}:
            raise RuntimeError(f"okx:{code}:{payload.get('msg')}")
        data = payload.get("data")
        if isinstance(data, list) and data:
            return data[0]
        raise ValueError("okx_data_missing")

    def _fetch_deribit(self, instrument: str, now: float) -> dict[str, Any]:
        payload = self._client.get(
            "https://www.deribit.com/api/v2/public/ticker",
            {"instrument_name": instrument},
        )
        if not isinstance(payload, Mapping) or not isinstance(payload.get("result"), Mapping):
            raise ValueError("unexpected_deribit_payload")
        result = payload["result"]
        timestamp = _parse_ts(result.get("timestamp")) or now
        mark = _num(result.get("mark_price"))
        index = _num(result.get("index_price"))
        basis_bps = None
        if mark is not None and index is not None and index > 0:
            basis_bps = (mark / index - 1.0) * 10_000.0
        return {
            "source": "deribit",
            "instrument": instrument,
            "timestamp": timestamp,
            "timestamp_iso": _iso(timestamp),
            "state": result.get("state"),
            "mark_price": mark,
            "index_price": index,
            "last_price": _num(result.get("last_price")),
            "best_bid": _num(result.get("best_bid_price")),
            "best_ask": _num(result.get("best_ask_price")),
            "open_interest": _num(result.get("open_interest")),
            "current_funding": _num(result.get("current_funding")),
            "funding_8h": _num(result.get("funding_8h")),
            "basis_bps": basis_bps,
            "quality": 1.0 if mark is not None and index is not None else 0.5,
        }

    def health(self) -> dict[str, Any]:
        with self._lock:
            cached_assets = sorted(self._cache)
            inflight_assets = sorted(asset for asset, future in self._inflight.items() if not future.done())
            now = time.time()
            open_assets = sorted(a for a, until in self._open_until.items() if now < until)
        return {
            "version": VERSION,
            "enabled": self.enabled,
            "read_only": True,
            "public_endpoints_only": True,
            "requests": self._requests,
            "successes": self._successes,
            "failures": self._failures,
            "last_error": self._last_error,
            "cached_assets": cached_assets,
            "inflight_assets": inflight_assets,
            "breaker_open_assets": open_assets,
            "settings": {
                "refresh_seconds": self.refresh_seconds,
                "stale_seconds": self.stale_seconds,
                "timeout_seconds": self.timeout_seconds,
                "book_levels": self.book_levels,
                "trade_horizon_seconds": self.trade_horizon_seconds,
                "coinbase_enabled": self.coinbase_enabled,
                "kraken_enabled": self.kraken_enabled,
                "deribit_enabled": self.deribit_enabled,
                "okx_enabled": self.okx_enabled,
            },
        }


__all__ = [
    "ASSET_ROUTES",
    "AssetRoute",
    "HttpJsonClient",
    "PublicMarketDataHub",
    "READ_ONLY",
    "VERSION",
    "_trade_flow",
    "_weighted_book_metrics",
]
