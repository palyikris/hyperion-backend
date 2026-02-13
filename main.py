from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from app.api import auth
from app.api import system
from fastapi.middleware.cors import CORSMiddleware
import os

# media, stats


app = FastAPI(
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
    allow_origins=[FRONTEND_URL, "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Public Routes
app.include_router(auth.router, prefix="/api/auth", tags=["Authentication"])
app.include_router(system.router, prefix="/api", tags=["System"])

# protected Routes
# app.include_router(media.router, prefix="/media", tags=["Media Operations"])
# app.include_router(stats.router, prefix="/stats", tags=["Statistics"])


@app.get("/")
async def root():
    return {"message": "Hyperion API is running"}
