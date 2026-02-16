from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from app.api.deps import get_current_user
from app.models.dashboard.ai_workers import AIWorkersResponse

router = APIRouter()


@router.get(
    "/ai-workers",
    status_code=status.HTTP_200_OK,
    response_model=AIWorkersResponse,
)
async def ai_workers(current_user=Depends(get_current_user)):
    """
    Mock endpoint for the upcoming AI Worker fleet implementation.
    """
    return JSONResponse(
        content={
            "total_active_fleet": 12,
            "cluster_status": "Stabilized",
            "nodes": [
                {"name": "Orion", "status": "Active"},
                {"name": "Vega", "status": "Syncing"},
                {"name": "Lyra", "status": "Idle"},
                {"name": "Atlas", "status": "Paused"},
            ],
        }
    )
