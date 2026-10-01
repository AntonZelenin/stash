"""storage reconciliation

Revision ID: c5d1a8e3f902
Revises: b2c7e9f4a031
Create Date: 2026-10-01 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5d1a8e3f902'
down_revision: Union[str, Sequence[str], None] = 'b2c7e9f4a031'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Every column that holds a stored object's key: the reconciliation scan
# (`app.storage.reconciliation`) looks up each listed key in all of them.
_KEY_INDEXES = [
    ('ix_item_images_storage_key', 'item_images', 'storage_key'),
    ('ix_item_images_thumbnail_key', 'item_images', 'thumbnail_key'),
    ('ix_item_files_storage_key', 'item_files', 'storage_key'),
]


def upgrade() -> None:
    """Upgrade schema."""
    for name, table, column in _KEY_INDEXES:
        op.create_index(name, table, [column])
    # Where the scan of each prefix resumes.
    op.create_table(
        'storage_reconciliation',
        sa.Column('prefix', sa.String(), nullable=False),
        sa.Column('cursor', sa.String(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('prefix'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('storage_reconciliation')
    for name, table, _ in reversed(_KEY_INDEXES):
        op.drop_index(name, table_name=table)
