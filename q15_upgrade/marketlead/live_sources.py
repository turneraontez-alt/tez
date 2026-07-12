"""Read-only adapters for already-running local market-data collectors."""
from __future__ import annotations

import json
import math
from typing import Any, Mapping


_PRODUCT_BY_ASSET = {
    asset: f"{asset}-USD"
    for asset in ("BTC", "ETH", "SOL", "DOGE", "XRP", "BNB", "HYPE")
}
_SYMBOL_BY_ASSET = {
    asset: f"{asset}/USD"
    for asset in ("BTC", "ETH", "SOL", "DOGE", "XRP", "BNB", "HYPE")
}


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _top_size(snapshot: Mapping[str, Any], side: str) -> float | None:
    direct = _num(snapshot.get(f"{side}_depth_top"))
    if direct is not None:
        return direct
    try:
        levels = json.loads(str(snapshot.get(f"{side}_levels_json") or "[]"))
        return _num(levels[0][1]) if levels else None
    except (TypeError, ValueError, json.JSONDecodeError, IndexError):
        return None


def _normalize(
    snapshot: Mapping[str, Any] | None,
    *,
    instrument: str,
    transport: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if not snapshot:
        return None, {
            "status": "missing",
            "instrument": instrument,
            "transport": transport,
        }
    bid = _num(snapshot.get("best_bid"))
    ask = _num(snapshot.get("best_ask"))
    mid = _num(snapshot.get("mid"))
    timestamp = _num(snapshot.get("sample_timestamp"))
    book_timestamp = _num(snapshot.get("book_timestamp"))
    if bid is None or ask is None or mid is None or timestamp is None:
        return None, {
            "status": "invalid",
            "instrument": instrument,
            "transport": transport,
        }
    if bid <= 0 or ask <= bid or mid <= 0:
        return None, {
            "status": "crossed_or_invalid",
            "instrument": instrument,
            "transport": transport,
            "best_bid": bid,
            "best_ask": ask,
        }
    bid_size = _top_size(snapshot, "bid")
    ask_size = _top_size(snapshot, "ask")
    microprice_bps = None
    if bid_size is not None and ask_size is not None and bid_size + ask_size > 0:
        microprice = (ask * bid_size + bid * ask_size) / (bid_size + ask_size)
        microprice_bps = (microprice / mid - 1.0) * 10_000.0
    buy = _num(snapshot.get("matched_buy_notional_60s"))
    sell = _num(snapshot.get("matched_sell_notional_60s"))
    flow_imbalance = None
    if buy is not None and sell is not None and buy + sell > 0:
        flow_imbalance = (buy - sell) / (buy + sell)
    source = {
        "price": mid,
        "timestamp": timestamp,
        "quality": 1.0,
        "transport": transport,
        "instrument": instrument,
        "best_bid": bid,
        "best_ask": ask,
        "spread_bps": _num(snapshot.get("spread_bps")),
        "sequence": snapshot.get("sequence_num") or snapshot.get("checksum"),
        "transport_connected": bool(snapshot.get("transport_connected")),
        "transport_message_age_seconds": _num(
            snapshot.get("last_message_age_seconds")
        ),
        "book_update_timestamp": book_timestamp,
        "book_update_age_seconds": _num(snapshot.get("book_age_seconds")),
        "flow": {"imbalance": flow_imbalance},
        "book": {
            "imbalance": _num(snapshot.get("depth_imbalance")),
            "microprice_bps": microprice_bps,
        },
    }
    return source, {
        "status": "ok",
        "instrument": instrument,
        "transport": transport,
        "timestamp": timestamp,
        "book_update_timestamp": book_timestamp,
        "book_update_age_seconds": _num(snapshot.get("book_age_seconds")),
        "transport_connected": bool(snapshot.get("transport_connected")),
        "transport_message_age_seconds": _num(
            snapshot.get("last_message_age_seconds")
        ),
    }


def live_market_sources(asset: str, now: float) -> dict[str, Any]:
    """Read Coinbase/Kraken singleton books without starting feeds or doing I/O."""
    normalized = str(asset or "").upper()
    sources: dict[str, Any] = {}
    diagnostics: dict[str, Any] = {}

    product = _PRODUCT_BY_ASSET.get(normalized)
    if product is None:
        return {"sources": {}, "diagnostics": {"asset": {"status": "unsupported"}}}

    try:
        from coinbase_adv_l2 import peek_coinbase_adv_l2_feed

        feed = peek_coinbase_adv_l2_feed()
        if feed is None:
            diagnostics["coinbase"] = {
                "status": "collector_not_running",
                "instrument": product,
            }
        else:
            source, diagnostic = _normalize(
                feed.latest_top_snapshot(product, now=now),
                instrument=product,
                transport="websocket_l2",
            )
            diagnostics["coinbase"] = diagnostic
            if source is not None:
                sources["coinbase"] = source
    except Exception as exc:
        diagnostics["coinbase"] = {
            "status": f"error:{type(exc).__name__}",
            "instrument": product,
        }

    symbol = _SYMBOL_BY_ASSET[normalized]
    try:
        from kraken_l3 import peek_kraken_l3_feed

        feed = peek_kraken_l3_feed()
        if feed is None:
            diagnostics["kraken"] = {
                "status": "collector_not_running",
                "instrument": symbol,
            }
        else:
            source, diagnostic = _normalize(
                feed.latest_top_snapshot(symbol, now=now),
                instrument=symbol,
                transport="websocket_l3",
            )
            diagnostics["kraken"] = diagnostic
            if source is not None:
                sources["kraken"] = source
    except Exception as exc:
        diagnostics["kraken"] = {
            "status": f"error:{type(exc).__name__}",
            "instrument": symbol,
        }

    return {"sources": sources, "diagnostics": diagnostics}


__all__ = ["live_market_sources"]
