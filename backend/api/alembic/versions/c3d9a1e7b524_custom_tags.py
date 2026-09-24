"""custom tags

Revision ID: c3d9a1e7b524
Revises: b7e2f4a9c613
Create Date: 2026-09-24 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d9a1e7b524'
down_revision: Union[str, Sequence[str], None] = 'b7e2f4a9c613'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # The old `item_tags` (a tag string per item) was an unused placeholder;
    # tags are now their own table, linked to items many-to-many.
    op.drop_table('item_tags')

    op.create_table(
        'tags',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    # One tag per name per user, ignoring case; also serves the per-user
    # name lookups.
    op.create_index('uq_tags_user_id_lower_name', 'tags', ['user_id', sa.text('lower(name)')], unique=True)

    op.create_table(
        'item_tags',
        sa.Column('item_id', sa.Uuid(), nullable=False),
        sa.Column('tag_id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['item_id'], ['items.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tag_id'], ['tags.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('item_id', 'tag_id'),
    )
    op.create_index('ix_item_tags_tag_id', 'item_tags', ['tag_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_item_tags_tag_id', table_name='item_tags')
    op.drop_table('item_tags')
    op.drop_index('uq_tags_user_id_lower_name', table_name='tags')
    op.drop_table('tags')
    op.create_table(
        'item_tags',
        sa.Column('item_id', sa.Uuid(), nullable=False),
        sa.Column('tag', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['item_id'], ['items.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('item_id', 'tag'),
    )
