"""Entry Economics — configuration (entry-econ-v1).

A SEPARATE, read-only system that answers ONE question per contract:

    "Even if the predicted side is correct, is this contract currently worth
     buying at an executable price?"

It never places, modifies, or cancels an order and never changes the frozen
production prediction or the live entry gate. Everything is env-driven and
additive; the pure economics computation is always available, while the
optional entry-performance ledger only writes when explicitly enabled.

This is intentionally NOT merged with the directional prediction model. It keeps
three concerns separate:

  * prediction quality  — which side is most likely to settle (upstream model)
  * trade quality       — is the available entry economically favourable (here)
  * entry timing        — ENTER now / WAIT for a better price / SKIP (here)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields

ENTRY_ECONOMICS_VERSION = "entry-econ-v1"


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _str(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class EntryEconConfig:
    # -- master switches --
    # The pure economics computation + panel are always available when this is on
    # (default on; it is read-only and side-effect free). The LEDGER (DB writes)
    # is a separate, default-OFF switch so importing/using the engine in tests or
    # the hot path never creates files unless explicitly opted in.
    enabled: bool = field(default_factory=lambda: _bool("Q15_ENTRY_ECON_ENABLED", True))
    ledger_enabled: bool = field(default_factory=lambda: _bool("Q15_ENTRY_ECON_LEDGER", False))
    # Inject the compact panel into the live Telegram checkpoint alert. Default OFF
    # so the FROZEN alert layout / suppression markers are never disturbed; the
    # panel is always surfaced in the snapshot + /api regardless of this flag.
    panel_in_alert: bool = field(default_factory=lambda: _bool("Q15_ENTRY_ECON_PANEL_IN_ALERT", False))

    # -- cost model (executable, conservative) --
    fee_rate: float = field(default_factory=lambda: _float("Q15_ENTRY_ECON_FEE_RATE", 0.07))
    # Kalshi rounds the per-order fee UP to the cent. Modelling a single marginal
    # contract with that ceil is the conservative choice for an entry gate.
    fee_ceil: bool = field(default_factory=lambda: _bool("Q15_ENTRY_ECON_FEE_CEIL", True))
    # Contracts to simulate when walking the book for slippage / partial fill.
    sim_contracts: float = field(default_factory=lambda: _float("Q15_ENTRY_ECON_SIM_CONTRACTS", 5.0))
    # Fallback slippage when the book depth/levels are unavailable (cents).
    slippage_fallback_cents: float = field(default_factory=lambda: _float("Q15_ENTRY_ECON_SLIPPAGE_FALLBACK_CENTS", 0.75))
    # Fraction of the spread charged as slippage when only top-of-book is known.
    slippage_spread_fraction: float = field(default_factory=lambda: _float("Q15_ENTRY_ECON_SLIPPAGE_FRAC", 0.50))
    latency_cost_cents: float = field(default_factory=lambda: _float("Q15_ENTRY_ECON_LATENCY_CENTS", 0.25))
    # Extra margin charged when the executable price is STALE (per stale-second,
    # capped) — a quote we cannot trust is more expensive to act on.
    stale_penalty_cents_per_s: float = field(default_factory=lambda: _float("Q15_ENTRY_ECON_STALE_PENALTY_CPS", 0.05))
    stale_penalty_cap_cents: float = field(default_factory=lambda: _float("Q15_ENTRY_ECON_STALE_PENALTY_CAP", 3.0))

    # -- required edge / safety margin (per checkpoint) --
    # The minimum conservative net edge (after all costs) a trade must clear.
    required_edge_15m: float = field(default_factory=lambda: _float("Q15_ENTRY_ECON_REQUIRED_EDGE_15M", 5.0))
    required_edge_10m: float = field(default_factory=lambda: _float("Q15_ENTRY_ECON_REQUIRED_EDGE_10M", 6.0))
    required_edge_7m: float = field(default_factory=lambda: _float("Q15_ENTRY_ECON_REQUIRED_EDGE_7M", 6.0))
    # Risk-aversion penalty per cent of binary-outcome stdev (for EV-after-uncertainty).
    risk_aversion_k: float = field(default_factory=lambda: _float("Q15_ENTRY_ECON_RISK_K", 0.04))
    # Width (cents) of the recommended entry band below the maximum entry.
    recommended_band_cents: float = field(default_factory=lambda: _float("Q15_ENTRY_ECON_BAND_CENTS", 3.0))

    # -- liquidity / timing gates --
    max_spread_cents: float = field(default_factory=lambda: _float("Q15_ENTRY_ECON_MAX_SPREAD_CENTS", 12.0))
    min_depth_contracts: float = field(default_factory=lambda: _float("Q15_ENTRY_ECON_MIN_DEPTH", 3.0))
    min_seconds_remaining: float = field(default_factory=lambda: _float("Q15_ENTRY_ECON_MIN_SECONDS", 20.0))
    max_quote_age_seconds: float = field(default_factory=lambda: _float("Q15_ENTRY_ECON_MAX_QUOTE_AGE", 30.0))
    # Above this data-age the executable price is treated as STALE (SKIP-eligible).
    stale_quote_age_seconds: float = field(default_factory=lambda: _float("Q15_ENTRY_ECON_STALE_QUOTE_AGE", 45.0))

    # -- side validity / risk invalidation --
    # The conservative probability must exceed this for the side to be "valid".
    min_conservative_side_prob: float = field(default_factory=lambda: _float("Q15_ENTRY_ECON_MIN_CONS_PROB", 0.50))
    # Normalised flip-risk score (0..1) at/above which the side is treated as too
    # unstable to ENTER (forces WAIT or, combined with no edge, SKIP).
    flip_invalidate_score: float = field(default_factory=lambda: _float("Q15_ENTRY_ECON_FLIP_INVALIDATE", 0.70))
    # When manipulation is suspected AGAINST the side, block ENTER (a manipulated
    # quote may offer a better entry shortly → WAIT, never a forced ENTER).
    manipulation_blocks_enter: bool = field(default_factory=lambda: _bool("Q15_ENTRY_ECON_MANIP_BLOCKS_ENTER", True))

    # -- conservative-probability shrink factors (floors keep shrink bounded) --
    # Each reliability multiplier is clamped to [factor_floor, 1.0]; the product
    # shrinks the side probability toward 0.5. A floor < 1 means even a perfectly
    # reliable factor can only restore confidence, never inflate it.
    factor_floor: float = field(default_factory=lambda: _float("Q15_ENTRY_ECON_FACTOR_FLOOR", 0.40))
    # Sample-size half-saturation: reliability = n / (n + k).
    sample_half_k: float = field(default_factory=lambda: _float("Q15_ENTRY_ECON_SAMPLE_HALF_K", 40.0))
    # Max directional probability penalty (prob units) from flip risk and manip.
    flip_penalty_max: float = field(default_factory=lambda: _float("Q15_ENTRY_ECON_FLIP_PENALTY_MAX", 0.12))
    manip_penalty: float = field(default_factory=lambda: _float("Q15_ENTRY_ECON_MANIP_PENALTY", 0.06))
    # Prior per-interval reliability (15M is historically the least reliable leg).
    interval_reliability_15m: float = field(default_factory=lambda: _float("Q15_ENTRY_ECON_INTERVAL_REL_15M", 0.85))
    interval_reliability_10m: float = field(default_factory=lambda: _float("Q15_ENTRY_ECON_INTERVAL_REL_10M", 0.95))
    interval_reliability_7m: float = field(default_factory=lambda: _float("Q15_ENTRY_ECON_INTERVAL_REL_7M", 1.0))

    # -- persistence --
    db_path: str = field(default_factory=lambda: _str("Q15_ENTRY_ECON_DB", "data/q15_entry_economics_v1.sqlite3"))
    model_version: str = field(default_factory=lambda: _str("Q15_ENTRY_ECON_MODEL_VERSION", ENTRY_ECONOMICS_VERSION))

    def required_edge_for(self, checkpoint: str) -> float:
        cp = str(checkpoint or "").upper()
        if "7M" in cp:
            return self.required_edge_7m
        if "10M" in cp:
            return self.required_edge_10m
        if "15M" in cp:
            return self.required_edge_15m
        return self.required_edge_10m

    def interval_reliability_for(self, checkpoint: str) -> float:
        cp = str(checkpoint or "").upper()
        if "7M" in cp:
            return self.interval_reliability_7m
        if "10M" in cp:
            return self.interval_reliability_10m
        if "15M" in cp:
            return self.interval_reliability_15m
        return self.interval_reliability_10m

    @classmethod
    def from_env(cls) -> "EntryEconConfig":
        return cls()

    def with_overrides(self, **kw) -> "EntryEconConfig":
        valid = {f.name for f in fields(self)}
        bad = set(kw) - valid
        if bad:
            raise ValueError(f"unknown config keys: {sorted(bad)}")
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        data.update(kw)
        return EntryEconConfig(**data)
