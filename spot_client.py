"""Public spot price source used as a CF Benchmark proxy.

CF Benchmarks' real-time index is a licensed feed and is not freely
available, so we approximate the underlying with public spot prices and
emit a data-quality warning. Coinbase covers most assets in USD; BNB and
HYPE are sourced from OKX in USDT (treated as ~USD with a noted caveat).
"""
import os
import time
import threading
import logging

import requests

logger = logging.getLogger(__name__)

# asset -> (provider, symbol, quote_currency)
SPOT_SOURCES = {
    "BTC":  ("coinbase", "BTC-USD", "USD"),
    "ETH":  ("coinbase", "ETH-USD", "USD"),
    "SOL":  ("coinbase", "SOL-USD", "USD"),
    "XRP":  ("coinbase", "XRP-USD", "USD"),
    "DOGE": ("coinbase", "DOGE-USD", "USD"),
    "BNB":  ("okx", "BNB-USDT", "USDT"),
    "HYPE": ("okx", "HYPE-USDT", "USDT"),
}

# Last successful spot per asset, so a single transient fetch failure (rate
# limit, network blip) does not blank `underlying_current` and force the whole
# checkpoint into "NO PREDICTION (underlying spot price unavailable)". A cached
# price is reused for at most Q15_SPOT_MAX_STALE_SECONDS, after which we honestly
# report failure rather than predict on a stale price.
_LAST_GOOD = {}
_LAST_GOOD_LOCK = threading.Lock()


def _max_stale_seconds():
    try:
        return max(0.0, float(os.environ.get("Q15_SPOT_MAX_STALE_SECONDS", "30") or 30))
    except (TypeError, ValueError):
        return 30.0


def _ws_spot(asset):
    """Fresh websocket tick for ``asset`` in get_spot() shape, or None.

    Imported lazily to avoid an import cycle and to keep the websocket feed
    entirely optional. Never raises: any problem falls through to the REST path.
    """
    try:
        from spot_ws import get_ws_spot
        return get_ws_spot(asset)
    except Exception:
        return None


def get_spot(asset):
    """Return {ok, price, bid, ask, ts, source, quote} for an asset.

    On a fetch failure, fall back to the last good price for this asset if it is
    still within the staleness budget, flagged with ``stale``/``stale_age_seconds``
    so downstream surfaces can see it is a fallback.
    """
    # Optional low-latency path: prefer a fresh public-exchange websocket tick
    # when Q15_SPOT_WS_ENABLED is set. Returns None otherwise, so the default
    # REST behaviour below is unchanged; a per-asset stale/missing tick also
    # falls through to REST + last-good.
    ws = _ws_spot(asset)
    if ws is not None:
        with _LAST_GOOD_LOCK:
            _LAST_GOOD[asset] = (time.time(), dict(ws))
        return ws
    result = _fetch_spot(asset)
    now = time.time()
    if result.get("ok") and result.get("price"):
        with _LAST_GOOD_LOCK:
            _LAST_GOOD[asset] = (now, dict(result))
        return result
    with _LAST_GOOD_LOCK:
        cached = _LAST_GOOD.get(asset)
    if cached:
        cached_at, good = cached
        age = now - cached_at
        if 0.0 <= age <= _max_stale_seconds():
            fallback = dict(good)
            # Stamp the reuse time so candle/freshness continuity holds, but keep
            # the failure reason and original timestamp visible for diagnostics.
            fallback["ts"] = now
            fallback["stale"] = True
            fallback["stale_age_seconds"] = round(age, 3)
            fallback["original_ts"] = good.get("ts")
            fallback["error"] = result.get("error")
            fallback["source"] = f"{good.get('source')} (last good {age:.0f}s)"
            logger.warning(
                "Spot fetch for %s failed (%s); reusing last good price %.6gs old",
                asset, result.get("error"), age,
            )
            return fallback
    return result


def _fetch_spot(asset):
    """Single live fetch from the configured public source (no caching)."""
    src, sym, quote = SPOT_SOURCES.get(asset, (None, None, None))
    if src is None:
        return {"ok": False, "price": None, "source": None, "error": "no spot source"}
    try:
        if src == "coinbase":
            r = requests.get(
                f"https://api.exchange.coinbase.com/products/{sym}/ticker",
                timeout=(3.05, 4), headers={"User-Agent": "kalshi-monitor"},
            )
            r.raise_for_status()
            d = r.json()
            return {
                "ok": True,
                "price": float(d["price"]),
                "bid": float(d["bid"]),
                "ask": float(d["ask"]),
                "ts": time.time(),
                "source": f"Coinbase {sym}",
                "quote": quote,
            }
        if src == "okx":
            r = requests.get(
                f"https://www.okx.com/api/v5/market/ticker?instId={sym}",
                timeout=(3.05, 4),
            )
            r.raise_for_status()
            d = r.json()["data"][0]
            return {
                "ok": True,
                "price": float(d["last"]),
                "bid": float(d["bidPx"]),
                "ask": float(d["askPx"]),
                "ts": time.time(),
                "source": f"OKX {sym}",
                "quote": quote,
            }
    except Exception as e:
        logger.warning(f"Spot fetch failed for {asset} ({src} {sym}): {e}")
        return {"ok": False, "price": None, "source": f"{src} {sym}", "error": str(e)}
    return {"ok": False, "price": None, "source": None, "error": "unknown provider"}
