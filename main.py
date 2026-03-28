from dotenv import load_dotenv

load_dotenv()

from contextlib import asynccontextmanager
import asyncio
from fastapi import FastAPI
from app.api import auth
from app.api import system
from app.api import dashboard
from app.api import upload
from app.api import vault
from app.api import map
from app.api import stats
from app.api import lab
from fastapi.middleware.cors import CORSMiddleware
from app.api.dashboard_utils.ux import track_ux_metrics
from app.api.dashboard_utils.utils.init_workers import initialize_worker_fleet
from app.api.auth import prune_expired_blacklisted_tokens
from app.database import AsyncSessionLocal
import os

# Video temp cleaner import
from app.api.upload_utils.video_temp_cleaner import cleanup_old_video_temp_dirs


BLACKLIST_PRUNE_INTERVAL_SECONDS = 3600


async def _blacklist_pruner() -> None:
    while True:
        await asyncio.sleep(BLACKLIST_PRUNE_INTERVAL_SECONDS)
        async with AsyncSessionLocal() as session:
            await prune_expired_blacklisted_tokens(session)
            await session.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(initialize_worker_fleet())

    async with AsyncSessionLocal() as session:
        await prune_expired_blacklisted_tokens(session)
        await session.commit()

    prune_task = asyncio.create_task(_blacklist_pruner())

    async def periodic_cleanup():
        while True:
            await asyncio.to_thread(cleanup_old_video_temp_dirs)
            await asyncio.sleep(24 * 3600)  # 24 óra

    video_cleanup_task = asyncio.create_task(periodic_cleanup())

    try:
        yield
    finally:
        prune_task.cancel()
        video_cleanup_task.cancel()
        try:
            await prune_task
        except asyncio.CancelledError:
            pass
        try:
            await video_cleanup_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    lifespan=lifespan,
    title="Hyperion AI Backend",
    description="""
    API for the Hyperion platform. 
    Supports user authentication, geospatial media management with PostGIS, 
    and AI-driven object detection.
    """,
    version="1.0.0",
    contact={
        "name": "Hyperion Dev Team",
    },
    license_info={
        "name": "MIT",
    },
)

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL, "http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type"],
)

@app.middleware("http")
async def middleware(request, call_next):
    return await track_ux_metrics(request, call_next)


# Public Routes
app.include_router(auth.router, prefix="/api/auth", tags=["Authentication"])
app.include_router(system.router, prefix="/api", tags=["System"])
app.include_router(dashboard.router, prefix="/api/dashboard", tags=["Dashboard"])
app.include_router(upload.router, prefix="/api/upload", tags=["Media Upload"])
app.include_router(vault.router, prefix="/api", tags=["Media Vault"])
app.include_router(map.router, prefix="/api", tags=["Map Data"])
app.include_router(stats.router, prefix="/api", tags=["Statistics"])
app.include_router(lab.router, prefix="/api/lab", tags=["Lab"])


@app.get("/")
async def root():
    return {"message": "Hyperion API is running"}
