import os
import sys
from logging.config import fileConfig

sys.path.append(os.getcwd())


from dotenv import load_dotenv

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context


# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config


load_dotenv()
database_url = os.getenv("DATABASE_URL")

if database_url:
    # Alembic migrations are usually sync, so we ensure the protocol is correct

    config.set_main_option("sqlalchemy.url", database_url)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
from app.database import Base
from app.models.db.TokenBlacklist import (
    TokenBlacklist,
)
from app.models.db.User import (
    User,
)
from app.models.db.Media import Media, MediaType
from app.models.upload.MediaStatus import MediaStatus
from app.models.db.AIWorker import AIWorkerState
from app.models.db.MediaLog import MediaLog
from app.models.db.Detection import Detection
from app.models.db.VideoDetection import VideoDetection

target_metadata = Base.metadata


# --- Ignore spatial_ref_sys table (PostGIS) in migrations ---
def include_object(object, name, type_, reflected, compare_to):
    if type_ == "table" and name == "spatial_ref_sys":
        return False
    return True


# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import pool


def do_run_migrations(connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        # It's good practice to keep the dialect_opts for async pg
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations():
    """In this scenario we need to create an AsyncEngine
    and associate a connection with the context."""

    url = config.get_main_option("sqlalchemy.url")
    if url is None:
        raise RuntimeError("sqlalchemy.url is not set")

    connectable = create_async_engine(
        url,
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


import sys

def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""

    # ADD THESE TWO LINES FOR WINDOWS COMPATIBILITY:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
