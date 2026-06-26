from __future__ import annotations

import time
from typing import Iterable


class HybridMarketData:
    """Prefer WebSocket data and REST-backfill every reconnect gap."""

    def __init__(self, rest_client, ws_feed):
        self.rest = rest_client
        self.ws = ws_feed
        self._trade_bootstrapped = set()
        self._last_connected = False
        self._last_seen_trade_ts = {}
        self._ticker_sources = {}

    def _sync_connection_state(self) -> bool:
        connected = bool(self.ws and self.ws.is_connected())
        if connected != self._last_connected:
            # Force a REST overlap backfill after each connect/reconnect.
            self._trade_bootstrapped.clear()
            self._last_connected = connected
        return connected

    def subscribe(self, tickers: Iterable[str]) -> None:
        wanted = {ticker for ticker in tickers if ticker}
        self._trade_bootstrapped.intersection_update(wanted)
        self._last_seen_trade_ts = {
            ticker: ts
            for ticker, ts in self._last_seen_trade_ts.items()
            if ticker in wanted
        }
        # Diagnostic metadata only — restrict to the wanted set so it cannot
        # grow unbounded as 15m markets roll over (behavior-neutral).
        self._ticker_sources = {
            ticker: source
            for ticker, source in self._ticker_sources.items()
            if ticker in wanted
        }
        if self.ws:
            self.ws.subscribe(wanted)

    def is_connected(self) -> bool:
        return self._sync_connection_state()

    def health(self) -> dict:
        if not self.ws:
            return {
                "connected": False,
                "mode": "rest-fallback",
                "settlement_feed": False,
                "ticker_sources": dict(self._ticker_sources),
            }
        result = dict(self.ws.health() or {})
        result["ticker_sources"] = dict(self._ticker_sources)
        if hasattr(self.ws, "book_ages"):
            result["book_ages"] = self.ws.book_ages()
        return result

    def get_orderbook(self, ticker):
        if self._sync_connection_state() and self.ws:
            book = self.ws.get_orderbook(ticker)
            if book is not None:
                self._ticker_sources.setdefault(ticker, {})["book"] = "ws"
                if isinstance(book, dict):
                    out = dict(book)
                    out["_hybrid_source"] = "ws"
                    if out.get("_updated_at") is None:
                        out["_updated_at"] = out.get("updated_at")
                    return out
                return book
        self._ticker_sources.setdefault(ticker, {})["book"] = "rest"
        book = self.rest.get_orderbook(ticker)
        if isinstance(book, dict):
            out = dict(book)
            out["_hybrid_source"] = "rest"
            out["_updated_at"] = time.time()
            return out
        return book

    def get_trades(self, ticker, min_ts=None):
        connected = self._sync_connection_state()
        if connected and self.ws:
            if ticker not in self._trade_bootstrapped:
                overlap_min = min_ts
                last_seen = self._last_seen_trade_ts.get(ticker)
                if last_seen is not None:
                    overlap = max(0.0, float(last_seen) - 2.0)
                    overlap_min = (
                        overlap
                        if overlap_min is None
                        else min(float(overlap_min), overlap)
                    )
                rows = self.rest.get_trades(ticker, min_ts=overlap_min)
                self._trade_bootstrapped.add(ticker)
                self._ticker_sources.setdefault(ticker, {})["trades"] = "rest-backfill"
            else:
                rows = self.ws.get_trades(ticker, min_ts=min_ts)
                self._ticker_sources.setdefault(ticker, {})["trades"] = "ws"
        else:
            rows = self.rest.get_trades(ticker, min_ts=min_ts)
            self._ticker_sources.setdefault(ticker, {})["trades"] = "rest"

        deduped = {}
        for row in rows or []:
            key = row.get("id") or (
                row.get("ts"),
                row.get("yes_cents"),
                row.get("count"),
                row.get("taker_side"),
            )
            deduped[key] = row
        output = sorted(
            deduped.values(),
            key=lambda row: float(row.get("ts") or 0.0),
        )
        if output:
            self._last_seen_trade_ts[ticker] = max(
                float(row.get("ts") or 0.0) for row in output
            )
        return output
