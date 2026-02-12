import time
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db

router = APIRouter()


@router.get("/health")
async def health_check(db: AsyncSession = Depends(get_db)):
    health_status = {
        "status": "healthy",
        "timestamp": time.time(),
        "components": {"api": "up", "database": "down"},
    }

    try:
        # Start a timer to measure latency
        start_time = time.perf_counter()

        # Execute a simple query to ping the DB
        await db.execute(text("SELECT 1"))

        end_time = time.perf_counter()

        health_status["components"]["database"] = "up"
        health_status["database_latency_ms"] = round((end_time - start_time) * 1000, 2)

    except Exception as e:
        health_status["status"] = "unhealthy"
        health_status["error"] = str(e)
        
        # return a 503 Service Unavailable so load balancers know we are down
        raise HTTPException(status_code=503, detail=health_status)

    return health_status
