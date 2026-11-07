from dotenv import load_dotenv

load_dotenv()

from contextlib import asynccontextmanager
import asyncio
import logging
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
from app.database import AsyncSessionLocal, engine

# Video temp cleaner import
from app.api.upload_utils.video_temp_cleaner import cleanup_old_video_temp_dirs
from app.core.csrf import enforce_csrf_origin, get_allowed_frontend_origins


BLACKLIST_PRUNE_INTERVAL_SECONDS = 3600
VIDEO_CLEANUP_INTERVAL_SECONDS = 24 * 3600
logger = logging.getLogger(__name__)


async def _blacklist_pruner() -> None:
    while True:
        await asyncio.sleep(BLACKLIST_PRUNE_INTERVAL_SECONDS)
        try:
            async with AsyncSessionLocal() as session:
                await prune_expired_blacklisted_tokens(session)
                await session.commit()
        except Exception as exc:
            logger.warning("Blacklist cleanup failed (%s)", type(exc).__name__)


async def _video_temp_cleaner() -> None:
    while True:
        try:
            await asyncio.to_thread(cleanup_old_video_temp_dirs)
        except Exception as exc:
            logger.warning("Video cleanup failed (%s)", type(exc).__name__)
        await asyncio.sleep(VIDEO_CLEANUP_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = []
    try:
        async with AsyncSessionLocal() as session:
            await prune_expired_blacklisted_tokens(session)
            await session.commit()
        # Initialization must finish before serving traffic. Retain every task
        # so reloads and shutdown do not leave an unowned worker fleet running.
        tasks.extend(await initialize_worker_fleet())
        tasks.append(asyncio.create_task(_blacklist_pruner(), name="blacklist-pruner"))
        tasks.append(asyncio.create_task(_video_temp_cleaner(), name="video-temp-cleaner"))
        app.state.background_tasks = tasks
        yield
    finally:
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.warning("Background task stopped with %s", type(result).__name__)
        await engine.dispose()


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

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_allowed_frontend_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def csrf_origin_middleware(request, call_next):
    return await enforce_csrf_origin(request, call_next)


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
