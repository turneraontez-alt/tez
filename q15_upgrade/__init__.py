"""Q15 crypto monitor upgrade package.

Read-only analytics and alerting only.  Nothing in this package submits orders.
"""

from .runtime import Q15Runtime
from .signals import SignalEngine
from .learning import LearningEngine

__all__ = ["Q15Runtime", "SignalEngine", "LearningEngine"]
__version__ = "2.0.0"
