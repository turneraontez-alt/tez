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
INTERVAL_MARKS: dict[str, int] = {"15M": 900, "12M": 720, "11M": 660, "10M": 600, "7M": 420}


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
    # Edge gate (gate_c) for the NO side. DEFAULT: ENFORCED (no_edge_waive=False). History: it
    # was briefly WAIVED — the inverse-edge finding lifted TOTAL gross P&L on a smaller/earlier
    # sample. But a 13-agent ROI sweep + adversarial verification over the fuller ~42h capture
    # data (4774 captures) found ENFORCING the edge gate MAXIMIZES ROI (return per $ staked):
    # 10M/7M ROI 15.6%->~23%, win 72%->81%, holding in BOTH time-halves and every leave-one-
    # asset-out. Enforcing trims the low/negative-edge NOs (mostly the expensive 7M tail) so the
    # book leans 10M. NOTE: this REVERSES the earlier owner-enabled waive — gross $ and ROI can
    # point different ways and this default optimizes ROI (owner choice). Confidence floor + ask
    # band still apply. Set Q15_ULTOIM_V2_NO_EDGE_WAIVE=true to waive again (higher-gross, lower-ROI).
    no_edge_waive: bool = field(
        default_factory=lambda: _bool("Q15_ULTOIM_V2_NO_EDGE_WAIVE", False)
    )
    # Skip the 15M (900s) checkpoint. DEFAULT ON (owner-enabled). 15M is the weak, money-
    # losing bin for these NO binaries (accuracy ~coin-flip, negative P&L; -EV on all 7
    # assets cross-ledger), vs strong, priced-in 10M/7M; the frozen champion already
    # disables its own 15M alerts. v2 fires only at 10M/7M. Set false to restore 15M.
    skip_15m: bool = field(default_factory=lambda: _bool("Q15_ULTOIM_V2_SKIP_15M", True))
    # --- Net-improvement levers (workflow-verified; all reversible).
    # 7M ASK CAP — DEFAULT OFF (flipped from ON). At the 7M mark only, do not admit a NO whose
    # ask > cap_7m_ask_max. NON-STATIONARY SIGNAL: the cap was originally enabled when an EARLIER,
    # smaller delivered sample showed 7M asks >72c net-NEGATIVE (-57c over 21 bets). On the fuller
    # 189-fired sample the slice REVERSED — 7M ask>72c is +98c over 40 bets (82.5% win) while
    # ask<=72c is -174c over 27 bets (55.6% win), so the cap was vetoing the winners and keeping
    # the losers (the genuinely toxic [60,72) zone it does NOT touch). Default OFF avoids
    # suppressing the now-positive expensive-7M slice; the gain is THIN (the live-deliverable
    # (72,78] band the expensive-NO admit toggles is +65c over n=15). Set =true to restore the
    # cap if the >72c slice reverts to negative on a larger sample. Read-only; suppresses BOTH
    # delivery and research for the over-cap 7M NO; 10M and the YES side are untouched.
    cap_7m_ask: bool = field(default_factory=lambda: _bool("Q15_ULTOIM_V2_CAP_7M_ASK", False))
    cap_7m_ask_max: int = field(
        default_factory=lambda: int(_float("Q15_ULTOIM_V2_CAP_7M_ASK_MAX", 72.0))
    )
    # 11M / 12M marks — captured DEFAULT ON, but both RECORD-ONLY (never deliver/trade). 12M
    # was briefly promoted to live delivery on ~1 in-sample day, but the larger ~42h capture
    # replay showed the DELIVERED 12M slice runs net-NEGATIVE (57.6% win at a ~60c break-even,
    # -2c/bet) while 10M/7M carry the book (+10c / +21c per bet) — so the owner reverted 12M to
    # record-only (delivery is "back to 10M and 7M"). Both marks stay ENABLED so they keep
    # accruing gradeable data without adding a near-duplicate alert/trade; to re-promote either,
    # drop it from research_only_intervals (a deliberate, tested edit). 13M stays excluded
    # (fragile, both-halves 82%->65%). 10M is the proven anchor.
    enable_11m: bool = field(default_factory=lambda: _bool("Q15_ULTOIM_V2_ENABLE_11M", True))
    enable_12m: bool = field(default_factory=lambda: _bool("Q15_ULTOIM_V2_ENABLE_12M", True))
    # Promote the 12M EXPENSIVE-NO slice to LIVE delivery (default OFF -> 12M stays research-only).
    # DATA (real ledger, fired+research resolved): 12M ask>=65 NO is 90.9% / +17c/bet (n=11) and
    # ask>=72 is 100% / +25c/bet (n=9) — the SAME expensive-NO edge that carries 10M — while 12M
    # ask<65 is 33% / -17c/bet (n=12, the cheap near-strike loss zone). When ON, 12M is treated
    # like 10M/7M for delivery (same gate) EXCEPT a 12M NO with ask < floor_12m_ask is suppressed
    # from delivery (recorded research-only so it keeps accruing data). THIN sample (n=11) — owner-
    # enabled, reversible. NOTE: if you set Q15_EXEC_ALLOWED_INTERVALS, include 12M or the executor
    # will refuse it. Set Q15_ULTOIM_V2_DELIVER_12M=true.
    deliver_12m: bool = field(default_factory=lambda: _bool("Q15_ULTOIM_V2_DELIVER_12M", False))
    floor_12m_ask: float = field(default_factory=lambda: _float("Q15_ULTOIM_V2_FLOOR_12M_ASK", 65.0))
    # Marks that may RECORD but never DELIVER (and so never TRADE — the executor only fires on a
    # delivered row), even when enabled. 11M and 12M are both here: the delivered marks are 10M
    # and 7M only (15M is skipped via skip_15m). To promote a mark to live delivery, drop it.
    research_only_intervals: frozenset[str] = field(
        default_factory=lambda: frozenset({"11M", "12M"})
    )
    # DELIVERY breadth + selection. DEFAULT 1 (owner-enabled, SELECTIVE): the single best
    # by reward:risk per (interval, window). The owner trades 1-2 picks per 15-min window
    # manually, and now fires across more marks (12M/10M/7M), so top-1-per-mark keeps total
    # alert volume to ~1-2 distinct contracts/window (deduped per CONTRACT across marks) AND
    # concentrates capital on the single best reward:risk pick (settled record: best-per-window
    # ~+13c/bet vs ~+11c taking all, fewer fees, less correlated exposure). Set TOP_N>1 to widen
    # (raises correlated exposure + alert volume); =1 + BY_RR=false restores legacy best-by-edge.
    #  * deliver_by_reward_risk: pick by CHEAPEST ask (best payoff per $) not highest net-edge.
    # DEFAULT 2 (owner ROI-sweep choice): the top-2 by reward:risk per (mark, window), deduped per
    # contract across marks -> ~1-2 distinct alerts/window, matching the executor's 2-picks/window
    # cap (the "two @ 4%" sizing). The ROI sweep's verified winner (edge gate ON + exp_hi 78) ran at
    # top_n 2: ROI 23.4%, win 80.8%, n=73, +$11.2@$1, both time-halves positive. Set TOP_N=1 for a
    # strictly single pick/window; >2 widens correlated exposure + alert volume.
    deliver_top_n: int = field(
        default_factory=lambda: max(1, int(_float("Q15_ULTOIM_V2_DELIVER_TOP_N", 2.0)))
    )
    deliver_by_reward_risk: bool = field(
        default_factory=lambda: _bool("Q15_ULTOIM_V2_DELIVER_BY_RR", True)
    )
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
    # Expensive-NO ADMIT band. DEFAULT ON (owner-enabled). On non-15M intervals, admit a
    # NO candidate whose ask is in (ask_hi, expensive_no_ask_hi] EVEN when its stated net
    # edge is below min_edge: it extends the ask ceiling AND waives the edge gate for THIS
    # band only. The confidence floor and ask_lo still apply; 15M and the YES side are never
    # affected; and it NEVER places/modifies/cancels a real order. Basis: for NO the model's
    # stated edge is INVERSE (the best NO entries carry negative edge), and the expensive-NO
    # band wins ~84% across the pooled ledgers (n=893, v1+v95+v2), net-positive after the
    # (large) expensive losses. Capped at expensive_no_ask_hi because above ~85¢ the thin
    # max-profit (100−ask) lets the rare loss dominate (the (85,100] slice is net-negative),
    # so the ceiling is held at 85 and NOT raised to 100. Set Q15_ULTOIM_V2_EXPENSIVE_NO=
    # false to opt out (restores byte-identical plain-band behaviour).
    expensive_no_enabled: bool = field(
        default_factory=lambda: _bool("Q15_ULTOIM_V2_EXPENSIVE_NO", True)
    )
    # Ceiling TIGHTENED to 78 (was 85) by the ROI sweep: the (78,85] expensive-NO slice is thin-
    # payoff (100-ask) where the rare loss dominates ROI, so capping at 78 lifts return per $ staked
    # without hurting the robust win rate. Set Q15_ULTOIM_V2_EXPENSIVE_NO_ASK_HI=85 to restore the
    # wider band.
    expensive_no_ask_hi: float = field(
        default_factory=lambda: _float("Q15_ULTOIM_V2_EXPENSIVE_NO_ASK_HI", 78.0)
    )
    # Flow-against-NO research SCREEN threshold (record-only; surfaced by the recap via
    # ledger.flow_research_scoreboard). The champion's per-asset directional flow factor
    # (feature_values["flow"]) recorded as `champion_flow`: a NO bet placed against
    # strong buy-side flow (flow >= this) is the loss zone — the single fix that lifted
    # P&L ~50% out-of-sample on the v95 NO side (and survived a 3-way time-split +
    # leave-one-asset-out). The recap measures the candidate abstention (would-keep
    # flow<thr vs would-abstain flow>=thr) AND the v2-native regime_directional proxy.
    # Record-only — NEVER gates delivery. Default 0.6 from the OOS finding.
    flow_against_no_threshold: float = field(
        default_factory=lambda: _float("Q15_ULTOIM_V2_FLOW_AGAINST_NO", 0.6)
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
    # Fail-CLOSED on an UNKNOWN spot-staleness. DEFAULT OFF (fail-open, byte-identical): when the
    # champion exposes no staleness age, _spot_stale_age returns None and the freshness check is
    # skipped (a would-be fire still fires). With this ON, a None/missing staleness is treated as
    # STALE and the fire abstains — so the LIVE-MONEY path never places blind to feed freshness.
    # Gated + default-OFF because today the staleness column is unpopulated (always 0.0/None);
    # turn this on only once the feed reliably reports an age. Set Q15_ULTOIM_V2_STALE_FAIL_CLOSED.
    stale_fail_closed: bool = field(
        default_factory=lambda: _bool("Q15_ULTOIM_V2_STALE_FAIL_CLOSED", False)
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
    # earlier in the same window) if, at/after the watch-start (exit_watch_from_seconds,
    # default 8M), the champion's call has FLIPPED to the opposite side and the flip is
    # BOTH decisive (new-side prob >=
    # exit_min_flip_conf) AND sustained (held for >= exit_confirm_cycles consecutive
    # observations spanning >= exit_confirm_seconds) — so a short-lived spike never
    # triggers it. Records + grades every warning (learns whether the exit was right
    # and how much it would have recovered). Default ON when the overlay is enabled.
    exit_warnings_enabled: bool = field(
        default_factory=lambda: _bool("Q15_ULTOIM_V2_EXIT_WARNINGS", True)
    )
    # Watch for the flip from this many seconds remaining onward. Default 480 (8M), raised from
    # 420 (7M): live exit-warning data (n=51, 84% correct) showed 11 correct flips were CLIPPED by
    # the old 420s watch-start — the flip was already present but the watcher wasn't looking, so the
    # exit fired later and recovered less residual value (corr(time-left, recovered)=+0.31). Watching
    # from 8M fires those already-correct exits SOONER without touching the anti-spike gate
    # (confirm_cycles/seconds) or the decisiveness bar, so the false-alarm rate is unchanged.
    exit_watch_from_seconds: float = field(
        default_factory=lambda: _float("Q15_ULTOIM_V2_EXIT_WATCH_SECONDS", 480.0)
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
    # Record-only SETTLEMENT-STREAK signal (observational; default ON). At each prediction,
    # stamp the asset's consecutive same-outcome settled-window run (signed: +N = N YES in a
    # row, -N = N NO in a row, time-adjacent only) so the recap can GRADE whether a streak
    # predicts the next window's settlement. NEVER gates fire/size/alert — one indexed read
    # per recorded row. Set Q15_ULTOIM_V2_STREAK_SIGNAL=false to disable.
    streak_signal_enabled: bool = field(
        default_factory=lambda: _bool("Q15_ULTOIM_V2_STREAK_SIGNAL", True)
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
