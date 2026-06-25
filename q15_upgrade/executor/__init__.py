"""Ultoim V2 EXECUTOR — opt-in live-order layer on top of the read-only v2 paper system.

DEFAULT-OFF and DRY-RUN-by-default. Nothing here can place a real order unless the
operator sets Q15_EXEC_ENABLED=true AND Q15_EXEC_DRY_RUN=false AND provides Kalshi
keys AND leaves the kill switch off. v2 itself remains read-only; this is the only
package that talks to the trading endpoints.
"""
from .config import ExecutorConfig, yes_config_from_env
from .executor import Executor, get_executor, get_yes_executor, reset_executor_for_tests
from .risk import Pick, PortfolioState, Decision, decide, apply_fill

__all__ = [
    "ExecutorConfig", "yes_config_from_env", "Executor", "get_executor",
    "get_yes_executor", "reset_executor_for_tests",
    "Pick", "PortfolioState", "Decision", "decide", "apply_fill",
]
