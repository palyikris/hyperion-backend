"""add failed_reason to media

Revision ID: a3b4c5d6e7f8
Revises: a1b2c3d4e5f6
Create Date: 2026-03-01 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3b4c5d6e7f8'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('media', sa.Column('failed_reason', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('media', 'failed_reason')
