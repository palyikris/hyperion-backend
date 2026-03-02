"""
Stats utilities package.

Re-exports cache and KPI functions for convenient importing.
"""

# Cache
from app.api.stats_utils.cache import get_cached_stats, cache_stats, clear_cache

# KPIs
from app.api.stats_utils.kpi_trash_composition import get_trash_composition
from app.api.stats_utils.kpi_environmental_footprint import get_environmental_footprint
from app.api.stats_utils.kpi_fleet_efficiency import get_ai_fleet_efficiency
from app.api.stats_utils.kpi_temporal_trends import get_temporal_trends
from app.api.stats_utils.kpi_processing_time import get_mean_time_to_process
from app.api.stats_utils.kpi_hotspot_density import get_hotspot_density
from app.api.stats_utils.fun_facts import get_fun_facts

__all__ = [
    # Cache
    "get_cached_stats",
    "cache_stats",
    "clear_cache",
    # KPIs
    "get_trash_composition",
    "get_environmental_footprint",
    "get_ai_fleet_efficiency",
    "get_temporal_trends",
    "get_mean_time_to_process",
    "get_hotspot_density",
    "get_fun_facts",
]
