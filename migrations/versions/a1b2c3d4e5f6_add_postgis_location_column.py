"""add postgis location column to media

Revision ID: a1b2c3d4e5f6
Revises: 3b1b9e8f2a44
Create Date: 2026-03-01 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from geoalchemy2 import Geometry


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "3b1b9e8f2a44"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Enable PostGIS extension
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis;")

    # Add geometry column for location (POINT with SRID 4326 = WGS84)
    op.add_column(
        "media",
        sa.Column(
            "location",
            Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=True,
        ),
    )

    # Migrate existing lat/lng data to the new location column
    op.execute(
        """
        UPDATE media
        SET location = ST_SetSRID(ST_MakePoint(lng, lat), 4326)
        WHERE lat IS NOT NULL AND lng IS NOT NULL;
        """
    )

    # Drop old B-tree indexes on lat/lng
    op.drop_index("ix_media_lat_lng", table_name="media")
    op.drop_index("ix_media_lat", table_name="media")
    op.drop_index("ix_media_lng", table_name="media")

    # Create GIST spatial index on the new location column
    op.create_index(
        "ix_media_location_gist",
        "media",
        ["location"],
        unique=False,
        postgresql_using="gist",
    )


def downgrade() -> None:
    # Drop GIST spatial index
    op.drop_index("ix_media_location_gist", table_name="media")

    # Recreate old B-tree indexes
    op.create_index("ix_media_lat", "media", ["lat"], unique=False)
    op.create_index("ix_media_lng", "media", ["lng"], unique=False)
    op.create_index("ix_media_lat_lng", "media", ["lat", "lng"], unique=False)

    # Drop the location column
    op.drop_column("media", "location")

    # Note: We don't drop the postgis extension as other tables might use it
