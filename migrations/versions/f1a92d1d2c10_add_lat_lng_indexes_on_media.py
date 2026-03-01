"""add lat lng indexes on media

Revision ID: f1a92d1d2c10
Revises: d74a13b1e4aa
Create Date: 2026-03-01 13:05:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "f1a92d1d2c10"
down_revision: Union[str, Sequence[str], None] = "d74a13b1e4aa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_media_lat", "media", ["lat"], unique=False)
    op.create_index("ix_media_lng", "media", ["lng"], unique=False)
    op.create_index("ix_media_lat_lng", "media", ["lat", "lng"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_media_lat_lng", table_name="media")
    op.drop_index("ix_media_lng", table_name="media")
    op.drop_index("ix_media_lat", table_name="media")
