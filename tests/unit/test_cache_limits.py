from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api import map as map_api
from app.api.stats_utils import cache
from app.core.ttl_cache import prune_ttl_cache


def test_pruning_removes_expired_and_then_oldest_live_entries():
    values = {"expired": (0, {}), "old": (90, {}), "recent": (95, {}), "new": (100, {})}
    prune_ttl_cache(values, now=100, ttl=20, max_entries=2)
    assert list(values) == ["recent", "new"]


def test_statistics_cache_keeps_ttl_boundary_and_removes_expired_reads(monkeypatch):
    values = {}
    monkeypatch.setattr(cache, "_stats_cache", values)
    monkeypatch.setattr(cache, "monotonic", lambda: 100)
    cache.cache_stats("owner", 7, {"value": 1})
    monkeypatch.setattr(cache, "monotonic", lambda: 400)
    assert cache.get_cached_stats("owner", 7) == {"value": 1}
    monkeypatch.setattr(cache, "monotonic", lambda: 400.1)
    assert cache.get_cached_stats("owner", 7) is None
    assert values == {}


def test_statistics_cache_is_bounded_and_keeps_users_and_windows_separate(monkeypatch):
    values = {}
    clock = iter([1, 2, 3, 3, 3, 3, 3])
    monkeypatch.setattr(cache, "_stats_cache", values)
    monkeypatch.setattr(cache, "monotonic", lambda: next(clock))
    monkeypatch.setattr(cache, "STATS_CACHE_MAX_ENTRIES", 2)
    cache.cache_stats("old", 1, {})
    cache.cache_stats("owner", 7, {"value": 7})
    cache.cache_stats("owner", 30, {"value": 30})
    assert len(values) == 2
    assert cache.get_cached_stats("old", 1) is None
    assert cache.get_cached_stats("owner", 7) == {"value": 7}
    assert cache.get_cached_stats("owner", 30) == {"value": 30}
    assert cache.get_cached_stats("other", 7) is None


@pytest.mark.asyncio
async def test_map_cache_prunes_old_views_and_obeys_its_size_limit(monkeypatch):
    values = {("expired",): (0, {})}
    now = [100]
    monkeypatch.setattr(map_api, "_map_stats_cache", values)
    monkeypatch.setattr(map_api, "monotonic", lambda: now[0])
    monkeypatch.setattr(map_api, "STATS_CACHE_MAX_ENTRIES", 2)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [])))
    for west in [18, 19, 20]:
        await map_api.get_map_stats(min_lat=47, max_lat=48, min_lng=west,
                                   max_lng=west + 0.5, resolution=0.005,
                                   db=db, current_user=SimpleNamespace(id="owner"))
        now[0] += 1
    assert len(values) == 2
    assert ("expired",) not in values
    assert all(key[3] in {19, 20} for key in values)
    await map_api.get_map_stats(min_lat=47, max_lat=48, min_lng=20, max_lng=20.5,
                               resolution=0.005, db=db, current_user=SimpleNamespace(id="owner"))
    assert db.execute.await_count == 3


@pytest.mark.asyncio
async def test_map_cache_does_not_round_distinct_resolutions_into_the_same_key(monkeypatch):
    monkeypatch.setattr(map_api, "_map_stats_cache", {})
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [])))
    for resolution in [0.0000001, 0.0000002]:
        await map_api.get_map_stats(min_lat=47, max_lat=48, min_lng=19, max_lng=20,
                                   resolution=resolution, db=db,
                                   current_user=SimpleNamespace(id="owner"))
    assert db.execute.await_count == 2
