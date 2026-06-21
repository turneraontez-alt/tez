"""Two frozen Q15 predictions, EV-ranked focus, rejection diagnostics, and learning.

This module is intentionally read-only.  It never submits, modifies, or cancels
orders.  It only annotates snapshots, stores paper decision events, and sends
Telegram messages through the monitor's existing notifier.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import math
import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple

from .self_review import SelfReviewEngine

logger = logging.getLogger(__name__)

VERSION = "q15-two-window-v2"
POLICY_VERSION = os.environ.get("Q15_TWO_WINDOW_POLICY_VERSION", "q15-two-window-shadow-v2")
MODEL_VERSION = os.environ.get("Q15_TWO_WINDOW_MODEL_VERSION", "q15-two-window-chart-v2")
SCHEMA_VERSION = os.environ.get("Q15_TWO_WINDOW_SCHEMA_VERSION", "q15-two-window-features-v2")
DISCLAIMER = "Probabilistic paper estimate only; it can be wrong. Read-only monitor — no orders are placed."


def _esc(value: Any) -> str:
    """HTML-escape dynamic text before it is interpolated into a Telegram
    parse_mode=HTML message. Reason/diagnostic strings contain characters such
    as '<', '>', and '&' (e.g. "consensus 64% < 72%"); without escaping these
    Telegram rejects the whole message with HTTP 400 'can't parse entities' and
    the alert is never delivered. Literal markup tags are added separately.

    quote=True also escapes single/double quotes, so a value that lands inside a
    quoted HTML attribute (e.g. an href) cannot break out of it — robust even
    though current messages interpolate dynamic text only as tag content."""
    return html.escape("" if value is None else str(value), quote=True)


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return int(default)


def _num(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def _iso(epoch: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(epoch, timezone.utc) if epoch is not None else datetime.now(timezone.utc)
    return dt.isoformat()


def _parse_time(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _hash(*parts: Any) -> str:
    raw = "|".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _last_close(candles: Sequence[Mapping[str, Any]]) -> Optional[float]:
    for candle in reversed(list(candles or [])):
        value = _num(candle.get("close"))
        if value is not None and value > 0:
            return value
    return None


def _trend_pct(candles: Sequence[Mapping[str, Any]], lookback_seconds: float) -> float:
    rows: List[Tuple[float, float]] = []
    for candle in candles or []:
        ts = _num(candle.get("end_time"), _num(candle.get("start_time")))
        close = _num(candle.get("close"))
        if ts is not None and close is not None and close > 0:
            rows.append((ts, close))
    if len(rows) < 2:
        return 0.0
    rows.sort()
    cutoff = rows[-1][0] - lookback_seconds
    start = next((price for ts, price in rows if ts >= cutoff), rows[0][1])
    end = rows[-1][1]
    return ((end / start) - 1.0) * 100.0 if start > 0 else 0.0


def _rsi(candles: Sequence[Mapping[str, Any]], period: int = 14) -> Optional[float]:
    closes = [_num(c.get("close")) for c in candles or []]
    closes = [value for value in closes if value is not None]
    if len(closes) < period + 1:
        return None
    gains: List[float] = []
    losses: List[float] = []
    for left, right in zip(closes[-period - 1 : -1], closes[-period:]):
        change = right - left
        gains.append(max(0.0, change))
        losses.append(max(0.0, -change))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss <= 1e-12:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _fee_cents(price_cents: Optional[float], contracts: float = 1.0, fee_rate: float = 0.07) -> float:
    """Approximate the monitor's configured Kalshi general-fee model."""
    price = _num(price_cents)
    if price is None:
        return 0.0
    probability = _clamp(price / 100.0, 0.0, 1.0)
    return math.ceil(max(0.0, fee_rate) * max(0.0, contracts) * probability * (1.0 - probability) * 100.0 - 1e-12)


