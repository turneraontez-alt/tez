from __future__ import annotations

from collections import defaultdict
from typing import Dict, Iterable, List, Sequence, Tuple

from .precision import dollars_to_cents


def _levels(raw_levels) -> List[List[float]]:
    out: List[List[float]] = []
    for level in raw_levels or []:
        try:
            price_raw, qty_raw = level[0], level[1]
        except Exception:
            continue
        price = dollars_to_cents(price_raw)
        try:
            qty = float(qty_raw)
        except (TypeError, ValueError):
            continue
        if price is None or qty <= 0:
            continue
        out.append([round(float(price), 4), qty])
    out.sort(key=lambda x: x[0])
    return out


def _normalize_raw(raw):
    if not isinstance(raw, dict):
        return {}
    for key in ("orderbook_fp", "orderbook"):
        nested = raw.get(key)
        if isinstance(nested, dict):
            raw = nested
            break
    return raw


def parse_orderbook(raw):
    """Parse Kalshi REST or WebSocket orderbooks without cent rounding.

    Kalshi exposes YES bids and NO bids.  The opposite side's bids imply asks:
    a NO bid at 35c is a YES ask at 65c, and vice versa.
    """
    raw = _normalize_raw(raw)
    yes_raw = (
        raw.get("yes_dollars")
        or raw.get("yes_dollars_fp")
        or raw.get("yes")
        or []
    )
    no_raw = (
        raw.get("no_dollars")
        or raw.get("no_dollars_fp")
        or raw.get("no")
        or []
    )
    yes_bids_asc = _levels(yes_raw)
    no_bids_asc = _levels(no_raw)

    yes_bid_levels = sorted(yes_bids_asc, key=lambda x: x[0], reverse=True)
    no_bid_levels = sorted(no_bids_asc, key=lambda x: x[0], reverse=True)
    yes_ask_levels = sorted([[round(100.0 - p, 4), q] for p, q in no_bids_asc], key=lambda x: x[0])
    no_ask_levels = sorted([[round(100.0 - p, 4), q] for p, q in yes_bids_asc], key=lambda x: x[0])

    yes_bid = yes_bid_levels[0][0] if yes_bid_levels else None
    yes_bid_qty = yes_bid_levels[0][1] if yes_bid_levels else None
    no_bid = no_bid_levels[0][0] if no_bid_levels else None
    no_bid_qty = no_bid_levels[0][1] if no_bid_levels else None
    yes_ask = yes_ask_levels[0][0] if yes_ask_levels else None
    yes_ask_qty = yes_ask_levels[0][1] if yes_ask_levels else None
    no_ask = no_ask_levels[0][0] if no_ask_levels else None
    no_ask_qty = no_ask_levels[0][1] if no_ask_levels else None

    spread = None
    if yes_bid is not None and yes_ask is not None:
        spread = round(yes_ask - yes_bid, 4)

    def depth_within(levels: Sequence[Sequence[float]], best: float | None, width: float = 3.0) -> float:
        if best is None:
            return 0.0
        return round(sum(float(q) for p, q in levels if float(p) <= best + width), 2)

    return {
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "yes_bid_qty": yes_bid_qty,
        "yes_ask_qty": yes_ask_qty,
        "no_bid_qty": no_bid_qty,
        "no_ask_qty": no_ask_qty,
        "spread": spread,
        "yes_bid_levels": yes_bid_levels,
        "yes_ask_levels": yes_ask_levels,
        "no_bid_levels": no_bid_levels,
        "no_ask_levels": no_ask_levels,
        "depth_3c_yes": depth_within(yes_ask_levels, yes_ask),
        "depth_3c_no": depth_within(no_ask_levels, no_ask),
    }


class OrderbookTracker:
    """Tracks book changes while preserving sub-cent price levels."""

    def __init__(self):
        self.prev = None

    @staticmethod
    def _map(levels):
        return {round(float(p), 4): float(q) for p, q in (levels or [])}

    def update(self, parsed):
        yes_bids = self._map(parsed.get("yes_bid_levels"))
        yes_asks = self._map(parsed.get("yes_ask_levels"))

        bid_added = bid_removed = ask_added = ask_removed = 0.0
        imbalance_flip = False
        previous_imbalance = None
        if self.prev:
            prev_bids = self._map(self.prev.get("yes_bid_levels"))
            prev_asks = self._map(self.prev.get("yes_ask_levels"))
            for p in set(prev_bids) | set(yes_bids):
                change = yes_bids.get(p, 0.0) - prev_bids.get(p, 0.0)
                if change > 0:
                    bid_added += change
                else:
                    bid_removed += -change
            for p in set(prev_asks) | set(yes_asks):
                change = yes_asks.get(p, 0.0) - prev_asks.get(p, 0.0)
                if change > 0:
                    ask_added += change
                else:
                    ask_removed += -change
            previous_imbalance = self.prev.get("_imbalance")

        bid_depth = sum(yes_bids.values())
        ask_depth = sum(yes_asks.values())
        total = bid_depth + ask_depth
        imbalance = ((bid_depth - ask_depth) / total) if total > 0 else None
        if previous_imbalance is not None and imbalance is not None:
            imbalance_flip = (previous_imbalance <= 0 < imbalance) or (previous_imbalance >= 0 > imbalance)

        stored = dict(parsed)
        stored["_imbalance"] = imbalance
        self.prev = stored
        return {
            "orderbook_imbalance": round(imbalance, 4) if imbalance is not None else None,
            "imbalance_flip": imbalance_flip,
            "bid_liquidity_added": round(bid_added, 2),
            "bid_liquidity_removed": round(bid_removed, 2),
            "ask_liquidity_added": round(ask_added, 2),
            "ask_liquidity_removed": round(ask_removed, 2),
        }
