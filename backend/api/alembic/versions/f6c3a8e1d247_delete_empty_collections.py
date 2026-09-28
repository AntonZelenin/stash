"""delete empty collections

Revision ID: f6c3a8e1d247
Revises: e2b6d9f4a158
Create Date: 2026-09-28 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'f6c3a8e1d247'
down_revision: Union[str, Sequence[str], None] = 'e2b6d9f4a158'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Collections now exist only while some item is in them; ones emptied
    # before that rule are removed.
    op.execute(
        'DELETE FROM collections WHERE NOT EXISTS '
        '(SELECT 1 FROM item_collections WHERE item_collections.collection_id = collections.id)'
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Deleted collections held no items; nothing to restore.
