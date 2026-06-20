"""Orderbook parsing and snapshot-diff liquidity tracking.

Kalshi's public REST gives full orderbook snapshots (no delta channel without
the authenticated WebSocket), so we approximate added/removed liquidity by
diffing successive snapshots. This is labeled approximate in data-quality
warnings. The Kalshi book holds YES bids and NO bids; a NO bid at price p is
equivalent to a YES ask at (100 - p).
"""


def parse_orderbook(ob):
    """Parse orderbook_fp -> best prices, depth, and full level maps (cents)."""
    out = dict(
        yes_bid=None, yes_ask=None, no_bid=None, no_ask=None,
        yes_bid_qty=0.0, yes_ask_qty=0.0, no_bid_qty=0.0, no_ask_qty=0.0,
        depth_1c_yes=0.0, depth_3c_yes=0.0, depth_1c_no=0.0, depth_3c_no=0.0,
        yes_levels={}, no_levels={},
    )
    if not ob:
        return out

    def parse_levels(raw):
        levels = {}
        for item in raw or []:
            try:
                p = round(float(item[0]) * 100)
                q = float(item[1])
                levels[p] = levels.get(p, 0.0) + q
            except (TypeError, ValueError, IndexError):
                pass
        return levels

    yes_levels = parse_levels(ob.get("yes_dollars", []))
    no_levels = parse_levels(ob.get("no_dollars", []))
    out["yes_levels"] = yes_levels
    out["no_levels"] = no_levels

    if yes_levels:
        best = max(yes_levels)
        out["yes_bid"] = best
        out["yes_bid_qty"] = yes_levels[best]
        for p, q in yes_levels.items():
            if abs(p - best) <= 1:
                out["depth_1c_yes"] += q
            if abs(p - best) <= 3:
                out["depth_3c_yes"] += q

    if no_levels:
        best = max(no_levels)
        out["no_bid"] = best
        out["no_bid_qty"] = no_levels[best]
        for p, q in no_levels.items():
            if abs(p - best) <= 1:
                out["depth_1c_no"] += q
            if abs(p - best) <= 3:
                out["depth_3c_no"] += q

    if out["no_bid"] is not None:
        out["yes_ask"] = 100 - out["no_bid"]
        out["yes_ask_qty"] = out["no_bid_qty"]
    if out["yes_bid"] is not None:
        out["no_ask"] = 100 - out["yes_bid"]
        out["no_ask_qty"] = out["yes_bid_qty"]

    return out


def _diff_levels(prev, cur):
    """Return (added, removed) total quantity between two level maps."""
    added = removed = 0.0
    for p, q in cur.items():
        d = q - prev.get(p, 0.0)
        if d > 0:
            added += d
    for p, q in prev.items():
        d = cur.get(p, 0.0) - q
        if d < 0:
            removed += -d
    return added, removed


class OrderbookTracker:
    """Tracks liquidity added/removed and imbalance across snapshots."""

    def __init__(self):
        self.prev_yes = {}
        self.prev_no = {}
        self.has_prev = False
        self.prev_imbalance = None

    def update(self, parsed):
        yes_levels = parsed.get("yes_levels", {})
        no_levels = parsed.get("no_levels", {})

        res = {
            "bid_liquidity_added": 0.0, "bid_liquidity_removed": 0.0,
            "ask_liquidity_added": 0.0, "ask_liquidity_removed": 0.0,
            "orderbook_imbalance": None, "imbalance_flip": False,
        }

        if self.has_prev:
            ba, br = _diff_levels(self.prev_yes, yes_levels)
            aa, ar = _diff_levels(self.prev_no, no_levels)
            res["bid_liquidity_added"] = round(ba, 2)
            res["bid_liquidity_removed"] = round(br, 2)
            res["ask_liquidity_added"] = round(aa, 2)
            res["ask_liquidity_removed"] = round(ar, 2)

        # Imbalance from near-top depth (YES bid vs NO bid within 3c).
        yes_depth = parsed.get("depth_3c_yes", 0.0)
        no_depth = parsed.get("depth_3c_no", 0.0)
        total = yes_depth + no_depth
        if total > 0:
            imb = (yes_depth - no_depth) / total
            res["orderbook_imbalance"] = round(imb, 3)
            if self.prev_imbalance is not None:
                # A flip is a sign change with meaningful magnitude.
                if (self.prev_imbalance > 0.1 and imb < -0.1) or \
                   (self.prev_imbalance < -0.1 and imb > 0.1):
                    res["imbalance_flip"] = True
            self.prev_imbalance = imb

        self.prev_yes = yes_levels
        self.prev_no = no_levels
        self.has_prev = True
        return res
