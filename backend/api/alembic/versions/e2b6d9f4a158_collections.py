"""collections

Revision ID: e2b6d9f4a158
Revises: d4a8e2f6c193
Create Date: 2026-09-28 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e2b6d9f4a158'
down_revision: Union[str, Sequence[str], None] = 'd4a8e2f6c193'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'collections',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    # One collection per name per user, ignoring case; also serves the
    # per-user name lookups and listing.
    op.create_index(
        'uq_collections_user_id_lower_name', 'collections', ['user_id', sa.text('lower(name)')], unique=True
    )

    op.create_table(
        'item_collections',
        sa.Column('item_id', sa.Uuid(), nullable=False),
        sa.Column('collection_id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['item_id'], ['items.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['collection_id'], ['collections.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('item_id', 'collection_id'),
    )
    op.create_index('ix_item_collections_collection_id', 'item_collections', ['collection_id'])

    # Collections to put an upload's item in once it's finalized.
    op.add_column(
        'pending_uploads',
        sa.Column('collection_names', sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('pending_uploads', 'collection_names')
    op.drop_index('ix_item_collections_collection_id', table_name='item_collections')
    op.drop_table('item_collections')
    op.drop_index('uq_collections_user_id_lower_name', table_name='collections')
    op.drop_table('collections')
