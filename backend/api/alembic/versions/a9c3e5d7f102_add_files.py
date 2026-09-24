"""add files

Revision ID: a9c3e5d7f102
Revises: f3b8d1e6a274
Create Date: 2026-09-24 06:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a9c3e5d7f102'
down_revision: Union[str, Sequence[str], None] = 'f3b8d1e6a274'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Same constraint as the 'link' migration: a new enum value can't be
    # added inside a transaction.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE item_type ADD VALUE 'file'")

    op.create_table(
        'item_files',
        sa.Column('item_id', sa.Uuid(), nullable=False),
        sa.Column('storage_key', sa.String(), nullable=False),
        sa.Column('filename', sa.String(), nullable=False),
        sa.Column('content_type', sa.String(), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['item_id'], ['items.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('item_id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('item_files')
    # The 'file' enum value can't be dropped (see the 'link' migration).
