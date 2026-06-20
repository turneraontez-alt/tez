"""Shared cache of immutable settled-market results.

Four independent settlement reconcilers — performance.py, q15_upgrade/learning.py,
q15_upgrade/window_focus.py, and the V9.5 ledger — each poll Kalshi's
``get_market`` for the same closed contracts. A market's official result is
immutable once it resolves YES/NO, so the first resolved fetch is cached and
served to every other caller, collapsing the ~4x duplicate REST calls per
settled market down to one. Unresolved markets are never cached, so live polling
continues normally until settlement.

The cache is a transparent proxy: any attribute other than ``get_market`` is
delegated to the wrapped client, so it is a drop-in replacement anywhere the
Kalshi client is used for settlement.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Mapping


class MarketResultCache:
    def __init__(self, client: Any, max_entries: int = 4000) -> None:
        self._client = client
        self._max_entries = max(1, int(max_entries))
        self._resolved: "OrderedDict[str, Any]" = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._fetches = 0

    @staticmethod
    def _resolved_result(market: Any) -> str | None:
        """The immutable YES/NO outcome, or None if not yet officially resolved."""
        if not isinstance(market, Mapping):
            return None
        result = str(market.get("result") or "").upper()
        return result if result in ("YES", "NO") else None

    def get_market(self, ticker: str) -> Any:
        if ticker:
            with self._lock:
                cached = self._resolved.get(ticker)
                if cached is not None:
                    self._resolved.move_to_end(ticker)
                    self._hits += 1
                    return cached
        with self._lock:
            self._fetches += 1
        market = self._client.get_market(ticker)
        if ticker and self._resolved_result(market) is not None:
            with self._lock:
                self._resolved[ticker] = market
                self._resolved.move_to_end(ticker)
                while len(self._resolved) > self._max_entries:
                    self._resolved.popitem(last=False)
        return market

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._hits + self._fetches
            return {
                "cached_results": len(self._resolved),
                "hits": self._hits,
                "fetches": self._fetches,
                "hit_rate": round(self._hits / total, 4) if total else None,
                "max_entries": self._max_entries,
            }

    def __getattr__(self, name: str) -> Any:
        # Reached only for attributes not found on the instance. Guard _client to
        # avoid infinite recursion before __init__ assigns it.
        if name == "_client":
            raise AttributeError(name)
        return getattr(self._client, name)
