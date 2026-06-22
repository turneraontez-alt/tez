"""Challenger v6 RESEARCH feature set — leakage-safe, append-only.

This extends the frozen v5 feature vector with additional decision-time
microstructure features for the challenger's OWN research track. It is built to
PRESERVE the challenger's identity, not to make it copy the production system:

  * the v5 features and their FROZEN order are kept exactly, untouched;
  * the new features are APPENDED after them (so v5 models/ledgers are unaffected
    and a v6 model is a strict superset — never a different lineage);
  * every new feature is computed ONLY from information available at decision
    time and is re-checked against the same ``FORBIDDEN_KEYS`` leakage guard.

These features are NOT wired into the live shadow predictor. They live in the
research harness (``challenger/research.py``), which evaluates whether the v6 set
beats the previous (v5) challenger out-of-sample before anything is promoted.

The dimensions covered map to the workstream-1 upgrade list: strike pressure,
time above/below strike, strike-crossing quality, failed continuation, order-flow
persistence, book resiliency, return entropy/noise, regime transition, spot-vs-
contract disagreement, cross-asset confirmation, and a manipulation-vs-genuine
separation proxy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping

from .features import FEATURE_NAMES, FORBIDDEN_KEYS, assert_no_leakage, extract
from .mathx import clamp, normal_cdf, num

# New decision-time features appended AFTER the frozen v5 names (order frozen here).
V6_DERIVED: tuple[str, ...] = (
    "strike_pressure",
    "time_above_strike_frac",
    "time_below_strike_frac",
    "strike_cross_rate",
    "failed_continuation",
    "flow_persistence",
    "book_resiliency",
    "return_entropy",
    "regime_transition",
    "spot_contract_disagreement",
    "cross_asset_confirmation",
    "manipulation_score",
)

V6_FEATURE_NAMES: tuple[str, ...] = FEATURE_NAMES + V6_DERIVED

# Every decision-time snapshot key the v6 extractor may read. Declared explicitly
# so a test can assert NONE of them looks like outcome/settlement leakage — a
# stronger guarantee than scanning arbitrary caller keys at runtime (which would
# false-positive on a benign key that merely contains a forbidden substring).
V6_READ_KEYS: tuple[str, ...] = (
    "spot", "spot_price", "current_price", "underlying_price",
    "target", "target_price", "strike", "threshold",
    "recent_closes", "minute_closes",
    "taker_yes_volume_15s", "aggressive_yes_volume", "taker_yes_15s",
    "taker_no_volume_15s", "aggressive_no_volume", "taker_no_15s",
    "book_imbalance", "orderbook_imbalance",
    "orderbook_persistence", "book_persistence_ratio", "book_persistent",
    "cross_asset_confirmation", "cross_asset_agreement", "shadow_factor_cross_asset",
)


@dataclass
class FeatureVectorV6:
    details: dict[str, float]
    coverage: dict[str, bool]
    base: Any = None                 # the underlying v5 FeatureVector (context passthrough)
    names: tuple[str, ...] = V6_FEATURE_NAMES
    notes: list[str] = field(default_factory=list)

    @property
    def coverage_fraction(self) -> float:
        if not self.coverage:
            return 0.0
        return sum(1 for v in self.coverage.values() if v) / len(self.coverage)

    def ordered_values(self) -> list[float]:
        return [float(self.details.get(n, 0.0)) for n in self.names]


def _first(snap: Mapping[str, Any], *keys, default=None):
    for k in keys:
        if k in snap and snap[k] is not None:
            return snap[k]
    return default


def _closes(snap: Mapping[str, Any]) -> list[float]:
    raw = _first(snap, "recent_closes", "minute_closes", default=None)
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for c in raw:
        v = num(c)
        if v is not None and v > 0:
            out.append(v)
    return out


def _flow_skew(snap: Mapping[str, Any]) -> tuple[float | None, bool]:
    yes = num(_first(snap, "taker_yes_volume_15s", "aggressive_yes_volume", "taker_yes_15s"))
    no = num(_first(snap, "taker_no_volume_15s", "aggressive_no_volume", "taker_no_15s"))
    if yes is None and no is None:
        return None, False
    yes = yes or 0.0
    no = no or 0.0
    tot = yes + no
    if tot <= 0:
        return 0.0, True
    return (yes - no) / tot, True


def _book_imbalance(snap: Mapping[str, Any]) -> tuple[float | None, bool]:
    b = num(_first(snap, "book_imbalance", "orderbook_imbalance"))
    if b is None:
        return None, False
    if abs(b) > 1.0 and abs(b) <= 100.0:
        b /= 100.0
    return clamp(b, -1.0, 1.0), True


def _persistence(snap: Mapping[str, Any]) -> tuple[float | None, bool]:
    p = num(_first(snap, "orderbook_persistence", "book_persistence_ratio"))
    if p is None:
        flag = _first(snap, "book_persistent", default=None)
        if flag is None:
            return None, False
        return (1.0 if bool(flag) else 0.0), True
    return clamp(p, 0.0, 1.0), True


def extract_v6(snap: Mapping[str, Any]) -> FeatureVectorV6:
    """Build the v6 feature vector: the v5 features plus the appended research
    features. Robust to missing data (absent source -> 0.0 with coverage False)."""
    base = extract(snap)
    details: dict[str, float] = dict(base.details)
    coverage: dict[str, bool] = {n: c for n, c in zip(base.names, base.coverage)}
    notes: list[str] = []

    spot = num(_first(snap, "spot", "spot_price", "current_price", "underlying_price"))
    target = num(_first(snap, "target", "target_price", "strike", "threshold"))
    closes = _closes(snap)
    direction_needed = 0.0
    if spot is not None and target is not None and spot > 0:
        direction_needed = 1.0 if target > spot else (-1.0 if target < spot else 0.0)

    flow_skew, flow_present = _flow_skew(snap)
    book_imb, book_present = _book_imbalance(snap)
    persistence, persist_present = _persistence(snap)

    def put(name: str, value: float, present: bool) -> None:
        details[name] = float(value)
        coverage[name] = bool(present)

    # 1. strike_pressure: order flow aligned with the direction needed to cross.
    #    +1 => flow pushing the price toward the strike; -1 => away.
    if flow_present and flow_skew is not None and direction_needed != 0.0:
        put("strike_pressure", clamp(direction_needed * flow_skew, -1.0, 1.0), True)
    else:
        put("strike_pressure", 0.0, False)

    # 2/3. time above/below strike: fraction of recent closes on each side of it.
    if closes and target is not None and len(closes) >= 2:
        above = sum(1 for c in closes if c >= target) / len(closes)
        put("time_above_strike_frac", above, True)
        put("time_below_strike_frac", 1.0 - above, True)
    else:
        put("time_above_strike_frac", 0.0, False)
        put("time_below_strike_frac", 0.0, False)

    # 4. strike_cross_rate: how often the path crosses the strike (chop quality).
    if closes and target is not None and len(closes) >= 3:
        signs = [1 if c >= target else -1 for c in closes]
        crosses = sum(1 for a, b in zip(signs, signs[1:]) if a != b)
        put("strike_cross_rate", crosses / (len(signs) - 1), True)
    else:
        put("strike_cross_rate", 0.0, False)

    # 5. failed_continuation: last 1-min return reversing the prior trend (a stall).
    if len(closes) >= 4:
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
        if len(rets) >= 3:
            prior = sum(rets[:-1]) / len(rets[:-1])
            last = rets[-1]
            # 1 when the latest return opposes the prior drift (failed continuation).
            put("failed_continuation", 1.0 if (prior * last) < 0 else 0.0, True)
        else:
            put("failed_continuation", 0.0, False)
    else:
        put("failed_continuation", 0.0, False)

    # 6. flow_persistence: |flow skew| weighted by book persistence (durable flow).
    if flow_present and flow_skew is not None and persist_present and persistence is not None:
        put("flow_persistence", clamp(abs(flow_skew) * persistence, 0.0, 1.0), True)
    else:
        put("flow_persistence", 0.0, False)

    # 7. book_resiliency: signed book imbalance scaled by its persistence.
    if book_present and book_imb is not None and persist_present and persistence is not None:
        put("book_resiliency", clamp(book_imb * persistence, -1.0, 1.0), True)
    else:
        put("book_resiliency", 0.0, False)

    # 8. return_entropy: Shannon entropy of up/down return signs (noise measure).
    if len(closes) >= 4:
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
        ups = sum(1 for r in rets if r > 0)
        n = len(rets)
        if n > 0:
            p_up = ups / n
            ent = 0.0
            for p in (p_up, 1.0 - p_up):
                if p > 0:
                    ent -= p * math.log(p, 2)
            put("return_entropy", clamp(ent, 0.0, 1.0), True)
        else:
            put("return_entropy", 0.0, False)
    else:
        put("return_entropy", 0.0, False)

    # 9. regime_transition: recent-half volatility vs earlier-half (>1 ramping up).
    if len(closes) >= 6:
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
        if len(rets) >= 4:
            half = len(rets) // 2
            early, late = rets[:half], rets[half:]

            def _sd(xs):
                m = sum(xs) / len(xs)
                return math.sqrt(sum((x - m) ** 2 for x in xs) / max(1, len(xs) - 1))
            se, sl = _sd(early), _sd(late)
            ratio = (sl / se) if se > 1e-12 else 1.0
            # map ratio to [-1, 1] via tanh of log-ratio (0 => no transition).
            put("regime_transition", clamp(math.tanh(math.log(max(ratio, 1e-6))), -1.0, 1.0), True)
        else:
            put("regime_transition", 0.0, False)
    else:
        put("regime_transition", 0.0, False)

    # 10. spot_contract_disagreement: structural P(yes) minus market-implied P(yes).
    #     Both are decision-time; the gap is a leakage-safe model-vs-market signal.
    nd = details.get("normalized_distance", 0.0)
    nd_present = coverage.get("normalized_distance", False)
    market_p = base.market_yes_prob
    if nd_present and market_p is not None:
        structural_p = normal_cdf(-nd)
        put("spot_contract_disagreement", clamp(structural_p - market_p, -1.0, 1.0), True)
    else:
        put("spot_contract_disagreement", 0.0, False)

    # 11. cross_asset_confirmation: provided cross-asset directional agreement.
    xa = num(_first(snap, "cross_asset_confirmation", "cross_asset_agreement",
                    "shadow_factor_cross_asset"))
    if xa is not None:
        if abs(xa) > 1.0 and abs(xa) <= 100.0:
            xa /= 100.0
        put("cross_asset_confirmation", clamp(xa, -1.0, 1.0), True)
    else:
        put("cross_asset_confirmation", 0.0, False)

    # 12. manipulation_score: spoof-like signature — strong book imbalance with LOW
    #     persistence and flow pushing AGAINST the book (decision-time only).
    if (book_present and book_imb is not None and persist_present and persistence is not None
            and flow_present and flow_skew is not None):
        against = 1.0 if (book_imb * flow_skew) < 0 else 0.0
        score = clamp(abs(book_imb) * (1.0 - persistence) * against, 0.0, 1.0)
        put("manipulation_score", score, True)
    else:
        put("manipulation_score", 0.0, False)

    return FeatureVectorV6(details=details, coverage=coverage, base=base, notes=notes)


def ordered_vector_v6(feature_dict: Mapping[str, Any]) -> list[float]:
    """Map a stored v6 feature_details dict to the frozen V6_FEATURE_NAMES order."""
    return [float(feature_dict.get(name, 0.0)) for name in V6_FEATURE_NAMES]


__all__ = [
    "V6_DERIVED", "V6_FEATURE_NAMES", "V6_READ_KEYS", "FeatureVectorV6", "extract_v6",
    "ordered_vector_v6", "FORBIDDEN_KEYS", "assert_no_leakage",
]
