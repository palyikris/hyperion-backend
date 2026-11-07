from collections import deque
from datetime import datetime, timezone
import os
import time

import psutil
from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from app.api.deps import get_current_user
from app.models.dashboard.system_health import SystemHealthResponse

router = APIRouter()

START_TIME = time.monotonic()
# In-memory load history is process-local. Multi-worker deployments should use a
# shared store if they need a fleet-wide dashboard view.
# Initialize with zeros so the chart does not look broken on first load.
load_history = deque([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], maxlen=7)
_last_cpu_usage = None
_last_cpu_time = None


def get_container_memory_percent():
    """
    Read memory usage from cgroup files (container-aware).
    Falls back to psutil if not in a container.
    """
    try:
        # trying cgroup v2 (newer Docker/K8s)
        if os.path.exists("/sys/fs/cgroup/memory.current"):
            with open("/sys/fs/cgroup/memory.current") as f:
                used = int(f.read().strip())
            with open("/sys/fs/cgroup/memory.max") as f:
                limit_str = f.read().strip()
                # "max" means no limit, fall back to psutil
                if limit_str == "max":
                    return psutil.virtual_memory().percent
                limit = int(limit_str)
            if limit <= 0:
                return psutil.virtual_memory().percent
            limit = min(limit, psutil.virtual_memory().total)
            return min(100.0, (used / limit) * 100.0)

        # trying cgroup v1
        elif os.path.exists("/sys/fs/cgroup/memory/memory.usage_in_bytes"):
            with open("/sys/fs/cgroup/memory/memory.usage_in_bytes") as f:
                used = int(f.read().strip())
            with open("/sys/fs/cgroup/memory/memory.limit_in_bytes") as f:
                limit = int(f.read().strip())
            # very high -> not actual limit
            if limit <= 0 or limit > 1e15:
                return psutil.virtual_memory().percent
            limit = min(limit, psutil.virtual_memory().total)
            return min(100.0, (used / limit) * 100.0)
    except Exception:
        pass

    # local development
    return psutil.virtual_memory().percent


def _cpu_capacity():
    """CPU cores available to the process, bounded by the cgroup CPU quota."""
    capacity = float(os.cpu_count() or 1)
    if hasattr(os, "sched_getaffinity"):
        capacity = min(capacity, float(len(os.sched_getaffinity(0))))
    try:
        if os.path.exists("/sys/fs/cgroup/cpu.max"):
            with open("/sys/fs/cgroup/cpu.max") as stream:
                quota, period = stream.read().split()
            if quota != "max" and int(period) > 0:
                capacity = min(capacity, int(quota) / int(period))
        elif os.path.exists("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"):
            with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us") as stream:
                quota = int(stream.read())
            with open("/sys/fs/cgroup/cpu/cpu.cfs_period_us") as stream:
                period = int(stream.read())
            if quota > 0 and period > 0:
                capacity = min(capacity, quota / period)
    except (OSError, ValueError):
        pass
    return capacity if capacity > 0 else 1.0


def get_container_cpu_percent():
    """Cgroup CPU usage as a percentage of available CPU capacity."""
    global _last_cpu_usage, _last_cpu_time
    try:
        if os.path.exists("/sys/fs/cgroup/cpu.stat"):
            with open("/sys/fs/cgroup/cpu.stat") as stream:
                values = dict(line.split() for line in stream if line.strip())
            usage = int(values["usage_usec"]) / 1_000_000
        elif os.path.exists("/sys/fs/cgroup/cpuacct/cpuacct.usage"):
            with open("/sys/fs/cgroup/cpuacct/cpuacct.usage") as stream:
                usage = int(stream.read()) / 1_000_000_000
        else:
            return float(psutil.cpu_percent(interval=None))
        now = time.monotonic()
        previous_usage, previous_time = _last_cpu_usage, _last_cpu_time
        _last_cpu_usage, _last_cpu_time = usage, now
        if previous_usage is None or previous_time is None or now <= previous_time:
            return 0.0
        usage_delta = max(0.0, usage - previous_usage)
        percent = usage_delta / (now - previous_time) / _cpu_capacity() * 100
        return min(100.0, max(0.0, percent))
    except (OSError, ValueError, KeyError):
        return float(psutil.cpu_percent(interval=None))


def get_uptime_sla_percent():
    """Availability is unknown without persisted uptime/downtime observations.

    Process age divided by 30 days is not an SLA. Keep the optional API field
    unset until an availability history exists; process age is reported separately.
    """
    return None


def get_formatted_uptime():
    """Returns human-readable uptime string (e.g., '3d 5h 22m')."""
    uptime_seconds = max(0, int(time.monotonic() - START_TIME))
    days, remainder = divmod(uptime_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")

    return " ".join(parts)


@router.get(
    "/system-health",
    status_code=status.HTTP_200_OK,
    response_model=SystemHealthResponse,
)
async def get_system_health(current_user=Depends(get_current_user)):
    """
    Returns real-time hardware metrics for the Hyperion environment.
    Container-aware: reads from cgroup files when available.
    """
    cpu_load = get_container_cpu_percent()
    ram_load = get_container_memory_percent()

    combined_pressure = max(cpu_load, ram_load)
    load_history.append(round(combined_pressure, 1))

    if combined_pressure > 95:
        system_status = "STRESSED"
    elif combined_pressure > 80:
        system_status = "HEAVY_LOAD"
    elif combined_pressure < 30:
        system_status = "STABILIZED"
    else:
        system_status = "ACTIVE"

    return JSONResponse(
        content={
            "uptime": get_uptime_sla_percent(),
            "uptime_formatted": get_formatted_uptime(),
            "server_load": list(load_history),
            "environment": "PROD",
            "status": system_status,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
    )
