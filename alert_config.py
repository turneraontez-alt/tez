"""Configurable thresholds for the entry-alert engine.

Every trading threshold is a typed default that can be overridden by an
environment variable, so nothing is hard-coded inside the signal logic.
Edge is expressed in cents, which on Kalshi equals percentage points of
probability (1c = 1pp).
"""
import os


def _f(env, default):
    try:
        return float(os.environ.get(env, default))
    except (TypeError, ValueError):
        return float(default)


def _i(env, default):
    try:
        return int(float(os.environ.get(env, default)))
    except (TypeError, ValueError):
        return int(default)


def _b(env, default):
    v = os.environ.get(env)
    if v is None:
        return bool(default)
    return v.strip().lower() in ("1", "true", "yes", "on")


class AlertConfig:
    def __init__(self):
        # Watch is an early heads-up; entry needs a richer, time-tiered edge.
        self.watch_min_edge = _f("ALERT_WATCH_MIN_EDGE", 3.0)
        self.entry_min_edge_default = _f("ALERT_ENTRY_MIN_EDGE", 5.0)
        # Don't CONFIRM a buy unless the model's side win-probability clears this
        # floor. The fair-prob model is overconfident, so sub-coin-flip "value"
        # longshots (win_prob < ~0.5) almost never win and were the main loss
        # driver. Tune via ENTRY_MIN_WIN_PROB; lower = more (riskier) entries.
        self.entry_min_win_prob = _f("ENTRY_MIN_WIN_PROB", 0.55)

        # Time-tiered minimum edge (cents == pp). More time -> demand more edge
        # because the estimate has longer to be wrong; the final 2 min demand
        # extra edge AND a settlement-feed confirmation.
        self.edge_tier_12_15m = _f("ALERT_EDGE_12_15M", 8.0)   # secs >= 720
        self.edge_tier_6_12m = _f("ALERT_EDGE_6_12M", 6.0)     # 360 <= secs < 720
        self.edge_tier_2_6m = _f("ALERT_EDGE_2_6M", 5.0)       # 120 <= secs < 360
        self.edge_tier_under_2m = _f("ALERT_EDGE_UNDER_2M", 7.0)  # secs < 120

        # Spread cap raised to 3c (real 15-min crypto markets commonly quote 3c)
        # but a wide spread (>2c) must be paid for with extra edge and deeper
        # book, so we never chase an illiquid quote.
        self.max_spread_cents = _f("ALERT_MAX_SPREAD", 3.0)
        self.spread_penalty_per_cent = _f("ALERT_SPREAD_PENALTY_PER_CENT", 1.0)
        self.wide_spread_min_depth = _f("ALERT_WIDE_SPREAD_MIN_DEPTH", 30.0)
        self.max_data_age_s = _f("ALERT_MAX_DATA_AGE_S", 3.0)
        self.min_confirmations = _i("ALERT_MIN_CONFIRMATIONS", 3)
        self.persistence_seconds = _f("ALERT_PERSISTENCE_SECONDS", 5.0)
        # Tolerate brief single-cycle gate flicker so normal data jitter doesn't
        # keep resetting the persistence clock and starve ENTRY CONFIRMED.
        self.persistence_grace_seconds = _f("ALERT_PERSISTENCE_GRACE_S", 3.0)
        self.cooldown_after_invalidation_s = _f("ALERT_INVALIDATION_COOLDOWN_S", 30.0)
        self.repeat_edge_improvement = _f("ALERT_REPEAT_EDGE_IMPROVEMENT", 2.0)
        self.min_depth_contracts = _f("ALERT_MIN_DEPTH_CONTRACTS", 20.0)
        self.under_2min_requires_settlement = _b("ALERT_UNDER_2MIN_REQUIRES_SETTLEMENT", True)

        # --- Learning loop (learns 24/7 from settled outcomes) ---
        self.learning_enabled = _b("LEARNING_ENABLED", True)
        self.learning_recompute_seconds = _f("LEARNING_RECOMPUTE_S", 300.0)
        self.learn_min_n = _i("LEARNING_MIN_N", 20)
        self.learn_strong_n = _i("LEARNING_STRONG_N", 40)
        self.learn_suppress_n = _i("LEARNING_SUPPRESS_N", 60)
        self.learn_prior_strength = _f("LEARNING_PRIOR_STRENGTH", 20.0)
        self.learn_gap_small = _f("LEARNING_GAP_SMALL", 0.08)
        self.learn_gap_large = _f("LEARNING_GAP_LARGE", 0.12)
        self.learn_suppress_winrate = _f("LEARNING_SUPPRESS_WINRATE", 0.45)
        self.learn_max_adjustment = _f("LEARNING_MAX_ADJUSTMENT", 4.0)

        # --- Hourly performance + mistake report ---
        self.hourly_report_enabled = _b("HOURLY_REPORT_ENABLED", True)
        self.report_interval_seconds = _f("REPORT_INTERVAL_S", 3600.0)

        # --- Scalp (short-term bounce) predictions ---
        self.scalp_enabled = _b("SCALP_ENABLED", True)
        self.scalp_min_move_cents = max(12.0, _f("SCALP_MIN_MOVE_CENTS", 12.0))
        self.scalp_target_cents = max(10.0, _f("SCALP_TARGET_CENTS", 10.0))
        # Min reversal tick off the extreme to confirm the snap-back has begun.
        self.scalp_min_reversal_cents = _f("SCALP_MIN_REVERSAL_CENTS", 2.0)
        self.scalp_cooldown_s = _f("SCALP_COOLDOWN_S", 180.0)
        self.scalp_min_secs = _f("SCALP_MIN_SECS", 60.0)
        self.scalp_max_secs = _f("SCALP_MAX_SECS", 840.0)
        self.scalp_max_spread = _f("SCALP_MAX_SPREAD", 3.0)
        self.scalp_min_trades_15s = _f("SCALP_MIN_TRADES_15S", 3.0)
        # Only emit a scalp when reversal confidence clears this (user: >70%).
        self.scalp_min_confidence = max(80.0, _f("SCALP_MIN_CONFIDENCE", 80.0))
        # Min seconds a scalp is held before a stop can resolve it as a loss, so
        # a first-cycle wobble can't cut it instantly — give it time to work.
        self.scalp_min_hold_secs = max(0.0, _f("SCALP_MIN_HOLD_SECS", 0.0))
        self.scalp_actionable_alerts = _b("SCALP_ACTIONABLE_ALERTS", False)
        self.scalp_min_settled_for_actionable = _i("SCALP_MIN_SETTLED_FOR_ACTIONABLE", 30)

        # Correlation control: when correlated assets move together, alert only
        # the strongest and name the next as an alternative.
        self.correlation_enabled = _b("ALERT_CORRELATION_ENABLED", True)

        # Master switch + presentation.
        self.alerts_enabled = _b("ALERTS_ENABLED", True)
        self.dashboard_url = os.environ.get(
            "DASHBOARD_URL", "https://phone-dashboard.replit.app"
        )

        # --- Kalshi WebSocket real-time source (default OFF until credentials set) ---
        self.ws_enabled = _b("KALSHI_WS_ENABLED", False)
        self.ws_url = os.environ.get(
            "KALSHI_WS_URL", "wss://api.elections.kalshi.com/trade-api/ws/v2"
        )
        self.ws_max_data_age_s = _f("KALSHI_WS_MAX_DATA_AGE_S", 5.0)

    def min_edge_for(self, secs_remaining):
        """Time-tiered minimum edge (cents/pp) required for an ENTRY signal."""
        if secs_remaining is None:
            return self.entry_min_edge_default
        if secs_remaining >= 720:
            return self.edge_tier_12_15m
        if secs_remaining >= 360:
            return self.edge_tier_6_12m
        if secs_remaining >= 120:
            return self.edge_tier_2_6m
        return self.edge_tier_under_2m

    def as_dict(self):
        """Non-secret config snapshot for /api/health and debugging."""
        return {
            "watch_min_edge": self.watch_min_edge,
            "entry_min_edge_default": self.entry_min_edge_default,
            "edge_tiers": {
                "12-15m": self.edge_tier_12_15m,
                "6-12m": self.edge_tier_6_12m,
                "2-6m": self.edge_tier_2_6m,
                "under_2m": self.edge_tier_under_2m,
            },
            "max_spread_cents": self.max_spread_cents,
            "spread_penalty_per_cent": self.spread_penalty_per_cent,
            "wide_spread_min_depth": self.wide_spread_min_depth,
            "max_data_age_s": self.max_data_age_s,
            "min_confirmations": self.min_confirmations,
            "persistence_seconds": self.persistence_seconds,
            "persistence_grace_seconds": self.persistence_grace_seconds,
            "cooldown_after_invalidation_s": self.cooldown_after_invalidation_s,
            "repeat_edge_improvement": self.repeat_edge_improvement,
            "min_depth_contracts": self.min_depth_contracts,
            "under_2min_requires_settlement": self.under_2min_requires_settlement,
            "learning_enabled": self.learning_enabled,
            "hourly_report_enabled": self.hourly_report_enabled,
            "scalp_enabled": self.scalp_enabled,
            "correlation_enabled": self.correlation_enabled,
            "alerts_enabled": self.alerts_enabled,
            "ws_enabled": self.ws_enabled,
        }
