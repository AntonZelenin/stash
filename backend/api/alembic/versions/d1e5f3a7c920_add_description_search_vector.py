"""add description search vector

Revision ID: d1e5f3a7c920
Revises: 3f9a1c2d7e45
Create Date: 2026-09-24 03:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd1e5f3a7c920'
down_revision: Union[str, Sequence[str], None] = '3f9a1c2d7e45'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Generated + stored, so Postgres keeps it in sync with `text` on every
    # insert/update and no application code can forget to. 'english' gives
    # stemming ("cats" matches "cat") for the English AI-generated image
    # descriptions; words it has no stemmer for (e.g. other languages in
    # notes) are still indexed, just lowercased as-is.
    #
    # The text is indexed twice: as-is, and with punctuation turned into
    # spaces. Postgres' parser keeps a URL's host and path as whole tokens
    # ("github.com", "/rust-lang/rust"), so on its own "rust" wouldn't match
    # a saved link to github.com/rust-lang/rust; the second pass splits it
    # into plain words while the first keeps the original tokens searchable.
    op.execute(
        "ALTER TABLE item_descriptions ADD COLUMN search_vector tsvector "
        "GENERATED ALWAYS AS ("
        "to_tsvector('english', text) || "
        "to_tsvector('english', regexp_replace(text, '[[:punct:]]+', ' ', 'g'))"
        ") STORED"
    )
    op.create_index(
        'ix_item_descriptions_search_vector',
        'item_descriptions',
        ['search_vector'],
        postgresql_using='gin',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_item_descriptions_search_vector', table_name='item_descriptions')
    op.drop_column('item_descriptions', 'search_vector')
