"""TT-Edge — TT Elite Series betting ANALYSIS pipeline (read-only).

Analysis and alerting only. This package finds prices and sends Telegram
alerts; a human clicks buttons. Nothing in here places, modifies, or cancels
a bet, and it must stay that way (see tt_edge/README.md, including the kill
criteria written there).

Layout mirrors the build spec:

* ``scrapers`` — sofascore board / H2H / form payload parsers plus the
  Playwright XHR-intercept fetcher (lazy import; parsers are pure).
* ``model``    — features -> logistic blend -> Platt calibration (versioned).
* ``edge``     — de-vig, THE single edge calculation, fractional-Kelly staking.
* ``alerts``   — Telegram adapter on the shared Q15 send client.
* ``db``       — portable schema (PostgreSQL or SQLite) + repository.
* ``jobs``     — scan / grade / odds_entry / bankroll CLIs.
"""

__all__ = ["config", "freshness"]
