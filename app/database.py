import os
import re
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import DateTime, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, with_loader_criteria
from datetime import datetime

load_dotenv()


DATABASE_URL = os.getenv("DATABASE_URL")

engine = create_async_engine(
    DATABASE_URL or "",
    echo=False,
    hide_parameters=True,  # SQL errors must not expose tokens or password hashes.
    pool_pre_ping=True,  # verify connections are alive before using them
    pool_size=20,
    max_overflow=10,  # allow up to 10 additional connections beyond the pool_size when needed
)
AsyncSessionLocal = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False
)


class Base(DeclarativeBase):
    pass


class SoftDeletableDetection:
    """Cleaned detections are retained for history but absent from normal reads."""

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


@event.listens_for(Session, "do_orm_execute")
def _exclude_cleaned_detections(execute_state):
    # Apply to entities, aggregates, joins, unions and relationship loads alike.
    # The opt-out is for internal history inspection and permanent media cleanup.
    if execute_state.is_select and not execute_state.execution_options.get(
        "include_cleaned_detections", False
    ):
        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(
                SoftDeletableDetection,
                lambda detection: detection.deleted_at.is_(None),
                include_aliases=True,
            )
        )


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
