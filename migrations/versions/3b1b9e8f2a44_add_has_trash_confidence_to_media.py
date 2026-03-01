"""add has_trash and confidence columns to media

Revision ID: 3b1b9e8f2a44
Revises: f1a92d1d2c10
Create Date: 2026-03-01 16:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "3b1b9e8f2a44"
down_revision: Union[str, Sequence[str], None] = "f1a92d1d2c10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "media",
        sa.Column("has_trash", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "media",
        sa.Column("confidence", sa.Float(), nullable=False, server_default=sa.text("0")),
    )

    op.execute(
        """
        UPDATE media AS m
        SET
            has_trash = TRUE,
            confidence = agg.max_confidence * 100.0
        FROM (
            SELECT d.media_id, MAX(d.confidence) AS max_confidence
            FROM detections AS d
            GROUP BY d.media_id
        ) AS agg
        WHERE m.id = agg.media_id
        """
    )


def downgrade() -> None:
    op.drop_column("media", "confidence")
    op.drop_column("media", "has_trash")
