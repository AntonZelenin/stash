"""item tag created_at

Revision ID: c8e4a2f6d173
Revises: b4f6d2a8c915
Create Date: 2026-09-25 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8e4a2f6d173'
down_revision: Union[str, Sequence[str], None] = 'b4f6d2a8c915'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # When each tag was put on each item, for ranking tag suggestions by
    # recency. Existing links didn't record it; the item's creation time is
    # the closest thing we have.
    op.add_column('item_tags', sa.Column('created_at', sa.DateTime(timezone=True), nullable=True))
    op.execute('UPDATE item_tags SET created_at = items.created_at FROM items WHERE items.id = item_tags.item_id')
    op.alter_column('item_tags', 'created_at', nullable=False, server_default=sa.text('now()'))

    # Replaces the tag_id index: still serves filtering by tag, and also
    # lets suggestions count uses and find the latest one per tag from the
    # index alone.
    op.create_index('ix_item_tags_tag_id_created_at', 'item_tags', ['tag_id', 'created_at'])
    op.drop_index('ix_item_tags_tag_id', table_name='item_tags')


def downgrade() -> None:
    """Downgrade schema."""
    op.create_index('ix_item_tags_tag_id', 'item_tags', ['tag_id'])
    op.drop_index('ix_item_tags_tag_id_created_at', table_name='item_tags')
    op.drop_column('item_tags', 'created_at')
