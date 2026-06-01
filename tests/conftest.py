import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from main import app
from app.database import get_db
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database import Base

# 1. Connect to the ephemeral CI database
TEST_DATABASE_URL = (
    "postgresql+asyncpg://test_user:test_pass@localhost:5432/hyperion_test"
)

engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture(scope="session", autouse=True)
async def setup_database():
    """Runs once per test session to create and drop tables."""
  
    from app.database import Base

    async with engine.begin() as conn:
        # Create PostGIS extension
        from sqlalchemy import text

        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis;"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def db_session():
    """Provides a fresh database session for a single test."""
    async with TestSessionLocal() as session:
        yield session
        # ! Rollback after each test so tests don't pollute each other's data
        await session.rollback()


@pytest_asyncio.fixture
async def client(db_session):
    """
    Creates an async HTTP client and overrides the database dependency
    so FastAPI uses the test database session.
    """

    async def override_get_db():
        yield db_session

    # Override the dependency injected in app/api/auth.py
    app.dependency_overrides[get_db] = override_get_db

    # Create the test client
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac

    # Clean up overrides after test
    app.dependency_overrides.clear()
