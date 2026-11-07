from collections import deque
from datetime import datetime, timezone
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.api.dashboard_utils import system_health, ux


def test_active_users_expire_even_without_another_user_request(monkeypatch):
    monkeypatch.setattr(ux, "active_users", {"expired": 100, "active": 399})
    monkeypatch.setattr(ux, "last_trend_update", 399)
    with patch.object(ux.time, "time", return_value=400):
        ux.update_metrics()
    assert ux.active_users == {"active": 399}


def test_daily_rollover_retains_first_user_and_fills_missing_days(monkeypatch):
    monkeypatch.setattr(ux, "current_day", datetime(2026, 6, 1).date())
    monkeypatch.setattr(ux, "daily_user_sessions", {"old": True})
    monkeypatch.setattr(ux, "daily_history", deque([0] * 7, maxlen=7))
    monkeypatch.setattr(ux, "active_users", {})
    monkeypatch.setattr(ux, "active_trend_history", deque([0] * 7, maxlen=7))
    monkeypatch.setattr(ux, "last_trend_update", 0)
    with patch.object(ux, "datetime") as clock:
        clock.now.return_value = datetime(2026, 6, 4, tzinfo=timezone.utc)
        ux.update_active_users("first-today")
    assert list(ux.daily_history)[-3:] == [1, 0, 0]
    assert ux.daily_user_sessions == {"first-today": True}
    assert ux.active_trend_history[-1] == 1


def test_cpu_usage_is_normalized_by_container_capacity(monkeypatch):
    monkeypatch.setattr(system_health, "_last_cpu_usage", None)
    monkeypatch.setattr(system_health, "_last_cpu_time", None)
    files = {"/sys/fs/cgroup/cpu.stat": "usage_usec 1000000\n",
             "/sys/fs/cgroup/cpu.max": "200000 100000"}
    with patch.object(system_health.os.path, "exists", side_effect=lambda path: path in files), \
         patch("builtins.open", side_effect=lambda path, *args, **kwargs: StringIO(files[path])), \
         patch.object(system_health.time, "monotonic", side_effect=[1, 2]), \
         patch.object(system_health.os, "cpu_count", return_value=8):
        assert system_health.get_container_cpu_percent() == 0
        files["/sys/fs/cgroup/cpu.stat"] = "usage_usec 2000000\n"
        assert system_health.get_container_cpu_percent() == 50


def test_cpu_falls_back_to_measured_host_usage():
    with patch.object(system_health.os.path, "exists", return_value=False), \
         patch.object(system_health.psutil, "cpu_percent", return_value=37.5):
        assert system_health.get_container_cpu_percent() == 37.5


def test_uptime_does_not_invent_an_availability_percentage(monkeypatch):
    monkeypatch.setattr(system_health, "START_TIME", 100)
    with patch.object(system_health.time, "monotonic", return_value=100 + 90061):
        assert system_health.get_formatted_uptime() == "1d 1h 1m"
    assert system_health.get_uptime_sla_percent() is None


def test_memory_zero_limit_uses_host_measurement():
    files = {"/sys/fs/cgroup/memory.current": "100", "/sys/fs/cgroup/memory.max": "0"}
    with patch.object(system_health.os.path, "exists", side_effect=lambda path: path in files), \
         patch("builtins.open", side_effect=lambda path, *args, **kwargs: StringIO(files[path])), \
         patch.object(system_health.psutil, "virtual_memory", return_value=SimpleNamespace(percent=42)):
        assert system_health.get_container_memory_percent() == 42
