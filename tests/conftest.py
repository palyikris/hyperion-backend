import pytest
import pytest_asyncio
import os
from contextlib import asynccontextmanager
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool
from sqlalchemy import text

# Never fall back to the application's DATABASE_URL: setup recreates tables.
TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://test_user:test_pass@localhost:5432/hyperion_test",
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("SECRET_KEY", "hyperion-tests-only-secret")

from main import app
from app.database import get_db, Base
from app.api.dashboard_utils import ux

@pytest_asyncio.fixture(scope="session", autouse=True)
async def setup_database():
    """Create a private schema in an explicitly selected disposable database."""
    if not os.getenv("TEST_DATABASE_URL"):
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL/PostGIS integration tests")
    schema = "hyperion_test_" + uuid.uuid4().hex

    database_name = make_url(TEST_DATABASE_URL).database or ""
    if not (database_name.startswith("test_") or database_name.endswith("_test")):
        raise pytest.UsageError(
            "TEST_DATABASE_URL must name a dedicated database starting with test_ "
            "or ending with _test."
        )

    # Create a temporary engine just for setup
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
        TEST_DATABASE_URL, poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{setup_database},public"}},
    )
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                async with AsyncSession(
                    bind=connection,
                    expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                ) as session:
                    yield session
            finally:
                # Endpoint commits only release savepoints; no rows leak to
                # the next test, including users with fixed email addresses.
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def client(db_session, monkeypatch):
    """Creates an async HTTP client and overrides the database dependency."""

    async def override_get_db():
        yield db_session

    @asynccontextmanager
    async def metrics_session():
        # Middleware must see the same uncommitted test data as the routes.
        yield db_session

    monkeypatch.setattr(ux, "AsyncSessionLocal", metrics_session)
    app.dependency_overrides[get_db] = override_get_db

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test"
        ) as ac:
            yield ac
    finally:
        app.dependency_overrides.pop(get_db, None)


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
    client.headers["Origin"] = "https://test"

    yield {"client": client, "user": test_user}
