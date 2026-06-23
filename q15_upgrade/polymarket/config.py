"""Polymarket shadow — configuration (env-driven, default-OFF).

Mirrors the challenger config conventions: every switch is read from the
environment, everything defaults so production is byte-identical until you opt
in, and ``model_version`` doubles as the reset key for the visible comparison.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields


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


def _str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _assets(name: str, default: str) -> tuple[str, ...]:
    raw = os.environ.get(name, default) or ""
    return tuple(a.strip().upper() for a in raw.split(",") if a.strip())


@dataclass
class PolymarketConfig:
    # -- master switch (default OFF; inert package until enabled) --
    enabled: bool = field(default_factory=lambda: _bool("Q15_POLYMARKET_ENABLED", False))

    # Emit the compact "POLYMARKET SHADOW" Telegram card at each window boundary.
    report_enabled: bool = field(default_factory=lambda: _bool("Q15_POLYMARKET_REPORT", True))

    # Assets to attempt. Polymarket runs 15-minute crypto Up/Down for BTC, ETH, SOL,
    # XRP (confirmed via their 15m fee announcement + the live /crypto/15M board);
    # anything else (e.g. the Kalshi-only DOGE/BNB/HYPE) shows "NO MATCHING MARKET".
    assets: tuple[str, ...] = field(default_factory=lambda: _assets("Q15_POLYMARKET_ASSETS", "BTC,ETH,SOL,XRP"))

    # -- read-only HTTP endpoints (public; no auth needed for market data) --
    clob_base: str = field(default_factory=lambda: _str("Q15_POLYMARKET_CLOB_URL", "https://clob.polymarket.com"))
    gamma_base: str = field(default_factory=lambda: _str("Q15_POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com"))
    http_timeout: float = field(default_factory=lambda: _float("Q15_POLYMARKET_HTTP_TIMEOUT", 5.0))

    # -- model (reuses the champion's structural diffusion math) --
    # Cap on the drift term as a fraction of horizon sigma — mirrors
    # Q15_V95_MAX_DRIFT_FRACTION_OF_SIGMA so OUR up/down probability is computed
    # the SAME way the champion computes P(spot >= strike).
    max_drift_fraction_of_sigma: float = field(
        default_factory=lambda: _float("Q15_POLYMARKET_MAX_DRIFT_FRAC", 0.35))

    # How often (seconds) the reconcile pass may query Polymarket for settled
    # results of still-open predictions. Throttled so the 1s loop stays cheap.
    reconcile_every_seconds: float = field(
        default_factory=lambda: _float("Q15_POLYMARKET_RECONCILE_EVERY", 60.0))

    # -- persistence --
    db_path: str = field(default_factory=lambda: _str("Q15_POLYMARKET_DB", "data/q15_polymarket_shadow_v1.sqlite3"))
    # Reset key for the visible comparison: bumping it starts a fresh record while
    # every prior version's rows survive as an internal archive (never mixed in).
    model_version: str = field(default_factory=lambda: _str("Q15_POLYMARKET_MODEL_VERSION", "polymarket-v1"))
    # Optional explicit reset instant (epoch seconds, UTC). 0 -> stamped on first
    # record so the report can show "Comparison reset: <time>".
    reset_at: float = field(default_factory=lambda: _float("Q15_POLYMARKET_RESET_AT", 0.0))

    @classmethod
    def from_env(cls) -> "PolymarketConfig":
        return cls()

    def with_overrides(self, **kw) -> "PolymarketConfig":
        valid = {f.name for f in fields(self)}
        bad = set(kw) - valid
        if bad:
            raise ValueError(f"unknown config keys: {sorted(bad)}")
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        data.update(kw)
        return PolymarketConfig(**data)
