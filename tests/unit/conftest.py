import pytest


@pytest.fixture(scope="session", autouse=True)
def setup_database():
    """Override the integration suite's PostgreSQL setup for isolated tests."""
    yield
