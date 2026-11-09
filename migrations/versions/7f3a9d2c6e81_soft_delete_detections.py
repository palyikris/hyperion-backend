"""Retain detections marked clean without exposing them to application reads.

Revision ID: 7f3a9d2c6e81
Revises: 29cd64845037
"""
from alembic import op
import sqlalchemy as sa

revision = "7f3a9d2c6e81"
down_revision = "29cd64845037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("detections", "video_detections"):
        op.add_column(table, sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for table in ("video_detections", "detections"):
        op.drop_column(table, "deleted_at")
