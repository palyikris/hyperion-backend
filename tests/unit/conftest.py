"""Run isolated pipeline tests with --confcutdir=tests/unit, without live services."""
import os

import pytest

# app.database creates an engine at import time; never use developer credentials here.
os.environ["DATABASE_URL"] = (
    "postgresql+asyncpg://test_user:test_pass@localhost:5432/hyperion_test"
)
os.environ.setdefault("SECRET_KEY", "detection-pipeline-unit-test-key")


@pytest.fixture(scope="session", autouse=True)
def setup_database():
    # Override the integration suite's autouse fixture for these mocked service tests.
    yield
