"""Ultoim V2 — configuration.

Ultoim V2 is a SEPARATE, default-OFF, read-only PAPER entry-alert system. It runs
beside the live champion, reuses the champion's frozen per-asset analysis
(read-only), and — when explicitly enabled — emits a single paper "BEST ENTRY"
research card per qualifying contract per 15-minute window into its OWN database
and (optionally) its OWN Telegram channel. It NEVER trades, never modifies the
live system's predictions / records / Telegram channel, and never places, edits,
or cancels a real order.

DEFAULT-OFF is enforced: with ``Q15_ULTOIM_V2_ENABLED`` unset the app is
byte-identical (``get_runner()`` returns ``None`` and no DB / worker / channel is
touched). Set ``Q15_ULTOIM_V2_ENABLED=true`` to turn the research overlay on.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _str(name: str, default: str) -> str:
    return os.environ.get(name, default) or default


# The entry-research intervals and their fire mark (seconds before settlement).
# Matches the champion's three live checkpoints so the paper entry card lines up
# 1:1 with the live 15M / 10M / 7M cards it visually resembles.
INTERVAL_MARKS: dict[str, int] = {"15M": 900, "10M": 600, "7M": 420}


@dataclass(frozen=True)
class UltoimV2Config:
    # DEFAULT OFF (enforced). With the env unset the app is byte-identical: no DB,
    # no worker, no Telegram. Set Q15_ULTOIM_V2_ENABLED=true to enable the overlay.
    enabled: bool = field(default_factory=lambda: _bool("Q15_ULTOIM_V2_ENABLED", False))
    model_version: str = field(
        default_factory=lambda: _str("Q15_ULTOIM_V2_MODEL_VERSION", "ultoim-v2")
    )
    db_path: str = field(
        default_factory=lambda: _str("Q15_ULTOIM_V2_DB", "data/q15_ultoim_v2_v1.sqlite3")
    )
    # Same bot token as the live system (TELEGRAM_BOT_TOKEN) but a SEPARATE chat,
    # so paper entry cards land in their own channel and never mix with the live
    # feed. Empty (default) => muted: records research rows but never delivers.
    telegram_chat_id: str = field(
        default_factory=lambda: _str("Q15_ULTOIM_V2_TELEGRAM_CHAT_ID", "")
    )
    # Entry gate thresholds (all research-only; tunable). Confidence is the
    # selected-side probability; ask band keeps entries in a sensible cents range;
    # min_edge is the conservative net edge after costs to fire.
    min_confidence: float = field(
        default_factory=lambda: _float("Q15_ULTOIM_V2_MIN_CONF", 0.55)
    )
    ask_lo: float = field(default_factory=lambda: _float("Q15_ULTOIM_V2_ASK_LO", 50.0))
    ask_hi: float = field(default_factory=lambda: _float("Q15_ULTOIM_V2_ASK_HI", 72.0))
    min_edge_cents: float = field(
        default_factory=lambda: _float("Q15_ULTOIM_V2_MIN_EDGE", 2.0)
    )
    # NO-only by default: the live record favours the NO side for these binaries,
    # and the paper system starts conservative. Set false to admit YES entries.
    no_only: bool = field(default_factory=lambda: _bool("Q15_ULTOIM_V2_NO_ONLY", True))
    # A candidate fires when seconds_remaining first falls into [mark - band, mark].
    mark_band_seconds: float = field(
        default_factory=lambda: _float("Q15_ULTOIM_V2_MARK_BAND_SECONDS", 90.0)
    )
    reconcile_every_seconds: float = field(
        default_factory=lambda: _float("Q15_ULTOIM_V2_RECONCILE_EVERY_SECONDS", 30.0)
    )
    # Research recap cadence (30-min default). Throttled; never raises into the loop.
    recap_every_seconds: float = field(
        default_factory=lambda: _float("Q15_ULTOIM_V2_RECAP_SECONDS", 1800.0)
    )
    # Refuse to ALERT on a staler spot than this (seconds). A would-be fire on a
    # stale feed is recorded as an abstain (STALE_FEED) instead of delivered.
    max_spot_stale_seconds: float = field(
        default_factory=lambda: _float("Q15_ULTOIM_V2_MAX_STALE", 8.0)
    )
    # Suppress the headline accuracy % below this resolved-N (CI is too wide to
    # report a number honestly).
    min_scoreboard_n: int = field(
        default_factory=lambda: int(_float("Q15_ULTOIM_V2_MIN_SCOREBOARD_N", 30))
    )

    @classmethod
    def from_env(cls) -> "UltoimV2Config":
        return cls()


_enabled_cache: bool | None = None


def is_enabled() -> bool:
    global _enabled_cache
    if _enabled_cache is None:
        _enabled_cache = UltoimV2Config.from_env().enabled
    return _enabled_cache


def reset_enabled_cache() -> None:
    """Test hook: clear the cached enabled read."""
    global _enabled_cache
    _enabled_cache = None
