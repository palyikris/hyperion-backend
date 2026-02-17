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

START_TIME = time.time()
# Initialize with zeros so the chart does not look broken on first load.
load_history = deque([0, 0, 0, 0, 0, 0, 0], maxlen=7)
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
            return (used / limit) * 100.0

        # trying cgroup v1
        elif os.path.exists("/sys/fs/cgroup/memory/memory.usage_in_bytes"):
            with open("/sys/fs/cgroup/memory/memory.usage_in_bytes") as f:
                used = int(f.read().strip())
            with open("/sys/fs/cgroup/memory/memory.limit_in_bytes") as f:
                limit = int(f.read().strip())
            # very high -> not actual limit
            if limit > 1e15:
                return psutil.virtual_memory().percent
            return (used / limit) * 100.0
    except Exception:
        pass

    # local development
    return psutil.virtual_memory().percent


def get_container_cpu_percent():
    """
    Read CPU usage from cgroup files (container-aware).
    Falls back to psutil if not in a container.
    """
    global _last_cpu_usage, _last_cpu_time

    try:
        # Try cgroup v2
        if os.path.exists("/sys/fs/cgroup/cpu.stat"):
            with open("/sys/fs/cgroup/cpu.stat") as f:
                for line in f:
                    if line.startswith("usage_usec"):
                        current_usage = int(line.split()[1])
                        current_time = time.time()

                        if _last_cpu_usage is not None and _last_cpu_time is not None:
                            usage_diff = current_usage - _last_cpu_usage
                            time_diff = current_time - _last_cpu_time
                            # converting microseconds to percentage
                            cpu_percent = (usage_diff / (time_diff * 1_000_000)) * 100.0
                            _last_cpu_usage = current_usage
                            _last_cpu_time = current_time
                            return min(100.0, max(0.0, cpu_percent))
                        else:
                            _last_cpu_usage = current_usage
                            _last_cpu_time = current_time
                            return psutil.cpu_percent(interval=0.1)

        # Try cgroup v1
        elif os.path.exists("/sys/fs/cgroup/cpuacct/cpuacct.usage"):
            with open("/sys/fs/cgroup/cpuacct/cpuacct.usage") as f:
                current_usage = int(f.read().strip())
                current_time = time.time()

                if _last_cpu_usage is not None and _last_cpu_time is not None:
                    usage_diff = current_usage - _last_cpu_usage
                    time_diff = current_time - _last_cpu_time
                    # converting nanoseconds to percentage
                    cpu_percent = (usage_diff / (time_diff * 1_000_000_000)) * 100.0
                    _last_cpu_usage = current_usage
                    _last_cpu_time = current_time
                    return min(100.0, max(0.0, cpu_percent))
                else:
                    _last_cpu_usage = current_usage
                    _last_cpu_time = current_time
                    return psutil.cpu_percent(interval=0.1)
    except Exception:
        pass

    return psutil.cpu_percent(interval=0.1)


def get_formatted_uptime():
    """Calculates uptime as a percentage of a 24-hour window."""
    uptime_seconds = time.time() - START_TIME
    uptime_percent = min(100.0, (uptime_seconds / 86400) * 100.0)
    return round(uptime_percent, 1)


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
    load_history.append(round(combined_pressure))

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
            "uptime": get_formatted_uptime(),
            "server_load": list(load_history),
            "environment": "PROD",
            "status": system_status,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
    )
