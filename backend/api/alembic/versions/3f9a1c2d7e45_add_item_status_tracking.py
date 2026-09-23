"""add item status tracking

Revision ID: 3f9a1c2d7e45
Revises: 7c83f176a91d
Create Date: 2026-09-24 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3f9a1c2d7e45'
down_revision: Union[str, Sequence[str], None] = '7c83f176a91d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'items',
        sa.Column('status_updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )
    op.add_column('items', sa.Column('requeue_count', sa.Integer(), server_default='0', nullable=False))
    op.create_index(
        'ix_items_unfinished_status_updated_at',
        'items',
        ['status_updated_at'],
        postgresql_where=sa.text("status IN ('pending', 'processing')"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_items_unfinished_status_updated_at', table_name='items')
    op.drop_column('items', 'requeue_count')
    op.drop_column('items', 'status_updated_at')
