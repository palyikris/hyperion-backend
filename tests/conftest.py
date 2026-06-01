import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool
from sqlalchemy import text

from main import app
from app.database import get_db, Base

TEST_DATABASE_URL = (
    "postgresql+asyncpg://test_user:test_pass@localhost:5432/hyperion_test"
)


@pytest_asyncio.fixture(scope="session", autouse=True)
async def setup_database():
    """Runs once per test session to create and drop tables."""
    # Create a temporary engine just for setup
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis;"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

    yield

    # Create a temporary engine just for teardown
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session():
    """Provides a fresh database session for a single test."""
    # Creating the engine inside the fixture ensures it binds to the correct event loop!
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    TestSessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async with TestSessionLocal() as session:
        yield session
        await session.rollback()

    await engine.dispose()


@pytest_asyncio.fixture
async def client(db_session):
    """Creates an async HTTP client and overrides the database dependency."""

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac

    app.dependency_overrides.clear()
