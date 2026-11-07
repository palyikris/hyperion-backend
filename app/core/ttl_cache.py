"""Bound process-local TTL dictionaries without adding a background task."""

from typing import TypeVar


Key = TypeVar("Key")
Value = TypeVar("Value")


def prune_ttl_cache(
    cache: dict[Key, tuple[float, Value]],
    now: float,
    ttl: float,
    max_entries: int,
) -> None:
    for key, (created_at, _) in list(cache.items()):
        if now - created_at > ttl:
            del cache[key]
    while len(cache) > max_entries:
        oldest = min(cache, key=lambda key: cache[key][0])
        del cache[oldest]
