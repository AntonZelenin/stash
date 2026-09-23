"""backfill text item descriptions

Revision ID: c4d7e9a2b815
Revises: 8b2e4d6f1a93
Create Date: 2026-09-24 02:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4d7e9a2b815'
down_revision: Union[str, Sequence[str], None] = '8b2e4d6f1a93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Text/link items now store their text as their description too, so
    # `item_descriptions` is the single source search reads from. Backfill
    # the ones created before that.
    op.execute(
        "INSERT INTO item_descriptions (item_id, text) "
        "SELECT item_id, text FROM item_text_contents "
        "ON CONFLICT (item_id) DO NOTHING"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "DELETE FROM item_descriptions d USING item_text_contents t "
        "WHERE d.item_id = t.item_id AND d.text = t.text"
    )
