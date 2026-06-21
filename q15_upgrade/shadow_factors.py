"""Cross-asset / correlated-movement SHADOW factors (read-only, decision-time).

The monitor analyses each asset independently, yet every cycle it already holds a
fresh snapshot for ALL assets. Crypto is highly correlated — BTC leads the alts,
and broad-market strength/weakness moves every 15-minute binary — so an asset's
own momentum/flow is not the whole story. These factors expose that already-fetched
cross-asset context as extra signals for the shadow FACTOR LAB to grade.

They are recorded in the ledger's ISOLATED ``shadow_factor_json`` column, never in
``feature_json``, so they cannot perturb the frozen champion, the challenger, the
calibration, or the pattern overlay (all of which read only their own known feature
names). Nothing here changes a live prediction or entry — the lab decides, over
settled data, whether any of them has earned promotion.

Sign convention matches the champion features: a POSITIVE value leans YES (price
pressure that pushes the contract toward settling YES), so the lab can grade each
one's directional reliability the same way it grades ``momentum``/``wick``/etc.

No look-ahead: every input is a decision-time feature value already computed for
this cycle. The outcome is never an input.
"""
from __future__ import annotations

import math
from typing import Any, Mapping

LEADER = "BTC"  # the asset the rest of the market tends to follow


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        f = float(value)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _feature(analysis: Mapping[str, Any] | None, name: str) -> float | None:
    fv = (analysis or {}).get("feature_values") or {}
    return _num(fv.get(name))


def compute_market(analyses: Mapping[str, Mapping[str, Any]], *, leader: str = LEADER) -> dict[str, Any]:
    """Broad-market context for this cycle from every available analysis: the mean
    momentum and flow across assets, and the leader's (BTC's) momentum. Missing
    values are skipped — they shrink ``n`` rather than bias the mean. Returns a
    dict with ``None`` entries when nothing was available."""
    moms: list[float] = []
    flows: list[float] = []
    leader_mom: float | None = None
    for asset, analysis in (analyses or {}).items():
        m = _feature(analysis, "momentum")
        if m is not None:
            moms.append(m)
            if str(asset).upper() == str(leader).upper():
                leader_mom = m
        f = _feature(analysis, "flow")
        if f is not None:
            flows.append(f)
    return {
        "market_momentum": (sum(moms) / len(moms)) if moms else None,
        "market_flow": (sum(flows) / len(flows)) if flows else None,
        "leader_momentum": leader_mom,
        "n": len(moms),
    }


def for_asset(asset: str, analysis: Mapping[str, Any],
              market: Mapping[str, Any] | None) -> dict[str, float]:
    """YES-signed cross-asset factors for one asset (only the ones with data).

    * ``x_market_momentum`` / ``x_market_flow`` — broad-market direction (beta).
    * ``x_leader_momentum`` — BTC's momentum (the market lead).
    * ``x_rel_strength`` — this asset's momentum minus the market's (out/under-
      performance).
    * ``x_div_from_leader`` — this asset's momentum minus BTC's (divergence from
      the lead; 0 for BTC itself).
    """
    out: dict[str, float] = {}
    if not market:
        return out
    mm = _num(market.get("market_momentum"))
    mf = _num(market.get("market_flow"))
    lm = _num(market.get("leader_momentum"))
    am = _feature(analysis, "momentum")
    if mm is not None:
        out["x_market_momentum"] = mm
    if mf is not None:
        out["x_market_flow"] = mf
    if lm is not None:
        out["x_leader_momentum"] = lm
    if am is not None and mm is not None:
        out["x_rel_strength"] = am - mm
    if am is not None and lm is not None:
        out["x_div_from_leader"] = am - lm
    return out
