import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool
from sqlalchemy import text

import os

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://test_user:test_pass@localhost:5432/hyperion_test",
)
# Never point tests (including application dependencies) at the configured app DB.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("SECRET_KEY", "hyperion-test-secret")

from app.database import get_db, Base

@pytest_asyncio.fixture(scope="session")
async def setup_database():
    """Create tables in a unique schema on an explicitly selected test database."""
    if not os.getenv("TEST_DATABASE_URL"):
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL/PostGIS integration tests")
    from main import app  # Register all models without starting background workers.
    from sqlalchemy.engine import make_url
    import uuid

    if not (make_url(TEST_DATABASE_URL).database or "").endswith("_test"):
        raise ValueError("TEST_DATABASE_URL must name a disposable database ending in _test")
    schema = "hyperion_test_" + uuid.uuid4().hex

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis;"))
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.execute(text(f'SET search_path TO "{schema}", public'))
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

    yield schema

    # Create a temporary engine just for teardown
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(setup_database):
    """Provides a fresh database session for a single test."""
    # Creating the engine inside the fixture ensures it binds to the correct event loop!
    engine = create_async_engine(
        TEST_DATABASE_URL,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{setup_database},public"}},
    )
    TestSessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async with TestSessionLocal() as session:
        yield session
        await session.rollback()

    await engine.dispose()


@pytest_asyncio.fixture
async def client(db_session, monkeypatch):
    """Creates an async HTTP client and overrides the database dependency."""

    from main import app
    from app.api.dashboard_utils import ux

    monkeypatch.setattr(ux, "AsyncSessionLocal", async_sessionmaker(
        db_session.bind, class_=AsyncSession, expire_on_commit=False
    ))

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as ac:
        yield ac

    app.dependency_overrides.clear()


from app.models.db.User import User
from app.core import security
import uuid


@pytest_asyncio.fixture
async def auth_client(client: AsyncClient, db_session: AsyncSession):

    user_id = str(uuid.uuid4())[:20]

    test_user = User(
        id=user_id,
        email=f"vault_{uuid.uuid4()}@example.com",
        hashed_password=security.hash_password("password"),
        full_name="Vault Tester",
        language="en",
    )
    db_session.add(test_user)
    await db_session.commit()

    token = security.create_access_token(data={"sub": test_user.email})

    client.cookies.set("access_token", token)

    yield {"client": client, "user": test_user}
