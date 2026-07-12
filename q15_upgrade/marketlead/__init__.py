"""Q15 MarketLead prospective market-evidence collector."""
from .config import MarketLeadConfig
from .features import MarketLeadFeatureEngine
from .runner import MarketLeadRunner, get_runner, reset_runner

SYSTEM_NAME = "Q15 MarketLead"
SYSTEM_VERSION = "q15-marketlead-v1"

__all__ = [
    "MarketLeadConfig",
    "MarketLeadFeatureEngine",
    "MarketLeadRunner",
    "SYSTEM_NAME",
    "SYSTEM_VERSION",
    "get_runner",
    "reset_runner",
]
