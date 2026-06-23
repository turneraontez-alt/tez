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
    # Skip the 15M (900s) checkpoint. DEFAULT OFF (byte-identical). The live record
    # shows 15M is the weak, money-losing bin for these NO binaries (accuracy ~coin-
    # flip and negative P&L at 15M, vs strong, priced-in 10M/7M); the frozen champion
    # already disables its own 15M alert delivery. When true, v2 only ever fires at
    # 10M/7M — observation/behaviour at those marks is unchanged.
    skip_15m: bool = field(default_factory=lambda: _bool("Q15_ULTOIM_V2_SKIP_15M", False))
    # --- 15M selective-entry research SCREEN thresholds (record-only; fifteen_min.py).
    # Scoped to 15M NO candidates ONLY; NEVER gates fire/delivery and never affects the
    # 10M/7M marks. The runner stamps, for every 15M NO row, whether this SELECTIVE gate
    # WOULD fire (s15_pass = LUKEWARM & CHEAP) plus two orthogonal tilt signals
    # (CAL_DRIFT / FRESH), so the rule accrues prospective, gradeable data. Defaults from
    # a five-agent study of the settled ledgers (single-session fit — must clear
    # validate.py's promotion bar on the cross-session record before it could ever gate).
    # All tunable; none of them is read by the live gate.
    s15_sel_max: float = field(
        default_factory=lambda: _float("Q15_ULTOIM_V2_S15_SEL_MAX", 0.55)
    )
    s15_ask_lo: float = field(default_factory=lambda: _float("Q15_ULTOIM_V2_S15_ASK_LO", 47.0))
    s15_ask_hi: float = field(default_factory=lambda: _float("Q15_ULTOIM_V2_S15_ASK_HI", 60.0))
    s15_cal_drift_max: float = field(
        default_factory=lambda: _float("Q15_ULTOIM_V2_S15_CAL_DRIFT_MAX", -0.03)
    )
    s15_fresh_min_seconds: float = field(
        default_factory=lambda: _float("Q15_ULTOIM_V2_S15_FRESH_MIN_SEC", 875.0)
    )
    # Distance-to-strike research SCREEN threshold (record-only; surfaced by the recap
    # via ledger.distance_research_scoreboard). distance_sigma is the ONE record-only
    # shadow feature shown to transport across ledgers (near-strike NO loses, far NO
    # wins). NO rows with |distance_sigma| < this are the NEAR-strike "pin" (the
    # would-abstain bucket the recap measures). Record-only — NEVER gates delivery.
    # Default from the cross-ledger finding |sigma|>=~0.15 -> ~0% loss.
    distance_pin_sigma: float = field(
        default_factory=lambda: _float("Q15_ULTOIM_V2_DISTANCE_PIN_SIGMA", 0.15)
    )
    # Distance GATE. DEFAULT ON (owner-enabled): ABSTAINS (suppresses paper delivery;
    # measurement via research_fired is UNCHANGED) on 15M near-strike NO candidates only —
    # those with |distance_sigma| < distance_pin_sigma (REUSES that field as the threshold;
    # no separate knob). 10M/7M and the YES side are unaffected, and the gate NEVER places,
    # modifies, or cancels a real order. Basis: the near-strike-NO loss replicates on the v95
    # champion ledger (n=186, −6.8¢ near vs +5.7¢ far); it is a payoff-asymmetry edge and is
    # NOT yet p<0.05 on the small delivered 15M sample (n=8). Set Q15_ULTOIM_V2_DISTANCE_GATE=
    # false to opt out (restores byte-identical no-gate behaviour).
    distance_gate_enabled: bool = field(
        default_factory=lambda: _bool("Q15_ULTOIM_V2_DISTANCE_GATE", True)
    )
    # Record the broad-market cross-asset flow factor (x_market_flow) on every
    # candidate row, for measure-first validation of a possible NO-side veto (high
    # market-wide YES pressure precedes NO losses, per an OOS time-split). DEFAULT
    # OFF (byte-identical; column stays NULL). Pure observation — NEVER read by the
    # gate, so v2's fire decision is unaffected whether on or off.
    record_xflow: bool = field(default_factory=lambda: _bool("Q15_ULTOIM_V2_RECORD_XFLOW", False))
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
    # Promotion bar — DISTINCT from the print-floor above. n=30 is enough to print
    # a number, but at a realistic ~80-85% hit-rate it is far too few to separate
    # the gate from the ~75% base NO rate (one-sided binomial). 50 is the minimum
    # where a sustained ~85% clears p<0.05 vs 0.75 with margin; the verdict also
    # requires the Wilson lower bound to exceed the empirical base rate.
    min_promote_n: int = field(
        default_factory=lambda: int(_float("Q15_ULTOIM_V2_MIN_PROMOTE_N", 50))
    )
    # Record the best YES-side candidate per (interval, window) as a RESEARCH-ONLY
    # row (never delivered; delivery stays NO-only). This is the data that lets the
    # system finally measure YES-prone windows — the precondition for promotion.
    # Default ON when the overlay is enabled; the whole overlay is still default-OFF.
    record_research_yes: bool = field(
        default_factory=lambda: _bool("Q15_ULTOIM_V2_RESEARCH_YES", True)
    )
    # Defensive-exit / flip warning. Fires (ONLY when a paper entry was suggested
    # earlier in the same window) if, at/after the 7M mark, the champion's call has
    # FLIPPED to the opposite side and the flip is BOTH decisive (new-side prob >=
    # exit_min_flip_conf) AND sustained (held for >= exit_confirm_cycles consecutive
    # observations spanning >= exit_confirm_seconds) — so a short-lived spike never
    # triggers it. Records + grades every warning (learns whether the exit was right
    # and how much it would have recovered). Default ON when the overlay is enabled.
    exit_warnings_enabled: bool = field(
        default_factory=lambda: _bool("Q15_ULTOIM_V2_EXIT_WARNINGS", True)
    )
    # Watch for the flip from this many seconds remaining onward (7M = 420s).
    exit_watch_from_seconds: float = field(
        default_factory=lambda: _float("Q15_ULTOIM_V2_EXIT_WATCH_SECONDS", 420.0)
    )
    # Anti-spike: the opposite-side call must hold this many consecutive observations
    # AND span at least this many seconds before a warning fires.
    exit_confirm_cycles: int = field(
        default_factory=lambda: int(_float("Q15_ULTOIM_V2_EXIT_CONFIRM_CYCLES", 3))
    )
    exit_confirm_seconds: float = field(
        default_factory=lambda: _float("Q15_ULTOIM_V2_EXIT_CONFIRM_SECONDS", 20.0)
    )
    # The flipped side must be at least this confident (P of the new side) — a
    # marginal ~0.51 flip is noise, not a mistake signal.
    exit_min_flip_conf: float = field(
        default_factory=lambda: _float("Q15_ULTOIM_V2_EXIT_MIN_FLIP_CONF", 0.55)
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