def _side_prices(snapshot: Mapping[str, Any], side: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    yes_bid = _num(snapshot.get("yes_bid"))
    yes_ask = _num(snapshot.get("yes_ask"))
    no_bid = _num(snapshot.get("no_bid"))
    no_ask = _num(snapshot.get("no_ask"))
    if side == "YES":
        bid, ask = yes_bid, yes_ask
    else:
        bid = no_bid if no_bid is not None else (100.0 - yes_ask if yes_ask is not None else None)
        ask = no_ask if no_ask is not None else (100.0 - yes_bid if yes_bid is not None else None)
    spread = None if bid is None or ask is None else max(0.0, ask - bid)
    return bid, ask, spread


def _side_depth(snapshot: Mapping[str, Any], side: str) -> Optional[float]:
    keys = ("yes_ask_qty", "depth_3c_yes") if side == "YES" else ("no_ask_qty", "depth_3c_no")
    for key in keys:
        value = _num(snapshot.get(key))
        if value is not None:
            return value
    return None


def adaptive_confirmation_ok(
    *,
    snapshot: Optional[Mapping[str, Any]] = None,
    confirmations: Any = None,
    estimate: Any = None,
    edge: Any = None,
    required_edge: Any = None,
    settings: Any = None,
) -> bool:
    """Return True for 3 moderate groups or 2 exceptionally strong groups.

    The two-group path deliberately demands extra edge and higher model-input
    quality.  It relaxes only the confirmation group/score blockers; spread,
    data freshness, depth, win probability, follow-through, and opposition
    vetoes remain independent gates.
    """
    snap = snapshot or {}
    groups = None
    score = None
    if confirmations is not None:
        groups = _num(getattr(confirmations, "active_group_count", None))
        score = _num(getattr(confirmations, "total_score", None))
        if isinstance(confirmations, Mapping):
            groups = _num(confirmations.get("active_group_count"), groups)
            score = _num(confirmations.get("total_score"), score)
    groups = int(groups if groups is not None else (_num(snap.get("confirmation_group_count"), _num(snap.get("confirmation_count"), 0.0)) or 0))
    score = float(score if score is not None else (_num(snap.get("confirmation_score"), 0.0) or 0.0))
    quality = _num(getattr(estimate, "confidence", None)) if estimate is not None else None
    quality = float(quality if quality is not None else (_num(snap.get("model_confidence"), 0.0) or 0.0))
    modeled_edge = _num(edge, _num(snap.get("estimated_edge"), -999.0))
    required = _num(required_edge, _num(snap.get("required_edge"), 5.0)) or 5.0

    standard_groups = int(getattr(settings, "adaptive_min_groups", _int("Q15_ADAPTIVE_MIN_GROUPS", 3)))
    standard_score = float(getattr(settings, "adaptive_min_score", _float("Q15_ADAPTIVE_MIN_SCORE", 55.0)))
    strong_groups = int(getattr(settings, "adaptive_strong_groups", _int("Q15_ADAPTIVE_STRONG_GROUPS", 2)))
    strong_score = float(getattr(settings, "adaptive_strong_score", _float("Q15_ADAPTIVE_STRONG_SCORE", 75.0)))
    strong_quality = float(getattr(settings, "adaptive_strong_quality", _float("Q15_ADAPTIVE_STRONG_QUALITY", 0.70)))
    extra_edge = float(getattr(settings, "adaptive_extra_edge_cents", _float("Q15_ADAPTIVE_EXTRA_EDGE_CENTS", 2.0)))

    moderate_path = groups >= standard_groups and score >= standard_score
    exceptional_path = (
        groups >= strong_groups
        and score >= strong_score
        and quality >= strong_quality
        and modeled_edge is not None
        and modeled_edge >= required + extra_edge
    )
    return bool(moderate_path or exceptional_path)


@dataclass(frozen=True)
class FocusSettings:
    top_n: int = _int("Q15_FOCUS_TOP_N", 3)
    checkpoint_15_seconds: int = _int("Q15_15M_PREDICTION_AT_SECONDS", 885)
    checkpoint_15_latest_seconds: int = _int("Q15_15M_PREDICTION_LATEST_SECONDS", 720)
    checkpoint_10_seconds: int = _int("Q15_10M_PREDICTION_AT_SECONDS", 600)
    checkpoint_10_latest_seconds: int = _int("Q15_10M_PREDICTION_LATEST_SECONDS", 480)
    vote_lookback_seconds: float = _float("Q15_SIDE_VOTE_LOOKBACK_SECONDS", 45.0)
    min_vote_samples: int = _int("Q15_SIDE_MIN_VOTE_SAMPLES", 5)
    initial_consensus: float = _float("Q15_SIDE_INITIAL_CONSENSUS", 0.72)
    switch_consensus: float = _float("Q15_SIDE_SWITCH_CONSENSUS", 0.80)
    initial_side_probability: float = _float("Q15_SIDE_INITIAL_PROBABILITY", 0.58)
    switch_side_probability: float = _float("Q15_SIDE_SWITCH_PROBABILITY", 0.60)
    switch_min_groups: int = _int("Q15_SIDE_SWITCH_MIN_GROUPS", 3)
    switch_min_score: float = _float("Q15_SIDE_SWITCH_MIN_SCORE", 65.0)
    switch_min_consecutive_votes: int = _int("Q15_SIDE_SWITCH_MIN_CONSECUTIVE", 5)
    neutral_low: float = _float("Q15_UNCERTAIN_LOW", 0.45)
    neutral_high: float = _float("Q15_UNCERTAIN_HIGH", 0.55)
    min_win_probability: float = _float("Q15_FOCUS_MIN_WIN_PROBABILITY", 0.55)
    max_spread_cents: float = _float("Q15_FOCUS_MAX_SPREAD_CENTS", 3.0)
    min_entry_seconds: int = _int("Q15_FOCUS_MIN_ENTRY_SECONDS", 60)
    max_data_age_seconds: float = _float("Q15_FOCUS_MAX_DATA_AGE_SECONDS", 5.0)
    min_depth: float = _float("Q15_FOCUS_MIN_EXECUTABLE_DEPTH", 10.0)
    adaptive_min_groups: int = _int("Q15_ADAPTIVE_MIN_GROUPS", 3)
    adaptive_min_score: float = _float("Q15_ADAPTIVE_MIN_SCORE", 55.0)
    adaptive_strong_groups: int = _int("Q15_ADAPTIVE_STRONG_GROUPS", 2)
    adaptive_strong_score: float = _float("Q15_ADAPTIVE_STRONG_SCORE", 75.0)
    adaptive_strong_quality: float = _float("Q15_ADAPTIVE_STRONG_QUALITY", 0.70)
    adaptive_extra_edge_cents: float = _float("Q15_ADAPTIVE_EXTRA_EDGE_CENTS", 2.0)
    safety_buffer_cents: float = _float("Q15_PRICE_SAFETY_BUFFER_CENTS", 1.5)
    uncertainty_buffer_cents: float = _float("Q15_SELL_UNCERTAINTY_BUFFER_CENTS", 3.0)
    slippage_fraction_of_spread: float = _float("Q15_SLIPPAGE_SPREAD_FRACTION", 0.25)
    telegram_enabled: bool = _bool("Q15_FOCUS_TELEGRAM", True)
    # Owner consolidated Telegram delivery onto the unified V9.5 CHECK panel /
    # hourly report / follow-up. The two-window checkpoint alerts used a separate
    # older plain-text format, so their Telegram delivery is OFF by default (the
    # two-window ranking/learning/dashboard still run). Re-enable with
    # Q15_FOCUS_CHECKPOINT_ALERTS=true. telegram_enabled stays the master switch.
    checkpoint_alerts_enabled: bool = _bool("Q15_FOCUS_CHECKPOINT_ALERTS", False)
    alert_15_at_seconds: int = _int("Q15_15M_TELEGRAM_AT_SECONDS", 884)
    alert_10_at_seconds: int = _int("Q15_10M_TELEGRAM_AT_SECONDS", 599)
    alert_7_at_seconds: int = _int("Q15_7M_TELEGRAM_AT_SECONDS", 419)
    learning_shadow: bool = _bool("Q15_LEARNING_V2_SHADOW", True)
    apply_learning_live: bool = _bool("Q15_APPLY_CALIBRATION_TO_LIVE", False)
    learning_min_samples: int = _int("Q15_TWO_WINDOW_LEARNING_MIN_SAMPLES", 20)
    max_learned_adjustment_pp: float = _float("Q15_TWO_WINDOW_MAX_ADJUSTMENT_PP", 5.0)
    reconcile_interval_seconds: float = _float("Q15_TWO_WINDOW_RECONCILE_SECONDS", 30.0)
    max_reconcile_calls: int = _int("Q15_TWO_WINDOW_MAX_RECONCILE_CALLS", 8)
    management_hold_probability: float = _float("Q15_HOLD_TO_CLOSE_MIN_PROBABILITY", 0.70)
    management_hold_confidence: float = _float("Q15_HOLD_TO_CLOSE_MIN_CONFIDENCE", 0.80)
    late_strong_shadow_enabled: bool = _bool("Q15_LATE_STRONG_SHADOW", True)
    late_strong_min_probability: float = _float("Q15_LATE_STRONG_MIN_PROBABILITY", 0.72)
    late_strong_min_consensus: float = _float("Q15_LATE_STRONG_MIN_CONSENSUS", 0.85)
    late_strong_min_groups: int = _int("Q15_LATE_STRONG_MIN_GROUPS", 3)
    late_strong_min_score: float = _float("Q15_LATE_STRONG_MIN_SCORE", 70.0)
    late_strong_min_net_edge_cents: float = _float("Q15_LATE_STRONG_MIN_NET_EDGE_CENTS", 7.0)
    late_strong_max_spread_cents: float = _float("Q15_LATE_STRONG_MAX_SPREAD_CENTS", 2.0)
    regime_thresholds_shadow: bool = _bool("Q15_REGIME_THRESHOLDS_SHADOW", True)
    regime_high_consensus_add: float = _float("Q15_REGIME_HIGH_CONSENSUS_ADD", 0.05)
    regime_high_samples_add: int = _int("Q15_REGIME_HIGH_SAMPLES_ADD", 2)
    regime_high_edge_add_cents: float = _float("Q15_REGIME_HIGH_EDGE_ADD_CENTS", 2.0)
    regime_high_max_spread_cents: float = _float("Q15_REGIME_HIGH_MAX_SPREAD_CENTS", 2.0)
    regime_stable_consensus_subtract: float = _float("Q15_REGIME_STABLE_CONSENSUS_SUBTRACT", 0.03)
    self_review_enabled: bool = _bool("Q15_SELF_REVIEW_ENABLED", True)
    self_review_auto_adapt: bool = _bool("Q15_SELF_REVIEW_AUTO_ADAPT", True)
    self_review_adapt_min_samples: int = _int("Q15_SELF_REVIEW_ADAPT_MIN_SAMPLES", 8)
    self_review_adapt_loss_rate: float = _float("Q15_SELF_REVIEW_ADAPT_LOSS_RATE", 0.55)
    self_review_adapt_eval_window: int = _int("Q15_SELF_REVIEW_ADAPT_EVAL_WINDOW", 8)
    self_review_adapt_decay_window: int = _int("Q15_SELF_REVIEW_ADAPT_DECAY_WINDOW", 30)
    # Dip alert also used the older plain-text format -> OFF by default as part of
    # consolidating onto the unified panel. Re-enable with Q15_DIP_ALERT_ENABLED=true.
    dip_alert_enabled: bool = _bool("Q15_DIP_ALERT_ENABLED", False)
    dip_alert_min_edge_cents: float = _float("Q15_DIP_ALERT_MIN_EDGE_CENTS", 6.0)
    dip_alert_rearm_edge_cents: float = _float("Q15_DIP_ALERT_REARM_EDGE_CENTS", 2.0)
    dip_alert_min_win_probability: float = _float("Q15_DIP_ALERT_MIN_WIN_PROBABILITY", 0.55)
    dip_alert_cooldown_seconds: float = _float("Q15_DIP_ALERT_COOLDOWN_SECONDS", 90.0)
    dip_alert_max_per_cycle: int = _int("Q15_DIP_ALERT_MAX_PER_CYCLE", 3)
    dip_alert_min_seconds_remaining: int = _int("Q15_DIP_ALERT_MIN_SECONDS_REMAINING", 60)
    # When a claimed alert fails to deliver (Telegram 429/400/network blip,
    # notifier.send() returns falsy or raises), release the local claim and do
    # NOT advance state so the alert is retried on a later cycle instead of being
    # silently dropped. The shared claim store's ledger is permanent by design,
    # so a retry only truly re-fires in the common single-process deployment;
    # against a shared store the second attempt is treated as already-delivered.
    alert_retry_on_send_failure: bool = _bool("Q15_V95_ALERT_RETRY_ON_SEND_FAILURE", True)
    # Read-only NO-conviction score for the 10m checkpoint. Derived from settled
    # history: a 10m NO has been ~97% accurate when it clears a high-consensus /
    # high-side-probability / high-decisiveness bar with macro alignment, and is
    # a coin-flip otherwise. Observational only — never gates entries.
    no_confidence_enabled: bool = _bool("Q15_NO_CONFIDENCE_ENABLED", True)
    no_confidence_min_consensus: float = _float("Q15_NO_CONFIDENCE_MIN_CONSENSUS", 0.97)
    no_confidence_min_side_probability: float = _float("Q15_NO_CONFIDENCE_MIN_SIDE_PROB", 0.70)
    no_confidence_min_decisiveness: float = _float("Q15_NO_CONFIDENCE_MIN_DECISIVENESS", 0.40)
    no_confidence_require_alignment: bool = _bool("Q15_NO_CONFIDENCE_REQUIRE_ALIGNMENT", True)
    # Read-only YES-conviction score for the 10m checkpoint. Derived from settled
    # history: a 10m YES is ~90% accurate when all three macro signals
    # (market/distance/model) are aligned and it clears modest numeric floors, but
    # only ~62% otherwise — distance alignment is the dominant differentiator
    # (~91% vs ~50%). Numeric metrics barely separate right from wrong for YES, so
    # the floors are looser than the NO score. Observational only — never gates.
    yes_confidence_enabled: bool = _bool("Q15_YES_CONFIDENCE_ENABLED", True)
    yes_confidence_min_consensus: float = _float("Q15_YES_CONFIDENCE_MIN_CONSENSUS", 0.95)
    yes_confidence_min_side_probability: float = _float("Q15_YES_CONFIDENCE_MIN_SIDE_PROB", 0.65)
    yes_confidence_min_decisiveness: float = _float("Q15_YES_CONFIDENCE_MIN_DECISIVENESS", 0.40)
    yes_confidence_require_alignment: bool = _bool("Q15_YES_CONFIDENCE_REQUIRE_ALIGNMENT", True)

    def public_dict(self) -> dict:
        return asdict(self)


class TwoWindowFocusManager:
    """Freeze two predictions, rank top three, and gate deep evaluation."""

    def __init__(self, store=None, notifier=None, config=None, client=None, settings: Optional[FocusSettings] = None):
        self.store = store
        self.notifier = notifier
        self.config = config
        self.client = client
        self.settings = settings or FocusSettings()
        self._lock = threading.RLock()
        self._cycles: Dict[str, dict] = {}
        self._rankings: Dict[str, dict] = {}
        self._top_by_close: Dict[str, set] = defaultdict(set)
        self._local_claims: set = set()
        self._dip_state: Dict[str, dict] = {}
        self._last_reconcile = 0.0
        self._db_ready = False
        self._migration_attempted = False
        self._blocker_counts: Dict[str, int] = defaultdict(int)
        self._last_error: Optional[str] = None
        self._breaker_active = False
        self._empty_close_key_warned_at = 0.0
        self._migrate()
        # Final 10m prediction self-review (read-only post-mortems + bounded
        # auto-adaptation of decision thresholds). Hydrating the engine re-applies
        # any persisted active adaptations onto self.settings.
        self.self_review = SelfReviewEngine(self.store, self.settings)

    # ----------------------------- public loop integration -----------------
    def pre_enrich(self, snapshots: Mapping[str, dict], now: float) -> Dict[str, dict]:
        """Attach an entry gate before Q15Runtime can confirm a new position."""
        output: Dict[str, dict] = {}
        with self._lock:
            for asset, raw in snapshots.items():
                if not isinstance(raw, dict):
                    continue
                snap = dict(raw)
                if snap.get("market_state") != "live":
                    output[asset] = snap
                    continue
                close_key = str(snap.get("close_time") or "")
                top_assets = self._top_by_close.get(close_key, set())
                cycle = self._cycles.get(str(snap.get("ticker") or ""), {})
                final_side = cycle.get("final_side")
                active = snap.get("position_state") == "ACTIVE" or snap.get("decision_state") == "ENTRY CONFIRMED"
                allowed = bool(active or (asset in top_assets and final_side in {"YES", "NO"}))
                snap["q15_new_entry_allowed"] = allowed
                snap["q15_expected_side"] = final_side
                if not allowed:
                    snap["q15_entry_gate_reason"] = "awaiting_two_matching_predictions_or_top3"
                output[asset] = snap
        return output

    def update(self, snapshots: Mapping[str, dict], now: float, ws_health: Optional[dict] = None) -> Dict[str, dict]:
        output: Dict[str, dict] = {asset: dict(snap) for asset, snap in snapshots.items() if isinstance(snap, dict)}
        with self._lock:
            live = {asset: snap for asset, snap in output.items() if snap.get("market_state") == "live" and snap.get("ticker")}
            reported = [snap.get("learning_circuit_breakers") for snap in live.values() if snap.get("learning_circuit_breakers") is not None]
            if reported:
                self._breaker_active = any(bool(b) for b in reported)
            for asset, snap in live.items():
                cycle = self._cycle(asset, snap)
                self._observe(cycle, snap, now)
                self._capture_due(cycle, snap, now)

            by_close: Dict[str, Dict[str, dict]] = defaultdict(dict)
            for asset, snap in live.items():
                by_close[str(snap.get("close_time") or "")][asset] = snap
            for close_key, group in by_close.items():
                if not close_key:
                    # A live market without a close_time cannot be identified or
                    # deduplicated (its claim key would collapse to an empty
                    # suffix, e.g. "q15-two-window:7m:"). Skip until close_time is
                    # populated rather than emit a degenerate shared claim.
                    continue
                self._rebuild_rankings(close_key, group)
                self._maybe_notify(close_key, group, now)

            for asset, snap in output.items():
                if snap.get("market_state") != "live" or not snap.get("ticker"):
                    continue
                cycle = self._cycles.get(str(snap.get("ticker"))) or {}
                p15 = cycle.get("predictions", {}).get("15m")
                p10 = cycle.get("predictions", {}).get("10m")
                final_side, agreement = self._final(p15, p10)
                decision_state, late_shadow = self._decision_state(p15, p10, snap)
                close_key = str(snap.get("close_time") or "")
                ranking = self._rankings.get(close_key, {})
                rank = next((row["rank"] for row in ranking.get("final", []) if row.get("asset") == asset), None)
                in_top = asset in self._top_by_close.get(close_key, set())
                plan = self._trade_plan(snap, final_side, final_ready=(agreement == "SAME"), provisional=False)
                if final_side not in {"YES", "NO"}:
                    plan = None
                active = snap.get("position_state") == "ACTIVE" or snap.get("decision_state") == "ENTRY CONFIRMED"
                entry_allowed = bool(active or (in_top and agreement == "SAME" and final_side in {"YES", "NO"}))
                snap.update({
                    "q15_prediction_15m": p15,
                    "q15_prediction_10m": p10,
                    "q15_no_confidence_10m": self._no_confidence(p10),
                    "q15_yes_confidence_10m": self._yes_confidence(p10),
                    "q15_prediction_agreement": agreement,
                    "q15_decision_state": decision_state,
                    "q15_late_strong_shadow": late_shadow,
                    "q15_final_result": final_side or "UNCERTAIN",
                    "q15_focus_rank": rank,
                    "q15_in_top3": in_top,
                    "q15_new_entry_allowed": entry_allowed,
                    "q15_expected_side": final_side,
                    "q15_trade_plan": plan,
                    "q15_management_plan": (plan or {}).get("management_mode") if plan else "NO_TRADE",
                    "q15_regime_threshold_shadow": self._regime_shadow_recommendation(snap),
                    "q15_focus_version": VERSION,
                })
        return output

    def deep_evaluation_snapshots(self, snapshots: Mapping[str, dict], signal_engine=None, scalp_engine=None) -> Dict[str, dict]:
        active_assets = set()
        try:
            for row in signal_engine.get_active_signals() if signal_engine is not None else []:
                if row.get("state") in {"ENTRY CONFIRMED", "REVERSAL WARNING", "ENTRY INVALIDATED"}:
                    active_assets.add(row.get("asset"))
        except Exception:
            pass
        try:
            for row in scalp_engine.open_positions() if scalp_engine is not None else []:
                active_assets.add(row.get("asset"))
        except Exception:
            pass

        selected: Dict[str, dict] = {}
        with self._lock:
            for asset, raw in snapshots.items():
                if not isinstance(raw, dict) or raw.get("market_state") != "live":
                    continue
                close_key = str(raw.get("close_time") or "")
                in_top = asset in self._top_by_close.get(close_key, set())
                if not in_top and asset not in active_assets and raw.get("position_state") != "ACTIVE":
                    continue
                snap = dict(raw)
                expected = snap.get("q15_expected_side")
                current = snap.get("entry_side")
                is_active = asset in active_assets or snap.get("position_state") == "ACTIVE"
                if not is_active and expected in {"YES", "NO"} and current in {"YES", "NO"} and current != expected:
                    snap["decision_state"] = "WATCH"
                    snap["entry_status"] = "WATCH"
                    snap["is_confirmed"] = False
                    blockers = list(snap.get("entry_blockers") or [])
                    if "two_prediction_side_veto" not in blockers:
                        blockers.append("two_prediction_side_veto")
                    snap["entry_blockers"] = blockers
                selected[asset] = snap
        return selected

    def reconcile_settlements(self, now: float) -> None:
        if self.client is None or now - self._last_reconcile < self.settings.reconcile_interval_seconds:
            return
        self._last_reconcile = now
        pending: List[dict] = []
        with self._lock:
            for cycle in self._cycles.values():
                if cycle.get("graded"):
                    continue
                close_ts = _parse_time(cycle.get("close_time"))
                if close_ts is not None and close_ts <= now + 2:
                    pending.append(cycle)
        calls = 0
        # Wall-clock budget so a batch of slow Kalshi lookups for
        # recently-closed-but-unsettled markets can't monopolise the refresh
        # loop; ungraded cycles are retried next pass.
        budget = _float("Q15_RECONCILE_BUDGET_SECONDS", 4.0)
        started = time.monotonic()
        for cycle in pending:
            if calls >= self.settings.max_reconcile_calls or time.monotonic() - started > budget:
                break
            ticker = cycle.get("ticker")
            try:
                market = self.client.get_market(ticker)
                calls += 1
            except Exception as exc:
                self._last_error = f"settlement fetch {ticker}: {exc}"
                continue
            result = str((market or {}).get("result") or "").upper()
            status = str((market or {}).get("status") or "").lower()
            if result not in {"YES", "NO"} and status not in {"settled", "finalized", "determined", "closed"}:
                continue
            if result in {"YES", "NO"}:
                self._grade_cycle(cycle, result, now)

    def focus_status(self) -> dict:
        with self._lock:
            return {
                "version": VERSION,
                "read_only": True,
                "shadow_learning": self.settings.learning_shadow,
                "rankings": self._rankings,
                "top_assets_by_close": {key: sorted(value) for key, value in self._top_by_close.items()},
                "blocker_counts": dict(sorted(self._blocker_counts.items(), key=lambda item: item[1], reverse=True)),
                "cycles": [self._public_cycle(cycle) for cycle in self._cycles.values()],
                "database_ready": self._db_ready,
                "last_error": self._last_error,
                "settings": self.settings.public_dict(),
                "warning": DISCLAIMER,
            }

    def predictions_status(self) -> list:
        with self._lock:
            rows = [self._public_cycle(cycle) for cycle in self._cycles.values()]
            rows.sort(key=lambda row: (row.get("close_time") or "", row.get("asset") or ""), reverse=True)
            return rows

    def self_review_status(self, limit: int = 50) -> dict:
        if getattr(self, "self_review", None) is None:
            return {"enabled": False, "read_only": True}
        status = self.self_review.status(limit=limit)
        status["circuit_breaker_active"] = self._breaker_active
        return status

    def learning_summary(self) -> dict:
        counts = {}
        if self._db_ready:
            try:
                rows = self._query(
                    "SELECT COUNT(*) AS predictions, COUNT(*) FILTER (WHERE outcome IS NOT NULL) AS graded "
                    "FROM q15_prediction_checkpoints"
                )
                counts = rows[0] if rows else {}
            except Exception:
                counts = {}
        return {
            "version": VERSION,
            "shadow": self.settings.learning_shadow or not self.settings.apply_learning_live,
            "max_adjustment_percentage_points": self.settings.max_learned_adjustment_pp,
            "counts": counts,
            "blocker_counts": dict(self._blocker_counts),
            "warning": "Learned chart associations are capped and are not guarantees.",
        }

    def health(self) -> dict:
        return {
            "version": VERSION,
            "database_ready": self._db_ready,
            "cycles_in_memory": len(self._cycles),
            "focused_assets": sorted({asset for assets in self._top_by_close.values() for asset in assets}),
            "telegram_enabled": self.settings.telegram_enabled,
            "read_only": True,
            "last_error": self._last_error,
        }

    # ----------------------------- state/prediction -------------------------
    def _cycle(self, asset: str, snapshot: Mapping[str, Any]) -> dict:
        ticker = str(snapshot.get("ticker"))
        cycle = self._cycles.get(ticker)
        if cycle is None:
            cycle = {
                "ticker": ticker,
                "asset": asset,
                "close_time": snapshot.get("close_time"),
                "votes": deque(maxlen=180),
                "predictions": {},
                "final_side": None,
                "agreement": "PENDING",
                "decision_state": "PENDING",
                "late_strong_shadow": None,
                "graded": False,
                "actual_result": None,
            }
            self._cycles[ticker] = cycle
            self._hydrate_cycle(cycle)
        return cycle

    def _observe(self, cycle: dict, snapshot: Mapping[str, Any], now: float) -> None:
        raw_p, features = self._raw_yes_probability(snapshot)
        candidate = "YES" if raw_p >= self.settings.neutral_high else "NO" if raw_p <= self.settings.neutral_low else "UNCERTAIN"
        tags = self._feature_tags(snapshot, features, candidate)
        learned = self._learned_adjustment(cycle.get("asset"), candidate, tags)
        adjusted = raw_p
        if self.settings.apply_learning_live and candidate in {"YES", "NO"}:
            adjusted = raw_p + learned if candidate == "YES" else raw_p - learned
        adjusted = _clamp(adjusted, 0.01, 0.99)
        side = "YES" if adjusted >= self.settings.neutral_high else "NO" if adjusted <= self.settings.neutral_low else "UNCERTAIN"
        availability = features.get("availability", 0.0)
        agreement = features.get("component_agreement", 0.0)
        confidence = _clamp(0.35 * availability + 0.45 * agreement + 0.20 * abs(adjusted - 0.5) * 2.0, 0.0, 1.0)
        cycle["votes"].append({
            "ts": now,
            "raw_yes_probability": round(raw_p, 6),
            "yes_probability": round(adjusted, 6),
            "side": side,
            "confidence": round(confidence, 4),
            "features": features,
            "feature_tags": tags,
            "learned_adjustment": round(learned, 6),
        })

    def _raw_yes_probability(self, snapshot: Mapping[str, Any]) -> Tuple[float, dict]:
        # V9_CANONICAL_FOCUS_PROBABILITY
        _v9_probability = snapshot.get("v9_uncalibrated_p_yes") if isinstance(snapshot, dict) else None
        if _v9_probability is not None:
            try:
                _v9_probability = float(_v9_probability)
                if _v9_probability > 1.0:
                    _v9_probability /= 100.0
                _v9_probability = max(0.001, min(0.999, _v9_probability))
            except (TypeError, ValueError):
                _v9_probability = None
        # Freshness gate: the canonical v9 probability overrides the live
        # component blend below (see the return statements), so a value carried on
        # a stale snapshot — e.g. after a failed refresh or a restart that reuses
        # the prior dict — must not resurrect an old cycle's lean. When the
        # snapshot is older than the focus data-age budget, drop it and let the
        # freshly-computed components stand. No-op for fresh snapshots.
        if _v9_probability is not None and isinstance(snapshot, dict):
            data_age = _num(snapshot.get("data_age_seconds"), 0.0) or 0.0
            if data_age > self.settings.max_data_age_seconds:
                _v9_probability = None
        components: List[Tuple[str, float, float]] = []
        fair = _num(snapshot.get("calibrated_yes_probability"), _num(snapshot.get("estimated_fair_probability")))
        if fair is not None:
            components.append(("model", _clamp(fair, 0.01, 0.99), 0.45))
        yes_bid = _num(snapshot.get("yes_bid"))
        yes_ask = _num(snapshot.get("yes_ask"))
        if yes_bid is not None and yes_ask is not None:
            components.append(("market", _clamp((yes_bid + yes_ask) / 200.0, 0.01, 0.99), 0.20))

        current = _num(snapshot.get("underlying_current"))
        target = _num(snapshot.get("target_value"))
        distance_pct = _num(snapshot.get("target_distance_pct"))
        if distance_pct is None and current and target:
            distance_pct = (current - target) / target * 100.0
        if distance_pct is not None:
            scale = max(0.02, abs(_num(snapshot.get("model_sigma_per_sqrt_second"), 0.0001) or 0.0001) * 100.0 * math.sqrt(max(1.0, _num(snapshot.get("seconds_remaining"), 600.0) or 600.0)))
            distance_p = _sigmoid(distance_pct / max(0.03, scale))
            if snapshot.get("target_type") in {"less_than", "less_or_equal", "below"}:
                distance_p = 1.0 - distance_p
            components.append(("distance", distance_p, 0.15))

        candles = snapshot.get("underlying_candles_5s") or []
        trend30 = _trend_pct(candles, 30.0)
        trend60 = _trend_pct(candles, 60.0)
        trend180 = _trend_pct(candles, 180.0)
        rsi = _rsi(candles)
        chart_z = _clamp(10.0 * trend30 + 6.0 * trend60 + 2.0 * trend180, -2.5, 2.5)
        if rsi is not None:
            chart_z += _clamp((rsi - 50.0) / 25.0, -1.0, 1.0) * 0.20
        chart_p = _sigmoid(chart_z)
        if candles:
            components.append(("chart", chart_p, 0.15))

        flow_yes = _num(snapshot.get("taker_yes_volume_15s"), 0.0) or 0.0
        flow_no = _num(snapshot.get("taker_no_volume_15s"), 0.0) or 0.0
        imbalance = _num(snapshot.get("orderbook_imbalance"), 0.0) or 0.0
        flow_total = flow_yes + flow_no
        flow_skew = (flow_yes - flow_no) / flow_total if flow_total > 0 else 0.0
        micro_p = _sigmoid(1.2 * flow_skew + 0.9 * imbalance)
        if flow_total > 0 or snapshot.get("orderbook_imbalance") is not None:
            components.append(("microstructure", micro_p, 0.05))

        if not components:
            return (_v9_probability if _v9_probability is not None else 0.5), {
                "availability": 0.0,
                "component_agreement": 0.0,
                "trend_30s_pct": trend30,
                "trend_60s_pct": trend60,
                "trend_180s_pct": trend180,
                "rsi_14": rsi,
            }
        total_weight = sum(weight for _, _, weight in components)
        probability = sum(prob * weight for _, prob, weight in components) / total_weight
        directional = [1 if prob >= 0.55 else -1 if prob <= 0.45 else 0 for _, prob, _ in components]
        majority = 1 if sum(directional) > 0 else -1 if sum(directional) < 0 else 0
        aligned = sum(1 for direction in directional if direction == majority and direction != 0)
        agreement = aligned / max(1, len(directional))
        features = {
            "components": {name: round(prob, 6) for name, prob, _ in components},
            "availability": round(min(1.0, total_weight), 4),
            "component_agreement": round(agreement, 4),
            "trend_30s_pct": round(trend30, 6),
            "trend_60s_pct": round(trend60, 6),
            "trend_180s_pct": round(trend180, 6),
            "rsi_14": round(rsi, 4) if rsi is not None else None,
            "flow_skew": round(flow_skew, 5),
            "orderbook_imbalance": round(imbalance, 5),
            "target_distance_pct": round(distance_pct, 6) if distance_pct is not None else None,
            "model_quality": _num(snapshot.get("model_confidence"), 0.0),
            "confirmation_groups": int(snapshot.get("confirmation_group_count") or snapshot.get("confirmation_count") or 0),
            "confirmation_score": _num(snapshot.get("confirmation_score"), 0.0),
            "pattern_direction": snapshot.get("pattern_direction"),
            "pattern": snapshot.get("pattern_detected"),
        }
        return (_v9_probability if _v9_probability is not None else _clamp(probability, 0.01, 0.99)), features

    def _feature_tags(self, snapshot: Mapping[str, Any], features: Mapping[str, Any], side: str) -> List[str]:
        if side not in {"YES", "NO"}:
            return ["uncertain"]
        positive = side == "YES"
        tags = [f"side:{side}"]
        for name, value in (features.get("components") or {}).items():
            aligned = value >= 0.55 if positive else value <= 0.45
            opposed = value <= 0.45 if positive else value >= 0.55
            if aligned:
                tags.append(f"{name}:aligned")
            elif opposed:
                tags.append(f"{name}:opposed")
        pattern_direction = features.get("pattern_direction")
        if pattern_direction in {"bullish", "bearish"}:
            aligned = (positive and pattern_direction == "bullish") or ((not positive) and pattern_direction == "bearish")
            tags.append("pattern:aligned" if aligned else "pattern:opposed")
        groups = int(features.get("confirmation_groups") or 0)
        tags.append(f"groups:{min(4, groups)}")
        return sorted(set(tags))

    def _vote_summary(self, cycle: dict, now: float) -> dict:
        cutoff = now - self.settings.vote_lookback_seconds
        votes = [vote for vote in cycle.get("votes", []) if vote.get("ts", 0) >= cutoff]
        if not votes:
            return {"side": "UNCERTAIN", "consensus": 0.0, "samples": 0, "consecutive": 0, "yes_probability": 0.5, "confidence": 0.0}
        counts = {"YES": 0, "NO": 0, "UNCERTAIN": 0}
        for vote in votes:
            counts[vote.get("side", "UNCERTAIN")] += 1
        side = "YES" if counts["YES"] > counts["NO"] else "NO" if counts["NO"] > counts["YES"] else "UNCERTAIN"
        consensus = counts.get(side, 0) / len(votes) if side != "UNCERTAIN" else 0.0
        consecutive = 0
        for vote in reversed(votes):
            if vote.get("side") == side:
                consecutive += 1
            else:
                break
        mean_p = sum(float(vote.get("yes_probability", 0.5)) for vote in votes) / len(votes)
        confidence = sum(float(vote.get("confidence", 0.0)) for vote in votes) / len(votes)
        latest = votes[-1]
        return {
            "side": side,
            "consensus": consensus,
            "samples": len(votes),
            "consecutive": consecutive,
            "yes_probability": mean_p,
            "confidence": confidence,
            "features": latest.get("features") or {},
            "feature_tags": latest.get("feature_tags") or [],
            "raw_yes_probability": latest.get("raw_yes_probability"),
            "learned_adjustment": latest.get("learned_adjustment", 0.0),
            "counts": counts,
        }

    def _capture_due(self, cycle: dict, snapshot: Mapping[str, Any], now: float) -> None:
        seconds = _num(snapshot.get("seconds_remaining"))
        if seconds is None:
            return
        predictions = cycle["predictions"]
        if "15m" not in predictions:
            if seconds <= self.settings.checkpoint_15_seconds and seconds >= self.settings.checkpoint_15_latest_seconds:
                predictions["15m"] = self._freeze(cycle, snapshot, "15m", now)
            elif seconds < self.settings.checkpoint_15_latest_seconds:
                predictions["15m"] = self._missed(cycle, snapshot, "15m", now)
        if "10m" not in predictions:
            if seconds <= self.settings.checkpoint_10_seconds and seconds >= self.settings.checkpoint_10_latest_seconds:
                predictions["10m"] = self._freeze(cycle, snapshot, "10m", now)
            elif seconds < self.settings.checkpoint_10_latest_seconds:
                predictions["10m"] = self._missed(cycle, snapshot, "10m", now)
        cycle["final_side"], cycle["agreement"] = self._final(predictions.get("15m"), predictions.get("10m"))
        cycle["decision_state"], cycle["late_strong_shadow"] = self._decision_state(
            predictions.get("15m"), predictions.get("10m"), snapshot
        )

    def _freeze(self, cycle: dict, snapshot: Mapping[str, Any], checkpoint: str, now: float) -> dict:
        summary = self._vote_summary(cycle, now)
        candidate_side = summary["side"]
        if candidate_side not in {"YES", "NO"}:
            if summary["yes_probability"] > 0.5:
                candidate_side = "YES"
            elif summary["yes_probability"] < 0.5:
                candidate_side = "NO"
            else:
                candidate_side = "UNCERTAIN"
        candidate_probability = (
            summary["yes_probability"] if candidate_side == "YES"
            else 1.0 - summary["yes_probability"] if candidate_side == "NO"
            else 0.5
        )
        side = summary["side"]
        reason = "consensus_met"
        rejection_details: List[str] = []
        min_consensus = self.settings.initial_consensus
        min_probability = self.settings.initial_side_probability
        early = cycle.get("predictions", {}).get("15m")
        switching = checkpoint == "10m" and early and early.get("side") in {"YES", "NO"} and side != early.get("side")
        if switching:
            min_consensus = self.settings.switch_consensus
            min_probability = self.settings.switch_side_probability
        groups = int(snapshot.get("confirmation_group_count") or snapshot.get("confirmation_count") or 0)
        score = _num(snapshot.get("confirmation_score"), 0.0) or 0.0
        if summary["samples"] < self.settings.min_vote_samples:
            rejection_details.append(f"only {summary['samples']}/{self.settings.min_vote_samples} rolling observations")
        if summary["side"] not in {"YES", "NO"}:
            rejection_details.append("rolling votes were tied or remained neutral")
        if summary["consensus"] < min_consensus:
            rejection_details.append(f"consensus {summary['consensus'] * 100:.0f}% < {min_consensus * 100:.0f}%")
        if candidate_probability < min_probability:
            rejection_details.append(f"side estimate {candidate_probability * 100:.1f}% < {min_probability * 100:.0f}%")
        if switching:
            if groups < self.settings.switch_min_groups:
                rejection_details.append(f"switch groups {groups}/{self.settings.switch_min_groups}")
            if score < self.settings.switch_min_score:
                rejection_details.append(f"switch score {score:.0f} < {self.settings.switch_min_score:.0f}")
            if summary["consecutive"] < self.settings.switch_min_consecutive_votes:
                rejection_details.append(
                    f"switch persistence {summary['consecutive']}/{self.settings.switch_min_consecutive_votes} votes"
                )
        if summary["samples"] < self.settings.min_vote_samples:
            side, reason = "UNCERTAIN", "insufficient_rolling_votes"
        elif summary["side"] not in {"YES", "NO"}:
            side, reason = "UNCERTAIN", "neutral_vote"
        elif summary["consensus"] < min_consensus:
            side, reason = "UNCERTAIN", "consensus_below_threshold"
        elif candidate_probability < min_probability:
            side, reason = "UNCERTAIN", "side_probability_below_threshold"
        elif switching and (
            groups < self.settings.switch_min_groups
            or score < self.settings.switch_min_score
            or summary["consecutive"] < self.settings.switch_min_consecutive_votes
        ):
            side, reason = "UNCERTAIN", "opposite_switch_not_strong_or_persistent_enough"
        sample_progress = min(1.0, summary["samples"] / max(1, self.settings.min_vote_samples))
        consensus_progress = min(1.0, summary["consensus"] / max(0.01, min_consensus))
        probability_progress = min(1.0, candidate_probability / max(0.01, min_probability))
        persistence_progress = 1.0
        if switching:
            persistence_progress = min(1.0, summary["consecutive"] / max(1, self.settings.switch_min_consecutive_votes))
        near_miss_score = 100.0 * (
            0.25 * sample_progress
            + 0.35 * consensus_progress
            + 0.25 * probability_progress
            + 0.15 * persistence_progress
        )
        if side in {"YES", "NO"}:
            rejection_details = []
            near_miss_score = 100.0
        prediction = {
            "checkpoint": checkpoint,
            "side": side,
            "candidate_side": candidate_side,
            "candidate_side_probability": round(float(candidate_probability), 6),
            "reason": reason,
            "rejection_details": rejection_details,
            "near_miss_score": round(near_miss_score, 3),
            "required_consensus": round(float(min_consensus), 4),
            "required_side_probability": round(float(min_probability), 4),
            "captured_at": _iso(now),
            "captured_at_epoch": now,
            "seconds_remaining": _num(snapshot.get("seconds_remaining")),
            "raw_yes_probability": round(float(summary.get("raw_yes_probability") or summary["yes_probability"]), 6),
            "yes_probability": round(float(summary["yes_probability"]), 6),
            "side_probability": round(float(summary["yes_probability"] if side == "YES" else 1.0 - summary["yes_probability"] if side == "NO" else candidate_probability), 6),
            "confidence": round(float(summary["confidence"]), 4),
            "decisiveness": round(abs(float(summary["yes_probability"]) - 0.5) * 2.0, 4),
            "consensus": round(float(summary["consensus"]), 4),
            "sample_count": int(summary["samples"]),
            "consecutive_votes": int(summary["consecutive"]),
            "features": {**(summary.get("features") or {}), "circuit_breaker_active": bool(snapshot.get("learning_circuit_breakers"))},
            "feature_tags": summary.get("feature_tags") or [],
            "learned_adjustment": round(float(summary.get("learned_adjustment") or 0.0), 6),
            "regime_threshold_shadow": self._regime_shadow_recommendation(snapshot),
            "model_version": MODEL_VERSION,
            "policy_version": POLICY_VERSION,
            "schema_version": SCHEMA_VERSION,
            "shadow": self.settings.learning_shadow,
            "warning": DISCLAIMER,
        }
        plan_side = side if side in {"YES", "NO"} else candidate_side
        if plan_side in {"YES", "NO"}:
            prediction["trade_plan"] = self._trade_plan(
                snapshot, plan_side, final_ready=(checkpoint == "10m" and side in {"YES", "NO"}),
                provisional=(checkpoint == "15m"), count_blockers=False
            )
        self._persist_prediction(cycle, prediction)
        return prediction

    def _missed(self, cycle: dict, snapshot: Mapping[str, Any], checkpoint: str, now: float) -> dict:
        prediction = {
            "checkpoint": checkpoint,
            "side": "UNCERTAIN",
            "candidate_side": "UNCERTAIN",
            "candidate_side_probability": 0.5,
            "reason": "checkpoint_missed_after_restart_or_data_gap",
            "rejection_details": ["checkpoint missed after restart or data gap"],
            "near_miss_score": 0.0,
            "captured_at": _iso(now),
            "captured_at_epoch": now,
            "seconds_remaining": _num(snapshot.get("seconds_remaining")),
            "raw_yes_probability": None,
            "yes_probability": None,
            "side_probability": 0.5,
            "confidence": 0.0,
            "decisiveness": 0.0,
            "consensus": 0.0,
            "sample_count": 0,
            "consecutive_votes": 0,
            "features": {},
            "feature_tags": ["missed_checkpoint"],
            "learned_adjustment": 0.0,
            "model_version": MODEL_VERSION,
            "policy_version": POLICY_VERSION,
            "schema_version": SCHEMA_VERSION,
            "shadow": True,
            "warning": DISCLAIMER,
        }
        self._persist_prediction(cycle, prediction)
        return prediction

    @staticmethod
    def _final(pred15: Optional[dict], pred10: Optional[dict]) -> Tuple[Optional[str], str]:
        if not pred15 or not pred10:
            return None, "PENDING"
        first, second = pred15.get("side"), pred10.get("side")
        if first not in {"YES", "NO"} or second not in {"YES", "NO"}:
            return None, "UNCERTAIN"
        if first == second:
            return first, "SAME"
        return None, "CONFLICT"

    @staticmethod
    def _candidate_side(prediction: Optional[Mapping[str, Any]]) -> Optional[str]:
        if not prediction:
            return None
        side = prediction.get("side")
        if side in {"YES", "NO"}:
            return str(side)
        candidate = prediction.get("candidate_side")
        if candidate in {"YES", "NO"}:
            return str(candidate)
        yes_probability = _num(prediction.get("yes_probability"))
        if yes_probability is None or yes_probability == 0.5:
            return None
        return "YES" if yes_probability > 0.5 else "NO"

    @staticmethod
    def _candidate_probability(prediction: Optional[Mapping[str, Any]], side: Optional[str]) -> Optional[float]:
        if not prediction or side not in {"YES", "NO"}:
            return None
        direct = _num(prediction.get("candidate_side_probability"))
        if direct is not None and prediction.get("candidate_side") == side:
            return direct
        direct = _num(prediction.get("side_probability"))
        if direct is not None and prediction.get("side") == side:
            return direct
        yes_probability = _num(prediction.get("yes_probability"))
        if yes_probability is None:
            return None
        return yes_probability if side == "YES" else 1.0 - yes_probability

    def _late_strong_assessment(
        self, pred15: Optional[dict], pred10: Optional[dict], snapshot: Mapping[str, Any]
    ) -> Optional[dict]:
        if not self.settings.late_strong_shadow_enabled or not pred15 or not pred10:
            return None
        if pred15.get("side") in {"YES", "NO"} or pred10.get("side") not in {"YES", "NO"}:
            return None
        side = str(pred10.get("side"))
        probability = self._candidate_probability(pred10, side) or 0.0
        consensus = _num(pred10.get("consensus"), 0.0) or 0.0
        groups = int(snapshot.get("confirmation_group_count") or snapshot.get("confirmation_count") or 0)
        score = _num(snapshot.get("confirmation_score"), 0.0) or 0.0
        plan = self._trade_plan(snapshot, side, final_ready=True, provisional=False, count_blockers=False) or {}
        spread = _num(plan.get("current_executable_ask"))
        bid = _num(plan.get("current_executable_bid"))
        spread = None if spread is None or bid is None else max(0.0, spread - bid)
        net_edge = _num(plan.get("net_edge_after_costs_cents"), -999.0) or -999.0
        reasons: List[str] = []
        if probability < self.settings.late_strong_min_probability:
            reasons.append(f"side estimate {probability * 100:.1f}% < {self.settings.late_strong_min_probability * 100:.0f}%")
        if consensus < self.settings.late_strong_min_consensus:
            reasons.append(f"10m consensus {consensus * 100:.0f}% < {self.settings.late_strong_min_consensus * 100:.0f}%")
        if groups < self.settings.late_strong_min_groups:
            reasons.append(f"confirmation groups {groups}/{self.settings.late_strong_min_groups}")
        if score < self.settings.late_strong_min_score:
            reasons.append(f"confirmation score {score:.0f} < {self.settings.late_strong_min_score:.0f}")
        if net_edge < self.settings.late_strong_min_net_edge_cents:
            reasons.append(f"net edge {net_edge:+.1f}¢ < +{self.settings.late_strong_min_net_edge_cents:.1f}¢")
        if spread is None or spread > self.settings.late_strong_max_spread_cents:
            reasons.append(
                "executable spread missing" if spread is None
                else f"spread {spread:.1f}¢ > {self.settings.late_strong_max_spread_cents:.1f}¢"
            )
        if (_num(snapshot.get("data_age_seconds"), 0.0) or 0.0) > self.settings.max_data_age_seconds:
            reasons.append("critical data stale")
        if not bool(snapshot.get("follow_through_confirmed")):
            reasons.append("follow-through not confirmed")
        return {
            "state": "LATE_STRONG_SHADOW" if not reasons else "LATE_STRONG_REJECTED",
            "side": side,
            "side_probability": round(probability, 5),
            "consensus": round(consensus, 4),
            "confirmation_groups": groups,
            "confirmation_score": round(score, 3),
            "net_edge_after_costs_cents": round(net_edge, 4),
            "spread_cents": round(spread, 4) if spread is not None else None,
            "reasons": reasons,
            "trade_plan": plan,
            "entry_allowed": False,
            "explanation": "Shadow-only diagnostic. The frozen 15m prediction was uncertain, so no new trade is allowed.",
        }

    def _decision_state(
        self, pred15: Optional[dict], pred10: Optional[dict], snapshot: Mapping[str, Any]
    ) -> Tuple[str, Optional[dict]]:
        if not pred15 or not pred10:
            return "PENDING", None
        first, second = pred15.get("side"), pred10.get("side")
        if first in {"YES", "NO"} and second in {"YES", "NO"}:
            return ("AGREEMENT", None) if first == second else ("CONFLICT", None)
        late = self._late_strong_assessment(pred15, pred10, snapshot)
        if late and late.get("state") == "LATE_STRONG_SHADOW":
            return "LATE_STRONG_SHADOW", late
        return "NO_SIGNAL", late

    def _regime_shadow_recommendation(self, snapshot: Mapping[str, Any]) -> dict:
        if not self.settings.regime_thresholds_shadow:
            return {"enabled": False, "applied_live": False}
        regime_text = " ".join(str(snapshot.get(key) or "") for key in (
            "volatility_regime", "market_regime", "regime", "volatility_state"
        )).lower()
        atr_pct = _num(snapshot.get("atr_pct"), _num(snapshot.get("realized_volatility_pct")))
        if any(token in regime_text for token in ("high", "extreme", "volatile", "expansion")) or (atr_pct is not None and atr_pct >= 0.50):
            regime = "HIGH_VOLATILITY"
            suggested = {
                "min_vote_samples": self.settings.min_vote_samples + self.settings.regime_high_samples_add,
                "initial_consensus": round(min(0.95, self.settings.initial_consensus + self.settings.regime_high_consensus_add), 4),
                "required_edge_add_cents": self.settings.regime_high_edge_add_cents,
                "max_spread_cents": self.settings.regime_high_max_spread_cents,
                "management_bias": "TAKE_PROFIT_EARLY",
            }
        elif any(token in regime_text for token in ("low", "calm", "stable", "compression")) or (atr_pct is not None and atr_pct <= 0.20):
            regime = "STABLE"
            suggested = {
                "min_vote_samples": self.settings.min_vote_samples,
                "initial_consensus": round(max(0.60, self.settings.initial_consensus - self.settings.regime_stable_consensus_subtract), 4),
                "required_edge_add_cents": 0.0,
                "max_spread_cents": self.settings.max_spread_cents,
                "management_bias": "NORMAL",
            }
        else:
            regime = "NORMAL_OR_UNKNOWN"
            suggested = {
                "min_vote_samples": self.settings.min_vote_samples,
                "initial_consensus": self.settings.initial_consensus,
                "required_edge_add_cents": 0.0,
                "max_spread_cents": self.settings.max_spread_cents,
                "management_bias": "NORMAL",
            }
        return {
            "enabled": True,
            "regime": regime,
            "observed_atr_or_realized_volatility_pct": atr_pct,
            "suggested_thresholds": suggested,
            "applied_live": False,
            "note": "Shadow-only until enough settled cycles show improved calibration and net EV.",
        }

    @staticmethod
    def _human_blocker(blocker: str) -> str:
        mapping = {
            "awaiting_matching_10m_prediction": "waiting for the matching frozen 10m prediction",
            "side_win_probability_below_55pct": "side estimate below the minimum",
            "two_sided_executable_quote_missing": "executable bid/ask missing",
            "spread_above_3c": "spread above 3¢",
            "less_than_60s_remaining": "less than 60 seconds remain",
            "critical_data_stale": "critical data stale",
            "insufficient_executable_depth": "not enough executable depth",
            "follow_through_not_confirmed": "follow-through not confirmed",
            "strong_chart_or_setup_opposition": "strong chart/setup opposition",
            "required_modeled_edge_not_met": "required model edge not met",
            "adaptive_confirmation_not_met": "confirmation groups/score not met",
        }
        return mapping.get(str(blocker), str(blocker).replace("_", " "))

    def _diagnostic_row(self, asset: str, snap: Mapping[str, Any], checkpoint: str) -> dict:
        cycle = self._cycles.get(str(snap.get("ticker"))) or {}
        p15 = cycle.get("predictions", {}).get("15m")
        p10 = cycle.get("predictions", {}).get("10m")
        target = p15 if checkpoint == "15m" else p10
        candidate_side = self._candidate_side(target)
        probability = self._candidate_probability(target, candidate_side)
        reasons: List[str] = []
        if checkpoint == "15m":
            if not p15:
                reasons.append("15m checkpoint pending")
            elif p15.get("side") not in {"YES", "NO"}:
                reasons.extend([f"15m: {text}" for text in (p15.get("rejection_details") or [p15.get("reason")]) if text])
        else:
            if not p15:
                reasons.append("15m checkpoint missing")
            if not p10:
                reasons.append("10m checkpoint pending")
            if p15 and p10:
                if p15.get("side") in {"YES", "NO"} and p10.get("side") in {"YES", "NO"} and p15.get("side") != p10.get("side"):
                    reasons.append(f"frozen conflict: 15m {p15.get('side')} vs 10m {p10.get('side')}")
                if p15.get("side") not in {"YES", "NO"}:
                    reasons.extend([f"15m: {text}" for text in (p15.get("rejection_details") or [p15.get("reason")]) if text])
                if p10.get("side") not in {"YES", "NO"}:
                    reasons.extend([f"10m: {text}" for text in (p10.get("rejection_details") or [p10.get("reason")]) if text])
        if candidate_side is None:
            other = p10 if checkpoint == "10m" else p15
            candidate_side = self._candidate_side(other)
            probability = self._candidate_probability(other, candidate_side)
        plan = self._trade_plan(snap, candidate_side, final_ready=(checkpoint == "10m"), provisional=(checkpoint == "15m"), count_blockers=False) if candidate_side else None
        final_side, agreement = self._final(p15, p10)
        if checkpoint == "10m" and agreement == "SAME" and final_side in {"YES", "NO"} and plan:
            reasons.extend(self._human_blocker(value) for value in plan.get("blockers") or [])
            if plan.get("status") == "WAIT_FOR_BETTER_PRICE":
                reasons.append("current ask is above the ideal/max entry")
        late = self._late_strong_assessment(p15, p10, snap) if checkpoint == "10m" else None
        near_scores = [
            _num((p15 or {}).get("near_miss_score"), 0.0) or 0.0,
            _num((p10 or {}).get("near_miss_score"), 0.0) or 0.0,
        ]
        near = near_scores[0] if checkpoint == "15m" else sum(near_scores) / 2.0
        net_edge = _num((plan or {}).get("net_edge_after_costs_cents"), -999.0) or -999.0
        closeness = near + max(-20.0, min(20.0, net_edge))
        return {
            "asset": asset,
            "ticker": snap.get("ticker"),
            "candidate_side": candidate_side or "UNCERTAIN",
            "side_probability": round(probability, 5) if probability is not None else None,
            "near_miss_score": round(near, 3),
            "closeness_score": round(closeness, 3),
            "reasons": reasons or ["no directional candidate"],
            "trade_plan": plan,
            "late_strong_shadow": late,
        }

    def _rebuild_rankings(self, close_key: str, snapshots: Mapping[str, dict]) -> None:
        early_rows: List[dict] = []
        final_rows: List[dict] = []
        early_diagnostics: List[dict] = []
        final_diagnostics: List[dict] = []
        late_shadow_rows: List[dict] = []
        for asset, snap in snapshots.items():
            cycle = self._cycles.get(str(snap.get("ticker"))) or {}
            p15 = cycle.get("predictions", {}).get("15m")
            p10 = cycle.get("predictions", {}).get("10m")
            early_diagnostics.append(self._diagnostic_row(asset, snap, "15m"))
            final_diagnostic = self._diagnostic_row(asset, snap, "10m")
            final_diagnostics.append(final_diagnostic)
            if (final_diagnostic.get("late_strong_shadow") or {}).get("state") == "LATE_STRONG_SHADOW":
                late_shadow_rows.append(final_diagnostic)
            if p15 and p15.get("side") in {"YES", "NO"}:
                plan = self._trade_plan(snap, p15["side"], final_ready=False, provisional=True, count_blockers=False)
                ev = _num((plan or {}).get("net_edge_after_costs_cents"), -999.0) or -999.0
                prediction_quality = 100.0 * (
                    0.55 * float(p15.get("confidence") or 0.0)
                    + 0.45 * float(p15.get("decisiveness") or 0.0)
                )
                early_rows.append({
                    "asset": asset, "ticker": snap.get("ticker"), "side": p15["side"],
                    "score": round(ev, 3), "ranking_expected_value_cents": round(ev, 4),
                    "prediction_quality": round(prediction_quality, 3),
                    "prediction": p15, "trade_plan": plan,
                })
            final_side, agreement = self._final(p15, p10)
            if agreement == "SAME" and final_side in {"YES", "NO"}:
                confidence = (float(p15.get("confidence") or 0.0) + float(p10.get("confidence") or 0.0)) / 2.0
                decisive = (float(p15.get("decisiveness") or 0.0) + float(p10.get("decisiveness") or 0.0)) / 2.0
                quality = _num(snap.get("model_confidence"), 0.0) or 0.0
                prediction_quality = 100.0 * (0.45 * confidence + 0.35 * decisive + 0.20 * quality)
                plan = self._trade_plan(snap, final_side, final_ready=True, provisional=False, count_blockers=False)
                ev = _num((plan or {}).get("net_edge_after_costs_cents"), -999.0) or -999.0
                final_rows.append({
                    "asset": asset,
                    "ticker": snap.get("ticker"),
                    "side": final_side,
                    "score": round(ev, 3),
                    "ranking_expected_value_cents": round(ev, 4),
                    "prediction_quality": round(prediction_quality, 3),
                    "combined_confidence": round(confidence, 4),
                    "combined_decisiveness": round(decisive, 4),
                    "trade_plan": plan,
                })
        early_rows.sort(
            key=lambda row: (
                _num(row.get("ranking_expected_value_cents"), -999.0) or -999.0,
                _num(row.get("prediction_quality"), 0.0) or 0.0,
            ), reverse=True
        )
        status_priority = {"READY": 2, "WAIT_FOR_BETTER_PRICE": 1, "NO_TRADE": 0}
        final_rows.sort(
            key=lambda row: (
                status_priority.get((row.get("trade_plan") or {}).get("status"), 0),
                _num(row.get("ranking_expected_value_cents"), -999.0) or -999.0,
                _num(row.get("prediction_quality"), 0.0) or 0.0,
            ), reverse=True
        )
        early_diagnostics.sort(key=lambda row: row.get("closeness_score", -999.0), reverse=True)
        final_diagnostics.sort(key=lambda row: row.get("closeness_score", -999.0), reverse=True)
        late_shadow_rows.sort(
            key=lambda row: _num((row.get("late_strong_shadow") or {}).get("net_edge_after_costs_cents"), -999.0) or -999.0,
            reverse=True,
        )
        for index, row in enumerate(early_rows, 1):
            row["rank"] = index
        for index, row in enumerate(final_rows, 1):
            row["rank"] = index
        top = final_rows[: max(1, self.settings.top_n)]
        self._top_by_close[close_key] = {row["asset"] for row in top}
        self._rankings[close_key] = {
            "close_time": close_key,
            "ranking_method": "trade_status_then_net_expected_edge_after_costs_then_prediction_quality",
            "early_15m": early_rows,
            "final": final_rows,
            "top_three": top,
            "early_diagnostics": early_diagnostics[: max(3, self.settings.top_n)],
            "final_diagnostics": final_diagnostics[: max(3, self.settings.top_n)],
            "late_strong_shadow": late_shadow_rows[: max(3, self.settings.top_n)],
        }

    # ----------------------------- price/management -------------------------
    def _trade_plan(
        self, snapshot: Mapping[str, Any], side: Optional[str], final_ready: bool, provisional: bool,
        count_blockers: bool = True,
    ) -> Optional[dict]:
        if side not in {"YES", "NO"}:
            return None
        bid, ask, spread = _side_prices(snapshot, side)
        fair_yes = _num(snapshot.get("calibrated_yes_probability"), _num(snapshot.get("estimated_fair_probability")))
        if fair_yes is None:
            fair_yes = _num(snapshot.get("end_prediction_yes_probability"))
        side_probability = fair_yes if side == "YES" else (1.0 - fair_yes if fair_yes is not None else None)
        if side_probability is None:
            side_probability = 0.5
        fair_value = side_probability * 100.0
        required_edge = max(5.0, _num(snapshot.get("required_edge"), 5.0) or 5.0)
        fee_rate = _num(getattr(self.config, "fee_rate", None), _num(os.environ.get("Q15_FEE_RATE"), 0.07)) or 0.07
        entry_fee = _fee_cents(ask, fee_rate=fee_rate)
        slippage = max(0.1, (spread or 0.0) * self.settings.slippage_fraction_of_spread)
        maximum = fair_value - required_edge - entry_fee - slippage - self.settings.safety_buffer_cents
        maximum = _clamp(maximum, 1.0, 98.0)
        ideal = _clamp(maximum - 1.5, 1.0, maximum)
        ideal_low = _clamp(ideal - 1.0, 1.0, maximum)
        ideal_high = _clamp(ideal + 1.0, ideal_low, maximum)
        primary_ceiling = _clamp(fair_value - self.settings.uncertainty_buffer_cents, 2.0, 98.0)
        primary = _clamp(max((ask or ideal) + 3.0, ideal + 3.0), 2.0, primary_ceiling)
        stretch_ceiling = _clamp(fair_value - 1.0, primary, 99.0)
        stretch = _clamp(max(primary, primary + 2.0), primary, stretch_ceiling)
        stop_distance = max(5.0, min(10.0, (primary - (ask or ideal)) * 0.55))
        stop = _clamp((ask or ideal) - stop_distance, 1.0, 95.0)
        exit_fee = _fee_cents(primary, fee_rate=fee_rate)
        estimated_fees = entry_fee + exit_fee
        net_edge_before_costs = fair_value - ask if ask is not None else None
        net_edge_after_costs = (fair_value - ask - entry_fee - slippage) if ask is not None else None
        net_profit = primary - (ask or ideal) - estimated_fees - slippage
        estimated_loss = (ask or ideal) - stop + entry_fee + slippage
        reward_risk = net_profit / estimated_loss if estimated_loss > 0 else None
        blockers: List[str] = []
        seconds = _num(snapshot.get("seconds_remaining"))
        data_age = _num(snapshot.get("data_age_seconds"), 0.0) or 0.0
        depth = _side_depth(snapshot, side)
        if provisional or not final_ready:
            blockers.append("awaiting_matching_10m_prediction")
        if side_probability < self.settings.min_win_probability:
            blockers.append("side_win_probability_below_55pct")
        if ask is None or bid is None:
            blockers.append("two_sided_executable_quote_missing")
        if spread is None or spread > self.settings.max_spread_cents:
            blockers.append("spread_above_3c")
        if seconds is None or seconds < self.settings.min_entry_seconds:
            blockers.append("less_than_60s_remaining")
        if data_age > self.settings.max_data_age_seconds:
            blockers.append("critical_data_stale")
        if depth is None or depth < self.settings.min_depth:
            blockers.append("insufficient_executable_depth")
        if not bool(snapshot.get("follow_through_confirmed")):
            blockers.append("follow_through_not_confirmed")
        current_side = snapshot.get("entry_side")
        groups = int(snapshot.get("confirmation_group_count") or snapshot.get("confirmation_count") or 0)
        if current_side in {"YES", "NO"} and current_side != side and groups >= 3:
            blockers.append("strong_chart_or_setup_opposition")
        modeled_edge = _num(snapshot.get("estimated_edge"), -999.0)
        if modeled_edge is not None and current_side == side and modeled_edge < required_edge:
            blockers.append("required_modeled_edge_not_met")
        if not adaptive_confirmation_ok(
            snapshot=snapshot,
            edge=modeled_edge,
            required_edge=required_edge,
            settings=self.settings,
        ):
            blockers.append("adaptive_confirmation_not_met")
        if count_blockers:
            for blocker in blockers:
                self._blocker_counts[blocker] += 1
        if blockers:
            status = "NO_TRADE"
        elif ask is not None and ask > maximum:
            status = "WAIT_FOR_BETTER_PRICE"
        elif ask is not None and ask > ideal_high:
            status = "WAIT_FOR_BETTER_PRICE"
        else:
            status = "READY"
        confidence = _num(snapshot.get("model_confidence"), 0.0) or 0.0
        warnings = list(snapshot.get("monitor_warnings") or []) + list(snapshot.get("data_quality_warnings") or [])
        management_mode = "TAKE_PROFIT_EARLY_AT_PRIMARY"
        if (
            status == "READY"
            and side_probability >= self.settings.management_hold_probability
            and confidence >= self.settings.management_hold_confidence
            and (spread or 99.0) <= 2.0
            and not warnings
        ):
            management_mode = "HOLD_TO_CLOSE_OR_STRETCH_TP"
        return {
            "side": side,
            "current_executable_ask": round(ask, 4) if ask is not None else None,
            "current_executable_bid": round(bid, 4) if bid is not None else None,
            "ideal_entry_price": round(ideal, 4),
            "ideal_entry_zone": [round(ideal_low, 4), round(ideal_high, 4)],
            "absolute_maximum_entry_price": round(maximum, 4),
            "model_fair_value": round(fair_value, 4),
            "side_win_probability": round(side_probability, 5),
            "net_edge_before_costs_cents": round(net_edge_before_costs, 4) if net_edge_before_costs is not None else None,
            "net_edge_after_costs_cents": round(net_edge_after_costs, 4) if net_edge_after_costs is not None else None,
            "expected_settlement_ev_cents": round(net_edge_after_costs, 4) if net_edge_after_costs is not None else None,
            "primary_take_profit": round(primary, 4),
            "stretch_take_profit": round(stretch, 4),
            "contract_price_stop_reference": round(stop, 4),
            "estimated_entry_and_exit_fees": round(estimated_fees, 4),
            "estimated_slippage": round(slippage, 4),
            "estimated_net_profit_at_primary": round(net_profit, 4),
            "estimated_loss_at_stop": round(estimated_loss, 4),
            "reward_risk": round(reward_risk, 4) if reward_risk is not None else None,
            "status": status,
            "blockers": blockers,
            "management_mode": management_mode if status != "NO_TRADE" else "NO_TRADE",
            "management_explanation": (
                "Strong probability, data quality, and tight spread support holding for settlement or stretch TP."
                if management_mode == "HOLD_TO_CLOSE_OR_STRETCH_TP" and status != "NO_TRADE"
                else "Default to the primary take-profit; holding to settlement adds binary outcome risk."
            ),
            "warning": "Contract stop is a warning reference only; frozen underlying invalidation and sustained opposite evidence remain primary.",
        }

    # ----------------------------- notifications ----------------------------
    def _maybe_notify(self, close_key: str, snapshots: Mapping[str, dict], now: float) -> None:
        if not self.settings.telegram_enabled or self.notifier is None or not getattr(self.notifier, "enabled", False):
            return
        seconds_values = [_num(snap.get("seconds_remaining")) for snap in snapshots.values()]
        seconds_values = [value for value in seconds_values if value is not None]
        if not close_key:
            # No close_time -> cannot build a unique per-market claim key, so the
            # checkpoint alert is dropped. When checkpoint alerts are enabled this
            # must not be SILENT: if a market is near a checkpoint, a degraded feed
            # is costing the owner alerts. Warn (throttled to once a minute).
            if self.settings.checkpoint_alerts_enabled:
                due = bool(seconds_values) and min(seconds_values) <= self.settings.alert_15_at_seconds
                if due and (now - self._empty_close_key_warned_at) >= 60.0:
                    self._empty_close_key_warned_at = now
                    logger.warning(
                        "Checkpoint alert skipped: missing close_time (empty close_key) while a "
                        "market is %.0fs from close — degraded feed is suppressing alerts.",
                        min(seconds_values),
                    )
            return
        if not seconds_values:
            return
        # Two-window checkpoint alerts use the older plain-text format and are OFF
        # by default in favour of the unified V9.5 CHECK panel. The ranking they
        # read is still built every cycle (dashboard + learning); only the Telegram
        # delivery is gated. Re-enable with Q15_FOCUS_CHECKPOINT_ALERTS=true.
        if self.settings.checkpoint_alerts_enabled:
            seconds = min(seconds_values)
            ranking = self._rankings.get(close_key, {})
            if seconds <= self.settings.alert_15_at_seconds:
                key = f"q15-two-window:15m:{close_key}"
                rows = ranking.get("early_15m", [])
                self._claim_and_send(key, self._format_checkpoint_alert(
                    "15m", rows[0] if rows else None, ranking.get("early_diagnostics", [])
                ))
            if seconds <= self.settings.alert_10_at_seconds:
                key = f"q15-two-window:10m:{close_key}"
                rows = ranking.get("final", [])
                self._claim_and_send(key, self._format_checkpoint_alert(
                    "10m", rows[0] if rows else None, ranking.get("final_diagnostics", [])
                ))
            if seconds <= self.settings.alert_7_at_seconds:
                key = f"q15-two-window:7m:{close_key}"
                rows = ranking.get("final", [])
                self._claim_and_send(key, self._format_checkpoint_alert(
                    "7m", rows[0] if rows else None, ranking.get("final_diagnostics", [])
                ))
        self._maybe_dip_alert(close_key, snapshots, now)

    def _maybe_dip_alert(self, close_key: str, snapshots: Mapping[str, dict], now: float) -> None:
        """Between-checkpoint "dip" alert.

        Pings once when the live price for the model's preferred side becomes
        favorable versus fair value (conservative edge after costs clears a
        threshold) — i.e. a price dip the user would otherwise miss between the
        fixed 15m/10m/7m reports. Read-only/observational: it never mutates the
        engine, never feeds the learning corpus, and never changes entry
        decisions. Anti-spam: one ping per dip event (the trigger re-arms only
        after the edge recedes below ``dip_alert_rearm_edge_cents``), a per-asset
        cooldown, and a per-cycle cap.
        """
        if not self.settings.dip_alert_enabled:
            return
        for asset, snap in snapshots.items():
            ticker = snap.get("ticker")
            if not ticker:
                continue
            seconds = _num(snap.get("seconds_remaining"))
            if seconds is None or seconds < self.settings.dip_alert_min_seconds_remaining:
                continue
            side = str(snap.get("calibrated_edge_side") or "").upper()
            edge = _num(snap.get("conservative_edge_after_costs_cents"))
            if edge is None:
                edge = _num(snap.get("net_edge_cents"))
            win = _num(snap.get("calibrated_side_probability"))
            if side not in {"YES", "NO"} or edge is None:
                continue
            state = self._dip_state.setdefault(
                str(ticker), {"armed": True, "count": 0, "last_sent": None, "side": side}
            )
            if state.get("side") != side:
                # Preferred side flipped — treat as a fresh dip opportunity.
                state.update({"armed": True, "side": side})
            if not state["armed"] and edge <= self.settings.dip_alert_rearm_edge_cents:
                state["armed"] = True
            if not state["armed"]:
                continue
            if edge < self.settings.dip_alert_min_edge_cents:
                continue
            if win is not None and win < self.settings.dip_alert_min_win_probability:
                continue
            if state["count"] >= self.settings.dip_alert_max_per_cycle:
                continue
            last_sent = state["last_sent"]
            if last_sent is not None and (now - last_sent) < self.settings.dip_alert_cooldown_seconds:
                continue
            key = f"q15-dip:{close_key}:{asset}:{state['count'] + 1}"
            # Send only if we win the claim. On a LOST claim ("duplicate") the
            # store is permanent and SHARED across dev+prod processes, so the
            # event was already delivered (by a peer or before a restart) —
            # advance local state anyway to avoid starving on the claimed key.
            # On a delivery FAILURE ("failed") leave the trigger armed and the
            # counters untouched so the next cycle retries instead of silently
            # dropping the dip alert.
            result = self._claim_and_send(
                key, self._format_dip_alert(asset, side, snap, edge, win, seconds)
            )
            if result == "failed":
                continue
            state["armed"] = False
            state["count"] += 1
            state["last_sent"] = now

    def _format_dip_alert(
        self,
        asset: str,
        side: str,
        snap: Mapping[str, Any],
        edge: float,
        win: Optional[float],
        seconds: float,
    ) -> str:
        ask = _num(snap.get("yes_ask" if side == "YES" else "no_ask"))
        if ask is not None and 0 < ask <= 1:
            ask = ask * 100.0
        minutes = int(max(0.0, seconds) // 60)
        secs = int(max(0.0, seconds) % 60)
        detail: List[str] = []
        if ask is not None and 0 < ask <= 100:
            detail.append(f"{_esc(side)} ask {ask:.0f}¢")
        detail.append(f"edge {edge:+.1f}¢ vs fair")
        if win is not None:
            detail.append(f"win {win * 100:.0f}%")
        return "\n".join([
            f"⚡ <b>DIP — {_esc(asset)} {_esc(side)}</b>",
            " · ".join(detail),
            f"~{minutes}m{secs:02d}s left · price cheaper than fair value",
            DISCLAIMER,
        ])

    def _format_no_trade_diagnostics(self, checkpoint: str, diagnostics: Sequence[Mapping[str, Any]]) -> List[str]:
        lines = ["Closest candidates:"]
        for index, item in enumerate(list(diagnostics)[:3], 1):
            asset = item.get("asset") or "?"
            side = item.get("candidate_side") or "UNCERTAIN"
            probability = _num(item.get("side_probability"))
            probability_text = f" — side estimate {probability * 100:.1f}%" if probability is not None else ""
            reasons = [str(value) for value in (item.get("reasons") or []) if value]
            reason_text = "; ".join(reasons[:2]) if reasons else "no directional candidate"
            lines.append(f"{index}. <b>{_esc(asset)} {_esc(side)}</b>{probability_text}")
            lines.append(f"   Rejected: {_esc(reason_text)}")
            late = item.get("late_strong_shadow") or {}
            if checkpoint in ("10m", "7m") and late.get("state") == "LATE_STRONG_SHADOW":
                lines.append("   Shadow note: exceptionally strong 10m setup, but 15m was uncertain — no entry allowed.")
        return lines

    @staticmethod
    def _end_probability_yes(
        checkpoint: str,
        row: Optional[Mapping[str, Any]],
        prediction: Mapping[str, Any],
        plan: Mapping[str, Any],
        probability: Any,
    ) -> Optional[float]:
        """Canonical end-of-window YES probability for the checkpoint pick.

        15m rows carry the frozen prediction (with yes_probability); 10m/7m
        rows do not, so derive p_yes from the selected side's win probability.
        Read-only: no state is mutated.
        """
        p_yes = _num((prediction or {}).get("yes_probability"))
        if p_yes is None:
            p_yes = _num((prediction or {}).get("raw_yes_probability"))
        if p_yes is None:
            side = str((row or {}).get("side") or (plan or {}).get("side") or "").upper()
            side_prob = _num(probability)
            if side_prob is None:
                side_prob = _num((plan or {}).get("side_win_probability"))
            if side_prob is not None and side in {"YES", "NO"}:
                p_yes = side_prob if side == "YES" else 1.0 - side_prob
        if p_yes is None:
            return None
        return min(1.0, max(0.0, float(p_yes)))

    def _no_confidence(self, prediction: Optional[Mapping[str, Any]]) -> Optional[dict]:
        """Read-only NO-conviction score for a 10m prediction.

        Returns None when disabled or when the prediction is not a committed NO.
        Otherwise returns {"label": "STRONG"|"WEAK", "reasons": [...],
        "metrics": {...}}. Pure/observational: it never mutates state and never
        gates an entry decision. The thresholds encode the settled-history
        signature where a 10m NO has been ~97% accurate.
        """
        if not self.settings.no_confidence_enabled:
            return None
        if not isinstance(prediction, Mapping):
            return None
        if str(prediction.get("side") or "").upper() != "NO":
            return None
        consensus = _num(prediction.get("consensus"))
        side_prob = _num(prediction.get("side_probability"))
        decisive = _num(prediction.get("decisiveness"))
        tags = {str(tag) for tag in (prediction.get("feature_tags") or [])}
        reasons: List[str] = []
        if consensus is None or consensus < self.settings.no_confidence_min_consensus:
            reasons.append(
                f"consensus {(consensus or 0.0) * 100:.0f}% < {self.settings.no_confidence_min_consensus * 100:.0f}%"
            )
        if side_prob is None or side_prob < self.settings.no_confidence_min_side_probability:
            reasons.append(
                f"side prob {(side_prob or 0.0) * 100:.0f}% < {self.settings.no_confidence_min_side_probability * 100:.0f}%"
            )
        if decisive is None or decisive < self.settings.no_confidence_min_decisiveness:
            reasons.append(
                f"decisiveness {(decisive or 0.0):.2f} < {self.settings.no_confidence_min_decisiveness:.2f}"
            )
        if self.settings.no_confidence_require_alignment:
            missing = [t.split(":")[0] for t in ("market:aligned", "distance:aligned", "model:aligned") if t not in tags]
            if missing:
                reasons.append("missing alignment: " + ", ".join(missing))
        return {
            "label": "STRONG" if not reasons else "WEAK",
            "reasons": reasons,
            "metrics": {
                "consensus": consensus,
                "side_probability": side_prob,
                "decisiveness": decisive,
            },
            "shadow": True,
        }

    def _yes_confidence(self, prediction: Optional[Mapping[str, Any]]) -> Optional[dict]:
        """Read-only YES-conviction score for a 10m prediction.

        Returns None when disabled or when the prediction is not a committed YES.
        Otherwise returns {"label": "STRONG"|"WEAK", "reasons": [...],
        "metrics": {...}}. Pure/observational: it never mutates state and never
        gates an entry decision. The thresholds encode the settled-history
        signature where a 10m YES has been ~90% accurate when all three macro
        signals are aligned (distance alignment being the dominant driver) and is
        otherwise close to a coin-flip.
        """
        if not self.settings.yes_confidence_enabled:
            return None
        if not isinstance(prediction, Mapping):
            return None
        if str(prediction.get("side") or "").upper() != "YES":
            return None
        consensus = _num(prediction.get("consensus"))
        side_prob = _num(prediction.get("side_probability"))
        decisive = _num(prediction.get("decisiveness"))
        tags = {str(tag) for tag in (prediction.get("feature_tags") or [])}
        reasons: List[str] = []
        if consensus is None or consensus < self.settings.yes_confidence_min_consensus:
            reasons.append(
                f"consensus {(consensus or 0.0) * 100:.0f}% < {self.settings.yes_confidence_min_consensus * 100:.0f}%"
            )
        if side_prob is None or side_prob < self.settings.yes_confidence_min_side_probability:
            reasons.append(
                f"side prob {(side_prob or 0.0) * 100:.0f}% < {self.settings.yes_confidence_min_side_probability * 100:.0f}%"
            )
        if decisive is None or decisive < self.settings.yes_confidence_min_decisiveness:
            reasons.append(
                f"decisiveness {(decisive or 0.0):.2f} < {self.settings.yes_confidence_min_decisiveness:.2f}"
            )
        if self.settings.yes_confidence_require_alignment:
            missing = [t.split(":")[0] for t in ("market:aligned", "distance:aligned", "model:aligned") if t not in tags]
            if missing:
                reasons.append("missing alignment: " + ", ".join(missing))
        return {
            "label": "STRONG" if not reasons else "WEAK",
            "reasons": reasons,
            "metrics": {
                "consensus": consensus,
                "side_probability": side_prob,
                "decisiveness": decisive,
            },
            "shadow": True,
        }

    def _format_checkpoint_alert(
        self, checkpoint: str, row: Optional[dict], diagnostics: Optional[Sequence[Mapping[str, Any]]] = None
    ) -> str:
        if row is None:
            title = {
                "15m": "15M EARLY CHECK",
                "10m": "10M FINAL CHECK",
                "7m": "7M FINAL CHECK",
            }.get(checkpoint, "10M FINAL CHECK")
            lines = [f"🛑 <b>{title} — NO TRADE</b>"]
            lines.extend(self._format_no_trade_diagnostics(checkpoint, diagnostics or []))
            lines.append(f"<i>{DISCLAIMER}</i>")
            return "\n".join(lines)
        asset, side = _esc(row.get("asset")), _esc(row.get("side"))
        prediction = row.get("prediction") or {}
        plan = row.get("trade_plan") or prediction.get("trade_plan") or {}
        if checkpoint == "15m":
            lines = [
                f"🕒 <b>15M EARLY #1 WATCH — {asset} {side}</b>",
            ]
            confidence = prediction.get("confidence")
            probability = prediction.get("side_probability")
        else:
            final_label = "7M FINAL" if checkpoint == "7m" else "10M FINAL"
            status = plan.get("status") or "NO_TRADE"
            if status == "READY":
                title = f"🎯 <b>{final_label} #1 READY — {asset} {side}</b>"
            elif status == "WAIT_FOR_BETTER_PRICE":
                title = f"⏳ <b>{final_label} #1 — WAIT FOR PRICE — {asset} {side}</b>"
            else:
                title = f"🛑 <b>{final_label} #1 CANDIDATE — NO TRADE — {asset} {side}</b>"
            lines = [title]
            confidence = row.get("combined_confidence")
            probability = (plan or {}).get("side_win_probability")
            row_side = str(row.get("side") or "").upper()
            if row_side == "NO":
                cyc = self._cycles.get(str(row.get("ticker"))) or {}
                nc = self._no_confidence((cyc.get("predictions") or {}).get("10m"))
                if nc and nc["label"] == "STRONG":
                    lines.append("NO conviction: <b>STRONG</b> ✅ — matches the historically ~97%-reliable 10m-NO signature (shadow)")
                elif nc:
                    why = "; ".join(nc["reasons"][:3])
                    lines.append(f"NO conviction: <b>WEAK</b> ⚠️ — {_esc(why)} (shadow)")
            elif row_side == "YES":
                cyc = self._cycles.get(str(row.get("ticker"))) or {}
                yc = self._yes_confidence((cyc.get("predictions") or {}).get("10m"))
                if yc and yc["label"] == "STRONG":
                    lines.append("YES conviction: <b>STRONG</b> ✅ — matches the historically ~90%-reliable 10m-YES signature (shadow)")
                elif yc:
                    why = "; ".join(yc["reasons"][:3])
                    lines.append(f"YES conviction: <b>WEAK</b> ⚠️ — {_esc(why)} (shadow)")
        if probability is not None:
            lines.append(f"Estimated side probability: <b>{float(probability) * 100:.1f}%</b>")
        end_yes = self._end_probability_yes(checkpoint, row, prediction, plan, probability)
        if end_yes is not None:
            lines.append(
                f"Overall end probability — YES: <b>{end_yes * 100:.1f}%</b> | "
                f"NO: <b>{(1.0 - end_yes) * 100:.1f}%</b>"
            )
        if confidence is not None:
            lines.append(f"Model/data confidence: {float(confidence) * 100:.0f}%")
        if plan:
            ask = plan.get("current_executable_ask")
            zone = plan.get("ideal_entry_zone") or [None, None]
            net_edge = plan.get("net_edge_after_costs_cents")
            lines.append(f"Plan status: <b>{_esc(plan.get('status'))}</b>")
            if net_edge is not None:
                lines.append(f"Net expected edge after entry costs: <b>{float(net_edge):+.1f}¢</b>")
            if ask is not None:
                lines.append(f"Current executable ask: {float(ask):.1f}¢")
            if zone[0] is not None:
                lines.append(f"Ideal entry zone: {float(zone[0]):.1f}–{float(zone[1]):.1f}¢ | absolute max {float(plan.get('absolute_maximum_entry_price')):.1f}¢")
            lines.append(f"Primary TP: {float(plan.get('primary_take_profit')):.1f}¢ | stretch TP: {float(plan.get('stretch_take_profit')):.1f}¢")
            lines.append(f"Stop reference: {float(plan.get('contract_price_stop_reference')):.1f}¢ | R/R {plan.get('reward_risk')}")
            lines.append(f"Management: <b>{_esc(plan.get('management_mode'))}</b>")
            lines.append(_esc(plan.get("management_explanation") or ""))
            blockers = plan.get("blockers") or []
            if blockers:
                lines.append("Current blockers: " + ", ".join(_esc(self._human_blocker(value)) for value in blockers[:4]))
        lines.append(f"<i>{DISCLAIMER}</i>")
        return "\n".join(line for line in lines if line)

    # ----------------------------- settlement learning ----------------------
    def _grade_cycle(self, cycle: dict, actual: str, now: float) -> None:
        with self._lock:
            if cycle.get("graded"):
                return
            p15 = cycle.get("predictions", {}).get("15m") or {}
            p10 = cycle.get("predictions", {}).get("10m") or {}
            early_correct = p15.get("side") == actual if p15.get("side") in {"YES", "NO"} else None
            late_correct = p10.get("side") == actual if p10.get("side") in {"YES", "NO"} else None
            final_side, agreement = self._final(p15, p10)
            conflict_class = None
            if agreement == "CONFLICT":
                if early_correct and not late_correct:
                    conflict_class = "early_prediction_correct_late_flip_wrong"
                elif late_correct and not early_correct:
                    conflict_class = "early_prediction_wrong_late_correction_right"
                else:
                    conflict_class = "nonbinary_or_void_only_both_wrong_is_impossible_for_opposite_binary_sides"
            changes = self._feature_changes(p15.get("features") or {}, p10.get("features") or {})
            aligned_notes: List[str] = []
            misleading_notes: List[str] = []
            for label, prediction, correct in (("15m", p15, early_correct), ("10m", p10, late_correct)):
                tags = prediction.get("feature_tags") or []
                if correct is True:
                    aligned_notes.extend(f"{label}:{tag}" for tag in tags)
                elif correct is False:
                    misleading_notes.extend(f"{label}:{tag}" for tag in tags)
                if prediction.get("side") in {"YES", "NO"} and correct is not None:
                    self._update_feature_stats(cycle.get("asset"), tags, bool(correct))
            grade = {
                "ticker": cycle.get("ticker"),
                "asset": cycle.get("asset"),
                "market_close": cycle.get("close_time"),
                "actual_result": actual,
                "prediction_15m": p15.get("side"),
                "prediction_10m": p10.get("side"),
                "correct_15m": early_correct,
                "correct_10m": late_correct,
                "agreement": agreement,
                "final_result": final_side or "UNCERTAIN",
                "conflict_classification": conflict_class,
                "feature_changes": changes,
                "aligned_notes": aligned_notes,
                "misleading_notes": misleading_notes,
                "model_version": MODEL_VERSION,
                "policy_version": POLICY_VERSION,
                "schema_version": SCHEMA_VERSION,
                "graded_at": _iso(now),
            }
            cycle["graded"] = True
            cycle["actual_result"] = actual
            cycle["grade"] = grade
            self._persist_grade(grade)
            self._execute(
                "UPDATE q15_prediction_checkpoints SET outcome=%s, correct=(predicted_side=%s), graded_at=NOW() WHERE ticker=%s",
                (actual, actual, cycle.get("ticker")),
            )
            # Final 10m prediction self-review: grade vs the real result, diagnose
            # why, persist the post-mortem, and (when a failure mode repeats and no
            # circuit breaker is active) auto-adapt the relevant decision threshold.
            if getattr(self, "self_review", None) is not None and self.self_review.enabled:
                decision_state, _ = self._decision_state(p15, p10, {})
                try:
                    cycle["self_review"] = self.self_review.review(
                        ticker=cycle.get("ticker"),
                        asset=cycle.get("asset"),
                        market_close=cycle.get("close_time"),
                        prediction=p10,
                        agreement=agreement,
                        decision_state=decision_state,
                        actual_result=actual,
                        now=now,
                        breaker_active=self._breaker_active,
                        prior_side=p15.get("side"),
                    )
                except Exception as exc:  # never let self-review break settlement
                    logger.warning("self-review failed for %s: %s", cycle.get("ticker"), exc)

    @staticmethod
    def _feature_changes(first: Mapping[str, Any], second: Mapping[str, Any]) -> dict:
        changes = {}
        for key in sorted(set(first) | set(second)):
            left = _num(first.get(key))
            right = _num(second.get(key))
            if left is not None and right is not None:
                changes[key] = round(right - left, 8)
        return changes

    def _learned_adjustment(self, asset: Optional[str], side: str, tags: Sequence[str]) -> float:
        if side not in {"YES", "NO"} or not self._db_ready or not tags:
            return 0.0
        keys = [f"global|{side}|{tag}" for tag in tags]
        if asset:
            keys += [f"asset:{asset}|{side}|{tag}" for tag in tags]
        try:
            rows = self._query(
                "SELECT feature_key, n, adjustment_pp FROM q15_prediction_feature_stats WHERE feature_key = ANY(%s)",
                (keys,),
            )
        except Exception:
            return 0.0
        values = [float(row.get("adjustment_pp") or 0.0) / 100.0 for row in rows if int(row.get("n") or 0) >= self.settings.learning_min_samples]
        if not values:
            return 0.0
        cap = max(0.0, self.settings.max_learned_adjustment_pp) / 100.0
        return _clamp(sum(values) / math.sqrt(len(values)), -cap, cap)

    def _update_feature_stats(self, asset: Optional[str], tags: Sequence[str], correct: bool) -> None:
        if not self._db_ready:
            return
        sides = [tag for tag in tags if tag.startswith("side:")]
        side = sides[0].split(":", 1)[1] if sides else "UNKNOWN"
        keys = [f"global|{side}|{tag}" for tag in tags]
        if asset:
            keys += [f"asset:{asset}|{side}|{tag}" for tag in tags]
        cap = max(0.0, self.settings.max_learned_adjustment_pp)
        for key in keys:
            self._execute(
                "INSERT INTO q15_prediction_feature_stats(feature_key,n,correct_count,adjustment_pp,updated_at) "
                "VALUES (%s,1,%s,0,NOW()) ON CONFLICT(feature_key) DO UPDATE SET "
                "n=q15_prediction_feature_stats.n+1, "
                "correct_count=q15_prediction_feature_stats.correct_count+EXCLUDED.correct_count, "
                "adjustment_pp=GREATEST(-%s,LEAST(%s, "
                "(((q15_prediction_feature_stats.correct_count+EXCLUDED.correct_count+5.0)/"
                "(q15_prediction_feature_stats.n+1+10.0))-0.5)*10.0)), updated_at=NOW()",
                (key, 1 if correct else 0, cap, cap),
            )

    # ----------------------------- persistence ------------------------------
    def _migrate(self) -> None:
        if self._migration_attempted:
            return
        self._migration_attempted = True
        if not self.store or not getattr(self.store, "enabled", False) or not hasattr(self.store, "execute"):
            return
        statements = [
            """
            CREATE TABLE IF NOT EXISTS q15_prediction_checkpoints (
                prediction_key TEXT PRIMARY KEY,
                ticker TEXT NOT NULL,
                asset TEXT NOT NULL,
                market_close TIMESTAMPTZ,
                checkpoint TEXT NOT NULL,
                captured_at TIMESTAMPTZ NOT NULL,
                seconds_remaining DOUBLE PRECISION,
                predicted_side TEXT NOT NULL,
                raw_yes_probability DOUBLE PRECISION,
                adjusted_yes_probability DOUBLE PRECISION,
                side_probability DOUBLE PRECISION,
                confidence DOUBLE PRECISION,
                decisiveness DOUBLE PRECISION,
                consensus DOUBLE PRECISION,
                required_consensus DOUBLE PRECISION,
                required_side_probability DOUBLE PRECISION,
                sample_count INTEGER,
                feature_snapshot JSONB,
                feature_tags JSONB,
                trade_plan JSONB,
                learned_adjustment DOUBLE PRECISION,
                model_version TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                shadow BOOLEAN NOT NULL DEFAULT TRUE,
                outcome TEXT,
                correct BOOLEAN,
                graded_at TIMESTAMPTZ,
                UNIQUE(ticker, checkpoint)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS q15_prediction_grades (
                ticker TEXT PRIMARY KEY,
                asset TEXT NOT NULL,
                market_close TIMESTAMPTZ,
                actual_result TEXT NOT NULL,
                prediction_15m TEXT,
                prediction_10m TEXT,
                correct_15m BOOLEAN,
                correct_10m BOOLEAN,
                agreement TEXT,
                final_result TEXT,
                conflict_classification TEXT,
                feature_changes JSONB,
                aligned_notes JSONB,
                misleading_notes JSONB,
                model_version TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                graded_at TIMESTAMPTZ NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS q15_prediction_feature_stats (
                feature_key TEXT PRIMARY KEY,
                n INTEGER NOT NULL DEFAULT 0,
                correct_count INTEGER NOT NULL DEFAULT 0,
                adjustment_pp DOUBLE PRECISION NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            SCALP_SCHEMA,
            "ALTER TABLE q15_prediction_checkpoints ADD COLUMN IF NOT EXISTS required_consensus DOUBLE PRECISION",
            "ALTER TABLE q15_prediction_checkpoints ADD COLUMN IF NOT EXISTS required_side_probability DOUBLE PRECISION",
        ]
        try:
            for statement in statements:
                if not self.store.execute(statement):
                    raise RuntimeError("database migration statement returned false")
            self._db_ready = True
        except Exception as exc:
            self._last_error = f"two-window migration failed: {exc}"
            logger.warning(self._last_error)

    def _persist_prediction(self, cycle: Mapping[str, Any], prediction: Mapping[str, Any]) -> None:
        if not self._db_ready:
            return
        key = _hash(cycle.get("ticker"), prediction.get("checkpoint"), POLICY_VERSION, SCHEMA_VERSION)
        self._execute(
            "INSERT INTO q15_prediction_checkpoints (prediction_key,ticker,asset,market_close,checkpoint,captured_at,"
            "seconds_remaining,predicted_side,raw_yes_probability,adjusted_yes_probability,side_probability,confidence,"
            "decisiveness,consensus,required_consensus,required_side_probability,sample_count,feature_snapshot,feature_tags,trade_plan,learned_adjustment,model_version,"
            "policy_version,schema_version,shadow) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,"
            "%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s) ON CONFLICT(ticker,checkpoint) DO NOTHING",
            (
                key, cycle.get("ticker"), cycle.get("asset"), cycle.get("close_time"), prediction.get("checkpoint"),
                prediction.get("captured_at"), prediction.get("seconds_remaining"), prediction.get("side"),
                prediction.get("raw_yes_probability"), prediction.get("yes_probability"), prediction.get("side_probability"),
                prediction.get("confidence"), prediction.get("decisiveness"), prediction.get("consensus"),
                prediction.get("required_consensus"), prediction.get("required_side_probability"),
                prediction.get("sample_count"), _json(prediction.get("features") or {}), _json(prediction.get("feature_tags") or []),
                _json(prediction.get("trade_plan") or {}), prediction.get("learned_adjustment"), MODEL_VERSION, POLICY_VERSION,
                SCHEMA_VERSION, bool(prediction.get("shadow", True)),
            ),
        )

    def _persist_grade(self, grade: Mapping[str, Any]) -> None:
        if not self._db_ready:
            return
        self._execute(
            "INSERT INTO q15_prediction_grades (ticker,asset,market_close,actual_result,prediction_15m,prediction_10m,"
            "correct_15m,correct_10m,agreement,final_result,conflict_classification,feature_changes,aligned_notes,"
            "misleading_notes,model_version,policy_version,schema_version,graded_at) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s,%s) "
            "ON CONFLICT(ticker) DO NOTHING",
            (
                grade.get("ticker"), grade.get("asset"), grade.get("market_close"), grade.get("actual_result"),
                grade.get("prediction_15m"), grade.get("prediction_10m"), grade.get("correct_15m"),
                grade.get("correct_10m"), grade.get("agreement"), grade.get("final_result"),
                grade.get("conflict_classification"), _json(grade.get("feature_changes") or {}),
                _json(grade.get("aligned_notes") or []), _json(grade.get("misleading_notes") or []), MODEL_VERSION,
                POLICY_VERSION, SCHEMA_VERSION, grade.get("graded_at"),
            ),
        )

    def _hydrate_cycle(self, cycle: MutableMapping[str, Any]) -> None:
        if not self._db_ready:
            return
        try:
            rows = self._query(
                "SELECT checkpoint,predicted_side,raw_yes_probability,adjusted_yes_probability,side_probability,"
                "confidence,decisiveness,consensus,required_consensus,required_side_probability,sample_count,feature_snapshot,feature_tags,trade_plan,learned_adjustment,"
                "captured_at,seconds_remaining,outcome,correct FROM q15_prediction_checkpoints WHERE ticker=%s",
                (cycle.get("ticker"),),
            )
            for row in rows:
                checkpoint = row.get("checkpoint")
                if checkpoint not in {"15m", "10m"}:
                    continue
                cycle["predictions"][checkpoint] = {
                    "checkpoint": checkpoint,
                    "side": row.get("predicted_side"),
                    "raw_yes_probability": row.get("raw_yes_probability"),
                    "yes_probability": row.get("adjusted_yes_probability"),
                    "side_probability": row.get("side_probability"),
                    "confidence": row.get("confidence"),
                    "decisiveness": row.get("decisiveness"),
                    "consensus": row.get("consensus"),
                    "required_consensus": row.get("required_consensus"),
                    "required_side_probability": row.get("required_side_probability"),
                    "sample_count": row.get("sample_count"),
                    "features": row.get("feature_snapshot") or {},
                    "feature_tags": row.get("feature_tags") or [],
                    "trade_plan": row.get("trade_plan") or {},
                    "learned_adjustment": row.get("learned_adjustment"),
                    "captured_at": str(row.get("captured_at")),
                    "seconds_remaining": row.get("seconds_remaining"),
                    "outcome": row.get("outcome"),
                    "correct": row.get("correct"),
                    "model_version": MODEL_VERSION,
                    "policy_version": POLICY_VERSION,
                    "schema_version": SCHEMA_VERSION,
                }
            cycle["final_side"], cycle["agreement"] = self._final(cycle["predictions"].get("15m"), cycle["predictions"].get("10m"))
            if any(row.get("outcome") for row in rows):
                cycle["graded"] = True
        except Exception as exc:
            self._last_error = f"prediction hydration failed: {exc}"

    def _query(self, sql: str, params: Optional[tuple] = None) -> List[dict]:
        if not self.store or not hasattr(self.store, "query"):
            return []
        return self.store.query(sql, params or ()) or []

    def _execute(self, sql: str, params: Optional[tuple] = None) -> bool:
        if not self.store or not hasattr(self.store, "execute"):
            return False
        return bool(self.store.execute(sql, params or ()))

    def _claim(self, key: str) -> bool:
        if key in self._local_claims:
            return False
        if self.store is not None and hasattr(self.store, "claim_event"):
            try:
                if not self.store.claim_event(key):
                    return False
            except Exception as exc:
                # Fail CLOSED: if we cannot coordinate via the shared store we
                # must not claim locally and send, or two processes (dev+prod)
                # both racing a transient store outage would each deliver the
                # same alert. Skip this cycle; we retry once the store recovers.
                logger.error("claim store error for key=%s: %s", key, exc)
                return False
        self._local_claims.add(key)
        return True

    def _release(self, key: str) -> None:
        """Undo a *local* claim so a failed delivery is retried next cycle.

        The shared store's claim ledger is permanent by design, so this only
        re-opens retries within this process (the common single-process case);
        against a shared store the next attempt sees the key already claimed and
        is treated as already-delivered.
        """
        self._local_claims.discard(key)

    def _claim_and_send(self, key: str, message: str) -> str:
        """Claim ``key`` then deliver ``message``.

        Returns one of:
          ``"sent"``      – we won the claim and delivery was accepted;
          ``"duplicate"`` – claim lost (already delivered by a peer / restart);
          ``"failed"``    – we won the claim but delivery failed. With
                            ``alert_retry_on_send_failure`` the local claim is
                            released so a later cycle retries; otherwise the
                            claim is consumed (legacy behavior) and ``"sent"``
                            is returned so callers advance as before.
        """
        if not self._claim(key):
            return "duplicate"
        delivered = True
        if self.notifier is not None:
            try:
                delivered = bool(self.notifier.send(message))
            except Exception as exc:
                logger.error("alert send raised for key=%s: %s", key, exc)
                delivered = False
        if delivered:
            return "sent"
        if self.settings.alert_retry_on_send_failure:
            self._release(key)
            logger.warning("alert delivery failed for key=%s; will retry", key)
            return "failed"
        logger.warning("alert delivery failed for key=%s; not retried", key)
        return "sent"

    @staticmethod
    def _public_cycle(cycle: Mapping[str, Any]) -> dict:
        return {
            "ticker": cycle.get("ticker"),
            "asset": cycle.get("asset"),
            "close_time": cycle.get("close_time"),
            "prediction_15m": cycle.get("predictions", {}).get("15m"),
            "prediction_10m": cycle.get("predictions", {}).get("10m"),
            "agreement": cycle.get("agreement"),
            "decision_state": cycle.get("decision_state") or "PENDING",
            "late_strong_shadow": cycle.get("late_strong_shadow"),
            "final_result": cycle.get("final_side") or "UNCERTAIN",
            "graded": bool(cycle.get("graded")),
            "actual_result": cycle.get("actual_result"),
            "grade": cycle.get("grade"),
        }


# ----------------------------- separate scalp learning ----------------------
SCALP_SCHEMA = """
CREATE TABLE IF NOT EXISTS q15_scalp_decision_events (
    scalp_key TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    asset TEXT NOT NULL,
    side TEXT NOT NULL,
    opened_at TIMESTAMPTZ NOT NULL,
    market_close TIMESTAMPTZ,
    entry_price DOUBLE PRECISION,
    target_price DOUBLE PRECISION,
    stop_price DOUBLE PRECISION,
    target_probability DOUBLE PRECISION,
    stop_probability DOUBLE PRECISION,
    timeout_probability DOUBLE PRECISION,
    expected_net_ev_cents DOUBLE PRECISION,
    feature_snapshot JSONB,
    model_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    shadow BOOLEAN NOT NULL DEFAULT TRUE,
    exit_at TIMESTAMPTZ,
    exit_price DOUBLE PRECISION,
    exit_reason TEXT,
    target_hit BOOLEAN,
    stop_hit BOOLEAN,
    censored_timeout BOOLEAN,
    realized_net_pnl DOUBLE PRECISION
)
"""


def assess_scalp_candidate(*, target_net_cents: Any, spread: Any, reversal_score: Any, observed_extension: Any,
                           time_remaining: Any, entry_price: Any, stop_price: Any, estimated_fee: Any) -> dict:
    target = max(10.0, _num(target_net_cents, 10.0) or 10.0)
    spread_value = max(0.0, _num(spread, 99.0) or 99.0)
    reversal = _num(reversal_score, 0.0) or 0.0
    extension = max(0.0, _num(observed_extension, 0.0) or 0.0)
    seconds = max(0.0, _num(time_remaining, 0.0) or 0.0)
    entry = _num(entry_price, 50.0) or 50.0
    stop = _num(stop_price, max(1.0, entry - 6.0)) or max(1.0, entry - 6.0)
    fee = max(0.0, _num(estimated_fee, 0.0) or 0.0)
    linear = -0.55 + 0.035 * (reversal - 70.0) + 0.055 * (extension - target) + 0.002 * max(0.0, seconds - 60.0) - 0.28 * spread_value
    target_probability = _clamp(_sigmoid(linear), 0.05, 0.95)
    timeout_probability = _clamp(0.18 - 0.001 * max(0.0, seconds - 60.0), 0.04, 0.25)
    stop_probability = _clamp(1.0 - target_probability - timeout_probability, 0.02, 0.93)
    stop_loss = max(1.0, entry - stop) + fee
    slippage = max(0.1, spread_value * 0.25)
    expected_ev = target_probability * target - stop_probability * stop_loss - slippage
    min_probability = _float("Q15_SCALP_MIN_TARGET_PROBABILITY", 0.62)
    min_ev = _float("Q15_SCALP_MIN_EXPECTED_EV_CENTS", 1.0)
    qualifies = bool(target >= 10.0 and target_probability >= min_probability and expected_ev >= min_ev)
    return {
        "qualifies": qualifies,
        "target_probability": round(target_probability, 4),
        "stop_probability": round(stop_probability, 4),
        "timeout_probability": round(timeout_probability, 4),
        "expected_net_ev_cents": round(expected_ev, 3),
        "predicted_seconds_to_target": round(max(5.0, target / max(0.1, extension) * 30.0), 1),
        "model_version": "q15-scalp-target-before-stop-v1",
        "warning": "A 10-cent move is a probabilistic target, not a guarantee.",
    }


def ensure_scalp_schema(store) -> bool:
    if not store or not getattr(store, "enabled", False) or not hasattr(store, "execute"):
        return False
    try:
        return bool(store.execute(SCALP_SCHEMA))
    except Exception:
        return False


def record_scalp_open_event(store, sig: MutableMapping[str, Any], now: float) -> None:
    if not ensure_scalp_schema(store):
        return
    assessment = sig.get("learning_assessment") or {}
    key = sig.get("learning_scalp_key") or _hash(sig.get("ticker"), sig.get("side"), now, POLICY_VERSION)
    sig["learning_scalp_key"] = key
    features = sig.get("learning_features") or {
        "target_net_cents": sig.get("target_net_cents"),
        "spread": sig.get("spread"),
        "reversal_score": sig.get("rc"),
        "observed_extension": sig.get("observed_extension"),
        "time_remaining": sig.get("secs"),
        "estimated_fee": sig.get("fee"),
    }
    try:
        store.execute(
            "INSERT INTO q15_scalp_decision_events (scalp_key,ticker,asset,side,opened_at,market_close,entry_price,target_price,"
            "stop_price,target_probability,stop_probability,timeout_probability,expected_net_ev_cents,feature_snapshot,model_version,"
            "policy_version,schema_version,shadow) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s) "
            "ON CONFLICT(scalp_key) DO NOTHING",
            (key, sig.get("ticker"), sig.get("asset"), sig.get("side"), _iso(now), sig.get("market_close"),
             sig.get("entry_exec") or sig.get("entry"), sig.get("target"), sig.get("stop"),
             assessment.get("target_probability"), assessment.get("stop_probability"), assessment.get("timeout_probability"),
             assessment.get("expected_net_ev_cents"), _json(features), assessment.get("model_version") or "q15-scalp-target-before-stop-v1",
             POLICY_VERSION, SCHEMA_VERSION, True),
        )
    except Exception as exc:
        logger.warning("record scalp learning event failed: %s", exc)


def settle_scalp_event(store, sig: Mapping[str, Any], now: float, exit_price: Any, exit_reason: Any, realized: Any) -> None:
    key = sig.get("learning_scalp_key")
    if not key or not store or not hasattr(store, "execute"):
        return
    reason = str(exit_reason or "")
    try:
        store.execute(
            "UPDATE q15_scalp_decision_events SET exit_at=%s,exit_price=%s,exit_reason=%s,target_hit=%s,stop_hit=%s,"
            "censored_timeout=%s,realized_net_pnl=%s WHERE scalp_key=%s",
            (_iso(now), exit_price, reason, reason == "take_profit", reason == "stop", reason in {"rollover", "exit_before_close", "timeout"}, realized, key),
        )
    except Exception as exc:
        logger.warning("settle scalp learning event failed: %s", exc)
