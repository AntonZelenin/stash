"""items user_id type index

Revision ID: d6f1b3e8a425
Revises: c8e4a2f6d173
Create Date: 2026-09-25 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd6f1b3e8a425'
down_revision: Union[str, Sequence[str], None] = 'c8e4a2f6d173'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Replaces the user_id index: still serves every per-user lookup, and
    # also lets item counts per type be read from the index.
    op.create_index('ix_items_user_id_type', 'items', ['user_id', 'type'])
    op.drop_index('ix_items_user_id', table_name='items')


def downgrade() -> None:
    """Downgrade schema."""
    op.create_index('ix_items_user_id', 'items', ['user_id'])
    op.drop_index('ix_items_user_id_type', table_name='items')
