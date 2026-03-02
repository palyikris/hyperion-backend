"""
In-memory caching for stats endpoints.

Cache Configuration:
- 5-minute TTL to reduce database load for expensive aggregation queries
- Cache key is (user_id, days) tuple
- Uses monotonic() for TTL comparison (immune to system clock changes)
"""

from time import monotonic

# Cache structure: { (user_id, days): (timestamp_monotonic, response_dict) }
STATS_CACHE_TTL_SECONDS = 300
_stats_cache: dict[tuple, tuple[float, dict]] = {}


def get_cached_stats(user_id: str, days: int) -> dict | None:
    """
    Retrieve cached stats if they exist and are within TTL.
    
    Returns:
        Response dict if cache hit, None if miss or expired
    """
    cache_key = (user_id, days)
    cached = _stats_cache.get(cache_key)
    now = monotonic()
    
    if cached and now - cached[0] <= STATS_CACHE_TTL_SECONDS:
        return cached[1]
    return None


def cache_stats(user_id: str, days: int, response_dict: dict) -> None:
    """Store response in cache with current timestamp."""
    cache_key = (user_id, days)
    _stats_cache[cache_key] = (monotonic(), response_dict)


def clear_cache() -> None:
    """Clear all cached stats."""
    global _stats_cache
    _stats_cache.clear()
