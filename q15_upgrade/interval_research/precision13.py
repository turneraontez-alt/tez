"""High-precision 13M research policy.

The policy is deliberately selective. It emits a side only when the complete
seven-asset 13M slate and the same contract's earlier 15M probability form one
of two historically stable regimes. All other rows abstain and remain eligible
for a later checkpoint. This module is pure research code: it does not deliver,
trade, persist, or alter the champion prediction.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
import time
from typing import Any, Mapping, Sequence


EXPECTED_ASSETS = frozenset({"BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP"})
POLICY_VERSION = "q15-13m-selective-precision-v2"
NOTIFICATION_RECORD_KIND = "PRECISION_13M_SIZED_V2"


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Precision13Policy:
    """Frozen thresholds from the 944-complete-window retrospective audit."""

    yes_probability_min: float = 0.60
    yes_window_mean_min: float = 0.62
    yes_breadth_min: int = 6
    yes_btc_probability_min: float = 0.54
    yes_probability_change_min: float = -0.05

    no_probability_max: float = 0.35
    no_window_mean_max: float = 0.50
    no_breadth_max: int = 1
    no_btc_probability_max: float = 0.50
    no_probability_change_max: float = -0.075


def sizing_components(
    *,
    side: str,
    probability_13m: Any,
    entry_ask_cents: Any,
    spread_cents: Any,
    captured_at: Any,
    distance_from_strike: Any = None,
) -> dict[str, Any]:
    """Return the frozen Drift-derived research sizing components.

    These values are advisory metadata for V3 cards and shadow economics. They
    never place or size an order. The near-strike observation is intentionally
    a tag, not a filter or multiplier.
    """
    probability = _num(probability_13m)
    ask = _num(entry_ask_cents)
    spread = _num(spread_cents)
    timestamp = _num(captured_at)
    distance = _num(distance_from_strike)
    normalized_side = str(side or "").upper()
    side_probability = None
    disagreement = None
    if probability is not None and normalized_side in {"YES", "NO"}:
        side_probability = probability if normalized_side == "YES" else 1.0 - probability
        if ask is not None:
            disagreement = side_probability - ask / 100.0

    if disagreement is None:
        disagreement_weight = 1.0
    elif disagreement >= -0.0917:
        disagreement_weight = 1.5
    elif disagreement < -0.1157:
        disagreement_weight = 0.5
    else:
        disagreement_weight = 1.0

    if spread is None or spread <= 2.0:
        spread_weight = 1.0
    elif spread <= 4.0:
        spread_weight = 1.5
    else:
        spread_weight = 0.5

    hour_utc = None
    if timestamp is not None:
        try:
            hour_utc = time.gmtime(timestamp).tm_hour
        except (OverflowError, OSError, ValueError):
            hour_utc = None
    if hour_utc is None:
        session_weight = 1.0
    elif 16 <= hour_utc <= 23:
        session_weight = 1.33
    elif 8 <= hour_utc <= 15:
        session_weight = 0.75
    else:
        session_weight = 0.84

    stack_weight = max(0.5, min(1.5, spread_weight * session_weight))
    recommended_size_weight = disagreement_weight * stack_weight
    return {
        "side_probability": side_probability,
        "disagreement": disagreement,
        "disagreement_weight": disagreement_weight,
        "spread_weight": spread_weight,
        "session_weight": session_weight,
        "stack_weight": stack_weight,
        "recommended_size_weight": recommended_size_weight,
        "hour_utc": hour_utc,
        "near_strike_quality": distance is not None and distance <= 3e-5,
    }


def evaluate_slate(
    captures_13m: Sequence[Mapping[str, Any]],
    captures_15m: Mapping[str, Mapping[str, Any]],
    policy: Precision13Policy | None = None,
) -> list[dict[str, Any]]:
    """Return one leakage-safe research decision per complete-slate row.

    ``captures_15m`` is keyed by ticker. Missing or incomplete source data fails
    closed to ``ABSTAIN``; no row is silently scored with fabricated defaults.
    """
    cfg = policy or Precision13Policy()
    rows = [dict(row) for row in captures_13m]
    assets = {str(row.get("asset") or "").upper() for row in rows}
    probabilities = [_num(row.get("calibrated_yes_probability")) for row in rows]
    complete = (
        len(rows) == len(EXPECTED_ASSETS)
        and assets == EXPECTED_ASSETS
        and all(value is not None for value in probabilities)
    )
    if not complete:
        return [
            {
                "ticker": row.get("ticker"),
                "asset": row.get("asset"),
                "decision": "ABSTAIN",
                "reason": "INCOMPLETE_13M_SLATE",
            }
            for row in rows
        ]

    probability_values = [float(value) for value in probabilities if value is not None]
    window_mean = mean(probability_values)
    breadth = sum(value >= 0.50 for value in probability_values)
    btc_probability = next(
        float(probability_values[index])
        for index, row in enumerate(rows)
        if str(row.get("asset") or "").upper() == "BTC"
    )

    decisions: list[dict[str, Any]] = []
    for row, probability in zip(rows, probability_values):
        ticker = str(row.get("ticker") or "")
        prior = captures_15m.get(ticker)
        probability_15m = _num(
            prior.get("calibrated_yes_probability") if prior is not None else None
        )
        if not ticker or probability_15m is None:
            decision = "ABSTAIN"
            reason = "MISSING_15M_PROBABILITY"
            probability_change = None
        else:
            probability_change = probability - probability_15m
            yes = (
                probability >= cfg.yes_probability_min
                and window_mean >= cfg.yes_window_mean_min
                and breadth >= cfg.yes_breadth_min
                and btc_probability >= cfg.yes_btc_probability_min
                and probability_change >= cfg.yes_probability_change_min
            )
            no = (
                probability <= cfg.no_probability_max
                and window_mean <= cfg.no_window_mean_max
                and breadth <= cfg.no_breadth_max
                and btc_probability <= cfg.no_btc_probability_max
                and probability_change <= cfg.no_probability_change_max
            )
            if yes:
                decision, reason = "YES", "SYNCHRONIZED_YES_REGIME"
            elif no:
                decision, reason = "NO", "CONFIRMED_NO_EXPANSION"
            else:
                decision, reason = "ABSTAIN", "PRECISION_CONDITIONS_NOT_MET"

        decisions.append(
            {
                "ticker": ticker,
                "asset": row.get("asset"),
                "decision": decision,
                "reason": reason,
                "probability_13m": probability,
                "probability_15m": probability_15m,
                "probability_change": probability_change,
                "window_mean_probability": window_mean,
                "yes_breadth": breadth,
                "btc_probability": btc_probability,
            }
        )
    return decisions
