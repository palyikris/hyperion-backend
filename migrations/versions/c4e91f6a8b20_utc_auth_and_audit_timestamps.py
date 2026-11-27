"""Align token revocation and audit timestamps with the application's UTC values.

Revision ID: c4e91f6a8b20
Revises: 7f3a9d2c6e81
"""
from alembic import context, op
import sqlalchemy as sa

revision = "c4e91f6a8b20"
down_revision = "7f3a9d2c6e81"
branch_labels = None
depends_on = None

TIMESTAMP_COLUMNS = (
    ("token_blacklist", "blacklisted_at"),
    ("token_blacklist", "expires_at"),
    ("media_task_logs", "timestamp"),
)


def _set_timezone(table: str, column: str, aware: bool) -> None:
    if not context.is_offline_mode():
        columns = sa.inspect(op.get_bind()).get_columns(table)
        current_type = next(item["type"] for item in columns if item["name"] == column)
        # Some deployments already created audit columns from the ORM models.
        # Avoid reinterpreting timestamps that are already timezone aware.
        if current_type.timezone == aware:
            return
    op.alter_column(
        table, column,
        existing_type=sa.DateTime(timezone=not aware),
        type_=sa.DateTime(timezone=aware),
        existing_nullable=False,
        postgresql_using=f'"{column}" AT TIME ZONE \'UTC\'',
    )


def upgrade() -> None:
    for table, column in TIMESTAMP_COLUMNS:
        _set_timezone(table, column, True)


def downgrade() -> None:
    for table, column in reversed(TIMESTAMP_COLUMNS):
        _set_timezone(table, column, False)
