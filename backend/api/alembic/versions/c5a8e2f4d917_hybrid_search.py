"""hybrid search: description full-text + pg_trgm

Revision ID: c5a8e2f4d917
Revises: a2d7f4c9e813
Create Date: 2026-09-26 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c5a8e2f4d917'
down_revision: Union[str, Sequence[str], None] = 'a2d7f4c9e813'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Search matches the user's own text (notes, links, captions) against
    # the query by trigram similarity (`word_similarity`): language-neutral,
    # and tolerant of inflections ("город" finds "городу") and typos. No
    # index: it's a scan of one user's items, like the filename match.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # Full-text search over the descriptions, alongside the semantic one,
    # so a literal word ("city") finds a description containing it even
    # when the embedding scores it too far. Brought back as defined in
    # d1e5f3a7c920 (b7e2f4a9c613 dropped it when semantic search replaced
    # full-text): generated + stored, 'english' for the English generated
    # descriptions, and indexed a second time with punctuation turned into
    # spaces so a link's host/path words are searchable on their own.
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
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
