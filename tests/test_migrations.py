"""Check the deployment schema, independently of ORM create_all fixtures."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import inspect, text

from app.api.auth import prune_expired_blacklisted_tokens
from app.api.medialog_utils.media_log_utils import create_status_change_log
from app.database import Base
from app.models.db.Media import Media, MediaType
from app.models.db.TokenBlacklist import TokenBlacklist
from app.models.db.User import User
from app.models.upload.MediaStatus import MediaStatus


@pytest.mark.asyncio
@pytest.mark.parametrize("session_timezone", ["UTC", "Europe/Budapest"])
async def test_migrated_schema_matches_models_and_accepts_utc_writes(
    db_session, session_timezone
):
    connection = await db_session.connection()
    schema = "migration_test_" + uuid.uuid4().hex

    def migrate(sync_connection):
        sync_connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        sync_connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        config.attributes["connection"] = sync_connection
        # CI has already migrated public. Keep this schema's revision history
        # private so Alembic cannot inherit public.alembic_version and skip it.
        config.attributes["version_table_schema"] = schema
        command.upgrade(config, "7f3a9d2c6e81")
        columns = inspect(sync_connection).get_columns("token_blacklist", schema=schema)
        assert all(
            not column["type"].timezone
            for column in columns
            if column["name"] in {"blacklisted_at", "expires_at"}
        )
        sync_connection.execute(
            text("SELECT set_config('TimeZone', :timezone, true)"),
            {"timezone": session_timezone},
        )
        sync_connection.execute(text("""
            INSERT INTO token_blacklist (token, user_email, blacklisted_at, expires_at)
            VALUES ('legacy-token', 'legacy@example.test', '2026-01-01 12:00:00', '2030-01-01 12:00:00')
        """))
        command.upgrade(config, "head")
        expires_at = sync_connection.execute(text(
            "SELECT expires_at FROM token_blacklist WHERE token = 'legacy-token'"
        )).scalar_one()
        assert expires_at == datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
        migration_context = MigrationContext.configure(sync_connection, opts={
            "include_object": lambda obj, name, kind, reflected, compare_to:
                kind != "table" or name in Base.metadata.tables,
        })
        assert compare_metadata(migration_context, Base.metadata) == []

    # All schema changes and records roll back with the integration fixture.
    await connection.run_sync(migrate)
    await prune_expired_blacklisted_tokens(db_session)
    user = User(email="migration@example.test", full_name="Test", hashed_password="test")
    db_session.add(user)
    await db_session.flush()
    media = Media(uploader_id=user.id, media_type=MediaType.IMAGE, status=MediaStatus.READY)
    db_session.add(media)
    await db_session.flush()
    db_session.add(create_status_change_log(media.id, MediaStatus.READY))
    token = TokenBlacklist(token="migration-test-token", user_email=user.email,
                           expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    db_session.add(token)
    await db_session.flush()
    await db_session.refresh(token)
    assert token.expires_at.utcoffset() == timedelta(0)
