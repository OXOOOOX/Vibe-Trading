"""Unified, policy-driven access to market and research data.

The data layer deliberately does not replace the market cache or Loader Cache.
It is the single policy surface that decides when each is used and what a
research agent is allowed to receive.
"""

from .service import UnifiedDataService, get_unified_data_service
from .prewarm import DataPrewarmScheduler, get_data_prewarm_scheduler

__all__ = ["UnifiedDataService", "get_unified_data_service", "DataPrewarmScheduler", "get_data_prewarm_scheduler"]
