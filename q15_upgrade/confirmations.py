from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, List, Sequence


@dataclass
class ConfirmationGroup:
    active: bool
    score: float
    reasons: List[str]


@dataclass
class ConfirmationResult:
    groups: Dict[str, ConfirmationGroup]
    active_group_count: int
    total_score: float
    reasons: List[str]

    def as_dict(self) -> dict:
        return {
            "groups": {name: asdict(group) for name, group in self.groups.items()},
            "active_group_count": self.active_group_count,
            "total_score": self.total_score,
            "reasons": list(self.reasons),
        }


class ConfirmationEngine:
    """Build confirmation from independent source groups.

    Multiple labels from one feed no longer count as multiple confirmations.
    """

    GROUP_WEIGHTS = {
        "price_action": 35.0,
        "trade_flow": 25.0,
        "orderbook": 25.0,
        "cross_market": 15.0,
    }

    def __init__(self):
        self._book_alignment_since: Dict[tuple, float] = {}

    def evaluate(
        self,
        asset: str,
        snapshot: dict,
        direction: str | None,
        follow_through: bool,
        broader_returns: Dict[str, float],
        now: float,
    ) -> ConfirmationResult:
        if direction not in {"bullish", "bearish"}:
            groups = {name: ConfirmationGroup(False, 0.0, []) for name in self.GROUP_WEIGHTS}
            return ConfirmationResult(groups, 0, 0.0, [])

        groups = {
            "price_action": self._price_action(snapshot, direction, follow_through),
            "trade_flow": self._trade_flow(snapshot, direction, now),
            "orderbook": self._orderbook(asset, snapshot, direction, now),
            "cross_market": self._cross_market(asset, direction, broader_returns),
        }
        active_count = sum(1 for group in groups.values() if group.active)
        total_score = round(sum(group.score for group in groups.values()), 1)
        reasons = [reason for group in groups.values() for reason in group.reasons]
        return ConfirmationResult(groups, active_count, total_score, reasons)

    def _price_action(self, snapshot: dict, direction: str, follow_through: bool) -> ConfirmationGroup:
        strength = 0.0
        reasons: List[str] = []
        if follow_through:
            strength += 0.55
            reasons.append("next completed candle continued the setup")

        wick = snapshot.get("latest_wick_metrics") or {}
        crossed = bool(wick.get("crossed_and_reclaimed_target"))
        if crossed:
            strength += 0.30
            reasons.append("target was crossed and reclaimed/lost")

        ratio_key = "lower_wick_to_body_ratio" if direction == "bullish" else "upper_wick_to_body_ratio"
        try:
            ratio = float(wick.get(ratio_key, 0) or 0)
        except (TypeError, ValueError):
            ratio = 0.0
        if ratio >= 1.5:
            strength += min(0.25, 0.10 + ratio / 20.0)
            reasons.append("supportive rejection wick")

        # A price-action group must include actual follow-through.  A wick by
        # itself remains a WATCH condition rather than an entry confirmation.
        active = follow_through and strength >= 0.50
        score = self.GROUP_WEIGHTS["price_action"] * min(1.0, strength)
        return ConfirmationGroup(active, round(score, 1), reasons)

    def _trade_flow(self, snapshot: dict, direction: str, now: float) -> ConfirmationGroup:
        yes = self._num(snapshot.get("taker_yes_volume_15s"), 0.0)
        no = self._num(snapshot.get("taker_no_volume_15s"), 0.0)
        total = yes + no
        recent = snapshot.get("recent_trades") or []
        trade_count = 0
        for trade in recent:
            try:
                if now - float(trade.get("ts")) <= 15.0:
                    trade_count += 1
            except (TypeError, ValueError):
                pass
        if trade_count == 0:
            trade_count = min(len(recent), 12)

        skew = (yes - no) / total if total > 0 else 0.0
        aligned_skew = skew if direction == "bullish" else -skew
        reasons: List[str] = []
        strength = 0.0
        if trade_count >= 3 and aligned_skew >= 0.20:
            strength += min(0.80, 0.45 + aligned_skew * 0.45)
            reasons.append(f"taker flow aligned ({aligned_skew:+.0%})")

        candles = snapshot.get("candles_15s") or []
        if len(candles) >= 2:
            previous = self._num(candles[-2].get("volume"), 0.0)
            current = self._num(candles[-1].get("volume"), 0.0)
            if previous > 0 and current >= previous * 1.20:
                strength += 0.20
                reasons.append("trade volume expanded")

        active = trade_count >= 3 and aligned_skew >= 0.20
        score = self.GROUP_WEIGHTS["trade_flow"] * min(1.0, strength)
        return ConfirmationGroup(active, round(score, 1), reasons)

    def _orderbook(self, asset: str, snapshot: dict, direction: str, now: float) -> ConfirmationGroup:
        imbalance = self._num(snapshot.get("orderbook_imbalance"), 0.0)
        aligned = imbalance if direction == "bullish" else -imbalance
        bid_added = self._num(snapshot.get("bid_liquidity_added"), 0.0)
        ask_added = self._num(snapshot.get("ask_liquidity_added"), 0.0)
        supportive_added = bid_added if direction == "bullish" else ask_added
        opposing_added = ask_added if direction == "bullish" else bid_added

        key = (asset, direction)
        if aligned >= 0.18:
            self._book_alignment_since.setdefault(key, now)
        else:
            self._book_alignment_since.pop(key, None)
        sustained_for = now - self._book_alignment_since.get(key, now)

        reasons: List[str] = []
        strength = 0.0
        if aligned >= 0.18:
            strength += min(0.70, 0.35 + aligned * 0.65)
            reasons.append(f"orderbook imbalance aligned ({aligned:+.0%})")
        if supportive_added > 0 and supportive_added >= max(1.0, opposing_added * 1.4):
            strength += 0.25
            reasons.append("supportive liquidity absorbed/added")

        active = aligned >= 0.18 and sustained_for >= 1.0
        if active:
            reasons.append(f"book support persisted {sustained_for:.1f}s")
        score = self.GROUP_WEIGHTS["orderbook"] * min(1.0, strength)
        return ConfirmationGroup(active, round(score, 1), reasons)

    def _cross_market(self, asset: str, direction: str, returns: Dict[str, float]) -> ConfirmationGroup:
        # Exclude the current asset so BTC can never confirm BTC and ETH can
        # never confirm ETH.  Alts primarily use BTC/ETH; BTC/ETH use peers.
        peers = {name: value for name, value in returns.items() if name != asset}
        if asset not in {"BTC", "ETH"}:
            preferred = {name: peers[name] for name in ("BTC", "ETH") if name in peers}
            if preferred:
                peers = preferred

        aligned: List[tuple] = []
        for name, change in peers.items():
            signed = change if direction == "bullish" else -change
            if signed >= 0.015:
                aligned.append((name, signed))
        aligned.sort(key=lambda item: item[1], reverse=True)

        reasons: List[str] = []
        strength = 0.0
        if aligned:
            names = ", ".join(name for name, _ in aligned[:3])
            reasons.append(f"independent crypto peers aligned: {names}")
            strength = min(1.0, 0.45 + 0.25 * len(aligned) + sum(v for _, v in aligned[:2]) * 2.0)

        # For an alt, one of BTC/ETH is useful; for BTC or ETH, demand either
        # two peers or one peer with a stronger move.
        if asset in {"BTC", "ETH"}:
            active = len(aligned) >= 2 or any(value >= 0.05 for _, value in aligned)
        else:
            active = len(aligned) >= 1
        score = self.GROUP_WEIGHTS["cross_market"] * strength
        return ConfirmationGroup(active, round(score, 1), reasons)

    @staticmethod
    def _num(value, default=None):
        try:
            return float(value) if value is not None else default
        except (TypeError, ValueError):
            return default
