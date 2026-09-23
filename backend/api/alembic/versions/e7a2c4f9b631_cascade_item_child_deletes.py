"""cascade item child deletes

Revision ID: e7a2c4f9b631
Revises: d1e5f3a7c920
Create Date: 2026-09-24 04:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7a2c4f9b631'
down_revision: Union[str, Sequence[str], None] = 'd1e5f3a7c920'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Every table hanging off `items` by `item_id`. Deleting an item deletes its
# rows in all of them, in the database, so no code path can leave orphans.
_CHILD_TABLES = [
    'item_text_contents',
    'item_images',
    'item_descriptions',
    'item_tags',
    'item_embeddings',
]


def _recreate_item_fks(*, ondelete: str | None) -> None:
    for table in _CHILD_TABLES:
        name = f'{table}_item_id_fkey'
        op.drop_constraint(name, table, type_='foreignkey')
        op.create_foreign_key(name, table, 'items', ['item_id'], ['id'], ondelete=ondelete)


def upgrade() -> None:
    """Upgrade schema."""
    _recreate_item_fks(ondelete='CASCADE')


def downgrade() -> None:
    """Downgrade schema."""
    _recreate_item_fks(ondelete=None)
