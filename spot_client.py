"""Public spot price source used as a CF Benchmark proxy.

CF Benchmarks' real-time index is a licensed feed and is not freely
available, so we approximate the underlying with public spot prices and
emit a data-quality warning. Coinbase covers most assets in USD; BNB and
HYPE are sourced from OKX in USDT (treated as ~USD with a noted caveat).
"""
import time
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


def get_spot(asset):
    """Return {ok, price, bid, ask, ts, source, quote} for an asset."""
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
