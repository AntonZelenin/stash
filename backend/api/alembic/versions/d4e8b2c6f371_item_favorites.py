"""item favorites

Revision ID: d4e8b2c6f371
Revises: c3d9a1e7b524
Create Date: 2026-09-24 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e8b2c6f371'
down_revision: Union[str, Sequence[str], None] = 'c3d9a1e7b524'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Existing items start out not favorited.
    op.add_column('items', sa.Column('is_favorite', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('items', 'is_favorite')
