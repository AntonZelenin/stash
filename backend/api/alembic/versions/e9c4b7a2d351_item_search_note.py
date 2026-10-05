"""item search note

Revision ID: e9c4b7a2d351
Revises: d8b3f1a6c294
Create Date: 2026-10-05 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e9c4b7a2d351'
down_revision: Union[str, Sequence[str], None] = 'd8b3f1a6c294'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # The user's optional extra context for search, kept apart from the
    # caption (shown with the item) and the generated description. Existing
    # items have none.
    op.add_column('items', sa.Column('search_note', sa.String(), nullable=True))
    # Carried from the start of an upload to the item finalize creates.
    op.add_column('pending_uploads', sa.Column('search_note', sa.String(), nullable=True))
    # Indexed like `item_descriptions.search_vector` (as-is, and with
    # punctuation turned into spaces), weighted 'A' so a match in the
    # user's own note ranks above one in a description (weight 'D', the
    # default) when the two are ranked together. NULL without a note.
    op.execute(
        "ALTER TABLE items ADD COLUMN search_note_vector tsvector "
        "GENERATED ALWAYS AS (setweight("
        "to_tsvector('english', search_note) || "
        "to_tsvector('english', regexp_replace(search_note, '[[:punct:]]+', ' ', 'g')), "
        "'A')) STORED"
    )
    op.create_index(
        'ix_items_search_note_vector',
        'items',
        ['search_note_vector'],
        postgresql_using='gin',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_items_search_note_vector', table_name='items')
    op.drop_column('items', 'search_note_vector')
    op.drop_column('pending_uploads', 'search_note')
    op.drop_column('items', 'search_note')
