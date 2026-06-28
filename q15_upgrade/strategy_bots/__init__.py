"""Asset-specific filtered strategy bots.

These bots are tracking-only by default. They sit beside the existing V2 and
High Vol Flip ledgers so fresh cycles can compare current behavior against the
new BNB/HYPE/MoreFire confirmation filters without changing alert delivery.
"""

from .rules import STRATEGY_VERSION

__all__ = ["STRATEGY_VERSION"]
