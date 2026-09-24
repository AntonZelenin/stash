"""add image thumbnail key

Revision ID: f3b8d1e6a274
Revises: e7a2c4f9b631
Create Date: 2026-09-24 05:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3b8d1e6a274'
down_revision: Union[str, Sequence[str], None] = 'e7a2c4f9b631'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Set by the thumbnail worker once the thumbnail is stored; null until
    # then (or if thumbnailing failed).
    op.add_column('item_images', sa.Column('thumbnail_key', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('item_images', 'thumbnail_key')
