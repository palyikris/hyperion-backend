from app.api.dashboard_utils.ai_workers import router as ai_workers_router
from app.api.dashboard_utils.system_health import router as system_health_router
from app.api.dashboard_utils.ux import router as ux_router

__all__ = [
    "ai_workers_router",
    "system_health_router",
    "ux_router",
]
