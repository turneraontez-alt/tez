"""TT-Edge configuration — every knob is env-driven (``TT_EDGE_*``), nothing
is hardcoded at call sites.

Reads happen at :func:`load_config` time (not import time) so tests can
monkeypatch the environment. Each variable is read exactly once here with a
literal name so ``tools/config_audit.py`` inventories it; document every name
in ``.env.example``.

Money and probability thresholds are :class:`~decimal.Decimal` — prices are
money (see ENGINEERING_GUIDELINES.md). Model-internal weights stay float (they
feed a sigmoid, not a ledger).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


class ConfigError(ValueError):
    """An environment value could not be parsed for its declared type."""


def _dec(name: str, raw: str | None, default: str) -> Decimal:
    text = default if raw is None or raw.strip() == "" else raw.strip()
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ConfigError(f"{name}={text!r} is not a valid decimal") from exc


def _int(name: str, raw: str | None, default: int) -> int:
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not a valid integer") from exc


def _float(name: str, raw: str | None, default: float) -> float:
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not a valid float") from exc


def _bool(raw: str | None, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class TTEdgeConfig:
    """Immutable snapshot of every TT-Edge tunable."""

    # Storage. sqlite path (default) or a postgres:// / postgresql:// URL.
    database_url: str

    # Freshness guards (seconds). Stale data was the manual-mode failure
    # mode — these are hard rejects, not warnings.
    max_board_age_s: int
    max_h2h_age_s: int
    max_form_age_s: int
    max_odds_age_s: int

    # Edge thresholds (probability points, as Decimal).
    min_edge: Decimal        # alert only at/above this edge
    no_edge_band: Decimal    # |model - fair| inside this band = "no edge"

    # Staking (quarter-Kelly, capped, floored; bankroll lives in the DB).
    kelly_fraction: Decimal
    kelly_cap_pct: Decimal
    stake_floor_cents: int
    max_open_recommendations: int

    # Fix-risk guard: adverse line movement (American cents) that hard-passes.
    fix_risk_cents: int

    # Model blend (hand-set until >= calibration_min_rows graded matches).
    w_h2h: float
    w_form: float
    w_common: float
    blend_scale: float
    h2h_half_life_days: float
    common_opp_window_days: int
    form_last_n: int
    form_hot_n: int

    # Abstain data minimums.
    min_h2h_meetings: int
    min_common_opps: int

    # Calibration.
    calibration_min_rows: int

    # Board filter.
    tournament_keyword: str

    # Telegram (token/chat unset => muted, alerts logged but not sent).
    telegram_token: str | None
    telegram_chat_id: str
    telegram_enabled: bool


def load_config() -> TTEdgeConfig:
    """Build the config snapshot from the environment (validated)."""
    cfg = TTEdgeConfig(
        database_url=os.environ.get("TT_EDGE_DATABASE_URL", "data/tt_edge.sqlite3"),
        max_board_age_s=_int("TT_EDGE_MAX_BOARD_AGE_S",
                             os.environ.get("TT_EDGE_MAX_BOARD_AGE_S"), 1800),
        max_h2h_age_s=_int("TT_EDGE_MAX_H2H_AGE_S",
                           os.environ.get("TT_EDGE_MAX_H2H_AGE_S"), 86400),
        max_form_age_s=_int("TT_EDGE_MAX_FORM_AGE_S",
                            os.environ.get("TT_EDGE_MAX_FORM_AGE_S"), 86400),
        max_odds_age_s=_int("TT_EDGE_MAX_ODDS_AGE_S",
                            os.environ.get("TT_EDGE_MAX_ODDS_AGE_S"), 600),
        min_edge=_dec("TT_EDGE_MIN_EDGE",
                      os.environ.get("TT_EDGE_MIN_EDGE"), "0.05"),
        no_edge_band=_dec("TT_EDGE_NO_EDGE_BAND",
                          os.environ.get("TT_EDGE_NO_EDGE_BAND"), "0.03"),
        kelly_fraction=_dec("TT_EDGE_KELLY_FRACTION",
                            os.environ.get("TT_EDGE_KELLY_FRACTION"), "0.25"),
        kelly_cap_pct=_dec("TT_EDGE_KELLY_CAP_PCT",
                           os.environ.get("TT_EDGE_KELLY_CAP_PCT"), "0.05"),
        stake_floor_cents=_int("TT_EDGE_STAKE_FLOOR_CENTS",
                               os.environ.get("TT_EDGE_STAKE_FLOOR_CENTS"), 100),
        max_open_recommendations=_int(
            "TT_EDGE_MAX_OPEN_RECS",
            os.environ.get("TT_EDGE_MAX_OPEN_RECS"), 3),
        fix_risk_cents=_int("TT_EDGE_FIX_RISK_CENTS",
                            os.environ.get("TT_EDGE_FIX_RISK_CENTS"), 15),
        w_h2h=_float("TT_EDGE_W_H2H", os.environ.get("TT_EDGE_W_H2H"), 0.45),
        w_form=_float("TT_EDGE_W_FORM", os.environ.get("TT_EDGE_W_FORM"), 0.35),
        w_common=_float("TT_EDGE_W_COMMON",
                        os.environ.get("TT_EDGE_W_COMMON"), 0.20),
        blend_scale=_float("TT_EDGE_BLEND_SCALE",
                           os.environ.get("TT_EDGE_BLEND_SCALE"), 2.5),
        h2h_half_life_days=_float("TT_EDGE_H2H_HALF_LIFE_DAYS",
                                  os.environ.get("TT_EDGE_H2H_HALF_LIFE_DAYS"),
                                  365.0),
        common_opp_window_days=_int(
            "TT_EDGE_COMMON_OPP_WINDOW_DAYS",
            os.environ.get("TT_EDGE_COMMON_OPP_WINDOW_DAYS"), 60),
        form_last_n=_int("TT_EDGE_FORM_LAST_N",
                         os.environ.get("TT_EDGE_FORM_LAST_N"), 15),
        form_hot_n=_int("TT_EDGE_FORM_HOT_N",
                        os.environ.get("TT_EDGE_FORM_HOT_N"), 5),
        min_h2h_meetings=_int("TT_EDGE_MIN_H2H_MEETINGS",
                              os.environ.get("TT_EDGE_MIN_H2H_MEETINGS"), 5),
        min_common_opps=_int("TT_EDGE_MIN_COMMON_OPPS",
                             os.environ.get("TT_EDGE_MIN_COMMON_OPPS"), 3),
        calibration_min_rows=_int(
            "TT_EDGE_CALIBRATION_MIN_ROWS",
            os.environ.get("TT_EDGE_CALIBRATION_MIN_ROWS"), 300),
        tournament_keyword=os.environ.get("TT_EDGE_TOURNAMENT_KEYWORD",
                                          "TT Elite"),
        telegram_token=os.environ.get("TT_EDGE_TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.environ.get("TT_EDGE_TELEGRAM_CHAT_ID", ""),
        telegram_enabled=_bool(os.environ.get("TT_EDGE_TELEGRAM_ENABLED"), True),
    )
    _validate(cfg)
    return cfg


def _validate(cfg: TTEdgeConfig) -> None:
    if cfg.min_edge <= 0 or cfg.min_edge >= 1:
        raise ConfigError(f"TT_EDGE_MIN_EDGE must be in (0, 1); got {cfg.min_edge}")
    if cfg.no_edge_band < 0 or cfg.no_edge_band > cfg.min_edge:
        raise ConfigError(
            "TT_EDGE_NO_EDGE_BAND must be in [0, TT_EDGE_MIN_EDGE]; got "
            f"{cfg.no_edge_band} vs {cfg.min_edge}")
    if not (Decimal("0") < cfg.kelly_fraction <= Decimal("1")):
        raise ConfigError(
            f"TT_EDGE_KELLY_FRACTION must be in (0, 1]; got {cfg.kelly_fraction}")
    if not (Decimal("0") < cfg.kelly_cap_pct <= Decimal("1")):
        raise ConfigError(
            f"TT_EDGE_KELLY_CAP_PCT must be in (0, 1]; got {cfg.kelly_cap_pct}")
    if cfg.stake_floor_cents < 0:
        raise ConfigError("TT_EDGE_STAKE_FLOOR_CENTS must be >= 0")
    if cfg.max_open_recommendations < 1:
        raise ConfigError("TT_EDGE_MAX_OPEN_RECS must be >= 1")
    if cfg.fix_risk_cents < 1:
        raise ConfigError("TT_EDGE_FIX_RISK_CENTS must be >= 1")
    if min(cfg.w_h2h, cfg.w_form, cfg.w_common) < 0 or \
            (cfg.w_h2h + cfg.w_form + cfg.w_common) <= 0:
        raise ConfigError("model weights must be >= 0 with a positive sum")
    if cfg.form_hot_n > cfg.form_last_n:
        raise ConfigError("TT_EDGE_FORM_HOT_N cannot exceed TT_EDGE_FORM_LAST_N")
    for name, value in (("TT_EDGE_MAX_BOARD_AGE_S", cfg.max_board_age_s),
                        ("TT_EDGE_MAX_H2H_AGE_S", cfg.max_h2h_age_s),
                        ("TT_EDGE_MAX_FORM_AGE_S", cfg.max_form_age_s),
                        ("TT_EDGE_MAX_ODDS_AGE_S", cfg.max_odds_age_s)):
        if value < 1:
            raise ConfigError(f"{name} must be >= 1 second")
